# -*- coding: utf-8 -*-
"""终端软换行/选区文本提取的测试

覆盖 terminal_widget.py 中 _get_selected_text / _get_all_content 的
wrap_type/threshold 启发式（复制粘贴正确性的核心），以及两者共用的
模块级纯函数 merge_extracted_lines。

spaceless（无内部空格 token）行的合并判据是「写满终端实际宽度
（last_col >= columns - 2）」——曾经的 run_max 相对判据会让块内最长的
spaceless 行恒满足条件，把 ls -1 输出的多条独立路径误并成一行，
TestMergeExtractedLines 中的边界测试固化了修复后的行为。

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

from terminal_widget import TerminalWidget, merge_extracted_lines


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
        # 77 字符的命令在 40 列终端折成两行（终端软换行）→ 无缝合并；
        # 续行只占 37 列（未写满宽度），其后的独立短行 "done" 保留换行
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "$ echo " + "a" * 70 + "\r\ndone\r\n")
        self.assertEqual(
            w._get_all_content(),
            "$ echo " + "a" * 70 + "\ndone",
        )

    def test_long_command_hard_wrapped_merges_via_selection(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "$ echo " + "a" * 70 + "\r\ndone\r\n")
        w._selection_start = (0, 0)
        w._selection_end = (2, w.term_cols - 1)
        self.assertEqual(
            w._get_selected_text(),
            "$ echo " + "a" * 70 + "\ndone",
        )

    def test_cjk_terminal_wrap_merges(self):
        # 35 个全角字符 = 70 列，在 40 列终端折两行，复制应合并为一行；
        # 续行只占 30 列（未写满宽度），其后的独立短行 "ok" 保留换行
        w = self.make_widget(cols=40, rows=10)
        text = "这是一段很长的中文文本用来测试宽字符在终端里的自动换行行为是否正确处理"
        self.feed(w, text + "\r\nok\r\n")
        self.assertEqual(w._get_all_content(), text + "\nok")

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
        # 20 个全角字符 = 40 列，顶满整行 + 显式换行 → 与下一行无缝拼接（CJK 不补空格）
        w = self.make_widget(cols=40, rows=10)
        line_full = "这是第一行的中文内容刚好顶满整行的宽度啦"  # 20 个全角字符
        self.assertEqual(len(line_full) * 2, 40)
        self.feed(w, line_full + "\r\n继续的第二行。\r\n")
        self.assertEqual(
            w._get_all_content(),
            line_full + "继续的第二行。",
        )

    def test_cjk_one_col_short_app_wrap_seamless(self):
        # 行尾因放不下一个全角字符而只填到第 39 列（last_col = columns - 2）：
        # 视同写满，仍无缝拼接 —— 这是 columns-2（而非 columns-1）判据的用途
        w = self.make_widget(cols=40, rows=10)
        line39 = "x" + "这是第一行的中文内容刚好顶满整行的宽度"  # 1 + 19*2 = 39 列
        self.assertEqual(1 + (len(line39) - 1) * 2, 39)
        self.feed(w, line39 + "\r\n继续的第二行。\r\n")
        self.assertEqual(
            w._get_all_content(),
            line39 + "继续的第二行。",
        )

    def test_spaceless_paths_of_varying_length_keep_newlines(self):
        # ls -1 式的多条长短不一的独立路径：没有任何一条写满终端宽度，
        # 必须各占一行（曾经的 run_max 相对判据会把它们误并成一行）
        w = self.make_widget(cols=40, rows=10)
        self.feed(
            w,
            "/usr/local/bin/python3\r\n"
            "/usr/local/share/doc\r\n"
            "/etc/hosts\r\n",
        )
        self.assertEqual(
            w._get_all_content(),
            "/usr/local/bin/python3\n"
            "/usr/local/share/doc\n"
            "/etc/hosts",
        )

    def test_short_spaceless_paths_below_threshold_keep_newlines(self):
        # run_max 低于 threshold_low（max(8, 40*0.40)=16）→ 不触发合并
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "/etc/hosts\r\n/etc/fstab\r\n")
        self.assertEqual(w._get_all_content(), "/etc/hosts\n/etc/fstab")


class TestMergeExtractedLines(unittest.TestCase):
    """merge_extracted_lines 纯函数直测（无 Qt 依赖）

    行元组格式: (text, is_soft, last_content_col, spaceless, can_heuristic)
    重点是 spaceless 行合并判据 last_col >= columns - 2 的边界。
    """

    COLS = 40

    def row(self, text, is_soft=False, last_col=None, spaceless=None, can_h=True):
        if last_col is None:
            last_col = len(text.rstrip()) - 1
        if spaceless is None:
            core = text.strip()
            spaceless = bool(core) and (" " not in core)
        return (text, is_soft, last_col, spaceless, can_h)

    # ---- spaceless 行合并判据的宽度边界 ----

    def test_spaceless_at_columns_minus_1_merges(self):
        # last_col = 39 = columns-1（写满）→ 无缝并入下一行
        rows = [self.row("A" * 40), self.row("tail")]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS), "A" * 40 + "tail"
        )

    def test_spaceless_at_columns_minus_2_merges(self):
        # last_col = 38 = columns-2（差 1 列写满，行尾放不下宽字符的场景）→ 仍合并
        rows = [self.row("A" * 39), self.row("tail")]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS), "A" * 39 + "tail"
        )

    def test_spaceless_at_columns_minus_3_keeps_newline(self):
        # last_col = 37 = columns-3（明显未写满）→ 保留换行
        rows = [self.row("A" * 38), self.row("tail")]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS), "A" * 38 + "\ntail"
        )

    def test_block_longest_spaceless_line_not_merged_when_short_of_width(self):
        # 修复的核心场景：块内最长的 spaceless 行（ls -1 的最长路径）即使是
        # run_max 本身，只要没写满终端宽度就不能并入下一行
        rows = [
            self.row("/usr/local/bin/python3"),
            self.row("/usr/local/share/doc"),
            self.row("/etc/hosts"),
        ]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS),
            "/usr/local/bin/python3\n/usr/local/share/doc\n/etc/hosts",
        )

    def test_spaceless_full_width_not_merged_into_indented_next_line(self):
        # 下一行以空格开头 → 不是 token 截断的续行，保留换行
        rows = [self.row("A" * 40), ("  indented", False, 9, False, True)]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS), "A" * 40 + "\n  indented"
        )

    def test_spaceless_full_width_not_merged_into_list_item(self):
        # 列表项强制保留换行，即使上一行写满
        rows = [self.row("A" * 40), self.row("- item", spaceless=False)]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS), "A" * 40 + "\n- item"
        )

    def test_can_heuristic_false_disables_spaceless_merge(self):
        # 选区未覆盖整行宽度（can_heuristic=False）→ 即使写满也不合并
        rows = [self.row("A" * 40, can_h=False), self.row("tail", can_h=False)]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS), "A" * 40 + "\ntail"
        )

    # ---- 其余换行类型保持原行为 ----

    def test_terminal_soft_wrap_merges_regardless_of_width(self):
        # 终端层软换行标记优先，与宽度判据无关
        rows = [self.row("short", is_soft=True), self.row("rest")]
        self.assertEqual(merge_extracted_lines(rows, self.COLS), "shortrest")

    def test_prose_wrap_uses_block_run_max(self):
        # 散文折行仍用块内 run_max（兼容应用按盒子边距比终端更窄折行）：
        # 两行都只折到 35 列左右，下一行首词放不下 → 合并补空格
        l1 = "The quick brown fox jumps over the"  # 34 字符, last_col=33
        l2 = "lazy dog."
        rows = [self.row(l1), self.row(l2)]
        self.assertEqual(
            merge_extracted_lines(rows, self.COLS),
            "The quick brown fox jumps over the lazy dog.",
        )

    def test_empty_input(self):
        self.assertEqual(merge_extracted_lines([], self.COLS), "")


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


class TestCopyNormalizesNbsp(TerminalTextExtractBase):
    """复制时把 U+00A0 不换行空格归一化为普通空格

    TUI 应用（如 Claude Code/Ink）用 NBSP 做布局缩进；原样进剪贴板后
    粘贴到 shell/代码里会引发肉眼不可见的报错（command not found /
    Python 语法错误），编辑器里显示为 <0xa0> 乱码标记。
    """

    def test_selection_copy_replaces_nbsp(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "\u23bf\xa0Added 3 lines\r\n")
        w._selection_start = (0, 0)
        w._selection_end = (0, w.term_cols - 1)
        w._copy_selection_to_clipboard()
        text = QApplication.clipboard().text()
        self.assertNotIn("\xa0", text)
        self.assertEqual(text, "\u23bf Added 3 lines")

    def test_visible_copy_replaces_nbsp(self):
        w = self.make_widget(cols=40, rows=10)
        self.feed(w, "a\xa0b\r\n")
        w._copy_to_clipboard()
        text = QApplication.clipboard().text()
        self.assertNotIn("\xa0", text)
        self.assertEqual(text, "a b")


if __name__ == "__main__":
    unittest.main()


class TestConPTYFakeSoftWrap(TerminalTextExtractBase):
    """Windows ConPTY 假软换行：重绘把行尾清空区写成真实空格且连续绘制，
    pyte 在右缘自动折行，把应用层折行的行误标成终端软换行——行尾填充
    空格随无缝拼接混进复制结果（跨行 URL 平白多出几个空格）。
    修复：ConPTY（win32）下拼接前剥掉软换行行的尾部空格；Unix 不开启。
    """

    URL_A = "https://claude.com/oauth?scope=org%3Acreate_"
    URL_B = "api_key+user%3Aprofile&state=Z0nFUg"

    def _feed_conpty_style(self, w):
        # 模拟 ConPTY：行内容 + 填充空格写满整行、不发换行连续画下一行
        pad = w.term_cols - len(self.URL_A)
        self.feed(w, self.URL_A + " " * pad + self.URL_B + "\r\n")

    def test_pure_function_strips_soft_trailing_when_enabled(self):
        rows = [
            (self.URL_A + "    ", True, 47, True, True),
            (self.URL_B, False, len(self.URL_B) - 1, True, True),
        ]
        # 开启（win32 路径）：填充空格剥掉，URL 无缝复原
        self.assertEqual(
            merge_extracted_lines(rows, 48, strip_soft_trailing=True),
            self.URL_A + self.URL_B)
        # 默认（mac 路径）：行为不变，尾部空格保留
        self.assertEqual(
            merge_extracted_lines(rows, 48),
            self.URL_A + "    " + self.URL_B)

    def test_widget_copy_on_win32_strips_padding(self):
        w = self.make_widget(cols=60, rows=8)
        self._feed_conpty_style(w)
        # 填充写满整行 + 连续绘制 → pyte 确实把该行标成了软换行
        self.assertTrue(w.screen.is_soft_wrapped(w.screen.buffer[0]))
        old = sys.platform
        try:
            sys.platform = 'win32'
            self.assertEqual(w._get_all_content(), self.URL_A + self.URL_B)
        finally:
            sys.platform = old

    def test_widget_copy_on_mac_keeps_current_behavior(self):
        # 同样的喂流在非 win32 平台维持既有语义（尾部空格视为真实内容保留）
        if sys.platform == 'win32':
            self.skipTest("non-windows only")
        w = self.make_widget(cols=60, rows=8)
        self._feed_conpty_style(w)
        pad = 60 - len(self.URL_A)
        self.assertEqual(w._get_all_content(),
                         self.URL_A + " " * pad + self.URL_B)
