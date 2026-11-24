"""Drag across differently scaled Qt screens in an isolated application."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_mixed_dpi_drag(tmp_path):
    config = tmp_path / "screens.json"
    config.write_text(json.dumps({"screens": [
        {"name": "left", "x": 0, "y": 0, "width": 1200, "height": 800, "logicalDpi": 96},
        {"name": "right", "x": 1200, "y": 0, "width": 1920, "height": 1080, "logicalDpi": 192},
    ]}))
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root), STELLAR_DATA_DIR=str(tmp_path),
               QT_SCREEN_SCALE_FACTORS="left=1;right=2", QT_SCALE_FACTOR="1")
    # Pass a relative config path: Qt uses ':' to separate plugin parameters,
    # and an absolute Windows drive path contains a colon.
    env["QT_QPA_PLATFORM"] = f"offscreen:configfile={config.name}"
    subprocess.run([sys.executable, str(Path(__file__).resolve())], cwd=tmp_path,
                   env=env, check=True, timeout=30)


def check_drag():
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtWidgets import QApplication
    from terminal_widget import TerminalWidget

    app = QApplication([])
    left, right = app.screens()
    widget = TerminalWidget()
    widget.resize(640, 360)
    widget.show()
    app.processEvents()
    widget.flush_resize()
    widget._process_output_text('\x1b[?1049h\x1b[?1000h' +
                                '\r\n'.join(f'row {i:02} 中文' for i in range(10)))
    button = Qt.MouseButton.LeftButton

    def mouse(kind, row, col, changed, held):
        x, _ = widget._col_tile(col)
        y, _ = widget._row_tile(row)
        app.sendEvent(widget, QMouseEvent(kind, QPointF(x + .25, y + .25), changed,
                                          held, Qt.KeyboardModifier.NoModifier))

    try:
        mouse(QEvent.Type.MouseButtonPress, 2, 0, button, button)
        mouse(QEvent.Type.MouseMove, 4, 9, Qt.MouseButton.NoButton, button)
        expected = 'row 02 中文\nrow 03 中文\nrow 04 中文'
        for screen, ratio in ((right, 2), (left, 1), (right, 2)):
            widget.windowHandle().setScreen(screen)
            widget.move(screen.geometry().x() + 20, 20)
            app.processEvents()
            widget.grab()
            assert widget.windowHandle().screen() is screen
            assert widget.devicePixelRatioF() == widget._cache_pixmap.devicePixelRatio() == ratio
            assert widget._is_selecting and widget._get_selected_text() == expected
        mouse(QEvent.Type.MouseButtonRelease, 4, 9, button, Qt.MouseButton.NoButton)
        widget._copy_selection_to_clipboard()
        assert app.clipboard().text() == expected
    finally:
        widget.cleanup()
        widget.close()


if __name__ == "__main__":
    check_drag()
