"""Diagnostic failure semantics, existing-master-only execution and manual UI."""
import json
import os
import subprocess
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox, QInputDialog

import forward_diagnostics as diagnostics
from i18n import t
from remote_explorer_widget import _ForwardsDialog
import ssh_control
from ssh_session import HostConfig


def rule(kind='L'):
    return dict(type=kind, bind_host='127.0.0.1', bind_port='6006',
                dest_host='127.0.0.1', dest_port='8080')


@pytest.fixture
def host():
    return HostConfig(alias='test', hostname='example.invalid', user='test')


@pytest.mark.parametrize('value', ['', 'localhost', ':80', 'a:0', 'a:65536',
                                  'evil;command:80', 'a:abc'])
def test_invalid_target_is_rejected_before_network(host, value):
    with patch.object(diagnostics.socket, 'create_connection') as connect, \
         patch.object(ssh_control, 'forward_statuses') as status:
        with pytest.raises(ValueError):
            diagnostics.check_forward(host, rule('D'), value)
    connect.assert_not_called()
    status.assert_not_called()


def test_target_whitespace_is_normalized():
    assert diagnostics.parse_target(' example.invalid : 443 ') == ('example.invalid', 443)


@pytest.mark.parametrize('payload,expected', [
    ({'status': 'ok'}, 'ok'), ({'status': 'failed', 'detail': 'refused'}, 'failed'),
    ({'status': 'unknown', 'detail': 'missing_probe_tool'}, 'unknown'),
    ([], 'unknown'), ({'status': 'unexpected'}, 'unknown'),
])
def test_remote_result_and_no_authentication_fallback(host, payload, expected):
    output = b'login banner\nSTELLAR_TCP_RESULT=' + json.dumps(payload).encode() + b'\n'
    with patch.object(ssh_control, 'control_path_for', return_value='/tmp/isolated-control'), \
         patch.object(ssh_control, '_forward_socket_identity', return_value=[1, 2, 3]), \
         patch.object(ssh_control, 'master_pid', return_value=123), \
         patch.object(diagnostics.subprocess, 'run', return_value=
                      subprocess.CompletedProcess([], 0, output, b'')) as run:
        result = diagnostics._remote_tcp(host, '127.0.0.1', 8080, 3)
    assert result.status == expected
    argv = run.call_args.args[0]
    assert 'ProxyCommand=false' in argv
    assert 'BatchMode=yes' in argv
    assert 'ControlMaster=no' in argv
    assert 'ClearAllForwardings=yes' in argv
    assert not any(option in argv for option in ('-L', '-R', '-D', '-M'))
    if isinstance(payload, dict) and payload.get('detail') == 'missing_probe_tool':
        assert result.detail == t('remote.fwd_probe_missing_tool')


@pytest.mark.parametrize('failure', [
    subprocess.CompletedProcess([], 255, b'', b'master not available'),
    subprocess.TimeoutExpired('ssh', 8),
    subprocess.CompletedProcess([], 0, b'STELLAR_TCP_RESULT=broken', b''),
])
def test_inability_to_run_remote_probe_is_not_target_failure(host, failure):
    kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
    with patch.object(ssh_control, 'control_path_for', return_value='/tmp/isolated-control'), \
         patch.object(ssh_control, '_forward_socket_identity', return_value=[1, 2, 3]), \
         patch.object(ssh_control, 'master_pid', return_value=123), \
         patch.object(diagnostics.subprocess, 'run', **kwargs):
        assert diagnostics._remote_tcp(host, '127.0.0.1', 8080, 3).status == 'unknown'


def test_missing_master_never_starts_ssh(host):
    with patch.object(ssh_control, 'control_path_for', return_value='/tmp/absent-control'), \
         patch.object(ssh_control, '_forward_socket_identity', return_value=None), \
         patch.object(diagnostics.subprocess, 'run') as run:
        assert diagnostics._remote_tcp(host, '127.0.0.1', 8080, 3).status == 'unknown'
    run.assert_not_called()


@pytest.mark.parametrize('responses,expected', [
    ([b'\x05\xff'], ('failed', 'unknown')),
    ([b'\x05\x00', b'\x05\x05\x00\x01'], ('ok', 'failed')),
    ([b'\x05\x00', b''], ('ok', 'failed')),
    ([b'\x05\x00', b'\x05\x00\x00\x01', b'\0' * 6], ('ok', 'ok')),
    ([b'\x05', b'\x00', b'\x05\x00', b'\x00\x01', b'\0' * 6], ('ok', 'ok')),
])
def test_socks_handshake_is_distinct_from_target_connect(responses, expected):
    stream = Mock()
    stream.recv.side_effect = responses
    connection = Mock()
    connection.__enter__ = Mock(return_value=stream)
    connection.__exit__ = Mock(return_value=False)
    with patch.object(diagnostics.socket, 'create_connection', return_value=connection):
        listener, target = diagnostics._socks('127.0.0.1', 6006, 'example.invalid', 443, 3)
    assert (listener.status, target.status) == expected


def test_unknown_listener_is_not_contacted(host):
    with patch.object(ssh_control, 'forward_statuses', return_value=['unknown']), \
         patch.object(diagnostics, '_remote_tcp', return_value=diagnostics.Check('ok')), \
         patch.object(diagnostics, '_tcp') as tcp, patch.object(diagnostics, '_socks') as socks:
        result = diagnostics.check_forward(host, rule())
        assert result.listener.status == 'skipped'
        assert result.target.status == 'ok'
        result = diagnostics.check_forward(host, rule('D'), 'example.invalid:443')
        assert result.target.status == 'skipped'
    tcp.assert_not_called()
    socks.assert_not_called()


_APP = None


@pytest.fixture
def dialog():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    dialogs = []
    def make(kind='L', **kwargs):
        spec = rule(kind)
        result = diagnostics.ForwardCheck(kind, 'running', '127.0.0.1', 6006,
                                          '127.0.0.1', 8080, diagnostics.Check('ok'),
                                          diagnostics.Check('failed', 'refused'))
        arguments = dict(alias='test', rules=[spec], apply_cb=Mock(), cancel_cb=Mock(),
                         probe_cb=Mock(return_value=result))
        arguments.update(kwargs)
        d = _ForwardsDialog(**arguments)
        dialogs.append(d)
        return d
    yield make
    for d in dialogs:
        d.close()
        d.deleteLater()


def test_manual_test_shows_separate_results_without_changing_forward(dialog):
    d = dialog()
    with patch.object(QMessageBox, 'information') as information:
        assert d._probe_selected()
    d._probe_cb.assert_called_once_with(rule(), None)
    text = information.call_args.args[2]
    assert t('remote.fwd_state_running') in text
    assert t('remote.fwd_probe_status_ok') in text
    assert t('remote.fwd_probe_status_failed') in text
    assert t('remote.fwd_probe_note') in text
    assert 'refused' in text
    d._apply_cb.assert_not_called()
    d._cancel_cb.assert_not_called()


@pytest.mark.parametrize('text,accepted,should_call', [
    ('', False, False), ('bad', True, False), ('example.invalid:443', True, True)])
def test_socks_requires_explicit_valid_destination(dialog, text, accepted, should_call):
    d = dialog('D')
    with patch.object(QInputDialog, 'getText', return_value=(text, accepted)), \
         patch.object(QMessageBox, 'information'), patch.object(QMessageBox, 'warning'):
        assert d._probe_selected() == should_call
    assert d._probe_cb.called == should_call
    if should_call:
        d._probe_cb.assert_called_once_with(rule('D'), text)


def test_probe_guards_operations_and_recovers_from_error(dialog):
    d = dialog()
    def probe(spec, target):
        assert d._operation_running and not d.isEnabled()
        assert not d._apply_selected(False)
        assert not d._restart_selected()
        assert not d._probe_selected()
        d._on_delete()
        assert len(d.rules()) == 1
        raise RuntimeError('probe unavailable')
    d._probe_cb = probe
    with patch.object(QMessageBox, 'warning') as warning:
        assert not d._probe_selected()
    warning.assert_called_once()
    assert d.isEnabled() and not d._operation_running


def test_bad_rule_cannot_be_probed(dialog):
    d = dialog(rules=[dict(rule(), bind_port='bad')])
    assert not d._probe_btn.isEnabled()
    with patch.object(QMessageBox, 'warning'):
        assert not d._probe_selected()
    d._probe_cb.assert_not_called()
