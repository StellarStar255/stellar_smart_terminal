# -*- coding: utf-8 -*-
"""toolbar_manager.migrate_toolbar_order 的单元测试

老配置升级后 remote/images 可能被放到离功能邻居很远的位置；
迁移应把 remote 挪到 git 紧后、images 挪到 clear 紧后（跟随锚点所在分组），
且只做一次（order_version 门控），不覆盖用户之后的显式摆放。
不需要 QApplication。
"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from toolbar_manager import (
    TOOLBAR_ORDER_VERSION, ToolbarManagerDialog, migrate_toolbar_order,
)


class TestDefaults(unittest.TestCase):
    def test_definitions_images_after_clear(self):
        names = [n for n, _, _, g in ToolbarManagerDialog.BUTTON_DEFINITIONS
                 if g == "操作"]
        self.assertEqual(
            names, ["export_btn", "history_btn", "clear_btn", "images_btn"])

    def test_definitions_remote_after_git(self):
        names = [n for n, _, _, g in ToolbarManagerDialog.BUTTON_DEFINITIONS
                 if g == "面板"]
        self.assertEqual(
            names,
            ["explorer_toggle_btn", "git_toggle_btn", "remote_toggle_btn"])


class TestMigrate(unittest.TestCase):
    def test_non_dict_ignored(self):
        self.assertFalse(migrate_toolbar_order(None))
        self.assertFalse(migrate_toolbar_order([]))

    def test_stamps_version_on_minimal_config(self):
        cfg = {"layout": "single"}
        self.assertTrue(migrate_toolbar_order(cfg))
        self.assertEqual(cfg["order_version"], TOOLBAR_ORDER_VERSION)

    def test_already_migrated_untouched(self):
        cfg = {
            "order_version": TOOLBAR_ORDER_VERSION,
            "button_order": {"面板与编辑器": ["vscode_open_btn", "remote_toggle_btn"]},
        }
        snapshot = copy.deepcopy(cfg)
        self.assertFalse(migrate_toolbar_order(cfg))
        self.assertEqual(cfg, snapshot)

    def test_repositions_into_anchor_override_group(self):
        # 真实场景：explorer/git 被用户跨组挪进「分屏管理」，remote 残留在
        # 旧版「面板与编辑器」组尾，images 在「操作」组内 clear 之前
        cfg = {
            "button_order": {
                "分屏管理": ["explorer_toggle_btn", "git_toggle_btn",
                             "split_btn", "split_v_btn"],
                "面板与编辑器": ["vscode_open_btn", "cursor_open_btn",
                                 "log_toggle_btn", "remote_toggle_btn"],
                "操作": ["export_btn", "history_btn", "images_btn", "clear_btn"],
            },
            "button_groups": {
                "explorer_toggle_btn": "分屏管理",
                "git_toggle_btn": "分屏管理",
            },
        }
        self.assertTrue(migrate_toolbar_order(cfg))
        # remote 跟随 git：插到「分屏管理」里 git 紧后，并写跨组覆盖
        self.assertEqual(
            cfg["button_order"]["分屏管理"],
            ["explorer_toggle_btn", "git_toggle_btn", "remote_toggle_btn",
             "split_btn", "split_v_btn"])
        self.assertNotIn("remote_toggle_btn", cfg["button_order"]["面板与编辑器"])
        self.assertEqual(cfg["button_groups"]["remote_toggle_btn"], "分屏管理")
        # images 挪到 clear 紧后；「操作」是它的默认组，不需要跨组覆盖
        self.assertEqual(
            cfg["button_order"]["操作"],
            ["export_btn", "history_btn", "clear_btn", "images_btn"])
        self.assertNotIn("images_btn", cfg["button_groups"])

    def test_anchor_in_default_group_without_saved_list(self):
        # git/clear 都没被挪过、组内顺序也没存过 → 默认顺序已正确，
        # 迁移只需清掉可能残留的跨组覆盖并盖版本号
        cfg = {
            "button_order": {},
            "button_groups": {"remote_toggle_btn": "面板与编辑器"},
        }
        self.assertTrue(migrate_toolbar_order(cfg))
        self.assertNotIn("remote_toggle_btn", cfg["button_groups"])
        self.assertEqual(cfg["order_version"], TOOLBAR_ORDER_VERSION)

    def test_idempotent_second_call_noop(self):
        cfg = {
            "button_order": {
                "操作": ["export_btn", "history_btn", "images_btn", "clear_btn"],
            },
        }
        self.assertTrue(migrate_toolbar_order(cfg))
        snapshot = copy.deepcopy(cfg)
        self.assertFalse(migrate_toolbar_order(cfg))
        self.assertEqual(cfg, snapshot)


if __name__ == "__main__":
    unittest.main()
