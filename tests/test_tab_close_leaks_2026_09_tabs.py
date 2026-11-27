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
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, Qt, QTimer
from PyQt6.QtGui import QCloseEvent, QContextMenuEvent, QInputMethodEvent, QKeySequence
from PyQt6.QtTest import QTest
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
class TestCloseLastTab(_WindowCase):
    def _wait_for_start_result(self, terminal):
        for _ in range(100):
            if not terminal.is_starting():
                return
            QTest.qWait(20)
        self.fail("Timed out waiting for the terminal startup result")

    @unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
    def test_startup_input_stays_in_order_and_in_original_tab(self):
        from terminal_backend import UnixBackend

        win = self.win
        self._ensure_tabs(2)
        term = win.terminal
        child_exec = UnixBackend._child_exec

        def delayed_exec(backend, *args):
            time.sleep(0.2)
            child_exec(backend, *args)

        with tempfile.TemporaryDirectory() as cwd:
            with mock.patch.object(UnixBackend, "_child_exec", delayed_exec):
                term.start_process(["/bin/sh"], cwd)
            QTest.keyClicks(term, "echo firsx")
            QTest.keyClick(term, Qt.Key.Key_Backspace)
            QTest.keyClicks(term, "t > steps")
            QTest.keyClick(term, Qt.Key.Key_Return)
            QTest.keyClicks(term, "echo ")
            ime = QInputMethodEvent()
            ime.setCommitString("第二")
            QApplication.sendEvent(term, ime)
            # Snapshot clipboard text without touching the user's system clipboard.
            term._ensure_clipboard_watch()
            with mock.patch.object(QApplication, "clipboard") as clipboard:
                clipboard.return_value.text.return_value = "-paste"
                term._paste_from_clipboard()
                term._on_clipboard_data_changed()
            win.tab_widget.setCurrentIndex(0)
            self._wait_for_start_result(term)
            win.tab_widget.setCurrentIndex(1)
            QTest.keyClicks(term, " >> steps")
            QTest.keyClick(term, Qt.Key.Key_Return)
            marker = Path(cwd, "steps")
            for _ in range(100):
                QTest.qWait(20)
                if marker.exists() and len(marker.read_text().splitlines()) == 2:
                    break
            self.assertEqual(marker.read_text().splitlines(), ["first", "第二-paste"])

    @unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
    def test_slow_start_allows_switching_tabs_and_cancellation(self):
        import select
        import signal
        from terminal_backend import UnixBackend

        win = self.win
        self._ensure_tabs(2)
        term = win.terminal
        child_exec = UnixBackend._child_exec
        read_fd, write_fd = os.pipe()

        def delayed_exec(backend, *args):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.close(write_fd)
            # Hold the forked child before exec until the GUI has requested stop.
            select.select([read_fd], [], [], 5)
            child_exec(backend, *args)

        with tempfile.TemporaryDirectory() as cwd:
            try:
                with mock.patch.object(UnixBackend, "_child_exec", delayed_exec):
                    term.start_process(["/bin/sh", "-c", "touch launched"], cwd)
                pid = term._backend._child_pid
                timer_states = []
                QTimer.singleShot(0, lambda: timer_states.append(term.is_starting()))
                win.tab_widget.setCurrentIndex(0)
                deadline = time.monotonic() + 3
                while not timer_states and time.monotonic() < deadline:
                    QTest.qWait(10)
                self.assertEqual(timer_states, [True])
                win.tab_widget.setCurrentIndex(1)
                self.assertTrue(win.stop_btn.isEnabled())
                win.stop_btn.click()
                self.assertEqual(win.tab_widget.count(), 1)
            finally:
                os.close(read_fd)
                os.close(write_fd)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                QTest.qWait(10)
            else:
                self.fail("Cancelled startup process was not reaped")
            self.assertFalse(Path(cwd, "launched").exists())

    @unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
    def test_stop_split_keeps_sibling_shell_and_output(self):
        win = self.win
        left = win.terminal
        with tempfile.TemporaryDirectory() as cwd:
            left.start_process(["/bin/sh"], cwd)
            self._wait_for_start_result(left)
            left._send_text_now("echo LEFT_OUTPUT\r")
            for _ in range(100):
                QTest.qWait(20)
                if "LEFT_OUTPUT" in "".join(left.screen.display):
                    break
            win._split_current_tab()
            right = win.terminal
            self._wait_for_start_result(right)
            self.assertIsNot(left, right)
            left.setFocus()
            self.app.processEvents()
            win.stop_btn.click()
            self.assertFalse(left.is_running())
            self.assertIn("LEFT_OUTPUT", "".join(left.screen.display))
            self.assertTrue(right.is_running())
            self.assertEqual(win.tab_terminals[0], [left, right])
            right.setFocus()
            self.app.processEvents()
            self.assertTrue(win.stop_btn.isEnabled())
            marker = Path(cwd, "still-running")
            right._send_text_now(f"touch '{marker}'\r")
            for _ in range(100):
                QTest.qWait(20)
                if marker.exists():
                    break
            self.assertTrue(marker.exists())

    @unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
    def test_failed_exec_shows_reason_and_enter_retries_original_directory(self):
        from i18n import t

        win = self.win
        term = win.terminal
        with tempfile.TemporaryDirectory() as cwd:
            shell = Path(cwd, "missing-shell")
            term.start_process([str(shell)], cwd)
            self._wait_for_start_result(term)
            self.assertFalse(term.is_running())
            self.assertEqual(win.session_label.text(), t("status.start_failed"))
            self.assertIn("missing-shell", "".join(term.screen.display).replace(" ", ""))
            shell.symlink_to("/bin/sh")
            QTest.keyClick(term, Qt.Key.Key_Return)
            self._wait_for_start_result(term)
            self.assertTrue(term.is_running())
            self.assertEqual(win.session_label.text(), t("status.running"))
            term._send_text_now("pwd > actual-cwd\r")
            marker = Path(cwd, "actual-cwd")
            for _ in range(100):
                QTest.qWait(20)
                if marker.exists() and marker.read_text().strip():
                    break
            self.assertTrue(Path(marker.read_text().strip()).samefile(cwd))

    @unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
    def test_retry_restores_preset_commands_once_in_order(self):
        win = self.win
        with tempfile.TemporaryDirectory() as cwd:
            shell = Path(cwd, "missing-shell")
            marker = Path(cwd, "steps")
            win.presets = [{"name": "Probe", "commands": [
                str(shell), "echo first >> steps", "echo second >> steps"]}]
            win.preset_combo.setCurrentIndex(0)
            win._start_session(cwd)
            term = win.terminal
            QTest.qWait(600)
            self.assertTrue(term._failed_process_start)
            QTest.keyClick(term, Qt.Key.Key_Return)
            QTest.qWait(100)
            self.assertTrue(term._failed_process_start)
            shell.symlink_to("/bin/sh")
            QTest.keyClick(term, Qt.Key.Key_Return)
            QTest.qWait(1500)
            self.assertEqual(marker.read_text().splitlines(), ["first", "second"])

    @unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
    def test_retry_runs_selected_quick_commands_but_discards_typed_input(self):
        term = self.win.terminal
        with tempfile.TemporaryDirectory() as cwd:
            term.set_working_dir(cwd)
            shell = Path(cwd, "missing-shell")
            with mock.patch.object(term, "_get_default_shell", return_value=str(shell)):
                term._execute_commands(["echo first >> steps"])
                term._execute_commands(["echo second >> steps"])
                term.send_text("touch stale-input\r")
                self._wait_for_start_result(term)
                self.assertTrue(term._failed_process_start)
                QTest.keyClick(term, Qt.Key.Key_Return)
                self._wait_for_start_result(term)
                self.assertTrue(term._failed_process_start)
                shell.symlink_to("/bin/sh")
                QTest.keyClick(term, Qt.Key.Key_Return)
                self._wait_for_start_result(term)
                term.send_text("echo done >> steps\r")
                marker = Path(cwd, "steps")
                for _ in range(100):
                    QTest.qWait(20)
                    if marker.exists() and "done" in marker.read_text():
                        break
                self.assertEqual(marker.read_text().splitlines(), ["first", "second", "done"])
                self.assertFalse(Path(cwd, "stale-input").exists())

    @unittest.skipIf(os.name == "nt", "Requires a POSIX shell")
    def test_stop_and_replacement_discard_pending_keys_and_commands(self):
        term = self.win.terminal
        with tempfile.TemporaryDirectory() as cwd:
            for stop_first in (True, False):
                term.start_process(["/bin/sh"], cwd)
                term._execute_commands(["touch stale-quick"])
                QTest.keyClicks(term, "touch stale-keys")
                QTest.keyClick(term, Qt.Key.Key_Return)
                if stop_first:
                    term.stop_process()
                term.start_process(["/bin/sh"], cwd)
                self._wait_for_start_result(term)
                term.send_text("echo ok >> fresh\r")
                marker = Path(cwd, "fresh")
                expected = ["ok"] if stop_first else ["ok", "ok"]
                for _ in range(100):
                    QTest.qWait(20)
                    if marker.exists() and marker.read_text().splitlines() == expected:
                        break
                self.assertEqual(marker.read_text().splitlines(), expected)
                self.assertFalse(Path(cwd, "stale-quick").exists())
                self.assertFalse(Path(cwd, "stale-keys").exists())
                term.stop_process()

    def test_window_close_buttons_and_default(self):
        from i18n import t

        seen = []
        def press_default():
            box = QApplication.activeModalWidget()
            seen.append((box.defaultButton().text(), box.escapeButton().text(),
                         box.button(QMessageBox.StandardButton.Yes).text()))
            QTest.keyClick(box, Qt.Key.Key_Return)
        QTimer.singleShot(0, press_default)
        self.assertFalse(self.win._confirm_window_close(t("msg.confirm_close_window")))
        self.assertEqual(seen, [(t("msg.keep_window"), t("msg.keep_window"), t("msg.close_window"))])

    def test_empty_workspace_restores_and_start_opens_one_tab(self):
        win = self.win
        win._apply_restored_window_state({})
        self.assertEqual(win.tab_widget.count(), 1)
        win._apply_restored_window_state({'tabs': []})
        self.assertEqual(win.tab_widget.count(), 0)
        self.assertEqual(win.tab_sessions, {})
        win.presets = [{'name': 'Test shell', 'commands': ['sh']}]
        win.preset_combo.setCurrentIndex(0)
        with mock.patch.object(self.TerminalWidget, 'start_process') as start:
            win.start_btn.click()
            start.assert_called_once()
        self.assertEqual(win.tab_widget.count(), 1)
        self.assertIs(win.active_terminal, win.tab_terminals[0][0])

    def test_toolbar_close_and_empty_window_reopen(self):
        from dialogs import get_terminal_shell
        from i18n import t

        win = self.win
        tabs = win.tab_widget
        button = tabs.empty_terminal_button
        tools = (win.split_btn, win.split_v_btn, win.close_split_btn, win.close_tab_btn)
        term = win.terminal
        term._backend = _fake_backend()
        self.assertTrue(term.is_running())
        with mock.patch.object(QMessageBox, 'question') as question:
            win.close_tab_btn.click()
            question.assert_not_called()
        self.assertFalse(term.is_running())
        self.assertEqual(tabs.count(), 0)
        self.assertEqual(win.session_label.text(), t("status.no_open_terminal"))
        self.assertTrue(all(not tool.isEnabled() for tool in tools))
        self.assertTrue(button.isVisible())
        self.assertIs(tabs.childAt(button.geometry().center()), button)
        action = win.shortcut_actions["new_terminal"]
        action.setShortcut(QKeySequence("Ctrl+Shift+9"))
        self.assertIn(action.shortcut().toString(QKeySequence.SequenceFormat.NativeText), button.text())
        action.setShortcut(QKeySequence())
        self.assertEqual(button.text(), t("terminal.new_in_current_dir"))
        with mock.patch.object(self.TerminalWidget, 'start_process') as start:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            start.assert_called_once_with([get_terminal_shell()], cwd=win._window_cwd)
        self.assertEqual(tabs.count(), 1)
        self.assertFalse(button.isVisible())
        self.assertTrue(all(tool.isEnabled() for tool in tools))
        self.assertNotEqual(win.session_label.text(), t("status.running"))

    def test_empty_menu_creates_a_shell_only_on_command_selection(self):
        from dialogs import get_terminal_shell
        from i18n import t

        win = self.win
        win._close_tab(0)
        win.local_presets = [{'name': 'Probe', 'commands': ['echo probe']}]
        tabs = win.tab_widget
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10))
        entries = []

        def browse(menu, pos):
            actions = [action for action in menu.actions() if not action.isSeparator()]
            self.assertTrue(actions[0].text().startswith(t('shortcuts.act.new_terminal')))
            self.assertEqual([action.text() for action in actions[1:]], [
                t('ctx.quick_commands'), t('ctx.local_quick_commands'),
                t('ctx.open_current_dir'), t('ctx.copy_current_dir')])
            submenu = actions[2].menu()
            submenu.aboutToShow.emit()
            entries.append(submenu.command_list.item(0).data(Qt.ItemDataRole.UserRole))

        with mock.patch.object(QMenu, 'exec', browse):
            tabs.contextMenuEvent(event)
        self.assertEqual(tabs.count(), 0)
        with mock.patch.object(self.TerminalWidget, 'start_process') as start, \
             mock.patch.object(self.TerminalWidget, '_execute_commands') as execute:
            entries[0].run()
            entries[0].run()  # A stale menu must never launch a second shell.
            start.assert_called_once_with([get_terminal_shell()], cwd=win._window_cwd)
            execute.assert_called_once_with(['echo probe'])
        self.assertEqual(tabs.count(), 1)

    def test_close_window_only_after_tabs_are_closed(self):
        win = self.win
        with mock.patch.object(win, '_focus_in_editor_area', return_value=False), \
             mock.patch.object(win, 'close') as close, \
             mock.patch.object(win, '_confirm_window_close', side_effect=[False, True]) as confirm:
            win._close_tab_or_window()
            self.assertEqual(win.tab_widget.count(), 0)
            close.assert_not_called()
            self.assertFalse(win._force_closing)
            win._close_tab_or_window()
            confirm.assert_called_once()
            close.assert_not_called()
            self.assertEqual(win.tab_widget.count(), 0)
            win._close_tab_or_window()
            close.assert_called_once()


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

    def test_close_last_tab_leaves_empty_window_and_can_reopen(self):
        win = self.win
        self.assertEqual(win.tab_widget.count(), 1)
        old_term = win.tab_terminals[0][0]
        win._close_tab(0)
        _flush(self.app)
        self.assertEqual(win.tab_widget.count(), 0)
        self.assertIsNone(win.terminal)
        self.assertTrue(sip.isdeleted(old_term))
        win._new_terminal()
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
