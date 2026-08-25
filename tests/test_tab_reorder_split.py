# -*- coding: utf-8 -*-
"""拖动标签重排后分屏「串页」的回归测试。

用户报告：开多个标签页，在最后面的标签里分屏，新终端有时跑到前面的
标签页里去了。

根因链：
1. `tab_widget.setMovable(True)` 允许拖动标签重排，QTabWidget 内部会同步
   页面顺序，但 **tabMoved 信号从未被接**；
2. `tab_splitters` / `tab_terminals` / `tab_cwds` 都是按索引存的字典，
   重排后整体错位：`tab_terminals[currentIndex]` 取到的是别的标签页的
   终端列表；
3. `_target_terminal_in_tab` 里焦点终端不在（错位的）列表里 →
   active_terminal 也不在 → 回退 `terminals[0]`，即**前面那个标签页**的
   终端；
4. 分屏于是插进了那个标签页的 splitter —— 用户看到「分到前面的
   terminal 去了」。关闭分屏同理会关错终端。

修复：
- `tabMoved` → `_on_tab_moved` → `_rebuild_tab_mappings()`（根因）；
- 分屏/关闭分屏入口 `_synced_tab_splitter` 以真实页面校验映射，错位时
  兜底重建（防御未来再有遗漏重建的路径）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_tab_reorder_split.py -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import QApplication


def _page_of(widget):
    """沿 parent 链找到 widget 所在的标签页页面（QTabWidget 的直接子页面）。"""
    from PyQt6.QtWidgets import QTabWidget, QStackedWidget
    node = widget
    while node is not None:
        parent = node.parent()
        if isinstance(parent, QStackedWidget) and isinstance(parent.parent(), QTabWidget):
            return node
        node = parent
    return None


class TestTabReorderSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.win = main_window.MainWindow()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.win.deleteLater()
        del cls.win
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def _ensure_tabs(self, n):
        while self.win.tab_widget.count() < n:
            self.win._add_new_tab()

    def _focus_in(self, terminal):
        """直接投递 FocusIn（offscreen 下不依赖真实焦点），更新 active_terminal。"""
        QApplication.sendEvent(terminal, QFocusEvent(QEvent.Type.FocusIn))

    def test_split_lands_in_current_tab_after_reorder(self):
        """拖动标签重排后，在最后一个标签分屏必须分在它自己页里"""
        win = self.win
        self._ensure_tabs(3)

        # 把第一个标签拖到最后（QTabBar.moveTab 与用户拖动走同一条路径，
        # 会发出 tabMoved 并让 QTabWidget 同步页面顺序）
        win.tab_widget.tabBar().moveTab(0, 2)

        # 用户此刻在「最后面的」标签页里操作
        last = win.tab_widget.count() - 1
        win.tab_widget.setCurrentIndex(last)
        current_page = win.tab_widget.widget(last)
        other_pages = [win.tab_widget.widget(i)
                       for i in range(win.tab_widget.count()) if i != last]

        # 聚焦当前页里的终端（沿真实页面找，不信可能错位的映射）
        from terminal_widget import TerminalWidget
        page_terms = current_page.findChildren(TerminalWidget)
        self.assertTrue(page_terms)
        self._focus_in(page_terms[0])

        before = set(page_terms)
        other_before = {p: set(p.findChildren(TerminalWidget)) for p in other_pages}
        with patch.object(TerminalWidget, 'start_process', lambda *a, **k: None):
            win._split_current_tab()

        new_terms = [t for t in current_page.findChildren(TerminalWidget)
                     if t not in before]
        # 新终端必须出现在当前页里……
        self.assertEqual(len(new_terms), 1,
                         "分屏后的新终端没有出现在当前标签页里（串到别的页去了）")
        # ……并且没有任何新终端混进其它标签页
        for page in other_pages:
            self.assertEqual(set(page.findChildren(TerminalWidget)),
                             other_before[page],
                             "别的标签页里多出了不属于它的终端")
        # 映射也应指向真实页面与新终端
        self.assertIs(win.tab_splitters.get(last), win.tab_widget.widget(last))
        self.assertIn(new_terms[0], win.tab_terminals.get(last, []))

    def test_vertical_split_lands_in_current_tab_after_reorder(self):
        """上下分屏同样不得串页"""
        win = self.win
        self._ensure_tabs(3)
        win.tab_widget.tabBar().moveTab(0, win.tab_widget.count() - 1)

        last = win.tab_widget.count() - 1
        win.tab_widget.setCurrentIndex(last)
        current_page = win.tab_widget.widget(last)

        from terminal_widget import TerminalWidget
        page_terms = current_page.findChildren(TerminalWidget)
        self.assertTrue(page_terms)
        self._focus_in(page_terms[0])

        before = set(page_terms)
        with patch.object(TerminalWidget, 'start_process', lambda *a, **k: None):
            win._split_vertical_current_terminal()

        new_terms = [t for t in current_page.findChildren(TerminalWidget)
                     if t not in before]
        self.assertEqual(len(new_terms), 1,
                         "上下分屏后的新终端没有出现在当前标签页里（串到别的页去了）")
        self.assertIn(new_terms[0], win.tab_terminals.get(last, []))

    def test_close_split_closes_own_terminal_after_reorder(self):
        """重排后关闭分屏必须关自己页里的终端，而不是别的页的"""
        win = self.win
        self._ensure_tabs(3)

        from terminal_widget import TerminalWidget
        # 先在第一个标签页里分出第二个终端（此刻映射是好的）
        win.tab_widget.setCurrentIndex(0)
        first_page = win.tab_widget.widget(0)
        page_terms = first_page.findChildren(TerminalWidget)
        self._focus_in(page_terms[0])
        with patch.object(TerminalWidget, 'start_process', lambda *a, **k: None):
            win._split_current_tab()
        self.app.processEvents()

        # 再把它拖到最后，制造映射错位
        last = win.tab_widget.count() - 1
        win.tab_widget.tabBar().moveTab(0, last)
        win.tab_widget.setCurrentIndex(last)
        current_page = win.tab_widget.widget(last)
        self.assertIs(current_page, first_page)

        other_terms_before = {
            t for i in range(win.tab_widget.count()) if i != last
            for t in win.tab_widget.widget(i).findChildren(TerminalWidget)}

        terms = current_page.findChildren(TerminalWidget)
        self.assertGreaterEqual(len(terms), 2)
        self._focus_in(terms[-1])
        win._close_current_split()
        self.app.processEvents()

        other_terms_after = {
            t for i in range(win.tab_widget.count()) if i != last
            for t in win.tab_widget.widget(i).findChildren(TerminalWidget)}
        # 别的标签页的终端一个都不能少
        self.assertEqual(len(other_terms_before), len(other_terms_after),
                         "关闭分屏关掉了别的标签页的终端")
        # 自己页里被关的那个才应该消失
        self.assertEqual(len(current_page.findChildren(TerminalWidget)),
                         len(terms) - 1,
                         "关闭分屏没有关掉当前页的终端")


if __name__ == '__main__':
    unittest.main()
