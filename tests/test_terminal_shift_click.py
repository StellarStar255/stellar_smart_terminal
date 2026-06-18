"""终端 Shift+点击扩展选区测试。

场景：选中一段 → 滚动若干页 → 按住 Shift 点击新位置 → 中间整段连选。
用桩替换 _pos_to_absolute_cell / _pos_to_cell，避免依赖字体度量/布局。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_terminal_shift_click -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestShiftClickExtend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.stream.feed('x\r\n' * 60)
        return w

    def _press(self, w, mods, abs_cell, rel_cell=(0, 0)):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QPointF, QEvent, Qt
        w._pos_to_absolute_cell = lambda pos, _c=abs_cell: _c
        w._pos_to_cell = lambda pos, _c=rel_cell: _c
        w.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(9, 9),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mods))

    def _release(self, w, mods, abs_cell, rel_cell=(0, 0)):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QPointF, QEvent, Qt
        w._pos_to_absolute_cell = lambda pos, _c=abs_cell: _c
        w._pos_to_cell = lambda pos, _c=rel_cell: _c
        w.mouseReleaseEvent(QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(9, 9),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, mods))

    def _drag_select(self, w, start, end):
        from PyQt6.QtCore import Qt
        self._press(w, Qt.KeyboardModifier.NoModifier, start, (start[0], start[1]))
        self._release(w, Qt.KeyboardModifier.NoModifier, end, (end[0], end[1]))

    def test_shift_click_extends_from_anchor(self):
        from PyQt6.QtCore import Qt
        w = self._widget()
        self._drag_select(w, (5, 0), (8, 10))
        self.assertEqual(w._selection_start, (5, 0))
        self.assertEqual(w._selection_end, (8, 10))

        # 滚动若干页后 Shift+点击远处一格
        self._press(w, Qt.KeyboardModifier.ShiftModifier, (40, 3), (3, 3))
        self.assertEqual(w._selection_start, (5, 0), "锚点应保留")
        self.assertEqual(w._selection_end, (40, 3), "终点应扩展到点击处")
        self.assertTrue(w._shift_extend_click)

        self._release(w, Qt.KeyboardModifier.ShiftModifier, (40, 3), (3, 3))
        self.assertEqual(w._selection_start, (5, 0), "释放后选区不应被清")
        self.assertEqual(w._selection_end, (40, 3))

    def test_plain_click_resets_selection(self):
        from PyQt6.QtCore import Qt
        w = self._widget()
        self._drag_select(w, (5, 0), (8, 10))
        # 无拖动的普通单击 → 清空选区
        self._press(w, Qt.KeyboardModifier.NoModifier, (12, 5), (2, 5))
        self._release(w, Qt.KeyboardModifier.NoModifier, (12, 5), (2, 5))
        self.assertIsNone(w._selection_start)
        self.assertIsNone(w._selection_end)

    def test_shift_click_without_prior_selection_starts_fresh(self):
        from PyQt6.QtCore import Qt
        w = self._widget()
        self.assertIsNone(w._selection_start)
        # 无已有选区时 Shift+点击不应崩溃，按新选区起点处理
        self._press(w, Qt.KeyboardModifier.ShiftModifier, (20, 2), (2, 2))
        self.assertEqual(w._selection_start, (20, 2))
        self.assertFalse(w._shift_extend_click)


if __name__ == '__main__':
    unittest.main()
