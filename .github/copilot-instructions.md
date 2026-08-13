# FluentYTDL — Copilot Instructions

> Auto-generated from `docs/RULES_EN.md` by `scripts/sync_rules.py`
>
> Companion documents (read on demand):
> - `docs/ARCHITECTURE_EN.md` — Architecture with 6 parsing flow details
> - `docs/YTDLP_KNOWLEDGE_EN.md` — Empirical yt-dlp troubleshooting knowledge

# FluentYTDL Development Rules

> [中文版](RULES.md)
>
> This is the source file for AI rule generation. CLAUDE.md, AGENTS.md, and .github/copilot-instructions.md are generated from this file by `scripts/sync_rules.py`.

## 1. Project Identity

- **Name**: FluentYTDL — Professional YouTube/video downloader
- **Language**: Python 3.10+
- **UI Framework**: PySide6 (Qt6) + QFluentWidgets (Fluent Design)
- **Download Engine**: yt-dlp CLI subprocess (NOT Python API)
- **Media Processing**: FFmpeg
- **Codebase**: 148 .py files, ~50k LOC, `src/fluentytdl/` package
- **Platform**: Windows primary, cross-platform aspirational

## 2. Architecture Rules

### Layer Separation

```
UI Layer (ui/)
  ↓ depends on
Service Layer (auth/, youtube/, download/, processing/, storage/)
  ↓ depends on
Core Infrastructure (core/)
  ↓ depends on
Foundation (utils/, models/)
```

- **UI must NOT call yt-dlp directly** — go through `youtube_service`
- **Services must NOT import from ui/** — communicate via Qt Signals
- **Models are self-contained** — no circular dependencies

### Singletons

The project uses singleton pattern extensively. Key singletons: `config_manager`, `download_manager`, `auth_service`, `cookie_sentinel`, `youtube_service`, `pot_manager`, `task_db`.

When creating a new singleton, document it in this list.

### Qt Signal/Slot

All UI-backend communication MUST use Qt Signal/Slot mechanism. Never call backend methods directly from UI event handlers — emit a signal instead.

### Six Parsing Modes

The project supports 6 distinct parsing modes. See `docs/ARCHITECTURE_EN.md` Section 3 for the complete flow of each mode:

1. **Video** — standard single video download
2. **VR** — VR video with `android_vr` client, EAC conversion
3. **Channel** — channel tab listing with lazy loading
4. **Playlist** — playlist with batch operations
5. **Subtitle** — standalone subtitle download (lightweight extract)
6. **Cover** — standalone thumbnail download (direct or lightweight)

When modifying download logic, consider the impact on ALL 6 modes.

## 3. Code Style

### Ruff (enforced)

```toml
target-version = "py310"
line-length = 100
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]  # long lines allowed
```

- `F401` ignored in `__init__.py` files (re-exports are intentional)
- isort: `known-first-party = ["fluentytdl"]`

### Pyright (advisory)

```toml
pythonVersion = "3.10"
# Many report* settings are relaxed — do not add new type:ignore without discussion
```

### UI Rules (PySide6-Fluent-Widgets Best Practices)

- **MUST** use QFluentWidgets (`FluentWindow`, `InfoBar`, `MessageBox`, etc.).
- **NEVER** use raw `QMessageBox`, `QDialog`, or plain `QWidget` when building new UI components. Always prefer `qfluentwidgets` equivalents.
- **Imports**: Prefer direct imports from `qfluentwidgets` (e.g., `from qfluentwidgets import PushButton`) rather than mixing raw `PySide6.QtWidgets` unless for layouts (e.g., `QVBoxLayout`).
- **Theme Adaptation**: ABSOLUTELY NO hardcoded colors. Use `isDarkTheme()` or QFluentWidgets' `ThemeColor` macros to ensure seamless Light/Dark mode transitions.
- **Routing & Pages**: Complex sub-interfaces must inherit from core page components (like `ScrollArea` or `QWidget` styled appropriately) and be registered via the main window's navigation/router interface.
- **Delegates**: Use QPainter delegates for list items (avoids QWidget overhead for large lists).
- **Dark mode support**: use `CustomInfoBar`, not raw InfoBar.
- **Typography & Font Weight**: NEVER use plain `BodyLabel` for titles, instructions, or prominent text (causes text to appear thin and blurry). MUST use `StrongBodyLabel` or `SubtitleLabel`.
- **Text Color Contrast**: NEVER hardcode `Qt.GlobalColor.darkGray` or `QColor(160, 160, 160)`. Secondary descriptive text (e.g., `CaptionLabel`) MUST manually receive high contrast colors via `setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))` to ensure sharpness in dark mode.
- **SettingCard Safety**: NEVER globally monkey patch `SettingCard` behavior. Custom components (like `InlineComboBoxCard`) may replace `contentLabel` with a standard `QLabel` lacking the `setTextColor` method, causing `AttributeError` during global initialization. Use explicit `findChildren` iterations within the page's `__init__` and perform `hasattr` checks instead.

### File Naming

- Snake_case for all Python files
- One class per file preferred (especially in ui/components/)
- Prefix `_` for private module-level functions

## 4. yt-dlp Integration Rules [CRITICAL]

These rules are hard-won from production issues. Violating them WILL cause user-facing bugs.

1. **NEVER force `player_client`** — trust yt-dlp's default strategy (tv → web_safari → android_vr)
2. **NEVER enable `sleep_interval`** — causes signed URL expiry → HTTP 403
3. **NEVER use `--cookies-from-browser`** — causes DPAPI file lock on Windows
4. **Language format injection** — `-S lang:xx` cannot override `language_preference=10`; use `_inject_language_into_format()`
5. **Validate file size on non-zero exit** — Windows `.part-Frag` deletion fails but download is complete
6. **Sync POT plugins to exe directory** — compiled yt-dlp cannot discover plugins via PYTHONPATH
7. **TUN mode: no proxy env vars** — injecting `HTTPS_PROXY` causes double-proxying
8. **web_music needs `disable_innertube=True`** — InnerTube challenges broken for that client
9. **BCP-47 alias expansion** — `zh-Hans` must match `zh-CN`, `zh-SG`, etc.
10. **Sandbox download model** — temp dir per task, move on success, sweep on cancel

See `docs/YTDLP_KNOWLEDGE_EN.md` for the full empirical knowledge base.

## 5. Cookie System Rules

- `CookieSentinel` manages the single `bin/cookies.txt` lifecycle
- **Lazy cleanup**: NEVER delete old cookies until new extraction succeeds
- **Required cookies**: SID, HSID, SSID, SAPISID, APISID
- **Chromium v130+**: needs admin for App-Bound Encryption decryption
- **403 recovery**: auto-detect cookie expiry keywords, prompt refresh
- **JSON cookie files**: reject with warning (yt-dlp expects Netscape format)
- **WebView2 Mode**: WebView2 is the project's internal name for the WebView2-based cookie extraction method (formerly known as DLE). In code, always use `AuthSourceType.WEBVIEW2` and the term "webview2". In user-facing UI text, use "登录获取 (WebView2)" or "WebView2 登录". **NEVER** rename it back to "DLE" or use other terms.
- **WebView2 Provider**: The current implementation uses `WebView2CookieProvider` (in `providers/webview2_provider.py`). When referencing the provider, use "WebView2CookieProvider" in code and "WebView2" in user-facing contexts.

## 6. Post-Processing Pipeline Order

1. `SponsorBlockFeature` — sponsorblock_remove/mark
2. `MetadataFeature` — FFmpegMetadata postprocessor
3. `SubtitleFeature` — bilingual merge, embed, cleanup
4. `ThumbnailFeature` — embed via AtomicParsley (MP4) > FFmpeg (MKV) > mutagen (audio)
5. `VRFeature` — EAC→Equi conversion + spatial metadata (VR mode only)

## 7. Testing Rules

- pytest >= 7.0
- Test files in `tests/` directory
- **No conftest.py yet** — each test does its own `sys.path` setup
- 2 tests require GUI (QApplication) — cannot run in headless CI
- 1 test has no assertions (test_error_parser.py) — needs fixing
- CI uses `continue-on-error: true` on all checks — nothing blocks merges
- When adding tests: prefer plain pytest functions over unittest.TestCase

## 8. What NOT To Do

- **Do not** use raw Qt widgets in UI (must use QFluentWidgets)
- **Do not** import yt-dlp as Python library (always use CLI subprocess)
- **Do not** use `cookies_from_browser` (DPAPI lock)
- **Do not** force sleep intervals (signed URL expiry)
- **Do not** create new singletons without documenting in Section 2
- **Do not** add dependencies without updating `pyproject.toml`
- **Do not** commit `config.json`, credentials, API tokens, or cookies
- **Do not** use `type:ignore` without discussion
- **Do not** bypass the sandbox download model for video downloads

## 9. Companion Documents

| Document | Purpose |
|----------|---------|
| `docs/ARCHITECTURE_EN.md` | Current architecture with 6 parsing flow details |
| `docs/YTDLP_KNOWLEDGE_EN.md` | Empirical yt-dlp troubleshooting knowledge |
| `docs/RULES.md` | Chinese version of this document |
| `CONTRIBUTING.md` | Contribution guidelines |
| `SECURITY.md` | Security policy |

## 10. Build & Release Rules

### Version Management

- **Source of truth**: `VERSION` file (project root), holding a **bare** version with **no `v` prefix**
- **Do not manually edit** version numbers in `__init__.py`, `pyproject.toml`, or `FluentYTDL.iss` — use `scripts/version_manager.py`
- The Git tag is always `"v" + VERSION` — the `v` lives in the tag, never in the file

### Version Format (PEP 440 / SemVer)

```text
MAJOR.MINOR.PATCH[-(rc|beta).N]
```

| VERSION file | Git tag | Channel | Distribution |
| --- | --- | --- | --- |
| `3.5.5` | `v3.5.5` | stable | GitHub Release (Latest) — receives in-app auto-update |
| `3.5.6-rc.1` | `v3.5.6-rc.1` | rc | GitHub Release (Pre-release) — auto-update **locked** |
| `3.6.0-beta.1` | `v3.6.0-beta.1` | beta | Artifacts only, distributed in groups/channels — auto-update **locked** |

- **No prefixes.** The legacy `v-` / `pre-` / `beta-` prefix scheme is retired. Runtime code still *reads* those formats for backward compatibility with installs predating 3.5.5, but nothing writes them.
- `3.5.6-rc.1` is valid PEP 440 (normalizes to `3.5.6rc1`), so `pyproject.toml` stores the full version.
- **Inno Setup / PE resources accept only numeric versions.** `FluentYTDL.iss` stores the numeric part (`3.5.6`); its `MyAppVersionNumeric` macro truncates at the first hyphen.
- Only `rc` and `beta` are accepted as pre-release channels. `alpha`, bare `-rc`, and `3.5.5rc1` are rejected.

### AI Agent: Release Workflow

**Stable release**:

1. `python scripts/version_manager.py set 3.5.6`
2. `python scripts/version_manager.py check` (verify consistency across all 4 files)
3. `git add -A && git commit -m "release: v3.5.6"`
4. `git tag v3.5.6`
5. `git push && git push --tags`
6. CI auto-triggers `release.yml` → build → GitHub Release (Latest)

**Release candidate**: `python scripts/version_manager.py set 3.5.6-rc.1` (or `bump patch --pre rc`), then steps 2-5 with tag `v3.5.6-rc.1` → GitHub Release marked Pre-release.

**Beta**: `python scripts/version_manager.py set 3.6.0-beta.1`, then steps 2-5 → Artifacts only, no GitHub Release. Project lead downloads from GitHub Actions Artifacts.

### Local Build

- GUI: `python scripts/build_gui.py` → leave the version field blank to use `VERSION` → click Build
- CLI: `python scripts/build.py --target all` (version read from `VERSION`)
- `--target` options: `all`, `7z` (or `full`), `setup`
- **`build.py` only writes back to `VERSION` when `--version` is passed explicitly.** Building without `--version` never mutates the source of truth.
- Passing a `v`-prefixed version to `build.py` / `version_manager.py set` is rejected with a corrective hint.

### Release Artifacts

| Artifact | Audience |
| --- | --- |
| `FluentYTDL-{VERSION}-win64-full.7z` | **Primary recommendation** — portable, extract and run, bundles all `bin/` tools |
| `FluentYTDL-{VERSION}-win64-setup.exe` | Inno Setup installer — registry entries, shortcuts, requires admin |
| `FluentYTDL-{VERSION}-win64-app-core.7z` | **Internal** — incremental payload for in-app auto-update. Excludes `bin/` and `updater.exe`. Not standalone-runnable; never present it as a user download. |
| `update-manifest.json` | Consumed by the in-app updater via the `releases/latest/download/` RAW redirect |
| `SHA256SUMS.txt` | Integrity verification for all of the above |

Asset download URLs are keyed by **tag**, not version — `generate_manifest.py` takes `--tag` for exactly this reason (`/releases/download/v3.5.5/FluentYTDL-3.5.5-win64-full.7z`).

### Packaging Hygiene [CRITICAL]

- **`pyproject.toml [tool.fluentytdl.build]` is the single source of truth for what ships.** `app_core_include` (whitelist), `app_core_exclude` (known-and-deliberately-dropped), and `dist_forbidden` (runtime-garbage denylist) live there and nowhere else. `classify_app_core_items()` fails the build when a top-level `dist/` entry matches neither list — the whitelist's real hazard is a *new legitimate* artifact being silently dropped, and that assertion turns it into a red light. Keep each array on a **single line**: `_load_config()` falls back to a `key = [...]` line parser on Python 3.10 (no `tomllib`), and a multi-line array parses as empty, which silently disables the check.
- **`assert_dist_clean()` runs for all three release targets** (`full.7z`, `app-core.7z`, `setup.exe`), never just one. Anyone who launches the app from `dist/` leaves their own `config.json`, `logs/`, `state/tasks/tasks.db` behind, and `bin/cookies_*.txt` + `bin/dle_user/` hold **real credentials** — shipping those in a public archive is a session leak, not a cosmetic flaw. `full.7z` legitimately contains `bin/` and `updater.exe`, so it cannot reuse the app-core whitelist; the denylist is what covers it.
- **Building `updater.exe` requires the `build` extra: `uv sync --extra build`.** `py7zr` is the updater's only way to unpack an app-core archive. The pinned version must match in three places — `pyproject.toml`'s `build` extra, `.github/workflows/release.yml`'s `PY7ZR_VERSION`, and the assertion inside `scripts/updater.spec`.
- **`updater.exe.new` is delivered *with* app-core; `updater.exe` is not in it.** On a user's machine the running `updater.exe` cannot overwrite itself, so fixes reach installed users as a plain file in the archive: `build_updater()` copies its output to `dist/updater.exe.new`, and the actual swap happens either in `main.py::_cleanup_update_residuals()` (portable / writable install paths) or in the elevated post-exit helper `updater.py::_self_update_updater()` (Program Files). The two are each other's fallback — never "clean up" `updater.exe.new` on failure, it is the retry material.

### Data Location [CRITICAL]

`utils/paths.py::user_data_dir()` resolves the data root by **double track, never by probing for write permission**:

| Situation | Location |
| --- | --- |
| `--data-dir` / `FLUENTYTDL_DATA_DIR_OVERRIDE` set | that path (used when the updater relaunches the new build de-elevated) |
| frozen + `portable.txt` next to the exe | the exe's own directory (portable `full.7z`) |
| frozen, no marker | `%LOCALAPPDATA%\FluentYTDL` (installed builds) |
| not frozen | `project_root()` |

- **Never reintroduce a `.writetest` write probe.** That is exactly what split one machine's data into two trees: an elevated session could write into `C:\Program Files\FluentYTDL` while a normal session could not, and the user saw "the update ate my settings and my task list".
- **`portable.txt` goes only into `full.7z`**, appended by `create_7z()` from a `tempfile.TemporaryDirectory()`. It must never be written into `dist/` — `dist/` is the shared source for app-core and `setup.exe`, and `dist_forbidden` lists it so a slip breaks the build. `.iss` also carries `Excludes: "portable.txt"` as belt-and-braces.
- **Migration copies and never deletes the legacy location** (`migrate_user_data()`), because a binary rollback must stay a data-compatible rollback. The `.migrated_v2` marker is written only by `finalize_startup()` → `commit_migration_marker()`, only when the run had zero failures — writing it earlier would let a rolled-back build keep using the old path while the next update skips migration and adopts a stale copy.
- **`paths.py` must never import loguru.** `utils/logger.py:13` evaluates `LOG_DIR = str(user_data_dir() / "logs")` at import time, so the import would cycle; migration messages are queued in module-level lists and replayed by `utils/startup_info.py::log_startup_info()`.

### Notes

- `build.py` syncs the version to `pyproject.toml`, `__init__.py`, and `.iss` before building; `__init__.py` is skipped when it reads `VERSION` dynamically
- Output filenames carry the bare version, never the tag: `FluentYTDL-3.5.5-win64-full.7z`
- Missing ISCC or a missing `.iss` is a **hard failure** — the build never reports success with zero artifacts
