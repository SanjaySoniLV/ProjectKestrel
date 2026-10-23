"""Folder-level smart search (SigLIP 2, pipeline 3.0) and its bridge methods, without model weights.

The SigLIP towers are stubbed with deterministic vectors so ranking, the
frame/crop z-merge, species chips as filters, coverage, crop-path jailing and
bridge validation are tested in the fast lane.
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
from kestrel_analyzer import upgrade_v3
from kestrel_analyzer.ml import siglip

pytestmark = pytest.mark.unit

HEADER = "filename,species,family,quality,scene_count,crop_path,export_path,crops_json"
AXES = {"red": 1, "green": 2, "blue": 3}


def _axis(i, w=1.0, base=1.0):
    v = np.zeros(siglip.DIM, np.float32)
    v[0] = base  # shared "a photo" direction
    v[i] = w
    return v / np.linalg.norm(v)


def _make_folder(root: Path, rows, backslash=False, version="2.0.4"):
    """rows: [(filename, scene, species, [crop colours (r,g,b) or None for whole-frame])]."""
    from PIL import Image

    kdir = root / ".kestrel"
    (kdir / "crop").mkdir(parents=True)
    (kdir / "export").mkdir(parents=True)
    lines = [HEADER]
    for fn, scene, species, colours in rows:
        stem = os.path.splitext(fn)[0]
        crops = []
        for ci, col in enumerate(colours):
            rel = f".kestrel/crop/{stem}_crop_{ci}.jpg"
            Image.new("RGB", (32, 32), col or (0, 0, 0)).save(root / rel)
            crops.append({
                "crop_index": ci,
                "crop_path": rel.replace("/", "\\") if backslash else rel,
                "detection_index": -1 if col is None else ci,
                "species": species if col is not None else "Unknown",
                "family": "Sparrow sp." if species.endswith("Sparrow") else "Unknown",
                "quality": 0.5,
            })
        exp_rel = f".kestrel/export/{stem}_export.jpg"
        # Export colour = first crop colour, or grey for no-detection frames.
        Image.new("RGB", (48, 32), (colours[0] or (128, 128, 128))).save(root / exp_rel)
        cj = json.dumps(crops).replace('"', '""')
        fam = "Sparrow sp." if species.endswith("Sparrow") else "Unknown"
        lines.append(f'{fn},{species},{fam},0.5,{scene},,{exp_rel},"{cj}"')
    (kdir / "kestrel_database.csv").write_text("\n".join(lines), encoding="utf-8")
    (kdir / "kestrel_metadata.json").write_text(json.dumps({"version": version, "species_regions": ["NA"]}))
    return kdir


class StubSiglip:
    """Maps an image's dominant colour to an axis: red->1, green->2, blue->3 (grey->4)."""

    providers_used = ["CPUExecutionProvider"]

    def __init__(self):
        self.calls = 0

    def embed(self, img):
        self.calls += 1
        m = np.asarray(img, np.float32).reshape(-1, 3).mean(axis=0)
        if float(m.max() - m.min()) < 10:
            return _axis(4)[None]
        return _axis(1 + int(np.argmax(m)))[None]


class StubEncoder:
    def encode(self, q):
        q = siglip.normalize_query(q)
        for word, i in AXES.items():
            if word in q:
                return _axis(i, 1.2)
        if "landscape" in q:
            return _axis(4, 1.2)
        v = np.zeros(siglip.DIM, np.float32)
        v[0] = 1.0
        return v


class StubQuality:
    def score(self, emb):
        return float(0.25 + 0.5 * float(np.asarray(emb)[1] > 0.1))


class StubSpecies:
    """BioCLIP stand-in: blue crops are Mourning Doves, everything else Song Sparrows."""

    def __init__(self):
        self.regions_set = None

    def set_regions(self, regions):
        self.regions_set = list(regions)

    def embed(self, img):
        m = np.asarray(img, np.float32).reshape(-1, 3).mean(axis=0)
        return _axis(6 if int(np.argmax(m)) == 2 else 5)[None]

    def route(self, emb):
        return {"route": "bird", "bird_prob": 0.99, "wildlife_prob": 0.01, "wildlife_label": None,
                "wildlife_scientific": "", "wildlife_family": "", "wildlife_class": "", "wildlife_confidence": 0.0}

    def classify_embedding(self, emb, top_k=5):
        if float(np.asarray(emb)[6]) > 0.1:
            return {"top_species_labels": np.array(["Mourning Dove"], dtype=object), "top_species_scores": np.array([0.8]),
                    "top_family_labels": ["Pigeon/Dove sp."], "top_family_scores": [0.9]}
        return {"top_species_labels": np.array(["Song Sparrow"], dtype=object), "top_species_scores": np.array([0.9]),
                "top_family_labels": ["Sparrow sp."], "top_family_scores": [0.95]}


class StubGate:
    def animal_score(self, emb):
        return 0.9


class StubModels:
    def __init__(self):
        self.species_clf = StubSpecies()
        self.siglip_img = StubSiglip()
        self.siglip_quality = StubQuality()
        self.animal_gate = StubGate()
        self.sn_sam = None

    def load_models(self, *a, **k):
        return None


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "shoot"
    _make_folder(root, [
        ("A.CR3", 1, "Song Sparrow", [(250, 0, 0)]),
        ("B.CR3", 1, "Song Sparrow", [(0, 250, 0), (250, 10, 10)]),
        ("C.CR3", 2, "Mourning Dove", [(0, 0, 250)]),
        ("D.CR3", 3, "Unknown", [None]),  # no detection: whole-frame fallback only
    ], backslash=True)
    return root


def _upgrade(kdir, **kw):
    return upgrade_v3.upgrade_folder(kdir, StubModels(), quality_model="siglip", **kw)


def test_read_folder_rows(folder):
    rows = ss.read_folder_rows(str(folder / ".kestrel"))
    assert rows.scene_of == {"A.CR3": "1", "B.CR3": "1", "C.CR3": "2", "D.CR3": "3"}
    assert rows.indexable == {"A.CR3", "B.CR3", "C.CR3"}
    assert rows.species["C.CR3"] == {"Mourning Dove"}
    assert "Sparrow sp." in rows.families["A.CR3"]


def test_upgrade_then_search_ranks_scenes(folder):
    kdir = str(folder / ".kestrel")
    res = ss.search_folder(kdir, "red", encoder=StubEncoder())
    assert res["scenes"] == [] and res["needs_index"] and res["indexable_images"] == 3
    progress = []
    stats = _upgrade(kdir, progress_cb=lambda d, t, f: progress.append((d, t)))
    assert stats["upgraded"] == 4 and progress[-1] == (4, 4)
    cov = ss.coverage(kdir)
    assert cov == {"indexed_images": 3, "indexable_images": 3, "frame_indexed_images": 4, "frame_indexable_images": 4}

    res = ss.search_folder(kdir, "green", encoder=StubEncoder())
    assert not res["needs_index"] and res["mode"] == "semantic"
    assert res["scenes"][0]["scene_id"] == "1" and res["scenes"][0]["best_filename"] == "B.CR3"
    res = ss.search_folder(kdir, "blue", encoder=StubEncoder())
    assert res["scenes"][0]["scene_id"] == "2"
    res = ss.search_folder(kdir, "red", limit=1, encoder=StubEncoder())
    assert [s["scene_id"] for s in res["scenes"]] == ["1"]
    assert {i["filename"] for i in res["images"]} == {"A.CR3", "B.CR3"}
    # Context query only the whole frame answers (the no-detection grey frame).
    res = ss.search_folder(kdir, "wide landscape", encoder=StubEncoder())
    assert res["scenes"][0]["best_filename"] == "D.CR3"
    # A second upgrade has nothing left to do.
    assert _upgrade(kdir)["total"] == 0


def test_species_in_query_filters(folder):
    kdir = str(folder / ".kestrel")
    _upgrade(kdir)
    # "doves" -> family/species chip; filter-only, sorted by quality.
    res = ss.search_folder(kdir, "mourning doves", encoder=StubEncoder())
    assert res["mode"] == "filter"
    assert all(i["percent"] is None for i in res["images"])   # quality values, not match scores
    assert [c["label"] for c in res["chips"]] == ["Mourning Dove"] and res["chips"][0]["hits"] == 1
    assert [i["filename"] for i in res["images"]] == ["C.CR3"]
    # Filter + semantic remainder: only sparrows, ranked by "green".
    res = ss.search_folder(kdir, "song sparrow green", encoder=StubEncoder())
    assert res["mode"] == "semantic+filter"
    assert {i["filename"] for i in res["images"]} <= {"A.CR3", "B.CR3"}
    assert res["images"][0]["filename"] == "B.CR3"
    assert all(0 <= i["percent"] <= 100 for i in res["images"] + res["scenes"])
    pcts = [i["percent"] for i in res["images"]]
    assert pcts == sorted(pcts, reverse=True)
    # Un-chipping the words turns the filter off.
    res = ss.search_folder(kdir, "mourning doves", encoder=StubEncoder(), disabled_terms=["mourning doves"])
    assert res["chips"] == [] and res["mode"] == "semantic"


def test_user_edited_scene_tags_win(folder):
    kdir = folder / ".kestrel"
    _upgrade(str(kdir))
    sd = {"version": "2.0", "image_ratings": {}, "scenes": {
        "1": {"scene_id": "1", "image_filenames": ["A.CR3", "B.CR3"], "name": "", "status": "pending",
              "user_tags": {"species": ["Mourning Dove"], "families": [], "finalized": True}},
        "2": {"scene_id": "2", "image_filenames": ["C.CR3"], "name": "", "status": "pending",
              "user_tags": {"species": ["Song Sparrow"], "families": [], "finalized": True}},
    }}
    (kdir / "kestrel_scenedata.json").write_text(json.dumps(sd))
    res = ss.search_folder(str(kdir), "mourning dove", encoder=StubEncoder())
    assert {i["filename"] for i in res["images"]} == {"A.CR3", "B.CR3"}


def test_merge_image_scores_zscore():
    crop = {"a": 0.30, "b": 0.10, "c": 0.20}
    frame = {"a": 0.0, "b": 0.9, "d": 0.1}  # c has no frame, d no crop
    out = ss.merge_image_scores(crop, frame)
    assert set(out) == {"a", "b", "c", "d"}
    # b is the best frame by far -> its frame z beats a's crop z.
    assert out["b"] > out["a"] > out["c"]
    # Single-source images just use the one z they have.
    assert out["c"] == pytest.approx(0.0, abs=1e-6)
    assert ss.merge_image_scores({}, {}) == {}


def test_match_percent_mapping():
    assert ss.match_percent(ss.MATCH_Z_FLOOR) == 0
    assert ss.match_percent(-3.0) == 0                      # below average clamps to 0
    assert ss.match_percent(ss.MATCH_Z_CEIL) == 100
    assert ss.match_percent(12.0) == 100                    # clamps to 100
    assert ss.match_percent(2.0) == 50
    assert ss.match_percent(float("nan")) == 0
    assert ss.match_percent("junk") == 0
    zs = [-1, 0, 0.7, 1.5, 2.5, 3.9, 6]
    pcts = [ss.match_percent(z) for z in zs]
    assert pcts == sorted(pcts)                             # monotonic: keeps ranking order


def test_empty_query_returns_no_scenes(folder):
    kdir = str(folder / ".kestrel")
    _upgrade(kdir)
    assert ss.search_folder(kdir, "   ", encoder=StubEncoder())["scenes"] == []


def test_crop_and_export_paths_are_jailed(folder, tmp_path):
    kdir = str(folder / ".kestrel")
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"x")
    evil = {"crop_index": 99, "crop_path": "../secret.jpg", "detection_index": 0}
    assert ss.resolve_crop_file(kdir, "A.CR3", evil) is None
    evil_abs = {"crop_index": 99, "crop_path": str(secret), "detection_index": 0}
    assert ss.resolve_crop_file(kdir, "A.CR3", evil_abs) is None
    ok = {"crop_index": 0, "crop_path": ".kestrel\\crop\\A_crop_0.jpg"}
    assert ss.resolve_crop_file(kdir, "A.CR3", ok).endswith("A_crop_0.jpg")
    assert ss.resolve_crop_file(kdir, "A.CR3", {"crop_index": 0, "crop_path": "../nope"}).endswith("A_crop_0.jpg")
    assert ss.resolve_export_file(kdir, "A.CR3", "../secret.jpg").endswith("A_export.jpg")
    assert ss.resolve_export_file(kdir, "Z.CR3", str(secret)) is None


def test_cancel_and_stop(folder):
    import threading

    kdir = str(folder / ".kestrel")
    ev = threading.Event()
    ev.set()
    assert _upgrade(kdir, cancel_event=ev)["canceled"]
    assert _upgrade(kdir, should_stop=lambda: True)["stopped"]
    assert ss.coverage(kdir)["indexed_images"] == 0
    assert upgrade_v3.eligibility(kdir)["status"] == "upgradable"


# ---------------------------------------------------------------- bridge


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(siglip, "assets_available", lambda *a, **k: True)
    monkeypatch.setattr(siglip, "image_assets_available", lambda *a, **k: True)
    monkeypatch.setattr(siglip, "shared_text_encoder", lambda: StubEncoder())
    monkeypatch.setattr(api_bridge._queue_manager, "shared_pipeline", lambda use_gpu: StubModels())
    return api_bridge.Api()


def _wait(api, job_id):
    for _ in range(400):
        p = api.get_smart_search_index_progress(job_id)["progress"]
        if p.get("done"):
            return p
        time.sleep(0.02)
    return p


def test_bridge_classify_search_query(api):
    r = api.classify_search_query("Roseate Spoonbill")
    assert r == {"success": True, "kind": "filter", "labels": ["Roseate Spoonbill"]}
    assert api.classify_search_query("spoonbill wading")["kind"] == "semantic"
    assert api.classify_search_query("  ")["kind"] == "empty"
    r = api.classify_search_query("robin", ["American Robin", 5], "nope")
    assert r["kind"] == "filter" and "American Robin" in r["labels"]
    assert not api.classify_search_query(None)["success"]


def test_bridge_search_and_upgrade(api, folder):
    f = str(folder)
    st = api.smart_search_status(f)
    assert st["success"] and st["needs_index"] and st["indexed_images"] == 0
    assert st["upgrade"]["status"] == "upgradable"
    r = api.smart_search(f, "red")
    assert r["success"] and r["scenes"] == [] and r["needs_index"]
    job = api.upgrade_folder_v3(f)
    assert job["success"]
    p = _wait(api, job["job_id"])
    assert p["phase"] == "complete" and p["current"] == p["total"] == 4
    assert api.smart_search_status(f)["upgrade"]["status"] == "current"
    r = api.smart_search(str(folder / ".kestrel"), "blue")  # .kestrel path is accepted too
    assert r["success"] and r["scenes"][0]["scene_id"] == "2"
    r = api.smart_search(f, "mourning dove", 48, {"disabled_terms": ["mourning dove"]})
    assert r["success"] and r["chips"] == []
    assert api.get_smart_search_index_progress("nope")["success"] is False
    assert api.cancel_smart_search_index("nope")["success"] is False


def _upgrade_rows(api):
    return api.get_queue_status()["upgrades"]


def test_queue_status_reports_finished_upgrade_until_dismissed(api, folder):
    st = api.get_queue_status()
    assert st["upgrades"] == [] and "items" in st and "running" in st  # analysis keys unchanged
    job = api.upgrade_folder_v3(str(folder))
    _wait(api, job["job_id"])
    (row,) = _upgrade_rows(api)
    assert row["job_id"] == job["job_id"] and row["kind"] == "upgrade"
    assert row["path"] == str(folder) and row["name"] == folder.name
    assert row["status"] == "done" and row["phase"] == "complete"
    assert row["current"] == row["total"] == 4 and row["error"] == ""
    assert row["finished_at"] >= row["started_at"]
    json.dumps(api.get_queue_status())  # the bridge payload must stay JSON-serialisable
    assert api.clear_queue_done()["success"]
    assert _upgrade_rows(api) == []


def _blocking_upgrade(monkeypatch, result_on_cancel):
    """upgrade_folder that reports 1/3 then waits for the cancel event."""
    import threading

    started = threading.Event()

    def fake(kestrel_dir, pipeline, progress_cb=None, cancel_event=None, should_stop=None, log_path=None):
        progress_cb(1, 3, "A.CR3")
        started.set()
        assert cancel_event.wait(5)
        return dict(result_on_cancel)

    monkeypatch.setattr(upgrade_v3, "upgrade_folder", fake)
    return started


def test_queue_status_running_upgrade_and_cancel(api, folder, monkeypatch):
    started = _blocking_upgrade(monkeypatch, {"canceled": True})
    job = api.upgrade_folder_v3(str(folder))
    assert started.wait(5)
    (row,) = _upgrade_rows(api)
    assert row["status"] == "running" and row["phase"] == "upgrading"
    assert (row["current"], row["total"]) == (1, 3) and row["finished_at"] is None
    assert api.clear_queue_done()["success"]
    assert [r["job_id"] for r in _upgrade_rows(api)] == [job["job_id"]]  # running rows are not dismissed
    assert api.cancel_smart_search_index(job["job_id"]) == {"success": True}
    assert _wait(api, job["job_id"])["phase"] == "canceled"
    (row,) = _upgrade_rows(api)
    assert row["status"] == "cancelled" and row["current"] == 1


def test_queue_status_upgrade_failure_and_pause(api, folder, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(upgrade_v3, "upgrade_folder", boom)
    job = api.upgrade_folder_v3(str(folder))
    _wait(api, job["job_id"])
    (row,) = _upgrade_rows(api)
    assert row["status"] == "error" and "disk full" in row["error"]

    monkeypatch.setattr(upgrade_v3, "upgrade_folder", lambda *a, **k: {"stopped": True})
    job2 = api.upgrade_folder_v3(str(folder))
    _wait(api, job2["job_id"])
    (row,) = _upgrade_rows(api)  # a new run replaces the folder's finished row
    assert row["job_id"] == job2["job_id"] and row["status"] == "stopped"


def test_queue_status_survives_a_broken_job_registry(api, monkeypatch):
    monkeypatch.setattr(api_bridge.Api, "_upgrade_jobs_status", lambda self: 1 / 0)
    assert api.get_queue_status()["upgrades"] == []


def test_bridge_old_index_name_starts_the_upgrade(api, folder):
    job = api.smart_search_index_folder(str(folder))
    assert job["success"]
    assert _wait(api, job["job_id"])["phase"] == "complete"


def test_bridge_refuses_too_old_folders(api, tmp_path):
    root = tmp_path / "old"
    _make_folder(root, [("A.CR3", 1, "Song Sparrow", [(250, 0, 0)])], version="1.7.0")
    r = api.upgrade_folder_v3(str(root))
    assert r["success"] is False and r["error"] == "reanalyze_required"
    assert api.smart_search_status(str(root))["upgrade"]["status"] == "reanalyze"


def test_bridge_rejects_bad_paths_and_input(api, tmp_path, folder, monkeypatch):
    assert api.smart_search(str(tmp_path / "missing"), "red")["success"] is False
    assert api.smart_search("", "red")["success"] is False
    assert api.smart_search(str(folder), 123)["success"] is False
    assert api.upgrade_folder_v3(str(tmp_path / "missing"))["success"] is False
    assert api.smart_search_status("")["success"] is False
    monkeypatch.setattr(api_bridge, "_ALLOWED_ROOT", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    for call in (lambda: api.smart_search(str(folder), "red"),
                 lambda: api.smart_search_status(str(folder)),
                 lambda: api.upgrade_folder_v3(str(folder))):
        r = call()
        assert r["success"] is False and "outside" in r["error"].lower()


def test_bridge_refuses_upgrade_during_analysis(api, folder, monkeypatch):
    monkeypatch.setattr(api_bridge._queue_manager, "has_running_writer", lambda root: True)
    assert api.upgrade_folder_v3(str(folder)) == {"success": False, "error": "analysis_running"}


def test_bridge_unavailable_without_assets(api, folder, monkeypatch):
    monkeypatch.setattr(siglip, "assets_available", lambda *a, **k: False)
    assert api.smart_search(str(folder), "red")["error"] == "smart_search_unavailable"
    assert api.smart_search_status(str(folder))["available"] is False


def test_settings_are_sanitised():
    import settings_utils

    out = settings_utils._sanitize_settings_payload({"smart_search_enabled": "false"})
    assert out["smart_search_enabled"] is False
    assert settings_utils._sanitize_settings_payload({"smart_search_enabled": True})["smart_search_enabled"] is True
    s = settings_utils._sanitize_settings_payload
    assert s({"quality_model": "legacy"})["quality_model"] == "legacy"
    assert s({"quality_model": "bogus"})["quality_model"] == "siglip"
    assert s({"tower_device": "cpu"})["tower_device"] == "cpu"
    assert s({"tower_device": 7})["tower_device"] == "auto"


def test_read_kestrel_csv_applies_the_quality_model_lazily(api, folder, monkeypatch):
    import pandas as pd

    kdir = str(folder / ".kestrel")
    _upgrade(kdir)  # siglip effective
    monkeypatch.setattr(api_bridge, "load_persisted_settings", lambda: {"quality_model": "legacy"})
    r = api.read_kestrel_csv(str(folder))
    assert r["success"]
    df = pd.read_csv(os.path.join(kdir, "kestrel_database.csv"))
    rows = df[df["quality_siglip"] >= 0]
    assert len(rows) and (rows["quality"] - rows["quality_legacy"]).abs().max() < 1e-6
    # Not while the folder is being analysed.
    monkeypatch.setattr(api_bridge, "load_persisted_settings", lambda: {"quality_model": "siglip"})
    monkeypatch.setattr(api_bridge._queue_manager, "has_running_writer", lambda root: True)
    api.read_kestrel_csv(str(folder))
    df2 = pd.read_csv(os.path.join(kdir, "kestrel_database.csv"))
    assert (df2["quality"] - df2["quality_legacy"]).abs().max() < 1e-6
