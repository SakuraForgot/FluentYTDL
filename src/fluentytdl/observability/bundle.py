"""Task/flow scoped, bounded and explicitly partial diagnostic exports."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from ..utils.log_privacy import POLICY_VERSION, redact_text, redact_value
from ..utils.log_runtime import SESSION_ID, get_log_root, health_snapshot, record_failure
from .sinks import flush_sinks, get_trace_dir

_MAX_APP_LOG_BYTES = 2 * 1024 * 1024
_MAX_BUNDLE_BYTES = 30 * 1024 * 1024
_CRITICAL_KINDS = {"diagnosis", "outcome", "identity", "expect", "actual", "config"}


def _valid_id(value):
    text = str(value or "")
    if text and text != "-" and not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise ValueError("Invalid diagnostic identity")
    return text if text != "-" else ""


def _events(path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    yield event
            except ValueError:
                continue


def resolve_flows_for_task(task_id):
    target = str(task_id)
    found = set()
    for path in get_trace_dir().glob("*.jsonl"):
        try:
            for event in _events(path):
                if str(event.get("task")) == target and event.get("flow") not in (None, "", "-"):
                    found.add(_valid_id(event["flow"]))
        except OSError as exc:
            record_failure("bundle_scan", exc)
    return found


def resolve_flow_for_task(task_id):
    return next(iter(sorted(resolve_flows_for_task(task_id))), "")


def _meta(task_id, flow_id):
    from .. import __version__

    return {
        "app_version": __version__,
        "session": SESSION_ID,
        "task": str(task_id),
        "flow": flow_id or None,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }


def _text_lines(path):
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    with archive.open(info) as raw:
                        yield from io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
    else:
        with path.open(encoding="utf-8", errors="replace") as stream:
            yield from stream


def export_bug_bundle(
    task_id=None, dest=None, *, flow_id=None, run_id=None, session_id=None, progress=None
):
    """Export selected evidence. manifest.json distinguishes missing/truncated files."""
    temporary = None
    try:
        task, flow = _valid_id(task_id), _valid_id(flow_id)
        run, selected_session = _valid_id(run_id), _valid_id(session_id)
        if not task and not flow:
            raise ValueError("Select a task or flow")
        flush_sinks()
        cutoff = time.time()
        flows = resolve_flows_for_task(task) if task and not run else set()
        if flow:
            flows.add(flow)
        root, trace_dir = get_log_root(), get_trace_dir()
        manifest = {
            "schema_version": 1,
            "selection": {
                "task": task,
                "flows": sorted(flows),
                "run": run,
                "session": selected_session,
            },
            "cutoff": cutoff,
            "privacy_version": POLICY_VERSION,
            "files": [],
            "missing": [],
            "source_runtime": [],
            "export_runtime": _meta(task, flow),
            "health": health_snapshot(),
        }
        label = "task" + task if task else "flow" + flow
        output = Path(dest) if dest else root / "bundles"
        if output.suffix.lower() != ".zip":
            output = output / f"fluentytdl-bug-{label}.zip"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
        remaining = max(0, _MAX_BUNDLE_BYTES - 128 * 1024)
        sessions, dates, runs = set(), set(), set()
        windows = {}

        def belongs(event):
            if selected_session and event.get("session") != selected_session:
                return False
            if run and event.get("run") not in (None, "", "-", run):
                return False
            return not task or str(event.get("task") or "-") in (task, "-")

        def missing(name, reason):
            manifest["missing"].append({"file": name, "reason": redact_text(reason, 300)})

        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as zf:

            def write(name, data, *, source_size=None, truncated=False):
                nonlocal remaining
                if len(data) > remaining:
                    missing(name, "bundle_budget_exceeded")
                    return
                zf.writestr(name, data)
                remaining -= len(data)
                manifest["files"].append(
                    {
                        "file": name,
                        "size": len(data),
                        "source_size": source_size,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "truncated": truncated,
                    }
                )

                if progress:
                    try:
                        progress({"file": name, "files": len(manifest["files"])})
                    except Exception:
                        pass

            candidates = set()
            for item in flows:
                matches = list(trace_dir.glob(f"flow-{item}*.jsonl"))
                matches = [
                    p
                    for p in matches
                    if p.name == f"flow-{item}.jsonl" or p.name.startswith(f"flow-{item}.")
                ]
                candidates.update(matches)
                if not matches:
                    missing(f"flow-{item}.jsonl", "not_found")
            if task:
                candidates.update(trace_dir.glob(f"task-{task}.jsonl"))
                candidates.update(trace_dir.glob(f"task-{task}.*.jsonl"))
            if not candidates:
                missing("trace", "no_matching_trace")
            for path in sorted(candidates):
                try:
                    critical, ordinary = [], []
                    used = 0
                    truncated = False
                    for event in _events(path):
                        if not belongs(event) or float(event.get("_ts") or 0) > cutoff:
                            continue
                        session = event.get("session")
                        if session and session != "-":
                            sessions.add(_valid_id(session))
                        if event.get("run") not in (None, "", "-"):
                            runs.add(str(event["run"]))
                        if event.get("_ts"):
                            stamp = float(event["_ts"])
                            day = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
                            dates.add(day)
                            low, high = windows.get(day, (stamp, stamp))
                            windows[day] = (min(low, stamp), max(high, stamp))
                        data = (json.dumps(redact_value(event), ensure_ascii=False) + "\n").encode(
                            "utf-8"
                        )
                        allowance = min(remaining, 10 * 1024 * 1024)
                        if event.get("kind") not in _CRITICAL_KINDS:
                            allowance = max(0, allowance - 1024 * 1024)
                        if used + len(data) > allowance:
                            truncated = True
                            continue
                        (critical if event.get("kind") in _CRITICAL_KINDS else ordinary).append(
                            (float(event.get("_ts") or 0), data)
                        )
                        used += len(data)
                    write(
                        "traces/" + path.name,
                        b"".join(row[1] for row in sorted(critical + ordinary, key=lambda x: x[0])),
                        source_size=path.stat().st_size,
                        truncated=truncated,
                    )
                except Exception as exc:
                    missing(path.name, exc)
            for session in sorted(sessions):
                paths = list(trace_dir.glob(f"session-{session}.jsonl")) + list(
                    trace_dir.glob(f"session-{session}.*.jsonl")
                )
                if not paths:
                    missing(f"session-{session}.jsonl", "source_runtime_unavailable")
                for path in paths:
                    try:
                        data = bytearray()
                        truncated = False
                        for event in _events(path):
                            if float(event.get("_ts") or 0) > cutoff:
                                continue
                            if event.get("kind") == "config" and event.get("scope") in (
                                None,
                                "runtime",
                                "snapshot",
                                "toolchain",
                            ):
                                if len(manifest["source_runtime"]) < 64:
                                    manifest["source_runtime"].append(redact_value(event))
                                else:
                                    truncated = True
                            row = (
                                json.dumps(redact_value(event), ensure_ascii=False) + "\n"
                            ).encode("utf-8")
                            if len(data) + len(row) > min(remaining, _MAX_APP_LOG_BYTES):
                                truncated = True
                                break
                            data.extend(row)
                        write(
                            "traces/" + path.name,
                            bytes(data),
                            source_size=path.stat().st_size,
                            truncated=truncated,
                        )
                    except Exception as exc:
                        missing(path.name, exc)
            for sub in sorted(flows | {"no-flow"}):
                folder = trace_dir / sub
                if not folder.is_dir():
                    continue
                for path in folder.glob(f"task-{task or '*'}-run-*.ytdlp.log"):
                    if run and f"-run-{run}." not in path.name:
                        continue
                    try:
                        with path.open("rb") as f:
                            data = f.read(min(remaining, _MAX_APP_LOG_BYTES) + 1)
                        truncated = len(data) > min(remaining, _MAX_APP_LOG_BYTES)
                        data = redact_text(
                            data[: min(remaining, _MAX_APP_LOG_BYTES)].decode("utf-8", "replace")
                        ).encode("utf-8")
                        write(
                            "raw/" + sub + "/" + path.name,
                            data,
                            source_size=path.stat().st_size,
                            truncated=truncated,
                        )
                    except Exception as exc:
                        missing(path.name, exc)
            for day in sorted(dates):
                paths = list(root.glob(f"app_{day}*.log")) + list(root.glob(f"app_{day}*.log.zip"))
                paths += list(root.glob("errors_sync*.log")) + list(
                    root.glob("errors_sync*.log.zip")
                )
                if not paths:
                    missing("app_" + day, "not_found")
                for path in paths:
                    try:
                        data = bytearray()
                        truncated = False
                        include = False
                        low, high = windows[day]
                        for line in _text_lines(path):
                            match = re.match(r"^(\d{4}-\d\d-\d\d [\d:.]+) \|", line)
                            if match:
                                stamp = datetime.fromisoformat(match[1]).timestamp()
                                include = low - 5 <= stamp <= min(high + 5, cutoff)
                            if not include:
                                continue
                            row = redact_text(line).encode("utf-8")
                            if len(data) + len(row) > min(remaining, _MAX_APP_LOG_BYTES):
                                truncated = True
                                break
                            data.extend(row)
                        write(
                            "logs/" + day + "/" + path.name.removesuffix(".zip"),
                            bytes(data),
                            source_size=path.stat().st_size,
                            truncated=truncated,
                        )
                    except Exception as exc:
                        missing(path.name, exc)
            if not manifest["source_runtime"]:
                missing("source_runtime", "snapshot_unavailable")
            manifest["partial"] = bool(
                manifest["missing"] or any(f["truncated"] for f in manifest["files"])
            )
            zf.writestr("meta.json", json.dumps(manifest["export_runtime"], ensure_ascii=False))
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(temporary, output)
        return output
    except Exception as exc:
        record_failure("bundle_export", exc)
        return None
    finally:
        if temporary and temporary.exists():
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                record_failure("bundle_cleanup", exc)
