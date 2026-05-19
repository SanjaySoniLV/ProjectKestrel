"""Unit tests for the bundled global-bird catalog and its fuzzy search.

Covers three contracts the rest of the app depends on:

  * Every ML model label resolves to a catalog record (compatibility).
  * Region filtering and alpha-code lookup behave deterministically.
  * Fuzzy search ranks the expected tier ordering for representative queries.
"""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.bird_catalog import (
    ALLOWED_REGION_CODES,
    BirdCatalog,
    BirdRecord,
    DEFAULT_REGION_SELECTION,
    REGION_LABELS,
    load_catalog,
    score_record,
    _is_subsequence,
    _token_prefix_match,
    _tokens,
)
from kestrel_analyzer.config import MODELS_DIR

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------------
# Module-scoped catalog (loaded once -- the CSV is ~1 MB)
# ----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def catalog() -> BirdCatalog:
    return BirdCatalog()


@pytest.fixture(scope="module")
def model_labels() -> list[str]:
    with open(MODELS_DIR / "labels.txt", encoding="utf-8-sig") as f:
        return [line.strip() for line in f if line.strip()]


# ----------------------------------------------------------------------------
# Region vocabulary
# ----------------------------------------------------------------------------

class TestRegionVocabulary:

    def test_default_selection_is_subset_of_allowed(self):
        assert all(r in ALLOWED_REGION_CODES for r in DEFAULT_REGION_SELECTION)

    def test_labels_cover_every_allowed_code(self):
        # Every allowed region must have a human-readable label for the UI.
        for code in ALLOWED_REGION_CODES:
            assert code in REGION_LABELS
            assert REGION_LABELS[code], f"label for {code} is empty"

    def test_no_extra_labels(self):
        # And no orphan labels left over from earlier iterations.
        for code in REGION_LABELS:
            assert code in ALLOWED_REGION_CODES


# ----------------------------------------------------------------------------
# Compatibility: every model label resolves
# ----------------------------------------------------------------------------

class TestModelLabelCoverage:
    """The 500 species the ML model can predict must each have a catalog row,
    otherwise pressing CTRL+SHIFT+R after a confident prediction would leave the
    user with no matching combobox entry."""

    def test_every_model_label_has_a_catalog_entry(self, catalog, model_labels):
        missing = [lbl for lbl in model_labels if catalog.lookup(lbl) is None]
        assert not missing, (
            f"{len(missing)} model labels are not in the catalog. "
            f"First few: {missing[:5]}"
        )

    def test_every_model_label_row_is_flagged(self, catalog, model_labels):
        unflagged = [lbl for lbl in model_labels
                     if (rec := catalog.lookup(lbl)) is not None and not rec.is_model_species]
        assert not unflagged, (
            f"{len(unflagged)} model labels resolve to rows missing the "
            f"is_model_species flag: {unflagged[:5]}"
        )

    def test_model_species_count_matches_labels_file(self, catalog, model_labels):
        flagged = sum(1 for r in catalog.records if r.is_model_species)
        assert flagged == len(model_labels)

    def test_model_species_have_scientific_names(self, catalog, model_labels):
        # No model species should have an empty scientific name. The combobox
        # uses this for the show-scientific-names toggle, and a blank value
        # would render an empty <em> block.
        empty_sci = [lbl for lbl in model_labels
                     if (rec := catalog.lookup(lbl)) is not None and not rec.scientific_name]
        assert not empty_sci, f"model labels with empty scientific names: {empty_sci}"

    def test_model_species_have_family_display_names(self, catalog, model_labels):
        empty_fam = [lbl for lbl in model_labels
                     if (rec := catalog.lookup(lbl)) is not None and not rec.family_common]
        assert not empty_fam, f"model labels with empty family display names: {empty_fam}"


# ----------------------------------------------------------------------------
# CSV invariants
# ----------------------------------------------------------------------------

class TestCsvInvariants:

    def test_canonical_names_unique(self, catalog):
        seen: dict[str, BirdRecord] = {}
        dupes: list[tuple[str, BirdRecord, BirdRecord]] = []
        for rec in catalog.records:
            key = rec.canonical_common_name.lower()
            if key in seen:
                dupes.append((rec.canonical_common_name, seen[key], rec))
            seen[key] = rec
        assert not dupes, f"duplicate canonical names: {dupes[:3]}"

    def test_alpha_codes_are_well_formed(self, catalog):
        bad = [r for r in catalog.records
               if r.alpha_4 and not (len(r.alpha_4) == 4 and r.alpha_4.isalpha() and r.alpha_4.isupper())]
        assert not bad, f"malformed alpha codes: {[r.canonical_common_name for r in bad[:5]]}"

    def test_regions_valid(self, catalog):
        allowed = set(ALLOWED_REGION_CODES)
        for rec in catalog.records:
            for region in rec.regions:
                assert region in allowed, (
                    f"unknown region {region!r} in {rec.canonical_common_name!r}"
                )

    def test_every_species_has_at_least_one_region(self, catalog):
        no_region = [r for r in catalog.records if not r.regions]
        assert not no_region, (
            f"{len(no_region)} species missing region: "
            f"{[r.canonical_common_name for r in no_region[:5]]}"
        )


# ----------------------------------------------------------------------------
# Region filtering
# ----------------------------------------------------------------------------

class TestRegionFilter:

    def test_filter_na_includes_american_robin(self, catalog):
        names = {r.canonical_common_name for r in catalog.filter(["NA"])}
        assert "American Robin" in names

    def test_filter_au_excludes_american_robin(self, catalog):
        names = {r.canonical_common_name for r in catalog.filter(["AU"])}
        assert "American Robin" not in names

    def test_empty_region_set_returns_empty(self, catalog):
        assert catalog.filter([]) == []
        assert catalog.filter([""]) == []

    def test_worldwide_species_match_any_selection(self, catalog):
        # Bank Swallow / Sand Martin is Worldwide in the IOC list, so it must
        # appear in every region's filter.
        for region in ("NA", "PAL", "AF", "AU", "SA"):
            names = {r.canonical_common_name for r in catalog.filter([region])}
            assert "Bank Swallow" in names, f"Worldwide species missing from {region}"

    def test_multi_region_is_union_not_intersection(self, catalog):
        na_only = {r.canonical_common_name for r in catalog.filter(["NA"])}
        pal_only = {r.canonical_common_name for r in catalog.filter(["PAL"])}
        both = {r.canonical_common_name for r in catalog.filter(["NA", "PAL"])}
        assert na_only.issubset(both)
        assert pal_only.issubset(both)


# ----------------------------------------------------------------------------
# Alpha-code lookup
# ----------------------------------------------------------------------------

class TestAlphaCodeLookup:

    def test_amro_returns_american_robin(self, catalog):
        rec = catalog.lookup_by_alpha("AMRO")
        assert rec is not None and rec.canonical_common_name == "American Robin"

    def test_lookup_is_case_insensitive(self, catalog):
        assert catalog.lookup_by_alpha("amro") is catalog.lookup_by_alpha("AMRO")

    def test_invalid_length_returns_none(self, catalog):
        assert catalog.lookup_by_alpha("AMR") is None
        assert catalog.lookup_by_alpha("AMROX") is None
        assert catalog.lookup_by_alpha("") is None

    def test_unknown_code_returns_none(self, catalog):
        assert catalog.lookup_by_alpha("XXXX") is None


# ----------------------------------------------------------------------------
# Fuzzy search ranking
# ----------------------------------------------------------------------------

class TestFuzzySearchRanking:
    """Each test pins one ranking tier so changes that subtly reorder the
    dropdown surface as test failures rather than UX regressions."""

    def test_alpha_4_query_ranks_first(self, catalog):
        results = catalog.search("AMRO", ["NA"], limit=5)
        assert results[0].canonical_common_name == "American Robin"

    def test_alpha_4_query_case_insensitive(self, catalog):
        upper = catalog.search("BAEA", ["NA"], limit=3)
        lower = catalog.search("baea", ["NA"], limit=3)
        assert upper and lower
        assert upper[0].canonical_common_name == lower[0].canonical_common_name == "Bald Eagle"

    def test_exact_common_name_beats_substring(self, catalog):
        # "Yellow Warbler" is an exact match; "Yellow-throated Warbler" is a
        # substring/prefix neighbour that must not outrank it.
        results = catalog.search("Yellow Warbler", ["NA"], limit=5)
        names = [r.canonical_common_name for r in results]
        assert names[0] == "Yellow Warbler"

    def test_prefix_beats_substring(self, catalog):
        # "American Robin" (prefix) ranks above species that merely contain
        # the substring "american".
        results = catalog.search("american robin", ["NA"], limit=5)
        assert results[0].canonical_common_name == "American Robin"

    def test_token_prefix_match(self, catalog):
        # "amer rob" should still surface American Robin -- both tokens
        # prefix words in the canonical name.
        results = catalog.search("amer rob", ["NA"], limit=5)
        names = [r.canonical_common_name for r in results]
        assert "American Robin" in names

    def test_subsequence_match(self, catalog):
        # "amrob" -> subsequence of "americanrobin"
        results = catalog.search("amrob", ["NA"], limit=10)
        names = [r.canonical_common_name for r in results]
        assert "American Robin" in names

    def test_alias_lookup_finds_record(self, catalog):
        # "Sand Martin" is the IOC name for the AOS "Bank Swallow" -- it's
        # stored in the ``aliases`` column.
        results = catalog.search("Sand Martin", ["NA"], limit=5)
        names = [r.canonical_common_name for r in results]
        assert "Bank Swallow" in names

    def test_scientific_name_search(self, catalog):
        results = catalog.search("Turdus migratorius", ["NA"], limit=5)
        assert results and results[0].canonical_common_name == "American Robin"

    def test_empty_query_returns_alphabetical_head(self, catalog):
        results = catalog.search("", ["NA"], limit=5)
        names = [r.canonical_common_name for r in results]
        assert names == sorted(names, key=str.lower)

    def test_empty_region_yields_no_results(self, catalog):
        assert catalog.search("american robin", [], limit=5) == []

    def test_region_filters_out_off_region_matches(self, catalog):
        # Restricting to a non-NA region must drop NA-only species even if the
        # query matches them.
        results = catalog.search("American Robin", ["AU"], limit=5)
        names = [r.canonical_common_name for r in results]
        assert "American Robin" not in names


# ----------------------------------------------------------------------------
# Score-tier sanity (pure unit tests, no catalog needed)
# ----------------------------------------------------------------------------

class TestScoreTiers:

    @staticmethod
    def _rec(common="American Robin", sci="Turdus migratorius", alpha="AMRO",
             aliases=()) -> BirdRecord:
        return BirdRecord(
            canonical_common_name=common, scientific_name=sci,
            family_sci="Turdidae", family_common="Thrush sp.",
            order="Passeriformes", regions=frozenset({"NA"}),
            alpha_4=alpha, aliases=tuple(aliases), is_model_species=True,
        )

    def test_alpha_beats_exact_common(self):
        r = self._rec()
        # An alpha-4 exact match must outrank an exact common-name match.
        assert score_record("AMRO", r) > score_record("American Robin", r)

    def test_exact_beats_prefix(self):
        r = self._rec()
        assert score_record("American Robin", r) > score_record("Ameri", r)

    def test_prefix_beats_subsequence(self):
        r = self._rec()
        assert score_record("Ameri", r) > score_record("amrob", r)

    def test_substring_lowest_nonzero_for_common(self):
        r = self._rec(common="Long-tailed Tit")
        # "tit" is a substring but not a prefix or token-prefix.
        assert score_record("tit", r) > 0

    def test_no_match_returns_zero(self):
        r = self._rec()
        assert score_record("zzzzzzzz", r) == 0

    def test_alias_exact_below_common_exact(self):
        r = self._rec(common="Bank Swallow", aliases=("Sand Martin",))
        assert score_record("Bank Swallow", r) > score_record("Sand Martin", r) > 0


# ----------------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------------

class TestTokens:

    def test_simple_split(self):
        assert _tokens("American Robin") == ["american", "robin"]

    def test_hyphen_splits(self):
        assert _tokens("Black-capped Chickadee") == ["black", "capped", "chickadee"]

    def test_apostrophe_kept(self):
        assert _tokens("Wilson's Snipe") == ["wilson's", "snipe"]


class TestSubsequence:

    def test_full_string_is_subsequence(self):
        assert _is_subsequence("americanrobin", "americanrobin")

    def test_letters_in_order_match(self):
        assert _is_subsequence("amrob", "americanrobin")

    def test_letters_out_of_order_do_not(self):
        assert not _is_subsequence("robam", "americanrobin")

    def test_empty_needle_matches(self):
        assert _is_subsequence("", "anything")


class TestTokenPrefix:

    def test_each_query_word_prefixes_unique_target(self):
        assert _token_prefix_match(["amer", "rob"], ["american", "robin"])

    def test_extra_query_token_fails(self):
        assert not _token_prefix_match(["amer", "rob", "x"], ["american", "robin"])

    def test_query_token_must_be_prefix_not_substring(self):
        assert not _token_prefix_match(["rob"], ["barnswallow"])
