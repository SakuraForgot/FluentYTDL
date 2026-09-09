"""`download/staging.py` 的单测 —— 下载产物事务层，纯逻辑、不需要 Qt。

用例编号与实施计划的验证清单一一对应（1 整组命名 … 26 占位符不是交付物），
每个用例的 docstring 第一行写的是它守住的那条性质，而不是它调了哪个函数 ——
用例失效时要能一眼看出「哪条不变量塌了」。

三条约定：

- **不 mock 文件系统。** 这一层的价值全在真实的 `os.replace` / `O_EXCL` /
  fingerprint 上，mock 掉它们等于测一个不存在的模块。故障注入只包一层真
  `os.replace`，按路径谓词选择性抛错。
- **信号用 `events` fixture 收，不去读日志文件。** 断言的是 `kind=signal` 的
  `code`，那是事务层唯一的对外诊断通道。
- **`_RESERVED_DESTS` 是模块级的**，每个用例前后都清 —— 否则一个用例的预留会让
  下一个用例莫名避让一位。

计划里的用例 14（`apply_subtitle_delivery()` 的四种保留组合）属于 Step 6，那个
函数还不存在，所以标了 skip 而不是悄悄不写。用例 13 / 15 / 23 / 24 各有一半依赖
后续 Step 的调用侧，这里只测 `staging.py` 自己那一半，docstring 里写明了边界。
"""

from __future__ import annotations

import ast
import errno
import inspect
import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

# logger 在 import 期就 `user_data_dir() / "logs"` 并 makedirs（`observability/events.py`
# 自己 import 它），所以数据目录必须在 import fluentytdl 之前挪走。
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-staging-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.download import staging  # noqa: E402
from fluentytdl.download.staging import (  # noqa: E402
    CommitFailed,
    StagingArea,
    StagingCancelled,
    StagingError,
    StagingEscape,
    VerifyBlocked,
    gc_orphans,
)

MEDIA_BYTES = 20 * 1024
"""比 `MIN_VALID_MEDIA_BYTES` 大一截 —— 用例不该踩在阈值上。"""


# ── fixtures / helpers ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_reserved():
    staging._RESERVED_DESTS.clear()
    yield
    staging._RESERVED_DESTS.clear()


@pytest.fixture
def events(monkeypatch):
    """收 `emit_event`。顺带让用例完全不写真实日志。"""
    seen: list[tuple[str, dict]] = []

    def record(kind, /, *, trace=None, level="INFO", stage=None, fields=None, _depth=1, **kwargs):
        merged = dict(fields or {})
        merged.update(kwargs)
        merged["_level"] = level
        merged["_stage"] = stage
        seen.append((str(kind), merged))

    monkeypatch.setattr(staging, "emit_event", record)
    return seen


def codes(seen: list[tuple[str, dict]]) -> list[str]:
    return [f.get("code") for k, f in seen if k == "signal"]


def write(path: str, size: int = 64, byte: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(byte * size)
    return path


def make_dl(tmp_path: Path, name: str = "dl") -> str:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def make_area(
    tmp_path: Path,
    *,
    task: str = "42",
    dl: str | None = None,
    staging_id: str | None = None,
    group_stem: str | None = None,
    cancel_check=None,
    trace_run_id: str = "abc123",
) -> StagingArea:
    return StagingArea.create(
        dl or make_dl(tmp_path),
        task,
        staging_id,
        trace_run_id=trace_run_id,
        group_stem=group_stem,
        cancel_check=cancel_check,
    )


def seed_group(
    area: StagingArea,
    *,
    stem: str = "Title",
    media_ext: str = ".mp4",
    langs: tuple[str, ...] = ("en-GB", "zh-Hans"),
    thumb: bool = True,
    media_size: int = MEDIA_BYTES,
    opts=None,
) -> dict[str, str]:
    """标准一组：主媒体 + 若干字幕 + 封面。返回 `role → artifact_id`。"""
    ids: dict[str, str] = {}
    p = area.payload_dir
    media = write(os.path.join(p, stem + media_ext), media_size)
    ids["media"] = area.add_reported(media, "media", primary=True).id
    for lang in langs:
        sub = write(os.path.join(p, f"{stem}.{lang}.vtt"), 128)
        ids[f"sub:{lang}"] = area.add_reported(sub, "subtitle", opts=opts).id
    if thumb:
        img = write(os.path.join(p, f"{stem}.jpg"), 256)
        ids["thumbnail"] = area.add_reported(img, "thumbnail").id
    return ids


def run_commit(area: StagingArea, final_opts=None):
    """`reconcile → seal → verify → build_plan → commit` 的完整正常路径。"""
    area.reconcile(final_opts)
    area.seal_discovery()
    area.verify(final_opts or {})
    plan = area.build_plan()
    area.commit(plan)
    return plan


def names_in(directory: str) -> set[str]:
    return {n for n in os.listdir(directory) if n != staging.SANDBOX_ROOT_NAME}


def patch_replace(monkeypatch, should_fail, exc: BaseException | None = None, hook=None):
    """选择性让 `os.replace` 抛错。真调用照旧 —— 不 mock 文件系统。

    `staging.os` 就是 `os`，所以这一层包在全局 `os.replace` 上；谓词按 (src, dst)
    精确匹配，journal 的 tmp→json 写入不受影响。
    """
    real = os.replace

    def fake(src, dst, *args, **kwargs):
        if should_fail(str(src), str(dst)):
            if hook is not None:
                hook(str(src), str(dst))
            raise exc or OSError(errno.EACCES, "injected")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(staging.os, "replace", fake)
    return real


def code_without_docstring(func) -> str:
    """函数体源码（去掉 docstring）—— 用来断言「代码里没引用某个概念」。

    直接 substring 查 docstring 会假阳性：`verify()` 的 docstring 正是在解释
    「为什么不能消费 `delivered:*`」。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    fn = tree.body[0]
    body = list(getattr(fn, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


def attr_call_count(func, name: str) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    )


def make_orphan(dl: str, task: str, sid: str, phase: str | None, placeholders=None) -> str:
    """手搓一个孤儿沙盒 —— GC 的输入是磁盘状态，不是活对象。"""
    txn = os.path.join(dl, staging.SANDBOX_ROOT_NAME, f"task_{task}", f"txn_{sid}")
    os.makedirs(os.path.join(txn, staging.CONTROL_NAME), exist_ok=True)
    os.makedirs(os.path.join(txn, staging.PAYLOAD_NAME), exist_ok=True)
    if phase is not None:
        with open(
            os.path.join(txn, staging.CONTROL_NAME, staging.JOURNAL_NAME), "w", encoding="utf-8"
        ) as fh:
            json.dump(
                {
                    "version": staging.JOURNAL_VERSION,
                    "phase": phase,
                    "placeholders": placeholders or {},
                },
                fh,
            )
    return txn


# ── 0. 后缀表分工是 artifacts._IGNORED_EXTS 的真分割 ────────


def test_ext_tables_partition_ignored_exts():
    """`metadata` / `intermediate` 两张表的并集必须等于观测侧的 `_IGNORED_EXTS`。

    模块 docstring 明文说这条由本文件机械保证。那边加一种后缀而这边漏掉，只会
    表现成「有时把 `.description` 当成 media」—— 一个 kind 判错，全组命名跟着错。
    """
    from fluentytdl.observability.artifacts import _IGNORED_EXTS

    assert staging._METADATA_EXTS | staging._INTERMEDIATE_EXTS == _IGNORED_EXTS
    assert not (staging._METADATA_EXTS & staging._INTERMEDIATE_EXTS)


# ── 1. 整组命名（缺陷 A）────────────────────────────────────


def test_01_group_rename_keeps_language_segment(tmp_path, events):
    """撞名时整组同改后缀，语言段不被逐文件去重打散。

    旧代码对 `os.walk` 出的每个文件独立调 `get_unique_path()`：视频变
    `Title (1).mp4` 而字幕留在 `Title.en-GB.vtt`，于是播放器不再自动挂载、
    `_subtitle_lang_from_name()` 把 `en (1)` 判成 `subtitle:any`、`aux_files()`
    也再也找不到它们（「彻底删除」删不干净）。
    """
    dl = make_dl(tmp_path)
    occupied = write(os.path.join(dl, "Title.mp4"), 7, b"o")  # 别人的成品
    area = make_area(tmp_path, dl=dl)
    seed_group(area)

    run_commit(area)

    assert names_in(dl) == {
        "Title.mp4",
        "Title (1).mp4",
        "Title (1).en-GB.vtt",
        "Title (1).zh-Hans.vtt",
        "Title (1).jpg",
    }
    # 沙盒外的既有文件绝不被动
    with open(occupied, "rb") as fh:
        assert fh.read() == b"o" * 7
    assert area.manifest.primary_media().final_path == os.path.join(dl, "Title (1).mp4")


# ── 2. 对账不丢历史（presence）──────────────────────────────


def test_02_reconcile_adds_unreported(tmp_path, events):
    """盘上有而清单没有的补录成 `origin="reconciled"`，并落 `manifest_reconciled`。"""
    area = make_area(tmp_path)
    write(os.path.join(area.payload_dir, "Title.mp4"), MEDIA_BYTES)

    stats = area.reconcile({})

    assert stats["added"] == 1
    art = area.manifest.get(os.path.normcase("Title.mp4"))
    assert art is not None and art.origin == "reconciled" and art.kind == "media"
    assert art.primary is True  # 唯一候选，reconcile 里补的
    assert "manifest_reconciled" in codes(events)
    assert "primary_media_inferred" in codes(events)


def test_02b_reconcile_keeps_vanished_records(tmp_path, events):
    """清单有而盘上没有的**不删记录**，只改 `presence` —— 历史事实不许消失。

    删记录的话 `emit_actual` 就得从别处补历史，而 `artifacts.py` 的契约是
    「yt-dlp 报告过创建」而非「此刻磁盘上还在」。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=("ja",), thumb=False)
    os.remove(os.path.join(area.payload_dir, "Title.ja.vtt"))

    stats = area.reconcile({})

    assert stats["missing"] == 1
    sub = area.manifest.require(ids["sub:ja"])
    assert sub.presence == "missing"
    assert sub in area.manifest.observed()  # 进 emit_actual
    assert sub not in area.manifest.present()
    assert sub not in area.manifest.kept()  # 不进 build_plan
    assert "manifest_reconciled" in codes(events)


def test_02c_reconcile_refuses_after_seal(tmp_path, events):
    """对账是**发现**行为，封板后必须抛。

    这条同时保证 `.internal/` 里被 supersede 的旧内容永远不会被对账捡回来 ——
    那些内容只在封板之后产生。
    """
    area = make_area(tmp_path)
    seed_group(area)
    area.seal_discovery()
    with pytest.raises(StagingError):
        area.reconcile({})


# ── 3. control plane 不进清单 ───────────────────────────────


def test_03_control_plane_never_enters_manifest(tmp_path, events):
    """`.fytdl/` `.parts/` `.work/` `.internal/` 在物理上就不在 `reconcile()` 的视野里。

    分区本身就是排除，所以不需要维护一张「排除自己的控制文件」黑名单。
    """
    area = make_area(tmp_path)
    area.prepare_attempt(0)
    write(os.path.join(area.parts_dir, "Title.mp4.part"), 4096)
    write(os.path.join(area.work_dir, "candidate.mp4"), 4096)
    write(os.path.join(area.internal_dir, "old.mp4"), 4096)
    seed_group(area, langs=(), thumb=False)

    run_commit(area)

    ids = {a.id for a in area.manifest.observed()}
    assert ids == {os.path.normcase("Title.mp4")}
    assert names_in(area.download_dir) == {"Title.mp4"}
    assert os.path.isfile(area.journal_path())  # 控制文件还在原处


def test_03b_intermediate_always_internal_metadata_follows_intent(tmp_path, events):
    """`intermediate` 恒为 `internal`；`metadata` 由 `writeinfojson` 决定。

    写死成 internal 的话，以后加「保存 info.json」开关就得回头改 Kind/Disposition
    的语义 —— 那正是这一层要消灭的「同一件事两处判定」。
    """
    for wanted, expect_dest in ((False, "internal"), (True, "deliver")):
        area = make_area(tmp_path, task=f"m{int(wanted)}", dl=make_dl(tmp_path, f"dl{int(wanted)}"))
        write(os.path.join(area.payload_dir, "Title.mp4"), MEDIA_BYTES)
        write(os.path.join(area.payload_dir, "Title.info.json"), 32)
        write(os.path.join(area.payload_dir, "Title.f399.mp4"), 4096)  # DASH 分片
        opts = {"writeinfojson": wanted}

        area.reconcile(opts)

        meta = area.manifest.require(os.path.normcase("Title.info.json"))
        frag = area.manifest.require(os.path.normcase("Title.f399.mp4"))
        assert meta.kind == "metadata" and meta.disposition == expect_dest
        assert frag.kind == "intermediate" and frag.disposition == "internal"

        area.seal_discovery()
        area.verify(opts)
        area.commit(area.build_plan())
        assert ("Title.info.json" in names_in(area.download_dir)) is wanted
        assert "Title.f399.mp4" not in names_in(area.download_dir)


# ── 4. 发现封板 ≠ 生产封板 ─────────────────────────────────


def test_04_seal_blocks_discovery_not_creation(tmp_path, events):
    """封住的是**推断**，不是**创造** —— 否则 VRFeature 根本无法工作。"""
    area = make_area(tmp_path)
    seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()

    with pytest.raises(StagingError):
        area.add_reported(os.path.join(area.payload_dir, "New.mp4"), "media")
    with pytest.raises(StagingError):
        area.manifest.add_reconciled(os.path.join(area.payload_dir, "New.mp4"), "media")

    work = area.reserve_workfile("Title", ".mp4")
    write(work, MEDIA_BYTES)
    art = area.register_generated(
        work, kind="media", producer="VRFeature", target_name="Title_equi.mp4"
    )
    assert art.origin == "generated" and art.producer == "VRFeature"
    assert os.path.isfile(art.path) and not os.path.exists(work)


def test_04b_register_generated_rejects_missing_and_outside(tmp_path, events):
    """前置条件全部强制：必须已存在、必须在沙盒内、`producer` 必填。"""
    area = make_area(tmp_path)
    with pytest.raises(StagingError):
        area.register_generated(
            os.path.join(area.work_dir, "nope.mp4"), kind="media", producer="X"
        )

    outside = write(str(tmp_path / "outside.mp4"), 128)
    with pytest.raises(StagingEscape):
        area.register_generated(outside, kind="media", producer="X")

    work = write(area.reserve_workfile("t", ".mp4"), 128)
    with pytest.raises(StagingError):
        area.register_generated(work, kind="media", producer="")


# ── 5. VR 两条分支走原子 API ───────────────────────────────


def test_05_vr_keep_source_delivers_both(tmp_path, events):
    """`vr_keep_source=True`（默认）下两个 media 合法共存，且整组改名后各自的
    `member_tail` 都保住。
    """
    dl = make_dl(tmp_path)
    write(os.path.join(dl, "Title.mp4"), 3, b"o")  # 制造整组避让
    area = make_area(tmp_path, dl=dl)
    ids = seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()

    work = write(area.reserve_workfile("Title", ".mp4"), MEDIA_BYTES, b"e")
    equi = area.register_generated(
        work, kind="media", producer="VRFeature", parent_ids=[ids["media"]],
        target_name="Title_equi.mp4",
    )
    area.rename_artifact(ids["media"], "Title.eac.mp4")
    area.rename_artifact(equi.id, "Title.mp4")
    area.manifest.promote(equi.id)

    area.verify({})
    area.commit(area.build_plan())

    assert names_in(dl) == {"Title.mp4", "Title (1).mp4", "Title (1).eac.mp4"}
    assert area.manifest.primary_media().id == equi.id
    assert equi.final_path == os.path.join(dl, "Title (1).mp4")
    assert area.manifest.require(ids["media"]).final_path == os.path.join(dl, "Title (1).eac.mp4")


def test_05b_vr_drop_source_supersedes_without_deleting(tmp_path, events):
    """`vr_keep_source=False` 也**不删源** —— 只移进 `.internal/` + 标 internal。

    「物理删除只发生在 `rmtree`」这条不变量不为 VR 破例；顺带好处是提交前发现
    转码产物有问题时旧源还在。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()

    work = write(area.reserve_workfile("Title", ".mp4"), MEDIA_BYTES, b"e")
    equi = area.register_generated(
        work, kind="media", producer="VRFeature", target_name="Title_equi.mp4"
    )
    area.supersede_artifact(ids["media"], equi.id, reason="vr_transcode")
    area.rename_artifact(equi.id, "Title.mp4")

    old = area.manifest.require(ids["media"])
    assert old.disposition == "internal"
    assert old.keep is True  # 不是 drop()
    assert old.presence == "present" and os.path.isfile(old.path)
    assert os.path.dirname(old.path) == area.internal_dir
    assert [a.id for a in area.manifest.kept("media")] == [equi.id]
    assert equi.primary is True

    area.verify({})
    area.commit(area.build_plan())
    assert names_in(area.download_dir) == {"Title.mp4"}
    with open(os.path.join(area.download_dir, "Title.mp4"), "rb") as fh:
        assert fh.read(1) == b"e"


def test_05c_failed_transcode_workfile_never_registered(tmp_path, events):
    """转码失败时 workfile 从未登记，随沙盒 `rmtree` 消失，清单无感。"""
    area = make_area(tmp_path)
    seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    work = write(area.reserve_workfile("Title", ".mp4"), 8)  # ffmpeg 半途死掉

    area.verify({})
    area.commit(area.build_plan())

    assert len(area.manifest) == 1
    assert names_in(area.download_dir) == {"Title.mp4"}
    assert os.path.isfile(work)  # 还在 .work/ 里，等 rmtree
    area.cleanup()
    assert not os.path.exists(area.txn_dir)


# ── 6. assert_inside ────────────────────────────────────────


def test_06_assert_inside_hard_fails(tmp_path, events):
    """包含性是最底层的 invariant，判据是 `realpath` + `commonpath`，不是字符串前缀。

    相对 `outtmpl` 不等于在沙盒内 —— `../../foo.mp4` 也是相对路径。
    """
    area = make_area(tmp_path)
    with pytest.raises(StagingEscape):
        area.assert_inside(os.path.join(area.payload_dir, "..", "..", "x.mp4"))
    with pytest.raises(StagingEscape):
        area.assert_inside(str(tmp_path / "x.mp4"))
    # 沙盒内的正常路径必须原样通过
    inside = os.path.join(area.payload_dir, "sub", "x.mp4")
    assert area.assert_inside(inside) == os.path.realpath(inside)


def test_06b_assert_inside_rejects_other_volume(tmp_path, events):
    """跨盘时 `commonpath` 抛 `ValueError` —— 那当然也是逃逸，不能漏成 True。"""
    area = make_area(tmp_path)
    used = os.path.splitdrive(os.path.abspath(area.payload_dir))[0].upper()
    other = "Z:" if used != "Z:" else "Y:"
    if not used:
        pytest.skip("非 Windows：无盘符概念")
    with pytest.raises(StagingEscape):
        area.assert_inside(other + os.sep + "x.mp4")


def test_06c_assert_inside_follows_symlink(tmp_path, events):
    """payload 里指向外面的 symlink/junction 必须被认出来。"""
    area = make_area(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = os.path.join(area.payload_dir, "link")
    try:
        os.symlink(str(target), link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接")
    with pytest.raises(StagingEscape):
        area.assert_inside(os.path.join(link, "x.mp4"))


# ── 6bis. 报告路径几乎全在 temp 侧（实测结论，不是推理）─────────
#
# `home != temp` 之后 yt-dlp 报告的是它此刻正在写的那个路径，而它写的是 temp。
# 实测（`yt-dlp.exe` 2026.08.30，`-P home:<payload> -P temp:<parts>`，真实下载）：
#
#     [info] Writing video subtitles to:        <parts>\Me at the zoo.en.vtt
#     [download] Destination:                   <parts>\Me at the zoo.f395.mp4
#     [info] Writing video thumbnail 37 to:     <parts>\Me at the zoo.webp
#     [Merger] Merging formats into             "<parts>\Me at the zoo.webm"
#     [info] Writing video metadata as JSON to: <payload>\Me at the zoo.info.json   ← 唯一直写 home
#     [MoveFiles] Moving file "<parts>\…" to "<payload>\…"
#
# 所以 `add_reported()` 要是直接 `assert_inside(payload)`，**每一个文件都会
# `StagingEscape`**，整条下载路径当场硬失败 —— 这是接线 `workers.py` 之前必须先
# 关掉的门。`landing_path()` 是那道换算，下面四条用例钉住它的边界。


def test_06d_reported_temp_path_lands_in_payload(tmp_path, events):
    """报告路径在 `.parts/` 时换算成它**最终落在 payload 里的那个路径**。

    换算规则是「相对 `.parts/` 的路径照搬到 `payload/` 下」：两侧 basename 恒等
    （temp 名与 home 名由同一个 outtmpl 渲染，只有目录不同），所以这是等价映射。
    """
    area = make_area(tmp_path)
    reported = os.path.join(area.parts_dir, "Title.en-GB.vtt")

    art = area.add_reported(reported, "subtitle")

    assert art.path == os.path.join(area.payload_dir, "Title.en-GB.vtt")
    assert art.id == os.path.normcase("Title.en-GB.vtt")
    assert art.qualifier == "en-GB"  # 语言键从换算后的 basename 上取，不受目录影响


def test_06e_temp_and_move_reports_collapse_to_one_id(tmp_path, events):
    """同一个文件先被报告在 temp、后被 `[MoveFiles]` 报告在 home ⇒ 清单里只有一项。

    这是选换算而不是「只记笔记」的直接理由：两个报告落在同一个 `id` 上，由
    `Manifest._put()` 自然去重，不需要在 Worker 侧攒一张 temp→home 的对照表。
    """
    area = make_area(tmp_path)

    first = area.add_reported(os.path.join(area.parts_dir, "Title.mp4"), "media", primary=True)
    second = area.add_reported(os.path.join(area.payload_dir, "Title.mp4"), "media")

    assert first.id == second.id
    assert first is second
    assert first.primary is True  # 已有记录胜出，primary 只被补强不被抹掉
    assert len(area.manifest.observed()) == 1


def test_06f_landing_path_does_not_require_the_file_to_exist(tmp_path, events):
    """换算之后 artifact 的 `path` 指向一个**此刻还不存在**的文件 —— 这不影响消费者。

    `presence` 的真相由紧接着的 `reconcile()` 统一裁决（找不到就标 `missing` /
    `consumed`），而 `observed()` 要的正是「报告过创建」这个历史事实。所以登记时
    不 stat、也不因为文件还在 temp 侧就拒绝登记。
    """
    area = make_area(tmp_path)
    art = area.add_reported(os.path.join(area.parts_dir, "Title.ja.srt"), "subtitle")

    assert not os.path.exists(art.path)
    assert [a.id for a in area.manifest.observed()] == [art.id]

    # 对账才是裁决点：盘上没有 ⇒ 记录留着，只是不再 present
    area.reconcile()
    assert art.presence != "present"
    assert [a.id for a in area.manifest.observed()] == [art.id]
    assert area.manifest.kept("subtitle") == []


def test_06g_control_plane_and_outside_paths_still_hard_fail(tmp_path, events):
    """换算只从 `.parts/` 做。控制面与沙盒外仍是硬失败，不降级。

    `.work` / `.internal` / `.fytdl` 是我们自己的控制面，yt-dlp 不会报告它们；要是
    也一并换算，一个写歪的内部落点就会被当成用户产物交付出去。
    """
    area = make_area(tmp_path)
    for outside in (
        os.path.join(area.work_dir, "tmp1234.mp4"),
        os.path.join(area.internal_dir, "old.mp4"),
        os.path.join(area.control_dir, "final.0.txt"),
        str(tmp_path / "Title.mp4"),
        os.path.join(area.payload_dir, "..", "..", "Title.mp4"),
    ):
        with pytest.raises(StagingEscape):
            area.landing_path(outside)


def test_06h_parts_bytes_is_the_section_progress_probe(tmp_path, events):
    """`--download-sections` 时的进度兜底探针只读字节数，不推断任何角色。

    那段代码原先扫的是 `paths["home"]`，`home != temp` 之后那里永远是 0 字节，
    进度条会静默卡死；扫描收敛到事务层，因为它是唯一允许物理扫描的地方。
    """
    area = make_area(tmp_path)
    assert area.parts_bytes() == 0

    write(os.path.join(area.parts_dir, "Title.f395.mp4.part"), 1500)
    write(os.path.join(area.parts_dir, "Title.f251.webm.part"), 700)
    write(os.path.join(area.parts_dir, "Title.f395.mp4.ytdl"), 99)  # 不是 .part，不计
    write(os.path.join(area.payload_dir, "Title.mp4"), 4096)  # 不在 .parts/，不计

    assert area.parts_bytes() == 2200

    # 沙盒已经 rmtree 掉时也不能抛 —— 它跑在独立线程里，抛出去没人接
    area.discard()
    assert area.parts_bytes() == 0


# ── 7. typed paths ──────────────────────────────────────────


def test_07_typed_paths_sandboxed_but_intent_preserved(tmp_path, events):
    """`-P [TYPES:]PATH` 的**语义**保住，**值**不保住：每个类型子键都进 payload，
    用户想要的目的地只在 commit plan 里复现。
    """
    dl = make_dl(tmp_path)
    subs_dir = str(tmp_path / "subs")
    area = make_area(tmp_path, dl=dl)
    opts = {"paths": {"home": dl, "subtitle": subs_dir}}

    area.apply_to_opts(opts)

    assert opts["paths"] == {
        "home": area.payload_dir,
        "subtitle": area.payload_dir,
        "temp": area.parts_dir,
    }
    assert opts["paths"]["home"] != opts["paths"]["temp"]  # .part 不再与产物混层
    assert area.dest_intent["subtitle"] == os.path.abspath(subs_dir)

    seed_group(area, langs=("en",))
    run_commit(area)

    assert names_in(dl) == {"Title.mp4", "Title.jpg"}
    assert set(os.listdir(subs_dir)) == {"Title.en.vtt"}


def test_07b_dest_intent_inside_sandbox_degrades(tmp_path, events):
    """指向沙盒内部的目的地降级回 `home` 并记信号 —— 不静默通过。"""
    area = make_area(tmp_path)
    opts = {"paths": {"home": area.download_dir, "subtitle": area.payload_dir}}
    area.apply_to_opts(opts)
    assert area.dest_intent["subtitle"] == area.download_dir
    assert "dest_intent_inside_sandbox" in codes(events)


# ── 8. 并发预留 ─────────────────────────────────────────────


def test_08_concurrent_commit_never_overwrites(tmp_path, events):
    """胜负由 `O_EXCL` 裁决，不是 `os.path.exists()`。

    旧代码用裸 `exists()`：两个 worker 可以同时看到 `Title (1).mp4` 空闲并同时选中
    它，`shutil.move` 在 Windows 上 rename 失败后回落 `copy2`，于是**静默覆盖**
    另一个任务的成品。
    """
    dl = make_dl(tmp_path)
    write(os.path.join(dl, "Title.mp4"), 3, b"o")

    areas = []
    for i in range(2):
        area = make_area(tmp_path, task=f"t{i}", dl=dl)
        seed_group(area, langs=("en",), thumb=False)
        area.reconcile({})
        area.seal_discovery()
        area.verify({})
        areas.append((area, area.build_plan()))

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(area, plan):
        try:
            barrier.wait(timeout=10)
            area.commit(plan)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=ap) for ap in areas]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    groups = []
    for area, _plan in areas:
        published = sorted(os.path.basename(p) for p in area.published_paths())
        assert len(published) == 2
        stems = {os.path.basename(p).split(".")[0] for p in published}
        assert len(stems) == 1, f"成员错组: {published}"  # 一个组只能有一个 stem
        groups.append(sorted(published))

    assert groups[0] != groups[1]
    assert names_in(dl) == {"Title.mp4"} | {n for g in groups for n in g}
    assert not staging._RESERVED_DESTS  # 成功也必须释放


# ── 9. 跨卷两阶段 ───────────────────────────────────────────


def test_09_cross_volume_two_phase(tmp_path, events, monkeypatch):
    """跨卷走 `copy2 → 校验 → os.replace(tmp, dst)`；`.tmp` 与 dst 同卷所以仍原子。"""
    monkeypatch.setattr(staging, "_same_volume", lambda *a: False)
    area = make_area(tmp_path)
    seed_group(area, langs=("en",), thumb=False)

    run_commit(area)

    dl = area.download_dir
    assert names_in(dl) == {"Title.mp4", "Title.en.vtt"}
    assert not [n for n in os.listdir(dl) if n.endswith(".tmp")]
    # 源**不删** —— 它随沙盒 rmtree 消失，回滚时也还有一份完整的源
    assert os.path.isfile(os.path.join(area.payload_dir, "Title.mp4"))


def test_09b_cross_volume_failure_cleans_tmp(tmp_path, events, monkeypatch):
    """`os.replace(tmp, dst)` 失败 ⇒ `.tmp` 被清、源仍在 payload 完整。"""
    monkeypatch.setattr(staging, "_same_volume", lambda *a: False)
    area = make_area(tmp_path)
    seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    patch_replace(monkeypatch, lambda src, dst: src.endswith(".tmp"))

    with pytest.raises(CommitFailed):
        area.commit(plan)

    dl = area.download_dir
    assert not [n for n in os.listdir(dl) if n.endswith(".tmp")]
    assert names_in(dl) == set()
    src = os.path.join(area.payload_dir, "Title.mp4")
    assert os.path.getsize(src) == MEDIA_BYTES
    assert area.phase == "prepared"


# ── 10. 补偿回滚 + rollback_failed ─────────────────────────


def test_10_compensation_restores_group(tmp_path, events, monkeypatch):
    """一个成员失败 ⇒ 整组回滚：目标目录干净（含占位符已清）、成员回到 payload、
    phase 退回 `prepared`、沙盒保留、抛 `CommitFailed`。

    `CommitFailed` 这个类型是载重的：`finalize_failure()` 在 `prepared` 下按
    `retain_staging` 裁决，裸 `OSError` 会让刚收敛好的诊断现场被立刻 rmtree。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=("en",), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    dl = area.download_dir
    patch_replace(monkeypatch, lambda src, dst: dst == os.path.join(dl, "Title.mp4"))

    with pytest.raises(CommitFailed):
        area.commit(plan)

    assert names_in(dl) == set()  # 占位符也清了
    assert os.path.isfile(os.path.join(area.payload_dir, "Title.mp4"))
    assert os.path.isfile(os.path.join(area.payload_dir, "Title.en.vtt"))
    assert area.phase == "prepared"
    assert os.path.isdir(area.txn_dir)
    journal = staging._read_journal(area.txn_dir)
    assert journal["phase"] == "prepared"
    assert journal["items"][ids["sub:en"]]["state"] == "rolled_back"
    assert not staging._RESERVED_DESTS


def test_10b_rollback_failure_is_its_own_terminal_phase(tmp_path, events, monkeypatch):
    """补偿本身也失败时**不许**把 journal 写回 `prepared`。

    那会让 GC 以为「清掉占位符就能 rmtree」，而用户目录里其实躺着一个搬出去了
    又搬不回来的成品。所以 `rollback_failed` 是独立终态，GC 永不自动动它。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=("en",), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    dl = area.download_dir
    payload = area.payload_dir

    def fail(src, dst):
        if dst == os.path.join(dl, "Title.mp4"):
            return True  # 发布主媒体失败
        return dst.startswith(payload) and dst.endswith(".vtt")  # 回滚字幕也失败

    patch_replace(monkeypatch, fail)

    with pytest.raises(CommitFailed):
        area.commit(plan)

    assert area.phase == "rollback_failed"
    assert names_in(dl) == {"Title.en.vtt"}  # 明确不声称目标目录干净
    journal = staging._read_journal(area.txn_dir)
    assert journal["items"][ids["sub:en"]]["state"] == "rollback_failed"
    assert "commit_rollback_incomplete" in codes(events)

    stats = gc_orphans(dl, [], older_than_hours=0)
    assert stats["retained"] == 1 and stats["removed"] == 0
    assert os.path.isdir(area.txn_dir)
    assert "staging_orphan_rollback_failed" in codes(events)


# ── 11. write-ahead journal + ownership proof ──────────────


def test_11_journal_is_write_ahead(tmp_path, events, monkeypatch):
    """进入更危险的状态**先**写 journal：崩在 `os.replace` 与 journal 更新之间时，
    journal 自己也得知道「这一项可能已搬」。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    dl = area.download_dir
    captured: dict = {}

    patch_replace(
        monkeypatch,
        lambda src, dst: dst == os.path.join(dl, "Title.mp4"),
        hook=lambda src, dst: captured.update(staging._read_journal(area.txn_dir) or {}),
    )
    with pytest.raises(CommitFailed):
        area.commit(plan)

    assert captured["phase"] == "committing"
    assert captured["items"][ids["media"]]["state"] == "publishing"


def test_11b_gc_phase_table(tmp_path, events):
    """五种 phase 各一个孤儿沙盒：只有 `downloading` / `prepared` / `committed` /
    无 journal 可以直接 rmtree。
    """
    dl = make_dl(tmp_path)
    dirs = {
        phase or "none": make_orphan(dl, "1", f"sid{i}", phase)
        for i, phase in enumerate([None, "downloading", "prepared", "committed"])
    }
    dirs["committing"] = make_orphan(dl, "1", "sidC", "committing")
    dirs["rollback_failed"] = make_orphan(dl, "1", "sidR", "rollback_failed")

    gc_orphans(dl, [], older_than_hours=0)

    for key in ("none", "downloading", "prepared", "committed"):
        assert not os.path.exists(dirs[key]), key
    assert os.path.isdir(dirs["committing"])
    assert os.path.isdir(dirs["rollback_failed"])
    assert "staging_orphan_interrupted" in codes(events)
    assert "staging_orphan_rollback_failed" in codes(events)


def test_11c_gc_only_removes_provable_placeholders(tmp_path, events):
    """沙盒外只删能证明是自己刚写出来的：fingerprint 对得上**且**0 字节。

    `size != 0` 意味着那已经不是占位符而是某个成品，此时删除就是数据丢失。
    """
    dl = make_dl(tmp_path)
    ghost = write(os.path.join(dl, "Ghost.mp4"), 0)
    real = write(os.path.join(dl, "Real.mp4"), 5, b"r")  # 「其实是别人的成品」
    placeholders = {ghost: staging._fingerprint(ghost), real: staging._fingerprint(real)}
    txn = make_orphan(dl, "9", "sidP", "committing", placeholders)

    stats = gc_orphans(dl, [], older_than_hours=0)

    assert stats["placeholders_cleared"] == 1
    assert not os.path.exists(ghost)
    assert os.path.isfile(real)
    assert os.path.isdir(txn)  # committing 的沙盒原样保留


def test_11d_gc_skips_live_and_fresh(tmp_path, events):
    """活事务与新沙盒都不动 —— 判活只看目录名里的 `staging_id`，不必读 journal。"""
    dl = make_dl(tmp_path)
    live = make_orphan(dl, "1", "liveid", "downloading")
    fresh = make_orphan(dl, "1", "freshid", "downloading")

    gc_orphans(dl, ["liveid"], older_than_hours=6)

    assert os.path.isdir(live)
    assert os.path.isdir(fresh)


# ── 12. group_stem authority ───────────────────────────────


def test_12_subtitle_only_task_defines_stem(tmp_path, events):
    """纯字幕任务（`skip_download`）里首个字幕就是 authority。"""
    area = make_area(tmp_path)
    sub = write(os.path.join(area.payload_dir, "Title.ja.vtt"), 128)
    area.add_reported(sub, "subtitle")
    assert area.manifest.group_stem == "Title"
    assert area.manifest.stem_authority == "subtitle"

    opts = {"skip_download": True}
    run_commit(area, opts)

    assert names_in(area.download_dir) == {"Title.ja.vtt"}


def test_12b_media_authority_overrides_subtitle(tmp_path, events):
    """普通任务里主媒体一旦确定就该盖过字幕占的位。

    DASH 场景下 `Title.en.vtt` 确实比合并产物先落盘，但整组该跟着主媒体的名字走。
    """
    area = make_area(tmp_path)
    area.add_reported(write(os.path.join(area.payload_dir, "Sub.en.vtt"), 128), "subtitle")
    assert area.manifest.stem_authority == "subtitle"
    area.add_reported(
        write(os.path.join(area.payload_dir, "Real.mp4"), MEDIA_BYTES), "media", primary=True
    )
    area.seal_discovery()
    assert area.manifest.group_stem == "Real"
    assert area.manifest.stem_authority == "media"


def test_12c_explicit_stem_wins_and_missing_stem_raises(tmp_path, events):
    """cover-direct 的 `explicit` stem 不被 `seal_discovery()` 改；
    `group_stem` 为空时 `build_plan()` **抛**而不是猜。
    """
    area = make_area(tmp_path, group_stem="MyCover")
    area.add_reported(write(os.path.join(area.payload_dir, "raw.jpg"), 256), "thumbnail")
    area.reconcile({})
    area.seal_discovery()
    assert area.manifest.group_stem == "MyCover"
    area.commit(area.build_plan())
    assert names_in(area.download_dir) == {"MyCover.jpg"}

    bare = make_area(tmp_path, task="43", dl=make_dl(tmp_path, "dl2"))
    bare.add_reported(write(os.path.join(bare.payload_dir, "x.jpg"), 32), "thumbnail")
    bare.seal_discovery()
    assert bare.manifest.group_stem is None
    with pytest.raises(StagingError):
        bare.build_plan()


def test_12d_build_plan_refuses_duplicate_destinations(tmp_path, events):
    """两个成员映射到同一个目标 = 静默数据丢失，必须硬失败。

    自动去重不是选项：命名规则不许引入 uuid/时间戳后缀，能选的只有「盖掉一个」
    和「拒绝提交」，后者至少沙盒还留着。
    """
    area = make_area(tmp_path)
    area.add_reported(
        write(os.path.join(area.payload_dir, "Title.mp4"), MEDIA_BYTES), "media", primary=True
    )
    # 两条同 qualifier 同后缀的字幕（stdout 丢字符时 `_member_tail` 会走回落）
    a = write(os.path.join(area.payload_dir, "Ti★tle.en.vtt"), 64)
    b = write(os.path.join(area.payload_dir, "Ti☆tle.en.vtt"), 64)
    area.add_reported(a, "subtitle", qualifier="en")
    area.add_reported(b, "subtitle", qualifier="en")
    area.seal_discovery()

    with pytest.raises(VerifyBlocked):
        area.build_plan()
    assert "duplicate_destination" in [
        f.get("reason") for k, f in events if k == "signal" and f.get("code") == "verify_blocked"
    ]


# ── 13. 缺省即保留（缺陷 B）────────────────────────────────


def test_13_missing_keep_flag_means_keep(tmp_path, events):
    """**保留标志缺失 ⇒ 保留。**（今天是缺失 ⇒ 删除。）

    旧代码里挡在用户独立封面前面的只有 `__fluentytdl_keep_thumbnail`，而那个键有
    6 个设置点，其中一个藏在 `elif hasattr(self, "subtitle_check")` 里 —— 任何没有
    该控件的路径都不设它，于是键缺失就静默删掉封面。事务层这一侧的对应保证是：
    `staging.py` 从不读那个键，删除只能由 Feature 显式 `drop()` 表达。
    """
    area = make_area(tmp_path)
    seed_group(area, langs=())
    run_commit(area, {})  # opts 里没有任何 keep 标志

    assert "Title.jpg" in names_in(area.download_dir)
    src = code_without_docstring(staging.StagingArea.build_plan)
    assert "keep_thumbnail" not in src and "keep_subtitle" not in src


# ── 14. 四种保留组合（Step 6）──────────────────────────────


@pytest.mark.skip(reason="apply_subtitle_delivery() 属 Step 6，函数尚未存在")
def test_14_subtitle_delivery_matrix():
    """`embed × keep_external` 四态，`(False, False)` 必须产出 `writesubtitles=False`。"""


# ── 15. 只在成功证据下 drop ────────────────────────────────


def test_15_drop_requires_reason_and_keeps_history(tmp_path, events):
    """`drop()` 只改标记：被 drop 的项仍在 `observed()` 里，只是不再交付。"""
    area = make_area(tmp_path)
    ids = seed_group(area, langs=("en-GB",), thumb=False)
    with pytest.raises(StagingError):
        area.manifest.drop(kind="subtitle", reason="")

    n = area.manifest.drop(kind="subtitle", reason="embedded_successfully")
    assert n == 1
    sub = area.manifest.require(ids["sub:en-GB"])
    assert sub.keep is False and sub.presence == "present"
    assert sub in area.manifest.observed()
    assert sub not in area.manifest.kept()


def test_15b_consumed_needs_structured_evidence(tmp_path, events):
    """字幕消失只有在**有结构化嵌入证据**时才算 `consumed`，否则是 `missing`。

    人类日志行只作 fallback / 显示，不作 authority，也不再把结论绑在 yt-dlp 的 rc 上。
    """
    for evidence, expect in ((False, "missing"), (True, "consumed")):
        area = make_area(tmp_path, task=f"e{int(evidence)}", dl=make_dl(tmp_path, f"e{int(evidence)}"))
        ids = seed_group(area, langs=("en",), thumb=False)
        if evidence:
            area.manifest.embed_evidence.add("subtitle")
        os.remove(os.path.join(area.payload_dir, "Title.en.vtt"))

        area.reconcile({})

        assert area.manifest.require(ids["sub:en"]).presence == expect


def test_15c_missing_media_is_never_explained_away(tmp_path, events):
    """主媒体不见了必须是 `missing` —— 那正是要报出来的事，不能被解释掉。"""
    area = make_area(tmp_path)
    ids = seed_group(area, langs=(), thumb=False)
    area.manifest.embed_evidence.update({"subtitle", "thumbnail"})
    os.remove(os.path.join(area.payload_dir, "Title.mp4"))

    area.reconcile({})

    assert area.manifest.require(ids["media"]).presence == "missing"


# ── 16. finalize_* 越界（缺陷 F）───────────────────────────


def test_16_finalize_never_touches_outside_sandbox(tmp_path, events):
    """沙盒之外的路径永远不被删除。

    旧的 `_sweep_part_files()` 对 `output_path ∪ dest_paths` 无条件 `os.remove`，
    而成功上岸后 `output_path` 指向的是**已交付给用户的真实文件**。
    """
    dl = make_dl(tmp_path)
    keeper = write(os.path.join(dl, "Previous.mp4"), 9, b"k")

    for finalize in ("cancel", "failure"):
        area = make_area(tmp_path, task=finalize, dl=dl)
        seed_group(area, langs=("en",))
        if finalize == "cancel":
            assert area.finalize_cancel() == "cancelled"
        else:
            assert area.finalize_failure(RuntimeError("boom")) == "failed"
        assert not os.path.exists(area.txn_dir)
        assert os.path.isfile(keeper)

    with open(keeper, "rb") as fh:
        assert fh.read() == b"k" * 9


# ── 17. 校验门只做二值判断 ─────────────────────────────────


def test_17_verify_blocks_only_on_media(tmp_path, events):
    """safety gate ≠ contract evaluation：只回答「现在提交安全吗」。"""
    area = make_area(tmp_path)
    area.add_reported(write(os.path.join(area.payload_dir, "Title.en.vtt"), 64), "subtitle")
    area.seal_discovery()
    with pytest.raises(VerifyBlocked):
        area.verify({})  # 期望 media 而一个都没有
    assert "primary_media_missing" in [
        f.get("reason") for k, f in events if k == "signal" and f.get("code") == "verify_blocked"
    ]

    small = make_area(tmp_path, task="s", dl=make_dl(tmp_path, "s"))
    seed_group(small, langs=(), thumb=False, media_size=128)
    small.seal_discovery()
    with pytest.raises(VerifyBlocked):
        small.verify({})


def test_17b_missing_subtitle_still_commits(tmp_path, events):
    """少一条字幕绝不能变成扔掉整个视频的理由 —— 降级由 commit 之后的减法产出。"""
    area = make_area(tmp_path)
    ids = seed_group(area, langs=("ja",), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    # reconcile 之后、verify 之前被杀软隔离掉
    os.remove(os.path.join(area.payload_dir, "Title.ja.vtt"))

    area.verify({})

    ghost = area.manifest.require(ids["sub:ja"])
    assert ghost.presence == "missing" and ghost.reason == "vanished_before_commit"
    assert "staging_artifact_vanished" in codes(events)

    plan = area.build_plan()
    assert [m.artifact_id for m in plan.members] == [ids["media"]]
    area.commit(plan)
    assert names_in(area.download_dir) == {"Title.mp4"}


def test_17c_verify_never_consumes_delivered_tokens():
    """`delivered:*` 是提交后才成立的事实，提交前的门对它做减法就是生命周期倒置。

    每个正常下载都会「缺」`delivered:media` —— 那会让每个任务被自己的门拦下。
    断言查的是**代码**（docstring 里正是在解释这件事，substring 查会假阳性）。
    """
    src = code_without_docstring(staging.StagingArea.verify)
    assert "delivered" not in src
    assert "expected_artifacts" in src  # 唯一一处读期望集合，且只问布尔
    assert " - " not in src.replace("  - ", "")  # 没有集合减法


# ── 18. 三个集合不串味 ─────────────────────────────────────


def test_18_three_collections_have_one_consumer_each(tmp_path, events):
    """`observed` / `present` / `kept` 三层事实彻底分离。

    嵌进容器后被吃掉的字幕仍必须计入 `actual`（`observed`），否则会把「嵌进去了」
    误报成「没拿到」；但它不该被 `build_plan()` 搬。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=("en-GB",), thumb=False)
    area.manifest.embed_evidence.add("subtitle")
    area.manifest.drop(id=ids["sub:en-GB"], reason="embedded_successfully")
    os.remove(os.path.join(area.payload_dir, "Title.en-GB.vtt"))
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    area.commit(plan)

    observed = {a.id for a in area.manifest.observed()}
    assert ids["sub:en-GB"] in observed
    assert ids["sub:en-GB"] not in {a.id for a in area.manifest.kept()}
    assert ids["sub:en-GB"] not in {m.artifact_id for m in plan.members}
    # 取得侧喂的是 payload 内原名（语言段完整、未经整组改名）
    assert any(p.endswith("Title.en-GB.vtt") for p in area.observed_paths())
    assert area.published_paths() == [os.path.join(area.download_dir, "Title.mp4")]


# ── 19. attempt 隔离 ───────────────────────────────────────


def test_19_attempt_scoped_final_file(tmp_path, events):
    """`--print-to-file` 是 **Append** 语义，而自动重试不换 `run_id`。

    单文件会被上一次 attempt 的路径污染，读到的「主媒体」可能是失败那次的。
    失败 attempt 的证据仍留在磁盘上，事后能看出它写出了什么。
    """
    area = make_area(tmp_path)
    first = area.prepare_attempt(0)
    with open(first, "a", encoding="utf-8") as fh:
        fh.write("C:/x/old.mp4\n")
    second = area.prepare_attempt(1)
    with open(second, "a", encoding="utf-8") as fh:
        fh.write("C:/x/new.mp4\n")

    assert area.read_attempt_paths() == ["C:/x/new.mp4"]
    assert area.read_attempt_paths(0) == ["C:/x/old.mp4"]
    assert os.path.isfile(first)
    journal = staging._read_journal(area.txn_dir)
    assert journal["attempt"] == 1
    assert journal["final_path_file"] == os.path.join(staging.CONTROL_NAME, "final.1.txt")


# ── 20. 事务隔离 ───────────────────────────────────────────


def test_20_transaction_scoped_sandbox(tmp_path, events):
    """沙盒是 transaction-scoped 且永不复用。

    `task_<db_id>` 在重下时是同一个目录（`restore_db_id` 明文复用同一个 `tasks.id`），
    旧 run 的残骸会被新 run 的 `reconcile()` 当成自己的产物交付出去。
    """
    dl = make_dl(tmp_path)
    a = make_area(tmp_path, task="42", dl=dl, staging_id="a" * 32)
    write(os.path.join(a.payload_dir, "Old.mp4"), MEDIA_BYTES, b"o")

    b = make_area(tmp_path, task="42", dl=dl, staging_id="b" * 32)
    assert a.txn_dir != b.txn_dir
    seed_group(b, langs=(), thumb=False)
    run_commit(b)

    assert {art.id for art in b.manifest.observed()} == {os.path.normcase("Title.mp4")}
    assert names_in(dl) == {"Title.mp4"}

    stats = gc_orphans(dl, [b.staging_id], older_than_hours=0)
    assert stats["removed"] == 1
    assert not os.path.exists(a.txn_dir)
    assert os.path.isdir(b.txn_dir)


def test_20b_directory_identity_is_staging_id_not_run_id(tmp_path, events):
    """目录身份是全长 `uuid4()`，**不是** 24-bit 的 `trace.run_id`。

    拿只用于展示的 24-bit ID 当 filesystem ownership identity 就得为碰撞准备
    `.2` fallback，而两个目录的 journal 里 `run_id` 相同，GC 反而无法区分谁活着。
    """
    dl = make_dl(tmp_path)
    a = make_area(tmp_path, task="42", dl=dl, staging_id="a" * 32, trace_run_id="dup123")
    b = make_area(tmp_path, task="42", dl=dl, staging_id="b" * 32, trace_run_id="dup123")
    assert a.txn_dir != b.txn_dir
    assert staging._read_journal(a.txn_dir)["run_id"] == staging._read_journal(b.txn_dir)["run_id"]

    gc_orphans(dl, [b.staging_id], older_than_hours=0)

    assert not os.path.exists(a.txn_dir)
    assert os.path.isdir(b.txn_dir)


def test_20c_create_refuses_to_reuse_a_directory(tmp_path, events):
    """`exist_ok=False`：全长 uuid 撞了就是 bug，直接抛，不做 fallback。"""
    dl = make_dl(tmp_path)
    make_area(tmp_path, task="42", dl=dl, staging_id="c" * 32)
    with pytest.raises(FileExistsError):
        make_area(tmp_path, task="42", dl=dl, staging_id="c" * 32)


# ── 21. 不可取消临界区覆盖 reserve ─────────────────────────


def test_21_cancel_after_first_publish_still_completes(tmp_path, events):
    """临界区内取消只记 `pending_cancel`：绝不出现「已取消 + 半组成品」。"""
    dl = make_dl(tmp_path)
    flag = {"cancelled": False}
    area = make_area(tmp_path, dl=dl, cancel_check=lambda: flag["cancelled"])
    seed_group(area, langs=("en",), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()

    real = os.replace

    def fake(src, dst, *a, **kw):
        result = real(src, dst, *a, **kw)
        if str(dst).endswith("Title.en.vtt"):
            flag["cancelled"] = True  # 第 1 个成员刚上岸，用户点了取消
        return result

    staging.os.replace = fake
    try:
        area.commit(plan)
    finally:
        staging.os.replace = real

    assert area.phase == "committed"
    assert area.pending_cancel is True
    assert names_in(dl) == {"Title.mp4", "Title.en.vtt"}  # 完整一组
    assert "pending_cancel" in codes(events)
    assert area.finalize_cancel() == "no_op"


def test_21b_cancel_between_placeholders_still_completes(tmp_path, events):
    """`reserve` 本身就是 external side effect —— 它必须在临界区**内**。

    否则存在「占位符已建、phase 还不是 committing、取消路径认为可以 discard」这条
    竞态，用户目录里会留下一串 0 字节文件。
    """
    dl = make_dl(tmp_path)
    flag = {"cancelled": False}
    area = make_area(tmp_path, dl=dl, cancel_check=lambda: flag["cancelled"])
    seed_group(area, langs=("en",), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()

    real_open = os.open

    def fake_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        if flags & os.O_EXCL:
            flag["cancelled"] = True  # 第 1 个占位符刚建好
        return fd

    staging.os.open = fake_open
    try:
        area.commit(plan)
    finally:
        staging.os.open = real_open

    assert area.phase == "committed"
    assert area.pending_cancel is True
    assert names_in(dl) == {"Title.mp4", "Title.en.vtt"}
    assert not [n for n in names_in(dl) if os.path.getsize(os.path.join(dl, n)) == 0]


def test_21c_cancel_before_gate_never_touches_user_dir(tmp_path, events):
    """gate 之前取消 ⇒ 根本不进 reserve、用户目录零变化、沙盒消失。"""
    dl = make_dl(tmp_path)
    area = make_area(tmp_path, dl=dl, cancel_check=lambda: True)
    seed_group(area, langs=("en",), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()

    with pytest.raises(StagingCancelled):
        area.commit(plan)

    assert names_in(dl) == set()
    assert area.phase == "prepared"  # 没进 committing
    assert area.finalize_cancel() == "cancelled"
    assert not os.path.exists(area.txn_dir)


def test_21d_commit_has_exactly_one_cancel_gate():
    """内部不许有第二个取消检查点 —— 多一处就多一条「半组成品」的路径。"""
    assert attr_call_count(StagingArea.commit, "_check_cancelled") == 1
    assert attr_call_count(StagingArea._reserve_group, "_check_cancelled") == 0
    assert attr_call_count(StagingArea._publish_group, "_check_cancelled") == 0
    assert attr_call_count(StagingArea._reserve_group, "_cancel_check") == 0

    tree = ast.parse(textwrap.dedent(inspect.getsource(StagingArea.commit)))
    body = list(tree.body[0].body)
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    assert ast.unparse(body[0]) == "self._check_cancelled()"  # 且必须是第一句


# ── 22. 失败裁决单点 ───────────────────────────────────────


def test_22_finalize_failure_decides_by_phase(tmp_path, events):
    """判据是 phase，不是异常类型 —— 未知异常也不能在 `committing` 之后毁现场。"""
    cases = {
        "downloading": False,
        "prepared": False,
        "committing": True,
        "rollback_failed": True,
        "committed": True,
    }
    for phase, retained in cases.items():
        area = make_area(tmp_path, task=phase, dl=make_dl(tmp_path, f"p_{phase}"))
        area._phase = phase
        verdict = area.finalize_failure(RuntimeError("unknown"))
        assert verdict == ("already_succeeded" if phase == "committed" else "failed")
        assert os.path.isdir(area.txn_dir) is retained, phase


def test_22b_retain_intent_survives_safe_phases(tmp_path, events):
    """`retain_staging=True` 的异常在 `downloading` / `prepared` 下也保留现场。"""
    for exc in (StagingEscape("x"), VerifyBlocked("y"), CommitFailed("z")):
        area = make_area(tmp_path, task=type(exc).__name__, dl=make_dl(tmp_path, type(exc).__name__))
        assert area.finalize_failure(exc) == "failed"
        assert os.path.isdir(area.txn_dir)
        assert "staging_retained" in codes(events)


def test_22c_commit_wraps_unknown_errors_so_the_scene_survives(tmp_path, events, monkeypatch):
    """裸 `OSError` 必须被包成 `CommitFailed`，否则单点裁决会把现场清掉。

    这是上一版的自相矛盾：`commit()` 声称保留沙盒，而外层通用 handler 立刻
    `discard()` 掉它。
    """
    area = make_area(tmp_path)
    seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    dl = area.download_dir
    patch_replace(monkeypatch, lambda src, dst: dst == os.path.join(dl, "Title.mp4"))

    with pytest.raises(CommitFailed) as caught:
        area.commit(plan)
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.retain_staging is True

    assert area.finalize_failure(caught.value) == "failed"
    assert os.path.isdir(area.txn_dir)


# ── 23. 物理丢失的记录不消失（degraded 的原料）─────────────


def test_23_vanished_artifact_is_observed_but_not_delivered(tmp_path, events):
    """`actual` 是历史事实，所以物理丢失推不出 `missing` —— 需要交付侧那一层。

    这里守住事务层提供的原料：`observed()` 仍含它（取得事实），
    `published_paths()` 不含它（交付事实）。两者的减法（`delivered:*` token）
    是 Step 7 的事。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=("ja",), thumb=False)
    os.remove(os.path.join(area.payload_dir, "Title.ja.vtt"))
    run_commit(area)

    assert ids["sub:ja"] in {a.id for a in area.manifest.observed()}
    assert any(p.endswith("Title.ja.vtt") for p in area.observed_paths())
    assert area.published_paths() == [os.path.join(area.download_dir, "Title.mp4")]
    assert area.manifest.require(ids["sub:ja"]).final_path == ""


# ── 24. 加工走事务层（封面嵌入）─────────────────────────────


def test_24_replace_artifact_content_keeps_identity(tmp_path, events):
    """同一个逻辑 artifact 换内容：`id` 不变、`final_path` 规划不变、旧内容不删。

    `ThumbnailEmbedder` 今天直接 `os.replace(temp, video)` 覆盖主媒体、自己
    `os.remove(temp)`、临时文件还 `mkstemp(dir=video.parent)` 落在 payload 里。
    改成纯 transformer 之后，采纳与替换由这里做。
    """
    area = make_area(tmp_path)
    ids = seed_group(area, langs=(), thumb=False)
    art = area.manifest.require(ids["media"])
    planned_path = art.path

    work = write(area.reserve_workfile("Title", ".mp4"), MEDIA_BYTES, b"n")
    area.replace_artifact_content(ids["media"], work, producer="ThumbnailEmbedder")

    assert art.id == ids["media"] and art.path == planned_path
    with open(art.path, "rb") as fh:
        assert fh.read(1) == b"n"
    backups = os.listdir(area.internal_dir)
    assert len(backups) == 1  # 旧内容移进内部区，没被 os.remove
    with open(os.path.join(area.internal_dir, backups[0]), "rb") as fh:
        assert fh.read(1) == b"x"
    assert art.producer == "ThumbnailEmbedder"


def test_24b_failed_embed_leaves_media_untouched(tmp_path, events, monkeypatch):
    """嵌入失败 ⇒ 主媒体原样不动、workfile 从未进清单。"""
    area = make_area(tmp_path)
    ids = seed_group(area, langs=(), thumb=False)
    art = area.manifest.require(ids["media"])
    work = write(area.reserve_workfile("Title", ".mp4"), 512, b"n")
    patch_replace(monkeypatch, lambda src, dst: src == work)

    with pytest.raises(OSError):
        area.replace_artifact_content(ids["media"], work, producer="ThumbnailEmbedder")

    assert os.path.getsize(art.path) == MEDIA_BYTES
    with open(art.path, "rb") as fh:
        assert fh.read(1) == b"x"
    assert len(area.manifest) == 1


def test_24c_seed_from_gives_in_place_tools_a_copy(tmp_path, events):
    """原地改写型工具（AtomicParsley `--overWrite` / mutagen `save()`）拿到的是副本。"""
    area = make_area(tmp_path)
    ids = seed_group(area, langs=(), thumb=False)
    src = area.manifest.require(ids["media"]).path
    before = os.stat(src)

    work = area.reserve_workfile("Title", ".mp4", seed_from=ids["media"])

    assert os.path.dirname(work) == area.work_dir
    assert os.path.getsize(work) == MEDIA_BYTES
    with open(work, "ab") as fh:  # 工具原地改写副本
        fh.write(b"cover")
    after = os.stat(src)
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_24d_replace_rejects_empty_or_escaping_work(tmp_path, events):
    """空文件 / 沙盒外的候选一律拒绝 —— 否则一次失败的嵌入会清空主媒体。"""
    area = make_area(tmp_path)
    ids = seed_group(area, langs=(), thumb=False)
    empty = write(area.reserve_workfile("Title", ".mp4"), 0)
    with pytest.raises(StagingError):
        area.replace_artifact_content(ids["media"], empty, producer="X")
    outside = write(str(tmp_path / "outside.mp4"), 128)
    with pytest.raises(StagingEscape):
        area.replace_artifact_content(ids["media"], outside, producer="X")
    assert os.path.getsize(area.manifest.require(ids["media"]).path) == MEDIA_BYTES


# ── 25. 提交后异常不得推翻成功 ─────────────────────────────


def test_25_postcommit_exception_returns_already_succeeded(tmp_path, events):
    """`phase=committed` 是不可逆的成功边界。

    全部成员 published 之后用户手上的文件已经完整，此时任何异常都只能是
    housekeeping 出错 —— 一个裸 `except Exception: error.emit(...)` 会制造这轮重构
    最想消灭的另一种谎言的镜像版：**文件明明下好了，任务说失败。**
    """
    area = make_area(tmp_path)
    seed_group(area, langs=("en",), thumb=False)
    run_commit(area)

    verdict = area.finalize_failure(RuntimeError("emit_actual 炸了"))

    assert verdict == "already_succeeded"
    assert "postcommit_exception" in codes(events)
    assert names_in(area.download_dir) == {"Title.mp4", "Title.en.vtt"}
    assert os.path.isdir(area.txn_dir)  # 留给 GC


def test_25b_cleanup_failure_is_recovered_by_gc(tmp_path, events, monkeypatch):
    """`rmtree` 撞上占用 ⇒ 沙盒留着，下次 GC 按 `committed` 直接回收。"""
    area = make_area(tmp_path)
    seed_group(area, langs=(), thumb=False)
    run_commit(area)

    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky(path, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("被资源管理器占用")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(staging.shutil, "rmtree", flaky)
    with pytest.raises(PermissionError):
        area.cleanup()
    assert os.path.isdir(area.txn_dir)
    assert area.phase == "committed"

    monkeypatch.setattr(staging.shutil, "rmtree", real_rmtree)
    stats = gc_orphans(area.download_dir, [], older_than_hours=0)
    assert stats["removed"] == 1
    assert not os.path.exists(area.txn_dir)
    assert names_in(area.download_dir) == {"Title.mp4"}


# ── 26. 占位符不是交付物 ───────────────────────────────────


def test_26_placeholders_cleaned_when_interrupted_before_publish(tmp_path, events, monkeypatch):
    """断点在第一个 publish **之前**：`reserve` 已经改了用户目录，补偿必须清干净。

    与用例 10 的区别就是这个断点 —— 专门守「预留本身就是 external side effect」。
    """
    area = make_area(tmp_path)
    seed_group(area, langs=("en",), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    dl = area.download_dir
    patch_replace(monkeypatch, lambda src, dst: dst.startswith(dl) and not dst.endswith(".tmp"))

    with pytest.raises(CommitFailed):
        area.commit(plan)

    assert names_in(dl) == set()  # 没有 0 字节残留
    assert area.phase == "prepared"
    assert os.path.isfile(os.path.join(area.payload_dir, "Title.mp4"))


def test_26b_journal_retreats_only_after_the_world_is_clean(tmp_path, events, monkeypatch):
    """退回更安全的状态**后**写 journal（write-behind）。

    顺序反了的话，崩在「占位符已清、`prepared` 还没落盘」之间会留下一个自称
    `prepared`（= 沙盒外干净）而实际还挂着占位符的 journal，GC 就会照 `prepared`
    那一行直接 `rmtree`，用户目录里的 0 字节文件永远没人清。
    """
    area = make_area(tmp_path)
    seed_group(area, langs=(), thumb=False)
    area.reconcile({})
    area.seal_discovery()
    area.verify({})
    plan = area.build_plan()
    dl = area.download_dir
    seen: list[str] = []

    real_remove = os.remove

    def watch_remove(path, *a, **kw):
        seen.append(str((staging._read_journal(area.txn_dir) or {}).get("phase")))
        return real_remove(path, *a, **kw)

    patch_replace(monkeypatch, lambda src, dst: dst == os.path.join(dl, "Title.mp4"))
    monkeypatch.setattr(staging.os, "remove", watch_remove)

    with pytest.raises(CommitFailed):
        area.commit(plan)

    assert seen and set(seen) == {"committing"}
    assert staging._read_journal(area.txn_dir)["phase"] == "prepared"
