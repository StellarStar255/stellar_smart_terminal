"""安全加固测试（第 7 条）

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_security_hardening.py -v

覆盖：
1. known_hosts 加载失败不再静默 —— 记日志 + 置降级标志 + 发信号
2. 注入终端的 HTTP 请求正文剥离控制字符（防键盘/终端转义注入）
3. STELLAR_OPENAI_API_KEY 可选开启 Bearer 鉴权，默认不破坏无鉴权行为
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPtyInputSanitization(unittest.TestCase):
    def test_strips_control_and_escape(self):
        from openai_server import _sanitize_pty_input
        # ESC 序列 + C0 控制符应被剥离
        dirty = "hello\x1b]0;pwned\x07 world\x00\x08\x1b[31m!"
        clean = _sanitize_pty_input(dirty)
        self.assertNotIn('\x1b', clean)
        self.assertNotIn('\x00', clean)
        self.assertNotIn('\x07', clean)
        self.assertEqual(clean, "hello]0;pwned world[31m!")

    def test_preserves_tab_and_newlines(self):
        from openai_server import _sanitize_pty_input
        text = "line1\n\tindented\r\nline2"
        self.assertEqual(_sanitize_pty_input(text), text)

    def test_strips_c1_and_del(self):
        from openai_server import _sanitize_pty_input
        self.assertEqual(_sanitize_pty_input("a\x7fb\x9cc"), "abc")

    def test_empty(self):
        from openai_server import _sanitize_pty_input
        self.assertEqual(_sanitize_pty_input(""), "")

    def test_build_input_sanitizes(self):
        """端到端：_build_input 汇聚请求正文后应已净化"""
        from openai_server import OpenAIRequestHandler
        handler = OpenAIRequestHandler.__new__(OpenAIRequestHandler)
        messages = [{'role': 'user', 'content': "run\x1b[2J\x00 now"}]
        out = handler._build_input(messages)
        self.assertNotIn('\x1b', out)
        self.assertNotIn('\x00', out)


class TestOptionalAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _make_handler(self, api_key: str):
        from openai_server import OpenAIRequestHandler, ServerConfig
        handler = OpenAIRequestHandler.__new__(OpenAIRequestHandler)
        handler.config = ServerConfig(port=0, api_key=api_key)
        return handler

    def test_no_key_allows_all(self):
        handler = self._make_handler('')
        handler.headers = {}
        self.assertTrue(handler._check_auth())

    def test_key_required_when_set(self):
        handler = self._make_handler('secret123')
        handler.headers = {}
        self.assertFalse(handler._check_auth())
        handler.headers = {'Authorization': 'Bearer wrong'}
        self.assertFalse(handler._check_auth())
        handler.headers = {'Authorization': 'Bearer secret123'}
        self.assertTrue(handler._check_auth())

    def test_env_var_wires_into_server(self):
        from openai_server import OpenAIServerManager
        mgr = OpenAIServerManager.__new__(OpenAIServerManager)
        mgr.servers = {}

        class _FakePort:
            def allocate(self, *a):
                return 8199
        mgr.port_allocator = _FakePort()

        captured = {}

        class _FakeServer:
            def __init__(self, term, config):
                captured['api_key'] = config.api_key
            tab_index = 0
            listening = mock.MagicMock()
            stopped = mock.MagicMock()
            error = mock.MagicMock()

            def start(self):
                pass

        with mock.patch.dict(os.environ, {'STELLAR_OPENAI_API_KEY': 'envkey'}):
            with mock.patch('openai_server.OpenAICompatServer', _FakeServer):
                mgr.start_server(0, terminal_widget=object(), port=8199)
        self.assertEqual(captured['api_key'], 'envkey')


class TestKnownHostsDegradation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_corrupt_known_hosts_emits_and_flags(self, ):
        import ssh_session
        from ssh_session import SSHSession, HostConfig

        host = HostConfig(alias='h', hostname='localhost', user='u', port=22)
        sess = SSHSession(host)
        signals = []
        sess.host_key_check_degraded.connect(lambda msg: signals.append(msg))

        # 让 load_host_keys 抛错，模拟 known_hosts 损坏/不可读
        class _BadClient:
            def load_system_host_keys(self):
                pass

            def load_host_keys(self, path):
                raise ValueError("corrupt known_hosts")

            def set_missing_host_key_policy(self, p):
                pass

            def connect(self, **kw):
                raise RuntimeError("stop here")  # 只测到加载阶段

        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: _BadClient()), \
             mock.patch.object(ssh_session.os.path, 'isfile', lambda p: True):
            with self.assertRaises(Exception):
                sess._connect_client(host)

        self.assertTrue(sess._host_key_degraded)
        self.app.processEvents()
        self.assertEqual(len(signals), 1)
        self.assertIn('known_hosts', signals[0])


if __name__ == '__main__':
    unittest.main()
