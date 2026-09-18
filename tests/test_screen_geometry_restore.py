"""窗口几何恢复要认所有显示器，不只主屏。

以前浮动导航窗 / 主窗口恢复几何时只拿 primaryScreen 的尺寸判"在不在屏幕内"，
放在左侧副屏（x 为负）或右侧副屏（x 大于主屏宽）的窗口每次启动都被拉回主屏。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_screen_geometry_restore.py -q
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QRect, QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


class _FakeScreen:
    def __init__(self, rect):
        self._rect = rect

    def availableGeometry(self):
        return self._rect


PRIMARY = _FakeScreen(QRect(0, 0, 1512, 950))
LEFT = _FakeScreen(QRect(-2560, -300, 2560, 1440))


def _screens(*screens):
    return mock.patch.object(QApplication, 'screens', staticmethod(lambda: list(screens)))


class RectVisibleHelper(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_rect_on_secondary_screen_counts_as_visible(self):
        from utils import rect_visible_on_any_screen
        with _screens(PRIMARY, LEFT):
            self.assertTrue(rect_visible_on_any_screen(-1500, 100, 400, 300))
            self.assertTrue(rect_visible_on_any_screen(100, 100, 400, 300))

    def test_rect_needs_real_overlap(self):
        from utils import rect_visible_on_any_screen
        with _screens(PRIMARY):
            # 左侧副屏不存在：整个在负坐标区 → 不可见
            self.assertFalse(rect_visible_on_any_screen(-1500, 100, 400, 300))
            # 只露 10px 的窄条也算不可见（用户根本抓不到它）
            self.assertFalse(rect_visible_on_any_screen(-390, 100, 400, 300))
            self.assertFalse(rect_visible_on_any_screen(100, 940, 400, 300))
            # 露出 40x40 以上就算可见
            self.assertTrue(rect_visible_on_any_screen(-350, 100, 400, 300))


class NavigatorGeometryRestore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _nav(self, geo, *screens):
        import main_window  # noqa: F401 — 先于 window_navigator 导入，打破循环 import
        import window_navigator
        with mock.patch.object(window_navigator.app_config, 'read_config',
                               return_value={'navigator_geometry': list(geo)}), \
                _screens(*screens):
            nav = window_navigator.WindowNavigatorPanel()
        self.addCleanup(nav.deleteLater)
        return nav

    def test_secondary_screen_geometry_is_restored(self):
        nav = self._nav([-1500, 100, 400, 300], PRIMARY, LEFT)
        g = nav.geometry()
        self.assertEqual((g.x(), g.y(), g.width(), g.height()), (-1500, 100, 400, 300))

    def test_offscreen_geometry_is_not_restored(self):
        nav = self._nav([-1500, 100, 400, 300], PRIMARY)
        self.assertNotEqual(nav.geometry().x(), -1500)


class MainWindowGeometryRestore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _win(self, geo, *screens):
        import app_config
        import main_window
        from PyQt6.QtGui import QCloseEvent
        with mock.patch.object(app_config, 'read_config',
                               return_value={'window_geometry': list(geo)}), \
                _screens(*screens):
            win = main_window.MainWindow()

        def _dispose():
            QApplication.sendEvent(win, QCloseEvent())
            win.deleteLater()
            for _ in range(3):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
        self.addCleanup(_dispose)
        return win

    def test_secondary_screen_geometry_is_restored(self):
        win = self._win([-1500, 100, 900, 600], PRIMARY, LEFT)
        self.assertEqual(win.geometry().x(), -1500)

    def test_offscreen_geometry_is_not_restored(self):
        win = self._win([-1500, 100, 900, 600], PRIMARY)
        self.assertNotEqual(win.geometry().x(), -1500)


if __name__ == '__main__':
    unittest.main()
