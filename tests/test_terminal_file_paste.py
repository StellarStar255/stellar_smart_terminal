"""File clipboard URLs must reach the TUI as complete, individually escaped paths."""
import os
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QApplication
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def terminal():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    written = []
    widget._backend = SimpleNamespace(write=lambda b: written.append(b) or True)
    widget.stream.feed('\x1b[?2004h')
    yield widget, written
    widget._backend = None
    widget.cleanup()
    widget.deleteLater()
    _APP.processEvents()


def pasted_arguments(written):
    arguments = []
    for data in written:
        assert data.startswith(b'\x1b[200~') and data.endswith(b'\x1b[201~')
        text = data[len(b'\x1b[200~'):-len(b'\x1b[201~')].decode()
        parts = shlex.split(text)
        assert len(parts) == 1, text
        arguments.extend(parts)
    return arguments


def canonical(paths):
    return [os.path.normcase(os.path.realpath(path)) for path in paths]


def test_qt_file_urls_handle_every_image_document_and_directory(terminal, tmp_path):
    widget, written = terminal
    paths = [tmp_path / '地下停车场的空气一般怎么样？.png',
             tmp_path / 'project notes.pdf', tmp_path / 'second image.PNG',
             tmp_path / 'folder with spaces']
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    clipboard = SimpleNamespace(mimeData=lambda: mime, text=Mock(return_value='names only'))
    images = []
    widget.image_pasted.connect(images.append)
    widget._paste_from_clipboard_qt(clipboard)
    assert canonical(pasted_arguments(written)) == canonical(paths)
    assert canonical(images) == canonical([paths[0], paths[2]])
    clipboard.text.assert_not_called()


def test_drop_uses_bracketed_paste_and_same_file_delivery(terminal, tmp_path):
    widget, written = terminal
    path = str(tmp_path / '中文 image.png')
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    widget.dropEvent(event)
    assert canonical(pasted_arguments(written)) == canonical([path])
    assert event.isAccepted()


@pytest.mark.parametrize('path', ["/tmp/中文 空格？.png", "/tmp/quote'and\"double.png",
                                 '/tmp/$dollar`command`*.png', '/tmp/new\nline.png'])
def test_posix_path_round_trips_as_one_literal_argument(path):
    with patch('terminal_input.sys.platform', 'darwin'):
        assert shlex.split(TerminalWidget._quote_pasted_path(path)) == [path]


def test_windows_paths_preserve_backslashes_and_wrap_spaces():
    path = 'C:\\Users\\Example\\My Pictures\\中文.png'
    with patch('terminal_input.sys.platform', 'win32'):
        assert TerminalWidget._quote_pasted_path(path) == '"' + path + '"'


def test_non_file_url_keeps_normal_text_paste(terminal):
    widget, written = terminal
    mime = QMimeData()
    mime.setUrls([QUrl('https://example.invalid/image.png')])
    clipboard = SimpleNamespace(mimeData=lambda: mime, text=lambda: 'https://example.invalid/image.png')
    widget._paste_from_clipboard_qt(clipboard)
    assert pasted_arguments(written) == ['https://example.invalid/image.png']
