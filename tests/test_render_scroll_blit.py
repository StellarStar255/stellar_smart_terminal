"""渲染优化的像素一致性测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_render_scroll_blit.py -v

覆盖两条新路径：
1. 回看滚动的 pixmap 平移复用 —— 按行比较新旧像素带高度，一致的成段
   blit、不一致的重绘；结果必须与整屏重绘逐像素一致（任何分数行距下）。
2. ASCII 合批 drawText —— 合批开/关的渲染结果一致（仅在列宽为整数像素
   时可逐像素比较，分数列宽存在亚像素差异则跳过）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _RenderTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, lines=200):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.resize(640, 400)
        w._update_terminal_size()
        # 混合内容：编号 + ASCII 文本 + 少量宽字符，覆盖合批与逐格两条路径
        for i in range(lines):
            w.stream.feed(f"line {i:04d} the quick brown fox 中文宽字符\r\n")
        return w

    def _rebuild(self, w):
        with w._screen_lock:
            w._rebuild_cache()

    def _force_full_rebuild(self, w):
        w._last_render_state = None
        w._cache_valid = False
        self._rebuild(w)

    def _image(self, w):
        return w._cache_pixmap.toImage()

    def _scroll_to(self, w, offset):
        w.scroll_offset = offset
        w._invalidate_display_info()
        w._cache_valid = False
        self._rebuild(w)


class TestScrollBlit(_RenderTestBase):
    def test_scroll_blit_matches_full_redraw(self):
        """向上回看：平移复用与整屏重绘逐像素一致（各种步长）"""
        w = self._widget()
        for delta in (1, 2, 3, 7):
            with self.subTest(delta=delta):
                self._scroll_to(w, 20)
                self._force_full_rebuild(w)

                blit_before = w._scroll_blit_hits
                self._scroll_to(w, 20 + delta)
                self.assertEqual(w._scroll_blit_hits, blit_before + 1,
                                 "滚动未命中 pixmap 平移复用路径")
                img_blit = self._image(w)

                self._force_full_rebuild(w)
                self.assertEqual(img_blit, self._image(w),
                                 "平移复用与整屏重绘的像素不一致")

    def test_scroll_blit_down_matches_full_redraw(self):
        """向下滚回（delta > 0 分支）同样逐像素一致"""
        w = self._widget()
        self._scroll_to(w, 30)
        self._force_full_rebuild(w)

        blit_before = w._scroll_blit_hits
        self._scroll_to(w, 25)
        self.assertEqual(w._scroll_blit_hits, blit_before + 1)
        img_blit = self._image(w)

        self._force_full_rebuild(w)
        self.assertEqual(img_blit, self._image(w))

    def test_scroll_back_to_bottom_matches(self):
        """一路滚回底部（offset → 0）也走平移且像素一致"""
        w = self._widget()
        self._scroll_to(w, 4)
        self._force_full_rebuild(w)

        blit_before = w._scroll_blit_hits
        self._scroll_to(w, 0)
        self.assertEqual(w._scroll_blit_hits, blit_before + 1)
        img_blit = self._image(w)

        self._force_full_rebuild(w)
        self.assertEqual(img_blit, self._image(w))

    def test_new_output_falls_back_to_full_redraw(self):
        """回看期间有新输出（历史增长）时不得走平移路径"""
        w = self._widget()
        self._scroll_to(w, 10)
        self._force_full_rebuild(w)

        w.stream.feed("new output line\r\n")
        blit_before = w._scroll_blit_hits
        self._scroll_to(w, 11)
        self.assertEqual(w._scroll_blit_hits, blit_before,
                         "历史增长后仍走了平移路径（内容会错位）")

    def test_noop_rebuild_while_scrolled(self):
        """回看时位置与内容都没变（如光标闪烁触发）→ 既不平移也不整屏重画"""
        w = self._widget()
        self._scroll_to(w, 10)
        self._force_full_rebuild(w)
        img_before = self._image(w)

        blit_before = w._scroll_blit_hits
        w._cache_valid = False
        self._rebuild(w)
        self.assertEqual(w._scroll_blit_hits, blit_before)
        self.assertEqual(img_before, self._image(w))

    def test_drawpixmap_source_rect_is_physical(self):
        """回归守卫：drawPixmap 源矩形按物理像素解释（带 DPR 的源 pixmap）。

        平移复用在 Retina (dpr=2) 上依赖此语义（源坐标乘 dpr 做 1:1 blit）；
        若 Qt 版本升级改变语义，此测试会先失败。
        """
        from PyQt6.QtGui import QPixmap, QPainter, QColor
        src = QPixmap(100, 100)
        src.setDevicePixelRatio(2.0)
        p = QPainter(src)
        p.fillRect(0, 0, 50, 10, QColor('red'))    # 逻辑顶部 10 行 = 物理 0-19
        p.fillRect(0, 10, 50, 40, QColor('green'))
        p.end()
        dst = QPixmap(100, 100)
        dst.setDevicePixelRatio(2.0)
        dst.fill(QColor('black'))
        p = QPainter(dst)
        # 源矩形按物理像素：物理 (0,20,100,20) = 逻辑 10-20 的绿带 → 画到顶部
        p.drawPixmap(0, 0, src, 0, 20, 100, 20)
        p.end()
        img = dst.toImage()
        self.assertEqual(img.pixelColor(10, 10).name(), '#008000')
        self.assertEqual(img.pixelColor(10, 25).name(), '#000000')


class TestAsciiBatch(_RenderTestBase):
    def _monospace_font(self):
        """返回真实解析的等宽字体；环境无字体时注册系统字体文件兜底。

        Windows 的 offscreen 平台插件用不了系统 GDI 字体库（Qt6 也不再自带
        字体），QFontDatabase 为空，任何 QFont 都解析成无效字体——度量值与
        实际字形排布对不上，像素比较无意义（合批探测会被骗过然后比较失败）。
        此时把系统等宽字体文件注册进 Qt 再用；找不到可注册的字体则返回 None。
        """
        from PyQt6.QtGui import QFont, QFontInfo, QFontDatabase
        f = QFont('Monaco', 11)  # macOS: cell_w 恰为 9.0 的整像素等宽字体
        if QFontInfo(f).family():
            return f
        candidates = [
            r'C:\Windows\Fonts\consola.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
        ]
        for path in candidates:
            if not os.path.exists(path):
                continue
            fid = QFontDatabase.addApplicationFont(path)
            if fid >= 0:
                fams = QFontDatabase.applicationFontFamilies(fid)
                if fams:
                    return QFont(fams[0], 11)
        return None

    def _integral_width_widget(self, lines=50):
        """构造列宽为整数像素的 widget，使合批与逐格可做逐像素比较"""
        from terminal_widget import TerminalWidget
        font = self._monospace_font()
        if font is None:
            self.skipTest("环境无可用字体且找不到可注册的系统等宽字体")
        w = TerminalWidget()
        w.term_font = font
        w._calculate_char_size()
        w.resize(640, 400)
        w._update_terminal_size()
        for i in range(lines):
            w.stream.feed(f"line {i:04d} the quick brown fox 中文宽字符\r\n")
        return w

    def test_batched_matches_per_cell(self):
        w = self._integral_width_widget()
        if not w._ascii_batch_ok:
            self.skipTest("当前字体不满足合批条件（推进 != 列宽）")
        if abs(w.char_width - round(w.char_width)) > 1e-6:
            self.skipTest("列宽为分数像素，合批与逐格存在亚像素差异，跳过逐像素比较")

        w.scroll_offset = 0
        self._force_full_rebuild(w)
        img_batched = self._image(w)

        w._ascii_batch_ok = False
        self._force_full_rebuild(w)
        img_per_cell = self._image(w)
        w._ascii_batch_ok = True

        self.assertEqual(img_batched, img_per_cell,
                         "合批与逐格绘制的像素不一致")

    def test_probe_rejects_mismatched_advance(self):
        """cell 宽度被 CJK fallback 撑大时必须禁用合批"""
        from terminal_widget import TerminalWidget
        from PyQt6.QtGui import QFontMetricsF
        w = self._widget(lines=1)
        fmf = QFontMetricsF(w.term_font)
        adv = float(fmf.horizontalAdvance('W'))
        # 列宽与 ASCII 推进不一致 → 禁止合批
        self.assertFalse(TerminalWidget._probe_ascii_batch(fmf, adv + 1.0))
        # 一致 → 是否允许取决于字体等宽性（仅验证不抛异常）
        TerminalWidget._probe_ascii_batch(fmf, adv)


if __name__ == '__main__':
    unittest.main()


class TestCellLoopOptimizations(_RenderTestBase):
    """cell 循环优化守卫：行截断/属性 memo 不改变渲染结果"""

    def _cell_probe(self, w, row, col):
        """取 (row, col) 格中心的像素颜色"""
        img = self._image(w)
        dpr = round(w.devicePixelRatioF())
        x = int((w.PADDING + (col + 0.5) * w.char_width) * dpr)
        y = int((w.PADDING + w._header_h + (row + 0.5) * w.char_height) * dpr)
        return img.pixelColor(x, y).name()

    def test_trailing_bg_cell_still_painted(self):
        """行内只有行尾一个带背景色的格子：行截断不得把它跳过"""
        w = self._widget(lines=5)
        last_col = w.term_cols - 1
        # 光标定位到本行行尾，写一个红底空格（SGR 41）
        w.stream.feed(f"\x1b[6;{last_col + 1}H\x1b[41m \x1b[0m")
        self._force_full_rebuild(w)
        # 红底（ANSI red），非默认背景
        probe = self._cell_probe(w, 5, last_col)
        self.assertNotEqual(probe, w.bg_color.name())

    def test_reverse_and_bold_render_differently(self):
        """属性 memo 键含 bold/reverse：不同属性不得串色"""
        w = self._widget(lines=1)
        w.stream.feed("\x1b[3;1Hnormal\r\n")
        w.stream.feed("\x1b[4;1H\x1b[7mreverse\x1b[0m\r\n")
        self._force_full_rebuild(w)
        # reverse 行的字符格背景应为前景色（非默认背景）
        self.assertNotEqual(self._cell_probe(w, 3, 0),
                            self._cell_probe(w, 2, 0))

    def test_full_redraw_pixel_stable_across_two_rebuilds(self):
        """两次整屏重绘逐像素一致（memo/缓存不引入不确定性）"""
        w = self._widget(lines=30)
        w.stream.feed("\x1b[31m红色 red\x1b[0m \x1b[1;34mbold blue\x1b[0m\r\n")
        self._force_full_rebuild(w)
        img1 = self._image(w).copy()
        self._force_full_rebuild(w)
        self.assertEqual(img1, self._image(w))
