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
        self.assertIs(a._tab_drop_target_at(strip_b.center(), exclude=a), b)
        # 被拖的窗口自己不算目标
        self.assertIsNone(b._tab_drop_target_at(strip_b.center(), exclude=b))
        # 标签栏以外（窗口正中）不算
        mid = b.mapToGlobal(QPoint(b.width() // 2, b.height() // 2))
        self.assertIsNone(a._tab_drop_target_at(mid, exclude=a))

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


class TestShrinkMaximizedForDrag(unittest.TestCase):
    """拖最大化窗口的标签：先还原成普通窗口再跟手（最大化窗口动不了、挡住目标）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window
        cls.win = main_window.MainWindow()
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

    def test_normal_window_is_left_alone(self):
        w = self.win
        w.showNormal()
        self.app.processEvents()
        self.assertIsNone(w._shrink_for_tab_drag(w, QPoint(w.x() + 50, w.y() + 30)))

    def test_maximized_window_is_restored_under_cursor(self):
        w = self.win
        w.showMaximized()
        self.app.processEvents()
        self.assertTrue(w.isMaximized())
        old = w.frameGeometry()
        cursor = QPoint(old.x() + old.width() // 2, old.y() + 40)

        pos = w._shrink_for_tab_drag(w, cursor)

        self.assertIsNotNone(pos)
        self.app.processEvents()
        self.assertFalse(w.isMaximized())
        avail = QApplication.primaryScreen().availableGeometry()
        # 离屏的"屏幕"只有 800x600，比窗口最小尺寸还小：按公式断言而不是硬比屏幕
        want_w = max(w.minimumWidth(), int(avail.width() * w._DRAG_SHRINK_RATIO))
        self.assertEqual(w.width(), want_w)
        # 光标仍落在窗口内、离顶部还是原来那么远（标签栏那一行）
        x, y = pos
        self.assertLessEqual(x, cursor.x())
        self.assertGreaterEqual(x + w.width(), cursor.x())
        self.assertEqual(cursor.y() - y, 40)
