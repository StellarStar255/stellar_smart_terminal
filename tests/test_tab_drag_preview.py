"""拖标签 / 窗格 / 导航条目时的拖影卡片（自绘，不再截图）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_tab_drag_preview.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTabDragPreview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _card(self, *a, **k):
        from widgets import TabDragPreview
        c = TabDragPreview(*a, **k)
        self.addCleanup(c.deleteLater)
        return c

    def test_long_title_is_elided_and_width_capped(self):
        from widgets import TabDragPreview
        c = self._card('x' * 400, {'bg_light': '#222', 'text': '#eee', 'accent': '#66f'})
        self.assertIn('…', c._title)
        self.assertLessEqual(c._card_w, TabDragPreview.MAX_TEXT_W + 60)

    def test_follow_puts_card_at_cursor_offset_not_under_cursor(self):
        from PyQt6.QtCore import QPoint
        from widgets import TabDragPreview
        c = self._card('tab')
        c.follow(QPoint(500, 400))
        card_top_left = c.pos() + QPoint(TabDragPreview.SHADOW, TabDragPreview.SHADOW)
        self.assertEqual(card_top_left, QPoint(500, 400) + TabDragPreview.CURSOR_OFFSET)
        # 整个小窗（含阴影边）都不能盖住光标，否则 topLevelAt(光标) 会查到它
        self.assertFalse(c.frameGeometry().contains(QPoint(500, 400)))

    def test_thumbnail_adds_height_and_paints(self):
        from PyQt6.QtGui import QColor, QPixmap
        thumb = QPixmap(480, 300)
        thumb.fill(QColor('#123'))
        plain = self._card('pane')
        with_thumb = self._card('pane', thumbnail=thumb)
        self.assertGreater(with_thumb.height(), plain.height() + 100)
        self.assertFalse(with_thumb.grab().isNull())

    def test_navigator_drag_uses_card_with_theme_and_clean_title(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QListWidgetItem
        import main_window  # noqa: F401
        import window_navigator as wn
        from widgets import TabDragPreview
        lw = wn.NavListWidget()
        self.addCleanup(lw.deleteLater)
        lw.ghost_theme = {'bg_light': '#101010', 'text': '#fafafa', 'accent': '#ff00ff'}
        it = QListWidgetItem('3. stellar_search_everything')
        it.setForeground(QColor('#22c55e'))
        lw.addItem(it)
        lw.show()
        lw.setCurrentRow(0)
        lw.startDrag(Qt.DropAction.MoveAction)
        self.addCleanup(lw._end_drag)
        self.assertIsInstance(lw._ghost, TabDragPreview)
        self.assertEqual(lw._ghost._title, 'stellar_search_everything')
        self.assertEqual(lw._ghost._dot.name(), '#22c55e')
        self.assertEqual(lw._ghost._bg.name(), '#101010')


if __name__ == '__main__':
    unittest.main()
