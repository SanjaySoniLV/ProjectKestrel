"""Regression tests for editor_launch.launch() with a custom editor.

The custom-editor branch used to reference an ``info`` name that had
been shadowed later in the same function by a local assignment
(``info = _EDITOR_REGISTRY.get(editor)``), which made the earlier
``info(f'...')`` call fail with ``UnboundLocalError`` — surfaced as
"cannot access local variable 'info' where it is not associated with
a value" in the ``[API] open_in_editor error:`` log.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import editor_launch


pytestmark = pytest.mark.unit


def test_launch_custom_editor_does_not_raise_unbound_local(monkeypatch, tmp_path):
    """The custom-editor path must not shadow the ``info`` logger."""
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"")

    custom_exe = tmp_path / "fake_editor"
    custom_exe.write_bytes(b"")

    launched: list[list[str]] = []

    monkeypatch.setattr(
        editor_launch,
        "load_persisted_settings",
        lambda: {"customEditorPath": str(custom_exe)},
    )
    monkeypatch.setattr(
        editor_launch,
        "_validate_custom_editor_path",
        lambda raw: str(custom_exe),
    )
    monkeypatch.setattr(
        editor_launch.subprocess,
        "Popen",
        lambda argv, *a, **kw: launched.append(list(argv)) or None,
    )

    editor_launch.launch(str(target), "custom")

    assert launched, "custom editor Popen was not invoked"
    assert launched[0][0] == str(custom_exe)
    assert launched[0][-1] == str(target)


def test_launch_named_editor_still_uses_registry(monkeypatch, tmp_path):
    """Renaming the local variable must not break the registry lookup."""
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"")

    launched: list[list[str]] = []
    monkeypatch.setattr(
        editor_launch.subprocess,
        "Popen",
        lambda argv, *a, **kw: launched.append(list(argv)) or None,
    )
    # Force the Linux branch so the test is portable and hits the
    # ``if entry:`` path deterministically.
    monkeypatch.setattr(editor_launch.sys, "platform", "linux")

    editor_launch.launch(str(target), "gimp")

    assert launched, "no editor launch attempted"
    # The registry lists ``flatpak run org.gimp.GIMP`` first; either
    # that or plain ``gimp`` is acceptable, as long as the target is
    # the last argv element.
    assert launched[0][-1] == str(target)
