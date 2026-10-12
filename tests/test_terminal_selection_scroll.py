"""拖动选择跨页滚动：连续事件可先于 paint 到达，终点必须跟随新视口。"""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication

from terminal_widget import TerminalWidget


class TestSelectionScroll(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = TerminalWidget()
        self.w.resize(640, 400)
        self.w._update_terminal_size()
        for i in range(250):
            self.w.stream.feed(f'row {i:04d} 中文 sample\r\n')
        self.w.scroll_offset = 80
        self.w._invalidate_render_cache()
        with self.w._screen_lock:
            self.w._rebuild_cache()
        self.start_view = self.w._rendered_display_start
        self.pos = QPoint(
            int(self.w.PADDING + 6 * self.w.char_width),
            int(self.w.PADDING + self.w._header_h + 10.5 * self.w.char_height))
        self.w.mousePressEvent(self.mouse(QEvent.Type.MouseButtonPress,
                                        Qt.MouseButton.LeftButton))
        self.anchor = self.w._selection_start
        self.saved_sensitivity = TerminalWidget._scroll_sensitivity_cache
        TerminalWidget._scroll_sensitivity_cache = 1.0

    def tearDown(self):
        TerminalWidget._scroll_sensitivity_cache = self.saved_sensitivity
        self.w.cleanup()
        self.w.deleteLater()

    def mouse(self, kind, buttons):
        return QMouseEvent(kind, QPointF(self.pos), Qt.MouseButton.LeftButton,
                           buttons, Qt.KeyboardModifier.NoModifier)

    def wheel(self, degrees):
        self.w.wheelEvent(QWheelEvent(
            QPointF(self.pos), QPointF(self.pos), QPoint(), QPoint(0, degrees),
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False))

    def test_wheel_extends_drag_before_any_repaint_and_release_keeps_endpoint(self):
        for step in range(1, 4):
            self.wheel(240)  # 每次上滚 3 行，不处理 Qt paint 事件
            self.assertEqual(self.w._rendered_display_start, self.start_view)
            self.assertEqual(self.w._selection_start, self.anchor)
            self.assertEqual(self.w._selection_end,
                             (self.anchor[0] - 3 * step, self.anchor[1]))
        expected = self.w._selection_end
        self.w.mouseReleaseEvent(self.mouse(QEvent.Type.MouseButtonRelease,
                                          Qt.MouseButton.NoButton))
        self.assertEqual(self.w._selection_end, expected)
        selected = self.w._get_selected_text()
        self.wheel(-240)
        self.assertEqual(self.w._selection_end, expected)
        self.assertEqual(self.w._get_selected_text(), selected)

    def test_auto_scroll_ticks_do_not_lag_a_frame(self):
        self.w._last_mouse_pos = self.pos
        self.w._auto_scroll_direction = -1
        self.w._auto_scroll_pull = 0
        for step in range(1, 4):
            self.w._auto_scroll_tick()
            self.assertEqual(self.w._selection_end,
                             (self.anchor[0] - step, self.anchor[1]))
        self.w._auto_scroll_direction = 1
        self.w._auto_scroll_tick()
        self.assertEqual(self.w._selection_end,
                         (self.anchor[0] - 2, self.anchor[1]))

    def test_mouse_move_after_wheel_uses_pending_viewport(self):
        self.wheel(240)
        self.pos.setY(self.pos.y() + int(2 * self.w.char_height))
        self.w.mouseMoveEvent(self.mouse(QEvent.Type.MouseMove,
                                       Qt.MouseButton.LeftButton))
        display_row = self.w._pos_to_cell(self.pos)[0]
        self.assertEqual(self.w._selection_end[0],
                         self.start_view - 3 + display_row)

    def test_scroll_limits_clamp_endpoint(self):
        self.wheel(120_000)
        self.assertEqual(self.w.scroll_offset, self.w._get_history_count())
        self.assertEqual(self.w._selection_end[0], 10)
        endpoint = self.w._selection_end
        self.wheel(240)
        self.assertEqual(self.w._selection_end, endpoint)
        self.wheel(-120_000)
        self.assertEqual(self.w.scroll_offset, 0)
        self.assertEqual(self.w._selection_end[0],
                         self.w._get_history_count() + 10)

    def test_scrollbar_and_bottom_jump_update_drag_endpoint(self):
        self.w._on_scrollbar_value_changed(self.start_view - 4)
        self.assertEqual(self.w._selection_end[0], self.anchor[0] - 4)
        self.w.scroll_to_bottom()
        self.assertEqual(self.w._selection_end[0],
                         self.w._get_history_count() + 10)

    def test_completed_selection_moves_with_text_and_pixels_match_full_redraw(self):
        self.wheel(240)
        self.w.mouseReleaseEvent(self.mouse(QEvent.Type.MouseButtonRelease,
                                          Qt.MouseButton.NoButton))
        selected = self.w._get_selected_text()
        self.w.grab()
        before = self.w._get_visible_selection_range()
        self.wheel(240)
        image = self.w.grab().toImage()
        after = self.w._get_visible_selection_range()
        self.assertEqual(after[0], before[0] + 3)
        self.assertEqual(self.w._get_selected_text(), selected)
        self.w._last_render_state = None
        self.w._cache_valid = False
        self.assertEqual(image, self.w.grab().toImage())
