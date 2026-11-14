"""Slow native clipboard probes must leave the GUI responsive and preserve order."""
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtWidgets import QApplication
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def terminal():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    written = []
    widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True)
    widget._ensure_clipboard_watch = lambda: None
    widget._clipboard_change_token = lambda: 7
    yield widget, written
    widget._backend = None
    widget.cleanup()
    deadline = time.monotonic() + 2
    while getattr(widget, '_native_paste_process', None) is not None:
        assert time.monotonic() < deadline
        _APP.processEvents()
        time.sleep(.005)
    widget.deleteLater()
    _APP.processEvents()


def pump_until(done, timeout=2):
    deadline = time.monotonic() + timeout
    while not done():
        assert time.monotonic() < deadline, 'asynchronous probe did not finish'
        _APP.processEvents()
        time.sleep(.005)


def slow_probe(delay=.1):
    real_start = QProcess.start

    def start(process, program, arguments):
        assert program == 'osascript'
        return real_start(process, sys.executable,
                          ['-c', f'import time; time.sleep({delay}); print("NOTHING")'])
    return patch.object(QProcess, 'start', new=start)


def test_slow_probe_returns_immediately_pumps_gui_and_keeps_paste_order(terminal):
    widget, written = terminal
    clipboard1 = SimpleNamespace(text=lambda: 'first')
    clipboard2 = SimpleNamespace(text=lambda: 'second')
    heartbeats = []
    with slow_probe(.15):
        start = time.monotonic()
        widget._paste_from_clipboard_macos(clipboard1)
        widget._paste_from_clipboard_macos(clipboard2)
        assert time.monotonic() - start < .2
        assert not written
        QTimer.singleShot(0, lambda: heartbeats.append(True))
        _APP.processEvents()
        assert heartbeats
        assert not written
        pump_until(lambda: len(written) == 2)
    assert written == [b'first', b'second']
    assert not widget._native_paste_queue


@pytest.mark.parametrize('change', ['clipboard', 'backend', 'cleanup'])
def test_late_probe_does_not_paste_into_changed_context(terminal, change):
    widget, written = terminal
    clipboard = SimpleNamespace(text=Mock(return_value='stale'))
    with slow_probe(.15):
        widget._paste_from_clipboard_macos(clipboard)
        path = widget._native_paste_queue[0][1]
        if change == 'clipboard':
            widget._on_clipboard_data_changed()
        elif change == 'backend':
            widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True)
        else:
            widget._backend = None
            start = time.monotonic()
            widget.cleanup()
            assert time.monotonic() - start < .2
        pump_until(lambda: widget._native_paste_process is None)
    assert not written
    clipboard.text.assert_not_called()
    assert not os.path.exists(path)


def test_timeout_kills_probe_without_blocking_gui_and_cleans_temp(terminal):
    widget, written = terminal
    with slow_probe(5):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'fallback'))
        path = widget._native_paste_queue[0][1]
        widget._native_paste_process.findChild(QTimer).start(30)
        pump_until(lambda: bool(written))
    assert written == [b'fallback']
    assert not os.path.exists(path)


def test_missing_script_falls_back_once_and_releases_process(terminal):
    widget, written = terminal
    real_start = QProcess.start

    def missing(process, program, arguments):
        real_start(process, '/nonexistent/stellar-paste-test', [])

    with patch.object(QProcess, 'start', new=missing):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'fallback'))
        pump_until(lambda: bool(written))
    assert written == [b'fallback']
    assert widget._native_paste_process is None
