"""Isolate application state before collection imports any application module."""

import os
import tempfile

os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="fytdl-pytest-")
os.environ["FLUENTYTDL_LOG_ORIGIN"] = "test"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
