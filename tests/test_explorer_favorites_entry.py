# -*- coding: utf-8 -*-
"""快捷方式（收藏）入口的回归测试。

用户反馈：面包屑路径栏上没法把路径加成快捷方式。此前收藏只能在文件树里
右键**子项**添加：当前所在目录本身不是树里的一项，面包屑各段也没有右键
菜单，于是"我现在就在这个目录、想把它收藏"没有任何入口。

修复后的三个入口：
1. 面包屑任一段右键 → 添加/移除快捷方式、复制路径；
2. 文件树空白处右键 → 把当前目录添加到快捷方式；
3. Explorer 头部 ★ 下拉菜单顶部 → 把当前目录添加到快捷方式。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_favorites_entry.py -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtWidgets import QApplication, QToolButton


def _action(menu, text):
    for act in menu.actions():
        if act.text() == text:
            return act
    return None


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import explorer_favorites
        import explorer_widget
        from i18n import t
        cls.fav = explorer_favorites
        cls.ew = explorer_widget
        cls.t = staticmethod(t)

    def setUp(self):
        self.fav.clear()
        self.tmp = tempfile.mkdtemp(prefix="fav_entry_")
        self.tmp = os.path.realpath(self.tmp)

    def tearDown(self):
        self.fav.clear()

    def _drain(self):
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()


class TestBreadcrumbContextMenu(_Base):
    def setUp(self):
        super().setUp()
        self.panel = self.ew.ExplorerPanel()
        self.panel.resize(600, 400)
        self.panel.set_root_path(self.tmp)
        self.app.processEvents()

    def tearDown(self):
        self.panel.deleteLater()
        self._drain()
        super().tearDown()

    def test_segment_right_click_emits_its_path(self):
        """右键面包屑的当前目录段 → 发出该段的完整路径"""
        got = []
        # 面板自己的处理函数会 menu.exec 阻塞，测试里换成记录器
        self.panel.breadcrumb.segment_context_requested.disconnect(
            self.panel._show_path_context_menu)
        self.panel.breadcrumb.segment_context_requested.connect(
            lambda p, pos: got.append(p))
        name = os.path.basename(self.tmp)
        btns = [b for b in self.panel.breadcrumb.findChildren(QToolButton)
                if b.text() == name]
        self.assertTrue(btns, "面包屑里找不到当前目录那一段")
        btns[0].customContextMenuRequested.emit(QPoint(1, 1))
        self.assertEqual(got, [self.tmp])

    def test_path_menu_toggles_favorite(self):
        """路径菜单：未收藏时显示"添加"，点了就落盘；已收藏时显示"移除" """
        menu = self.panel._build_path_context_menu(self.tmp)
        add = _action(menu, self.t("explorer.favorite_add"))
        self.assertIsNotNone(add, "路径菜单缺少「添加到快捷方式」")
        self.assertIsNone(_action(menu, self.t("explorer.favorite_remove")))
        add.trigger()
        self.assertTrue(self.fav.is_favorite(self.tmp))

        menu2 = self.panel._build_path_context_menu(self.tmp)
        rm = _action(menu2, self.t("explorer.favorite_remove"))
        self.assertIsNotNone(rm, "已收藏时应显示「从快捷方式移除」")
        rm.trigger()
        self.assertFalse(self.fav.is_favorite(self.tmp))

    def test_path_menu_has_copy_path(self):
        menu = self.panel._build_path_context_menu(self.tmp)
        self.assertIsNotNone(_action(menu, self.t("explorer.copy_path")))

    def test_blank_area_menu_offers_current_folder(self):
        """文件树空白处右键 → 能把当前目录添加到快捷方式"""
        menu = self.panel._build_context_menu(QPoint(-1, -1))
        label = self.t("explorer.favorite_add_current",
                       name=os.path.basename(self.tmp))
        act = _action(menu, label)
        self.assertIsNotNone(act, "空白处右键菜单缺少「把当前目录添加到快捷方式」")
        act.trigger()
        self.assertTrue(self.fav.is_favorite(self.tmp))


class TestStarMenuOffersCurrentFolder(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import main_window
        cls.win = main_window.MainWindow()

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.win.deleteLater()
        del cls.win
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def test_star_menu_first_item_adds_current_folder(self):
        self.win.explorer_panel.set_root_path(self.tmp)
        self.app.processEvents()
        menu = self.win._build_explorer_favorites_menu()
        label = self.t("explorer.favorite_add_current",
                       name=os.path.basename(self.tmp))
        first = menu.actions()[0]
        self.assertEqual(first.text(), label, "★ 菜单顶部应是「把当前目录添加到快捷方式」")
        first.trigger()
        self.assertTrue(self.fav.is_favorite(self.tmp))

        menu2 = self.win._build_explorer_favorites_menu()
        label_rm = self.t("explorer.favorite_remove_current",
                          name=os.path.basename(self.tmp))
        self.assertEqual(menu2.actions()[0].text(), label_rm)
        # 已收藏的当前目录也出现在列表里
        texts = [a.text() for a in menu2.actions()]
        self.assertIn(os.path.basename(self.tmp) + os.sep, texts)


if __name__ == '__main__':
    unittest.main()
