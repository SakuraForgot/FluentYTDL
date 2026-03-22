from typing import Any


def infer_entry_thumbnail(entry: dict[str, Any]) -> str:
    """推断视频条目的缩略图 URL，优先使用中等质量以加速加载"""
    thumb = str(entry.get("thumbnail") or "").strip()

    # 尝试从 thumbnails 列表中找到合适尺寸的缩略图
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        # 优先选择中等质量（~320x180），避免加载过大的图片
        preferred_ids = {"mqdefault", "medium", "default", "sddefault", "hqdefault"}
        for t in thumbs:
            if not isinstance(t, dict):
                continue
            t_id = str(t.get("id") or "").lower()
            if t_id in preferred_ids:
                u = str(t.get("url") or "").strip()
                if u:
                    return u

        # 如果没有找到首选，选择宽度在 200-400 之间的
        for t in thumbs:
            if not isinstance(t, dict):
                continue
            w = t.get("width") or 0
            if 200 <= w <= 400:
                u = str(t.get("url") or "").strip()
                if u:
                    return u

        # 最后回退到第一个可用的
        for t in thumbs:
            if not isinstance(t, dict):
                continue
            u = str(t.get("url") or t.get("src") or "").strip()
            if u:
                return u

    # 如果有直接的 thumbnail 字段，尝试转换为中等质量
    if thumb:
        # YouTube URL 优化：maxresdefault/hqdefault -> mqdefault
        if "i.ytimg.com" in thumb or "i9.ytimg.com" in thumb:
            for high_res in ["maxresdefault", "hqdefault", "sddefault"]:
                if high_res in thumb:
                    return thumb.replace(high_res, "mqdefault")
        return thumb

    return ""


def infer_entry_url(entry: dict[str, Any]) -> str:
    # Prefer webpage_url / original_url over url.
    for key in ("webpage_url", "original_url"):
        val = str(entry.get(key) or "").strip()
        if val.startswith("http://") or val.startswith("https://"):
            return val

    url = str(entry.get("url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    vid = str(entry.get("id") or url).strip()
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return url
