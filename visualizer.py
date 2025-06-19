import sys
import os
import pandas as pd
import subprocess
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFileDialog, 
    QPushButton, QSplitter, QGridLayout, QScrollArea, QMessageBox, QLineEdit,
    QFrame, QComboBox, QSizePolicy
)
from PyQt5.QtGui import QPixmap, QImage, QFont, QPalette, QResizeEvent
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QSize
import cv2


DIR_PATH = None  # Global variable to hold the directory path

class ModernButton(QPushButton):
    """Custom styled button for modern appearance"""
    def __init__(self, text, primary=False):
        super().__init__(text)
        if primary:
            self.setStyleSheet("""
                QPushButton {
                    background-color: #0078D4;
                    color: white;
                    border: none;
                    padding: 8px 16px;
                    border-radius: 4px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #106EBE;
                }
                QPushButton:pressed {
                    background-color: #005A9E;
                }
                QPushButton:disabled {
                    background-color: #CCCCCC;
                    color: #888888;
                }
            """)
        else:
            self.setStyleSheet("""
                QPushButton {
                    background-color: #F3F2F1;
                    color: #323130;
                    border: 1px solid #CCCCCC;
                    padding: 8px 16px;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #EDEBE9;
                    border-color: #0078D4;
                }
                QPushButton:pressed {
                    background-color: #E1DFDD;
                }
                QPushButton:disabled {
                    background-color: #F3F2F1;
                    color: #A19F9D;
                    border-color: #EDEBE9;
                }
            """)

class SearchBar(QWidget):
    """Modern search bar with text-based keyword search for species"""
    searchChanged = pyqtSignal(str)
    
    def __init__(self, species_list=None):
        super().__init__()
        self.setStyleSheet("""
            QWidget {
                background-color: #FAFAFA;
                border: 1px solid #E1E1E1;
                border-radius: 6px;
                padding: 4px;
            }
        """)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        
        # Search label
        search_label = QLabel("🔍 Search species:")
        search_label.setStyleSheet("border: none; padding: 0px; font-weight: bold;")
        layout.addWidget(search_label)
        
        # Search text input
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Enter species keyword (e.g., 'grouse', 'hawk', 'warbler')")
        self.search_input.setStyleSheet("""
            QLineEdit {
                border: 1px solid #CCCCCC;
                border-radius: 4px;
                padding: 6px 8px;
                min-width: 300px;
                background-color: white;
                font-size: 13px;
            }
            QLineEdit:hover {
                border-color: #0078D4;
            }
            QLineEdit:focus {
                border-color: #0078D4;
                outline: none;
            }
        """)
        #self.search_input.textChanged.connect(self.on_search_changed)
        layout.addWidget(self.search_input)
        
        # Search button
        search_btn = ModernButton("Search")
        search_btn.clicked.connect(self.perform_search)
        layout.addWidget(search_btn)
        
        # Clear button
        clear_btn = ModernButton("Clear")
        clear_btn.clicked.connect(self.clear_search)
        layout.addWidget(clear_btn)
        
        layout.addStretch()
    
    def on_search_changed(self, text):
        """Emit search signal when text changes"""
        self.searchChanged.emit(text.strip())
    
    def perform_search(self):
        """Trigger search with current text"""
        self.searchChanged.emit(self.search_input.text().strip())
    
    def clear_search(self):
        """Clear the search input"""
        self.search_input.clear()

class CropInfoPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumWidth(760)
        self.setMaximumWidth(760)
        self.setStyleSheet("""
            QWidget {
                background-color: #FAFAFA;
                border-right: 1px solid #E1E1E1;
            }
        """)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(16, 16, 16, 16)
        
        # Title
        title = QLabel("Image Details")
        title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        title.setStyleSheet("color: #323130; margin-bottom: 8px;")
        layout.addWidget(title)
        
        # Image display
        self.crop_label = QLabel()
        self.crop_label.setFixedSize(720, 720)
        self.crop_label.setAlignment(Qt.AlignCenter)
        self.crop_label.setStyleSheet("""
            border: 2px solid #E1E1E1;
            border-radius: 8px;
            background-color: white;
        """)
        layout.addWidget(self.crop_label)
        
        # Info display
        self.info_label = QLabel()
        self.info_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("""
            color: #323130;
            background-color: white;
            border: 1px solid #E1E1E1;
            border-radius: 6px;
            padding: 12px;
            font-size: 12px;
        """)
        layout.addWidget(self.info_label)
        
        # Buttons
        button_layout = QVBoxLayout()
        button_layout.setSpacing(8)
        
        self.open_btn = ModernButton("Open Original File", primary=True)
        self.open_btn.setEnabled(False)
        button_layout.addWidget(self.open_btn)
        
        self.open_darktable_btn = ModernButton("Open in Darktable")
        self.open_darktable_btn.setEnabled(False)
        button_layout.addWidget(self.open_darktable_btn)
        
        layout.addLayout(button_layout)
        layout.addStretch()
        
        self.current_base_file = None
        self.open_btn.clicked.connect(self.open_file)
        self.open_darktable_btn.clicked.connect(self.open_in_darktable)  

    def show_info(self, crop_path, metadata, base_file=None):
        if crop_path and os.path.exists(crop_path):
            img = cv2.imread(crop_path)
            if img is not None:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                h, w, ch = img.shape
                bytes_per_line = ch * w
                qimg = QImage(img.data, w, h, bytes_per_line, QImage.Format_RGB888)
                pixmap = QPixmap.fromImage(qimg)
                self.crop_label.setPixmap(pixmap.scaled(720, 720, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                self.crop_label.setText("Image not available")
                self.crop_label.setStyleSheet(self.crop_label.styleSheet() + "color: #A19F9D;")
        else:
            self.crop_label.setText("No crop available")
            self.crop_label.setStyleSheet(self.crop_label.styleSheet() + "color: #A19F9D;")
        
        if metadata:
            info = (
                f"<b>📁 File:</b> {metadata.get('filename','N/A')}<br><br>"
                f"<b>🎯 Species:</b> {metadata.get('species','Unknown')}<br>"
                f"<b>📊 Species Confidence:</b> {metadata.get('species_confidence','N/A'):.3f}<br><br>"
                f"<b>⭐ Quality Score:</b> {metadata.get('quality','N/A'):.3f}<br>"
                f"<b>🎬 Scene:</b> {metadata.get('scene_count','N/A')}"
            )
            self.info_label.setText(info)
            self.current_base_file = base_file
            self.open_btn.setEnabled(base_file is not None and os.path.exists(base_file))
            self.open_darktable_btn.setEnabled(base_file is not None and os.path.exists(base_file))
        else:
            self.info_label.setText("Select an image to view details")
            self.current_base_file = None
            self.open_btn.setEnabled(False)
            self.open_darktable_btn.setEnabled(False)

    def open_file(self):
        if self.current_base_file and os.path.exists(self.current_base_file):
            os.startfile(self.current_base_file)

    def open_in_darktable(self):
        if self.current_base_file and os.path.exists(self.current_base_file):
            try:
                subprocess.Popen(["darktable.exe", self.current_base_file])
            except FileNotFoundError:
                QMessageBox.warning(self, "Darktable Not Found", 
                                  "Darktable executable not found. Please ensure it's installed and in your PATH.")

class DynamicImageTile(QWidget):
    """A tile widget that dynamically resizes its image based on available space"""
    def __init__(self, row, select_callback, doubleclick_callback):
        super().__init__()
        self.row = row
        self.select_callback = select_callback
        self.doubleclick_callback = doubleclick_callback
        
        self.setStyleSheet("""
            QWidget {
                background-color: white;
                border: 2px solid #E1E1E1;
                border-radius: 8px;
                padding: 8px;
            }
            QWidget:hover {
                border-color: #0078D4;
                background-color: #F8F9FA;
            }
        """)
        
        # Set size policy to expand
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(4, 4, 4, 4)
        
        # Thumbnail - will resize dynamically
        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("border: 1px solid #E1E1E1; border-radius: 4px; background-color: #FAFAFA;")
        self.thumb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.thumb.setMinimumSize(120, 80)  # Minimum size
        
        # Load and set image
        self.original_pixmap = None
        self.load_image()
        
        layout.addWidget(self.thumb, 1)  # Give it stretch factor of 1
        
        # Info label
        info_text = f"<b>{row['filename']}</b><br>Q: {row['quality']:.3f}"
        self.label = QLabel(info_text)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("border: none; color: #323130; font-size: 10px;")
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.label, 0)  # No stretch for label
        
        # Set up mouse events
        self.setToolTip(f"File: {row['filename']}\nSpecies: {row['species']}\nQuality: {row['quality']:.3f}")
        self.mousePressEvent = lambda e: self.select_callback(self.row)
        self.mouseDoubleClickEvent = lambda e: self.doubleclick_callback()
        self.enterEvent = lambda e: self.select_callback(self.row)
    
    def load_image(self):
        """Load the image and store original pixmap"""
        img_path = self.row['export_path']
        if os.path.exists(img_path):
            img = cv2.imread(img_path)
            if img is not None:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                h, w, ch = img.shape
                bytes_per_line = ch * w
                qimg = QImage(img.data, w, h, bytes_per_line, QImage.Format_RGB888)
                self.original_pixmap = QPixmap.fromImage(qimg)
                self.update_image_size()
            else:
                self.thumb.setText("Image\nUnavailable")
                self.thumb.setStyleSheet(self.thumb.styleSheet() + "color: #A19F9D;")
        else:
            self.thumb.setText("Image\nNot Found")
            self.thumb.setStyleSheet(self.thumb.styleSheet() + "color: #A19F9D;")
    
    def update_image_size(self):
        """Update image size to fit current thumbnail size"""
        if self.original_pixmap:
            size = self.thumb.size()
            if size.width() > 10 and size.height() > 10:  # Avoid tiny sizes
                scaled_pixmap = self.original_pixmap.scaled(
                    size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.thumb.setPixmap(scaled_pixmap)
    
    def resizeEvent(self, event):
        """Handle resize events to update image size"""
        super().resizeEvent(event)
        QTimer.singleShot(10, self.update_image_size)  # Slight delay to ensure size is updated

class DynamicSceneTile(QWidget):
    """A scene tile widget that dynamically resizes based on available space"""
    def __init__(self, scene_info, select_callback, doubleclick_callback):
        super().__init__()
        self.scene_info = scene_info
        self.select_callback = select_callback
        self.doubleclick_callback = doubleclick_callback
        
        self.setStyleSheet("""
            QWidget {
                background-color: white;
                border: 2px solid #E1E1E1;
                border-radius: 12px;
                padding: 12px;
            }
            QWidget:hover {
                border-color: #0078D4;
                background-color: #F8F9FA;
            }
        """)
        
        # Set size policy to expand
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)
        
        # Scene thumbnail - will resize dynamically
        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("""
            border: 2px solid #E1E1E1;
            border-radius: 8px;
            background-color: #FAFAFA;
        """)
        self.thumb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.thumb.setMinimumSize(200, 140)  # Minimum size
        
        # Load and set image
        self.original_pixmap = None
        self.load_image()
        
        layout.addWidget(self.thumb, 1)  # Give it stretch factor of 1
        
        # Scene info
        representative_img = scene_info['representative_image']
        scene_id = scene_info['scene_id']
        image_count = scene_info['image_count']
        species_list = scene_info['species_list']
        max_quality = scene_info['max_quality']
        
        species_str = ", ".join(species_list[:3])  # Show first 3 species
        if len(species_list) > 3:
            species_str += f" + {len(species_list) - 3} more"
        
        info_html = f"""
        <div style="text-align: center;">
            <h3 style="margin: 2px 0; color: #323130;">Scene {scene_id}</h3>
            <p style="margin: 1px 0; color: #605E5C; font-size: 11px;"><b>📸 {image_count} images | ⭐ Max Quality: {max_quality:.3f}</b></p>
            <p style="margin: 1px 0; color: #0078D4; font-size: 10px;"><b>🐦 {species_str}</b></p>
        </div>
        """
        
        self.label = QLabel(info_html)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("border: none; background-color: transparent;")
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.label, 0)  # No stretch for label
        
        # Set up mouse events and tooltip
        tooltip_text = f"Scene {scene_id}\n{image_count} images\nSpecies: {species_str}\nAvg Quality: {max_quality:.3f}"
        self.setToolTip(tooltip_text)
        self.mousePressEvent = lambda e: self.select_callback(self.scene_info)
        self.mouseDoubleClickEvent = lambda e: self.doubleclick_callback(self.scene_info)
    
    def load_image(self):
        """Load the scene image and store original pixmap"""
        representative_img = self.scene_info['representative_image']
        img_path = representative_img.get('export_path', '')
        if os.path.exists(img_path):
            img = cv2.imread(img_path)
            if img is not None:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                h, w, ch = img.shape
                bytes_per_line = ch * w
                qimg = QImage(img.data, w, h, bytes_per_line, QImage.Format_RGB888)
                self.original_pixmap = QPixmap.fromImage(qimg)
                self.update_image_size()
            else:
                self.thumb.setText("Scene Image\nUnavailable")
                self.thumb.setStyleSheet(self.thumb.styleSheet() + "color: #A19F9D;")
        else:
            self.thumb.setText("Scene Image\nNot Found")
            self.thumb.setStyleSheet(self.thumb.styleSheet() + "color: #A19F9D;")
    
    def update_image_size(self):
        """Update image size to fit current thumbnail size"""
        if self.original_pixmap:
            size = self.thumb.size()
            if size.width() > 10 and size.height() > 10:  # Avoid tiny sizes
                scaled_pixmap = self.original_pixmap.scaled(
                    size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.thumb.setPixmap(scaled_pixmap)
    
    def resizeEvent(self, event):
        """Handle resize events to update image size"""
        super().resizeEvent(event)
        QTimer.singleShot(10, self.update_image_size)  # Slight delay to ensure size is updated

class FlexibleGridLayout(QGridLayout):
    """A grid layout that automatically adjusts columns based on available width"""
    def __init__(self, target_columns=4, min_item_width=200):
        super().__init__()
        self.target_columns = target_columns
        self.min_item_width = min_item_width
        self.setSpacing(8)
        self.setContentsMargins(16, 16, 16, 16)
    
    def calculate_columns(self, available_width):
        """Calculate optimal number of columns based on available width"""
        if available_width < self.min_item_width:
            return 1
        
        # Calculate how many columns can fit
        max_possible = available_width // self.min_item_width
        return min(max_possible, self.target_columns)

class DynamicImageTileView(QScrollArea):
    def __init__(self, images, select_callback, doubleclick_callback, target_columns=4):
        super().__init__()
        self.images = images
        self.select_callback = select_callback
        self.doubleclick_callback = doubleclick_callback
        self.target_columns = target_columns
        self.tiles = []
        
        self.widget = QWidget()
        self.layout = FlexibleGridLayout(target_columns, 200)
        self.widget.setLayout(self.layout)
        self.setWidgetResizable(True)
        self.setWidget(self.widget)
        self.setStyleSheet("""
            QScrollArea {
                background-color: white;
                border: none;
            }
        """)
        
        self.populate()
        
        # Set up resize timer to avoid constant relayout during resize
        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.relayout_tiles)
    
    def populate(self):
        """Create all image tiles"""
        self.tiles = []
        for row in self.images:
            tile = DynamicImageTile(row, self.select_callback, self.doubleclick_callback)
            self.tiles.append(tile)
        
        self.relayout_tiles()
    
    def relayout_tiles(self):
        """Relayout tiles based on current width"""
        # Clear existing layout
        for i in reversed(range(self.layout.count())):
            child = self.layout.itemAt(i).widget()
            if child:
                self.layout.removeWidget(child)
        
        # Calculate columns based on available width
        available_width = self.width() - 40  # Account for margins and scrollbar
        cols = self.layout.calculate_columns(available_width)
        
        # Add tiles to layout
        for idx, tile in enumerate(self.tiles):
            row = idx // cols
            col = idx % cols
            self.layout.addWidget(tile, row, col)
    
    def resizeEvent(self, event):
        """Handle resize events with a timer to avoid constant relayout"""
        super().resizeEvent(event)
        self.resize_timer.start(100)  # 100ms delay

class DynamicSceneTileView(QScrollArea):
    def __init__(self, scenes, select_callback, doubleclick_callback, target_columns=4):
        super().__init__()
        self.scenes = scenes
        self.select_callback = select_callback
        self.doubleclick_callback = doubleclick_callback
        self.target_columns = target_columns
        self.tiles = []
        
        self.widget = QWidget()
        self.layout = FlexibleGridLayout(target_columns, 280)
        self.widget.setLayout(self.layout)
        self.setWidgetResizable(True)
        self.setWidget(self.widget)
        self.setStyleSheet("""
            QScrollArea {
                background-color: white;
                border: none;
            }
        """)
        
        self.populate()
        
        # Set up resize timer
        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.relayout_tiles)
    
    def populate(self):
        """Create all scene tiles"""
        self.tiles = []
        for scene_info in self.scenes:
            tile = DynamicSceneTile(scene_info, self.select_callback, self.doubleclick_callback)
            self.tiles.append(tile)
        
        self.relayout_tiles()
    
    def relayout_tiles(self):
        """Relayout tiles based on current width"""
        # Clear existing layout
        for i in reversed(range(self.layout.count())):
            child = self.layout.itemAt(i).widget()
            if child:
                self.layout.removeWidget(child)
        
        # Calculate columns based on available width
        available_width = self.width() - 40  # Account for margins and scrollbar
        cols = self.layout.calculate_columns(available_width)
        
        # Add tiles to layout
        for idx, tile in enumerate(self.tiles):
            row = idx // cols
            col = idx % cols
            self.layout.addWidget(tile, row, col)
    
    def resizeEvent(self, event):
        """Handle resize events with a timer to avoid constant relayout"""
        super().resizeEvent(event)
        self.resize_timer.start(100)  # 100ms delay

class SceneDetailVisualizer(QWidget):
    def __init__(self, images, scene_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Kestrel Scene {scene_id} Detail Visualizer v2.1")
        self.resize(1400, 800)
        # Sort images by quality descending
        self.images = sorted(images, key=lambda x: -x.get('quality', 0))
        self.filename_to_path = {row['filename']: os.path.join(DIR_PATH,row.get('filename', '')) for row in self.images}
        self.init_ui()
        self.show_images()

    def init_ui(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #F5F5F5;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        
        self.splitter = QSplitter(Qt.Horizontal)
        self.left_panel = CropInfoPanel()
        self.right_panel = QWidget()
        self.right_panel.setStyleSheet("background-color: white;")
        
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setContentsMargins(0, 0, 0, 0)
        self.right_panel.setLayout(self.right_layout)
        
        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(self.right_panel)
        self.splitter.setSizes([400, 1000])
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.splitter)
        self.setLayout(layout)

    def show_images(self):
        # Clear existing widgets
        for i in reversed(range(self.right_layout.count())):
            widget = self.right_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        
        # Use 3 columns for scene detail view
        tile_view = DynamicImageTileView(self.images, self.on_image_select, 
                                       self.left_panel.open_in_darktable, target_columns=3)
        self.right_layout.addWidget(tile_view)

    def on_image_select(self, row):
        base_file = self.filename_to_path.get(row.get('filename', ''), None)
        #print(base_file) # debug
        self.left_panel.show_info(row.get('crop_path'), row, base_file)

class HighLevelVisualizerV2_1(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kestrel Folder Explorer")
        self.resize(1600, 900)
        self.scenes = []
        self.filtered_scenes = []
        self.db = None
        self.scene_to_images = {}
        self.all_species = set()
        self.init_ui()
        self.prompt_for_dir()

    def init_ui(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #F5F5F5;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Header
        header = QWidget()
        header.setStyleSheet("""
            QWidget {
                background-color: white;
                border-bottom: 2px solid #E1E1E1;
                padding: 12px;
            }
        """)
        header_layout = QVBoxLayout(header)
        
        # Title
        title = QLabel("🦅 Kestrel Folder Explorer")
        title.setFont(QFont("Segoe UI", 18, QFont.Bold))
        title.setStyleSheet("color: #323130; margin-bottom: 8px;")
        header_layout.addWidget(title)
        
        # Search bar (will be populated after loading data)
        self.search_widget = QWidget()
        search_layout = QHBoxLayout(self.search_widget)
        search_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(self.search_widget)
        
        main_layout.addWidget(header)
        
        # Main content area
        self.content_area = QWidget()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.content_area)

    def prompt_for_dir(self):
        global DIR_PATH
        dir_path = QFileDialog.getExistingDirectory(self, "Select directory containing .kestrel folder", "")
        if not dir_path:
            QMessageBox.information(self, "No Directory Selected", "No directory was selected. Exiting.")
            sys.exit(0)
        
        # Store the directory path globally
        DIR_PATH = dir_path
        
        db_path = os.path.join(dir_path, ".kestrel", "kestrel_database.csv")
        if not os.path.exists(db_path):
            QMessageBox.critical(self, "Database Not Found", f"Could not find .kestrel/kestrel_database.csv in {dir_path}")
            sys.exit(1)
        
        try:
            # Read CSV with proper column names
            self.db = pd.read_csv(db_path).dropna()
            
            # Collect all unique species
            self.all_species = set(self.db['species'].unique())
            
            # Process scenes
            self.process_scenes()
            
            # Setup search bar now that we have species data
            self.setup_search_bar()
            
            # Show scenes
            self.show_scenes()
            
        except Exception as e:
            QMessageBox.critical(self, "Error Loading Database", f"Error loading database: {str(e)}")
            sys.exit(1)

    def setup_search_bar(self):
        # Clear existing search widget
        for i in reversed(range(self.search_widget.layout().count())):
            child = self.search_widget.layout().itemAt(i).widget()
            if child:
                child.setParent(None)
        # Create new search bar
        self.search_bar = SearchBar()
        self.search_bar.searchChanged.connect(self.filter_scenes)
        self.search_widget.layout().addWidget(self.search_bar)
    
    def process_scenes(self):
        """Process database to create scene summaries"""
        # Group by scene_count
        scene_groups = self.db.groupby('scene_count')
        
        self.scenes = []
        self.scene_to_images = {}
        
        for scene_id, group in scene_groups:
            images = group.to_dict('records')
            self.scene_to_images[scene_id] = images
            
            # Get representative image (highest quality)
            representative_img = max(images, key=lambda x: x.get('quality', 0))
            
            # Calculate scene statistics
            image_count = len(images)
            
            # Filter species by confidence > 0.5 to reduce false positives
            high_confidence_species = group[group['species_confidence'] > 0.5]['species'].unique()
            species_list = list(high_confidence_species)
            
            max_quality = group['quality'].max()
            
            scene_info = {
                'scene_id': scene_id,
                'representative_image': representative_img,
                'image_count': image_count,
                'species_list': species_list,
                'max_quality': max_quality,
                'images': images
            }
            
            self.scenes.append(scene_info)
          # Sort scenes by max quality
        #self.scenes.sort(key=lambda x: x['max_quality'], reverse=True)
        self.filtered_scenes = self.scenes.copy()

    def filter_scenes(self, keyword_filter):
        """Filter scenes based on species keyword search"""
        if not keyword_filter:
            self.filtered_scenes = self.scenes.copy()
        else:
            # Convert keyword to lowercase for case-insensitive search
            keyword_lower = keyword_filter.lower()
            self.filtered_scenes = []
            
            for scene in self.scenes:
                # Check if any species in the scene contains the keyword
                scene_matches = False
                for species in scene['species_list']:
                    if keyword_lower in species.lower():
                        scene_matches = True
                        break
                
                if scene_matches:
                    self.filtered_scenes.append(scene)
        
        self.show_scenes()

    def show_scenes(self):
        """Display the filtered scenes"""
        # Clear existing content
        for i in reversed(range(self.content_layout.count())):
            widget = self.content_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)
        
        # Add status info
        status_widget = QWidget()
        status_widget.setStyleSheet("background-color: white; padding: 8px; border-bottom: 1px solid #E1E1E1;")
        status_layout = QHBoxLayout(status_widget)
        
        total_images = sum(scene['image_count'] for scene in self.filtered_scenes)
        status_text = f"📊 Showing {len(self.filtered_scenes)} scenes with {total_images} total images"
        
        if len(self.filtered_scenes) < len(self.scenes):
            status_text += f" (filtered from {len(self.scenes)} scenes)"
        
        status_label = QLabel(status_text)
        status_label.setStyleSheet("color: #605E5C; font-weight: bold;")
        status_layout.addWidget(status_label)
        status_layout.addStretch()
        
        self.content_layout.addWidget(status_widget)
        
        # Add scenes tile view with 5 columns for main view
        if self.filtered_scenes:
            tile_view = DynamicSceneTileView(self.filtered_scenes, self.on_scene_select, 
                                           self.open_scene_window, target_columns=5)
            self.content_layout.addWidget(tile_view)
        else:
            no_results = QLabel("No scenes found matching the search criteria.")
            no_results.setAlignment(Qt.AlignCenter)
            no_results.setStyleSheet("color: #A19F9D; font-size: 16px; padding: 40px;")
            self.content_layout.addWidget(no_results)   
         
    def on_scene_select(self, scene_info):
        """Handle scene selection (currently just a placeholder)"""
        pass

    def open_scene_window(self, scene_info):
        """Open detailed view for a specific scene"""
        scene_id = scene_info['scene_id']
        images = scene_info['images']
        
        # Create new window without parent to make it truly independent
        win = SceneDetailVisualizer(images, scene_id, parent=None)
        
        # Store reference to prevent garbage collection
        if not hasattr(self, 'scene_windows'):
            self.scene_windows = []
        self.scene_windows.append(win)
        
        # Remove from list when window is closed
        win.setAttribute(Qt.WA_DeleteOnClose)
        win.destroyed.connect(lambda: self.scene_windows.remove(win) if win in self.scene_windows else None)
        
        win.show()
        win.raise_()  # Bring window to front
        win.activateWindow()  # Activate the window

def main():
    app = QApplication(sys.argv)
    
    # Set application style
    app.setStyle('Fusion')
    
    # Modern color palette
    palette = QPalette()
    palette.setColor(QPalette.Window, Qt.white)
    palette.setColor(QPalette.WindowText, Qt.black)
    app.setPalette(palette)
    
    visualizer = HighLevelVisualizerV2_1()
    visualizer.show()
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
