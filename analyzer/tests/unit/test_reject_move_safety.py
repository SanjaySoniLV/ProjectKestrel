"""Data-safety guards around the culling reject-move and its undo point.

The culling assistant can move rejected photos into ``_KESTREL_Rejects`` and
offers an Undo that restores them together with the pre-move database. This
file covers the backup half of that promise:

    Taking a new backup must not destroy the previous one. There is a single
    canonical ``kestrel_database_old.csv`` slot, so a second reject move over
    the same folder would otherwise overwrite the first move's restore point
    with no warning — losing the curation (ratings, labels, crop choices) the
    first pass produced.

The other half — that moving or restoring a file must never overwrite a file
already at the destination, because cameras reuse filenames across cards — is
covered by ``test_reject_move.py`` against the implementation that shipped in
#120.

It matters because the files involved are the user's originals.
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

# The rest of this PR's original suite covered reject/undo refusing to
# overwrite an existing destination, and a `failed_filenames` key on
# move_rejects_to_folder. Both landed first via #120, which implements the same
# guarantee with a stronger mechanism (hardlink-or-O_EXCL rather than an
# exists() check, so the TOCTOU window is closed) and reports the outcome as
# structured `skipped` / `moved_requested` lists instead of a flat list of
# names. Those tests were written against the superseded 2-tuple return and are
# dropped here rather than rewritten; `test_reject_move.py` covers the same
# behaviour against the contract that shipped.
