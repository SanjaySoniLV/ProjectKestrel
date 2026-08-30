"""Unit tests for build_attestation.py — BOM tolerance + happy path.

The Windows CI workflow used to write build_attestation.json via
PowerShell 5.1's `Set-Content -Encoding utf8`, which prepends a UTF-8
BOM. Python's `json.load` rejects a leading BOM, so the loader silently
failed and every Windows build was tagged 'legacy' by the API worker.
These tests pin the BOM-tolerant read path and the no-BOM happy path so
the regression cannot return through either end.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


pytestmark = pytest.mark.unit


@pytest.fixture
def fresh_module(monkeypatch, tmp_path):
    """Import build_attestation with module-level cache cleared and the
    candidate-paths list redirected at a writable temp directory.
    """
    sys.modules.pop('build_attestation', None)
    import build_attestation as ba

    attest_path = tmp_path / 'build_attestation.json'
    monkeypatch.setattr(ba, '_candidate_paths', lambda: [str(attest_path)])
    ba._loaded = False
    ba._meta = None
    ba._sig = None
    return ba, attest_path


def test_loads_clean_utf8_no_bom(fresh_module):
    ba, attest_path = fresh_module
    payload = {'meta': 'Rufous Hummingbird|abc123|1700000000', 'sig': 'deadbeef'}
    attest_path.write_text(json.dumps(payload), encoding='utf-8')

    assert ba.is_official() is True
    headers = ba.auth_headers()
    assert headers['X-Kestrel-Build-Meta'] == payload['meta']
    assert headers['X-Kestrel-Build-Sig'] == payload['sig']


def test_loads_utf8_with_bom(fresh_module):
    """Regression: PowerShell 5.1's `Set-Content -Encoding utf8` writes a
    BOM. Loader must still recognise the build as official."""
    ba, attest_path = fresh_module
    payload = {'meta': 'Rufous Hummingbird|abc123|1700000000', 'sig': 'deadbeef'}
    attest_path.write_bytes(
        b'\xef\xbb\xbf' + json.dumps(payload).encode('utf-8')
    )

    assert ba.is_official() is True
    headers = ba.auth_headers()
    assert headers['X-Kestrel-Build-Meta'] == payload['meta']
    assert headers['X-Kestrel-Build-Sig'] == payload['sig']


def test_missing_file_falls_back_to_legacy(fresh_module):
    ba, _ = fresh_module
    assert ba.is_official() is False
    assert ba.auth_headers() == {}


def test_malformed_json_falls_back_to_legacy(fresh_module):
    ba, attest_path = fresh_module
    attest_path.write_text('not-json', encoding='utf-8')
    assert ba.is_official() is False
    assert ba.auth_headers() == {}


def test_missing_sig_field_falls_back_to_legacy(fresh_module):
    ba, attest_path = fresh_module
    attest_path.write_text(json.dumps({'meta': 'x|y|1'}), encoding='utf-8')
    assert ba.is_official() is False
    assert ba.auth_headers() == {}
