"""字体缩放的 reflow 防抖回归测试

背景：_zoom_in/_zoom_out（及全局缩放对每个终端的应用）以前直接同步调
_update_terminal_size()——满历史时每按一次 Cmd+± 就是一次 100~200ms 的
全量 reflow 冻结，多分屏下串行放大。修复：字号立即生效（度量+重绘），
网格重算走已有的 150ms resize 防抖，连按只在停手后 reflow 一次。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestZoomDebounce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.resize(800, 500)
        w._update_terminal_size()
        for i in range(100):
            w.stream.feed(f"line {i} some content here\r\n")
        return w

    def test_zoom_defers_grid_resize(self):
        """缩放后：字体立即变，screen 网格不立即 reflow，而是挂起防抖"""
        w = self._widget()
        w.flush_resize()
        old_cols = w.screen.columns
        old_size = w.term_font.pointSize()

        # 连放大 3 级（12→15pt）：单级在 Windows 的整数像素度量下
        # char_width 可能不变（12pt 与 13pt 同宽），列数断言会平台相关；
        # 3 级放大在任何平台都足以撑大 char_width
        w._zoom_in()
        w._zoom_in()
        w._zoom_in()

        self.assertEqual(w.term_font.pointSize(), old_size + 3)
        # 网格重算被防抖挂起：screen 尚未按新字号 reflow
        self.assertEqual(w.screen.columns, old_cols)
        self.assertTrue(w._resize_pending)
        self.assertTrue(w._resize_timer.isActive())

        # 防抖 flush 后网格按新字号收敛：列数变少，且与度量公式一致
        w.flush_resize()
        self.assertFalse(w._resize_pending)
        self.assertLess(w.term_cols, old_cols)
        expected_cols = max(20, int((w.width() - w.PADDING * 2) / w.char_width))
        self.assertEqual(w.term_cols, expected_cols)
        w.deleteLater()

    def test_rapid_zoom_single_reflow(self):
        """连按 3 次缩放只触发一次网格重算"""
        w = self._widget()
        w.flush_resize()
        calls = []
        orig = w._update_terminal_size
        w._update_terminal_size = lambda: (calls.append(1), orig())[1]

        w._zoom_in()
        w._zoom_in()
        w._zoom_out()
        self.assertEqual(len(calls), 0)  # 全部挂起
        w.flush_resize()
        self.assertEqual(len(calls), 1)  # 停手后只重算一次
        w.deleteLater()

    def test_apply_font_size_api(self):
        """全局缩放走 apply_font_size：同字号幂等，不同字号防抖生效"""
        w = self._widget()
        w.flush_resize()
        size = w.term_font.pointSize()

        w.apply_font_size(size)  # 幂等：不挂起防抖
        self.assertFalse(w._resize_pending)

        w.apply_font_size(size + 2)
        self.assertEqual(w.term_font.pointSize(), size + 2)
        self.assertTrue(w._resize_pending)
        w.flush_resize()
        w.deleteLater()


if __name__ == '__main__':
    unittest.main()
