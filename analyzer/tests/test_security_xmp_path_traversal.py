"""Regression tests for FINDING-02: XMP sidecar path traversal.

Background
----------
``metadata_writer.write_xmp_metadata`` takes a ``filename`` from each entry in
``image_data`` (which is populated from the CSV / JS-side payload) and joins it
with ``root_path`` via ``os.path.join``.  Python's ``os.path.join`` does NOT
normalise ``..`` segments, so a filename like ``../../evil`` resolves outside
``root_path`` and the resulting ``.xmp`` file is written wherever the user
process has write permission (e.g. a Windows Startup folder).

Expected fix
------------
Each entry's ``filename`` must be reduced to a bare basename (or otherwise
jailed to ``root_path``) before constructing the XMP path, and entries that
fail the check must be skipped rather than silently redirected.

Run with::

    cd analyzer
    python -m unittest tests.test_security_xmp_path_traversal
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(_THIS_DIR)
if _ANALYZER_DIR not in sys.path:
    sys.path.insert(0, _ANALYZER_DIR)

from metadata_writer import write_xmp_metadata  # noqa: E402


class _TraversalTestBase(unittest.TestCase):
    """Shared scaffold: creates a ``root`` dir and a sibling ``sensitive`` dir
    that must never receive a file during any test."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="kestrel_xmp_sec_")
        self.root = os.path.join(self.tmp, "photos")
        self.outside = os.path.join(self.tmp, "sensitive")
        os.makedirs(self.root)
        os.makedirs(self.outside)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _files_outside_root(self) -> list[str]:
        root_real = os.path.realpath(self.root)
        leaks: list[str] = []
        for dirpath, _dirs, filenames in os.walk(self.tmp):
            for fn in filenames:
                full = os.path.realpath(os.path.join(dirpath, fn))
                try:
                    common = os.path.commonpath([full, root_real])
                except ValueError:
                    common = ""
                if common != root_real:
                    leaks.append(full)
        return leaks

    def _assert_no_leak(self) -> None:
        leaks = self._files_outside_root()
        self.assertFalse(leaks, f"Files written outside root: {leaks}")


class TestXmpPathTraversal(_TraversalTestBase):
    def test_relative_dotdot_traversal_is_rejected(self) -> None:
        payload = [{"filename": "../sensitive/evil", "rating": 5, "culled": "accept"}]
        result = write_xmp_metadata(self.root, payload, overwrite_external=False)
        self.assertTrue(result.get("success"))
        self.assertFalse(
            os.path.exists(os.path.join(self.outside, "evil.xmp")),
            "Traversal via ../sensitive/evil leaked outside root",
        )
        self._assert_no_leak()
        self.assertEqual(
            result.get("written", 0),
            0,
            "Traversal entry must be rejected (written=0, errors populated)",
        )

    def test_deep_traversal_is_rejected(self) -> None:
        payload = [
            {"filename": "../../../../tmp/evil_deep", "rating": 1, "culled": "reject"}
        ]
        write_xmp_metadata(self.root, payload)
        self._assert_no_leak()

    def test_absolute_posix_path_is_rejected(self) -> None:
        abs_target = os.path.join(self.outside, "absolute_evil")
        payload = [{"filename": abs_target, "rating": 5, "culled": "accept"}]
        write_xmp_metadata(self.root, payload)
        self.assertFalse(
            os.path.exists(abs_target + ".xmp"),
            "Absolute filename leaked outside root",
        )
        self._assert_no_leak()

    def test_windows_style_traversal_is_rejected(self) -> None:
        payload = [
            {"filename": r"..\..\sensitive\winevil", "rating": 3, "culled": "reject"}
        ]
        write_xmp_metadata(self.root, payload)
        self.assertFalse(
            os.path.exists(os.path.join(self.outside, "winevil.xmp")),
            "Windows-style traversal leaked outside root",
        )
        self._assert_no_leak()

    def test_embedded_separator_is_not_silently_retargeted(self) -> None:
        """Even if contained within root, a filename containing path separators
        indicates caller confusion and should be rejected or normalised to a
        bare basename — never silently written to a sibling subdirectory."""
        payload = [{"filename": "subdir/inner", "rating": 1, "culled": "accept"}]
        write_xmp_metadata(self.root, payload)
        unexpected = os.path.join(self.root, "subdir", "inner.xmp")
        if os.path.exists(unexpected):
            # Permissible only if explicitly contained — and basename-only is preferred
            self.assertTrue(
                os.path.realpath(unexpected).startswith(os.path.realpath(self.root))
            )
        self._assert_no_leak()

    def test_null_byte_in_filename_is_rejected(self) -> None:
        payload = [{"filename": "normal\x00../evil", "rating": 0, "culled": ""}]
        # Either skipped (preferred) or raises — both prove the bug is fixed.
        try:
            write_xmp_metadata(self.root, payload)
        except ValueError:
            pass
        self._assert_no_leak()

    def test_legitimate_bare_filename_still_works(self) -> None:
        """Regression guard: sanitization must not break the happy path."""
        payload = [
            {
                "filename": "IMG_0001.CR3",
                "rating": 4,
                "culled": "accept",
                "species": "Red-Tailed Hawk",
                "family": "Accipitridae",
                "quality": 0.812,
            }
        ]
        result = write_xmp_metadata(self.root, payload)
        self.assertTrue(result.get("success"))
        self.assertEqual(result.get("written"), 1)
        expected = os.path.join(self.root, "IMG_0001.xmp")
        self.assertTrue(os.path.exists(expected))

    def test_symlink_image_does_not_write_xmp_outside_root(self) -> None:
        """S1-07: FINDING-02 jails the filename string, not the sidecar path.

        A bare name that is a symlink to a file outside root still passes
        the basename jail. Deriving ``base + '.xmp'`` from ``realpath`` of
        the image then writes the sidecar next to the target.
        """
        target = os.path.join(self.outside, "IMG_0001.CR3")
        with open(target, "wb") as fh:
            fh.write(b"not-a-raw")
        link = os.path.join(self.root, "IMG_0001.CR3")
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"cannot create symlink: {exc}")

        payload = [{"filename": "IMG_0001.CR3", "rating": 5, "culled": "accept"}]
        result = write_xmp_metadata(self.root, payload)

        self.assertTrue(result.get("success"))
        outside_xmp = [
            p for p in self._files_outside_root() if p.lower().endswith(".xmp")
        ]
        self.assertFalse(outside_xmp, f"XMP written outside root: {outside_xmp}")
        self.assertFalse(
            os.path.exists(os.path.join(self.outside, "IMG_0001.xmp")),
            "Sidecar followed the symlink target outside root",
        )
        in_root = os.path.join(self.root, "IMG_0001.xmp")
        self.assertTrue(
            os.path.exists(in_root),
            "Sidecar must stay beside the symlink in root",
        )
        self.assertFalse(os.path.islink(in_root))
        self.assertEqual(result.get("written"), 1)


if __name__ == "__main__":
    unittest.main()
