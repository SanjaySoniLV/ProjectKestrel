"""
Cloud-compute client — desktop-side wrapper for the Kestrel cloud-compute Worker.

Adapted from kestrel-cloud-compute-client/client/upload_test.py. Same protocol;
exposed as a class so api_bridge.py can drive it from a worker thread instead
of running the CLI as a subprocess. Auth comes from the Perch JWT — same
identity that gates Perch — set on the constructor.

Output of `run_full_job` lands in <images_dir>/.kestrel/cloud-packs/ (raw
pack zips) and is merged into <images_dir>/.kestrel/ (database CSV, scenedata,
metadata, crops, exports). Same on-disk layout the local pipeline writes.
"""

from __future__ import annotations

import concurrent.futures
import csv
import hashlib
import hmac
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

# Shared certifi-backed TLS context. Without it, bare urlopen() fails every
# HTTPS request with CERTIFICATE_VERIFY_FAILED in a frozen macOS .app. See
# net_tls for the full rationale. Dual import so the module resolves whether
# this package is on sys.path as the root (frozen / tests) or as ``analyzer``.
try:
    from net_tls import ssl_context as _ssl_context
except ImportError:  # pragma: no cover - package-style import path
    from analyzer.net_tls import ssl_context as _ssl_context

try:
    from kestrel_analyzer.database import retry_on_file_lock
except ImportError:  # pragma: no cover - package-style import path
    from analyzer.kestrel_analyzer.database import retry_on_file_lock


_DEFAULT_API_BASE = "https://cloudcompute.projectkestrel.org"
_MAX_UPLOAD_WORKERS = 6
_POLL_INTERVAL_SEC = 5
# R2 PUTs stream the whole file body; cap each attempt so a stalled socket
# can't hang the upload pool forever — critical at app shutdown, where the
# ThreadPoolExecutor's atexit join would otherwise wait on a wedged upload
# even after cancel_event short-circuits the queued ones.
_PUT_TIMEOUT_SEC = 60
# Per-call timeouts. Status polls are short so a stuck poller can't freeze the
# UI; submit/notify need a bit more headroom for the first request after a cold
# Worker. See JobCancelled below for cooperative shutdown.
_STATUS_TIMEOUT_SEC = 15
_NOTIFY_TIMEOUT_SEC = 30

# Analysis-settings allowlist — mirrors the Worker's and Modal's allowlists
# (defence in depth). Only these keys are sent in the ``analysisSettings``
# field of POST /api/jobs. Anything outside this tuple is dropped before the
# request goes out, so the desktop can pass its full settings dict and trust
# the filter.
ANALYSIS_SETTINGS_ALLOWLIST: tuple[str, ...] = (
    "detector_name",
    "species_detection_enabled",
    "wildlife_enabled",
    "confidence_threshold",
    "scene_grouping_enabled",
    "crop_generation_enabled",
    "quality_model_enabled",
    # Advanced analysis settings (settings.json names verbatim — no rename like
    # detection_threshold->confidence_threshold). Modal's _settings_to_cli_args
    # converts these into the matching CLI flags.
    "max_bird_crops",
    "exposure_quality",
    "scene_time_threshold",
    "thumbnail_max_width",
    "thumbnail_jpeg_compression",
    "retry_errored",
)


def filter_analysis_settings(raw: Any) -> dict | None:
    """Return a copy of ``raw`` containing only allowlisted keys with primitive
    values. ``None`` is returned when nothing survives so callers can decide
    whether to omit the field from the wire payload entirely (vs. sending an
    empty object, which the Worker would treat the same way)."""
    if not isinstance(raw, dict):
        return None
    cleaned: dict = {}
    for key in ANALYSIS_SETTINGS_ALLOWLIST:
        if key not in raw:
            continue
        val = raw[key]
        if isinstance(val, (str, int, float, bool)):
            cleaned[key] = val
    return cleaned or None


def default_api_base() -> str:
    """Resolve cloud-compute Worker base URL — env override, then default."""
    return os.environ.get("KESTREL_CC_API_BASE", _DEFAULT_API_BASE).rstrip("/")


def _safe_pack_filename(name: Any) -> Optional[str]:
    """Reduce a Worker-supplied pack filename to a trusted basename, or None.

    Result packs are named server-side, so a malicious/compromised Worker (or a
    MITM, despite TLS) could return a traversal name like ``../../evil.zip`` or
    ``/etc/x.zip``. Joining that to the local pack dir would let it be written —
    or, worse, read back and merged — from an arbitrary path. We accept the name
    only when it is already a bare basename with no separators, parent refs,
    drive/colon, or NUL. This mirrors the zip-member guard in
    ``merge_pack_into_kestrel`` and ``_sanitize_plain_filename`` in api_bridge.
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    # Any separator (posix or windows), parent ref, drive-colon or NUL is out.
    if ("/" in raw or "\\" in raw or "\x00" in raw or ":" in raw):
        return None
    base = os.path.basename(raw)
    if base != raw or base in ("", ".", ".."):
        return None
    return base


def _discover_upload_images(folder: Path) -> list[Path]:
    """Discover analyzable images in ``folder``, honouring the same RAW-priority
    rule the desktop pipeline and folder inspector use: when the folder contains
    any RAW files we return only RAWs (their JPEG sidecars are skipped); only
    when there are zero RAWs do we fall back to JPEGs. Hidden files and macOS
    AppleDouble (``._*``) companions are filtered out.

    Delegates to ``folder_inspector.list_images_in_folder`` so the upload-speed
    test and the real job agree on exactly which files count — keeping the
    cloud paths in lock-step with the canonical ``RAW_EXTENSIONS`` /
    ``JPEG_EXTENSIONS`` config instead of a hardcoded ``cr3/jpg/jpeg`` subset.
    Returns absolute ``Path`` objects sorted by filename (the canonical
    processing order). Non-recursive.
    """
    try:
        from folder_inspector import list_images_in_folder
    except ImportError:  # packaged / fully-qualified import path
        from analyzer.folder_inspector import list_images_in_folder  # type: ignore[no-redef]
    return [folder / name for name in list_images_in_folder(str(folder))]


class CloudComputeError(RuntimeError):
    """Raised on non-2xx response from the cloud-compute Worker."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class CloudComputeAuthError(CloudComputeError):
    """Raised when a worker call returns 401 (expired/invalid session) and the
    client could not recover by refreshing the token. Distinct from a generic
    ``CloudComputeError`` so callers can treat it as TRANSIENT (the session
    expired — e.g. after laptop sleep — but the job is fine server-side) and
    keep the job in its current non-terminal local state instead of marking it
    'failed'. ``status`` is always 401."""

    def __init__(self, message: str = "Session expired") -> None:
        super().__init__(401, message)


class CloudComputeNetworkError(CloudComputeError):
    """Raised on transport-level failure talking to the Worker (timeout, DNS,
    connection reset, malformed JSON). Distinct from ``CloudComputeError``
    (HTTP-level error) so callers can decide whether to back off and retry vs.
    treat as a hard failure (e.g. 401 needSignIn). ``status`` is 0 for these."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class JobInProgressError(CloudComputeError):
    """The user already has a Cloud Compute job in flight. Cloud-compute
    worker returned 403 with reason='job_in_progress' from the Auth Worker's
    concurrency gate. Carries the activeJobId so the UI can offer a deep-link
    to MyAccount, plus the richer fields ``activeJobIds`` / ``current`` /
    ``limit`` so the desktop's auto-drain queue can decide between
    "shelve and wait" vs "show orphan warning" without an extra round-trip
    to the Auth Worker's entitlements endpoint."""

    def __init__(
        self,
        active_job_id: str | None,
        message: str,
        *,
        active_job_ids: list[str] | None = None,
        current: int | None = None,
        limit: int | None = None,
    ):
        super().__init__(403, message)
        self.active_job_id = active_job_id
        self.active_job_ids = list(active_job_ids) if active_job_ids else []
        self.current = current
        self.limit = limit


class LegalAcceptanceRequiredError(CloudComputeError):
    """The user must agree to the current ToS + Privacy Policy before
    submitting a new job. Cloud-compute worker returned 403 with
    ``error='legal_acceptance_required'`` from the Auth Worker's legal gate
    (launch item #13). Carries the URL the desktop should open in the
    system browser so the user can review and accept."""

    def __init__(self, accept_url: str | None, current_effective_date: str | None, message: str):
        super().__init__(403, message)
        self.accept_url = accept_url or "https://myaccount.projectkestrel.org/legal/accept"
        self.current_effective_date = current_effective_date


class JobCancelled(RuntimeError):
    """Raised inside ``run_full_job`` when the supplied ``cancel_event`` fires.
    Distinct from generic exceptions so the caller can mark the job
    ``cancelled`` (not ``failed``) without inspecting the message string."""


class CloudComputeClient:
    """Stateless-ish wrapper around the cloud-compute Worker REST API.

    Reuses the auth-header construction pattern from
    `analyzer/perch_uploader.py:PerchKestrelUploader.__init__`.
    """

    # Bounded number of token-refresh-and-retry attempts on a 401. One retry is
    # enough for the laptop-sleep case (token expired while suspended); the cap
    # stops a permanently-revoked session from looping forever.
    _MAX_AUTH_RETRIES = 2

    def __init__(
        self,
        api_base: str,
        jwt_token: str | None,
        timeout: int = 120,
        dev_user: str | None = None,
        token_provider: Optional[Callable[[], str | None]] = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        # ``token_provider`` is an optional zero-arg callable returning a FRESH
        # JWT (the bridge wires it to _check_auth_token, which triggers an OAuth
        # refresh). On a 401 we call it, swap the new token into our auth header
        # and retry the request — so a session that expires mid-job (e.g. after
        # laptop sleep) self-heals instead of failing the whole job.
        self._token_provider = token_provider
        self._auth_lock = threading.Lock()
        self._auth_headers: dict = {}
        du = dev_user or os.environ.get("KESTREL_DEV_USER_ID")
        if du:
            self._auth_headers["x-dev-user-id"] = str(du)
        t = str(jwt_token).strip() if jwt_token else ""
        if t:
            self._auth_headers["Authorization"] = f"Bearer {t}"
        if not du and not t:
            raise ValueError(
                "CloudComputeClient needs a Clerk JWT (preferred) or "
                "KESTREL_DEV_USER_ID (wrangler dev only)"
            )

    # ─── HTTP helpers ────────────────────────────────────────────────────

    def _refresh_token(self) -> bool:
        """Ask ``token_provider`` for a fresh JWT and swap it into the auth
        header. Returns True if a usable (and, when possible, *different*) token
        was installed, False otherwise. Thread-safe: serialised so concurrent
        upload/poller/download threads that all 401 at once refresh once."""
        if self._token_provider is None:
            return False
        with self._auth_lock:
            try:
                fresh = self._token_provider()
            except Exception:
                return False
            t = str(fresh).strip() if fresh else ""
            if not t:
                return False
            new_header = f"Bearer {t}"
            # If the provider handed back the same token we already hold, the
            # refresh didn't actually advance — retrying would just 401 again.
            if self._auth_headers.get("Authorization") == new_header:
                return False
            self._auth_headers["Authorization"] = new_header
            return True

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        url = f"{self.api_base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        # On a 401 we refresh the token and retry (bounded). The body/data are
        # re-used as-is; only the Authorization header changes between attempts.
        for attempt in range(self._MAX_AUTH_RETRIES + 1):
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "KestrelDesktop/CloudCompute/1.0",
                **self._auth_headers,
            }
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=_ssl_context()) as resp:
                    raw = resp.read()
                    if not raw:
                        return {}
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as e:
                        raise CloudComputeNetworkError(
                            f"Worker returned malformed JSON: {e}"
                        ) from e
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < self._MAX_AUTH_RETRIES and self._refresh_token():
                    continue  # got a fresh token — retry with new Authorization
                text = e.read().decode("utf-8", errors="replace")
                if e.code == 401:
                    # Couldn't recover (no provider / refresh failed / still
                    # 401). Surface as the transient auth error so the caller
                    # keeps the job alive rather than marking it 'failed'.
                    raise CloudComputeAuthError("Session expired") from e
                raise CloudComputeError(e.code, text) from e
            except urllib.error.URLError as e:
                raise CloudComputeNetworkError(f"network error: {e.reason}") from e
            except socket.timeout as e:
                raise CloudComputeNetworkError(f"request timed out after {timeout or self.timeout}s") from e
            except (TimeoutError, ConnectionError) as e:
                raise CloudComputeNetworkError(f"transport error: {e}") from e
        # Unreachable: the loop either returns or raises on the final attempt.
        raise CloudComputeAuthError("Session expired")

    # ─── REST endpoints ──────────────────────────────────────────────────

    def submit_job(
        self,
        file_paths: list[Path],
        analysis_settings: dict | None = None,
    ) -> dict:
        """POST /api/jobs — returns {jobId, presignedUrls, ...}.

        ``analysis_settings`` is filtered through
        :func:`filter_analysis_settings` before send; the Worker re-validates
        on receipt (defence in depth). Pass ``None`` to let Modal use its
        built-in defaults.
        """
        if not file_paths:
            raise ValueError("submit_job requires at least one file path")
        body: dict = {
            "imageCount": len(file_paths),
            "fileNames": [p.name for p in file_paths],
        }
        cleaned = filter_analysis_settings(analysis_settings)
        if cleaned is not None:
            body["analysisSettings"] = cleaned
        try:
            return self._request("POST", "/api/jobs", body)
        except CloudComputeError as e:
            # Stage 6 concurrency gate: Auth Worker rejects a second concurrent
            # job per user. The cloud-compute Worker propagates this as a 403
            # with JSON body {error:'job_in_progress', activeJobId, message}.
            # Surface it as a typed exception so api_bridge can show a
            # MyAccount deep-link instead of a generic "submit failed". Older
            # workers without this gate either return a different 403 body
            # shape or a non-403 — in either case we fall through and re-raise.
            if e.status == 403:
                try:
                    parsed = json.loads(e.message)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("error") == "job_in_progress":
                    # Worker also returns `activeJobIds[]`, `current`, `limit`
                    # (see kestrel-cloud-compute-cloudflare-worker/src/index.ts
                    # handleCreateJob's job_in_progress branch). Capture them
                    # so the desktop can show "1/1 used" without a second
                    # round-trip to /v1/me/entitlements.
                    raw_ids = parsed.get("activeJobIds")
                    active_ids = (
                        [str(x) for x in raw_ids if x]
                        if isinstance(raw_ids, list)
                        else None
                    )
                    cur_val = parsed.get("current")
                    lim_val = parsed.get("limit")
                    try:
                        current = int(cur_val) if cur_val is not None else None
                    except (TypeError, ValueError):
                        current = None
                    try:
                        limit = int(lim_val) if lim_val is not None else None
                    except (TypeError, ValueError):
                        limit = None
                    raise JobInProgressError(
                        parsed.get("activeJobId"),
                        str(parsed.get("message") or "You have a Cloud Compute job running."),
                        active_job_ids=active_ids,
                        current=current,
                        limit=limit,
                    ) from e
                # Launch item #13 — ToS / Privacy Policy gate. The worker
                # returns the URL the system browser should open; api_bridge
                # surfaces a "review updated terms" dialog around this and
                # launches the browser.
                if isinstance(parsed, dict) and parsed.get("error") == "legal_acceptance_required":
                    raise LegalAcceptanceRequiredError(
                        parsed.get("accept_url"),
                        parsed.get("currentEffectiveDate"),
                        str(
                            parsed.get("message")
                            or "Project Kestrel's Terms of Service or Privacy Policy have been updated."
                        ),
                    ) from e
            raise

    def get_status(self, job_id: str) -> dict:
        # Short timeout: this is the UI poller, a stuck call must not freeze
        # the panel for two minutes.
        return self._request("GET", f"/api/jobs/{job_id}", timeout=_STATUS_TIMEOUT_SEC)

    def notify_uploaded(self, job_id: str, filenames: list[str]) -> dict:
        return self._request(
            "POST",
            f"/api/jobs/{job_id}/images/notify",
            {"filenames": filenames},
            timeout=_NOTIFY_TIMEOUT_SEC,
        )

    def notify_failed(self, job_id: str, filenames: list[str], attempts: int = 6) -> bool:
        """Tell the Worker an upload permanently failed (R2 PUT never
        succeeded). Retries until acked because a missing upload_failed signal
        would leave the index as a permanent un-uploaded hole that blocks the
        analysis frontier. Returns True on ack, False if all attempts failed
        (the Worker's /complete reconciliation + cron vanished-client backstop
        are the final safety nets). 4xx (other than transient) is treated as a
        terminal non-retry."""
        for attempt in range(1, attempts + 1):
            try:
                self._request(
                    "POST",
                    f"/api/jobs/{job_id}/images/failed",
                    {"filenames": filenames},
                    timeout=_NOTIFY_TIMEOUT_SEC,
                )
                return True
            except CloudComputeNetworkError:
                pass  # transport failure — retry
            except CloudComputeError as e:
                # 5xx is transient; 4xx (e.g. job not found) won't fix itself.
                if e.status and e.status < 500:
                    return False
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 10))
        return False

    def mark_complete(
        self,
        job_id: str,
        succeeded: Optional[list[str]] = None,
        failed: Optional[list[str]] = None,
    ) -> dict:
        """Signal all uploads are done. ``succeeded`` are filenames whose R2
        PUT landed but whose per-file /notify did not confirm (so the Worker
        reconciles them upload_pending -> uploaded); ``failed`` are filenames
        whose upload permanently failed. The Worker also sweeps any remaining
        upload_pending rows to upload_failed so no hole survives."""
        body: dict = {}
        if succeeded:
            body["succeeded"] = succeeded
        if failed:
            body["failed"] = failed
        return self._request("POST", f"/api/jobs/{job_id}/complete", body)

    # The cloud-compute pause feature was removed entirely — there are no
    # /pause or /resume endpoints and the desktop no longer holds an upload
    # pause. Cancel is the only client-driven job control.

    def cancel_job_remote(self, job_id: str, *, origin: str = "user") -> dict:
        """POST /api/jobs/{jobId}/cancel — terminal cancellation. Worker marks
        the job ``cancelled``, sets ``stop_requested = 1`` so the Modal fetcher
        exits on its next poll, and async-deletes staging objects for this job.
        Results bucket is left intact so the client can still pull whatever
        finished before the cancel landed.

        ``origin`` distinguishes user-initiated cancellation from the desktop
        bootstrap orphan reaper (pass ``"orphan"`` for the latter). Worker
        records this in the audit log so the dashboard can show "the desktop
        crashed mid-upload" vs "the user clicked Cancel" without ambiguity."""
        path = f"/api/jobs/{job_id}/cancel"
        if origin == "orphan":
            path += "?origin=orphan"
        return self._request("POST", path, {})

    def request_upload_test_urls(
        self,
        count: int,
        sizes: list[int] | None = None,
    ) -> dict:
        """POST /api/upload-test — returns a batch of short-lived presigned PUT
        URLs scoped to a per-user prefix in the staging bucket. ``sizes`` is an
        optional list of per-file Content-Length hints; the Worker returns
        413 / ``file_too_large`` if any size exceeds the 200 MB cap."""
        body: dict = {"count": int(count)}
        if sizes is not None:
            body["sizes"] = list(sizes)
        return self._request("POST", "/api/upload-test", body)

    def get_usage(self, period: str = "monthly") -> dict:
        """GET /api/usage — Stage 5D. Returns the caller's aggregate cloud
        activity (totalJobs, totalImagesAnalyzed, byTerminalReason). Pass
        ``period='all'`` for lifetime totals; default is current UTC month.

        ``remainingImages`` stays ``None`` until quota enforcement is wired."""
        path = "/api/usage" if period == "monthly" else f"/api/usage?period={period}"
        return self._request("GET", path)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        from_iso: str | None = None,
        to_iso: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict:
        """GET /api/jobs — Stage 5C. Paginated list of the caller's jobs with
        live counters baked in (uploaded/dispatched/downloaded/analyzed). For
        the dashboard's "my jobs" tab. ``status`` accepts a csv (`'running'`
        is shorthand for `'uploading,processing'`)."""
        params: list[str] = []
        if status:    params.append(f"status={urllib.parse.quote(status)}")
        if from_iso:  params.append(f"from={urllib.parse.quote(from_iso)}")
        if to_iso:    params.append(f"to={urllib.parse.quote(to_iso)}")
        if limit:     params.append(f"limit={int(limit)}")
        if cursor:    params.append(f"cursor={urllib.parse.quote(cursor)}")
        suffix = ("?" + "&".join(params)) if params else ""
        return self._request("GET", f"/api/jobs{suffix}")

    def get_job_events(self, job_id: str, *, order: str = "desc") -> dict:
        """GET /api/jobs/:jobId/events — Stage 5C. Full audit timeline for one
        job. ``order='asc'`` for chronological replay; default `'desc'`
        (newest first) for the dashboard's "recent activity" view."""
        suffix = "?order=asc" if order == "asc" else ""
        return self._request("GET", f"/api/jobs/{job_id}/events{suffix}")

    def get_job_timing_stats(self, job_id: str) -> dict:
        """GET /api/jobs/:jobId/timing-stats — Stage 5C. Derived throughput +
        latency aggregates (p50/p95) from job_images timestamps. Returns null
        fields when not enough samples exist (e.g. analyze stats mid-upload)."""
        return self._request("GET", f"/api/jobs/{job_id}/timing-stats")

    def list_results(self, job_id: str) -> list[dict]:
        body = self._request("GET", f"/api/jobs/{job_id}/results")
        return list(body.get("files", []))

    def delete_packs(self, job_id: str, pack_names: list[str]) -> dict:
        """Tell the Worker to delete a set of result packs from R2 RESULTS_BUCKET.

        Called after the desktop has confirmed each pack is merged into the
        local kestrel database. Bounded R2 storage: a job's results live in
        the bucket only as long as some pack hasn't yet been merged on the
        client. Best-effort — exceptions are caller's problem; on failure
        the pack stays in R2 and the next bootstrap reconciliation will
        retry.
        """
        if not pack_names:
            return {"deleted": 0, "failed": 0}
        # Worker caps batch size at 200 — chunk if the desktop ever needs more
        # (today the typical job has <50 packs, so this is future-proofing).
        deleted = 0
        failed = 0
        for i in range(0, len(pack_names), 200):
            chunk = pack_names[i:i + 200]
            body = self._request(
                "POST",
                f"/api/jobs/{job_id}/results/delete",
                {"packs": chunk},
            )
            deleted += int(body.get("deleted", 0))
            failed += int(body.get("failed", 0))
        return {"deleted": deleted, "failed": failed}

    def download_pack(self, job_id: str, filename: str, dest: Path) -> Path:
        """Stream a result-pack zip from the Worker (NOT direct R2).

        Verifies the pack against the X-Pack-SHA256 header (set by Worker
        from job_packs.pack_sha256, which Modal reports at upload time). The
        header is absent on legacy packs and on segment packs; verification
        is skipped silently in that case.
        """
        # Path-traversal guard at the sink (defends every caller): the Worker
        # supplies ``filename``, so refuse anything that isn't a bare basename
        # and ensure ``dest`` actually lands on that name (no escaping pack_dir).
        safe = _safe_pack_filename(filename)
        if safe is None:
            raise CloudComputeError(0, f"Refusing unsafe pack filename: {filename!r}")
        filename = safe
        dest = Path(dest)
        if dest.name != filename:
            raise CloudComputeError(
                0, f"Pack dest {dest.name!r} does not match filename {filename!r}"
            )
        url = f"{self.api_base}/api/jobs/{job_id}/results/{filename}"
        # Same 401 refresh-and-retry contract as _request: a session that
        # expires mid-download (laptop sleep) self-heals instead of failing.
        for attempt in range(self._MAX_AUTH_RETRIES + 1):
            headers = {
                "User-Agent": "KestrelDesktop/CloudCompute/1.0",
                **self._auth_headers,
            }
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as resp:
                    expected_sha = resp.headers.get("X-Pack-SHA256") or ""
                    data = resp.read()
                    if expected_sha:
                        actual_sha = hashlib.sha256(data).hexdigest()
                        if not hmac.compare_digest(
                            actual_sha.lower(), expected_sha.strip().lower()
                        ):
                            raise CloudComputeError(
                                0,
                                f"Pack integrity check failed for {filename}: "
                                f"expected sha256={expected_sha}, got {actual_sha}",
                            )
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    return dest
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < self._MAX_AUTH_RETRIES and self._refresh_token():
                    continue
                text = e.read().decode("utf-8", errors="replace")
                if e.code == 401:
                    raise CloudComputeAuthError("Session expired") from e
                raise CloudComputeError(e.code, text) from e
        raise CloudComputeAuthError("Session expired")

    # ─── Direct-to-R2 upload ─────────────────────────────────────────────

    @staticmethod
    def _put_file(url: str, file_path: Path, attempts: int = 3) -> int:
        """PUT a single file to its presigned R2 URL. Returns the HTTP status
        (or 0 on a transport failure that never produced a response).

        Retries transient failures (network errors, 5xx) up to ``attempts``
        times with exponential backoff. 4xx responses are NOT retried — a
        client error (e.g. an expired presign) won't fix itself. The caller
        treats any final status >= 400 (or 0) as a permanent upload failure and
        notifies the Worker via /images/failed so the index can't wedge the
        analysis frontier."""
        data = file_path.read_bytes()
        last_status = 0
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(
                url,
                data=data,
                method="PUT",
                headers={"Content-Type": "application/octet-stream"},
            )
            try:
                with urllib.request.urlopen(req, timeout=_PUT_TIMEOUT_SEC, context=_ssl_context()) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                last_status = e.code
                if e.code < 500:
                    return e.code  # client error — retrying won't help
            except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout):
                last_status = 0  # transport failure, no HTTP status
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
        return last_status

    # ─── Upload speed test ───────────────────────────────────────────────

    def upload_test(
        self,
        folder: Path,
        sample_count: int = 10,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """Measure real upload throughput against the staging bucket.

        Discovers the first ``sample_count`` images in ``folder`` (RAW-priority,
        any supported format — see ``_discover_upload_images``),
        requests presigned PUT URLs from ``/api/upload-test`` (scoped to a
        short-lived per-user prefix that the bucket's lifecycle policy
        auto-purges — these files are NOT analyzed and do NOT count against
        usage), uploads them with the same concurrency as a real job, and
        returns aggregate stats.

        If the folder has fewer than ``sample_count`` images, files are
        re-used in round-robin to fill out the request so the user still
        gets a meaningful measurement.

        ``on_progress(idx, total)`` is fired after each upload completes (one
        call per finished slot), letting the dialog show ``Running speed
        test... N/10``.

        Returns ``{mbps, samples_uploaded, total_bytes, elapsed_ms,
        bytes_per_sample, errors, pipeline}``. Raises :class:`CloudComputeError`
        if the Worker rejects the request (e.g. a 200 MB file size cap is hit).

        ``pipeline`` is the Worker's dispatch/scale-out descriptor (thresholds,
        container cap, cold start, per-container throughput) used by the
        analyze dialog's job-time estimate. It is passed through verbatim and
        is ``None`` against a Worker too old to serve it — the estimate falls
        back to its own built-in values, so this is advisory, never fatal.
        """
        folder = Path(folder).resolve()
        if not folder.is_dir():
            raise ValueError(f"folder not a directory: {folder}")
        if sample_count < 1:
            raise ValueError("sample_count must be >= 1")
        sample_count = min(sample_count, 10)  # Worker caps at 10 slots

        all_images = _discover_upload_images(folder)
        if not all_images:
            raise ValueError(f"no images found in {folder}")

        # Round-robin fill if the folder is smaller than the requested sample.
        chosen: list[Path] = []
        i = 0
        while len(chosen) < sample_count:
            chosen.append(all_images[i % len(all_images)])
            i += 1

        sizes = [p.stat().st_size for p in chosen]
        biggest = max(sizes)
        UPLOAD_TEST_MAX_BYTES = 200 * 1024 * 1024
        if biggest > UPLOAD_TEST_MAX_BYTES:
            # Surface the user-friendly error early instead of waiting for the
            # Worker to 413. Matches the Worker's `file_too_large` semantics.
            raise CloudComputeError(
                413,
                json.dumps({
                    "error": "file_too_large",
                    "maxBytes": UPLOAD_TEST_MAX_BYTES,
                    "biggestFile": chosen[sizes.index(biggest)].name,
                }),
            )

        resp = self.request_upload_test_urls(count=sample_count, sizes=sizes)
        slots = list(resp.get("presignedUrls") or [])
        if len(slots) < sample_count:
            raise CloudComputeError(
                500,
                f"Worker returned {len(slots)} slots for {sample_count}-image request",
            )

        results: list[tuple[int, int, float]] = []  # (status, bytes, elapsed_s)
        results_lock = threading.Lock()
        errors: list[str] = []

        def _upload_one(idx: int, slot: dict, path: Path) -> None:
            url = slot["url"]
            data = path.read_bytes()
            t0 = time.perf_counter()
            req = urllib.request.Request(
                url, data=data, method="PUT",
                headers={"Content-Type": "application/octet-stream"},
            )
            try:
                with urllib.request.urlopen(req, timeout=_PUT_TIMEOUT_SEC, context=_ssl_context()) as r:
                    status = int(r.status)
            except urllib.error.HTTPError as e:
                status = int(e.code)
                with results_lock:
                    errors.append(f"slot {idx}: HTTP {status}")
            elapsed = time.perf_counter() - t0
            with results_lock:
                results.append((status, len(data), elapsed))
                if on_progress is not None:
                    try:
                        on_progress(len(results), sample_count)
                    except Exception:
                        pass

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_UPLOAD_WORKERS) as pool:
            futures = [
                pool.submit(_upload_one, i, slots[i], chosen[i])
                for i in range(sample_count)
            ]
            for f in concurrent.futures.as_completed(futures):
                f.result()
        elapsed_total = time.perf_counter() - t_start

        ok_results = [r for r in results if 200 <= r[0] < 300]
        total_bytes = sum(r[1] for r in ok_results)
        mbps = (total_bytes / 1_048_576) / elapsed_total if elapsed_total > 0 else 0.0
        pipeline = resp.get("pipeline")
        return {
            "mbps": mbps,
            "samples_uploaded": len(ok_results),
            "samples_attempted": sample_count,
            "total_bytes": total_bytes,
            "elapsed_ms": int(elapsed_total * 1000),
            "bytes_per_sample": sizes,
            "errors": errors,
            "pipeline": pipeline if isinstance(pipeline, dict) else None,
        }

    # ─── End-to-end orchestrator ─────────────────────────────────────────

    def run_full_job(
        self,
        images_dir: Path,
        file_paths: list[Path] | None = None,
        analysis_settings: dict | None = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        on_pack_merged: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        merge_into_kestrel: bool = True,
        protected_filenames: Optional[set[str]] = None,
        overwrite_errors: bool = False,
        job_id: Optional[str] = None,
        presigned_urls: Optional[list[dict]] = None,
    ) -> dict:
        """End-to-end job: submit → upload → notify → complete → poll → download → merge.

        When ``job_id`` and ``presigned_urls`` are provided the caller has
        already submitted the job; this method skips the internal submit so
        only one job is created on the Worker. Pass them from api_bridge after
        calling ``submit_job`` so the poller and the upload thread watch the
        same job.

        Returns a dict summarizing the final job state plus pack paths.
        Raises CloudComputeError on Worker failures, ValueError on input issues.
        """
        images_dir = Path(images_dir).resolve()
        if not images_dir.is_dir():
            raise ValueError(f"images_dir not a directory: {images_dir}")

        files = file_paths or _discover_upload_images(images_dir)
        if not files:
            raise ValueError(f"no images found in {images_dir}")

        def _emit(event: str, **payload: Any) -> None:
            if on_progress is None:
                return
            try:
                on_progress({"event": event, **payload})
            except Exception:
                pass

        def _check_cancel() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise JobCancelled("Job cancelled by client")

        # 1) Submit — skip when the caller pre-submitted and passed job_id +
        # presigned_urls so we don't create a second Worker job.
        if job_id and presigned_urls is not None:
            presigned: list[dict] = list(presigned_urls)
            _emit("submitted", jobId=job_id)
        else:
            _emit("submit", imageCount=len(files))
            _submit = self.submit_job(files, analysis_settings=analysis_settings)
            job_id = str(_submit["jobId"])
            presigned = list(_submit.get("presignedUrls", []))
            _emit("submitted", jobId=job_id)
        if len(presigned) != len(files):
            raise CloudComputeError(
                500, f"Expected {len(files)} presigned URLs, got {len(presigned)}"
            )

        # 2) Spawn the pack-download poller BEFORE uploads start. Modal
        # dispatches at BASE_DISPATCH_THRESHOLD (=50) — packs can land in R2
        # while uploads are still streaming, and serial-then-poll would leave
        # them sitting unfetched until the upload pool drains. The poller and
        # upload pool now run concurrently; the poller exits when the Worker
        # reports a terminal status.
        pack_dir = images_dir / ".kestrel" / "cloud-packs"
        pack_dir.mkdir(parents=True, exist_ok=True)
        poller_downloaded: set[str] = set()
        poller_lock = threading.Lock()
        poller_done = threading.Event()
        poller_state: dict = {"final": None, "analyzed": 0, "exception": None}

        # Fix 3: bound how long a wedged poller keeps the foreground hung. If
        # get_status keeps failing — a permanently-revoked session that the
        # token refresh can't fix, or the network is down for good — the
        # foreground's unconditional poller_thread.join() would otherwise wait
        # forever and the job would never reach its terminal mapping (the cloud
        # queue never advances). After this many CONSECUTIVE get_status failures
        # we give up and surface the last error via poller_state['exception'] so
        # run_full_job resolves (the api_bridge maps an auth error to a kept
        # non-terminal state; any other error → 'failed'). A single success
        # resets the budget, so a flaky network just slows polling, it doesn't
        # kill the job. ~15 failures * 5s poll ≈ 75s of grace.
        _MAX_CONSECUTIVE_POLL_FAILURES = 15

        def _poll_loop() -> None:
            consecutive_failures = 0
            try:
                while not poller_done.is_set():
                    try:
                        status_body = self.get_status(job_id)
                    except CloudComputeError as e:
                        # CloudComputeAuthError (401) lands here too — it is a
                        # subclass — so an expired session never escapes as a
                        # fatal poller exception; it's just another transient.
                        consecutive_failures += 1
                        _emit("status_failed", error=str(e))
                        if consecutive_failures >= _MAX_CONSECUTIVE_POLL_FAILURES:
                            # Budget exhausted — stop hanging the foreground.
                            poller_state["exception"] = e
                            return
                        if poller_done.wait(timeout=_POLL_INTERVAL_SEC):
                            return
                        continue
                    consecutive_failures = 0  # a success clears the budget
                    cur_status = str(status_body.get("status", ""))
                    analyzed = int(status_body.get("analyzedCount") or 0)
                    poller_state["analyzed"] = analyzed
                    _emit(
                        "status",
                        jobStatus=cur_status,
                        analyzedCount=analyzed,
                        imageCount=int(status_body.get("image_count") or len(files)),
                    )

                    # Remote terminal-for-UPLOAD status. cancel_event STOPS THE
                    # UPLOAD POOL ONLY — the download drain below keeps running so
                    # we still fetch results that exist (and, for 'incomplete', are
                    # still being produced server-side). We do NOT return here:
                    #   cancelled → server halted everything; drain once, then the
                    #     loop-exit below stops us.
                    #   incomplete → uploads halted but analysis CONTINUES; keep
                    #     draining until the last container exits (handled below).
                    #   failed → no recoverable packs beyond what exists; drain, stop.
                    if cur_status in ("cancelled", "incomplete", "failed"):
                        if cancel_event is not None:
                            cancel_event.set()

                    try:
                        files_meta = self.list_results(job_id)
                    except CloudComputeError as e:
                        _emit("list_failed", error=str(e))
                        files_meta = []

                    for meta in files_meta:
                        fname = str(meta.get("filename") or "")
                        if not fname.endswith(".zip"):
                            continue
                        safe = _safe_pack_filename(fname)
                        if safe is None:
                            _emit("pack_rejected", filename=fname, reason="unsafe_filename")
                            continue
                        fname = safe
                        # Dedup under the lock; the actual download + merge
                        # runs OUTSIDE so concurrent merges of different packs
                        # don't serialise.
                        with poller_lock:
                            if fname in poller_downloaded:
                                continue
                            poller_downloaded.add(fname)
                        dest = pack_dir / fname
                        try:
                            self.download_pack(job_id, fname, dest)
                        except CloudComputeError as e:
                            _emit("pack_download_failed", filename=fname, error=str(e))
                            with poller_lock:
                                poller_downloaded.discard(fname)
                            continue
                        _emit("pack_downloaded", filename=fname, packs=len(poller_downloaded))
                        if merge_into_kestrel:
                            try:
                                merge_pack_into_kestrel(
                                    dest, images_dir,
                                    protected_filenames=protected_filenames,
                                    overwrite_errors=overwrite_errors,
                                )
                                _emit("pack_merged", filename=fname)
                                if on_pack_merged is not None:
                                    try:
                                        on_pack_merged(fname)
                                    except Exception:
                                        pass
                            except Exception as e:
                                _emit("pack_merge_failed", filename=fname, error=str(e))

                    # Loop-exit AFTER draining this tick's packs:
                    #   complete / cancelled / failed → fully terminal, no more
                    #     results will be produced; stop now.
                    #   incomplete → stop only once the last container has drained
                    #     (active_container_count==0). The cron reaps stale
                    #     containers, so this always converges; until then keep
                    #     polling so newly-produced packs are fetched and the
                    #     analysis bar keeps advancing.
                    if cur_status in ("complete", "cancelled", "failed"):
                        poller_state["final"] = status_body
                        return
                    if cur_status == "incomplete" and int(status_body.get("active_container_count") or 0) == 0:
                        poller_state["final"] = status_body
                        return
                    if poller_done.wait(timeout=_POLL_INTERVAL_SEC):
                        return
            except Exception as e:
                # Capture but don't raise — the foreground thread joins on
                # poller_done and inspects poller_state["exception"].
                poller_state["exception"] = e
            finally:
                poller_done.set()

        poller_thread = threading.Thread(
            target=_poll_loop, name=f"cc-pack-poller-{job_id}", daemon=True,
        )
        poller_thread.start()

        # 3) Concurrent uploads + per-file notify (runs alongside the poller)
        notified_lock = threading.Lock()
        notified_count = 0
        failed_uploads: list[str] = []        # R2 PUT failed (after retries)
        notify_unconfirmed: list[str] = []    # PUT ok but /notify didn't land

        def _upload_and_notify(item: dict, file_path: Path) -> None:
            nonlocal notified_count
            _check_cancel()
            status = self._put_file(item["url"], file_path)
            if status >= 400 or status == 0:
                with notified_lock:
                    failed_uploads.append(file_path.name)
                _emit("upload_failed", filename=file_path.name, status=status)
                # Authoritatively tell the Worker the upload failed (retry until
                # acked). Without this, the never-arriving image would block the
                # analysis frontier until the cron's vanished-client backstop.
                if not self.notify_failed(job_id, [file_path.name]):
                    _emit("notify_failed_unacked", filename=file_path.name)
                return
            try:
                self.notify_uploaded(job_id, [file_path.name])
            except CloudComputeError as e:
                # PUT succeeded but /notify didn't land. Don't abort the job —
                # record it so mark_complete reconciles this straggler to
                # 'uploaded' (closes the silent-stranding bug).
                with notified_lock:
                    notify_unconfirmed.append(file_path.name)
                _emit("notify_failed", filename=file_path.name, error=str(e))
                return
            with notified_lock:
                notified_count += 1
                _emit(
                    "uploaded",
                    filename=file_path.name,
                    notified=notified_count,
                    total=len(files),
                )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_UPLOAD_WORKERS) as pool:
                futures = [
                    pool.submit(_upload_and_notify, item, fp)
                    for item, fp in zip(presigned, files)
                ]
                for fut in concurrent.futures.as_completed(futures):
                    _check_cancel()
                    fut.result()  # propagate exceptions

            # 4) Mark uploads complete. Pass the reconciliation lists so the
            # Worker can confirm PUT-ok-but-unnotified stragglers as 'uploaded'
            # and finalize failed uploads; it also sweeps any remaining
            # upload_pending to upload_failed so no hole survives.
            _emit("uploads_done", failed=len(failed_uploads))
            self.mark_complete(
                job_id,
                succeeded=list(notify_unconfirmed),
                failed=list(failed_uploads),
            )
        except JobCancelled:
            # Uploads were stopped because the job went terminal-for-upload
            # (local cancel OR a remote cancelled/incomplete observed by the
            # poller, which sets cancel_event). Do NOT kill the poller — it must
            # keep draining the remaining/late result packs and observe the
            # terminal status. Skip mark_complete (the job is already terminal)
            # and fall through to the join below.
            _emit("uploads_stopped", reason="cancelled_or_incomplete")
        except Exception:
            # A genuine upload error: make sure the poller exits before we
            # re-raise — otherwise we'd leak the thread.
            poller_done.set()
            poller_thread.join(timeout=10.0)
            raise

        # 5) Wait for the poller to observe a terminal status. It picks up any
        # stragglers (the final pack(s) Modal produces after `mark_complete`),
        # and for 'incomplete' keeps draining until the last container exits.
        poller_thread.join()
        if poller_state["exception"] is not None:
            raise poller_state["exception"]

        final = poller_state["final"] or {}
        cur_status = str(final.get("status", ""))
        analyzed = int(final.get("analyzedCount") or poller_state["analyzed"])

        return {
            "ok": cur_status == "complete",
            "jobId": job_id,
            "status": cur_status,
            "analyzedCount": analyzed,
            "uploadFailures": failed_uploads,
            "packsDownloaded": sorted(poller_downloaded),
            "packDir": str(pack_dir),
        }


# ─── Pack merge ──────────────────────────────────────────────────────────
#
# Mirror of upload_test.py's merge_pack_into_kestrel(). Kept verbatim in
# behavior so a desktop-driven job produces the same on-disk shape as a
# CLI-driven one.

def merge_pack_into_kestrel(
    pack_path: Path,
    target_root: Path,
    protected_filenames: Optional[set[str]] = None,
    overwrite_errors: bool = False,
) -> None:
    """Unzip a result pack into target_root/.kestrel.

    - copy .kestrel/crop/* into target .kestrel/crop/
    - copy .kestrel/export/* into target .kestrel/export/
    - append+dedupe .kestrel/kestrel_database.csv by filename (latest row wins)
    - overwrite .kestrel/{kestrel_metadata.json, kestrel_scenedata.json}

    ``protected_filenames`` is the set of filenames whose existing CSV row
    should NOT be overwritten by an incoming row. Used for the scene-merger
    anchor file: the desktop re-uploads it so the cloud pipeline has a real
    `previous_image` for scene-grouping continuity, but the local row is
    already authoritative — replacing it with cloud-derived data (potentially
    different settings) corrupts the database.

    ``overwrite_errors`` enables the retry-errored path: a local row whose
    ``species == "Error"`` will be replaced when the incoming cloud row has
    a real classification (``species != "Error"``). Protected filenames
    still take priority — a row in both ``protected_filenames`` and the
    errored set is kept unchanged.
    """
    protected = {str(p).strip() for p in (protected_filenames or set()) if str(p).strip()}
    target_kestrel = target_root / ".kestrel"
    target_crop = target_kestrel / "crop"
    target_export = target_kestrel / "export"
    target_kestrel.mkdir(parents=True, exist_ok=True)
    target_crop.mkdir(parents=True, exist_ok=True)
    target_export.mkdir(parents=True, exist_ok=True)

    def _is_protected_artifact(name: str) -> bool:
        # Match the local pipeline's naming: <stem>_export.jpg / <stem>_crop_*.jpg.
        # If any protected filename's stem matches the artifact's stem, skip.
        if not protected:
            return False
        for pf in protected:
            stem = Path(pf).stem
            if not stem:
                continue
            if name == f"{stem}_export.jpg":
                return True
            if name.startswith(f"{stem}_crop_") and name.endswith(".jpg"):
                return True
        return False

    with tempfile.TemporaryDirectory(prefix="kestrel-cc-pack-") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(pack_path, "r") as zf:
            # Zip-Slip guard: refuse any member whose resolved target would
            # escape tmp_path. Worker/Modal should never emit such entries,
            # but the desktop should not trust the server side here.
            extract_root = tmp_path.resolve()
            for info in zf.infolist():
                target = (extract_root / info.filename).resolve()
                if target != extract_root and not str(target).startswith(
                    str(extract_root) + os.sep
                ):
                    raise RuntimeError(
                        f"Zip member escapes extraction root: {info.filename!r}"
                    )
            zf.extractall(tmp_path)

        src_kestrel = tmp_path / ".kestrel"
        if not src_kestrel.is_dir():
            # Some pack layouts place files at archive root.
            src_kestrel = tmp_path

        # Copy crops + exports. Stage 4C: skip if a destination file already
        # exists — local artifacts are authoritative. This generalises the
        # protected_filenames check (which only covered anchor files) to all
        # local artifacts, matching the CSV append-only semantics from 4A.
        # protected_filenames is still consulted as an early-skip for the
        # anchor case (same name + present locally → skipped twice, harmless).
        for sub in ("crop", "export"):
            src = src_kestrel / sub
            if not src.is_dir():
                continue
            dst = target_kestrel / sub
            for entry in src.iterdir():
                if not entry.is_file():
                    continue
                if _is_protected_artifact(entry.name):
                    continue
                dst_path = dst / entry.name
                if dst_path.exists():
                    # Don't clobber a local artifact — keep what the user has.
                    continue
                shutil.copy2(entry, dst_path)

        # CSV merge — last-write-wins on filename column, EXCEPT protected
        # filenames where the existing row (if any) is preserved.
        src_csv = src_kestrel / "kestrel_database.csv"
        if src_csv.is_file():
            target_csv = target_kestrel / "kestrel_database.csv"
            _merge_database_csv(
                src_csv, target_csv,
                protected=protected,
                overwrite_errors=overwrite_errors,
            )

        # Scenedata: split into two concerns.
        #
        # `image_ratings` IS trustworthy in the pack (filename-keyed, no
        # scene_id collision risk). Merge additively — local entries always
        # win; pack ratings fill in for files not yet rated locally. Same
        # semantics as the old _merge_scenedata_additive flow.
        #
        # `scenes` from the pack uses container-local scene_ids that collide
        # across containers (scene "1" from container A and container B
        # describe different content). Don't trust it — rebuild from the
        # scene_count-corrected CSV. User edits (name/status/user_tags) are
        # preserved by snapshot-restore through the rebuild.
        src_scene = src_kestrel / "kestrel_scenedata.json"
        target_csv = target_kestrel / "kestrel_database.csv"
        if src_scene.is_file() or target_csv.is_file():
            try:
                _rebuild_scenedata_from_csv(
                    target_kestrel,
                    pack_scenedata_path=src_scene if src_scene.is_file() else None,
                )
            except Exception as e:
                # Best-effort — CSV is the rendering source of truth. Stale
                # scenedata is harmless for display; it only matters for
                # scene merge/split ops.
                import sys as _sys
                print(f"[cloud-compute] scenedata rebuild failed for {target_kestrel}: {e}", file=_sys.stderr)

        # Metadata: full replacement is safe — file contains no user data
        # (analysis_settings, version stamps, quality histogram).
        src_metadata = src_kestrel / "kestrel_metadata.json"
        if src_metadata.is_file():
            shutil.copy2(src_metadata, target_kestrel / "kestrel_metadata.json")


def _write_file_atomic(
    path: Path,
    write: Callable[[Any], None],
    *,
    newline: Optional[str] = None,
) -> None:
    """Write ``path`` via tempfile + fsync + ``os.replace``.

    Mirrors ``kestrel_analyzer.database._to_csv_atomic`` and
    ``settings_utils.save_settings``: the destination is never opened with
    ``"w"``, so a crash or ENOSPC cannot truncate a live ``.kestrel`` file.
    ``write`` receives the open temp file. Failures propagate; the previous
    destination is left intact. ``os.replace`` goes through
    ``retry_on_file_lock`` so a Windows UI reader holding the CSV does not
    abort the pack merge.
    """
    directory = str(path.parent)
    os.makedirs(directory, exist_ok=True)
    tmp_fd, tmp = tempfile.mkstemp(
        prefix=".kestrel_merge_",
        suffix=".tmp",
        dir=directory,
    )
    try:
        open_kw: dict[str, Any] = {"encoding": "utf-8"}
        if newline is not None:
            open_kw["newline"] = newline
        with os.fdopen(tmp_fd, "w", **open_kw) as f:
            write(f)
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError:
                pass
        retry_on_file_lock(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _merge_database_csv(
    src: Path,
    dst: Path,
    protected: Optional[set[str]] = None,
    overwrite_errors: bool = False,
) -> None:
    """Merge ``src`` into ``dst``, deduping on the ``filename`` column.

    **Append-new-rows-only semantics (Stage 4A).** Any filename already
    present locally is preserved verbatim — the cloud row is dropped. This
    is stricter than the previous "protected_filenames only" gate: it
    protects every row from being clobbered by cloud-derived data, including
    user-editable columns (`culled`, `culled_origin`, etc.) that share the
    same CSV with analysis columns.

    ``overwrite_errors`` carves a single exception out of that rule: a local
    row whose ``species == "Error"`` (the marker the analyzer writes when a
    file fails) is replaced when the incoming cloud row has a real
    classification. If the cloud row is also ``"Error"``, the local row is
    kept (no churn). Rows in ``protected`` keep priority — they're scene
    anchors, never to be overwritten regardless of error state.

    New rows from the cloud pack are appended. Cloud-only columns that
    don't exist in the local CSV get added to the fieldnames list so the
    new rows can populate them.
    """
    protected_set = {str(p).strip() for p in (protected or set()) if str(p).strip()}
    rows: dict[str, dict[str, Any]] = {}
    fieldnames: list[str] = []
    existing_keys: set[str] = set()
    errored_keys: set[str] = set()
    cloud_sourced_keys: set[str] = set()  # rows whose data came from src this merge

    if dst.is_file():
        with dst.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            for row in reader:
                key = (row.get("filename") or "").strip()
                if key:
                    rows[key] = row
                    existing_keys.add(key)
                    if (row.get("species") or "").strip() == "Error":
                        errored_keys.add(key)

    with src.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        new_fields = list(reader.fieldnames or [])
        if not fieldnames:
            fieldnames = new_fields
        else:
            for fld in new_fields:
                if fld not in fieldnames:
                    fieldnames.append(fld)
        for row in reader:
            key = (row.get("filename") or "").strip()
            if not key:
                continue
            if key in existing_keys:
                # Retry-errored exception: if the local row is errored AND the
                # cloud row has a real classification, the cloud row wins.
                # Anchor protection takes priority over this — a protected
                # row is never overwritten.
                if (
                    overwrite_errors
                    and key in errored_keys
                    and key not in protected_set
                    and (row.get("species") or "").strip() != "Error"
                ):
                    rows[key] = row
                    cloud_sourced_keys.add(key)
                    continue
                # Local row wins — never overwrite. Protects user-editable
                # columns alongside analysis columns.
                continue
            rows[key] = row
            cloud_sourced_keys.add(key)

    # Renumber scene_count for cloud-sourced rows so they pick up from where
    # the prior CSV left off, instead of restarting at 1 per Modal container.
    # Each Modal segment-container starts its scene_count locally; without this
    # pass, "scene 1" ends up containing images from every container run (see
    # pack-merge bug diagnosis). Matches pipeline.py:856-857 — split a scene
    # iff `similar == False` against the prior file in sort order.
    sorted_keys = sorted(rows.keys())
    prev_scene_count = 0
    for key in sorted_keys:
        row = rows[key]
        if key in cloud_sourced_keys:
            is_similar = str(row.get("similar", "")).strip().lower() in ("true", "1")
            if not is_similar:
                prev_scene_count += 1
            row["scene_count"] = str(prev_scene_count)
        else:
            # Locally-authoritative row — trust its scene_count and let it
            # seed the running counter for any subsequent cloud row.
            try:
                prev_scene_count = int(float(str(row.get("scene_count") or "0")))
            except (TypeError, ValueError):
                pass

    def _write_csv(f) -> None:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for key in sorted_keys:
            writer.writerow(rows[key])

    _write_file_atomic(dst, _write_csv, newline="")


def _rebuild_scenedata_from_csv(
    target_kestrel: Path,
    pack_scenedata_path: Optional[Path] = None,
) -> None:
    """Rebuild ``<target_kestrel>/kestrel_scenedata.json`` after a pack merge.

    Same end-state as the local pipeline's post-analysis flow at
    pipeline.py:1477-1483, adapted for the cloud pack-merge:

    - ``image_ratings`` from the pack is merged additively (local entries
      always win; pack ratings fill in for files not yet rated locally).
      Runs regardless of whether a CSV exists.
    - When the merged CSV exists, ``scenes`` is rebuilt from the scene_count-
      corrected CSV (the column written by ``_merge_database_csv``). The
      pack's own ``scenes`` dict is ignored — its scene_ids collide across
      containers. User-edited scene fields (``name``, ``status``,
      ``user_tags``) are preserved by snapshot-restore through the rebuild.
    - When no CSV exists (synthetic / test cases), scenes from the pack are
      merged additively into local scenes — same legacy semantics as
      ``_merge_scenedata_additive``, since there's nothing to rebuild from.
    """
    try:
        try:
            from kestrel_analyzer.database import (
                load_database,
                load_scenedata,
                save_scenedata,
                build_scenedata_from_database,
            )
        except ImportError:
            from analyzer.kestrel_analyzer.database import (  # type: ignore[no-redef]
                load_database,
                load_scenedata,
                save_scenedata,
                build_scenedata_from_database,
            )
    except ImportError:
        return  # analyzer package not on path — nothing we can do

    existing = load_scenedata(str(target_kestrel))

    # ── image_ratings: additive merge from the pack (always) ─────────────
    cur_ratings = dict(existing.get("image_ratings") or {})
    pack_sd: dict = {}
    if pack_scenedata_path is not None:
        try:
            with pack_scenedata_path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                pack_sd = loaded
        except (OSError, ValueError):
            pack_sd = {}
        pack_ratings = pack_sd.get("image_ratings") or {}
        if isinstance(pack_ratings, dict):
            for fname, rating in pack_ratings.items():
                if fname not in cur_ratings:
                    cur_ratings[fname] = rating

    # ── scenes: rebuild from CSV if present; else additive from pack ─────
    csv_path = target_kestrel / "kestrel_database.csv"
    if csv_path.is_file():
        # Snapshot user-edited per-scene metadata so we can restore after
        # the CSV-driven rebuild replaces the scenes dict.
        user_edits: dict[str, dict] = {}
        for sid, scene in (existing.get("scenes") or {}).items():
            if not isinstance(scene, dict):
                continue
            edits: dict = {}
            name = scene.get("name") or ""
            if name:
                edits["name"] = name
            status = scene.get("status") or ""
            if status and status != "pending":
                edits["status"] = status
            user_tags = scene.get("user_tags")
            if isinstance(user_tags, dict) and (
                user_tags.get("species")
                or user_tags.get("families")
                or user_tags.get("finalized")
            ):
                edits["user_tags"] = user_tags
            if edits:
                user_edits[str(sid)] = edits

        database, _ = load_database(
            str(target_kestrel), analyzer_name="cloud-compute-merge"
        )
        rebuilt = build_scenedata_from_database(database)

        # Restore user-edited scene metadata. Only re-applies to scene_ids
        # that still exist after the rebuild (a renumbered scene loses its
        # edits — acceptable since scene_ids are stable when the cloud path
        # is operating correctly; for already-corrupted folders the user
        # edits were attached to wrong content anyway).
        for sid, edits in user_edits.items():
            scene = rebuilt["scenes"].get(sid)
            if isinstance(scene, dict):
                scene.update(edits)

        rebuilt["image_ratings"] = cur_ratings
        save_scenedata(rebuilt, str(target_kestrel))
        return

    # ── No CSV: legacy additive scenes merge (test/edge case only) ───────
    pack_scenes = pack_sd.get("scenes") or {}
    cur_scenes = existing.setdefault("scenes", {})
    if isinstance(pack_scenes, dict):
        for sid, pack_scene in pack_scenes.items():
            if not isinstance(pack_scene, dict):
                continue
            sid = str(sid)
            if sid not in cur_scenes or not isinstance(cur_scenes[sid], dict):
                cur_scenes[sid] = dict(pack_scene)
                continue
            local_scene = cur_scenes[sid]
            local_files = list(local_scene.get("image_filenames") or [])
            seen = set(local_files)
            for fname in (pack_scene.get("image_filenames") or []):
                if isinstance(fname, str) and fname and fname not in seen:
                    local_files.append(fname)
                    seen.add(fname)
            local_scene["image_filenames"] = local_files
            for f in ("name", "status", "user_tags"):
                if f not in local_scene and f in pack_scene:
                    local_scene[f] = pack_scene[f]
    existing["image_ratings"] = cur_ratings
    # version bump (lexicographic, matches old behavior)
    inc_ver = str(pack_sd.get("version") or "")
    cur_ver = str(existing.get("version") or "")
    existing["version"] = inc_ver if inc_ver > cur_ver else (cur_ver or inc_ver)
    save_scenedata(existing, str(target_kestrel))


def _merge_scenedata_additive(src: Path, dst: Path) -> None:
    """Merge ``src`` scenedata JSON into ``dst`` additively (Stage 4B).

    Mirrors the safety semantics of `database.update_scenedata_with_database`
    (line 265 in `kestrel_analyzer/database.py`) but operates JSON-to-JSON
    so the pack-merge path doesn't have to round-trip through a DataFrame:

    - `image_ratings`: existing entries are preserved; new entries from the
      pack are added. A user rating is never overwritten.
    - `scenes`: for each incoming scene_id:
        * If the scene exists locally, keep `name`, `status`, `user_tags`
          from the local copy; take the UNION of `image_filenames`.
        * If the scene is new, copy it wholesale.
    - `version`: take the higher of the two strings (lexicographic — both
      are dotted-decimal in practice; "2.0" < "2.0.1" < "2.1").

    When ``dst`` doesn't exist yet (fresh folder), the incoming file is
    written verbatim.
    """
    try:
        with src.open("r", encoding="utf-8") as f:
            incoming = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(incoming, dict):
        return

    if not dst.is_file():
        _write_file_atomic(dst, lambda f: json.dump(incoming, f, indent=2))
        return

    try:
        with dst.open("r", encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    # version: take the higher string (lexicographic ordering works for
    # the dotted-decimal scheme used in practice).
    inc_ver = str(incoming.get("version") or "")
    cur_ver = str(existing.get("version") or "")
    existing["version"] = inc_ver if inc_ver > cur_ver else (cur_ver or inc_ver)

    # image_ratings: existing entries win.
    inc_ratings = incoming.get("image_ratings") or {}
    cur_ratings = existing.setdefault("image_ratings", {})
    if isinstance(inc_ratings, dict):
        for fname, rating in inc_ratings.items():
            if fname not in cur_ratings:
                cur_ratings[fname] = rating

    # scenes: existing scene_ids keep user-editable fields; image_filenames
    # gets a stable de-duplicated union.
    inc_scenes = incoming.get("scenes") or {}
    cur_scenes = existing.setdefault("scenes", {})
    if isinstance(inc_scenes, dict):
        for sid, inc_scene in inc_scenes.items():
            if not isinstance(inc_scene, dict):
                continue
            if sid not in cur_scenes or not isinstance(cur_scenes[sid], dict):
                # New scene — copy wholesale.
                cur_scenes[sid] = dict(inc_scene)
                continue
            local_scene = cur_scenes[sid]
            # Union image_filenames preserving local order, appending any
            # new incoming filenames at the end.
            local_files = list(local_scene.get("image_filenames") or [])
            seen = set(local_files)
            for fname in (inc_scene.get("image_filenames") or []):
                if isinstance(fname, str) and fname and fname not in seen:
                    local_files.append(fname)
                    seen.add(fname)
            local_scene["image_filenames"] = local_files
            # Defensively keep local name/status/user_tags untouched. If a
            # local scene is missing these fields (legacy data), seed them
            # from the incoming scene so the schema stays consistent.
            for f in ("name", "status", "user_tags"):
                if f not in local_scene and f in inc_scene:
                    local_scene[f] = inc_scene[f]

    _write_file_atomic(dst, lambda f: json.dump(existing, f, indent=2))
