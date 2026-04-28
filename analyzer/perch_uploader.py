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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

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
class _RowUpload:
    filename: str
    scene_count: str
    export_path: Optional[str]
    crop_path: Optional[str]
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
    export_asset_id: Optional[str] = None
    crop_asset_id: Optional[str] = None


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

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(self._auth_headers)
        return s

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def run(
        self,
        session_path: str | os.PathLike[str],
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
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
            rows.append(
                _RowUpload(
                    filename=str(row.get("filename", "")),
                    scene_count=sc,
                    export_path=str(row["export_path"])
                    if "export_path" in df.columns and pd.notna(row.get("export_path"))
                    else None,
                    crop_path=str(row["crop_path"])
                    if "crop_path" in df.columns and pd.notna(row.get("crop_path"))
                    else None,
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
                    crops_json=_normalize_crops_json_cell(row, df),
                    exposure_correction=_opt_float_csv(row, "exposure_correction"),
                    exposure_subject_stops=_opt_float_csv(row, "exposure_subject_stops"),
                    exposure_meter_scale=_opt_float_csv(row, "exposure_meter_scale"),
                    exposure_pipeline=_opt_str_csv(row, "exposure_pipeline"),
                )
            )

        res = self.s.post(
            self._url("/v1/perches"),
            json={"title": title or session_root.name},
            timeout=self.timeout,
        )
        _raise_for_status(res)
        data = res.json()
        perch_id = str(data["id"])
        base_url = str(data.get("url", ""))

        will_export: set[int] = set()
        for idx, ru in enumerate(rows):
            if ru.export_path:
                try:
                    ep = _join_under_session(session_root, ru.export_path)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if ep.is_file():
                    will_export.add(idx)

        upload_tasks: List[tuple[int, str, Path, _RowUpload, str]] = []
        for idx, ru in enumerate(rows):
            crops_full = ru.crops_json if ru.crops_json else "[]"
            if ru.export_path:
                try:
                    ep = _join_under_session(session_root, ru.export_path)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if ep.is_file():
                    upload_tasks.append((idx, "export", ep, ru, crops_full))
            if ru.crop_path:
                try:
                    cp = _join_under_session(session_root, ru.crop_path)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if cp.is_file():
                    crops_body = crops_full if idx not in will_export else ""
                    upload_tasks.append((idx, "crop", cp, ru, crops_body))

        # One authenticated call: insert all D1 rows + get presigned PUT URLs
        presigned = self._batch_presign(perch_id, upload_tasks)

        # Record asset IDs (Worker already wrote D1 rows)
        _task_lookup: dict = {(t[0], t[1]): t[3] for t in upload_tasks}
        for idx, kind, path, asset_id, upload_url, content_type in presigned:
            ru = _task_lookup[(idx, kind)]
            if kind == "export":
                ru.export_asset_id = asset_id
            else:
                ru.crop_asset_id = asset_id

        # Parallel direct-to-R2 PUT — no Worker, no auth header
        MAX_PARALLEL = 8
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
            futures = {
                executor.submit(self._upload_direct, upload_url, path, content_type): (idx, kind)
                for idx, kind, path, asset_id, upload_url, content_type in presigned
            }
            for fut in as_completed(futures):
                fut.result()

        manifest = self._build_manifest(rows, scenedata)
        m = self.s.post(
            self._url(f"/v1/perches/{perch_id}/manifest"),
            json=manifest,
            timeout=self.timeout,
        )
        _raise_for_status(m)

        return {
            "perch_id": perch_id,
            "url": base_url,
            "scene_count": len(manifest.get("scenes", [])),
        }

    def _batch_presign(
        self,
        perch_id: str,
        upload_tasks: List[tuple],
    ) -> List[tuple]:
        """POST all asset metadata in one authenticated call; returns presigned PUT URLs.

        Returns list of (idx, kind, path, asset_id, upload_url, content_type).
        The Worker inserts all D1 rows atomically via batch().
        """
        assets_payload = []
        task_meta: List[tuple] = []  # (idx, kind, path, content_type)

        for idx, kind, path, ru, crops_body in upload_tasks:
            name = path.name
            el = name.lower()
            if el.endswith(".png"):
                ct = "image/png"
            elif el.endswith(".webp"):
                ct = "image/webp"
            else:
                ct = "image/jpeg"
            assets_payload.append({
                "kind": kind,
                "filename": name,
                "contentType": ct,
                "quality": ru.quality,
                "species": ru.species,
                "speciesConfidence": ru.species_confidence,
                "family": ru.family,
                "familyConfidence": ru.family_confidence,
                "captureTimeMs": ru.capture_time_ms,
                "cropsJson": crops_body if crops_body else None,
                "secondaryJson": ru.secondary_json if ru.secondary_json else None,
                "exposureCorrection": ru.exposure_correction,
                "exposureSubjectStops": ru.exposure_subject_stops,
                "exposureMeterScale": ru.exposure_meter_scale,
                "exposurePipeline": ru.exposure_pipeline,
            })
            task_meta.append((idx, kind, path, ct))

        r = self.s.post(
            self._url(f"/v1/perches/{perch_id}/assets/presign"),
            json={"assets": assets_payload},
            timeout=self.timeout,
        )
        _raise_for_status(r)
        resp = r.json()

        results = []
        for i, item in enumerate(resp["assets"]):
            idx, kind, path, ct = task_meta[i]
            results.append((idx, kind, path, item["assetId"], item["uploadUrl"], ct))
        return results

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
                        "cropAssetId": x.crop_asset_id,
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
