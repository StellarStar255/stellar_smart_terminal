"""Explorer parent navigation uses real Qt shortcut dispatch and stays focus-scoped."""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from explorer_widget import ExplorerPanel


@pytest.fixture
def explorer(tmp_path):
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    layout = QVBoxLayout(host)
    panel = ExplorerPanel(theme={})
    other = QLineEdit()
    layout.addWidget(panel)
    layout.addWidget(other)
    child = tmp_path / 'project' / 'child'
    child.mkdir(parents=True)
    panel.set_root_path(str(child))
    host.show()
    host.activateWindow()
    app.processEvents()
    yield app, host, panel, other, child
    panel.model.setRootPath('')
    host.close()
    host.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def press_up(app, target, modifiers=None):
    target.setFocus()
    app.processEvents()
    assert app.focusWidget() is target
    if modifiers is None:
        modifiers = (Qt.KeyboardModifier.ControlModifier if sys.platform == 'darwin'
                     else Qt.KeyboardModifier.AltModifier)
    QTest.keyClick(target, Qt.Key.Key_Up, modifiers)
    app.processEvents()


@pytest.mark.parametrize('focus', ['tree_view', 'search_edit'])
def test_shortcut_navigates_to_parent(explorer, focus):
    app, host, panel, other, child = explorer
    press_up(app, getattr(panel, focus))
    assert os.path.realpath(panel._current_path) == os.path.realpath(child.parent)


def test_shortcut_does_not_navigate_from_another_panel(explorer):
    app, host, panel, other, child = explorer
    press_up(app, other)
    assert os.path.realpath(panel._current_path) == os.path.realpath(child)


def test_plain_up_does_not_navigate(explorer):
    app, host, panel, other, child = explorer
    press_up(app, panel.tree_view, Qt.KeyboardModifier.NoModifier)
    assert os.path.realpath(panel._current_path) == os.path.realpath(child)


def test_shortcut_at_filesystem_root_is_safe(explorer):
    app, host, panel, other, child = explorer
    root = os.path.abspath(os.sep)
    panel.set_root_path(root)
    press_up(app, panel.tree_view)
    assert os.path.realpath(panel._current_path) == os.path.realpath(root)
    assert not panel.up_btn.isEnabled()
