"""Folder-level smart search (search + backfill) and its bridge methods, without model weights.

The image embedder and text encoder are stubbed with deterministic vectors so
ranking, coverage, crop-path jailing and bridge validation are tested in the
fast lane.
"""

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
from kestrel_analyzer import smart_search as ss
from kestrel_analyzer.ml import clip_search as cs

pytestmark = pytest.mark.unit

HEADER = "filename,species,scene_count,crop_path,crops_json"


def _axis(i, w=1.0, base=1.0):
    v = np.zeros(cs.DIM, np.float32)
    v[0] = base  # shared "a bird" direction
    v[i] = w
    return v / np.linalg.norm(v)


def _make_folder(root: Path, rows, backslash=False):
    """rows: [(filename, scene, [crop colours (r,g,b) or None for whole-frame])]."""
    from PIL import Image

    kdir = root / ".kestrel"
    (kdir / "crop").mkdir(parents=True)
    lines = [HEADER]
    for fn, scene, colours in rows:
        stem = os.path.splitext(fn)[0]
        crops = []
        for ci, col in enumerate(colours):
            rel = f".kestrel/crop/{stem}_crop_{ci}.jpg"
            Image.new("RGB", (32, 32), col or (0, 0, 0)).save(root / rel)
            crops.append({
                "crop_index": ci,
                "crop_path": rel.replace("/", "\\") if backslash else rel,
                "detection_index": -1 if col is None else ci,
            })
        cj = json.dumps(crops).replace('"', '""')
        lines.append(f'{fn},Unknown,{scene},,"{cj}"')
    (kdir / "kestrel_database.csv").write_text("\n".join(lines), encoding="utf-8")
    return kdir


class StubEmbedder:
    """Maps a crop's dominant colour to an axis: red->1, green->2, blue->3."""

    def __init__(self):
        self.calls = 0

    def embed(self, img):
        self.calls += 1
        m = np.asarray(img, np.float32).reshape(-1, 3).mean(axis=0)
        return _axis(1 + int(np.argmax(m)))[None]


class StubEncoder:
    VOCAB = {"red": _axis(1, 1.2), "green": _axis(2, 1.2), "blue": _axis(3, 1.2)}

    def encode(self, q):
        return self.VOCAB.get(cs.normalize_query(q), self.baseline())

    def baseline(self):
        v = np.zeros(cs.DIM, np.float32)
        v[0] = 1.0
        return v


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "shoot"
    _make_folder(root, [
        ("A.CR3", 1, [(250, 0, 0)]),
        ("B.CR3", 1, [(0, 250, 0), (250, 10, 10)]),
        ("C.CR3", 2, [(0, 0, 250)]),
        ("D.CR3", 3, [None]),  # no detection: whole-frame fallback, not indexable
    ], backslash=True)
    return root


def test_read_folder_rows(folder):
    rows = ss.read_folder_rows(str(folder / ".kestrel"))
    assert rows.scene_of == {"A.CR3": "1", "B.CR3": "1", "C.CR3": "2", "D.CR3": "3"}
    assert rows.indexable == {"A.CR3", "B.CR3", "C.CR3"}


def test_backfill_then_search_ranks_scenes(folder):
    kdir = str(folder / ".kestrel")
    res = ss.search_folder(kdir, "red", encoder=StubEncoder())
    assert res["scenes"] == [] and res["needs_index"] and res["indexable_images"] == 3
    emb = StubEmbedder()
    progress = []
    stats = ss.index_folder(kdir, emb, progress_cb=lambda d, t, f: progress.append((d, t)))
    assert stats["indexed"] == 3 and emb.calls == 4 and progress[-1] == (3, 3)
    assert ss.coverage(kdir) == {"indexed_images": 3, "indexable_images": 3}

    res = ss.search_folder(kdir, "green", encoder=StubEncoder())
    assert not res["needs_index"]
    assert [s["scene_id"] for s in res["scenes"]] == ["1", "2"]
    assert res["scenes"][0]["best_filename"] == "B.CR3" and res["scenes"][0]["best_crop"] == 0
    res = ss.search_folder(kdir, "blue", encoder=StubEncoder())
    assert res["scenes"][0]["scene_id"] == "2"
    # Scene 1 holds two red crops (A, B#1): the scene shows once, B's image score is its best crop.
    res = ss.search_folder(kdir, "red", limit=1, encoder=StubEncoder())
    assert [s["scene_id"] for s in res["scenes"]] == ["1"]
    assert {i["filename"] for i in res["images"]} == {"A.CR3", "B.CR3"}
    # A second backfill only does missing files.
    assert ss.index_folder(kdir, emb)["total"] == 0


def test_empty_query_returns_no_scenes(folder):
    kdir = str(folder / ".kestrel")
    ss.index_folder(kdir, StubEmbedder())
    assert ss.search_folder(kdir, "   ", encoder=StubEncoder())["scenes"] == []


def test_crop_path_is_jailed(folder, tmp_path):
    kdir = str(folder / ".kestrel")
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"x")
    evil = {"crop_index": 99, "crop_path": "../secret.jpg", "detection_index": 0}
    assert ss.resolve_crop_file(kdir, "A.CR3", evil) is None
    evil_abs = {"crop_index": 99, "crop_path": str(secret), "detection_index": 0}
    assert ss.resolve_crop_file(kdir, "A.CR3", evil_abs) is None
    ok = {"crop_index": 0, "crop_path": ".kestrel\\crop\\A_crop_0.jpg"}
    assert ss.resolve_crop_file(kdir, "A.CR3", ok).endswith("A_crop_0.jpg")
    # Bad crop_path falls back to the pipeline's naming.
    assert ss.resolve_crop_file(kdir, "A.CR3", {"crop_index": 0, "crop_path": "../nope"}).endswith("A_crop_0.jpg")


def test_cancel_and_stop(folder):
    import threading

    kdir = str(folder / ".kestrel")
    ev = threading.Event()
    ev.set()
    assert ss.index_folder(kdir, StubEmbedder(), cancel_event=ev)["canceled"]
    assert ss.index_folder(kdir, StubEmbedder(), should_stop=lambda: True)["stopped"]
    assert ss.coverage(kdir)["indexed_images"] == 0


# ---------------------------------------------------------------- bridge


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(cs, "assets_available", lambda *a, **k: True)
    monkeypatch.setattr(cs, "shared_text_encoder", lambda: StubEncoder())
    monkeypatch.setattr(cs, "ClipImageEmbedder", lambda *a, **k: StubEmbedder())
    return api_bridge.Api()


def test_bridge_search_and_index(api, folder):
    f = str(folder)
    st = api.smart_search_status(f)
    assert st["success"] and st["needs_index"] and st["indexed_images"] == 0
    r = api.smart_search(f, "red")
    assert r["success"] and r["scenes"] == [] and r["needs_index"]
    job = api.smart_search_index_folder(f)
    assert job["success"]
    for _ in range(200):
        p = api.get_smart_search_index_progress(job["job_id"])["progress"]
        if p.get("done"):
            break
        time.sleep(0.02)
    assert p["phase"] == "complete" and p["current"] == p["total"] == 3
    r = api.smart_search(str(folder / ".kestrel"), "blue")  # .kestrel path is accepted too
    assert r["success"] and r["scenes"][0]["scene_id"] == "2"
    assert api.get_smart_search_index_progress("nope")["success"] is False
    assert api.cancel_smart_search_index("nope")["success"] is False


def test_bridge_rejects_bad_paths_and_input(api, tmp_path, folder, monkeypatch):
    assert api.smart_search(str(tmp_path / "missing"), "red")["success"] is False
    assert api.smart_search("", "red")["success"] is False
    assert api.smart_search(str(folder), 123)["success"] is False
    assert api.smart_search_index_folder(str(tmp_path / "missing"))["success"] is False
    assert api.smart_search_status("")["success"] is False
    # Outside the configured root jail.
    monkeypatch.setattr(api_bridge, "_ALLOWED_ROOT", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    for call in (lambda: api.smart_search(str(folder), "red"),
                 lambda: api.smart_search_status(str(folder)),
                 lambda: api.smart_search_index_folder(str(folder))):
        r = call()
        assert r["success"] is False and "outside" in r["error"].lower()


def test_bridge_refuses_index_during_analysis(api, folder, monkeypatch):
    monkeypatch.setattr(api_bridge._queue_manager, "has_running_writer", lambda root: True)
    assert api.smart_search_index_folder(str(folder)) == {"success": False, "error": "analysis_running"}


def test_bridge_unavailable_without_assets(api, folder, monkeypatch):
    monkeypatch.setattr(cs, "assets_available", lambda *a, **k: False)
    assert api.smart_search(str(folder), "red")["error"] == "smart_search_unavailable"
    assert api.smart_search_status(str(folder))["available"] is False


def test_setting_is_sanitised_as_bool():
    import settings_utils

    out = settings_utils._sanitize_settings_payload({"smart_search_enabled": "false"})
    assert out["smart_search_enabled"] is False
    assert settings_utils._sanitize_settings_payload({"smart_search_enabled": True})["smart_search_enabled"] is True
