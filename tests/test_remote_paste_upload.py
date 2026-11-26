# -*- coding: utf-8 -*-
"""SSH 标签里粘贴的图片先上传到远端，再把远端路径敲进终端。

用户报告：本地截图粘进远端 Claude Code 没用——敲进去的是 Mac 上的本地路径，
远端根本读不到。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_paste_upload.py -v
"""
import os
import sys
import tempfile
import threading
import time
import shlex
import unittest
from concurrent.futures import Future

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from ssh_session import HostConfig


class _FakeSession:
    """只实现 _upload_pasted_media_for_terminal 用到的那几个接口。"""

    def __init__(self, alias, fail=False):
        self.host_config = HostConfig(alias=alias, hostname='1.2.3.4')
        self.calls = []
        self.fail = fail

    def is_connected(self):
        return True

    def home(self):
        return '/root'

    def mkdir(self, path):
        self.calls.append(('mkdir', path))

    def upload(self, local, remote):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(('upload', local, remote))

    def invalidate_cache(self, path):
        pass

    def submit(self, fn, *a, **kw):
        fut = Future()
        try:
            fut.set_result(fn(*a, **kw))
        except Exception as e:
            fut.set_exception(e)
        return fut


class _FakePanel:
    def __init__(self, session, path):
        self._session = session
        self._current_path = path


class TestRemotePasteUpload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window
        cls.win = main_window.MainWindow()
        cls.win.show()
        cls.app.processEvents()
        cls.tmp = tempfile.mkdtemp(prefix='stellar-paste-')
        cls.local = os.path.join(cls.tmp, 'shot one.png')
        with open(cls.local, 'wb') as f:
            f.write(b'\x89PNG')

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtGui import QCloseEvent
        cls.win._force_closing = True
        QApplication.sendEvent(cls.win, QCloseEvent())
        cls.win.deleteLater()
        cls.app.processEvents()
        del cls.win

    def _wait(self, cond, timeout=5.0):
        """ps 探测在工作线程：带截止地等，别数 processEvents 次数。"""
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        return cond()

    def _terminal(self, ssh_running=True):
        term = self.win.tab_terminals[self.win.tab_widget.currentIndex()][0]
        host = HostConfig(alias='gpu13', hostname='10.0.0.1')
        term._ssh_host_config = host
        # 是否在 ssh 里现在看进程树：这里直接桩掉"shell 子孙里的 ssh 命令行"
        self.win._known_remote_hosts = lambda: [host]
        self.win._ssh_args_under = (
            (lambda pid, processes=None: "ssh -tt gpu13") if ssh_running
            else (lambda pid, processes=None: None))
        typed = []
        term._write_paste = lambda data: typed.append(data) or True
        pasted = []
        term.image_pasted.connect(pasted.append)
        return term, typed, pasted

    def test_ssh_tab_types_remote_path_after_upload(self):
        term, typed, pasted = self._terminal()
        sess = _FakeSession('gpu13')
        self.win.remote_panel = _FakePanel(sess, '/data/proj')

        term._deliver_media_path(self.local, prefix='', suffix='')
        self._wait(lambda: typed)

        self.assertEqual(len(typed), 1)
        remote_path = shlex.split(typed[0].decode())[0]
        self.assertTrue(remote_path.startswith('/data/proj/.images/'))
        self.assertTrue(remote_path.endswith('.png'))
        self.assertIn(('mkdir', '/data/proj/.images'), sess.calls)
        self.assertIn(('upload', self.local, remote_path), sess.calls)
        self.assertEqual(pasted, [self.local])       # 画廊仍用本地缩略图
        self.assertEqual(term._pending_remote_pastes, {})

    def test_media_file_keeps_prefix_and_suffix(self):
        term, typed, pasted = self._terminal()
        self.win.remote_panel = _FakePanel(_FakeSession('gpu13'), '/data/proj')
        term._deliver_media_path(self.local, prefix='@', suffix=' ')
        self._wait(lambda: typed)
        self.assertEqual(len(typed), 1)
        self.assertTrue(typed[0].startswith(b'@/data/proj/.images/'))
        self.assertTrue(typed[0].endswith(b'.png '))

    def test_panel_on_other_host_falls_back_to_local_path(self):
        term, typed, pasted = self._terminal()
        self.win.remote_panel = _FakePanel(_FakeSession('other-host'), '/x')
        self.win._ask_connect_for_paste = lambda host: False   # 没会话时的引导框：这里选「用本地路径」
        term._deliver_media_path(self.local, prefix='', suffix='')
        self._wait(lambda: typed)
        self.assertEqual(len(typed), 1)
        self.assertEqual(shlex.split(typed[0].decode()), [self.local])
        self.assertEqual(pasted, [self.local])

    def test_upload_failure_falls_back_to_local_path(self):
        term, typed, pasted = self._terminal()
        self.win.remote_panel = _FakePanel(_FakeSession('gpu13', fail=True), '/data/proj')
        term._deliver_media_path(self.local, prefix='', suffix='')
        self._wait(lambda: typed)
        self.assertEqual(len(typed), 1)
        self.assertEqual(shlex.split(typed[0].decode()), [self.local])
        self.assertEqual(term._pending_remote_pastes, {})

    def test_local_tab_is_untouched(self):
        term, typed, pasted = self._terminal(ssh_running=False)
        self.win.remote_panel = _FakePanel(_FakeSession('gpu13'), '/data/proj')
        term._deliver_media_path(self.local, prefix='', suffix='')
        self._wait(lambda: typed)
        self.assertEqual(len(typed), 1)
        self.assertEqual(shlex.split(typed[0].decode()), [self.local])

    def test_unknown_destination_with_known_remote_argument_uses_local_path(self):
        term, typed, _ = self._terminal()
        session = _FakeSession('gpu13')
        self.win.remote_panel = _FakePanel(session, '/data/proj')
        self.win._ssh_args_under = lambda pid: 'ssh unknown-host echo gpu13'
        term._deliver_media_path(self.local)
        self.assertTrue(self._wait(lambda: typed))
        self.assertEqual(shlex.split(typed[0].decode()), [self.local])
        self.assertFalse(session.calls)

    def test_process_scan_runs_off_gui_thread(self):
        """识别 ssh 要起一次 ps（最坏 3 秒）：不能卡在 GUI 线程里。"""
        term, typed, pasted = self._terminal()
        self.win.remote_panel = _FakePanel(_FakeSession('gpu13'), '/data/proj')
        seen = []
        self.win._ssh_args_under = (
            lambda pid, processes=None: seen.append(threading.get_ident()) or "ssh -tt gpu13")
        term._deliver_media_path(self.local, prefix='', suffix='')
        self._wait(lambda: typed)
        self.assertEqual(len(typed), 1)
        remote_path = shlex.split(typed[0].decode())[0]
        self.assertTrue(remote_path.startswith('/data/proj/.images/'))
        self.assertTrue(remote_path.endswith('.png'))
        self.assertTrue(seen, "没做 ssh 探测")
        self.assertNotIn(threading.get_ident(), seen, "ps 探测在 GUI 线程里跑了")




class TestSshDetection(unittest.TestCase):
    """不靠标签打开时记的主机：看 shell 子孙里有没有 ssh、命令行对到哪台。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window

    def test_ssh_args_under_walks_descendants(self):
        rows = [(100, 1, "/bin/zsh -l"), (200, 100, "python3 foo.py"),
                (300, 200, "/usr/bin/ssh -o ControlPath=/tmp/c-1 user@h:22"),
                (400, 1, "ssh other")]
        M = self.mw.MainWindow
        self.assertEqual(M._ssh_args_under(100, rows), "/usr/bin/ssh -o ControlPath=/tmp/c-1 user@h:22")
        self.assertIsNone(M._ssh_args_under(200, [(100, 1, "/bin/zsh")]))
        self.assertIsNone(M._ssh_args_under(None, rows))

    def test_match_prefers_controlpath_then_target_then_alias(self):
        import ssh_control
        M = self.mw.MainWindow
        a = HostConfig(alias='gpu13', hostname='bastion.example', user='me@root@10.0.0.13')
        b = HostConfig(alias='gpu16', hostname='bastion.example', user='me@root@10.0.0.16')
        c = HostConfig(alias='lab', hostname='192.168.1.9', user='huang')
        ctl = ssh_control.control_path_for(b)
        self.assertIs(M._match_host_for_ssh_args(
            f"ssh -o ControlMaster=no -o ControlPath={ctl} me@root@10.0.0.13@bastion.example", [a, b, c]), b)
        self.assertIs(M._match_host_for_ssh_args("ssh -tt me@root@10.0.0.13@bastion.example", [a, b, c]), a)
        self.assertIs(M._match_host_for_ssh_args("ssh lab", [a, b, c]), c)
        self.assertIs(M._match_host_for_ssh_args("ssh -p 22 192.168.1.9", [a, b, c]), c)
        self.assertIsNone(M._match_host_for_ssh_args("ssh nowhere", [a, b, c]))


    def test_only_destination_and_real_control_options_select_a_host(self):
        import ssh_control

        match = self.mw.MainWindow._match_host_for_ssh_args
        jump = HostConfig(alias="gateway", hostname="jump.example", user="ops")
        target = HostConfig(alias="worker", hostname="worker.example", user="runner")
        hosts = [jump, target]
        socket = ssh_control.control_path_for(jump)
        for command, expected in [
            ("ssh -J gateway worker", target),
            ("ssh -ttJgateway worker echo ops@jump.example", target),
            ("ssh -F gateway unknown echo worker", None),
            ("ssh unknown echo gateway", None),
            (f"ssh worker echo ControlPath={socket}", target),
            (f"ssh worker -S {socket}", jump),
            ("ssh -o 'ProxyCommand=ssh gateway -W %h:%p' worker", target),
            ("ssh runner@worker echo ops@jump.example", target),
        ]:
            with self.subTest(command=command):
                self.assertIs(match(command, hosts, preferred=jump), expected)

    def test_explicit_user_and_port_disambiguate_shared_hostname(self):
        match = self.mw.MainWindow._match_host_for_ssh_args
        a = HostConfig(alias='prod', hostname='node.example', user='alice', port=22)
        b = HostConfig(alias='lab', hostname='node.example', user='bob', port=2202)
        # Windows and tabs may retain distinct copies of the same saved profile.
        from dataclasses import replace
        saved_copy = replace(b)
        self.assertIs(match('ssh bob@node.example', [a, b], preferred=saved_copy), saved_copy)
        for args, expected in [
            ('ssh node.example', None),
            ('ssh -l bob -p2202 node.example', b),
            ('ssh -o "User bob" -oPort=2202 node.example', b),
            ('ssh ssh://bob@node.example:2202 -p 22', b),
            ('ssh bob@node.example -l alice', b),
            ('ssh -p 22 bob@node.example', None),
            ('ssh -oHostName=other.example prod', None),
        ]:
            with self.subTest(args=args):
                self.assertIs(match(args, [a, b], preferred=a), expected)

if __name__ == '__main__':
    unittest.main()
