"""The settings orphan-temp reaper must not delete another process's in-flight save.

Two app instances started together share one settings directory. ``_SAVE_LOCK``
only serializes saves inside one process, so instance A can be between writing
its ``settings.json.<rand>.tmp`` and ``os.replace`` when instance B finishes a
save and reaps orphan temp files. Reaping every ``*.tmp`` deleted A's file and
A's replace failed with "file not found" (WinError 2 on Windows).
"""
import os
import time

import pytest

import settings_utils as su

pytestmark = pytest.mark.unit


def _make_tmp(directory, name, age_sec=0.0):
    path = os.path.join(directory, su._TMP_FILE_PREFIX + name + su._TMP_FILE_SUFFIX)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('{}')
    if age_sec:
        t = time.time() - age_sec
        os.utime(path, (t, t))
    return path


def test_recent_tmp_from_another_save_survives_reap(tmp_path):
    d = str(tmp_path)
    in_flight = _make_tmp(d, 'other_process')
    su._reap_orphan_tmp_files(d)
    assert os.path.exists(in_flight)
    # The other process can still complete its atomic replace.
    os.replace(in_flight, os.path.join(d, 'settings.json'))
    assert os.path.exists(os.path.join(d, 'settings.json'))


def test_old_orphan_tmp_is_reaped(tmp_path):
    d = str(tmp_path)
    stale = _make_tmp(d, 'crashed', age_sec=su._ORPHAN_TMP_MIN_AGE_SEC + 60)
    fresh = _make_tmp(d, 'fresh')
    su._reap_orphan_tmp_files(d)
    assert not os.path.exists(stale)
    assert os.path.exists(fresh)


def test_reaper_ignores_unrelated_files(tmp_path):
    d = str(tmp_path)
    other = tmp_path / 'settings.json.bak'
    other.write_text('{}', encoding='utf-8')
    t = time.time() - 10_000
    os.utime(other, (t, t))
    su._reap_orphan_tmp_files(d)
    assert other.exists()
