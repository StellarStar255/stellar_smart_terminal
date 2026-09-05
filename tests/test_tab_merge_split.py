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
        self.assertIsNone(a._tab_drop_hit_at(left, exclude=a))


if __name__ == '__main__':
    unittest.main()
