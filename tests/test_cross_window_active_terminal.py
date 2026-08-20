# -*- coding: utf-8 -*-
"""跨窗口 active_terminal 串台的回归测试。

用户报告：多窗口（尤其远程连接）下按 Cmd+Shift+- 分屏，会把**之前的**终端
莫名关掉/弄丢。

根因链：
1. `_create_terminal` 里 `terminal.installEventFilter(self)` 让所属窗口能在
   FocusIn 时更新 `active_terminal`；
2. 标签页被拖到另一个窗口时，新窗口 `installEventFilter(self)` 接管，但
   **旧窗口的事件过滤器从未被摘除**（全代码库没有一处 removeEventFilter）；
3. 于是该终端获得焦点时，两个窗口的过滤器都跑，旧窗口的
   `active_terminal` 被设成一个**已经不属于它**的终端；
4. 旧窗口里任何依赖 active_terminal 的操作从此指向别的窗口：
   - `_close_current_split`：活动终端不在本页 → 回退关掉 `terminals[-1]`，
     即**关错终端**（用户看到的「把之前的 terminal 关掉了」）；
   - `_split_vertical_current_terminal` / `_split_current_tab`：
     `active_terminal not in terminals` → 误判为「整页分屏」，
     走 `_wrap_tab_page` 重组整个标签页，而不是分裂当前窗格。

修复：终端被别的窗口接管时先 `removeEventFilter(旧窗口)`；并给
`active_terminal` 加归属校验，非本窗口的一律不采信。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_cross_window_active_terminal.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QFocusEvent
from PyQt6.QtWidgets import QApplication


class _CrossWindowBase(unittest.TestCase):
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

    def _terminal(self, owner):
        """建一个归 owner 窗口所有的终端（不启动子进程）。"""
        return owner._create_terminal()

    def _focus_in(self, terminal):
        """直接投递 FocusIn 事件（offscreen 下不依赖真实焦点）。"""
        QApplication.sendEvent(
            terminal, QFocusEvent(QEvent.Type.FocusIn))


class TestEventFilterOwnership(_CrossWindowBase):
    def test_adopted_terminal_stops_updating_old_window(self):
        """终端被 B 接管后，聚焦它不能再改 A 的 active_terminal"""
        a, b = self.win_a, self.win_b
        term = self._terminal(a)
        a.active_terminal = None
        b.active_terminal = None

        # A 仍持有它时：聚焦应更新 A
        self._focus_in(term)
        self.assertIs(a.active_terminal, term)

        # B 接管（真实路径里由 _add_new_tab 的外部分支执行）
        b._adopt_terminal(term)
        a.active_terminal = None
        b.active_terminal = None

        self._focus_in(term)
        self.assertIs(b.active_terminal, term, "接管方应更新自己的活动终端")
        self.assertIsNone(
            a.active_terminal,
            "原窗口的事件过滤器未摘除 → active_terminal 被别窗口的终端污染")


class TestStaleActiveTerminalIsIgnored(_CrossWindowBase):
    """即使 active_terminal 因任何原因串台，依赖它的操作也必须自保"""

    def test_close_split_never_closes_wrong_terminal(self):
        """活动终端不属于本窗口时，关闭分屏不能拿 terminals[-1] 顶包"""
        a, b = self.win_a, self.win_b
        idx = a.tab_widget.currentIndex()
        own = a.tab_terminals.get(idx, [])
        # 必须有 2 个以上终端，否则 _close_current_split 会提前返回，
        # 危险的 terminals[-1] 兜底路径根本走不到（测试就成了摆设）
        extra = self._terminal(a)
        own.append(extra)
        self.assertGreater(len(own), 1)

        # 人为制造串台：A 的活动终端指向 B 的终端
        foreign = self._terminal(b)
        a.active_terminal = foreign
        before = list(own)

        try:
            a._close_current_split()
            self.assertEqual(
                a.tab_terminals.get(idx, []), before,
                "活动终端不在本窗口时，不应关掉本页任何终端")
        finally:
            if extra in a.tab_terminals.get(idx, []):
                a.tab_terminals[idx].remove(extra)

    def test_split_does_not_fall_back_to_whole_tab(self):
        """活动终端串台时，分屏不应误判成「整页分屏」而重组标签页"""
        a, b = self.win_a, self.win_b
        idx = a.tab_widget.currentIndex()
        page_before = a.tab_widget.widget(idx)

        foreign = self._terminal(b)
        a.active_terminal = foreign

        a._split_vertical_current_terminal()

        self.assertIs(
            a.tab_widget.widget(idx), page_before,
            "串台的活动终端不应触发整页重组（_wrap_tab_page 会换掉页面控件）")


class TestTabPageInvariant(_CrossWindowBase):
    def test_wrap_tab_page_uses_actual_widget(self):
        """_wrap_tab_page 必须以 tab_widget 里的真实页面为准

        若它信任 tab_splitters[idx] 而该映射恰好过期，removeTab 摘掉的是真实
        页面、重新插回的却是别的页面 —— 真实页面连同其终端从界面上消失，
        用户看到的就是「终端被关掉了」。
        """
        a = self.win_a
        idx = a.tab_widget.currentIndex()
        real_page = a.tab_widget.widget(idx)

        # 人为把映射弄脏（模拟重排/接管后未同步的窗口）
        a.tab_splitters[idx] = a._styled_splitter(
            self.mw_mod.Qt.Orientation.Horizontal)

        new_term = a._create_terminal()
        a._wrap_tab_page(idx, self.mw_mod.Qt.Orientation.Vertical, new_term)

        outer = a.tab_widget.widget(idx)
        self.assertIsNotNone(outer)
        # 真实页面必须仍在界面里（被包进新的外层 splitter），没有被丢弃
        self.assertIs(real_page.parent(), outer,
                      "真实页面被 removeTab 摘走后没有重新挂回界面")


if __name__ == '__main__':
    unittest.main()
