# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all

# models/ includes bundled SpeciesNet (models/speciesnet/info.json + weights) and SAM-HQ checkpoints.
datas = [('models', 'models'), ('kestrel_telemetry.py', '.'), ('build_attestation.py', '.'), ('folder_inspector.py', '.'), ('cli.py', '.'), ('VERSION.txt', '.'), ('kestrel_analyzer', 'kestrel_analyzer'), ('visualizer.html', '.'), ('css', 'css'), ('js', 'js'), ('csv_parser.js', '.'), ('culling.html', '.'), ('logo.png', '.'), ('logo.ico', '.'), ('sample_sets', 'sample_sets'), ('settings_utils.py', '.'), ('editor_launch.py', '.'), ('queue_manager.py', '.'), ('api_bridge.py', '.')]

# CI generates build_attestation.json with the HMAC attestation for official builds.
# Source builds and dev workflows skip generation, in which case the bundle just
# doesn't include the file and the client falls back to the legacy auth header.
if os.path.exists('build_attestation.json'):
    datas.append(('build_attestation.json', '.'))
    print('[spec] Including build_attestation.json (official build).')
else:
    print('[spec] No build_attestation.json present (source/dev build).')

binaries = []
hiddenimports = ['pywebview','PIL','exifread','settings_utils','editor_launch','queue_manager','api_bridge','build_attestation']
binaries += collect_dynamic_libs('onnxruntime')
tmp_ret = collect_all('msvc-runtime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# optree removed: was a transitive dependency of torch via speciesnet (now eliminated)


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
    upx=True,
    console=False,
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
    upx=True,
    upx_exclude=[],
    name='ProjectKestrel',
)
