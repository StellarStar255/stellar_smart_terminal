# -*- coding: utf-8 -*-
"""回归（2026-09 审查 D）：_load_config 的大 try 吞异常必须留日志。

以前 except 里只做 self.presets = []：配置文件某个键类型不对（比如
working_dir_freq 被写成 list），后面几十个键全部悄悄退回默认值，用户只看到
"设置丢了"，日志里一个字没有。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_config_load_warning_2026_09_win.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from _qt_quit_reset_2026_09_win import dispose_windows


class TestConfigLoadWarning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import app_config
        import main_window
        cls.app_config = app_config
        cls.mw = main_window

    def test_malformed_config_logs_warning_and_does_not_raise(self):
        w = self.mw.MainWindow()
        try:
            bad = {
                'working_dir_history': ['/tmp/some-dir'],
                'working_dir_freq': [],          # 应为 dict：补默认频率时 TypeError
                'split_spring_default_on_migrated': True,
            }
            with mock.patch.object(self.app_config, 'read_config', return_value=bad), \
                 self.assertLogs('main_window_config', level='WARNING') as cm:
                w._load_config()
            self.assertTrue(any('config load failed' in line for line in cm.output),
                            cm.output)
            # 异常信息要带上（exc_info），否则还是查不出是哪个键坏了
            self.assertTrue(any('TypeError' in line for line in cm.output), cm.output)
        finally:
            dispose_windows(self.app, [w])


if __name__ == '__main__':
    unittest.main()
