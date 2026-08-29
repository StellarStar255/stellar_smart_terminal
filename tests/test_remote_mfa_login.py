"""Remote Explorer 的 MFA / 动态码登录（不依赖真实 SSH）：

ssh_session 侧
- 交互认证期间放宽 transport.auth_timeout（默认 30s 撑不过"翻手机抄 6 位码"）
- 问过一次性动态码的会话被标记（used_otp_auth），重连只试一次、认证失败不重试

面板侧
- MFA 登录框预收的答案在认证回调里直接作答，全程不弹二次框
- 密码/动态码各自只自动作答一次；再被问到就回到弹框（避免用错答案打满 MaxAuthTries）
- 动态码用完即从内存擦除、永不落盘；断开时预收答案一并清掉
- 「这台主机走 MFA」按别名持久化：连接时自动先弹登录框；认证真要过码时自动记上
- 主连接空闲保持：空闲超时断开，任何远程活动都会续期
- MFA 登录默认不再自动开 SSH 终端标签（那是另一条连接，会再要一次码）

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_mfa_login.py -q
"""
import os
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

import paramiko

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------- ssh_session

class _OtpTransport:
    """只问一步动态码的堡垒机；记录认证期间看到的 auth_timeout。"""

    auth_timeout = 30      # paramiko.Transport 的默认值

    def __init__(self, otp="123456"):
        self.otp = otp
        self.seen_auth_timeout = None
        self.keepalive = None

    def is_active(self):
        return True

    def auth_none(self, username):
        raise paramiko.BadAuthenticationType(
            "none not allowed", ["keyboard-interactive"])

    def auth_interactive(self, username, handler, submethods=""):
        # 认证进行中才是放宽窗口 —— 抄下此刻的值
        self.seen_auth_timeout = self.auth_timeout
        answers = handler("MFA", "", [("[OTP Code]:", True)])
        if answers != [self.otp]:
            raise paramiko.AuthenticationException("bad code")
        return []

    def set_keepalive(self, interval):
        self.keepalive = interval


class _PwdTransport(_OtpTransport):
    """只问密码（PAM 交互式）的主机：不该被记成"需要动态码"。"""

    def auth_interactive(self, username, handler, submethods=""):
        self.seen_auth_timeout = self.auth_timeout
        handler("login", "", [("Password: ", False)])
        return []


class _Client:
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


def _make_session(alias='otp-host'):
    from ssh_session import SSHSession, HostConfig
    host = HostConfig(alias=alias, hostname='example.com', user='u', port=22)
    return SSHSession(host), host


class TestInteractiveAuthTimeout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _connect(self, sess, host, client):
        import ssh_session
        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: client):
            return sess._connect_client(host)

    def test_auth_timeout_relaxed_during_interactive_auth(self):
        """paramiko 默认 auth_timeout=30s：用户翻手机抄码常常超时，
        认证期间必须放宽，否则码没输错也会 Authentication timeout。"""
        import ssh_session
        sess, host = _make_session()
        sess._interactive_provider = lambda alias, prompt, echo: "123456"
        transport = _OtpTransport()
        self._connect(sess, host, _Client(transport))
        self.assertGreaterEqual(
            transport.seen_auth_timeout, 120,
            "留给用户读码/输码的时间不能只有 paramiko 默认的 30s")
        self.assertEqual(transport.seen_auth_timeout,
                         ssh_session.INTERACTIVE_AUTH_TIMEOUT)
        # 认证结束后恢复原值，别把放宽的超时留给后续握手
        self.assertEqual(transport.auth_timeout, 30)

    def test_otp_session_is_flagged(self):
        """问过一次性动态码 → used_otp_auth() 为真（重连要重新找人要码）。"""
        sess, host = _make_session()
        sess._interactive_provider = lambda alias, prompt, echo: "123456"
        self._connect(sess, host, _Client(_OtpTransport()))
        self.assertTrue(sess.used_otp_auth())
        # 一次性动态码绝不进缓存
        self.assertNotIn('123456', str(sess._auth_secrets))

    def test_password_only_interactive_is_not_flagged_as_otp(self):
        """只问密码的交互式主机能自动重连，不该被当成 OTP 主机。"""
        sess, host = _make_session('pwd-host')
        sess._interactive_provider = lambda alias, prompt, echo: "secret"
        self._connect(sess, host, _Client(_PwdTransport()))
        self.assertFalse(sess.used_otp_auth())


class _BastionTransport(_OtpTransport):
    """JumpServer 式堡垒机：**同时宣告 publickey**，但真正能过的只有交互认证。

    这就是 shanghai-centralrd-13 的形态——只看"有没有 publickey"来决定要不要试
    密钥，会把 MaxAuthTries 打满被掐线（transport shut down or saw EOF）。
    """

    def auth_none(self, username):
        raise paramiko.BadAuthenticationType(
            "none not allowed",
            ["publickey", "password", "keyboard-interactive"])


class TestPreferInteractive(unittest.TestCase):
    """MFA 主机绝不试密钥（否则动态码框还没弹，连接就被堡垒机掐了）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _run(self, prefer: bool, client):
        sess, host = _make_session()
        sess._interactive_provider = lambda alias, prompt, echo: "123456"
        sess._prefer_interactive = prefer
        import ssh_session
        with mock.patch.object(ssh_session.paramiko, 'SSHClient',
                               lambda: client):
            return sess, sess._connect_client(host)

    def test_mfa_host_skips_keys_even_when_publickey_advertised(self):
        client = _Client(_BastionTransport())
        sess, result = self._run(True, client)
        self.assertIs(result, client)
        # 只有零凭据探测那一次 connect，且没带 agent / 扫 key
        self.assertEqual(len(client.connect_calls), 1)
        self.assertFalse(client.connect_calls[0]['allow_agent'])
        self.assertFalse(client.connect_calls[0]['look_for_keys'])
        self.assertTrue(sess.used_otp_auth())

    def test_without_the_flag_publickey_hosts_keep_old_behaviour(self):
        """普通主机不受影响：仍然照常尝试 agent / 密钥。"""
        client = _Client(_BastionTransport())
        _sess, result = self._run(False, client)
        self.assertIs(result, client)
        self.assertGreaterEqual(len(client.connect_calls), 2,
                                "探测之后应仍走原有的完整认证流程")
        self.assertTrue(client.connect_calls[-1]['allow_agent'])

    def test_probe_failure_still_avoids_key_spray_for_mfa_hosts(self):
        """探测连接建不起来时的兜底路径也不能去试密钥。"""

        class _NoProbeClient(_Client):
            def __init__(self, transport):
                super().__init__(transport)
                self._first = True

            def connect(self, **kwargs):
                self.connect_calls.append(kwargs)
                if self._first:
                    self._first = False
                    raise OSError("probe failed")   # 探测阶段网络抖动
                raise paramiko.SSHException("No authentication methods available")

        client = _NoProbeClient(_BastionTransport())
        _sess, result = self._run(True, client)
        self.assertIs(result, client)
        self.assertFalse(client.connect_calls[-1]['allow_agent'])
        self.assertFalse(client.connect_calls[-1]['look_for_keys'])


class TestReconnectPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _count_attempts(self, sess, exc):
        calls = {'n': 0}

        def _boom():
            calls['n'] += 1
            raise exc

        with mock.patch.object(sess, '_establish', _boom), \
                mock.patch.object(sess, '_teardown_connection', lambda: None), \
                mock.patch('ssh_session.time.sleep', lambda *_: None):
            with self.assertRaises(RuntimeError):
                sess._reconnect()
        return calls['n']

    def test_otp_host_reconnects_only_once(self):
        """OTP 主机重试 3 次 = 连弹 3 个动态码框，只许试一次。"""
        sess, _host = _make_session()
        sess._auth_secrets['otp-host'] = {'needs_otp': True}
        self.assertEqual(self._count_attempts(sess, OSError("net down")), 1)

    def test_plain_host_keeps_retrying(self):
        """普通主机的三次退避重试保持原样。"""
        import ssh_session
        sess, _host = _make_session('plain')
        self.assertEqual(self._count_attempts(sess, OSError("net down")),
                         ssh_session.RECONNECT_MAX_ATTEMPTS)

    def test_auth_failure_is_not_retried(self):
        """认证被拒/用户取消：再试只会再弹一次框、再被拒一次。"""
        sess, _host = _make_session('plain')
        n = self._count_attempts(
            sess, paramiko.AuthenticationException("cancelled by user"))
        self.assertEqual(n, 1)


# --------------------------------------------------------------------- 面板侧

class _FakeSession:
    """够 _on_session_connected / _disconnect 用的假会话。"""

    def __init__(self, alias='bastion', otp=True):
        from ssh_session import HostConfig
        self.host_config = HostConfig(alias=alias, hostname='h', user='u')
        self._otp = otp
        self.disconnect_calls = 0

    def used_otp_auth(self):
        return self._otp

    def home(self):
        return '/home/u'

    def is_connected(self):
        return True

    def listdir(self, *_a, **_k):
        return []

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except Exception as e:            # pragma: no cover — 测试里不该发生
            fut.set_exception(e)
        return fut

    def invalidate_cache(self, *_a):
        pass

    def disconnect(self):
        self.disconnect_calls += 1


class _FakeMfaDialog:
    """替身 MFA 对话框：记录构造参数，直接返回预设答案（不进事件循环）。"""

    calls: list = []
    answer = ''
    pwd = ''
    accepted = True

    def __init__(self, *_args, **kwargs):
        type(self).calls.append(kwargs)

    @classmethod
    def record(cls, code='', password='', accepted=True) -> list:
        cls.calls = []
        cls.answer = code
        cls.pwd = password
        cls.accepted = accepted
        return cls.calls

    def exec(self):
        from PyQt6.QtWidgets import QDialog
        return (QDialog.DialogCode.Accepted if type(self).accepted
                else QDialog.DialogCode.Rejected)

    def code(self):
        return type(self).answer

    def password(self):
        return type(self).pwd

    def keep_secs(self):
        return 3600

    def open_terminal(self):
        return False


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


class TestPreSuppliedAnswers(_PanelBase):
    """登录框先收码 → 认证回调直接作答，认证中途不再弹任何框。"""

    def _armed_panel(self, code='123456', password='pw'):
        panel = self._panel()
        panel._pending_mfa = {
            'alias': 'bastion', 'code': code, 'code_used': False,
            'password': password, 'password_used': False,
        }
        return panel

    def _no_dialogs(self):
        """任何弹框都视为失败（预收答案时一次都不该弹）。"""
        def _boom(*_a, **_k):
            self.fail("预收答案时不应再弹框")
        return mock.patch.multiple(
            'remote_explorer_widget',
            _MfaLoginDialog=_boom,
            QInputDialog=mock.Mock(getText=_boom))

    def test_password_and_code_answered_without_dialogs(self):
        panel = self._armed_panel()
        with self._no_dialogs():
            self.assertEqual(
                panel._prompt_interactive('bastion', 'Password: ', False), 'pw')
            self.assertEqual(
                panel._prompt_interactive('bastion', '[OTP Code]:', True),
                '123456')

    def test_code_is_single_use_then_falls_back_to_dialog(self):
        """服务器再问一次码 = 刚才那个被拒/还要第二个 → 必须回去问用户。"""
        panel = self._armed_panel()
        with self._no_dialogs():
            self.assertEqual(
                panel._prompt_interactive('bastion', '[OTP Code]:', True),
                '123456')
        asked = _FakeMfaDialog.record(code='654321')
        with mock.patch('remote_explorer_widget._MfaLoginDialog', _FakeMfaDialog):
            self.assertEqual(
                panel._prompt_interactive('bastion', '[OTP Code]:', True),
                '654321')
        self.assertEqual([k.get('prompt') for k in asked], ['[OTP Code]:'])

    def test_password_is_single_use_too(self):
        panel = self._armed_panel()
        self.assertEqual(
            panel._prompt_interactive('bastion', 'Password: ', False), 'pw')
        self.assertIsNone(panel._take_pre_answer('bastion', 'Password: '))

    def test_answers_are_scoped_to_their_host(self):
        """跳板链上的另一台主机不该吃掉目标机的动态码。"""
        panel = self._armed_panel()
        self.assertIsNone(panel._take_pre_answer('other', '[OTP Code]:'))

    def test_used_code_is_wiped_from_memory(self):
        panel = self._armed_panel()
        panel._take_pre_answer('bastion', '[OTP Code]:')
        self.assertIsNone(panel._pending_mfa['code'])

    def test_disconnect_clears_pending_answers(self):
        panel = self._armed_panel()
        panel._disconnect()
        self.assertIsNone(panel._pending_mfa)


class TestMfaHostFlag(_PanelBase):
    def test_flag_persists_and_carries_keep_duration(self):
        panel = self._panel()
        self.assertFalse(panel._is_mfa_host('bastion'))
        panel._set_mfa_host('bastion', 3600)
        self.assertTrue(self._panel()._is_mfa_host('bastion'))
        self.assertEqual(self._panel()._get_mfa_keep('bastion'), 3600)
        panel._set_mfa_host('bastion', None)
        self.assertFalse(self._panel()._is_mfa_host('bastion'))

    def test_marked_hosts_show_a_key_marker_in_the_list(self):
        """列表上一眼看出哪台要动态码（🔑），取消标记后立刻恢复。"""
        panel = self._panel()
        panel._extra_hosts = [self._host('bastion'), self._host('plain')]
        panel._set_mfa_host('bastion', 3600)
        texts = [panel._hosts_list.item(i).text()
                 for i in range(panel._hosts_list.count())]
        self.assertTrue(any(x.startswith('🔑') and 'bastion' in x for x in texts),
                        texts)
        self.assertTrue(any(x.startswith('🖥') and 'plain' in x for x in texts),
                        texts)
        panel._set_mfa_host('bastion', None)
        texts = [panel._hosts_list.item(i).text()
                 for i in range(panel._hosts_list.count())]
        self.assertFalse(any(x.startswith('🔑') for x in texts), texts)

    def test_connect_to_marked_host_asks_for_code_first(self):
        """已知走 MFA 的主机：连接前先弹登录框收码，而不是裸连等服务器问。"""
        panel = self._panel()
        panel._set_mfa_host('bastion', 3600)
        host = self._host()
        with mock.patch.object(type(panel), '_mfa_login') as m:
            panel._connect_to(host)
        m.assert_called_once()
        self.assertIsNone(panel._session, "应停在登录框，不该已经建会话")

    def test_plain_host_connects_directly(self):
        panel = self._panel()
        with mock.patch.object(type(panel), '_mfa_login') as m, \
                mock.patch('remote_explorer_widget.SSHSession') as sess_cls:
            panel._connect_to(self._host('plain'))
        m.assert_not_called()
        sess_cls.assert_called_once()
        # 普通主机不带 prefer_interactive，认证回退链保持原样
        self.assertFalse(
            sess_cls.return_value.connect_async.call_args.kwargs[
                'prefer_interactive'])

    def test_mfa_connect_tells_session_to_skip_key_attempts(self):
        """带着动态码去连时必须告诉会话「别试密钥」——否则堡垒机先掐线。"""
        panel = self._panel()
        answers = {'alias': 'bastion', 'code': '123456', 'code_used': False,
                   'password': None, 'password_used': False}
        with mock.patch('remote_explorer_widget.SSHSession') as sess_cls:
            panel._connect_to(self._host(), mfa_answers=answers)
        self.assertTrue(
            sess_cls.return_value.connect_async.call_args.kwargs[
                'prefer_interactive'])

    def test_auth_that_needed_a_code_marks_the_host(self):
        """用户没手工标记过也没关系：认证真要过码就自动记上，下次直接弹框。"""
        panel = self._panel()
        sess = _FakeSession(otp=True)
        panel._session = sess
        panel._on_session_connected(sess)
        self.assertTrue(panel._is_mfa_host('bastion'))

    def test_plain_auth_does_not_mark_the_host(self):
        panel = self._panel()
        sess = _FakeSession(alias='plain', otp=False)
        panel._session = sess
        panel._on_session_connected(sess)
        self.assertFalse(panel._is_mfa_host('plain'))

    def test_one_time_code_never_reaches_disk(self):
        """动态码只在内存里活到认证结束：配置文件里不能有它的影子。"""
        panel = self._panel()
        panel._pending_mfa = {
            'alias': 'bastion', 'code': '987654', 'code_used': False,
            'password': None, 'password_used': False,
        }
        panel._set_mfa_host('bastion', 3600)
        panel._take_pre_answer('bastion', '[OTP Code]:')
        sess = _FakeSession()
        panel._session = sess
        panel._on_session_connected(sess)
        self.assertIsNone(panel._pending_mfa)
        self.assertNotIn('987654', self._tmp_cfg.read_text(encoding='utf-8'))


class TestTerminalTabPolicy(_PanelBase):
    def test_mfa_login_does_not_open_terminal_by_default(self):
        """终端是另一条连接，会再要一次码 —— 没勾就别开。"""
        panel = self._panel()
        panel._mfa_open_terminal = False
        fired = []
        panel.host_connected.connect(lambda h: fired.append(h))
        sess = _FakeSession()
        panel._session = sess
        panel._on_session_connected(sess)
        self.assertEqual(fired, [])

    def test_plain_connect_still_opens_terminal(self):
        panel = self._panel()
        fired = []
        panel.host_connected.connect(lambda h: fired.append(h))
        sess = _FakeSession(alias='plain', otp=False)
        panel._session = sess
        panel._on_session_connected(sess)
        self.assertEqual(len(fired), 1)

    def test_plain_connect_resets_terminal_policy(self):
        """上一次 MFA 登录里的选择不能粘到下一台普通主机上。"""
        panel = self._panel()
        panel._mfa_open_terminal = False
        panel._mfa_keep_secs = 3600
        with mock.patch('remote_explorer_widget.SSHSession'):
            panel._connect_to(self._host('plain'))
        self.assertTrue(panel._mfa_open_terminal)
        self.assertEqual(panel._mfa_keep_secs, 0)


class TestIdleKeepAlive(_PanelBase):
    def test_idle_beyond_keep_disconnects(self):
        panel = self._panel()
        sess = _FakeSession()
        panel._session = sess
        panel._mfa_keep_secs = 3600
        panel._last_activity = -10_000.0     # 远早于 monotonic 的当前值
        panel._check_idle_timeout()
        self.assertEqual(sess.disconnect_calls, 1)
        self.assertIsNone(panel._session)

    def test_recent_activity_keeps_connection(self):
        panel = self._panel()
        sess = _FakeSession()
        panel._session = sess
        panel._mfa_keep_secs = 3600
        panel._touch_activity()
        panel._check_idle_timeout()
        self.assertEqual(sess.disconnect_calls, 0)

    def test_keep_never_means_never(self):
        panel = self._panel()
        sess = _FakeSession()
        panel._session = sess
        panel._mfa_keep_secs = 0             # "不自动断开"
        panel._last_activity = -10_000.0
        panel._check_idle_timeout()
        self.assertEqual(sess.disconnect_calls, 0)

    def test_remote_operations_renew_the_lease(self):
        """长传输/浏览期间不能被空闲看门狗掐掉。"""
        panel = self._panel()
        sess = _FakeSession()
        panel._session = sess
        panel._mfa_keep_secs = 3600
        panel._last_activity = -10_000.0
        panel._on_download_progress('/x/f.bin', 1024, 4096)
        panel._check_idle_timeout()
        self.assertEqual(sess.disconnect_calls, 0)

    def test_idle_watchdog_only_runs_for_mfa_sessions(self):
        """普通连接不设保持时长 → 看门狗不启动，行为完全不变。"""
        panel = self._panel()
        with mock.patch('remote_explorer_widget.SSHSession'):
            panel._connect_to(self._host('plain'))
        sess = _FakeSession(alias='plain', otp=False)
        panel._session = sess
        panel._on_session_connected(sess)
        self.assertFalse(panel._idle_timer.isActive())


class TestMfaDialog(_PanelBase):
    def test_dialog_collects_code_password_and_keep(self):
        from remote_explorer_widget import _MfaLoginDialog
        dlg = _MfaLoginDialog(None, alias='bastion')
        self.assertFalse(dlg._ok_btn.isEnabled(), "空表单不该能提交")
        dlg._code_edit.setText(' 123456 ')
        self.assertTrue(dlg._ok_btn.isEnabled())
        self.assertEqual(dlg.code(), '123456')
        self.assertEqual(dlg.keep_secs(), _MfaLoginDialog.DEFAULT_KEEP_SECS)
        self.assertFalse(dlg.open_terminal(), "终端标签默认不开（会再要一次码）")
        dlg._password_edit.setText('pw')
        self.assertEqual(dlg.password(), 'pw')
        dlg.deleteLater()

    def test_password_only_form_is_submittable(self):
        """有的堡垒机只问密码：只填密码也该能提交。"""
        from remote_explorer_widget import _MfaLoginDialog
        dlg = _MfaLoginDialog(None, alias='bastion')
        dlg._password_edit.setText('pw')
        self.assertTrue(dlg._ok_btn.isEnabled())
        dlg.deleteLater()

    def test_reauth_mode_shows_server_prompt_only(self):
        from remote_explorer_widget import _MfaLoginDialog
        dlg = _MfaLoginDialog(None, alias='bastion', reauth=True,
                              prompt='[OTP Code]:', echo=True)
        self.assertIsNone(dlg._password_edit)
        self.assertIsNone(dlg._keep_combo)
        dlg._code_edit.setText('654321')
        self.assertEqual(dlg.code(), '654321')
        dlg.deleteLater()

    def test_login_flow_stores_answers_and_marks_host(self):
        """走一遍完整登录：答案进 _pending_mfa、主机被标记、保持时长记住。"""
        panel = self._panel()
        _FakeMfaDialog.record(code='123456', password='pw')
        with mock.patch('remote_explorer_widget._MfaLoginDialog', _FakeMfaDialog), \
                mock.patch('remote_explorer_widget.SSHSession') as sess_cls:
            panel._mfa_login(self._host())
        sess_cls.assert_called_once()
        self.assertEqual(panel._pending_mfa['code'], '123456')
        self.assertEqual(panel._pending_mfa['password'], 'pw')
        self.assertEqual(panel._mfa_keep_secs, 3600)
        self.assertFalse(panel._mfa_open_terminal)
        self.assertTrue(panel._is_mfa_host('bastion'))
        # 密码可缓存（SSH 终端自动回填），动态码不行
        self.assertEqual(panel.get_cached_password('bastion'), 'pw')
        self.assertNotIn('123456', str(panel._cached_passwords))

    def test_cancelled_login_connects_nothing(self):
        panel = self._panel()
        _FakeMfaDialog.record(accepted=False)
        with mock.patch('remote_explorer_widget._MfaLoginDialog', _FakeMfaDialog), \
                mock.patch('remote_explorer_widget.SSHSession') as sess_cls:
            panel._mfa_login(self._host())
        sess_cls.assert_not_called()
        self.assertIsNone(panel._pending_mfa)


if __name__ == '__main__':
    unittest.main()
