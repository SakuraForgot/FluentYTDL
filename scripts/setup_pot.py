"""Install upstream POT into the actual development tool directory and verify it.

Usage: uv run python scripts/setup_pot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from PySide6.QtCore import QCoreApplication

    from fluentytdl.core.config_manager import config_manager
    from fluentytdl.utils.component_paths import component_target
    from fluentytdl.youtube.pot_manager import pot_manager

    app = QCoreApplication.instance() or QCoreApplication([])
    target = component_target("pot-provider")
    from fluentytdl.core.pot_upstream import install_release
    from fluentytdl.core.update_transport import configured_check

    check = configured_check()
    try:
        candidate = install_release(check.pot_release(), target, check.transport)
    finally:
        check.transport.session.close()
    # Explicit setup validates readiness in this process only; saved preferences
    # (including a deliberately disabled provider) must survive unchanged.
    enabled = config_manager.get("pot_provider_enabled", True)
    config_manager.config["pot_provider_enabled"] = True
    try:
        pot_manager.request_retry()
        ready = pot_manager.wait_until_ready(timeout=60)
        app.processEvents()
        state = pot_manager.snapshot()
        print(
            json.dumps(
                {
                    "target": str(target),
                    "bundle_id": candidate.bundle_id,
                    "ready": ready,
                    "code": state["code"],
                    "detail": state["detail"],
                    "deno_path": state.get("deno_path"),
                    "deno_version": state.get("deno_version"),
                    "token_length": state["token_length"],
                    "saved_pot_enabled": enabled,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if ready else 1
    finally:
        config_manager.config["pot_provider_enabled"] = enabled
        pot_manager.stop_server()


if __name__ == "__main__":
    raise SystemExit(main())
