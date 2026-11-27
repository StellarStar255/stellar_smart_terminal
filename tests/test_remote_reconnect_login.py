"""Exercise browser login and reconnect against a password-only loopback SFTP server."""
import os
import socketserver
import threading
import time

import paramiko
import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QInputDialog, QListWidgetItem, QMessageBox

from remote_explorer_widget import RemoteExplorerPanel, _ROLE_ENTRY
from ssh_session import HostConfig

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


class Login(paramiko.ServerInterface):
    def __init__(self, server):
        self.server = server

    def get_allowed_auths(self, username):
        return 'password'

    def check_auth_password(self, username, password):
        self.server.attempts.append((username, password))
        if password == self.server.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Files(paramiko.SFTPServerInterface):
    def list_folder(self, path):
        return paramiko.SFTP_NO_SUCH_FILE if path == '/removed' else []


class Connection(socketserver.BaseRequestHandler):
    def handle(self):
        if self.server.offline:
            return
        with paramiko.Transport(self.request) as transport:
            transport.add_server_key(self.server.key)
            transport.set_subsystem_handler('sftp', paramiko.SFTPServer, Files)
            self.server.transports.append(transport)
            transport.start_server(server=Login(self.server))
            transport.join(15)


@pytest.fixture
def server():
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
    with Server(('127.0.0.1', 0), Connection) as service:
        service.offline = False
        service.attempts = []
        service.transports = []
        service.password = 'first-test-password'
        service.key = paramiko.RSAKey.generate(1024)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield service
        finally:
            service.shutdown()
            for transport in service.transports:
                transport.close()
            thread.join(2)


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(20)
    assert predicate()


def test_double_click_and_password_recovery(server, tmp_path, monkeypatch, app, caplog):
    import ssh_session
    monkeypatch.setenv('STELLAR_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(ssh_session, '_append_known_hosts', lambda entries: None)
    panel = RemoteExplorerPanel(theme={})
    host = HostConfig('loopback-test', '127.0.0.1', 'tester', server.server_address[1])
    item = QListWidgetItem()
    item.setData(_ROLE_ENTRY, host)
    prompts, opens, errors = [], [], []
    panel.host_connected.connect(opens.append)
    panel.error_occurred.connect(errors.append)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: errors.append(args[-1]))
    cancel = False

    def answer(dialog):
        prompts.append(dialog.labelText())
        for _ in range(10):
            panel._on_host_activated(item)
        if cancel:
            return QInputDialog.DialogCode.Rejected
        dialog.setTextValue(server.password)
        return QInputDialog.DialogCode.Accepted

    monkeypatch.setattr(QInputDialog, 'exec', answer)

    def drop(*, background_refresh=False):
        session = panel._session
        # The previous login/recovery also schedules a directory listing. Finish
        # it before injecting the next outage so it cannot reconnect in the
        # interval between closing the transport and observing the disconnect.
        drained = session.submit(lambda: None)
        wait_for(drained.done)
        drained.result()
        if session.is_alive():
            server.transports[-1].close()
            wait_for(lambda: not session.is_alive())
        if background_refresh:
            refresh = session.submit(session.listdir, panel._current_path, False)
        for _ in range(10):
            panel._on_host_activated(item)
        wait_for(lambda: panel._recovering_session is None)
        if background_refresh:
            assert refresh.result(timeout=2) == []
        assert panel._session is session

    try:
        for _ in range(10):
            panel._on_host_activated(item)
        wait_for(lambda: bool(opens))
        assert len(prompts) == len(server.attempts) == 1
        panel._current_path = '/working'
        panel._toggle_hosts_view()
        panel._on_host_activated(item)
        assert panel._current_path == '/working'
        drop(background_refresh=True)
        assert panel._current_path == '/working'
        assert len(prompts) == len(opens) == 1
        assert len(server.attempts) == 2

        server.offline = True
        drop()
        assert panel.get_cached_password(host) == server.password
        assert len(prompts) == 1
        assert len(errors) == 1
        errors.clear()
        server.offline = False
        drop()
        assert len(prompts) == 1
        server.password = 'rotated-test-password'
        panel._current_path = '/removed'
        drop()
        wait_for(lambda: panel._current_path == '/')
        assert len(errors) == 1
        assert len(prompts) == 2
        assert server.attempts[-2:] == [('tester', 'first-test-password'), ('tester', server.password)]
        assert panel.get_cached_password(host) == server.password
        assert len(opens) == 1
        wait_for(lambda: sum(t.is_active() for t in server.transports) == 1)

        cancel = True
        server.password = 'another-test-password'
        count = len(server.attempts)
        drop()
        assert len(server.attempts) == count + 1
        assert len(prompts) == 3
        assert not panel.get_cached_password(host)
        cancel = False
        drop()
        assert len(prompts) == 4
        assert panel.get_cached_password(host) == server.password

        panel._disconnect()
        assert not panel.get_cached_password(host)
        panel._on_host_activated(item)
        wait_for(lambda: len(opens) == 2)
        assert len(prompts) == 5
        panel.close()
        assert not panel.get_cached_password(host)
        assert 'test-password' not in caplog.text
        for file in tmp_path.rglob('*.json'):
            assert 'test-password' not in file.read_text()
    finally:
        panel.close()
        panel.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_switch_target_while_password_dialog_is_open(server, tmp_path, monkeypatch, app):
    from dataclasses import replace
    from PyQt6.QtCore import QTimer
    import ssh_session

    monkeypatch.setenv('STELLAR_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(ssh_session, '_append_known_hosts', lambda entries: None)
    panel = RemoteExplorerPanel(theme={})
    first = HostConfig('same-alias', '127.0.0.1', 'first-user', server.server_address[1])
    second = replace(first, user='second-user')
    connected, failures, handled = [], [], []
    panel.host_connected.connect(connected.append)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: failures.append(args[-1]))
    timer = QTimer()
    timer.setInterval(15)

    def visit_dialog():
        dialog = app.activeModalWidget()
        if not isinstance(dialog, QInputDialog) or dialog in handled:
            return
        handled.append(dialog)
        if len(handled) == 1:
            panel._connect_to(second)
        else:
            dialog.setTextValue(server.password)
            dialog.accept()

    timer.timeout.connect(visit_dialog)
    timer.start()
    try:
        panel._connect_to(first)
        wait_for(lambda: len(connected) == 1)
        assert len(handled) == 2
        assert server.attempts == [('second-user', server.password)]
        assert connected == [second]
        for other in (first, replace(second, port=22), replace(second, hostname='localhost')):
            assert panel.get_cached_password(other) is None
        panel._connect_to(first)
        wait_for(lambda: len(connected) == 2)
        assert len(handled) == 3
        panel._connect_to(second)
        wait_for(lambda: len(connected) == 3)
        assert len(handled) == 3
        assert not failures
        old_session = panel._session
        panel._disconnect_btn.click()
        assert not panel.get_cached_password(second)
        assert panel._prompt_password('same-alias', old_session) is None
    finally:
        timer.stop()
        panel.close()
        panel.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
