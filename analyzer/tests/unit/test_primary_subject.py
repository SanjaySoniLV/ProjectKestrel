"""Primary-subject linking (kestrel_analyzer.primary_subject) and its bridge call."""

import itertools
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
from kestrel_analyzer import embedding_store
from kestrel_analyzer import primary_subject as ps

pytestmark = pytest.mark.unit

RNG = np.random.default_rng(7)
# One appearance vector per bird; every frame adds a little pose noise.
BIRDS = {k: RNG.normal(size=768).astype(np.float32) for k in range(4)}


def _bbox(cx, cy, size):
    return dict(x_min_norm=cx - size / 2, x_max_norm=cx + size / 2,
                y_min_norm=cy - size / 2, y_max_norm=cy + size / 2)


def _scene(n_frames=5, pan=0.03, drop=None):
    """Birds at fixed spots; the camera pans right each frame; detection order shuffled.

    ``drop`` = (frame, bird) removed from that frame. Returns (frames, truth) where
    truth[f][bird] is the crop position of that bird in frame f (or missing).
    """
    spots = {0: (0.2, 0.5), 1: (0.4, 0.45), 2: (0.6, 0.55), 3: (0.75, 0.4)}
    frames, truth = [], []
    for f in range(n_frames):
        order = list(spots)
        RNG.shuffle(order)
        if drop and drop[0] == f:
            order.remove(drop[1])
        crops, t = [], {}
        for pos, k in enumerate(order):
            cx, cy = spots[k]
            emb = BIRDS[k] + 0.25 * RNG.normal(size=768).astype(np.float32)
            crops.append(ps.make_crop(_bbox(cx - pan * f, cy, 0.08), 0.1 * (k + 1), emb, emb))
            t[k] = pos
        frames.append(crops)
        truth.append(t)
    return frames, truth


def test_hungarian_matches_brute_force():
    for n in range(1, 6):
        for _ in range(20):
            C = RNG.uniform(0, 10, size=(n, n))
            best = min(sum(C[i, p[i]] for i in range(n)) for p in itertools.permutations(range(n)))
            got = sum(C[i, j] for i, j in ps.hungarian(C))
            assert got == pytest.approx(best)


def test_follow_subject_through_shuffled_detection_order():
    frames, truth = _scene()
    for bird in range(4):
        res = ps.follow_subject(frames, 0, truth[0][bird])
        for f in range(len(frames)):
            assert res[f][0] == truth[f][bird]


def test_follow_subject_seeded_in_last_frame():
    frames, truth = _scene()
    last = len(frames) - 1
    res = ps.follow_subject(frames, last, truth[last][2])
    assert [res[f][0] for f in range(len(frames))] == [truth[f][2] for f in range(len(frames))]


def test_missing_bird_is_not_found_then_reacquired():
    frames, truth = _scene(drop=(2, 1))
    res = ps.follow_subject(frames, 0, truth[0][1])
    assert res[2][0] is None
    assert res[3][0] == truth[3][1] and res[4][0] == truth[4][1]


def test_label_birds_numbers_are_consistent_across_frames():
    frames, truth = _scene()
    ids, _ = ps.label_birds(frames)
    for bird in range(4):
        assert len({ids[f][truth[f][bird]] for f in range(len(frames))}) == 1
    assert sorted(ids[0]) == [1, 2, 3, 4]


def test_label_birds_numbers_a_bird_that_enters_later():
    frames, truth = _scene(drop=(0, 3))
    ids, _ = ps.label_birds(frames)
    assert ids[1][truth[1][3]] == 4      # first seen in frame 1 -> next number
    assert ids[4][truth[4][3]] == 4


def _write_folder(tmp_path, frames):
    kdir = tmp_path / ".kestrel"
    kdir.mkdir()
    bio = embedding_store.EmbeddingStore(str(kdir), model="bioclip-2")
    sig = embedding_store.EmbeddingStore(str(kdir), model=ps.SIGLIP_SLOT, slot=ps.SIGLIP_SLOT)
    payload = []
    for f, crops in enumerate(frames):
        fn = f"IMG_{f:04d}.CR3"
        bio.put(fn, {i: c["b"] for i, c in enumerate(crops)})
        sig.put(fn, {i: c["s"] for i, c in enumerate(crops)})
        payload.append({"filename": fn, "crops": [
            {"crop_index": i, "quality": c["quality"],
             "bbox": _bbox(c["cx"], c["cy"], c["w"])} for i, c in enumerate(crops)]})
    return kdir, payload


def test_analyze_scene_reads_stored_embeddings(tmp_path):
    frames, truth = _scene()
    kdir, payload = _write_folder(tmp_path, frames)
    out = ps.analyze_scene(str(kdir), payload, seed=(0, truth[0][2]))
    assert out["embedded_crops"] == out["crops"] == sum(len(f) for f in frames)
    assert [x["crop"] for x in out["follow"]] == [truth[f][2] for f in range(len(frames))]
    assert all(x["confident"] for x in out["follow"])


def test_analyze_scene_without_embeddings_falls_back_to_position(tmp_path):
    frames, truth = _scene(pan=0.0)
    _, payload = _write_folder(tmp_path, frames)
    empty = tmp_path / "other" / ".kestrel"
    empty.mkdir(parents=True)
    out = ps.analyze_scene(str(empty), payload, seed=(0, truth[0][0]))
    assert out["embedded_crops"] == 0
    assert [x["crop"] for x in out["follow"]] == [truth[f][0] for f in range(len(frames))]


def _write_csv(kdir, payload, scene="1"):
    import csv, json
    with open(kdir / "kestrel_database.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["filename", "scene_count", "capture_time", "crops_json"])
        w.writeheader()
        for i, fr in enumerate(payload):
            w.writerow({"filename": fr["filename"], "scene_count": scene, "capture_time": f"2026-10-01T08:00:{i:02d}",
                        "crops_json": json.dumps(fr["crops"])})


def _ids(data, fn, scene="1"):
    return data["scenes"][scene]["ids"][fn]


def test_ensure_links_numbers_birds_consistently(tmp_path):
    frames, truth = _scene()
    kdir, payload = _write_folder(tmp_path, frames)
    _write_csv(kdir, payload)
    data = ps.ensure_links(str(kdir))
    assert (kdir / ps.LINKS_FILENAME).is_file()
    for bird in range(4):
        nums = {_ids(data, payload[f]["filename"])[str(truth[f][bird])] for f in range(len(frames))}
        assert len(nums) == 1


def test_ensure_links_survive_a_deleted_frame(tmp_path):
    frames, truth = _scene()
    kdir, payload = _write_folder(tmp_path, frames)
    _write_csv(kdir, payload)
    before = ps.ensure_links(str(kdir))
    _write_csv(kdir, payload[:1] + payload[3:])          # cull frames 2-3
    after = ps.ensure_links(str(kdir))
    assert after["scenes"]["1"] == before["scenes"]["1"]  # stored links kept, not recomputed


def test_ensure_links_recomputes_changed_scene_and_params(tmp_path, monkeypatch):
    frames, _ = _scene()
    kdir, payload = _write_folder(tmp_path, frames)
    _write_csv(kdir, payload[:3])
    first = ps.ensure_links(str(kdir))
    _write_csv(kdir, payload)                             # frames added to the scene
    grown = ps.ensure_links(str(kdir))
    assert len(grown["scenes"]["1"]["files"]) == len(payload) > len(first["scenes"]["1"]["files"])
    monkeypatch.setattr(ps, "PARAMS", dict(ps.PARAMS, tau=2.0))
    assert ps.ensure_links(str(kdir))["params"] != grown["params"]


def test_bridge_primary_subject_links(tmp_path):
    frames, truth = _scene()
    kdir, payload = _write_folder(tmp_path, frames)
    _write_csv(kdir, payload)
    res = api_bridge.Api().primary_subject_links(str(tmp_path))
    assert res["success"] and res["min_margin"] == ps.MIN_MARGIN
    assert set(res["scenes"]["1"]["ids"]) == {fr["filename"] for fr in payload}


def test_bridge_primary_subject_links_rejects_missing_folder(tmp_path):
    assert api_bridge.Api().primary_subject_links(str(tmp_path / "nope"))["success"] is False
