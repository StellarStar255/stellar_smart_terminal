"""Local command menus contain user presets; new terminals choose their own shell."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QAction, QContextMenuEvent, QInputMethodEvent
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
        submenu.search_toggle.trigger()
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

    def _open_project_menu_with_keyboard(self):
        self.w.show()
        self.w.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.w))
        self.w.local_quick_commands_provider = lambda: [
            {'name': 'Other', 'commands': ['echo other']},
            {'name': '中文', 'commands': ['echo selected']}]
        self.w._quick_command_search = {'local': True}
        self.w._add_quick_command_menu(
            self.menu, QPoint(30, 30), 'ctx.local_quick_commands',
            self.w._populate_local_commands_menu, self.w.add_local_command_requested,
            self.w.manage_local_presets_requested, 'ctx.manage_local_presets')
        self.menu.popup(QPoint(30, 30))
        action = self.menu.actions()[0]
        self.menu.setActiveAction(action)
        QTest.keyClick(self.menu, Qt.Key.Key_Right)
        self.app.processEvents()
        self.assertIs(self.app.focusWidget(), action.menu().search_box)
        return action.menu()

    def test_hover_moves_selection_and_keyboard_uses_highlighted_command(self):
        menu = self._open_project_menu_with_keyboard()
        listing = menu.command_list
        self.assertTrue(QTest.qWaitForWindowExposed(menu))
        for row in (0, 1):
            point = listing.visualItemRect(listing.item(row)).center()
            QTest.mouseMove(menu.windowHandle(), listing.viewport().mapTo(menu, point))
        self.assertEqual(listing.selectedItems(), [listing.item(1)])
        self.w._execute_commands.assert_not_called()
        QTest.keyClick(menu.search_box, Qt.Key.Key_Up)
        self.assertEqual(listing.selectedItems(), [listing.item(0)])
        QTest.keyClick(menu.search_box, Qt.Key.Key_Return)
        self.w._execute_commands.assert_called_once_with(['echo other'])

    def test_preedit_return_preserves_menu_until_committed_query_is_confirmed(self):
        menu = self._open_project_menu_with_keyboard()
        field = menu.search_box
        self.app.sendEvent(field, QInputMethodEvent('zhong', []))
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Down)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Return)
        self.assertTrue(menu.isVisible())
        self.assertEqual(menu.command_list.currentRow(), 0)
        self.w._execute_commands.assert_not_called()
        commit = QInputMethodEvent()
        commit.setCommitString('中文')
        self.app.sendEvent(field, commit)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Backspace)
        self.assertEqual(field.text(), '中')
        self.assertEqual(menu.command_list.count(), 1)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Return)
        self.w._execute_commands.assert_called_once_with(['echo selected'])

    def test_down_from_filtered_results_reaches_footer_in_one_press(self):
        managed = Mock()
        self.w.manage_local_presets_requested.connect(managed)
        for query in ('other', 'missing'):
            with self.subTest(query=query):
                menu = self._open_project_menu_with_keyboard()
                QTest.keyClicks(self.app.focusWidget(), query)
                QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Down)
                self.assertIs(menu.activeAction(), menu.actions()[-3])
                QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Down)
                self.assertIs(menu.activeAction(), menu.actions()[-2])
                QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Return)
                self.w._execute_commands.assert_not_called()
                self.menu.clear()
        self.assertEqual(managed.call_count, 2)

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
            if not global_menu.search_toggle.isChecked():
                global_menu.search_toggle.trigger()
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

    def test_add_command_receives_click_after_search_is_disabled(self):
        self.w.show()
        self.w.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.w))
        presets = [{'name': f'Command {i}', 'commands': ['echo ok']} for i in range(100)]
        self.w.local_quick_commands_provider = lambda: presets
        add = Mock()
        self.w.add_local_command_requested.connect(add)
        pos = self.app.primaryScreen().availableGeometry().bottomRight() - QPoint(400, 90)
        self.w._add_quick_command_menu(self.menu, pos, 'ctx.local_quick_commands',
                                       self.w._populate_local_commands_menu, self.w.add_local_command_requested,
                                       self.w.manage_local_presets_requested, 'ctx.manage_local_presets')
        menu = self.menu.actions()[0].menu()
        self.menu.popup(pos)
        self.menu.setActiveAction(menu.menuAction())
        QTest.keyClick(self.menu, Qt.Key.Key_Right)
        self.assertTrue(QTest.qWaitForWindowExposed(menu))
        for enabled in (True, False):
            point = menu.actionGeometry(menu.search_toggle).center()
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=point)
            self.app.processEvents()
            self.assertEqual(menu.search_box.isVisible(), enabled)
        point = menu.mapToGlobal(menu.actionGeometry(menu.footer_actions[0]).center())
        target = self.app.widgetAt(point)
        self.assertIs(target, menu)
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, pos=target.mapFromGlobal(point))
        add.assert_called_once_with()
        self.w._execute_commands.assert_not_called()

    def test_long_submenu_scrolls_inside_popup_and_runs_last_preset(self):
        self.w.show()
        self.w.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(self.w))
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
        self.menu.setActiveAction(submenu.menuAction())
        QTest.keyClick(self.menu, Qt.Key.Key_Right)
        self.assertTrue(QTest.qWaitForWindowExposed(submenu))
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
            footer = [add_action, manage_action, submenu.search_toggle]
            for enabled in (True, False, True, False):
                toggle_rect = submenu.actionGeometry(submenu.search_toggle)
                QTest.mouseClick(submenu, Qt.MouseButton.LeftButton, pos=toggle_rect.center())
                self.app.processEvents()
                self.assertTrue(submenu.isVisible())
                self.assertEqual(submenu.search_box.isVisible(), enabled)
                for action in footer:
                    rect = submenu.actionGeometry(action)
                    self.assertFalse(rect.isEmpty())
                    self.assertTrue(submenu.rect().contains(rect), (action.text(), rect, submenu.rect()))
                    self.assertFalse(listing.geometry().intersects(rect))
                    self.assertIsNone(submenu.childAt(rect.center()))
                    self.assertIs(submenu.actionAt(rect.center()), action)
                    self.assertTrue(screen.availableGeometry().contains(submenu.mapToGlobal(rect.bottomRight())))
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
