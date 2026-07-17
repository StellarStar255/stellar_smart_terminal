"""启动自动检查更新的行为测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_auto_update_check.py -v

覆盖：每日节流、设置开关、角标出现/跳过逻辑。网络层用 stub 替换
UpdateChecker（不发真实请求）；配置一律 patch app_config.get_config_path
到临时文件——直接用真实路径会污染项目配置（此前踩过的坑）。
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

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication


class _StubChecker(QThread):
    """替身：start() 不做任何事，不碰网络。"""
    result = pyqtSignal(dict)
    error = pyqtSignal(str)
    started_count = 0

    def start(self, *a, **k):
        _StubChecker.started_count += 1


class TestAutoUpdateCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # main_window 必须晚于 QApplication 导入（模块级导入会在无 app 时崩）
        global app_config, app_updater, main_window
        import app_config
        import app_updater
        import main_window

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='auto_update_test_')
        self._cfg_path = Path(self._tmp.name) / 'config.json'
        self._cfg_path.write_text('{}')
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._cfg_path
        self._orig_checker = app_updater.UpdateChecker
        app_updater.UpdateChecker = _StubChecker
        _StubChecker.started_count = 0
        main_window.MainWindow._auto_update_check_done = False
        self.w = main_window.MainWindow()

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        self.app.processEvents()
        app_config.get_config_path = self._orig_get_path
        app_updater.UpdateChecker = self._orig_checker
        main_window.MainWindow._auto_update_check_done = False
        self._tmp.cleanup()

    def _write_cfg(self, d: dict):
        self._cfg_path.write_text(json.dumps(d))

    def test_check_runs_when_due(self):
        self._write_cfg({'update_last_check_ts': 0})
        self.w._maybe_auto_check_updates()
        self.assertEqual(_StubChecker.started_count, 1)
        self.assertTrue(main_window.MainWindow._auto_update_check_done)
        # 节流时间戳已写盘
        cfg = json.loads(self._cfg_path.read_text())
        self.assertGreater(cfg.get('update_last_check_ts', 0), 0)

    def test_daily_throttle(self):
        self._write_cfg({'update_last_check_ts': time.time() - 3600})  # 1h 前查过
        self.w._maybe_auto_check_updates()
        self.assertEqual(_StubChecker.started_count, 0)

    def test_disabled_by_setting(self):
        self._write_cfg({'auto_update_check': False, 'update_last_check_ts': 0})
        self.w._maybe_auto_check_updates()
        self.assertEqual(_StubChecker.started_count, 0)

    def test_once_per_process(self):
        self._write_cfg({'update_last_check_ts': 0})
        self.w._maybe_auto_check_updates()
        self._write_cfg({'update_last_check_ts': 0})  # 重置节流也不该再查
        self.w._maybe_auto_check_updates()
        self.assertEqual(_StubChecker.started_count, 1)

    def test_badge_on_newer_version(self):
        self.w._on_auto_update_result(
            {'tag': 'v99.0.0', 'notes': '', 'asset': None})
        badge = getattr(self.w, '_update_badge', None)
        self.assertIsNotNone(badge)
        self.assertIn('v99.0.0', badge.text())

    def test_no_badge_when_up_to_date(self):
        cur = app_updater.get_current_version()
        self.w._on_auto_update_result(
            {'tag': f'v{cur}', 'notes': '', 'asset': None})
        self.assertIsNone(getattr(self.w, '_update_badge', None))

    def test_no_badge_for_skipped_tag(self):
        self._write_cfg({'update_skipped_tag': 'v99.0.0'})
        self.w._on_auto_update_result(
            {'tag': 'v99.0.0', 'notes': '', 'asset': None})
        self.assertIsNone(getattr(self.w, '_update_badge', None))


if __name__ == "__main__":
    unittest.main()
