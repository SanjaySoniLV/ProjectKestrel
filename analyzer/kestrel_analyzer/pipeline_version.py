"""The one pipeline version a ``.kestrel/`` folder records, and how to read it.

``config.PIPELINE_VERSION`` is the version of the analysis code that turns photos
into ``.kestrel/`` data. A folder stores it as ``pipeline_version`` in
``kestrel_metadata.json``; that is the only version used to decide whether the
folder's content is current. Its major number is the compatibility boundary
(see ``config.PIPELINE_MAJOR`` and ``upgrade_v3``); minor and patch numbers never
make a folder "outdated".

Folders written before ``pipeline_version`` existed carry the app-style stamps
``version`` (the build that created the folder) and ``kestrel_version`` (the
build of the last run or of the in-place v3 upgrade). They are read here as
fallbacks and never written again.

The app release (``VERSION.txt`` codename, ``VERSION_NUMBER.txt`` number) is a
separate thing: it is logged as ``app_version`` for support, and no decision uses it.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

from .config import ANALYZER_DIR, PIPELINE_MAJOR, PIPELINE_VERSION, REPO_ROOT

# Legacy (pre-``pipeline_version``) stamps; read-only.
_LEGACY_KEYS = ("version", "kestrel_version")


def _first_int(text) -> Optional[int]:
    m = re.match(r"\s*v?(\d+)", str(text or ""))
    return int(m.group(1)) if m else None


def _explicit_major(meta: dict) -> int:
    try:
        return max(0, int(meta.get("pipeline_major") or 0))
    except (TypeError, ValueError):
        return 0


def folder_pipeline_major(meta: dict) -> int:
    """Pipeline generation of a folder: 1, 2, 3, ...

    ``pipeline_major`` wins when present. Otherwise it is the major of the full
    ``pipeline_version``, then of the legacy creating-build ``version``, then 2
    (readable metadata that predates every stamp).
    """
    if not isinstance(meta, dict):
        return 0
    explicit = _explicit_major(meta)
    if explicit:
        return explicit
    for key in ("pipeline_version", "version"):
        major = _first_int(meta.get(key))
        if major:
            return major
    return 2


def folder_pipeline_version(meta: dict) -> str:
    """Full pipeline version string of a folder ('' when none was recorded).

    For a legacy folder already upgraded to pipeline 3 this is ``kestrel_version``
    (the upgrade stamped the upgrading build there); otherwise ``version``, the
    build that analysed the folder, since ``kestrel_version`` is also rewritten
    by top-up runs that leave older rows in place.
    """
    if not isinstance(meta, dict):
        return ""
    current = str(meta.get("pipeline_version") or "").strip()
    if current:
        return current
    if _explicit_major(meta) >= 3:
        legacy = str(meta.get("kestrel_version") or meta.get("version") or "").strip()
    else:
        legacy = str(meta.get("version") or "").strip()
    return legacy


def stamp_pipeline(meta: dict) -> None:
    """Record this build's pipeline as the folder's one pipeline version.

    Only call when every row of the folder was produced (or upgraded) by this
    build. Drops the legacy app-style stamps it supersedes.
    """
    meta["pipeline_version"] = PIPELINE_VERSION
    meta["pipeline_major"] = PIPELINE_MAJOR
    for key in _LEGACY_KEYS:
        meta.pop(key, None)


def read_app_version() -> str:
    """``"Arctic Tern 7.0.0.0"``-style app release label, '' when unavailable."""
    dirs = [str(ANALYZER_DIR), str(REPO_ROOT)]
    if getattr(sys, "_MEIPASS", None):
        dirs.insert(0, sys._MEIPASS)

    def _read(name: str) -> str:
        for d in dirs:
            try:
                with open(os.path.join(d, name), "r", encoding="utf-8") as f:
                    lines = [ln.strip() for ln in f if ln.strip()]
            except OSError:
                continue
            for ln in lines:
                if ln.lower().startswith("version:"):
                    return ln.split(":", 1)[1].strip()
            if lines:
                return lines[0]
        return ""

    return " ".join(p for p in (_read("VERSION.txt"), _read("VERSION_NUMBER.txt")) if p)


def scan_summary(meta) -> dict:
    """What the folder tree needs from a folder's metadata.

    ``{pipeline_major, pipeline_version, upgraded}``; both version values are
    None when the metadata is not a readable object (unknown: never flagged).
    """
    if not isinstance(meta, dict):
        return {"pipeline_major": None, "pipeline_version": None, "upgraded": False}
    return {
        "pipeline_major": folder_pipeline_major(meta),
        "pipeline_version": folder_pipeline_version(meta),
        "upgraded": bool(meta.get("upgraded_to_v3_utc")),
    }
