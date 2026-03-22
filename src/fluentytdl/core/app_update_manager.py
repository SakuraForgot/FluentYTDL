"""
FluentYTDL 主程序自动更新管理器

复用 DependencyManager 的网络基础设施 (代理/SSL/镜像)。
根据安装类型自动选择最优更新策略:
  - setup: InnoSetup /SILENT 覆盖安装
  - full:  rename → 解压覆盖 → 重启
  - portable: rename → 替换单文件 → 重启
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import requests
from PySide6.QtCore import QObject, QThread, Signal

from ..utils.logger import logger
from ..utils.paths import detect_install_type, frozen_app_dir
from .config_manager import config_manager

# ─── 版本比较 ────────────────────────────────────────────


def _parse_version(ver: str) -> tuple[int, ...]:
    """将 '3.0.0' 或 'v3.0.0-pre' 解析为可比较的整数元组"""
    clean = re.sub(r"^v", "", str(ver).strip())
    clean = clean.split("-")[0]  # 去掉 -pre / -beta
    parts: list[int] = []
    for p in clean.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _get_proxies() -> dict[str, str]:
    """从 config 构建 requests 代理字典，复用 DependencyManager 的代理逻辑。"""
    proxy_mode = str(config_manager.get("proxy_mode") or "off").lower()
    proxy_url = str(config_manager.get("proxy_url") or "")

    if proxy_mode in ("http", "socks5") and proxy_url:
        scheme = "socks5h" if proxy_mode == "socks5" else "http"
        url = proxy_url if "://" in proxy_url else f"{scheme}://{proxy_url}"
        return {"http": url, "https": url}

    # system / off → 让 requests 自行处理（system 会走环境变量代理）
    return {}


# ─── 检查更新线程 ────────────────────────────────────────


class _CheckWorker(QThread):
    """后台线程：查询 GitHub Releases API"""

    finished = Signal(dict)  # {tag_name, body, assets: [...]}
    error = Signal(str)

    GITHUB_API = "https://api.github.com/repos/prideicker/FluentYTDL/releases/latest"

    def run(self) -> None:
        try:
            proxies = _get_proxies()
            resp = requests.get(self.GITHUB_API, proxies=proxies, timeout=15)
            resp.raise_for_status()
            self.finished.emit(resp.json())
        except Exception as e:
            self.error.emit(str(e))


# ─── 下载更新线程 ────────────────────────────────────────


class _DownloadWorker(QThread):
    """后台线程：下载更新文件 (带进度 + SHA256 校验)"""

    progress = Signal(int)  # 0-100
    finished = Signal(str)  # 下载后的本地文件路径
    error = Signal(str)

    def __init__(self, url: str, expected_sha256: str = ""):
        super().__init__()
        self.url = url
        self.expected_sha256 = expected_sha256

    def run(self) -> None:
        try:
            # 应用镜像
            from .dependency_manager import dependency_manager

            final_url = dependency_manager.get_mirror_url(self.url)

            proxies = _get_proxies()

            # 下载到临时文件
            tmp_dir = Path(tempfile.mkdtemp(prefix="fluentytdl_update_"))
            filename = self.url.rsplit("/", 1)[-1]
            dest = tmp_dir / filename

            resp = requests.get(final_url, proxies=proxies, timeout=120, stream=True)
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

            # SHA256 校验
            if self.expected_sha256:
                actual = sha256.hexdigest().lower()
                expected = self.expected_sha256.strip().lower()
                if actual != expected:
                    dest.unlink(missing_ok=True)
                    self.error.emit(f"SHA256 校验失败\n预期: {expected}\n实际: {actual}")
                    return

            self.progress.emit(100)
            self.finished.emit(str(dest))

        except Exception as e:
            self.error.emit(str(e))


# ─── 主管理器 ────────────────────────────────────────────


class AppUpdateManager(QObject):
    """FluentYTDL 主程序自动更新管理器"""

    # 信号
    update_available = Signal(dict)  # {version, changelog, download_url, sha256, install_type}
    no_update = Signal()
    check_error = Signal(str)

    download_progress = Signal(int)  # 0-100
    download_finished = Signal(str)  # 本地路径
    download_error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._check_worker: _CheckWorker | None = None
        self._download_worker: _DownloadWorker | None = None
        self._silent: bool = False

    # ── 检查更新 ──────────────────────────────────────────

    def check_for_update(self, silent: bool = False) -> None:
        """发起更新检查 (异步)"""
        self._silent = silent

        worker = _CheckWorker()
        worker.finished.connect(self._on_check_done)
        worker.error.connect(self._on_check_error)
        self._check_worker = worker
        worker.start()

    def _on_check_done(self, data: dict[str, Any]) -> None:
        from fluentytdl import __version__

        tag = str(data.get("tag_name") or "")
        if not tag:
            if not self._silent:
                self.no_update.emit()
            return

        latest = _parse_version(tag)
        current = _parse_version(__version__)

        if latest <= current:
            if not self._silent:
                self.no_update.emit()
            return

        # 检查用户是否跳过了这个版本
        skipped = str(config_manager.get("skipped_app_version") or "")
        if skipped and _parse_version(skipped) >= latest and self._silent:
            return

        # 解析资产
        assets = data.get("assets") or []
        install_type = detect_install_type()
        download_url, sha256 = self._pick_asset(assets, install_type)

        changelog = str(data.get("body") or "")

        self.update_available.emit(
            {
                "version": tag.lstrip("v"),
                "tag": tag,
                "changelog": changelog,
                "download_url": download_url,
                "sha256": sha256,
                "install_type": install_type,
            }
        )

    def _on_check_error(self, msg: str) -> None:
        if not self._silent:
            self.check_error.emit(msg)

    @staticmethod
    def _pick_asset(assets: list[dict[str, Any]], install_type: str) -> tuple[str, str]:
        """根据安装类型选择最佳下载资产"""
        # 按优先级选择资产
        patterns: dict[str, list[str]] = {
            "setup": ["-setup.exe"],
            "full": ["-full.7z"],
            "portable": ["-portable.exe"],
        }

        target_patterns = patterns.get(install_type, patterns["setup"])

        for pat in target_patterns:
            for a in assets:
                name = str(a.get("name") or "")
                if name.endswith(pat):
                    url = str(a.get("browser_download_url") or "")
                    return url, ""  # SHA256 暂不在此解析

        # 兜底: 返回第一个 .exe
        for a in assets:
            name = str(a.get("name") or "")
            if name.endswith(".exe"):
                return str(a.get("browser_download_url") or ""), ""

        return "", ""

    # ── 下载更新 ──────────────────────────────────────────

    def download_update(self, url: str, sha256: str = "") -> None:
        """下载更新文件 (异步)"""
        worker = _DownloadWorker(url, sha256)
        worker.progress.connect(self.download_progress)
        worker.finished.connect(self._on_download_done)
        worker.error.connect(self.download_error)
        self._download_worker = worker
        worker.start()

    def _on_download_done(self, path: str) -> None:
        self.download_finished.emit(path)

    # ── 应用更新 ──────────────────────────────────────────

    @staticmethod
    def apply_update(file_path: str, install_type: str) -> None:
        """应用更新 (同步，调用后应立即退出主程序)"""
        path = Path(file_path)

        if install_type == "setup":
            _apply_setup(path)
        elif install_type == "full":
            _apply_full(path)
        elif install_type == "portable":
            _apply_portable(path)
        else:
            _apply_setup(path)  # 兜底


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 三种安装策略的实现
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _apply_setup(installer_path: Path) -> None:
    """安装版: 启动 InnoSetup /SILENT 覆盖安装，然后退出自身"""
    logger.info(f"[AppUpdate] 启动安装器: {installer_path}")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [
            str(installer_path),
            "/SILENT",  # 静默安装
            "/CLOSEAPPLICATIONS",  # 自动关闭正在运行的实例
            "/RESTARTAPPLICATIONS",  # 安装后重新启动
            "/NOCANCEL",  # 不允许取消
        ],
        creationflags=creationflags,
    )
    sys.exit(0)


def _apply_full(archive_path: Path) -> None:
    """Full 便携版: rename self → 解压覆盖 → PowerShell 重启

    核心技巧: Windows 允许重命名正在运行的 exe，只是不允许覆盖。
    """
    app_dir = frozen_app_dir()
    exe_name = Path(sys.executable).name
    exe_path = Path(sys.executable)
    old_exe = exe_path.with_suffix(".exe.old")

    # 1. 重命名当前 exe
    try:
        if old_exe.exists():
            old_exe.unlink()
        os.rename(str(exe_path), str(old_exe))
    except Exception as e:
        logger.error(f"[AppUpdate] 重命名失败: {e}")
        return

    # 2. 用 PowerShell 在后台完成: 等待退出 → 解压 → 清理 → 重启
    ps_script = (
        f"Start-Sleep -Seconds 2; "
        f'$7z = Join-Path "{app_dir}" "bin\\7z.exe"; '
        f"if (Test-Path $7z) {{ "
        f'  & $7z x "{archive_path}" -o"{app_dir}" -aoa -y '
        f"}} else {{ "
        f"  $sys7z = (Get-Command 7z -ErrorAction SilentlyContinue).Source; "
        f'  if ($sys7z) {{ & $sys7z x "{archive_path}" -o"{app_dir}" -aoa -y }} '
        f"}}; "
        f'Remove-Item -Path "{old_exe}" -Force -ErrorAction SilentlyContinue; '
        f'Start-Process "{app_dir / exe_name}" -ArgumentList "--cleanup-update"'
    )

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
        creationflags=creationflags,
    )
    sys.exit(0)


def _apply_portable(new_exe_path: Path) -> None:
    """单文件便携版: rename self → move new → PowerShell 重启"""
    exe_path = Path(sys.executable)
    old_exe = exe_path.with_suffix(".exe.old")

    # 1. 重命名
    try:
        if old_exe.exists():
            old_exe.unlink()
        os.rename(str(exe_path), str(old_exe))
    except Exception as e:
        logger.error(f"[AppUpdate] 重命名失败: {e}")
        return

    # 2. 移动新版到位
    try:
        shutil.move(str(new_exe_path), str(exe_path))
    except Exception as e:
        # 回滚
        os.rename(str(old_exe), str(exe_path))
        logger.error(f"[AppUpdate] 替换失败: {e}")
        return

    # 3. PowerShell 重启 + 清理
    ps_script = (
        f"Start-Sleep -Seconds 1; "
        f'Remove-Item -Path "{old_exe}" -Force -ErrorAction SilentlyContinue; '
        f'Start-Process "{exe_path}" -ArgumentList "--cleanup-update"'
    )

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-Command", ps_script],
        creationflags=creationflags,
    )
    sys.exit(0)


# ── 单例 ──
app_update_manager = AppUpdateManager()
