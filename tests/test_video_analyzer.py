"""
Unit tests for core.video_analyzer.check_is_vr_content

Run with:
    python -m pytest tests/test_video_analyzer.py -v
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from fluentytdl.core.video_analyzer import check_is_vr_content

# ── Projection field ─────────────────────────────────────────────────────────


def test_vr_projection_equirectangular():
    assert check_is_vr_content({"projection": "equirectangular"}) is True


def test_vr_projection_mesh():
    assert check_is_vr_content({"projection": "mesh"}) is True


def test_vr_projection_360():
    assert check_is_vr_content({"projection": "360"}) is True


def test_vr_projection_vr180():
    assert check_is_vr_content({"projection": "vr180"}) is True


def test_vr_projection_case_insensitive():
    assert check_is_vr_content({"projection": "EQUIRECTANGULAR"}) is True


def test_normal_projection():
    assert check_is_vr_content({"projection": "rectangular"}) is False


# ── Tags field ───────────────────────────────────────────────────────────────


def test_vr_tag_360_video():
    assert check_is_vr_content({"tags": ["360 video", "travel"]}) is True


def test_vr_tag_vr_video():
    assert check_is_vr_content({"tags": ["VR Video"]}) is True


def test_vr_tag_vr180():
    assert check_is_vr_content({"tags": ["vr180", "landscape"]}) is True


def test_normal_tags():
    assert check_is_vr_content({"tags": ["travel", "vlog", "4k"]}) is False


def test_empty_tags():
    assert check_is_vr_content({"tags": []}) is False


# ── Title field ──────────────────────────────────────────────────────────────


def test_vr_title_360_video():
    assert check_is_vr_content({"title": "Amazing 360 Video of Tokyo"}) is True


def test_vr_title_vr_360():
    assert check_is_vr_content({"title": "Nature VR 360 Experience"}) is True


def test_vr_title_vr180():
    assert check_is_vr_content({"title": "Deep Sea VR180"}) is True


def test_vr_title_360_degree_symbol():
    assert check_is_vr_content({"title": "Live Concert 360°"}) is True


def test_vr_title_3d_180():
    assert check_is_vr_content({"title": "Relaxing Forest 3D 180"}) is True


def test_vr_title_3d_360():
    assert check_is_vr_content({"title": "Best of Tokyo 3D 360 tour"}) is True


def test_normal_title():
    assert check_is_vr_content({"title": "How to make pasta | CookingChannel"}) is False


# ── Edge / combined cases ────────────────────────────────────────────────────


def test_empty_dict():
    assert check_is_vr_content({}) is False


def test_none_values():
    assert check_is_vr_content({"projection": None, "tags": None, "title": None}) is False


def test_vr_by_projection_overrides_normal_title():
    """Projection match returns True even if the title looks normal."""
    assert (
        check_is_vr_content(
            {
                "projection": "equirectangular",
                "title": "Just a cooking video",
            }
        )
        is True
    )


def test_normal_content_all_fields():
    """All fields present but nothing triggers VR detection."""
    assert (
        check_is_vr_content(
            {
                "projection": "rectangular",
                "tags": ["tutorial", "python"],
                "title": "Python for Beginners",
            }
        )
        is False
    )
