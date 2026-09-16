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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from _qt_quit_reset_2026_09_win import reset_quit_state


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
        """模拟 Qt 的整批关闭（Cmd+Q / Dock Quit）：给 app 发 QEvent.Quit，
        QApplication::event 在同一条调用链里 closeAllWindows() 逐个发
        closeEvent（已用探针证实 offscreen 下也如此）。返回弹框次数。"""
        prompts = []

        def fake_box(self_, icon, title, text, buttons=None):
            prompts.append(self_)
            return _FakeBox(reply)

        with mock.patch.object(self.mw.TerminalWidget, 'is_running', return_value=True), \
             mock.patch.object(self.mw.MainWindow, '_make_styled_message_box', fake_box):
            QApplication.sendEvent(self.app, QEvent(QEvent.Type.Quit))
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
        # Quit 事件没有事件循环在跑时会把 Qt 的 quitNow 粘住（之后任何
        # exec() 立刻返回 -1），跑一个立刻退出的 exec() 复位
        reset_quit_state(self.app)

    def setUp(self):
        self.mw.MainWindow._quit_confirmed_at = 0.0
        self.mw.MainWindow._batch_closing = False

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
            # 第一个窗口点取消：Qt 的 closeAllWindows 遇到拒绝就停，后面的
            # 窗口不再被问；两个窗口都没关，也没有武装"复用确认"
            self.assertEqual(len(prompts), 1)
            for w in wins:
                self.assertFalse(w._closing_in_progress)
            self.assertEqual(self.mw.MainWindow._quit_confirmed_at, 0.0)
            self.assertFalse(self.mw.MainWindow._batch_closing)
            # 再来一次整批退出：必须重新问（取消没有留下可复用的确认）
            prompts = self._close_all(wins, reply=QMessageBox.StandardButton.Yes)
            self.assertEqual(len(prompts), 1)
            for w in wins:
                self.assertTrue(w._closing_in_progress)
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
