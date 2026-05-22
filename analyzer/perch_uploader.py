"""
Upload a Kestrel-analyzed session to Perch (Cloudflare Worker + R2 + D1).

The HTTP API is implemented in the **Perch Worker** repository (not in ProjectKestrel).
Call from the desktop app with a Clerk JWT. See `PerchKestrelUploader.run`.

For each CSV row, multipart asset POST includes species/quality and row-level **exposure**
fields (`exposure_correction`, `exposure_subject_stops`, `exposure_meter_scale`,
`exposure_pipeline`) plus the full **`crops_json`** array from the database when an export
is uploaded, or on the crop asset alone when no export file exists for that row (so
metadata is not lost). Only one crop **file** per row is uploaded (`crop_path`); other
crops may appear inside `crops_json` for future multi-crop support.

This uploader uses create perch, assets, manifest only. The worker also exposes social
and lifecycle routes for apps/UI (same ``Authorization: Bearer`` for mutations):

- ``GET /v1/me`` — current user's public profile blob.
- ``POST``/``DELETE /v1/assets/{assetId}/like``
- ``GET /v1/public/assets/{assetId}/likes`` and ``.../comments`` (optional Bearer for
  ``likedByMe``).
- ``POST``/``DELETE /v1/assets/{assetId}/comments`` (and comment-id delete).
- ``PUT /v1/perches/{id}/favorites`` with ``{"sceneIds": [...]}`` (owner).
- ``POST /v1/perches/{id}/unpublish`` and ``DELETE /v1/perches/{id}`` (owner).

Perch GET JSON (public slug and owner GET) includes ``owner``, ``photographerFavorites``,
and per-export ``likeCount`` where implemented.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore


def _find_kestrel_dir(session_path: str | os.PathLike[str]) -> Path:
    p = Path(session_path).resolve()
    if p.name == ".kestrel" and p.is_dir():
        return p
    kd = p / ".kestrel"
    if kd.is_dir():
        return kd
    raise FileNotFoundError(f"No .kestrel directory under {p}")


def _raise_for_status(r: requests.Response) -> None:
    """Like Response.raise_for_status but include JSON `error` body (Worker auth errors)."""
    if r.ok:
        return
    detail = ""
    try:
        j = r.json()
        if isinstance(j, dict) and j.get("error") is not None:
            detail = f" — {j.get('error')}"
    except Exception:
        pass
    if not detail and r.text:
        detail = f" — {r.text[:800]}"
    msg = f"{r.status_code} Client Error: {r.reason} for url: {r.url}{detail}"
    raise requests.HTTPError(msg, response=r)


def _norm_rel(path_str: str) -> str:
    return (path_str or "").replace("\\", "/").strip()


class _PerchDeleted(Exception):
    """Raised by sync helpers when the server returns 404 for the linked perch.
    Caller is responsible for cleaning up perch_link.json + manifest. The
    bridge layer translates this to a `perch_deleted` job error and the JS
    layer surfaces a "this perch was deleted" toast + Re-publish CTA."""
    def __init__(self, perch_id: str):
        super().__init__(f"perch {perch_id} not found on server")
        self.perch_id = perch_id


class PerchLegalAcceptanceRequired(Exception):
    """Raised when Perch Worker returned 403 ``legal_acceptance_required``
    (launch item #13). The caller (api_bridge) opens ``accept_url`` in the
    system browser and surfaces a "review updated terms" toast to JS."""
    def __init__(self, accept_url: str | None, current_effective_date: str | None, message: str):
        super().__init__(message or "Updated terms must be reviewed before uploading.")
        self.accept_url = accept_url or "https://myaccount.projectkestrel.org/legal/accept"
        self.current_effective_date = current_effective_date


# Server error codes the Worker emits when a plan-tier cap denies a write.
# Keep in sync with `Perch Worker/src/lib/caps.ts`.
_PERCH_PLAN_LIMIT_ERROR_CODES = frozenset({
    "perch_limit_reached",
    "perch_storage_limit_reached",
    "perch_image_limit_reached",
    "perch_asset_limit_reached",
    "asset_too_large",
})


class PerchPlanLimitExceeded(Exception):
    """Raised when Perch Worker returned a structured plan-tier cap denial
    (HTTP 403 or 413 with one of the known ``error`` codes — see
    ``_PERCH_PLAN_LIMIT_ERROR_CODES``). The api_bridge layer translates this
    into a progress payload that the JS UI surfaces as a "you've hit your
    plan limit" card with an Upgrade button (links to
    ``myaccount.projectkestrel.org/perch``).

    This is distinct from generic HTTPError so the UI can show a friendly
    message + a direct upgrade link rather than a "presigning stuck"
    spinner or a raw "403" toast.
    """
    def __init__(
        self,
        error_code: str,
        *,
        status: int,
        message: str | None = None,
        tier: str | None = None,
        current: int | None = None,
        limit: int | None = None,
        filename: str | None = None,
        upgrade_url: str | None = None,
    ):
        super().__init__(message or f"Plan limit: {error_code}")
        self.error_code = error_code
        self.status = status
        self.tier = tier
        self.current = current
        self.limit = limit
        self.filename = filename
        self.upgrade_url = upgrade_url or "https://myaccount.projectkestrel.org/perch"

    @classmethod
    def from_response(cls, r: "requests.Response") -> "PerchPlanLimitExceeded | None":
        """Inspect a response; return a typed exception if the body matches
        a known plan-limit error code, else None. Safe to call on any 4xx —
        non-matching responses just return None so the caller can fall
        through to the generic error path."""
        if r.status_code not in (403, 413):
            return None
        try:
            body = r.json()
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        code = body.get("error")
        if not isinstance(code, str) or code not in _PERCH_PLAN_LIMIT_ERROR_CODES:
            return None
        def _i(k: str) -> int | None:
            v = body.get(k)
            return int(v) if isinstance(v, (int, float)) else None
        return cls(
            code,
            status=r.status_code,
            message=body.get("message") if isinstance(body.get("message"), str) else None,
            tier=body.get("tier") if isinstance(body.get("tier"), str) else None,
            current=_i("current"),
            limit=_i("limit"),
            filename=body.get("filename") if isinstance(body.get("filename"), str) else None,
            upgrade_url=body.get("upgrade_url") if isinstance(body.get("upgrade_url"), str) else None,
        )


def _join_under_session(session_root: Path, rel: str) -> Path:
    rel = _norm_rel(rel)
    if not rel or rel == ".":
        raise ValueError("Empty path")
    candidate = (session_root / rel).resolve()
    root = session_root.resolve()
    if not str(candidate).startswith(str(root)):
        raise ValueError(f"Path escapes session root: {rel}")
    return candidate


def _make_client_asset_id(
    filename: str,
    kind: str,
    crop_idx: int,
    scene_count: str,
    file_path: Path,
) -> str:
    """Deterministic client-stable asset ID for resumable uploads (Phase 2).

    Includes mtime+size so that a re-exported file produces a *different*
    ID — the server then treats it as a new asset rather than refusing to
    re-upload because "the same client_asset_id is already committed."
    The ``scene_count`` disambiguates duplicate filenames across scenes
    inside one folder. 24 hex chars of SHA-256 give plenty of headroom.
    """
    try:
        st = file_path.stat()
        mtime = st.st_mtime_ns
        size = st.st_size
    except OSError:
        mtime = 0
        size = 0
    raw = f"{filename}|{kind}|{crop_idx}|{scene_count}|{mtime}|{size}"
    return "kr_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _perch_upload_manifest_path(session_root: Path) -> Path:
    """Path to the in-progress per-asset manifest (Phase 2).

    Lives next to ``perch_link.json`` but is ephemeral — present only between
    presign and final success. After success it's renamed to
    ``perch_upload_manifest.completed.json`` for diagnostics.
    """
    return session_root / ".kestrel" / "perch_upload_manifest.json"


class _ManifestWriter:
    """Single-writer, batched, atomic-rename JSON manifest writer.

    The R2 upload pool is up to 48 threads — letting them race to write one
    JSON file is a recipe for partial reads on resume. This class owns one
    writer thread and a queue; callers enqueue ``(client_asset_id, patch)``
    tuples without blocking. The writer flushes on N pending updates OR
    every ``flush_interval_s`` seconds, whichever fires first.
    """

    def __init__(self, path: Path, initial: Dict[str, Any]):
        self._path = path
        self._state: Dict[str, Any] = dict(initial)
        if "assets" not in self._state or not isinstance(self._state["assets"], dict):
            self._state["assets"] = {}
        self._queue: Queue = Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._dirty = False
        self._flush_interval_s = 2.0
        self._flush_threshold = 10
        self._thread = threading.Thread(target=self._run, name="PerchManifestWriter", daemon=True)
        self._thread.start()
        # Write the initial state synchronously so a crash before the first
        # flush still leaves a usable file on disk.
        self._write_now()

    def patch_asset(self, client_asset_id: str, patch: Dict[str, Any]) -> None:
        """Merge ``patch`` into the asset's entry. Non-blocking."""
        self._queue.put((client_asset_id, dict(patch)))

    def update_root(self, patch: Dict[str, Any]) -> None:
        """Merge ``patch`` into top-level fields (non-asset). Forces a flush."""
        with self._lock:
            self._state.update(patch)
            self._dirty = True
        self._queue.put((None, None))  # wake the writer

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))  # deep copy via JSON

    def stop(self, final_flush: bool = True) -> None:
        self._stop.set()
        if final_flush:
            self._flush_pending()
            self._write_now()
        # Drain the worker by enqueueing a sentinel.
        self._queue.put((None, None))
        self._thread.join(timeout=5.0)

    def _flush_pending(self) -> None:
        while True:
            try:
                cid, patch = self._queue.get_nowait()
            except Empty:
                return
            if cid is None:
                continue
            with self._lock:
                slot = self._state["assets"].setdefault(cid, {})
                slot.update(patch or {})
                self._dirty = True

    def _run(self) -> None:
        last_flush = time.monotonic()
        pending = 0
        while not self._stop.is_set():
            try:
                cid, patch = self._queue.get(timeout=0.5)
            except Empty:
                cid, patch = None, None
            if cid is not None and patch is not None:
                with self._lock:
                    slot = self._state["assets"].setdefault(cid, {})
                    slot.update(patch)
                    self._dirty = True
                pending += 1
            now = time.monotonic()
            if self._dirty and (pending >= self._flush_threshold or now - last_flush >= self._flush_interval_s):
                self._write_now()
                last_flush = now
                pending = 0
        # Final drain on shutdown.
        self._flush_pending()
        if self._dirty:
            self._write_now()

    def _write_now(self) -> None:
        with self._lock:
            if not self._dirty and self._path.is_file():
                return
            payload = json.dumps(self._state, indent=2).encode("utf-8")
            self._dirty = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to .tmp in same dir, then os.replace.
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=self._path.parent, delete=False, suffix=".json.tmp"
        ) as fh:
            tmp = Path(fh.name)
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, self._path)


@dataclass
class PerchPreflightScene:
    """Per-scene summary used by the pre-upload UI to render a checkbox list."""
    scene_id: str
    title: str
    capture_time_ms: Optional[int]
    image_count: int
    export_count: int
    crop_count: int
    total_bytes: int
    top_quality: Optional[float]
    thumbnail_rel: Optional[str] = None  # session-relative export path of the highest-quality export, for UI preview
    reviewed: bool = False               # user_tags.finalized — drives the gold border in the Perch dialog timeline
    rejected_skipped: int = 0            # rows in this scene dropped by skip_rejected — shown as "+ N hidden" on the scene card
    species: List[str] = field(default_factory=list)   # user_tags.species — for scene-card pills
    families: List[str] = field(default_factory=list)  # user_tags.families — for scene-card pills


@dataclass
class PerchPreflight:
    """Aggregate totals + per-scene breakdown for one folder, no network calls."""
    scene_count: int
    image_count: int
    export_count: int
    crop_count: int
    total_bytes: int
    file_count: int
    scenes: List[PerchPreflightScene]
    rejected_skipped: int = 0  # number of CSV rows omitted because culled is True


def project_expected_after_exclusion(
    preflight: Optional[PerchPreflight],
    excluded_scene_ids: Iterable[str],
) -> Dict[str, int]:
    """Compute the `expected` POST body for /v1/perches given a preflight and a
    set of scene IDs the user de-selected. Returns zeros when no preflight is
    available so the worker treats it as "client didn't declare anything" and
    falls back to the legacy perch-count-only check (no regression for old
    clients or unusual code paths)."""
    out: Dict[str, int] = {
        "totalBytes": 0,
        "exportCount": 0,
        "cropCount": 0,
        "fileCount": 0,
    }
    if preflight is None:
        return out
    excluded = {str(s) for s in (excluded_scene_ids or ())}
    kept = [s for s in preflight.scenes if str(s.scene_id) not in excluded]
    out["totalBytes"] = int(sum(s.total_bytes for s in kept))
    out["exportCount"] = int(sum(s.export_count for s in kept))
    out["cropCount"] = int(sum(s.crop_count for s in kept))
    out["fileCount"] = out["exportCount"] + out["cropCount"]
    return out


@dataclass
class _RowUpload:
    filename: str
    scene_count: str
    export_path: Optional[str]
    crop_paths: List[str]
    crop_data: List[dict]  # per-crop metadata from crops_json, parallel to crop_paths
    quality: Optional[float]
    species: Optional[str]
    species_confidence: Optional[float]
    family: Optional[str]
    family_confidence: Optional[float]
    capture_time_ms: Optional[int]
    scene_name: str
    secondary_json: str
    crops_json: str
    exposure_correction: Optional[float]
    exposure_subject_stops: Optional[float]
    exposure_meter_scale: Optional[float]
    exposure_pipeline: Optional[str]
    group_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    export_asset_id: Optional[str] = None
    crop_asset_ids: List[str] = field(default_factory=list)


class PerchKestrelUploader:
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
        du = dev_user or os.environ.get("PERCH_DEV_USER_ID")
        if du:
            self._auth_headers["x-dev-user-id"] = str(du)
        t = str(jwt_token).strip() if jwt_token else ""
        if t:
            self._auth_headers["Authorization"] = f"Bearer {t}"
        if not du and not t:
            raise ValueError("Need Clerk JWT or PERCH_DEV_USER_ID for local Worker dev auth")
        self.s = self._new_session()
        # Cached preflight state — lets `run()` skip the CSV parse if preflight()
        # already ran for the same session_path.
        self._preflighted_root: Optional[Path] = None
        self._cached_rows: List[_RowUpload] = []
        self._cached_file_map: Dict[tuple, tuple] = {}
        self._cached_scenedata: Dict[str, Any] = {}
        self._cached_preflight: Optional[PerchPreflight] = None
        self._cached_skip_rejected: Optional[bool] = None
        self._catalog = None

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(self._auth_headers)
        return s

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    # ─── Preflight (no network) ─────────────────────────────────────────

    def preflight(
        self,
        session_path: str | os.PathLike[str],
        skip_rejected: bool = True,
    ) -> PerchPreflight:
        """Parse the session's CSV/scenedata, resolve file paths, sum byte sizes.

        No network calls. Caches state on `self` so a subsequent `run()` against
        the same session can skip the work.

        ``skip_rejected``: when True, rows with ``culled`` == truthy are dropped
        before any aggregation runs. The dropped count is returned as
        ``PerchPreflight.rejected_skipped`` so the UI can surface it.
        """
        session_root = Path(session_path).resolve()
        kestrel = _find_kestrel_dir(session_root)
        meta_path = kestrel / "kestrel_metadata.json"
        csv_name = "kestrel_database.csv"
        if meta_path.is_file():
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            csv_name = str(meta.get("database_file") or csv_name)
        csv_path = kestrel / csv_name
        if not csv_path.is_file():
            raise FileNotFoundError(f"Missing {csv_path}")

        scenedata_path = kestrel / "kestrel_scenedata.json"
        scenedata: Dict[str, Any] = {"version": "2.0", "scenes": {}}
        if scenedata_path.is_file():
            with open(scenedata_path, "r", encoding="utf-8") as f:
                scenedata = json.load(f)

        if pd is None:
            raise RuntimeError("pandas is required for PerchKestrelUploader")
        df = pd.read_csv(csv_path)
        rows: List[_RowUpload] = []
        rejected_skipped = 0
        rejected_per_scene: Dict[str, int] = {}
        for _, row in df.iterrows():
            # Parse scene_count first so we can attribute rejection counts to
            # the right scene bucket (the timeline shows "+ N hidden" per scene).
            sc_val = row.get("scene_count", 0) if "scene_count" in df.columns else 0
            try:
                sc = str(int(float(sc_val)))
            except (TypeError, ValueError):
                sc = str(sc_val) if sc_val is not None else "0"
            if skip_rejected and "culled" in df.columns:
                culled_val = row.get("culled")
                if culled_val is not None and pd.notna(culled_val):
                    cv = str(culled_val).strip().lower()
                    if cv in ("true", "reject", "1", "yes"):
                        rejected_skipped += 1
                        rejected_per_scene[sc] = rejected_per_scene.get(sc, 0) + 1
                        continue
            cap_ms = _parse_capture(row, df)
            sec_obj = {
                "secondary_species_list": row.get("secondary_species_list"),
                "secondary_species_scores": row.get("secondary_species_scores"),
                "secondary_family_list": row.get("secondary_family_list"),
                "secondary_family_scores": row.get("secondary_family_scores"),
            }
            # Parse crops_json to get all crops with per-crop metadata (primary source).
            _crops_json_str = _normalize_crops_json_cell(row, df)
            _crop_paths: List[str] = []
            _crop_data: List[dict] = []
            try:
                _parsed_crops = json.loads(_crops_json_str) if _crops_json_str and _crops_json_str != "[]" else []
                if isinstance(_parsed_crops, list):
                    for _c in _parsed_crops:
                        _cp = _c.get("crop_path") if isinstance(_c, dict) else None
                        if _cp and str(_cp).strip():
                            _crop_paths.append(str(_cp))
                            _crop_data.append(_c)
            except (json.JSONDecodeError, TypeError):
                pass

            # Fallback: legacy crop_path_0/crop_path_1 columns or single crop_path column.
            if not _crop_paths:
                _i = 0
                while f"crop_path_{_i}" in df.columns:
                    _v = row.get(f"crop_path_{_i}")
                    if _v is not None and pd.notna(_v):
                        _crop_paths.append(str(_v))
                        _crop_data.append({})
                    _i += 1
                if not _crop_paths and "crop_path" in df.columns and pd.notna(row.get("crop_path")):
                    _crop_paths.append(str(row["crop_path"]))
                    _crop_data.append({})

            rows.append(
                _RowUpload(
                    filename=str(row.get("filename", "")),
                    scene_count=sc,
                    export_path=str(row["export_path"])
                    if "export_path" in df.columns and pd.notna(row.get("export_path"))
                    else None,
                    crop_paths=_crop_paths,
                    crop_data=_crop_data,
                    quality=float(row["quality"])
                    if "quality" in df.columns and pd.notna(row.get("quality"))
                    else None,
                    species=str(row["species"])
                    if "species" in df.columns and pd.notna(row.get("species"))
                    else None,
                    species_confidence=float(row["species_confidence"])
                    if "species_confidence" in df.columns
                    and pd.notna(row.get("species_confidence"))
                    else None,
                    family=str(row["family"])
                    if "family" in df.columns and pd.notna(row.get("family"))
                    else None,
                    family_confidence=float(row["family_confidence"])
                    if "family_confidence" in df.columns
                    and pd.notna(row.get("family_confidence"))
                    else None,
                    capture_time_ms=cap_ms,
                    scene_name=str(row.get("scene_name", "") or "")
                    if "scene_name" in df.columns
                    else "",
                    secondary_json=json.dumps(sec_obj, default=str),
                    crops_json=_crops_json_str,
                    exposure_correction=_opt_float_csv(row, "exposure_correction"),
                    exposure_subject_stops=_opt_float_csv(row, "exposure_subject_stops"),
                    exposure_meter_scale=_opt_float_csv(row, "exposure_meter_scale"),
                    exposure_pipeline=_opt_str_csv(row, "exposure_pipeline"),
                )
            )

        will_export: set[int] = set()
        for idx, ru in enumerate(rows):
            if ru.export_path:
                try:
                    ep = _join_under_session(session_root, ru.export_path)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if ep.is_file():
                    will_export.add(idx)

        # file_map: (row_idx, kind, crop_idx) -> (Path, crops_body)
        file_map: Dict[tuple, tuple] = {}
        for idx, ru in enumerate(rows):
            crops_full = ru.crops_json if ru.crops_json else "[]"
            if ru.export_path:
                try:
                    ep = _join_under_session(session_root, ru.export_path)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if ep.is_file():
                    file_map[(idx, "export", 0)] = (ep, crops_full)
            for ci, cp_rel in enumerate(ru.crop_paths):
                try:
                    cp = _join_under_session(session_root, cp_rel)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if cp.is_file():
                    crops_body = crops_full if idx not in will_export else ""
                    file_map[(idx, "crop", ci)] = (cp, crops_body)

        # Per-scene aggregation — group file_map entries by their row's scene_id.
        per_scene: Dict[str, Dict[str, Any]] = {}
        for (idx, kind, _ci), (path, _body) in file_map.items():
            ru = rows[idx]
            sid = ru.scene_count
            bucket = per_scene.setdefault(
                sid,
                {
                    "scene_id": sid,
                    "title_candidates": [],     # keep raw scene_name strings; pick first non-empty after sort
                    "capture_time_ms": None,
                    "image_count": 0,
                    "export_count": 0,
                    "crop_count": 0,
                    "total_bytes": 0,
                    "top_quality": None,
                    "row_indices": set(),       # for image_count via export rows
                    "thumbnail_rel": None,      # rel path of highest-quality export
                    "thumbnail_quality": None,  # tracks current best quality
                },
            )
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            bucket["total_bytes"] += int(size)
            if kind == "export":
                bucket["export_count"] += 1
                # Track the best (highest-quality) export rel path for the UI thumbnail.
                cur_thumb_q = bucket["thumbnail_quality"]
                if bucket["thumbnail_rel"] is None:
                    bucket["thumbnail_rel"] = ru.export_path
                    bucket["thumbnail_quality"] = ru.quality
                elif ru.quality is not None and (cur_thumb_q is None or ru.quality > cur_thumb_q):
                    bucket["thumbnail_rel"] = ru.export_path
                    bucket["thumbnail_quality"] = ru.quality
            else:
                bucket["crop_count"] += 1
            bucket["row_indices"].add(idx)
            if ru.scene_name:
                bucket["title_candidates"].append(ru.scene_name.strip())
            if ru.capture_time_ms is not None:
                cur = bucket["capture_time_ms"]
                if cur is None or ru.capture_time_ms < cur:
                    bucket["capture_time_ms"] = ru.capture_time_ms
            if ru.quality is not None:
                cur_q = bucket["top_quality"]
                bucket["top_quality"] = ru.quality if cur_q is None else max(cur_q, ru.quality)

        scenes: List[PerchPreflightScene] = []
        for sid, b in per_scene.items():
            sd = (scenedata.get("scenes") or {}).get(str(sid), {})
            title = ""
            if isinstance(sd, dict) and (sd.get("name") or "").strip():
                title = str(sd["name"]).strip()
            if not title:
                for cand in b["title_candidates"]:
                    if cand:
                        title = cand
                        break
            if not title:
                title = f"Scene {sid}"
            # image_count = export count when exports exist; otherwise distinct row count
            image_count = b["export_count"] if b["export_count"] > 0 else len(b["row_indices"])
            user_tags = sd.get("user_tags") if isinstance(sd, dict) else None
            user_tags = user_tags if isinstance(user_tags, dict) else {}
            ut_species = user_tags.get("species") or []
            ut_families = user_tags.get("families") or []
            scenes.append(
                PerchPreflightScene(
                    scene_id=str(sid),
                    title=title,
                    capture_time_ms=b["capture_time_ms"],
                    image_count=image_count,
                    export_count=b["export_count"],
                    crop_count=b["crop_count"],
                    total_bytes=b["total_bytes"],
                    top_quality=b["top_quality"],
                    thumbnail_rel=b["thumbnail_rel"],
                    reviewed=bool(user_tags.get("finalized") is True),
                    rejected_skipped=int(rejected_per_scene.get(str(sid), 0)),
                    species=[str(x) for x in ut_species if isinstance(x, (str, int))],
                    families=[str(x) for x in ut_families if isinstance(x, (str, int))],
                )
            )
        # Chronological order; scenes without timestamps sort last.
        scenes.sort(
            key=lambda s: (
                0 if s.capture_time_ms is not None else 1,
                s.capture_time_ms or 0,
                int(s.scene_id) if s.scene_id.isdigit() else 0,
            )
        )

        total_bytes = sum(s.total_bytes for s in scenes)
        export_count = sum(s.export_count for s in scenes)
        crop_count = sum(s.crop_count for s in scenes)
        image_count = sum(s.image_count for s in scenes)

        preflight = PerchPreflight(
            scene_count=len(scenes),
            image_count=image_count,
            export_count=export_count,
            crop_count=crop_count,
            total_bytes=total_bytes,
            file_count=len(file_map),
            scenes=scenes,
            rejected_skipped=rejected_skipped,
        )

        # Cache for run().
        self._preflighted_root = session_root
        self._cached_rows = rows
        self._cached_file_map = file_map
        self._cached_scenedata = scenedata
        self._cached_skip_rejected = skip_rejected
        self._cached_preflight = preflight
        return preflight

    # ─── Upload (network) ───────────────────────────────────────────────

    def run(
        self,
        session_path: str | os.PathLike[str],
        title: Optional[str] = None,
        excluded_scene_ids: Iterable[str] = (),
        progress_callback: Optional[Callable[[dict], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        skip_rejected: bool = True,
        existing_perch_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_root = Path(session_path).resolve()

        def emit(payload: dict) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(payload)
            except Exception:
                pass  # never let UI errors break the upload

        # Reuse cached preflight if it matches; otherwise run preflight now.
        # Note: cached preflight is only reused if its skip_rejected matches —
        # we re-run preflight if the caller flipped the flag between calls.
        cached_skip = getattr(self, "_cached_skip_rejected", None)
        if (
            self._preflighted_root != session_root
            or self._cached_preflight is None
            or cached_skip != skip_rejected
        ):
            self.preflight(session_root, skip_rejected=skip_rejected)
        rows = self._cached_rows
        file_map = self._cached_file_map
        scenedata = self._cached_scenedata

        excluded = {str(s) for s in (excluded_scene_ids or ())}

        if excluded:
            keep_indices = {
                idx for idx, ru in enumerate(rows) if str(ru.scene_count) not in excluded
            }
            rows = [ru for idx, ru in enumerate(rows) if idx in keep_indices]
            # Rebuild file_map with the new row indices (re-derive index by identity).
            old_to_new: Dict[int, int] = {}
            old_idx = 0
            new_idx = 0
            for kept_idx in sorted(keep_indices):
                old_to_new[kept_idx] = new_idx
                new_idx += 1
                old_idx = kept_idx
            del old_idx
            new_file_map: Dict[tuple, tuple] = {}
            for (oi, kind, ci), val in file_map.items():
                if oi in old_to_new:
                    new_file_map[(old_to_new[oi], kind, ci)] = val
            file_map = new_file_map

        if not rows or not file_map:
            raise RuntimeError("No assets selected for upload")

        # ── Create or reuse the perch (Phase 2: idempotency-key) ────────────
        idemp_key = idempotency_key or str(uuid.uuid4())
        if existing_perch_id:
            # Resume path — caller already knows the server-side perch.
            perch_id = str(existing_perch_id)
            # Fetch base_url so the success card has a working "Open" link.
            base_url = ""
            try:
                gr = self.s.get(self._url(f"/v1/perches/{perch_id}"), timeout=self.timeout)
                if gr.ok:
                    gj = gr.json()
                    base_url = str(gj.get("url") or "")
            except Exception:
                pass
            emit({"phase": "creating_perch", "resumed": True})
        else:
            emit({"phase": "creating_perch"})

            # Project the post-exclusion preflight so the worker can pre-check
            # user-wide storage / image / total-asset quotas before issuing
            # presigns. Server treats this body as an untrusted hint — declared
            # bytes are reconciled at commit via R2 HEAD — but a truthful client
            # gets pre-rejection instead of failing midway through presign.
            expected_payload = project_expected_after_exclusion(
                self._cached_preflight, excluded
            )

            res = self.s.post(
                self._url("/v1/perches"),
                json={
                    "title": title or session_root.name,
                    "idempotencyKey": idemp_key,
                    "expected": expected_payload,
                },
                headers={"Idempotency-Key": idemp_key},
                timeout=self.timeout,
            )
            # ToS / Privacy Policy gate (launch item #13). The Perch Worker
            # returns 403 with a structured body that includes the URL the
            # desktop should open in the system browser. Surface as a typed
            # exception so api_bridge can open the browser + show a clean
            # toast instead of a generic HTTP error.
            if res.status_code == 403:
                try:
                    body_json = res.json()
                except Exception:
                    body_json = None
                if (
                    isinstance(body_json, dict)
                    and body_json.get("error") == "legal_acceptance_required"
                ):
                    raise PerchLegalAcceptanceRequired(
                        body_json.get("accept_url"),
                        body_json.get("currentEffectiveDate"),
                        str(body_json.get("message") or ""),
                    )
            # Plan-tier caps (Stage 7). `perch_limit_reached` is the only
            # code that can fire from POST /v1/perches today; the rest are
            # presign-only. Detect generically so future codes Just Work.
            plan_err = PerchPlanLimitExceeded.from_response(res)
            if plan_err is not None:
                raise plan_err
            _raise_for_status(res)
            data = res.json()
            perch_id = str(data["id"])
            base_url = str(data.get("url", ""))

        # Build scene-structured presign payload from the (possibly filtered) rows + file_map.
        scene_payload, ordered_keys = self._build_scene_presign_payload(
            rows, scenedata, file_map
        )

        # Presign in chunks; emit per-chunk progress.
        presign_results = self._presign_scenes(
            perch_id, scene_payload, ordered_keys, on_chunk=lambda cur, tot: emit(
                {"phase": "presigning", "current": cur, "total": tot}
            )
        )

        # ── Initialize the on-disk manifest for resumability (Phase 2) ──────
        # Records every asset we just presigned (or that the server reported as
        # already-committed). Updated atomically as R2 PUTs complete + commit
        # calls succeed. Survives a Kestrel crash so the next launch can resume.
        manifest_path = _perch_upload_manifest_path(session_root)
        manifest_initial: Dict[str, Any] = {
            "version": 1,
            "perch_id": perch_id,
            "perch_url": base_url,
            "idempotency_key": idemp_key,
            "started_at_ms": int(time.time() * 1000),
            "total_assets": len(presign_results),
            "skip_rejected_used": bool(skip_rejected),
            "title": title or session_root.name,
            "assets": {},
        }
        for rec in presign_results:
            cid = rec.get("client_asset_id")
            if not cid:
                continue
            fk = rec["file_key"]
            path = file_map[fk][0]
            manifest_initial["assets"][cid] = {
                "filename": path.name,
                "kind": fk[1],
                "asset_id_remote": rec.get("asset_id"),
                "state": "committed" if rec.get("committed") else "pending",
                "uploaded_at_ms": int(time.time() * 1000) if rec.get("committed") else None,
            }
        manifest = _ManifestWriter(manifest_path, manifest_initial)

        # ── Upload list — only assets the server says are NOT yet committed ──
        upload_items: List[tuple] = []
        already_committed = 0
        for rec in presign_results:
            if rec.get("committed"):
                already_committed += 1
                continue
            fk = rec["file_key"]
            path = file_map[fk][0]
            upload_items.append((
                fk,
                path,
                rec["upload_url"],
                self._content_type(path),
                rec.get("asset_id"),
                rec.get("client_asset_id"),
            ))

        total = len(presign_results)
        remaining = len(upload_items)
        print(f"[perch] Resumable upload: {already_committed}/{total} already committed; "
              f"{remaining} files to PUT.")

        # Counter starts at the already-committed count so the UI shows the
        # true "uploaded so far" rather than counting from zero on a resume.
        completed_count = already_committed
        lock = threading.Lock()
        canceled = False

        def _do_upload(item: tuple) -> Optional[str]:
            nonlocal completed_count
            file_key, path, upload_url, content_type, asset_id, client_asset_id = item
            if cancel_event is not None and cancel_event.is_set():
                return None
            self._upload_direct(upload_url, path, content_type)
            # Confirm the R2 PUT to the Worker — flips upload_state to 'committed'.
            committed_ok = False
            try:
                cr = self.s.post(
                    self._url(f"/v1/perches/{perch_id}/assets/{asset_id}/commit"),
                    timeout=self.timeout,
                )
                if cr.ok:
                    committed_ok = True
            except Exception:
                pass
            # Update manifest entry — non-blocking, batched flush.
            if client_asset_id:
                manifest.patch_asset(client_asset_id, {
                    "state": "committed" if committed_ok else "uploaded_uncommitted",
                    "uploaded_at_ms": int(time.time() * 1000),
                    "asset_id_remote": asset_id,
                })
            with lock:
                completed_count += 1
                fname = path.name
                print(f"[perch] {completed_count}/{total} uploaded — {fname}")
            emit({
                "phase": "uploading",
                "uploaded": completed_count,
                "total": total,
                "filename": fname,
            })
            return fname

        MAX_PARALLEL = 48
        try:
            with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
                futures = [executor.submit(_do_upload, item) for item in upload_items]
                for fut in as_completed(futures):
                    if cancel_event is not None and cancel_event.is_set() and not canceled:
                        # Cancel any not-yet-started futures; in-flight PUTs finish.
                        for pending in futures:
                            pending.cancel()
                        canceled = True
                    try:
                        fut.result()
                    except CancelledError:
                        pass
                    except Exception:
                        # Surface upload errors to the caller, but emit error event first.
                        if not canceled:
                            raise
        except Exception:
            # Manifest is already on disk via the writer thread — keep it for
            # the resume flow. Just stop the writer cleanly before bubbling up.
            manifest.update_root({"finished_at_ms": int(time.time() * 1000), "outcome": "error"})
            manifest.stop()
            raise

        if canceled:
            print(f"[perch] Upload canceled at {completed_count}/{total} files.")
            manifest.update_root({"finished_at_ms": int(time.time() * 1000), "outcome": "canceled"})
            manifest.stop()
            emit({
                "phase": "canceled",
                "perch_url": base_url,
                "perch_id": perch_id,
                "uploaded": completed_count,
                "total": total,
            })
            return {
                "perch_id": perch_id,
                "url": base_url,
                "scene_count": len(scene_payload),
                "canceled": True,
                "uploaded": completed_count,
                "total": total,
            }

        print(f"[perch] All {total} files uploaded.")
        scene_count = len(scene_payload)

        # Notify the server that uploads are finished so it can flip the
        # perch from upload_state='uploading' to 'complete'. This is what
        # converts the held byte-budget reservation into actual usage
        # (server-side); without it, the perch would eventually be swept
        # to 'incomplete' by the cron and the user would lose their
        # storage allocation. Best-effort: log on failure and continue
        # (the server's cron will reconcile on its own schedule, and a
        # later sync from the desktop will retry).
        try:
            uc_resp = self.s.post(
                self._url(f"/v1/perches/{perch_id}/upload-complete"),
                json={},
                timeout=self.timeout,
            )
            if uc_resp.status_code >= 500:
                # 5xx is retryable; one quick retry covers transient
                # Worker hiccups without delaying the user noticeably.
                try:
                    uc_resp = self.s.post(
                        self._url(f"/v1/perches/{perch_id}/upload-complete"),
                        json={},
                        timeout=self.timeout,
                    )
                except requests.RequestException as e:
                    print(f"[perch] upload-complete retry failed: {e}")
            if uc_resp.status_code >= 400:
                print(
                    f"[perch] upload-complete returned {uc_resp.status_code}: "
                    f"{uc_resp.text[:300]}"
                )
        except requests.RequestException as e:
            print(f"[perch] upload-complete network error: {e}")

        # Mark complete and rotate the manifest aside (kept for diagnostics).
        manifest.update_root({"finished_at_ms": int(time.time() * 1000), "outcome": "done"})
        manifest.stop()
        try:
            completed_path = manifest_path.with_name("perch_upload_manifest.completed.json")
            os.replace(manifest_path, completed_path)
        except OSError:
            pass
        emit({"phase": "done", "perch_url": base_url, "perch_id": perch_id})
        return {
            "perch_id": perch_id,
            "url": base_url,
            "scene_count": scene_count,
            "idempotency_key": idemp_key,
        }

    # ─── Sync (Phase 3) ─────────────────────────────────────────────────

    # Tolerance for "did this float change?" — quality + exposure values are
    # lossy through CSV round-trip; treat tiny diffs as no-op so we don't spam
    # PATCHes on every Sync click.
    _FLOAT_DRIFT_EPS = 1e-4

    @staticmethod
    def _floats_equal(a: Optional[float], b: Optional[float]) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        try:
            return abs(float(a) - float(b)) <= PerchKestrelUploader._FLOAT_DRIFT_EPS
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _strs_equal(a: Optional[str], b: Optional[str]) -> bool:
        # Treat None and "" as equivalent — server normalizes empty to NULL.
        sa = (a or "").strip()
        sb = (b or "").strip()
        return sa == sb

    @staticmethod
    def _ints_equal(a: Optional[int], b: Optional[int]) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return int(a) == int(b)

    def _build_sync_inputs(
        self, session_path: str | os.PathLike[str]
    ) -> tuple[Path, Dict[str, Any], Dict[str, Any], Dict[tuple, tuple], List["_RowUpload"], Dict[str, Any]]:
        """Read perch_link.json, run preflight (skip_rejected=False), GET the
        flat asset list. Returns the pieces compute_sync_diff/sync need."""
        session_root = Path(session_path).resolve()
        link_path = session_root / ".kestrel" / "perch_link.json"
        if not link_path.is_file():
            raise FileNotFoundError("This folder isn't linked to a perch yet — upload it first.")
        with open(link_path, "r", encoding="utf-8") as f:
            link = json.load(f)
        perch_id = str(link.get("perch_id") or "").strip()
        if not perch_id:
            raise RuntimeError("perch_link.json has no perch_id")

        # Re-run preflight WITH rejected rows so we can mark them deleted server-side.
        self.preflight(session_root, skip_rejected=False)

        # Server flat list — asks for everything we need to diff.
        r = self.s.get(self._url(f"/v1/perches/{perch_id}/assets"), timeout=self.timeout)
        if r.status_code == 404:
            # Caller (compute_sync_diff / sync_to_perch) handles the cleanup.
            raise _PerchDeleted(perch_id)
        _raise_for_status(r)
        body = r.json()

        # Index server assets by client_asset_id. Anything without a
        # clientAssetId is a legacy upload — we can't safely diff it because we
        # don't have a stable key, so we just leave it untouched.
        server_assets = body.get("assets") or []
        server_by_cid: Dict[str, Dict[str, Any]] = {}
        for a in server_assets:
            cid = a.get("clientAssetId")
            if isinstance(cid, str) and cid:
                server_by_cid[cid] = a

        # Map kestrelSceneId → (server scene_id, server scene title) for title diff.
        scene_titles_by_kid: Dict[str, Dict[str, Any]] = {}
        for a in server_assets:
            kid = a.get("kestrelSceneId")
            if not kid:
                continue
            scene_titles_by_kid.setdefault(
                str(kid),
                {"server_scene_id": a.get("sceneId"), "server_title": a.get("sceneTitle")},
            )

        return (
            session_root,
            link,
            {"server_by_cid": server_by_cid, "scene_titles_by_kid": scene_titles_by_kid, "raw": body},
            self._cached_file_map or {},
            self._cached_rows or [],
            self._cached_scenedata or {"scenes": {}},
        )

    def compute_sync_diff(self, session_path: str | os.PathLike[str]) -> Dict[str, Any]:
        """Read-only — returns what would change without applying anything.

        The returned shape feeds the diff-preview modal. ``additions`` is
        reported but NOT applied by ``sync_to_perch`` in v1 (would require a
        new presign+upload flow); the modal surfaces them so the user knows
        they need a fresh re-upload to add them.
        """
        session_root, link, server, file_map, rows, scenedata = self._build_sync_inputs(session_path)
        server_by_cid: Dict[str, Dict[str, Any]] = server["server_by_cid"]
        scene_titles_by_kid: Dict[str, Dict[str, Any]] = server["scene_titles_by_kid"]

        # Build a kestrel_scene_id → local title map (mirrors the logic in
        # _build_scene_presign_payload: prefer scenedata.scenes[sid].name, else
        # the row's scene_name, else "Scene N").
        scenes_meta = (scenedata or {}).get("scenes") or {}
        local_title_by_kid: Dict[str, str] = {}
        for ru in rows:
            kid = str(ru.scene_count)
            if kid in local_title_by_kid:
                continue
            sd = scenes_meta.get(kid, {}) if isinstance(scenes_meta, dict) else {}
            title = ""
            if isinstance(sd, dict) and (sd.get("name") or "").strip():
                title = str(sd["name"]).strip()
            elif (ru.scene_name or "").strip():
                title = ru.scene_name.strip()
            if title:
                local_title_by_kid[kid] = title

        additions: List[Dict[str, Any]] = []
        deletions: List[Dict[str, Any]] = []
        field_updates: List[Dict[str, Any]] = []
        scene_title_updates: List[Dict[str, Any]] = []

        # Track which client_asset_ids the local state covers, so we can spot
        # server_only rows (currently ignored in v1 — surfaced in report only).
        local_cids: set = set()

        # Build a per-row "is this row rejected" flag. The reject filter in
        # preflight() drops rejected rows when skip_rejected=True; here we ran
        # with skip_rejected=False, so rejected rows are present and we use
        # the row's own .culled-equivalent flag if available. _RowUpload
        # doesn't carry culled directly — re-check the cached rows attribute.
        rejected_cids: set = set()

        # Re-derive culled from the dataframe cache by re-reading the CSV row
        # is too expensive; instead, _build_sync_inputs ran preflight with
        # skip_rejected=False, so every row is in self._cached_rows. We need
        # to know which ones the user marked culled. Add a quick re-read here.
        rejected_filenames: set = self._read_culled_filenames(session_root)

        for idx, ru in enumerate(rows):
            row_is_rejected = ru.filename in rejected_filenames
            kid = str(ru.scene_count)

            # Build (kind, crop_idx) entries that have a file_map entry.
            entries: List[tuple] = []
            if (idx, "export", 0) in file_map:
                entries.append(("export", 0))
            ci = 0
            while (idx, "crop", ci) in file_map:
                entries.append(("crop", ci))
                ci += 1

            for kind, crop_idx in entries:
                path = file_map[(idx, kind, crop_idx)][0]
                cid = _make_client_asset_id(path.name, kind, crop_idx, kid, path)
                local_cids.add(cid)
                server_row = server_by_cid.get(cid)

                if server_row is None:
                    if not row_is_rejected:
                        additions.append({
                            "client_asset_id": cid,
                            "kind": kind,
                            "filename": path.name,
                            "scene_count": kid,
                        })
                    # rejected + missing on server → nothing to do
                    continue

                if row_is_rejected and server_row.get("status") != "deleted":
                    deletions.append({
                        "server_asset_id": server_row.get("assetId"),
                        "client_asset_id": cid,
                        "filename": path.name,
                        "kind": kind,
                    })
                    continue

                # Compare per-asset metadata fields.
                changes: Dict[str, Any] = {}
                # species / family — strings
                if not self._strs_equal(server_row.get("species"), ru.species):
                    changes["species"] = ru.species or None
                if not self._strs_equal(server_row.get("family"), ru.family):
                    changes["family"] = ru.family or None
                # scientific species / family — resolved from the bird catalog
                sci_sp, sci_fa = self._resolve_scientific(ru.species, ru.family)
                if not self._strs_equal(server_row.get("speciesScientific"), sci_sp):
                    changes["speciesScientific"] = sci_sp or None
                if not self._strs_equal(server_row.get("familyScientific"), sci_fa):
                    changes["familyScientific"] = sci_fa or None
                # quality, capture time
                if not self._floats_equal(server_row.get("quality"), ru.quality):
                    changes["quality"] = ru.quality
                if not self._ints_equal(server_row.get("captureTimeMs"), ru.capture_time_ms):
                    changes["captureTimeMs"] = ru.capture_time_ms
                # secondary_json — string compare (already JSON-encoded both sides)
                if not self._strs_equal(server_row.get("secondaryJson") or server_row.get("secondary_json"), ru.secondary_json):
                    changes["secondaryJson"] = ru.secondary_json or None
                # exposure_* — numeric, all skipped because they're ML-derived
                # (not user-edited). Add later if a use-case shows up.

                if changes:
                    field_updates.append({
                        "server_asset_id": server_row.get("assetId"),
                        "client_asset_id": cid,
                        "filename": path.name,
                        "kind": kind,
                        "changes": changes,
                    })

        # Scene-title diffs. Only PATCH a scene the server actually has.
        for kid, server_scene in scene_titles_by_kid.items():
            new_title = local_title_by_kid.get(kid, "")
            old_title = (server_scene.get("server_title") or "")
            if new_title and not self._strs_equal(old_title, new_title):
                scene_title_updates.append({
                    "server_scene_id": server_scene.get("server_scene_id"),
                    "kestrel_scene_id": kid,
                    "old_title": old_title or None,
                    "new_title": new_title,
                })

        return {
            "perch_id": str(link.get("perch_id") or ""),
            "perch_url": str(link.get("perch_url") or ""),
            "title": str(link.get("title") or ""),
            "additions": additions,
            "deletions": deletions,
            "field_updates": field_updates,
            "scene_title_updates": scene_title_updates,
            "totals": {
                "additions": len(additions),
                "deletions": len(deletions),
                "field_updates": len(field_updates),
                "scene_title_updates": len(scene_title_updates),
                "local_assets": len(local_cids),
                "server_assets": len(server_by_cid),
            },
        }

    @staticmethod
    def _read_culled_filenames(session_root: Path) -> set:
        """Re-read the CSV's culled column to flag rejected rows. Cheap; the
        CSV was already loaded by preflight, but pandas doesn't keep it on the
        uploader instance. Re-reads only the filename + culled columns."""
        kestrel = _find_kestrel_dir(session_root)
        meta_path = kestrel / "kestrel_metadata.json"
        csv_name = "kestrel_database.csv"
        if meta_path.is_file():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                csv_name = str(meta.get("database_file") or csv_name)
            except OSError:
                pass
        csv_path = kestrel / csv_name
        if not csv_path.is_file() or pd is None:
            return set()
        try:
            df = pd.read_csv(csv_path, usecols=lambda c: c in ("filename", "culled"))
        except (ValueError, OSError):
            return set()
        if "culled" not in df.columns or "filename" not in df.columns:
            return set()
        rejected: set = set()
        for _, row in df.iterrows():
            v = row.get("culled")
            if v is None or pd.isna(v):
                continue
            if str(v).strip().lower() in ("true", "reject", "1", "yes"):
                rejected.add(str(row.get("filename") or ""))
        return rejected

    def sync_to_perch(
        self,
        session_path: str | os.PathLike[str],
        progress_callback: Optional[Callable[[dict], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Apply the diff returned by compute_sync_diff. PATCHes are sent
        sequentially — D1's write throughput is the bottleneck and parallel
        writes would just queue up server-side. ``additions`` are reported
        but NOT applied in v1 (would need a fresh presign + upload pass).
        """
        def emit(payload: dict) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(payload)
            except Exception:
                pass

        emit({"phase": "fetching_state"})
        try:
            diff = self.compute_sync_diff(session_path)
        except _PerchDeleted as pd_err:
            emit({"phase": "error", "message": "perch_deleted", "perch_id": pd_err.perch_id})
            return {"ok": False, "error": "perch_deleted", "perch_id": pd_err.perch_id}
        except Exception as e:
            emit({"phase": "error", "message": str(e)})
            raise

        perch_id = diff["perch_id"]
        deletions = diff["deletions"]
        field_updates = diff["field_updates"]
        scene_title_updates = diff["scene_title_updates"]
        additions = diff["additions"]

        total_actions = len(deletions) + len(field_updates) + len(scene_title_updates)
        emit({"phase": "computing_diff", "total": total_actions})

        applied = 0
        errors: List[str] = []

        def _apply(kind: str, fn: Callable[[], requests.Response], label: str) -> bool:
            nonlocal applied
            if cancel_event is not None and cancel_event.is_set():
                return False
            try:
                resp = fn()
                if not resp.ok:
                    errors.append(f"{kind} {label}: HTTP {resp.status_code}")
                    return False
            except Exception as e:
                errors.append(f"{kind} {label}: {e}")
                return False
            applied += 1
            emit({
                "phase": "applying",
                "current": applied,
                "total": total_actions,
                "action": kind,
                "label": label,
            })
            return True

        # 1) Deletions first — they free server storage before we PATCH others.
        for d in deletions:
            aid = d["server_asset_id"]
            if not aid:
                continue
            _apply(
                "delete",
                lambda aid=aid: self.s.patch(
                    self._url(f"/v1/perches/{perch_id}/assets/{aid}"),
                    json={"status": "deleted"},
                    timeout=self.timeout,
                ),
                d.get("filename") or "",
            )

        # 2) Per-asset metadata field updates.
        for u in field_updates:
            aid = u["server_asset_id"]
            if not aid:
                continue
            _apply(
                "update",
                lambda aid=aid, body=u["changes"]: self.s.patch(
                    self._url(f"/v1/perches/{perch_id}/assets/{aid}"),
                    json=body,
                    timeout=self.timeout,
                ),
                u.get("filename") or "",
            )

        # 3) Scene title PATCHes.
        for s in scene_title_updates:
            sid = s["server_scene_id"]
            if not sid:
                continue
            _apply(
                "scene-title",
                lambda sid=sid, title=s["new_title"]: self.s.patch(
                    self._url(f"/v1/perches/{perch_id}/scenes/{sid}"),
                    json={"title": title},
                    timeout=self.timeout,
                ),
                s.get("new_title") or "",
            )

        canceled = bool(cancel_event and cancel_event.is_set())
        emit({
            "phase": "canceled" if canceled else "done",
            "applied": applied,
            "total": total_actions,
            "errors": errors,
            "additions_skipped": len(additions),
        })
        return {
            "ok": True,
            "applied": applied,
            "total": total_actions,
            "errors": errors,
            "additions_skipped": len(additions),
            "canceled": canceled,
        }

    # Max scenes per presign request. The worker generates one HMAC-SHA256 per
    # asset (export + each crop), so wall-clock CPU per request scales with
    # `chunk * avg_assets_per_scene`. Kept conservative because Cloudflare's
    # per-isolate CPU budget tightens after sustained heavy use, and a single
    # presign call that times out forces the user to start over.
    _PRESIGN_SCENE_CHUNK = 15

    @staticmethod
    def _content_type(path: Path) -> str:
        el = path.name.lower()
        if el.endswith(".png"):
            return "image/png"
        if el.endswith(".webp"):
            return "image/webp"
        return "image/jpeg"

    def _resolve_scientific(
        self,
        common_species: Optional[str],
        common_family: Optional[str],
    ) -> tuple[str, str]:
        """Resolve (scientific_species, scientific_family) from the bundled bird
        catalog. Returns empty strings when nothing matches; never raises."""
        if self._catalog is None:
            try:
                try:
                    from kestrel_analyzer.bird_catalog import get_catalog
                except ImportError:
                    from analyzer.kestrel_analyzer.bird_catalog import get_catalog
                self._catalog = get_catalog()
            except Exception:
                self._catalog = False  # type: ignore[assignment]  # sentinel: catalog unavailable
        if not self._catalog:
            return "", ""
        sci_sp = ""
        sci_fa = ""
        if common_species:
            rec = self._catalog.lookup(common_species)
            if rec:
                sci_sp = rec.scientific_name or ""
                sci_fa = rec.family_sci or ""
        if common_family and not sci_fa:
            sci_fa = self._catalog.family_sci_for(common_family) or ""
        return sci_sp, sci_fa

    def _build_scene_presign_payload(
        self,
        rows: List["_RowUpload"],
        scenedata: Dict[str, Any],
        file_map: Dict[tuple, tuple],
    ) -> tuple:
        """Group rows into scenes and build the JSON payload for the presign endpoint.

        Returns (scene_payload, ordered_keys) where ordered_keys is the flat list of
        (row_idx, kind, crop_idx) in the same order assets appear in the payload —
        used to match returned uploadUrls back to local file paths.
        """
        by_scene: Dict[str, List[tuple]] = {}  # scene_id -> [(idx, ru)]
        for idx, ru in enumerate(rows):
            by_scene.setdefault(ru.scene_count, []).append((idx, ru))

        scene_ids = sorted(by_scene.keys(), key=lambda s: (int(s) if s.isdigit() else 0, s))

        scene_payload = []
        ordered_keys: List[tuple] = []  # (row_idx, kind, crop_idx)

        for sort_i, sc in enumerate(scene_ids):
            srows = by_scene[sc]
            srows.sort(
                key=lambda x: (x[1].quality is not None, x[1].quality or 0.0),
                reverse=True,
            )

            cap: Optional[int] = None
            max_q: Optional[float] = None
            for _, ru in srows:
                if ru.capture_time_ms is not None:
                    if cap is None or ru.capture_time_ms < cap:
                        cap = ru.capture_time_ms
                if ru.quality is not None:
                    max_q = ru.quality if max_q is None else max(max_q, ru.quality)

            title = srows[0][1].scene_name.strip() if srows[0][1].scene_name else f"Scene {sc}"
            sd = (scenedata.get("scenes") or {}).get(str(sc), {})
            if isinstance(sd, dict) and (sd.get("name") or "").strip():
                title = str(sd["name"]).strip()

            assets_payload = []
            sort_in_scene = 0
            for idx, ru in srows:
                export_sort_in_scene = sort_in_scene
                # Export
                if (idx, "export", 0) in file_map:
                    path, crops_body = file_map[(idx, "export", 0)]
                    try:
                        byte_len = path.stat().st_size
                    except OSError:
                        byte_len = 0
                    sci_sp_ex, sci_fa_ex = self._resolve_scientific(ru.species, ru.family)
                    assets_payload.append({
                        "kind": "export",
                        "filename": path.name,
                        "contentType": self._content_type(path),
                        "sortInScene": export_sort_in_scene,
                        "quality": ru.quality,
                        "species": ru.species,
                        "speciesConfidence": ru.species_confidence,
                        "family": ru.family,
                        "familyConfidence": ru.family_confidence,
                        "speciesScientific": sci_sp_ex or None,
                        "familyScientific": sci_fa_ex or None,
                        "captureTimeMs": ru.capture_time_ms,
                        "cropsJson": crops_body or None,
                        "secondaryJson": ru.secondary_json or None,
                        "exposureCorrection": ru.exposure_correction,
                        "exposureSubjectStops": ru.exposure_subject_stops,
                        "exposureMeterScale": ru.exposure_meter_scale,
                        "exposurePipeline": ru.exposure_pipeline,
                        "byteLength": int(byte_len),
                        "clientAssetId": _make_client_asset_id(
                            path.name, "export", 0, ru.scene_count, path
                        ),
                    })
                    ordered_keys.append((idx, "export", 0))
                # Crops — use per-crop metadata from crops_json when available
                ci = 0
                while (idx, "crop", ci) in file_map:
                    path, crops_body = file_map[(idx, "crop", ci)]
                    cd = ru.crop_data[ci] if ci < len(ru.crop_data) else {}
                    try:
                        byte_len = path.stat().st_size
                    except OSError:
                        byte_len = 0
                    crop_species = cd.get("species", ru.species)
                    crop_family = cd.get("family", ru.family)
                    sci_sp_cr, sci_fa_cr = self._resolve_scientific(crop_species, crop_family)
                    assets_payload.append({
                        "kind": "crop",
                        "filename": path.name,
                        "contentType": self._content_type(path),
                        "sortInScene": export_sort_in_scene + 1000 + ci,
                        "parentSortInScene": export_sort_in_scene,
                        "quality": cd.get("quality", ru.quality),
                        "species": crop_species,
                        "speciesConfidence": cd.get("species_confidence", ru.species_confidence),
                        "family": crop_family,
                        "familyConfidence": cd.get("family_confidence", ru.family_confidence),
                        "speciesScientific": sci_sp_cr or None,
                        "familyScientific": sci_fa_cr or None,
                        "captureTimeMs": ru.capture_time_ms,
                        "cropsJson": crops_body or None,
                        "secondaryJson": ru.secondary_json or None,
                        "exposureCorrection": cd.get("exposure_correction", ru.exposure_correction),
                        "exposureSubjectStops": cd.get("exposure_subject_stops", ru.exposure_subject_stops),
                        "exposureMeterScale": cd.get("exposure_meter_scale", ru.exposure_meter_scale),
                        "exposurePipeline": cd.get("exposure_pipeline", ru.exposure_pipeline),
                        "byteLength": int(byte_len),
                        "clientAssetId": _make_client_asset_id(
                            path.name, "crop", ci, ru.scene_count, path
                        ),
                    })
                    ordered_keys.append((idx, "crop", ci))
                    ci += 1
                sort_in_scene += 1

            if assets_payload:
                entry: Dict[str, Any] = {
                    "kestrelSceneId": sc,
                    "title": title,
                    "sortIndex": sort_i,
                    "captureTimeMs": cap,
                    "maxQuality": max_q,
                    "assets": assets_payload,
                }
                # Forward user-finalized scene tags from the manual review UI.
                # The finalized flag is sent independently of array contents so
                # the website can distinguish "reviewed but empty" (suppress ML
                # fallback) from "never reviewed" (fall back to ML as before).
                user_tags = sd.get("user_tags") if isinstance(sd, dict) else None
                if isinstance(user_tags, dict) and user_tags.get("finalized") is True:
                    entry["userTagsFinalized"] = True
                    sp = [str(x).strip() for x in (user_tags.get("species") or []) if str(x).strip()]
                    fa = [str(x).strip() for x in (user_tags.get("families") or []) if str(x).strip()]
                    if sp:
                        entry["userTagsSpecies"] = sp
                    if fa:
                        entry["userTagsFamilies"] = fa
                scene_payload.append(entry)

        return scene_payload, ordered_keys

    def _presign_scenes(
        self,
        perch_id: str,
        scene_payload: List[Dict[str, Any]],
        ordered_keys: List[tuple],
        on_chunk: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        """POST scene payload in chunks; returns one record per asset, in order.

        Each record: ``{"file_key": (idx, kind, ci), "client_asset_id": str,
        "asset_id": str, "upload_url": str | None, "committed": bool}``.

        ``committed`` is True for assets the server already has fully uploaded
        (Phase 2 resume case): no R2 PUT is needed. ``upload_url`` will be
        ``None`` for those.

        Retries on 503 / 429 with exponential backoff. Cloudflare's per-isolate
        CPU budget can tighten after sustained heavy use; a brief pause is
        usually enough to clear it without forcing the user to restart the
        whole upload.
        """
        import time as _time
        # Flatten the assets-payload to client_asset_ids in the same order as
        # ordered_keys, so we can match them up against returned records that
        # may not echo clientAssetId.
        client_ids_in_order: List[Optional[str]] = []
        for s in scene_payload:
            for a in s["assets"]:
                client_ids_in_order.append(a.get("clientAssetId"))

        results: List[Dict[str, Any]] = []
        key_offset = 0
        total_chunks = max(1, (len(scene_payload) + self._PRESIGN_SCENE_CHUNK - 1) // self._PRESIGN_SCENE_CHUNK)

        chunk_idx = 0
        for i in range(0, len(scene_payload), self._PRESIGN_SCENE_CHUNK):
            chunk_idx += 1
            chunk = scene_payload[i : i + self._PRESIGN_SCENE_CHUNK]
            chunk_asset_count = sum(len(s["assets"]) for s in chunk)

            if on_chunk is not None:
                try:
                    on_chunk(chunk_idx, total_chunks)
                except Exception:
                    pass

            # Retry up to 4 times on transient throttle (503/429), with backoff.
            backoff_s = 5
            attempts = 4
            r = None
            for attempt in range(1, attempts + 1):
                r = self.s.post(
                    self._url(f"/v1/perches/{perch_id}/assets/presign"),
                    json={"scenes": chunk},
                    timeout=self.timeout,
                )
                if r.status_code not in (503, 429):
                    break
                if attempt >= attempts:
                    break
                # Honor server-provided Retry-After if present, else exponential.
                wait = backoff_s
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        wait = max(wait, int(float(ra)))
                    except (TypeError, ValueError):
                        pass
                print(
                    f"[perch] presign chunk {chunk_idx}/{total_chunks} got "
                    f"HTTP {r.status_code}; retrying in {wait}s (attempt {attempt}/{attempts})"
                )
                _time.sleep(wait)
                backoff_s = min(backoff_s * 2, 60)

            # Stage 7: surface tier-cap denials as a typed exception so the
            # UI can render an upgrade card instead of a stuck "presigning"
            # spinner. Runs BEFORE _raise_for_status so a 413
            # ``asset_too_large`` becomes a clean plan-limit error rather
            # than a generic HTTPError.
            plan_err = PerchPlanLimitExceeded.from_response(r)
            if plan_err is not None:
                raise plan_err
            _raise_for_status(r)  # raise the final non-retried response
            resp = r.json()

            for j, item in enumerate(resp["assets"]):
                fk = ordered_keys[key_offset + j]
                cid = item.get("clientAssetId") or client_ids_in_order[key_offset + j]
                results.append({
                    "file_key": fk,
                    "client_asset_id": cid,
                    "asset_id": item.get("assetId"),
                    "upload_url": item.get("uploadUrl"),
                    "committed": bool(item.get("committed")),
                })
            key_offset += chunk_asset_count

        return results

    def _upload_direct(self, upload_url: str, path: "Path", content_type: str) -> None:
        """PUT file bytes directly to a presigned R2 URL — no auth header needed.

        R2 occasionally returns transient 500 InternalError (and 502/503/504)
        under load even on otherwise-valid PUTs. The presigned URL is good for
        an hour, so retry with exponential backoff. 4xx is not retried — those
        are permanent (expired sig, bad content type, etc.).
        """
        import time as _time
        with open(path, "rb") as fh:
            data = fh.read()
        s = requests.Session()
        attempts = 4
        backoff_s = 2.0
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                r = s.put(
                    upload_url,
                    data=data,
                    headers={"Content-Type": content_type},
                    timeout=self.timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt >= attempts:
                    raise
                print(
                    f"[perch] PUT {path.name} network error: {e}; "
                    f"retrying in {backoff_s:.1f}s (attempt {attempt}/{attempts})"
                )
                _time.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 30.0)
                continue
            if r.status_code < 500 or attempt >= attempts:
                _raise_for_status(r)
                return
            wait = backoff_s
            ra = r.headers.get("Retry-After")
            if ra:
                try:
                    wait = max(wait, float(ra))
                except (TypeError, ValueError):
                    pass
            print(
                f"[perch] PUT {path.name} got HTTP {r.status_code}; "
                f"retrying in {wait:.1f}s (attempt {attempt}/{attempts})"
            )
            _time.sleep(wait)
            backoff_s = min(backoff_s * 2, 30.0)
        # Loop only exits via return or raise above; this is unreachable but
        # keeps mypy/pylint happy if the loop body changes.
        if last_exc is not None:
            raise last_exc

    def _post_file(
        self, perch_id: str, path: Path, kind: str, ru: _RowUpload, crops_json_body: str
    ) -> str:
        return self._post_file_with_session(self.s, perch_id, path, kind, ru, crops_json_body)

    def _post_file_with_session(
        self,
        session: requests.Session,
        perch_id: str,
        path: Path,
        kind: str,
        ru: _RowUpload,
        crops_json_body: str,
    ) -> str:
        with open(path, "rb") as f:
            data = f.read()
        name = path.name
        el = name.lower()
        ct = "image/jpeg"
        if el.endswith(".png"):
            ct = "image/png"
        elif el.endswith(".webp"):
            ct = "image/webp"
        form_data = {
            "kind": kind,
            "filename": name,
            "quality": str(ru.quality) if ru.quality is not None else "",
            "species": ru.species or "",
            "species_confidence": str(ru.species_confidence)
            if ru.species_confidence is not None
            else "",
            "family": ru.family or "",
            "family_confidence": str(ru.family_confidence)
            if ru.family_confidence is not None
            else "",
            "capture_time": str(ru.capture_time_ms) if ru.capture_time_ms is not None else "",
            "crops_json": crops_json_body,
            "secondary_json": ru.secondary_json,
            "exposure_correction": str(ru.exposure_correction)
            if ru.exposure_correction is not None
            else "",
            "exposure_subject_stops": str(ru.exposure_subject_stops)
            if ru.exposure_subject_stops is not None
            else "",
            "exposure_meter_scale": str(ru.exposure_meter_scale)
            if ru.exposure_meter_scale is not None
            else "",
            "exposure_pipeline": ru.exposure_pipeline or "",
        }
        r = session.post(
            self._url(f"/v1/perches/{perch_id}/assets"),
            data=form_data,
            files={"file": (name, data, ct)},
            timeout=self.timeout,
        )
        _raise_for_status(r)
        j = r.json()
        return str(j["id"])

    def _build_manifest(
        self, rows: List[_RowUpload], scenedata: Dict[str, Any]
    ) -> Dict[str, Any]:
        by_scene: Dict[str, List[_RowUpload]] = {}
        for ru in rows:
            by_scene.setdefault(ru.scene_count, []).append(ru)

        scenes_out: List[Dict[str, Any]] = []
        scene_ids = sorted(by_scene.keys(), key=lambda s: (int(s) if s.isdigit() else 0, s))
        for sort_i, sc in enumerate(scene_ids):
            srows = by_scene[sc]
            srows.sort(
                key=lambda x: (x.quality is not None, x.quality or 0.0), reverse=True
            )
            cap: Optional[int] = None
            for x in srows:
                if x.capture_time_ms is not None:
                    if cap is None or x.capture_time_ms < cap:
                        cap = x.capture_time_ms
            max_q: Optional[float] = None
            for x in srows:
                if x.quality is not None:
                    max_q = x.quality if max_q is None else max(max_q, x.quality)
            title = (
                srows[0].scene_name.strip() if srows[0].scene_name else f"Scene {sc}"
            )
            sd = (scenedata.get("scenes") or {}).get(str(sc), {})
            if isinstance(sd, dict) and (sd.get("name") or "").strip():
                title = str(sd.get("name")).strip()

            images = []
            for j, x in enumerate(srows):
                images.append(
                    {
                        "filename": x.filename,
                        "quality": x.quality,
                        "exportAssetId": x.export_asset_id,
                        "cropAssetIds": [aid for aid in x.crop_asset_ids if aid],
                        "sortIndex": j,
                    }
                )
            scenes_out.append(
                {
                    "kestrelSceneId": sc,
                    "title": title,
                    "sortIndex": sort_i,
                    "captureTimeMs": cap,
                    "maxQuality": max_q,
                    "images": images,
                }
            )
        return {"scenes": scenes_out}


def _normalize_crops_json_cell(row: Any, df: Any) -> str:
    """Return compact JSON array/object string for Worker; \"[]\" if missing or invalid."""
    if "crops_json" not in df.columns:
        return "[]"
    raw = row.get("crops_json")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "[]"
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return "[]"
    try:
        parsed = json.loads(s)
        return json.dumps(parsed, default=str, separators=(",", ":"))
    except json.JSONDecodeError:
        return "[]"


def _opt_float_csv(row: Any, col: str) -> Optional[float]:
    if col not in row.index:
        return None
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_str_csv(row: Any, col: str) -> Optional[str]:
    if col not in row.index:
        return None
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    out = str(v).strip()
    return out if out else None


def _parse_capture(row: Any, df: Any) -> Optional[int]:
    for key in ("capture_time", "Capture Time", "capture time"):
        if key in row.index and pd.notna(row.get(key)):  # type: ignore[attr-defined]
            v = row[key]
            if isinstance(v, str):
                m = re.match(
                    r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}:\d{2}:\d{2})",
                    v.strip(),
                )
                if m:
                    from datetime import datetime

                    try:
                        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))  # type: ignore[call-arg]
                        return int(dt.timestamp() * 1000)
                    except Exception:
                        pass
            try:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    x = int(v)
                    if x > 1_000_000_000_000:
                        return x
                    if x > 1_000_000_000:
                        return x * 1000
            except (TypeError, ValueError):
                pass
    return None
