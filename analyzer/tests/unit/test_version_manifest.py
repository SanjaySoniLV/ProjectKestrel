"""Release-safety checks for the numeric version the update prompt relies on.

The in-app update check compares this build's number against the published
``version_v2.json`` (see ``analyzer/js/auth.js``). The number is read at runtime
by fetching ``VERSION_NUMBER.txt`` from the local static server, so it only
works if the file is present *and* bundled by PyInstaller.

Both failure modes are silent: with no number the client falls back to the old
codename comparison, and the effective-date scheduling plus the dev-build
suppression quietly stop working. Nothing crashes, so only a test catches it.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ANALYZER = Path(__file__).resolve().parents[2]
REPO = ANALYZER.parent

VERSION_NUMBER_FILE = ANALYZER / "VERSION_NUMBER.txt"
APPXMANIFEST = REPO / "packaging" / "ProjectKestrel.appxmanifest"
SPECS = (
    "ProjectKestrel.spec",
    "ProjectKestrel-macos.spec",
    "ProjectKestrel-macos-appstore.spec",
)

DOTTED = re.compile(r"^\d+(\.\d+)*$")


def _version_number() -> str:
    return VERSION_NUMBER_FILE.read_text(encoding="utf-8").strip()


def test_version_number_file_exists_and_parses():
    assert VERSION_NUMBER_FILE.is_file(), f"missing {VERSION_NUMBER_FILE}"
    raw = _version_number()
    assert DOTTED.match(raw), f"VERSION_NUMBER.txt must be dotted digits, got {raw!r}"


def test_version_number_matches_appxmanifest():
    """The bundled number and the Store package's Identity Version are the same
    release, so they must not drift apart during a version bump."""
    manifest = APPXMANIFEST.read_text(encoding="utf-8")
    identity = re.search(r"<Identity\b[^>]*?Version=\"([\d.]+)\"", manifest, re.S)
    assert identity, "no <Identity ... Version=...> in the appxmanifest"

    assert _version_number() == identity.group(1), (
        "analyzer/VERSION_NUMBER.txt and packaging/ProjectKestrel.appxmanifest "
        "disagree — bump both together"
    )


@pytest.mark.parametrize("spec_name", SPECS)
def test_specs_bundle_the_version_number(spec_name):
    """A frozen build with no VERSION_NUMBER.txt silently loses the numeric
    update gate, so every spec must ship it alongside VERSION.txt."""
    spec = (ANALYZER / spec_name).read_text(encoding="utf-8")
    assert "('VERSION_NUMBER.txt', '.')" in spec, (
        f"{spec_name} does not bundle VERSION_NUMBER.txt"
    )
