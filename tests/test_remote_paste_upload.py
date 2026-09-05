# -*- coding: utf-8 -*-
"""SSH 标签里粘贴的图片先上传到远端，再把远端路径敲进终端。

用户报告：本地截图粘进远端 Claude Code 没用——敲进去的是 Mac 上的本地路径，
远端根本读不到。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_paste_upload.py -v
"""
import os
import sys
import tempfile
import unittest
from concurrent.futures import Future

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
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

    def _terminal(self):
        term = self.win.tab_terminals[self.win.tab_widget.currentIndex()][0]
        term._ssh_host_config = HostConfig(alias='gpu13', hostname='10.0.0.1')
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
        self.app.processEvents()

        self.assertEqual(typed, [b'/data/proj/.images/shot one.png'])
        self.assertIn(('mkdir', '/data/proj/.images'), sess.calls)
        self.assertIn(('upload', self.local, '/data/proj/.images/shot one.png'), sess.calls)
        self.assertEqual(pasted, [self.local])       # 画廊仍用本地缩略图
        self.assertEqual(term._pending_remote_pastes, {})

    def test_media_file_keeps_prefix_and_suffix(self):
        term, typed, pasted = self._terminal()
        self.win.remote_panel = _FakePanel(_FakeSession('gpu13'), '/data/proj')
        term._deliver_media_path(self.local, prefix='@', suffix=' ')
        self.app.processEvents()
        self.assertEqual(typed, [b'@/data/proj/.images/shot one.png '])

    def test_panel_on_other_host_falls_back_to_local_path(self):
        term, typed, pasted = self._terminal()
        self.win.remote_panel = _FakePanel(_FakeSession('other-host'), '/x')
        term._deliver_media_path(self.local, prefix='', suffix='')
        self.assertEqual(typed, [self.local.encode()])
        self.assertEqual(pasted, [self.local])

    def test_upload_failure_falls_back_to_local_path(self):
        term, typed, pasted = self._terminal()
        self.win.remote_panel = _FakePanel(_FakeSession('gpu13', fail=True), '/data/proj')
        term._deliver_media_path(self.local, prefix='', suffix='')
        self.app.processEvents()
        self.assertEqual(typed, [self.local.encode()])
        self.assertEqual(term._pending_remote_pastes, {})

    def test_local_tab_is_untouched(self):
        term, typed, pasted = self._terminal()
        term._ssh_host_config = None
        self.win.remote_panel = _FakePanel(_FakeSession('gpu13'), '/data/proj')
        term._deliver_media_path(self.local, prefix='', suffix='')
        self.assertEqual(typed, [self.local.encode()])


if __name__ == '__main__':
    unittest.main()
