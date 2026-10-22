"""Lightweight folder inspection utilities used by the visualizer.

This module deliberately avoids importing the heavy ML pipeline so the
visualizer can show folder progress without loading models.
"""
from __future__ import annotations

import os
from typing import Dict, List
import math
try:
    import pandas as pd  # pandas is fast for reading CSVs
except Exception:
    pd = None

from kestrel_analyzer.config import (
    KESTREL_DIR_NAME,
    DATABASE_NAME,
    select_camera_images,
)
from kestrel_analyzer.database import load_database


def list_images_in_folder(folder: str) -> list:
    """Return analyzable image filenames in ``folder`` (sorted), honouring the
    RAW-priority rule via :func:`select_camera_images`: a JPEG is dropped only
    when a same-stem RAW exists (an in-camera sidecar of that RAW). ORPHAN
    JPEGs — a lone JPG-only frame with no RAW partner — are KEPT, so a mixed
    RAW+JPG (or JPG-only) shoot is never silently truncated. When there are no
    RAWs at all, every JPEG is returned. Hidden files and macOS AppleDouble
    (``._*``) companions are filtered out via :func:`is_supported_image_file`.

    Names only (not paths), non-recursive. This is the single source of truth
    for "which files count" so folder inspection, local analysis, the cloud
    upload-speed test, and BOTH cloud discovery paths
    (``cloud_compute_client._discover_upload_images`` and
    ``api_bridge._cc_select_upload_files``) all agree. Do not re-implement this
    filter elsewhere — call this helper, or cloud/local drift by an image.
    """
    try:
        entries = [
            name for name in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, name))
        ]
        files = select_camera_images(entries)
        files.sort()
        return files
    except Exception:
        return []


# Back-compat alias: this function was private until the cloud upload-speed test
# began reusing it. Keep the old name working for any in-tree callers.
_list_images_in_folder = list_images_in_folder


#: ``size`` value used when a directory entry is listed but its metadata could
#: not be read. The file is definitely present — it just has no trustworthy
#: byte count — so consumers must treat it as "exists, size unknown" and fall
#: back to filename-only matching for that entry.
SIZE_UNKNOWN = -1


def scan_folder_images(folder: str) -> tuple[list[tuple[str, int]], str]:
    """Return ``([(filename, size), ...], error)`` for ``folder``.

    Same file-selection rule as :func:`list_images_in_folder` — it delegates to
    :func:`select_camera_images` so the two can never disagree about which
    files count — but with two differences that the repair path depends on:

    1. It reports each file's size. ``os.scandir`` carries the stat data from
       the directory enumeration on Windows, and on POSIX it costs the same one
       stat per entry that ``list_images_in_folder``'s ``os.path.isfile`` check
       already pays. So the size is effectively free either way.

    2. **It reports enumeration failure instead of hiding it.** This is the
       whole reason the function exists. ``list_images_in_folder`` returns
       ``[]`` for every error, which makes "this folder is unreadable"
       indistinguishable from "this folder is empty" — and the repair UI reads
       an empty listing against a populated database as "every one of your
       photos is gone", which is exactly the state in which it must NOT offer
       to delete anything. A stale macOS security-scoped bookmark (see
       ``mac_sandbox.resolve_bookmark``, which returns an explicit ``is_stale``
       flag), an unresponsive network share, or an external drive mid-reconnect
       all produce a readable-looking path whose enumeration raises.

    ``error`` is ``''`` when the listing is authoritative and a human-readable
    reason otherwise. When it is non-empty the file list is meaningless and
    callers must not draw any conclusion about the folder's contents from it.

    ``list_images_in_folder`` keeps its swallow-everything contract unchanged —
    the analysis pipeline and both cloud discovery paths rely on it.
    """
    sizes: dict[str, int] = {}
    try:
        with os.scandir(folder) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    # Broken symlink or a racing delete — not a real file.
                    continue
                try:
                    sizes[entry.name] = entry.stat().st_size
                except OSError:
                    # The entry is genuinely present; we just can't measure it.
                    # Recording it as unknown keeps it out of the "missing" set,
                    # which is the answer that matters here.
                    sizes[entry.name] = SIZE_UNKNOWN
    except OSError as e:
        return [], f'{type(e).__name__}: {e}'
    except Exception as e:  # pragma: no cover - defensive
        return [], f'{type(e).__name__}: {e}'

    names = select_camera_images(list(sizes.keys()))
    names.sort()
    return [(n, sizes[n]) for n in names], ''


def inspect_folder(path: str) -> Dict[str, int | str | bool]:
    """Return a small summary about a folder.

    Returns keys: 'root' (abs path), 'has_kestrel' (bool), 'total' (int),
    'processed' (int), 'errored' (int — rows where species == 'Error'),
    'db_path' (str). 'processed' counts every CSV row (including errored ones)
    so existing analyzed-full/partial UI math is unchanged; 'errored' is the
    new field for the errored-folder UX.
    """
    result = {
        'root': '',
        'has_kestrel': False,
        'total': 0,
        'processed': 0,
        'errored': 0,
        'db_path': '',
    }
    if not path:
        return result
    p = path.strip()
    while p and p[-1] in ('/', '\\'):
        p = p[:-1]
    if not p:
        return result
    # If caller passed the .kestrel folder itself, use the parent as root
    base_name = os.path.basename(p)
    if base_name == KESTREL_DIR_NAME:
        root = os.path.dirname(p)
    else:
        root = p

    result['root'] = root
    files = list_images_in_folder(root)
    total = len(files)
    result['total'] = total
    files_set = set(files)

    kestrel_dir = os.path.join(root, KESTREL_DIR_NAME)
    db_path = os.path.join(kestrel_dir, DATABASE_NAME)
    result['db_path'] = db_path
    if os.path.isfile(db_path):
        result['has_kestrel'] = True
        try:
            processed = 0
            errored = 0
            if pd is not None:
                try:
                    df = pd.read_csv(db_path, usecols=['filename', 'species'])
                    df['filename'] = df['filename'].astype(str)
                    df = df[df['filename'].isin(files_set)]
                    processed = int(len(df))
                    if 'species' in df.columns:
                        errored = int((df['species'].astype(str) == 'Error').sum())
                except Exception:
                    # Fall back to filename-only read or load_database
                    try:
                        df = pd.read_csv(db_path, usecols=['filename'])
                        processed_set = set(df['filename'].astype(str).values)
                        processed = sum(1 for f in files if f in processed_set)
                    except Exception:
                        try:
                            db, _ = load_database(kestrel_dir, analyzer_name='visualizer-inspector')
                            if not db.empty and 'filename' in db.columns:
                                processed_set = set(db['filename'].values)
                                processed = sum(1 for f in files if f in processed_set)
                                if 'species' in db.columns:
                                    errored = int(
                                        ((db['filename'].isin(files_set)) & (db['species'].astype(str) == 'Error')).sum()
                                    )
                        except Exception:
                            processed = 0
            else:
                try:
                    db, _ = load_database(kestrel_dir, analyzer_name='visualizer-inspector')
                    if not db.empty and 'filename' in db.columns:
                        processed_set = set(db['filename'].values)
                        processed = sum(1 for f in files if f in processed_set)
                        if 'species' in db.columns:
                            errored = int(
                                ((db['filename'].isin(files_set)) & (db['species'].astype(str) == 'Error')).sum()
                            )
                except Exception:
                    processed = 0
            result['processed'] = int(processed)
            result['errored'] = int(errored)
        except Exception:
            # Fail silently; the visualizer should still work without DB details
            result['processed'] = 0
            result['errored'] = 0
    return result


def inspect_folders(paths: List[str]) -> Dict[str, Dict]:
    """Batch-inspect many folders quickly.

    Returns a mapping: {path: info_dict}
    The inspection is ordered by path depth (shallow first) to surface
    high-level folders quickly.
    """
    out: Dict[str, Dict] = {}
    if not paths:
        return out
    # Deduplicate and normalize
    uniq = []
    seen = set()
    for p in paths:
        if not p:
            continue
        pp = p.strip()
        while pp and pp[-1] in ('/', '\\'):
            pp = pp[:-1]
        if not pp:
            continue
        if pp in seen:
            continue
        seen.add(pp)
        uniq.append(pp)

    # Sort by path depth ascending (shallow folders first)
    uniq.sort(key=lambda x: (x.count(os.sep), len(x)))

    for p in uniq:
        try:
            info = inspect_folder(p)
            out[p] = info
        except Exception:
            out[p] = {'root': p, 'has_kestrel': False, 'total': 0, 'processed': 0, 'db_path': ''}
    return out
