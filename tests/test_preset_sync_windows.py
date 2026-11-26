"""预设是全局配置：一个窗口改了预设，其它已开窗口的下拉框要立刻跟上。

以前每个窗口启动时各读一份预设进内存，管理框只刷新自己那个窗口，
其它窗口要重启才能看到新预设（用户在双窗口下实测）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_preset_sync_windows.py -v
"""
import os
import sys
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def _preset(name):
    return {'name': name, 'command': f'echo {name}', 'working_dir': ''}


class PresetSyncAcrossWindowsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import main_window
        self.windows = []
        for _ in range(2):
            w = main_window.MainWindow()
            w.presets = [_preset('zsh'), _preset('Claude')]
            w._presets_modified = False
            w.last_preset_index = 1
            w._populate_presets()
            w.show()
            self.windows.append(w)
        self.app.processEvents()

    def tearDown(self):
        for w in self.windows:
            w.close()
            w.deleteLater()
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    @staticmethod
    def _names(w):
        return [w.preset_combo.itemText(i) for i in range(w.preset_combo.count())]

    def test_new_preset_shows_up_in_other_window(self):
        a, b = self.windows
        self.assertEqual(b.preset_combo.currentText(), 'Claude')

        a._apply_edited_presets([_preset('zsh'), _preset('Claude'),
                                 _preset('Claude (with proxy 10808)')])

        self.assertIn('Claude (with proxy 10808)', self._names(b))
        self.assertEqual(b.presets, a.presets)
        self.assertIsNot(b.presets, a.presets)          # 各窗口独立副本
        # 接收方只是采用磁盘内容，不能被标成「本窗口改过」
        self.assertFalse(b._presets_modified)
        # 成功落盘后清除脏标记，防止退出时重新覆盖磁盘新版本。
        self.assertFalse(a._presets_modified)
        # B 窗口原先选着 Claude，同步后仍选 Claude
        self.assertEqual(b.preset_combo.currentText(), 'Claude')

    def test_palette_finds_quick_commands_in_current_project_and_active_terminal(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from i18n import t

        window, other = self.windows
        window.active_terminal = window.terminal
        window.presets = [{'name': 'Attach globally', 'commands': ['tmux attach -t global']}]
        window.local_presets = [{'name': 'Training', 'commands': ['tmux attach -t project_one']}]
        palette = window.command_palette
        results = palette._rank('tmux attach')
        self.assertEqual({entry.group for entry in results},
                         {t('palette.quick_commands_global'), t('ctx.local_quick_commands')})
        stale, = palette._rank('project_one')
        window._window_cwd = '/tmp/second-project'
        window.local_presets = [{'name': 'Training', 'commands': ['tmux attach -t project_two']}]

        with patch.object(window.terminal, '_execute_commands') as execute, \
                patch.object(other.terminal, '_execute_commands') as other_execute:
            stale.run()
            execute.assert_not_called()
            self.assertEqual(palette._rank('project_one'), [])
            window.activateWindow()
            window.terminal.setFocus()
            self.app.processEvents()
            if sys.platform == 'darwin':
                QTest.keyClick(window.terminal, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
                self.assertTrue(palette.line_edit.hasFocus())
            else:
                palette.focus_search()
            palette.line_edit.setText('project_two')
            self.assertEqual(palette.list_widget.count(), 1)
            QTest.keyClick(palette.line_edit, Qt.Key.Key_Return)
            execute.assert_called_once_with(['tmux attach -t project_two'])
            other_execute.assert_not_called()

    def test_open_command_menu_rejects_replaced_preset_and_keeps_duplicate_target(self):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QMenu
        from terminal_mouse import _QuickCommandMenu

        window = self.windows[0]
        terminal = window.terminal
        window.activateWindow()
        terminal.setFocus()
        self.app.processEvents()
        parent = QMenu(terminal)
        def open_results():
            menu = _QuickCommandMenu('Local', parent)
            parent.addMenu(menu)
            terminal._populate_local_commands_menu(menu)
            menu.addAction('Manage')
            terminal._fit_quick_command_menu(menu, QPoint(30, 30))
            parent.popup(QPoint(30, 30))
            menu.popup(QPoint(200, 30))
            self.app.processEvents()
            menu.search_box.setText('duplicate')
            return menu

        window.local_presets = [{'name': 'duplicate', 'commands': ['echo first']},
                                {'name': 'duplicate', 'commands': ['echo second']}]
        with patch.object(terminal, '_execute_commands') as execute:
            menu = open_results()
            QTest.keyClick(menu.search_box, Qt.Key.Key_Down)
            QTest.keyClick(menu.search_box, Qt.Key.Key_Return)
            execute.assert_called_once_with(['echo second'])
            execute.reset_mock()
            menu = open_results()
            window.local_presets = [{'name': 'duplicate', 'commands': ['echo replacement']}]
            QTest.keyClick(menu.search_box, Qt.Key.Key_Return)
            execute.assert_not_called()

    def test_palette_rechecks_command_contents_before_execution(self):
        window = self.windows[0]
        preset = {'name': 'same title', 'commands': ['echo original']}
        window.presets = [preset]
        entry, = window.command_palette._rank('echo original')
        preset['commands'][0] = 'echo edited'
        with patch.object(window.terminal, '_execute_commands') as execute:
            entry.run()
            execute.assert_not_called()
            current, = window.command_palette._rank('echo edited')
            current.run()
            execute.assert_called_once_with(['echo edited'])

    def test_open_result_does_not_follow_tab_or_restarted_session(self):
        from i18n import t

        window = self.windows[0]
        first = window.terminal
        window.presets = [{'name': 'Target test', 'commands': ['echo expected']}]
        entry, = window.command_palette._rank('echo expected')
        with patch('terminal_widget.TerminalWidget.start_process'):
            window.shortcut_actions['new_terminal'].trigger()
        window._force_closing = True
        other = window.terminal
        self.assertIsNot(first, other)
        with patch.object(first, '_execute_commands') as first_run, \
                patch.object(other, '_execute_commands') as other_run:
            entry.run()
            first_run.assert_not_called()
            other_run.assert_not_called()
            self.assertEqual(window.statusbar.currentMessage(), t("palette.selection_changed"))
            current, = window.command_palette._rank('echo expected')
            with patch.object(other, '_process_start_token', object()):
                current.run()
                other_run.assert_not_called()

    def test_insert_rechecks_body_after_line_picker(self):
        window = self.windows[0]
        terminal = window.terminal
        preset = {'name': 'Choose line', 'commands': ['echo one', 'echo two']}
        window.presets = [preset]

        def pick(*args):
            preset['commands'].remove('echo two')
            return 'echo two', True

        with patch.object(terminal, 'is_running', return_value=True), \
                patch.object(terminal, 'insert_command_line') as insert, \
                patch('main_window.QInputDialog.getItem', side_effect=pick) as picker:
            entry, = window.command_palette._rank('echo two')
            entry.insert()
            picker.assert_called_once()
            insert.assert_not_called()

    def test_open_result_survives_completion_of_its_own_pending_start(self):
        window = self.windows[0]
        terminal = window.terminal
        window.presets = [{'name': 'Startup', 'commands': ['echo ready']}]
        with patch.object(terminal, '_process_start_token', object()), \
                patch.object(terminal, '_pending_process_start', object()), \
                patch.object(terminal, '_backend', None), \
                patch.object(terminal, '_execute_commands') as execute:
            entry, = window.command_palette._rank('echo ready')
            terminal._pending_process_start = None
            entry.run()  # Start failed, leaving no backend.
            execute.assert_not_called()
            terminal._backend = Mock(is_running=True)
            entry.run()
            execute.assert_called_once_with(['echo ready'])
            live_entry, = window.command_palette._rank('echo ready')
            terminal._backend.is_running = False
            live_entry.run()
            execute.assert_called_once_with(['echo ready'])

    def test_palette_preedit_does_not_run_current_result(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QInputMethodEvent
        from PyQt6.QtTest import QTest

        window = self.windows[0]
        window.presets = [{'name': 'IME验证', 'commands': ['echo selected']}]
        window.activateWindow()
        self.app.processEvents()
        palette = window.command_palette
        palette.focus_search()
        field = palette.line_edit
        field.setText('IME')
        with patch.object(window.terminal, '_execute_commands') as execute:
            self.app.sendEvent(field, QInputMethodEvent('yan', []))
            QTest.keyClick(field, Qt.Key.Key_Return)
            execute.assert_not_called()
            self.assertTrue(palette.popup.isVisible())
            commit = QInputMethodEvent()
            commit.setCommitString('验证')
            self.app.sendEvent(field, commit)
            self.assertEqual(field.text(), 'IME验证')
            QTest.keyClick(field, Qt.Key.Key_Return)
            execute.assert_called_once_with(['echo selected'])

    def test_palette_inserts_one_line_without_submitting(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        window = self.windows[0]
        term = window.terminal
        term.input_buffer = ''
        window.presets = [{'name': 'Prepare', 'commands': ['echo one', 'echo two']}]
        palette = window.command_palette
        backend = SimpleNamespace(is_running=True, write=Mock(return_value=True))
        window.activateWindow()
        term.setFocus()
        self.app.processEvents()
        with patch.object(term, '_backend', backend), \
                patch.object(term, '_is_bracketed_paste_enabled', return_value=False), \
                patch.object(term, '_execute_commands') as execute, \
                patch('main_window.QInputDialog.getItem', return_value=('echo two', True)) as pick:
            palette.focus_search()
            palette.line_edit.setText('Prepare')
            QTest.keyClick(palette.line_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
            self.assertEqual(pick.call_args.args[3], ['echo one', 'echo two'])
            backend.write.assert_called_once_with(b'echo two')
            self.assertEqual(term.input_buffer, 'echo two')
            for text in ('echo bad\n', 'echo bad\r', 'echo bad\x1b', 'echo bad\t'):
                self.assertFalse(term.insert_command_line(text))
            backend.write.assert_called_once()
            execute.assert_not_called()

    def test_palette_shows_tab_pane_and_foreground_ssh(self):
        from PyQt6.QtCore import QThreadPool
        from PyQt6.QtTest import QTest
        from ssh_session import HostConfig

        window = self.windows[0]
        term = window.terminal
        window.tab_widget.setTabText(window.tab_widget.currentIndex(), 'Experiments')
        term.set_split_label('Training')
        window.presets = [{'name': 'Status', 'commands': ['echo status']}]
        host = HostConfig(alias='training', hostname='host.test', user='user')
        palette = window.command_palette
        window.activateWindow()
        term.setFocus()
        self.app.processEvents()
        backend = SimpleNamespace(_child_pid=21, is_running=True)
        with patch.object(term, '_backend', backend), \
                patch.object(window, '_quick_command_foreground_group', return_value=42) as group, \
                patch.object(window, '_known_remote_hosts', return_value=[host]), \
                patch.object(window, '_ssh_args_under', return_value='ssh user@host.test') as find:
            palette.focus_search()
            palette.line_edit.setText('Status')
            self.assertTrue(QThreadPool.globalInstance().waitForDone(1500))
            self.app.processEvents()
            self.assertIn('Experiments', palette.target_label.text())
            self.assertIn('1 (Training)', palette.target_label.text())
            self.assertIn('SSH training', palette.target_label.text())
            find.assert_called_once_with(21, foreground_group=42)
            group.return_value = 63
            for _ in range(20):
                if 'SSH training' not in palette.target_label.text():
                    break
                QTest.qWait(40)
            self.assertNotIn('SSH training', palette.target_label.text())
            palette._clear_and_hide()

    def test_palette_edit_selects_exact_local_preset_and_saves_it(self):
        from PyQt6.QtWidgets import QDialog
        from dialogs import PresetDialog

        window = self.windows[0]
        term = window.terminal
        palette = window.command_palette
        window.activateWindow()
        term.setFocus()
        self.app.processEvents()
        window.local_presets = [
            {'name': 'Port', 'commands': ['echo 6006']},
            {'name': 'Port', 'commands': ['echo 7070'], 'pinned': True},
        ]

        def edit(dialog):
            self.assertEqual(dialog.preset_list.currentRow(), 1)
            self.assertEqual(dialog.commands_edit.toPlainText(), 'echo 7070')
            dialog.commands_edit.setPlainText('echo 8120')
            dialog.accept()
            return QDialog.DialogCode.Accepted

        with TemporaryDirectory() as project, patch.object(window, '_window_cwd', project), \
                patch.object(PresetDialog, 'exec', edit), \
                patch.object(term, '_execute_commands') as execute:
            palette.focus_search()
            palette.line_edit.setText('7070')
            palette.edit_button.click()
            execute.assert_not_called()
            window._load_local_commands()
            self.assertEqual(window.local_presets, [
                {'name': 'Port', 'commands': ['echo 6006']},
                {'name': 'Port', 'commands': ['echo 8120'], 'pinned': True},
            ])

    def test_selection_falls_back_when_current_preset_deleted(self):
        a, b = self.windows
        a._apply_edited_presets([_preset('zsh')])
        self.assertEqual(self._names(b), ['zsh'])
        self.assertEqual(b.preset_combo.currentIndex(), 0)

    def test_selection_syncs_both_directions_without_restarting_sessions(self):
        import app_config
        a, b = self.windows
        with patch.object(a, '_start_session') as start_a, \
                patch.object(b, '_start_session') as start_b:
            a.preset_combo.setCurrentIndex(0)
            self.assertEqual(b.preset_combo.currentText(), 'zsh')
            self.assertEqual(b.last_preset_index, 0)
            b.preset_combo.setCurrentIndex(1)
            self.assertEqual(a.preset_combo.currentText(), 'Claude')
            self.assertEqual(a.last_preset_index, 1)
            self.assertEqual(app_config.read_config()['last_preset_index'], 1)
            start_a.assert_not_called()
            start_b.assert_not_called()

    def test_populating_combo_does_not_broadcast_intermediate_selection(self):
        a, b = self.windows
        a._populate_presets()
        self.assertEqual(a.preset_combo.currentText(), 'Claude')
        self.assertEqual(b.preset_combo.currentText(), 'Claude')

    def test_old_workspace_selection_does_not_override_global_default(self):
        a, b = self.windows
        a._apply_restored_window_state({'preset_name': 'zsh'})
        self.assertEqual(a.preset_combo.currentText(), 'Claude')
        self.assertEqual(b.preset_combo.currentText(), 'Claude')


if __name__ == '__main__':
    unittest.main()
