# -*- coding: utf-8 -*-
"""标签页状态徽章（运行中/等确认/已完成）的测试

覆盖：
- interaction/attention 信号 → 标签页出现挂起徽章（橙/绿点）
- 清除挂起（切标签/按键路径共用 _clear_tab_pending_badge）后：
  无运行终端 → 图标清空；挂起态优先于运行态
- 状态切换时标签几何与标题像素稳定
- _find_tab_of_terminal 定位

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_tab_badges.py -v
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication


class TestTabBadges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        global main_window
        import main_window
        # 共用一个 MainWindow（每用例新建/销毁完整主窗口会在 CI 段错误）
        cls.w = main_window.MainWindow()
        cls.w._add_new_tab()
        cls.w.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtCore import QEvent
        cls.w.close()
        cls.w.deleteLater()
        del cls.w
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def setUp(self):
        w = self.w
        w.tab_widget.setCurrentIndex(1)
        self.idx = 0
        self.term = w.tab_terminals[self.idx][0]
        # 清干净挂起态
        page = w.tab_widget.widget(self.idx)
        page._badge_pending = None
        w._refresh_tab_badge(self.idx)

    def test_find_tab_of_terminal(self):
        self.assertEqual(self.w._find_tab_of_terminal(self.term), self.idx)
        self.assertIsNone(self.w._find_tab_of_terminal(object()))

    def test_interaction_sets_waiting_badge(self):
        w = self.w
        self.assertIsNone(w._tab_badge_color(self.idx))
        w._on_terminal_interaction(self.term)
        self.assertIsNotNone(w._tab_badge_color(self.idx))
        self.assertEqual(
            getattr(w.tab_widget.widget(self.idx), '_badge_pending', None),
            'waiting')

    def test_done_badge_and_clear(self):
        w = self.w
        w._set_tab_pending_badge(self.term, 'done')
        self.assertIsNotNone(w._tab_badge_color(self.idx))
        # 清除挂起：无运行终端 → 图标清空
        w._clear_tab_pending_badge(self.idx)
        self.assertIsNone(
            getattr(w.tab_widget.widget(self.idx), '_badge_pending', None))
        if not any(t.is_running() for t in w.tab_terminals[self.idx]):
            self.assertIsNone(w._tab_badge_color(self.idx))

    def test_no_persistent_dot_while_running(self):
        """常驻「运行中」灰点已移除：挂起提醒显示图标，清除后即空——
        哪怕会话仍在运行也不再回落出灰点（用户点名去掉，2026-08-04）。"""
        w = self.w
        orig = self.term.is_running
        self.term.is_running = lambda: True
        try:
            w._set_tab_pending_badge(self.term, 'waiting')
            self.assertEqual(w._tab_badge_color(self.idx), "#f59e0b")
            w._clear_tab_pending_badge(self.idx)
            self.assertIsNone(w._tab_badge_color(self.idx))
        finally:
            self.term.is_running = orig
            w._refresh_tab_badge(self.idx)

    def test_background_output_pulses_until_selected_or_acknowledged(self):
        w = self.w
        bar = w.detachable_tab_bar
        self.term.output_active_changed.emit(True)
        bar.grab()
        self.assertTrue(bar._pulse_timer.isActive())
        self.assertTrue(bar._pulse_region.intersects(bar.tabRect(self.idx)))
        w._set_tab_pending_badge(self.term, 'done')
        bar.grab()
        self.assertFalse(bar._pulse_timer.isActive())
        self.assertIsNotNone(w._tab_badge_color(self.idx))
        w.tab_widget.setCurrentIndex(self.idx)
        w._on_terminal_interaction(self.term)
        bar.grab()
        self.assertIsNone(w._tab_badge_color(self.idx))
        self.assertFalse(bar._pulse_timer.isActive())
        w.tab_widget.setCurrentIndex(1)
        bar.grab()
        self.assertTrue(bar._pulse_timer.isActive())
        bar.setUpdatesEnabled(False)
        try:
            self.term.set_output_marker_enabled(False)
            self.assertFalse(bar._pulse_timer.isActive())
        finally:
            bar.setUpdatesEnabled(True)
        bar.grab()
        self.assertFalse(bar._pulse_timer.isActive())
        self.term.set_output_marker_enabled(True)

    def test_exit_keeps_selected_tab_unmarked_in_inactive_window(self):
        from unittest.mock import patch
        w = self.w
        idx = w.tab_widget.currentIndex()
        selected = w.tab_terminals[idx][0]
        with patch.object(w, 'isActiveWindow', return_value=False):
            w._on_tab_session_ended(selected)
            self.assertIsNone(w._tab_badge_color(idx))
            w._on_tab_session_ended(self.term)
            self.assertIsNotNone(w._tab_badge_color(self.idx))
        w.tab_widget.setCurrentIndex(self.idx)
        self.assertIsNone(w._tab_badge_color(self.idx))

    def test_status_changes_only_repaint_the_dot(self):
        w = self.w
        bar = w.detachable_tab_bar
        rect = bar.tabRect(self.idx)
        baseline = bar.grab(rect).toImage()
        scale = baseline.devicePixelRatio()
        title = baseline.rect().adjusted(round(20 * scale), 0, 0, 0)
        self.term.output_active_changed.emit(True)
        grey = bar.grab(rect).toImage()
        self.assertNotEqual(grey, baseline)
        self.assertEqual(grey.copy(title), baseline.copy(title))
        for state in ('waiting', 'done', None):
            w._set_tab_pending_badge(self.term, state)
            frame = bar.grab(rect).toImage()
            self.assertEqual(bar.tabRect(self.idx), rect)
            self.assertEqual(frame.copy(title), baseline.copy(title))
            if state:
                self.assertNotEqual(frame, baseline)
                center = round(10 * scale), round((rect.height() // 2 + 2) * scale)
                self.assertEqual(frame.pixelColor(*center).name(), w._tab_badge_color(self.idx))
        self.term.output_active_changed.emit(False)
        self.assertEqual(bar.grab(rect).toImage(), baseline)

if __name__ == '__main__':
    unittest.main()
