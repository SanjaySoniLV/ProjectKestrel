"""App Store storefront lookup via StoreKit's SKStorefront (PyObjC).

Apple's anti-steering rule is **storefront-scoped**, not geographic. Guideline
3.1.1(a):

    These entitlements are not required for developers to include buttons,
    external links, or other calls to action in their United States storefront
    apps. In all other storefronts, except for the United States storefront,
    where this prohibition does not apply, apps and their metadata may not
    include buttons, external links, or other calls to action that direct
    customers to purchasing mechanisms other than in-app purchase.

Guideline 3.1.3's preamble carries the same carve-out. So the App Store build
may link out to Stripe (donations, plan management) **only** for customers whose
App Store account is on the United States storefront, and must not for anyone
else. That is what this module answers.

Why StoreKit and not IP geolocation: the rule keys on the *account's*
storefront, which is not where the machine happens to be. An American travelling
abroad is still a US-storefront customer and should see the donate CTA; someone
on a VPN is not and must not. IP geolocation gets both cases wrong, and reads as
evasion of a rule the app otherwise satisfies legitimately.

Design notes / gotchas:

  * ``SKPaymentQueue.defaultQueue().storefront()`` returns **nil** when StoreKit
    hasn't resolved a storefront yet (no App Store account signed in, or simply
    called too early in launch). nil is NOT "not US" forever — so we cache only
    *successful* lookups and re-query after a nil, letting a later call (e.g.
    when the user actually clicks Support) get the real answer.
  * ``countryCode`` is ISO 3166-1 **alpha-3** ("USA", "GBR", "DEU"), not alpha-2.
  * Everything here **fails closed**: any error, missing framework, non-mac
    platform, or unresolved storefront resolves to "not US", i.e. the
    conservative no-payment-link behaviour. Being wrong in that direction costs
    a US user a donate button; being wrong the other way costs an App Store
    rejection.

Only the App Store build ever calls this — ``api_bridge.get_support_url()``
short-circuits on ``dist_channel`` first, so the DMG and Windows builds never
import it. Import-safe everywhere regardless: nothing here imports PyObjC at
module load, and every public function returns a safe default rather than
raising.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ISO 3166-1 alpha-3 for the United States storefront, as reported by
# SKStorefront.countryCode.
US_STOREFRONT_CODE = "USA"

_load_lock = threading.Lock()
_storekit: Optional[dict] = None
_load_attempted = False

# Cache successful lookups only (see module docstring — nil is transient).
_country_lock = threading.Lock()
_country_cache: Optional[str] = None


def _load() -> Optional[dict]:
    """Import the StoreKit symbols we need, cached.

    Returns a dict of symbols, or ``None`` off-mac / without PyObjC / without
    the StoreKit wrapper installed. Never raises.
    """
    global _storekit, _load_attempted

    if sys.platform != "darwin":
        return None

    with _load_lock:
        if _load_attempted:
            return _storekit
        _load_attempted = True
        try:
            from StoreKit import SKPaymentQueue  # type: ignore

            _storekit = {"SKPaymentQueue": SKPaymentQueue}
        except Exception as e:  # pragma: no cover - platform/deps dependent
            logger.info("StoreKit unavailable; storefront lookup disabled (%s)", e)
            _storekit = None
        return _storekit


def is_available() -> bool:
    """True when StoreKit can be reached at all. Never raises."""
    return _load() is not None


def get_storefront_country() -> Optional[str]:
    """The App Store account's storefront as ISO alpha-3 (e.g. ``"USA"``).

    Returns ``None`` when the storefront can't be determined — off-mac, no
    StoreKit, no signed-in App Store account, or StoreKit hasn't resolved one
    yet. Callers must treat ``None`` as "not the US storefront".
    """
    global _country_cache

    with _country_lock:
        if _country_cache is not None:
            return _country_cache

    symbols = _load()
    if symbols is None:
        return None

    try:
        queue = symbols["SKPaymentQueue"].defaultQueue()
        storefront = queue.storefront() if queue is not None else None
        if storefront is None:
            # Transient: not signed in, or StoreKit still resolving. Don't
            # cache — a later call may succeed.
            logger.debug("SKStorefront not yet resolved")
            return None
        code = storefront.countryCode()
        if not code:
            return None
        country = str(code).strip().upper()
    except Exception as e:  # pragma: no cover - requires a real StoreKit
        logger.warning("SKStorefront lookup failed: %s", e)
        return None

    with _country_lock:
        _country_cache = country
    logger.info("App Store storefront: %s", country)
    return country


def is_us_storefront() -> bool:
    """True only when we positively confirmed the US storefront.

    Fails closed: any ambiguity resolves to ``False``, which suppresses external
    purchase links. Never raises.
    """
    return get_storefront_country() == US_STOREFRONT_CODE
