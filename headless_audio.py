"""Windows 音频后台采集。只打开声音设备，进程无需浏览器或窗口。"""

from array import array
import base64
from collections import deque
import io
import math
import queue
import sys
import threading
import wave

from interview import InterviewError, REALTIME_STT, recognize_clip
from realtime_asr import RealtimeASR

RATE = 16000
BLOCK = RATE // 10
SILENCE = bytes(BLOCK * 2)


def _driver():
    try:
        import pyaudiowpatch
    except ImportError as exc:
        raise InterviewError("请先安装 Windows 音频依赖：python -m pip install -r requirements.txt") from exc
    return pyaudiowpatch


class PcmConverter:
    """按连续采样转换为 16kHz 单声道，跨数据块保留重采样相位。"""

    def __init__(self, sample_rate, channels):
        self.rate = sample_rate
        self.channels = channels
        self.phase = self.total = self.count = 0

    def convert(self, raw):
        if self.rate == RATE and self.channels == 1:
            return raw
        source = array("h", raw)
        if sys.byteorder != "little":
            source.byteswap()
        output = array("h")
        for start in range(0, len(source), self.channels):
            frame = source[start:start + self.channels]
            self.total += sum(frame) // len(frame)
            self.count += 1
            self.phase += RATE
            if self.phase >= self.rate:
                sample = self.total // self.count
                while self.phase >= self.rate:
                    output.append(sample)
                    self.phase -= self.rate
                self.total = self.count = 0
        if sys.byteorder != "little":
            output.byteswap()
        return output.tobytes()


class Utterance:
    """整段 ASR 的本地断句，保留少量前导音频和尾音。"""

    def __init__(self, silence_ms):
        self.silence_samples = RATE * silence_ms // 1000
        self.chunks = deque(maxlen=2)
        self.voiced = self.silent = self.samples = 0

    def add(self, pcm):
        values = array("h", pcm)
        count = len(values)
        loud = count and math.sqrt(sum(v * v for v in values) / count) > 393
        if loud:
            if not self.voiced:
                self.samples = sum(len(chunk) // 2 for chunk in self.chunks)
                self.chunks = list(self.chunks)
            self.voiced += count
            self.silent = 0
        elif self.voiced:
            self.silent += count
        self.chunks.append(pcm)
        if self.voiced:
            self.samples += count
            if self.silent >= self.silence_samples or self.samples >= RATE * 12:
                return self.flush()
        return None

    def flush(self):
        if self.voiced >= RATE * 350 // 1000:
            pcm = b"".join(self.chunks)
            extra = max(0, self.silent - RATE * 150 // 1000) * 2
            audio = io.BytesIO()
            with wave.open(audio, "wb") as wav:
                wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                wav.writeframes(pcm[:-extra] if extra else pcm)
            result = audio.getvalue()
        else:
            result = None
        self.chunks = deque(maxlen=2)
        self.samples = self.voiced = self.silent = 0
        return result


class HeadlessCapture:
    def __init__(self, speech, source, language, silence_ms, submit):
        self.speech = speech
        self.language = language
        self.silence_ms = silence_ms
        self.submit = submit
        self.session_id = None
        self.audio = queue.Queue(maxsize=30)
        self.turns = queue.Queue()
        self.stopping = threading.Event()
        self.aborted = threading.Event()
        self.done = threading.Event()
        self.device = None
        self.realtime = None
        self.preview = None
        self.error = ""
        self.dropped = 0
        self.latency_ms = None
        self.outputs = (False, False)

        driver = _driver()
        try:
            self.device = driver.PyAudio()
            item = (self.device.get_default_input_device_info() if source == "mic"
                    else self.device.get_default_wasapi_loopback())
            self.source = item["name"].strip()
            rate = round(item["defaultSampleRate"])
            channels = min(2, int(item["maxInputChannels"])) if source == "mic" else int(item["maxInputChannels"])
            if rate <= 0 or channels <= 0:
                raise ValueError("invalid audio input format")
            self.converter = PcmConverter(rate, channels)

            def capture(data, frame_count, time_info, flags):
                if data and not self.stopping.is_set():
                    try:
                        self.audio.put_nowait(data)
                    except queue.Full:
                        self.dropped += 1
                return (None, driver.paContinue)

            self.stream = self.device.open(
                format=driver.paInt16, channels=channels, rate=rate, input=True,
                input_device_index=item["index"], frames_per_buffer=rate // 10,
                stream_callback=capture, start=False)
            if speech.get("type") == REALTIME_STT:
                self.realtime = RealtimeASR(speech, language, silence_ms)
            self.stream.start_stream()
        except (OSError, LookupError, ValueError) as exc:
            self.release()
            raise InterviewError("无法打开电脑音频设备，请检查 Windows 的默认扬声器 / 麦克风") from exc
        except Exception:
            self.release()
            raise

    def start(self):
        self.asr_worker = threading.Thread(target=self._recognize, daemon=True)
        self.answer_worker = threading.Thread(target=self._submit, daemon=True)
        self.answer_worker.start()
        self.asr_worker.start()

    def release(self):
        if self.realtime:
            self.realtime.close()
        stream = getattr(self, "stream", None)
        if stream is not None:
            self.stream = None
            try:
                stream.close()
            except OSError:
                pass
        if self.device is not None:
            device = self.device
            self.device = None
            try:
                device.terminate()
            except OSError:
                pass

    def state(self):
        return {"recording": not self.stopping.is_set(), "source": self.source,
                "error": self.error, "dropped": self.dropped,
                "audio_queue": self.audio.qsize(), "turn_queue": self.turns.qsize(),
                "latency_ms": self.latency_ms, "streaming": bool(self.realtime)}

    def _final(self, text, lang):
        self.preview = {"type": "final", "text": text}
        self.turns.put((text, lang, self.outputs))

    def _events(self, result):
        for event in result["events"]:
            kind = event["type"]
            if kind == "final":
                self.latency_ms = event.get("latency_ms")
                self._final(event["text"], event["lang"])
            elif kind == "partial":
                self.preview = event
            elif kind in ("error", "notice"):
                self.error = event.get("message") or "流式语音识别异常"

    def _batch(self, raw):
        if not raw:
            return
        try:
            result = recognize_clip(raw, self.speech, self.language)
        except Exception as exc:
            self.error = "语音识别失败（%s）" % type(exc).__name__
            return
        self.latency_ms = result["latency_ms"]
        if result["text"]:
            self._final(result["text"], result["lang"])

    def _recognize(self):
        utterance = Utterance(self.silence_ms)
        try:
            while not self.aborted.is_set():
                try:
                    raw = self.audio.get(timeout=.1) if not self.stopping.is_set() else self.audio.get_nowait()
                    pcm = self.converter.convert(raw)
                except queue.Empty:
                    if self.stopping.is_set():
                        break
                    if not self.stream.is_active():
                        raise InterviewError("电脑音频设备已断开")
                    pcm = SILENCE
                if self.realtime:
                    result = self.realtime.push(base64.b64encode(pcm).decode("ascii"))
                    self._events(result)
                    if result["closed"]:
                        raise InterviewError(self.error or "流式 ASR 连接已断开")
                else:
                    self._batch(utterance.add(pcm))
            if not self.aborted.is_set():
                if self.realtime:
                    result = self.realtime.finish()
                    self._events(result)
                    if not result["finalized"]:
                        self.error = "流式 ASR 未确认最后一段"
                else:
                    self._batch(utterance.flush())
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, InterviewError) else "电脑音频采集已中断"
        finally:
            self.stopping.set()
            try:
                self.release()
            finally:
                self.turns.put(None)

    def _submit(self):
        try:
            while True:
                turn = self.turns.get()
                if turn is None or self.aborted.is_set():
                    return
                text, lang, (translate, answers) = turn
                try:
                    self.submit({"text": text, "lang": lang, "session_id": self.session_id,
                                 "translate": translate, "answers": answers})
                except Exception:
                    if not self.aborted.is_set():
                        self.error = "一条识别结果未能写入会话"
        finally:
            self.done.set()

    def stop(self, save):
        if not save:
            self.aborted.set()
        self.stopping.set()
        if not save and self.realtime:
            self.realtime.close()
        if save and not self.done.wait(40):
            self.aborted.set()
            if self.realtime:
                self.realtime.close()
            return "剩余结果处理超时，已保存已完成的记录"
        return self.error if save else ""
