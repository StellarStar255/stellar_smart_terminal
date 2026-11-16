"""Markdown image paste saves portable assets through the actual Qt paste path."""
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtGui import QImage, QKeySequence, QTextCursor, QTextDocument
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from file_editor import FileEditorWidget


class TestMarkdownImagePaste(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / '中文 notes' / 'example.md'
        self.path.parent.mkdir()
        self.path.write_text('before\n', encoding='utf-8')
        self.pane = FileEditorWidget(theme={})
        self.pane.resize(800, 600)
        self.assertTrue(self.pane.open_file(str(self.path)))
        self.pane._autosave_timer.stop()
        self.addCleanup(self.pane.deleteLater)
        self.addCleanup(self.pane._stop_watching)
        self.addCleanup(self.app.clipboard().clear)
        self.image = QImage(12, 8, QImage.Format.Format_ARGB32)
        self.image.fill(0x804080C0)
        self.editor = self.pane.editor
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(cursor)

    def mime(self):
        mime = QMimeData()
        mime.setImageData(self.image)
        return mime

    def links(self):
        return re.findall(r'!\[image\]\((assets/[^)]+)\)', self.editor.toPlainText())

    def test_image_only_clipboard_paste_enabled_and_saves_png_beside_document(self):
        self.app.clipboard().setMimeData(self.mime())
        self.assertTrue(self.editor.canPaste())
        self.editor.paste()
        self.assertEqual(len(self.links()), 1)
        asset = self.path.parent / self.links()[0]
        actual = QImage(str(asset))
        self.assertFalse(actual.isNull())
        self.assertEqual(actual.size(), self.image.size())
        self.assertEqual(actual.pixelColor(0, 0), self.image.pixelColor(0, 0))
        self.assertTrue(self.pane.is_modified())
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'before\n')

    def test_real_paste_shortcut_undo_redo_and_preview(self):
        self.pane.show()
        self.pane.activateWindow()
        self.editor.setFocus()
        self.app.processEvents()
        self.app.clipboard().setImage(self.image)
        QTest.keySequence(self.editor, QKeySequence(QKeySequence.StandardKey.Paste))
        self.app.processEvents()
        self.assertEqual(len(self.links()), 1)
        reference = self.links()[0]
        after = self.editor.toPlainText()
        self.editor.undo()
        self.assertEqual(self.editor.toPlainText(), 'before\n')
        self.editor.redo()
        self.assertEqual(self.editor.toPlainText(), after)
        self.pane._set_md_preview(True, sync_scroll=False)
        doc = self.pane._md_browser.document()
        image = doc.resource(QTextDocument.ResourceType.ImageResource,
                             doc.baseUrl().resolved(QUrl(reference)))
        self.assertIsNotNone(image)
        self.assertFalse(image.isNull())

    def test_repeated_paste_does_not_overwrite_assets(self):
        self.editor.insertFromMimeData(self.mime())
        first = (self.path.parent / self.links()[0]).read_bytes()
        self.editor.insertFromMimeData(self.mime())
        self.assertEqual(len(set(self.links())), 2)
        self.assertEqual((self.path.parent / self.links()[0]).read_bytes(), first)

    def test_file_urls_preserve_original_bytes_and_are_one_undo_step(self):
        first = self.root / 'a 中文 image.png'
        second = self.root / 'b.png'
        self.image.save(str(first))
        self.image.save(str(second))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in (first, second)])
        self.app.clipboard().setMimeData(mime)
        self.assertTrue(self.editor.canPaste())
        self.editor.paste()
        self.assertEqual(len(self.links()), 2)
        for link, original in zip(self.links(), (first, second)):
            self.assertEqual((self.path.parent / link).read_bytes(), original.read_bytes())
        self.editor.undo()
        self.assertEqual(self.editor.toPlainText(), 'before\n')

    def test_failed_asset_write_keeps_selected_text_and_no_broken_link(self):
        self.editor.selectAll()
        with mock.patch('file_editor.atomic_writer', side_effect=PermissionError('read only')), \
                mock.patch('file_editor.QMessageBox.warning') as warning:
            self.editor.insertFromMimeData(self.mime())
        warning.assert_called_once()
        self.assertEqual(self.editor.toPlainText(), 'before\n')
        self.assertEqual(list((self.path.parent / 'assets').iterdir()), [])
        self.assertFalse(self.pane.is_modified())

    def test_failed_second_file_rolls_back_first_asset(self):
        first = self.root / 'good.png'
        self.image.save(str(first))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(first)),
                      QUrl.fromLocalFile(str(self.root / 'missing.png'))])
        with mock.patch('file_editor.QMessageBox.warning') as warning:
            self.editor.insertFromMimeData(mime)
        warning.assert_called_once()
        self.assertEqual(self.editor.toPlainText(), 'before\n')
        self.assertEqual(list((self.path.parent / 'assets').iterdir()), [])

    def test_plain_text_other_file_types_and_read_only_are_unaffected(self):
        text = QMimeData()
        text.setText('a\r\nb\u2028c')
        self.editor.insertFromMimeData(text)
        self.assertTrue(self.editor.toPlainText().endswith('a\nb\nc'))
        self.editor.setReadOnly(True)
        self.editor.insertFromMimeData(self.mime())
        self.assertFalse((self.path.parent / 'assets').exists())
        self.editor.setReadOnly(False)
        other = self.path.with_suffix('.txt')
        other.write_text('plain', encoding='utf-8')
        with mock.patch('file_editor.QMessageBox.question'):
            self.assertTrue(self.pane.open_file(str(other)))
        self.editor.insertFromMimeData(self.mime())
        self.assertEqual(self.editor.toPlainText(), 'plain')
        self.assertFalse((self.path.parent / 'assets').exists())

    def test_save_and_reopen_keeps_relative_image_reference(self):
        self.editor.insertFromMimeData(self.mime())
        expected = self.editor.toPlainText()
        self.assertTrue(self.pane.save_file())
        self.assertEqual(self.path.read_text(encoding='utf-8'), expected)
        self.assertTrue(self.pane.open_file(str(self.path)))
        self.assertEqual(self.editor.toPlainText(), expected)
        self.assertTrue((self.path.parent / self.links()[0]).is_file())

    def test_assets_file_conflict_reports_failure_without_changing_document(self):
        (self.path.parent / 'assets').write_text('keep', encoding='utf-8')
        with mock.patch('file_editor.QMessageBox.warning') as warning:
            self.editor.insertFromMimeData(self.mime())
        warning.assert_called_once()
        self.assertEqual(self.editor.toPlainText(), 'before\n')
        self.assertEqual((self.path.parent / 'assets').read_text(encoding='utf-8'), 'keep')
