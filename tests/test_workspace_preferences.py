"""Per-window preset and persistent navigator order regressions; isolated config."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu
import app_config
from main_window import MainWindow
from window_navigator import WindowNavigatorPanel


class TestWorkspacePreferences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / 'config.json'
        self.config_patch = patch.object(app_config, 'get_config_path', return_value=self.cfg)
        self.config_patch.start()
        app_config.update_config({'auto_update_check': False, 'navigator_enabled': False})
        self.windows = [MainWindow(), MainWindow()]
        for w in self.windows:
            w._adopt_presets([{'name': 'A', 'commands': ['zsh']},
                              {'name': 'B', 'commands': ['zsh']}])
            w.show()
        self.app.processEvents()
        self.navs = []

    def tearDown(self):
        for nav in self.navs:
            nav.deleteLater()
        for w in self.windows:
            w.close()
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        self.config_patch.stop()
        self.tmp.cleanup()

    def test_each_window_restores_own_preset_by_name(self):
        a, b = self.windows
        a.preset_combo.setCurrentIndex(0)
        b.preset_combo.setCurrentIndex(1)
        entries = MainWindow._collect_windows_snapshot()
        by_id = {e['window_id']: e for e in entries}
        for w in self.windows:
            entry = by_id[w._workspace_id]
            expected = w.preset_combo.currentText()
            w._adopt_presets(list(reversed(w.presets)))
            w._apply_restored_window_state(entry)
            self.assertEqual(w.preset_combo.currentText(), expected)

    def test_command_search_toggle_is_live_and_restores_per_window_and_menu(self):
        a, b = self.windows
        a.raise_()
        a.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(a))
        term = a.terminal
        parent = QMenu(term)
        term._add_quick_command_menu(
            parent, QPoint(100, 100), 'ctx.quick_commands',
            lambda menu: term._populate_quick_command_presets(menu, a.presets, 'Global'),
            term.add_command_requested, term.manage_presets_requested, 'ctx.manage_presets')
        menu = parent.actions()[0].menu()
        parent.popup(QPoint(100, 100))
        parent.setActiveAction(parent.actions()[0])
        QTest.keyClick(parent, Qt.Key.Key_Right)
        self.assertTrue(QTest.qWaitForWindowExposed(menu))
        self.app.processEvents()

        def click_toggle():
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton,
                             pos=menu.actionGeometry(menu.search_toggle).center())
            self.app.processEvents()
            self.assertTrue(menu.isVisible())

        try:
            self.assertFalse(menu.search_box.isVisible())
            click_toggle()
            self.assertTrue(menu.search_box.isVisible())
            self.assertTrue(menu.search_box.hasFocus())
            QTest.keyClicks(menu.search_box, 'missing')
            click_toggle()
            self.assertFalse(menu.search_box.isVisible())
            self.assertEqual(menu.command_list.count(), 2)
            click_toggle()
        finally:
            menu.close()
            parent.close()
        a._write_workspace_snapshot()
        entries = {entry['window_id']: entry for entry in
                   app_config.read_config()['workspace_snapshot']['windows']}
        self.assertEqual(entries[a._workspace_id]['quick_command_search'], {'global': True})
        self.assertEqual(entries[b._workspace_id]['quick_command_search'], {})
        a._apply_restored_window_state({'window_id': a._workspace_id})
        self.assertEqual(a._quick_command_search, {})
        a._apply_restored_window_state(entries[a._workspace_id])
        self.assertEqual(a._quick_command_search, {'global': True})
        self.assertEqual(b._quick_command_search, {})

    def test_sort_order_and_options_survive_new_panel_and_window_ids(self):
        nav = WindowNavigatorPanel()
        self.navs.append(nav)
        nav._force_refresh()
        nav._set_sort_mode('manual')
        item = nav.window_list.takeItem(1)
        nav.window_list.insertItem(0, item)
        nav._on_rows_moved()
        self.app.processEvents()
        nav.compact_checkbox.setChecked(False)
        nav.quick_close_checkbox.setChecked(True)
        expected = list(nav._persisted_order)
        self.assertEqual(len(expected), 2)
        self.assertEqual(app_config.read_config()['navigator_manual_order'], expected)
        # Recreated objects have different Python identities but the restored IDs match.
        entries = MainWindow._collect_windows_snapshot()
        for w, entry in zip(self.windows, reversed(entries)):
            w._apply_restored_window_state(entry)
        restored = WindowNavigatorPanel()
        self.navs.append(restored)
        restored._force_refresh()
        self.assertEqual(restored._sort_mode, 'manual')
        self.assertFalse(restored.compact_checkbox.isChecked())
        self.assertTrue(restored.quick_close_checkbox.isChecked())
        actual = [restored._resolve_window(restored.window_list.item(i))._workspace_id
                  for i in range(restored.window_list.count())]
        self.assertEqual(actual, expected)

    def test_batch_quit_keeps_all_windows_after_first_is_destroyed(self):
        self.windows[0]._write_workspace_snapshot()
        original = app_config.read_config()['workspace_snapshot']
        with patch.object(MainWindow, '_batch_closing', True):
            first = self.windows.pop(0)
            first.close()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
            # A remaining window's timer/config save must not shrink the snapshot.
            self.windows[0]._write_workspace_snapshot()
            self.windows[0]._save_config()
            self.windows[0].close()
            self.windows.clear()
        self.assertEqual(app_config.read_config()['workspace_snapshot'], original)

    def test_saved_preset_editor_does_not_overwrite_later_disk_edit(self):
        w = self.windows[0]
        w.preset_combo.setCurrentIndex(1)
        w._apply_edited_presets(list(reversed(w.presets)))
        self.assertEqual(w.preset_combo.currentText(), 'B')
        latest = [{'name': 'External', 'commands': ['zsh']}]
        app_config.update_config({'presets': latest})
        w._save_config()
        self.assertEqual(app_config.read_config()['presets'], latest)

    def test_close_flushes_latest_preset_without_waiting_for_timer(self):
        w = self.windows[0]
        w.preset_combo.setCurrentIndex(1)
        window_id = w._workspace_id
        w.close()
        snap = app_config.read_config()['workspace_snapshot']['windows']
        self.assertEqual(next(e for e in snap if e['window_id'] == window_id)['preset_name'], 'B')
        self.windows.remove(w)
