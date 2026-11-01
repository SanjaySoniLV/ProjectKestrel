"""Cloud Compute price display: channel gate, public catalog fetch, validation.

The Analyze folders dialog shows Cloud Compute prices to a signed-in user whose
queue is larger than their remaining images. The numbers are never hardcoded in
the app: they come from the service's public price catalog (no sign-in needed)
and are shown exactly as supplied. The client does no cap, credit or tax math.

Rules enforced here (the page only ever sees what this module returns):

* **Channel gate, in Python.** On the ``'appstore'`` channel the pricing call
  returns ``{'ok': False, 'reason': 'unavailable'}`` *before any network call*,
  so no price, link or free-image offer reaches the page on that build. The gate
  deliberately mirrors the support-link gate: if the channel cannot be read, the
  build is treated as ``'direct'`` (fail open). That is an accepted, recorded
  risk; ``test_cloud_pricing.py`` pins that the App Store spec bakes
  ``appstore`` into ``dist_channel.txt`` so the fallback is not reachable in a
  correctly built package.
* **Best effort.** Short timeout, never raises. A failed or malformed fetch
  yields ``catalog: None`` and the UI shows no prices (not an error).
* **Prices are before sales tax.** The response is ignored unless the service
  states ``pricesBeforeTax: true``.
"""

from __future__ import annotations

import json
import math
import urllib.request
from typing import Any

# Dual import for root-vs-package sys.path (see auth_client.py).
try:
    from net_tls import ssl_context as _ssl_context
except ImportError:  # pragma: no cover - package-style import path
    from analyzer.net_tls import ssl_context as _ssl_context

try:
    import dist_channel as _dist_channel
except ImportError:  # pragma: no cover - package-style import path
    try:
        from analyzer import dist_channel as _dist_channel
    except ImportError:
        _dist_channel = None

CATALOG_PATH = "/v1/public/catalog"
DEFAULT_AUTH_API_BASE = "https://auth.projectkestrel.org"
FETCH_TIMEOUT_SECONDS = 4
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ITEMS = 8

# Web pages the dialog links to. Opened only through the bridge's open_url.
PLANS_URL = "https://myaccount.projectkestrel.org/plan/"
LEARN_MORE_URL = "https://projectkestrel.org/cloud"

UNAVAILABLE = {"ok": False, "reason": "unavailable"}


def current_channel() -> str:
    """The build's distribution channel; ``'direct'`` if it cannot be read."""
    try:
        if _dist_channel is not None:
            return _dist_channel.get_channel()
    except Exception:
        pass
    return "direct"


def pricing_allowed() -> bool:
    """False only on the Mac App Store build."""
    return current_channel() != "appstore"


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return float(v)


def _label(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:60] if v else None


def _items(v: Any) -> list:
    return v[:MAX_ITEMS] if isinstance(v, list) else []


def parse_catalog(raw: Any) -> dict | None:
    """Validate the public catalog body. Returns display fields or ``None``.

    Unknown fields are dropped. Returns ``None`` unless the service says prices
    are before tax, so a future tax-inclusive change is never shown as the
    "plus sales tax" figure.
    """
    if not isinstance(raw, dict) or raw.get("pricesBeforeTax") is not True:
        return None
    if raw.get("currency") != "USD":
        return None

    plans: list[dict] = []
    for p in _items(raw.get("plans")):
        if not isinstance(p, dict):
            continue
        label, price = _label(p.get("label")), _num(p.get("priceUsd"))
        images, per1000 = _num(p.get("imagesPerMonth")), _num(p.get("per1000Usd"))
        if label is None or price is None or images is None or per1000 is None:
            continue
        plans.append({
            "tier": _label(p.get("tier")) or label,
            "label": label,
            "priceUsd": price,
            "imagesPerMonth": int(images),
            "per1000Usd": per1000,
        })

    packs: list[dict] = []
    for p in _items(raw.get("packs")):
        if not isinstance(p, dict):
            continue
        label, price = _label(p.get("label")), _num(p.get("priceUsd"))
        images, per1000 = _num(p.get("images")), _num(p.get("per1000Usd"))
        if label is None or price is None or images is None or per1000 is None:
            continue
        packs.append({
            "id": _label(p.get("id")) or label,
            "label": label,
            "images": int(images),
            "priceUsd": price,
            "per1000Usd": per1000,
        })

    bonus = _num(raw.get("signupBonusImages"))
    return {
        "plans": plans,
        "packs": packs,
        # None when the service predates the field or reports no bonus.
        "signupBonusImages": int(bonus) if bonus else None,
    }


def fetch_catalog(api_base: str = DEFAULT_AUTH_API_BASE,
                  timeout: float = FETCH_TIMEOUT_SECONDS) -> dict | None:
    """GET the public catalog. ``None`` on any failure or invalid body."""
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}{CATALOG_PATH}",
        headers={"Accept": "application/json", "User-Agent": "KestrelDesktop/Pricing/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return None
        return parse_catalog(json.loads(raw))
    except Exception:  # best effort: offline, timeout, bad JSON, TLS error, ...
        return None


def get_pricing(api_base: str = DEFAULT_AUTH_API_BASE) -> dict:
    """Bridge payload for the Analyze dialog.

    ``{'ok': False, 'reason': 'unavailable'}`` on the App Store build (no
    network call is made). Otherwise ``{'ok': True, 'catalog': dict | None,
    'plansUrl': str, 'learnMoreUrl': str}``.
    """
    if not pricing_allowed():
        return dict(UNAVAILABLE)
    catalog = fetch_catalog(api_base)
    return {
        "ok": True,
        "catalog": catalog,
        "plansUrl": PLANS_URL,
        "learnMoreUrl": LEARN_MORE_URL,
    }
