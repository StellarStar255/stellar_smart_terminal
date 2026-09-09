# -*- coding: utf-8 -*-
"""不需要动态码的普通主机也能用端口转发：没有主连接就自己建一条。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_port_forward_no_mfa.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

import ssh_control
from ssh_session import HostConfig


def _proc(rc, err=b""):
    m = mock.Mock()
    m.returncode = rc
    m.stdout = b""
    m.stderr = err
    return m


class TestEnsureMaster(unittest.TestCase):
    def setUp(self):
        self.host = HostConfig(alias='gpu-main1', hostname='10.10.80.70', user='huang', port=2222)
        # Windows 的 OpenSSH 没有 ControlMaster → is_supported() 为 False；这里只测
        # 决策逻辑（subprocess 已桩掉），把平台判断也桩成支持
        p = mock.patch.object(ssh_control, 'is_supported', return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_alive_master_is_reused(self):
        with mock.patch.object(ssh_control, 'master_alive', return_value=True), \
             mock.patch.object(ssh_control.subprocess, 'run') as run:
            ctl = ssh_control.ensure_master(self.host)
        self.assertEqual(ctl, ssh_control.control_path_for(self.host))
        run.assert_not_called()

    def test_keys_first_batch_mode_no_prompt(self):
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0)) as run:
            ctl = ssh_control.ensure_master(self.host)
        self.assertEqual(ctl, ssh_control.control_path_for(self.host))
        args = run.call_args[0][0]
        self.assertIn('BatchMode=yes', args)          # 绝不弹交互提示
        self.assertIn('ControlMaster=yes', args)
        self.assertIn('Port=2222', args)
        self.assertEqual(args[-1], 'huang@10.10.80.70')

    def test_password_fallback_uses_askpass_login(self):
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(255, b"Permission denied (publickey,password)")), \
             mock.patch.object(ssh_control, 'mfa_login', return_value='/tmp/ctl') as login:
            ctl = ssh_control.ensure_master(self.host, password='s3cret')
        self.assertEqual(ctl, '/tmp/ctl')
        login.assert_called_once()
        self.assertEqual(login.call_args.kwargs.get('password'), 's3cret')
        self.assertEqual(login.call_args.kwargs.get('code'), '')

    def test_no_password_raises_needs_password(self):
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(255, b"Permission denied (publickey,password)")):
            with self.assertRaises(ssh_control.NeedsPassword):
                ssh_control.ensure_master(self.host)


class TestPanelGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from remote_explorer_widget import RemoteExplorerPanel
        cls.panel = RemoteExplorerPanel()

    @classmethod
    def tearDownClass(cls):
        cls.panel.close()
        cls.panel.deleteLater()
        cls.app.processEvents()

    def test_plain_host_builds_master_instead_of_mfa_message(self):
        host = HostConfig(alias='plain', hostname='1.2.3.4', user='u')
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=False), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=False), \
             mock.patch.object(ssh_control, 'ensure_master', return_value='/tmp/ctl') as ens, \
             mock.patch('remote_explorer_widget.QMessageBox.information') as info:
            self.assertTrue(self.panel._ensure_master_interactive(host))
        ens.assert_called_once()
        info.assert_not_called()

    def test_mfa_host_still_points_to_mfa_login(self):
        host = HostConfig(alias='bastion', hostname='b', user='u')
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=False), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=True), \
             mock.patch.object(ssh_control, 'ensure_master') as ens, \
             mock.patch('remote_explorer_widget.QMessageBox.information') as info:
            self.assertFalse(self.panel._ensure_master_interactive(host))
        ens.assert_not_called()
        info.assert_called_once()

    def test_asks_for_password_once_when_keys_fail(self):
        host = HostConfig(alias='pwhost', hostname='1.2.3.5', user='u')
        calls = []

        def fake_ensure(cfg, password=""):
            calls.append(password)
            if not password:
                raise ssh_control.NeedsPassword("Permission denied")
            return '/tmp/ctl'

        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=False), \
             mock.patch.object(self.panel, '_is_mfa_host', return_value=False), \
             mock.patch.object(ssh_control, 'ensure_master', side_effect=fake_ensure), \
             mock.patch('remote_explorer_widget.QInputDialog.getText', return_value=('pw!', True)):
            self.assertTrue(self.panel._ensure_master_interactive(host))
        self.assertEqual(calls, ['', 'pw!'])
        self.assertEqual(self.panel.get_cached_password('pwhost'), 'pw!')


if __name__ == '__main__':
    unittest.main()
