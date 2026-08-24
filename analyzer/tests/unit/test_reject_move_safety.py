"""Data-safety guards around the culling reject-move and its undo point.

The culling assistant can move rejected photos into ``_KESTREL_Rejects`` and
offers an Undo that restores them together with the pre-move database. Two
properties have to hold for that promise to be worth anything:

1. Taking a new backup must not destroy the previous one. There is a single
   canonical ``kestrel_database_old.csv`` slot, so a second reject move over
   the same folder would otherwise overwrite the first move's restore point
   with no warning — losing the curation (ratings, labels, crop choices) the
   first pass produced.
2. Moving or restoring a file must never overwrite a file already at the
   destination. Cameras reuse filenames across cards (``IMG_0001`` per card),
   so a second import into one shoot folder can collide with a photo rejected
   earlier — and ``shutil.move`` overwrites silently on every platform.

Both matter because the files involved are the user's originals.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from api_bridge import Api
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f'api_bridge not importable in this env: {e}', allow_module_level=True)


pytestmark = pytest.mark.unit


@pytest.fixture
def api():
    """An Api instance without __init__ (which needs a webview window).

    Only the self-contained file helpers are exercised here.
    """
    obj = Api.__new__(Api)
    obj._culling_companion_extensions = ['.xmp', '.jpg']
    return obj


@pytest.fixture
def shoot(tmp_path):
    """A folder with an analyzed .kestrel database and a rejects subfolder."""
    kdir = tmp_path / '.kestrel'
    kdir.mkdir()
    (kdir / 'kestrel_database.csv').write_text('filename,quality\nPASS1.CR3,0.9\n')
    (kdir / 'kestrel_scenedata.json').write_text('{"version":"2.0"}')
    (tmp_path / '_KESTREL_Rejects').mkdir()
    return tmp_path


class TestBackupRotation:
    def test_second_backup_preserves_the_first(self, api, shoot):
        first = api.backup_kestrel_db(str(shoot))
        assert first['success']
        assert not first['rotated_previous']

        # Simulate the first reject move rewriting the working database.
        (shoot / '.kestrel' / 'kestrel_database.csv').write_text(
            'filename,quality\nPASS2.CR3,0.5\n'
        )

        second = api.backup_kestrel_db(str(shoot))
        assert second['success']
        assert second['rotated_previous'], 'previous backup was not rotated aside'

        archived = sorted(
            p for p in os.listdir(shoot / '.kestrel')
            if p.startswith('OLD_precull_kestrel_database_')
        )
        assert archived, 'no timestamped archive of the previous backup'
        content = (shoot / '.kestrel' / archived[0]).read_text()
        assert 'PASS1.CR3' in content, 'the first pass restore point was lost'

    def test_scenedata_backup_is_rotated_too(self, api, shoot):
        api.backup_kestrel_db(str(shoot))
        api.backup_kestrel_db(str(shoot))
        archived = [
            p for p in os.listdir(shoot / '.kestrel')
            if p.startswith('OLD_precull_kestrel_scenedata_')
        ]
        assert archived

    def test_current_slot_still_holds_the_newest_backup(self, api, shoot):
        api.backup_kestrel_db(str(shoot))
        (shoot / '.kestrel' / 'kestrel_database.csv').write_text(
            'filename,quality\nNEWEST.CR3,0.1\n'
        )
        api.backup_kestrel_db(str(shoot))
        # Undo restores from the canonical slot; it must be the most recent
        # state, so undoing the second move rolls back exactly that move.
        current = (shoot / '.kestrel' / 'kestrel_database_old.csv').read_text()
        assert 'NEWEST.CR3' in current


class TestArchiveHousekeeping:
    def test_schema_upgrade_backups_are_never_pruned(self, api, shoot):
        """``database._perform_db_upgrade`` writes OLD_kestrel_database_*.csv.

        Those fire once per schema upgrade and are the only copy of the
        pre-upgrade database. Rotation here fires on every cull pass, so the
        two must not share a namespace or pruning would eat them.
        """
        kdir = shoot / '.kestrel'
        upgrade_backup = kdir / 'OLD_kestrel_database_20200101_000000.csv'
        upgrade_backup.write_text('filename,quality\nPRE_UPGRADE.CR3,0.9\n')

        for i in range(api._BACKUP_ARCHIVE_RETENTION + 3):
            (kdir / 'kestrel_database.csv').write_text(f'filename,quality\nP{i}.CR3,0.5\n')
            api.backup_kestrel_db(str(shoot))

        assert upgrade_backup.exists(), 'schema-upgrade backup was pruned'
        assert 'PRE_UPGRADE.CR3' in upgrade_backup.read_text()

    def test_archives_are_capped(self, api, shoot):
        kdir = shoot / '.kestrel'
        for i in range(api._BACKUP_ARCHIVE_RETENTION + 4):
            (kdir / 'kestrel_database.csv').write_text(f'filename,quality\nP{i}.CR3,0.5\n')
            api.backup_kestrel_db(str(shoot))

        archives = [p for p in os.listdir(kdir) if p.startswith('OLD_precull_kestrel_database_')]
        assert len(archives) <= api._BACKUP_ARCHIVE_RETENTION, (
            f'{len(archives)} archives kept; a culled folder would grow without bound'
        )

    def test_same_second_rotations_do_not_lose_a_restore_point(self, api, shoot):
        """Two moves inside one second must not collapse into one archive."""
        kdir = shoot / '.kestrel'
        (kdir / 'kestrel_database.csv').write_text('filename,quality\nFIRST.CR3,0.9\n')
        api.backup_kestrel_db(str(shoot))
        (kdir / 'kestrel_database.csv').write_text('filename,quality\nSECOND.CR3,0.5\n')
        api.backup_kestrel_db(str(shoot))
        (kdir / 'kestrel_database.csv').write_text('filename,quality\nTHIRD.CR3,0.1\n')
        api.backup_kestrel_db(str(shoot))

        archived = [p for p in os.listdir(kdir) if p.startswith('OLD_precull_kestrel_database_')]
        bodies = [(kdir / name).read_text() for name in archived]
        assert any('FIRST.CR3' in b for b in bodies), 'first restore point lost'
        assert any('SECOND.CR3' in b for b in bodies), 'second restore point lost'

    def test_scenedata_slot_is_not_stranded_when_there_is_no_scenedata(self, api, shoot):
        """Rotating a slot whose replacement is never written breaks Undo.

        Undo restores the CSV and the scene data together; archiving the
        scenedata backup while writing no new one leaves them mismatched.
        """
        (shoot / '.kestrel' / 'kestrel_scenedata.json').unlink()
        api.backup_kestrel_db(str(shoot))
        (shoot / '.kestrel' / 'kestrel_scenedata_old.json').write_text('{"version":"2.0"}')

        api.backup_kestrel_db(str(shoot))

        assert (shoot / '.kestrel' / 'kestrel_scenedata_old.json').exists(), (
            'scenedata backup slot was rotated away with nothing to replace it'
        )


class TestFailedMovesAreReported:
    """The caller drops the rows it asked us to move, so it needs the failures.

    A row dropped for a photo still sitting in the shoot folder takes its
    rating, labels and crop choice out of the database while the file itself
    stays put — invisible until a re-analysis, and not repairable by Undo.
    """

    def test_collision_is_named_in_failed_filenames(self, api, shoot):
        rejects = shoot / '_KESTREL_Rejects'
        (rejects / 'IMG_0001.CR3').write_text('card-A-original')
        (shoot / 'IMG_0001.CR3').write_text('card-B-different-photo')
        (shoot / 'IMG_0007.CR3').write_text('moves-fine')

        res = api.move_rejects_to_folder(str(shoot), ['IMG_0001.CR3', 'IMG_0007.CR3'])

        assert res['success']
        assert res['failed_filenames'] == ['IMG_0001.CR3']
        assert (shoot / 'IMG_0001.CR3').exists()
        assert not (shoot / 'IMG_0007.CR3').exists()

    def test_clean_batch_reports_nothing_failed(self, api, shoot):
        (shoot / 'IMG_0010.CR3').write_text('a')
        (shoot / 'IMG_0011.CR3').write_text('b')

        res = api.move_rejects_to_folder(str(shoot), ['IMG_0010.CR3', 'IMG_0011.CR3'])

        assert res['success']
        assert res['failed_filenames'] == []

    def test_missing_file_is_reported_as_failed(self, api, shoot):
        res = api.move_rejects_to_folder(str(shoot), ['NOT_THERE.CR3'])
        assert res['success']
        assert res['failed_filenames'] == ['NOT_THERE.CR3']


class TestCompanionFilesAreGuardedToo:
    """The sidecar carries the edits; overwriting it loses them just the same."""

    def test_move_refuses_to_clobber_an_existing_sidecar(self, api, shoot):
        rejects = shoot / '_KESTREL_Rejects'
        (rejects / 'IMG_0003.xmp').write_text('card-A-edits')
        (shoot / 'IMG_0003.CR3').write_text('card-B-raw')
        (shoot / 'IMG_0003.xmp').write_text('card-B-edits')

        ok, moved = api._move_file_with_sidecars(
            str(shoot), 'IMG_0003.CR3', str(rejects), None
        )

        assert ok, 'the primary file had no collision and should still move'
        assert 'IMG_0003.xmp' not in moved
        assert (rejects / 'IMG_0003.xmp').read_text() == 'card-A-edits'
        assert (shoot / 'IMG_0003.xmp').exists(), 'sidecar must be left in place'

    def test_restore_refuses_to_clobber_an_existing_sidecar(self, api, shoot):
        rejects = shoot / '_KESTREL_Rejects'
        (rejects / 'IMG_0004.CR3').write_text('rejected-raw')
        (rejects / 'IMG_0004.xmp').write_text('old-edits')
        (shoot / 'IMG_0004.xmp').write_text('newer-edits')

        ok, restored = api._restore_file_with_sidecars(
            str(rejects), str(shoot), 'IMG_0004.CR3', None
        )

        assert ok
        assert 'IMG_0004.xmp' not in restored
        assert (shoot / 'IMG_0004.xmp').read_text() == 'newer-edits'


class TestMoveDoesNotOverwrite:
    def test_move_refuses_to_clobber_a_rejected_photo(self, api, shoot):
        rejects = shoot / '_KESTREL_Rejects'
        (rejects / 'IMG_0001.CR3').write_text('card-A-original')
        (shoot / 'IMG_0001.CR3').write_text('card-B-different-photo')

        ok, moved = api._move_file_with_sidecars(
            str(shoot), 'IMG_0001.CR3', str(rejects), None
        )

        assert not ok
        assert moved == []
        assert (rejects / 'IMG_0001.CR3').read_text() == 'card-A-original'
        assert (shoot / 'IMG_0001.CR3').exists(), 'source must be left in place'

    def test_restore_refuses_to_clobber_a_reimported_photo(self, api, shoot):
        rejects = shoot / '_KESTREL_Rejects'
        (rejects / 'IMG_0002.CR3').write_text('old-reject')
        (shoot / 'IMG_0002.CR3').write_text('reimported')

        ok, restored = api._restore_file_with_sidecars(
            str(rejects), str(shoot), 'IMG_0002.CR3', None
        )

        assert not ok
        assert restored == []
        assert (shoot / 'IMG_0002.CR3').read_text() == 'reimported'

    def test_uncontested_move_is_unaffected(self, api, shoot):
        rejects = shoot / '_KESTREL_Rejects'
        (shoot / 'IMG_9999.CR3').write_text('to-reject')

        ok, moved = api._move_file_with_sidecars(
            str(shoot), 'IMG_9999.CR3', str(rejects), None
        )

        assert ok
        assert moved == ['IMG_9999.CR3']
        assert (rejects / 'IMG_9999.CR3').exists()
        assert not (shoot / 'IMG_9999.CR3').exists()

    def test_uncontested_restore_is_unaffected(self, api, shoot):
        rejects = shoot / '_KESTREL_Rejects'
        (rejects / 'IMG_8888.CR3').write_text('restore-me')

        ok, restored = api._restore_file_with_sidecars(
            str(rejects), str(shoot), 'IMG_8888.CR3', None
        )

        assert ok
        assert restored == ['IMG_8888.CR3']
        assert (shoot / 'IMG_8888.CR3').exists()
