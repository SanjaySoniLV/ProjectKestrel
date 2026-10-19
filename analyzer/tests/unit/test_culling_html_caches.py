"""culling.html: LRU caches, detached thumbs, stale preview, all_moved (WP-24)."""

from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
CULLING = ANALYZER / "culling.html"


def _html() -> str:
    return CULLING.read_text(encoding="utf-8")


def _fn_body(src: str, header: str) -> str:
    start = src.find(header)
    assert start != -1, f"missing {header!r}"
    nxt = src.find("\n    function ", start + len(header))
    async_nxt = src.find("\n    async function ", start + len(header))
    candidates = [i for i in (nxt, async_nxt) if i != -1]
    end = min(candidates) if candidates else len(src)
    return src[start:end]


@pytest.mark.unit
def test_culling_blob_and_raw_caches_are_blob_url_cache_not_plain_maps():
    src = _html()
    assert "let blobUrlCache = new BlobUrlCache();" in src
    assert "let rawCache = new BlobUrlCache();" in src
    assert "let blobUrlCache = new Map()" not in src
    assert "let rawCache = new Map()" not in src
    assert "class BlobUrlCache" in src
    assert "URL.revokeObjectURL" in src
    assert "BLOB_URL_CACHE_MAX" in src


@pytest.mark.unit
def test_get_blob_url_uses_blob_urls_and_rechecks_after_await():
    body = _fn_body(_html(), "async function getBlobUrl")
    await_i = body.find("await window.pywebview.api.read_image_file")
    has_i = body.find("blobUrlCache.has(key)")
    blob_i = body.find("_base64ToBlobUrl")
    assert await_i != -1
    assert has_i != -1
    assert blob_i != -1
    assert "data:" not in body or "_base64ToBlobUrl" in body
    assert await_i < blob_i
    # Post-await recheck sits between the IPC and createObjectURL.
    assert body.find("blobUrlCache.has(key)", await_i) != -1
    assert body.find("blobUrlCache.has(key)", await_i) < blob_i


@pytest.mark.unit
def test_lazy_load_img_skips_detached_elements():
    body = _fn_body(_html(), "function lazyLoadImg")
    await_i = body.find("await resolverFn()")
    assert await_i != -1
    first = body.find("if (!img.isConnected) return")
    second = body.find("if (!img.isConnected) return", first + 1)
    assert first != -1
    assert second != -1
    assert first < await_i < second


@pytest.mark.unit
def test_update_preview_drops_stale_generations():
    body = _fn_body(_html(), "async function updatePreview")
    assert "_previewGen" in body
    assert "++_previewGen" in body or "_previewGen++" in body
    first_await = body.find("await getBlobUrl")
    assert first_await != -1
    guard = body.find("gen !== _previewGen", first_await)
    assert guard != -1
    second_await = body.find("await getBlobUrl", first_await + 1)
    assert second_await != -1
    assert body.find("gen !== _previewGen", second_await) != -1


@pytest.mark.unit
def test_load_raw_rechecks_cache_after_await():
    body = _fn_body(_html(), "async function loadRawAsync")
    await_i = body.find("await window.pywebview.api.read_raw_full")
    has_i = body.find("rawCache.has(cacheKey)", await_i)
    blob_i = body.find("_base64ToBlobUrl")
    assert await_i != -1
    assert has_i != -1
    assert blob_i != -1
    assert await_i < has_i < blob_i
    assert "data:image/jpeg;base64" not in body


@pytest.mark.unit
def test_move_rejects_reads_all_moved():
    src = _html()
    start = src.find("window.pywebview.api.move_rejects_to_folder")
    end = src.find("async function undoMove")
    assert start != -1
    assert end != -1
    block = src[start:end]
    assert "moveRes.all_moved" in block
    assert "anyFailed = true" in block


@pytest.mark.unit
def test_partial_move_subtitle_does_not_blame_xmp():
    src = _html()
    start = src.find("async function executeActions")
    if start == -1:
        start = src.find("function executeActions")
    end = src.find("async function undoMove")
    assert start != -1
    body = src[start:end]
    assert "let xmpFailed = false" in body
    assert "xmpFailed = true" in body
    assert "if (doXmp &&  anyFailed)" not in body
    assert "if (doXmp &&  xmpFailed)" in body or "if (doXmp && xmpFailed)" in body
    assert "Some rejects were skipped" in body
    assert "moveRes.all_moved === false" in body
