"""End-to-end language contracts for compiled catalogs, progress and log replay."""

from __future__ import annotations

import copy
import json
import os
import re
import string
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-i18n-"))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402
from PySide6.QtCore import QLocale, QTranslator  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fluentytdl.download.output_parser import YtDlpOutputParser  # noqa: E402
from fluentytdl.download.playlist_progress import PlaylistProgressTracker  # noqa: E402
from fluentytdl.utils.clean_logger import CleanLogger  # noqa: E402
from fluentytdl.utils.language import normalize_language, normalize_language_setting  # noqa: E402
from fluentytdl.utils.localized_log import log_text, metadata_filter, render_record  # noqa: E402
from fluentytdl.utils.log_history import match_display_records, read_display_records  # noqa: E402
from fluentytdl.utils.message_catalog import catalog, standalone_text  # noqa: E402
from fluentytdl.utils.ui_text import tr_text  # noqa: E402

HAN = re.compile(r"[\u4e00-\u9fff]")


def _format_fields(text):
    return Counter(
        (field, spec, conversion)
        for _, field, spec, conversion in string.Formatter().parse(text)
        if field is not None
    )


@pytest.fixture
def language():
    app = QApplication.instance() or QApplication([])
    original = QLocale()
    translator = QTranslator()

    def select(name):
        app.removeTranslator(translator)
        assert translator.load(str(ROOT / "assets/locales" / f"fluentytdl_{name}.qm"))
        app.installTranslator(translator)
        QLocale.setDefault(QLocale(name))
        return app

    yield select
    app.removeTranslator(translator)
    QLocale.setDefault(original)


@pytest.mark.parametrize(
    "setting,system,expected",
    [
        ("auto", "zh_TW", "zh_CN"),
        ("auto", "zh_HK", "zh_CN"),
        ("auto", "ja_JP", "en_US"),
        ("auto", "fr_FR", "en_US"),
        ("auto", "en_GB", "en_US"),
        ("auto", "Chinese (Traditional)_Taiwan", "zh_CN"),
        ("auto", "Chinese (Simplified)_China", "zh_CN"),
        ("zh-TW", "en_US", "zh_CN"),
        ("ja_JP", "zh_CN", "en_US"),
        ("en_AU", "zh_CN", "en_US"),
    ],
)
def test_language_resolution(setting, system, expected):
    assert normalize_language(setting, system) == expected
    assert normalize_language_setting(setting) == ("auto" if setting == "auto" else expected)


def test_compiled_english_matches_every_active_translation(language):
    language("en_US")
    translator = QTranslator()
    assert translator.load(str(ROOT / "assets/locales/fluentytdl_en_US.qm"))
    root = ET.parse(ROOT / "assets/locales/fluentytdl_en_US.ts").getroot()
    for context in root.findall("context"):
        for message in context.findall("message"):
            translation = message.find("translation")
            if translation is not None and translation.get("type") in {"vanished", "obsolete"}:
                continue
            source = message.findtext("source", "")
            expected = message.findtext("translation", "")
            assert translation is not None and not translation.get("type"), (
                context.findtext("name"),
                source,
            )
            assert expected.strip() and not HAN.search(expected), source
            assert _format_fields(source) == _format_fields(expected), source
            assert translator.translate(context.findtext("name", ""), source) == expected, source
            if context.findtext("name") == "RuntimeText":
                assert catalog()[source] == expected


@pytest.mark.parametrize("locale", ["en_GB", "en_AU", "en_CA"])
def test_english_regional_aliases(locale, language):
    language("en_US")
    translator = QTranslator()
    assert translator.load(QLocale(locale), "fluentytdl", "_", str(ROOT / "assets/locales"))
    assert translator.translate("SettingsPage", "外观") == "Appearance"


def _progress_replay():
    output = []
    clean = CleanLogger(lambda *row: output.append(row))
    clean.handle_status("[youtube] Extracting URL: https://example.invalid/video")
    clean.handle_status("[info] Downloading 1 format(s)")
    for filename, info in [
        ("a.mp4", {"vcodec": "avc1", "acodec": "none"}),
        ("a.m4a", {"vcodec": "none", "acodec": "mp4a"}),
        ("a.srt", {}),
        ("a.jpg", {}),
    ]:
        clean.handle_progress(
            dict(
                status="downloading",
                downloaded_bytes=512,
                total_bytes=1024,
                speed=64,
                eta=8,
                filename=filename,
                info_dict=info,
            )
        )
    clean.handle_status("Retrying fragment 1 (2/3)")
    clean.handle_status("There are no subtitles for the requested languages")
    for pp in [
        "Merger",
        "EmbedSubtitle",
        "Metadata",
        "ThumbnailsConvertor",
        "EmbedThumbnail",
        "MoveFiles",
        "SponsorBlock",
    ]:
        clean.handle_progress(dict(status="postprocess", postprocessor=pp, pp_status="started"))
    for state, text in [
        ("paused", "⏸️ 下载已暂停"),
        ("downloading", "▶️ 继续下载..."),
        ("processing", "🧾 正在核对产物..."),
        ("processing", "📦 正在整理文件..."),
        ("completed", "✅ 下载并处理完成！"),
        ("error", "❌ 错误: {0}"),
        ("cancelled", "🗑️ 任务已取消并清理残骸"),
    ]:
        clean.force_update(
            state, 100.0, tr_text(text, "HTTP 403") if "{0}" in text else tr_text(text)
        )
    return output


def test_progress_language_changes_text_only(language):
    language("zh_CN")
    chinese = _progress_replay()
    language("en_US")
    english = _progress_replay()
    assert [(r[0], r[1]) for r in chinese] == [(r[0], r[1]) for r in english]
    assert all(not HAN.search(r[2]) for r in english)
    assert any("Remaining: 00:08" in r[2] for r in english)
    assert any("Video stream" in r[2] for r in english)
    assert any("Audio stream" in r[2] for r in english)
    assert any("Verifying artifacts" in r[2] for r in english)


def test_playlist_keeps_user_titles(language):
    language("en_US")
    tracker = PlaylistProgressTracker(
        total_items=3, current_item=2, current_title="用户的视频", item_eta=9, item_speed=10
    )
    assert "用户的视频" in tracker.build_status_text()
    assert "Remaining: 00:09" in tracker.build_status_text()
    assert not HAN.search(tracker.build_completed_text())


def test_postprocessor_labels_translate_without_changing_protocol(language):
    parser = YtDlpOutputParser()
    for pp in ["Merger", "MoveFiles", "Metadata", "EmbedSubtitle", "FFmpegSubtitlesConvertor"]:
        language("zh_CN")
        chinese = parser.parse_line(f"FLUENTYTDL|postprocess|started|{pp}")
        language("en_US")
        english = parser.parse_line(f"FLUENTYTDL|postprocess|started|{pp}")
        assert chinese.postprocessor == english.postprocessor == pp
        assert chinese.postprocessor_status == english.postprocessor_status == "started"
        assert english.message and not HAN.search(english.message)


def test_display_text_can_cross_copy_boundaries(language):
    language("zh_CN")
    value = copy.deepcopy(tr_text("已完成 {0} 个视频 · 总计 {1}", 3, "1MB"))
    assert value.message_args == (3, "1MB")
    assert str(value) == "已完成 3 个视频 · 总计 1MB"


def test_logs_preserve_exception_caller_and_nested_language(language, tmp_path):
    records = []
    handle = logger.add(
        lambda msg: records.append(msg.record), filter=metadata_filter, format="{message}"
    )
    try:
        language("zh_CN")
        try:
            raise ValueError("original error")
        except ValueError as exc:
            log_text(
                logger.opt(exception=exc),
                "error",
                "[POT][Diagnose] 探测输出:\n{}",
                tr_text("已完成 {0} 个视频 · 总计 {1}", 3, "1MB"),
            )
        assert len(records) == 1
        record = records[0]
        assert record["exception"].type is ValueError
        assert record["function"] == "test_logs_preserve_exception_caller_and_nested_language"
        assert not HAN.search(record["message"])
        payload = record["extra"]["display_record"]
        encoded = json.loads(record["extra"]["display_json"])
        assert payload == encoded and "source" not in payload
        assert "已完成 3 个视频" in render_record(payload)
        language("en_US")
        assert "Completed 3 videos" in render_record(payload)
        # A fresh read from a compressed, partially damaged history remains usable.
        with zipfile.ZipFile(tmp_path / "display_2026-09-12.jsonl.zip", "w") as archive:
            archive.writestr(
                "display.jsonl", json.dumps(payload) + "\n{broken\n" + json.dumps(payload) + "\n"
            )
        restored = read_display_records(tmp_path)
        assert restored == [payload]
        time = record["time"].strftime("%H:%M:%S")
        lines = record["message"].splitlines()
        entries = [
            (time, "ERROR", record["name"], lines[0]),
            *[(time, "ERROR", "", line) for line in lines[1:]],
        ]
        language("zh_CN")
        replay = list(match_display_records(entries, restored))
        assert len(replay) == 1 and "已完成 3 个视频" in replay[0][0][3]
        assert replay[0][1] == record["message"]
    finally:
        logger.remove(handle)


def test_legacy_and_corrupt_metadata_preserve_raw_log(tmp_path):
    (tmp_path / "display_2026-09-12.jsonl").write_text("{broken\n[]\n", encoding="utf-8")
    raw = [("12:00:00", "INFO", "legacy", "旧版原始日志")]
    assert list(match_display_records(raw, read_display_records(tmp_path))) == [(raw[0], raw[0][3])]


def test_standalone_language_does_not_require_qt(monkeypatch):
    monkeypatch.setenv("FLUENTYTDL_UI_LANGUAGE", "en_US")
    assert standalone_text("FluentYTDL 更新") == "FluentYTDL Update"
    monkeypatch.setenv("FLUENTYTDL_UI_LANGUAGE", "zh_CN")
    assert standalone_text("FluentYTDL 更新") == "FluentYTDL 更新"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from fluentytdl.utils.message_catalog import standalone_text; assert standalone_text('FluentYTDL 更新'); assert not any(n.startswith('PySide6') for n in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mode", ["coarse", "precise"])
def test_clip_progress_in_both_languages(language, mode):
    results = []
    for name in ("zh_CN", "en_US"):
        language(name)
        events = []
        clean = CleanLogger(
            lambda *row, events=events: events.append(row),
            section_cut_mode=mode,
            section_duration=60,
            section_start=120,
            section_stream_layout="video_audio",
            section_estimated_bytes=4_000_000,
        )
        clean.handle_progress(
            dict(status="section_file_progress", output_bytes=2_000_000, speed=500_000)
        )
        clean.handle_progress(
            dict(status="ffmpeg_progress", time_sec=150, speed="1.5x", output_bytes=2_000_000)
        )
        if mode == "precise":
            clean.handle_progress(
                dict(status="postprocess", postprocessor="ModifyChapters", pp_status="started")
            )
            clean.handle_progress(dict(status="ffmpeg_progress", time_sec=30, speed="2.0x"))
        results.append(events)
    assert [(e[0], e[1]) for e in results[0]] == [(e[0], e[1]) for e in results[1]]
    assert all(not HAN.search(e[2]) for e in results[1])
    assert "488.28KB/s" in results[1][0][2]


def test_log_metadata_redacts_credentials(language):
    language("en_US")
    records = []
    handle = logger.add(lambda m: records.append(m.record), filter=metadata_filter)
    try:
        log_text(
            logger,
            "warning",
            "读取失败: {0}",
            "https://alice:secretpass@example.test token=abc password=hunter2",
        )
        serialized = records[0]["extra"]["display_json"]
        assert "secretpass" not in serialized and "hunter2" not in serialized
        assert "abc" not in serialized
        assert "example.test" in serialized
    finally:
        logger.remove(handle)


def test_runtime_catalog_languages_are_closed():
    files = {
        p.name for p in (ROOT / "assets/locales").iterdir() if p.suffix in {".ts", ".qm", ".json"}
    }
    assert files == {
        "fluentytdl_en_US.ts",
        "fluentytdl_zh_CN.ts",
        "fluentytdl_en_US.qm",
        "fluentytdl_en.qm",
        "fluentytdl_zh_CN.qm",
        "fluentytdl_zh.qm",
        "runtime_en.json",
    }


def test_metadata_sink_exception_jsonl_and_unwritable_fallback(tmp_path, language):
    from fluentytdl.utils.localized_log import install_metadata_sink

    language("en_US")
    handle = install_metadata_sink(logger, str(tmp_path))
    assert handle is not None
    try:
        try:
            raise ValueError(tr_text("读取失败: {0}", "test.txt"))
        except ValueError as exc:
            log_text(logger.opt(exception=exc), "error", "读取失败: {0}", exc)
        logger.complete()
    finally:
        logger.remove(handle)
    lines = next(tmp_path.glob("display_*.jsonl")).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert not HAN.search(record["raw"])
    language("zh_CN")
    assert "读取失败" in render_record(record)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("unchanged", encoding="utf-8")
    assert install_metadata_sink(logger, str(blocker)) is None
    assert blocker.read_text(encoding="utf-8") == "unchanged"


def test_live_localized_signal_does_not_duplicate_legacy_channel(language):
    from fluentytdl.utils.log_signal_handler import log_signal_handler

    language("en_US")
    logger.complete()
    log_signal_handler._drain()
    display, legacy, events = [], [], []
    on_display = display.append

    def on_legacy(*row):
        legacy.append(row)

    on_event = events.append
    log_signal_handler.display_record_received.connect(on_display)
    log_signal_handler.log_received.connect(on_legacy)
    log_signal_handler.event_received.connect(on_event)
    sink = logger.add(log_signal_handler._emit_log)
    try:
        log_text(logger.bind(fytdl={"kind": "test"}), "info", "读取失败: {0}", "sample")
        log_signal_handler._drain()
        assert len(display) == 1 and not legacy
        assert events[0]["kind"] == "test"
        logger.info("third-party raw text")
        log_signal_handler._drain()
        assert len(display) == 1 and len(legacy) == 1
        assert legacy[0][3] == "third-party raw text"
    finally:
        logger.remove(sink)
        log_signal_handler.display_record_received.disconnect(on_display)
        log_signal_handler.log_received.disconnect(on_legacy)
        log_signal_handler.event_received.disconnect(on_event)


def test_audit_checks_static_context_and_source():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/i18n_audit.py")],
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_audit_does_not_hide_unmarked_parameters(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from i18n_audit import inspect_sources

    directory = tmp_path / "src/fluentytdl"
    directory.mkdir(parents=True)
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    (directory / "example.py").write_text(
        'log_text(logger, "info", "模板 {0}", "裸中文参数")\ntr_text("模板 {0}".format(3))\n',
        encoding="utf-8",
    )
    marked, unmarked, errors = inspect_sources(tmp_path)
    assert marked[0]["source"] == "模板 {0}"
    assert any(entry["source"] == "裸中文参数" for entry in unmarked)
    assert any("formatted source" in error for error in errors)


@pytest.mark.parametrize("english,valid", [("Retry {0}", True), ("Retry {1}", False), ("", False)])
def test_audit_validates_independent_bilingual_catalog(tmp_path, english, valid):
    sys.path.insert(0, str(ROOT / "scripts"))
    from i18n_audit import inspect_sources

    directory = tmp_path / "src/fluentytdl/utils"
    directory.mkdir(parents=True)
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    (directory / "control_center_text.py").write_text(
        f'MESSAGES = {{"retry": ("重试 {{0}}", {english!r})}}\nother = "仍然检查裸中文"\n',
        encoding="utf-8",
    )
    marked, unmarked, errors = inspect_sources(tmp_path)
    assert bool(errors) is not valid
    assert bool(marked) is valid
    assert any(entry["source"] == "仍然检查裸中文" for entry in unmarked)


@pytest.mark.parametrize(
    "saved,expected_en,expected_zh",
    [
        ("🎬 最佳画质", "🎬 Best Quality MKV", "🎬 最佳画质 MKV"),
        ("📺 1080p 高清", "📺 1080p HD MKV", "📺 1080p 高清 MKV"),
        ("🎬 Best Video Quality", "🎬 Best Quality MKV", "🎬 最佳画质 MKV"),
        ("📺 1080p HD (MP4)", "📺 1080p HD (MP4) MKV", "📺 1080p 高清 (MP4) MKV"),
        ("最佳画质(原盘)", "Best Quality (Source Codecs) MKV", "最佳画质（原始编码） MKV"),
        ("Best Quality (Raw)", "Best Quality (Source Codecs) MKV", "最佳画质（原始编码） MKV"),
        ("最佳画质（原始编码）", "Best Quality (Source Codecs) MKV", "最佳画质（原始编码） MKV"),
        ("📺 720p SD", "📺 720p HD MKV", "📺 720p 高清 MKV"),
        ("📺 720p 高清", "📺 720p HD MKV", "📺 720p 高清 MKV"),
        ("Pure Audio", "Audio Only MKV", "纯音频 MKV"),
        ("[全局] 最佳音质", "[Global] Best Audio Quality MKV", "[全局] 最佳音质 MKV"),
        ("自定义最佳画质说明", "自定义最佳画质说明 MKV", "自定义最佳画质说明 MKV"),
    ],
)
def test_saved_task_preset_labels_follow_language_without_mutating_data(
    language, saved, expected_en, expected_zh
):
    from fluentytdl.ui.models.task_row import TaskRow
    from fluentytdl.utils.formatters import derive_format_note

    opts = {"__fluentytdl_format_note": saved, "format": "bestvideo+bestaudio"}
    record = {
        "id": 9,
        "title": "我的最佳画质视频",
        "state": "completed",
        "ydl_opts_json": json.dumps(opts),
        "output_path": "C:/videos/sample.mkv",
    }
    original = copy.deepcopy(record)
    for locale, expected in [("en_US", expected_en), ("zh_CN", expected_zh)]:
        language(locale)
        row = TaskRow.from_db_row(record)
        assert row.effective_format_note == expected
        assert derive_format_note(opts, record["output_path"]) == expected
        assert row.title == original["title"]
        assert record == original and opts["__fluentytdl_format_note"] == saved


def test_notification_history_relocalizes_without_rewriting_rows(language):
    from fluentytdl.notification import Notification, notification_center
    from fluentytdl.notification.notification_text import display_fields

    language("zh_CN")
    notification = Notification(
        type="update_available",
        title=tr_text("{0} 有新版本 {1}", "yt-dlp", "2026.09"),
        message=tr_text("{0}{1}", tr_text("队列已自动暂停"), "\n<original error>"),
    )
    identity = notification_center.push(notification)
    try:
        restored = next(row for row in notification_center.get_all() if row.id == identity)
        before = (restored.title, restored.message, copy.deepcopy(restored.metadata))
        language("en_US")
        title, body = display_fields(restored)
        assert "yt-dlp" in title and "2026.09" in title and not HAN.search(title)
        assert not HAN.search(body) and "<original error>" in body
        language("zh_CN")
        assert display_fields(restored)[0] == notification.title
        assert (restored.title, restored.message, restored.metadata) == before
    finally:
        notification_center.delete_notification(identity)


def test_legacy_notification_and_announcement_content(language):
    from fluentytdl.notification import Notification
    from fluentytdl.notification.notification_text import display_fields

    language("en_US")
    old = Notification(
        type="update_available",
        title="yt-dlp 有新版本 2026.09",
        message="yt-dlp 2026.09 已可用（当前 2026.08），前往「设置 → 组件」即可更新。",
    )
    title, body = display_fields(old)
    assert not HAN.search(title + body) and "Settings → Update" in body
    old.metadata = {"display_text": {"title": {"source": 123}}}
    assert display_fields(old)[0] == old.title
    old.type = "announcement"
    assert display_fields(old) == (old.title, old.message)


@pytest.mark.parametrize(
    "platform,name,zh",
    [
        ("x", "X 默认账号", "X 默认账号"),
        ("youtube", "YouTube 默认账号", "YouTube 默认账号"),
        ("x", "X default account", "X 默认账号"),
        ("youtube", "YouTube Default Account", "YouTube 默认账号"),
        ("x", "Default", "默认账号"),
        ("x", "未命名账号", "未命名账号"),
        ("youtube", "Unnamed account", "未命名账号"),
    ],
)
def test_legacy_account_names_follow_language_without_rewriting(language, platform, name, zh):
    from fluentytdl.auth.auth_service import WebView2Account

    account = WebView2Account.from_dict(
        dict(
            account_id="existing",
            display_name=name,
            platform=platform,
            profile_dir="existing/profile",
            cached_cookie_path="existing/cookies.txt",
        )
    )
    original = account.to_dict()
    language("en_US")
    assert not HAN.search(account.localized_name)
    language("zh_CN")
    assert account.localized_name == zh
    assert account.to_dict() == original


def test_generated_account_identity_and_explicit_rename(language, monkeypatch, tmp_path):
    from fluentytdl.auth.auth_service import AuthService, WebView2Account

    service = AuthService.__new__(AuthService)
    service._webview2_accounts = {}
    service._current_webview2_account_ids = {}
    monkeypatch.setattr(service, "_save_config", lambda: None)
    monkeypatch.setattr(service, "_save_webview2_accounts", lambda: None)
    monkeypatch.setattr(
        service,
        "_build_webview2_account_paths",
        lambda identity, platform: (
            tmp_path / identity / "profile",
            tmp_path / identity / "cookies.txt",
        ),
    )
    language("zh_CN")
    service._ensure_current_webview2_account_valid("x")
    account = next(iter(service._webview2_accounts.values()))
    assert account.builtin_name == "platform_default"
    assert account.display_name == "X Default Account"
    assert account.localized_name == "X 默认账号"
    language("en_US")
    restored = WebView2Account.from_dict(json.loads(json.dumps(account.to_dict())))
    assert not HAN.search(restored.localized_name)
    service.update_webview2_account(account.account_id, display_name="X 默认账号")
    assert account.builtin_name == "" and account.localized_name == "X 默认账号"
    custom = service.create_webview2_account("我的 YouTube 默认账号", "youtube")
    assert custom.builtin_name == "" and custom.localized_name == custom.display_name
    unnamed = service.create_webview2_account(" ", "youtube")
    assert unnamed.builtin_name == "unnamed" and not HAN.search(unnamed.localized_name)
    language("zh_CN")
    assert unnamed.localized_name == "未命名账号"


def test_settings_account_selector_uses_localized_names(language, monkeypatch):
    from qfluentwidgets import FluentIcon

    from fluentytdl.auth.auth_service import WebView2Account, auth_service
    from fluentytdl.ui.components.settings.platform_auth_card import PlatformAuthExpandCard

    accounts = [
        WebView2Account("default", "X 默认账号", platform="x", is_default=True),
        WebView2Account("custom", "我的账号", platform="x", builtin_name=""),
    ]
    monkeypatch.setattr(auth_service, "list_webview2_accounts", lambda **kwargs: accounts)
    monkeypatch.setattr(auth_service, "get_current_webview2_account_id", lambda platform: "default")
    app = language("en_US")
    card = PlatformAuthExpandCard("x", FluentIcon.PEOPLE, "X", "")
    try:
        card.reload_accounts()
        assert not HAN.search(card.accountComboBox.itemText(0))
        assert card.accountComboBox.itemText(1) == "我的账号"
        assert card._account_ids == ["default", "custom"]
    finally:
        card.deleteLater()
        app.processEvents()
