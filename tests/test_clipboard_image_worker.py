"""Image encoding and disk I/O must not block input, or outlive its terminal."""
import os
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtGui import QImage, QColor
from PyQt6.QtWidgets import QApplication
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def image_terminal(tmp_path):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    written = []
    widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True, stop=lambda: None)
    widget.image_save_local = True
    widget.get_cwd = lambda: str(tmp_path)
    widget._clipboard_change_token = lambda: 7
    image = QImage(3, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor('green'))
    clipboard = SimpleNamespace(
        mimeData=lambda: SimpleNamespace(hasUrls=lambda: False, hasImage=lambda: True),
        image=lambda: image)
    yield widget, written, clipboard
    widget.cleanup()
    widget.deleteLater()
    _APP.processEvents()


def pump_until(done):
    deadline = time.monotonic() + 2
    while not done():
        assert time.monotonic() < deadline
        _APP.processEvents()
        time.sleep(.005)


def test_encoding_and_io_run_off_gui_and_text_paste_keeps_order(image_terminal, tmp_path):
    widget, written, clipboard = image_terminal
    entered, release = threading.Event(), threading.Event()
    workers = []
    real_save = QImage.save

    def save(image, *args):
        workers.append(threading.get_ident())
        entered.set()
        release.wait(1)
        return real_save(image, *args)

    try:
        with patch.object(QImage, 'save', new=save):
            start = time.monotonic()
            widget._paste_from_clipboard_qt(clipboard)
            assert time.monotonic() - start < .2
            assert entered.wait(1)
            plain = SimpleNamespace(
                mimeData=lambda: SimpleNamespace(hasUrls=lambda: False, hasImage=lambda: False),
                text=lambda: ' tail')
            widget._paste_from_clipboard_qt(plain)
            widget.send_text('\r')
            assert written == []
            release.set()
            pump_until(lambda: len(written) == 3)
        assert len(workers) == 1 and workers[0] != threading.get_ident()
        assert written[1:] == [b' tail', b'\r']
        path = next((tmp_path / '.images').glob('*.png'))
        assert written[0] == widget._quote_pasted_path(str(path)).encode()
        saved = QImage(str(path))
        assert not saved.isNull() and saved.size() == clipboard.image().size()
    finally:
        release.set()


@pytest.mark.parametrize('outcome', ['interrupt', 'cleanup', 'replacement', 'failure'])
def test_cancel_replacement_or_failure_never_inserts_image_or_enter(image_terminal, outcome, tmp_path):
    widget, written, clipboard = image_terminal
    entered, release = threading.Event(), threading.Event()
    real_save = QImage.save

    def save(image, *args):
        entered.set()
        release.wait(1)
        return False if outcome == 'failure' else real_save(image, *args)

    try:
        with patch.object(QImage, 'save', new=save):
            widget._paste_from_clipboard_qt(clipboard)
            assert entered.wait(1)
            request = next(iter(widget._pending_image_saves))
            future = request.future
            widget.send_text('\r')
            if outcome == 'interrupt':
                widget.send_text('\x03')
            elif outcome == 'cleanup':
                widget.cleanup()
            elif outcome == 'replacement':
                widget._backend = SimpleNamespace(write=lambda data: written.append(data) or True,
                                                  stop=lambda: None)
                widget.send_text('fresh')
            release.set()
            future.result(timeout=2)
            pump_until(lambda: not widget._pending_image_saves and not widget._native_paste_queue)
        expected = [b'\x03'] if outcome == 'interrupt' else [b'fresh'] if outcome == 'replacement' else []
        assert written == expected
        pump_until(lambda: not list((tmp_path / '.images').glob('*.png')))
    finally:
        release.set()


def test_rejected_image_job_does_not_leave_input_queue_blocked(image_terminal):
    widget, written, clipboard = image_terminal
    executor = SimpleNamespace(submit=Mock(side_effect=RuntimeError('executor closed')))
    with patch.object(widget, '_get_image_save_executor', return_value=executor):
        widget._paste_from_clipboard_qt(clipboard)
    assert widget._pending_image_saves == {}
    assert not widget._native_paste_queue
    widget.send_text('fresh')
    assert written == [b'fresh']


def test_cancelled_image_future_does_not_leave_input_queue_blocked(image_terminal):
    widget, written, clipboard = image_terminal
    future = Future()
    executor = SimpleNamespace(submit=lambda *args: future)
    with patch.object(widget, '_get_image_save_executor', return_value=executor):
        widget._paste_from_clipboard_qt(clipboard)
        widget.send_text('\r')
        future.cancel()
        _APP.processEvents()
    assert widget._pending_image_saves == {}
    assert not widget._native_paste_queue
    widget.send_text('fresh')
    assert written == [b'fresh']
