"""Behavioral regressions from the September logging review."""

import json
import os
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fluentytdl.observability import bundle, sinks
from fluentytdl.utils import log_runtime
from fluentytdl.utils.log_privacy import redact_text
from fluentytdl.utils.log_reader import read_page


@pytest.fixture
def isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(log_runtime, "_root", tmp_path)
    monkeypatch.setattr(sinks, "_TRACE_DIR", tmp_path / "traces")
    (tmp_path / "traces").mkdir()
    original = sinks._open_files
    monkeypatch.setattr(sinks, "_open_files", type(original)())
    yield tmp_path
    for path, (handle, _) in sinks._open_files.items():
        handle.close()
        log_runtime.mark_active(path, False)


def message(event, *, text="line", identity="id"):
    class Message(str):
        pass

    msg = Message(text)
    msg.record = {
        "extra": {"fytdl": event, "record_id": identity, "origin": "test"},
        "time": datetime.now(UTC),
        "level": SimpleNamespace(name="ERROR"),
    }
    return msg


def test_session_only_event_is_persisted(isolated_logs):
    sinks.jsonl_sink(message({"kind": "config", "session": "s", "flow": "-", "task": "-"}))
    event = json.loads((isolated_logs / "traces/session-s.jsonl").read_text())
    assert event["kind"] == "config" and event["_event_id"] == "id"
    assert event["_origin"] == "test"


def test_rotation_same_instant_preserves_all_ids_and_exact_size(isolated_logs, monkeypatch):
    monkeypatch.setattr(sinks, "_MAX_JSONL_BYTES", 600)
    monkeypatch.setattr(sinks.time, "time_ns", lambda: 42)
    for i in range(12):
        sinks.jsonl_sink(
            message({"kind": "signal", "flow": "f", "task": "1", "session": "s"}, identity=str(i))
        )
        for path, (_, count) in sinks._open_files.items():
            assert count == path.stat().st_size
            assert count <= 600
    events = [
        json.loads(line)
        for p in (isolated_logs / "traces").glob("*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert sorted(e["_event_id"] for e in events) == sorted(map(str, range(12)))
    assert len(list((isolated_logs / "traces").glob("*.jsonl"))) >= 3


def test_final_exception_text_and_json_metadata_are_redacted(tmp_path):
    payload = "https://alice:password@example.test/x?sig=FAKESIGN&v=123 token=FAKETOKEN"
    safe = log_runtime.SafeFileSink(tmp_path, "app")
    safe.write(message({}, text="Traceback\nValueError: " + payload))
    safe.stop()
    body = next(tmp_path.glob("*.log")).read_text()
    assert "FAKESIGN" not in body and "FAKETOKEN" not in body and "alice" not in body
    assert "ValueError" in body and "v=123" in body
    metadata = log_runtime.SafeFileSink(tmp_path, "display", jsonl=True)
    metadata.write(message({}, text=json.dumps({"raw": payload, "token": "SECRET"})))
    metadata.stop()
    saved = json.loads(next(tmp_path.glob("*.jsonl")).read_text())
    assert saved["token"] == "***" and "FAKETOKEN" not in saved["raw"]


def test_trace_and_raw_apply_same_redaction(isolated_logs):
    secret = "Authorization: Bearer SYNTHETIC_SECRET"
    sinks.jsonl_sink(message({"flow": "f", "raw_line": secret}))
    path = sinks.write_raw_dump("f", "1", "r", [secret])
    assert "SYNTHETIC_SECRET" not in path.read_text()
    assert "SYNTHETIC_SECRET" not in (isolated_logs / "traces/flow-f.jsonl").read_text()


def test_sink_partial_install_can_retry_without_duplicate(isolated_logs, monkeypatch):
    monkeypatch.setattr(sinks, "_sink_ids", {})
    monkeypatch.setattr(sinks, "start_maintenance", lambda: None)
    calls = []

    def add(sink, **kwargs):
        calls.append(sink)
        if len(calls) == 2:
            raise OSError("blocked")
        return len(calls)

    monkeypatch.setattr(sinks.logger, "add", add)
    sinks.install_sinks()
    assert not sinks._sinks_installed
    sinks.install_sinks()
    assert sinks._sinks_installed and len(calls) == 3
    sinks.install_sinks()
    assert len(calls) == 3


def test_retention_preserves_active_unknown_and_bundle_files(tmp_path):
    active = tmp_path / "app_active.log"
    expired = tmp_path / "app_old.log"
    unknown = tmp_path / "my_notes.txt"
    bundle_dir = tmp_path / "bundles"
    bundle_dir.mkdir()
    exported = bundle_dir / "user.zip"
    for p in (active, expired, unknown, exported):
        p.write_text("data")
        os.utime(p, (0, 0))
    log_runtime.mark_active(active, True)
    try:
        log_runtime.sweep_logs(tmp_path)
    finally:
        log_runtime.mark_active(active, False)
    assert not expired.exists()
    assert all(p.exists() for p in (active, unknown, exported))


def test_typed_filesystem_failure_has_action_and_error_numbers():
    import errno

    from fluentytdl.utils.translator import translate_error

    for exc, code in [
        (FileNotFoundError(errno.ENOENT, "missing", "E:/x"), "file_missing"),
        (PermissionError(errno.EACCES, "denied"), "permission_denied"),
        (OSError(errno.ENOSPC, "full"), "disk_full"),
    ]:
        result = translate_error(exc)
        assert result["code"] == code
        assert result["errno"] == exc.errno
        assert result["fix_action"] == "change_download_dir"


def test_sealed_run_ignores_late_updates_but_new_run_can_start():
    from fluentytdl.download.download_manager import _emit_transition
    from fluentytdl.observability import TaskTrace

    worker = SimpleNamespace(trace=TaskTrace(task_id="1"))
    assert _emit_transition(worker, "completed", 100)
    assert not _emit_transition(worker, "processing", 99)
    assert worker._last_transition_state == "completed"
    worker.trace.new_run()
    assert _emit_transition(worker, "parsing", 0)


def _write_trace(root, filename, events):
    (root / "traces" / filename).write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )


def test_bundle_uses_original_session_and_date_and_manifest(isolated_logs):
    root = isolated_logs
    stamp = datetime(2026, 9, 1, 12).timestamp()
    _write_trace(
        root,
        "flow-f.jsonl",
        [
            {
                "kind": "identity",
                "flow": "f",
                "task": "7",
                "session": "old",
                "run": "r",
                "_ts": stamp,
            },
            {
                "kind": "outcome",
                "flow": "f",
                "task": "7",
                "session": "old",
                "run": "r",
                "_ts": stamp + 1,
                "outcome": "success",
            },
        ],
    )
    _write_trace(
        root,
        "session-old.jsonl",
        [
            {
                "kind": "config",
                "scope": "runtime",
                "session": "old",
                "app_version": "old-build",
                "_ts": stamp - 10,
            }
        ],
    )
    (root / "app_2026-09-01.log").write_text(
        "2026-09-01 12:00:00.000 | INFO     | app:f:1 - original session\n"
    )
    (root / "app_2026-09-12.log").write_text("unrelated current log")
    path = bundle.export_bug_bundle("7")
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["source_runtime"][0]["app_version"] == "old-build"
        assert manifest["export_runtime"]["session"] != "old"
        assert not manifest["partial"]
        assert not any("09-12" in name for name in archive.namelist())
        for entry in manifest["files"]:
            import hashlib

            assert hashlib.sha256(archive.read(entry["file"])).hexdigest() == entry["sha256"]


def test_bundle_exports_parse_failure_without_task_and_reports_missing(isolated_logs):
    _write_trace(
        isolated_logs,
        "flow-parse.jsonl",
        [
            {
                "kind": "diagnosis",
                "flow": "parse",
                "task": "-",
                "session": "missing",
                "_ts": time.time(),
            }
        ],
    )
    path = bundle.export_bug_bundle(flow_id="parse")
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["partial"]
        assert any(x["reason"] == "source_runtime_unavailable" for x in manifest["missing"])
        assert "traces/flow-parse.jsonl" in archive.namelist()


def test_history_cross_day_same_second_and_pagination(tmp_path):
    rows = [
        {"id": str(i), "raw": "same", "level": "INFO", "_ts": 100 + i, "session": "s"}
        for i in range(8)
    ]
    (tmp_path / "display_2026-09-01.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    first = read_page(tmp_path, limit=3)
    second = read_page(tmp_path, limit=3, before=first["before"])
    assert [r["id"] for r in first["records"]] == ["5", "6", "7"]
    assert [r["id"] for r in second["records"]] == ["2", "3", "4"]
    assert read_page(tmp_path, session="other")["records"] == []


def test_no_secret_in_header_query_or_exception():
    for text in ("Cookie: SID=FAKE; HSID=FAKE2", "token=FAKE", "https://a:b@host/?signature=FAKE"):
        assert "FAKE" not in redact_text(text)


def test_pytest_bootstrap_keeps_markers_out_of_application_logs():
    import uuid

    from fluentytdl.utils.logger import LOG_DIR, logger

    original = Path(__file__).resolve().parents[1] / "logs"
    assert Path(LOG_DIR).resolve() != original.resolve()
    assert log_runtime.ORIGIN == "test"
    marker = "logging-isolation-" + uuid.uuid4().hex
    logger.info(marker)
    logger.complete()
    assert any(marker in p.read_text(encoding="utf-8") for p in Path(LOG_DIR).glob("app_*.log"))
    assert not any(marker in p.read_text(encoding="utf-8") for p in original.glob("app_*.log"))


def test_successful_subtitle_warning_does_not_arbitrate_failure(monkeypatch):
    from fluentytdl.download import workers

    payloads = []
    worker = SimpleNamespace(task_warning=SimpleNamespace(emit=payloads.append))
    monkeypatch.setattr(
        workers, "diagnose", lambda *a, **kw: pytest.fail("success called diagnose")
    )
    workers.DownloadWorker._scan_subtitle_warnings(
        worker, ["[info] There are no subtitles for the requested languages"]
    )
    assert payloads and payloads[0]["severity"] == "warning"
    assert payloads[0]["exit_code"] == 0
