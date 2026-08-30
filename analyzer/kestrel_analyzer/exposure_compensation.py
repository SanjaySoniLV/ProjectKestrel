from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# sRGB gamma LUT  (65 536 entries → uint8)
# ---------------------------------------------------------------------------

_SRGB_LUT_U8: Optional[np.ndarray] = None


def _build_srgb_lut() -> np.ndarray:
    x = np.linspace(0.0, 1.0, 65536, dtype=np.float64)
    srgb = np.where(
        x <= 0.0031308,
        x * 12.92,
        1.055 * np.power(np.maximum(x, 1e-12), 1.0 / 2.4) - 0.055,
    )
    return (np.clip(srgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _get_srgb_lut() -> np.ndarray:
    global _SRGB_LUT_U8
    if _SRGB_LUT_U8 is None:
        _SRGB_LUT_U8 = _build_srgb_lut()
    return _SRGB_LUT_U8


def linear_to_srgb_u8(linear: np.ndarray) -> np.ndarray:
    """Convert linear float32 [0,1] array to sRGB uint8 via LUT (~3 ms / 1 MP)."""
    lut = _get_srgb_lut()
    idx = np.clip((linear * 65535.0 + 0.5).astype(np.int32), 0, 65535).astype(np.uint16)
    return lut[idx]


# ---------------------------------------------------------------------------
# Exposure quality profiles
# ---------------------------------------------------------------------------

_ALLOWED_QUALITY = {"lenient", "balanced", "aggressive"}

_EXPOSURE_QUALITY_PROFILES: Dict[str, Dict[str, float]] = {
    "aggressive": {
        "TARGET_HI_P95": 0.76,
        "TARGET_HI_P98": 0.84,
        "TARGET_SHADOW_P10": 0.19,
        "CLIP_THRESH": 0.965,
        "MAX_CLIP_RATIO": 0.008,
        "BRIGHTEN_STRENGTH": 1.00,
        "DARKEN_STRENGTH": 1.00,
        "MAX_BRIGHTEN": 8.0,
        "MAX_DARKEN": -3.0,
    },
    "balanced": {
        "TARGET_HI_P95": 0.76,
        "TARGET_HI_P98": 0.84,
        "TARGET_SHADOW_P10": 0.19,
        "CLIP_THRESH": 0.965,
        "MAX_CLIP_RATIO": 0.008,
        "BRIGHTEN_STRENGTH": 0.75,
        "DARKEN_STRENGTH": 0.75,
        "MAX_BRIGHTEN": 5.0,
        "MAX_DARKEN": -2.0,
    },
    "lenient": {
        "TARGET_HI_P95": 0.76,
        "TARGET_HI_P98": 0.84,
        "TARGET_SHADOW_P10": 0.19,
        "CLIP_THRESH": 0.965,
        "MAX_CLIP_RATIO": 0.008,
        "BRIGHTEN_STRENGTH": 0.50,
        "DARKEN_STRENGTH": 0.50,
        "MAX_BRIGHTEN": 3.0,
        "MAX_DARKEN": -1.5,
    },
}


def normalize_quality_name(quality: str) -> str:
    q = str(quality or "balanced").strip().lower()
    if q not in _ALLOWED_QUALITY:
        q = "balanced"
    return q


# ---------------------------------------------------------------------------
# Global meter scale  (used for detection image)
# ---------------------------------------------------------------------------

def compute_global_meter_scale(
    noauto_linear_rgb: np.ndarray,
) -> Tuple[float, Dict[str, float]]:
    """Compute a global brightness scale so the scene has reasonable exposure.

    Input must be truly linear float32 [0, 1] data (gamma=(1,1) decode).
    Returns (meter_scale, debug_dict).
    """
    lum = (
        0.2126 * noauto_linear_rgb[..., 0]
        + 0.7152 * noauto_linear_rgb[..., 1]
        + 0.0722 * noauto_linear_rgb[..., 2]
    )
    p50, p90, p98 = [float(x) for x in np.percentile(lum, [50, 90, 98])]
    eps = 1e-4
    # Fixed targets — we just want a reasonable scene exposure for detection
    target_p50, target_p90, target_p98 = 0.33, 0.72, 0.90
    g50 = target_p50 / max(p50, eps)
    g90 = target_p90 / max(p90, eps)
    g98 = target_p98 / max(p98, eps)
    meter_scale = float(np.clip(min(g50, g90, g98), 0.25, 8.0))
    return meter_scale, {
        "p50": p50, "p90": p90, "p98": p98,
        "g50": float(g50), "g90": float(g90), "g98": float(g98),
        "meter_scale": meter_scale,
    }


# ---------------------------------------------------------------------------
# RAW decode + metered detection image
# ---------------------------------------------------------------------------

def build_metered_detection_image(
    raw_obj,
) -> Tuple[Optional[np.ndarray], float, Dict[str, Any], Optional[np.ndarray]]:
    """Decode RAW once, returning:
      (metered8, meter_scale, meter_debug, noauto_linear)

    - metered8:      uint8 RGB preview for detection / display
    - meter_scale:   global brightness scale factor
    - meter_debug:   diagnostic dict
    - noauto_linear: float32 [0,1] truly-linear full-res array; reused for all
                     per-bird numpy corrections — no second rawpy call needed
    """
    def _decode(use_camera_wb: bool) -> np.ndarray:
        kwargs: Dict[str, Any] = {
            "gamma": (1, 1),        # truly linear — no gamma curve applied
            "no_auto_bright": True, # no auto-brightness compression
            "output_bps": 16,       # full dynamic range
        }
        if use_camera_wb:
            kwargs["use_camera_wb"] = True  # camera-metered WB from EXIF (fixes hazy look)
        return raw_obj.postprocess(**kwargs)

    try:
        try:
            noauto16 = _decode(use_camera_wb=True)
        except Exception:
            # Some RAW files (manual WB, certain bodies) have no stored camera WB
            # coefficients — fall back to rawpy's default daylight WB.
            noauto16 = _decode(use_camera_wb=False)
        noauto_linear = np.clip(noauto16.astype(np.float32) / 65535.0, 0.0, 1.0)
        meter_scale, meter_debug = compute_global_meter_scale(noauto_linear)
        metered_linear = np.clip(noauto_linear * meter_scale, 0.0, 1.0)
        metered8 = linear_to_srgb_u8(metered_linear)
        return metered8, meter_scale, meter_debug, noauto_linear
    except Exception as exc:
        return None, 1.0, {"error": str(exc), "meter_scale": 1.0}, None


# ---------------------------------------------------------------------------
# Per-bird numpy solver
# ---------------------------------------------------------------------------

_MAX_SOLVER_PX = 384  # downsample crop to this size for fast solver iterations

# Clip-ratio statistics use only pixels at least this many px inside the mask boundary
# (L2 distanceTransform). Percentile targets still use the full mask. ``None`` = legacy
# behavior (clip evaluated on entire mask, including edge/sky leakage).
DEFAULT_CLIP_IGNORE_EDGE_PX = 2.0


def _core_mask_for_clip(mask_bool: np.ndarray, clip_ignore_edge_px: float | None) -> np.ndarray:
    """Pixels at least *clip_ignore_edge_px* away from the mask boundary (L2 distance).

    Used only for **clip ratio** statistics so bright boundary/sky leakage deprioritizes
    less than the interior. Percentile targets still use the full mask.

    If *clip_ignore_edge_px* is None or <= 0, returns *mask_bool* unchanged.
    If erosion would remove all pixels, falls back to *mask_bool*.
    """
    if clip_ignore_edge_px is None or float(clip_ignore_edge_px) <= 0:
        return mask_bool

    m = (mask_bool.astype(np.uint8) * 255)
    if not m.any():
        return mask_bool
    dist = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    core = (dist >= float(clip_ignore_edge_px)) & mask_bool
    if not core.any():
        return mask_bool
    return core


def compute_stops_numpy_solver(
    noauto_linear: np.ndarray,
    mask: np.ndarray,
    meter_scale: float,
    quality: str = "balanced",
    clip_ignore_edge_px: float | None = DEFAULT_CLIP_IGNORE_EDGE_PX,
) -> float:
    """Compute per-bird exposure correction stops using numpy only.

    Algorithm:
    1. Extract the bird crop from noauto_linear, downsample to ≤384px for speed.
    2. Iteratively find the stop value that meets the brightness targets
       (all iterations at full solver strength — no dampening during loop).
    3. Apply BRIGHTEN_STRENGTH / DARKEN_STRENGTH and stop limits once,
       post-convergence.

    clip_ignore_edge_px:
        Defaults to :data:`DEFAULT_CLIP_IGNORE_EDGE_PX` (2px): highlight **clip ratio**
        uses only mask pixels at least this far inside the boundary (``cv2.distanceTransform``),
        reducing bias from bright sky at the segmentation edge. **Percentile** targets (p10/p95/p98)
        still use the full mask.
        Pass ``None`` or ``<= 0`` for legacy behavior: clip ratio on the full mask.

    Returns stops (float) to apply via apply_exposure_crop_numpy.
    """
    EPS = 1e-4
    quality_name = normalize_quality_name(quality)
    cfg = _EXPOSURE_QUALITY_PROFILES[quality_name]

    TARGET_HI_P95    = float(cfg["TARGET_HI_P95"])
    TARGET_HI_P98    = float(cfg["TARGET_HI_P98"])
    TARGET_SHADOW_P10 = float(cfg["TARGET_SHADOW_P10"])
    CLIP_THRESH      = float(cfg["CLIP_THRESH"])
    MAX_CLIP_RATIO   = float(cfg["MAX_CLIP_RATIO"])
    BRIGHTEN_STRENGTH = float(cfg["BRIGHTEN_STRENGTH"])
    DARKEN_STRENGTH  = float(cfg["DARKEN_STRENGTH"])
    MAX_BRIGHTEN     = float(cfg["MAX_BRIGHTEN"])
    MAX_DARKEN       = float(cfg["MAX_DARKEN"])

    try:
        # --- build mask bool ---
        mask_bool = np.asarray(mask, dtype=bool)
        if not mask_bool.any():
            return 0.0

        # --- extract crop bounding box ---
        rows = np.any(mask_bool, axis=1)
        cols = np.any(mask_bool, axis=0)
        r0, r1 = int(np.argmax(rows)), int(len(rows) - 1 - np.argmax(rows[::-1]))
        c0, c1 = int(np.argmax(cols)), int(len(cols) - 1 - np.argmax(cols[::-1]))
        r1 += 1
        c1 += 1

        crop_linear = noauto_linear[r0:r1, c0:c1]
        crop_mask   = mask_bool[r0:r1, c0:c1]

        # --- downsample for solver iterations ---
        ch, cw = crop_linear.shape[:2]
        if max(ch, cw) > _MAX_SOLVER_PX:
            scale_factor = _MAX_SOLVER_PX / max(ch, cw)
            new_h = max(1, int(ch * scale_factor))
            new_w = max(1, int(cw * scale_factor))
            crop_small = cv2.resize(crop_linear, (new_w, new_h), interpolation=cv2.INTER_AREA)
            mask_small = cv2.resize(
                crop_mask.astype(np.uint8), (new_w, new_h), interpolation=cv2.INTER_NEAREST
            ).astype(bool)
        else:
            crop_small = crop_linear
            mask_small = crop_mask

        if not mask_small.any():
            mask_small = np.ones(crop_small.shape[:2], dtype=bool)

        mask_clip = _core_mask_for_clip(mask_small, clip_ignore_edge_px)

        def _lum_pixels(arr: np.ndarray, msk: np.ndarray) -> np.ndarray:
            px = arr[msk]
            lum = 0.2126 * px[:, 0] + 0.7152 * px[:, 1] + 0.0722 * px[:, 2]
            return lum[np.isfinite(lum)]

        def _clip_ratio(lum_clip_1d: np.ndarray, stops_val: float) -> float:
            return float(np.mean((lum_clip_1d * (2.0 ** stops_val)) >= CLIP_THRESH))

        # base luminance at meter_scale — full mask for percentiles, core (optional) for clip
        base_lum = _lum_pixels(crop_small * meter_scale, mask_small)
        if base_lum.size == 0:
            return 0.0
        base_lum_clip = _lum_pixels(crop_small * meter_scale, mask_clip)
        if base_lum_clip.size == 0:
            base_lum_clip = base_lum

        # clip ceiling (binary search) — uses interior/core pixels only when clip_ignore_edge_px set
        if _clip_ratio(base_lum_clip, MAX_DARKEN) > MAX_CLIP_RATIO:
            clip_ceiling = MAX_DARKEN
        elif _clip_ratio(base_lum_clip, MAX_BRIGHTEN) <= MAX_CLIP_RATIO:
            clip_ceiling = MAX_BRIGHTEN
        else:
            lo, hi = MAX_DARKEN, MAX_BRIGHTEN
            for _ in range(20):
                mid = (lo + hi) / 2.0
                if _clip_ratio(base_lum_clip, mid) <= MAX_CLIP_RATIO:
                    lo = mid
                else:
                    hi = mid
            clip_ceiling = lo

        # --- iterative solver (up to 12 passes, 384px crop, ~5 ms / iter) ---
        current_stops = 0.0
        residual_tol = 0.02
        adaptive_gain = 0.85
        for _ in range(12):
            scaled_lum = base_lum * (2.0 ** current_stops)
            p10, p95, p98 = np.percentile(scaled_lum, [10, 95, 98])

            hi95_stop = float(np.log2(TARGET_HI_P95 / max(float(p95), EPS)))
            hi98_stop = float(np.log2(TARGET_HI_P98 / max(float(p98), EPS)))
            highlight_ceiling = min(hi95_stop, hi98_stop, clip_ceiling - current_stops, MAX_BRIGHTEN - current_stops)

            shadow_push = float(np.log2(TARGET_SHADOW_P10 / max(float(p10), EPS)))

            if highlight_ceiling >= 0.0:
                residual = highlight_ceiling
                if shadow_push > 0.0:
                    residual = max(residual, min(shadow_push, highlight_ceiling))
            else:
                residual = highlight_ceiling

            if abs(residual) <= residual_tol:
                break

            step = float(np.clip(residual * adaptive_gain, -2.0, 2.0))
            if abs(step) < 0.005:
                break
            current_stops = float(np.clip(current_stops + step, MAX_DARKEN, MAX_BRIGHTEN))

        ideal_stops = float(np.clip(current_stops, MAX_DARKEN, MAX_BRIGHTEN))

        # --- apply strength POST-convergence ---
        if ideal_stops >= 0.0:
            final_stops = ideal_stops * BRIGHTEN_STRENGTH
        else:
            final_stops = ideal_stops * DARKEN_STRENGTH
        final_stops = float(np.clip(final_stops, MAX_DARKEN, MAX_BRIGHTEN))

        if not np.isfinite(final_stops):
            return 0.0
        return final_stops

    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Fast numpy crop apply
# ---------------------------------------------------------------------------

def apply_exposure_crop_numpy(
    noauto_linear: np.ndarray,
    crop_bbox: Tuple[int, int, int, int],
    total_scale: float,
) -> np.ndarray:
    """Apply total_scale to the crop region of noauto_linear.

    crop_bbox: (r0, c0, r1, c1)  — row/col bounding box (exclusive end)
    Returns uint8 sRGB crop.
    """
    r0, c0, r1, c1 = crop_bbox
    crop = noauto_linear[r0:r1, c0:c1]
    corrected = np.clip(crop * float(total_scale), 0.0, 1.0)
    return linear_to_srgb_u8(corrected)


# ---------------------------------------------------------------------------
# Compose total scale / stops
# ---------------------------------------------------------------------------

def compose_total_stops(subject_stops: float, meter_scale: float) -> float:
    """Combine meter_scale with per-bird subject_stops into a total stops value."""
    meter_scale = float(max(meter_scale, 1e-6))
    return float(np.log2(meter_scale) + float(subject_stops))


def compute_exposure_stops(
    img_u8: np.ndarray,
    mask: np.ndarray,
    quality: str = "balanced",
) -> float:
    """One-step ideal / residual stops from sRGB uint8 crop and mask (legacy helper).

    Kept for older call sites; production path uses :func:`compute_stops_numpy_solver`.
    """
    quality_name = normalize_quality_name(quality)
    cfg = _EXPOSURE_QUALITY_PROFILES[quality_name]
    eps = 1e-3
    max_brighten_solve = 8.0
    max_darken_solve = -3.0
    target_p95 = float(cfg["TARGET_HI_P95"])
    target_p98 = float(cfg["TARGET_HI_P98"])
    target_p10 = float(cfg["TARGET_SHADOW_P10"])
    clip_thresh = float(cfg["CLIP_THRESH"])
    max_clip = float(cfg["MAX_CLIP_RATIO"])

    img_f = img_u8.astype(np.float32) / 255.0
    mask_b = np.asarray(mask, dtype=bool)
    pixels = img_f[mask_b] if mask_b.any() else img_f.reshape(-1, 3)
    lum = 0.2126 * pixels[:, 0] + 0.7152 * pixels[:, 1] + 0.0722 * pixels[:, 2]
    lum = lum[np.isfinite(lum)]
    if lum.size == 0:
        return 0.0
    p10, p95, p98 = np.percentile(lum, [10, 95, 98])

    if float(np.mean((lum * 2.0**max_darken_solve) >= clip_thresh)) > max_clip:
        clip_ceil = max_darken_solve
    elif float(np.mean((lum * 2.0**max_brighten_solve) >= clip_thresh)) <= max_clip:
        clip_ceil = max_brighten_solve
    else:
        lo, hi = max_darken_solve, max_brighten_solve
        for _ in range(20):
            mid = (lo + hi) / 2.0
            if float(np.mean((lum * 2.0**mid) >= clip_thresh)) <= max_clip:
                lo = mid
            else:
                hi = mid
        clip_ceil = lo

    hi95 = float(np.log2(target_p95 / max(float(p95), eps)))
    hi98 = float(np.log2(target_p98 / max(float(p98), eps)))
    hl_ceil = min(hi95, hi98, clip_ceil, max_brighten_solve)

    shad = float(np.log2(target_p10 / max(float(p10), eps)))
    if hl_ceil >= 0.0:
        s = hl_ceil
        if shad > 0.0:
            s = max(s, min(shad, hl_ceil))
    else:
        s = hl_ceil
    return float(np.clip(s, max_darken_solve, max_brighten_solve))


def preserve_highlights_for_stops(stops: float, profile: Optional[str] = None) -> float:
    """Map subject stops to rawpy ``exp_preserve_highlights`` (0..1).

    *profile* is accepted for call-site compatibility but is not used.
    """
    del profile  # unused — kept for API compatibility with older call sites
    if stops > 1.0:
        return 0.95
    if stops > 0.4:
        return 0.9
    if stops > 0.0:
        return 0.85
    return 0.0
