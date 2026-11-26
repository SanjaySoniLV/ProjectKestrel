"""Favorites (js/favorites.js): the `favorite` column and the card heart.

Behaviour is driven through node with a tiny DOM stub, matching the repo's
other frontend tests. Python-side preservation of the column across a pipeline
save is covered in test_database.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.unit

_ANALYZER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FAVORITES = os.path.join(_ANALYZER_DIR, "js", "favorites.js")
_NODE = shutil.which("node")

_HARNESS = textwrap.dedent(
    """
    const fs = require('fs');
    global.window = global;
    global.header = ['filename'];
    global.dirty = [];
    global.markDirty = (root) => { dirty.push(root); };
    global.KestrelIcon = { el: (name) => ({ icon: name }) };
    eval(fs.readFileSync(process.argv[1], 'utf8'));
    const F = window.KestrelFavorites;
    const row = { filename: 'a.NEF', __rootPath: '/p' };
    const out = {};
    out.before = F.isFavorite(row);
    out.toggled = F.toggle(row);
    out.value = row.favorite;
    out.header = header.slice();
    out.setSameIsNoop = F.set(row, true);
    out.dirtyCalls = dirty.length;
    out.count = F.count([row, { favorite: '' }, { favorite: 'true' }]);
    F.toggle(row);
    out.after = F.isFavorite(row);
    out.cleared = row.favorite;
    console.log(JSON.stringify(out));
    """
)


@pytest.mark.skipif(_NODE is None, reason="no node runtime available")
def test_toggle_writes_the_column_and_marks_the_folder_dirty():
    res = subprocess.run([_NODE, "-e", _HARNESS, _FAVORITES], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["before"] is False
    assert out["toggled"] is True
    assert out["value"] == "1"
    assert "favorite" in out["header"]
    assert out["setSameIsNoop"] is False
    assert out["dirtyCalls"] == 1
    assert out["count"] == 2
    assert out["after"] is False
    assert out["cleared"] == ""
