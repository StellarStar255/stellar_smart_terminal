"""GUI 字号切换不能触发应用全局 polish 或内容区的重渲染。"""
import os
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QApplication, QPushButton
from main_window import MainWindow


_app = None  # Keep QApplication alive across test modules.


@pytest.fixture
def windows():
    global _app
    _app = QApplication.instance() or QApplication([])
    app = _app
    wins = [MainWindow(), MainWindow()]
    for w in wins:
        w._gui_font_size = 0
        w._select_combo_value(w.gui_font_spin, 0)
        w._apply_gui_font()
        w.show()
    app.processEvents()
    yield app, wins
    for w in wins:
        w._force_closing = True
        w.close()
        w.deleteLater()
    for _ in range(3):
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def select(w, size):
    w.gui_font_spin.setCurrentIndex(w.gui_font_spin.findData(size))


class StyleCounter(QObject):
    def __init__(self):
        super().__init__()
        self.changes = 0

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.StyleChange:
            self.changes += 1
        return False


def test_gui_change_keeps_app_style_and_content_untouched(windows):
    app, (a, b) = windows
    # An unrelated top-level widget must not receive a global style invalidation.
    unrelated = QPushButton('unrelated')
    unrelated.show()
    app.processEvents()
    counter = StyleCounter()
    unrelated.installEventFilter(counter)
    original = app.styleSheet()
    with patch.object(a.editor_area, 'set_editor_font_size') as editor, \
            patch.object(a, '_apply_zoom_to_explorer_tree') as tree, \
            patch.object(a, '_apply_global_zoom') as content:
        select(a, 14)
        app.processEvents()
        editor.assert_not_called()
        tree.assert_not_called()
        content.assert_not_called()
    assert app.styleSheet() == original
    assert counter.changes == 0
    unrelated.close()
    unrelated.deleteLater()


def test_embed_sync_and_auto_restore_without_compounding(windows):
    app, (a, b) = windows
    baseline = a.styleSheet()
    with patch.object(MainWindow, '_navigator_dock_mode', 'embed'):
        for size in (14, 16, 14):
            select(a, size)
            app.processEvents()
            for w in (a, b):
                assert w._gui_font_size == size
                assert w.gui_font_spin.currentData() == size
                assert w.styleSheet().count('QWidget { font-size:') == 1
                probe = QPushButton('probe', w)
                probe.ensurePolished()
                assert probe.font().pixelSize() == size
                probe.deleteLater()
        a._apply_theme(a.current_theme)
        assert a.styleSheet().count('QWidget { font-size:') == 1
        select(a, 0)
        assert a.styleSheet() == baseline
        assert 'QWidget { font-size:' not in b.styleSheet()


def test_floating_mode_does_not_resize_other_window(windows):
    app, (a, b) = windows
    original = b.styleSheet()
    with patch.object(MainWindow, '_navigator_dock_mode', 'float'):
        select(a, 16)
        app.processEvents()
        assert b.styleSheet() == original
        assert b._gui_font_size == 0
