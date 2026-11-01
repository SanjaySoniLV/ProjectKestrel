"""Per-folder settings: the settings file, folder info, and species-region re-scoring.

Re-scoring runs the real BioCLIP text assets (small, always committed) against
embeddings we store ourselves, with the image tower skipped
(``load_image_tower=False``), so it is deterministic and needs no ONNX model.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
from kestrel_analyzer import embedding_store, folder_settings as fs
from kestrel_analyzer.bird_catalog import get_catalog
from kestrel_analyzer.config import BIOCLIP_MODELS_DIR, DATABASE_NAME, METADATA_FILENAME
from kestrel_analyzer.database import ensure_columns, read_database_csv, save_database
from kestrel_analyzer.ml import bioclip_species as bs

pytestmark = pytest.mark.unit

_HAVE_ASSETS = (
    (Path(BIOCLIP_MODELS_DIR) / bs.TEXT_EMB_FILENAME).is_file()
    and (Path(BIOCLIP_MODELS_DIR) / bs.INDEX_FILENAME).is_file()
    and (Path(BIOCLIP_MODELS_DIR) / bs.TEXT_EMB_FILENAME).stat().st_size > 1_000_000
)
needs_assets = pytest.mark.skipif(not _HAVE_ASSETS, reason="BioCLIP text assets missing")

# Common Blackbird lives in PAL/OR/AU only; Barn Swallow is Worldwide.
EURASIAN = "Common Blackbird"


@pytest.fixture(scope="module")
def clf():
    if not _HAVE_ASSETS:
        pytest.skip("BioCLIP text assets missing")
    return bs.BioClipSpeciesClassifier(str(BIOCLIP_MODELS_DIR), None, load_image_tower=False)


def _text_vec(clf, common_name):
    rec = get_catalog().lookup(common_name)
    assert rec is not None, common_name
    return clf.text_emb[clf.scientific_names.index(rec.scientific_name)].copy()


def _crop(ci, species="Barn Swallow", subject="bird", di=None):
    return {"crop_index": ci, "detection_index": ci if di is None else di, "crop_path": f"crop/x_{ci}.jpg",
            "species": species, "species_confidence": 0.5, "family": "Swallows", "family_confidence": 0.5,
            "subject": subject}


def _make_folder(tmp_path, clf, images, scenedata=None, meta=None):
    """images: {filename: (scene, [crops])}; every non-wildlife crop embeds as a Common Blackbird."""
    kdir = tmp_path / ".kestrel"
    kdir.mkdir()
    rows = []
    for fn, (scene, crops) in images.items():
        rows.append({"filename": fn, "scene_count": scene, "species": crops[0]["species"],
                     "species_confidence": 0.5, "family": "Swallows", "family_confidence": 0.5,
                     "crops_json": json.dumps(crops), "primary_crop_index": 0})
    df = ensure_columns(pd.DataFrame(rows))
    save_database(df, str(kdir / DATABASE_NAME))
    store = embedding_store.EmbeddingStore(str(kdir), model=bs.MODEL_ID)
    for fn, (_s, crops) in images.items():
        store.put(fn, {c["crop_index"]: _text_vec(clf, EURASIAN) for c in crops if c["subject"] != "wildlife"})
    if scenedata is not None:
        (kdir / "kestrel_scenedata.json").write_text(json.dumps(scenedata), encoding="utf-8")
    m = {"version": "3.0.0", "pipeline_major": 3, "species_regions": ["NA"],
         "analysis_settings": {"species_detection_enabled": True, "wildlife_enabled": True,
                               "species_regions": ["NA"]}}
    m.update(meta or {})
    (kdir / METADATA_FILENAME).write_text(json.dumps(m), encoding="utf-8")
    return str(kdir)


def _crops_of(kdir, fn):
    df = read_database_csv(str(Path(kdir) / DATABASE_NAME))
    return json.loads(df.loc[df["filename"] == fn, "crops_json"].iloc[0]), df.loc[df["filename"] == fn].iloc[0]


# ----------------------------------------------------------------- settings file

class TestSettingsFile:
    def test_defaults_when_missing(self, tmp_path):
        assert fs.read_folder_settings(str(tmp_path)) == {"ignore_wildlife": False}

    def test_round_trip(self, tmp_path):
        out = fs.write_folder_setting(str(tmp_path), "ignore_wildlife", True)
        assert out == {"ignore_wildlife": True}
        assert fs.read_folder_settings(str(tmp_path))["ignore_wildlife"] is True
        fs.write_folder_setting(str(tmp_path), "ignore_wildlife", False)
        assert fs.read_folder_settings(str(tmp_path))["ignore_wildlife"] is False

    def test_unknown_key_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            fs.write_folder_setting(str(tmp_path), "../evil", True)
        assert not (tmp_path / fs.SETTINGS_FILENAME).exists()

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path):
        (tmp_path / fs.SETTINGS_FILENAME).write_text("{not json", encoding="utf-8")
        assert fs.read_folder_settings(str(tmp_path)) == {"ignore_wildlife": False}

    def test_non_bool_values_are_coerced_not_trusted(self, tmp_path):
        (tmp_path / fs.SETTINGS_FILENAME).write_text('{"ignore_wildlife": "yes", "extra": 1}', encoding="utf-8")
        assert fs.read_folder_settings(str(tmp_path)) == {"ignore_wildlife": True}


# ----------------------------------------------------------------- support + info

@needs_assets
class TestSupportAndInfo:
    def test_unsupported_without_embeddings(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)])})
        import shutil
        shutil.rmtree(Path(kdir) / "embeddings")
        sup = fs.rescore_support(kdir)
        assert not sup["supported"] and "pipeline 3.0" in sup["reason"]

    def test_unsupported_when_species_detection_was_off(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)])},
                            meta={"analysis_settings": {"species_detection_enabled": False}})
        assert not fs.rescore_support(kdir)["supported"]

    def test_supported_with_embeddings(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)]), "b.jpg": (1, [_crop(0)])})
        sup = fs.rescore_support(kdir)
        assert sup["supported"] and sup["crops"] == 2

    def test_folder_info_reports_counts_and_date(self, tmp_path, clf):
        kdir = _make_folder(
            tmp_path, clf,
            {"a.jpg": (1, [_crop(0)]), "b.jpg": (2, [_crop(0, "Bear", "wildlife")])},
            meta={"analyzed_utc": "2026-05-01T12:00:00.000000Z"})
        info = fs.folder_info(kdir)
        assert info["image_count"] == 2 and info["scene_count"] == 2
        assert info["wildlife_crops"] == 1 and info["pipeline_major"] == 3
        assert info["analyzed_utc"].startswith("2026-05-01")
        assert info["rescore_supported"] is True
        assert info["settings"] == {"ignore_wildlife": False}

    def test_folder_info_without_date_leaves_it_empty(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)])})
        assert fs.folder_info(kdir)["analyzed_utc"] == ""


# ----------------------------------------------------------------- re-scoring

@needs_assets
class TestRescore:
    def test_region_change_rewrites_species_and_family(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0), _crop(1)])})
        stats = fs.rescore_species_regions(kdir, ["PAL"], clf)
        assert stats["updated"] == 2 and stats["changed"] == 2 and stats["regions"] == ["PAL"]
        crops, row = _crops_of(kdir, "a.jpg")
        assert [c["species"] for c in crops] == [EURASIAN, EURASIAN]
        assert row["species"] == EURASIAN
        assert 0.0 < float(row["species_confidence"]) <= 1.0
        assert json.loads(row["secondary_species_list"]) == [EURASIAN, EURASIAN]

    def test_region_that_excludes_the_species_changes_it_back(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0, EURASIAN)])})
        fs.rescore_species_regions(kdir, ["PAL"], clf)
        fs.rescore_species_regions(kdir, ["NA"], clf)
        crops, _ = _crops_of(kdir, "a.jpg")
        assert crops[0]["species"] != EURASIAN
        rec = get_catalog().lookup(crops[0]["species"])
        assert rec is not None and rec.matches_any_region(frozenset({"NA"}))

    def test_metadata_records_the_new_region(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)])})
        fs.rescore_species_regions(kdir, ["PAL"], clf)
        meta = json.loads((Path(kdir) / METADATA_FILENAME).read_text(encoding="utf-8"))
        assert meta["species_regions"] == ["PAL"]
        assert meta["analysis_settings"]["species_regions"] == ["PAL"]

    def test_user_edited_scene_images_are_rescored_but_scene_tags_kept(self, tmp_path, clf):
        scenedata = {"scenes": {
            "1": {"image_filenames": ["a.jpg"], "user_tags": {"species": ["Barn Swallow"], "families": [], "finalized": False}},
            "2": {"image_filenames": ["b.jpg"], "user_tags": {"species": [], "families": [], "finalized": False}},
        }}
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)]), "b.jpg": (2, [_crop(0)])}, scenedata=scenedata)
        sd_path = Path(kdir) / "kestrel_scenedata.json"
        before = sd_path.read_bytes()
        stats = fs.rescore_species_regions(kdir, ["PAL"], clf)
        assert stats["locked"] == 1 and stats["updated"] == 2 and stats["images"] == 2
        assert _crops_of(kdir, "a.jpg")[0][0]["species"] == EURASIAN
        assert _crops_of(kdir, "b.jpg")[0][0]["species"] == EURASIAN
        assert sd_path.read_bytes() == before

    def test_finalized_scene_images_change_but_scene_tags_do_not(self, tmp_path, clf):
        scenedata = {"scenes": {
            "1": {"image_filenames": ["a.jpg"], "user_tags": {"species": ["Barn Swallow"], "families": ["Swallows"], "finalized": True}},
        }}
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)])}, scenedata=scenedata)
        sd_path = Path(kdir) / "kestrel_scenedata.json"
        before = sd_path.read_bytes()
        stats = fs.rescore_species_regions(kdir, ["PAL"], clf)
        assert stats["locked"] == 1 and stats["updated"] == 1
        crops, row = _crops_of(kdir, "a.jpg")
        assert crops[0]["species"] == EURASIAN and row["species"] == EURASIAN
        assert json.loads(sd_path.read_text(encoding="utf-8"))["scenes"]["1"]["user_tags"]["species"] == ["Barn Swallow"]
        assert sd_path.read_bytes() == before

    def test_wildlife_crops_keep_their_label(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0), _crop(1, "Black Bear", "wildlife")])})
        stats = fs.rescore_species_regions(kdir, ["PAL"], clf)
        assert stats["wildlife_skipped"] == 1 and stats["updated"] == 1
        crops, _ = _crops_of(kdir, "a.jpg")
        assert crops[0]["species"] == EURASIAN and crops[1]["species"] == "Black Bear"

    def test_culling_and_ratings_columns_survive(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)])})
        db = Path(kdir) / DATABASE_NAME
        df = read_database_csv(str(db))
        df["culled"], df["culled_origin"] = "reject", "manual"
        save_database(df, str(db))
        fs.rescore_species_regions(kdir, ["PAL"], clf)
        row = read_database_csv(str(db)).iloc[0]
        assert row["culled"] == "reject" and row["culled_origin"] == "manual"

    def test_cancel_leaves_database_untouched(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)]), "b.jpg": (1, [_crop(0)])})
        before = (Path(kdir) / DATABASE_NAME).read_bytes()
        stats = fs.rescore_species_regions(kdir, ["PAL"], clf, cancel=lambda: True)
        assert stats["canceled"] is True
        assert (Path(kdir) / DATABASE_NAME).read_bytes() == before

    def test_progress_is_reported(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)]), "b.jpg": (1, [_crop(0)])})
        seen = []
        fs.rescore_species_regions(kdir, ["PAL"], clf, progress_cb=lambda d, t: seen.append((d, t)))
        assert seen[0] == (0, 2) and seen[-1] == (2, 2)

    def test_no_embeddings_raises(self, tmp_path, clf):
        kdir = _make_folder(tmp_path, clf, {"a.jpg": (1, [_crop(0)])})
        import shutil
        shutil.rmtree(Path(kdir) / "embeddings")
        with pytest.raises(RuntimeError):
            fs.rescore_species_regions(kdir, ["PAL"], clf)

    def test_text_only_classifier_refuses_to_embed(self, clf):
        with pytest.raises(RuntimeError):
            clf.embed(np.zeros((8, 8, 3), np.uint8))


# ----------------------------------------------------------------- bridge methods

class TestBridge:
    @pytest.fixture
    def api_and_folder(self, tmp_path):
        folder = tmp_path / "photos"
        (folder / ".kestrel").mkdir(parents=True)
        return api_bridge.Api(), str(folder)

    def test_settings_round_trip_through_bridge(self, api_and_folder):
        api, folder = api_and_folder
        assert api.get_folder_settings(folder) == {"success": True, "settings": {"ignore_wildlife": False}}
        res = api.set_folder_setting(folder, "ignore_wildlife", True)
        assert res["success"] and res["settings"]["ignore_wildlife"] is True
        assert api.get_folder_settings(folder)["settings"]["ignore_wildlife"] is True

    def test_bridge_rejects_bad_key_and_non_bool(self, api_and_folder):
        api, folder = api_and_folder
        assert not api.set_folder_setting(folder, "nope", True)["success"]
        assert not api.set_folder_setting(folder, "ignore_wildlife", "true")["success"]
        assert not api.set_folder_setting(folder, 5, True)["success"]

    def test_bridge_rejects_bad_paths(self, tmp_path):
        api = api_bridge.Api()
        missing = str(tmp_path / "does-not-exist")
        assert not api.get_folder_info(missing)["success"]
        assert not api.get_folder_settings(missing)["success"]
        assert not api.set_folder_setting(missing, "ignore_wildlife", True)["success"]
        assert not api.rescore_folder_species_regions(missing, ["NA"])["success"]

    def test_set_requires_kestrel_data(self, tmp_path):
        folder = tmp_path / "plain"
        folder.mkdir()
        assert not api_bridge.Api().set_folder_setting(str(folder), "ignore_wildlife", True)["success"]

    def test_rescore_requires_a_region(self, api_and_folder):
        api, folder = api_and_folder
        res = api.rescore_folder_species_regions(folder, [])
        assert not res["success"] and "region" in res["error"].lower()

    def test_rescore_refused_without_embeddings(self, api_and_folder):
        api, folder = api_and_folder
        res = api.rescore_folder_species_regions(folder, ["NA"])
        assert not res["success"]

    def test_status_is_idle_before_any_run(self, api_and_folder):
        api, folder = api_and_folder
        assert api.get_species_rescore_status(folder) == {"success": True, "state": "idle"}

    @needs_assets
    def test_rescore_end_to_end_through_bridge(self, tmp_path, clf):
        import time
        folder = tmp_path / "photos"
        folder.mkdir()
        kdir = _make_folder(folder, clf, {"a.jpg": (1, [_crop(0)])})
        api = api_bridge.Api()
        res = api.rescore_folder_species_regions(str(folder), ["PAL"])
        assert res["success"], res
        for _ in range(100):
            st = api.get_species_rescore_status(str(folder))
            if st["state"] != "running":
                break
            time.sleep(0.1)
        assert st["state"] == "done", st
        assert st["stats"]["changed"] == 1
        assert _crops_of(kdir, "a.jpg")[0][0]["species"] == EURASIAN
