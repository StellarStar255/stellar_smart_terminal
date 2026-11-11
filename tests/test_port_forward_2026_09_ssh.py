"""端口转发三处修补（2026-09 审查 D 项）。

① 强制释放只按进程名 "ssh" 杀，会把本主机自己的主连接一起杀掉；
② 转发框「删除」时撤销失败也把规则条目删了，留下孤儿监听；
③ ensure_master 明明拿到了密码还要先拿密钥撞一次（堡垒机 MaxAuthTries），
   且内存态主机指定了 IdentityFile 时 agent 里的钥匙照样一把把试。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_port_forward_2026_09_ssh.py -q
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import ssh_control  # noqa: E402
from ssh_session import HostConfig  # noqa: E402

_SPEC = {"type": "L", "bind_host": "127.0.0.1", "bind_port": "6013",
         "dest_host": "127.0.0.1", "dest_port": "6013"}


def _proc(rc=0, out=b"", err=b""):
    m = mock.Mock()
    m.returncode = rc
    m.stdout = out
    m.stderr = err
    return m


class ForceApplySparesOwnMaster(unittest.TestCase):

    def setUp(self):
        self.host = HostConfig(alias='rd-13', hostname='10.10.64.13', user='root', port=2222)
        self.tmp = tempfile.mkdtemp(prefix='fwd-')
        self.ctl = os.path.join(self.tmp, 'c-abc')
        for name, val in (('is_supported', True), ('control_path_for', self.ctl)):
            p = mock.patch.object(ssh_control, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        open(self.ctl, 'w').close()

    def test_owned_rule_still_busy_never_kills_or_rebuilds_master(self):
        with mock.patch.object(ssh_control, 'local_port_busy', return_value=True), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('ssh', 100)]), \
             mock.patch.object(ssh_control, '_forward_owned', return_value=True), \
             mock.patch.object(ssh_control, 'refresh_master') as refresh, \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0)), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            with self.assertRaises(ssh_control.LocalPortBusy):
                ssh_control.forward_force_apply(self.host, _SPEC)
        kill.assert_not_called()
        refresh.assert_not_called()

    def test_mfa_unknown_owner_never_reauthenticates_or_cancels(self):
        with mock.patch.object(ssh_control, 'local_port_busy', return_value=True), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('ssh', 100)]), \
             mock.patch.object(ssh_control, '_forward_owned', return_value=False), \
             mock.patch.object(ssh_control, 'forward_apply') as apply, \
             mock.patch.object(ssh_control, 'refresh_master') as refresh, \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            with self.assertRaises(ssh_control.LocalPortBusy):
                ssh_control.forward_force_apply(self.host, _SPEC, mfa=True)
        apply.assert_not_called()
        kill.assert_not_called()
        refresh.assert_not_called()


class DialogDeleteKeepsRuleWhenCancelFails(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, cancel_cb):
        from remote_explorer_widget import _ForwardsDialog
        dlg = _ForwardsDialog(alias='rd-13', rules=[dict(_SPEC)],
                              apply_cb=lambda s: None, cancel_cb=cancel_cb,
                              active={_ForwardsDialog.rule_key(_SPEC)})
        dlg._list.setCurrentRow(0)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_failed_cancel_leaves_the_item_in_the_list(self):
        def cancel_cb(spec):
            raise RuntimeError('主连接卡住了')

        dlg = self._dialog(cancel_cb)
        with mock.patch.object(QMessageBox, 'warning') as warn:
            dlg._on_delete()
        warn.assert_called_once()
        self.assertEqual(dlg._list.count(), 1, "撤不掉就不能把规则删掉（否则孤儿监听没人管）")
        self.assertIn(dlg.rule_key(_SPEC), dlg.active_keys())

    def test_successful_cancel_removes_the_item(self):
        dlg = self._dialog(lambda spec: None)
        dlg._on_delete()
        self.assertEqual(dlg._list.count(), 0)
        self.assertNotIn(dlg.rule_key(_SPEC), dlg.active_keys())

    def test_ssh_zero_exit_cancel_failure_keeps_rule_and_active_state(self):
        host = HostConfig(alias='rd-13', hostname='10.10.64.13', user='root', port=2222)
        dlg = self._dialog(lambda spec: ssh_control.forward_apply(host, spec, cancel=True))
        with mock.patch.object(ssh_control, 'is_supported', return_value=True), \
             mock.patch.object(ssh_control, 'control_path_for', return_value='/tmp/fwd-test'), \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(
                 0, err=b'mux_client_forward: forwarding request failed: port not forwarded\n'
                        b'master cancel forward request failed\n')), \
             mock.patch.object(QMessageBox, 'warning') as warn:
            self.assertFalse(dlg._apply_selected(True))
            dlg._on_delete()
        self.assertEqual(warn.call_count, 2)
        self.assertEqual(dlg._list.count(), 1)
        self.assertIn(dlg.rule_key(_SPEC), dlg.active_keys())


class EnsureMasterAuth(unittest.TestCase):

    def setUp(self):
        self.host = HostConfig(alias='gpu-main1', hostname='10.10.80.70', user='huang', port=2222)
        p = mock.patch.object(ssh_control, 'is_supported', return_value=True)
        p.start()
        self.addCleanup(p.stop)
        ssh_control._IPQOS_CACHE.clear()
        self.addCleanup(ssh_control._IPQOS_CACHE.clear)

    def test_password_given_skips_the_key_attempt(self):
        """拿到密码就直接走 askpass 登录，别先拿密钥去撞一次（堡垒机会计 MaxAuthTries）。"""
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control, '_clear_stale_socket', return_value=False, create=True), \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0)) as run, \
             mock.patch.object(ssh_control, 'mfa_login', return_value='/tmp/ctl') as login:
            ctl = ssh_control.ensure_master(self.host, password='s3cret')
        self.assertEqual(ctl, '/tmp/ctl')
        login.assert_called_once()
        self.assertEqual(login.call_args.kwargs.get('password'), 's3cret')
        batch_attempts = [c for c in run.call_args_list if '-N' in c.args[0]]
        self.assertEqual(batch_attempts, [], "有密码就不该再起 BatchMode 的密钥尝试")

    def test_batch_master_pins_identities_only_when_identity_file_given(self):
        key = tempfile.NamedTemporaryFile(prefix='id_', delete=False)
        key.close()
        self.addCleanup(os.unlink, key.name)
        host = HostConfig(alias='mem', hostname='10.0.0.2', user='u', port=22,
                          identity_file=key.name)
        with mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(0, b"ipqos ef cs0\n")):
            args = ssh_control._master_args(host, '/tmp/ctl', 'yes', batch=True)
            plain = ssh_control._master_args(self.host, '/tmp/ctl', 'yes', batch=True)
        self.assertIn(f'IdentityFile={key.name}', args)
        self.assertIn('IdentitiesOnly=yes', args)
        self.assertNotIn('IdentitiesOnly=yes', plain)


if __name__ == '__main__':
    unittest.main()
