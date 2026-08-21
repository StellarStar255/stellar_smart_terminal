"""keyboard-interactive（OTP/2FA）认证单元测试（不依赖真实 SSH）：

- 密钥/密码都不行、服务器走多步提示（密码 + 验证码）时能连上
- 提示回答被逐条转给 interactive provider，echo 标志原样传递
- 密码类回答缓存进 _auth_secrets（自动重连自动作答），验证码不缓存
- 缓存密码存在时只向用户要验证码（重连场景少弹一次框）
- 服务器不支持 keyboard-interactive → 回落原有单密码逻辑
- 用户取消 → 抛 AuthenticationException，连接失败而非卡死

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_ssh_keyboard_interactive -v
"""
import os
import sys
import unittest
from unittest import mock

import paramiko

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeTransport:
    """两步 keyboard-interactive：先要密码（echo 关），再要验证码（echo 开）。"""

    def __init__(self, password="secret", otp="123456"):
        self.password = password
        self.otp = otp
        self.keepalive = None
        self.auth_username = None

    def is_active(self):
        return True

    def auth_interactive(self, username, handler, submethods=""):
        self.auth_username = username
        answers = handler("login", "", [("Password: ", False)])
        if answers != [self.password]:
            raise paramiko.AuthenticationException("bad password")
        answers = handler("login", "", [("Verification code: ", True)])
        if answers != [self.otp]:
            raise paramiko.AuthenticationException("bad verification code")
        return []

    def set_keepalive(self, interval):
        self.keepalive = interval


class _FakeClient:
    """connect() 永远认证失败（模拟无可用密钥），transport 仍存活可续认证。"""

    def __init__(self, transport):
        self._transport = transport
        self.connect_calls = []

    def load_system_host_keys(self):
        pass

    def load_host_keys(self, path):
        pass

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        raise paramiko.AuthenticationException("no valid key")

    def get_transport(self):
        return self._transport

    def save_host_keys(self, path):
        pass

    def close(self):
        pass


def _make_session():
    from ssh_session import SSHSession, HostConfig
    host = HostConfig(alias='otp-host', hostname='example.com', user='u', port=22)
    return SSHSession(host), host


class TestKeyboardInteractiveAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _connect_with(self, sess, host, client):
        import ssh_session
        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: client):
            return sess._connect_client(host)

    def test_otp_host_connects_via_keyboard_interactive(self):
        """密码 + 验证码两步提示的主机：逐条转给 provider 后连接成功。"""
        sess, host = _make_session()
        prompts_seen = []

        def provider(alias, prompt, echo):
            prompts_seen.append((alias, prompt, echo))
            return "secret" if "password" in prompt.lower() else "123456"

        sess._interactive_provider = provider
        transport = _FakeTransport()
        client = self._connect_with(sess, host, _FakeClient(transport))

        self.assertIsNotNone(client)
        self.assertEqual(transport.auth_username, 'u')
        self.assertEqual(prompts_seen, [
            ('otp-host', 'Password:', False),
            ('otp-host', 'Verification code:', True),
        ])
        # 密码缓存供自动重连；一次性验证码不缓存
        self.assertEqual(sess._auth_secrets['otp-host'].get('password'), 'secret')
        self.assertNotIn('123456', str(sess._auth_secrets))
        # keep-alive 正常设置（走完了 _connect_client 全程）
        self.assertEqual(transport.keepalive, 30)

    def test_cached_password_only_prompts_for_otp(self):
        """重连场景：密码步骤用缓存自动作答，只向用户要验证码。"""
        sess, host = _make_session()
        sess._auth_secrets['otp-host'] = {'password': 'secret'}
        prompts_seen = []

        def provider(alias, prompt, echo):
            prompts_seen.append(prompt)
            return "123456"

        sess._interactive_provider = provider
        self._connect_with(sess, host, _FakeClient(_FakeTransport()))
        self.assertEqual(prompts_seen, ['Verification code:'])

    def test_falls_back_to_password_when_kbd_not_supported(self):
        """服务器不收 keyboard-interactive → 原有单密码回退不受影响。"""
        sess, host = _make_session()

        class _NoKbdTransport(_FakeTransport):
            def auth_interactive(self, username, handler, submethods=""):
                raise paramiko.BadAuthenticationType(
                    "kbd not allowed", ["password"])

        class _PasswordClient(_FakeClient):
            def connect(self, **kwargs):
                self.connect_calls.append(kwargs)
                if kwargs.get('password') == 'pw':
                    return  # 密码认证成功
                raise paramiko.AuthenticationException("auth failed")

        sess._interactive_provider = lambda alias, prompt, echo: self.fail(
            "interactive provider should not be asked")
        sess._password_provider = lambda label: 'pw'
        client = _PasswordClient(_NoKbdTransport())
        self._connect_with(sess, host, client)
        self.assertEqual(client.connect_calls[-1].get('password'), 'pw')
        self.assertEqual(sess._auth_secrets['otp-host'].get('password'), 'pw')

    def test_user_cancel_raises_auth_error(self):
        """任一步提示被取消 → 抛 AuthenticationException（连接失败，不挂起）。"""
        sess, host = _make_session()
        sess._interactive_provider = lambda alias, prompt, echo: None
        with self.assertRaises(paramiko.AuthenticationException):
            self._connect_with(sess, host, _FakeClient(_FakeTransport()))


if __name__ == '__main__':
    unittest.main()
