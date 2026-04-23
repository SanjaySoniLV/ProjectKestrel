import sys

def _create_splash(app):
    splash = QWidget()
    splash.setWindowTitle("Kestrel Analyzer")
    splash.setFixedSize(420, 160)
    layout = QVBoxLayout(splash)
    title_label = QLabel("Project Kestrel is Loading…", splash)
    title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title_label.setObjectName("splashTitle")
    status_label = QLabel("Starting…", splash)
    status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    status_label.setObjectName("splashStatus")
    layout.addStretch(1)
    layout.addWidget(title_label)
    layout.addWidget(status_label)
    layout.addStretch(1)
    splash.setLayout(layout)
    splash.show()
    app.processEvents()
    return splash

def _set_splash_text(app, splash, text: str) -> None:
    label = splash.findChild(QLabel, "splashStatus")
    if label:
        label.setText(text)
        app.processEvents()


def _run_cli() -> None:
    from cli import main
    main()

if __name__ == "__main__":
    if "--cli" in sys.argv:
        sys.argv = [arg for arg in sys.argv if arg != "--cli"]
        _run_cli()
        raise SystemExit(0)

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

    app = QApplication(sys.argv)
    splash = _create_splash(app)

    _set_splash_text(app, splash, "Loading ONNX Runtime…")
    import onnxruntime
    # Surface detected providers during startup to make backend/runtime debugging easier.
    _set_splash_text(app, splash, f"ONNX providers: {', '.join(onnxruntime.get_available_providers())}")

    _set_splash_text(app, splash, "Starting UI…")

    from kestrel_analyzer.logging_utils import get_log_path, log_event
    from gui_app import main

    log_path = get_log_path(None)
    log_event(
        log_path,
        {
            "level": "info",
            "event": "gui_start",
        },
    )
    splash.close()
    main(app)
