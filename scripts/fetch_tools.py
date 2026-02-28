#!/usr/bin/env python3
"""
FluentYTDL 外部工具下载脚本

从 GitHub Releases 获取 yt-dlp, ffmpeg, deno 的最新版本。
自动校验 SHA256 确保下载完整性。

用法:
    python scripts/fetch_tools.py
    python scripts/fetch_tools.py --force  # 强制重新下载
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import ssl
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
TARGET_DIR = ROOT / "assets" / "bin"


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
) -> None:
    """下载文件并显示进度"""
    print(f"  📥 下载: {url}")

    ctx = create_ssl_context()
    req = Request(url, headers={"User-Agent": "FluentYTDL-Builder/1.0"})

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
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"下载失败: {url} - {e}") from e


def verify_sha256(file_path: Path, expected_hash: str) -> bool:
    """校验文件 SHA256"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest().upper()
    expected = expected_hash.upper()
    if actual != expected:
        print("  ❌ 校验失败!")
        print(f"     期望: {expected[:32]}...")
        print(f"     实际: {actual[:32]}...")
        return False
    print(f"  ✓ 校验通过 ({actual[:16]}...)")
    return True


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
# 工具下载函数
# ============================================================================


def fetch_yt_dlp(dest_dir: Path) -> None:
    """获取 yt-dlp"""
    print("\n🔧 获取 yt-dlp...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    release = github_api("/repos/yt-dlp/yt-dlp/releases/latest")
    tag = release.get("tag_name", "unknown")
    print(f"  最新版本: {tag}")

    # 查找 exe 资产
    exe_asset = next((a for a in release["assets"] if a["name"] == "yt-dlp.exe"), None)
    if not exe_asset:
        raise RuntimeError("未找到 yt-dlp.exe 资产")

    # 查找校验和文件
    checksum_asset = next((a for a in release["assets"] if a["name"] == "SHA2-256SUMS"), None)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 下载 exe
        exe_path = tmp_path / "yt-dlp.exe"
        download_file(exe_asset["browser_download_url"], exe_path)

        # 下载并校验
        if checksum_asset:
            checksum_path = tmp_path / "checksums.txt"
            download_file(checksum_asset["browser_download_url"], checksum_path)
            checksums = checksum_path.read_text(encoding="utf-8")
            for line in checksums.splitlines():
                if "yt-dlp.exe" in line:
                    expected_hash = line.split()[0]
                    if not verify_sha256(exe_path, expected_hash):
                        raise RuntimeError("yt-dlp.exe 校验失败")
                    break
        else:
            print("  ⚠ 未找到校验文件，跳过校验")

        # 移动到目标
        final_path = dest_dir / "yt-dlp.exe"
        shutil.move(str(exe_path), str(final_path))

    print(f"  ✓ yt-dlp {tag} 已安装到 {dest_dir}")


def fetch_ffmpeg(dest_dir: Path) -> None:
    """获取 ffmpeg (yt-dlp 官方修复版本)"""
    print("\n🔧 获取 ffmpeg (yt-dlp FFmpeg-Builds)...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 使用 yt-dlp 官方提供的 FFmpeg 构建
    # https://github.com/yt-dlp/FFmpeg-Builds
    url = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "ffmpeg.zip"

        download_file(url, zip_path)

        # 解压
        print("  📦 解压中...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_path)

        # 找到 bin 目录
        extracted_dirs = [
            d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("ffmpeg")
        ]
        if not extracted_dirs:
            raise RuntimeError("未找到解压后的 ffmpeg 目录")

        bin_dir = extracted_dirs[0] / "bin"
        if not bin_dir.exists():
            raise RuntimeError(f"未找到 bin 目录: {bin_dir}")

        # 复制可执行文件
        for exe in ["ffmpeg.exe", "ffprobe.exe"]:
            src = bin_dir / exe
            if src.exists():
                shutil.copy2(src, dest_dir / exe)
                size_mb = src.stat().st_size / 1024 / 1024
                print(f"  ✓ 已复制 {exe} ({size_mb:.1f} MB)")

    print(f"  ✓ ffmpeg (yt-dlp) 已安装到 {dest_dir}")


def fetch_deno(dest_dir: Path) -> None:
    """获取 Deno (JavaScript 运行时)"""
    print("\n🔧 获取 Deno...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    release = github_api("/repos/denoland/deno/releases/latest")
    tag = release.get("tag_name", "unknown")
    print(f"  最新版本: {tag}")

    # 查找 Windows x86_64 zip
    zip_asset = next(
        (
            a
            for a in release["assets"]
            if "x86_64" in a["name"]
            and "windows" in a["name"].lower()
            and a["name"].endswith(".zip")
        ),
        None,
    )
    if not zip_asset:
        raise RuntimeError("未找到 Deno Windows x86_64 zip 资产")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "deno.zip"

        download_file(zip_asset["browser_download_url"], zip_path)

        # 解压
        print("  📦 解压中...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_path)

        # 查找 deno.exe
        exe_found = False
        for f in tmp_path.rglob("deno.exe"):
            shutil.copy2(f, dest_dir / "deno.exe")
            exe_found = True
            break

        if not exe_found:
            raise RuntimeError("未找到 deno.exe")

    print(f"  ✓ Deno {tag} 已安装到 {dest_dir}")


def fetch_atomicparsley(dest_dir: Path) -> None:
    """获取 AtomicParsley (用于嵌入封面)"""
    print("\n🔧 获取 AtomicParsley...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    release = github_api("/repos/wez/atomicparsley/releases/latest")
    tag = release.get("tag_name", "unknown")
    print(f"  最新版本: {tag}")

    # 查找 Windows zip
    zip_asset = next(
        (a for a in release["assets"] if "Windows" in a["name"] and a["name"].endswith(".zip")),
        None,
    )
    if not zip_asset:
        raise RuntimeError("未找到 AtomicParsley Windows zip 资产")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "atomicparsley.zip"

        download_file(zip_asset["browser_download_url"], zip_path)

        # 解压
        print("  📦 解压中...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(tmp_path)

        # 查找 AtomicParsley.exe
        exe_found = False
        for f in tmp_path.rglob("AtomicParsley.exe"):
            shutil.copy2(f, dest_dir / "AtomicParsley.exe")
            exe_found = True
            break

        if not exe_found:
            raise RuntimeError("未找到 AtomicParsley.exe")

    print(f"  ✓ AtomicParsley {tag} 已安装到 {dest_dir}")


def fetch_pot_provider(dest_dir: Path) -> None:
    """获取 POT Provider (bgutil-ytdlp-pot-provider-rs)"""
    print("\n🔧 获取 POT Provider...")
    dest_dir.mkdir(parents=True, exist_ok=True)

    release = github_api("/repos/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest")
    tag = release.get("tag_name", "unknown")
    print(f"  最新版本: {tag}")

    # 查找 Windows exe
    # 通常命名为: bgutil-pot-windows-x86_64.exe
    exe_asset = next(
        (
            a
            for a in release["assets"]
            if "windows" in a["name"].lower() and a["name"].endswith(".exe")
        ),
        None,
    )
    if not exe_asset:
        raise RuntimeError("未找到 POT Provider Windows exe 资产")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        exe_path = tmp_path / "bgutil-pot-provider.exe"

        download_file(exe_asset["browser_download_url"], exe_path)

        # 移动到目标
        final_path = dest_dir / "bgutil-pot-provider.exe"
        shutil.move(str(exe_path), str(final_path))

    print(f"  ✓ POT Provider {tag} 已安装到 {dest_dir}")


# ============================================================================
# 主入口
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="FluentYTDL 外部工具下载器")
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="强制重新下载（忽略已存在的工具）",
    )
    args = parser.parse_args()

    print("=" * 50)
    print("FluentYTDL 外部工具下载器")
    print("=" * 50)
    print(f"目标目录: {TARGET_DIR}")

    # 检查是否已存在
    checks = [
        TARGET_DIR / "yt-dlp" / "yt-dlp.exe",
        TARGET_DIR / "ffmpeg" / "ffmpeg.exe",
        TARGET_DIR / "deno" / "deno.exe",
        TARGET_DIR / "pot-provider" / "bgutil-pot-provider.exe",
        TARGET_DIR / "atomicparsley" / "AtomicParsley.exe",
    ]

    if not args.force and all(p.exists() for p in checks):
        print("\n✓ 所有工具已存在，跳过下载")
        print("  使用 --force 强制重新下载")
        return

    # 如果强制重新下载，清理目标目录
    if args.force and TARGET_DIR.exists():
        print("\n🧹 清理现有工具...")
        shutil.rmtree(TARGET_DIR)

    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    # 下载各工具
    try:
        fetch_yt_dlp(TARGET_DIR / "yt-dlp")
        fetch_ffmpeg(TARGET_DIR / "ffmpeg")
        fetch_deno(TARGET_DIR / "deno")
        fetch_pot_provider(TARGET_DIR / "pot-provider")
        fetch_atomicparsley(TARGET_DIR / "atomicparsley")
    except Exception as e:
        print(f"\n❌ 下载失败: {e}")
        sys.exit(1)

    print("\n" + "=" * 50)
    print("🎉 所有工具下载完成!")
    print("=" * 50)

    # 显示下载的文件
    print("\n已下载的文件:")
    for check in checks:
        if check.exists():
            size = check.stat().st_size
            print(f"  ✓ {check.relative_to(TARGET_DIR)} ({size:,} bytes)")


if __name__ == "__main__":
    main()
