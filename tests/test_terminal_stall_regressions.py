"""GUI progress while output/reflow owns the screen, and transient PTY reads."""
import os
import threading
import time
from contextlib import contextmanager
from unittest.mock import Mock, patch
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtWidgets import QApplication

from terminal_backend import coalesce_reads
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture(scope='module')
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@contextmanager
def busy_screen(widget):
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with widget._screen_lock:
            acquired.set()
            release.wait(1)

    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(1)
    try:
        yield
    finally:
        release.set()
        thread.join(2)


@pytest.mark.parametrize('cached', [False, True])
def test_paint_keeps_gui_responsive_and_retries_after_busy_screen(app, cached):
    widget = TerminalWidget()
    widget.resize(800, 480)
    widget._update_terminal_size()
    try:
        if cached:
            widget.grab()
            assert widget._cache_valid
        widget._invalidate_render_cache()
        with busy_screen(widget):
            start = time.monotonic()
            widget.grab()
            elapsed = time.monotonic() - start
            assert elapsed < .2, f'paint waited {elapsed:.3f}s for output parsing'
            assert widget._content_dirty
            assert not widget._cache_valid
        widget._conditional_update()
        widget.grab()
        assert widget._cache_valid
    finally:
        widget.cleanup()
        widget.deleteLater()


def test_repeated_resize_does_not_wait_for_inflight_reflow(app):
    widget = TerminalWidget()
    widget.resize(800, 480)
    widget._update_terminal_size()
    try:
        widget._reflow_inflight = True
        with busy_screen(widget):
            # Change cell metrics so the next size calculation has a new target,
            # without depending on delivery of a hidden widget's resize event.
            old_cols = widget.term_cols
            widget.term_cols = old_cols - 10
            start = time.monotonic()
            widget._update_terminal_size()
            elapsed = time.monotonic() - start
            assert elapsed < .2, f'resize waited {elapsed:.3f}s for reflow'
            assert widget.term_cols == old_cols
    finally:
        widget._reflow_inflight = False
        widget.cleanup()
        widget.deleteLater()


@pytest.mark.parametrize('scaling', [False, True])
def test_cached_tui_selection_paint_does_not_wait_for_screen(app, scaling):
    widget = TerminalWidget()
    widget.resize(800, 480)
    widget._update_terminal_size()
    try:
        widget.stream.feed('\x1b[?1049h\x1b[?1000hselected text')
        widget._mouse_mode = True
        widget._begin_tui_selection()
        widget._selection_start = (0, 0)
        widget._selection_end = (0, 3)
        widget.grab()
        widget._reflow_scaling = scaling
        with busy_screen(widget):
            start = time.monotonic()
            widget.grab()
            assert time.monotonic() - start < .2
        widget._reflow_scaling = False
        widget.grab()
    finally:
        widget.cleanup()
        widget.deleteLater()


def test_first_resize_queues_reflow_without_waiting_for_parser(app):
    widget = TerminalWidget()
    widget.resize(800, 480)
    widget._update_terminal_size()
    try:
        widget.term_cols -= 10
        with busy_screen(widget), patch.object(widget, '_start_async_reflow') as submit:
            start = time.monotonic()
            widget._update_terminal_size()
            assert time.monotonic() - start < .2
            submit.assert_called_once()
    finally:
        widget.cleanup()
        widget.deleteLater()


def test_search_retries_latest_query_without_waiting_for_screen(app):
    widget = TerminalWidget()
    widget.resize(800, 480)
    widget._update_terminal_size()
    try:
        widget._show_search_bar()
        widget.stream.feed('latest needle')
        widget._search_pending_text = 'obsolete'
        with busy_screen(widget):
            start = time.monotonic()
            widget._perform_search()
            assert time.monotonic() - start < .2
            assert widget._search_debounce_timer.isActive()
            widget._search_pending_text = 'needle'
        deadline = time.monotonic() + 2
        while not widget._search_matches and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert widget._search_matches == [(0, 7, 6)]
        assert not widget._search_debounce_timer.isActive()
    finally:
        widget.cleanup()
        widget.deleteLater()


def test_idle_notification_waits_without_blocking_or_losing_prompt(app):
    widget = TerminalWidget()
    seen = []
    widget.interaction_requested.connect(lambda: seen.append('prompt'))
    try:
        widget.stream.feed('\x1b[30;1HContinue? [y/n]')
        widget._had_output_activity = True
        widget._activity_bytes = 20
        with busy_screen(widget):
            start = time.monotonic()
            widget._on_activity_settled()
            assert time.monotonic() - start < .2
            assert widget._had_output_activity
            assert not seen
        widget._on_activity_settled()
        assert seen == ['prompt']
        assert not widget._had_output_activity
    finally:
        widget.cleanup()
        widget.deleteLater()


@pytest.mark.skipif(os.name == 'nt', reason='Unix cwd fallback')
def test_cwd_fallback_runs_off_gui_and_coalesces_repeated_calls(app):
    widget = TerminalWidget()
    widget._backend = SimpleNamespace(is_running=True, _child_pid=123)
    entered, release = threading.Event(), threading.Event()
    worker_threads = []

    def lsof(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        entered.set()
        release.wait(1)
        return SimpleNamespace(returncode=0, stdout='p123\nn/example/cwd\n')

    try:
        with patch('terminal_widget._cwd_via_native', return_value=None), \
                patch('subprocess.run', side_effect=lsof) as run:
            start = time.monotonic()
            assert widget.get_cwd() is None
            assert time.monotonic() - start < .2
            assert entered.wait(1)
            for _ in range(10):
                assert widget.get_cwd() is None
            assert run.call_count == 1
            assert len(worker_threads) == 1
            assert worker_threads[0] != threading.get_ident()
            release.set()
            assert widget._cwd_future.result(timeout=2) == '/example/cwd'
            assert widget.get_cwd() == '/example/cwd'
            assert run.call_count == 1
    finally:
        release.set()
        widget._backend = None
        widget.cleanup()
        widget.deleteLater()


@pytest.mark.skipif(os.name == 'nt', reason='Unix cwd fallback')
def test_cwd_native_avoids_network_stat_and_discards_previous_session_cache(app):
    widget = TerminalWidget()
    try:
        widget._backend = SimpleNamespace(is_running=True, _child_pid=123)
        with patch('terminal_widget._cwd_via_native', return_value='/network/cwd'), \
                patch('os.path.isdir', side_effect=AssertionError('GUI must not stat cwd')):
            assert widget.get_cwd() == '/network/cwd'
        widget._backend = SimpleNamespace(is_running=True, _child_pid=456)
        with patch('terminal_widget._cwd_via_native', return_value=None), \
                patch('terminal_widget._cwd_via_lsof', return_value=None):
            assert widget.get_cwd() is None
    finally:
        widget._backend = None
        widget.cleanup()
        widget.deleteLater()


def test_late_reflow_result_cannot_restart_work_after_cleanup(app):
    widget = TerminalWidget()
    widget.cleanup()
    widget.term_cols -= 10
    try:
        with patch.object(widget, '_start_async_reflow') as submit:
            widget._on_reflow_finished(100, 10)
            submit.assert_not_called()
    finally:
        widget.deleteLater()


@pytest.mark.skipif(os.name == 'nt', reason='Unix backend lifecycle')
def test_closing_terminal_cancels_reader_waiting_for_reflow_lock(app):
    from terminal_backend import UnixBackend

    widget = TerminalWidget()
    backend = UnixBackend()
    widget._backend = backend
    started = threading.Event()

    def receive():
        started.set()
        widget._on_output(b'output waiting for screen')

    reader = threading.Thread(target=receive)
    backend._reader_thread = reader
    backend._running = True
    try:
        with busy_screen(widget):
            reader.start()
            assert started.wait(1)
            time.sleep(.03)
            start = time.monotonic()
            widget.stop_process()
            assert time.monotonic() - start < .2
            assert not reader.is_alive()
        assert 'output waiting' not in ''.join(widget.screen.display)
    finally:
        reader.join(2)
        backend.stop()
        widget._backend = None
        widget.cleanup()
        widget.deleteLater()


def test_late_gui_callbacks_cannot_revive_cleaned_terminal(app):
    widget = TerminalWidget()
    widget.cleanup()
    try:
        widget._on_output_activity(100)
        widget._on_output(b'late output')
        widget._update_terminal_size()
        assert not widget._activity_idle_timer.isActive()
        assert not widget._reflow_inflight
        assert 'late output' not in ''.join(widget.screen.display)
    finally:
        widget.deleteLater()


def test_reflow_completion_retries_without_waiting_for_new_output(app):
    widget = TerminalWidget()
    try:
        widget._reflow_inflight = True
        widget._reflow_scaling = True
        with busy_screen(widget):
            start = time.monotonic()
            widget._on_reflow_finished(0, 0)
            assert time.monotonic() - start < .2
            assert widget._reflow_inflight
        deadline = time.monotonic() + 2
        while widget._reflow_inflight and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert not widget._reflow_inflight
        assert not widget._reflow_scaling
    finally:
        widget.cleanup()
        widget.deleteLater()


def test_url_hover_does_not_wait_for_background_screen_lock(app):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    widget = TerminalWidget()
    widget.resize(800, 480)
    widget._update_terminal_size()
    widget.stream.feed('https://example.com/path')
    pos = QPointF(widget.PADDING + widget.char_width,
                  widget.PADDING + widget._header_h + widget.char_height / 2)
    event = QMouseEvent(QEvent.Type.MouseMove, pos, Qt.MouseButton.NoButton,
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.AltModifier)
    try:
        with busy_screen(widget):
            start = time.monotonic()
            widget.mouseMoveEvent(event)
            assert time.monotonic() - start < .2
            assert not widget._hover_cursor_on
        widget.mouseMoveEvent(event)
        assert widget._hover_cursor_on
    finally:
        widget.cleanup()
        widget.deleteLater()


@pytest.mark.parametrize('operation', ['clear_screen', 'clear_scrollback'])
def test_clear_operations_retry_without_waiting_for_screen_lock(app, operation):
    widget = TerminalWidget()
    widget.stream.feed(''.join(f'line {i}\r\n' for i in range(150)) + 'visible marker')
    before = '\n'.join(widget.screen.display)
    assert widget._get_history_count() > 0
    try:
        with busy_screen(widget):
            start = time.monotonic()
            getattr(widget, operation)()
            assert time.monotonic() - start < .2
            assert '\n'.join(widget.screen.display) == before
        deadline = time.monotonic() + 2
        if operation == 'clear_screen':
            done = lambda: 'visible marker' not in '\n'.join(widget.screen.display)
        else:
            done = lambda: widget._get_history_count() == 0
        while not done() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert done()
        if operation == 'clear_scrollback':
            assert '\n'.join(widget.screen.display) == before
    finally:
        widget.cleanup()
        widget.deleteLater()


@pytest.mark.parametrize('error', [BlockingIOError(), InterruptedError()])
def test_coalescing_delivers_prefix_after_transient_read_error(error):
    def read_more():
        raise error

    assert coalesce_reads(b'already read', lambda timeout: True, read_more,
                          window=1, max_bytes=1024) == b'already read'


@pytest.mark.skipif(os.name == 'nt', reason='Unix PTY')
@pytest.mark.parametrize('error', [BlockingIOError(), InterruptedError()])
def test_reader_survives_transient_first_read(error):
    from terminal_backend import UnixBackend

    backend = UnixBackend()
    backend._master_fd = -1
    backend._running = True
    backend._reap_child = Mock()
    backend._read_master_once = Mock(side_effect=[error, b'continued', b''])
    backend._master_has_data = Mock(return_value=False)
    received = []
    backend.on_output = received.append
    try:
        with patch('terminal_backend.select.select', return_value=([-1], [], [])):
            backend._read_loop()
        assert received == [b'continued']
        assert backend._read_master_once.call_count == 3
    finally:
        backend._master_fd = None
        backend.stop()
