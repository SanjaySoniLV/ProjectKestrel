"""Tests for the Cloud Compute price display (Analyze folders dialog).

Pins:
* the Mac App Store gate lives in Python and makes no network call;
* the gate fails open (unreadable channel -> 'direct'), an accepted decision, so
  the App Store spec must positively bake ``appstore`` into ``dist_channel.txt``;
* the catalog is ignored unless it says prices are before tax, and only display
  fields survive validation;
* a failed fetch yields no catalog rather than an error.
"""

import io
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import cloud_pricing

pytestmark = pytest.mark.unit

ANALYZER_DIR = Path(__file__).parent.parent.parent

GOOD = {
    "currency": "USD",
    "pricesBeforeTax": True,
    "plans": [
        {"tier": "blue_jay", "label": "Blue Jay", "priceUsd": 15, "imagesPerMonth": 20000,
         "per1000Usd": 0.75, "priceId": "price_secret"},
        {"tier": "sandpiper", "label": "Sandpiper", "priceUsd": 35, "imagesPerMonth": 50000,
         "per1000Usd": 0.7},
    ],
    "packs": [
        {"id": "backlog_10k", "label": "10,000 images", "images": 10000, "priceUsd": 8,
         "per1000Usd": 0.8},
        {"id": "backlog_25k", "label": "25,000 images", "images": 25000, "priceUsd": 20,
         "per1000Usd": 0.8},
    ],
    "signupBonusImages": 2500,
    "asOf": "2026-11-01T00:00:00.000Z",
}


class _Chan:
    def __init__(self, ch):
        self._ch = ch

    def get_channel(self):
        return self._ch


class _Boom:
    def get_channel(self):
        raise RuntimeError("channel read failed")


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test if anything tries to open a URL."""
    calls = []

    def _fail(*a, **k):
        calls.append(a)
        raise AssertionError("network call attempted")

    monkeypatch.setattr(cloud_pricing.urllib.request, "urlopen", _fail)
    return calls


# ── channel gate ────────────────────────────────────────────────────────────

def test_appstore_is_unavailable_and_makes_no_network_call(monkeypatch, no_network):
    monkeypatch.setattr(cloud_pricing, "_dist_channel", _Chan("appstore"))
    assert cloud_pricing.get_pricing("https://example.test") == {
        "ok": False, "reason": "unavailable"}
    assert no_network == []


def test_appstore_gate_through_the_bridge_method(monkeypatch, no_network):
    monkeypatch.setattr(cloud_pricing, "_dist_channel", _Chan("appstore"))
    out = api_bridge.Api.cloud_compute_get_pricing(object.__new__(api_bridge.Api))
    assert out == {"ok": False, "reason": "unavailable"}
    assert no_network == []


@pytest.mark.parametrize("chan", [_Chan("direct"), _Chan("something-else"), _Boom(), None])
def test_every_non_appstore_channel_is_allowed_fail_open(monkeypatch, chan):
    monkeypatch.setattr(cloud_pricing, "_dist_channel", chan)
    assert cloud_pricing.pricing_allowed() is True


def test_direct_build_returns_catalog_and_links(monkeypatch):
    monkeypatch.setattr(cloud_pricing, "_dist_channel", _Chan("direct"))
    monkeypatch.setattr(cloud_pricing, "fetch_catalog", lambda base, **k: {"plans": [], "packs": []})
    out = cloud_pricing.get_pricing("https://example.test")
    assert out["ok"] is True
    assert out["plansUrl"].startswith("https://")
    assert out["learnMoreUrl"].startswith("https://")


def test_appstore_spec_bakes_appstore_into_dist_channel_txt():
    spec = (ANALYZER_DIR / "ProjectKestrel-macos-appstore.spec").read_text(encoding="utf-8")
    # The default channel for this spec is 'appstore'...
    assert re.search(r'os\.environ\.get\("KESTREL_DIST_CHANNEL",\s*"appstore"\)', spec)
    # ...an unrecognised value is coerced to 'appstore', not 'direct'...
    assert re.search(r'if _dist_channel not in \("direct", "appstore"\):\s*\n\s*_dist_channel = "appstore"', spec)
    # ...it is written to dist_channel.txt and that file ships in the bundle.
    assert 'open("dist_channel.txt", "w"' in spec
    assert "_dcf.write(_dist_channel)" in spec
    assert "('dist_channel.txt', '.')" in spec


def test_direct_specs_do_not_bake_appstore():
    for name in ("ProjectKestrel.spec", "ProjectKestrel-macos.spec"):
        spec = (ANALYZER_DIR / name).read_text(encoding="utf-8")
        assert re.search(r"KESTREL_DIST_CHANNEL',\s*'direct'\)", spec), name


# ── catalog validation ──────────────────────────────────────────────────────

def test_parse_catalog_keeps_display_fields_only():
    cat = cloud_pricing.parse_catalog(GOOD)
    assert [p["label"] for p in cat["plans"]] == ["Blue Jay", "Sandpiper"]
    assert [p["priceUsd"] for p in cat["packs"]] == [8, 20]
    assert cat["signupBonusImages"] == 2500
    assert "priceId" not in json.dumps(cat)
    assert "asOf" not in cat


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("pricesBeforeTax"),
    lambda d: d.update(pricesBeforeTax=False),
    lambda d: d.update(pricesBeforeTax="true"),
    lambda d: d.update(currency="EUR"),
])
def test_catalog_ignored_unless_prices_are_before_tax_usd(mutate):
    body = json.loads(json.dumps(GOOD))
    mutate(body)
    assert cloud_pricing.parse_catalog(body) is None


@pytest.mark.parametrize("raw", [None, [], "x", 5, {}, {"pricesBeforeTax": True}])
def test_non_catalog_bodies_are_rejected(raw):
    assert cloud_pricing.parse_catalog(raw) is None


def test_malformed_rows_are_dropped_not_fatal():
    body = json.loads(json.dumps(GOOD))
    body["plans"] = [
        "junk",
        {"label": "NoPrice"},
        {"label": "Neg", "priceUsd": -1, "imagesPerMonth": 1, "per1000Usd": 1},
        {"label": "Bool", "priceUsd": True, "imagesPerMonth": 1, "per1000Usd": 1},
        GOOD["plans"][0],
    ]
    cat = cloud_pricing.parse_catalog(body)
    assert [p["label"] for p in cat["plans"]] == ["Blue Jay"]


def test_missing_or_zero_signup_bonus_is_none():
    body = json.loads(json.dumps(GOOD))
    del body["signupBonusImages"]
    assert cloud_pricing.parse_catalog(body)["signupBonusImages"] is None
    body["signupBonusImages"] = 0
    assert cloud_pricing.parse_catalog(body)["signupBonusImages"] is None


def test_item_count_is_capped():
    body = json.loads(json.dumps(GOOD))
    body["packs"] = [GOOD["packs"][0]] * 50
    assert len(cloud_pricing.parse_catalog(body)["packs"]) == cloud_pricing.MAX_ITEMS


# ── fetch ───────────────────────────────────────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_parses_a_good_response(monkeypatch):
    seen = {}

    def fake(req, timeout=None, context=None):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return _Resp(json.dumps(GOOD).encode())

    monkeypatch.setattr(cloud_pricing.urllib.request, "urlopen", fake)
    cat = cloud_pricing.fetch_catalog("https://auth.example.test/")
    assert seen["url"] == "https://auth.example.test/v1/public/catalog"
    assert seen["timeout"] == cloud_pricing.FETCH_TIMEOUT_SECONDS <= 5
    assert cat["plans"][0]["per1000Usd"] == 0.75


@pytest.mark.parametrize("payload", [
    pytest.param(b"", id="empty"),
    pytest.param(b"not json", id="not-json"),
    pytest.param(b"[1,2]", id="array"),
    pytest.param(b'{"pricesBeforeTax": false}', id="tax-inclusive"),
    pytest.param(b"x" * (cloud_pricing.MAX_RESPONSE_BYTES + 10), id="oversized"),
])
def test_fetch_returns_none_for_bad_bodies(monkeypatch, payload):
    monkeypatch.setattr(cloud_pricing.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(payload))
    assert cloud_pricing.fetch_catalog("https://auth.example.test") is None


@pytest.mark.parametrize("exc", [TimeoutError("t"), OSError("down"), ConnectionError("c"),
                                 RuntimeError("tls")])
def test_fetch_returns_none_on_transport_errors(monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cloud_pricing.urllib.request, "urlopen", boom)
    assert cloud_pricing.fetch_catalog("https://auth.example.test") is None


def test_failed_fetch_on_direct_build_still_returns_links(monkeypatch):
    monkeypatch.setattr(cloud_pricing, "_dist_channel", _Chan("direct"))
    monkeypatch.setattr(cloud_pricing.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    out = cloud_pricing.get_pricing("https://auth.example.test")
    assert out["ok"] is True and out["catalog"] is None
    assert out["plansUrl"] and out["learnMoreUrl"]

# ── frontend wiring ─────────────────────────────────────────────────────────

def test_sign_in_entry_point_used_by_the_cloud_card_is_defined():
    """The Cloud card calls the global ``openPerchSignInWindow`` behind a
    ``typeof`` guard, so a missing definition fails silently (the sign-in links
    just do nothing). Pin that auth.js defines it."""
    js = ANALYZER_DIR / "js"
    used = (js / "cloud-compute.js").read_text(encoding="utf-8")
    assert "openPerchSignInWindow" in used
    auth = (js / "auth.js").read_text(encoding="utf-8")
    assert re.search(r"window\.openPerchSignInWindow\s*=\s*openSignInUI", auth)
