#!/usr/bin/env python3
"""
FluentYTDL 外部工具下载脚本

从 GitHub Releases 获取每个工具的**最新**发布：
yt-dlp / ffmpeg / deno / AtomicParsley / POT Provider。

完整性保障：

1. **上游校验和**（能拿到的就必须过）
   - yt-dlp 提供 ``SHA2-256SUMS``（GNU coreutils 格式）
   - deno 提供 ``*.zip.sha256sum``（PowerShell ``Get-FileHash | Format-List`` 格式，
     不是 GNU 格式 —— 解析器必须认 ``Hash :`` 行，见 ``parse_upstream_sha256``）
   校验失败一律硬失败 —— "有校验但失败只警告" 等于没有校验。
   ffmpeg-builds / AtomicParsley / POT Provider 上游不发布校验文件。

2. **GitHub 资产 digest**（size + ``sha256:`` digest）由 ``component_snapshot.ReleaseSource``
   对每个下载的资产逐一比对，挡住"同一个滚动地址、不同的字节"。

``scripts/TOOLS.lock.json`` 现在只是**只读的漂移基线**：``component_snapshot`` 拿它与本次
解析出的最新版本逐工具比对，产出 new-component / version-change /
same-version-content-change / unchanged 分类，供审计。它不再是构建门禁，本脚本也不回写它。

真正的抓取入口是 ``component_snapshot.prepare()`` —— 每个工具独立解析、并发下载到一个
全新的快照目录，源码树里任何受控文件都不动。

用法:
    python scripts/fetch_tools.py                    # 抓取最新组件到新快照目录
    python scripts/fetch_tools.py --destination DIR  # 指定空的目标目录
    python scripts/fetch_tools.py --force            # 兼容别名：始终抓取最新
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hashing import sha256_file

# 修复 Windows 控制台 GBK/CP1252 编码问题
# 确保可以正确输出 UTF-8 字符（包括中文和 emoji）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    except Exception:
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
TOOLS_LOCK = ROOT / "scripts" / "TOOLS.lock.json"

# 组件 → GitHub 仓库的唯一事实源（最低层，供 component_snapshot / generate_manifest /
# prepare_7zip import，避免各处手抄一份慢慢对不上）。src/ 里 update_transport.py 的
# nightly/master 表和 collect_licenses.py 的许可证来源是**故意不同**的，不并入这里。
REPOSITORIES = {
    "yt-dlp": "yt-dlp/yt-dlp",
    "ffmpeg": "yt-dlp/FFmpeg-Builds",
    "deno": "denoland/deno",
    "atomicparsley": "wez/atomicparsley",
    "pot-provider": "jim60105/bgutil-ytdlp-pot-provider-rs",
    "7zip": "ip7z/7zip",
}


# ============================================================================
# 网络工具
# ============================================================================


def create_ssl_context() -> ssl.SSLContext:
    """创建 SSL 上下文"""
    ctx = ssl.create_default_context()
    return ctx


def download_file(
    url: str,
    dest: Path,
    chunk_size: int = 8192,
    timeout: int = 60,
    retries: int = 3,
) -> None:
    """下载文件并显示进度。

    下完必须核对 ``Content-Length``：连接中断时 ``resp.read()`` 返回空 chunk，
    循环正常退出，磁盘上留下一个"看起来下完了"的半截文件。之前 ffmpeg 就这么
    栽过 —— 截断的 zip 一路飘到解压才炸成 ``File is not a zip file``；而对于
    ffmpeg / AtomicParsley / POT Provider 这类上游不发布校验和的工具，
    半截文件只会在锁文件比对时表现为哈希不符，被报成"疑似供应链投毒"。
    网络抖动不该长成投毒的样子，所以在这里就地判定并重试。
    """
    ctx = create_ssl_context()
    req = Request(url, headers={"User-Agent": "FluentYTDL-Builder/1.0"})

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        suffix = "" if attempt == 1 else f"  (第 {attempt}/{retries} 次尝试)"
        print(f"  📥 下载: {url}{suffix}")
        try:
            with urlopen(req, context=ctx, timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0

                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded * 100 // total
                            bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                            print(
                                f"\r  [{bar}] {pct}% ({downloaded:,}/{total:,} bytes)",
                                end="",
                                flush=True,
                            )

            print()  # 换行

            if total and downloaded != total:
                raise RuntimeError(
                    f"下载不完整: 期望 {total:,} 字节，实际收到 {downloaded:,} 字节"
                    f"（缺 {total - downloaded:,}）"
                )
            if downloaded == 0:
                raise RuntimeError("下载内容为空")
            return

        except (HTTPError, URLError, RuntimeError, TimeoutError, OSError) as e:
            last_error = e
            dest.unlink(missing_ok=True)  # 别把半截文件留给下一层去误判
            if attempt < retries:
                print(f"  ⚠ {e} —— 重试中...")

    raise RuntimeError(f"下载失败（已重试 {retries} 次）: {url} - {last_error}") from last_error


def asset_meta(url: str, path: Path) -> dict:
    """记录**被下载的那个资产**的 url / sha256 / size。

    锁文件原本只记解压后文件的哈希（deno 记的是 ``deno.exe`` 的，不是 ``.zip`` 的），
    但运行时更新下载的是资产本身，`update-manifest.json` 需要的也是资产的哈希 ——
    拿解压后的哈希去校验压缩包，每一次下载都会被判成"文件校验失败"。

    这里记的是**实际下载用的那个 URL**，多数上游是 ``releases/latest/download/...``
    这种滚动地址。运行时不会因此装错版本：`dependency_manager._overlay_manifest()`
    只在清单版本与 API 查到的最新版本**精确相等**时才采用清单的 url/sha256，版本一动
    就整体退回 API 解析。剩下的窗口只有"查完版本、下载途中上游正好发新版"，那种情况
    表现为一次 sha256 不符的失败，重试即可，不会静默装上不对的东西。
    """
    return {"url": url, "sha256": sha256_file(path), "size": path.stat().st_size}


def assert_sha256(file_path: Path, expected_hash: str, source: str) -> None:
    """校验文件 SHA256，不一致直接抛错。

    历史上这里失败只打一行 warning 就继续，等于把校验退化成装饰。
    """
    actual = sha256_file(file_path)
    expected = expected_hash.strip().lower()
    if actual != expected:
        raise RuntimeError(
            f"{file_path.name} SHA256 校验失败（来源: {source}）\n"
            f"  期望: {expected}\n"
            f"  实际: {actual}\n"
            f"  下载可能被中间人篡改或上游产物已变更，构建中止。"
        )
    print(f"  ✓ 上游校验通过 ({actual[:16]}… / {source})")


HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# BSD/OpenSSL 风格: ``SHA256 (filename) = <hash>``
BSD_SUM_RE = re.compile(r"^\s*SHA256\s*\((?P<name>.+?)\)\s*=\s*(?P<hash>[0-9a-fA-F]{64})\s*$")


def _basename(path_value: str) -> str:
    """取路径末段，同时容忍 Windows 反斜杠（上游校验文件里就是这种）。"""
    return path_value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def parse_upstream_sha256(content: str, needle: str) -> str | None:
    """从上游校验和文件内容里取出 needle 对应的哈希，取不到返回 None。

    上游的格式并不统一，实测有三种，必须全部认：

    1. GNU coreutils（yt-dlp ``SHA2-256SUMS``）::

           <64位十六进制>  yt-dlp.exe

    2. PowerShell ``Get-FileHash | Format-List``（deno ``*.zip.sha256sum``）::

           Algorithm : SHA256
           Hash      : 68ED08B0...
           Path      : C:\\a\\deno\\...\\deno-x86_64-pc-windows-msvc.zip

    3. BSD/OpenSSL::

           SHA256 (deno.zip) = 68ed08b0...

    **任何分支都必须先确认取到的 token 真的是 64 位十六进制再返回。**
    早先的实现只按位置取 ``line.split()[0]``，于是 deno 的 ``Path :`` 行
    （末段正好以 needle 结尾）会命中 GNU 分支并返回字面量 ``"Path"``，
    把一次正常发布变成"校验失败，疑似投毒"的假警报。
    """
    lines = content.splitlines()

    # 1) PowerShell Format-List —— 放最前面，它的 Path 行最容易被其他分支误读
    ps_hash: str | None = None
    ps_name_matched = False
    for line in lines:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "hash" and HEX64_RE.match(value):
            ps_hash = value
        elif key == "path" and _basename(value).endswith(needle):
            ps_name_matched = True
    if ps_hash and ps_name_matched:
        return ps_hash

    # 2) BSD/OpenSSL
    for line in lines:
        m = BSD_SUM_RE.match(line)
        if m and _basename(m.group("name")).endswith(needle):
            return m.group("hash")

    # 3) GNU coreutils —— parts[0] 必须是哈希，'*' 前缀是 binary mode 标记
    for line in lines:
        parts = line.split()
        if (
            len(parts) >= 2
            and HEX64_RE.match(parts[0])
            and _basename(parts[-1].lstrip("*")).endswith(needle)
        ):
            return parts[0]

    # 4) 整个文件就一个裸哈希、不带文件名。放最后，避免在多条目清单里误取。
    bare = [line.strip() for line in lines if HEX64_RE.match(line.strip())]
    if len(bare) == 1:
        return bare[0]

    return None


def fetch_upstream_sha256(url: str, needle: str, tmp_dir: Path, *, download=download_file) -> str:
    """下载上游校验和文件并取出 needle 对应的哈希。

    上游明确提供了校验文件时，拿不到它本身就是异常信号，不做静默降级。

    ``download`` 默认走模块级 ``download_file``（standalone 抓取）；
    ``component_snapshot`` 注入 ``ReleaseSource.download`` 后，连校验文件也经过
    "GitHub 资产 digest 校验 + 记录到 downloads" 这条路，与被校验的资产同源。
    """
    checksum_path = tmp_dir / "upstream_checksums.txt"
    download(url, checksum_path)
    content = checksum_path.read_text(encoding="utf-8", errors="replace")

    found = parse_upstream_sha256(content, needle)
    if found is not None:
        return found

    # 解析不出来时把原文摘要带上，否则只能靠翻 CI 日志猜上游改了什么格式
    preview = "\n".join(f"    | {line}" for line in content.splitlines()[:10] if line.strip())
    raise RuntimeError(
        f"上游校验文件里找不到 {needle} 的条目: {url}\n"
        f"  已尝试 GNU coreutils / PowerShell Format-List / BSD / 裸哈希 四种格式。\n"
        f"  校验文件内容（前 10 行）:\n{preview or '    | <空>'}"
    )


def github_api(endpoint: str, timeout: int = 30) -> dict:
    """调用 GitHub API"""
    url = f"https://api.github.com{endpoint}"
    req = Request(
        url,
        headers={
            "User-Agent": "FluentYTDL-Builder/1.0",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    ctx = create_ssl_context()
    try:
        with urlopen(req, context=ctx, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"GitHub API 调用失败: {url} - {e}") from e


# ============================================================================
# 版本探测
# ============================================================================


def probe_version(exe: Path, args: list[str], pattern: str | None = None) -> str:
    """执行工具自身的 version 命令取版本号。

    版本号有两个用途：锁文件的比对基准，以及产物里 BUILD_INFO.json 的溯源信息。
    探测失败不阻断构建（某些工具在无 GUI/无网络环境会异常退出），退化为 "unknown"。
    """
    if not exe.exists():
        return "unknown"
    try:
        proc = subprocess.run(
            [str(exe), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"

    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if not out:
        return "unknown"
    if pattern:
        m = re.search(pattern, out)
        if m:
            return m.group(1).strip()
    return out.splitlines()[0].strip()


# ============================================================================
# 工具下载函数
#   每个函数返回 (版本号, {落盘文件名: 路径}, 资产元数据 | None)，
#   由 main() 统一交给锁文件比对。资产元数据经 TOOLS.lock.json 流向
#   scripts/generate_manifest.py，最终成为 update-manifest.json 里 bin/* 的
#   url + sha256 + size；返回 None 表示"这个工具不进清单，运行时只走 API"。
# ============================================================================

YT_DLP_ASSET_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"


def fetch_yt_dlp(
    dest_dir: Path, *, download=download_file, asset_meta=asset_meta
) -> tuple[str, dict[str, Path], dict | None]:
    """获取 yt-dlp（上游提供 SHA2-256SUMS，硬校验）"""
    print("\n🔧 获取 yt-dlp...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        exe_path = tmp_path / "yt-dlp.exe"
        download(YT_DLP_ASSET_URL, exe_path)

        expected = fetch_upstream_sha256(
            "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS",
            "yt-dlp.exe",
            tmp_path,
            download=download,
        )
        assert_sha256(exe_path, expected, "yt-dlp SHA2-256SUMS")

        asset = asset_meta(YT_DLP_ASSET_URL, exe_path)

        final_path = dest_dir / "yt-dlp.exe"
        shutil.move(str(exe_path), str(final_path))

    version = probe_version(dest_dir / "yt-dlp.exe", ["--version"])
    print(f"  ✓ yt-dlp {version} 已安装到 {dest_dir}")
    return version, {"yt-dlp.exe": dest_dir / "yt-dlp.exe"}, asset


def fetch_ffmpeg(
    dest_dir: Path, *, download=download_file, asset_meta=asset_meta
) -> tuple[str, dict[str, Path], dict | None]:
    """获取 ffmpeg (yt-dlp 官方修复版本)

    yt-dlp/FFmpeg-Builds 的 ``latest`` release 不发布 .sha256 附件；下载完整性靠
    ``ReleaseSource`` 的 size + GitHub 资产 digest 校验。``asset_meta`` 形参仅为与
    其他 fetcher 统一调用签名 —— 本函数不进清单（见下方 return None）。
    """
    print("\n🔧 获取 ffmpeg (yt-dlp FFmpeg-Builds)...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    url = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "ffmpeg.zip"

        download(url, zip_path)

        print("  📦 解压中...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_path)

        extracted_dirs = [
            d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("ffmpeg")
        ]
        if not extracted_dirs:
            raise RuntimeError("未找到解压后的 ffmpeg 目录")

        bin_dir = extracted_dirs[0] / "bin"
        if not bin_dir.exists():
            raise RuntimeError(f"未找到 bin 目录: {bin_dir}")

        for exe in ["ffmpeg.exe", "ffprobe.exe"]:
            src = bin_dir / exe
            if not src.exists():
                raise RuntimeError(f"ffmpeg 压缩包内缺少 {exe}")
            shutil.copy2(src, dest_dir / exe)
            size_mb = src.stat().st_size / 1024 / 1024
            print(f"  ✓ 已复制 {exe} ({size_mb:.1f} MB)")

    version = probe_version(dest_dir / "ffmpeg.exe", ["-version"], r"ffmpeg version (\S+)")
    print(f"  ✓ ffmpeg {version} 已安装到 {dest_dir}")
    # 资产元数据故意返回 None：`yt-dlp/FFmpeg-Builds` 用滚动的 `latest` tag，
    # 同一个 URL 的内容随时被替换，钉不住版本。ffmpeg 因此不进清单，运行时只走 API。
    return (
        version,
        {
            "ffmpeg.exe": dest_dir / "ffmpeg.exe",
            "ffprobe.exe": dest_dir / "ffprobe.exe",
        },
        None,
    )


def fetch_deno(
    dest_dir: Path, *, download=download_file, asset_meta=asset_meta
) -> tuple[str, dict[str, Path], dict | None]:
    """获取 Deno（上游提供 .zip.sha256sum，硬校验）"""
    print("\n🔧 获取 Deno...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    base = "https://github.com/denoland/deno/releases/latest/download"
    asset = "deno-x86_64-pc-windows-msvc.zip"
    asset_url = f"{base}/{asset}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "deno.zip"

        download(asset_url, zip_path)

        expected = fetch_upstream_sha256(
            f"{asset_url}.sha256sum", asset, tmp_path, download=download
        )
        assert_sha256(zip_path, expected, "deno .sha256sum")

        asset_info = asset_meta(asset_url, zip_path)

        print("  📦 解压中...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_path)

        exe_found = False
        for f in tmp_path.rglob("deno.exe"):
            shutil.copy2(f, dest_dir / "deno.exe")
            exe_found = True
            break

        if not exe_found:
            raise RuntimeError("未找到 deno.exe")

    version = probe_version(dest_dir / "deno.exe", ["--version"], r"deno (\S+)")
    print(f"  ✓ Deno {version} 已安装到 {dest_dir}")
    return version, {"deno.exe": dest_dir / "deno.exe"}, asset_info


def fetch_atomicparsley(
    dest_dir: Path, *, download=download_file, asset_meta=asset_meta
) -> tuple[str, dict[str, Path], dict | None]:
    """获取 AtomicParsley (用于嵌入封面)

    上游不发布校验文件，下载完整性靠 ``ReleaseSource`` 的 size + GitHub 资产 digest 校验。
    """
    print("\n🔧 获取 AtomicParsley...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "atomicparsley.zip"

        url = (
            "https://github.com/wez/atomicparsley/releases/latest/download/AtomicParsleyWindows.zip"
        )
        download(url, zip_path)
        asset_info = asset_meta(url, zip_path)

        print("  📦 解压中...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_path)

        exe_found = False
        for f in tmp_path.rglob("AtomicParsley.exe"):
            shutil.copy2(f, dest_dir / "AtomicParsley.exe")
            exe_found = True
            break

        if not exe_found:
            raise RuntimeError("未找到 AtomicParsley.exe")

    version = probe_version(
        dest_dir / "AtomicParsley.exe", ["--version"], r"AtomicParsley version:?\s*(\S+)"
    )
    print(f"  ✓ AtomicParsley {version} 已安装到 {dest_dir}")
    return version, {"AtomicParsley.exe": dest_dir / "AtomicParsley.exe"}, asset_info


def fetch_pot_provider(
    dest_dir: Path, *, download=download_file, asset_meta=asset_meta
) -> tuple[str, dict[str, Path], dict | None]:
    """获取 POT Provider (bgutil-ytdlp-pot-provider-rs)

    上游不发布校验文件，下载完整性靠 ``ReleaseSource`` 的 size + GitHub 资产 digest 校验。
    """
    print("\n🔧 获取 POT Provider...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        exe_path = tmp_path / "bgutil-pot-provider.exe"

        url = "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-windows-x86_64.exe"
        download(url, exe_path)
        asset_info = asset_meta(url, exe_path)

        final_path = dest_dir / "bgutil-pot-provider.exe"
        shutil.move(str(exe_path), str(final_path))

    version = probe_version(
        dest_dir / "bgutil-pot-provider.exe", ["--version"], r"bgutil-pot\s+(\S+)"
    )
    print(f"  ✓ POT Provider {version} 已安装到 {dest_dir}")
    return version, {"bgutil-pot-provider.exe": dest_dir / "bgutil-pot-provider.exe"}, asset_info


# ============================================================================
# 工具登记表
#   下载与"只校验"两条路径共用同一份文件清单与版本探测参数，
#   否则两边容易各写一份、慢慢对不上。
# ============================================================================

FETCHERS = [
    ("yt-dlp", "yt-dlp", fetch_yt_dlp, ["yt-dlp.exe"], ("yt-dlp.exe", ["--version"], None)),
    (
        "ffmpeg",
        "ffmpeg",
        fetch_ffmpeg,
        ["ffmpeg.exe", "ffprobe.exe"],
        ("ffmpeg.exe", ["-version"], r"ffmpeg version (\S+)"),
    ),
    ("deno", "deno", fetch_deno, ["deno.exe"], ("deno.exe", ["--version"], r"deno (\S+)")),
    (
        "pot-provider",
        "pot-provider",
        fetch_pot_provider,
        ["bgutil-pot-provider.exe"],
        ("bgutil-pot-provider.exe", ["--version"], r"bgutil-pot\s+(\S+)"),
    ),
    (
        "atomicparsley",
        "atomicparsley",
        fetch_atomicparsley,
        ["AtomicParsley.exe"],
        ("AtomicParsley.exe", ["--version"], r"AtomicParsley version:?\s*(\S+)"),
    ),
]


def _load_lock_tools() -> dict[str, dict]:
    """读锁文件的 tools 段，读不出来就当空 —— 调用方都能靠降级路径继续。"""
    snapshot = os.environ.get("FLUENTYTDL_COMPONENT_SNAPSHOT")
    if snapshot:
        return json.loads(Path(snapshot).read_text(encoding="utf-8"))["tools"]
    if not TOOLS_LOCK.exists():
        return {}
    try:
        data = json.loads(TOOLS_LOCK.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        return {}
    return {name: entry for name, entry in tools.items() if isinstance(entry, dict)}


def load_tool_versions() -> dict[str, str]:
    """给 build.py 用：读锁文件里记录的工具版本，写进 BUILD_INFO.json。"""
    return {name: entry.get("version", "unknown") for name, entry in _load_lock_tools().items()}


def load_tool_assets() -> dict[str, dict]:
    """给 generate_manifest.py 用：{工具: {version, url, sha256, size}}。

    只返回三个字段都齐的条目。缺一个就整条不给 —— 清单里"有 url 没 sha256"比没有
    更危险（下载不校验），"有 sha256 没 url"则是死路（`install_component()` 拿不到
    地址）。锁里没有的工具（ffmpeg，或旧锁文件）在清单里保持空串，运行时会按
    `dependency_manager._overlay_manifest()` 自动退回 API 解析。
    """
    out: dict[str, dict] = {}
    for name, entry in _load_lock_tools().items():
        asset = entry.get("asset")
        if not isinstance(asset, dict):
            continue
        url = str(asset.get("url") or "").strip()
        sha256 = str(asset.get("sha256") or "").strip()
        size = asset.get("size")
        if not url or not sha256 or not isinstance(size, int) or size <= 0:
            continue
        out[name] = {
            "version": str(entry.get("version", "unknown")),
            "url": url,
            "sha256": sha256,
            "size": size,
        }
    return out


def main():
    """Standalone fetch is also latest-only and isolated; historical locks are read-only."""
    import uuid

    from component_snapshot import prepare

    parser = argparse.ArgumentParser(description="Fetch all latest release components")
    parser.add_argument(
        "--force", "-f", action="store_true", help="Compatibility alias: always fetch latest"
    )
    parser.add_argument("--destination", type=Path, help="New empty snapshot directory")
    args = parser.parse_args()
    destination = args.destination or ROOT / "build/components" / uuid.uuid4().hex
    print(prepare(destination))


if __name__ == "__main__":
    main()
