"""Observability Event 层 —— 把项目已有的各种判定接到日志上的那根线。

九层职责：

| 模块 | 职责 |
|---|---|
| `events` | 封闭的 `Stage` / `EventKind` / `Outcome` 集合、`Event`、JSON-safe normalizer、`emit_event()` |
| `trace` | 五级标识 `session → flow → task → run → attempt` 与 run 级累积状态 |
| `sanitize` | argv / URL / 路径 / 异常的脱敏 |
| `artifacts` | 产物词汇表 + `kind=expect` / `kind=actual`（硬规则 3 的 `expected − actual`） |
| `config_snapshot` | 生效配置快照（`kind=config scope=snapshot`）与单键变更（`scope=change`），落盘前脱敏 |
| `signals` | 成功路径上的异常征兆 → `kind=signal`（**绝不** `diagnosis`，硬规则 2） |
| `sinks` | JSONL 路由 sink、raw dump、同步 ERROR sink、trace 目录清扫 |
| `futures` | `ThreadPoolExecutor` 的异常观测（`threading.excepthook` 管不到的那部分） |
| `bundle` | 一键导出 bug 包（经 `identity` 事件反查 flow） |

依赖方向：`observability` → `diagnostics` + `utils/paths` + `utils/logger` +
`utils/aux_files` + `models/subtitle_config`，**反向禁止**（`diagnostics` 里不许出现
`from ..observability import`）。后两个是纯 stdlib 的 Foundation 层常量声明，
`artifacts` 从它们取字幕后缀表和 `__fluentytdl_subtitle_resolution` 的键名 ——
在这里重抄一份字符串，日后改名就会静默失配。

**不依赖 `core`** —— `config_snapshot` 要取配置值，但它接的是一个取值 callable，
而不是 import `core.config_manager`（后者反过来要调 `emit_config_change()`，直连必成环）。
"""

from .artifacts import (
    CONTAINER_PREFIX,
    DELIVERED_MEDIA,
    DELIVERED_PREFIX,
    DELIVERED_THUMBNAIL,
    EMBEDDED_PREFIX,
    EMBEDDED_SUBTITLE,
    EMBEDDED_THUMBNAIL,
    MEDIA,
    SUBTITLE_ANY,
    SUBTITLE_PREFIX,
    THUMBNAIL,
    audio_langs_from_opts,
    container_token,
    delivery_tokens,
    embed_tokens,
    emit_actual,
    emit_expect,
    expect_fields,
    expected_artifacts,
    found_artifacts,
    subtitle_token,
)
from .config_snapshot import (
    CONFIG_SNAPSHOT_KEYS,
    WATCHED_CONFIG_KEYS,
    emit_config_change,
    emit_config_snapshot,
    sanitize_config_value,
    snapshot_fields,
)
from .events import (
    EVENT_KINDS,
    NO_ID,
    OUTCOME_PRIORITY,
    OUTCOMES,
    SESSION_ID,
    STAGES,
    Event,
    EventKind,
    Outcome,
    Stage,
    build_event,
    emit_event,
    emit_events,
    json_safe,
    new_id,
    render_fields,
    worst_outcome,
)
from .futures import observe_future, observe_futures
from .sanitize import (
    mask_secrets,
    render_argv,
    sanitize_argv,
    sanitize_exception,
    sanitize_path,
    sanitize_proxy,
    sanitize_url,
)
from .signals import MAX_SIGNALS, emit_success_signals
from .sinks import (
    dump_raw_for_outcome,
    get_trace_dir,
    install_sinks,
    should_keep_raw_line,
    sweep_trace_dir,
    write_raw_dump,
)
from .trace import FlowTrace, TaskTrace, bind_current_flow, current_flow, new_flow

__all__ = [
    "CONFIG_SNAPSHOT_KEYS",
    "CONTAINER_PREFIX",
    "DELIVERED_MEDIA",
    "DELIVERED_PREFIX",
    "DELIVERED_THUMBNAIL",
    "EMBEDDED_PREFIX",
    "EMBEDDED_SUBTITLE",
    "EMBEDDED_THUMBNAIL",
    "EVENT_KINDS",
    "MAX_SIGNALS",
    "MEDIA",
    "NO_ID",
    "OUTCOMES",
    "OUTCOME_PRIORITY",
    "SESSION_ID",
    "STAGES",
    "SUBTITLE_ANY",
    "SUBTITLE_PREFIX",
    "THUMBNAIL",
    "WATCHED_CONFIG_KEYS",
    "Event",
    "EventKind",
    "FlowTrace",
    "Outcome",
    "Stage",
    "TaskTrace",
    "audio_langs_from_opts",
    "bind_current_flow",
    "build_event",
    "container_token",
    "current_flow",
    "delivery_tokens",
    "dump_raw_for_outcome",
    "embed_tokens",
    "emit_actual",
    "emit_config_change",
    "emit_config_snapshot",
    "emit_event",
    "emit_events",
    "emit_expect",
    "emit_success_signals",
    "expect_fields",
    "expected_artifacts",
    "found_artifacts",
    "get_trace_dir",
    "install_sinks",
    "json_safe",
    "mask_secrets",
    "new_flow",
    "new_id",
    "observe_future",
    "observe_futures",
    "render_argv",
    "render_fields",
    "sanitize_argv",
    "sanitize_config_value",
    "sanitize_exception",
    "sanitize_path",
    "sanitize_proxy",
    "sanitize_url",
    "should_keep_raw_line",
    "snapshot_fields",
    "subtitle_token",
    "sweep_trace_dir",
    "worst_outcome",
    "write_raw_dump",
]
