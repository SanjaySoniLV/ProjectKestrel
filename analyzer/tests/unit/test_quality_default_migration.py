"""Legacy quality scorer as the default, and its one-time migration for existing users.

Settings I/O is redirected to a temp directory; nothing here touches the real settings file.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import settings_utils as su  # noqa: E402
from kestrel_analyzer import quality_feedback as qf  # noqa: E402
from kestrel_analyzer import quality_model as qm  # noqa: E402

pytestmark = pytest.mark.unit

MARKER = su.QUALITY_MODEL_MIGRATED_KEY


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(su, "_get_settings_path", lambda: str(path))
    monkeypatch.setattr(su, "_QM_MIGRATION_PERSIST_TRIED", False)
    return path


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ── Defaults ───────────────────────────────────────────────────────────────


def test_defaults_are_legacy_everywhere():
    assert qm.DEFAULT_QUALITY_MODEL == "legacy"
    assert su.DEFAULT_QUALITY_MODEL == qm.DEFAULT_QUALITY_MODEL
    assert qm.resolve(None) == "legacy" and qm.resolve("") == "legacy" and qm.resolve("bogus") == "legacy"
    assert qm.resolve("SigLIP") == "siglip"
    assert su._sanitize_settings_payload({"quality_model": "bogus"})["quality_model"] == "legacy"
    assert su._sanitize_settings_payload({"quality_model": "siglip"})["quality_model"] == "siglip"


def test_effective_follows_the_default_and_falls_back_to_the_other_scorer():
    assert qm.effective(0.8, 0.3, qm.DEFAULT_QUALITY_MODEL) == 0.3
    assert qm.effective(0.8, -1, "legacy") == 0.8     # legacy missing -> SigLIP
    assert qm.effective(-1, 0.3, "siglip") == 0.3


def test_quality_feedback_reports_legacy_by_default():
    rows = [{"filename": "a", "crop_path": "c", "quality": 0.3, "quality_siglip": 0.8, "quality_legacy": 0.3}]
    assert qf.eligible_rows(rows)  # default arg accepted
    import inspect
    assert inspect.signature(qf.eligible_rows).parameters["active_scorer"].default == "legacy"
    assert inspect.signature(qf.select_feedback_frames).parameters["active_scorer"].default == "legacy"


def test_marker_is_registered_in_the_sanitizer():
    assert su._sanitize_settings_payload({MARKER: True})[MARKER] is True
    assert su._sanitize_settings_payload({MARKER: "garbage"})[MARKER] is False
    assert MARKER not in su._sanitize_settings_payload({})


# ── Migration ──────────────────────────────────────────────────────────────


def test_new_user_without_a_settings_file(settings_file):
    assert su.load_persisted_settings() == {}
    assert not settings_file.exists()                       # loading must not create the file
    assert qm.settings_quality_model() == "legacy"           # CLI before the app ever opened
    assert not settings_file.exists()


def test_new_user_first_save_is_stamped_so_a_siglip_choice_is_kept(settings_file):
    su.save_persisted_settings({"quality_model": "siglip"})
    assert _read(settings_file)["quality_model"] == "siglip" and _read(settings_file)[MARKER] is True
    assert su.load_persisted_settings()["quality_model"] == "siglip"
    assert qm.settings_quality_model() == "siglip"


def test_stored_siglip_without_marker_is_migrated(settings_file):
    _write(settings_file, {"quality_model": "siglip", "editor": "darktable", "kestrel_impact_total_files": 7})
    assert qm.settings_quality_model() == "legacy"           # the CLI path
    on_disk = _read(settings_file)
    assert on_disk["quality_model"] == "legacy" and on_disk[MARKER] is True
    assert on_disk["kestrel_impact_total_files"] == 7 and on_disk["editor"] == "darktable"
    assert su.load_persisted_settings()["quality_model"] == "legacy"


def test_stored_legacy_without_marker_gets_the_marker(settings_file):
    _write(settings_file, {"quality_model": "legacy"})
    assert su.load_persisted_settings()["quality_model"] == "legacy"
    assert _read(settings_file)[MARKER] is True


def test_settings_without_a_quality_model_key_are_migrated(settings_file):
    _write(settings_file, {"editor": "darktable"})
    assert su.load_persisted_settings()["quality_model"] == "legacy"
    assert _read(settings_file)["quality_model"] == "legacy" and _read(settings_file)[MARKER] is True


def test_later_choice_of_siglip_is_kept_forever(settings_file):
    _write(settings_file, {"quality_model": "siglip"})
    assert su.load_persisted_settings()["quality_model"] == "legacy"      # migrated
    su.save_persisted_settings({**su.load_persisted_settings(), "quality_model": "siglip"})
    for _ in range(3):
        assert su.load_persisted_settings()["quality_model"] == "siglip"
    assert _read(settings_file)["quality_model"] == "siglip"
    # A new process (flag reset) must not migrate again.
    su._QM_MIGRATION_PERSIST_TRIED = False
    assert su.load_persisted_settings()["quality_model"] == "siglip"


def test_save_that_omits_the_marker_does_not_re_arm_the_migration(settings_file):
    _write(settings_file, {"quality_model": "legacy", MARKER: True})
    su.save_persisted_settings({"quality_model": "siglip"})            # a partial caller
    assert _read(settings_file)[MARKER] is True
    su._QM_MIGRATION_PERSIST_TRIED = False
    assert su.load_persisted_settings()["quality_model"] == "siglip"


def test_migration_writes_once_and_is_idempotent(settings_file, monkeypatch):
    _write(settings_file, {"quality_model": "siglip"})
    calls = []
    real = su.save_persisted_settings
    monkeypatch.setattr(su, "save_persisted_settings", lambda d: (calls.append(1), real(d))[1])
    for _ in range(4):
        assert su.load_persisted_settings()["quality_model"] == "legacy"
    assert len(calls) == 1


def test_corrupt_settings_without_backup_do_not_raise(settings_file):
    settings_file.write_text("{not json", encoding="utf-8")
    assert su.load_persisted_settings() == {}
    assert qm.settings_quality_model() == "legacy"
    assert settings_file.read_text(encoding="utf-8") == "{not json"      # left for manual recovery


def test_corrupt_settings_with_backup_are_migrated_from_the_backup(settings_file):
    settings_file.write_text("{not json", encoding="utf-8")
    Path(str(settings_file) + ".bak").write_text(json.dumps({"quality_model": "siglip"}), encoding="utf-8")
    assert su.load_persisted_settings()["quality_model"] == "legacy"
    assert _read(settings_file)["quality_model"] == "legacy" and _read(settings_file)[MARKER] is True


def test_failed_write_does_not_break_startup(settings_file, monkeypatch):
    _write(settings_file, {"quality_model": "siglip"})

    def boom(_d):
        raise PermissionError("read-only")

    monkeypatch.setattr(su, "save_persisted_settings", boom)
    assert su.load_persisted_settings()["quality_model"] == "legacy"      # still legacy in memory
    assert _read(settings_file)["quality_model"] == "siglip"              # unchanged on disk
    assert qm.settings_quality_model() == "legacy"
