"""Slow native clipboard probes must leave the GUI responsive and preserve order."""
import os
import sys
import time
import threading
import re
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, QProcess, QTimer, Qt
from PyQt6.QtGui import QKeyEvent, QInputMethodEvent
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


def press(widget, key, text='', modifiers=Qt.KeyboardModifier.NoModifier):
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text))


def test_paste_then_enter_submits_pasted_command_and_records_correct_history(terminal):
    widget, written = terminal
    recorded = []
    widget.input_recorded.connect(recorded.append)
    with slow_probe(.1):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'echo hello'))
        press(widget, Qt.Key.Key_Return, '\r')
        assert not written
        pump_until(lambda: widget._native_paste_process is None)
    assert written == [b'echo hello', b'\r']
    assert recorded == ['echo hello']
    assert widget.input_buffer == ''


def test_interleaved_pastes_keyboard_and_programmatic_input_keep_order(terminal):
    widget, written = terminal
    with slow_probe(.1):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'one'))
        widget.send_text('+')
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'two'))
        press(widget, Qt.Key.Key_Exclam, '!')
        press(widget, Qt.Key.Key_Return, '\r')
        assert not written
        pump_until(lambda: widget._native_paste_process is None)
    assert written == [b'one', b'+', b'two', b'!', b'\r']
    assert widget.input_buffer == ''


def test_ime_commit_waits_for_paste_but_device_queries_reply_immediately(terminal):
    widget, written = terminal
    with slow_probe(.1):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'prefix'))
        event = QInputMethodEvent()
        event.setCommitString('中文')
        widget.inputMethodEvent(event)
        widget._on_output(b'\x1b[6n')
        assert written == [b'\x1b[1;1R']
        pump_until(lambda: widget._native_paste_process is None)
    assert written == [b'\x1b[1;1R', b'prefix', '中文'.encode()]
    assert widget.input_buffer == 'prefix中文'


def test_ctrl_c_cancels_pending_paste_and_buffered_enter(terminal):
    widget, written = terminal
    clipboard = SimpleNamespace(text=Mock(return_value='must not run'))
    with slow_probe(.2):
        widget._paste_from_clipboard_macos(clipboard)
        press(widget, Qt.Key.Key_Return, '\r')
        ctrl = (Qt.KeyboardModifier.MetaModifier if sys.platform == 'darwin'
                else Qt.KeyboardModifier.ControlModifier)
        press(widget, Qt.Key.Key_C, '\x03', ctrl)
        assert written == [b'\x03']
        pump_until(lambda: widget._native_paste_process is None)
    assert written == [b'\x03']
    clipboard.text.assert_not_called()


@pytest.mark.parametrize('change', ['clipboard_signal', 'clipboard_token', 'backend', 'stop'])
def test_obsolete_paste_cannot_leave_an_orphaned_enter_or_queued_paste(terminal, change):
    widget, written = terminal
    clipboard = SimpleNamespace(text=Mock(return_value='obsolete'))
    with slow_probe(.1):
        widget._paste_from_clipboard_macos(clipboard)
        press(widget, Qt.Key.Key_Return, '\r')
        widget._paste_from_clipboard_macos(clipboard)
        press(widget, Qt.Key.Key_Return, '\r')
        if change == 'clipboard_signal':
            widget._on_clipboard_data_changed()
        elif change == 'clipboard_token':
            widget._clipboard_change_token = lambda: 8
        elif change == 'backend':
            widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True)
        else:
            widget._backend.stop = lambda: None
            widget.stop_process()
        pump_until(lambda: widget._native_paste_process is None)
    assert written == []
    clipboard.text.assert_not_called()


def test_preset_command_waits_for_pending_paste(terminal):
    widget, written = terminal
    widget._backend.is_running = True
    with slow_probe(.1):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'prefix '))
        widget._execute_commands(['next'])
        assert not written
        pump_until(lambda: widget._native_paste_process is None)
    assert written == [b'prefix ', b'next\r']


def test_quick_command_remainder_does_not_follow_session_replacement(terminal):
    widget, written = terminal
    widget._backend.is_running = True
    new_write = Mock(return_value=True)
    widget.input_recorded.connect(lambda _: setattr(
        widget, '_backend', SimpleNamespace(is_running=True, write=new_write)))
    widget._execute_commands(['first', 'second'])
    assert written == [b'first\r']
    new_write.assert_not_called()


def test_queued_quick_command_does_not_restart_exited_process(terminal):
    widget, written = terminal
    widget._backend.is_running = True
    with slow_probe(.1), patch.object(widget, 'start_process') as start:
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: ''))
        widget._execute_commands(['next'])
        widget._backend.is_running = False
        pump_until(lambda: widget._native_paste_process is None)
        start.assert_not_called()
    assert not written


@pytest.mark.skipif(os.name == 'nt', reason='Real Unix PTY')
def test_writer_rejection_stops_remaining_quick_commands(terminal, tmp_path, monkeypatch):
    from terminal_writer import TerminalWriter
    widget, _ = terminal
    widget._backend = None
    received = tmp_path / 'received'
    program = ("import sys\nwith open('received', 'w', buffering=1) as f:\n"
               "    for line in sys.stdin: f.write(line)\n")
    recorded = []
    widget.input_recorded.connect(recorded.append)
    try:
        widget.start_process([sys.executable, '-u', '-c', program], cwd=str(tmp_path))
        pump_until(received.exists)
        monkeypatch.setattr(TerminalWriter, 'MAX_PENDING_BYTES', 256)
        widget._execute_commands(['first', 'x' * 300, 'third'])
        widget._execute_commands(['independent'])
        pump_until(lambda: 'independent' in received.read_text())
        assert received.read_text().splitlines() == ['first', 'independent']
        assert recorded == ['first', 'independent']
    finally:
        widget.stop_process()


def test_session_preset_stops_after_deferred_writer_rejection(terminal):
    widget, written = terminal
    widget._backend.is_running = True
    attempts = []

    def write(data):
        attempts.append(data)
        if data == b'rejected\r':
            return False
        written.append(data)
        return True

    widget._backend.write = write
    callbacks = []
    # Drive the preset timers independently of the real clipboard probe.
    with patch('terminal_widget.QTimer') as timer:
        timer.singleShot.side_effect = lambda delay, cb: callbacks.append(cb)
        widget._send_preset_commands(['first', 'rejected', 'third'])
        callbacks.pop(0)()
        with slow_probe(.1):
            widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'prefix '))
            callbacks.pop(0)()
            assert attempts == [b'first\r']
            pump_until(lambda: widget._native_paste_process is None)
        assert callbacks == []
    assert attempts == [b'first\r', b'prefix ', b'rejected\r']
    assert written == [b'first\r', b'prefix ']


def test_synchronous_probe_start_failure_cannot_strand_later_input(terminal, monkeypatch):
    widget, written = terminal
    # 每步都让出事件循环，覆盖排队输入分批回放的情况。
    monkeypatch.setattr(widget, '_PASTE_DRAIN_BUDGET_S', 0)
    real_start = QProcess.start
    starts = []

    def start(process, program, arguments):
        starts.append(process)
        if len(starts) == 1:
            return real_start(process, sys.executable, ['-c',
                'import time; time.sleep(.1); print(\'FILES_JSON:["/tmp/first.txt"]\')'])
        process.errorOccurred.emit(QProcess.ProcessError.FailedToStart)

    with patch.object(QProcess, 'start', new=start):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'unused'))
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'second'))
        press(widget, Qt.Key.Key_Return, '\r')
        pump_until(lambda: widget._native_paste_process is None
                   and not widget._native_paste_queue
                   and not getattr(widget, '_paste_drain_scheduled', False))
    assert written == [b'/tmp/first.txt ', b'second', b'\r']
    assert not widget._native_paste_queue


@pytest.mark.skipif(os.name == 'nt', reason='Real Unix PTY')
def test_paste_then_enter_reaches_real_shell_as_one_command(terminal):
    widget, _ = terminal
    widget._backend = None
    widget.start_process(['/bin/sh', '-c',
        'stty -echo; printf "READY\\n"; IFS= read -r line; printf "GOT:%s\\n" "$line"'])

    def contains(text):
        with widget._screen_lock:
            return text in '\n'.join(widget.screen.display)

    try:
        assert widget._backend is not None
        pump_until(lambda: contains('READY'))
        with slow_probe(.1):
            widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'pasted payload'))
            press(widget, Qt.Key.Key_Return, '\r')
            pump_until(lambda: contains('GOT:pasted payload'))
    finally:
        widget.stop_process()


@pytest.mark.parametrize('replace_session', [False, True])
def test_startup_preset_queued_behind_paste_stays_in_its_session(terminal, replace_session):
    widget, written = terminal
    widget._backend.is_running = True
    with slow_probe(.1):
        widget._paste_from_clipboard_macos(SimpleNamespace(text=lambda: 'prefix '))
        widget._start_and_execute(['preset'])
        if replace_session:
            widget._backend = SimpleNamespace(is_running=True, write=lambda data: written.append(data) or True)
        pump_until(lambda: widget._native_paste_process is None)
    assert written == ([] if replace_session else [b'prefix ', b'preset\r'])


@pytest.mark.parametrize('copy_during_save', [False, True, 'queued_paste'])
def test_native_image_disk_write_runs_off_gui_and_keeps_enter_order(terminal, tmp_path, copy_during_save):
    import shutil
    widget, written = terminal
    widget.image_save_local = True
    widget.get_cwd = lambda: str(tmp_path)
    real_start, real_move = QProcess.start, shutil.move
    entered, release = threading.Event(), threading.Event()
    workers = []

    def start(process, program, arguments):
        path = re.search(r'writeToFileAtomically\("([^"]+)"', arguments[-1])[1]
        script = f'from pathlib import Path; Path({path!r}).write_bytes(b"image fixture"); print("IMAGE_OK")'
        return real_start(process, sys.executable, ['-c', script])

    def move(source, target):
        workers.append(threading.get_ident())
        entered.set()
        release.wait(1)
        return real_move(source, target)

    try:
        with patch.object(QProcess, 'start', new=start), patch('shutil.move', side_effect=move):
            widget._paste_from_clipboard_macos(SimpleNamespace(text=Mock(side_effect=AssertionError)))
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                t0 = time.monotonic()
                _APP.processEvents()
                assert time.monotonic() - t0 < .2
                time.sleep(.005)
            assert entered.is_set()
            widget.send_text('\r')
            assert written == []
            queued_clipboard = None
            if copy_during_save == 'queued_paste':
                queued_clipboard = SimpleNamespace(text=Mock(return_value='obsolete second paste'))
                widget._paste_from_clipboard_macos(queued_clipboard)
                widget.send_text('\r')
            if copy_during_save:
                widget._on_clipboard_data_changed()
                widget._clipboard_change_token = lambda: 8
            release.set()
            pump_until(lambda: len(written) == 2)
            pump_until(lambda: not widget._native_paste_queue)
        assert workers and workers[0] != threading.get_ident()
        assert written[1] == b'\r'
        if queued_clipboard is not None:
            queued_clipboard.text.assert_not_called()
    finally:
        release.set()
