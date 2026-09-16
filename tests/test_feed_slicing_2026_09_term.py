# -*- coding: utf-8 -*-
"""大块输出分片 feed（2026-09 审查 C 项）

读取端一次攒到 256KB，整块持 _screen_lock feed 要 200~300ms，GUI 线程
paintEvent/滚动在锁上等同样久。现按 _FEED_SLICE_CHARS 分片、片间释放锁。

覆盖：
1. pyte Stream 跨 feed 调用状态连续：含 SGR/CSI/OSC 的文本整块喂与任意切片
   喂（含切在 ESC 序列中间）后 display、逐格属性、光标完全一致；
2. widget 端 _process_output_text 确实分片持锁（每次 feed ≤ 上限、锁被多次获取）；
3. 分片后 _history_grew 按总和补偿一次（回滚浏览时 scroll_offset 只加总数）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_feed_slicing_2026_09_term.py -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pyte

from terminal_screen import CompatibleHistoryScreen


def _make(cols=60, rows=8, history=500):
    screen = CompatibleHistoryScreen(cols, rows, history=history)
    screen.set_mode(pyte.modes.LNM)
    screen.set_mode(pyte.modes.DECAWM)
    stream = pyte.Stream(screen)
    stream.use_utf8 = False
    return screen, stream


def _sample_text():
    parts = []
    for i in range(300):
        parts.append(f"\x1b[1;31mred{i}\x1b[0m plain \x1b[4;38;5;208munder\x1b[24m "
                     f"\x1b[7mrev\x1b[27m 中文宽字符 \x1b[{(i % 5) + 1}C tab\x1b[K\r\n")
        if i % 17 == 0:
            parts.append("\x1b[2A\x1b[3D x\x1b[2B\r\n")   # 光标移动
        if i % 23 == 0:
            parts.append("\x1b[?25l\x1b[?25h")             # 私有模式
    return ''.join(parts)


def _snapshot(screen):
    rows = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        rows.append(tuple(sorted((x, tuple(c)) for x, c in line.items())))
    return rows, list(screen.history.top).__len__(), (screen.cursor.x, screen.cursor.y), screen.display


class TestPyteCrossFeedContinuity(unittest.TestCase):
    def _compare(self, step):
        text = _sample_text()
        s_whole, st_whole = _make()
        st_whole.feed(text)
        s_part, st_part = _make()
        for i in range(0, len(text), step):
            st_part.feed(text[i:i + step])
        self.assertEqual(_snapshot(s_whole), _snapshot(s_part), f"step={step} 分片后状态不一致")

    def test_slice_sizes(self):
        for step in (1, 2, 3, 7, 13, 1000, 16 * 1024):
            with self.subTest(step=step):
                self._compare(step)


class _WidgetBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w._backend = None
        self.addCleanup(w.deleteLater)
        return w


class _CountingLock:
    """包一层 RLock：统计 with 进入次数，仍然真实加锁。"""

    def __init__(self, inner):
        self._inner = inner
        self.enters = 0

    def __enter__(self):
        self.enters += 1
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def acquire(self, *a, **k):
        self.enters += 1
        return self._inner.acquire(*a, **k)

    def release(self):
        return self._inner.release()


class TestWidgetFeedSlicing(_WidgetBase):
    def test_big_block_fed_in_slices_with_lock_released_between(self):
        from terminal_widget import TerminalWidget
        w = self._widget()
        step = TerminalWidget._FEED_SLICE_CHARS
        lock = _CountingLock(w._screen_lock)
        w._screen_lock = lock
        sizes = []
        real_feed = w.stream.feed

        def feed(text):
            sizes.append(len(text))
            real_feed(text)
        w.stream.feed = feed

        text = ''.join(f"line {i:06d} " + "x" * 50 + "\r\n" for i in range(4000))  # ~256K 字符
        self.assertGreater(len(text), 200 * 1024)
        w._process_output_text(text)

        self.assertTrue(sizes, "没有 feed")
        self.assertLessEqual(max(sizes), step, f"单次 feed 超过分片上限: {max(sizes)}")
        expected = -(-len(text) // step)
        self.assertEqual(len(sizes), expected)
        self.assertGreaterEqual(lock.enters, expected, "分片之间没有释放/重新获取锁")
        self.assertEqual(sum(sizes), len(text), "分片丢字")
        self.assertIn("line 003999", '\n'.join(w.screen.display))

    def test_small_block_single_feed(self):
        w = self._widget()
        sizes = []
        real_feed = w.stream.feed
        w.stream.feed = lambda t: (sizes.append(len(t)), real_feed(t))
        w._process_output_text("hello\r\n")
        self.assertEqual(sizes, [7])

    def test_history_grew_compensation_sums_over_slices(self):
        """回滚浏览时，一整块新增的历史行只补偿一次、按总和补偿。"""
        w = self._widget()
        w._process_output_text(''.join(f"seed {i}\r\n" for i in range(200)))
        w.scroll_offset = 5
        before = w._get_history_count()
        n_lines = 3000
        text = ''.join(f"grow {i:05d} " + "y" * 60 + "\r\n" for i in range(n_lines))
        w._process_output_text(text)
        added = w._get_history_count() - before
        self.assertEqual(added, n_lines)
        self.assertEqual(w.scroll_offset, 5 + n_lines)

    def test_split_escape_across_slices_renders_correctly(self):
        """分片恰好切在 SGR 序列中间：颜色/文字都不错乱。"""
        from terminal_widget import TerminalWidget
        w = self._widget()
        step = TerminalWidget._FEED_SLICE_CHARS
        pad = "p" * (step - 3)                  # 让 "\x1b[31m" 跨越第一片边界
        text = pad + "\x1b[31mRED\x1b[0m done\r\n"
        w._process_output_text(text)
        joined = '\n'.join(w.screen.display)
        self.assertIn("RED done", joined)
        self.assertNotIn("[31m", joined)
        # 找到 RED 所在格子确认是红色
        found = False
        for y in range(w.screen.lines):
            line = w.screen.buffer[y]
            for x, ch in line.items():
                if ch.data == 'R' and ch.fg == 'red':
                    found = True
        self.assertTrue(found, "跨片 SGR 未生效")


if __name__ == '__main__':
    unittest.main()
