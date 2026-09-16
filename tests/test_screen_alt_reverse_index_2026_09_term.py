# -*- coding: utf-8 -*-
"""备用屏幕 RI（ESC M）不得把行塞进 history.bottom（2026-09 审查 B 项）

pyte HistoryScreen.reverse_index 在光标位于顶行时把被挤出的底行 append 进
history.bottom；本应用从不用 prev/next_page，bottom 没有消费者，但 resize
reflow 把它当"屏幕下方的内容"并进主屏——vim/less 在备用屏幕里的 ESC M 会让
退出 TUI 后提示符下多出几行残影。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_screen_alt_reverse_index_2026_09_term.py -q
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


class TestAltScreenReverseIndex(unittest.TestCase):
    def test_alt_screen_ri_then_resize_does_not_leak_into_main(self):
        screen, stream = _make(40, 5)
        stream.feed("one\r\ntwo\r\nthree\r\n$ ")
        stream.feed("\x1b[?1049h")
        stream.feed("v1\r\nv2\r\nv3\r\nv4\r\nv5")     # 画满 5 行（不滚动）
        stream.feed("\x1b[H")                          # 光标回顶
        stream.feed("\x1bM\x1bM\x1bM")                 # 3 次 RI：以前会把 v5/v4/v3 塞进 bottom
        self.assertEqual(len(screen.history.bottom), 0,
                         "备用屏幕上的 RI 不该往 history.bottom 塞行")
        screen.resize(6, 40)                           # TUI 期间变高 → 主屏 reflow
        stream.feed("\x1b[?1049l")
        texts = [line.rstrip() for line in screen.display]
        self.assertEqual(texts[:4], ["one", "two", "three", "$"])
        self.assertFalse(any(t.startswith('v') for t in texts),
                         f"vim 内容并进了主屏: {texts}")
        self.assertEqual(len(screen.history.bottom), 0)

    def test_main_screen_ri_discards_bottom_line(self):
        """主屏顶行 RI：底行按真实终端语义丢弃，不进 bottom，不进 top。"""
        screen, stream = _make(40, 5)
        stream.feed("a\r\nb\r\nc\r\nd\r\ne")
        stream.feed("\x1b[H\x1bM")
        self.assertEqual(len(screen.history.bottom), 0)
        self.assertEqual(len(screen.history.top), 0)
        self.assertEqual([t.rstrip() for t in screen.display], ["", "a", "b", "c", "d"])

    def test_ri_within_margins_still_scrolls_region(self):
        """滚动区（DECSTBM）内的 RI 只滚区内，语义与 pyte.Screen 一致。"""
        screen, stream = _make(40, 5)
        stream.feed("a\r\nb\r\nc\r\nd\r\ne")
        stream.feed("\x1b[2;4r")     # 第 2~4 行为滚动区
        stream.feed("\x1b[2;1H\x1bM")
        self.assertEqual([t.rstrip() for t in screen.display], ["a", "", "b", "c", "e"])
        self.assertEqual(len(screen.history.bottom), 0)


if __name__ == '__main__':
    unittest.main()
