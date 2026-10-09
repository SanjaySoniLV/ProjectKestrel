"""queue.js _thumbCache must LRU-cap and revoke blob: URLs like #119.

A plain Map stored ``createObjectURL`` results with no cap and no
``revokeObjectURL``. ``BlobUrlCache`` in blob-zoom.js already does both;
queue thumbs must use that class, clear it on folder-cache cleanup, and
not create a second URL when a concurrent load won the race.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ANALYZER = Path(__file__).resolve().parents[2]
_QUEUE_JS = _ANALYZER / "js" / "queue.js"
_BLOB_ZOOM_JS = _ANALYZER / "js" / "blob-zoom.js"
_VISUALIZER_HTML = _ANALYZER / "visualizer.html"


@pytest.fixture(scope="module")
def queue_js() -> str:
    return _QUEUE_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def blob_zoom_js() -> str:
    return _BLOB_ZOOM_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def visualizer_html() -> str:
    return _VISUALIZER_HTML.read_text(encoding="utf-8")


def test_thumb_cache_is_blob_url_cache_not_a_plain_map(queue_js: str, blob_zoom_js: str):
    assert re.search(r"const _thumbCache\s*=\s*new BlobUrlCache\s*\(\s*\)", queue_js), (
        "_thumbCache must be a BlobUrlCache so eviction/clear revoke blob: URLs"
    )
    assert re.search(r"const _thumbCache\s*=\s*new Map\s*\(", queue_js) is None
    assert "class BlobUrlCache extends Map" in blob_zoom_js
    assert "URL.revokeObjectURL" in blob_zoom_js
    assert "BLOB_URL_CACHE_MAX" in blob_zoom_js


def test_blob_zoom_loads_before_queue(visualizer_html: str):
    blob_i = visualizer_html.find("js/blob-zoom.js")
    queue_i = visualizer_html.find("js/queue.js")
    assert blob_i != -1 and queue_i != -1
    assert blob_i < queue_i, "BlobUrlCache is declared in blob-zoom.js; queue.js must load after"


def test_cleanup_clears_thumb_cache(queue_js: str):
    start = queue_js.find("async function _cleanupCullingCachesForPaths")
    assert start != -1
    nxt = queue_js.find("\n    async function ", start + 1)
    if nxt == -1:
        nxt = queue_js.find("\n    function ", start + 1)
    body = queue_js[start : nxt if nxt != -1 else None]
    assert "_thumbCache.clear()" in body, (
        "folder uncheck/app close must revoke queue thumbs, not only blobUrlCache"
    )


def test_load_img_is_the_only_thumb_blob_url_creator(queue_js: str):
    assert queue_js.count("_base64ToBlobUrl") == 1
    load_start = queue_js.find("async function _loadImg")
    assert load_start != -1
    nxt = queue_js.find("\n    function ", load_start + 1)
    load_fn = queue_js[load_start : nxt if nxt != -1 else None]
    assert "_base64ToBlobUrl" in load_fn
    assert "_loadImg(img, img.dataset.thumbRel" in queue_js


def test_load_img_rechecks_cache_after_await(queue_js: str):
    load_start = queue_js.find("async function _loadImg")
    nxt = queue_js.find("\n    function ", load_start + 1)
    load_fn = queue_js[load_start : nxt if nxt != -1 else None]
    await_i = load_fn.find("await window.pywebview.api.read_image_file")
    assert await_i != -1
    after = load_fn[await_i:]
    assert "_thumbCache.has(key)" in after, (
        "concurrent loads must reuse the first blob: URL; BlobUrlCache.set does not revoke overwrites"
    )
