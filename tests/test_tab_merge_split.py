# -*- coding: utf-8 -*-
"""把另一个标签页并入当前页做分屏（同窗口 / 跨窗口，右键菜单与拖放落区）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_tab_merge_split.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication, QSplitter


class TestMergeTabIntoSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window
        cls.win_a = main_window.MainWindow()
        cls.win_a.resize(1000, 700)
        cls.win_a.move(0, 0)
        cls.win_a.show()
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
        del cls.win_a

    def _consistent(self, win):
        for i in range(win.tab_widget.count()):
            self.assertIs(win.tab_splitters[i], win.tab_widget.widget(i))
            for term in win.tab_terminals[i]:
                self.assertIs(term._owner_window, win)

    def _terminals_in(self, widget):
        return widget.findChildren(self.mw.TerminalWidget)

    def test_same_window_merge_side_by_side(self):
        a = self.win_a
        a._add_new_tab(tab_name="src")
        src_idx = a.tab_widget.count() - 1
        src_terms = list(a.tab_terminals[src_idx])
        dst_terms = list(a.tab_terminals[0])
        n_before = a.tab_widget.count()

        ok = a._merge_tab_into_split(a, src_idx, 0, Qt.Orientation.Horizontal)

        self.assertTrue(ok)
        self.assertEqual(a.tab_widget.count(), n_before - 1)
        page = a.tab_widget.widget(0)
        self.assertIsInstance(page, QSplitter)
        self.assertEqual(page.orientation(), Qt.Orientation.Horizontal)
        self.assertEqual(a.tab_terminals[0], dst_terms + src_terms)
        # 两页的终端都真的在这一页里，且并入的在右边
        page_terms = self._terminals_in(page)
        for t in dst_terms + src_terms:
            self.assertIn(t, page_terms)
        self.assertLess(page.indexOf(self._top_child_of(page, dst_terms[0])),
                        page.indexOf(self._top_child_of(page, src_terms[0])))
        self._consistent(a)

    def _top_child_of(self, splitter, widget):
        """widget 在 splitter 直接子控件里的那个祖先。"""
        w = widget
        while w is not None and w.parent() is not splitter:
            w = w.parent()
        return w

    def test_vertical_merge_wraps_and_before_puts_it_on_top(self):
        a = self.win_a
        a._add_new_tab(tab_name="top")
        src_idx = a.tab_widget.count() - 1
        src_terms = list(a.tab_terminals[src_idx])
        old_page = a.tab_widget.widget(0)

        ok = a._merge_tab_into_split(a, src_idx, 0, Qt.Orientation.Vertical, before=True)

        self.assertTrue(ok)
        page = a.tab_widget.widget(0)
        self.assertEqual(page.orientation(), Qt.Orientation.Vertical)
        self.assertIsNot(page, old_page)             # 方向不同 → 包了一层
        self.assertIs(page.widget(1), old_page)      # 旧页在下面
        self.assertIs(page.widget(0), src_terms[0])  # 单窗格源页被解包成终端本身
        self.assertEqual(a.tab_terminals[0][0], src_terms[0])
        self._consistent(a)

    def test_cross_window_merge_closes_emptied_source(self):
        a = self.win_a
        b = self.mw.MainWindow()
        b.show()
        self.app.processEvents()
        b_terms = list(b.tab_terminals[0])

        ok = a._merge_tab_into_split(b, 0, 0, Qt.Orientation.Horizontal)

        self.assertTrue(ok)
        for t in b_terms:
            self.assertIs(t._owner_window, a)
            self.assertIn(t, a.tab_terminals[0])
        for _ in range(10):
            self.app.processEvents()
            if sip.isdeleted(b) or not b.isVisible():
                break
        self.assertTrue(sip.isdeleted(b) or not b.isVisible())
        self._consistent(a)

    def test_refuses_merging_tab_into_itself(self):
        a = self.win_a
        n = a.tab_widget.count()
        self.assertFalse(a._merge_tab_into_split(a, 0, 0, Qt.Orientation.Horizontal))
        self.assertEqual(a.tab_widget.count(), n)

    def test_split_zone_hit_test(self):
        a = self.win_a
        rect = a._tab_page_rect()
        self.assertIsNotNone(rect)
        left = QPoint(rect.left() + 5, rect.center().y())
        right = QPoint(rect.right() - 5, rect.center().y())
        top = QPoint(rect.center().x(), rect.top() + 5)
        bottom = QPoint(rect.center().x(), rect.bottom() - 5)
        self.assertEqual(a._split_zone_at(left), (Qt.Orientation.Horizontal, True))
        self.assertEqual(a._split_zone_at(right), (Qt.Orientation.Horizontal, False))
        self.assertEqual(a._split_zone_at(top), (Qt.Orientation.Vertical, True))
        self.assertEqual(a._split_zone_at(bottom), (Qt.Orientation.Vertical, False))
        self.assertIsNone(a._split_zone_at(rect.center()))
        # 综合命中：标签栏优先于页面落区
        hit = a._tab_drop_hit_at(a._tab_drop_strip_rect().center())
        self.assertEqual((hit[0], hit[1]), (a, 'strip'))
        hit = a._tab_drop_hit_at(left)
        self.assertEqual(hit, (a, 'split', (Qt.Orientation.Horizontal, True)))
        # 正拖着的就是当前页：落回自己当前页做分屏没意义，不算命中
        self.assertIsNone(a._tab_drop_hit_at(left, dragging_index=a.tab_widget.currentIndex()))

    def test_finish_drag_on_split_zone_merges(self):
        a = self.win_a
        a._add_new_tab(tab_name="zone")
        src = a.tab_widget.count() - 1
        a.tab_widget.setCurrentIndex(0)
        n = a.tab_widget.count()
        rect = a._tab_page_rect()
        pos = QPoint(rect.right() - 5, rect.center().y())
        a._finish_tab_drag(src, pos, (a, 'split', (Qt.Orientation.Horizontal, False)))
        self.assertEqual(a.tab_widget.count(), n - 1)
        page = a.tab_widget.widget(0)
        self.assertEqual(page.orientation(), Qt.Orientation.Horizontal)
        self._consistent(a)


class TestDragCurrentTabSplitsInOneGo(unittest.TestCase):
    """拖当前页：页面区先切到邻页，落在页面边缘就能直接和它分屏（不必先拆窗再拖回）。"""

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
        cls.win._force_closing = True
        QApplication.sendEvent(cls.win, QCloseEvent())
        cls.win.deleteLater()
        cls.app.processEvents()
        del cls.win

    def test_switches_to_neighbor_and_merges_on_edge_drop(self):
        w = self.win
        w._add_new_tab(tab_name="second")
        idx = w.tab_widget.count() - 1
        w.tab_widget.setCurrentIndex(idx)
        second_terms = list(w.tab_terminals[idx])
        neighbor = idx - 1
        first_page = w.tab_widget.widget(neighbor)
        n = w.tab_widget.count()

        self.assertTrue(w._drag_switch_to_neighbor(idx))
        self.assertEqual(w.tab_widget.currentIndex(), neighbor)   # 页面区显示的是要并进去的那页
        rect = w._tab_page_rect()
        pos = QPoint(rect.right() - 5, rect.center().y())
        hit = w._tab_drop_hit_at(pos, dragging_index=idx)
        self.assertEqual(hit, (w, 'split', (Qt.Orientation.Horizontal, False)))

        w._finish_tab_drag(idx, pos, hit)
        self.assertEqual(w.tab_widget.count(), n - 1)
        page = w.tab_widget.widget(neighbor)
        self.assertEqual(page.orientation(), Qt.Orientation.Horizontal)
        self.assertIn(second_terms[0], w.tab_terminals[neighbor])
        # 目标页顶层方向相同 → 直接插进它（page 就是原页）；否则外面包一层
        self.assertTrue(page is first_page or page.widget(0) is first_page)
        self.assertIs(page.widget(page.count() - 1), second_terms[0])   # 并入的在右边

    def test_nothing_happens_restores_dragged_tab(self):
        w = self.win
        w._add_new_tab(tab_name="third")
        idx = w.tab_widget.count() - 1
        w.tab_widget.setCurrentIndex(idx)
        page = w.tab_widget.widget(idx)
        self.assertTrue(w._drag_switch_to_neighbor(idx))
        self.assertNotEqual(w.tab_widget.currentIndex(), idx)
        w._drag_restore_current(page)
        self.assertEqual(w.tab_widget.currentIndex(), idx)

    def test_page_center_is_still_open_space(self):
        w = self.win
        rect = w._tab_page_rect()
        self.assertIsNone(w._split_zone_at(rect.center()))
        # 离边 30% 处已经算靠边（以前要 <30% 才算，中间一大片什么都不发生）
        p = QPoint(rect.left() + int(rect.width() * 0.35), rect.center().y())
        self.assertEqual(w._split_zone_at(p), (Qt.Orientation.Horizontal, True))


if __name__ == '__main__':
    unittest.main()
