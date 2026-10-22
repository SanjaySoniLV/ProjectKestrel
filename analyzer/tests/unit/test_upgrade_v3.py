"""Pipeline-3 upgrade of an existing folder and the free quality-scorer switch.

Built from the real legacy fixtures in fixtures/legacy_databases (plus tiny crop
/ export JPEGs) with stubbed BioCLIP / SigLIP towers, so it runs in the fast
lane. The key property: the upgrade and the switch never touch the user's
decisions -- culling marks, manual star ratings, scene names / tags, the
primary-crop choice -- and never delete a detection.
"""

import json
import os
import shutil
import sys
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import quality_model as qm
from kestrel_analyzer import smart_search as ss
from kestrel_analyzer import upgrade_v3
from kestrel_analyzer.ml import siglip

pytestmark = pytest.mark.unit

FIX = Path(__file__).parent.parent / "fixtures" / "legacy_databases"


def _axis(i):
    v = np.zeros(siglip.DIM, np.float32)
    v[0] = 1.0
    v[i] = 1.0
    return v / np.linalg.norm(v)


class _Species:
    def set_regions(self, regions):
        self.regions = list(regions)

    def embed(self, img):
        return _axis(3)[None]

    def route(self, emb):
        return {"route": "bird", "bird_prob": 0.98, "wildlife_prob": 0.02, "wildlife_label": None,
                "wildlife_scientific": "", "wildlife_family": "", "wildlife_class": "", "wildlife_confidence": 0.0}

    def classify_embedding(self, emb, top_k=5):
        return {"top_species_labels": np.array(["Pine Siskin"], dtype=object), "top_species_scores": np.array([0.77]),
                "top_family_labels": ["Finch sp."], "top_family_scores": [0.88]}


class _Siglip:
    def embed(self, img):
        return _axis(4)[None]


class _Quality:
    def score(self, emb):
        return 0.912


class _Gate:
    def __init__(self, score=0.9):
        self.score = score

    def animal_score(self, emb):
        return self.score


class _Models:
    def __init__(self, gate_score=0.9):
        self.species_clf = _Species()
        self.siglip_img = _Siglip()
        self.siglip_quality = _Quality()
        self.animal_gate = _Gate(gate_score)
        self.sn_sam = None


def _build(tmp_path, stem="v_file-size", culled=True):
    from PIL import Image

    root = tmp_path / "shoot"
    kdir = root / ".kestrel"
    (kdir / "crop").mkdir(parents=True)
    (kdir / "export").mkdir()
    df = pd.read_csv(FIX / f"{stem}_kestrel_database.csv")
    if culled:
        df["culled"] = ["reject", "", "accept", ""]
        df["culled_origin"] = ["manual", "", "manual", ""]
    df.to_csv(kdir / "kestrel_database.csv", index=False)
    shutil.copy(FIX / f"{stem}_kestrel_metadata.json", kdir / "kestrel_metadata.json")
    sd_src = FIX / f"{stem}_kestrel_scenedata.json"
    sd = json.loads(sd_src.read_text()) if sd_src.exists() else {"version": "2.0", "image_ratings": {}, "scenes": {}}
    if "2" in sd.get("scenes", {}):
        sd["scenes"]["2"]["user_tags"] = {"species": ["Yellow Warbler"], "families": [], "finalized": True}
        sd["scenes"]["2"]["name"] = "warblers at the pond"
    sd["image_ratings"] = {"IMG_2014.CR3": {"rating": 5, "origin": "manual"}}
    (kdir / "kestrel_scenedata.json").write_text(json.dumps(sd, indent=2))
    for fn in df["filename"]:
        base = os.path.splitext(fn)[0]
        Image.new("RGB", (64, 64), (90, 120, 60)).save(kdir / "crop" / f"{base}_crop_0.jpg")
        Image.new("RGB", (96, 64), (40, 80, 160)).save(kdir / "export" / f"{base}_export.jpg")
    return root, kdir


def test_eligibility_rules(tmp_path):
    _, kdir = _build(tmp_path)
    e = upgrade_v3.eligibility(str(kdir))
    assert e["status"] == "upgradable" and e["version"] == "2.0.4" and e["missing"] == 4
    # Kentucky-Warbler (1.7.0) predates the 2.0.0 detector/crop pipeline.
    _, k_old = _build(tmp_path / "old", stem="v_Kentucky-Warbler")
    assert upgrade_v3.eligibility(str(k_old))["status"] == "reanalyze"
    assert upgrade_v3.eligibility(str(tmp_path / "nothing"))["status"] == "not_analyzed"
    # Crops deleted -> re-analyse.
    shutil.rmtree(kdir / "crop")
    (kdir / "crop").mkdir()
    assert upgrade_v3.eligibility(str(kdir))["status"] == "reanalyze"


def test_upgrade_keeps_user_decisions(tmp_path):
    _, kdir = _build(tmp_path)
    before = pd.read_csv(kdir / "kestrel_database.csv")
    sd_before = (kdir / "kestrel_scenedata.json").read_bytes()
    stats = upgrade_v3.upgrade_folder(str(kdir), _Models(), quality_model="siglip")
    assert stats["upgraded"] == 4 and stats["failed"] == 0 and stats["backup"].startswith("OLD_kestrel_database_pre-v3_")
    after = pd.read_csv(kdir / "kestrel_database.csv")

    # Scenedata (ratings, names, tags, membership) is byte-for-byte untouched.
    assert (kdir / "kestrel_scenedata.json").read_bytes() == sd_before
    # Culling marks and the primary-crop choice survive.
    assert after["culled"].fillna("").tolist() == ["reject", "", "accept", ""]
    assert after["culled_origin"].fillna("").tolist() == ["manual", "", "manual", ""]
    assert after["primary_crop_index"].tolist() == before["primary_crop_index"].tolist()
    # Every detection is kept.
    for a, b in zip(before["crops_json"], after["crops_json"]):
        assert len(json.loads(a)) == len(json.loads(b))
    # Old quality is now the legacy score; the effective one is SigLIP.
    assert np.allclose(after["quality_legacy"], before["quality"], atol=1e-6)
    assert np.allclose(after["quality_siglip"], 0.912) and np.allclose(after["quality"], 0.912)
    # Scene 1 (no user tags) re-IDed; scene 2 (finalized tags) kept its species.
    assert after.loc[after.filename == "IMG_2014.CR3", "species"].item() == "Pine Siskin"
    assert after.loc[after.filename == "IMG_3067.CR3", "species"].item() == "Yellow Warbler"
    crop = json.loads(after.loc[after.filename == "IMG_3067.CR3", "crops_json"].item())[0]
    assert crop["species"] == "Yellow Warbler" and crop["quality_siglip"] == pytest.approx(0.912)
    assert crop["quality_legacy"] == pytest.approx(before.loc[2, "quality"], abs=1e-6)
    meta = json.loads((kdir / "kestrel_metadata.json").read_text())
    assert meta["pipeline_major"] == 3 and meta["schema_version"] == 3 and meta["quality_model"] == "siglip"
    assert meta["version"] == "2.0.4"  # creation version is history, not overwritten
    assert upgrade_v3.eligibility(str(kdir))["status"] == "current"
    cov = ss.coverage(str(kdir))
    assert cov["indexed_images"] == cov["indexable_images"] == 4
    assert cov["frame_indexed_images"] == 4


def test_gate_only_flags_in_an_upgrade(tmp_path):
    _, kdir = _build(tmp_path)
    stats = upgrade_v3.upgrade_folder(str(kdir), _Models(gate_score=0.01), quality_model="siglip")
    assert stats["flagged"] == 4
    after = pd.read_csv(kdir / "kestrel_database.csv")
    crops = [json.loads(c) for c in after["crops_json"]]
    assert all(len(c) == 1 and c[0]["gate_flagged"] is True for c in crops)


def test_upgrade_is_resumable_and_idempotent(tmp_path):
    _, kdir = _build(tmp_path)
    calls = {"n": 0}
    ev = threading.Event()

    def progress(done, total, fn):
        calls["n"] = done
        if done >= 2:
            ev.set()

    s1 = upgrade_v3.upgrade_folder(str(kdir), _Models(), progress_cb=progress, cancel_event=ev, quality_model="siglip")
    assert s1["canceled"] and s1["upgraded"] == 2
    meta = json.loads((kdir / "kestrel_metadata.json").read_text())
    assert meta.get("pipeline_major", 0) < 3          # not stamped until complete
    s2 = upgrade_v3.upgrade_folder(str(kdir), _Models(), quality_model="siglip")
    assert s2["total"] == 2 and s2["upgraded"] == 2 and s2["backup"] == ""  # one backup only
    assert len([n for n in os.listdir(kdir) if n.startswith(upgrade_v3.BACKUP_PREFIX)]) == 1
    assert upgrade_v3.upgrade_folder(str(kdir), _Models())["total"] == 0


def test_quality_model_switch_is_free(tmp_path):
    _, kdir = _build(tmp_path)
    upgrade_v3.upgrade_folder(str(kdir), _Models(), quality_model="siglip")
    sd_before = (kdir / "kestrel_scenedata.json").read_bytes()
    assert not qm.needs_apply(str(kdir), "siglip")
    assert qm.needs_apply(str(kdir), "legacy")
    assert qm.apply_quality_model(str(kdir), "legacy")
    df = pd.read_csv(kdir / "kestrel_database.csv")
    assert np.allclose(df["quality"], df["quality_legacy"])
    for cj in df["crops_json"]:
        for c in json.loads(cj):
            assert c["quality"] == pytest.approx(c["quality_legacy"])
    assert df["culled"].fillna("").tolist() == ["reject", "", "accept", ""]
    assert (kdir / "kestrel_scenedata.json").read_bytes() == sd_before
    assert qm.folder_quality_model(str(kdir)) == "legacy" and not qm.needs_apply(str(kdir), "legacy")
    assert qm.apply_quality_model(str(kdir), "siglip")
    assert np.allclose(pd.read_csv(kdir / "kestrel_database.csv")["quality"], 0.912)


def test_switch_is_a_noop_for_pre_v3_folders(tmp_path):
    _, kdir = _build(tmp_path)
    before = (kdir / "kestrel_database.csv").read_bytes()
    assert not qm.needs_apply(str(kdir), "siglip")   # no quality_siglip column yet
    assert not qm.apply_quality_model(str(kdir), "siglip")
    assert (kdir / "kestrel_database.csv").read_bytes() == before
