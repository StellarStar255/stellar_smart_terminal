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
        second = self.win._remote_session_for_host(self.host)   # first 仍未 alive
        self.assertIsNot(second, first)
        self.assertIn('disconnect', first.calls, "被换掉的旧实例必须 disconnect（关 executor）")
        self.assertIs(self.win._upload_sessions['gpu13'], second)

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
        win._ssh_host_for_terminal = lambda term: host
        win._remote_session_for_host = lambda h: sess
        term = _FakeTerm()
        results = []
        term.remote_upload_done.connect(lambda local, remote: results.append((local, remote)))
        tmp = tempfile.mkdtemp(prefix='paste-')
        local = os.path.join(tmp, 'shot.png')
        with open(local, 'wb') as fh:
            fh.write(b'\x89PNG')

        self.assertTrue(win._upload_pasted_media_for_terminal(term, local))
        self.app.processEvents()
        self.assertEqual(results, [(local, '')], "会话死了 → 回退敲本地路径")
        self.assertNotIn('home', sess.calls)
        self.assertFalse(any(isinstance(c, tuple) and c[0] in ('mkdir', 'upload')
                             for c in sess.calls), sess.calls)


if __name__ == '__main__':
    unittest.main()
