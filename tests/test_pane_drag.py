# -*- coding: utf-8 -*-
"""窗格把手：拖着把手把窗格挪到任意位置 / 变成独立标签页。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_pane_drag.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication, QSplitter


class TestPaneDrag(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window
        cls.win = main_window.MainWindow()
        cls.win.resize(1000, 700)
        cls.win.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtGui import QCloseEvent
        for w in [w for w in cls.app.topLevelWidgets()
                  if isinstance(w, cls.mw.MainWindow) and not sip.isdeleted(w)]:
            if getattr(w, '_closing_in_progress', False):
                continue
            w._force_closing = True
            QApplication.sendEvent(w, QCloseEvent())
            w.deleteLater()
        cls.app.processEvents()
        del cls.win

    def _fresh_split_tab(self, win, n=2, orientation=Qt.Orientation.Horizontal):
        """新开一页并分成 n 个窗格，返回 (idx, terminals)。"""
        idx = win._add_new_tab(tab_name=f"panes{n}")
        win.tab_widget.setCurrentIndex(idx)
        for _ in range(n - 1):
            win.active_terminal = win.tab_terminals[idx][-1]
            win.active_terminal.setFocus()
            if orientation == Qt.Orientation.Horizontal:
                win._split_current_tab(whole_tab=True)
            else:
                win._split_vertical_current_terminal(whole_tab=True)
        self.app.processEvents()
        return idx, list(win.tab_terminals[idx])

    def _consistent(self, win):
        for i in range(win.tab_widget.count()):
            self.assertIs(win.tab_splitters[i], win.tab_widget.widget(i))
            page = win.tab_widget.widget(i)
            for term in win.tab_terminals[i]:
                self.assertIs(term._owner_window, win)
                self.assertIn(term, page.findChildren(self.mw.TerminalWidget))

    def test_handles_follow_pane_count(self):
        w = self.win
        idx, (a, b) = self._fresh_split_tab(w, 2)
        self.assertTrue(a._pane_handle_visible and b._pane_handle_visible)
        self.assertGreater(a._header_h, 0)
        w.active_terminal = b
        b.setFocus()
        w._close_current_split()
        self.app.processEvents()
        self.assertFalse(a._pane_handle_visible)
        self.assertEqual(a._header_h, 0)

    def test_move_pane_to_other_side_same_orientation(self):
        w = self.win
        idx, (a, b) = self._fresh_split_tab(w, 2)
        top = w.tab_widget.widget(idx)
        self.assertEqual([top.widget(0), top.widget(1)], [a, b])
        # 把 b 挪到 a 的左边
        self.assertTrue(w._move_pane_next_to(w, b, a, Qt.Orientation.Horizontal, True))
        top = w.tab_widget.widget(idx)
        self.assertEqual([top.widget(0), top.widget(1)], [b, a])
        self.assertEqual(set(w.tab_terminals[idx]), {a, b})
        self._consistent(w)

    def test_drop_on_the_side_it_already_sits_swaps_the_two(self):
        """[A|B]：把 B 拖到 A 的右半边（它本来就在那）→ 直接交换成 [B|A]，
        不必非得拖到最左边。"""
        w = self.win
        idx, (a, b) = self._fresh_split_tab(w, 2)
        top = w.tab_widget.widget(idx)
        top.setSizes([300, 500])
        self.assertTrue(w._move_pane_next_to(w, b, a, Qt.Orientation.Horizontal, False))
        self.assertEqual([top.widget(0), top.widget(1)], [b, a])
        s0, s1 = top.sizes()
        self.assertGreater(s0, s1)                        # 尺寸跟着换（splitter 会按比例缩放）
        self.assertEqual(w.tab_terminals[idx], [b, a])
        # 再拖一次（A 现在在右边，拖到 B 的右半边）→ 换回来
        self.assertTrue(w._move_pane_next_to(w, a, b, Qt.Orientation.Horizontal, False))
        self.assertEqual([top.widget(0), top.widget(1)], [a, b])
        self._consistent(w)

    def test_three_panes_move_is_not_a_swap(self):
        """[A|B|C]：C 拖到 A 的右半边 → [A|C|B]（真正的挪动，不是交换）。"""
        w = self.win
        idx, (a, b, c) = self._fresh_split_tab(w, 3)
        top = w.tab_widget.widget(idx)
        self.assertTrue(w._move_pane_next_to(w, c, a, Qt.Orientation.Horizontal, False))
        self.assertEqual([top.widget(0), top.widget(1), top.widget(2)], [a, c, b])
        self._consistent(w)

    def test_move_pane_below_wraps_target(self):
        w = self.win
        idx, (a, b, c) = self._fresh_split_tab(w, 3)
        # c 挪到 a 的下面：a 的位置换成一个竖向 splitter [a, c]
        self.assertTrue(w._move_pane_next_to(w, c, a, Qt.Orientation.Vertical, False))
        top = w.tab_widget.widget(idx)
        self.assertEqual(top.count(), 2)
        inner = top.widget(0)
        self.assertIsInstance(inner, QSplitter)
        self.assertEqual(inner.orientation(), Qt.Orientation.Vertical)
        self.assertEqual([inner.widget(0), inner.widget(1)], [a, c])
        self.assertIs(top.widget(1), b)
        self._consistent(w)

    def test_pop_pane_into_its_own_tab(self):
        w = self.win
        idx, (a, b) = self._fresh_split_tab(w, 2)
        n = w.tab_widget.count()
        self.assertTrue(w._pop_pane_to_tab(w, b))
        self.assertEqual(w.tab_widget.count(), n + 1)
        new_idx = w.tab_widget.count() - 1
        self.assertEqual(w.tab_terminals[new_idx], [b])
        self.assertEqual(w.tab_terminals[idx], [a])
        self.assertFalse(a._pane_handle_visible)
        self._consistent(w)

    def test_pop_refuses_when_pane_already_alone(self):
        w = self.win
        idx = w._add_new_tab(tab_name="alone")
        term = w.tab_terminals[idx][0]
        self.assertFalse(w._pop_pane_to_tab(w, term))

    def test_move_pane_across_windows(self):
        w = self.win
        other = self.mw.MainWindow()
        other.show()
        self.app.processEvents()
        idx, (a, b) = self._fresh_split_tab(w, 2)
        target = other.tab_terminals[other.tab_widget.currentIndex()][0]
        self.assertTrue(other._move_pane_next_to(w, b, target, Qt.Orientation.Horizontal, False))
        self.assertIs(b._owner_window, other)
        self.assertIn(b, other.tab_terminals[other.tab_widget.currentIndex()])
        self.assertEqual(w.tab_terminals[idx], [a])
        self._consistent(w)
        self._consistent(other)
        # 离屏下 topLevelAt 拿不到，命中按几何找：别让这个窗口留着盖住主窗口
        from PyQt6.QtGui import QCloseEvent
        other._force_closing = True
        QApplication.sendEvent(other, QCloseEvent())
        other.deleteLater()
        self.app.processEvents()

    def test_pane_hit_test_picks_nearest_edge(self):
        w = self.win
        idx, (a, b) = self._fresh_split_tab(w, 2)
        rect_a = a.geometry()
        ga = a.mapToGlobal(QPoint(rect_a.width() // 2, 5))
        hit = w._pane_drop_hit_at(ga, dragging=b)
        self.assertEqual(hit, (w, 'pane', (a, Qt.Orientation.Vertical, True)))
        gb_left = b.mapToGlobal(QPoint(3, b.height() // 2))
        hit = w._pane_drop_hit_at(gb_left, dragging=a)
        self.assertEqual(hit, (w, 'pane', (b, Qt.Orientation.Horizontal, True)))
        # 光标在自己身上 → 没有目标
        self.assertIsNone(w._pane_drop_hit_at(gb_left, dragging=b))


if __name__ == '__main__':
    unittest.main()


class TestHandleBarSwallowsMouse(unittest.TestCase):
    """拖把手时按下/拖动不能漏给终端——否则整屏文字被选成一片蓝。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from terminal_widget import TerminalWidget
        cls.term = TerminalWidget()
        cls.term.resize(600, 400)
        cls.term.show()
        cls.term.set_pane_handle_visible(True)
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        cls.term.cleanup()
        cls.term.deleteLater()
        cls.app.processEvents()

    def test_press_and_drag_on_handle_selects_nothing(self):
        from PyQt6.QtTest import QTest
        bar = self.term._header_bar
        self.assertTrue(bar.isVisible())
        got = []
        self.term.pane_drag_requested.connect(lambda p: got.append(p))
        start = QPoint(bar.width() // 2, bar.height() // 2)
        QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=start)
        for d in (3, 6, 12, 40):
            QTest.mouseMove(bar, start + QPoint(d, d))
        QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=start + QPoint(40, 40))
        self.app.processEvents()
        self.assertEqual(len(got), 1)
        self.assertFalse(self.term._has_selection())
