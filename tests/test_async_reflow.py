"""大历史 reflow 移出 GUI 线程的回归测试

背景：列宽变化时 screen.resize 对全部历史做 O(cell) reflow，满 5000 行
真实内容实测 100~200ms，以前在 GUI 线程同步执行 = 明显冻结。修复：
预测耗时 ≥ 阈值时把 reflow 提交到共享后台线程（feed 同款 _screen_lock
互斥），期间 paintEvent 沿用旧缓存贴图不阻塞；完成后经信号回 GUI 线程
收尾。小缓冲（预测低于阈值）保持原同步路径，语义不变。

STELLAR_SYNC_REFLOW=1 可整体退回同步（排障用）。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AsyncReflowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, hist_lines=300):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.resize(900, 500)
        w._update_terminal_size()
        for i in range(hist_lines + w.term_rows):
            w.stream.feed(f"line {i:05d} content for reflow test\r\n")
        return w

    def _wait_reflow_done(self, w, timeout=10.0):
        """异步等待收敛（Windows CI 教训：必须 sleep + 截止时间）"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if (not w._reflow_inflight
                    and (w.screen.lines, w.screen.columns)
                    == (w.term_rows, w.term_cols)):
                return
            time.sleep(0.01)
        self.fail("async reflow did not converge in time")


class TestAsyncReflow(AsyncReflowBase):
    def test_large_history_goes_async(self):
        """预测耗时超阈值：_update_terminal_size 立即返回，reflow 在后台"""
        w = self._widget()
        # 抬高每行耗时估计，确保预测值远超阈值（不依赖机器快慢）
        w._reflow_ms_per_line = 1.0
        old_cols = w.screen.columns

        w.resize(700, 500)
        w._update_terminal_size()

        # 调用返回时 reflow 尚未（必然）完成——已转入后台
        self.assertTrue(w._reflow_inflight or w.screen.columns != old_cols)
        self._wait_reflow_done(w)
        self.assertEqual(w.screen.columns, w.term_cols)
        self.assertLess(w.term_cols, old_cols)
        self.assertFalse(w._reflow_scaling)
        w.deleteLater()

    def test_async_side_effects_match_sync(self):
        """异步完成后与同步路径同款收尾：epoch/搜索缓存/scroll clamp"""
        w = self._widget()
        w._reflow_ms_per_line = 1.0
        epoch = w._render_epoch
        w._search_line_cache['sentinel'] = ('x', 'y', 'z')
        w.scroll_offset = 10 ** 9

        w.resize(600, 400)
        w._update_terminal_size()
        self._wait_reflow_done(w)

        self.assertGreater(w._render_epoch, epoch)
        self.assertEqual(len(w._search_line_cache), 0)
        self.assertLessEqual(w.scroll_offset, len(w.screen.history.top))
        w.deleteLater()

    def test_resize_during_inflight_converges(self):
        """在途期间再次 resize：worker 续跑，最终收敛到最后一次目标"""
        w = self._widget()
        w._reflow_ms_per_line = 1.0
        w.resize(700, 500)
        w._update_terminal_size()
        # 立刻再改目标（无论上一次是否已完成都应收敛到新目标）
        w.resize(850, 500)
        w._update_terminal_size()
        self._wait_reflow_done(w)
        self.assertEqual((w.screen.lines, w.screen.columns),
                         (w.term_rows, w.term_cols))
        w.deleteLater()

    def test_small_buffer_stays_sync(self):
        """小缓冲：预测耗时低于阈值，保持同步语义（调用返回即已收敛）"""
        w = self._widget(hist_lines=10)
        w._reflow_ms_per_line = 0.001
        w.resize(700, 500)
        w._update_terminal_size()
        self.assertFalse(w._reflow_inflight)
        self.assertEqual((w.screen.lines, w.screen.columns),
                         (w.term_rows, w.term_cols))
        w.deleteLater()

    def test_paint_uses_scaled_cache_while_inflight(self):
        """reflow 在途时 paintEvent 不重建缓存（不会卡在 _screen_lock 上）"""
        w = self._widget(hist_lines=50)
        w.flush_resize()
        w.show()  # offscreen 平台需 show 才派发 paintEvent
        self.app.processEvents()
        with w._screen_lock:
            w._rebuild_cache()  # 先有一份可贴图的缓存
        self.assertIsNotNone(w._cache_pixmap)

        rebuilds = []
        orig = w._rebuild_cache
        w._rebuild_cache = lambda: (rebuilds.append(1), orig())[1]
        w._reflow_scaling = True
        w._cache_valid = False  # 置脏：没有 gate 的话 paintEvent 必然重建
        try:
            w.repaint()
            self.assertEqual(len(rebuilds), 0)
        finally:
            w._reflow_scaling = False
            w._rebuild_cache = orig
        w.deleteLater()

    def test_alt_screen_resize_goes_async(self):
        """备用屏幕（tmux/vim）期间 resize 也走异步。

        陷阱：_get_history_count() 在备用屏幕返回 0（那是给滚动条/压力点
        用的语义），但 resize 在备用屏幕仍会全量 reflow 被保存的主屏+历史
        （恰是 O(历史) 的贵路径）。预测行数必须用真实历史长度，否则 tmux
        里 resize 永远漏回同步路径照样冻结。
        """
        w = self._widget()
        w.stream.feed("\x1b[?1049h")  # 进入备用屏幕
        self.assertTrue(w.screen._in_alt_screen)
        self.assertEqual(w._get_history_count(), 0)  # 语义不变（滚动条用）
        w._reflow_ms_per_line = 1.0

        w.resize(700, 500)
        w._update_terminal_size()
        went_async = w._reflow_inflight
        self._wait_reflow_done(w)
        self.assertTrue(went_async)
        w.stream.feed("\x1b[?1049l")  # 退出备用屏幕
        self.assertFalse(w.screen._in_alt_screen)
        w.deleteLater()

    def test_env_forces_sync(self):
        """STELLAR_SYNC_REFLOW=1（映射为类开关）时永远同步"""
        from terminal_widget import TerminalWidget
        w = self._widget()
        old_flag = TerminalWidget._ASYNC_REFLOW_ENABLED
        TerminalWidget._ASYNC_REFLOW_ENABLED = False
        try:
            w._reflow_ms_per_line = 1.0
            w.resize(700, 500)
            w._update_terminal_size()
            self.assertFalse(w._reflow_inflight)
            self.assertEqual(w.screen.columns, w.term_cols)
        finally:
            TerminalWidget._ASYNC_REFLOW_ENABLED = old_flag
        w.deleteLater()


if __name__ == '__main__':
    unittest.main()
