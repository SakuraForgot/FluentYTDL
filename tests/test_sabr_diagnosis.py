"""SABR 强制流的诊断契约。

背景：YouTube 对某些客户端强制 SABR 流式传输后，yt-dlp 会把所有"没有下载 URL"的
格式丢掉，选片器于是挑不到东西，最终只留一句 ``ERROR: Requested format is not
available``。规则表按文本匹配会判成 `format_unavailable`——文案是"换一档画质通常
就能下载"，**而画质本身存在，换档位不可能有用**，这条引导把用户送进死胡同。

所以本文件锁死两件事，缺一不可：
1. 有 SABR 伴随信号时，主因被纠正成 `sabr_formats_skipped`，并给出真正可执行的动作；
2. **没有** SABR 信号时，`format_unavailable` 的判词一个字都不许变 —— 用户真的挑了
   一个不存在的档位时，原来的引导是对的。

文件末尾另有 `phase`（失败死在哪一步）的契约：诊断层的往返，以及 `executor`
根据"见过哪些输出行"算出它的三条分界线。两者都不起 Qt。
"""

import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.diagnostics import FALLBACK_CODE, Diagnosis, diagnose  # noqa: E402
from fluentytdl.download.executor import DownloadExecutor  # noqa: E402
from fluentytdl.models.errors import YtDlpExecutionError  # noqa: E402

# ── 夹具：真实 yt-dlp 输出片段 ────────────────────────────────

#: 触发本次故障的两条 WARNING（`diagnostics/collect.py` 实测会保留它们）。
SABR_WARNINGS = (
    "WARNING: [youtube] WfAU5IDLbdw: Some tv client https formats have been "
    "skipped as they are missing a url. YouTube is forcing SABR streaming for "
    "this client. See  https://github.com/yt-dlp/yt-dlp/issues/12482  for more details"
)
FORMAT_UNAVAILABLE = "ERROR: [youtube] WfAU5IDLbdw: Requested format is not available"
NSIG_FAILED = "WARNING: [youtube] WfAU5IDLbdw: nsig extraction failed: Some formats may be missing"
HTTP_403 = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


def _diag(*lines: str, rc: int = 1, phase: str = "") -> Diagnosis:
    return diagnose(rc, "\n".join(lines), phase=phase)


# ── 主路径：SABR 纠正判词 ─────────────────────────────────────


def test_sabr_overrides_format_unavailable():
    """两条 SABR 警告 + 格式不可用 ⇒ 判成 SABR，而不是"所选格式不可用"。"""
    diag = _diag(SABR_WARNINGS, FORMAT_UNAVAILABLE)

    assert diag.code == "sabr_formats_skipped"
    assert diag.category == "toolchain"
    # 规则里写 warning 只为不抢主因；一旦成为主因，它描述的是一次真失败。
    assert diag.severity == "recoverable"
    assert diag.fix_action == "enable_pot_provider"
    # after_fix ⇒ 任务挂起等用户修，而不是像 format_unavailable 那样直接硬失败。
    assert diag.retry.policy == "after_fix"
    assert diag.retry.is_automatic is False
    # 有 fix_action 才有按钮文案；空串会让弹窗上的修复按钮消失。
    assert diag.recovery_hint


def test_sabr_text_says_changing_quality_wont_help():
    """文案必须否掉"换档位"这个错方向，否则纠正了 code 也白纠正。"""
    diag = _diag(SABR_WARNINGS, FORMAT_UNAVAILABLE)

    assert "SABR" in diag.user_message
    assert "换档位没有用" in diag.user_message
    assert diag.user_title != Diagnosis(code="format_unavailable").user_title


def test_sabr_matches_either_warning_phrasing():
    """两个 substr 模式各自都要能单独命中（yt-dlp 换措辞时不至于全丢）。"""
    only_forcing = _diag(
        "WARNING: [youtube] abc: YouTube is forcing SABR streaming for this client",
        FORMAT_UNAVAILABLE,
    )
    only_missing_url = _diag(
        "WARNING: [youtube] abc: Some tv client https formats have been skipped "
        "as they are missing a url",
        FORMAT_UNAVAILABLE,
    )

    assert only_forcing.code == "sabr_formats_skipped"
    assert only_missing_url.code == "sabr_formats_skipped"


# ── 回归护栏：没有 SABR 信号时行为不变 ───────────────────────


def test_plain_format_unavailable_unchanged():
    """用户真挑错档位（无 SABR 警告）⇒ 判词与 fix_action 保持原样。"""
    diag = _diag(FORMAT_UNAVAILABLE)

    assert diag.code == "format_unavailable"
    assert diag.fix_action is None
    assert diag.retry.policy == "never"


def test_sabr_does_not_hijack_403():
    """SABR 警告 + 403：403 有自己的正解，不许被顶掉。"""
    diag = _diag(SABR_WARNINGS, HTTP_403)

    assert diag.code != "sabr_formats_skipped"
    assert diag.has_event("sabr_formats_skipped")  # 仍作为伴随信号留在事件流里


def test_sabr_warning_alone_does_not_become_primary():
    """rc != 0 但全场只有警告：不许把 SABR 报成整个任务的失败主因。

    这条护栏是 `_is_warning_only_primary` 的既有语义。下载其实已经完成、只是
    Windows 删不掉 `.part-Frag` 导致 rc != 0（`executor` 的体积校验照样抛异常）
    时走的就是这条路 —— 那一刻顶上来的警告与失败无关。
    """
    diag = diagnose(1, SABR_WARNINGS)

    assert diag.code == FALLBACK_CODE
    assert diag.has_event("sabr_formats_skipped")  # 伴随信号仍在，只是不当主因


def test_sabr_takes_over_unrecognized_error():
    """有 error 级事件但没有规则认得它 ⇒ SABR 是现场最好的解释，允许接管。

    与上一条成对：区别只在"到底有没有一条 ERROR: 行"。这里刻意不带
    ``[extractor]`` 前缀 —— 带了的话兜底会抽出 extractor 名并写好标题，
    那条判词信息量更大，按设计不许被 SABR 顶掉（见下一条）。
    """
    diag = _diag(SABR_WARNINGS, "ERROR: something nobody has a rule for")

    assert diag.code == "sabr_formats_skipped"
    assert diag.fix_action == "enable_pot_provider"


def test_sabr_yields_to_informative_fallback_title():
    """兜底已经抽出 extractor 名 / HTTP 码时，那个判词更具体，SABR 不许顶掉。"""
    with_extractor = _diag(SABR_WARNINGS, "ERROR: [somesite] abc: no rule matches this")

    assert with_extractor.code == FALLBACK_CODE
    assert with_extractor.override_title
    assert with_extractor.code != "sabr_formats_skipped"


# ── 伴随信号叠加 ─────────────────────────────────────────────


def test_stale_toolchain_upgrades_fix_action_after_sabr():
    """SABR + nsig 提取失败 ⇒ 更新组件才是首选动作（证明"刻意不 return"是对的）。"""
    diag = _diag(SABR_WARNINGS, NSIG_FAILED, FORMAT_UNAVAILABLE)

    assert diag.code == "sabr_formats_skipped"
    assert diag.fix_action == "update_component"
    assert diag.recovery_hint


def test_js_runtime_missing_still_wins_over_sabr():
    """缺 JS runtime 时正解是装 Deno，那条分支必须先 return。"""
    diag = _diag(
        "WARNING: [youtube] abc: No supported JavaScript runtime "
        "(Deno) found. Some formats may be missing",
        SABR_WARNINGS,
        FORMAT_UNAVAILABLE,
    )

    assert diag.fix_action == "install_js_runtime"
    assert diag.code != "sabr_formats_skipped"


# ── phase：失败发生在哪一步 ──────────────────────────────────


def test_phase_shows_up_in_technical_detail():
    diag = _diag(SABR_WARNINGS, FORMAT_UNAVAILABLE, phase="select")

    assert diag.phase == "select"
    assert "phase=select" in diag.technical_detail
    assert diag.technical_detail.startswith("exit_code=1 phase=select")


def test_phase_absent_keeps_old_detail_head():
    """算不出阶段的调用点（展示层重算诊断）不许多出一个空 `phase=`。"""
    diag = _diag(FORMAT_UNAVAILABLE)

    assert diag.phase == ""
    assert "phase=" not in diag.technical_detail
    assert diag.technical_detail.startswith("exit_code=1\n")


def test_phase_survives_dict_round_trip():
    """`to_dict()` 要过 Qt Signal，phase 丢了 UI 就看不到阶段。"""
    diag = _diag(SABR_WARNINGS, FORMAT_UNAVAILABLE, phase="select")
    data = diag.to_dict()

    assert data["phase"] == "select"
    assert "phase=select" in data["technical_detail"]
    assert Diagnosis.from_dict(data).phase == "select"


def test_dict_payload_carries_fix_action_for_dialog_buttons():
    """弹窗按钮按 `fix_action` / `events` 收敛，两者都必须出现在载荷里。"""
    data = _diag(SABR_WARNINGS, FORMAT_UNAVAILABLE).to_dict()

    assert data["fix_action"] == "enable_pot_provider"
    assert data["recovery_hint"]
    assert data["category"] == "toolchain"
    assert "sabr_formats_skipped" in {ev["code"] for ev in data["events"]}


# ── executor 算出的 phase ────────────────────────────────────

#: 结构化进度行 —— `output_parser` 判成 `type="progress"`，意味着字节已经开始走。
PROGRESS_LINE = b"FLUENTYTDL|download|1048576|10485760|NA|1048576|9|avc1|mp4a|mp4|v.mp4"
#: 格式决策行 —— yt-dlp 选定格式后才会打，是 parse → select 的无歧义分界。
FORMAT_DECISION_LINE = b"[info] WfAU5IDLbdw: Downloading 1 format(s): 137+251"


def _phase_of(*out_lines: bytes) -> str:
    """跑一遍 executor 的失败路径，取回它判定的阶段。"""
    process = MagicMock()
    process.stdout = BytesIO(b"\n".join(out_lines))
    process.wait.return_value = 1
    process.returncode = 1

    with (
        patch("fluentytdl.download.executor.resolve_yt_dlp_exe", return_value=Path("yt-dlp.exe")),
        patch("fluentytdl.download.executor.subprocess.Popen", return_value=process),
    ):
        try:
            DownloadExecutor().execute(
                url="https://youtube.com/watch?v=mock_id_456",
                ydl_opts={"format": "best_mp4"},
                on_progress=lambda _e: None,
                on_status=lambda _s: None,
                on_path=lambda _p: None,
                cancel_check=lambda: False,
            )
        except YtDlpExecutionError as exc:
            return exc.phase
    raise AssertionError("rc=1 且无有效产物时应抛 YtDlpExecutionError")


def test_executor_reports_parse_phase_for_the_reported_failure():
    """本次故障的真实形态：一条格式决策行都没有 ⇒ 死在拿元数据这一步。

    这正是用户在任务卡上看到"正在拉取元数据"却收到弹窗的那一刻 —— 弹窗从此
    自己说得出来，不必让用户去猜。
    """
    phase = _phase_of(SABR_WARNINGS.encode(), FORMAT_UNAVAILABLE.encode())

    assert phase == "parse"


def test_executor_reports_select_phase_after_format_decision():
    """格式已选定但一个字节都没走 ⇒ select。"""
    phase = _phase_of(FORMAT_DECISION_LINE, b"ERROR: unable to download video data")

    assert phase == "select"


def test_executor_reports_download_phase_once_bytes_move():
    """出现过进度行 ⇒ download，且后续 info 行不许把它退回去。"""
    phase = _phase_of(
        FORMAT_DECISION_LINE,
        PROGRESS_LINE,
        b"[info] WfAU5IDLbdw: Downloading 1 format(s): 137+251",
        b"ERROR: unable to download video data: HTTP Error 403: Forbidden",
    )

    assert phase == "download"


def test_executor_phase_reaches_the_dialog_payload():
    """端到端：executor 的 phase 一路走到弹窗读的那个字典里。"""
    phase = _phase_of(SABR_WARNINGS.encode(), FORMAT_UNAVAILABLE.encode())
    data = diagnose(1, "\n".join((SABR_WARNINGS, FORMAT_UNAVAILABLE)), phase=phase).to_dict()

    assert data["phase"] == "parse"
    assert data["technical_detail"].startswith("exit_code=1 phase=parse")
    assert data["code"] == "sabr_formats_skipped"
