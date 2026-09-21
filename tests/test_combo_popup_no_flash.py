"""下拉框关闭时不做“选中项闪烁”（2026-09）。

现象：点开工具栏 Language / Theme / Preset 等下拉框，再点空白处关闭，列表消失前
蓝色高亮条灭一下、亮一下，然后才消失，看起来像坏了。

根因：macOS 样式的 SH_Menu_FlashTriggeredItem 为真，QComboBox::hidePopup 会先
“去掉选中 → 60ms → 重新选中 → 20ms → 真正隐藏”。我们的下拉是 QSS 定制的列表
弹窗，这个模仿原生菜单的闪烁只剩下副作用。

修法：suppress_popup_flash 给下拉框挂一个把该提示置 0 的 QProxyStyle。本测试在
macOS 上跑（cocoa/offscreen 皆可）会在修复前失败；其它平台样式本就不闪，退化为
“hidePopup 后列表立即隐藏且选区不动”的守卫。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication, QComboBox, QStyle  # noqa: E402

from widgets import CenteredComboBox, QuietPopupComboBox, suppress_popup_flash  # noqa: E402

FLASH = QStyle.StyleHint.SH_Menu_FlashTriggeredItem
# 与真实应用一致：应用级样式表会让 Qt 用 QStyleSheetStyle 包住控件样式，
# 提示必须穿透这层包装才算数。
APP_QSS = ("QComboBox { border: 1px solid #888; combobox-popup: 0; }"
           "QComboBox QAbstractItemView { selection-background-color: #667eea; }")


class ComboPopupNoFlashTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls._saved_qss = cls.app.styleSheet()
        cls.app.setStyleSheet(APP_QSS)

    @classmethod
    def tearDownClass(cls):
        cls.app.setStyleSheet(cls._saved_qss)

    def _make(self, cls):
        combo = cls()
        combo.addItem("中文", "zh")
        combo.addItem("English", "en")
        combo.setCurrentIndex(1)
        combo.resize(120, 28)
        combo.show()
        self.addCleanup(combo.deleteLater)
        self.app.processEvents()
        return combo

    def test_flash_hint_disabled_for_custom_combos(self):
        for cls in (CenteredComboBox, QuietPopupComboBox):
            combo = self._make(cls)
            with self.subTest(cls=cls.__name__):
                self.assertEqual(combo.style().styleHint(FLASH), 0)
                # QSS 仍然生效：样式对象是 QStyleSheetStyle 包着我们的代理
                self.assertEqual(combo.style().metaObject().className(), "QStyleSheetStyle")

    def test_helper_applies_to_plain_combo(self):
        combo = self._make(QComboBox)
        suppress_popup_flash(combo)
        self.assertEqual(combo.style().styleHint(FLASH), 0)

    def test_hide_popup_is_immediate_and_keeps_selection(self):
        """hidePopup 后列表立即隐藏，且过程中选区一次都不改（不再灭/亮）。"""
        combo = self._make(CenteredComboBox)
        combo.showPopup()
        self.app.processEvents()
        view = combo.view()
        container = view.parentWidget()
        self.assertTrue(container.isVisible(), "前置：弹窗应已显示")
        changes = []
        view.selectionModel().selectionChanged.connect(
            lambda *_: changes.append([i.row() for i in view.selectionModel().selectedIndexes()]))

        combo.hidePopup()
        self.assertFalse(container.isVisible(), "hidePopup 后列表应立即隐藏，而不是等闪烁做完")

        # 放事件循环跑 150ms（覆盖 60+20ms 的闪烁链），确认没有延迟的选区翻转
        loop_done = []
        QTimer.singleShot(150, lambda: loop_done.append(True))
        while not loop_done:
            self.app.processEvents()
        self.assertEqual(changes, [], f"关闭过程中选区不应变化，实际: {changes}")
        self.assertEqual(combo.currentIndex(), 1)


if __name__ == "__main__":
    unittest.main()
