"""The scene viewer's sharpness thumbs-up sends one plain telemetry event.

Covers the three layers: the JS click handler (real ``qfRenderSceneControl``
run in a Node ``vm`` with a tiny DOM stub), ``Api.record_quality_thumbs_up``
(consent gate, scorer), and ``kestrel_telemetry.send_quality_thumbs_up``
(payload contents, never raises). The thumbs-down path and
``/v1/feedback/quality`` are untouched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge  # noqa: E402
import kestrel_telemetry  # noqa: E402

pytestmark = pytest.mark.unit

ANALYZER = Path(__file__).resolve().parents[2]
QF_JS = (ANALYZER / "js" / "quality-feedback.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


# -- Python: telemetry payload ------------------------------------------------

class TestSendQualityThumbsUp:
    @pytest.fixture
    def posted(self, monkeypatch):
        calls = []
        monkeypatch.setattr(kestrel_telemetry, "_post_json_async", lambda ep, p: calls.append((ep, p)))
        return calls

    def test_payload_is_aggregate_only(self, posted):
        assert kestrel_telemetry.send_quality_thumbs_up(7, 5, "siglip", machine_id="m-1", version="v") is True
        assert len(posted) == 1
        endpoint, payload = posted[0]
        assert endpoint == "/api/ui_event"
        assert payload["event"] == "quality_thumbs_up"
        assert payload["scene_images"] == 7 and payload["scene_crops"] == 5
        assert payload["scorer"] == "siglip"
        assert set(payload) == {"machine_id", "version", "os", "build", "event",
                                "scene_images", "scene_crops", "scorer"}

    def test_bad_values_are_clamped(self, posted):
        kestrel_telemetry.send_quality_thumbs_up("x", -4, "weird")
        p = posted[0][1]
        assert (p["scene_images"], p["scene_crops"], p["scorer"]) == (0, 0, "")

    def test_failed_send_does_not_raise(self, monkeypatch):
        def boom(*a, **k):
            raise urllib.error.URLError("offline")
        monkeypatch.setattr(kestrel_telemetry.urllib.request, "urlopen", boom)
        # The real _post_json runs here (in this thread): it must swallow the error.
        kestrel_telemetry._post_json("/api/ui_event", {"event": "quality_thumbs_up"})

    def test_queueing_failure_returns_false(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(kestrel_telemetry, "_post_json_async", boom)
        assert kestrel_telemetry.send_quality_thumbs_up(3, 2, "legacy") is False


# -- Python: bridge consent gate ----------------------------------------------

class TestRecordQualityThumbsUpBridge:
    @pytest.fixture
    def sent(self, monkeypatch):
        calls = []
        monkeypatch.setattr(api_bridge._telemetry, "send_quality_thumbs_up",
                            lambda **kw: calls.append(kw) or True)
        monkeypatch.setattr(api_bridge._telemetry, "get_machine_id", lambda s: "m-1")
        return calls

    def _settings(self, monkeypatch, settings):
        monkeypatch.setattr(api_bridge, "load_persisted_settings", lambda: dict(settings))

    def test_opted_in_sends_one_event(self, monkeypatch, sent):
        self._settings(monkeypatch, {"analytics_opted_in": True, "quality_model": "siglip"})
        res = api_bridge.Api.record_quality_thumbs_up(
            None, {"scene_images": 6, "scene_crops": 4, "has_siglip": True})
        assert res == {"success": True, "sent": True}
        assert sent == [{"scene_images": 6, "scene_crops": 4, "scorer": "siglip", "machine_id": "m-1"}]

    def test_scene_without_siglip_scores_reports_legacy(self, monkeypatch, sent):
        self._settings(monkeypatch, {"analytics_opted_in": True, "quality_model": "siglip"})
        api_bridge.Api.record_quality_thumbs_up(None, {"scene_images": 3, "scene_crops": 3, "has_siglip": False})
        assert sent[0]["scorer"] == "legacy"

    def test_legacy_setting_reports_legacy(self, monkeypatch, sent):
        self._settings(monkeypatch, {"analytics_opted_in": True, "quality_model": "legacy"})
        api_bridge.Api.record_quality_thumbs_up(None, {"scene_images": 3, "scene_crops": 3, "has_siglip": True})
        assert sent[0]["scorer"] == "legacy"

    @pytest.mark.parametrize("settings", [{"analytics_opted_in": False}, {}, {"analytics_opted_in": "yes"}])
    def test_opt_out_or_never_asked_sends_nothing(self, monkeypatch, sent, settings):
        self._settings(monkeypatch, settings)
        res = api_bridge.Api.record_quality_thumbs_up(None, {"scene_images": 6, "scene_crops": 4})
        assert res == {"success": True, "sent": False}
        assert sent == []

    def test_bad_payload_and_telemetry_error_do_not_raise(self, monkeypatch, sent):
        self._settings(monkeypatch, {"analytics_opted_in": True})
        assert api_bridge.Api.record_quality_thumbs_up(None, "nope")["sent"] is False

        def boom(**kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(api_bridge._telemetry, "send_quality_thumbs_up", boom)
        assert api_bridge.Api.record_quality_thumbs_up(None, {"scene_images": 2}) == {"success": True, "sent": False}


# -- JS: the click handler ------------------------------------------------------

def _fn(start, end):
    s = QF_JS.index(start)
    return QF_JS[s:QF_JS.index(end, s)]


SRC = "\n".join([
    "const QF_MODES = ['ask', 'always', 'never']; const _qfAnswered = new Set();",
    _fn("function qfMode()", "function _qfNum"),
    _fn("function _qfNum", "function _qfHasCrop"),
    _fn("function _qfHasCrop", "/** "),
    _fn("function qfSceneEligible", "function _qfSceneKey"),
    _fn("function _qfSceneKey", "function _qfButton"),
    _fn("function _qfButton", "/** Count a"),
    _fn("function qfRecordThumbsUp", "/** Paint the thumbs"),
    _fn("function qfRenderSceneControl", "// -- Sort dialog" if "// -- Sort dialog" in QF_JS else "async function qfOpenSortDialog"),
])

_HARNESS = r"""
const vm = require('vm');
const [src, payload] = process.argv.slice(1);
const { mode, apiThrows, apiRejects } = JSON.parse(payload);
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; this.textContent = ''; this.handlers = {};
    this.classList = { toggle: () => {} }; }
  replaceChildren(...c) { this.children = c; }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute() {} blur() {}
  addEventListener(t, f) { this.handlers[t] = f; }
}
const box = new El('div');
const calls = [];
const api = {
  get_quality_feedback_candidates: () => {},
  record_quality_thumbs_up: (d) => {
    calls.push(d);
    if (apiThrows) throw new Error('bridge down');
    if (apiRejects) return Promise.reject(new Error('rejected'));
  },
};
const ctx = vm.createContext({
  document: { getElementById: () => box, createElement: (t) => new El(t) },
  window: { pywebview: { api } },
  getSetting: () => mode,
  Promise, Set, Number, String, console,
});
const row = (n, extra) => Object.assign({ filename: n, crop_path: '.kestrel/crop/' + n + '.jpg', quality: 0.5, quality_siglip: 0.4 }, extra);
const scene = { id: 's1', images: [row('a.jpg'), row('b.jpg'), row('c.jpg', { crop_path: '' })] };
vm.runInContext(src + '\nthis.__render = qfRenderSceneControl;', ctx);
ctx.__render(scene);
const thumbsUp = box.children.find(c => c.textContent === '\u{1F44D}');
let clickError = null;
if (thumbsUp) { try { thumbsUp.handlers.click({ stopPropagation() {} }); } catch (e) { clickError = String(e); } }
setTimeout(() => process.stdout.write(JSON.stringify({
  hasThumbsUp: !!thumbsUp, calls, clickError,
  after: box.children.map(c => c.textContent),
})), 0);
"""


def _click(mode="ask", api_throws=False, api_rejects=False):
    out = subprocess.run(
        [NODE, "-e", _HARNESS, SRC, json.dumps({"mode": mode, "apiThrows": api_throws, "apiRejects": api_rejects})],
        capture_output=True, text=True, check=True, encoding="utf-8")
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
class TestThumbsUpClick:
    def test_click_sends_one_aggregate_event_and_says_thanks(self):
        res = _click()
        assert res["hasThumbsUp"] and res["clickError"] is None
        assert res["calls"] == [{"scene_images": 3, "scene_crops": 2, "has_siglip": True}]
        assert res["after"] == ["Thanks!"]

    def test_bridge_exception_still_says_thanks(self):
        res = _click(api_throws=True)
        assert res["clickError"] is None
        assert res["after"] == ["Thanks!"]

    def test_bridge_rejection_still_says_thanks(self):
        res = _click(api_rejects=True)
        assert res["clickError"] is None
        assert res["after"] == ["Thanks!"]

    def test_mode_never_shows_no_prompt_and_sends_nothing(self):
        res = _click(mode="never")
        assert res["hasThumbsUp"] is False
        assert res["calls"] == []
