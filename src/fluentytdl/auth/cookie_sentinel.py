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
import os
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Signal

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..utils.logger import logger
from .auth_service import BROWSER_COMBO_ITEMS, AuthSourceType, auth_service


class CookieSentinel(QObject):
    """
    Cookie 卫士 - 统一 Cookie 生命周期管理

    核心职责：
    1. 维护两个真相源 bin/cookies_youtube.txt / bin/cookies_twitter.txt
    2. 启动时静默尝试更新（Best-Effort），完成后发出启动健康度
    3. 提供错误检测与修复接口

    继承 QObject 只为承载 startupHealthReady —— 服务层不能 import ui，
    健康度只能以信号的形式交给 UI 层（CLAUDE.md §2）。
    """

    # 启动静默刷新收尾时发出 get_startup_health() 的结果。
    # 发出方是后台线程，UI 侧必须用 Qt.ConnectionType.QueuedConnection 连接。
    startupHealthReady = Signal(dict)

    # 启动提醒阈值：只有 24 小时内就要过期才值得打扰用户
    EXPIRY_WARNING_MINUTES = 60 * 24

    _initialized = False
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
        if self._initialized:
            return

        super().__init__()
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
                log_text(
                    logger,
                    "info",
                    "[CookieSentinel] 已迁移旧版 Cookie 文件: {0} -> {1}",
                    old_cookie_path.name,
                    new_cookie_path.name,
                )
            except Exception as e:
                log_text(logger, "warning", "[CookieSentinel] 迁移旧版 Cookie 文件失败: {0}", e)

        if old_meta_path.exists() and not new_meta_path.exists():
            import shutil

            try:
                shutil.move(str(old_meta_path), str(new_meta_path))
                log_text(
                    logger,
                    "info",
                    "[CookieSentinel] 已迁移旧版 Cookie 元数据: {0} -> {1}",
                    old_meta_path.name,
                    new_meta_path.name,
                )
            except Exception as e:
                log_text(logger, "warning", "[CookieSentinel] 迁移旧版 Cookie 元数据失败: {0}", e)

        # 设置兼容属性（默认指向 youtube）
        self.cookie_path = new_cookie_path
        self.meta_path = new_meta_path

        # 状态追踪
        self._last_update: datetime | None = None
        # 按平台的刷新占用（同平台并发才拒绝，youtube 刷新不再阻塞 twitter）
        self._updating: set[str] = set()
        self._update_lock = threading.Lock()

        # 回退状态追踪（当提取失败但有旧 Cookie 可用时）
        self._using_fallback = False
        self._fallback_warning: str | None = None

        # 真相源写入闸门的失败原因（按平台），供设置页状态卡标红显示
        self._commit_warnings: dict[str, str] = {}

        log_text(logger, "info", "Cookie Sentinel 初始化: {0}", self.cookie_path)

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
            log_text(logger, "warning", "[CookieSentinel] 读取元数据失败 ({0}): {1}", platform, e)
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
            log_text(
                logger,
                "debug",
                "[CookieSentinel] 元数据已保存 ({0}): {1}, {2} cookies",
                platform,
                source,
                cookie_count,
            )
        except Exception as e:
            log_text(logger, "warning", "[CookieSentinel] 保存元数据失败 ({0}): {1}", platform, e)

    def _clear_cookie_and_meta(self, platform: str = "youtube") -> None:
        """清除 Cookie 文件和元数据"""
        try:
            cookie_path = self.get_cookie_path_for_platform(platform)
            meta_path = self.get_meta_path_for_platform(platform)
            if cookie_path.exists():
                cookie_path.unlink()
                log_text(logger, "info", "[CookieSentinel] 已删除旧 Cookie 文件: {0}", cookie_path)
            if meta_path.exists():
                meta_path.unlink()
                log_text(logger, "info", "[CookieSentinel] 已删除旧元数据文件: {0}", meta_path)
        except Exception as e:
            log_text(logger, "warning", "[CookieSentinel] 清除文件失败 ({0}): {1}", platform, e)

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
            log_text(logger, "debug", "[CookieSentinel] Cookie 文件缺少来源元数据")
            return False, None

        # WebView2 多账号场景下，source 可能写成 webview2:<account_id>，此时视为与 dle 一致
        normalized_actual = actual_source
        if isinstance(actual_source, str) and actual_source.startswith("webview2:"):
            normalized_actual = "webview2"

        if normalized_actual != expected_source:
            log_text(
                logger,
                "debug",
                "[CookieSentinel] Cookie 来源不匹配: 现有={0}, 期望={1}",
                actual_source,
                expected_source,
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

    # ==================== 真相源写入闸门 ====================

    def _commit_to_truth_source(
        self, src: Path | str, platform: str, source_tag: str
    ) -> tuple[bool, str]:
        """
        校验 → 原子落盘。这是写入两个真相源的唯一入口。

        弱回退语义：新 Cookie 的平台必需字段不齐全时，目的地文件纹丝不动，旧 Cookie
        继续为 yt-dlp 服务，失败原因记入 self._commit_warnings[platform]。
        "提取成功但内容是半份/已过期" 与 "提取整体失败" 在这里被同等对待。

        Args:
            src: 新 Cookie 的来源文件（WebView2 缓存 / 浏览器提取产物 / 手动导入文件）
            platform: 目标平台（youtube / twitter）
            source_tag: 写入元数据的来源标识（如 "edge" / "file" / "webview2:<account_id>"）

        Returns:
            (是否已覆盖真相源, 原因说明)
        """
        src_path = Path(src)
        dest = self.get_cookie_path_for_platform(platform)

        if not src_path.exists():
            return self._reject_commit(platform, tr_text("来源文件不存在: {0}", src_path.name))

        try:
            content = src_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return self._reject_commit(platform, tr_text("读取来源文件失败: {0}", e))

        cookies = auth_service._parse_netscape_cookies(content)
        if not cookies:
            return self._reject_commit(
                platform, tr_text("未解析出任何 Cookie（可能不是 Netscape 格式）")
            )

        # 必需 Cookie 齐全才允许覆盖真相源
        validation = auth_service._validate_cookies(cookies, platform)
        if not validation.get("valid"):
            return self._reject_commit(
                platform, validation.get("message") or tr_text("Cookie 校验未通过")
            )

        # 原子落盘：同卷 os.replace，与 utils/paths.py::_install_item() 同一手法
        if src_path.resolve() != dest.resolve():
            tmp = dest.with_suffix(".txt.tmp")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(src_path.read_bytes())
                os.replace(tmp, dest)
            except OSError as e:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return self._reject_commit(platform, tr_text("写入真相源失败: {0}", e))

        self._save_meta(source_tag, len(cookies), platform)
        self._commit_warnings.pop(platform, None)
        auth_service._update_status_from_file(str(dest), platform)
        self._last_update = datetime.now()
        log_text(
            logger,
            "info",
            "[CookieSentinel] {0} 真相源已更新: {1}（{2} 个 Cookie，来源 {3}）",
            platform,
            dest.name,
            len(cookies),
            source_tag,
        )
        return True, validation.get("message") or tr_text("已更新")

    def _reject_commit(self, platform: str, reason: str) -> tuple[bool, str]:
        """记录一次被闸门拒绝的写入（目的地保持不变），返回 (False, reason)"""
        self._commit_warnings[platform] = reason
        if self.get_cookie_path_for_platform(platform).exists():
            log_text(
                logger,
                "warning",
                "[CookieSentinel] {0} 新 Cookie 不可用，已保留旧真相源: {1}",
                platform,
                reason,
            )
        else:
            log_text(
                logger,
                "warning",
                "[CookieSentinel] {0} 新 Cookie 不可用且无旧文件可回退: {1}",
                platform,
                reason,
            )
        return False, reason

    def get_commit_warning(self, platform: str = "youtube") -> str | None:
        """获取该平台最近一次真相源写入被拒的原因（无失败则返回 None）"""
        return self._commit_warnings.get(platform)

    # ==================== 启动健康度 ====================

    def get_startup_health(self) -> dict[str, dict]:
        """
        聚合两个真相源的启动健康度，供 UI 做分级提醒。

        Returns:
            {platform: {"enabled", "exists", "valid", "expiring_soon",
                        "expiry_seconds", "reason", "commit_warning"}}
        """
        return {
            platform: self._get_platform_health(platform) for platform in ("youtube", "twitter")
        }

    def _get_platform_health(self, platform: str) -> dict:
        """单个平台的健康度快照（不做任何提取，只读现有真相源）"""
        cookie_path = self.get_cookie_path_for_platform(platform)
        exists = cookie_path.exists()

        valid = False
        reason = ""

        if not exists:
            reason = tr_text("尚无 Cookie 文件")
        else:
            try:
                content = cookie_path.read_text(encoding="utf-8", errors="replace")
                cookies = auth_service._parse_netscape_cookies(content)
                validation = auth_service._validate_cookies(cookies, platform)
                valid = bool(validation.get("valid"))
                reason = validation.get("message") or ""
            except OSError as e:
                reason = QCoreApplication.translate(
                    "CookieSentinel", "读取 Cookie 文件失败: {}"
                ).format(e)

        return {
            "enabled": self._is_platform_enabled(platform),
            "exists": exists,
            "valid": valid,
            "expiring_soon": self.is_expiring_soon(platform, self.EXPIRY_WARNING_MINUTES),
            "expiry_seconds": self.get_earliest_expiry(platform),
            "reason": reason,
            # 闸门最近一次拒绝的原因："新 Cookie 不可用，仍在用旧文件"
            "commit_warning": self.get_commit_warning(platform),
        }

    def _is_platform_enabled(self, platform: str) -> bool:
        """
        该平台是否被用户实际使用 —— 决定启动时要不要为它发提醒。

        WebView2 模式下账号列表就是用户的意图声明：没有该平台账号就不提醒。
        "只用 X 的用户每次启动都被 YouTube 的缺失状态误伤" 正是这里治的病。
        其余模式退回到"该平台真相源（或其元数据）是否曾经存在"。
        """
        try:
            if auth_service.current_source == AuthSourceType.WEBVIEW2:
                return bool(auth_service.list_webview2_accounts(platform))
        except Exception as e:
            log_text(
                logger, "debug", "[CookieSentinel] 读取 {0} WebView2 账号失败: {1}", platform, e
            )

        return (
            self.get_cookie_path_for_platform(platform).exists()
            or self.get_meta_path_for_platform(platform).exists()
        )

    def _emit_startup_health(self) -> None:
        """启动刷新收尾：把健康度交给 UI 层（best-effort，绝不影响启动）"""
        try:
            health = self.get_startup_health()
            log_text(
                logger,
                "info",
                "[CookieSentinel] 启动健康度: {0}",
                "; ".join(
                    f"{p}(enabled={h['enabled']} exists={h['exists']} valid={h['valid']}"
                    f" expiring={h['expiring_soon']})"
                    for p, h in health.items()
                ),
            )
            self.startupHealthReady.emit(health)
        except Exception as e:
            log_text(logger, "warning", "[CookieSentinel] 发出启动健康度失败: {0}", e)

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
                log_text(logger, "info", "[CookieSentinel] 启动时静默刷新开始...")

                # 重置回退状态
                self._using_fallback = False
                self._fallback_warning = None

                # 检查 AuthService 当前配置
                current_source = auth_service.current_source

                if current_source == AuthSourceType.NONE:
                    log_text(logger, "info", "[CookieSentinel] 未启用验证源，跳过静默刷新")
                    return

                # WebView2 模式是交互式流程（需用户登录），不能在启动时自动触发
                if current_source == AuthSourceType.WEBVIEW2:
                    for plat in ("youtube", "twitter"):
                        cache_file = auth_service.get_cookie_file_for_ytdlp(
                            platform=plat, force_refresh=False
                        )
                        if not (cache_file and Path(cache_file).exists()):
                            # 该平台用户从未登录过，这不是错误
                            log_text(
                                logger,
                                "debug",
                                "[CookieSentinel] {0} 尚无 WebView2 登录态，跳过同步",
                                plat,
                            )
                            continue

                        account = auth_service.get_current_webview2_account(platform=plat)
                        source_tag = (
                            f"webview2:{account.account_id}"
                            if account and account.account_id
                            else "webview2"
                        )
                        ok, reason = self._commit_to_truth_source(cache_file, plat, source_tag)
                        if ok:
                            log_text(
                                logger,
                                "info",
                                "[CookieSentinel] WebView2 {0} Cookie 同步完成",
                                plat,
                            )
                        else:
                            log_text(
                                logger,
                                "info",
                                "[CookieSentinel] WebView2 {0} Cookie 未同步（沿用旧真相源）: {1}",
                                plat,
                                reason,
                            )

                    return

                # 获取期望的来源标识
                expected_source = current_source.value  # 如 "edge", "firefox", "file"

                # 检查来源一致性（只检查，不清理）
                is_consistent, actual_source = self.validate_source_consistency(expected_source)

                if current_source == AuthSourceType.FILE:
                    # 手动导入文件：逐平台过写入闸门（元数据与状态由闸门写入）
                    if self._copy_from_auth_service():
                        log_text(logger, "info", "[CookieSentinel] 已同步手动导入的 Cookie 文件")
                    else:
                        log_text(
                            logger,
                            "warning",
                            "[CookieSentinel] 手动导入的 Cookie 文件不可用，沿用旧真相源",
                        )
                    return

                # 浏览器来源：尝试提取
                success = self._update_from_browser(silent=True)

                if success:
                    # 提取成功，元数据已在 _update_from_browser 中保存
                    self._using_fallback = False
                    self._fallback_warning = None
                    log_text(
                        logger,
                        "info",
                        "[CookieSentinel] 启动时静默刷新成功：{0}",
                        auth_service.current_source_display,
                    )
                    log_text(
                        logger,
                        "info",
                        "[CookieSentinel] 提取了 {0} 个 Cookie",
                        auth_service.last_status.cookie_count,
                    )
                else:
                    # 提取失败，检查是否有旧 Cookie 可用作回退
                    if self.exists and actual_source:
                        # 有旧 Cookie，标记为回退状态
                        self._using_fallback = True
                        self._fallback_warning = tr_text(
                            "配置为 {0}，但提取失败，当前使用 {1} 的 Cookie",
                            auth_service.current_source_display,
                            self._get_source_display(actual_source),
                        )
                        logger.warning(f"[CookieSentinel] {self._fallback_warning}")
                        # 验证回退 Cookie 的有效性，供 UI 层 check_cookie_status 使用
                        auth_service._update_status_from_file(str(self.cookie_path), "youtube")
                    else:
                        log_text(
                            logger,
                            "warning",
                            "[CookieSentinel] 启动时静默刷新失败: {0}",
                            auth_service.last_status.message,
                        )
                    log_text(logger, "info", "[CookieSentinel] 用户可在设置页点击'手动刷新'重试")

            except Exception as e:
                # 静默失败，不影响启动
                log_text(
                    logger, "warning", "[CookieSentinel] 启动时静默刷新异常（预期行为）: {0}", e
                )

            finally:
                # 无论走哪条分支（含未配置验证源、含异常）都恰好发出一次健康度，
                # UI 层的启动提醒完全由这个信号驱动，不再靠 QTimer 猜时序。
                self._emit_startup_health()

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
        targets = [platform] if platform else ["youtube", "twitter"]

        # 按平台占用：youtube 正在刷新时不再阻塞 twitter 的刷新
        with self._update_lock:
            busy = [p for p in targets if p in self._updating]
            if busy:
                return False, QCoreApplication.translate(
                    "CookieSentinel", "{} 正在更新中，请稍候..."
                ).format("、".join(busy))

            self._updating.update(targets)

        try:
            log_text(logger, "info", "[CookieSentinel] 用户触发强制刷新（允许 UAC）")

            current_source = auth_service.current_source

            if current_source == AuthSourceType.NONE:
                return False, tr_text("未配置验证源，请先在设置中选择浏览器或导入 Cookie 文件")

            # 获取当前来源状态（只检查，不清理）
            expected_source = current_source.value
            is_consistent, actual_source = self.validate_source_consistency(expected_source)

            has_fallback = self._has_any_truth_source(targets)

            if current_source == AuthSourceType.FILE:
                success = self._copy_from_auth_service()
                if success:
                    # 元数据已在写入闸门中保存
                    self._using_fallback = False
                    self._fallback_warning = None
                    return True, tr_text("已更新为手动导入的 Cookie 文件")
                else:
                    # 失败时保留旧文件
                    if has_fallback and actual_source:
                        self._using_fallback = True
                        self._fallback_warning = tr_text(
                            "导入失败，继续使用 {0} 的 Cookie",
                            self._get_source_display(actual_source),
                        )
                        return False, tr_text("导入失败（保留旧 Cookie）")
                    return False, tr_text("手动导入的 Cookie 文件不存在或无效")

            # 浏览器来源：强制刷新（允许 UAC）
            success = self._update_from_browser(silent=False, force=True, platform=platform)

            if success:
                # 提取成功，元数据已在写入闸门中保存
                self._using_fallback = False
                self._fallback_warning = None
                msg = tr_text("✅ Cookie 已更新（{}）").format(auth_service.current_source_display)
                if auth_service.last_status.cookie_count > 0:
                    msg += QCoreApplication.translate(
                        "CookieSentinel", "\n提取了 {} 个 Cookie"
                    ).format(auth_service.last_status.cookie_count)
                return True, msg
            else:
                # 提取失败或未通过写入闸门，检查是否有旧 Cookie 可用作回退
                gate_reasons = [r for r in (self.get_commit_warning(p) for p in targets) if r]
                detail = gate_reasons[0] if gate_reasons else auth_service.last_status.message

                if has_fallback and actual_source:
                    self._using_fallback = True
                    self._fallback_warning = QCoreApplication.translate(
                        "CookieSentinel", "从 {} 提取失败，继续使用 {} 的 Cookie"
                    ).format(
                        auth_service.current_source_display, self._get_source_display(actual_source)
                    )
                    return False, tr_text("更新失败: {0}\n（保留旧 Cookie 可用）", detail)
                return False, tr_text("更新失败: {0}", detail)

        except Exception as e:
            log_text(logger, "exception", "[CookieSentinel] 强制刷新异常")
            return False, tr_text("更新异常: {0}", e)

        finally:
            with self._update_lock:
                self._updating.difference_update(targets)

    def _has_any_truth_source(self, platforms: list[str]) -> bool:
        """目标平台中是否至少有一个真相源文件存在（可作为弱回退）"""
        return any(self.get_cookie_path_for_platform(p).exists() for p in platforms)

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
        cookie_valid_msg = tr_text("未读取")

        if exists:
            try:
                cookie_path.read_text(encoding="utf-8", errors="replace")
                # 更新 auth_service 的 last_status（使状态保持同步）
                auth_service._update_status_from_file(str(cookie_path), platform)
                cookie_count = auth_service.last_status.cookie_count
                cookie_valid = auth_service.last_status.valid
                cookie_valid_msg = auth_service.last_status.message
            except Exception as e:
                log_text(
                    logger, "debug", "[CookieSentinel] 读取Cookie文件失败 ({0}): {1}", platform, e
                )

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
            # 写入闸门最近一次拒绝的原因：新 Cookie 不可用，当前仍在用旧文件（弱回退）
            "commit_warning": self.get_commit_warning(platform),
        }

    def _get_source_display(self, source_id: str | None) -> str:
        """获取来源的显示名称

        `source_id` 来自 `.txt.meta` 里记下的**历史**来源，可能是本版本已经不支持的
        提取源（chrome / centbrowser），所以这里的映射表必须比 `BROWSER_COMBO_ITEMS`
        更宽 —— 用户的旧真相源还在用，界面上不该突然显示成裸 id。
        """
        if not source_id:
            return tr_text("未知")

        # 浏览器名都是专名，直接复用下拉框那份唯一列表，不再手抄第 N 份
        display_names = {source.value: label for source, label in BROWSER_COMBO_ITEMS}
        display_names.update(
            {
                # 已停止支持，但老 meta 里仍可能存着，保留展示名
                "chrome": "Google Chrome",
                "centbrowser": tr_text("百分浏览器 (Cent)"),
                "webview2": tr_text("登录获取 (WebView2)"),
                "file": tr_text("手动导入"),
            }
        )

        if source_id.startswith("webview2:"):
            account_id = source_id.split(":", 1)[1]
            account = auth_service.current_webview2_account
            if account and account.account_id == account_id:
                return QCoreApplication.translate(
                    "CookieSentinel", "登录获取 (WebView2 - {})"
                ).format(account.localized_name)
            return tr_text("登录获取 (WebView2 - {})").format(account_id[:8])

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

                if not (auth_cookie_file and Path(auth_cookie_file).exists()):
                    self._reject_commit(
                        plat,
                        tr_text(
                            "从 {0} 提取失败: {1}",
                            auth_service.current_source_display,
                            auth_service.last_status.message,
                        ),
                    )
                    continue

                source_id = auth_service.current_source.value
                if auth_service.current_source == AuthSourceType.WEBVIEW2:
                    account = auth_service.get_current_webview2_account(platform=plat)
                    source_id = f"webview2:{account.account_id}" if account else "webview2"

                # 经过写入闸门：校验不通过则旧真相源纹丝不动
                ok, _reason = self._commit_to_truth_source(auth_cookie_file, plat, source_id)
                success_any = success_any or ok
            except Exception as e:
                if silent:
                    log_text(logger, "debug", "[CookieSentinel] {0} 静默更新失败: {1}", plat, e)
                else:
                    log_text(logger, "warning", "[CookieSentinel] {0} 更新失败: {1}", plat, e)
                self._commit_warnings[plat] = tr_text("提取过程异常: {0}", e)

        return success_any

    def _copy_from_auth_service(self) -> bool:
        """
        从 AuthService 当前文件复制到各个平台的真相源

        同一份手动导入文件会分别按 youtube / twitter 的必需字段校验，
        只有校验通过的平台才会被覆盖（例如仅含 YouTube 字段时不会污染 cookies_twitter.txt）。

        Returns:
            复制是否成功 (只要任一平台成功即为 True)
        """
        success_any = False
        for platform in ("youtube", "twitter"):
            try:
                auth_cookie_file = auth_service.get_cookie_file_for_ytdlp(platform=platform)
                if not (auth_cookie_file and Path(auth_cookie_file).exists()):
                    self._reject_commit(platform, tr_text("手动导入的 Cookie 文件不存在"))
                    continue

                ok, _reason = self._commit_to_truth_source(auth_cookie_file, platform, "file")
                success_any = success_any or ok
            except Exception as e:
                log_text(logger, "error", "[CookieSentinel] 复制 {0} 失败: {1}", platform, e)
                self._commit_warnings[platform] = tr_text("导入过程异常: {0}", e)

        return success_any


# 全局单例
cookie_sentinel = CookieSentinel()
