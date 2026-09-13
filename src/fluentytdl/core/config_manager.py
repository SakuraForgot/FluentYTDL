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
        "youtube_cookies_enabled": True,
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
        # Empty uses the shared runtime resolver (local candidates, then PATH).
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
        # 默认关闭：缓存键里没有出口 IP 这一维（system/TUN 模式下 proxy 压根不设），
        # 换节点撞不掉旧条目；cookie 那边弱回退被闸门拒绝时 .meta 也不动。
        # 机制没研究透之前默认不保留，想要的人自己在设置页开。
        "parse_cache_enabled": True,
        "parse_cache_ttl_seconds": 0,
        # 一次性迁移标记：旧版本把保留时间写死成 300 且无 UI 入口，见 _load()。
        "parse_cache_ttl_migrated": True,
        # 一次性迁移标记：把上一版的默认 1800 降到 0（默认关闭），见 _load()。
        "parse_cache_ttl_off_migrated": True,
        # 播放列表逐条深解析（entry_detail）的独立容量。它与弹窗结果分桶存放，
        # 调大只会占更多内存，不会挤掉弹窗/列表缓存。
        "parse_cache_entry_max": 64,
        # Dependency update source
        # github: official github api/releases
        # cloudflare: cached update metadata; downloads stay on GitHub
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
        # 交付：两个**正交**开关，不是一个 XOR（旧的 subtitle_embed_type/subtitle_embed_mode
        # 表达不出「都要」和「都不要」，见 models/subtitle_config._read_delivery）
        "subtitle_embed": True,  # 是否嵌入视频容器（软字幕轨）
        "subtitle_keep_external": False,  # 是否另存一份独立字幕文件
        "subtitle_delivery_migrated": True,  # 一次性迁移标记，见 _migrate_subtitle_delivery()
        "subtitle_output_format": "vtt",  # 字幕输出格式：srt/ass/vtt/lrc
        "subtitle_quality_check": True,  # 是否启用字幕质量检查
        "subtitle_remove_ads": False,  # 是否自动移除字幕广告
        "subtitle_fallback_to_english": True,  # 是否回退到英语
        "subtitle_max_languages": 2,  # 最多下载字幕数量
        # 音频偏好设置
        # audio_track_strategy: 音轨策略，见 utils/format_scorer.py AUDIO_STRATEGIES
        # 'original_first' 原音优先 / 'language_first' 指定语言优先 / 'original_only' 仅原音
        "audio_track_strategy": "original_first",
        # preferred_audio_languages: 首选音轨语言（多选优先级，仅在 language_first 下生效）
        # 'zh-Hans': 简体中文, 'en': 英文, 'ja': 日语……任意 BCP-47 标签
        # 'orig' **不再**是这里的条目，已拆成 audio_track_strategy（见 _migrate_audio_orig_to_strategy）
        "preferred_audio_languages": ["zh-Hans", "en"],
        # audio_allow_descriptive: 是否允许自动选中音频描述轨（视障辅助解说轨）
        "audio_allow_descriptive": False,
        "audio_strategy_migrated": True,
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
        "app_update_channel": "stable",
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
        # === 观测层 ===
        # yt-dlp 原始输出的全程留档。默认关：干净成功的下载不留原文，那是绝大多数
        # 情形，留下来只会把 trace 目录写满。**关掉也仍会**在 failed / degraded /
        # recovered 三种异常终态自动留一份（`sinks.dump_raw_for_outcome()`），
        # 这个开关只决定"正常成功的下载要不要也留"。
        "log_raw_ytdlp": False,
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
        from ..utils.paths import frozen_app_dir, is_frozen

        if is_frozen() and (frozen_app_dir() / "install-language.txt").is_file():
            from ..utils.install_registration import register_data_root

            register_data_root(frozen_app_dir(), self.config_file.parent)

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
            defaults = self.DEFAULT_CONFIG.copy()
            from ..utils.paths import frozen_app_dir, is_frozen

            if is_frozen():
                seed = frozen_app_dir() / "install-language.txt"
                if seed.is_file():
                    language = seed.read_text(encoding="utf-8-sig").strip()
                    if language in ("zh_CN", "en_US"):
                        defaults["app_language"] = language
            return defaults

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
            if merged.get("update_source") == "ghproxy":
                merged["update_source"] = "cloudflare"
            from ..utils.language import normalize_language_setting

            merged["app_language"] = normalize_language_setting(merged["app_language"])

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
            # Migration: parse cache retention default 30 min -> off.
            self._migrate_parse_cache_ttl_off(data, merged)
            # Migration: 'orig' 条目 -> 独立的 audio_track_strategy。
            self._migrate_audio_orig_to_strategy(data, merged)
            # Migration: subtitle_embed_type/-_mode 这个 XOR -> 两个正交开关。
            self._migrate_subtitle_delivery(data, merged)

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

    @staticmethod
    def _migrate_parse_cache_ttl_off(data: dict[str, Any], merged: dict[str, Any]) -> None:
        """解析结果保留时间默认关闭：磁盘上的 1800s（半小时）降到 0（不保留）。

        缓存机制本身还没研究透——键里没有出口 IP 这一维（`proxy_mode == "system"`
        和 TUN 模式下 `ydl_opts["proxy"]` 压根不设），在 V2RayN 里换节点撞不掉旧条目；
        cookie 提交被校验闸门拒绝（弱回退）时 `.meta` 不动，缓存也不失效。用户看到的
        就是"换了 Cookie、切了 IP，画质还是上次那样"。默认关掉止损，想要的人自己开。

        **判定读的是 `merged` 而不是 `data`**：`_load()` 里这一步跑在
        `_migrate_parse_cache_ttl()` 之后，旧的 300 安装刚被那一步抬成 1800，
        读 `data`（还是 300）会让它卡在 1800 上，恰好是最该被关掉的那批人。

        代价是设置页上线后主动挑过「30 分钟」的用户会被降一次。这个选择和旧默认值在
        磁盘上无法区分，而本次改动的目的正是"机制不可靠，先静默"，降掉可以接受。
        标记位保证只跑一次：用户降完再挑回 30 分钟，下次启动不会又被降。

        比较刻意用 `== 1800` 而不是 `int(...)`——理由同 `_migrate_parse_cache_ttl`：
        `_load()` 整段外面套着 except，这里抛一次会把用户的整份配置回退成默认值。
        """
        if data.get("parse_cache_ttl_off_migrated"):
            return
        if merged.get("parse_cache_ttl_seconds") == 1800:
            merged["parse_cache_ttl_seconds"] = 0
        merged["parse_cache_ttl_off_migrated"] = True

    @staticmethod
    def _migrate_audio_orig_to_strategy(data: dict[str, Any], merged: dict[str, Any]) -> None:
        """把 `preferred_audio_languages` 里的 `orig` 条目拆成独立的 `audio_track_strategy`。

        旧方案把「原音」当成语言列表里的一个条目，和 `zh-Hans`/`en` 挤在一起排序 ——
        既表达不了"只要原音"，也让"原音第几位"这种没有意义的排序变得可能。现在原音是
        一个正交的策略维度，列表只管语言。

        迁移读用户原来把 `orig` 放在哪：
        - 首位 → `original_first`（"原音优先"，正是他们想表达的）
        - 其它位置 → `language_first`（语言排在原音之前 → 语言优先）
        - 不含 `orig` → 保持默认 `original_first`

        然后从列表里剔掉 `orig`。剔掉之后可能空掉（老配置里就写了个 `["orig"]`），
        补回默认的 `["zh-Hans", "en"]` —— 空列表在 `language_first` 下等于没有偏好。

        标记位保证只跑一次：用户迁移后自己把策略改回来，下次启动不能被再改一遍。
        判定用 `in` / `index()` 而不做类型转换 —— `_load()` 整段外面套着 except，
        这里抛一次异常会把用户的整份配置回退成默认值。
        """
        if data.get("audio_strategy_migrated"):
            return
        merged["audio_strategy_migrated"] = True

        langs = data.get("preferred_audio_languages")
        if not isinstance(langs, list):
            return

        normalized = [str(x).strip() for x in langs if str(x).strip()]
        lowered = [x.lower() for x in normalized]
        if "orig" not in lowered and "original" not in lowered:
            return

        pos = lowered.index("orig") if "orig" in lowered else lowered.index("original")
        merged["audio_track_strategy"] = "original_first" if pos == 0 else "language_first"
        rest = [x for x in normalized if x.lower() not in {"orig", "original"}]
        merged["preferred_audio_languages"] = rest or ["zh-Hans", "en"]

    @staticmethod
    def _migrate_subtitle_delivery(data: dict[str, Any], merged: dict[str, Any]) -> None:
        """`subtitle_embed_type` / `subtitle_embed_mode` 这个 XOR → 两个正交开关。

        旧模型只能表达「嵌入」**或**「另存」，四种组合里的「都要」和「都不要」压根
        写不出来。新模型是 `subtitle_embed` + `subtitle_keep_external` 两个布尔。

        映射与 `models/subtitle_config._read_delivery()` 逐字一致（那边管
        `SubtitleConfig.from_dict()` 的旧任务字典，这边管磁盘上的 `config.json`，
        两处必须给出同一个答案，否则设置页显示的和实际下载用的会分叉）：

        | `embed_type` | `embed_mode` | → `subtitle_embed` | → `subtitle_keep_external` |
        |---|---|---|---|
        | `"soft"`（默认）| `≠ "never"` | True | False |
        | `"soft"` | `"never"` | False | True |
        | `"external"` | 任意 | False | True |

        **不会迁到第四态**（都不要）：旧的 `embed_mode == "never"` 在旧文档里字面
        写的是「总是保存为单独文件」，映射成「不要字幕」会让老用户升级一次就字幕
        凭空消失。所以 `keep_external = not embed` 恒成立，两个新态只能由用户在新
        UI 里主动选出来。

        **必须在这里做，不能靠 `get_subtitle_config()` 里的 `_get()` 兜底** ——
        `_load_config()` 走的是 `{**DEFAULT_CONFIG, **data}`，新键在合并后**必然存在**
        （值是 `DEFAULT_CONFIG` 的 `True`/`False`），`_get()` 因此永远看不到「缺失」，
        旧键会被静默无视。判定读的是 `data`（磁盘原文）而不是 `merged`。

        标记位保证只跑一次：用户迁移后自己把开关改回来，下次启动不能被再改一遍。
        两个旧键**不从 `merged` 里删** —— `save()` 写整份配置，留着它们是回滚到旧版本
        时的唯一依据，而新版本已经没有任何读者了（Step 6 把读者清零）。
        """
        if data.get("subtitle_delivery_migrated"):
            return
        merged["subtitle_delivery_migrated"] = True

        if "subtitle_embed" in data or "subtitle_keep_external" in data:
            # 已经是新模型了（多半是同版本内的重复迁移），别覆盖用户的选择
            return
        if "subtitle_embed_type" not in data and "subtitle_embed_mode" not in data:
            # 全新安装，DEFAULT_CONFIG 已经给了正确的值
            return

        legacy_soft = data.get("subtitle_embed_type", "soft") == "soft"
        embed = legacy_soft and data.get("subtitle_embed_mode", "always") != "never"
        merged["subtitle_embed"] = embed
        merged["subtitle_keep_external"] = not embed

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
        # 旧值必须在覆盖前取 —— `kind=config scope=change` 记的是 old → new。
        old = self.config.get(key)
        self.config[key] = value
        self.save()
        try:
            # 函数级 import：`observability` 不许依赖 `core`（`config_snapshot` 反过来
            # 只接一个取值 callable 就是为了这个），模块级 import 会成环。
            #
            # 整段裹 try/except 而不是只信任 `emit_config_change()` 自己的兜底：
            # import 语句本身在它的 try 之外，而硬规则 5 要求记录失败绝不能影响业务 ——
            # 保存设置显然是业务。
            #
            # `_depth=3` 让日志里的 `{name}:{function}:{line}` 指向**改配置的那一行**
            # （设置页某个控件的槽函数），而不是恒定的 `config_manager.py:set` ——
            # 排查"我的设置怎么自己变了"时要的正是"谁改的"。
            from ..observability.config_snapshot import emit_config_change

            emit_config_change(key, old, value, _depth=3)
        except Exception:
            pass
        self.configChanged.emit(key, value)

    def get_subtitle_config(self) -> SubtitleConfig:
        """获取字幕配置对象。

        默认值一律回落到 `DEFAULT_CONFIG`，不再在这里重写一遍字面量 ——
        `_load()` 走的是 `{**DEFAULT_CONFIG, **data}`，所以这些键必然存在，
        原先那份副本（`"vtt"` / `2`）纯属第二处真相来源。

        注意：`SubtitleConfig` 的 dataclass 默认值（`srt` / `10`）与 `DEFAULT_CONFIG`
        （`vtt` / `2`）**并不一致**。以 `DEFAULT_CONFIG` 为准，dataclass 默认值只在
        直接 `SubtitleConfig()` 时生效（测试、临时覆盖）。
        """
        from ..models.subtitle_config import SubtitleTypePreference

        def _get(key: str) -> Any:
            return self.config.get(key, self.DEFAULT_CONFIG[key])

        return SubtitleConfig(
            enabled=_get("subtitle_enabled"),
            type_preference=SubtitleTypePreference(_get("subtitle_type_preference")),
            default_languages=_get("subtitle_default_languages"),
            enable_auto_captions=_get("subtitle_enable_auto_captions"),
            embed=bool(_get("subtitle_embed")),
            keep_external=bool(_get("subtitle_keep_external")),
            output_format=_get("subtitle_output_format"),
            quality_check=_get("subtitle_quality_check"),
            remove_ads=_get("subtitle_remove_ads"),
            fallback_to_english=_get("subtitle_fallback_to_english"),
            max_languages=_get("subtitle_max_languages"),
        )

    def set_subtitle_config(self, config: SubtitleConfig) -> None:
        """设置字幕配置并保存"""
        self.config["subtitle_enabled"] = config.enabled
        self.config["subtitle_type_preference"] = config.type_preference.value
        self.config["subtitle_default_languages"] = config.default_languages
        self.config["subtitle_enable_auto_captions"] = config.enable_auto_captions
        self.config["subtitle_embed"] = config.embed
        self.config["subtitle_keep_external"] = config.keep_external
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
