"""Per-folder cloud-compute metadata.

The desktop's `cloud_jobs.json` (managed by `cloud_jobs_store.py`) is a
*global* ledger keyed by `jobId`. It tracks which jobs the desktop has ever
launched and a few summary fields. Historically it also stored
`downloadedPacks` — but that field was the *only* dedup source, so a JSON
write that crashed mid-flight or a deleted on-disk zip caused already-merged
packs to re-appear as "available" forever.

This module owns the *folder-local* truth: which packs have been merged into
this folder's kestrel database. It lives at
``<folder>/.kestrel/kestrel_cloudcompute.json`` so:

  - It travels with the data. Move/copy the folder → merged-pack info follows.
  - It survives `cloud_jobs.json` corruption.
  - It is the basis for "after merge, delete the pack zip + R2 result" because
    the merged set is now durable and folder-bound.

Bootstrap reconciliation reads BOTH this file (folder truth) and the legacy
``downloadedPacks`` field (desktop-global cache) — their union is the merged
set used to compute "packs to download". New merges write here; the legacy
field is left untouched but no longer the dedup driver.

Atomic-write pattern mirrors `settings_utils.py` / `_to_csv_atomic`:
write to tempfile, flush+fsync, then `os.replace` over the canonical file.
A per-folder in-process lock serializes saves because the live job's
`_on_pack_merged` callback and the resume worker can both write concurrently.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

CLOUD_FOLDER_STATE_FILENAME = "kestrel_cloudcompute.json"
SCHEMA_VERSION = 1

# One lock per absolute folder path — protects concurrent writes from the
# live `_on_pack_merged` callback and the resume worker for the same folder.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GLOBAL = threading.Lock()


def _lock_for(folder_abs: str) -> threading.RLock:
    with _LOCKS_GLOBAL:
        lk = _LOCKS.get(folder_abs)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[folder_abs] = lk
        return lk


def _state_path(folder: Path) -> Path:
    return folder / ".kestrel" / CLOUD_FOLDER_STATE_FILENAME


def _empty_state() -> dict:
    return {"version": SCHEMA_VERSION, "jobs": {}}


def _read_raw(folder: Path) -> dict:
    p = _state_path(folder)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return _empty_state()
    except (OSError, json.JSONDecodeError):
        # Corrupt → back up + reset. We'd rather lose the merged-packs index
        # (which the resume code can rebuild from on-disk zips + downloaded-
        # Packs) than block the user with an unparseable file.
        try:
            os.replace(p, str(p) + ".corrupt")
        except OSError:
            pass
        return _empty_state()
    return _empty_state()


def _write_atomic(folder: Path, data: dict) -> None:
    target_dir = folder / ".kestrel"
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = _state_path(folder)
    fd, tmp_path = tempfile.mkstemp(
        prefix=CLOUD_FOLDER_STATE_FILENAME + ".",
        suffix=".tmp",
        dir=str(target_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=False)
            # flush() errors (ENOSPC, EIO) must propagate: a partial temp
            # file must not be os.replace'd over a good destination.
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync can legitimately fail on some network filesystems;
                # the replace below still gives readers an all-or-nothing view.
                pass
        os.replace(tmp_path, final_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _job_block(state: dict, job_id: str) -> dict:
    jobs = state.setdefault("jobs", {})
    block = jobs.get(job_id)
    if not isinstance(block, dict):
        block = {"mergedPacks": [], "firstMergedAtUtc": None, "lastMergedAtUtc": None}
        jobs[job_id] = block
    block.setdefault("mergedPacks", [])
    return block


def list_merged_packs(folder: Path | str, job_id: str) -> list[str]:
    """Return the packs already merged into this folder for ``job_id``. Returns
    an empty list if the folder doesn't have a state file yet, or the job
    isn't tracked yet."""
    folder = Path(folder)
    folder_abs = str(folder.resolve()) if folder.is_dir() else str(folder)
    with _lock_for(folder_abs):
        state = _read_raw(folder)
    block = (state.get("jobs") or {}).get(job_id) or {}
    packs = block.get("mergedPacks") or []
    return [str(p) for p in packs if isinstance(p, str)]


def is_pack_merged(folder: Path | str, job_id: str, pack_name: str) -> bool:
    return pack_name in list_merged_packs(folder, job_id)


def mark_pack_merged(folder: Path | str, job_id: str, pack_name: str) -> None:
    """Record that ``pack_name`` has been successfully merged into the kestrel
    database for ``folder`` under ``job_id``. Idempotent."""
    if not job_id or not pack_name:
        return
    folder = Path(folder)
    folder_abs = str(folder.resolve()) if folder.is_dir() else str(folder)
    with _lock_for(folder_abs):
        state = _read_raw(folder)
        block = _job_block(state, job_id)
        if pack_name not in block["mergedPacks"]:
            block["mergedPacks"].append(pack_name)
        now = _utc_now_iso()
        if not block.get("firstMergedAtUtc"):
            block["firstMergedAtUtc"] = now
        block["lastMergedAtUtc"] = now
        _write_atomic(folder, state)


def mark_packs_merged(folder: Path | str, job_id: str, pack_names: list[str]) -> None:
    """Batch variant — single read+write for multiple packs."""
    if not job_id or not pack_names:
        return
    folder = Path(folder)
    folder_abs = str(folder.resolve()) if folder.is_dir() else str(folder)
    with _lock_for(folder_abs):
        state = _read_raw(folder)
        block = _job_block(state, job_id)
        added = False
        for name in pack_names:
            if isinstance(name, str) and name and name not in block["mergedPacks"]:
                block["mergedPacks"].append(name)
                added = True
        if added:
            now = _utc_now_iso()
            if not block.get("firstMergedAtUtc"):
                block["firstMergedAtUtc"] = now
            block["lastMergedAtUtc"] = now
            _write_atomic(folder, state)


def remove_job(folder: Path | str, job_id: str) -> bool:
    """Drop a job entry from the folder-local state. Returns True if removed.
    Used when the user clears the job from their queue."""
    if not job_id:
        return False
    folder = Path(folder)
    folder_abs = str(folder.resolve()) if folder.is_dir() else str(folder)
    with _lock_for(folder_abs):
        state = _read_raw(folder)
        jobs = state.get("jobs") or {}
        if job_id not in jobs:
            return False
        jobs.pop(job_id, None)
        _write_atomic(folder, state)
        return True
