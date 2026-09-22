"""换主题后，所有窗口里"只靠调色板着色"的控件（菜单栏等）都必须换到新主题。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_theme_menubar_palette_all_windows.py -v

回归（Ubuntu/Wayland 双窗口，2026-09）：在窗口 A 把主题从"粉红"切到"浅色"，
窗口 B 的菜单栏仍是粉红的深紫底。根因是 _apply_theme 的顺序：先重设各控件
样式表、最后才改应用级 QPalette。被样式表接管的控件（WA_StyleSheet）不吃
父级/应用级调色板传播，只在重新 polish 时按"当时的"应用调色板取色；
_apply_theme_to_all_windows 逐窗口套用时，先处理的窗口在旧调色板下 polish
完毕，随后才换应用调色板，于是那个窗口的菜单栏永远停在旧色上。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMenuBarPaletteFollowsThemeInAllWindows(unittest.TestCase):
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
            w.setWindowTitle(f'THEME_PAL_{i}')
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

    @staticmethod
    def _menubar_window_color(win) -> str:
        from PyQt6.QtGui import QPalette
        return win.menuBar().palette().color(
            QPalette.ColorGroup.Active, QPalette.ColorRole.Window).name()

    def _switch_and_check(self, src_key: str, dst_key: str):
        wins = self._make_windows(2)
        try:
            a, b = wins
            # 两窗口先统一到源主题（走与用户操作相同的联动路径）
            a.theme_combo.setCurrentIndex(a.theme_combo.findData(src_key))
            self.app.processEvents()
            src_bg = a.THEMES[src_key]['bg_dark']
            for w in wins:
                self.assertEqual(self._menubar_window_color(w), src_bg)

            # 在 A 上切到目标主题：B 的菜单栏调色板必须同步换过去
            a.theme_combo.setCurrentIndex(a.theme_combo.findData(dst_key))
            self.app.processEvents()
            dst_bg = a.THEMES[dst_key]['bg_dark']
            self.assertEqual(self._menubar_window_color(a), dst_bg)
            self.assertEqual(
                self._menubar_window_color(b), dst_bg,
                f"另一窗口菜单栏仍停在旧主题调色板 {src_key}→{dst_key}")
        finally:
            self._teardown(wins)

    def test_dark_to_light(self):
        self._switch_and_check('粉红', '浅色')

    def test_light_to_dark(self):
        self._switch_and_check('浅色', '深蓝')


if __name__ == '__main__':
    unittest.main()
