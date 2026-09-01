"""Unit tests for folder_diff.py — drift detection between disk and .kestrel.

The safety-critical case here is ``scan_status``: an unreadable folder must
never present as "every photo was deleted", because that is the state in which
the repair UI would offer to destroy a perfectly good database.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import folder_diff
from folder_diff import compute_folder_diff
from folder_inspector import scan_folder_images, SIZE_UNKNOWN

pytestmark = pytest.mark.unit


def _write_image(folder: Path, name: str, size: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"\0" * size)


def _write_db(root: Path, rows, include_size: bool = True) -> None:
    """Write a minimal kestrel_database.csv. ``rows`` is [(filename, size)]."""
    kdir = root / ".kestrel"
    kdir.mkdir(parents=True, exist_ok=True)
    if include_size:
        lines = ["filename,species,file_size"]
        for name, size in rows:
            lines.append(f"{name},Unknown,{'' if size is None else size}")
    else:
        lines = ["filename,species"]
        for name, _ in rows:
            lines.append(f"{name},Unknown")
    (kdir / "kestrel_database.csv").write_text("\n".join(lines), encoding="utf-8")


class TestScanStatus:
    def test_no_database_is_its_own_status(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        res = compute_folder_diff(str(tmp_path))
        assert res["scan_status"] == "no_database"
        assert res["has_drift"] is False

    def test_unreadable_folder_reports_nothing_missing(self, tmp_path, monkeypatch):
        """The whole point of the scan_status gate.

        A folder whose enumeration fails must not present its 3 database rows as
        3 deleted photos — that is what would make the repair UI offer to delete
        a database whose photos are perfectly fine.
        """
        _write_db(tmp_path, [("IMG_001.CR3", 100), ("IMG_002.CR3", 200), ("IMG_003.CR3", 300)])

        def _fail(_folder):
            return [], "PermissionError: [Errno 13] Permission denied"

        monkeypatch.setattr(folder_diff, "scan_folder_images", _fail)
        res = compute_folder_diff(str(tmp_path))

        assert res["scan_status"] == "unreadable"
        assert res["scan_error"]
        assert res["missing"] == []
        assert res["new"] == []
        assert res["has_drift"] is False

    def test_scan_folder_images_reports_error_for_missing_dir(self, tmp_path):
        entries, err = scan_folder_images(str(tmp_path / "does_not_exist"))
        assert entries == []
        assert err != ""

    def test_scan_folder_images_returns_sizes(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 1234)
        entries, err = scan_folder_images(str(tmp_path))
        assert err == ""
        assert entries == [("IMG_001.CR3", 1234)]


class TestCleanFolder:
    def test_matching_folder_has_no_drift(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_image(tmp_path, "IMG_002.CR3", 200)
        _write_db(tmp_path, [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])

        res = compute_folder_diff(str(tmp_path))
        assert res["scan_status"] == "ok"
        assert res["missing"] == []
        assert res["new"] == []
        assert res["renamed"] == []
        assert res["has_drift"] is False

    def test_new_files_detected(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_image(tmp_path, "IMG_002.CR3", 200)
        _write_db(tmp_path, [("IMG_001.CR3", 100)])

        res = compute_folder_diff(str(tmp_path))
        assert res["new"] == ["IMG_002.CR3"]
        assert res["missing"] == []

    def test_new_files_alone_are_not_drift(self, tmp_path):
        """Unanalysed photos are workflow, not damage.

        Badging them as something to repair would be noisy and misleading — the
        analyze dialog already reports "N here, M analysed".
        """
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_image(tmp_path, "IMG_002.CR3", 200)
        _write_db(tmp_path, [("IMG_001.CR3", 100)])

        res = compute_folder_diff(str(tmp_path))
        assert res["new"] == ["IMG_002.CR3"]
        assert res["has_drift"] is False

    def test_missing_files_are_drift(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_db(tmp_path, [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])

        res = compute_folder_diff(str(tmp_path))
        assert res["has_drift"] is True

    def test_deleted_files_detected(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_db(tmp_path, [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])

        res = compute_folder_diff(str(tmp_path))
        assert res["missing"] == ["IMG_002.CR3"]
        assert res["new"] == []


class TestRenameMatching:
    def test_unique_size_is_matched(self, tmp_path):
        _write_image(tmp_path, "Kingfisher_01.CR3", 100)
        _write_db(tmp_path, [("IMG_001.CR3", 100)])

        res = compute_folder_diff(str(tmp_path))
        assert res["renamed"] == [
            {"from": "IMG_001.CR3", "to": "Kingfisher_01.CR3", "size": 100}
        ]
        # A matched pair is removed from both residual sets.
        assert res["missing"] == []
        assert res["new"] == []

    def test_ambiguous_sizes_are_never_matched(self, tmp_path):
        """Two missing rows and two new files sharing a size → no pairing.

        Guessing here would reattach the wrong rating and culling decision.
        """
        _write_image(tmp_path, "A_new.CR3", 100)
        _write_image(tmp_path, "B_new.CR3", 100)
        _write_db(tmp_path, [("A_old.CR3", 100), ("B_old.CR3", 100)])

        res = compute_folder_diff(str(tmp_path))
        assert res["renamed"] == []
        assert res["missing"] == ["A_old.CR3", "B_old.CR3"]
        assert res["new"] == ["A_new.CR3", "B_new.CR3"]

    def test_unique_pair_matched_alongside_ambiguous_group(self, tmp_path):
        _write_image(tmp_path, "A_new.CR3", 100)
        _write_image(tmp_path, "B_new.CR3", 100)
        _write_image(tmp_path, "C_new.CR3", 555)
        _write_db(
            tmp_path,
            [("A_old.CR3", 100), ("B_old.CR3", 100), ("C_old.CR3", 555)],
        )

        res = compute_folder_diff(str(tmp_path))
        assert res["renamed"] == [
            {"from": "C_old.CR3", "to": "C_new.CR3", "size": 555}
        ]
        assert res["missing"] == ["A_old.CR3", "B_old.CR3"]
        assert res["new"] == ["A_new.CR3", "B_new.CR3"]


class TestLegacyDatabase:
    def test_no_file_size_column_still_finds_missing_and_new(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_image(tmp_path, "IMG_003.CR3", 300)
        _write_db(
            tmp_path,
            [("IMG_001.CR3", None), ("IMG_002.CR3", None)],
            include_size=False,
        )

        res = compute_folder_diff(str(tmp_path))
        assert res["scan_status"] == "ok"
        assert res["missing"] == ["IMG_002.CR3"]
        assert res["new"] == ["IMG_003.CR3"]
        # No stored sizes, so no basis for a rename pairing.
        assert res["renamed"] == []

    def test_blank_file_size_is_unknown_not_zero(self, tmp_path):
        """A blank size must not read as a mismatch against the real file."""
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_db(tmp_path, [("IMG_001.CR3", None)])

        res = compute_folder_diff(str(tmp_path))
        assert res["missing"] == []
        assert res["size_changed"] == []
        assert res["has_drift"] is False


class TestRejectFolder:
    def test_culled_photos_are_not_reported_missing(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_image(tmp_path / "_KESTREL_Rejects", "IMG_002.CR3", 200)
        _write_db(tmp_path, [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])

        res = compute_folder_diff(str(tmp_path))
        assert res["missing"] == []
        assert res["rejected"] == ["IMG_002.CR3"]

    def test_reject_folder_is_not_a_relocation_candidate(self, tmp_path):
        _write_image(tmp_path / "_KESTREL_Rejects", "IMG_002.CR3", 200)
        _write_db(tmp_path, [("IMG_002.CR3", 200)])

        res = compute_folder_diff(str(tmp_path))
        assert res["subfolder_hits"] == []


class TestSubfolderScan:
    def test_files_moved_into_subfolder_are_located(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_image(tmp_path / "keepers", "IMG_002.CR3", 200)
        _write_image(tmp_path / "keepers", "IMG_003.CR3", 300)
        _write_db(
            tmp_path,
            [("IMG_001.CR3", 100), ("IMG_002.CR3", 200), ("IMG_003.CR3", 300)],
        )

        res = compute_folder_diff(str(tmp_path))
        assert res["missing"] == ["IMG_002.CR3", "IMG_003.CR3"]
        assert len(res["subfolder_hits"]) == 1
        hit = res["subfolder_hits"][0]
        assert hit["name"] == "keepers"
        assert hit["filenames"] == ["IMG_002.CR3", "IMG_003.CR3"]
        assert hit["count"] == 2

    def test_kestrel_dir_is_skipped(self, tmp_path):
        _write_image(tmp_path, "IMG_001.CR3", 100)
        _write_db(tmp_path, [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        # An export JPEG inside .kestrel must never be offered as a relocation.
        _write_image(tmp_path / ".kestrel" / "export", "IMG_002.CR3", 200)

        res = compute_folder_diff(str(tmp_path))
        assert res["subfolder_hits"] == []

    def test_subfolder_scan_can_be_disabled(self, tmp_path):
        _write_image(tmp_path / "keepers", "IMG_002.CR3", 200)
        _write_db(tmp_path, [("IMG_002.CR3", 200)])

        res = compute_folder_diff(str(tmp_path), scan_subfolders=False)
        assert res["missing"] == ["IMG_002.CR3"]
        assert res["subfolder_hits"] == []


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "legacy_databases"


def _fixtures_without_file_size():
    """Fixtures whose schema predates ``file_size``.

    Selected by reading each fixture's header rather than by filename, so a
    fixture captured from a newer schema can be dropped into the same directory
    without silently invalidating these tests — the archive holds every schema,
    including current ones, and only the size-less ones exercise the legacy path.
    """
    if not FIXTURE_DIR.exists():
        return []
    out = []
    for path in sorted(FIXTURE_DIR.glob("v_*_kestrel_database.csv")):
        try:
            header = path.read_text(encoding="utf-8").split("\n", 1)[0]
        except Exception:
            continue
        if "file_size" not in [c.strip() for c in header.split(",")]:
            out.append(path)
    return out


_LEGACY_FIXTURES = _fixtures_without_file_size()


@pytest.mark.skipif(not _LEGACY_FIXTURES, reason="No pre-file_size fixtures present")
@pytest.mark.parametrize(
    "fixture_path", _LEGACY_FIXTURES, ids=[p.name for p in _LEGACY_FIXTURES]
)
class TestRealLegacyDatabases:
    """Drift detection against the real historical schemas shipped to users.

    These are literal CSVs captured from released tags (see
    ``fixtures/legacy_databases/SCHEMA_NOTES.md``), none of which carry
    ``file_size``. They are the schema the overwhelming majority of already-
    analysed folders on users' disks are in, so the diff's degraded-but-correct
    behaviour on them matters more than its behaviour on new folders.
    """

    @staticmethod
    def _stage(tmp_path: Path, fixture_path: Path):
        """Copy a fixture into place and create a stub file for every row."""
        import shutil
        import pandas as pd

        kdir = tmp_path / ".kestrel"
        kdir.mkdir(parents=True, exist_ok=True)
        shutil.copy(fixture_path, kdir / "kestrel_database.csv")
        frame = pd.read_csv(kdir / "kestrel_database.csv")
        names = [str(n) for n in frame["filename"].dropna()]
        for i, name in enumerate(names):
            # Distinct sizes so any accidental size-based matching would be
            # visible rather than masked by every stub being identical.
            (tmp_path / name).write_bytes(b"\0" * (1000 + i))
        return names

    def test_intact_legacy_folder_reports_no_drift(self, tmp_path, fixture_path):
        self._stage(tmp_path, fixture_path)
        res = compute_folder_diff(str(tmp_path))
        assert res["scan_status"] == "ok"
        assert res["missing"] == []
        assert res["new"] == []
        assert res["has_drift"] is False

    def test_deleted_file_detected_without_file_size(self, tmp_path, fixture_path):
        names = self._stage(tmp_path, fixture_path)
        (tmp_path / names[0]).unlink()

        res = compute_folder_diff(str(tmp_path))
        assert res["missing"] == [names[0]]
        assert res["new"] == []

    def test_added_file_detected_without_file_size(self, tmp_path, fixture_path):
        self._stage(tmp_path, fixture_path)
        _write_image(tmp_path, "IMG_9999.CR3", 4242)

        res = compute_folder_diff(str(tmp_path))
        assert res["new"] == ["IMG_9999.CR3"]
        assert res["missing"] == []

    def test_rename_is_not_guessed_on_legacy_rows(self, tmp_path, fixture_path):
        """No stored size means no evidence, so no pairing is offered.

        The file shows up as one missing plus one new, which is honest. Guessing
        would risk reattaching another photo's rating and culling decision.
        """
        names = self._stage(tmp_path, fixture_path)
        (tmp_path / names[0]).rename(tmp_path / "Renamed_Bird.CR3")

        res = compute_folder_diff(str(tmp_path))
        assert res["renamed"] == []
        assert res["missing"] == [names[0]]
        assert res["new"] == ["Renamed_Bird.CR3"]

    def test_legacy_rows_never_report_size_changed(self, tmp_path, fixture_path):
        self._stage(tmp_path, fixture_path)
        res = compute_folder_diff(str(tmp_path))
        assert res["size_changed"] == []

    def test_files_moved_to_subfolder_found_by_name(self, tmp_path, fixture_path):
        names = self._stage(tmp_path, fixture_path)
        (tmp_path / "keepers").mkdir()
        (tmp_path / names[0]).rename(tmp_path / "keepers" / names[0])

        res = compute_folder_diff(str(tmp_path))
        assert res["missing"] == [names[0]]
        assert len(res["subfolder_hits"]) == 1
        assert res["subfolder_hits"][0]["filenames"] == [names[0]]


class TestSizeChanged:
    def test_same_name_different_bytes_is_informational(self, tmp_path):
        _write_image(tmp_path, "IMG_001.JPG", 150)
        _write_db(tmp_path, [("IMG_001.JPG", 100)])

        res = compute_folder_diff(str(tmp_path))
        assert res["size_changed"] == [
            {"filename": "IMG_001.JPG", "stored": 100, "disk": 150}
        ]
        # Not drift the user needs to act on — the file is right where it should be.
        assert res["missing"] == []
        assert res["new"] == []
        assert res["has_drift"] is False
