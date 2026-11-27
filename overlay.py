"""面试时用的隐身提示窗。

窗口由本进程（tkinter）创建，因此可以调用
SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)：
本机能看见，屏幕共享 / 录屏 / 截图拍不到。这是 Windows 允许捕获排除的
唯一零依赖形态——Edge / Chrome 的 --app 窗口属于别的进程，做不到。

只显示最近一次生成的回答，不做配置；配置界面在浏览器里，面试前关掉。
    python overlay.py --url http://127.0.0.1:8765 --name Notes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import stealth

BG = "#f5f5f5"
FG = "#1b1b1b"
MUTED = "#777777"
LINE = "#d8d8d8"
_opener = build_opener(ProxyHandler({}))


def _fetch(url, token=""):
    headers = {"X-Copilot-Token": token} if token else {}
    req = Request(url, headers=headers)
    with _opener.open(req, timeout=1.2) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Overlay:
    def __init__(self, base_url: str, display_name: str, token: str = "",
                 poll_ms: int = 1200):
        import tkinter as tk
        self.tk = tk
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.poll_ms = poll_ms
        self.display_name = display_name or stealth.DEFAULT_DISPLAY_NAME
        self.last_ts = None
        self.protected = False
        self.protection_note = ""

        root = tk.Tk()
        self.root = root
        root.title(self.display_name)
        root.overrideredirect(True)          # 无边框：不进任务栏 / Alt+Tab
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.96)
        root.configure(bg=BG)
        width, height = 470, 300
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (width, height,
                                       max(0, sw - width - 24),
                                       max(0, sh - height - 64)))

        bar = tk.Frame(root, bg=BG, height=26)
        bar.pack(fill="x", side="top")
        title = tk.Label(bar, text=self.display_name, bg=BG, fg=MUTED,
                         font=("Microsoft YaHei", 9))
        title.pack(side="left", padx=9)
        self.badge = tk.Label(bar, text="", bg=BG, fg=MUTED,
                              font=("Microsoft YaHei", 9))
        self.badge.pack(side="left", padx=6)
        close = tk.Label(bar, text="×", bg=BG, fg=FG, cursor="hand2",
                         font=("Microsoft YaHei", 13))
        close.pack(side="right", padx=9)
        close.bind("<Button-1>", lambda e: root.destroy())

        for widget in (bar, title):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
        self._drag = None

        sep = tk.Frame(root, bg=LINE, height=1)
        sep.pack(fill="x")

        self.text = tk.Text(root, wrap="word", bg=BG, fg=FG,
                            font=("Microsoft YaHei", 10), relief="flat",
                            padx=12, pady=10, spacing1=3, spacing3=6)
        self.text.pack(fill="both", expand=True)
        self.text.configure(state="disabled")
        self._write([("等待面试官提问", "head"),
                     ("回答生成后会显示在这里。配置界面请在浏览器里使用，"
                      "面试前关掉。", "body")])
        root.update_idletasks()
        root.update()
        self._protect()

    def _drag_start(self, event):
        self._drag = (event.x_root, event.y_root,
                      self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event):
        if not self._drag:
            return
        x0, y0, wx, wy = self._drag
        self.root.geometry("+%d+%d" % (wx + event.x_root - x0,
                                       wy + event.y_root - y0))

    def _protect(self):
        """窗口是本进程的，捕获排除在这里才可能成功。"""
        # tkinter 还会创建 TtkMonitorWindow / IME 等隐藏窗口，必须按标题精确匹配，
        # 否则会把 affinity 设到错误的窗口上（自进程调用不会报错，很难发现）。
        rows = [r for r in stealth.enum_windows(pid=os.getpid(),
                                                 visible_only=False)
                if r["own"] and r["visible"]
                and r["title"] == self.display_name]
        if not rows:
            self.protection_note = "未找到提示窗句柄，捕获排除未生效"
            return
        ok, note = stealth.set_capture_exclusion(rows[0]["hwnd"], True)
        self.protected = ok
        self.protection_note = note
        self.badge.configure(
            text="共享不可见" if self.protected else "共享可能可见",
            fg="#2c6b2c" if self.protected else "#a02c2c")

    def _write(self, blocks):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for content, kind in blocks:
            if kind == "head":
                t.insert("end", content + "\n", "head")
            elif kind == "label":
                t.insert("end", content + "\n", "label")
            else:
                t.insert("end", content + "\n\n", "body")
        t.tag_config("head", font=("Microsoft YaHei", 11, "bold"),
                     foreground=FG)
        t.tag_config("label", font=("Microsoft YaHei", 9),
                     foreground=MUTED)
        t.tag_config("body", foreground=FG)
        t.configure(state="disabled")

    def _poll(self):
        try:
            data = _fetch(self.base_url + "/api/answer/latest", self.token)
            answer = data.get("answer")
            if answer and answer.get("ts") != self.last_ts:
                self.last_ts = answer.get("ts")
                blocks = [("问题", "label"), (answer.get("question") or "—", "body")]
                if answer.get("answer_zh"):
                    blocks += [("中文回答", "label"),
                               (answer["answer_zh"], "body")]
                if answer.get("answer_en"):
                    blocks += [("English", "label"),
                               (answer["answer_en"], "body")]
                stamp = time.strftime("%H:%M:%S",
                                      time.localtime(self.last_ts or time.time()))
                blocks += [("更新 " + stamp + " · " +
                            ("共享不可见" if self.protected else "共享可能可见"),
                            "label")]
                self._write(blocks)
        except Exception:
            pass
        self.root.after(self.poll_ms, self._poll)

    def run(self):
        self.root.after(300, self._poll)
        self.root.mainloop()


def run_overlay(base_url, display_name=None, token="", poll_ms=1200):
    """在主线程运行；返回时表示用户关掉了提示窗。"""
    if not stealth.IS_WINDOWS:
        raise RuntimeError("隐身提示窗目前只支持 Windows")
    Overlay(base_url, display_name or stealth.DEFAULT_DISPLAY_NAME,
            token, poll_ms).run()


def main(argv=None):
    parser = argparse.ArgumentParser(description="隐身提示窗")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--name", default=stealth.DEFAULT_DISPLAY_NAME)
    parser.add_argument("--token", default="")
    args = parser.parse_args(argv)
    run_overlay(args.url, args.name, args.token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
