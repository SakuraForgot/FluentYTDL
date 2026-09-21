"""Real readers, read-only guarantees and request/lifecycle regression coverage."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fluentytdl.models.media_inspection import InspectionResult
from fluentytdl.processing.media_inspection_process import (
    InspectionCancelled,
    InspectionError,
    run_reader,
)
from fluentytdl.processing.media_inspector import (
    build_result,
    export_report,
    inspect_media,
    native_command,
)

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("FLUENTYTDL_TEST_COMPONENTS", ROOT / "assets")) / "bin" / "ffmpeg"


@pytest.fixture
def media_tools():
    if not (BIN / "ffmpeg.exe").is_file() or not (BIN / "ffprobe.exe").is_file():
        if os.environ.get("FLUENTYTDL_REQUIRE_BUILD_TESTS") == "1":
            pytest.fail("Required media inspection integration tools missing")
        pytest.skip("FFmpeg / FFprobe unavailable")
    return BIN


@pytest.mark.parametrize(
    "extension,codec",
    [
        ("mp3", "libmp3lame"),
        ("flac", "flac"),
        ("opus", "libopus"),
        ("ogg", "libvorbis"),
        ("m4a", "aac"),
        ("webm", "libopus"),
        ("mkv", "flac"),
        ("wav", "pcm_s16le"),
    ],
)
def test_real_audio_inspection_preserves_source(media_tools, tmp_path, extension, codec):
    path = tmp_path / f"中文 test.{extension}"
    subprocess.run(
        [
            str(media_tools / "ffmpeg.exe"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            "-c:a",
            codec,
            "-metadata",
            "title=标题",
            "-metadata",
            "artist=作者",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    before = (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns)
    result = inspect_media(str(path), lambda: False, configured=str(media_tools))
    assert result.status == "ready", result.issues
    assert result.streams[0]["codec_type"] == "audio"
    assert any(f["key"] == "title" and "标题" in f["value"] for f in result.fields)
    assert any(f["key"] == "artist" and "作者" in f["value"] for f in result.fields)
    if extension in {"mp3", "flac", "opus", "ogg", "m4a"}:
        assert result.coverage["native"] == "ready"
        assert any(t["reader"] == "native" for t in result.tags)
    assert before == (hashlib.sha256(path.read_bytes()).digest(), path.stat().st_mtime_ns)


def test_video_only_mp4_native_multi_values(media_tools, tmp_path):
    from mutagen.mp4 import MP4Tags
    from mutagen.mp4._atom import Atoms

    path = tmp_path / "video.mp4"
    subprocess.run(
        [
            str(media_tools / "ffmpeg.exe"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=30000/1001:duration=0.25",
            "-an",
            "-c:v",
            "libx264",
            "-metadata",
            "title=Video",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    with path.open("rb") as stream:
        tags = MP4Tags(Atoms(stream), stream)
    tags["©ART"] = ["甲", "乙"]
    tags["----:com.fluentytdl:authors_json"] = [b'["one", "two"]']
    tags.save(path)
    result = inspect_media(str(path), lambda: False, configured=str(media_tools))
    assert result.status == "ready", result.issues
    assert not any(s.get("codec_type") == "audio" for s in result.streams)
    assert result.streams[0]["average_rate"]["fraction"] == "30000/1001"
    assert any(f["key"] == "artist" and f["value"] == ["甲", "乙"] for f in result.fields)
    assert any(f["key"] == "authors_json" and f["value"] == ["one", "two"] for f in result.fields)


def test_unknown_webm_values_do_not_borrow_other_tracks():
    result = build_result(
        "sample.webm",
        {"size": 1000},
        {
            "format": {"format_name": "matroska,webm", "duration": "2"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "avg_frame_rate": "0/0",
                    "r_frame_rate": "30/1",
                },
                {"index": 1, "codec_type": "audio", "bit_rate": "64000"},
            ],
        },
    )
    assert result.fields[3]["value"] == 4000
    assert result.fields[3]["source"] == "derived"
    video = result.streams[0]
    assert video["average_rate"] is None
    assert video["reference_rate"]["fps"] == 30
    assert video["reported_bitrate"] is None and video["reported_frames"] is None
    assert video["frame_rate_mode"] == "not_analyzed"


def test_helper_reads_id3_multi_values_and_binary_summary(tmp_path):
    from mutagen.id3 import APIC, ID3, TIT2, TPE1

    path = tmp_path / "tags.id3"
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["标题"]))
    tags.add(TPE1(encoding=3, text=["作者一", "作者二"]))
    tags.add(APIC(encoding=3, mime="image/png", type=3, data=b"binary-picture"))
    tags.save(path)
    result = json.loads(
        run_reader(
            native_command(),
            lambda: False,
            request=json.dumps({"path": str(path), "kind": "mp3"}).encode(),
        )
    )
    assert result["status"] == "ready"
    assert next(t for t in result["tags"] if t["key"] == "TPE1")["values"] == ["作者一", "作者二"]
    assert "binary-picture" not in json.dumps(result)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows inherited pipe handles")
def test_windowed_helper_without_python_standard_streams(tmp_path):
    from mutagen.id3 import ID3, TIT2

    path = tmp_path / "windowed.id3"
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Windowed helper"]))
    tags.save(path)
    code = "import sys; sys.stdin = sys.stdout = None; from fluentytdl.processing.media_tags_worker import main; raise SystemExit(main())"
    result = json.loads(
        run_reader(
            [sys.executable, "-c", code],
            lambda: False,
            request=json.dumps({"path": str(path), "kind": "mp3"}).encode(),
        )
    )
    assert result["status"] == "ready"
    assert result["tags"][0]["values"] == ["Windowed helper"]


@pytest.mark.parametrize(
    "path,expected",
    [
        ("", "path_missing"),
        ("https://example.com/a.mp4", "local_file_only"),
        ("//server/share/a.mp4", "local_file_only"),
    ],
)
def test_reject_invalid_input_before_starting_reader(path, expected, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Reader must not start for invalid input")

    monkeypatch.setattr("fluentytdl.processing.media_inspector.run_reader", unexpected)
    with pytest.raises(InspectionError, match=expected):
        inspect_media(path, lambda: False)


def test_file_removed_during_native_read_preserves_partial_result(media_tools, tmp_path):
    path = tmp_path / "a.mp3"
    subprocess.run(
        [
            str(media_tools / "ffmpeg.exe"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=0.1",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    result = inspect_media(
        str(path), lambda: False, configured=str(media_tools), partial=lambda result: path.unlink()
    )
    assert result.status == "changed"
    assert "file_changed" in result.issues
    assert result.streams[0]["codec_type"] == "audio"


@pytest.mark.parametrize("scenario", ["cancel", "timeout", "stdout", "stderr"])
def test_bounded_child_execution(scenario):
    cancel = threading.Event()
    code = "import time; time.sleep(30)"
    if scenario in {"stdout", "stderr"}:
        code = f"import sys; sys.{scenario}.write('X' * 1000000); sys.{scenario}.flush()"
    timer = threading.Timer(0.15, cancel.set) if scenario == "cancel" else None
    if timer:
        timer.start()
    start = time.monotonic()
    error = InspectionCancelled if scenario == "cancel" else InspectionError
    with pytest.raises(error):
        run_reader(
            [sys.executable, "-c", code],
            cancel.is_set,
            timeout=0.3 if scenario == "timeout" else 5,
            limit=1024,
        )
    assert time.monotonic() - start < 4
    if timer:
        timer.join()


def test_export_defaults_and_same_file_protection(tmp_path):
    source = tmp_path / "original.mp3"
    source.write_bytes(b"original media")
    result = InspectionResult(str(source), raw={"format": {"filename": str(source)}})
    report = tmp_path / "report.json"
    export_report(result, str(report))
    data = json.loads(report.read_text("utf-8"))
    assert "path" not in data and "raw" not in data and "tags" not in data
    export_report(result, str(report), include_raw=True)
    assert str(source) not in report.read_text("utf-8")
    alias = tmp_path / "alias.json"
    os.link(source, alias)
    for target in (source, alias):
        with pytest.raises(InspectionError, match="export_is_source"):
            export_report(result, str(target))
    assert source.read_bytes() == b"original media"


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def pump_until(qapp, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert predicate()


def test_latest_request_wins_and_shutdown(qapp, monkeypatch):
    from fluentytdl.core import media_inspection_service as module

    entered = threading.Event()
    calls = []

    def fake(path, cancel, **kwargs):
        calls.append(path)
        if path == "A":
            entered.set()
            while not cancel():
                time.sleep(0.005)
        # Deliberately emit a late result despite cancellation.
        return InspectionResult(path)

    monkeypatch.setattr(module, "inspect_media", fake)
    service = module.MediaInspectionService()
    results = []
    service.result.connect(lambda result, final: results.append(result.path))
    service.inspect("A")
    pump_until(qapp, entered.is_set)
    service.inspect("B")
    service.inspect("C")
    pump_until(qapp, lambda: not service.busy)
    assert calls == ["A", "C"]
    assert results == ["C"]
    entered.clear()
    service.inspect("A")
    pump_until(qapp, entered.is_set)
    service.shutdown()
    pump_until(qapp, lambda: not service.busy)
    assert results == ["C"]


def test_page_navigation_missing_values_and_export_options(qapp):
    from fluentytdl.ui.media_info_page import MediaInfoPage

    page = MediaInfoPage()
    requested = []
    page.inspect_requested.connect(requested.append)
    page.open_file("a.webm", task_title="已完成任务")
    assert requested == ["a.webm"]
    assert not page.back_button.isHidden()
    result = build_result(
        "a.webm",
        {"size": 12},
        {
            "format": {"format_name": "webm"},
            "streams": [
                {"index": 0, "codec_type": "video"},
                {"index": 1, "codec_type": "audio"},
                {"index": 2, "codec_type": "audio"},
            ],
        },
    )
    page.show_result(result, True)
    assert page.mode_combo.currentIndex() == 0
    assert [
        c.key for c in page.card_data if c.key.startswith("audio")
    ] == []  # No meaningful audio values, no empty cards.
    assert not page.status.isVisible()
    assert not hasattr(page, "table")
    assert not hasattr(page, "include_path")
    page.show_error("cancelled")
    assert page.result.report()["status"] == "cancelled"
    assert "cancelled" in page.result.report()["issues"]
    page.open_file("b.mp3")
    assert page.result is None and page.back_button.isHidden()
    assert not page.export_action.isEnabled()
    page.close()


def test_cards_reflow_without_table_or_export_controls(qapp):
    from PySide6.QtCore import QRect
    from qfluentwidgets import CheckBox, TableView

    from fluentytdl.ui.media_info_page import MediaExportDialog, MediaInfoPage

    result = build_result(
        "C:/Videos/test.mp4",
        {"size": 123456},
        {
            "format": {
                "format_name": "mov,mp4",
                "duration": "70",
                "tags": {"title": "Example", "artist": "Author"},
            },
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "codec_name": "h264",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        },
    )
    page = MediaInfoPage()
    page.resize(1000, 760)
    page.show()
    page.open_file(result.path, task_title="Example")
    page.show_result(result, True)
    assert [c.key for c in page.card_data] == ["video:0", "audio:1"]
    requested = []
    page.inspect_requested.connect(requested.append)
    page.mode_combo.setCurrentIndex(1)
    assert {c.key for c in page.card_data} >= {"content", "file", "video:0", "audio:1"}
    assert page.result is result and requested == []
    page.mode_combo.setCurrentIndex(0)
    assert [c.key for c in page.card_data] == ["video:0", "audio:1"]
    for width, columns in ((1000, 2), (650, 1), (1000, 2)):
        page.resize(width, 760)
        for _ in range(10):
            qapp.processEvents()
        assert page._columns == columns
        rectangles = [
            QRect(card.mapTo(page.canvas, card.rect().topLeft()), card.size())
            for card in page.cards
        ]
        assert all(rect.right() <= page.canvas.width() for rect in rectangles)
        assert all(
            not a.intersects(b) for i, a in enumerate(rectangles) for b in rectangles[i + 1 :]
        )
    assert not page.findChildren(TableView)
    assert not page.findChildren(CheckBox)
    assert page.status.isHidden() and page.cancel_button.isHidden()
    page.copy_summary()
    assert "1920" in qapp.clipboard().text()
    assert "C:\\Videos" not in qapp.clipboard().text()
    dialog = MediaExportDialog(page)
    assert dialog.options() == {"include_path": False, "include_raw": False}
    dialog.deleteLater()
    page.close()
