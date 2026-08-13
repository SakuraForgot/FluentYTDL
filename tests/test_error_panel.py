"""Regression coverage for errors presented by the download error panel."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.diagnostics import diagnose
from fluentytdl.models.errors import YtDlpExecutionError


def test_execution_error_context_maps_to_diagnosis():
    """YtDlpExecutionError 的三段上下文要能原样喂给诊断引擎。"""
    execution_error = YtDlpExecutionError(
        1,
        "ERROR: HTTP Error 403: Forbidden",
        {"error": {"_type": "DownloadError"}},
    )

    diagnosis = diagnose(
        execution_error.exit_code,
        execution_error.stderr,
        execution_error.parsed_json,
    )

    assert diagnosis.code == "http_403_forbidden"
    assert diagnosis.category == "network"
    assert diagnosis.severity == "recoverable"
    assert diagnosis.fix_action == "switch_proxy"
    assert "403" in diagnosis.user_title


def test_panel_payload_carries_everything_the_ui_reads():
    """错误面板只吃 dict，to_dict() 必须自带展开后的文案字段。"""
    payload = diagnose(1, "ERROR: HTTP Error 403: Forbidden").to_dict()

    for key in (
        "code",
        "category",
        "severity",
        "fix_action",
        "user_title",
        "user_message",
        "recovery_hint",
        "technical_detail",
    ):
        assert key in payload, key

    assert payload["user_title"].strip()
    assert payload["user_message"].strip()
