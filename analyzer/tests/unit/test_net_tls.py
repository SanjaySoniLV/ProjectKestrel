"""Unit tests for the shared certifi-backed TLS context.

Regression guard for the frozen-macOS bug where stdlib-urllib clients used a
bare ``urlopen`` (default OpenSSL cert path → CERTIFICATE_VERIFY_FAILED in a
bundled ``.app``). Every urllib client must route HTTPS through
``net_tls.ssl_context`` and pass a bounded ``timeout``.
"""

import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import net_tls

pytestmark = pytest.mark.unit


class TestSslContext:
    def test_returns_ssl_context(self):
        ctx = net_tls.ssl_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_verifies_certs_and_checks_hostname(self):
        # A misconfigured context that skipped verification would silently
        # accept MITM'd uploads — assert the secure defaults survived.
        ctx = net_tls.ssl_context()
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_loads_a_ca_bundle(self):
        # certifi's bundle (or the system default fallback) must actually load
        # roots, otherwise every HTTPS handshake fails.
        ctx = net_tls.ssl_context()
        assert ctx.cert_store_stats().get("x509_ca", 0) > 0

    def test_is_cached(self):
        assert net_tls.ssl_context() is net_tls.ssl_context()


class TestClientsUseSharedContext:
    """Each stdlib-urllib client must import the shared context and bound its
    upload timeout — the two things that broke under bundling."""

    def test_cloud_compute_client_wired(self):
        import cloud_compute_client as ccc
        assert ccc._ssl_context is net_tls.ssl_context
        assert ccc._PUT_TIMEOUT_SEC == 60

    def test_auth_client_wired(self):
        import auth_client
        assert auth_client._ssl_context is net_tls.ssl_context

    def test_oauth_client_wired(self):
        import oauth_client
        assert oauth_client._ssl_context is net_tls.ssl_context
