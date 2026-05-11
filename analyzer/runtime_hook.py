import os
import sys
import ctypes
import glob


# This file runs before the rest of the codebase is importable (it IS the
# PyInstaller runtime hook). We can't import the leveled logger from
# ``settings_utils`` here. Instead, gate the debug output behind the same
# env var the leveled logger uses, so users see this tree dump only when
# they've opted in for verbose diagnostics.
_DEBUG_BUNDLE = os.environ.get('KESTREL_LOG_LEVEL', '').upper() == 'DEBUG'


def _debug(msg: str) -> None:
    if _DEBUG_BUNDLE:
        print(f"[runtime_hook] {msg}")


def _dump_tree(root: str, max_depth: int = 2) -> None:
    if not _DEBUG_BUNDLE:
        return
    if not os.path.isdir(root):
        _debug(f"MEIPASS not a directory: {root}")
        return
    _debug(f"MEIPASS tree (max depth {max_depth}): {root}")
    root_depth = root.rstrip(os.sep).count(os.sep)
    for current_root, dirs, files in os.walk(root):
        depth = current_root.rstrip(os.sep).count(os.sep) - root_depth
        if depth > max_depth:
            dirs[:] = []
            continue
        indent = '  ' * depth
        _debug(f"{indent}{os.path.basename(current_root) or current_root}")
        for name in sorted(files):
            _debug(f"{indent}  {name}")

if sys.platform == 'win32' and getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    _debug(f"frozen=True platform=win32 base_path={base_path}")
    _dump_tree(base_path, max_depth=2)

    # Add base path to DLL search
    os.add_dll_directory(base_path)
    
    # Prepend to PATH
    path_env = os.environ.get('PATH', '')
    os.environ['PATH'] = base_path + os.pathsep + path_env

    # Preload MSVC runtime
    msvc_dlls = ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']
    for dll in msvc_dlls:
        dll_path = os.path.join(base_path, dll)
        if os.path.exists(dll_path):
            try:
                ctypes.CDLL(dll_path)
            except Exception:
                pass