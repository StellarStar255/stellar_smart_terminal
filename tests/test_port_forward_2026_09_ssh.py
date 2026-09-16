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

    def test_other_ssh_holders_are_killed_but_own_master_is_not(self):
        """占端口的两条 ssh 里有一条是本主机主连接（-O check 报的 pid）→ 只杀另一条，
        自己的主连接（非 MFA）走 refresh_master 重建。"""
        busy = [True, True, False]
        with mock.patch.object(ssh_control, 'local_port_busy', side_effect=lambda h, p: busy.pop(0)), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('ssh', 100), ('ssh', 200)]), \
             mock.patch.object(ssh_control, 'master_pid', return_value=100, create=True), \
             mock.patch.object(ssh_control, 'refresh_master', return_value=self.ctl) as refresh, \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0)), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            ssh_control.forward_force_apply(self.host, _SPEC, mfa=False, password='pw')
        self.assertEqual([c.args[0] for c in kill.call_args_list], [200])
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.kwargs.get('password'), 'pw')

    def test_mfa_host_whose_master_holds_the_port_asks_for_relogin(self):
        busy = [True, True]
        with mock.patch.object(ssh_control, 'local_port_busy', side_effect=lambda h, p: busy.pop(0)), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('ssh', 100)]), \
             mock.patch.object(ssh_control, 'master_pid', return_value=100, create=True), \
             mock.patch.object(ssh_control, 'refresh_master') as refresh, \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0)), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            with self.assertRaises(RuntimeError) as cm:
                ssh_control.forward_force_apply(self.host, _SPEC, mfa=True)
        kill.assert_not_called()
        refresh.assert_not_called()
        self.assertIn('登录', str(cm.exception))
        self.assertNotIsInstance(cm.exception, ssh_control.MasterNotRunning)

    def test_socket_gone_after_kill_is_a_plain_error_not_the_mfa_text(self):
        """杀完占端口的 ssh 发现自己的 socket 没了 → 明确报错，而不是让 forward_apply
        甩出「先做一次 MFA 登录」（普通主机根本没有 MFA）。"""
        busy = [True, True, False]

        def kill(pid):
            os.unlink(self.ctl)

        def run(argv, **kw):
            if argv[:3] == ['ssh', '-O', 'forward']:
                return _proc(255, b"", b"Control socket connect(/tmp/c-abc): No such file or directory\n")
            return _proc(0)

        with mock.patch.object(ssh_control, 'local_port_busy', side_effect=lambda h, p: busy.pop(0)), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('ssh', 300)]), \
             mock.patch.object(ssh_control, 'master_pid', return_value=0, create=True), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=run), \
             mock.patch.object(ssh_control, '_kill_wait', side_effect=kill):
            with self.assertRaises(RuntimeError) as cm:
                ssh_control.forward_force_apply(self.host, _SPEC)
        self.assertNotIsInstance(cm.exception, ssh_control.MasterNotRunning)
        self.assertNotIn('MFA', str(cm.exception))
        self.assertIn('6013', str(cm.exception))


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
