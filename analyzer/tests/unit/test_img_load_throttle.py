"""image-loader.js IPC throttle queue is bounded (WP-25)."""

from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
IMAGE_LOADER = ANALYZER / "js" / "image-loader.js"


def _src() -> str:
    return IMAGE_LOADER.read_text(encoding="utf-8")


def _schedule_body() -> str:
    src = _src()
    start = src.find("function _scheduleLoad")
    assert start != -1
    nxt = src.find("\n    function ", start + 1)
    if nxt == -1:
        nxt = src.find("\n    const _lazyObserver", start + 1)
    return src[start:nxt]


@pytest.mark.unit
def test_img_load_queue_has_a_numeric_cap():
    src = _src()
    assert "_IMG_LOAD_QUEUE_MAX" in src
    # Cap must be a finite literal, not a comment-only mention.
    assert "const _IMG_LOAD_QUEUE_MAX =" in src
    body = src.split("const _IMG_LOAD_QUEUE_MAX =", 1)[1].split("\n", 1)[0]
    n = int(body.strip().rstrip(";"))
    assert 0 < n <= 1024


@pytest.mark.unit
def test_schedule_load_drops_oldest_when_queue_is_full():
    body = _schedule_body()
    assert "_imgLoadThrottle.queue.push(fn)" in body
    assert "_imgLoadThrottle.queue.shift()" in body
    assert "_IMG_LOAD_QUEUE_MAX" in body
    push_i = body.find("_imgLoadThrottle.queue.push(fn)")
    # Oldest-drop sits on the overflow path before the push.
    overflow = body.find("queue.length >= _IMG_LOAD_QUEUE_MAX")
    if overflow == -1:
        overflow = body.find("queue.length > _IMG_LOAD_QUEUE_MAX")
    assert overflow != -1
    assert overflow < push_i
    assert body.find("queue.shift()", overflow) != -1
    assert body.find("queue.shift()", overflow) < push_i
