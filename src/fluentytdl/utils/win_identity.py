"""Windows 身份工具：取当前进程令牌的 TokenUser SID。

**为什么需要它**：更新时 `updater.exe` 会经 UAC 提权，提权后它手上的令牌**不一定还是
发起更新的那个用户**。over-the-shoulder 提权（标准用户 Alice 触发更新，UAC 框里输入
管理员 Bob 的密码）下，updater 自身以及它的 linked token 都是 **Bob** 的。若用 Bob 的
令牌启动新版，新版会去开 Bob 的 `%LOCALAPPDATA%`，而用户数据在 Alice 那边 —— 完整性
级别对了，用户身份错了，传 `--data-dir` 也救不回来（那是 ACL 问题）。

所以"原用户是谁"必须在**主程序进程里**取好（此刻还没有任何提权发生），再经
`--origin-user-sid` 交给 updater，当作它每一级降权启动的准入条件。
消费端见 `core/updater.py::pick_launch_rung()` / `_launch_medium()`。

纯 ctypes + 标准库，不引入 pywin32 依赖。非 Windows 平台与任何失败一律返回 `""`
（调用方据此退回"只看完整性级别"的旧行为）。

`core/updater.py::_token_user_sid()` 是同一套逻辑的**有意重复** —— 那个文件被单独
打包成 exe，不能 import 本包内任何东西。两处必须保持同一种 SID 字符串格式
（`ConvertSidToStringSidW` 的 `S-1-5-21-...`）。
"""

from __future__ import annotations

import sys

# TOKEN_INFORMATION_CLASS.TokenUser / 访问位（与 core/updater.py 的同名常量一致）
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1


def current_user_sid() -> str:
    """返回当前进程令牌的用户 SID 字符串（如 `S-1-5-21-...`），失败返回 `""`。"""
    if sys.platform != "win32":
        return ""
    try:
        return _current_user_sid_win()
    except Exception:
        # 身份探测是纯增强手段：拿不到就退化，绝不让它影响调用方的主流程。
        return ""


def _current_user_sid_win() -> str:
    import ctypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # ctypes 默认 restype 是 c_int（32 位），x64 下句柄会被截断 —— 必须显式声明。
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    advapi32.GetTokenInformation.restype = ctypes.c_int
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int

    h_token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h_token)
    ):
        return ""

    try:
        # 第一次调用只为取长度，必然以 ERROR_INSUFFICIENT_BUFFER 失败，不检查返回值。
        size = ctypes.c_ulong(0)
        advapi32.GetTokenInformation(h_token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        if not size.value:
            return ""

        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            h_token, TOKEN_USER_CLASS, buf, size.value, ctypes.byref(size)
        ):
            return ""

        # TOKEN_USER 的第一个字段就是 SID_AND_ATTRIBUTES.Sid（一个 PSID 指针）。
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents
        sid_str = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
            return ""
        try:
            return sid_str.value or ""
        finally:
            kernel32.LocalFree(ctypes.cast(sid_str, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(h_token)
