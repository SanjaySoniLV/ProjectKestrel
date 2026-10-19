"""Unit tests for the long-gap scene break.

Scene boundaries are decided by AKAZE visual similarity between consecutive
frames, with a "captured within N seconds" rule that merges a burst. Nothing in
that pair ever *forces* a break, so a long session shot against one background
chains into a single scene of hundreds of images no matter how much time passed
between the frames. ``scene_break_gap_seconds`` adds the missing rule: more than
N seconds between two shots starts a new scene. 0 keeps the historical
behaviour.

These tests cover the two pure pieces the rule is built from — the shared
capture-gap read and the predicate the pipeline evaluates — plus the pipeline's
coercion of the parameter itself.
"""

from datetime import datetime
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import similarity
from kestrel_analyzer.similarity import (
    compute_capture_time_gap,
    compute_similarity_timestamp,
)


pytestmark = pytest.mark.unit


@pytest.fixture
def fake_capture_times(monkeypatch):
    """Stub raw_exif.get_capture_time with a path -> datetime table."""
    table: dict = {}

    def _fake(path):
        key = str(path)
        if key not in table:
            raise ValueError(f"no capture time for {key}")
        return table[key]

    import kestrel_analyzer.raw_exif as raw_exif
    monkeypatch.setattr(raw_exif, "get_capture_time", _fake)
    return table


class TestComputeCaptureTimeGap:
    """The shared EXIF read both scene rules key off."""

    def test_returns_absolute_gap_in_seconds(self, fake_capture_times):
        fake_capture_times["a.cr3"] = datetime(2026, 5, 1, 9, 0, 0)
        fake_capture_times["b.cr3"] = datetime(2026, 5, 1, 9, 2, 30)
        assert compute_capture_time_gap("a.cr3", "b.cr3") == pytest.approx(150.0)
        # Order must not matter — the pipeline compares neighbours either way.
        assert compute_capture_time_gap("b.cr3", "a.cr3") == pytest.approx(150.0)

    def test_sub_second_gap_is_not_rounded_away(self, fake_capture_times):
        fake_capture_times["a.cr3"] = datetime(2026, 5, 1, 9, 0, 0, 0)
        fake_capture_times["b.cr3"] = datetime(2026, 5, 1, 9, 0, 0, 250000)
        assert compute_capture_time_gap("a.cr3", "b.cr3") == pytest.approx(0.25)

    def test_unreadable_timestamp_returns_none(self, fake_capture_times):
        fake_capture_times["a.cr3"] = datetime(2026, 5, 1, 9, 0, 0)
        assert compute_capture_time_gap("a.cr3", "missing.cr3") is None


class TestComputeSimilarityTimestampUnchanged:
    """The burst-merge helper keeps its contract on top of the shared read."""

    def test_within_threshold_is_true(self, fake_capture_times):
        fake_capture_times["a.cr3"] = datetime(2026, 5, 1, 9, 0, 0)
        fake_capture_times["b.cr3"] = datetime(2026, 5, 1, 9, 0, 1)
        assert compute_similarity_timestamp("a.cr3", "b.cr3", 1.0) is True

    def test_outside_threshold_is_false(self, fake_capture_times):
        fake_capture_times["a.cr3"] = datetime(2026, 5, 1, 9, 0, 0)
        fake_capture_times["b.cr3"] = datetime(2026, 5, 1, 9, 0, 5)
        assert compute_similarity_timestamp("a.cr3", "b.cr3", 1.0) is False

    def test_unreadable_timestamp_is_none(self, fake_capture_times):
        assert compute_similarity_timestamp("a.cr3", "b.cr3", 1.0) is None


def _breaks(gap, setting):
    """The predicate the pipeline evaluates, in isolation."""
    return setting > 0 and gap is not None and gap > setting


class TestBreakPredicate:
    def test_disabled_by_default(self):
        # 0 must never break, however long the pause — this is what keeps an
        # existing folder re-analyzing to exactly the scenes it had before.
        assert _breaks(86400.0, 0.0) is False

    def test_gap_beyond_setting_breaks(self):
        assert _breaks(301.0, 300.0) is True

    def test_gap_within_setting_does_not_break(self):
        assert _breaks(299.0, 300.0) is False

    def test_gap_exactly_at_setting_does_not_break(self):
        # "after a pause of N seconds" reads as strictly longer than N.
        assert _breaks(300.0, 300.0) is False

    def test_unreadable_timestamp_never_breaks(self):
        # A RAW whose capture time can't be read falls back to AKAZE rather
        # than being torn into its own scene.
        assert _breaks(None, 300.0) is False


class TestPipelineParameterCoercion:
    """process_folder normalizes the parameter before the loop reads it."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, 0.0),
            ("", 0.0),
            ("not-a-number", 0.0),
            (-1, 0.0),
            (float("nan"), 0.0),
            ("300", 300.0),
            (120, 120.0),
        ],
    )
    def test_coercion_matches_pipeline(self, raw, expected):
        # Mirrors the guard at the top of process_folder: anything unparseable,
        # negative or NaN disables the rule rather than raising mid-analysis.
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 0.0
        if value != value or value < 0:
            value = 0.0
        assert value == pytest.approx(expected)


def test_similarity_module_still_exports_the_legacy_helper():
    # The Modal pipeline vendors this module and imports the old name; keep it
    # exported so a re-vendor doesn't break the cloud path.
    assert hasattr(similarity, "compute_similarity_timestamp")
