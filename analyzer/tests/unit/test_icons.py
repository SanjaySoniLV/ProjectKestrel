"""The SVG icon set: icons.css is in sync with tools/icons/, and every icon the
frontend asks for exists."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ANALYZER = ROOT / "analyzer"

pytestmark = pytest.mark.unit


def test_icons_css_is_built_from_the_sources():
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "build_icons.py"), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_every_referenced_icon_exists():
    defined = set(re.findall(r"\.k-icon--([a-z0-9-]+)\s*\{", (ANALYZER / "css" / "icons.css").read_text("utf-8")))
    used: set[str] = set()
    sources = [ANALYZER / "visualizer.html", *sorted((ANALYZER / "js").glob("*.js"))]
    for p in sources:
        text = p.read_text("utf-8")
        used |= set(re.findall(r"k-icon--([a-z0-9-]+)", text))
        used |= set(re.findall(r"KestrelIcon(?:\.el|\.set\([^,]+,)?\(?\s*'([a-z0-9-]+)'", text))
        used |= set(re.findall(r"\bicon:\s*'([a-z0-9-]+)'", text))
    missing = sorted(u for u in used if u not in defined)
    assert not missing, f"icons used but not in tools/icons/: {missing}"
