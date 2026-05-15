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


_DEFAULT_API_BASE = "https://cloudcompute.projectkestrel.org"
_MAX_UPLOAD_WORKERS = 6
_POLL_INTERVAL_SEC = 5
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


class CloudComputeError(RuntimeError):
    """Raised on non-2xx response from the cloud-compute Worker."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


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
    to MyAccount."""

    def __init__(self, active_job_id: str | None, message: str):
        super().__init__(403, message)
        self.active_job_id = active_job_id


class JobCancelled(RuntimeError):
    """Raised inside ``run_full_job`` when the supplied ``cancel_event`` fires.
    Distinct from generic exceptions so the caller can mark the job
    ``cancelled`` (not ``failed``) without inspecting the message string."""


class CloudComputeClient:
    """Stateless-ish wrapper around the cloud-compute Worker REST API.

    Reuses the auth-header construction pattern from
    `analyzer/perch_uploader.py:PerchKestrelUploader.__init__`.
    """

    def __init__(
        self,
        api_base: str,
        jwt_token: str | None,
        timeout: int = 120,
        dev_user: str | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
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

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        timeout: int | None = None,
    ) -> dict:
        url = f"{self.api_base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "KestrelDesktop/CloudCompute/1.0",
            **self._auth_headers,
        }
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
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
            text = e.read().decode("utf-8", errors="replace")
            raise CloudComputeError(e.code, text) from e
        except urllib.error.URLError as e:
            raise CloudComputeNetworkError(f"network error: {e.reason}") from e
        except socket.timeout as e:
            raise CloudComputeNetworkError(f"request timed out after {timeout or self.timeout}s") from e
        except (TimeoutError, ConnectionError) as e:
            raise CloudComputeNetworkError(f"transport error: {e}") from e

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
                    raise JobInProgressError(
                        parsed.get("activeJobId"),
                        str(parsed.get("message") or "You have a Cloud Compute job running."),
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

    def mark_complete(self, job_id: str) -> dict:
        return self._request("POST", f"/api/jobs/{job_id}/complete", {})

    def pause_job(self, job_id: str) -> dict:
        """POST /api/jobs/{jobId}/pause — upload-side pause. Modal keeps
        analyzing whatever is already in flight; only further client uploads
        are held. Idempotent."""
        return self._request("POST", f"/api/jobs/{job_id}/pause", {})

    def resume_job(self, job_id: str) -> dict:
        """POST /api/jobs/{jobId}/resume — clears the upload-side pause."""
        return self._request("POST", f"/api/jobs/{job_id}/resume", {})

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
        """Stream a result-pack zip from the Worker (NOT direct R2)."""
        url = f"{self.api_base}/api/jobs/{job_id}/results/{filename}"
        headers = {
            "User-Agent": "KestrelDesktop/CloudCompute/1.0",
            **self._auth_headers,
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(resp.read())
                return dest
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            raise CloudComputeError(e.code, text) from e

    # ─── Direct-to-R2 upload ─────────────────────────────────────────────

    @staticmethod
    def _put_file(url: str, file_path: Path) -> int:
        """PUT a single file to its presigned R2 URL. Returns HTTP status."""
        data = file_path.read_bytes()
        req = urllib.request.Request(
            url,
            data=data,
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    # ─── Upload speed test ───────────────────────────────────────────────

    def upload_test(
        self,
        folder: Path,
        sample_count: int = 10,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> dict:
        """Measure real upload throughput against the staging bucket.

        Discovers the first ``sample_count`` images in ``folder`` (CR3/JPEG),
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
        bytes_per_sample, errors}``. Raises :class:`CloudComputeError` if the
        Worker rejects the request (e.g. a 200 MB file size cap is hit).
        """
        folder = Path(folder).resolve()
        if not folder.is_dir():
            raise ValueError(f"folder not a directory: {folder}")
        if sample_count < 1:
            raise ValueError("sample_count must be >= 1")
        sample_count = min(sample_count, 10)  # Worker caps at 10 slots

        all_images = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in {".cr3", ".jpg", ".jpeg"}
        )
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
                with urllib.request.urlopen(req) as r:
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
        return {
            "mbps": mbps,
            "samples_uploaded": len(ok_results),
            "samples_attempted": sample_count,
            "total_bytes": total_bytes,
            "elapsed_ms": int(elapsed_total * 1000),
            "bytes_per_sample": sizes,
            "errors": errors,
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
        pause_event: Optional[threading.Event] = None,
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

        files = file_paths or sorted(
            p for p in images_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".cr3", ".jpg", ".jpeg"}
        )
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

        def _poll_loop() -> None:
            try:
                while not poller_done.is_set():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    try:
                        status_body = self.get_status(job_id)
                    except CloudComputeError as e:
                        _emit("status_failed", error=str(e))
                        if poller_done.wait(timeout=_POLL_INTERVAL_SEC):
                            return
                        continue
                    cur_status = str(status_body.get("status", ""))
                    analyzed = int(status_body.get("analyzedCount") or 0)
                    poller_state["analyzed"] = analyzed
                    _emit(
                        "status",
                        jobStatus=cur_status,
                        analyzedCount=analyzed,
                        imageCount=int(status_body.get("image_count") or len(files)),
                    )

                    try:
                        files_meta = self.list_results(job_id)
                    except CloudComputeError as e:
                        _emit("list_failed", error=str(e))
                        files_meta = []

                    for meta in files_meta:
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        fname = str(meta.get("filename") or "")
                        if not fname.endswith(".zip"):
                            continue
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

                    if cur_status in ("complete", "failed"):
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
        failed_uploads: list[str] = []

        def _upload_and_notify(item: dict, file_path: Path) -> None:
            nonlocal notified_count
            _check_cancel()
            # Honour pause-event: when uploads are paused via the Worker
            # endpoint, the api_bridge clears this event; we block (with a
            # short timeout so cancellation can still preempt) until resumed.
            if pause_event is not None:
                while not pause_event.is_set():
                    _check_cancel()
                    if pause_event.wait(timeout=1.0):
                        break
            status = self._put_file(item["url"], file_path)
            if status >= 400:
                with notified_lock:
                    failed_uploads.append(file_path.name)
                _emit("upload_failed", filename=file_path.name, status=status)
                return
            try:
                self.notify_uploaded(job_id, [file_path.name])
            except CloudComputeError as e:
                # /complete will catch stragglers — don't abort the whole job.
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

            # 4) Mark uploads complete (also dispatches stragglers on the Worker)
            _emit("uploads_done", failed=len(failed_uploads))
            self.mark_complete(job_id)
        except Exception:
            # If uploads failed or were cancelled, make sure the poller exits
            # before we re-raise — otherwise we'd leak the thread.
            poller_done.set()
            poller_thread.join(timeout=10.0)
            raise

        # 5) Wait for the poller to observe a terminal status. It picks up any
        # stragglers (the final pack(s) Modal produces after `mark_complete`).
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

        # Scenedata: additive merge (Stage 4B). Never overwrite existing
        # image_ratings, scene names, statuses, or user_tags. Add new
        # image-to-scene assignments and new scene records from the pack.
        src_scene = src_kestrel / "kestrel_scenedata.json"
        if src_scene.is_file():
            _merge_scenedata_additive(src_scene, target_kestrel / "kestrel_scenedata.json")

        # Metadata: full replacement is safe — file contains no user data
        # (analysis_settings, version stamps, quality histogram).
        src_metadata = src_kestrel / "kestrel_metadata.json"
        if src_metadata.is_file():
            shutil.copy2(src_metadata, target_kestrel / "kestrel_metadata.json")


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
                    continue
                # Local row wins — never overwrite. Protects user-editable
                # columns alongside analysis columns.
                continue
            rows[key] = row

    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for key in sorted(rows.keys()):
            writer.writerow(rows[key])


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
        with dst.open("w", encoding="utf-8") as f:
            json.dump(incoming, f, indent=2)
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

    with dst.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
