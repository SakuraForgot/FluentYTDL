"""Language selection shared by the application and standalone processes."""

from __future__ import annotations


def normalize_language(value: str, system_language: str = "en_US") -> str:
    value = str(value or "auto").strip().replace("-", "_").lower()
    if value == "auto":
        value = str(system_language or "en_US").strip().replace("-", "_").lower()
    # Python's Windows locale API may return English language names instead of BCP-47.
    return "zh_CN" if value.split("_")[0] == "zh" or value.startswith("chinese") else "en_US"


def normalize_language_setting(value: str) -> str:
    setting = str(value or "auto").strip().lower()
    return "auto" if setting in {"", "auto"} else normalize_language(setting)
