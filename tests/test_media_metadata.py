"""Metadata policy, source integrity and real container round trips."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from fluentytdl.download.staging import StagingArea
from fluentytdl.models.metadata import METADATA_POLICY, MetadataPolicy
from fluentytdl.models.quick_download_params import QuickDownloadParams
from fluentytdl.processing.metadata_finalizer import finalize_metadata
from fluentytdl.processing.metadata_normalizer import normalize_metadata, page_url, valid_date
from fluentytdl.processing.metadata_policy import configure_metadata, freeze_metadata_policy
from fluentytdl.processing.metadata_process import MetadataCancelled
from fluentytdl.processing.metadata_source import SOURCE_TEMPLATE, read_source
from fluentytdl.processing.metadata_verifier import mp4_protection
from fluentytdl.utils.quick_opts import quick_params_to_opts
from fluentytdl.youtube.yt_dlp_cli import ydl_opts_to_cli_args

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get("FLUENTYTDL_TEST_COMPONENTS", str(ROOT / "assets"))) / "bin"
FFMPEG = BIN / "ffmpeg" / "ffmpeg.exe"
FFPROBE = BIN / "ffmpeg" / "ffprobe.exe"
ATOMIC_PARSLEY = BIN / "atomicparsley" / "AtomicParsley.exe"
YTDLP = Path(os.environ.get("FLUENTYTDL_TEST_YTDLP", str(BIN / "yt-dlp" / "yt-dlp.exe")))


def require_tool(path):
    if path.is_file():
        return
    message = f"Metadata integration tool required: {path}"
    if os.environ.get("FLUENTYTDL_REQUIRE_BUILD_TESTS") == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(autouse=True)
def metadata_runtime_tools(monkeypatch):
    # Exercise writers with the same snapshot used to create/probe test media.
    # Never fall through to a developer's installed tools during CI acceptance.
    def locate(name, *relative_paths):
        path = {
            "ffmpeg.exe": FFMPEG,
            "ffprobe.exe": FFPROBE,
            "AtomicParsley.exe": ATOMIC_PARSLEY,
        }[name]
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    monkeypatch.setattr("fluentytdl.processing.metadata_finalizer.locate_runtime_tool", locate)


@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_task_choice_overrides_global_and_legacy_processors(default, enabled):
    opts = {"addmetadata": enabled, "postprocessors": [{"key": "FFmpegMetadata"}]}
    freeze_metadata_policy(opts, default)
    restored = json.loads(json.dumps(opts))
    policy = configure_metadata(restored, not default)
    assert policy.enabled is enabled
    args = ydl_opts_to_cli_args(restored)
    assert "--no-embed-metadata" in args and "--embed-metadata" not in args
    assert "--add-metadata" not in args and "--no-embed-info-json" in args
    assert restored[METADATA_POLICY] == opts[METADATA_POLICY]


def test_quick_false_and_independent_chapters():
    opts = quick_params_to_opts(QuickDownloadParams(embed_metadata=False))
    assert opts["addmetadata"] is False
    opts["sponsorblock_mark"] = ["sponsor"]
    policy = configure_metadata(opts, True)
    assert not policy.enabled
    assert "--embed-chapters" in ydl_opts_to_cli_args(opts)


def test_future_policy_is_not_silently_downgraded():
    with pytest.raises(ValueError, match="unsupported_metadata_policy"):
        freeze_metadata_policy({METADATA_POLICY: {"schema_version": 2}})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20240229", "2024-02-29"),
        ("20230229", ""),
        ("2026-09-20", "2026-09-20"),
        ("2026", ""),
        ("NA", ""),
        ("20261301", ""),
        (20260920, ""),
    ],
)
def test_dates_are_calendar_validated(raw, expected):
    assert valid_date(raw) == expected


def test_ordinary_video_does_not_become_a_music_album():
    normal = normalize_metadata(
        {
            "title": "体育视频",
            "track": "背景音乐",
            "artists": ["背景音乐作者"],
            "channel": "频道",
            "categories": ["Sports"],
            "tags": ["体育", "赛事", "体育"],
            "upload_date": "20260920",
            "playlist_title": "列表",
            "playlist_index": 7,
        },
        audio_only=True,
    )
    assert normal.values["title"] == "体育视频"
    assert normal.values["artist"] == "频道"
    assert normal.values["author_role"] == "channel"
    assert normal.values["year"] == "2026"
    assert "genre" not in normal.values and "album" not in normal.values
    assert "track_number" not in normal.values
    assert json.loads(normal.values["keywords"]) == ["体育", "赛事"]


def test_music_preserves_artists_and_separate_dates():
    normal = normalize_metadata(
        {
            "media_type": "music",
            "title": "视频标题",
            "track": "曲目标题",
            "artists": ["甲,乙", "丙"],
            "release_year": 2001,
            "release_date": "20020101",
            "upload_date": "20260920",
            "description": "中文\n" * 10000,
        },
        audio_only=True,
    )
    assert normal.values["title"] == "曲目标题"
    assert json.loads(normal.values["authors_json"]) == ["甲,乙", "丙"]
    assert normal.values["year"] == "2001" and normal.sources["year"] == "release_year"
    assert normal.issues["release_year"] == "date_conflict"
    assert normal.issues["description"] == "truncated"
    assert len(normal.values["description"].encode("utf-8")) <= 16384


def test_source_urls_do_not_expose_credentials_or_signed_streams():
    assert not page_url("https://user:secret@example.com/video")
    assert not page_url("https://video.googlevideo.com/videoplayback?id=a")
    assert not page_url("https://example.com/media?signature=secret")
    assert (
        page_url("https://www.youtube.com/watch?v=abc&utm_source=foo&si=bar")
        == "https://www.youtube.com/watch?v=abc"
    )


def test_source_rejects_other_files_identities_and_broken_json(tmp_path):
    path = tmp_path / "metadata.1.jsonl"
    media = tmp_path / "own.mp4"
    row = {"id": "a", "extractor_key": "Youtube", "filepath": str(media), "title": "本条目"}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert read_source(str(path), [str(media)])["title"] == "本条目"
    assert read_source(str(path), [str(tmp_path / "other.mp4")]) is None
    path.write_text(json.dumps(row) + "\n" + json.dumps({**row, "id": "b"}), encoding="utf-8")
    assert read_source(str(path), [str(media)]) is None
    path.write_text('{"id":', encoding="utf-8")
    assert read_source(str(path), [str(media)]) is None


def command(argv):
    result = subprocess.run(
        [str(x) for x in argv],
        capture_output=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")[-3000:]
    return result.stdout


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    for tool in (FFMPEG, FFPROBE, ATOMIC_PARSLEY):
        require_tool(tool)
    folder = tmp_path_factory.mktemp("metadata-media")
    outputs = {}
    for ext, codec in [
        ("mp4", "aac"),
        ("m4a", "aac"),
        ("mp3", "libmp3lame"),
        ("flac", "flac"),
        ("opus", "libopus"),
        ("ogg", "libvorbis"),
        ("mkv", "aac"),
        ("webm", "libopus"),
    ]:
        output = folder / ("原始." + ext)
        argv = [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
        if ext in ("mp4", "mkv"):
            argv += [
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:r=25:d=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
            ]
        argv += [
            "-c:a",
            codec,
            "-metadata",
            "title=Old",
            "-metadata",
            "copyright=Preserve me",
            output,
        ]
        command(argv)
        outputs[ext] = output
    return outputs


def staged(tmp_path, source):
    area = StagingArea.create(str(tmp_path), "metadata-test")
    area.prepare_attempt(1)
    media = Path(area.payload_dir) / ("标题 100%." + source.suffix.lstrip("."))
    media.write_bytes(source.read_bytes())
    area.add_reported(str(media), "media", primary=True)
    area.seal_discovery()
    info = {
        "id": "example",
        "extractor_key": "Youtube",
        "filepath": str(media),
        "title": "测试标题 · 中文 🎵",
        "creators": ["甲,乙", "丙"],
        "upload_date": "20260920",
        "webpage_url": "https://www.youtube.com/watch?v=example",
        "description": "简介\n第二行 = # ; \\",
        "categories": ["Sports"],
        "tags": ["体育", "赛事"],
    }
    Path(area.metadata_source_path()).write_text(
        json.dumps(info, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return area, media


@pytest.mark.parametrize("ext", ["mp4", "m4a", "mp3", "flac", "opus", "ogg", "mkv", "webm"])
def test_real_container_tags_and_media_payload_unchanged(tmp_path, samples, ext):
    area, media = staged(tmp_path, samples[ext])
    before = command(
        [FFMPEG, "-v", "error", "-i", media, "-map", "0", "-c", "copy", "-f", "streamhash", "-"]
    )
    reports = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: False
    )
    assert len(reports) == 1
    assert reports[0].code == "verified", reports[0]
    assert not reports[0].missing
    after = command(
        [FFMPEG, "-v", "error", "-i", media, "-map", "0", "-c", "copy", "-f", "streamhash", "-"]
    )
    assert before == after


def test_missing_source_and_failed_writer_keep_original(tmp_path, samples, monkeypatch):
    area, media = staged(tmp_path, samples["mp4"])
    original = hashlib.sha256(media.read_bytes()).hexdigest()
    from fluentytdl.processing import metadata_finalizer as finalizer

    monkeypatch.setattr(finalizer, "write_mp4", lambda *args: Path(args[1]).write_bytes(b"invalid"))
    report = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: False
    )[0]
    assert report.missing
    assert hashlib.sha256(media.read_bytes()).hexdigest() == original
    report = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(2), lambda: False
    )[0]
    assert report.code == "source_unavailable" and report.missing == {"source"}
    assert hashlib.sha256(media.read_bytes()).hexdigest() == original


def test_cancel_never_adopts_candidate(tmp_path, samples):
    area, media = staged(tmp_path, samples["mp4"])
    original = media.read_bytes()
    with pytest.raises(MetadataCancelled):
        finalize_metadata(
            area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: True
        )
    assert media.read_bytes() == original


def test_unknown_uuid_is_preserved_by_mp4_writer(tmp_path, samples):
    import struct

    area, media = staged(tmp_path, samples["mp4"])
    payload = b"0123456789abcdef" + b"protected spatial metadata"
    with media.open("ab") as stream:
        stream.write(struct.pack(">I4s", len(payload) + 8, b"uuid") + payload)
    before = mp4_protection(str(media))
    report = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: False
    )[0]
    assert report.code == "verified", report
    assert before == mp4_protection(str(media))


@pytest.mark.parametrize("ext", ["m4a", "mp3", "flac", "opus", "mkv"])
def test_music_native_fields_roundtrip(tmp_path, samples, ext):
    area, media = staged(tmp_path, samples[ext])
    source = Path(area.metadata_source_path())
    info = json.loads(source.read_text(encoding="utf-8"))
    info.update(
        media_type="music",
        track="作品标题",
        artists=["甲,乙", "丙"],
        album="专辑",
        album_artist="专辑作者",
        genre="Rock",
        composer="作曲者",
        release_year=2001,
        track_number=3,
        disc_number=2,
        series="系列",
        episode_id="EP01",
        season_number=1,
        episode_number=5,
    )
    source.write_text(json.dumps(info), encoding="utf-8")
    report = finalize_metadata(area, MetadataPolicy(True, False), str(source), lambda: False)[0]
    assert report.code == "verified", report
    assert report.sources["year"] == "release_year"
    # Existing native tags and ffprobe aliases must survive another identical run.
    again = finalize_metadata(area, MetadataPolicy(True, False), str(source), lambda: False)[0]
    assert again.code == "verified", again


def test_cancellation_after_library_write_keeps_original(tmp_path, samples, monkeypatch):
    from fluentytdl.processing import metadata_finalizer as finalizer

    area, media = staged(tmp_path, samples["mp3"])
    original = media.read_bytes()
    cancelled = False
    real_write = finalizer.write_id3

    def cancel_after_write(*args):
        nonlocal cancelled
        real_write(*args)
        cancelled = True

    monkeypatch.setattr(finalizer, "write_id3", cancel_after_write)
    with pytest.raises(MetadataCancelled):
        finalize_metadata(
            area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: cancelled
        )
    assert media.read_bytes() == original


def test_unowned_artwork_loss_rejects_candidate(tmp_path, samples, monkeypatch):
    from mutagen.id3 import APIC, ID3

    from fluentytdl.processing import metadata_finalizer as finalizer

    area, media = staged(tmp_path, samples["mp3"])
    tags = ID3(media)
    tags.add(APIC(encoding=1, mime="image/png", type=3, desc="cover", data=b"cover bytes"))
    tags.save(media)
    original = media.read_bytes()
    real_write = finalizer.write_id3

    def drop_cover(path, values):
        real_write(path, values)
        changed = ID3(path)
        changed.delall("APIC")
        changed.save(path)

    monkeypatch.setattr(finalizer, "write_id3", drop_cover)
    report = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: False
    )[0]
    assert report.missing and report.code != "verified"
    assert media.read_bytes() == original


def test_existing_id3_v24_and_unrelated_comments_survive(tmp_path, samples):
    from mutagen.id3 import COMM, ID3, TXXX

    area, media = staged(tmp_path, samples["mp3"])
    tags = ID3(media)
    tags.add(COMM(encoding=3, lang="eng", desc="user", text=["私人备注"]))
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=["-3.00 dB"]))
    tags.save(media, v2_version=4)
    report = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: False
    )[0]
    assert report.code == "verified", report
    after = ID3(media)
    assert after.version[1] == 4 and str(after["COMM:user:eng"]) == "私人备注"
    assert str(after["TXXX:REPLAYGAIN_TRACK_GAIN"]) == "-3.00 dB"


def test_multiple_outputs_receive_their_own_role_metadata(tmp_path, samples):
    from fluentytdl.processing.metadata_writers import read_mp4

    area, video = staged(tmp_path, samples["mp4"])
    audio = Path(area.payload_dir) / "音频.m4a"
    audio.write_bytes(samples["m4a"].read_bytes())
    area.register_generated(
        str(audio),
        kind="media",
        producer="test-audio",
        parent_ids=[area.manifest.primary_media().id],
    )
    source = Path(area.metadata_source_path())
    info = json.loads(source.read_text(encoding="utf-8"))
    info.update(media_type="music", track="曲目名", artist="作品作者")
    source.write_text(json.dumps(info), encoding="utf-8")
    reports = finalize_metadata(area, MetadataPolicy(True, False), str(source), lambda: False)
    assert len(reports) == 2 and all(r.code == "verified" for r in reports)
    assert read_mp4(str(video), {"title": ""})["title"] == info["title"]
    assert read_mp4(str(audio), {"title": ""})["title"] == "曲目名"


def test_actual_ytdlp_cli_metadata_protocol(tmp_path, samples):
    require_tool(YTDLP)
    info = tmp_path / "source.json"
    info.write_text(
        json.dumps(
            {
                "id": "offline",
                "title": "离线样本",
                "extractor": "generic",
                "extractor_key": "Generic",
                "url": samples["mp4"].as_uri(),
                "ext": "mp4",
                "webpage_url": "https://example.com/video",
                "upload_date": "20260920",
                "description": "第一行\n第二行",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "输出"
    destination.mkdir()
    snapshot = tmp_path / "metadata.jsonl"
    opts = {"addmetadata": True}
    configure_metadata(opts)
    stdout = command(
        [
            YTDLP,
            "--ignore-config",
            "--enable-file-urls",
            "--load-info-json",
            info,
            "--no-simulate",
            "--newline",
            "--progress",
            "--progress-template",
            "download:TEST_PROGRESS",
            "--ffmpeg-location",
            FFMPEG.parent,
            "-P",
            destination,
            "-o",
            "%(id)s.%(ext)s",
            "--print-to-file",
            SOURCE_TEMPLATE,
            snapshot,
            *ydl_opts_to_cli_args(opts),
        ]
    )
    result = read_source(str(snapshot), [str(destination / "offline.mp4")])
    assert result and result["title"] == "离线样本" and result["upload_date"] == "20260920"
    assert result["description"] == "第一行\n第二行"
    assert "formats" not in result
    assert b"TEST_PROGRESS" in stdout


def test_video_only_mp4(tmp_path, samples):
    video = tmp_path / "video-only.mp4"
    command([FFMPEG, "-v", "error", "-i", samples["mp4"], "-map", "0:v", "-c", "copy", video])
    area, _ = staged(tmp_path / "download", video)
    report = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: False
    )[0]
    assert report.code == "verified", report


def test_faststart_mp4_with_cover_padding_and_long_description(tmp_path, samples):
    from mutagen.mp4 import MP4, MP4Cover

    # Reproduces the real download layout: moov first, cover + metadata padding,
    # followed by tags larger than that padding. AP --overWrite can fail here.
    sample = tmp_path / "padded.mp4"
    command(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            samples["mp4"],
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            sample,
        ]
    )
    cover = tmp_path / "cover.png"
    command([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=s=64x64", "-frames:v", "1", cover])
    tags = MP4(sample)
    tags["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_PNG)]
    tags.save(sample, padding=lambda _: 2000)
    area, media = staged(tmp_path / "download", sample)
    source = Path(area.metadata_source_path())
    info = json.loads(source.read_text(encoding="utf-8"))
    info.update(channel="NTT INDYCAR SERIES", creators=[], description="Long description. " * 160)
    source.write_text(json.dumps(info), encoding="utf-8")
    before = command(
        [FFMPEG, "-v", "error", "-i", media, "-map", "0", "-c", "copy", "-f", "streamhash", "-"]
    )
    report = finalize_metadata(area, MetadataPolicy(True, False), str(source), lambda: False)[0]
    assert report.code == "verified", report
    assert str(MP4(media)["©ART"][0]) == "NTT INDYCAR SERIES"
    assert MP4(media)["covr"][0] == cover.read_bytes()
    after = command(
        [FFMPEG, "-v", "error", "-i", media, "-map", "0", "-c", "copy", "-f", "streamhash", "-"]
    )
    assert before == after


@pytest.mark.parametrize("cancelled", [False, True])
def test_mp4_separate_output_failure_keeps_seed(tmp_path, monkeypatch, cancelled):
    from fluentytdl.processing import metadata_writers as writers
    from fluentytdl.processing.metadata_process import MetadataError

    area = StagingArea.create(str(tmp_path), "output-failure")
    area.prepare_attempt(1)
    seed = Path(area.payload_dir) / "original.mp4"
    seed.write_bytes(b"untouched input")
    area.add_reported(str(seed), "media", primary=True)
    area.seal_discovery()
    candidate = area.reserve_workfile("metadata", ".mp4")
    should_cancel = False

    def run(argv, cancel):
        nonlocal should_cancel
        output = Path(argv[argv.index("--output") + 1])
        assert output != seed and "--overWrite" not in argv
        output.write_bytes(b"partial output")
        if cancelled:
            should_cancel = True
        else:
            raise MetadataError("tool_failed")

    monkeypatch.setattr(writers, "run_tool", run)
    with pytest.raises(MetadataCancelled if cancelled else MetadataError):
        writers.write_mp4(
            str(seed), candidate, {"artist": "author"}, "AtomicParsley", lambda: should_cancel
        )
    assert seed.read_bytes() == b"untouched input"
    assert [a.path for a in area.manifest.kept("media")] == [str(seed)]
    assert Path(candidate).parent == Path(area.work_dir)
    area.discard()
    assert not Path(candidate).exists()


@pytest.mark.parametrize("ext", ["m4a", "mp3", "flac", "opus"])
def test_artwork_survives_text_tags(tmp_path, samples, ext):
    import base64

    from mutagen import id3
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus

    image = tmp_path / "cover.png"
    command(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16", "-frames:v", "1", image]
    )
    cover = image.read_bytes()
    area, media = staged(tmp_path / "download", samples[ext])
    if ext == "m4a":
        audio = MP4(media)
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG)]
        audio.save()
    elif ext == "mp3":
        tags = id3.ID3(media)
        tags.add(id3.APIC(encoding=3, mime="image/png", type=3, desc="cover", data=cover))
        tags.add(id3.TXXX(encoding=3, desc="OTHER_APP", text=["preserve me"]))
        tags.save(media, v2_version=4)
    else:
        picture = Picture()
        picture.type, picture.mime, picture.data = 3, "image/png", cover
        if ext == "flac":
            audio = FLAC(media)
            audio.add_picture(picture)
        else:
            audio = OggOpus(media)
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
        audio.save()
    report = finalize_metadata(
        area, MetadataPolicy(True, False), area.metadata_source_path(), lambda: False
    )[0]
    assert report.code == "verified", report
    if ext == "mp3":
        tags = id3.ID3(media)
        assert tags.version[1] == 4
        assert tags["APIC:cover"].data == cover
        assert str(tags["TXXX:OTHER_APP"]) == "preserve me"


@pytest.mark.parametrize("ext", ["mp4", "mkv"])
def test_subtitle_chapter_and_two_audio_tracks_survive(tmp_path, samples, ext):
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,700\n字幕示例\n", encoding="utf-8")
    chapters = tmp_path / "chapters.txt"
    chapters.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=700\ntitle=第一章\n",
        encoding="utf-8",
        newline="\n",
    )
    media = tmp_path / ("complex." + ext)
    command(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            samples["mp4"],
            "-i",
            subtitle,
            "-f",
            "ffmetadata",
            "-i",
            chapters,
            "-map",
            "0:v",
            "-map",
            "0:a",
            "-map",
            "0:a",
            "-map",
            "1:0",
            "-c",
            "copy",
            "-c:s",
            "mov_text" if ext == "mp4" else "srt",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=jpn",
            "-map_chapters",
            "2",
            media,
        ]
    )
    area, _ = staged(tmp_path / "download", media)
    report = finalize_metadata(
        area, MetadataPolicy(True, True), area.metadata_source_path(), lambda: False
    )[0]
    assert report.code == "verified", report


@pytest.mark.parametrize("write_failure", [False, True])
def test_worker_commits_verified_tags_or_keeps_media_with_warning(
    tmp_path, samples, monkeypatch, write_failure
):
    from fluentytdl.diagnostics import DiagnosticLineCollector
    from fluentytdl.download import workers as worker_mod
    from fluentytdl.processing.metadata_writers import read_mp4

    if write_failure:
        from fluentytdl.processing import metadata_finalizer

        monkeypatch.setattr(
            metadata_finalizer,
            "write_mp4",
            lambda source, output, *a, **kw: Path(output).write_bytes(b"bad candidate"),
        )

    class FakeExecutor:
        raw_lines = []
        diag_lines = DiagnosticLineCollector()

        def execute(self, url, opts, **callbacks):
            out = Path(opts["paths"]["home"]) / "media.mp4"
            out.write_bytes(samples["mp4"].read_bytes())
            callbacks["on_file_created"](str(out), "media")
            callbacks["on_path"](str(out))
            final = Path(callbacks["final_paths_file"])
            final.write_text(str(out) + "\n", encoding="utf-8")
            metadata_file = final.with_name(final.name.replace("final.", "metadata.")).with_suffix(
                ".jsonl"
            )
            metadata_file.write_text(
                json.dumps(
                    {
                        "id": "test",
                        "extractor_key": "Generic",
                        "filepath": str(out),
                        "title": "最终标题",
                        "upload_date": "20260920",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return str(out)

    monkeypatch.setattr(worker_mod, "DownloadExecutor", FakeExecutor)
    monkeypatch.setattr(type(worker_mod.youtube_service), "build_ydl_options", lambda *a, **kw: {})
    opts = {"format": "best", "paths": {"home": str(tmp_path)}, "addmetadata": True}
    freeze_metadata_policy(opts)
    worker = worker_mod.DownloadWorker("https://example.com/video", opts)
    worker.run()
    assert worker._run_outcome == "success"
    assert bool(worker.trace.missing) == write_failure
    output = Path(worker.output_path)
    assert output.is_file() and output.parent == tmp_path
    if write_failure:
        assert output.read_bytes() == samples["mp4"].read_bytes()
    else:
        assert read_mp4(str(output), {"title": "", "year": ""}) == {
            "title": "最终标题",
            "year": "2026",
        }
    assert not (tmp_path / ".fluent_temp").exists()
