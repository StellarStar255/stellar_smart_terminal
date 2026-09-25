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
        # 别的窗口页面正中（不靠边、不在标签栏）：并成它的新标签
        page_b = b._tab_page_rect()
        self.assertEqual(a._tab_drop_hit_at(page_b.center()), (b, 'page', None))
        # 自己窗口的页面正中：什么都不发生
        page_a = a._tab_page_rect()
        self.assertIsNone(a._tab_drop_hit_at(page_a.center(), dragging_index=None))

    def test_finish_drag_on_page_of_other_window_appends_tab(self):
        a, b = self.win_a, self.win_b
        a._add_new_tab(tab_name="into-body")
        idx = a.tab_widget.count() - 1
        b_before = b.tab_widget.count()
        a._finish_tab_drag(idx, b._tab_page_rect().center(), (b, 'page', None))
        self.assertEqual(b.tab_widget.count(), b_before + 1)
        self.assertEqual(b.tab_widget.tabText(b.tab_widget.count() - 1), "into-body")
        self._assert_mappings_consistent(a)
        self._assert_mappings_consistent(b)

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

    def test_sole_tab_dropped_in_the_open_moves_whole_window(self):
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
        before = b.frameGeometry()
        drop = QPoint(700, 400)
        self.assertFalse(before.contains(drop))
        b._finish_tab_drag(0, drop, None)
        self.app.processEvents()
        self.assertEqual(b.tab_widget.count(), 1)
        self.assertEqual(live() - wins_before, set())   # 不拆新窗口
        # 唯一的标签 = 整个窗口：窗口搬到松手处（松手点落在新位置的窗口里）
        self.assertNotEqual(b.frameGeometry().topLeft(), before.topLeft())
        self.assertTrue(b.frameGeometry().contains(drop), (b.frameGeometry(), drop))
        b.move(before.topLeft())

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

    def test_drop_hint_shows_insert_caret_on_strip_and_label_on_page(self):
        """落点高亮：插在标签之间 → 细插入线（落在标签边界）；追加末尾 → 标签占位；
        内容区 → 带说明的圆角区域。"""
        from PyQt6.QtCore import Qt
        from widgets import DropHintOverlay
        from i18n import t
        b = self.win_b
        while b.tab_widget.count() < 2:
            b._add_new_tab(tab_name=f"h{b.tab_widget.count()}")
        bar = b.tab_widget.tabBar()
        r1 = bar.tabRect(1)
        pos = bar.mapToGlobal(QPoint(r1.left() + 3, r1.center().y()))   # 插到第 1 个前面
        b._show_tab_drop_hint('strip', None, pos)
        hint = b._tab_drop_hint
        self.assertIsInstance(hint, DropHintOverlay)
        self.assertEqual(hint._mode, 'strip')
        boundary = hint.mapFromGlobal(bar.mapToGlobal(QPoint(r1.left(), 0))).x()
        self.assertEqual(hint._caret_x, boundary)
        self.assertIsNone(hint._slot)
        # 光标移到最后一个标签右侧空白 → 不画插入线，改画末尾「标签占位」，写着被拖标签名
        far = bar.mapToGlobal(QPoint(bar.width() - 2, r1.center().y()))
        b._update_tab_drop_caret(far, "dragged-tab")
        last = bar.tabRect(bar.count() - 1)
        self.assertIsNone(hint._caret_x)
        self.assertIsNotNone(hint._slot)
        self.assertEqual(hint._slot_title, "dragged-tab")
        last_right = hint.mapFromGlobal(bar.mapToGlobal(QPoint(last.right(), 0))).x()
        self.assertGreater(hint._slot.left(), last_right)          # 紧跟在最后一个标签后
        self.assertLess(hint._slot.left(), last_right + 10)
        self.assertLessEqual(hint._slot.right(), hint.width())
        # 占位与真标签同宽：拖的标签若与最后一个同名，宽度正好等于它（扣 margin-right）
        b._update_tab_drop_caret(far, bar.tabText(bar.count() - 1))
        self.assertEqual(round(hint._slot.width()), last.width() - 2)
        b._show_tab_drop_hint('page')
        self.assertEqual((hint._mode, hint._label), ('page', t("tab.drop_as_new_tab")))
        self.assertIsNone(hint._caret_x)
        b._show_tab_drop_hint('split', (Qt.Orientation.Horizontal, False))
        self.assertEqual(hint._mode, 'area')
        self.assertFalse(hint.grab().isNull())
        b._hide_tab_drop_hint()

    def test_horizontal_drag_uses_shadow_drag_not_qt_movable(self):
        """左右拖也进影子拖拽：Qt 自带的可移动标签会截一张不带 × 的残缺标签图。"""
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        a = self.win_a
        bar = a.tab_widget.tabBar()
        self.assertFalse(a.tab_widget.isMovable())
        got = []
        bar.tab_detach_requested.connect(lambda i, p: got.append(i))
        r0 = bar.tabRect(0)
        start = QPointF(r0.center())

        def ev(kind, pt, buttons):
            return QMouseEvent(kind, pt, bar.mapToGlobal(pt), Qt.MouseButton.LeftButton,
                               buttons, Qt.KeyboardModifier.NoModifier)
        bar.mousePressEvent(ev(QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton))
        step = QApplication.startDragDistance() + 2
        bar.mouseMoveEvent(ev(QEvent.Type.MouseMove, start + QPointF(step, 0),
                              Qt.MouseButton.LeftButton))
        bar.tab_detach_requested.disconnect()
        bar.tab_detach_requested.connect(a._begin_tab_drag)
        self.assertEqual(got, [0])

    def test_reorder_on_own_strip_keeps_page_and_restores_after(self):
        """在本窗口标签栏上左右排序：页面区不切走（不闪）；离开标签栏才切到邻页；
        松手重排后页面区回到被拖的那页。"""
        from unittest import mock
        from PyQt6.QtCore import Qt
        import main_window_tabs as mwt
        a = self.win_a
        while a.tab_widget.count() < 3:
            a._add_new_tab(tab_name=f"r{a.tab_widget.count()}")
        last = a.tab_widget.count() - 1
        a.tab_widget.setCurrentIndex(last)
        dragged = a.tab_widget.widget(last)
        bar = a.tab_widget.tabBar()
        r0 = bar.tabRect(0)
        on_strip = bar.mapToGlobal(QPoint(r0.left() + 3, r0.center().y()))   # 插到最前
        on_page = a._tab_page_rect().center()
        cursor = {'pos': on_strip, 'btn': Qt.MouseButton.LeftButton}
        with mock.patch.object(mwt.QCursor, 'pos', side_effect=lambda: cursor['pos']), \
                mock.patch.object(mwt.QApplication, 'mouseButtons',
                                  side_effect=lambda: cursor['btn']), \
                mock.patch.object(mwt.QApplication, 'topLevelAt', return_value=a):
            # 离屏下前面用例留下的窗口可能叠在同一位置；真实屏幕上光标下最上层就是 a
            a._begin_tab_drag(last, on_strip)
            timer = a._tab_drag_timer
            timer.timeout.emit()
            self.assertIs(a.tab_widget.currentWidget(), dragged)       # 标签栏上：不切页
            cursor['pos'] = on_page
            timer.timeout.emit()
            self.assertIsNot(a.tab_widget.currentWidget(), dragged)    # 离开标签栏：切到邻页
            cursor['pos'] = on_strip
            timer.timeout.emit()
            cursor['btn'] = Qt.MouseButton.NoButton
            timer.timeout.emit()                                       # 松手
        self.assertFalse(timer.isActive())
        self.assertIs(a.tab_widget.widget(0), dragged)                 # 排到了最前
        self.assertIs(a.tab_widget.currentWidget(), dragged)           # 页面回到它
        self._assert_mappings_consistent(a)


if __name__ == '__main__':
    unittest.main()
