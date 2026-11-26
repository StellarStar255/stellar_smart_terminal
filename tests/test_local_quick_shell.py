"""Local command menus contain user presets; new terminals choose their own shell."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QAction, QContextMenuEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu
from terminal_mouse import _QuickCommandMenu
from terminal_widget import TerminalWidget


class TestLocalQuickShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = TerminalWidget()
        self.w._execute_commands = Mock()
        self.menu = QMenu(self.w)

    def tearDown(self):
        self.w.cleanup()
        self.w.deleteLater()

    def test_platform_shells(self):
        from dialogs import get_terminal_shell
        cases = [
            ('darwin', '/bin/zsh', {'zsh'}, 'zsh'),
            ('linux', '/bin/bash', {'bash'}, 'bash'),
            ('linux', '/missing/shell', {'bash'}, 'bash'),
            ('linux', '', set(), 'sh'),
            ('win32', '', {'pwsh.exe', 'powershell.exe'}, 'pwsh.exe'),
            ('win32', '', {'powershell.exe'}, 'powershell.exe'),
            ('win32', '', {'cmd.exe'}, 'cmd.exe'),
        ]
        for platform, login_shell, available, expected in cases:
            with self.subTest(platform=platform, available=available):
                with patch('dialogs.sys.platform', platform), \
                     patch.dict(os.environ, {'SHELL': login_shell}), \
                     patch('dialogs.shutil.which',
                           side_effect=lambda name: name if name in available else None):
                    self.assertEqual(get_terminal_shell(), expected)

    def test_empty_project_has_no_injected_shell_or_separator(self):
        for provider in (None, lambda: []):
            with self.subTest(provider=provider):
                self.menu.clear()
                self.w.local_quick_commands_provider = provider
                self.w._populate_local_commands_menu(self.menu)
                self.assertEqual(self.menu.actions(), [])

    def test_project_commands_remain_separate_and_unchanged(self):
        presets = [{'name': 'Project command', 'commands': ['echo one', 'echo two']}]
        self.w.local_quick_commands_provider = lambda: presets
        self.w._populate_local_commands_menu(self.menu)
        actions = [a for a in self.menu.actions() if not a.isSeparator()]
        self.assertEqual([a.text() for a in actions], ['Project command'])
        actions[0].trigger()
        self.w._execute_commands.assert_called_once_with(['echo one', 'echo two'])
        self.assertEqual(presets, [
            {'name': 'Project command', 'commands': ['echo one', 'echo two']}])

    def test_user_can_keep_an_explicit_shell_preset(self):
        # Exercise the existing command execution path without starting a real shell.
        del self.w._execute_commands
        self.w._backend = Mock(is_running=True)
        self.w._write_to_backend = Mock(return_value=True)
        self.w.local_quick_commands_provider = lambda: [
            {'name': 'My PowerShell', 'commands': ['powershell.exe']}]
        self.w._populate_local_commands_menu(self.menu)
        self.menu.actions()[0].trigger()
        self.w._write_to_backend.assert_called_once_with(b'powershell.exe\r')
        self.w._backend = None

    def test_filter_matches_command_body_and_runs_keyboard_selection(self):
        self.w.show()
        self.w.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.w))
        submenu = _QuickCommandMenu('Local commands', self.menu)
        self.menu.addMenu(submenu)
        self.w.local_quick_commands_provider = lambda: [
            {'name': 'One', 'commands': ['git status']},
            {'name': '二号', 'commands': ['git diff']},
            {'name': 'Other', 'commands': ['echo hello']}]
        self.w._populate_local_commands_menu(submenu)
        submenu.addAction('Manage')
        self.w._fit_quick_command_menu(submenu, QPoint(30, 30))
        self.menu.popup(QPoint(30, 30))
        submenu.popup(QPoint(30 + self.menu.width(), 30))
        self.app.processEvents()
        search = submenu.search_box
        QTest.keyClicks(submenu, 'xyz')
        self.assertEqual(search.text(), 'xyz')
        self.assertTrue(submenu.isVisible())
        QTest.keyClick(search, Qt.Key.Key_Return)
        self.w._execute_commands.assert_not_called()
        self.assertTrue(submenu.isVisible())
        search.setText('二号')
        self.assertEqual(submenu.command_list.item(0).text(), '二号')
        search.clear()
        QTest.keyClicks(search, 'GIT')
        listing = submenu.command_list
        self.assertEqual([listing.item(i).text() for i in range(listing.count())], ['二号', 'One'])
        QTest.keyClick(search, Qt.Key.Key_Down)
        QTest.keyClick(search, Qt.Key.Key_Return)
        self.w._execute_commands.assert_called_once_with(['git status'])
        self.assertFalse(submenu.isVisible())

    def test_context_menu_defers_presets_and_releases_actions(self):
        source = Mock(return_value=[{'name': 'Local', 'commands': ['echo local']}])
        self.w.local_quick_commands_provider = source
        self.w.quick_commands_provider = lambda: [{'name': 'Global', 'commands': ['echo global']}]
        self.w.show()
        self.w.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.w))
        before = len(self.w.findChildren(QAction))
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10))

        def exercise(menu, pos):
            menus = [a.menu() for a in menu.actions() if isinstance(a.menu(), _QuickCommandMenu)]
            global_menu, local_menu = menus
            self.assertIsNone(global_menu.command_list)
            self.assertIsNone(local_menu.command_list)
            menu.popup(pos)
            for _ in range(2):
                local_menu.popup(pos + QPoint(menu.width(), 0))
                self.app.processEvents()
                self.assertEqual(local_menu.command_list.count(), 1)
                self.assertEqual(local_menu.command_list.item(0).text(), 'Local')
                local_menu.hide()
            self.assertIsNone(global_menu.command_list)
            global_menu.popup(pos + QPoint(menu.width(), 0))
            self.app.processEvents()
            QTest.keyClicks(global_menu.search_box, 'global')
            QTest.keyClick(global_menu.search_box, Qt.Key.Key_Return)
            self.w._execute_commands.assert_called_with(['echo global'])
            self.assertFalse(menu.isVisible())

        with patch.object(QMenu, 'exec', exercise):
            for _ in range(3):
                self.w.contextMenuEvent(event)
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertEqual(len(self.w.findChildren(QAction)), before)
        self.assertEqual(source.call_count, 3)
        self.assertEqual(self.w._execute_commands.call_count, 3)

    def test_long_submenu_scrolls_inside_popup_and_runs_last_preset(self):
        long_name = 'Long command 中文 & ' * 20
        presets = [{'name': f'Command {i}', 'commands': [f'echo {i}']} for i in range(100)]
        presets[-1]['name'] = long_name.strip()
        presets[3]['pinned'] = True
        presets[9]['pinned'] = True
        self.w.local_quick_commands_provider = lambda: presets
        submenu = _QuickCommandMenu('Local commands', self.menu)
        self.menu.addMenu(submenu)
        self.w._populate_local_commands_menu(submenu)
        manage = Mock()
        add_action = submenu.addAction('Add')
        manage_action = submenu.addAction('Manage', manage)
        pos = QPoint(30, 30)
        self.w._fit_quick_command_menu(submenu, pos)

        self.menu.popup(pos)
        submenu.popup(pos + QPoint(self.menu.width(), 0))
        self.app.processEvents()
        try:
            listing = submenu.command_list
            screen = self.app.screenAt(pos) or self.w.window().screen()
            budget = screen.availableGeometry().height() * 85 // 100
            self.assertAlmostEqual(submenu.height(), budget, delta=submenu.fontMetrics().height())
            self.assertEqual([listing.item(i).text() for i in range(4)],
                             ['★  Command 3', '★  Command 9', 'Command 0', 'Command 1'])
            self.assertEqual(listing.count(), len(presets))
            scrollbar = listing.verticalScrollBar()
            self.assertGreater(scrollbar.maximum(), 0)
            geometry = submenu.geometry()
            controls = [submenu.actionGeometry(action) for action in (add_action, manage_action)]
            self.assertTrue(all(submenu.rect().contains(rect) for rect in controls))
            scrollbar.setValue(scrollbar.maximum())
            self.app.processEvents()
            self.assertTrue(submenu.isVisible())
            self.assertEqual(submenu.geometry(), geometry)
            self.assertEqual(controls, [submenu.actionGeometry(action)
                                        for action in (add_action, manage_action)])

            last_command = listing.item(len(presets) - 1)
            self.assertEqual(last_command.toolTip(), presets[-1]['name'])
            visible_row = listing.visualItemRect(last_command).intersected(listing.viewport().rect())
            self.assertFalse(visible_row.isEmpty())
            QTest.mouseClick(listing.viewport(), Qt.MouseButton.LeftButton, pos=visible_row.center())
            self.w._execute_commands.assert_called_once_with(['echo 99'])
            manage.assert_not_called()
            self.assertFalse(self.menu.isVisible())
            self.assertFalse(submenu.isVisible())
        finally:
            submenu.close()
            self.menu.close()
