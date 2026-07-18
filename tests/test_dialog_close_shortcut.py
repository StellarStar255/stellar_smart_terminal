"""Cmd+W / Ctrl+W 关闭弹窗的全局过滤器测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_dialog_close_shortcut.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import (QApplication, QDialog, QLineEdit, QMainWindow,
                             QVBoxLayout)
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from app import DialogCloseShortcutFilter


class TestDialogCloseShortcut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.filter = DialogCloseShortcutFilter(cls.app)
        cls.app.installEventFilter(cls.filter)

    @classmethod
    def tearDownClass(cls):
        cls.app.removeEventFilter(cls.filter)

    def test_ctrl_w_closes_dialog(self):
        dlg = QDialog()
        dlg.show()
        self.app.processEvents()
        self.assertTrue(dlg.isVisible())
        QTest.keyClick(dlg, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertFalse(dlg.isVisible())
        dlg.deleteLater()

    def test_ctrl_w_from_child_widget_closes_dialog(self):
        # 焦点在对话框内的输入框上：事件收件人是子控件，仍应关掉整个对话框
        dlg = QDialog()
        lay = QVBoxLayout(dlg)
        edit = QLineEdit()
        lay.addWidget(edit)
        dlg.show()
        edit.setFocus()
        self.app.processEvents()
        QTest.keyClick(edit, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertFalse(dlg.isVisible())
        dlg.deleteLater()

    def test_main_window_not_closed(self):
        # 主窗口（非 QDialog）按 Ctrl+W 不被过滤器关闭——留给「关标签页」快捷键
        win = QMainWindow()
        win.show()
        self.app.processEvents()
        QTest.keyClick(win, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertTrue(win.isVisible())
        win.close()
        win.deleteLater()

    def test_plain_w_does_not_close(self):
        dlg = QDialog()
        dlg.show()
        self.app.processEvents()
        QTest.keyClick(dlg, Qt.Key.Key_W)  # 无修饰键
        self.app.processEvents()
        self.assertTrue(dlg.isVisible())
        dlg.close()
        dlg.deleteLater()

    def test_ctrl_shift_w_does_not_close(self):
        # 修饰键必须精确等于 Ctrl（Cmd）：带 Shift 不触发
        dlg = QDialog()
        dlg.show()
        self.app.processEvents()
        QTest.keyClick(dlg, Qt.Key.Key_W,
                       Qt.KeyboardModifier.ControlModifier
                       | Qt.KeyboardModifier.ShiftModifier)
        self.app.processEvents()
        self.assertTrue(dlg.isVisible())
        dlg.close()
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
