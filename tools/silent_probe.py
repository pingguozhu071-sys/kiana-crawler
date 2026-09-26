# -*- coding: utf-8 -*-
"""NSIS 静默测试探针（v2.17.1.1 检修工具）。

静默模式安装器若发生 fatal error，NSIS 会弹错误框（前几轮"桌面莫名其妙弹窗"来源）。
本工具以 CreateProcessW 启动安装器（无 shell 解释），每 200ms 探测其窗口，
命中即抓取窗口标题与控件文本，并自动 WM_CLOSE 关闭，测试不再干扰桌面。

用法：
  python tools/silent_probe.py <setup.exe> [args...]
退出码：安装器退出码（弹窗被关=0）。捕获到的弹窗文本与 install 成败见输出。
"""
import ctypes
import ctypes.wintypes as wt
import sys
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

EnumWindows = user32.EnumWindows
EnumChildWindows = user32.EnumChildWindows
GetWindowTextW = user32.GetWindowTextW
GetClassNameW = user32.GetClassNameW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
PostMessageW = user32.PostMessageW

GetExitCodeProcess = kernel32.GetExitCodeProcess
CloseHandle = kernel32.CloseHandle
WaitForSingleObject = kernel32.WaitForSingleObject
INFINITE = 0xFFFFFFFF


class _SI(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.POINTER(wt.BYTE)), ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
    ]


class _PI(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def _pid_of(hwnd):
    pid = wt.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _text(hwnd, fn=GetWindowTextW):
    buf = ctypes.create_unicode_buffer(1024)
    fn(hwnd, buf, 1024)
    return buf.value


def _all_texts(hwnd):
    texts = [_text(hwnd)]
    CHILD_CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, l):
        t = _text(h, user32.GetWindowTextW)
        c = _text(h, GetClassNameW)
        if t:
            texts.append(f"[{c}] {t}")
        return True

    EnumChildWindows(hwnd, CHILD_CB(cb), 0)
    return [t for t in texts if t.strip()]


def _enum_windows():
    out = []

    def cb(hwnd, l):
        out.append(hwnd)
        return True

    EnumWindows(ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(cb), 0)
    return out


def _spawn(executable, args):
    """CreateProcessW 直接启动；args 列表经 list2cmdline 转原始命令行（无 shell 参与）"""
    import subprocess as _sp
    cmdline = _sp.list2cmdline([executable] + list(args))
    si = _SI()
    si.cb = ctypes.sizeof(_SI)
    pi = _PI()
    ok = kernel32.CreateProcessW(None, cmdline, None, None, False, 0, None, None,
                                 ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise OSError(ctypes.get_last_error(), "CreateProcessW failed")
    return pi


def probe(executable, args, poll=0.2, timeout=900):
    pi = _spawn(executable, args)
    target_pid = pi.dwProcessId
    seen = set()
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc = WaitForSingleObject(pi.hProcess, 0)
        if rc != 0x102:  # WAIT_TIMEOUT → 进程仍运行
            code = wt.DWORD()
            GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
            CloseHandle(pi.hThread)
            CloseHandle(pi.hProcess)
            return code.value
        for hwnd in _enum_windows():
            if _pid_of(hwnd) == target_pid:
                title = _text(hwnd)
                if title and hwnd not in seen:
                    seen.add(hwnd)
                    texts = _all_texts(hwnd)
                    print("═══ 弹窗捕获 ═══")
                    for t in texts:
                        print("  " + t)
                    # 直接杀进程：WM_CLOSE 会触发 onUserAbort 连环弹窗，绝不能用在探针里
                    kernel32.TerminateProcess(pi.hProcess, 33)
                    CloseHandle(pi.hThread)
                    CloseHandle(pi.hProcess)
                    return 33
        time.sleep(poll)
    kernel32.TerminateProcess(pi.hProcess, 1)
    CloseHandle(pi.hThread)
    CloseHandle(pi.hProcess)
    return -1


if __name__ == "__main__":
    # 关键：禁止从 shell argv 传 "/S"——Git Bash(MSYS2) 会把 "/S" 转成 "S:/"，
    # NSIS 收不到参数就弹 GUI。"/S"、"/LANG" 一律由本程序生成。
    setup = sys.argv[1]
    args = []
    i = 2
    while i < len(sys.argv):
        a = sys.argv[i]
        if a == "--silent":
            args.append("/S")
        elif a.startswith("--lang="):
            args.append(f"/LANG={a.split('=', 1)[1]}")
        elif a.startswith("--dir="):
            args.append(f"/D={a.split('=', 1)[1]}")
        else:
            args.append(a)
        i += 1
    print("命令参数:", [setup] + args)
    rc = probe(setup, args)
    print(f"PROBE_EXIT={rc}")
    sys.exit(rc)
