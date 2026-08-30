"""Unit tests for cloud-compute image discovery.

Regression: the upload-speed test ("stress test") and ``run_full_job``'s
fallback discovery used to hardcode ``{".cr3", ".jpg", ".jpeg"}``, so a folder
full of other supported RAW formats (NEF/ARW/CR2/DNG/RAF/...) reported
"no images found" even though the real job submit path handled them fine.
Discovery now delegates to ``folder_inspector.list_images_in_folder`` so every
cloud path agrees with the canonical RAW_EXTENSIONS / JPEG_EXTENSIONS config and
honours the RAW-priority rule.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cloud_compute_client as ccc
import folder_inspector

pytestmark = pytest.mark.unit


def _touch(folder: Path, *names: str) -> None:
    for n in names:
        (folder / n).write_bytes(b"x")


class TestDiscoverUploadImages:
    def test_finds_non_cr3_raw_formats(self, tmp_path):
        # The exact regression: a folder of NEFs must be discovered.
        _touch(tmp_path, "A.NEF", "B.nef", "C.ARW", "D.dng")
        found = ccc._discover_upload_images(tmp_path)
        assert sorted(p.name for p in found) == ["A.NEF", "B.nef", "C.ARW", "D.dng"]
        assert all(isinstance(p, Path) and p.is_absolute() for p in found)

    def test_raw_priority_skips_jpeg_sidecars(self, tmp_path):
        # When RAWs are present, JPEGs are ignored (mirrors the real job).
        _touch(tmp_path, "IMG_1.CR3", "IMG_1.jpg", "IMG_2.NEF", "IMG_2.JPG")
        found = ccc._discover_upload_images(tmp_path)
        assert sorted(p.name for p in found) == ["IMG_1.CR3", "IMG_2.NEF"]

    def test_jpeg_fallback_when_no_raws(self, tmp_path):
        _touch(tmp_path, "one.jpg", "two.jpeg", "three.png")
        found = ccc._discover_upload_images(tmp_path)
        assert sorted(p.name for p in found) == ["one.jpg", "three.png", "two.jpeg"]

    def test_filters_hidden_and_appledouble(self, tmp_path):
        _touch(tmp_path, "real.NEF", "._real.NEF", ".DS_Store")
        found = ccc._discover_upload_images(tmp_path)
        assert [p.name for p in found] == ["real.NEF"]

    def test_empty_folder_returns_empty(self, tmp_path):
        _touch(tmp_path, "notes.txt", "raw.bin")
        assert ccc._discover_upload_images(tmp_path) == []

    def test_sorted_by_name(self, tmp_path):
        _touch(tmp_path, "c.NEF", "a.NEF", "b.NEF")
        assert [p.name for p in ccc._discover_upload_images(tmp_path)] == [
            "a.NEF", "b.NEF", "c.NEF"
        ]

    def test_matches_folder_inspector(self, tmp_path):
        # Single source of truth: the helper is just folder_inspector + Paths.
        _touch(tmp_path, "x.ARW", "y.ARW", "skip.jpg")
        names = folder_inspector.list_images_in_folder(str(tmp_path))
        assert [p.name for p in ccc._discover_upload_images(tmp_path)] == names


class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestUploadTestUsesDiscovery:
    """End-to-end-ish: a NEF folder must reach the upload stage instead of
    raising 'no images found', and round-robin must still fill the sample."""

    def _client(self):
        return ccc.CloudComputeClient(api_base="https://example.invalid", jwt_token="t")

    def test_nef_folder_does_not_raise_no_images(self, tmp_path, monkeypatch):
        _touch(tmp_path, "A.NEF", "B.NEF")  # fewer than sample_count -> round-robin
        client = self._client()

        captured = {}

        def fake_urls(count, sizes=None):
            captured["count"] = count
            captured["sizes"] = sizes
            return {"presignedUrls": [{"url": f"https://up/{i}"} for i in range(count)]}

        monkeypatch.setattr(client, "request_upload_test_urls", fake_urls)
        monkeypatch.setattr(ccc.urllib.request, "urlopen", lambda *a, **k: _FakeResp(200))

        result = client.upload_test(tmp_path, sample_count=5)

        assert captured["count"] == 5  # round-robin filled 2 NEFs up to 5 slots
        assert result["samples_attempted"] == 5
        assert result["samples_uploaded"] == 5

    def test_truly_empty_folder_still_raises(self, tmp_path):
        _touch(tmp_path, "readme.txt")
        with pytest.raises(ValueError, match="no images found"):
            self._client().upload_test(tmp_path, sample_count=3)
