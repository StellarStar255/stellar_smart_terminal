"""系统 ssh + ControlMaster 后端（MFA 堡垒机专用）单元测试，不碰真实网络。

覆盖的是这条路上最容易出错、且出错就"看起来能用其实是坏的"的地方：

- askpass 脚本的分流（真的用 /bin/sh 跑一遍）：密码类提示回密码，其余回动态码
- mfa_login 的 ssh 参数：-M -N -f / ControlPersist / ControlPath / BatchMode=no，
  以及码只走环境变量、临时脚本用完即删
- 普通命令必须 BatchMode=yes + ControlMaster=no（否则后台线程会被交互提示挂死，
  或自己去建一条过不了 MFA 的连接）
- ControlPath 的 104 字节 unix socket 限制
- ls 输出解析：文件名里的空格、软链、GNU/BSD 两种时间列
- 远端命令的引号（唯一的命令注入面）
- 主连接断了的报错要能被面板识别成"断线"，从而走重连提示

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_ssh_control_master.py -q
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssh_control                                    # noqa: E402
from ssh_session import HostConfig                    # noqa: E402


# 整个后端是 POSIX 能力：ControlMaster 走 unix socket、askpass 是 shell 脚本。
# Windows 的 OpenSSH 没有 ControlMaster，那边照旧走 paramiko 路径（另有用例
# 覆盖回落），所以这个文件在 Windows 上整体跳过——不是"没测到"，是"没这条路"。
_POSIX_ONLY = unittest.skipIf(
    sys.platform.startswith('win'),
    'ssh ControlMaster / askpass 是 POSIX 专属，Windows 走 paramiko 路径')


def _host(alias='bastion', raw=True):
    return HostConfig(alias=alias, hostname='bastion.example.com',
                      user='u@root@10.0.0.9', port=2222,
                      raw={'hostname': 'bastion.example.com'} if raw else {})


@_POSIX_ONLY
class TestAskpassScript(unittest.TestCase):
    """askpass 是动态码真正被喂进 ssh 的地方——直接用 sh 跑它，别靠脑补。"""

    def _answer(self, prompt: str, code='123456', password='pw') -> str:
        d = tempfile.mkdtemp(prefix='askpass-test-')
        try:
            p = os.path.join(d, 'askpass.sh')
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(ssh_control._ASKPASS_SCRIPT)
            os.chmod(p, 0o700)
            env = dict(os.environ,
                       STELLAR_SSH_CODE=code, STELLAR_SSH_PASSWORD=password)
            out = subprocess.run([p, prompt], env=env, stdout=subprocess.PIPE,
                                 timeout=10)
            return out.stdout.decode()
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_password_prompts_get_the_password(self):
        for prompt in ("Password: ", "u@host's password:",
                       "请输入密码:", "Enter passphrase for key:"):
            self.assertEqual(self._answer(prompt), 'pw', prompt)

    def test_everything_else_gets_the_code(self):
        for prompt in ("[MFA auth] Please Enter MFA Code:", "Verification code:",
                       "[OTP Code]:", "请输入动态码:", ""):
            self.assertEqual(self._answer(prompt), '123456', prompt)

    def test_code_falls_back_to_password_when_only_password_given(self):
        """只填了密码（有些堡垒机第一步就问密码）也不能空手回答。"""
        self.assertEqual(self._answer("[OTP Code]:", code='', password='pw'), 'pw')


@_POSIX_ONLY
class TestControlPath(unittest.TestCase):
    def test_stable_and_per_host(self):
        a, b = _host('h1'), _host('h2')
        self.assertTrue(ssh_control.control_path_for(a))
        self.assertEqual(ssh_control.control_path_for(a),
                         ssh_control.control_path_for(a))
        self.assertNotEqual(ssh_control.control_path_for(a),
                            ssh_control.control_path_for(b))

    def test_path_stays_within_the_unix_socket_limit(self):
        """sun_path 只有 104 字节，还要给 ssh 自己追加的随机后缀留位置。"""
        p = ssh_control.control_path_for(_host())
        self.assertLess(len(os.path.realpath(os.path.dirname(p)))
                        + 1 + len(os.path.basename(p))
                        + ssh_control._CTL_SUFFIX_BUDGET, 104)

    def test_gives_up_when_temp_dir_is_too_long(self):
        """macOS 的 /var/folders/… 这种长临时目录：宁可不复用连接也别报错。"""
        long_dir = '/tmp/' + 'x' * 95
        with mock.patch.object(ssh_control, '_ctl_dir', lambda: long_dir):
            self.assertEqual(ssh_control.control_path_for(_host()), '')


@_POSIX_ONLY
class TestSshTarget(unittest.TestCase):
    def test_config_hosts_use_their_alias(self):
        """用别名 = 让 ssh 自己解析 ProxyJump/IdentityFile/User，和终端里一致。"""
        self.assertEqual(ssh_control.ssh_target(_host('shanghai-13')), 'shanghai-13')

    def test_memory_hosts_fall_back_to_user_at_host(self):
        h = _host('mem', raw=False)
        self.assertEqual(ssh_control.ssh_target(h), 'u@root@10.0.0.9@bastion.example.com')


@_POSIX_ONLY
class TestMfaLoginArgs(unittest.TestCase):
    def _run_login(self, **kw):
        seen = {}

        def _fake_run(args, **kwargs):
            seen['args'] = args
            seen['env'] = kwargs.get('env') or {}
            # 认证发生的那一刻，askpass 脚本必须还在（ssh 要执行它）
            ap = seen['env'].get('SSH_ASKPASS', '')
            seen['askpass_exists'] = os.path.isfile(ap)
            seen['askpass'] = ap
            return subprocess.CompletedProcess(args, 0, b'', b'')

        with mock.patch.object(ssh_control.subprocess, 'run', _fake_run):
            ssh_control.mfa_login(_host(), **kw)
        return seen

    def test_opens_a_persistent_background_master(self):
        seen = self._run_login(code='123456', hours=8)
        args = seen['args']
        self.assertEqual(args[0], 'ssh')
        # -N 不跑命令、-f 转后台、ControlMaster=yes 即 -M（主连接）
        for flag in ('-N', '-f'):
            self.assertIn(flag, args)
        self.assertIn('ControlPersist=8h', args)
        self.assertIn('ControlMaster=yes', args)
        self.assertIn(f'ControlPath={ssh_control.control_path_for(_host())}', args)
        # 认证这一条必须允许交互（askpass 才有机会作答）
        self.assertIn('BatchMode=no', args)
        self.assertEqual(args[-1], 'bastion')

    def test_code_travels_only_through_the_environment(self):
        seen = self._run_login(code='123456', password='pw')
        self.assertEqual(seen['env']['STELLAR_SSH_CODE'], '123456')
        self.assertEqual(seen['env']['STELLAR_SSH_PASSWORD'], 'pw')
        self.assertEqual(seen['env']['SSH_ASKPASS_REQUIRE'], 'force')
        self.assertTrue(seen['env'].get('DISPLAY'))
        # 命令行上不能出现动态码（ps 能看到整条命令行）
        self.assertNotIn('123456', ' '.join(seen['args']))
        self.assertTrue(seen['askpass_exists'], 'askpass 在认证时必须存在')

    def test_temp_askpass_is_removed_afterwards(self):
        seen = self._run_login(code='123456')
        self.assertFalse(os.path.exists(seen['askpass']))
        self.assertFalse(os.path.exists(os.path.dirname(seen['askpass'])))

    def test_keep_hours_clamped(self):
        self.assertIn('ControlPersist=24h', self._run_login(code='1', hours=999)['args'])
        self.assertIn('ControlPersist=1h', self._run_login(code='1', hours=1)['args'])

    def test_never_expire_maps_to_persist_yes(self):
        """用户选「不自动断开」→ ssh 那边也不能设期限（0h 会立刻断）。"""
        self.assertIn('ControlPersist=yes', self._run_login(code='1', hours=0)['args'])

    def test_empty_code_is_rejected_before_spawning_ssh(self):
        with mock.patch.object(ssh_control.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                ssh_control.mfa_login(_host(), code='', password='')
        run.assert_not_called()

    def test_failure_surfaces_ssh_stderr(self):
        def _fail(args, **kwargs):
            return subprocess.CompletedProcess(
                args, 255, b'', b'debug1: noise\nPermission denied (keyboard-interactive).\n')

        with mock.patch.object(ssh_control.subprocess, 'run', _fail):
            with self.assertRaises(RuntimeError) as cm:
                ssh_control.mfa_login(_host(), code='000000')
        self.assertIn('keyboard-interactive', str(cm.exception))


@_POSIX_ONLY
class TestSessionCommands(unittest.TestCase):
    """普通操作必须复用主连接、绝不自己发起交互。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _sess(self):
        return ssh_control.ControlMasterSession(_host())

    def test_base_args_never_allow_interaction_or_new_masters(self):
        args = self._sess()._base_args()
        self.assertIn('BatchMode=yes', args)      # 后台线程没人能回答提示
        self.assertIn('ControlMaster=no', args)   # 自己建的连接过不了 MFA
        self.assertIn(f'ControlPath={ssh_control.control_path_for(_host())}', args)

    def _capture(self, method, *args, **kwargs):
        sess = self._sess()
        seen = {}

        def _run(cmd, timeout=None):
            seen['cmd'] = cmd
            return seen.get('out', '')

        sess._run = _run
        getattr(sess, method)(*args, **kwargs)
        return seen['cmd']

    def test_quoting_blocks_command_injection(self):
        """远端命令行是唯一的注入面：整条命令必须只解析出 4 个词。"""
        import shlex
        evil = "/data/x'; rm -rf ~; echo '"
        cmd = self._capture('mkdir', evil)
        self.assertEqual(shlex.split(cmd), ['mkdir', '-p', '--', evil])

    def test_tilde_expands_to_home_not_a_literal_folder(self):
        """`~` 被单引号包住就成了一个真叫「~」的目录，必须换成 "$HOME"。"""
        cmd = self._capture('mkdir', '~/work')
        self.assertIn('"$HOME"/', cmd)
        self.assertNotIn("'~/work'", cmd)

    def test_rename_and_remove_shapes(self):
        self.assertEqual(self._capture('rename', '/a/b', '/a/c'),
                         "mv -- /a/b /a/c")
        self.assertEqual(self._capture('remove', '/a/b'), "rm -f -- /a/b")
        self.assertEqual(self._capture('remove_tree', '/a/b'), "rm -rf -- /a/b")

    def test_listdir_asks_for_epoch_timestamps_with_a_fallback(self):
        sess = self._sess()
        sess._run = lambda cmd, timeout=None: (
            "/data/x\n"
            "total 12\n"
            "drwxr-xr-x 2 u g 4096 1712345678 my folder\n"
            "-rw-r--r-- 1 u g  187 1712345679 attn debug.log\n"
        )
        entries = sess.listdir('/data/x', use_cache=False)
        self.assertEqual([e.name for e in entries], ['my folder', 'attn debug.log'])
        self.assertTrue(entries[0].is_dir)
        self.assertEqual(entries[1].size, 187)
        self.assertEqual(entries[1].path, '/data/x/attn debug.log')

    def test_listdir_uses_the_resolved_absolute_path(self):
        """`cd ~ && pwd -P` 出来的绝对路径才是子项 path 的前缀。"""
        sess = self._sess()
        sess._run = lambda cmd, timeout=None: "/home/me\n-rw-r--r-- 1 u g 5 1 a.txt\n"
        entries = sess.listdir('~', use_cache=False)
        self.assertEqual(entries[0].path, '/home/me/a.txt')

    def test_stat_follows_symlinks(self):
        """-L 不加的话软链的 size 是"目标路径字符数"，进度条分母全错。"""
        sess = self._sess()
        seen = {}

        def _run(cmd, timeout=None):
            seen['cmd'] = cmd
            return "file\n-rw-r--r-- 1 0 0 4096 1712345678 f.bin\n"

        sess._run = _run
        st = sess.stat('/data/f.bin')
        self.assertIn('-ldnL', seen['cmd'])
        self.assertEqual(st.size, 4096)
        self.assertFalse(st.is_dir)

    def test_stat_missing_path_raises(self):
        sess = self._sess()
        sess._run = lambda cmd, timeout=None: "none\n"
        with self.assertRaises(FileNotFoundError):
            sess.stat('/nope')

    def test_listdir_cache_roundtrip(self):
        sess = self._sess()
        calls = {'n': 0}

        def _run(cmd, timeout=None):
            calls['n'] += 1
            return "/d\n-rw-r--r-- 1 u g 1 1 a\n"

        sess._run = _run
        sess.listdir('/d')
        sess.listdir('/d')
        self.assertEqual(calls['n'], 1)
        sess.invalidate_cache('/d')
        sess.listdir('/d')
        self.assertEqual(calls['n'], 2)


class _FakeProc:
    """够 upload/download 用的假 Popen：吐固定数据 / 收下写入的数据。"""

    class _Sink(__import__('io').BytesIO):
        """close() 后还能取回写进去的内容（真 Popen 的 stdin 会被关掉）。"""

        def close(self):
            self.written = self.getvalue()

    def __init__(self, out=b'', rc=0, err=b''):
        import io
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.stdin = _FakeProc._Sink()
        self.returncode = rc
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


@_POSIX_ONLY
class TestTransfers(unittest.TestCase):
    """传输走 `ssh + cat`（不是 scp：OpenSSH 9 的 scp 改走 SFTP，引号会进文件名）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ctl-xfer-')
        self.sess = ssh_control.ControlMasterSession(_host())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _arm(self, proc, cmds):
        def _spawn(remote_cmd, **kw):
            cmds.append(remote_cmd)
            return proc
        self.sess._spawn = _spawn
        self.sess._reap = lambda p: None

    def test_download_streams_to_a_part_file_then_renames(self):
        from ssh_session import RemoteEntry
        payload = b'x' * (ssh_control._CHUNK + 7)
        self.sess.stat = lambda p: RemoteEntry(
            name='f', path=p, is_dir=False, size=len(payload), mtime=99)
        cmds = []
        self._arm(_FakeProc(out=payload), cmds)
        local = os.path.join(self.tmp, 'f.bin')
        seen = []
        attr = self.sess.download_with_progress('/d/f.bin', local,
                                                lambda d, t: seen.append((d, t)))
        self.assertEqual(open(local, 'rb').read(), payload)
        self.assertFalse(os.path.exists(local + '.part'), '.part 必须被改名掉')
        self.assertEqual(seen[-1], (len(payload), len(payload)))
        self.assertGreater(len(seen), 1, '大文件应有多次进度回调')
        self.assertEqual(attr.st_size, len(payload))
        self.assertEqual(cmds[0], 'cat -- /d/f.bin')

    def test_failed_download_leaves_no_half_file(self):
        from ssh_session import RemoteEntry
        self.sess.stat = lambda p: RemoteEntry(
            name='f', path=p, is_dir=False, size=10, mtime=0)
        self._arm(_FakeProc(out=b'partial', rc=1, err=b'cat: no such file'), [])
        local = os.path.join(self.tmp, 'f.bin')
        with self.assertRaises(RuntimeError):
            self.sess.download_with_progress('/d/f.bin', local, None)
        self.assertFalse(os.path.exists(local))
        self.assertFalse(os.path.exists(local + '.part'))

    def test_upload_writes_to_part_then_moves_into_place(self):
        payload = b'y' * (ssh_control._CHUNK + 3)
        local = os.path.join(self.tmp, 'up.bin')
        with open(local, 'wb') as fh:
            fh.write(payload)
        proc = _FakeProc()
        cmds = []
        self._arm(proc, cmds)
        moved = []
        self.sess._run = lambda cmd, timeout=None: moved.append(cmd) or ''
        seen = []
        self.sess.upload_with_progress(local, '/d/up.bin',
                                       lambda d, t: seen.append((d, t)))
        self.assertEqual(proc.stdin.written, payload)
        self.assertEqual(cmds[0], 'cat > /d/up.bin.part')
        self.assertEqual(moved, ['mv -- /d/up.bin.part /d/up.bin'])
        self.assertEqual(seen[-1], (len(payload), len(payload)))

    def test_failed_upload_cleans_the_remote_part(self):
        local = os.path.join(self.tmp, 'up.bin')
        with open(local, 'wb') as fh:
            fh.write(b'z')
        self._arm(_FakeProc(rc=1, err=b'disk full'), [])
        ran = []
        self.sess._run = lambda cmd, timeout=None: ran.append(cmd) or ''
        with self.assertRaises(RuntimeError):
            self.sess.upload_with_progress(local, '/d/up.bin', None)
        self.assertEqual(ran, ['rm -f -- /d/up.bin.part'])

    def test_abort_kills_running_children(self):
        proc = _FakeProc()
        self.sess._procs.add(proc)
        self.sess.abort()
        self.assertTrue(proc.killed)


@_POSIX_ONLY
class TestDisconnectReporting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_dead_master_reads_as_a_disconnect_to_the_panel(self):
        """主连接没了要走面板的「重连」提示，而不是一句看不懂的 ssh 报错。"""
        from remote_explorer_widget import RemoteExplorerPanel
        msg = ssh_control.ControlMasterSession._explain(
            'ssh: Control socket connect(/tmp/stellar-ssh/c-x): '
            'No such file or directory\r\n')
        self.assertTrue(RemoteExplorerPanel._looks_like_disconnect(
            RemoteExplorerPanel, msg), msg)
        self.assertIn('MFA', msg)


@_POSIX_ONLY
class TestTerminalTabReusesMaster(unittest.TestCase):
    """终端标签也搭主连接的车 —— 否则 MFA 主机开个终端就要再输一次码。"""

    def test_terminal_command_rides_the_master_when_present(self):
        from ssh_session import build_ssh_terminal_command
        h = _host('bastion')
        with mock.patch('ssh_control.is_supported', lambda: True), \
                mock.patch('ssh_control.master_socket_exists', lambda c: True):
            cmd = build_ssh_terminal_command(h)
        self.assertIn('ControlMaster=no', cmd)
        self.assertIn(ssh_control.control_path_for(h), cmd)

    def test_no_master_no_extra_options(self):
        from ssh_session import build_ssh_terminal_command
        with mock.patch('ssh_control.master_socket_exists', lambda c: False):
            cmd = build_ssh_terminal_command(_host('bastion'))
        self.assertNotIn('ControlPath', cmd)


@_POSIX_ONLY
class TestBastionTerminalMode(unittest.TestCase):
    """JumpServer/koko 不接受"远程命令"：递过去就是一片黑（用户实测）。"""

    def test_bastion_command_carries_no_remote_command(self):
        from ssh_session import build_ssh_terminal_command
        with mock.patch('ssh_control.master_socket_exists', lambda c: False):
            cmd = build_ssh_terminal_command(_host('bastion'), '/root/work',
                                             bastion=True)
        self.assertTrue(cmd.endswith('-tt'), cmd)
        self.assertNotIn('exec ${SHELL', cmd)
        self.assertNotIn('cd ', cmd)
        self.assertIn('bastion', cmd)

    def test_normal_hosts_keep_the_remote_command(self):
        from ssh_session import build_ssh_terminal_command
        with mock.patch('ssh_control.master_socket_exists', lambda c: False):
            cmd = build_ssh_terminal_command(_host('plain'), '/root/work')
        self.assertIn('exec ${SHELL:-/bin/bash} -l', cmd)
        self.assertIn('/root/work', cmd)

    def test_boot_line_carries_env_and_cd(self):
        """远程命令递不进去 → 环境注入和 cd 只能等 shell 出来后敲进去。"""
        from ssh_session import bastion_boot_line
        line = bastion_boot_line("/root/my dir")
        self.assertIn('export', line)
        self.assertIn("cd -- '/root/my dir'", line)   # 带空格的路径要引起来
        self.assertNotIn('\n', line, '补发的是一行，换行由调用方加')

    def test_boot_line_without_cd(self):
        from ssh_session import bastion_boot_line
        self.assertNotIn('cd ', bastion_boot_line(None))

    def test_bastion_still_rides_the_master(self):
        """堡垒机模式也要复用主连接，否则终端又要一个动态码。"""
        from ssh_session import build_ssh_terminal_command
        h = _host('bastion')
        with mock.patch('ssh_control.is_supported', lambda: True), \
                mock.patch('ssh_control.master_socket_exists', lambda c: True):
            cmd = build_ssh_terminal_command(h, bastion=True)
        self.assertIn(ssh_control.control_path_for(h), cmd)
        self.assertIn('ControlMaster=no', cmd)


@_POSIX_ONLY
class TestBastionTerminalWiring(unittest.TestCase):
    """面板→终端标签这一段的接线：MFA 主机要走堡垒机模式并补发启动行。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _mixin():
        # main_window_remote 模块级 `import main_window`，而 main_window 又
        # 导入本模块 —— 先导入 main_window 才不会撞上这个循环（应用里天然
        # 是这个顺序，只有直接单独导入 mixin 才会踩到）。
        import main_window  # noqa: F401
        from main_window_remote import RemotePanelMixin
        return RemotePanelMixin

    def _stub_window(self, is_mfa: bool):
        from PyQt6.QtCore import QObject

        class _Term(QObject):
            def __init__(self):
                super().__init__()
                self.executed = []
                self.sent = []
                self._ssh_host_config = None

            def has_started(self):
                return False

            def setFocus(self):
                pass

            def _start_and_execute(self, cmds):
                self.executed.extend(cmds)

            def send_text(self, text):
                self.sent.append(text)

            def arm_password_autofill(self, pw):
                pass

        term = _Term()
        # 不加 spec：_open_ssh_terminal_tab 还会碰到 MainWindow 本体上的方法
        # （_update_window_title_from_tab / _refresh_tab_badge 等）
        win = mock.MagicMock()
        win.remote_panel = mock.MagicMock()
        win.remote_panel._is_mfa_host.return_value = is_mfa
        win.remote_panel.get_cached_password.return_value = None
        win.tab_widget = mock.MagicMock()
        win.tab_widget.currentIndex.return_value = 0
        win.tab_widget.widget.return_value = mock.MagicMock(_custom_tab_name=None)
        win.tab_terminals = {0: [term]}
        return win, term

    def _open(self, is_mfa, cd_path=None):
        RemotePanelMixin = self._mixin()
        win, term = self._stub_window(is_mfa)
        RemotePanelMixin._open_ssh_terminal_tab(win, _host('bastion'), cd_path)
        return term

    def test_mfa_host_gets_a_bare_tty_session(self):
        """带远程命令的 ssh 在 JumpServer 上就是一片黑（用户实测的现象）。"""
        with mock.patch('ssh_control.master_socket_exists', lambda c: False):
            term = self._open(True, '/root/work')
        self.assertEqual(len(term.executed), 1)
        self.assertTrue(term.executed[0].endswith('-tt'), term.executed[0])
        self.assertNotIn('exec ${SHELL', term.executed[0])

    def test_plain_host_keeps_the_remote_command(self):
        with mock.patch('ssh_control.master_socket_exists', lambda c: False):
            term = self._open(False, '/root/work')
        self.assertIn('exec ${SHELL:-/bin/bash} -l', term.executed[0])

    def test_boot_line_waits_for_the_remote_to_speak_first(self):
        """定时器靠不住：堡垒机登录快慢差很多，早敲会打进认证过程。"""
        RemotePanelMixin = self._mixin()
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Term(QObject):
            raw_output_received = pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.sent = []

            def send_text(self, text):
                self.sent.append(text)

        term = _Term()
        RemotePanelMixin._send_bastion_boot_line(
            mock.MagicMock(), term, "export X=1; cd -- /root", settle_ms=0)
        self.app.processEvents()
        self.assertEqual(term.sent, [], '远端还没出声就不能敲')

        term.raw_output_received.emit('Last login: ...\n[root@host ~]# ')
        self.app.processEvents()
        self.assertEqual(term.sent, ["export X=1; cd -- /root\n"])

        # 只敲一次：后续输出不该再触发
        term.raw_output_received.emit('more output')
        self.app.processEvents()
        self.assertEqual(len(term.sent), 1)

    def test_nothing_is_typed_when_there_is_nothing_to_say(self):
        """没东西要补就别乱敲——网关菜单里敲一行会被当成选项。"""
        RemotePanelMixin = self._mixin()
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Term(QObject):
            raw_output_received = pyqtSignal(str)

            def __init__(self):
                super().__init__()
                self.sent = []

            def send_text(self, text):
                self.sent.append(text)

        term = _Term()
        RemotePanelMixin._send_bastion_boot_line(mock.MagicMock(), term, "",
                                                 settle_ms=0)
        term.raw_output_received.emit('banner')
        self.app.processEvents()
        self.assertEqual(term.sent, [])


@_POSIX_ONLY
class TestLsParsing(unittest.TestCase):
    """ls 输出解析是这个后端最容易悄悄错的地方（错了就是文件名被截断）。"""

    def test_symlink_target_is_stripped_from_the_name(self):
        out = ssh_control.parse_ls_output(
            'lrwxrwxrwx 1 u g 11 1712345680 link -> /tmp/target', '/d')
        self.assertEqual([e.name for e in out], ['link'])
        self.assertEqual(out[0].path, '/d/link')

    def test_bsd_style_date_columns(self):
        """非 GNU 的 ls 不认 --time-style，回退格式也得解析对（含空格文件名）。"""
        out = ssh_control.parse_ls_output(
            '-rw-r--r--  1 u  g  187 Jul 10 12:33 attn debug.log\n'
            'drwxr-xr-x  2 u  g 4096 Jul 10  2023 old stuff\n'
            '-rw-r--r--  1 u  g   42 2024-01-02 03:04 iso file.txt\n', '/d')
        self.assertEqual([e.name for e in out],
                         ['attn debug.log', 'old stuff', 'iso file.txt'])
        self.assertEqual(out[0].size, 187)
        self.assertTrue(out[1].is_dir)
        self.assertGreater(out[2].mtime, 0, 'ISO 日期应能解出时间戳')

    def test_total_line_and_dot_entries_skipped(self):
        out = ssh_control.parse_ls_output(
            'total 40\n'
            'drwxr-xr-x 2 u g 4096 1712345678 .\n'
            'drwxr-xr-x 9 u g 4096 1712345678 ..\n'
            'drwxr-xr-x 2 u g 4096 1712345678 .cache\n', '/d')
        self.assertEqual([e.name for e in out], ['.cache'])

    def test_garbage_lines_are_ignored(self):
        out = ssh_control.parse_ls_output(
            'ls: cannot access x: Permission denied\n'
            '-rw-r--r-- 1 u g 1 1712345678 ok.txt\n', '/d')
        self.assertEqual([e.name for e in out], ['ok.txt'])


if __name__ == '__main__':
    unittest.main()
