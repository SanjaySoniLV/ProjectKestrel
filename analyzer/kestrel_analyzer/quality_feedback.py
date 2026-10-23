"""Quality feedback: pick a burst's frames for the user to sort by sharpness.

The scene viewer asks "Did Kestrel sort this burst's sharpness well?". The
user can then drag 2-5 bird crops of that burst into order from sharpest to
blurriest. With the user's consent the crops, their ranking and both quality
scorers' outputs are shared so Kestrel's quality sorting can be measured
against people (and, later, improved). This module is the pure part of that
feature -- no network, no UI:

* :func:`select_feedback_frames` picks which frames to show and the
  (shuffled) order to show them in.
* :func:`normalize_ranking` / :func:`build_submission_payload` /
  :func:`validate_payload` build the upload request body (the HTTP client is
  ``analyzer/quality_feedback_client.py``).
* :func:`reencode_crop` turns a crop JPEG into the bytes that are uploaded:
  re-encoded with every metadata block (EXIF, GPS, ICC, XMP, comments)
  dropped and the longest side capped.

Only focus/sharpness is asked about. Kestrel's quality scores are objective
(sharpness), so the labels must be too: pose and composition don't count.
"""

from __future__ import annotations

import io
import math
import random
import re
from typing import Any, Iterable, Optional, Sequence

MIN_FRAMES = 2
MAX_FRAMES = 5
# Two effective scores closer than this count as "Kestrel is unsure".
CLOSE_GAP = 0.05
# Upload limits (mirrors the server-side contract).
MAX_CROP_BYTES = 1_500_000
MAX_TAGS = 10
MAX_TAG_CHARS = 80
UPLOAD_MAX_SIDE = 768
UPLOAD_JPEG_QUALITY = 90

INSTALL_ID_RE = re.compile(r"^qfi_[0-9a-f]{32}$")
THUMBS = ("up", "down")
SCORERS = ("siglip", "legacy")
PLATFORMS = ("windows", "macos", "linux")


def _num(v) -> Optional[float]:
    """A stored 0-1 score as float, or None for missing/-1/blank/NaN."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    return min(f, 1.0)


def _effective(row: dict, active_scorer: str) -> Optional[float]:
    q = _num(row.get("quality"))
    if q is not None:
        return q
    s, l = _num(row.get("quality_siglip")), _num(row.get("quality_legacy"))
    first, second = (s, l) if active_scorer == "siglip" else (l, s)
    return first if first is not None else second


def _has_crop(row: dict) -> bool:
    cp = row.get("crop_path")
    if cp is None:
        return False
    if isinstance(cp, float) and not math.isfinite(cp):
        return False
    s = str(cp).strip()
    return bool(s) and s.lower() not in ("nan", "none")


def eligible_rows(rows: Iterable[dict], active_scorer: str = "siglip") -> list[dict]:
    """Rows that can be shown: a bird crop and at least one quality score."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not _has_crop(r):
            continue
        if _effective(r, active_scorer) is None:
            continue
        out.append(r)
    return out


def _cull(row: dict) -> str:
    v = str(row.get("culled") or "").strip().lower()
    return v if v in ("accept", "reject") else ""


def select_feedback_frames(
    rows: Sequence[dict],
    k: int = MAX_FRAMES,
    *,
    active_scorer: str = "siglip",
    rng: Optional[random.Random] = None,
) -> dict:
    """Choose 2..k frames of one burst for the sort dialog.

    ``rows`` are database rows (dicts with ``filename``, ``crop_path``,
    ``quality``, ``quality_siglip``, ``quality_legacy`` and optionally
    ``culled``). Rows without a crop or without any quality score are
    excluded.

    Priority (the frames most worth a human label):

    1. **Disagreement** -- pairs the two scorers rank in opposite order
       (strongest disagreement first).
    2. **Uncertainty** -- neighbours whose effective scores are within
       :data:`CLOSE_GAP`.
    3. **The user's own decisions** -- an accepted and a rejected frame.
    4. **Fill-in** -- frames spread across the burst's score range
       (farthest-point on the effective score).

    When the burst has more eligible frames than ``k`` (and ``k > 2``), steps
    1-3 take at most ``k - 1`` slots so at least one frame comes from the
    spread.

    Returns ``{"frames": [...], "shown_order": [...], "reasons": {...}}``.
    ``frames`` is in Kestrel's order (effective score, sharpest first) and
    each frame carries ``index`` (its position in that order, which is the
    crop index sent to the server). ``shown_order`` is the order the dialog
    must show them in: a shuffle of the indexes that is never Kestrel's own
    order, so the user isn't anchored by it. Fewer than two eligible frames
    gives empty lists.
    """
    active_scorer = active_scorer if active_scorer in SCORERS else "siglip"
    rng = rng or random.Random()
    k = max(MIN_FRAMES, min(MAX_FRAMES, int(k or MAX_FRAMES)))
    pool = eligible_rows(rows, active_scorer)
    if len(pool) < MIN_FRAMES:
        return {"frames": [], "shown_order": [], "reasons": {}}

    eff = [_effective(r, active_scorer) for r in pool]
    names = [str(r.get("filename") or "") for r in pool]
    n = len(pool)
    chosen: list[int] = []
    reasons: dict[int, str] = {}
    # Keep one slot for the spread, except when only a pair is asked for.
    priority_cap = k if (n <= k or k <= MIN_FRAMES) else k - 1

    def take(i: int, why: str, cap: int) -> bool:
        if i in reasons or len(chosen) >= cap:
            return False
        chosen.append(i)
        reasons[i] = why
        return True

    # 1. Scorer disagreement.
    pairs = []
    for i in range(n):
        si, li = _num(pool[i].get("quality_siglip")), _num(pool[i].get("quality_legacy"))
        if si is None or li is None:
            continue
        for j in range(i + 1, n):
            sj, lj = _num(pool[j].get("quality_siglip")), _num(pool[j].get("quality_legacy"))
            if sj is None or lj is None:
                continue
            ds, dl = si - sj, li - lj
            if ds * dl < 0:
                pairs.append((abs(ds) + abs(dl), i, j))
    pairs.sort(key=lambda p: (-p[0], names[p[1]], names[p[2]]))
    for _, i, j in pairs:
        if len(chosen) >= priority_cap:
            break
        take(i, "disagreement", priority_cap)
        take(j, "disagreement", priority_cap)

    # 2. Close effective scores (Kestrel unsure).
    order = sorted(range(n), key=lambda i: (-eff[i], names[i]))
    close = []
    for a, b in zip(order, order[1:]):
        gap = eff[a] - eff[b]
        if gap <= CLOSE_GAP:
            close.append((gap, a, b))
    close.sort(key=lambda p: (p[0], names[p[1]]))
    for _, a, b in close:
        if len(chosen) >= priority_cap:
            break
        take(a, "close", priority_cap)
        take(b, "close", priority_cap)

    # 3. The user's accepted / rejected frames (one of each first).
    accepted = [i for i in order if _cull(pool[i]) == "accept"]
    rejected = [i for i in order if _cull(pool[i]) == "reject"]
    for group in (accepted[:1], rejected[:1], accepted[1:], rejected[1:]):
        for i in group:
            take(i, "culled", priority_cap)

    # 4. Fill-in: spread across the score range.
    remaining = [i for i in order if i not in reasons]
    if not chosen and remaining:
        # Start from both ends of the range.
        take(order[0], "spread", k)
        take(order[-1], "spread", k)
        remaining = [i for i in order if i not in reasons]
    while len(chosen) < k and remaining:
        best = max(
            remaining,
            key=lambda i: (min(abs(eff[i] - eff[c]) for c in chosen), -order.index(i)),
        )
        take(best, "spread", k)
        remaining.remove(best)

    # Kestrel's order over the chosen frames defines the crop indexes.
    kestrel = sorted(chosen, key=lambda i: (-eff[i], names[i]))
    frames = []
    out_reasons = {}
    for idx, i in enumerate(kestrel):
        r = pool[i]
        frames.append({
            "index": idx,
            "filename": names[i],
            "crop_path": str(r.get("crop_path")).strip(),
            "quality": eff[i],
            "quality_siglip": _num(r.get("quality_siglip")),
            "quality_legacy": _num(r.get("quality_legacy")),
            "culled": _cull(r),
        })
        out_reasons[idx] = reasons[i]

    shown = list(range(len(frames)))
    identity = list(shown)
    for _ in range(50):
        rng.shuffle(shown)
        if shown != identity:
            break
    if shown == identity:  # pragma: no cover - astronomically unlikely
        shown = identity[1:] + identity[:1]
    return {"frames": frames, "shown_order": shown, "reasons": out_reasons}


# ── Payload ────────────────────────────────────────────────────────────────


def normalize_ranking(ranking: Any, n: int) -> list[list[int]]:
    """Validate a ranking (groups of crop indexes, sharpest group first).

    A group of more than one index is a tie ("too close to call"). Every
    index ``0..n-1`` must appear exactly once. Raises ``ValueError``.
    """
    if not isinstance(ranking, (list, tuple)) or not ranking:
        raise ValueError("invalid_ranking")
    out: list[list[int]] = []
    seen: set[int] = set()
    for group in ranking:
        if isinstance(group, bool):
            raise ValueError("invalid_ranking")
        if isinstance(group, int):
            group = [group]
        if not isinstance(group, (list, tuple)) or not group:
            raise ValueError("invalid_ranking")
        g = []
        for v in group:
            if isinstance(v, bool) or not isinstance(v, int) or not (0 <= v < n) or v in seen:
                raise ValueError("invalid_ranking")
            seen.add(v)
            g.append(int(v))
        out.append(g)
    if len(seen) != n:
        raise ValueError("invalid_ranking")
    return out


def clean_tags(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for v in values or []:
        s = str(v or "").strip()
        if not s or s.lower() in ("unknown", "n/a", "nan", "none", "no bird"):
            continue
        s = s[:MAX_TAG_CHARS]
        if s not in out:
            out.append(s)
        if len(out) >= MAX_TAGS:
            break
    return out


def _score(v) -> Optional[float]:
    f = _num(v)
    return None if f is None else round(f, 6)


def build_submission_payload(
    *,
    frames: Sequence[dict],
    shown_order: Sequence[int],
    ranking: Any,
    thumbs: str,
    duration_ms: Any,
    install_id: str,
    crop_sizes: Sequence[int],
    active_scorer: str,
    species: Iterable[Any] = (),
    family: Iterable[Any] = (),
    app_version: str = "",
    pipeline_version: int = 3,
    platform: str = "",
) -> dict:
    """The ``POST /v1/feedback/quality`` body. Validated before returning."""
    n = len(frames)
    try:
        dur = int(round(float(duration_ms)))
    except (TypeError, ValueError):
        dur = 0
    payload = {
        "install_id": str(install_id or ""),
        "client": {
            "app_version": str(app_version or "unknown")[:64],
            "pipeline_version": int(pipeline_version),
            "platform": platform if platform in PLATFORMS else "linux",
        },
        "thumbs": thumbs,
        "active_scorer": active_scorer if active_scorer in SCORERS else "siglip",
        "shown_order": [int(i) for i in shown_order],
        "ranking": normalize_ranking(ranking, n),
        "duration_ms": max(0, min(dur, 24 * 3600 * 1000)),
        "species": clean_tags(species),
        "family": clean_tags(family),
        "crops": [
            {
                "index": int(f["index"]),
                "bytes": int(crop_sizes[i]),
                "quality": _score(f.get("quality")),
                "quality_siglip": _score(f.get("quality_siglip")),
                "quality_legacy": _score(f.get("quality_legacy")),
            }
            for i, f in enumerate(frames)
        ],
    }
    validate_payload(payload)
    return payload


def validate_payload(p: dict) -> None:
    """Check a payload against the API contract. Raises ``ValueError``."""
    if not isinstance(p, dict):
        raise ValueError("invalid_payload")
    if not INSTALL_ID_RE.match(str(p.get("install_id") or "")):
        raise ValueError("invalid_install_id")
    if p.get("thumbs") not in THUMBS:
        raise ValueError("invalid_thumbs")
    if p.get("active_scorer") not in SCORERS:
        raise ValueError("invalid_active_scorer")
    crops = p.get("crops")
    if not isinstance(crops, list) or not (MIN_FRAMES <= len(crops) <= MAX_FRAMES):
        raise ValueError("invalid_crops")
    n = len(crops)
    if sorted(c.get("index") for c in crops) != list(range(n)):
        raise ValueError("invalid_crops")
    for c in crops:
        b = c.get("bytes")
        if isinstance(b, bool) or not isinstance(b, int) or not (1 <= b <= MAX_CROP_BYTES):
            raise ValueError("invalid_crop_bytes")
        for key in ("quality", "quality_siglip", "quality_legacy"):
            v = c.get(key)
            if v is not None and (not isinstance(v, (int, float)) or not (0 <= v <= 1)):
                raise ValueError("invalid_score")
    shown = p.get("shown_order")
    if not isinstance(shown, list) or sorted(shown) != list(range(n)):
        raise ValueError("invalid_shown_order")
    normalize_ranking(p.get("ranking"), n)
    for key in ("species", "family"):
        tags = p.get(key)
        if not isinstance(tags, list) or len(tags) > MAX_TAGS:
            raise ValueError(f"invalid_{key}")
        if any(not isinstance(t, str) or len(t) > MAX_TAG_CHARS for t in tags):
            raise ValueError(f"invalid_{key}")
    client = p.get("client")
    if not isinstance(client, dict) or client.get("platform") not in PLATFORMS:
        raise ValueError("invalid_client")
    d = p.get("duration_ms")
    if isinstance(d, bool) or not isinstance(d, int) or d < 0:
        raise ValueError("invalid_duration")


# ── Crop re-encoding ───────────────────────────────────────────────────────


def reencode_crop(src, max_side: int = UPLOAD_MAX_SIDE, quality: int = UPLOAD_JPEG_QUALITY) -> bytes:
    """Return a clean JPEG of the crop at ``src`` (path or bytes).

    The pixels are copied into a brand-new image, so nothing from the source
    file survives: no EXIF (and so no GPS or camera serial), no ICC profile,
    no XMP, no comments. The longest side is capped at ``max_side``.
    """
    from PIL import Image, ImageOps

    if isinstance(src, (bytes, bytearray)):
        im = Image.open(io.BytesIO(bytes(src)))
    else:
        im = Image.open(src)
    with im:
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
        rgb = im.convert("RGB")
    if max(rgb.size) > max_side:
        rgb.thumbnail((max_side, max_side), Image.LANCZOS)
    clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
    buf = io.BytesIO()
    clean.save(buf, format="JPEG", quality=int(quality), optimize=True)
    data = buf.getvalue()
    # A pathological crop (huge noise image) could exceed the server cap even
    # at 768 px; step the quality down rather than fail the submission.
    q = int(quality)
    while len(data) > MAX_CROP_BYTES and q > 40:
        q -= 15
        buf = io.BytesIO()
        clean.save(buf, format="JPEG", quality=q, optimize=True)
        data = buf.getvalue()
    return data
