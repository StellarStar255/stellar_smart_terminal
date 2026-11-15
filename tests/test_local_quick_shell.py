"""Local command menus contain user presets; new terminals choose their own shell."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication, QMenu
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
