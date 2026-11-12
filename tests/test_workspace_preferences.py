"""Per-window preset and persistent navigator order regressions; isolated config."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication
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
