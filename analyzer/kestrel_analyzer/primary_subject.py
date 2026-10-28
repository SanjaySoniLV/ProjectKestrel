"""Primary subject: find the same bird in every frame of a scene (prototype).

Uses only what pipeline 3.0 already stores: each crop's normalised bbox and
quality (``crops_json``) and its BioCLIP 2 / SigLIP 2 embeddings
(``.kestrel/embeddings/``). No model runs, so a pipeline-3 folder works as soon
as it is loaded. Evaluation and the variants that were tried are in
``experiments/primary_subject/README.md``.

Method:

1. Camera motion between consecutive frames (zoom + shift, no rotation). Each
   crop pair proposes a motion; the proposal that lines up the most other crops
   wins, each aligned crop weighted by SigLIP similarity (with one or two birds,
   geometry alone ties).
2. Link cost ``min(pos, 5) + 10 * (0.5 * (1 - cos_bioclip) + 1 * (1 - cos_siglip))``,
   ``pos`` = motion-compensated centre distance in target-box sizes + half the
   log size ratio.
3. Hungarian assignment padded with "no match" at ``tau`` so birds can leave or
   enter the frame.
4. Multi-target tracking: all birds compete for the next frame's crops at once,
   each track keeps an averaged appearance template, and a missed frame can be
   re-acquired.
5. Confidence = margin between the chosen cost and the next-best option.
   Links with margin >= 1 were right 98.6 % of the time on labelled bursts.
"""
from __future__ import annotations

import itertools
import math
import os
import threading
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import embedding_store

# tau 4.5 -> 2.5 after the full-burst perch (Oct 2026): in dense flocks the subject often has
# no box in the next frame (crop cap), and a looser "no match" linked it to a lookalike.
PARAMS = dict(wp=1.0, wb=0.5, ws=1.0, tau=2.5, motion=True, motion_emb=2.0, alpha=0.5)
MIN_MARGIN = 1.0
SIGLIP_SLOT = "siglip2-b16"   # kestrel_analyzer.ml.siglip.MODEL_ID; BioCLIP is the primary slot
NO_MARGIN = 9.0


# --------------------------------------------------------------------------- crops
def make_crop(bbox: dict, quality: float = -1.0, bioclip=None, siglip=None) -> dict:
    """``bbox`` is normalised: x_min_norm / x_max_norm / y_min_norm / y_max_norm."""
    x0, x1 = float(bbox["x_min_norm"]), float(bbox["x_max_norm"])
    y0, y1 = float(bbox["y_min_norm"]), float(bbox["y_max_norm"])
    return dict(cx=(x0 + x1) / 2, cy=(y0 + y1) / 2, w=max(x1 - x0, 1e-4), h=max(y1 - y0, 1e-4),
                quality=float(quality), b=_unit(bioclip), s=_unit(siglip))


def _unit(v):
    if v is None:
        return None
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def _cos(u, v) -> Optional[float]:
    if u is None or v is None:
        return None
    return float(u @ v) / (float(np.linalg.norm(u)) * float(np.linalg.norm(v)) + 1e-9)


# --------------------------------------------------------------------------- geometry
def global_motion(A: Sequence[dict], B: Sequence[dict], emb_w: float = 2.0) -> Tuple[float, float, float]:
    """Camera zoom + shift from frame A to frame B: ``(s, tx, ty)``, p_B = s * p_A + t."""
    best, best_score = (1.0, 0.0, 0.0), 0.0
    sims = [[_cos(a["s"], b["s"]) for b in B] for a in A]
    for a, b in itertools.product(A, B):
        s = math.sqrt((b["w"] * b["h"]) / max(a["w"] * a["h"], 1e-9))
        s = min(max(s, 0.5), 2.0)
        tx, ty = b["cx"] - s * a["cx"], b["cy"] - s * a["cy"]
        score = 0.0
        for i2, a2 in enumerate(A):
            px, py = s * a2["cx"] + tx, s * a2["cy"] + ty
            d = min(math.hypot(b2["cx"] - px, b2["cy"] - py) / max(b2["w"], 1e-3) for b2 in B)
            if d < 0.5:
                row = [x for x in sims[i2] if x is not None]
                score += 1 + emb_w * (max(row) if row else 0.0)
        if score > best_score:
            best_score, best = score, (s, tx, ty)
    return best


def pos_cost(a: dict, b: dict, motion=(1.0, 0.0, 0.0)) -> float:
    s, tx, ty = motion
    px, py = s * a["cx"] + tx, s * a["cy"] + ty
    scale = max(math.sqrt(b["w"] * b["h"]), 0.02)
    d = math.hypot(b["cx"] - px, b["cy"] - py) / scale
    size = abs(math.log(max(b["w"] * b["h"], 1e-9) / max(s * s * a["w"] * a["h"], 1e-9)))
    return d + 0.5 * size


def link_cost(a: dict, b: dict, motion, p: dict, gap: int = 1, ta=None) -> float:
    """``ta`` optionally replaces a's appearance with a track template {'b', 's'}."""
    src = ta or a
    cost = p["wp"] * min(pos_cost(a, b, motion), 5.0) / gap
    cb, cs = _cos(src["b"], b["b"]), _cos(src["s"], b["s"])
    if cb is not None:
        cost += 10 * p["wb"] * (1 - cb)
    if cs is not None:
        cost += 10 * p["ws"] * (1 - cs)
    return cost


# --------------------------------------------------------------------------- assignment
def hungarian(C: np.ndarray) -> List[Tuple[int, int]]:
    """Min-cost assignment for a square matrix (O(n^3)); returns (row, col) pairs."""
    n = C.shape[0]
    INF = float("inf")
    u = [0.0] * (n + 1); v = [0.0] * (n + 1); p = [0] * (n + 1); way = [0] * (n + 1)
    for i in range(1, n + 1):
        p[0] = i; j0 = 0
        minv = [INF] * (n + 1); used = [False] * (n + 1)
        while True:
            used[j0] = True; i0 = p[j0]; delta = INF; j1 = 0
            for j in range(1, n + 1):
                if not used[j]:
                    cur = C[i0 - 1, j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur; way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]; j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta; v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
            if j0 == 0:
                break
    return [(p[j] - 1, j - 1) for j in range(1, n + 1) if p[j] > 0]


def assign(C: np.ndarray, tau: float) -> Tuple[Dict[int, Optional[int]], Dict[int, float]]:
    """Assignment with "no match" at cost tau. Returns ({row: col|None}, {row: margin})."""
    n, m = C.shape
    big = np.full((n + m, m + n), 1e6)
    big[:n, :m] = C
    big[:n, m:] = np.where(np.eye(n) > 0, tau, 1e6)
    big[n:, :m] = np.where(np.eye(m) > 0, tau, 1e6)
    big[n:, m:] = 0
    out: Dict[int, Optional[int]] = {i: None for i in range(n)}
    for i, j in hungarian(big):
        if i < n and j < m:
            out[i] = j
    margin = {}
    for i in range(n):
        row = np.sort(np.append(C[i], tau))
        margin[i] = float(row[1] - row[0]) if len(row) > 1 else NO_MARGIN
    return out, margin


# --------------------------------------------------------------------------- tracking
def track_scene(frames: Sequence[Sequence[dict]], p: dict = PARAMS, spawn: bool = False):
    """Multi-target tracks through a scene, seeded from ``frames[0]``.

    Returns ``(tracks, margins)``: ``tracks[k]`` is {frame_index: crop_position} and
    ``margins[k]`` is {frame_index: margin}. Tracks 0..n-1 are the birds of frame 0;
    with ``spawn`` every crop left unmatched in a later frame starts a new track.
    """
    tracks: List[Dict[int, int]] = []
    margins: List[Dict[int, float]] = []
    T: List[dict] = []
    alpha = p.get("alpha", 0.5)

    def start(f, j, c):
        T.append(dict(b=c["b"], s=c["s"], last=c, lastf=f))
        tracks.append({f: j}); margins.append({f: NO_MARGIN})

    first = 0
    while first < len(frames) and not frames[first]:
        first += 1
    if first >= len(frames) or (first > 0 and not spawn):
        return tracks, margins
    for j, c in enumerate(frames[first]):
        start(first, j, c)
    for f in range(first + 1, len(frames)):
        B = frames[f]
        if not B:
            continue
        prev = frames[f - 1]
        motion = (global_motion(prev, B, p.get("motion_emb", 2.0))
                  if p.get("motion") and prev else (1.0, 0.0, 0.0))
        C = np.array([[link_cost(t["last"], b, motion if t["lastf"] == f - 1 else (1.0, 0.0, 0.0),
                                 p, gap=f - t["lastf"], ta=t) for b in B] for t in T])
        asg, mg = assign(C, p["tau"])
        taken = set()
        for i, j in asg.items():
            if j is None:
                continue
            b = B[j]; t = T[i]
            for key in ("b", "s"):
                if t[key] is not None and b[key] is not None:
                    t[key] = alpha * t[key] + (1 - alpha) * b[key]
            t["last"] = b; t["lastf"] = f
            tracks[i][f] = j; margins[i][f] = mg[i]
            taken.add(j)
        if spawn:
            for j, c in enumerate(B):
                if j not in taken:
                    start(f, j, c)
    return tracks, margins


def follow_subject(frames: Sequence[Sequence[dict]], seed_frame: int, seed_crop: int,
                   p: dict = PARAMS) -> Dict[int, Tuple[Optional[int], float]]:
    """Where is the bird that is crop ``seed_crop`` of ``seed_frame`` in every frame?

    Tracks forward and backward from the seed frame, so the subject can be picked
    in any frame. Returns {frame_index: (crop_position | None, margin)}.
    """
    out = {f: (None, 0.0) for f in range(len(frames))}
    out[seed_frame] = (seed_crop, NO_MARGIN)
    fwd = list(frames[seed_frame:])
    bwd = list(reversed(frames[: seed_frame + 1]))
    for seq, to_idx in ((fwd, lambda k: seed_frame + k), (bwd, lambda k: seed_frame - k)):
        if len(seq) < 2:
            continue
        tracks, margins = track_scene(seq, p)
        tr, mg = tracks[seed_crop], margins[seed_crop]
        for k in range(1, len(seq)):
            out[to_idx(k)] = (tr.get(k), mg.get(k, 0.0))
    return out


def label_birds(frames: Sequence[Sequence[dict]], p: dict = PARAMS):
    """Give every crop a bird number that is shared across frames.

    Returns ``(ids, margins)`` aligned with ``frames``: ``ids[f][j]`` is the bird number
    (1-based, in order of first appearance) of crop j in frame f.
    """
    tracks, mgs = track_scene(frames, p, spawn=True)
    ids = [[0] * len(fr) for fr in frames]
    margins = [[0.0] * len(fr) for fr in frames]
    order = sorted(range(len(tracks)), key=lambda k: (min(tracks[k]), tracks[k][min(tracks[k])]))
    for num, k in enumerate(order, start=1):
        for f, j in tracks[k].items():
            ids[f][j] = num
            margins[f][j] = mgs[k].get(f, 0.0)
    return ids, margins


# --------------------------------------------------------------------------- embeddings
class _EmbeddingCache:
    """Per-folder {(filename, crop_index): (bioclip, siglip)}, reloaded when the
    embedding index files change (analysis or an upgrade appends to them)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_dir: Dict[str, Tuple[tuple, dict]] = {}

    @staticmethod
    def _signature(kestrel_dir: str) -> tuple:
        sig = []
        for slot in (None, SIGLIP_SLOT):
            path = os.path.join(embedding_store._dir(kestrel_dir, slot), embedding_store.INDEX_FILENAME)
            try:
                st = os.stat(path)
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        return tuple(sig)

    def get(self, kestrel_dir: str) -> dict:
        sig = self._signature(kestrel_dir)
        with self._lock:
            hit = self._by_dir.get(kestrel_dir)
            if hit and hit[0] == sig:
                return hit[1]
        table: Dict[Tuple[str, int], list] = {}
        for pos, slot in enumerate((None, SIGLIP_SLOT)):
            keys, mat, _ = embedding_store.load_embeddings(kestrel_dir, slot=slot)
            for (fn, ci), row in zip(keys, mat):
                table.setdefault((fn, int(ci)), [None, None])[pos] = row
        with self._lock:
            self._by_dir[kestrel_dir] = (sig, table)
            while len(self._by_dir) > 8:
                self._by_dir.pop(next(iter(self._by_dir)))
        return table


_CACHE = _EmbeddingCache()


def analyze_scene(kestrel_dir: str, frames: Sequence[dict], seed: Optional[Tuple[int, int]] = None) -> dict:
    """Bird numbers for every crop of a scene, and optionally follow one bird.

    ``frames`` is the scene in capture order: ``[{"filename", "crops": [{"crop_index",
    "bbox", "quality"}]}]``. ``seed`` is ``(frame_position, crop_position)``.
    Positions in the result refer to the same lists.
    """
    table = _CACHE.get(kestrel_dir)
    built, n_crops, n_emb = [], 0, 0
    for fr in frames:
        row = []
        for c in fr["crops"]:
            b, s = table.get((fr["filename"], int(c["crop_index"])), (None, None))
            n_crops += 1
            n_emb += b is not None and s is not None
            row.append(make_crop(c["bbox"], c.get("quality", -1.0), b, s))
        built.append(row)
    ids, margins = label_birds(built)
    out = {
        "birds": ids,
        "margins": [[round(m, 3) for m in fr] for fr in margins],
        "embedded_crops": n_emb,
        "crops": n_crops,
        "follow": None,
    }
    if seed is not None:
        sf, sc = seed
        if 0 <= sf < len(built) and 0 <= sc < len(built[sf]):
            res = follow_subject(built, sf, sc)
            out["follow"] = [
                {"crop": res[f][0], "margin": round(res[f][1], 3),
                 "confident": res[f][0] is not None and res[f][1] >= MIN_MARGIN}
                for f in range(len(built))
            ]
    return out
