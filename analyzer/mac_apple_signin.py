"""Native "Sign in with Apple" via ASAuthorizationController (PyObjC).

Apple's App Review (Guideline 4) requires that *Sign in with Apple* complete
"without leaving the app" — i.e. via the native ``ASAuthorizationController``
system sheet, not a web redirect (even an in-app ``ASWebAuthenticationSession``
one). This module drives that native flow and returns the Apple **identity
token** (a JWT), which the caller exchanges with Clerk's Frontend API
(``strategy=oauth_token_apple``) — see ``oauth_client.run_apple_native_flow``.

Only the App Store (sandboxed) macOS build uses this. Windows/Linux and the
direct-download DMG keep the existing OAuth/loopback/ASWeb transports; Google
and email sign-in continue to go through ``ASWebAuthenticationSession`` on
macOS. This module is *only* the Apple-branded button's handler.

Design notes / gotchas this file encapsulates:

  * ``ASAuthorizationController.performRequests()`` MUST run on the AppKit main
    thread and needs a key window to anchor the sheet. The sign-in flow runs on
    a background worker, so we dispatch onto the main run loop (via mac_sandbox)
    and block the worker on an Event until the delegate fires.
  * ``delegate`` and ``presentationContextProvider`` are held **weakly** by the
    controller, so we keep strong Python refs to both (and to the controller)
    until completion, or the sheet silently never appears.
  * NONCE: Apple echoes whatever we set as ``request.nonce`` into the id_token's
    ``nonce`` claim. Convention (Firebase/Clerk/Apple sample code) is to set the
    **SHA-256 hex of a random raw nonce** on the request, and keep the raw nonce
    for the backend to re-hash and compare. Clerk requires a nonce to be present
    for native/desktop id-token sign-in, so we always set one. We return the raw
    nonce too, in case the exchange wants it.
  * ``identityToken`` / ``authorizationCode`` arrive as ``NSData``; we decode to
    UTF-8 str (the id_token is a compact JWS; the auth code is ASCII).

Import-safe everywhere: nothing here imports PyObjC at module load, and
:func:`is_available` returns ``False`` (never raises) off-mac, without PyObjC,
or without the AuthenticationServices wrapper.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sys
import threading
import time
from typing import Optional

# ASAuthorizationError.canceled — the user dismissed the sheet. Stable Apple
# constant (ASAuthorizationErrorCanceled = 1001). Treated as a benign
# "cancelled", not a failure, so we don't flash an error when the user backs out.
_ASAUTH_ERR_CANCELED = 1001

_load_lock = threading.Lock()
_cocoa: Optional[dict] = None
_load_attempted = False


def _gen_raw_nonce() -> str:
    """A high-entropy URL-safe raw nonce (kept; its SHA-256 goes on the request)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _load() -> Optional[dict]:
    """Import the AuthenticationServices/AppKit symbols we need, cached.

    Returns a dict of symbols, or ``None`` if anything required is missing
    (off-mac, no PyObjC, or the AuthenticationServices wrapper isn't installed).
    Also defines the delegate + presentation-context-provider ObjC subclasses
    once.
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
            import objc  # type: ignore  # noqa: F401
            from Foundation import NSObject  # type: ignore
            from AppKit import NSApplication  # type: ignore
            from AuthenticationServices import (  # type: ignore
                ASAuthorizationAppleIDProvider,
                ASAuthorizationController,
            )

            # Scope constants are optional — if the wrapper is missing them we
            # simply request no scopes (still a valid, if leaner, credential).
            try:
                from AuthenticationServices import (  # type: ignore
                    ASAuthorizationScopeFullName,
                    ASAuthorizationScopeEmail,
                )
                scopes = [ASAuthorizationScopeFullName, ASAuthorizationScopeEmail]
            except Exception:
                scopes = None

            def _anchor():
                app = NSApplication.sharedApplication()
                win = app.keyWindow()
                if win is None:
                    wins = app.windows()
                    if wins is not None and len(wins) > 0:
                        win = wins[0]
                return win

            # Delegate: receives the credential (or error). Per-call state
            # (result dict + done Event) is attached to the instance as plain
            # Python attributes after alloc().init() — PyObjC subclass instances
            # support that. Methods never raise back into ObjC.
            class _KestrelAppleDelegate(NSObject):
                def authorizationController_didCompleteWithAuthorization_(
                    self, controller, authorization
                ):
                    result = getattr(self, "py_result", None)
                    done = getattr(self, "py_done", None)
                    try:
                        cred = authorization.credential()
                        id_token = _nsdata_to_str(cred.identityToken())
                        auth_code = _nsdata_to_str(cred.authorizationCode())
                        user = _safe_str(cred.user()) if hasattr(cred, "user") else ""
                        email = _safe_str(cred.email()) if hasattr(cred, "email") else ""
                        if result is not None:
                            if not id_token:
                                result["error"] = "apple_no_identity_token"
                                result["error_description"] = (
                                    "ASAuthorizationAppleIDCredential had no identityToken."
                                )
                            else:
                                result["identity_token"] = id_token
                                result["authorization_code"] = auth_code
                                result["user"] = user
                                result["email"] = email
                    except Exception as e:  # noqa: BLE001
                        if result is not None:
                            result["error"] = "apple_credential_error"
                            result["error_description"] = str(e)
                    finally:
                        if done is not None:
                            done.set()

                def authorizationController_didCompleteWithError_(self, controller, error):
                    result = getattr(self, "py_result", None)
                    done = getattr(self, "py_done", None)
                    try:
                        code = None
                        try:
                            code = int(error.code())
                        except Exception:
                            code = None
                        if result is not None:
                            if code == _ASAUTH_ERR_CANCELED:
                                result["error"] = "cancelled"
                            else:
                                try:
                                    desc = str(error.localizedDescription())
                                except Exception:
                                    desc = ""
                                result["error"] = "apple_auth_error"
                                result["error_description"] = f"code {code}: {desc}".strip()
                    finally:
                        if done is not None:
                            done.set()

            # Presentation-context provider: returns the anchor NSWindow.
            class _KestrelApplePresenter(NSObject):
                def presentationAnchorForAuthorizationController_(self, controller):
                    return _anchor()

            _cocoa = {
                "ASAuthorizationAppleIDProvider": ASAuthorizationAppleIDProvider,
                "ASAuthorizationController": ASAuthorizationController,
                "scopes": scopes,
                "DelegateClass": _KestrelAppleDelegate,
                "PresenterClass": _KestrelApplePresenter,
            }
        except Exception:
            _cocoa = None
        return _cocoa


def _nsdata_to_str(data) -> str:
    """Decode an NSData (UTF-8 bytes) to str; '' on None/failure."""
    if data is None:
        return ""
    try:
        return bytes(data).decode("utf-8", errors="replace")
    except Exception:
        try:
            return str(data)
        except Exception:
            return ""


def _safe_str(v) -> str:
    if v is None:
        return ""
    try:
        return str(v)
    except Exception:
        return ""


def is_available() -> bool:
    """True iff native Sign in with Apple can be driven on this build.

    Never raises. False off-mac, without PyObjC, or without the
    AuthenticationServices wrapper bundled.
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
    *,
    timeout: float = 300.0,
    cancel_event: Optional[threading.Event] = None,
) -> dict:
    """Present the native Sign in with Apple sheet; return the Apple credential.

    Blocks the calling (worker) thread until the user finishes, cancels, or the
    timeout elapses. Returns one of:

      * ``{"identity_token": "<jwt>", "authorization_code": "...",
           "raw_nonce": "...", "user": "<apple sub>", "email": "..."}`` on success
      * ``{"error": "cancelled"}``                — user dismissed the sheet
      * ``{"error": "timeout"}``                  — no response within ``timeout``
      * ``{"error": "apple_unavailable"}``        — API not usable on this build
      * ``{"error": "apple_present_failed"|"apple_auth_error"|
                     "apple_credential_error"|"apple_no_identity_token",
           "error_description": ...}``

    Must be called off the main thread (it dispatches ``performRequests()`` to
    the main run loop and waits).
    """
    cocoa = _load()
    if cocoa is None:
        return {
            "error": "apple_unavailable",
            "error_description": "Native Sign in with Apple is not available on this build.",
        }

    ASAuthorizationAppleIDProvider = cocoa["ASAuthorizationAppleIDProvider"]
    ASAuthorizationController = cocoa["ASAuthorizationController"]
    scopes = cocoa["scopes"]
    DelegateClass = cocoa["DelegateClass"]
    PresenterClass = cocoa["PresenterClass"]

    raw_nonce = _gen_raw_nonce()
    hashed_nonce = _sha256_hex(raw_nonce)

    result: dict = {}
    done = threading.Event()
    # Strong refs kept alive for the whole flow: controller, delegate, presenter
    # (the latter two are referenced only weakly by the controller).
    holder: dict = {}

    def _start():
        try:
            provider = ASAuthorizationAppleIDProvider.alloc().init()
            request = provider.createRequest()
            if scopes is not None:
                try:
                    request.setRequestedScopes_(scopes)
                except Exception:
                    pass
            # Apple echoes this into the id_token's `nonce` claim; Clerk requires
            # it for native id-token sign-in.
            try:
                request.setNonce_(hashed_nonce)
            except Exception:
                pass

            controller = ASAuthorizationController.alloc().initWithAuthorizationRequests_([request])
            delegate = DelegateClass.alloc().init()
            delegate.py_result = result
            delegate.py_done = done
            presenter = PresenterClass.alloc().init()

            holder["controller"] = controller
            holder["delegate"] = delegate
            holder["presenter"] = presenter

            controller.setDelegate_(delegate)
            try:
                controller.setPresentationContextProvider_(presenter)
            except Exception:
                pass
            controller.performRequests()
        except Exception as e:  # noqa: BLE001
            result["error"] = "apple_present_failed"
            result["error_description"] = str(e)
            done.set()

    # Present from the main thread.
    try:
        _main_dispatch(_start, timeout=15.0)
    except Exception as e:
        return {
            "error": "apple_present_failed",
            "error_description": f"could not present Sign in with Apple sheet: {e}",
        }

    # Wait for the delegate, honoring cancellation and a hard timeout.
    deadline = time.monotonic() + float(timeout)
    while not done.wait(0.25):
        if cancel_event is not None and cancel_event.is_set():
            return {"error": "cancelled"}
        if time.monotonic() >= deadline:
            return {"error": "timeout"}

    if result.get("error"):
        return {"error": result["error"], "error_description": result.get("error_description")}
    if not result.get("identity_token"):
        return {"error": "apple_no_identity_token"}

    result["raw_nonce"] = raw_nonce
    return result
