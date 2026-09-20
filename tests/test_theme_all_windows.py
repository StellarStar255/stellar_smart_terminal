"""换主题必须作用于所有 MainWindow 窗口，而不是只换当前窗口。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_theme_all_windows.py -v

回归：_on_theme_changed 以前只在当前窗口调 _apply_theme；主题是全局配置
（config 只存一份 'theme'），其他窗口停在旧主题上直到重启。
图标蒙版开关（Tint）同属主题组、同为全局配置，一并联动。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestThemeAppliesToAllWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _make_windows(self, n=2):
        import main_window
        wins = []
        for i in range(n):
            w = main_window.MainWindow()
            w.setWindowTitle(f'THEME_ALL_{i}')
            w.show()
            wins.append(w)
        self.app.processEvents()
        return wins

    def _teardown(self, wins):
        from PyQt6.QtCore import QEvent
        for w in wins:
            w.close()
            w.deleteLater()
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_theme_combo_change_updates_other_windows(self):
        wins = self._make_windows(2)
        try:
            a, b = wins
            keys = [a.theme_combo.itemData(i) for i in range(a.theme_combo.count())]
            target = next(k for k in keys if k != a.current_theme)
            # 两窗口起始主题一致，且都不是目标主题
            b.theme_combo.blockSignals(True)
            b.theme_combo.setCurrentIndex(b.theme_combo.findData(a.current_theme))
            b.theme_combo.blockSignals(False)
            b.current_theme = a.current_theme
            self.assertNotEqual(b.current_theme, target)

            a.theme_combo.setCurrentIndex(a.theme_combo.findData(target))
            self.app.processEvents()

            self.assertEqual(a.current_theme, target)
            self.assertEqual(b.current_theme, target,
                             "换主题只作用于当前窗口，其他窗口没跟上")
            self.assertEqual(b.theme_combo.currentData(), target,
                             "其他窗口的主题下拉框没有对齐")
            # 样式表确实按目标主题重写了（拿最外层背景色做证据）
            bg = a.THEMES[target]['bg_darkest']
            self.assertIn(bg, b.styleSheet())
        finally:
            self._teardown(wins)

    def test_icon_tint_change_updates_other_windows(self):
        wins = self._make_windows(2)
        try:
            a, b = wins
            start = a._use_icon_tint
            b._use_icon_tint = start
            b.icon_tint_checkbox.blockSignals(True)
            b.icon_tint_checkbox.setChecked(start)
            b.icon_tint_checkbox.blockSignals(False)

            a.icon_tint_checkbox.setChecked(not start)
            self.app.processEvents()

            self.assertEqual(a._use_icon_tint, not start)
            self.assertEqual(b._use_icon_tint, not start,
                             "Tint 开关只作用于当前窗口，其他窗口没跟上")
            self.assertEqual(b.icon_tint_checkbox.isChecked(), not start)
        finally:
            self._teardown(wins)


if __name__ == '__main__':
    unittest.main()
