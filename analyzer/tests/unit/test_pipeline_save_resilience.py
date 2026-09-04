"""A transient CSV lock must not end an analysis run.

``save_database`` re-reads the CSV to recover the user's ``culled`` marks before
overwriting, and raises if that read fails. Raising is correct — writing without
the merge would discard those marks — but the failure it raises on is a Windows
sharing violation that ``retry_on_file_lock`` documents as routine ("the window
is wide open in practice"), and the save runs after *every* image.

Letting it propagate produced a three-step failure:

1. The per-image handler caught the save error and recorded the photo as
   ``species="Error"`` — misattributing a file lock to a photo that decoded fine.
2. The handler then called ``save_database`` again from inside its own ``except``.
3. That second raise had no handler left, escaped the per-image loop, and ended
   the whole run as "Fatal error".

These tests pin the fix: per-image saves skip on failure, the post-analysis save
stays strict, and the writer gets a longer retry ceiling than the UI's readers.
"""

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from kestrel_analyzer import pipeline as pipeline_mod
    from kestrel_analyzer.pipeline import AnalysisPipeline
except Exception as exc:  # pragma: no cover - environment-dependent
    # The pipeline pulls in cv2 / onnxruntime / rawpy; skip rather than fail the
    # file, matching the other native-dependency suites here.
    pytest.skip(
        f"kestrel_analyzer.pipeline unavailable in this environment: {exc}",
        allow_module_level=True,
    )

from kestrel_analyzer import database as db_mod
from kestrel_analyzer.database import (
    _SAVE_PRESERVE_READ_ATTEMPTS,
    BASE_COLUMNS,
    save_database,
)

pytestmark = pytest.mark.unit


_PIPELINE_SRC = (
    Path(__file__).parent.parent.parent / "kestrel_analyzer" / "pipeline.py"
).read_text(encoding="utf-8")


def _stub_pipeline(tmp_path):
    """An AnalysisPipeline without running __init__'s model construction."""
    p = AnalysisPipeline.__new__(AnalysisPipeline)
    p._log_path = str(tmp_path / "kestrel_error.jsonl")
    return p


def _frame(filename="IMG_001.CR3"):
    row = {col: None for col in BASE_COLUMNS}
    row["filename"] = filename
    row["species"] = "aves"
    row["quality"] = 0.5
    return pd.DataFrame([row])


# ---------------------------------------------------------------------------
# The soft-save helper
# ---------------------------------------------------------------------------


class TestSoftSave:
    def test_failure_does_not_raise(self, tmp_path, monkeypatch):
        """The whole point: a failed save must not propagate to the caller."""
        def boom(*_a, **_k):
            raise PermissionError("simulated sharing violation")

        monkeypatch.setattr(pipeline_mod, "save_database", boom)
        p = _stub_pipeline(tmp_path)

        # Must not raise.
        result = p._save_database_soft(_frame(), str(tmp_path / "db.csv"), "save_database")
        assert result is False

    def test_success_reports_true_and_writes(self, tmp_path):
        p = _stub_pipeline(tmp_path)
        csv_path = tmp_path / "kestrel_database.csv"

        assert p._save_database_soft(_frame(), str(csv_path), "save_database") is True
        assert pd.read_csv(csv_path).loc[0, "filename"] == "IMG_001.CR3"

    def test_failure_is_recorded_not_swallowed_silently(self, tmp_path, monkeypatch):
        """Skipping is only acceptable because it leaves a trace."""
        recorded = []

        def boom(*_a, **_k):
            raise PermissionError("simulated sharing violation")

        monkeypatch.setattr(pipeline_mod, "save_database", boom)
        monkeypatch.setattr(
            pipeline_mod,
            "log_exception",
            lambda *a, **k: recorded.append((a, k)),
        )

        p = _stub_pipeline(tmp_path)
        p._save_database_soft(_frame(), str(tmp_path / "db.csv"), "save_database")

        assert recorded, "a skipped save left no record at all"
        assert recorded[0][1].get("stage") == "save_database"

    def test_a_later_save_persists_what_a_skipped_one_missed(self, tmp_path, monkeypatch):
        """Why skipping is safe: the frame is cumulative and each save writes it whole.

        This is the property that makes the fix work without any buffering — a
        skipped save costs on-disk freshness for an image or two, not data.
        """
        csv_path = tmp_path / "kestrel_database.csv"
        p = _stub_pipeline(tmp_path)

        first = _frame("IMG_001.CR3")
        assert p._save_database_soft(first, str(csv_path), "save_database") is True

        # Image two: the save fails.
        both = pd.concat([first, _frame("IMG_002.CR3")], ignore_index=True)

        def boom(*_a, **_k):
            raise PermissionError("simulated sharing violation")

        monkeypatch.setattr(pipeline_mod, "save_database", boom)
        assert p._save_database_soft(both, str(csv_path), "save_database") is False
        assert pd.read_csv(csv_path)["filename"].tolist() == ["IMG_001.CR3"]

        # Image three succeeds and carries image two with it.
        monkeypatch.undo()
        all_three = pd.concat([both, _frame("IMG_003.CR3")], ignore_index=True)
        assert p._save_database_soft(all_three, str(csv_path), "save_database") is True
        assert pd.read_csv(csv_path)["filename"].tolist() == [
            "IMG_001.CR3",
            "IMG_002.CR3",
            "IMG_003.CR3",
        ]


# ---------------------------------------------------------------------------
# Which call sites are soft, and which must stay strict
# ---------------------------------------------------------------------------


class TestCallSitePolicy:
    """Structural, so the policy cannot drift back without a test failing.

    Driving these through ``process_folder`` would need real ML weights, so the
    source is read directly — the same approach ``test_pipeline_stage_instrumentation``
    uses for its cost invariant.
    """

    def test_no_bare_save_in_the_per_image_loop(self):
        """Exactly two bare calls may remain: inside the helper, and the final save.

        Any third is a per-image save that bypasses the soft helper, which is
        the regression this whole module exists to prevent.
        """
        bare = re.findall(
            r"^([ ]*)save_database\(database, db_path\)", _PIPELINE_SRC, re.M
        )
        assert len(bare) == 2, (
            f"expected exactly 2 bare save_database(database, db_path) calls — "
            f"the one inside _save_database_soft and the post-analysis save — "
            f"found {len(bare)} at indents {[len(i) for i in bare]}. A per-image "
            "save that bypasses the soft helper can end a run on a transient lock."
        )

        # The shallower one is the helper's own call; the deeper one is the
        # post-analysis save nested inside process_folder's try block.
        helper_indent, final_indent = sorted(len(i) for i in bare)
        assert helper_indent < final_indent

        helper_body = _PIPELINE_SRC[
            _PIPELINE_SRC.index("def _save_database_soft") :
        ][:2000]
        assert "save_database(database, db_path)" in helper_body, (
            "the shallow bare call is not the helper's — the other bare call "
            "may be an unguarded per-image save"
        )

    def test_error_handler_save_cannot_escape(self):
        """The save inside the per-image ``except`` is the one with no handler left."""
        assert 'self._save_database_soft(database, db_path, "save_error_entry")' in _PIPELINE_SRC, (
            "the save inside the per-image error handler must be soft; a raise "
            "there escapes the loop and ends the run while reporting the wrong cause"
        )
        assert "time.sleep(2)" in _PIPELINE_SRC

    def test_post_analysis_save_stays_strict(self):
        """No later save can cover for the final one, so it must surface."""
        idx = _PIPELINE_SRC.index('if not database.empty and "quality" in database.columns:')
        tail = _PIPELINE_SRC[idx : idx + 600]
        assert "save_database(database, db_path)" in tail
        assert "_save_database_soft" not in tail, (
            "the post-analysis save was made soft; a failure there would leave "
            "the run looking complete with results missing from disk"
        )


# ---------------------------------------------------------------------------
# The writer's retry ceiling
# ---------------------------------------------------------------------------


class TestWriterRetryCeiling:
    def test_preserve_read_asks_for_more_attempts_than_the_default(self, tmp_path, monkeypatch):
        """The writer waits longer than a UI reader, and does so explicitly."""
        seen = {}
        real = db_mod.retry_on_file_lock

        def spy(op, **kwargs):
            seen.update(kwargs)
            return real(op, **kwargs)

        csv_path = tmp_path / "kestrel_database.csv"
        _frame().to_csv(csv_path, index=False)

        monkeypatch.setattr(db_mod, "retry_on_file_lock", spy)
        save_database(_frame(), str(csv_path))

        assert seen.get("attempts") == _SAVE_PRESERVE_READ_ATTEMPTS
        assert _SAVE_PRESERVE_READ_ATTEMPTS > 12, (
            "the writer's ceiling must exceed retry_on_file_lock's default, or "
            "raising it achieved nothing"
        )

    def test_readers_keep_the_short_default(self, tmp_path, monkeypatch):
        """The UI's reader must not inherit the writer's longer wait.

        ``read_database_csv`` is shared with the window's auto-refresh; a
        multi-second block there would be a visible freeze.
        """
        seen = []
        real = db_mod.retry_on_file_lock

        def spy(op, **kwargs):
            seen.append(kwargs)
            return real(op, **kwargs)

        csv_path = tmp_path / "kestrel_database.csv"
        _frame().to_csv(csv_path, index=False)

        monkeypatch.setattr(db_mod, "retry_on_file_lock", spy)
        db_mod.read_database_csv(str(csv_path))

        assert seen == [{}], f"reader passed a non-default retry ceiling: {seen}"
