"""
Project Kestrel — Build Attestation Loader

Reads ``build_attestation.json`` if CI baked one into this build, and exposes
the HMAC headers needed for the worker's "official" auth tier. When the file
is absent (source builds, pre-attestation binaries, dev workflows) this module
returns an empty dict so the caller can fall back to the legacy X-Kestrel-Key.

DESIGN RULES (same as kestrel_telemetry):
  1. Never raise — telemetry must stay fire-and-forget.
  2. No I/O after first load; results are cached at module scope.
"""

import json
import os
import sys
from typing import Dict, List, Optional

_loaded: bool = False
_meta: Optional[str] = None
_sig: Optional[str] = None


def _candidate_paths() -> List[str]:
    """Locations where build_attestation.json may live, in priority order."""
    paths: List[str] = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        paths.append(os.path.join(meipass, 'build_attestation.json'))
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, 'build_attestation.json'))
    paths.append(os.path.join(here, '..', 'build_attestation.json'))
    return paths


def _load() -> None:
    global _loaded, _meta, _sig
    if _loaded:
        return
    _loaded = True
    for path in _candidate_paths():
        try:
            if not os.path.isfile(path):
                continue
            # utf-8-sig tolerates a leading BOM. The macOS workflow writes the
            # file via Python json.dump (no BOM), but if any writer accidentally
            # emits one — historically the Windows workflow's Set-Content -Encoding
            # utf8 did — utf-8-sig still parses cleanly so the build stays attested.
            with open(path, 'r', encoding='utf-8-sig') as f:
                data = json.load(f)
            meta = data.get('meta')
            sig = data.get('sig')
            if isinstance(meta, str) and isinstance(sig, str) and meta and sig:
                _meta = meta
                _sig = sig
                return
        except Exception:
            # Failsafe — telemetry must never raise from import-time work.
            # Keep trying remaining candidates instead of giving up on the
            # first malformed/unreadable file.
            continue


def auth_headers() -> Dict[str, str]:
    """Return the official-build HMAC headers, or ``{}`` if this is not an official build."""
    _load()
    if _meta and _sig:
        return {
            'X-Kestrel-Build-Meta': _meta,
            'X-Kestrel-Build-Sig': _sig,
        }
    return {}


def is_official() -> bool:
    """True iff a usable attestation bundle was loaded."""
    _load()
    return bool(_meta and _sig)
