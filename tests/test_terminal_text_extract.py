# -*- coding: utf-8 -*-
"""终端软换行/选区文本提取的 characterization 测试

覆盖 terminal_widget.py 中 _get_selected_text / _get_all_content 的
wrap_type/threshold 启发式（复制粘贴正确性的核心）。

所有预期值都是用当前实现实际跑出来的结果（characterization），
固化现状以防止后续重构悄悄改变复制行为；并非"理想行为"的断言。

运行方式：
    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_terminal_text_extract -v
（QT_QPA_PLATFORM 已在文件顶部 setdefault，直接 discover 也可以。）
"""

import os
# 必须在 import PyQt6 之前设置，保证离屏运行
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from terminal_widget import TerminalWidget


class TerminalTextExtractBase(unittest.TestCase):
    """公共基类：管理 QApplication 单例与 widget 生命周期"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._widgets = []

    def tearDown(self):
        for w in self._widgets:
            try:
                w.cleanup()
            except Exception:
                pass
            w.deleteLater()
        self._widgets = []

    def make_widget(self, cols=40, rows=10):
        """构造离屏终端 widget（不启动任何 shell/PTY 后端）。

        TerminalWidget() 构造时不会 spawn 子进程（_backend 为 None，
        只有显式调用 start_terminal 才会），所以可以安全离屏实例化。
        """
        w = TerminalWidget()
        w.term_cols = cols
        w.term_rows = rows
        w.screen.resize(rows, cols)
        self._widgets.append(w)
        return w

    def feed(self, w, text):
        w.stream.feed(text)


class TestTerminalSoftWrap(TerminalTextExtractBase):
    """终端层软换行（DECAWM 自动换行）：复制时应无缝合并"""

    def test_long_command_hard_wrapped_merges_via_all_content(self):
        # 77 字符的命令在 40 列终端折成两行（终端软换行）
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "$ echo " + "a" * 70 + "\r\ndone\r\n")
        # 当前行为：软换行两行无缝合并；且后面的 "done" 也被
        # spaceless 启发式（wrap_type 3）并了进来 —— 见 bug 备注。
        self.assertEqual(
            w._get_all_content(),
            "$ echo " + "a" * 70 + "done",
        )

    def test_long_command_hard_wrapped_merges_via_selection(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "$ echo " + "a" * 70 + "\r\ndone\r\n")
        w._selection_start = (0, 0)
        w._selection_end = (2, w.term_cols - 1)
        self.assertEqual(
            w._get_selected_text(),
            "$ echo " + "a" * 70 + "done",
        )

    def test_cjk_terminal_wrap_merges(self):
        # 35 个全角字符 = 70 列，在 40 列终端折两行，复制应合并为一行
        w = self.make_widget(cols=40, rows=10)
        text = "这是一段很长的中文文本用来测试宽字符在终端里的自动换行行为是否正确处理"
        self.feed(w, text + "\r\nok\r\n")
        # 当前行为：软换行合并后，末行 "处理" 与下一行 "ok" 也被
        # spaceless 启发式无缝拼接
        self.assertEqual(w._get_all_content(), text + "ok")

    def test_long_url_wrapped_reconstructed_without_spaces(self):
        # 复制登录 URL 的关键场景：折行处绝不能插入空格
        w = self.make_widget(cols=40, rows=10)
        url = "https://example.com/auth?code=" + "Z" * 50
        self.feed(w, url + "\r\n")
        self.assertEqual(w._get_all_content(), url)

    def test_soft_wrap_preserves_inner_spaces(self):
        # 软换行行保留行内/行尾空格（它们是真实内容）
        w = self.make_widget(cols=10, rows=8)
        self.feed(w, "ab    cdefghij\r\nk\r\n")
        self.assertEqual(w._get_all_content(), "ab    cdefghij\nk")

    def test_selection_spanning_soft_wrap(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "$ git commit -m " + "m" * 40 + "\r\n")
        w._selection_start = (0, 0)
        w._selection_end = (1, 39)
        self.assertEqual(
            w._get_selected_text(),
            "$ git commit -m " + "m" * 40,
        )


class TestApplicationLevelWrap(TerminalTextExtractBase):
    """应用层折行启发式（显式换行但疑似被宽度折断）"""

    def test_prose_app_level_wrap_merged_with_spaces(self):
        # 散文按词边界折行（每行显式 \r\n 结束）→ 拼接并补回词间空格
        w = self.make_widget(cols=40, rows=10)
        self.feed(
            w,
            "The quick brown fox jumps over the\r\n"
            "lazy dog and keeps running through\r\n"
            "the field.\r\n",
        )
        self.assertEqual(
            w._get_all_content(),
            "The quick brown fox jumps over the lazy dog "
            "and keeps running through the field.",
        )

    def test_indented_continuation_lstripped(self):
        # 续行的对齐缩进被折叠，补回一个空格
        w = self.make_widget(cols=40, rows=10)
        self.feed(
            w,
            "Usage: mytool --input FILE --output\r\n"
            "       DIR --verbose\r\n",
        )
        self.assertEqual(
            w._get_all_content(),
            "Usage: mytool --input FILE --output DIR --verbose",
        )

    def test_exact_width_code_line_merges_with_space(self):
        # 行尾刚好顶满宽度（40 列写满 40 字符）+ 显式换行：
        # 当前行为是按散文折行处理，与下一行合并并补一个空格
        w = self.make_widget(cols=40, rows=10)
        code = "result = foo(bar, baz) + qux(1, 234)1234"  # 正好 40 字符
        self.assertEqual(len(code), 40)
        self.feed(w, code + "\r\nprint(result)\r\n")
        self.assertEqual(w._get_all_content(), code + " print(result)")

    def test_exact_width_spaceless_line_merges_seamlessly(self):
        # 无内部空格的整行 token 顶满宽度 → wrap_type 3 无缝拼接（不补空格）
        w = self.make_widget(cols=40, rows=10)
        line40 = "abcdefghij" * 4  # 正好 40 字符，无空格
        self.feed(w, line40 + "\r\nnext_line()\r\n")
        self.assertEqual(w._get_all_content(), line40 + "next_line()")

    def test_cjk_exact_width_app_wrap_seamless(self):
        # 全角字符顶满整行 + 显式换行 → 与下一行无缝拼接（CJK 不补空格）
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "这是第一行的中文内容正好顶满整行宽度啦\r\n继续的第二行。\r\n")
        self.assertEqual(
            w._get_all_content(),
            "这是第一行的中文内容正好顶满整行宽度啦继续的第二行。",
        )

    def test_spaceless_paths_at_block_max_merge(self):
        # 【可疑行为，固化为现状】ls -1 式的多条独立路径：
        # 块内最长的 spaceless 行恒满足 last_col >= run_max - 2，
        # 导致与下一行无缝并联 —— 三条路径被并成一行。
        w = self.make_widget(cols=40, rows=10)
        self.feed(
            w,
            "/usr/local/bin/python3\r\n"
            "/usr/local/share/doc\r\n"
            "/etc/hosts\r\n",
        )
        self.assertEqual(
            w._get_all_content(),
            "/usr/local/bin/python3/usr/local/share/doc/etc/hosts",
        )

    def test_short_spaceless_paths_below_threshold_keep_newlines(self):
        # run_max 低于 threshold_low（max(8, 40*0.40)=16）→ 不触发合并
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "/etc/hosts\r\n/etc/fstab\r\n")
        self.assertEqual(w._get_all_content(), "/etc/hosts\n/etc/fstab")


class TestIntentionalNewlines(TerminalTextExtractBase):
    """真正的多行输出：换行必须保留"""

    def test_independent_short_lines_keep_newlines(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "file1.txt\r\nfile2.txt\r\nfile3.txt\r\n")
        self.assertEqual(
            w._get_all_content(), "file1.txt\nfile2.txt\nfile3.txt"
        )

    def test_blank_line_paragraphs_preserved(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "Paragraph one line.\r\n\r\nParagraph two line.\r\n")
        self.assertEqual(
            w._get_all_content(),
            "Paragraph one line.\n\nParagraph two line.",
        )

    def test_list_markers_force_newline(self):
        # 列表项即使上一行填满也保留换行（_LIST_MARKER_RE）
        w = self.make_widget(cols=40, rows=10)
        self.feed(
            w,
            "Items found in the scan are listed:\r\n"
            "- first item of the list\r\n"
            "- second item\r\n",
        )
        self.assertEqual(
            w._get_all_content(),
            "Items found in the scan are listed:\n"
            "- first item of the list\n"
            "- second item",
        )


class TestSelectionExtraction(TerminalTextExtractBase):
    """_get_selected_text 的选区语义"""

    def test_partial_selection_disables_heuristic(self):
        # 选区未覆盖整行宽度 → can_heuristic=False，保留换行
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "hello world something\r\nsecond line here\r\n")
        w._selection_start = (0, 6)
        w._selection_end = (1, 5)
        self.assertEqual(w._get_selected_text(), "world something\nsecond")

    def test_single_line_partial_selection(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "copy just this\r\n")
        w._selection_start = (0, 5)
        w._selection_end = (0, 8)
        self.assertEqual(w._get_selected_text(), "just")

    def test_reversed_selection_normalized(self):
        # 从右往左拖选：start/end 自动交换
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "alpha beta gamma\r\n")
        w._selection_start = (0, 9)
        w._selection_end = (0, 6)
        self.assertEqual(w._get_selected_text(), "beta")

    def test_select_all_mode_includes_history(self):
        # 5 行屏幕喂 8 行输出 → 部分行进入 scrollback，全选要包含历史
        w = self.make_widget(cols=40, rows=5)
        for i in range(8):
            self.feed(w, "line %d\r\n" % i)
        self.assertGreater(w._get_history_count(), 0)
        w._select_all_mode = True
        self.assertEqual(
            w._get_selected_text(),
            "\n".join("line %d" % i for i in range(8)),
        )

    def test_cjk_full_row_selection(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "中文ABC混排\r\n")
        w._selection_start = (0, 0)
        w._selection_end = (0, 39)
        self.assertEqual(w._get_selected_text(), "中文ABC混排")

    def test_cjk_partial_selection_wide_char_cells(self):
        # 全角字符占两列：列 0-3 = 「中文」
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "中文ABC混排\r\n")
        w._selection_start = (0, 0)
        w._selection_end = (0, 3)
        self.assertEqual(w._get_selected_text(), "中文")

    def test_cjk_selection_starting_at_second_cell_of_wide_char(self):
        # 选区起点落在宽字符的第二列 → 该宽字符仍被包含
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "中文ABC混排\r\n")
        w._selection_start = (0, 1)
        w._selection_end = (0, 5)
        self.assertEqual(w._get_selected_text(), "中文AB")


class TestPureHelpers(TerminalTextExtractBase):
    """启发式依赖的纯辅助函数"""

    def test_need_boundary_space(self):
        w = self.make_widget()
        # Latin+Latin 需要补空格
        self.assertTrue(w._need_boundary_space("a", "b"))
        # CJK+CJK 不需要
        self.assertFalse(w._need_boundary_space("中", "文"))
        # Latin+CJK 需要（词边界）
        self.assertTrue(w._need_boundary_space("a", "中"))
        # 边界已有空白则不补
        self.assertFalse(w._need_boundary_space(" ", "b"))
        self.assertFalse(w._need_boundary_space("a", " "))
        # 空输入
        self.assertFalse(w._need_boundary_space("", "b"))
        self.assertFalse(w._need_boundary_space("a", ""))

    def test_is_wide_char(self):
        w = self.make_widget()
        self.assertTrue(w._is_wide_char("中"))
        self.assertFalse(w._is_wide_char("a"))
        # 半角片假名是 East Asian "H"（Halfwidth），不算宽字符
        self.assertFalse(w._is_wide_char("ｱ"))
        self.assertFalse(w._is_wide_char(""))

    def test_list_marker_regex(self):
        RE = TerminalWidget._LIST_MARKER_RE
        for s in ["- item", "* item", "12. item", "1) x", "# Title", "> quote", "a) opt"]:
            self.assertIsNotNone(RE.match(s), s)
        for s in ["normal text", "-nodash", "100x. item"]:
            self.assertIsNone(RE.match(s), s)


if __name__ == "__main__":
    unittest.main()
