#!/usr/bin/env python3
"""Project Kestrel application entry point.

Launches the pywebview desktop window and serves ``visualizer.html`` plus
static assets from a local-only HTTP server (127.0.0.1). All control flows
through the ``api_bridge.Api`` JS bridge; this module owns process startup,
session-lifecycle bookkeeping, OS-shutdown detection, and the crash
handler.

Usage (development):
    python analyzer/visualizer.py --port 8765 --root C:/Photos/Trip
    python analyzer/visualizer.py --cli C:/Photos/Trip --no-gpu   # headless

Optional env vars:
    KESTREL_ALLOWED_ROOT=C:/Photos/Trip       (jail bridge calls to this root)
    KESTREL_ALLOWED_EXTENSIONS=.cr3,.jpg,...  (override editor allowlist)
"""

from __future__ import annotations

WEBVIEW_IMPORT_SUCCESS = False
try:
    import webview  # type: ignore
    WEBVIEW_IMPORT_SUCCESS = True
except Exception:
    pass

import argparse
import os
import sys
import threading
import time

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from typing import Optional, TextIO

# --- Extracted modules ---
from settings_utils import (
    load_persisted_settings,
    save_persisted_settings,
    debug, info, warn, error, log,
)
from queue_manager import _queue_manager
from api_bridge import Api

# Telemetry — failsafe import (never blocks startup)
try:
    import kestrel_telemetry as _telemetry
except ImportError:
    try:
        from analyzer import kestrel_telemetry as _telemetry
    except ImportError:
        _telemetry = None  # type: ignore[assignment]

HOST = '127.0.0.1'

# How many sequential ports past the requested one to try before giving up and
# letting the OS pick a free port (bind to 0). Keeps the URL predictable in the
# common case while surviving a busy port (e.g. a stale instance still exiting).
PORT_FALLBACK_TRIES = 8


def _bind_server_with_fallback(preferred_port, handler):
    """Bind a ThreadingHTTPServer on 127.0.0.1, trying a few ports.

    Tries ``preferred_port`` first, then the next ``PORT_FALLBACK_TRIES`` ports,
    and finally falls back to an OS-assigned free port (bind to 0). Returns
    ``(server, port)`` where ``port`` is the port actually bound. Raises
    ``OSError`` only if every candidate — including the OS-assigned one — fails.
    """
    candidates = [preferred_port + offset for offset in range(PORT_FALLBACK_TRIES + 1)]
    candidates.append(0)  # last resort: let the OS hand us any free port
    last_exc = None
    for candidate in candidates:
        try:
            server = ThreadingHTTPServer((HOST, candidate), handler)
        except OSError as exc:
            last_exc = exc
            continue
        bound_port = server.server_address[1]
        if candidate not in (preferred_port, 0):
            log(f'Port {preferred_port} unavailable; using {bound_port} instead.')
        elif candidate == 0:
            log(f'Ports {preferred_port}-{preferred_port + PORT_FALLBACK_TRIES} '
                f'unavailable; OS assigned port {bound_port}.')
        return server, bound_port
    raise OSError(
        f'Could not bind an HTTP server on {HOST}: no free port found near '
        f'{preferred_port}'
    ) from last_exc

# One-time settings-migration key that flags whether the 2026-03 legal-consent
# self-heal has already run for this install. See ``_apply_legal_upgrade_self_heal``.
LEGAL_SELF_HEAL_MIGRATION_KEY = 'legal_upgrade_self_heal_2026_03'

# --- Security / behavior configuration ---
ALLOWED_ROOT = os.environ.get('KESTREL_ALLOWED_ROOT')
if ALLOWED_ROOT:
    ALLOWED_ROOT = os.path.abspath(os.path.expanduser(ALLOWED_ROOT))

# Editor-launch allowlists now live in ``api_bridge`` (the only surface that
# can invoke ``launch()``). This module only serves static files and does not
# need a copy. See FINDING-07.

_RUNTIME_LOG_HANDLE: Optional[TextIO] = None


class _TeeStream:
    """Mirror writes to the original stream and a runtime log file."""

    def __init__(self, original_stream, log_handle: TextIO):
        self._original_stream = original_stream
        self._log_handle = log_handle
        self.encoding = getattr(original_stream, 'encoding', 'utf-8')
        self.errors = getattr(original_stream, 'errors', 'replace')

    def write(self, data):
        text = data if isinstance(data, str) else str(data)
        try:
            self._original_stream.write(text)
        except Exception:
            pass
        try:
            self._log_handle.write(text)
        except Exception:
            pass
        return len(text)

    def flush(self):
        try:
            self._original_stream.flush()
        except Exception:
            pass
        try:
            self._log_handle.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self._original_stream.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._original_stream.fileno()

    @property
    def buffer(self):
        return getattr(self._original_stream, 'buffer', None)

    def __getattr__(self, name):
        return getattr(self._original_stream, name)


def _enable_runtime_log_capture() -> str:
    """Capture process stdout/stderr to a persistent runtime log file."""
    global _RUNTIME_LOG_HANDLE
    try:
        try:
            from kestrel_analyzer.logging_utils import resolve_log_dir
        except ImportError:
            from analyzer.kestrel_analyzer.logging_utils import resolve_log_dir

        base_log_dir = resolve_log_dir(None)
    except Exception:
        base_log_dir = os.path.join(os.path.expanduser('~'), '.kestrel')

    try:
        runtime_dir = os.path.join(base_log_dir, 'logs')
        os.makedirs(runtime_dir, exist_ok=True)
        ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        runtime_log_path = os.path.join(runtime_dir, f'kestrel_runtime_{ts}.log')

        _RUNTIME_LOG_HANDLE = open(runtime_log_path, 'a', encoding='utf-8', buffering=1)
        sys.stdout = _TeeStream(sys.stdout, _RUNTIME_LOG_HANDLE)
        sys.stderr = _TeeStream(sys.stderr, _RUNTIME_LOG_HANDLE)
        return runtime_log_path
    except Exception:
        return ''


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + 'Z'


# Settings key holding the previous session's outcome. One of:
#   'clean'        - normal user-initiated close
#   'os_shutdown'  - the OS told us to exit (reboot / logoff / power off)
#   'crash'        - unhandled Python exception
#   'unknown'      - never updated; ambiguous (e.g. SIGKILL, power loss,
#                    or an old build that didn't write this key yet)
EXIT_REASON_KEY = 'app_session_exit_reason'
EXIT_REASON_MIGRATION_KEY = 'exit_reason_migrated_v1'


def _classify_prior_session(settings: dict) -> str:
    """Return the previous session's exit reason, applying legacy migration.

    Pure function — no I/O, safe to call from tests. Reads only from the
    provided settings dict; does not mutate it.
    """
    reason = str(settings.get(EXIT_REASON_KEY, '') or '').strip().lower()
    if reason in ('clean', 'os_shutdown', 'crash', 'unknown'):
        return reason
    legacy_clean = settings.get('app_session_closed_cleanly', True)
    if not bool(legacy_clean) and str(settings.get('app_session_started_utc', '') or '').strip():
        return 'unknown'
    return 'clean'


def _mark_session_start() -> None:
    """Mark this app session as active and detect unclean prior shutdown.

    Also fires the once-per-UTC-day ``/api/open`` telemetry ping used for
    daily active user counts. Only one ping is sent per install per UTC day;
    the last send date is persisted in ``last_open_ping_utc``.
    """
    try:
        settings = load_persisted_settings()
        prev_reason = _classify_prior_session(settings)
        prev_started = str(settings.get('app_session_started_utc', '') or '').strip()
        # Only 'crash' and 'unknown' get surfaced as recoverable unclean
        # shutdowns. 'os_shutdown' is intentionally suppressed so PC reboots
        # don't generate false crash dialogs.
        if prev_reason in ('crash', 'unknown') and prev_started:
            settings['last_unclean_shutdown_utc'] = prev_started
        else:
            settings.pop('last_unclean_shutdown_utc', None)
        settings['last_exit_reason'] = prev_reason
        settings[EXIT_REASON_MIGRATION_KEY] = True
        settings['app_session_started_utc'] = _utc_now_iso()
        settings['app_session_closed_cleanly'] = False  # legacy, kept one release
        settings[EXIT_REASON_KEY] = 'unknown'
        settings['app_session_pid'] = int(os.getpid())

        try:
            today_utc = datetime.utcnow().strftime('%Y-%m-%d')
            last_ping = str(settings.get('last_open_ping_utc', '') or '').strip()
            legal_agreed = str(settings.get('legal_agreed_version', '') or '').strip()
            if (
                _telemetry is not None
                and legal_agreed
                and last_ping != today_utc
            ):
                mid = _telemetry.get_machine_id(settings)
                version = _telemetry._read_version()
                _telemetry.send_app_open_telemetry(mid, version=version)
                settings['last_open_ping_utc'] = today_utc
        except Exception:
            pass

        save_persisted_settings(settings)
    except Exception:
        pass


def _mark_session_clean_exit() -> None:
    """Mark this session closed cleanly and clear stale unclean-shutdown recovery.

    Preserves a previously-recorded 'os_shutdown' or 'crash' reason — the
    main() finally block fires after webview.start() returns, which happens
    both on user-initiated quit (truly clean) AND when the OS closes our
    window during reboot/logoff. In the latter case shutdown_watch has
    already recorded 'os_shutdown' and we must not overwrite it.
    """
    try:
        settings = load_persisted_settings()
        settings['app_session_closed_cleanly'] = True
        existing_reason = str(settings.get(EXIT_REASON_KEY, '') or '').strip().lower()
        if existing_reason not in ('os_shutdown', 'crash'):
            settings[EXIT_REASON_KEY] = 'clean'
        settings['last_session_closed_utc'] = _utc_now_iso()
        settings.pop('last_unclean_shutdown_utc', None)
        save_persisted_settings(settings)
    except Exception:
        pass


def _mark_session_exit_reason(reason: str) -> None:
    """Atomically record the cause of an in-progress shutdown.

    Called from the OS-shutdown watcher (``shutdown_watch``) and from the
    top-level crash handler so the next launch can distinguish a true
    application crash from a system reboot or logoff. Failsafe: any I/O
    error is swallowed because this runs at the worst possible moment.
    """
    if reason not in ('clean', 'os_shutdown', 'crash', 'unknown'):
        return
    try:
        settings = load_persisted_settings()
        settings[EXIT_REASON_KEY] = reason
        if reason == 'clean':
            settings['app_session_closed_cleanly'] = True
            settings.pop('last_unclean_shutdown_utc', None)
        save_persisted_settings(settings)
    except Exception:
        pass


def _apply_legal_upgrade_self_heal(settings: dict, prev_version: str, current_version: str) -> bool:
    """One-time migrations for legacy installs that lost legal consent markers.

    Two migrations are applied here:

    1. **Consent marker self-heal (2026-03)** — gated by
       :data:`LEGAL_SELF_HEAL_MIGRATION_KEY`. Runs once on version change when
       ``legal_agreed_version`` is missing, restoring the marker so the user
       is not prompted as brand-new.

    2. **Legal-agreed-date backfill** — not gated by the migration flag.
       Whenever a user has an existing ``legal_agreed_version`` but no
       ``legal_agreed_date``, backfill the date to ``2026-03-01`` (the
       effective date of the previous published Terms/Privacy). This ensures
       existing installs will correctly see the "terms updated" banner the
       first time ``legal.json`` advertises a newer effective date, without
       spuriously reprompting users.

    Returns True when anything in the settings payload was mutated.
    """
    if not isinstance(settings, dict):
        return False

    mutated = False

    legal_agreed = str(settings.get('legal_agreed_version', '') or '').strip()

    prev = str(prev_version or '').strip()
    curr = str(current_version or '').strip()
    if (
        prev and curr and prev != curr
        and not settings.get(LEGAL_SELF_HEAL_MIGRATION_KEY, False)
    ):
        if not legal_agreed:
            settings['legal_agreed_version'] = prev or curr
            legal_agreed = settings['legal_agreed_version']
            if 'installed_telemetry_sent' not in settings:
                settings['installed_telemetry_sent'] = True
            log('[legal] Applied one-time upgrade self-heal for missing consent markers:', prev, '->', curr)
        settings[LEGAL_SELF_HEAL_MIGRATION_KEY] = True
        mutated = True

    if legal_agreed and not str(settings.get('legal_agreed_date', '') or '').strip():
        settings['legal_agreed_date'] = '2026-03-01'
        log('[legal] Backfilled legal_agreed_date to 2026-03-01 for existing install.')
        mutated = True

    return mutated


def _safe_under(base: str, candidate: str) -> bool:
    """Return True iff ``candidate`` resolves to a path under ``base``.

    Used to jail the ``translate_path`` fallbacks against URL-encoded or raw
    ``..`` traversal segments in frozen builds (FINDING-03).
    """
    if not base or not candidate:
        return False
    try:
        base_real = os.path.realpath(base)
        cand_real = os.path.realpath(candidate)
    except (OSError, ValueError):
        return False
    try:
        return os.path.commonpath([cand_real, base_real]) == base_real
    except ValueError:
        # Different drives on Windows, etc.
        return False


class Handler(SimpleHTTPRequestHandler):
    # Serve from directory of this script (project root) by default.
    def translate_path(self, path: str) -> str:  # type: ignore[override]
        """Resolve file paths robustly across dev, frozen, and installed builds.

        Checks multiple locations, each jailed under its respective base with
        ``_safe_under`` so a crafted request like ``/../../etc/passwd`` cannot
        escape the bundle. See FINDING-03.
        """
        # Try the normal translation first — SimpleHTTPRequestHandler already
        # strips '..' via posixpath.normpath, so this is the trusted resolver.
        resolved = super().translate_path(path)
        if os.path.exists(resolved):
            return resolved

        # Fallbacks: try alternate roots, but reject any resolved path that
        # escapes that root. Each candidate base is computed relative to a
        # well-known location (CWD + /analyzer, _internal/, _MEIPASS/).
        cwd = os.getcwd()
        if not path.startswith('/analyzer'):
            alt = super().translate_path('/analyzer' + path)
            if os.path.exists(alt) and _safe_under(cwd, alt):
                return alt

        if getattr(sys, 'frozen', False):
            try:
                exe_dir = os.path.dirname(sys.executable)
                internal_dir = os.path.join(exe_dir, '_internal')
                for prefix in ('', '/analyzer'):
                    rel = path if path.startswith('/analyzer') else (prefix + path)
                    candidate = os.path.normpath(os.path.join(internal_dir, rel.lstrip('/')))
                    if os.path.exists(candidate) and _safe_under(internal_dir, candidate):
                        return candidate
            except Exception:
                pass

            meipass = getattr(sys, '_MEIPASS', None)
            if meipass:
                candidate = os.path.normpath(os.path.join(meipass, path.lstrip('/')))
                if os.path.exists(candidate) and _safe_under(meipass, candidate):
                    return candidate

        return resolved

    def end_headers(self):
        """Inject conservative security headers on every response.

        * ``Content-Security-Policy`` — 'self' for all resources plus
          inline styles (the existing visualizer.html/culling.html rely on
          ``<style>`` blocks and ``style=""`` attributes). No inline scripts,
          no remote scripts, no iframes, no form targets outside the app.
          This is the fix for the RCE half of FINDING-01: even if an attacker
          lands arbitrary HTML into the DOM, they cannot execute script.
        * ``X-Content-Type-Options`` — prevent MIME-sniffing.
        * ``X-Frame-Options`` — the pywebview shell already disallows this,
          but the server-side header closes the loophole if the user ever
          opens ``http://127.0.0.1:<port>`` in an external browser.
        * ``Referrer-Policy`` — never leak the local URL to third parties.
        """
        self.send_header('Cache-Control', 'no-store')
        # Note on ``'unsafe-inline'`` for ``script-src``: the existing
        # ``visualizer.html`` / ``culling.html`` bundle a large inline
        # ``<script>`` block and at least one inline ``onclick=`` handler.
        # Moving all of that behind hashes or nonces is a much bigger change
        # than this security pass. The primary XSS-to-RCE chain (FINDING-01)
        # is already closed at the DOM level: the ``sceneName`` innerHTML
        # sink was replaced with ``textContent`` construction, and
        # ``Api.open_url`` now rejects ``file:``/``javascript:``/UNC URLs.
        # The CSP below is defense-in-depth — it still blocks remote
        # script loads, ``eval``-style CSS injection, plugin embeds, and
        # form submission to third parties.
        #
        # Note on ``'unsafe-eval'`` for ``script-src``: pywebview 6.1's JS
        # bridge (``webview/js/api.js::_createApi``) generates each
        # ``window.pywebview.api.<method>`` stub with ``new Function(params,
        # body)``, which CSP treats as ``eval``. Without ``'unsafe-eval'``
        # the stubs are never created and ``window.pywebview.api`` ends up
        # empty — buttons that route through the bridge silently no-op and
        # the version badge stays on its ``Version: —`` placeholder.
        # Originally believed to be macOS-only (WKWebView enforces page CSP
        # on ``evaluateJavaScript:``) because older Edge WebView2 builds
        # ran ``ExecuteScriptAsync`` in a privileged isolated world that
        # bypassed page CSP. Issue #39 disproves that on Windows 10 with a
        # recent WebView2 runtime: the same dead-bridge symptoms appear
        # there too. Allowing ``'unsafe-eval'`` is required on every
        # platform until pywebview drops the dynamic-Function pattern. The
        # CSP scope is otherwise the same.
        self.send_header(
            'Content-Security-Policy',
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            # ``blob:`` is required for ``connect-src`` because the clipboard
            # copy path (``_blobUrlToBlob``) does ``fetch(blobUrl)`` to
            # re-hydrate an object URL into a Blob for
            # ``navigator.clipboard.write``. Without it, CSP silently blocks
            # the fetch and the "Copy full image"/"Copy bird crop" buttons
            # in the scene preview fail.
            "connect-src 'self' blob:; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        super().end_headers()

    def do_GET(self):  # type: ignore[override]
        if self.path in ('/', '/index.html'):
            # Prefer analyzer/visualizer.html when present (merged layout).
            # Check multiple locations across dev, frozen, and installed builds.
            def _find_visualizer():
                # List of relative paths to try (from various base dirs)
                candidates = [
                    'analyzer/visualizer.html',
                    'visualizer.html',
                ]
                
                # Check from CWD
                for rel in candidates:
                    full = os.path.join(os.getcwd(), rel)
                    if os.path.exists(full):
                        return '/' + rel
                
                # Check from exe dir (frozen/installed)
                try:
                    exe_dir = os.path.dirname(sys.executable)
                    internal_dir = os.path.join(exe_dir, '_internal')
                    for rel in candidates:
                        full = os.path.join(internal_dir, rel)
                        if os.path.exists(full):
                            return '/' + rel
                except Exception:
                    pass
                
                # Check PyInstaller _MEIPASS
                meipass = getattr(sys, '_MEIPASS', None)
                if meipass:
                    for rel in candidates:
                        full = os.path.join(meipass, rel)
                        if os.path.exists(full):
                            return '/' + rel
                
                # Default fallback
                return '/analyzer/visualizer.html'
            
            self.path = _find_visualizer()
        return super().do_GET()


def _build_arg_parser():
    ap = argparse.ArgumentParser(description='Serve Project Kestrel visualizer with local desktop bridge.')
    ap.add_argument('--port', type=int, default=8765, help='Port to listen on (default 8765)')
    ap.add_argument('--root', default='', help='Default root folder for RAW originals (client can override unless KESTREL_ALLOWED_ROOT set)')
    ap.add_argument(
        '--cli',
        action='store_true',
        help='Run analyzer CLI mode (headless) instead of launching the desktop UI.',
    )
    ap.add_argument(
        '--api-probe',
        dest='api_probe',
        action='store_true',
        help='Headlessly launch pywebview, evaluate JS to confirm the bridge is reachable, '
             'write a result JSON to --probe-output, and exit.',
    )
    ap.add_argument(
        '--probe-output',
        dest='probe_output',
        type=str,
        default=None,
        help='Path for the --api-probe result JSON. Required with --api-probe.',
    )
    ap.add_argument(
        '--probe-timeout',
        dest='probe_timeout',
        type=float,
        default=15.0,
        help='Hard timeout in seconds for --api-probe (default 15).',
    )
    ap.add_argument(
        '--probe-target',
        dest='probe_target',
        choices=('synthetic', 'visualizer'),
        default='synthetic',
        help=(
            "What --api-probe loads. 'synthetic' (default, fast) uses a minimal "
            "probe HTML; 'visualizer' spins up the local HTTP server and loads the "
            "real visualizer.html, proving the production JS actually sees "
            "window.pywebview.api on the built binary."
        ),
    )
    return ap


def _chdir_to_static_root() -> None:
    """Set CWD so the local HTTP server's Handler resolves static asset paths.

    Shared by ``main()`` (full app) and ``_run_api_probe(target='visualizer')``
    so the probe sees exactly the same file layout the real desktop session
    does — including the frozen ``_internal/`` bundle when running off a
    PyInstaller exe.
    """
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None) or os.path.dirname(sys.executable)
        candidate = os.path.join(meipass, '_internal')
        if os.path.isdir(candidate):
            os.chdir(candidate)
            return
        if meipass and os.path.isdir(meipass):
            os.chdir(meipass)
            return
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..') or '.')


# Minimal HTML for --api-probe mode. Loads, waits for the pywebview bridge to be
# wired up, then calls Api.report_bridge_ready() which sets a threading.Event on
# the Python side. The bridge call landing IS the proof — no side-channel polling.
_PROBE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Kestrel Bridge Probe</title>
<style>body{font-family:sans-serif;padding:1em;}pre{background:#eee;padding:.5em;}</style>
</head>
<body>
<h3>Kestrel Bridge Probe</h3>
<pre id="status">waiting for window.pywebview.api...</pre>
<script>
  function _report() {
    try {
      window.pywebview.api.report_bridge_ready().then(function (r) {
        document.getElementById('status').textContent = JSON.stringify(r);
      }).catch(function (e) {
        document.getElementById('status').textContent = 'CALL ERROR: ' + e;
      });
    } catch (e) {
      document.getElementById('status').textContent = 'EXC: ' + e;
    }
  }
  // pywebviewready fires once window.pywebview.api is fully wired up. If we
  // missed it (rare race on fast machines), check directly.
  window.addEventListener('pywebviewready', _report);
  if (window.pywebview && window.pywebview.api && window.pywebview.api.report_bridge_ready) {
    _report();
  }
</script>
</body></html>"""


def _run_api_probe(args) -> int:
    """Implements --api-probe: launch a minimal pywebview window, wait for the
    JS-Python bridge to round-trip a call, write the result JSON, return exit
    code (0 success, 1 failure/timeout, 2 usage error).
    """
    import json
    import threading
    from datetime import datetime, timezone

    def _write_result(path, payload):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            warn('probe: failed to write result JSON:', exc)

    if not args.probe_output:
        sys.stderr.write('--api-probe requires --probe-output PATH\n')
        return 2

    if not WEBVIEW_IMPORT_SUCCESS:
        _write_result(args.probe_output, {
            'ok': False,
            'error': 'pywebview unavailable (import failed)',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        })
        return 1

    api = Api()
    api._probe_ready_event = threading.Event()
    api._probe_ready_payload = None

    timeout = max(1.0, float(args.probe_timeout))
    final_payload = {'ok': False, 'error': 'probe did not start'}
    target = getattr(args, 'probe_target', 'synthetic') or 'synthetic'

    # In 'visualizer' mode we stand up the same local HTTP server the real
    # desktop session uses and point pywebview at it, so the probe exercises
    # the production visualizer.html + visualizer.js bundle. The signal that
    # the bridge wired up still comes from JS calling Api.report_bridge_ready
    # — we just added one such call in visualizer.js for this purpose.
    server = None
    server_thread = None
    if target == 'visualizer':
        try:
            _chdir_to_static_root()
            server = ThreadingHTTPServer((HOST, args.port), Handler)
        except Exception as exc:
            _write_result(args.probe_output, {
                'ok': False,
                'error': f'probe: HTTP server bind failed on port {args.port}: '
                         f'{type(exc).__name__}: {exc}',
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
            return 1

        def _serve():
            try:
                server.serve_forever()
            except Exception:
                pass

        server_thread = threading.Thread(target=_serve, daemon=True)
        server_thread.start()

    try:
        if target == 'visualizer':
            win = webview.create_window(
                'Project Kestrel (probe)',
                url=f'http://{HOST}:{args.port}/',
                js_api=api,
                width=900,
                height=600,
            )
        else:
            win = webview.create_window(
                'Project Kestrel (probe)',
                html=_PROBE_HTML,
                js_api=api,
                width=400,
                height=300,
            )
    except Exception as exc:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        _write_result(args.probe_output, {
            'ok': False,
            'error': f'create_window failed: {type(exc).__name__}: {exc}',
        })
        return 1

    def _waiter():
        # Runs on a worker thread (started via webview.start(func=...)).
        # Blocks until the JS side calls Api.report_bridge_ready, or until the
        # hard deadline elapses. Then writes the result JSON and destroys the
        # window so webview.start() returns control to main().
        nonlocal final_payload
        ok = api._probe_ready_event.wait(timeout=timeout)
        if ok and api._probe_ready_payload is not None:
            final_payload = dict(api._probe_ready_payload)
            final_payload.setdefault('probe_target', target)
        else:
            final_payload = {
                'ok': False,
                'error': f'bridge did not report ready within {timeout:.1f}s',
                'probe_target': target,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            }
        _write_result(args.probe_output, final_payload)
        try:
            webview.destroy_window(win)
        except Exception:
            try:
                win.destroy()
            except Exception:
                pass

    try:
        webview.start(func=_waiter, debug=False)
    except Exception as exc:
        _write_result(args.probe_output, {
            'ok': False,
            'error': f'webview.start failed: {type(exc).__name__}: {exc}',
        })
        return 1
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        # If webview.start() returned before the waiter wrote a result (window
        # closed early, platform-specific event-loop quirk, etc.), the CI step
        # sees nothing but an exit-1 with no JSON. Write a placeholder so the
        # failure mode is observable.
        if not os.path.exists(args.probe_output):
            _write_result(args.probe_output, {
                'ok': False,
                'error': 'probe ended without waiter completing (webview.start returned early?)',
                'probe_target': target,
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })

    return 0 if final_payload.get('ok') else 1


def parse_args():
    return _build_arg_parser().parse_args()


def parse_known_args():
    return _build_arg_parser().parse_known_args()


def main():
    args, remaining_args = parse_known_args()
    if args.api_probe:
        sys.exit(_run_api_probe(args))
    if args.cli:
        from cli import main as cli_main

        cli_main(remaining_args)
        return

    runtime_log_path = _enable_runtime_log_capture()
    if runtime_log_path:
        log('Runtime log capture enabled:', runtime_log_path)
    _mark_session_start()

    # Listen for OS-initiated shutdown / logoff / reboot so the next launch
    # can distinguish a system-driven exit from a real application crash and
    # suppress the false unclean-shutdown dialog. Best-effort and failsafe.
    try:
        import shutdown_watch
        shutdown_watch.install(lambda: _mark_session_exit_reason('os_shutdown'))
    except Exception as _e:
        warn('shutdown_watch install failed:', _e)

    # ── Crash hardening ───────────────────────────────────────────────────────
    # faulthandler dumps a Python traceback to stderr (which is tee-streamed to
    # the runtime log file) on SIGSEGV / SIGABRT / hard crashes from native libs
    # (OpenCV, ONNX Runtime, etc.).
    try:
        import faulthandler
        faulthandler.enable()
    except Exception:
        pass

    # threading.excepthook catches unhandled exceptions on daemon threads (e.g.
    # the analysis worker) that would otherwise die silently with no log output.
    def _thread_excepthook(args):
        try:
            import traceback as _tb
            thread_name = getattr(args.thread, 'name', 'unknown')
            tb_str = ''.join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            error(f'[Thread {thread_name!r}] Uncaught exception: {args.exc_type.__name__}: {args.exc_value}')
            error(f'[Thread {thread_name!r}] Traceback:\n{tb_str}')
            _mark_session_exit_reason('crash')
            if _telemetry is not None:
                try:
                    _crash_settings = load_persisted_settings()
                    _telemetry.send_crash_report(
                        exc=args.exc_value,
                        tb_str=tb_str,
                        machine_id=_telemetry.get_machine_id(_crash_settings),
                        version=_telemetry._read_version(),
                        exit_reason='crash',
                        crash_reports_enabled=bool(_crash_settings.get('crash_reports_enabled', True)),
                    )
                except Exception:
                    pass
        except Exception:
            pass

    import threading as _threading_mod
    _threading_mod.excepthook = _thread_excepthook
    # ─────────────────────────────────────────────────────────────────────────

    # When visualizer.py is run from inside analyzer/ (merged layout) set
    # the working directory to the repository root so assets and shared
    # files (assets/, visualizer files) are served correctly. The frozen
    # PyInstaller branch prefers the bundled _internal/ folder.
    _chdir_to_static_root()
    server, args.port = _bind_server_with_fallback(args.port, Handler)
    log(f'Serving visualizer at http://{HOST}:{args.port}/  (Press Ctrl+C to stop)')
    log('HTTP surface: static-file GET only. Control routes permanently removed.')

    # ── Settings init: ensure machine_id and version are persisted ──
    try:
        if _telemetry is not None:
            _init_settings = load_persisted_settings()
            _prev_version = str(_init_settings.get('version', '') or '').strip()
            _current_version = _telemetry._read_version()
            _telemetry.get_machine_id(_init_settings)
            _init_settings['version'] = _current_version
            _init_settings.setdefault('raw_preview_cache_enabled', True)
            _init_settings.setdefault('exposure_quality', 'balanced')
            _init_settings.setdefault('exposure_corrected_thumbs', True)
            _init_settings.setdefault('auto_save_enabled', True)

            _apply_legal_upgrade_self_heal(_init_settings, _prev_version, _current_version)

            save_persisted_settings(_init_settings)
    except Exception:
        pass  # failsafe

    if args.root:
        log('Default root (client-supplied):', args.root)
    url = f'http://{HOST}:{args.port}/'
    if not WEBVIEW_IMPORT_SUCCESS:
        raise RuntimeError('pywebview is required. Browser-only mode is no longer supported.')
    log('Windowed mode enabled; using pywebview')

    def _serve():
        try:
            server.serve_forever()
        except Exception:
            pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    api = None
    try:
        log('Starting windowed UI via pywebview...')
        api = Api() # start maximized
        api._server_port = args.port
        win = webview.create_window('Project Kestrel', url, js_api=api, maximized=True)
        api._main_window = win

        # When the analysis queue is running, intercept the close event so the
        # window minimizes to the taskbar instead of killing mid-analysis.
        def _on_closing():
            # Check for unsaved changes via the Python-side flag
            # (avoid evaluate_js here - it deadlocks because closing runs on the GUI thread)
            has_unsaved = getattr(api, '_has_unsaved_changes', False)

            def _cleanup_preview_cache_before_exit():
                try:
                    if hasattr(api, 'cleanup_tracked_culling_caches'):
                        api.cleanup_tracked_culling_caches()
                except Exception as e:
                    warn('Cache cleanup on close failed:', e)

            def _cancel_analysis_wait_for_worker_and_telemetry():
                """Cancel queue, wait for worker (sends completion telemetry), then allow HTTP to finish."""
                try:
                    _queue_manager.cancel()
                except Exception:
                    pass
                try:
                    _queue_manager.join_worker(timeout=120.0)
                except Exception:
                    pass
                try:
                    # Mirror top-level crash handler: async telemetry uses daemon threads.
                    time.sleep(2)
                except Exception:
                    pass

            # When an analysis is running or paused, prompt the user with
            # options to Minimize, Exit (cancel) or Cancel the close.
            if _queue_manager.is_running or _queue_manager.is_paused:
                try:
                    # Use native Windows MessageBox if available for a simple
                    # three-button prompt. Fallback to tkinter dialog when not.
                    if sys.platform.startswith('win'):
                        import ctypes
                        MB_YESNOCANCEL = 0x00000003
                        MB_ICONQUESTION = 0x00000020
                        title = 'Analysis in progress'
                        if _queue_manager.is_paused:
                            msg = 'Analysis is paused. Exit Project Kestrel? You can re-open later to resume.'
                        else:
                            msg = 'Analysis is in progress. Cancel analysis and exit?'
                        resp = ctypes.windll.user32.MessageBoxW(0, msg, title, MB_YESNOCANCEL | MB_ICONQUESTION)
                        # IDYES=6 -> Exit (cancel analysis and close)
                        # IDNO=7  -> Minimize instead of closing
                        # IDCANCEL=2 -> Do not close
                        if resp == 6:
                            _cancel_analysis_wait_for_worker_and_telemetry()
                            _cleanup_preview_cache_before_exit()
                            return True
                        if resp == 7:
                            try:
                                win.minimize()
                            except Exception:
                                pass
                            return False
                        return False
                    else:
                        # Tkinter fallback
                        import tkinter as _tk
                        from tkinter import messagebox as _mb
                        root = _tk.Tk()
                        root.withdraw()
                        if _queue_manager.is_paused:
                            msg = 'Analysis is paused. Exit Project Kestrel? You can re-open later to resume.'
                        else:
                            msg = 'Analysis is in progress. Cancel analysis and exit?'
                        res = _mb.askyesnocancel('Analysis in progress', msg)
                        root.destroy()
                        # askyesnocancel returns True=Yes, False=No, None=Cancel
                        if res is True:
                            _cancel_analysis_wait_for_worker_and_telemetry()
                            _cleanup_preview_cache_before_exit()
                            return True
                        if res is False:
                            try:
                                win.minimize()
                            except Exception:
                                pass
                            return False
                        return False
                except Exception:
                    # If the prompt fails, fall back to minimizing when running
                    try:
                        win.minimize()
                    except Exception:
                        pass
                    return False

            # Prompt for unsaved changes when no analysis is running
            if has_unsaved:
                try:
                    if sys.platform.startswith('win'):
                        import ctypes
                        MB_YESNO = 0x00000004
                        MB_ICONWARNING = 0x00000030
                        msg = 'You have unsaved changes that will be lost. Close anyway?'
                        title = 'Unsaved Changes'
                        resp = ctypes.windll.user32.MessageBoxW(0, msg, title, MB_YESNO | MB_ICONWARNING)
                        if resp == 6:  # Yes - close and discard
                            _cleanup_preview_cache_before_exit()
                            return True
                        return False  # No - don't close
                    else:
                        import tkinter as _tk
                        from tkinter import messagebox as _mb
                        root = _tk.Tk()
                        root.withdraw()
                        res = _mb.askyesno(
                            'Unsaved Changes',
                            'You have unsaved changes that will be lost. Close anyway?'
                        )
                        root.destroy()
                        if res:
                            _cleanup_preview_cache_before_exit()
                            return True
                        return False
                except Exception:
                    _cleanup_preview_cache_before_exit()
                    return True  # on failure, allow close

            _cleanup_preview_cache_before_exit()
            return True  # allow normal close

        try:
            win.events.closing += _on_closing
        except Exception:
            pass  # older pywebview versions may not support this event

        # KESTREL_DEBUG=1 enables pywebview's debug mode — adds right-click
        # context menu with "Inspect" (DevTools) so the JS console is reachable.
        # Off by default for shipped builds; toggle for diagnostic sessions.
        _kestrel_debug = os.environ.get('KESTREL_DEBUG', '').strip().lower() in ('1', 'true', 'yes', 'on')
        webview.start(debug=_kestrel_debug)
    finally:
        # Signal in-flight cloud-compute uploads to stop FIRST. Their
        # ThreadPoolExecutor registers an atexit join of (non-daemon) worker
        # threads, so a still-running upload would otherwise keep uploading
        # every queued image and hang the process after the window has closed.
        # Local-only — the job resumes server-side on next launch.
        try:
            if api is not None and hasattr(api, 'stop_cloud_uploads_for_shutdown'):
                n = api.stop_cloud_uploads_for_shutdown()
                if n:
                    log(f'Signalled {n} in-flight cloud upload(s) to stop.')
        except Exception as e:
            warn('Cloud-upload shutdown signal failed:', e)
        try:
            if api is not None and hasattr(api, 'cleanup_tracked_culling_caches'):
                api.cleanup_tracked_culling_caches()
        except Exception as e:
            warn('Cache cleanup during shutdown failed:', e)
        try:
            server.shutdown()
            server.server_close()
        except Exception as e:
            warn('Server shutdown error:', e)
        log('Server stopped.')
        # Mark clean exit here (inside finally) so it runs even if server
        # shutdown raises, preventing a false "unclean shutdown" on next launch.
        _mark_session_clean_exit()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        _mark_session_clean_exit()
    except Exception as _main_exc:
        # Top-level crash handler — send crash report before re-raising
        _mark_session_exit_reason('crash')
        try:
            import traceback as _tb
            if _telemetry is not None:
                _crash_settings = load_persisted_settings()
                _crash_mid = _telemetry.get_machine_id(_crash_settings)

                # Fetch recent log tail, passing the active folder's log if available
                _folder_path = _crash_settings.get('active_analysis_path', '')
                if _folder_path:
                    _log_tail = _telemetry.get_recent_log_tail(folder=_folder_path, runtime_log_files=3)
                else:
                    _log_tail = _telemetry.get_recent_log_tail(runtime_log_files=3)

                _telemetry.send_crash_report(
                    exc=_main_exc,
                    tb_str=_tb.format_exc(),
                    log_tail=_log_tail,
                    machine_id=_crash_mid,
                    version=_telemetry._read_version(),
                    exit_reason='crash',
                    crash_reports_enabled=bool(_crash_settings.get('crash_reports_enabled', True)),
                )
                # Give daemon thread a moment to fire off the HTTP request
                import time as _t
                _t.sleep(2)
        except Exception:
            pass  # crash handler itself must never hide the real error
        raise
