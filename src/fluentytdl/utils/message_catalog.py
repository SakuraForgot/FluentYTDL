"""Qt-free English catalog for logs and standalone tools.

The JSON is generated from the reviewed Qt catalogs by i18n_release.py.
Never import configuration, paths, logging or Qt here: startup uses this module.
"""

from __future__ import annotations

import json
import locale
import os
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def catalog() -> dict[str, str]:
    roots = [Path(__file__).resolve().parents[3]]
    if getattr(sys, "frozen", False):
        roots = [
            Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)),
            Path(sys.executable).parent,
            *roots,
        ]
    for root in roots:
        path = root / "assets" / "locales" / "runtime_en.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def english(source: str, *args, **kwargs) -> str:
    template = catalog().get(source, source)
    return template.format(*args, **kwargs) if args or kwargs else template


def standalone_text(source: str, *args, **kwargs) -> str:
    from .language import normalize_language

    language = normalize_language(
        os.environ.get("FLUENTYTDL_UI_LANGUAGE", "auto"), locale.getlocale()[0] or "en_US"
    )
    template = catalog().get(source, source) if language == "en_US" else source
    return template.format(*args, **kwargs) if args or kwargs else template
