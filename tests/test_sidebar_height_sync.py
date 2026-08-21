# -*- coding: utf-8 -*-
"""侧栏跨窗口联动（高度勾选框 + 左侧栏宽度）的测试。

需求：
1. 侧栏底部「各窗口联动高度」勾选框控制嵌入式导航列表高度（侧栏里那根可拖
   分隔条）是否跨窗口联动：勾选即刻对齐、拖动经节流广播跟随、默认关闭。
2. 窗口分散在不同显示器上时任何联动都不生效（高度和左侧栏宽度都是）：
   跨屏窗口的尺寸语境不同（分辨率/缩放各异），强行对齐没有意义。
   共享值按屏幕分桶（QScreen.name()），None 桶存磁盘播种的兜底值。

offscreen 平台只有一块虚拟屏，跨屏场景用实例级遮蔽 _screen_key 模拟。

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


class _SyncBase(unittest.TestCase):
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
        # 每个用例从「联动关闭、无共享值、同一屏幕」的干净状态出发
        MW = self.mw_mod.MainWindow
        MW._sidebar_height_sync = False
        MW._nav_height_by_screen.clear()
        MW._left_width_by_screen.clear()
        for w in (self.win_a, self.win_b):
            cb = w.sidebar_sync_checkbox
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
            w._nav_height_broadcast_pending = None
            w._nav_height_broadcast_timer.stop()
            # 清掉用例里遮蔽的假屏幕标识
            for attr in ('_screen_key', 'isVisible', '_apply_shared_left_panel_width'):
                try:
                    delattr(w, attr)
                except AttributeError:
                    pass

    def _spin(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

    def _wait_until(self, cond, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not cond() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        return cond()


class TestSidebarHeightSync(_SyncBase):
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
                         "勾选后应立即以本窗口高度对齐同屏的其它窗口")
        self.assertTrue(b.sidebar_sync_checkbox.isChecked(),
                        "勾选状态应联动到其它窗口的勾选框")
        self.assertEqual(
            self.mw_mod.MainWindow._nav_height_by_screen.get(a._screen_key()),
            220)

    def test_drag_broadcasts_to_other_window_when_on(self):
        a, b = self.win_a, self.win_b
        self.mw_mod.MainWindow._sidebar_height_sync = True
        a.nav_panel.set_embedded_list_height(200)
        b.nav_panel.set_embedded_list_height(200)
        a._on_nav_resize_drag(-80)
        expected = a.nav_panel.embedded_list_height()
        self.assertNotEqual(expected, 200, "拖动应改变 A 自身高度（前置条件）")
        # 广播走 80ms 节流 timer：带截止时间地等 B 跟上
        self.assertTrue(self._wait_until(
            lambda: b.nav_panel.embedded_list_height() == expected),
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


class TestCrossScreenNoSync(_SyncBase):
    """窗口分散在不同显示器上时，高度/宽度联动一律不生效。"""

    def _put_on_screens(self, key_a, key_b):
        self.win_a._screen_key = lambda: key_a
        self.win_b._screen_key = lambda: key_b

    def test_height_not_synced_across_screens(self):
        a, b = self.win_a, self.win_b
        self.mw_mod.MainWindow._sidebar_height_sync = True
        self._put_on_screens('screen-1', 'screen-2')
        a.nav_panel.set_embedded_list_height(200)
        b.nav_panel.set_embedded_list_height(280)
        a._on_nav_resize_drag(-80)
        self.assertNotEqual(a.nav_panel.embedded_list_height(), 200)
        self._spin(0.3)  # 给节流 timer 充分触发机会
        self.assertEqual(b.nav_panel.embedded_list_height(), 280,
                         "异屏窗口即使联动开启也不应跟随")

    def test_height_toggle_on_does_not_align_other_screen(self):
        a, b = self.win_a, self.win_b
        self._put_on_screens('screen-1', 'screen-2')
        a.nav_panel.set_embedded_list_height(220)
        b.nav_panel.set_embedded_list_height(300)
        a.sidebar_sync_checkbox.setChecked(True)
        self.assertTrue(b.sidebar_sync_checkbox.isChecked(),
                        "开关状态本身仍全局同步（它是全局设置）")
        self.assertEqual(b.nav_panel.embedded_list_height(), 300,
                         "勾选联动不应把高度强加给异屏窗口")

    def test_height_synced_on_same_screen_key(self):
        a, b = self.win_a, self.win_b
        self.mw_mod.MainWindow._sidebar_height_sync = True
        self._put_on_screens('screen-1', 'screen-1')
        a.nav_panel.set_embedded_list_height(200)
        b.nav_panel.set_embedded_list_height(280)
        a._on_nav_resize_drag(-80)
        expected = a.nav_panel.embedded_list_height()
        self.assertTrue(self._wait_until(
            lambda: b.nav_panel.embedded_list_height() == expected),
            "同屏窗口仍应正常联动（遮蔽 _screen_key 后的对照组）")

    def test_width_broadcast_skips_other_screen(self):
        a, b = self.win_a, self.win_b
        self._put_on_screens('screen-1', 'screen-2')
        b.isVisible = lambda: True  # 广播只投递给可见窗口
        received = []
        b._apply_shared_left_panel_width = lambda w: received.append(w)
        a._broadcast_left_panel_width(500)
        self.assertEqual(received, [], "宽度广播不应投递到异屏窗口")
        # 对照组：同屏则投递
        self._put_on_screens('screen-1', 'screen-1')
        a._broadcast_left_panel_width(500)
        self.assertEqual(received, [500], "同屏窗口应收到宽度广播")

    def test_width_memory_is_per_screen(self):
        a, b = self.win_a, self.win_b
        MW = self.mw_mod.MainWindow
        self._put_on_screens('screen-1', 'screen-2')
        a._saved_left_panel_width = 300
        self.assertEqual(a._saved_left_panel_width, 300)
        self.assertIsNone(b._saved_left_panel_width,
                          "A 屏确立的宽度不应通过共享记忆泄漏给 B 屏窗口")
        # 磁盘播种的兜底（None 桶）：两块屏都还没有自己的值时才用它
        MW._left_width_by_screen.clear()
        MW._left_width_by_screen[None] = 260
        self.assertEqual(a._saved_left_panel_width, 260)
        self.assertEqual(b._saved_left_panel_width, 260)
        b._saved_left_panel_width = 340
        self.assertEqual(b._saved_left_panel_width, 340)
        self.assertEqual(a._saved_left_panel_width, 260,
                         "B 屏拖出的新宽度不应覆盖 A 屏在用的兜底值")


if __name__ == '__main__':
    unittest.main()
