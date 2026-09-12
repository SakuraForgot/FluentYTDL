"""显示层格式化函数的唯一归口。

字节格式化在树里曾有三份实现（历史卡片 `1.5 MB`、下载卡片 `1.50 MiB`、这里的
`1.5MB`），三份的零值占位符、进制、小数位、空格全都不同。融合后同一个 meta 行会同时
渲染活任务与历史行，靠调用方各自带一份必然出现「同一列两种写法」，所以收敛到
`format_size` 一处，零值占位符由参数区分而不是由副本区分。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP

from fluentytdl.utils.message_catalog import english
from fluentytdl.utils.ui_text import tr_text


def format_duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except Exception:
        return "--:--"
    if s < 0:
        return "--:--"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def format_upload_date(value: Any) -> str:
    s = str(value or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s or "-"


def format_time_ago(ts: Any) -> str:
    """时间戳 → 「刚刚 / 3 分钟前 / 昨天 / 2026-08-17」。

    搬自 `history_item_widget._format_time_ago`（该文件在融合收尾阶段整体删除）。
    融合后同一条 meta 行要给「历史行」显示完成时间，delegate 每次 paint 都会走这里，
    所以保持纯函数、不碰 Qt：`tr()` 需要 QObject，而 delegate 的 meta 行同时还要
    渲染 CleanLogger 直接产出的中文串，两边混用 tr 只会让译文半边生效。
    """
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""

    diff = time.time() - t
    if diff < 0:
        # 机器时钟回拨过，别显示「-3 分钟前」
        return tr_text("刚刚")
    if diff < 60:
        return tr_text("刚刚")
    if diff < 3600:
        return tr_text("{0} 分钟前", int(diff // 60))
    if diff < 86400:
        return tr_text("{0} 小时前", int(diff // 3600))

    days = int(diff // 86400)
    if days == 1:
        return tr_text("昨天")
    if days < 30:
        return tr_text("{0} 天前", days)
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def format_size(value: Any, zero: str = "-") -> str:
    """字节数 → 人读字符串，十进制记法带空格（`512 B` / `48.3 KB` / `1.5 MB`）。

    `zero` 是「没有有效数值」时的占位符。meta 行要的是 `-`（一个已完成但没记下大小的
    任务写 `0 B` 会被当成「文件是空的」），而实时速度/已下载量要的是 `0 B`，所以由调用方
    传，不再各自复制一份实现。

    单位阶梯到 PB 为止且**最后一档不再截断**：旧实现的阶梯停在 GB，2TB 会显示成
    `2048.0GB`。
    """
    try:
        x = float(value)
    except (TypeError, ValueError):
        return zero
    if x <= 0:
        return zero
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{int(round(x))} {u}" if u == "B" else f"{x:.1f} {u}"
        x /= 1024
    return f"{x:.1f} {units[-1]}"


# 「height<=1080」这类 format 选择器里的高度上限。写在模块级是因为它会被每一行历史
# 记录走一遍（分页每次 100 行），re 的内部缓存靠不住（有大小上限且会被其他模式挤掉）。
_HEIGHT_LIMIT_RE = re.compile(r"height<=(\d+)")

# Old tasks persist the translated preset title, sometimes with an icon/container.
# Match complete built-in labels only; arbitrary format notes must remain untouched.
_SAVED_PRESET_LABELS = (
    QT_TRANSLATE_NOOP("RuntimeText", "最佳画质"),
    QT_TRANSLATE_NOOP("RuntimeText", "最佳画质 (原盘)"),
    QT_TRANSLATE_NOOP("RuntimeText", "1080p 高清"),
    QT_TRANSLATE_NOOP("RuntimeText", "720p 标清"),
    QT_TRANSLATE_NOOP("RuntimeText", "最佳画质 (无音频)"),
    QT_TRANSLATE_NOOP("RuntimeText", "1080p视频 (无音频)"),
    QT_TRANSLATE_NOOP("RuntimeText", "最佳质量(无声)"),
    QT_TRANSLATE_NOOP("RuntimeText", "1080p(无声)"),
    QT_TRANSLATE_NOOP("RuntimeText", "最佳音质"),
    QT_TRANSLATE_NOOP("RuntimeText", "高品质音频"),
    QT_TRANSLATE_NOOP("RuntimeText", "标准音频"),
    QT_TRANSLATE_NOOP("RuntimeText", "纯音频"),
    QT_TRANSLATE_NOOP("RuntimeText", "自定义音频"),
    QT_TRANSLATE_NOOP("RuntimeText", "全局格式"),
    QT_TRANSLATE_NOOP("RuntimeText", "高品质 (320kbps)"),
    QT_TRANSLATE_NOOP("RuntimeText", "标准品质 (192kbps)"),
    QT_TRANSLATE_NOOP("RuntimeText", "纯音频 (MP3 - 320k)"),
    QT_TRANSLATE_NOOP("RuntimeText", "720p 高清"),
    QT_TRANSLATE_NOOP("RuntimeText", "最佳画质（原始编码）"),
    QT_TRANSLATE_NOOP("RuntimeText", "最佳画质（仅视频）"),
    QT_TRANSLATE_NOOP("RuntimeText", "1080p（仅视频）"),
    QT_TRANSLATE_NOOP("RuntimeText", "音频码率 320 kbps"),
    QT_TRANSLATE_NOOP("RuntimeText", "音频码率 192 kbps"),
)
# Historical English labels are data aliases, not current UI wording.
_LEGACY_PRESET_ENGLISH = {
    "Best Quality (Original)": QT_TRANSLATE_NOOP("RuntimeText", "最佳画质 (原盘)"),
    "Best Quality (Raw)": QT_TRANSLATE_NOOP("RuntimeText", "最佳画质 (原盘)"),
    "720p SD": QT_TRANSLATE_NOOP("RuntimeText", "720p 标清"),
    "Best Quality (Silent)": QT_TRANSLATE_NOOP("RuntimeText", "最佳质量(无声)"),
    "1080p (Silent)": QT_TRANSLATE_NOOP("RuntimeText", "1080p(无声)"),
    "1080p Video (No Audio)": QT_TRANSLATE_NOOP("RuntimeText", "1080p视频 (无音频)"),
    "High Quality (320kbps)": QT_TRANSLATE_NOOP("RuntimeText", "高品质 (320kbps)"),
    "Standard Quality (192kbps)": QT_TRANSLATE_NOOP("RuntimeText", "标准品质 (192kbps)"),
    "Pure Audio": QT_TRANSLATE_NOOP("RuntimeText", "纯音频"),
    "Pure Audio (MP3 - 320k)": QT_TRANSLATE_NOOP("RuntimeText", "纯音频 (MP3 - 320k)"),
}
_GLOBAL_NOTE_PREFIX = QT_TRANSLATE_NOOP("RuntimeText", "[全局] ")
_NOTE_CONTAINER_RE = re.compile(r"(\s+\(?(?:MP4|MKV|WEBM|M4A|MP3|FLAC|OPUS|WAV|OGG)\)?)$", re.I)


def _localize_saved_format_note(note: str) -> str:
    original = note
    global_note = False
    for prefix in (_GLOBAL_NOTE_PREFIX, english(_GLOBAL_NOTE_PREFIX)):
        if note.startswith(prefix):
            note = note[len(prefix) :]
            global_note = True
            break
    icon = re.match(r"^[🎬🎯📺🎵🎧🔊\ufe0f\s]+", note)
    prefix = icon.group() if icon else ""
    note = note[len(prefix) :]
    container = _NOTE_CONTAINER_RE.search(note)
    suffix = container.group() if container else ""
    label = note[: len(note) - len(suffix)] if suffix else note
    if label == "Best Video Quality":
        label = "Best Quality"
    label = _LEGACY_PRESET_ENGLISH.get(label, label)

    def key(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    for source in _SAVED_PRESET_LABELS:
        if key(label) in {key(source), key(english(source))}:
            result = prefix + tr_text(source) + suffix
            return tr_text(_GLOBAL_NOTE_PREFIX) + result if global_note else result
    return original


def derive_format_note(ydl_opts: Mapping[str, Any] | None, output_path: str = "") -> str:
    """从任务的 `ydl_opts` + 输出路径推导「1080p MP4」这类画质标签。

    逻辑整体搬自 `history_service._convert_row_to_record`（历史页唯一的推导来源），
    抽出来是为了让融合后的任务模型能直接复用：**历史行没有 worker，画质标签只能从
    落库的 opts 里反推**。四条分支按优先级：

    1. `__fluentytdl_format_note` —— 解析期就写好的权威值（内部键，yt-dlp 不认，
       仅用于在 DB 里留痕）；
    2. `format` 里含 `1080` / `720` / `480` 子串 —— 预设档位；
    3. `format` 里的 `height<=NNN` —— 自定义上限；
    4. 都没有就只剩容器扩展名。

    扩展名总是追加（已经出现在标签里就不重复），因为 `1080p` 不说明是 MP4 还是 MKV。
    """
    opts: Mapping[str, Any] = ydl_opts or {}
    note = _localize_saved_format_note(str(opts.get("__fluentytdl_format_note") or ""))

    if not note:
        fmt = str(opts.get("format") or "")
        if "1080" in fmt:
            note = "1080p"
        elif "720" in fmt:
            note = "720p"
        elif "480" in fmt:
            note = "480p"
        else:
            m = _HEIGHT_LIMIT_RE.search(fmt)
            if m:
                note = f"{m.group(1)}p"

    ext = os.path.splitext(output_path or "")[1].lstrip(".").upper()
    if ext and ext.lower() not in note.lower():
        note = f"{note} {ext}".strip() if note else ext
    return note
