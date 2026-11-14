"""Real AppKit/JXA integration on a named pasteboard; never replaces user clipboard."""
import json
import os
import shlex
import subprocess
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtGui import QImage, QColor
from PyQt6.QtWidgets import QApplication
from terminal_widget import TerminalWidget

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS AppKit/JXA')
_APP = None


@pytest.fixture
def pasteboard(tmp_path):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    widget.image_save_local = True
    written, images = [], []
    widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True)
    widget.stream.feed('\x1b[?2004h')
    widget.image_pasted.connect(images.append)
    project = tmp_path / 'project with spaces'
    project.mkdir()
    widget.get_cwd = lambda: str(project)
    widget._ensure_clipboard_watch = lambda: None
    widget._clipboard_change_token = lambda: None
    name = 'StellarFilePasteTest-' + uuid.uuid4().hex
    board = '$.NSPasteboard.pasteboardWithName(' + json.dumps(name) + ')'
    real_run = subprocess.run

    def run_js(body):
        result = real_run(['osascript', '-l', 'JavaScript', '-e',
                           "ObjC.import('AppKit'); var pb=" + board + '; ' + body],
                          capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        return result

    def paste():
        def isolated(cmd, **kwargs):
            assert cmd[0] == 'osascript'
            cmd = list(cmd)
            cmd[-1] = cmd[-1].replace('$.NSPasteboard.generalPasteboard', board)
            result = real_run(cmd, **kwargs)
            assert result.returncode == 0, result.stderr
            return result
        clipboard = SimpleNamespace(text=Mock(side_effect=AssertionError('file became filename text')))
        with patch('subprocess.run', side_effect=isolated):
            widget._paste_from_clipboard_macos(clipboard)
        clipboard.text.assert_not_called()

    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor('green'))
    path = tmp_path / '地下停车场的空气一般怎么样？.png'
    assert image.save(str(path), 'PNG')
    try:
        yield widget, run_js, paste, written, images, path, image, project
    finally:
        run_js('pb.releaseGlobally;')
        widget._backend = None
        widget.cleanup()
        widget.deleteLater()
        _APP.processEvents()


def arguments(written):
    result = []
    for data in written:
        assert data.startswith(b'\x1b[200~') and data.endswith(b'\x1b[201~')
        parts = shlex.split(data[6:-6].decode())
        assert len(parts) == 1
        result.extend(parts)
    return result


@pytest.mark.parametrize('kind', ['modern', 'legacy'])
def test_real_finder_file_formats_deliver_full_chinese_path(pasteboard, kind):
    widget, run_js, paste, written, images, path, image, project = pasteboard
    if kind == 'modern':
        run_js('pb.clearContents; pb.setStringForType(' + json.dumps(path.as_uri())
               + ', "public.file-url"); pb.setStringForType('
               + json.dumps(path.name) + ', "public.utf8-plain-text");')
    else:
        run_js('pb.clearContents; pb.setPropertyListForType($(' + json.dumps([str(path)])
               + '), "NSFilenamesPboardType"); pb.setStringForType('
               + json.dumps(path.name) + ', "public.utf8-plain-text");')
    paste()
    assert arguments(written) == [str(path)]
    assert images == [str(path)]
    assert not (project / '.images').exists()  # existing files need no image conversion/copy


def test_real_legacy_multi_file_list_preserves_all_paths(pasteboard):
    widget, run_js, paste, written, images, path, image, project = pasteboard
    second = path.with_name('second file with spaces.png')
    assert image.save(str(second), 'PNG')
    document = path.with_name('notes.txt')
    document.write_text('synthetic fixture')
    paths = [str(path), str(second), str(document)]
    run_js('pb.clearContents; pb.setPropertyListForType($(' + json.dumps(paths)
           + '), "NSFilenamesPboardType");')
    paste()
    assert arguments(written) == paths
    assert images == paths[:2]


def test_real_multiple_pasteboard_items_preserve_each_file(pasteboard):
    widget, run_js, paste, written, images, path, image, project = pasteboard
    second = path.with_name('second image.png')
    assert image.save(str(second), 'PNG')
    paths = [str(path), str(second)]
    urls = [path.as_uri(), second.as_uri()]
    result = run_js('pb.clearContents; var entries=$.NSMutableArray.alloc.init; '
                    'var urls=' + json.dumps(urls) + '; '
                    'for(var i=0; i<urls.length; i++) { '
                    'var entry=$.NSPasteboardItem.alloc.init; '
                    'entry.setStringForType(urls[i], "public.file-url"); '
                    'entries.addObject(entry); } '
                    'pb.writeObjects(entries); pb.pasteboardItems.count;')
    assert result.stdout.strip() == '2'
    paste()
    assert arguments(written) == paths
    assert images == paths


def test_real_file_url_preserves_newlines_quotes_and_spaces_in_filename(pasteboard):
    widget, run_js, paste, written, images, path, image, project = pasteboard
    unusual = path.with_name('中文 \"quote\" and\nnewline.png')
    assert image.save(str(unusual), 'PNG')
    run_js('pb.clearContents; pb.setStringForType(' + json.dumps(unusual.as_uri())
           + ', "public.file-url");')
    paste()
    assert arguments(written) == [str(unusual)]
    assert images == [str(unusual)]


def test_real_raster_image_still_saves_and_pastes_with_space_in_project_path(pasteboard):
    widget, run_js, paste, written, images, path, image, project = pasteboard
    run_js('pb.clearContents; pb.setDataForType($.NSData.dataWithContentsOfFile('
           + json.dumps(str(path)) + '), "public.png");')
    paste()
    paths = arguments(written)
    assert len(paths) == 1 and images == paths
    assert paths[0].startswith(str(project / '.images'))
    saved = QImage(paths[0])
    assert not saved.isNull() and saved.size() == image.size()
