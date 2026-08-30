"""Unit tests for the Perch upload path jail (S1-06).

``_join_under_session`` must reject prefix-sibling traversal such as
session ``.../trip`` + rel ``../trip_private/secret.CR3``. A
``str.startswith`` check treats that as inside the session because
``.../trip_private`` begins with the characters ``.../trip``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from perch_uploader import _join_under_session


pytestmark = pytest.mark.unit


def _assert_under_session(path: Path, session: Path) -> None:
    common = os.path.commonpath(
        [os.path.normcase(str(path.resolve())), os.path.normcase(str(session.resolve()))]
    )
    assert common == os.path.normcase(str(session.resolve()))


class TestJoinUnderSession:
    def test_prefix_sibling_raises(self, tmp_path: Path) -> None:
        session = tmp_path / "trip"
        session.mkdir()
        private = tmp_path / "trip_private"
        private.mkdir()
        secret = private / "secret.CR3"
        secret.write_bytes(b"not-a-raw")

        with pytest.raises(ValueError, match="Path escapes session root"):
            _join_under_session(session, "../trip_private/secret.CR3")

    def test_legitimate_export_stays_inside(self, tmp_path: Path) -> None:
        session = tmp_path / "trip"
        exports = session / "exports"
        exports.mkdir(parents=True)
        img = exports / "IMG_001.jpg"
        img.write_bytes(b"jpeg")

        got = _join_under_session(session, "exports/IMG_001.jpg")
        assert got == img.resolve()
        _assert_under_session(got, session)

    def test_dotdot_that_resolves_inside_is_allowed(self, tmp_path: Path) -> None:
        session = tmp_path / "trip"
        exports = session / "exports"
        exports.mkdir(parents=True)
        img = exports / "IMG_001.jpg"
        img.write_bytes(b"jpeg")

        got = _join_under_session(session, "exports/../exports/IMG_001.jpg")
        assert got == img.resolve()
        _assert_under_session(got, session)

    def test_empty_and_dot_raise(self, tmp_path: Path) -> None:
        session = tmp_path / "trip"
        session.mkdir()
        with pytest.raises(ValueError, match="Empty path"):
            _join_under_session(session, "")
        with pytest.raises(ValueError, match="Empty path"):
            _join_under_session(session, ".")
