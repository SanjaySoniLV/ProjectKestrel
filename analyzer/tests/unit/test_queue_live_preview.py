"""Live Analysis preview: cheap backend writes + change counters, and the UI contracts.

The pipeline hands the queue a full 1200 px overlay and ~1000 px crops for every
image. Writing those as-is cost encode time on the analysis thread and made the
UI refetch (and base64-decode) the same bytes on every poll. The queue now
writes small JPEGs atomically and bumps per-item ``live_*_ver`` counters so the
frontend reloads an image only when it actually changed.
"""

import re
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import queue_manager

pytestmark = pytest.mark.unit

_ANALYZER = Path(__file__).resolve().parents[2]


def _rgb(h, w):
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)


def _jpeg_size(path):
    import cv2
    img = cv2.imread(str(path))
    assert img is not None, f"{path} is not a readable JPEG"
    return img.shape[1], img.shape[0]


# ── _write_live_jpeg ─────────────────────────────────────────────────────────

def test_write_live_jpeg_downscales_to_max_dim_and_leaves_no_temp(tmp_path):
    out = tmp_path / '.kestrel' / 'export' / '__live_overlay.jpg'
    assert queue_manager._write_live_jpeg(str(out), _rgb(800, 1200), 720, 80) is True
    assert _jpeg_size(out) == (720, 480)
    assert not (out.parent / (out.name + '.tmp')).exists()


def test_write_live_jpeg_keeps_small_images_at_their_size(tmp_path):
    out = tmp_path / 'c.jpg'
    assert queue_manager._write_live_jpeg(str(out), _rgb(100, 120), 224, 85) is True
    assert _jpeg_size(out) == (120, 100)


def test_write_live_jpeg_replaces_existing_frame(tmp_path):
    out = tmp_path / 'c.jpg'
    queue_manager._write_live_jpeg(str(out), _rgb(300, 300), 224, 85)
    queue_manager._write_live_jpeg(str(out), _rgb(50, 60), 224, 85)
    assert _jpeg_size(out) == (60, 50)


def test_write_live_jpeg_is_best_effort_and_never_raises(tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    # Parent "directory" is a regular file -> cannot be created; must return False.
    assert queue_manager._write_live_jpeg(str(blocker / 'x.jpg'), _rgb(10, 10), 224, 85) is False
    assert queue_manager._write_live_jpeg(str(tmp_path / 'y.jpg'), None, 224, 85) is False


# ── version counters through the real callbacks ──────────────────────────────

class _LivePipeline:
    detector_name = None
    use_gpu = False

    def process_folder(self, path, pause_event=None, cancel_event=None,
                       callbacks=None, **kwargs):
        cb = callbacks or {}
        cb['on_thumbnail']({'filename': 'a.CR3', 'export_path': '.kestrel\\export\\a_export.jpg'})
        cb['on_detection']({'filename': 'a.CR3', 'overlay': _rgb(800, 1200), 'bird_count': 2})
        cb['on_crops']({'filename': 'a.CR3', 'crops': [_rgb(1024, 1024), _rgb(1024, 1024)],
                        'confidences': [0.9, 0.8]})
        self.seen = None  # filled by the test through the manager below


def _run(tmp_path):
    mgr = queue_manager.QueueManager()
    mgr._pipeline = _LivePipeline()
    mgr.enqueue([str(tmp_path)], use_gpu=False)
    assert mgr.join_worker(timeout=30) is True
    return mgr._items[0]


def test_live_callbacks_write_small_files_and_bump_versions(tmp_path):
    item = _run(tmp_path)
    d = item.to_dict()
    assert d['live_frame_ver'] == 1
    assert d['live_overlay_ver'] == 1
    assert d['live_crops_ver'] == 1
    assert d['current_export_path'] == '.kestrel/export/a_export.jpg'
    assert d['current_overlay_rel'] == '.kestrel/export/__live_overlay.jpg'
    assert d['current_crops_rel'] == ['.kestrel/export/__live_crop_0.jpg',
                                      '.kestrel/export/__live_crop_1.jpg']
    export = tmp_path / '.kestrel' / 'export'
    assert _jpeg_size(export / '__live_overlay.jpg') == (queue_manager._LIVE_OVERLAY_MAX_DIM, 480)
    assert _jpeg_size(export / '__live_crop_0.jpg') == (queue_manager._LIVE_CROP_MAX_DIM,) * 2
    assert list(export.glob('*.tmp')) == []


def test_new_item_starts_with_zero_versions():
    d = queue_manager._QueueItem('/x', 'x').to_dict()
    assert (d['live_frame_ver'], d['live_overlay_ver'], d['live_crops_ver']) == (0, 0, 0)


def test_empty_detection_overlay_does_not_bump_version(tmp_path):
    """A failed/absent overlay write leaves the version alone so the UI keeps the last frame."""
    class _P(_LivePipeline):
        def process_folder(self, path, pause_event=None, cancel_event=None, callbacks=None, **kw):
            callbacks['on_detection']({'filename': 'a.CR3', 'overlay': None, 'bird_count': 0})

    mgr = queue_manager.QueueManager()
    mgr._pipeline = _P()
    mgr.enqueue([str(tmp_path)], use_gpu=False)
    assert mgr.join_worker(timeout=30) is True
    d = mgr._items[0].to_dict()
    assert d['live_overlay_ver'] == 0 and d['current_overlay_rel'] == ''


# ── frontend contracts (static) ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def queue_js() -> str:
    return (_ANALYZER / 'js' / 'queue.js').read_text(encoding='utf-8')


@pytest.fixture(scope="module")
def html() -> str:
    return (_ANALYZER / 'visualizer.html').read_text(encoding='utf-8')


def test_live_images_reload_only_when_their_version_changes(queue_js):
    assert 'live_overlay_ver' in queue_js and 'live_crops_ver' in queue_js and 'live_frame_ver' in queue_js
    # The old "always reload live files" behaviour must be gone.
    assert 'Date.now()' not in queue_js[queue_js.find('function updateLiveAnalysisDlg'):
                                        queue_js.find('function _formatStars')]


def test_poll_is_adaptive_and_ticks_do_not_overlap(queue_js):
    assert re.search(r'_liveAnalysisDlgOpen \? 250 : 500', queue_js)
    assert '_queuePollBusy' in queue_js


def test_live_blob_urls_are_owned_and_revoked(queue_js):
    assert 'URL.revokeObjectURL' in queue_js
    assert '_releaseOwnedImg' in queue_js


def test_motion_respects_reduced_motion_in_js_and_css(queue_js):
    assert 'prefers-reduced-motion' in queue_js
    for name in ('analyze.css', 'live-analysis.css'):
        css = (_ANALYZER / 'css' / 'dialogs' / name).read_text(encoding='utf-8')
        assert '@media (prefers-reduced-motion: reduce)' in css, name


def test_queue_controls_keep_ids_and_use_a_single_pause_toggle(html):
    for ident in ('queuePauseBtn', 'queueCancelBtn', 'queueLiveBtn'):
        assert f'id="{ident}"' in html
    # Pause/Resume is one button, toggled in JS; there is no separate resume button.
    assert 'queueResumeBtn' not in html
    pause = html[html.find('id="queuePauseBtn"'):][:260]
    assert 'qctl-ico' in pause and 'qctl-lbl' in pause and 'title="Pause analysis"' in pause
    cancel = html[html.find('id="queueCancelBtn"'):][:260]
    assert 'qctl--danger' in cancel and 'title=' in cancel


def test_cancel_still_asks_for_confirmation():
    wiring = (_ANALYZER / 'js' / 'event-wiring.js').read_text(encoding='utf-8')
    start = wiring.find("getElementById('queueCancelBtn')")
    assert start != -1
    assert 'confirm(' in wiring[start:start + 400]
