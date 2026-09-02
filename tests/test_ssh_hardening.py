# -*- coding: utf-8 -*-
"""SSH 两个后端的加固回归（审查清单 2026-09）：

ssh_control（ControlMaster）
- 远端普通 EPERM（mkdir/rm 在无写权限目录）不能被翻译成"主连接已断开"逼用户
  重输动态码：只有 ssh 自己的退出码 255 才是连接层错误。
- 主连接不在（控制套接字文件不存在）时，普通命令直接抛 MasterNotRunning，
  不起 ssh 进程——否则 OpenSSH 会静默回落成一次完整直连+认证，把堡垒机的
  MaxAuthTries 打满。
- 普通命令的参数必须关掉所有认证方式（Pubkey/KbdInteractive/Password=no），
  就算回落直连也零认证尝试即失败；登录那条（-M）仍必须允许认证。
- 动态码/密码不再经环境变量交给 `-f` 转后台的常驻主连接（同用户 `ps -E` /
  /proc/<pid>/environ 可读、且随 ControlPersist 存活数小时）：改走 askpass
  同目录下的 0600 文件，登录返回即整目录删除。

ssh_session（paramiko）
- 首次接受新主机时不能用 save_host_keys 整体重写 ~/.ssh/known_hosts（会丢
  注释、@cert-authority/@revoked 行和 paramiko 不认识的密钥类型），只追加一行。
- ProxyJump 链路的 open_channel 必须带超时。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_ssh_hardening.py -q
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssh_control                                    # noqa: E402
from ssh_session import HostConfig                    # noqa: E402

_REAL_RUN = subprocess.run   # 测试里会 patch ssh_control.subprocess.run（就是全局的那个）

_POSIX_ONLY = unittest.skipIf(
    sys.platform.startswith('win'),
    'ssh ControlMaster / askpass 是 POSIX 专属，Windows 走 paramiko 路径')


def _host(alias='bastion'):
    return HostConfig(alias=alias, hostname='bastion.example.com',
                      user='u', port=2222, raw={'hostname': 'bastion.example.com'})


def _qt():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class _Proc:
    def __init__(self, rc, err=b'', out=b''):
        self.returncode = rc
        self._err, self._out = err, out

    def communicate(self, input=None, timeout=None):
        return self._out, self._err

    def kill(self):
        pass


# ---------------------------------------------------------------- 错误分类

@_POSIX_ONLY
class TestPermissionDeniedIsNotADisconnect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qt()

    def test_remote_eperm_keeps_raw_stderr(self):
        """mkdir 在只读目录的 EPERM：退出码 1，不是断线，原样报给用户。"""
        from remote_explorer_widget import RemoteExplorerPanel
        msg = ssh_control.ControlMasterSession._explain(
            "mkdir: cannot create directory '/etc/x': Permission denied\n",
            returncode=1)
        self.assertIn('cannot create directory', msg)
        self.assertFalse(RemoteExplorerPanel._looks_like_disconnect(
            RemoteExplorerPanel, msg), msg)

    def test_ssh_layer_permission_denied_is_a_disconnect(self):
        """ssh 自己认证失败（退出码 255）才是"主连接没了、回落直连被拒"。"""
        from remote_explorer_widget import RemoteExplorerPanel
        msg = ssh_control.ControlMasterSession._explain(
            'u@bastion: Permission denied (publickey,keyboard-interactive).\n',
            returncode=255)
        self.assertTrue(RemoteExplorerPanel._looks_like_disconnect(
            RemoteExplorerPanel, msg), msg)

    def test_run_passes_exit_code_to_explain(self):
        sess = ssh_control.ControlMasterSession(_host())
        sess._spawn = lambda cmd, **kw: _Proc(
            1, b"rm: cannot remove '/etc/passwd': Permission denied\n")
        sess._reap = lambda p: None
        with self.assertRaises(RuntimeError) as cm:
            sess._run("rm -f -- /etc/passwd")
        self.assertIn('cannot remove', str(cm.exception))
        self.assertNotIn('MFA', str(cm.exception))


# ---------------------------------------------------------------- 主连接不在

@_POSIX_ONLY
class TestNoFallbackWithoutMaster(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _qt()

    def test_missing_socket_raises_before_spawning_ssh(self):
        sess = ssh_control.ControlMasterSession(_host())
        ctl = ssh_control.control_path_for(_host())
        self.assertFalse(os.path.exists(ctl))
        with mock.patch.object(ssh_control.subprocess, 'Popen') as popen:
            with self.assertRaises(ssh_control.MasterNotRunning) as cm:
                sess._run("cd ~ && pwd -P")
        popen.assert_not_called()
        # 面板要能把它识别成断线 → 走"重连"提示
        from remote_explorer_widget import RemoteExplorerPanel
        self.assertTrue(RemoteExplorerPanel._looks_like_disconnect(
            RemoteExplorerPanel, str(cm.exception)), str(cm.exception))

    def test_client_args_disable_every_auth_method(self):
        args = ssh_control.ControlMasterSession(_host())._base_args()
        for opt in ('PubkeyAuthentication=no', 'KbdInteractiveAuthentication=no',
                    'PasswordAuthentication=no'):
            self.assertIn(opt, args, f'{opt} 缺失：回落直连会去试认证')
        self.assertIn('ControlMaster=no', args)

    def test_master_login_args_keep_auth_enabled(self):
        seen = {}

        def _fake_run(args, **kwargs):
            seen['args'] = args
            return subprocess.CompletedProcess(args, 0, b'', b'')

        with mock.patch.object(ssh_control.subprocess, 'run', _fake_run):
            ssh_control.mfa_login(_host(), code='123456')
        for opt in ('PubkeyAuthentication=no', 'KbdInteractiveAuthentication=no',
                    'PasswordAuthentication=no'):
            self.assertNotIn(opt, seen['args'], '登录那条必须允许认证')


# ---------------------------------------------------------------- 码不走环境变量

@_POSIX_ONLY
class TestSecretsNeverInEnvironment(unittest.TestCase):
    def _run_login(self, **kw):
        seen = {}

        def _fake_run(args, **kwargs):
            env = kwargs.get('env') or {}
            seen['args'] = args
            seen['env'] = env
            ap = env.get('SSH_ASKPASS', '')
            seen['askpass'] = ap
            seen['dir'] = os.path.dirname(ap)
            # 认证发生的那一刻真的用 sh 跑一遍 askpass：答案必须能取出来
            seen['pw_answer'] = _REAL_RUN(
                [ap, 'Password:'], env=env, stdout=subprocess.PIPE,
                timeout=10).stdout.decode()
            seen['code_answer'] = _REAL_RUN(
                [ap, '[MFA auth]: '], env=env, stdout=subprocess.PIPE,
                timeout=10).stdout.decode()
            seen['modes'] = {
                name: stat.S_IMODE(os.stat(os.path.join(seen['dir'], name)).st_mode)
                for name in os.listdir(seen['dir'])}
            return subprocess.CompletedProcess(args, 0, b'', b'')

        with mock.patch.object(ssh_control.subprocess, 'run', _fake_run):
            ssh_control.mfa_login(_host(), **kw)
        return seen

    def test_env_and_argv_carry_no_secret(self):
        seen = self._run_login(code='123456', password='hunter2')
        joined = ' '.join(seen['args'])
        self.assertNotIn('123456', joined)
        self.assertNotIn('hunter2', joined)
        for v in seen['env'].values():
            self.assertNotIn('123456', v)
            self.assertNotIn('hunter2', v)

    def test_askpass_still_answers_from_private_files(self):
        seen = self._run_login(code='123456', password='hunter2')
        self.assertEqual(seen['pw_answer'], 'hunter2')
        self.assertEqual(seen['code_answer'], '123456')
        for name, mode in seen['modes'].items():
            self.assertEqual(mode & 0o077, 0, f'{name} 对同组/其他用户可读')

    def test_code_falls_back_to_password_when_only_password_given(self):
        seen = self._run_login(password='hunter2')
        self.assertEqual(seen['code_answer'], 'hunter2')

    def test_secret_files_removed_after_login(self):
        seen = self._run_login(code='123456', password='hunter2')
        self.assertFalse(os.path.exists(seen['dir']))


# ---------------------------------------------------------------- known_hosts

class TestKnownHostsAppendOnly(unittest.TestCase):
    """首次接受新主机只追加一行：注释、CA 行、陌生密钥类型都要原样保留。"""

    # 注释、@ 标记行、paramiko 不认识的密钥类型：save_host_keys 全部会丢。
    # （@ 标记行按 paramiko 的三列切法第三列被当 base64 解，这里给它一个能解的
    # 值让 load 走得过去；真实 CA 行会让 load 直接抛 InvalidHostKey，是另一个坑。）
    _ORIG = (
        "# my bastions\n"
        "@cert-authority *.corp.example AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "old.example.com ssh-futurekey AAAAfuture==\n"
    )

    def setUp(self):
        import paramiko
        self.tmp = tempfile.mkdtemp(prefix='kh-')
        self.path = os.path.join(self.tmp, 'known_hosts')
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write(self._ORIG)
        self.key = paramiko.ECDSAKey.generate()

    def _persist(self, hostname='new.example.com'):
        import paramiko
        from ssh_session import SSHSession, _PersistOnFirstUsePolicy
        client = paramiko.SSHClient()
        if os.path.isfile(self.path):
            client.load_host_keys(self.path)
        policy = _PersistOnFirstUsePolicy()
        client.set_missing_host_key_policy(policy)
        policy.missing_host_key(client, hostname, self.key)
        with mock.patch.object(os.path, 'expanduser',
                               lambda p: self.path if 'known_hosts' in p else p):
            SSHSession._persist_new_host_key(client, policy)
        with open(self.path, encoding='utf-8') as fh:
            return fh.read()

    def test_existing_lines_survive(self):
        text = self._persist()
        self.assertTrue(text.startswith(self._ORIG),
                        f'原有内容被改写:\n{text}')

    def test_new_host_appended_once(self):
        import paramiko
        text = self._persist()
        tail = text[len(self._ORIG):]
        lines = [l for l in tail.split('\n') if l.strip()]
        self.assertEqual(len(lines), 1, tail)
        entry = paramiko.hostkeys.HostKeyEntry.from_line(lines[0])
        self.assertEqual(entry.hostnames, ['new.example.com'])
        self.assertEqual(entry.key.get_base64(), self.key.get_base64())

    def test_missing_trailing_newline_is_repaired(self):
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write("a.example ssh-futurekey AAAA==")     # 没有末尾换行
        text = self._persist()
        self.assertTrue(text.startswith("a.example ssh-futurekey AAAA==\nnew.example.com "))

    def test_created_private_when_absent(self):
        os.remove(self.path)
        self._persist()
        if not sys.platform.startswith('win'):
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode) & 0o077, 0)


# ---------------------------------------------------------------- ProxyJump 超时

class TestJumpChannelTimeout(unittest.TestCase):
    def test_open_channel_has_timeout(self):
        import ssh_session
        seen = {}

        class _T:
            def open_channel(self, kind, dest, src, **kw):
                seen['kw'] = kw
                return object()

        class _C:
            def get_transport(self):
                return _T()

            def close(self):
                pass

        sess = ssh_session.SSHSession.__new__(ssh_session.SSHSession)
        hop = HostConfig(alias='jump', hostname='j', user='u', port=22)
        target = HostConfig(alias='t', hostname='t', user='u', port=22)
        target.proxy_jump = 'jump'
        with mock.patch.object(ssh_session.SSHSession, '_connect_client',
                               lambda self, cfg, sock=None: _C()), \
             mock.patch.object(ssh_session.SSHSession, '_resolve_jump_spec',
                               lambda self, spec: hop):
            sess._connect_jump_chain(target)
        self.assertEqual(seen['kw'].get('timeout'), ssh_session.CONNECT_TIMEOUT)


if __name__ == '__main__':
    unittest.main()
