"""Persistent job ledger for cloud compute.

Records every cloud-compute job the desktop has submitted so that the next
launch can poll for finished result packs and prompt the user to download
them. Lives next to ``settings.json`` in the platform-specific user data dir.

Schema (JSON):
    {
      "jobs": [
        {
          "jobId": str,
          "folderPath": str,            # absolute path of analyzed folder
          "createdAtUtc": str,          # ISO-8601
          "status": str,                # uploading|downloading|done|cancelled|failed|upload_paused
          "imageCount": int,
          "settingsSnapshot": dict,     # filtered analysis-settings sent to Modal
          "downloadedPacks": [str, ...] # filenames already merged locally
        },
        ...
      ]
    }

Atomic-write pattern mirrors ``settings_utils.py`` / ``_to_csv_atomic``:
write to tempfile, flush+fsync, then ``os.replace`` over the canonical file.
A single in-process re-entrant lock serializes saves because the JS bridge,
the per-job background download thread, and startup all write concurrently.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from typing import Any

CLOUD_JOBS_FILENAME = "cloud_jobs.json"
_SAVE_LOCK = threading.RLock()

# 'incomplete' = the client disconnected >10min with uploads UNFINISHED. Uploads
# are halted, but analysis CONTINUES server-side and result packs remain
# downloadable from R2 for ~30 days. It stays in _TERMINAL_STATUSES because it's
# terminal for UPLOAD-resume (uploads can never resume) and badge-folding logic
# that keys off this set. It is, however, DOWNLOAD-resumable: see
# _DOWNLOAD_RESUMABLE_STATUSES below.
_TERMINAL_STATUSES = {"done", "cancelled", "failed", "incomplete"}

# Statuses that are terminal-for-upload but whose result packs may still be
# pulled from R2. list_pending_jobs treats these as download-resumable
# (NOT skipped) when the worker still reports available, un-merged packs.
# 'incomplete': uploads halted, analysis continued server-side, packs live ~30d.
_DOWNLOAD_RESUMABLE_STATUSES = {"incomplete"}
_VALID_STATUSES = {
    "uploading", "downloading", "done",
    "cancelled", "failed", "upload_paused", "incomplete",
}
_MAX_JOBS_RETAINED = 200  # oldest non-terminal kept; terminal jobs prune on touch


def _user_data_dir() -> str:
    """Same directory ``settings_utils.py`` writes to. Kept in sync by convention."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ProjectKestrel")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "ProjectKestrel")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "project-kestrel")


def _store_path() -> str:
    return os.path.join(_user_data_dir(), CLOUD_JOBS_FILENAME)


def _coerce_str(v: Any, *, max_len: int = 4096) -> str:
    if v is None:
        return ""
    s = str(v)
    return s[:max_len]


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce_strlist(v: Any, *, max_len: int = 1024) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(item)[:max_len] for item in v if item is not None][:_MAX_JOBS_RETAINED * 4]


def _coerce_dict(v: Any) -> dict:
    return dict(v) if isinstance(v, dict) else {}


def _sanitize_job(raw: Any) -> dict | None:
    """Return a canonical job dict, or ``None`` if input is unusable."""
    if not isinstance(raw, dict):
        return None
    job_id = _coerce_str(raw.get("jobId"), max_len=128).strip()
    folder = _coerce_str(raw.get("folderPath"), max_len=4096).strip()
    if not job_id or not folder:
        return None
    status = _coerce_str(raw.get("status"), max_len=32) or "uploading"
    if status not in _VALID_STATUSES:
        status = "uploading"
    return {
        "jobId": job_id,
        "folderPath": folder,
        # Stable id (JWT `sub`) of the account that submitted this job. Stamped
        # at submit time so history can be filtered to the current account —
        # switching accounts must never surface another user's jobs. Empty for
        # legacy rows written before this field existed; those are claimed by
        # the first signed-in account that lists them (adopt_unowned_jobs).
        "ownerId": _coerce_str(raw.get("ownerId"), max_len=128),
        "createdAtUtc": _coerce_str(raw.get("createdAtUtc"), max_len=64),
        "status": status,
        # Free-form short tag explaining a non-obvious terminal status (e.g.
        # "upload_interrupted" from a legacy local mark). Surfaced in the
        # cloud queue panel when set.
        "failureReason": _coerce_str(raw.get("failureReason"), max_len=64),
        # Worker terminal_reason captured when the job reached a terminal state
        # (complete | client_disconnected | modal_retries_exhausted |
        # runaway_dispatch | stalled_no_container | user_cancel | orphan_reaped …).
        # Lets the account panel's history show a specific "why it ended"
        # message in later sessions without a Worker round-trip. Empty until
        # a terminal reason is observed.
        "terminalReason": _coerce_str(raw.get("terminalReason"), max_len=64),
        "imageCount": _coerce_int(raw.get("imageCount")),
        "anchorFilename": _coerce_str(raw.get("anchorFilename"), max_len=512),
        "settingsSnapshot": _coerce_dict(raw.get("settingsSnapshot")),
        "downloadedPacks": _coerce_strlist(raw.get("downloadedPacks")),
    }


def _load_raw() -> dict:
    path = _store_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {"jobs": []}
    except (OSError, json.JSONDecodeError):
        # Corrupt file — back it up and start fresh rather than blocking the user.
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return {"jobs": []}
    return {"jobs": []}


def _write_atomic(data: dict) -> None:
    user_dir = _user_data_dir()
    os.makedirs(user_dir, exist_ok=True)
    final_path = _store_path()
    fd, tmp_path = tempfile.mkstemp(
        prefix="cloud_jobs.json.", suffix=".tmp", dir=user_dir
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


def load_jobs() -> list[dict]:
    """Return all known cloud jobs, sanitized. Safe on first run (empty file)."""
    with _SAVE_LOCK:
        raw = _load_raw()
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw.get("jobs", []) if isinstance(raw, dict) else []:
        job = _sanitize_job(item)
        if job is None or job["jobId"] in seen:
            continue
        seen.add(job["jobId"])
        out.append(job)
    return out


def save_jobs(jobs: list[dict]) -> None:
    """Replace the on-disk job list with ``jobs`` (sanitized), enforcing the
    ``_MAX_JOBS_RETAINED`` cap. When over the cap, the oldest terminal entries
    (done|cancelled|failed, sorted by ``createdAtUtc``) are evicted first so
    active jobs are never dropped."""
    sanitized: list[dict] = []
    seen: set[str] = set()
    for item in jobs:
        job = _sanitize_job(item)
        if job is None or job["jobId"] in seen:
            continue
        seen.add(job["jobId"])
        sanitized.append(job)
    if len(sanitized) > _MAX_JOBS_RETAINED:
        non_terminal = [j for j in sanitized if j["status"] not in _TERMINAL_STATUSES]
        terminal = sorted(
            (j for j in sanitized if j["status"] in _TERMINAL_STATUSES),
            key=lambda j: j.get("createdAtUtc") or "",
        )
        excess = len(sanitized) - _MAX_JOBS_RETAINED
        sanitized = non_terminal + terminal[excess:]
    with _SAVE_LOCK:
        _write_atomic({"jobs": sanitized})


def upsert_job(job: dict) -> dict | None:
    """Insert or replace a single job by ``jobId``. Returns the canonical row."""
    canonical = _sanitize_job(job)
    if canonical is None:
        return None
    with _SAVE_LOCK:
        existing = load_jobs()
        merged: list[dict] = []
        replaced = False
        for j in existing:
            if j["jobId"] == canonical["jobId"]:
                merged.append(canonical)
                replaced = True
            else:
                merged.append(j)
        if not replaced:
            merged.append(canonical)
        save_jobs(merged)
        return canonical


def update_job(job_id: str, **fields: Any) -> dict | None:
    """Patch fields on an existing job. Unknown fields are ignored. Returns the
    updated row, or ``None`` if the job_id is unknown."""
    job_id = (job_id or "").strip()
    if not job_id:
        return None
    with _SAVE_LOCK:
        existing = load_jobs()
        updated: dict | None = None
        for j in existing:
            if j["jobId"] == job_id:
                for k, v in fields.items():
                    if k in j:
                        j[k] = v
                updated = _sanitize_job(j)
                if updated is not None:
                    j.update(updated)
                break
        if updated is not None:
            save_jobs(existing)
        return updated


def add_downloaded_pack(job_id: str, pack_filename: str) -> None:
    """Record that a pack has been downloaded/merged locally. Idempotent."""
    job_id = (job_id or "").strip()
    pack_filename = (pack_filename or "").strip()
    if not job_id or not pack_filename:
        return
    with _SAVE_LOCK:
        existing = load_jobs()
        for j in existing:
            if j["jobId"] == job_id:
                packs = list(j.get("downloadedPacks") or [])
                if pack_filename not in packs:
                    packs.append(pack_filename)
                    j["downloadedPacks"] = packs
                    save_jobs(existing)
                return


def jobs_for_owner(owner_id: str, *, adopt_unowned: bool = True) -> list[dict]:
    """Return jobs belonging to ``owner_id``.

    Legacy rows with an empty ``ownerId`` predate per-account tagging; we can't
    know who submitted them. When ``adopt_unowned`` (the default), the first
    signed-in account that lists history claims them — they're stamped with
    ``owner_id`` and persisted, so the dominant single-user case keeps its full
    pre-upgrade history and rediscovery (downloaded-pack counts, packs for
    folders that aren't currently mounted) without leaking across accounts going
    forward. Returns [] when ``owner_id`` is empty (signed out)."""
    owner_id = (owner_id or "").strip()
    if not owner_id:
        return []
    with _SAVE_LOCK:
        existing = load_jobs()
        changed = False
        for j in existing:
            if not (j.get("ownerId") or "").strip() and adopt_unowned:
                j["ownerId"] = owner_id
                changed = True
        if changed:
            save_jobs(existing)
        return [j for j in existing if (j.get("ownerId") or "") == owner_id]


def remove_job(job_id: str) -> bool:
    """Drop a job entry. Returns True if removed."""
    job_id = (job_id or "").strip()
    if not job_id:
        return False
    with _SAVE_LOCK:
        existing = load_jobs()
        before = len(existing)
        existing = [j for j in existing if j["jobId"] != job_id]
        if len(existing) == before:
            return False
        save_jobs(existing)
        return True


def remove_terminal_jobs() -> list[str]:
    """Drop every job in a terminal status (done|cancelled|failed).
    Returns the list of removed job IDs."""
    with _SAVE_LOCK:
        existing = load_jobs()
        removed = [j["jobId"] for j in existing if j["status"] in _TERMINAL_STATUSES]
        if not removed:
            return []
        kept = [j for j in existing if j["status"] not in _TERMINAL_STATUSES]
        save_jobs(kept)
        return removed


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
