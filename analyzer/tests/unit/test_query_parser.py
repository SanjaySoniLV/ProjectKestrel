"""Smart-search query parsing: species / family / wildlife detection in free text."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import query_parser as qp

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def index():
    return qp.shared_index()


def _parse(index, q, **kw):
    kw.setdefault("regions", ["NA"])
    p = qp.parse_query(q, index=index, **kw)
    return p, [(m.kind, m.label) for m in p.matches]


@pytest.mark.parametrize("q,expected,remainder", [
    ("Yellow Warbler", [("species", "Yellow Warbler")], ""),
    ("yellow warblers", [("species", "Yellow Warbler")], ""),            # plural
    ("canada geese flying", [("species", "Canada Goose")], "flying"),   # irregular plural
    ("great blue herons fishing", [("species", "Great Blue Heron")], "fishing"),
    ("Cooper's hawk perched", [("species", "Cooper's Hawk")], "perched"),
    ("coopers hawk", [("species", "Cooper's Hawk")], ""),                # apostrophe folded
    ("Anas platyrhynchos", [("species", "Mallard")], ""),               # scientific name
    ("Black-crowned Night Heron", [("species", "Black-crowned Night-Heron")], ""),  # alias/hyphens
])
def test_species(index, q, expected, remainder):
    p, got = _parse(index, q)
    assert got == expected
    assert p.remainder == remainder


@pytest.mark.parametrize("q,family", [
    ("heron in a marsh", "Heron/Egret/Bittern sp."),
    ("dove pair", "Pigeon/Dove sp."),
    ("hawks", "Hawk/Eagle/Kite sp."),
    ("sparrows on a fence", "Sparrow sp."),
])
def test_family_words(index, q, family):
    _, got = _parse(index, q)
    assert got == [("family", family)]


@pytest.mark.parametrize("q,label", [
    ("yelow warbler", "Yellow Warbler"),          # one typo
    ("chipping sparow", "Chipping Sparrow"),
    ("great blue heorn", "Great Blue Heron"),
])
def test_typos(index, q, label):
    p, got = _parse(index, q)
    assert got == [("species", label)]
    assert p.matches[0].fuzzy is True


def test_longest_match_wins(index):
    # "blue heron" must not become two filters; the 3-word species wins.
    _, got = _parse(index, "great blue heron")
    assert got == [("species", "Great Blue Heron")]


def test_wildlife(index):
    _, got = _parse(index, "white-tailed deer in snow")
    assert got == [("wildlife", "White-Tailed Deer")]


@pytest.mark.parametrize("q", [
    "bird in flight", "perched on a wire", "two birds fighting", "blurry", "bird on a rail",
    "eating", "wide landscape", "a swift bird",
])
def test_plain_descriptions_have_no_filter(index, q):
    p, got = _parse(index, q)
    assert got == []
    assert p.remainder == q


def test_folder_group_word(index):
    # "robin" is no catalog name on its own, but the folder has an American Robin.
    p, got = _parse(index, "robin eating", folder_species=["American Robin", "Song Sparrow"])
    assert got == [("group", "robin")]
    assert p.matches[0].labels == ("American Robin",)
    assert p.remainder == "eating"
    _, got = _parse(index, "robin eating", folder_species=["Song Sparrow"])
    assert got == []


def test_folder_tags_break_ties(index):
    # Prefer the candidate tagged in the folder when a phrase is ambiguous.
    p, _ = _parse(index, "barn owl", folder_species=["Barn Owl"])
    assert p.matches[0].label == "Barn Owl"


def test_disabled_terms_stay_text(index):
    p, got = _parse(index, "mourning dove pair", disabled_terms=["mourning dove"])
    assert got == []
    assert p.remainder == "mourning dove pair"


def test_semantic_text(index):
    p, _ = _parse(index, "heron in a marsh")
    assert qp.semantic_text(p) == "heron in a marsh"     # whole query when a filter matched
    p, _ = _parse(index, "yellow warbler")
    assert qp.semantic_text(p) == ""                     # filter-only
    p, _ = _parse(index, "yellow warbler photo")
    assert qp.semantic_text(p) == ""                     # filler words carry nothing
    p, _ = _parse(index, "bird in flight")
    assert qp.semantic_text(p) == "bird in flight"


def test_normalisation_helpers():
    assert qp.normalize("Cooper’s  Hawk!") == "coopers hawk"
    assert qp.singular("geese") == "goose"
    assert qp.singular("flies") == "fly"
    assert qp.singular("finches") == "finch"
    assert qp.singular("albatrosses") == "albatross"
    assert qp.singular("grass") == "grass"
    assert qp.family_parts("Hawk/Eagle/Kite sp.") == ["Hawk", "Eagle", "Kite", "Hawk/Eagle/Kite"]
    assert qp._lev_within("warbler", "warbelr", 2) and not qp._lev_within("warbler", "sparrow", 2)


# ── classify_query: the unified search box routes on this ──────────────────

@pytest.mark.parametrize("q", [
    "Spoonbill", "roseate spoonbill", "Roseate Spoonbill", "  ROSEATE   spoonbill ",
    "spoonbills", "roseate spoonbils",          # plural / one typo
    "Anas platyrhynchos",                        # scientific name
    "hawks", "heron", "Canada geese", "Spoonbill and Roseate Spoonbill",
])
def test_classify_pure_filter(index, q):
    r = qp.classify_query(q, index=index)
    assert r.kind == "filter"
    assert r.labels


@pytest.mark.parametrize("q", [
    "Spoonbill wading", "bird in flight at sunset", "dove pair", "two spoonbills",
    "heron in a marsh", "blurry", "perched on a wire", "sunset",
])
def test_classify_semantic(index, q):
    assert qp.classify_query(q, index=index).kind == "semantic"


@pytest.mark.parametrize("q", ["", "   ", None])
def test_classify_empty(index, q):
    assert qp.classify_query(q, index=index).kind == "empty"


def test_classify_labels_are_tag_values(index):
    assert qp.classify_query("roseate spoonbills", index=index).labels == ("Roseate Spoonbill",)
    assert qp.classify_query("hawks", index=index).labels == ("Hawk/Eagle/Kite sp.",)


def test_classify_uses_folder_group_words(index):
    # "robin" alone is a folder-aware group word only when the folder has one.
    r = qp.classify_query("robin", index=index, folder_species=["American Robin"])
    assert r.kind == "filter" and "American Robin" in r.labels
