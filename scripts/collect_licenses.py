#!/usr/bin/env python3
"""
FluentYTDL 许可证收集脚本

从各开源项目获取许可证文本，生成汇总文件。

用法:
    python scripts/collect_licenses.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
LICENSES_DIR = ROOT / "licenses"

# 许可证来源 URL
LICENSE_SOURCES = {
    "yt-dlp": "https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/LICENSE",
    "FFmpeg": "https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/COPYING.GPLv3",
    "Deno": "https://raw.githubusercontent.com/denoland/deno/main/LICENSE.md",
    "PySide6": "https://www.gnu.org/licenses/lgpl-3.0.txt",
}


def download_license(name: str, url: str, timeout: int = 15) -> str:
    """下载许可证文本"""
    from fluentytdl.utils.tls_trust import _tls_context

    ctx = _tls_context()
    req = Request(url, headers={"User-Agent": "FluentYTDL-Builder/1.0"})

    try:
        with urlopen(req, context=ctx, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (HTTPError, URLError) as e:
        return f"[Failed to fetch license from {url}: {e}]"
    except Exception as e:
        return f"[Error: {e}]"


def collect_all() -> None:
    """收集所有许可证"""
    print("=" * 50)
    print("FluentYTDL 许可证收集器")
    print("=" * 50)

    LICENSES_DIR.mkdir(parents=True, exist_ok=True)

    all_licenses = []

    for name, url in LICENSE_SOURCES.items():
        print(f"📄 获取 {name} 许可证...")
        content = download_license(name, url)

        # 单独保存
        license_file = LICENSES_DIR / f"{name}-LICENSE.txt"
        license_file.write_text(content, encoding="utf-8")
        print(f"  ✓ 已保存: {license_file.name}")

        # 汇总
        all_licenses.append(f"{'=' * 60}\n{name}\n{'=' * 60}\n\n{content}\n")

    # 生成汇总文件
    summary = LICENSES_DIR / "THIRD_PARTY_LICENSES.txt"
    summary.write_text(
        "FluentYTDL Third-Party Licenses\n"
        "================================\n\n"
        "This file contains the licenses of all third-party components\n"
        "bundled or used by FluentYTDL.\n\n"
        "Components:\n"
        + "\n".join(f"  - {name}" for name in LICENSE_SOURCES.keys())
        + "\n\n"
        + "\n".join(all_licenses),
        encoding="utf-8",
    )
    print(f"\n✓ 汇总文件已生成: {summary}")

    # 复制项目自身许可证 (如果存在)
    project_license = ROOT / "LICENSE"
    if project_license.exists():
        dest = LICENSES_DIR / "FluentYTDL-LICENSE.txt"
        dest.write_text(project_license.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"✓ 项目许可证已复制: {dest.name}")

    print("\n" + "=" * 50)
    print("🎉 所有许可证收集完成!")
    print("=" * 50)


def main():
    try:
        collect_all()
    except Exception as e:
        print(f"\n❌ 收集失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
