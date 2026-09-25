"""导航面板「整组搬家」把手：一次拖动列表里的全部窗口。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_window_group_move.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeScreen:
    def __init__(self, rect):
        self._rect = rect

    def availableGeometry(self):
        return self._rect


class TestMapRect(unittest.TestCase):
    def test_small_to_large_scales_and_keeps_relative_layout(self):
        from PyQt6.QtCore import QRect
        from window_group_move import map_rect_between
        src = QRect(0, 0, 1000, 800)
        dst = QRect(2000, 100, 2000, 1600)
        # 左半屏窗口 → 新屏左半屏
        self.assertEqual(map_rect_between(QRect(0, 0, 500, 800), src, dst),
                         QRect(2000, 100, 1000, 1600))
        # 右下 1/4 → 新屏右下 1/4
        self.assertEqual(map_rect_between(QRect(500, 400, 500, 400), src, dst),
                         QRect(3000, 900, 1000, 800))

    def test_result_is_clamped_inside_destination(self):
        from PyQt6.QtCore import QRect
        from window_group_move import map_rect_between
        src = QRect(0, 0, 1000, 800)
        dst = QRect(0, 0, 1000, 800)
        r = map_rect_between(QRect(900, 700, 400, 300), src, dst)
        self.assertTrue(dst.contains(r), r)


class TestGroupMoveSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _make_windows(self):
        from PyQt6.QtWidgets import QWidget
        a, b = QWidget(), QWidget()
        a.setGeometry(100, 100, 300, 200)
        b.setGeometry(500, 150, 300, 200)
        a.show()
        b.show()
        self.app.processEvents()
        self.addCleanup(a.deleteLater)
        self.addCleanup(b.deleteLater)
        return a, b

    def test_drag_moves_every_window_by_same_delta(self):
        from PyQt6.QtCore import QPoint
        from window_group_move import GroupMoveSession
        a, b = self._make_windows()
        pa, pb = a.pos(), b.pos()
        s = GroupMoveSession([a, b, a], QPoint(10, 10))  # 重复项只算一次
        s.drag_to(QPoint(60, 40))
        self.assertEqual(a.pos(), pa + QPoint(50, 30))
        self.assertEqual(b.pos(), pb + QPoint(50, 30))

    def test_drop_on_other_screen_relayouts_proportionally(self):
        from PyQt6.QtCore import QPoint, QRect
        import window_group_move as gm
        a, b = self._make_windows()
        src = _FakeScreen(QRect(0, 0, 1000, 800))
        dst = _FakeScreen(QRect(1000, 0, 2000, 1600))
        with mock.patch.object(gm, '_screen_of', return_value=src):
            s = gm.GroupMoveSession([a, b], QPoint(10, 10))
        s.drag_to(QPoint(1200, 10))
        with mock.patch.object(gm.QApplication, 'screenAt', return_value=dst):
            s.finish(QPoint(1200, 10))
        self.assertEqual(a.geometry(), QRect(1200, 200, 600, 400))
        self.assertEqual(b.geometry(), QRect(2000, 300, 600, 400))

    def test_drop_on_same_screen_keeps_translation(self):
        from PyQt6.QtCore import QPoint, QRect
        import window_group_move as gm
        a, b = self._make_windows()
        scr = _FakeScreen(QRect(0, 0, 1000, 800))
        with mock.patch.object(gm, '_screen_of', return_value=scr):
            s = gm.GroupMoveSession([a, b], QPoint(0, 0))
        s.drag_to(QPoint(20, 20))
        pa = a.pos()
        with mock.patch.object(gm.QApplication, 'screenAt', return_value=scr):
            s.finish(QPoint(20, 20))
        self.assertEqual(a.pos(), pa)

    def test_relayout_with_anchor_drops_window_at_cursor(self):
        from PyQt6.QtCore import QPoint, QRect
        import window_group_move as gm
        a, _b = self._make_windows()
        src = _FakeScreen(QRect(0, 0, 1000, 800))
        dst = _FakeScreen(QRect(1000, 0, 2000, 1600))
        with mock.patch.object(gm, '_screen_of', return_value=src):
            n = gm.move_windows_to_screen([a], dst, anchor=QPoint(2000, 500))
        self.assertEqual(n, 1)
        # 尺寸等比放大，顶边中点对准松手点
        self.assertEqual(a.geometry(), QRect(1700, 490, 600, 400))


class TestDragCancelled(unittest.TestCase):
    """拖到应用外松手 vs 按 Esc 取消：Qt 都报 IgnoreAction，靠物理输入状态区分。"""

    def _check(self, platform, fn, state):
        import window_group_move as gm
        with mock.patch.object(gm.sys, 'platform', platform), \
                mock.patch.object(gm, fn, return_value=state):
            return gm.drag_cancelled()

    def test_released_button_is_a_real_drop(self):
        self.assertFalse(self._check('darwin', '_mac_input_state', (False, False)))
        self.assertFalse(self._check('win32', '_win_input_state', (False, False)))

    def test_button_still_held_means_esc_cancel(self):
        self.assertTrue(self._check('darwin', '_mac_input_state', (True, False)))
        self.assertTrue(self._check('win32', '_win_input_state', (True, False)))

    def test_esc_down_means_cancel(self):
        self.assertTrue(self._check('darwin', '_mac_input_state', (False, True)))
        self.assertTrue(self._check('win32', '_win_input_state', (False, True)))

    def test_state_query_failure_falls_back_to_drop(self):
        import window_group_move as gm
        with mock.patch.object(gm.sys, 'platform', 'darwin'), \
                mock.patch.object(gm, '_mac_input_state', side_effect=OSError):
            self.assertFalse(gm.drag_cancelled())

    @unittest.skipUnless(sys.platform == 'darwin', 'CoreGraphics only on macOS')
    def test_mac_query_really_works(self):
        import window_group_move as gm
        btn, esc = gm._mac_input_state()
        self.assertIsInstance(btn, bool)
        self.assertIsInstance(esc, bool)


class TestNavigatorGrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_grip_targets_listed_windows_and_floating_panel(self):
        from PyQt6.QtWidgets import QWidget
        import main_window  # noqa: F401  先载 main_window，走正常的 import 环顺序
        from window_navigator import WindowNavigatorPanel
        from window_group_move import GroupMoveGrip
        nav = WindowNavigatorPanel()
        self.addCleanup(nav.deleteLater)
        self.assertIsInstance(nav.group_move_grip, GroupMoveGrip)
        fake = QWidget()
        self.addCleanup(fake.deleteLater)
        with mock.patch.object(nav, '_resolve_window', return_value=fake):
            nav.window_list.addItem('x')
            wins = nav._group_move_windows()
        self.assertIn(fake, wins)
        self.assertIn(nav, wins)  # 浮动面板自己也跟着走

    def _nav_with_window(self):
        from PyQt6.QtCore import QRect
        from PyQt6.QtWidgets import QWidget
        import main_window  # noqa: F401
        from window_navigator import WindowNavigatorPanel
        import weakref
        nav = WindowNavigatorPanel()
        self.addCleanup(nav.deleteLater)
        w = QWidget()
        w.force_close_with_save = lambda: None
        w.setGeometry(QRect(100, 100, 300, 200))
        w.show()
        self.addCleanup(w.deleteLater)
        nav._window_refs = {id(w): weakref.ref(w)}
        return nav, w

    def test_item_dropped_on_other_screen_moves_that_window(self):
        from PyQt6.QtCore import QPoint, QRect
        import window_group_move as gm
        import window_navigator as wn
        nav, w = self._nav_with_window()
        src = _FakeScreen(QRect(0, 0, 1000, 800))
        dst = _FakeScreen(QRect(1000, 0, 1000, 800))
        with mock.patch.object(gm, '_screen_of', return_value=src), \
                mock.patch.object(wn.QApplication, 'screenAt', return_value=dst):
            nav._on_item_dropped_outside(id(w), QPoint(1500, 300))
        self.assertTrue(QRect(1000, 0, 1000, 800).contains(w.geometry()), w.geometry())

    def test_item_dropped_on_same_screen_does_nothing(self):
        from PyQt6.QtCore import QPoint, QRect
        import window_group_move as gm
        import window_navigator as wn
        nav, w = self._nav_with_window()
        scr = _FakeScreen(QRect(0, 0, 1000, 800))
        before = w.geometry()
        with mock.patch.object(gm, '_screen_of', return_value=scr), \
                mock.patch.object(wn.QApplication, 'screenAt', return_value=scr):
            nav._on_item_dropped_outside(id(w), QPoint(700, 300))
        self.assertEqual(w.geometry(), before)

    def test_list_emits_dropped_outside_only_when_released_outside(self):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem
        import main_window  # noqa: F401
        import window_navigator as wn
        lw = wn.NavListWidget()
        self.addCleanup(lw.deleteLater)
        lw.resize(200, 200)
        lw.show()
        it = QListWidgetItem('x')
        it.setData(Qt.ItemDataRole.UserRole, 42)
        lw.addItem(it)
        lw.setCurrentItem(it)
        got = []
        lw.dropped_outside.connect(lambda wid, p: got.append(wid))
        inside = lw.viewport().mapToGlobal(lw.viewport().rect().center())
        outside = lw.viewport().mapToGlobal(lw.viewport().rect().bottomRight()) + QPoint(500, 500)
        with mock.patch.object(QListWidget, 'startDrag'), \
                mock.patch.object(wn, 'drag_cancelled', return_value=False):
            with mock.patch.object(wn.QCursor, 'pos', return_value=inside):
                lw.startDrag(Qt.DropAction.MoveAction)
            self.assertEqual(got, [])  # 列表内松手 = 排序，不搬窗口
            with mock.patch.object(wn.QCursor, 'pos', return_value=outside):
                lw.startDrag(Qt.DropAction.MoveAction)
        self.assertEqual(got, [42])

        # 按 Esc 取消：光标虽在列表外（甚至在别的屏上），也不算松手
        got.clear()
        with mock.patch.object(QListWidget, 'startDrag'), \
                mock.patch.object(wn, 'drag_cancelled', return_value=True), \
                mock.patch.object(wn.QCursor, 'pos', return_value=outside):
            lw.startDrag(Qt.DropAction.MoveAction)
        self.assertEqual(got, [])

    def test_context_menu_lists_displays(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu
        nav, w = self._nav_with_window()
        nav.window_list.addItem('x')
        menus = []

        def fake_exec(menu, *a, **k):
            menus.append([act.text() for act in menu.actions()])
            return None
        with mock.patch.object(nav, '_resolve_window', return_value=w), \
                mock.patch.object(QMenu, 'exec', new=fake_exec):
            rect = nav.window_list.visualItemRect(nav.window_list.item(0))
            nav._show_window_context_menu(rect.center() if rect.isValid() else QPoint(5, 5))
        from i18n import t
        self.assertTrue(menus and t("window.move_to_display") in menus[0], menus)


if __name__ == '__main__':
    unittest.main()
