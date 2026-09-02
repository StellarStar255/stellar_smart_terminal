# -*- coding: utf-8 -*-
"""Explorer 工具栏的「主页」按钮：一键回到窗口当前工作目录。

用户往上翻了几层看别的目录后，想回到顶部 Directory 里那个项目路径得逐级
点回去。现在 ↑ 旁边有个主页按钮，点一下根目录就切回窗口工作目录。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_home_button.py -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication


class TestExplorerHomeButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_panel_has_home_button_emitting_signal(self):
        from explorer_widget import ExplorerPanel
        p = ExplorerPanel()
        got = []
        p.home_requested.connect(lambda: got.append(1))
        self.assertTrue(hasattr(p, 'home_btn'), "Explorer 工具栏缺少主页按钮")
        self.assertFalse(p.home_btn.icon().isNull(), "主页按钮没有图标")
        p.home_btn.click()
        self.assertEqual(got, [1])
        p.deleteLater()

    def test_home_returns_to_window_cwd(self):
        import main_window
        win = main_window.MainWindow()
        try:
            proj = os.path.realpath(tempfile.mkdtemp(prefix="proj_"))
            other = os.path.realpath(tempfile.mkdtemp(prefix="other_"))
            win._window_cwd = proj
            panel = win._ensure_explorer_panel()
            panel.set_root_path(other)
            self.app.processEvents()
            self.assertEqual(os.path.realpath(panel._current_path), other)
            panel.home_btn.click()
            self.app.processEvents()
            self.assertEqual(os.path.realpath(panel._current_path), proj,
                             "主页按钮应把根目录切回窗口工作目录")
        finally:
            win._force_closing = True
            QApplication.sendEvent(win, QCloseEvent())
            win.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
