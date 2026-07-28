# -*- coding: utf-8 -*-
"""标签页状态徽章（运行中/等确认/已完成）的测试

覆盖：
- interaction/attention 信号 → 标签页出现挂起徽章（橙/绿点）
- 清除挂起（切标签/按键路径共用 _clear_tab_pending_badge）后：
  无运行终端 → 图标清空；挂起态优先于运行态
- 图标缓存（同 state 复用同一 QIcon）
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
        self.idx = w.tab_widget.currentIndex()
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
        self.assertTrue(w.tab_widget.tabIcon(self.idx).isNull())
        w._on_terminal_interaction(self.term)
        self.assertFalse(w.tab_widget.tabIcon(self.idx).isNull())
        self.assertEqual(
            getattr(w.tab_widget.widget(self.idx), '_badge_pending', None),
            'waiting')

    def test_done_badge_and_clear(self):
        w = self.w
        w._set_tab_pending_badge(self.term, 'done')
        self.assertFalse(w.tab_widget.tabIcon(self.idx).isNull())
        # 清除挂起：无运行终端 → 图标清空
        w._clear_tab_pending_badge(self.idx)
        self.assertIsNone(
            getattr(w.tab_widget.widget(self.idx), '_badge_pending', None))
        if not any(t.is_running() for t in w.tab_terminals[self.idx]):
            self.assertTrue(w.tab_widget.tabIcon(self.idx).isNull())

    def test_pending_overrides_running(self):
        w = self.w
        # 伪造运行态：挂起徽章仍优先显示
        orig = self.term.is_running
        self.term.is_running = lambda: True
        try:
            w._set_tab_pending_badge(self.term, 'waiting')
            icon_waiting = w.tab_widget.tabIcon(self.idx)
            self.assertFalse(icon_waiting.isNull())
            # 清除挂起后回落到运行状态点（仍有图标）
            w._clear_tab_pending_badge(self.idx)
            self.assertFalse(w.tab_widget.tabIcon(self.idx).isNull())
        finally:
            self.term.is_running = orig
            w._refresh_tab_badge(self.idx)

    def test_icon_cache_reuse(self):
        w = self.w
        self.assertIs(w._tab_badge_icon('running'), w._tab_badge_icon('running'))
        self.assertIsNot(w._tab_badge_icon('running'), w._tab_badge_icon('done'))


if __name__ == '__main__':
    unittest.main()
