# -*- coding: utf-8 -*-
"""跨窗口移动标签页（拖到另一个窗口的标签栏上并入）的回归测试。

用户报告：标签拖出成新窗口之后，再也没法拖回去。以前只有「拖出」这一个
方向：_detach_tab 在窗口只剩一个标签时直接 return，别的窗口也没有任何
接收标签的入口。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_tab_move_between_windows.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication


class TestMoveTabBetweenWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw_mod = main_window
        cls.win_a = main_window.MainWindow()
        cls.win_b = main_window.MainWindow()
        cls.win_a.move(0, 0)
        cls.win_b.move(0, 900)
        cls.win_a.show()
        cls.win_b.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtGui import QCloseEvent
        for w in (cls.win_a, cls.win_b):
            if sip.isdeleted(w):
                continue
            w._force_closing = True
            QApplication.sendEvent(w, QCloseEvent())
            w.deleteLater()
        cls.app.processEvents()
        del cls.win_a, cls.win_b

    def _assert_mappings_consistent(self, win):
        self.assertEqual(win.tab_widget.count(), len(win.tab_splitters))
        for i in range(win.tab_widget.count()):
            self.assertIs(win.tab_splitters[i], win.tab_widget.widget(i))
            self.assertIn(i, win.tab_terminals)
            for term in win.tab_terminals[i]:
                self.assertIs(getattr(term, '_owner_window', None), win)

    def test_adopt_tab_moves_page_and_ownership(self):
        a, b = self.win_a, self.win_b
        a._add_new_tab(tab_name="moving")
        moving_idx = a.tab_widget.count() - 1
        moving_terms = list(a.tab_terminals[moving_idx])
        a_before, b_before = a.tab_widget.count(), b.tab_widget.count()

        idx = b._adopt_tab_from(a, moving_idx)

        self.assertGreaterEqual(idx, 0)
        self.assertEqual(a.tab_widget.count(), a_before - 1)
        self.assertEqual(b.tab_widget.count(), b_before + 1)
        self.assertEqual(b.tab_widget.tabText(idx), "moving")
        self.assertEqual(b.tab_terminals[idx], moving_terms)
        for term in moving_terms:
            self.assertIs(term._owner_window, b)
            for terms in a.tab_terminals.values():
                self.assertNotIn(term, terms)
        self._assert_mappings_consistent(a)
        self._assert_mappings_consistent(b)

    def test_adopt_respects_insert_index(self):
        a, b = self.win_a, self.win_b
        a._add_new_tab(tab_name="front")
        idx = b._adopt_tab_from(a, a.tab_widget.count() - 1, insert_index=0)
        self.assertEqual(idx, 0)
        self.assertEqual(b.tab_widget.tabText(0), "front")
        self._assert_mappings_consistent(b)

    def test_adopt_refuses_self(self):
        a = self.win_a
        n = a.tab_widget.count()
        self.assertEqual(a._adopt_tab_from(a, 0), -1)
        self.assertEqual(a.tab_widget.count(), n)

    def test_drop_target_hit_test(self):
        a, b = self.win_a, self.win_b
        strip_b = b._tab_drop_strip_rect()
        hit = a._tab_drop_hit_at(strip_b.center())
        self.assertEqual((hit[0], hit[1]), (b, 'strip'))
        # 页面正中（不靠任何边）不算
        mid = b.mapToGlobal(QPoint(b.width() // 2, b.height() // 2))
        self.assertIsNone(a._tab_drop_hit_at(mid))

    def test_finish_drag_on_strip_of_other_window_moves_tab(self):
        a, b = self.win_a, self.win_b
        a._add_new_tab(tab_name="fly")
        idx = a.tab_widget.count() - 1
        a_before, b_before = a.tab_widget.count(), b.tab_widget.count()
        a._finish_tab_drag(idx, b._tab_drop_strip_rect().center(), (b, 'strip', None))
        self.assertEqual(a.tab_widget.count(), a_before - 1)
        self.assertEqual(b.tab_widget.count(), b_before + 1)
        self._assert_mappings_consistent(a)
        self._assert_mappings_consistent(b)

    def test_finish_drag_on_own_strip_reorders(self):
        a = self.win_a
        while a.tab_widget.count() < 3:
            a._add_new_tab(tab_name=f"t{a.tab_widget.count()}")
        a.tab_widget.setTabText(0, "first")
        last = a.tab_widget.count() - 1
        a.tab_widget.setTabText(last, "last")
        bar = a.tab_widget.tabBar()
        r0 = bar.tabRect(0)
        pos = bar.mapToGlobal(QPoint(r0.left() + 2, r0.center().y()))   # 插到第 0 个前面
        a._finish_tab_drag(last, pos, (a, 'strip', None))
        self.assertEqual(a.tab_widget.tabText(0), "last")
        self.assertEqual(a.tab_widget.tabText(1), "first")
        self._assert_mappings_consistent(a)

    def test_finish_drag_in_the_open_detaches_at_drop_point(self):
        a = self.win_a
        a._add_new_tab(tab_name="loose")
        idx = a.tab_widget.count() - 1
        n = a.tab_widget.count()
        wins_before = {w for w in self.app.topLevelWidgets() if isinstance(w, self.mw_mod.MainWindow)}
        drop = QPoint(300, 300)
        a._finish_tab_drag(idx, drop, None)
        self.app.processEvents()
        new = [w for w in self.app.topLevelWidgets()
               if isinstance(w, self.mw_mod.MainWindow) and w not in wins_before]
        self.assertEqual(len(new), 1)
        self.assertEqual(a.tab_widget.count(), n - 1)
        self.assertEqual(new[0].tab_widget.tabText(0), "loose")
        self.assertFalse(new[0].isMaximized())
        self._assert_mappings_consistent(new[0])
        new[0]._force_closing = True
        from PyQt6.QtGui import QCloseEvent
        QApplication.sendEvent(new[0], QCloseEvent())
        new[0].deleteLater()

    def test_sole_tab_dropped_in_the_open_stays(self):
        b = self.win_b
        while b.tab_widget.count() > 1:
            b._close_tab(b.tab_widget.count() - 1, auto_create_new=False)
        def live():
            # 只数活着的：别的用例 deleteLater 的窗口可能在这次 processEvents 里才真正回收
            return {w for w in self.app.topLevelWidgets()
                    if isinstance(w, self.mw_mod.MainWindow) and not sip.isdeleted(w)
                    and w.isVisible() and not getattr(w, '_closing_in_progress', False)}
        self.app.processEvents()
        wins_before = live()
        b._finish_tab_drag(0, QPoint(50, 50), None)
        self.app.processEvents()
        self.assertEqual(b.tab_widget.count(), 1)
        self.assertEqual(live() - wins_before, set())

    def test_insert_index_follows_cursor_half(self):
        b = self.win_b
        bar = b.tab_widget.tabBar()
        self.assertGreaterEqual(bar.count(), 1)
        r0 = bar.tabRect(0)
        left = bar.mapToGlobal(QPoint(r0.left() + 2, r0.center().y()))
        right = bar.mapToGlobal(QPoint(r0.right() - 2, r0.center().y()))
        self.assertEqual(b._tab_insert_index_at(left), 0)
        self.assertEqual(b._tab_insert_index_at(right), 1)
        far = bar.mapToGlobal(QPoint(bar.width() + 500, r0.center().y()))
        self.assertIsNone(b._tab_insert_index_at(far))

    def test_moving_last_tab_closes_source_window(self):
        a = self.win_a
        c = self.mw_mod.MainWindow()
        c.move(0, 1800)
        c.show()
        self.app.processEvents()
        self.assertEqual(c.tab_widget.count(), 1)
        n_before = a.tab_widget.count()

        idx = a._adopt_tab_from(c, 0)

        self.assertGreaterEqual(idx, 0)
        self.assertEqual(a.tab_widget.count(), n_before + 1)
        self.assertEqual(c.tab_widget.count(), 0)
        # 关闭推迟到下一轮事件循环
        for _ in range(10):
            self.app.processEvents()
            if sip.isdeleted(c) or not c.isVisible():
                break
        self.assertTrue(sip.isdeleted(c) or not c.isVisible())
        self._assert_mappings_consistent(a)


if __name__ == '__main__':
    unittest.main()
