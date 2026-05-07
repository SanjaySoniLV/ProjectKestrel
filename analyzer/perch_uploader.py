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

import json
import os
import re
import threading
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
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


def _join_under_session(session_root: Path, rel: str) -> Path:
    rel = _norm_rel(rel)
    if not rel or rel == ".":
        raise ValueError("Empty path")
    candidate = (session_root / rel).resolve()
    root = session_root.resolve()
    if not str(candidate).startswith(str(root)):
        raise ValueError(f"Path escapes session root: {rel}")
    return candidate


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

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(self._auth_headers)
        return s

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    # ─── Preflight (no network) ─────────────────────────────────────────

    def preflight(self, session_path: str | os.PathLike[str]) -> PerchPreflight:
        """Parse the session's CSV/scenedata, resolve file paths, sum byte sizes.

        No network calls. Caches state on `self` so a subsequent `run()` against
        the same session can skip the work.
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
        for _, row in df.iterrows():
            sc_val = row.get("scene_count", 0) if "scene_count" in df.columns else 0
            try:
                sc = str(int(float(sc_val)))
            except (TypeError, ValueError):
                sc = str(sc_val) if sc_val is not None else "0"
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
        )

        # Cache for run().
        self._preflighted_root = session_root
        self._cached_rows = rows
        self._cached_file_map = file_map
        self._cached_scenedata = scenedata
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
        if self._preflighted_root != session_root or self._cached_preflight is None:
            self.preflight(session_root)
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

        emit({"phase": "creating_perch"})
        res = self.s.post(
            self._url("/v1/perches"),
            json={"title": title or session_root.name},
            timeout=self.timeout,
        )
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

        upload_items = [
            (file_key, file_map[file_key][0], upload_url, self._content_type(file_map[file_key][0]))
            for file_key, upload_url in presign_results
        ]

        total = len(upload_items)
        print(f"[perch] Starting upload of {total} files to R2...")
        completed_count = 0
        lock = threading.Lock()
        canceled = False

        def _do_upload(item: tuple) -> Optional[str]:
            nonlocal completed_count
            file_key, path, upload_url, content_type = item
            if cancel_event is not None and cancel_event.is_set():
                return None
            self._upload_direct(upload_url, path, content_type)
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

        if canceled:
            print(f"[perch] Upload canceled at {completed_count}/{total} files.")
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
        emit({"phase": "done", "perch_url": base_url, "perch_id": perch_id})
        return {
            "perch_id": perch_id,
            "url": base_url,
            "scene_count": scene_count,
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
                        "captureTimeMs": ru.capture_time_ms,
                        "cropsJson": crops_body or None,
                        "secondaryJson": ru.secondary_json or None,
                        "exposureCorrection": ru.exposure_correction,
                        "exposureSubjectStops": ru.exposure_subject_stops,
                        "exposureMeterScale": ru.exposure_meter_scale,
                        "exposurePipeline": ru.exposure_pipeline,
                        "byteLength": int(byte_len),
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
                    assets_payload.append({
                        "kind": "crop",
                        "filename": path.name,
                        "contentType": self._content_type(path),
                        "sortInScene": export_sort_in_scene + 1000 + ci,
                        "parentSortInScene": export_sort_in_scene,
                        "quality": cd.get("quality", ru.quality),
                        "species": cd.get("species", ru.species),
                        "speciesConfidence": cd.get("species_confidence", ru.species_confidence),
                        "family": cd.get("family", ru.family),
                        "familyConfidence": cd.get("family_confidence", ru.family_confidence),
                        "captureTimeMs": ru.capture_time_ms,
                        "cropsJson": crops_body or None,
                        "secondaryJson": ru.secondary_json or None,
                        "exposureCorrection": cd.get("exposure_correction", ru.exposure_correction),
                        "exposureSubjectStops": cd.get("exposure_subject_stops", ru.exposure_subject_stops),
                        "exposureMeterScale": cd.get("exposure_meter_scale", ru.exposure_meter_scale),
                        "exposurePipeline": cd.get("exposure_pipeline", ru.exposure_pipeline),
                        "byteLength": int(byte_len),
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
                # Only sent when finalized=True to avoid clobbering ML-derived
                # per-asset species/family with empty review state.
                user_tags = sd.get("user_tags") if isinstance(sd, dict) else None
                if isinstance(user_tags, dict) and user_tags.get("finalized") is True:
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
    ) -> List[tuple]:
        """POST scene payload in chunks; returns (file_key, upload_url) pairs in order.

        Retries on 503 / 429 with exponential backoff. Cloudflare's per-isolate
        CPU budget can tighten after sustained heavy use; a brief pause is
        usually enough to clear it without forcing the user to restart the
        whole upload.
        """
        import time as _time
        upload_urls: List[tuple] = []
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

            _raise_for_status(r)  # raise the final non-retried response
            resp = r.json()

            for j, item in enumerate(resp["assets"]):
                upload_urls.append((ordered_keys[key_offset + j], item["uploadUrl"]))
            key_offset += chunk_asset_count

        return upload_urls

    def _upload_direct(self, upload_url: str, path: "Path", content_type: str) -> None:
        """PUT file bytes directly to a presigned R2 URL — no auth header needed."""
        with open(path, "rb") as fh:
            data = fh.read()
        s = requests.Session()
        r = s.put(
            upload_url,
            data=data,
            headers={"Content-Type": content_type},
            timeout=self.timeout,
        )
        _raise_for_status(r)

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
