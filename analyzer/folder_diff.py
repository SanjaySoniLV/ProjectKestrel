"""Drift detection between a folder's images on disk and its ``.kestrel`` database.

Kestrel keys a database row to a photo by name and location, and the filesystem
lets users change both without telling the app. This module reports what
changed so the repair UI can offer to fix it.

Kept separate from :mod:`folder_inspector`, which is deliberately ML-free and
sits on the folder-tree hot path; this module reads the CSV and may walk one
level of subdirectories, so it belongs off that path.

**The safety contract.** Every result carries ``scan_status``. Only ``'ok'``
means the file listing is authoritative. ``'unreadable'`` means enumeration
failed and the caller learned *nothing* about the folder's contents — in
particular it must not read the empty file list as "every photo is gone" and
offer to delete anything. See :func:`folder_inspector.scan_folder_images` for
why that distinction cannot be recovered after the fact.
"""
from __future__ import annotations

import os
from typing import Dict, List

try:
    import pandas as pd
except Exception:  # pragma: no cover - pandas is a hard dep in practice
    pd = None

from kestrel_analyzer.config import KESTREL_DIR_NAME, DATABASE_NAME
from kestrel_analyzer.database import parse_file_size, read_database_csv
from folder_inspector import scan_folder_images, SIZE_UNKNOWN

#: Culling moves rejected originals here. Must match the literal in
#: ``api_bridge.move_rejects_to_folder``; a mismatch would make every culled
#: photo look like a missing one.
REJECT_DIR_NAME = "_KESTREL_Rejects"

#: Directories never treated as a relocation target when scanning one level
#: down for moved files.
_SKIP_SUBDIRS = {KESTREL_DIR_NAME, REJECT_DIR_NAME}

#: Cap on immediate subdirectories scanned for relocated files. A photo folder
#: with more children than this is not the "user dragged some keepers into a
#: subfolder" shape this scan exists to catch, and walking them all would put
#: real I/O on a path the user did not ask for.
_MAX_SUBDIRS_SCANNED = 64


def _load_db_rows(kestrel_dir: str) -> "tuple[list[tuple[str, int | None]], str]":
    """Return ``([(filename, size_or_None), ...], error)`` from the database CSV."""
    db_path = os.path.join(kestrel_dir, DATABASE_NAME)
    if not os.path.isfile(db_path):
        return [], "no_database"
    if pd is None:  # pragma: no cover - defensive
        return [], "pandas_unavailable"
    try:
        # read_database_csv, not a bare pd.read_csv: the analysis pipeline saves
        # this file after every image, and on Windows a concurrent reader and
        # that atomic replace collide transiently. See database.retry_on_file_lock.
        frame = read_database_csv(db_path, usecols=lambda c: c in ("filename", "file_size"))
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"

    if "filename" not in frame.columns:
        return [], "malformed_database"

    has_size = "file_size" in frame.columns
    rows: List[tuple] = []
    names = frame["filename"].astype(str).values
    sizes = frame["file_size"].values if has_size else None
    for i, name in enumerate(names):
        if not name or name == "nan":
            continue
        rows.append((name, parse_file_size(sizes[i]) if has_size else None))
    return rows, ""


def _match_renames(
    missing: "list[tuple[str, int | None]]",
    new: "list[tuple[str, int]]",
) -> "tuple[list[dict], set, set]":
    """Pair renamed files by size, but only when the size is unambiguous.

    A pair is offered only when exactly one missing row and exactly one new file
    share a byte count. If two or more entries on either side share a size, every
    member of that group is left unmatched.

    That strictness is deliberate. Without a capture-time check to corroborate,
    size is the only evidence available, and a wrong pairing does not merely
    misattribute analysis results — it reattaches the wrong star rating and the
    wrong culling decision to a photo. Those are user decisions, and unlike the
    machine-generated columns they cannot be reconstructed by re-running
    anything. An unoffered rename costs the user a re-analysis; a wrong one
    costs them work they cannot get back.

    Returns ``(pairs, matched_missing_names, matched_new_names)``.
    """
    by_size_missing: Dict[int, List[str]] = {}
    for name, size in missing:
        if size is None:
            continue
        by_size_missing.setdefault(size, []).append(name)

    by_size_new: Dict[int, List[str]] = {}
    for name, size in new:
        if size is None or size < 0:
            continue
        by_size_new.setdefault(size, []).append(name)

    pairs: List[dict] = []
    matched_missing: set = set()
    matched_new: set = set()
    for size, old_names in by_size_missing.items():
        new_names = by_size_new.get(size)
        if not new_names:
            continue
        if len(old_names) != 1 or len(new_names) != 1:
            # Ambiguous group — no basis to pick a pairing. Leave it alone.
            continue
        pairs.append({"from": old_names[0], "to": new_names[0], "size": size})
        matched_missing.add(old_names[0])
        matched_new.add(new_names[0])

    pairs.sort(key=lambda p: p["from"].lower())
    return pairs, matched_missing, matched_new


def _scan_subfolders(root: str, missing: "list[tuple[str, int | None]]") -> List[dict]:
    """Look one level down for the missing files.

    Catches the common "user dragged the keepers into a subfolder" split, which
    is otherwise indistinguishable from deletion because Kestrel's listing is
    non-recursive.
    """
    if not missing:
        return []
    wanted_by_name: Dict[str, "int | None"] = {name: size for name, size in missing}

    try:
        with os.scandir(root) as it:
            subdirs = [
                e.path for e in it
                if e.is_dir() and e.name not in _SKIP_SUBDIRS and not e.name.startswith(".")
            ]
    except OSError:
        return []

    if len(subdirs) > _MAX_SUBDIRS_SCANNED:
        return []

    hits: List[dict] = []
    for sub in sorted(subdirs):
        entries, err = scan_folder_images(sub)
        if err:
            continue
        found = []
        for name, size in entries:
            want = wanted_by_name.get(name, "__absent__")
            if want == "__absent__":
                continue
            # Size confirms when both sides know it; name alone is the fallback
            # for legacy rows, which is the same standard the diff itself uses.
            if want is None or size == SIZE_UNKNOWN or want == size:
                found.append(name)
        if found:
            found.sort()
            hits.append({
                "path": sub,
                "name": os.path.basename(sub),
                "filenames": found,
                "count": len(found),
            })
    hits.sort(key=lambda h: (-h["count"], h["name"].lower()))
    return hits


def compute_folder_diff(root: str, scan_subfolders: bool = True) -> dict:
    """Compare ``root``'s images on disk against its ``.kestrel`` database.

    Returns a dict with:

    ``scan_status``
        ``'ok'`` (the listing is authoritative), ``'unreadable'`` (enumeration
        failed — draw no conclusions, offer no repairs), or ``'no_database'``
        (nothing analysed here yet).
    ``missing``
        Filenames with a database row and no file on disk, after rename matching
        and after excluding anything sitting in ``_KESTREL_Rejects``.
    ``new``
        Files on disk with no database row, after rename matching.
    ``renamed``
        ``[{'from', 'to', 'size'}]`` — unambiguous size pairings only.
    ``rejected``
        Missing files found in ``_KESTREL_Rejects``. Culling normally deletes
        these rows at move time, so a non-empty list means a move was interrupted.
    ``subfolder_hits``
        ``[{'path', 'name', 'filenames', 'count'}]`` — missing files located one
        level down.
    ``size_changed``
        Informational only: same name, different bytes. Expected when Kestrel
        embeds XMP into a JPEG, so it is reported but is not a repair prompt.
    ``has_drift``
        True when something needs *repairing* — missing, renamed, or rejected.
        Deliberately excludes ``new``: unanalysed photos are ordinary workflow,
        not damage, and the analyze dialog already reports them.
    """
    result = {
        "root": root or "",
        "scan_status": "ok",
        "scan_error": "",
        "missing": [],
        "new": [],
        "renamed": [],
        "rejected": [],
        "subfolder_hits": [],
        "size_changed": [],
        "db_rows": 0,
        "disk_files": 0,
        "has_drift": False,
    }
    if not root:
        result["scan_status"] = "unreadable"
        result["scan_error"] = "No folder path given"
        return result

    kestrel_dir = os.path.join(root, KESTREL_DIR_NAME)
    db_rows, db_err = _load_db_rows(kestrel_dir)
    if db_err == "no_database":
        result["scan_status"] = "no_database"
        return result
    if db_err:
        result["scan_status"] = "unreadable"
        result["scan_error"] = db_err
        return result

    entries, scan_err = scan_folder_images(root)
    if scan_err:
        # The folder could not be enumerated. Everything below would be a
        # conclusion drawn from a listing we know is wrong, so stop here.
        result["scan_status"] = "unreadable"
        result["scan_error"] = scan_err
        return result

    result["db_rows"] = len(db_rows)
    result["disk_files"] = len(entries)

    disk_sizes: Dict[str, int] = {name: size for name, size in entries}
    db_sizes: Dict[str, "int | None"] = {}
    for name, size in db_rows:
        db_sizes[name] = size

    raw_missing: List[tuple] = []
    for name, size in db_rows:
        if name not in disk_sizes:
            raw_missing.append((name, size))
        else:
            disk = disk_sizes[name]
            if size is not None and disk >= 0 and size != disk:
                result["size_changed"].append(
                    {"filename": name, "stored": size, "disk": disk}
                )

    raw_new: List[tuple] = [
        (name, size) for name, size in entries if name not in db_sizes
    ]

    # Culled originals live in _KESTREL_Rejects. The culling flow removes their
    # rows at move time, so this should normally be empty — but an interrupted
    # move would otherwise present every half-moved photo as deleted.
    #
    # If that folder exists but cannot be read, the scan is NOT authoritative:
    # a culled photo and a deleted one become indistinguishable, and reporting
    # the culled ones as missing would let the repair UI offer to delete
    # analysis data for photos sitting intact in a folder we simply could not
    # open. That is the same failure the root-level scan_status gate exists to
    # prevent, one directory deeper, so it gets the same answer — refuse to
    # draw a conclusion rather than draw a destructive one.
    reject_dir = os.path.join(root, REJECT_DIR_NAME)
    if raw_missing and os.path.isdir(reject_dir):
        reject_entries, reject_err = scan_folder_images(reject_dir)
        if reject_err:
            result["scan_status"] = "unreadable"
            result["scan_error"] = f"{REJECT_DIR_NAME}: {reject_err}"
            return result
        reject_names = {name for name, _ in reject_entries}
        still_missing = []
        for name, size in raw_missing:
            if name in reject_names:
                result["rejected"].append(name)
            else:
                still_missing.append((name, size))
        raw_missing = still_missing

    pairs, matched_missing, matched_new = _match_renames(raw_missing, raw_new)
    result["renamed"] = pairs
    raw_missing = [t for t in raw_missing if t[0] not in matched_missing]
    raw_new = [t for t in raw_new if t[0] not in matched_new]

    if scan_subfolders and raw_missing:
        result["subfolder_hits"] = _scan_subfolders(root, raw_missing)

    result["missing"] = sorted((name for name, _ in raw_missing), key=str.lower)
    result["new"] = sorted((name for name, _ in raw_new), key=str.lower)
    result["rejected"].sort(key=str.lower)
    # ``new`` deliberately does NOT count as drift. Photos added to a folder are
    # ordinary workflow, already surfaced by the analyze dialog's "1500 here,
    # 1000 analysed" count, and badging them as something to *repair* would be
    # both noisy and wrong. They are still reported so the repair dialog can
    # mention them once it is open for a real reason.
    result["has_drift"] = bool(
        result["missing"] or result["renamed"] or result["rejected"]
    )
    return result
