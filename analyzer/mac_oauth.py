"""macOS ASWebAuthenticationSession transport for the Clerk OAuth flow.

Apple's App Review (Guideline 4) rejects native apps that bounce the user out
to the *default web browser* to sign in. The sanctioned pattern is
``ASWebAuthenticationSession`` — a Safari-backed sign-in sheet presented
in-app that captures a custom-scheme redirect (``kestrel://callback``) and
hands the callback URL straight back to the app, with no loopback server.

This module wraps that AppKit / AuthenticationServices API via PyObjC. It is the
macOS counterpart to the loopback transport in :mod:`oauth_client`; the token
exchange (PKCE, ``/oauth/token``) is identical and lives there. Only the
"open the authorize URL, get the redirect back" step differs.

Design notes / gotchas that this file exists to encapsulate:

  * ``ASWebAuthenticationSession.start()`` MUST run on the AppKit main thread.
    The OAuth flow runs on a background worker thread, so we dispatch the
    creation + ``start()`` onto the main run loop (via mac_sandbox) and then
    block the worker on an Event until the completion handler fires.
  * ``presentationContextProvider`` is held **weakly** by the session, so we
    must keep a strong Python reference to the provider or the sheet fails to
    present. Same for the session object itself until completion.
  * The completion handler is an ObjC block ``(NSURL?, NSError?)``. PyObjC can
    only marshal a plain Python callable into it when the
    ``pyobjc-framework-AuthenticationServices`` metadata is present — hence the
    dependency in requirements-macos.txt and the spec hiddenimport.

Import-safe everywhere: nothing here imports PyObjC at module load, and
:func:`is_available` returns ``False`` (never raises) off-mac, without PyObjC,
or without the AuthenticationServices wrapper. Callers fall back to the
loopback transport on non-macOS; on the sandboxed App Store build there is no
fallback (opening the external browser is exactly what Apple rejected), so an
unavailable session surfaces as an error instead.
"""

from __future__ import annotations

import sys
import time
import threading
from typing import Optional

# ASWebAuthenticationSessionErrorCodeCanceledLogin — the user dismissed the
# sheet. Stable Apple constant; treated as a benign "cancelled", not a failure.
_ASWEBAUTH_ERR_CANCELED = 1

_load_lock = threading.Lock()
_cocoa: Optional[dict] = None
_load_attempted = False


def _load() -> Optional[dict]:
    """Import the AuthenticationServices/AppKit symbols we need, cached.

    Returns a dict of symbols, or ``None`` if anything required is missing
    (off-mac, no PyObjC, or the AuthenticationServices wrapper isn't installed).
    Also defines the presentation-context-provider ObjC subclass once.
    """
    global _cocoa, _load_attempted
    if _load_attempted:
        return _cocoa
    with _load_lock:
        if _load_attempted:
            return _cocoa
        _load_attempted = True
        if sys.platform != "darwin":
            _cocoa = None
            return None
        try:
            import objc  # type: ignore
            from Foundation import NSObject, NSURL  # type: ignore
            from AppKit import NSApplication  # type: ignore
            from AuthenticationServices import (  # type: ignore
                ASWebAuthenticationSession,
            )

            # Presentation-context provider: returns the NSWindow the sheet
            # anchors to. Declare formal protocol conformance when we can get
            # the protocol object (some macOS versions check conformsToProtocol:
            # rather than just respondsToSelector:); fall back to informal
            # conformance (implementing the selector) otherwise.
            def _anchor(_self, _session):
                app = NSApplication.sharedApplication()
                win = app.keyWindow()
                if win is None:
                    wins = app.windows()
                    if wins is not None and len(wins) > 0:
                        win = wins[0]
                return win

            provider_cls = None
            try:
                proto = objc.protocolNamed(
                    "ASWebAuthenticationPresentationContextProviding"
                )

                class _KestrelAuthAnchor(NSObject, protocols=[proto]):
                    def presentationAnchorForWebAuthenticationSession_(self, session):
                        return _anchor(self, session)

                provider_cls = _KestrelAuthAnchor
            except Exception:
                class _KestrelAuthAnchorInformal(NSObject):
                    def presentationAnchorForWebAuthenticationSession_(self, session):
                        return _anchor(self, session)

                provider_cls = _KestrelAuthAnchorInformal

            _cocoa = {
                "NSURL": NSURL,
                "NSApplication": NSApplication,
                "ASWebAuthenticationSession": ASWebAuthenticationSession,
                "ProviderClass": provider_cls,
            }
        except Exception:
            _cocoa = None
        return _cocoa


def is_available() -> bool:
    """True iff ASWebAuthenticationSession can be driven on this build.

    Never raises. False off-mac, without PyObjC, or without the
    AuthenticationServices wrapper bundled (e.g. the direct-download DMG build,
    which keeps the loopback transport).
    """
    return _load() is not None


def _main_dispatch(fn, timeout: float):
    """Run ``fn`` on the AppKit main thread (delegates to mac_sandbox)."""
    try:
        import mac_sandbox  # type: ignore
    except Exception:
        from analyzer import mac_sandbox  # type: ignore
    return mac_sandbox.run_on_main_and_wait(fn, timeout=timeout)


def authenticate(
    authorize_url: str,
    callback_scheme: str,
    *,
    timeout: float = 300.0,
    cancel_event: Optional[threading.Event] = None,
) -> dict:
    """Present the sign-in sheet and return the captured callback URL.

    Blocks the calling (worker) thread until the user finishes, cancels, or the
    timeout elapses. Returns one of:

      * ``{"callback_url": "kestrel://callback?code=...&state=..."}`` on success
      * ``{"error": "cancelled"}``           — user dismissed the sheet / superseded
      * ``{"error": "timeout"}``             — no callback within ``timeout``
      * ``{"error": "aswebauth_unavailable"}`` — API not usable on this build
      * ``{"error": "aswebauth_start_failed"|"aswebauth_error", "error_description": ...}``

    Must be called off the main thread (it dispatches ``start()`` to the main
    run loop and waits).
    """
    cocoa = _load()
    if cocoa is None:
        return {
            "error": "aswebauth_unavailable",
            "error_description": "AuthenticationServices is not available on this build.",
        }

    ASWebAuthenticationSession = cocoa["ASWebAuthenticationSession"]
    NSURL = cocoa["NSURL"]
    ProviderClass = cocoa["ProviderClass"]

    result: dict = {}
    done = threading.Event()
    # Strong refs kept alive for the whole flow: the session, and — crucially —
    # the presentation provider, which the session references only weakly.
    holder: dict = {}

    def _completion(callback_url, error):
        try:
            if error is not None:
                code = None
                try:
                    code = int(error.code())
                except Exception:
                    code = None
                if code == _ASWEBAUTH_ERR_CANCELED:
                    result["error"] = "cancelled"
                else:
                    try:
                        desc = str(error.localizedDescription())
                    except Exception:
                        desc = ""
                    result["error"] = "aswebauth_error"
                    result["error_description"] = f"code {code}: {desc}".strip()
            elif callback_url is not None:
                result["callback_url"] = str(callback_url.absoluteString())
            else:
                result["error"] = "no_result"
        except Exception as e:  # noqa: BLE001 — never let the block raise into ObjC
            result["error"] = "aswebauth_error"
            result["error_description"] = str(e)
        finally:
            done.set()

    def _start():
        url = NSURL.URLWithString_(authorize_url)
        session = ASWebAuthenticationSession.alloc().initWithURL_callbackURLScheme_completionHandler_(
            url, callback_scheme, _completion
        )
        provider = ProviderClass.alloc().init()
        holder["session"] = session
        holder["provider"] = provider
        try:
            session.setPresentationContextProvider_(provider)
        except Exception:
            pass
        # Non-ephemeral: reuse the user's existing Safari/Clerk session so an
        # already-signed-in user gets a one-tap consent rather than re-entering
        # credentials. (Ephemeral would force a fresh login every time.)
        try:
            session.setPrefersEphemeralWebBrowserSession_(False)
        except Exception:
            pass
        started = bool(session.start())
        if not started:
            result["error"] = "aswebauth_start_failed"
            result["error_description"] = "ASWebAuthenticationSession.start() returned NO."
            done.set()

    def _cancel_session():
        sess = holder.get("session")
        if sess is None:
            return
        def _do_cancel():
            try:
                sess.cancel()
            except Exception:
                pass
        try:
            _main_dispatch(_do_cancel, timeout=5.0)
        except Exception:
            pass

    # Present the sheet from the main thread.
    try:
        _main_dispatch(_start, timeout=15.0)
    except Exception as e:
        return {
            "error": "aswebauth_start_failed",
            "error_description": f"could not present sign-in sheet: {e}",
        }

    # Wait for completion, honoring cancellation and a hard timeout. Poll in
    # short slices so a superseding sign-in (cancel_event) is picked up promptly.
    deadline = time.monotonic() + float(timeout)
    while not done.wait(0.25):
        if cancel_event is not None and cancel_event.is_set():
            _cancel_session()
            return {"error": "cancelled"}
        if time.monotonic() >= deadline:
            _cancel_session()
            return {"error": "timeout"}

    return result or {"error": "no_result"}
