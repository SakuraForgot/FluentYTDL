#!/usr/bin/env python3
"""
FluentYTDL Build System - 现代化构建编排器
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 修复 Windows 控制台 GBK 编码问题
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

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "dist"
RELEASE_DIR = ROOT / "release"
ASSETS_BIN = ROOT / "assets" / "bin"
INSTALLER_DIR = ROOT / "installer"
LICENSES_DIR = ROOT / "licenses"


# ============================================================================
# 工具函数
# ============================================================================


def _terminate_processes(exe_names: list[str]) -> None:
    for exe in exe_names:
        try:
            subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True, timeout=5)
        except Exception:
            pass


def _safe_rmtree(path: Path, retries: int = 3, delay: float = 1.0) -> bool:
    if not path.exists():
        return True
    for attempt in range(retries):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return True
        except PermissionError:
            if attempt < retries - 1:
                _terminate_processes(["FluentYTDL.exe", "yt-dlp.exe", "ffmpeg.exe", "deno.exe"])
                time.sleep(delay)
                delay *= 2
            else:
                return False
        except Exception:
            return False
    return False


def sha256_file(file_path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


VERSION_INFO_TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404b0',
          [
            StringStruct('CompanyName', '{company}'),
            StringStruct('FileDescription', '{description}'),
            StringStruct('FileVersion', '{version}'),
            StringStruct('InternalName', '{internal_name}'),
            StringStruct('LegalCopyright', '{copyright}'),
            StringStruct('OriginalFilename', '{original_filename}'),
            StringStruct('ProductName', '{product_name}'),
            StringStruct('ProductVersion', '{version}'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def generate_version_info(
    version: str,
    output_path: Path,
    company: str = "FluentYTDL Team",
    description: str = "FluentYTDL - 专业 YouTube 下载器",
    product_name: str = "FluentYTDL",
    copyright_text: str = "Copyright (C) 2024-2026 FluentYTDL Team",
    internal_name: str = "FluentYTDL",
    original_filename: str = "FluentYTDL.exe",
) -> Path:
    # 兼容 beta0.0.1, v1.2.3, 1.2.3-beta 等任意格式版本号
    nums = re.findall(r"\d+", version)
    major = int(nums[0]) if len(nums) > 0 else 0
    minor = int(nums[1]) if len(nums) > 1 else 0
    patch = int(nums[2]) if len(nums) > 2 else 0

    content = VERSION_INFO_TEMPLATE.format(
        major=major,
        minor=minor,
        patch=patch,
        version=version.lstrip("v"),
        company=company,
        description=description,
        product_name=product_name,
        copyright=copyright_text,
        internal_name=internal_name,
        original_filename=original_filename,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


# ============================================================================
# 构建编排器
# ============================================================================


class Builder:
    def __init__(self, override_version: str | None = None, skip_hygiene: bool = False):
        self.arch = "win64" if sys.maxsize > 2**32 else "win32"
        self.skip_hygiene = skip_hygiene
        self.config = self._load_config()
        self.version = (override_version or self.config.get("version", "0.0.0")).lstrip("v")

    def _load_config(self) -> dict:
        pyproject = ROOT / "pyproject.toml"
        cfg = {}
        if not pyproject.exists():
            return cfg

        try:
            import tomllib

            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
                cfg["version"] = data.get("project", {}).get("version", "0.0.0")
                b_cfg = data.get("tool", {}).get("fluentytdl", {}).get("build", {})
                cfg.update(b_cfg)
                return cfg
        except ImportError:
            # Fallback 粗糙解析
            content = pyproject.read_text(encoding="utf-8")
            in_build = False
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("version =") and not in_build:
                    cfg["version"] = line.split("=", 1)[1].strip(" '\"")
                if line == "[tool.fluentytdl.build]":
                    in_build = True
                    continue
                elif line.startswith("["):
                    in_build = False
                if in_build and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if v == "true":
                        cfg[k] = True
                    elif v == "false":
                        cfg[k] = False
                    elif v.startswith("[") and v.endswith("]"):
                        items = [x.strip(" '\"") for x in v[1:-1].split(",") if x.strip()]
                        cfg[k] = items
                    else:
                        cfg[k] = v.strip(" '\"")
            return cfg

    def _check_hygiene(self):
        """确保在一个干净的打包环境中"""
        if self.skip_hygiene or not self.config.get("strict_env_check", True):
            print("  ⚠️ 已跳过环境污染检测")
            return

        print("🩺 正在进行环境体检...")
        import importlib.metadata

        installed = {dist.metadata["Name"].lower() for dist in importlib.metadata.distributions()}
        blacklist = set(pkg.lower() for pkg in self.config.get("env_blacklist", []))

        found = installed.intersection(blacklist)
        if found:
            print(f"  ❌ 严重警告: 构建环境被污染！发现黑名单依赖: {', '.join(found)}")
            print("  这会导致打包产物极其臃肿并有可能引起杀软误报。")
            print("  请在干净的 venv 环境中重试。")
            print("  （如需无视警告强行打包，请传递 --skip-hygiene 参数）")
            sys.exit(1)
        print("  ✓ 环境干净，准许打包")

    def clean(self) -> None:
        print("🧹 清理历史构建...")
        _terminate_processes(["FluentYTDL.exe", "yt-dlp.exe", "ffmpeg.exe", "deno.exe"])
        time.sleep(0.5)
        for d in [DIST_DIR, ROOT / "build"]:
            if d.exists() and _safe_rmtree(d):
                print(f"  ✓ 已删除: {d.name}")

    def ensure_tools(self) -> None:
        required = ["yt-dlp/yt-dlp.exe", "ffmpeg/ffmpeg.exe"]
        missing = [t for t in required if not (ASSETS_BIN / t).exists()]
        if missing:
            print("⚠ 缺少必备工具环境，自动拉取...")
            fetch_script = ROOT / "scripts" / "fetch_tools.py"
            if fetch_script.exists():
                subprocess.run([sys.executable, str(fetch_script)], check=True)
            else:
                raise FileNotFoundError(f"工具下载脚本不存在: {fetch_script}")

    def build_spec(self) -> Path:
        """根据 FluentYTDL.spec 核心蓝图进行构建"""
        self.clean()
        self._check_hygiene()
        self.ensure_tools()

        version_file = ROOT / "build" / "version_info.txt"
        generate_version_info(self.version, version_file)

        spec_file = ROOT / "scripts" / "FluentYTDL.spec"
        if not spec_file.exists():
            raise FileNotFoundError(f"缺少打包蓝图: {spec_file}")

        # 将 TOML 配置投射到系统环境变量中以通信给 .spec 文件
        env = os.environ.copy()
        env["FLUENTYTDL_VERSION_FILE"] = str(version_file)
        env["FLUENTYTDL_QT_EXCLUDES"] = ",".join(self.config.get("qt_excludes", []))

        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--workpath",
            str(ROOT / "build"),
            "--distpath",
            str(ROOT / "dist"),
            str(spec_file),
        ]

        print(f"🔨 使用 PyInstaller 编译 (版本: {self.version})...")
        subprocess.run(cmd, env=env, check=True, cwd=ROOT)

        output = DIST_DIR / "FluentYTDL"
        if not output.exists():
            raise ChildProcessError("构建异常：未生成对应文件夹。")

        print(f"✓ .spec 构建落地: {output}")
        return output

    def bundle_tools(self, target_dir: Path) -> None:
        excluded_tool_dirs = {"dle_user", "dle_profile", "profile", "profiles", "cookies"}
        bin_dest = target_dir / "bin"
        if ASSETS_BIN.exists():

            def _ignore_tool_user_data(_src: str, names: list[str]) -> set[str]:
                return {name for name in names if name.lower() in excluded_tool_dirs}

            shutil.copytree(ASSETS_BIN, bin_dest, dirs_exist_ok=True, ignore=_ignore_tool_user_data)
            print("✓ 捆绑工具至 bin (已排除会话数据)")

        if LICENSES_DIR.exists():
            shutil.copytree(LICENSES_DIR, target_dir / "licenses", dirs_exist_ok=True)

    def create_7z(self, source_dir: Path, output_name: str) -> Path:
        RELEASE_DIR.mkdir(exist_ok=True)
        output_path = RELEASE_DIR / f"{output_name}.7z"
        if output_path.exists():
            output_path.unlink()

        sevenzip = shutil.which("7z") or shutil.which("7za")
        if sevenzip:
            subprocess.run(
                [sevenzip, "a", "-t7z", "-mx=9", "-mmt=on", str(output_path), "."],
                check=True,
                cwd=source_dir,
            )
        else:
            import importlib

            py7zr = importlib.import_module("py7zr")
            with py7zr.SevenZipFile(output_path, "w") as archive:
                archive.writeall(source_dir, arcname=".")

        print(f"📦 压缩包: {output_path.name}")
        return output_path

    def build_setup(self, source_dir: Path) -> Path:
        iss_file = INSTALLER_DIR / "FluentYTDL.iss"
        if not iss_file.exists():
            return Path()

        iscc_paths = [
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Inno Setup 6/ISCC.exe",
            Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
            Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
        ]
        iscc = next((p for p in iscc_paths if p.exists()), None)
        if not iscc:
            return Path()

        RELEASE_DIR.mkdir(exist_ok=True)
        out_name = f"FluentYTDL-v{self.version}-{self.arch}-setup"
        cmd = [
            str(iscc),
            f"/DMyAppVersion={self.version}",
            f"/DSourceDir={source_dir}",
            f"/DOutputDir={RELEASE_DIR}",
            f"/DOutputBaseFilename={out_name}",
            str(iss_file),
        ]

        print("📦 正在编译安装向导程序...")
        subprocess.run(cmd, check=True)
        print(f"📦 安装包: {out_name}.exe")
        return RELEASE_DIR / f"{out_name}.exe"

    def run_all(self, target: str = "all") -> None:
        print(f"========== FluentYTDL Pipelined Build v{self.version} ==========")

        # 1. 编译核心依赖
        app_dir = self.build_spec()

        # 2. 注入二进制工具
        self.bundle_tools(app_dir)

        # 3. 产物分发
        print("\n========== Release 打包 ==========")
        results = []

        # 完整绿化包
        if target in ("all", "7z"):
            full_archive = self.create_7z(app_dir, f"FluentYTDL-v{self.version}-{self.arch}-full")
            results.append(full_archive)

        # Inno 安装包
        if target in ("all", "setup"):
            setup_exe = self.build_setup(app_dir)
            if setup_exe and setup_exe.exists():
                results.append(setup_exe)

        # 计算全局指纹
        self.generate_checksums()

        print("\n✅ 流水线完成！")
        for res in results:
            print(f"   ► {res.name}")

    def generate_checksums(self):
        checksums = []
        for file in sorted(RELEASE_DIR.iterdir()):
            if file.is_file() and file.suffix in {".exe", ".7z", ".zip"}:
                hash_value = sha256_file(file)
                checksums.append(f"{hash_value}  {file.name}")

        checksum_file = RELEASE_DIR / "SHA256SUMS.txt"
        checksum_file.write_text("\n".join(checksums) + "\n", encoding="utf-8")


# ============================================================================
# Entry Point
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description="FluentYTDL 构建中枢系统")
    parser.add_argument(
        "--target", "-t", choices=["all", "7z", "setup"], default="all", help="构建目标 (默认: all)"
    )
    parser.add_argument("--version", "-v", help="覆盖打包版本号")
    parser.add_argument("--skip-hygiene", action="store_true", help="强制无视黑名单环境污染告警")

    args = parser.parse_args()

    builder = Builder(override_version=args.version, skip_hygiene=args.skip_hygiene)

    try:
        builder.run_all(target=args.target)
    except Exception as e:
        print(f"\n❌ 流水线崩溃: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
