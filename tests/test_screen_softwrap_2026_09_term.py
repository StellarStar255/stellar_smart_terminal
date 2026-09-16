# -*- coding: utf-8 -*-
"""软换行标记的生命周期回归（2026-09 审查 A 项）

以前 CompatibleHistoryScreen 按 id(line) 把软换行行登记在集合里：行被历史
deque 淘汰后对象被回收、id 被新行复用，集合里的陈旧 id 让新写的短行被误判
成软换行（复制少换行、双击选词跨行、resize reflow 拼错行）。现改为把标记
挂在行对象上（line.soft_wrapped），生命周期与行一致。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_screen_softwrap_2026_09_term.py -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyte

from terminal_screen import CompatibleHistoryScreen


def _make(cols=40, rows=5, history=200):
    screen = CompatibleHistoryScreen(cols, rows, history=history)
    screen.set_mode(pyte.modes.LNM)
    screen.set_mode(pyte.modes.DECAWM)
    stream = pyte.Stream(screen)
    return screen, stream


def _row_text(line, cols):
    return ''.join(line[x].data for x in range(cols)).rstrip()


class TestSoftWrapLifecycle(unittest.TestCase):
    @staticmethod
    def _misjudged_short_rows(screen, n_recent):
        bad = []
        rows = list(screen.history.top)[-n_recent:] + [screen.buffer[y] for y in range(screen.lines)]
        for line in rows:
            text = _row_text(line, screen.columns)
            if text.startswith('short') and screen.is_soft_wrapped(line):
                bad.append(text)
        return bad

    def test_new_short_lines_not_misjudged_after_history_eviction(self):
        """喂超过 history 上限的折行后再写短行：新短行绝不能是软换行。

        短行用 CR + IND（\\x1bD）换行而不是 LF：LF 会顺手清掉光标行的标记，掩盖
        了 id 复用；而凡是不以 LF 结束的行（IND、光标定位重画的 TUI 行、
        Claude Code/Ink 帧的末行、提示符行）都直接暴露在陈旧 id 之下——
        旧实现里这类新行有约 2/3 被误判。
        """
        screen, stream = _make(cols=40, rows=5, history=200)
        # 2000 条 100 字符长行 → 每条折成 3 行，远超 history=200，触发大量淘汰
        for i in range(2000):
            stream.feed(f"L{i:04d}-" + "x" * 94 + "\r\n")
        # 随后写 150 条不折行的短行
        for i in range(150):
            stream.feed(f"short {i}\r\x1bD")

        bad = self._misjudged_short_rows(screen, 140)
        self.assertEqual(bad, [], f"{len(bad)} 条新写的短行被误判为软换行: {bad[:5]}")

    def test_wrapped_line_flags(self):
        """长行折成的前两行是软换行，最后一行（显式 \\r\\n 结束）不是。"""
        screen, stream = _make(cols=40, rows=5)
        stream.feed("A" * 100 + "\r\n")
        lines = [screen.buffer[y] for y in range(3)]
        self.assertTrue(screen.is_soft_wrapped(lines[0]))
        self.assertTrue(screen.is_soft_wrapped(lines[1]))
        self.assertFalse(screen.is_soft_wrapped(lines[2]))

    def test_explicit_linefeed_clears_flag(self):
        """刚好写满一行后紧跟 \\r\\n：pyte 延迟换行，显式换行要清掉本行的软换行标记。"""
        screen, stream = _make(cols=40, rows=5)
        stream.feed("B" * 40)
        stream.feed("\r\n")
        stream.feed("next\r\n")
        self.assertFalse(screen.is_soft_wrapped(screen.buffer[0]))

    def test_erase_display_clears_flags(self):
        screen, stream = _make(cols=40, rows=5)
        stream.feed("C" * 100)
        self.assertTrue(screen.is_soft_wrapped(screen.buffer[0]))
        stream.feed("\x1b[2J")
        for y in range(screen.lines):
            self.assertFalse(screen.is_soft_wrapped(screen.buffer[y]))

    def test_alt_screen_keeps_main_flags_and_isolates_alt_lines(self):
        """进入备用屏幕：保存的主屏拷贝带标记，被复用为 alt 行的原对象不带；
        退出后主屏标记还在（resize reflow 依赖它）。"""
        screen, stream = _make(cols=40, rows=5)
        stream.feed("D" * 100 + "\r\n")
        stream.feed("\x1b[?1049h")
        saved = screen._saved_main_buffer
        self.assertTrue(screen.is_soft_wrapped(saved[0]))
        self.assertTrue(screen.is_soft_wrapped(saved[1]))
        for y in range(screen.lines):
            self.assertFalse(screen.is_soft_wrapped(screen.buffer[y]),
                             "alt 屏幕的空行不该继承主屏标记")
        stream.feed("E" * 100)   # alt 屏上自己折行
        self.assertTrue(screen.is_soft_wrapped(screen.buffer[0]))
        stream.feed("\x1b[?1049l")
        self.assertTrue(screen.is_soft_wrapped(screen.buffer[0]))
        self.assertTrue(screen.is_soft_wrapped(screen.buffer[1]))
        self.assertFalse(screen.is_soft_wrapped(screen.buffer[2]))

    def test_reflow_rebuilds_flags_on_new_line_objects(self):
        screen, stream = _make(cols=40, rows=5)
        stream.feed("F" * 100 + "\r\n")
        screen.resize(5, 60)   # 100 字符 → 2 行
        self.assertTrue(screen.is_soft_wrapped(screen.buffer[0]))
        self.assertFalse(screen.is_soft_wrapped(screen.buffer[1]))
        self.assertEqual(_row_text(screen.buffer[0], 60), "F" * 60)
        self.assertEqual(_row_text(screen.buffer[1], 60), "F" * 40)
        screen.resize(5, 120)  # 拼回一行
        self.assertFalse(screen.is_soft_wrapped(screen.buffer[0]))
        self.assertEqual(_row_text(screen.buffer[0], 120), "F" * 100)


if __name__ == '__main__':
    unittest.main()
