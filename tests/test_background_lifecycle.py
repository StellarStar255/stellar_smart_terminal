"""Server startup cancellation, tab remapping, and final recording persistence."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

import openai_server
from openai_server import OpenAICompatServer, OpenAIServerManager, ServerConfig
from session_manager import SessionManager
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def test_stop_before_server_start_does_not_bind_a_port(app):
    server = OpenAICompatServer(SimpleNamespace(), ServerConfig(port=8100))
    constructor = Mock()
    try:
        with patch('openai_server._FastBindHTTPServer', constructor):
            server.stop()
            server.start()
            assert server.wait(1000)
        constructor.assert_not_called()
    finally:
        server.stop()
        assert server.wait(3000)


def test_bind_failure_cleans_bridge_and_reports_stop(app):
    server = OpenAICompatServer(SimpleNamespace(), ServerConfig(port=8100))
    errors, stopped = [], []
    server.error.connect(errors.append)
    server.stopped.connect(lambda: stopped.append(True))
    with patch('openai_server._FastBindHTTPServer', side_effect=OSError('port in use')):
        server.start()
        assert server.wait(1000)
    app.processEvents()
    assert errors == ['port in use']
    assert stopped == [True]
    assert server.bridge.shutdown_event.is_set()
    assert not server.bridge.is_collecting


class FakeServer(QObject):
    listening = pyqtSignal(int)
    stopped = pyqtSignal()
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, terminal, config):
        super().__init__()
        self.config = config
        self.tab_index = 0
        self.stop = Mock()
        self.wait = Mock(return_value=True)

    def start(self):
        pass

    def finish(self):
        self.stopped.emit()
        self.finished.emit()


@pytest.fixture
def manager(app):
    manager = OpenAIServerManager()
    manager.port_allocator._is_available = lambda _: True
    with patch('openai_server.OpenAICompatServer', FakeServer):
        yield manager
    for server in list(manager.servers.values()) + list(manager._retiring):
        server.finish()
    manager.deleteLater()
    app.processEvents()


def test_closed_tab_server_is_retained_and_included_in_shutdown(manager):
    manager.start_server(0, object(), 8101)
    closed = manager.servers[0]
    manager.start_server(1, object(), 8102)
    survivor = manager.servers[1]
    manager.stop_server(0)
    manager.remap_indices({1: 0})
    assert manager.servers[0] is survivor
    assert closed in manager._retiring
    assert closed in openai_server._LIVE_OPENAI_SERVERS
    manager.stop_all()
    closed.wait.assert_called_once()
    survivor.wait.assert_called_once()
    closed.finish()
    assert manager.servers[0] is survivor
    assert closed not in manager._retiring
    assert closed not in openai_server._LIVE_OPENAI_SERVERS
    assert manager.get_port(0) == 8102


def test_finished_server_releases_port_after_startup_error(manager):
    manager.start_server(0, object(), 8101)
    server = manager.servers[0]
    errors = []
    manager.server_error.connect(lambda index, message: errors.append((index, message)))
    server.error.emit('bind failed')
    server.finish()
    assert errors == [(0, 'bind failed')]
    assert not manager.is_running(0)
    assert manager.get_port(0) is None
    manager.start_server(0, object(), 8101)
    assert manager.is_running(0)


def test_shutdown_queues_latest_recording_after_slow_autosave(tmp_path):
    manager = SessionManager()
    manager.sessions_dir = tmp_path
    session = manager.create_session('test')
    manager.add_output('old')
    entered, release = threading.Event(), threading.Event()
    write = manager._write_session_data
    writes = []
    def slow_write(data, sid):
        writes.append(data['entries'][0]['content'])
        if len(writes) == 1:
            entered.set()
            assert release.wait(3)
        return write(data, sid)
    try:
        with patch.object(manager, '_write_session_data', side_effect=slow_write):
            manager.auto_save()
            assert entered.wait(1)
            manager.add_output(' latest')
            start = time.monotonic()
            pending = manager.shutdown()
            assert time.monotonic() - start < .5
            assert manager.current_session is None
            assert pending is not None and not pending.done()
            release.set()
            manager.shutdown(wait=True)
            manager.shutdown(wait=True)
        saved = manager.load_session(session.session_id)
        assert saved.entries[0].content == 'old latest'
        assert saved.end_time
        assert writes == ['old', 'old latest']
        with pytest.raises(RuntimeError, match='closed'):
            manager.create_session()
    finally:
        release.set()
        manager._save_executor.shutdown(wait=True)


def test_terminal_cleanup_records_output_arriving_during_stop(app):
    terminal = TerminalWidget()
    received = []
    terminal.output_recorded.connect(received.append)
    terminal._buffer_output('before stop')
    try:
        with patch.object(terminal, 'stop_process', side_effect=lambda: terminal._buffer_output(' final tail')):
            terminal.cleanup()
        assert received == ['before stop final tail']
    finally:
        terminal.cleanup()
        terminal.deleteLater()


def test_window_close_persists_last_unflushed_recording(app, tmp_path):
    from main_window import MainWindow
    from _qt_quit_reset_2026_09_win import reset_quit_state
    reset_quit_state(app)
    window = MainWindow()
    manager = window.session_manager
    manager.sessions_dir = tmp_path
    session = manager.create_session('close-test')
    window.current_session = session
    try:
        window.active_terminal._buffer_output('last output before closing')
        QApplication.sendEvent(window, QCloseEvent())
        manager.flush()
        saved = manager.load_session(session.session_id)
        assert saved.entries[-1].content == 'last output before closing'
        assert saved.end_time
        assert manager._closed
    finally:
        window._force_closing = True
        window.close()
        window.deleteLater()
        reset_quit_state(app)
