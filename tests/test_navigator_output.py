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


def start_output_process(terminal, program, cwd=None):
    interpreter = sys.executable
    if os.name == 'nt':
        # The Windows venv launcher and pytest capture can redirect stdout.
        # Exercise the attached pseudoconsole, as a console application would.
        interpreter = sys._base_executable
        program = "import sys\nsys.stdout = open('CONOUT$', 'w', buffering=1)\n" + program
    terminal.start_process([interpreter, '-u', '-c', program], cwd=cwd)


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
        floating.select_window(second)
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
    start_output_process(terminal, program, cwd=str(tmp_path))
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
    wait_for(lambda: 'work' in '\n'.join(terminal.screen.display))
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
    nav.select_window(second if active is first else first)
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
    start_output_process(source.terminal, program)
    wait_for(lambda: bool(row(nav, source).data(NAV_OUTPUT_ROLE)))
    QTest.qWait(200)
    assert len(ticks) == 0
    # Minimizing the host can bring the output-producing window to the front.
    assert other.window_list._pulse_timer.isActive() == (other.window_list.viewed_window_id != id(source))
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


def test_viewed_window_suppresses_only_output_marker(panels):
    first, second, floating, embedded = panels
    _APP.setActiveWindow(first)
    def marker_color():
        listing = floating.window_list
        rect = listing.visualItemRect(row(floating, first))
        image = listing.viewport().grab().toImage()
        x = min(rect.right(), listing.viewport().rect().right()) - 14
        ratio = image.devicePixelRatio()
        return image.pixelColor(int(x * ratio), int(rect.center().y() * ratio))

    idle_color = marker_color()
    first.terminal._process_output_text('working')
    assert marker_color() == idle_color
    assert not floating.window_list._pulse_timer.isActive()
    assert embedded.window_list._pulse_timer.isActive()
    with patch('main_window.play_notify_sound'):
        first._request_nav_attention(first.terminal)
    assert marker_color() == QColor('#2ecc71')
    first._clear_nav_attention()
    _APP.setActiveWindow(second)
    assert floating.window_list._pulse_timer.isActive()
    first.terminal.stop_process()
    second.terminal._process_output_text('still working')
    assert not floating.window_list._pulse_timer.isActive()
    assert not embedded.window_list._pulse_timer.isActive()
    floating.select_window(first)
    assert floating.window_list._pulse_timer.isActive()


def test_output_dot_mutes_target_pane_or_window_and_restores_choices(panels):
    from PyQt6.QtGui import QContextMenuEvent
    from PyQt6.QtWidgets import QMenu
    import app_config
    from i18n import t

    first, second, nav, embedded = panels
    monitor = first.terminal
    first._add_new_tab(tab_name='other')
    other = first.terminal
    monitor._process_output_text('GPU usage')
    second.terminal._process_output_text('other window')

    def click_action(key, expected):
        def choose(menu, *_):
            action = next(a for a in menu.actions() if a.text() == t(key))
            assert action.isChecked() is expected
            action.trigger()
            return action
        return patch.object(QMenu, 'exec', choose)

    def toggle_window(expected):
        with click_action('window.output_marker', expected):
            pos = nav.window_list.visualItemRect(row(nav, first)).center()
            nav._show_window_context_menu(pos)

    def check_first(active):
        for panel in (nav, embedded):
            assert row(panel, first).data(NAV_OUTPUT_ROLE) is active
            assert row(panel, second).data(NAV_OUTPUT_ROLE)

    with click_action('ctx.terminal_output_marker', True):
        monitor.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5)))
    monitor._process_output_text('GPU usage again')
    check_first(False)
    other._process_output_text('important job')
    check_first(True)
    toggle_window(True)
    check_first(False)
    with patch('main_window.play_notify_sound'):
        first._request_nav_attention(other)
    assert row(nav, first).data(NAV_ATTENTION_ROLE)
    first._clear_nav_attention()
    for panel in (nav, embedded):
        panel._force_refresh()
    check_first(False)
    first._write_workspace_snapshot()
    entries = app_config.read_config()['workspace_snapshot']['windows']
    saved = next(entry for entry in entries if entry['window_id'] == first._workspace_id)
    assert saved['output_marker_enabled'] is False
    assert saved['tabs'][0]['output_marker_enabled'] is False
    assert next(e for e in entries if e['window_id'] == second._workspace_id)['output_marker_enabled']
    toggle_window(False)
    check_first(True)
    other.stop_process()
    check_first(False)
    monitor.set_output_marker_enabled(True)
    check_first(True)
    first._apply_restored_window_state(dict(saved, current_tab=0))
    check_first(False)
    assert not monitor.output_marker_enabled
    assert not first.window_output_marker_enabled


def test_muted_output_pane_keeps_preference_across_window_move(panels):
    source, target, floating, embedded = panels
    source._add_new_tab(tab_name='monitor')
    monitor = source.terminal
    monitor._process_output_text('GPU status')
    monitor.set_output_marker_enabled(False)
    target.set_window_output_marker_enabled(False)
    assert target._move_pane_next_to(source, monitor, target.terminal, Qt.Orientation.Horizontal, False)
    target.set_window_output_marker_enabled(True)
    monitor._process_output_text('next GPU status')
    for panel in (floating, embedded):
        assert not row(panel, source).data(NAV_OUTPUT_ROLE)
        assert not row(panel, target).data(NAV_OUTPUT_ROLE)
    monitor.set_output_marker_enabled(True)
    for panel in (floating, embedded):
        assert row(panel, target).data(NAV_OUTPUT_ROLE)
        assert not row(panel, source).data(NAV_OUTPUT_ROLE)


def test_output_dot_actions_on_tab_and_split_headers(panels):
    from PyQt6.QtWidgets import QMenu
    from i18n import t
    win = panels[0]
    first = win.terminal
    bar = win.tab_widget.tabBar()

    def choose(menu, *_):
        action = next(action for action in menu.actions() if action.text() == t(key))
        action.trigger()

    key = 'ctx.terminal_output_marker'
    with patch.object(QMenu, 'exec', choose):
        win._show_tab_context_menu(bar.tabRect(0).center())
    assert not first.output_marker_enabled
    win._split_current_tab()
    first, second = win.tab_terminals[0]
    key = 'pane.output_marker'
    with patch.object(QMenu, 'exec', choose):
        win._show_pane_menu(second, QPoint(20, 20))
    assert not first.output_marker_enabled and not second.output_marker_enabled
    win._add_new_tab(tab_name='unrelated')
    unrelated = win.terminal

    def choose_split(menu, *_):
        dots = next(a.menu() for a in menu.actions() if a.text() == t('tab.output_markers'))
        dots.actions()[choice].trigger()

    for choice, states in [(-1, (False, True)), (0, (False, False)), (0, (True, True))]:
        with patch.object(QMenu, 'exec', choose_split):
            win._show_tab_context_menu(bar.tabRect(0).center())
        assert (first.output_marker_enabled, second.output_marker_enabled) == states
        assert unrelated.output_marker_enabled
