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
- **Language**: Python 3.12.12
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
target-version = "py312"
line-length = 100
select = ["E", "F", "I", "UP", "B"]
ignore = ["E501"]  # long lines allowed
```

- `F401` ignored in `__init__.py` files (re-exports are intentional)
- isort: `known-first-party = ["fluentytdl"]`

### Pyright (advisory)

```toml
pythonVersion = "3.12"
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

1. **Prefer yt-dlp's default `player_client` strategy** (tv → web_safari → android_vr) — never *pin* a single client. **Exception (SABR-only accounts):** when yt-dlp reports the account is under the SABR-only experiment (`forcing SABR streaming` / `formats ... missing a url` → high-res formats have no direct URL and get dropped, leaving only 360p), **append** `web_safari` to the client set (`default,web_safari`, or `<existing>,web_safari` when POT already set e.g. `default,mweb`). This is an *addition*, not a pin — default clients still run first; web_safari is the fallback that recovers direct-URL high-res (HLS). Detected at parse time (`yt_dlp_cli._maybe_mark_sabr_only`), persisted **on the account** (`WebView2Account.sabr_only`, or an in-memory session flag when logged out), and consumed in `build_ydl_options` (`_maybe_append_sabr_web_safari`) so it covers the parse **and** download opts paths (each builds opts independently). Provisional — may need tuning as more videos are sampled.
2. **NEVER enable `sleep_interval`** — causes signed URL expiry → HTTP 403
3. **NEVER use `--cookies-from-browser`** — causes DPAPI file lock on Windows
4. **`-S lang:xx` is inert — never use it for language preference.** `lang` is a **numeric** alias of `language_preference` and does not accept language codes (it rewrites the global `settings['lang']['convert']` to `'string'` and compares 10/5/−1/−10 against `"ja"`), and `FormatSorter.add_item` admits only the **first** `lang:` entry. Language and original-audio preferences must be expressed as format-string filters via `_inject_language_into_format()`: `[language^=xx]` (startswith — a bare `[language=en]` misses the real tag `en-US`) and `[language_preference>=?10]` for original audio (**the `?` is mandatory and belongs right after the operator** — `language_preference` exists only on YouTube, so without none-inclusive matching every audio track on Twitter etc. gets filtered away; and yt-dlp's filter grammar puts the marker between operator and value, so the value-side `>=10?` is a hard `SyntaxError: Invalid filter specification` that kills the whole download). The unfiltered format string always stays as the last fallback
5. **Validate file size on non-zero exit** — Windows `.part-Frag` deletion fails but download is complete
6. **Sync POT plugins to exe directory** — compiled yt-dlp cannot discover plugins via PYTHONPATH
7. **TUN mode: no proxy env vars** — injecting `HTTPS_PROXY` causes double-proxying
8. **web_music needs `disable_innertube=True`** — InnerTube challenges broken for that client
9. **BCP-47 alias expansion** — `zh-Hans` must match `zh-CN`, `zh-SG`, etc.; audio **and subtitles** both go through the single `utils/bcp47.py`. Every `--sub-langs` entry is matched by yt-dlp as an **anchored regex** against the real caption keys, so a bare `en` does NOT match `en-GB` — user preferences must be resolved to real keys (or fall back to `en(-.+)?`) before they reach the command line
10. **Transaction-scoped sandbox model** — `StagingArea` in `download/staging.py` owns the full artifact lifecycle. The lifecycle is: `create(txn-scoped uuid4 dir)` → `prepare_attempt(n)` → yt-dlp runs with `paths.home = payload/`, `paths.temp = .parts/` → `add_reported()` per output line → `reconcile()` (only physical scan, only `payload/`) → `seal_discovery()` → Feature mutations via `StagingArea` atomic API → `verify()` (safety gate, binary, no set subtraction) → `build_plan()` → `commit()` (cancel gate → phase=committing → reserve group → WAL-publish per item → phase=committed) → point of no return → `finalize_failure()` as the single failure/cancel arbitration point. Four explicit constraints that must never be violated:
    - **`outtmpl` must be relative and must pass `assert_inside`** — an absolute `-o` silences `-P` entirely and breaks typed-path sandbox redirection.
    - **From before `reserve`, the transaction is non-cancellable and must not be externally deleted** — `commit()` contains the single cancel gate; after it fires, cancellation only records `pending_cancel`. `core/controller.py` must not `rmtree` the sandbox of a live transaction (would pull source files out from under a commit, violating hard constraint 8).
    - **`phase=committed` is an irreversible success boundary** — post-commit exceptions (`emit_actual`, `cleanup`) may only become `kind=signal`; they must not change the outcome to `failed`.
    - **`delivered:*` tokens must not be consumed by any pre-commit gate** — they do not exist until after `commit()`, so `verify()` must not reference them (doing so makes every normal download fail its own safety gate).

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
- CI enforces lint, formatting, version/lock consistency, translation synchronization and tests; Pyright alone remains advisory.
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
- **Do not** bypass the transaction-scoped sandbox model for video downloads — every download mode must go through `StagingArea` (§4.10)
- **Do not** use an absolute path for `outtmpl` — it silences `-P` and breaks typed-path sandbox redirection (§4.10)
- **Do not** `rmtree` or externally delete a live transaction's sandbox — request cancellation and wait for the terminal-state callback instead (§4.10 hard constraint 8)
- **Do not** reference `delivered:*` tokens inside `verify()` or any pre-commit gate — those tokens don't exist until after `commit()` (§4.10)
- **Do not** let post-commit exceptions (`emit_actual`, `cleanup`) change the outcome to `failed` — they must become `kind=signal` only (§4.10 hard constraint 9)
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

- Build contract: Windows x64, Python **3.12.12**. Read `build-environment.json`; run `uv sync --locked --extra dev --extra build`. Do not silently re-lock during checks/builds.
- VERSION contains the bare version. Tags use `v` + VERSION. Only `version_manager.py set/bump` changes source versions and refreshes uv.lock. Build overrides are staged and NEVER modify source files.
- Accepted versions: X.Y.Z, X.Y.Z-rc.N, X.Y.Z-beta.N. Stable releases are Latest, rc releases are prereleases, beta produces Actions artifacts only.
- **Every new release MUST fetch the latest yt-dlp, FFmpeg/ffprobe, Deno, AtomicParsley, POT Provider AND embedded 7-Zip.** Keep existing upstream channels. Historical TOOLS.lock.json is an audit baseline, never a version ceiling. Fail download/integrity errors; never silently reuse older tools.
- Resolve releases once into a build-local snapshot with asset identity, URL, size and hashes. All artifacts in that build share it. Snapshot replay is diagnostic-only and forbidden for publication.
- `scripts/build.py::TARGET_OUTPUTS` is the only artifact contract: all=full+app-core+setup+manifest+checksums; 7z/full=full only; app-core=archive+manifest; setup=setup only; spec=frozen smoke only. Publication requires all.
- Each build has its own `build/runs/<id>` workspace. Never kill processes globally by executable name or delete historical release artifacts. `build/latest-result.json` points only to a successful build; publish only the explicit artifact paths and hashes in that report.
- App-core include/exclude and runtime-data exclusions come from pyproject.toml. Unknown top-level payload entries and polluted payloads fail packaging. All release targets share hygiene validation.
- `portable.txt` belongs only to full.7z; never place it in the shared dist tree. Frozen portable data uses the exe directory; installed config/database/logs use LocalAppData unless explicitly overridden. Authentication still uses app/bin/dle_user. Do not assume those roots are identical.
- updater.exe.new travels in app-core; never remove the updater self-update delivery/retry mechanism. Bundle the uninstall maintenance helper in the shared _internal tree so automatic updates do not remove uninstall support.
- Archive encoding is COPY/LZMA2 only. Verify real file hashes with py7zr and the frozen updater with empty PATH and Unicode paths. The frozen application must pass its isolated build self-test.
- Inno supports English/Simplified Chinese; current-user installation is default, all-users is optional. Preserve AppId and existing install scope. First-run language defaults never overwrite user settings.
- **Uninstall clears ALL accounts, cookies, config, task databases/history, logs and application caches, without a retention option.** All-users uninstall covers actual Windows users. Preserve downloaded media and unknown files. Never recursively delete an entire install/download/Documents root or follow reparse points. Cleanup failures must be reported as failures.
- Updates, overwrite installs and rollback preserve data; they must never call uninstall cleanup. Migration remains copy-only until startup acceptance, preserving rollback compatibility.
- Release validates tag/commit/main ancestry, executes shared checks and artifact tests, verifies a draft, publishes those exact bytes, then verifies public downloads/latest. Never replace public release assets under an existing version.
- Operational details and acceptance limits: `docs/build_release.md`.
