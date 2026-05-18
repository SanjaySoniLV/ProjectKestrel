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
    RAW_EXTENSIONS,
    JPEG_EXTENSIONS,
    KESTREL_DIR_NAME,
    DATABASE_NAME,
    is_supported_image_file,
)
from kestrel_analyzer.database import load_database


def _list_images_in_folder(folder: str) -> list:
    try:
        files = [
            f for f in os.listdir(folder)
            if is_supported_image_file(f, RAW_EXTENSIONS)
            and os.path.isfile(os.path.join(folder, f))
        ]
        if not files:
            files = [
                f for f in os.listdir(folder)
                if is_supported_image_file(f, JPEG_EXTENSIONS)
                and os.path.isfile(os.path.join(folder, f))
            ]
        files.sort()
        return files
    except Exception:
        return []


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
    files = _list_images_in_folder(root)
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
