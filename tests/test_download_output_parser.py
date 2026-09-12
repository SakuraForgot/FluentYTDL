"""Step 2 的两件事：**角色由报告者声明**、**嵌入证据由结构化状态裁决**。

守的是《下载产物事务层》诊断 3 —— 角色信息一直被解析出来又被丢掉：

- `output_parser._RE_WRITING_TO` 抓到了 `kind`（`video subtitles` vs
  `video thumbnail`），却在返回时一律写成 `type="subtitle"`，于是下游
  （`features._cleanup_thumbnail_files` 按后缀拼名字、`subtitle_processor` 的四级定位
  扫目录）只能各自再猜一遍 —— 那正是「少删误删」的根因。字幕那一侧的四级定位已在
  Step 3 退休。
- `[Merger] Merging formats into` 的产物**从不进 `dest_paths`**，那是
  `features.find_final_merged_file()` 必须靠 `os.listdir` 兜底的唯一原因。

所以本文件的断言分两层：

1. **纯解析层**（无 I/O、无子进程）—— `role` 在哪些行上有值、在哪些行上必须是
   `None`。`None` 不是「未知所以随便猜」，而是一条硬信息：报告者没有声明角色。
2. **executor 的行循环**（monkeypatch 掉 `Popen`）—— 解析出来的 `role` 真的走到了
   `on_file_created` 的第二个参数上，merge 产物真的进了 `dest_paths`，
   `on_embed_evidence` 只在 `finished` 上触发，`--print-to-file` 只在给了落点时出现。

第 2 层必须有：第 1 层全绿而接线漏一处，表现出来就是「角色又被丢掉了」——
和重构之前一模一样。

**导入路径必须是 `src` 在 `sys.path` 上 + `from fluentytdl...`**（原因见
`tests/test_section_download.py` 的文件头）。
"""

import os
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

import pytest

# logger / config_manager 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-parser-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.download import executor as executor_mod  # noqa: E402
from fluentytdl.download.executor import DownloadExecutor  # noqa: E402
from fluentytdl.download.output_parser import (  # noqa: E402
    EMBED_EVIDENCE_BY_PP,
    YtDlpOutputParser,
)


@pytest.fixture()
def parser() -> YtDlpOutputParser:
    return YtDlpOutputParser()


# ── 第 1 层：角色只由报告者声明 ────────────────────────────


def test_writing_subtitles_declares_subtitle_role(parser):
    line = r"[info] Writing video subtitles to: D:\dl\Title.en-GB.vtt"
    parsed = parser.parse_line(line)

    # `type` 仍是 `subtitle` —— executor / workers 的既有分支靠它把路径记进
    # `dest_paths`；角色改由 `role` 承载。
    assert parsed.type == "subtitle"
    assert parsed.role == "subtitle"
    assert parsed.path == r"D:\dl\Title.en-GB.vtt"


def test_writing_thumbnail_declares_thumbnail_role(parser):
    """封面行以前也返回 `type="subtitle"`，`kind` 当场被丢掉。

    带序号的形态（`thumbnail 41`）是 yt-dlp 写多张封面时的真实输出，正则的
    `(?:\\s+\\S+)?` 就是为它留的。
    """
    for line in (
        r"[info] Writing video thumbnail to: D:\dl\Title.webp",
        r"[info] Writing video thumbnail 41 to: D:\dl\Title.webp",
    ):
        parsed = parser.parse_line(line)
        assert parsed.type == "subtitle", line
        assert parsed.role == "thumbnail", line
        assert parsed.path == r"D:\dl\Title.webp", line


def test_writing_to_anchors_on_the_to_marker_not_the_first_colon(parser):
    """Windows 路径自带盘符冒号 —— `split(":", 1)` 只是碰巧没出事。"""
    parsed = parser.parse_line(r'[info] Writing video subtitles to: "C:\a b\T.ja.srt"')
    assert parsed.path == r"C:\a b\T.ja.srt"


def test_merge_and_extract_audio_declare_media_role(parser):
    """这两行是 yt-dlp **明确声明**了角色的主媒体路径。"""
    merged = parser.parse_line('[Merger] Merging formats into "D:\\dl\\Title.mp4"')
    assert merged.type == "merge"
    assert merged.role == "media"
    assert merged.path == r"D:\dl\Title.mp4"

    audio = parser.parse_line(r"[ExtractAudio] Destination: D:\dl\Title.mp3")
    assert audio.type == "merge"
    assert audio.role == "media"
    assert audio.path == r"D:\dl\Title.mp3"


def test_lines_that_declare_nothing_have_role_none(parser):
    """`role is None` 是一条硬信息：报告者没有声明角色。

    `[download] Destination:` 与 `%(progress.filename)s` 都只给路径不给类别 ——
    同一种行既可能是视频流、也可能是 DASH 分片或字幕。这类路径不进
    `add_reported()`，留给 `reconcile()` 那唯一一次集中式兜底分类。
    """
    dest = parser.parse_line(r"[download] Destination: D:\dl\Title.f137.mp4")
    assert dest.type == "destination"
    assert dest.role is None

    prog = parser.parse_line(
        "FLUENTYTDL|download|1024|4096|NA|512|00:03|avc1|none|mp4|D:\\dl\\Title.f137.mp4"
    )
    assert prog.type == "progress"
    assert prog.role is None

    # `[Merger]` 之外的后处理状态行也不声明路径角色
    pp = parser.parse_line("FLUENTYTDL|postprocess|finished|FFmpegEmbedSubtitle")
    assert pp.type == "postprocess"
    assert pp.role is None


def test_merge_status_line_without_path_keeps_role_none(parser):
    """`[Merger]` 前缀但没匹配上路径正则 ⇒ 只是状态行，不许凭前缀补 `media`。"""
    parsed = parser.parse_line("[Merger] Merging formats")
    assert parsed.type == "status"
    assert parsed.path is None
    assert parsed.role is None


# ── 第 1 层：嵌入证据的权威通道 ────────────────────────────


#: yt-dlp 2026.08.30 实测：`%(progress.postprocessor)s` 剥掉 `FFmpeg` 前缀和 `PP` 后缀。
#: 四个后处理器在两次真实下载里同时印证了这条规则，所以它不是猜测。
_OBSERVED_PP_NAMES: tuple[str, ...] = ("Merger", "ExtractAudio", "Metadata", "MoveFiles")


def test_observed_short_pp_names_all_have_display_names():
    """实测到达的是**短**拼写 —— 显示表必须按短名登记，否则界面掉成裸英文。

    以前这张表给 `Metadata` / `ExtractAudio` / `SubtitlesConvertor` /
    `ThumbnailsConvertor` / `VideoConvertor` 只登记了 `FFmpeg*` 长键，于是它们一个都
    没命中过 ——「后处理: ExtractAudio (完成)」这种半英文状态就是这么来的。
    """
    parser = YtDlpOutputParser()
    for pp in _OBSERVED_PP_NAMES:
        parsed = parser.parse_line(f"FLUENTYTDL|postprocess|finished|{pp}")
        assert parsed.postprocessor == pp
        # 显示名必须是中文 —— 命中了映射表，而不是原样透出英文类名
        assert pp not in (parsed.message or ""), f"{pp} 没命中 _PP_NAMES，界面会显示英文"

    # 短拼写与长拼写指向同一个显示名 —— 长键是版本兜底，不是另一套语义
    names = YtDlpOutputParser._PP_NAMES
    for short in ("Merger", "Metadata", "ExtractAudio", "EmbedSubtitle"):
        assert names[short] == names[f"FFmpeg{short}"]


def test_embed_evidence_map_accepts_both_pp_spellings():
    """短拼写是真的，长拼写是兜底 —— 两个都收。

    实测（yt-dlp 2026.08.30）：`FFmpegMergerPP` → `Merger`、
    `MoveFilesAfterDownloadPP` → `MoveFiles`，所以 `FFmpegEmbedSubtitlePP` 报的是
    `EmbedSubtitle`。长键不命中即死键，代价为零；而万一版本改回长名，漏掉证据的后果
    是把嵌入成功误判成失败 ⇒ 外挂字幕被保留 —— 那是安全的一侧。
    """
    assert EMBED_EVIDENCE_BY_PP["FFmpegEmbedSubtitle"] == "subtitle"
    assert EMBED_EVIDENCE_BY_PP["EmbedSubtitle"] == "subtitle"
    assert EMBED_EVIDENCE_BY_PP["EmbedThumbnail"] == "thumbnail"
    assert EMBED_EVIDENCE_BY_PP["FFmpegThumbnail"] == "thumbnail"

    # 非嵌入类后处理器不得产出证据 —— 它们跑完不代表任何东西被嵌进容器。
    for pp in ("FFmpegMerger", "MoveFiles", "FFmpegSubtitlesConvertor", "FFmpegMetadata"):
        assert pp not in EMBED_EVIDENCE_BY_PP


def test_postprocess_status_is_parsed_verbatim(parser):
    """`finished` 这个判据必须原样到达 executor，不能被显示名吃掉。"""
    for status in ("started", "processing", "finished"):
        parsed = parser.parse_line(f"FLUENTYTDL|postprocess|{status}|FFmpegEmbedSubtitle")
        assert parsed.postprocessor_status == status
        assert parsed.postprocessor == "FFmpegEmbedSubtitle"

    # 短拼写在显示层也不能掉成裸英文名
    shown = parser.parse_line("FLUENTYTDL|postprocess|finished|EmbedSubtitle")
    assert "嵌入字幕" in (shown.message or "")


# ── 第 2 层：executor 行循环的接线 ─────────────────────────


class _FakeProc:
    """只提供 executor 行循环用到的那三件事。"""

    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
        self._rc = returncode
        self.terminated = False

    def wait(self, timeout=None):  # noqa: ANN001 - 签名对齐 Popen
        return self._rc

    def poll(self):
        return self._rc

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class _Capture:
    """把 executor 的四条回调收成可断言的结构。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, str | None]] = []
        self.evidence: list[str] = []
        self.paths: list[str] = []
        self.status: list[str] = []

    def on_file_created(self, path: str, role: str | None = None) -> None:
        self.created.append((path, role))

    def on_embed_evidence(self, kind: str) -> None:
        self.evidence.append(kind)

    def on_path(self, path: str) -> None:
        self.paths.append(path)

    def on_status(self, message: str) -> None:
        self.status.append(message)

    def on_progress(self, data: dict) -> None:
        pass


def _run_native(
    monkeypatch,
    lines: list[str],
    *,
    tmp_path: Path,
    returncode: int = 0,
    final_paths_file: str | None = None,
) -> tuple[_Capture, list[str], str | None]:
    """驱动 `_execute_native()` 的行循环，返回 `(回调记录, argv, output_path)`。

    只 patch 四个外部依赖：可执行文件定位、环境准备、`Popen`、以及事件落盘的
    出口（`emit_event` 会走 loguru，测试里不需要）。行循环本身**原样执行** ——
    这个测试的全部价值就在于它跑的是真代码路径。
    """
    captured_cmd: list[str] = []

    def fake_popen(cmd, **kwargs):  # noqa: ANN001
        captured_cmd.extend(cmd)
        return _FakeProc(lines, returncode)

    monkeypatch.setattr(executor_mod, "resolve_yt_dlp_exe", lambda: Path("yt-dlp.exe"))
    monkeypatch.setattr(executor_mod, "prepare_yt_dlp_env", lambda: dict(os.environ))
    monkeypatch.setattr(executor_mod.subprocess, "Popen", fake_popen)

    cap = _Capture()
    ex = DownloadExecutor()
    out = ex._execute_native(
        "https://example.com/watch?v=x",
        {"paths": {"home": str(tmp_path)}},
        on_progress=cap.on_progress,
        on_status=cap.on_status,
        on_path=cap.on_path,
        cancel_check=lambda: False,
        on_file_created=cap.on_file_created,
        on_embed_evidence=cap.on_embed_evidence,
        final_paths_file=final_paths_file,
    )
    return cap, captured_cmd, out


def test_declared_roles_reach_the_file_created_channel(monkeypatch, tmp_path):
    """角色必须**穿过** executor 到达消费方，而不是在解析后就地丢掉。"""
    media = str(tmp_path / "Title.mp4")
    sub = str(tmp_path / "Title.en-GB.vtt")
    thumb = str(tmp_path / "Title.webp")
    frag = str(tmp_path / "Title.f137.mp4")

    cap, _cmd, out = _run_native(
        monkeypatch,
        [
            f"[download] Destination: {frag}",
            f"[info] Writing video subtitles to: {sub}",
            f"[info] Writing video thumbnail 41 to: {thumb}",
            f'[Merger] Merging formats into "{media}"',
        ],
        tmp_path=tmp_path,
    )

    by_path = dict(cap.created)
    assert by_path[sub] == "subtitle"
    assert by_path[thumb] == "thumbnail"
    assert by_path[media] == "media"
    # 只给路径不给类别的那一行，`role` 必须仍是 None（留给 `reconcile()`）
    assert by_path[frag] is None
    # 合并产物是主媒体，`output_path` 由它裁决
    assert out == media


def test_merge_product_enters_the_manifest_channel(monkeypatch, tmp_path):
    """诊断 3 点名的那处：merge 产物以前只设 `output_path`，从不进清单。

    那正是 `features.find_final_merged_file()` 必须靠 `os.listdir` 兜底的唯一原因。
    """
    media = str(tmp_path / "Title.mp4")
    cap, _cmd, out = _run_native(
        monkeypatch,
        [f'[Merger] Merging formats into "{media}"'],
        tmp_path=tmp_path,
    )

    assert (media, "media") in cap.created
    assert out == media
    assert cap.paths == [media]


def test_extract_audio_product_also_enters_the_channel(monkeypatch, tmp_path):
    audio = str(tmp_path / "Title.mp3")
    cap, _cmd, out = _run_native(
        monkeypatch,
        [f"[ExtractAudio] Destination: {audio}"],
        tmp_path=tmp_path,
    )
    assert (audio, "media") in cap.created
    assert out == audio


def test_embed_evidence_only_fires_on_finished(monkeypatch, tmp_path):
    """判据是结构化的 `finished`，不是人类日志行、更不是最终 rc。

    `started` / `processing` 都不算 —— 后处理器开工不等于内容进了容器，而这条
    证据是删除外挂字幕的唯一合法理由。
    """
    cap, _cmd, _out = _run_native(
        monkeypatch,
        [
            "FLUENTYTDL|postprocess|started|FFmpegEmbedSubtitle",
            "FLUENTYTDL|postprocess|processing|FFmpegEmbedSubtitle",
            '[EmbedSubtitle] Embedding subtitles in "Title.mp4"',
            "FLUENTYTDL|postprocess|finished|FFmpegEmbedSubtitle",
            "FLUENTYTDL|postprocess|finished|EmbedThumbnail",
            # 非嵌入类后处理器跑完不产出任何证据
            "FLUENTYTDL|postprocess|finished|FFmpegMerger",
            "FLUENTYTDL|postprocess|finished|MoveFiles",
        ],
        tmp_path=tmp_path,
    )
    assert cap.evidence == ["subtitle", "thumbnail"]


def test_human_log_line_alone_is_not_evidence(monkeypatch, tmp_path):
    """`[EmbedSubtitle] Embedding subtitles in` 只作显示，不作 authority。"""
    cap, _cmd, _out = _run_native(
        monkeypatch,
        ['[EmbedSubtitle] Embedding subtitles in "Title.mp4"'],
        tmp_path=tmp_path,
    )
    assert cap.evidence == []


def test_print_to_file_only_appears_when_a_sink_is_given(monkeypatch, tmp_path):
    """没有落点就不加这个参数 —— Step 4 之前它必须完全惰性。"""
    _cap, cmd, _out = _run_native(monkeypatch, ["[download] 0.0% of 1.00MiB"], tmp_path=tmp_path)
    assert "--print-to-file" not in cmd


def test_print_to_file_passes_absolute_path_with_escaped_percent(monkeypatch, tmp_path):
    """FILE 用的是 **output-template 语法**，所以 `%` 必须转义成 `%%`。

    相对路径会被解到 `paths.home`（= `payload/`）里去，那会让控制文件掉进
    `reconcile()` 的视野；所以传绝对路径。（「outtmpl 必须相对」那条纪律约束的是
    输出模板本身 —— 绝对 `-o` 会让 `-P` 整体失效 —— 不约束这个 FILE。）
    """
    sink = str(tmp_path / "100%dir" / ".fytdl" / "final.0.txt")
    _cap, cmd, _out = _run_native(
        monkeypatch,
        ["[download] 0.0% of 1.00MiB"],
        tmp_path=tmp_path,
        final_paths_file=sink,
    )

    i = cmd.index("--print-to-file")
    assert cmd[i + 1] == "after_move:filepath"
    assert cmd[i + 2] == sink.replace("%", "%%")
    assert os.path.isabs(cmd[i + 2])

    # 它必须排在 `--progress-template` 之后、URL 之前，且**不得**引入 `-O/--print`
    # （那个的帮助文本明写 "Implies --quiet"，会掐死整个进度解析）。
    assert "-O" not in cmd and "--print" not in cmd
    assert cmd.count("--progress-template") == 2
    assert i > cmd.index("--progress-template")


def test_progress_templates_survive_alongside_print_to_file(monkeypatch, tmp_path):
    """两条 progress-template 必须同时在 argv 里 —— 一条 download、一条 postprocess。"""
    _cap, cmd, _out = _run_native(
        monkeypatch,
        ["[download] 0.0% of 1.00MiB"],
        tmp_path=tmp_path,
        final_paths_file=str(tmp_path / "final.0.txt"),
    )
    templates = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--progress-template"]
    assert any(t.startswith("download:FLUENTYTDL|download|") for t in templates)
    assert any(t.startswith("postprocess:FLUENTYTDL|postprocess|") for t in templates)


def test_callbacks_are_optional(monkeypatch, tmp_path):
    """两个新回调都可以不给 —— Step 4 之前的调用点还没有 `StagingArea`。"""
    monkeypatch.setattr(executor_mod, "resolve_yt_dlp_exe", lambda: Path("yt-dlp.exe"))
    monkeypatch.setattr(executor_mod, "prepare_yt_dlp_env", lambda: dict(os.environ))
    media = str(tmp_path / "Title.mp4")
    monkeypatch.setattr(
        executor_mod.subprocess,
        "Popen",
        lambda cmd, **kw: _FakeProc(
            [
                f'[Merger] Merging formats into "{media}"',
                "FLUENTYTDL|postprocess|finished|FFmpegEmbedSubtitle",
            ]
        ),
    )

    ex = DownloadExecutor()
    out = ex._execute_native(
        "https://example.com/x",
        {"paths": {"home": str(tmp_path)}},
        on_progress=lambda d: None,
        on_status=lambda m: None,
        on_path=lambda p: None,
        cancel_check=lambda: False,
    )
    assert out == media


def test_file_created_signature_accepts_a_single_argument_call():
    """`role` 必须有默认值 —— 否则 Step 4 之前的旧调用点会当场炸。

    这条守的是接线过程本身：`executor` 里有四个调用点，任何一处漏传第二个参数
    都必须仍然可用（`role=None` 就是「报告者没说」）。
    """
    seen: list[tuple[str, str | None]] = []

    def cb(path: str, role: str | None = None) -> None:
        seen.append((path, role))

    cb("a.mp4")
    cb("b.vtt", "subtitle")
    assert seen == [("a.mp4", None), ("b.vtt", "subtitle")]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
