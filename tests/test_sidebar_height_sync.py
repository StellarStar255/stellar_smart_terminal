# -*- coding: utf-8 -*-
"""侧栏高度跨窗口联动（侧栏底部「各窗口联动高度」勾选框）的测试。

需求：多窗口下嵌入式导航列表的高度（侧栏里那根可拖分隔条）每个窗口各自
独立，切换窗口后要逐个重新拖拽。新增勾选框开启联动：
- 勾选即刻把本窗口当前高度对齐到所有窗口，且所有窗口的勾选框状态同步；
- 联动开启时拖动任一窗口的分隔条，其它窗口经节流广播跟随；
- 默认关闭，关闭时拖动不影响其它窗口。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_sidebar_height_sync.py -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication


class TestSidebarHeightSync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw_mod = main_window
        # 两个窗口共用整个测试类：反复建销 MainWindow 在 CI 上会段错误
        cls.win_a = main_window.MainWindow()
        cls.win_b = main_window.MainWindow()

    @classmethod
    def tearDownClass(cls):
        for w in (cls.win_a, cls.win_b):
            w.close()
            w.deleteLater()
        del cls.win_a, cls.win_b
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def setUp(self):
        # 每个用例从「联动关闭、无共享值」的干净状态出发
        MW = self.mw_mod.MainWindow
        MW._sidebar_height_sync = False
        MW._shared_nav_list_height = None
        for w in (self.win_a, self.win_b):
            cb = w.sidebar_sync_checkbox
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
            w._nav_height_broadcast_pending = None
            w._nav_height_broadcast_timer.stop()

    def _spin(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

    def test_default_off_drag_does_not_affect_other_window(self):
        a, b = self.win_a, self.win_b
        a.nav_panel.set_embedded_list_height(200)
        b.nav_panel.set_embedded_list_height(260)
        a._on_nav_resize_drag(-50)
        self._spin(0.2)  # 即便有残留 timer 也给它机会触发
        self.assertEqual(b.nav_panel.embedded_list_height(), 260,
                         "联动未开启时，拖动 A 不应影响 B")

    def test_toggle_on_aligns_all_windows_and_checkboxes(self):
        a, b = self.win_a, self.win_b
        a.nav_panel.set_embedded_list_height(220)
        b.nav_panel.set_embedded_list_height(300)
        a.sidebar_sync_checkbox.setChecked(True)  # 真实用户路径：触发 toggled
        self.assertTrue(self.mw_mod.MainWindow._sidebar_height_sync)
        self.assertEqual(b.nav_panel.embedded_list_height(), 220,
                         "勾选后应立即以本窗口高度对齐其它窗口")
        self.assertTrue(b.sidebar_sync_checkbox.isChecked(),
                        "勾选状态应联动到其它窗口的勾选框")
        self.assertEqual(self.mw_mod.MainWindow._shared_nav_list_height, 220)

    def test_drag_broadcasts_to_other_window_when_on(self):
        a, b = self.win_a, self.win_b
        self.mw_mod.MainWindow._sidebar_height_sync = True
        a.nav_panel.set_embedded_list_height(200)
        b.nav_panel.set_embedded_list_height(200)
        a._on_nav_resize_drag(-80)
        expected = a.nav_panel.embedded_list_height()
        self.assertNotEqual(expected, 200, "拖动应改变 A 自身高度（前置条件）")
        # 广播走 80ms 节流 timer：带截止时间地等 B 跟上
        deadline = time.monotonic() + 2.0
        while (b.nav_panel.embedded_list_height() != expected
               and time.monotonic() < deadline):
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(b.nav_panel.embedded_list_height(), expected,
                         "联动开启时，拖动 A 后 B 应跟随到相同高度")
        # 双方的落盘记忆也应一致（重启后仍对齐）
        self.assertEqual(a._saved_nav_list_height, expected)
        self.assertEqual(b._saved_nav_list_height, expected)

    def test_toggle_off_stops_syncing(self):
        a, b = self.win_a, self.win_b
        a.sidebar_sync_checkbox.setChecked(True)
        a.sidebar_sync_checkbox.setChecked(False)
        self.assertFalse(self.mw_mod.MainWindow._sidebar_height_sync)
        self.assertFalse(b.sidebar_sync_checkbox.isChecked())
        a.nav_panel.set_embedded_list_height(240)
        b.nav_panel.set_embedded_list_height(320)
        a._on_nav_resize_drag(-60)
        self._spin(0.2)
        self.assertEqual(b.nav_panel.embedded_list_height(), 320,
                         "取消勾选后拖动不应再联动")


if __name__ == '__main__':
    unittest.main()
