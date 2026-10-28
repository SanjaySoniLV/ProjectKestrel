"""Tests for the donate prompt: run-count copy and click counting.

The prompt fires when the lifetime photo count crosses a milestone. After a
folder finishes it leads with that run's numbers ("Kestrel just sorted 2,400
photos into 180 scenes"); at startup there is no run, so it uses lifetime-only
copy. Prompt shown / Support / Feedback / closed and the top-strip Support
button are counted through the telemetry module, only for users who opted in
to analytics.

The repo has no JS test runner. The frontend logic is tested by evaluating the
real block of ``analyzer/js/feedback-donate.js`` in Node with small DOM and
bridge stubs (skipped when Node is not installed), plus source-level checks
for the wiring in ``queue.js`` and ``welcome.js``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import kestrel_telemetry
import queue_manager

pytestmark = pytest.mark.unit

_ANALYZER_DIR = Path(__file__).resolve().parent.parent.parent
_DONATE_JS = _ANALYZER_DIR / "js" / "feedback-donate.js"


def _read(*parts: str) -> str:
    return (_ANALYZER_DIR.joinpath(*parts)).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Node harness for the frontend block
# ---------------------------------------------------------------------------

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const start = src.indexOf('    const DONATE_THRESHOLDS');
const end = src.indexOf('    /** Check if the cumulative total crosses a donation milestone.');
if (start < 0 || end < 0) throw new Error('donate block markers not found');
const block = src.slice(start, end);

function makeEnv(opts) {
  const calls = [];
  const els = {};
  const el = (id) => (els[id] = els[id] || { id, textContent: '', hidden: false });
  const listeners = [];
  const dlg = el('donateDlg');
  dlg.open = false;
  dlg.showModal = () => { dlg.open = true; };
  dlg.close = () => { dlg.open = false; listeners.forEach((f) => f()); };
  const state = { shown: (opts.shown || []).slice(), otherDialogOpen: !!opts.otherDialogOpen };
  const ctx = {
    calls, els, state,
    hasPywebviewApi: true,
    window: { pywebview: { api: { record_donate_event: (d) => calls.push(d) } } },
    _ensureSupportUrl: async () => 'u',
    _supportUrlSticky: opts.donateLink === false ? null : 'https://projectkestrel.org/support-me',
    kdebug: () => {},
    _loadDonateThresholdsShown: () => state.shown.slice(),
    _saveDonateThresholdsShown: (a) => { state.shown = a.slice(); },
    document: {
      getElementById: (id) => el(id),
      querySelector: () => (state.otherDialogOpen || dlg.open ? {} : null),
    },
  };
  vm.createContext(ctx);
  // Pin number formatting so the test doesn't depend on the machine locale.
  vm.runInContext("Number.prototype.toLocaleString = function () { return new Intl.NumberFormat('en-US').format(this); };", ctx);
  vm.runInContext(block + '\nthis.__onClose = _onDonatePromptClosed;', ctx);
  listeners.push(() => ctx.__onClose());
  return ctx;
}
const flush = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  const out = {};
  const pure = makeEnv({});
  out.copy = {
    full_run: pure.buildDonatePromptCopy(10000, { filesThisRun: 2400, scenes: 180 }),
    one_scene: pure.buildDonatePromptCopy(1000, { filesThisRun: 50, scenes: 1 }),
    no_scenes: pure.buildDonatePromptCopy(5000, { filesThisRun: 2400, scenes: 0 }),
    small_run: pure.buildDonatePromptCopy(1000, { filesThisRun: 49, scenes: 3 }),
    startup: pure.buildDonatePromptCopy(25000, null),
  };
  out.milestones = [999, 1000, 4999, 5000, 250000].map((n) => pure.donateMilestoneFor(n));
  out.runs = {
    whole: pure.donateRunFromQueueItem({ files_this_run: 300, total: 300, scene_total: 40 }),
    resumed: pure.donateRunFromQueueItem({ files_this_run: 100, total: 300, scene_total: 40 }),
    no_scene_total: pure.donateRunFromQueueItem({ files_this_run: 300, total: 300, scene_total: null }),
    none: pure.donateRunFromQueueItem(null),
  };

  // Analysis trigger, Support clicked.
  let e = makeEnv({});
  e._showNextDonateMilestone(10400, { filesThisRun: 2400, scenes: 180 }, 'analysis');
  await flush();
  e.noteDonatePromptChoice('support');
  e.els.donateDlg.close();
  await flush();
  out.support = { calls: e.calls, shown: e.state.shown,
                  heading: e.els.donateDlgHeading.textContent,
                  subline: e.els.donateDlgSubline.textContent,
                  sublineHidden: e.els.donateDlgSubline.hidden };

  // Startup trigger, Feedback clicked.
  e = makeEnv({ shown: [1000] });
  e._showNextDonateMilestone(6000, null, 'startup');
  await flush();
  e.noteDonatePromptChoice('feedback');
  e.els.donateDlg.close();
  await flush();
  out.feedback = { calls: e.calls, shown: e.state.shown,
                   heading: e.els.donateDlgHeading.textContent,
                   sublineHidden: e.els.donateDlgSubline.hidden };

  // Dismissed (Close button / Escape both end in dialog.close()).
  e = makeEnv({ donateLink: false });
  e._showNextDonateMilestone(1200, null, 'startup');
  await flush();
  e.els.donateDlg.close();
  await flush();
  out.closed = { calls: e.calls };

  // Another dialog open: nothing shown, nothing recorded, milestone kept.
  e = makeEnv({ otherDialogOpen: true });
  e._showNextDonateMilestone(1200, { filesThisRun: 1200, scenes: 90 }, 'analysis');
  await flush();
  out.blocked = { calls: e.calls, shown: e.state.shown };

  // All milestones up to the total already shown: no prompt.
  e = makeEnv({ shown: [1000, 5000] });
  e._showNextDonateMilestone(6000, null, 'startup');
  await flush();
  out.already = { calls: e.calls, shown: e.state.shown };

  // Top-strip button event shape.
  e = makeEnv({});
  await e.recordDonateEvent('support_button_click', 0, '');
  out.button = { calls: e.calls };

  process.stdout.write(JSON.stringify(out));
})().catch((err) => { console.error(err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def js_results(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js not installed; frontend donate tests need it")
    harness = tmp_path_factory.mktemp("donate_js") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(_DONATE_JS)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestPromptCopy:
    def test_full_run_leads_with_photos_and_scenes(self, js_results):
        c = js_results["copy"]["full_run"]
        assert c["heading"] == "Kestrel just sorted 2,400 photos into 180 scenes"
        assert c["subline"] == "That takes you past 10,000 photos organized with Kestrel."

    def test_single_scene_is_singular(self, js_results):
        assert js_results["copy"]["one_scene"]["heading"] == "Kestrel just sorted 50 photos into 1 scene"

    def test_unknown_scenes_omits_scene_clause(self, js_results):
        c = js_results["copy"]["no_scenes"]
        assert c["heading"] == "Kestrel just sorted 2,400 photos"
        assert c["subline"] == "That takes you past 5,000 photos organized with Kestrel."

    def test_small_run_falls_back_to_lifetime_copy(self, js_results):
        c = js_results["copy"]["small_run"]
        assert c["heading"] == "You\u2019ve organized over 1,000 photos with Kestrel!"
        assert c["subline"] == ""

    def test_startup_has_lifetime_copy_only(self, js_results):
        c = js_results["copy"]["startup"]
        assert c["heading"] == "You\u2019ve organized over 25,000 photos with Kestrel!"
        assert c["subline"] == ""

    def test_milestone_rounds_down(self, js_results):
        assert js_results["milestones"] == [0, 1000, 1000, 5000, 200000]

    def test_scenes_only_for_whole_folder_runs(self, js_results):
        r = js_results["runs"]
        assert r["whole"] == {"filesThisRun": 300, "scenes": 40}
        # Resumed run: the folder's scene total covers photos from earlier
        # runs too, so it isn't reported next to this run's photo count.
        assert r["resumed"] == {"filesThisRun": 100, "scenes": 0}
        assert r["no_scene_total"] == {"filesThisRun": 300, "scenes": 0}
        assert r["none"] is None


class TestPromptEvents:
    def test_support_click_records_shown_then_support_only(self, js_results):
        res = js_results["support"]
        assert [c["event"] for c in res["calls"]] == ["donate_prompt_shown", "donate_prompt_support"]
        for c in res["calls"]:
            assert c == {"event": c["event"], "milestone": 10000, "trigger": "analysis", "donate_link": True}
        assert res["shown"] == [1000]  # one milestone per prompt, as before
        assert res["heading"] == "Kestrel just sorted 2,400 photos into 180 scenes"
        assert res["subline"] == "That takes you past 10,000 photos organized with Kestrel."
        assert res["sublineHidden"] is False

    def test_feedback_click_at_startup(self, js_results):
        res = js_results["feedback"]
        assert [c["event"] for c in res["calls"]] == ["donate_prompt_shown", "donate_prompt_feedback"]
        assert all(c["trigger"] == "startup" and c["milestone"] == 5000 for c in res["calls"])
        assert res["shown"] == [1000, 5000]
        assert res["heading"] == "You\u2019ve organized over 5,000 photos with Kestrel!"
        assert res["sublineHidden"] is True

    def test_dismiss_records_close(self, js_results):
        calls = js_results["closed"]["calls"]
        assert [c["event"] for c in calls] == ["donate_prompt_shown", "donate_prompt_close"]
        # No-payment build (Mac App Store): still counted, flagged donate_link False.
        assert all(c["donate_link"] is False for c in calls)

    def test_blocked_prompt_records_nothing_and_keeps_milestone(self, js_results):
        res = js_results["blocked"]
        assert res["calls"] == []
        assert res["shown"] == []

    def test_no_prompt_when_milestones_already_shown(self, js_results):
        res = js_results["already"]
        assert res["calls"] == []
        assert res["shown"] == [1000, 5000]

    def test_top_strip_button_event(self, js_results):
        assert js_results["button"]["calls"] == [
            {"event": "support_button_click", "milestone": 0, "trigger": "", "donate_link": True}
        ]


class TestFrontendWiring:
    """Source-level checks for wiring that needs the full app to exercise."""

    def test_queue_calls_threshold_check_with_run_numbers(self):
        src = _read("js", "queue.js")
        assert "donateRunFromQueueItem(justFinishedItem)" in src
        assert "checkDonationThresholdAsync(run)" in src

    def test_dialog_buttons_record_choice_before_closing(self):
        src = _read("js", "welcome.js")
        go = src.split("getElementById('donateDlgGoBtn')", 1)[1].split("});", 1)[0]
        fb = src.split("getElementById('donateDlgFeedbackBtn')", 1)[1].split("});", 1)[0]
        assert go.index("noteDonatePromptChoice('support')") < go.index("dlg.close()")
        assert fb.index("noteDonatePromptChoice('feedback')") < fb.index("dlg.close()")

    def test_top_strip_button_records_then_opens(self):
        src = _read("js", "feedback-donate.js")
        wiring = src.split("getElementById('donateBtnMain')", 1)[1].split("});", 1)[0]
        assert "recordDonateEvent('support_button_click'" in wiring
        assert "openSupportLink()" in wiring

    def test_dialog_has_heading_and_subline_targets(self):
        html = _read("visualizer.html")
        assert 'id="donateDlgHeading"' in html
        assert 'id="donateDlgSubline"' in html
        assert "consider supporting me" in html


# ---------------------------------------------------------------------------
# Python side: telemetry, bridge opt-in gate, queue numbers
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_posts(monkeypatch):
    posts = []
    monkeypatch.setattr(
        kestrel_telemetry, "_post_json_async",
        lambda endpoint, payload: posts.append((endpoint, payload)),
    )
    return posts


class TestSendDonateEvent:
    def test_valid_event_posts_to_ui_event(self, captured_posts, monkeypatch):
        monkeypatch.setattr(kestrel_telemetry, "get_build_label", lambda: "windows_installer")
        ok = kestrel_telemetry.send_donate_event(
            "donate_prompt_support", milestone=10000, trigger="analysis",
            donate_link=True, machine_id="m-123", version="v1",
        )
        assert ok is True
        assert len(captured_posts) == 1
        endpoint, payload = captured_posts[0]
        assert endpoint == "/api/ui_event"
        assert payload["event"] == "donate_prompt_support"
        assert payload["milestone"] == 10000
        assert payload["trigger"] == "analysis"
        assert payload["donate_link"] is True
        assert payload["build"] == "windows_installer"
        assert payload["machine_id"] == "m-123"
        assert payload["version"] == "v1"

    def test_unknown_event_is_dropped(self, captured_posts):
        assert kestrel_telemetry.send_donate_event("something_else") is False
        assert captured_posts == []

    def test_inputs_are_sanitised(self, captured_posts):
        kestrel_telemetry.send_donate_event(
            "donate_prompt_shown", milestone="junk", trigger="<script>", donate_link="yes",
        )
        payload = captured_posts[0][1]
        assert payload["milestone"] == 0
        assert payload["trigger"] == ""
        assert payload["donate_link"] is None

    def test_milestone_is_clamped(self, captured_posts):
        kestrel_telemetry.send_donate_event("donate_prompt_shown", milestone=-5)
        kestrel_telemetry.send_donate_event("donate_prompt_shown", milestone=10**15)
        assert [p[1]["milestone"] for p in captured_posts] == [0, 1_000_000_000]


class TestBuildLabel:
    def _set(self, monkeypatch, frozen, channel, platform, exe="/x/app"):
        monkeypatch.setattr(kestrel_telemetry, "is_frozen", lambda: frozen)
        monkeypatch.setattr(kestrel_telemetry, "_get_dist_channel", lambda: channel)
        monkeypatch.setattr(kestrel_telemetry.sys, "platform", platform)
        monkeypatch.setattr(kestrel_telemetry.sys, "executable", exe, raising=False)
        monkeypatch.delenv("APPX_PACKAGE_ROOT", raising=False)

    def test_source(self, monkeypatch):
        self._set(monkeypatch, False, "direct", "win32")
        assert kestrel_telemetry.get_build_label() == "source"

    def test_mac_appstore(self, monkeypatch):
        self._set(monkeypatch, True, "appstore", "darwin")
        assert kestrel_telemetry.get_build_label() == "mac_appstore"

    def test_mac_dmg(self, monkeypatch):
        self._set(monkeypatch, True, "direct", "darwin")
        assert kestrel_telemetry.get_build_label() == "mac_dmg"

    def test_windows_store(self, monkeypatch):
        self._set(monkeypatch, True, "direct", "win32",
                  exe=r"C:\Program Files\WindowsApps\Kestrel_1.0\ProjectKestrel.exe")
        assert kestrel_telemetry.get_build_label() == "windows_store"

    def test_windows_installer(self, monkeypatch):
        self._set(monkeypatch, True, "direct", "win32",
                  exe=r"C:\Program Files\Project Kestrel\ProjectKestrel.exe")
        assert kestrel_telemetry.get_build_label() == "windows_installer"


class TestRecordDonateEventBridge:
    """``Api.record_donate_event`` never touches ``self``; called unbound."""

    @pytest.fixture
    def sent(self, monkeypatch):
        calls = []

        def _fake_send(**kw):
            calls.append(kw)
            return kw["event"] in kestrel_telemetry.DONATE_EVENTS

        monkeypatch.setattr(api_bridge._telemetry, "send_donate_event", _fake_send)
        monkeypatch.setattr(api_bridge._telemetry, "get_machine_id", lambda s: "m-1")
        return calls

    def _settings(self, monkeypatch, settings):
        monkeypatch.setattr(api_bridge, "load_persisted_settings", lambda: dict(settings))

    def test_opted_out_sends_nothing(self, monkeypatch, sent):
        self._settings(monkeypatch, {"analytics_opted_in": False})
        res = api_bridge.Api.record_donate_event(None, {"event": "donate_prompt_shown"})
        assert res == {"success": True, "sent": False}
        assert sent == []

    def test_never_asked_sends_nothing(self, monkeypatch, sent):
        self._settings(monkeypatch, {})
        res = api_bridge.Api.record_donate_event(None, {"event": "donate_prompt_shown"})
        assert res["sent"] is False
        assert sent == []

    def test_opted_in_sends(self, monkeypatch, sent):
        self._settings(monkeypatch, {"analytics_opted_in": True})
        res = api_bridge.Api.record_donate_event(None, {
            "event": "donate_prompt_close", "milestone": 5000,
            "trigger": "startup", "donate_link": False,
        })
        assert res == {"success": True, "sent": True}
        assert sent == [{
            "event": "donate_prompt_close", "milestone": 5000, "trigger": "startup",
            "donate_link": False, "machine_id": "m-1",
        }]

    def test_bad_payload_is_harmless(self, monkeypatch, sent):
        self._settings(monkeypatch, {"analytics_opted_in": True})
        assert api_bridge.Api.record_donate_event(None, "nope") == {"success": True, "sent": False}
        assert sent == []


class TestQueueRunNumbers:
    def _write_db(self, folder: Path, header: str, rows: list[str]) -> None:
        k = folder / ".kestrel"
        k.mkdir(parents=True)
        (k / "kestrel_database.csv").write_text(
            "\n".join([header] + rows) + "\n", encoding="utf-8")

    def test_counts_distinct_scenes(self, tmp_path):
        self._write_db(tmp_path, "filename,scene_count,quality",
                       ["a.jpg,1,0.5", "b.jpg,1,0.4", "c.jpg,2,0.3", "d.jpg,7,0.1", "e.jpg,,0.2"])
        assert queue_manager._count_folder_scenes(str(tmp_path)) == 3

    def test_missing_csv_or_column_is_none(self, tmp_path):
        assert queue_manager._count_folder_scenes(str(tmp_path)) is None
        self._write_db(tmp_path, "filename,quality", ["a.jpg,0.5"])
        assert queue_manager._count_folder_scenes(str(tmp_path)) is None

    def test_to_dict_reports_this_run(self):
        item = queue_manager._QueueItem("/photos/outing", "outing")
        item.total = 500
        item.processed = 500
        item.initial_processed = 200
        item.scene_total = 61
        d = item.to_dict()
        assert d["files_this_run"] == 300
        assert d["scene_total"] == 61

    def test_to_dict_defaults(self):
        d = queue_manager._QueueItem("/p", "p").to_dict()
        assert d["files_this_run"] == 0
        assert d["scene_total"] is None
