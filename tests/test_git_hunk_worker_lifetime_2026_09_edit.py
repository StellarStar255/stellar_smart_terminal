"""hunk worker 的 done 槽必须连绑定方法：视图销毁后回调不能再碰已删的 C++ 对象。

审查发现 GitDiffView._apply_hunk 用 lambda 连 done 信号，注释却写着
"绑定方法自动断开"。lambda 不是接收者的方法，Qt 不会随视图销毁把它断开，
apply_patch 慢一点、用户先关了 diff 视图 → 回调访问已删控件 → RuntimeError
（PyQt 默认 excepthook 下直接 qFatal 整个程序）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_hunk_worker_lifetime_2026_09_edit.py -v
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class _SlowGM:
    """apply_patch 卡在 Event 上，直到测试放行；get_diff 返回非空让回调走到 set_diff。"""

    def __init__(self):
        self.gate = threading.Event()

    def apply_patch(self, patch, cached=False, reverse=False):
        self.gate.wait(10)
        return True

    def get_diff(self, path, staged):
        return 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n'


class TestHunkWorkerOutlivesView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_done_after_view_destroyed_raises_nothing(self):
        from PyQt6 import sip
        from PyQt6.QtCore import QEvent
        from git_widget import GitDiffView

        gm = _SlowGM()
        view = GitDiffView()
        view.set_context(gm, 'a.txt', False)
        view._file_header = ['diff --git a/a.txt b/a.txt', '--- a/a.txt', '+++ b/a.txt']
        view._hunk_patches = ['@@ -1 +1 @@\n-x\n+y']
        view._apply_hunk(0)
        self.assertEqual(len(view._hunk_workers), 1)
        worker = next(iter(view._hunk_workers))
        self.assertTrue(worker.isRunning())

        hooked = []
        old_hook = sys.excepthook
        sys.excepthook = lambda *a: hooked.append(a)
        try:
            # 视图先于 worker 销毁
            view.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
            self.assertTrue(sip.isdeleted(view))

            gm.gate.set()
            self.assertTrue(worker.wait(5000), "worker 没有结束")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        finally:
            sys.excepthook = old_hook
        self.assertEqual(hooked, [],
                         f"视图销毁后 done 回调抛了异常: {hooked}")


if __name__ == '__main__':
    unittest.main()
