"""Pipeline 3.0 model glue on synthetic embeddings: false-positive gate, bird/wildlife
routing, SigLIP quality calibration and the effective-quality selection. No weights."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml import animal_gate
from kestrel_analyzer.ml.animal_gate import AnimalGate
from kestrel_analyzer.ml.bioclip_species import BioClipSpeciesClassifier, format_display_name
from kestrel_analyzer.ml import siglip
from kestrel_analyzer import quality_model as qm

pytestmark = pytest.mark.unit

D = 16


def _unit(i, j=None, w=1.0):
    v = np.zeros(D, np.float32)
    v[i] = 1.0
    if j is not None:
        v[j] = w
    return v / np.linalg.norm(v)


# --------------------------------------------------------------------- gate


@pytest.fixture
def gate_dir(tmp_path):
    # 2 animal prompts (axes 0, 1) then 3 background prompts (axes 2, 3, 4).
    emb = np.stack([_unit(0), _unit(1), _unit(2), _unit(3), _unit(4)]).astype(np.float16)
    np.save(tmp_path / "gate_text_emb.npy", emb)
    (tmp_path / "gate_index.json").write_text(json.dumps({
        "animal": ["a bird", "an animal"], "background": ["grass", "bark", "sky"], "logit_scale": 100.0}))
    return tmp_path


def test_gate_scores_animals_high_background_low(gate_dir):
    g = AnimalGate(gate_dir)
    assert g.animal_score(_unit(0)) > 0.99            # bird-like crop
    assert g.animal_score(_unit(3)) < 0.01            # bark
    mixed = g.animal_score(_unit(0, 3))               # equidistant: split
    assert 0.3 < mixed < 0.7


def test_gate_threshold_mirrors_speciesnet_rule(gate_dir):
    g = AnimalGate(gate_dir)
    # SpeciesNet dropped a box when pred_score < detector threshold: same here.
    assert animal_gate.threshold_for(0.25) == 0.25
    assert g.keep_score(0.25, 0.25) and not g.keep_score(0.2499, 0.25)
    assert g.keep(_unit(1), 0.5) and not g.keep(_unit(4), 0.1)
    assert animal_gate.threshold_for("junk") == 0.25


def test_gate_method_is_pluggable(gate_dir, monkeypatch):
    monkeypatch.setitem(animal_gate.METHODS, "always", lambda gate, emb: 0.123)
    assert AnimalGate(gate_dir, method="always").animal_score(_unit(0)) == 0.123
    with pytest.raises(ValueError):
        AnimalGate(gate_dir, method="nope")


def test_gate_rejects_mismatched_assets(gate_dir):
    (gate_dir / "gate_index.json").write_text(json.dumps({"animal": ["a"], "background": ["b"]}))
    with pytest.raises(ValueError):
        AnimalGate(gate_dir)


# ------------------------------------------------------------------ routing


def _classifier(with_wildlife=True):
    c = BioClipSpeciesClassifier.__new__(BioClipSpeciesClassifier)
    c.logit_scale = 100.0
    c.text_emb = np.stack([_unit(0), _unit(1), _unit(2)])        # 3 bird species
    c._allowed = np.array([0, 1, 2])
    if with_wildlife:
        c.wildlife_emb = np.stack([_unit(5), _unit(6)])
        c.wildlife_labels = [
            {"common_name": "white-tailed deer", "scientific_name": "odocoileus virginianus",
             "class": "mammalia", "order": "artiodactyla", "family": "cervidae"},
            {"common_name": "eastern gray squirrel", "scientific_name": "sciurus carolinensis",
             "class": "mammalia", "order": "rodentia", "family": "sciuridae"},
        ]
    else:
        c.wildlife_emb = np.zeros((0, D), np.float32)
        c.wildlife_labels = []
    return c


def test_route_bird():
    r = _classifier().route(_unit(1))
    assert r["route"] == "bird" and r["bird_prob"] > 0.99


def test_route_wildlife_label_and_family():
    r = _classifier().route(_unit(6))
    assert r["route"] == "wildlife"
    assert r["wildlife_label"] == "Eastern Gray Squirrel"
    assert r["wildlife_family"] == "sciuridae"
    assert r["wildlife_confidence"] > 0.99


def test_route_respects_geofence():
    c = _classifier()
    # Crop halfway between bird species 2 and the deer: with species 2 geofenced
    # out, the wildlife side wins.
    emb = _unit(2, 5, 1.0)
    assert c.route(emb)["route"] in ("bird", "wildlife")
    c._allowed = np.array([0, 1])
    assert c.route(emb)["route"] == "wildlife"


def test_route_without_wildlife_assets_is_always_bird():
    assert _classifier(with_wildlife=False).route(_unit(5))["route"] == "bird"


def test_display_name_matches_speciesnet_casing():
    assert format_display_name("white-tailed deer") == "White-Tailed Deer"
    assert format_display_name("cane toad") == "Cane Toad"


# ------------------------------------------------------------------ quality


@pytest.fixture
def siglip_dir(tmp_path):
    cal = {"prompts": {"pos": "a sharp photo", "neg": "a blurry photo"},
           "knots_raw": [-0.1, 0.0, 0.1], "knots_quality": [0.0, 0.5, 1.0]}
    (tmp_path / siglip.CALIBRATION_FILENAME).write_text(json.dumps(cal))
    pos = np.zeros(siglip.DIM, np.float32); pos[0] = 1
    neg = np.zeros(siglip.DIM, np.float32); neg[1] = 1
    np.save(tmp_path / siglip.QUALITY_PROMPT_EMB_FILENAME, np.stack([pos, neg]))
    (tmp_path / "quality_prompt_emb.json").write_text(json.dumps({"prompts": ["a sharp photo", "a blurry photo"]}))
    return tmp_path


def test_quality_calibration_interpolates(siglip_dir):
    q = siglip.SiglipQualityScorer(siglip_dir)
    e = np.zeros(siglip.DIM, np.float32)
    assert q.score(e) == pytest.approx(0.5)                   # s = 0
    e[0] = 0.05
    assert q.score(e) == pytest.approx(0.75)                  # s = 0.05
    e[0] = 5.0
    assert q.score(e) == pytest.approx(1.0)                   # clamped at the last knot
    many = q.score(np.stack([np.zeros(siglip.DIM, np.float32), e]))
    assert many.shape == (2,)


def test_quality_prompt_asset_must_match_prompts(siglip_dir):
    (siglip_dir / "quality_prompt_emb.json").write_text(json.dumps({"prompts": ["other", "prompts"]}))

    class Enc:
        def encode_texts(self, texts):
            assert texts == ["a sharp photo", "a blurry photo"]
            v = np.zeros((2, siglip.DIM), np.float32)
            v[0, 2] = 1
            v[1, 3] = 1
            return v

    q = siglip.SiglipQualityScorer(siglip_dir, text_encoder=Enc())
    assert q.direction[2] == pytest.approx(1.0) and q.direction[3] == pytest.approx(-1.0)


def test_effective_quality_selection():
    assert qm.effective(0.8, 0.3, "siglip") == 0.8
    assert qm.effective(0.8, 0.3, "legacy") == 0.3
    assert qm.effective(-1, 0.3, "siglip") == 0.3            # falls back to the other scorer
    assert qm.effective("", -1.0, "legacy") == -1.0
    assert qm.effective(float("nan"), 0.4, "siglip") == 0.4
    assert qm.resolve("LEGACY") == "legacy" and qm.resolve(None) == "siglip"


def test_preprocess_shape_and_range():
    img = np.zeros((300, 500, 3), np.uint8)
    img[..., 0] = 255
    x = siglip.preprocess(img)
    assert x.shape == (3, 224, 224) and x.dtype == np.float32
    assert x[0].max() == pytest.approx(1.0) and x[1].min() == pytest.approx(-1.0)
