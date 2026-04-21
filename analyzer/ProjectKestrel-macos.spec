# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_submodules
# tree is already imported by pyinstaller runtime environment.


# See if sample_sets exists
print(os.listdir("sample_sets"))
# Build datas list with proper sample_sets bundling using Tree()
# models/ includes bundled SpeciesNet (models/speciesnet/info.json + weights) and SAM-HQ checkpoints.
datas = [('models', 'models'), ('kestrel_telemetry.py', '.'), ('folder_inspector.py', '.'), ('cli.py', '.'), ('VERSION.txt', '.'), ('kestrel_analyzer', 'kestrel_analyzer'), ('visualizer.html', '.'), ('visualizer.css', '.'), ('visualizer.js', '.'), ('csv_parser.js', '.'), ('culling.html', '.'), ('logo.png', '.'), ('logo.ico', '.'), ('settings_utils.py', '.'), ('editor_launch.py', '.'), ('queue_manager.py', '.'), ('api_bridge.py', '.')]

# Add sample_sets using Tree() - convert 3-element tuples to 2-element format for datas
sample_sets_tree = Tree('sample_sets', prefix='sample_sets')
datas += [(item[0], item[1]) for item in sample_sets_tree]  # Only use first 2 elements of each tuple
binaries = []
hiddenimports = ['pywebview', 'certifi','PIL','exifread','settings_utils','editor_launch','queue_manager','api_bridge']
binaries += collect_dynamic_libs('onnxruntime')
tmp_ret = collect_all('msvc-runtime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('optree')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
hiddenimports += collect_submodules('optree')

# After your datas definition, add:
print("=== Verifying source files exist ===")
for src, dst in datas:
    exists = os.path.exists(src)
    print(f"  {src} -> {dst} | exists: {exists}")
    if os.path.isdir(src):
        contents = os.listdir(src)
        print(f"    contents: {contents}")


a = Analysis(
    ['visualizer.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
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
    name='ProjectKestrel',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon='../assets/logo.ico',
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
    upx=False,
    upx_exclude=[],
    name='ProjectKestrel',
    icon='../assets/logo.ico',
)

app = BUNDLE(
    coll,
    name='Project Kestrel.app',
    icon='../assets/logo.ico',
    bundle_identifier='org.ProjectKestrel',
)
