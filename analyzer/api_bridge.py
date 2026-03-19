"""JavaScript API bridge for Project Kestrel visualizer.

Provides the Api class that exposes methods to the pywebview JavaScript layer
and serves as the bridge between the web UI and native OS operations.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import webbrowser

from settings_utils import load_persisted_settings, save_persisted_settings, log
from queue_manager import _queue_manager

# Telemetry — failsafe import (never blocks startup)
try:
    import kestrel_telemetry as _telemetry
except ImportError:
    try:
        from analyzer import kestrel_telemetry as _telemetry
    except ImportError:
        _telemetry = None  # type: ignore[assignment]

# pywebview availability
WEBVIEW_IMPORT_SUCCESS = False
try:
    import webview  # type: ignore  # noqa: F401
    WEBVIEW_IMPORT_SUCCESS = True
except Exception:
    pass

# Metadata writing utilities
try:
    from metadata_writer import write_xmp_metadata as _write_xmp_metadata
except ImportError:
    _write_xmp_metadata = None  # type: ignore[assignment]

HOST = '127.0.0.1'


class Api:
    """JavaScript API exposed to webview for native file/folder operations."""

    # Extension → MIME type map used by read_image_file (avoids mimetypes.guess_type overhead)
    _MIME_MAP: dict = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.jpe': 'image/jpeg', '.jfif': 'image/jpeg',
        '.png': 'image/png',  '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp',
        '.tif': 'image/tiff', '.tiff': 'image/tiff',
    }

    def __init__(self):
        # Cache os.path.realpath(root_path) — root_path is constant for the session
        # but realpath() does a GetFinalPathNameByHandle syscall on Windows each time.
        self._realpath_cache: dict = {}
        self._has_unsaved_changes: bool = False

    def notify_dirty(self, is_dirty: bool) -> dict:
        """Called from JS whenever the dirty flag changes."""
        self._has_unsaved_changes = bool(is_dirty)
        return {'success': True}

    def _root_realpath(self, root_path: str) -> str:
        """Return os.path.realpath(root_path), cached for the lifetime of this Api."""
        if root_path not in self._realpath_cache:
            self._realpath_cache[root_path] = os.path.realpath(root_path)
        return self._realpath_cache[root_path]

    def get_legal_status(self) -> dict:
        """Check if the user has agreed to the terms and if install telemetry was sent."""
        settings = load_persisted_settings()
        agreed = settings.get('legal_agreed_version', '') != ''
        install_sent = settings.get('installed_telemetry_sent', False)
        log(f'[legal] get_legal_status: agreed={agreed}, install_sent={install_sent}')
        return {
            'agreed': agreed,
            'install_sent': install_sent
        }

    def agree_to_legal(self):
        """Mark legal agreement as accepted and trigger installation telemetry if needed."""
        settings = load_persisted_settings()
        version = _telemetry._read_version() if _telemetry else 'unknown'
        settings['legal_agreed_version'] = version
        log(f'[legal] User agreed to terms (version {version})')
        
        # Trigger installation telemetry on first agreement
        if not settings.get('installed_telemetry_sent', False):
            if _telemetry:
                mid = _telemetry.get_machine_id(settings)
                _telemetry.send_installation_telemetry(mid, version=version)
                settings['installed_telemetry_sent'] = True
                log('[legal] Initial installation telemetry triggered.')
        
        save_persisted_settings(settings)
        return {'success': True}
    
    def choose_directory(self):
        """Open native folder picker dialog.
        Returns: absolute path to selected folder, or None if cancelled.
        """
        print(f"[API] choose_directory() called (platform: {sys.platform})", flush=True)
        try:
            if sys.platform == 'darwin':
                script = 'POSIX path of (choose folder with prompt "Select folder containing analyzed photos")'
                result = subprocess.run(
                    ['osascript', '-e', script],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if result.returncode == 0 and result.stdout.strip():
                    selected_path = result.stdout.strip()
                    print(f"[API] choose_directory() -> Success: {selected_path}", flush=True)
                    return selected_path
                print("[API] choose_directory() -> Cancelled by user", flush=True)
                return None
            elif sys.platform.startswith('win'):
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                folder = filedialog.askdirectory(title="Select folder containing analyzed photos")
                root.destroy()
                if folder:
                    print(f"[API] choose_directory() -> Success: {folder}", flush=True)
                    return folder
                else:
                    print("[API] choose_directory() -> Cancelled by user", flush=True)
                    return None
            else:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                folder = filedialog.askdirectory(title="Select folder containing analyzed photos")
                root.destroy()
                if folder:
                    print(f"[API] choose_directory() -> Success: {folder}", flush=True)
                    return folder
                else:
                    print("[API] choose_directory() -> Cancelled by user", flush=True)
                    return None
        except Exception as e:
            print(f"[API] choose_directory() -> Error: {e}", flush=True)
            log(f"Error in choose_directory: {e}")
            return None

    def open_file_explorer(self, folder_path):
        """Open a folder in the native file explorer."""
        if not folder_path:
            return
        
        try:
            abs_path = os.path.abspath(folder_path)
            if not os.path.exists(abs_path):
                print(f"[API] open_file_explorer: Path does not exist {abs_path}", flush=True)
                return
                
            if sys.platform.startswith('win'):
                if hasattr(os, 'startfile'):
                    os.startfile(abs_path)
                else:
                    # Fallback for Windows if startfile is somehow missing (e.g. specialized python builds)
                    subprocess.run(['explorer', abs_path], check=False)
            elif sys.platform == 'darwin':
                subprocess.run(['open', abs_path], check=False)
            else:
                subprocess.run(['xdg-open', abs_path], check=False)
        except Exception as e:
            print(f"[API] open_file_explorer error: {e}", flush=True)
            return

    def choose_application(self):
        """Open native file picker for choosing an application executable.
        Returns: absolute path to selected file, or None if cancelled.
        """
        try:
            if sys.platform == 'darwin':
                import subprocess as _sp
                script = 'POSIX path of (choose file of type {"app","APPL"} with prompt "Select an application")'
                result = _sp.run(['osascript', '-e', script], capture_output=True, text=True, timeout=120)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                return None
            else:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                if sys.platform.startswith('win'):
                    filetypes = [('Executables', '*.exe'), ('All Files', '*.*')]
                else:
                    filetypes = [('All Files', '*.*')]
                filepath = filedialog.askopenfilename(
                    title="Select application executable",
                    filetypes=filetypes
                )
                root.destroy()
                return filepath if filepath else None
        except Exception as e:
            print(f"[API] choose_application() -> Error: {e}", flush=True)
            return None

    def read_kestrel_csv(self, folder_path):
        """Read the kestrel_database.csv from the given folder path.
        
        Args:
            folder_path: Absolute path to folder (may be parent folder or .kestrel folder itself)
            
        Returns:
            dict with 'success': bool, 'data': str (CSV content), 'error': str, 'path': str, 'root': str
        """
        
        try:
            # Normalize path: remove trailing separators to ensure reliable basename detection
            folder_path = folder_path.strip()
            while folder_path and folder_path[-1] in ('/', '\\'):
                folder_path = folder_path[:-1]
            
            if not folder_path:
                raise ValueError("Empty folder path")
            
            
            # Determine if this IS the .kestrel folder or contains one
            folder_name = os.path.basename(folder_path)
            
            is_kestrel_folder = (folder_name == '.kestrel')
            
            if is_kestrel_folder:
                csv_path = os.path.join(folder_path, 'kestrel_database.csv')
                parent_folder = os.path.dirname(folder_path)
                
            else:
                csv_path = os.path.join(folder_path, '.kestrel', 'kestrel_database.csv')
                parent_folder = folder_path
                
            
            if not os.path.exists(csv_path):
                
                return {
                    'success': False,
                    'error': f'Could not find kestrel_database.csv at: {csv_path}',
                    'path': csv_path,
                    'data': ''
                }
            
            with open(csv_path, 'r', encoding='utf-8') as f:
                data = f.read()
            
            
            return {
                'success': True,
                'data': data,
                'error': '',
                'path': csv_path,
                'root': parent_folder
            }
        except Exception as e:
            print(f"[API] read_kestrel_csv() -> Error: {e}", flush=True)
            return {
                'success': False,
                'error': str(e),
                'path': '',
                'data': ''
            }

    def read_kestrel_metadata(self, folder_path: str):
        """Read kestrel_metadata.json from a folder's .kestrel directory."""
        try:
            folder_path = str(folder_path).strip()
            meta_path = os.path.join(folder_path, '.kestrel', 'kestrel_metadata.json')
            if not os.path.isfile(meta_path):
                return {'success': False, 'error': 'Metadata file not found'}
            with open(meta_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {'success': True, 'metadata': data}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def clear_kestrel_data(self, folder_path: str):
        """Delete the contents of the .kestrel folder within the given folder."""
        try:
            folder_path = str(folder_path).strip()
            kestrel_dir = os.path.join(folder_path, '.kestrel')
            if not os.path.isdir(kestrel_dir):
                return {'success': True, 'message': 'No .kestrel folder found'}
            # Verify the .kestrel dir is actually inside folder_path (prevent path traversal)
            real_parent = os.path.realpath(folder_path)
            real_kestrel = os.path.realpath(kestrel_dir)
            if not real_kestrel.startswith(real_parent + os.sep) and real_kestrel != os.path.join(real_parent, '.kestrel'):
                return {'success': False, 'error': 'Invalid path'}
            shutil.rmtree(kestrel_dir)
            print(f"[API] clear_kestrel_data() -> Removed .kestrel from {folder_path}", flush=True)
            return {'success': True, 'message': 'Kestrel analysis data cleared'}
        except Exception as e:
            print(f"[API] clear_kestrel_data() -> Error: {e}", flush=True)
            return {'success': False, 'error': str(e)}

    def is_frozen_app(self):
        """Return whether the application is running as a frozen (PyInstaller) build."""
        return {'frozen': getattr(sys, 'frozen', False)}

    def get_app_version(self):
        """Return the current application version from config."""
        try:
            from kestrel_analyzer.config import VERSION
            return {'success': True, 'version': VERSION}
        except Exception:
            try:
                from analyzer.kestrel_analyzer.config import VERSION
                return {'success': True, 'version': VERSION}
            except Exception:
                return {'success': True, 'version': 'unknown'}

    def fetch_remote_version(self):
        """Fetch version.json from projectkestrel.org to bypass CORS in JS."""
        try:
            import urllib.request
            import urllib.error
            import json
            import ssl
            import certifi
            
            url = "https://projectkestrel.org/version.json"
            ctx = ssl.create_default_context(cafile=certifi.where())
            
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'ProjectKestrel/1.0'},
                method='GET'
            )
            
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return {'success': True, 'data': data}
        except Exception as e:
            print(f"[API] fetch_remote_version() -> Error: {e}", flush=True)
            return {'success': False, 'error': str(e)}

    def get_platform_info(self):
        """Return platform information (windows, macos, linux)."""
        import sys
        if sys.platform == 'darwin':
            return {'success': True, 'platform': 'macos'}
        elif sys.platform == 'win32':
            return {'success': True, 'platform': 'windows'}
        else:
            return {'success': True, 'platform': 'linux'}

    def is_windows_store_app(self):
        """Check if running as a Windows Store app."""
        try:
            import sys
            if sys.platform != 'win32':
                return {'success': True, 'is_store': False}
            # Check if running from Program Files\WindowsApps (typical Store app location)
            import os
            app_path = os.path.dirname(sys.executable)
            is_store = 'WindowsApps' in app_path or os.environ.get('APPX_PACKAGE_ROOT') is not None
            return {'success': True, 'is_store': is_store}
        except Exception:
            return {'success': True, 'is_store': False}

    def inspect_folder(self, folder_path: str):
        """Return lightweight folder summary (total images, processed count)."""
        try:
            import importlib
            inspector = None
            try:
                inspector = importlib.import_module('analyzer.folder_inspector')
            except Exception:
                try:
                    inspector = importlib.import_module('folder_inspector')
                except Exception:
                    inspector = None
            if inspector is None or not hasattr(inspector, 'inspect_folder'):
                return {'success': False, 'error': 'Inspector unavailable'}
            info = inspector.inspect_folder(folder_path)
            return {'success': True, 'info': info}
        except Exception as e:
            print(f"[API] inspect_folder() -> Error: {e}", flush=True)
            return {'success': False, 'error': str(e)}

    def inspect_folders(self, paths):
        """Batch-inspect multiple folders. Expects a list of absolute paths."""
        try:
            import importlib
            inspector = None
            try:
                inspector = importlib.import_module('analyzer.folder_inspector')
            except Exception:
                try:
                    inspector = importlib.import_module('folder_inspector')
                except Exception:
                    inspector = None
            if inspector is None or not hasattr(inspector, 'inspect_folders'):
                return {'success': False, 'error': 'Inspector unavailable', 'results': {}}
            if isinstance(paths, str):
                try:
                    paths = json.loads(paths)
                except Exception:
                    paths = [paths]
            results = inspector.inspect_folders(list(paths))
            return {'success': True, 'results': results}
        except Exception as e:
            print(f"[API] inspect_folders() -> Error: {e}", flush=True)
            return {'success': False, 'error': str(e), 'results': {}}
    
    def read_image_file(self, relative_path, root_path):
        """Read an image file and return it as base64-encoded data.
        
        Args:
            relative_path: Path relative to root (e.g., ".kestrel/export/photo.jpg") 
                          OR absolute path (for backward compatibility with old databases)
            root_path: Absolute path to root folder
            
        Returns:
            dict with 'success': bool, 'data': str (base64), 'mime': str, 'error': str
        """
        try:
            # Normalize separators
            root_path = root_path.rstrip('/\\')
            relative_path = relative_path.replace('\\', '/')

            # Resolve to full path
            if os.path.isabs(relative_path):
                full_path = relative_path
            else:
                relative_path = relative_path.lstrip('/\\')
                full_path = os.path.join(root_path, relative_path)

            # Security check — fast path: no '..' means path cannot escape root.
            # Slow path: resolve symlinks only when traversal sequences are present.
            if '..' in relative_path or os.path.isabs(relative_path):
                root_path_real = self._root_realpath(root_path)
                if not os.path.realpath(full_path).startswith(root_path_real):
                    return {'success': False, 'error': 'Path escapes root directory', 'data': '', 'mime': ''}

            # Read — let open() raise FileNotFoundError rather than a separate stat call
            try:
                with open(full_path, 'rb') as f:
                    data = f.read()
            except FileNotFoundError:
                return {'success': False, 'error': f'File not found: {full_path}', 'data': '', 'mime': ''}

            ext = os.path.splitext(full_path)[1].lower()
            mime_type = self._MIME_MAP.get(ext, 'image/jpeg')

            return {
                'success': True,
                'data': base64.b64encode(data).decode('ascii'),
                'mime': mime_type,
                'error': ''
            }
        except Exception as e:
            print(f"[API] read_image_file() -> Error: {e}", flush=True)
            return {'success': False, 'error': str(e), 'data': '', 'mime': ''}

    def list_subfolders(self, root_path: str, max_depth: int = 3):
        """Recursively list subfolders under root_path, flagging those with .kestrel.

        Args:
            root_path: Absolute path to the root folder to scan.
            max_depth:  How many directory levels to descend (1 = direct children only).

        Returns:
            dict with 'success': bool, 'tree': list[node], 'error': str
            Each node: {name, path, has_kestrel, children: [...]}
        """
        print(f"[API] list_subfolders() called: root='{root_path}' max_depth={max_depth}", flush=True)
        try:
            root_path = root_path.strip().rstrip('/\\')
            if not root_path or not os.path.isdir(root_path):
                return {'success': False, 'tree': [], 'error': f'Not a directory: {root_path}'}

            # Safety caps
            max_depth = max(1, min(int(max_depth), 6))
            try:
                MAX_NODES = max(100, int(os.environ.get('KESTREL_TREE_NODE_LIMIT', '2000')))
            except Exception:
                MAX_NODES = 2000
            node_count = [0]
            limit_reached = [False]

            def _scan(dir_path: str, depth: int) -> list:
                if depth < 1 or node_count[0] >= MAX_NODES:
                    return []
                result = []
                try:
                    entries = sorted(os.scandir(dir_path), key=lambda e: e.name.lower())
                except PermissionError:
                    return []
                for entry in entries:
                    if node_count[0] >= MAX_NODES:
                        limit_reached[0] = True
                        break
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name.startswith('.') or name in ('__pycache__', '$RECYCLE.BIN', 'System Volume Information'):
                        continue
                    node_count[0] += 1
                    full = entry.path
                    has_kestrel = os.path.isfile(os.path.join(full, '.kestrel', 'kestrel_database.csv'))
                    kestrel_version = ''
                    if has_kestrel:
                        try:
                            meta_path = os.path.join(full, '.kestrel', 'kestrel_metadata.json')
                            if os.path.isfile(meta_path):
                                with open(meta_path, 'r', encoding='utf-8') as mf:
                                    kestrel_version = json.load(mf).get('version', '')
                        except Exception:
                            pass
                    children = _scan(full, depth - 1)
                    result.append({
                        'name': name,
                        'path': full,
                        'has_kestrel': has_kestrel,
                        'kestrel_version': kestrel_version,
                        'children': children,
                    })
                return result

            tree = _scan(root_path, max_depth)
            root_has_kestrel = os.path.isfile(os.path.join(root_path, '.kestrel', 'kestrel_database.csv'))
            root_kestrel_version = ''
            if root_has_kestrel:
                try:
                    meta_path = os.path.join(root_path, '.kestrel', 'kestrel_metadata.json')
                    if os.path.isfile(meta_path):
                        with open(meta_path, 'r', encoding='utf-8') as mf:
                            root_kestrel_version = json.load(mf).get('version', '')
                except Exception:
                    pass
            if limit_reached[0]:
                print(f"[API] list_subfolders() -> Node limit reached ({MAX_NODES}); scan truncated at {node_count[0]} nodes", flush=True)
            else:
                print(f"[API] list_subfolders() -> {node_count[0]} nodes found, root_has_kestrel={root_has_kestrel}", flush=True)
            return {
                'success': True,
                'tree': tree,
                'root_has_kestrel': root_has_kestrel,
                'root_kestrel_version': root_kestrel_version,
                'error': '',
                'nodes': node_count[0],
                'truncated': bool(limit_reached[0]),
            }
        except Exception as e:
            print(f"[API] list_subfolders() -> Error: {e}", flush=True)
            return {'success': False, 'tree': [], 'error': str(e)}

    def write_kestrel_csv(self, folder_path: str, csv_content: str):
        """Write CSV content back to .kestrel/kestrel_database.csv for the given folder."""
        try:
            folder_name = os.path.basename(folder_path)
            if folder_name == '.kestrel':
                csv_path = os.path.join(folder_path, 'kestrel_database.csv')
            else:
                csv_path = os.path.join(folder_path, '.kestrel', 'kestrel_database.csv')
            if not os.path.exists(csv_path):
                print(f'[API] write_kestrel_csv({folder_path!r}) -> CSV not found: {csv_path}', flush=True)
                return {'success': False, 'error': f'CSV not found: {csv_path}'}
            with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                f.write(csv_content)
            print(f'[API] write_kestrel_csv({folder_path!r}) -> {len(csv_content)} bytes written to {csv_path}', flush=True)
            return {'success': True, 'path': csv_path}
        except Exception as e:
            print(f'[API] write_kestrel_csv({folder_path!r}) -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

    def apply_normalization(self, folder_path: str, mode: str = None) -> dict:
        """Compute star ratings for all rows in a folder's database using the active rating profile.

        Reads the ``rating_profile`` setting, looks up its quality-score thresholds, and maps
        each image's raw quality score to a 1–5 star rating without any rank-based normalization.
        Returns the computed map WITHOUT writing to the CSV file.

        Also caches the folder's quality distribution in kestrel_metadata.json for potential
        future use (e.g. histogram display).

        The ``mode`` parameter is accepted for API compatibility but is ignored; profile
        thresholds always apply.

        Returns:
            {
              'success': bool,
              'normalized_ratings': {filename: int, ...},  # 0-5 for every row
              'mode_used': str,  # the active profile name
              'error': str
            }
        """
        try:
            import pandas as pd

            try:
                from kestrel_analyzer.ratings import (
                    compute_quality_distribution,
                    get_profile_thresholds,
                    quality_to_rating,
                )
            except ImportError:
                from analyzer.kestrel_analyzer.ratings import (
                    compute_quality_distribution,
                    get_profile_thresholds,
                    quality_to_rating,
                )

            folder_path = str(folder_path).strip().rstrip('/\\')
            kestrel_dir = os.path.join(folder_path, '.kestrel')
            csv_path = os.path.join(kestrel_dir, 'kestrel_database.csv')
            metadata_path = os.path.join(kestrel_dir, 'kestrel_metadata.json')

            if not os.path.exists(csv_path):
                return {'success': False, 'error': 'No database found', 'normalized_ratings': {}, 'mode_used': ''}

            settings = load_persisted_settings()
            profile = settings.get('rating_profile', 'balanced')
            thresholds = get_profile_thresholds(profile)

            df = pd.read_csv(csv_path)
            if df.empty:
                return {'success': True, 'normalized_ratings': {}, 'mode_used': profile, 'error': ''}

            # --- Cache per-folder quality distribution (for potential histogram display) ---
            quality_scores = df['quality'].tolist() if 'quality' in df.columns else []
            folder_dist = compute_quality_distribution(quality_scores)

            try:
                _meta = {}
                if os.path.exists(metadata_path):
                    with open(metadata_path, 'r', encoding='utf-8') as mf:
                        content = mf.read().strip()
                        if content:
                            loaded = json.loads(content)
                            if isinstance(loaded, dict):
                                _meta = loaded
                _meta['quality_distribution'] = folder_dist
                _meta['quality_distribution_stored'] = True
                with open(metadata_path, 'w', encoding='utf-8') as mf:
                    json.dump(_meta, mf, indent=2)
            except Exception:
                pass

            # --- Map quality scores to star ratings (in memory only — no CSV write) ---
            if 'filename' not in df.columns or 'quality' not in df.columns:
                return {'success': True, 'normalized_ratings': {}, 'mode_used': profile, 'error': ''}

            def _get_rating(q_val):
                try:
                    return quality_to_rating(float(q_val), thresholds)
                except (TypeError, ValueError):
                    return 0

            normalized_map = {
                str(row['filename']): _get_rating(row['quality'])
                for _, row in df.iterrows()
            }
            
            return {
                'success': True,
                'normalized_ratings': normalized_map,
                'mode_used': profile,
                'error': '',
            }
        except Exception as e:
            print(f'[API] apply_normalization() -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e), 'normalized_ratings': {}, 'mode_used': ''}

    def read_kestrel_scenedata(self, folder_path: str) -> dict:
        """Read kestrel_scenedata.json from a folder's .kestrel directory.

        Returns:
            {'success': bool, 'data': dict, 'error': str}
        """
        try:
            folder_path = str(folder_path).strip().rstrip('/\\')
            folder_name = os.path.basename(folder_path)
            if folder_name == '.kestrel':
                scenedata_path = os.path.join(folder_path, 'kestrel_scenedata.json')
            else:
                scenedata_path = os.path.join(folder_path, '.kestrel', 'kestrel_scenedata.json')

            if not os.path.exists(scenedata_path):
                # Return an empty-but-valid structure; the UI will fall back to scene_count grouping
                
                return {'success': True, 'data': {'version': '2.0', 'image_ratings': {}, 'scenes': {}}, 'error': ''}

            with open(scenedata_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Ensure expected keys
            data.setdefault('version', '2.0')
            data.setdefault('image_ratings', {})
            data.setdefault('scenes', {})
            
            return {'success': True, 'data': data, 'error': ''}
        except Exception as e:
            print(f'[API] read_kestrel_scenedata({folder_path!r}) -> Error: {e}', flush=True)
            return {'success': False, 'data': {}, 'error': str(e)}

    def write_kestrel_scenedata(self, folder_path: str, scenedata: dict) -> dict:
        """Write kestrel_scenedata.json to a folder's .kestrel directory.

        Args:
            folder_path: Absolute path to folder (parent or .kestrel itself).
            scenedata: The scenedata dict (version, image_ratings, scenes).

        Returns:
            {'success': bool, 'path': str, 'error': str}
        """
        try:
            folder_path = str(folder_path).strip().rstrip('/\\')
            folder_name = os.path.basename(folder_path)
            if folder_name == '.kestrel':
                kestrel_dir = folder_path
            else:
                kestrel_dir = os.path.join(folder_path, '.kestrel')

            if not os.path.isdir(kestrel_dir):
                return {'success': False, 'error': f'.kestrel directory not found at: {kestrel_dir}', 'path': ''}

            scenedata_path = os.path.join(kestrel_dir, 'kestrel_scenedata.json')
            if not isinstance(scenedata, dict):
                return {'success': False, 'error': 'scenedata must be a dict', 'path': ''}

            with open(scenedata_path, 'w', encoding='utf-8') as f:
                json.dump(scenedata, f, indent=2)
            print(f'[API] write_kestrel_scenedata({folder_path!r}) -> {scenedata_path}', flush=True)
            return {'success': True, 'path': scenedata_path, 'error': ''}
        except Exception as e:
            print(f'[API] write_kestrel_scenedata({folder_path!r}) -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e), 'path': ''}

    def open_folder(self, path: str):
        """Open a folder in the system file browser (pywebview desktop mode)."""
        try:
            import platform as _platform
            p = _platform.system()
            if p == 'Windows':
                subprocess.Popen(['explorer', os.path.normpath(path)])
            elif p == 'Darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
            print(f'[API] open_folder({path!r}) -> success', flush=True)
            return {'success': True}
        except Exception as e:
            print(f'[API] open_folder({path!r}) -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

    def open_url(self, url: str):
        """Open a URL in the system default browser (pywebview desktop mode)."""
        try:
            webbrowser.open(url)
            print(f'[API] open_url({url!r}) -> success', flush=True)
            return {'success': True}
        except Exception as e:
            print(f'[API] open_url({url!r}) -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

    # ------------------------------------------------------------------ #
    #  Telemetry / Feedback API                                            #
    # ------------------------------------------------------------------ #

    def send_feedback(self, data):
        """Send feedback / bug report (async, failsafe). Called from JS."""
        try:
            if _telemetry is None:
                print('[API] send_feedback() -> telemetry unavailable', flush=True)
                return {'success': False, 'error': 'Telemetry module not available'}
            if not isinstance(data, dict):
                return {'success': False, 'error': 'Invalid data'}
            settings = load_persisted_settings()
            machine_id = _telemetry.get_machine_id(settings)
            log_tail = ''
            if data.get('include_logs', False):
                log_tail = _telemetry.get_recent_log_tail()
            _telemetry.send_feedback(
                report_type=data.get('type', 'general'),
                description=data.get('description', ''),
                contact=data.get('contact', ''),
                screenshot_b64=data.get('screenshot_b64', ''),
                log_tail=log_tail,
                machine_id=machine_id,
                version=_telemetry._read_version(),
            )
            print(f'[API] send_feedback() -> queued ({data.get("type", "general")})', flush=True)
            return {'success': True}
        except Exception as e:
            print(f'[API] send_feedback() -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

    def get_settings(self):
        """Return persisted settings, ensuring machine_id and version exist."""
        try:
            settings = load_persisted_settings()
            if _telemetry is not None:
                _telemetry.get_machine_id(settings)
            if _telemetry is not None:
                settings['version'] = _telemetry._read_version()
            save_persisted_settings(settings)
            return {'success': True, 'settings': settings}
        except Exception as e:
            print(f'[API] get_settings() -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e), 'settings': {}}

    def save_settings_data(self, settings_dict):
        """Persist settings from JavaScript (wraps save_persisted_settings)."""
        try:
            if not isinstance(settings_dict, dict):
                return {'success': False, 'error': 'Invalid settings'}
            save_persisted_settings(settings_dict)
            return {'success': True}
        except Exception as e:
            print(f'[API] save_settings_data() -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

    # ------------------------------------------------------------------ #
    #  Sample Sets API                                                     #
    # ------------------------------------------------------------------ #

    def get_sample_sets_paths(self):
        """Return absolute paths to bundled sample bird-photo sets.

        Works both during development (sample_sets/ next to the repo root)
        and in PyInstaller frozen builds (bundled via _MEIPASS).
        """
        try:
            candidates = []
            debug_info = []
            
            is_frozen = getattr(sys, 'frozen', False)
            debug_info.append(f'[init] sys.frozen={is_frozen}')
            
            if is_frozen:
                debug_info.append('[frozen] Checking frozen build paths...')
                meipass = getattr(sys, '_MEIPASS', None)
                exe_dir = os.path.dirname(sys.executable) if hasattr(sys, 'executable') else None
                debug_info.append(f'[frozen] sys._MEIPASS={meipass}')
                debug_info.append(f'[frozen] sys.executable={sys.executable}')
                debug_info.append(f'[frozen] exe_dir={exe_dir}')
                
                candidates_checked = []
                bases = []
                
                if meipass:
                    bases.append(meipass)
                    bases.append(os.path.join(meipass, '_internal'))
                if exe_dir:
                    bases.append(exe_dir)
                    bases.append(os.path.join(exe_dir, '_internal'))
                    parent_exe = os.path.dirname(exe_dir)
                    if parent_exe and parent_exe != exe_dir:
                        bases.append(parent_exe)
                        bases.append(os.path.join(parent_exe, '_internal'))
                
                sources_internal = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_internal'))
                bases.append(sources_internal)
                
                debug_info.append(f'[frozen] Will check {len(bases)} base paths')
                for base in bases:
                    if not base or base in candidates_checked:
                        continue
                    candidates_checked.append(base)
                    d = os.path.join(base, 'sample_sets')
                    exists = os.path.isdir(d)
                    debug_info.append(f'[frozen] Checking {d}: exists={exists}')
                    if exists:
                        debug_info.append(f'[frozen] Found sample_sets at: {d}')
                        candidates.append(d)
                        break
                
                if not candidates and exe_dir:
                    debug_info.append(f'[frozen-fallback] Exhaustive search starting from {exe_dir}')
                    try:
                        start_dir = os.path.abspath(os.path.join(exe_dir, '..', '..'))
                        if not os.path.isdir(start_dir):
                            start_dir = exe_dir
                        for root, dirs, files in os.walk(start_dir):
                            depth = root[len(exe_dir):].count(os.sep)
                            if depth > 5:
                                del dirs[:]
                                continue
                            if 'sample_sets' in dirs:
                                found = os.path.join(root, 'sample_sets')
                                debug_info.append(f'[frozen-fallback] Found sample_sets at: {found}')
                                candidates.append(found)
                                break
                    except Exception as e:
                        debug_info.append(f'[frozen-fallback] Exhaustive search failed: {e}')
            else:
                debug_info.append('[dev] Not a frozen build')
            
            cwd_candidate = os.path.join(os.getcwd(), 'sample_sets')
            cwd_exists = os.path.isdir(cwd_candidate)
            debug_info.append(f'[dev-cwd] {cwd_candidate}: exists={cwd_exists}')
            if cwd_exists and cwd_candidate not in candidates:
                candidates.append(cwd_candidate)
            
            file_candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sample_sets')
            file_candidate = os.path.normpath(file_candidate)
            file_exists = os.path.isdir(file_candidate)
            debug_info.append(f'[dev-file] {file_candidate}: exists={file_exists}')
            if file_exists and file_candidate not in candidates:
                candidates.append(file_candidate)
            
            if not candidates and sys.platform.startswith('win'):
                debug_info.append('[fallback] Starting Program Files search...')
                pf_paths = [
                    os.environ.get('ProgramFiles'),
                    os.environ.get('ProgramFiles(x86)'),
                    'C:\\Program Files',
                    'C:\\Program Files (x86)',
                ]
                for pf_base in pf_paths:
                    if not pf_base or not os.path.isdir(pf_base):
                        continue
                    for dirname in os.listdir(pf_base):
                        if 'kestrel' in dirname.lower():
                            kestrel_dir = os.path.join(pf_base, dirname)
                            direct = os.path.join(kestrel_dir, 'sample_sets')
                            if os.path.isdir(direct):
                                debug_info.append(f'[fallback] Found sample_sets at: {direct}')
                                candidates.append(direct)
                                break
                            internal = os.path.join(kestrel_dir, '_internal', 'sample_sets')
                            if os.path.isdir(internal):
                                debug_info.append(f'[fallback] Found sample_sets at: {internal}')
                                candidates.append(internal)
                                break
                    if candidates:
                        break

            debug_info.append(f'[collect] Found {len(candidates)} candidate roots')
            for idx, cand in enumerate(candidates):
                debug_info.append(f'[collect]   [{idx}] {cand}')

            if not candidates:
                error_msg = 'sample_sets folder not found'
                for line in debug_info:
                    print(line, flush=True)
                print(f'[API] get_sample_sets_paths() -> Error: {error_msg}', flush=True)
                return {'success': False, 'error': error_msg, 'paths': []}

            sample_root = candidates[0]
            debug_info.append(f'[api] Using root: {sample_root}')
            
            try:
                items = os.listdir(sample_root)
                debug_info.append(f'[api] Root contains {len(items)} items: {items}')
            except Exception as e:
                debug_info.append(f'[api] Failed to list {sample_root}: {e}')
                items = []
            
            paths = []
            for name in sorted(items):
                full = os.path.join(sample_root, name)
                is_dir = os.path.isdir(full)
                kestrel_dir = os.path.join(full, '.kestrel')
                kestrel_exists = os.path.isdir(kestrel_dir)
                debug_info.append(f'[api]   Item "{name}": is_dir={is_dir}, has .kestrel={kestrel_exists}')
                
                if is_dir and kestrel_exists:
                    readonly_src = os.path.join(kestrel_dir, 'kestrel_database_readonly.csv')
                    db_dst       = os.path.join(kestrel_dir, 'kestrel_database.csv')
                    readonly_exists = os.path.isfile(readonly_src)
                    debug_info.append(f'[api]     readonly_src: {readonly_src} exists={readonly_exists}')
                    
                    if readonly_exists:
                        try:
                            shutil.copy2(readonly_src, db_dst)
                            debug_info.append(f'[api]     Restored sample DB: {db_dst}')
                        except Exception as e:
                            debug_info.append(f'[api]     Failed to restore DB: {e}')
                    else:
                        debug_info.append(f'[api]     No readonly DB found at {readonly_src}')
                    
                    paths.append(full)
                    debug_info.append(f'[api]     Added path: {full}')
            
            for line in debug_info:
                print(line, flush=True)
            print(f'[API] get_sample_sets_paths() -> {len(paths)} sets from {sample_root}', flush=True)
            return {'success': True, 'paths': paths}
        except Exception as e:
            import traceback
            print(f'[API] get_sample_sets_paths() -> Error: {e}', flush=True)
            print(f'[API] Traceback: {traceback.format_exc()}', flush=True)
            return {'success': False, 'error': str(e), 'paths': []}

    # ------------------------------------------------------------------ #
    #  Analysis Queue API (called from JavaScript in pywebview mode)       #
    # ------------------------------------------------------------------ #

    def start_analysis_queue(self, paths, use_gpu=True, wildlife_enabled=True):
        """Enqueue folders for analysis. ``paths`` may be a JSON string or list."""
        try:
            if isinstance(paths, str):
                paths = json.loads(paths)
            if not isinstance(paths, list):
                return {'success': False, 'error': 'paths must be a list'}
            paths = [str(p).strip() for p in paths if p]
            sett = load_persisted_settings()
            detection_threshold = float(sett.get('detection_threshold', 0.75))
            detection_threshold = max(0.1, min(0.99, detection_threshold))
            scene_time_threshold = float(sett.get('scene_time_threshold', 1.0))
            scene_time_threshold = max(0.0, scene_time_threshold)
            mask_threshold = float(sett.get('mask_threshold', 0.5))
            mask_threshold = max(0.5, min(0.95, mask_threshold))
            return _queue_manager.enqueue(paths, use_gpu=bool(use_gpu),
                                          wildlife_enabled=bool(wildlife_enabled),
                                          detection_threshold=detection_threshold,
                                          scene_time_threshold=scene_time_threshold,
                                          mask_threshold=mask_threshold)
        except Exception as e:
            print(f'[API] start_analysis_queue() -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

    def pause_analysis_queue(self):
        """Pause the running analysis queue."""
        return _queue_manager.pause()

    def resume_analysis_queue(self):
        """Resume a paused analysis queue."""
        return _queue_manager.resume()

    def cancel_analysis_queue(self):
        """Cancel the analysis queue (marks pending items as cancelled)."""
        return _queue_manager.cancel()

    def get_queue_status(self):
        """Return the current state of the analysis queue."""
        return _queue_manager.get_status()

    def clear_queue_done(self):
        """Remove finished/errored/cancelled items from the queue list."""
        return _queue_manager.clear_done()

    def remove_queue_item(self, path: str):
        """Remove a single pending item from the queue by path."""
        try:
            return _queue_manager.remove_pending_item(str(path))
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def reorder_queue(self, ordered_paths):
        """Reorder pending queue items. ordered_paths is a JSON string or list of paths."""
        try:
            if isinstance(ordered_paths, str):
                ordered_paths = json.loads(ordered_paths)
            if not isinstance(ordered_paths, list):
                return {'success': False, 'error': 'ordered_paths must be a list'}
            return _queue_manager.reorder_pending(ordered_paths)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def is_analysis_running(self):
        """Return True if the analysis queue is actively running."""
        return {'running': _queue_manager.is_running}

    # ------------------------------------------------------------------ #
    #  Culling Assistant API                                               #
    # ------------------------------------------------------------------ #

    _main_window = None
    _culling_window = None
    _server_port = None

    def open_culling_window(self, root_path: str):
        """Open a new pywebview window for the Culling Assistant."""
        try:
            if not WEBVIEW_IMPORT_SUCCESS:
                return {'success': False, 'error': 'pywebview not available'}
            import webview as _wv
            folder_name = os.path.basename(root_path) if root_path else 'Unknown'
            port = self._server_port or 8765
            from urllib.parse import quote
            culling_url = f'http://{HOST}:{port}/culling.html?root={quote(root_path, safe="")}'
            
            methods = [m for m in dir(self) if not m.startswith('_') and callable(getattr(self, m))]
            log(f'[culling] Creating window with Api instance')
            log(f'[culling] Available public methods (first 10): {methods[:10]}')
            log(f'[culling] read_kestrel_csv available: {"read_kestrel_csv" in methods}')
            
            win = _wv.create_window(
                f'Culling Assistant \u2014 {folder_name}',
                culling_url,
                js_api=self,
                width=1400,
                height=900,
            )
            self._culling_window = win
            log(f'[culling] Culling window created successfully')
            return {'success': True}
        except Exception as e:
            log(f'open_culling_window error: {e}')
            import traceback
            log(f'[culling] Traceback: {traceback.format_exc()}')
            return {'success': False, 'error': str(e)}

    def _find_sidecar_file(self, root_path: str, filename: str, ext: str = '.xmp'):
        """Find sidecar file with given extension for an image file.
        
        Checks multiple naming conventions:
        - filename + ext (e.g., IMG_001.CR3.xmp)
        - name_without_ext + ext (e.g., IMG_001.xmp for IMG_001.CR3)
        
        Returns the filename (not path) if found, None otherwise.
        Searches in the same directory as the image.
        """
        # Check primary naming: filename + ext (e.g., IMG_001.CR3.xmp)
        sidecar_path = os.path.join(root_path, filename + ext)
        if os.path.exists(sidecar_path):
            return filename + ext
        
        # Check secondary naming: name_without_ext + ext (e.g., IMG_001.xmp)
        if '.' in filename:
            base_name = filename.rsplit('.', 1)[0]
            alt_sidecar_path = os.path.join(root_path, base_name + ext)
            if os.path.exists(alt_sidecar_path):
                return base_name + ext
        
        log(f'_find_sidecar_file: Not found for {filename}')
        return None

    def _move_file_with_sidecars(self, root_path: str, filename: str, reject_dir: str):
        """Move a file and its sidecar files (.xmp) to reject directory.
        
        Returns (success: bool, moved_files: list[str])
        """
        moved_files = []
        errors = []
        
        # Move main file
        src = os.path.join(root_path, filename)
        dst = os.path.join(reject_dir, filename)
        try:
            if os.path.exists(src):
                shutil.move(src, dst)
                moved_files.append(filename)
            else:
                errors.append(f'{filename}: file not found')
        except Exception as e:
            errors.append(f'{filename}: {e}')
            return False, moved_files
        
        # Move XMP sidecar if it exists
        xmp_sidecar = self._find_sidecar_file(root_path, filename, '.xmp')
        if xmp_sidecar:
            xmp_src = os.path.join(root_path, xmp_sidecar)
            xmp_dst = os.path.join(reject_dir, xmp_sidecar)
            try:
                if os.path.exists(xmp_src):
                    shutil.move(xmp_src, xmp_dst)
                    moved_files.append(xmp_sidecar)
                    
                else:
                    log(f'move_rejects: Warning - XMP detected but not found at: {xmp_src}')
            except Exception as e:
                # Log warning but don't fail the main move if XMP fails
                log(f'move_rejects: Warning - Failed to move {xmp_sidecar}: {e}')
        else:
            log(f'move_rejects: No XMP sidecar found for: {filename}')
        
        return True, moved_files

    def move_rejects_to_folder(self, root_path: str, filenames):
        """Move original photo files and their XMP sidecars into _KESTREL_Rejects subfolder."""
        try:
            if not root_path or not os.path.isdir(root_path):
                return {'success': False, 'error': 'Invalid root path'}
            reject_dir = os.path.join(root_path, '_KESTREL_Rejects')
            os.makedirs(reject_dir, exist_ok=True)
            moved = []
            errors = []
            for fn in (filenames or []):
                success, moved_files = self._move_file_with_sidecars(root_path, fn, reject_dir)
                if success:
                    moved.extend(moved_files)
                else:
                    errors.append(f'{fn}: move failed')
            log(f'move_rejects: moved {len(moved)} file(s) (including sidecars), errors {len(errors)}')
            return {'success': True, 'moved': len(moved), 'errors': errors, 'reject_folder': reject_dir}
        except Exception as e:
            log(f'move_rejects_to_folder error: {e}')
            return {'success': False, 'error': str(e)}

    def write_xmp_metadata(self, root_path: str, image_data, overwrite_external: bool = False, use_auto_labels: bool = False):
        """Write XMP sidecar files for each image, embedding star rating and culling label."""
        if _write_xmp_metadata is None:
            return {'success': False, 'error': 'metadata_writer module not available'}
        return _write_xmp_metadata(root_path, image_data, overwrite_external, use_auto_labels)

    def _restore_file_with_sidecars(self, reject_dir: str, root_path: str, filename: str):
        """Restore a file and its sidecar files (.xmp) from reject directory.
        
        Checks multiple XMP naming conventions to ensure compatibility.
        Returns (success: bool, restored_files: list[str])
        """
        restored_files = []
        
        # Restore main file
        src = os.path.join(reject_dir, filename)
        dst = os.path.join(root_path, filename)
        try:
            if os.path.exists(src):
                shutil.move(src, dst)
                restored_files.append(filename)
                
            else:
                
                return False, restored_files
        except Exception as e:
            
            return False, restored_files
        
        # Restore XMP sidecar - check multiple naming conventions
        xmp_sidecar = None
        
        # Check primary naming: filename + .xmp (e.g., IMG_001.CR3.xmp)
        xmp_primary = filename + '.xmp'
        xmp_src_primary = os.path.join(reject_dir, xmp_primary)
        if os.path.exists(xmp_src_primary):
            xmp_sidecar = xmp_primary
            
        else:
            # Check secondary naming: name_without_ext + .xmp (e.g., IMG_001.xmp)
            if '.' in filename:
                base_name = filename.rsplit('.', 1)[0]
                xmp_secondary = base_name + '.xmp'
                xmp_src_secondary = os.path.join(reject_dir, xmp_secondary)
                if os.path.exists(xmp_src_secondary):
                    xmp_sidecar = xmp_secondary
                    
        
        if xmp_sidecar:
            xmp_src = os.path.join(reject_dir, xmp_sidecar)
            xmp_dst = os.path.join(root_path, xmp_sidecar)
            try:
                shutil.move(xmp_src, xmp_dst)
                restored_files.append(xmp_sidecar)
                
            except Exception as e:
                # Log warning but don't fail if XMP restore fails
                log(f'undo_reject_move: Warning - Failed to restore {xmp_sidecar}: {e}')
        else:
            log(f'undo_reject_move: No XMP sidecar found for: {filename}')
        
        return True, restored_files

    def undo_reject_move(self, root_path: str, filenames):
        """Move files and their XMP sidecars back from _KESTREL_Rejects to the root folder."""
        try:
            reject_dir = os.path.join(root_path, "_KESTREL_Rejects")
            if not os.path.isdir(reject_dir):
                return {"success": False, "error": "_KESTREL_Rejects folder not found"}
            restored = []
            errors = []
            for fn in (filenames or []):
                success, restored_files = self._restore_file_with_sidecars(reject_dir, root_path, fn)
                if success:
                    restored.extend(restored_files)
                else:
                    errors.append(f"{fn}: not found in rejects")
            log(f"undo_reject_move: restored {len(restored)} file(s) (including sidecars), errors {len(errors)}")
            return {"success": True, "restored": len(restored), "errors": errors}
        except Exception as e:
            log(f"undo_reject_move error: {e}")
            return {"success": False, "error": str(e)}
    def backup_kestrel_csv(self, root_path: str):
        """Copy kestrel_database.csv to kestrel_database_old.csv as backup.
        
        Deprecated: Use backup_kestrel_db instead for dual backup.
        Kept for backward compatibility.
        """
        return self.backup_kestrel_db(root_path)

    def backup_kestrel_db(self, root_path: str):
        """Backup both kestrel_database.csv and kestrel_scenedata.json before major operations.
        
        Creates:
        - .kestrel/kestrel_database_old.csv (from kestrel_database.csv)
        - .kestrel/kestrel_scenedata_old.json (from kestrel_scenedata.json)
        
        Returns:
            {"success": bool, "backup_csv": str, "backup_scenedata": str, "error": str}
        """
        try:
            kestrel_dir = os.path.join(root_path, ".kestrel")
            csv_path = os.path.join(kestrel_dir, "kestrel_database.csv")
            scenedata_path = os.path.join(kestrel_dir, "kestrel_scenedata.json")
            csv_backup = os.path.join(kestrel_dir, "kestrel_database_old.csv")
            scenedata_backup = os.path.join(kestrel_dir, "kestrel_scenedata_old.json")
            
            if not os.path.exists(csv_path):
                return {"success": False, "error": "kestrel_database.csv not found", "backup_csv": "", "backup_scenedata": ""}
            
            # Backup CSV
            shutil.copy2(csv_path, csv_backup)
            log(f"backup_kestrel_db: CSV backed up to {csv_backup}")
            
            # Backup scenedata if it exists
            scenedata_backed = False
            if os.path.exists(scenedata_path):
                shutil.copy2(scenedata_path, scenedata_backup)
                scenedata_backed = True
                log(f"backup_kestrel_db: Scenedata backed up to {scenedata_backup}")
            
            return {
                "success": True,
                "backup_csv": csv_backup,
                "backup_scenedata": scenedata_backup if scenedata_backed else "",
                "error": ""
            }
        except Exception as e:
            log(f"backup_kestrel_db error: {e}")
            return {"success": False, "error": str(e), "backup_csv": "", "backup_scenedata": ""}

    def restore_kestrel_csv_backup(self, root_path: str):
        """Restore kestrel_database_old.csv back to kestrel_database.csv.
        
        Deprecated: Use restore_kestrel_db_backup instead for dual restore.
        Kept for backward compatibility.
        """
        return self.restore_kestrel_db_backup(root_path)

    def restore_kestrel_db_backup(self, root_path: str):
        """Restore both kestrel_database.csv and kestrel_scenedata.json from backups.
        
        Restores from:
        - .kestrel/kestrel_database_old.csv (to kestrel_database.csv)
        - .kestrel/kestrel_scenedata_old.json (to kestrel_scenedata.json, if backup exists)
        
        Returns:
            {"success": bool, "error": str}
        """
        try:
            kestrel_dir = os.path.join(root_path, ".kestrel")
            csv_path = os.path.join(kestrel_dir, "kestrel_database.csv")
            csv_backup = os.path.join(kestrel_dir, "kestrel_database_old.csv")
            scenedata_path = os.path.join(kestrel_dir, "kestrel_scenedata.json")
            scenedata_backup = os.path.join(kestrel_dir, "kestrel_scenedata_old.json")
            
            if not os.path.exists(csv_backup):
                return {"success": False, "error": "kestrel_database_old.csv not found"}
            
            # Restore CSV
            shutil.copy2(csv_backup, csv_path)
            log(f"restore_kestrel_db_backup: CSV restored from {csv_backup}")
            
            # Restore scenedata if backup exists
            if os.path.exists(scenedata_backup):
                shutil.copy2(scenedata_backup, scenedata_path)
                log(f"restore_kestrel_db_backup: Scenedata restored from {scenedata_backup}")
            
            return {"success": True, "error": ""}
        except Exception as e:
            log(f"restore_kestrel_db_backup error: {e}")
            return {"success": False, "error": str(e)}
    def open_reject_folder(self, root_path: str):
        """Open the _KESTREL_Rejects folder in the system file browser."""
        reject_dir = os.path.join(root_path, '_KESTREL_Rejects')
        if os.path.isdir(reject_dir):
            return self.open_folder(reject_dir)
        return {'success': False, 'error': '_KESTREL_Rejects folder not found'}

    def notify_main_window_refresh(self):
        """Tell the main visualizer window to reload its data."""
        try:
            if not WEBVIEW_IMPORT_SUCCESS:
                return {'success': False, 'error': 'pywebview not available'}
            import webview as _wv
            if _wv.windows and len(_wv.windows) > 0:
                main_win = _wv.windows[0]
                main_win.evaluate_js('if(window.reloadCurrentFolders) window.reloadCurrentFolders();')
                return {'success': True}
            return {'success': False, 'error': 'No main window found'}
        except Exception as e:
            log(f'notify_main_window_refresh error: {e}')
            return {'success': False, 'error': str(e)}

    def read_raw_full(self, filename: str, root_path: str, exp_correction: float = 0.0):
        """Process a RAW file and return full-resolution JPEG as base64.
        Results are cached in {root}/.kestrel/culling_TMP/ for fast subsequent loads.
        Falls back to read_image_file for non-RAW formats.
        
        exp_correction: exposure offset in stops applied during postprocessing.
            0.0 (default) = no correction, matches standard display preview.
            Positive = brighten, negative = darken.  Clamped to [-1.5, +3.0].
        """
        from io import BytesIO

        try:
            # Normalize separators from CSV/JS so macOS/Linux don't treat '\\' as a literal char.
            filename = str(filename or '').replace('\\', '/')
            full_path = os.path.join(root_path, filename)
            full_path = os.path.normpath(full_path)
            full_path_real = os.path.realpath(full_path)
            root_path_real = os.path.realpath(root_path)
            # Ensure the requested file is inside root_path (or exactly root_path).
            if full_path_real != root_path_real and not full_path_real.startswith(root_path_real + os.sep):
                return {'success': False, 'error': 'Path escapes root directory'}
            if not os.path.exists(full_path):
                return {'success': False, 'error': f'File not found: {filename}'}

            try:
                from kestrel_analyzer.config import RAW_EXTENSIONS as _cfg_raw_ext
            except ImportError:
                try:
                    from analyzer.kestrel_analyzer.config import RAW_EXTENSIONS as _cfg_raw_ext
                except ImportError:
                    # Final fallback — keep in sync with kestrel_analyzer/config.py
                    _cfg_raw_ext = ['.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.pef', '.sr2', '.raf', '.x3f', '.srw']
            raw_extensions = {ext.lower() for ext in _cfg_raw_ext}
            ext = os.path.splitext(filename)[1].lower()

            if ext not in raw_extensions:
                return self.read_image_file(filename, root_path)

            # Clamp exposure correction to the same limits as the pipeline
            try:
                exp_correction = float(exp_correction)
            except (TypeError, ValueError):
                exp_correction = 0.0
            exp_correction = max(-1.5, min(3.0, exp_correction))

            settings = load_persisted_settings()
            use_cache = bool(settings.get('raw_preview_cache_enabled', True))
            debug_logging_enabled = bool(settings.get('raw_preview_debug_logging_enabled', True))

            cache_dir = os.path.join(root_path, '.kestrel', 'culling_TMP')
            # Cache key includes relative path + extension + file identity.
            # Exposure is intentionally excluded: the UI requests one RAW preview
            # variant per file, already using the selected exposure correction.
            file_stat = os.stat(full_path)
            rel_for_key = os.path.normpath(os.path.relpath(full_path_real, root_path_real)).replace('\\', '/')
            key_material = f'{rel_for_key}|{ext}|{int(file_stat.st_mtime_ns)}|{int(file_stat.st_size)}'
            cache_token = hashlib.sha1(key_material.encode('utf-8')).hexdigest()[:16]
            base = os.path.splitext(os.path.basename(filename))[0]
            cache_name = f'{base}_{cache_token}_preview.jpg'
            cache_path = os.path.join(cache_dir, cache_name)

            debug_meta = {
                'filename': filename,
                'full_path': full_path,
                'platform': sys.platform,
                'exp_correction': round(float(exp_correction), 4),
                'use_cache': bool(use_cache),
                'cache_dir': cache_dir,
                'cache_name': cache_name,
                'cache_path': cache_path,
                'key_material': key_material,
                'cache_token': cache_token,
            }

            if use_cache and os.path.exists(cache_path):
                log(f'read_raw_full: Cache hit for {filename} (exp={exp_correction:+.3f})')
                with open(cache_path, 'rb') as f:
                    cache_bytes = f.read()
                cache_stat = os.stat(cache_path)
                debug_meta.update({
                    'cache_hit': True,
                    'cache_file_bytes': int(len(cache_bytes)),
                    'cache_file_mtime_ns': int(cache_stat.st_mtime_ns),
                    'storage_preview_path': cache_path,
                })
                if debug_logging_enabled:
                    log(f'read_raw_full debug: {json.dumps(debug_meta, sort_keys=True)}')
                b64 = base64.b64encode(cache_bytes).decode('ascii')
                return {'success': True, 'data': b64, 'mime': 'image/jpeg', 'debug': debug_meta}

            import rawpy
            from PIL import Image

            log(f'read_raw_full: Processing RAW file {filename} (exp={exp_correction:+.3f}, cache={use_cache})')
            with rawpy.imread(full_path) as raw:
                try:
                    sizes = raw.sizes
                    raw_sizes = {
                        'width': int(getattr(sizes, 'width', 0) or 0),
                        'height': int(getattr(sizes, 'height', 0) or 0),
                        'raw_width': int(getattr(sizes, 'raw_width', 0) or 0),
                        'raw_height': int(getattr(sizes, 'raw_height', 0) or 0),
                        'iwidth': int(getattr(sizes, 'iwidth', 0) or 0),
                        'iheight': int(getattr(sizes, 'iheight', 0) or 0),
                        'flip': int(getattr(sizes, 'flip', 0) or 0),
                    }
                except Exception:
                    raw_sizes = {}
                
                # Match pipeline postprocess flow: first call with defaults, then expose-shift if needed
                rgb = raw.postprocess()
                
                if exp_correction != 0.0:
                    linear_scale = float(max(0.25, min(8.0, 2.0 ** exp_correction)))
                    preserve = 0.8 if exp_correction > 0 else 0.0
                    rgb = raw.postprocess(
                        no_auto_bright=True,
                        exp_shift=linear_scale,
                        exp_preserve_highlights=preserve,
                    )

            img = Image.fromarray(rgb)

            buf = BytesIO()
            img.save(buf, format='JPEG', quality=90, subsampling=0, optimize=False, progressive=False)
            jpg_bytes = buf.getvalue()
            wrote_cache = False
            if use_cache:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, 'wb') as f:
                    f.write(jpg_bytes)
                wrote_cache = True

            storage_preview_path = cache_path
            if not wrote_cache:
                # Even when cache is disabled, persist one debug copy for inspection.
                os.makedirs(cache_dir, exist_ok=True)
                debug_name = f'{base}_{cache_token}_preview_debug.jpg'
                storage_preview_path = os.path.join(cache_dir, debug_name)
                with open(storage_preview_path, 'wb') as f:
                    f.write(jpg_bytes)

            b64 = base64.b64encode(jpg_bytes).decode('ascii')
            debug_meta.update({
                'cache_hit': False,
                'cache_written': bool(wrote_cache),
                'storage_preview_path': storage_preview_path,
                'raw_sizes': raw_sizes,
                'postprocess_rgb_shape': list(rgb.shape) if hasattr(rgb, 'shape') else [],
                'postprocess_rgb_dtype': str(getattr(rgb, 'dtype', '')),
                'jpeg_bytes': int(len(jpg_bytes)),
                'jpeg_kb': round(len(jpg_bytes) / 1024.0, 2),
                'jpeg_dimensions': {'width': int(img.width), 'height': int(img.height)},
            })
            if debug_logging_enabled:
                log(f'read_raw_full debug: {json.dumps(debug_meta, sort_keys=True)}')
            if use_cache:
                log(f'read_raw_full: Done, {len(jpg_bytes)//1024}KB JPEG ({img.width}x{img.height}), cached as {cache_name}')
            else:
                log(f'read_raw_full: Done, {len(jpg_bytes)//1024}KB JPEG ({img.width}x{img.height}), cache disabled')
            return {'success': True, 'data': b64, 'mime': 'image/jpeg', 'debug': debug_meta}
        except Exception as e:
            log(f'read_raw_full error: {e} (filename={filename}, root_path={root_path})')
            return {'success': False, 'error': str(e)}

    def cleanup_culling_cache(self, root_path: str):
        """Remove the .kestrel/culling_TMP folder to free up space."""
        try:
            cache_dir = os.path.join(root_path, '.kestrel', 'culling_TMP')
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)
                log(f'cleanup_culling_cache: Removed {cache_dir}')
                return {'success': True}
            return {'success': True}
        except Exception as e:
            log(f'cleanup_culling_cache error: {e}')
            return {'success': False, 'error': str(e)}
