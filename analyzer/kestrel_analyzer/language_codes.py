"""BCP-47 language-tag helpers shared by settings, the bird-name loaders and
the bird-name converter.

Kestrel accepts a deliberately small subset of BCP-47:

    language[-Script][-REGION]

* ``language`` -- 2 or 3 lowercase letters (ISO 639), e.g. ``pt``, ``zh``.
* ``Script``   -- 4 letters, title case (ISO 15924), e.g. ``Hans``, ``Hant``.
* ``REGION``   -- 2 uppercase letters or 3 digits, e.g. ``BR``, ``419``.

``canonicalize_language_tag`` repairs case (``pt-br`` -> ``pt-BR``,
``zh-hans`` -> ``zh-Hans``), maps ``_`` to ``-`` and returns ``None`` for
anything outside the subset, so a caller can never turn a setting into a
path. ``language_fallback_chain`` gives the lookup order for a tag; it always
ends in ``en`` (the source language).

``analyzer/js/i18n.js`` carries a JavaScript copy of the same rules
(``canonicalizeLang`` / ``fallbackChain``); the unit tests run both against
the same table so they cannot drift.
"""

from __future__ import annotations

import re

SOURCE_LANGUAGE = "en"

_TAG_RE = re.compile(
    r"^(?P<lang>[A-Za-z]{2,3})"
    r"(?:-(?P<script>[A-Za-z]{4}))?"
    r"(?:-(?P<region>[A-Za-z]{2}|[0-9]{3}))?$"
)

# Region-only Chinese tags imply a script; resolve them so ``zh-TW`` finds
# ``zh-Hant`` data and ``zh-CN`` finds ``zh-Hans``.
_IMPLIED_SCRIPT = {
    ("zh", "TW"): "Hant", ("zh", "HK"): "Hant", ("zh", "MO"): "Hant",
    ("zh", "CN"): "Hans", ("zh", "SG"): "Hans", ("zh", "MY"): "Hans",
}

# A bare language whose data is split by script falls back to this script
# before the bare language (``zh`` -> ``zh-Hans`` -> ``zh``).
_DEFAULT_SCRIPT = {"zh": "Hans"}

# Scripts written right-to-left, and languages whose default script is RTL.
_RTL_SCRIPTS = {"Arab", "Hebr", "Thaa", "Syrc", "Nkoo", "Adlm", "Rohg"}
_RTL_LANGUAGES = {"ar", "he", "fa", "ur", "ps", "yi", "dv", "ckb", "sd", "ug"}


def canonicalize_language_tag(tag) -> str | None:
    """Return the canonical form of ``tag`` or ``None`` when it is not valid."""
    if not isinstance(tag, str):
        return None
    s = tag.strip().replace("_", "-")
    if not s or len(s) > 16:
        return None
    m = _TAG_RE.match(s)
    if not m:
        return None
    lang = m.group("lang").lower()
    script = m.group("script")
    region = m.group("region")
    out = lang
    if script:
        out += "-" + script[0].upper() + script[1:].lower()
    if region:
        out += "-" + region.upper()
    return out


def is_valid_language_tag(tag) -> bool:
    """True when ``tag`` is already in canonical form."""
    return isinstance(tag, str) and canonicalize_language_tag(tag) == tag


def language_fallback_chain(tag) -> list[str]:
    """Lookup order for ``tag``, most specific first, always ending in ``en``.

    ``pt-BR`` -> ``[pt-BR, pt, en]``; ``zh-TW`` -> ``[zh-Hant-TW, zh-Hant,
    zh, en]``; ``zh`` -> ``[zh-Hans, zh, en]``; junk -> ``[en]``.
    """
    canon = canonicalize_language_tag(tag)
    if not canon:
        return [SOURCE_LANGUAGE]
    parts = canon.split("-")
    lang = parts[0]
    script = next((p for p in parts[1:] if len(p) == 4), None)
    region = next((p for p in parts[1:] if len(p) != 4), None)
    if script is None and region is not None:
        script = _IMPLIED_SCRIPT.get((lang, region))
    chain: list[str] = []

    def add(t: str) -> None:
        if t not in chain:
            chain.append(t)

    if script and region:
        add(f"{lang}-{script}-{region}")
    if region and not script:
        add(f"{lang}-{region}")
    if script:
        add(f"{lang}-{script}")
    elif lang in _DEFAULT_SCRIPT:
        add(f"{lang}-{_DEFAULT_SCRIPT[lang]}")
    add(lang)
    add(SOURCE_LANGUAGE)
    return chain


def text_direction(tag) -> str:
    """``'rtl'`` for right-to-left languages/scripts, else ``'ltr'``."""
    canon = canonicalize_language_tag(tag)
    if not canon:
        return "ltr"
    parts = canon.split("-")
    script = next((p for p in parts[1:] if len(p) == 4), None)
    if script:
        return "rtl" if script in _RTL_SCRIPTS else "ltr"
    return "rtl" if parts[0] in _RTL_LANGUAGES else "ltr"
