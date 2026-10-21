"""Shared helpers: eval-set construction and IOC taxonomy / label strings.

Everything keys off the IOC v15.1 catalog bundled with the desktop app
(analyzer/models/birds/birds_global.csv), so label sets, geofences and the
ground truth all live in one namespace: the IOC scientific name.
"""
import csv
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
CATALOG = os.path.join(REPO, "analyzer", "models", "birds", "birds_global.csv")
WORK = os.environ.get("BIOCLIP_WORK", "/home/user/bioclip-work")
DATA = os.path.join(WORK, "data")
CACHE = os.path.join(WORK, "cache")

# Hand fixes for scene labels that don't resolve by name/alias.
LABEL_FIXES = {"great white pelinan": "Great White Pelican"}
# AOS names whose IOC namesake is the Old World taxon. Applied to perches in
# the Americas and to the (North-American-trained) CNN's outputs. Note the
# desktop catalog (birds_global.csv) maps both of these to the Old World species.
AMERICAS_FIXES = {"Sandwich Tern": "Cabot's Tern", "Barn Owl": "American Barn Owl"}

# Which perches to evaluate lives in a private, uncommitted config (perch
# slugs are unlisted links). Schema: see eval_config.example.json.
CONFIG = os.environ.get("BIOCLIP_EVAL_CONFIG", os.path.join(WORK, "eval_config.json"))


def load_config():
    if not os.path.exists(CONFIG):
        return {"perches": {}, "exclude": set()}
    cfg = json.load(open(CONFIG))
    cfg["exclude"] = {(s, lab) for s, lab, *_ in cfg.get("exclude_scene_labels", [])}
    return cfg


_CFG = load_config()
PERCHES = {k: v["name"] for k, v in _CFG["perches"].items()}
AMERICAN_SLUGS = {k for k, v in _CFG["perches"].items() if v["region"] == "us"}
CHECKLIST = {k: v.get("checklist") for k, v in _CFG["perches"].items()}
EXCLUDE = _CFG["exclude"]


def _norm(s):
    return re.sub(r"[^a-z]", "", s.lower())


def load_catalog():
    """List of dicts, one per IOC species, with taxonomy + label strings."""
    rows = list(csv.DictReader(open(CATALOG, encoding="utf-8-sig")))
    for r in rows:
        r["genus"] = r["scientific_name"].split()[0]
        r["regions_set"] = set(r["regions"].split(","))
    return rows


def name_index(catalog):
    idx = {}
    for r in catalog:
        for n in [r["canonical_common_name"]] + [a for a in r["aliases"].split("|") if a]:
            idx.setdefault(_norm(n), r)
    return idx


def resolve(label, idx, americas=False):
    label = LABEL_FIXES.get(label, label)
    if americas:
        label = AMERICAS_FIXES.get(label, label)
    return idx.get(_norm(label))


# --- Prompt text types ------------------------------------------------------
# BioCLIP / BioCLIP 2 were trained on five caption types per image
# (src/training/data.py in Imageomics/bioclip-2): sci, com, taxon, sci_com,
# taxon_com. The BioCLIP paper gives the combined form as
# "<taxonomy> with common name <common>". We reproduce each verbatim.
def text_for(r, text_type):
    taxon = " ".join(["Animalia", "Chordata", "Aves", r["order"], r["family_sci"], r["scientific_name"]])
    com = r["canonical_common_name"]
    sci = r["scientific_name"]
    return {
        "com": com,
        "sci": sci,
        "taxon": taxon,
        "sci_com": f"{sci} with common name {com}",
        "taxon_com": f"{taxon} with common name {com}",
    }[text_type]


TEXT_TYPES = ["com", "sci", "taxon", "sci_com", "taxon_com"]

# Template sets. "single" is the plain CLIP prompt; "openai80" is the
# 80-template ImageNet ensemble that BioCLIP's own NABirds eval uses
# (imported from open_clip so the strings are exactly theirs).
def templates(name):
    if name == "single":
        return [lambda c: f"a photo of {c}."]
    if name == "openai80":
        from open_clip.zero_shot_metadata import OPENAI_IMAGENET_TEMPLATES
        return list(OPENAI_IMAGENET_TEMPLATES)
    if name == "bird7":  # a small bird-photography-flavoured set, our own
        return [
            lambda c: f"a photo of {c}.",
            lambda c: f"a photo of a {c}, a type of bird.",
            lambda c: f"a wildlife photograph of {c}.",
            lambda c: f"a close-up photo of {c}.",
            lambda c: f"a blurry photo of {c}.",
            lambda c: f"a photo of {c} in flight.",
            lambda c: f"a photo of {c} perched on a branch.",
        ]
    raise ValueError(name)


def load_eval_crops(single_species_only=True):
    """Crops from labelled scenes, with the scene's IOC truth attached.

    A scene is kept when every label resolves to an IOC bird and (by default)
    there is exactly one. Scenes tagged with a non-bird ("Mammal", "Domestic
    Duck", "Unknown") alongside a bird are dropped: the crops could be either.
    """
    cat = load_catalog()
    idx = name_index(cat)
    out = []
    for row in csv.DictReader(open(os.path.join(DATA, "crops_index.csv"))):
        labels = json.loads(row["scene_species"])
        if not labels:
            continue
        res = [resolve(l, idx, americas=row["slug"] in AMERICAN_SLUGS) for l in labels]
        if any(r is None for r in res):
            continue
        sci = sorted({r["scientific_name"] for r in res})
        if single_species_only and len(sci) != 1:
            continue
        if any((row["slug"], r["canonical_common_name"]) in EXCLUDE for r in res):
            continue
        row["truth"] = sci  # list of IOC scientific names
        row["truth_family"] = sorted({r["family_sci"] for r in res})
        row["abspath"] = os.path.join(DATA, row["path"])
        out.append(row)
    return out
