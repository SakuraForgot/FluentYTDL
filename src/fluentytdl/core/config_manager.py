from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models.subtitle_config import SubtitleConfig
from ..utils.paths import config_path, legacy_config_path


class ConfigManager:
    """配置管理单例（JSON 持久化）。"""

    _instance: ConfigManager | None = None

    DEFAULT_CONFIG: dict[str, Any] = {
        "download_dir": str(Path.home() / "Downloads" / "FluentYTDL"),
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
        "pot_provider_enabled": True,  # 启用内置 POT 服务
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
        # 封面嵌入设置
        # embed_thumbnail: 是否启用封面嵌入功能（全局开关）
        "embed_thumbnail": True,
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
        "subtitle_default_languages": ["zh-Hans", "en"],  # 默认字幕语言优先级
        "subtitle_enable_auto_captions": True,  # 是否启用自动生成字幕
        "subtitle_embed_mode": "always",  # 嵌入模式: always/never/ask
        "subtitle_write_separate_file": False,  # 是否同时保存单独文件（嵌入模式下默认不保留）
        "subtitle_format": "srt",  # 字幕格式偏好
        "subtitle_quality_check": True,  # 是否启用字幕质量检查
        "subtitle_remove_ads": False,  # 是否自动移除字幕广告
        "subtitle_fallback_to_english": True,  # 是否回退到英语
        "subtitle_max_languages": 2,  # 最多下载字幕数量
        # 音频偏好设置
        # preferred_audio_languages: 首选音轨语言（多选优先级，对于多音轨视频）
        # 'orig': 优先原音/默认, 'zh-Hans': 中文, 'en': 英文, 'ja': 日语等
        "preferred_audio_languages": ["orig", "zh-Hans", "en"],
        # 风控探测设置
        "auto_risk_probe": False,  # 是否在后台自动探测风控状态
        "probe_link_pool": [
            "https://www.youtube.com/watch?v=543xUxalGYY",  # age-restricted
            "https://www.youtube.com/watch?v=0k5wrt_6v3c",  # age-restricted
            "https://www.youtube.com/watch?v=l_aB3_iR3XQ",  # age-restricted
            "https://www.youtube.com/watch?v=N_H0gJ15mfs",  # typical warning
            "https://www.youtube.com/watch?v=q6EoRBvdVPQ",  # vevo
            "https://www.youtube.com/watch?v=YQHsXMglC9A",  # adele
            "https://www.youtube.com/watch?v=fJ9rUzIMcZQ",  # queen
            "https://www.youtube.com/watch?v=kJQP7kiw5Fk",  # despacito
            "https://www.youtube.com/watch?v=JGwWNGJdvx8",  # ed sheeran
            "https://www.youtube.com/watch?v=RgKAFK5djSk",  # wiz khalifa
            "https://www.youtube.com/watch?v=60ItHLz5WEA",  # alan walker
            "https://www.youtube.com/watch?v=CevxZvSJLk8",  # katy perry
            "https://www.youtube.com/watch?v=V1bFr2SWP1I",  # maroon 5
            "https://www.youtube.com/watch?v=09R8_2nJtjg",  # maroon 5 sugar
            "https://www.youtube.com/watch?v=PT2_F-1esPk",  # the chainsmokers
            "https://www.youtube.com/watch?v=OPf0YbXqDm0",  # mark ronson
            "https://www.youtube.com/watch?v=7WTqi14s4Zk",  # major lazer
            "https://www.youtube.com/watch?v=uelHwf8o7_U",  # eminem
            "https://www.youtube.com/watch?v=hT_nvWreIhg",  # onerepublic
            "https://www.youtube.com/watch?v=34Na4j8HLjc",  # the weeknd
        ],
        "probe_link_history": {},  # {url: timestamp_selected}
        # Theme Mode
        "theme_mode": "Auto",  # Light / Dark / Auto
    }

    def __new__(cls) -> ConfigManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        # Dev: repo root config.json; Frozen: user-writable Documents/FluentYTDL/config.json
        self.config_file = config_path()
        self.config: dict[str, Any] = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        # Backward-compat: if new location doesn't exist but legacy exists, load legacy.
        # If we are running frozen, also migrate the legacy file into the new location.
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

    def get_subtitle_config(self) -> SubtitleConfig:
        """获取字幕配置对象"""
        return SubtitleConfig(
            enabled=self.config.get("subtitle_enabled", False),
            default_languages=self.config.get("subtitle_default_languages", ["zh-Hans", "en"]),
            enable_auto_captions=self.config.get("subtitle_enable_auto_captions", True),
            embed_type=self.config.get("subtitle_embed_type", "soft"),
            embed_mode=self.config.get("subtitle_embed_mode", "always"),
            write_separate_file=self.config.get("subtitle_write_separate_file", False),
            format=self.config.get("subtitle_format", "srt"),
            quality_check=self.config.get("subtitle_quality_check", True),
            remove_ads=self.config.get("subtitle_remove_ads", False),
            fallback_to_english=self.config.get("subtitle_fallback_to_english", True),
            max_languages=self.config.get("subtitle_max_languages", 2),
        )

    def set_subtitle_config(self, config: SubtitleConfig) -> None:
        """设置字幕配置并保存"""
        self.config["subtitle_enabled"] = config.enabled
        self.config["subtitle_default_languages"] = config.default_languages
        self.config["subtitle_enable_auto_captions"] = config.enable_auto_captions
        self.config["subtitle_embed_type"] = config.embed_type
        self.config["subtitle_embed_mode"] = config.embed_mode
        self.config["subtitle_write_separate_file"] = config.write_separate_file
        self.config["subtitle_format"] = config.format
        self.config["subtitle_quality_check"] = config.quality_check
        self.config["subtitle_remove_ads"] = config.remove_ads
        self.config["subtitle_fallback_to_english"] = config.fallback_to_english
        self.config["subtitle_max_languages"] = config.max_languages
        self.save()


config_manager = ConfigManager()
