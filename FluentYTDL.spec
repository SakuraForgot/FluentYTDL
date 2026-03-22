# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import copy_metadata

datas = [('E:\\YouTube\\FluentYTDL\\docs', 'docs'), ('E:\\YouTube\\FluentYTDL\\assets\\logo.ico', 'assets'), ('E:\\YouTube\\FluentYTDL\\assets\\logo.png', 'assets'), ('E:\\YouTube\\FluentYTDL\\src\\fluentytdl\\yt_dlp_plugins_ext', 'fluentytdl/yt_dlp_plugins_ext')]
hiddenimports = ['webview']
datas += copy_metadata('rookiepy')
hiddenimports += collect_submodules('fluentytdl')
hiddenimports += collect_submodules('rookiepy')
hiddenimports += collect_submodules('webview')


a = Analysis(
    ['E:\\YouTube\\FluentYTDL\\main.py'],
    pathex=['E:\\YouTube\\FluentYTDL\\src'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtWebEngine', 'PySide6.QtWebEngineWidgets', 'PySide6.QtMultimedia', 'PySide6.QtBluetooth', 'PySide6.QtPositioning'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FluentYTDL',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='E:\\YouTube\\FluentYTDL\\build\\version_info.txt',
    icon=['E:\\YouTube\\FluentYTDL\\assets\\logo.ico'],
    contents_directory='runtime',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FluentYTDL',
)
