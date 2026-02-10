import os
import sys
import ctypes


def _prepend_env_path(var_name: str, path: str) -> None:
    if not path:
        return
    current = os.environ.get(var_name, '')
    if current:
        os.environ[var_name] = path + os.pathsep + current
    else:
        os.environ[var_name] = path


def _debug(msg: str) -> None:
    print(f"[runtime_hook] {msg}")


def _dump_tree(root: str, max_depth: int = 2) -> None:
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


def _first_existing_dir(base_path: str, candidates: list[str]) -> str | None:
    for rel in candidates:
        path = os.path.join(base_path, rel)
        if os.path.isdir(path):
            return path
    return None


def _find_coders_dir(base_path: str) -> str | None:
    candidates = []
    for root, dirs, _files in os.walk(base_path):
        for name in dirs:
            if name == "coders" and "modules-" in root:
                candidates.append(os.path.join(root, name))
        # Keep the search shallow to avoid heavy walks.
        depth = root[len(base_path) :].count(os.sep)
        if depth > 4:
            dirs[:] = []
    if candidates:
        return sorted(candidates)[0]
    return None


if sys.platform == 'win32' and getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    _debug(f"frozen=True platform=win32 base_path={base_path}")
    _dump_tree(base_path, max_depth=2)

    # Add base path to DLL search
    os.add_dll_directory(base_path)
    _prepend_env_path('PATH', base_path)

    # Preload MSVC runtime
    msvc_dlls = ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll']
    for dll in msvc_dlls:
        dll_path = os.path.join(base_path, dll)
        if os.path.exists(dll_path):
            try:
                ctypes.CDLL(dll_path)
            except Exception:
                pass
elif sys.platform == 'darwin' and getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    _debug(f"frozen=True platform=darwin base_path={base_path}")
    _dump_tree(base_path, max_depth=2)
    # Flattened layout: ImageMagick directories live directly under MEIPASS.
    magick_home = base_path
    magick_bin = _first_existing_dir(magick_home, [
        'bin',
        os.path.join('ImageMagick-7.0.10', 'bin'),
        os.path.join('ImageMagick', 'ImageMagick-7.0.10', 'bin'),
    ])
    magick_lib = _first_existing_dir(magick_home, [
        'lib',
        os.path.join('ImageMagick-7.0.10', 'lib'),
        os.path.join('ImageMagick', 'ImageMagick-7.0.10', 'lib'),
    ])
    magick_etc = _first_existing_dir(magick_home, [
        os.path.join('etc', 'ImageMagick-7'),
        os.path.join('ImageMagick-7.0.10', 'etc', 'ImageMagick-7'),
        os.path.join('ImageMagick', 'ImageMagick-7.0.10', 'etc', 'ImageMagick-7'),
    ])
    magick_coders = None
    if magick_lib:
        magick_coders = _first_existing_dir(magick_lib, [
            os.path.join('ImageMagick-7.0.10', 'modules-Q16HDRI', 'coders'),
            os.path.join('ImageMagick-7.0.10', 'modules-Q16', 'coders'),
        ])
    if not magick_coders:
        magick_coders = _find_coders_dir(magick_home)

    _debug(f"MAGICK_HOME={magick_home}")
    _debug(f"MAGICK_BIN={magick_bin}")
    _debug(f"MAGICK_LIB={magick_lib}")
    _debug(f"MAGICK_ETC={magick_etc}")
    _debug(f"MAGICK_CODERS={magick_coders}")

    if os.path.isdir(magick_home):
        os.environ.setdefault('MAGICK_HOME', magick_home)
        if magick_etc:
            os.environ.setdefault('MAGICK_CONFIGURE_PATH', magick_etc)
        if magick_coders:
            os.environ.setdefault('MAGICK_CODER_MODULE_PATH', magick_coders)
        if magick_bin:
            _prepend_env_path('PATH', magick_bin)
        if magick_lib:
            _prepend_env_path('DYLD_FALLBACK_LIBRARY_PATH', magick_lib)
            _prepend_env_path('DYLD_LIBRARY_PATH', magick_lib)
