# -*- coding: utf-8 -*-
"""面包屑相邻目录名之间的间距不能太宽。

用户反馈："Documents  /  Zhiyuan_Mac  /  proj" 中间空得难看。原因是每段用
QToolButton，macOS 样式给它 10px 固定额外宽度（padding 调 0 也去不掉），加上
分隔符两侧各 3px，相邻名字之间空出约 25px。改用可点击的 QLabel 后由样式表
的 padding 决定，约 10px。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_breadcrumb_spacing.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class TestBreadcrumbSpacing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_gap_between_adjacent_names_is_compact(self):
        from explorer_widget import _BreadcrumbBar
        from PyQt6.QtGui import QFontMetrics
        bar = _BreadcrumbBar()
        bar.resize(600, 24)
        bar.set_path('/Users/x/Documents/Zhiyuan_Mac/proj')
        bar.show()
        self.app.processEvents()
        # 收集按布局顺序排列的可见段控件（有文字的）
        items = []
        for i in range(bar._layout.count()):
            w = bar._layout.itemAt(i).widget()
            if w is not None and w.text():
                items.append(w)
        names = [w for w in items if w.text() != '/']
        self.assertGreaterEqual(len(names), 3)
        fm = QFontMetrics(bar.font())
        # 相邻两个目录名的文字之间的空白（含分隔符本身的字宽）
        gaps = []
        for a, b in zip(names, names[1:]):
            a_text_right = a.x() + (a.width() + fm.horizontalAdvance(a.text())) // 2
            b_text_left = b.x() + (b.width() - fm.horizontalAdvance(b.text())) // 2
            gaps.append(b_text_left - a_text_right)
        # 旧实现（QToolButton）在 mac 上是 22-25px；新实现 mac 约 10-11px、
        # Linux CI 的字体下 12-15px（末段加粗、字宽估算偏差）。阈值取 18。
        self.assertTrue(all(g <= 18 for g in gaps),
                        f"相邻目录名之间空白过宽: {gaps}px（含 '/' 字宽）")
        self.assertTrue(all(g >= 6 for g in gaps),
                        f"相邻目录名挤在一起了: {gaps}px")

    def test_segment_click_still_navigates(self):
        from explorer_widget import _BreadcrumbBar, _CrumbLabel
        bar = _BreadcrumbBar()
        bar.resize(600, 24)
        bar.set_path('/Users/x/Documents')
        got = []
        bar.path_selected.connect(got.append)
        seg = [w for w in bar.findChildren(_CrumbLabel) if w.text() == 'x'][0]
        seg.clicked.emit()
        self.assertEqual(got, ['/Users/x'])



class TestNoPhantomSampleWidgets(unittest.TestCase):
    """_seg_overhead() 的测量样本不能成为面包屑的子控件（否则会画在左上角）"""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_only_real_segments_are_children(self):
        from PyQt6.QtWidgets import QLabel
        from explorer_widget import _BreadcrumbBar
        bar = _BreadcrumbBar()
        bar.resize(600, 24)
        bar.set_path('/Users/x/Documents')
        bar.show()
        self.app.processEvents()
        in_layout = {bar._layout.itemAt(i).widget() for i in range(bar._layout.count())}
        strays = [w for w in bar.findChildren(QLabel) if w not in in_layout]
        self.assertEqual([w.text() for w in strays], [],
                         "面包屑里有不在布局中的子控件（测量样本泄漏，会画成多余的 '/'）")


if __name__ == '__main__':
    unittest.main()
