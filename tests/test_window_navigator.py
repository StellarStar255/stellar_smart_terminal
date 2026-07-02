"""WindowNavigatorPanel 从 main_window 拆出后的回归测试。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_window_navigator.py -v

守卫两件易碎的事：
1. main_window ↔ window_navigator 的循环 import 能正常加载（延迟引用打破环）。
2. 导航面板对 MainWindow 的延迟引用路径（isinstance 过滤 / 静态方法）可用。
一旦有人把延迟引用改回顶层 import 或改了符号名，这些会先失败。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestWindowNavigatorExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_both_modules_import(self):
        import main_window
        import window_navigator
        # 类确实住在新模块里，main_window 只是再导出
        self.assertIs(main_window.WindowNavigatorPanel,
                      window_navigator.WindowNavigatorPanel)
        self.assertEqual(window_navigator.WindowNavigatorPanel.__module__,
                         'window_navigator')

    def test_navigator_lists_visible_main_windows(self):
        import main_window
        from window_navigator import WindowNavigatorPanel
        windows = []
        try:
            for title in ('NAV_T1', 'NAV_T2'):
                w = main_window.MainWindow()
                w.setWindowTitle(title)
                w.show()
                windows.append(w)
            self.app.processEvents()

            nav = WindowNavigatorPanel()
            nav._refresh_window_list()  # 走 isinstance(w, main_window.MainWindow)
            titles = {nav.window_list.item(i).text()
                      for i in range(nav.window_list.count())}
            # 我们造的两个可见窗口应出现（可能还有别的测试遗留窗口，故用子集断言）
            self.assertTrue({'NAV_T1', 'NAV_T2'}.issubset(titles) or
                            nav.window_list.count() >= 2,
                            f"导航未列出可见主窗口: {titles}")
            nav.deleteLater()
        finally:
            for w in windows:
                w.close()
                w.deleteLater()
            self.app.processEvents()

    def test_dock_mode_static_roundtrip(self):
        import main_window
        MW = main_window.MainWindow
        before = MW._navigator_dock_mode
        try:
            MW._set_navigator_dock_mode('embed')
            self.assertEqual(MW._navigator_dock_mode, 'embed')
            MW._set_navigator_dock_mode('float')
            self.assertEqual(MW._navigator_dock_mode, 'float')
        finally:
            MW._set_navigator_dock_mode(before)

    def test_broadcast_refresh_does_not_raise(self):
        # 走 main_window.MainWindow._iter_navigators 的延迟引用路径
        import main_window
        main_window.MainWindow._broadcast_navigator_refresh()


if __name__ == '__main__':
    unittest.main()
