"""QualityClassifier must not silently drop the percentile CSV.

``__init__`` used to wrap ``_load_normalization_data`` in ``except Exception``
and clear the tables. ``classify`` then returned the raw model score instead
of a 0–1 percentile, so star ratings were wrong with no error. A given path
must load or raise.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml.quality import QualityClassifier


pytestmark = pytest.mark.unit


def _qc():
    qc = QualityClassifier.__new__(QualityClassifier)
    qc._norm_qualities = None
    qc._norm_percentiles = None
    return qc


def _write_csv(path: Path, rows: list[tuple[str, str]]) -> Path:
    lines = ["percentile,quality"]
    lines.extend(f"{p},{q}" for p, q in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLoadNormalizationData:
    def test_valid_csv_maps_quality_to_percentile(self, tmp_path):
        csv_path = _write_csv(
            tmp_path / "norm.csv",
            [("0", "0.0"), ("100", "1.0")],
        )
        qc = _qc()
        qc._load_normalization_data(str(csv_path))
        assert qc._normalize_quality_to_percentile(0.5) == pytest.approx(0.5)

    def test_missing_file_raises(self, tmp_path):
        qc = _qc()
        missing = tmp_path / "missing.csv"
        with pytest.raises(FileNotFoundError):
            qc._load_normalization_data(str(missing))
        assert qc._norm_qualities is None

    def test_header_only_csv_raises(self, tmp_path):
        csv_path = _write_csv(tmp_path / "empty.csv", [])
        qc = _qc()
        with pytest.raises(ValueError, match="no valid percentile/quality"):
            qc._load_normalization_data(str(csv_path))
        assert qc._norm_qualities is None

    def test_garbage_rows_raise(self, tmp_path):
        csv_path = _write_csv(
            tmp_path / "garbage.csv",
            [("not-a-number", "also-bad"), ("", "")],
        )
        qc = _qc()
        with pytest.raises(ValueError, match="no valid percentile/quality"):
            qc._load_normalization_data(str(csv_path))
        assert qc._norm_qualities is None

    def test_lfs_pointer_raises(self, tmp_path):
        csv_path = tmp_path / "quality_normalization_data.csv"
        csv_path.write_text(
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "size 12\n",
            encoding="utf-8",
        )
        qc = _qc()
        with pytest.raises(ValueError, match="no valid percentile/quality"):
            qc._load_normalization_data(str(csv_path))
        assert qc._norm_qualities is None


class TestInitDoesNotSwallowCsvErrors:
    @pytest.fixture(autouse=True)
    def _stub_onnx(self, monkeypatch):
        class FakeInput:
            name = "input"

        class FakeSession:
            def get_providers(self):
                return ["CPUExecutionProvider"]

            def get_inputs(self):
                return [FakeInput()]

        monkeypatch.setattr(
            "kestrel_analyzer.ml.quality.ResilientOnnxSession",
            lambda *_a, **_k: FakeSession(),
        )
        monkeypatch.setattr(
            "kestrel_analyzer.ml.quality.debug",
            lambda *_a, **_k: None,
        )

    def test_missing_csv_raises_from_init(self, tmp_path):
        missing = tmp_path / "nope.csv"
        with pytest.raises(FileNotFoundError):
            QualityClassifier("quality.onnx", str(missing), coord=object())

    def test_empty_csv_raises_from_init(self, tmp_path):
        csv_path = _write_csv(tmp_path / "empty.csv", [])
        with pytest.raises(ValueError, match="no valid percentile/quality"):
            QualityClassifier("quality.onnx", str(csv_path), coord=object())

    def test_valid_csv_loads_on_init(self, tmp_path):
        csv_path = _write_csv(
            tmp_path / "norm.csv",
            [("0", "0.0"), ("100", "1.0")],
        )
        qc = QualityClassifier("quality.onnx", str(csv_path), coord=object())
        assert qc._normalize_quality_to_percentile(0.25) == pytest.approx(0.25)

    def test_omitted_path_still_skips_tables(self):
        qc = QualityClassifier("quality.onnx", None, coord=object())
        assert qc._norm_qualities is None
        assert qc._normalize_quality_to_percentile(0.4) == pytest.approx(0.4)
