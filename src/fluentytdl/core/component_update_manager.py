"""
FluentYTDL 统一组件更新协调器

协调 app-core 和 bin/ 工具的版本检查与更新。
通过 GitHub Release 的 update-manifest.json 统一管理所有组件版本。

版本通道:
  - X.Y.Z (stable): 检查 /releases/latest，接收稳定版自动更新
  - X.Y.Z-rc.N / X.Y.Z-beta.N: 锁定更新，弹窗提示去 GitHub 手动下载
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

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
        logger.debug(f"[ComponentUpdate] 读取 PE 版本资源失败 ({exe_path}): {e}")
        return None


def _get_update_channel() -> str:
    """根据当前版本号确定更新通道。

    正式版（无预发布后缀）支持自动更新；rc / beta 一律 locked，
    提示用户去 GitHub 手动下载。

    同时容忍 3.5.5 之前的 `v-` / `pre-` / `beta-` 前缀格式：升级安装时
    旧 VERSION 文件可能残留，不能因此把老用户判成 locked。
    """
    from fluentytdl import __version__

    ver = str(__version__).strip()

    # 旧格式兼容：v- 视为 stable，pre-/beta- 视为 locked
    if ver.startswith("v-"):
        return "stable"
    if ver.startswith(("pre-", "beta-")):
        return "locked"

    # 新格式：X.Y.Z 为 stable，带 -rc.N / -beta.N 后缀为 locked
    if re.match(r"^v?\d+\.\d+\.\d+$", ver):
        return "stable"
    return "locked"


def _get_proxies() -> dict[str, str]:
    """从 config 构建代理字典。"""
    proxy_mode = str(config_manager.get("proxy_mode") or "off").lower()
    proxy_url = str(config_manager.get("proxy_url") or "")

    if proxy_mode in ("http", "socks5") and proxy_url:
        scheme = "socks5h" if proxy_mode == "socks5" else "http"
        url = proxy_url if "://" in proxy_url else f"{scheme}://{proxy_url}"
        return {"http": url, "https": url}
    return {}


def _get_mirror_url(url: str) -> str:
    """根据配置应用镜像。"""
    source = str(config_manager.get("update_source") or "github").lower()
    if source == "ghproxy" and url.startswith("https://github.com/"):
        mirror = "https://ghfast.top/"
        return mirror + url
    return url


# ─── 清单获取线程 ────────────────────────────────────────


class _ManifestWorker(QThread):
    """后台线程：获取 update-manifest.json

    使用 RAW 直链 (releases/latest/download/) 替代 GitHub API，
    彻底绕过 API 速率限制（无 token 时 60 次/小时）。
    失败时回退到本地缓存清单（7 天有效期）。
    """

    finished = Signal(dict)  # manifest dict
    error = Signal(str)

    def __init__(self, release_tag: str = ""):
        super().__init__()
        self.release_tag = release_tag

    def run(self) -> None:
        try:
            import json

            import requests

            from ..utils.paths import user_data_dir

            proxies = _get_proxies()

            # RAW 直链下载 — 一步到位，无需 API 调用
            manifest_url = _get_mirror_url(MANIFEST_RAW_URL)
            sep = "&" if "?" in manifest_url else "?"
            final_url = f"{manifest_url}{sep}t={int(time.time())}"

            resp = requests.get(final_url, proxies=proxies, timeout=15)
            resp.raise_for_status()
            manifest = resp.json()

            # 本地缓存（离线回退用）
            try:
                cache_path = user_data_dir() / "update_manifest_cache.json"
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

            self.finished.emit(manifest)

        except Exception as e:
            # 回退到本地缓存清单
            try:
                import json

                from ..utils.paths import user_data_dir

                cache_path = user_data_dir() / "update_manifest_cache.json"
                if cache_path.exists():
                    age = time.time() - cache_path.stat().st_mtime
                    if age < 7 * 86400:  # 7 天有效期
                        manifest = json.loads(cache_path.read_text(encoding="utf-8"))
                        logger.info(
                            f"[ComponentUpdate] 网络失败，使用缓存清单（{int(age / 3600)}小时前）"
                        )
                        self.finished.emit(manifest)
                        return
            except Exception:
                pass

            logger.error(f"[ComponentUpdate] 清单获取失败: {e}")
            self.error.emit(str(e))


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
        max_retries = 3
        for attempt in range(max_retries):
            try:
                result = self._download_once()
                if result:
                    self.finished.emit(result)
                    return
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2**attempt  # 1s, 2s
                    logger.warning(
                        f"[ComponentUpdate] 下载失败（尝试 {attempt + 1}/{max_retries}），"
                        f"{wait}s 后重试: {e}"
                    )
                    self.progress.emit(0)
                    time.sleep(wait)
                else:
                    logger.error(f"[ComponentUpdate] 下载失败（已重试 {max_retries} 次）: {e}")
                    self.error.emit(str(e))
                    return

    def _download_once(self) -> str | None:
        """单次下载尝试。成功返回文件路径，失败抛异常。"""
        import hashlib
        import tempfile

        import requests

        final_url = _get_mirror_url(self.url)
        proxies = _get_proxies()

        tmp_dir = Path(tempfile.mkdtemp(prefix="fluentytdl_update_"))
        filename = self.url.rsplit("/", 1)[-1]
        dest = tmp_dir / filename

        resp = requests.get(final_url, proxies=proxies, timeout=600, stream=True)
        resp.raise_for_status()

        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        sha256 = hashlib.sha256()

        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                f.write(chunk)
                sha256.update(chunk)
                downloaded += len(chunk)
                if total > 0:
                    self.progress.emit(int(downloaded / total * 100))

        if self.expected_sha256:
            actual = sha256.hexdigest().lower()
            expected = self.expected_sha256.strip().lower()
            if actual != expected:
                dest.unlink(missing_ok=True)
                raise ValueError(f"SHA256 校验失败\n预期: {expected}\n实际: {actual}")

        self.progress.emit(100)
        return str(dest)


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

    # 下载信号
    download_progress = Signal(int)
    download_finished = Signal(str)  # 本地路径
    download_error = Signal(str)

    # 应用更新信号（Phase 5 状态机）
    apply_requested = Signal()  # 校验通过、可以退出了 → 主窗口执行优雅退出
    apply_error = Signal(str)  # 终止性失败，UI 负责告知用户
    apply_confirm_needed = Signal(int, int)  # (活跃任务数, generation)

    # 通用信号
    check_complete = Signal(list)  # 所有组件检查结果列表

    def __init__(self) -> None:
        super().__init__()
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

    @property
    def manifest(self) -> dict | None:
        return self._manifest

    # ── 清单获取 ──────────────────────────────────────────

    def fetch_manifest(self) -> None:
        """异步获取更新清单。"""
        channel = _get_update_channel()
        if channel == "locked":
            logger.info("[ComponentUpdate] locked 通道，跳过清单获取")
            return

        worker = _ManifestWorker(release_tag="")
        worker.finished.connect(self._on_manifest_fetched)
        worker.error.connect(self._on_manifest_error)
        self._manifest_worker = worker
        worker.start()

    def _on_manifest_fetched(self, manifest: dict) -> None:
        self._manifest = manifest
        logger.info(f"[ComponentUpdate] 清单获取成功: {manifest.get('app_version', '?')}")
        self.manifest_fetched.emit(manifest)

    def _on_manifest_error(self, msg: str) -> None:
        logger.warning(f"[ComponentUpdate] 清单获取失败: {msg}")
        self.manifest_error.emit(msg)

    # ── 统一检查 ──────────────────────────────────────────

    def check_all(self) -> None:
        """检查所有组件更新（app-core + bin/ 工具）。"""
        channel = _get_update_channel()

        if channel == "locked":
            # locked 通道（beta/pre）不检查更新
            return

        # 先获取清单
        self.fetch_manifest()

    def check_app_update(self) -> None:
        """仅检查 app-core 更新。"""
        channel = _get_update_channel()

        if channel == "locked":
            self.app_check_error.emit("locked")
            return

        if self._manifest:
            self._compare_app_version()
        else:
            # 需要先获取清单，使用一次性连接
            self._manifest_app_check_conn = True
            self.manifest_fetched.connect(self._on_manifest_for_app_check)
            self.fetch_manifest()

    def _on_manifest_for_app_check(self, _manifest: dict) -> None:
        """清单获取完成后比对 app 版本（一次性回调）。"""
        try:
            self.manifest_fetched.disconnect(self._on_manifest_for_app_check)
        except RuntimeError:
            pass
        self._compare_app_version()

    def _compare_app_version(self) -> None:
        """比对 app-core 版本（仅 stable 通道）。"""
        if not self._manifest:
            self.app_check_error.emit("清单未获取")
            return

        try:
            from fluentytdl import __version__
        except ImportError:
            self.app_check_error.emit("无法获取当前版本")
            return

        manifest_version = str(self._manifest.get("app_version", "")).strip()
        manifest_tag = self._manifest.get("release_tag", "") or f"v{manifest_version}"

        current = _parse_version(__version__)
        latest = _parse_version(manifest_version)

        if latest <= current:
            self.app_no_update.emit()
            return

        # 检查跳过版本（仅 stable）
        skipped = str(config_manager.get("skipped_stable_version") or "")
        if skipped and _parse_version(skipped) >= latest:
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
                "is_prerelease": False,
            }
        )

    # ── 下载 app-core 更新 ────────────────────────────────

    def download_app_update(self, url: str, sha256: str = "") -> None:
        """下载 app-core 更新归档。"""
        if not url:
            self.download_error.emit("下载 URL 为空")
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
            logger.debug(f"[ComponentUpdate] 忽略重复的更新请求: {archive_path}")
            return
        self._requested_archives.add(archive_path)

        # 已经决定退出了，任何来源都不允许再把流程重新拉起来
        if self._update_state in ("QUITTING", "LAUNCHED"):
            logger.warning(f"[ComponentUpdate] 状态 {self._update_state}，忽略新的更新请求")
            return

        if not archive_path or not Path(archive_path).exists():
            self._fail_pending(f"更新归档不存在: {archive_path}")
            return

        updater_path = self._resolve_updater_path()
        if updater_path is None:
            self._fail_pending(f"updater.exe 不存在: {frozen_app_dir() / 'updater.exe'}")
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
            logger.warning(f"[ComponentUpdate] 无法查询活跃任务，按无任务处理: {e}")

        if active > 0:
            self._update_state = "AWAITING_CONFIRM"
            logger.info(f"[ComponentUpdate] 有 {active} 个活跃任务，等待用户确认 (gen={gen})")
            self.apply_confirm_needed.emit(active, gen)
            return

        self._approve(gen)

    def confirm_pending_update(self, gen: int) -> None:
        """用户确认中断活跃任务并继续更新。"""
        if self._update_state != "AWAITING_CONFIRM":
            logger.warning(f"[ComponentUpdate] 状态 {self._update_state}，忽略确认")
            return
        if gen != self._pending_generation:
            logger.info(
                f"[ComponentUpdate] 忽略过期 generation 的确认: {gen} != {self._pending_generation}"
            )
            return
        self._approve(gen)

    def cancel_pending_update(self, gen: int) -> None:
        """用户取消更新，回到 IDLE。"""
        if self._update_state != "AWAITING_CONFIRM":
            logger.warning(f"[ComponentUpdate] 状态 {self._update_state}，忽略取消")
            return
        if gen != self._pending_generation:
            logger.info(
                f"[ComponentUpdate] 忽略过期 generation 的取消: {gen} != {self._pending_generation}"
            )
            return
        # 状态机已经拦得住 launch_pending_updater()，清空 pending 是双保险
        self._pending_update = None
        self._update_state = "IDLE"
        logger.info(f"[ComponentUpdate] 用户取消了更新 (gen={gen})")

    def _fail_pending(self, msg: str) -> None:
        """终止性校验失败：回到 IDLE 并把消息交给 UI。"""
        logger.error(f"[ComponentUpdate] {msg}")
        self._pending_update = None
        self._update_state = "IDLE"
        self.apply_error.emit(msg)

    def _approve(self, gen: int) -> None:
        """批准更新并请求退出。"""
        self._update_state = "APPROVED"
        logger.info(f"[ComponentUpdate] 更新已批准 (gen={gen})，请求优雅退出")
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
            logger.warning(f"[ComponentUpdate] 状态 {self._update_state}，不启动 updater")
            return

        pending = self._pending_update
        if not pending:
            logger.error("[ComponentUpdate] 状态为 QUITTING 但没有 pending 更新")
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
                logger.warning("[ComponentUpdate] 取不到当前用户 SID，不传 --origin-user-sid")
        else:
            logger.info(
                f"[ComponentUpdate] updater.exe 版本 {updater_version} 过旧，"
                "不传 --data-dir/--origin-user-sid（updater 将退化为 survival 监护模式）"
            )

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW

        try:
            # 保持 list 形态。（`updater.py` 里给 cmd.exe 拼命令行时必须用单个
            # 字符串，因为 list2cmdline 的 \" 转义 cmd.exe 不认；这里是直接
            # CreateProcess 一个 exe，list 形态才是正确的引号处理方式。）
            subprocess.Popen(cmd, creationflags=creationflags)
        except Exception as e:
            # 此刻事件循环已经结束，没有 UI 可以告知，只能留日志
            logger.error(f"[ComponentUpdate] 启动 updater.exe 失败: {e}")
            return

        self._update_state = "LAUNCHED"
        logger.info(
            f"[ComponentUpdate] updater.exe 已启动: pid={pid}, "
            f"archive={archive_path}, dest={app_dir}"
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
        """是否为锁定版本（beta/pre），不支持自动更新。"""
        return _get_update_channel() == "locked"

    def get_manifest_component(self, key: str) -> dict | None:
        """从缓存清单中获取指定组件信息。"""
        if not self._manifest:
            return None
        return self._manifest.get("components", {}).get(key)


# ── 单例 ──
component_update_manager = ComponentUpdateManager()
