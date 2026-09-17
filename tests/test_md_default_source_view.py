"""Markdown 文件打开后默认停在源码视图，不自动进渲染预览。

用户明确要求：点开 .md 默认是"不预览"的；◎ 按钮 / 快捷键手动切预览照旧可用。
（此前小于阈值的 .md 打开即进预览，用户想直接编辑时每次都得先切回来。）

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_md_default_source_view.py -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402


class MdDefaultSourceViewTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from file_editor import FileEditorWidget
        cls.FileEditorWidget = FileEditorWidget
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

    def test_small_md_opens_in_source_view(self):
        pane = self._pane()
        self.assertTrue(pane.open_file(self._write('a.md', "# A\n\ntext\n")))
        self.assertTrue(pane._md_preview_supported, "◎ 按钮仍要对 md 可用")
        self.assertTrue(pane.md_btn.isVisible())
        self.assertFalse(pane._in_md_preview, "md 打开后不应自动进预览")
        self.assertFalse(pane.md_btn.isChecked())
        self.assertEqual(pane._stack.currentIndex(), 0)

    def test_manual_preview_still_works_and_next_md_opens_in_source(self):
        pane = self._pane()
        pane.open_file(self._write('b.md', "# B\n\n> q\n"))
        pane._toggle_md_preview()
        self.assertTrue(pane._in_md_preview)
        self.assertEqual(pane._stack.currentIndex(), 2)
        self.assertIn('▎', pane._md_browser.document().toPlainText())
        # 预览态下再点开另一个 md：仍按默认走源码视图，不继承预览态
        pane.open_file(self._write('c.md', "# C\n"))
        self.assertFalse(pane._in_md_preview)
        self.assertFalse(pane.md_btn.isChecked())
        self.assertEqual(pane._stack.currentIndex(), 0)


if __name__ == '__main__':
    unittest.main()
