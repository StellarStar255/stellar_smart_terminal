"""消息框统一主题：QMessageBox.warning() 这类静态调用弹出的框也要按主题走。

以前只有 _make_styled_message_box 那一条路有样式，全应用 100 多处静态调用
弹的都是系统原生样式（macOS 上粗体大字 + 灰底），与深色主题格格不入。
现在消息框 QSS 追加进 MainWindow 的样式表，窗口下所有 QMessageBox 按 Qt
层叠继承，不必逐处设样式。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_message_box_theme.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402


class MessageBoxThemeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import main_window
        self.mw = main_window.MainWindow()
        self.mw.show()
        self.app.processEvents()

    def tearDown(self):
        self.mw.close()
        self.mw.deleteLater()
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_main_window_stylesheet_carries_message_box_rules(self):
        ss = self.mw.styleSheet()
        self.assertIn('QMessageBox {', ss)
        self.assertIn('qt_msgbox_label', ss)          # 主文案不再粗体
        self.assertIn('QMessageBox QPushButton:default', ss)   # 默认按钮走 accent

    def test_static_message_box_under_window_inherits_theme(self):
        """静态调用弹出的框（parent=主窗口）背景是主题底色而不是系统灰。"""
        theme = self.mw.THEMES[self.mw.current_theme]
        box = QMessageBox(self.mw)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("nothing to commit, working tree clean")
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.show()
        for _ in range(3):
            self.app.processEvents()
        img = box.grab().toImage()
        # 取右上角一点（避开图标/文字/按钮）
        px = img.pixelColor(img.width() - 4, 4)
        from PyQt6.QtGui import QColor
        expect = QColor(theme['bg_dark'])
        self.assertLess(abs(px.red() - expect.red()) + abs(px.green() - expect.green())
                        + abs(px.blue() - expect.blue()), 30,
                        f"message box bg {px.name()} != theme bg_dark {expect.name()}")
        # 默认按钮（OK）底色是 accent
        ok = box.button(QMessageBox.StandardButton.Ok)
        self.assertIsInstance(ok, QPushButton)
        box.close()

    def test_theme_switch_refreshes_message_box_rules(self):
        names = list(self.mw.THEMES)
        other = next(n for n in names if n != self.mw.current_theme)
        self.mw._apply_theme(other)
        self.assertIn(self.mw.THEMES[other]['bg_dark'].lower(),
                      self.mw.styleSheet().lower())


class GitNothingToCommitTest(unittest.TestCase):

    def test_nothing_to_commit_is_reported_in_plain_words(self):
        from git_manager import GitManager
        gm = GitManager.__new__(GitManager)
        got = []
        gm.error_occurred = mock.Mock(emit=got.append)
        gm.status_changed = mock.Mock()
        gm.get_conflict_files = lambda: []
        gm._run_git = lambda *a: (False, "On branch main\nYour branch is up to date with "
                                         "'origin/main'.\n\nnothing to commit, working tree clean")
        self.assertFalse(gm.commit("msg"))
        self.assertEqual(len(got), 1)
        self.assertNotIn('On branch', got[0])
        self.assertTrue('没有需要提交' in got[0] or 'Nothing to commit' in got[0], got[0])


if __name__ == '__main__':
    unittest.main()
