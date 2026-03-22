from typing import Any


def check_is_vr_content(info: dict[str, Any]) -> bool:
    """Check if the provided video info describes VR content."""
    # 1. Check projection field
    proj = str(info.get("projection") or "").lower()
    if proj in ("equirectangular", "mesh", "360", "vr180"):
        return True

    # 2. Check tags
    tags = [str(t).lower() for t in (info.get("tags") or [])]
    if any(k in tags for k in ("360 video", "vr video", "360°", "vr180")):
        return True

    # 3. Check title
    title = str(info.get("title") or "").lower()
    keywords = (
        "360 video",
        "360 movie",
        "vr 360",
        "360°",
        "vr180",
        "180 vr",
        "3d 180",
        "3d 360",
    )
    if any(k in title for k in keywords):
        return True

    return False
