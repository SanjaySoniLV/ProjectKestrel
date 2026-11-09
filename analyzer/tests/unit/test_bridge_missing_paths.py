"""A folder that has gone missing must not take the rest of the batch with it.

``inspect_folders`` and ``start_analysis_queue`` both receive lists of paths,
and both used to refuse the entire list if any one entry failed
``_validate_root_dir``.  In practice the failing entry is almost always benign:
a recents item for a folder that has since been renamed, or a camera card that
has been ejected.  One of those must not stop the user analysing the folder
they just picked.

The one case that still has to refuse everything is a path escaping
``KESTREL_ALLOWED_ROOT`` -- that is a security boundary, not housekeeping.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import api_bridge
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f'api_bridge not importable in this env: {e}', allow_module_level=True)


pytestmark = pytest.mark.unit


class _FakeQueueManager:
    """Records what would have been queued instead of starting a pipeline."""

    def __init__(self):
        self.calls = []

    def enqueue(self, paths, **kwargs):
        self.calls.append(list(paths))
        return {'success': True, 'added': len(paths)}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(api_bridge, '_ALLOWED_ROOT', None)
    return api_bridge.Api()


@pytest.fixture
def real_dir(tmp_path):
    d = tmp_path / 'outing'
    d.mkdir()
    return d


@pytest.fixture
def gone_dir(tmp_path):
    # Never created: stands in for a renamed folder or an ejected card.
    return tmp_path / 'renamed-since'


def _norm(p):
    return str(Path(p).resolve()).lower()


class TestInspectFolders:
    def test_missing_entry_does_not_block_the_rest(self, api, real_dir, gone_dir):
        res = api.inspect_folders([str(gone_dir), str(real_dir)])

        assert res['success'] is True
        assert res['missing_paths'] == [str(gone_dir)]
        assert [_norm(k) for k in res['results']] == [_norm(real_dir)]

    def test_all_missing_still_succeeds_with_empty_results(self, api, gone_dir):
        res = api.inspect_folders([str(gone_dir)])

        assert res['success'] is True
        assert res['results'] == {}
        assert res['missing_paths'] == [str(gone_dir)]

    def test_nothing_missing_reports_empty_list(self, api, real_dir):
        res = api.inspect_folders([str(real_dir)])

        assert res['success'] is True
        assert res['missing_paths'] == []
        assert len(res['results']) == 1

    def test_sandbox_escape_still_refuses_the_whole_batch(self, api, real_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(api_bridge, '_ALLOWED_ROOT', str(real_dir))
        outside = tmp_path / 'outside'
        outside.mkdir()

        res = api.inspect_folders([str(real_dir), str(outside)])

        assert res['success'] is False
        assert res['invalid_paths'] == [str(outside)]
        assert res['results'] == {}


class TestStartAnalysisQueue:
    @pytest.fixture(autouse=True)
    def fake_queue(self, monkeypatch):
        self.qm = _FakeQueueManager()
        monkeypatch.setattr(api_bridge, '_queue_manager', self.qm)

    def _start(self, api, paths):
        return api.start_analysis_queue(json.dumps(paths), False, False, False, True)

    def test_missing_entry_is_skipped_and_named(self, api, real_dir, gone_dir):
        res = self._start(api, [str(gone_dir), str(real_dir)])

        assert res['success'] is True
        assert res['added'] == 1
        assert res['skipped_paths'] == [str(gone_dir)]
        assert [_norm(p) for p in self.qm.calls[0]] == [_norm(real_dir)]

    def test_nothing_skipped_leaves_result_untouched(self, api, real_dir):
        res = self._start(api, [str(real_dir)])

        assert res == {'success': True, 'added': 1}

    def test_all_missing_is_an_error_that_names_them(self, api, gone_dir):
        res = self._start(api, [str(gone_dir)])

        assert res['success'] is False
        assert res['error'] == 'No valid paths provided'
        assert res['skipped_paths'] == [str(gone_dir)]
        assert self.qm.calls == []

    def test_sandbox_escape_still_refuses_the_whole_queue(self, api, real_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(api_bridge, '_ALLOWED_ROOT', str(real_dir))
        outside = tmp_path / 'outside'
        outside.mkdir()

        res = self._start(api, [str(real_dir), str(outside)])

        assert res['success'] is False
        assert res['invalid_paths'] == [str(outside)]
        assert self.qm.calls == []


class TestInspectFoldersKeysByInputPath:
    """Results must be keyed by the path the caller sent, not its realpath.

    ``_validate_root_dir`` canonicalises with ``os.path.realpath``. On Windows
    that turns a mapped network drive (``P:\\Birds``) into its UNC form
    (``\\\\server\\share\\Birds``), so a caller looking its own path up in
    ``results`` found nothing and the Analyze dialog showed 0 images for a NAS
    folder that analysed fine. A symlink reproduces the same rewrite on any OS.
    """

    @pytest.fixture
    def linked_dir(self, real_dir, tmp_path):
        (real_dir / 'IMG_0001.JPG').write_bytes(b'\xff\xd8\xff\xd9')
        link = tmp_path / 'mapped-drive'
        try:
            link.symlink_to(real_dir, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - Windows without symlink rights
            pytest.skip('symlinks unavailable')
        return link

    def test_result_is_found_under_the_path_the_caller_sent(self, api, linked_dir):
        res = api.inspect_folders([str(linked_dir)])

        assert res['success'] is True
        assert list(res['results']) == [str(linked_dir)]
        assert res['results'][str(linked_dir)]['total'] == 1

    def test_plain_path_is_still_keyed_by_itself(self, api, real_dir):
        (real_dir / 'IMG_0001.JPG').write_bytes(b'\xff\xd8\xff\xd9')

        res = api.inspect_folders([str(real_dir)])

        assert res['results'][str(real_dir)]['total'] == 1

    def test_two_spellings_of_one_folder_each_get_a_result(self, api, real_dir, linked_dir):
        res = api.inspect_folders([str(linked_dir), str(real_dir)])

        assert set(res['results']) == {str(linked_dir), str(real_dir)}
        assert res['results'][str(linked_dir)] == res['results'][str(real_dir)]
