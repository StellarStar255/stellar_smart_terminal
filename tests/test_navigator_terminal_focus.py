"""Clicking a navigator hands keyboard input to the destination's active pane."""
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from main_window import MainWindow
from window_navigator import WindowNavigatorPanel

_APP = None


@pytest.fixture
def windows():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    wins = [MainWindow(), MainWindow()]
    for w in wins:
        w.show()
    _APP.processEvents()
    yield _APP, wins
    for w in wins:
        w._force_closing = True
        w.close()
        w.deleteLater()
    for _ in range(3):
        _APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _APP.processEvents()


def click_window(nav, target):
    nav._force_refresh()
    item = next(nav.window_list.item(i) for i in range(nav.window_list.count())
                if nav.window_list.item(i).data(Qt.ItemDataRole.UserRole) == id(target))
    nav.window_list.scrollToItem(item)
    QTest.mouseClick(nav.window_list.viewport(), Qt.MouseButton.LeftButton,
                    pos=nav.window_list.visualItemRect(item).center())
    QApplication.processEvents()


def assert_keyboard_reaches(term):
    assert QApplication.focusWidget() is term
    backend = SimpleNamespace(is_running=True)
    with patch.object(term, '_backend', backend), patch.object(term, '_write_to_backend', return_value=True) as write:
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_A)
    write.assert_called_once_with(b'a')


def test_click_current_window_releases_navigator_keyboard_focus(windows):
    app, (a, _) = windows
    a.activateWindow()
    app.processEvents()
    a.nav_panel.window_list.setFocus()
    click_window(a.nav_panel, a)
    assert_keyboard_reaches(a.terminal)


def test_cross_window_click_uses_active_split_not_first_terminal(windows):
    app, (a, b) = windows
    idx = b.tab_widget.currentIndex()
    first = b.terminal
    second = b._create_terminal()
    b.tab_splitters[idx].addWidget(second)
    b.tab_terminals[idx].append(second)
    b.active_terminal = second
    b.activateWindow()
    second.setFocus()
    app.processEvents()
    b.working_dir_combo.setFocus()
    a.activateWindow()
    app.processEvents()
    click_window(a.nav_panel, b)
    assert b.active_terminal is second
    assert_keyboard_reaches(second)
    assert not first.hasFocus()


def test_minimized_window_is_restored_before_focus(windows):
    app, (a, b) = windows
    b.showMinimized()
    app.processEvents()
    a.nav_panel._switch_to_window(b)
    app.processEvents()
    assert not b.isMinimized()
    assert_keyboard_reaches(b.terminal)


def test_floating_navigator_hands_focus_to_terminal(windows):
    app, (a, b) = windows
    nav = WindowNavigatorPanel()
    try:
        nav.show()
        app.processEvents()
        nav.window_list.setFocus()
        click_window(nav, b)
        assert_keyboard_reaches(b.terminal)
    finally:
        nav.close()
        nav.deleteLater()


def test_rapid_switch_ignores_outdated_focus_callback(windows):
    app, (a, b) = windows
    a.nav_panel._switch_to_window(b)
    a.nav_panel._switch_to_window(a)
    app.processEvents()
    assert_keyboard_reaches(a.terminal)
    assert not b.terminal.hasFocus()


def test_deferred_focus_does_not_steal_back_from_another_window(windows):
    app, (a, b) = windows
    a.nav_panel._switch_to_window(b)
    a.activateWindow()
    app.processEvents()
    a.working_dir_combo.setFocus()
    app.processEvents()
    assert app.activeWindow() is a
    assert not b.terminal.hasFocus()
