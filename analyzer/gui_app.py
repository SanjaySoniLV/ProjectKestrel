import torch as t
import onnxruntime as ort
import tensorflow as tf

import os
import sys
import threading
from typing import Optional, Dict, List

from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QWidget,
    QPushButton,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QFileDialog,
    QProgressBar,
    QCheckBox,
    QMessageBox,
    QGroupBox,
    QGridLayout,
    QSizePolicy,
)

#from pathlib import Path

#sys.path.insert(0, str(Path(__file__).parent))

from kestrel_analyzer.pipeline import AnalysisPipeline
from kestrel_analyzer.logging_utils import get_log_path, log_event, log_exception
from gui_helpers import load_qimage_from_path, numpy_to_qimage


def _read_version_text() -> str:
    candidates = []
    if getattr(sys, "frozen", False):
        base_dir = getattr(sys, "_MEIPASS", None)
        if base_dir:
            candidates.append(os.path.join(base_dir, "VERSION.txt"))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION.txt"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return ""


def _format_version_label(raw_text: str) -> str:
    if not raw_text:
        return "Version: unknown"
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return "Version: unknown"
    if len(lines) == 1:
        line = lines[0]
        return line if line.lower().startswith("version") else f"Version: {line}"
    return " | ".join(lines)


class ProcessingWorker(QThread):
    progress = pyqtSignal(int, int)
    image_processed = pyqtSignal(dict, QImage, QImage)
    status = pyqtSignal(str)
    thumbnail_ready = pyqtSignal(object)
    detection_ready = pyqtSignal(object)
    crops_ready = pyqtSignal(object)
    quality_ready = pyqtSignal(object)
    species_ready = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(self, folder: str, use_gpu: bool, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.use_gpu = use_gpu
        self._pause_event = threading.Event()
        self._pause_event.set()

    def pause(self):
        self._pause_event.clear()

    def resume(self):
        self._pause_event.set()

    def run(self):
        pipeline = AnalysisPipeline(use_gpu=self.use_gpu)

        def on_status(msg: str):
            self.status.emit(msg)

        def on_progress(processed: int, total: int):
            self.progress.emit(processed, total)

        def on_image(entry: dict):
            # Convert relative paths (new format) to absolute paths for Qt image loading
            export_path = entry.get("export_path")
            crop_path = entry.get("crop_path")
            
            # If paths are relative, make them absolute using the folder
            if export_path and not os.path.isabs(export_path):
                export_path = os.path.join(self.folder, export_path)
            if crop_path and not os.path.isabs(crop_path):
                crop_path = os.path.join(self.folder, crop_path)
            
            export_q = load_qimage_from_path(export_path)
            crop_q = load_qimage_from_path(crop_path)
            self.image_processed.emit(entry, export_q, crop_q)

        def on_thumbnail(data: dict):
            self.thumbnail_ready.emit(data)

        def on_detection(data: dict):
            self.detection_ready.emit(data)

        def on_crops(data: dict):
            self.crops_ready.emit(data)

        def on_quality(data: dict):
            self.quality_ready.emit(data)

        def on_species(data: dict):
            self.species_ready.emit(data)

        pipeline.process_folder(
            self.folder,
            pause_event=self._pause_event,
            callbacks={
                "on_status": on_status,
                "on_progress": on_progress,
                "on_image": on_image,
                "on_thumbnail": on_thumbnail,
                "on_detection": on_detection,
                "on_crops": on_crops,
                "on_quality": on_quality,
                "on_species": on_species,
            },
            analyzer_name="gui",
        )
        self.finished.emit()


class KestrelGUI(QWidget):
    def __init__(self):
        super().__init__()
        self._version_label = _format_version_label(_read_version_text())
        title_suffix = f" - {self._version_label}" if self._version_label != "Version: unknown" else ""
        self.setWindowTitle(f"Kestrel Analyzer{title_suffix}")
        self.setMinimumSize(1100, 700)

        self.folder: Optional[str] = None
        self.worker: Optional[ProcessingWorker] = None
        self._thumbnail_image: Optional[QImage] = None
        self._overlay_image: Optional[QImage] = None
        self._crop_images: List[Optional[QImage]] = [None] * 5
        self._paused: bool = False

        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout()

        control_box = QGroupBox("Controls")
        control_layout = QHBoxLayout()
        self.btn_select = QPushButton("Select Folder")
        self.btn_start = QPushButton("Start")
        self.btn_pause = QPushButton("Pause")
        self.btn_resume = QPushButton("Resume")
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)
        self.chk_gpu = QCheckBox("Use GPU When Possible")
        self.chk_gpu.setChecked(True)
        control_layout.addWidget(self.btn_select)
        control_layout.addWidget(self.btn_start)
        control_layout.addWidget(self.btn_pause)
        control_layout.addWidget(self.btn_resume)
        control_layout.addWidget(self.chk_gpu)
        control_layout.addStretch(1)
        control_box.setLayout(control_layout)

        status_box = QGroupBox("Live Status")
        status_layout = QVBoxLayout()
        self.progress = QProgressBar()
        self.lbl_status = QLabel("Idle")
        self.lbl_status.setWordWrap(True)
        self.lbl_filename = QLabel("File: -")
        self.lbl_version = QLabel(self._version_label)
        status_layout.addWidget(self.progress)
        status_layout.addWidget(self.lbl_status)
        status_layout.addWidget(self.lbl_filename)
        status_layout.addWidget(self.lbl_version)
        status_box.setLayout(status_layout)

        preview_box = QGroupBox("Preview")
        preview_layout = QHBoxLayout()
        self.lbl_thumbnail = QLabel("Thumbnail")
        self.lbl_overlay = QLabel("Detections")
        for lbl in (self.lbl_thumbnail, self.lbl_overlay):
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            lbl.setMinimumHeight(260)
        preview_layout.addWidget(self.lbl_thumbnail)
        preview_layout.addWidget(self.lbl_overlay)
        preview_box.setLayout(preview_layout)

        birds_box = QGroupBox("Top Detections")
        birds_layout = QGridLayout()
        self.crop_cards: List[Dict[str, QLabel]] = []
        for idx in range(5):
            card = QWidget()
            card_layout = QVBoxLayout()
            img_label = QLabel("No bird")
            img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            img_label.setMinimumSize(160, 160)
            confidence_label = QLabel("Detection Confidence: -")
            rating_label = QLabel("Rating: -")
            species_label = QLabel("Species: -")
            family_label = QLabel("Family: -")
            for lbl in (confidence_label, rating_label, species_label, family_label):
                lbl.setWordWrap(True)
            card_layout.addWidget(img_label)
            card_layout.addWidget(confidence_label)
            card_layout.addWidget(rating_label)
            card_layout.addWidget(species_label)
            card_layout.addWidget(family_label)
            card.setLayout(card_layout)
            birds_layout.addWidget(card, 0, idx)
            self.crop_cards.append(
                {
                    "image": img_label,
                    "confidence": confidence_label,
                    "rating": rating_label,
                    "species": species_label,
                    "family": family_label,
                }
            )
        birds_box.setLayout(birds_layout)

        main_layout.addWidget(control_box)
        main_layout.addWidget(status_box)
        main_layout.addWidget(preview_box, stretch=2)
        main_layout.addWidget(birds_box, stretch=2)
        self.setLayout(main_layout)

        self.btn_select.clicked.connect(self.select_folder)
        self.btn_start.clicked.connect(self.start_processing)
        self.btn_pause.clicked.connect(self.pause_processing)
        self.btn_resume.clicked.connect(self.resume_processing)

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Image Folder")
        if folder:
            self.folder = folder
            self.lbl_status.setText(f"Selected folder: {folder}")

    def start_processing(self):
        if not self.folder:
            QMessageBox.warning(self, "No Folder", "Please select a folder first.")
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "Running", "Processing already in progress.")
            return
        use_gpu = self.chk_gpu.isChecked()
        self.worker = ProcessingWorker(self.folder, use_gpu)
        self.worker.progress.connect(self.on_progress)
        self.worker.image_processed.connect(self.on_image_processed)
        self.worker.status.connect(self.on_status)
        self.worker.thumbnail_ready.connect(self.on_thumbnail_ready)
        self.worker.detection_ready.connect(self.on_detection_ready)
        self.worker.crops_ready.connect(self.on_crops_ready)
        self.worker.quality_ready.connect(self.on_quality_ready)
        self.worker.species_ready.connect(self.on_species_ready)
        self.worker.finished.connect(self.on_finished)
        self.progress.setValue(0)
        self.lbl_status.setText("Initializing...")
        self.lbl_filename.setText("File: -")
        self._paused = False
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.worker.start()

    def pause_processing(self):
        if self.worker and self.worker.isRunning():
            self.worker.pause()
            self._paused = True
            self.lbl_status.setText("Paused")
            self.btn_pause.setEnabled(False)
            self.btn_resume.setEnabled(True)

    def resume_processing(self):
        if self.worker and self.worker.isRunning():
            self.worker.resume()
            self._paused = False
            self.lbl_status.setText("Resumed")
            self.btn_pause.setEnabled(True)
            self.btn_resume.setEnabled(False)

    def on_progress(self, processed: int, total: int):
        if self.progress.maximum() != total:
            self.progress.setMaximum(total)
        self.progress.setValue(processed)

    def on_image_processed(self, entry: dict, export_img: QImage, crop_img: QImage):
        if entry.get("filename"):
            self.lbl_filename.setText(f"File: {entry.get('filename')}")

    def resizeEvent(self, event):
        self._refresh_images()
        super().resizeEvent(event)

    def on_status(self, msg: str):
        if self._paused:
            return
        self.lbl_status.setText(msg)

    def on_finished(self):
        self._paused = False
        self.lbl_status.setText("Finished")
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_resume.setEnabled(False)

    def _set_image_label(self, label: QLabel, image: Optional[QImage]) -> None:
        if image is None:
            label.setPixmap(QPixmap())
            return
        label.setPixmap(
            QPixmap.fromImage(image).scaled(
                label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _refresh_images(self) -> None:
        self._set_image_label(self.lbl_thumbnail, self._thumbnail_image)
        self._set_image_label(self.lbl_overlay, self._overlay_image)
        for idx, card in enumerate(self.crop_cards):
            self._set_image_label(card["image"], self._crop_images[idx])

    def _clear_cards(self) -> None:
        self._crop_images = [None] * 5
        for card in self.crop_cards:
            card["image"].setText("No bird")
            card["confidence"].setText("Detection Confidence: -")
            card["rating"].setText("Rating: -")
            card["species"].setText("Species: -")
            card["family"].setText("Family: -")

    @staticmethod
    def _format_stars(rating: int) -> str:
        rating = max(0, min(5, int(rating)))
        return "★" * rating + "☆" * (5 - rating)

    def on_thumbnail_ready(self, data: dict):
        thumb = numpy_to_qimage(data.get("thumbnail"))
        self._thumbnail_image = thumb
        self._overlay_image = None
        self._clear_cards()
        if data.get("filename"):
            self.lbl_filename.setText(f"File: {data.get('filename')}")
        self._refresh_images()

    def on_detection_ready(self, data: dict):
        overlay = numpy_to_qimage(data.get("overlay"))
        self._overlay_image = overlay
        self._refresh_images()

    def on_crops_ready(self, data: dict):
        crops = data.get("crops") or []
        confidences = data.get("confidences") or []
        self._crop_images = [None] * 5
        for idx, crop in enumerate(crops[:5]):
            self._crop_images[idx] = numpy_to_qimage(crop)
            self.crop_cards[idx]["image"].setText("")
            if idx < len(confidences):
                self.crop_cards[idx]["confidence"].setText(
                    f"Detection Confidence: {float(confidences[idx]):.2f}"
                )
            else:
                self.crop_cards[idx]["confidence"].setText("Detection Confidence: -")
            self.crop_cards[idx]["rating"].setText("Rating: -")
            self.crop_cards[idx]["species"].setText("Species: -")
            self.crop_cards[idx]["family"].setText("Family: -")
        for idx in range(len(crops), 5):
            self.crop_cards[idx]["image"].setText("No bird")
            self.crop_cards[idx]["confidence"].setText("Detection Confidence: -")
            self.crop_cards[idx]["rating"].setText("Rating: -")
            self.crop_cards[idx]["species"].setText("Species: -")
            self.crop_cards[idx]["family"].setText("Family: -")
        self._refresh_images()

    def on_quality_ready(self, data: dict):
        results = data.get("results") or []
        for idx in range(5):
            if idx < len(results):
                rating = results[idx].get("rating", 0)
                stars = self._format_stars(rating)
                detail = results[idx].get("detail_score", -1)
                sharpness = results[idx].get("sharpness_score", -1)
                detail_text = f" | Detail: {detail:.2f} Sharpness: {sharpness:.2f}" if detail >= 0 else ""
                self.crop_cards[idx]["rating"].setText(f"Rating: {stars}{detail_text}")
            else:
                self.crop_cards[idx]["rating"].setText("Rating: -")

    def on_species_ready(self, data: dict):
        results = data.get("results") or []
        for idx in range(5):
            if idx < len(results):
                species = results[idx].get("species", "Unknown")
                species_conf = results[idx].get("species_confidence", 0.0)
                family = results[idx].get("family", "Unknown")
                family_conf = results[idx].get("family_confidence", 0.0)
                self.crop_cards[idx]["species"].setText(
                    f"Species: {species} ({species_conf:.2f})"
                )
                self.crop_cards[idx]["family"].setText(
                    f"Family: {family} ({family_conf:.2f})"
                )
            else:
                self.crop_cards[idx]["species"].setText("Species: -")
                self.crop_cards[idx]["family"].setText("Family: -")


def main(app: Optional[QApplication] = None):
    log_path = get_log_path(None)
    try:
        log_event(
            log_path,
            {
                "level": "info",
                "event": "gui_start",
            },
        )
        owns_app = app is None
        app = app or QApplication(sys.argv)
        win = KestrelGUI()
        win.show()
        if owns_app:
            sys.exit(app.exec())
        else:
            app.exec()
    except Exception as e:
        log_exception(
            log_path,
            e,
            stage="startup",
            context={"analyzer": "gui"},
        )
        raise


if __name__ == "__main__":
    main()
