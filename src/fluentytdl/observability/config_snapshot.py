"""生效配置快照与变更追踪（`kind=config`）。

**为什么需要它。** `core/config_manager.py`（388 行）**零日志**。用户报"我明明勾了字幕
却没下到"，日志里读不出当时 `subtitle_enabled` 是真是假、`subtitle_default_languages`
是哪几个、有没有被快速模式的 `quick_subtitle_*` 覆盖掉 —— 于是每个字幕类工单都得先花
几轮问答重建现场。同理："下载很慢"要先知道 `rate_limit` 与 `max_concurrent_downloads`，
"一直 403"要先知道认证来源是 cookie 还是 OAuth2。

**两种形态，同一个 kind：**

| 形态 | 何时 | 内容 |
|---|---|---|
| `scope=snapshot` | 启动时一次，挂在 `startup_info.log_startup_info()` 旁边 | `CONFIG_SNAPSHOT_KEYS` 全量 |
| `scope=change` | `config_manager.set()` 写入被观测键时 | 单键 `old` → `new` |

**只收影响下载行为的键。** 主题色、欢迎页看过没有、上次检查更新的时间戳这些进来只会
稀释信息。`recent_target_url` 也刻意排除 —— 那是用户最近下过什么，属于隐私，而排查
问题时手上本来就有出问题的那条 URL。

**脱敏是这里的硬要求，不是可选项。** 快照行里躺着 `proxy_url`（可能含账号密码）、
`download_dir`（含 Windows 用户名）、`youtube_po_token`（凭据本体）。日志文件是用户会
直接贴进 GitHub Issue 的东西，所以三类值分别走 `sanitize_proxy` / `sanitize_path` /
整体涂掉，**绝不原样落盘**。

**为什么 `stage=startup`。** `Stage` 是封闭集合，里面没有"设置页"这一档，也不该为一条
日志给它开一个 —— 那个枚举描述的是下载流水线的位置。配置事件本来就产生在流水线之外，
`startup` 是唯一的非流水线阶段；固定用它能让 `stage=startup kind=config` 成为一个可以
直接 grep 的组合，即使那条 change 事件发生在下载中途。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .events import emit_event
from .sanitize import MASK, sanitize_path, sanitize_proxy

#: 值是凭据本体：只记"有没有设"，绝不记内容。
_SECRET_KEYS = frozenset({"youtube_po_token"})
#: 值是代理地址：只留 ``scheme://host:port``。
_PROXY_KEYS = frozenset({"proxy_url"})
#: 值是本机路径：折掉主目录，保留盘符与结构（那部分对排查有用，泄漏的只有用户名）。
_PATH_KEYS = frozenset(
    {
        "download_dir",
        "quick_download_dir",
        "ffmpeg_path",
        "yt_dlp_exe_path",
        "js_runtime_path",
        "cookie_file",
    }
)

#: 快照的键与**顺序**。有序是刻意的 —— 两次启动的快照行能直接 diff，
#: 而 dict 的偶然顺序做不到这一点。分组即是"哪一类问题该看哪几行"。
CONFIG_SNAPSHOT_KEYS: tuple[str, ...] = (
    # 网络：429 / 403 / 超时的第一现场
    "proxy_mode",
    "proxy_url",
    "rate_limit",
    "network_retries",
    "concurrent_fragments",
    "max_concurrent_downloads",
    # 认证：cookie / OAuth2 / PO Token 三条来源，任一错配都表现成 403
    "auth_mode",
    "cookie_mode",
    "cookie_browser",
    "cookie_file",
    "oauth2_status",
    "youtube_po_token",
    "pot_provider_enabled",
    # 工具链：nsig / EJS 一类失败的前提条件
    "ytdlp_channel",
    "yt_dlp_exe_path",
    "ffmpeg_path",
    "js_runtime",
    "js_runtime_path",
    # 字幕：用户最初的痛点
    "subtitle_enabled",
    "subtitle_default_languages",
    "subtitle_type_preference",
    "subtitle_enable_auto_captions",
    "subtitle_max_languages",
    "subtitle_output_format",
    "subtitle_embed_type",
    "subtitle_embed_mode",
    # 音轨
    "preferred_audio_languages",
    "audio_multistream_default_count",
    # 容器与格式记忆
    "single_container_override",
    "single_audio_override",
    "playlist_container_override",
    "playlist_audio_override",
    # 画质守卫
    "quality_guard_mode",
    "quality_guard_ffprobe",
    "quality_guard_suspend_threshold",
    # 后处理
    "embed_metadata",
    "embed_thumbnail",
    "download_thumbnail",
    "sponsorblock_enabled",
    "sponsorblock_action",
    "vr_eac_auto_convert",
    # 续传与清理
    "enable_resume",
    "failed_task_retention_days",
    # 解析
    "playlist_skip_authcheck",
    "playlist_extract_concurrency",
    "parse_cache_enabled",
    "parse_cache_ttl_seconds",
    # 路径
    "download_dir",
    # 快速模式：它**会覆盖**上面几个同义项（`init_quick_mode_defaults()` 只在首次
    # 初始化时同步一次，之后两套值各走各的）。不带上这几行，就解释不了"设置页里
    # 明明勾了字幕，快速模式下的任务却没有字幕"。
    "quick_download_type",
    "quick_video_quality",
    "quick_container",
    "quick_audio_format",
    "quick_subtitle_enabled",
    "quick_subtitle_languages",
    "quick_playlist_strategy",
    "quick_download_dir",
)

#: 哪些键的变更值得落一条 `scope=change`。与快照同一份键集 ——
#: "启动时值得记"和"改动时值得记"是同一个判断，分成两份必然会漂移。
WATCHED_CONFIG_KEYS: frozenset[str] = frozenset(CONFIG_SNAPSHOT_KEYS)


def sanitize_config_value(key: str, value: Any) -> Any:
    """按键名决定脱敏方式。未列名的键原样返回（它们本来就是布尔/数字/枚举）。"""
    if key in _SECRET_KEYS:
        # 只表达"有没有设"。空值渲染成 `""`，非空渲染成 `***`，两者一眼可分。
        return MASK if value else ""
    if key in _PROXY_KEYS:
        return sanitize_proxy(value) if value else ""
    if key in _PATH_KEYS:
        return sanitize_path(value) if value else ""
    return value


def snapshot_fields(get: Callable[..., Any]) -> dict[str, Any]:
    """按 `CONFIG_SNAPSHOT_KEYS` 取值并脱敏。

    参数是取值函数（`config_manager.get`）而**不是** `ConfigManager` 实例：
    `observability` 不许依赖 `core`（`config_manager.set()` 反过来要调本模块，
    互相 import 就成环了）。传一个 callable 把方向掰直，顺便让测试不需要单例。
    """
    out: dict[str, Any] = {}
    for key in CONFIG_SNAPSHOT_KEYS:
        try:
            out[key] = sanitize_config_value(key, get(key))
        except Exception:
            # 某个键读不出来不该让整份快照消失 —— 剩下 50 个键仍然有排查价值。
            out[key] = "<unreadable>"
    return out


def emit_config_snapshot(get: Callable[..., Any], *, trace: Any = None, _depth: int = 2) -> None:
    """启动时落一条全量生效配置。"""
    try:
        fields: dict[str, Any] = {"scope": "snapshot"}
        fields.update(snapshot_fields(get))
        emit_event("config", trace=trace, stage="startup", fields=fields, _depth=_depth)
    except Exception:  # noqa: BLE001 - 硬规则 5：观测永远 best-effort
        pass


def emit_config_change(
    key: str,
    old: Any,
    new: Any,
    *,
    trace: Any = None,
    _depth: int = 2,
) -> None:
    """记一次配置写入。非观测键与"写了同一个值"都静默丢弃。

    **为什么外面还要包一层 try/except**，`emit_event()` 明明已经吞异常了：脱敏和
    `old == new` 这两步跑在**调用方的栈帧里**（`config_manager.set()`），`emit_event`
    的 try/except 兜不到。硬规则 5 要求记录失败绝不能影响业务，而保存设置显然是业务。

    大量"写回当前值"来自设置页初始化时的信号回灌 —— 不过滤掉，打开一次设置页就能
    刷出几十条毫无信息量的 change 事件。
    """
    try:
        if key not in WATCHED_CONFIG_KEYS:
            return
        if old == new:
            return
        emit_event(
            "config",
            trace=trace,
            stage="startup",
            _depth=_depth,
            fields={
                "scope": "change",
                "key": key,
                "old": sanitize_config_value(key, old),
                "new": sanitize_config_value(key, new),
            },
        )
    except Exception:  # noqa: BLE001 - 同上
        pass
