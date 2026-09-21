"""Curated display values. Never mutate the inspection evidence or its report."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import PureWindowsPath

from ..utils.ui_text import tr_text

EMPTY = {"", "n/a", "none", "null", "nan", "unknown", "undefined", "not_analyzed", "—", "-"}


def clean_values(value):
    """Keep literal names (including commas); trim, normalize and deduplicate."""
    if not isinstance(value, (list, tuple)):
        value = [value]
    result, seen = [], set()
    for item in value:
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            continue
        text = unicodedata.normalize("NFC", str(item)).replace("\x00", "").strip()
        key = " ".join(text.split())
        if key.casefold() in EMPTY or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def duration(value):
    value = positive(value)
    if value is None:
        return ""
    milliseconds = round(value * 1000)
    seconds, ms = divmod(milliseconds, 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    text = f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"
    return text + (f".{ms:03}" if ms else "")


def scaled(value, units, base):
    value = positive(value)
    if value is None:
        return ""
    for unit in units:
        if value < base or unit == units[-1]:
            return f"{value:,.2f}".rstrip("0").rstrip(".") + " " + unit
        value /= base
    return ""


def date_text(value):
    values = []
    for item in clean_values(value):
        try:
            if re.fullmatch(r"\d{4}", item) and 1 <= int(item) <= 9999:
                values.append(item)
            elif re.fullmatch(r"\d{8}", item):
                values.append(date(int(item[:4]), int(item[4:6]), int(item[6:])).isoformat())
            else:
                values.append(date.fromisoformat(item).isoformat())
        except ValueError:
            pass
    return " · ".join(clean_values(values))


@dataclass
class MediaCardData:
    key: str
    title: str
    hero: str = ""
    rows: list[tuple[str, str]] = field(default_factory=list)
    body: str = ""
    wide: bool = False
    evidence: dict = field(default_factory=dict)

    def summary(self, *, include_path=True):
        return "\n".join(
            filter(
                None,
                (
                    self.title,
                    self.hero,
                    *(
                        f"{label}: {value}"
                        for label, value in self.rows
                        if include_path or label != tr_text("文件位置")
                    ),
                    self.body,
                ),
            )
        )


def present_media(result):
    """Build cards only for available data, in reading order instead of tag order."""

    def values(*keys):
        candidates = [f for f in result.fields if f["key"] in keys and f.get("value") is not None]
        containers = [f for f in candidates if f.get("scope") == "container"]
        candidates = containers or candidates
        candidates.sort(key=lambda f: f.get("source") != "native_tag")
        return clean_values([v for item in candidates for v in clean_values(item["value"])])

    def text(*keys):
        return " · ".join(values(*keys))

    def add(rows, label, value):
        if value:
            rows.append((label, value))

    def number(key):
        return next(
            (f["value"] for f in result.fields if f["key"] == key and positive(f["value"])), None
        )

    cards = []
    authors = values("authors_json")
    # Accept the structured extension even in historical reader snapshots.
    if len(authors) == 1:
        try:
            parsed = json.loads(authors[0])
            if isinstance(parsed, list):
                authors = clean_values(parsed)
        except (ValueError, TypeError):
            pass
    artist = values("artist")
    if authors:
        joined = "; ".join(authors).casefold()
        artist = [v for v in artist if v.casefold() != joined]
    authors = clean_values([*authors, *artist])
    titles = values("title")
    content = MediaCardData("content", tr_text("内容信息"), titles[0] if titles else "", wide=True)
    add(content.rows, tr_text("作者 / 艺术家"), " · ".join(authors))
    years = clean_values(
        [
            value[:4]
            for value in date_text(values("year", "release_year", "date")).split(" · ")
            if value
        ]
    )
    add(content.rows, tr_text("年份"), " · ".join(years))
    add(content.rows, tr_text("其他标题"), " · ".join(titles[1:]))
    content.evidence = {
        "fields": [
            f
            for f in result.fields
            if f["key"]
            in {"title", "artist", "authors_json", "author_role", "year", "release_year", "date"}
        ]
    }
    if content.hero or content.rows:
        cards.append(content)

    videos = [
        s
        for s in result.streams
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    audios = [s for s in result.streams if s.get("codec_type") == "audio"]
    codecs = {
        "h264": "H.264 / AVC",
        "hevc": "H.265 / HEVC",
        "vp9": "VP9",
        "av1": "AV1",
        "aac": "AAC",
        "opus": "Opus",
        "vorbis": "Vorbis",
        "flac": "FLAC",
        "mp3": "MP3",
    }
    for kind, streams in (("video", videos), ("audio", audios)):
        for ordinal, stream in enumerate(streams, 1):
            title = tr_text("视频") if kind == "video" else tr_text("音频")
            if len(streams) > 1:
                title += f" {ordinal}"
            card = MediaCardData(f"{kind}:{stream.get('index')}", title, evidence=stream)
            codec = " · ".join(clean_values(stream.get("codec_name")))
            codec = codecs.get(codec, codec)
            if kind == "video":
                width, height = positive(stream.get("width")), positive(stream.get("height"))
                card.hero = f"{int(width)} × {int(height)}" if width and height else codec
                if width and height:
                    add(card.rows, tr_text("编码"), codec)
                add(card.rows, tr_text("帧宽度"), f"{int(width)} px" if width else "")
                add(card.rows, tr_text("帧高度"), f"{int(height)} px" if height else "")
                rate = stream.get("average_rate") or stream.get("reference_rate")
                fps = positive(rate.get("fps")) if isinstance(rate, dict) else None
                if fps:
                    label = (
                        tr_text("平均帧率") if stream.get("average_rate") else tr_text("参考帧率")
                    )
                    add(card.rows, label, f"{fps:.3f}".rstrip("0").rstrip(".") + " fps")
                add(
                    card.rows,
                    tr_text("视频码率"),
                    scaled(
                        stream.get("reported_bitrate"),
                        ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"],
                        1000,
                    ),
                )
                frames = positive(stream.get("reported_frames"))
                add(card.rows, tr_text("容器报告帧数"), f"{int(frames):,}" if frames else "")
                add(
                    card.rows,
                    tr_text("色彩空间"),
                    " · ".join(clean_values(stream.get("color_space"))),
                )
                transfer = stream.get("color_transfer")
                add(
                    card.rows,
                    tr_text("动态范围"),
                    {"smpte2084": "HDR · PQ", "arib-std-b67": "HDR · HLG"}.get(transfer, ""),
                )
            else:
                card.hero = codec
                sample = positive(stream.get("sample_rate"))
                add(card.rows, tr_text("采样率"), scaled(sample, ["Hz", "kHz"], 1000))
                channels = positive(stream.get("channels"))
                layout = {"stereo": tr_text("立体声"), "mono": tr_text("单声道")}.get(
                    stream.get("channel_layout"), ""
                )
                channel_text = tr_text("{0} 声道", int(channels)) if channels else ""
                add(card.rows, tr_text("声道"), " · ".join(filter(None, (channel_text, layout))))
                add(
                    card.rows,
                    tr_text("音频码率"),
                    scaled(
                        stream.get("reported_bitrate"),
                        ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"],
                        1000,
                    ),
                )
            depth = positive(stream.get("bits_per_raw_sample")) or positive(
                stream.get("bits_per_sample")
            )
            add(card.rows, tr_text("位深"), f"{int(depth)} bit" if depth else "")
            add(card.rows, tr_text("轨道时长"), duration(stream.get("duration")))
            for key, label in (
                ("profile", tr_text("编码规格")),
                ("pix_fmt", tr_text("像素格式")),
                ("sample_fmt", tr_text("解码采样格式")),
                ("display_aspect_ratio", tr_text("画面宽高比")),
                ("sample_aspect_ratio", tr_text("像素宽高比")),
                ("color_range", tr_text("色彩范围")),
                ("color_primaries", tr_text("色彩原色")),
                ("color_transfer", tr_text("传递特性")),
                ("field_order", tr_text("扫描方式")),
            ):
                value = " · ".join(clean_values(stream.get(key)))
                if value not in {"0:1", "0:0"}:
                    add(card.rows, label, value)
            for side in stream.get("side_data_list", []):
                rotation = side.get("rotation")
                if isinstance(rotation, (int, float)) and math.isfinite(rotation):
                    add(card.rows, tr_text("旋转角度"), f"{rotation:g}°")
            tags = stream.get("tags", {})
            add(card.rows, tr_text("轨道名称"), " · ".join(clean_values(tags.get("title"))))
            language = tags.get("language")
            if language not in {"und", "zxx"}:
                add(card.rows, tr_text("语言"), " · ".join(clean_values(language)))
            if card.hero or card.rows:
                cards.append(card)

    container = text("container")
    family = {
        "mov": "MP4 / MOV",
        "matroska": "Matroska / WebM",
        "webm": "WebM",
        "mp3": "MP3",
        "flac": "FLAC",
        "ogg": "Ogg",
        "wav": "WAV",
    }
    file_card = MediaCardData(
        "file",
        tr_text("文件"),
        family.get(container.split(",")[0], container.upper()),
        evidence={"file": result.file, "fields": result.fields[:4], "path": result.path},
    )
    suffix = PureWindowsPath(result.path).suffix.lstrip(".").upper()
    add(file_card.rows, tr_text("类型"), tr_text("{0} 文件", suffix) if suffix else "")
    add(file_card.rows, tr_text("文件名"), result.file.get("name") or filename(result.path))
    add(file_card.rows, tr_text("时长"), duration(number("duration")))
    add(
        file_card.rows,
        tr_text("文件大小"),
        scaled(number("size"), ["B", "KiB", "MiB", "GiB", "TiB"], 1024),
    )
    bitrate = scaled(number("total_bitrate"), ["bit/s", "kbit/s", "Mbit/s", "Gbit/s"], 1000)
    derived = any(
        f["key"] == "total_bitrate" and f.get("source") == "derived" for f in result.fields
    )
    add(
        file_card.rows, tr_text("总平均码率（估算）") if derived else tr_text("总平均码率"), bitrate
    )
    if PureWindowsPath(result.path).is_absolute():
        add(file_card.rows, tr_text("文件位置"), str(PureWindowsPath(result.path).parent))
    modified = positive(result.file.get("mtime_ns"))
    if modified:
        try:
            add(
                file_card.rows,
                tr_text("修改日期"),
                datetime.fromtimestamp(modified / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S"),
            )
        except (ValueError, OverflowError, OSError):
            pass
    if file_card.hero or file_card.rows:
        cards.append(file_card)

    source = MediaCardData("source", tr_text("来源与版权"))
    for key, label in (("channel", tr_text("频道")), ("uploader", tr_text("上传者"))):
        add(source.rows, label, text(key))
    for key, label in (("upload_date", tr_text("上传日期")), ("release_date", tr_text("发行日期"))):
        add(source.rows, label, date_text(values(key)))
    if not values("upload_date", "release_date"):
        add(source.rows, tr_text("日期"), date_text(values("date", "year", "release_year")))
    urls, comments = values("webpage_url"), []
    for comment in values("comment"):
        if re.fullmatch(r"https?://\S+", comment):
            urls.append(comment)
        else:
            comments.append(comment)
    link_label = (
        tr_text("备注 / 来源链接")
        if any(re.fullmatch(r"https?://\S+", v) for v in values("comment"))
        else tr_text("来源链接")
    )
    add(source.rows, link_label, "\n".join(clean_values(urls)))
    add(source.rows, tr_text("版权"), text("copyright"))
    add(source.rows, tr_text("许可"), text("license"))
    source.evidence = {
        "fields": [
            f
            for f in result.fields
            if f["key"]
            in {
                "channel",
                "uploader",
                "upload_date",
                "release_date",
                "date",
                "year",
                "release_year",
                "webpage_url",
                "comment",
                "copyright",
                "license",
            }
        ]
    }
    if source.rows:
        cards.append(source)

    collection = MediaCardData("collection", tr_text("专辑与剧集"))
    for key, label in (
        ("album", tr_text("专辑")),
        ("album_artist", tr_text("专辑艺术家")),
        ("composer", tr_text("作曲")),
        ("series", tr_text("系列")),
        ("episode_id", tr_text("剧集")),
        ("track_number", tr_text("曲目")),
        ("disc_number", tr_text("碟片")),
        ("season_number", tr_text("季")),
        ("episode_number", tr_text("集")),
    ):
        add(collection.rows, label, text(key))
    if collection.rows:
        collection.evidence = {"fields": result.fields[4:]}
        cards.append(collection)

    categories = clean_values([*values("categories"), *values("genre")])
    keywords = [
        v for v in values("keywords") if v.casefold() not in {c.casefold() for c in categories}
    ]
    tags = MediaCardData(
        "tags",
        tr_text("分类与关键词"),
        wide=True,
        evidence={
            "fields": [f for f in result.fields if f["key"] in {"categories", "genre", "keywords"}]
        },
    )
    add(tags.rows, tr_text("分类"), " · ".join(categories))
    add(tags.rows, tr_text("关键词"), " · ".join(keywords))
    if tags.rows:
        cards.append(tags)
    descriptions = values("description")
    comments = [v for v in comments if v not in descriptions]
    if descriptions or comments:
        cards.append(
            MediaCardData(
                "description",
                tr_text("简介与备注"),
                body="\n\n".join([*descriptions, *comments]),
                wide=True,
                evidence={
                    "fields": [f for f in result.fields if f["key"] in {"description", "comment"}]
                },
            )
        )

    if result.chapters:
        chapters = MediaCardData(
            "chapters", tr_text("章节"), wide=True, evidence={"chapters": result.chapters}
        )
        for index, chapter in enumerate(result.chapters, 1):
            title = " · ".join(clean_values(chapter.get("tags", {}).get("title"))) or tr_text(
                "章节 {0}", index
            )
            raw_start = chapter.get("start_time")
            start = duration(raw_start)
            if re.fullmatch(r"0(?:\.0+)?", str(raw_start)):
                start = "00:00"
            if not start:
                start = tr_text("章节 {0}", index)
            add(chapters.rows, start, title)
        cards.append(chapters)
    subtitles = [s for s in result.streams if s.get("codec_type") == "subtitle"]
    if subtitles:
        card = MediaCardData("subtitles", tr_text("字幕"), evidence={"streams": subtitles})
        for index, stream in enumerate(subtitles, 1):
            tags = stream.get("tags", {})
            add(
                card.rows,
                tr_text("字幕 {0}", index),
                " · ".join(
                    clean_values(
                        [
                            tags.get("title"),
                            tags.get("language")
                            if tags.get("language") not in {"und", "zxx"}
                            else None,
                            stream.get("codec_name"),
                        ]
                    )
                ),
            )
        if card.rows:
            cards.append(card)
    attachments = [
        s
        for s in result.streams
        if s.get("codec_type") == "attachment" or s.get("disposition", {}).get("attached_pic")
    ]
    native_pictures = [t for t in result.tags if t.get("binary")]
    if attachments or native_pictures:
        card = MediaCardData(
            "attachments",
            tr_text("封面与附件"),
            evidence={"streams": attachments, "native": native_pictures},
        )
        for index, stream in enumerate(attachments, 1):
            tags = stream.get("tags", {})
            add(
                card.rows,
                tr_text("附件 {0}", index),
                " · ".join(
                    clean_values(
                        [tags.get("filename"), stream.get("codec_name"), tags.get("mimetype")]
                    )
                ),
            )
        if native_pictures and not attachments:
            add(card.rows, tr_text("嵌入图片"), str(len(native_pictures)))
        if card.rows:
            cards.append(card)
    return cards


def chroma_depth(stream):
    """Decode recognized pixel layouts, never infer depth from codec/profile."""
    pixel = str(stream.get("pix_fmt") or "").lower()
    sampling, pixel_depth = "", None
    planar = re.fullmatch(r"yuva?j?(420|422|444|440|411|410)p(?:(9|10|12|14|16)(?:le|be))?", pixel)
    if planar:
        sampling = ":".join(planar[1])
        pixel_depth = int(planar[2] or 8)
    elif pixel in {"nv12", "nv21", "nv16", "nv24", "nv42", "yuyv422", "uyvy422"}:
        sampling = {
            "nv12": "4:2:0",
            "nv21": "4:2:0",
            "nv16": "4:2:2",
            "nv24": "4:4:4",
            "nv42": "4:4:4",
            "yuyv422": "4:2:2",
            "uyvy422": "4:2:2",
        }[pixel]
        pixel_depth = 8
    elif packed := re.fullmatch(r"p(0|2|4)(10|12|16)(?:le|be)", pixel):
        sampling = {"0": "4:2:0", "2": "4:2:2", "4": "4:4:4"}[packed[1]]
        pixel_depth = int(packed[2])
    depth = (
        positive(stream.get("bits_per_raw_sample"))
        or positive(stream.get("bits_per_sample"))
        or pixel_depth
    )
    return " · ".join(filter(None, (sampling, f"{int(depth)} bit" if depth else "")))


def present_compact_media(result):
    """The default view contains only the requested audio/video properties."""
    cards = []
    for full in present_media(result):
        kind = full.key.split(":")[0]
        if kind not in {"video", "audio"}:
            continue
        stream, rows = full.evidence, dict(full.rows)
        card = MediaCardData(full.key, full.title, evidence=stream)
        if kind == "video":
            width, height = positive(stream.get("width")), positive(stream.get("height"))
            resolution = f"{int(width)} × {int(height)}" if width and height else ""
            # Gamut is described by primaries, not by the YCbCr matrix or HDR transfer.
            primaries = " · ".join(clean_values(stream.get("color_primaries")))
            gamut = {
                "bt709": "BT.709",
                "bt2020": "BT.2020",
                "smpte431": "DCI-P3",
                "smpte432": "Display P3",
            }.get(primaries, primaries)
            video_duration = duration(stream.get("duration")) or next(
                (
                    duration(f.get("value"))
                    for f in result.fields
                    if f["key"] == "duration" and positive(f.get("value"))
                ),
                "",
            )
            card.rows = [
                (tr_text("分辨率"), resolution),
                (tr_text("色度采样和位深"), chroma_depth(stream)),
                (
                    tr_text("编码格式"),
                    rows.get(tr_text("编码"), "") or (full.hero if not resolution else ""),
                ),
                (tr_text("码率"), rows.get(tr_text("视频码率"), "")),
                (tr_text("时长"), video_duration),
                (tr_text("色域"), gamut),
            ]
        else:
            card.rows = [
                (label, rows.get(label, ""))
                for label in (
                    tr_text("采样率"),
                    tr_text("声道"),
                    tr_text("音频码率"),
                    tr_text("轨道时长"),
                )
            ]
        card.rows = [(label, value) for label, value in card.rows if value]
        if card.rows:
            cards.append(card)
    return cards


def filename(path):
    return PureWindowsPath(path).name if path else ""
