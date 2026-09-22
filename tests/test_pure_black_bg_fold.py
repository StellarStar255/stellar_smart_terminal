"""真彩色纯黑背景折算成终端默认底色（所有主题）。

Claude Code 启动 logo 用 ``\\x1b[48;2;0;0;0m`` 画"眼睛"，不管终端底色是什么
（实测对 OSC 11 回任何颜色都一样）。底色非纯黑的主题（紫/琥珀/粉/浅色）里
照画就成一块块黑斑。浅色主题折成白底后，白字由 _ensure_visible 压暗兜底。
"""
import unittest

from PyQt6.QtGui import QColor

from terminal_render import TerminalRenderMixin
from terminal_widget import TerminalWidget


class _Stub(TerminalRenderMixin):
    def __init__(self, bg: str):
        self.bg_color = QColor(bg)
        self.fg_color = QColor("#abb2bf")
        self._current_colors = TerminalWidget.DEFAULT_COLORS.copy()
        self._current_bright_colors = TerminalWidget.BRIGHT_COLORS.copy()


class PureBlackBgTest(unittest.TestCase):
    def test_dark_theme_pure_black_bg_becomes_terminal_bg(self):
        t = _Stub("#1a1424")  # 紫色主题的终端底
        for color in ('000000', '#000000', (0, 0, 0), 16):
            with self.subTest(color=color):
                self.assertEqual(t._compute_color(color, False, True).name(), "#1a1424")

    def test_dark_theme_near_black_and_fg_untouched(self):
        t = _Stub("#1a1424")
        # 256 色 232 号是 #080808，不是纯黑，照原样画
        self.assertEqual(t._compute_color('080808', False, True).name(), "#080808")
        # 纯黑**前景**不折算（另有可见性保护逻辑处理）
        self.assertEqual(t._compute_color('000000', False, False).name(), "#000000")

    def test_light_theme_pure_black_bg_becomes_terminal_bg(self):
        t = _Stub("#ffffff")
        self.assertEqual(t._compute_color('000000', False, True).name(), "#ffffff")
        self.assertEqual(t._compute_color((0, 0, 0), False, True).name(), "#ffffff")

    def test_light_theme_white_text_on_folded_black_bg_stays_readable(self):
        # 折成白底后，原本"白字黑底"的白字不能跟着消失：可见性保护按解析后的底色压暗
        t = _Stub("#ffffff")
        t._visible_color_cache = {}
        t._COLOR_CACHE_MAX = 64
        bg = t._compute_color('000000', False, True)
        fg = t._ensure_visible(QColor("#ffffff"), bg)
        lum = 0.299 * fg.red() + 0.587 * fg.green() + 0.114 * fg.blue()
        self.assertLess(lum, 195)

    def test_ansi_black_bg_still_terminal_bg_on_any_theme(self):
        # 既有规则：SGR 40 的 'black' 背景在所有主题下都等于默认底色
        self.assertEqual(_Stub("#ffffff")._compute_color('black', False, True).name(), "#ffffff")
        self.assertEqual(_Stub("#1a1424")._compute_color('black', False, True).name(), "#1a1424")


if __name__ == "__main__":
    unittest.main()
