"""English diagnostic records with language-neutral display metadata."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import uuid
from functools import lru_cache
from pathlib import Path

from .message_catalog import english


def _safe(value):
    from .log_privacy import redact_value

    if isinstance(value, BaseException) and len(value.args) == 1:
        return _safe(value.args[0])
    if hasattr(value, "message_source"):
        return {
            "message_id": hashlib.sha256(value.message_source.encode()).hexdigest()[:20],
            "args": [_safe(x) for x in value.message_args],
            "kwargs": _safe(value.message_kwargs),
        }
    return redact_value(value)


def log_text(target, level: str, source: str, *args, **kwargs) -> None:
    """Retain caller module/exception options; failures must not affect work."""
    try:
        params = [_safe(arg) for arg in args]
        named = _safe(kwargs)
        raw = english(
            source,
            *[_render_param(p, False) for p in params],
            **{k: _render_param(v, False) for k, v in named.items()},
        )
        payload = {
            "id": uuid.uuid4().hex,
            "message_id": hashlib.sha256(source.encode()).hexdigest()[:20],
            "args": params,
            "kwargs": named,
            "raw": raw,
        }
        if isinstance(target, logging.Logger):
            if args and "%" in source and "{" not in source:
                raw = english(source) % tuple(params)
                payload["raw"] = raw
            target.log(
                logging.ERROR
                if level == "exception"
                else getattr(logging, level.upper(), logging.INFO),
                raw,
                exc_info=level == "exception",
                stacklevel=2,
                extra={"localized": payload},
            )
        else:
            caller = sys._getframe(1)
            module = caller.f_globals.get("__name__", "")
            function, line = caller.f_code.co_name, caller.f_lineno
            filename = caller.f_code.co_filename

            def set_caller(record):
                record.update(name=module, function=function, line=line)
                record["file"] = type(record["file"])(Path(filename).name, filename)

            # patch/bind preserve existing opt(exception=...) settings; opt(depth=...)
            # would reset those options and silently lose the original traceback.
            bound = target.bind(localized=payload).patch(set_caller)
            getattr(bound, level)(raw)
    except Exception as exc:
        from .log_runtime import record_failure

        record_failure("text_emit", exc)
        return


def metadata_filter(record: dict) -> bool:
    """Attach serialized metadata for a dedicated sink; leave message intact."""
    try:
        payload = dict(record["extra"].get("localized") or {})
        payload.update(
            time=record["time"].isoformat(),
            level=record["level"].name,
            module=record["name"] or "",
            raw=record["message"],
        )
        payload["id"] = record["extra"].get("record_id") or payload.get("id") or uuid.uuid4().hex
        for key in ("session", "origin", "process"):
            payload[key] = record["extra"].get(key)
        payload["_ts"] = record["time"].timestamp()
        payload["event"] = record["extra"].get("fytdl")
        if record.get("exception"):
            import traceback

            from .log_privacy import redact_text

            payload["exception"] = redact_text(
                "".join(traceback.format_exception(*record["exception"])), 16000
            )
        record["extra"]["display_record"] = payload
        record["extra"]["display_json"] = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        return True
    except Exception as exc:
        from .log_runtime import record_failure

        record_failure("metadata_filter", exc)
        return False


def install_metadata_sink(target, directory: str) -> int | None:
    """A failed optional sidecar must never prevent the main logger from starting."""
    from .log_runtime import SafeFileSink, record_failure

    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        return target.add(
            SafeFileSink(Path(directory), "display", jsonl=True),
            filter=metadata_filter,
            # A callable avoids loguru appending exception text to JSONL records.
            format=lambda record: "{extra[display_json]}\n",
            level="DEBUG",
            enqueue=True,
            backtrace=False,
            diagnose=False,
            catch=True,
        )
    except (OSError, ValueError) as exc:
        record_failure("metadata_install", exc)
        return None


def render_record(record: dict) -> str:
    from .message_catalog import catalog
    from .ui_text import tr_text

    try:
        message_id = record.get("message_id")
        if message_id:
            source = _sources_by_id().get(message_id)
            if source is not None and source in catalog():
                return tr_text(
                    source,
                    *[_render_param(p, True) for p in record.get("args", [])],
                    **{k: _render_param(v, True) for k, v in record.get("kwargs", {}).items()},
                )
    except Exception:
        pass
    return str(record.get("raw", ""))


def _render_param(value, localized: bool):
    if isinstance(value, dict) and "message_id" in value:
        if localized:
            return render_record(value)
        source = _sources_by_id().get(value["message_id"], "")
        return english(
            source,
            *[_render_param(p, False) for p in value.get("args", [])],
            **{k: _render_param(v, False) for k, v in value.get("kwargs", {}).items()},
        )
    return value


@lru_cache(maxsize=1)
def _sources_by_id() -> dict[str, str]:
    from .message_catalog import catalog

    return {hashlib.sha256(source.encode()).hexdigest()[:20]: source for source in catalog()}
