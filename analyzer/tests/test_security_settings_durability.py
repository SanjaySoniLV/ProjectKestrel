"""Regression tests for FINDING-06: settings.json data loss from silent
empty-dict fallback on corrupt load, non-atomic fallback writes, and the
dead ``_merge_forward_compatible_keys`` path.

These tests exercise ``settings_utils.load_persisted_settings`` and
``save_persisted_settings`` against a temp directory that stands in for the
real user-data folder. They intentionally don't touch the real settings
file.

What's guarded here
-------------------
1. The monotonic counter (``kestrel_impact_total_files``) cannot regress
   across a load/save cycle, even if the caller passes a lower value.
2. If ``settings.json`` is corrupt on disk and a valid ``.bak`` exists, the
   next save transparently recovers counters from ``.bak`` and quarantines
   the corrupt file rather than silently writing a tiny dict over it.
3. If ``settings.json`` is corrupt AND ``.bak`` is also unusable, the save
   is refused — preserving the corrupt file for manual recovery is strictly
   better than clobbering data.
4. Unknown ("forward-compatible") keys survive a load/save round-trip so a
   newer-build settings file loaded by an older build doesn't lose them.

Run with::

    cd analyzer
    python -m unittest tests.test_security_settings_durability
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(_THIS_DIR)
if _ANALYZER_DIR not in sys.path:
    sys.path.insert(0, _ANALYZER_DIR)

import settings_utils  # noqa: E402


class _SettingsTempDirMixin:
    """Redirect settings I/O to a per-test temp directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="kestrel_settings_test_")
        self._data_dir = self._tmp.name
        self._path = os.path.join(self._data_dir, settings_utils.SETTINGS_FILENAME)
        self._patch = mock.patch.object(
            settings_utils, "_get_settings_path", return_value=self._path
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def _write_raw(self, contents: str, *, suffix: str = "") -> None:
        target = self._path + suffix
        with open(target, "w", encoding="utf-8") as f:
            f.write(contents)

    def _read_raw(self, *, suffix: str = "") -> str:
        with open(self._path + suffix, "r", encoding="utf-8") as f:
            return f.read()


class TestMonotonicCounter(_SettingsTempDirMixin, unittest.TestCase):
    """FINDING-06: the impact counter must never regress."""

    def test_counter_clamped_up_on_regression(self) -> None:
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 500, "editor": "darktable"}
        )
        # Stale caller tries to save a lower value (e.g. raced with an
        # analysis that just bumped the counter, or hydrated from a cold
        # localStorage).
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 100, "editor": "lightroom"}
        )
        loaded = settings_utils.load_persisted_settings()
        self.assertEqual(
            loaded.get("kestrel_impact_total_files"),
            500,
            "Counter must not regress across saves (monotonic guard)",
        )
        self.assertEqual(loaded.get("editor"), "lightroom")

    def test_counter_clamped_up_on_missing_key(self) -> None:
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 1234}
        )
        # Caller omits the counter entirely — must not be resurrected as 0.
        settings_utils.save_persisted_settings({"editor": "darktable"})
        loaded = settings_utils.load_persisted_settings()
        self.assertEqual(loaded.get("kestrel_impact_total_files"), 1234)

    def test_counter_increases_are_preserved(self) -> None:
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 10}
        )
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 25}
        )
        loaded = settings_utils.load_persisted_settings()
        self.assertEqual(loaded.get("kestrel_impact_total_files"), 25)


class TestForwardCompatKeys(_SettingsTempDirMixin, unittest.TestCase):
    """FINDING-06: unknown keys survive the sanitize round-trip."""

    def test_future_scalar_key_is_preserved(self) -> None:
        self._write_raw(
            json.dumps(
                {
                    "editor": "darktable",
                    "kestrel_impact_total_files": 42,
                    "a_future_toggle": True,
                    "future_nested": {"ratio": 0.75, "label": "ok"},
                }
            )
        )
        loaded = settings_utils.load_persisted_settings()
        self.assertTrue(loaded.get("a_future_toggle"))
        self.assertEqual(loaded.get("future_nested"), {"ratio": 0.75, "label": "ok"})

        # Re-save and verify the keys still round-trip.
        settings_utils.save_persisted_settings(loaded)
        reloaded = settings_utils.load_persisted_settings()
        self.assertTrue(reloaded.get("a_future_toggle"))
        self.assertEqual(
            reloaded.get("future_nested"), {"ratio": 0.75, "label": "ok"}
        )


class TestCorruptionHandling(_SettingsTempDirMixin, unittest.TestCase):
    """FINDING-06: corrupt settings.json must not be silently clobbered."""

    def test_recovers_counter_from_bak_after_corruption(self) -> None:
        # Establish a known-good state + .bak sidecar (written by the save).
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 999, "editor": "darktable"}
        )
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 1000, "editor": "lightroom"}
        )
        self.assertTrue(os.path.exists(self._path + ".bak"))

        # Corrupt the main file (simulate a bad power loss / partial write).
        self._write_raw("{not valid json")

        # load_persisted_settings falls back to .bak.
        loaded = settings_utils.load_persisted_settings()
        self.assertIn("kestrel_impact_total_files", loaded)
        self.assertGreaterEqual(loaded["kestrel_impact_total_files"], 999)

        # A save now should NOT clobber a fresh counter bump with zeros.
        settings_utils.save_persisted_settings({"editor": "capture_one"})

        # The corrupt main file should have been quarantined, a fresh file
        # written, and the counter preserved from .bak.
        quarantined = [
            name
            for name in os.listdir(self._data_dir)
            if name.startswith(settings_utils.SETTINGS_FILENAME + ".corrupt-")
        ]
        self.assertTrue(
            quarantined, "Corrupt settings.json must be quarantined, not deleted"
        )

        reloaded = settings_utils.load_persisted_settings()
        self.assertEqual(reloaded.get("editor"), "capture_one")
        self.assertGreaterEqual(
            reloaded.get("kestrel_impact_total_files", 0),
            999,
            "Counter must survive corruption via .bak recovery",
        )

    def test_refuses_save_when_no_recovery_available(self) -> None:
        # Corrupt main, NO .bak — must refuse to save to preserve evidence.
        self._write_raw("{not valid json")
        self.assertFalse(os.path.exists(self._path + ".bak"))

        original_bytes = self._read_raw().encode("utf-8")

        settings_utils.save_persisted_settings({"editor": "darktable"})

        # The corrupt file must remain exactly as it was — no quarantine,
        # no overwrite. The running app continues with in-memory defaults.
        self.assertTrue(os.path.exists(self._path))
        self.assertEqual(self._read_raw().encode("utf-8"), original_bytes)

    def test_atomic_write_leaves_no_partial_file(self) -> None:
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 7, "editor": "gimp"}
        )
        # There must be no leftover .tmp file after a successful save.
        self.assertFalse(os.path.exists(self._path + ".tmp"))
        # Also no orphan ``settings.json.*.tmp`` (from mkstemp naming).
        import glob as _glob
        orphans = _glob.glob(os.path.join(self._data_dir, "settings.json.*.tmp"))
        self.assertFalse(orphans, f"Unexpected orphan tmp files: {orphans}")


class TestConcurrentSaves(_SettingsTempDirMixin, unittest.TestCase):
    """Regression test for the WinError 2 race: multiple threads saving to
    the same settings file used to collide on the shared ``settings.json.tmp``
    name, causing one side's ``os.replace`` to fail with 'cannot find the
    file specified'. Each save now uses a unique ``mkstemp`` name, and the
    whole save sequence is serialized by ``_SAVE_LOCK``.
    """

    def test_concurrent_saves_all_succeed_and_no_orphans(self) -> None:
        import threading as _threading
        import glob as _glob

        # Seed a known-good file so the monotonic guard has a baseline.
        settings_utils.save_persisted_settings(
            {"kestrel_impact_total_files": 1, "editor": "darktable"}
        )

        n_threads = 8
        per_thread_writes = 5
        errors: list[BaseException] = []
        errors_lock = _threading.Lock()

        def writer(tid: int) -> None:
            try:
                for i in range(per_thread_writes):
                    settings_utils.save_persisted_settings(
                        {
                            # Different counter bumps per thread so the monotonic
                            # guard is exercised in a race.
                            "kestrel_impact_total_files": tid * 100 + i,
                            "editor": f"editor_{tid}_{i}",
                        }
                    )
            except BaseException as exc:  # noqa: BLE001
                with errors_lock:
                    errors.append(exc)

        threads = [
            _threading.Thread(target=writer, args=(tid,))
            for tid in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertFalse(errors, f"Concurrent saves raised: {errors}")

        # Final file must be valid JSON and a dict.
        with open(self._path, "r", encoding="utf-8") as f:
            final = json.load(f)
        self.assertIsInstance(final, dict)

        # Monotonic guard must have preserved the maximum counter any thread
        # wrote (the highest possible value is (n_threads-1)*100 + (per_thread_writes-1)).
        max_possible = (n_threads - 1) * 100 + (per_thread_writes - 1)
        self.assertGreaterEqual(final.get("kestrel_impact_total_files", 0), max_possible)

        # No orphan tmp files should remain.
        orphans = _glob.glob(os.path.join(self._data_dir, "settings.json.*.tmp"))
        self.assertFalse(orphans, f"Unexpected orphan tmp files: {orphans}")


if __name__ == "__main__":
    unittest.main()
