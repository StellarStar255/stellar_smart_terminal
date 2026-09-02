# -*- coding: utf-8 -*-
"""侧面板懒加载的回归测试。

审查发现：每个窗口构造时都把 Explorer / Git / Remote 三个面板控件 eager 建好，
各自读配置、起常驻定时器（Git 的 3 分钟 fetch、Explorer 的 60s 安全网、
Remote 的自动刷新），顶层 import 链还经 main_window_remote 拉进 paramiko。
一个窗口启动至少解析同一份配置 6 次，而默认三个面板全是隐藏的。

修复：容器和标题栏照旧 eager（便宜，且 hasattr 守卫全部保持有效），重量级
面板控件推迟到首次打开（`_ensure_*_panel`）；Explorer 沿用启动后 1.2s 的
空闲预热，Git / Remote 不用就永远不建。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_lazy_panels.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication


class TestLazyPanels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw_mod = main_window

    def _new_window(self):
        # 前一个用例关窗时会把"面板可见"落盘，下一窗口构造时按配置恢复
        # 就会合法地把面板建出来；这里把恢复项清掉，只考察懒加载本身
        import app_config
        app_config.update_config(
            {'explorer_panel_visible': False, 'git_panel_visible': False},
            description='test-lazy-panels')
        win = self.mw_mod.MainWindow()
        self.addCleanup(self._dispose, win)
        return win

    def _dispose(self, win):
        try:
            # 未显示过的窗口 close() 不派发 closeEvent；直接投递 QCloseEvent
            # 走真实清理路径（Git 面板的后台线程要在销毁前 shutdown）
            QApplication.sendEvent(win, QCloseEvent())
        finally:
            win.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_fresh_window_builds_no_heavy_panel(self):
        win = self._new_window()
        for name in ('explorer_panel', 'git_panel', 'remote_panel'):
            self.assertFalse(
                hasattr(win, name),
                f"{name} 在窗口构造期间就被建出来了（应推迟到首次打开）")
        # 容器和标题栏照旧存在，守卫/主题代码依赖它们
        self.assertTrue(hasattr(win, 'explorer_panel_container'))
        self.assertTrue(hasattr(win, 'explorer_splitter'))
        self.assertTrue(hasattr(win, 'remote_splitter'))
        self.assertTrue(hasattr(win, 'editor_area'))

    def test_theme_language_zoom_survive_without_panels(self):
        """面板未建时，主题/语言/缩放遍历不能崩"""
        win = self._new_window()
        win._apply_theme(win.current_theme)
        win._apply_global_zoom()
        if hasattr(win, '_apply_language'):
            win._apply_language()
        win._on_working_dir_changed_for_test = None  # 占位，无副作用
        for name in ('explorer_panel', 'git_panel', 'remote_panel'):
            self.assertFalse(hasattr(win, name))

    def test_toggle_explorer_builds_and_wires_panel(self):
        win = self._new_window()
        win._toggle_explorer_panel()
        self.assertTrue(hasattr(win, 'explorer_panel'))
        panel = win.explorer_panel
        self.assertTrue(win.explorer_panel_container.isVisibleTo(win))
        # 信号接线：文件编辑请求 → 编辑器；收藏变化 → ★ 提示
        self.assertEqual(panel.receivers(panel.file_edit_requested), 1)
        self.assertEqual(panel.receivers(panel.favorites_changed), 1)
        self.assertEqual(panel.receivers(panel.save_file_requested), 1)
        # 面板在 splitter 里位于编辑器之前
        self.assertIs(win.explorer_splitter.widget(0), panel)
        # 再切一次不会重复建
        win._toggle_explorer_panel()
        win._toggle_explorer_panel()
        self.assertIs(win.explorer_panel, panel)

    def test_explorer_panel_follows_current_zoom(self):
        """懒建出来的面板要追平当前缩放（树字号 = 13 + delta）"""
        win = self._new_window()
        win._global_zoom_delta = 3
        win._apply_global_zoom()
        win._toggle_explorer_panel()
        tree = win.explorer_panel.tree_view
        self.assertEqual(tree.font().pointSize(), 16)

    def test_toggle_git_builds_panel_and_sets_repo(self):
        win = self._new_window()
        win._toggle_git_panel()
        self.assertTrue(hasattr(win, 'git_panel'))
        self.assertEqual(win.git_panel.receivers(win.git_panel.diff_requested), 1)
        self.assertTrue(win.git_panel_container.isVisibleTo(win))

    def test_toggle_remote_builds_panel(self):
        win = self._new_window()
        win._toggle_remote_panel()
        self.assertTrue(hasattr(win, 'remote_panel'))
        self.assertIs(win.remote_splitter.widget(0), win.remote_panel)
        self.assertEqual(
            win.remote_panel.receivers(win.remote_panel.open_terminal_at), 1)

    def test_ensure_is_idempotent(self):
        win = self._new_window()
        a = win._ensure_git_panel()
        b = win._ensure_git_panel()
        self.assertIs(a, b)



class TestDetachedWindowTracking(unittest.TestCase):
    """派生窗口销毁后必须从 detached_windows 摘除，否则 Python 对象永久滞留"""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw_mod = main_window

    def test_destroyed_window_is_forgotten(self):
        parent = self.mw_mod.MainWindow()
        child = self.mw_mod.MainWindow()
        try:
            parent._track_detached_window(child)
            self.assertIn(child, parent.detached_windows)
            child.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
            self.assertEqual(parent.detached_windows, [],
                             "子窗口销毁后仍留在 detached_windows 里")
        finally:
            QApplication.sendEvent(parent, QCloseEvent())
            parent.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
