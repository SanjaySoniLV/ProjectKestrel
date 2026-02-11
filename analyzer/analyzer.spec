# -*- mode: python ; coding: utf-8 -*-
import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 20)
import os

# COMMAND USED TO GENERATE THIS: python -m PyInstaller main.py --onefile --paths=. --runtime-hook=runtime_hook.py --add-data "models;models" --add-data "gui_app.py;." --add-data "gui_helpers.py;." --add-data "cli.py;." --add-data "VERSION.txt;." --add-data "kestrel_analyzer;kestrel_analyzer" --collect-all msvc-runtime --collect-binaries torch --collect-binaries onnxruntime --collect-binaries tensorflow --name "main_with_msvcruntime"
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_system_data_files

datas = [('models', 'models'), ('gui_app.py', '.'), ('gui_helpers.py', '.'), ('cli.py', '.'), ('VERSION.txt', '.'), ('kestrel_analyzer', 'kestrel_analyzer')]

# Collect cv2 comprehensively to avoid recursion errors
print("Collecting all cv2 components...")
cv2_datas, cv2_binaries, cv2_hiddenimports = collect_all('cv2')
print(f"cv2: {len(cv2_datas)} datas, {len(cv2_binaries)} binaries, {len(cv2_hiddenimports)} hidden imports")

# Collect rawpy comprehensively for RAW image processing
print("Collecting all rawpy components...")
rawpy_datas, rawpy_binaries, rawpy_hiddenimports = collect_all('rawpy')
print(f"rawpy: {len(rawpy_datas)} datas, {len(rawpy_binaries)} binaries, {len(rawpy_hiddenimports)} hidden imports")

binaries = []
hiddenimports = []
datas += cv2_datas
binaries += cv2_binaries
hiddenimports += cv2_hiddenimports
datas += rawpy_datas
binaries += rawpy_binaries
hiddenimports += rawpy_hiddenimports
binaries += collect_dynamic_libs('torch')
binaries += collect_dynamic_libs('onnxruntime')
binaries += collect_dynamic_libs('tensorflow')


a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],  # Using community hooks only (custom hook-cv2.py disabled)
    hooksconfig={},
    runtime_hooks=['runtime_hook.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='kestrel_analyzer',
    icon='../assets/logo.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='kestrel_analyzer',
)

app = BUNDLE(
    coll,
    name='kestrel_analyzer.app',
    icon='../assets/logo.ico',
    bundle_identifier='org.ProjectKestrel.Analyzer',
)
