"""
执行器模块

单管线执行器：使用 yt-dlp Native Pipeline。
负责实际的子进程管理、进度转发、容器决策和后处理编排。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.message_catalog import english

from ..diagnostics.collect import DiagnosticLineCollector
from ..models.errors import YtDlpExecutionError
from ..observability import (
    current_flow,
    emit_event,
    emit_success_signals,
    render_argv,
    sanitize_path,
    should_keep_raw_line,
)
from ..utils.container_compat import choose_lossless_merge_container
from ..utils.disk_space import check_space_for_download
from ..youtube.yt_dlp_cli import (
    log_pot_from_output,
    log_pot_in_argv,
    prepare_yt_dlp_env,
    resolve_yt_dlp_exe,
    ydl_opts_to_cli_args,
)
from .output_parser import EMBED_EVIDENCE_BY_PP, YtDlpOutputParser

# 字幕/封面等附属文件后缀，不应被视为主输出文件
_AUXILIARY_EXTENSIONS = frozenset(
    {
        ".vtt",
        ".srt",
        ".ass",
        ".ssa",
        ".sub",
        ".lrc",  # 字幕
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",  # 封面
        ".json",
        ".description",
        ".txt",  # 元数据
    }
)


def _is_auxiliary_file(path: str) -> bool:
    """判断路径是否为附属文件（字幕、封面、元数据等），不应作为主输出路径。"""
    ext = os.path.splitext(path)[1].lower()
    return ext in _AUXILIARY_EXTENSIONS


def _mark_recovered(trace: Any, reason: str) -> None:
    """把"某个异常被容忍了"这个事实钉到 trace 上，让 run 的终态边界能读到。

    executor 只**决定**"rc≠0 但产物有效"这件事，`recovered` 标志却要一路带到
    `DownloadWorker.run()` 的 `finish()` 里去（硬规则 4：outcome 只属于 run 终态）。
    下载路径上 `current_flow()` 拿到的正是那个 worker 的 `TaskTrace`，它有
    `mark_recovered`；解析路径拿到的是纯 `FlowTrace`（没有 run，也就没有终态），
    `hasattr` 保护掉后者即可，缺了它只是少一条 recovery，不影响下载判定。
    """
    fn = getattr(trace, "mark_recovered", None)
    if fn is None:
        return
    try:
        fn(reason)
    except Exception:
        pass  # 硬规则 5：观测失败绝不回传业务层


#: 预检时要求的最小剩余空间（GB）。**这个数字不用来拦下载**，只用来决定日志级别。
#:
#: 开跑前的体积预估根本不可靠（DASH 分流各算一份、合并产物、后处理临时文件都不在
#: `filesize_approx` 里），拿它当门禁只会误杀。所以它是"剩这么点该被看见"的门槛，
#: 不是"不许下载"的门槛。
_PREFLIGHT_MIN_FREE_GB = 1.0


def _emit_disk_space_signal(output_dir: str) -> None:
    """`Popen` 之前落一条 `kind=signal stage=preflight subsystem=disk`。

    **只报告，绝不阻止下载**（`utils/disk_space.py` 模块 docstring 讲了为什么）。
    真正的判定留给 yt-dlp 自己的写盘错误 —— 这条事件的全部作用是让那个错误
    **有上下文**：`code=no_space_left` 落地时，往上翻一屏就能看到开跑前只剩 200 MB，
    而不是对着一句"写入失败"猜是不是磁盘满了。

    三个 code 分得很清，因为它们对应三种完全不同的下一步：

    - ``disk_space_unknown`` —— 探测本身失败（路径不存在 / 无权限）。**我们瞎了**，
      不是磁盘满了。两者在 `SpaceCheckResult` 里都表现为
      `sufficient=False, available_bytes=0`，混成一个 code 就永远分不出来。
    - ``disk_space_low`` —— 确实低于门槛。WARNING 级，要冒到控制台。
    - ``disk_space_ok`` —— DEBUG 级，进文件与 JSONL 不刷控制台。它存在的唯一理由是
      给"磁盘满"排除法提供反证：没有这条，"日志里没提磁盘"既可能是空间充足，
      也可能是这段代码压根没跑。

    不记 `result.message` —— 那是本地化文案，写进日志会让内容随界面语言变化、搜不着。
    """
    try:
        result = check_space_for_download(output_dir, 0, min_free_gb=_PREFLIGHT_MIN_FREE_GB)
        if result.error:
            code, level = "disk_space_unknown", "WARNING"
        elif not result.sufficient:
            code, level = "disk_space_low", "WARNING"
        else:
            code, level = "disk_space_ok", "DEBUG"
        emit_event(
            "signal",
            trace=current_flow(),
            level=level,
            stage="preflight",
            subsystem="disk",
            code=code,
            # 目录本身也是线索（哪个盘、哪个子目录），脱敏只折掉用户名前缀。
            dir=sanitize_path(output_dir),
            available_bytes=result.available_bytes,
            required_bytes=result.required_bytes,
            shortfall_bytes=result.shortfall_bytes or None,
            # 探测失败时记异常类名，不记那句拼了路径的中文 message。
            probe_error=result.error or None,
        )
    except Exception:
        # 硬规则 5：观测永远 best-effort，绝不能让一条日志挡掉下载。
        pass


# 有效媒体文件的最小大小门槛（10 KB）
# 低于此大小的文件几乎不可能是有效的音视频
_MIN_VALID_MEDIA_BYTES = 10 * 1024


# ── 回调协议 ──────────────────────────────────────────────


class ProgressCallback(Protocol):
    def __call__(self, data: dict[str, Any]) -> None: ...


class StatusCallback(Protocol):
    def __call__(self, message: str) -> None: ...


class CancelCheck(Protocol):
    def __call__(self) -> bool: ...


class PathCallback(Protocol):
    def __call__(self, path: str) -> None: ...


class FileCreatedCallback(Protocol):
    """ "yt-dlp 报告创建了一个文件"的唯一通道。

    `role` 是 yt-dlp **自己声明**的角色（`media` / `subtitle` / `thumbnail`），
    `None` 表示报告者没说 —— 见 `output_parser.ParsedLine.role`。消费方
    （Step 4 起是 `StagingArea`）据此决定走 `add_reported(kind=role)` 还是留给
    `reconcile()` 那唯一一次兜底分类；**不许自己按后缀补一个角色出来**。
    """

    def __call__(self, path: str, role: str | None = None) -> None: ...


class EmbedEvidenceCallback(Protocol):
    """结构化的"嵌入确实成功了"证据通道，取值 `subtitle` / `thumbnail`。

    只在 `postprocessor_status == "finished"` 时被调用。这是删除外挂字幕的唯一
    合法依据（见《下载产物事务层》「删除只有一个理由」）—— 人类日志行只作显示。
    """

    def __call__(self, kind: str) -> None: ...


class PartsProbe(Protocol):
    """`--download-sections` 时的进度兜底探针：返回**此刻**半成品的字节总数。

    FFmpegFD 不会把子 ffmpeg 的进度转发给 yt-dlp 的 stdout，所以那段时间里唯一
    能证明"还在下"的证据是半成品文件在长大。以前这里是 `Path(work_dir).glob("*.part")`
    —— 而 `work_dir` 是 `paths["home"]`，`home != temp` 之后那里**永远是 0 字节**，
    进度条会静默卡死。所以扫描收敛到事务层（`StagingArea.parts_bytes()`，唯一允许
    物理扫描的地方），executor 只拿一个数字，不知道也不需要知道文件在哪。

    实现方必须自己吞掉 `OSError`（沙盒可能已被 `rmtree`）—— 它跑在守护线程里，
    抛出去没人接。
    """

    def __call__(self) -> int: ...


# ── 容器决策 ──────────────────────────────────────────────

_SUBTITLE_COMPATIBLE_CONTAINERS = {"mp4", "mkv", "mov", "m4v"}


def determine_merge_container(
    ydl_opts: dict[str, Any],
    video_ext: str | None = None,
    audio_ext: str | None = None,
) -> str:
    """确定最终输出容器格式。

    优先级:
    1. ydl_opts["merge_output_format"] — 用户/预设已指定
    2. 字幕兼容性修正 — webm 不支持 SRT/ASS → mkv
    3. choose_lossless_merge_container(v_ext, a_ext)
    4. 兜底 mkv
    """
    merge_fmt = (ydl_opts.get("merge_output_format") or "").strip().lower()

    if merge_fmt:
        # 字幕兼容性检查
        if ydl_opts.get("embedsubtitles") and merge_fmt == "webm":
            log_text(logger, "info", "[Executor] 字幕嵌入 + webm → 强制 mkv")
            return "mkv"
        return merge_fmt

    # 没有指定容器 → 根据流的 ext 推断
    computed = choose_lossless_merge_container(video_ext, audio_ext)
    if computed:
        if ydl_opts.get("embedsubtitles") and computed not in _SUBTITLE_COMPATIBLE_CONTAINERS:
            log_text(logger, "info", "[Executor] 字幕嵌入 + {} → 强制 mkv", computed)
            return "mkv"
        return computed

    return "mkv"


# ── Win32 工具 ────────────────────────────────────────────


def _win_hide_kwargs() -> dict[str, Any]:
    """Windows: 隐藏子进程窗口。"""
    kw: dict[str, Any] = {}
    if os.name != "nt":
        return kw
    try:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0
        kw["startupinfo"] = si
    except Exception:
        pass
    return kw


# ── 执行器 ────────────────────────────────────────────────


class DownloadExecutor:
    """原生下载执行器 (Native Only)。

    仅使用 yt-dlp 原生管线。
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[Any] | None = None
        # 本次执行的 cookie 运行副本上下文（见 `cookie_runfile`）。yt-dlp 每次运行结束都会
        # 把 jar 回写进 `--cookies` 文件，所以绝不把真相源直接交给它；副本随子进程生命周期
        # 结束而删除——其生命周期恰等于 `self._proc`，两处 `self._proc = None` 各配一次释放。
        self._cookie_stack: ExitStack | None = None
        self._ytdlp_parser = YtDlpOutputParser()
        # 诊断行缓冲：失败时作为 `diagnose()` 的输入，成功时供再跑一次 `parse_events`
        # —— 字幕缺失不会让任务失败，而 `diagnose()` 只在 rc != 0 时跑。
        # 不能复用 `_execute_native` 里的 `tail`：那个 maxlen=120 且混着进度行，
        # 一段稍长的下载就能把开头的警告挤出去。
        # "哪些行算诊断线索"的判据集中在 `diagnostics/collect.py`，与规则表同步；
        # 尤其是 `[download] ... Skipping` 这类无级别前缀的行，靠它才能被捞到。
        self.diag_lines = DiagnosticLineCollector()
        # raw 输出缓冲：`diag_lines` 是**筛过**的行（只留诊断线索），失败复现时往往还需要
        # 它周围那些上下文 —— 用了哪个 client、走了哪条 format、ffmpeg 的完整命令行。
        # 2000 行足够覆盖一次下载的非进度输出；只在 failed / degraded / recovered
        # （或用户开了 `log_raw_ytdlp`）时落盘，见 `sinks.dump_raw_for_outcome()`。
        self.raw_lines: deque[str] = deque(maxlen=2000)

    def execute(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        *,
        on_progress: ProgressCallback,
        on_status: StatusCallback,
        on_path: PathCallback,
        cancel_check: CancelCheck,
        on_file_created: FileCreatedCallback | None = None,
        on_embed_evidence: EmbedEvidenceCallback | None = None,
        final_paths_file: str | None = None,
        parts_probe: PartsProbe | None = None,
        cached_info_dict: dict[str, Any] | None = None,
    ) -> str | None:
        """执行下载，返回输出文件路径。

        Args:
            url: 视频 URL。
            ydl_opts: yt-dlp 选项字典。
            on_progress: 进度回调。
            on_status: 状态消息回调。
            on_path: 输出路径回调。
            cancel_check: 取消检查回调。
            on_file_created: 文件创建回调，带 yt-dlp 声明的 `role`（见
                `FileCreatedCallback`）。
            on_embed_evidence: 嵌入成功证据回调（见 `EmbedEvidenceCallback`）。
            final_paths_file: `--print-to-file after_move:filepath` 的落点（**绝对**
                路径，通常是 `StagingArea.prepare_attempt(n)` 给的
                `.fytdl/final.<n>.txt`）。给了才加这个参数。
            parts_probe: `--download-sections` 的进度兜底探针（见 `PartsProbe`）。
                不给就不开那个监视线程 —— 没有探针的兜底只会报 0 字节。
            cached_info_dict: (Optional) 预先提取的 info dict，避免重复提取。

        Returns:
            输出文件路径，或 None（如果失败）。

        Raises:
            RuntimeError: 子进程失败或取消。
        """
        # 总是使用原生管线
        return self._execute_native(
            url,
            ydl_opts,
            on_progress=on_progress,
            on_status=on_status,
            on_path=on_path,
            cancel_check=cancel_check,
            on_file_created=on_file_created,
            on_embed_evidence=on_embed_evidence,
            final_paths_file=final_paths_file,
            parts_probe=parts_probe,
            cached_info_dict=cached_info_dict,
        )

    # ── yt-dlp Native Pipeline ────────────────────────────

    def _execute_native(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        *,
        on_progress: ProgressCallback,
        on_status: StatusCallback,
        on_path: PathCallback,
        cancel_check: CancelCheck,
        on_file_created: FileCreatedCallback | None = None,
        on_embed_evidence: EmbedEvidenceCallback | None = None,
        final_paths_file: str | None = None,
        parts_probe: PartsProbe | None = None,
        label: str = "",
        cached_info_dict: dict[str, Any] | None = None,
    ) -> str | None:
        """yt-dlp 原生管线 — 与现有 _download_via_exe() 等效。"""
        exe = resolve_yt_dlp_exe()
        if exe is None:
            raise RuntimeError(english("yt-dlp 可执行文件未找到"))

        ydl_opts["skip_unavailable_fragments"] = True

        progress_prefix = "FLUENTYTDL|"
        cmd: list[str] = [
            str(exe),
            "--ignore-config",
            # 刻意**不加** `--no-warnings`：字幕限流、PO Token 缺失这些"任务成功但
            # 结果不对"的唯一线索都是 WARNING: 级，加上它就等于把原因扔掉，用户只剩
            # 一句"未找到字幕文件"（见 `FluentYTDL-字幕下载问题排查报告.md`）。
            # 代价是 WARNING 会进 `diagnose()`：`engine.py` 的护栏负责保证一条警告
            # 不会被当成任务的失败主因。
            "--no-color",
            "--newline",
            "--progress",
            "--progress-template",
            (
                "download:"
                + progress_prefix
                + "download|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s"
                + "|%(progress.speed)s|%(progress.eta)s"
                + "|%(info.vcodec)s|%(info.acodec)s|%(info.ext)s|%(progress.filename)s"
            ),
            "--progress-template",
            (
                "postprocess:"
                + progress_prefix
                + "postprocess|%(progress.status)s|%(progress.postprocessor)s"
            ),
        ]

        # 主媒体最终路径的**权威来源**：走文件不走控制台。
        #
        # 为什么不是 `-O/--print`：它的帮助文本明写 *"Implies --quiet"* —— 用它就等于
        # 掐死上面两条 `--progress-template`，整个进度解析随之失效。`--print-to-file`
        # 的帮助里**没有**这句（已对 v2026.08.30 的 `--help` 核实），而 `after_move`
        # 属 later-stage WHEN，所以也不隐含 `--simulate`。
        #
        # 为什么走文件：Windows 控制台代码页会把非 ASCII 路径吃掉字符，而这个值正是
        # 用来定位用户成品的 —— 丢一个字就等于定位不到。
        #
        # FILE 用的是 **output-template 语法**，相对路径会被解到 `paths.home`（=
        # `payload/`）里去，那会让控制文件掉进 `reconcile()` 的视野；所以这里传**绝对**
        # 路径，并把 `%` 转义成 `%%`。（「outtmpl 必须相对」那条纪律约束的是输出模板
        # 本身 —— 绝对 `-o` 会让 `-P` 整体失效 —— 不约束这个 FILE。）
        if final_paths_file:
            cmd += [
                "--print-to-file",
                "after_move:filepath",
                final_paths_file.replace("%", "%%"),
            ]

        # yt-dlp 每次运行结束都把 cookie jar 回写进 `--cookies` 文件（固有行为，无开关可关）。
        # 绝不把 Sentinel 真相源直接交给它——改传一份用完即弃的字节副本，回写只污染副本，
        # 真相源逐字节不变（非托管/用户自管文件与 None 由 `cookie_runfile` 原样直通）。
        # 副本必须存活到子进程结束（回写发生在进程退出时），故其生命周期挂在 `self._proc`
        # 上、由 `_release_cookie_runfile()` 在两个死亡点释放。**绝不**把副本路径写回
        # `ydl_opts`：重试会复用同一份 `ydl_opts`（见 workers.py 的 `merged`），必须让每次
        # attempt 都从真相源重新拷一份干净 jar，而不是继承上一轮被污染的副本。
        from ..auth.cookie_runfile import cookie_runfile

        self._cookie_stack = ExitStack()
        from ..utils.url_router import UrlRouter
        from ..utils.youtube_request import enforce_cookie_mode

        if UrlRouter.detect_platform(url) == "youtube":
            ydl_opts = dict(ydl_opts)
            enforce_cookie_mode(ydl_opts)
        _run_cf = self._cookie_stack.enter_context(cookie_runfile(ydl_opts.get("cookiefile")))
        run_opts = {**ydl_opts, "cookiefile": _run_cf}
        cmd += ydl_opts_to_cli_args(run_opts)
        cmd.append(url)

        # 这条 argv 里可能有 `socks5://user:pass@host`、cookie 文件的完整路径、含
        # Windows 用户名的输出路径，而日志文件是用户会直接贴进 Issue 的东西 ——
        # 所以落盘前必过 `render_argv()`（脱敏规则见 `observability/sanitize.py`）。
        # 走 `kind=argv` 事件而不是裸 `logger.info`：导 bug 包时要能从 JSONL 里
        # 按 run 取到"这次到底是用什么命令行跑的"，那是复现的第一手材料。
        emit_event(
            "argv",
            trace=current_flow(),
            stage="download",
            component="executor.native",
            label=label or "native",
            argv=render_argv(cmd),
        )
        log_pot_in_argv(cmd, stage="Download", task_id=label or "native")

        env = prepare_yt_dlp_env()
        env["PYTHONIOENCODING"] = "utf-8"
        work_dir = self._resolve_output_dir(ydl_opts)
        _emit_disk_space_signal(work_dir)

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            env=env,
            cwd=work_dir,
            **_win_hide_kwargs(),
        )

        # FFmpegFD does not consistently forward its child FFmpeg progress to
        # yt-dlp's stdout. While it is silent, poll the transaction layer's
        # `.parts/` byte total so the UI still reflects real download activity.
        # 没有探针就不开线程 —— 兜底进度只会一路报 0 字节，比没有更糟。
        if ydl_opts.get("download_sections") and parts_probe is not None:
            threading.Thread(
                target=_monitor_section_part_progress,
                args=(self._proc, parts_probe, on_progress),
                daemon=True,
                name="section-part-progress",
            ).start()

        output_path: str | None = None
        self._active_postprocessor = ""
        dest_paths: set[str] = set()
        tail: deque[str] = deque(maxlen=120)
        # 规则驱动的自动重试会复用同一个 executor，上一轮的警告不能算进这一轮的诊断
        self.diag_lines.clear()
        self.raw_lines.clear()
        from ..observability.warnings import WarningSummary

        warnings = WarningSummary(current_flow())
        # 预期总大小，按文件名分流累计，用于 rc != 0 时的完整性校验。
        # 每个流（视频/音频各一次独立下载）在**每个 tick** 都会重报自己的 total_bytes，
        # 所以这里按文件名**赋值**而不是 `+=`，最后求和。
        # 原先是 `expected_total_bytes = max(expected_total_bytes, tb)`：DASH 合并产物
        # 必然大于任何单流，ratio 恒 > 1，下面那道 `ratio < 0.5` 的保险丝从未触发过。
        expected_by_file: dict[str, int] = {}

        # 失败阶段判定的两个观察点。用户在任务卡上只看到"正在拉取元数据"，
        # 一个裸 exit_code=1 说不出这次失败是死在解析、选片还是下载途中 ——
        # 而 yt-dlp 的输出恰好给了两个无歧义的分界线：
        #   `[info] <id>: Downloading 1 format(s): 137+251` ⇒ 格式已经选定
        #   结构化进度行 / `[download] Destination:`        ⇒ 字节已经开始走
        saw_format_decision = False
        saw_progress = False

        proc = self._proc
        assert proc is not None
        assert proc.stdout is not None
        for raw in _iter_process_output(proc.stdout):
            if cancel_check():
                self._terminate_proc()
                raise RuntimeError(english("用户取消下载"))

            line = _decode_line(raw)
            if not line:
                continue
            tail.append(line)
            log_pot_from_output(line, stage="Download")

            parsed = self._ytdlp_parser.parse_line(line)

            # 阶段推进只往前走，不回退：合并/后处理阶段再出现的 info 行不会把
            # 已经 True 的 saw_progress 抹掉。
            if parsed.type in ("progress", "ffmpeg_progress", "destination"):
                saw_progress = True
            elif parsed.type == "info" and "format(s)" in (parsed.message or ""):
                # `output_parser` 的 `[info]` 分支刻意保留整行原文（含 `[info]` 前缀），
                # 所以这里直接在 message 上判子串。
                saw_format_decision = True

            # 诊断行的收集口只此一处：判据在 `diagnostics/collect.py`，随规则表演进。
            # 以前是散在下面 warning/error/info 三个分支里各 append 一次，于是
            # `[download] ... Skipping`（被 output_parser 判成 status）永远进不来。
            # 进度行提前排掉：`--newline` 下它们能有上万条，且**不可能**是诊断线索，
            # 没必要让每条都走一遍 53 条规则的匹配。
            if parsed.type not in ("progress", "ffmpeg_progress"):
                self.diag_lines.feed(line)
                # raw 缓冲比 `diag_lines` 宽一档：前者只留命中规则的行，复现问题时往往
                # 还要看它旁边那些"用了哪个 client、挑了哪个 format、ffmpeg 完整命令行"。
                # `should_keep_raw_line()` 再挡一次自家的 `FLUENTYTDL|` 进度模板行 ——
                # 那种行 `parsed.type` 未必是 progress，却纯粹是噪音。
                if should_keep_raw_line(line):
                    self.raw_lines.append(line)

            if parsed.type == "progress" and parsed.progress:
                tb = parsed.progress.total_bytes
                if isinstance(tb, (int, float)) and tb > 0:
                    # 附属文件（字幕/封面）不计入期望值：`actual_size` 那一侧只量主媒体
                    # 文件，两边口径必须一致，否则封面体积会系统性压低 ratio。
                    fname = parsed.progress.filename or ""
                    if not fname or not _is_auxiliary_file(fname):
                        expected_by_file[fname] = int(tb)

                on_progress(
                    {
                        "status": parsed.progress.status,
                        "downloaded_bytes": parsed.progress.downloaded_bytes,
                        "total_bytes": parsed.progress.total_bytes,
                        "speed": parsed.progress.speed,
                        "eta": parsed.progress.eta,
                        "filename": parsed.progress.filename,
                        "info_dict": parsed.progress.info_dict,
                        "label": label,
                    }
                )
                if parsed.progress.filename:
                    p = _abs(parsed.progress.filename)
                    dest_paths.add(p)
                    if on_file_created:
                        # `%(progress.filename)s` 只给路径不给类别（同一种行既可能是
                        # 视频流、也可能是 DASH 分片或字幕），所以 `role` 恒为 None ——
                        # 这类路径留给 `reconcile()` 那唯一一次兜底分类。
                        on_file_created(p, parsed.role)
                    if not output_path and not _is_auxiliary_file(p):
                        output_path = p
                        on_path(p)

            elif parsed.type == "destination":
                if parsed.path:
                    p = _abs(parsed.path)
                    dest_paths.add(p)
                    if on_file_created:
                        # 同上：`[download] Destination:` 也不声明角色。
                        on_file_created(p, parsed.role)
                    if not output_path and not _is_auxiliary_file(p):
                        output_path = p
                        on_path(p)

            elif parsed.type == "warning":
                # 去掉 `--no-warnings` 之后这里才真的有东西可收。字幕限流 / PO Token
                # 缺失都是 WARNING 级，且**不会**让任务失败 —— 不落到日志文件和
                # `diag_lines` 里就等于彻底丢掉，用户只剩一句"未找到字幕文件"。
                log_warning = logger.warning if warnings.feed(line) else logger.debug
                log_warning("[yt-dlp] {}", parsed.message or line)
                if parsed.message:
                    on_status("⚠️ " + parsed.message)

            elif parsed.type == "error":
                # 刻意**不**发 on_status：yt-dlp 的 ERROR: 不等于任务失败（跳过失效
                # 分片、播放列表里某条不可用都会打），把 UI 状态改成 error 会把成功
                # 的任务标红。真正的失败判定归 rc != 0 那段两级体积校验。
                logger.error("[yt-dlp] {}", parsed.message or line)

            elif parsed.type == "info":
                # `[info] There are no subtitles for the requested languages` 走的是
                # 这条。它既不是 WARNING 也不是 ERROR，以前没有分支接，直接消失在
                # `unknown` 兜底里 —— 那正是报告里"日志什么都没说"的由来。
                logger.info("[yt-dlp] {}", parsed.message or line)
                if parsed.message:
                    on_status(parsed.message)

            elif parsed.type == "ffmpeg_progress":
                if parsed.progress:
                    on_progress(
                        {
                            "status": "ffmpeg_progress",
                            "time_sec": parsed.progress.info_dict.get("time_sec"),
                            "speed": parsed.progress.info_dict.get("speed"),
                            "output_bytes": parsed.progress.info_dict.get("output_bytes"),
                            "bitrate": parsed.progress.info_dict.get("bitrate"),
                            "postprocessor": getattr(self, "_active_postprocessor", ""),
                        }
                    )

            elif parsed.type == "merge":
                if parsed.path:
                    p = _abs(parsed.path)
                    output_path = p
                    on_path(p)
                    # 合并/提取音频的产物**必须进清单**。以前这里只设 `output_path`、
                    # 从不 `dest_paths.add()` —— 那正是当年 `features.find_final_merged_file()`
                    # 只能靠 `os.listdir` 兜底的唯一原因（《下载产物事务层》诊断 3）。
                    # 这一行补上之后那个函数就没有存在理由了，已随 Step 5 退休。
                    # 角色由 yt-dlp 自己声明（`role="media"`），不需要任何推断。
                    dest_paths.add(p)
                    if on_file_created:
                        on_file_created(p, parsed.role)
                if parsed.message:
                    on_status(parsed.message)

            elif parsed.type == "postprocess":
                self._active_postprocessor = parsed.postprocessor or ""
                on_progress(
                    {
                        "status": "postprocess",
                        "postprocessor": parsed.postprocessor or "",
                        "pp_status": parsed.postprocessor_status or "",
                    }
                )
                # 嵌入证据：判据是结构化的 `finished`，不是人类日志行、也不是最终 rc。
                # 这是删除外挂字幕/独立封面的唯一合法依据 —— 少收一条证据只会导致
                # "嵌入成功却仍保留外挂文件"，那是安全的一侧；多删一个文件不是。
                if on_embed_evidence and parsed.postprocessor_status == "finished":
                    evidence = EMBED_EVIDENCE_BY_PP.get(parsed.postprocessor or "")
                    if evidence:
                        on_embed_evidence(evidence)
                if parsed.message:
                    on_status(parsed.message)

            elif parsed.type in ("subtitle", "status"):
                if parsed.message:
                    on_status(parsed.message)
                if parsed.path:
                    p = _abs(parsed.path)
                    dest_paths.add(p)
                    if on_file_created:
                        # `[info] Writing video subtitles/thumbnail to:` —— 这两行是
                        # yt-dlp **明确声明**了角色的，`role` 是 `subtitle` 或
                        # `thumbnail`。正则一直抓得到 `kind`，只是以前在 parser 里被
                        # 丢掉，于是下游各自按后缀再猜一遍（那正是"少删误删"的根因）。
                        on_file_created(p, parsed.role)

        warnings.finish()
        rc = proc.wait()
        self._proc = None
        # 子进程已退出，yt-dlp 的 cookie 回写（若有）已落在运行副本上——现在删掉它。
        self._release_cookie_runfile()

        if rc != 0:
            last_lines = "\n".join(tail)

            # ━━━ 关卡 1: 文件存在性 + 大小有效性检查 ━━━
            # 容错场景：Windows 下 yt-dlp 常因无法删除 .part-Frag 文件返回 exit code 1
            # 但此时文件实际上已经完整下载并合并成功
            is_valid = False
            valid_path_found = None
            actual_size = 0

            if output_path and os.path.exists(output_path):
                try:
                    actual_size = os.path.getsize(output_path)
                    if actual_size >= _MIN_VALID_MEDIA_BYTES:
                        is_valid = True
                        valid_path_found = output_path
                except OSError:
                    pass

            # 兜底探测：如果日志没截出 output_path，但生成了物理产物
            if not is_valid:
                for d_path in dest_paths:
                    if os.path.exists(d_path) and not _is_auxiliary_file(d_path):
                        try:
                            sz = os.path.getsize(d_path)
                            if sz >= _MIN_VALID_MEDIA_BYTES:
                                is_valid = True
                                valid_path_found = d_path
                                actual_size = sz
                                break
                        except OSError:
                            pass

            # ━━━ 关卡 2: 预期大小比对 ━━━
            # 如果进度回调中记录了预期总大小，且实际文件远小于预期，
            # 说明文件是不完整的残留，不应视为成功
            #
            # 有一类选项会**合法地**让产物远小于各流之和（转码降码率、剪掉赞助段、
            # 只下一段），此时体积比对没有意义 —— 与其调阈值，不如直接不参与判定：
            # 这道保险丝只在 rc != 0 的宽恕路径上被问到，宁可放过也不能误杀好文件。
            size_changing_opts = (
                bool(ydl_opts.get("extract_audio"))
                or bool(ydl_opts.get("sponsorblock_remove"))
                or bool(ydl_opts.get("download_sections"))
            )
            expected_total_bytes = 0 if size_changing_opts else sum(expected_by_file.values())
            if is_valid and expected_total_bytes > 0 and actual_size > 0:
                ratio = actual_size / expected_total_bytes
                if ratio < 0.5:
                    log_text(
                        logger,
                        "warning",
                        "文件大小 ({}) 仅为预期大小 ({}) 的 {:.0%}，判定为不完整下载",
                        actual_size,
                        expected_total_bytes,
                        ratio,
                    )
                    is_valid = False
                elif ratio < 0.9:
                    # 阈值刻意仍留 0.5：容器开销让合并产物与分流之和有正常偏差，调高会
                    # 误杀好文件。0.5–0.9 这段是"可疑但不判失败"——丢一条音轨通常只差
                    # 5-10%，落在这里。只记录，不改判定语义。
                    log_text(
                        logger,
                        "warning",
                        "文件大小 ({}) 为预期 ({}) 的 {:.0%}，低于预期但仍判定为有效（预期由 {} 个流累计）",
                        actual_size,
                        expected_total_bytes,
                        ratio,
                        len(expected_by_file),
                    )
                    # `signal` 而不是 `diagnosis`：这次操作没被判失败（硬规则 2），
                    # 而且这里**只报告观察到的事实**，不参与 `is_valid` 的裁决。
                    # 丢一条音轨正是落在这一段 —— 真正把它认定成降级要靠
                    # `expected − actual`（硬规则 3），体积比只能提供怀疑的理由。
                    emit_event(
                        "signal",
                        trace=current_flow(),
                        level="WARNING",
                        stage="download",
                        code="output_size_below_expected",
                        ratio=round(ratio, 3),
                        actual_bytes=actual_size,
                        expected_bytes=expected_total_bytes,
                        streams=len(expected_by_file),
                    )

            if is_valid:
                if not output_path and valid_path_found:
                    output_path = valid_path_found
                log_text(
                    logger,
                    "warning",
                    "yt-dlp 退出码 {} (非零)，但输出文件有效 ({}, {:.1f} KB)。忽略错误。",
                    rc,
                    output_path,
                    actual_size / 1024,
                )
                # 这里**不许**宣布 outcome（硬规则 4）：executor 返回之后还有
                # postprocess / verify / finalize，任何一步都可能再失败，一个 run
                # 就会出两个 outcome。所以只报告事实 `kind=recovery`，由 run 的终态
                # 边界（`DownloadWorker.run()`）唯一地裁决。
                #
                # `confidence=low` 是**必须**的：判据是"文件存在且体积不离谱"，
                # 一个体积启发式，不是完整性证明。把它记成高置信度，等于教读日志的人
                # 相信一条它给不出的保证。
                _mark_recovered(current_flow(), "nonzero_exit_valid_output")
                emit_event(
                    "recovery",
                    trace=current_flow(),
                    level="WARNING",
                    stage="download",
                    decision="recovery_accept",
                    reason="nonzero_exit_valid_output",
                    verification="size_heuristic",
                    confidence="low",
                    exit_code=rc,
                    size_bytes=actual_size,
                    expected_bytes=expected_total_bytes or None,
                )
            else:
                # 喂给 `diagnose()` 的必须是 `diag_lines`，不是 `tail`：`tail` maxlen=120
                # 且混着进度行，一段稍长的下载能把唯一那句 `ERROR:` 挤出窗口，于是主因
                # 仲裁只能落到兜底。`or last_lines` 是空保险 —— 一条诊断行都没收到时
                # （例如进程被环境直接打死，只留下几行进度），传空串会让 `diagnose()`
                # 走 `_EMPTY_OUTPUT` 占位，把唯一的线索也丢掉。
                raise YtDlpExecutionError(
                    exit_code=rc,
                    stderr=self.diag_lines.as_text() or last_lines,
                    phase=(
                        "download"
                        if saw_progress
                        else ("select" if saw_format_decision else "parse")
                    ),
                )
        else:
            # rc == 0 —— 但"进程成功退出"不等于"拿到了想要的东西"。请求的字幕语言
            # 一个都没匹配上、指定的音轨不存在、nsig 提取降级，全都以 WARNING 出现在
            # **正常退出**的输出里。这些行早就被收进 `diag_lines` 了，只是一直只为
            # 失败路径的 `diagnose()` 服务，rc=0 时整个 buffer 被原样丢掉 —— 于是
            # "下载完了但字幕没有"在日志里一个字都没有，正是这轮重构的起因。
            #
            # 落 `signal` 而**不是** `diagnosis`（硬规则 2）：这次操作确实成功了，
            # 在这里做主因仲裁会让 `count(kind=diagnosis)` 不再等于"真正发生了错误"。
            #
            # `diag_lines` 已经是筛过的行，`emit_success_signals` 内部会再筛一遍 ——
            # 判据是同一个 `is_diagnostic_line`，重筛是幂等的。为省这一遍去开个
            # "已筛过"的旁路参数，不值得让这个函数长出两种输入语义。
            emit_success_signals(
                self.diag_lines.as_text(),
                trace=current_flow(),
                stage="download",
                operation="download",
            )

        return output_path

    # ── 子步骤 ────────────────────────────────────────────

    def _should_run_post_process(self, opts: dict[str, Any]) -> bool:
        """检查是否有需要 yt-dlp 处理的后处理选项。"""
        keys = ["writesubtitles", "writeautomaticsub", "writethumbnail", "addmetadata"]
        if any(opts.get(k) for k in keys):
            return True
        # 检查 postprocessors 列表
        pps = opts.get("postprocessors")
        if isinstance(pps, list) and pps:
            return True
        return False

    def _run_post_download_pass(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        *,
        on_progress: ProgressCallback,
        on_status: StatusCallback,
        cancel_check: CancelCheck,
        on_file_created: FileCreatedCallback | None = None,
        on_embed_evidence: EmbedEvidenceCallback | None = None,
        final_paths_file: str | None = None,
    ) -> None:
        """运行后处理 pass (skip-download)。"""
        # 克隆选项并强制跳过下载
        opts = ydl_opts.copy()
        opts["skip_download"] = True

        # 移除可能冲突的格式选项
        opts.pop("format", None)

        # 这里的 callbacks 只需要 status，以及部分 progress (如下载字幕时)
        # 我们传入 on_progress，但标记为 "补充"
        self._execute_native(
            url,
            opts,
            on_progress=on_progress,
            on_status=on_status,
            on_path=lambda path: None,
            cancel_check=cancel_check,
            on_file_created=on_file_created,
            on_embed_evidence=on_embed_evidence,
            final_paths_file=final_paths_file,
        )

    def _extract_stream_urls(
        self,
        url: str,
        ydl_opts: dict[str, Any],
        cancel_check: CancelCheck,
        check_protocol: bool = True,
    ) -> dict[str, Any]:
        """用 yt-dlp --dump-single-json 提取流 URL。"""
        exe = resolve_yt_dlp_exe()
        if exe is None:
            raise RuntimeError(english("yt-dlp 可执行文件未找到"))

        cmd: list[str] = [str(exe), "--ignore-config", "--no-warnings", "-J"]

        # 传递格式选择器
        fmt = ydl_opts.get("format")
        if isinstance(fmt, str) and fmt:
            cmd += ["-f", fmt]

        # 传递认证参数
        for key, flag in [("cookiefile", "--cookies"), ("proxy", "--proxy")]:
            val = ydl_opts.get(key)
            if isinstance(val, str) and val.strip():
                cmd += [flag, val.strip()]

        cookies_from_browser = ydl_opts.get("cookiesfrombrowser")
        if isinstance(cookies_from_browser, (list, tuple)) and cookies_from_browser:
            cmd += ["--cookies-from-browser", str(cookies_from_browser[0])]

        # extractor-args
        extractor_args = ydl_opts.get("extractor_args")
        if isinstance(extractor_args, dict):
            for ie_key, ie_args in extractor_args.items():
                if not isinstance(ie_args, dict):
                    continue
                parts = []
                for k, v in ie_args.items():
                    if isinstance(v, (list, tuple)):
                        parts.append(f"{k}={','.join(str(x) for x in v)}")
                    else:
                        parts.append(f"{k}={v}")
                if parts:
                    cmd += ["--extractor-args", f"{ie_key}:{';'.join(parts)}"]

        cmd.append(url)

        # DEBUG 级：这条只是取流地址的辅助探测，正常排查用不到，但脱敏一样不能省。
        emit_event(
            "argv",
            trace=current_flow(),
            level="DEBUG",
            stage="preflight",
            component="executor.extract_stream_urls",
            argv=render_argv(cmd),
        )
        log_pot_in_argv(cmd, stage="Download", task_id="extract_stream_urls")

        env = prepare_yt_dlp_env()
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **_win_hide_kwargs(),
        )

        stdout, stderr = proc.communicate()

        log_pot_from_output(stderr, stage="Download")

        if cancel_check():
            raise RuntimeError(english("用户取消下载"))

        if proc.returncode != 0:
            raise RuntimeError(
                english("yt-dlp 信息提取失败 (rc={0}): {1}", proc.returncode, stderr[:500])
            )

        try:
            info = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(english("yt-dlp JSON 解析失败: {0}", e)) from e

        return self._parse_stream_info(info, check_protocol=check_protocol)

    def _parse_stream_info(
        self, info: dict[str, Any], check_protocol: bool = True
    ) -> dict[str, Any]:
        """从 yt-dlp JSON 信息中提取流 URL。"""
        result: dict[str, Any] = {
            "title": info.get("title") or info.get("id") or "video",
        }

        requested_formats = info.get("requested_formats")

        # Debug Log
        if requested_formats:
            logger.debug(
                "[Executor] requested_formats: {}",
                [(f.get("format_id"), f.get("vcodec"), f.get("acodec")) for f in requested_formats],
            )
        else:
            logger.debug("[Executor] No requested_formats found, using single format info.")

        if not requested_formats:
            # 没有 requested_formats → 可能是预合流或单文件
            stream_url = info.get("url")
            if stream_url:
                # 假设为主视频流 (可能是 Muxed 或 Video-Only 或 Audio-Only)
                # Check protocol compatibility for Aria2
                proto = info.get("protocol", "")
                if check_protocol and proto in ("m3u8", "m3u8_native", "rtsp"):
                    raise RuntimeError(english("流协议 {0} 需要 yt-dlp native 处理", proto))

                vcodec = info.get("vcodec")
                acodec = info.get("acodec")
                is_audio_only = vcodec == "none" and acodec != "none"

                fmt_info = {
                    "url": stream_url,
                    "ext": info.get("ext", "mp4"),
                    "http_headers": info.get("http_headers", {}),
                    "filesize": info.get("filesize") or info.get("filesize_approx"),
                }

                if is_audio_only:
                    result["audio"] = fmt_info
                else:
                    result["video"] = fmt_info
            return result

        # 遍历 requested_formats 自动归类
        for fmt in requested_formats:
            proto = fmt.get("protocol", "")
            if check_protocol and proto in ("m3u8", "m3u8_native", "rtsp"):
                raise RuntimeError(english("流协议 {0} 需要 yt-dlp native 处理", proto))

            # 提取信息
            stream_url = fmt.get("url", "")
            if not stream_url:
                continue

            headers = fmt.get("http_headers", {})
            ext = fmt.get("ext", "")
            filesize = fmt.get("filesize") or fmt.get("filesize_approx")

            fmt_data = {
                "url": stream_url,
                "ext": ext,
                "http_headers": headers,
                "filesize": filesize,
            }

            vcodec = fmt.get("vcodec")
            acodec = fmt.get("acodec")
            has_video = vcodec and vcodec != "none"
            has_audio = acodec and acodec != "none"

            # 判定类型
            if has_video:
                # 任何包含视频流的都视为视频 (包括 Muxed)
                # 如果已经存在 video (例如之前的 stream)，则覆盖 (通常 requested_formats 顺序不管是怎样的, 只要有 video 就行)
                result["video"] = fmt_data
            elif has_audio:
                # 只有音频
                result["audio"] = fmt_data

        return result

    def _resolve_output_dir(self, ydl_opts: dict[str, Any]) -> str:
        """解析输出目录。"""
        paths = ydl_opts.get("paths")
        if isinstance(paths, dict):
            home = paths.get("home")
            if isinstance(home, str) and home.strip():
                d = home.strip()
                os.makedirs(d, exist_ok=True)
                return d

        # 从配置获取
        try:
            from ..core.config_manager import config_manager

            d = str(config_manager.get("download_dir") or "").strip()
            if d:
                os.makedirs(d, exist_ok=True)
                return d
        except Exception:
            pass

        return os.getcwd()

    def _release_cookie_runfile(self) -> None:
        """删除本次执行的 cookie 运行副本（若有），幂等。

        yt-dlp 的 `--cookies` 回写只落在这份副本上，真相源不受影响；副本生命周期等于
        子进程生命周期。正常 `proc.wait()` 之后与 `_terminate_proc()`（取消/强杀的唯一
        咽喉）都会调它。先把字段置空再 `close()`，避免重入时二次进入。
        """
        stack = self._cookie_stack
        if stack is not None:
            self._cookie_stack = None
            stack.close()

    def _terminate_proc(self) -> None:
        """终止当前子进程并尽可能杀死整个进程树防止锁释放失败。"""
        if self._proc:
            import platform

            try:
                if platform.system() == "Windows":
                    import subprocess

                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                    )
                else:
                    self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            # 取消/强杀路径：进程已死，释放本次执行的 cookie 运行副本（幂等）。
            self._release_cookie_runfile()

    def terminate(self) -> None:
        """外部调用：终止执行器的子进程。"""
        self._terminate_proc()


# ── 工具查找 ──────────────────────────────────────────────


def _iter_process_output(stream) -> Any:
    """Yield subprocess output frames separated by either LF or CR.

    yt-dlp prints normal events with newlines, while FFmpeg continuously
    rewrites its progress line with carriage returns. Iterating over the pipe
    directly only sees the latter after FFmpeg exits, which leaves the UI
    stuck on the preceding status message.
    """
    pending = b""
    while True:
        reader = getattr(stream, "read1", None)
        chunk = reader(4096) if callable(reader) else stream.read(4096)
        if not chunk:
            break
        pending += chunk
        frames = re.split(rb"[\r\n]+", pending)
        pending = frames.pop()
        for frame in frames:
            if frame:
                yield frame
    if pending:
        yield pending


def _monitor_section_part_progress(
    proc: subprocess.Popen[Any], parts_probe: PartsProbe, on_progress: ProgressCallback
) -> None:
    """Emit fallback progress from the transaction layer's growing `.part` bytes.

    以前这里自己 `Path(work_dir).glob("*.part")`。那有两个问题：`work_dir` 是
    `paths["home"]`，而 `home != temp` 之后 `.part` 全在 `.parts/` 里，所以它永远读
    到 0；而且它是 `download/` 下唯一一处物理扫描（架构测试的基线里就记着这条）。
    现在只调一个返回字节数的探针 —— 扫描留在事务层，这里连沙盒长什么样都不知道。
    """
    previous_bytes = 0
    previous_tick = time.monotonic()
    while proc.poll() is None:
        try:
            total_bytes = parts_probe()
        except Exception:
            total_bytes = 0

        now = time.monotonic()
        if total_bytes > 0:
            speed = 0.0
            if total_bytes >= previous_bytes and now > previous_tick:
                speed = (total_bytes - previous_bytes) / (now - previous_tick)
            on_progress(
                {
                    "status": "section_file_progress",
                    "output_bytes": total_bytes,
                    "speed": speed,
                }
            )
        previous_bytes = total_bytes
        previous_tick = now
        time.sleep(0.75)


def _find_ffmpeg() -> str | None:
    """查找 ffmpeg 可执行文件。"""
    try:
        from ..core.config_manager import config_manager

        ffmpeg_path = str(config_manager.get("ffmpeg_path") or "").strip()
        if ffmpeg_path and Path(ffmpeg_path).exists():
            return ffmpeg_path
    except Exception:
        logger.debug("_find_ffmpeg: config_manager lookup failed")
    try:
        from ..utils.paths import locate_runtime_tool

        return str(locate_runtime_tool("ffmpeg.exe", "ffmpeg/ffmpeg.exe"))
    except Exception:
        logger.debug("_find_ffmpeg: locate_runtime_tool failed")
    return shutil.which("ffmpeg")


# ── 辅助函数 ──────────────────────────────────────────────


def _decode_line(raw: bytes) -> str:
    """健壮的行解码 (UTF-8 → GBK → replace)。"""
    try:
        line = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            line = raw.decode("gbk")
        except UnicodeDecodeError:
            line = raw.decode("utf-8", errors="replace")
    return line.rstrip("\r\n")


def _abs(path: str) -> str:
    """安全的 abspath。"""
    try:
        return os.path.abspath(path)
    except Exception:
        return path


_RE_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    """清理文件名中不安全的字符。"""
    s = _RE_UNSAFE.sub("_", name).strip(". ")
    return s[:200] if s else "download"
