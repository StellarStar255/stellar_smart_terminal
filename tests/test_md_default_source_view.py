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
from unittest import mock

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


class MdDefaultPreviewSettingTest(unittest.TestCase):
    """可选的「Markdown 默认预览」开关：开了就恢复 v1.31.0 以前的行为——
    小于阈值的 .md 打开即进预览；大文件仍走源码视图。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from file_editor import FileEditorWidget
        cls.FileEditorWidget = FileEditorWidget
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        orig = getattr(self.FileEditorWidget, 'MD_DEFAULT_PREVIEW', False)
        self.addCleanup(setattr, self.FileEditorWidget, 'MD_DEFAULT_PREVIEW', orig)

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

    def test_setting_on_opens_small_md_in_preview(self):
        self.FileEditorWidget.MD_DEFAULT_PREVIEW = True
        pane = self._pane()
        pane.open_file(self._write('p.md', "# P\n\ntext\n"))
        self.assertTrue(pane._in_md_preview)
        self.assertTrue(pane.md_btn.isChecked())
        self.assertEqual(pane._stack.currentIndex(), 2)

    def test_setting_on_keeps_large_md_in_source_view(self):
        import file_editor
        self.FileEditorWidget.MD_DEFAULT_PREVIEW = True
        with mock.patch.object(file_editor, '_MD_AUTO_PREVIEW_MAX_BYTES', 100):
            pane = self._pane()
            pane.open_file(self._write('big.md', "line of text\n" * 50))
            self.assertFalse(pane._in_md_preview)
            self.assertEqual(pane._stack.currentIndex(), 0)

    def test_window_setting_drives_class_flag_and_config(self):
        import app_config
        import main_window
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QCloseEvent
        wins = []

        def _dispose():
            for w in wins:
                QApplication.sendEvent(w, QCloseEvent())
                w.deleteLater()
            for _ in range(3):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
        self.addCleanup(_dispose)
        with mock.patch.object(app_config, 'read_config', return_value={}):
            a = main_window.MainWindow()
            b = main_window.MainWindow()
        wins += [a, b]
        self.assertFalse(a._md_default_preview)
        with mock.patch.object(app_config, 'update_config_with') as saver:
            a._set_md_default_preview(True)
        self.assertTrue(self.FileEditorWidget.MD_DEFAULT_PREVIEW)
        self.assertTrue(b._md_default_preview, "要广播到其它窗口")
        self.assertTrue(saver.called, "要落盘")
        # 配置里带 md_default_preview=True 启动 → 类开关直接为 True
        self.FileEditorWidget.MD_DEFAULT_PREVIEW = False
        with mock.patch.object(app_config, 'read_config',
                               return_value={'md_default_preview': True}):
            c = main_window.MainWindow()
        wins.append(c)
        self.assertTrue(c._md_default_preview)
        self.assertTrue(self.FileEditorWidget.MD_DEFAULT_PREVIEW)


if __name__ == '__main__':
    unittest.main()
