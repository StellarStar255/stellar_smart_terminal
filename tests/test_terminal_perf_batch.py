# -*- coding: utf-8 -*-
"""终端核心性能/生命周期批次的回归测试（2026-09 审查清单）。

1. 一次鼠标手势只拍一次历史快照：双击选词 / Cmd 悬停 URL 以前逐行
   `list(history.top)`，5000 行历史下一次双击就是几百次 O(N) 拷贝。
2. 异步 reflow 持锁期间，滚动条/滚轮走的 `_get_history_count` 不能在锁上
   阻塞——那正是异步化想避免的冻结。
3. 关闭长会话时的路径提取与落盘不在调用线程做。
4. 全历史搜索的逐格提取在 worker 线程跑，GUI 只收结果。
5. DSR（\\x1b[6n）必须按查询点之前已 feed 的内容作答。
6. shell 退出后重启不泄漏旧后端；cleanup 停掉 resize/idle 定时器。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_terminal_perf_batch.py -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class _CountingDeque(deque):
    """整体迭代（list()/islice）计数；len()/append 不计。"""
    iter_count = 0

    def __iter__(self):
        self.iter_count += 1
        return super().__iter__()


class _FakeBackend:
    def __init__(self, running=False):
        self.is_running = running
        self.stopped = False
        self.writes = []
        self.on_output = None
        self.on_exit = None

    def write(self, data: bytes) -> bool:
        self.writes.append(bytes(data))
        return True

    def stop(self):
        self.stopped = True
        self.is_running = False

    def start(self, command, cwd, cols, rows):
        return False   # 不真的 spawn；start_process 会把 _backend 置回 None

    def resize(self, cols, rows):
        pass


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from terminal_widget import TerminalWidget
        cls.TerminalWidget = TerminalWidget

    def _widget(self, lines=0, pattern="line {i:05d} /usr/local/bin/tool_{i} ok"):
        w = self.TerminalWidget()
        w.resize(800, 500)
        w._update_terminal_size()
        for i in range(lines):
            w.stream.feed(pattern.format(i=i) + "\r\n")
        return w

    def _pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)


class TestGestureSnapshot(_Base):
    def _install_counter(self, w):
        top = w.screen.history.top
        counting = _CountingDeque(top, maxlen=top.maxlen)
        w.screen.history = w.screen.history._replace(top=counting)
        return counting

    def test_double_click_word_select_copies_history_at_most_twice(self):
        w = self._widget(lines=3000)
        counting = self._install_counter(w)
        counting.iter_count = 0
        w._select_word_at((100, 20))
        self.assertLessEqual(
            counting.iter_count, 2,
            f"双击选词整拷了 {counting.iter_count} 次历史（应 ≤ 2）")
        self.assertIsNotNone(w._selection_start)
        w.deleteLater()

    def test_url_hover_copies_history_at_most_twice(self):
        w = self._widget(lines=2000, pattern="see https://example.com/p{i} now")
        counting = self._install_counter(w)
        counting.iter_count = 0
        w.scroll_offset = 50
        url = w._get_url_at_pos((0, 6))
        self.assertLessEqual(counting.iter_count, 2)
        self.assertTrue(url.startswith("https://example.com/"))
        w.deleteLater()


class TestHistoryCountNonBlocking(_Base):
    def test_count_returns_cached_value_while_lock_held(self):
        w = self._widget(lines=300)
        expected = w._get_history_count()
        self.assertGreater(expected, 0)

        acquired = threading.Event()
        release = threading.Event()

        def holder():
            with w._screen_lock:
                acquired.set()
                release.wait(3.0)

        th = threading.Thread(target=holder, daemon=True)
        th.start()
        self.assertTrue(acquired.wait(2.0))
        try:
            t0 = time.monotonic()
            got = w._get_history_count()
            w._sync_scrollbar()
            fractions = w._mark_fractions()
            dt = time.monotonic() - t0
        finally:
            release.set()
            th.join(3.0)
        self.assertLess(dt, 0.5, f"锁被别的线程持有时 GUI 侧阻塞了 {dt:.2f}s")
        self.assertEqual(got, expected)
        self.assertIsInstance(fractions, list)
        w.deleteLater()


class TestSessionEndOffThread(unittest.TestCase):
    def test_end_session_extracts_and_saves_off_calling_thread(self):
        import session_manager as sm_mod
        from session_manager import SessionManager
        sm = SessionManager()
        sm.sessions_dir = Path(tempfile.mkdtemp(prefix="sess_end_"))
        try:
            sm.create_session('claude')
            real = str(Path(__file__).resolve())
            entry = sm.add_output(f'see {real} for details')
            self.assertEqual(entry.files, [])
            threads = []
            real_fn = sm_mod.extract_file_paths

            def spy(text):
                threads.append(threading.current_thread().name)
                return real_fn(text)

            with mock.patch.object(sm_mod, 'extract_file_paths', side_effect=spy):
                session = sm.end_session()
                main = threading.current_thread().name
                self.assertNotIn(
                    main, threads,
                    "end_session 在调用线程上跑了 extract_file_paths")
                sm._wait_pending_save()
            self.assertTrue(threads, "后台没有做路径提取")
            self.assertIn(real, entry.files)
            saved = sm.sessions_dir / f"{session.session_id}.json"
            self.assertTrue(saved.exists())
            import json
            data = json.loads(saved.read_text(encoding='utf-8'))
            self.assertIn(real, data['entries'][0]['files'])
            self.assertTrue(data['end_time'])
        finally:
            sm._save_executor.shutdown(wait=True)


class TestSearchOffThread(_Base):
    def test_history_extraction_runs_on_worker(self):
        w = self._widget(lines=400, pattern="row {i:05d} needle here")
        w._show_search_bar()
        names = set()
        orig = w._extract_search_line

        def spy(line, *a, **k):
            names.add(threading.current_thread().name)
            return orig(line, *a, **k)

        w._extract_search_line = spy
        w._search_pending_text = "needle"
        w._perform_search()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not w._search_matches:
            self._pump(0.02)
        self.assertTrue(w._search_matches, "搜索没有产出结果")
        self.assertGreaterEqual(len(w._search_matches), 400)
        off_main = {n for n in names if n != threading.current_thread().name}
        self.assertTrue(off_main, "历史行提取全部在 GUI 线程上执行")
        w.cleanup()
        w.deleteLater()

    def test_stale_result_discarded(self):
        w = self._widget(lines=200, pattern="row {i:05d} needle here")
        w._show_search_bar()
        w._search_pending_text = "needle"
        w._perform_search()
        # 结果还没回来就改了查询：旧结果必须被丢弃
        w._search_pending_text = "zzz-not-there"
        w._perform_search()
        self._pump(1.0)
        self.assertEqual(w._search_matches, [])
        w.cleanup()
        w.deleteLater()


class TestDsrReply(_Base):
    def test_reply_reflects_prefix_position(self):
        w = self._widget()
        fb = _FakeBackend(running=True)
        w._backend = fb
        w._on_output(b"abc\x1b[6n")
        self.assertIn(b"\x1b[1;4R", fb.writes,
                      f"DSR 应答应基于已 feed 的前缀（列 4），实际 {fb.writes}")
        self.assertNotIn(b"\x1b[1;1R", fb.writes)
        # 查询后面的文本照常上屏
        w._on_output(b"\r\nxy\x1b[6nz")
        self.assertIn(b"\x1b[2;3R", fb.writes)
        w._backend = None
        w.deleteLater()


class TestRestartAndCleanup(_Base):
    def test_restart_stops_exited_backend(self):
        import terminal_widget as tw
        w = self._widget()
        old = _FakeBackend(running=False)
        w._backend = old
        new = _FakeBackend(running=False)
        with mock.patch.object(tw, 'create_backend', return_value=new):
            w.start_process(['sh'])
        self.assertTrue(old.stopped, "重启前没有 stop() 已退出的旧后端（fd 泄漏）")
        w.deleteLater()

    def test_cleanup_stops_resize_and_idle_timers(self):
        w = self._widget()
        w._resize_timer.start(5000)
        w._activity_idle_timer.start(5000)
        w.cleanup()
        self.assertFalse(w._resize_timer.isActive())
        self.assertFalse(w._activity_idle_timer.isActive())
        w.deleteLater()

    def test_dead_execute_command_removed(self):
        self.assertFalse(hasattr(self.TerminalWidget, '_execute_command'),
                         "_execute_command（发 \\n，无调用者）应已删除")


if __name__ == '__main__':
    unittest.main()
