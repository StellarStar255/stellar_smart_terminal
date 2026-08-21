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


class _OtpOnlyTransport(_FakeTransport):
    """2FA 场景：publickey 部分成功后服务器只再要一步验证码。"""

    def auth_interactive(self, username, handler, submethods=""):
        self.auth_username = username
        answers = handler("MFA", "Please Enter MFA Code.",
                          [("[OTP Code]:", True)])
        if answers != [self.otp]:
            raise paramiko.AuthenticationException("bad verification code")
        return []


class TestTwoFactorAndRecovery(unittest.TestCase):
    """2FA（key 部分成功）与「认证尝试过多被掐线」两条真实故障路径。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_no_stdin_transport_blocks_dumb_interactive(self):
        """paramiko 2FA 内部回退的 auth_interactive_dumb 用 stdin input() 问
        验证码（GUI 下 EOFError/卡死）——必须被替换成可控的认证失败。"""
        from ssh_session import _NoStdinTransport
        with self.assertRaises(paramiko.AuthenticationException):
            _NoStdinTransport.auth_interactive_dumb(
                object.__new__(_NoStdinTransport), 'u')

    def test_connect_kwargs_use_no_stdin_transport_factory(self):
        """_connect_client 的首次 connect 必须带 transport_factory，
        否则 2FA 主机会掉进 paramiko 的 stdin 交互。"""
        from ssh_session import _NoStdinTransport
        sess, host = _make_session()
        sess._interactive_provider = lambda alias, prompt, echo: "123456"
        client = _FakeClient(_OtpOnlyTransport())
        import ssh_session
        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: client):
            sess._connect_client(host)
        self.assertIs(client.connect_calls[0].get('transport_factory'),
                      _NoStdinTransport)

    def test_two_factor_prompts_only_otp(self):
        """key 部分成功后（transport 存活、partial 状态保留）：只弹一次
        验证码框即连接成功——终端 ssh 的体验。"""
        sess, host = _make_session()
        prompts_seen = []

        def provider(alias, prompt, echo):
            prompts_seen.append((prompt, echo))
            return "123456"

        sess._interactive_provider = provider
        transport = _OtpOnlyTransport()
        import ssh_session
        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: _FakeClient(transport)):
            client = sess._connect_client(host)
        self.assertIsNotNone(client)
        self.assertEqual(prompts_seen, [("[OTP Code]:", True)],
                         "2FA 只应弹验证码框，不应弹密码框")
        self.assertEqual(transport.keepalive, 30)

    def test_dead_transport_reopens_clean_connection(self):
        """服务器在多 key 尝试后掐线（transport 已死）→ 重开一条禁 agent/
        禁扫 key 的干净连接走交互认证，而不是退回密码框。"""

        class _DeadTransport(_FakeTransport):
            def is_active(self):
                return False

        class _CleanClient(_FakeClient):
            def connect(self, **kwargs):
                self.connect_calls.append(kwargs)
                # 干净连接：无可用认证方法，transport 保持存活
                raise paramiko.SSHException("No authentication methods available")

        sess, host = _make_session()
        prompts_seen = []
        sess._interactive_provider = (
            lambda alias, prompt, echo: prompts_seen.append(prompt) or "123456")
        dead = _FakeClient(_DeadTransport())
        fresh_transport = _OtpOnlyTransport()
        fresh = _CleanClient(fresh_transport)
        clients = [dead, fresh]
        import ssh_session
        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: clients.pop(0)):
            client = sess._connect_client(host)
        self.assertIs(client, fresh, "应返回重开的干净连接")
        self.assertEqual(prompts_seen, ["[OTP Code]:"])
        # 干净连接不得再自动尝试 agent / 扫 key（避免再次触发掐线）
        self.assertFalse(fresh.connect_calls[0]['allow_agent'])
        self.assertFalse(fresh.connect_calls[0]['look_for_keys'])
        self.assertEqual(fresh_transport.keepalive, 30)

    def test_dead_transport_in_jump_chain_falls_back(self):
        """跳板链内层（sock 一次性）transport 死了无法重连 → 老老实实
        回落单密码逻辑，不额外重开连接。"""

        class _DeadTransport(_FakeTransport):
            def is_active(self):
                return False

        class _PasswordClient(_FakeClient):
            def connect(self, **kwargs):
                self.connect_calls.append(kwargs)
                if kwargs.get('password') == 'pw':
                    return
                raise paramiko.AuthenticationException("auth failed")

        sess, host = _make_session()
        sess._interactive_provider = lambda alias, prompt, echo: self.fail(
            "跳板内层不应走交互重连")
        sess._password_provider = lambda label: 'pw'
        client = _PasswordClient(_DeadTransport())
        import ssh_session
        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: client):
            sess._connect_client(host, sock=object())
        self.assertEqual(client.connect_calls[-1].get('password'), 'pw')


if __name__ == '__main__':
    unittest.main()
