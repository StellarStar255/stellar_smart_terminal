# -*- coding: utf-8 -*-
"""面包屑 resize 不能每次都全量重建、每次都新建样本控件实测 QSS。

审查发现 resizeEvent → _rebuild → visible_segments → _seg_overhead 每次
都新建 2 个样本控件设样式表量宽度，再把所有段控件拆了重建——拖分栏时
每帧都在建/删十几个 QLabel。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_breadcrumb_rebuild_cache_2026_09_edit.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402

import explorer_common  # noqa: E402


class TestBreadcrumbRebuildCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _bar(self):
        bar = explorer_common._BreadcrumbBar()
        bar.resize(600, 24)
        bar.set_path('/Users/x/Documents/proj')
        bar.show()
        self.app.processEvents()
        self.addCleanup(bar.deleteLater)
        return bar

    def _layout_widgets(self, bar):
        return [bar._layout.itemAt(i).widget() for i in range(bar._layout.count())
                if bar._layout.itemAt(i).widget() is not None]

    def test_same_visible_segments_resize_keeps_children(self):
        bar = self._bar()
        before = self._layout_widgets(bar)
        self.assertGreaterEqual(len(before), 3)
        with mock.patch.object(explorer_common, '_CrumbLabel',
                               wraps=explorer_common._CrumbLabel) as ctor:
            bar.resize(601, 24)   # 宽度变了、可见段没变
            self.app.processEvents()
            self.assertEqual(ctor.call_count, 0,
                             "可见段未变的 resize 又新建了段控件/样本控件")
        after = self._layout_widgets(bar)
        self.assertEqual([id(w) for w in before], [id(w) for w in after],
                         "可见段未变却全量重建了子控件")

    def test_seg_overhead_is_cached_per_font(self):
        bar = self._bar()
        first = bar._seg_overhead()
        with mock.patch.object(explorer_common, '_CrumbLabel',
                               wraps=explorer_common._CrumbLabel) as ctor:
            self.assertEqual(bar._seg_overhead(), first)
            self.assertEqual(ctor.call_count, 0, "_seg_overhead 每次都新建样本控件")

    def test_narrowing_still_collapses(self):
        bar = self._bar()
        bar.resize(60, 24)
        self.app.processEvents()
        shown = bar.visible_segments()
        self.assertIsNone(shown[0][0], "变窄后左侧应折叠成 …")
        texts = [w.text() for w in self._layout_widgets(bar) if hasattr(w, 'text')]
        self.assertIn('…', texts)

    def test_set_colors_forces_rebuild(self):
        bar = self._bar()
        bar.set_colors('#123456', '#654321')
        self.app.processEvents()
        last = [w for w in self._layout_widgets(bar) if w.text() == 'proj'][0]
        self.assertIn('#123456', last.styleSheet())


if __name__ == '__main__':
    unittest.main()
