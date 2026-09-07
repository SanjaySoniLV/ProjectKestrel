"""open_culling_window reuses one window (WP-25)."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import api_bridge


pytestmark = pytest.mark.unit


class _ClosedEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeCullingWindow:
    def __init__(self):
        self.events = types.SimpleNamespace(closed=_ClosedEvent())
        self.load_url = MagicMock()
        self.show = MagicMock()
        self.restore = MagicMock()
        self.title = ''


@pytest.fixture
def api():
    return api_bridge.Api()


@pytest.fixture
def fake_webview(monkeypatch):
    windows: list = []
    created: list = []

    def _create_window(*_a, **_k):
        win = FakeCullingWindow()
        windows.append(win)
        created.append(win)
        return win

    fake = types.ModuleType('webview')
    fake.windows = windows
    fake.create_window = MagicMock(side_effect=_create_window)
    monkeypatch.setitem(sys.modules, 'webview', fake)
    monkeypatch.setattr(api_bridge, 'WEBVIEW_IMPORT_SUCCESS', True)
    return fake, windows, created


def test_first_open_creates_one_window(api, fake_webview, tmp_path):
    fake, _windows, created = fake_webview
    root = tmp_path / 'shoot'
    root.mkdir()
    res = api.open_culling_window(str(root))
    assert res['success'] is True
    assert fake.create_window.call_count == 1
    assert len(created) == 1
    assert api._culling_window is created[0]
    assert created[0].events.closed.handlers


def test_second_open_same_root_reuses_without_reload(api, fake_webview, tmp_path):
    fake, _windows, created = fake_webview
    root = tmp_path / 'shoot'
    root.mkdir()
    first = api.open_culling_window(str(root))
    second = api.open_culling_window(str(root))
    assert first['success'] is True
    assert second['success'] is True
    assert fake.create_window.call_count == 1
    win = created[0]
    win.load_url.assert_not_called()
    win.show.assert_called()
    win.restore.assert_called()


def test_second_open_different_root_reuses_and_loads_url(api, fake_webview, tmp_path):
    fake, _windows, created = fake_webview
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    api.open_culling_window(str(a))
    res = api.open_culling_window(str(b))
    assert res['success'] is True
    assert fake.create_window.call_count == 1
    win = created[0]
    win.load_url.assert_called_once()
    url = win.load_url.call_args[0][0]
    assert 'culling.html' in url
    assert 'b' in url


def test_closed_window_allows_a_new_create(api, fake_webview, tmp_path):
    fake, windows, created = fake_webview
    root = tmp_path / 'shoot'
    root.mkdir()
    api.open_culling_window(str(root))
    win = created[0]
    windows.remove(win)
    for handler in list(win.events.closed.handlers):
        handler()
    assert api._culling_window is None
    res = api.open_culling_window(str(root))
    assert res['success'] is True
    assert fake.create_window.call_count == 2
