@echo off
cd /d "%~dp0.."
uv sync --locked --extra dev --extra build
if errorlevel 1 exit /b 1
uv run --locked --no-sync python scripts/build_gui.py
