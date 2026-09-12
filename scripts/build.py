#!/usr/bin/env python3
"""
FluentYTDL Build System - 现代化构建编排器
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime
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

# 版本解析统一走 version_manager，避免出现第二份规则实现
sys.path.insert(0, str(Path(__file__).resolve().parent))
from archive_compat import (  # noqa: E402
    CLI_OPTIONS,
    python_filters,
    verify_archive,
    verify_frozen_updater,
)
from fetch_tools import load_tool_versions  # noqa: E402
from version_manager import parse_version, strip_v_prefix, tag_for  # noqa: E402

# ============================================================================
# 目标 → 产出物映射（唯一事实源）
#
# run_all() 的分发、_assert_expected_artifacts() 的断言、build_gui.py 的产物
# 清单全都读这一张表。以前是三处各写一遍 `if target in ("all", "7z")`，于是
# 「只要便携版」实际还会吐出 app-core.7z、update-manifest.json 和一份把
# release/ 里所有历史遗留文件都列进去的 SHA256SUMS.txt —— 目标形同虚设。
#
# 五种产出物：
#   full      FluentYTDL-{v}-{arch}-full.7z      便携完整版
#   app-core  FluentYTDL-{v}-{arch}-app-core.7z  增量更新内部包
#   setup     FluentYTDL-{v}-{arch}-setup.exe    Inno Setup 安装向导
#   manifest  update-manifest.json               程序内更新器读的清单
#   checksums SHA256SUMS.txt                     本次产物的校验和
#
# manifest 绑定 app-core：清单里唯一有实质内容的组件就是 app-core，缺了它
# generate_manifest.py 只会打印「⚠ app-core 归档不存在」并写出一份没有
# app-core 组件的空壳清单 —— 那种清单一旦发布，程序内更新器会认为新版本
# 无可下载载荷。所以宁可不生成，也不生成空壳。
# ============================================================================

TARGET_OUTPUTS: dict[str, frozenset[str]] = {
    "all": frozenset({"full", "app-core", "setup", "manifest", "checksums"}),
    "7z": frozenset({"full"}),
    "app-core": frozenset({"app-core", "manifest"}),
    "setup": frozenset({"setup"}),
}

# CLI 别名。"spec" 不在 TARGET_OUTPUTS 里 —— 它在产物分发之前就 return 了
TARGET_ALIASES = {"full": "7z"}

# 产出物 kind → 文件名模板。{base} = FluentYTDL-{版本}-{架构}
OUTPUT_FILENAMES = {
    "full": "{base}-full.7z",
    "app-core": "{base}-app-core.7z",
    "setup": "{base}-setup.exe",
    "manifest": "update-manifest.json",
    "checksums": "SHA256SUMS.txt",
}


def release_names(target: str, version: str, arch: str = "win64") -> dict[str, str]:
    """目标 → {产出物 kind: 文件名}。

    模块级纯函数，`build_gui.py` 直接 import 这一份 —— GUI 以前自己抄了一张
    产物清单，和 run_all() 的真实行为对不上，用户看到的"本次产出"是假的。
    """
    effective = TARGET_ALIASES.get(target, target)
    base = f"FluentYTDL-{version}-{arch}"
    return {
        kind: OUTPUT_FILENAMES[kind].format(base=base)
        for kind in sorted(TARGET_OUTPUTS.get(effective, frozenset()))
    }


# ============================================================================
# 工具函数
# ============================================================================


def _build_timestamp() -> str:
    """构建时刻（UTC, ISO 8601）。

    尊重 SOURCE_DATE_EPOCH —— 可复现构建的事实标准，设置后同一份源码
    两次构建的 BUILD_INFO.json 完全一致。
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.isdigit():
        dt = datetime.fromtimestamp(int(epoch), tz=UTC)
    else:
        dt = datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_commit() -> str:
    """当前 HEAD 的短 commit；非 git 环境（如从 sdist 构建）返回 unknown。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return (proc.stdout or "").strip() or "unknown"


def source_fingerprint() -> str:
    digest = hashlib.sha256(subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT))
    digest.update(_git_commit().encode())
    for name in (
        subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode("utf-8")
        .split("\0")
    ):
        if name and (ROOT / name).is_file():
            digest.update(name.encode())
            digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def _dist_version(name: str) -> str:
    """已安装发行包的版本；未安装返回 unknown。"""
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:
        return "unknown"


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
# 发布物内容校验
#
# 两个独立的问题，故意分成两个函数（不要再合并回去）：
#   assert_dist_clean()        —— dist 里有没有**运行期垃圾**？黑名单，三个发布
#                                 目标（full.7z / app-core / setup.exe）全都调。
#   classify_app_core_items()  —— app-core 该收哪些？白名单，只有 app-core 调。
#
# full.7z 合法地包含 bin/ 与 updater.exe，套不了 app-core 的白名单；但它同样
# 不允许夹带 config.json 或 bin/cookies_*.txt —— 后者进了公开发布包等于泄漏
# 用户的真实 YouTube 会话。
#
# 都是模块级纯函数（不进 Builder），便于 tests/test_build_app_core.py 用
# importlib 按路径加载后直接调用。
# ============================================================================


def classify_app_core_items(
    names: list[str],
    include: list[str],
    exclude: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """把 dist 顶层条目分成 (keep, drop, unknown) 三类。

    白名单语义：**只有** include 里的条目会进 app-core 归档。
    exclude 是"已知且故意不收"的显式登记，存在的唯一目的是让 unknown
    真正只剩下意料之外的东西。

    unknown 非空即构建期硬失败 —— 白名单最大的风险是"以后新增的合法发布物
    被静默丢掉"，这条断言把它变成一盏红灯。
    """
    inc, exc = set(include), set(exclude)
    keep: list[str] = []
    drop: list[str] = []
    unknown: list[str] = []
    for name in sorted(names):
        if name in inc:
            keep.append(name)
        elif name in exc:
            drop.append(name)
        else:
            unknown.append(name)
    return keep, drop, unknown


def assert_dist_clean(dist_dir: Path, forbidden: list[str]) -> None:
    """dist 目录里若出现运行期产物就中止构建。

    成因：任何人在打包前从 dist/ 直接启动过程序，那台机器的 config.json /
    logs/ / state/tasks/tasks.db 就会留在 dist 里，随后被打进发布归档。
    后果有两层 —— updater 应用归档时会用开发者的 config 覆盖用户的，
    而 bin/cookies_*.txt、bin/dle_user/ 里是**真实凭据**。

    匹配规则（故意不做无条件递归）：
      * 含 `*` 的条目按 glob 处理（`**/` 开头则递归）
      * 其余按**相对 dist 根的精确路径**匹配，因此 "config.json" 与
        "bin/dle_user" 都能直接写

    不递归匹配裸名字是刻意的：`_internal/` 里有几千个第三方包的资源文件，
    随便一个叫 config.json 的包数据就会把整条发布链误判成"被污染"。
    运行期产物落在哪些相对路径是完全可枚举的，精确匹配才不会误伤。
    需要递归时在配置里显式写 `**/name`。
    """
    if not dist_dir.exists():
        return

    hits: list[str] = []
    for rel in forbidden:
        pattern = rel.replace("\\", "/")
        if "*" in pattern:
            if next(dist_dir.glob(pattern), None) is not None:
                hits.append(rel)
        elif (dist_dir / pattern).exists():
            hits.append(rel)

    if hits:
        raise RuntimeError(
            "发布目录被运行期产物污染，构建中止：\n"
            + "\n".join(f"  ✗ {dist_dir.name}/{h}" for h in hits)
            + "\n\n这些文件会被打进发布归档 —— 用户的配置会被开发者的覆盖，"
            "\nbin/ 下的 cookies 与登录 profile 则是真实凭据，绝不能进公开发布包。"
            f"\n\n修复：删掉 {dist_dir} 后重新构建（不要只删这几个文件，"
            "\n      dist 已经被运行过一次，可能还有其他残留）。"
        )


# ============================================================================
# 构建编排器
# ============================================================================


class Builder:
    def __init__(
        self,
        override_version: str | None = None,
        skip_hygiene: bool = False,
        strict_tools: bool = False,
        replay_snapshot: Path | None = None,
    ):
        self.arch = "win64" if sys.maxsize > 2**32 else "win32"
        self.skip_hygiene = skip_hygiene
        self.strict_tools = strict_tools
        self.replay_snapshot = replay_snapshot
        self.snapshot = {}
        self.config = self._load_config()
        # 是否由调用方显式指定版本 —— 决定 VERSION 文件是否可被回写（见 _sync_version_to_all）
        self._version_overridden = override_version is not None
        # 完整版本（含 -rc.N / -beta.N 后缀），不带 "v" 前缀
        raw_version = override_version or self.config.get("version", "0.0.0")
        try:
            numeric, channel = parse_version(raw_version)
        except ValueError as e:
            print(f"❌ {e}")
            sys.exit(1)
        self._full_version = strip_v_prefix(raw_version)
        self.channel = channel
        self.version = numeric  # PE 资源 / Inno Setup 用的纯数字版本
        self.tag = tag_for(self._full_version)

    def _load_config(self) -> dict:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return {
            **data["tool"]["fluentytdl"]["build"],
            "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        }

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

    def _sync_version_to_all(self) -> None:
        """Stage VERSION for PyInstaller; never modify a source version carrier."""
        version = self.work_dir / "VERSION"
        version.write_text(self._full_version + "\n", encoding="utf-8")
        os.environ["FLUENTYTDL_VERSION_SOURCE"] = str(version)

    def clean(self) -> None:
        """A fresh UUID workspace has no historical outputs to delete."""
        if not self.work_dir.resolve().is_relative_to((ROOT / "build/runs").resolve()):
            raise RuntimeError("Build workspace escaped build/runs")

    def ensure_tools(self) -> None:
        from component_snapshot import prepare, replay, verify

        if getattr(self, "snapshot_path", None):
            return
        directory = self.work_dir / "components"
        self.snapshot_path = (
            replay(self.replay_snapshot, directory) if self.replay_snapshot else prepare(directory)
        )
        self.snapshot = verify(self.snapshot_path)
        global ASSETS_BIN
        ASSETS_BIN = directory / "bin"
        os.environ["FLUENTYTDL_COMPONENT_SNAPSHOT"] = str(self.snapshot_path)
        os.environ["FLUENTYTDL_ASSETS_BIN"] = str(ASSETS_BIN)
        os.environ["FLUENTYTDL_7ZIP_DIR"] = str(directory / "7zip")

    def build_spec(self) -> Path:
        """根据 FluentYTDL.spec 核心蓝图进行构建。

        这里刻意不调用 ensure_tools()：assets/bin 下的外部工具是 bundle_tools()
        的输入，.spec 蓝图并不引用它们。分开之后 CI 可以只跑 --target spec 验证
        PyInstaller 配置，不必先下载上百 MB 的 ffmpeg。
        """
        self._sync_version_to_all()
        self.clean()
        self._check_hygiene()

        # 编译翻译文件
        print("🌐 正在编译多语言翻译文件...")
        i18n_script = ROOT / "scripts" / "i18n_release.py"
        if i18n_script.exists():
            subprocess.run([sys.executable, str(i18n_script)], check=True)
        else:
            print("  ⚠️ 未找到翻译构建脚本，跳过...")

        version_file = self.work_dir / "version_info.txt"
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
            str(self.work_dir / "pyinstaller"),
            "--distpath",
            str(DIST_DIR),
            str(spec_file),
        ]

        print(f"🔨 使用 PyInstaller 编译 (版本: {self.version})...")
        subprocess.run(cmd, env=env, check=True, cwd=ROOT)

        output = DIST_DIR / "FluentYTDL"
        if not output.exists():
            raise ChildProcessError("构建异常：未生成对应文件夹。")

        # 清理 PyInstaller 未能通过 excludes 彻底拦截的 Qt 残留
        self.strip_qt_bloat(output)

        print(f"✓ .spec 构建落地: {output}")
        return output

    def strip_qt_bloat(self, target_dir: Path) -> None:
        """清理 _internal/PySide6 中确定不需要的文件。

        PyInstaller 的 excludes 只能阻止 Python 模块（.pyd）被收集，
        但无法阻止对应的 C++ DLL 和资源文件被拖入。
        此函数在构建后物理删除这些残留。
        """
        pyside_dir = target_dir / "_internal" / "PySide6"
        if not pyside_dir.exists():
            print("  ⚠️ 未找到 PySide6 目录，跳过清理")
            return

        saved = 0

        # ── 1. WebEngine 相关（最大头，约 390 MB） ──
        webengine_patterns = [
            "Qt6WebEngine*.dll",
            "qtwebengine_*.pak",
            "QtWebEngine*.pyd",
            "QtWebChannel.pyd",
            "v8_context_snapshot*.bin",
            "icudtl.dat",  # ICU 数据（WebEngine 专用）
            "vk_swiftshader*.dll",  # Vulkan 软件渲染
            "libGLESv2.dll",
            "libEGL.dll",
        ]
        for pattern in webengine_patterns:
            for f in pyside_dir.glob(pattern):
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                saved += sz

        # ── 2. QML / Quick（约 32 MB） ──
        qml_dir = pyside_dir / "qml"
        if qml_dir.exists():
            sz = sum(f.stat().st_size for f in qml_dir.rglob("*") if f.is_file())
            shutil.rmtree(qml_dir, ignore_errors=True)
            saved += sz

        quick_patterns = [
            "Qt6Quick*.dll",
            "Qt6Qml*.dll",
            "QtQuick*.pyd",
            "QtQml*.pyd",
            "qtquickcontrols2*.dll",
        ]
        for pattern in quick_patterns:
            for f in pyside_dir.glob(pattern):
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                saved += sz

        # ── 3. 3D 模块（约 7 MB） ──
        for f in pyside_dir.glob("Qt63D*.dll"):
            sz = f.stat().st_size
            f.unlink(missing_ok=True)
            saved += sz

        # ── 4. 软件 OpenGL 渲染器（20 MB） ──
        sw_gl = pyside_dir / "opengl32sw.dll"
        if sw_gl.exists():
            saved += sw_gl.stat().st_size
            sw_gl.unlink()

        # ── 5. Qt 内置 FFmpeg（已有外部 ffmpeg，约 16 MB） ──
        for pattern in [
            "avcodec-*.dll",
            "avformat-*.dll",
            "avutil-*.dll",
            "swresample-*.dll",
            "swscale-*.dll",
        ]:
            for f in pyside_dir.glob(pattern):
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                saved += sz

        # ── 6. PDF / Charts / Graphs / ShaderTools 等 DLL ──
        misc_patterns = [
            "Qt6Pdf*.dll",
            "Qt6Charts*.dll",
            "Qt6DataVisualization*.dll",
            "Qt6Graphs*.dll",
            "Qt6ShaderTools.dll",
            "Qt6Bluetooth*.dll",
            "Qt6Nfc*.dll",
            "Qt6SerialPort*.dll",
            "Qt6Sensors*.dll",
            "Qt6Positioning*.dll",
            "Qt6Location*.dll",
            "Qt6RemoteObjects*.dll",
            "Qt6Designer*.dll",
            "Qt6Help*.dll",
            "Qt6Test*.dll",
            "Qt6Sql*.dll",
            "QtOpenGL.pyd",
            "Qt6OpenGL.dll",
        ]
        for pattern in misc_patterns:
            for f in pyside_dir.glob(pattern):
                sz = f.stat().st_size
                f.unlink(missing_ok=True)
                saved += sz

        # ── 7. 翻译文件：只保留中文和英文 ──
        tr_dir = pyside_dir / "translations"
        if tr_dir.exists():
            keep_prefixes = ("qtbase_zh", "qt_zh", "qtbase_en", "qt_en")
            for f in tr_dir.iterdir():
                if f.is_file() and f.suffix == ".qm":
                    if not any(f.name.startswith(p) for p in keep_prefixes):
                        saved += f.stat().st_size
                        f.unlink()

        # ── 8. resources 目录中的 WebEngine 资源 ──
        res_dir = pyside_dir / "resources"
        if res_dir.exists():
            for f in res_dir.iterdir():
                if f.is_file() and ("webengine" in f.name.lower() or "devtools" in f.name.lower()):
                    saved += f.stat().st_size
                    f.unlink()

        # ── 9. plugins 中不需要的插件 ──
        plugins_dir = pyside_dir / "plugins"
        if plugins_dir.exists():
            unwanted_plugins = [
                "multimedia",
                "qmltooling",
                "qmllint",
                "position",
                "sensors",
                "sqldrivers",
                "designer",
                "webview",
            ]
            for name in unwanted_plugins:
                plugin_subdir = plugins_dir / name
                if plugin_subdir.exists():
                    sz = sum(f.stat().st_size for f in plugin_subdir.rglob("*") if f.is_file())
                    shutil.rmtree(plugin_subdir, ignore_errors=True)
                    saved += sz

        saved_mb = saved / (1024 * 1024)
        print(f"🧹 Qt 瘦身完成：清理了 {saved_mb:.1f} MB 的无用文件")

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

        for doc in ["LICENSE", "README.md", "TRADEMARK.md", "ACADEMIC_HONESTY.md"]:
            src_doc = ROOT / doc
            if src_doc.exists():
                shutil.copy2(src_doc, target_dir / doc)
        print("✓ 捆绑核心说明与法律协议文档")

        helpers = target_dir / "_internal/installer"
        helpers.mkdir(parents=True, exist_ok=True)
        shutil.copy2(INSTALLER_DIR / "maintenance.ps1", helpers / "maintenance.ps1")
        self.write_build_info(target_dir)

    def write_build_info(self, target_dir: Path) -> Path:
        """在产物根目录写 BUILD_INFO.json。

        用户拿到的是一个 7z 包，包里此前没有任何东西能回答"这个包内置的
        yt-dlp / ffmpeg 是哪个版本、构建自哪个 commit"。排障时只能靠猜。
        这份清单让 issue 里贴一个文件就能对证。

        构建时间取 SOURCE_DATE_EPOCH（若已设置），使可复现构建能得到一致的输出。
        """
        info = {
            "app_version": self._full_version,
            "numeric_version": self.version,
            "channel": self.channel,
            "release_tag": self.tag,
            "arch": self.arch,
            "built_at_utc": _build_timestamp(),
            "git_commit": _git_commit(),
            "python_version": platform.python_version(),
            "pyinstaller_version": _dist_version("pyinstaller"),
            "pyside6_version": _dist_version("PySide6"),
            "bundled_tools": load_tool_versions(),
            "component_snapshot": self.snapshot,
            "dirty": self.dirty,
        }

        out = target_dir / "BUILD_INFO.json"
        out.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"✓ 生成构建溯源清单: {out.name} (commit={info['git_commit']})")
        return out

    def _assert_dist_clean(self, source_dir: Path) -> None:
        """三个发布目标共用的污染检查入口。

        额外拦一道"配置没解析出来" —— 空黑名单会让这道防线静默失效，
        那比没有防线更糟（构建照样绿灯，脏归档照样发出去）。
        """
        forbidden = self.config.get("dist_forbidden") or []
        if not forbidden:
            raise ValueError(
                "pyproject.toml 的 [tool.fluentytdl.build].dist_forbidden 为空或未解析成功。\n"
                "  该数组必须写成**单行** —— _load_config() 在无 tomllib 的环境\n"
                "  （Python 3.10）会退化到只认 `key = [...]` 的行解析器。"
            )
        assert_dist_clean(source_dir, forbidden)

    #: 便携标记的内容。**写成人类可读的说明而不是零字节文件** —— 空文件在用户眼里
    #: 就是垃圾，随手删掉之后数据落点会从 exe 同级悄悄跳到 %LOCALAPPDATA%，
    #: 而"我的任务和设置怎么都没了"正是这一轮要根除的那个投诉。
    PORTABLE_MARKER_TEXT = (
        "FluentYTDL 便携模式标记\n"
        "\n"
        "这个文件的存在让 FluentYTDL 把配置、任务数据库和日志写在\n"
        "FluentYTDL.exe 所在的这个目录里（便携使用，整个文件夹拷走即迁移）。\n"
        "\n"
        "删除这个文件会让数据落点改为 %LOCALAPPDATA%\\FluentYTDL —— 已有数据\n"
        "会在下次启动时被自动搬过去（只复制，旧文件保留），但请不要在\n"
        "没有备份的情况下随手删它。\n"
    )

    def create_7z(self, source_dir: Path, output_name: str) -> Path:
        self._assert_dist_clean(source_dir)
        RELEASE_DIR.mkdir(exist_ok=True)
        output_path = RELEASE_DIR / f"{output_name}.7z"
        if output_path.exists():
            output_path.unlink()

        # 便携标记只进这一个归档，**绝不落进 source_dir**（= `dist/FluentYTDL/`）。
        # dist/ 是 create_app_core_7z() 与 build_setup() 的共同取材地：标记一旦写进去，
        # app-core 和 setup.exe 会跟着带上它（安装版于是把数据写进 Program Files，
        # 正是 P0-1 的成因），而开发者从 dist/ 直接运行也会污染 dist/。
        # `dist_forbidden` 里列着 portable.txt，所以哪天真写进去了，上面那行
        # `_assert_dist_clean()` 会直接把构建打断 —— 这条注释不是唯一的防线。
        # 把 ~500MB 的树复制到临时目录再打包太贵，所以走"先打包、再追加一个文件"。
        import tempfile

        with tempfile.TemporaryDirectory(prefix="fluentytdl_portable_") as tmp_dir:
            marker = Path(tmp_dir) / "portable.txt"
            marker.write_text(self.PORTABLE_MARKER_TEXT, encoding="utf-8")

            sevenzip = (
                str(Path(os.environ["FLUENTYTDL_7ZIP_DIR"]) / "7za.exe")
                if os.environ.get("FLUENTYTDL_7ZIP_DIR")
                else shutil.which("7z") or shutil.which("7za")
            )
            if sevenzip:
                subprocess.run(
                    [sevenzip, "a", *CLI_OPTIONS, str(output_path), "."],
                    check=True,
                    cwd=source_dir,
                )
                # 第二次 `a` 是追加：路径给绝对路径时 7z 会剥掉目录部分，
                # 文件正好落在归档根 —— 与 exe 同级，这是 paths.py 找它的地方。
                subprocess.run(
                    [sevenzip, "a", *CLI_OPTIONS, str(output_path), str(marker)],
                    check=True,
                )
            else:
                import importlib

                py7zr = importlib.import_module("py7zr")
                # 必须在**同一个 "w" 会话**里写：py7zr 的 "w" 是截断重写，
                # 二次打开会把上面 writeall 的结果整棵覆盖掉。
                with py7zr.SevenZipFile(output_path, "w", filters=python_filters()) as archive:
                    archive.set_encoded_header_mode(False)
                    for item in sorted(source_dir.iterdir()):
                        archive.writeall(item, arcname=item.name)
                    archive.write(marker, arcname="portable.txt")

            verify_archive(output_path, source_dir, {"portable.txt": marker})
            verify_frozen_updater(output_path, source_dir / "updater.exe")

        print(f"📦 压缩包: {output_path.name} (含 portable.txt)")
        return output_path

    def build_setup(self, source_dir: Path) -> Path:
        self._assert_dist_clean(source_dir)
        iss_file = INSTALLER_DIR / "FluentYTDL.iss"
        if not iss_file.exists():
            raise FileNotFoundError(
                f"Inno Setup 脚本不存在: {iss_file}\n"
                "  安装包目标需要该脚本；若只想产出便携包请改用 --target 7z"
            )

        from build_environment import find_iscc

        iscc_paths = [
            find_iscc(),
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Inno Setup 6/ISCC.exe",
            Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
            Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
        ]
        iscc = next((p for p in iscc_paths if p and p.exists()), None)
        if not iscc:
            raise FileNotFoundError(
                "未找到 Inno Setup 编译器 ISCC.exe，无法生成安装包。\n"
                "  已查找: " + "; ".join(str(p) for p in iscc_paths) + "\n"
                "  请安装 Inno Setup 6 (https://jrsoftware.org/isdl.php)，\n"
                "  或改用 --target 7z 只产出便携包。"
            )

        RELEASE_DIR.mkdir(exist_ok=True)
        out_name = f"FluentYTDL-{self._full_version}-{self.arch}-setup"
        cmd = [
            str(iscc),
            f"/DMyAppVersion={self._full_version}",
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
        effective_target = TARGET_ALIASES.get(target, target)

        print(f"========== FluentYTDL Pipelined Build {self._full_version} ==========")

        self.initialize_workspace(effective_target)
        if effective_target != "spec":
            self.ensure_tools()
        app_dir = self.build_spec()
        self.smoke_app(app_dir)

        # "spec" 只验证 PyInstaller 蓝图能否落地（CI 用），不产出发布物
        if effective_target == "spec":
            self.write_result(effective_target, app_dir)
            print(f"\n✅ .spec 验证通过: {app_dir}")
            return

        wanted = TARGET_OUTPUTS[effective_target]

        # 2. 拉取外部工具（bundle_tools 的输入）
        self.ensure_tools()

        # 3. 构建 updater.exe 并复制到应用目录
        self.build_updater(copy_to=app_dir)

        # 4. 注入二进制工具
        self.bundle_tools(app_dir)

        # 5. 产物分发 —— 严格按 TARGET_OUTPUTS，目标没点名的一律不产出
        print(f"\n========== Release 打包 (target={effective_target}) ==========")
        print(f"   本次产出: {', '.join(sorted(wanted))}")
        results = []

        # 完整绿化包
        if "full" in wanted:
            full_archive = self.create_7z(
                app_dir, f"FluentYTDL-{self._full_version}-{self.arch}-full"
            )
            results.append(full_archive)

        # app-core 归档（仅主程序，用于增量更新）
        if "app-core" in wanted:
            app_core_archive = self.create_app_core_7z(app_dir)
            if app_core_archive.exists():
                results.append(app_core_archive)

        # Inno 安装包
        if "setup" in wanted:
            setup_exe = self.build_setup(app_dir)
            if setup_exe and setup_exe.exists():
                results.append(setup_exe)

        # 生成更新清单（只在产出 app-core 时才有意义，见 TARGET_OUTPUTS 注释）
        if "manifest" in wanted:
            results.append(self.generate_update_manifest())

        # 计算指纹 —— 只覆盖本次产出，不再把 release/ 里的陈年旧物一起列进去
        if "checksums" in wanted:
            self.generate_checksums(results)

        # 校验目标要求的产物是否真的落盘 —— 否则"构建成功"是假的
        self._assert_expected_artifacts(effective_target)

        self.write_result(effective_target, app_dir)
        print("\n✅ 流水线完成！")
        for res in results:
            size_mb = res.stat().st_size / 1024 / 1024
            print(f"   ► {res.name} ({size_mb:.1f} MB)")

        self._warn_foreign_release_files(effective_target)

    def initialize_workspace(self, target: str) -> None:
        from build_environment import preflight

        failures = [message for failed, message in preflight(target) if failed]
        if failures:
            raise RuntimeError("\n".join(failures))
        if self.strict_tools:
            raise ValueError(
                "--strict-tools retired: builds fetch latest; use --snapshot for diagnostics"
            )
        self.source_hash = source_fingerprint()
        self.dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
        self.work_dir = ROOT / "build" / "runs" / uuid.uuid4().hex
        self.work_dir.mkdir(parents=True)
        global DIST_DIR, RELEASE_DIR
        DIST_DIR = self.work_dir / "dist"
        RELEASE_DIR = self.work_dir / "release"
        os.environ["FLUENTYTDL_BUILD_REPORTS"] = str(self.work_dir / "archive-reports")
        (self.work_dir / "source-state.json").write_text(
            json.dumps(
                {
                    "git_commit": _git_commit(),
                    "dirty": self.dirty,
                    "source_fingerprint": self.source_hash,
                }
            ),
            encoding="utf-8",
        )
        locale_dir = self.work_dir / "locales"
        shutil.copytree(ROOT / "assets/locales", locale_dir)
        os.environ["FLUENTYTDL_LOCALES_DIR"] = str(locale_dir)

    def smoke_app(self, app_dir: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="fluentytdl_app_smoke_") as tmp:
            try:
                subprocess.run(
                    [str(app_dir / "FluentYTDL.exe"), "--build-self-test", tmp],
                    check=True,
                    timeout=120,
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                )
            finally:
                report = Path(tmp) / "self-test.json"
                if report.is_file():
                    shutil.copy2(report, self.work_dir / "self-test.json")
            if not report.is_file():
                raise RuntimeError("Frozen application produced no self-test report")

    def write_result(self, target: str, app_dir: Path) -> None:
        if source_fingerprint() != self.source_hash:
            raise RuntimeError("Source changed during build; artifacts not promoted")
        files = self._expected_paths(target)
        report = {
            "schema_version": 1,
            "target": target,
            "version": self._full_version,
            "git_commit": _git_commit(),
            "dirty": self.dirty,
            "component_policy": "replay" if self.replay_snapshot else "latest",
            "source_fingerprint": self.source_hash,
            "workspace": str(self.work_dir),
            "app_dir": str(app_dir),
            "snapshot": str(getattr(self, "snapshot_path", "")),
            "artifacts": [
                {"name": p.name, "path": str(p), "sha256": sha256_file(p), "size": p.stat().st_size}
                for p in files
            ],
            "checks": {"frozen_startup": "passed"},
        }
        result = self.work_dir / "build-result.json"
        result.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # Stable pointer is updated only after every required artifact passed validation.
        pointer_tmp = ROOT / "build" / f"{self.work_dir.name}-result.tmp"
        pointer_tmp.write_text(result.read_text(encoding="utf-8"), encoding="utf-8")
        pointer_tmp.replace(ROOT / "build/latest-result.json")
        print(f"BUILD_RESULT={result}")

    def _warn_foreign_release_files(self, effective_target: str) -> None:
        """列出 release/ 里不属于本次目标的残留文件。

        clean() 只清 dist/ 与 build/，从不清 release/。上一次构建的 app-core.7z
        或 SHA256SUMS.txt 会原地留着，让人误以为"选了便携版却打了全部"。
        这里只提示不删除 —— 删产物必须是用户的显式动作（GUI 有勾选项）。
        """
        if not RELEASE_DIR.exists():
            return

        expected = {p.name for p in self._expected_paths(effective_target)}
        foreign = sorted(
            p.name
            for p in RELEASE_DIR.iterdir()
            if p.is_file() and p.name not in expected and p.name != ".gitkeep"
        )
        if foreign:
            print(f"\n⚠ release/ 内另有 {len(foreign)} 个文件不属于本次目标（未删除）:")
            for name in foreign[:15]:
                print(f"     · {name}")
            if len(foreign) > 15:
                print(f"     … 另有 {len(foreign) - 15} 个")

    def _expected_paths(self, effective_target: str) -> list[Path]:
        """本次目标应当落盘的全部文件（含 manifest / checksums）。"""
        names = release_names(effective_target, self._full_version, self.arch)
        return [RELEASE_DIR / name for name in names.values()]

    def _assert_expected_artifacts(self, effective_target: str) -> None:
        """断言目标对应的产物都已生成。

        历史上 build_setup() 在缺少 ISCC 时静默返回空路径，流水线照样打印
        "✅ 流水线完成"，导致零产物的构建被当成成功。
        """
        missing = [
            p
            for p in self._expected_paths(effective_target)
            if not p.is_file() or p.stat().st_size == 0
        ]
        if missing:
            raise FileNotFoundError(
                "构建目标 '"
                + effective_target
                + "' 要求的产物缺失:\n"
                + "\n".join(f"  ✗ {p.name}" for p in missing)
            )

    def generate_checksums(self, artifacts: list[Path]) -> Path:
        """写出 SHA256SUMS.txt，只覆盖本次构建产出的文件。

        以前是遍历整个 release/ —— 本地反复构建时，那份校验和里会混进上个
        版本的 full.7z，发布出去等于给用户一张对不上的清单。
        """
        checksums = []
        for file in sorted(artifacts, key=lambda p: p.name):
            if file.is_file():
                checksums.append(f"{sha256_file(file)}  {file.name}")

        checksum_file = RELEASE_DIR / "SHA256SUMS.txt"
        checksum_file.write_text("\n".join(checksums) + "\n", encoding="utf-8")
        print(f"✓ 校验和: {checksum_file.name}（{len(checksums)} 项）")
        return checksum_file

    def build_updater(self, copy_to: Path | None = None) -> Path:
        """构建 updater.exe（独立更新器）。"""
        spec_file = ROOT / "scripts" / "updater.spec"
        if not spec_file.exists():
            raise FileNotFoundError(
                f"updater.spec 不存在: {spec_file}\n"
                "updater.exe 是自动更新功能的必要组件，请确保 scripts/updater.spec 已提交到仓库。"
            )

        # 前置检查：py7zr 是旧版兼容校验及 updater 的回退解压器。
        # updater.spec 里也有一道同样的断言（collect_submodules 返回 [] 时 SystemExit），
        # 这里再拦一次是为了让 `--target all` 在几秒内失败，而不是先花几分钟
        # 打完主程序再倒在 updater 这一步。
        # 检查当前解释器：PyInstaller 是用 sys.executable 以子进程方式调起的，
        # 收集 hiddenimports 时看到的就是这个环境。
        if importlib.util.find_spec("py7zr") is None:
            raise ModuleNotFoundError(
                "构建 updater.exe 需要 py7zr，当前环境未安装。\n"
                "  修复: uv sync --extra build\n"
                '  或:   pip install "py7zr==1.1.3"\n'
                "版本须与 pyproject.toml 的 build extra 及 release.yml 的 "
                "PY7ZR_VERSION 严格一致。"
            )

        print("🔨 构建 updater.exe ...")

        # PE 版本资源。复用主程序那套 generate_version_info()，只换字符串字段。
        # 版本号与主程序同源（self.version）—— 这不只是为了好看：
        # component_update_manager::launch_pending_updater() 会读安装目录里
        # updater.exe 的这份资源做能力探测，决定能不能传 --data-dir /
        # --origin-user-sid（旧 updater 见到未知参数会 SystemExit(2)）。
        # 版本号缺失 → 一律当旧版 → 看门狗永远退化成 survival 模式。
        updater_version_file = self.work_dir / "updater_version_info.txt"
        generate_version_info(
            self.version,
            updater_version_file,
            description="FluentYTDL 更新程序",
            internal_name="FluentYTDLUpdater",
            original_filename="updater.exe",
        )

        env = os.environ.copy()
        env["FLUENTYTDL_UPDATER_VERSION_FILE"] = str(updater_version_file)

        cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--workpath",
            str(self.work_dir / "updater"),
            "--distpath",
            str(DIST_DIR),
            str(spec_file),
        ]
        subprocess.run(cmd, env=env, check=True, cwd=ROOT)

        updater_exe = DIST_DIR / "updater.exe"
        if not updater_exe.exists():
            raise ChildProcessError(
                "updater.exe 构建失败：PyInstaller 运行完成但未生成 updater.exe，请检查构建日志。"
            )

        # 复制到目标目录
        if copy_to:
            dest = copy_to / "updater.exe"
            shutil.copy2(updater_exe, dest)
            print(f"✓ updater.exe 已复制到 {dest}")

            # 同一份产物再复制成 updater.exe.new —— updater 自更新的投递载体。
            #
            # updater.exe 被 app_core_exclude 明确排除（用户机器上它正在运行，
            # 覆写不了），所以修在 updater 里的东西没法通过 app-core 送出去 ——
            # 已安装用户手上的 updater 会永远是旧的那个。`.new` 是绕开这个死锁的
            # 唯一通道：它对旧 updater 只是归档里一个普通文件，搬进安装目录即完成
            # 投递；替换由新版 main.py（§3）或提权 updater 的退出后 helper（Step 8）
            # 完成。两端细节见 core/updater.py::_self_update_updater()。
            #
            # 排在 create_app_core_7z() / create_7z() / build_setup() 之前是硬要求，
            # 而 run_all() 里 build_updater() 恰好是第一个打包动作 —— 顺序天然成立。
            # 注意 full.7z 与 setup.exe 也会带上这个文件：无害（它们本来就带
            # updater.exe），且能让便携版第一次启动就顺手做一次自更新演练。
            dest_new = copy_to / "updater.exe.new"
            shutil.copy2(updater_exe, dest_new)
            print(f"✓ updater.exe.new 已复制到 {dest_new}（app-core 自更新载体）")

        print(f"✓ updater.exe 构建完成: {updater_exe}")
        return updater_exe

    def create_app_core_7z(self, source_dir: Path) -> Path:
        """创建 app-core 归档（仅主程序，不含 bin/ 工具）。

        白名单打包。updater.py::_move_extracted_files() 会对归档里出现过的
        每一个顶层条目先 rmtree/unlink 再 move —— 所以"归档里有什么"直接决定
        "用户安装目录里什么会被删"。归档干净是用户数据安全的第一道保证，
        而且是唯一根治的那道（updater 侧的 PROTECTED_NAMES 只是纵深防御）。
        """
        self._assert_dist_clean(source_dir)

        include = self.config.get("app_core_include") or []
        exclude = self.config.get("app_core_exclude") or []
        if not include:
            raise ValueError(
                "pyproject.toml 的 [tool.fluentytdl.build].app_core_include 为空或未解析成功。\n"
                "  该数组必须写成**单行**（见 _assert_dist_clean 里的同款说明）。"
            )

        names = [item.name for item in source_dir.iterdir()]
        keep, drop, unknown = classify_app_core_items(names, include, exclude)

        if unknown:
            raise RuntimeError(
                "app-core 白名单遇到未登记的顶层条目，构建中止：\n"
                + "\n".join(f"  ? {n}" for n in unknown)
                + "\n\n二选一：\n"
                "  · 它是运行期残留 → 删掉 dist/ 重新构建\n"
                "  · 它是新增的合法发布物 → 加进 pyproject.toml 的\n"
                "    [tool.fluentytdl.build].app_core_include（要随更新分发）\n"
                "    或 app_core_exclude（不随更新分发）"
            )

        # 白名单里声明了却不存在 → 大概率是 spec 的 datas 改了名字而这里忘了跟。
        # updater.exe.new 例外：它由 build_updater() 产出，单独跑 create_app_core_7z
        # 时（或 build_updater 被跳过时）允许缺失。
        missing = [n for n in include if n not in names and n != "updater.exe.new"]
        if missing:
            raise FileNotFoundError(
                "app_core_include 声明的条目在 dist 中不存在：\n"
                + "\n".join(f"  ✗ {n}" for n in missing)
                + f"\n\ndist 目录: {source_dir}\n"
                "若发布物已改名/移除，请同步更新 pyproject.toml 的 app_core_include。"
            )
        if "updater.exe.new" in include and "updater.exe.new" not in names:
            print("  ⚠ updater.exe.new 不在 dist 中，本次归档不含 updater 自更新投递")

        RELEASE_DIR.mkdir(exist_ok=True)
        output_name = f"FluentYTDL-{self._full_version}-{self.arch}-app-core"
        output_path = RELEASE_DIR / f"{output_name}.7z"
        if output_path.exists():
            output_path.unlink()

        print(f"  app-core 收录 {len(keep)} 项，排除 {len(drop)} 项 ({', '.join(drop)})")

        # 创建临时目录，只包含 app-core 白名单内的文件
        import tempfile

        with tempfile.TemporaryDirectory(prefix="fluentytdl_appcore_") as tmp_dir:
            tmp_path = Path(tmp_dir)

            for name in keep:
                item = source_dir / name
                dest = tmp_path / name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

            # 压缩
            sevenzip = (
                str(Path(os.environ["FLUENTYTDL_7ZIP_DIR"]) / "7za.exe")
                if os.environ.get("FLUENTYTDL_7ZIP_DIR")
                else shutil.which("7z") or shutil.which("7za")
            )
            if sevenzip:
                subprocess.run(
                    [sevenzip, "a", *CLI_OPTIONS, str(output_path), "."],
                    check=True,
                    cwd=tmp_path,
                )
            else:
                import importlib

                py7zr = importlib.import_module("py7zr")
                with py7zr.SevenZipFile(output_path, "w", filters=python_filters()) as archive:
                    archive.set_encoded_header_mode(False)
                    for item in sorted(tmp_path.iterdir()):
                        archive.writeall(item, arcname=item.name)

            verify_archive(output_path, tmp_path)
            verify_frozen_updater(output_path, source_dir / "updater.exe")

        print(f"📦 app-core 归档: {output_path.name}")
        return output_path

    def generate_update_manifest(self) -> Path:
        """生成更新清单 update-manifest.json。"""
        manifest_script = ROOT / "scripts" / "generate_manifest.py"
        if not manifest_script.exists():
            raise FileNotFoundError(manifest_script)

        print("📋 生成更新清单...")
        cmd = [
            sys.executable,
            str(manifest_script),
            "--version",
            self._full_version,
            # 下载 URL 用的是 Release 的 tag（v3.5.5），不是版本号（3.5.5）
            "--tag",
            self.tag,
            "--release-dir",
            str(RELEASE_DIR),
        ]
        subprocess.run(cmd, check=True, cwd=ROOT)

        manifest_path = RELEASE_DIR / "update-manifest.json"
        if manifest_path.exists():
            print(f"✓ 更新清单: {manifest_path.name}")
        return manifest_path


# ============================================================================
# Entry Point
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="FluentYTDL 构建中枢系统",
        # RawText 而非默认 formatter：--target 的帮助是一张多行对照表，
        # 默认 formatter 会把换行全压成一行，表格就废了
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--target",
        "-t",
        choices=["all", "7z", "full", "app-core", "setup", "spec"],
        default="all",
        help=(
            "构建目标（严格产出，目标没点名的一律不生成）:\n"
            "  all      full.7z + app-core.7z + setup.exe + update-manifest.json + SHA256SUMS.txt\n"
            "  7z/full  只产出 full.7z（便携完整版）\n"
            "  app-core 只产出 app-core.7z + update-manifest.json（增量更新内部包）\n"
            "  setup    只产出 setup.exe\n"
            "  spec     只验证 PyInstaller 蓝图，不产出发布物"
        ),
    )
    parser.add_argument("--version", "-v", help="覆盖打包版本号")
    parser.add_argument(
        "--print-names",
        action="store_true",
        help=(
            "只打印该目标本次会产出的文件名（每行一个）后退出，不构建。\n"
            "给 CI 校验步骤用：预期产物清单必须来自 TARGET_OUTPUTS 这张表，\n"
            "在工作流里手抄一份的话，目标语义一变就立刻误报"
        ),
    )
    parser.add_argument(
        "--snapshot", type=Path, help="Replay a verified snapshot for diagnostics only"
    )
    parser.add_argument("--skip-hygiene", action="store_true", help="强制无视黑名单环境污染告警")
    parser.add_argument(
        "--strict-tools",
        action="store_true",
        help="已废弃；诊断重现请显式使用 --snapshot，正式构建始终获取最新组件",
    )

    args = parser.parse_args()

    builder = Builder(
        override_version=args.version,
        skip_hygiene=args.skip_hygiene,
        strict_tools=args.strict_tools,
        replay_snapshot=args.snapshot,
    )

    if args.print_names:
        # 走 Builder 而不是直接调 release_names()：版本号解析（VERSION 文件回落、
        # -rc.N 后缀、v 前缀拒绝）和架构判定都在 Builder 里，绕过去就是第二份实现
        names = release_names(args.target, builder._full_version, builder.arch)
        for name in sorted(names.values()):
            print(name)
        return

    try:
        builder.run_all(target=args.target)
    except Exception as e:
        print(f"\n❌ 流水线崩溃: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
