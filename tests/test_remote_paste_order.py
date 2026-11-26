"""Remote uploads must preserve input order and never write into another session."""
import os
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, QTimer
from main_window_remote import RemotePanelMixin
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def remote():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    written, uploads = [], []
    widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True,
                                      is_running=True, stop=lambda: None)
    widget._clipboard_change_token = lambda: None

    def upload(term, path, **kwargs):
        uploads.append((path, kwargs.get('request')))
        return True

    widget._owner_window = SimpleNamespace(_upload_pasted_media_for_terminal=upload)
    yield widget, written, uploads
    widget.cleanup()
    widget.deleteLater()
    _APP.processEvents()


def complete(widget, upload, result):
    path, request = upload
    if request is None:
        widget.remote_upload_done.emit(path, result)
    else:
        widget._remote_paste_finished.emit(request, result)


def test_ssh_target_discovery_skips_background_jobs():
    processes = [
        (10, 1, 'zsh'),
        (11, 10, 'ssh -N forwarding-host'),
        (12, 10, 'ssh training-host'),
    ]
    with patch('main_window_remote.os.getpgid', side_effect=lambda pid: pid, create=True):
        assert RemotePanelMixin._ssh_args_under(10, processes) == 'ssh -N forwarding-host'
        assert RemotePanelMixin._ssh_args_under(
            10, processes, foreground_group=12) == 'ssh training-host'
        assert RemotePanelMixin._ssh_args_under(10, processes, foreground_group=10) is None
        assert RemotePanelMixin._ssh_args_under(
            10, [(10, 1, 'ssh exec-host')], foreground_group=10) == 'ssh exec-host'


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(.005)


def test_enter_waits_until_remote_path_is_inserted(remote):
    widget, written, uploads = remote
    widget._PASTE_DRAIN_MAX_ACTIONS = 1
    widget._deliver_media_path('/tmp/a.png')
    widget.send_text('\r')
    assert written == []
    complete(widget, uploads[0], '/remote/a.png')
    wait_for(lambda: len(written) >= 2)
    assert written == [b'/remote/a.png', b'\r']
    assert widget._pending_remote_pastes == {}


def test_multiple_files_and_text_keep_original_order(remote):
    widget, written, uploads = remote

    widget._paste_file_paths(['/tmp/a.png', '/tmp/note.txt', '/tmp/b.png'])
    widget.send_text('\r')
    assert written == []
    assert len(uploads) == 1
    complete(widget, uploads[0], '/remote/a.png')
    wait_for(lambda: len(uploads) >= 2)
    assert written == [b'/remote/a.png ', b'/tmp/note.txt ']
    assert len(uploads) == 2
    complete(widget, uploads[1], '/remote/b.png')
    wait_for(lambda: len(written) >= 4)
    assert written == [b'/remote/a.png ', b'/tmp/note.txt ', b'/remote/b.png ', b'\r']


def test_old_upload_cannot_complete_new_request_for_same_file(remote):
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png')
    old = uploads[0]
    widget.stop_process()
    widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True,
                                      is_running=True, stop=lambda: None)
    widget._deliver_media_path('/tmp/a.png')
    new = uploads[-1]
    complete(widget, old, '/old/a.png')
    assert written == []
    complete(widget, new, '/new/a.png')
    assert written == [b'/new/a.png']


def test_duplicate_completion_is_ignored(remote):
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png')
    complete(widget, uploads[0], '/remote/a.png')
    complete(widget, uploads[0], '/remote/a.png')
    assert written == [b'/remote/a.png']


def test_legacy_completion_cannot_claim_a_new_identified_request(remote):
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png')
    widget.remote_upload_done.emit('/tmp/a.png', '/old/a.png')
    assert written == []
    complete(widget, uploads[0], '/current/a.png')
    assert written == [b'/current/a.png']


def test_same_file_pasted_twice_keeps_independent_options(remote):
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png', prefix='@', suffix=' ')
    widget._deliver_media_path('/tmp/a.png', prefix='', suffix='')
    assert len(uploads) == 1
    complete(widget, uploads[0], '/remote/a.png')
    assert len(uploads) == 2
    complete(widget, uploads[1], '/remote/a.png')
    assert written == [b'@/remote/a.png ', b'/remote/a.png']


@pytest.mark.parametrize('cancel', ['interrupt', 'cleanup'])
def test_cancel_drops_queued_enter_and_late_upload_result(remote, cancel):
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png')
    widget.send_text('\r')
    if cancel == 'interrupt':
        widget.send_text('\x03')
    else:
        widget.cleanup()
    complete(widget, uploads[0], '/remote/a.png')
    assert written == ([b'\x03'] if cancel == 'interrupt' else [])
    assert widget._pending_remote_pastes == {}


class _ProbeWindow(RemotePanelMixin, QObject):
    def __init__(self):
        super().__init__()
        self.statusbar = SimpleNamespace(showMessage=lambda *args: None)
        self._ssh_args_under = lambda pid: None
        self._known_remote_hosts = Mock(return_value=[])

    def dispose(self):
        pool = getattr(self, '_paste_probe_executor', None)
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        self.deleteLater()
        _APP.processEvents()


def test_file_stat_runs_off_gui_and_keeps_enter_order(remote, tmp_path):
    widget, written, _ = remote
    window = _ProbeWindow()
    widget._owner_window = window
    path = str(tmp_path / 'remote.png')
    entered, release = threading.Event(), threading.Event()
    workers = []
    real_isfile = os.path.isfile

    def stat(candidate):
        if candidate == path:
            workers.append(threading.get_ident())
            entered.set()
            release.wait(1)
            return True
        return real_isfile(candidate)

    try:
        with patch('main_window_remote.os.path.isfile', side_effect=stat):
            start = time.monotonic()
            widget._deliver_media_path(path)
            assert time.monotonic() - start < .2
            assert entered.wait(1)
            widget.send_text('\r')
            assert written == []
            request = next(iter(widget._pending_remote_pastes))
            release.set()
            request.future.result(timeout=2)
            deadline = time.monotonic() + 2
            while not written and time.monotonic() < deadline:
                _APP.processEvents()
                time.sleep(.005)
        assert len(workers) == 1
        assert workers[0] != threading.get_ident()
        assert len(written) == 2 and written[1] == b'\r'
    finally:
        release.set()
        window.dispose()


def test_cancelled_probe_cannot_start_host_resolution_or_upload(remote, tmp_path):
    widget, written, _ = remote
    window = _ProbeWindow()
    widget._owner_window = window
    path = tmp_path / 'remote.png'
    path.write_bytes(b'fixture')
    entered, release = threading.Event(), threading.Event()

    def scan(pid):
        entered.set()
        release.wait(1)
        return 'ssh test-host'

    window._ssh_args_under = scan
    try:
        widget._deliver_media_path(str(path))
        assert entered.wait(1)
        request = next(iter(widget._pending_remote_pastes))
        future = request.future
        widget.send_text('\x03')
        release.set()
        future.result(timeout=2)
        _APP.processEvents()
        window._known_remote_hosts.assert_not_called()
        assert written == [b'\x03']
    finally:
        release.set()
        window.dispose()


def test_different_local_files_with_same_name_cannot_overwrite_remote_image(remote):
    widget, _, _ = remote
    window = _ProbeWindow()
    destinations = []

    def submit(fn):
        future = Future()
        future.set_result(fn())
        return future

    session = SimpleNamespace(submit=submit, home=lambda: '/home', mkdir=lambda path: None,
                              upload=lambda local, target: destinations.append(target),
                              invalidate_cache=lambda path: None)
    host = SimpleNamespace(alias='host')

    def upload(term, path, request=None):
        window._start_paste_upload(term, host, session, path, request)
        return True

    widget._owner_window = SimpleNamespace(_upload_pasted_media_for_terminal=upload)
    try:
        widget._deliver_media_path('/tmp/project-a/image.png')
        widget._deliver_media_path('/tmp/project-b/image.png')
        assert len(destinations) == 2
        assert destinations[0] != destinations[1]
        assert all(path.startswith('/home/.images/') and path.endswith('.png') for path in destinations)
    finally:
        window.dispose()


def test_transferring_terminal_cancels_upload_owned_by_previous_window(remote):
    from main_window import MainWindow
    widget, written, uploads = remote
    old, new = QObject(), QObject()
    old._upload_pasted_media_for_terminal = widget._owner_window._upload_pasted_media_for_terminal
    old.active_terminal = widget
    widget._owner_window = old
    try:
        widget._deliver_media_path('/tmp/a.png')
        widget.send_text('\r')
        assert written == []
        MainWindow._adopt_terminal(new, widget)
        complete(widget, uploads[0], '/remote/a.png')
        assert written == []
        widget.send_text('fresh')
        assert written == [b'fresh']
        assert not widget._pending_remote_pastes
    finally:
        old.deleteLater()
        new.deleteLater()


@pytest.mark.parametrize('stage', ['probe', 'upload'])
def test_rejected_remote_job_completes_request_and_unblocks_input(remote, tmp_path, stage):
    widget, written, _ = remote
    window = _ProbeWindow()
    widget._owner_window = window
    path = tmp_path / 'image.png'
    path.write_bytes(b'fixture')
    rejected = SimpleNamespace(submit=Mock(side_effect=RuntimeError('executor closed')))
    if stage == 'probe':
        window._paste_probe_executor = rejected
    else:
        # 上传提交发生在探测回调里，已不在 _deliver_media_path 的异常保护中。
        widget._owner_window = SimpleNamespace(_upload_pasted_media_for_terminal=lambda *a, **kw: True)
    try:
        widget._deliver_media_path(str(path))
        if stage == 'upload':
            request = next(iter(widget._pending_remote_pastes))
            window._start_paste_upload(widget, SimpleNamespace(alias='host'), rejected,
                                       str(path), request)
        assert widget._pending_remote_pastes == {}
        assert not widget._native_paste_queue
        widget.send_text('\r')
        assert len(written) == 2 and written[1] == b'\r'
    finally:
        window._paste_probe_executor = None
        window.dispose()


def test_releasing_many_queued_inputs_yields_to_gui_events(remote):
    widget, written, uploads = remote

    def slow_write(data):
        time.sleep(.001)
        written.append(data)
        return True

    widget._backend.write = slow_write
    widget._deliver_media_path('/tmp/a.png')
    for _ in range(80):
        widget.send_text('x')
    heartbeats = []
    QTimer.singleShot(0, lambda: heartbeats.append(len(written)))
    start = time.monotonic()
    complete(widget, uploads[0], '/remote/a.png')
    assert time.monotonic() - start < .2
    assert len(written) < 81
    deadline = time.monotonic() + 2
    while len(written) < 81 and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(.001)
    assert len(written) == 81
    assert heartbeats and heartbeats[0] < 81
    assert written == [b'/remote/a.png'] + [b'x'] * 80


def test_rejected_paste_does_not_send_following_enter(remote):
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png')
    widget.send_text('\r')

    def reject_path(data):
        if data != b'\r':
            return False
        written.append(data)
        return True

    widget._backend.write = reject_path
    complete(widget, uploads[0], '/remote/a.png')
    assert written == []
    assert not widget._native_paste_queue


def test_programmatic_return_records_and_clears_command(remote):
    widget, written, _ = remote
    recorded = []
    widget.input_recorded.connect(recorded.append)
    widget.send_text('echo test')
    widget.send_text('\r')
    assert written == [b'echo test', b'\r']
    assert recorded == ['echo test']
    assert widget.input_buffer == ''


def test_natural_process_exit_cancels_pending_upload_and_enter(remote):
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png')
    widget.send_text('\r')
    widget._backend.is_running = False
    widget._on_process_finished(0)
    assert widget._pending_remote_pastes == {}
    assert not widget._native_paste_queue
    complete(widget, uploads[0], '/remote/a.png')
    assert written == []


def test_mouse_reports_do_not_overtake_pending_paste(remote):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    widget, written, uploads = remote
    widget._deliver_media_path('/tmp/a.png')
    pos = QPointF(widget.PADDING + widget.char_width / 2,
                  widget.PADDING + widget._header_h + widget.char_height / 2)
    event = QMouseEvent(QEvent.Type.MouseButtonPress, pos, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget._send_mouse_event(event, 'press')
    widget._send_wheel_to_app(True, event.pos(), 1)
    widget.send_text('\r')
    assert written == []
    complete(widget, uploads[0], '/remote/a.png')
    assert written == [b'/remote/a.png', b'\x1b[<0;1;1M', b'\x1b[<64;1;1M', b'\r']


def test_cursor_click_does_not_use_position_before_paste_echo(remote):
    widget, written, uploads = remote
    widget.screen.cursor.x = widget.screen.cursor.y = 0
    widget._deliver_media_path('/tmp/a.png')
    widget._move_cursor_to_click((0, 5))
    assert written == []
    complete(widget, uploads[0], '/remote/a.png')
    widget._move_cursor_to_click((0, 5))
    assert written == [b'/remote/a.png', b'\x1b[C' * 5]


def test_paste_after_process_exit_does_not_read_clipboard_or_start_work(remote):
    widget, written, _ = remote
    widget._backend.is_running = False
    widget._on_process_finished(0)
    with patch('terminal_input.QApplication.clipboard') as clipboard:
        widget._paste_from_clipboard()
        clipboard.assert_not_called()
    widget.send_text('must not send')
    assert written == []
