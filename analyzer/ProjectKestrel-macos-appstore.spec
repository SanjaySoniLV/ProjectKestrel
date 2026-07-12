# -*- mode: python ; coding: utf-8 -*-
#
# Mac App Store (sandboxed) build variant of Project Kestrel.
#
# This is DUAL-TRACK with ProjectKestrel-macos.spec (the direct-download
# Developer ID build): that spec is left untouched and keeps shipping the full
# unsandboxed app. This variant differs only where the App Store requires it:
#
#   * console=False           — a GUI .app, not a terminal binary.
#   * info_plist={...}         — real CFBundleVersion/ShortVersionString (NUMERIC,
#                               App Store rejects codenames), min-OS, category,
#                               encryption-exemption declaration, etc.
#   * bundle_identifier        — reverse-DNS; MUST match the registered App ID.
#   * icon .icns               — App Store needs an .icns, not the Windows .ico.
#   * mac_sandbox bundled      — the PyObjC sandbox helper module.
#
# Signing + entitlements are applied by CI after the build (see
# build-macos-appstore.yml), mirroring the proven post-build inside-out signing
# in build-macos.yml — so this spec stays signing-agnostic.
#
# Tunables via environment (CI sets these):
#   KESTREL_APPSTORE_VERSION  -> CFBundleShortVersionString (default "1.0")
#   KESTREL_APPSTORE_BUILD    -> CFBundleVersion            (default "1")
#   KESTREL_BUNDLE_ID         -> bundle identifier (default "org.projectkestrel.desktop")
#   KESTREL_MIN_MACOS         -> LSMinimumSystemVersion     (default "12.0")
#   KESTREL_TARGET_ARCH       -> PyInstaller target_arch (default native; e.g. "universal2")
import os

from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_all

# Bake the distribution channel into the bundle (read at runtime by
# dist_channel.py). This is the Mac App Store build, so it defaults to
# 'appstore'; CI may still override via KESTREL_DIST_CHANNEL.
_dist_channel = os.environ.get("KESTREL_DIST_CHANNEL", "appstore").strip().lower()
if _dist_channel not in ("direct", "appstore"):
    _dist_channel = "appstore"
with open("dist_channel.txt", "w", encoding="utf-8") as _dcf:
    _dcf.write(_dist_channel)
print(f"[spec] dist_channel = {_dist_channel}")

print(os.listdir("sample_sets"))

# --- Read the human-readable release codename (e.g. "Rock Wren") for reference;
#     it is NOT used as the App Store version (which must be numeric). ---
_release_name = ""
try:
    with open("VERSION.txt", "r", encoding="utf-8") as _vf:
        _release_name = _vf.read().strip()
except Exception:
    _release_name = ""

_appstore_version = os.environ.get("KESTREL_APPSTORE_VERSION", "1.0").strip() or "1.0"
_appstore_build = os.environ.get("KESTREL_APPSTORE_BUILD", "1").strip() or "1"
_bundle_id = os.environ.get("KESTREL_BUNDLE_ID", "org.projectkestrel.desktop").strip() or "org.projectkestrel.desktop"
_min_macos = os.environ.get("KESTREL_MIN_MACOS", "12.0").strip() or "12.0"
_target_arch = os.environ.get("KESTREL_TARGET_ARCH", "").strip() or None

# App Store needs an .icns. Generated in CI from assets/logo.png (sips+iconutil);
# fall back to no custom icon (PyInstaller default) if it isn't present, so a
# local/source build doesn't hard-fail on the missing icon.
_icns = "../assets/logo.icns"
if not os.path.exists(_icns):
    print(f"[spec] {_icns} not found; building without a custom .icns icon.")
    _icns = None

# Identical payload to ProjectKestrel-macos.spec, plus mac_sandbox.py.
datas = [('models', 'models'), ('kestrel_telemetry.py', '.'), ('build_attestation.py', '.'), ('folder_inspector.py', '.'), ('cli.py', '.'), ('VERSION.txt', '.'), ('kestrel_analyzer', 'kestrel_analyzer'), ('visualizer.html', '.'), ('css', 'css'), ('js', 'js'), ('csv_parser.js', '.'), ('culling.html', '.'), ('logo.png', '.'), ('perch-logo.png', '.'), ('logo.ico', '.'), ('settings_utils.py', '.'), ('editor_launch.py', '.'), ('queue_manager.py', '.'), ('api_bridge.py', '.'), ('mac_sandbox.py', '.'), ('dist_channel.py', '.'), ('dist_channel.txt', '.')]

if os.path.exists('build_attestation.json'):
    datas.append(('build_attestation.json', '.'))
    print('[spec] Including build_attestation.json (official build).')
else:
    print('[spec] No build_attestation.json present (source/dev build).')

sample_sets_tree = Tree('sample_sets', prefix='sample_sets')
datas += [(item[0], item[1]) for item in sample_sets_tree]
binaries = []
hiddenimports = ['pywebview', 'certifi', 'PIL', 'exifread', 'settings_utils', 'editor_launch', 'queue_manager', 'api_bridge', 'build_attestation', 'mac_sandbox', 'dist_channel', 'Foundation', 'AppKit']
binaries += collect_dynamic_libs('onnxruntime')
tmp_ret = collect_all('msvc-runtime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# pyexiv2 bundles the exiv2 C++ library as a native extension inside the wheel
# (used to embed XMP into JPEG originals). collect_all pulls in the compiled
# module and its bundled shared libs so the frozen app can import it.
tmp_ret = collect_all('pyexiv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

print("=== Verifying source files exist ===")
for src, dst in datas:
    exists = os.path.exists(src)
    print(f"  {src} -> {dst} | exists: {exists}")

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
    console=False,
    icon=_icns,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=_target_arch,
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
    icon=_icns,
)

_info_plist = {
    'CFBundleName': 'Project Kestrel',
    'CFBundleDisplayName': 'Project Kestrel',
    'CFBundleShortVersionString': _appstore_version,
    'CFBundleVersion': _appstore_build,
    'LSMinimumSystemVersion': _min_macos,
    'NSHighResolutionCapable': True,
    'LSApplicationCategoryType': 'public.app-category.photography',
    # We use only standard HTTPS/TLS — no proprietary/non-exempt crypto — so we
    # can declare exemption and skip the annual export-compliance questionnaire.
    'ITSAppUsesNonExemptEncryption': False,
    'NSHumanReadableCopyright': '© Sanjay Soni. All rights reserved.',
    # Reference only; the displayed version is the numeric one above.
    'KestrelReleaseName': _release_name,
}

app = BUNDLE(
    coll,
    name='Project Kestrel.app',
    icon=_icns,
    bundle_identifier=_bundle_id,
    info_plist=_info_plist,
)
