"""SABR-only 账号级标记 → build_ydl_options 追加 web_safari 的契约。

背景：YouTube 把某些账号拉进 SABR-only 灰度后，web_creator/tv 客户端的高清格式只以
SABR 流形式返回、没有直链 URL，被 yt-dlp 丢光，只剩 360p。唯一解是**追加**（而非锁定）
web_safari 客户端，它走 HLS 仍能拿到带直链的高清档。

本文件锁死四件事：
1. `WebView2Account.sabr_only` 能跟着 accounts.json round-trip；旧配置缺键 → False；
2. `mark_youtube_sabr_only` 无账号时置内存 sticky、有账号时写账号且落盘，且幂等；
3. `_maybe_mark_sabr_only`（解析输出扫描）命中 SABR 文本才打标；
4. `build_ydl_options` 读标记后把 web_safari 追加进 player_client：无既有值 →
   `default,web_safari`；POT 已设 `default,mweb` → 追加去重成 `default,mweb,web_safari`；
   标记为 False → 一个字都不动。

所有触碰 AuthService 存储的用例都把账号存储重指到 tmp_path，**绝不**碰真实
accounts.json / 凭据。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-sabr-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.auth.auth_service import WebView2Account, auth_service  # noqa: E402

# ── 1. 账号字段 round-trip ─────────────────────────────────────


def test_account_sabr_only_roundtrips():
    acc = WebView2Account(account_id="a1", display_name="acc", sabr_only=True)
    restored = WebView2Account.from_dict(acc.to_dict())
    assert restored.sabr_only is True


def test_account_legacy_dict_missing_key_defaults_false():
    """旧 accounts.json 没有 sabr_only 键 → known-fields 过滤令其默认 False。"""
    legacy = {"account_id": "old", "display_name": "old", "platform": "youtube"}
    acc = WebView2Account.from_dict(legacy)
    assert acc.sabr_only is False


# ── 2. mark / get 语义 ────────────────────────────────────────


@pytest.fixture
def isolated_accounts(tmp_path, monkeypatch):
    """把账号存储与当前账号映射重指到 tmp_path，隔离真实凭据。"""
    monkeypatch.setattr(auth_service, "_webview2_accounts_path", tmp_path / "accounts.json")
    monkeypatch.setattr(auth_service, "_webview2_accounts", {})
    monkeypatch.setattr(auth_service, "_current_webview2_account_ids", {})
    monkeypatch.setattr(auth_service, "_session_sabr_only", False)
    return tmp_path


def test_mark_without_account_sets_session_flag(isolated_accounts):
    assert auth_service.get_youtube_sabr_only() is False
    auth_service.mark_youtube_sabr_only()
    assert auth_service.get_youtube_sabr_only() is True
    # 无账号 → 不落盘
    assert not (isolated_accounts / "accounts.json").exists()


def test_mark_with_account_persists_and_is_idempotent(isolated_accounts, monkeypatch):
    acc = WebView2Account(account_id="acc1", display_name="acc1", platform="youtube")
    auth_service._webview2_accounts["acc1"] = acc
    auth_service._current_webview2_account_ids["youtube"] = "acc1"

    saves = {"n": 0}
    real_save = auth_service._save_webview2_accounts

    def _counting_save():
        saves["n"] += 1
        real_save()

    monkeypatch.setattr(auth_service, "_save_webview2_accounts", _counting_save)

    assert auth_service.get_youtube_sabr_only() is False
    auth_service.mark_youtube_sabr_only()
    assert auth_service.get_youtube_sabr_only() is True
    assert acc.sabr_only is True
    assert saves["n"] == 1

    # 幂等：已 True 不再写盘
    auth_service.mark_youtube_sabr_only()
    assert saves["n"] == 1

    # 真落到磁盘且能读回
    written = json.loads((isolated_accounts / "accounts.json").read_text(encoding="utf-8"))
    persisted = next(a for a in written["accounts"] if a["account_id"] == "acc1")
    assert persisted["sabr_only"] is True


# ── 3. 解析输出扫描打标 ────────────────────────────────────────


def test_maybe_mark_sabr_only_triggers_on_marker(isolated_accounts):
    from fluentytdl.youtube.yt_dlp_cli import _maybe_mark_sabr_only

    out = (
        "WARNING: [youtube] X: Some tv client https formats have been skipped as "
        "they are missing a url. YouTube is forcing SABR streaming for this client."
    )
    assert auth_service.get_youtube_sabr_only() is False
    _maybe_mark_sabr_only(out)
    assert auth_service.get_youtube_sabr_only() is True


def test_maybe_mark_sabr_only_ignores_normal_output(isolated_accounts):
    from fluentytdl.youtube.yt_dlp_cli import _maybe_mark_sabr_only

    _maybe_mark_sabr_only("[youtube] X: Downloading webpage\n[info] Available formats:")
    assert auth_service.get_youtube_sabr_only() is False


# ── 4. build_ydl_options 追加 web_safari ──────────────────────


def _player_client(ydl_opts) -> str:
    ea = ydl_opts.get("extractor_args", {})
    yt = ea.get("youtube", {}) if isinstance(ea, dict) else {}
    pc = yt.get("player_client")
    if isinstance(pc, (list, tuple)) and pc:
        return ",".join(str(x) for x in pc)
    return pc if isinstance(pc, str) else ""


def test_append_web_safari_when_flagged(isolated_accounts):
    from fluentytdl.youtube.youtube_service import youtube_service

    ydl_opts = {}
    auth_service.mark_youtube_sabr_only()
    youtube_service._maybe_append_sabr_web_safari(ydl_opts)
    assert _player_client(ydl_opts) == "default,web_safari"


def test_append_web_safari_dedupes_and_preserves_pot_client(isolated_accounts):
    from fluentytdl.youtube.youtube_service import youtube_service

    # 模拟 POT 分支已设 default,mweb
    ydl_opts = {"extractor_args": {"youtube": {"player_client": ["default,mweb"]}}}
    auth_service.mark_youtube_sabr_only()
    youtube_service._maybe_append_sabr_web_safari(ydl_opts)
    assert _player_client(ydl_opts) == "default,mweb,web_safari"

    # 再跑一次不重复追加
    youtube_service._maybe_append_sabr_web_safari(ydl_opts)
    assert _player_client(ydl_opts) == "default,mweb,web_safari"


def test_no_append_when_not_flagged(isolated_accounts):
    from fluentytdl.youtube.youtube_service import youtube_service

    ydl_opts = {}
    youtube_service._maybe_append_sabr_web_safari(ydl_opts)
    assert _player_client(ydl_opts) == ""
