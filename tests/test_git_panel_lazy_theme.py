# -*- coding: utf-8 -*-
"""懒建的 Git 面板必须带着图标出生。

面板改成首次打开才构建后，主窗口只在换主题时才调 apply_theme；头部三个
工具按钮（刷新 / 贮藏 / 设置）的图标只在 apply_theme 里画 → 用户看到三个
空方块（"按键全看不到了"）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_panel_lazy_theme.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class TestGitPanelLazyTheme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import app_config
        # 面板默认隐藏，才走懒建路径
        app_config.update_config({'git_panel_visible': False}, description='test')
        import main_window
        cls.win = main_window.MainWindow()
        cls.win.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtGui import QCloseEvent
        cls.win._force_closing = True
        QApplication.sendEvent(cls.win, QCloseEvent())
        cls.win.deleteLater()
        cls.app.processEvents()
        del cls.win

    def test_header_buttons_have_icons_right_after_lazy_build(self):
        from git_widget import GitHeaderWidget
        self.assertIsNone(getattr(self.win, 'git_panel', None))
        self.win._toggle_git_panel()
        self.app.processEvents()
        header = self.win.git_panel.findChild(GitHeaderWidget)
        self.assertIsNotNone(header)
        for name in ('refresh_btn', 'stash_btn', 'settings_btn'):
            self.assertFalse(getattr(header, name).icon().isNull(), f"{name} has no icon")

    def test_standalone_header_has_icons(self):
        from git_widget import GitHeaderWidget
        import themes
        h = GitHeaderWidget(themes.THEMES["午夜黑"])
        for name in ('refresh_btn', 'stash_btn', 'settings_btn'):
            self.assertFalse(getattr(h, name).icon().isNull(), f"{name} has no icon")
        h.deleteLater()


if __name__ == '__main__':
    unittest.main()
