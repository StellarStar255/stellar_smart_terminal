# -*- coding: utf-8 -*-
"""Remote 面板认证提示与会话生命周期的回归（审查清单 2026-09）：

- 密码/交互提示的工作线程侧等待必须有截止：以前 `while not done: sleep`
  唯一出口是 UI 槽置位，面板断开/关闭后线程永久自旋，退出时解释器 join 卡死。
- 已连着别的主机时做 MFA 登录：_connect_to 里的 _disconnect() 会清掉刚缓存的
  密码，SSH 终端标签自动回填与新窗口 prime 全部失效。
- 连接失败时丢弃的会话要 disconnect()，否则每次失败留一个空闲 executor 线程。
- 远端文件缓存不能落在可预测的共享临时目录（/tmp/smart_terminal_remote_<alias>）：
  改成进程级 mkdtemp（0700）根目录。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_auth_prompt.py -q
"""
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeSession:
    def __init__(self, alias='bastion'):
        from ssh_session import HostConfig
        self.host_config = HostConfig(alias=alias, hostname='h', user='u')
        self.disconnect_calls = 0

    def used_otp_auth(self):
        return False

    def home(self):
        return '/home/u'

    def is_connected(self):
        return True

    def listdir(self, *_a, **_k):
        return []

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        fut.set_result(fn(*args, **kwargs))
        return fut

    def invalidate_cache(self, *_a):
        pass

    def disconnect(self):
        self.disconnect_calls += 1


class _PanelBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app_config
        self._tmp_dir = tempfile.mkdtemp()
        self._tmp_cfg = Path(self._tmp_dir) / "cfg.json"
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._tmp_cfg

    def tearDown(self):
        import app_config
        app_config.get_config_path = self._orig_get_path

    def _panel(self):
        from remote_explorer_widget import RemoteExplorerPanel
        return RemoteExplorerPanel(theme={})

    def _host(self, alias='bastion'):
        from ssh_session import HostConfig
        return HostConfig(alias=alias, hostname='h', user='u')


class TestPromptWaitIsBounded(_PanelBase):
    def _run_in_thread(self, fn):
        out = {}

        def _t():
            out['v'] = fn()
        th = threading.Thread(target=_t, daemon=True)
        th.start()
        return th, out

    def test_disconnect_unblocks_password_prompt(self):
        panel = self._panel()
        # 不让信号真的弹 QInputDialog（offscreen 下会挂）
        panel._password_prompt_signal.disconnect()
        th, out = self._run_in_thread(lambda: panel._prompt_password('bastion'))
        time.sleep(0.2)
        self.assertTrue(th.is_alive())
        panel._disconnect()
        th.join(2.0)
        self.assertFalse(th.is_alive(), '断开后工作线程仍在等提示答案')
        self.assertIsNone(out.get('v'))

    def test_close_unblocks_interactive_prompt(self):
        from PyQt6.QtGui import QCloseEvent
        panel = self._panel()
        panel._interactive_prompt_signal.disconnect()
        th, out = self._run_in_thread(
            lambda: panel._prompt_interactive('bastion', 'Verification code:', True))
        time.sleep(0.2)
        self.assertTrue(th.is_alive())
        panel.closeEvent(QCloseEvent())
        th.join(2.0)
        self.assertFalse(th.is_alive(), '关闭面板后工作线程仍在等提示答案')
        self.assertIsNone(out.get('v'))

    def test_prompt_times_out(self):
        panel = self._panel()
        panel._password_prompt_signal.disconnect()
        with mock.patch.object(type(panel), '_PROMPT_WAIT_SECS', 0.3):
            th, out = self._run_in_thread(lambda: panel._prompt_password('bastion'))
            th.join(3.0)
        self.assertFalse(th.is_alive(), '提示等待没有截止时间')
        self.assertIsNone(out.get('v'))


class TestMfaPasswordSurvivesReconnect(_PanelBase):
    def test_password_cached_after_switching_hosts(self):
        """已连着 A 时对 B 做 MFA 登录：_connect_to 先 _disconnect（清缓存），
        刚从登录框拿到的密码必须在之后仍可取到。"""
        import remote_explorer_widget as rew
        panel = self._panel()
        panel._session = _FakeSession('other')
        created = []

        class _Sess:
            def __init__(self, host, parent=None):
                self.host_config = host
                created.append(self)
                from PyQt6.QtCore import QObject, pyqtSignal

                class _Sig(QObject):
                    connected = pyqtSignal()
                    connect_failed = pyqtSignal(str)
                    host_key_check_degraded = pyqtSignal(str)
                sig = _Sig()
                self.connected = sig.connected
                self.connect_failed = sig.connect_failed
                self.host_key_check_degraded = sig.host_key_check_degraded
                self._sig = sig

            def connect_async(self, **kw):
                return Future()

            def disconnect(self):
                pass

        with mock.patch.object(rew.ssh_control, 'is_supported', lambda: False), \
             mock.patch.object(rew, 'SSHSession', _Sess):
            panel._connect_to(self._host('bastion'), mfa_answers={
                'alias': 'bastion', 'code': '123456', 'code_used': False,
                'password': 'hunter2', 'password_used': False})
        self.assertEqual(panel.get_cached_password('bastion'), 'hunter2')
        self.assertTrue(created)


class TestFailedSessionIsReleased(_PanelBase):
    def test_connect_failed_disconnects_discarded_session(self):
        from PyQt6.QtWidgets import QMessageBox
        panel = self._panel()
        sess = _FakeSession()
        panel._session = sess
        with mock.patch.object(QMessageBox, 'warning', lambda *a, **k: None):
            panel._on_session_connect_failed(sess, 'boom')
        self.assertIsNone(panel._session)
        self.assertEqual(sess.disconnect_calls, 1, '失败的会话没有 disconnect()')

    def test_stale_master_path_also_disconnects(self):
        panel = self._panel()
        sess = _FakeSession()
        panel._session = sess
        panel._mfa_reuse_attempt = True
        with mock.patch.object(type(panel), '_mfa_login', lambda self, h, **k: None):
            panel._on_session_connect_failed(sess, 'stale')
        self.assertEqual(sess.disconnect_calls, 1)


class TestRemoteCacheRootIsPrivate(_PanelBase):
    def test_cache_path_under_private_root(self):
        import remote_explorer_widget as rew
        panel = self._panel()
        p = panel._temp_local_path_for('bastion', '/srv/app/config.yaml', 'config.yaml')
        shared = os.path.join(tempfile.gettempdir(), 'smart_terminal_remote_bastion')
        self.assertFalse(p.startswith(shared), p)
        root = rew._remote_cache_root()
        self.assertTrue(p.startswith(root + os.sep), (p, root))
        self.assertTrue(p.endswith(os.path.join('srv', 'app', 'config.yaml')))
        if not sys.platform.startswith('win'):
            self.assertEqual(stat.S_IMODE(os.stat(root).st_mode) & 0o077, 0)

    def test_root_is_stable_within_process(self):
        import remote_explorer_widget as rew
        self.assertEqual(rew._remote_cache_root(), rew._remote_cache_root())


if __name__ == '__main__':
    unittest.main()
