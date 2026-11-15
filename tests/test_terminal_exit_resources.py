"""Exited tabs keep their output, without retaining live terminal I/O resources."""
import os
import sys
import time
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtWidgets import QApplication

from terminal_backend import IS_WINDOWS, create_backend
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture(scope='module')
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def pump_until(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert predicate()


@pytest.mark.skipif(IS_WINDOWS, reason='Unix PTY resources')
@pytest.mark.parametrize('reader_parse', [False, True])
def test_natural_exit_releases_io_but_preserves_final_output(app, monkeypatch, reader_parse):
    monkeypatch.setattr(TerminalWidget, 'PARSE_ON_READER_THREAD', reader_parse)
    widget = TerminalWidget()
    ended = []
    widget.session_ended.connect(lambda: ended.append(True))
    try:
        for attempt in range(3):
            ended.clear()
            ready, final = f'READY_{attempt}', f'FINAL_OUTPUT_{attempt}'
            widget.start_process([sys.executable, '-u', '-c',
                                  f'import sys; print({ready!r}); '
                                  f'sys.stdin.readline(); print({final!r})'])
            backend = widget._backend
            pump_until(app, lambda: ready in '\n'.join(widget.screen.display))
            widget.send_text('exit\r')
            writer = backend._input_writer
            assert writer is not None
            pump_until(app, lambda: bool(ended))
            assert not writer.thread.is_alive(), 'exited tab retained its input thread'
            assert backend._master_fd is None
            assert backend._wake_r is None and backend._wake_w is None
            assert backend._child_pid is None
            assert widget._backend is backend and widget.has_started()
            assert final in '\n'.join(widget.screen.display)
    finally:
        widget.cleanup()
        widget.deleteLater()


@pytest.mark.skipif(IS_WINDOWS, reason='Unix child reaping')
@pytest.mark.parametrize('idle_exit', [False, True])
def test_reaped_child_is_forgotten_before_exit_callback(idle_exit):
    backend = create_backend()
    backend._child_pid = 12345
    backend._running = True
    seen = []
    backend.on_exit = lambda status: seen.append((status, backend._child_pid))
    try:
        with patch('terminal_backend.select.select', return_value=([], [], [])), \
                patch('terminal_backend.os.waitpid', return_value=(12345, 0)), \
                patch('terminal_backend.os.kill') as kill:
            if idle_exit:
                backend._read_loop()
            else:
                backend._reap_child(wait=True)
            backend.stop()
        assert seen == [(0, None)]
        kill.assert_not_called()
    finally:
        backend._child_pid = None
        backend.stop()
