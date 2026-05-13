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
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional


_DEFAULT_API_BASE = "https://cloudcompute.projectkestrel.org"
_MAX_UPLOAD_WORKERS = 6
_POLL_INTERVAL_SEC = 5

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
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            raise CloudComputeError(e.code, text) from e

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
        return self._request("POST", "/api/jobs", body)

    def get_status(self, job_id: str) -> dict:
        return self._request("GET", f"/api/jobs/{job_id}")

    def notify_uploaded(self, job_id: str, filenames: list[str]) -> dict:
        return self._request(
            "POST", f"/api/jobs/{job_id}/images/notify", {"filenames": filenames}
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

    def cancel_job_remote(self, job_id: str) -> dict:
        """POST /api/jobs/{jobId}/cancel — terminal cancellation. Worker marks
        the job ``cancelled``, sets ``stop_requested = 1`` so the Modal fetcher
        exits on its next poll, and async-deletes staging objects for this job.
        Results bucket is left intact so the client can still pull whatever
        finished before the cancel landed."""
        return self._request("POST", f"/api/jobs/{job_id}/cancel", {})

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

    def get_usage(self) -> dict:
        """GET /api/usage — currently a stub (Stage 3 fleshes this out into
        per-user image/credit metering). Returns ``{remainingImages: None,
        stub: True}`` today."""
        return self._request("GET", "/api/usage")

    def list_results(self, job_id: str) -> list[dict]:
        body = self._request("GET", f"/api/jobs/{job_id}/results")
        return list(body.get("files", []))

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
    ) -> dict:
        """End-to-end job: submit → upload → notify → complete → poll → download → merge.

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
                raise RuntimeError("Job cancelled by client")

        # 1) Submit
        _emit("submit", imageCount=len(files))
        submit = self.submit_job(files, analysis_settings=analysis_settings)
        job_id: str = str(submit["jobId"])
        presigned: list[dict] = list(submit.get("presignedUrls", []))
        if len(presigned) != len(files):
            raise CloudComputeError(
                500, f"Expected {len(files)} presigned URLs, got {len(presigned)}"
            )
        _emit("submitted", jobId=job_id)

        # 2) Concurrent uploads + per-file notify
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

        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_UPLOAD_WORKERS) as pool:
            futures = [
                pool.submit(_upload_and_notify, item, fp)
                for item, fp in zip(presigned, files)
            ]
            for fut in concurrent.futures.as_completed(futures):
                _check_cancel()
                fut.result()  # propagate exceptions

        # 3) Mark uploads complete (also dispatches stragglers on the Worker)
        _emit("uploads_done", failed=len(failed_uploads))
        self.mark_complete(job_id)

        # 4) Poll status, download new packs as they appear
        pack_dir = images_dir / ".kestrel" / "cloud-packs"
        pack_dir.mkdir(parents=True, exist_ok=True)
        downloaded: set[str] = set()

        while True:
            _check_cancel()
            status_body = self.get_status(job_id)
            cur_status = str(status_body.get("status", ""))
            analyzed = int(status_body.get("analyzedCount") or 0)
            _emit(
                "status",
                jobStatus=cur_status,
                analyzedCount=analyzed,
                imageCount=int(status_body.get("image_count") or len(files)),
            )

            try:
                files_meta = self.list_results(job_id)
            except CloudComputeError as e:
                # Don't fail the job over a transient list error — try again next tick.
                _emit("list_failed", error=str(e))
                files_meta = []

            for meta in files_meta:
                fname = str(meta.get("filename") or "")
                if not fname.endswith(".zip") or fname in downloaded:
                    continue
                _check_cancel()
                dest = pack_dir / fname
                try:
                    self.download_pack(job_id, fname, dest)
                except CloudComputeError as e:
                    _emit("pack_download_failed", filename=fname, error=str(e))
                    continue
                downloaded.add(fname)
                _emit("pack_downloaded", filename=fname, packs=len(downloaded))
                if merge_into_kestrel:
                    try:
                        merge_pack_into_kestrel(dest, images_dir)
                        _emit("pack_merged", filename=fname)
                        if on_pack_merged is not None:
                            try:
                                on_pack_merged(fname)
                            except Exception:
                                pass
                    except Exception as e:
                        _emit("pack_merge_failed", filename=fname, error=str(e))

            if cur_status in ("complete", "failed"):
                break
            time.sleep(_POLL_INTERVAL_SEC)

        return {
            "ok": cur_status == "complete",
            "jobId": job_id,
            "status": cur_status,
            "analyzedCount": analyzed,
            "uploadFailures": failed_uploads,
            "packsDownloaded": sorted(downloaded),
            "packDir": str(pack_dir),
        }


# ─── Pack merge ──────────────────────────────────────────────────────────
#
# Mirror of upload_test.py's merge_pack_into_kestrel(). Kept verbatim in
# behavior so a desktop-driven job produces the same on-disk shape as a
# CLI-driven one.

def merge_pack_into_kestrel(pack_path: Path, target_root: Path) -> None:
    """Unzip a result pack into target_root/.kestrel.

    - copy .kestrel/crop/* into target .kestrel/crop/
    - copy .kestrel/export/* into target .kestrel/export/
    - append+dedupe .kestrel/kestrel_database.csv by filename (latest row wins)
    - overwrite .kestrel/{kestrel_metadata.json, kestrel_scenedata.json}
    """
    target_kestrel = target_root / ".kestrel"
    target_crop = target_kestrel / "crop"
    target_export = target_kestrel / "export"
    target_kestrel.mkdir(parents=True, exist_ok=True)
    target_crop.mkdir(parents=True, exist_ok=True)
    target_export.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="kestrel-cc-pack-") as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(pack_path, "r") as zf:
            zf.extractall(tmp_path)

        src_kestrel = tmp_path / ".kestrel"
        if not src_kestrel.is_dir():
            # Some pack layouts place files at archive root.
            src_kestrel = tmp_path

        # Copy crops + exports (overwrite existing)
        for sub in ("crop", "export"):
            src = src_kestrel / sub
            if not src.is_dir():
                continue
            dst = target_kestrel / sub
            for entry in src.iterdir():
                if entry.is_file():
                    shutil.copy2(entry, dst / entry.name)

        # CSV merge — last-write-wins on filename column
        src_csv = src_kestrel / "kestrel_database.csv"
        if src_csv.is_file():
            target_csv = target_kestrel / "kestrel_database.csv"
            _merge_database_csv(src_csv, target_csv)

        # Metadata + scenedata are full replacements
        for fname in ("kestrel_metadata.json", "kestrel_scenedata.json"):
            src_meta = src_kestrel / fname
            if src_meta.is_file():
                shutil.copy2(src_meta, target_kestrel / fname)


def _merge_database_csv(src: Path, dst: Path) -> None:
    rows: dict[str, dict[str, Any]] = {}
    fieldnames: list[str] = []

    if dst.is_file():
        with dst.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            for row in reader:
                key = (row.get("filename") or "").strip()
                if key:
                    rows[key] = row

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
            if key:
                rows[key] = row  # last-wins

    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for key in sorted(rows.keys()):
            writer.writerow(rows[key])
