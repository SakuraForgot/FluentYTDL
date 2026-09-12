# Contributing to FluentYTDL

Thank you for helping improve FluentYTDL. Bug reports, documentation fixes, translations, tests, and focused code changes are welcome.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Do not include cookies, account data, API tokens, private URLs, or unredacted logs in issues or pull requests. Security vulnerabilities must use the private process in [SECURITY.md](SECURITY.md).

## Before opening an issue

1. Install the latest stable release and update the managed components from the application.
2. Search existing issues and discussions for the same symptom.
3. Reduce the report to one URL type and one failing workflow when possible.
4. Remove credentials, cookies, tokens, personal paths, and private media URLs from logs.

Bug reports should include:

- FluentYTDL version and package type
- Windows version
- affected platform and URL type, without exposing private content
- exact stage that failed: parsing, authentication, format selection, download, post-processing, or update
- expected and actual behavior
- minimal reproduction steps and redacted log excerpts

## Development setup

FluentYTDL requires Python 3.12 or newer. Windows is the primary development and release platform.

```powershell
git clone https://github.com/SakuraForgot/FluentYTDL.git
cd FluentYTDL

uv sync --locked --extra dev --extra build
uv run python main.py
```

If `uv` is unavailable, create a virtual environment and run `pip install -e ".[dev]"`.

External tools such as yt-dlp, FFmpeg, Deno, and AtomicParsley are managed separately from Python dependencies. Do not commit downloaded binaries, cookies, logs, `config.json`, or local build output.

## Project rules

Read [docs/RULES_EN.md](docs/RULES_EN.md) and [docs/ARCHITECTURE_EN.md](docs/ARCHITECTURE_EN.md) before changing application architecture. In particular:

- keep yt-dlp behind the existing CLI subprocess boundary
- do not call yt-dlp directly from the UI
- communicate across UI and backend boundaries with Qt signals
- preserve the task sandbox and cancellation cleanup model
- consider all supported parsing workflows when changing shared download logic
- do not add a dependency without updating `pyproject.toml` and the applicable license notices

## Checks

Run the checks relevant to your change before opening a pull request:

```powershell
uv run ruff check src tests

$env:QT_QPA_PLATFORM = "offscreen"
uv run pytest tests -q

uv run python scripts/version_manager.py check
```

Pyright is currently advisory while existing type debt is reduced. New code should still use precise types and should not add broad `type: ignore` suppressions.

## Pull requests

- Keep each pull request focused on one problem.
- Explain the user-visible behavior and the root cause of a fix.
- Add or update tests for behavior changes.
- Include screenshots for UI changes and redact personal data.
- Preserve backward compatibility for configuration and persisted task data unless a migration is included.
- Do not combine formatting-only rewrites with functional changes.
- Allow maintainers to edit the branch when practical.

Contributions are licensed under the repository's [GNU GPL v3.0](LICENSE). Trademark and academic-integrity documents describe attribution and branding expectations but do not replace the source-code license.
