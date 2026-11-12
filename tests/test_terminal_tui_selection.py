"""Full-screen transcript selection survives application-owned scrolling/redraw."""
import os
import unittest
from unittest.mock import Mock

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

    def test_edge_timer_forwards_wheel_and_waits_for_redraw(self):
        self.pos = self.cell(self.w.term_rows - 1)
        self.mouse(QEvent.Type.MouseMove, Qt.MouseButton.LeftButton)
        self.w._auto_scroll_tick()
        self.w._auto_scroll_tick()
        self.w._write_to_backend.assert_called_once()
        self.assertIn(b'\x1b[<65;', self.w._write_to_backend.call_args.args[0])

    def test_wheel_uses_snapshot_path(self):
        self.w.wheelEvent(QWheelEvent(QPointF(self.pos), QPointF(self.pos),
            QPoint(), QPoint(0, 240), Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
        self.assertEqual(self.w._tui_selection['pending'], 1)
        self.w._write_to_backend.assert_called_once()

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

    def test_resize_cancels_pending_tui_selection(self):
        self.w._scroll_tui_selection(True, self.pos)
        self.w.term_cols += 1
        self.w._refresh_tui_selection()
        self.assertIsNone(self.w._tui_selection)
        self.assertFalse(self.w._has_selection())

    def test_plain_shell_does_not_enable_tui_snapshots(self):
        self.w.stream.feed('\x1b[?1049l')
        self.w._begin_tui_selection()
        self.assertIsNone(self.w._tui_selection)
