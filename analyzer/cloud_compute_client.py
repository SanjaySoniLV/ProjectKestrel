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

    def submit_job(self, file_paths: list[Path]) -> dict:
        """POST /api/jobs — returns {jobId, presignedUrls, ...}."""
        if not file_paths:
            raise ValueError("submit_job requires at least one file path")
        return self._request(
            "POST",
            "/api/jobs",
            {
                "imageCount": len(file_paths),
                "fileNames": [p.name for p in file_paths],
            },
        )

    def get_status(self, job_id: str) -> dict:
        return self._request("GET", f"/api/jobs/{job_id}")

    def notify_uploaded(self, job_id: str, filenames: list[str]) -> dict:
        return self._request(
            "POST", f"/api/jobs/{job_id}/images/notify", {"filenames": filenames}
        )

    def mark_complete(self, job_id: str) -> dict:
        return self._request("POST", f"/api/jobs/{job_id}/complete", {})

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

    # ─── End-to-end orchestrator ─────────────────────────────────────────

    def run_full_job(
        self,
        images_dir: Path,
        file_paths: list[Path] | None = None,
        on_progress: Optional[Callable[[dict], None]] = None,
        cancel_event: Optional[threading.Event] = None,
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
        submit = self.submit_job(files)
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
