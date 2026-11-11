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


@pytest.mark.parametrize('rc', [0, 255])
@pytest.mark.parametrize('summary', ['', '\nmaster cancel forward request failed'])
def test_cancel_absent_rule_is_success_and_records_stopped(master, rc, summary):
    import subprocess
    cfg, ctl = master
    spec = rule('R')
    record(ctl, spec)
    error = ('mux_client_forward: forwarding request failed: port not forwarded' + summary).encode()
    with patch.object(ssh_control, 'is_supported', return_value=True), \
         patch.object(ssh_control.subprocess, 'run', return_value=
                      subprocess.CompletedProcess([], rc, b'', error)):
        ssh_control.forward_apply(cfg, spec, cancel=True)
    assert ssh_control.forward_statuses(cfg, [spec]) == ['stopped']


def test_missing_rule_message_never_hides_other_errors(master):
    import subprocess
    cfg, ctl = master
    spec = rule('R')
    record(ctl, spec)
    error = (b'mux_client_forward: forwarding request failed: port not forwarded\n'
             b'Control socket connect: No such file or directory')
    with patch.object(ssh_control, 'is_supported', return_value=True), \
         patch.object(ssh_control.subprocess, 'run', return_value=
                      subprocess.CompletedProcess([], 255, b'', error)):
        with pytest.raises(ssh_control.MasterNotRunning):
            ssh_control.forward_apply(cfg, spec, cancel=True)


@pytest.mark.parametrize('action', ['restart', 'delete'])
def test_unknown_absent_rule_can_restart_or_delete(master, dialog, action):
    import subprocess
    cfg, _ = master
    spec = rule('R')
    d = dialog(rules=[spec], status_cb=lambda specs: ['unknown'],
               cancel_cb=lambda s: ssh_control.forward_apply(cfg, s, cancel=True))
    with patch.object(ssh_control, 'is_supported', return_value=True), \
         patch.object(ssh_control.subprocess, 'run', return_value=
                      subprocess.CompletedProcess([], 0, b'',
                          b'mux_client_forward: forwarding request failed: port not forwarded')), \
         patch.object(QMessageBox, 'warning') as warning:
        if action == 'restart':
            assert d._restart_selected()
            d._apply_cb.assert_called_once_with(spec)
        else:
            d._on_delete()
            assert not d.rules()
    warning.assert_not_called()


@pytest.mark.parametrize('bad', [None, [], 'bad', {}, dict(rule(), bind_port='oops'),
                                 dict(rule(), dest_port=70000), dict(rule(), type='X'),
                                 dict(rule(), bind_port=float('inf'))])
def test_bad_rule_does_not_interrupt_other_statuses(master, bad):
    cfg, _ = master
    with patch.object(ssh_control, 'local_port_busy', return_value=False):
        assert ssh_control.forward_statuses(cfg, [rule(), bad, rule('D')]) == [
            'stopped', 'invalid', 'stopped']
    with patch.object(ssh_control, 'master_pid', return_value=0):
        assert ssh_control.forward_statuses(cfg, [bad, rule()]) == ['invalid', 'unknown']


def test_invalid_rule_is_visible_deletable_and_does_not_block_batch(dialog):
    bad = dict(rule(port=6007), dest_port='oops')
    d = dialog(rules=[rule(), bad, rule(port=6008)])
    d._list.setCurrentRow(1)
    assert d._states[d.rule_key(bad)] == 'invalid'
    assert d._list.item(1).toolTip()
    assert not d._start_btn.isEnabled()
    assert not d._restart_btn.isEnabled()
    d._check_all(True)
    with patch.object(QMessageBox, 'warning') as warning:
        d._apply_checked(False)
    warning.assert_called_once()
    assert [call.args[0]['bind_port'] for call in d._apply_cb.call_args_list] == ['6006', '6008']
    d._on_delete()
    assert len(d.rules()) == 2
    d._cancel_cb.assert_not_called()


def test_auto_apply_reports_invalid_rule_and_still_starts_healthy_rules(master, dialog):
    from remote_explorer_widget import RemoteExplorerPanel
    cfg, _ = master
    panel = RemoteExplorerPanel(theme={})
    specs = [dict(rule(), auto=True), dict(rule(port=6007), auto=True, dest_port='bad'),
             dict(rule(port=6008), auto=True)]
    errors = []
    panel.error_occurred.connect(errors.append)
    try:
        with patch.object(panel, '_load_forwards', return_value=specs), \
             patch.object(panel, '_active_forwards', {}), \
             patch.object(panel, '_refresh_pre_qos_master'), \
             patch.object(panel, '_populate_hosts_list'), \
             patch.object(panel, '_run_blocking', side_effect=lambda fn, label: fn()), \
             patch.object(ssh_control, 'is_supported', return_value=True), \
             patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             patch.object(ssh_control, 'local_port_busy', return_value=False), \
             patch.object(ssh_control, 'forward_apply') as apply:
            panel._auto_apply_forwards(cfg)
        assert [call.args[1]['bind_port'] for call in apply.call_args_list] == ['6006', '6008']
        assert len(errors) == 1
        assert '6007' in errors[0]
    finally:
        panel.close()
        panel.deleteLater()


def begin_background(d, result=None):
    from concurrent.futures import Future
    future = Future()
    pool = Mock()
    pool.submit.return_value = future
    d._status_executor = pool
    d.show()
    QApplication.processEvents()
    d._start_status_refresh()
    if result is not None:
        future.set_result(result)
    return future, pool


def test_background_updates_state_without_popup_or_selection_changes(dialog):
    from PyQt6.QtCore import Qt
    d = dialog(rules=[rule(), rule(port=6007)], status_cb=lambda specs: ['running', 'stopped'])
    d._list.setCurrentRow(1)
    d._list.item(0).setCheckState(Qt.CheckState.Checked)
    future, pool = begin_background(d, ['stopped', 'running'])
    with patch.object(QMessageBox, 'warning') as warning:
        d._poll_status_refresh()
    warning.assert_not_called()
    assert d.active_keys() == {d.rule_key(rule(port=6007))}
    assert d._list.currentRow() == 1
    assert d._list.item(0).checkState() == Qt.CheckState.Checked
    assert d.isEnabled()
    assert d._status_pending is None
    assert d._status_timer.interval() == 15000
    pool.submit.assert_called_once()


def test_background_never_overlaps_itself_or_operations(dialog):
    d = dialog(status_cb=lambda specs: ['stopped'])
    future, pool = begin_background(d)
    d._start_status_refresh()
    d._refresh_statuses()
    pool.submit.assert_called_once()
    future.set_result(['stopped'])
    d._poll_status_refresh()
    d._operation_running = True
    d._start_status_refresh()
    d._operation_running = False
    d._batch_running = True
    d._start_status_refresh()
    d._batch_running = False
    pool.submit.assert_called_once()


def test_late_background_snapshot_cannot_overwrite_completed_start(dialog):
    d = dialog(status_cb=lambda specs: ['stopped'])
    future, _ = begin_background(d)
    assert d._apply_selected(False)
    future.set_result(['stopped'])
    d._poll_status_refresh()
    assert d.active_keys() == {d.rule_key(rule())}


def test_background_snapshot_is_discarded_after_rule_deletion(dialog):
    d = dialog(rules=[rule(), rule(port=6007)], status_cb=lambda specs: ['stopped', 'stopped'])
    future, _ = begin_background(d)
    d._on_delete()
    future.set_result(['running', 'running'])
    with patch.object(QMessageBox, 'warning') as warning:
        d._poll_status_refresh()
    warning.assert_not_called()
    assert len(d.rules()) == 1
    assert not d.active_keys()


def test_background_error_is_unknown_with_detail_and_no_modal(dialog):
    d = dialog(status_cb=lambda specs: ['running'])
    future, _ = begin_background(d)
    future.set_exception(RuntimeError('check failed'))
    with patch.object(QMessageBox, 'warning') as warning:
        d._poll_status_refresh()
    warning.assert_not_called()
    assert d._states[d.rule_key(rule())] == 'unknown'
    assert not d.active_keys()
    assert d._list.item(0).toolTip() == 'check failed'


def test_close_stops_polling_without_waiting_for_worker(dialog):
    d = dialog(status_cb=lambda specs: ['stopped'])
    future, pool = begin_background(d)
    future.set_running_or_notify_cancel()
    d.close()
    assert d._status_closed
    assert not d._status_timer.isActive()
    assert not d._status_poll_timer.isActive()
    pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    future.set_result(['running'])
    d._poll_status_refresh()
    assert not d.active_keys()


def test_real_background_worker_is_off_gui_thread_and_does_not_disable_controls(dialog):
    import threading
    from PyQt6.QtCore import QTimer
    started, release = threading.Event(), threading.Event()
    gui_thread = threading.get_ident()
    d = dialog(status_cb=lambda specs: ['stopped'])
    def worker(specs):
        assert threading.get_ident() != gui_thread
        started.set()
        assert release.wait(2)
        return ['stopped']
    d._status_cb = worker
    d.show()
    QApplication.processEvents()
    d._start_status_refresh()
    try:
        assert started.wait(1)
        assert d.isEnabled()
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        QApplication.processEvents()
        assert ticks
        assert d._apply_selected(False)
    finally:
        release.set()
        if d._status_pending is not None:
            d._status_pending[0].result(timeout=2)
        d._poll_status_refresh()
    assert d.active_keys() == {d.rule_key(rule())}


def test_timer_schedules_only_while_visible(dialog):
    from concurrent.futures import Future
    from PyQt6.QtTest import QTest
    d = dialog(status_cb=lambda specs: ['stopped'])
    pool = Mock()
    pool.submit.return_value = Future()
    d._status_executor = pool
    d._status_timer.setInterval(10)
    d._start_status_refresh()
    pool.submit.assert_not_called()
    d.show()
    QTest.qWait(50)
    pool.submit.assert_called_once()
    d.hide()
    assert not d._status_timer.isActive()
    QTest.qWait(30)
    pool.submit.assert_called_once()


@pytest.mark.parametrize('background', [False, True])
def test_failed_refresh_preserves_invalid_rule_error_and_delete_action(dialog, background):
    bad = dict(rule(), dest_port='bad')
    callback = Mock(return_value=['invalid'])
    d = dialog(rules=[bad], status_cb=callback)
    original_error = d._list.item(0).toolTip()
    with patch.object(QMessageBox, 'warning'):
        if background:
            future, _ = begin_background(d)
            future.set_exception(RuntimeError('check failed'))
            d._poll_status_refresh()
        else:
            callback.side_effect = RuntimeError('check failed')
            d._refresh_statuses()
    assert d._states[d.rule_key(bad)] == 'invalid'
    assert d._list.item(0).toolTip() == original_error
    d._on_delete()
    assert not d.rules()
    d._cancel_cb.assert_not_called()
