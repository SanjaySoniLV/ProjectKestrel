"""Unit tests for kestrel_analyzer.regroup — scene regrouping from stored data.

Regrouping replays the pipeline's scene-boundary rules against the values the
analysis already wrote to kestrel_database.csv, so an analyzed folder can be
re-scened without decoding a single image (and without caring whether the
analysis ran locally or in the cloud).

The fixtures below are shaped like real pipeline output, which matters for one
non-obvious reason: inside a burst the pipeline short-circuits on the capture
time and writes -1 similarity sentinels, so the only pairs carrying a real
score are the joins *between* bursts. That is what the similarity control acts
on, and several tests here pin that behaviour down.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.regroup import (
    DEFAULT_COLOR_CUT,
    DEFAULT_FEATURE_CUT,
    MAX_COLOR_CUT,
    MAX_FEATURE_CUT,
    REASON_APPEARANCE,
    REASON_FIRST,
    REASON_ORIENTATION,
    REASON_TIME_GAP,
    CHANGE_MERGED,
    CHANGE_SAME,
    CHANGE_SPLIT,
    capture_gap_seconds,
    classify_changes,
    compute_groups,
    current_assignment,
    count_scored_pairs,
    map_similarity_threshold,
    parse_capture_time,
    plan_regroup,
    sort_items,
    stored_similarity,
    summarize,
    summarize_current,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixture builders — rows shaped the way the pipeline writes them.
# ---------------------------------------------------------------------------

def burst_row(filename, time, orientation="landscape"):
    """A row inside a burst: the pipeline merged it on time and skipped AKAZE,
    so every similarity field is the -1 'not computed' sentinel."""
    return {
        "filename": filename, "capture_time": time, "orientation": orientation,
        "feature_similarity": -1.0, "feature_confidence": -1.0,
        "color_similarity": -1.0, "color_confidence": -1.0,
        "scene_count": "1",
    }


def scored_row(filename, time, feature_similarity, orientation="landscape"):
    """A row the pipeline actually ran AKAZE on: real feature score, colour
    fields flat at 0 (which is how the two paths are told apart)."""
    return {
        "filename": filename, "capture_time": time, "orientation": orientation,
        "feature_similarity": feature_similarity, "feature_confidence": 1.0,
        "color_similarity": 0, "color_confidence": 0,
        "scene_count": "1",
    }


def color_row(filename, time, color_similarity, color_confidence=0.5,
              orientation="landscape"):
    """A row that took the colour fallback (too few keypoints): feature fields
    flat at 0, real colour score and a confidence of at least 0.25."""
    return {
        "filename": filename, "capture_time": time, "orientation": orientation,
        "feature_similarity": 0, "feature_confidence": 0,
        "color_similarity": color_similarity, "color_confidence": color_confidence,
        "scene_count": "1",
    }


def scenes_of(assignment):
    """Collapse an assignment into a list of scene sizes."""
    sizes = []
    for a in assignment:
        if a["scene"] > len(sizes):
            sizes.append(0)
        sizes[a["scene"] - 1] += 1
    return sizes


# A realistic folder: three 4-frame bursts at 10fps, 20 minutes apart, all
# shot against the same background so every burst-to-burst join scores as
# visually similar (0.40) and the analysis put the lot in one scene. This is
# the shape of the report that motivated the feature.
def one_pond_folder():
    return [
        burst_row("IMG_0001.CR3", "2026-05-01T09:00:00"),
        burst_row("IMG_0002.CR3", "2026-05-01T09:00:00.1"),
        burst_row("IMG_0003.CR3", "2026-05-01T09:00:00.2"),
        burst_row("IMG_0004.CR3", "2026-05-01T09:00:00.3"),
        scored_row("IMG_0005.CR3", "2026-05-01T09:20:00", 0.40),
        burst_row("IMG_0006.CR3", "2026-05-01T09:20:00.1"),
        burst_row("IMG_0007.CR3", "2026-05-01T09:20:00.2"),
        burst_row("IMG_0008.CR3", "2026-05-01T09:20:00.3"),
        scored_row("IMG_0009.CR3", "2026-05-01T09:40:00", 0.40),
        burst_row("IMG_0010.CR3", "2026-05-01T09:40:00.1"),
        burst_row("IMG_0011.CR3", "2026-05-01T09:40:00.2"),
        burst_row("IMG_0012.CR3", "2026-05-01T09:40:00.3"),
    ]


# ---------------------------------------------------------------------------
# The 0-1 similarity control
# ---------------------------------------------------------------------------

class TestMapSimilarityThreshold:
    def test_midpoint_reproduces_the_pipeline_cuts(self):
        # 0.5 must mean "exactly as analyzed" or the dialog's default is a
        # silent regrouping.
        feature, color = map_similarity_threshold(0.5)
        assert feature == pytest.approx(DEFAULT_FEATURE_CUT)
        assert color == pytest.approx(DEFAULT_COLOR_CUT)

    def test_zero_never_splits_on_appearance(self):
        feature, color = map_similarity_threshold(0.0)
        assert feature == pytest.approx(0.0)
        assert color == pytest.approx(0.0)

    def test_one_reaches_the_ceilings(self):
        feature, color = map_similarity_threshold(1.0)
        assert feature == pytest.approx(MAX_FEATURE_CUT)
        assert color == pytest.approx(MAX_COLOR_CUT)

    def test_monotonic_increasing(self):
        # Higher control must always mean "needs to look more alike", or the
        # slider's direction stops meaning anything.
        prev_f, prev_c = -1.0, -1.0
        for i in range(0, 101):
            f, c = map_similarity_threshold(i / 100)
            assert f >= prev_f
            assert c >= prev_c
            prev_f, prev_c = f, c

    @pytest.mark.parametrize("bad", [None, "", "abc", float("nan")])
    def test_unparseable_falls_back_to_as_analyzed(self, bad):
        assert map_similarity_threshold(bad) == pytest.approx(
            (DEFAULT_FEATURE_CUT, DEFAULT_COLOR_CUT)
        )

    @pytest.mark.parametrize("value", [-5, 5, 1.5, -0.1])
    def test_out_of_range_is_clamped(self, value):
        f, c = map_similarity_threshold(value)
        assert 0.0 <= f <= MAX_FEATURE_CUT
        assert 0.0 <= c <= MAX_COLOR_CUT


# ---------------------------------------------------------------------------
# Reading what the pipeline stored
# ---------------------------------------------------------------------------

class TestStoredSimilarity:
    def test_feature_path_row(self):
        assert stored_similarity(scored_row("a", "t", 0.42)) == ("feature", 0.42)

    def test_colour_fallback_row(self):
        assert stored_similarity(color_row("a", "t", 0.9)) == ("color", 0.9)

    def test_sentinel_row_has_no_score(self):
        assert stored_similarity(burst_row("a", "t")) is None

    def test_feature_score_of_zero_is_a_real_score(self):
        # 0.0 means "measured, and nothing matched" — it must not be confused
        # with the -1 sentinel, or every genuine scene break would vanish.
        assert stored_similarity(scored_row("a", "t", 0.0)) == ("feature", 0.0)

    def test_missing_fields_have_no_score(self):
        assert stored_similarity({"filename": "a"}) is None

    def test_none_row(self):
        assert stored_similarity(None) is None


class TestCaptureTime:
    def test_parses_iso(self):
        assert parse_capture_time("2026-05-01T09:00:00") is not None

    def test_parses_exif_colon_format(self):
        assert parse_capture_time("2026:05:01 09:00:00") is not None

    @pytest.mark.parametrize("bad", ["", None, "nan", "not-a-time"])
    def test_unparseable_is_none(self, bad):
        assert parse_capture_time(bad) is None

    def test_gap_is_absolute_seconds(self):
        gap = capture_gap_seconds("2026-05-01T09:00:00", "2026-05-01T09:02:30")
        assert gap == pytest.approx(150.0)

    def test_gap_none_when_either_side_unreadable(self):
        assert capture_gap_seconds("2026-05-01T09:00:00", "") is None
        assert capture_gap_seconds("", "2026-05-01T09:00:00") is None

    def test_gap_none_for_mixed_naive_and_aware(self):
        # Subtracting these would raise; a repaired CSV can mix them.
        assert capture_gap_seconds(
            "2026-05-01T09:00:00", "2026-05-01T09:00:30+00:00"
        ) is None


# ---------------------------------------------------------------------------
# The grouping rules
# ---------------------------------------------------------------------------

class TestComputeGroups:
    def test_empty_input(self):
        assert compute_groups([]) == []

    def test_single_image_is_one_scene(self):
        out = compute_groups([burst_row("a.CR3", "2026-05-01T09:00:00")])
        assert [a["scene"] for a in out] == [1]
        assert out[0]["reason"] == REASON_FIRST

    def test_as_analyzed_defaults_keep_the_one_pond_folder_whole(self):
        # The whole point of the report: with the shipped defaults this folder
        # is a single 12-image scene, because nothing can force a break.
        out = compute_groups(one_pond_folder())
        assert scenes_of(out) == [12]

    def test_split_after_gap_breaks_the_bursts_apart(self):
        out = compute_groups(one_pond_folder(), split_after_seconds=60)
        assert scenes_of(out) == [4, 4, 4]
        assert [a["reason"] for a in out if a["reason"]] == [
            REASON_FIRST, REASON_TIME_GAP, REASON_TIME_GAP,
        ]

    def test_split_after_zero_is_disabled(self):
        assert scenes_of(compute_groups(one_pond_folder(), split_after_seconds=0)) == [12]

    def test_split_after_longer_than_every_gap_does_nothing(self):
        assert scenes_of(compute_groups(one_pond_folder(), split_after_seconds=3600)) == [12]

    def test_gap_exactly_at_the_setting_does_not_break(self):
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            scored_row("b.CR3", "2026-05-01T09:05:00", 0.40),
        ]
        assert scenes_of(compute_groups(rows, split_after_seconds=300)) == [2]
        assert scenes_of(compute_groups(rows, split_after_seconds=299)) == [1, 1]

    def test_similarity_control_acts_on_the_burst_joins(self):
        # The joins score 0.40. Pushing the cut above that splits them; the
        # frames *inside* each burst are unscored and stay together either way.
        out = compute_groups(one_pond_folder(), similarity_threshold=1.0)
        assert scenes_of(out) == [4, 4, 4]
        assert [a["reason"] for a in out if a["reason"]] == [
            REASON_FIRST, REASON_APPEARANCE, REASON_APPEARANCE,
        ]

    def test_similarity_zero_never_splits(self):
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            scored_row("b.CR3", "2026-05-01T09:20:00", 0.0),
        ]
        # 0.0 scores below the shipped cut, so the default splits here...
        assert scenes_of(compute_groups(rows)) == [1, 1]
        # ...and a control of 0 must not.
        assert scenes_of(compute_groups(rows, similarity_threshold=0.0)) == [2]

    def test_unscored_pairs_are_never_split_on_appearance(self):
        # Two bursts 20 minutes apart whose join was never scored. Even at the
        # strictest similarity there is no evidence to split on, so only the
        # time rule can separate them.
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            burst_row("b.CR3", "2026-05-01T09:20:00"),
        ]
        assert scenes_of(compute_groups(rows, similarity_threshold=1.0)) == [2]
        assert scenes_of(compute_groups(rows, split_after_seconds=60)) == [1, 1]

    def test_group_within_merges_a_low_scoring_pair(self):
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            scored_row("b.CR3", "2026-05-01T09:00:03", 0.0),
        ]
        # 3s apart and visually different: split by default...
        assert scenes_of(compute_groups(rows)) == [1, 1]
        # ...merged once the burst window covers the gap.
        assert scenes_of(compute_groups(rows, group_within_seconds=5)) == [2]

    def test_orientation_change_always_breaks(self):
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00", orientation="landscape"),
            burst_row("b.CR3", "2026-05-01T09:00:00.1", orientation="portrait"),
        ]
        out = compute_groups(rows, group_within_seconds=60)
        assert scenes_of(out) == [1, 1]
        assert out[1]["reason"] == REASON_ORIENTATION

    def test_unknown_orientation_does_not_break(self):
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00", orientation="landscape"),
            burst_row("b.CR3", "2026-05-01T09:00:00.1", orientation=""),
        ]
        assert scenes_of(compute_groups(rows, group_within_seconds=60)) == [2]

    def test_orientation_outranks_the_time_merge(self):
        # Rule 1 beats rule 3: a flip inside a burst still starts a scene.
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00", orientation="landscape"),
            burst_row("b.CR3", "2026-05-01T09:00:00.1", orientation="portrait"),
        ]
        assert scenes_of(compute_groups(rows, group_within_seconds=3600)) == [1, 1]

    def test_time_gap_outranks_the_time_merge(self):
        # Rule 2 beats rule 3 when the windows overlap, so a split-after
        # smaller than group-within still splits rather than being swallowed.
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            burst_row("b.CR3", "2026-05-01T09:00:30"),
        ]
        assert scenes_of(
            compute_groups(rows, group_within_seconds=60, split_after_seconds=10)
        ) == [1, 1]

    def test_missing_timestamps_fall_back_to_appearance(self):
        rows = [
            scored_row("a.CR3", "", 0.40),
            scored_row("b.CR3", "", 0.0),
        ]
        # No gap to test, so rule 4 decides: b scored below the cut -> break.
        assert scenes_of(compute_groups(rows)) == [1, 1]

    def test_missing_timestamps_never_trip_the_gap_break(self):
        rows = [
            burst_row("a.CR3", ""),
            burst_row("b.CR3", ""),
        ]
        assert scenes_of(compute_groups(rows, split_after_seconds=1)) == [2]

    def test_colour_fallback_rows_use_the_colour_cut(self):
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            color_row("b.CR3", "2026-05-01T09:20:00", 0.85),
        ]
        # 0.85 clears the 0.82 default...
        assert scenes_of(compute_groups(rows)) == [2]
        # ...but not a stricter setting.
        assert scenes_of(compute_groups(rows, similarity_threshold=1.0)) == [1, 1]

    @pytest.mark.parametrize("bad", [None, "", "abc", -1, float("nan")])
    def test_bad_split_after_is_treated_as_disabled(self, bad):
        assert scenes_of(compute_groups(one_pond_folder(), split_after_seconds=bad)) == [12]

    @pytest.mark.parametrize("bad", [None, "", "abc"])
    def test_bad_group_within_falls_back_to_one_second(self, bad):
        out = compute_groups(one_pond_folder(), group_within_seconds=bad)
        assert scenes_of(out) == [12]

    def test_scene_numbers_are_contiguous_from_one(self):
        out = compute_groups(one_pond_folder(), split_after_seconds=60)
        seen = sorted({a["scene"] for a in out})
        assert seen == list(range(1, len(seen) + 1))

    def test_every_row_is_assigned_exactly_once(self):
        rows = one_pond_folder()
        out = compute_groups(rows, split_after_seconds=60)
        assert [a["filename"] for a in out] == [r["filename"] for r in rows]


class TestSortItems:
    def test_sorts_by_filename(self):
        rows = [burst_row("c.CR3", "t"), burst_row("a.CR3", "t"), burst_row("b.CR3", "t")]
        assert [r["filename"] for r in sort_items(rows)] == ["a.CR3", "b.CR3", "c.CR3"]

    def test_filename_order_not_capture_order(self):
        # Stored scores were computed against the alphabetical predecessor, so
        # regrouping must use the same adjacency even when the clock disagrees.
        rows = [
            burst_row("a.CR3", "2026-05-01T10:00:00"),
            burst_row("b.CR3", "2026-05-01T09:00:00"),
        ]
        assert [r["filename"] for r in sort_items(rows)] == ["a.CR3", "b.CR3"]


class TestCountScoredPairs:
    def test_counts_exclude_the_first_image(self):
        rows = one_pond_folder()
        scored, unscored = count_scored_pairs(rows)
        assert scored + unscored == len(rows) - 1
        # Only the two burst-to-burst joins carry a score.
        assert scored == 2

    def test_empty_and_single(self):
        assert count_scored_pairs([]) == (0, 0)
        assert count_scored_pairs([burst_row("a.CR3", "t")]) == (0, 0)


# ---------------------------------------------------------------------------
# Preview payload
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_one_entry_per_scene_with_sizes_and_bounds(self):
        rows = sort_items(one_pond_folder())
        out = summarize(rows, compute_groups(rows, split_after_seconds=60))
        assert [s["size"] for s in out] == [4, 4, 4]
        assert out[0]["first_filename"] == "IMG_0001.CR3"
        assert out[0]["last_filename"] == "IMG_0004.CR3"
        assert out[1]["reason"] == REASON_TIME_GAP

    def test_current_grouping_is_read_from_scene_count(self):
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            burst_row("b.CR3", "2026-05-01T09:00:01"),
            burst_row("c.CR3", "2026-05-01T09:00:02"),
        ]
        rows[2]["scene_count"] = "7"
        assert [s["size"] for s in summarize_current(rows)] == [2, 1]

    def test_current_grouping_renumbers_by_first_appearance(self):
        # Stored scene_count values need not be sorted or contiguous; the
        # preview still has to list them in the order they appear.
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            burst_row("b.CR3", "2026-05-01T09:00:01"),
        ]
        rows[0]["scene_count"] = "9"
        rows[1]["scene_count"] = "3"
        out = summarize_current(rows)
        assert [s["scene"] for s in out] == [1, 2]
        assert out[0]["first_filename"] == "a.CR3"


class TestPlanRegroup:
    def test_payload_shape(self):
        plan = plan_regroup(one_pond_folder(), split_after_seconds=60)
        assert plan["image_count"] == 12
        assert plan["stats"]["proposed_scene_count"] == 3
        assert plan["stats"]["largest_proposed_scene"] == 4
        assert plan["stats"]["scored_pairs"] == 2
        assert plan["stats"]["unscored_pairs"] == 9
        assert plan["stats"]["breaks_by_reason"] == {REASON_TIME_GAP: 2}
        assert len(plan["assignment"]) == 12

    def test_assignment_covers_every_filename_once(self):
        plan = plan_regroup(one_pond_folder(), split_after_seconds=60)
        names = [a["filename"] for a in plan["assignment"]]
        assert sorted(names) == sorted(r["filename"] for r in one_pond_folder())
        assert len(set(names)) == len(names)

    def test_reports_the_cuts_actually_used(self):
        plan = plan_regroup(one_pond_folder(), similarity_threshold=0.5)
        assert plan["stats"]["feature_cut"] == pytest.approx(DEFAULT_FEATURE_CUT)
        assert plan["stats"]["color_cut"] == pytest.approx(DEFAULT_COLOR_CUT)

    def test_empty_folder(self):
        plan = plan_regroup([])
        assert plan["image_count"] == 0
        assert plan["stats"]["proposed_scene_count"] == 0
        assert plan["stats"]["largest_proposed_scene"] == 0
        assert plan["assignment"] == []

    def test_unsorted_input_is_ordered_before_grouping(self):
        rows = list(reversed(one_pond_folder()))
        plan = plan_regroup(rows, split_after_seconds=60)
        assert [a["filename"] for a in plan["assignment"]][0] == "IMG_0001.CR3"
        assert plan["stats"]["proposed_scene_count"] == 3


# ---------------------------------------------------------------------------
# Parity with the pipeline
# ---------------------------------------------------------------------------

class TestPipelineParity:
    """Regrouping with a folder's own analysis settings must be a no-op.

    These use rows built the way the pipeline writes them, then check that the
    regrouped boundaries land exactly where the stored scene_count says the
    analysis put them.
    """

    def test_defaults_reproduce_the_stored_grouping(self):
        # Two bursts whose join scored 0.0 — the pipeline split there, so the
        # stored scene_count is 1,1,2,2 and regrouping must agree.
        rows = [
            burst_row("a.CR3", "2026-05-01T09:00:00"),
            burst_row("b.CR3", "2026-05-01T09:00:00.5"),
            scored_row("c.CR3", "2026-05-01T09:30:00", 0.0),
            burst_row("d.CR3", "2026-05-01T09:30:00.5"),
        ]
        rows[0]["scene_count"] = rows[1]["scene_count"] = "1"
        rows[2]["scene_count"] = rows[3]["scene_count"] = "2"

        plan = plan_regroup(rows, group_within_seconds=1.0,
                            split_after_seconds=0.0, similarity_threshold=0.5)
        assert [s["size"] for s in plan["proposed"]] == [
            s["size"] for s in plan["current"]
        ]
        assert plan["stats"]["proposed_scene_count"] == plan["stats"]["current_scene_count"]

    def test_rule_order_matches_the_documented_ladder(self):
        # orientation > time-gap > burst-merge > appearance. One row that
        # would qualify under several rules must report the highest one.
        rows = [
            scored_row("a.CR3", "2026-05-01T09:00:00", 0.40, orientation="landscape"),
            scored_row("b.CR3", "2026-05-01T09:10:00", 0.0, orientation="portrait"),
        ]
        out = compute_groups(rows, group_within_seconds=3600, split_after_seconds=60)
        assert out[1]["reason"] == REASON_ORIENTATION

        rows[1]["orientation"] = "landscape"
        out = compute_groups(rows, group_within_seconds=3600, split_after_seconds=60)
        assert out[1]["reason"] == REASON_TIME_GAP

        out = compute_groups(rows, group_within_seconds=3600, split_after_seconds=0)
        assert out[1]["reason"] == ""  # burst-merge swallowed the appearance break


# ---------------------------------------------------------------------------
# Boundary detail: which threshold fired and the measured value
# ---------------------------------------------------------------------------

class TestBoundaryDetail:
    def test_time_gap_break_reports_gap_and_threshold(self):
        out = compute_groups(one_pond_folder(), split_after_seconds=600)
        breaks = [a for a in out if a["reason"] == REASON_TIME_GAP]
        assert len(breaks) == 2
        assert breaks[0]["detail"]["kind"] == "time"
        assert breaks[0]["detail"]["value"] == pytest.approx(1199.7)
        assert breaks[0]["detail"]["threshold"] == 600

    def test_appearance_break_reports_score_and_the_cut_used(self):
        # Control 1.0 -> feature cut 0.60; the stored 0.40 is below it.
        out = compute_groups(one_pond_folder(), similarity_threshold=1.0)
        hit = next(a for a in out if a["reason"] == REASON_APPEARANCE)
        assert hit["detail"] == {
            "kind": "feature", "value": 0.40, "threshold": MAX_FEATURE_CUT,
        }

    def test_colour_fallback_break_is_labelled_colour(self):
        rows = [
            burst_row("a.jpg", "2026-05-01T09:00:00"),
            color_row("b.jpg", "2026-05-01T09:30:00", 0.30),
        ]
        out = compute_groups(rows)
        assert out[1]["reason"] == REASON_APPEARANCE
        assert out[1]["detail"]["kind"] == "color"
        assert out[1]["detail"]["value"] == pytest.approx(0.30)
        assert out[1]["detail"]["threshold"] == pytest.approx(DEFAULT_COLOR_CUT)

    def test_orientation_break_reports_both_orientations(self):
        rows = [
            burst_row("a.jpg", "2026-05-01T09:00:00", "landscape"),
            burst_row("b.jpg", "2026-05-01T09:00:00.1", "portrait"),
        ]
        out = compute_groups(rows)
        assert out[1]["detail"] == {
            "kind": "orientation", "from": "landscape", "to": "portrait",
        }

    def test_continuing_rows_and_the_first_row_have_no_detail(self):
        out = compute_groups(one_pond_folder())
        assert all("detail" not in a for a in out)

    def test_detail_names_the_rule_that_won_not_a_lower_one(self):
        # Orientation outranks the time gap, so the reported cause is the
        # rotation even though the gap also exceeds the setting.
        rows = [
            burst_row("a.jpg", "2026-05-01T09:00:00", "landscape"),
            burst_row("b.jpg", "2026-05-01T10:00:00", "portrait"),
        ]
        out = compute_groups(rows, split_after_seconds=60)
        assert out[1]["reason"] == REASON_ORIENTATION
        assert out[1]["detail"]["kind"] == "orientation"


# ---------------------------------------------------------------------------
# Index ranges (how the UI finds a scene's photos)
# ---------------------------------------------------------------------------

class TestSceneRanges:
    def test_contiguous_scenes_are_one_range_each(self):
        rows = sort_items(one_pond_folder())
        out = summarize(rows, compute_groups(rows, split_after_seconds=600))
        assert [s["ranges"] for s in out] == [[[0, 3]], [[4, 7]], [[8, 11]]]

    def test_non_contiguous_scene_gets_several_ranges(self):
        rows = [
            {**burst_row(f"{i}.jpg", "2026-05-01T09:00:00"), "scene_count": sc}
            for i, sc in enumerate(["1", "1", "2", "1"])
        ]
        out = summarize_current(rows)
        assert out[0]["ranges"] == [[0, 1], [3, 3]]
        assert out[1]["ranges"] == [[2, 2]]

    def test_ranges_cover_every_image_exactly_once(self):
        rows = sort_items(one_pond_folder())
        for entries in (summarize_current(rows),
                        summarize(rows, compute_groups(rows, split_after_seconds=60))):
            covered = [i for s in entries for a, b in s["ranges"] for i in range(a, b + 1)]
            assert sorted(covered) == list(range(len(rows)))

    def test_summary_carries_the_boundary_detail(self):
        rows = sort_items(one_pond_folder())
        out = summarize(rows, compute_groups(rows, split_after_seconds=600))
        assert "detail" not in out[0]
        assert out[1]["detail"]["kind"] == "time"


# ---------------------------------------------------------------------------
# Unchanged / merged / split versus the current grouping
# ---------------------------------------------------------------------------

def _rows_with_scenes(scene_ids):
    return [
        {**burst_row(f"IMG_{i:04d}.jpg", "2026-05-01T09:00:00"), "scene_count": str(sc)}
        for i, sc in enumerate(scene_ids)
    ]


def _classify(current_ids, proposed_ids):
    rows = _rows_with_scenes(current_ids)
    cur_assign = current_assignment(rows)
    new_assign = [
        {"filename": r["filename"], "scene": sc} for r, sc in zip(rows, proposed_ids)
    ]
    current = summarize(rows, cur_assign)
    proposed = summarize(rows, new_assign)
    classify_changes(current, proposed, cur_assign, new_assign)
    return current, proposed


class TestClassifyChanges:
    def test_identical_grouping_is_all_same(self):
        current, proposed = _classify([1, 1, 2, 2], [1, 1, 2, 2])
        assert [s["change"] for s in current + proposed] == [CHANGE_SAME] * 4

    def test_a_split_marks_the_pieces_split_and_the_original_split(self):
        current, proposed = _classify([1, 1, 1, 1], [1, 1, 2, 2])
        assert current[0]["change"] == CHANGE_SPLIT
        assert current[0]["change_count"] == 2
        assert [s["change"] for s in proposed] == [CHANGE_SPLIT, CHANGE_SPLIT]

    def test_a_merge_marks_the_result_merged_and_the_originals_merged(self):
        current, proposed = _classify([1, 1, 2, 2], [1, 1, 1, 1])
        assert proposed[0]["change"] == CHANGE_MERGED
        assert proposed[0]["change_count"] == 2
        assert [s["change"] for s in current] == [CHANGE_MERGED, CHANGE_MERGED]

    def test_untouched_scenes_stay_same_beside_a_changed_one(self):
        # Current: A A | B B B | C.  Proposed splits B in two and keeps A, C.
        current, proposed = _classify([1, 1, 2, 2, 2, 3], [1, 1, 2, 2, 3, 4])
        assert [s["change"] for s in current] == [CHANGE_SAME, CHANGE_SPLIT, CHANGE_SAME]
        assert [s["change"] for s in proposed] == [
            CHANGE_SAME, CHANGE_SPLIT, CHANGE_SPLIT, CHANGE_SAME]

    def test_plan_reports_change_counts(self):
        plan = plan_regroup(one_pond_folder(), split_after_seconds=600)
        stats = plan["stats"]
        assert stats["proposed_scene_count"] == 3
        assert stats["split_scenes"] == 3
        assert stats["merged_scenes"] == 0
        assert stats["unchanged_scenes"] == 0
        assert all(s["change"] == CHANGE_SPLIT for s in plan["proposed"])
        assert plan["current"][0]["change"] == CHANGE_SPLIT

    def test_as_analyzed_settings_report_no_change(self):
        plan = plan_regroup(one_pond_folder())
        assert plan["stats"]["unchanged_scenes"] == plan["stats"]["proposed_scene_count"]
        assert all(s["change"] == CHANGE_SAME for s in plan["current"] + plan["proposed"])
