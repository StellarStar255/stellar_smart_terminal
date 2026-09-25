# -*- coding: utf-8 -*-
"""浅色主题整体重设计（2026-09）。

用户反馈：浅色主题灰上叠灰"像有 bug"，工具栏一排饱和彩块、导航列表里的
窗口色文字看不清、Claude Code 的用户消息在浅色底上是黑条。覆盖：
1. 浅色配色表对比度：正文/次要文字对面板与输入区、白字对填色按钮；
2. 品牌色按钮在浅色主题下中性化、checked 才上强调色，深色主题原样；
3. 标签页 × 按钮浅色主题不再是红球；
4. 窗口色作为文字色在浅色下被压暗（readable_on_light）；
5. 终端应答 OSC 10/11/12 颜色查询（TUI 据此自动选浅色配色）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_light_theme_2026_09.py -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import themes
from themes import THEMES, readable_on_light


def _lum(hex_color: str) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


LIGHT = THEMES["浅色"]
DARK = THEMES["午夜黑"]


class TestLightPalette(unittest.TestCase):
    def test_text_contrast_on_every_surface(self):
        for surface in ('bg_darkest', 'bg_dark', 'bg_medium', 'bg_lighter', 'bg_hover'):
            with self.subTest(surface=surface):
                self.assertGreaterEqual(contrast(LIGHT['text'], LIGHT[surface]), 7.0)
        # 次要文字只出现在面板/输入区上，不会落在按钮 hover 底上
        for surface in ('bg_darkest', 'bg_dark', 'bg_medium', 'bg_lighter'):
            with self.subTest(surface=surface):
                self.assertGreaterEqual(contrast(LIGHT['text_dim'], LIGHT[surface]), 4.0)

    def test_white_text_on_filled_buttons(self):
        for key in ('accent', 'success', 'danger'):
            with self.subTest(key=key):
                self.assertGreaterEqual(contrast('#ffffff', LIGHT[key]), 3.0)

    def test_light_surfaces_are_actually_light_and_layered(self):
        # 输入区/列表是纯白，面板比窗口底浅：层级感靠明度而不是靠一片中灰
        self.assertEqual(LIGHT['bg_medium'].lower(), '#ffffff')
        self.assertGreater(_lum(LIGHT['bg_dark']), _lum(LIGHT['bg_darkest']))
        self.assertGreater(_lum(LIGHT['bg_darkest']), 0.8)

    def test_terminal_is_white_with_dark_text(self):
        self.assertEqual(LIGHT['terminal_bg'].lower(), '#ffffff')
        self.assertGreaterEqual(contrast(LIGHT['terminal_fg'], LIGHT['terminal_bg']), 12.0)
        for name, c in LIGHT['terminal_colors'].items():
            with self.subTest(color=name):
                self.assertGreaterEqual(contrast(c, LIGHT['terminal_bg']), 3.0)


class TestReadableOnLight(unittest.TestCase):
    def test_bright_window_colors_get_darkened(self):
        for c in ('#667eea', '#22c55e', '#facc15', '#38bdf8', '#f59e0b'):
            with self.subTest(color=c):
                out = readable_on_light(c)
                self.assertNotEqual(out, c)
                self.assertGreaterEqual(contrast(out, '#ffffff'), 4.0)

    def test_dark_colors_and_garbage_pass_through(self):
        self.assertEqual(readable_on_light('#1d1d1f'), '#1d1d1f')
        self.assertEqual(readable_on_light('red'), 'red')
        self.assertEqual(readable_on_light(''), '')
        self.assertEqual(readable_on_light(None), None)

    def test_is_light(self):
        self.assertTrue(themes.is_light(LIGHT))
        self.assertFalse(themes.is_light(DARK))


class TestBrandButtonQss(unittest.TestCase):
    def test_light_theme_neutralises_brand_colours(self):
        from main_window_theme import brand_button_qss
        qss = brand_button_qss(LIGHT, '#22c55e', '#4ade80', '#16a34a')
        self.assertNotIn('#22c55e', qss)
        self.assertNotIn('#4ade80', qss)
        self.assertNotIn('#16a34a', qss)
        self.assertIn(LIGHT['bg_lighter'], qss)
        self.assertIn(LIGHT['text'], qss)
        # 面板打开（checked）才上强调色
        self.assertIn('QPushButton:checked', qss)
        self.assertIn(LIGHT['accent'], qss)

    def test_light_theme_non_checkable_has_no_checked_rule(self):
        from main_window_theme import brand_button_qss
        qss = brand_button_qss(LIGHT, '#007ACC', '#1a8ad4')
        self.assertNotIn(':checked', qss)

    def test_dark_theme_keeps_brand_colours(self):
        from main_window_theme import brand_button_qss
        qss = brand_button_qss(DARK, '#22c55e', '#4ade80', '#16a34a')
        for c in ('#22c55e', '#4ade80', '#16a34a'):
            self.assertIn(c, qss)
        self.assertIn('color: white', qss)

    def test_tab_close_button(self):
        """× 平时是次要文字色（不再挂红球），hover 才浮出危险色圆底——深浅主题一致。"""
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QApplication
        from widgets import TabCloseButton
        # 挂到类上：局部变量出作用域时 QApplication 会被回收，残留控件随后段错误
        type(self)._qt_app = QApplication.instance() or QApplication([])
        for theme in (LIGHT, DARK):
            b = TabCloseButton()
            b.set_theme(theme)
            self.assertEqual(b._x.name(), QColor(theme['text_dim']).name())
            self.assertEqual(b._hover_bg.name(), QColor(theme.get('danger', '#e74c3c')).name())
            b.deleteLater()

    def test_pane_splitter_follows_theme(self):
        from main_window_theme import pane_splitter_qss
        self.assertIn(LIGHT['border'], pane_splitter_qss(LIGHT))
        self.assertNotIn('#3d3d5c', pane_splitter_qss(LIGHT))

    def test_menu_is_white_in_light_theme(self):
        from main_window_theme import menu_qss
        self.assertIn(f"background-color: {LIGHT['bg_medium']}", menu_qss(LIGHT))
        self.assertIn(f"background-color: {DARK['bg_light']}", menu_qss(DARK))

    def test_git_generate_and_push_are_neutral_in_light(self):
        from git_widget import GitCommitWidget
        qss = GitCommitWidget._generate_btn_style(LIGHT)
        self.assertNotIn('#7c3aed', qss)
        self.assertIn(LIGHT['bg_lighter'], qss)
        self.assertIn('#7c3aed', GitCommitWidget._generate_btn_style(DARK))


class TestOscColorQuery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.deleteLater)
        sent = []

        class _FakeBackend:
            def write(self, data):
                sent.append(data)
                return True
        w._backend = _FakeBackend()
        return w, sent

    def test_replies_to_osc_11_with_current_background(self):
        from PyQt6.QtGui import QColor
        w, sent = self._widget()
        w.bg_color = QColor('#ffffff')
        w.fg_color = QColor('#1d1d1f')
        w._process_output_text('\x1b]11;?\x07')
        self.assertEqual(sent, [b'\x1b]11;rgb:ffff/ffff/ffff\x07'])
        # 查询本身不能泄漏到屏幕
        self.assertNotIn('?', ''.join(w.screen.display))

    def test_replies_to_osc_10_with_st_terminator(self):
        from PyQt6.QtGui import QColor
        w, sent = self._widget()
        w.fg_color = QColor('#1d1d1f')
        w._process_output_text('abc\x1b]10;?\x1b\\def')
        self.assertEqual(sent, [b'\x1b]10;rgb:1d1d/1d1d/1f1f\x1b\\'])
        self.assertIn('abcdef', w.screen.display[0])

    def test_osc_12_cursor_and_multiple_queries_in_one_chunk(self):
        from PyQt6.QtGui import QColor
        w, sent = self._widget()
        w.bg_color = QColor('#000000')
        w._cursor_color = QColor(200, 200, 200, 180)
        w._process_output_text('\x1b]11;?\x1b\\\x1b]12;?\x1b\\')
        self.assertEqual(sent, [b'\x1b]11;rgb:0000/0000/0000\x1b\\',
                                b'\x1b]12;rgb:c8c8/c8c8/c8c8\x1b\\'])

    def test_osc_11_set_is_not_a_query(self):
        w, sent = self._widget()
        w._process_output_text('\x1b]11;#000000\x07')
        self.assertEqual(sent, [])


if __name__ == '__main__':
    unittest.main()


class TestComboPopupSelectedRowVisible(unittest.TestCase):
    """下拉列表选中项：给 ::item 写了 padding/圆角后 Qt 不画 selection-background-color，
    选中项变成白字白底（浅色主题下"选项完全看不清"）。用离屏 grab 断言选中行
    真的画出了强调色。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.win = main_window.MainWindow()
        cls.win.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtGui import QCloseEvent
        from PyQt6.QtWidgets import QApplication
        cls.win._force_closing = True
        QApplication.sendEvent(cls.win, QCloseEvent())
        cls.win.deleteLater()
        cls.app.processEvents()
        del cls.win

    def _accent_pixels(self, combo, accent):
        from PyQt6.QtGui import QColor
        combo.showPopup()
        for _ in range(5):
            self.app.processEvents()
        try:
            img = combo.view().grab().toImage()
        finally:
            combo.hidePopup()
            self.app.processEvents()
        target = QColor(accent).rgb() & 0xFFFFFF
        hits = 0
        for y in range(0, img.height(), 2):
            for x in range(0, img.width(), 2):
                if (img.pixel(x, y) & 0xFFFFFF) == target:
                    hits += 1
        return hits

    def test_light_theme_selected_row_paints_accent(self):
        self.win.current_theme = "浅色"
        self.win._apply_theme("浅色")
        self.app.processEvents()
        for name in ('lang_combo', 'theme_combo', 'preset_combo'):
            with self.subTest(combo=name):
                combo = getattr(self.win, name)
                self.assertGreater(self._accent_pixels(combo, LIGHT['accent']), 20)
