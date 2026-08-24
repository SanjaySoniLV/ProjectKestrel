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
            if p.startswith('OLD_kestrel_database_')
        )
        assert archived, 'no timestamped archive of the previous backup'
        content = (shoot / '.kestrel' / archived[0]).read_text()
        assert 'PASS1.CR3' in content, 'the first pass restore point was lost'

    def test_scenedata_backup_is_rotated_too(self, api, shoot):
        api.backup_kestrel_db(str(shoot))
        api.backup_kestrel_db(str(shoot))
        archived = [
            p for p in os.listdir(shoot / '.kestrel')
            if p.startswith('OLD_kestrel_scenedata_')
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
