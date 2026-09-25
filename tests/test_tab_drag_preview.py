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

    def test_mini_window_body_follows_thumbnail_aspect(self):
        from PyQt6.QtGui import QColor, QPixmap
        from widgets import TabDragPreview
        plain = self._card('pane')
        self.assertEqual(plain._body_h, TabDragPreview.DEFAULT_BODY_H)  # 无图也有「窗口内容」区
        wide = QPixmap(520, 260)          # 2:1 → 内容区高 = 宽 / 2
        wide.fill(QColor('#123'))
        self.assertEqual(self._card('w', thumbnail=wide)._body_h, TabDragPreview.WIDTH // 2)
        tall = QPixmap(520, 2000)         # 超高截图封顶
        tall.fill(QColor('#123'))
        c = self._card('t', thumbnail=tall)
        self.assertEqual(c._body_h, TabDragPreview.MAX_BODY_H)
        self.assertFalse(c.grab().isNull())

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
        from PyQt6.QtGui import QPixmap
        shot = QPixmap(520, 300)
        shot.fill(QColor('#224'))
        asked = []
        lw.thumbnail_provider = lambda wid: (asked.append(wid), shot)[1]
        it = QListWidgetItem('3. stellar_search_everything')
        it.setData(Qt.ItemDataRole.UserRole, 77)
        it.setForeground(QColor('#22c55e'))
        lw.addItem(it)
        lw.show()
        lw.setCurrentRow(0)
        lw.startDrag(Qt.DropAction.MoveAction)
        self.addCleanup(lw._end_drag)
        self.assertIsInstance(lw._ghost, TabDragPreview)
        self.assertEqual(lw._ghost._title, 'stellar_search_everything')
        self.assertEqual(lw._ghost._dot.name(), '#22c55e')
        self.assertEqual(lw._ghost._title_bg.name(), '#101010')
        self.assertEqual(asked, [77])                  # 拖影里是该窗口的缩略图
        self.assertIsNotNone(lw._ghost._thumb)


    def test_tabs_use_vector_close_button_that_follows_theme(self):
        """标签 × 是自绘 TabCloseButton（不是红球 QPushButton），切主题会重新取色。"""
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QTabBar
        import main_window
        from widgets import TabCloseButton
        w = main_window.MainWindow()
        self.addCleanup(w.deleteLater)
        w._add_new_tab(tab_name='t')
        bar = w.tab_widget.tabBar()
        for i in range(bar.count()):
            self.assertIsInstance(bar.tabButton(i, QTabBar.ButtonPosition.RightSide),
                                  TabCloseButton)
        for name in ('浅色', '午夜黑'):
            w._apply_theme(name)
            btn = bar.tabButton(bar.count() - 1, QTabBar.ButtonPosition.RightSide)
            self.assertEqual(btn._x.name(), QColor(w.THEMES[name]['text_dim']).name())
        # 右侧自带留白：× 不再贴着标签边缘
        self.assertGreaterEqual(btn.width() - TabCloseButton.DIAMETER, TabCloseButton.RIGHT_GUTTER)


if __name__ == '__main__':
    unittest.main()
