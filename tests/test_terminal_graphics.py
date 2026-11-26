"""Codex-style Kitty PNG uploads, placement, painting and cleanup."""

import base64
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from terminal_graphics import Placement
from terminal_widget import TerminalWidget


_APP = None


@pytest.fixture
def terminal():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    yield widget
    widget.cleanup()
    widget.deleteLater()
    _APP.processEvents()


def command(controls, payload=''):
    return f'\x1b_G{controls};{payload}\x1b\\'


def red_png():
    image = QImage(4, 8, QImage.Format.Format_RGB32)
    image.fill(0xffff0000)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    return base64.b64encode(bytes(buffer.data())).decode('ascii')


def test_chunked_upload_uses_cursor_and_paints_image(terminal):
    payload = red_png()
    first = '\x1b[4;3H' + command('a=T,t=d,f=100,c=2,r=3,i=7,q=2,C=1,m=1', payload[:32])
    terminal._process_output_text(first[:-1])
    terminal._process_output_text(first[-1:])
    assert not terminal.screen.graphics.placements
    terminal._process_output_text(command('m=0', payload[32:]) + 'x')
    assert terminal.screen.graphics.placements == {7: Placement(2, 3, 2, 3)}
    assert terminal.screen.buffer[3][2].data == 'x'

    frame = QImage(300, 300, QImage.Format.Format_RGB32)
    frame.fill(0)
    painter = QPainter(frame)
    terminal._draw_terminal_graphics(painter)
    painter.end()
    x, _ = terminal._col_tile(2)
    y, _ = terminal._row_tile(3)
    assert frame.pixelColor(x + 1, y + 1).name() == '#ff0000'
    assert frame.pixelColor(x - 1, y + 1).name() == '#000000'


def test_image_only_output_schedules_frame_without_text_activity(terminal):
    packets = []
    terminal._output_activity.connect(lambda *args: packets.append(args))
    terminal._process_output_text(command('a=T,f=100,i=9,c=2,r=2', red_png()))
    assert terminal.refresh_timer.isActive()
    assert not terminal._output_buffer_timer.isActive()
    assert not packets
    assert 9 in terminal.screen.graphics.placements


def test_resize_reuse_delete_and_screen_switch(terminal):
    terminal._process_output_text(command('a=T,f=100,i=9,c=2,r=2', red_png()))
    graphics = terminal.screen.graphics
    terminal.screen.resize(columns=terminal.screen.columns + 1)
    assert not graphics.placements and 9 in graphics.images
    terminal._process_output_text(command('a=d,d=i,i=9') + '\x1b[5;2H'
                                  + command('a=p,i=9,c=4,r=3,C=1'))
    assert graphics.placements == {9: Placement(1, 4, 4, 3)}
    terminal._process_output_text(command('a=d,d=I,i=9'))
    assert not graphics.placements and not graphics.images

    terminal._process_output_text('\x1b[?1049h' + command('a=T,f=100,i=1,c=1,r=1', red_png())
                                  + '\x1b[?1049l')
    assert not graphics.placements and not graphics.images


def test_ignores_invalid_data_and_file_transmission(terminal):
    controls = 'a=T,f=100,i=1,c=2,r=2'
    terminal._process_output_text(command(controls, '%%%') + command(controls + ',t=f', red_png()))
    assert not terminal.screen.graphics.images
