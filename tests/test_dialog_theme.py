# -*- coding: utf-8 -*-
"""对话框 / 消息框 / 弹出菜单 / 自绘控件跟随主题的回归测试。

审查发现：dialogs.py 五个对话框的 QSS 全部写死深色（91 处 hex、不收 theme），
消息框反向写死浅色 #f0f0f0，弹出菜单每次 popup 重建一段写死的 QSS，
widgets.py 里下拉箭头 / 导航分隔条 / 分离窗口的颜色也写死——切到「浅色」主题
时这些地方仍是深色块，深色主题下消息框又是一块白。

修复：对话框接受 theme 字典（缺省回退到深蓝色板，老调用方不变），消息框与
菜单 QSS 由主题推导并缓存，自绘控件提供颜色 setter 并在 _apply_theme 里下发。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_dialog_theme.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

import themes

LIGHT = themes.THEMES["浅色"]
DARK = themes.THEMES["深蓝"]
LIGHT_BG = LIGHT["bg_dark"]          # 对话框底色
DARK_BG = DARK["bg_dark"]            # 以前写死的 #1a1a2e

SPECS = [("history", "Ctrl+Shift+H", "shortcuts.act.history", "_show_history")]
GROUPS = [("Global", [("⌘K", "Focus the command search box")])]


def _dialogs(theme):
    from dialogs import (PresetDialog, LLMConfigDialog, DirectoryHistoryDialog,
                         ShortcutSettingsDialog, ShortcutCheatSheetDialog)
    kw = {"theme": theme} if theme is not None else {}
    return [
        PresetDialog([], **kw),
        LLMConfigDialog([], **kw),
        DirectoryHistoryDialog([], **kw),
        ShortcutSettingsDialog(SPECS, {}, **kw),
        ShortcutCheatSheetDialog(GROUPS, **kw),
    ]


class TestDialogsFollowTheme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_light_theme_yields_light_stylesheet(self):
        for dlg in _dialogs(LIGHT):
            qss = dlg.styleSheet()
            self.assertIn(LIGHT_BG, qss, f"{type(dlg).__name__} 未使用浅色主题底色")
            self.assertNotIn(DARK_BG, qss, f"{type(dlg).__name__} 仍含写死的深色底")
            self.assertIn(LIGHT["text"], qss, f"{type(dlg).__name__} 文字色未跟随主题")
            dlg.deleteLater()

    def test_no_theme_keeps_dark_default(self):
        for dlg in _dialogs(None):
            self.assertIn(DARK_BG, dlg.styleSheet(), f"{type(dlg).__name__} 缺省不再是深色")
            dlg.deleteLater()


class TestMessageBoxAndMenusFollowTheme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.win = main_window.MainWindow()

    @classmethod
    def tearDownClass(cls):
        # _apply_theme 会改 app 级样式表/调色板，影响同进程里后续测试的窗口
        # 几何（spring 测试卡在 1px 边界上）；退场前切回默认主题
        try:
            cls.win.current_theme = "午夜黑"
            cls.win._apply_theme("午夜黑")
        except Exception:
            pass
        QApplication.sendEvent(cls.win, QCloseEvent())
        cls.win.deleteLater()
        del cls.win
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def _box_qss(self, theme_name):
        # 与 _on_theme_changed 的真实流程一致：先记名字再应用
        self.win.current_theme = theme_name
        self.win._apply_theme(theme_name)
        box = self.win._make_styled_message_box(QMessageBox.Icon.Information, "t", "x")
        qss = box.styleSheet()
        box.deleteLater()
        return qss

    def test_message_box_dark_theme_is_dark(self):
        qss = self._box_qss("深蓝")
        self.assertNotIn("#f0f0f0", qss, "深色主题下消息框仍是写死的浅色")
        self.assertIn(DARK["bg_dark"], qss)
        self.assertIn(DARK["text"], qss)

    def test_message_box_light_theme_is_light(self):
        qss = self._box_qss("浅色")
        self.assertIn(LIGHT["bg_dark"], qss)
        self.assertIn(LIGHT["text"], qss)
        self.assertNotIn(DARK["bg_dark"], qss)

    def test_menu_qss_helper_follows_theme_and_caches(self):
        from main_window_theme import menu_qss
        light = menu_qss(LIGHT)
        dark = menu_qss(DARK)
        self.assertIn(LIGHT["bg_light"], light)
        self.assertIn(LIGHT["text"], light)
        self.assertNotIn(DARK["bg_light"], light)
        self.assertIn(DARK["bg_light"], dark)
        self.assertIs(menu_qss(LIGHT), light, "同一主题应命中缓存")

    def test_widgets_receive_theme_colours(self):
        from widgets import CenteredComboBox
        self.win.current_theme = "浅色"
        self.win._apply_theme("浅色")
        combos = self.win.findChildren(CenteredComboBox)
        self.assertTrue(combos)
        for c in combos:
            self.assertEqual(c.arrow_color().lower(), LIGHT["text"].lower())
        handle = self.win.nav_resize_handle
        self.assertEqual(handle._IDLE.lower(), LIGHT["border"].lower())
        self.assertEqual(handle._ACTIVE.lower(), LIGHT["accent"].lower())


if __name__ == '__main__':
    unittest.main()
