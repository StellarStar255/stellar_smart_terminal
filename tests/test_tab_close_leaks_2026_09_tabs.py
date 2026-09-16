# -*- coding: utf-8 -*-
"""2026-09 审查：标签页关闭 / 分屏重组 / 右键菜单 的四个回归。

1. Cmd+W 关最后一个标签：走 _close_tab_or_window → _force_closing → close()，
   closeEvent 里的 prompt_save_all 被跳过，编辑器未保存改动直接丢。
2. _close_tab 只 cleanup + removeTab，页面 splitter 与终端从不销毁（每关一
   个标签泄漏整套 TerminalWidget）。
3. 右键菜单（窗格把手 / 标签）的 QMenu(self) exec 完不销毁；切语言重建菜单栏
   时 menuBar().clear() 只摘 action，旧 QMenu 一直挂在 menubar 下。
4. _wrap_tab_page 的 removeTab/insertTab/setCurrentIndex 没屏蔽信号，
   _on_tab_changed 被连发四次（含 -1）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_tab_close_leaks_2026_09_tabs.py -q
"""
import gc
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox


def _flush(app, rounds=5):
    """把排队的 deleteLater 冲干净，再收一次 Python 垃圾。"""
    for _ in range(rounds):
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    gc.collect()


def _fake_backend():
    """假后端：is_running / write / stop / resize 齐全，不 fork 真 shell。"""
    return types.SimpleNamespace(
        is_running=True, write=lambda data: True, stop=lambda: None,
        resize=lambda cols, rows: None, on_output=None, on_exit=None)


class _WindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        import terminal_widget
        cls.mw = main_window
        cls.TerminalWidget = terminal_widget.TerminalWidget

    def setUp(self):
        self.win = self.mw.MainWindow()
        self.win.show()
        self.app.processEvents()

    def tearDown(self):
        win = self.win
        if not sip.isdeleted(win):
            if not win._closing_in_progress:
                win._force_closing = True
                QApplication.sendEvent(win, QCloseEvent())
            win.deleteLater()
        _flush(self.app)

    def _ensure_tabs(self, n):
        while self.win.tab_widget.count() < n:
            self.win._add_new_tab()
        self.app.processEvents()


# ---------------------------------------------------------------------------
# 1. Cmd+W 关最后一个标签必须先过编辑器的保存提示
# ---------------------------------------------------------------------------
class TestCloseLastTabPromptsEditorSave(_WindowCase):
    def _run_close(self, prompt_reply):
        win = self.win
        self.assertEqual(win.tab_widget.count(), 1)
        closed = []
        win.close = lambda: closed.append(True) or True
        with mock.patch.object(self.mw.MainWindow, '_focus_in_editor_area',
                               return_value=False), \
             mock.patch.object(type(win.editor_area), 'prompt_save_all',
                               return_value=prompt_reply) as prompt, \
             mock.patch.object(type(win.editor_area), 'flush_autosave_all') as flush, \
             mock.patch('main_window_tabs.QMessageBox.question',
                        return_value=QMessageBox.StandardButton.Yes):
            win._close_tab_or_window()
        return prompt, flush, closed

    def test_cancel_in_editor_prompt_aborts_window_close(self):
        """编辑器提示里点取消 → 标签不关、窗口不关、不置 _force_closing。"""
        prompt, flush, closed = self._run_close(prompt_reply=False)
        self.assertEqual(prompt.call_count, 1,
                         "Cmd+W 关最后一个标签没有先问编辑器是否保存")
        self.assertEqual(closed, [], "编辑器提示取消后窗口仍被关闭")
        self.assertEqual(self.win.tab_widget.count(), 1, "取消后标签页被关掉了")
        self.assertFalse(self.win._force_closing)
        flush.assert_not_called()

    def test_confirmed_prompt_flushes_autosave_then_closes(self):
        """保存/丢弃后：刷崩溃恢复备份，再走强制关闭。"""
        prompt, flush, closed = self._run_close(prompt_reply=True)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(flush.call_count, 1)
        self.assertEqual(closed, [True])
        self.assertTrue(self.win._force_closing)


# ---------------------------------------------------------------------------
# 2. 关标签必须销毁页面与终端
# ---------------------------------------------------------------------------
class TestCloseTabDestroysPage(_WindowCase):
    def test_closed_tab_page_and_terminal_are_deleted(self):
        win = self.win
        self._ensure_tabs(2)
        page = win.tab_widget.widget(1)
        term = win.tab_terminals[1][0]
        term._backend = _fake_backend()
        self.assertIs(term.parent(), page)

        win._close_tab(1)
        _flush(self.app)

        self.assertTrue(sip.isdeleted(term), "关掉的标签页里的终端没有被销毁（泄漏）")
        self.assertTrue(sip.isdeleted(page), "关掉的标签页页面没有被销毁（泄漏）")
        remaining = sum(len(v) for v in win.tab_terminals.values())
        self.assertEqual(len(win.tab_widget.findChildren(self.TerminalWidget)),
                         remaining)

    def test_close_last_tab_recreates_and_leaves_no_orphans(self):
        win = self.win
        self.assertEqual(win.tab_widget.count(), 1)
        old_term = win.tab_terminals[0][0]
        win._close_tab(0)               # auto_create_new=True → 补一个新标签
        _flush(self.app)
        self.assertEqual(win.tab_widget.count(), 1)
        self.assertTrue(sip.isdeleted(old_term))
        self.assertEqual(len(win.tab_widget.findChildren(self.TerminalWidget)), 1)
        self.assertIs(win.active_terminal, win.tab_terminals[0][0])

    def test_pane_moved_to_other_window_survives_source_tab_close(self):
        """跨窗口把最后一个窗格挪走 → 源页经 _finish_pane_source→_close_tab 关掉，
        被搬走的终端必须活着。"""
        win = self.win
        other = self.mw.MainWindow()
        try:
            term = win.tab_terminals[0][0]
            self.assertTrue(other._pop_pane_to_tab(win, term))
            _flush(self.app)
            self.assertFalse(sip.isdeleted(term), "被搬到别的窗口的终端被销毁了")
            self.assertTrue(other.isAncestorOf(term))
            self.assertIn(term, other.tab_terminals[other.tab_widget.count() - 1])
            # 源窗口补了一个空白标签，旧页面已销毁
            self.assertEqual(win.tab_widget.count(), 1)
            self.assertIsNot(win.tab_terminals[0][0], term)
        finally:
            if not sip.isdeleted(other):
                other._force_closing = True
                QApplication.sendEvent(other, QCloseEvent())
                other.deleteLater()
            _flush(self.app)


# ---------------------------------------------------------------------------
# 3. QMenu 不能越积越多
# ---------------------------------------------------------------------------
class TestMenusDoNotLeak(_WindowCase):
    ROUNDS = 5

    def _count_menus(self):
        _flush(self.app)
        return len(self.win.findChildren(QMenu))

    def test_pane_menu_is_released_after_exec(self):
        win = self.win
        term = win.tab_terminals[0][0]
        before = self._count_menus()
        with mock.patch.object(QMenu, 'exec', return_value=None):
            for _ in range(self.ROUNDS):
                win._show_pane_menu(term, QPoint(10, 10))
        self.assertEqual(self._count_menus(), before,
                         "窗格右键菜单 exec 后没有销毁，QMenu 越积越多")

    def test_tab_context_menu_is_released_after_exec(self):
        win = self.win
        bar = win.tab_widget.tabBar()
        bar.tabAt = lambda pos: 0     # offscreen 下不依赖真实布局
        before = self._count_menus()
        with mock.patch.object(QMenu, 'exec', return_value=None):
            for _ in range(self.ROUNDS):
                win._show_tab_context_menu(QPoint(5, 5))
        self.assertEqual(self._count_menus(), before,
                         "标签右键菜单 exec 后没有销毁，QMenu 越积越多")

    def test_rebuild_menus_releases_old_menubar_menus(self):
        win = self.win
        before = len(win.menuBar().findChildren(QMenu))
        n_actions = len(win.menuBar().actions())
        for _ in range(self.ROUNDS):
            win._rebuild_menus()
        _flush(self.app)
        self.assertEqual(len(win.menuBar().findChildren(QMenu)), before,
                         "重建菜单栏后旧 QMenu 仍挂在 menubar 下")
        self.assertEqual(len(win.menuBar().actions()), n_actions)
        # 重建后的菜单仍可用
        self.assertTrue(all(a.menu() is not None and not sip.isdeleted(a.menu())
                            for a in win.menuBar().actions()))


# ---------------------------------------------------------------------------
# 4. _wrap_tab_page 只让 _on_tab_changed 跑一次
# ---------------------------------------------------------------------------
class TestWrapTabPageSignals(_WindowCase):
    def test_whole_tab_vertical_split_syncs_tab_once(self):
        win = self.win
        idx = win.tab_widget.currentIndex()
        self.assertEqual(win.tab_splitters[idx].orientation(), Qt.Orientation.Horizontal)
        calls = []
        # 信号连的是构造时绑定的方法，patch 实例属性拦不到 → 另接一个槽记录
        # 信号发射；手动直调走实例属性。二者之和才是 _on_tab_changed 的实际次数。
        win.tab_widget.currentChanged.connect(calls.append)
        real = win._on_tab_changed
        win._on_tab_changed = lambda i: (calls.append(i), real(i))[1]
        with mock.patch.object(self.TerminalWidget, 'start_process',
                               lambda *a, **k: None):
            win._split_vertical_current_terminal(whole_tab=True)
        win.tab_widget.currentChanged.disconnect(calls.append)

        self.assertEqual(len(calls), 1,
                         f"_wrap_tab_page 触发了 {len(calls)} 次 tab 切换回调: {calls}")
        self.assertNotEqual(calls[0], -1)
        self.assertEqual(calls[0], idx)
        # 重组后的不变式：页面控件 == tab_splitters[idx]，终端都在页里
        outer = win.tab_widget.widget(idx)
        self.assertIs(win.tab_splitters[idx], outer)
        self.assertEqual(outer.orientation(), Qt.Orientation.Vertical)
        self.assertEqual(len(win.tab_terminals[idx]), 2)
        for term in win.tab_terminals[idx]:
            self.assertTrue(outer.isAncestorOf(term))


if __name__ == '__main__':
    unittest.main()
