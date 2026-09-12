#!/usr/bin/env python3
"""
FluentYTDL SHA256 校验文件生成器

为 release 目录中的所有发布文件生成 SHA256SUMS.txt

用法:
    python scripts/checksums.py
    python scripts/checksums.py release/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RELEASE_DIR = ROOT / "release"


def sha256_file(file_path: Path) -> str:
    """计算文件 SHA256"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def generate_checksums(release_dir: Path) -> Path:
    """为 release 目录中的所有文件生成校验和"""
    if not release_dir.exists():
        raise FileNotFoundError(f"Release 目录不存在: {release_dir}")

    print("=" * 50)
    print("FluentYTDL SHA256 校验文件生成器")
    print("=" * 50)
    print(f"目录: {release_dir}")
    print()

    checksums = []
    result_file = ROOT / "build/latest-result.json"
    result = json.loads(result_file.read_text(encoding="utf-8"))
    for artifact in result["artifacts"]:
        file = Path(artifact["path"])
        if file.name == "SHA256SUMS.txt":
            continue
        if file.parent.resolve() != release_dir.resolve():
            raise ValueError("Requested directory is not the successful build output")
        if sha256_file(file) != artifact["sha256"]:
            raise ValueError(f"Artifact changed since verification: {file}")
        checksums.append(f"{artifact['sha256']}  {file.name}")

    if not checksums:
        print("⚠ 未找到需要计算校验和的文件")
        return release_dir / "SHA256SUMS.txt"

    # 写入校验文件
    checksum_file = release_dir / "SHA256SUMS.txt"
    checksum_file.write_text("\n".join(checksums) + "\n", encoding="utf-8")

    print()
    print("=" * 50)
    print(f"✓ 校验文件已生成: {checksum_file}")
    print(f"  包含 {len(checksums)} 个文件的校验和")
    print("=" * 50)

    return checksum_file


def main():
    parser = argparse.ArgumentParser(description="FluentYTDL SHA256 校验文件生成器")
    parser.add_argument(
        "release_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_RELEASE_DIR,
        help=f"Release 目录路径 (默认: {DEFAULT_RELEASE_DIR})",
    )
    args = parser.parse_args()

    try:
        generate_checksums(args.release_dir)
    except Exception as e:
        print(f"\n❌ 生成失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
