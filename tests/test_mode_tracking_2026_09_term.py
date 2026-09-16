# -*- coding: utf-8 -*-
"""焦点/鼠标上报模式与 DSR/DA 应答跨块可靠（2026-09 审查 E 项）

以前 widget 在每个读取块内找 '\\x1b[?1004h'、'\\x1b[6n' 等子串，序列被块边界
拆开就漏掉（焦点上报判不出 → Claude Code 里点击又灌方向键；DSR 不应答 →
依赖光标位置的 TUI 卡死）。现在交给 pyte 的增量解析器，经 set_mode/reset_mode
和 report_device_* 钩子处理。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_mode_tracking_2026_09_term.py -q
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from terminal_widget import TerminalWidget
        self.w = TerminalWidget()
        self.writes = []
        self.w._backend = SimpleNamespace(is_running=True,
                                          write=lambda d: self.writes.append(d) or True)

    def tearDown(self):
        self.w._backend = None
        self.w.deleteLater()
        self.app.processEvents()

    def feed(self, *chunks):
        for c in chunks:
            self.w._process_output_text(c)


class TestFocusAndMouseModeAcrossChunks(_Base):
    def test_focus_report_split_across_chunks(self):
        self.feed('\x1b[?10', '04h')
        self.assertTrue(self.w._focus_report_mode)
        self.feed('\x1b[?1004', 'l')
        self.assertFalse(self.w._focus_report_mode)

    def test_focus_report_whole_chunk_and_no_leak(self):
        self.feed('hi\x1b[?1004hthere')
        self.assertTrue(self.w._focus_report_mode)
        self.assertIn('hithere', self.w.screen.display[0])

    def test_mouse_mode_split_across_chunks(self):
        for mode in ('1000', '1002', '1003', '1006'):
            with self.subTest(mode=mode):
                self.feed('\x1b[?' + mode[:2], mode[2:] + 'h')
                self.assertTrue(self.w._mouse_mode)
                self.feed('\x1b[?' + mode, 'l')
                self.assertFalse(self.w._mouse_mode)

    def test_mouse_mode_combined_sequence(self):
        """ncurses 常见的 \\x1b[?1000;1002;1006h 一次开三个。"""
        self.feed('\x1b[?1000;1002;', '1006h')
        self.assertTrue(self.w._mouse_mode)
        self.feed('\x1b[?1006;1002;1000l')
        self.assertFalse(self.w._mouse_mode)

    def test_externally_set_flag_not_clobbered_by_unrelated_output(self):
        """测试/外部直接设的值：没有模式变化的输出不会把它冲掉。"""
        self.w._mouse_mode = True
        self.feed('plain text\r\n')
        self.assertTrue(self.w._mouse_mode)

    def test_process_exit_resets_focus_and_reenable_works(self):
        self.feed('\x1b[?1004h')
        self.assertTrue(self.w._focus_report_mode)
        self.w._on_process_finished(0)
        self.assertFalse(self.w._focus_report_mode)
        self.feed('x\r\n')
        self.assertFalse(self.w._focus_report_mode)
        self.feed('\x1b[?1004h')
        self.assertTrue(self.w._focus_report_mode)


class TestDeviceQueriesAcrossChunks(_Base):
    def test_dsr_cursor_split_across_chunks(self):
        self.feed('abc', '\x1b[6', 'n')
        self.assertIn(b'\x1b[1;4R', self.writes)

    def test_dsr_cursor_answers_position_at_query_point(self):
        """同块 'text\\x1b[6n' 要按查询点之前已上屏的内容作答，序列本身不上屏。"""
        self.feed('hello\x1b[6nworld')
        self.assertEqual(self.writes, [b'\x1b[1;6R'])
        self.assertTrue(self.w.screen.display[0].startswith('helloworld'))

    def test_dsr_status_split(self):
        self.feed('\x1b[', '5n')
        self.assertIn(b'\x1b[0n', self.writes)

    def test_primary_da_split_and_format(self):
        self.feed('\x1b[', 'c')
        self.assertIn(b'\x1b[?62;c', self.writes)
        self.feed('\x1b[0c')
        self.assertEqual(self.writes.count(b'\x1b[?62;c'), 2)
        self.assertNotIn('c', self.w.screen.display[0].strip())

    def test_secondary_da_still_answered_and_not_confused_with_primary(self):
        self.feed('\x1b[>c')
        self.assertIn(b'\x1b[>65;100;0c', self.writes)
        self.assertNotIn(b'\x1b[?62;c', self.writes)

    def test_no_backend_is_safe(self):
        self.w._backend = None
        self.feed('\x1b[6n\x1b[c\x1b[5n')
        self.assertEqual(self.writes, [])


if __name__ == '__main__':
    unittest.main()
