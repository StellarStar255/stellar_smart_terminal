# -*- coding: utf-8 -*-
"""本地 / 远程 explorer 共用同一份传输等待逻辑的回归测试。

审查发现 `_wait_future_with_progress` 在两个面板里已实质分叉：远程版有
sizes / live / on_bytes 做字节级进度、速率与 ETA，本地版只按 future 个数
计数——远程→本地粘贴一个大文件时进度条一直 0/1，远程面板内同样操作却有
MB/ETA。`_download_remote_recursive` 也是两份几乎逐字相同的拷贝。

修复：两者都搬进 explorer_common.TransferJobHost；本地复制传 sizes 和
字节回调，单个大文件也有字节级进度。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_transfer_progress_shared.py -v
"""
import os
import sys
import tempfile
import unittest
from concurrent.futures import Future

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication


class TestSharedImplementation(unittest.TestCase):
    def test_both_panels_use_explorer_common_wait(self):
        import explorer_common
        import explorer_widget
        import remote_explorer_widget
        shared = explorer_common.TransferJobHost.__dict__.get("_wait_future_with_progress")
        self.assertIsNotNone(shared, "TransferJobHost 缺少 _wait_future_with_progress")
        self.assertIs(explorer_widget.ExplorerPanel._wait_future_with_progress, shared)
        self.assertIs(remote_explorer_widget.RemoteExplorerPanel._wait_future_with_progress,
                      shared)

    def test_both_panels_share_download_remote_recursive(self):
        import explorer_common
        import explorer_widget
        import remote_explorer_widget
        shared = explorer_common.TransferJobHost.__dict__.get("_download_remote_recursive")
        self.assertIsNotNone(shared)
        self.assertIs(explorer_widget.ExplorerPanel._download_remote_recursive, shared)
        self.assertIs(remote_explorer_widget.RemoteExplorerPanel._download_remote_recursive,
                      shared)


class TestLocalCopyReportsBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_copy_local_entry_reports_increasing_bytes(self):
        from explorer_widget import copy_local_entry, local_entry_size
        tmp = tempfile.mkdtemp(prefix="cp_")
        src = os.path.join(tmp, "big.bin")
        with open(src, "wb") as fh:
            fh.write(os.urandom(3 * 1024 * 1024))
        dst = os.path.join(tmp, "copy.bin")
        seen = []
        copy_local_entry(src, dst, False, on_bytes=seen.append)
        self.assertTrue(seen, "复制过程没有任何字节回调")
        self.assertEqual(seen, sorted(seen), "字节回调必须单调递增")
        self.assertEqual(seen[-1], os.path.getsize(src))
        self.assertEqual(local_entry_size(src), os.path.getsize(src))
        with open(src, "rb") as a, open(dst, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_directory_size_and_copy_report_all_files(self):
        from explorer_widget import copy_local_entry, local_entry_size
        tmp = tempfile.mkdtemp(prefix="cpd_")
        src = os.path.join(tmp, "dir")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "a"), "wb") as fh:
            fh.write(b"x" * 1000)
        with open(os.path.join(src, "sub", "b"), "wb") as fh:
            fh.write(b"y" * 2000)
        self.assertEqual(local_entry_size(src), 3000)
        seen = []
        copy_local_entry(src, os.path.join(tmp, "out"), False, on_bytes=seen.append)
        self.assertEqual(seen[-1], 3000)

    def test_wait_passes_byte_progress_to_stage(self):
        """本地面板等待单个大文件时，统一窗口应收到递增的比例，而非只在结束时跳满"""
        import explorer_widget
        panel = explorer_widget.ExplorerPanel()
        try:
            fracs = []

            class _Job(QObject):   # 须是 sip 对象：等待循环里会 sip.isdeleted(job)
                def set_stage(self, *_):
                    pass

                def set_stage_progress(self, frac, detail=""):
                    fracs.append(frac)

                def was_canceled(self):
                    return False

                def was_force_canceled(self):
                    return False

            job = _Job()
            panel._active_transfer_job = lambda: job
            panel._register_job_abort = lambda *a, **k: None
            fut = Future()
            live = {"bytes": 0}

            # 模拟 worker：分 4 步推进字节，每步让事件循环跑一会儿
            from PyQt6.QtCore import QTimer
            steps = iter([250, 500, 750, 1000])

            def advance():
                try:
                    live["bytes"] = next(steps)
                except StopIteration:
                    fut.set_result(None)
                    return
                QTimer.singleShot(120, advance)
            QTimer.singleShot(120, advance)

            panel._wait_future_with_progress([fut], "copy", sizes=[1000], live=live)
            self.assertGreaterEqual(len([f for f in fracs if 0 < f < 1]), 2,
                                    f"进度应逐步推进而不是只在结束时跳满: {fracs}")
            self.assertEqual(fracs[-1], 1.0)
        finally:
            panel.deleteLater()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
