# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all
# tree is already imported by pyinstaller runtime environment.

# Bake the distribution channel into the bundle (read at runtime by
# dist_channel.py). The notarized Developer ID DMG is the 'direct' channel;
# CI may override via KESTREL_DIST_CHANNEL.
_dist_channel = os.environ.get('KESTREL_DIST_CHANNEL', 'direct').strip().lower()
if _dist_channel not in ('direct', 'appstore'):
    _dist_channel = 'direct'
with open('dist_channel.txt', 'w', encoding='utf-8') as _dcf:
    _dcf.write(_dist_channel)
print(f'[spec] dist_channel = {_dist_channel}')


# See if sample_sets exists
print(os.listdir("sample_sets"))
# Build datas list with proper sample_sets bundling using Tree()
# models/ includes bundled SpeciesNet (models/speciesnet/info.json + weights) and SAM-HQ checkpoints.
datas = [('models', 'models'), ('kestrel_telemetry.py', '.'), ('build_attestation.py', '.'), ('folder_inspector.py', '.'), ('cli.py', '.'), ('VERSION.txt', '.'), ('kestrel_analyzer', 'kestrel_analyzer'), ('visualizer.html', '.'), ('css', 'css'), ('js', 'js'), ('csv_parser.js', '.'), ('culling.html', '.'), ('logo.png', '.'), ('perch-logo.png', '.'), ('logo.ico', '.'), ('settings_utils.py', '.'), ('editor_launch.py', '.'), ('queue_manager.py', '.'), ('api_bridge.py', '.'), ('mac_sandbox.py', '.'), ('dist_channel.py', '.'), ('dist_channel.txt', '.')]

# CI generates build_attestation.json with the HMAC attestation for official builds.
# Source builds and dev workflows skip generation, in which case the bundle just
# doesn't include the file and the client falls back to the legacy auth header.
if os.path.exists('build_attestation.json'):
    datas.append(('build_attestation.json', '.'))
    print('[spec] Including build_attestation.json (official build).')
else:
    print('[spec] No build_attestation.json present (source/dev build).')

# Add sample_sets using Tree() - convert 3-element tuples to 2-element format for datas
sample_sets_tree = Tree('sample_sets', prefix='sample_sets')
datas += [(item[0], item[1]) for item in sample_sets_tree]  # Only use first 2 elements of each tuple
binaries = []
hiddenimports = ['pywebview', 'certifi','PIL','exifread','settings_utils','editor_launch','queue_manager','api_bridge','build_attestation','mac_sandbox','dist_channel']
binaries += collect_dynamic_libs('onnxruntime')
tmp_ret = collect_all('msvc-runtime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# pyexiv2 bundles the exiv2 C++ library as a native extension inside the wheel
# (used to embed XMP into JPEG originals). collect_all pulls in the compiled
# module and its bundled shared libs so the frozen app can import it.
tmp_ret = collect_all('pyexiv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# optree removed: was a transitive dependency of torch via speciesnet (now eliminated)

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
