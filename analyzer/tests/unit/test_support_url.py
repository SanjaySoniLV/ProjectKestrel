"""Tests for the storefront-gated support URL (Apple Guideline 3.1.1(a)).

``/support-me`` carries the donate option; ``/support`` has no payment path at
all. Apple's anti-steering rule exempts the US storefront and no other, so the
App Store build must *positively confirm* a US storefront before it may link
out — every other outcome, including any error, has to fail closed to
``/support``. These tests pin that asymmetry, since getting it wrong in the
permissive direction costs the App Store listing.

``get_support_url`` never touches ``self``, so we call it unbound rather than
standing up a full ``Api`` instance.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import mac_storefront

pytestmark = pytest.mark.unit

FULL = "https://projectkestrel.org/support-me"
NO_PAY = "https://projectkestrel.org/support"


class _Chan:
    def __init__(self, ch):
        self._ch = ch

    def get_channel(self):
        return self._ch


class _Boom:
    def get_channel(self):
        raise RuntimeError("channel read failed")


class _Storefront:
    US_STOREFRONT_CODE = "USA"

    def __init__(self, country):
        self._country = country

    def get_storefront_country(self):
        return self._country


class _StorefrontBoom:
    US_STOREFRONT_CODE = "USA"

    def get_storefront_country(self):
        raise RuntimeError("StoreKit blew up")


def _call():
    return api_bridge.Api.get_support_url(None)


# ── channel short-circuit ────────────────────────────────────────────────────

def test_direct_channel_gets_full_page_without_touching_storekit(monkeypatch):
    # The DMG/Windows path must never reach StoreKit — that's what keeps those
    # builds free of the dependency entirely.
    monkeypatch.setattr(api_bridge, "_dist_channel", _Chan("direct"))
    monkeypatch.setattr(
        api_bridge, "_load_storefront",
        lambda: pytest.fail("non-appstore must not consult StoreKit"),
    )
    assert _call() == {"success": True, "url": FULL, "donate": True}


def test_missing_dist_channel_defaults_to_direct(monkeypatch):
    monkeypatch.setattr(api_bridge, "_dist_channel", None)
    monkeypatch.setattr(api_bridge, "_load_storefront", lambda: None)
    assert _call() == {"success": True, "url": FULL, "donate": True}


def test_dist_channel_error_defaults_to_direct(monkeypatch):
    monkeypatch.setattr(api_bridge, "_dist_channel", _Boom())
    monkeypatch.setattr(api_bridge, "_load_storefront", lambda: None)
    assert _call() == {"success": True, "url": FULL, "donate": True}


# ── App Store storefront gate ────────────────────────────────────────────────

def test_appstore_us_storefront_gets_donate(monkeypatch):
    monkeypatch.setattr(api_bridge, "_dist_channel", _Chan("appstore"))
    monkeypatch.setattr(api_bridge, "_load_storefront", lambda: _Storefront("USA"))
    assert _call() == {"success": True, "url": FULL, "donate": True}


def test_appstore_non_us_storefront_gets_no_payment_page(monkeypatch):
    monkeypatch.setattr(api_bridge, "_dist_channel", _Chan("appstore"))
    monkeypatch.setattr(api_bridge, "_load_storefront", lambda: _Storefront("GBR"))
    assert _call() == {"success": True, "url": NO_PAY, "donate": False}


def test_appstore_unresolved_storefront_fails_closed(monkeypatch):
    # StoreKit loaded but the storefront hasn't resolved (country=None) — the
    # real-world nil case. Must fail closed to the no-payment page.
    monkeypatch.setattr(api_bridge, "_dist_channel", _Chan("appstore"))
    monkeypatch.setattr(api_bridge, "_load_storefront", lambda: _Storefront(None))
    assert _call() == {"success": True, "url": NO_PAY, "donate": False}


def test_appstore_storekit_unavailable_fails_closed(monkeypatch):
    monkeypatch.setattr(api_bridge, "_dist_channel", _Chan("appstore"))
    monkeypatch.setattr(api_bridge, "_load_storefront", lambda: None)
    assert _call() == {"success": True, "url": NO_PAY, "donate": False}


def test_appstore_storekit_error_fails_closed(monkeypatch):
    monkeypatch.setattr(api_bridge, "_dist_channel", _Chan("appstore"))
    monkeypatch.setattr(api_bridge, "_load_storefront", lambda: _StorefrontBoom())
    assert _call() == {"success": True, "url": NO_PAY, "donate": False}


# ── mac_storefront: import-safety + caching ──────────────────────────────────

def _fake_storekit(monkeypatch, sequence):
    """Install a fake SKPaymentQueue whose storefront() walks ``sequence``
    (None -> nil storefront, str -> that countryCode). Returns the call log."""
    calls = []

    class _SF:
        def __init__(self, code):
            self._code = code

        def countryCode(self):
            return self._code

    class _Queue:
        def storefront(self):
            code = sequence[min(len(calls), len(sequence) - 1)]
            calls.append(code)
            return _SF(code) if code is not None else None

    class _PaymentQueue:
        @staticmethod
        def defaultQueue():
            return _Queue()

    monkeypatch.setattr(mac_storefront, "_country_cache", None, raising=False)
    monkeypatch.setattr(mac_storefront, "_load", lambda: {"SKPaymentQueue": _PaymentQueue})
    # Priming needs real PyObjC (objc/Foundation) — stub it so these read tests
    # stay pure, and pin retries to a single read so the per-call read-count
    # assertions below measure exactly one storefront() call per invocation.
    monkeypatch.setattr(mac_storefront, "prime", lambda: True)
    monkeypatch.setattr(mac_storefront, "_READ_RETRIES", 1, raising=False)
    monkeypatch.setattr(mac_storefront, "_READ_RETRY_SLEEP", 0, raising=False)
    return calls


@pytest.mark.skipif(sys.platform == "darwin", reason="off-mac behaviour")
def test_storefront_is_inert_off_mac():
    # No PyObjC, no StoreKit, no raise — and never "US".
    assert mac_storefront.is_available() is False
    assert mac_storefront.get_storefront_country() is None
    assert mac_storefront.is_us_storefront() is False


def test_storefront_nil_is_not_cached(monkeypatch):
    # nil is transient (App Store not signed in, or StoreKit still resolving at
    # launch). Caching it would poison the session and strand a US user on the
    # no-donate page for good.
    calls = _fake_storekit(monkeypatch, [None, "USA"])
    assert mac_storefront.get_storefront_country() is None
    assert mac_storefront.get_storefront_country() == "USA"
    assert len(calls) == 2


def test_storefront_success_is_cached(monkeypatch):
    calls = _fake_storekit(monkeypatch, ["USA"])
    assert mac_storefront.get_storefront_country() == "USA"
    assert mac_storefront.get_storefront_country() == "USA"
    assert len(calls) == 1  # second call served from cache


def test_storefront_country_is_normalised(monkeypatch):
    _fake_storekit(monkeypatch, ["  usa  "])
    assert mac_storefront.get_storefront_country() == "USA"
    assert mac_storefront.is_us_storefront() is True


def test_non_us_storefront_is_not_us(monkeypatch):
    _fake_storekit(monkeypatch, ["GBR"])
    assert mac_storefront.is_us_storefront() is False


def test_empty_country_code_fails_closed(monkeypatch):
    _fake_storekit(monkeypatch, [""])
    assert mac_storefront.get_storefront_country() is None
    assert mac_storefront.is_us_storefront() is False


def test_storefront_retry_resolves_within_window(monkeypatch):
    # A single call retries across the async resolution window: storefront() is
    # nil twice, then resolves. One get_storefront_country() call must ride it
    # out and return the country (this is what priming + retry buys us on the
    # first Support click, vs. the old fail-closed-on-first-nil behaviour).
    calls = _fake_storekit(monkeypatch, [None, None, "USA"])
    monkeypatch.setattr(mac_storefront, "_READ_RETRIES", 5, raising=False)
    monkeypatch.setattr(mac_storefront, "_READ_RETRY_SLEEP", 0, raising=False)
    assert mac_storefront.get_storefront_country() == "USA"
    assert len(calls) == 3  # two nils, then the resolved read — all in one call
