#!/usr/bin/env python3
"""
FluentYTDL 版本管理工具

版本号采用标准 PEP 440 / SemVer 格式，不带任何前缀：
  X.Y.Z         — 正式发布（稳定版），GitHub Release Latest
  X.Y.Z-rc.N    — 预发布（候选版），GitHub Release Pre-release
  X.Y.Z-beta.N  — 测试版，仅 Artifacts + 项目负责人在群/频道分发

Git tag = "v" + 版本号，例如 v3.5.5 / v3.5.6-rc.1。

VERSION 文件（根目录）是唯一的 source of truth，存储不带 "v" 的完整版本。
Inno Setup 只接受纯数字版本，因此 .iss 只写 X.Y.Z 部分。

用法:
    python scripts/version_manager.py check                # 检查版本一致性
    python scripts/version_manager.py set 3.5.6            # 设置正式版
    python scripts/version_manager.py set 3.5.6-rc.1       # 设置预发布版本
    python scripts/version_manager.py set 3.6.0-beta.1     # 设置测试版本
    python scripts/version_manager.py bump major|minor|patch  # 自动递增版本
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent

# 标准版本号: X.Y.Z 可选带 -rc.N / -beta.N 预发布后缀
VERSION_RE = re.compile(r"^(?P<numeric>\d+\.\d+\.\d+)(?:-(?P<channel>rc|beta)\.(?P<serial>\d+))?$")

# 各通道的中文名与发布行为
CHANNEL_INFO = {
    "stable": ("正式版", "GitHub Release (Latest)"),
    "rc": ("预发布", "GitHub Release (Pre-release)"),
    "beta": ("测试版", "仅 Artifacts + 群/频道分发"),
}


@dataclass
class VersionFile:
    """版本文件配置"""

    path: Path
    pattern: str  # 正则表达式模式，必须包含一个捕获组
    template: str  # 替换模板，使用 {version} 占位符
    description: str
    writes_full: bool = False  # True: 写完整版本（含预发布后缀）; False: 只写 X.Y.Z


def strip_v_prefix(version_str: str) -> str:
    """去掉 Git tag 风格的 "v" 前缀。"v3.5.5" → "3.5.5"，"3.5.5" 原样返回。"""
    version_str = version_str.strip()
    if re.match(r"^v\d", version_str):
        return version_str[1:]
    return version_str


def parse_version(version_str: str) -> tuple[str, str]:
    """解析版本号 → (数字部分, 通道)。

    "3.5.5"        → ("3.5.5", "stable")
    "3.5.6-rc.1"   → ("3.5.6", "rc")
    "3.6.0-beta.2" → ("3.6.0", "beta")

    容忍 "v" 前缀（tag 名可直接传入）。格式非法时抛 ValueError。
    """
    cleaned = strip_v_prefix(version_str)
    match = VERSION_RE.match(cleaned)
    if not match:
        raise ValueError(
            f"无效的版本号: {version_str!r}\n"
            f"  期望格式: X.Y.Z / X.Y.Z-rc.N / X.Y.Z-beta.N（如 3.5.5、3.5.6-rc.1）"
        )
    return match.group("numeric"), match.group("channel") or "stable"


def is_valid_version(version_str: str) -> bool:
    """判断版本号是否符合规范（不接受 "v" 前缀）。"""
    return VERSION_RE.match(version_str.strip()) is not None


def tag_for(version_str: str) -> str:
    """由版本号推导 Git tag。"3.5.5" → "v3.5.5"。"""
    return f"v{strip_v_prefix(version_str)}"


class VersionManager:
    """版本管理器"""

    VERSION_FILES = [
        VersionFile(
            path=ROOT / "VERSION",
            pattern=r"^.+$",
            template="{version}",
            description="VERSION 源文件",
            writes_full=True,
        ),
        VersionFile(
            path=ROOT / "pyproject.toml",
            pattern=r'^version\s*=\s*["\']([^"\']+)["\']',
            template='version = "{version}"',
            description="项目配置文件",
            # "3.5.6-rc.1" 是合法 PEP 440（规范化为 3.5.6rc1），可以写完整版本
            writes_full=True,
        ),
        VersionFile(
            path=ROOT / "src" / "fluentytdl" / "__init__.py",
            pattern=r'^__version__\s*=\s*["\']([^"\']+)["\']',
            template='__version__ = "{version}"',
            description="Python 包版本",
            writes_full=True,
        ),
        VersionFile(
            path=ROOT / "installer" / "FluentYTDL.iss",
            pattern=r'#define\s+MyAppVersion\s+"([^"]+)"',
            template='#define MyAppVersion "{version}"',
            description="Inno Setup 默认版本",
            # Display keeps the channel; the installer derives numeric PE fields separately.
            writes_full=True,
        ),
    ]

    def __init__(self):
        self.current_versions: dict[Path, str] = {}

    def _read_version_from_file(self, vf: VersionFile) -> str | None:
        """从文件中读取版本号。返回 None 表示文件不存在或无法读取。"""
        if not vf.path.exists():
            return None

        # VERSION 文件特殊处理：纯文本单行
        if vf.path.name == "VERSION":
            return vf.path.read_text(encoding="utf-8").strip()

        content = vf.path.read_text(encoding="utf-8")

        # __init__.py 特殊处理：动态读取 VERSION 文件时返回 VERSION 的值
        if vf.path.name == "__init__.py" and "_read_version()" in content:
            version_vf = self.VERSION_FILES[0]
            return self._read_version_from_file(version_vf)

        match = re.search(vf.pattern, content, re.MULTILINE)
        return match.group(1) if match else None

    def check_consistency(self) -> bool:
        """检查版本一致性。

        VERSION / pyproject.toml / __init__.py 存储完整版本（含预发布后缀），
        .iss 的显示版本也保留后缀，PE 数字版本由安装脚本单独派生。
        """
        print("🔍 检查版本号一致性...\n")

        self.current_versions = {}

        # 读取 VERSION 文件获取期望值
        version_vf = self.VERSION_FILES[0]  # VERSION
        full_version = self._read_version_from_file(version_vf)
        if not full_version:
            print("  ❌ VERSION 文件不存在或为空")
            return False

        try:
            numeric, channel = parse_version(full_version)
        except ValueError as e:
            print(f"  ❌ {e}")
            return False

        if full_version != strip_v_prefix(full_version):
            print(f'  ❌ VERSION 文件不应包含 "v" 前缀: {full_version}')
            print(f"     应写作 {strip_v_prefix(full_version)}，Git tag 才是 {full_version}")
            return False

        channel_name = CHANNEL_INFO[channel][0]
        print(f"  📌 VERSION 源文件: {full_version} ({channel_name}, 数字: {numeric})")
        print(f"  🏷️  对应 Git tag: {tag_for(full_version)}\n")

        all_ok = True
        for vf in self.VERSION_FILES:
            actual = self._read_version_from_file(vf)
            if actual is None:
                print(f"  ⚠️  {vf.description}: 文件不存在 - {vf.path}")
                continue

            self.current_versions[vf.path] = actual

            # 判断期望值
            expected = full_version if vf.writes_full else numeric
            if actual == expected:
                print(f"  ✅ {vf.description:20s}: {actual}")
            else:
                print(f"  ❌ {vf.description:20s}: {actual} (期望: {expected})")
                all_ok = False

        print()
        if all_ok:
            print(f"✅ 所有版本号一致: {full_version}")
        else:
            print("❌ 版本号不一致")
        return all_ok

    def get_current_version(self) -> str | None:
        """获取当前完整版本（从 VERSION 文件）"""
        vf = self.VERSION_FILES[0]
        return self._read_version_from_file(vf)

    def set_version(self, new_version: str) -> bool:
        """设置新版本号到所有文件。

        new_version 格式: "3.5.6" / "3.5.6-rc.1" / "3.6.0-beta.1"
        不接受 "v" 前缀 —— 那是 Git tag 的格式，不是 VERSION 文件的内容。
        """
        new_version = new_version.strip()

        if re.match(r"^v\d", new_version):
            print(f'❌ 版本号不应带 "v" 前缀: {new_version}')
            print(f"   请改用: python scripts/version_manager.py set {new_version[1:]}")
            print(f'   "v" 只用于 Git tag（发布时执行 git tag {new_version}）')
            return False

        try:
            numeric, channel = parse_version(new_version)
        except ValueError as e:
            print(f"❌ {e}")
            return False

        channel_name = CHANNEL_INFO[channel][0]
        print(f"📝 设置版本号为: {new_version}")
        print(f"   类型: {channel_name}, 数字: {numeric}, Git tag: {tag_for(new_version)}\n")

        success_count = 0
        for vf in self.VERSION_FILES:
            if not vf.path.exists():
                print(f"  ⚠️  跳过 {vf.description}: 文件不存在")
                continue

            # __init__.py 使用动态读取时跳过（运行时自动从 VERSION 读取）
            if vf.path.name == "__init__.py":
                content_check = vf.path.read_text(encoding="utf-8")
                if "_read_version()" in content_check:
                    print(f"  ⏭️  {vf.description:20s}: 动态读取，无需写入")
                    success_count += 1
                    continue

            try:
                content_to_write = new_version if vf.writes_full else numeric
                old_version = self._read_version_from_file(vf)

                if vf.path.name == "VERSION":
                    # VERSION 文件：纯文本写入
                    vf.path.write_text(content_to_write + "\n", encoding="utf-8")
                else:
                    # 其他文件：正则替换
                    content = vf.path.read_text(encoding="utf-8")
                    new_line = vf.template.format(version=content_to_write)
                    content = re.sub(vf.pattern, new_line, content, flags=re.MULTILINE)
                    vf.path.write_text(content, encoding="utf-8")

                status = (
                    f"{old_version} → {content_to_write}"
                    if old_version
                    else f"设置为 {content_to_write}"
                )
                print(f"  ✅ {vf.description:20s}: {status}")
                success_count += 1

            except Exception as e:
                print(f"  ❌ {vf.description:20s}: 失败 - {e}")

        print(f"\n✅ 已更新 {success_count}/{len(self.VERSION_FILES)} 个文件")
        complete = success_count == len([vf for vf in self.VERSION_FILES if vf.path.exists()])
        if complete:
            from build_environment import versions

            subprocess.run(["uv", "lock", "--python", versions()["python"]], cwd=ROOT, check=True)
        return complete

    def bump_version(
        self,
        bump_type: Literal["major", "minor", "patch"],
        pre: Literal["rc", "beta"] | None = None,
    ) -> bool:
        """递增版本号。

        默认产出正式版（丢弃当前的预发布后缀）；传 pre 则产出该通道的 .1 预发布。
        例: 3.5.5 --bump patch          → 3.5.6
            3.5.5 --bump minor --pre rc → 3.6.0-rc.1
        """
        current = self.get_current_version()
        if not current:
            print("❌ 无法获取当前版本号")
            return False

        try:
            numeric, _channel = parse_version(current)
        except ValueError as e:
            print(f"❌ {e}")
            return False

        major, minor, patch = (int(p) for p in numeric.split("."))

        if bump_type == "major":
            major += 1
            minor = 0
            patch = 0
        elif bump_type == "minor":
            minor += 1
            patch = 0
        elif bump_type == "patch":
            patch += 1

        new_version = f"{major}.{minor}.{patch}"
        if pre:
            new_version += f"-{pre}.1"

        print(f"🔼 版本递增: {current} → {new_version} ({bump_type})\n")
        return self.set_version(new_version)

    @staticmethod
    def _is_valid_numeric_version(version: str) -> bool:
        """验证版本号格式（X.Y.Z 可带 -rc.N / -beta.N）"""
        return is_valid_version(version)

    def generate_summary(self) -> None:
        """生成版本信息摘要"""
        current = self.get_current_version()
        if not current:
            print("❌ 无法获取当前版本号")
            return

        try:
            numeric, channel = parse_version(current)
        except ValueError as e:
            print(f"❌ {e}")
            return

        channel_name, distribution = CHANNEL_INFO[channel]

        print("=" * 60)
        print("FluentYTDL 版本信息")
        print("=" * 60)
        print(f"当前版本: {current}")
        print(f"  类型: {channel_name} ({channel})")
        print(f"  数字: {numeric}")
        print(f"  Git tag: {tag_for(current)}")
        print(f"  发布去向: {distribution}")
        print()
        print("版本文件:")
        for vf in self.VERSION_FILES:
            status = "✓" if vf.path.exists() else "✗"
            kind = "完整" if vf.writes_full else "数字"
            print(f"  [{status}] {vf.description:20s}: {kind:4s} ({vf.path.relative_to(ROOT)})")
        print()
        print('版本规范 (Git tag = "v" + 版本号):')
        for key, (name, dest) in CHANNEL_INFO.items():
            sample = {"stable": "3.5.6", "rc": "3.5.6-rc.1", "beta": "3.6.0-beta.1"}[key]
            print(f"  {sample:16s} {name} → {dest}")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="FluentYTDL 版本管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/version_manager.py check                # 检查版本一致性
  python scripts/version_manager.py set 3.5.6            # 设置正式版
  python scripts/version_manager.py set 3.5.6-rc.1       # 设置预发布版
  python scripts/version_manager.py set 3.6.0-beta.1     # 设置测试版
  python scripts/version_manager.py bump patch           # 递增补丁版本 → 正式版
  python scripts/version_manager.py bump minor --pre rc  # 递增次版本 → rc.1
  python scripts/version_manager.py summary              # 显示版本摘要

注意: 版本号不带 "v" 前缀，Git tag 才带（VERSION=3.5.6 → tag=v3.5.6）。
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="命令")

    subparsers.add_parser("check", help="检查版本号一致性")

    set_parser = subparsers.add_parser("set", help="设置新版本号")
    set_parser.add_argument(
        "version",
        help="新版本号，如: 3.5.6, 3.5.6-rc.1, 3.6.0-beta.1 (不带 v 前缀)",
    )

    bump_parser = subparsers.add_parser("bump", help="自动递增版本号")
    bump_parser.add_argument(
        "type",
        choices=["major", "minor", "patch"],
        help="递增类型: major (主版本), minor (次版本), patch (补丁版本)",
    )
    bump_parser.add_argument(
        "--pre",
        choices=["rc", "beta"],
        default=None,
        help="产出该通道的预发布版本（如 --pre rc → X.Y.Z-rc.1），默认产出正式版",
    )

    subparsers.add_parser("summary", help="显示版本信息摘要")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    manager = VersionManager()

    if args.command == "check":
        success = manager.check_consistency()
        return 0 if success else 1

    elif args.command == "set":
        success = manager.set_version(args.version)
        if success:
            _print_git_hint(manager.get_current_version())
        return 0 if success else 1

    elif args.command == "bump":
        success = manager.bump_version(args.type, args.pre)
        if success:
            _print_git_hint(manager.get_current_version())
        return 0 if success else 1

    elif args.command == "summary":
        manager.generate_summary()
        return 0

    return 0


def _print_git_hint(version: str | None) -> None:
    """打印发布用的 Git 命令提示"""
    if not version:
        return
    tag = tag_for(version)
    print("\n💡 提示: 记得提交版本更改到 Git:")
    print("   git add -A")
    print(f'   git commit -m "release: {tag}"')
    print(f"   git tag {tag}")
    print("   git push && git push --tags")


if __name__ == "__main__":
    import io

    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
