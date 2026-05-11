"""Unit tests for ratings.py - quality score to star rating mapping."""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ratings import (
    RATING_PROFILES,
    get_profile_thresholds,
    quality_to_rating,
    compute_quality_distribution,
    get_image_display_rating,
)


pytestmark = pytest.mark.unit


class TestRatingProfiles:
    """Tests for RATING_PROFILES constant and threshold lookup."""

    def test_all_expected_profiles_exist(self):
        """All 5 standard profiles are defined."""
        expected = {'very_strict', 'strict', 'balanced', 'lenient', 'very_lenient'}
        assert expected.issubset(set(RATING_PROFILES.keys()))

    def test_profile_thresholds_decreasing(self):
        """Each profile has thresholds: five > four > three > two."""
        for name, thresholds in RATING_PROFILES.items():
            assert thresholds['five'] > thresholds['four'], f"{name}: five > four"
            assert thresholds['four'] > thresholds['three'], f"{name}: four > three"
            assert thresholds['three'] > thresholds['two'], f"{name}: three > two"

    def test_strictness_ordering(self):
        """very_strict has higher thresholds than balanced, which has higher than very_lenient."""
        very_strict = RATING_PROFILES['very_strict']
        balanced = RATING_PROFILES['balanced']
        very_lenient = RATING_PROFILES['very_lenient']

        assert very_strict['five'] > balanced['five']
        assert balanced['five'] > very_lenient['five']

    def test_get_profile_thresholds_known(self):
        """Known profile name returns correct thresholds."""
        result = get_profile_thresholds('balanced')
        assert result == RATING_PROFILES['balanced']

    def test_get_profile_thresholds_case_insensitive(self):
        """Profile name is case-insensitive."""
        assert get_profile_thresholds('BALANCED') == RATING_PROFILES['balanced']
        assert get_profile_thresholds('Balanced') == RATING_PROFILES['balanced']

    def test_get_profile_thresholds_unknown_defaults_to_balanced(self):
        """Unknown profile name defaults to 'balanced'."""
        result = get_profile_thresholds('nonexistent_profile')
        assert result == RATING_PROFILES['balanced']


class TestQualityToRating:
    """Tests for quality_to_rating()."""

    def test_high_quality_gets_five_stars(self):
        """Quality 0.95 with balanced profile → 5 stars."""
        assert quality_to_rating(0.95) == 5

    def test_low_quality_gets_one_star(self):
        """Quality 0.05 → 1 star (below all thresholds)."""
        assert quality_to_rating(0.05) == 1

    def test_threshold_boundaries_balanced(self):
        """Boundary values for balanced profile (5=0.85, 4=0.60, 3=0.40, 2=0.15)."""
        balanced = RATING_PROFILES['balanced']
        # Just above five threshold → 5
        assert quality_to_rating(0.85, balanced) == 5
        # Just above four threshold → 4
        assert quality_to_rating(0.61, balanced) == 4
        # Just above three threshold → 3
        assert quality_to_rating(0.41, balanced) == 3
        # Just above two threshold → 2
        assert quality_to_rating(0.16, balanced) == 2
        # Below two threshold → 1
        assert quality_to_rating(0.10, balanced) == 1

    def test_negative_quality_returns_zero(self):
        """Negative quality (no detection) → 0 stars."""
        assert quality_to_rating(-1.0) == 0
        assert quality_to_rating(-0.5) == 0

    def test_invalid_quality_returns_zero(self):
        """None or non-numeric → 0 stars."""
        assert quality_to_rating(None) == 0
        assert quality_to_rating("not a number") == 0

    def test_different_profiles_give_different_ratings(self):
        """Same quality score under different profiles → potentially different rating."""
        q = 0.70
        strict_rating = quality_to_rating(q, RATING_PROFILES['strict'])
        lenient_rating = quality_to_rating(q, RATING_PROFILES['lenient'])

        # Strict profile has higher thresholds, so for q=0.70:
        # - strict: 5=0.90, 4=0.72, 3=0.48 → q=0.70 → 3 stars
        # - lenient: 5=0.78, 4=0.53 → q=0.70 → 4 stars
        assert lenient_rating >= strict_rating

    def test_zero_quality_gets_one_star_or_zero(self):
        """Quality of 0 → 1 star (or 0 depending on profile threshold)."""
        result = quality_to_rating(0.0)
        # 0.0 is below 'two' threshold (0.15) → 1 star
        assert result == 1


class TestComputeQualityDistribution:
    """Tests for compute_quality_distribution()."""

    def test_returns_100_buckets(self):
        """Always returns a list of 100 buckets."""
        result = compute_quality_distribution([])
        assert len(result) == 100
        assert all(b == 0 for b in result)

    def test_basic_distribution(self):
        """Scores distributed into correct buckets."""
        scores = [0.05, 0.55, 0.95]
        result = compute_quality_distribution(scores)
        # 0.05 → bucket 5
        # 0.55 → bucket 55
        # 0.95 → bucket 95
        assert result[5] == 1
        assert result[55] == 1
        assert result[95] == 1

    def test_excludes_negative_scores(self):
        """Negative scores (no detection) are not counted."""
        scores = [-1.0, -0.5, 0.5]
        result = compute_quality_distribution(scores)
        # Only 0.5 should be counted
        assert sum(result) == 1
        assert result[50] == 1

    def test_score_at_one_goes_to_bucket_99(self):
        """Score of 1.0 → bucket 99 (the last bucket)."""
        result = compute_quality_distribution([1.0])
        assert result[99] == 1

    def test_invalid_scores_ignored(self):
        """Non-numeric scores are ignored."""
        scores = [0.5, None, "garbage", 0.7]
        result = compute_quality_distribution(scores)
        # Only 0.5 and 0.7 should be counted
        assert sum(result) == 2


class TestGetImageDisplayRating:
    """Tests for get_image_display_rating()."""

    def test_user_rating_takes_precedence(self):
        """User-specified rating overrides auto-computed."""
        user_ratings = {"IMG_001.CR3": 5}
        rating, origin = get_image_display_rating(
            "IMG_001.CR3", 0.10, user_ratings  # quality 0.10 → would be 1 star auto
        )
        # User rating wins
        assert rating == 5
        assert origin == "manual"

    def test_auto_rating_when_no_user_override(self):
        """Without user rating, use auto-computed quality_to_rating."""
        user_ratings = {}
        rating, origin = get_image_display_rating(
            "IMG_001.CR3", 0.95, user_ratings  # quality 0.95 → 5 stars
        )
        assert rating == 5
        assert origin == "auto"

    def test_user_rating_clamped_to_valid_range(self):
        """User rating outside 0-5 → clamped."""
        user_ratings = {"IMG_001.CR3": 99}
        rating, origin = get_image_display_rating(
            "IMG_001.CR3", 0.5, user_ratings
        )
        assert rating == 5
        assert origin == "manual"

    def test_invalid_user_rating_falls_back_to_auto(self):
        """Non-int user rating → fall through to auto-computed."""
        user_ratings = {"IMG_001.CR3": "garbage"}
        rating, origin = get_image_display_rating(
            "IMG_001.CR3", 0.95, user_ratings
        )
        # Falls back to auto since user rating is invalid
        assert rating == 5
        assert origin == "auto"

    def test_custom_thresholds_respected(self):
        """Custom thresholds change the auto rating result."""
        user_ratings = {}
        # Very strict thresholds — needs 0.93 for 5 stars
        very_strict = RATING_PROFILES['very_strict']
        rating, origin = get_image_display_rating(
            "IMG_001.CR3", 0.85, user_ratings, very_strict
        )
        # Under very_strict, 0.85 is < 0.93 (5-threshold), >= 0.82 (4-threshold) → 4 stars
        assert rating == 4
        assert origin == "auto"
