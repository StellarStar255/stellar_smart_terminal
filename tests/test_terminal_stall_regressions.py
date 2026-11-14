"""GUI progress while output/reflow owns the screen, and transient PTY reads."""
import os
import threading
import time
from contextlib import contextmanager
from unittest.mock import Mock, patch

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
