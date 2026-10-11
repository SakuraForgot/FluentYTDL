"""Tests for scripts/fetch_tools.py integrity checks.

回归背景（两个真实炸过 release 的 bug）：

1. deno 的 ``*.zip.sha256sum`` 不是 GNU coreutils 格式，而是 PowerShell
   ``Get-FileHash | Format-List`` 输出。旧解析器按位置取 ``line.split()[0]``，于是
   ``Path : C:\\...\\deno-x86_64-pc-windows-msvc.zip`` 这一行命中了 GNU 分支，
   返回字面量 ``"Path"``，把一次正常发布报成"疑似供应链投毒"并中止了 release。

2. ``download_file`` 从不核对 ``Content-Length``。连接中断时 ``read()`` 返回空
   chunk、循环正常退出，半截文件被当成下完了 —— ffmpeg 因此在解压阶段炸成
   ``File is not a zip file``；而对上游无校验和的工具，半截文件会在锁文件比对时
   表现为哈希不符，同样被误报成投毒。
"""

import sys
from pathlib import Path

import pytest

# Resolve scripts/ for direct execution
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_tools  # noqa: E402
from fetch_tools import download_file, parse_upstream_sha256  # noqa: E402

DENO_HASH = "68ED08B05C56CF887E9AA509947DC3F468F7E12F47A13E5C1ABD51D46D1453EF"
YTDLP_HASH = "a" * 64


def test_pot_snapshot_lists_zip_beside_ytdlp(tmp_path, monkeypatch):
    import component_snapshot

    from fluentytdl.core import pot_upstream
    from fluentytdl.core.pot_component import PotBundle

    release = {
        "id": 1,
        "tag_name": "2.0.2",
        "assets": [
            {
                "name": pot_upstream.PLUGIN_ASSET,
                "size": 8,
                "digest": "sha256:" + "a" * 64,
                "browser_download_url": f"https://github.com/{pot_upstream.UPSTREAM_REPOSITORY}/releases/download/2.0.2/{pot_upstream.PLUGIN_ASSET}",
            }
        ],
    }

    def install(metadata, target, transport, *, deno_path, ytdlp_path, packaged):
        assert packaged
        assert deno_path == target.parent / "deno/deno.exe"
        assert ytdlp_path == target.parent / "yt-dlp/yt-dlp.exe"
        (target / "manifest.json").write_text("{}")
        bundle = PotBundle(target, {})
        bundle.plugin_root.mkdir(parents=True)
        bundle.plugin_archive.write_bytes(b"original")
        return bundle

    monkeypatch.setattr(fetch_tools, "github_api", lambda _: release)
    monkeypatch.setattr(pot_upstream, "install_release", install)
    result = component_snapshot._resolve_component(
        tmp_path,
        {},
        "pot-provider",
        "pot-provider",
        fetch_tools.fetch_pot_provider,
        ["manifest.json"],
    )
    assert set(result["files"]) == {
        "bin/pot-provider/manifest.json",
        "bin/yt-dlp/yt-dlp-plugins/bgutil-ytdlp-pot-provider.zip",
    }


# 逐字节照抄自 denoland/deno 的真实产物（含 CRLF 与首尾空行）
DENO_SHA256SUM = (
    "\r\n"
    "Algorithm : SHA256\r\n"
    f"Hash      : {DENO_HASH}\r\n"
    "Path      : C:\\a\\deno\\deno\\target\\release\\deno-x86_64-pc-windows-msvc.zip\r\n"
    "\r\n"
)


def test_powershell_format_list_is_parsed():
    """deno 格式：必须取 Hash 行，而不是 Path 行的第一个 token。"""
    got = parse_upstream_sha256(DENO_SHA256SUM, "deno-x86_64-pc-windows-msvc.zip")
    assert got == DENO_HASH


def test_powershell_format_never_returns_the_literal_path_key():
    """这就是当初炸掉 release 的那个具体症状，钉死它。"""
    got = parse_upstream_sha256(DENO_SHA256SUM, "deno-x86_64-pc-windows-msvc.zip")
    assert got is not None
    assert got.lower() != "path"
    assert len(got) == 64


def test_gnu_coreutils_multi_entry():
    """yt-dlp SHA2-256SUMS：多条目清单里要精确取到对应文件那一行。"""
    content = f"{'b' * 64}  yt-dlp\n{YTDLP_HASH}  yt-dlp.exe\n{'c' * 64}  yt-dlp_macos\n"
    assert parse_upstream_sha256(content, "yt-dlp.exe") == YTDLP_HASH


def test_gnu_coreutils_binary_mode_star_prefix():
    content = f"{YTDLP_HASH} *yt-dlp.exe\n"
    assert parse_upstream_sha256(content, "yt-dlp.exe") == YTDLP_HASH


def test_bsd_style():
    content = f"SHA256 (deno.zip) = {DENO_HASH.lower()}\n"
    assert parse_upstream_sha256(content, "deno.zip") == DENO_HASH.lower()


def test_bare_single_hash():
    assert parse_upstream_sha256(f"{DENO_HASH}\n", "anything.zip") == DENO_HASH


def test_bare_hash_rejected_when_ambiguous():
    """多个裸哈希无法判断归属，宁可报错也不能猜。"""
    content = f"{'a' * 64}\n{'b' * 64}\n"
    assert parse_upstream_sha256(content, "x.zip") is None


def test_missing_entry_returns_none():
    content = f"{YTDLP_HASH}  some-other-file.exe\n"
    assert parse_upstream_sha256(content, "yt-dlp.exe") is None


def test_non_hash_token_is_not_accepted():
    """伪造一行长得像 GNU 格式、但首 token 不是哈希的内容，必须拒绝。"""
    content = "NotAHash  deno-x86_64-pc-windows-msvc.zip\n"
    assert parse_upstream_sha256(content, "deno-x86_64-pc-windows-msvc.zip") is None


@pytest.mark.parametrize(
    "needle",
    ["deno-x86_64-pc-windows-msvc.zip", "msvc.zip"],
)
def test_path_line_suffix_matching(needle):
    assert parse_upstream_sha256(DENO_SHA256SUM, needle) == DENO_HASH


# ---------------------------------------------------------------------------
# download_file: 截断检测
# ---------------------------------------------------------------------------


class _FakeResponse:
    """最小化的 urlopen 响应替身，可指定声明长度与实际投递的字节。"""

    def __init__(self, payload: bytes, declared_length: int | None):
        self._payload = payload
        self._pos = 0
        self.headers = {}
        if declared_length is not None:
            self.headers["Content-Length"] = str(declared_length)

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._payload):
            return b""
        end = len(self._payload) if size is None or size < 0 else self._pos + size
        chunk = self._payload[self._pos : end]
        self._pos = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, responses):
    """按调用次序依次返回 responses 里的替身，并记录调用次数。"""
    calls = {"n": 0}

    def fake_urlopen(req, context=None, timeout=None):
        idx = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[idx]()

    monkeypatch.setattr(fetch_tools, "urlopen", fake_urlopen)
    return calls


def test_truncated_download_raises(monkeypatch, tmp_path):
    """声明 1000 字节只收到 400 —— 必须报"下载不完整"，而不是悄悄放行。"""
    monkeypatch.setattr(fetch_tools, "create_ssl_context", lambda: None)
    _patch_urlopen(monkeypatch, [lambda: _FakeResponse(b"x" * 400, declared_length=1000)])

    dest = tmp_path / "tool.zip"
    with pytest.raises(RuntimeError, match="下载不完整"):
        download_file("https://example.invalid/tool.zip", dest, retries=1)


def test_truncated_download_does_not_leave_partial_file(monkeypatch, tmp_path):
    """半截文件必须删掉，否则下一层会把它当成完整产物去解压/比对哈希。"""
    monkeypatch.setattr(fetch_tools, "create_ssl_context", lambda: None)
    _patch_urlopen(monkeypatch, [lambda: _FakeResponse(b"x" * 400, declared_length=1000)])

    dest = tmp_path / "tool.zip"
    with pytest.raises(RuntimeError):
        download_file("https://example.invalid/tool.zip", dest, retries=1)
    assert not dest.exists()


def test_truncated_download_is_retried_then_succeeds(monkeypatch, tmp_path):
    """第一次截断、第二次完整 —— 网络抖动应当被重试吸收。"""
    monkeypatch.setattr(fetch_tools, "create_ssl_context", lambda: None)
    calls = _patch_urlopen(
        monkeypatch,
        [
            lambda: _FakeResponse(b"x" * 400, declared_length=1000),
            lambda: _FakeResponse(b"x" * 1000, declared_length=1000),
        ],
    )

    dest = tmp_path / "tool.zip"
    download_file("https://example.invalid/tool.zip", dest, retries=3)
    assert dest.read_bytes() == b"x" * 1000
    assert calls["n"] == 2


def test_complete_download_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch_tools, "create_ssl_context", lambda: None)
    _patch_urlopen(monkeypatch, [lambda: _FakeResponse(b"y" * 1000, declared_length=1000)])

    dest = tmp_path / "tool.zip"
    download_file("https://example.invalid/tool.zip", dest, retries=1)
    assert dest.read_bytes() == b"y" * 1000


def test_empty_download_raises(monkeypatch, tmp_path):
    """0 字节且无 Content-Length —— 依然不能当成功。"""
    monkeypatch.setattr(fetch_tools, "create_ssl_context", lambda: None)
    _patch_urlopen(monkeypatch, [lambda: _FakeResponse(b"", declared_length=None)])

    dest = tmp_path / "tool.zip"
    with pytest.raises(RuntimeError, match="下载内容为空"):
        download_file("https://example.invalid/tool.zip", dest, retries=1)


def test_no_content_length_is_accepted(monkeypatch, tmp_path):
    """上游不给 Content-Length 时不能误判为截断。"""
    monkeypatch.setattr(fetch_tools, "create_ssl_context", lambda: None)
    _patch_urlopen(monkeypatch, [lambda: _FakeResponse(b"z" * 50, declared_length=None)])

    dest = tmp_path / "tool.zip"
    download_file("https://example.invalid/tool.zip", dest, retries=1)
    assert dest.read_bytes() == b"z" * 50
