# -*- coding: utf-8 -*-
"""回归（2026-09 审查 B）：「确认退出只弹一次」必须只对整批退出生效。

以前 _quit_confirmed_at 是进程级时间戳：任一窗口确认后 2 秒内，用户手动
去关另一个有进程在跑的窗口，也被当成"已确认"直接杀掉——没有任何提示。
现在只有 Cmd+Q / Dock Quit（QEvent.Quit → closeAllWindows）这一条同步
调用链里的窗口才复用确认。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_quit_batch_vs_manual_2026_09_win.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from _qt_quit_reset_2026_09_win import dispose_windows


class _FakeBox:
    def __init__(self, reply):
        self._reply = reply

    def exec(self):
        return self._reply


class TestQuitBatchVsManual(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window

    def setUp(self):
        self.mw.MainWindow._quit_confirmed_at = 0.0
        self.prompts = []

    def _fake_box(self, reply):
        prompts = self.prompts

        def fake_box(self_, icon, title, text, buttons=None):
            prompts.append(self_)
            return _FakeBox(reply)
        return fake_box

    def _make_windows(self, n):
        wins = [self.mw.MainWindow() for _ in range(n)]
        for w in wins:
            w.show()
        self.app.processEvents()
        return wins

    def test_manual_close_right_after_confirm_prompts_again(self):
        a, b = self._make_windows(2)
        try:
            with mock.patch.object(self.mw.TerminalWidget, 'is_running', return_value=True), \
                 mock.patch.object(self.mw.MainWindow, '_make_styled_message_box',
                                   self._fake_box(QMessageBox.StandardButton.Yes)):
                QApplication.sendEvent(a, QCloseEvent())
                self.assertEqual(len(self.prompts), 1)
                self.assertTrue(a._closing_in_progress)
                # 1 秒内手动关 B：不是整批退出，必须再问一次
                QApplication.sendEvent(b, QCloseEvent())
                self.assertEqual(len(self.prompts), 2)
                self.assertTrue(b._closing_in_progress)
        finally:
            dispose_windows(self.app, [a, b])

    def test_quit_event_batch_prompts_once(self):
        wins = self._make_windows(3)
        try:
            with mock.patch.object(self.mw.TerminalWidget, 'is_running', return_value=True), \
                 mock.patch.object(self.mw.MainWindow, '_make_styled_message_box',
                                   self._fake_box(QMessageBox.StandardButton.Yes)):
                # Cmd+Q / Dock Quit：Qt 给 app 发 Quit，QApplication::event 里
                # 同步 closeAllWindows()
                QApplication.sendEvent(self.app, QEvent(QEvent.Type.Quit))
            self.assertEqual(len(self.prompts), 1)
            for w in wins:
                self.assertTrue(w._closing_in_progress)
            # 整批结束后标志必须已清
            self.assertFalse(self.mw.MainWindow._batch_closing)
        finally:
            dispose_windows(self.app, wins)


if __name__ == '__main__':
    unittest.main()
