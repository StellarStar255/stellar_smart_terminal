# -*- coding: utf-8 -*-
"""回归（2026-09 审查 A）：更新检查/下载线程不能挂在 WA_DeleteOnClose 的主窗口上。

以前 UpdateChecker(self) / UpdateDownloader(..., self) 以窗口为 Qt 父对象：
检查还没返回就关窗 → 窗口 deleteLater → 连带析构运行中的 QThread →
"QThread: Destroyed while thread is still running" → 进程 abort（退出码 134）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_update_thread_lifetime_2026_09_win.py -v
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

from _qt_quit_reset_2026_09_win import reset_quit_state


def _slow_run(self_):
    time.sleep(1.0)


class TestUpdateThreadLifetime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import app_updater
        import main_window
        cls.app_updater = app_updater
        cls.mw = main_window

    def _flush(self):
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_close_window_while_update_check_running(self):
        w = self.mw.MainWindow()
        w.show()
        self.app.processEvents()
        with mock.patch.object(self.app_updater.UpdateChecker, 'run', _slow_run):
            w._check_for_updates()
            checker = w._update_checker
            self.assertIsNotNone(checker)
            self.assertTrue(checker.isRunning())
            # 线程不能以窗口为父对象，否则窗口析构会连带析构运行中的线程
            self.assertIsNone(checker.parent())
            QApplication.sendEvent(w, QCloseEvent())
            w.deleteLater()      # 模拟 WA_DeleteOnClose 的延迟删除
            self._flush()        # bug 版在这里 abort（exit 134）
            self.assertTrue(sip.isdeleted(w))
            self.assertFalse(sip.isdeleted(checker))
            self.assertTrue(checker.isRunning())
            # 收尾：等线程自然退出，finished → deleteLater 自行回收
            self.assertTrue(checker.wait(5000))
            self._flush()
        reset_quit_state(self.app)

    def test_close_window_while_download_running_cancels_it(self):
        w = self.mw.MainWindow()
        w.show()
        self.app.processEvents()

        class _SlowDownloader(self.app_updater.UpdateDownloader):
            def run(self_):
                for _ in range(50):
                    if self_._cancelled:
                        return
                    time.sleep(0.05)

        trusted = (f"https://github.com/{self.app_updater.REPO}"
                   "/releases/download/v9.9.9/x.zip")
        with mock.patch.object(self.app_updater, 'UpdateDownloader', _SlowDownloader):
            w._start_update_download({'browser_download_url': trusted, 'size': 1})
            dl = w._update_downloader
            self.assertTrue(dl.isRunning())
            self.assertIsNone(dl.parent())
            QApplication.sendEvent(w, QCloseEvent())
            # 关窗即请求中止，不在 GUI 线程无限等
            self.assertTrue(dl._cancelled)
            w.deleteLater()
            self._flush()
            self.assertTrue(sip.isdeleted(w))
            # 线程收到取消后自己收工，finished → 池里 deleteLater 回收；
            # 收尾快的话此时 C++ 对象已经没了，只在还活着时才 wait
            if not sip.isdeleted(dl):
                self.assertTrue(dl.wait(5000))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not sip.isdeleted(dl):
                self._flush()
            self.assertTrue(sip.isdeleted(dl), "取消后的下载线程应被池回收")
        reset_quit_state(self.app)


if __name__ == '__main__':
    unittest.main()
