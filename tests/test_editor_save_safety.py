"""编辑器保存安全性测试（修复 #1 退出保存、#2 原子写）。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_editor_save_safety -v
"""
import os
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
