#!/usr/bin/env python3
"""
FluentYTDL Build System - 纯 Python 构建脚本

用法:
    python scripts/build.py --target all
    python scripts/build.py --target setup
    python scripts/build.py --target full
    python scripts/build.py --target portable
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 修复 Windows 控制台 GBK 编码问题
# 确保可以正确输出 UTF-8 字符（包括 emoji）
if sys.platform == "win32":
    # 尝试设置控制台输出编码为 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    except Exception:
        # Python 3.6 或更早版本，或者其他环境
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
    """终止可能占用文件的进程"""
    for exe in exe_names:
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", exe],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass


def _safe_rmtree(path: Path, retries: int = 3, delay: float = 1.0) -> bool:
    """安全删除目录，带重试机制"""
    if not path.exists():
        return True

    for attempt in range(retries):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return True
        except PermissionError as e:
            if attempt < retries - 1:
                print(f"  ⚠ 删除失败 (attempt {attempt + 1}): {e}")
                print(f"    等待 {delay}s 后重试...")
                _terminate_processes(["FluentYTDL.exe", "yt-dlp.exe", "ffmpeg.exe", "deno.exe"])
                time.sleep(delay)
                delay *= 2
            else:
                print(f"  ❌ 无法删除: {path}")
                return False
        except Exception as e:
            print(f"  ❌ 删除错误: {e}")
            return False
    return False


def sha256_file(file_path: Path) -> str:
    """计算文件 SHA256"""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


# ============================================================================
# 版本信息生成
# ============================================================================

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
    """生成 PyInstaller 版本信息文件"""
    parts = version.lstrip("v").split(".")
    major = int(parts[0]) if len(parts) > 0 else 0
    minor = int(parts[1]) if len(parts) > 1 else 0
    patch_str = parts[2] if len(parts) > 2 else "0"
    # 处理可能的 -beta, -rc 等后缀
    patch = int("".join(c for c in patch_str if c.isdigit()) or "0")

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
# Builder 类
# ============================================================================


class Builder:
    """FluentYTDL 构建器"""

    def __init__(self, version: str | None = None):
        raw_version = version or self._get_version()
        # 确保版本号不带 'v' 前缀（统一格式）
        self.version = raw_version.lstrip("v")
        self.arch = "win64" if sys.maxsize > 2**32 else "win32"

    def _get_version(self) -> str:
        """从 pyproject.toml 读取版本号"""
        pyproject = ROOT / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version"):
                    # version = "1.0.18"
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        return parts[1].strip().strip('"').strip("'")
        return "0.0.0"

    def _generate_version_file(self) -> Path:
        """生成版本信息文件"""
        version_file = ROOT / "build" / "version_info.txt"
        generate_version_info(
            version=self.version,
            output_path=version_file,
        )
        print(f"  ✓ 版本信息文件: {version_file}")
        return version_file

    def clean(self) -> None:
        """清理构建目录"""
        print("🧹 清理构建目录...")

        # 终止可能占用的进程
        _terminate_processes(["FluentYTDL.exe", "yt-dlp.exe", "ffmpeg.exe", "deno.exe"])
        time.sleep(0.5)

        # 清理目录
        for d in [DIST_DIR, ROOT / "build"]:
            if d.exists():
                if _safe_rmtree(d):
                    print(f"  ✓ 已删除: {d}")
                else:
                    print(f"  ⚠ 跳过: {d}")

        print("✓ 清理完成")

    def ensure_tools(self) -> None:
        """确保外部工具已下载"""
        required = [
            "yt-dlp/yt-dlp.exe",
            "ffmpeg/ffmpeg.exe",
            "deno/deno.exe",
            "pot-provider/bgutil-pot-provider.exe",
            "atomicparsley/AtomicParsley.exe",
        ]
        missing = [t for t in required if not (ASSETS_BIN / t).exists()]
        if missing:
            print(f"⚠ 缺少工具: {missing}")
            print("  运行: python scripts/fetch_tools.py")
            fetch_script = ROOT / "scripts" / "fetch_tools.py"
            if fetch_script.exists():
                subprocess.run([sys.executable, str(fetch_script)], check=True)
            else:
                raise FileNotFoundError(f"工具下载脚本不存在: {fetch_script}")

    def _cleanup_runtime(self, runtime_dir: Path) -> None:
        """P2 优化: 清理 runtime 目录中不需要的文件"""
        if not runtime_dir.exists():
            return

        print("🧹 清理不需要的运行时文件...")
        cleaned_size = 0

        # 需要删除的文件列表 (相对于 runtime 目录)
        files_to_remove = [
            "opengl32sw.dll",  # 软件 OpenGL 渲染器 (~20 MB)，应用不需要
            "d3dcompiler_47.dll",  # Direct3D 编译器，通常不需要
        ]

        # 需要删除的目录列表
        dirs_to_remove = [
            "PySide6/qml",  # QML 运行时，应用不使用
            "PySide6/translations",  # Qt 翻译文件，应用有自己的翻译
        ]

        # 删除指定文件
        for filename in files_to_remove:
            file_path = runtime_dir / filename
            if file_path.exists():
                size = file_path.stat().st_size
                file_path.unlink()
                cleaned_size += size
                print(f"  ✓ 已删除: {filename} ({size / 1024 / 1024:.1f} MB)")

        # 删除指定目录
        for dirname in dirs_to_remove:
            dir_path = runtime_dir / dirname
            if dir_path.exists():
                size = sum(f.stat().st_size for f in dir_path.rglob("*") if f.is_file())
                shutil.rmtree(dir_path)
                cleaned_size += size
                print(f"  ✓ 已删除目录: {dirname} ({size / 1024 / 1024:.1f} MB)")

        # 清理 PySide6/plugins 中不需要的插件
        plugins_dir = runtime_dir / "PySide6" / "plugins"
        if plugins_dir.exists():
            # 不需要的插件类型
            unneeded_plugins = [
                "qmltooling",  # QML 调试
                "scenegraph",  # 场景图
                "qmllint",  # QML 检查
                "multimedia",  # 多媒体
                "position",  # 定位
            ]
            for plugin_name in unneeded_plugins:
                plugin_path = plugins_dir / plugin_name
                if plugin_path.exists():
                    size = sum(f.stat().st_size for f in plugin_path.rglob("*") if f.is_file())
                    shutil.rmtree(plugin_path)
                    cleaned_size += size
                    print(f"  ✓ 已删除插件: plugins/{plugin_name} ({size / 1024 / 1024:.1f} MB)")

        if cleaned_size > 0:
            print(f"✓ 已清理 {cleaned_size / 1024 / 1024:.1f} MB 不需要的文件")
        else:
            print("  (无需清理的文件)")

    def _compress_with_upx(self, target_dir: Path) -> None:
        """P3 优化: 使用 UPX 压缩可执行文件"""
        upx_path = ROOT / "tools" / "upx.exe"
        if not upx_path.exists():
            print("⚠ UPX 未找到，跳过压缩")
            return

        print("📦 使用 UPX 压缩文件...")

        # 不应该压缩的文件模式 (这些文件压缩后可能无法运行)
        skip_patterns = [
            "Qt6*.dll",  # Qt 核心 DLL 不建议压缩
            "python*.dll",  # Python DLL
            "api-ms-*.dll",  # Windows API DLL
            "vcruntime*.dll",  # VC 运行时
            "msvcp*.dll",  # MSVC 库
            "ucrtbase*.dll",  # Universal CRT
            "concrt*.dll",  # 并发运行时
            "libcrypto*.dll",  # OpenSSL
            "libssl*.dll",  # OpenSSL
            "shiboken*.pyd",  # Shiboken 绑定
        ]

        # 查找可压缩的文件
        files_to_compress = []
        for pattern in ["*.dll", "*.pyd"]:
            for f in target_dir.rglob(pattern):
                # 检查是否在跳过列表中
                skip = False
                for skip_pattern in skip_patterns:
                    if f.match(skip_pattern):
                        skip = True
                        break
                if not skip and f.stat().st_size > 100 * 1024:  # 只压缩大于 100KB 的文件
                    files_to_compress.append(f)

        if not files_to_compress:
            print("  (没有需要压缩的文件)")
            return

        total_before = sum(f.stat().st_size for f in files_to_compress)
        compressed_count = 0
        failed_count = 0

        for f in files_to_compress:
            before_size = f.stat().st_size
            try:
                result = subprocess.run(
                    [str(upx_path), "-q", "--best", str(f)], capture_output=True, timeout=60
                )
                if result.returncode == 0:
                    after_size = f.stat().st_size
                    saved = before_size - after_size
                    if saved > 0:
                        compressed_count += 1
                else:
                    failed_count += 1
            except (subprocess.TimeoutExpired, Exception):
                failed_count += 1

        total_after = sum(f.stat().st_size for f in files_to_compress if f.exists())
        saved = total_before - total_after

        print(f"✓ UPX 压缩完成: {compressed_count} 个文件")
        if failed_count > 0:
            print(f"  ⚠ {failed_count} 个文件跳过/失败")
        print(f"  节省空间: {saved / 1024 / 1024:.1f} MB")

    def build_onedir(self) -> Path:
        """构建 onedir 模式 (用于安装包和完整版)"""
        self.clean()
        output = DIST_DIR / "FluentYTDL"

        version_file = self._generate_version_file()

        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--noconsole",
            "--name",
            "FluentYTDL",
            "--onedir",
            "--contents-directory",
            "runtime",
            "--paths",
            str(ROOT / "src"),
            "--icon",
            str(ROOT / "assets" / "logo.ico"),
            "--version-file",
            str(version_file),
            "--add-data",
            f"{ROOT / 'docs'}{os.pathsep}docs",
            # 只打包必要的 assets 文件，排除 assets/bin（外部工具由 bundle_tools 单独复制）
            "--add-data",
            f"{ROOT / 'assets' / 'logo.ico'}{os.pathsep}assets",
            "--add-data",
            f"{ROOT / 'assets' / 'logo.png'}{os.pathsep}assets",
            # 手动添加非标准 Python 结构的 yt-dlp 插件目录
            "--add-data",
            f"{ROOT / 'src' / 'fluentytdl' / 'yt_dlp_plugins_ext'}{os.pathsep}fluentytdl/yt_dlp_plugins_ext",
            # 自动收集所有子模块（推荐方式）
            "--collect-submodules",
            "fluentytdl",
            "--collect-submodules",
            "rookiepy",
            # 复制二进制文件
            "--copy-metadata",
            "rookiepy",
            # 排除未使用的 PySide6 模块以减小体积
            "--exclude-module",
            "PySide6.QtQml",
            "--exclude-module",
            "PySide6.QtQuick",
            "--exclude-module",
            "PySide6.QtQuickWidgets",
            "--exclude-module",
            "PySide6.QtPdf",
            "--exclude-module",
            "PySide6.QtPdfWidgets",
            "--exclude-module",
            "PySide6.Qt3DCore",
            "--exclude-module",
            "PySide6.Qt3DRender",
            "--exclude-module",
            "PySide6.QtWebEngine",
            "--exclude-module",
            "PySide6.QtWebEngineWidgets",
            "--exclude-module",
            "PySide6.QtMultimedia",
            "--exclude-module",
            "PySide6.QtBluetooth",
            "--exclude-module",
            "PySide6.QtPositioning",
            "--workpath",
            str(ROOT / "build"),
            "--distpath",
            str(DIST_DIR),
            str(ROOT / "main.py"),
        ]

        print("🔨 构建 onedir 版本...")
        subprocess.run(cmd, check=True)

        if not output.exists():
            raise RuntimeError(f"构建失败: {output} 不存在")

        # P2 优化: 清理不需要的文件
        self._cleanup_runtime(output / "runtime")

        # P3 优化: UPX 压缩
        self._compress_with_upx(output / "runtime")

        print(f"✓ onedir 构建完成: {output}")
        return output

    def build_onefile(self) -> Path:
        """构建 onefile 模式 (便携版)"""
        self.clean()
        output = DIST_DIR / "FluentYTDL.exe"

        version_file = self._generate_version_file()

        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--noconsole",
            "--name",
            "FluentYTDL",
            "--onefile",
            "--paths",
            str(ROOT / "src"),
            "--icon",
            str(ROOT / "assets" / "logo.ico"),
            "--version-file",
            str(version_file),
            "--add-data",
            f"{ROOT / 'docs'}{os.pathsep}docs",
            # 只打包必要的 assets 文件，排除 assets/bin
            "--add-data",
            f"{ROOT / 'assets' / 'logo.ico'}{os.pathsep}assets",
            "--add-data",
            f"{ROOT / 'assets' / 'logo.png'}{os.pathsep}assets",
            # 手动添加非标准 Python 结构的 yt-dlp 插件目录
            "--add-data",
            f"{ROOT / 'src' / 'fluentytdl' / 'yt_dlp_plugins_ext'}{os.pathsep}fluentytdl/yt_dlp_plugins_ext",
            # 自动收集所有子模块（推荐方式）
            "--collect-submodules",
            "fluentytdl",
            "--collect-submodules",
            "rookiepy",
            # 复制二进制文件
            "--copy-metadata",
            "rookiepy",
            "--workpath",
            str(ROOT / "build"),
            "--distpath",
            str(DIST_DIR),
            str(ROOT / "main.py"),
        ]

        print("🔨 构建 onefile 版本...")
        subprocess.run(cmd, check=True)

        if not output.exists():
            raise RuntimeError(f"构建失败: {output} 不存在")

        print(f"✓ onefile 构建完成: {output}")
        return output

    def bundle_tools(self, target_dir: Path) -> None:
        """将外部工具复制到目标目录"""
        # 复制工具
        bin_dest = target_dir / "bin"
        if ASSETS_BIN.exists():
            shutil.copytree(ASSETS_BIN, bin_dest, dirs_exist_ok=True)
            print(f"✓ 已捆绑工具到: {bin_dest}")
        else:
            print("⚠ 未找到外部工具目录，跳过捆绑")

        # 复制许可证
        if LICENSES_DIR.exists():
            licenses_dest = target_dir / "licenses"
            shutil.copytree(LICENSES_DIR, licenses_dest, dirs_exist_ok=True)
            print(f"✓ 已捆绑许可证到: {licenses_dest}")

    def create_7z(self, source_dir: Path, output_name: str) -> Path:
        """创建 7z 压缩包"""
        RELEASE_DIR.mkdir(exist_ok=True)
        output_path = RELEASE_DIR / f"{output_name}.7z"

        # 如果已存在则删除
        if output_path.exists():
            output_path.unlink()

        # 优先使用系统 7z，否则回退到 py7zr
        sevenzip = shutil.which("7z") or shutil.which("7za")
        if sevenzip:
            cmd = [sevenzip, "a", "-t7z", "-mx=9", "-mmt=on", str(output_path), "."]
            subprocess.run(cmd, check=True, cwd=source_dir)
        else:
            try:
                import importlib

                py7zr = importlib.import_module("py7zr")
                with py7zr.SevenZipFile(output_path, "w") as archive:
                    archive.writeall(source_dir, arcname=".")
            except ImportError as e:
                raise RuntimeError("需要安装 py7zr 或系统 7z: pip install py7zr") from e

        print(f"✓ 已创建压缩包: {output_path}")
        return output_path

    def build_setup(self, source_dir: Path) -> Path:
        """调用 Inno Setup 构建安装包"""
        iss_file = INSTALLER_DIR / "FluentYTDL.iss"
        if not iss_file.exists():
            raise FileNotFoundError(
                f"Inno Setup 脚本不存在: {iss_file}\n请先创建 installer/FluentYTDL.iss"
            )

        # 查找 Inno Setup 编译器
        iscc_paths = [
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Inno Setup 6"
            / "ISCC.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Inno Setup 6" / "ISCC.exe",
            Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
            Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
        ]
        iscc = next((p for p in iscc_paths if p.exists()), None)

        if not iscc:
            raise RuntimeError(
                "未找到 Inno Setup 编译器 (ISCC.exe)\n"
                "请从 https://jrsoftware.org/isinfo.php 下载安装 Inno Setup 6"
            )

        RELEASE_DIR.mkdir(exist_ok=True)
        output_name = f"FluentYTDL-v{self.version}-{self.arch}-setup"

        cmd = [
            str(iscc),
            f"/DMyAppVersion={self.version}",
            f"/DSourceDir={source_dir}",
            f"/DOutputDir={RELEASE_DIR}",
            f"/DOutputBaseFilename={output_name}",
            str(iss_file),
        ]

        print("🔨 构建安装包...")
        subprocess.run(cmd, check=True)

        output_path = RELEASE_DIR / f"{output_name}.exe"
        if not output_path.exists():
            raise RuntimeError(f"安装包构建失败: {output_path} 不存在")

        print(f"✓ 已创建安装包: {output_path}")
        return output_path

    def generate_checksums(self) -> Path:
        """生成 SHA256 校验文件"""
        checksums = []

        for file in sorted(RELEASE_DIR.iterdir()):
            if file.is_file() and file.suffix in {".exe", ".7z", ".zip"}:
                hash_value = sha256_file(file)
                checksums.append(f"{hash_value}  {file.name}")
                print(f"  {file.name}: {hash_value[:16]}...")

        checksum_file = RELEASE_DIR / "SHA256SUMS.txt"
        checksum_file.write_text("\n".join(checksums) + "\n", encoding="utf-8")

        print(f"✓ 校验文件已生成: {checksum_file}")
        return checksum_file

    # === 高级构建目标 ===

    def target_portable(self) -> Path:
        """构建便携版 (单文件 EXE)"""
        print("\n" + "=" * 50)
        print("📦 构建目标: 便携版 (Portable)")
        print("=" * 50)

        exe_path = self.build_onefile()

        # 重命名并移动到 release
        RELEASE_DIR.mkdir(exist_ok=True)
        final_name = f"FluentYTDL-v{self.version}-{self.arch}-portable.exe"
        final_path = RELEASE_DIR / final_name

        if final_path.exists():
            final_path.unlink()
        shutil.copy2(exe_path, final_path)

        print(f"✅ 便携版完成: {final_path}")
        return final_path

    def target_full(self) -> Path:
        """构建完整版 (onedir + 工具 -> 7z)"""
        print("\n" + "=" * 50)
        print("📦 构建目标: 完整版 (Full)")
        print("=" * 50)

        self.ensure_tools()
        app_dir = self.build_onedir()
        self.bundle_tools(app_dir)

        output_name = f"FluentYTDL-v{self.version}-{self.arch}-full"
        archive_path = self.create_7z(app_dir, output_name)

        print(f"✅ 完整版完成: {archive_path}")
        return archive_path

    def target_setup(self) -> Path:
        """构建安装包"""
        print("\n" + "=" * 50)
        print("📦 构建目标: 安装包 (Setup)")
        print("=" * 50)

        self.ensure_tools()
        app_dir = self.build_onedir()
        self.bundle_tools(app_dir)

        setup_path = self.build_setup(app_dir)

        print(f"✅ 安装包完成: {setup_path}")
        return setup_path

    def target_all(self) -> dict[str, Path]:
        """构建所有目标"""
        print("\n" + "=" * 50)
        print("📦 构建目标: 全部 (All)")
        print(f"   版本: {self.version}")
        print(f"   架构: {self.arch}")
        print("=" * 50)

        results: dict[str, Path] = {}

        # 1. 便携版 (独立构建，不依赖工具)
        results["portable"] = self.target_portable()

        # 2. 完整版和安装包 (共享 onedir 构建)
        self.ensure_tools()
        app_dir = self.build_onedir()
        self.bundle_tools(app_dir)

        output_name = f"FluentYTDL-v{self.version}-{self.arch}-full"
        results["full"] = self.create_7z(app_dir, output_name)

        # 3. 安装包 (检查 iss 是否存在)
        iss_file = INSTALLER_DIR / "FluentYTDL.iss"
        if iss_file.exists():
            results["setup"] = self.build_setup(app_dir)
        else:
            print(f"⚠ 跳过安装包构建: {iss_file} 不存在")

        # 4. 生成校验文件
        print("\n📋 生成 SHA256 校验文件...")
        results["checksums"] = self.generate_checksums()

        print("\n" + "=" * 50)
        print("🎉 全部构建完成!")
        print("=" * 50)
        for target, path in results.items():
            print(f"  [{target}] {path}")

        return results


# ============================================================================
# 主入口
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="FluentYTDL 构建系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/build.py --target all
  python scripts/build.py --target portable
  python scripts/build.py --target full
  python scripts/build.py --target setup
  python scripts/build.py --target all --version v1.0.19
        """,
    )
    parser.add_argument(
        "--target",
        "-t",
        choices=["all", "setup", "full", "portable"],
        default="all",
        help="构建目标 (默认: all)",
    )
    parser.add_argument(
        "--version",
        "-v",
        help="覆盖版本号 (默认从 pyproject.toml 读取)",
    )
    args = parser.parse_args()

    # 环境变量覆盖
    version = args.version or os.environ.get("PACKAGE_VERSION")

    builder = Builder(version=version)

    print("FluentYTDL Build System")
    print(f"Python: {sys.version}")
    print(f"Version: {builder.version}")
    print(f"Target: {args.target}")
    print()

    target_methods = {
        "all": builder.target_all,
        "setup": builder.target_setup,
        "full": builder.target_full,
        "portable": builder.target_portable,
    }

    try:
        target_methods[args.target]()
    except Exception as e:
        print(f"\n❌ 构建失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
