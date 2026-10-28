"""Link the same bird across the frames of a scene, from data Kestrel already stores.

Inputs per crop are what the pipeline writes today: the normalised bbox and quality from
``crops_json`` and the per-crop BioCLIP 2 / SigLIP 2 embeddings from
``.kestrel/embeddings/``. No model runs here.

Method (best of the variants tried, see README.md):

1. Camera motion between consecutive frames: a similarity transform without rotation
   (zoom ``s``, shift ``tx, ty``). Every candidate crop pair proposes one; the proposal that
   lines up the most other crops wins, with each aligned crop weighted by how alike it
   looks (SigLIP). With one or two birds many proposals tie on geometry alone, which is
   why appearance has to vote.
2. Cost of linking crop ``a`` (frame t) to crop ``b`` (frame t+1):
   ``wp * min(pos, 5) + 10 * (wb * (1 - cos_bioclip) + ws * (1 - cos_siglip))`` where ``pos``
   is the motion-compensated centre distance in units of the target box size plus half the
   log size ratio. SigLIP separates individuals of one species (pose, plumage, perch);
   BioCLIP adds a little on mixed-species flocks.
3. Assignment: Hungarian over the cost matrix padded with "no match" slots at cost ``tau``,
   so birds can leave or enter the frame.
4. Multi-target tracking through the scene: every bird in the first frame is a track with
   an exponentially averaged appearance template and its last box. All tracks compete for
   the next frame's crops at once, so the subject can't steal a neighbour's slot, and a
   track that misses a frame can re-acquire later (position cost is divided by the gap).
5. Confidence: the margin between the chosen cost and the next-best option (another crop
   or "no match"). On the labelled data, links with margin >= 1 were right 98.6 % of the
   time; below 0.5, 87.5 %.
"""
from __future__ import annotations

import itertools
import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

DEFAULT_PARAMS = dict(wp=1.0, wb=0.5, ws=1.0, tau=4.5, motion=True, motion_emb=2.0, alpha=0.5)


# --------------------------------------------------------------------------- crops
def make_crop(bbox: dict, quality: float = -1.0, bioclip=None, siglip=None, **extra) -> dict:
    """``bbox`` uses normalised coords: x_min_norm/x_max_norm/y_min_norm/y_max_norm."""
    x0, x1 = float(bbox["x_min_norm"]), float(bbox["x_max_norm"])
    y0, y1 = float(bbox["y_min_norm"]), float(bbox["y_max_norm"])
    c = dict(cx=(x0 + x1) / 2, cy=(y0 + y1) / 2, w=max(x1 - x0, 1e-4), h=max(y1 - y0, 1e-4),
             quality=float(quality), b=_unit(bioclip), s=_unit(siglip))
    c.update(extra)
    return c


def _unit(v):
    if v is None:
        return None
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else None


def _cos(u, v) -> Optional[float]:
    if u is None or v is None:
        return None
    return float(u @ v) / (float(np.linalg.norm(u)) * float(np.linalg.norm(v)) + 1e-9)


# --------------------------------------------------------------------------- geometry
def global_motion(A: Sequence[dict], B: Sequence[dict], emb_w: float = 2.0) -> Tuple[float, float, float]:
    """Camera zoom + shift from frame A to frame B: ``(s, tx, ty)`` with p_B = s * p_A + t."""
    best, best_score = (1.0, 0.0, 0.0), 0.0
    for a, b in itertools.product(A, B):
        s = math.sqrt((b["w"] * b["h"]) / max(a["w"] * a["h"], 1e-9))
        s = min(max(s, 0.5), 2.0)
        tx, ty = b["cx"] - s * a["cx"], b["cy"] - s * a["cy"]
        score = 0.0
        for a2 in A:
            px, py = s * a2["cx"] + tx, s * a2["cy"] + ty
            d = min(math.hypot(b2["cx"] - px, b2["cy"] - py) / max(b2["w"], 1e-3) for b2 in B)
            if d < 0.5:
                sims = [x for x in (_cos(a2["s"], b2["s"]) for b2 in B) if x is not None]
                score += 1 + emb_w * (max(sims) if sims else 0.0)
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
    """``ta`` optionally overrides a's appearance with a track template {'b','s'}."""
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
    """Assignment with 'no match' at cost tau. Returns ({row: col|None}, {row: margin})."""
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
        margin[i] = float(row[1] - row[0]) if len(row) > 1 else 9.0
    return out, margin


def match_frames(A: Sequence[dict], B: Sequence[dict], p: dict = DEFAULT_PARAMS):
    """Crop i of frame A -> crop j of frame B (or None), with per-crop margins."""
    if not A or not B:
        return {i: None for i in range(len(A))}, {i: 9.0 for i in range(len(A))}
    motion = global_motion(A, B, p.get("motion_emb", 2.0)) if p.get("motion") else (1.0, 0.0, 0.0)
    C = np.array([[link_cost(a, b, motion, p) for b in B] for a in A])
    return assign(C, p["tau"])


# --------------------------------------------------------------------------- tracking
def track_scene(frames: Sequence[Sequence[dict]], p: dict = DEFAULT_PARAMS):
    """Multi-target tracks through a scene, seeded from frames[0].

    Returns ``(tracks, margins)``: ``tracks[k]`` is {frame_index: crop_index} for the bird
    that is crop k in frame 0; ``margins[k]`` is {frame_index: margin}.
    """
    if not frames or not frames[0]:
        return [], []
    T = [dict(b=c["b"], s=c["s"], last=c, lastf=0) for c in frames[0]]
    tracks = [{0: i} for i in range(len(frames[0]))]
    margins = [{0: 9.0} for _ in frames[0]]
    alpha = p.get("alpha", 0.5)
    for f in range(1, len(frames)):
        B = frames[f]
        if not B:
            continue
        motion = (global_motion(frames[f - 1], B, p.get("motion_emb", 2.0))
                  if p.get("motion") and frames[f - 1] else (1.0, 0.0, 0.0))
        C = np.array([[link_cost(t["last"], b, motion if t["lastf"] == f - 1 else (1.0, 0.0, 0.0),
                                 p, gap=f - t["lastf"], ta=t) for b in B] for t in T])
        asg, mg = assign(C, p["tau"])
        for i, j in asg.items():
            if j is None:
                continue
            b = B[j]; t = T[i]
            for key in ("b", "s"):
                if t[key] is not None and b[key] is not None:
                    t[key] = alpha * t[key] + (1 - alpha) * b[key]
            t["last"] = b; t["lastf"] = f
            tracks[i][f] = j; margins[i][f] = mg[i]
    return tracks, margins


def follow_subject(frames: Sequence[Sequence[dict]], seed_frame: int, seed_crop: int,
                   p: dict = DEFAULT_PARAMS):
    """Where is the bird that is ``seed_crop`` in ``seed_frame`` in every other frame?

    Tracks forward from the seed frame and backward from it (reversed frames), so the
    subject can be picked in any frame. Returns {frame_index: (crop_index|None, margin)}.
    """
    out = {seed_frame: (seed_crop, 9.0)}
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


def rank_by_subject(frames, matches, min_margin: float = 1.0):
    """Frame order for 'sort by my subject': confident matches by the subject's crop
    quality, then uncertain matches, then frames where the subject wasn't found."""
    def key(f):
        j, m = matches.get(f, (None, 0.0))
        if j is None:
            return (2, 0.0)
        q = frames[f][j]["quality"]
        return (0 if m >= min_margin else 1, -q)
    return sorted(range(len(frames)), key=key)
