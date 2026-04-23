"""Editor launching logic for Project Kestrel.

Handles opening original photo files in various editors across Windows, macOS, and Linux.
"""

from __future__ import annotations

import os
import subprocess
import sys

from settings_utils import load_persisted_settings, log

# Cache for discovered darktable executable on Windows
_DARKTABLE_EXE = None


def _validate_custom_editor_path(raw: str) -> str | None:
    """Return a canonicalised ``customEditorPath`` or ``None`` if unsafe.

    Minimum hardening for FINDING-05. The feature is intentionally retained,
    with its inherent "execute whatever the user configured" risk documented,
    but we close the most obvious foot-guns:

      * Reject empty / non-string values.
      * Reject any control character (incl. newline, NUL) — no shell is
        spawned via ``Popen([...])``, but these still confuse logs and have
        historically been used as argument-splitting delimiters on some
        shells.
      * Require an absolute path.
      * Reject UNC paths (``\\\\server\\share\\evil.exe``) and forward-slash
        UNC equivalents on Windows — those exfiltrate to arbitrary SMB
        shares via authentication-on-attempt.
      * Require the target to actually exist as a regular file on disk.
        On macOS we additionally allow ``.app`` bundles (which are
        directories). Directories anywhere else are rejected.

    If the path is acceptable, returns ``os.path.realpath(expanded)`` so any
    symlink games are resolved once, up front.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    for ch in candidate:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            log('[security] customEditorPath contains control characters; rejecting.')
            return None
    # UNC paths — drive-by NTLM relay risk.
    if sys.platform.startswith('win'):
        if candidate.startswith('\\\\') or candidate.startswith('//'):
            log(f'[security] customEditorPath rejected (UNC not allowed): {candidate!r}')
            return None
    expanded = os.path.expanduser(candidate)
    if not os.path.isabs(expanded):
        log(f'[security] customEditorPath rejected (must be absolute): {candidate!r}')
        return None
    try:
        resolved = os.path.realpath(expanded)
    except (OSError, ValueError):
        log(f'[security] customEditorPath could not be resolved: {candidate!r}')
        return None
    if not os.path.exists(resolved):
        log(f'[security] customEditorPath does not exist: {resolved!r}')
        return None
    if os.path.isdir(resolved):
        # macOS ``.app`` bundles are directories and are valid targets for
        # ``open -a``. Everywhere else, a directory target is almost always
        # a misconfiguration or an attempt to hand a bogus value to Popen.
        if sys.platform == 'darwin' and resolved.endswith('.app'):
            return resolved
        log(f'[security] customEditorPath rejected (is a directory): {resolved!r}')
        return None
    if not os.path.isfile(resolved):
        log(f'[security] customEditorPath is neither file nor .app bundle: {resolved!r}')
        return None
    return resolved


def _find_darktable_exe() -> str:
    """Best-effort discovery of darktable.exe on Windows.

    Many installs place darktable in one of:
      C:\\Program Files\\darktable\\bin\\darktable.exe
      C:\\Program Files\\darktable\\darktable.exe
      C:\\Program Files (x86)\\darktable\\bin\\darktable.exe
    We also scan PATH entries. Falls back to 'darktable.exe'.
    """
    global _DARKTABLE_EXE
    if _DARKTABLE_EXE and os.path.exists(_DARKTABLE_EXE):
        return _DARKTABLE_EXE
    candidates = [
        os.path.join(os.environ.get('ProgramFiles', ''), 'darktable', 'bin', 'darktable.exe'),
        os.path.join(os.environ.get('ProgramFiles', ''), 'darktable', 'darktable.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'darktable', 'bin', 'darktable.exe'),
    ]
    # Add PATH search
    for p in os.environ.get('PATH', '').split(os.pathsep):
        if not p:
            continue
        exe = os.path.join(p, 'darktable.exe')
        candidates.append(exe)
    for exe in candidates:
        if exe and os.path.exists(exe):
            _DARKTABLE_EXE = exe
            return exe
    return 'darktable.exe'


def launch(path: str, editor: str):
    path = os.path.abspath(path)
    print(f"[LAUNCH] requested path={path!r} editor={editor!r} platform={sys.platform}", flush=True)
    if not os.path.exists(path):
        print(f"[LAUNCH] ERROR: path does not exist: {path}", flush=True)
        raise FileNotFoundError(path)

    # Custom editor: load path from settings and validate before exec.
    # We can't prove the user-chosen binary is benign, but we can refuse the
    # obviously-dangerous cases (UNC, non-existent, control chars, relative
    # paths). See FINDING-05.
    if editor == 'custom':
        settings = load_persisted_settings()
        raw_custom = settings.get('customEditorPath')
        custom_exe = _validate_custom_editor_path(raw_custom) if raw_custom else None
        if custom_exe:
            # Audit log every custom-editor launch so the trail is obvious if
            # something goes sideways.
            log(f'[editor] launching custom editor: exe={custom_exe!r} target={path!r}')
            try:
                if sys.platform == 'darwin' and custom_exe.endswith('.app'):
                    subprocess.Popen(['open', '-a', custom_exe, path]); return
                else:
                    subprocess.Popen([custom_exe, path]); return
            except Exception as e:
                log(f'Custom editor launch failed ({custom_exe}): {e}, falling back to system default')
        else:
            if raw_custom:
                log('[security] customEditorPath failed validation; falling back to system default.')
        editor = 'system'

    # Editor name -> (Windows exe candidates, macOS app name, Linux commands)
    _EDITOR_REGISTRY = {
        'darktable': {
            'win_find': lambda: [_find_darktable_exe()],
            'mac_app': 'darktable',
            'linux': [['flatpak', 'run', 'org.darktable.Darktable'], ['darktable']],
        },
        'lightroom': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'Adobe', 'Adobe Lightroom Classic', 'Lightroom.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Adobe', 'Lightroom', 'Lightroom.exe'),
                'Lightroom.exe',
            ],
            'mac_app': 'Adobe Lightroom Classic',
            'linux': [['lightroom']],
        },
        'photoshop': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'Adobe', 'Adobe Photoshop 2025', 'Photoshop.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Adobe', 'Adobe Photoshop 2024', 'Photoshop.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Adobe', 'Adobe Photoshop 2023', 'Photoshop.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Adobe', 'Adobe Photoshop CC 2022', 'Photoshop.exe'),
                'Photoshop.exe',
            ],
            'mac_app': 'Adobe Photoshop 2025',
            'mac_app_fallbacks': ['Adobe Photoshop 2024', 'Adobe Photoshop 2023', 'Adobe Photoshop'],
            'linux': [],
        },
        'capture_one': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'Capture One', 'CaptureOne.exe'),
                'CaptureOne.exe',
            ],
            'mac_app': 'Capture One',
            'linux': [],
        },
        'affinity': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'Affinity', 'Photo 2', 'Photo.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Affinity', 'Photo', 'Photo.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Affinity Photo 2', 'Photo.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Affinity Photo', 'Photo.exe'),
            ],
            'mac_app': 'Affinity Photo 2',
            'mac_app_fallbacks': ['Affinity Photo'],
            'linux': [],
        },
        'gimp': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'GIMP 2', 'bin', 'gimp-2.10.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'GIMP 2', 'bin', 'gimp.exe'),
                'gimp.exe',
            ],
            'mac_app': 'GIMP',
            'linux': [['flatpak', 'run', 'org.gimp.GIMP'], ['gimp']],
        },
        'rawtherapee': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'RawTherapee', 'rawtherapee.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'RawTherapee', '5.9', 'rawtherapee.exe'),
                'rawtherapee.exe',
            ],
            'mac_app': 'RawTherapee',
            'linux': [['flatpak', 'run', 'com.rawtherapee.RawTherapee'], ['rawtherapee']],
        },
        'luminar': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'Skylum', 'Luminar Neo', 'Luminar Neo.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Luminar Neo', 'Luminar Neo.exe'),
            ],
            'mac_app': 'Luminar Neo',
            'mac_app_fallbacks': ['Luminar AI', 'Luminar 4'],
            'linux': [],
        },
        'dxo': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'DxO', 'DxO PhotoLab 7', 'DxO.PhotoLab.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'DxO', 'DxO PhotoLab 6', 'DxO.PhotoLab.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'DxO', 'DxO PhotoLab', 'DxO.PhotoLab.exe'),
            ],
            'mac_app': 'DxO PhotoLab 7',
            'mac_app_fallbacks': ['DxO PhotoLab 6', 'DxO PhotoLab'],
            'linux': [],
        },
        'on1': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'ON1', 'ON1 Photo RAW 2024', 'ON1 Photo RAW.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'ON1', 'ON1 Photo RAW', 'ON1 Photo RAW.exe'),
            ],
            'mac_app': 'ON1 Photo RAW 2024',
            'mac_app_fallbacks': ['ON1 Photo RAW'],
            'linux': [],
        },
        'acdsee': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'ACD Systems', 'ACDSee Photo Studio Ultimate 2024', 'ACDSee.exe'),
                os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'ACD Systems', 'ACDSee', 'ACDSee.exe'),
                'ACDSee.exe',
            ],
            'mac_app': None,
            'linux': [],
        },
        'paintshop': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'Corel', 'Corel PaintShop Pro 2024', 'Corel PaintShop Pro.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'Corel', 'Corel PaintShop Pro', 'Corel PaintShop Pro.exe'),
            ],
            'mac_app': None,
            'linux': [],
        },
        'faststone': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'FastStone Image Viewer', 'FSViewer.exe'),
                os.path.join(os.environ.get('ProgramFiles', ''), 'FastStone Image Viewer', 'FSViewer.exe'),
                'FSViewer.exe',
            ],
            'mac_app': None,
            'linux': [],
        },
        'xnview': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'XnViewMP', 'xnviewmp.exe'),
                os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'XnViewMP', 'xnviewmp.exe'),
                'xnviewmp.exe',
            ],
            'mac_app': 'XnViewMP',
            'linux': [['xnviewmp']],
        },
        'irfanview': {
            'win_candidates': [
                os.path.join(os.environ.get('ProgramFiles', ''), 'IrfanView', 'i_view64.exe'),
                os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'IrfanView', 'i_view32.exe'),
                'i_view64.exe',
            ],
            'mac_app': None,
            'linux': [],
        },
    }

    info = _EDITOR_REGISTRY.get(editor)

    # Windows
    if sys.platform.startswith('win'):
        if info:
            # Special finder for darktable
            if 'win_find' in info:
                candidates = info['win_find']()
            else:
                candidates = info.get('win_candidates', [])
            for exe in candidates:
                if exe and os.path.exists(exe):
                    try:
                        subprocess.Popen([exe, path]); return
                    except Exception:
                        continue
            log(f'{editor} not found on Windows, falling back to system default')
        os.startfile(path)  # type: ignore[attr-defined]
        return

    # macOS
    if sys.platform == 'darwin':
        if info:
            apps_to_try = []
            if info.get('mac_app'):
                apps_to_try.append(info['mac_app'])
            apps_to_try.extend(info.get('mac_app_fallbacks', []))
            for app_name in apps_to_try:
                try:
                    cmd = ['open', '-a', app_name, path]
                    print(f"[LAUNCH] macOS: running: {cmd}", flush=True)
                    subprocess.Popen(cmd)
                    return
                except Exception as e:
                    print(f"[LAUNCH] macOS {app_name} launch failed: {e}", flush=True)
            if not apps_to_try:
                log(f'{editor} not available on macOS, falling back to system default')

        # System default: try a couple of strategies and log results
        try:
            cmd = ['open', path]
            print(f"[LAUNCH] macOS: trying system open: {cmd}", flush=True)
            p = subprocess.run(cmd, check=False)
            print(f"[LAUNCH] macOS: open returned code {p.returncode}", flush=True)
            if p.returncode == 0:
                return
        except Exception as e:
            print(f"[LAUNCH] macOS: open() raised: {e}", flush=True)

        # NOTE: the previous ``osascript -e 'tell ... open (POSIX file "<path>")'``
        # fallback has been removed. Interpolating ``path`` into an AppleScript
        # string allowed a filename containing a literal double-quote to break
        # out of the POSIX-file literal and inject arbitrary AppleScript —
        # which in turn can ``do shell script``. See FINDING-04. ``open path``
        # above is the canonical macOS launcher; the only remaining fallback
        # is Finder reveal (which is argv-safe because ``path`` is passed as a
        # distinct argv element, not interpolated into any DSL).

        # Last resort: reveal in Finder
        try:
            cmd = ['open', '-R', path]
            print(f"[LAUNCH] macOS: fallback reveal: {cmd}", flush=True)
            subprocess.Popen(cmd)
            return
        except Exception as e:
            print(f"[LAUNCH] macOS: reveal fallback failed: {e}", flush=True)
        return

    # Linux / other
    if info:
        for cmd_args in info.get('linux', []):
            try:
                subprocess.Popen(cmd_args + [path]); return
            except FileNotFoundError:
                continue
    subprocess.Popen(['xdg-open', path])
