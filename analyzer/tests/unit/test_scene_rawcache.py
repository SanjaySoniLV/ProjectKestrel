"""scene-zoom.js sceneRawCache: LRU + revoke via BlobUrlCache (WP-23)."""

from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
SCENE_ZOOM = ANALYZER / "js" / "scene-zoom.js"
BLOB_ZOOM = ANALYZER / "js" / "blob-zoom.js"
QUEUE_JS = ANALYZER / "js" / "queue.js"
VISUALIZER = ANALYZER / "visualizer.html"


def _scene() -> str:
    return SCENE_ZOOM.read_text(encoding="utf-8")


def _blob() -> str:
    return BLOB_ZOOM.read_text(encoding="utf-8")


def _queue() -> str:
    return QUEUE_JS.read_text(encoding="utf-8")


def _visualizer() -> str:
    return VISUALIZER.read_text(encoding="utf-8")


@pytest.mark.unit
def test_scene_raw_cache_is_blob_url_cache_not_a_plain_map():
    scene = _scene()
    blob = _blob()
    assert "const sceneRawCache = new BlobUrlCache();" in scene
    assert "const sceneRawCache = new Map()" not in scene
    assert "class BlobUrlCache" in blob
    assert "URL.revokeObjectURL" in blob
    assert "BLOB_URL_CACHE_MAX" in blob


@pytest.mark.unit
def test_blob_zoom_loads_before_scene_zoom():
    html = _visualizer()
    blob_i = html.find('src="js/blob-zoom.js"')
    scene_i = html.find('src="js/scene-zoom.js"')
    assert blob_i != -1 and scene_i != -1
    assert blob_i < scene_i


@pytest.mark.unit
def test_cleanup_still_clears_scene_raw_cache():
    src = _queue()
    start = src.find("function _cleanupCullingCachesForPaths")
    assert start != -1
    snippet = src[start : start + 1200]
    assert "sceneRawCache.clear()" in snippet


@pytest.mark.unit
def test_load_scene_raw_rechecks_cache_after_await():
    src = _scene()
    start = src.find("async function loadSceneRawAsync")
    end = src.find("\nasync function ", start + 1)
    if end == -1:
        end = src.find("\nfunction ", start + 1)
    body = src[start:end]
    await_i = body.find("await window.pywebview.api.read_raw_full")
    has_i = body.find("sceneRawCache.has(key)")
    blob_i = body.find("_base64ToBlobUrl")
    assert await_i != -1
    assert has_i != -1
    assert blob_i != -1
    assert await_i < has_i < blob_i
    assert "if (!url)" in body
    assert "sceneRawCache.set(key, url)" in body
