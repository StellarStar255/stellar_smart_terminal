# -*- coding: utf-8 -*-
"""整批退出（Dock Quit / Cmd+Q）只弹一次"确认退出"的回归测试。

以前 Qt 逐个给窗口发 closeEvent，每个有进程在跑的窗口都各弹一次确认框，
四个窗口要点四次 Yes。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_quit_confirm_once.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox


class _FakeBox:
    def __init__(self, reply):
        self._reply = reply

    def exec(self):
        return self._reply


class TestQuitConfirmOnce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window

    def _make_windows(self, n):
        wins = [self.mw.MainWindow() for _ in range(n)]
        for w in wins:
            w.show()
        self.app.processEvents()
        return wins

    def _close_all(self, wins, reply=QMessageBox.StandardButton.Yes):
        """模拟 Qt 的整批关闭：同一条调用链里逐个发 closeEvent，返回弹框次数。"""
        prompts = []

        def fake_box(self_, icon, title, text, buttons=None):
            prompts.append(self_)
            return _FakeBox(reply)

        with mock.patch.object(self.mw.TerminalWidget, 'is_running', return_value=True), \
             mock.patch.object(self.mw.MainWindow, '_make_styled_message_box', fake_box):
            for w in wins:
                QApplication.sendEvent(w, QCloseEvent())
        return prompts

    def _dispose(self, wins):
        for w in wins:
            if sip.isdeleted(w):
                continue
            if not w._closing_in_progress:
                w._force_closing = True
                QApplication.sendEvent(w, QCloseEvent())
            w.deleteLater()
        self.app.processEvents()

    def setUp(self):
        self.mw.MainWindow._quit_confirmed_at = 0.0

    def test_batch_close_prompts_once(self):
        wins = self._make_windows(3)
        try:
            prompts = self._close_all(wins)
            self.assertEqual(len(prompts), 1)
            for w in wins:
                self.assertTrue(w._closing_in_progress)
        finally:
            self._dispose(wins)

    def test_cancel_stops_the_batch_and_does_not_arm_reuse(self):
        wins = self._make_windows(2)
        try:
            prompts = self._close_all(wins, reply=QMessageBox.StandardButton.Cancel)
            # 每个窗口都各自问了（取消不会武装"复用确认"），且都没关
            self.assertEqual(len(prompts), 2)
            for w in wins:
                self.assertFalse(w._closing_in_progress)
        finally:
            self._dispose(wins)

    def test_stale_confirmation_prompts_again(self):
        wins = self._make_windows(1)
        try:
            import time
            self.mw.MainWindow._quit_confirmed_at = (
                time.monotonic() - self.mw.MainWindow._QUIT_CONFIRM_REUSE_SECS - 1)
            prompts = self._close_all(wins)
            self.assertEqual(len(prompts), 1)
        finally:
            self._dispose(wins)


if __name__ == '__main__':
    unittest.main()
