"""Candidate label sets (lists of IOC scientific names) for restriction experiments.

Tiers, from loosest to tightest:
  global      all 11,251 IOC species
  continent   IOC breeding-range codes (NA for "us" perches, OR for "india")
              + "Worldwide" + Kestrel's 500 CNN species for the US perches
              (the CNN list is by construction a North American list, and
              adds introduced species / pelagics the breeding codes miss)
  checklist   a country/state checklist (geofence/<name>.txt, named per perch in the config),
              built by an LLM sub-agent from the IOC list blind to eval labels
  cnn500      exactly Kestrel's current CNN vocabulary (closed-set baseline)
  oracle      only the species actually present in the perch (upper bound)
"""
import os

from common import AMERICAN_SLUGS, CHECKLIST, HERE, load_catalog
from kestrel_cnn import cnn_labels_sci

GEO = os.environ.get("BIOCLIP_GEOFENCE", os.path.join(HERE, "geofence"))


def _codes(codes):
    return {r["scientific_name"] for r in load_catalog()
            if r["regions_set"] & codes or r["regions"] == "Worldwide"}


def cnn500():
    return {s for s in cnn_labels_sci()[1] if s}


def checklist(name):
    p = os.path.join(GEO, f"{name}.txt")
    return {l.strip() for l in open(p) if l.strip()} if os.path.exists(p) else None


def for_perch(slug, tier, rows=None):
    american = slug in AMERICAN_SLUGS
    if tier == "global":
        return {r["scientific_name"] for r in load_catalog()}
    if tier == "continent":
        return (_codes({"NA"}) | cnn500()) if american else _codes({"OR"})
    if tier == "continent+":  # wider: Middle America / Palearctic winterers
        return (_codes({"NA", "MA"}) | cnn500()) if american else _codes({"OR", "PAL"})
    if tier == "checklist":
        return checklist(CHECKLIST[slug])
    if tier == "cnn500":
        return cnn500()
    if tier == "oracle":
        return {r["truth"][0] for r in rows if r["slug"] == slug}
    raise ValueError(tier)
