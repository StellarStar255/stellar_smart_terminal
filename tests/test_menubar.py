# -*- coding: utf-8 -*-
"""原生菜单栏（文件 / 视图 / 终端 / 窗口 / 帮助）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_menubar.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication


class TestMenuBar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window
        cls.win = main_window.MainWindow()
        cls.win.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtGui import QCloseEvent
        for w in [w for w in cls.app.topLevelWidgets()
                  if isinstance(w, cls.mw.MainWindow) and not sip.isdeleted(w)]:
            if getattr(w, '_closing_in_progress', False):
                continue
            w._force_closing = True
            QApplication.sendEvent(w, QCloseEvent())
            w.deleteLater()
        cls.app.processEvents()
        del cls.win

    def _menus(self):
        return [a.menu() for a in self.win.menuBar().actions() if a.menu() is not None]

    def test_five_menus_in_order(self):
        from i18n import t
        titles = [m.title() for m in self._menus()]
        self.assertEqual(titles, [t("menu.file"), t("menu.view"), t("menu.terminal"),
                                  t("window.menu"), t("menu.help")])

    def test_shortcut_actions_are_shared_not_duplicated(self):
        """菜单里带快捷键的项必须就是 _setup_shortcuts 建的那个 QAction，
        否则同键位两个 QAction → Qt 报歧义、两个都不触发。"""
        file_menu = self._menus()[0]
        self.assertIn(self.win.shortcut_actions['new_tab'], file_menu.actions())
        self.assertIn(self.win.shortcut_actions['close_tab'], file_menu.actions())
        seqs = {}
        for act in self.win.findChildren(type(self.win.shortcut_actions['new_tab'])):
            for seq in act.shortcuts():
                key = seq.toString()
                if not key:
                    continue
                self.assertNotIn(key, seqs, f"duplicate shortcut {key}")
                seqs[key] = act

    def test_rebuild_keeps_single_copy_of_actions(self):
        n_before = len(self.win.menuBar().actions())
        cycle = self.win._window_menu_actions
        self.win._rebuild_menus()
        self.app.processEvents()
        self.assertEqual(len(self.win.menuBar().actions()), n_before)
        self.assertIs(self.win._window_menu_actions, cycle)  # 应用级快捷键只建一次
        self.test_shortcut_actions_are_shared_not_duplicated()

    def test_recent_dirs_menu_lists_history(self):
        from PyQt6.QtWidgets import QMenu
        self.win.working_dir_history = ['/tmp/one', '/tmp/two']
        m = QMenu()
        self.win._fill_recent_dirs_menu(m)
        self.assertEqual([a.text() for a in m.actions()], ['/tmp/one', '/tmp/two'])
        self.win.working_dir_history = []
        self.win._fill_recent_dirs_menu(m)
        self.assertEqual(len(m.actions()), 1)
        self.assertFalse(m.actions()[0].isEnabled())

    def test_open_new_window_creates_tracked_window(self):
        before = [w for w in self.app.topLevelWidgets() if isinstance(w, self.mw.MainWindow)]
        win = self.win._open_new_window()
        self.assertIsNotNone(win)
        self.assertIn(win, self.win.detached_windows)
        after = [w for w in self.app.topLevelWidgets() if isinstance(w, self.mw.MainWindow)]
        self.assertEqual(len(after), len(before) + 1)


class TestTerminalMenuReservedKeys(unittest.TestCase):
    """macOS 上 Cmd+N / Cmd+O 放给菜单栏；物理 Ctrl+N/O 仍归 shell。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from terminal_widget import TerminalWidget
        cls.term = TerminalWidget()

    @classmethod
    def tearDownClass(cls):
        cls.term.cleanup()
        cls.term.deleteLater()
        cls.app.processEvents()

    def test_reserved_on_darwin(self):
        cmd = Qt.KeyboardModifier.ControlModifier
        ctrl = Qt.KeyboardModifier.MetaModifier
        with mock.patch('terminal_input.sys.platform', 'darwin'):
            self.assertTrue(self.term._is_menu_reserved_combo(Qt.Key.Key_N, cmd))
            self.assertTrue(self.term._is_menu_reserved_combo(Qt.Key.Key_O, cmd | Qt.KeyboardModifier.ShiftModifier))
            self.assertFalse(self.term._is_menu_reserved_combo(Qt.Key.Key_N, ctrl))
            self.assertFalse(self.term._is_menu_reserved_combo(Qt.Key.Key_T, cmd))
        with mock.patch('terminal_input.sys.platform', 'win32'):
            self.assertFalse(self.term._is_menu_reserved_combo(Qt.Key.Key_N, cmd))


if __name__ == '__main__':
    unittest.main()
