"""Forward ownership, read-only reconciliation, errors and UI lifecycle."""
import errno
import json
import os
import socket
import tempfile
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import ssh_control
from remote_explorer_widget import _ForwardsDialog
from ssh_session import HostConfig


def rule(kind='L', port=6006):
    return dict(type=kind, bind_host='127.0.0.1', bind_port=str(port),
                dest_host='127.0.0.1', dest_port='80')


@pytest.fixture
def master():
    if os.name == 'nt':
        pytest.skip('ControlMaster uses POSIX sockets')
    with tempfile.TemporaryDirectory(prefix='fwd-', dir='/tmp') as directory, \
         socket.socket(socket.AF_UNIX) as sock:
        ctl = os.path.join(directory, 'ctl')
        sock.bind(ctl)
        cfg = HostConfig(alias='test', hostname='example.invalid', user='test')
        with patch.object(ssh_control, 'control_path_for', return_value=ctl), \
             patch.object(ssh_control, 'master_pid', return_value=123):
            yield cfg, ctl


def record(ctl, spec, active=True):
    ssh_control._record_forward_state(
        ctl, ssh_control._forward_socket_identity(ctl), spec, active)


def test_recover_recorded_forwards_after_reopening(master):
    cfg, ctl = master
    specs = [rule(), rule('D', 6007), rule('R', 6008)]
    for spec in specs:
        record(ctl, spec)
    with patch.object(ssh_control, 'local_port_busy', return_value=True), \
         patch.object(ssh_control, 'port_holders', return_value=[('ssh', 123)]), \
         patch.object(ssh_control.subprocess, 'run') as run:
        assert ssh_control.forward_statuses(cfg, specs) == ['running'] * 3
    run.assert_not_called()  # No SSH forward/cancel as a status probe.


def test_stale_listener_and_foreign_owner_are_not_running(master):
    cfg, ctl = master
    record(ctl, rule())
    with patch.object(ssh_control, 'local_port_busy', return_value=False):
        assert ssh_control.forward_statuses(cfg, [rule()]) == ['stopped']
    with patch.object(ssh_control, 'local_port_busy', return_value=True), \
         patch.object(ssh_control, 'port_holders', return_value=[('ssh', 999)]):
        assert ssh_control.forward_statuses(cfg, [rule()]) == ['unknown']


def test_replaced_socket_does_not_inherit_records(master):
    cfg, ctl = master
    record(ctl, rule('R'))
    with patch.object(ssh_control, '_forward_socket_identity', return_value=[0, 0, 0]):
        assert ssh_control.forward_statuses(cfg, [rule('R')]) == ['unknown']
    with patch.object(ssh_control, 'master_pid', return_value=0):
        assert ssh_control.forward_statuses(cfg, [rule('R')]) == ['unknown']
        with patch.object(ssh_control, '_forward_socket_identity', return_value=None):
            assert ssh_control.forward_statuses(cfg, [rule('R')]) == ['stopped']


def test_successful_apply_and_cancel_persist_only_confirmed_results(master):
    import subprocess
    cfg, ctl = master
    spec = rule('R')
    with patch.object(ssh_control, 'is_supported', return_value=True), \
         patch.object(ssh_control.subprocess, 'run', return_value=
                      subprocess.CompletedProcess([], 0, b'', b'')):
        ssh_control.forward_apply(cfg, spec)
        assert ssh_control.forward_statuses(cfg, [spec]) == ['running']
        ssh_control.forward_apply(cfg, spec, cancel=True)
        assert ssh_control.forward_statuses(cfg, [spec]) == ['stopped']
    with patch.object(ssh_control, 'is_supported', return_value=True), \
         patch.object(ssh_control.subprocess, 'run', return_value=
                      subprocess.CompletedProcess([], 1, b'', b'forwarding refused')):
        with pytest.raises(RuntimeError):
            ssh_control.forward_apply(cfg, spec)
        assert ssh_control.forward_statuses(cfg, [spec]) == ['stopped']


def test_changed_destination_and_unrecorded_remote_are_unknown(master):
    cfg, ctl = master
    spec = rule()
    record(ctl, spec)
    spec['dest_port'] = '81'
    with patch.object(ssh_control, 'local_port_busy', return_value=True), \
         patch.object(ssh_control, 'port_holders', return_value=[('ssh', 123)]):
        assert ssh_control.forward_statuses(cfg, [spec, rule('R')]) == ['unknown', 'unknown']


def test_cancel_record_and_malformed_metadata(master):
    cfg, ctl = master
    record(ctl, rule('R'))
    record(ctl, rule('R'), False)
    assert ssh_control.forward_statuses(cfg, [rule('R')]) == ['stopped']
    with open(ctl + '.forwards.json', 'w') as stream:
        json.dump([], stream)
    assert ssh_control.forward_statuses(cfg, [rule('R')]) == ['unknown']


def test_recovery_requires_exact_record_and_current_master(master):
    cfg, ctl = master
    assert not ssh_control._forward_owned(cfg, rule(), [('ssh', 123)])
    record(ctl, rule())
    assert ssh_control._forward_owned(cfg, rule(), [('ssh', 123)])
    assert not ssh_control._forward_owned(cfg, rule(), [('ssh', 999)])
    assert not ssh_control._forward_owned(cfg, rule(), [('ssh', 123), ('ssh', 999)])
    assert not ssh_control._forward_owned(cfg, rule(), [])
    changed = dict(rule(), dest_port='81')
    assert not ssh_control._forward_owned(cfg, changed, [('ssh', 123)])


@pytest.mark.parametrize('error,kind', [
    (PermissionError(errno.EACCES, 'denied'), 'permission'),
    (OSError(errno.EPERM, 'denied'), 'permission'),
    (OSError(errno.EADDRNOTAVAIL, 'missing address'), 'address'),
    (socket.gaierror(socket.EAI_NONAME, 'unknown host'), 'dns'),
    (OSError(errno.EMFILE, 'too many files'), 'other'),
])
def test_bind_errors_preserve_reason_instead_of_reporting_conflict(error, kind):
    sock = Mock()
    sock.bind.side_effect = error
    with patch('socket.socket', return_value=sock):
        with pytest.raises(ssh_control.LocalBindError) as exc:
            ssh_control.local_port_busy('bad-address', 6006)
    assert exc.value.kind == kind
    assert exc.value.errno == error.errno
    sock.close.assert_called_once()


def test_only_address_in_use_is_a_conflict_and_listen_is_checked():
    sock = Mock()
    sock.listen.side_effect = OSError(errno.EADDRINUSE, 'in use')
    with patch('socket.socket', return_value=sock):
        assert ssh_control.local_port_busy('127.0.0.1', 6006)
    sock.close.assert_called_once()


def test_forward_timeout_reports_uncertain_result(master):
    import subprocess
    cfg, ctl = master
    with patch.object(ssh_control, 'is_supported', return_value=True), \
         patch.object(ssh_control, 'local_port_busy', return_value=False), \
         patch.object(ssh_control.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 15)):
        with pytest.raises(RuntimeError):
            ssh_control.forward_apply(cfg, rule())
    records = ssh_control._read_forward_state(ctl, ssh_control._forward_socket_identity(ctl))
    assert records[ssh_control._forward_identity(rule())] is None


def test_unverified_ssh_only_shows_details_without_recovery(dialog):
    d = dialog(apply_cb=Mock(side_effect=ssh_control.LocalPortBusy(6006, [('ssh', 999)])),
               force_cb=Mock())
    with patch.object(QMessageBox, 'warning') as warning, \
         patch.object(QMessageBox, 'exec') as question:
        assert not d._apply_selected(False)
    warning.assert_called_once()
    question.assert_not_called()
    d._force_cb.assert_not_called()


_APP = None


@pytest.fixture
def dialog():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    dialogs = []
    def make(**kw):
        defaults = dict(alias='test', rules=[rule()], apply_cb=Mock(), cancel_cb=Mock())
        defaults.update(kw)
        d = _ForwardsDialog(**defaults)
        dialogs.append(d)
        return d
    yield make
    for d in dialogs:
        d.close()
        d.deleteLater()


def test_dialog_reconciles_in_memory_state_on_open_and_refresh(dialog):
    status = Mock(return_value=['running'])
    d = dialog(status_cb=status)
    assert d.active_keys() == {d.rule_key(rule())}
    status.return_value = ['stopped']
    d._refresh_statuses()
    assert d.active_keys() == set()
    status.return_value = ['unknown']
    d._refresh_statuses()
    assert d._states[d.rule_key(rule())] == 'unknown'


def test_restart_stops_then_starts_and_guards_entire_transaction(dialog):
    calls = []
    d = dialog(active={_ForwardsDialog.rule_key(rule())})
    def callback(cancel):
        calls.append('stop' if cancel else 'start')
        assert d._operation_running
        assert not d.isEnabled()
        assert d._states[d.rule_key(rule())] == ('stopping' if cancel else 'starting')
        assert not d._apply_selected(False)
        assert not d._restart_selected()
        d._on_delete()
        d.reject()
        d.accept()
        assert d._list.count() == 1
        assert d.result() == 0
    d._cancel_cb = lambda spec: callback(True)
    d._apply_cb = lambda spec: callback(False)
    assert d._restart_selected()
    assert calls == ['stop', 'start']
    assert d.isEnabled()
    assert not d._operation_running
    assert not d._start_btn.isEnabled()
    assert d._stop_btn.isEnabled()


def test_failed_stop_never_starts_and_preserves_active_rule(dialog):
    d = dialog(active={_ForwardsDialog.rule_key(rule())},
               cancel_cb=Mock(side_effect=RuntimeError('cancel failed')))
    with patch.object(QMessageBox, 'warning'):
        assert not d._restart_selected()
    d._apply_cb.assert_not_called()
    assert d.active_keys() == {d.rule_key(rule())}
    assert d._states[d.rule_key(rule())] == 'failed'
    assert d._list.item(0).toolTip() == 'cancel failed'
    assert d.isEnabled()


def test_failed_restart_start_leaves_rule_stopped_and_retryable(dialog):
    d = dialog(active={_ForwardsDialog.rule_key(rule())},
               apply_cb=Mock(side_effect=RuntimeError('start failed')))
    with patch.object(QMessageBox, 'warning'):
        assert not d._restart_selected()
    assert not d.active_keys()
    assert d._start_btn.isEnabled()
    assert not d._stop_btn.isEnabled()
    assert d._states[d.rule_key(rule())] == 'failed'


def test_restart_of_stopped_rule_only_starts_and_duplicate_start_is_skipped(dialog):
    d = dialog()
    assert d._restart_selected()
    d._cancel_cb.assert_not_called()
    assert d._apply_selected(False)
    d._apply_cb.assert_called_once()


def test_refresh_failure_does_not_leave_false_running_status(dialog):
    status = Mock(return_value=['running'])
    d = dialog(status_cb=status)
    status.side_effect = RuntimeError('check failed')
    with patch.object(QMessageBox, 'warning'):
        d._refresh_statuses()
    assert not d.active_keys()
    assert d._states[d.rule_key(rule())] == 'unknown'
    assert d.isEnabled()


def test_start_timeout_is_uncertain_and_can_be_stopped(dialog):
    import subprocess
    error = RuntimeError('timeout')
    error.__cause__ = subprocess.TimeoutExpired('ssh', 15)
    d = dialog(apply_cb=Mock(side_effect=error))
    with patch.object(QMessageBox, 'warning'):
        assert not d._apply_selected(False)
    assert d.rule_key(rule()) in d._uncertain
    assert d._stop_btn.isEnabled()
    assert d._apply_selected(True)
    assert not d._uncertain
