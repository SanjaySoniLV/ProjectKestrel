"""
Auth-worker client — desktop-side wrapper for auth.projectkestrel.org.

Minimal surface: a single method for ``GET /v1/me/entitlements``, used by the
analyze dialog's cloud-destination flow to learn how many concurrency slots
are in use vs. allowed before attempting a submit. Same Clerk JWT the
cloud-compute client uses; this lives in its own module because the target
domain is different (Auth Worker, not CC Worker) and we want a clean
boundary so future Auth-Worker endpoints can land here without bloating the
CC client.

Response shape (mirror of MyAccount's renderer):
    {
        "userId": "user_...",
        "tier": "free" | "paid" | ...,
        "limits": { "maxConcurrentJobs": int, ... },
        "currentUsage": { ... },
        "activeJobs": [ { "jobId": str, "startedAt": int }, ... ]
    }
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import quote
from typing import Any


_DEFAULT_AUTH_API_BASE = "https://auth.projectkestrel.org"


def default_auth_api_base() -> str:
    """Resolve Auth Worker base URL — env override, then default."""
    return os.environ.get("KESTREL_AUTH_API_BASE", _DEFAULT_AUTH_API_BASE).rstrip("/")


class AuthClientError(RuntimeError):
    """Raised on non-2xx response from the Auth Worker."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class AuthClientNetworkError(AuthClientError):
    """Transport-level failure talking to Auth (DNS, timeout, malformed JSON).
    Status is 0 so callers can distinguish from HTTP errors."""

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class AuthClient:
    """Stateless-ish wrapper around the Auth Worker REST API. Pattern mirrors
    ``analyzer/cloud_compute_client.py:CloudComputeClient`` but trimmed to a
    single endpoint."""

    def __init__(
        self,
        api_base: str,
        jwt_token: str | None,
        dev_user: str | None = None,
        timeout: int = 15,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._auth_headers: dict = {}
        du = dev_user or os.environ.get("KESTREL_DEV_USER_ID")
        if du:
            self._auth_headers["x-dev-user-id"] = str(du)
        t = str(jwt_token).strip() if jwt_token else ""
        if t:
            self._auth_headers["Authorization"] = f"Bearer {t}"
        if not du and not t:
            raise ValueError(
                "AuthClient needs a Clerk JWT (preferred) or "
                "KESTREL_DEV_USER_ID (wrangler dev only)"
            )

    def _request(self, method: str, path: str, body: Any | None = None) -> dict:
        url = f"{self.api_base}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "KestrelDesktop/Auth/1.0",
            **self._auth_headers,
        }
        data: bytes | None = None
        if method in ("POST", "PUT", "PATCH"):
            # The Auth Worker's mutate endpoints accept an empty JSON body.
            data = json.dumps(body if body is not None else {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise AuthClientNetworkError(
                        f"Auth Worker returned malformed JSON: {e}"
                    ) from e
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            raise AuthClientError(e.code, text) from e
        except urllib.error.URLError as e:
            raise AuthClientNetworkError(f"network error: {e.reason}") from e
        except socket.timeout as e:
            raise AuthClientNetworkError(
                f"request timed out after {self.timeout}s"
            ) from e
        except (TimeoutError, ConnectionError) as e:
            raise AuthClientNetworkError(f"transport error: {e}") from e

    def get_my_entitlements(self) -> dict:
        """GET /v1/me/entitlements — returns the user's tier, limits, usage,
        and active-job slots. Same data MyAccount renders on the Cloud
        Compute dashboard."""
        return self._request("GET", "/v1/me/entitlements")

    # ── Notifications (H6) — the central Auth-hosted store ──────────────────
    def get_notifications(self, limit: int = 30) -> dict:
        """GET /v1/me/notifications — {notifications: [...], unreadCount}."""
        return self._request("GET", f"/v1/me/notifications?limit={int(limit)}")

    def mark_notification_read(self, notif_id: str) -> dict:
        """POST /v1/me/notifications/{id}/read."""
        return self._request(
            "POST", f"/v1/me/notifications/{quote(str(notif_id), safe='')}/read"
        )

    def mark_all_notifications_read(self) -> dict:
        """POST /v1/me/notifications/read-all."""
        return self._request("POST", "/v1/me/notifications/read-all")

    def hide_notification(self, notif_id: str) -> dict:
        """DELETE /v1/me/notifications/{id} — soft-hide ("permanently hide")."""
        return self._request(
            "DELETE", f"/v1/me/notifications/{quote(str(notif_id), safe='')}"
        )

    def post_feedback(
        self,
        report_type: str,
        message: str,
        subject: str = "",
        version: str = "",
        os: str = "",
        contact: str = "",
    ) -> dict:
        """POST /v1/me/feedback — submit feedback as the signed-in user.

        report_type must be one of: bug, suggestion, general, account.
        Returns {ok: true, id} on success; raises AuthClientError on failure.
        """
        body: dict = {
            "product": "desktop",
            "report_type": report_type,
            "message": message,
        }
        if subject:
            body["subject"] = subject
        if version:
            body["version"] = version
        if os:
            body["os"] = os
        if contact:
            body["contact"] = contact
        return self._request("POST", "/v1/me/feedback", body)
