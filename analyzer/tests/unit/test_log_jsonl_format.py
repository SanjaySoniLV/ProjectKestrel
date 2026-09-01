"""The analysis log is JSONL: append-only writes, per-line recovery.

Two properties motivated the format change, and each is pinned here.

**Cost.** The log was a single JSON array that ``log_event`` read,
re-serialised and rewrote on every call, so recording n entries cost O(n^2).
That is why only the four once-per-run setup stages were ever persisted:
marking the per-image stages would have made an analysis run quadratic in
file count. Appending one line is O(1) regardless of how much is already
there.

**Recoverability.** A process killed mid-write leaves a partial trailing
record. In a JSON array that makes the *whole document* unparseable, so the
log describing a crash is lost precisely when it is needed — the failure mode
this instrumentation exists to avoid. In JSONL the damage is confined to the
final line and every earlier entry still reads back.

Logs written before the switch are JSON arrays and installed builds still
hold them, so the reader must keep parsing both shapes.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import (  # noqa: E402
    LEGACY_LOG_FILE_EXTENSION,
    LOG_FILE_EXTENSION,
)
from kestrel_analyzer.logging_utils import (  # noqa: E402
    get_log_path,
    log_event,
    parse_log_text,
    read_log_entries,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def test_each_event_is_one_line(tmp_path):
    log_path = str(tmp_path / "log.jsonl")
    for i in range(5):
        log_event(log_path, {"level": "info", "event": "stage", "n": i})

    lines = [ln for ln in Path(log_path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 5
    assert [json.loads(ln)["n"] for ln in lines] == [0, 1, 2, 3, 4]


def test_writes_are_append_only(tmp_path):
    """The bytes already on disk are never rewritten.

    This is the property that makes the write O(1): the previous format
    reproduced the entire file on every call.
    """
    log_path = str(tmp_path / "log.jsonl")
    for i in range(20):
        log_event(log_path, {"event": "stage", "n": i})
    before = Path(log_path).read_bytes()

    log_event(log_path, {"event": "stage", "n": 20})
    after = Path(log_path).read_bytes()

    assert after.startswith(before), "an existing region of the log was rewritten"
    assert json.loads(after[len(before):].decode("utf-8").strip())["n"] == 20


def test_every_entry_is_timestamped(tmp_path):
    log_path = str(tmp_path / "log.jsonl")
    log_event(log_path, {"event": "analysis_start"})
    entry = read_log_entries(log_path)[-1]
    assert entry["timestamp_utc"].endswith("Z")
    assert entry["event"] == "analysis_start"


def test_a_newline_inside_a_value_stays_on_one_line(tmp_path):
    """Tracebacks contain newlines; one entry must remain one physical line."""
    log_path = str(tmp_path / "log.jsonl")
    log_event(log_path, {"event": "error", "traceback": "line one\nline two\nline three"})

    lines = [ln for ln in Path(log_path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    assert read_log_entries(log_path)[0]["traceback"] == "line one\nline two\nline three"


def test_unserializable_values_degrade_instead_of_losing_the_entry(tmp_path):
    """``default=str`` keeps a diagnostic record that would otherwise be lost.

    These entries exist to explain a failure. Raising on an odd value would
    discard the explanation and, on the crash path, mask the real fault.
    """
    class Opaque:
        def __str__(self):
            return "opaque-value"

    log_path = str(tmp_path / "log.jsonl")
    log_event(log_path, {"event": "stage", "context": {"obj": Opaque()}})

    assert read_log_entries(log_path)[0]["context"]["obj"] == "opaque-value"


def test_io_errors_still_propagate(tmp_path):
    """Callers on the crash path swallow these deliberately; don't pre-empt them.

    ``_mark_stage`` and ``_log_resolved_providers`` wrap their calls so
    instrumentation can never fail a run. Swallowing the error here instead
    would hide a misconfigured log directory from every other caller.
    """
    log_path = str(tmp_path / "no" / "such" / "dir" / "log.jsonl")
    with pytest.raises(OSError):
        log_event(log_path, {"event": "stage"})


# ---------------------------------------------------------------------------
# Reading — recoverability
# ---------------------------------------------------------------------------


def test_a_torn_final_line_costs_one_entry_not_the_log(tmp_path):
    """The headline benefit: a mid-write process death stays diagnosable."""
    log_path = tmp_path / "log.jsonl"
    for i in range(4):
        log_event(str(log_path), {"event": "stage", "n": i})

    # Simulate the process dying partway through writing the fifth record.
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write('{"event": "stage", "n": 4, "partial"')

    entries = read_log_entries(str(log_path))
    assert [e["n"] for e in entries] == [0, 1, 2, 3]


def test_a_truncated_legacy_array_is_not_recoverable(tmp_path):
    """Documents the failure the new format exists to avoid.

    A pretty-printed array spans many lines per object, so line-wise parsing
    cannot salvage it. One interrupted write costs the entire log — which is
    the whole argument for JSONL, so it is pinned rather than left implicit.
    """
    log_path = tmp_path / "legacy.json"
    log_path.write_text(
        '[\n  {\n    "event": "analysis_start"\n  },\n  {\n    "event": "sta',
        encoding="utf-8",
    )
    assert read_log_entries(str(log_path)) == []


def test_blank_lines_are_ignored(tmp_path):
    log_path = tmp_path / "log.jsonl"
    log_path.write_text('{"event": "a"}\n\n\n{"event": "b"}\n', encoding="utf-8")
    assert [e["event"] for e in read_log_entries(str(log_path))] == ["a", "b"]


def test_missing_file_reads_as_empty(tmp_path):
    assert read_log_entries(str(tmp_path / "absent.jsonl")) == []


def test_empty_file_reads_as_empty(tmp_path):
    log_path = tmp_path / "log.jsonl"
    log_path.write_text("", encoding="utf-8")
    assert read_log_entries(str(log_path)) == []


# ---------------------------------------------------------------------------
# Reading — legacy compatibility
# ---------------------------------------------------------------------------


def test_legacy_json_array_still_reads(tmp_path):
    """Installed builds hold these, and their crash reports must still parse."""
    log_path = tmp_path / "kestrel_error_20260725T182313Z.json"
    log_path.write_text(
        json.dumps(
            [
                {"timestamp_utc": "2026-07-25T18:23:13Z", "event": "analysis_start"},
                {"timestamp_utc": "2026-07-25T18:23:14Z", "event": "stage", "stage": "load_models"},
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    entries = read_log_entries(str(log_path))
    assert [e["event"] for e in entries] == ["analysis_start", "stage"]


def test_non_dict_members_are_dropped_from_a_legacy_array(tmp_path):
    log_path = tmp_path / "legacy.json"
    log_path.write_text('[{"event": "a"}, "junk", 7, {"event": "b"}]', encoding="utf-8")
    assert [e["event"] for e in read_log_entries(str(log_path))] == ["a", "b"]


def test_a_legacy_array_is_told_apart_by_its_first_character():
    """Format detection is structural, not name-based, so a misnamed file works."""
    assert parse_log_text('  \n [{"event": "a"}]') == [{"event": "a"}]
    assert parse_log_text('{"event": "a"}\n{"event": "b"}') == [
        {"event": "a"},
        {"event": "b"},
    ]


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def test_new_sessions_write_the_jsonl_extension(tmp_path):
    path = get_log_path(str(tmp_path))
    assert path.endswith(f".{LOG_FILE_EXTENSION}")
    assert LOG_FILE_EXTENSION == "jsonl"


def test_the_legacy_extension_is_still_declared():
    """Readers scan for it; dropping the constant would silently orphan old logs."""
    assert LEGACY_LOG_FILE_EXTENSION == "json"
