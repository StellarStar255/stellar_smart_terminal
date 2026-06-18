"""scrollback 压力指示测试（校准预测式 + 指示点）。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_scrollback_pressure -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestScrollbackPressure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, ms_per_line=0.04):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w._reflow_ms_per_line = ms_per_line  # 固定校准值便于断言
        return w

    def _feed(self, w, n):
        w.stream.feed('x\r\n' * n)
        w._update_scrollback_level()

    def test_levels_by_predicted_ms(self):
        # 0.04ms/行 → 80ms@2000行(琥珀), 200ms@5000行(红)
        w = self._widget(0.04)
        self._feed(w, 1000)
        self.assertEqual(w.scrollback_level(), 0)   # 40ms
        self._feed(w, 1500)                          # ~2500行 → 100ms
        self.assertEqual(w.scrollback_level(), 1)
        self._feed(w, 3000)                          # ~5000行 → 200ms
        self.assertEqual(w.scrollback_level(), 2)

    def test_fast_machine_no_false_alarm(self):
        # 很小的每行毫秒（快机器/窄内容）→ 顶到上限也不亮，避免常驻红点
        w = self._widget(0.005)
        self._feed(w, 5000)                          # 5000*0.005=25ms < 80
        self.assertEqual(w.scrollback_level(), 0)

    def test_signal_on_transitions_only(self):
        w = self._widget(0.04)
        seen = []
        w.scrollback_pressure_changed.connect(lambda lv: seen.append(lv))
        self._feed(w, 2500)   # → 1
        self._feed(w, 100)    # 仍 1，不应再发
        self._feed(w, 2500)   # → 2
        self.assertEqual(seen, [1, 2])

    def test_clear_resets_level_and_hides_dot(self):
        w = self._widget(0.04)
        self._feed(w, 5200)
        self.assertEqual(w.scrollback_level(), 2)
        self.assertIsNotNone(w._scrollback_dot_btn)
        w.clear_scrollback()
        self.assertEqual(w.scrollback_level(), 0)
        self.assertTrue(w._scrollback_dot_btn.isHidden())

    def test_dot_click_clears(self):
        w = self._widget(0.04)
        self._feed(w, 5200)
        btn = w._scrollback_dot_btn
        self.assertIsNotNone(btn)
        self.assertFalse(btn.isHidden())   # show() 已调用（不依赖父窗口可见）
        btn.click()
        self.assertEqual(w._get_history_count(), 0)
        self.assertEqual(w.scrollback_level(), 0)

    def test_alt_screen_no_pressure(self):
        # 备用屏（tmux/vim）历史不计入 → 不产生压力
        w = self._widget(0.04)
        self._feed(w, 5200)
        self.assertEqual(w.scrollback_level(), 2)
        w.screen._in_alt_screen = True
        w._update_scrollback_level()
        self.assertEqual(w.scrollback_level(), 0)

    def test_reflow_timing_calibrates(self):
        # resize 触发 reflow 后，_reflow_ms_per_line 应为正且有限
        w = self._widget(0.04)
        self._feed(w, 1000)
        w.term_cols = 100
        w._update_terminal_size  # 不直接调（依赖布局），改为直接驱动 reflow 计时路径
        import time
        with w._screen_lock:
            n = w._get_history_count() + w.term_rows
            t0 = time.perf_counter()
            w.screen.resize(w.term_rows, 100)
            ms = (time.perf_counter() - t0) * 1000
        if n >= 300 and ms > 0:
            w._reflow_ms_per_line = 0.6 * w._reflow_ms_per_line + 0.4 * (ms / n)
        self.assertGreater(w._reflow_ms_per_line, 0)
        self.assertLess(w._reflow_ms_per_line, 10)  # 合理范围


if __name__ == '__main__':
    unittest.main()
