# -*- mode: python ; coding: utf-8 -*-
"""
FluentYTDL 声明式 PyInstaller 蓝图
由 scripts/build.py 动态调用并注入参数
"""

import os
import sys
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

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
    ('../assets/logo.ico', 'assets'),
    ('../assets/logo.png', 'assets'),
    ('../src/fluentytdl/yt_dlp_plugins_ext', 'fluentytdl/yt_dlp_plugins_ext'),
]

# 自动收集子模块和元数据
hiddenimports = ['mutagen']
hiddenimports += collect_submodules('fluentytdl')
hiddenimports += collect_submodules('rookiepy')
datas += copy_metadata('rookiepy')

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
    icon=['../assets/logo.ico'],
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
