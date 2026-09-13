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

    def test_pre_qos_master_detected_only_when_socket_exists_without_marker(self):
        self.assertFalse(ssh_control.master_is_pre_qos(self.host))   # 没 socket
        open(self.ctl, 'w').close()
        self.assertTrue(ssh_control.master_is_pre_qos(self.host))    # 有 socket 没记账
        open(self.ctl + '.qos', 'w').close()
        self.assertFalse(ssh_control.master_is_pre_qos(self.host))   # 已记账

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

    def test_mfa_host_pre_qos_master_left_alone(self):
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=True), \
             mock.patch.object(ssh_control, 'refresh_master') as refresh:
            self.assertTrue(self.panel._ensure_master_interactive(self.host))
        refresh.assert_not_called()
        self.assertIn('gpu', self.panel._active_forwards)

    def test_new_master_not_touched(self):
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=False), \
             mock.patch.object(ssh_control, 'refresh_master') as refresh:
            self.assertTrue(self.panel._ensure_master_interactive(self.host))
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
