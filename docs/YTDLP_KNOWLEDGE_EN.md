# yt-dlp Empirical Knowledge Book

> [中文版](YTDLP_KNOWLEDGE.md)
>
> Each entry follows: **Symptom → Root Cause → Rule → Code Reference**

## 1. Download Failures

### 1.1 403 During Download (Signed URL Expiry)

**Symptom**: HTTP 403 mid-download, especially on long videos

**Root Cause**: `sleep_interval` introduces delays between requests; YouTube signed URLs have short TTL (~minutes)

**Rule**: NEVER set `sleep_interval_min` or `sleep_interval_max` to non-zero values

**Code**: `src/fluentytdl/youtube/youtube_service.py` — AntiBlockingOptions comment

### 1.2 403 on Age-Restricted / Members-Only Content

**Symptom**: "Sign in to confirm your age" or "This video is members-only"

**Root Cause**: Missing or expired cookies; age-restricted content requires authenticated session

**Rule**: CookieSentinel auto-detects these keywords and triggers cookie refresh flow

**Code**: `src/fluentytdl/auth/cookie_sentinel.py` — `COOKIE_ERROR_KEYWORDS`

### 1.3 Bot Detection (LOGIN_REQUIRED)

**Symptom**: `LOGIN_REQUIRED` error from yt-dlp extraction

**Root Cause**: YouTube detected automated access; needs PO Token or fresh cookies

**Rule**: POT Manager provides PO Token; if unavailable, falls back to `mweb` client with static token from settings

**Code**: `src/fluentytdl/youtube/youtube_service.py` — fallback logic in `build_ydl_options()`

### 1.4 "Page Needs to Be Reloaded" Error

**Symptom**: yt-dlp returns error about page reload

**Root Cause**: Stale cookies or session state

**Rule**: Auto-detect this error and force-refresh DLE cookies for a single retry

**Code**: `src/fluentytdl/youtube/youtube_service.py` — retry logic in extraction methods

### 1.5 Playlist Auth Check Hint

**Symptom**: yt-dlp hints about `youtubetab:skip=authcheck`

**Root Cause**: YouTube playlist requires auth check that can be skipped

**Rule**: Auto-detect this hint and inject `youtubetab:skip=authcheck` as extractor-arg on retry

**Code**: `src/fluentytdl/youtube/youtube_service.py` — playlist retry logic

### 1.6 Sub-request Failures Also Print `ERROR:` (the task did not fail)

**Symptom**: when one caption track gets rate-limited, yt-dlp prints `ERROR: Unable to download video subtitles for 'zh-Hans-en-GB': HTTP Error 429: Too Many Requests` — yet it goes on to fetch the other languages and finishes normally, possibly with exit code 0

**Root Cause**: that `ERROR:` prefix describes **that one sub-request**, not the task. A diagnostics layer that tiers events by prefix will put it in the same tier as genuinely fatal errors and let priority decide between them

**Rule**: such rules declare `"appliesTo": "both"` to accept the `ERROR:` line, then `"demoteToWarning": true` to push the event back into the warning tier. Priority stays untouched — it is needed to beat more generic rules on the **same line** (subtitle 429 over `rate_limited_429`), while the level decides the tier in cross-line arbitration. Without the demotion, the subtitle rule's p98 shoves a real cause like `bot_check_sign_in` (p92) into a footnote; without `appliesTo: both`, the real-world `ERROR:` line never even gets a chance to match and falls through to the generic `rate_limited_429`, so the subtitle-specific hint never appears

**Code**: `src/fluentytdl/diagnostics/engine.py::parse_events`, `assets/error_rules.json`

## 2. Format Selection

### 2.1 `-S lang:xx` Never Meant "Prefer This Language"

**Symptom**: `-S lang:xx` in format sort does not select the correct audio track

**Root Cause** (verified against upstream source; the earlier note claiming "`language_preference=10` overrides sort priority" is **wrong**): `FormatSorter.settings` has no sort field named `language` at all — `lang` is a **numeric** alias of `language_preference`. Given a language code, `"ja".isnumeric()` is false, so yt-dlp rewrites the **global** `settings['lang']['convert'] = 'string'` with `limit="ja"` and the sort key degenerates into comparing 10/5/−1/−10 as strings against `"ja"`. Worse, `add_item` has `if field in self._order: return` — of a run of `lang:` entries **only the first is ever admitted**, so expanding BCP-47 aliases into a dozen of them contributes exactly nothing

**Rule**: language and original-audio preferences can **only** be expressed as filters inside the format string, via `_inject_language_into_format()`:

- Language uses `[language^=xx]` (startswith). A bare `[language=en]` does not match the real tag `en-US`, which is precisely why "preferred English, got no audio track at all" happened; alias branches use exact `=` (the alias table widens `zh-Hans` to bare `zh`, and `^=` would also match `zh-Hant`)
- Original audio uses `[language_preference>=?10]`. **The `?` (none-inclusive) must not be dropped**: `_build_format_filter`'s `_filter` returns `m.group('none_inclusive')` when `actual_value is None`, which is falsy without `?` — and only the YouTube extractor computes `language_preference`, so platforms like Twitter would have **every** audio track filtered away. **Its position is part of the syntax**: the marker sits between operator and value (`>=?10`). The value-side `>=10?` fails to parse as a number and real yt-dlp rejects the run outright with `SyntaxError: Invalid filter specification` (measured against yt-dlp 2026.08.30)
- The unfiltered format string always stays as the final fallback branch. Without it, a video with no matching track selects no format at all (the merge fails outright)
- Intent travels via `ydl_opts["_fytdl_audio_langs"]` / `["_fytdl_audio_strategy"]` (underscore prefix = never reaches argv); `format_sort` keeps only `res,br,fps,acodec`

**The four `language_preference` values** (computed by `extractor/youtube/_video.py::get_language_code_and_preference()`, present in `-J` output): `10` original, `5` `audioIsDefault` (account/region default, **not** original), `-1` ordinary dub, `-10` descriptive audio. The project's audio-track classification reads exactly this field (`format_scorer.audio_track_kind()`)

**Code**: `src/fluentytdl/youtube/yt_dlp_cli.py` — `_plan_language_injection()` / `_ORIGINAL_AUDIO_FILTER`; `tests/test_audio_format_injection.py` is its regression lock

### 2.2 BCP-47 Alias Expansion

**Symptom**: Language matching fails for locale variants — audio (`zh-CN` vs `zh-Hans`), subtitles (preference `en` fails to match the real key `en-GB`)

**Root Cause**: YouTube uses different locale codes in different contexts. Subtitles go further: real caption keys carry a region (`en-GB`), and auto-generated/translated ones are `{target}-{source}` compound keys (`en-en-GB`, `zh-Hans-en-GB`)

**Rule**:

- Audio: aliases expand into filter branches inside the format string (see §2.1). Do **not** put `lang:` entries into format_sort — that path was disproven; not one of them takes effect
- Subtitles: **every `--sub-langs` entry is matched by yt-dlp as an anchored regex** against the caption keys — a bare `en` does not match `en-GB`, not a single `.vtt` gets written, and yt-dlp leaves only `[info] There are no subtitles for the requested languages` behind. User preferences must therefore be resolved to real keys via `bcp47.resolve_requested()` first; when the available-track list cannot be obtained, fall back to `bcp47.to_sub_langs_pattern()` (`en(-.+)?`, **not** `en.*` — the latter would also match `eng`/`enm`, two different languages)
- Matching is **one-directional**: `en` matches `en-GB`, but `en-GB` does not match `en` (a user who explicitly asked for British English should not be handed a region-less `en`)
- Simplified and Traditional Chinese must **never** mix: `zh-Hans` must not match `zh-Hant`

**Code**: `src/fluentytdl/utils/bcp47.py` (the single authority; `utils/format_scorer.py` keeps only a thin delegation)

### 2.3 web_music Client Needs disable_innertube

**Symptom**: Format extraction fails for YouTube Music URLs

**Root Cause**: `web_music` client has broken InnerTube challenge handling

**Rule**: When using `web_music` player_client, always set `disable_innertube=True` in PO Token request

**Code**: `src/fluentytdl/yt_dlp_plugins_ext/yt_dlp_plugins/extractor/getpot_bgutil_http.py`

### 2.4 No player_client Forcing

**Symptom**: Temptation to force `android` or `ios` client for better formats

**Root Cause**: Android/iOS simulation can return incomplete format lists; yt-dlp's default strategy (selected by the active yt-dlp version and request authentication) is well-tested

**Rule**: NEVER force `player_client` via extractor_args; trust yt-dlp defaults

**Code**: `src/fluentytdl/youtube/youtube_service.py` — comment in `build_ydl_options()`

## 3. Windows-Specific Issues

### 3.1 .part-Frag File Deletion Failure

**Symptom**: yt-dlp returns exit code 1 but download appears complete

**Root Cause**: Windows file locking prevents deletion of `.part-Frag` files; download is actually complete

**Rule**: On non-zero exit code, check if output file exists and its size >= 50% of expected total bytes

**Code**: `src/fluentytdl/download/executor.py` — expected size validation

### 3.2 DPAPI Cookie Lock

**Symptom**: Browser cookie extraction hangs or fails; other browser features break

**Root Cause**: `--cookies-from-browser` locks Chrome/Edge SQLite DB via DPAPI on Windows

**Rule**: NEVER use `--cookies-from-browser`; always extract to file via rookiepy first

**Code**: `src/fluentytdl/auth/auth_service.py`

### 3.3 POT Plugin Discovery Failure

**Symptom**: PO Token provider not found by compiled yt-dlp.exe

**Root Cause**: Standalone compiled yt-dlp does not read PYTHONPATH for plugin discovery

**Rule**: Sync POT plugin `.py` files to `<exe-dir>/yt-dlp-plugins/bgutil-ytdlp-pot-provider/` using mtime-based incremental sync

**Code**: `src/fluentytdl/youtube/yt_dlp_cli.py` — `sync_pot_plugins_to_ytdlp()`

### 3.4 Process Tree Termination

**Symptom**: Orphaned yt-dlp or ffmpeg processes after cancel

**Root Cause**: `terminate()` only kills direct child, not spawned subprocesses

**Rule**: On Windows, use `taskkill /F /T /PID` to kill entire process tree

**Code**: `src/fluentytdl/download/executor.py` — process termination logic

### 3.5 Cross-Drive File Move Failure

**Symptom**: `os.replace()` fails when temp and target are on different drives

**Root Cause**: `os.replace()` cannot move across drives on Windows

**Rule**: Create temp files in same directory as target to avoid cross-drive moves

**Code**: `src/fluentytdl/download/workers.py` — sandbox directory creation

## 4. Network / Proxy

### 4.1 TUN Mode Double-Proxy

**Symptom**: Downloads fail or are extremely slow when system TUN/VPN (e.g., V2RayN) is active

**Root Cause**: Injecting `HTTPS_PROXY`/`HTTP_PROXY` env vars causes traffic to go through both TUN and proxy

**Rule**: When TUN mode is detected, do NOT inject proxy env vars into POT Manager subprocess

**Code**: `src/fluentytdl/youtube/pot_manager.py` — proxy injection logic

### 4.2 Proxy Off Override

**Symptom**: System proxy still used even when "No Proxy" selected

**Root Cause**: System-level proxy env vars override yt-dlp settings

**Rule**: When proxy mode is "off", explicitly set `proxy: ""` to override any system proxy

**Code**: `src/fluentytdl/youtube/youtube_service.py` — `NetworkOptions`

### 4.3 Localhost Proxy Bypass

**Symptom**: POT Manager HTTP requests go through TUN proxy instead of localhost

**Root Cause**: `urllib.request.urlopen()` respects system proxy settings

**Rule**: Use empty `ProxyHandler` for localhost requests to bypass TUN-mode proxies

**Code**: `src/fluentytdl/youtube/pot_manager.py` — `_local_urlopen()`

## 5. Cookie System

### 5.1 Lazy Cookie Cleanup

**Symptom**: Old cookies deleted before new extraction succeeds → auth gap

**Root Cause**: Eager cleanup removes working cookies before replacement is verified

**Rule**: NEVER delete old cookies until new extraction succeeds and validates

**Code**: `src/fluentytdl/auth/cookie_sentinel.py`

### 5.2 Required YouTube Cookies

**Symptom**: Partial auth — some features work, others don't

**Root Cause**: Missing required cookie fields

**Rule**: Validate presence of: SID, HSID, SSID, SAPISID, APISID

**Code**: `src/fluentytdl/auth/cookie_cleaner.py`

### 5.3 Chromium v130+ App-Bound Encryption

**Symptom**: Cookie extraction fails silently on newer Chrome/Edge

**Root Cause**: Chromium v130+ uses App-Bound Encryption requiring admin privileges for decryption

**Rule**: Detect Chromium version and prompt for admin elevation if needed

**Code**: `src/fluentytdl/auth/cookie_manager.py`

### 5.4 JSON Cookie File Rejection

**Symptom**: User provides cookies in JSON format, yt-dlp ignores them

**Root Cause**: yt-dlp expects Netscape format only

**Rule**: Detect JSON cookie files and reject with clear warning message

**Code**: `src/fluentytdl/youtube/youtube_service.py`

## 6. VR Video

### 6.1 Dual-Pass Extraction

**Symptom**: VR video shows low resolution formats only

**Root Cause**: Default client does not expose high-res VR formats; need `android_vr` client

**Rule**: VR mode always uses `extract_vr_info_sync()` with `player_client=["android_vr"]`

**Code**: `src/fluentytdl/youtube/youtube_service.py:1229`

### 6.2 EAC to Equirectangular Conversion

**Symptom**: VR video plays in wrong projection on non-VR players

**Root Cause**: Some YouTube VR videos use EAC (Equi-Angular Cubemap) projection

**Rule**: If projection is EAC and auto-convert enabled, run ffmpeg `v360=eac:e` filter

**Code**: `src/fluentytdl/download/features.py:308` — `VRFeature.on_post_process()`

### 6.3 VR Detection Heuristics

**Symptom**: Non-VR video incorrectly detected as VR, or VR video missed

**Root Cause**: VR detection uses multiple signals: title keywords, format metadata, resolution anomalies

**Rule**: Check projection field, tags, and title for keywords (360, VR, vr180, equirectangular)

**Code**: `src/fluentytdl/core/video_analyzer.py`

## 7. Sandbox Download Model

### 7.1 Temp Directory Per Task

**Symptom**: Partial files pollute download directory on cancel

**Root Cause**: Direct download to final directory leaves fragments on failure

**Rule**: Each download runs in `.fluent_temp/task_<id>/`; files moved to final dir only on success

**Code**: `src/fluentytdl/download/workers.py` — sandbox creation in `DownloadWorker`

### 7.2 Cancel Cleanup Delay

**Symptom**: Sandbox directory not fully deleted on cancel

**Root Cause**: Windows file lock release takes time after process termination

**Rule**: Wait 1 second after process kill before sweeping sandbox directory

**Code**: `src/fluentytdl/download/workers.py` — cancel cleanup logic
