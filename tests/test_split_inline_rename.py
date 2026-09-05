# -*- coding: utf-8 -*-
"""分屏窗格就地改名（不弹对话框）+ 按项目记忆的常用名称。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_split_inline_rename.py -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('STELLAR_DATA_DIR', tempfile.mkdtemp(prefix='stellar-test-'))

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication


class TestInlineRename(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window
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

    def _terminal(self):
        idx = self.win.tab_widget.currentIndex()
        return self.win.tab_terminals[idx][0]

    def test_rename_is_inline_and_commits_on_enter(self):
        term = self._terminal()
        self.win._rename_split(term)          # 以前弹 QInputDialog；现在把手上出现输入框
        ed = term._inline_rename_editor
        self.assertIsNotNone(ed)
        self.assertTrue(term._header_bar.isVisible())
        ed.setText("people count")
        QTest.keyClick(ed, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertIsNone(term._inline_rename_editor)
        self.assertEqual(term.get_split_label(), "people count")

    def test_escape_cancels_and_hides_temporary_handle(self):
        term = self._terminal()
        term.set_split_label(None)
        self.assertEqual(term._header_h, 0)
        self.win._rename_split(term)
        ed = term._inline_rename_editor
        QTest.keyClick(ed, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertIsNone(term.get_split_label())
        self.assertEqual(term._header_h, 0)   # 临时亮出来的把手收回去

    def test_names_are_remembered_per_project(self):
        w = self.win
        w._window_cwd = "/tmp/project-a"
        w._remember_split_name("train")
        w._remember_split_name("eval")
        self.assertEqual(w._split_name_suggestions()[:2], ["eval", "train"])
        w._window_cwd = "/tmp/project-b"
        w._remember_split_name("serve")
        sugg_b = w._split_name_suggestions()
        self.assertEqual(sugg_b[0], "serve")
        # 别的项目的名称只作为后备排在后面
        self.assertGreater(sugg_b.index("train"), 0)
        w._window_cwd = "/tmp/project-a"
        self.assertEqual(w._split_name_suggestions()[0], "eval")

    def test_committed_name_is_recorded_for_project(self):
        w = self.win
        w._window_cwd = "/tmp/project-c"
        term = self._terminal()
        w._rename_split(term)
        ed = term._inline_rename_editor
        ed.setText("gpu watch")
        QTest.keyClick(ed, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(w._split_name_suggestions()[0], "gpu watch")


if __name__ == '__main__':
    unittest.main()
