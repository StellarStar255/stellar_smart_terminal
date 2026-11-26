"""Navigation output markers through real window, terminal and delegate entry points."""
import os
import sys
import time
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication

from main_window import MainWindow
from window_navigator import NAV_ATTENTION_ROLE, NAV_OUTPUT_ROLE, WindowNavigatorPanel


_APP = None


def wait_for(check):
    deadline = time.monotonic() + 5
    while not check() and time.monotonic() < deadline:
        QTest.qWait(20)
    assert check()


@pytest.fixture
def panels():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    first, second = MainWindow(), MainWindow()
    for window in (first, second):
        window.show()
    floating = WindowNavigatorPanel()
    embedded = WindowNavigatorPanel(parent=second, embedded=True, pin_to=second)
    with patch.object(MainWindow, '_global_window_navigator', floating):
        for nav in (floating, embedded):
            nav.show()
            nav._force_refresh()
        _APP.processEvents()
        try:
            yield first, second, floating, embedded
        finally:
            for nav in (floating, embedded):
                nav.hide()
                nav.deleteLater()
            for window in (first, second):
                window._force_closing = True
                window.close()
                window.deleteLater()
            _APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            _APP.processEvents()


def row(nav, window):
    listing = nav.window_list
    return next(listing.item(i) for i in range(listing.count())
                if listing.item(i).data(Qt.ItemDataRole.UserRole) == id(window))


def test_window_output_aggregates_and_follows_moving_pane(panels):
    first, second, floating, embedded = panels
    first._add_new_tab(tab_name='moving')
    moving = first.terminal
    staying = first.tab_terminals[0][0]
    changes = QSignalSpy(first.navigator_output_changed)
    original = row(floating, first)
    staying._process_output_text('one')
    moving._process_output_text('two')
    moving._process_output_text('more')
    assert len(changes) == 1
    assert row(floating, first) is original
    for nav in (floating, embedded):
        assert row(nav, first).data(NAV_OUTPUT_ROLE)
        assert not row(nav, second).data(NAV_OUTPUT_ROLE)
    assert second._move_pane_next_to(first, moving, second.terminal, Qt.Orientation.Horizontal, False)
    for nav in (floating, embedded):
        assert row(nav, first).data(NAV_OUTPUT_ROLE)
        assert row(nav, second).data(NAV_OUTPUT_ROLE)
    staying.stop_process()
    for nav in (floating, embedded):
        assert not row(nav, first).data(NAV_OUTPUT_ROLE)
        assert row(nav, second).data(NAV_OUTPUT_ROLE)
    moving.cleanup()
    assert not row(floating, second).data(NAV_OUTPUT_ROLE)
    assert not row(embedded, second).data(NAV_OUTPUT_ROLE)


def test_real_output_pulses_then_settles_and_green_takes_priority(panels, tmp_path):
    first, _, nav, _ = panels
    terminal = first.terminal
    listing = nav.window_list
    frames = QSignalSpy(listing._pulse_timer.timeout)
    release = tmp_path / 'release'
    program = ("import pathlib,time\nprint('start',flush=True)\n"
               "while not pathlib.Path('release').exists():\n"
               "    time.sleep(.25)\n    print('work',flush=True)\n"
               "time.sleep(10)")
    terminal.start_process([sys.executable, '-u', '-c', program], cwd=str(tmp_path))
    wait_for(lambda: bool(row(nav, first).data(NAV_OUTPUT_ROLE)))
    listing.scrollToItem(row(nav, first))

    def center_pixel():
        rect = listing.visualItemRect(row(nav, first))
        image = listing.viewport().grab().toImage()
        ratio = image.devicePixelRatio()
        x = min(rect.right(), listing.viewport().rect().right()) - 14
        return image.pixelColor(int(x * ratio), int(rect.center().y() * ratio))

    wait_for(lambda: len(frames) >= 2)
    pixels = []
    for _ in range(8):
        pixels.append(center_pixel().blue())
        QTest.qWait(100)
    assert max(pixels) - min(pixels) >= 12
    nav.search_input.setText('absent-from-the-window-list')
    QTest.qWait(180)
    assert not listing._pulse_timer.isActive()
    nav.search_input.clear()
    wait_for(listing._pulse_timer.isActive)
    nav.hide()
    assert not listing._pulse_timer.isActive()
    nav.show()
    wait_for(listing._pulse_timer.isActive)
    with patch('main_window.play_notify_sound'):
        first._request_nav_attention(terminal)
    wait_for(lambda: bool(row(nav, first).data(NAV_ATTENTION_ROLE)))
    assert center_pixel() == QColor('#2ecc71')
    QTest.qWait(180)
    assert not listing._pulse_timer.isActive()
    first._clear_nav_attention()
    wait_for(listing._pulse_timer.isActive)
    release.touch()
    QTest.qWait(450)
    assert row(nav, first).data(NAV_OUTPUT_ROLE)
    wait_for(lambda: not row(nav, first).data(NAV_OUTPUT_ROLE))
    assert terminal.is_running()
    assert not row(nav, first).data(NAV_ATTENTION_ROLE)
    QTest.qWait(180)
    assert not listing._pulse_timer.isActive()


def test_attention_changes_preserve_rows_scroll_and_other_window_pulse(panels):
    first, second, nav, embedded = panels
    listing = nav.window_list
    listing.setFixedHeight(32)
    _APP.processEvents()
    last = listing.item(listing.count() - 1)
    listing.scrollToItem(last, listing.ScrollHint.PositionAtTop)
    position = listing.verticalScrollBar().value()
    assert position > 0
    first.terminal._process_output_text('running')
    second.terminal._process_output_text('also running')
    original = row(nav, first)
    with patch('main_window.play_notify_sound'):
        first._request_nav_attention(first.terminal)
    for panel in (nav, embedded):
        assert row(panel, first).data(NAV_ATTENTION_ROLE)
        assert row(panel, second).data(NAV_OUTPUT_ROLE)
    assert row(nav, first) is original
    assert listing.verticalScrollBar().value() == position
    first._clear_nav_attention()
    assert row(nav, first) is original
    assert listing.verticalScrollBar().value() == position
    assert not row(embedded, first).data(NAV_ATTENTION_ROLE)
    with patch.object(nav, '_refresh_window_list', wraps=nav._refresh_window_list) as refresh:
        nav._check_and_refresh()
        refresh.assert_not_called()
