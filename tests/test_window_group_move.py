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

    def test_cancel_snaps_windows_back(self):
        from PyQt6.QtCore import QPoint
        from window_group_move import GroupMoveSession
        a, b = self._make_windows()
        pa, pb = a.pos(), b.pos()
        s = GroupMoveSession([a, b], QPoint(0, 0))
        s.drag_to(QPoint(300, 200))
        s.cancel()
        self.assertEqual((a.pos(), b.pos()), (pa, pb))

    def test_grip_esc_mid_drag_restores_and_ignores_release(self):
        from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
        from PyQt6.QtGui import QKeyEvent, QMouseEvent
        from window_group_move import GroupMoveGrip
        a, b = self._make_windows()
        pa, pb = a.pos(), b.pos()
        grip = GroupMoveGrip(lambda: [a, b])
        self.addCleanup(grip.deleteLater)

        def mouse(kind, gx, gy, buttons=Qt.MouseButton.LeftButton):
            return QMouseEvent(kind, QPointF(1, 1), QPointF(gx, gy),
                               Qt.MouseButton.LeftButton, buttons,
                               Qt.KeyboardModifier.NoModifier)
        grip.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, 10, 10))
        grip.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 310, 210))
        self.assertEqual(a.pos(), pa + QPoint(300, 200))
        grip.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                     Qt.KeyboardModifier.NoModifier))
        self.assertEqual((a.pos(), b.pos()), (pa, pb))
        # 取消后手还按着继续拖、再松手：都不应再动窗口
        grip.mouseMoveEvent(mouse(QEvent.Type.MouseMove, 500, 400))
        grip.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, 500, 400,
                                     Qt.MouseButton.NoButton))
        self.assertEqual((a.pos(), b.pos()), (pa, pb))


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

    def test_item_dropped_on_same_screen_moves_window_to_drop_point(self):
        from PyQt6.QtCore import QPoint, QRect
        import window_group_move as gm
        import window_navigator as wn
        nav, w = self._nav_with_window()
        scr = _FakeScreen(QRect(0, 0, 1000, 800))
        size = w.size()
        with mock.patch.object(gm, '_screen_of', return_value=scr), \
                mock.patch.object(wn.QApplication, 'screenAt', return_value=scr):
            nav._on_item_dropped_outside(id(w), QPoint(700, 300))
        # 同屏：尺寸不变，顶边中点落到松手点（再收回屏内）
        self.assertEqual(w.size(), size)
        self.assertEqual(w.geometry(), QRect(550, 290, 300, 200))

    def test_item_dropped_inside_own_window_does_nothing(self):
        from PyQt6.QtCore import QRect
        import window_group_move as gm
        import window_navigator as wn
        nav, w = self._nav_with_window()
        nav.show()
        self.app.processEvents()
        scr = _FakeScreen(QRect(0, 0, 4000, 4000))
        before = w.geometry()
        with mock.patch.object(gm, '_screen_of', return_value=scr), \
                mock.patch.object(wn.QApplication, 'screenAt', return_value=scr):
            nav._on_item_dropped_outside(id(w), nav.frameGeometry().center())
        self.assertEqual(w.geometry(), before)

    # ---- 自绘拖拽：用真实鼠标/按键事件驱动 NavListWidget ----
    def _drag_list(self, ids=(42, 43, 44)):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QListWidgetItem
        import main_window  # noqa: F401
        import window_navigator as wn
        lw = wn.NavListWidget()
        self.addCleanup(lw.deleteLater)
        lw.setDragEnabled(True)
        lw.setDragDropMode(wn.QListWidget.DragDropMode.InternalMove)
        lw.resize(200, 200)
        for i, wid in enumerate(ids):
            it = QListWidgetItem(f'item{i}')
            it.setData(Qt.ItemDataRole.UserRole, wid)
            lw.addItem(it)
        lw.show()
        self.app.processEvents()
        return lw

    def _mouse(self, lw, kind, gpos, buttons=None):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        if buttons is None:
            buttons = Qt.MouseButton.LeftButton
        local = lw.viewport().mapFromGlobal(gpos)
        return QMouseEvent(kind, QPointF(local), QPointF(gpos), Qt.MouseButton.LeftButton,
                           buttons, Qt.KeyboardModifier.NoModifier)

    def _row_center(self, lw, row):
        return lw.viewport().mapToGlobal(lw.visualItemRect(lw.item(row)).center())

    def _start(self, lw, row):
        from PyQt6.QtCore import Qt
        lw.setCurrentRow(row)
        lw.startDrag(Qt.DropAction.MoveAction)
        self.assertTrue(lw.is_dragging())
        self.assertIsNotNone(lw._ghost)

    def test_release_outside_emits_drop_immediately(self):
        from PyQt6.QtCore import QEvent, QPoint, Qt
        lw = self._drag_list()
        got = []
        lw.dropped_outside.connect(lambda wid, p: got.append((wid, p)))
        self._start(lw, 1)
        outside = lw.mapToGlobal(QPoint(0, 0)) + QPoint(900, 900)
        lw.mouseMoveEvent(self._mouse(lw, QEvent.Type.MouseMove, outside))
        lw.mouseReleaseEvent(self._mouse(lw, QEvent.Type.MouseButtonRelease, outside,
                                         Qt.MouseButton.NoButton))
        self.assertEqual(got, [(43, outside)])
        self.assertFalse(lw.is_dragging())
        self.assertIsNone(lw._ghost)  # 拖影当场消失，不飞回

    def test_release_inside_list_reorders_and_does_not_emit(self):
        from PyQt6.QtCore import QEvent, Qt
        lw = self._drag_list()
        got, moved = [], []
        lw.dropped_outside.connect(lambda *a: got.append(a))
        lw.model().rowsMoved.connect(lambda *a: moved.append(1))
        self._start(lw, 0)
        r = lw.visualItemRect(lw.item(2))
        below_last = lw.viewport().mapToGlobal(r.center() + type(r.center())(0, r.height() // 4))
        lw.mouseMoveEvent(self._mouse(lw, QEvent.Type.MouseMove, below_last))
        lw.mouseReleaseEvent(self._mouse(lw, QEvent.Type.MouseButtonRelease, below_last,
                                         Qt.MouseButton.NoButton))
        texts = [lw.item(i).text() for i in range(lw.count())]
        self.assertEqual(got, [])
        self.assertEqual(moved, [1])  # 面板靠 rowsMoved 切到手动排序
        self.assertEqual(texts[-1], 'item0')

    def test_esc_cancels_drag_and_release_moves_nothing(self):
        from PyQt6.QtCore import QEvent, QPoint, Qt
        from PyQt6.QtGui import QKeyEvent
        lw = self._drag_list()
        got, moved = [], []
        lw.dropped_outside.connect(lambda *a: got.append(a))
        lw.model().rowsMoved.connect(lambda *a: moved.append(1))
        self._start(lw, 1)
        outside = lw.mapToGlobal(QPoint(0, 0)) + QPoint(900, 900)
        lw.mouseMoveEvent(self._mouse(lw, QEvent.Type.MouseMove, outside))
        lw.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                   Qt.KeyboardModifier.NoModifier))
        self.assertFalse(lw.is_dragging())
        self.assertIsNone(lw._ghost)
        lw.mouseReleaseEvent(self._mouse(lw, QEvent.Type.MouseButtonRelease, outside,
                                         Qt.MouseButton.NoButton))
        self.assertEqual((got, moved), ([], []))

    def test_esc_reaches_list_through_window_while_focus_elsewhere(self):
        """真实分发路径：焦点在别的控件上，Esc 发给窗口也要被拖拽中的列表收到。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QLineEdit, QVBoxLayout, QWidget
        import window_navigator as wn
        host = QWidget()
        self.addCleanup(host.deleteLater)
        lay = QVBoxLayout(host)
        edit = QLineEdit()
        lw = wn.NavListWidget()
        lay.addWidget(edit)
        lay.addWidget(lw)
        lw.addItem('x')
        host.show()
        edit.setFocus()
        self.app.processEvents()
        lw.setCurrentRow(0)
        lw.startDrag(Qt.DropAction.MoveAction)
        QTest.keyClick(host.windowHandle(), Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(lw.is_dragging())
        self.assertEqual(edit.text(), '')

    def test_drop_signal_keeps_64bit_window_id_end_to_end(self):
        """真实 id(window) 远超 32 位：信号传到面板后必须还能查到窗口并搬走它。
        （回归：信号声明成 int 时 id 被截断，面板查不到窗口、拖出静默无效。）"""
        from PyQt6.QtCore import QEvent, QPoint, QRect, Qt
        from PyQt6.QtWidgets import QListWidgetItem
        import window_group_move as gm
        import window_navigator as wn
        nav, w = self._nav_with_window()
        self.assertGreater(id(w), 2 ** 32)  # 64 位 macOS/Linux 上的真实情形
        lw = nav.window_list
        it = QListWidgetItem('x')
        it.setData(Qt.ItemDataRole.UserRole, id(w))
        lw.addItem(it)
        lw.setCurrentItem(it)
        lw.startDrag(Qt.DropAction.MoveAction)
        outside = nav.frameGeometry().bottomRight() + QPoint(500, 500)
        src = _FakeScreen(QRect(0, 0, 1000, 800))
        dst = _FakeScreen(QRect(1000, 0, 4000, 4000))
        with mock.patch.object(gm, '_screen_of', return_value=src), \
                mock.patch.object(wn.QApplication, 'screenAt', return_value=dst):
            lw.mouseReleaseEvent(self._mouse(lw, QEvent.Type.MouseButtonRelease, outside,
                                             Qt.MouseButton.NoButton))
        self.assertTrue(QRect(1000, 0, 4000, 4000).contains(w.geometry()), w.geometry())

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
