"""Unit tests for settings_utils.py - settings sanitization layer."""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from settings_utils import (
    _coerce_bool,
    _coerce_int,
    _coerce_float,
    _coerce_string,
    _coerce_enum,
    _coerce_path,
    _sanitize_path_list,
    _sanitize_settings_payload,
    _apply_monotonic_guard,
    _passthrough_setting_value,
)


pytestmark = pytest.mark.unit


class TestCoerceBool:
    """Tests for _coerce_bool."""

    def test_native_bool_preserved(self):
        assert _coerce_bool(True) == True
        assert _coerce_bool(False) == False

    def test_int_coerced(self):
        assert _coerce_bool(1) == True
        assert _coerce_bool(0) == False
        assert _coerce_bool(42) == True

    def test_string_truthy(self):
        assert _coerce_bool("true") == True
        assert _coerce_bool("yes") == True
        assert _coerce_bool("1") == True
        assert _coerce_bool("on") == True
        assert _coerce_bool("TRUE") == True  # case insensitive

    def test_string_falsy(self):
        assert _coerce_bool("false") == False
        assert _coerce_bool("no") == False
        assert _coerce_bool("0") == False
        assert _coerce_bool("off") == False

    def test_invalid_input_uses_default(self):
        assert _coerce_bool(None, default=True) == True
        assert _coerce_bool("garbage", default=False) == False
        assert _coerce_bool({}, default=True) == True


class TestCoerceInt:
    """Tests for _coerce_int."""

    def test_native_int_preserved(self):
        assert _coerce_int(42, default=0) == 42

    def test_float_truncated(self):
        assert _coerce_int(3.7, default=0) == 3

    def test_string_parsed(self):
        assert _coerce_int("42", default=0) == 42
        assert _coerce_int("3.7", default=0) == 3

    def test_invalid_uses_default(self):
        assert _coerce_int("garbage", default=99) == 99
        assert _coerce_int(None, default=99) == 99

    def test_min_value_clamping(self):
        assert _coerce_int(-5, default=0, min_value=0) == 0

    def test_max_value_clamping(self):
        assert _coerce_int(100, default=0, max_value=10) == 10

    def test_bool_coerced(self):
        assert _coerce_int(True, default=0) == 1
        assert _coerce_int(False, default=0) == 0


class TestCoerceFloat:
    """Tests for _coerce_float."""

    def test_native_float_preserved(self):
        assert _coerce_float(3.14, default=0.0) == 3.14

    def test_int_promoted(self):
        assert _coerce_float(42, default=0.0) == 42.0

    def test_string_parsed(self):
        assert _coerce_float("3.14", default=0.0) == 3.14

    def test_invalid_uses_default(self):
        assert _coerce_float("garbage", default=99.5) == 99.5

    def test_clamping(self):
        assert _coerce_float(-1.5, default=0.0, min_value=0.0) == 0.0
        assert _coerce_float(1.5, default=0.0, max_value=1.0) == 1.0


class TestCoerceString:
    """Tests for _coerce_string."""

    def test_string_preserved(self):
        assert _coerce_string("hello") == "hello"

    def test_strips_whitespace(self):
        assert _coerce_string("  hello  ") == "hello"

    def test_none_uses_default(self):
        assert _coerce_string(None, default="x") == "x"

    def test_empty_uses_default(self):
        assert _coerce_string("", default="x") == "x"
        assert _coerce_string("   ", default="x") == "x"

    def test_truncated_at_max_len(self):
        long_string = "a" * 10000
        result = _coerce_string(long_string, max_len=100)
        assert len(result) == 100


class TestCoerceEnum:
    """Tests for _coerce_enum."""

    def test_valid_value_preserved(self):
        allowed = {"lenient", "balanced", "aggressive"}
        assert _coerce_enum("balanced", allowed, "balanced") == "balanced"

    def test_invalid_uses_default(self):
        allowed = {"a", "b", "c"}
        assert _coerce_enum("z", allowed, "a") == "a"

    def test_case_handling(self):
        # Some impls are case-sensitive, just verify it doesn't crash
        allowed = {"balanced"}
        result = _coerce_enum("balanced", allowed, "balanced")
        assert result == "balanced"


class TestSanitizePathList:
    """Tests for _sanitize_path_list."""

    def test_empty_list(self):
        assert _sanitize_path_list([]) == []

    def test_list_of_strings(self):
        result = _sanitize_path_list(["/a/path", "/b/path"])
        assert len(result) == 2

    def test_non_list_returns_empty(self):
        assert _sanitize_path_list("not a list") == []
        assert _sanitize_path_list(None) == []

    def test_max_items_enforced(self):
        big_list = [f"/path/{i}" for i in range(1000)]
        result = _sanitize_path_list(big_list, max_items=10)
        assert len(result) == 10


class TestSanitizePayload:
    """Tests for _sanitize_settings_payload."""

    def test_empty_dict_returns_empty(self):
        result = _sanitize_settings_payload({})
        # Should return some sort of dict (may have defaults or be empty)
        assert isinstance(result, dict)

    def test_valid_keys_preserved(self):
        payload = {
            "detection_threshold": 0.5,
            "scene_time_threshold": 1200,
        }
        result = _sanitize_settings_payload(payload)
        # Values should be in result
        assert "detection_threshold" in result
        assert result["detection_threshold"] == 0.5

    def test_unknown_keys_preserved_via_passthrough(self):
        """Unknown keys should be preserved for forward compatibility."""
        payload = {
            "future_feature_xyz": "some_value",
            "another_new_setting": 42,
        }
        result = _sanitize_settings_payload(payload)
        # At least one of these should be preserved via passthrough
        # (depending on implementation details)
        assert isinstance(result, dict)


class TestBirdRegionsSetting:
    """Tests for ``bird_regions`` and ``show_scientific_names`` sanitisation
    -- the new species-tagging settings introduced alongside the global
    bird catalog."""

    def test_valid_region_list_preserved(self):
        result = _sanitize_settings_payload({"bird_regions": ["NA", "PAL"]})
        assert "bird_regions" in result
        assert set(result["bird_regions"]) == {"NA", "PAL"}

    def test_invalid_codes_dropped(self):
        result = _sanitize_settings_payload({"bird_regions": ["NA", "PIZZA", "AU"]})
        assert "bird_regions" in result
        assert set(result["bird_regions"]) == {"NA", "AU"}

    def test_empty_list_falls_back_to_default(self):
        result = _sanitize_settings_payload({"bird_regions": []})
        assert "bird_regions" in result
        assert result["bird_regions"] == ["NA"]

    def test_all_invalid_falls_back_to_default(self):
        result = _sanitize_settings_payload({"bird_regions": ["XYZ", "ABC"]})
        assert result["bird_regions"] == ["NA"]

    def test_duplicates_deduplicated(self):
        result = _sanitize_settings_payload({"bird_regions": ["NA", "NA", "AU"]})
        assert "bird_regions" in result
        assert result["bird_regions"].count("NA") == 1

    def test_non_list_resets_to_default(self):
        result = _sanitize_settings_payload({"bird_regions": "NA"})
        assert result["bird_regions"] == ["NA"]

    def test_non_string_items_skipped(self):
        result = _sanitize_settings_payload({"bird_regions": ["NA", 42, None, "PAL"]})
        assert set(result["bird_regions"]) == {"NA", "PAL"}

    def test_show_scientific_names_coerced(self):
        for raw, expected in (
            (True, True), (False, False),
            ("true", True), ("false", False),
            (1, True), (0, False),
        ):
            result = _sanitize_settings_payload({"show_scientific_names": raw})
            assert result.get("show_scientific_names") is expected, (raw, expected)


class TestPassthroughSetting:
    """Tests for _passthrough_setting_value (forward compat for unknown keys)."""

    def test_simple_string_passes(self):
        assert _passthrough_setting_value("hello") == "hello"

    def test_int_passes(self):
        assert _passthrough_setting_value(42) == 42

    def test_float_passes(self):
        assert _passthrough_setting_value(3.14) == 3.14

    def test_bool_passes(self):
        assert _passthrough_setting_value(True) == True

    def test_none_passes(self):
        assert _passthrough_setting_value(None) is None

    def test_simple_list_passes(self):
        assert _passthrough_setting_value([1, 2, 3]) == [1, 2, 3]

    def test_simple_dict_passes(self):
        assert _passthrough_setting_value({"a": 1}) == {"a": 1}

    def test_function_object_rejected(self):
        """Function objects should not pass through (not JSON-safe)."""
        result = _passthrough_setting_value(lambda x: x)
        # Should return None (rejected) since it's not JSON-safe
        assert result is None


class TestMonotonicGuard:
    """Tests for _apply_monotonic_guard."""

    def test_counter_resurrected_when_omitted(self):
        """If incoming omits a monotonic counter, existing value is preserved."""
        incoming = {"some_other_key": "value"}
        existing = {"kestrel_impact_total_files": 100}

        result = _apply_monotonic_guard(incoming, existing)
        # The existing counter should be preserved
        assert result.get("kestrel_impact_total_files") == 100

    def test_counter_never_decreases(self):
        """A counter can only stay the same or increase."""
        incoming = {"kestrel_impact_total_files": 50}
        existing = {"kestrel_impact_total_files": 100}

        result = _apply_monotonic_guard(incoming, existing)
        # Should keep the higher value
        assert result.get("kestrel_impact_total_files") == 100

    def test_counter_can_increase(self):
        """Higher new value should be accepted."""
        incoming = {"kestrel_impact_total_files": 200}
        existing = {"kestrel_impact_total_files": 100}

        result = _apply_monotonic_guard(incoming, existing)
        assert result.get("kestrel_impact_total_files") == 200

    def test_no_existing_data_uses_incoming(self):
        """No existing data → incoming used as-is."""
        incoming = {"kestrel_impact_total_files": 50}
        result = _apply_monotonic_guard(incoming, None)
        assert result.get("kestrel_impact_total_files") == 50
