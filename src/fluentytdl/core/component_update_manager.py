"""
FluentYTDL 统一组件更新协调器

协调 app-core 和 bin/ 工具的版本检查与更新。
通过 GitHub Release 的 update-manifest.json 统一管理所有组件版本。

版本通道:
  - stable（默认）：只接收正式版
  - pre：接收正式版与 rc 预发布，所有已安装版本均可检查更新
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..utils.logger import logger
from ..utils.paths import frozen_app_dir, is_frozen
from .config_manager import config_manager

# ─── 常量 ────────────────────────────────────────────────

REPO_OWNER = "SakuraForgot"
REPO_NAME = "FluentYTDL"
GITHUB_API_BASE = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

MANIFEST_FILENAME = "update-manifest.json"
# RAW 直链：releases/latest/download/ 会自动 302 重定向到最新 release 的 asset
# 完全绕过 GitHub API 速率限制（无 token 时 60 次/小时）
MANIFEST_RAW_URL = (
    f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/latest/download/{MANIFEST_FILENAME}"
)

# ─── 版本比较 ────────────────────────────────────────────


def _parse_version(ver: str) -> tuple[int, ...]:
    """将 '3.0.0' 或 'v3.0.0' 解析为可比较的整数元组。

    `^(v-?|pre-|beta-)` 分支保留是为了兼容 3.5.5 之前的前缀格式 —— 旧客户端会
    读到新 manifest，新客户端也可能读到升级前遗留的旧 VERSION 文件。
    """
    clean = re.sub(r"^(v-?|pre-|beta-)", "", str(ver).strip())
    clean = clean.split("-")[0]
    parts: list[int] = []
    for p in clean.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


# updater.exe 从这个版本起认识 --data-dir / --origin-user-sid。
# 更早的版本用 argparse，遇到未知参数会直接 SystemExit(2)，整次更新失败。
UPDATER_MIN_VERSION_FOR_DATA_DIR = (3, 6, 6)


def _read_pe_file_version(exe_path: Path) -> tuple[int, int, int] | None:
    """读 exe 的 PE 版本资源，返回 `(major, minor, patch)`；任何失败返回 `None`。

    这是一次**能力探测**，不是装饰：已安装用户手上的 `updater.exe` 可能任意旧
    （它不在 app-core 归档里，只随 full.7z / setup.exe 分发），而给旧 argparse
    传新参数会让整次更新以 `SystemExit(2)` 告终。读不到版本 → 一律当旧版处理。

    版本资源由 `scripts/build.py::generate_version_info()` 写入，形状是
    `filevers=(major, minor, patch, 0)`；第四位是占位的 build 号，不参与比较。
    """
    if sys.platform != "win32":
        return None
    if not exe_path.exists():
        return None
    try:
        import ctypes

        version_dll = ctypes.WinDLL("version", use_last_error=True)
        path_str = str(exe_path)

        size = version_dll.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path_str), None)
        if not size:
            return None

        buf = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(
            ctypes.c_wchar_p(path_str), 0, size, ctypes.byref(buf)
        ):
            return None

        # VerQueryValueW 返回的是指向 buf 内部的指针，不需要释放。
        block = ctypes.c_void_p()
        length = ctypes.c_uint(0)
        if not version_dll.VerQueryValueW(
            ctypes.byref(buf),
            ctypes.c_wchar_p("\\"),
            ctypes.byref(block),
            ctypes.byref(length),
        ):
            return None
        if length.value < 4 * 13:  # VS_FIXEDFILEINFO 是 13 个 DWORD
            return None

        # VS_FIXEDFILEINFO: [0]=dwSignature [1]=dwStrucVersion
        #                   [2]=dwFileVersionMS [3]=dwFileVersionLS
        fields = ctypes.cast(block, ctypes.POINTER(ctypes.c_ulong * 13)).contents
        if fields[0] != 0xFEEF04BD:  # VS_FFI_SIGNATURE
            return None
        ver_ms, ver_ls = fields[2], fields[3]
        return (ver_ms >> 16 & 0xFFFF, ver_ms & 0xFFFF, ver_ls >> 16 & 0xFFFF)
    except Exception as e:
        log_text(logger, "debug", "[ComponentUpdate] 读取 PE 版本资源失败 ({0}): {1}", exe_path, e)
        return None


def _get_update_channel() -> str:
    """User preference is independent of the installed build; default to stable."""
    return "pre" if config_manager.get("app_update_channel", "stable") == "pre" else "stable"


class _ManifestWorker(QThread):
    """Fetch a fresh manifest through the selected update-check source."""

    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, release_tag: str = "", check_session=None):
        super().__init__()
        from .update_transport import configured_check

        self.release_tag = release_tag
        self.channel = _get_update_channel()
        self.check_session = check_session or configured_check()

    def run(self) -> None:
        try:
            self.finished.emit(self.check_session.manifest(self.channel))
        except Exception as error:
            self.error.emit(str(error))


# ─── 下载线程 ────────────────────────────────────────────


class _DownloadWorker(QThread):
    """后台线程：下载更新文件"""

    progress = Signal(int)  # 0-100
    finished = Signal(str)  # 本地文件路径
    error = Signal(str)

    def __init__(self, url: str, expected_sha256: str = ""):
        super().__init__()
        self.url = url
        self.expected_sha256 = expected_sha256

    def run(self) -> None:
        try:
            self.finished.emit(self._download_once())
        except Exception as error:
            self.error.emit(str(error))

    def _download_once(self) -> str:
        import tempfile
        from urllib.parse import unquote, urlsplit

        from .update_transport import configured_transport

        directory = Path(tempfile.mkdtemp(prefix="fluentytdl_update_"))
        filename = Path(unquote(urlsplit(self.url).path)).name
        destination = directory / filename
        try:
            configured_transport().download(
                self.url, destination, self.expected_sha256, self.progress.emit
            )
        except Exception:
            directory.rmdir()
            raise
        return str(destination)


# ─── 主管理器 ────────────────────────────────────────────


class ComponentUpdateManager(QObject):
    """统一组件更新协调器"""

    # 清单信号
    manifest_fetched = Signal(dict)
    manifest_error = Signal(str)

    # app-core 信号
    app_update_available = Signal(dict)  # {version, tag, changelog, url, sha256, is_prerelease}
    app_no_update = Signal()
    app_check_error = Signal(str)
    app_check_started = Signal()

    # 下载信号
    download_progress = Signal(int)
    download_finished = Signal(str)  # 本地路径
    download_error = Signal(str)

    # 应用更新信号（Phase 5 状态机）
    apply_requested = Signal()  # 校验通过、可以退出了 → 主窗口执行优雅退出
    apply_error = Signal(str)  # 终止性失败，UI 负责告知用户
    apply_confirm_needed = Signal(int, int)  # (活跃任务数, generation)

    # 通用信号
    channel_changed = Signal(str)
    channel_change_requested = Signal(str)

    check_complete = Signal(list)  # 所有组件检查结果列表

    def __init__(self) -> None:
        super().__init__()
        self.channel_change_requested.connect(self.set_update_channel)
        self._manifest: dict | None = None
        self._manifest_worker: _ManifestWorker | None = None
        self._download_worker: _DownloadWorker | None = None

        # 更新状态机：IDLE / AWAITING_CONFIRM / APPROVED / QUITTING / LAUNCHED
        self._update_state: str = "IDLE"
        self._pending_update: dict | None = None
        self._pending_generation: int = 0
        # download_finished 目前有两个转发者（设置页卡片 + 更新弹窗），同一个归档
        # 会驱动两次 apply 流程。_DownloadWorker 每次下载都新建临时目录，所以
        # "同一个归档路径" 必然意味着同一次下载 —— 据此去重，而不是去猜调用者。
        self._requested_archives: set[str] = set()
        # 本轮 app 更新检查是否为「自动/静默」触发。静默时 UI 不弹任何 InfoBar，
        # 有更新才写进消息中心。emit 是同步的，所以槽里读这个标记是安全的。
        self._app_check_silent: bool = False

    @property
    def is_silent_check(self) -> bool:
        """当前这轮 app 更新检查是否由自动（启动/定时）逻辑触发。"""
        return self._app_check_silent

    @property
    def manifest(self) -> dict | None:
        return self._manifest

    def set_update_channel(self, channel: str) -> None:
        if channel not in {"stable", "pre"} or channel == _get_update_channel():
            return
        if (
            (self._manifest_worker and self._manifest_worker.isRunning())
            or (self._download_worker and self._download_worker.isRunning())
            or self._update_state != "IDLE"
        ):
            return
        config_manager.set("app_update_channel", channel)
        self._manifest = None
        self.channel_changed.emit(channel)
        self.check_app_update(silent=False)

    # ── 清单获取 ──────────────────────────────────────────

    def fetch_manifest(self, check_session=None) -> None:
        """异步获取更新清单。"""
        if self._manifest_worker and self._manifest_worker.isRunning():
            return
        worker = _ManifestWorker(release_tag="", check_session=check_session)
        worker.finished.connect(self._on_manifest_fetched)
        worker.error.connect(self._on_manifest_error)
        self._manifest_worker = worker
        worker.start()

    def _on_manifest_fetched(self, manifest: dict) -> None:
        self._manifest = manifest
        log_text(
            logger, "info", "[ComponentUpdate] 清单获取成功: {0}", manifest.get("app_version", "?")
        )
        self.manifest_fetched.emit(manifest)

    def _on_manifest_error(self, msg: str) -> None:
        log_text(logger, "warning", "[ComponentUpdate] 清单获取失败: {0}", msg)
        self.manifest_error.emit(msg)
        if getattr(self, "_manifest_app_check_conn", False):
            self._manifest_app_check_conn = False
            try:
                self.manifest_fetched.disconnect(self._on_manifest_for_app_check)
            except RuntimeError:
                pass
            self.app_check_error.emit(msg)

    # ── 统一检查 ──────────────────────────────────────────

    def check_all(self) -> None:
        """检查所有组件更新（app-core + bin/ 工具）。"""
        # 先获取清单
        self.fetch_manifest()

    def check_app_update(self, silent: bool = False, check_session=None) -> None:
        """仅检查 app-core 更新。

        ``silent=True`` 表示自动（启动/定时）检查：UI 侧读 :attr:`is_silent_check`
        决定不弹「已是最新」「检查失败」这类提示，只有真有更新才进消息中心。
        """
        if (
            self._download_worker and self._download_worker.isRunning()
        ) or self._update_state != "IDLE":
            return
        self._app_check_silent = silent
        self.app_check_started.emit()
        if not getattr(self, "_manifest_app_check_conn", False):
            self._manifest_app_check_conn = True
            self.manifest_fetched.connect(self._on_manifest_for_app_check)
        self.fetch_manifest(check_session=check_session)

    def _on_manifest_for_app_check(self, _manifest: dict) -> None:
        """清单获取完成后比对 app 版本（一次性回调）。"""
        try:
            self.manifest_fetched.disconnect(self._on_manifest_for_app_check)
        except RuntimeError:
            pass
        self._manifest_app_check_conn = False
        self._compare_app_version()

    def _compare_app_version(self) -> None:
        """按所选通道比对 app-core 完整版本。"""
        if not self._manifest:
            self.app_check_error.emit(tr_text("清单未获取"))
            return

        try:
            from fluentytdl import __version__
        except ImportError:
            self.app_check_error.emit(tr_text("无法获取当前版本"))
            return

        manifest_version = str(self._manifest.get("app_version", "")).strip()
        manifest_tag = self._manifest.get("release_tag", "") or f"v{manifest_version}"

        from ..utils.app_version import public_version, version_key

        channel = _get_update_channel()
        is_pre = bool(self._manifest.get("_is_prerelease")) or "-rc." in manifest_version
        if not public_version(manifest_version, channel) or (channel == "stable" and is_pre):
            self.app_no_update.emit()
            return
        try:
            current, latest = version_key(__version__), version_key(manifest_version)
        except ValueError as error:
            self.app_check_error.emit(str(error))
            return
        if latest <= current:
            self.app_no_update.emit()
            return
        skipped = str(config_manager.get(f"skipped_{channel}_version") or "")
        if skipped and skipped == manifest_version and self._app_check_silent:
            self.app_no_update.emit()
            return

        # 获取 app-core 组件信息
        app_core = self._manifest.get("components", {}).get("app-core", {})

        self.app_update_available.emit(
            {
                "version": manifest_version,
                "tag": manifest_tag,
                "changelog": self._manifest.get("changelog", ""),
                "url": app_core.get("url", ""),
                "sha256": app_core.get("sha256", ""),
                "size": app_core.get("size", 0),
                "is_prerelease": is_pre,
                "channel": channel,
                "silent": self._app_check_silent,
            }
        )

    # ── 下载 app-core 更新 ────────────────────────────────

    def download_app_update(self, url: str, sha256: str = "") -> None:
        """下载 app-core 更新归档。"""
        if not url:
            self.download_error.emit(tr_text("下载 URL 为空"))
            return

        worker = _DownloadWorker(url, sha256)
        worker.progress.connect(self.download_progress)
        worker.finished.connect(self._on_download_done)
        worker.error.connect(self.download_error)
        self._download_worker = worker
        worker.start()

    def _on_download_done(self, path: str) -> None:
        self.download_finished.emit(path)

    # ── 应用 app-core 更新 ────────────────────────────────
    #
    # 状态机（Phase 5）：
    #   IDLE ──request_app_core_update() 校验通过──┬─无活跃任务─→ APPROVED
    #                                             └─有活跃任务─→ AWAITING_CONFIRM
    #   AWAITING_CONFIRM ──confirm_pending_update(gen)──→ APPROVED
    #   AWAITING_CONFIRM ──cancel_pending_update(gen)───→ IDLE
    #   APPROVED ──emit apply_requested──→ QUITTING
    #   QUITTING ──app.exec() 返回后 Popen 成功──→ LAUNCHED
    #   任意状态 ──校验失败──→ IDLE + emit apply_error(msg)
    #
    # 为什么不在这里 Popen + sys.exit()：`sys.exit()` 抛的是 SystemExit
    # （BaseException），从 Qt 槽栈抛出后 app.exec() 不会正常返回，main.py 的收尾
    # 全部跳过 —— yt-dlp 子进程成孤儿、db_writer 未落盘的数据丢失。更要紧的是
    # download_manager.shutdown() 逐 worker 串行，最坏能超过 updater 的等待预算，
    # 那时 updater 会去替换一个还在运行的进程的文件。所以 Popen 挪到 app.exec()
    # 返回之后（launch_pending_updater()），当作进程的最后一个动作。

    @staticmethod
    def _resolve_updater_path() -> Path | None:
        """定位 updater.exe（应用目录根 → `_internal/` 回退），找不到返回 None。"""
        app_dir = frozen_app_dir()
        candidate = app_dir / "updater.exe"
        if candidate.exists():
            return candidate
        candidate = app_dir / "_internal" / "updater.exe"
        return candidate if candidate.exists() else None

    def has_pending_update(self) -> bool:
        """是否有已暂存、尚未启动的更新。"""
        return self._pending_update is not None

    def request_app_core_update(self, archive_path: str) -> None:
        """请求应用 app-core 更新：只做校验 + 暂存，绝不在此退出进程。

        校验失败 → `apply_error`；有活跃下载 → `apply_confirm_needed`；
        否则直接批准并 emit `apply_requested`，由主窗口执行优雅退出。
        """
        # download_finished 有两个转发者，同一个归档会到两次。归档路径来自
        # _DownloadWorker 每次新建的临时目录，路径相同即同一次下载。
        if archive_path in self._requested_archives:
            log_text(logger, "debug", "[ComponentUpdate] 忽略重复的更新请求: {0}", archive_path)
            return
        self._requested_archives.add(archive_path)

        # 已经决定退出了，任何来源都不允许再把流程重新拉起来
        if self._update_state in ("QUITTING", "LAUNCHED"):
            log_text(
                logger,
                "warning",
                "[ComponentUpdate] 状态 {0}，忽略新的更新请求",
                self._update_state,
            )
            return

        if not archive_path or not Path(archive_path).exists():
            self._fail_pending(tr_text("更新归档不存在: {0}", archive_path))
            return

        updater_path = self._resolve_updater_path()
        if updater_path is None:
            self._fail_pending(tr_text("updater.exe 不存在: {0}", frozen_app_dir() / "updater.exe"))
            return

        self._pending_generation += 1
        gen = self._pending_generation
        self._pending_update = {
            "archive": archive_path,
            "updater": str(updater_path),
            "generation": gen,
        }

        # 活跃下载拦截。Core 不能在 import 期依赖 Service 层（CLAUDE.md §2），
        # 故函数级 lazy import；导入失败时**放行** —— 被中断的下载可从数据库恢复，
        # 而一个写着"0 个任务"的确认框是更糟的 bug。
        active = 0
        try:
            from ..download.download_manager import download_manager

            if download_manager.has_active_tasks():
                active = download_manager.running_count() + download_manager.pending_count()
        except Exception as e:
            log_text(logger, "warning", "[ComponentUpdate] 无法查询活跃任务，按无任务处理: {0}", e)

        if active > 0:
            self._update_state = "AWAITING_CONFIRM"
            log_text(
                logger,
                "info",
                "[ComponentUpdate] 有 {0} 个活跃任务，等待用户确认 (gen={1})",
                active,
                gen,
            )
            self.apply_confirm_needed.emit(active, gen)
            return

        self._approve(gen)

    def confirm_pending_update(self, gen: int) -> None:
        """用户确认中断活跃任务并继续更新。"""
        if self._update_state != "AWAITING_CONFIRM":
            log_text(logger, "warning", "[ComponentUpdate] 状态 {0}，忽略确认", self._update_state)
            return
        if gen != self._pending_generation:
            log_text(
                logger,
                "info",
                "[ComponentUpdate] 忽略过期 generation 的确认: {0} != {1}",
                gen,
                self._pending_generation,
            )
            return
        self._approve(gen)

    def cancel_pending_update(self, gen: int) -> None:
        """用户取消更新，回到 IDLE。"""
        if self._update_state != "AWAITING_CONFIRM":
            log_text(logger, "warning", "[ComponentUpdate] 状态 {0}，忽略取消", self._update_state)
            return
        if gen != self._pending_generation:
            log_text(
                logger,
                "info",
                "[ComponentUpdate] 忽略过期 generation 的取消: {0} != {1}",
                gen,
                self._pending_generation,
            )
            return
        # 状态机已经拦得住 launch_pending_updater()，清空 pending 是双保险
        self._pending_update = None
        self._update_state = "IDLE"
        log_text(logger, "info", "[ComponentUpdate] 用户取消了更新 (gen={0})", gen)

    def _fail_pending(self, msg: str) -> None:
        """终止性校验失败：回到 IDLE 并把消息交给 UI。"""
        logger.error(f"[ComponentUpdate] {msg}")
        self._pending_update = None
        self._update_state = "IDLE"
        self.apply_error.emit(msg)

    def _approve(self, gen: int) -> None:
        """批准更新并请求退出。"""
        self._update_state = "APPROVED"
        log_text(logger, "info", "[ComponentUpdate] 更新已批准 (gen={0})，请求优雅退出", gen)
        # 先切 QUITTING 再 emit：这次 emit 本身就是 APPROVED→QUITTING 这条边，
        # 任何在 emit 期间重入的观察者都必须已经看到 QUITTING。
        self._update_state = "QUITTING"
        self.apply_requested.emit()

    def launch_pending_updater(self) -> None:
        """启动 updater.exe。**必须是进程退出前的最后一个动作。**

        只认 `QUITTING` —— 这是整个流程的安全底线：它杜绝了"用户点右上角关闭窗口，
        结果触发一次更新替换"。由 `main.py` 在 `app.exec()` 返回之后调用。
        """
        if self._update_state == "IDLE":
            # 绝大多数退出都走这条路（普通关窗口），不留日志噪音
            return
        if self._update_state != "QUITTING":
            log_text(
                logger, "warning", "[ComponentUpdate] 状态 {0}，不启动 updater", self._update_state
            )
            return

        pending = self._pending_update
        if not pending:
            log_text(logger, "error", "[ComponentUpdate] 状态为 QUITTING 但没有 pending 更新")
            return

        app_dir = frozen_app_dir()
        exe_name = Path(sys.executable).name if is_frozen() else "FluentYTDL.exe"
        updater_path = pending["updater"]
        archive_path = pending["archive"]
        pid = os.getpid()

        cmd = [
            updater_path,
            "--pid",
            str(pid),
            "--archive",
            str(archive_path),
            "--dest",
            str(app_dir),
            "--exe",
            exe_name,
            # 退出耗时全在 app.exec() 返回之前就付掉了，这里 updater 只需覆盖
            # Python 解释器 teardown（约 1 秒）。60s 是余量而非预算。
            "--timeout",
            "60",
        ]

        # 能力探测：--data-dir / --origin-user-sid 是 3.6.6 引入的新参数，旧
        # updater 的 argparse 见到未知参数会 SystemExit(2)，整次更新失败。
        # 两个参数同传同不传 —— 能力探测是一次判断，不拆成两次。
        updater_version = _read_pe_file_version(Path(updater_path))
        if updater_version is not None and updater_version >= UPDATER_MIN_VERSION_FOR_DATA_DIR:
            from ..utils.paths import user_data_dir
            from ..utils.win_identity import current_user_sid

            cmd += ["--data-dir", str(user_data_dir())]
            origin_sid = current_user_sid()
            if origin_sid:
                cmd += ["--origin-user-sid", origin_sid]
            else:
                # updater 会退回"只看完整性级别"的旧行为（可能在 OTS 提权下漂移）
                log_text(
                    logger,
                    "warning",
                    "[ComponentUpdate] 取不到当前用户 SID，不传 --origin-user-sid",
                )
        else:
            log_text(
                logger,
                "info",
                "[ComponentUpdate] updater.exe 版本 {0} 过旧，不传 --data-dir/--origin-user-sid（updater 将退化为 survival 监护模式）",
                updater_version,
            )

        creationflags = 0
        if sys.platform == "win32":
            # 只用 DETACHED_PROCESS：updater.exe 是 GUI subsystem（scripts/updater.spec
            # 的 console=False），本来就不要控制台，detach 掉是为了不继承父进程的控制台、
            # 不进父进程的 Ctrl+C 进程组。
            #
            # **不要再 `| CREATE_NO_WINDOW`。** 那两个标志在 MSDN 里是互斥的：
            # DETACHED_PROCESS 在场时 CREATE_NO_WINDOW 被忽略，写上去只会误导后来人
            # 把这对组合抄到会 spawn 控制台程序的地方 —— 抄过去就会弹窗，因为
            # detached 的进程没有控制台可继承，它的控制台子进程只能自己 AllocConsole。
            # updater.py 的 HELPER_CREATIONFLAGS 那段注释记录了这个真实 bug。
            creationflags = subprocess.DETACHED_PROCESS

        try:
            # 保持 list 形态。（`updater.py` 里给 cmd.exe 拼命令行时必须用单个
            # 字符串，因为 list2cmdline 的 \" 转义 cmd.exe 不认；这里是直接
            # CreateProcess 一个 exe，list 形态才是正确的引号处理方式。）
            # Environment transport is compatible with older updater argument parsers.
            from PySide6.QtCore import QLocale

            from ..utils.language import normalize_language

            updater_env = os.environ.copy()
            updater_env["FLUENTYTDL_UI_LANGUAGE"] = normalize_language(
                config_manager.get("app_language", "auto"), QLocale.system().name()
            )
            subprocess.Popen(cmd, creationflags=creationflags, env=updater_env)
        except Exception as e:
            # 此刻事件循环已经结束，没有 UI 可以告知，只能留日志
            log_text(logger, "error", "[ComponentUpdate] 启动 updater.exe 失败: {0}", e)
            return

        self._update_state = "LAUNCHED"
        log_text(
            logger,
            "info",
            "[ComponentUpdate] updater.exe 已启动: pid={0}, archive={1}, dest={2}",
            pid,
            archive_path,
            app_dir,
        )

    # ── 版本通道工具 ──────────────────────────────────────

    @staticmethod
    def get_update_channel() -> str:
        """获取当前更新通道。"""
        return _get_update_channel()

    @staticmethod
    def is_beta() -> bool:
        """已弃用，使用 is_locked()。"""
        return ComponentUpdateManager.is_locked()

    @staticmethod
    def is_locked() -> bool:
        """兼容旧调用：所有通道均允许更新。"""
        return False

    def get_manifest_component(self, key: str) -> dict | None:
        """从缓存清单中获取指定组件信息。"""
        if not self._manifest:
            return None
        return self._manifest.get("components", {}).get(key)


# ── 单例 ──
component_update_manager = ComponentUpdateManager()
