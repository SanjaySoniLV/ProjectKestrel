"""Unit tests for the analysis pipeline's crash-localisation instrumentation.

A native-level process death — a segfault or abort inside onnxruntime, LibRaw
or OpenCV — never unwinds to an ``except`` block, so ``log_exception`` never
runs and the in-memory ``stage_ctx`` is lost. That is precisely the crash class
we cannot currently diagnose: the analysis log ends at ``analysis_start`` and
the next event never arrives, which narrows the fault only to "somewhere
between opening the database and finishing the first decode batch" — a window
that contains ONNX session construction, GPU provider initialisation, and RAW
decoding.

These tests pin the two additions that close that gap:

1. Each once-per-run setup stage is *persisted* as it is entered, so the last
   record in the log names the stage the process died in.
2. ``analysis_start`` records the GPU request and ``models_loaded`` records
   what ONNX Runtime actually granted, so a silent GPU->CPU fallback is
   visible in a crash report.

They also pin the invariant that all of this is observability only: it must
never change what a run does, and must never fail a run.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from kestrel_analyzer.pipeline import AnalysisPipeline
    from kestrel_analyzer.logging_utils import log_event, read_log_entries
    from kestrel_analyzer.ml import GPU_EP
except Exception as exc:  # pragma: no cover - environment-dependent
    # The pipeline pulls in cv2 / onnxruntime / rawpy. Several CI containers
    # have no wheel for one of them; skip rather than fail the whole file, the
    # same way the other native-dependency suites here do.
    pytest.skip(
        f"kestrel_analyzer.pipeline unavailable in this environment: {exc}",
        allow_module_level=True,
    )


# Running process_folder() under pytest hits a pre-existing, unrelated bug:
# pytest resets warning filters to "always", so the DeprecationWarning that
# datetime.utcnow() raises on Python 3.12+ reaches the pipeline's own
# showwarning hook, whose log write calls utcnow() again -> RecursionError.
# It is not reachable in a normal run (Python's default filter shows each
# warning once) and it is not caused by anything here; PR #129
# "fix(logging): avoid utcnow RecursionError in showwarning hook" is the fix.
# Scoped narrowly to the integration tests that actually drive process_folder,
# so it cannot hide a regression in the code under test.
_SUPPRESS_UTCNOW_RECURSION = pytest.mark.filterwarnings(
    "ignore:datetime.datetime.utcnow:DeprecationWarning"
)


def _read_log(folder: Path) -> list:
    """Return every event written to the analysis log under ``folder``.

    Deliberately goes through the production reader rather than parsing here,
    so these tests break if the on-disk format and its reader ever drift
    apart. Both extensions are globbed: sessions write ``.jsonl``, while a few
    cases below point ``_log_path`` at an explicit ``.json`` name.
    """
    log_dir = folder / ".kestrel"
    entries = []
    for log_file in sorted(log_dir.glob("*.json")) + sorted(log_dir.glob("*.jsonl")):
        entries.extend(read_log_entries(str(log_file)))
    return entries


def _stub_pipeline(use_gpu=False):
    """An AnalysisPipeline without running __init__'s siblings.

    ``__init__`` only assigns attributes, but going through ``__new__`` keeps
    these tests honest about which attributes ``_log_resolved_providers``
    actually depends on.
    """
    pipeline = AnalysisPipeline.__new__(AnalysisPipeline)
    pipeline.use_gpu = use_gpu
    pipeline.sn_sam = None
    pipeline.species_clf = None
    pipeline.quality_clf = None
    pipeline._log_path = None
    return pipeline


# ---------------------------------------------------------------------------
# Stage markers
# ---------------------------------------------------------------------------


@_SUPPRESS_UTCNOW_RECURSION
def test_setup_stage_is_persisted_before_it_can_crash(tmp_path):
    """An empty folder still records the stage it reached.

    ``list_files`` is the first marked stage. Running against a folder with no
    supported images returns early, which makes this the cheapest possible
    exercise of the marker: no models load, no images decode.
    """
    AnalysisPipeline(use_gpu=False).process_folder(str(tmp_path))

    events = _read_log(tmp_path)
    stages = [e.get("stage") for e in events if e.get("event") == "stage"]

    assert "list_files" in stages, (
        "the list_files stage was not persisted; a process death during "
        f"folder enumeration would be unattributable. Events: {events}"
    )


@_SUPPRESS_UTCNOW_RECURSION
def test_stage_events_precede_the_work_they_describe(tmp_path):
    """The marker is written on entry, not on completion.

    Written on completion it would be useless — a stage that crashes would
    never log. The empty-folder run must therefore show ``stage: list_files``
    *before* the ``no_supported_files`` event that ends it.
    """
    AnalysisPipeline(use_gpu=False).process_folder(str(tmp_path))

    events = _read_log(tmp_path)
    ordering = [
        e.get("event")
        for e in events
        if e.get("event") in ("stage", "no_supported_files")
    ]

    assert ordering[:2] == ["stage", "no_supported_files"], (
        f"expected the stage marker to be written first, got {ordering}"
    )


@_SUPPRESS_UTCNOW_RECURSION
def test_instrumentation_does_not_suppress_the_early_return(tmp_path):
    """Observability only: the empty-folder path still reports itself.

    Guards against the marker swallowing or reordering the behaviour it is
    meant to observe.
    """
    statuses = []
    AnalysisPipeline(use_gpu=False).process_folder(
        str(tmp_path), callbacks={"on_status": statuses.append}
    )

    assert any("No supported image files" in s for s in statuses), statuses
    assert any(
        e.get("event") == "no_supported_files" for e in _read_log(tmp_path)
    )


# ---------------------------------------------------------------------------
# GPU request vs. GPU grant
# ---------------------------------------------------------------------------


@_SUPPRESS_UTCNOW_RECURSION
def test_analysis_start_records_the_gpu_request(tmp_path):
    """``analysis_start`` must carry use_gpu, or a crash report cannot say
    whether the GPU path was even in play.

    A folder with one unreadable file is enough: enumeration finds a candidate,
    so ``analysis_start`` fires, and the run fails later without needing models
    that this environment may not be able to load.
    """
    (tmp_path / "IMG_0001.JPG").write_bytes(b"not a real jpeg")

    try:
        AnalysisPipeline(use_gpu=False).process_folder(str(tmp_path))
    except Exception:
        # Model loading may legitimately fail here; analysis_start is written
        # before that point, which is the whole reason it is a useful marker.
        pass

    starts = [e for e in _read_log(tmp_path) if e.get("event") == "analysis_start"]
    assert starts, "analysis_start was never written"
    assert starts[0]["use_gpu"] is False
    assert starts[0]["gpu_ep"] == GPU_EP


def test_models_loaded_reports_the_providers_actually_granted(tmp_path):
    """``providers`` reflects what ONNX Runtime returned, per model."""

    class _Clf:
        def __init__(self, providers):
            self.providers_used = providers

    pipeline = _stub_pipeline(use_gpu=True)
    pipeline._log_path = str(tmp_path / ".kestrel" / "log.json")
    (tmp_path / ".kestrel").mkdir()
    pipeline.quality_clf = _Clf(["CPUExecutionProvider"])
    pipeline.species_clf = _Clf([GPU_EP, "CPUExecutionProvider"])

    pipeline._log_resolved_providers()

    events = _read_log(tmp_path)
    loaded = [e for e in events if e.get("event") == "models_loaded"]
    assert len(loaded) == 1, events

    # The gap this exists to expose: GPU was requested, quality fell back.
    assert loaded[0]["use_gpu"] is True
    assert loaded[0]["providers"]["quality"] == ["CPUExecutionProvider"]
    assert loaded[0]["providers"]["species"] == [GPU_EP, "CPUExecutionProvider"]


def test_models_loaded_tolerates_classifiers_that_never_loaded(tmp_path):
    """Missing or provider-less classifiers are omitted, not fatal.

    This runs on the crash path, where a model may be half-constructed. It must
    degrade to a thinner record rather than raising and masking the real fault.
    """
    pipeline = _stub_pipeline(use_gpu=False)
    pipeline._log_path = str(tmp_path / ".kestrel" / "log.json")
    (tmp_path / ".kestrel").mkdir()

    pipeline._log_resolved_providers()

    loaded = [e for e in _read_log(tmp_path) if e.get("event") == "models_loaded"]
    assert len(loaded) == 1
    assert loaded[0]["providers"] == {}


def test_provider_logging_never_raises_into_the_run(tmp_path):
    """An unwritable log path must not turn into an analysis failure."""
    pipeline = _stub_pipeline()
    # A path whose parent does not exist — log_event will raise internally.
    pipeline._log_path = str(tmp_path / "no" / "such" / "dir" / "log.json")

    pipeline._log_resolved_providers()  # must not raise


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


def test_per_file_stages_are_not_persisted(tmp_path):
    """Only once-per-run stages are written.

    ``log_event`` reads, re-serialises and rewrites the entire JSON log on
    every call, so persisting the per-file stages would make a run O(n^2) in
    file count. A 6000-image folder is a real workload here, so this is a
    correctness constraint, not a micro-optimisation.
    """
    source = Path(__file__).parent.parent.parent / "kestrel_analyzer" / "pipeline.py"
    text = source.read_text(encoding="utf-8")

    for per_file_stage in ("read_image", "compute_similarity"):
        assert f'_mark_stage("{per_file_stage}")' not in text, (
            f"{per_file_stage} is a per-file stage; persisting it would make "
            "the analysis log quadratic in file count"
        )
        assert f'stage_ctx["stage"] = "{per_file_stage}"' in text, (
            f"{per_file_stage} should still update stage_ctx for exception "
            "reporting, just without a log write"
        )
