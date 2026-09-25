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


if __name__ == '__main__':
    unittest.main()
