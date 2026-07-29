# -*- coding: utf-8 -*-
"""输出规则提醒（Traceback/FAILED 等 → 标签橙点+导航提醒）的测试

覆盖：
- 命中默认规则发 alert_matched；ANSI 颜色切碎关键词仍命中
- 跨块切开的模式经尾缓冲拼接命中
- 命中后静默窗口去重；按键解除静默
- 备用屏幕不扫描；开关关闭不扫描
- set_output_alert_rules：非法正则跳过不炸

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_output_alerts.py -v
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from terminal_widget import TerminalWidget

DEFAULTS = [
    r'Traceback \(most recent call last\)',
    r'\bFAILED\b',
]


class TestOutputAlerts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        TerminalWidget.set_output_alert_rules(DEFAULTS, True)
        self.w = TerminalWidget()
        self.hits = []
        self.w.alert_matched.connect(self.hits.append)

    def tearDown(self):
        self.w.cleanup()
        self.w.deleteLater()
        self.app.processEvents()
        TerminalWidget.set_output_alert_rules(DEFAULTS, True)

    def test_basic_match(self):
        self.w._scan_output_alerts("ok\nTraceback (most recent call last)\n")
        self.app.processEvents()
        self.assertEqual(self.hits, [r'Traceback \(most recent call last\)'])

    def test_ansi_split_keyword_matches(self):
        self.w._scan_output_alerts("2 \x1b[31mFAIL\x1b[0mED tests/x.py\n")
        self.app.processEvents()
        self.assertEqual(len(self.hits), 1)

    def test_cross_chunk_match_via_tail(self):
        self.w._scan_output_alerts("...Traceback (most re")
        self.w._scan_output_alerts("cent call last):\n  File x")
        self.app.processEvents()
        self.assertEqual(len(self.hits), 1)

    def test_mute_window_dedups_and_key_unmutes(self):
        self.w._scan_output_alerts("FAILED a\n")
        self.w._scan_output_alerts("FAILED b\n")  # 静默期内不重复提醒
        self.app.processEvents()
        self.assertEqual(len(self.hits), 1)
        self.w._alert_muted_until = 0.0  # 等价于用户按键解除
        self.w._scan_output_alerts("FAILED c\n")
        self.app.processEvents()
        self.assertEqual(len(self.hits), 2)

    def test_alt_screen_not_scanned(self):
        self.w.stream.feed("\x1b[?1049h")
        self.w._scan_output_alerts("FAILED in tui\n")
        self.app.processEvents()
        self.assertEqual(self.hits, [])
        self.w.stream.feed("\x1b[?1049l")

    def test_disabled_via_output_path(self):
        TerminalWidget.set_output_alert_rules(DEFAULTS, False)
        # 走真实输出路径：开关关闭时不扫描
        self.w._on_output(b"FAILED x\n")
        self.app.processEvents()
        self.assertEqual(self.hits, [])

    def test_output_path_end_to_end(self):
        # 真实 _on_output 路径（含解码/过滤）也能命中
        self.w._on_output(b"\x1b[31mTraceback (most recent call last)\x1b[0m\r\n")
        self.app.processEvents()
        self.assertEqual(len(self.hits), 1)

    def test_invalid_pattern_skipped(self):
        TerminalWidget.set_output_alert_rules(['[bad', r'\bGOOD\b'], True)
        self.assertEqual([p for p, _ in TerminalWidget._ALERT_COMPILED],
                         [r'\bGOOD\b'])


if __name__ == '__main__':
    unittest.main()
