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


@pytest.fixture(scope='session')
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


def test_keychain_opt_in_reuse_replace_and_forget(server, app, monkeypatch):
    import ssh_keychain
    import ssh_session
    from PyQt6.QtWidgets import QCheckBox
    from i18n import t

    store = {}
    monkeypatch.setattr(ssh_keychain, 'available', True)
    monkeypatch.setattr(ssh_keychain, 'read', lambda host: store.get(host.credential_key))
    monkeypatch.setattr(ssh_keychain, 'save', lambda host, pwd: store.update({host.credential_key: pwd}))
    monkeypatch.setattr(ssh_keychain, '_delete', lambda host: store.pop(host.credential_key, None))
    monkeypatch.setattr(ssh_session, '_append_known_hosts', lambda entries: None)
    errors = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: errors.append(args[-1]))
    host = HostConfig('keychain-test', '127.0.0.1', 'tester', server.server_address[1])
    prompts = []
    answer = server.password
    opt_in = False
    cancel = False

    def respond(dialog):
        remember, = [box for box in dialog.findChildren(QCheckBox) if box.text() in (t('remote.keychain_save'), t('remote.keychain_update'))]
        prompts.append(remember.isChecked())
        remember.setChecked(opt_in)
        dialog.setTextValue(answer)
        return QInputDialog.DialogCode.Rejected if cancel else QInputDialog.DialogCode.Accepted

    monkeypatch.setattr(QInputDialog, 'exec', respond)

    def login(*, success=True, forget=False):
        panel = RemoteExplorerPanel(theme={})
        opened = []
        errors.clear()
        panel.host_connected.connect(opened.append)
        try:
            panel._connect_to(host)
            wait_for(lambda: panel._connecting_session is None)
            assert bool(opened) == success
            if forget:
                panel._forget_keychain_password(host)
        finally:
            panel.close()
            panel.deleteLater()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    login()
    assert not store
    opt_in = True
    login()
    assert store == {host.credential_key: answer}
    login()
    assert prompts == [False, False]
    server.password = 'replacement-test-password'
    cancel = True
    login(success=False)
    assert not errors
    assert store[host.credential_key] == 'first-test-password'
    cancel = False
    answer = 'mistyped-test-password'
    login(success=False)
    assert store[host.credential_key] == 'first-test-password'
    answer = server.password
    login()
    assert store[host.credential_key] == answer
    login(forget=True)
    assert not store
    opt_in = False
    login()
    assert prompts == [False] * 6


def test_forget_from_another_window_while_login_finishes(server, monkeypatch, app):
    import ssh_keychain
    import ssh_session
    from PyQt6.QtWidgets import QCheckBox

    host = HostConfig('pending-save', '127.0.0.1', 'tester', server.server_address[1])
    panel = RemoteExplorerPanel(theme={})
    other = RemoteExplorerPanel(theme={})
    opened, writes = [], []
    panel.host_connected.connect(opened.append)
    verified, resume = threading.Event(), threading.Event()
    open_channel = ssh_session.SSHSession._open_sftp_bounded

    def pause(session, client):
        verified.set()
        assert resume.wait(8)
        return open_channel(session, client)

    def consent(dialog):
        dialog.findChild(QCheckBox).setChecked(True)
        dialog.setTextValue(server.password)
        return QInputDialog.DialogCode.Accepted

    monkeypatch.setattr(ssh_keychain, 'available', True)
    monkeypatch.setattr(ssh_keychain, 'read', lambda h: None)
    monkeypatch.setattr(ssh_keychain, 'save', lambda h, pwd: writes.append(h))
    monkeypatch.setattr(ssh_keychain, '_delete', lambda h: None)
    monkeypatch.setattr(ssh_session, '_append_known_hosts', lambda entries: None)
    monkeypatch.setattr(ssh_session.SSHSession, '_open_sftp_bounded', pause)
    monkeypatch.setattr(QInputDialog, 'exec', consent)
    try:
        panel._connect_to(host)
        wait_for(verified.is_set)
        other._forget_keychain_password(host)
        resume.set()
        wait_for(lambda: bool(opened))
        assert not writes
    finally:
        resume.set()
        for widget in (panel, other):
            widget.close()
            widget.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_keychain_access_denied_still_allows_manual_login(server, app, monkeypatch):
    import ssh_keychain
    import ssh_session
    from i18n import t
    from PyQt6.QtWidgets import QCheckBox

    def denied(*args):
        raise OSError("denied")

    def answer(dialog):
        dialog.findChild(QCheckBox).setChecked(True)
        dialog.setTextValue(server.password)
        return QInputDialog.DialogCode.Accepted

    monkeypatch.setattr(ssh_keychain, 'available', True)
    monkeypatch.setattr(ssh_keychain, 'read', denied)
    monkeypatch.setattr(ssh_keychain, 'save', denied)
    monkeypatch.setattr(ssh_keychain, '_delete', denied)
    monkeypatch.setattr(QInputDialog, 'exec', answer)
    monkeypatch.setattr(ssh_session, '_append_known_hosts', lambda entries: None)
    host = HostConfig('keychain-denied', '127.0.0.1', 'tester', server.server_address[1])
    panel = RemoteExplorerPanel(theme={})
    notices, opened = [], []
    panel.error_occurred.connect(notices.append)
    panel.host_connected.connect(opened.append)
    monkeypatch.setattr(QMessageBox, 'warning', lambda parent, title, message: notices.append(message))
    try:
        panel._connect_to(host)
        wait_for(lambda: bool(opened))
        assert panel._session.is_alive()
        panel._forget_keychain_password(host)
        assert notices == [t('remote.keychain_read_failed'), t('remote.keychain_save_failed'),
                           t('remote.keychain_forget_failed')]
    finally:
        panel.close()
        panel.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_closed_session_does_not_login_after_keychain_unlock(monkeypatch, app):
    import ssh_keychain
    from ssh_session import SSHSession

    entered, unlocked = threading.Event(), threading.Event()
    attempts = []

    def delayed_read(host):
        entered.set()
        assert unlocked.wait(4)
        return 'previously-saved-test-password'

    monkeypatch.setattr(ssh_keychain, 'read', delayed_read)
    sess = SSHSession(HostConfig('retired', 'test.invalid'))
    sess.use_keychain = True
    monkeypatch.setattr(sess, '_establish', lambda: attempts.append(1))
    future = sess.connect_async()
    try:
        assert entered.wait(2)
        sess.disconnect()
    finally:
        unlocked.set()
    with pytest.raises(RuntimeError, match="closed"):
        future.result(timeout=2)
    assert not attempts
    assert sess.verified_password() is None
