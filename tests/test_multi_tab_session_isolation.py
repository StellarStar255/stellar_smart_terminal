# -*- coding: utf-8 -*-
"""多标签页共享录制会话的隔离性回归测试。

背景：录制 session 是**窗口级共享**的——同一窗口所有标签页/分屏的输出都
写进同一个 SessionEntry。历史 bug：任一标签页的进程结束（SSH 掉线最常见）
都会无条件调 session_manager.end_session()，把整窗停录：

- 其它仍在运行的标签页，输出不再进历史/导出（add_output 抛 "No active
  session"），后续「导出会话」「历史搜索」全都缺失这些内容；
- 状态栏误报「已停止」，条目/文件计数归零；
- auto_save_timer 被停掉，窗口级自动保存失效。

用户视角就是「一个远程连接挂了，会影响其他 terminal 的使用」。

正确行为：只有窗口内再没有终端在跑时，才真正结束共享会话。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_multi_tab_session_isolation.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class _FakeTerminal:
    """只实现 is_running 的终端替身（不建真 pty，测试快且无副作用）。"""

    def __init__(self, running: bool):
        self._running = running

    def is_running(self) -> bool:
        return self._running


class TestMultiTabSessionIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        global main_window
        import main_window
        # 共用一个 MainWindow：反复建销主窗口的延迟析构残留在 CI 上会段错误
        cls.w = main_window.MainWindow()

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
        self._orig_tab_terminals = w.tab_terminals
        w.session_manager.create_session("ssh gpu-box")
        w.current_session = w.session_manager.current_session
        w.session_manager.add_output("标签 A 的输出\n")

    def tearDown(self):
        w = self.w
        w.tab_terminals = self._orig_tab_terminals
        w.session_manager.current_session = None
        w.current_session = None

    def test_other_running_tab_keeps_recording(self):
        """还有别的标签在跑 → 共享会话必须保留。"""
        w = self.w
        w.tab_terminals = {0: [_FakeTerminal(False)],   # 掉线的这个
                           1: [_FakeTerminal(True)]}    # 仍在跑的另一个
        w.auto_save_timer.start(30000)

        w._on_session_ended()

        self.assertIsNotNone(
            w.session_manager.current_session,
            "其它标签仍在运行时不应结束窗口共享会话")
        # 其它标签的输出仍能录进历史
        w.session_manager.add_output("标签 B 的后续输出\n")
        self.assertTrue(w.auto_save_timer.isActive(),
                        "自动保存不应被单个标签的结束关掉")

    def test_last_tab_ending_closes_session(self):
        """全部终端都停了 → 正常结束会话并停掉自动保存。"""
        w = self.w
        w.tab_terminals = {0: [_FakeTerminal(False)],
                           1: [_FakeTerminal(False)]}
        w.auto_save_timer.start(30000)

        w._on_session_ended()

        self.assertIsNone(w.session_manager.current_session,
                          "没有终端在跑时应正常结束会话")
        self.assertFalse(w.auto_save_timer.isActive())

    def test_split_pane_still_running_counts(self):
        """同一标签页内的分屏窗格仍在跑，也算窗口还在运行。"""
        w = self.w
        w.tab_terminals = {0: [_FakeTerminal(False), _FakeTerminal(True)]}
        w._on_session_ended()
        self.assertIsNotNone(w.session_manager.current_session)

    def test_any_terminal_running_tolerates_destroyed_widgets(self):
        """已析构的控件不应让判定抛异常（关标签页与进程退出可能竞态）。"""
        w = self.w

        class _Destroyed:
            def is_running(self):
                raise RuntimeError("wrapped C/C++ object has been deleted")

        w.tab_terminals = {0: [_Destroyed()], 1: [_FakeTerminal(True)]}
        self.assertTrue(w._any_terminal_running())

        w.tab_terminals = {0: [_Destroyed()]}
        self.assertFalse(w._any_terminal_running())


if __name__ == '__main__':
    unittest.main()
