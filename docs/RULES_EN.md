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
- **Dark mode support**: use the `InfoBar` from `ui/components/common/custom_info_bar.py`, not the one imported straight from `qfluentwidgets`. It subclasses `qfluentwidgets.InfoBar` under the same name (import it as `from fluentytdl.ui.components.common.custom_info_bar import InfoBar`) and adds the dark-mode contrast handling. There is **no** class named `CustomInfoBar` — do not go searching for that name.
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
4. **`-S lang:xx` is inert — never use it for language preference.** `lang` is a **numeric** alias of `language_preference` and does not accept language codes (it rewrites the global `settings['lang']['convert']` to `'string'` and compares 10/5/−1/−10 against `"ja"`), and `FormatSorter.add_item` admits only the **first** `lang:` entry. Language and original-audio preferences must be expressed as format-string filters via `_inject_language_into_format()`: `[language^=xx]` (startswith — a bare `[language=en]` misses the real tag `en-US`) and `[language_preference>=10?]` for original audio (**the `?` is mandatory** — `language_preference` exists only on YouTube, and without none-inclusive matching every audio track on Twitter etc. gets filtered away). The unfiltered format string always stays as the last fallback
5. **Validate file size on non-zero exit** — Windows `.part-Frag` deletion fails but download is complete
6. **Sync POT plugins to exe directory** — compiled yt-dlp cannot discover plugins via PYTHONPATH
7. **TUN mode: no proxy env vars** — injecting `HTTPS_PROXY` causes double-proxying
8. **web_music needs `disable_innertube=True`** — InnerTube challenges broken for that client
9. **BCP-47 alias expansion** — `zh-Hans` must match `zh-CN`, `zh-SG`, etc.; audio **and subtitles** both go through the single `utils/bcp47.py`. Every `--sub-langs` entry is matched by yt-dlp as an **anchored regex** against the real caption keys, so a bare `en` does NOT match `en-GB` — user preferences must be resolved to real keys (or fall back to `en(-.+)?`) before they reach the command line
10. **Sandbox download model** — temp dir per task, move on success, sweep on cancel

See `docs/YTDLP_KNOWLEDGE_EN.md` for the full empirical knowledge base.

## 5. Observability Event Layer [CRITICAL]

`src/fluentytdl/observability/` is the project's single structured-log layer. Emit through
`emit_event(kind, *, trace=None, level="INFO", stage=None, **fields)` or `trace.emit(...)` —
`kind` is positional-only so business fields may also be named `kind`.

Its entire value rests on the five rules below. Break any one of them and `count(kind=...)`
stops meaning anything, which was the whole reason the layer replaced seven ad-hoc log channels.

### The Five Hard Rules

1. **`transition` has exactly one authoritative producer** — `download_manager._on_unified_status()`, the boundary where a UI state becomes a persisted state. One state change = one `transition`. A second producer doubles the count and invalidates every event-based statistic.
2. **`diagnosis` belongs only to the failure boundary.** Success-path anomalies are `signal`, never `diagnosis`, and the success path **never calls `diagnose()`** (that is root-cause arbitration). `signal` = what I observed; `diagnosis` = what this failure was finally judged to be. Only under that split is `count(kind=diagnosis)` identical to "an error really happened". Only the `except` block that built the `Diagnosis` emits it — the UI layer consumes and never emits.
3. **`degraded` may only be derived from `expected − actual`** — never from `what the system supports − actual`. A missing thumbnail the user never asked for is not a degradation.
4. **Exactly one `outcome` per `run_id`, and Outcome belongs to the run, not the task.** Subsystems report facts (`kind=recovery`, `kind=signal`) and must not declare final results; only the run's terminal boundary emits `outcome`. `task=42 run=A→paused, run=B→success` is legitimate.
5. **Observability is always best-effort.** `emit_event()` swallows its own exceptions and never raises into the caller. A logging layer that throws detonates precisely at the error site it exists to record.

### Identity (five levels)

`session → flow → task → run → attempt`. A rule-driven auto-retry calls `trace.next_attempt()` and
**keeps the same `run_id`**; only recovering the same task after a restart mints a new run via
`trace.new_run()`. If retry minted a new run, `run_id` and `attempt` would encode the same fact and
the hierarchy would collapse.

`flow_id` exists because `parse` / `select` happen before any `task_id` does — all four extract
workers run ahead of `DownloadWorker`. Without it, the timeline's entire pre-download half is
orphaned. `create_worker()` emits `kind=identity` once `db_id` is known, pinning flow → task.

### EventKind is a closed set

```
stage  decision  expect  actual  signal  diagnosis  recovery
retry  transition  outcome  config  argv  identity
```

Do not invent new kinds. Two are deliberately absent: **`mismatch`** (express it as
`actual matched=false expected=... actual=...`) and **`ui_progress`** (progress refreshes stay on the
Qt signal channel — emitting them would turn the JSONL trace into a progress database; only a
semantic phase switch earns `kind=stage`).

- `stage` ∈ `startup parse select preflight download postprocess verify finalize retry cancel`
- `outcome` ∈ `success failed cancelled paused interrupted restored_pending`; `recovered` and `degraded` are orthogonal flags and may both be true on the same outcome

### Field discipline

- **Record `code` / `category` / `severity`, never localized text.** `Diagnosis.user_title` / `user_message` / `recovery_hint` and any `self.tr()` string make log contents vary with the UI language and destroy searchability.
- **Sanitize before emitting** — `sanitize_argv()` / `sanitize_url()` / `sanitize_path()` / `sanitize_exception()`. argv can carry `socks5://user:pass@host`, cookie paths, and full output paths. Never log a token; record its length or `base_url` only.
- **`Event.as_dict()` must stay JSON-safe** (`Path`→str, `Enum`→value, `set`→sorted list, unknown→truncated `repr`). A text sink that prints fine while the JSONL sink raises `TypeError` is rule 5 failing at the worst possible moment.
- **`utils/` must not import `observability`** — `utils/logger.py` would cycle. Foundation modules take an opaque `trace: Any = None` and call `trace.emit(...)`; `processing/` and `youtube/` are Service layer and import it directly.
- **Emit only on real change.** "Checked it, left it alone" is not a decision — logging it breaks `count(kind=decision subsystem=container)` ≡ "how many times the system rewrote my container".
- **Do not change the `format` string of any loguru sink.** `emit_event()` renders `key=value` into the message itself; the machine-readable dict rides in `extra` via `logger.bind(fytdl=...)` for the JSONL sink alone. That is what keeps `log_viewer_window` and `log_signal_handler` risk-free.

Each hard rule has a matching assertion in `tests/test_observability_contract.py`. A rule without a
test is not a rule.

## 6. Cookie System Rules [CRITICAL]

### Two Truth Sources

`bin/cookies_youtube.txt` and `bin/cookies_twitter.txt` are the **only** cookie files yt-dlp ever reads. `yt_dlp_cli.py`'s `--cookies` injection is the single funnel — never add a second cookie path, and **never** `--cookies-from-browser` (§4.3, DPAPI lock). Every account cache under `bin/dle_user/<platform>/` and every browser extraction is upstream material that must be *committed* into a truth source before it has any effect.

Resolve paths through `cookie_sentinel.get_cookie_path_for_platform(platform)` / `get_meta_path_for_platform(platform)`. Nothing is keyed on a bare `cookies.txt` anymore, and no method that takes a `platform` argument may default it away silently — passing the wrong platform is the one failure mode that destroys the *other* platform's credentials.

### The Write Gate (weak fallback)

`CookieSentinel._commit_to_truth_source(src, platform, source_tag) -> (ok, reason)` is the **only** way to write a truth source. It parses the candidate, runs `auth_service._validate_cookies(cookies, platform)`, and only on success replaces the destination atomically (`.txt.tmp` → `os.replace`) and writes the `.txt.meta` sidecar.

- **Weak fallback**: validation failure leaves the old file **byte-identical**. "Extraction succeeded but the payload is half a cookie jar / all expired" must never overwrite a working file. Never reintroduce a bare `shutil.copy2` onto a truth source.
- The rejection reason is stored per platform (`get_commit_warning(platform)`) and surfaced on `get_status_info(platform)["commit_warning"]` so the UI can say "still using the older file" instead of failing silently.
- **Required cookies are per platform**: YouTube `SID / HSID / SSID / SAPISID / APISID`; X `auth_token / ct0`. Session cookies (`expires == 0`) are not expired.
- **Lazy cleanup**: NEVER delete old cookies until a new extraction has been committed.

### Refresh Concurrency & Threading

`force_refresh_with_uac(platform=None)` holds a **per-platform** mutex (`self._updating` set; `None` occupies both). Refreshing YouTube must not block a refresh of X.

Refresh performs DPAPI reads and waits on a WebView2 subprocess, so it must **never** run on the Qt main thread — that is the reported freeze. All UI entry points go through `CookieRefreshWorker(QThread)` and connect its `finished` signal with `Qt.ConnectionType.QueuedConnection`. `CookieSentinel` is a `QObject` and reports startup state via `startupHealthReady = Signal(dict)`; it must not import from `ui/`.

### Startup Behaviour

The startup refresh is **silent**. `get_startup_health()` returns per-platform `{enabled, exists, valid, expiring_soon, expiry_seconds, reason, commit_warning}`, and the UI only notifies for platforms with `enabled=True` — a user who only downloads from X must never be nagged about YouTube. Valid → nothing at all; expiring soon → `InfoBar.info`; missing or invalid → `InfoBar.warning`. Never re-add a timer-based popup guess.

### Extraction Sources

- **`BROWSER_COMBO_ITEMS` in `auth_service.py` is the single source of order** for every browser dropdown; `BROWSER_SOURCES` and `BROWSER_COMBO_LABELS` derive from it, and `browser_source_at()` / `browser_combo_index()` do the index mapping. Never hand-write a positional browser list in the UI layer again.
- **Chrome and CentBrowser are unsupported.** Their enum members are deliberately kept so `AuthSourceType(old_value)` cannot raise; `_load_config()` migrates them to `EDGE` once and writes the config back.
- **WebView2 mode must pass `is_webview2_runtime_available()` first** (`auth/webview2_runtime.py`, registry probe). Missing runtime gets a `MessageBox` with download / browser-extraction / manual-import exits — never a 5-minute queue wait. The subprocess wraps `webview.start()` in `try/except` and the parent polls `process.is_alive()` while waiting.
- **Chromium v130+**: needs admin for App-Bound Encryption decryption (`ADMIN_REQUIRED_BROWSERS`).
- **403 recovery**: auto-detect cookie expiry keywords, refresh the affected platform only.
- **JSON cookie files**: reject with warning (yt-dlp expects Netscape format).
- **WebView2 Mode**: WebView2 is the project's internal name for the WebView2-based cookie extraction method (formerly known as DLE). In code, always use `AuthSourceType.WEBVIEW2` and the term "webview2". In user-facing UI text, use "登录获取 (WebView2)" or "WebView2 登录". **NEVER** rename it back to "DLE" or use other terms.
- **WebView2 Provider**: The current implementation uses `WebView2CookieProvider` (in `providers/webview2_provider.py`). When referencing the provider, use "WebView2CookieProvider" in code and "WebView2" in user-facing contexts.

### Compliance Cleaning

`CookieCleaner.clean(cookies, platform, enable_cleaning, *, drop_expired=True)`. The name whitelist (`YOUTUBE_ALLOWED_NAMES`) applies to **YouTube only** — the twitter branch does no name filtering, so the cleaner cannot eat `auth_token` / `ct0` **as long as the caller passes the right platform**. That is the whole risk: `clean(x_cookies, "youtube")` returns an empty list. Domain matching is equal-or-subdomain only (a bare `.com` must not match `.x.com`), and `drop_expired` is a dimension independent of `enable_cleaning`.


## 7. Post-Processing Pipeline Order

1. `SponsorBlockFeature` — sponsorblock_remove/mark
2. `MetadataFeature` — FFmpegMetadata postprocessor
3. `SubtitleFeature` — language resolution, embed, cleanup
4. `ThumbnailFeature` — embed via AtomicParsley (MP4) > FFmpeg (MKV) > mutagen (audio)
5. `VRFeature` — EAC→Equi conversion + spatial metadata (VR mode only)

## 8. Testing Rules

- pytest >= 7.0
- Test files in `tests/` directory
- **No conftest.py yet** — each test does its own `sys.path` setup
- Some tests need a `QApplication` — they run headless as long as `QT_QPA_PLATFORM=offscreen` and `FLUENTYTDL_DATA_DIR_OVERRIDE` are set **before** importing fluentytdl (copy the header of `tests/test_subtitle_selector_ux.py`). Do not hardcode "N GUI tests" here; that number goes stale on every added test
- CI uses `continue-on-error: true` on all checks — nothing blocks merges
- When adding tests: prefer plain pytest functions over unittest.TestCase

## 9. What NOT To Do

- **Do not** use raw Qt widgets in UI (must use QFluentWidgets)
- **Do not** import yt-dlp as Python library (always use CLI subprocess)
- **Do not** use `cookies_from_browser` (DPAPI lock)
- **Do not** force sleep intervals (signed URL expiry)
- **Do not** create new singletons without documenting in Section 2
- **Do not** add dependencies without updating `pyproject.toml`
- **Do not** commit `config.json`, credentials, API tokens, or cookies
- **Do not** use `type:ignore` without discussion
- **Do not** bypass the sandbox download model for video downloads
- **Do not** write a cookie truth source with anything but `_commit_to_truth_source()` (§6)
- **Do not** call `force_refresh_with_uac()` from the Qt main thread — use `CookieRefreshWorker` (§6)
- **Do not** pass a `platform` you have not verified into `CookieCleaner.clean()` or `_validate_cookies()` — the wrong one wipes the other platform's credentials (§6)

## 10. Companion Documents

| Document | Purpose |
|----------|---------|
| `docs/ARCHITECTURE_EN.md` | Current architecture with 6 parsing flow details |
| `docs/YTDLP_KNOWLEDGE_EN.md` | Empirical yt-dlp troubleshooting knowledge |
| `docs/RULES.md` | Chinese version of this document |
| `CONTRIBUTING.md` | Contribution guidelines |
| `SECURITY.md` | Security policy |

## 11. Build & Release Rules

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
- **`build.py` only writes back to `VERSION` when `--version` is passed explicitly.** Building without `--version` never mutates the source of truth.
- Passing a `v`-prefixed version to `build.py` / `version_manager.py set` is rejected with a corrective hint.

### Build Targets [CRITICAL]

**`TARGET_OUTPUTS` in `scripts/build.py` is the single source of truth for target → artifacts.** `run_all()`'s dispatch, `_assert_expected_artifacts()`, `build_gui.py`'s output panel, and release.yml's verification step all read that one table. Never write a second copy.

| `--target` | Produces |
| --- | --- |
| `all` | `full.7z` + `app-core.7z` + `setup.exe` + `update-manifest.json` + `SHA256SUMS.txt` |
| `7z` (or `full`) | `full.7z` **only** |
| `app-core` | `app-core.7z` + `update-manifest.json` |
| `setup` | `setup.exe` **only** |
| `spec` | nothing — validates the PyInstaller blueprint and returns |

- **Targets are strict: anything the target does not name is not generated.** This used to be three separate `if target in ("all", "7z")` blocks plus unconditional manifest/checksum generation, so "portable only" still emitted `app-core.7z`, `update-manifest.json`, and a `SHA256SUMS.txt` that listed every historical leftover in `release/`. Adding a target means adding a row to the table, not another `if`.
- **`manifest` is bound to `app-core`, never produced alone.** The only component with real content in the manifest is the app-core archive; without it `generate_manifest.py` prints "⚠ app-core 归档不存在" and writes a hollow manifest whose publication makes the in-app updater believe the new version has no downloadable payload. Better no manifest than a hollow one.
- **`generate_checksums()` hashes only this build's artifacts**, not everything in `release/`. It walked the directory before, which is why a portable-only build shipped a checksum file naming other versions' packages.
- **`clean()` never touches `release/`.** Previous artifacts stay put by design (they are the only local copy), so a target-scoped build leaves unrelated files next to its own output. `build_gui.py` labels those 历史遗留 and offers a "clear `release/` first" checkbox; `_warn_foreign_release_files()` lists them but never deletes.
- **`--print-names --target X` prints that target's expected filenames and exits without building.** release.yml's verification step queries it instead of hardcoding a list — that hardcoded list is exactly what turned into a false failure when targets became strict.
- **`publish=true` requires `targets=all`** (enforced in release.yml's version step). A non-`all` target cannot fill the Release body's download links and omits `update-manifest.json`, which would break auto-update for every installed user. Tag pushes always force `all`.

### Release Artifacts

| Artifact | Audience |
| --- | --- |
| `FluentYTDL-{VERSION}-win64-full.7z` | **Primary recommendation** — portable, extract and run, bundles all `bin/` tools |
| `FluentYTDL-{VERSION}-win64-setup.exe` | Inno Setup installer — registry entries, shortcuts, requires admin |
| `FluentYTDL-{VERSION}-win64-app-core.7z` | **Internal** — incremental payload for in-app auto-update. Excludes `bin/` and `updater.exe`. Not standalone-runnable; never present it as a user download. |
| `update-manifest.json` | Consumed by the in-app updater via the `releases/latest/download/` RAW redirect |
| `SHA256SUMS.txt` | Integrity verification — covers the artifacts of **this build** only (`--target all`) |

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
