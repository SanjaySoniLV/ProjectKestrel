"""Unit tests for macOS app-quit detection in ``shutdown_watch``.

On macOS the Quit menu item pywebview installs is wired to
``NSApplication.terminate:``. That calls ``exit()`` without ever returning
from ``NSApp.run()``, so ``webview.start()`` does not return and the
clean-exit write in ``visualizer.main()``'s ``finally`` — gated on
``webview.start()`` having returned — never runs. A perfectly ordinary ⌘Q
therefore left ``app_session_exit_reason`` at 'unknown' and the *next* launch
raised a false unclean-shutdown recovery prompt.

``NSApplicationWillTerminateNotification`` is posted just before that
``exit()``, so observing it is the only chance to record the quit. These
tests pin that wiring against a fake PyObjC, because CI has no macOS runner
with a window server and the real notification cannot be posted headlessly.

Two things are easy to get wrong and are asserted explicitly:

* The notification is posted on ``NSNotificationCenter.defaultCenter()``, not
  on the workspace centre the power-off observer uses. Registering it on the
  wrong centre installs cleanly and then silently never fires.
* The selector registered must exist on the observer class, or PyObjC raises
  only at post time — i.e. during the user's quit, where the exception is
  swallowed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import shutdown_watch

pytestmark = pytest.mark.unit


# ── Fake PyObjC ───────────────────────────────────────────────────────────
# Just enough of AppKit/Foundation for _install_macos to run off-macOS:
# an NSObject base that supports the alloc().init() dance and subclassing,
# and two notification centres that record their registrations.


class _FakeCenter:
    def __init__(self):
        self.registrations = []

    def addObserver_selector_name_object_(self, observer, selector, name, obj):
        self.registrations.append((observer, selector, name, obj))

    def post(self, name):
        """Dispatch like Cocoa does: look the selector up on the observer."""
        for observer, selector, reg_name, _obj in self.registrations:
            if reg_name != name:
                continue
            method = getattr(observer, selector.replace(':', '_'), None)
            assert method is not None, (
                f'observer has no method for selector {selector!r}; '
                f'PyObjC would raise at post time, inside the user\'s quit'
            )
            method(None)


class _FakeNSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


@pytest.fixture
def fake_cocoa(monkeypatch):
    """Install fake AppKit/Foundation/objc modules and reset module state."""
    import types

    default_center = _FakeCenter()
    workspace_center = _FakeCenter()

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace():
            return _FakeWorkspace()

        def notificationCenter(self):
            return workspace_center

    class _FakeNotificationCenter:
        @staticmethod
        def defaultCenter():
            return default_center

    appkit = types.ModuleType('AppKit')
    appkit.NSWorkspace = _FakeWorkspace
    foundation = types.ModuleType('Foundation')
    foundation.NSObject = _FakeNSObject
    foundation.NSNotificationCenter = _FakeNotificationCenter
    objc_mod = types.ModuleType('objc')

    monkeypatch.setitem(sys.modules, 'AppKit', appkit)
    monkeypatch.setitem(sys.modules, 'Foundation', foundation)
    monkeypatch.setitem(sys.modules, 'objc', objc_mod)

    # install() is guarded by module-level state; clear it between tests.
    monkeypatch.setattr(shutdown_watch, '_installed', False, raising=False)
    monkeypatch.setattr(shutdown_watch, '_keepalive', [], raising=False)
    monkeypatch.setattr(shutdown_watch, '_listeners', set(), raising=False)

    return default_center, workspace_center


class TestMacOSAppQuit:
    def test_quit_observer_registered_on_default_center(self, fake_cocoa):
        default_center, workspace_center = fake_cocoa

        assert shutdown_watch._install_macos(lambda: None, lambda: None) is True

        quit_names = [r[2] for r in default_center.registrations]
        assert 'NSApplicationWillTerminateNotification' in quit_names, (
            'quit notification must be observed on the DEFAULT centre — '
            'NSApplication does not post it to the workspace centre'
        )
        # And the power-off observer stays where it was.
        assert [r[2] for r in workspace_center.registrations] == [
            'NSWorkspaceWillPowerOffNotification'
        ]

    def test_quit_notification_invokes_callback(self, fake_cocoa):
        default_center, _ = fake_cocoa
        fired = []

        shutdown_watch._install_macos(
            lambda: fired.append('power_off'),
            lambda: fired.append('app_quit'),
        )
        default_center.post('NSApplicationWillTerminateNotification')

        assert fired == ['app_quit']

    def test_power_off_and_quit_callbacks_are_independent(self, fake_cocoa):
        default_center, workspace_center = fake_cocoa
        fired = []

        shutdown_watch._install_macos(
            lambda: fired.append('power_off'),
            lambda: fired.append('app_quit'),
        )
        workspace_center.post('NSWorkspaceWillPowerOffNotification')
        default_center.post('NSApplicationWillTerminateNotification')

        assert fired == ['power_off', 'app_quit']

    def test_no_quit_callback_leaves_power_off_working(self, fake_cocoa):
        """``on_app_quit`` is optional; omitting it must not break install."""
        default_center, workspace_center = fake_cocoa
        fired = []

        assert shutdown_watch._install_macos(lambda: fired.append('power_off')) is True
        assert default_center.registrations == []

        workspace_center.post('NSWorkspaceWillPowerOffNotification')
        assert fired == ['power_off']

    def test_quit_registration_failure_keeps_power_off_observer(self, fake_cocoa):
        """The new observer must not be able to break the old one.

        The power-off observer has shipped for releases; the quit observer is
        new. If anything about the quit registration fails — an older PyObjC
        without the symbol, a raising centre — power-off must survive it.
        """
        _, workspace_center = fake_cocoa
        import types

        # An older PyObjC: NSObject is there, NSNotificationCenter is not, so
        # `from Foundation import NSNotificationCenter` raises ImportError.
        older_pyobjc = types.ModuleType('Foundation')
        older_pyobjc.NSObject = _FakeNSObject
        sys.modules['Foundation'] = older_pyobjc  # undone by the fixture's monkeypatch

        fired = []
        assert shutdown_watch._install_macos(
            lambda: fired.append('power_off'), lambda: fired.append('app_quit')
        ) is True

        assert shutdown_watch.installed_listeners() == ('macos_power_off',)
        workspace_center.post('NSWorkspaceWillPowerOffNotification')
        assert fired == ['power_off']

    def test_install_reports_which_listeners_landed(self, fake_cocoa, monkeypatch):
        monkeypatch.setattr(shutdown_watch.sys, 'platform', 'darwin', raising=False)
        monkeypatch.delenv('KESTREL_FAKE_OS_SHUTDOWN', raising=False)

        assert shutdown_watch.install(lambda: None, on_app_quit=lambda: None) is True
        assert shutdown_watch.installed_listeners() == (
            'macos_app_quit',
            'macos_power_off',
        )

    def test_quit_callback_fires_at_most_once(self, fake_cocoa, monkeypatch):
        """A second WillTerminate must not re-write the exit reason."""
        monkeypatch.setattr(shutdown_watch.sys, 'platform', 'darwin', raising=False)
        monkeypatch.delenv('KESTREL_FAKE_OS_SHUTDOWN', raising=False)
        default_center, _ = fake_cocoa
        fired = []

        shutdown_watch.install(lambda: None, on_app_quit=lambda: fired.append(1))
        default_center.post('NSApplicationWillTerminateNotification')
        default_center.post('NSApplicationWillTerminateNotification')

        assert fired == [1]

    def test_quit_callback_exception_is_swallowed(self, fake_cocoa, monkeypatch):
        """This runs inside the user's quit — it must never raise."""
        monkeypatch.setattr(shutdown_watch.sys, 'platform', 'darwin', raising=False)
        monkeypatch.delenv('KESTREL_FAKE_OS_SHUTDOWN', raising=False)
        default_center, _ = fake_cocoa

        def _boom():
            raise RuntimeError('settings write failed')

        shutdown_watch.install(lambda: None, on_app_quit=_boom)
        default_center.post('NSApplicationWillTerminateNotification')  # no raise


class TestNonMacOSUnaffected:
    def test_install_accepts_on_app_quit_off_macos(self, monkeypatch):
        """The parameter is macOS-only but must be harmless everywhere."""
        monkeypatch.setattr(shutdown_watch, '_installed', False, raising=False)
        monkeypatch.setattr(shutdown_watch, '_listeners', set(), raising=False)
        monkeypatch.setattr(shutdown_watch.sys, 'platform', 'linux', raising=False)
        monkeypatch.delenv('KESTREL_FAKE_OS_SHUTDOWN', raising=False)
        fired = []

        shutdown_watch.install(
            lambda: fired.append('os'), on_app_quit=lambda: fired.append('quit')
        )

        # Nothing posts NSApplicationWillTerminate off macOS.
        assert 'quit' not in fired
        assert not any(
            name.startswith('macos_') for name in shutdown_watch.installed_listeners()
        )

    def test_missing_pyobjc_returns_false(self, monkeypatch):
        monkeypatch.setattr(shutdown_watch, '_installed', False, raising=False)
        monkeypatch.setattr(shutdown_watch, '_listeners', set(), raising=False)
        monkeypatch.setitem(sys.modules, 'AppKit', None)

        assert shutdown_watch._install_macos(lambda: None, lambda: None) is False


class TestVisualizerWiring:
    """The fix is only live if ``main()`` actually passes the callback."""

    def test_main_passes_clean_exit_as_on_app_quit(self):
        import ast

        src = Path(__file__).parent.parent.parent / 'visualizer.py'
        tree = ast.parse(src.read_text(encoding='utf-8'))
        main_fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'main'
        )

        install_calls = [
            node
            for node in ast.walk(main_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'install'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'shutdown_watch'
        ]
        assert len(install_calls) == 1, 'expected one shutdown_watch.install() call'

        kwargs = {kw.arg: kw.value for kw in install_calls[0].keywords}
        assert 'on_app_quit' in kwargs, (
            'main() must pass on_app_quit or ⌘Q still reports a false crash'
        )
        called = {
            node.func.id
            for node in ast.walk(kwargs['on_app_quit'])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert '_mark_session_clean_exit' in called, (
            'a ⌘Q is a clean user quit, not an os_shutdown or a crash'
        )
