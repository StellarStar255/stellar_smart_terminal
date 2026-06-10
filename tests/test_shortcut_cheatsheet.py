# -*- coding: utf-8 -*-
"""快捷键速查表对话框的测试（offscreen）

覆盖 dialogs.ShortcutCheatSheetDialog：
- 分组/行渲染数量正确；
- 搜索过滤：命中描述或键位、空格分词 AND、清空恢复、整组隐藏；
- customize_requested 信号；
- 速查表用到的 i18n 键在翻译表里都存在（防止改键漏文案）。

运行方式：
    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_shortcut_cheatsheet -v
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from dialogs import ShortcutCheatSheetDialog
from i18n import t, TRANSLATIONS

GROUPS = [
    ("Global", [("⌘K", "Focus the command search box"),
                ("⌘T", "New Tab")]),
    ("Terminal", [("⇧End", "Jump to bottom of history"),
                  ("⌘C", "Copy selection")]),
]


class CheatSheetBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dlg = ShortcutCheatSheetDialog(GROUPS)

    def tearDown(self):
        self.dlg.close()
        self.dlg.deleteLater()

    def visible_rows(self):
        rows = []
        for gi in range(self.dlg.tree.topLevelItemCount()):
            g = self.dlg.tree.topLevelItem(gi)
            if g.isHidden():
                continue
            for ci in range(g.childCount()):
                c = g.child(ci)
                if not c.isHidden():
                    rows.append((c.text(0), c.text(1)))
        return rows


class TestRendering(CheatSheetBase):
    def test_groups_and_rows_rendered(self):
        self.assertEqual(self.dlg.tree.topLevelItemCount(), 2)
        self.assertEqual(len(self.visible_rows()), 4)

    def test_row_columns(self):
        desc, keys = self.visible_rows()[0][0], self.visible_rows()[0][1]
        self.assertEqual((keys, desc), ("⌘K", "Focus the command search box"))


class TestFiltering(CheatSheetBase):
    def test_filter_matches_description(self):
        self.dlg.search_input.setText("copy")
        self.assertEqual(self.visible_rows(), [("Copy selection", "⌘C")])

    def test_filter_matches_keys(self):
        self.dlg.search_input.setText("⌘k")
        self.assertEqual(self.visible_rows(),
                         [("Focus the command search box", "⌘K")])

    def test_multi_token_and_semantics(self):
        self.dlg.search_input.setText("jump history")
        self.assertEqual(self.visible_rows(),
                         [("Jump to bottom of history", "⇧End")])

    def test_group_hidden_when_all_children_filtered(self):
        self.dlg.search_input.setText("copy")
        self.assertTrue(self.dlg.tree.topLevelItem(0).isHidden())
        self.assertFalse(self.dlg.tree.topLevelItem(1).isHidden())

    def test_clear_restores_all(self):
        self.dlg.search_input.setText("copy")
        self.dlg.search_input.setText("")
        self.assertEqual(len(self.visible_rows()), 4)

    def test_no_match_hides_everything(self):
        self.dlg.search_input.setText("nonexistent-zzz")
        self.assertEqual(self.visible_rows(), [])


class TestSignals(CheatSheetBase):
    def test_customize_requested_emitted(self):
        fired = []
        self.dlg.customize_requested.connect(lambda: fired.append(True))
        from PyQt6.QtWidgets import QPushButton
        buttons = self.dlg.findChildren(QPushButton)
        customize = [b for b in buttons if b.text() == t("shortcuts.cheatsheet_customize")]
        self.assertEqual(len(customize), 1)
        customize[0].click()
        self.assertEqual(fired, [True])


class TestI18nCoverage(unittest.TestCase):
    """速查表静态条目用到的 i18n 键必须存在（改 keyPressEvent 键位时同步文案的约束）"""

    KEYS = [
        "shortcuts.cheatsheet_title", "shortcuts.cheatsheet_search",
        "shortcuts.cheatsheet_customize", "shortcuts.cheatsheet_close",
        "shortcuts.cheatsheet_menu_item", "shortcuts.act.cheatsheet",
        "shortcuts.group.global", "shortcuts.group.terminal", "shortcuts.group.editor",
        "shortcuts.sc.cmd_search",
        "shortcuts.sc.term_copy", "shortcuts.sc.term_interrupt",
        "shortcuts.sc.term_paste", "shortcuts.sc.term_select_all",
        "shortcuts.sc.term_page", "shortcuts.sc.term_home_end",
        "shortcuts.sc.term_jump", "shortcuts.sc.term_line_ends",
        "shortcuts.sc.term_close_search",
        "shortcuts.sc.edit_save", "shortcuts.sc.edit_ai_accept",
        "shortcuts.sc.edit_ai_dismiss", "shortcuts.sc.edit_ai_trigger",
    ]

    def test_all_cheatsheet_keys_translated(self):
        for key in self.KEYS:
            with self.subTest(key=key):
                self.assertIn(key, TRANSLATIONS, f"i18n 缺少 {key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
