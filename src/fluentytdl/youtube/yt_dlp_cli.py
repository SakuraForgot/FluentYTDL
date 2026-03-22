from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from threading import Event
from typing import Any

from fluentytdl.utils.paths import (
    config_path,
    find_bundled_executable,
    is_frozen,
    locate_runtime_tool,
)

from ..core.config_manager import config_manager


class YtDlpCancelled(Exception):
    """Raised when a yt-dlp subprocess is cancelled by the UI."""


def _safe_working_dir() -> str:
    """Return a stable writable working directory for yt-dlp child processes."""

    try:
        configured = str(config_manager.get("download_dir") or "").strip()
        if configured:
            Path(configured).mkdir(parents=True, exist_ok=True)
            return str(Path(configured).resolve())
    except Exception:
        pass

    try:
        data_dir = config_path().parent
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir.resolve())
    except Exception:
        pass

    return str(Path.cwd())


def _win_hide_console_kwargs() -> dict[str, Any]:
    """Hide console window for subprocess on Windows (GUI apps)."""

    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {}
    try:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:
        pass

    return kwargs


def resolve_yt_dlp_exe() -> Path | None:
    """Resolve yt-dlp executable path.

    Priority:
    1) config yt_dlp_exe_path (if exists)
    2) bundled _internal/yt-dlp/yt-dlp.exe (frozen)
    3) yt-dlp on PATH
    """

    cfg = str(config_manager.get("yt_dlp_exe_path") or "").strip()
    if cfg:
        p = Path(cfg)
        if p.exists():
            return p

    # Prefer tools placed into exe-adjacent `bin` (or project `bin`) via locate_runtime_tool.
    try:
        return locate_runtime_tool("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
    except FileNotFoundError:
        # fallback to legacy bundled search when frozen
        if is_frozen():
            p = find_bundled_executable(
                "yt-dlp.exe",
                "yt-dlp/yt-dlp.exe",
                "yt_dlp/yt-dlp.exe",
            )
            if p is not None:
                return p

    which = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    return Path(which) if which else None


def _prepend_path(env: dict[str, str], *dirs: str) -> None:
    existing = env.get("PATH") or ""
    cleaned = [d for d in dirs if d]
    if not cleaned:
        return
    env["PATH"] = os.pathsep.join(cleaned + [existing]) if existing else os.pathsep.join(cleaned)


def prepare_yt_dlp_env(extra_paths: list[str] | None = None) -> dict[str, str]:
    """Prepare environment so yt-dlp.exe can find bundled ffmpeg and JS runtime.

    We intentionally prefer PATH injection over less-portable flags.

    Args:
        extra_paths: Additional paths to prepend to PATH
    """

    env = dict(os.environ)

    # FFmpeg
    ffmpeg_path = str(config_manager.get("ffmpeg_path") or "").strip() or None
    if ffmpeg_path and Path(ffmpeg_path).exists():
        _prepend_path(env, str(Path(ffmpeg_path).resolve().parent))
    else:
        try:
            p = locate_runtime_tool("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
            _prepend_path(env, str(Path(p).resolve().parent))
        except FileNotFoundError:
            bundled_ffmpeg = find_bundled_executable("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
            if bundled_ffmpeg is not None:
                _prepend_path(env, str(bundled_ffmpeg.resolve().parent))

    # JS runtime (deno preferred)
    js_runtime_path = str(config_manager.get("js_runtime_path") or "").strip() or None
    if js_runtime_path and Path(js_runtime_path).exists():
        _prepend_path(env, str(Path(js_runtime_path).resolve().parent))
    else:
        try:
            p = locate_runtime_tool("deno.exe", "js/deno.exe", "deno/deno.exe")
            _prepend_path(env, str(Path(p).resolve().parent))
        except FileNotFoundError:
            bundled_deno = find_bundled_executable("deno.exe", "js/deno.exe", "deno/deno.exe")
            if bundled_deno is not None:
                _prepend_path(env, str(bundled_deno.resolve().parent))

    # Extra paths
    if extra_paths:
        for p in extra_paths:
            if p and Path(p).exists():
                _prepend_path(env, p)

    # Allow yt-dlp to load plugins from the internal directory
    # The plugin directory structure is: src/fluentytdl/yt_dlp_plugins_ext/yt_dlp_plugins/
    plugin_dir = Path(__file__).resolve().parent.parent / "yt_dlp_plugins_ext"
    if plugin_dir.exists():
        existing_pypath = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = (
            f"{plugin_dir}{os.pathsep}{existing_pypath}" if existing_pypath else str(plugin_dir)
        )

    return env


def _inject_language_into_format(fmt: str, format_sort: list | str | None) -> str:
    """Prepend language-filtered format alternatives to the format string.

    yt-dlp's ``-S lang:xx`` cannot override the built-in ``language_preference=10``
    assigned to audio tracks marked as *original* + *default*.  To truly prefer a
    specific language, we need ``[language=xx]`` filters directly in the format
    selection string, using the original (unfiltered) format as the final fallback.

    Example::

        fmt   = "bv*[height<=1080]+ba[ext=m4a]/b[height<=1080]/bv*[height<=1080]+ba"
        langs from format_sort = ["ja", "zh-hans"]
        result = ("bv*[height<=1080]+ba[ext=m4a][language=ja]/b[height<=1080][language=ja]"
                  "/bv*[height<=1080]+ba[language=ja]"
                  "/bv*[height<=1080]+ba[ext=m4a][language=zh-hans]/b[height<=1080][language=zh-hans]"
                  "/bv*[height<=1080]+ba[language=zh-hans]"
                  "/bv*[height<=1080]+ba[ext=m4a]/b[height<=1080]/bv*[height<=1080]+ba")
    """
    if not fmt or not format_sort:
        return fmt

    # Extract language codes from format_sort entries (e.g. "lang:ja" -> "ja")
    langs: list[str] = []
    items = format_sort if isinstance(format_sort, list) else [format_sort]
    for item in items:
        s = str(item).strip().lower()
        if s.startswith("lang:"):
            code = s[5:].strip()
            if code and code != "orig":
                langs.append(code)

    if not langs:
        return fmt

    alternatives = [a.strip() for a in fmt.split("/") if a.strip()]
    if not alternatives:
        return fmt

    lang_groups: list[str] = []
    for lang in langs:
        lang_alts: list[str] = []
        for alt in alternatives:
            if "+" in alt:
                # Merge pattern: video+audio -> add [language=xx] to audio part
                parts = alt.split("+", 1)
                lang_alts.append(f"{parts[0]}+{parts[1]}[language={lang}]")
            else:
                # Single format (muxed or audio-only) -> append [language=xx]
                lang_alts.append(f"{alt}[language={lang}]")
        if lang_alts:
            lang_groups.append("/".join(lang_alts))

    if not lang_groups:
        return fmt

    # Prepend language-filtered alternatives, with original format as fallback
    return "/".join(lang_groups) + "/" + fmt


def ydl_opts_to_cli_args(ydl_opts: dict[str, Any]) -> list[str]:
    """Convert a subset of yt-dlp Python options to CLI args.

    This mapping is intentionally minimal and only covers what the app uses.
    """

    args: list[str] = []

    proxy = ydl_opts.get("proxy")
    if isinstance(proxy, str):
        args += ["--proxy", proxy]

    user_agent = ydl_opts.get("user_agent")
    if isinstance(user_agent, str) and user_agent:
        args += ["--user-agent", user_agent]

    for key, flag in [
        ("socket_timeout", "--socket-timeout"),
        ("retries", "--retries"),
        ("fragment_retries", "--fragment-retries"),
        ("sleep_interval", "--sleep-interval"),
        ("max_sleep_interval", "--max-sleep-interval"),
        ("concurrent_fragment_downloads", "-N"),  # 并发分片数
    ]:
        v = ydl_opts.get(key)
        if isinstance(v, (int, float)):
            args += [flag, str(int(v))]

    # 外部下载器
    external_downloader = ydl_opts.get("external_downloader")
    if isinstance(external_downloader, str) and external_downloader:
        args += ["--downloader", external_downloader]

    # 外部下载器参数
    external_downloader_args = ydl_opts.get("external_downloader_args")
    if isinstance(external_downloader_args, dict):
        for dl_name, dl_args in external_downloader_args.items():
            if isinstance(dl_args, list):
                args += ["--downloader-args", f"{dl_name}:{' '.join(dl_args)}"]
            elif isinstance(dl_args, str):
                args += ["--downloader-args", f"{dl_name}:{dl_args}"]

    # 下载限速
    ratelimit = ydl_opts.get("ratelimit")
    if isinstance(ratelimit, (int, float)) and ratelimit > 0:
        args += ["--limit-rate", f"{int(ratelimit)}"]
    elif isinstance(ratelimit, str) and ratelimit:
        args += ["--limit-rate", ratelimit]

    # Cookie 统一通过文件传递（由 AuthService 处理）
    # 不再支持 --cookies-from-browser，避免文件锁问题
    cookiefile = ydl_opts.get("cookiefile")
    if isinstance(cookiefile, str) and cookiefile:
        args += ["--cookies", cookiefile]

    js_runtimes = ydl_opts.get("js_runtimes")
    if isinstance(js_runtimes, dict):
        # yt-dlp CLI: --js-runtimes RUNTIME[:PATH]
        # Example: {"deno": {"path": "C:/.../deno.exe"}}
        for runtime_id, cfg in js_runtimes.items():
            rid = str(runtime_id or "").strip()
            if not rid:
                continue
            path = ""
            if isinstance(cfg, dict):
                path = str(cfg.get("path") or "").strip()
            elif isinstance(cfg, str):
                path = cfg.strip()
            value = f"{rid}:{path}" if path else rid
            args += ["--js-runtimes", value]

    ffmpeg_location = ydl_opts.get("ffmpeg_location")
    if isinstance(ffmpeg_location, str) and ffmpeg_location.strip():
        args += ["--ffmpeg-location", ffmpeg_location.strip()]

    extractor_args = ydl_opts.get("extractor_args")
    if isinstance(extractor_args, dict):
        # Example (python API):
        # {"youtube": {"player_client": ["android,ios"], "player_skip": ["js,configs,hls"]}}
        from loguru import logger

        logger.debug("[CLI] extractor_args 输入: {}", extractor_args)
        for ie_key, ie_args in extractor_args.items():
            if not ie_key:
                continue
            if not isinstance(ie_args, dict):
                continue
            parts: list[str] = []
            for k, v in ie_args.items():
                if not k:
                    continue
                if isinstance(v, (list, tuple)):
                    flat = [str(x) for x in v if str(x).strip()]
                    if not flat:
                        continue
                    val = ",".join(flat)
                else:
                    val = str(v)
                val = val.strip()
                if not val:
                    continue
                parts.append(f"{k}={val}")
                logger.debug("[CLI] ie_key={}, k={}, v={}, val={}", ie_key, k, v, val)
            logger.debug("[CLI] ie_key={}, parts={}", ie_key, parts)
            if parts:
                # See yt-dlp CLI: --extractor-args IE_KEY:ARGS, where ARGS is semicolon-separated.
                extractor_arg = f"{ie_key}:{';'.join(parts)}"
                logger.debug("[CLI] 添加参数: --extractor-args {}", extractor_arg)
                args += ["--extractor-args", extractor_arg]

    outtmpl = ydl_opts.get("outtmpl")
    if isinstance(outtmpl, str) and outtmpl:
        args += ["-o", outtmpl]

    paths = ydl_opts.get("paths")
    if isinstance(paths, dict):
        # Support {'home': '...'} or {'temp': '...'}
        # CLI -P supports setting home path.
        home = paths.get("home")
        if isinstance(home, str) and home.strip():
            args += ["-P", home.strip()]
        # Note: yt-dlp CLI allows multiple -P, e.g. -P "temp:..."
        temp = paths.get("temp")
        if isinstance(temp, str) and temp.strip():
            args += ["-P", f"temp:{temp.strip()}"]

    fmt = ydl_opts.get("format")
    if isinstance(fmt, str) and fmt:
        # Inject [language=xx] filters into format string for multi-language audio.
        # -S lang:xx alone cannot override language_preference=10 on original tracks.
        format_sort_val = ydl_opts.get("format_sort")
        fmt = _inject_language_into_format(fmt, format_sort_val)
        args += ["-f", fmt]

    # 格式排序（音轨语言偏好等）
    format_sort = ydl_opts.get("format_sort")
    if isinstance(format_sort, list) and format_sort:
        args += ["-S", ",".join(str(x) for x in format_sort)]
    elif isinstance(format_sort, str) and format_sort:
        args += ["-S", format_sort]

    # format_sort_force: 强制排序覆盖用户提供的格式
    if ydl_opts.get("format_sort_force"):
        args += ["--format-sort-force"]

    merge_fmt = ydl_opts.get("merge_output_format")
    if isinstance(merge_fmt, str) and merge_fmt:
        args += ["--merge-output-format", merge_fmt]

    # ========== top-level audio extract params (simple/playlist mode) ==========
    # extract_audio / audio_format / audio_quality as top-level keys
    if ydl_opts.get("extract_audio"):
        args += ["--extract-audio"]
        audio_fmt = ydl_opts.get("audio_format")
        if isinstance(audio_fmt, str) and audio_fmt:
            args += ["--audio-format", audio_fmt]
        audio_quality = ydl_opts.get("audio_quality")
        if isinstance(audio_quality, str) and audio_quality:
            args += ["--audio-quality", audio_quality]
        elif isinstance(audio_quality, (int, float)):
            args += ["--audio-quality", str(audio_quality)]

    if ydl_opts.get("addmetadata") is True:
        args += ["--add-metadata"]

    # 封面缩略图下载
    if ydl_opts.get("writethumbnail") is True:
        args += ["--write-thumbnail"]

    # 转换封面格式（用于嵌入）
    convert_thumbnail_format = ydl_opts.get("convert_thumbnail")
    if isinstance(convert_thumbnail_format, str) and convert_thumbnail_format:
        args += ["--convert-thumbnails", convert_thumbnail_format]

    # Postprocessors handling
    postprocessors = ydl_opts.get("postprocessors")
    if isinstance(postprocessors, list):
        has_embed_metadata = False

        for pp in postprocessors:
            if not isinstance(pp, dict):
                continue
            key = str(pp.get("key") or "").strip()

            # 音频提取
            if key == "FFmpegExtractAudio":
                codec = str(pp.get("preferredcodec") or "mp3").strip() or "mp3"
                quality = str(pp.get("preferredquality") or "192").strip() or "192"
                args += [
                    "--extract-audio",
                    "--audio-format",
                    codec,
                    "--audio-quality",
                    f"{quality}K",
                ]

            # 封面嵌入 - 注意：现在由外置工具处理，yt-dlp 只负责下载封面
            elif key == "EmbedThumbnail":
                # 不再使用 yt-dlp 内置的封面嵌入，改用外置 AtomicParsley/FFmpeg
                pass

            # 元数据嵌入
            elif key == "FFmpegMetadata":
                has_embed_metadata = True

            # 封面格式转换（备用方式）
            elif key == "FFmpegThumbnailsConvertor":
                fmt = str(pp.get("format") or "jpg").strip()
                if fmt and "--convert-thumbnails" not in args:
                    args += ["--convert-thumbnails", fmt]

        # 注意：封面嵌入现在由外置工具 (AtomicParsley/FFmpeg) 处理
        # yt-dlp 只负责下载封面（通过 writethumbnail 选项）
        # 不再添加 --embed-thumbnail 参数

        # 添加元数据嵌入参数
        if has_embed_metadata:
            args += ["--embed-metadata"]

    # 后处理器参数（如 loudnorm 音量标准化）
    postprocessor_args = ydl_opts.get("postprocessor_args")
    if isinstance(postprocessor_args, dict):
        for pp_name, pp_args in postprocessor_args.items():
            if isinstance(pp_args, list):
                # CLI 格式: --postprocessor-args NAME:ARGS
                args += ["--postprocessor-args", f"{pp_name}:{' '.join(pp_args)}"]
            elif isinstance(pp_args, str):
                args += ["--postprocessor-args", f"{pp_name}:{pp_args}"]

    # ========== 字幕相关参数 ==========

    # 写入字幕
    if ydl_opts.get("writesubtitles"):
        args += ["--write-sub"]
    elif ydl_opts.get("writesubtitles") is False:
        # 显式禁用：覆盖外部 yt-dlp 配置中可能存在的 --write-sub
        args += ["--no-write-sub"]

    # 写入自动字幕
    if ydl_opts.get("writeautomaticsub"):
        args += ["--write-auto-sub"]
    elif ydl_opts.get("writeautomaticsub") is False:
        # 显式禁用：覆盖外部 yt-dlp 配置中可能存在的 --write-auto-sub
        args += ["--no-write-auto-sub"]

    # 字幕语言
    subtitleslangs = ydl_opts.get("subtitleslangs")
    if isinstance(subtitleslangs, list) and subtitleslangs:
        args += ["--sub-langs", ",".join(subtitleslangs)]
    elif isinstance(subtitleslangs, str) and subtitleslangs:
        args += ["--sub-langs", subtitleslangs]

    # 嵌入字幕
    if ydl_opts.get("embedsubtitles"):
        args += ["--embed-subs"]
        # 强制转换为 srt 以解决各大播放器对 mkv 中的 webvtt 轨兼容性差的问题（不转换会导致用户以为并没嵌进去）
        if not ydl_opts.get("convertsubtitles"):
            args += ["--convert-subs", "srt"]

    # 字幕格式转换 (显式指定的情况下)
    convert_subs = ydl_opts.get("convertsubtitles")
    if isinstance(convert_subs, str) and convert_subs:
        args += ["--convert-subs", convert_subs]

    # ========== 片段下载参数 ==========

    # 下载片段
    download_sections = ydl_opts.get("download_sections")
    if isinstance(download_sections, str) and download_sections:
        args += ["--download-sections", download_sections]

    # 强制关键帧切割
    if ydl_opts.get("force_keyframes_at_cuts"):
        args += ["--force-keyframes-at-cuts"]

    # ========== SponsorBlock 参数 ==========

    # 移除片段
    sponsorblock_remove = ydl_opts.get("sponsorblock_remove")
    if isinstance(sponsorblock_remove, list) and sponsorblock_remove:
        for cat in sponsorblock_remove:
            args += ["--sponsorblock-remove", cat]
    elif isinstance(sponsorblock_remove, str) and sponsorblock_remove:
        args += ["--sponsorblock-remove", sponsorblock_remove]

    # 标记片段
    sponsorblock_mark = ydl_opts.get("sponsorblock_mark")
    if isinstance(sponsorblock_mark, list) and sponsorblock_mark:
        for cat in sponsorblock_mark:
            args += ["--sponsorblock-mark", cat]
    elif isinstance(sponsorblock_mark, str) and sponsorblock_mark:
        args += ["--sponsorblock-mark", sponsorblock_mark]

    # 嵌入章节
    if ydl_opts.get("embed_chapters"):
        args += ["--embed-chapters"]

    # 跳过下载（仅获取元数据/字幕/封面等）
    if ydl_opts.get("skip_download"):
        args += ["--skip-download"]

    # NOTE: POT Token / POT Provider 的 extractor_args 已在 youtube_service.build_ydl_options() 中统一处理
    # 无需在此处再次添加

    return args


def _terminate_process_best_effort(proc: subprocess.Popen[str]) -> None:
    try:
        proc.terminate()
    except Exception:
        return
    try:
        proc.wait(timeout=1.0)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def run_dump_single_json(
    url: str,
    ydl_opts: dict[str, Any],
    extra_args: list[str] | None = None,
    *,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    exe = resolve_yt_dlp_exe()
    if exe is None:
        raise FileNotFoundError("未找到 yt-dlp.exe（既没有内置也不在 PATH 中）")

    cmd = [
        str(exe),
        "--no-color",
        "--no-progress",
        "-J",
        *ydl_opts_to_cli_args(ydl_opts),
    ]
    if extra_args:
        cmd += list(extra_args)
    cmd.append(url)

    env = prepare_yt_dlp_env()
    work_dir = _safe_working_dir()

    if cancel_event is None:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=work_dir,
            **_win_hide_console_kwargs(),
        )

        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp.exe 解析失败 (code={proc.returncode})\n{out.strip()}")
    else:
        proc2 = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=work_dir,
            **_win_hide_console_kwargs(),
        )

        # IMPORTANT: We must continuously drain stdout/stderr to avoid deadlocks
        # when yt-dlp prints large JSON (common for playlists).
        stdout = ""
        stderr = ""
        while True:
            if cancel_event.is_set():
                _terminate_process_best_effort(proc2)
                raise YtDlpCancelled("yt-dlp cancelled")
            try:
                stdout, stderr = proc2.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                # keep pumping
                time.sleep(0.02)
                continue

        out = (stdout or "") + "\n" + (stderr or "")
        if proc2.returncode != 0:
            raise RuntimeError(f"yt-dlp.exe 解析失败 (code={proc2.returncode})\n{out.strip()}")

    # yt-dlp may print other lines; pick the last parsable JSON line.
    for line in reversed(out.splitlines()):
        s = line.strip()
        if not s:
            continue
        if not (s.startswith("{") or s.startswith("[")):
            continue
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return data
        except Exception:
            continue

    raise RuntimeError(f"yt-dlp 未输出可解析的 JSON:\n{out.strip()}")


def run_version() -> str:
    exe = resolve_yt_dlp_exe()
    if exe is None:
        return ""
    try:
        out = subprocess.check_output(
            [str(exe), "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            **_win_hide_console_kwargs(),
        )
        return (out or "").strip()
    except Exception:
        return ""
