"""The sample sets bundled with the app: usable by the tutorial, smart search and
enhanced GPU validation, and free of machine-specific data (the repo is public)."""
import csv
import json
import re
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = ANALYZER / "sample_sets"
SETS = sorted(p for p in SAMPLE_ROOT.iterdir() if p.is_dir())
ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]|/Users/|/home/")


def _rows(sample_set):
    with open(sample_set / ".kestrel" / "kestrel_database.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_expected_sets_are_bundled():
    assert [p.name for p in SETS] == [
        "1_sonoran_desert", "2_houston_migration", "3_india", "4_high_island_galveston",
    ]


@pytest.mark.parametrize("sample_set", SETS, ids=lambda p: p.name)
def test_every_row_has_its_images(sample_set):
    rows = _rows(sample_set)
    assert rows
    for row in rows:
        for column in ("export_path", "crop_path"):
            value = row[column].replace("\\", "/")
            if value and value.lower() != "nan":
                assert (sample_set / value).is_file(), f"{row['filename']}: {value}"


@pytest.mark.parametrize("sample_set", SETS, ids=lambda p: p.name)
def test_every_frame_has_a_smart_search_embedding(sample_set):
    lines = (sample_set / ".kestrel" / "embeddings" / "siglip2-b16" / "index.jsonl").read_text(
        encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert (sample_set / ".kestrel" / "embeddings" / "siglip2-b16" / header["data"]).stat().st_size > 0
    indexed = {json.loads(line)["f"] for line in lines[1:]}
    # A frame the analysis left unrated (quality -1, species Unknown) has no embedding.
    assert {r["filename"] for r in _rows(sample_set) if float(r["quality"]) >= 0} <= indexed


@pytest.mark.parametrize("sample_set", SETS, ids=lambda p: p.name)
def test_no_machine_specific_data(sample_set):
    kestrel = sample_set / ".kestrel"
    assert not list(kestrel.glob("kestrel_error_*")), "analysis logs hold local paths"
    text_files = [p for p in kestrel.rglob("*") if p.suffix in {".csv", ".json", ".jsonl"}]
    assert text_files
    for path in text_files:
        assert not ABSOLUTE_PATH.search(path.read_text(encoding="utf-8")), path


@pytest.mark.parametrize("source", ["crop", "export"])
def test_enhanced_validation_finds_its_sample_images(source):
    from kestrel_analyzer.ml import enhanced_validate as ev
    assert len(ev._sample_images(source)) == ev._SAMPLE_COUNT
