"""传输期间远程目录树必须还能看（不依赖真实 SSH）。

用户报告：粘一批文件的几分钟里，左侧目录树整个空白，什么都浏览不了。
根因是 _populate_tree_root 先把树清空、再提交 listdir —— 而 listdir 排在
会话那条单 worker 线程上，得等整批传完才轮得到它。旧内容应该留着，等
结果到了再一次性换掉。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_tree_during_transfer.py
"""
import os
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _SlowSession:
    """listdir 挂着不返回（模拟排在大传输后面）；其余调用即刻完成。"""

    def __init__(self, entries=()):
        self.entries = list(entries)
        self.pending: list = []      # 挂起的 (future, fn, args)
        from ssh_session import HostConfig
        self.host_config = HostConfig(alias="h", hostname="h")

    def submit(self, fn, *args):
        fut = Future()
        if fn.__name__ == "listdir":
            self.pending.append((fut, fn, args))     # 先不完成
        else:
            fut.set_result(None)
        return fut

    def listdir(self, path):
        return list(self.entries)

    def is_connected(self):
        return True

    def invalidate_cache(self, path):
        pass

    def abort(self):
        pass

    def settle(self):
        """把挂起的 listdir 一次性放行。"""
        for fut, fn, args in self.pending:
            fut.set_result(fn(*args))
        self.pending.clear()


def _entry(name):
    from ssh_session import RemoteEntry
    return RemoteEntry(name=name, path=f"/data/{name}", is_dir=False,
                       size=1, mtime=0.0)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app_config
        self._tmp_cfg = Path(tempfile.mkdtemp()) / "cfg.json"
        self._orig = app_config.get_config_path
        app_config.get_config_path = lambda: self._tmp_cfg

    def tearDown(self):
        import app_config
        app_config.get_config_path = self._orig

    def _panel(self, sess):
        from remote_explorer_widget import RemoteExplorerPanel
        p = RemoteExplorerPanel(theme={})
        p._session = sess
        p._current_path = "/data"
        return p


class TestTreeStaysReadableWhileBusy(_Base):
    def test_refresh_keeps_old_entries_until_the_new_listing_lands(self):
        sess = _SlowSession([_entry("a.jsonl"), _entry("b.jsonl")])
        panel = self._panel(sess)
        panel._populate_tree_root()
        sess.settle()
        panel._apply_top_level(sess.entries)      # 首次填充
        self.assertEqual(panel._tree.topLevelItemCount(), 2)

        # 传输把 worker 占住 → 这次刷新的 listdir 一直不返回
        panel._on_refresh()
        self.assertEqual(panel._tree.topLevelItemCount(), 2,
                         "listdir 还没回来就把树清空 = 传输期间什么都看不了")

        sess.entries.append(_entry("c.jsonl"))
        sess.settle()
        panel._apply_top_level(sess.entries)
        self.assertEqual(panel._tree.topLevelItemCount(), 3)

    def test_disconnected_panel_still_clears(self):
        sess = _SlowSession([_entry("a.jsonl")])
        panel = self._panel(sess)
        panel._populate_tree_root()
        sess.settle()
        panel._apply_top_level(sess.entries)
        panel._session = None
        panel._populate_tree_root()
        self.assertEqual(panel._tree.topLevelItemCount(), 0,
                         "断开时该清空，不能留着假内容")

    def test_stale_listing_does_not_overwrite_a_newer_one(self):
        """慢的那次结果后到时必须作废，否则会盖掉新目录的内容。"""
        sess = _SlowSession([_entry("a.jsonl")])
        panel = self._panel(sess)
        seen = []
        panel._top_level_ready.connect(lambda e: seen.append(len(e)))

        panel._populate_tree_root()          # 第 1 次（旧目录）
        first = list(sess.pending)
        sess.pending.clear()
        panel._current_path = "/other"
        panel._populate_tree_root()          # 第 2 次（新目录）
        second = list(sess.pending)
        sess.pending.clear()

        for fut, fn, args in second:         # 新的先回来
            fut.set_result([_entry("new.jsonl")])
        for fut, fn, args in first:          # 旧的后到 → 应被丢弃
            fut.set_result([_entry("x.jsonl"), _entry("y.jsonl")])
        self.assertEqual(seen, [1], "过期的 listdir 结果不该再进 UI")


class TestAutoRefreshPausedDuringTransfer(_Base):
    def test_polling_is_skipped_while_a_batch_is_running(self):
        sess = _SlowSession([_entry("a.jsonl")])
        panel = self._panel(sess)
        panel.show()                          # tick 里要求 panel 可见
        panel._collect_auto_refresh_paths = lambda: ["/data"]
        calls = []
        panel._submit_auto_refresh = lambda s, p: calls.append(p)

        panel._auto_refresh_tick()
        self.assertEqual(calls, ["/data"])    # 平时照常轮询

        calls.clear()
        panel._begin_transfer_job(["a", "b"], header="h")
        panel._auto_refresh_tick()
        self.assertEqual(calls, [],
                         "传输期间轮询只会排在传输后面，还会反复重排目录")


if __name__ == "__main__":
    unittest.main()
