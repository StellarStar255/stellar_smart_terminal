# -*- coding: utf-8 -*-
"""配置写盘时机的回归测试。

审查发现两处浪费与风险：

1. `_apply_global_zoom` 末尾无条件 `_save_config()`，而 `MainWindow.__init__`
   在恢复几何/面板状态之前就调用它 → 每个窗口构造时都做一次全量读-改-写，
   写进去的是默认尺寸与全 False 的面板可见性。正常退出会被 closeEvent 覆盖
   回来，崩溃/被 kill 时上次布局就丢了；工作区恢复开 N 个窗口即 N 次写盘。
2. Cmd+± / 透明度快捷键每按一次同步落盘（两次读 JSON + mkstemp + rename），
   长按就是连续写盘。

修复：构造期间不写配置；高频动作走 `_save_config_debounced()`，合并窗口内
的连续调用只落盘一次；closeEvent / force_close 会先 flush 未落盘的请求。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_config_save_debounce.py -v
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication


def _pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


class TestConfigSaveTiming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        import app_config
        cls.mw_mod = main_window
        cls.app_config = app_config

    def test_constructing_window_does_not_write_config(self):
        """窗口构造期间不能落盘（写的是未恢复的几何/面板状态）"""
        with mock.patch.object(
                self.app_config, 'update_config_with',
                wraps=self.app_config.update_config_with) as spy:
            win = self.mw_mod.MainWindow()
            try:
                # 只统计主窗口自身的全量保存（description='main-window'）；
                # _load_config 里的一次性迁移写盘（老配置缺迁移标记时）不算
                full_saves = [
                    c for c in spy.call_args_list
                    if c.kwargs.get('description') == 'main-window']
                self.assertEqual(
                    len(full_saves), 0,
                    "MainWindow.__init__ 期间发生了配置全量写盘")
            finally:
                win.close()
                win.deleteLater()
                for _ in range(5):
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.app.processEvents()

    def test_zoom_burst_writes_config_once(self):
        """连按 5 次缩放：立即 0 次写盘，防抖到期后恰好 1 次"""
        win = self.mw_mod.MainWindow()
        try:
            with mock.patch.object(
                    self.app_config, 'update_config_with',
                    wraps=self.app_config.update_config_with) as spy:
                for _ in range(5):
                    win._global_zoom_in()
                self.assertEqual(spy.call_count, 0, "缩放应延迟落盘，而不是每按一次写一次")
                _pump(self.app, 1.0)
                self.assertEqual(spy.call_count, 1, "防抖到期后应只写盘一次")
        finally:
            win._global_zoom_delta = 0
            timer = getattr(win, '_config_save_timer', None)
            if timer is not None:
                timer.stop()
            win.close()
            win.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_close_flushes_pending_save(self):
        """有未落盘的防抖请求时，关窗必须先同步写盘，不能丢"""
        win = self.mw_mod.MainWindow()
        with mock.patch.object(
                self.app_config, 'update_config_with',
                wraps=self.app_config.update_config_with) as spy:
            win._global_zoom_in()
            self.assertEqual(spy.call_count, 0)
            win._global_zoom_delta = 0
            # 未显示过的窗口 close() 不派发 closeEvent（Qt 直接返回），
            # 这里直接投递 QCloseEvent 走真实的关窗清理路径
            QApplication.sendEvent(win, QCloseEvent())
            self.assertGreaterEqual(spy.call_count, 1, "关窗没有 flush 掉挂起的配置写盘")
            timer = getattr(win, '_config_save_timer', None)
            self.assertFalse(
                timer is not None and timer.isActive(),
                "关窗后防抖定时器仍在运行")
        win.deleteLater()
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
