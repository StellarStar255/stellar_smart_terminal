# -*- coding: utf-8 -*-
"""Remote Explorer 的路径行改成与本地 Explorer 同款面包屑。

用户反馈新版本地 Explorer 的面包屑好用，远程面板还是一行可编辑路径框。
现在远程也用 `_BreadcrumbBar`（切分规则固定 posixpath：远端永远是 POSIX
路径，Windows 宿主机上不能用 os.path），双击进入编辑、Esc 退回，段右键给
书签 / 在此打开终端 / 复制路径。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_breadcrumb.py -v
"""
import os
import posixpath
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication


class _HostConfig:
    alias = "box"


class _StubSession:
    """只够面包屑导航用：submit 同步执行并返回带 add_done_callback 的假 future"""
    host_config = _HostConfig()

    def __init__(self):
        self.stat_calls = []

    def home(self):
        return "/home/u"

    def stat(self, path):
        from ssh_session import RemoteEntry
        self.stat_calls.append(path)
        return RemoteEntry(name=posixpath.basename(path) or "/", path=path,
                           is_dir=True, size=0, mtime=0)

    def submit(self, fn, *args):
        class _F:
            def __init__(self, r):
                self._r = r
            def result(self):
                return self._r
            def add_done_callback(self, cb):
                cb(self)
        return _F(fn(*args))


class TestRemoteBreadcrumb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import remote_bookmarks
        cls.bm = remote_bookmarks

    def _panel(self):
        from remote_explorer_widget import RemoteExplorerPanel
        p = RemoteExplorerPanel(theme={})
        p.resize(700, 500)
        self.addCleanup(p.deleteLater)
        return p

    def test_posix_segments_even_on_windows_style_host(self):
        from explorer_common import _BreadcrumbBar
        bar = _BreadcrumbBar(path_module=posixpath)
        bar.set_path("/mnt/data/zy/tmp")
        self.assertEqual(bar.segments(),
                         [("/", "/"), ("mnt", "/mnt"), ("data", "/mnt/data"),
                          ("zy", "/mnt/data/zy"), ("tmp", "/mnt/data/zy/tmp")])

    def test_panel_uses_breadcrumb_and_hides_edit_by_default(self):
        p = self._panel()
        self.assertTrue(hasattr(p, 'breadcrumb'), "远程面板没有面包屑")
        self.assertIs(p.breadcrumb._pm, posixpath, "远端路径必须按 posixpath 切分")
        p._set_path_bar("/mnt/data/tmp")
        self.assertEqual(p.breadcrumb.path(), "/mnt/data/tmp")
        self.assertEqual(p._path_edit.text(), "/mnt/data/tmp")
        self.assertTrue(p._path_edit.isHidden(), "默认应显示面包屑而不是编辑框")

    def test_segment_click_navigates_via_stat(self):
        p = self._panel()
        sess = _StubSession()
        p._session = sess
        p._current_path = "/mnt/data/tmp"
        p._set_path_bar(p._current_path)
        # 只考察导航链路；树的填充要真会话的 listdir，桩掉（槽里抛异常会 abort 进程）
        p._populate_tree_root = lambda: None
        p.breadcrumb.path_selected.emit("/mnt/data")
        self.app.processEvents()
        self.assertIn("/mnt/data", sess.stat_calls, "点面包屑段应 stat 后跳转")
        self.assertEqual(p._current_path, "/mnt/data")
        self.assertEqual(p.breadcrumb.path(), "/mnt/data")

    def test_double_click_edits_then_escape_restores(self):
        p = self._panel()
        p._current_path = "/mnt/data"
        p._set_path_bar(p._current_path)
        p.breadcrumb.edit_requested.emit()
        self.assertFalse(p._path_edit.isHidden())
        self.assertTrue(p.breadcrumb.isHidden())
        self.assertEqual(p._path_edit.text(), "/mnt/data")
        QApplication.sendEvent(p._path_edit, QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        self.assertTrue(p._path_edit.isHidden(), "Esc 应退回面包屑")
        self.assertFalse(p.breadcrumb.isHidden())

    def test_segment_menu_offers_bookmark_terminal_copy(self):
        from i18n import t
        p = self._panel()
        p._session = _StubSession()
        self.bm.clear_for("box")
        try:
            menu = p._build_path_segment_menu("/mnt/data")
            texts = [a.text() for a in menu.actions() if a.text()]
            self.assertIn(t("remote.bookmark_add", path="/mnt/data"), texts)
            self.assertIn(t("remote.open_terminal_here"), texts)
            self.assertIn(t("explorer.copy_path"), texts)
            menu.actions()[0].trigger()
            self.assertTrue(self.bm.is_bookmarked("box", "/mnt/data"))
            menu2 = p._build_path_segment_menu("/mnt/data")
            self.assertEqual(menu2.actions()[0].text(),
                             t("remote.bookmark_remove", path="/mnt/data"))
        finally:
            self.bm.clear_for("box")

    def test_theme_colours_reach_breadcrumb(self):
        p = self._panel()
        with mock.patch.object(p.breadcrumb, 'set_colors') as spy:
            p.apply_theme({'text': '#111111', 'text_dim': '#222222'})
        spy.assert_called_with('#111111', '#222222')


if __name__ == '__main__':
    unittest.main()
