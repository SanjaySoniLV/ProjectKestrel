"""Headless cloud / Perch commands for CI E2E (and local smoke).

Driven by cli.py's mode flags: --selftest-reach / --cc-run / --perch-upload /
--e2e. Each builds a headless ``Api()`` (no webview), drives the SAME upload /
cloud-compute / Perch code paths the GUI uses, logs human progress to stderr,
emits a single JSON result object to stdout, and returns a process exit code
(0 = success, non-zero = failure). ``--json-out PATH`` also writes the JSON.

Why this is the macOS de-risk: these commands exercise the real
``cloud_compute_client`` and ``perch_uploader`` urllib paths, so running this
against a frozen ``.app`` on a CI macOS runner is what catches the
CERTIFICATE_VERIFY_FAILED bundling bug (net_tls) — a source run would not.

Auth: no special handling. The Api bridge loads the on-disk token bundle
(OS keychain / auth.json) exactly as the GUI does and auto-refreshes it, so CI
just places a refresh-token bundle at the expected path before invoking.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from typing import Any


# ── output helpers ──────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    """Human progress → stderr, so stdout stays a single parseable JSON blob."""
    print(msg, file=sys.stderr, flush=True)


def _emit(obj: dict, json_out: str | None) -> None:
    text = json.dumps(obj, indent=2, sort_keys=True)
    print(text, flush=True)
    if json_out:
        try:
            with open(json_out, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:  # non-fatal — stdout already has it
            _log(f"warn: could not write --json-out {json_out}: {e}")


def _make_api():
    """Construct the Api bridge headlessly. Lazy import — api_bridge pulls in
    heavy deps we don't want loaded for the plain analysis CLI."""
    try:
        from api_bridge import Api  # root-on-sys.path (frozen / cli.py)
    except ImportError:  # pragma: no cover - package-style path
        from analyzer.api_bridge import Api
    return Api()


def _require_auth(api) -> dict | None:
    """Return an error dict if there's no usable auth token, else None."""
    try:
        token, dev_user, err = api._check_auth_token()
    except Exception as e:
        return {"error": "auth_check_failed", "detail": str(e)}
    if not token and not dev_user:
        return {"error": "not_signed_in", "detail": err}
    return None


# ── commands ────────────────────────────────────────────────────────────────

def cmd_selftest_reach(api, args) -> tuple[dict, int]:
    """Cheap reach probe: auth + a real upload-throughput test to the staging
    bucket (no GPU). Exercises token refresh, CC presign, and the R2 PUT TLS
    path — the exact surface the macOS frozen-app TLS fix protects."""
    autherr = _require_auth(api)
    if autherr:
        return {"command": "selftest-reach", "ok": False, **autherr}, 3
    _log(f"selftest-reach: uploading {args.sample_count} sample(s) from {args.folder}")
    res = api.cloud_compute_upload_test(args.folder, sample_count=args.sample_count)
    out = {"command": "selftest-reach", **res}
    return out, (0 if res.get("ok") else 1)


def _clear_kestrel_state(folder: str) -> None:
    """Delete the folder's .kestrel analysis state for a from-scratch run.
    Gated behind --clear; destructive, intended for disposable CI test folders."""
    kdir = os.path.join(folder, ".kestrel")
    if os.path.isdir(kdir):
        _log(f"--clear: removing {kdir}")
        shutil.rmtree(kdir, ignore_errors=True)


def _run_cloud_job(api, folder: str, timeout_sec: int) -> tuple[dict, bool]:
    """Submit a cloud-compute job and wait for it to fully finish locally.

    submit_job spawns a background worker that uploads, waits on the GPU,
    downloads + merges result packs, and flips local status -> "done" once the
    Worker reports complete AND every pack is pulled (api_bridge
    _cc_maybe_mark_done). So we just POLL for status == "done" and let that
    worker do its thing — calling retrieve_results here would spawn a SECOND
    drainer that races it and can strand the done-flip.

    Fallback: if the Worker reports complete but the local merge hasn't reached
    "done" after a grace period (the worker died/stalled — the app-restart
    resume scenario), kick retrieve_results ONCE; by then there's no live
    worker to race.
    """
    sub = api.cloud_compute_submit_job(folder)
    if not sub.get("ok"):
        return {"stage": "submit", "ok": False, "error": sub.get("error"), "detail": sub}, False
    job_id = sub["jobId"]
    _log(f"cc: submitted job {job_id} "
         f"({sub.get('newImageCount')}/{sub.get('imageCount')} new image(s))")

    _LOCAL_TERMINAL = ("done", "failed", "cancelled")
    _STUCK_GRACE_SEC = 90
    deadline = time.monotonic() + timeout_sec
    complete_since = None
    resumed = False
    last = {}
    while True:
        last = api.cloud_compute_get_status(job_id)
        status = last.get("status")
        _log(f"cc: status={status} uploaded={last.get('uploadedCount')} "
             f"analyzed={last.get('analyzedCount')} downloaded={last.get('downloadedCount')} "
             f"remote={last.get('terminalReason')}")
        if status in _LOCAL_TERMINAL:
            break
        # Worker says complete but local merge hasn't flipped to done -> if it
        # stays that way past the grace window, the live worker is gone; resume.
        if last.get("terminalReason") == "complete" or last.get("resultsAvailable"):
            if complete_since is None:
                complete_since = time.monotonic()
            elif not resumed and (time.monotonic() - complete_since) > _STUCK_GRACE_SEC:
                _log("cc: results ready but not merged after grace; resuming drain")
                api.cloud_compute_retrieve_results(job_id)
                resumed = True
        if time.monotonic() > deadline:
            return {"stage": "poll", "ok": False, "error": "timeout",
                    "jobId": job_id, "lastStatus": last}, False
        time.sleep(5)

    ok = last.get("status") == "done"
    summary = {
        "stage": "done" if ok else last.get("status"),
        "ok": ok,
        "jobId": job_id,
        "status": last.get("status"),
        "terminalReason": last.get("terminalReason"),
        "imageCount": sub.get("imageCount"),
        "analyzedCount": last.get("analyzedCount"),
    }
    return summary, ok


def _upload_to_perch(api, folder: str, timeout_sec: int) -> tuple[dict, bool]:
    """Upload an analyzed folder to Perch, poll to done, return the link."""
    share = api.share_with_perch(folder)
    if not share.get("success"):
        return {"stage": "share_start", "ok": False, "error": share.get("error"),
                "detail": share}, False
    job_id = share["job_id"]
    _log(f"perch: upload job {job_id}")
    deadline = time.monotonic() + timeout_sec
    while True:
        prog = (api.get_share_progress(job_id) or {}).get("progress", {}) or {}
        phase = prog.get("phase")
        _log(f"perch: phase={phase} uploaded={prog.get('uploaded')}/{prog.get('total')}")
        if phase == "done":
            return {"stage": "done", "ok": True, "perchUrl": prog.get("perch_url"),
                    "perchId": prog.get("perch_id"), "sceneCount": prog.get("total")}, True
        if phase == "error":
            return {"stage": "error", "ok": False, "error": prog.get("error") or "perch_upload_failed",
                    "detail": prog}, False
        if time.monotonic() > deadline:
            return {"stage": "poll", "ok": False, "error": "timeout", "lastPhase": phase}, False
        time.sleep(2)


def cmd_cc_run(api, args) -> tuple[dict, int]:
    autherr = _require_auth(api)
    if autherr:
        return {"command": "cc-run", "ok": False, **autherr}, 3
    if args.clear:
        _clear_kestrel_state(args.folder)
    summary, ok = _run_cloud_job(api, args.folder, args.cloud_timeout)
    return {"command": "cc-run", **summary}, (0 if ok else 1)


def cmd_perch_upload(api, args) -> tuple[dict, int]:
    autherr = _require_auth(api)
    if autherr:
        return {"command": "perch-upload", "ok": False, **autherr}, 3
    summary, ok = _upload_to_perch(api, args.folder, args.cloud_timeout)
    return {"command": "perch-upload", **summary}, (0 if ok else 1)


def cmd_e2e(api, args) -> tuple[dict, int]:
    """Full headless E2E: (optional --clear) cloud job → retrieve → Perch upload."""
    autherr = _require_auth(api)
    if autherr:
        return {"command": "e2e", "ok": False, **autherr}, 3
    if args.clear:
        _clear_kestrel_state(args.folder)
    cc_summary, cc_ok = _run_cloud_job(api, args.folder, args.cloud_timeout)
    if not cc_ok:
        return {"command": "e2e", "ok": False, "stage": "cloud", "cloud": cc_summary}, 1
    perch_summary, perch_ok = _upload_to_perch(api, args.folder, args.cloud_timeout)
    return ({"command": "e2e", "ok": perch_ok, "cloud": cc_summary, "perch": perch_summary},
            0 if perch_ok else 1)


_DISPATCH = {
    "selftest_reach": cmd_selftest_reach,
    "cc_run": cmd_cc_run,
    "perch_upload": cmd_perch_upload,
    "e2e": cmd_e2e,
}


def run_cloud_command(args) -> int:
    """Entry point from cli.py. Picks the single selected mode, runs it, emits
    the JSON result, returns the exit code."""
    # Frozen Windows builds default stdout/stderr to cp1252; a unicode filename
    # or title in a log line would otherwise crash with UnicodeEncodeError.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    selected = [name for name in _DISPATCH if getattr(args, name, False)]
    if len(selected) != 1:
        _log("error: select exactly one headless cloud command")
        return 2
    name = selected[0]
    try:
        api = _make_api()
    except Exception as e:
        _emit({"command": name, "ok": False, "error": "api_init_failed", "detail": str(e)},
              getattr(args, "json_out", None))
        return 4
    try:
        result, code = _DISPATCH[name](api, args)
    except Exception as e:
        result, code = {"command": name, "ok": False, "error": "unhandled_exception",
                        "detail": str(e)}, 5
    _emit(result, getattr(args, "json_out", None))
    return code
