"""Markdown 预览态不能泄漏到下一个打开的文件。

审查离屏实测：预览 a.md → 打开 b.md 渲染 4 次（应 1 次）；预览 → 打开
1.6MB .py 白耗 1.88s 按 markdown 渲染；预览 → 打开 540KB .md 后
`_in_md_preview=True, stack=0, md_btn.checked=True`，源码视图里每敲一键
都把隐藏的预览重渲染一遍（1.36s/键）。

根因：open_file 先 setPlainText 再 _set_md_support/_set_md_preview，
中间 _on_text_changed 看到上一个文件留下的 _in_md_preview=True 就渲染；
超阈值 .md 走"默认源码视图"分支时 _in_md_preview / md_btn 根本没被重置。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_editor_md_preview_leak_2026_09_edit.py -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402


class MdPreviewLeakTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from file_editor import FileEditorWidget, _MD_AUTO_PREVIEW_MAX_BYTES
        cls.FileEditorWidget = FileEditorWidget
        cls.MAX = _MD_AUTO_PREVIEW_MAX_BYTES
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _pane(self):
        pane = self.FileEditorWidget(theme={})
        pane.resize(800, 600)
        pane.show()
        self.addCleanup(pane.deleteLater)
        self.addCleanup(pane._stop_watching)
        return pane

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def _preview_pane(self):
        pane = self._pane()
        pane.open_file(self._write('a.md', "# A\n\ntext\n"))
        # md 打开默认是源码视图（用户要求），这里手动切进预览态做前置
        pane._set_md_preview(True)
        self.assertTrue(pane._in_md_preview, "前置：手动切进预览")
        return pane

    def test_preview_then_open_other_md_opens_source_without_render(self):
        """预览态下切到另一个 md：按默认回源码视图，一次都不渲染。"""
        pane = self._preview_pane()
        with patch.object(pane, '_render_md_document',
                          wraps=pane._render_md_document) as render:
            pane.open_file(self._write('b.md', "# B\n\nother\n"))
        self.assertEqual(render.call_count, 0,
                         f"预览态切换 md 文件渲染了 {render.call_count} 次")
        self.assertFalse(pane._in_md_preview)
        self.assertFalse(pane.md_btn.isChecked())
        self.assertEqual(pane._stack.currentIndex(), 0)

    def test_preview_then_open_non_md_renders_zero(self):
        pane = self._preview_pane()
        with patch.object(pane, '_render_md_document',
                          wraps=pane._render_md_document) as render:
            pane.open_file(self._write('c.py', "x = 1\n" * 200))
        self.assertEqual(render.call_count, 0,
                         "非 markdown 文件不应按 markdown 渲染")
        self.assertFalse(pane._in_md_preview)
        self.assertFalse(pane.md_btn.isChecked())
        self.assertEqual(pane._stack.currentIndex(), 0)

    def test_preview_then_open_huge_md_leaves_clean_source_view(self):
        pane = self._preview_pane()
        huge = self._write('huge.md', "line of text\n" * (self.MAX // 8))
        self.assertGreater(os.path.getsize(huge), self.MAX)
        pane.open_file(huge)
        # 超阈值：源码视图，且预览态/按钮都要同步清干净
        self.assertEqual(pane._stack.currentIndex(), 0)
        self.assertFalse(pane._in_md_preview,
                         "超阈值 md 在源码视图里却仍标记为预览态")
        self.assertFalse(pane.md_btn.isChecked())
        # 源码视图里敲一个字符不能触发隐藏预览的重渲染
        with patch.object(pane, '_render_md_document',
                          wraps=pane._render_md_document) as render:
            pane.editor.insertPlainText('x')
        self.assertEqual(render.call_count, 0,
                         "源码视图里每敲一键都在重渲染隐藏的预览")
        # 手动切进预览仍然可用
        pane._set_md_preview(True)
        self.assertTrue(pane._in_md_preview)
        self.assertEqual(pane._stack.currentIndex(), 2)


if __name__ == '__main__':
    unittest.main()
