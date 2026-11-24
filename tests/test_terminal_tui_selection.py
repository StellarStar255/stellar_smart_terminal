"""Full-screen transcript selection survives application-owned scrolling/redraw."""
import os
import unittest
import re
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication

from terminal_widget import TerminalWidget


class TestTuiSelection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = TerminalWidget()
        self.w.resize(640, 400)
        self.w._update_terminal_size()
        self.w.stream.feed('\x1b[?1049h\x1b[?1000h')
        self.w._mouse_mode = True
        self.w._write_to_backend = Mock(return_value=True)
        self.w._backend = object()
        self.offset = 100
        self.draw()
        self.pos = self.cell(4)
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)

    def tearDown(self):
        self.w._backend = None
        self.w.cleanup()
        self.w.deleteLater()

    def cell(self, row, col=0):
        return QPoint(int(self.w.PADDING + (col + .2) * self.w.char_width),
                      int(self.w.PADDING + self.w._header_h + (row + .5) * self.w.char_height))

    def draw(self):
        self.w.stream.feed('\x1b[H\x1b[2Jheader 固定标题')
        for r in range(1, self.w.term_rows - 3):
            self.w.stream.feed(f'\x1b[{r + 1};1Hrow {self.offset + r:04d} 中文 sample')
        self.w.stream.feed(f'\x1b[{self.w.term_rows - 2};1HAsk Codex to do anything')
        self.w._invalidate_render_cache()

    def mouse(self, kind, buttons):
        e = QMouseEvent(kind, QPointF(self.pos), Qt.MouseButton.LeftButton,
                        buttons, Qt.KeyboardModifier.NoModifier)
        {QEvent.Type.MouseButtonPress: self.w.mousePressEvent,
         QEvent.Type.MouseMove: self.w.mouseMoveEvent,
         QEvent.Type.MouseButtonRelease: self.w.mouseReleaseEvent}[kind](e)

    def scroll(self, delta):
        self.w._scroll_tui_selection(delta < 0, self.pos)
        self.offset += delta
        self.draw()
        # Application output can arrive before a paint or another mouse event.
        self.w._refresh_tui_selection()

    def test_downward_selection_across_multiple_pages_without_footer_or_duplicates(self):
        self.pos = self.cell(self.w.term_rows - 4, 25)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        for _ in range(self.w.term_rows):
            self.scroll(3)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        text = self.w._get_selected_text()
        expected = '\n'.join(f'row {i:04d} 中文 sample'
                             for i in range(104, self.offset + self.w.term_rows - 3))
        self.assertEqual(text, expected)
        self.assertNotIn('Ask Codex', text)
        self.assertEqual(self.w._get_history_count(), 0)
        self.offset += 20
        self.draw()
        self.assertEqual(self.w._get_selected_text(), text)
        self.w.grab()  # completed selection must render safely after replacement output

    def test_snapshot_limit_keeps_last_confirmed_page_and_consumes_release(self):
        self.w.set_mouse_click_forward_enabled(True)
        self.pos = self.cell(self.w.term_rows - 4, 20)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.scroll(3)
        state = self.w._tui_selection
        saved = state['rows']
        for i in range(20000 - len(saved)):
            saved[-1 - i] = ({}, False)
        expected = self.w._get_selected_text()
        self.w._scroll_tui_selection(False, self.pos)
        self.offset += 3
        self.draw()
        self.w._write_to_backend.reset_mock()
        self.w._auto_scroll_tick()
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.assertFalse(self.w._auto_scroll_timer.isActive())
        self.assertLessEqual(len(saved), 20000)
        self.assertEqual(self.w._get_selected_text(), expected)
        self.w._write_to_backend.assert_not_called()

    def test_returning_drag_to_anchor_is_not_a_click(self):
        self.w.set_mouse_click_forward_enabled(True)
        self.pos = self.cell(5)
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)
        self.pos = self.cell(8, 20)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.pos = self.cell(5)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.w._write_to_backend.assert_not_called()

    def test_middle_release_during_left_drag_preserves_edge_scroll(self):
        self.w.set_mouse_click_forward_enabled(True)
        self.pos = self.cell(self.w.term_rows - 1)
        left, middle = Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton
        self.mouse(QEvent.Type.MouseMove, left)
        self.assertTrue(self.w._auto_scroll_timer.isActive())
        with patch.object(self.w, '_send_mouse_event') as send:
            for kind, buttons in ((QEvent.Type.MouseButtonPress, left | middle),
                                  (QEvent.Type.MouseButtonRelease, left)):
                self.app.sendEvent(self.w, QMouseEvent(kind, QPointF(self.pos), middle,
                                                       buttons, Qt.KeyboardModifier.NoModifier))
            self.assertEqual([call.args[1] for call in send.call_args_list], ['press', 'release'])
        self.assertTrue(self.w._auto_scroll_timer.isActive())
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.assertFalse(self.w._auto_scroll_timer.isActive())

    def test_release_checks_drag_distance_without_intermediate_move(self):
        self.w.set_mouse_click_forward_enabled(True)
        self.w.char_width = QApplication.startDragDistance() + 8
        self.pos = self.cell(6, 1)
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)
        self.pos = self.cell(6, 2)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.assertTrue(self.w._has_selection())
        self.w._write_to_backend.assert_not_called()

    def test_alt_screen_exit_before_next_tick_preserves_shell_scroll_position(self):
        self.pos = self.cell(0)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        expected = self.w._get_selected_text()
        self.w.stream.feed('\x1b[?1049l' + 'shell output\r\n' * 50)
        self.w._auto_scroll_tick()
        self.assertEqual(self.w.scroll_offset, 0)
        self.assertFalse(self.w._auto_scroll_timer.isActive())
        self.assertEqual(self.w._get_selected_text(), expected)

    def test_repeating_logs_need_a_distinguishable_scroll_anchor(self):
        def draw_pattern(offset, unique):
            self.w.stream.feed('\x1b[H\x1b[2Jheader')
            for r in range(1, self.w.term_rows - 2):
                i = offset + r
                label = 'checkpoint' if unique and i == 7 else f'status {i % 3}'
                self.w.stream.feed(f'\x1b[{r+1};1H{label}')

        for unique in (False, True):
            draw_pattern(0, unique)
            self.w._begin_tui_selection(self.pos)
            self.w._scroll_tui_selection(False, self.pos)
            draw_pattern(4, unique)
            self.w._refresh_tui_selection()
            self.assertEqual(self.w._tui_selection['origin'], 4 if unique else 0)

    def test_release_before_wheel_redraw_freezes_visible_selection(self):
        self.pos = self.cell(8, 20)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        expected = self.w._get_selected_text()
        self.w._scroll_tui_selection(False, self.pos)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.offset += 3
        self.draw()
        self.w._refresh_tui_selection(redraw=True)
        self.assertEqual(self.w._get_selected_text(), expected)
        self.assertFalse(self.w._auto_scroll_timer.isActive())

    def test_release_at_upper_edge_keeps_highlight_after_late_codex_frame(self):
        self.pos = self.cell(0, 2)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        for _ in range(self.w.term_rows // 3 + 2):
            self.scroll(-3)
        self.w._scroll_tui_selection(True, self.pos)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        expected = self.w._get_selected_text()
        self.assertGreater(len(expected.splitlines()), self.w.term_rows)

        def color(row):
            image = self.w.grab().toImage()
            pos = self.cell(row, 30) * image.devicePixelRatio()
            return image.pixelColor(pos)

        selected = color(6)
        self.offset -= 3
        self.draw()
        self.assertEqual(color(9), selected)
        self.assertNotEqual(color(2), selected)  # newly exposed row outside selection
        self.w._copy_selection_to_clipboard()
        self.assertEqual(self.app.clipboard().text(), expected)

    def test_upward_selection_and_reverse_keep_anchor(self):
        anchor = self.w._selection_start
        self.pos = self.cell(1)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        for _ in range(5):
            self.scroll(-3)
        self.assertEqual(self.w._selection_start, anchor)
        text = self.w._get_selected_text()
        self.assertTrue(text.startswith('row 0086 中文 sample\n'))
        self.assertTrue(text.endswith('\nr'))  # original anchor column zero
        self.scroll(3)
        self.assertTrue(self.w._get_selected_text().startswith('row 0089 中文 sample\n'))
        self.assertEqual(self.w._selection_start, anchor)

    def test_delayed_redraw_retries_confirmed_transcript_position(self):
        self.scroll(3)
        state = self.w._tui_selection
        self.assertNotEqual(state['bounds'], (0, self.w.term_rows))
        edge = self.cell(self.w.term_rows - 1)
        with patch.object(self.w, '_send_wheel_to_app') as send:
            self.w._scroll_tui_selection(False, edge)
            state['sent'] -= 1
            self.w._scroll_tui_selection(False, edge)
        self.assertEqual([c.args[1] for c in send.call_args_list], [self.pos, self.pos])

    def test_edge_timer_forwards_wheel_and_waits_for_redraw(self):
        self.pos = self.cell(self.w.term_rows - 1)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.w._auto_scroll_tick()
        self.w._auto_scroll_tick()
        self.w._write_to_backend.assert_called_once()
        self.assertIn(b'\x1b[<65;', self.w._write_to_backend.call_args.args[0])

    def test_wheel_uses_snapshot_path(self):
        self.pos = self.cell(self.w.term_rows - 1)  # wheel while pointer is over input/footer
        self.w.wheelEvent(QWheelEvent(QPointF(self.pos), QPointF(self.pos),
            QPoint(), QPoint(0, 240), Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
        self.assertEqual(self.w._tui_selection['pending'], 1)
        self.w._write_to_backend.assert_called_once()
        self.assertEqual(self.w._write_to_backend.call_args.args[0], b'\x1b[<64;1;5M')

    def test_edge_drag_obeys_codex_transcript_hit_testing_and_scroll_speed(self):
        """Codex ignores wheel events over its footer; one notch scrolls three rows.

        Unlike the old fixture, pages change only when an accepted SGR event
        arrives, rather than unconditionally after asking the terminal to scroll.
        """
        accepted = []

        def codex_wheel(data):
            for button, col, row in re.findall(rb'\x1b\[<(64|65);(\d+);(\d+)M', data):
                if not 2 <= int(row) <= self.w.term_rows - 3:
                    continue
                accepted.append((int(button), int(row)))
                self.offset += -3 if button == b'64' else 3
                self.draw()
            return True

        self.w._write_to_backend = Mock(side_effect=codex_wheel)
        self.pos = self.cell(self.w.term_rows + 3, self.w.term_cols - 1)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        for _ in range(20):
            self.w._auto_scroll_tick()
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.assertEqual(len(accepted), 20)
        self.assertTrue(all(row == 5 for _, row in accepted))
        expected = '\n'.join(f'row {i:04d} 中文 sample'
                             for i in range(104, self.offset + self.w.term_rows - 3))
        self.assertEqual(self.w._get_selected_text(), expected)
        # 每个 tick 只发送一格，不能被应用滚速再放大成整页跳跃。
        self.assertTrue(all(c.args[0].count(b'M') == 1
                            for c in self.w._write_to_backend.call_args_list))

    def test_top_edge_reports_the_original_transcript_position(self):
        self.pos = self.cell(-3)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.w._auto_scroll_tick()
        report = self.w._write_to_backend.call_args.args[0]
        self.assertEqual(report, b'\x1b[<64;1;5M')

    def test_selection_starting_on_sticky_header_retries_inside_transcript(self):
        self.pos = self.cell(0)
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)

        def codex_wheel(data):
            for button, col, row in re.findall(rb'\x1b\[<(64|65);(\d+);(\d+)M', data):
                if 2 <= int(row) <= self.w.term_rows - 3:
                    self.offset += 3
                    self.draw()
            return True

        self.w._write_to_backend = Mock(side_effect=codex_wheel)
        self.w._scroll_tui_selection(False, self.pos)
        self.assertEqual(self.offset, 100)  # pinned header ignored by Codex
        # A progress/status update must not make an ignored header wheel look
        # like a page redraw and block all subsequent scrolling.
        self.w.stream.feed(f'\x1b[{self.w.term_rows};1HWorking status update')
        sent = self.w._tui_selection['sent']
        with patch('terminal_mouse.time.monotonic', return_value=sent + 1):
            self.w._scroll_tui_selection(False, self.pos)
        self.w._refresh_tui_selection()
        self.assertEqual(self.offset, 103)
        self.assertEqual(self.w._tui_selection['origin'], 3)

    def test_changed_unaligned_frame_does_not_send_more_wheel_events(self):
        self.w._scroll_tui_selection(False, self.pos)
        sent = self.w._tui_selection['sent']
        self.offset += 1000  # no overlap; may be an incomplete redraw or unrelated output
        self.draw()
        with patch('terminal_mouse.time.monotonic', return_value=sent + 1):
            self.w._scroll_tui_selection(False, self.pos)
        self.w._write_to_backend.assert_called_once()

    def test_snapshot_preserves_cursor_positioned_columns_after_release(self):
        self.w.stream.feed('\x1bc\x1b[?1049h\x1b[?1000h\x1b[5;4H中\x1b[5;11H42')
        self.pos = self.cell(4, 0)
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)
        self.pos = self.cell(4, 12)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.w.stream.feed('\x1b[5;1H\x1b[2Kreplacement')
        self.w._copy_selection_to_clipboard()
        self.assertEqual(self.app.clipboard().text(), '   中     42')

    def test_unrelated_redraw_does_not_invent_a_scroll(self):
        self.w._scroll_tui_selection(True, self.pos)
        self.w.stream.feed('\x1b[6;1Hunrelated progress changed')
        self.w._refresh_tui_selection()
        self.assertEqual(self.w._tui_selection['origin'], 0)

    def test_changing_footer_is_not_part_of_scrolled_transcript(self):
        self.pos = self.cell(self.w.term_rows - 4, 25)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.w._scroll_tui_selection(False, self.pos)
        self.offset += 3
        self.draw()
        self.w.stream.feed(f'\x1b[{self.w.term_rows - 1};1HBack to bottom - esc')
        self.w._refresh_tui_selection()
        self.assertEqual(self.w._tui_selection['bounds'], (1, self.w.term_rows - 3))
        text = self.w._get_selected_text()
        self.assertNotIn('Ask Codex', text)
        self.assertNotIn('Back to bottom', text)

    def test_footer_resizes_during_drag_and_scroll_reversal(self):
        rows = self.w.term_rows

        def draw_footer(height):
            self.draw()
            self.w.stream.feed(f'\x1b[{rows - height + 1};1H\x1b[J'
                               '• Waiting for background terminal\r\n└ gh run watch'
                               '\r\n↓ Back to bottom · esc')

        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        draw_footer(4)
        self.pos = self.cell(3)
        self.mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton)
        self.pos = self.cell(rows - 6, 1)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.assertFalse(self.w._auto_scroll_timer.isActive())
        draw_footer(6)
        self.w._rebuild_cache()
        self.assertTrue(self.w._auto_scroll_timer.isActive())
        draw_footer(4)
        self.w._rebuild_cache()
        self.assertFalse(self.w._auto_scroll_timer.isActive())
        self.pos = self.cell(rows - 4, 1)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        for delta, height in [(3, 6), (3, 4), (-3, 5)]:
            self.w._scroll_tui_selection(delta < 0, self.pos)
            self.offset += delta
            draw_footer(height)
            self.w._refresh_tui_selection()
            self.assertEqual(self.w._tui_selection['origin'], self.offset - 100)
            self.assertEqual(self.w._tui_selection['footer'], rows - height)
            expected = '\n'.join(f'row {i:04d} 中文 sample'
                                 for i in range(103, self.offset + rows - height))
            self.assertEqual(self.w._get_selected_text(), expected)

    def test_resize_cancels_pending_tui_selection(self):
        self.w._scroll_tui_selection(True, self.pos)
        self.w.term_cols += 1
        self.w._refresh_tui_selection()
        self.assertIsNone(self.w._tui_selection)
        self.assertFalse(self.w._has_selection())

    def test_fractional_cells_hit_the_painted_row_and_column(self):
        self.w.char_width, self.w.char_height = 9.6, 18.75
        self.w._header_h = 17
        for row in range(self.w.term_rows):
            top, height = self.w._row_tile(row)
            for col in range(self.w.term_cols):
                left, width = self.w._col_tile(col)
                for pos in (QPoint(left, top), QPoint(left + width - 1, top + height - 1)):
                    self.assertEqual(self.w._pos_to_cell(pos), (row, col))
                    self.assertEqual(self.w._pos_to_absolute_cell(pos), (row, col))

    def test_drag_keeps_logical_cells_across_device_pixel_ratios(self):
        self.pos = self.cell(8, 24)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.scroll(3)
        expected = self.w._get_selected_text()
        anchor = self.w._selection_start
        for ratio in (1.0, 1.25, 2.0, 1.0):
            with patch.object(self.w, 'devicePixelRatioF', return_value=ratio):
                self.w._rebuild_cache()
                self.assertEqual(self.w._cache_pixmap.devicePixelRatio(), ratio)
                self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
                self.assertEqual(self.w._selection_start, anchor)
                self.assertEqual(self.w._get_selected_text(), expected)

    def test_grid_resize_preserves_snapshot_width(self):
        self.pos = self.cell(8, 24)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.scroll(3)
        expected = self.w._get_selected_text()
        self.assertIn('中文 sample', expected)
        self.w.term_cols = 10
        self.w._refresh_tui_selection()
        self.assertFalse(self.w._is_selecting)
        self.assertEqual(self.w._get_selected_text(), expected)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.assertEqual(self.w._get_selected_text(), expected)

    def test_leave_reenter_then_lose_grab_at_edge(self):
        self.w.set_mouse_click_forward_enabled(True)
        self.pos = self.cell(self.w.term_rows + 3, 20)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.app.sendEvent(self.w, QEvent(QEvent.Type.Leave))
        self.assertTrue(self.w._auto_scroll_timer.isActive())
        self.pos = self.cell(5, 15)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.assertFalse(self.w._auto_scroll_timer.isActive())
        self.pos = self.cell(self.w.term_rows + 3, 20)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        expected = self.w._get_selected_text()
        self.app.sendEvent(self.w, QEvent(QEvent.Type.UngrabMouse))
        self.assertFalse(self.w._auto_scroll_timer.isActive())
        self.pos = self.cell(0)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton)
        self.mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)
        self.assertEqual(self.w._get_selected_text(), expected)
        self.w._write_to_backend.assert_not_called()

    def test_cleanup_during_drag_ignores_late_ungrab(self):
        self.w._backend = None
        self.w.cleanup()
        self.app.sendEvent(self.w, QEvent(QEvent.Type.UngrabMouse))
        self.assertFalse(self.w._is_selecting)

    def test_plain_shell_does_not_enable_tui_snapshots(self):
        self.w.stream.feed('\x1b[?1049l')
        self.w._begin_tui_selection()
        self.assertIsNone(self.w._tui_selection)
