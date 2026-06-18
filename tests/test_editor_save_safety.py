"""编辑器保存安全性测试（修复 #1 退出保存、#2 原子写）。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_editor_save_safety -v
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _area_with_file(self, content="original\n"):
        from file_editor import EditorArea
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.txt")
        with open(p, "w") as f:
            f.write(content)
        area = EditorArea(theme={})
        self.assertTrue(area.open_file_in_active(p))
        return area, area.active_pane, p


class TestAtomicSave(_Base):
    def test_save_writes_correct_content(self):
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("edited\n")
        self.assertTrue(pane.is_modified())
        self.assertTrue(pane.save_file())
        with open(p) as f:
            self.assertEqual(f.read(), "edited\n")
        self.assertFalse(pane.is_modified())

    def test_save_is_atomic_replace(self):
        area, pane, p = self._area_with_file()
        ino = os.stat(p).st_ino
        pane.editor.setPlainText("v2\n")
        self.assertTrue(pane.save_file())
        # os.replace 换 inode → 证明走的是临时文件+rename，而非原地 truncate
        self.assertNotEqual(os.stat(p).st_ino, ino)

    def test_failed_write_keeps_original_and_cleans_tmp(self):
        import file_editor
        area, pane, p = self._area_with_file("keepme\n")
        orig_bytes = open(p, "rb").read()
        pane.editor.setPlainText("should not land\n")

        # 让 os.replace 抛错，模拟写入最后一步失败
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated replace failure")

        # 静默 save 失败弹窗
        from PyQt6.QtWidgets import QMessageBox
        orig_warn = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        os.replace = boom
        try:
            self.assertFalse(pane.save_file())  # 失败返回 False
        finally:
            os.replace = real_replace
            QMessageBox.warning = orig_warn

        # 原文件原封不动；目录里没有残留临时文件
        self.assertEqual(open(p, "rb").read(), orig_bytes)
        leftovers = [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        # _original_content 未被更新 → 仍是"已修改"，缓冲区数据没丢
        self.assertTrue(pane.is_modified())


class TestQuitSaveGuard(_Base):
    def test_no_changes_proceeds(self):
        area, pane, p = self._area_with_file()
        self.assertFalse(area.has_unsaved())
        self.assertTrue(area.prompt_save_all())  # 无改动 → 直接放行，不弹窗

    def test_cancel_aborts_close(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("unsaved\n")
        self.assertTrue(area.has_unsaved())
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Cancel)
        try:
            self.assertFalse(area.prompt_save_all())  # 取消 → 中止关闭
        finally:
            QMessageBox.question = orig
        # 取消后内容仍在
        self.assertEqual(pane.editor.toPlainText(), "unsaved\n")

    def test_save_choice_persists(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("saved on quit\n")
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Save)
        try:
            self.assertTrue(area.prompt_save_all())
        finally:
            QMessageBox.question = orig
        with open(p) as f:
            self.assertEqual(f.read(), "saved on quit\n")
        self.assertFalse(pane.is_modified())

    def test_discard_proceeds_without_writing(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("disk\n")
        pane.editor.setPlainText("discarded\n")
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        try:
            self.assertTrue(area.prompt_save_all())  # 丢弃 → 放行
        finally:
            QMessageBox.question = orig
        with open(p) as f:
            self.assertEqual(f.read(), "disk\n")  # 磁盘未被写

    def test_flush_autosave_writes_backup(self):
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("crash recovery\n")
        # 强制路径：不弹窗刷备份
        area.flush_autosave_all()
        paths = pane._backup_paths()
        self.assertIsNotNone(paths)
        backup_file, _meta = paths
        self.assertTrue(os.path.exists(backup_file),
                        "flush_autosave_all 应写出崩溃恢复备份")


class TestSaveConflict(_Base):
    """#3：保存前若磁盘已被改新（其它窗格/外部），先征求用户，默认不覆盖。"""

    def _make_disk_newer(self, pane, p, content):
        with open(p, "w") as f:
            f.write(content)
        future = time.time() + 10
        os.utime(p, (future, future))

    def test_conflict_no_aborts_and_preserves_disk(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("my local edit\n")
        self._make_disk_newer(pane, p, "external wrote this\n")
        calls = []
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: (calls.append(1), QMessageBox.StandardButton.No)[1])
        try:
            self.assertFalse(pane.save_file())  # No → 中止保存
        finally:
            QMessageBox.question = orig
        self.assertTrue(calls, "应弹出保存冲突提示")
        with open(p) as f:
            self.assertEqual(f.read(), "external wrote this\n")  # 磁盘未被覆盖

    def test_conflict_yes_overwrites(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("force mine\n")
        self._make_disk_newer(pane, p, "external\n")
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            self.assertTrue(pane.save_file())
        finally:
            QMessageBox.question = orig
        with open(p) as f:
            self.assertEqual(f.read(), "force mine\n")


class TestExternalChangeAfterSave(_Base):
    """#4：自己的保存靠字节指纹识别（不再用时间窗），保存后短时间内真实的外部
    改动不再被吞掉。"""

    def test_self_save_recognized_no_prompt(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("a\n")
        pane.editor.setPlainText("b\n")
        self.assertTrue(pane.save_file())
        # 保存后继续打字（缓冲区≠磁盘），再把 mtime 顶新以绕过 mtime 相等的早退，
        # 强制走到字节指纹比对那一步
        pane.editor.setPlainText("b kept typing\n")
        future = time.time() + 10
        os.utime(p, (future, future))
        calls = []
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: (calls.append(1), QMessageBox.StandardButton.No)[1])
        try:
            pane._handle_external_change()
        finally:
            QMessageBox.question = orig
        self.assertEqual(calls, [], "磁盘==自己刚写的字节 → 不应弹外部改动提示")
        self.assertEqual(pane.editor.toPlainText(), "b kept typing\n")  # 未被重载

    def test_real_external_change_detected(self):
        area, pane, p = self._area_with_file("a\n")
        # 干净状态（无本地改动）下外部写入不同内容 → 静默重载到新内容
        with open(p, "w") as f:
            f.write("external new\n")
        future = time.time() + 10
        os.utime(p, (future, future))
        pane._handle_external_change()
        self.assertEqual(pane.editor.toPlainText(), "external new\n")


class TestStaleAutosaveRestore(_Base):
    """#5：备份比磁盘旧时，恢复对话框默认「否」并用警示文案，避免顺手回车覆盖
    较新的磁盘内容。"""

    def test_disk_newer_defaults_to_no(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("disk v1\n")
        # 制造一个比磁盘"旧"的崩溃恢复备份
        pane.editor.setPlainText("old unsaved\n")
        pane._autosave_tick()
        paths = pane._backup_paths()
        self.assertIsNotNone(paths)
        self.assertTrue(os.path.exists(paths[0]))
        # 磁盘文件随后被外部更新成更新的版本
        with open(p, "w") as f:
            f.write("disk v2 newer\n")
        future = time.time() + 100
        os.utime(p, (future, future))

        captured = {}
        orig = QMessageBox.question

        def fake_q(parent, title, text, buttons, default=None):
            captured["default"] = default
            captured["text"] = text
            return QMessageBox.StandardButton.No

        QMessageBox.question = staticmethod(fake_q)
        try:
            restored = pane._maybe_restore_autosave(p, "disk v2 newer\n")
        finally:
            QMessageBox.question = orig
        self.assertIsNone(restored)  # No → 不恢复
        self.assertEqual(captured.get("default"), QMessageBox.StandardButton.No,
                         "磁盘更新时默认按钮应为 No")


if __name__ == "__main__":
    unittest.main()
