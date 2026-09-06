from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Any

from fluentytdl.utils.paths import (
    config_path,
    find_bundled_executable,
    frozen_internal_dir,
    get_clean_env,
    is_frozen,
    locate_runtime_tool,
)

from ..core.config_manager import config_manager
from ..models.errors import YtDlpExecutionError
from ..observability import current_flow, emit_event, emit_success_signals, mask_secrets


class YtDlpCancelled(Exception):
    """Raised when a yt-dlp subprocess is cancelled by the UI."""


# yt-dlp / bgutil 插件在输出里提到 POT 时的关键字。
# PoTokenProviderRejectedRequest 是"服务不可达导致静默降级"的唯一信号，必须转记。
_POT_OUTPUT_MARKERS = (
    "po token",
    "potoken",
    "bgutil",
    "getpot",
    "youtubepot",
)


def log_pot_from_output(out: str, *, stage: str) -> None:
    """把 yt-dlp 自身输出里与 POT 相关的行转记为 [POT][YtDlp]（阶段 4）。

    正常运行的 yt-dlp 不会打印 POT 细节（插件只在 trace 级别打），
    但拒绝/失败类信息会出现在 stderr 中，之前被整段吞掉。
    """
    if not out:
        return
    from loguru import logger

    for line in out.splitlines():
        s = line.strip()
        if not s or s.startswith("{"):
            continue
        low = s.lower()
        if any(m in low for m in _POT_OUTPUT_MARKERS):
            logger.info("[POT][YtDlp][{}] {}", stage, s[:400])


def log_pot_in_argv(cmd: list[str], *, stage: str, task_id: str = "") -> None:
    """断言式日志：POT 参数到底有没有落进最终 argv（阶段 3）。

    关键在负向分支——"开关开着但命令里没有 POT"目前完全看不出来。
    只记 base_url（本机回环地址），绝不记 Token。
    """
    from loguru import logger

    tail = f" task={task_id}" if task_id else ""
    hit = ""
    for i, a in enumerate(cmd):
        if "youtubepot" in str(a).lower():
            hit = str(a)
            break
        if str(a) == "--extractor-args" and i + 1 < len(cmd):
            nxt = str(cmd[i + 1])
            if "youtubepot" in nxt.lower():
                hit = nxt
                break

    if hit:
        base = ""
        if "base_url=" in hit:
            base = hit.split("base_url=", 1)[1].split(";", 1)[0].strip()
        logger.info(
            "[POT][{}] argv 已含 youtubepot-bgutilhttp base_url={}{}",
            stage,
            base or "(未解析出)",
            tail,
        )
        return

    try:
        enabled = bool(config_manager.get("pot_provider_enabled") or False)
    except Exception:
        enabled = False
    if enabled:
        logger.warning("[POT][{}] argv 未含 POT 参数 (enabled=True){}", stage, tail)
    else:
        logger.debug("[POT][{}] argv 未含 POT 参数 (enabled=False){}", stage, tail)


def _safe_working_dir() -> str:
    """Return a stable writable working directory for yt-dlp child processes."""

    try:
        configured = str(config_manager.get("download_dir") or "").strip()
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
            return str(Path(configured).resolve())
    except Exception:
        pass

    try:
        data_dir = config_path().parent
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir.resolve())
    except Exception:
        pass

    return str(Path.cwd())


def _win_hide_console_kwargs() -> dict[str, Any]:
    """Hide console window for subprocess on Windows (GUI apps)."""

    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {}
    try:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:
        pass

    return kwargs


# 可执行文件路径探测的记忆化：键是配置里的 yt_dlp_exe_path，
# 配置一变就自动失效。多路径探测每次解析都要跑，纯属重复劳动。
_exe_cache_lock = Lock()
_exe_cache: tuple[str, Path | None] | None = None


def resolve_yt_dlp_exe() -> Path | None:
    """Resolve yt-dlp executable path.

    Priority:
    1) config yt_dlp_exe_path (if exists)
    2) bundled _internal/yt-dlp/yt-dlp.exe (frozen)
    3) yt-dlp on PATH

    结果按配置项 `yt_dlp_exe_path` 记忆化；解析出的路径若已消失则重新探测。
    """
    global _exe_cache

    cfg = str(config_manager.get("yt_dlp_exe_path") or "").strip()

    with _exe_cache_lock:
        if _exe_cache is not None and _exe_cache[0] == cfg:
            cached = _exe_cache[1]
            # 缓存命中但文件被删/被移动时，退回重新探测。
            if cached is None or cached.exists():
                return cached

        resolved = _resolve_yt_dlp_exe_uncached(cfg)
        _exe_cache = (cfg, resolved)
        return resolved


def invalidate_yt_dlp_exe_cache() -> None:
    """清空 exe 路径缓存。

    常规配置变更无需调用——缓存键就是 `yt_dlp_exe_path`，改配置即自动失效；
    此函数留给"外部替换了 exe 文件但配置未变"这类场景。
    """
    global _exe_cache
    with _exe_cache_lock:
        _exe_cache = None


def _resolve_yt_dlp_exe_uncached(cfg: str) -> Path | None:
    """`resolve_yt_dlp_exe` 的实际探测逻辑。"""
    if cfg:
        p = Path(cfg)
        if p.exists():
            return p

    # Prefer tools placed into exe-adjacent `bin` (or project `bin`) via locate_runtime_tool.
    try:
        return locate_runtime_tool("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
    except FileNotFoundError:
        # fallback to legacy bundled search when frozen
        if is_frozen():
            p = find_bundled_executable(
                "yt-dlp.exe",
                "yt-dlp/yt-dlp.exe",
                "yt_dlp/yt-dlp.exe",
            )
            if p is not None:
                return p

    which = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    return Path(which) if which else None


def _prepend_path(env: dict[str, str], *dirs: str) -> None:
    existing = env.get("PATH") or ""
    cleaned = [d for d in dirs if d]
    if not cleaned:
        return
    env["PATH"] = os.pathsep.join(cleaned + [existing]) if existing else os.pathsep.join(cleaned)


# ---------------------------------------------------------------------------
# POT Plugin Sync — 标准目录插件安装
# ---------------------------------------------------------------------------
# 独立编译的 yt-dlp.exe（PyInstaller/Nuitka）不支持 PYTHONPATH 外部插件加载。
# 必须将插件放置在 <exe-dir>/yt-dlp-plugins/<pkg>/yt_dlp_plugins/extractor/ 下，
# 这是 yt-dlp 官方推荐的 "Executable location" 安装方式。
# See: https://github.com/yt-dlp/yt-dlp#installing-plugins
# ---------------------------------------------------------------------------

_PLUGIN_PACKAGE_NAME = "bgutil-ytdlp-pot-provider"
_PLUGIN_FILE_GLOB = "getpot_bgutil*.py"


def _get_pot_plugin_source_dir() -> Path | None:
    """获取 POT 插件源文件目录。

    Dev 模式: src/fluentytdl/yt_dlp_plugins_ext/yt_dlp_plugins/extractor/
    Frozen 模式: _internal/fluentytdl/yt_dlp_plugins_ext/yt_dlp_plugins/extractor/
    """
    _subpath = Path("yt_dlp_plugins_ext") / "yt_dlp_plugins" / "extractor"

    # 方式 1：基于 __file__ 的相对路径（dev 和大多数 frozen 场景）
    source = Path(__file__).resolve().parent.parent / _subpath
    if source.exists():
        return source

    # 方式 2：frozen 回退 — 基于 _internal 目录
    # PyInstaller 打包时 .spec datas 将 yt_dlp_plugins_ext 挂载到 fluentytdl/ 下
    # 但 __file__ 在某些 PyInstaller 配置下可能不指向 _internal
    if is_frozen():
        source2 = frozen_internal_dir() / "fluentytdl" / _subpath
        if source2.exists():
            return source2

    return None


# 插件同步的结果记忆化：键是源目录内容指纹（文件名 + mtime + size），
# 值是上次同步结论。每次解析都跑一遍 glob + 逐文件 stat 是纯浪费，
# 而并行解析（频道多标签）还会让多个线程同时 copy2 同一批文件。
_pot_sync_lock = Lock()
_pot_sync_cache: tuple[Any, bool] | None = None


def sync_pot_plugins_to_ytdlp() -> bool:
    """将 POT 插件文件同步到 yt-dlp.exe 旁的标准插件目录。

    yt-dlp 独立编译版（.exe）不支持 PYTHONPATH 插件加载，
    需要将插件放置在 <exe-dir>/yt-dlp-plugins/<pkg>/yt_dlp_plugins/extractor/ 下。

    此函数执行增量同步：仅当源文件更新（mtime 更新或目标不存在）时才复制。
    结果按源目录指纹记忆化，并由 `_pot_sync_lock` 串行化，可安全并发调用。

    Returns:
        True 如果插件目录就绪（已同步或无需同步）
    """
    from loguru import logger

    with _pot_sync_lock:
        return _sync_pot_plugins_locked(logger)


def _pot_source_fingerprint(source_dir: Path, source_files: list[Path]) -> Any:
    """源插件目录的轻量指纹：(路径, mtime_ns, size) 三元组的有序元组。"""
    items = []
    for f in sorted(source_files):
        try:
            st = f.stat()
            items.append((f.name, st.st_mtime_ns, st.st_size))
        except OSError:
            items.append((f.name, -1, -1))
    return (str(source_dir), tuple(items))


def _sync_pot_plugins_locked(logger: Any) -> bool:
    """`sync_pot_plugins_to_ytdlp` 的实际实现，调用方必须已持有 `_pot_sync_lock`。"""
    global _pot_sync_cache

    try:
        exe = resolve_yt_dlp_exe()
        if exe is None:
            logger.debug("POT Plugin Sync: yt-dlp.exe 未找到，跳过同步")
            return False

        source_dir = _get_pot_plugin_source_dir()
        if source_dir is None:
            logger.debug("POT Plugin Sync: 插件源目录不存在，跳过同步")
            return False

        source_files = list(source_dir.glob(_PLUGIN_FILE_GLOB))
        if not source_files:
            logger.debug("POT Plugin Sync: 未找到插件源文件，跳过同步")
            return False

        # 目标: <exe-dir>/yt-dlp-plugins/<pkg>/yt_dlp_plugins/extractor/
        target_dir = (
            exe.parent / "yt-dlp-plugins" / _PLUGIN_PACKAGE_NAME / "yt_dlp_plugins" / "extractor"
        )

        # 记忆化：源指纹 + 目标目录都没变，就没必要再逐文件 stat 一遍。
        fingerprint = (str(exe), _pot_source_fingerprint(source_dir, source_files))
        if _pot_sync_cache is not None and _pot_sync_cache[0] == fingerprint:
            return _pot_sync_cache[1]

        # 增量同步：只在需要时创建目录和复制文件
        needs_sync = False
        if not target_dir.exists():
            needs_sync = True
        else:
            for src_file in source_files:
                dst_file = target_dir / src_file.name
                if not dst_file.exists():
                    needs_sync = True
                    break
                # 比较修改时间（源更新则需要同步）
                if src_file.stat().st_mtime > dst_file.stat().st_mtime:
                    needs_sync = True
                    break

        if not needs_sync:
            logger.debug("POT Plugin Sync: 插件已是最新，无需同步")
            _pot_sync_cache = (fingerprint, True)
            return True

        # 执行同步
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            logger.warning(
                f"POT Plugin Sync: 无法创建插件目录 {target_dir}（权限不足）。"
                "如果安装在 Program Files 下，请以管理员身份运行一次，或手动复制插件文件。"
            )
            return False

        synced = 0
        for src_file in source_files:
            dst_file = target_dir / src_file.name
            try:
                shutil.copy2(src_file, dst_file)
                synced += 1
            except PermissionError:
                logger.warning(
                    f"POT Plugin Sync: 复制 {src_file.name} 失败（权限不足）。"
                    "请以管理员身份运行一次应用以完成插件部署。"
                )
            except Exception as e:
                logger.warning(f"POT Plugin Sync: 复制 {src_file.name} 失败: {e}")

        if synced > 0:
            logger.info(f"POT Plugin Sync: 已同步 {synced} 个插件文件到 {target_dir.parent.parent}")
            _pot_sync_cache = (fingerprint, True)
        return synced > 0

    except Exception as e:
        logger.debug(f"POT Plugin Sync: 同步异常: {e}")
        return False


def prepare_yt_dlp_env(extra_paths: list[str] | None = None) -> dict[str, str]:
    """Prepare environment so yt-dlp.exe can find bundled ffmpeg and JS runtime.

    We intentionally prefer PATH injection over less-portable flags.

    Args:
        extra_paths: Additional paths to prepend to PATH
    """

    env = get_clean_env()

    # 强制 yt-dlp 使用 utf-8 输出，防止截断或乱码
    env["PYTHONIOENCODING"] = "utf-8"

    # FFmpeg
    ffmpeg_path = str(config_manager.get("ffmpeg_path") or "").strip() or None
    if ffmpeg_path and Path(ffmpeg_path).exists():
        _prepend_path(env, str(Path(ffmpeg_path).resolve().parent))
    else:
        try:
            p = locate_runtime_tool("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
            _prepend_path(env, str(Path(p).resolve().parent))
        except FileNotFoundError:
            bundled_ffmpeg = find_bundled_executable("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
            if bundled_ffmpeg is not None:
                _prepend_path(env, str(bundled_ffmpeg.resolve().parent))

    # JS runtime (deno preferred)
    js_runtime_path = str(config_manager.get("js_runtime_path") or "").strip() or None
    if js_runtime_path and Path(js_runtime_path).exists():
        _prepend_path(env, str(Path(js_runtime_path).resolve().parent))
    else:
        try:
            p = locate_runtime_tool("deno.exe", "js/deno.exe", "deno/deno.exe")
            _prepend_path(env, str(Path(p).resolve().parent))
        except FileNotFoundError:
            bundled_deno = find_bundled_executable("deno.exe", "js/deno.exe", "deno/deno.exe")
            if bundled_deno is not None:
                _prepend_path(env, str(bundled_deno.resolve().parent))

    # Extra paths
    if extra_paths:
        for p in extra_paths:
            if p and Path(p).exists():
                _prepend_path(env, p)

    # --- POT Plugin Sync ---
    # 确保 POT 插件位于 yt-dlp.exe 旁的标准插件目录（独立 exe 兼容）
    sync_pot_plugins_to_ytdlp()

    # PYTHONPATH fallback: pip 安装的 yt-dlp（Python 脚本版）仍可通过此路径发现插件
    plugin_dir = Path(__file__).resolve().parent.parent / "yt_dlp_plugins_ext"
    if plugin_dir.exists():
        existing_pypath = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = (
            f"{plugin_dir}{os.pathsep}{existing_pypath}" if existing_pypath else str(plugin_dir)
        )

    return env


#: 原音过滤器。`>=10` 而不是 `=10`：阈值语义与 `format_scorer.audio_track_kind()`
#: 那边的 `pref >= AUDIO_ORIGINAL` 保持一致。
#:
#: **`?` 是必须的**（none-inclusive）：yt-dlp 的 `_build_format_filter` 在
#: `actual_value is None` 时返回 `m.group('none_inclusive')`，不带 `?` 就是假。
#: `language_preference` 只有 YouTube extractor 会算，Twitter 等平台的格式里压根
#: 没有这个键 —— 不带 `?` 会把那些平台的**所有**音轨过滤光，只能靠格式串末尾的
#: 无过滤兜底救回来。带 `?` 的语义正好是想要的："有这个字段就要原音，没有就别管。"
#:
#: **`?` 跟在运算符后面，不是跟在值后面**：yt-dlp 的过滤器正则把 none-inclusive 标记
#: 放在 `operator` 与 `value` 之间（`>=?10`）。写成 `>=10?` 会被当成值的一部分，
#: 真实 yt-dlp 直接 `SyntaxError: Invalid filter specification` 整个下载起不来 ——
#: 实测过，见 `tests/test_audio_format_injection.py::test_original_filter_is_none_inclusive`。
_ORIGINAL_AUDIO_FILTER = "[language_preference>=?10]"


def _inject_language_into_format(
    fmt: str,
    langs: list[str] | None,
    strategy: str | None,
    *,
    multistreams: bool = False,
) -> str:
    """把语言 / 原音偏好翻译成格式串里的过滤器分支，原始格式串作为最后兜底。

    这是音轨偏好在 CLI 上**唯一**的表达方式。`-S lang:xx` 不行：`lang` 是
    `language_preference` 的**数值**别名，不接受语言码，而且 `FormatSorter.add_item`
    只接受第一个 `lang:` 条目（见 `CLAUDE.md` §4 规则 4）。

    Example::

        fmt      = "bv*[height<=1080]+ba[ext=m4a]/b[height<=1080]/bv*[height<=1080]+ba"
        langs    = ["ja"]
        strategy = "language_first"
        result   = ("bv*[height<=1080]+ba[ext=m4a][language^=ja]/b[height<=1080][language^=ja]"
                    "/bv*[height<=1080]+ba[language^=ja]"
                    "/bv*[height<=1080]+ba[ext=m4a]/b[height<=1080]/bv*[height<=1080]+ba")

    判定本身在 `_plan_language_injection()` 里（纯函数、五条路径全收口），这里只负责
    emit 一条 `kind=decision subsystem=audio`：**"我设了日语音轨偏好，为什么下到的还是
    英语"只有在这里才答得上来** —— 真正决定成败的是 `[language^=ja]` 到底有没有进 `-f`。
    三种"没注入"的成因里，`explicit_multistream_ids` 是正常的（音轨已按 ID 点选），
    其余两种都值得看一眼。

    Args:
        langs: 已规范化的语言偏好（`youtube_service._resolve_preferred_audio_langs()`）。
        strategy: `original_first` / `language_first` / `original_only`。
        multistreams: `audio_multistreams` 是否开启。开着就**故意不注入** ——
            多音轨模式下格式串是显式的 `v+a1+a2` ID，插过滤器会把它改坏。
    """
    plan = _plan_language_injection(fmt, langs, strategy, multistreams=multistreams)
    emit_event(
        "decision",
        trace=current_flow(),
        # 每次下载一条，播放列表逐个视频一条 —— 只进文件与 JSONL，不刷控制台。
        level="DEBUG",
        stage="select",
        subsystem="audio",
        injected=plan.injected,
        reason=plan.reason,
        langs=plan.langs or None,
        strategy=plan.strategy,
    )
    return plan.fmt


@dataclass(frozen=True)
class _LangInjectionPlan:
    """`_plan_language_injection()` 的判定结果。

    `fmt` 是最终格式串，其余四个字段纯粹为了日志 —— 三种 `injected=False` 的成因在
    命令行上长得一模一样（都是"没有过滤器"），但一个是正常、两个是问题。`reason` 在
    注入成功时还要分清注了语言、注了原音、还是两者都注了："原音优先但这个视频没有
    原音标记"和"压根没注入"是两件事。
    """

    fmt: str
    langs: list[str]
    injected: bool
    reason: str
    strategy: str


def _language_filters(langs: list[str]) -> list[str]:
    """每个语言偏好展开成若干过滤器表达式，按优先级排列。

    偏好本身用 `^=`（startswith）：偏好 `en` 要能命中真实标注 `en-US`，这是旧的
    `[language=en]` 在带地区标注的视频上拿不到任何音轨的直接原因。写法走
    `bcp47.canonicalize()`，与 YouTube 的标注（`en-US`、`zh-Hans`）一致。

    别名分支用 `=`（精确）而**不是** `^=`：别名表里有把 `zh-Hans` 放宽到 `zh` 的条目，
    `[language^=zh]` 会连 `zh-Hant` 一起命中 —— 简中偏好换来一条繁中音轨。
    精确匹配一条真的标着 `zh` 的音轨才是这个别名想表达的意思。
    """
    from ..utils import bcp47

    exprs: list[str] = []
    for lang in langs:
        canon = bcp47.canonicalize(lang)
        if not bcp47.is_safe_tag(canon):
            # 非法字符会跑进命令行 —— 生产端已经挡过一层，这里是兜底
            continue
        candidates = [f"[language^={canon}]"]
        for alias in sorted(bcp47.BCP47_ALIASES.get(bcp47.normalize(canon), set())):
            candidates.append(f"[language={bcp47.canonicalize(alias)}]")
        for expr in candidates:
            if expr not in exprs:
                exprs.append(expr)
    return exprs


def _apply_audio_filter(alternatives: list[str], expr: str) -> str:
    """把一个过滤器表达式贴到每条候选的**音频侧**，返回一组 `/` 串起来的分支。"""
    out: list[str] = []
    for alt in alternatives:
        if "+" in alt:
            # 合并式 video+audio：过滤器只贴音频那一半
            head, tail = alt.split("+", 1)
            out.append(f"{head}+{tail}{expr}")
        else:
            # 单格式（已混流或纯音频）：整条贴
            out.append(f"{alt}{expr}")
    return "/".join(out)


def _plan_language_injection(
    fmt: str,
    langs: list[str] | None,
    strategy: str | None,
    *,
    multistreams: bool,
) -> _LangInjectionPlan:
    """纯函数：算出注入后的格式串，以及注入了 / 没注入以及为什么。

    五条路径全部收口在这里（包括 `multistreams` 那条"故意不注入"，它原先是调用点上的
    一个 `if` + 注释），所以 `_inject_language_into_format()` 只需要一个 emit 点，
    也就不存在"第六条路径忘了记日志"。

    分支顺序就是策略的全部体现：

    | 策略 | 分支顺序 |
    | --- | --- |
    | `original_first` / `original_only` | 原音 → 语言 → 原始格式串 |
    | `language_first` | 语言 → 原始格式串（不注原音） |

    `original_only` 与 `original_first` 在这条路径上**产出相同的格式串**：格式串是
    "按顺序试，第一条能满足的就用"，表达不了"只要原音，没有就报错"，而硬要表达就得
    去掉兜底 —— 那会让没有原音标记的视频一条格式都选不出来（合并直接失败）。这与
    `format_scorer` 那边"原音命中即赢、无原音则退化为语言优先"的取舍是同一个。
    """
    from ..utils.format_scorer import (
        AUDIO_STRATEGIES,
        STRATEGY_LANGUAGE_FIRST,
        STRATEGY_ORIGINAL_FIRST,
    )

    # 认不出的值按默认策略走，并且**报告规范化后的名字**：`want_original` 那行是
    # `!= language_first`，垃圾值本来就已经在按 original_first 的行为跑了 ——
    # 原样回传只会让日志里出现一个 `strategy=nonsense` 而行为对不上它。
    strategy_name = str(strategy or "").strip()
    if strategy_name not in AUDIO_STRATEGIES:
        strategy_name = STRATEGY_ORIGINAL_FIRST
    # 去重保序：重复偏好只会生成一份重复的分支组，纯粹让格式串变长。
    # 生产端（`youtube_service`）已经去过重，这里是兜底。
    deduped: list[str] = []
    for item in langs or []:
        text = str(item).strip()
        if text and text not in deduped:
            deduped.append(text)

    want_original = strategy_name != STRATEGY_LANGUAGE_FIRST

    if not fmt:
        return _LangInjectionPlan(fmt, deduped, False, "no_format_string", strategy_name)

    if not deduped and not want_original:
        # 语言优先 + 空偏好 = 压根没请求过什么 —— 不是"注入失败"，没什么可注入的
        return _LangInjectionPlan(fmt, [], False, "no_language_request", strategy_name)

    if multistreams:
        # 显式多音轨 ID（`v+a1+a2`）：音轨已经按 ID 点选完了，语言偏好是多余的，
        # 插过滤器只会把那串 ID 改坏
        return _LangInjectionPlan(fmt, deduped, False, "explicit_multistream_ids", strategy_name)

    alternatives = [a.strip() for a in fmt.split("/") if a.strip()]
    if not alternatives:
        return _LangInjectionPlan(fmt, deduped, False, "unparsable_format_string", strategy_name)

    lang_exprs = _language_filters(deduped)
    ordered = ([_ORIGINAL_AUDIO_FILTER] if want_original else []) + lang_exprs

    groups = [g for g in (_apply_audio_filter(alternatives, e) for e in ordered) if g]
    if not groups:
        return _LangInjectionPlan(fmt, deduped, False, "unparsable_format_string", strategy_name)

    if want_original and lang_exprs:
        reason = "injected_original_and_language"
    elif want_original:
        reason = "injected_original"
    else:
        reason = "injected_language"

    # 原始格式串留在最后兜底：上面每一条都可能在某个视频上选不出格式
    return _LangInjectionPlan("/".join(groups) + "/" + fmt, deduped, True, reason, strategy_name)


def build_subtitle_args(ydl_opts: dict[str, Any], *, allow_embed: bool = True) -> list[str]:
    """把字幕相关的 opts 翻译成 CLI 参数。

    `ydl_opts_to_cli_args()` 和 `workers.py::_run_lightweight_extract()` 共用这一份 ——
    后者原本自己抄了一遍（还用的是 `--write-subs` 复数别名），于是这次修复给
    `--sub-langs` 加的东西只在完整下载路径上生效，纯字幕模式还在走老逻辑。
    字幕参数是本次修复的核心，两份实现意味着 bug 只修一半。

    Args:
        allow_embed: `--embed-subs` / `--keep-subs` 是否可发。纯字幕模式带
            `--skip-download`，压根没有视频文件可嵌，那两个参数在那里没有意义。
    """
    args: list[str] = []

    # 写入字幕
    if ydl_opts.get("writesubtitles"):
        args += ["--write-sub"]
    elif ydl_opts.get("writesubtitles") is False:
        # 显式禁用：覆盖外部 yt-dlp 配置中可能存在的 --write-sub
        args += ["--no-write-sub"]

    # 写入自动字幕
    if ydl_opts.get("writeautomaticsub"):
        args += ["--write-auto-sub"]
    elif ydl_opts.get("writeautomaticsub") is False:
        # 显式禁用：覆盖外部 yt-dlp 配置中可能存在的 --write-auto-sub
        args += ["--no-write-auto-sub"]

    # 字幕语言。这里的每一项都必须是**真实字幕键**或锚定正则，不能是用户偏好 ——
    # 裸 `en` 匹配不到 `en-GB`，解析在 `utils/bcp47.py` 里做完。
    subtitleslangs = ydl_opts.get("subtitleslangs")
    if isinstance(subtitleslangs, (list, tuple)) and subtitleslangs:
        args += ["--sub-langs", ",".join(str(lang) for lang in subtitleslangs)]
    elif isinstance(subtitleslangs, str) and subtitleslangs:
        args += ["--sub-langs", subtitleslangs]

    if allow_embed:
        # 嵌入字幕
        if ydl_opts.get("embedsubtitles"):
            args += ["--embed-subs"]

        # 保留外置字幕文件
        # `--embed-subs` 嵌入完会顺手删掉外置字幕文件，字幕后处理因此永远校验不到东西
        # （旧版那句"未找到字幕文件"有一半来自这里）。置位方在
        # `download/features.py::SubtitleFeature.on_download_start`，校验完由后处理清理。
        if ydl_opts.get("keepsubtitles"):
            args += ["--keep-subs"]

    # 字幕格式转换 (显式指定的情况下)
    convert_subs = ydl_opts.get("convertsubtitles")
    if isinstance(convert_subs, str) and convert_subs:
        args += ["--convert-subs", convert_subs]

    return args


def ydl_opts_to_cli_args(ydl_opts: dict[str, Any]) -> list[str]:
    """Convert a subset of yt-dlp Python options to CLI args.

    This mapping is intentionally minimal and only covers what the app uses.
    """

    args: list[str] = []

    proxy = ydl_opts.get("proxy")
    if isinstance(proxy, str):
        args += ["--proxy", proxy]

    user_agent = ydl_opts.get("user_agent")
    if isinstance(user_agent, str) and user_agent:
        args += ["--user-agent", user_agent]

    for key, flag in [
        ("socket_timeout", "--socket-timeout"),
        ("retries", "--retries"),
        ("fragment_retries", "--fragment-retries"),
        ("sleep_interval", "--sleep-interval"),
        ("max_sleep_interval", "--max-sleep-interval"),
        ("concurrent_fragment_downloads", "-N"),  # 并发分片数
    ]:
        v = ydl_opts.get(key)
        if isinstance(v, (int, float)) and int(v) > 0:
            args += [flag, str(int(v))]

    # 外部下载器
    external_downloader = ydl_opts.get("external_downloader")
    if isinstance(external_downloader, str) and external_downloader:
        args += ["--downloader", external_downloader]

    # 外部下载器参数
    external_downloader_args = ydl_opts.get("external_downloader_args")
    if isinstance(external_downloader_args, dict):
        for dl_name, dl_args in external_downloader_args.items():
            if isinstance(dl_args, list):
                args += ["--downloader-args", f"{dl_name}:{' '.join(dl_args)}"]
            elif isinstance(dl_args, str):
                args += ["--downloader-args", f"{dl_name}:{dl_args}"]

    # 下载限速
    ratelimit = ydl_opts.get("ratelimit")
    if isinstance(ratelimit, (int, float)) and ratelimit > 0:
        args += ["--limit-rate", f"{int(ratelimit)}"]
    elif isinstance(ratelimit, str) and ratelimit:
        args += ["--limit-rate", ratelimit]

    # Cookie 统一通过文件传递（由 AuthService 处理）
    # 不再支持 --cookies-from-browser，避免文件锁问题
    cookiefile = ydl_opts.get("cookiefile")
    if isinstance(cookiefile, str) and cookiefile:
        args += ["--cookies", cookiefile]

    js_runtimes = ydl_opts.get("js_runtimes")
    if isinstance(js_runtimes, dict):
        # yt-dlp CLI: --js-runtimes RUNTIME[:PATH]
        # Example: {"deno": {"path": "C:/.../deno.exe"}}
        for runtime_id, cfg in js_runtimes.items():
            rid = str(runtime_id or "").strip()
            if not rid:
                continue
            path = ""
            if isinstance(cfg, dict):
                path = str(cfg.get("path") or "").strip()
            elif isinstance(cfg, str):
                path = cfg.strip()
            value = f"{rid}:{path}" if path else rid
            args += ["--js-runtimes", value]

    ffmpeg_location = ydl_opts.get("ffmpeg_location")
    if isinstance(ffmpeg_location, str) and ffmpeg_location.strip():
        args += ["--ffmpeg-location", ffmpeg_location.strip()]

    extractor_args = ydl_opts.get("extractor_args")
    if isinstance(extractor_args, dict):
        # Example (python API):
        # {"youtube": {"player_client": ["android,ios"], "player_skip": ["js,configs,hls"]}}
        from loguru import logger

        logger.debug("[CLI] extractor_args 键: {}", sorted(extractor_args.keys()))
        for ie_key, ie_args in extractor_args.items():
            if not ie_key:
                continue
            if not isinstance(ie_args, dict):
                continue
            parts: list[str] = []
            for k, v in ie_args.items():
                if not k:
                    continue
                if isinstance(v, (list, tuple)):
                    flat = [str(x) for x in v if str(x).strip()]
                    if not flat:
                        continue
                    val = ",".join(flat)
                else:
                    val = str(v)
                val = val.strip()
                if not val:
                    continue
                parts.append(f"{k}={val}")
            if parts:
                # See yt-dlp CLI: --extractor-args IE_KEY:ARGS, where ARGS is semicolon-separated.
                extractor_arg = f"{ie_key}:{';'.join(parts)}"
                # 只落**脱敏后**的一行。这里以前有四条 DEBUG（输入 dict、逐 key 的
                # `v`/`val`、`parts`、最终值），每一条都把 `po_token=<用户的完整 Token>`
                # 原样写进日志 —— 而文件 sink 是 DEBUG 级，那是真的落盘了。
                # `log_pot_in_argv` 的注释早就写明"只记 base_url，绝不记 Token"，
                # 这条路却整个绕过了它。四条内容本来就互相重复，留一条脱敏的信息量不减。
                logger.debug("[CLI] 添加参数: --extractor-args {}", mask_secrets(extractor_arg))
                args += ["--extractor-args", extractor_arg]

    outtmpl = ydl_opts.get("outtmpl")
    if isinstance(outtmpl, str) and outtmpl:
        args += ["-o", outtmpl]

    paths = ydl_opts.get("paths")
    if isinstance(paths, dict):
        # Support {'home': '...'} or {'temp': '...'}
        # CLI -P supports setting home path.
        home = paths.get("home")
        if isinstance(home, str) and home.strip():
            args += ["-P", home.strip()]
        # Note: yt-dlp CLI allows multiple -P, e.g. -P "temp:..."
        temp = paths.get("temp")
        if isinstance(temp, str) and temp.strip():
            args += ["-P", f"temp:{temp.strip()}"]

    fmt = ydl_opts.get("format")
    if isinstance(fmt, str) and fmt:
        # 语言 / 原音过滤器注入。`audio_multistreams`（显式 `v+a1+a2` ID）时故意不注入 ——
        # 这个"不注入"的决定连同其余四条路径一起收口在 `_plan_language_injection()`，
        # 所以它现在也会留下日志，而不是在这里被一个 `if` 悄悄跳过。
        #
        # 意图从 `_fytdl_audio_langs` / `_fytdl_audio_strategy` 读，**不是**从
        # `format_sort` 里抠 `lang:` 前缀：那些条目在 yt-dlp 的排序器里从来无效
        # （§4 规则 4），现在也不再生成了。
        fmt = _inject_language_into_format(
            fmt,
            ydl_opts.get("_fytdl_audio_langs"),
            ydl_opts.get("_fytdl_audio_strategy"),
            multistreams=bool(ydl_opts.get("audio_multistreams")),
        )
        args += ["-f", fmt]

    # 格式排序（音轨语言偏好等）
    format_sort = ydl_opts.get("format_sort")
    if isinstance(format_sort, list) and format_sort:
        args += ["-S", ",".join(str(x) for x in format_sort)]
    elif isinstance(format_sort, str) and format_sort:
        args += ["-S", format_sort]

    # format_sort_force: 强制排序覆盖用户提供的格式
    if ydl_opts.get("format_sort_force"):
        args += ["--format-sort-force"]

    merge_fmt = ydl_opts.get("merge_output_format")
    if isinstance(merge_fmt, str) and merge_fmt:
        args += ["--merge-output-format", merge_fmt]

    if ydl_opts.get("audio_multistreams"):
        args += ["--audio-multistreams"]

    # ========== top-level audio extract params (simple/playlist mode) ==========
    # extract_audio / audio_format / audio_quality as top-level keys
    if ydl_opts.get("extract_audio"):
        args += ["--extract-audio"]
        audio_fmt = ydl_opts.get("audio_format")
        if isinstance(audio_fmt, str) and audio_fmt:
            args += ["--audio-format", audio_fmt]
        audio_quality = ydl_opts.get("audio_quality")
        if isinstance(audio_quality, str) and audio_quality:
            args += ["--audio-quality", audio_quality]
        elif isinstance(audio_quality, (int, float)):
            args += ["--audio-quality", str(audio_quality)]

    if ydl_opts.get("addmetadata") is True:
        args += ["--add-metadata"]

    # 封面缩略图下载
    if ydl_opts.get("writethumbnail") is True:
        args += ["--write-thumbnail"]

    # 转换封面格式（用于嵌入）
    convert_thumbnail_format = ydl_opts.get("convert_thumbnail")
    if isinstance(convert_thumbnail_format, str) and convert_thumbnail_format:
        args += ["--convert-thumbnails", convert_thumbnail_format]

    # Postprocessors handling
    postprocessors = ydl_opts.get("postprocessors")
    if isinstance(postprocessors, list):
        has_embed_metadata = False

        for pp in postprocessors:
            if not isinstance(pp, dict):
                continue
            key = str(pp.get("key") or "").strip()

            # 音频提取
            if key == "FFmpegExtractAudio":
                codec = str(pp.get("preferredcodec") or "mp3").strip() or "mp3"
                quality = str(pp.get("preferredquality") or "192").strip() or "192"
                args += [
                    "--extract-audio",
                    "--audio-format",
                    codec,
                    "--audio-quality",
                    f"{quality}K",
                ]

            # 封面嵌入 - 注意：现在由外置工具处理，yt-dlp 只负责下载封面
            elif key == "EmbedThumbnail":
                # 不再使用 yt-dlp 内置的封面嵌入，改用外置 AtomicParsley/FFmpeg
                pass

            # 元数据嵌入
            elif key == "FFmpegMetadata":
                has_embed_metadata = True

            # 封面格式转换（备用方式）
            elif key == "FFmpegThumbnailsConvertor":
                fmt = str(pp.get("format") or "jpg").strip()
                if fmt and "--convert-thumbnails" not in args:
                    args += ["--convert-thumbnails", fmt]

        # 注意：封面嵌入现在由外置工具 (AtomicParsley/FFmpeg) 处理
        # yt-dlp 只负责下载封面（通过 writethumbnail 选项）
        # 不再添加 --embed-thumbnail 参数

        # 添加元数据嵌入参数
        if has_embed_metadata:
            args += ["--embed-metadata"]

    # 后处理器参数（如 loudnorm 音量标准化）
    postprocessor_args = ydl_opts.get("postprocessor_args")
    if isinstance(postprocessor_args, dict):
        for pp_name, pp_args in postprocessor_args.items():
            if isinstance(pp_args, list):
                # CLI 格式: --postprocessor-args NAME:ARGS
                args += ["--postprocessor-args", f"{pp_name}:{' '.join(pp_args)}"]
            elif isinstance(pp_args, str):
                args += ["--postprocessor-args", f"{pp_name}:{pp_args}"]

    # ========== 字幕相关参数 ==========

    args += build_subtitle_args(ydl_opts)

    # ========== 片段下载参数 ==========

    # 下载片段
    download_sections = ydl_opts.get("download_sections")
    if isinstance(download_sections, str) and download_sections:
        args += ["--download-sections", download_sections]

    # 强制关键帧切割
    if ydl_opts.get("force_keyframes_at_cuts"):
        args += ["--force-keyframes-at-cuts"]

    # ========== SponsorBlock 参数 ==========

    # 移除片段
    sponsorblock_remove = ydl_opts.get("sponsorblock_remove")
    if isinstance(sponsorblock_remove, list) and sponsorblock_remove:
        for cat in sponsorblock_remove:
            args += ["--sponsorblock-remove", cat]
    elif isinstance(sponsorblock_remove, str) and sponsorblock_remove:
        args += ["--sponsorblock-remove", sponsorblock_remove]

    # 标记片段
    sponsorblock_mark = ydl_opts.get("sponsorblock_mark")
    if isinstance(sponsorblock_mark, list) and sponsorblock_mark:
        for cat in sponsorblock_mark:
            args += ["--sponsorblock-mark", cat]
    elif isinstance(sponsorblock_mark, str) and sponsorblock_mark:
        args += ["--sponsorblock-mark", sponsorblock_mark]

    # 嵌入章节
    if ydl_opts.get("embed_chapters"):
        args += ["--embed-chapters"]

    # 跳过下载（仅获取元数据/字幕/封面等）
    if ydl_opts.get("skip_download"):
        args += ["--skip-download"]

    # 强制不下载整个播放列表
    if ydl_opts.get("noplaylist"):
        args += ["--no-playlist"]

    # NOTE: POT Token / POT Provider 的 extractor_args 已在 youtube_service.build_ydl_options() 中统一处理
    # 无需在此处再次添加

    return args


def _terminate_process_best_effort(proc: subprocess.Popen[str]) -> None:
    try:
        proc.terminate()
    except Exception:
        return
    try:
        proc.wait(timeout=1.0)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _extract_error_lines(output: str) -> str:
    """Extract lines starting with ERROR: or WARNING: to form a concise stderr."""
    lines = []
    for line in output.splitlines():
        if line.startswith("ERROR:") or line.startswith("WARNING:"):
            lines.append(line)
    if not lines:
        # If no explicit error lines, return the last 1500 chars
        return output[-1500:].strip()
    return "\n".join(lines)


def run_dump_single_json(
    url: str,
    ydl_opts: dict[str, Any],
    extra_args: list[str] | None = None,
    *,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    exe = resolve_yt_dlp_exe()
    if exe is None:
        raise FileNotFoundError("未找到 yt-dlp.exe（既没有内置也不在 PATH 中）")

    cmd = [
        str(exe),
        "--no-color",
        "--no-progress",
        "-J",
        *ydl_opts_to_cli_args(ydl_opts),
    ]
    if extra_args:
        cmd += list(extra_args)
    cmd.append(url)

    from loguru import logger

    log_pot_in_argv(cmd, stage="Parse")

    _t_env = time.perf_counter()
    env = prepare_yt_dlp_env()
    work_dir = _safe_working_dir()
    _env_ms = (time.perf_counter() - _t_env) * 1000
    _t_proc = time.perf_counter()

    def _safe_decode(b: bytes | None) -> str:
        if not b:
            return ""
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            import locale

            fallback = locale.getpreferredencoding()
            try:
                return b.decode(fallback)
            except UnicodeDecodeError:
                return b.decode("utf-8", errors="replace")

    if cancel_event is None:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            env=env,
            cwd=work_dir,
            **_win_hide_console_kwargs(),
        )

        out = _safe_decode(proc.stdout) + "\n" + _safe_decode(proc.stderr)
        if proc.returncode != 0:
            stderr_snippet = _extract_error_lines(out)
            raise YtDlpExecutionError(proc.returncode, stderr_snippet)
    else:
        proc2 = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=work_dir,
            **_win_hide_console_kwargs(),
        )

        # 使用独立的取消监控线程，communicate() 只调用一次
        import threading as _threading

        def _cancel_watcher():
            """后台监控 cancel_event，触发时终止子进程"""
            while proc2.poll() is None:
                if cancel_event.is_set():
                    _terminate_process_best_effort(proc2)
                    return
                cancel_event.wait(timeout=0.2)

        watcher = _threading.Thread(target=_cancel_watcher, daemon=True)
        watcher.start()

        try:
            stdout_bytes, stderr_bytes = proc2.communicate()
        except Exception as e:
            _terminate_process_best_effort(proc2)
            if cancel_event.is_set():
                raise YtDlpCancelled("yt-dlp cancelled") from e
            raise
        finally:
            watcher.join(timeout=1.0)

        if cancel_event.is_set():
            raise YtDlpCancelled("yt-dlp cancelled")

        out = _safe_decode(stdout_bytes) + "\n" + _safe_decode(stderr_bytes)
        if proc2.returncode != 0:
            stderr_snippet = _extract_error_lines(out)
            raise YtDlpExecutionError(proc2.returncode, stderr_snippet)

    _proc_ms = (time.perf_counter() - _t_proc) * 1000
    log_pot_from_output(out, stage="Parse")

    # rc == 0 —— 解析成功了，但输出里可能躺着 nsig 降级 / PO Token 被拒 /
    # "请求的语言没有字幕" 这类征兆。以前这段只有 POT 行被捞走，其余全丢，于是
    # "解析成功但结果不对"在日志里没有任何痕迹。
    #
    # 落 `signal` 而**不是** `diagnosis`：`diagnose()` 是失败边界的主因仲裁，
    # 在 rc=0 上调它会让 `count(kind=diagnosis)` 不再等于"真正发生了错误"（硬规则 2）。
    #
    # trace 取本线程的环境 flow：解析链路要穿过 `youtube_service` 十几个方法才到这里，
    # 为一条日志给整个服务层加贯穿参数不值得（见 `observability/trace.py` 的说明）。
    emit_success_signals(
        out,
        trace=current_flow(),
        stage="parse",
        operation="dump_single_json",
    )

    # yt-dlp may print other lines; pick the last parsable JSON line.
    _t_json = time.perf_counter()
    for line in reversed(out.splitlines()):
        s = line.strip()
        if not s:
            continue
        if not (s.startswith("{") or s.startswith("[")):
            continue
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                logger.info(
                    "[Timing][run_dump_single_json] env={:.0f}ms 子进程={:.0f}ms JSON={:.0f}ms 输出={}行",
                    _env_ms,
                    _proc_ms,
                    (time.perf_counter() - _t_json) * 1000,
                    len(out.splitlines()),
                )
                return data
        except Exception:
            continue

    stderr_snippet = _extract_error_lines(out)
    raise YtDlpExecutionError(1, f"yt-dlp 未输出可解析的 JSON\n{stderr_snippet}")


def run_version() -> str:
    exe = resolve_yt_dlp_exe()
    if exe is None:
        return ""
    try:
        out = subprocess.check_output(
            [str(exe), "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            **_win_hide_console_kwargs(),
        )
        return (out or "").strip()
    except Exception:
        return ""
