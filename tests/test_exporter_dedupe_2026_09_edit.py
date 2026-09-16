"""exporter._clean_whitespace 只能去掉相邻重复行，不能全局去重。

审查发现 `if stripped in seen_content: continue` 是全条目范围的：代码里
第二个 `}`、日志里重复的 `[INFO] ...` 行全部消失，三种导出格式都受影响。
TUI 重绘产生的重复本来就是相邻的，只去相邻重复即可保留抑噪效果。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exporter import Exporter  # noqa: E402


def clean(text):
    return Exporter.__new__(Exporter)._clean_whitespace(text)


class TestCleanWhitespaceDedupe(unittest.TestCase):
    def test_nonadjacent_duplicate_lines_are_kept(self):
        out = clean("a\n}\nb\n}\n")
        self.assertEqual(out.split('\n'), ['a', '}', 'b', '}'])

    def test_repeated_log_lines_are_kept(self):
        out = clean("[INFO] tick\nstep 1\n[INFO] tick\nstep 2\n")
        self.assertEqual(out.count('[INFO] tick'), 2)

    def test_adjacent_duplicates_are_collapsed(self):
        out = clean("x\nx\nx\ny\n")
        self.assertEqual(out.split('\n'), ['x', 'y'])

    def test_duplicates_across_blank_line_are_kept(self):
        # 空行隔开的相同语句是正经内容（代码里两处 foo()），不算相邻重复
        out = clean("x\n\nx\ny\n")
        self.assertEqual(out.split('\n'), ['x', '', 'x', 'y'])


if __name__ == '__main__':
    unittest.main()
