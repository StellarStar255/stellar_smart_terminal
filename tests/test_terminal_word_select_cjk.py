"""终端双击选词：含中文的文件名要整段选中。

以前「单词字符集」只有 ASCII，中文字符不算词的一部分；而且宽字符占两格、
第二格是空格占位，所以双击 images_容器空满状态_网络爬取_20260907_crops.zip
只能选到 _20260907_crops.zip（用户截图实测）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_terminal_word_select_cjk.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402
from pyte.screens import wcwidth  # noqa: E402

from terminal_widget import TerminalWidget  # noqa: E402


def _col_of(text: str, index: int) -> int:
    """text[index] 这个字符落在第几格（前面的宽字符各占两格）。"""
    return sum(max(wcwidth(ch), 0) for ch in text[:index])


class WordSelectCjkTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = TerminalWidget()
        self.w.term_cols, self.w.term_rows = 80, 10
        self.w.screen.resize(10, 80)

    def tearDown(self):
        try:
            self.w.cleanup()
        except Exception:
            pass
        self.w.deleteLater()

    def _select(self, line: str, char_index: int, word_chars=None):
        self.w.stream.feed(line + "\r\n")
        self.w._select_word_at((0, _col_of(line, char_index)), word_chars)
        return self.w._get_selected_text().strip()

    def test_double_click_on_ascii_tail_selects_whole_cjk_name(self):
        name = "images_容器空满状态_网络爬取_20260907_crops.zip"
        got = self._select(name, name.index("crops"))
        self.assertEqual(got, name)

    def test_double_click_on_cjk_char_selects_whole_name(self):
        name = "images_容器空满状态_网络爬取_20260907.zip"
        got = self._select(name, name.index("满"))
        self.assertEqual(got, name)

    def test_double_click_on_wide_placeholder_cell_selects_whole_name(self):
        name = "images_容器空满_20260907.zip"
        # 「容」占两格，点在它的第二格（占位格）上
        self.w.stream.feed(name + "\r\n")
        self.w._select_word_at((0, _col_of(name, name.index("容")) + 1))
        self.assertEqual(self.w._get_selected_text().strip(), name)

    def test_fullwidth_punctuation_still_breaks_words(self):
        line = "第一段，第二段 tail"
        got = self._select(line, line.index("一"))
        self.assertEqual(got, "第一段")

    def test_plain_ascii_behaviour_unchanged(self):
        line = "cd /home/huangqiliang/proj && ls"
        got = self._select(line, line.index("huang"))
        self.assertEqual(got, "/home/huangqiliang/proj")
        # 三击「窄」集：只取分隔符之间的一段
        self.w._selection_start = self.w._selection_end = None
        self.w._select_word_at((0, line.index("huang")), self.w._WORD_CHARS_NARROW)
        self.assertEqual(self.w._get_selected_text().strip(), "huangqiliang")


if __name__ == '__main__':
    unittest.main()
