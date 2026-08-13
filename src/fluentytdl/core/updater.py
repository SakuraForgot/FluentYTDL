"""
FluentYTDL 独立更新器

Telegram 风格的独立更新器，由主程序在下载完 app-core 更新后启动。
主程序退出后，updater 等待进程释放，替换文件，然后重启主程序。

用法:
    python updater.py --pid <PID> --archive <7z路径> --dest <应用目录> --exe <exe名>

打包:
    PyInstaller 打包为 updater.exe，随应用一起分发。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

# Windows API 常量
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
MB_ICONERROR = 0x00000010
MB_TOPMOST = 0x00040000

# 令牌与进程创建（Step 6 的降权启动）
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1  # TOKEN_INFORMATION_CLASS.TokenUser
TOKEN_LINKED_TOKEN_CLASS = 19  # TOKEN_INFORMATION_CLASS.TokenLinkedToken
SECURITY_IMPERSONATION = 2  # SECURITY_IMPERSONATION_LEVEL.SecurityImpersonation
TOKEN_PRIMARY = 1  # TOKEN_TYPE.TokenPrimary
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
LOGON_WITH_PROFILE = 0x00000001
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1

# 外壳图标缓存刷新（Step 5.5 的 SHChangeNotify）
SHCNE_UPDATEITEM = 0x00002000
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000
SHCNF_PATHW = 0x0005
SHCNF_FLUSH = 0x1000

# 看门狗时间预算（秒）
READY_TIMEOUT = 90.0
SURVIVAL_GRACE = 15.0
WATCH_POLL_INTERVAL = 0.5
STALE_READY_MAX_AGE = 300.0  # 5 分钟外的 READY 文件一律视为陈旧

# 延时 cmd helper 的 creationflags。**只能是 CREATE_NO_WINDOW，不许再 `|
# DETACHED_PROCESS`。** MSDN 明说这两个标志互斥：DETACHED_PROCESS（以及
# CREATE_NEW_CONSOLE）在场时 CREATE_NO_WINDOW 直接被忽略。被忽略之后，detached
# 的 cmd.exe 手里没有任何控制台可继承，于是它的控制台子进程（我们那个当 sleep
# 用的 `ping`）自己去 AllocConsole —— Windows 11 默认终端把这个新控制台渲染成
# 一个可见窗口，标题就是子进程的命令行（`ping  -n 4 127.0.0.1`）。用户在更新过程
# 中看到的就是它。CREATE_NO_WINDOW 单独用时控制台窗口是隐藏的，实测无弹窗。
#
# 去掉 DETACHED_PROCESS 不影响 helper 活过 updater 退出：Popen 不建立 job
# 关联，子进程本来就不随父进程终止。已在无控制台的 GUI 父进程（pythonw，等价于
# console=False 的 updater.exe）下实测：父进程退出 7 秒后 helper 仍写出了标记。
#
# getattr 兜底：本模块被 tests/test_updater.py 按路径 importlib 加载，
# subprocess.CREATE_NO_WINDOW 在非 Windows 上不存在，导入期取属性会 AttributeError。
HELPER_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ─── ctypes 结构体 ────────────────────────────────────────
#
# 一律用裸 ctypes 类型拼，**不 import ctypes.wintypes** —— 那个模块在非 Windows
# 上 import 即抛 ValueError，而 tests/test_updater.py 是按路径 importlib 加载本
# 模块的，导入期抛异常会让整个测试文件失败。结构体定义本身是纯 Python，跨平台安全；
# 真正的 windll 调用全部关在函数体里。


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong),
        ("dwThreadId", ctypes.c_ulong),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong),
        ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong),
        ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _SHELLEXECUTEINFOW(ctypes.Structure):
    # DUMMYUNIONNAME { HANDLE hIcon; HANDLE hMonitor; } 两个成员都是 HANDLE，
    # 尺寸与对齐一致，所以并成一个字段即可。
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


_prototypes_ready = False


def _setup_prototypes() -> None:
    """给用到的 Win32 函数声明 argtypes / restype。

    **不是可选步骤**：ctypes 的缺省 restype 是 c_int（32 位有符号）。x64 下
    OpenProcess / DuplicateTokenEx / CreateEnvironmentBlock 返回的句柄和指针会被
    静默截断成 32 位 —— 拿到一个看起来非零、实际无效的句柄，然后在后续调用里以
    ERROR_INVALID_HANDLE 的形式莫名失败。
    """
    global _prototypes_ready
    if _prototypes_ready or sys.platform != "win32":
        return

    k32 = ctypes.windll.kernel32
    a32 = ctypes.windll.advapi32
    u32 = ctypes.windll.user32
    ue = ctypes.windll.userenv

    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.WaitForSingleObject.restype = ctypes.c_ulong
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k32.TerminateProcess.restype = ctypes.c_int
    k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k32.GetProcessId.restype = ctypes.c_ulong
    k32.GetProcessId.argtypes = [ctypes.c_void_p]
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetCurrentProcess.argtypes = []
    k32.LocalFree.restype = ctypes.c_void_p
    k32.LocalFree.argtypes = [ctypes.c_void_p]

    a32.OpenProcessToken.restype = ctypes.c_int
    a32.OpenProcessToken.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    a32.GetTokenInformation.restype = ctypes.c_int
    a32.GetTokenInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    a32.DuplicateTokenEx.restype = ctypes.c_int
    a32.DuplicateTokenEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    a32.ConvertSidToStringSidW.restype = ctypes.c_int
    a32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    a32.CreateProcessWithTokenW.restype = ctypes.c_int
    a32.CreateProcessWithTokenW.argtypes = [
        ctypes.c_void_p,  # hToken
        ctypes.c_ulong,  # dwLogonFlags
        ctypes.c_wchar_p,  # lpApplicationName
        ctypes.c_wchar_p,  # lpCommandLine (in/out)
        ctypes.c_ulong,  # dwCreationFlags
        ctypes.c_void_p,  # lpEnvironment
        ctypes.c_wchar_p,  # lpCurrentDirectory
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]

    u32.GetShellWindow.restype = ctypes.c_void_p
    u32.GetShellWindow.argtypes = []
    u32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    ue.CreateEnvironmentBlock.restype = ctypes.c_int
    ue.CreateEnvironmentBlock.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    ue.DestroyEnvironmentBlock.restype = ctypes.c_int
    ue.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]

    ctypes.windll.shell32.ShellExecuteExW.restype = ctypes.c_int
    ctypes.windll.shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]

    # SHChangeNotify（Step 5.5）。dwItem1 声明成 c_wchar_p 而不是 c_void_p：本模块
    # 只用 SHCNF_PATHW（传宽字符路径）与 SHCNF_IDLIST（传 None），两者都合法，
    # 而 c_wchar_p 能让"不小心传了 bytes"在调用点就炸掉而不是变成野指针。
    ctypes.windll.shell32.SHChangeNotify.restype = None
    ctypes.windll.shell32.SHChangeNotify.argtypes = [
        ctypes.c_long,  # wEventId
        ctypes.c_uint,  # uFlags
        ctypes.c_wchar_p,  # dwItem1
        ctypes.c_void_p,  # dwItem2
    ]

    _prototypes_ready = True


# ─── 文件日志 ─────────────────────────────────────────────

_logger: logging.Logger | None = None


def _init_log(dest_dir: Path) -> None:
    """初始化文件日志（console=False 时 stderr 不可见）。"""
    global _logger
    log_dir = dest_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _logger = logging.getLogger("updater")
    _logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(log_dir / "updater.log", encoding="utf-8", mode="w")
    handler.setFormatter(logging.Formatter("%(asctime)s [updater] %(message)s", datefmt="%H:%M:%S"))
    _logger.addHandler(handler)


def log(msg: str) -> None:
    """日志输出到文件 + stderr（双通道，确保可追溯）。"""
    if _logger:
        _logger.info(msg)
    try:
        print(f"[updater] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _alert(msg: str, title: str = "FluentYTDL 更新") -> None:
    """弹一个模态错误框告知用户更新失败。

    updater.exe 是 console=False 的独立进程（scripts/updater.spec），既没有可见的
    stdout，也拿不到主程序的 InfoBar —— MessageBoxW 是唯一能让用户看见失败的通道。
    没有它，"点更新 → 程序退出 → 什么都没发生 → 程序自己回来了"就是一次完美的
    静默失败。

    ⚠ **这个调用是阻塞的**，用户不点确定它就一直挂着。所以调用点必须满足两个条件：
      1) 状态已收敛 —— 要么一个文件都没动过，要么已经完整回滚。绝不能在持有
         `_internal_old` 这类中间状态时弹窗，否则安装目录会一直半残着。
      2) 紧接着 return，不再做任何文件操作。

    成功路径绝不弹窗 —— 新版自己起来就是最好的成功反馈。
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.MessageBoxW(0, msg, title, MB_ICONERROR | MB_TOPMOST)
    except Exception as e:
        log(f"MessageBoxW 失败（用户看不到这次失败）: {e}")


def wait_for_process(pid: int, timeout: int = 30) -> bool:
    """等待指定 PID 的进程退出。

    Windows: 使用 ctypes 调用 OpenProcess + WaitForSingleObject。
    其他平台: 轮询 /proc 或 psutil。

    Returns:
        True 如果进程已退出，False 如果超时。
    """
    if sys.platform == "win32":
        return _wait_windows(pid, timeout)
    return _wait_polling(pid, timeout)


def _wait_windows(pid: int, timeout: int) -> bool:
    """Windows: 使用 WaitForSingleObject 等待进程退出。"""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    # PROCESS_SYNCHRONIZE = 0x00100000
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        # 进程不存在，视为已退出
        log(f"进程 {pid} 不存在或已退出")
        return True

    try:
        timeout_ms = timeout * 1000 if timeout > 0 else INFINITE
        result = kernel32.WaitForSingleObject(handle, timeout_ms)
        if result == WAIT_OBJECT_0:
            log(f"进程 {pid} 已退出")
            return True
        elif result == WAIT_TIMEOUT:
            log(f"等待进程 {pid} 超时 ({timeout}s)")
            return False
        else:
            log(f"WaitForSingleObject 返回异常值: {result}")
            return False
    finally:
        kernel32.CloseHandle(handle)


def _wait_polling(pid: int, timeout: int) -> bool:
    """跨平台回退: 轮询检查进程是否存在。"""
    deadline = time.time() + timeout if timeout > 0 else float("inf")
    while time.time() < deadline:
        try:
            os.kill(pid, 0)  # 检查进程是否存在
            time.sleep(0.5)
        except OSError:
            log(f"进程 {pid} 已退出")
            return True
    log(f"等待进程 {pid} 超时 ({timeout}s)")
    return False


def extract_archive(archive_path: Path, dest_dir: Path) -> None:
    """解压归档文件到目标目录。

    支持 .7z（通过 py7zr 或系统 7z）和 .zip。
    """
    if archive_path.suffix == ".7z":
        _extract_7z(archive_path, dest_dir)
    elif archive_path.suffix == ".zip":
        _extract_zip(archive_path, dest_dir)
    else:
        raise ValueError(f"不支持的归档格式: {archive_path.suffix}")


def _extract_7z(archive_path: Path, dest_dir: Path) -> None:
    """解压 7z 文件。优先使用 py7zr，回退到系统 7z CLI。"""
    # 尝试 py7zr
    try:
        import py7zr

        with py7zr.SevenZipFile(archive_path, "r") as archive:
            archive.extractall(dest_dir)
        log("通过 py7zr 解压完成")
        return
    except ImportError:
        log("py7zr 未安装，尝试 7z CLI")
    except Exception as e:
        log(f"py7zr 解压失败: {e}，尝试 7z CLI")

    # 回退到 7z CLI
    # 优先使用应用自带的 bin/7z.exe
    sevenzip: str | None = None
    for candidate in [
        dest_dir / "bin" / "7z.exe",
        dest_dir / "bin" / "7z" / "7z.exe",
    ]:
        if candidate.exists():
            sevenzip = str(candidate)
            log(f"使用应用自带 7z: {sevenzip}")
            break

    if not sevenzip:
        sevenzip = shutil.which("7z") or shutil.which("7za")
        if sevenzip:
            log(f"使用系统 7z: {sevenzip}")

    if not sevenzip:
        raise RuntimeError("无法解压 7z 文件: py7zr 不支持此压缩格式且系统中未找到 7z")

    # 使用 shell 模式避免路径含空格/括号时 -o 参数解析失败
    # 7z 的 -o 参数格式要求: -o"path with spaces"
    # Python subprocess 列表模式会将整个 "-opath (1)" 加引号导致 7z 解析错误
    cmd = f'"{sevenzip}" x "{archive_path}" -o"{dest_dir}" -aoa -y'
    log(f"7z 命令: {cmd}")

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        **kwargs,
    )

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        log(f"7z 退出码: {result.returncode}")
        if stderr:
            log(f"7z stderr: {stderr}")
        if stdout:
            log(f"7z stdout: {stdout}")
        raise RuntimeError(f"7z 解压失败 (exit {result.returncode}): {stderr or stdout}")

    log("通过 7z CLI 解压完成")


def _extract_zip(archive_path: Path, dest_dir: Path) -> None:
    """解压 zip 文件。"""
    with zipfile.ZipFile(archive_path) as zf:
        zf.extractall(dest_dir)
    log("通过 zipfile 解压完成")


def self_delete(exe_path: Path) -> None:
    """延迟自删除。通过 cmd 命令在短暂延迟后删除自身，带重试。"""
    if sys.platform != "win32":
        try:
            exe_path.unlink(missing_ok=True)
        except Exception:
            pass
        return

    # Windows: 用 cmd /c 延迟删除，增加重试避免进程未退出时删除失败
    # ping -n 3 ≈ 2 秒延迟，若删除失败再等待重试一次
    cmd = (
        f"ping -n 3 127.0.0.1 >nul 2>&1"
        f' & (del /f /q "{exe_path}" 2>nul'
        f' || (ping -n 3 127.0.0.1 >nul 2>&1 & del /f /q "{exe_path}" 2>nul))'
    )
    # 字符串形态，不是 ["cmd", "/c", cmd] 列表 —— 理由见
    # _spawn_updater_swap_helper() 里那段注释：list2cmdline 会把内嵌引号转义成
    # `\"`，cmd.exe 不认，带空格的路径（`C:\Program Files\...`）会被解析错。
    subprocess.Popen(
        f"cmd /c {cmd}",
        creationflags=HELPER_CREATIONFLAGS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _is_admin() -> bool:
    """当前进程是否具备管理员权限。

    **有意重复** utils/admin_utils.py:11-25 的实现：本文件被 scripts/updater.spec
    单独打包成 exe，不能 import fluentytdl 包内任何东西（那会把整个 Qt 依赖树拖进
    updater.exe）。改这里时记得那边也有一份。
    """
    if sys.platform != "win32":
        return False
    try:
        shell32 = ctypes.windll.shell32
        return bool(shell32.IsUserAnAdmin())
    except Exception as e:
        log(f"IsUserAnAdmin 调用失败，按无管理员权限处理: {e}")
        return False


def _can_write_dir(path: Path) -> bool:
    """探测目标目录当前进程能不能写。

    与 P0-4（paths.py 删掉 .writetest 探针）**不矛盾**：那里删探针，是因为它被用来
    决定**用户数据长期落在哪**，而探测结果会随提权状态漂移 —— 提权会话探测成功，
    数据就落进 Program Files，下次普通权限启动又找不到。这里问的是另一个问题：
    "我现在这一刻能不能替换这个目录里的文件"，那正是写探测唯一正确的用途，且发生
    在一个短命进程里，只影响这一次决策。

    用 NamedTemporaryFile 而不是固定文件名：delete=True 在 Windows 上走
    FILE_FLAG_DELETE_ON_CLOSE，异常退出也不会留下垃圾（固定名字的探针文件一旦
    残留，就会被 create_app_core_7z 的白名单当成未知条目而中止构建）。
    """
    try:
        with tempfile.NamedTemporaryFile(dir=str(path), prefix=".upd_probe_", suffix=".tmp") as f:
            f.write(b"probe")
        return True
    except OSError as e:
        log(f"写探测失败（目录不可写）: {path} — {e}")
        return False


def request_admin_if_needed(
    app_dir: Path,
    *,
    already_elevated: bool = False,
    is_admin_fn: Callable[[], bool] = _is_admin,
) -> bool:
    """检测是否需要管理员权限，如需要则提权重启自身。

    Args:
        app_dir: 应用安装目录（替换目标）。
        already_elevated: 本进程是否由 _elevate_self 提权启动（--elevated 哨兵）。
        is_admin_fn: 权限查询函数，仅为可测试性而注入（test_updater.py 零 mock 约束）。

    Returns:
        True 如果已提权并启动了新的 updater 进程（当前进程应立即退出）。
        False 如果不需要提权，或提权失败/被用户拒绝。
    """
    if sys.platform != "win32":
        return False

    # 闸门 1：哨兵优先。即使 IsUserAnAdmin 在某些环境下说谎（组策略、容器、
    # 被 hook），带 --elevated 的进程也绝不再次提权 —— 否则就是 UAC 无限递归：
    # 每次都弹框、每次都拉起一个新的 updater、永远走不到替换那一步。这正是
    # Program Files 安装用户"永远收不到更新"的直接成因。
    if already_elevated:
        log("本进程已是提权实例（--elevated），跳过提权检查")
        return False

    # 闸门 2：已经是管理员就没什么可提的。
    if is_admin_fn():
        log("当前进程已具备管理员权限，无需提权")
        return False

    # 检测是否在 Program Files 目录下
    app_dir_str = str(app_dir).lower()
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files").lower()
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)").lower()
    local_app_data = os.environ.get("LOCALAPPDATA", "").lower()

    # 如果在用户目录下（便携版 / %LOCALAPPDATA%\Programs 安装），不需要提权
    if local_app_data and app_dir_str.startswith(local_app_data):
        return False

    # 如果在 Program Files 下，需要提权
    if app_dir_str.startswith(program_files) or app_dir_str.startswith(program_files_x86):
        log("检测到 Program Files 目录，尝试请求管理员权限...")
        return _elevate_self()

    # 兜底：前缀启发式覆盖不到自定义只读安装位置（如 D:\Apps\FluentYTDL 被管理员
    # 收紧了 ACL）。真探一次比猜路径可靠。
    if not _can_write_dir(app_dir):
        log("目标目录不可写（自定义只读安装位置），尝试请求管理员权限...")
        return _elevate_self()

    return False


def _elevate_self() -> bool:
    """使用 ShellExecuteW 的 runas verb 提权重启自身。"""
    try:
        # 构建命令行参数（处理含空格的路径）
        def _quote(arg: str) -> str:
            return f'"{arg}"' if " " in arg else arg

        # 追加 --elevated 哨兵：提权后的实例靠它认出"我是二次启动的"，从而在
        # request_admin_if_needed 的第一道闸就返回 False。少了这一行就是 UAC
        # 无限递归 —— 提权实例重新走一遍检测、再弹一次框、再拉起一个自己。
        forwarded = list(sys.argv[1:])
        if "--elevated" not in forwarded:
            forwarded.append("--elevated")
        args = " ".join(_quote(a) for a in forwarded)

        if getattr(sys, "frozen", False):
            # 打包模式：直接重新启动 updater.exe
            exe = sys.executable
            params = args
        else:
            # 开发模式：用 python 运行 updater.py
            exe = sys.executable
            params = f"{_quote(str(Path(__file__).resolve()))} {args}"

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        ret = shell32.ShellExecuteW(
            None,
            "runas",
            exe,
            params,
            str(Path(sys.executable).resolve().parent),
            0,  # SW_HIDE — 不显示窗口
        )
        if ret > 32:
            log("已启动管理员权限进程，当前进程退出")
            return True
        else:
            log(f"ShellExecuteW 返回 {ret}，提权失败")
            return False
    except Exception as e:
        log(f"提权失败: {e}")
        return False


def _verify_extraction(extract_dir: Path, exe_name: str) -> bool:
    """验证解压后的文件完整性（飞行前检查）。

    看门狗（Step 7）是兜底，但它只能在**已经动过安装目录之后**才发现问题，代价是
    一次完整的回滚。这里多花几毫秒把坏归档拦在 Step 4 改名之前，那才是零成本的失败。

    检查项从"exe 非空 + _internal 非空"扩到 PyInstaller onedir 的两个必需件：
      - `_internal/base_library.zip` —— 缺了它 bootloader 装不起 stdlib
      - `_internal/python3*.dll`     —— 缺了它根本没有解释器
    两者任一缺失，解压出来的东西必然是启动即失败，没有理由拿它去替换一个能跑的版本。
    """
    exe_path = extract_dir / exe_name
    if not exe_path.exists() or exe_path.stat().st_size == 0:
        log(f"错误: {exe_name} 不存在或大小为 0")
        return False

    internal_dir = extract_dir / "_internal"
    if not internal_dir.exists() or not any(internal_dir.iterdir()):
        log("错误: _internal/ 目录不存在或为空")
        return False

    base_library = internal_dir / "base_library.zip"
    if not base_library.exists() or base_library.stat().st_size == 0:
        log("错误: _internal/base_library.zip 不存在或大小为 0（归档不完整）")
        return False

    python_dlls = [p for p in internal_dir.glob("python3*.dll") if p.stat().st_size > 0]
    if not python_dlls:
        log("错误: _internal/ 下找不到 python3*.dll（归档不完整）")
        return False
    log(f"  飞行前检查通过: base_library.zip + {python_dlls[0].name}")

    version_file = extract_dir / "VERSION"
    if not version_file.exists():
        log("警告: VERSION 文件不存在（非致命）")

    return True


# 运行期第二道防线：即使归档是 3.6.6 之前构建的**脏归档**（那时 create_app_core_7z
# 用的是黑名单，开发者从 dist/ 跑过一次程序就会把自己的 config.json / state/ / logs/
# 打进归档），也绝不覆盖这些条目。根治手段是构建期白名单，见 pyproject.toml 的
# [tool.fluentytdl.build].app_core_include；这里只是纵深防御。
#
# 本文件被 PyInstaller 打包成独立 exe（scripts/updater.spec），不能 import
# fluentytdl 包内任何东西，故硬编码。
#
# 为什么不给 updater 加 --files 参数把清单传进来：updater.exe 的版本可以任意旧
# （它不在 app-core 归档里），给 argparse 加未知参数会让旧 updater 直接
# SystemExit(2)，更新彻底失败。归档内嵌清单文件同样不行 —— 旧 updater 会把它
# 当普通文件搬进安装目录留垃圾。
PROTECTED_NAMES = frozenset(
    {
        "config.json",
        "state",
        "logs",
        "bin",
        "update_manifest_cache.json",
        "error_rules.override.json",
        "portable.txt",
        ".update_ready",
        ".migrated_v2",
    }
)
# NTFS 大小写不敏感，而脏归档里的大小写取决于打包时的实际文件名（不可控）：
# 精确匹配会被 "Config.json" / "STATE" 绕过。模块加载时预先 casefold 一次。
_PROTECTED_CASEFOLDED = frozenset(n.casefold() for n in PROTECTED_NAMES)


def _move_extracted_files(tmp_dir: Path, dest_dir: Path) -> bool:
    """从临时目录移动所有文件到目标目录。

    目标目录中的同名文件应已被重命名为备份（由调用方处理）。
    对于未被备份的文件（如 VERSION、docs/、licenses/），直接覆写。

    PROTECTED_NAMES 里的条目一律跳过 —— 覆盖它们等于销毁用户数据。
    """
    for item in tmp_dir.iterdir():
        if item.name.casefold() in _PROTECTED_CASEFOLDED:
            log(f"  跳过受保护条目（归档不该含它）: {item.name}")
            continue
        dest = dest_dir / item.name
        try:
            if dest.exists():
                # 这些文件应该已被重命名为备份，若仍存在则直接删除
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            shutil.move(str(item), str(dest))
            log(f"  移动: {item.name}")
        except OSError as e:
            log(f"移动 {item.name} 失败: {e}")
            return False
    return True


def _notify_shell_icon_change(exe_path: Path) -> None:
    """告诉资源管理器"这个 exe 换过了，图标别再用缓存的那张"。

    覆盖安装最常见的观感 bug：exe 的 PE 图标资源已经是新的，但任务栏 / 开始菜单 /
    桌面显示的仍是旧图标 —— Windows 的图标缓存按路径 + 时间戳索引，路径没变时它
    不会主动重取。

    两发通知，缺一不可：

    * ``SHCNE_UPDATEITEM`` + ``SHCNF_PATHW`` 精确指向新 exe，便宜；``SHCNF_FLUSH``
      让调用同步返回（这一刻紧接着就要启动新版，不能让通知排在队列里过期）。
    * ``SHCNE_ASSOCCHANGED`` + ``SHCNF_IDLIST`` 是广播，**只有它能让已固定到任务栏 /
      开始菜单的项重新取图标** —— 固定项是独立的 .lnk，不在第一发的路径范围里。

    明确不做：不删 ``IconCache*.db``，不 ``taskkill explorer.exe``。为一张图标重启
    用户的整个外壳，代价远大于收益。

    **全程 try/except 且不返回状态**：纯装饰性调用。此刻文件替换已经全部成功，
    让一次刷新通知的失败影响到那个结果是荒谬的。
    """
    if sys.platform != "win32":
        return
    try:
        _setup_prototypes()  # 自己调一次：Step 6 的 _launch_medium 还没跑到
        shell32 = ctypes.windll.shell32
        shell32.SHChangeNotify(
            SHCNE_UPDATEITEM,
            SHCNF_PATHW | SHCNF_FLUSH,
            str(exe_path),
            None,
        )
        shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        log("  已通知外壳刷新图标缓存")
    except Exception as e:  # 装饰性调用，任何异常都只记一行
        log(f"  通知外壳刷新图标缓存失败（无害）: {e}")


# ─── Step 6：降权启动新版 ──────────────────────────────────
#
# 三颗雷一起拆，因为解法收敛到同一处：
#   1) ShellExecuteW 的返回值是历史兼容的 HINSTANCE 风格状态码，**不是进程句柄** ——
#      没有句柄就没法 WaitForSingleObject / TerminateProcess，看门狗无从谈起。
#   2) 从 elevated updater 直接 CreateProcess 出来的子进程**继承管理员令牌**。那正是
#      本轮要根除的数据漂移源头（提权会话把 config.json 写进 Program Files）。
#   3) OTS 提权（Alice 触发更新、UAC 里输入管理员 Bob 的密码）下，"降权"拿到的
#      linked token 是 **Bob** 的 medium 令牌。IL 对了，用户身份错了 —— 用它启动的
#      新版去开 C:\Users\Alice\... 会撞 ACL，传 --data-dir 也救不了。
#
# 所以每一级令牌在使用前都要先比 TokenUser SID 是否等于发起更新的原用户 SID
# （由主程序经 --origin-user-sid 传入）。一级都对不上就**不自动启动**。


def _close_handle(handle: int | None) -> None:
    if handle:
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def _token_user_sid(h_token: int) -> str:
    """读令牌的 TokenUser 并转成 S-1-5-21-... 字符串形式。

    用字符串比而非 EqualSid：省掉在 ctypes 里管理 SID 缓冲区生命周期，
    而且日志里直接可读。与 utils/win_identity.py::current_user_sid() 同格式。
    """
    try:
        advapi32 = ctypes.windll.advapi32
        size = ctypes.c_ulong(0)
        advapi32.GetTokenInformation(h_token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        if not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            h_token, TOKEN_USER_CLASS, buf, size.value, ctypes.byref(size)
        ):
            return ""
        token_user = ctypes.cast(buf, ctypes.POINTER(_TOKEN_USER)).contents
        sid_str = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(token_user.User.Sid, ctypes.byref(sid_str)):
            return ""
        try:
            return sid_str.value or ""
        finally:
            ctypes.windll.kernel32.LocalFree(sid_str)
    except Exception as e:
        log(f"  读取令牌 SID 失败: {e}")
        return ""


def _open_self_token() -> int | None:
    try:
        k32 = ctypes.windll.kernel32
        h_token = ctypes.c_void_p()
        if not ctypes.windll.advapi32.OpenProcessToken(
            k32.GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(h_token)
        ):
            return None
        return h_token.value
    except Exception as e:
        log(f"  打开自身令牌失败: {e}")
        return None


def _open_shell_token() -> int | None:
    """取 Explorer 的令牌 —— 交互用户的 medium 令牌，也是唯一身份必然正确的来源。"""
    try:
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        hwnd = u32.GetShellWindow()
        if not hwnd:
            log("  shell token: GetShellWindow 返回 0（Explorer 未运行？）")
            return None
        pid = ctypes.c_ulong(0)
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        h_proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            log("  shell token: OpenProcess 失败")
            return None
        try:
            h_token = ctypes.c_void_p()
            if not ctypes.windll.advapi32.OpenProcessToken(
                h_proc, TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(h_token)
            ):
                log("  shell token: OpenProcessToken 失败")
                return None
            return h_token.value
        finally:
            _close_handle(h_proc)
    except Exception as e:
        log(f"  取 shell token 失败: {e}")
        return None


def _open_linked_token() -> int | None:
    """取自身令牌的 TokenLinkedToken（提权进程的"另一半"，即对应的 medium 令牌）。"""
    h_self = _open_self_token()
    if not h_self:
        return None
    try:
        linked = ctypes.c_void_p()
        ret_len = ctypes.c_ulong(0)
        if not ctypes.windll.advapi32.GetTokenInformation(
            h_self,
            TOKEN_LINKED_TOKEN_CLASS,
            ctypes.byref(linked),
            ctypes.sizeof(linked),
            ctypes.byref(ret_len),
        ):
            log("  linked token: GetTokenInformation(TokenLinkedToken) 失败")
            return None
        return linked.value
    except Exception as e:
        log(f"  取 linked token 失败: {e}")
        return None
    finally:
        _close_handle(h_self)


def _duplicate_primary(h_token: int) -> int | None:
    """复制成 primary token 供 CreateProcessWithTokenW 使用。

    dwDesiredAccess **必须**含 TOKEN_ASSIGN_PRIMARY —— 漏掉它是这里最容易犯的错：
    本机开发往往照样能跑，到用户机器上变成莫名的 ERROR_ACCESS_DENIED。
    """
    try:
        h_new = ctypes.c_void_p()
        if not ctypes.windll.advapi32.DuplicateTokenEx(
            h_token,
            TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY,
            None,
            SECURITY_IMPERSONATION,
            TOKEN_PRIMARY,
            ctypes.byref(h_new),
        ):
            log(f"  DuplicateTokenEx 失败 (err={ctypes.get_last_error()})")
            return None
        return h_new.value
    except Exception as e:
        log(f"  DuplicateTokenEx 异常: {e}")
        return None


def _quote_arg(value: str) -> str:
    return f'"{value}"' if " " in value or not value else value


def _create_process_with_token(
    h_token: int, exe_path: Path, params: str, cwd: Path
) -> tuple[int | None, int]:
    """用给定的 primary token 创建进程，返回 (hProcess, pid)。

    用 CreateProcessWithTokenW 而不是 CreateProcessAsUser：后者要
    SeAssignPrimaryTokenPrivilege，管理员默认**没有**；前者要 SeImpersonatePrivilege，
    管理员默认有。代价是它经由 Secondary Logon 服务（seclogon），服务被禁用时失败。
    """
    env_block = ctypes.c_void_p()
    have_env = False
    try:
        # CreateEnvironmentBlock 不是装饰：直接继承 elevated updater 的环境会让新版
        # 把 %LOCALAPPDATA% 解析到错误的用户 —— 正是 Phase 6 要根除的那类漂移。
        if ctypes.windll.userenv.CreateEnvironmentBlock(ctypes.byref(env_block), h_token, False):
            have_env = True
        else:
            log("  警告: CreateEnvironmentBlock 失败，退回继承父进程环境")

        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        pi = _PROCESS_INFORMATION()
        # lpCommandLine 是 in/out 参数，必须给可写缓冲区
        cmdline = ctypes.create_unicode_buffer(f"{_quote_arg(str(exe_path))} {params}".strip())

        ok = ctypes.windll.advapi32.CreateProcessWithTokenW(
            h_token,
            LOGON_WITH_PROFILE,  # 默认不加载 profile，HKCU 行为会与交互登录不一致
            str(exe_path),
            cmdline,
            CREATE_UNICODE_ENVIRONMENT,
            env_block if have_env else None,
            str(cwd),
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            log(f"  CreateProcessWithTokenW 失败 (err={ctypes.get_last_error()})")
            return None, 0
        _close_handle(pi.hThread)
        return pi.hProcess, int(pi.dwProcessId)
    except Exception as e:
        log(f"  CreateProcessWithTokenW 异常: {e}")
        return None, 0
    finally:
        if have_env:
            try:
                ctypes.windll.userenv.DestroyEnvironmentBlock(env_block)
            except Exception:
                pass


def _shell_execute_ex(exe_path: Path, params: str, cwd: Path) -> tuple[int | None, int]:
    """第 3 级退化：ShellExecuteExW + SEE_MASK_NOCLOSEPROCESS 换一个真句柄。

    必须用 Ex 版本 —— 普通 ShellExecuteW 的返回值是 HINSTANCE 风格状态码，
    拿不到进程句柄，看门狗就没有观察对象。
    """
    try:
        sei = _SHELLEXECUTEINFOW()
        sei.cbSize = ctypes.sizeof(sei)
        sei.fMask = SEE_MASK_NOCLOSEPROCESS
        sei.lpVerb = "open"
        sei.lpFile = str(exe_path)
        sei.lpParameters = params or None
        sei.lpDirectory = str(cwd)
        sei.nShow = SW_SHOWNORMAL
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
            log(f"  ShellExecuteExW 失败 (err={ctypes.get_last_error()})")
            return None, 0
        h_proc = sei.hProcess
        if not h_proc:
            log("  ShellExecuteExW 成功但未返回进程句柄")
            return None, 0
        return h_proc, int(ctypes.windll.kernel32.GetProcessId(h_proc))
    except Exception as e:
        log(f"  ShellExecuteExW 异常: {e}")
        return None, 0


def pick_launch_rung(
    shell_sid: str | None,
    linked_sid: str | None,
    self_sid: str | None,
    origin_sid: str,
) -> int | None:
    """决定用三级阶梯的哪一级启动新版。纯函数，Phase 8 直接单测。

    参数为各级令牌的 TokenUser SID 字符串，`None` 表示那一级的令牌**拿不到**
    （Explorer 未运行、seclogon 被禁、GetTokenInformation 失败……）。

    返回 1 / 2 / 3 分别对应 shell token / linked token / ShellExecuteExW 退化路径；
    返回 None 表示**没有任何一级的身份等于原用户**（典型即 OTS 提权），此时不自动启动。

    origin_sid 为空 = 旧主程序拉起的新 updater，它不知道该传原用户 SID。
    退回"只看完整性级别"的老行为：哪一级拿得到就用哪一级，不做身份校验。
    """
    if not origin_sid:
        if shell_sid is not None:
            return 1
        if linked_sid is not None:
            return 2
        return 3

    if shell_sid and shell_sid == origin_sid:
        return 1
    if linked_sid and linked_sid == origin_sid:
        return 2
    if self_sid and self_sid == origin_sid:
        return 3
    return None


def _launch_medium(
    exe_path: Path, cwd: Path, extra_args: list[str], origin_sid: str
) -> tuple[int | None, int, str]:
    """按三级阶梯降权启动 exe，返回 (hProcess, pid, mode)。

    mode 取值：`shell_token` / `linked_token` / `shell_execute` / `none`。
    `none` 表示准入条件一级都不满足 —— 调用方应当 COMMIT 并提示用户手动启动，
    **不要回滚**：_verify_extraction 已经通过，二进制是好的，失败在启动器不在构建。
    """
    if sys.platform != "win32":
        proc = subprocess.Popen([str(exe_path), *extra_args], cwd=str(cwd))
        return None, proc.pid, "posix"

    _setup_prototypes()
    params = " ".join(_quote_arg(a) for a in extra_args)

    h_shell = _open_shell_token()
    h_linked = _open_linked_token()
    h_self = _open_self_token()
    try:
        shell_sid = _token_user_sid(h_shell) if h_shell else None
        linked_sid = _token_user_sid(h_linked) if h_linked else None
        self_sid = _token_user_sid(h_self) if h_self else None

        log(f"  origin SID : {origin_sid or '(未提供)'}")
        log(f"  shell  SID : {shell_sid or '(取不到)'}")
        log(f"  linked SID : {linked_sid or '(取不到)'}")
        log(f"  self   SID : {self_sid or '(取不到)'}")
        if not origin_sid:
            log("  警告: 未收到 --origin-user-sid（旧主程序），跳过身份校验，只看完整性级别")

        rung = pick_launch_rung(shell_sid, linked_sid, self_sid, origin_sid)
        log(f"  选定降权级别: {rung}")

        if rung is None:
            # OTS 提权：三级令牌没有一个属于发起更新的用户。宁可让用户多点一次
            # 桌面图标，也不能用 Bob 的身份去开 Alice 的数据目录。
            log("  没有任何一级令牌的身份等于原用户（典型为 OTS 提权），不自动启动新版")
            return None, 0, "none"

        for candidate, h_token, label in (
            (1, h_shell, "shell_token"),
            (2, h_linked, "linked_token"),
        ):
            if rung != candidate:
                continue
            h_primary = _duplicate_primary(h_token) if h_token else None
            if not h_primary:
                log(f"  {label}: 复制 primary token 失败，退化到 ShellExecuteExW")
                break
            try:
                handle, pid = _create_process_with_token(h_primary, exe_path, params, cwd)
            finally:
                _close_handle(h_primary)
            if handle:
                log(f"  已用 {label} 降权启动新版 (pid={pid})")
                return handle, pid, label
            log(f"  {label}: CreateProcessWithTokenW 未成功，退化到 ShellExecuteExW")
            break

        # 第 3 级：同一用户但完整性级别偏高。--data-dir 已转发，数据位置仍然正确。
        log("  使用退化路径 ShellExecuteExW（新版将继承管理员令牌）")
        handle, pid = _shell_execute_ex(exe_path, params, cwd)
        if handle:
            log(f"  已启动新版 (pid={pid})，注意：该实例以管理员权限运行")
            return handle, pid, "shell_execute"
        return None, 0, "none"
    finally:
        _close_handle(h_shell)
        _close_handle(h_linked)
        _close_handle(h_self)


# ─── Step 7：看门狗 ───────────────────────────────────────


def _process_alive(handle: int | None) -> bool:
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    except Exception:
        return False


def _read_ready_payload(ready_file: Path | None) -> dict | None:
    """读 READY 信号文件。任何异常都当作"还没写好"（None），下一轮再看。"""
    if ready_file is None:
        return None
    try:
        if not ready_file.exists():
            return None
        data = json.loads(ready_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def decide_watch_outcome(
    watch_mode: str,
    ready_payload: dict | None,
    expected_pid: int,
    expected_token: str,
    proc_alive: bool,
    elapsed: float,
    ready_timeout: float = READY_TIMEOUT,
    survival_grace: float = SURVIVAL_GRACE,
) -> str:
    """判定看门狗该 commit / rollback / 继续 wait。纯函数，Phase 8 直接单测。

    两套协议靠 watch_mode 分支 —— **"收不到 READY" 和 "根本没有 READY 通道"
    必须是不同的东西**。否则旧主程序（不传 --data-dir）拉起新 updater 时，一个
    完全健康的新版会在 90 秒后被判超时、TerminateProcess、回滚。纯粹自造的故障。

    `ready` 模式（新主程序，有 --data-dir）：
        PID 与 nonce 双匹配才 commit —— 一刀切掉 PID 重用、陈旧文件、意外写入。
    `survival` 模式（旧主程序）：弱监护，只能抓"起来就崩"，且**绝不 Terminate**。

    watch_mode 只有这两个取值；任何其它字符串按 survival 处理（宁可漏判不误杀）。
    """
    if watch_mode == "ready":
        if ready_payload is not None:
            if (
                ready_payload.get("pid") == expected_pid
                and ready_payload.get("token") == expected_token
            ):
                return "commit"
            return "rollback"
        if not proc_alive:
            return "rollback"
        if elapsed >= ready_timeout:
            return "rollback"
        return "wait"

    # survival：活过宽限期即认定启动成功，不再等、不再杀
    if elapsed >= survival_grace:
        return "commit"
    if not proc_alive:
        return "rollback"
    return "wait"


def _process_alive_by_pid(pid: int) -> bool:
    """无句柄时的退路（非 Windows，或拿到 pid 却没拿到句柄）。"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _commit_update(
    internal_old: Path,
    exe_old: Path,
    tmp_dir: Path,
    archive_path: Path,
    ready_file: Path | None,
) -> None:
    """COMMIT：新版本已证明自己能跑，删掉全部回滚素材。

    每一项都是 best-effort —— 走到这里更新**已经成功**，清理残留失败不该把
    成功报成失败。删不掉的备份由 main.py 的场景 A/B 兜底逻辑下次启动时重试。
    """
    log("COMMIT: 新版本已就绪，清理回滚素材...")
    if internal_old.exists():
        shutil.rmtree(internal_old, ignore_errors=True)
        if internal_old.exists():
            log("  _internal_old/ 清理未完全（主程序启动时会重试）")
        else:
            log("  _internal_old/ 已删除")
    if exe_old.exists():
        try:
            exe_old.unlink(missing_ok=True)
            log("  .exe.old 已删除")
        except OSError as e:
            log(f"  .exe.old 清理失败（主程序启动时会重试）: {e}")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        archive_path.unlink(missing_ok=True)
        log("  归档文件已删除")
    except OSError:
        pass
    # READY 文件是一次性握手凭据，用完即弃 —— 留着它下次启动会被当成陈旧信号
    if ready_file is not None:
        try:
            ready_file.unlink(missing_ok=True)
        except OSError:
            pass


def _spawn_updater_swap_helper(target: Path, staged: Path) -> None:
    """spawn 一个"等我退出之后再动手"的 cmd helper，把 staged 换成 target。

    `self_delete()` 那套延时 cmd 的推广版：同样是"当前进程没法处理自己的
    image，那就交给一个活得比自己久的外部进程"。区别是这里不是删而是换，
    所以多了一步备份与失败恢复。

    helper 由**提权的 updater** spawn，继承管理员令牌 —— 这是它能在
    `C:\\Program Files\\FluentYTDL` 里改文件的唯一原因。

    序列（三条语句用 `&` 无条件串联，只有中间那步内部有 `||` 兜底）：

        1. move /y  updater.exe      → updater.exe.old      # 备份
        2. move /y  updater.exe.new  → updater.exe          # 换上新的
           失败则 move /y updater.exe.old → updater.exe      # 还原
        3. del /f/q updater.exe.old                          # 清备份

    **为什么第 3 步不挂在第 2 步的 `&&` 上**：cmd 的 `A && B || C` 等价于
    `(A && B) || C`，于是"换成功了但备份删不掉"会把 C（还原）也触发一次 ——
    刚换上的新 updater 会被旧的盖回去。把 del 单独用 `&` 串在后面，语义才是
    "无论如何都试着清一下备份"。删不掉也无所谓：`main.py` 的 §4 会兜。

    任何一步失败的净效果都是"什么都没变，`.new` 还在原地" —— 下一次启动由
    `main.py` 的 §3 重试，或下一次更新由新 updater 重试。
    """
    if sys.platform != "win32":  # pragma: no cover - helper 是 Windows 专用路径
        return

    backup = target.with_name(target.name + ".old")
    swap = (
        f'move /y "{target}" "{backup}" >nul 2>&1'
        f' & (move /y "{staged}" "{target}" >nul 2>&1'
        f' || move /y "{backup}" "{target}" >nul 2>&1)'
        f' & del /f /q "{backup}" >nul 2>&1'
    )
    # ping -n 4 ≈ 3 秒：updater 在本函数返回后立刻 return 出 main()，解释器
    # teardown 只需毫秒级，3 秒是给"进程句柄真正 signaled、image 锁释放"留的
    # 宽裕余量。等太久没有坏处（helper 是后台无窗口进程），等不够则第 1 步
    # 会因文件被占用而失败，白跑一次。
    cmd = f"ping -n 4 127.0.0.1 >nul 2>&1 & {swap}"
    try:
        # **必须传字符串，不能传 ["cmd", "/c", cmd] 列表。** Windows 上列表形态会
        # 过一遍 `subprocess.list2cmdline()`，它按 C 运行时的规则把内嵌引号转义成
        # `\"` —— 而 cmd.exe **不认这种转义**，会把反斜杠原样留下。于是
        #     move /y "C:\...\updater.exe" ...
        # 到了 cmd 手里变成
        #     move /y \"C:\...\updater.exe\" ...
        # 路径被解析成 `\C:\...\updater.exe\`，每一条 move 都失败。而 Popen 成功、
        # 日志照样打印"已 spawn" —— 一次完全静默的失败。字符串形态在 Windows 上
        # 原样下发命令行，不经 list2cmdline。（extract_archive 的 7z 调用同理，
        # 那边用的是 shell=True + 字符串。）
        subprocess.Popen(
            f"cmd /c {cmd}",
            creationflags=HELPER_CREATIONFLAGS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"  已 spawn updater 自更新 helper: {staged.name} → {target.name}")
    except OSError as e:
        log(f"  spawn updater 自更新 helper 失败（.new 保留，下次重试）: {e}")


def _self_update_updater(dest_dir: Path) -> None:
    """Step 8：把同批投递的 `updater.exe.new` 换成正式的 `updater.exe`。

    **为什么 updater 必须能自更新**：所有更新流程的修复都住在 `updater.exe`
    里，而 `updater.exe` 被 app-core 归档明确排除（运行时被锁，覆写不了）。
    没有这条路径，已安装用户手上的 updater 永远是旧的那个 —— 带着 UAC
    无限递归 bug 的那个 —— 于是**永远收不到任何新版本，包括修这个 bug 的
    这一版**。这是个 bootstrap 死锁，`updater.exe.new` 就是钥匙。

    投递不需要 updater 侧任何新逻辑：`_move_extracted_files` 把 `.new` 当普通
    文件搬进安装目录就行，所以**旧 updater 也能正确投递它**。需要新逻辑的只有
    这里的"替换"动作。

    **只在 COMMIT 之后调用**。ROLLBACK 意味着这一批 app-core 被判定为坏的，
    同批的 `.new` 自然同样不可信 —— `_rollback_update` 会直接删掉它。

    两级策略：
      1. 先试 `os.replace`（原子）。目标不是正在运行的 image 时（开发模式、
         或 updater 从别处被拷起来跑）这一步就成了，零延时、可立即验证。
      2. 目标就是自己 → Windows 不允许替换正在执行的 image，`os.replace`
         抛 `PermissionError` → 落到退出后 helper。

    **不赌"运行中的 image 能否被 rename"**：Windows 确实允许 rename 正在执行
    的 image（只禁止 delete），但那属于实现细节，不该让更新链的关键一环依赖
    它。第 1 步失败就走 helper，两条路都不依赖这个细节。
    """
    staged = dest_dir / "updater.exe.new"
    if not staged.exists():
        return

    target = dest_dir / "updater.exe"
    log("Step 8: 发现 updater.exe.new，开始 updater 自更新")

    try:
        if staged.resolve() == target.resolve():
            # 理论上不该发生（两个不同的文件名），但 resolve() 会跟符号链接，
            # 真撞上了就直接放手 —— 继续下去会把 updater.exe 自己删掉。
            log("  跳过: .new 与 updater.exe 解析到同一路径")
            return
    except OSError:
        pass

    try:
        os.replace(staged, target)
        log("  updater.exe 已就地替换（目标未被占用）")
        return
    except OSError as e:
        # 绝大多数情况会走到这里：target 就是正在跑的这个 image。
        log(f"  就地替换不可行（{e.__class__.__name__}），改用退出后 helper")

    _spawn_updater_swap_helper(target, staged)


def _rollback_update(
    exe_path: Path,
    internal_dir: Path,
    internal_old: Path,
    exe_old: Path,
    origin_sid: str,
) -> None:
    """ROLLBACK：把安装目录还原到更新前的状态，然后启动旧版本。

    这些删除/改名之所以能成立，全靠 updater **自己仍持有管理员令牌** ——
    `_internal_old` 是提权进程创建的、继承 `{app}` 的 ACL，普通权限的新版
    连删都删不掉。这正是把回滚从应用侧搬到 updater 侧的根本原因。
    """
    log("ROLLBACK: 还原到更新前的版本...")
    if internal_dir.exists():
        shutil.rmtree(internal_dir, ignore_errors=True)
    if internal_old.exists() and not internal_dir.exists():
        try:
            internal_old.rename(internal_dir)
            log("  _internal_old/ → _internal/ 已还原")
        except OSError as e:
            log(f"  还原 _internal 失败: {e}")
    if exe_path.exists():
        try:
            exe_path.unlink(missing_ok=True)
        except OSError as e:
            log(f"  删除新版 exe 失败: {e}")
    if exe_old.exists() and not exe_path.exists():
        try:
            exe_old.rename(exe_path)
            log(f"  .exe.old → {exe_path.name} 已还原")
        except OSError as e:
            log(f"  还原 exe 失败: {e}")

    # 这一批 app-core 已被判定为坏的，同批投递的 updater.exe.new 同样不可信
    updater_new = exe_path.parent / "updater.exe.new"
    if updater_new.exists():
        try:
            updater_new.unlink(missing_ok=True)
            log("  已删除同批投递的 updater.exe.new（该构建被判定为坏）")
        except OSError:
            pass

    log("  启动旧版本...")
    # 回滚启动**不传** --data-dir / --update-ready-token：旧版不认识这些参数，
    # 传了会让它 SystemExit(2)，把"已还原"变成"什么都起不来"。
    h_old, old_pid, mode = _launch_medium(exe_path, exe_path.parent, [], origin_sid)
    # 这里不监护旧版（它本来就是能跑的那一版），句柄立刻归还。进程退出时 OS 也会
    # 回收，但显式关掉才与 Step 6/7 的其余路径保持同一套约定。
    _close_handle(h_old)
    if old_pid:
        log(f"  旧版本已启动 (pid={old_pid}, mode={mode})")
    else:
        log("  警告: 旧版本未能自动启动，需用户手动启动")


def main() -> int:
    parser = argparse.ArgumentParser(description="FluentYTDL 独立更新器")
    parser.add_argument("--pid", type=int, required=True, help="主进程 PID")
    parser.add_argument("--archive", required=True, help="更新归档文件路径 (7z/zip)")
    parser.add_argument("--dest", required=True, help="应用安装目录")
    parser.add_argument("--exe", default="FluentYTDL.exe", help="主程序可执行文件名")
    parser.add_argument("--timeout", type=int, default=30, help="等待进程退出的超时秒数")
    parser.add_argument(
        "--elevated",
        action="store_true",
        help="内部哨兵：本进程由 _elevate_self 提权启动，禁止再次提权（防 UAC 递归）",
    )
    # 下面两个参数由**新**主程序传入（旧主程序不认识它们，见
    # component_update_manager::launch_pending_updater() 的 PE 版本资源能力探测）。
    # 缺省空串 = 旧主程序拉起 → 监护退化为 survival 模式、降权启动跳过身份校验。
    parser.add_argument(
        "--data-dir",
        default="",
        help="主程序的用户数据目录（.update_ready 信号落点）。为空则退化为 survival 监护",
    )
    parser.add_argument(
        "--origin-user-sid",
        default="",
        help="发起更新的原用户 TokenUser SID（S-1-5-21-...），用于降权启动的身份准入",
    )
    args = parser.parse_args()

    archive_path = Path(args.archive)
    dest_dir = Path(args.dest)
    exe_name = args.exe
    exe_path = dest_dir / exe_name

    # 初始化文件日志（console=False 后 stderr 不可见）
    _init_log(dest_dir)
    log_path = dest_dir / "logs" / "updater.log"

    log("=" * 50)
    log("FluentYTDL 更新器启动")
    log(f"  PID: {args.pid}")
    log(f"  归档: {archive_path}")
    log(f"  目标: {dest_dir}")
    log(f"  可执行文件: {exe_name}")
    log(f"  已提权实例: {args.elevated}")
    log(f"  数据目录: {args.data_dir or '(未提供 → survival 监护模式)'}")
    log(f"  原用户 SID: {args.origin_user_sid or '(未提供 → 跳过身份校验)'}")
    log("=" * 50)

    # 验证归档文件存在
    if not archive_path.exists():
        log(f"错误: 归档文件不存在: {archive_path}")
        _alert(
            "更新未能开始：找不到已下载的更新包。\n\n"
            f"{archive_path}\n\n"
            "请重新在设置页检查更新。程序本体未被改动，可以照常使用。\n\n"
            f"详细日志：{log_path}"
        )
        return 1

    # 验证目标目录存在
    if not dest_dir.exists():
        log(f"错误: 目标目录不存在: {dest_dir}")
        return 1

    # 检查是否需要管理员权限（Program Files / 只读安装位置场景）
    if request_admin_if_needed(dest_dir, already_elevated=args.elevated):
        # 已启动提权进程，当前进程退出
        return 0

    # 提权没有发生（不需要 / 用户点了"否" / ShellExecuteW 失败）时，必须在动任何
    # 文件之前确认目标目录真的可写。
    #
    # 旧行为：_elevate_self() 返回 False 后 updater **继续往下走**，一路走到 Step 4
    # 把 _internal 改名成 _internal_old 才因为权限失败，留下一个半残安装 —— 用户
    # 只是在 UAC 框上点了"否"，代价却是程序起不来了。前置中止让"拒绝提权"变成
    # 零副作用。
    if not _can_write_dir(dest_dir):
        log("目标目录不可写且未获得管理员权限，中止更新（未做任何改动）")
        _alert(
            "更新未能开始：没有权限修改安装目录。\n\n"
            f"{dest_dir}\n\n"
            "如果刚才的管理员授权对话框被取消，请重新检查更新并选择「是」。\n"
            "程序本体未被改动，可以照常使用。\n\n"
            f"详细日志：{log_path}"
        )
        return 2

    # 等待主进程退出
    log("等待主进程退出...")
    if not wait_for_process(args.pid, args.timeout):
        log("警告: 等待超时，尝试继续替换...")

    # 额外等待一小段时间，确保文件句柄释放
    time.sleep(0.5)

    # === 新流程：先解压到临时目录，验证后再原子替换 ===
    # 这确保即使解压失败也不会影响现有文件
    tmp_dir = dest_dir / "_update_tmp"

    # 清理可能残留的旧临时目录
    if tmp_dir.exists():
        log("清理残留的 _update_tmp/ 目录...")
        shutil.rmtree(tmp_dir, ignore_errors=True)

    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: 解压归档到临时目录（不触碰运行中的文件）
    log("Step 1: 解压新版本到临时目录...")
    try:
        extract_archive(archive_path, tmp_dir)
    except Exception as e:
        log(f"解压失败: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # 此刻安装目录一个文件都没动过（解压只写 _update_tmp/，已清理）→ 可以安全阻塞
        _alert(
            "更新未能完成：更新包解压失败。\n\n"
            f"{e}\n\n"
            "程序本体未被改动，可以照常使用。请稍后重新检查更新。\n\n"
            f"详细日志：{log_path}"
        )
        return 1

    # Step 2: 验证解压结果
    log("Step 2: 验证解压结果...")
    if not _verify_extraction(tmp_dir, exe_name):
        log("解压验证失败，中止更新")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _alert(
            "更新未能完成：更新包内容不完整，已中止。\n\n"
            "程序本体未被改动，可以照常使用。请稍后重新检查更新。\n\n"
            f"详细日志：{log_path}"
        )
        return 1
    log("解压验证通过")

    # Step 3: 准备备份变量
    internal_dir = dest_dir / "_internal"
    internal_old = dest_dir / "_internal_old"
    exe_old = exe_path.with_suffix(".exe.old")

    # 清理上次更新可能残留的旧备份
    if internal_old.exists():
        log("清理旧备份目录 _internal_old/ ...")
        shutil.rmtree(internal_old, ignore_errors=True)
    if exe_old.exists():
        exe_old.unlink(missing_ok=True)

    # Step 4: 重命名旧文件为备份
    if internal_dir.exists():
        log("Step 4: 重命名 _internal/ → _internal_old/ ...")
        try:
            internal_dir.rename(internal_old)
        except OSError as e:
            log(f"重命名 _internal 失败: {e}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            # rename 失败意味着它压根没动，状态已收敛
            _alert(
                "更新未能完成：程序文件正被占用，无法替换。\n\n"
                f"{e}\n\n"
                "请确认 FluentYTDL 已完全退出（检查任务管理器与托盘图标）后重试。\n"
                "程序本体未被改动，可以照常使用。\n\n"
                f"详细日志：{log_path}"
            )
            return 1

    if exe_path.exists():
        log(f"重命名 {exe_name} → {exe_name}.old ...")
        try:
            exe_path.rename(exe_old)
        except OSError as e:
            log(f"重命名 {exe_name} 失败: {e}")
            # 回滚 _internal 重命名
            if internal_old.exists() and not internal_dir.exists():
                internal_old.rename(internal_dir)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            # 上面这次回滚之后状态已收敛，才敢弹阻塞窗口
            _alert(
                f"更新未能完成：{exe_name} 正被占用，无法替换。\n\n"
                f"{e}\n\n"
                "请确认 FluentYTDL 已完全退出（检查任务管理器与托盘图标）后重试。\n"
                "已还原到更新前的状态，程序可以照常使用。\n\n"
                f"详细日志：{log_path}"
            )
            return 1

    # Step 5: 从临时目录移动文件到目标目录
    log("Step 5: 移动新文件到目标目录...")
    if not _move_extracted_files(tmp_dir, dest_dir):
        log("移动文件失败，执行完整回滚...")
        # 删除可能已移动的不完整文件
        if internal_dir.exists():
            shutil.rmtree(internal_dir, ignore_errors=True)
        if exe_path.exists():
            exe_path.unlink(missing_ok=True)
        # 恢复备份
        if internal_old.exists() and not internal_dir.exists():
            internal_old.rename(internal_dir)
        if exe_old.exists() and not exe_path.exists():
            exe_old.rename(exe_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # 回滚已完成（要么恢复了备份，要么本来就没备份可恢复）→ 状态收敛，可以阻塞
        _alert(
            "更新未能完成：复制新版本文件时出错，已还原到旧版本。\n\n"
            "程序可以照常使用。请稍后重新检查更新。\n\n"
            f"详细日志：{log_path}"
        )
        return 1

    # 清理临时目录
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # Step 5.5: 通知外壳刷新图标缓存
    #
    # 落在这里而不是 Step 6 之后：新 exe 已经就位、还没有人去读它的图标，这一刻发通知
    # 命中率最高。反过来说，等新版起来了再发，任务栏上那格图标已经被旧缓存画出来了。
    _notify_shell_icon_change(exe_path)

    # Step 6: 降权启动新版本并接管监护
    #
    # 下面这个失败分支对应方案里的"更新已完成但无法启动"：文件替换已经全部做完，
    # 安装目录里是一套完整可用的新版本，失败只发生在**启动器**这一步。所以：
    #   - 不回滚（二进制已通过 _verify_extraction，回滚只会把好版本换回旧版本，
    #     而且会在 Program Files 里留下谁也删不掉的备份）
    #   - 状态已收敛（`_internal_old` / `.exe.old` 只是待清理的备份，不是半残状态；
    #     `_update_tmp` 已在上面删掉），可以安全地弹阻塞窗口
    #   - 文案必须告诉用户"更新成功了，手动启动就行"，否则用户只会看到程序凭空消失
    log(f"Step 6: 启动新版本: {exe_path}")
    if not exe_path.exists():
        log(f"错误: 新版本 {exe_path} 不存在")
        _alert(
            "更新已完成，但找不到新版本的可执行文件。\n\n"
            f"{exe_path}\n\n"
            "请从开始菜单或桌面快捷方式手动启动 FluentYTDL。\n\n"
            f"详细日志：{log_path}"
        )
        return 1

    # READY 通道：有 --data-dir 才存在。**"收不到 READY" 与"根本没有 READY 通道"
    # 是两件不同的事** —— 后者绝不能走进 90s 超时分支，否则旧主程序拉起新 updater 时，
    # 一个完全健康的新版会被 TerminateProcess 掉。
    watch_mode = "ready" if args.data_dir else "survival"
    ready_file = Path(args.data_dir) / ".update_ready" if args.data_dir else None
    ready_token = secrets.token_hex(16)

    extra_args: list[str] = []
    if ready_file is not None:
        # 陈旧信号防护：先删一次（updater 有权限），确保看到的任何 READY 都是本次写的
        try:
            ready_file.unlink(missing_ok=True)
        except OSError as e:
            log(f"  警告: 无法删除陈旧的 .update_ready: {e}")
        # --data-dir 一路转发给新版：即使走第 3 级退化路径（继承管理员令牌），
        # 数据目录也不会随被继承的环境变量漂移。
        extra_args = ["--data-dir", args.data_dir, "--update-ready-token", ready_token]

    h_new, new_pid, launch_mode = _launch_medium(
        exe_path, dest_dir, extra_args, args.origin_user_sid
    )

    if launch_mode == "none" or not new_pid:
        _commit_update(internal_old, exe_old, tmp_dir, archive_path, ready_file)
        _alert(
            "更新已完成，但新版本没能自动启动。\n\n"
            "请手动双击 FluentYTDL 图标启动程序，你的配置与下载记录都还在。\n\n"
            f"详细日志：{log_path}"
        )
        # Step 8 放在 _alert **之后**：helper 会延时约 3 秒再动手，而 _alert 是
        # 阻塞的 —— 放在前面的话，用户盯着弹窗那几分钟里 helper 早就跑完并因为
        # updater.exe 仍被占用而失败了，白跑一次。这里也不违反"_alert 之后紧接
        # return"的约束：spawn 是个 detached 后台进程，是本进程的最后一个动作，
        # 安装目录的状态在 _commit_update 那一刻就已经收敛。
        _self_update_updater(dest_dir)
        return 1

    # Step 7: 看门狗 —— updater 不再打完就跑，它留下来看着新版起不起得来
    log(f"Step 7: 看门狗接管 (watch_mode={watch_mode}, pid={new_pid}, mode={launch_mode})")
    start = time.monotonic()
    outcome = "wait"
    alive = True
    try:
        while True:
            elapsed = time.monotonic() - start
            alive = _process_alive(h_new) if h_new else _process_alive_by_pid(new_pid)
            outcome = decide_watch_outcome(
                watch_mode,
                _read_ready_payload(ready_file),
                new_pid,
                ready_token,
                alive,
                elapsed,
            )
            if outcome != "wait":
                log(f"  看门狗判定: {outcome} (elapsed={elapsed:.1f}s, alive={alive})")
                break
            time.sleep(WATCH_POLL_INTERVAL)

        if outcome == "commit":
            _commit_update(internal_old, exe_old, tmp_dir, archive_path, ready_file)
            # Step 8: updater 自更新。只在 COMMIT 后执行 —— ROLLBACK 说明这批
            # 构建是坏的，同批的 .new 同样不可信（回滚流程里会删掉它）。
            _self_update_updater(dest_dir)
            log("更新器退出 (COMMIT)")
            return 0

        # ROLLBACK。仅在进程还活着时才需要杀 —— 覆盖"90s 超时"与"READY 内容不匹配"
        # 两种分支；进程自己已经退出的情况下 TerminateProcess 毫无意义。
        if alive and watch_mode == "ready" and h_new:
            log("  终止未能就绪的新版本进程...")
            try:
                ctypes.windll.kernel32.TerminateProcess(h_new, 1)
                ctypes.windll.kernel32.WaitForSingleObject(h_new, 5000)
            except Exception as e:
                log(f"  TerminateProcess 失败: {e}")
    finally:
        # 句柄必须活到 commit/rollback 判完，也必须在这里归还
        _close_handle(h_new)

    _rollback_update(exe_path, internal_dir, internal_old, exe_old, args.origin_user_sid)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    # 回滚已全部完成、旧版已启动 → 状态收敛，才敢弹阻塞窗口
    _alert(
        "新版本启动失败，已还原到旧版本。\n\n"
        "旧版本正在启动，你的配置与下载记录都还在。请稍后重新检查更新。\n\n"
        f"详细日志：{log_path}"
    )
    log("更新器退出 (ROLLBACK)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
