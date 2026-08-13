# -*- mode: python ; coding: utf-8 -*-
"""
FluentYTDL 更新器 PyInstaller 蓝图

独立的极简更新器，不依赖 Qt 或 fluentytdl 包。
仅包含 Python 标准库 + py7zr（7z 解压支持）。

打包命令:
    pyinstaller scripts/updater.spec

输出: dist/updater/updater.exe (onedir) 或 dist/updater.exe (onefile)
"""

import os
import sys

# ----------------------------------------------------------------------------
# 1. 入口和路径
# ----------------------------------------------------------------------------
spec_dir = SPECPATH if 'SPECPATH' in dir() else os.path.abspath(os.path.dirname(__file__))
entry_script = os.path.join(spec_dir, '..', 'src', 'fluentytdl', 'core', 'updater.py')

# ----------------------------------------------------------------------------
# 1b. 解析 build.py 注入的参数（PE 资源）
#
# 版本资源有两个用途，第二个是**功能性的**：
#   1) 右键属性能看到版本号与"FluentYTDL 更新程序"，而不是一片空白
#   2) component_update_manager::launch_pending_updater() 读这份资源做能力探测。
#      新参数（--data-dir / --origin-user-sid）会让旧 updater 直接 SystemExit(2)，
#      所以传参之前必须先知道安装目录里这个 updater.exe 有多新。没有版本资源就
#      只能一律当旧版处理，看门狗能力永远退化成 survival 模式。
#
# 图标：updater.py 用 ShellExecuteW 的 runas 动词提权，而 **UAC 同意框的图标与
# 程序名直接取自被提权 exe 的 PE 资源**。缺省时 PyInstaller 会嵌入自带的
# icon-windowed.ico（Python 双蛇 logo），于是安装版用户每次更新必经的那一屏 ——
# 整个流程里信任成本最高的一屏 —— 长得像来路不明的程序。与主程序 / 托盘 /
# 安装器共用同一份素材（assets/FluentYTDL_v2.ico）。
#
# 两者都沿用 FluentYTDL.spec:24,112 的保护写法：环境变量缺失或清单文件不存在时
# 传 None，这样直接跑 `pyinstaller scripts/updater.spec` 也不会失败。
# ----------------------------------------------------------------------------
version_file = os.environ.get(
    'FLUENTYTDL_UPDATER_VERSION_FILE',
    os.path.join(spec_dir, '..', 'build', 'updater_version_info.txt'),
)
icon_file = os.path.join(spec_dir, '..', 'assets', 'FluentYTDL_v2.ico')

# ----------------------------------------------------------------------------
# 2. Hidden imports (py7zr 用于 7z 解压)
#    py7zr 有大量子模块 (compressor, archiveinfo, properties, callbacks 等)，
#    仅列几个无法在运行时成功 import。使用 collect_submodules 全量收集。
#
#    ⚠ collect_submodules 对**不存在**的包不报错，只返回 []。历史上这导致
#      本地构建能"成功"产出一个不含 py7zr 的 updater.exe —— 它在用户机器上
#      解压 7z 时才失败，而且是在已经把 _internal 改名之后。构建期硬失败。
# ----------------------------------------------------------------------------
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules('py7zr')
if not hiddenimports:
    raise SystemExit(
        "\n"
        "======================================================================\n"
        " updater.exe 构建中止：当前 Python 环境里找不到 py7zr。\n"
        "\n"
        " py7zr 是 updater 解压 app-core 归档的唯一手段。缺了它，打出来的\n"
        " updater.exe 会在用户机器上解压失败（而且是在已经动过安装目录之后）。\n"
        "\n"
        " 修复：\n"
        "     uv sync --extra build\n"
        " 或：\n"
        "     pip install \"py7zr==1.1.3\"\n"
        "\n"
        " 版本须与 pyproject.toml 的 [project.optional-dependencies].build\n"
        " 以及 .github/workflows/release.yml 的 PY7ZR_VERSION 严格一致。\n"
        "======================================================================\n"
    )

# ----------------------------------------------------------------------------
# 3. Analysis
# ----------------------------------------------------------------------------
a = Analysis(
    [entry_script],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6', 'PyQt5', 'PyQt6', 'tkinter', 'matplotlib',
        'numpy', 'scipy', 'pandas', 'PIL', 'cv2',
        'qfluentwidgets', 'qframelesswindow',
    ],
    noarchive=False,
    optimize=0,
)

# ----------------------------------------------------------------------------
# 4. 构建
# ----------------------------------------------------------------------------
pyz = PYZ(a.pure)

# 使用 onefile 模式：updater.exe 是单个独立文件，便于分发
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    exclude_binaries=False,
    name='updater',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 不使用 UPX，避免杀软误报
    console=False,  # 无窗口模式，日志写入 logs/updater.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[icon_file],
    version=version_file if os.path.exists(version_file) else None,
)
