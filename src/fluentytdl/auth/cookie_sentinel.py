"""
FluentYTDL Cookie Sentinel (Cookie 卫士)

统一管理 bin/cookies.txt 的完整生命周期：
1. 启动阶段：静默预提取 (Best-Effort，无 UAC)
2. 下载阶段：yt-dlp 始终使用统一文件
3. 容错阶段：检测 403/登录错误，提示用户授权修复
4. 来源追踪：记录 Cookie 提取来源，切换浏览器时自动清理

设计原则：
- 单例模式，全局唯一
- 启动时不干扰用户体验（无弹窗）
- 失败时提供明确的修复引导
- 严格的来源追踪，避免混用不同浏览器的 Cookie
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from ..utils.logger import logger
from .auth_service import AuthSourceType, auth_service


class CookieSentinel:
    """
    Cookie 卫士 - 统一 Cookie 生命周期管理

    核心职责：
    1. 维护唯一的 bin/cookies.txt 文件
    2. 启动时静默尝试更新（Best-Effort）
    3. 提供错误检测与修复接口
    """

    _instance: CookieSentinel | None = None
    _lock = threading.Lock()

    def __new__(cls, cookie_path: Path | None = None) -> CookieSentinel:
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, cookie_path: Path | None = None):
        """
        初始化 Cookie 卫士

        Args:
            cookie_path: cookies.txt 文件路径，默认 bin/cookies.txt
        """
        if getattr(self, "_initialized", False):
            return

        self._initialized = True

        # 统一的 Cookie 文件路径
        if cookie_path is None:
            # 默认路径：应用目录/bin/cookies.txt
            try:
                from ..utils.paths import frozen_app_dir, is_frozen

                if is_frozen():
                    # 打包环境：使用可执行文件所在目录
                    root = frozen_app_dir()
                else:
                    # 开发环境：使用项目根目录
                    from ..utils.paths import project_root

                    root = project_root()
                self._base_dir = root / "bin"
            except Exception:
                # Fallback: 使用临时目录
                import tempfile

                self._base_dir = Path(tempfile.gettempdir())
        else:
            self._base_dir = cookie_path.parent

        # 确保目录存在
        self._base_dir.mkdir(parents=True, exist_ok=True)

        # 兼容旧版本：将旧的 cookies.txt 迁移到 cookies_youtube.txt
        old_cookie_path = self._base_dir / "cookies.txt"
        old_meta_path = self._base_dir / "cookies.txt.meta"
        new_cookie_path = self._base_dir / "cookies_youtube.txt"
        new_meta_path = self._base_dir / "cookies_youtube.txt.meta"

        if old_cookie_path.exists() and not new_cookie_path.exists():
            import shutil

            try:
                shutil.move(str(old_cookie_path), str(new_cookie_path))
                logger.info(
                    f"[CookieSentinel] 已迁移旧版 Cookie 文件: {old_cookie_path.name} -> {new_cookie_path.name}"
                )
            except Exception as e:
                logger.warning(f"[CookieSentinel] 迁移旧版 Cookie 文件失败: {e}")

        if old_meta_path.exists() and not new_meta_path.exists():
            import shutil

            try:
                shutil.move(str(old_meta_path), str(new_meta_path))
                logger.info(
                    f"[CookieSentinel] 已迁移旧版 Cookie 元数据: {old_meta_path.name} -> {new_meta_path.name}"
                )
            except Exception as e:
                logger.warning(f"[CookieSentinel] 迁移旧版 Cookie 元数据失败: {e}")

        # 设置兼容属性（默认指向 youtube）
        self.cookie_path = new_cookie_path
        self.meta_path = new_meta_path

        # 状态追踪
        self._last_update: datetime | None = None
        self._is_updating = False
        self._update_lock = threading.Lock()

        # 回退状态追踪（当提取失败但有旧 Cookie 可用时）
        self._using_fallback = False
        self._fallback_warning: str | None = None

        logger.info(f"Cookie Sentinel 初始化: {self.cookie_path}")

    # ==================== 元数据管理 ====================

    def _load_meta(self, platform: str = "youtube") -> dict | None:
        """
        加载 Cookie 元数据

        Returns:
            元数据字典，或 None 如果不存在/无效
        """
        meta_path = self.get_meta_path_for_platform(platform)
        if not meta_path.exists():
            return None
        try:
            import json

            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[CookieSentinel] 读取元数据失败 ({platform}): {e}")
            return None

    def _save_meta(self, source: str, cookie_count: int = 0, platform: str = "youtube") -> None:
        """
        保存 Cookie 元数据

        Args:
            source: 来源标识（如 "edge", "firefox", "file"）
            cookie_count: Cookie 数量
        """
        meta = {
            "source": source,
            "extracted_at": datetime.now().isoformat(),
            "cookie_count": cookie_count,
        }
        try:
            meta_path = self.get_meta_path_for_platform(platform)
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.debug(
                f"[CookieSentinel] 元数据已保存 ({platform}): {source}, {cookie_count} cookies"
            )
        except Exception as e:
            logger.warning(f"[CookieSentinel] 保存元数据失败 ({platform}): {e}")

    def _clear_cookie_and_meta(self, platform: str = "youtube") -> None:
        """清除 Cookie 文件和元数据"""
        try:
            cookie_path = self.get_cookie_path_for_platform(platform)
            meta_path = self.get_meta_path_for_platform(platform)
            if cookie_path.exists():
                cookie_path.unlink()
                logger.info(f"[CookieSentinel] 已删除旧 Cookie 文件: {cookie_path}")
            if meta_path.exists():
                meta_path.unlink()
                logger.info(f"[CookieSentinel] 已删除旧元数据文件: {meta_path}")
        except Exception as e:
            logger.warning(f"[CookieSentinel] 清除文件失败 ({platform}): {e}")

    def get_cookie_source(self, platform: str = "youtube") -> str | None:
        """
        获取当前 Cookie 文件的实际来源

        Returns:
            来源标识（如 "edge", "firefox"），或 None 如果无记录
        """
        meta = self._load_meta(platform)
        return meta.get("source") if meta else None

    def validate_source_consistency(
        self, expected_source: str, platform: str = "youtube"
    ) -> tuple[bool, str | None]:
        """
        验证 Cookie 来源是否与期望一致

        Args:
            expected_source: 期望的来源（当前配置的浏览器）

        Returns:
            (是否一致, 实际来源) - 不再强制清理，只返回状态
        """
        if not self.get_cookie_path_for_platform(platform).exists():
            return True, None  # 没有 Cookie 文件，视为一致

        actual_source = self.get_cookie_source(platform)
        if actual_source is None:
            # 旧版本的 Cookie 文件没有元数据
            logger.debug("[CookieSentinel] Cookie 文件缺少来源元数据")
            return False, None

        # WebView2 多账号场景下，source 可能写成 webview2:<account_id>，此时视为与 dle 一致
        normalized_actual = actual_source
        if isinstance(actual_source, str) and actual_source.startswith("webview2:"):
            normalized_actual = "webview2"

        if normalized_actual != expected_source:
            logger.debug(
                f"[CookieSentinel] Cookie 来源不匹配: 现有={actual_source}, 期望={expected_source}"
            )
            return False, actual_source

        return True, actual_source

    # ==================== 公共接口 ====================

    @property
    def exists(self) -> bool:
        """Cookie 文件是否存在"""
        return self.cookie_path.exists()

    @property
    def age_minutes(self) -> float | None:
        """Cookie 文件年龄（分钟），不存在返回 None (兼容旧接口，默认 youtube)"""
        return self.get_age_minutes("youtube")

    def get_age_minutes(self, platform: str = "youtube") -> float | None:
        """特定平台的 Cookie 文件年龄（分钟）"""
        cookie_path = self.get_cookie_path_for_platform(platform)
        if not cookie_path.exists():
            return None
        try:
            mtime = datetime.fromtimestamp(cookie_path.stat().st_mtime)
            return (datetime.now() - mtime).total_seconds() / 60
        except Exception:
            return None

    @property
    def is_stale(self) -> bool:
        """Cookie 是否过期（兼容旧接口，默认 youtube）"""
        return self.get_is_stale("youtube")

    def get_is_stale(self, platform: str = "youtube") -> bool:
        """特定平台的 Cookie 是否过期"""
        if not self.get_cookie_path_for_platform(platform).exists():
            return True

        # 仅检查 Cookie 实际 expires（SID/HSID 等关键字段）
        expiry = self.get_earliest_expiry(platform)
        if expiry is not None:
            return expiry <= 0

        # 无法解析出 expiry（全为 Session Cookie）→ 不视为过期
        # 真正的有效性由 auth_service._validate_cookies 判定
        return False

    def get_earliest_expiry(self, platform: str = "youtube") -> float | None:
        """
        获取关键 Cookie 中最早过期的剩余秒数。

        Returns:
            剩余秒数（负数=已过期），None=无法解析或无文件
        """
        cookie_path = self.get_cookie_path_for_platform(platform)
        if not cookie_path.exists():
            return None
        try:
            import time

            now = int(time.time())
            content = cookie_path.read_text(encoding="utf-8", errors="replace")
            _PLATFORM_KEY_NAMES = {
                "youtube": {"SID", "HSID", "SSID", "SAPISID", "APISID"},
                "twitter": {"auth_token", "ct0"},
            }
            key_names = _PLATFORM_KEY_NAMES.get(platform, set())
            if not key_names:
                return None
            earliest = None
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    name = parts[5]
                    if name in key_names:
                        try:
                            expires = int(parts[4])
                            if expires == 0:
                                continue  # session cookie，视为有效
                            remaining = expires - now
                            if earliest is None or remaining < earliest:
                                earliest = remaining
                        except (ValueError, IndexError):
                            pass
            return earliest
        except Exception:
            return None

    def is_expiring_soon(self, platform: str = "youtube", threshold_minutes: int = 60) -> bool:
        """Cookie 是否即将过期（默认 1 小时内）"""
        expiry = self.get_earliest_expiry(platform)
        if expiry is None:
            return False
        return 0 < expiry < threshold_minutes * 60

    def get_cookie_file_path(self) -> str:
        """
        获取 Cookie 文件路径（供 yt-dlp 使用）

        Returns:
            cookies.txt 的绝对路径字符串
        """
        return str(self.cookie_path.absolute())

    def get_cookie_path_for_platform(self, platform: str = "youtube") -> Path:
        """获取特定平台的 Cookie 文件路径"""
        filename = f"cookies_{platform}.txt"
        return self._base_dir / filename

    def get_meta_path_for_platform(self, platform: str = "youtube") -> Path:
        """获取特定平台的元数据文件路径"""
        return self.get_cookie_path_for_platform(platform).with_suffix(".txt.meta")

    def silent_refresh_on_startup(self) -> None:
        """
        启动时静默刷新 Cookie（Best-Effort）

        特点：
        - 非阻塞（后台线程）
        - 不请求 UAC（只尝试普通权限浏览器）
        - 失败静默处理，保留旧文件作为回退
        - 提取成功后才覆盖旧文件
        """

        def _refresh_worker():
            try:
                logger.info("[CookieSentinel] 启动时静默刷新开始...")

                # 重置回退状态
                self._using_fallback = False
                self._fallback_warning = None

                # 检查 AuthService 当前配置
                current_source = auth_service.current_source

                if current_source == AuthSourceType.NONE:
                    logger.info("[CookieSentinel] 未启用验证源，跳过静默刷新")
                    return

                # WebView2 模式是交互式流程（需用户登录），不能在启动时自动触发
                if current_source == AuthSourceType.WEBVIEW2:
                    for plat in ("youtube", "twitter"):
                        cache_file = auth_service.get_cookie_file_for_ytdlp(
                            platform=plat, force_refresh=False
                        )
                        if cache_file and Path(cache_file).exists():
                            account = auth_service.get_current_webview2_account(platform=plat)
                            source_tag = (
                                f"webview2:{account.account_id}"
                                if account and account.account_id
                                else "webview2"
                            )
                            import shutil

                            dest = self.get_cookie_path_for_platform(plat)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(cache_file), dest)

                            auth_service._update_status_from_file(str(cache_file), plat)
                            self._save_meta(source_tag, auth_service.last_status.cookie_count, plat)
                            logger.info(f"[CookieSentinel] WebView2 {plat} Cookie 同步完成")
                        else:
                            logger.info(f"[CookieSentinel] WebView2 模式：无 {plat} 缓存 Cookie")

                    self._last_update = datetime.now()
                    return

                # 获取期望的来源标识
                expected_source = current_source.value  # 如 "edge", "firefox", "file"

                # 检查来源一致性（只检查，不清理）
                is_consistent, actual_source = self.validate_source_consistency(expected_source)

                if current_source == AuthSourceType.FILE:
                    # 手动导入文件，直接复制
                    success = self._copy_from_auth_service()
                    if success:
                        for plat in ("youtube", "twitter"):
                            plat_path = self.get_cookie_path_for_platform(plat)
                            if plat_path.exists():
                                auth_service._update_status_from_file(str(plat_path), plat)
                                self._save_meta("file", auth_service.last_status.cookie_count, plat)
                        logger.info("[CookieSentinel] 已复制手动导入的Cookie文件")
                    else:
                        logger.warning("[CookieSentinel] 手动导入的Cookie文件不存在或无效")
                    return

                # 浏览器来源：尝试提取
                success = self._update_from_browser(silent=True)

                if success:
                    # 提取成功，元数据已在 _update_from_browser 中保存
                    self._using_fallback = False
                    self._fallback_warning = None
                    logger.info(
                        f"[CookieSentinel] 启动时静默刷新成功：{auth_service.current_source_display}"
                    )
                    logger.info(
                        f"[CookieSentinel] 提取了 {auth_service.last_status.cookie_count} 个 Cookie"
                    )
                else:
                    # 提取失败，检查是否有旧 Cookie 可用作回退
                    if self.exists and actual_source:
                        # 有旧 Cookie，标记为回退状态
                        self._using_fallback = True
                        self._fallback_warning = (
                            f"配置为 {auth_service.current_source_display}，"
                            f"但提取失败，当前使用 {self._get_source_display(actual_source)} 的 Cookie"
                        )
                        logger.warning(f"[CookieSentinel] {self._fallback_warning}")
                        # 验证回退 Cookie 的有效性，供 UI 层 check_cookie_status 使用
                        auth_service._update_status_from_file(str(self.cookie_path), "youtube")
                    else:
                        logger.warning(
                            f"[CookieSentinel] 启动时静默刷新失败: "
                            f"{auth_service.last_status.message}"
                        )
                    logger.info("[CookieSentinel] 用户可在设置页点击'手动刷新'重试")

            except Exception as e:
                # 静默失败，不影响启动
                logger.warning(f"[CookieSentinel] 启动时静默刷新异常（预期行为）: {e}")

        # 在后台线程执行，不阻塞主线程
        thread = threading.Thread(
            target=_refresh_worker, daemon=True, name="CookieSentinel-SilentRefresh"
        )
        thread.start()

    def force_refresh_with_uac(self, platform: str | None = None) -> tuple[bool, str]:
        """
        强制刷新 Cookie（允许 UAC 提权）

        用于用户手动触发修复或下载失败后的重试。
        采用延迟清理策略：只有成功提取后才覆盖旧文件。

        Args:
            platform: 指定更新的平台（None 则更新所有支持的平台）

        Returns:
            (成功标志, 状态消息)
        """
        with self._update_lock:
            if self._is_updating:
                return False, "正在更新中，请稍候..."

            self._is_updating = True

        try:
            logger.info("[CookieSentinel] 用户触发强制刷新（允许 UAC）")

            current_source = auth_service.current_source

            if current_source == AuthSourceType.NONE:
                return False, "未配置验证源，请先在设置中选择浏览器或导入 Cookie 文件"

            # 获取当前来源状态（只检查，不清理）
            expected_source = current_source.value
            is_consistent, actual_source = self.validate_source_consistency(expected_source)

            if current_source == AuthSourceType.FILE:
                success = self._copy_from_auth_service()
                if success:
                    # 元数据已在 _copy_from_auth_service 中保存
                    self._using_fallback = False
                    self._fallback_warning = None
                    return True, "已更新为手动导入的 Cookie 文件"
                else:
                    # 失败时保留旧文件
                    if self.exists and actual_source:
                        self._using_fallback = True
                        self._fallback_warning = f"导入失败，继续使用 {self._get_source_display(actual_source)} 的 Cookie"
                        return False, "导入失败（保留旧 Cookie）"
                    return False, "手动导入的 Cookie 文件不存在或无效"

            # 浏览器来源：强制刷新（允许 UAC）
            success = self._update_from_browser(silent=False, force=True, platform=platform)

            if success:
                # 提取成功，元数据已在 _update_from_browser 中保存
                self._using_fallback = False
                self._fallback_warning = None
                msg = QCoreApplication.translate("CookieSentinel", "✅ Cookie 已更新（{}）").format(
                    auth_service.current_source_display
                )
                if auth_service.last_status.cookie_count > 0:
                    msg += QCoreApplication.translate(
                        "CookieSentinel", "\n提取了 {} 个 Cookie"
                    ).format(auth_service.last_status.cookie_count)
                return True, msg
            else:
                # 提取失败，检查是否有旧 Cookie 可用作回退
                if self.exists and actual_source:
                    self._using_fallback = True
                    self._fallback_warning = QCoreApplication.translate(
                        "CookieSentinel", "从 {} 提取失败，继续使用 {} 的 Cookie"
                    ).format(
                        auth_service.current_source_display, self._get_source_display(actual_source)
                    )
                    return (
                        False,
                        f"更新失败: {auth_service.last_status.message}\n（保留旧 Cookie 可用）",
                    )
                return False, f"更新失败: {auth_service.last_status.message}"

        except Exception as e:
            logger.exception("[CookieSentinel] 强制刷新异常")
            return False, f"更新异常: {e}"

        finally:
            self._is_updating = False

    def detect_cookie_error(self, ytdlp_stderr: str) -> str:
        """
        检测 yt-dlp 错误的分类

        Args:
            ytdlp_stderr: yt-dlp 的标准错误输出

        Returns:
            "cookie" | "network" | "ambiguous" | "" (空字符串表示非相关错误)
        """
        if not ytdlp_stderr:
            return ""

        from ..diagnostics import FALLBACK_CODE, diagnose

        diag = diagnose(1, ytdlp_stderr)

        if diag.category == "auth":
            return "cookie"
        if diag.category == "network":
            return "network"
        if diag.code == FALLBACK_CODE:
            # 没能归类的错误：Cookie 失效常以各种面目出现，交给调用方走保守分支
            return "ambiguous"
        return ""

    def get_status_info(self, platform: str = "youtube") -> dict:
        """
        获取特定平台状态信息（供 UI 显示）

        Returns:
            状态字典，包含实时来源信息、Cookie 数量和有效性
        """
        actual_source = self.get_cookie_source(platform)
        configured_source = (
            auth_service.current_source.value
            if auth_service.current_source != AuthSourceType.NONE
            else None
        )

        cookie_path = self.get_cookie_path_for_platform(platform)
        exists = cookie_path.exists()

        # 检测来源不匹配
        source_mismatch = False
        if exists and actual_source and configured_source:
            # WebView2 多账号时 actual_source 格式为 "webview2:<account_id>"
            # 归一化后与 configured_source "webview2" 比较
            normalized_actual = (
                actual_source.split(":")[0] if ":" in actual_source else actual_source
            )
            source_mismatch = normalized_actual != configured_source

        # 实时读取 Cookie 文件，获取真实数量和有效性
        cookie_count = 0
        cookie_valid = False
        cookie_valid_msg = "未读取"

        if exists:
            try:
                cookie_path.read_text(encoding="utf-8", errors="replace")
                # 更新 auth_service 的 last_status（使状态保持同步）
                auth_service._update_status_from_file(str(cookie_path), platform)
                cookie_count = auth_service.last_status.cookie_count
                cookie_valid = auth_service.last_status.valid
                cookie_valid_msg = auth_service.last_status.message
            except Exception as e:
                logger.debug(f"[CookieSentinel] 读取Cookie文件失败 ({platform}): {e}")

        return {
            "exists": exists,
            "age_minutes": self.get_age_minutes(platform),
            "is_stale": self.get_is_stale(platform),
            "path": str(cookie_path),
            "source": auth_service.current_source_display,  # 配置的来源（显示名）
            "source_id": configured_source,  # 配置的来源 ID
            "actual_source": actual_source,  # Cookie 文件实际来源
            "actual_source_display": self._get_source_display(actual_source)
            if actual_source
            else None,
            "source_mismatch": source_mismatch,  # 是否来源不匹配
            "using_fallback": self._using_fallback,  # 是否正在使用回退
            "fallback_warning": self._fallback_warning,  # 回退警告信息
            "cookie_count": cookie_count,  # 实时计数
            "cookie_valid": cookie_valid,  # 是否包含必要 Cookie
            "cookie_valid_msg": cookie_valid_msg,  # 有效性说明
            "last_updated": self._last_update.isoformat() if self._last_update else None,
            "expiring_soon": self.is_expiring_soon(platform),  # 即将过期 (<1h)
            "earliest_expiry": self.get_earliest_expiry(platform),  # 最早过期剩余秒数
        }

    def _get_source_display(self, source_id: str | None) -> str:
        """获取来源的显示名称"""
        if not source_id:
            return "未知"
        display_names = {
            "edge": QCoreApplication.translate("CookieSentinel", "Edge"),
            "chrome": QCoreApplication.translate("CookieSentinel", "Chrome"),
            "chromium": QCoreApplication.translate("CookieSentinel", "Chromium"),
            "brave": QCoreApplication.translate("CookieSentinel", "Brave"),
            "opera": QCoreApplication.translate("CookieSentinel", "Opera"),
            "opera_gx": QCoreApplication.translate("CookieSentinel", "Opera GX"),
            "vivaldi": QCoreApplication.translate("CookieSentinel", "Vivaldi"),
            "arc": QCoreApplication.translate("CookieSentinel", "Arc"),
            "firefox": QCoreApplication.translate("CookieSentinel", "Firefox"),
            "librewolf": QCoreApplication.translate("CookieSentinel", "LibreWolf"),
            "webview2": QCoreApplication.translate("CookieSentinel", "登录获取 (WebView2)"),
            "file": QCoreApplication.translate("CookieSentinel", "手动导入"),
        }

        if source_id.startswith("webview2:"):
            account_id = source_id.split(":", 1)[1]
            account = auth_service.current_webview2_account
            if account and account.account_id == account_id:
                return QCoreApplication.translate(
                    "CookieSentinel", "登录获取 (WebView2 - {})"
                ).format(account.localized_name)
            return QCoreApplication.translate("CookieSentinel", "登录获取 (WebView2 - {})").format(
                account_id[:8]
            )

        return display_names.get(source_id, source_id)

    # ==================== 内部方法 ====================

    def _update_from_browser(
        self, silent: bool = False, force: bool = False, platform: str | None = None
    ) -> bool:
        """
        从浏览器更新支持平台 (YouTube, X) 的 Cookie

        Args:
            silent: 静默模式（失败不抛出异常）
            force: 强制刷新（允许 UAC）
            platform: 指定更新的平台（None 则更新所有支持的平台）

        Returns:
            更新是否成功 (只要任一平台成功即为 True)
        """
        success_any = False
        platforms = [platform] if platform else ["youtube", "twitter"]
        for plat in platforms:
            try:
                # 通过 AuthService 获取 Cookie 文件
                # force=True 时会触发 UAC（如果需要）
                auth_cookie_file = auth_service.get_cookie_file_for_ytdlp(
                    platform=plat, force_refresh=force
                )

                if auth_cookie_file and Path(auth_cookie_file).exists():
                    import shutil

                    dest_path = self.get_cookie_path_for_platform(plat)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(auth_cookie_file, dest_path)

                    from fluentytdl.auth.auth_service import AuthSourceType

                    source_id = auth_service.current_source.value
                    if auth_service.current_source == AuthSourceType.WEBVIEW2:
                        account = auth_service.get_current_webview2_account(platform=plat)
                        source_id = f"webview2:{account.account_id}" if account else "webview2"

                    self._save_meta(source_id, auth_service.last_status.cookie_count, plat)
                    success_any = True
                    logger.info(f"[CookieSentinel] {plat} Cookie 已更新: {dest_path}")
            except Exception as e:
                if silent:
                    logger.debug(f"[CookieSentinel] {plat} 静默更新失败: {e}")
                else:
                    logger.warning(f"[CookieSentinel] {plat} 更新失败: {e}")

        if success_any:
            self._last_update = datetime.now()
        elif not silent:
            raise RuntimeError("AuthService 未能生成任何有效的 Cookie 文件")

        return success_any

    def _copy_from_auth_service(self) -> bool:
        """
        从 AuthService 当前文件复制到各个平台的 cookies.txt

        Returns:
            复制是否成功 (只要任一平台成功即为 True)
        """
        success_any = False
        platforms = ["youtube", "twitter"]
        for platform in platforms:
            try:
                auth_cookie_file = auth_service.get_cookie_file_for_ytdlp(platform=platform)
                if auth_cookie_file and Path(auth_cookie_file).exists():
                    import shutil

                    dest_path = self.get_cookie_path_for_platform(platform)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(auth_cookie_file), dest_path)

                    self._save_meta("file", auth_service.last_status.cookie_count, platform)
                    success_any = True
                    logger.info(
                        f"[CookieSentinel] 已从 AuthService 复制 {platform} Cookie: {dest_path}"
                    )
            except Exception as e:
                logger.error(f"[CookieSentinel] 复制 {platform} 失败: {e}")

        if success_any:
            self._last_update = datetime.now()

        return success_any


# 全局单例
cookie_sentinel = CookieSentinel()
