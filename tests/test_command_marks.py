# -*- coding: utf-8 -*-
"""命令标记（Alt+↑/↓ 跳转 + 滚动条刻度）的测试

覆盖：
- Enter 记录标记（累计行号坐标）；备用屏幕不记录；去重；上限裁剪
- 历史 deque 丢头后标记换算仍指向正确的当前绝对行（剪掉失效标记）
- Alt+↑/↓ 跳转：上一条置顶、下一条置顶、越过最后一条回到底部
- 滚动条刻度比例在 [0,1) 且与标记数一致
- keyPressEvent 的 Alt+Up 路径（备用屏幕透传不拦截）

运行方式：
    python3 -m pytest tests/test_command_marks.py -v
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QKeyEvent

from terminal_widget import TerminalWidget


def _feed_lines(w, n):
    """喂 n 行输出（真实走 pyte，行会按需推入历史）"""
    for i in range(n):
        w.stream.feed(f"line {i}\r\n")


class TestCommandMarks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = TerminalWidget()
        self.w.resize(800, 480)

    def tearDown(self):
        self.w.cleanup()
        self.w.deleteLater()
        self.app.processEvents()

    def test_record_and_convert(self):
        w = self.w
        _feed_lines(w, 5)
        w._record_command_mark()
        marks = w._current_mark_positions()
        self.assertEqual(len(marks), 1)
        # 标记 = 当前历史行数 + 光标行
        expected = w._get_history_count() + w.screen.cursor.y
        self.assertEqual(marks[0], expected)

    def test_dedup_and_cap(self):
        w = self.w
        w._record_command_mark()
        w._record_command_mark()  # 同一行重复 Enter 去重
        self.assertEqual(len(w._command_marks), 1)
        w._COMMAND_MARKS_MAX = 5
        for i in range(10):
            _feed_lines(w, 1)
            w._record_command_mark()
        self.assertLessEqual(len(w._command_marks), 5)

    def test_alt_screen_not_recorded(self):
        w = self.w
        w.stream.feed("\x1b[?1049h")  # 进备用屏幕
        before = len(w._command_marks)
        w._record_command_mark()
        self.assertEqual(len(w._command_marks), before)
        w.stream.feed("\x1b[?1049l")

    def test_marks_survive_history_trim(self):
        w = self.w
        _feed_lines(w, 3)
        w._record_command_mark()
        cum = w._command_marks[0]
        # 模拟 deque 丢头：累计数继续涨、现存历史比累计少 100
        w.screen._total_history_lines += 200
        # 该标记的换算位置 = cum - (total - hist)；hist 取真实值
        hist = w._get_history_count()
        shift = w.screen._total_history_lines - hist
        expected = cum - shift
        marks = w._current_mark_positions()
        if expected >= 0:
            self.assertEqual(marks, [expected])
        else:
            self.assertEqual(marks, [])  # 已滚出历史的标记被剪掉

    def test_jump_prev_and_next(self):
        w = self.w
        rows = w.term_rows
        # 造两条命令标记，中间隔开足够多行
        _feed_lines(w, rows + 10)
        w._record_command_mark()
        w._current_mark_positions()[0]
        _feed_lines(w, rows + 20)
        w._record_command_mark()
        _feed_lines(w, rows)  # 命令后再有输出
        hist = w._get_history_count()
        self.assertEqual(w.scroll_offset, 0)

        w._jump_to_command_mark(-1)  # 跳到最近一条命令
        hist = w._get_history_count()
        marks = w._current_mark_positions()
        self.assertEqual(hist - w.scroll_offset, marks[-1])

        w._jump_to_command_mark(-1)  # 再跳到更早那条
        self.assertEqual(hist - w.scroll_offset, marks[0])

        w._jump_to_command_mark(1)   # 向下跳回最近那条
        self.assertEqual(hist - w.scroll_offset, marks[-1])

        w._jump_to_command_mark(1)   # 越过最后一条 → 回到底部
        self.assertEqual(w.scroll_offset, 0)

    def test_mark_fractions_range(self):
        w = self.w
        _feed_lines(w, 30)
        w._record_command_mark()
        fr = w._mark_fractions()
        self.assertEqual(len(fr), 1)
        self.assertTrue(0.0 <= fr[0] < 1.0)

    def test_alt_up_keypress_jumps(self):
        w = self.w
        rows = w.term_rows
        _feed_lines(w, rows + 5)
        w._record_command_mark()
        _feed_lines(w, rows + 5)
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                       Qt.KeyboardModifier.AltModifier)
        w.keyPressEvent(ev)
        hist = w._get_history_count()
        self.assertEqual(hist - w.scroll_offset,
                         w._current_mark_positions()[-1])

    def test_alt_up_passthrough_in_alt_screen(self):
        w = self.w
        _feed_lines(w, 5)
        w._record_command_mark()
        w.stream.feed("\x1b[?1049h")
        old_offset = w.scroll_offset
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Up,
                       Qt.KeyboardModifier.AltModifier)
        w.keyPressEvent(ev)
        # 备用屏幕：不做本地跳转（事件走透传/其它分支）
        self.assertEqual(w.scroll_offset, old_offset)
        w.stream.feed("\x1b[?1049l")


if __name__ == '__main__':
    unittest.main()
