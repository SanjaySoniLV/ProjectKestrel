"""Regression tests for ``Api.apply_normalization`` reading a live CSV.

``apply_normalization`` runs on every browse refresh, so it reads
``kestrel_database.csv`` at the same time the analysis pipeline is saving it.
The save is atomic (temp file + ``os.replace``), but on Windows that only
guarantees all-or-nothing *content*: CPython opens files without
``FILE_SHARE_DELETE``, so a reader landing inside the rename window gets
``PermissionError`` instead. Every other reader in the app goes through
``kestrel_analyzer.database.read_database_csv``, which retries across that
window; this one used to call ``pd.read_csv`` directly and surfaced the
collision to the user as ``apply_normalization error: [Errno 13] Permission
denied``, leaving the folder with no star ratings.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import kestrel_analyzer.database as _dbmod

pytestmark = pytest.mark.unit

_ANALYZER_DIR = Path(__file__).parent.parent.parent


def _folder_with_database(tmp_path: Path) -> str:
    kestrel_dir = tmp_path / ".kestrel"
    kestrel_dir.mkdir()
    (kestrel_dir / "kestrel_database.csv").write_text(
        "filename,quality\nDSC_0001.NEF,0.95\nDSC_0002.NEF,0.10\n",
        encoding="utf-8",
    )
    return str(tmp_path)


class TestApplyNormalizationToleratesTheRenameWindow:
    def test_a_transient_permission_error_is_retried(self, tmp_path, monkeypatch):
        """One PermissionError must not cost the folder its ratings."""
        folder = _folder_with_database(tmp_path)
        real_read_csv = _dbmod.pd.read_csv
        calls = {"n": 0}

        def flaky(path, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read_csv(path, *args, **kwargs)

        monkeypatch.setattr(_dbmod.pd, "read_csv", flaky)

        result = api_bridge.Api().apply_normalization(folder)

        assert calls["n"] >= 2, "the read was not retried"
        assert result["success"] is True, result.get("error")
        assert result["normalized_ratings"]["DSC_0001.NEF"] == 5
        assert result["normalized_ratings"]["DSC_0002.NEF"] == 1

    def test_a_permanent_permission_error_still_reports(self, tmp_path, monkeypatch):
        """Retrying must not turn a real permission problem into a silent pass."""
        folder = _folder_with_database(tmp_path)

        def always_denied(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(_dbmod.pd, "read_csv", always_denied)

        result = api_bridge.Api().apply_normalization(folder)

        assert result["success"] is False
        assert "Permission denied" in result["error"]

    def test_ratings_are_unchanged_when_the_read_succeeds(self, tmp_path):
        """The retrying reader must return exactly what the plain one did."""
        folder = _folder_with_database(tmp_path)

        result = api_bridge.Api().apply_normalization(folder)

        assert result["success"] is True
        assert result["normalized_ratings"] == {"DSC_0001.NEF": 5, "DSC_0002.NEF": 1}


class TestNoBareDatabaseReadsRemain:
    """The bridge must not reintroduce an unguarded database read.

    ``read_kestrel_csv`` and ``_load_repair_frame`` already go through the
    retrying helpers; this keeps ``apply_normalization`` in line with them.
    """

    def test_api_bridge_has_no_bare_read_of_the_database_csv(self):
        src = (_ANALYZER_DIR / "api_bridge.py").read_text(encoding="utf-8")
        offenders = re.findall(r"pd\.read_csv\(\s*csv_path", src)
        assert not offenders, (
            "kestrel_database.csv must be read through read_database_csv / "
            "read_database_text so the Windows rename window is retried"
        )
