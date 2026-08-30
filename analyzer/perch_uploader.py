"""Upload a Kestrel-analyzed session to Perch.

Post-refactor (May 2026): three-step flow.

  1. ``POST /v1/perches`` — create the perch row and an empty R2 manifest.
  2. ``POST /v1/perches/{id}/assets/presign`` — server mints scene/asset IDs
     for the whole nested payload, returns presigned R2 PUT URLs (with
     ``Content-Length`` baked into the signature) and the intended manifest.
  3. ``PUT`` each file directly to R2 in parallel.
  4. ``POST /v1/perches/{id}/commit`` — desktop sends the intended manifest
     back; the server verifies actual R2 object sizes via ``BUCKET.list``,
     writes the manifest, and flips the perch to ``upload_state='complete'``.

No more resumable uploads (the desktop's on-disk
``perch_upload_manifest.json`` machinery is gone), no sync, no per-asset
commits. If the desktop crashes mid-upload, the perch is left in
``upload_state='uploading'`` and gets swept to ``incomplete`` by the
worker's scheduled cron; the user re-uploads from scratch.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None  # type: ignore


# ─── Helpers ─────────────────────────────────────────────────────────────


def _find_kestrel_dir(session_path: str | os.PathLike[str]) -> Path:
    p = Path(session_path).resolve()
    if p.name == ".kestrel" and p.is_dir():
        return p
    kd = p / ".kestrel"
    if kd.is_dir():
        return kd
    raise FileNotFoundError(f"No .kestrel directory under {p}")


def _raise_for_status(r: requests.Response) -> None:
    """Like Response.raise_for_status but include JSON `error` body."""
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


def _content_type(path: Path) -> str:
    el = path.name.lower()
    if el.endswith(".png"):
        return "image/png"
    if el.endswith(".webp"):
        return "image/webp"
    if el.endswith(".avif"):
        return "image/avif"
    return "image/jpeg"


def _opt_float_csv(row, col: str) -> Optional[float]:
    if col not in row.index:
        return None
    v = row.get(col)
    if v is None or (pd is not None and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_str_csv(row, col: str) -> Optional[str]:
    if col not in row.index:
        return None
    v = row.get(col)
    if v is None or (pd is not None and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _parse_capture(row, df) -> Optional[int]:
    """Parse a CSV row's capture-time cell into Unix-ms.

    Kestrel writes ISO-8601 strings (`2025-05-22T15:30:00` or `... +00:00`),
    which is the dominant case. The numeric branches handle older fixtures
    and any direct-from-EXIF integer columns.
    """
    for key in ("capture_time", "Capture Time", "capture time",
                "capture_time_ms", "captureTimeMs"):
        if key not in row.index:
            continue
        v = row.get(key)
        if v is None or (pd is not None and pd.isna(v)):
            continue
        # ISO-8601 string (most common shape from the Kestrel CSV).
        if isinstance(v, str):
            s = v.strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}:\d{2}", s):
                try:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    return int(dt.timestamp() * 1000)
                except (ValueError, OSError):
                    pass
            # Numeric-as-string fallback (e.g. "1734789123000").
            try:
                num = int(float(s))
            except (TypeError, ValueError):
                continue
            if num > 1_000_000_000_000:
                return num
            if num > 1_000_000_000:
                return num * 1000
            continue
        # Numeric cell — detect ms vs s by magnitude.
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                num = int(v)
            except (TypeError, ValueError):
                continue
            if num > 1_000_000_000_000:
                return num
            if num > 1_000_000_000:
                return num * 1000
    return None


def _normalize_crops_json_cell(row, df) -> str:
    if "crops_json" not in df.columns:
        return "[]"
    v = row.get("crops_json")
    if v is None or (pd is not None and pd.isna(v)):
        return "[]"
    s = str(v).strip()
    return s or "[]"


def _clamp_norm(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


# ─── Exceptions surfaced to the bridge layer ─────────────────────────────


class PerchLegalAcceptanceRequired(Exception):
    """403 ``legal_acceptance_required``: ToS / Privacy Policy updated.
    api_bridge opens ``accept_url`` in the system browser."""
    def __init__(self, accept_url: str | None, current_effective_date: str | None, message: str):
        super().__init__(message or "Updated terms must be reviewed before uploading.")
        self.accept_url = accept_url or "https://myaccount.projectkestrel.org/legal/accept"
        self.current_effective_date = current_effective_date


_PERCH_PLAN_LIMIT_ERROR_CODES = frozenset({
    "perch_limit_reached",
    "perch_storage_limit_reached",
    "perch_image_limit_reached",
    "perch_asset_limit_reached",
    "user_storage_limit_reached",
    "user_image_limit_reached",
    "user_asset_limit_reached",
    "asset_too_large",
})


class PerchPlanLimitExceeded(Exception):
    """Plan-tier cap denial (HTTP 403/413). api_bridge translates into a
    progress payload that the JS UI surfaces as an upgrade card."""
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


# ─── Preflight (no network) ──────────────────────────────────────────────


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
    thumbnail_rel: Optional[str] = None
    reviewed: bool = False
    rejected_skipped: int = 0
    species: List[str] = field(default_factory=list)
    families: List[str] = field(default_factory=list)


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
    rejected_skipped: int = 0


def project_expected_after_exclusion(
    preflight: Optional[PerchPreflight],
    excluded_scene_ids: Iterable[str],
) -> Dict[str, int]:
    """Compute the `expected` POST body for /v1/perches given a preflight + scene exclusions."""
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


# ─── Per-row in-memory model ─────────────────────────────────────────────


@dataclass
class _CropEntry:
    """One detected crop on an export, parsed from `crops_json[i]`."""
    crop_index: int
    bbox: dict  # {xMinNorm, yMinNorm, xMaxNorm, yMaxNorm}
    quality: Optional[float] = None
    exposure_correction: Optional[float] = None
    crop_path: Optional[str] = None  # session-relative path to the crop file


@dataclass
class _Row:
    """A CSV row after parsing — one export + 0..N crops."""
    filename: str
    scene_count: str
    export_path: Optional[str]
    quality: Optional[float]
    capture_time_ms: Optional[int]
    scene_name: str
    crops: List[_CropEntry] = field(default_factory=list)


def _parse_crops(row, df, ru_quality: Optional[float], ru_exp_corr: Optional[float]) -> List[_CropEntry]:
    """Parse the row's `crops_json` cell into _CropEntry list (bbox + per-crop quality/exposure)."""
    crops_raw = _normalize_crops_json_cell(row, df)
    out: List[_CropEntry] = []
    try:
        parsed = json.loads(crops_raw) if crops_raw and crops_raw != "[]" else []
    except (json.JSONDecodeError, TypeError):
        return out
    if not isinstance(parsed, list):
        return out
    for i, c in enumerate(parsed):
        if not isinstance(c, dict):
            continue
        crop_path = c.get("crop_path") if isinstance(c.get("crop_path"), str) else None
        bbox_in = c.get("bbox") if isinstance(c.get("bbox"), dict) else {}
        bbox = {
            "xMinNorm": _clamp_norm(bbox_in.get("x_min_norm")),
            "yMinNorm": _clamp_norm(bbox_in.get("y_min_norm")),
            "xMaxNorm": _clamp_norm(bbox_in.get("x_max_norm", 1.0)),
            "yMaxNorm": _clamp_norm(bbox_in.get("y_max_norm", 1.0)),
        }
        # Clamp degenerate boxes to a sane fallback.
        if bbox["xMinNorm"] >= bbox["xMaxNorm"]:
            bbox["xMinNorm"], bbox["xMaxNorm"] = 0.0, 1.0
        if bbox["yMinNorm"] >= bbox["yMaxNorm"]:
            bbox["yMinNorm"], bbox["yMaxNorm"] = 0.0, 1.0

        q_raw = c.get("quality", ru_quality)
        try:
            q = float(q_raw) if q_raw is not None else None
        except (TypeError, ValueError):
            q = None
        if q is not None and q < 0:
            q = None  # -1 sentinel: "no bird detected, quality unknown"

        ec_raw = c.get("exposure_correction", ru_exp_corr)
        try:
            ec = float(ec_raw) if ec_raw is not None else None
        except (TypeError, ValueError):
            ec = None

        out.append(_CropEntry(
            crop_index=int(c.get("crop_index", i)) if isinstance(c.get("crop_index"), (int, float)) else i,
            bbox=bbox,
            quality=q,
            exposure_correction=ec,
            crop_path=crop_path,
        ))
    return out


# ─── Uploader ────────────────────────────────────────────────────────────


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
        self._cached_rows: List[_Row] = []
        self._cached_scenedata: Dict[str, Any] = {}
        self._cached_preflight: Optional[PerchPreflight] = None
        self._cached_skip_rejected: Optional[bool] = None

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(self._auth_headers)
        return s

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    # ── Preflight ──────────────────────────────────────────────────────

    def preflight(
        self,
        session_path: str | os.PathLike[str],
        skip_rejected: bool = True,
    ) -> PerchPreflight:
        """Parse the session's CSV/scenedata, resolve file paths, sum byte sizes."""
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
        rows: List[_Row] = []
        rejected_skipped = 0
        rejected_per_scene: Dict[str, int] = {}

        for _, row in df.iterrows():
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

            ru_quality = _opt_float_csv(row, "quality")
            ru_exp_corr = _opt_float_csv(row, "exposure_correction")
            crops = _parse_crops(row, df, ru_quality, ru_exp_corr)

            rows.append(_Row(
                filename=str(row.get("filename", "")),
                scene_count=sc,
                export_path=str(row["export_path"])
                if "export_path" in df.columns and pd.notna(row.get("export_path"))
                else None,
                quality=ru_quality,
                capture_time_ms=_parse_capture(row, df),
                scene_name=str(row.get("scene_name", "") or "")
                if "scene_name" in df.columns else "",
                crops=crops,
            ))

        # Build a file-existence map so the preflight only counts files actually present.
        will_export: Dict[int, Path] = {}
        crop_files: Dict[tuple, Path] = {}  # (row_idx, crop_index) → Path
        for idx, ru in enumerate(rows):
            if ru.export_path:
                try:
                    ep = _join_under_session(session_root, ru.export_path)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if ep.is_file():
                    will_export[idx] = ep
            for c in ru.crops:
                if not c.crop_path:
                    continue
                try:
                    cp = _join_under_session(session_root, c.crop_path)
                except ValueError as e:
                    raise FileNotFoundError(str(e)) from e
                if cp.is_file():
                    crop_files[(idx, c.crop_index)] = cp

        # Per-scene aggregation.
        per_scene: Dict[str, Dict[str, Any]] = {}
        for idx, ru in enumerate(rows):
            sid = ru.scene_count
            bucket = per_scene.setdefault(
                sid,
                {
                    "scene_id": sid,
                    "title_candidates": [],
                    "capture_time_ms": None,
                    "image_count": 0,
                    "export_count": 0,
                    "crop_count": 0,
                    "total_bytes": 0,
                    "top_quality": None,
                    "thumbnail_rel": None,
                    "thumbnail_quality": None,
                    "row_indices": set(),
                },
            )
            bucket["row_indices"].add(idx)
            if ru.scene_name:
                bucket["title_candidates"].append(ru.scene_name.strip())
            if ru.capture_time_ms is not None:
                cur = bucket["capture_time_ms"]
                if cur is None or ru.capture_time_ms < cur:
                    bucket["capture_time_ms"] = ru.capture_time_ms

            ep = will_export.get(idx)
            if ep is not None:
                try:
                    bucket["total_bytes"] += int(ep.stat().st_size)
                except OSError:
                    pass
                bucket["export_count"] += 1
                cur_thumb_q = bucket["thumbnail_quality"]
                if bucket["thumbnail_rel"] is None:
                    bucket["thumbnail_rel"] = ru.export_path
                    bucket["thumbnail_quality"] = ru.quality
                elif ru.quality is not None and (cur_thumb_q is None or ru.quality > cur_thumb_q):
                    bucket["thumbnail_rel"] = ru.export_path
                    bucket["thumbnail_quality"] = ru.quality
            for c in ru.crops:
                cp = crop_files.get((idx, c.crop_index))
                if cp is None:
                    continue
                try:
                    bucket["total_bytes"] += int(cp.stat().st_size)
                except OSError:
                    pass
                bucket["crop_count"] += 1
                if c.quality is not None:
                    cur_q = bucket["top_quality"]
                    bucket["top_quality"] = c.quality if cur_q is None else max(cur_q, c.quality)
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
        file_count = export_count + crop_count

        preflight = PerchPreflight(
            scene_count=len(scenes),
            image_count=image_count,
            export_count=export_count,
            crop_count=crop_count,
            total_bytes=total_bytes,
            file_count=file_count,
            scenes=scenes,
            rejected_skipped=rejected_skipped,
        )

        # Cache for run().
        self._preflighted_root = session_root
        self._cached_rows = rows
        self._cached_scenedata = scenedata
        self._cached_skip_rejected = skip_rejected
        self._cached_preflight = preflight
        return preflight

    # ── Upload (network) ───────────────────────────────────────────────

    def run(
        self,
        session_path: str | os.PathLike[str],
        title: Optional[str] = None,
        excluded_scene_ids: Iterable[str] = (),
        progress_callback: Optional[Callable[[dict], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        skip_rejected: bool = True,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_root = Path(session_path).resolve()

        def emit(payload: dict) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(payload)
            except Exception:
                pass

        # Reuse cached preflight if available and matching.
        cached_skip = getattr(self, "_cached_skip_rejected", None)
        if (
            self._preflighted_root != session_root
            or self._cached_preflight is None
            or cached_skip != skip_rejected
        ):
            self.preflight(session_root, skip_rejected=skip_rejected)
        rows = self._cached_rows
        scenedata = self._cached_scenedata

        excluded = {str(s) for s in (excluded_scene_ids or ())}
        if excluded:
            rows = [ru for ru in rows if str(ru.scene_count) not in excluded]
        if not rows:
            raise RuntimeError("No assets selected for upload")

        # ── Step 1: create perch (or reuse via idempotency key) ──────────
        idemp_key = idempotency_key or str(uuid.uuid4())
        emit({"phase": "creating_perch"})

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
        # ToS / Privacy Policy gate.
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
        plan_err = PerchPlanLimitExceeded.from_response(res)
        if plan_err is not None:
            raise plan_err
        _raise_for_status(res)
        data = res.json()
        perch_id = str(data["id"])
        base_url = str(data.get("url", ""))

        # ── Step 2: build presign payload + call presign ─────────────────
        presign_body, file_index = self._build_presign_payload(rows, scenedata, session_root)
        emit({"phase": "presigning", "current": 0, "total": 1})

        presign_res = self.s.post(
            self._url(f"/v1/perches/{perch_id}/assets/presign"),
            json=presign_body,
            timeout=self.timeout,
        )
        plan_err = PerchPlanLimitExceeded.from_response(presign_res)
        if plan_err is not None:
            raise plan_err
        _raise_for_status(presign_res)
        presign_json = presign_res.json()
        uploads: List[Dict[str, Any]] = list(presign_json.get("uploads") or [])
        intended_manifest = presign_json.get("intendedManifest")
        if intended_manifest is None or len(uploads) != len(file_index):
            raise RuntimeError(
                f"Presign response shape mismatch: uploads={len(uploads)} "
                f"local files={len(file_index)}"
            )

        # ── Step 3: parallel R2 PUTs ─────────────────────────────────────
        total = len(uploads)
        completed_count = 0
        lock = threading.Lock()
        canceled = False

        def _do_put(i: int) -> Optional[str]:
            nonlocal completed_count
            if cancel_event is not None and cancel_event.is_set():
                return None
            u = uploads[i]
            path = file_index[i]
            content_type = u.get("contentType") or _content_type(path)
            with open(path, "rb") as fh:
                body_bytes = fh.read()
            r = requests.put(
                u["uploadUrl"],
                data=body_bytes,
                headers={
                    "Content-Type": content_type,
                    "Content-Length": str(len(body_bytes)),
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            with lock:
                completed_count += 1
                fname = path.name
                print(f"[perch] {completed_count}/{total} uploaded — {fname}")
            emit({
                "phase": "uploading",
                "uploaded": completed_count,
                "total": total,
                "filename": path.name,
            })
            return path.name

        MAX_PARALLEL = 48
        try:
            with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
                futures = [executor.submit(_do_put, i) for i in range(len(uploads))]
                for fut in as_completed(futures):
                    if cancel_event is not None and cancel_event.is_set() and not canceled:
                        for pending in futures:
                            pending.cancel()
                        canceled = True
                    try:
                        fut.result()
                    except CancelledError:
                        pass
                    except Exception:
                        if not canceled:
                            raise
        except Exception:
            raise

        if canceled:
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
                "canceled": True,
                "uploaded": completed_count,
                "total": total,
            }

        # ── Step 4: commit ───────────────────────────────────────────────
        emit({"phase": "committing"})
        commit_res = self.s.post(
            self._url(f"/v1/perches/{perch_id}/commit"),
            json=intended_manifest,
            timeout=self.timeout,
        )
        plan_err = PerchPlanLimitExceeded.from_response(commit_res)
        if plan_err is not None:
            raise plan_err
        _raise_for_status(commit_res)
        commit_json = commit_res.json()

        scene_count = len(intended_manifest.get("scenes") or [])
        emit({"phase": "done", "perch_url": base_url, "perch_id": perch_id})
        return {
            "perch_id": perch_id,
            "url": str(commit_json.get("url") or base_url),
            "scene_count": scene_count,
            "idempotency_key": idemp_key,
            "manifest_etag": commit_json.get("manifestEtag"),
        }

    # ── Presign payload builder ────────────────────────────────────────

    def _build_presign_payload(
        self,
        rows: List[_Row],
        scenedata: Dict[str, Any],
        session_root: Path,
    ) -> tuple[Dict[str, Any], List[Path]]:
        """Group rows into scenes and build the nested presign payload.

        Returns (body, file_index). `file_index[i]` is the local Path that
        corresponds to `uploads[i]` in the presign response (same order).
        Crop-only rows (no export file) are silently dropped — the manifest
        schema requires every scene to have ≥1 export.
        """
        by_scene: Dict[str, List[_Row]] = {}
        for ru in rows:
            by_scene.setdefault(ru.scene_count, []).append(ru)
        scene_ids = sorted(by_scene.keys(), key=lambda s: (int(s) if s.isdigit() else 1 << 30, s))

        scenes_out: List[Dict[str, Any]] = []
        file_index: List[Path] = []

        for sc in scene_ids:
            srows = by_scene[sc]
            sd = (scenedata.get("scenes") or {}).get(str(sc), {})
            user_tags = sd.get("user_tags") if isinstance(sd, dict) else None
            user_tags = user_tags if isinstance(user_tags, dict) else {}
            species_list = [str(x).strip() for x in (user_tags.get("species") or []) if str(x).strip()] or None
            family_list = [str(x).strip() for x in (user_tags.get("families") or []) if str(x).strip()] or None
            user_tags_finalized = bool(user_tags.get("finalized") is True)

            # Earliest capture time within the scene.
            cap_times = [ru.capture_time_ms for ru in srows if ru.capture_time_ms is not None]
            scene_cap = min(cap_times) if cap_times else None

            exports_out: List[Dict[str, Any]] = []
            for ru in srows:
                if not ru.export_path:
                    continue  # crop-only row; skip
                try:
                    exp_path = _join_under_session(session_root, ru.export_path)
                except ValueError:
                    continue
                if not exp_path.is_file():
                    continue
                try:
                    exp_bytes = exp_path.stat().st_size
                except OSError:
                    continue

                crops_out: List[Dict[str, Any]] = []
                for c in ru.crops:
                    if not c.crop_path:
                        continue
                    try:
                        cp = _join_under_session(session_root, c.crop_path)
                    except ValueError:
                        continue
                    if not cp.is_file():
                        continue
                    try:
                        c_bytes = cp.stat().st_size
                    except OSError:
                        continue
                    crops_out.append({
                        "filename": cp.name,
                        "contentType": _content_type(cp),
                        "byteLength": int(c_bytes),
                        "quality": c.quality,
                        "exposureCorrection": c.exposure_correction,
                        "bbox": c.bbox,
                    })
                    file_index.append(cp)

                exports_out.append({
                    "filename": exp_path.name,
                    "contentType": _content_type(exp_path),
                    "byteLength": int(exp_bytes),
                    "captureTimeMs": ru.capture_time_ms,
                    "crops": crops_out,
                })
                # The export file goes BEFORE its crops in the upload index
                # (so the order matches how the server builds presigned URLs).
                # We push the export path AFTER pushing its crops above, then
                # rotate so the export is first in this chunk:
                file_index.insert(len(file_index) - len(crops_out), exp_path)

            if not exports_out:
                continue  # scene with no usable exports — skip entirely

            scenes_out.append({
                "kestrelSceneId": sc,
                "captureTimeMs": scene_cap,
                "speciesList": species_list,
                "familyList": family_list,
                "userTagsFinalized": user_tags_finalized,
                "exports": exports_out,
            })

        return {"scenes": scenes_out}, file_index
