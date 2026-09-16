"""非 MFA 主机「老主连接换新」不能掐断搭在它上面的终端标签（2026-09 审查 B 项）。

refresh_master = `-O exit` + 重建：所有 `ControlMaster=no` 搭车的终端 ssh 一起断。
以前 _auto_apply_forwards 连上就无条件重建（连一条自动转发都没有也重建）；
有终端搭车时更不能静默重建，改成一次性提示并跳过。另外 ssh -G 瞬时失败
回退到 ef 时，不能拿回退值去判「记账不一致」——那会每次都判成过期反复重建。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_master_refresh_riders_2026_09_ssh.py -q
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402

import ssh_control  # noqa: E402
from ssh_session import HostConfig  # noqa: E402


def _proc(rc=0, out=b"", err=b""):
    m = mock.Mock()
    m.returncode = rc
    m.stdout = out
    m.stderr = err
    return m


class _PanelBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app_config
        self._orig = app_config.get_config_path
        self.tmp = tempfile.mkdtemp(prefix='riders-')
        app_config.get_config_path = lambda: Path(self.tmp) / 'config.json'
        import remote_explorer_widget as rew
        self.rew = rew
        rew._ACTIVE_FORWARDS.clear()
        self.addCleanup(rew._ACTIVE_FORWARDS.clear)
        self.panel = rew.RemoteExplorerPanel(theme={})
        self.addCleanup(self.panel.deleteLater)
        self.host = HostConfig(alias='gpu', hostname='10.0.0.1', user='u', port=22)
        self.errors = []
        self.panel.error_occurred.connect(self.errors.append)

    def tearDown(self):
        import app_config
        app_config.get_config_path = self._orig


class AutoForwardsDoNotRebuildWithoutPending(_PanelBase):

    def test_no_pending_rules_means_no_refresh(self):
        """没有要自动挂的转发 → 根本别碰主连接（重建会掐断终端标签）。"""
        with mock.patch.object(ssh_control, 'is_supported', return_value=True), \
             mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=False), \
             mock.patch.object(ssh_control, 'master_riders', return_value=[], create=True), \
             mock.patch.object(ssh_control, 'refresh_master', return_value='/tmp/ctl') as refresh:
            self.panel._auto_apply_forwards(self.host)
        refresh.assert_not_called()

    def test_pending_rules_still_refresh_then_apply(self):
        """对照：有待挂的规则、没人搭车 → 照旧重建再挂。"""
        self.panel._save_forwards('gpu', [
            {'type': 'L', 'bind_port': 8888, 'dest_port': 8888, 'auto': True}])
        order = []
        with mock.patch.object(ssh_control, 'is_supported', return_value=True), \
             mock.patch.object(ssh_control, 'master_socket_exists', return_value=True), \
             mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=False), \
             mock.patch.object(ssh_control, 'master_riders', return_value=[], create=True), \
             mock.patch.object(ssh_control, 'refresh_master',
                               side_effect=lambda h, password='': order.append('refresh') or '/tmp/ctl'), \
             mock.patch.object(ssh_control, 'forward_apply',
                               side_effect=lambda h, spec, cancel=False: order.append('fwd')):
            self.panel._auto_apply_forwards(self.host)
        self.assertEqual(order, ['refresh', 'fwd'])


class RidersBlockSilentRebuild(_PanelBase):

    def test_riding_terminal_skips_rebuild_and_notifies_once(self):
        with mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=False), \
             mock.patch.object(ssh_control, 'master_riders', return_value=[4321], create=True), \
             mock.patch.object(ssh_control, 'refresh_master') as refresh:
            self.assertFalse(self.panel._refresh_pre_qos_master(self.host))
            self.assertFalse(self.panel._refresh_pre_qos_master(self.host))
        refresh.assert_not_called()
        self.assertEqual(len(self.errors), 1, "提示只发一次，别每次连接都刷屏")
        self.assertIn('gpu', self.errors[0])

    def test_no_riders_rebuilds_as_before(self):
        self.panel._active_forwards['gpu'] = {'L|127.0.0.1|8080'}
        with mock.patch.object(ssh_control, 'master_is_pre_qos', return_value=True), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=False), \
             mock.patch.object(ssh_control, 'master_riders', return_value=[], create=True), \
             mock.patch.object(ssh_control, 'refresh_master', return_value='/tmp/ctl') as refresh:
            self.assertTrue(self.panel._refresh_pre_qos_master(self.host))
        refresh.assert_called_once()
        self.assertNotIn('gpu', self.panel._active_forwards)
        self.assertEqual(self.errors, [])


class RidersDetection(unittest.TestCase):

    def test_master_riders_lists_control_master_no_clients_only(self):
        host = HostConfig(alias='gpu', hostname='10.0.0.1', user='u', port=22)
        ctl = '/tmp/stellar-ssh/c-abc'
        ps = (f"  100 ssh -o BatchMode=yes -o ControlMaster=yes -o ControlPath={ctl} -o ControlPersist=8h -N -f gpu\n"
              f"  101 ssh -o ControlMaster=no -o ControlPath={ctl} -o ServerAliveInterval=15 gpu\n"
              f"  102 ssh -o BatchMode=yes -o ControlMaster=no -o ControlPath={ctl} -o PubkeyAuthentication=no gpu ls\n"
              f"  103 ssh -O check -o ControlPath={ctl} gpu\n"
              f"  104 ssh -o ControlMaster=no -o ControlPath=/tmp/stellar-ssh/c-zzz gpu\n").encode()
        with mock.patch.object(ssh_control, 'is_supported', return_value=True), \
             mock.patch.object(ssh_control, 'control_path_for', return_value=ctl), \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0, ps)):
            self.assertEqual(ssh_control.master_riders(host), [101])


class FallbackQosDoesNotFlagMasterAsStale(unittest.TestCase):
    """ssh -G 失败回退 ef 的那一次，不能拿回退值去和记账比——比出「不一致」就会
    每次连接都重建一遍主连接。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='qos-')
        self.ctl = os.path.join(self.tmp, 'c-abc')
        self.host = HostConfig(alias='gpu', hostname='10.0.0.1', user='u', port=22)
        ssh_control._IPQOS_CACHE.clear()
        self.addCleanup(ssh_control._IPQOS_CACHE.clear)
        for name, val in (('is_supported', True), ('control_path_for', self.ctl)):
            p = mock.patch.object(ssh_control, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        open(self.ctl, 'w').close()

    def test_probe_failure_means_unknown_not_stale(self):
        with open(self.ctl + '.qos', 'w') as fh:
            fh.write('af21')
        with mock.patch.object(ssh_control.subprocess, 'run', side_effect=OSError('no ssh')):
            self.assertFalse(ssh_control.master_is_pre_qos(self.host))

    def test_missing_marker_is_still_stale(self):
        """没记账 = 修复前的老连接，这个判断不依赖 ssh -G。"""
        with mock.patch.object(ssh_control.subprocess, 'run', side_effect=OSError('no ssh')):
            self.assertTrue(ssh_control.master_is_pre_qos(self.host))

    def test_real_mismatch_is_still_stale(self):
        with open(self.ctl + '.qos', 'w') as fh:
            fh.write('af21')
        with mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(0, b"ipqos ef cs0\n")):
            self.assertTrue(ssh_control.master_is_pre_qos(self.host))


if __name__ == '__main__':
    unittest.main()
