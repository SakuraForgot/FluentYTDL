import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..extension_gen import ExtensionGenerator
from ..server import LocalCookieServer

logger = logging.getLogger(__name__)


class DLEProvider:
    """
    Dynamic Local Extension (DLE) Provider.
    Extracts cookies by injecting a temporary extension into a clean browser instance.
    """

    def __init__(self):
        self.server = LocalCookieServer()
        self.generator = ExtensionGenerator()

    def extract_cookies(self, browser_type: str = "edge") -> list[dict[str, Any]]:
        """
        Orchestrates the cookie extraction process.

        Args:
            browser_type: Hint for auto-detection fallback priority.

        Returns:
            A list of cookie dictionaries.

        Raises:
            FileNotFoundError: If no suitable browser is found.
            RuntimeError: If extraction fails or times out.
        """

        # 1. Resolve browser executable
        exe_path = self._resolve_browser(browser_type)
        if not exe_path or not exe_path.exists():
            raise FileNotFoundError(
                "未找到支持的浏览器。\n\n"
                "请安装以下任一浏览器：\n"
                "• Microsoft Edge (推荐，Windows 自带)\n"
                "• Google Chrome\n"
                "• Brave / Vivaldi / Opera 等 Chromium 内核浏览器"
            )

        logger.info(f"Using browser: {exe_path}")

        # 2. Start local server
        try:
            port = self.server.start()
            logger.info(f"Local receiver started on port {port}")
        except OSError as e:
            raise RuntimeError(
                f"无法启动本地服务：{e}\n请检查是否有其他程序占用端口，或防火墙阻止了本地连接。"
            ) from e

        # 3. Create temporary directories
        temp_dir = Path(tempfile.mkdtemp(prefix="fluentytdl_dle_"))
        ext_dir = temp_dir / "extension"
        user_data_dir = temp_dir / "profile"

        ext_dir.mkdir()
        user_data_dir.mkdir()

        browser_process: subprocess.Popen | None = None

        try:
            # 4. Generate extension (with auth token)
            self.generator.generate(ext_dir, port, auth_token=self.server.auth_token)
            logger.info(f"Extension generated at {ext_dir}")

            # 5. Launch browser
            cmd = [
                str(exe_path),
                f"--load-extension={str(ext_dir)}",
                f"--user-data-dir={str(user_data_dir)}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate",
                "--disable-popup-blocking",
                "--app=https://www.youtube.com",
            ]

            logger.info("Launching browser...")
            try:
                browser_process = subprocess.Popen(cmd)
            except FileNotFoundError as err:
                raise RuntimeError(f"无法启动浏览器: {exe_path}\n文件可能已被移动或删除。") from err
            except PermissionError as err:
                raise RuntimeError(
                    f"没有权限启动浏览器: {exe_path}\n请检查文件权限或尝试以管理员身份运行。"
                ) from err

            # 6. Wait for cookies
            logger.info("Waiting for user to login...")

            start_time = time.time()
            max_wait_time = 300  # 5 minutes

            while time.time() - start_time < max_wait_time:
                if browser_process.poll() is not None:
                    logger.warning("Browser process terminated by user.")
                    raise RuntimeError(
                        "浏览器已关闭，登录流程已取消。\n请重新点击「登录 YouTube」完成操作。"
                    )

                cookies = self.server.wait_for_cookies(timeout=1.0)
                if cookies:
                    logger.info(f"Received {len(cookies)} cookies.")
                    logger.info("Waiting for success notification to display...")
                    time.sleep(3)
                    return cookies

            raise RuntimeError("登录超时（5分钟），请重新点击登录并尽快完成操作。")

        finally:
            logger.info("Cleaning up...")
            self.server.stop()

            if browser_process and browser_process.poll() is None:
                try:
                    browser_process.terminate()
                    try:
                        browser_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        browser_process.kill()
                except Exception as e:
                    logger.warning(f"Failed to kill browser process: {e}")

            time.sleep(1)
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to remove temp dir {temp_dir}: {e}")

    # ==================== 浏览器检测 ====================

    # Chromium 浏览器已知 exe 文件名
    CHROMIUM_EXE_NAMES = {
        "msedge.exe",
        "chrome.exe",
        "brave.exe",
        "vivaldi.exe",
        "opera.exe",
        "browser.exe",
        "chromium.exe",
    }

    # 标准安装路径 (Windows)
    BROWSER_PATHS: dict[str, list[str]] = {
        "edge": [
            "{PROGRAMFILES(X86)}/Microsoft/Edge/Application/msedge.exe",
            "{PROGRAMFILES}/Microsoft/Edge/Application/msedge.exe",
        ],
        "chrome": [
            "{PROGRAMFILES}/Google/Chrome/Application/chrome.exe",
            "{PROGRAMFILES(X86)}/Google/Chrome/Application/chrome.exe",
            "{LOCALAPPDATA}/Google/Chrome/Application/chrome.exe",
        ],
        "brave": [
            "{PROGRAMFILES}/BraveSoftware/Brave-Browser/Application/brave.exe",
            "{LOCALAPPDATA}/BraveSoftware/Brave-Browser/Application/brave.exe",
        ],
        "vivaldi": [
            "{LOCALAPPDATA}/Vivaldi/Application/vivaldi.exe",
            "{PROGRAMFILES}/Vivaldi/Application/vivaldi.exe",
        ],
        "opera": [
            "{LOCALAPPDATA}/Programs/Opera/opera.exe",
            "{PROGRAMFILES}/Opera/opera.exe",
        ],
        "opera_gx": [
            "{LOCALAPPDATA}/Programs/Opera GX/opera.exe",
            "{PROGRAMFILES}/Opera GX/opera.exe",
        ],
        "centbrowser": [
            "{LOCALAPPDATA}/CentBrowser/Application/chrome.exe",
            "{PROGRAMFILES}/CentBrowser/Application/chrome.exe",
        ],
        "chromium": [
            "{LOCALAPPDATA}/Chromium/Application/chrome.exe",
            "{PROGRAMFILES}/Chromium/Application/chrome.exe",
        ],
    }

    @staticmethod
    def _resolve_default_browser() -> Path | None:
        """
        通过 Windows 注册表检测系统默认浏览器。
        仅返回 Chromium 内核浏览器（支持 --load-extension）。
        """
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            )
            prog_id = winreg.QueryValueEx(key, "ProgID")[0]
            winreg.CloseKey(key)

            # 尝试多种注册表路径结构解析 exe
            exe_path = None
            for reg_path in [f"{prog_id}\\shell\\open\\command", f"{prog_id}\\Application"]:
                try:
                    key2 = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, reg_path)
                    if "Application" in reg_path:
                        app_icon = winreg.QueryValueEx(key2, "ApplicationIcon")[0]
                        exe_path = app_icon.split(",")[0].strip('"')
                    else:
                        cmd = winreg.QueryValueEx(key2, "")[0]
                        exe_path = cmd.split('"')[1] if '"' in cmd else cmd.split()[0]
                    winreg.CloseKey(key2)
                    if exe_path:
                        break
                except (FileNotFoundError, OSError):
                    continue

            if not exe_path:
                logger.debug(f"[DLE] 无法从 ProgID '{prog_id}' 解析浏览器路径")
                return None

            path = Path(exe_path)
            if not path.exists():
                logger.debug(f"[DLE] 默认浏览器路径不存在: {exe_path}")
                return None

            # 验证是否为 Chromium 内核
            exe_name = path.name.lower()
            if exe_name in DLEProvider.CHROMIUM_EXE_NAMES:
                logger.info(f"[DLE] 检测到默认浏览器 (Chromium): {path}")
                return path

            # 未知 exe 名，尝试 --version 验证
            try:
                result = subprocess.run(
                    [str(path), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if "chromium" in result.stdout.lower() or "chrome" in result.stdout.lower():
                    logger.info(f"[DLE] 默认浏览器确认为 Chromium 内核: {path}")
                    return path
            except Exception:
                pass

            logger.info(f"[DLE] 默认浏览器 ({exe_name}) 非 Chromium 内核，跳过")
            return None

        except Exception as e:
            logger.debug(f"[DLE] 默认浏览器检测失败: {e}")
            return None

    def _resolve_browser(self, browser_type: str = "edge") -> Path | None:
        """Finds a valid Chromium browser executable.

        Search order:
        1. System default browser (if Chromium-based)
        2. Hardcoded paths for the requested browser_type
        3. Fallback: Edge -> Chrome -> other browsers
        """

        # 1. 系统默认浏览器
        default = self._resolve_default_browser()
        if default:
            return default

        def _expand_paths(templates: list[str]) -> list[Path]:
            result = []
            for tmpl in templates:
                expanded = tmpl.format(
                    **{
                        k: os.environ.get(k, "")
                        for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
                    }
                )
                if expanded:
                    result.append(Path(expanded))
            return result

        # 2. Requested browser type
        bt = browser_type.lower()
        if bt in self.BROWSER_PATHS:
            for p in _expand_paths(self.BROWSER_PATHS[bt]):
                if p.exists():
                    return p

        # 3. Fallback chain
        for browser in [
            "edge",
            "chrome",
            "brave",
            "vivaldi",
            "opera",
            "opera_gx",
            "centbrowser",
            "chromium",
        ]:
            if browser == bt:
                continue
            if browser in self.BROWSER_PATHS:
                for p in _expand_paths(self.BROWSER_PATHS[browser]):
                    if p.exists():
                        logger.info(f"Fallback: found {browser} at {p}")
                        return p

        return None

    @staticmethod
    def cookies_to_netscape(cookies: list[dict[str, Any]]) -> str:
        """Converts list of cookie dicts to Netscape format string."""
        lines = ["# Netscape HTTP Cookie File"]
        lines.append("# This file is generated by FluentYTDL DLE Provider")
        lines.append("")

        for cookie in cookies:
            domain = cookie.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = cookie.get("path", "/")
            secure = "TRUE" if cookie.get("secure", False) else "FALSE"
            expiration = (
                str(int(cookie.get("expirationDate", 0))) if "expirationDate" in cookie else "0"
            )
            name = cookie.get("name", "")
            value = cookie.get("value", "")

            lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiration}\t{name}\t{value}")

        return "\n".join(lines)
