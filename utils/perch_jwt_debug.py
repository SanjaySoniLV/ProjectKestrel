"""
Print Perch (Clerk) JWT debug from the same store the desktop app uses (keyring or
``perch_auth.json``). Does not log the full token.

From the ProjectKestrel folder, with the same venv you use to run the app::

    python scripts/perch_jwt_debug.py

Optionally (extra console lines)::

    set PERCH_DEBUG_JWT=1
    python scripts/perch_jwt_debug.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

# Resolve analyzer/ on path (api_bridge, settings_utils live there).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ANALYZER = os.path.join(_ROOT, "analyzer")
if _ANALYZER not in sys.path:
    sys.path.insert(0, _ANALYZER)


def _decode_payload(b64url: str) -> dict:
    pad = "=" * (-len(b64url) % 4)
    raw = base64.urlsafe_b64decode((b64url + pad).encode("ascii"))
    return json.loads(raw.decode("utf-8"))


def main() -> int:
    # Import after path fix.
    from api_bridge import (  # type: ignore  # noqa: E402
        _keyring_load,
        _perch_jwt_exp_unverified,
        _perch_jwt_seconds_until_exp,
    )

    print("Perch JWT debug (Project Kestrel keyring / perch_auth.json)\n", flush=True)
    data = _keyring_load()
    if not data:
        print("No stored Perch token found (empty keyring / no fallback file).", flush=True)
        return 0

    token = data.get("token")
    if not token:
        print("Record exists but `token` is empty.", flush=True)
        return 0

    t = str(token).strip()
    print(f"JWT length: {len(t)}", flush=True)
    print(f"First 32 chars: {t[:32]!r}", flush=True)

    try:
        parts = t.split(".")
        payload = _decode_payload(parts[1]) if len(parts) >= 2 else {}
    except Exception as e:  # pragma: no cover
        print(f"Could not decode JWT payload: {e}", flush=True)
        return 1

    exp = payload.get("exp")
    iss = payload.get("iss")
    aud = payload.get("aud")
    sub = payload.get("sub")
    if exp is not None:
        try:
            exp_f = float(exp)
            exp_dt = datetime.fromtimestamp(exp_f, tz=timezone.utc)
            print(f"iss: {iss!r}", flush=True)
            print(f"aud: {aud!r}", flush=True)
            print(f"sub: {sub!r}", flush=True)
            print(f"exp (unix): {exp_f}  =>  {exp_dt.isoformat()} (UTC)", flush=True)
        except (TypeError, ValueError) as e:
            print(f"exp claim present but not numeric: {exp!r} ({e})", flush=True)
    else:
        print("No `exp` in payload.", flush=True)

    ttl = _perch_jwt_seconds_until_exp(t)
    if ttl is not None:
        print(f"Seconds until exp (unverified, this machine): {ttl:.0f}", flush=True)
    exp_u = _perch_jwt_exp_unverified(t)
    if exp_u is not None and os.environ.get("PERCH_DEBUG_JWT", "").strip() in (
        "1",
        "true",
        "yes",
    ):
        print(
            f"Stored `expiry` key (keyring file): {data.get('expiry')!r}",
            flush=True,
        )
        print(f"Now (unix): {time.time():.0f}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
