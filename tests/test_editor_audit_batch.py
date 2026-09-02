# -*- coding: utf-8 -*-
"""编辑器审查批次的回归测试：

1. CRLF 文件保存后行尾保持 CRLF（此前一律写回 LF，Windows 项目改一行 diff 整文件）
2. 通过软链保存写的是目标文件、链接本身保留（此前 os.replace 把链接换成普通文件）
3. is_modified 不再每次击键都 toPlainText() 整份缓冲区
4. .py 多行 docstring 第二行也是字符串色（旧 PythonHighlighter 无跨行状态）
5. EditorArea.on_path_renamed / on_path_deleted：文件树改名/删除后编辑器跟着换路径

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_editor_audit_batch.py -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._areas = []
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ed_audit_"))

    def tearDown(self):
        for area in self._areas:
            for pane in list(getattr(area, '_panes', [])):
                fw = getattr(pane, '_file_watcher', None)
                if fw is not None:
                    try:
                        files = fw.files()
                        if files:
                            fw.removePaths(files)
                        fw.fileChanged.disconnect()
                    except (TypeError, RuntimeError):
                        pass
            try:
                area.deleteLater()
            except Exception:
                pass
        self._areas = []
        self.app.processEvents()

    def _write(self, name, data: bytes):
        p = os.path.join(self.tmp, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def _open(self, path):
        from file_editor import EditorArea
        area = EditorArea(theme={})
        self._areas.append(area)
        self.assertTrue(area.open_file_in_active(path))
        return area, area.active_pane


class TestCrlfPreserved(_Base):
    def test_crlf_round_trip(self):
        p = self._write("win.txt", b"a\r\nb\r\n")
        area, pane = self._open(p)
        self.assertEqual(pane.editor.toPlainText(), "a\nb\n")
        self.assertFalse(pane.is_modified(), "CRLF 文件一打开不应判成已修改")
        pane.editor.setPlainText("a\nb\nc\n")
        self.assertTrue(pane.save_file())
        with open(p, 'rb') as f:
            self.assertEqual(f.read(), b"a\r\nb\r\nc\r\n")
        self.assertIn("CRLF", pane.file_label.text())

    def test_lf_unchanged(self):
        p = self._write("unix.txt", b"a\nb\n")
        area, pane = self._open(p)
        pane.editor.setPlainText("a\nb\nc\n")
        self.assertTrue(pane.save_file())
        with open(p, 'rb') as f:
            self.assertEqual(f.read(), b"a\nb\nc\n")
        self.assertNotIn("CRLF", pane.file_label.text())

    def test_mixed_majority_wins(self):
        p = self._write("mixed.txt", b"a\r\nb\r\nc\n")
        area, pane = self._open(p)
        pane.editor.setPlainText("x\ny\nz\n")
        self.assertTrue(pane.save_file())
        with open(p, 'rb') as f:
            self.assertEqual(f.read(), b"x\r\ny\r\nz\r\n")


@unittest.skipIf(sys.platform == 'win32', "symlink 需要特权")
class TestSymlinkSafeSave(_Base):
    def test_save_through_symlink_writes_target(self):
        target = self._write("real.txt", b"orig\n")
        link = os.path.join(self.tmp, "link.txt")
        os.symlink(target, link)
        area, pane = self._open(link)
        pane.editor.setPlainText("new\n")
        self.assertTrue(pane.save_file())
        self.assertTrue(os.path.islink(link), "保存后软链被换成了普通文件")
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b"new\n", "写的不是软链指向的目标文件")


class TestModifiedTracking(_Base):
    def test_typing_marks_modified_without_full_copy(self):
        p = self._write("a.txt", b"hello\n")
        area, pane = self._open(p)
        self.assertFalse(pane.is_modified())
        calls = []
        orig = pane.editor.toPlainText
        pane.editor.toPlainText = lambda: (calls.append(1), orig())[1]
        try:
            cur = pane.editor.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            pane.editor.setTextCursor(cur)
            pane.editor.insertPlainText("x")
            self.assertTrue(pane.is_modified())
            self.assertEqual(
                calls, [], "每次击键都整份 toPlainText() 比较，大文件每键复制几 MB")
        finally:
            pane.editor.toPlainText = orig

    def test_save_clears_modified(self):
        p = self._write("a.txt", b"hello\n")
        area, pane = self._open(p)
        pane.editor.setPlainText("hello world\n")
        self.assertTrue(pane.is_modified())
        self.assertTrue(pane.save_file())
        self.assertFalse(pane.is_modified())

    def test_back_to_baseline_is_not_modified(self):
        """删掉又敲回同样的内容 → 与基准一致，不算修改（沿用原语义）"""
        p = self._write("a.txt", b"hello\n")
        area, pane = self._open(p)
        pane.editor.setPlainText("hello!\n")
        self.assertTrue(pane.is_modified())
        pane.editor.setPlainText("hello\n")
        self.assertFalse(pane.is_modified())


class TestPythonHighlighterBlockState(_Base):
    def test_multiline_docstring_second_line_is_string(self):
        from PyQt6.QtGui import QColor
        p = self._write("m.py", b'def f():\n    """first\n    second # not comment\n    """\n    return 1\n')
        area, pane = self._open(p)
        self.app.processEvents()
        doc = pane.editor.document()
        block = doc.findBlockByNumber(2)  # "    second # not comment"
        fmts = block.layout().formats()
        self.assertTrue(fmts, "第二行没有任何高亮格式")
        colors = {f.format.foreground().color().name() for f in fmts}
        self.assertIn(QColor("#98c379").name(), colors, "docstring 第二行不是字符串色")
        self.assertNotIn(QColor("#5c6370").name(), colors, "docstring 里的 # 被当成了注释")


class TestPathHooks(_Base):
    def test_rename_updates_current_file_and_tab(self):
        p = self._write("old.txt", b"x\n")
        area, pane = self._open(p)
        new = os.path.join(self.tmp, "new.txt")
        os.rename(p, new)
        area.on_path_renamed(p, new)
        self.assertEqual(pane.get_current_file(), new)
        self.assertEqual(pane.file_label.text().split()[0], "new.txt")
        self.assertIn(new, pane._tab_paths())
        self.assertNotIn(p, pane._tab_paths())
        self.assertIn(new, pane._file_watcher.files())
        self.assertFalse(pane.is_modified(), "改名不应把缓冲区标成已修改")
        # 之后保存写到新路径
        pane.editor.setPlainText("y\n")
        self.assertTrue(pane.save_file())
        with open(new, 'rb') as f:
            self.assertEqual(f.read(), b"y\n")
        self.assertFalse(os.path.exists(p), "保存又在旧路径重建了文件")

    def test_dir_rename_updates_nested_file(self):
        d = os.path.join(self.tmp, "dir")
        os.mkdir(d)
        p = os.path.join(d, "f.txt")
        with open(p, 'wb') as f:
            f.write(b"x\n")
        area, pane = self._open(p)
        d2 = os.path.join(self.tmp, "dir2")
        os.rename(d, d2)
        area.on_path_renamed(d, d2)
        self.assertEqual(pane.get_current_file(), os.path.join(d2, "f.txt"))

    def test_rename_keeps_view_state(self):
        import file_editor
        p = self._write("v.txt", b"1\n2\n3\n")
        area, pane = self._open(p)
        pane._save_view_state()
        new = os.path.join(self.tmp, "v2.txt")
        os.rename(p, new)
        area.on_path_renamed(p, new)
        self.assertIn(os.path.abspath(new), file_editor._VIEW_STATE_REGISTRY)
        self.assertNotIn(os.path.abspath(p), file_editor._VIEW_STATE_REGISTRY)

    def test_delete_marks_without_modal(self):
        from unittest import mock
        p = self._write("gone.txt", b"x\n")
        area, pane = self._open(p)
        os.remove(p)
        with mock.patch("file_editor.QMessageBox.warning") as warn:
            area.on_path_deleted(p)
            # watcher 的延迟处理跑到也不能再弹窗
            pane._handle_external_change()
            self.assertEqual(warn.call_count, 0, "删除通知后不应再弹模态框")
        self.assertIn("⚠", pane.file_label.text())
        self.assertEqual(pane.get_current_file(), p, "缓冲区应保留，下次保存重建")


if __name__ == '__main__':
    unittest.main()
