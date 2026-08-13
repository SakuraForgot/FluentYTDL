# -*- mode: python ; coding: utf-8 -*-
"""
FluentYTDL 声明式 PyInstaller 蓝图
由 scripts/build.py 动态调用并注入参数
"""

import os
import sys
from PyInstaller.utils.hooks import collect_submodules, copy_metadata, collect_data_files, collect_dynamic_libs

# 将 src 目录插入系统路径，以便 collect_submodules 能正确扫描内部模块
try:
    spec_dir = SPECPATH
except NameError:
    spec_dir = os.path.abspath(os.path.dirname(__file__))

src_path = os.path.abspath(os.path.join(spec_dir, '../src'))
if src_path not in sys.path:
    sys.path.insert(0, src_path)

# ----------------------------------------------------------------------------
# 1. 解析动态注入的参数
# ----------------------------------------------------------------------------
version_file = os.environ.get('FLUENTYTDL_VERSION_FILE', 'build/version_info.txt')
qt_excludes_raw = os.environ.get('FLUENTYTDL_QT_EXCLUDES', '')
qt_excludes = [m.strip() for m in qt_excludes_raw.split(',') if m.strip()]

# ----------------------------------------------------------------------------
# 2. 定义挂载数据 (Datas)
# ----------------------------------------------------------------------------
datas = [
    ('../docs', 'docs'),
    ('../assets/FluentYTDL_v2.ico', 'assets'),
    ('../assets/logo.png', 'assets'),
    # 托盘 / 窗口图标的多尺寸位图，由 utils/icons.py 组装成 QIcon。
    # 漏掉任何一张都会让对应 DPI 下的托盘图标退化为缩放糊图。
    ('../assets/logo_tight.png', 'assets'),
    ('../assets/logo_16.png', 'assets'),
    ('../assets/logo_32.png', 'assets'),
    ('../assets/logo_64.png', 'assets'),
    ('../assets/logo_128.png', 'assets'),
    ('../assets/locales', 'assets/locales'),
    # 错误诊断规则表，diagnostics/rules.py 通过 resource_path 加载。
    # 漏掉它会让所有错误退化成 unknown 兜底文案。
    ('../assets/error_rules.json', 'assets'),
    ('../src/fluentytdl/yt_dlp_plugins_ext', 'fluentytdl/yt_dlp_plugins_ext'),
    ('../VERSION', '.'),  # 版本文件，运行时 __init__.py 读取
]

# 自动收集子模块和元数据
hiddenimports = ['mutagen', 'webview', 'clr', 'pythonnet', 'clr_loader']
hiddenimports += ['tzdata', 'pycparser.lextab', 'pycparser.yacctab']
hiddenimports += collect_submodules('fluentytdl')
hiddenimports += collect_submodules('rookiepy')
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('clr_loader')

# QFluentWidgets 及其底层无边框窗体库
# - qfluentwidgets._rc.resource 包含 qInitResources()，注册 QSS / 图标等 Qt 资源
# - qframelesswindow 提供 DWM 阴影 / 透明效果（Win32 API）
# 缺少这些会导致菜单下拉框出现透明边框（阴影渲染失败）
hiddenimports += collect_submodules('qfluentwidgets')
hiddenimports += collect_submodules('qframelesswindow')

datas += copy_metadata('rookiepy')

# pythonnet / clr 需要的运行时 DLL 和数据文件
datas += collect_data_files('pythonnet')
datas += collect_data_files('clr_loader')
datas += collect_data_files('webview')

# ----------------------------------------------------------------------------
# 3. 核心 Analysis 阶段
# ----------------------------------------------------------------------------
a = Analysis(
    ['../main.py'],
    pathex=['../src'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=qt_excludes,
    noarchive=False,
    optimize=0,
)

# ----------------------------------------------------------------------------
# 4. 目标聚合阶段
# ----------------------------------------------------------------------------
pyz = PYZ(a.pure)

# 仅生成 onedir 的可执行文件入口（规避 onefile 的杀软特诊）
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FluentYTDL',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False, # 强烈建议不使用 UPX 给 Runtime Binary 加壳
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['../assets/FluentYTDL_v2.ico'],
    version=version_file if os.path.exists(version_file) else None,
)

# 生成最终的 onedir 输出文件夹结构
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FluentYTDL',
)
