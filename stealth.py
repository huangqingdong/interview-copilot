"""Windows 窗口隐身能力层。ctypes，零第三方依赖。

实测约束（本机 Windows 10 build 19045）：
    SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    只能由「窗口所属进程」调用。对外部进程窗口（如 Edge / Chrome 的
    --app 窗口）调用直接返回 FALSE；对自己进程创建的窗口返回 TRUE。

这条约束决定了 app 的形态：想让屏幕共享拍不到，只有两条路
    1) 自己进程创建窗口（overlay.py 的 tkinter 提示窗），再设捕获排除；
    2) 干脆不开窗口，界面放到手机 / 第二设备。

本模块对外部窗口只做「减少痕迹」的操作，并如实报告结果，不谎报隐身：
    改窗口标题、WS_EX_TOOLWINDOW（不进任务栏 / Alt+Tab）、显示 / 隐藏。
这些都不等于从捕获中排除，调用方必须把区别显示给用户。

CLI：
    python stealth.py list              列出可见顶层窗口及可隐身状态
    python stealth.py probe             报告本机能力与跨进程限制
"""
from __future__ import annotations

import os
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001           # Win7+，被捕获时显示为黑块
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Win10 2004+ (build 19041)，从捕获中剔除
# Winuser.h：GWL_EXSTYLE / GWLP_EXSTYLE 都是 -20；-16 是 GWL_STYLE，别混用
GWL_EXSTYLE = GWLP_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
SW_HIDE = 0
SW_SHOW = 5

# 默认中性显示名。屏幕共享时窗口标题、任务栏、Alt+Tab 里都不该出现
# 能让人联想到「面试辅助」的字样；用户可在界面上改。
DEFAULT_DISPLAY_NAME = "Notes"

if IS_WINDOWS:
    import ctypes

    _u = ctypes.WinDLL("user32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)

    _u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    _u.EnumWindows.restype = wintypes.BOOL
    _u.IsWindowVisible.argtypes = [wintypes.HWND]
    _u.IsWindowVisible.restype = wintypes.BOOL
    _u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    _u.SetWindowTextW.restype = wintypes.BOOL
    _u.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    _u.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u.ShowWindow.restype = wintypes.BOOL
    _u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
    _u.SetWindowPos.restype = wintypes.BOOL

    _set_long = getattr(_u, "SetWindowLongPtrW", None) or _u.SetWindowLongW
    _get_long = getattr(_u, "GetWindowLongPtrW", None) or _u.GetWindowLongW
    _set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _set_long.restype = ctypes.c_ssize_t
    _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    _get_long.restype = ctypes.c_ssize_t

    # GetWindowDisplayAffinity 在老系统上可能不存在
    _get_affinity = getattr(_u, "GetWindowDisplayAffinity", None)
    if _get_affinity:
        _get_affinity.argtypes = [wintypes.HWND,
                                  ctypes.POINTER(wintypes.DWORD)]
        _get_affinity.restype = wintypes.BOOL
else:
    ctypes = None
    _u = _k = None
    _get_affinity = None


def build_number() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except (AttributeError, ValueError):
        return 0


def capture_exclusion_supported() -> bool:
    """WDA_EXCLUDEFROMCAPTURE 需要 Win10 2004 (19041)+。"""
    return IS_WINDOWS and build_number() >= 19041


def affinity_flag() -> int:
    """老系统退回 WDA_MONITOR：共享里是黑块，但至少不会露出内容。"""
    return (WDA_EXCLUDEFROMCAPTURE if capture_exclusion_supported()
            else WDA_MONITOR if IS_WINDOWS else WDA_NONE)


def _last_error_text() -> str:
    if not IS_WINDOWS:
        return "非 Windows"
    code = ctypes.get_last_error()
    return "Win32 error %d" % code if code else "调用被拒绝（跨进程限制）"


def _window_title(hwnd) -> str:
    n = _u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def enum_windows(pid: int | None = None, visible_only: bool = True) -> list[dict]:
    """列出顶层窗口。pid 为 None 时列全部；own=True 表示属于当前进程。"""
    if not IS_WINDOWS:
        return []
    out: list[dict] = []
    me = os.getpid()
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        if visible_only and not _u.IsWindowVisible(hwnd):
            return True
        owner = wintypes.DWORD()
        _u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if pid is not None and owner.value != pid:
            return True
        title = _window_title(hwnd)
        if visible_only and not title:
            return True
        try:
            exstyle = int(_get_long(hwnd, GWL_EXSTYLE))
        except Exception:
            exstyle = 0
        aff = None
        if _get_affinity:
            value = wintypes.DWORD()
            if _get_affinity(hwnd, ctypes.byref(value)):
                aff = int(value.value)
        out.append({
            "hwnd": int(hwnd), "pid": int(owner.value), "title": title,
            "own": owner.value == me, "visible": bool(_u.IsWindowVisible(hwnd)),
            "exstyle": exstyle, "toolwindow": bool(exstyle & WS_EX_TOOLWINDOW),
            "affinity": aff,
            "capture_excluded": aff in (WDA_MONITOR, WDA_EXCLUDEFROMCAPTURE),
        })
        return True

    cb = cb_type(callback)
    _u.EnumWindows(cb, 0)
    return out


def get_affinity(hwnd) -> int | None:
    if not IS_WINDOWS or not _get_affinity:
        return None
    value = wintypes.DWORD()
    if _get_affinity(wintypes.HWND(hwnd), ctypes.byref(value)):
        return int(value.value)
    return None


def set_capture_exclusion(hwnd, enabled: bool = True) -> tuple[bool, str]:
    """从屏幕共享 / 录屏 / 截图中排除窗口。仅自进程窗口可成功。"""
    if not IS_WINDOWS:
        return False, "非 Windows 平台，无捕获排除能力"
    flag = affinity_flag() if enabled else WDA_NONE
    ctypes.set_last_error(0)
    ok = bool(_u.SetWindowDisplayAffinity(wintypes.HWND(hwnd), flag))
    if ok:
        return True, ("已排除捕获" if enabled else "已恢复可见")
    return False, _last_error_text()


def set_title(hwnd, title: str) -> bool:
    if not IS_WINDOWS:
        return False
    ctypes.set_last_error(0)
    return bool(_u.SetWindowTextW(wintypes.HWND(hwnd), str(title)))


def set_toolwindow(hwnd, enabled: bool = True) -> bool:
    """切换 WS_EX_TOOLWINDOW：不出现在任务栏和 Alt+Tab。跨进程通常允许。"""
    if not IS_WINDOWS:
        return False
    h = wintypes.HWND(hwnd)
    ctypes.set_last_error(0)
    style = int(_get_long(h, GWL_EXSTYLE))
    if style == 0 and ctypes.get_last_error():
        return False
    if enabled:
        style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    else:
        style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
    _set_long(h, GWL_EXSTYLE, style)
    # SWP_FRAMECHANGED 让任务栏重新读取窗口样式
    _u.SetWindowPos(h, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
    return bool(int(_get_long(h, GWL_EXSTYLE)) & WS_EX_TOOLWINDOW) == enabled


def show_window(hwnd, visible: bool) -> bool:
    """SW_HIDE 是兜底手段：连自己都看不到，但共享里也一定没有。"""
    if not IS_WINDOWS:
        return False
    return bool(_u.ShowWindow(wintypes.HWND(hwnd), SW_SHOW if visible else SW_HIDE))


class WindowShield:
    """记录对外部窗口做过的修改，退出时还原，避免残留痕迹或改坏用户窗口。"""

    def __init__(self):
        self._originals: dict[int, dict] = {}

    def _remember(self, hwnd):
        key = int(hwnd)
        if key in self._originals:
            return
        for row in enum_windows(visible_only=False):
            if row["hwnd"] == key:
                self._originals[key] = {
                    "title": row["title"],
                    "toolwindow": row["toolwindow"],
                    "affinity": row["affinity"],
                    "visible": row["visible"],
                }
                break

    def mask(self, hwnd, display_name: str) -> dict:
        """对外部窗口做尽力隐藏：中性标题 + 移出任务栏/Alt+Tab。"""
        self._remember(hwnd)
        result = {"hwnd": int(hwnd), "title": False,
                  "toolwindow": False, "capture_exclusion": False,
                  "note": ""}
        if not IS_WINDOWS:
            result["note"] = "非 Windows 平台"
            return result
        result["title"] = set_title(hwnd, display_name)
        result["toolwindow"] = set_toolwindow(hwnd, True)
        ok, why = set_capture_exclusion(hwnd, True)
        result["capture_exclusion"] = ok
        if ok:
            result["note"] = "窗口已排除捕获"
        else:
            result["note"] = (
                "该窗口不属于本进程，Windows 拒绝设置捕获排除；"
                "只做了标题与任务栏隐藏。共享整个屏幕时窗口内容仍可能被拍到，"
                "请改用「后台无窗口」或「隐身提示窗」模式。")
        return result

    def protect_own(self, hwnd) -> dict:
        """自进程窗口：可以真正排除捕获。"""
        self._remember(hwnd)
        ok, why = set_capture_exclusion(hwnd, True)
        return {"hwnd": int(hwnd), "capture_exclusion": ok, "note": why}

    def hide(self, hwnd) -> bool:
        return show_window(hwnd, False)

    def unhide(self, hwnd) -> bool:
        return show_window(hwnd, True)

    def restore(self):
        for hwnd, old in self._originals.items():
            try:
                if old.get("title") is not None:
                    set_title(hwnd, old["title"])
                if old.get("toolwindow") is False:
                    set_toolwindow(hwnd, False)
                if old.get("affinity") in (None, WDA_NONE):
                    set_capture_exclusion(hwnd, False)
                show_window(hwnd, old.get("visible", True))
            except Exception:
                pass
        self._originals.clear()


def capabilities() -> dict:
    return {
        "platform": sys.platform,
        "build": build_number(),
        "windows": IS_WINDOWS,
        "capture_exclusion_supported": capture_exclusion_supported(),
        "flag": "WDA_EXCLUDEFROMCAPTURE" if capture_exclusion_supported()
                else "WDA_MONITOR" if IS_WINDOWS else "none",
        "self_owned_only": True,
        "note": ("捕获排除只能作用于本进程创建的窗口；跨进程调用被 Windows 拒绝。"
                 if IS_WINDOWS else "当前平台无窗口隐身 API 支持。"),
    }


def status(pid: int | None = None) -> dict:
    caps = capabilities()
    rows = enum_windows(pid=pid)
    return {
        "capabilities": caps,
        "windows": rows,
        "own_windows": [r for r in rows if r["own"]],
        "excluded_count": sum(1 for r in rows if r["capture_excluded"]),
    }


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description="窗口隐身能力探测")
    parser.add_argument("cmd", nargs="?", default="probe",
                        choices=["list", "probe"])
    parser.add_argument("--pid", type=int)
    args = parser.parse_args(argv)
    if args.cmd == "list":
        print(json.dumps(enum_windows(pid=args.pid), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({**capabilities(),
                          "own_windows": enum_windows(pid=os.getpid())},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
