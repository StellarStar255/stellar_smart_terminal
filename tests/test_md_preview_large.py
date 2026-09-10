"""Markdown 预览：大文件不能把 GUI 线程卡死。

背景（Ubuntu 上实测，打开几 MB 的 .md 整个窗口冻住、关都关不掉）：
预览渲染的排版后处理（剥 HTML 标签、插代码块 pad、插引用条、每个
代码行插左内边距……）对预览文档做与文档大小成正比次数的 QTextCursor
编辑。文档一旦完成过布局，每次编辑都让 QTextDocumentLayout 从改动处
重排全文 → O(n²)：466KB 重渲染 35 秒。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_md_preview_large.py -v
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


_SECTION = (
    "## Heading\n"
    "Text with **bold** and `code` and <br> a break.\n\n"
    "> quote one\n> quote two\n\n"
    "```python\ndef f(x):\n    return x  # c\n```\n\n"
    "<div align=\"center\">\ncentered\n</div>\n\n"
)


class MdPreviewLargeTest(unittest.TestCase):

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
        return pane

    def _write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_rerender_notifies_layout_once(self):
        """后处理的所有编辑必须合并成一次 contentsChange（= 一次重排）。

        修复前每次 QTextCursor 编辑都单独通知布局，这里的文档实测产生
        404 次通知；修复后只剩 setMarkdown 自身的清空/填充与 endEditBlock
        合计 4 次常数开销。
        """
        pane = self._pane()
        pane.open_file(self._write('a.md', _SECTION * 40))
        self.assertTrue(pane._in_md_preview)
        doc = pane._md_browser.document()
        doc.size()   # 强制完成整篇布局：修复前 O(n²) 的触发条件

        hits = []
        doc.contentsChange.connect(lambda *a: hits.append(a))
        pane._render_md_preview()
        self.assertLessEqual(len(hits), 4, f"{len(hits)} layout notifications")
        # 渲染结果本身没坏：引用条 / 代码 pad 这些后处理仍然生效
        self.assertIn('▎', doc.toPlainText())

    def test_huge_markdown_opens_in_source_mode(self):
        """超过阈值的 .md 不默认进预览（渲染秒级会卡住），◎ 手动仍可切。"""
        import file_editor
        with patch.object(file_editor, '_MD_AUTO_PREVIEW_MAX_BYTES', 1000):
            pane = self._pane()
            self.assertTrue(pane.open_file(self._write('big.md', _SECTION * 20)))
            self.assertTrue(pane._md_preview_supported)
            self.assertFalse(pane._in_md_preview)
            self.assertEqual(pane._stack.currentIndex(), 0)
            # 手动切预览照常工作
            pane._toggle_md_preview()
            self.assertTrue(pane._in_md_preview)
            self.assertIn('▎', pane._md_browser.document().toPlainText())

            # 阈值以内的照旧默认进预览
            pane2 = self._pane()
            pane2.open_file(self._write('small.md', "# hi\n"))
            self.assertTrue(pane2._in_md_preview)

    def test_viewport_refit_skips_rerender_without_images(self):
        """没有图片的文档，视口变宽（滚动条出现）不再整篇重渲染。"""
        pane = self._pane()
        pane.open_file(self._write('noimg.md', _SECTION * 5))
        self.assertFalse(pane._md_has_images)
        pane._md_last_render_width = -1   # 模拟视口宽度变了
        with patch.object(pane, '_render_md_preview') as render:
            pane._md_refit_tick()
        render.assert_not_called()

        # 有图片时仍要重渲染（宽图按视口缩放依赖它）
        pane.editor.setPlainText("![x](x.png)\n\ntext\n")
        pane._render_md_preview()
        self.assertTrue(pane._md_has_images)
        pane._md_last_render_width = -1
        with patch.object(pane, '_render_md_preview') as render:
            pane._md_refit_tick()
        render.assert_called_once()


if __name__ == '__main__':
    unittest.main()
