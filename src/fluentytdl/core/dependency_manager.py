from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from fluentytdl.utils.localized_log import log_text

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

from ..utils.logger import logger
from ..utils.paths import get_clean_env, is_frozen
from ..utils.ytdlp_runtime import DEFAULT_YTDLP_CHANNEL, SABR_REPOSITORY, YTDLP_CHANNELS
from .config_manager import config_manager

#: component_key → 在 PATH 上查找时试的可执行文件名（不含扩展名，交给 ``shutil.which``）。
#: 只列自带包会用到的名字；PATH 上的同名工具由用户自己装，命名可能不同。
#: 模块级常量而不是类属性：`utils/startup_info.py` 也要按同一套别名找 PATH，
#: 两处各写一份必然漂移。
PATH_EXE_ALIASES: dict[str, tuple[str, ...]] = {
    "yt-dlp": ("yt-dlp",),
    "ffmpeg": ("ffmpeg",),
    "deno": ("deno",),
    "atomicparsley": ("AtomicParsley", "atomicparsley"),
}

#: `download_error` / `check_error` 信号里流通的**稳定错误码**。
#:
#: 信号里绝不能传本地化文案：这些字符串会进日志（`kind=diagnosis` 的 `code`），
#: 而本地化文案随界面语言变化，写进日志就破坏了可搜索性（CLAUDE.md §5）。
#: UI 侧 `ComponentSettingCard._on_error` 负责翻译；翻不动的原样显示。
ERR_URL_UNRESOLVED = "component_update_url_unresolved"
ERR_WORKER_START_FAILED = "component_worker_start_failed"
ERR_WORKER_CRASHED = "component_worker_crashed"
ERR_WORKER_EXIT_NONZERO = "component_worker_exit_nonzero"
ERR_DOWNLOAD_STALLED = "component_download_stalled"

#: GitHub API 响应的进程内缓存有效期。
#:
#: 未认证的 api.github.com 是 60 次/小时。启动检查有 24h 节流不成问题，但用户在
#: 设置页连点「检查更新」能在一分钟内打光配额 —— 之后所有组件一起报「检查失败」。
_API_CACHE_TTL_SEC = 600

#: 下载进程多久没有任何 progress 就判定为挂死。
#:
#: `DownloaderWorker` 没有取消路径，网络半死（TCP 连上但不发数据）时 QProcess
#: 不会退出、`finished` 不会触发，按钮永久停在「正在下载...」。
_DOWNLOAD_STALL_TIMEOUT_MS = 300_000


def _emit(kind: str, /, **fields) -> None:
    """落一条组件更新事件，永不抛异常。

    `stage` 一律 `"startup"`：`STAGES` 是封闭集合且没有 `update` 阶段，新增会牵动
    `tests/test_observability_contract.py`（sha256 校验那条用 `"verify"`，由调用方
    显式传）。只记稳定 code 和裸版本号，**不记本地化文案**（CLAUDE.md §5）。

    函数内 import：`observability` 在 Foundation 层，但这条链路在冷启动早期就会跑，
    模块级 import 会把它拉进 `dependency_manager` 的导入图。
    """
    try:
        from ..observability import emit_event

        fields.setdefault("stage", "startup")
        stage = fields.pop("stage")
        level = fields.pop("level", "INFO")
        emit_event(kind, level=level, stage=stage, fields=fields)
    except Exception:  # noqa: BLE001 - 观测不能反过来弄坏被观测的流程
        pass


def _sanitize_url(url: str | None) -> str:
    try:
        from ..observability import sanitize_url

        return sanitize_url(url)
    except Exception:  # noqa: BLE001
        return ""


def _sha256(value) -> str:
    """Accept a checksum value, never stringify missing or malformed metadata."""
    return (
        value.lower() if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value) else ""
    )


def _asset_sha256(asset: dict) -> str:
    value = asset.get("digest")
    return _sha256(value[7:]) if isinstance(value, str) and value.startswith("sha256:") else ""


@dataclass(frozen=True)
class RemoteVersion:
    """一次远端版本查询的结果。

    `version` 是**裸版本号**，绝不含频道后缀 —— 频道单独放在 `channel` 里。
    以前两者被拼成 `"2026.08.20 (nightly)"` 一个字符串到处传，比较时再用
    `split("(")` 拆回来；清单路径不拼后缀而 API 路径拼，于是跨频道检测在清单
    命中时静默失效。分成两个字段就没有"哪条路径拼了哪条没拼"这个问题。
    """

    version: str = "unknown"
    channel: str = ""  # 仅 yt-dlp 非空
    url: str = ""
    sha256: str = ""
    bundle: dict | None = None
    upstream: str = ""


class ComponentInfo:
    def __init__(self, key: str, name: str, exe_name: str, extra_exes: list[str] | None = None):
        self.key = key  # internal key: 'yt-dlp', 'ffmpeg', 'deno'
        self.name = name  # Display name
        self.exe_name = exe_name  # executable name (e.g., yt-dlp.exe)
        self.extra_exes = (
            extra_exes or []
        )  # Additional executables to update (e.g. ffprobe.exe for ffmpeg)
        self.current_version: str | None = None
        self.current_channel: str = ""
        self.latest_version: str | None = None
        self.latest_channel: str = ""
        self.download_url: str | None = None
        self.expected_sha256: str | None = None
        self.last_result: dict = {}
        self.local_result: dict = {}
        self.last_error: dict = {}
        self.operation_status = ""
        self.progress = 0
        self.local_epoch = 0


class DependencyManager(QObject):
    """
    Manages checking updates and downloading/installing external dependencies.
    """

    # Signals
    check_started = Signal(str)  # component_key
    check_finished = Signal(str, dict)  # component_key, {current, latest, update_available, url}
    check_error = Signal(str, str)  # component_key, error_msg

    download_started = Signal(str)  # component_key
    download_progress = Signal(str, int)  # component_key, percent
    download_status = Signal(str, str)
    download_finished = Signal(str)  # component_key
    download_error = Signal(str, str)  # component_key, error_msg

    install_finished = Signal(str)  # component_key
    check_requested = Signal(str)
    install_requested = Signal(str)
    import_requested = Signal(str, str)
    local_requested = Signal(str)
    local_refreshed = Signal(str, dict)
    error_detail = Signal(str, dict)

    def __init__(self):
        super().__init__()
        self.check_requested.connect(self.check_update)
        self.install_requested.connect(self.install_component)
        self.import_requested.connect(self.import_component)
        self.local_requested.connect(self.refresh_local)
        self._workers = {}
        #: component_key → 刚装上去的 `(版本, 频道)`。用于抑制"装完立刻又说有更新"的
        #: 误报，但**只在版本真的对上时**抑制 —— 见 `_on_check_finished`。
        self._just_installed: dict[str, tuple[str, str]] = {}
        #: 本轮检查是「自动/静默」触发的组件。静默检查不允许弹任何 InfoBar ——
        #: 启动时 5 个组件一起回来，否则主窗口顶部会同时炸出 5 条提示。
        self._silent_checks: set[str] = set()
        #: `install_component()` 因为 URL 缺失而自动补检查过的组件。只补一次，
        #: 否则「检查完还是没 URL」会和自动重试变成死循环。
        self._url_recheck_pending: set[str] = set()
        # A missing shared runtime is installed by the existing Deno component
        # updater, then this one POT operation resumes (at most once).
        self._pot_after_deno: str | None = None
        self._pot_deno_retried = False
        #: url → (取到的时刻, 响应体)。见 `_API_CACHE_TTL_SEC`。
        self._api_cache: dict[str, tuple[float, dict]] = {}

        # Define known components
        self.components = {
            "yt-dlp": ComponentInfo("yt-dlp", "yt-dlp", "yt-dlp.exe"),
            "ffmpeg": ComponentInfo("ffmpeg", "FFmpeg", "ffmpeg.exe", extra_exes=["ffprobe.exe"]),
            "deno": ComponentInfo("deno", "JS Runtime (Deno)", "deno.exe"),
            "pot-provider": ComponentInfo("pot-provider", "POT Provider", "manifest.json"),
            "atomicparsley": ComponentInfo("atomicparsley", "AtomicParsley", "AtomicParsley.exe"),
        }

    def get_target_dir(self, component_key: str) -> Path:
        """
        Get the installation directory for a component.
        Prioritizes exe_dir/bin/{component_key}/ for packaged apps.
        """
        from ..utils.component_paths import component_target

        return component_target(component_key)

    def get_exe_path(self, component_key: str) -> Path:
        return self.get_target_dir(component_key) / self.components[component_key].exe_name

    #: component_key → 在 PATH 上查找时试的可执行文件名。见模块级 ``PATH_EXE_ALIASES``。
    _PATH_ALIASES = PATH_EXE_ALIASES

    def resolve_exe(self, component_key: str) -> tuple[Path | None, str]:
        """解析组件**实际可用**的可执行文件，返回 ``(路径, 来源)``。

        来源为 ``"bundled"`` / ``"path"``；都找不到时返回 ``(None, "")``。

        **为什么不能直接用 `get_exe_path()` 判断"装了没有"**：那个方法回答的是
        "该装到哪里"，只看自带目录。而真正执行工具的 `locate_runtime_tool()`
        是"自带 → PATH → 报错"三段式，yt-dlp 子进程也能用 PATH 上的同名工具。
        两者分叉时 UI 会把一个正在正常工作的组件报成「未安装」，然后让用户去点
        「立即安装」—— 这就是这个方法存在的唯一理由。

        安装动作**不要**用这里的结果做目标路径：装到 PATH 上别人的目录里是越界。
        """
        if component_key == "pot-provider":
            from .pot_component import ComponentError, resolve_bundle

            try:
                return resolve_bundle().root / "manifest.json", "brainicism"
            except ComponentError:
                return None, ""
        if component_key == "deno":
            from ..utils.js_runtime import resolve_deno

            runtime = resolve_deno()
            return runtime.path, runtime.source
        if component_key == "yt-dlp":
            from ..youtube.yt_dlp_cli import resolve_yt_dlp_runtime

            runtime = resolve_yt_dlp_runtime()
            return runtime.path, runtime.source

        info = self.components.get(component_key)
        if info is None:
            return None, ""

        from ..utils.component_paths import managed_candidates

        for bundled in managed_candidates(component_key, info.exe_name):
            if bundled.is_file() and all(
                (bundled.parent / name).is_file() for name in info.extra_exes
            ):
                return bundled, "bundled"

        for name in self._PATH_ALIASES.get(component_key, (Path(info.exe_name).stem,)):
            found = shutil.which(name)
            if not found:
                continue
            # ffmpeg 这类带 extra_exes 的组件，PATH 上必须一整套都在才算可用：
            # 只有 ffmpeg 没有 ffprobe 时报「已安装」会把失败推迟到后处理阶段。
            missing_extra = [
                extra for extra in info.extra_exes if shutil.which(Path(extra).stem) is None
            ]
            if missing_extra:
                log_text(
                    logger,
                    "debug",
                    "{0}: PATH 上找到 {1}，但缺少 {2}，不计为可用",
                    component_key,
                    found,
                    missing_extra,
                )
                continue
            return Path(found), "path"

        return None, ""

    def check_update(self, component_key: str, silent: bool = False, check_session=None):
        """Async check for updates.

        ``silent=True`` 表示这是自动（启动/定时）触发的检查：结果 dict 里会带上
        ``silent`` 标记，UI 侧据此决定「不弹任何提示，只在真有更新时写进消息中心」。
        """
        if component_key not in self.components:
            return

        if silent:
            self._silent_checks.add(component_key)
        else:
            # 用户手动点了检查 —— 即使有一次静默检查正在飞，也按手动对待
            self._silent_checks.discard(component_key)

        existing = self._workers.get(f"check_{component_key}")
        if existing and existing.isRunning():
            return
        worker = UpdateCheckerWorker(component_key, self, check_session)
        worker.setParent(self)
        worker.finished.connect(worker.deleteLater)
        worker.finished_signal.connect(self._on_check_finished)
        worker.local_signal.connect(self._on_local_result)
        worker.error_signal.connect(self._on_check_error)
        self._workers[f"check_{component_key}"] = worker
        self.components[component_key].last_error = {}
        self.check_started.emit(component_key)
        worker.start()

    def refresh_local(self, key: str):
        """Read installed identity without requiring a successful network check."""
        if key not in self.components or self.is_installing(key) or self.is_checking(key):
            return
        previous = self._workers.get(f"local_{key}")
        if previous and previous.isRunning():
            return
        worker = UpdateCheckerWorker(key, self, local_only=True)
        worker.setParent(self)
        worker.local_signal.connect(self._on_local_result)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda k=key, w=worker: self._finish_local(k, w))
        self._workers[f"local_{key}"] = worker
        worker.start()

    def _finish_local(self, key, worker):
        if self._workers.get(f"local_{key}") is worker:
            self._workers.pop(f"local_{key}", None)
        if worker.local_epoch != self.components[key].local_epoch:
            self.refresh_local(key)

    def _on_local_result(self, key, result):
        if result.get("local_epoch") != self.components[key].local_epoch:
            return
        info = self.components[key]
        info.local_result = dict(result)
        # A POT download only stages a candidate. Clear the old update offer
        # when local readback confirms that exact candidate became active.
        expected = (info.last_result.get("bundle") or {}).get("bundle_id")
        if key == "pot-provider" and expected and result.get("current_bundle_id") == expected:
            info.last_result.update(
                current=result["current"],
                current_channel=result.get("current_channel", ""),
                update_available=False,
                decision="up_to_date",
            )
        self.local_refreshed.emit(key, result)

    def _on_check_error(self, key, code):
        if key in self.components:
            self.components[key].last_error = {"code": code, "silent": key in self._silent_checks}
        pending = key in self._url_recheck_pending
        self._url_recheck_pending.discard(key)
        self._silent_checks.discard(key)
        self._workers.pop(f"check_{key}", None)
        if key == "pot-provider":
            self._pot_deno_retried = False
        self.check_error.emit(key, code)
        if pending:
            self.download_error.emit(key, code)
        if key == "deno":
            self._finish_pot_dependency(code)

    def _on_check_finished(self, key, result):
        if key == "yt-dlp" and result.get("requested_channel") not in (
            None,
            str(config_manager.get("ytdlp_channel", DEFAULT_YTDLP_CHANNEL)).strip(),
        ):
            # A channel selection changed while the old check was in flight.
            self._workers.pop(f"check_{key}", None)
            self.check_update(key)
            return
        result["silent"] = key in self._silent_checks
        self._silent_checks.discard(key)

        # 刚装完的组件如果还说有更新，先看是不是**真的没装对**。
        #
        # 以前这里无条件把 `update_available` 抹成 False，代价是：切换 yt-dlp 频道后
        # 装了个不对的包（或者装完 sidecar 没写上），UI 会一口咬定"已是最新"，用户
        # 再点也没用。现在只有版本和频道都对上才算装成功、才抑制。
        installed = self._just_installed.pop(key, None)
        external_active = key == "yt-dlp" and result.get("exe_path") != result.get("install_path")
        if (
            installed is not None
            and result.get("update_available")
            and not external_active
            and key != "pot-provider"
            and result.get("current_channel") != "sabr"
        ):
            want_ver, want_ch = installed
            got_ver = str(result.get("current") or "")
            got_ch = str(result.get("current_channel") or "")
            if got_ver != "unknown" and got_ver == want_ver and got_ch == want_ch:
                logger.info(f"Suppressing update notification for {key} (just installed)")
                result["update_available"] = False
            else:
                _emit(
                    "signal",
                    level="WARNING",
                    code="post_install_version_mismatch",
                    component=key,
                    expected_version=want_ver,
                    expected_channel=want_ch,
                    actual_version=got_ver,
                    actual_channel=got_ch,
                )

        # Store result in our cache
        if key in self.components:
            self.components[key].last_result = dict(result)
            self.components[key].last_error = {}
            self.components[key].current_version = result.get("current")
            self.components[key].current_channel = str(result.get("current_channel") or "")
            self.components[key].latest_version = result.get("latest")
            self.components[key].latest_channel = str(result.get("latest_channel") or "")
            self.components[key].download_url = result.get("url")
            self.components[key].expected_sha256 = result.get("expected_sha256")

        times = dict(config_manager.get("component_check_times") or {})
        times[key] = time.time()
        config_manager.set("component_check_times", times)

        # Clean up worker ref
        self._workers.pop(f"check_{key}", None)
        self.check_finished.emit(key, result)

        # URL 缺失时 `install_component()` 会先补一次检查再回来 —— 现在结果到了。
        if key in self._url_recheck_pending:
            self._url_recheck_pending.discard(key)
            if self.components.get(key) and self.components[key].download_url:
                self.install_component(key)
            else:
                _emit(
                    "diagnosis",
                    level="ERROR",
                    code=ERR_URL_UNRESOLVED,
                    component=key,
                    latest_version=str(result.get("latest") or ""),
                )
                self._on_install_error(key, ERR_URL_UNRESOLVED)

    def is_installing(self, component_key: str) -> bool:
        # Registration precedes the started signal; completion removes ownership
        # before notifying views. This also covers QProcess's Starting state.
        return f"install_{component_key}" in self._workers or (
            component_key == "pot-provider" and self._pot_after_deno is not None
        )

    def is_checking(self, component_key: str) -> bool:
        return f"check_{component_key}" in self._workers

    def install_component(self, component_key: str):
        """Async download and install."""
        if component_key not in self.components:
            return

        if self.is_installing(component_key):
            return
        if self.is_checking(component_key):
            self._url_recheck_pending.add(component_key)
            return
        if component_key == "yt-dlp":
            info = self.components[component_key]
            wanted = str(config_manager.get("ytdlp_channel", DEFAULT_YTDLP_CHANNEL)).strip()
            if info.latest_channel != wanted:
                # Never install a cached official URL after selecting SABR (or vice versa).
                info.download_url = None

        url = self.components[component_key].download_url
        if not url:
            # 没有 URL 有两种可能：还没检查过，或者检查过但没解析出下载地址。
            # 以前这里直接甩一句英文 "Update URL not found. Please check for updates
            # first."，而用户明明刚点过检查 —— 因为清单命中时 URL 恒为空串，检查
            # 多少次都一样。现在先自动补一次检查（结果回到 `_on_check_finished`），
            # 只有补完还是没有才报错。
            if component_key in self._url_recheck_pending:
                return  # 已经在补检查，别叠加
            _emit(
                "signal",
                level="WARNING",
                code="component_update_url_missing",
                component=component_key,
                remedy="recheck",
            )
            self._url_recheck_pending.add(component_key)
            self.check_update(component_key)
            return

        try:
            target_exe = self.get_exe_path(component_key)
        except OSError as error:
            self._on_install_error(component_key, "component_install_unwritable", str(error))
            return

        expected_version = self.components[component_key].latest_version or "unknown"
        expected_sha256 = self.components[component_key].expected_sha256 or ""
        expected_channel = (
            str(config_manager.get("ytdlp_channel", DEFAULT_YTDLP_CHANNEL)).strip()
            if component_key == "yt-dlp"
            else ""
        )

        _emit(
            "stage",
            code="component_install_started",
            component=component_key,
            expected_version=expected_version,
            expected_channel=expected_channel,
            has_sha256=bool(expected_sha256),
            url=_sanitize_url(url),
        )

        worker = DownloaderWorker(
            component_key,
            url,
            target_exe,
            expected_version=expected_version,
            expected_channel=expected_channel,
            expected_sha256=expected_sha256,
            parent=self,
        )
        worker.bundle_id = (self.components[component_key].last_result.get("bundle") or {}).get(
            "bundle_id", ""
        )
        worker.pot_release = self.components[component_key].last_result.get("bundle")
        if component_key == "pot-provider":
            worker.deno_retried = self._pot_deno_retried
            self._pot_deno_retried = False
        self._start_install_worker(component_key, worker)

    def import_component(self, component_key: str, source: str):
        if (
            component_key not in self.components
            or self.is_installing(component_key)
            or self.is_checking(component_key)
        ):
            return
        try:
            target = self.get_exe_path(component_key)
        except OSError as error:
            self._on_install_error(component_key, "component_install_unwritable", str(error))
            return
        worker = DownloaderWorker(component_key, "", target, parent=self)
        worker.local_source = source
        if component_key == "pot-provider":
            worker.deno_retried = self._pot_deno_retried
            self._pot_deno_retried = False
        self._start_install_worker(component_key, worker)

    def _start_install_worker(self, component_key, worker):
        self.components[component_key].last_error = {}
        self.components[component_key].local_epoch += 1
        self.components[component_key].progress = 0
        self.components[component_key].operation_status = (
            "validate" if getattr(worker, "local_source", "") else "downloading"
        )
        worker.progress_signal.connect(self._on_progress)
        worker.status_signal.connect(self._on_install_status)
        worker.finished_signal.connect(self._on_install_finished)
        worker.error_signal.connect(self._on_install_error)
        self._workers[f"install_{component_key}"] = worker
        self.download_started.emit(component_key)
        worker.start()

    def _on_progress(self, key, percent):
        self.components[key].progress = percent
        self.download_progress.emit(key, percent)

    def _on_install_status(self, key, status):
        self.components[key].operation_status = status
        self.download_status.emit(key, status)

    def _on_install_finished(self, key):
        if key == "deno":
            from ..utils.js_runtime import clear_deno_selection

            clear_deno_selection()
        completed = self._workers.get(f"install_{key}")
        info = self.components.get(key)
        self._just_installed[key] = (
            getattr(completed, "expected_version", str(info.latest_version or "") if info else ""),
            getattr(completed, "expected_channel", ""),
        )

        # Installed bytes invalidate shared version identity; path resolution is always fresh.
        # Plugin synchronization still targets the executable selected for execution.
        if key == "yt-dlp":
            try:
                from ..youtube.yt_dlp_cli import (
                    invalidate_yt_dlp_exe_cache,
                    sync_pot_plugins_to_ytdlp,
                )

                invalidate_yt_dlp_exe_cache()
                sync_pot_plugins_to_ytdlp()
            except Exception as e:  # noqa: BLE001 - 安装已经成功，善后失败不该反转结论
                log_text(logger, "warning", "yt-dlp 安装后置处理失败: {0}", e)

        _emit("stage", code="component_install_finished", component=key)
        from ..youtube.pot_manager import pot_manager

        if key not in {"deno", "yt-dlp", "pot-provider"}:
            pot_manager.signals.component_changed.emit(key)
        self.components[key].operation_status = ""
        self.components[key].local_epoch += 1
        worker = self._workers.pop(f"install_{key}", None)
        self.install_finished.emit(key)
        self.refresh_local(key)
        if worker:
            worker.deleteLater()
        if key == "deno":
            self._finish_pot_dependency()

    def _finish_pot_dependency(self, error: str = ""):
        source = self._pot_after_deno
        if source is None:
            return
        self._pot_after_deno = None
        if error:
            self._on_install_error("pot-provider", error)
            return
        self._pot_deno_retried = True
        if source:
            self.import_component("pot-provider", source)
        else:
            self.install_component("pot-provider")

    def _on_install_error(self, key: str, code: str, message: str = ""):
        from ..utils.diagnostic_text import redact_detail

        self._url_recheck_pending.discard(key)
        worker = self._workers.get(f"install_{key}")
        if (
            key == "pot-provider"
            and worker is not None
            and not getattr(worker, "deno_retried", False)
            and code in {"pot_runtime_missing", "pot_runtime_invalid", "pot_runtime_incompatible"}
        ):
            from ..utils.js_runtime import resolve_deno

            # Never override a runtime path explicitly selected by the user.
            if resolve_deno().source != "custom":
                self._pot_after_deno = getattr(worker, "local_source", "")
                self._workers.pop(f"install_{key}", None)
                worker.deleteLater()
                self._on_install_status(key, "preparing")
                self.install_component("deno")
                return
        if key == "pot-provider":
            self._pot_deno_retried = False
        detail = dict(
            getattr(worker, "last_error", {}) or {"code": code, "detail": redact_detail(message)}
        )
        if key in self.components:
            self.components[key].last_error = detail
            self.components[key].operation_status = ""
        self.error_detail.emit(key, detail)
        _emit("diagnosis", level="ERROR", code=code, component=key)
        worker = self._workers.pop(f"install_{key}", None)
        self.download_error.emit(key, code)
        if worker:
            worker.deleteLater()
        if key == "deno":
            self._finish_pot_dependency(code)

    def _fetch_json(self, url: str) -> dict:
        return self._fetch_json_uncached(url)

    def _fetch_json_uncached(self, url: str) -> dict:
        from .update_transport import configured_transport

        return configured_transport().get_json(url)

    def _fetch_text(self, url: str) -> str:
        from .update_transport import configured_transport

        return configured_transport().get_text(url)


class UpdateCheckerWorker(QThread):
    finished_signal = Signal(str, dict)
    local_signal = Signal(str, dict)
    error_signal = Signal(str, str)

    def __init__(
        self, key: str, manager: DependencyManager, check_session=None, *, local_only=False
    ):
        super().__init__()
        self.key = key
        self.manager = manager
        self.local_only = local_only
        self.local_epoch = getattr(getattr(manager, "components", {}).get(key), "local_epoch", 0)
        self.requested_channel = str(
            config_manager.get("ytdlp_channel", DEFAULT_YTDLP_CHANNEL)
        ).strip()
        from .update_transport import configured_check

        self.check_session = check_session or configured_check()

    @staticmethod
    def _parse_version_tuple(ver: str) -> tuple[int, ...] | None:
        """将版本字符串归一化为可比较的整数元组。"""
        cleaned = ver.lstrip("vn").strip()
        m = re.match(r"(\d+(?:\.\d+)*)", cleaned)
        if m:
            return tuple(int(x) for x in m.group(1).split("."))
        return None

    def _compare(
        self, current_ver: str, current_ch: str, remote: RemoteVersion
    ) -> tuple[bool, str]:
        """判断是否有更新，返回 `(update_available, decision_code)`。

        `decision_code` 只用于落日志，是稳定标识符而非文案。
        """
        if not remote.version or remote.version == "unknown":
            return False, "remote_unknown"

        # 频道优先：yt-dlp 本地装的频道和配置要求的频道不一致时，无论版本号大小
        # 一律算「有更新」—— nightly→stable 是往下走的，按版本号比永远判不出来。
        #
        # 判据是「本地频道 vs 配置频道」而不是「本地频道 vs 远端频道」：远端频道本身
        # 就是照配置查的，两者恒等，拿它当判据等于什么都没判。
        if self.key == "yt-dlp":
            wanted_ch = self.requested_channel
            if current_ver != "unknown" and current_ch and wanted_ch and current_ch != wanted_ch:
                return True, "channel_mismatch"

        c_tuple = self._parse_version_tuple(current_ver)
        l_tuple = self._parse_version_tuple(remote.version)

        if c_tuple is not None and l_tuple is not None:
            # 对齐元组长度: (7,1) vs (7,1,3) → (7,1,0) vs (7,1,3)
            max_len = max(len(c_tuple), len(l_tuple))
            c_padded = c_tuple + (0,) * (max_len - len(c_tuple))
            l_padded = l_tuple + (0,) * (max_len - len(l_tuple))
            if l_padded > c_padded:
                return True, "remote_newer"
            if c_padded == l_padded and current_ver != remote.version:
                # 元组相等但字符串不等（例如带 build 后缀），保守认为有更新
                return True, "version_string_differs"
            return False, "up_to_date"

        # 特殊处理 FFmpeg 从 yt-dlp/FFmpeg-Builds 拉取时的日期比较
        # local: "N-125100-g10e9f273ee-20260618"
        # remote: "latest (20260618)"
        if self.key == "ffmpeg" and "latest" in remote.version:
            m_c = re.search(r"-(\d{8})", current_ver)
            m_l = re.search(r"latest \((\d{8})\)", remote.version)
            if m_c and m_l:
                return int(m_l.group(1)) > int(m_c.group(1)), "ffmpeg_date"
            return current_ver != remote.version, "ffmpeg_date_unparsed"

        return current_ver.lstrip("vn") != remote.version.lstrip("vn"), "string_compare"

    def run(self):
        try:
            # 用 resolve_exe() 而不是 get_exe_path()：只在 PATH 上装了 deno/ffmpeg 的
            # 用户，自带目录是空的，但 yt-dlp 子进程一直在正常使用它们。按自带目录
            # 判断会把这些组件报成「未安装」并让用户去点「立即安装」。
            resolved, source = self.manager.resolve_exe(self.key)
            exe_path = resolved if resolved is not None else self.manager.get_exe_path(self.key)
            current_ver, current_ch = self._get_local_version(self.key, exe_path)

            local = {
                "current": current_ver,
                "current_channel": current_ch,
                "source": source,
                "exe_path": str(resolved) if resolved else "",
                "install_path": str(self.manager.get_exe_path(self.key)),
                "version_status": getattr(self, "_local_version_status", ""),
                "local_epoch": self.local_epoch,
            }
            if self.key == "pot-provider" and resolved:
                from .pot_component import resolve_bundle

                local["current_bundle_id"] = resolve_bundle().manifest.get("bundle_id", "")
            self.local_signal.emit(self.key, local)
            if self.local_only:
                return

            remote = self._get_remote_version(self.key)

            update_available, decision = self._compare(current_ver, current_ch, remote)
            if self.key == "pot-provider":
                from .pot_component import ComponentError, bundle_order, resolve_bundle

                try:
                    current_bundle = resolve_bundle()
                except ComponentError:
                    current_bundle = None
                update_available = bool(
                    remote.bundle
                    and (
                        current_bundle is None
                        or bundle_order(remote.bundle) > bundle_order(current_bundle.manifest)
                    )
                )
                decision = (
                    "original_bundle_differs"
                    if update_available
                    else "upstream_pending"
                    if remote.upstream != remote.version
                    else "up_to_date"
                )
            if self.key == "yt-dlp" and current_ch == remote.channel == "sabr":
                # Date-only versions cannot distinguish same-day upstream revisions/rebuilds.
                with exe_path.open("rb") as stream:
                    current_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                update_available = current_hash != remote.sha256
                decision = "sabr_revision_differs" if update_available else "up_to_date"

            _emit(
                "decision",
                code="component_update_decision",
                component=self.key,
                reason=decision,
                update_available=update_available,
                current_version=current_ver,
                current_channel=current_ch,
                latest_version=remote.version,
                latest_channel=remote.channel,
                has_url=bool(remote.url),
                exe_source=source,
            )

            result = {
                # `current` / `latest` 是**裸版本号**：拼展示字符串是 UI 的事，
                # core 不再造 `"2026.08.20 (nightly)"` 这种复合串。
                "current": current_ver,
                "current_channel": current_ch,
                "version_status": getattr(self, "_local_version_status", ""),
                "latest": remote.version,
                "latest_channel": remote.channel,
                "update_available": update_available,
                "url": remote.url,
                "expected_sha256": remote.sha256,
                "requested_channel": self.requested_channel if self.key == "yt-dlp" else None,
                # "bundled" / "path" / ""：UI 靠它区分「未安装」和「用的是系统里那份」
                "source": source,
                "exe_path": str(exe_path),
                "install_path": str(self.manager.get_exe_path(self.key)),
                "bundle": remote.bundle,
                "upstream_latest": remote.upstream,
                "decision": decision,
            }
            self.finished_signal.emit(self.key, result)

        except Exception as e:
            if self.local_only:
                from ..utils.diagnostic_text import redact_detail

                self.local_signal.emit(
                    self.key,
                    {
                        "current": "unknown",
                        "source": "",
                        "local_epoch": self.local_epoch,
                        "local_error": redact_detail(str(e)),
                    },
                )
                return
            logger.error(f"Update check failed for {self.key}: {e}")
            _emit(
                "diagnosis",
                level="ERROR",
                code="component_check_failed",
                component=self.key,
                error_type=type(e).__name__,
            )
            self.error_signal.emit(self.key, str(e))

    def _get_local_version(self, key: str, path: Path) -> tuple[str, str]:
        """读本地已装版本，返回 `(裸版本号, 频道)`。频道只有 yt-dlp 非空。"""
        if key == "pot-provider":
            from .pot_component import ComponentError, resolve_bundle

            try:
                return resolve_bundle().version, "brainicism"
            except ComponentError:
                return "unknown", ""
        if key == "yt-dlp":
            from ..utils.ytdlp_runtime import probe_version

            result = probe_version(path)
            self._local_version_status = result.status
            return result.version, result.channel

        if not path.exists():
            return "unknown", ""

        try:
            # Run --version
            cmd = [str(path), "--version"]
            # Deno uses 'deno --version'
            # FFmpeg uses 'ffmpeg -version' (single dash often works too)
            if key == "ffmpeg":
                cmd = [str(path), "-version"]

            # Windows hide console
            kwargs = {}
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            env = get_clean_env()
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                env=env,
                timeout=5,
                **kwargs,
            )
            if proc.returncode != 0:
                return "unknown", ""

            out = proc.stdout.strip()
            if key == "deno":
                # deno 1.38.0 (release, x86_64-pc-windows-msvc) ...
                m = re.search(r"deno (\d+\.\d+\.\d+)", out)
                if m:
                    return m.group(1), ""
            elif key == "ffmpeg":
                # ffmpeg version 6.1-essentials_build-www.gyan.dev ...
                # or ffmpeg version n7.1.3-40-gcddd06f3b9-20260219 ...
                line = out.splitlines()[0]
                m = re.search(r"ffmpeg version ([^\s]+)", line)
                if m:
                    raw = m.group(1)
                    # 从完整版本字符串中仅提取核心数字版本
                    # 示例: "n7.1.3-40-gcddd06f3b9-20260219" → "7.1.3"
                    # 示例: "6.1-essentials_build-www.gyan.dev" → "6.1"
                    core = raw.lstrip("nN")
                    vm = re.match(r"(\d+(?:\.\d+)*)", core)
                    if vm:
                        return vm.group(1), ""
                    return raw, ""  # fallback
            elif key == "atomicparsley":
                # AtomicParsley outputs: "AtomicParsley version: 20240608.083822.0 1ed9031..."
                # 只取日期+时间部分 (YYYYMMDD.HHMMSS)，忽略后面的 build/commit 信息
                m = re.search(r"(\d{8}\.\d{6})", out)
                if m:
                    return m.group(1), ""
            elif key == "aria2c":
                # aria2 version 1.36.0
                m = re.search(r"aria2 version (\d+\.\d+\.\d+)", out)
                if m:
                    return m.group(1), ""

            return "installed", ""  # Fallback if parsing fails
        except Exception:
            return "unknown", ""

    def _overlay_manifest(
        self, key: str, remote: RemoteVersion, *, manifest: dict | None = None
    ) -> RemoteVersion:
        """清单里恰好有这个版本的制品信息时，用清单的 url/sha256 替换 API 解析结果。

        **版本判定权单向归 API** —— 清单永远不参与「最新是什么」的决定。清单里的
        版本号是构建机上那一刻的快照（`generate_manifest.py` 探测的是本地
        `assets/bin/` 里已装的版本），拿它当"最新"会把用户钉在发版时的旧版本上。

        以前的顺序是反的：清单命中就直接 return，连 `url` 是空串都照样 return。
        而 `generate_manifest.py` 给每个 `bin/*` 写死 `"url": ""`，于是下面这段能用的
        API 分支被完全遮住，`download_url` 恒为空 —— 这就是 deno 那句
        "Update URL not found" 的根。
        """
        if not remote.version or remote.version == "unknown":
            return remote
        try:
            from .component_update_manager import component_update_manager

            comp = (
                manifest.get("components", {}).get(f"bin/{key}")
                if manifest is not None
                else component_update_manager.get_manifest_component(f"bin/{key}")
            )
            if not isinstance(comp, dict):
                return remote

            m_version = str(comp.get("version", "") or "").strip()
            m_url = str(comp.get("url", "") or "").strip()
            if key == "atomicparsley":
                # Its executable reports a trailing build suffix (e.g. .0),
                # whereas update checks display only the date and time. The
                # exact release asset URL must still match below.
                match = re.fullmatch(r"(\d{8}\.\d{6})(?:\.[0-9a-fA-F]+)?", m_version)
                if match:
                    m_version = match[1]
            if m_version != remote.version:
                _emit(
                    "decision",
                    code="component_artifact_source",
                    component=key,
                    source="api",
                    reason="manifest_version_differs",
                    latest_version=remote.version,
                    manifest_version=m_version,
                )
                return remote
            if not m_url or m_url != remote.url:
                # 清单条目版本对得上但没带下载地址 —— 构建侧还没写 asset_url，
                # 或者这个组件（ffmpeg）本来就只能走 API。不是错误，但值得记一条。
                _emit(
                    "signal",
                    code="manifest_artifact_url_missing",
                    component=key,
                    latest_version=remote.version,
                )
                return remote

            _emit(
                "decision",
                code="component_artifact_source",
                component=key,
                source="manifest",
                latest_version=remote.version,
                url=_sanitize_url(m_url),
            )
            return RemoteVersion(
                version=remote.version,
                channel=remote.channel,
                url=m_url,
                sha256=_sha256(comp.get("sha256")) or remote.sha256,
            )
        except Exception:  # noqa: BLE001 - 清单读不到就用 API 的结果，不该让检查失败
            return remote

    def _get_remote_version(self, key: str) -> RemoteVersion:
        """查上游最新版本，再看清单能不能提供该版本的制品元数据。"""
        remote = self._fetch_remote_from_api(key)
        if remote.channel == "sabr" or key == "pot-provider":
            return remote
        remote = self._overlay_manifest(key, remote)
        if not _sha256(remote.sha256):
            # Older upstream assets (notably AtomicParsley) have no GitHub
            # digest. A standalone component check must fetch its own checksum
            # metadata instead of relying on a prior app-update UI action.
            channel = "pre" if config_manager.get("app_update_channel") == "pre" else "stable"
            manifest = self.check_session.manifest(channel)
            remote = self._overlay_manifest(key, remote, manifest=manifest)
        if not _sha256(remote.sha256):
            from .update_transport import UpdateError

            raise UpdateError("update_invalid_response")
        return remote

    def _fetch_remote_from_api(self, key: str) -> RemoteVersion:
        from .update_transport import UpdateError

        channel_label = self.requested_channel if key == "yt-dlp" else ""
        if channel_label not in ("", *YTDLP_CHANNELS):
            channel_label = DEFAULT_YTDLP_CHANNEL
        data = self.check_session.release(key, channel_label or "stable")
        if key == "pot-provider":
            return RemoteVersion(
                version=data["upstream_version"],
                channel="brainicism",
                url=data["asset_url"],
                sha256=data["sha256"],
                bundle=data,
                upstream=data["upstream_version"],
            )

        if key == "yt-dlp" and channel_label == "sabr":
            from .update_transport import UpdateError

            assets = {a["name"]: a for a in data.get("assets", [])}
            try:
                info = self.check_session.transport.get_json(
                    assets["BUILD_INFO.json"]["browser_download_url"]
                )
                digest = info["exe_sha256"]
                asset_digest = assets["yt-dlp.exe"].get("digest")
                if (
                    info.get("release_repository") != SABR_REPOSITORY
                    or info.get("tag") != data["tag_name"]
                    or info.get("architecture") != "Windows x64"
                    or info.get("variant") != "sabr-test"
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not re.fullmatch(r"[0-9a-f]{40}", info.get("source_sha", ""))
                    or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}(?:\.\d+)?", info.get("version", ""))
                    or (asset_digest and asset_digest != f"sha256:{digest}")
                ):
                    raise ValueError("SABR release identity mismatch")
                return RemoteVersion(
                    info["version"], "sabr", assets["yt-dlp.exe"]["browser_download_url"], digest
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise UpdateError("update_invalid_response") from exc

        if key == "yt-dlp":
            tag = data.get("tag_name", "unknown")

            # Find exe asset and checksums
            dl_url = ""
            checksum_url = ""
            for asset in data.get("assets", []):
                if asset["name"] == "yt-dlp.exe":
                    dl_url = asset["browser_download_url"]
                elif asset["name"] == "SHA2-256SUMS":
                    checksum_url = asset["browser_download_url"]

            expected_sha256 = ""
            asset_digest = next(
                (_asset_sha256(a) for a in data.get("assets", []) if a.get("name") == "yt-dlp.exe"),
                "",
            )
            if asset_digest:
                expected_sha256 = asset_digest
            if checksum_url:
                try:
                    sums = self.manager._fetch_text(checksum_url)
                    for line in sums.splitlines():
                        parts = line.split()
                        if (
                            len(parts) == 2
                            and parts[1].lstrip("*") == "yt-dlp.exe"
                            and re.fullmatch(r"[a-f0-9]{64}", parts[0])
                        ):
                            if expected_sha256 and expected_sha256 != parts[0]:
                                from .update_transport import UpdateError

                                raise UpdateError("update_hash")
                            expected_sha256 = parts[0]
                            break
                except UpdateError:
                    raise
                except Exception as e:
                    logger.warning(f"Failed to fetch checksums for yt-dlp: {e}")

            return RemoteVersion(
                version=tag, channel=channel_label, url=dl_url, sha256=expected_sha256
            )

        elif key == "deno":
            tag = data.get("tag_name", "vunknown").lstrip("v")

            # Find windows zip and checksum
            dl_url = ""
            checksum_url = ""
            for asset in data.get("assets", []):
                name = asset["name"]
                if name == "deno-x86_64-pc-windows-msvc.zip":
                    dl_url = asset["browser_download_url"]
                elif name == "deno-x86_64-pc-windows-msvc.zip.sha256sum":
                    checksum_url = asset["browser_download_url"]

            expected_sha256 = ""
            if checksum_url:
                try:
                    sums = self.manager._fetch_text(checksum_url)
                    for line in sums.splitlines():
                        if line.strip().lower().startswith("hash"):
                            parts = line.split(":")
                            if len(parts) >= 2:
                                expected_sha256 = parts[1].strip()
                            break
                    if not expected_sha256:
                        # Fallback in case format changes back to standard
                        expected_sha256 = sums.split()[0].strip()
                except Exception as e:
                    logger.warning(f"Failed to fetch checksums for deno: {e}")

            return RemoteVersion(
                version=tag,
                url=dl_url,
                sha256=_sha256(expected_sha256)
                or next(
                    (
                        _asset_sha256(a)
                        for a in data.get("assets", [])
                        if a.get("browser_download_url") == dl_url
                    ),
                    "",
                ),
            )

        elif key == "ffmpeg":
            tag = data.get("tag_name", "unknown")
            release_name = data.get("name", "")

            # 尝试从 "Latest Auto-Build (2026-06-18 17:15)" 中提取日期以对齐本地的 N-125100...-20260618
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", release_name)
            if m:
                tag = f"latest ({m.group(1)}{m.group(2)}{m.group(3)})"

            dl_url = ""
            checksum_url = ""
            zip_filename = ""
            for asset in data.get("assets", []):
                name = asset["name"]
                if "win64-gpl" in name and ".zip" in name and "shared" not in name:
                    dl_url = asset["browser_download_url"]
                    zip_filename = name
                elif name == "checksums.sha256":
                    checksum_url = asset["browser_download_url"]

            expected_sha256 = ""
            if checksum_url and zip_filename:
                try:
                    sums = self.manager._fetch_text(checksum_url)
                    for line in sums.splitlines():
                        if zip_filename in line:
                            expected_sha256 = line.split()[0].strip()
                            break
                except Exception as e:
                    logger.warning(f"Failed to fetch checksums for ffmpeg: {e}")

            return RemoteVersion(
                version=tag,
                url=dl_url,
                sha256=_sha256(expected_sha256)
                or next(
                    (
                        _asset_sha256(a)
                        for a in data.get("assets", [])
                        if a.get("browser_download_url") == dl_url
                    ),
                    "",
                ),
            )

        elif key == "atomicparsley":
            # wez/atomicparsley from GitHub
            tag = data.get("tag_name", "unknown")

            # 只取日期+时间部分 (YYYYMMDD.HHMMSS)，与本地版本格式一致
            # 否则 "20240608.083822.1ed9031" 会被 _parse_version_tuple 误解析为
            # (20240608, 83822, 1)，而本地输出 "20240608.083822.0" 解析为
            # (20240608, 83822, 0)，导致同版本也被判定为需要更新
            m = re.match(r"(\d{8}\.\d{6})", tag)
            if m:
                tag = m.group(1)

            # Find Windows zip asset
            dl_url = ""
            for asset in data.get("assets", []):
                name = asset["name"].lower()
                # AtomicParsleyWindows.zip
                if "windows" in name and name.endswith(".zip"):
                    dl_url = asset["browser_download_url"]
                    break
            return RemoteVersion(
                version=tag,
                url=dl_url,
                sha256=next(
                    (
                        _asset_sha256(a)
                        for a in data.get("assets", [])
                        if a.get("browser_download_url") == dl_url
                    ),
                    "",
                ),
            )

        return RemoteVersion()


class DownloaderWorker(QObject):
    progress_signal = Signal(str, int)
    status_signal = Signal(str, str)
    finished_signal = Signal(str)
    error_signal = Signal(str, str)
    activation_granted = Signal()

    def __init__(
        self,
        key: str,
        url: str,
        target_exe: Path,
        expected_version: str = "",
        expected_channel: str = "",
        expected_sha256: str = "",
        parent=None,
    ):
        super().__init__()
        if parent:
            self.setParent(parent)
        self.key = key
        self.url = url
        self.target_exe = target_exe
        self.expected_version = expected_version
        self.expected_channel = expected_channel
        self.expected_sha256 = expected_sha256
        self.bundle_id = ""
        self.pot_release = None
        self.local_source = ""
        self.last_error = {}
        self._done_received = False
        self._running = False
        self._gate_done = threading.Event()
        self._cancel = threading.Event()
        self.activation_granted.connect(self._grant_activation)
        self._activation_waiting = False
        self._owned_process = None
        self.extra_exes: list[str] = []

        if dependency_manager.components.get(key):
            self.extra_exes = dependency_manager.components[key].extra_exes

        from PySide6.QtCore import QProcess

        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._on_ready_read)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)
        self.process.started.connect(self._write_config)
        from PySide6.QtCore import QCoreApplication

        if QCoreApplication.instance():
            QCoreApplication.instance().aboutToQuit.connect(self._cancel_worker)

        self._buffer = ""
        self._is_finished_emitted = False
        self._error_emitted = False

        # 挂死看门狗：网络半死（TCP 连上但不发数据）时 QProcess 不会退出、
        # `finished` 不会触发，按钮就永久停在「正在下载...」。每来一个 progress
        # 重新计时；超时就杀进程，`_on_finished` 会把它变成一条错误。
        self._stall_timer = QTimer(self)
        self._stall_timer.setSingleShot(True)
        self._stall_timer.setInterval(_DOWNLOAD_STALL_TIMEOUT_MS)
        self._stall_timer.timeout.connect(self._on_stalled)

    def _on_stalled(self):
        log_text(logger, "warning", "组件下载无响应超时，终止 worker: {0}", self.key)
        self.last_error = {"code": ERR_DOWNLOAD_STALLED, "detail": ""}
        try:
            self.process.kill()
        except Exception:  # noqa: BLE001
            pass

    def start(self):
        self._running = True
        self._launch()

    def isRunning(self):
        return self._running and not self._error_emitted and not self._is_finished_emitted

    def _guarded_activate(self):
        from ..youtube.pot_manager import pot_manager
        from .runtime_activity import changing_components

        done = self._gate_done
        try:
            with changing_components(self._cancel):
                if self._cancel.is_set() or done.is_set():
                    return
                pot_manager._stop_owned()
                self.activation_granted.emit()
                done.wait()
                from .updater_worker import recover_install

                # A terminated installer cannot run its own rollback. Recover
                # before releasing queued CLI requests or restarting POT.
                root = self.target_exe.parent
                recover_install(root.parent if self.key == "pot-provider" else root)
        except InterruptedError:
            return
        pot_manager._invalidate()

    def _grant_activation(self):
        if self._cancel.is_set() or self._gate_done.is_set():
            return
        self.process.write(b'{"activate":true}\n')
        self.process.closeWriteChannel()
        self._stall_timer.start()
        self.status_signal.emit(self.key, "installing")

    def _cancel_worker(self):
        from PySide6.QtCore import QProcess

        from ..utils.owned_process import close_job

        self._cancel.set()
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._gate_done.set()
        self._running = False
        self._stall_timer.stop()
        close_job(self._owned_process)
        self.process.kill()

    def _launch(self):
        from .config_manager import config_manager

        if self._cancel.is_set():
            return

        proxy_url = config_manager.get("proxy_url")
        proxy_mode = config_manager.get("proxy_mode")

        config = {
            "key": self.key,
            "url": self.url,
            "target_exe": str(self.target_exe),
            "expected_version": self.expected_version,
            "expected_channel": self.expected_channel,
            "expected_sha256": self.expected_sha256,
            "extra_exes": self.extra_exes,
            "proxy_url": proxy_url,
            "proxy_mode": proxy_mode,
            "bundle_id": self.bundle_id,
            "pot_release": self.pot_release,
            "local_source": self.local_source,
            "activation_gate": self.key in {"deno", "yt-dlp", "pot-provider"},
        }
        if self.key == "pot-provider":
            from ..utils.js_runtime import resolve_deno

            runtime = resolve_deno()
            config["deno_path"] = str(runtime.path) if runtime.path else ""
            from ..youtube.yt_dlp_cli import resolve_yt_dlp_exe

            exe = resolve_yt_dlp_exe()
            config["ytdlp_path"] = str(exe) if exe else ""
        self._config_data = (json.dumps(config) + "\n").encode("utf-8")

        args = []
        if not is_frozen():
            exe = sys.executable
            main_py = str(Path(__file__).resolve().parents[3] / "main.py")
            args = [main_py, "--update-worker"]
        else:
            exe = sys.executable
            args = ["--update-worker"]

        self.process.start(exe, args)

    def _write_config(self):
        from types import SimpleNamespace

        from ..utils.owned_process import attach_job

        # Assign ownership before sending work: dependency probes can spawn their
        # own Deno/CLI children, which must also exit if the updater is cancelled.
        self._owned_process = SimpleNamespace(pid=int(self.process.processId()))
        attach_job(self._owned_process)
        self.process.write(self._config_data)
        if self.key not in {"deno", "yt-dlp", "pot-provider"}:
            self.process.closeWriteChannel()
        self._stall_timer.start()

    def _emit_error(self, code: str):
        """`code` 是**稳定错误码**，不是给人看的文案 —— 翻译在 UI 侧。"""
        self._stall_timer.stop()
        if not self._error_emitted:
            self._error_emitted = True
            self._running = False
            self._gate_done.set()
            self.error_signal.emit(self.key, code)

    def _on_ready_read(self):
        data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self._buffer += data
        lines = self._buffer.split("\n")
        self._buffer = lines[-1]

        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg_type = msg.get("type")
                if msg_type == "progress":
                    self._stall_timer.start()  # 有进展，重新计时
                    self.progress_signal.emit(self.key, msg.get("percent", 0))
                elif msg_type == "error":
                    from ..utils.diagnostic_text import redact_detail

                    self.last_error = {
                        "code": msg.get("code") or msg.get("msg", "component_worker_error"),
                        "detail": redact_detail(msg.get("detail", "")),
                    }
                elif msg_type == "status":
                    self._stall_timer.start()
                    self.status_signal.emit(self.key, msg.get("msg", "validate"))
                elif msg_type == "activation_request" and not self._activation_waiting:
                    self._activation_waiting = True
                    self._stall_timer.stop()
                    self.status_signal.emit(self.key, "waiting")
                    threading.Thread(
                        target=self._guarded_activate, daemon=True, name=f"install-{self.key}"
                    ).start()
                elif msg_type == "done":
                    # Let _on_finished handle the signal to ensure process has fully exited
                    self._done_received = True
            except json.JSONDecodeError:
                from ..utils.diagnostic_text import redact_detail
                from ..utils.logger import logger

                logger.debug("Worker stdout: {}", redact_detail(line))

    def _on_finished(self, exitCode, exitStatus):
        from PySide6.QtCore import QProcess

        from ..utils.diagnostic_text import redact_detail
        from ..utils.owned_process import close_job

        self._stall_timer.stop()
        self._on_ready_read()
        self._running = False
        close_job(self._owned_process)
        self._gate_done.set()
        if self.last_error:
            self._emit_error(self.last_error["code"])
            return

        if exitStatus == QProcess.ExitStatus.CrashExit:
            # 看门狗 kill 掉的进程也走这里，但那时 `_error_emitted` 已经是 True，
            # `_emit_error` 会自动让位给更具体的 ERR_DOWNLOAD_STALLED。
            #
            # 崩溃退出码 + stderr 尾巴是定位原生崩溃的唯一线索（例如 psutil 句柄枚举
            # 触发的 0xC0000005）。CrashExit 时 exitCode 往往就是那个 NTSTATUS，务必
            # 记下来 —— 否则日志里只剩一句无信息量的 "Crashed"，真凶查不出来。
            err = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
            log_text(
                logger,
                "error",
                "更新 worker 崩溃退出 code={0} hex={1}: {2}",
                exitCode,
                hex(exitCode & 0xFFFFFFFF),
                redact_detail(err)[:500],
            )
            self._emit_error(ERR_WORKER_CRASHED)
        elif exitCode != 0:
            err = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
            log_text(
                logger, "error", "更新 worker 退出码 {0}: {1}", exitCode, redact_detail(err)[:500]
            )
            self._emit_error(ERR_WORKER_EXIT_NONZERO)
        elif not self._done_received:
            self._emit_error("component_worker_incomplete")
        else:
            if not self._is_finished_emitted and not self._error_emitted:
                self._is_finished_emitted = True
                self.finished_signal.emit(self.key)

    def _on_error(self, error):
        from PySide6.QtCore import QProcess

        # **只有 FailedToStart 才是"没跑起来"**：此时 finished 信号不会再来，必须在这
        # 里定论。其余情形（尤其 Crashed）finished 一定会跟着触发，且那里能拿到退出码
        # 和 stderr —— 让位给它，别在这里用笼统的 START_FAILED 把真正的错误码盖掉。
        # （这正是 psutil 原生崩溃长期被误报成 component_worker_start_failed 的原因：
        #  errorOccurred(Crashed) 先到，抢先 emit 了 START_FAILED 并置 _error_emitted，
        #  把随后 CrashExit 里更具体的信息全挡在门外。）
        if error == QProcess.ProcessError.FailedToStart:
            log_text(logger, "error", "更新 worker 无法启动: {0}", self.process.errorString())
            self._emit_error(ERR_WORKER_START_FAILED)
        else:
            log_text(logger, "warning", "更新 worker 进程错误(交由 finished 定论): {0}", error)


# Global instance
dependency_manager = DependencyManager()
