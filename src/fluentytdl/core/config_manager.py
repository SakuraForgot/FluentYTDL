from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from ..models.subtitle_config import SubtitleConfig
from ..utils.paths import _migrate_file, config_path, legacy_config_path, old_user_data_dir


class ConfigManager(QObject):
    """配置管理单例（JSON 持久化）。"""

    configChanged = Signal(str, object)

    _instance: ConfigManager | None = None

    DEFAULT_CONFIG: dict[str, Any] = {
        "download_dir": str(Path.home() / "Downloads" / "FluentYTDL"),
        "app_language": "auto",  # auto / zh_CN / en_US
        "ffmpeg_path": "",  # 空代表自动检测
        # Proxy mode:
        # - off: do NOT use system/ambient proxy
        # - system: follow system/ambient proxy settings
        # - http: manual HTTP proxy (proxy_url is host:port or URL)
        # - socks5: manual SOCKS5 proxy (proxy_url is host:port or URL)
        "proxy_mode": "system",  # off / system / http / socks5
        # Backward-compat key (older versions used a switch + url)
        "proxy_enabled": False,
        "proxy_url": "127.0.0.1:7890",
        # Cookie mode:
        # - auto: 自动同步模式 (启动时/报错时自动从浏览器提取)
        # - browser: 运行时直接读取浏览器 (旧模式, 可能遇到文件锁)
        # - file: 手动导入 Netscape 格式文件
        "cookie_mode": "auto",  # auto / browser / file
        "cookie_browser": "edge",  # chrome / edge / firefox
        "cookie_file": "",
        # 自动同步相关配置
        "cookie_auto_sync_enabled": True,  # 是否启用启动时自动同步
        "cookie_last_sync_time": 0,  # 上次同步时间戳 (Unix timestamp)
        "cookie_managed_path": "",  # 托管文件路径 (自动生成, 用户无需配置)
        # YouTube PO Token (optional). See yt-dlp wiki: PO-Token-Guide
        # Example value: "mweb.gvs+<TOKEN>" or "mweb.gvs+<TOKEN>,mweb.player+<TOKEN>"
        "youtube_po_token": "",
        # POT Provider (bgutil-ytdlp-pot-provider) 自动 PO Token 服务
        "pot_provider_enabled": False,  # 启用内置 POT 服务
        # yt-dlp YouTube EJS/JS runtime (yt-dlp issue #15012)
        # auto: prefer deno if available (default), else try node/bun/quickjs
        "js_runtime": "auto",  # auto / deno / node / bun / quickjs
        "js_runtime_path": "",  # optional absolute path to runtime executable
        # Optional yt-dlp.exe override path
        # Empty means auto (prefer bundled _internal/yt-dlp/yt-dlp.exe, else PATH)
        "yt_dlp_exe_path": "",
        "max_concurrent_downloads": 3,
        # UI/behavior
        # Whether to auto-detect YouTube URLs from clipboard.
        "clipboard_auto_detect": False,
        # Download list behavior
        # Deletion Policy:
        # - KeepFiles: Only remove task from list, keep all files.
        # - DeleteFiles: Silently delete source/cache files based on task status.
        # - AlwaysAsk: Prompt user every time (Legacy behavior).
        "deletion_policy": "AlwaysAsk",
        # Legacy keys (kept for migration or fallback, but effectively deprecated by deletion_policy)
        "remove_task_ask_delete_source": False,
        "remove_task_ask_delete_cache": False,
        "remove_task_ask_enable_feature": True,
        # yt-dlp / playlist parsing
        # yt-dlp sometimes refuses to enumerate some public playlists unless you skip authcheck.
        # Official suggestion: --extractor-args youtubetab:skip=authcheck
        # Default OFF to avoid surprising behavior changes.
        "playlist_skip_authcheck": False,
        "playlist_extract_concurrency": 2,
        # 解析结果的进程内 TTL 缓存（不落盘）。覆盖弹窗解析、播放列表 flat 列举、
        # 频道标签页，以及播放列表逐条深解析；下载路径永不读取
        # （必须拿新鲜签名 URL，否则 403）。
        # 设置页「解析结果保留时间」直接写 parse_cache_ttl_seconds，0 表示不保留。
        "parse_cache_enabled": True,
        "parse_cache_ttl_seconds": 1800,
        # 一次性迁移标记：旧版本把保留时间写死成 300 且无 UI 入口，见 _load()。
        "parse_cache_ttl_migrated": True,
        # 播放列表逐条深解析（entry_detail）的独立容量。它与弹窗结果分桶存放，
        # 调大只会占更多内存，不会挤掉弹窗/列表缓存。
        "parse_cache_entry_max": 64,
        # Dependency update source
        # github: official github api/releases
        # ghproxy: use ghproxy mirror
        "update_source": "github",
        # Whether to check for component updates (yt-dlp, ffmpeg, etc.) on startup
        # If true, checks once every 24 hours.
        "check_updates_on_startup": True,
        # Timestamp of last automatic update check
        "last_update_check": 0,
        # Whether the user has seen the welcome guide (wizard)
        "has_shown_welcome_guide": False,
        # Version when user last saw the welcome guide (for version-aware re-trigger)
        "welcome_guide_shown_for_version": "",
        # 独立封面下载设置
        "download_thumbnail": False,
        # 封面嵌入设置
        # embed_thumbnail: 是否启用封面嵌入功能（全局开关）
        "embed_thumbnail": False,
        # embed_metadata: 是否嵌入元数据（标题、艺术家等）
        "embed_metadata": True,
        # SponsorBlock 设置
        # sponsorblock_enabled: 是否启用 SponsorBlock 广告跳过功能
        "sponsorblock_enabled": False,  # 默认关闭（避免意外修改视频）
        # sponsorblock_categories: 要处理的类别列表
        # 可选类别: sponsor, selfpromo, interaction, intro, outro, preview, music_offtopic, poi_highlight, filler
        "sponsorblock_categories": ["sponsor", "selfpromo", "interaction"],
        # sponsorblock_action: 处理动作 - remove: 移除片段, mark: 仅标记为章节
        "sponsorblock_action": "remove",
        # 字幕配置
        "subtitle_enabled": False,  # 是否启用字幕下载（全局开关）
        "subtitle_type_preference": "manual_and_asr",  # 字幕类型偏好
        "subtitle_default_languages": ["zh-Hans", "en"],  # 默认字幕语言优先级
        "subtitle_enable_auto_captions": True,  # 是否启用自动生成字幕
        "subtitle_embed_type": "soft",  # 嵌入类型: soft/external
        "subtitle_embed_mode": "always",  # 嵌入模式: always/never/ask
        "subtitle_output_format": "vtt",  # 字幕输出格式：srt/ass/vtt/lrc
        "subtitle_quality_check": True,  # 是否启用字幕质量检查
        "subtitle_remove_ads": False,  # 是否自动移除字幕广告
        "subtitle_fallback_to_english": True,  # 是否回退到英语
        "subtitle_max_languages": 2,  # 最多下载字幕数量
        # 音频偏好设置
        # preferred_audio_languages: 首选音轨语言（多选优先级，对于多音轨视频）
        # 'orig': 优先原音/默认, 'zh-Hans': 中文, 'en': 英文, 'ja': 日语等
        "preferred_audio_languages": ["zh-Hans", "en", "orig"],
        # audio_multistream_default_count: 多音轨视频默认选择几条音轨（0表示无限制，1表示仅最佳）
        "audio_multistream_default_count": 1,
        # 认证模式
        "auth_mode": "oauth2",  # 或者 "cookie"
        "oauth2_status": False,  # 是否已经成功完成过 oauth2 授权
        # === 格式与容器记忆 ===
        "single_container_override": "自动推断",
        "single_audio_override": "自动推断",
        "playlist_container_override": "自动推断",
        "playlist_audio_override": "自动推断",
        # === 下载分片与并发 ===
        "concurrent_fragments": 4,  # [新增] 全局分片并发数控制参数
        "network_retries": 10,  # [新增] 网络请求与切片下载的最大重试次数
        # Theme Mode
        "theme_mode": "Auto",  # Light / Dark / Auto
        # 更新通道跳过版本（按通道分别存储）
        "skipped_stable_version": "",
        "skipped_pre_version": "",
        # === 快速模式 ===
        "quick_mode_initialized": False,
        "quick_playlist_strategy": "auto",
        "quick_playlist_expand_threshold": 50,
        "quick_max_total_tasks": 500,
        "quick_embed_metadata": None,
        "quick_embed_thumbnail": None,
        "quick_download_thumbnail": None,
        "quick_subtitle_enabled": None,
        "quick_subtitle_languages": None,
        "quick_sponsorblock_enabled": None,
        "quick_download_type": "video_audio",
        "quick_video_quality": "best_mp4",
        "quick_container": "自动推断",
        "quick_audio_format": "自动推断",
        # === 补充缺失的活跃配置（防打包失效） ===
        "audio_default_preset": "mp3_320",
        "audio_normalize": False,
        "audio_target_lra": 11,
        "audio_target_lufs": -14,
        "audio_target_tp": -1,
        "clipboard_action_mode": "smart",
        "cookie_cleaning_enabled": True,
        "enable_resume": True,
        "quality_guard_ffprobe": False,
        "quality_guard_mode": "warn",
        "quick_download_dir": "",
        "rate_limit": "",
        "recent_target_url": "",
        "vr_eac_auto_convert": False,
        "ytdlp_channel": "stable",
        # === 补充第二批正则挖掘出的活跃配置 ===
        "clipboard_window_to_front": True,
        "failed_task_retention_days": 3,
        "quality_guard_suspend_threshold": 3,
        "vr_cpu_priority": "low",
        "vr_hw_accel_mode": "auto",
        "vr_keep_source": True,
        "vr_max_resolution": 2160,
    }

    def __new__(cls) -> ConfigManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        super().__init__()
        self._initialized = True
        self._init()

    def _init(self) -> None:
        # Dev: repo root config.json; Frozen: user-writable Documents/FluentYTDL/config.json
        self.config_file = config_path()
        self.config: dict[str, Any] = self._load_config()

        # 启动时进行一次配置瘦身：剔除不再使用的僵尸字段
        obsolete_keys = [k for k in self.config if k not in self.DEFAULT_CONFIG]
        if obsolete_keys:
            for k in obsolete_keys:
                del self.config[k]
            self.save()

    def _load_config(self) -> dict[str, Any]:
        # One-time migration: old Documents location -> new location
        old_docs_config = old_user_data_dir() / "config.json"
        _migrate_file(old_docs_config, self.config_file)

        # Backward-compat: if new location doesn't exist but legacy exists, load legacy.
        candidates = [self.config_file]
        legacy = legacy_config_path()
        if legacy != self.config_file:
            candidates.append(legacy)

        existing = next((p for p in candidates if p.exists()), None)
        if existing is None:
            return self.DEFAULT_CONFIG.copy()

        # Migration: legacy -> new
        if existing == legacy and legacy != self.config_file:
            try:
                self.config_file.parent.mkdir(parents=True, exist_ok=True)
                self.config_file.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
                existing = self.config_file
            except Exception:
                # If migration fails, continue using legacy in this session.
                existing = legacy

        try:
            data = json.loads(existing.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self.DEFAULT_CONFIG.copy()
            # 合并默认配置，防止新版本缺字段
            merged = {**self.DEFAULT_CONFIG, **data}

            # Migration: proxy_enabled/proxy_url -> proxy_mode
            if "proxy_mode" not in data:
                if bool(data.get("proxy_enabled")):
                    # default to http unless the url suggests socks5
                    proxy_url = str(data.get("proxy_url") or "").lower()
                    merged["proxy_mode"] = "socks5" if "socks" in proxy_url else "http"
                else:
                    merged["proxy_mode"] = "off"

            # Legacy value mapping
            if str(merged.get("proxy_mode") or "").lower().strip() == "custom":
                proxy_url = str(merged.get("proxy_url") or "").lower()
                merged["proxy_mode"] = "socks5" if "socks" in proxy_url else "http"

            # Normalize
            pm = str(merged.get("proxy_mode") or "off").lower().strip()
            if pm not in {"off", "system", "http", "socks5"}:
                pm = "off"
            merged["proxy_mode"] = pm

            # Migration: parse cache retention default 5 min -> 30 min.
            self._migrate_parse_cache_ttl(data, merged)

            # Normalize tool paths: if a user keeps an old absolute path that no longer
            # exists (common after packaging/moving folders), fall back to auto-detect.
            for key in ("ffmpeg_path", "yt_dlp_exe_path", "js_runtime_path"):
                try:
                    raw = str(merged.get(key) or "").strip()
                    if raw and not Path(raw).exists():
                        merged[key] = ""
                except Exception:
                    merged[key] = ""

            return merged
        except Exception:
            return self.DEFAULT_CONFIG.copy()

    @staticmethod
    def _migrate_parse_cache_ttl(data: dict[str, Any], merged: dict[str, Any]) -> None:
        """解析结果保留时间：旧默认 300s（5 分钟）抬到 1800s（半小时）。

        `save()` 写的是整份合并后的配置，所以任何存过盘的旧安装都带着 300。而这个键
        在设置页「解析结果保留时间」上线之前**没有任何 UI 入口**，磁盘上的 300 只可能
        是旧默认值，不可能是用户的选择，抬上去不会覆盖任何人的决定。

        标记位保证只跑一次：设置页上线后用户真挑了「5 分钟」，下次启动不能被悄悄改回
        半小时。比较刻意用 `== 300` 而不是 `int(...)`——`_load()` 整段外面套着 except，
        这里抛一次异常会把用户的整份配置回退成默认值，代价远大于收益。
        """
        if data.get("parse_cache_ttl_migrated"):
            return
        if data.get("parse_cache_ttl_seconds") == 300:
            merged["parse_cache_ttl_seconds"] = 1800
        merged["parse_cache_ttl_migrated"] = True

    def save(self) -> None:
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            self.config_file.write_text(
                json.dumps(self.config, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            # Avoid crashing UI if disk is read-only / permission issues.
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.config[key] = value
        self.save()
        self.configChanged.emit(key, value)

    def get_subtitle_config(self) -> SubtitleConfig:
        """获取字幕配置对象"""
        from ..models.subtitle_config import SubtitleTypePreference

        return SubtitleConfig(
            enabled=self.config.get("subtitle_enabled", False),
            type_preference=SubtitleTypePreference(
                self.config.get("subtitle_type_preference", "manual_and_asr")
            ),
            default_languages=self.config.get("subtitle_default_languages", ["zh-Hans", "en"]),
            enable_auto_captions=self.config.get("subtitle_enable_auto_captions", True),
            embed_type=self.config.get("subtitle_embed_type", "soft"),
            embed_mode=self.config.get("subtitle_embed_mode", "always"),
            output_format=self.config.get("subtitle_output_format", "vtt"),
            quality_check=self.config.get("subtitle_quality_check", True),
            remove_ads=self.config.get("subtitle_remove_ads", False),
            fallback_to_english=self.config.get("subtitle_fallback_to_english", True),
            max_languages=self.config.get("subtitle_max_languages", 2),
        )

    def set_subtitle_config(self, config: SubtitleConfig) -> None:
        """设置字幕配置并保存"""
        self.config["subtitle_enabled"] = config.enabled
        self.config["subtitle_type_preference"] = config.type_preference.value
        self.config["subtitle_default_languages"] = config.default_languages
        self.config["subtitle_enable_auto_captions"] = config.enable_auto_captions
        self.config["subtitle_embed_type"] = config.embed_type
        self.config["subtitle_embed_mode"] = config.embed_mode
        self.config["subtitle_output_format"] = config.output_format
        self.config["subtitle_quality_check"] = config.quality_check
        self.config["subtitle_remove_ads"] = config.remove_ads
        self.config["subtitle_fallback_to_english"] = config.fallback_to_english
        self.config["subtitle_max_languages"] = config.max_languages
        self.save()

    def init_quick_mode_defaults(self) -> None:
        """首次使用快速模式时，从全局设置初始化偏好。"""
        if self.get("quick_mode_initialized"):
            return

        mappings = {
            "quick_embed_metadata": "embed_metadata",
            "quick_embed_thumbnail": "embed_thumbnail",
            "quick_download_thumbnail": "download_thumbnail",
            "quick_subtitle_enabled": "subtitle_enabled",
            "quick_subtitle_languages": "subtitle_default_languages",
            "quick_sponsorblock_enabled": "sponsorblock_enabled",
            "quick_container": "single_container_override",
            "quick_audio_format": "single_audio_override",
        }

        for quick_key, global_key in mappings.items():
            if self.get(quick_key) is None:
                self.set(quick_key, self.get(global_key))

        self.set("quick_mode_initialized", True)


config_manager = ConfigManager()
