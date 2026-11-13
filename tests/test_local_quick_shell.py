"""Built-in local Shell action, platform selection and existing project commands."""
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
                with patch('terminal_widget.sys.platform', platform), \
                     patch.dict(os.environ, {'SHELL': login_shell}), \
                     patch('terminal_widget.shutil.which',
                           side_effect=lambda name: name if name in available else None):
                    self.assertEqual(self.w._get_local_shell_command(), expected)

    def test_empty_project_still_has_executable_shell(self):
        for provider in (None, lambda: []):
            with self.subTest(provider=provider):
                self.menu.clear()
                self.w.local_quick_commands_provider = provider
                with patch.object(self.w, '_get_local_shell_command', return_value='zsh'):
                    self.w._populate_local_commands_menu(self.menu)
                action = self.menu.actions()[0]
                self.assertEqual(action.text(), 'zsh')
                self.assertTrue(action.isEnabled())
                action.trigger()
                self.w._execute_commands.assert_called_with(['zsh'])

    def test_project_commands_remain_separate_and_unchanged(self):
        presets = [{'name': 'Project command', 'commands': ['echo one', 'echo two']}]
        self.w.local_quick_commands_provider = lambda: presets
        with patch.object(self.w, '_get_local_shell_command', return_value='bash'):
            self.w._populate_local_commands_menu(self.menu)
        actions = [a for a in self.menu.actions() if not a.isSeparator()]
        self.assertEqual([a.text() for a in actions], ['bash', 'Project command'])
        actions[1].trigger()
        self.w._execute_commands.assert_called_once_with(['echo one', 'echo two'])
        self.assertEqual(presets, [
            {'name': 'Project command', 'commands': ['echo one', 'echo two']}])

    def test_running_terminal_receives_shell_command(self):
        # Exercise the existing command execution path without starting a real shell.
        del self.w._execute_commands
        self.w._backend = Mock(is_running=True)
        self.w._write_to_backend = Mock(return_value=True)
        with patch.object(self.w, '_get_local_shell_command', return_value='powershell.exe'):
            self.w._populate_local_commands_menu(self.menu)
        self.menu.actions()[0].trigger()
        self.w._write_to_backend.assert_called_once_with(b'powershell.exe\r')
        self.w._backend = None
