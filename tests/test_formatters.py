"""`utils.formatters` 里两个被融合后的任务模型依赖的纯函数。

`derive_format_note` 是历史行画质标签的**唯一**来源 —— 没有 worker 的行只能从落库的
`ydl_opts` 反推，四条分支的优先级搞反就会让「1080p MP4」退化成「MP4」。
`format_size` 则是三份重复实现收敛后的那一份，零值占位符由参数区分。

纯函数，不碰 Qt、不碰数据库。
"""

import sys
import time
from datetime import datetime
from pathlib import Path

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.utils.formatters import (  # noqa: E402
    derive_format_note,
    format_size,
    format_time_ago,
)

# === derive_format_note ===


def test_explicit_note_wins_over_format_string():
    """解析期写好的 `__fluentytdl_format_note` 优先级最高。"""
    note = derive_format_note(
        {"__fluentytdl_format_note": "最佳画质", "format": "bv[height<=720]+ba"},
        "C:/out.mp4",
    )
    assert note == "最佳画质 MP4"


def test_preset_height_substring():
    assert derive_format_note({"format": "bv[height<=1080]+ba/b"}, "") == "1080p"
    assert derive_format_note({"format": "bestvideo[height<=720]"}, "") == "720p"
    assert derive_format_note({"format": "480p-ish"}, "") == "480p"


def test_substring_branch_precedes_regex():
    """`1080` 子串命中就不再走正则 —— 两条分支对同一个字符串都成立时结果必须一致。"""
    assert derive_format_note({"format": "bv[height<=1080]"}, "") == "1080p"


def test_regex_branch_for_custom_height():
    assert derive_format_note({"format": "bv*[height<=1440]+ba"}, "") == "1440p"
    assert derive_format_note({"format": "bv[height<=240]"}, "") == "240p"


def test_extension_only_when_nothing_else_matches():
    assert derive_format_note({"format": "bestaudio/best"}, "D:/a/b/song.m4a") == "M4A"
    assert derive_format_note({}, "D:/a/b/video.mkv") == "MKV"


def test_extension_is_appended_not_duplicated():
    assert derive_format_note({"format": "bv[height<=1080]"}, "x.mp4") == "1080p MP4"
    # 标签里已经含扩展名（大小写不敏感）就不再追加
    assert derive_format_note({"__fluentytdl_format_note": "MP4 整合流"}, "x.mp4") == "MP4 整合流"
    assert derive_format_note({"__fluentytdl_format_note": "mp4"}, "x.MP4") == "mp4"


def test_empty_inputs_yield_empty_string():
    assert derive_format_note(None, "") == ""
    assert derive_format_note({}, "") == ""
    # 没有扩展名的输出路径不该凭空造出标签
    assert derive_format_note({}, "D:/a/b/noext") == ""


def test_no_crash_on_non_string_opts():
    """`ydl_opts_json` 是用户机器上的历史数据，别假设类型。"""
    assert derive_format_note({"format": None}, "") == ""
    assert derive_format_note({"__fluentytdl_format_note": None, "format": ""}, "x.webm") == "WEBM"


# === format_size ===


def test_size_units_and_spacing():
    assert format_size(512) == "512 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1536) == "1.5 KB"
    assert format_size(5 * 1024 * 1024) == "5.0 MB"
    assert format_size(2 * 1024**3) == "2.0 GB"


def test_size_carries_past_gb():
    """旧实现的阶梯停在 GB，2TB 会显示成 `2048.0GB`。"""
    assert format_size(2 * 1024**4) == "2.0 TB"
    assert format_size(3 * 1024**5) == "3.0 PB"


def test_zero_sentinel_is_caller_supplied():
    assert format_size(0) == "-"
    assert format_size(-1) == "-"
    assert format_size(None) == "-"
    assert format_size("nope") == "-"
    # 实时速度 / 已下载量要的是 0 B
    assert format_size(0, zero="0 B") == "0 B"
    assert format_size(None, zero="0 B") == "0 B"


def test_size_accepts_numeric_strings_and_floats():
    assert format_size("2048") == "2.0 KB"
    assert format_size(1536.7) == "1.5 KB"


# === format_time_ago ===
#
# 融合后 meta 行的「完成时间」只有这一个来源：历史行没有 worker，时间基准就是
# `tasks.updated_at`。它是 delegate 每次 paint 都会走的路径，所以既要正确也要纯。


def test_time_ago_empty_for_missing_or_invalid_timestamp():
    """没有时间就返回空串，而不是 1970 年 —— 调用方按「缺项」省略这一段。"""
    assert format_time_ago(0) == ""
    assert format_time_ago(None) == ""
    assert format_time_ago("") == ""
    assert format_time_ago("nope") == ""
    assert format_time_ago(-5) == ""


def test_time_ago_relative_buckets():
    now = time.time()
    assert format_time_ago(now - 5) == "刚刚"
    assert format_time_ago(now - 59) == "刚刚"
    assert format_time_ago(now - 60) == "1 分钟前"
    assert format_time_ago(now - 3599) == "59 分钟前"
    assert format_time_ago(now - 3600) == "1 小时前"
    assert format_time_ago(now - 86399) == "23 小时前"


def test_time_ago_day_buckets():
    now = time.time()
    # 恰好跨过 24 小时是「昨天」，不是「1 天前」
    assert format_time_ago(now - 86400 - 10) == "昨天"
    assert format_time_ago(now - 2 * 86400 - 10) == "2 天前"
    assert format_time_ago(now - 29 * 86400 - 10) == "29 天前"


def test_time_ago_falls_back_to_absolute_date_after_a_month():
    ts = time.time() - 40 * 86400
    expected = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    assert format_time_ago(ts) == expected


def test_time_ago_survives_backwards_clock():
    """机器时钟被回拨过（或 DB 里的时间来自另一台机器）时不能显示「-3 分钟前」。"""
    assert format_time_ago(time.time() + 600) == "刚刚"
