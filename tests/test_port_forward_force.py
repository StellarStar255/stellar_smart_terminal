"""端口转发 Start 撞上「本机端口已被占用：ssh(pid N)」时能强制释放。

典型场景：这台主机的常驻主连接上早就挂着同一条转发（应用重启后内存
登记丢了、主连接还活着），再 Start 一次就撞到自己。强制释放 = 先在主
连接上 `-O cancel` 同一条再重试；还不行才结束占端口的 ssh 进程；占端口
的不是 ssh 进程时绝不动它。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_port_forward_force.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import ssh_control  # noqa: E402
from ssh_session import HostConfig  # noqa: E402

_SPEC = {"type": "L", "bind_host": "127.0.0.1", "bind_port": "6013",
         "dest_host": "127.0.0.1", "dest_port": "6013"}


def _proc(rc=0, err=b""):
    m = mock.Mock()
    m.returncode = rc
    m.stdout = b""
    m.stderr = err
    return m


class ForceApplyLogicTest(unittest.TestCase):

    def setUp(self):
        self.host = HostConfig(alias='rd-13', hostname='10.10.64.13', user='root', port=2222)
        for name, val in (('is_supported', True),):
            p = mock.patch.object(ssh_control, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def test_busy_port_raises_structured_error(self):
        with mock.patch.object(ssh_control, 'local_port_busy', return_value=True), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('ssh', 14527)]):
            with self.assertRaises(ssh_control.LocalPortBusy) as cm:
                ssh_control.forward_apply(self.host, _SPEC)
        self.assertTrue(cm.exception.held_by_ssh_only())
        self.assertIn('6013', str(cm.exception))
        self.assertIn('14527', str(cm.exception))

    def test_force_cancels_on_master_first_and_does_not_kill(self):
        """撤销同一条后端口空出来 → 直接重挂，不碰任何进程。"""
        busy = [True, False, False]   # 初查占用；cancel 后空闲；forward 前的自查空闲
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return _proc(0)

        with mock.patch.object(ssh_control, 'local_port_busy', side_effect=lambda h, p: busy.pop(0)), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=run), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            ssh_control.forward_force_apply(self.host, _SPEC)
        kill.assert_not_called()
        ops = [a[2] for a in calls if a[0] == 'ssh' and a[1] == '-O']
        self.assertEqual(ops, ['cancel', 'forward'])

    def test_force_kills_ssh_holder_when_cancel_does_not_free(self):
        busy = [True, True, False]    # 初查占用；cancel 后仍占用；kill 后 forward 前的自查空闲
        with mock.patch.object(ssh_control, 'local_port_busy', side_effect=lambda h, p: busy.pop(0)), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('ssh', 14527)]), \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0)), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            ssh_control.forward_force_apply(self.host, _SPEC)
        kill.assert_called_once_with(14527)

    def test_force_refuses_to_kill_non_ssh_holder(self):
        busy = [True, True]
        with mock.patch.object(ssh_control, 'local_port_busy', side_effect=lambda h, p: busy.pop(0)), \
             mock.patch.object(ssh_control, 'port_holders', return_value=[('python3', 999)]), \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0)), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            with self.assertRaises(ssh_control.LocalPortBusy):
                ssh_control.forward_force_apply(self.host, _SPEC)
        kill.assert_not_called()


class ForceApplyDialogTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, apply_cb, force_cb):
        from remote_explorer_widget import _ForwardsDialog
        dlg = _ForwardsDialog(alias='rd-13', rules=[dict(_SPEC)],
                              apply_cb=apply_cb, cancel_cb=lambda s: None,
                              force_cb=force_cb)
        dlg._list.setCurrentRow(0)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_busy_by_ssh_offers_force_and_marks_active(self):
        forced = []

        def apply_cb(spec):
            raise ssh_control.LocalPortBusy(6013, [('ssh', 14527)])

        dlg = self._dialog(apply_cb, lambda spec: forced.append(spec))
        # 用户点了「强制释放并启用」（第一个加进去的按钮）
        with mock.patch.object(QMessageBox, 'exec', lambda self: 0), \
             mock.patch.object(QMessageBox, 'clickedButton', lambda self: self.buttons()[0]), \
             mock.patch.object(QMessageBox, 'warning') as warn:
            dlg._apply_selected(False)
        self.assertEqual(len(forced), 1)
        warn.assert_not_called()
        self.assertIn(dlg.rule_key(_SPEC), dlg.active_keys())

    def test_busy_by_other_process_only_warns(self):
        forced = []

        def apply_cb(spec):
            raise ssh_control.LocalPortBusy(6013, [('python3', 999)])

        dlg = self._dialog(apply_cb, lambda spec: forced.append(spec))
        with mock.patch.object(QMessageBox, 'exec', lambda self: 0) as ex, \
             mock.patch.object(QMessageBox, 'warning') as warn:
            dlg._apply_selected(False)
        self.assertEqual(forced, [])
        warn.assert_called_once()
        self.assertNotIn(dlg.rule_key(_SPEC), dlg.active_keys())
        del ex


if __name__ == '__main__':
    unittest.main()
