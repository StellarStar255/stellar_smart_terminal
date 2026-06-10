# -*- coding: utf-8 -*-
"""explorer_clipboard.py 的 characterization 测试

覆盖：
- _split_basename / next_free_name 的命名冲突自增后缀逻辑（纯函数）
- set_items / effective_items / has_items / has_pastable / describe / clear
  的内部剪贴板 + 系统剪贴板桥接语义（用 offscreen Qt 的内存剪贴板）

预期值均为当前实现实际行为的固化（characterization）。
"""

import os
# 必须在 import PyQt6 之前设置
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QUrl, QMimeData

import explorer_clipboard as ec


def make_exists(names):
    s = set(names)
    return lambda n: n in s


class TestSplitBasename(unittest.TestCase):
    """'foo.tar.gz' 风格的文件名拆分（仅按最后一个点）"""

    def test_double_extension_splits_at_last_dot(self):
        self.assertEqual(ec._split_basename("foo.tar.gz"), ("foo.tar", ".gz"))

    def test_hidden_file_not_split(self):
        self.assertEqual(ec._split_basename(".bashrc"), (".bashrc", ""))

    def test_hidden_file_with_extension_is_split(self):
        # '.tar.gz' 有两个点，不走隐藏文件分支 → 按最后一个点拆
        self.assertEqual(ec._split_basename(".tar.gz"), (".tar", ".gz"))

    def test_no_extension(self):
        self.assertEqual(ec._split_basename("Makefile"), ("Makefile", ""))

    def test_trailing_dot(self):
        self.assertEqual(ec._split_basename("foo."), ("foo", "."))

    def test_multiple_dots(self):
        self.assertEqual(ec._split_basename("a.b.c"), ("a.b", ".c"))


class TestNextFreeName(unittest.TestCase):
    """粘贴命名冲突时的 '(N)' 自增后缀"""

    def test_no_conflict_returns_as_is(self):
        self.assertEqual(ec.next_free_name("foo.png", make_exists([])), "foo.png")

    def test_first_conflict_appends_1(self):
        self.assertEqual(
            ec.next_free_name("foo.png", make_exists(["foo.png"])),
            "foo (1).png",
        )

    def test_skips_taken_suffixes(self):
        self.assertEqual(
            ec.next_free_name("foo.png", make_exists(["foo.png", "foo (1).png"])),
            "foo (2).png",
        )

    def test_existing_suffix_increments_instead_of_stacking(self):
        # 'foo (3).png' → 'foo (4).png'，而不是 'foo (3) (1).png'
        self.assertEqual(
            ec.next_free_name("foo (3).png", make_exists(["foo (3).png"])),
            "foo (4).png",
        )

    def test_double_extension_inserts_before_last_ext(self):
        # 按最后一个点拆分 → 序号插在 .gz 前（与 Finder/VS Code 一致）
        self.assertEqual(
            ec.next_free_name("archive.tar.gz", make_exists(["archive.tar.gz"])),
            "archive.tar (1).gz",
        )

    def test_hidden_file(self):
        self.assertEqual(
            ec.next_free_name(".bashrc", make_exists([".bashrc"])),
            ".bashrc (1)",
        )

    def test_no_extension(self):
        self.assertEqual(
            ec.next_free_name("Makefile", make_exists(["Makefile"])),
            "Makefile (1)",
        )

    def test_nested_suffix_only_last_one_increments(self):
        # 'foo (1) (1).py' → base 是 'foo (1)'，从 (2) 继续
        self.assertEqual(
            ec.next_free_name("foo (1) (1).py", make_exists(["foo (1) (1).py"])),
            "foo (1) (2).py",
        )

    def test_fallback_timestamp_when_everything_taken(self):
        # exists_fn 恒为 True → 兜底返回时间戳名字（不死循环）
        name = ec.next_free_name("x.txt", lambda n: True)
        self.assertRegex(name, r"^x-\d+\.txt$")


class TestClipboardBridging(unittest.TestCase):
    """内部剪贴板与系统剪贴板的"谁更新"仲裁逻辑"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 重置模块全局状态和系统剪贴板
        self.app.clipboard().clear()
        ec.clear()

    def tearDown(self):
        self.app.clipboard().clear()
        ec.clear()

    @staticmethod
    def _set_system_urls(paths):
        """模拟外部应用（Finder 等）往系统剪贴板写文件 URL"""
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        QApplication.clipboard().setMimeData(mime)

    def test_internal_items_returned_when_system_unchanged(self):
        items = [("local", "/tmp/a.txt"), ("remote", "host", "/srv/b.txt", None)]
        ec.set_items(items)
        self.assertEqual(ec.effective_items(), items)
        self.assertTrue(ec.has_items())
        self.assertTrue(ec.has_pastable())

    def test_external_update_overrides_internal(self):
        ec.set_items([("local", "/tmp/a.txt")])
        self._set_system_urls(["/tmp/external.png"])
        self.assertEqual(ec.effective_items(), [("local", "/tmp/external.png")])

    def test_push_local_paths_snapshot_keeps_internal_preferred(self):
        # set_items 自己往系统剪贴板写 URL → 快照一致 → 仍用内部 items
        ec.set_items([("local", "/tmp/mine.txt")], push_local_paths=["/tmp/mine.txt"])
        self.assertEqual(ec.effective_items(), [("local", "/tmp/mine.txt")])

    def test_clear_empties_everything(self):
        ec.set_items([("local", "/tmp/a.txt")])
        ec.clear()
        self.assertEqual(ec.effective_items(), [])
        self.assertFalse(ec.has_items())
        self.assertFalse(ec.has_pastable())

    def test_describe_single_local(self):
        ec.set_items([("local", "/tmp/a.txt")])
        self.assertEqual(ec.describe(), "a.txt")

    def test_describe_multiple_shows_more_count(self):
        ec.set_items([
            ("local", "/tmp/a.txt"),
            ("remote", "host", "/srv/b.txt", None),
            ("local", "/tmp/c.txt"),
        ])
        self.assertEqual(ec.describe(), "a.txt (+2 more)")

    def test_describe_remote_uses_remote_path(self):
        ec.set_items([("remote", "myhost", "/srv/data/report.pdf", None)])
        self.assertEqual(ec.describe(), "report.pdf")

    def test_describe_strips_trailing_slash(self):
        ec.set_items([("local", "/tmp/dir/")])
        self.assertEqual(ec.describe(), "dir")

    def test_describe_empty(self):
        self.assertEqual(ec.describe(), "")

    def test_set_items_none_treated_as_empty(self):
        ec.set_items(None)
        self.assertEqual(ec.effective_items(), [])
        self.assertFalse(ec.has_items())


if __name__ == "__main__":
    unittest.main()
