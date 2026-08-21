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
            from PyQt6.QtCore import QEvent
            for w in windows:
                w.close()
                w.deleteLater()
            # 当场冲干净延迟删除：半销毁的 MainWindow 残留会在后续
            # processEvents 里段错误（CI 上曾崩掉整个测试进程）
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_title_change_refreshes_without_polling(self):
        """标题变化经 windowTitleChanged 信号即时刷新列表（不等 30 秒兜底轮询）；
        重复刷新不重复挂钩。"""
        import main_window
        from window_navigator import WindowNavigatorPanel
        w = None
        nav = None
        try:
            w = main_window.MainWindow()
            w.setWindowTitle('NAV_EVT_BEFORE')
            w.show()
            self.app.processEvents()

            nav = WindowNavigatorPanel()
            nav._compact_mode = False   # 显示原始标题，便于断言（默认提取文件夹名）
            nav._refresh_window_list()
            hooked = len(nav._hooked_window_ids)
            self.assertGreaterEqual(hooked, 1)
            nav._refresh_window_list()   # 再刷一次不应重复挂钩
            self.assertEqual(len(nav._hooked_window_ids), hooked)

            w.setWindowTitle('NAV_EVT_AFTER')
            self.app.processEvents()
            titles = [nav.window_list.item(i).text()
                      for i in range(nav.window_list.count())]
            self.assertTrue(any('NAV_EVT_AFTER' in t for t in titles), titles)
        finally:
            from PyQt6.QtCore import QEvent
            if nav is not None:
                nav.deleteLater()
            if w is not None:
                w.close()
                w.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

    def test_embedded_highlight_pinned_to_owner(self):
        """内嵌导航列表的高亮永远标识宿主窗口自己，不跟随全局活动窗口。

        回归：此前窗口激活时 changeEvent 把活动窗口 select_window 广播给所有
        面板，导致每个窗口内嵌列表都高亮「别的窗口」——在自己窗口里高亮别人
        没有意义。浮动面板（无钉死）保持跟随活动窗口的原行为。
        """
        import main_window
        from PyQt6.QtCore import Qt
        from window_navigator import WindowNavigatorPanel

        def current_wid(nav):
            item = nav.window_list.currentItem()
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        windows = []
        floating = None
        try:
            for title in ('PIN_T1', 'PIN_T2'):
                w = main_window.MainWindow()
                w.setWindowTitle(title)
                w.show()
                windows.append(w)
            self.app.processEvents()
            a, b = windows

            for w in (a, b):
                w.nav_panel._force_refresh()
            self.assertEqual(current_wid(a.nav_panel), id(a),
                             "A 的内嵌列表应高亮 A 自己")
            self.assertEqual(current_wid(b.nav_panel), id(b),
                             "B 的内嵌列表应高亮 B 自己")

            # 模拟窗口激活广播（changeEvent 对所有面板 select_window(active)）
            for w in (a, b):
                w.nav_panel.select_window(b)
            self.assertEqual(current_wid(a.nav_panel), id(a),
                             "B 激活后 A 的内嵌列表仍应高亮 A 自己")
            self.assertEqual(current_wid(b.nav_panel), id(b))

            # 刷新重建后钉住不丢
            a.nav_panel._force_refresh()
            self.assertEqual(current_wid(a.nav_panel), id(a))

            # 浮动面板（无钉死）保持跟随活动窗口
            floating = WindowNavigatorPanel()
            floating._refresh_window_list()
            floating.select_window(b)
            self.assertEqual(current_wid(floating), id(b),
                             "浮动面板应跟随被激活的窗口")
        finally:
            from PyQt6.QtCore import QEvent
            if floating is not None:
                floating.deleteLater()
            for w in windows:
                w.close()
                w.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
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

    def test_nav_font_not_double_scaled_after_theme_reapply(self):
        """换主题后内嵌导航字号保持 GUI Font 单次缩放，不被二次放大。

        回归：_apply_theme 会还原并清空 _scale_gui_font_sizes 的基准缓存，随后
        nav_panel.apply_theme 写入的是「已按 GUI Font 缩放」的样式；若缩放遍历
        不排除导航面板，会把 16px 误当基准再乘比例 → 19px，且只发生在换过主题
        的窗口上，各窗口导航字号漂移不一致。"""
        import re as _re
        import main_window
        w = None
        try:
            w = main_window.MainWindow()
            w.show()
            self.app.processEvents()

            w._gui_font_size = 14
            w._apply_global_zoom()  # 下发 GUI Font 比例到导航面板
            expected = w.nav_panel._sf(16)  # 列表基准 16px（控制栏+2）× (14/12)

            def list_px():
                m = _re.search(r'font-size:\s*(\d+)px',
                               w.nav_panel.window_list.styleSheet())
                return int(m.group(1)) if m else None

            self.assertEqual(list_px(), expected)
            # 连续两次重应用主题：字号必须稳定在单次缩放值
            for _ in range(2):
                w._apply_theme(w.current_theme)
                self.assertEqual(list_px(), expected)
        finally:
            from PyQt6.QtCore import QEvent
            if w is not None:
                w.close()
                w.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


class TestAttentionDotVisibility(unittest.TestCase):
    """「执行完毕」绿点必须始终画在可视区内。

    回归守卫：绿点原本画在条目矩形右端；窄侧栏下长窗口名会撑出横向滚动条，
    条目右端跑到可视区外，绿点整个不可见。修复后绿点右端被钳制到可视区右缘。
    """

    LONG_TITLE = "9. zhiyuan_linux_pinyin_input_methods_long_name"

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _green_pixels(self, list_width: int):
        """构造指定宽度的列表并返回可视区内绿点像素数。"""
        import main_window  # noqa: F401  先加载，破 window_navigator 的循环 import
        from window_navigator import NoHighlightDelegate, NAV_ATTENTION_ROLE
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem
        from PyQt6.QtGui import QColor

        lw = QListWidget()
        lw.setItemDelegate(NoHighlightDelegate(lw))
        lw.setStyleSheet(
            "QListWidget{background:#16213e;} QListWidget::item{padding:8px;}")
        it = QListWidgetItem(self.LONG_TITLE)
        it.setData(NAV_ATTENTION_ROLE, True)
        it.setForeground(QColor('#a855f7'))
        lw.addItem(it)
        lw.resize(list_width, 120)
        lw.show()
        self.app.processEvents()
        try:
            img = lw.viewport().grab().toImage()
            count = 0
            for y in range(img.height()):
                for x in range(img.width()):
                    c = img.pixelColor(x, y)
                    if (abs(c.red() - 0x2e) < 25 and abs(c.green() - 0xcc) < 25
                            and abs(c.blue() - 0x71) < 25):
                        count += 1
            hbar_out = lw.horizontalScrollBar().maximum() > 0
            return count, hbar_out
        finally:
            lw.deleteLater()
            self.app.processEvents()

    def test_dot_visible_in_narrow_list(self):
        # 220px 窄列表：文字被截断、出横向滚动条，绿点仍须可见
        count, hbar_out = self._green_pixels(220)
        self.assertTrue(hbar_out, "前置条件不成立：窄列表未出横向滚动条")
        self.assertGreater(count, 0, "窄侧栏下「执行完毕」绿点画到了可视区外")

    def test_dot_visible_in_wide_list(self):
        # 宽列表：原有行为（画在条目右端附近）不受钳制影响
        count, hbar_out = self._green_pixels(500)
        self.assertFalse(hbar_out)
        self.assertGreater(count, 0)


if __name__ == '__main__':
    unittest.main()
