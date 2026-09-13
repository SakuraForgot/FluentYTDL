"""Per-attempt warning aggregation; raw evidence remains in the diagnostic buffer."""

from __future__ import annotations

import time

from .events import emit_event


class WarningSummary:
    def __init__(self, trace):
        self.trace = trace
        self.items = {}

    def feed(self, line: str) -> bool:
        """Return True for the first observation, False for repeated warnings."""
        try:
            from ..diagnostics.engine import parse_events

            events = parse_events(line if line.startswith("WARNING:") else "WARNING: " + line)
            event = events[0] if events else None
            code = event.code if event else "unknown_warning"
            component = event.component if event else ""
            key = (code, component, line if code in ("unknown", "unknown_warning") else "")
            if key not in self.items:
                if len(self.items) >= 64:
                    return True
                self.items[key] = {"count": 0, "first_seen": time.time(), "raw_line": line}
            item = self.items[key]
            item["count"] += 1
            item["last_seen"] = time.time()
            item["last_raw_line"] = line
            return item["count"] == 1
        except Exception:
            return True

    def finish(self):
        for (code, component, _), fields in self.items.items():
            if fields["count"] > 1:
                emit_event(
                    "signal",
                    trace=self.trace,
                    level="DEBUG",
                    code=code,
                    component=component,
                    severity_hint="warning",
                    aggregation="attempt_warnings",
                    **fields,
                )
