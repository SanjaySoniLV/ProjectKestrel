"""HTTP client + local log for quality feedback (see kestrel_analyzer/quality_feedback.py).

Flow, after the user has agreed to share:

1. ``POST {base}/v1/feedback/quality`` with the metadata -> a submission id and
   one presigned PUT URL per crop.
2. ``PUT`` each crop's JPEG bytes straight to its URL (the bytes never pass
   through the API server). A failed PUT is retried once.
3. ``POST {base}/v1/feedback/quality/{id}/commit``. A ``409`` lists the crops
   the server could not find; those are PUT again once and the commit retried.

Nothing identifies the user: no account token is sent. The only identifier is
a random per-install id (``qfi_…``) that the user can reset from Settings;
``DELETE {base}/v1/feedback/quality/installs/{install_id}`` removes everything
sent under it.

API base: the Perch API (``https://perchapi.projectkestrel.org``). For local
testing against ``wrangler dev`` set the environment variable
``KESTREL_QF_API_BASE`` (e.g. ``http://127.0.0.1:8787``); there is deliberately
no settings or UI override.

Each successful submission appends one line to ``quality_feedback_log.jsonl``
in the app's user-data folder (submission id, time, species, ranking, crop
count; never file paths) so the user can see what they've shared.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

import requests

DEFAULT_API_BASE = "https://perchapi.projectkestrel.org"
API_BASE_ENV = "KESTREL_QF_API_BASE"
LOG_FILENAME = "quality_feedback_log.jsonl"
TIMEOUT = 20
PUT_TIMEOUT = 60
MAX_LOG_ENTRIES_RETURNED = 500

_log_lock = threading.Lock()


class QualityFeedbackError(Exception):
    """A submission failed. ``code`` is a short machine-readable reason."""

    def __init__(self, code: str, message: str = "", status: Optional[int] = None):
        super().__init__(message or code)
        self.code = code
        self.status = status


def api_base() -> str:
    return (os.environ.get(API_BASE_ENV) or DEFAULT_API_BASE).strip().rstrip("/")


def new_install_id() -> str:
    return "qfi_" + secrets.token_hex(16)


def _json(r: requests.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _error_from(r: requests.Response, fallback: str) -> QualityFeedbackError:
    body = _json(r)
    code = str(body.get("error") or fallback)
    if r.status_code == 429:
        code = "rate_limited"
    return QualityFeedbackError(code, f"{fallback}: HTTP {r.status_code} {code}", status=r.status_code)


def _put(session: requests.Session, upload: dict, data: bytes) -> bool:
    headers = {"Content-Type": "image/jpeg"}
    extra = upload.get("headers")
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    try:
        r = session.put(upload["url"], data=data, headers=headers, timeout=PUT_TIMEOUT)
        return 200 <= r.status_code < 300
    except requests.RequestException:
        return False


def submit(
    payload: dict,
    crops: Sequence[bytes],
    *,
    base: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> dict:
    """Run create -> PUT x n -> commit. Returns ``{"submission_id": …}``.

    ``crops[i]`` is the JPEG for the crop with ``index == i``. Raises
    :class:`QualityFeedbackError` on any failure (network errors included).
    """
    base = (base or api_base()).rstrip("/")
    s = session or requests.Session()
    try:
        try:
            r = s.post(f"{base}/v1/feedback/quality", json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise QualityFeedbackError("network", str(e)) from e
        if r.status_code not in (200, 201):
            raise _error_from(r, "create_failed")
        body = _json(r)
        sid = str(body.get("submission_id") or "")
        uploads = body.get("uploads")
        if not sid or not isinstance(uploads, list):
            raise QualityFeedbackError("bad_response", "create returned no uploads")
        by_index = {}
        for u in uploads:
            if isinstance(u, dict) and isinstance(u.get("url"), str):
                try:
                    by_index[int(u.get("index"))] = u
                except (TypeError, ValueError):
                    continue
        if set(by_index) != set(range(len(crops))):
            raise QualityFeedbackError("bad_response", "upload URLs do not match the crops")

        def put_all(indexes):
            for i in indexes:
                if not _put(s, by_index[i], crops[i]):
                    _put(s, by_index[i], crops[i])  # one retry, then let commit decide

        def commit():
            try:
                return s.post(
                    f"{base}/v1/feedback/quality/{sid}/commit",
                    json={"install_id": payload.get("install_id")},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as e:
                raise QualityFeedbackError("network", str(e)) from e

        put_all(range(len(crops)))
        r = commit()
        if r.status_code == 409:
            missing = _json(r).get("missing")
            if isinstance(missing, list):
                idx = [int(i) for i in missing if isinstance(i, int) and i in by_index]
            else:
                idx = []
            put_all(idx or range(len(crops)))
            r = commit()
        if r.status_code != 200:
            raise _error_from(r, "commit_failed")
        return {"submission_id": sid}
    finally:
        if session is None:
            s.close()


def forget(install_id: str, *, base: Optional[str] = None, session: Optional[requests.Session] = None) -> int:
    """Delete everything the server holds for ``install_id``. Returns the count."""
    base = (base or api_base()).rstrip("/")
    s = session or requests.Session()
    try:
        try:
            r = s.delete(f"{base}/v1/feedback/quality/installs/{install_id}", timeout=TIMEOUT)
        except requests.RequestException as e:
            raise QualityFeedbackError("network", str(e)) from e
        if r.status_code != 200:
            raise _error_from(r, "forget_failed")
        try:
            return int(_json(r).get("deleted") or 0)
        except (TypeError, ValueError):
            return 0
    finally:
        if session is None:
            s.close()


# ── Local log ──────────────────────────────────────────────────────────────


def default_log_path() -> str:
    try:
        from settings_utils import _get_user_data_dir
    except ImportError:  # pragma: no cover - package-style import
        from analyzer.settings_utils import _get_user_data_dir  # type: ignore
    return os.path.join(_get_user_data_dir(), LOG_FILENAME)


def log_entry(submission_id: str, payload: dict, now: Optional[datetime] = None) -> dict:
    """What gets written locally: no paths, no install id."""
    now = now or datetime.now(timezone.utc)
    return {
        "submission_id": submission_id,
        "time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "species": list(payload.get("species") or []),
        "family": list(payload.get("family") or []),
        "thumbs": payload.get("thumbs"),
        "active_scorer": payload.get("active_scorer"),
        "ranking": payload.get("ranking"),
        "n": len(payload.get("crops") or []),
    }


def append_log(entry: dict, path: Optional[str] = None) -> None:
    path = path or default_log_path()
    with _log_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(path: Optional[str] = None, limit: int = MAX_LOG_ENTRIES_RETURNED) -> list[dict]:
    """Entries newest first (malformed lines skipped)."""
    path = path or default_log_path()
    out: list[dict] = []
    try:
        with _log_lock, open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        return []
    out.reverse()
    return out[: max(0, int(limit))]


def clear_log(path: Optional[str] = None) -> None:
    path = path or default_log_path()
    with _log_lock:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def run_in_background(fn: Callable[[], Any], on_done: Callable[[Any, Optional[BaseException]], None]) -> threading.Thread:
    """Run ``fn`` on a daemon thread and report ``(result, error)`` to ``on_done``."""

    def _runner():
        try:
            res = fn()
        except BaseException as e:  # noqa: BLE001 - reported, never raised on the thread
            on_done(None, e)
            return
        on_done(res, None)

    t = threading.Thread(target=_runner, name="quality-feedback-upload", daemon=True)
    t.start()
    return t
