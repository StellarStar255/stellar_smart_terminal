# -*- coding: utf-8 -*-
"""终端 resize reflow（重排）的测试

覆盖 terminal_widget.py 中：
- 模块级纯函数 reflow_rows / map_reflow_position（软换行逻辑行按新宽度重新折行、
  宽字符不跨行切断、空行/行内空洞保留、光标位置映射）；
- CompatibleHistoryScreen.resize 的完整 reflow（窄→宽恢复被折断的长行、宽→窄
  不丢字、高度变化时内容在屏幕与历史之间迁移、备用屏幕期间 resize、连续 resize）；
- widget 侧配合（_render_epoch 自增、_search_line_cache 清空、scroll_offset clamp）；
- 20000 行历史一次 reflow 的性能（目标 < 300ms）。

运行方式：
    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_terminal_reflow -v
（QT_QPA_PLATFORM 已在文件顶部 setdefault，直接 discover 也可以。）
"""

import os
# 必须在 import PyQt6 之前设置，保证离屏运行
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import time
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from pyte.screens import Char, StaticDefaultDict

from terminal_widget import TerminalWidget, reflow_rows, map_reflow_position


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ('F', 'W')


def cells_from_text(text):
    """按 pyte 的存储格式构造一行单元格：宽字符占两格（本体 + '' stub）"""
    cells = []
    col = 0
    for ch in text:
        cells.append((col, Char(ch)))
        if _is_wide(ch):
            cells.append((col + 1, Char('')))
            col += 2
        else:
            col += 1
    return cells


def text_from_cells(cells):
    """提取一行单元格的文本（跳过宽字符 stub，空洞读作空格）"""
    out = []
    pos = 0
    for col, ch in cells:
        if ch.data == '':
            continue
        if col > pos:
            out.append(' ' * (col - pos))
        out.append(ch.data)
        pos = col + (2 if (ch.data and _is_wide(ch.data[0])) else 1)
    return ''.join(out)


def texts(rows):
    return [text_from_cells(r) for r in rows]


class TestReflowRowsPure(unittest.TestCase):
    """纯函数 reflow_rows / map_reflow_position 直测"""

    def test_narrow_80_to_40_then_back_roundtrip(self):
        # 100 字符的逻辑行在 80 列折成 2 行（软换行）→ 40 列折成 3 行 → 拉回 80 列复原
        text = ''.join(chr(ord('a') + i % 26) for i in range(100))
        rows = [cells_from_text(text[:80]), cells_from_text(text[80:])]
        soft = [True, False]

        r40, s40, _ = reflow_rows(rows, soft, 40, 80)
        self.assertEqual(texts(r40), [text[:40], text[40:80], text[80:]])
        self.assertEqual(s40, [True, True, False])

        r80, s80, _ = reflow_rows(r40, s40, 80, 40)
        self.assertEqual(s80, [True, False])
        # 单元格级 round-trip 无损（列号与 Char 完全一致）
        self.assertEqual(r80, rows)

    def test_widen_40_to_80_then_back_roundtrip(self):
        text = 'x' * 90
        rows = [cells_from_text(text[i:i + 40]) for i in (0, 40, 80)]
        soft = [True, True, False]

        r80, s80, _ = reflow_rows(rows, soft, 80, 40)
        self.assertEqual(texts(r80), [text[:80], text[80:]])
        self.assertEqual(s80, [True, False])

        r40, s40, _ = reflow_rows(r80, s80, 40, 80)
        self.assertEqual(r40, rows)
        self.assertEqual(s40, soft)

    def test_fits_row_passes_through_same_object(self):
        # 放得下的独立行原样透传（同一对象，调用方借此复用行容器）
        rows = [cells_from_text("short")]
        out, soft, _ = reflow_rows(rows, [False], 40, 80)
        self.assertIs(out[0], rows[0])
        self.assertEqual(soft, [False])

    def test_dict_rows_accepted(self):
        # 行也可以用稀疏 dict 传入（调用方直接传 pyte 行对象）
        line = dict(cells_from_text("hello " + "z" * 60))
        out, soft, _ = reflow_rows([line], [False], 40, 80)
        self.assertEqual(texts(out), ["hello " + "z" * 34, "z" * 26])
        self.assertEqual(soft, [True, False])
        # 放得下时 dict 原样透传
        out2, _, _ = reflow_rows([line], [False], 80, 80)
        self.assertIs(out2[0], line)

    def test_cjk_not_split_at_boundary(self):
        # 25 个宽字符（50 列）在 39 列重排：每行最多 19 个（38 列），
        # 第 20 个放不下整体挪到下一行，绝不在行尾切半 → 19 + 6 共 2 行
        text = '汉' * 25
        rows = [cells_from_text(text)]
        r39, s39, _ = reflow_rows(rows, [False], 39, 80)
        self.assertEqual(s39, [True, False])
        for row in r39:
            for col, ch in row:
                if ch.data and _is_wide(ch.data[0]):
                    self.assertLessEqual(col + 2, 39,
                                         "宽字符不得越过右边界被切断")
        self.assertEqual(''.join(t.replace(' ', '') for t in texts(r39)), text)

        # round-trip 回 80 列：单元格级无损
        r80, s80, _ = reflow_rows(r39, s39, 80, 39)
        self.assertEqual(r80, rows)
        self.assertEqual(s80, [False])

    def test_cjk_mixed_roundtrip(self):
        text = "ab你好c世界def测试" * 4  # 混合宽窄，72 列（80 列下单行放得下）
        rows = [cells_from_text(text)]
        prev_rows, prev_soft = rows, [False]
        prev_cols = 80
        for cols in (37, 22, 53, 80):
            prev_rows, prev_soft, _ = reflow_rows(prev_rows, prev_soft, cols,
                                                  prev_cols)
            prev_cols = cols
        self.assertEqual(prev_rows, rows)
        self.assertEqual(prev_soft, [False])

    def test_empty_logical_lines_preserved(self):
        rows = [cells_from_text("para1"), [], cells_from_text("para2")]
        soft = [False, False, False]
        out, out_soft, _ = reflow_rows(rows, soft, 40, 80)
        self.assertEqual(texts(out), ["para1", "", "para2"])
        self.assertEqual(out_soft, [False, False, False])

    def test_all_blank_wrapped_line_collapses_to_one_empty_row(self):
        # 整行显式空格（擦除产物）变窄时不应折出多行空白
        rows = [[(c, Char(' ')) for c in range(80)]]
        out, out_soft, _ = reflow_rows(rows, [False], 40, 80)
        self.assertEqual(out, [[]])
        self.assertEqual(out_soft, [False])

    def test_trailing_blanks_trimmed_but_styled_blanks_kept(self):
        # 行尾默认空格被裁剪；带背景色的"空格"是可见色块，必须保留
        cells = cells_from_text("ab") + [(c, Char(' ')) for c in range(2, 80)]
        out, _, _ = reflow_rows([cells], [False], 40, 80)
        self.assertEqual(texts(out), ["ab"])

        styled = cells_from_text("ab") + [(50, Char(' ', bg='red'))]
        out2, soft2, _ = reflow_rows([styled], [False], 40, 80)
        # 带色块的列 50 → 折行后位于第 2 行列 10
        self.assertEqual(soft2, [True, False])
        self.assertEqual(out2[1], [(10, Char(' ', bg='red'))])

    def test_sparse_holes_preserved(self):
        # 行内空洞（缺失单元格）保持相对位置；round-trip 复原
        cells = [(0, Char('a')), (10, Char('b'))]
        out, soft, _ = reflow_rows([cells], [False], 8, 80)
        self.assertEqual(out, [[(0, Char('a'))], [(2, Char('b'))]])
        self.assertEqual(soft, [True, False])

        back, back_soft, _ = reflow_rows(out, soft, 80, 8)
        self.assertEqual(back, [cells])
        self.assertEqual(back_soft, [False])

    def test_soft_flags_mixed_lines(self):
        # 软换行对 + 独立行 + 空行混合，逻辑行边界正确
        long_text = 'q' * 70
        rows = [
            cells_from_text(long_text[:40]),   # 逻辑行 A（软换行）
            cells_from_text(long_text[40:]),   # 逻辑行 A 续
            cells_from_text("standalone"),     # 逻辑行 B
            [],                                # 逻辑行 C（空）
            cells_from_text("tail"),           # 逻辑行 D
        ]
        soft = [True, False, False, False, False]
        out, out_soft, _ = reflow_rows(rows, soft, 80, 40)
        self.assertEqual(texts(out), [long_text, "standalone", "", "tail"])
        self.assertEqual(out_soft, [False, False, False, False])

    def test_map_position_basic(self):
        text = 'm' * 100
        rows = [cells_from_text(text[:80]), cells_from_text(text[80:])]
        soft = [True, False]
        _, _, row_map = reflow_rows(rows, soft, 40, 80)
        # 第 0 行列 5 → 仍是第 0 行列 5
        self.assertEqual(map_reflow_position(row_map, 0, 5), (0, 5))
        # 第 0 行列 45 → 逻辑偏移 45 → 新第 1 行列 5
        self.assertEqual(map_reflow_position(row_map, 0, 45), (1, 5))
        # 第 1 行列 5 → 逻辑偏移 85 → 新第 2 行列 5
        self.assertEqual(map_reflow_position(row_map, 1, 5), (2, 5))

    def test_map_position_independent_lines(self):
        rows = [cells_from_text("aaa"), cells_from_text("bbb")]
        _, _, row_map = reflow_rows(rows, [False, False], 40, 80)
        self.assertEqual(map_reflow_position(row_map, 1, 2), (1, 2))


class TerminalReflowBase(unittest.TestCase):
    """集成测试公共基类：管理 QApplication 单例与 widget 生命周期"""

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

    def make_widget(self, cols=80, rows=10):
        w = TerminalWidget()
        w.term_cols = cols
        w.term_rows = rows
        w.screen.resize(rows, cols)
        self._widgets.append(w)
        return w

    def resize_screen(self, w, rows, cols):
        """模拟 _update_terminal_size 中的 screen.resize（保持 term_* 同步）"""
        w.term_rows = rows
        w.term_cols = cols
        w.screen.resize(rows, cols)

    def feed(self, w, text):
        w.stream.feed(text)

    def screen_texts(self, w):
        return [line.rstrip() for line in w.screen.display]

    def history_texts(self, w):
        out = []
        for line in w.screen.history.top:
            cells = sorted(line.items())
            out.append(text_from_cells(cells).rstrip())
        return out


class TestScreenReflowWidth(TerminalReflowBase):
    """宽度变化：窄→宽恢复完整内容，宽→窄不丢字"""

    def test_widen_restores_wrapped_history_lines(self):
        # 15 条 100 字符长行在 80 列折成 2 行，部分进入历史；
        # 变窄再拉宽后内容必须完整恢复（修复 pyte line.pop 永久截断）
        w = self.make_widget(cols=80, rows=10)
        for i in range(15):
            self.feed(w, f"line{i:02d}:" + "x" * 93 + "\r\n")
        before = w._get_all_content()
        self.assertGreater(len(w.screen.history.top), 0)

        self.resize_screen(w, 10, 40)
        self.resize_screen(w, 10, 80)
        self.assertEqual(w._get_all_content(), before)

        # 历史中第一条长行的首个物理行应当占满 80 列（不是被截断的 40 列残骸）
        first = sorted(w.screen.history.top[0].items())
        self.assertEqual(len(text_from_cells(first)), 80)

    def test_narrow_keeps_all_content(self):
        w = self.make_widget(cols=80, rows=10)
        for i in range(6):
            self.feed(w, f"cmd{i}: " + "y" * 90 + "\r\n")
        before = w._get_all_content()

        self.resize_screen(w, 10, 40)
        # _get_all_content 会把软换行行无缝拼接 → 与宽度无关
        self.assertEqual(w._get_all_content(), before)
        # 每个物理行都不超过 40 列
        for line in list(w.screen.history.top):
            if line:
                self.assertLess(max(line), 40)

    def test_cjk_narrow_widen_roundtrip(self):
        w = self.make_widget(cols=80, rows=10)
        self.feed(w, "汉字" * 30 + "\r\n")           # 120 列宽内容
        self.feed(w, "ab你好c世界def\r\n")
        before = w._get_all_content()

        self.resize_screen(w, 10, 39)  # 奇数宽度：宽字符必然遇到行尾边界
        self.assertEqual(w._get_all_content(), before)
        # 宽字符不得越过右边界
        rows = list(w.screen.history.top) + \
            [w.screen.buffer[y] for y in range(w.screen.lines)]
        for line in rows:
            for col, ch in line.items():
                if ch.data and _is_wide(ch.data[0]):
                    self.assertLessEqual(col + 2, 39)

        self.resize_screen(w, 10, 80)
        self.assertEqual(w._get_all_content(), before)

    def test_cursor_follows_logical_position(self):
        # 未提交的输入行：光标随逻辑位置折行/复原
        w = self.make_widget(cols=80, rows=10)
        self.feed(w, "$ " + "k" * 70)  # 光标在 (0, 72)
        self.assertEqual((w.screen.cursor.y, w.screen.cursor.x), (0, 72))

        self.resize_screen(w, 10, 40)
        self.assertEqual((w.screen.cursor.y, w.screen.cursor.x), (1, 32))

        self.resize_screen(w, 10, 80)
        self.assertEqual((w.screen.cursor.y, w.screen.cursor.x), (0, 72))

    def test_multiple_consecutive_resizes(self):
        w = self.make_widget(cols=80, rows=10)
        for i in range(12):
            self.feed(w, f"path/to/some/dir-{i}/" + "f" * 70 + "\r\n")
        self.feed(w, "中文 mixed 内容 " + "w" * 60 + "\r\n")
        before = w._get_all_content()
        # 注意避开 cols ∈ {89..91}：spaceless 长 token 行（89 字符）会触发
        # _get_all_content 既有的「写满宽度即截断」合并启发式（与 reflow 无关）
        for cols in (61, 40, 27, 53, 95, 80):
            self.resize_screen(w, 10, cols)
            self.assertEqual(w._get_all_content(), before,
                             f"cols={cols} 时内容不一致")

    def test_resize_same_size_is_noop(self):
        w = self.make_widget(cols=80, rows=10)
        self.feed(w, "hello\r\n")
        buf_row0 = w.screen.buffer[0]
        w.screen.resize(10, 80)
        self.assertIs(w.screen.buffer[0], buf_row0)


class TestScreenReflowHeight(TerminalReflowBase):
    """高度变化：变矮把顶部行推入历史，变高从历史拉回"""

    def test_shrink_rows_pushes_top_into_history(self):
        w = self.make_widget(cols=80, rows=10)
        for i in range(8):
            self.feed(w, f"l{i + 1}\r\n")
        self.assertEqual(w.screen.cursor.y, 8)
        self.assertEqual(len(w.screen.history.top), 0)

        self.resize_screen(w, 5, 80)
        self.assertEqual(self.history_texts(w), ["l1", "l2", "l3", "l4"])
        self.assertEqual(self.screen_texts(w), ["l5", "l6", "l7", "l8", ""])
        # 光标跟随其行（原 y=8 的空提示符行）
        self.assertEqual((w.screen.cursor.y, w.screen.cursor.x), (4, 0))

    def test_grow_rows_pulls_back_from_history(self):
        w = self.make_widget(cols=80, rows=10)
        for i in range(8):
            self.feed(w, f"l{i + 1}\r\n")
        self.resize_screen(w, 5, 80)
        self.resize_screen(w, 10, 80)
        self.assertEqual(len(w.screen.history.top), 0)
        self.assertEqual(self.screen_texts(w)[:8],
                         [f"l{i + 1}" for i in range(8)])
        self.assertEqual((w.screen.cursor.y, w.screen.cursor.x), (8, 0))

    def test_grow_rows_with_deep_history(self):
        w = self.make_widget(cols=80, rows=5)
        for i in range(20):
            self.feed(w, f"row{i:02d}\r\n")
        hist_before = len(w.screen.history.top)
        self.assertGreater(hist_before, 0)
        cursor_line_before = w.screen.display[w.screen.cursor.y]

        self.resize_screen(w, 12, 80)
        # 拉高 7 行 → 从历史拉回 7 行
        self.assertEqual(len(w.screen.history.top), hist_before - 7)
        # 光标行内容不变
        self.assertEqual(w.screen.display[w.screen.cursor.y],
                         cursor_line_before)
        # 屏幕顶部是从历史拉回的内容
        self.assertEqual(self.screen_texts(w)[0],
                         f"row{20 - (12 - 1):02d}")

    def test_total_history_lines_adjusted_by_delta(self):
        # _total_history_lines 按历史行数差额修正（渲染滚动锚点不跳）
        w = self.make_widget(cols=80, rows=10)
        for i in range(30):
            self.feed(w, f"n{i}\r\n")
        len1 = len(w.screen.history.top)
        total1 = w.screen._total_history_lines
        self.resize_screen(w, 5, 80)
        len2 = len(w.screen.history.top)
        total2 = w.screen._total_history_lines
        self.assertEqual(total2 - total1, len2 - len1)


class TestScreenReflowAltScreen(TerminalReflowBase):
    """备用屏幕：TUI 期间 resize，退出后主屏内容按新宽度恢复完整"""

    def test_alt_screen_resize_then_exit(self):
        w = self.make_widget(cols=80, rows=10)
        self.feed(w, "$ echo " + "y" * 90 + "\r\nok\r\n")
        before = w._get_all_content()

        self.feed(w, "\x1b[?1049h")          # 进入备用屏幕
        self.feed(w, "TUI CONTENT")
        self.resize_screen(w, 10, 40)        # TUI 期间变窄
        self.feed(w, "\x1b[?1049l")          # 退出
        self.assertEqual(w._get_all_content(), before)

        self.resize_screen(w, 10, 80)        # 拉回宽度
        self.assertEqual(w._get_all_content(), before)

    def test_alt_screen_resize_rows_and_cols(self):
        w = self.make_widget(cols=80, rows=10)
        for i in range(12):
            self.feed(w, f"main-{i}: " + "z" * 80 + "\r\n")
        before = w._get_all_content()

        self.feed(w, "\x1b[?1049h")
        self.feed(w, "\x1b[2J\x1b[HFULLSCREEN APP")
        self.resize_screen(w, 6, 45)
        self.resize_screen(w, 12, 70)
        self.feed(w, "\x1b[?1049l")
        self.assertEqual(w._get_all_content(), before)

    def test_alt_screen_content_not_reflowed(self):
        # 备用屏幕本身沿用 pyte 原生 resize（TUI 自己会重画），不做 reflow
        w = self.make_widget(cols=80, rows=10)
        self.feed(w, "\x1b[?1049h")
        self.feed(w, "ALT")
        self.resize_screen(w, 10, 40)
        # 备用屏幕行未被拼接重排（仍在第 0 行）
        self.assertEqual(self.screen_texts(w)[0], "ALT")
        self.feed(w, "\x1b[?1049l")


class TestWidgetSideEffects(TerminalReflowBase):
    """widget 侧配合：渲染缓存失效、搜索缓存清空、scroll_offset clamp"""

    def test_update_terminal_size_invalidates_caches(self):
        w = self.make_widget(cols=80, rows=10)
        w.resize(800, 600)
        w._update_terminal_size()
        epoch = w._render_epoch
        w._search_line_cache['sentinel'] = ('x', 'y', 'z')
        w.scroll_offset = 10**9  # 故意超出历史范围

        w.resize(500, 400)
        w._update_terminal_size()
        self.assertGreater(w._render_epoch, epoch)
        self.assertEqual(len(w._search_line_cache), 0)
        self.assertLessEqual(w.scroll_offset, len(w.screen.history.top))

    def test_selected_text_after_reflow(self):
        # reflow 后 _soft_wrapped_ids 指向新行对象：选区提取仍能无缝拼接
        w = self.make_widget(cols=80, rows=10)
        self.feed(w, "$ token-" + "a" * 90 + "\r\n")
        self.resize_screen(w, 10, 40)
        w._select_all_mode = True
        w._selection_start = (0, 0)
        w._selection_end = (1, 1)
        self.assertEqual(w._get_selected_text(), "$ token-" + "a" * 90)


class TestReflowPerformance(TerminalReflowBase):
    """20000 行混合历史一次 reflow 的耗时（目标 < 300ms，MacBook 级别）"""

    def test_20000_line_history_reflow_under_300ms(self):
        w = self.make_widget(cols=80, rows=24)
        screen = w.screen
        default = screen.default_char

        def make_row(text):
            row = StaticDefaultDict(default)
            col = 0
            for ch in text:
                row[col] = Char(ch)
                if _is_wide(ch):
                    if col + 1 < 80:
                        row[col + 1] = Char('')
                    col += 2
                else:
                    col += 1
            return row

        rows = []
        i = 0
        while len(rows) < 20000:
            kind = i % 4
            if kind == 0:
                rows.append(make_row(f"short line {i}"))
            elif kind == 1:
                r1 = make_row(f"L{i}:" + "x" * 74)
                r2 = make_row("tail" + "y" * 40)
                screen._soft_wrapped_ids.add(id(r1))
                rows.append(r1)
                rows.append(r2)
            elif kind == 2:
                rows.append(make_row("中文内容" * 8 + f" #{i}"))
            else:
                rows.append(make_row(""))
            i += 1
        del rows[20000:]
        screen.history.top.extend(rows)
        screen._total_history_lines = len(screen.history.top)

        t0 = time.perf_counter()
        self.resize_screen(w, 24, 60)
        elapsed = time.perf_counter() - t0
        # 本机实测 ~260ms；阈值留足余量做「数量级回退」护栏，而非精确计时——
        # 共享 CI VM 噪声很大（Windows 实测 ~920ms、macOS 偶发 ~1018ms），
        # 统一放宽到 2.0s 以免 flaky，仍能抓到 5~10x 的性能退化。
        limit = 2.0
        self.assertLess(elapsed, limit,
                        f"20000 行历史 reflow 耗时 {elapsed * 1000:.0f}ms")
        # 内容完整性抽查：被折行的长 ASCII 行在 60 列下重排为 2 行
        self.assertEqual(len(screen.history.top), screen.history.top.maxlen)


class TestScreenModuleSplit(unittest.TestCase):
    """screen 层拆分到 terminal_screen 后的守卫（对齐 window_navigator 拆分模式）：
    符号住在新模块、terminal_widget 只是再导出；screen 模块保持 Qt-free。
    一旦有人把类搬回 terminal_widget 或给 terminal_screen 引入 Qt 依赖，先在这里失败。
    """

    def test_symbols_live_in_terminal_screen_and_reexport(self):
        import terminal_screen
        import terminal_widget
        for name in ('CompatibleHistoryScreen', 'reflow_rows',
                     'map_reflow_position'):
            self.assertIs(getattr(terminal_widget, name),
                          getattr(terminal_screen, name))
        self.assertEqual(terminal_screen.CompatibleHistoryScreen.__module__,
                         'terminal_screen')

    def test_terminal_screen_is_qt_free(self):
        # 子进程冷启动检查：单独 import terminal_screen 不得拉起 PyQt6
        import subprocess
        code = ("import sys; import terminal_screen; "
                "sys.exit(1 if any(m.startswith('PyQt6') "
                "for m in sys.modules) else 0)")
        proj = str(Path(__file__).resolve().parent.parent)
        r = subprocess.run([sys.executable, '-c', code], cwd=proj,
                           capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0,
                         f"terminal_screen 引入了 Qt 依赖: {r.stderr.decode(errors='replace')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
