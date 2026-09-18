"""远端粘图的临时会话在主连接陈旧时不能越积越多（2026-09 审查 C 项）。

_remote_session_for_host 对「有 socket 但主连接已死」的主机会起一条临时
ControlMasterSession；connect_async() 无码无密码必失败，connect_failed 无人
听；下次粘贴 is_alive() 为 False 又起一条覆盖缓存——旧实例带 parent 被 C++
持有、executor 永不 shutdown。而且 _job 照样 home()/mkdir/upload。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_paste_session_2026_09_ssh.py -q
"""
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QObject, pyqtSignal  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from main_window_remote import RemotePanelMixin  # noqa: E402
from ssh_session import HostConfig  # noqa: E402


class _FakeAdhoc(QObject):
    """替身 ControlMasterSession：不连、不起线程，只记录调用。"""

    connected = pyqtSignal()
    connect_failed = pyqtSignal(str)
    disconnected = pyqtSignal()
    instances: list = []

    def __init__(self, host_config, parent=None):
        super().__init__(parent)
        self.host_config = host_config
        self.alive = False
        self.calls = []
        _FakeAdhoc.instances.append(self)

    def is_alive(self):
        return self.alive

    def is_connected(self):
        return self.alive

    def connect_async(self, *a, **kw):
        self.calls.append('connect')
        return Future()

    def disconnect(self):
        self.calls.append('disconnect')
        self.alive = False

    def home(self):
        self.calls.append('home')
        return '/'

    def mkdir(self, path):
        self.calls.append(('mkdir', path))

    def upload(self, local, remote):
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


class _FakeTerm(QObject):
    remote_upload_done = pyqtSignal(str, str)


def _pump_until(app, cond, timeout=5.0):
    """带截止的等待：ps 探测/上传都在工作线程，固定次数 processEvents 靠不住。"""
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return cond()


class _Win(RemotePanelMixin, QObject):
    def __init__(self):
        super().__init__()
        self.statusbar = mock.Mock()
        self.remote_panel = None


class AdhocSessionCache(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        _FakeAdhoc.instances.clear()
        self.win = _Win()
        self.host = HostConfig(alias='gpu13', hostname='10.0.0.1')
        self.patches = [
            mock.patch('ssh_control.ControlMasterSession', _FakeAdhoc),
            mock.patch('ssh_control.is_supported', return_value=True),
            mock.patch('ssh_control.master_socket_exists', return_value=True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_dead_cached_session_is_disconnected_before_replacement(self):
        first = self.win._remote_session_for_host(self.host)
        self.assertIs(first, _FakeAdhoc.instances[0])
        # 连上过、之后主连接死了（is_alive 变 False、且不再处于连接中）
        first.alive = True
        first.connected.emit()
        self.app.processEvents()
        first.alive = False
        second = self.win._remote_session_for_host(self.host)
        self.assertIsNot(second, first)
        self.assertIn('disconnect', first.calls, "被换掉的旧实例必须 disconnect（关 executor）")
        self.assertIs(self.win._upload_sessions['gpu13'], second)

    def test_connecting_session_is_reused_not_disconnected(self):
        """连续粘两张图：第一条临时会话还在连接中（is_alive 尚为 False），
        第二次粘贴必须复用它——以前会把它 disconnect（abort 杀掉正在跑的
        ssh）换一条新的，第一张图就传废了。"""
        first = self.win._remote_session_for_host(self.host)
        second = self.win._remote_session_for_host(self.host)   # 还没 connected / failed
        self.assertIs(second, first)
        self.assertNotIn('disconnect', first.calls)
        self.assertEqual(len(_FakeAdhoc.instances), 1)
        self.assertEqual(first.calls.count('connect'), 1)

    def test_connect_failed_evicts_cache_and_tells_the_user(self):
        sess = self.win._remote_session_for_host(self.host)
        sess.connect_failed.emit('主连接已断开（connection lost）')
        self.app.processEvents()
        self.assertNotIn('gpu13', self.win._upload_sessions)
        self.assertIn('disconnect', sess.calls)
        self.win.statusbar.showMessage.assert_called()
        msg = self.win.statusbar.showMessage.call_args.args[0]
        self.assertIn('gpu13', msg)

    def test_live_cached_session_is_reused(self):
        first = self.win._remote_session_for_host(self.host)
        first.alive = True
        self.assertIs(self.win._remote_session_for_host(self.host), first)
        self.assertEqual(len(_FakeAdhoc.instances), 1)


class DeadSessionJobFallsBack(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_job_does_not_touch_remote_when_session_is_dead(self):
        win = _Win()
        host = HostConfig(alias='gpu13', hostname='10.0.0.1')
        sess = _FakeAdhoc(host)
        sess.alive = False
        win._known_remote_hosts = lambda: [host]
        win._ssh_args_under = lambda pid, processes=None: "ssh -tt gpu13"
        win._remote_session_for_host = lambda h: sess
        term = _FakeTerm()
        results = []
        term.remote_upload_done.connect(lambda local, remote: results.append((local, remote)))
        tmp = tempfile.mkdtemp(prefix='paste-')
        local = os.path.join(tmp, 'shot.png')
        with open(local, 'wb') as fh:
            fh.write(b'\x89PNG')

        self.assertTrue(win._upload_pasted_media_for_terminal(term, local))
        _pump_until(self.app, lambda: results)
        self.assertEqual(results, [(local, '')], "会话死了 → 回退敲本地路径")
        self.assertNotIn('home', sess.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] in ('mkdir', 'upload')
                             for c in sess.calls), sess.calls)


if __name__ == '__main__':
    unittest.main()


class _FakeIdlePanelSession(_FakeAdhoc):
    """替身 paramiko 面板会话：transport 闲置掉线（is_alive=False），但各操作
    自带自动重连——_reconnect_or_fail_fast 成功后就活了。"""

    def __init__(self, host_config, reconnect_ok=True):
        super().__init__(host_config)
        self.reconnect_ok = reconnect_ok

    def _reconnect_or_fail_fast(self):
        self.calls.append('reconnect')
        if not self.reconnect_ok:
            raise RuntimeError('reconnect failed (gpu13)')
        self.alive = True


class IdlePanelSessionReconnects(unittest.TestCase):
    """v1.31.0 回归：面板会话闲置掉线后粘图直接退回本地路径。

    合盖/换网后 paramiko transport 死了但 sftp 句柄还在：面板本身下一次操作
    会自动重连，粘图上传却被 _job 开头的 is_alive 门槛拦下，敲出来的是
    本地路径。能自愈的会话该先重连再传，连不上才回退。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _run(self, sess):
        win = _Win()
        host = HostConfig(alias='gpu13', hostname='10.0.0.1')
        win._known_remote_hosts = lambda: [host]
        win._ssh_args_under = lambda pid, processes=None: "ssh -tt gpu13"
        win._remote_session_for_host = lambda h: sess
        term = _FakeTerm()
        results = []
        term.remote_upload_done.connect(lambda local, remote: results.append((local, remote)))
        tmp = tempfile.mkdtemp(prefix='paste-')
        local = os.path.join(tmp, 'shot.png')
        with open(local, 'wb') as fh:
            fh.write(b'\x89PNG')
        self.assertTrue(win._upload_pasted_media_for_terminal(term, local))
        _pump_until(self.app, lambda: results)
        return local, results

    def test_idle_session_reconnects_then_uploads(self):
        sess = _FakeIdlePanelSession(HostConfig(alias='gpu13', hostname='10.0.0.1'))
        local, results = self._run(sess)
        self.assertIn('reconnect', sess.calls)
        uploads = [c for c in sess.calls if isinstance(c, tuple) and c[0] == 'upload']
        self.assertEqual(len(uploads), 1, sess.calls)
        self.assertEqual(results, [(local, '/.images/shot.png')], "重连成功后该敲远端路径")

    def test_reconnect_failure_still_falls_back_to_local_path(self):
        sess = _FakeIdlePanelSession(HostConfig(alias='gpu13', hostname='10.0.0.1'),
                                     reconnect_ok=False)
        local, results = self._run(sess)
        self.assertIn('reconnect', sess.calls)
        self.assertEqual(results, [(local, '')], "重连失败 → 回退敲本地路径")
        self.assertFalse(any(isinstance(c, tuple) and c[0] in ('mkdir', 'upload')
                             for c in sess.calls), sess.calls)


class _FakePanel(QObject):
    """替身 Remote 面板：只有粘图引导用到的几个成员。"""
    host_connected = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._session = None
        self._current_path = '/proj'
        self.connect_calls = []
        self.next_session = None     # _connect_to 后立刻挂上的会话（None = 用户取消了登录框）

    def _connect_to(self, host, **kw):
        self.connect_calls.append(host.alias)
        self._session = self.next_session


class NoSessionAsksToConnect(unittest.TestCase):
    """终端在 ssh 里、但面板没连着这台机器也没有主连接：问一句「连接并上传？」，
    同意就连面板、连上后补传一次；拒绝 / 连接失败 / 取消登录框 → 退回本地路径。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = HostConfig(alias='gpu13', hostname='10.0.0.1')
        self.win = _Win()
        self.panel = _FakePanel()
        self.win.remote_panel = self.panel
        self.win.remote_panel_visible = True
        self.win._ensure_remote_panel = lambda: self.panel
        self.win._toggle_remote_panel = lambda: None
        self.win._known_remote_hosts = lambda: [self.host]
        self.win._ssh_args_under = lambda pid, processes=None: "ssh -tt gpu13"
        for p_ in (mock.patch('ssh_control.is_supported', return_value=True),
                   mock.patch('ssh_control.master_socket_exists', return_value=False)):
            p_.start()
            self.addCleanup(p_.stop)
        self.term = _FakeTerm()
        self.results = []
        self.term.remote_upload_done.connect(
            lambda local, remote: self.results.append((local, remote)))
        tmp = tempfile.mkdtemp(prefix='paste-')
        self.local = os.path.join(tmp, 'shot.png')
        with open(self.local, 'wb') as fh:
            fh.write(b'\x89PNG')

    def test_accept_connects_panel_and_uploads_after_connected(self):
        self.win._ask_connect_for_paste = lambda host: True
        sess = _FakeAdhoc(self.host)
        sess.alive = True
        self.panel.next_session = sess
        self.assertTrue(self.win._upload_pasted_media_for_terminal(self.term, self.local))
        _pump_until(self.app, lambda: self.panel.connect_calls, timeout=5)
        self.assertEqual(self.panel.connect_calls, ['gpu13'])
        self.assertEqual(self.results, [], "连上之前不该敲任何路径")
        self.panel.host_connected.emit(self.host)
        _pump_until(self.app, lambda: self.results)
        self.assertEqual(self.results, [(self.local, '/proj/.images/shot.png')])
        self.assertIn(('upload', self.local, '/proj/.images/shot.png'), sess.calls)

    def test_decline_types_local_path_without_connecting(self):
        self.win._ask_connect_for_paste = lambda host: False
        self.assertTrue(self.win._upload_pasted_media_for_terminal(self.term, self.local))
        _pump_until(self.app, lambda: self.results)
        self.assertEqual(self.results, [(self.local, '')])
        self.assertEqual(self.panel.connect_calls, [])

    def test_connect_failure_falls_back_to_local_path(self):
        self.win._ask_connect_for_paste = lambda host: True
        sess = _FakeAdhoc(self.host)          # 连接中（alive=False）
        self.panel.next_session = sess
        self.assertTrue(self.win._upload_pasted_media_for_terminal(self.term, self.local))
        _pump_until(self.app, lambda: self.panel.connect_calls)
        self.panel.error_occurred.emit('auth failed')
        _pump_until(self.app, lambda: self.results)
        self.assertEqual(self.results, [(self.local, '')])
        self.assertFalse(any(isinstance(c, tuple) and c[0] == 'upload' for c in sess.calls))

    def test_cancelled_login_dialog_falls_back_immediately(self):
        self.win._ask_connect_for_paste = lambda host: True
        self.panel.next_session = None       # _connect_to 回来 _session 仍是 None：用户取消了 MFA 框
        self.assertTrue(self.win._upload_pasted_media_for_terminal(self.term, self.local))
        self.assertTrue(_pump_until(self.app, lambda: self.results, timeout=5),
                        "取消登录框后不该等 60s 超时才回退")
        self.assertEqual(self.results, [(self.local, '')])
