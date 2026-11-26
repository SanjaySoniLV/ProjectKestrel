"""js/analyze-estimate.js: Analyze dialog time estimates and per-folder plan, run under Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EST_JS = str(Path(__file__).resolve().parents[2] / "js" / "analyze-estimate.js")
NODE = shutil.which("node")

pytestmark = [pytest.mark.unit, pytest.mark.skipif(NODE is None, reason="node not installed")]

_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = vm.createContext({ window: {} });
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const E = ctx.window.KestrelAnalyzeEstimate;
const calls = JSON.parse(process.argv[2]);
const fix = v => (v === Infinity ? 'Infinity' : v);
process.stdout.write(JSON.stringify(calls.map(([fn, args]) => E[fn](...args.map(a => (a === 'Infinity' ? Infinity : a)))), (k, v) => fix(v)));
"""


def _run(*calls):
    out = subprocess.run([NODE, "-e", _HARNESS, EST_JS, json.dumps(calls)],
                         capture_output=True, text=True, check=True, encoding="utf-8")
    return json.loads(out.stdout)


# ── local rate ──────────────────────────────────────────────────────────────

def test_local_rate_uses_baseline_until_enough_samples():
    few = [{"imgs": 40, "secs": 40}]
    gpu, cpu = _run(["localRate", [few, True]], ["localRate", [few, False]])
    assert gpu == {"rate": 4.0, "source": "baseline", "sampledImgs": 40}
    assert cpu["rate"] == 10.0


def test_local_rate_switches_to_measured_and_ignores_bad_samples():
    samples = [{"imgs": 80, "secs": 100}, {"imgs": 40, "secs": 50}, {"imgs": 0, "secs": 9}, None, {"imgs": "x"}]
    (got,) = _run(["localRate", [samples, True]])
    assert got == {"rate": 150 / 120, "source": "local", "sampledImgs": 120}


def test_local_rate_tolerates_missing_samples():
    (got,) = _run(["localRate", [None, True]])
    assert got["source"] == "baseline"


# ── cloud ───────────────────────────────────────────────────────────────────

def test_cloud_params_take_only_finite_positive_served_values():
    (p,) = _run(["cloudParams", [{"maxContainers": 6, "dispatchThreshold": -1, "scaleThreshold": "abc", "extra": 9}]])
    assert p["maxContainers"] == 6
    assert p["dispatchThreshold"] == 100
    assert p["scaleThreshold"] == 200
    assert "extra" not in p


def test_cloud_estimate_without_speed_test_gives_best_case_only():
    (got,) = _run(["cloudEstimate", [3000, None]])
    assert got["secs"] is None and got["regime"] is None and got["hasTest"] is False
    # 3 containers at 1 photo/s, plus cold start and ramp-up.
    assert 1000 < got["analysisSecs"] < 1200


def test_cloud_estimate_slow_upload_is_upload_limited():
    test = {"mbps": 2.0, "samples_uploaded": 10, "total_bytes": 10 * 25 * 1048576}
    (got,) = _run(["cloudEstimate", [3000, test]])
    assert got["regime"] == "upload"
    assert got["uploadSecs"] == pytest.approx(3000 * 12.5)
    assert got["secs"] > got["uploadSecs"]


def test_cloud_estimate_fast_upload_is_compute_limited():
    test = {"mbps": 500.0, "samples_uploaded": 10, "total_bytes": 10 * 1048576}
    (got,) = _run(["cloudEstimate", [3000, test]])
    assert got["regime"] == "compute"
    assert got["secs"] == pytest.approx(got["analysisSecs"] * 1.05, rel=0.05)


def test_cloud_job_of_zero_photos_takes_no_time():
    assert _run(["simulateCloudJob", [0, "Infinity", None]]) == [0]


# ── per-folder plan ─────────────────────────────────────────────────────────

def _plan(folder, unlocked=False, retry=True):
    (got,) = _run(["planFolder", [folder, {"reanalyzeUnlocked": unlocked, "retryErrored": retry}]])
    return got


def test_plan_new_and_partial_folders_analyze_what_is_left():
    assert _plan({"total": 1240, "processed": 0, "errored": 0})["images"] == 1240
    got = _plan({"total": 1066, "processed": 400, "errored": 0})
    assert got["kind"] == "analyze" and got["images"] == 666


def test_plan_fully_analyzed_folder_is_skipped_unless_unlocked():
    done = {"total": 2310, "processed": 2310, "errored": 0}
    assert _plan(done)["kind"] == "skip"
    got = _plan(done, unlocked=True)
    assert got == {"kind": "reanalyze", "reason": "unlocked", "images": 2310, "retriedErrored": 0, "skippedErrored": 0}


def test_plan_upgrade_choice_wins_over_everything():
    folder = {"total": 1812, "processed": 1812, "errored": 0, "outdated": True}
    assert _plan({**folder, "upgradeMode": "upgrade"})["kind"] == "upgrade"
    assert _plan({**folder, "upgradeMode": "upgrade"})["images"] == 0
    got = _plan({**folder, "upgradeMode": "reanalyze"})
    assert (got["kind"], got["reason"], got["images"]) == ("reanalyze", "chosen", 1812)


def test_plan_outdated_folder_is_always_reanalyzed():
    got = _plan({"total": 500, "processed": 500, "errored": 0, "outdated": True})
    assert (got["kind"], got["reason"], got["images"]) == ("reanalyze", "outdated", 500)


def test_plan_failed_photos_follow_the_retry_setting():
    folder = {"total": 1000, "processed": 1000, "errored": 3}
    retry = _plan(folder, retry=True)
    assert retry["retriedErrored"] == 3 and retry["skippedErrored"] == 0
    skip = _plan(folder, retry=False)
    assert skip["skippedErrored"] == 3 and skip["retriedErrored"] == 0
