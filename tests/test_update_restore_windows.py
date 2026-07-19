"""升级后自动重开窗口（快照/恢复）的行为测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_update_restore_windows.py -v

覆盖：快照写入内容、启动恢复（含一次性消费）、过期快照跳过、
无快照零副作用。配置一律 patch app_config.get_config_path 到临时文件，
避免污染真实配置（与 test_auto_update_check 同一套约定）。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class TestUpdateRestoreWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        global app_config, main_window
        import app_config
        import main_window

    # 与 test_auto_update_check 相同：整个测试类共用一个 MainWindow，
    # 每用例新建完整主窗口的延迟销毁残留会在 CI 上段错误崩掉测试进程。
    @classmethod
    def _make_window(cls):
        cls.w_shared = main_window.MainWindow()
        # 快照只收集可见窗口；offscreen 下 show() 无副作用
        cls.w_shared.show()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='update_restore_test_')
        self._cfg_path = Path(self._tmp.name) / 'config.json'
        self._cfg_path.write_text('{}')
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._cfg_path
        if not hasattr(type(self), 'w_shared'):
            self._make_window()
        self.w = type(self).w_shared
        self.app.processEvents()

    def tearDown(self):
        app_config.get_config_path = self._orig_get_path
        self._tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtCore import QEvent
        if hasattr(cls, 'w_shared'):
            cls.w_shared.close()
            cls.w_shared.deleteLater()
            del cls.w_shared
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    # 配置随 _save_config 落盘时含中文（预设名等）；Windows 的 read_text
    # 默认 cp1252 会炸，必须显式 UTF-8（CI 曾以此崩红 Windows 构建）
    def _read_cfg(self) -> dict:
        return json.loads(self._cfg_path.read_text(encoding='utf-8'))

    def _write_cfg(self, d: dict):
        self._cfg_path.write_text(json.dumps(d), encoding='utf-8')

    def test_stash_writes_window_snapshot(self):
        self.w._stash_windows_for_restore()
        pending = self._read_cfg().get('update_restore_windows')
        self.assertIsInstance(pending, dict)
        self.assertGreater(pending.get('ts', 0), 0)
        entries = pending.get('windows')
        self.assertTrue(entries)
        entry = entries[0]
        self.assertEqual(entry['cwd'], self.w._window_cwd)
        self.assertEqual(len(entry['geometry']), 4)
        self.assertIn('maximized', entry)
        self.assertIn('color', entry)

    def test_restore_applies_and_consumes(self):
        cwd = self._tmp.name  # 一定存在的目录
        self._write_cfg({'update_restore_windows': {
            'ts': time.time(),
            'windows': [{'cwd': cwd, 'geometry': [60, 80, 1360, 820],
                         'maximized': False, 'color': '#22c55e'}],
        }})
        main_window.MainWindow.restore_windows_after_update(self.w)
        self.app.processEvents()
        # 快照被一次性消费
        self.assertNotIn('update_restore_windows', self._read_cfg())
        # 目录 / 颜色 / 几何均已套用（几何须大于窗口最小尺寸，否则被钳制）
        self.assertEqual(os.path.realpath(self.w._window_cwd),
                         os.path.realpath(cwd))
        self.assertEqual(self.w._window_color, '#22c55e')
        self.assertEqual(self.w.width(), 1360)
        self.assertEqual(self.w.height(), 820)

    def test_stale_snapshot_consumed_but_not_applied(self):
        old_cwd = self.w._window_cwd
        self._write_cfg({'update_restore_windows': {
            'ts': time.time() - main_window.MainWindow._UPDATE_RESTORE_MAX_AGE - 1,
            'windows': [{'cwd': self._tmp.name,
                         'geometry': [0, 0, 500, 400], 'maximized': False}],
        }})
        main_window.MainWindow.restore_windows_after_update(self.w)
        self.assertNotIn('update_restore_windows', self._read_cfg())
        self.assertEqual(self.w._window_cwd, old_cwd)

    def test_no_snapshot_is_noop(self):
        main_window.MainWindow.restore_windows_after_update(self.w)
        self.assertNotIn('update_restore_windows', self._read_cfg())

    def test_stash_records_sidebar_panel(self):
        self.w._apply_sidebar_panel('git')
        try:
            self.w._stash_windows_for_restore()
            entry = self._read_cfg()['update_restore_windows']['windows'][0]
            self.assertEqual(entry['panel'], 'git')
        finally:
            self.w._apply_sidebar_panel(None)
        self.w._stash_windows_for_restore()
        entry = self._read_cfg()['update_restore_windows']['windows'][0]
        self.assertIsNone(entry['panel'])

    def test_restore_reopens_sidebar_panel(self):
        self._write_cfg({'update_restore_windows': {
            'ts': time.time(),
            'windows': [{'cwd': '', 'geometry': [60, 80, 1360, 820],
                         'maximized': False, 'panel': 'git'}],
        }})
        main_window.MainWindow.restore_windows_after_update(self.w)
        self.app.processEvents()
        try:
            self.assertEqual(self.w._current_sidebar_panel(), 'git')
            self.assertTrue(self.w.left_panel_container.isVisible())
        finally:
            self.w._apply_sidebar_panel(None)

    def test_restore_closes_sidebar_when_snapshot_closed(self):
        self.w._apply_sidebar_panel('explorer')
        self._write_cfg({'update_restore_windows': {
            'ts': time.time(),
            'windows': [{'cwd': '', 'geometry': [60, 80, 1360, 820],
                         'maximized': False, 'panel': None}],
        }})
        main_window.MainWindow.restore_windows_after_update(self.w)
        self.assertIsNone(self.w._current_sidebar_panel())
        self.assertFalse(self.w.left_panel_container.isVisible())

    def test_restore_without_panel_key_keeps_current_state(self):
        # 旧版本写的快照没有 panel 键：不动当前侧边栏状态
        self.w._apply_sidebar_panel('git')
        self._write_cfg({'update_restore_windows': {
            'ts': time.time(),
            'windows': [{'cwd': '', 'geometry': [60, 80, 1360, 820],
                         'maximized': False}],
        }})
        main_window.MainWindow.restore_windows_after_update(self.w)
        try:
            self.assertEqual(self.w._current_sidebar_panel(), 'git')
        finally:
            self.w._apply_sidebar_panel(None)

    def test_missing_cwd_skipped(self):
        old_cwd = self.w._window_cwd
        self._write_cfg({'update_restore_windows': {
            'ts': time.time(),
            'windows': [{'cwd': '/nonexistent/path/xyz',
                         'geometry': [60, 80, 1320, 800], 'maximized': False}],
        }})
        main_window.MainWindow.restore_windows_after_update(self.w)
        # 目录不存在 → 保持原目录，但几何仍恢复
        self.assertEqual(self.w._window_cwd, old_cwd)
        self.assertEqual(self.w.width(), 1320)


if __name__ == "__main__":
    unittest.main()
