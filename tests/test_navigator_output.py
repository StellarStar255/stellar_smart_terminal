"""Navigation output markers through real window, terminal and delegate entry points."""
import os
import sys
import time
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, QModelIndex, QPoint, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication

from main_window import MainWindow
from utils import get_config_path
from window_navigator import NAV_ATTENTION_ROLE, NAV_OUTPUT_ROLE, WindowNavigatorPanel


_APP = None


def wait_for(check):
    deadline = time.monotonic() + 5
    while not check() and time.monotonic() < deadline:
        QTest.qWait(20)
    assert check()


@pytest.fixture
def panels(tmp_path, monkeypatch):
    global _APP
    monkeypatch.setenv('STELLAR_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(MainWindow, 'CONFIG_FILE', get_config_path())
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


def test_pulse_tracks_reordered_row_font_size_and_scroll(panels):
    first, second, nav, _ = panels
    listing = nav.window_list
    listing.setFixedHeight(160)
    _APP.processEvents()
    first.terminal._process_output_text('working')
    item = row(nav, first)
    src = listing.row(item)
    dst = listing.count() if src == 0 else 0
    assert listing.model().moveRow(QModelIndex(), src, QModelIndex(), dst)
    _APP.processEvents()

    assert listing.row(row(nav, first)) != src

    def marker_center():
        rect = listing.visualItemRect(row(nav, first))
        return QPoint(min(rect.right(), listing.viewport().rect().right()) - 14, rect.center().y())

    assert listing._pulse_region.contains(marker_center())
    nav._font_size = 24
    nav._apply_list_font_size()
    _APP.processEvents()
    assert listing._pulse_region.contains(marker_center())
    listing.setFixedHeight(listing.sizeHintForRow(0) + listing.horizontalScrollBar().sizeHint().height()
                           + 2 * listing.frameWidth())
    listing.scrollToItem(row(nav, second), listing.ScrollHint.PositionAtTop)
    _APP.processEvents()
    assert not listing.viewport().rect().contains(marker_center())
    assert not listing._pulse_timer.isActive()
    listing.scrollToItem(row(nav, first), listing.ScrollHint.PositionAtTop)
    _APP.processEvents()
    assert listing._pulse_timer.isActive()
    assert listing._pulse_region.contains(marker_center())


def test_pulse_stops_when_only_edge_of_active_row_is_visible(panels):
    first, second, nav, _ = panels
    listing = nav.window_list
    listing.setVerticalScrollMode(listing.ScrollMode.ScrollPerPixel)
    listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    listing.setFixedHeight(listing.sizeHintForRow(0) + 2 * listing.frameWidth())
    _APP.processEvents()
    active = next(w for w in (first, second) if row(nav, w) is listing.item(0))
    active.terminal._process_output_text('working')
    rect = listing.visualItemRect(row(nav, active))
    listing.verticalScrollBar().setValue(listing.verticalScrollBar().value() + rect.bottom() - 1)
    _APP.processEvents()
    rect = listing.visualItemRect(row(nav, active))
    assert rect.intersects(listing.viewport().rect()) and rect.center().y() + 5 < 0
    assert not listing._pulse_timer.isActive()
    listing.verticalScrollBar().setValue(0)
    assert listing._pulse_timer.isActive()


@pytest.mark.parametrize('embedded_panel', [False, True], ids=['hide-floating-panel', 'minimize-host-window'])
def test_background_navigator_tracks_new_output_without_animating(panels, embedded_panel):
    source, host, floating, embedded = panels
    nav = embedded if embedded_panel else floating
    conceal = host.showMinimized if embedded_panel else floating.hide
    restore = host.showNormal if embedded_panel else floating.show
    concealed = host.isMinimized if embedded_panel else floating.isHidden
    other = floating if embedded_panel else embedded
    conceal()
    wait_for(concealed)
    _APP.processEvents()
    ticks = QSignalSpy(nav.window_list._pulse_timer.timeout)
    program = "import time\nwhile True:\n print('working', flush=True)\n time.sleep(.05)"
    source.terminal.start_process([sys.executable, '-u', '-c', program])
    wait_for(lambda: bool(row(nav, source).data(NAV_OUTPUT_ROLE)))
    QTest.qWait(200)
    assert len(ticks) == 0
    assert other.window_list._pulse_timer.isActive()
    assert not nav.window_list._pulse_timer.isActive()
    restore()
    wait_for(nav.window_list._pulse_timer.isActive)
    conceal()
    wait_for(concealed)
    wait_for(lambda: not nav.window_list._pulse_timer.isActive())
    source.terminal.stop_process()
    restore()
    _APP.processEvents()
    assert not row(nav, source).data(NAV_OUTPUT_ROLE)
    assert not nav.window_list._pulse_timer.isActive()


def test_split_pane_key_acknowledges_only_its_own_navigation_alert(panels):
    window, _, floating, embedded = panels
    window._split_current_tab()
    left, right = window.tab_terminals[window.tab_widget.currentIndex()]
    attention = QSignalSpy(window.navigator_attention_changed)
    with patch('main_window.play_notify_sound') as sound:
        left._process_output_text('left\a')
        right._process_output_text('right\a')
        right._process_output_text('more work')
        QTest.keyClick(left, Qt.Key.Key_Shift)
        for panel in (embedded, floating):
            assert row(panel, window).data(NAV_ATTENTION_ROLE)
            assert not panel.window_list._pulse_timer.isActive()
        QTest.keyClick(right, Qt.Key.Key_Shift)
        for panel in (embedded, floating):
            assert not row(panel, window).data(NAV_ATTENTION_ROLE)
            assert panel.window_list._pulse_timer.isActive()
        assert list(attention) == [[True], [False]]
        sound.assert_called_once()


def test_terminal_key_preserves_overlapping_git_navigation_alert(panels):
    window, _, nav, _ = panels
    terminal = window.terminal
    with patch('main_window.play_notify_sound'):
        terminal._process_output_text('waiting\a')
        window._request_nav_attention()  # Git completion
        QTest.keyClick(terminal, Qt.Key.Key_Shift)
        assert row(nav, window).data(NAV_ATTENTION_ROLE)
        with patch.object(window, 'isActiveWindow', return_value=True):
            window._add_new_tab(tab_name='reviewed')
        assert not row(nav, window).data(NAV_ATTENTION_ROLE)


def test_queued_terminal_alerts_are_discarded_when_window_closes(panels):
    from threading import Thread

    window = MainWindow()
    terminal = window.terminal

    def notify():
        terminal.interaction_requested.emit()
        terminal.attention_requested.emit()
        terminal.alert_matched.emit('ERROR')

    with patch('main_window.play_notify_sound') as sound:
        worker = Thread(target=notify)
        worker.start()
        worker.join()
        window._force_closing = True
        window.close()
        window.deleteLater()
        _APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _APP.processEvents()
        sound.assert_not_called()
