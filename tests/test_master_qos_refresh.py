"""升级后还活着的老主连接要换成按交互档打 IPQoS 的新主连接，转发才真提速。

ControlPersist=yes 的主连接跨应用重启常驻；修复前建的连接没有 .qos 记账文件。
非 MFA 主机在打开转发框 / 连上自动挂载时静默 exit + 重建；MFA 主机不动。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_master_qos_refresh.py -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402

import ssh_control  # noqa: E402
from ssh_session import HostConfig  # noqa: E402


def _proc(stdout=b"", rc=0):
    m = mock.Mock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = b""
    return m


class QosMarkerTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qos-')
        self.ctl = os.path.join(self.tmp, 'c-abc')
        self.host = HostConfig(alias='gpu', hostname='10.0.0.1', user='u', port=22)
        ssh_control._IPQOS_CACHE.clear()
        for name, val in (('is_supported', True), ('control_path_for', self.ctl)):
            p = mock.patch.object(ssh_control, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def test_master_via_keys_writes_qos_marker(self):
        with mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(b"ipqos ef cs0\n")):
            ssh_control.start_master_with_keys(self.host)
        with open(self.ctl + '.qos') as fh:
            self.assertEqual(fh.read().strip(), 'ef')

    def test_pre_qos_master_detected_by_missing_or_mismatched_marker(self):
        # ssh -G 恒定报 ef → 当前交互档 = ef
        with mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(b"ipqos ef cs0\n")):
            self.assertFalse(ssh_control.master_is_pre_qos(self.host))   # 没 socket
            open(self.ctl, 'w').close()
            self.assertTrue(ssh_control.master_is_pre_qos(self.host))    # 有 socket 没记账
            with open(self.ctl + '.qos', 'w') as fh:
                fh.write('af21')                                        # 老连接记的是低优先级档
            self.assertTrue(ssh_control.master_is_pre_qos(self.host),
                            "记的档(af21)与当前交互档(ef)不一致 → 应判为过期")
            with open(self.ctl + '.qos', 'w') as fh:
                fh.write('ef')                                          # 记的档 == 当前交互档
            self.assertFalse(ssh_control.master_is_pre_qos(self.host))

    def test_marker_records_exact_qos_used_not_a_second_probe(self):
        """建连算一次 qos、记账用同一个值：即使两次 ssh -G 取值会漂移，
        记的也是主连接真正打的档（否则 master_is_pre_qos 会误判成过期反复重建）。"""
        seq = [_proc(b"ipqos ef cs0\n"), _proc(b"ipqos af21 cs1\n")]
        with mock.patch.object(ssh_control.subprocess, 'run',
                               side_effect=lambda *a, **k: seq.pop(0)):
            ssh_control.start_master_with_keys(self.host)
        with open(self.ctl + '.qos') as fh:
            self.assertEqual(fh.read().strip(), 'ef')

    def test_refresh_master_exits_then_rebuilds(self):
        order = []
        with mock.patch.object(ssh_control, 'master_exit',
                               side_effect=lambda cfg: order.append('exit') or True), \
             mock.patch.object(ssh_control, 'ensure_master',
                               side_effect=lambda cfg, password='', hours=0: order.append(('ensure', password)) or self.ctl):
            ssh_control.refresh_master(self.host, password='pw')
        self.assertEqual(order, ['exit', ('ensure', 'pw')])


class PanelRefreshTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import remote_explorer_widget as rew
        self.rew = rew
        rew._ACTIVE_FORWARDS.clear()
        self.addCleanup(rew._ACTIVE_FORWARDS.clear)
        self.panel = rew.RemoteExplorerPanel(theme={})
        self.addCleanup(self.panel.deleteLater)
        self.host = HostConfig(alias='gpu', hostname='10.0.0.1', user='u', port=22)
        self.panel._active_forwards['gpu'] = {'L|127.0.0.1|8080'}

    def test_plain_host_pre_qos_master_is_rebuilt_and_registry_cleared(self):
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=False), \
             mock.patch.object(ssh_control, 'refresh_master', return_value='/tmp/ctl') as refresh:
            self.assertTrue(self.panel._ensure_master_interactive(self.host))
        refresh.assert_called_once()
        self.assertNotIn('gpu', self.panel._active_forwards)

    def test_mfa_host_pre_qos_prompts_decline_keeps_slow_master(self):
        """MFA 主连接过期：提示用户重登；用户选「否」→ 不静默重建，照旧用。"""
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=True), \
             mock.patch.object(QMessageBox, 'question',
                               return_value=QMessageBox.StandardButton.No) as q, \
             mock.patch.object(ssh_control, 'refresh_master') as refresh, \
             mock.patch.object(self.panel, '_mfa_login') as mfa:
            self.assertTrue(self.panel._ensure_master_interactive(self.host))
        q.assert_called_once()
        refresh.assert_not_called()
        mfa.assert_not_called()
        self.assertIn('gpu', self.panel._active_forwards)

    def test_mfa_host_pre_qos_prompt_accept_drops_and_relogins(self):
        """用户选「是」→ 断开主连接 + 重新 MFA 登录；转发框不再打开（返回 False）。"""
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=True), \
             mock.patch.object(QMessageBox, 'question',
                               return_value=QMessageBox.StandardButton.Yes), \
             mock.patch.object(self.panel, '_drop_master') as drop, \
             mock.patch.object(self.panel, '_mfa_login') as mfa:
            self.assertFalse(self.panel._ensure_master_interactive(self.host))
        drop.assert_called_once()
        mfa.assert_called_once()

    def test_new_master_not_touched(self):
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=False), \
             mock.patch.object(ssh_control, 'refresh_master') as refresh:
            self.assertTrue(self.panel._ensure_master_interactive(self.host))
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
