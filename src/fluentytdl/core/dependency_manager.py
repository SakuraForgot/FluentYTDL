from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    psutil = None
    HAS_PSUTIL = False

from ..utils.logger import logger
from ..utils.paths import frozen_app_dir, is_frozen
from .config_manager import config_manager


class ComponentInfo:
    def __init__(self, key: str, name: str, exe_name: str, extra_exes: list[str] | None = None):
        self.key = key  # internal key: 'yt-dlp', 'ffmpeg', 'deno'
        self.name = name  # Display name
        self.exe_name = exe_name  # executable name (e.g., yt-dlp.exe)
        self.extra_exes = (
            extra_exes or []
        )  # Additional executables to update (e.g. ffprobe.exe for ffmpeg)
        self.current_version: str | None = None
        self.latest_version: str | None = None
        self.download_url: str | None = None


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
    download_finished = Signal(str)  # component_key
    download_error = Signal(str, str)  # component_key, error_msg

    install_finished = Signal(str)  # component_key

    def __init__(self):
        super().__init__()
        self._workers = {}
        self._just_installed: set[str] = set()  # 记录刚安装完的组件，用于抑制误报

        # Define known components
        self.components = {
            "yt-dlp": ComponentInfo("yt-dlp", "yt-dlp", "yt-dlp.exe"),
            "ffmpeg": ComponentInfo("ffmpeg", "FFmpeg", "ffmpeg.exe", extra_exes=["ffprobe.exe"]),
            "deno": ComponentInfo("deno", "JS Runtime (Deno)", "deno.exe"),
            "pot-provider": ComponentInfo(
                "pot-provider", "POT Provider", "bgutil-pot-provider.exe"
            ),
            "ytarchive": ComponentInfo("ytarchive", "ytarchive", "ytarchive.exe"),
            "atomicparsley": ComponentInfo("atomicparsley", "AtomicParsley", "AtomicParsley.exe"),
        }

    def get_target_dir(self, component_key: str) -> Path:
        """
        Get the installation directory for a component.
        Prioritizes exe_dir/bin/{component_key}/ for packaged apps.
        """
        # Default to 'bin' next to the executable (standard for our packaged app)
        # Fallback to project root assets/bin for dev
        if is_frozen():
            base = frozen_app_dir() / "bin"
        else:
            # Dev mode: src/fluentytdl/assets/bin or project_root/assets/bin
            # Let's use project_root/assets/bin for consistency
            base = Path(__file__).parents[3] / "assets" / "bin"

        target = base / component_key
        if not target.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Failed to create target dir {target}: {e}")
        return target

    def get_exe_path(self, component_key: str) -> Path:
        return self.get_target_dir(component_key) / self.components[component_key].exe_name

    def get_mirror_url(self, original_url: str) -> str:
        """Apply the configured mirror source."""
        source = config_manager.get("update_source") or "github"

        if source == "github":
            return original_url
        elif source == "ghproxy":
            # Typical ghproxy usage: https://ghproxy.com/https://github.com/...
            # Note: domain might vary, strictly example
            return f"https://mirror.ghproxy.com/{original_url}"
        # Add more mirrors as needed
        return original_url

    def check_update(self, component_key: str):
        """Async check for updates."""
        if component_key not in self.components:
            return

        worker = UpdateCheckerWorker(component_key, self)
        worker.finished_signal.connect(self._on_check_finished)
        worker.error_signal.connect(self.check_error)
        worker.start()
        self._workers[f"check_{component_key}"] = worker
        self.check_started.emit(component_key)

    def _on_check_finished(self, key, result):
        # 如果组件刚刚安装完，且版本比较仍显示有更新，抑制误报
        if key in self._just_installed:
            self._just_installed.discard(key)
            if result.get("update_available") and result.get("current") != "unknown":
                logger.info(f"Suppressing update notification for {key} (just installed)")
                result["update_available"] = False

        # Store result in our cache
        if key in self.components:
            self.components[key].current_version = result.get("current")
            self.components[key].latest_version = result.get("latest")
            self.components[key].download_url = result.get("url")

        self.check_finished.emit(key, result)
        # Clean up worker ref
        self._workers.pop(f"check_{key}", None)

    def install_component(self, component_key: str):
        """Async download and install."""
        if component_key not in self.components:
            return

        url = self.components[component_key].download_url
        if not url:
            # If checking hasn't run or failed, try to resolve url dynamically if possible,
            # but usually we expect check_update to run first.
            # For now, trigger an error if no URL known.
            self.download_error.emit(
                component_key, "Update URL not found. Please check for updates first."
            )
            return

        # Apply mirror
        final_url = self.get_mirror_url(url)
        target_exe = self.get_exe_path(component_key)

        expected_version = self.components[component_key].latest_version or "unknown"
        expected_channel = (
            str(config_manager.get("ytdlp_channel", "stable")).strip()
            if component_key == "yt-dlp"
            else ""
        )

        worker = DownloaderWorker(
            component_key,
            final_url,
            target_exe,
            expected_version=expected_version,
            expected_channel=expected_channel,
            parent=self,
        )
        worker.progress_signal.connect(self.download_progress)
        worker.finished_signal.connect(self._on_install_finished)
        worker.error_signal.connect(self.download_error)
        worker.start()
        self._workers[f"install_{component_key}"] = worker
        self.download_started.emit(component_key)

    def _on_install_finished(self, key):
        self._just_installed.add(key)
        self.install_finished.emit(key)
        worker = self._workers.pop(f"install_{key}", None)
        if worker:
            worker.deleteLater()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        """
        Builds a urllib OpenerDirector with proxy settings applied from the application config.
        Also explicitly builds an SSL context that tries to use default verification,
        but falls back to unverified if verification fails (to handle extremely broken setups).
        """
        handlers = []

        # 1. Proxy Handler
        proxy_url = config_manager.get("proxy_url")
        if config_manager.get("proxy_mode") in ("http", "socks5") and proxy_url:
            # urllib can handle http/https proxies.
            # Note: For SOCKS5, urllib natively might not support it without PySocks,
            # but typical standard SOCKS5 proxies can often be accessed if specified.
            # We'll set it for both http and https as requests did.
            proxies = {"http": proxy_url, "https": proxy_url}
            handlers.append(urllib.request.ProxyHandler(proxies))

        # 2. SSL Handler (use system certificates)
        # Windows Python automatically loads system certs into default context.
        ctx = ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))

        opener = urllib.request.build_opener(*handlers)
        return opener

    def _fetch_json(self, url: str) -> dict:
        """
        Fetches a JSON payload from the given URL using the configured opener.
        Handles basic SSL errors by attempting a fallback if the default system certs still fail.
        """
        opener = self._build_opener()
        req = urllib.request.Request(url, headers={"User-Agent": "FluentYTDL/DependencyManager"})

        try:
            with opener.open(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as e:
            # If we hit an SSL verification error, fallback to unverified context as a last resort
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                logger.warning(
                    f"SSL verification failed for {url}. Attempting fallback with unverified context."
                )

                # Rebuild opener with unverified context
                handlers = []
                proxy_url = config_manager.get("proxy_url")
                if config_manager.get("proxy_mode") in ("http", "socks5") and proxy_url:
                    handlers.append(
                        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                    )

                unverified_ctx = ssl._create_unverified_context()
                handlers.append(urllib.request.HTTPSHandler(context=unverified_ctx))

                fallback_opener = urllib.request.build_opener(*handlers)
                with fallback_opener.open(req, timeout=10) as fb_response:
                    return json.loads(fb_response.read().decode("utf-8"))
            raise


class UpdateCheckerWorker(QThread):
    finished_signal = Signal(str, dict)
    error_signal = Signal(str, str)

    def __init__(self, key: str, manager: DependencyManager):
        super().__init__()
        self.key = key
        self.manager = manager

    @staticmethod
    def _parse_version_tuple(ver: str) -> tuple[int, ...] | None:
        """将版本字符串归一化为可比较的整数元组。"""
        cleaned = ver.lstrip("vn").strip()
        m = re.match(r"(\d+(?:\.\d+)*)", cleaned)
        if m:
            return tuple(int(x) for x in m.group(1).split("."))
        return None

    def run(self):
        try:
            exe_path = self.manager.get_exe_path(self.key)
            current_ver = self._get_local_version(self.key, exe_path)

            latest_ver, url = self._get_remote_version(self.key)

            update_available = False
            if current_ver == "channel_switched":
                update_available = True
            elif latest_ver and latest_ver != "unknown":
                c_tuple = self._parse_version_tuple(current_ver)
                l_tuple = self._parse_version_tuple(latest_ver)

                if c_tuple is not None and l_tuple is not None:
                    # 对齐元组长度: (7,1) vs (7,1,3) → (7,1,0) vs (7,1,3)
                    max_len = max(len(c_tuple), len(l_tuple))
                    c_padded = c_tuple + (0,) * (max_len - len(c_tuple))
                    l_padded = l_tuple + (0,) * (max_len - len(l_tuple))
                    # 仅当远程版本严格大于本地时才提示更新
                    update_available = l_padded > c_padded

                    # 跨渠道切换时，无论数字版本号大小比较结果，一律强制触发更新 (例如 nightly 换 master 可能版本变小)
                    actual_ch = current_ver.split("(")[-1].strip(")") if "(" in current_ver else ""
                    latest_ch = latest_ver.split("(")[-1].strip(")") if "(" in latest_ver else ""
                    if self.key == "yt-dlp" and actual_ch and latest_ch and actual_ch != latest_ch:
                        update_available = True
                    elif c_padded == l_padded and current_ver != latest_ver:
                        update_available = True
                else:
                    # 无法解析则 fallback 到字符串比较
                    c_norm = current_ver.lstrip("vn")
                    l_norm = latest_ver.lstrip("vn")
                    update_available = c_norm != l_norm

            result = {
                "current": current_ver,
                "latest": latest_ver,
                "update_available": update_available,
                "url": url,
            }
            self.finished_signal.emit(self.key, result)

        except Exception as e:
            logger.error(f"Update check failed for {self.key}: {e}")
            self.error_signal.emit(self.key, str(e))

    def _get_local_version(self, key: str, path: Path) -> str:
        if not path.exists():
            return "unknown"

        # Manifest check for yt-dlp channel switches is now embedded into the returned version string.

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

            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", **kwargs
            )
            if proc.returncode != 0:
                return "unknown"

            out = proc.stdout.strip()
            if key == "yt-dlp":
                # yt-dlp output is just the date/version: "2023.11.16"
                version_str = out.splitlines()[0]
                actual_channel = "stable"
                manifest_path = path.parent / "manifest.json"
                if manifest_path.exists():
                    try:
                        with open(manifest_path, encoding="utf-8") as f:
                            data = json.load(f)
                            actual_channel = str(data.get("channel", "stable")).strip()
                    except Exception:
                        pass
                return f"{version_str} ({actual_channel})"
            elif key == "deno":
                # deno 1.38.0 (release, x86_64-pc-windows-msvc) ...
                m = re.search(r"deno (\d+\.\d+\.\d+)", out)
                if m:
                    return m.group(1)
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
                        return vm.group(1)
                    return raw  # fallback
            elif key == "pot-provider":
                # bgutil-ytdlp-pot-provider-rs
                # Output: something like "bgutil-pot-provider 0.1.5" or just version
                m = re.search(r"(\d+\.\d+\.\d+)", out)
                if m:
                    return m.group(1)
            elif key == "ytarchive":
                # ytarchive outputs: "ytarchive v0.4.0" or similar
                m = re.search(r"v?(\d+\.\d+\.\d+)", out)
                if m:
                    return m.group(1)
            elif key == "atomicparsley":
                # AtomicParsley outputs: "AtomicParsley version: 20240608.083822.0 1ed9031..."
                # 只取日期+时间部分 (YYYYMMDD.HHMMSS)，忽略后面的 build/commit 信息
                m = re.search(r"(\d{8}\.\d{6})", out)
                if m:
                    return m.group(1)
            elif key == "aria2c":
                # aria2 version 1.36.0
                m = re.search(r"aria2 version (\d+\.\d+\.\d+)", out)
                if m:
                    return m.group(1)

            return "installed"  # Fallback if parsing fails
        except Exception:
            return "unknown"

    def _get_remote_version(self, key: str) -> tuple[str, str]:
        # Return (version_tag, download_url)
        # Using GitHub API via urllib

        url = ""
        channel_label = ""
        if key == "yt-dlp":
            channel = str(config_manager.get("ytdlp_channel", "stable")).strip()
            channel_label = channel
            if channel == "nightly":
                url = "https://api.github.com/repos/yt-dlp/yt-dlp-nightly-builds/releases/latest"
            elif channel == "master":
                url = "https://api.github.com/repos/yt-dlp/yt-dlp-master-builds/releases/latest"
            else:
                url = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        elif key == "deno":
            url = "https://api.github.com/repos/denoland/deno/releases/latest"
        elif key == "ffmpeg":
            url = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
        elif key == "pot-provider":
            url = (
                "https://api.github.com/repos/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest"
            )
        elif key == "ytarchive":
            url = "https://api.github.com/repos/Kethsar/ytarchive/releases/latest"
        elif key == "atomicparsley":
            url = "https://api.github.com/repos/wez/atomicparsley/releases/latest"
        else:
            return "unknown", ""

        try:
            data = self.manager._fetch_json(url)
        except Exception as e:
            logger.error(f"Failed to fetch release info for {key}: {e}")
            return "unknown", ""

        if key == "yt-dlp":
            tag = data.get("tag_name", "unknown")

            # Find exe asset
            dl_url = ""
            for asset in data.get("assets", []):
                if asset["name"] == "yt-dlp.exe":
                    dl_url = asset["browser_download_url"]
                    break
            return f"{tag} ({channel_label})", dl_url

        elif key == "deno":
            tag = data.get("tag_name", "vunknown").lstrip("v")

            # Find windows zip
            dl_url = ""
            for asset in data.get("assets", []):
                if "x86_64-pc-windows-msvc.zip" in asset["name"]:
                    dl_url = asset["browser_download_url"]
                    break
            return tag, dl_url

        elif key == "ffmpeg":
            # BtbN/FFmpeg-Builds: "latest" release 包含多个版本 (n7.1, n8.0, master)

            # 收集所有版本化的 win64-gpl static 构建，选取最高版本
            candidates: list[tuple[tuple[int, ...], str, str, str]] = []
            for asset in data.get("assets", []):
                name = asset["name"]
                if "win64-gpl" in name and ".zip" in name and "shared" not in name:
                    m = re.search(r"ffmpeg-n(\d+(?:\.\d+)*)", name)
                    if m:
                        ver_str = m.group(1)
                        ver_tuple = tuple(int(x) for x in ver_str.split("."))
                        candidates.append((ver_tuple, ver_str, asset["browser_download_url"], name))

            if candidates:
                # 按版本号降序排列，取最高版本
                candidates.sort(reverse=True, key=lambda x: x[0])
                _, tag, dl_url, asset_name = candidates[0]
            else:
                # Fallback: master 构建
                dl_url, tag = "", "unknown"
                for asset in data.get("assets", []):
                    if (
                        "win64-gpl" in asset["name"]
                        and ".zip" in asset["name"]
                        and "shared" not in asset["name"]
                    ):
                        dl_url = asset["browser_download_url"]
                        tag = "master"
                        break

            return tag, dl_url

        elif key == "pot-provider":
            # bgutil-ytdlp-pot-provider-rs from jim60105
            tag = data.get("tag_name", "vunknown").lstrip("v")

            # Find windows exe or zip
            dl_url = ""
            for asset in data.get("assets", []):
                name = asset["name"].lower()
                # Look for windows exe: bgutil-pot-windows-x86_64.exe
                if "windows" in name and name.endswith(".exe"):
                    dl_url = asset["browser_download_url"]
                    break
            return tag, dl_url

        elif key == "ytarchive":
            # Kethsar/ytarchive from GitHub
            tag = data.get("tag_name", "vunknown").lstrip("v")

            # Find windows amd64 executable
            dl_url = ""
            for asset in data.get("assets", []):
                name = asset["name"].lower()
                # ytarchive_windows_amd64.exe
                if "windows" in name and "amd64" in name and name.endswith(".exe"):
                    dl_url = asset["browser_download_url"]
                    break
            return tag, dl_url

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
            return tag, dl_url

        return "unknown", ""


class DownloaderWorker(QObject):
    progress_signal = Signal(str, int)
    finished_signal = Signal(str)
    error_signal = Signal(str, str)

    def __init__(
        self,
        key: str,
        url: str,
        target_exe: Path,
        expected_version: str = "",
        expected_channel: str = "",
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
        self.extra_exes: list[str] = []

        if dependency_manager.components.get(key):
            self.extra_exes = dependency_manager.components[key].extra_exes

        from PySide6.QtCore import QProcess

        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self._on_ready_read)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_error)

        self._buffer = ""
        self._is_finished_emitted = False
        self._error_emitted = False

    def start(self):
        from .config_manager import config_manager

        proxy_url = config_manager.get("proxy_url")
        proxy_mode = config_manager.get("proxy_mode")

        config = {
            "key": self.key,
            "url": self.url,
            "target_exe": str(self.target_exe),
            "expected_version": self.expected_version,
            "expected_channel": self.expected_channel,
            "extra_exes": self.extra_exes,
            "proxy_url": proxy_url,
            "proxy_mode": proxy_mode,
        }

        import json

        from ..utils.paths import is_frozen

        args = []
        if not is_frozen():
            exe = sys.executable
            main_py = str(Path(__file__).resolve().parents[3] / "main.py")
            args = [main_py, "--update-worker"]
        else:
            exe = sys.executable
            args = ["--update-worker"]

        self.process.start(exe, args)
        if not self.process.waitForStarted():
            self._emit_error(f"Failed to start update worker process: {self.process.errorString()}")
            return

        config_data = json.dumps(config).encode("utf-8")
        self.process.write(config_data)
        self.process.closeWriteChannel()

    def _emit_error(self, message):
        if not self._error_emitted:
            self.error_signal.emit(self.key, message)
            self._error_emitted = True

    def _on_ready_read(self):
        data = self.process.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self._buffer += data
        lines = self._buffer.split("\n")
        self._buffer = lines[-1]

        import json

        for line in lines[:-1]:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                msg_type = msg.get("type")
                if msg_type == "progress":
                    self.progress_signal.emit(self.key, msg.get("percent", 0))
                elif msg_type == "error":
                    self._emit_error(msg.get("msg", "Unknown error in worker"))
                elif msg_type == "done":
                    # Let _on_finished handle the signal to ensure process has fully exited
                    pass
            except json.JSONDecodeError:
                from ..utils.logger import logger

                logger.debug(f"Worker stdout: {line}")

    def _on_finished(self, exitCode, exitStatus):
        from PySide6.QtCore import QProcess

        if exitStatus == QProcess.ExitStatus.CrashExit:
            self._emit_error("Update worker process crashed")
        elif exitCode != 0:
            err = self.process.readAllStandardError().data().decode("utf-8", errors="replace")
            self._emit_error(f"Update worker exited with code {exitCode}. Stderr: {err}")
        else:
            if not self._is_finished_emitted and not self._error_emitted:
                self.finished_signal.emit(self.key)
                self._is_finished_emitted = True

    def _on_error(self, error):
        self._emit_error(f"Worker process error: {error}")


# Global instance
dependency_manager = DependencyManager()
