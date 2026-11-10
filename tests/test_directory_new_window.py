"""目录栏的新窗口入口：真实分离、路径记忆及错误处理，禁用外部 shell。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication


class DirectoryNewWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.MW = main_window.MainWindow

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='directory-window-')
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'STELLAR_DATA_DIR': self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        import app_config
        self.config = app_config.get_config_path()
        self.config.write_text('{"onboarding_shown":true,"auto_update_check":false,'
                               '"workspace_restore_enabled":false}', encoding='utf-8')
        config_patch = patch.object(self.MW, 'CONFIG_FILE', self.config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.win = self.MW()
        self.win.show()
        self.started = []

        def start(window, cwd=None):
            self.started.append((cwd, window.presets[window.preset_combo.currentIndex()]))

        sessions = patch.object(self.MW, '_start_session', start)
        sessions.start()
        self.addCleanup(sessions.stop)
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in list(self.win.detached_windows) + [self.win]:
            window._force_closing = True
            QApplication.sendEvent(window, QCloseEvent())
            window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_new_window_uses_path_and_preset_and_persists_recent_directory(self):
        import app_config
        target = Path(self.tmp.name) / 'project with spaces'
        target.mkdir()
        original_cwd = self.win._window_cwd
        original_tabs = self.win.tab_widget.count()
        preset = {'name': 'Selected preset', 'commands': ['zsh', 'echo project']}
        self.win.presets = [preset]
        self.win._populate_presets()
        self.win.working_dir_combo.setCurrentText(str(target) + os.sep)
        self.win.open_dir_window_btn.click()
        QTest.qWait(450)
        self.assertEqual(len(self.win.detached_windows), 1)
        child = self.win.detached_windows[0]
        self.assertEqual(child._window_cwd, str(target))
        self.assertEqual(child.tab_cwds[0], str(target))
        self.assertEqual(self.started, [(str(target), preset)])
        self.assertEqual(self.win._window_cwd, original_cwd)
        self.assertEqual(self.win.tab_widget.count(), original_tabs)
        self.assertIn(str(target), app_config.read_config()['working_dir_history'])
        child._reload_dir_history_from_config()
        self.assertIn(str(target), child.working_dir_history)

    def test_invalid_path_does_not_open_or_remember_a_window(self):
        import app_config
        target = str(Path(self.tmp.name) / 'missing-project')
        self.win.working_dir_combo.setCurrentText(target)
        with patch.object(self.win, '_styled_message_box') as warning:
            self.win.open_dir_window_btn.click()
        QTest.qWait(150)
        warning.assert_called_once()
        self.assertEqual(self.win.detached_windows, [])
        self.assertNotIn(target, app_config.read_config().get('working_dir_history', []))
        self.assertEqual(self.started, [])

    def test_empty_path_does_not_start_a_session(self):
        self.win.working_dir_combo.setCurrentText('  ')
        self.win.open_dir_window_btn.click()
        QTest.qWait(150)
        self.assertEqual(self.started, [])
        self.assertEqual(self.win.detached_windows, [])

    def test_enter_with_history_suggestions_switches_to_typed_parent_directory(self):
        target = Path(self.tmp.name) / 'projects'
        child = target / 'recent-project'
        child.mkdir(parents=True)
        self.win._add_to_dir_history(str(child))
        self.win._toggle_explorer_panel()
        edit = self.win.working_dir_combo.lineEdit()
        edit.setFocus()
        edit.selectAll()
        QTest.keyClicks(edit, str(target) + os.sep)
        self.app.processEvents()
        edit.setText(str(target) + os.sep)
        self.win._dir_completer.setCompletionPrefix(edit.text())
        self.win._dir_completer.complete()
        self.app.processEvents()
        self.assertTrue(self.win._dir_completer.popup().isVisible())
        QTest.keyClick(self.win._dir_completer.popup(), Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(os.path.normpath(self.win._window_cwd), str(target))
        self.assertEqual(os.path.normpath(self.win.explorer_panel._current_path), str(target))
        self.assertFalse(self.win._dir_completer.popup().isVisible())

    def test_button_retranslates_at_runtime(self):
        import i18n
        language = i18n.get_language()
        try:
            for value, expected in [('en', 'Open as New Window'), ('zh', '在新窗口打开')]:
                i18n.set_language(value)
                self.win._apply_language()
                self.assertEqual(self.win.open_dir_window_btn.text(), expected)
                self.assertEqual(self.win.dir_back_btn.text(),
                                 '← Back' if value == 'en' else '← 返回')
        finally:
            i18n.set_language(language)

    def test_back_walks_directories_and_updates_explorer_and_terminal(self):
        original = self.win._window_cwd
        self.assertFalse(self.win.dir_back_btn.isEnabled())
        self.win._toggle_explorer_panel()
        first = Path(self.tmp.name) / 'first project'
        second = Path(self.tmp.name) / 'second'
        first.mkdir()
        second.mkdir()
        for target in (first, second, second):
            self.win.working_dir_combo.setCurrentText(str(target))
            self.win._apply_working_dir()
        with patch.object(self.win.active_terminal, 'set_working_dir') as change:
            self.win.dir_back_btn.click()
            change.assert_called_once_with(str(first))
        self.assertEqual(self.win._window_cwd, str(first))
        self.assertEqual(self.win.working_dir_combo.currentText(), str(first))
        self.assertEqual(self.win.tab_cwds[0], str(first))
        self.assertEqual(self.win.explorer_panel._current_path, str(first))
        self.win.dir_back_btn.click()
        self.assertEqual(self.win._window_cwd, original)
        self.assertFalse(self.win.dir_back_btn.isEnabled())

    def test_back_skips_removed_directories_and_invalid_input(self):
        original = self.win._window_cwd
        target = Path(self.tmp.name) / 'removed'
        target.mkdir()
        self.win.working_dir_combo.setCurrentText(str(target))
        self.win._apply_working_dir()
        self.win.working_dir_combo.setCurrentText(self.tmp.name)
        self.win._apply_working_dir()
        target.rmdir()
        self.win.working_dir_combo.setCurrentText(str(target))
        with patch.object(self.win, '_styled_message_box') as warning:
            self.win._apply_working_dir()
            self.win.dir_back_btn.click()
        warning.assert_called_once()
        self.assertEqual(self.win._window_cwd, original)
        self.assertFalse(self.win.dir_back_btn.isEnabled())

    def test_back_history_is_independent_for_each_tab(self):
        original = self.win._window_cwd
        self.win.working_dir_combo.setCurrentText(self.tmp.name)
        self.win._apply_working_dir()
        self.win._add_new_tab(tab_cwd=self.tmp.name)
        self.assertFalse(self.win.dir_back_btn.isEnabled())
        self.win.tab_widget.setCurrentIndex(0)
        self.assertTrue(self.win.dir_back_btn.isEnabled())
        self.win.dir_back_btn.click()
        self.assertEqual(self.win._window_cwd, original)
        self.assertEqual(self.win.tab_cwds[1], self.tmp.name)

    def test_enter_on_selected_history_completion_switches_directory(self):
        target = Path(self.tmp.name) / 'recent-project'
        target.mkdir()
        edit = self.win.working_dir_combo.lineEdit()
        edit.setText('recent')
        completer = self.win._dir_completer
        completer.set_items([str(target)])
        completer.setCompletionPrefix(edit.text())
        completer.complete()
        popup = completer.popup()
        popup.setCurrentIndex(completer.completionModel().index(0, 0))
        with patch.object(self.win, '_styled_message_box'):
            QTest.keyClick(popup, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.win._window_cwd, str(target))

    def test_down_then_enter_chooses_history_instead_of_typed_parent(self):
        parent = Path(self.tmp.name) / 'projects'
        child = parent / 'recent-project'
        child.mkdir(parents=True)
        edit = self.win.working_dir_combo.lineEdit()
        edit.setText(str(parent) + os.sep)
        completer = self.win._dir_completer
        completer.set_items([str(child)])
        completer.setCompletionPrefix(edit.text())
        completer.complete()
        popup = completer.popup()
        QTest.keyClick(popup, Qt.Key.Key_Down)
        QTest.keyClick(popup, Qt.Key.Key_Return)
        self.app.processEvents()
        self.assertEqual(self.win._window_cwd, str(child))
        self.assertEqual(self.win._working_dir_freq[str(child)], 1)

    def test_invalid_typed_path_is_not_replaced_by_history_suggestion(self):
        original_cwd = self.win._window_cwd
        target = str(Path(self.tmp.name) / 'missing')
        edit = self.win.working_dir_combo.lineEdit()
        edit.setText(target)
        completer = self.win._dir_completer
        completer.set_items([target + '-old-project'])
        completer.setCompletionPrefix(edit.text())
        completer.complete()
        with patch.object(self.win, '_styled_message_box') as warning:
            QTest.keyClick(completer.popup(), Qt.Key.Key_Enter)
        warning.assert_called_once()
        self.assertEqual(self.win._window_cwd, original_cwd)
        self.assertNotIn(target, self.win.working_dir_history)
        self.assertFalse(completer.popup().isVisible())

    def test_keypad_enter_without_suggestions_switches_directory(self):
        target = Path(self.tmp.name) / 'new-directory'
        target.mkdir()
        self.win.working_dir_combo.setCurrentText(str(target))
        QTest.keyClick(self.win.working_dir_combo.lineEdit(), Qt.Key.Key_Enter)
        self.app.processEvents()
        self.assertEqual(self.win._window_cwd, str(target))


if __name__ == '__main__':
    unittest.main()
