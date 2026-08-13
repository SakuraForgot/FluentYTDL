#!/usr/bin/env python3
"""
FluentYTDL 更新清单生成器

在构建流程中调用，生成 update-manifest.json 供运行时更新检查使用。

用法:
    python scripts/generate_manifest.py --version 3.5.5 --release-dir release/
    python scripts/generate_manifest.py --version 3.5.6-rc.1 --tag v3.5.6-rc.1

输出:
    release/update-manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    except Exception:
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from version_manager import parse_version, strip_v_prefix, tag_for  # noqa: E402


def load_app_core_include() -> list[str]:
    """读取 app-core 白名单（唯一事实源: pyproject.toml）。

    与 scripts/build.py::create_app_core_7z() 共读同一份数组 —— 之前这里是
    一份硬编码副本，而且是**错的**（`VERSION` 和 `docs/` 实际都在 `_internal/`
    里面，PyInstaller 的 datas 全部落在 `_internal/`，顶层根本没有这两项）。

    注意这份清单的用途：**发布元数据 / 审计**，运行期没有任何消费者
    （component_update_manager 只按整包 sha256 校验，不逐文件比对）。
    归档内容的权威在 build.py 的白名单拷贝，不在这里。
    """
    pyproject = ROOT / "pyproject.toml"
    try:
        import tomllib

        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        items = data.get("tool", {}).get("fluentytdl", {}).get("build", {}).get("app_core_include")
    except ImportError:
        # Python 3.10 无 tomllib —— 回退到行解析（数组在 pyproject 里写成单行）
        items = None
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("app_core_include") and "=" in stripped:
                raw = stripped.split("=", 1)[1].strip()
                if raw.startswith("[") and raw.endswith("]"):
                    items = [x.strip(" '\"") for x in raw[1:-1].split(",") if x.strip()]
                break

    if not items:
        raise RuntimeError(
            "无法从 pyproject.toml 读取 [tool.fluentytdl.build].app_core_include。\n"
            "  该数组必须存在且写成单行 —— 它是 app-core 归档内容的唯一事实源。"
        )
    return list(items)


def sha256_file(file_path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def detect_component_versions(release_dir: Path) -> dict[str, dict]:
    """检测 bin/ 工具版本。从 assets/bin/ 目录的 exe 文件中获取。"""
    bin_dir = ROOT / "assets" / "bin"
    components: dict[str, dict] = {}

    # 定义各工具的版本检测命令和 GitHub 仓库
    tool_defs = {
        "yt-dlp": {
            "exe": "yt-dlp/yt-dlp.exe",
            "cmd": ["--version"],
            "repo": "yt-dlp/yt-dlp",
        },
        "ffmpeg": {
            "exe": "ffmpeg/ffmpeg.exe",
            "cmd": ["-version"],
            "repo": "BtbN/FFmpeg-Builds",
        },
        "deno": {
            "exe": "deno/deno.exe",
            "cmd": ["--version"],
            "repo": "denoland/deno",
        },
        "pot-provider": {
            "exe": "pot-provider/bgutil-pot-provider.exe",
            "cmd": ["--version"],
            "repo": "jim60105/bgutil-ytdlp-pot-provider-rs",
        },
        "atomicparsley": {
            "exe": "atomicparsley/AtomicParsley.exe",
            "cmd": ["--version"],
            "repo": "wez/atomicparsley",
        },
    }

    import subprocess

    for key, defn in tool_defs.items():
        exe_path = bin_dir / defn["exe"]
        if not exe_path.exists():
            continue

        # 尝试获取本地版本
        version = None
        try:
            result = subprocess.run(
                [str(exe_path)] + defn["cmd"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout + result.stderr).strip()
            # 提取第一行中的版本号
            if output:
                first_line = output.split("\n")[0].strip()
                # 尝试匹配 x.y.z 或日期格式 (YYYY.MM.DD)
                import re

                match = re.search(r"(\d{4}\.\d{2}\.\d{2}|\d+\.\d+\.\d+)", first_line)
                if match:
                    version = match.group(1)
        except Exception:
            pass

        if version:
            components[key] = {
                "version": version,
                "repo": defn["repo"],
            }

    return components


def _read_changelog(full_version: str) -> str:
    """从 CHANGELOG.md 或 git tag 读取更新日志。

    优先从 docs/CHANGELOG.md 读取对应版本段落，
    回退到 git tag message。
    """
    import subprocess

    tag = tag_for(full_version)

    # 1. 尝试从 CHANGELOG.md 读取
    changelog_file = ROOT / "docs" / "CHANGELOG.md"
    if changelog_file.exists():
        try:
            content = changelog_file.read_text(encoding="utf-8")
            # 查找版本标题（## v3.5.5 或 ## 3.5.5）
            for pattern in [f"## {tag}", f"## {full_version}"]:
                idx = content.find(pattern)
                if idx != -1:
                    # 截取到下一个 ## 或文件末尾
                    rest = content[idx + len(pattern) :]
                    next_section = rest.find("\n## ")
                    if next_section != -1:
                        return rest[:next_section].strip()
                    return rest.strip()
        except Exception:
            pass

    # 2. 回退到 git tag message
    try:
        result = subprocess.run(
            ["git", "tag", "-l", "--format=%(contents:body)", tag],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ROOT,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    return ""


def generate_manifest(
    full_version: str,
    release_dir: Path,
    base_url: str,
    release_tag: str | None = None,
) -> dict:
    """生成更新清单。"""
    numeric, _channel = parse_version(full_version)
    full_version = strip_v_prefix(full_version)
    release_tag = release_tag or tag_for(full_version)
    arch = "win64" if sys.maxsize > 2**32 else "win32"

    # 读取 changelog（RAW 直链无法获取 release body，需内嵌到清单中）
    changelog = _read_changelog(full_version)

    manifest: dict = {
        "manifest_version": 1,
        "app_version": full_version,
        "release_tag": release_tag,
        "changelog": changelog,
        "components": {},
    }

    # app-core 组件
    app_core_name = f"FluentYTDL-{full_version}-{arch}-app-core.7z"
    app_core_path = release_dir / app_core_name
    if app_core_path.exists():
        manifest["components"]["app-core"] = {
            "version": full_version,
            "url": f"{base_url}/{app_core_name}",
            "sha256": sha256_file(app_core_path),
            "size": app_core_path.stat().st_size,
            # 归档实际收录的顶层条目，与 build.py 的白名单同源（见 load_app_core_include）。
            # updater.exe 不在其中：它运行时被进程锁死无法覆写，改由归档里的
            # updater.exe.new 投递、由退出后的 helper 或新版 main.py 完成替换。
            "files": load_app_core_include(),
        }
        print(f"  app-core: {app_core_name} (SHA256 OK)")
    else:
        print(f"  ⚠ app-core 归档不存在: {app_core_name}")

    # bin/ 工具组件（从 assets/bin/ 检测版本）
    bin_versions = detect_component_versions(release_dir)
    for key, info in bin_versions.items():
        manifest["components"][f"bin/{key}"] = {
            "version": info["version"],
            "url": "",  # bin 工具由各工具的 GitHub API 提供下载 URL
            "sha256": "",
            "repo": info["repo"],
        }
        print(f"  bin/{key}: {info['version']}")

    return manifest


def main():
    parser = argparse.ArgumentParser(description="FluentYTDL 更新清单生成器")
    parser.add_argument(
        "--version",
        "-v",
        required=True,
        help="完整版本号，不带 v 前缀 (如 3.5.5, 3.5.6-rc.1)",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="GitHub Release tag (默认: v + 版本号)。资产下载 URL 用的是 tag 而非版本号。",
    )
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=ROOT / "release",
        help="发布资产目录 (默认: release/)",
    )
    parser.add_argument(
        "--base-url",
        default="https://github.com/SakuraForgot/FluentYTDL/releases/download/{tag}",
        help="下载 URL 前缀模板，{tag} 替换为 Release tag，{version} 替换为版本号",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出文件路径 (默认: release-dir/update-manifest.json)",
    )

    args = parser.parse_args()

    try:
        parse_version(args.version)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    version = strip_v_prefix(args.version)
    tag = args.tag or tag_for(version)
    base_url = args.base_url.format(tag=tag, version=version)
    output_path = args.output or (args.release_dir / "update-manifest.json")

    print(f"生成更新清单: {version}")
    print(f"  Release tag: {tag}")
    print(f"  发布目录: {args.release_dir}")
    print(f"  下载基址: {base_url}")

    manifest = generate_manifest(version, args.release_dir, base_url, release_tag=tag)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"\n✓ 清单已生成: {output_path}")
    print(f"  组件数量: {len(manifest['components'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
