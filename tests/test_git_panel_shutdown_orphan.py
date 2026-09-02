# -*- coding: utf-8 -*-
"""GitPanel.shutdown() 等不到的后台线程不能随面板一起销毁。

全量测试偶发 "Fatal Python error: Aborted"，栈里是 fetch/status worker 仍在
subprocess.communicate() 里：shutdown() 先 cancel_running 再 wait(3s)、
terminate、wait(1s)，超时后就放弃；worker 的 parent 是面板，面板 deleteLater
时 Qt 连带析构一个仍在 running 的 QThread → abort 整个进程。

修复：shutdown() 结束时仍在跑的 worker 脱离父对象，由模块级集合持有到它
自己结束，绝不随面板销毁。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_panel_shutdown_orphan.py -v
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication


class _StubbornWorker(QThread):
    """terminate 对阻塞在 Python 里的线程无效，模拟卡在 communicate() 的 worker"""
    def __init__(self, parent, release: threading.Event):
        super().__init__(parent)
        self._release = release

    def run(self):
        self._release.wait(30)


class TestShutdownOrphansRunningWorkers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_running_worker_is_detached_from_panel(self):
        import git_widget
        panel = git_widget.GitPanel()
        release = threading.Event()
        try:
            panel._fetch_timer.stop()
            worker = _StubbornWorker(panel, release)
            panel._register_worker(worker)
            worker.start()
            self.assertTrue(worker.isRunning())

            # 把等待时间压短，别让测试真等 4 秒
            git_widget.GitPanel._SHUTDOWN_WAIT_MS = 50
            panel.shutdown()

            self.assertTrue(worker.isRunning(), "桩线程应仍在运行（terminate 对它无效）")
            self.assertIsNone(worker.parent(),
                              "仍在运行的 worker 必须脱离面板，否则面板析构时进程 abort")
            self.assertIn(worker, git_widget._ORPHANED_WORKERS)
        finally:
            release.set()
            worker.wait(5000)
            self.app.processEvents()
            panel.deleteLater()
            self.app.processEvents()
        self.assertNotIn(worker, git_widget._ORPHANED_WORKERS, "线程结束后应从孤儿集合移除")


if __name__ == '__main__':
    unittest.main()
