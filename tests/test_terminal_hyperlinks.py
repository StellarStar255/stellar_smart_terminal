"""OSC 8 addresses follow terminal cells through writes, reflow and clicks."""
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from terminal_widget import TerminalWidget

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
_app = None


@pytest.fixture(scope='module')
def app():
    global _app
    _app = QApplication.instance() or QApplication([])
    return _app


@pytest.fixture
def terminal(app):
    widget = TerminalWidget()
    widget.resize(800, 400)
    widget.show()
    widget._update_terminal_size()
    app.processEvents()
    yield widget
    widget.cleanup()
    widget.close()
    widget.deleteLater()


def _point(widget, row, col):
    return QPoint(int(widget.PADDING + (col + .5) * widget.char_width),
                  int(widget.PADDING + widget._header_h + (row + .5) * widget.char_height))


@pytest.mark.parametrize('terminator', ['\a', '\x1b\\'])
def test_split_osc8_opens_chinese_label_and_keeps_address_out_of_output(
        terminal, monkeypatch, terminator):
    url = 'http://127.0.0.1:8873/?sample=1;mode=demo'
    opened = Mock()
    monkeypatch.setattr('terminal_mouse.QDesktopServices.openUrl', opened)
    bell = Mock()
    terminal.interaction_requested.connect(bell)
    data = (f'前 \x1b]8;id=sample;{url}{terminator}\x1b[34m样例\x1b[0m知识库'
            f'\x1b]8;;{terminator} 后').encode()
    for byte in data:
        terminal._on_output(bytes([byte]))
    assert ''.join(terminal._output_buffer) == '前 \x1b[34m样例\x1b[0m知识库 后'
    assert url not in ''.join(terminal._output_buffer)
    bell.assert_not_called()
    # 中文字符占两列，第二列和 SGR 重置之后的字符均指向同一个链接。
    for col in (3, 4, 11):
        pos = _point(terminal, 0, col)
        hover = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), Qt.MouseButton.NoButton,
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        terminal.mouseMoveEvent(hover)
        assert terminal.cursor().shape() == Qt.CursorShape.PointingHandCursor
        QTest.mouseClick(terminal, Qt.MouseButton.LeftButton, pos=pos)
        assert opened.call_args.args[0].toString() == url
    assert opened.call_count == 3
    QTest.mouseClick(terminal, Qt.MouseButton.LeftButton, pos=_point(terminal, 0, 13))
    assert opened.call_count == 3


def test_hyperlink_survives_scrollback_reflow_but_not_cell_overwrites(terminal):
    url = 'https://example.com/sample'
    terminal._process_output_text(f'\x1b]8;;{url}\a知识库\x1b]8;;\a')
    terminal._process_output_text('\rX')
    terminal._process_output_text('\r\nnewline' * (terminal.term_rows + 2))
    with terminal._screen_lock:
        terminal.screen.resize(columns=10)
    terminal.scroll_offset = terminal._get_history_count()
    assert terminal._get_url_at_pos((0, 0), include_plain=False) == ''
    assert terminal._get_url_at_pos((0, 1), include_plain=False) == ''
    assert terminal._get_url_at_pos((0, 2), include_plain=False) == url


def test_osc8_file_link_opens_in_editor(terminal, monkeypatch, tmp_path):
    path = tmp_path / '监控 #1.log'
    path.write_text('In Progress\n', encoding='utf-8')
    opened = Mock()
    terminal.file_link_requested.connect(opened)
    external = Mock()
    monkeypatch.setattr('terminal_mouse.QDesktopServices.openUrl', external)
    local = path.as_uri()
    addresses = [local, local.replace('file://', 'file://localhost', 1),
                 local.replace('file://', 'file://example.com', 1),
                 (tmp_path / 'missing.log').as_uri(), 'file:relative.log',
                 'file:///%2Fserver/share.log', 'custom:///tmp/test.log',
                 local + '#L2', local + '#L0', local + '#L2147483648',
                 local + '?query=1#L2', local + '#other']
    for row, address in enumerate(addresses):
        terminal._on_output(f'\x1b]8;;{address}\x1b\\监控日志\x1b]8;;\x1b\\\r\n'.encode())
        QTest.mouseClick(terminal, Qt.MouseButton.LeftButton, pos=_point(terminal, row, 1))
    assert [(Path(p), line) for (p, line), _ in opened.call_args_list] == [
        (path, 0), (path, 0), (tmp_path / 'missing.log', 0), (path, 2)]
    external.assert_not_called()


def test_drag_select_and_unsafe_addresses_do_not_open(terminal, monkeypatch):
    opened = Mock()
    sent = Mock()
    monkeypatch.setattr('terminal_mouse.QDesktopServices.openUrl', opened)
    monkeypatch.setattr(terminal, '_send_mouse_event', sent)
    terminal._mouse_mode = True
    terminal._process_output_text(
        '\x1b]8;;https://example.com\aLink text\x1b]8;;\a '
        '\x1b]8;;file:///tmp/untrusted\aLocal file\x1b]8;;\a https://example.com/visible')
    left = Qt.MouseButton.LeftButton
    start, end = _point(terminal, 0, 1), _point(terminal, 0, 8)
    QTest.mouseClick(terminal, left, pos=start)
    opened.assert_called_once()
    sent.assert_not_called()
    opened.reset_mock()
    QTest.mousePress(terminal, left, pos=start)
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(end), Qt.MouseButton.NoButton,
                       left, Qt.KeyboardModifier.NoModifier)
    terminal.mouseMoveEvent(move)
    QTest.mouseRelease(terminal, left, pos=end)
    assert terminal._get_selected_text() == 'ink text'
    QTest.mouseClick(terminal, left, pos=_point(terminal, 0, 12))
    visible = _point(terminal, 0, 22)
    QTest.mouseClick(terminal, left, pos=visible)
    opened.assert_not_called()
    QTest.mouseClick(terminal, left, Qt.KeyboardModifier.AltModifier, visible)
    assert opened.call_args.args[0].toString() == 'https://example.com/visible'


def test_file_click_reaches_editor_and_reports_missing_file(app, monkeypatch, tmp_path):
    from main_window import MainWindow

    window = MainWindow()
    window.show()
    app.processEvents()
    terminal = window.tab_terminals[0][0]
    warning = Mock()
    monkeypatch.setattr('file_editor.QMessageBox.warning', warning)
    path = tmp_path / '监控 #1.log'
    path.write_text('one\ntwo\nthree\n', encoding='utf-8')
    missing = tmp_path / 'missing.log'
    try:
        for row, address in enumerate([path.as_uri() + '#L3', missing.as_uri()]):
            terminal._on_output(f'\x1b]8;;{address}\a监控日志\x1b]8;;\a\r\n'.encode())
            QTest.mouseClick(terminal, Qt.MouseButton.LeftButton, pos=_point(terminal, row, 1))
        editor = window.editor_area._active.editor
        assert editor.toPlainText() == path.read_text(encoding='utf-8')
        assert editor.textCursor().blockNumber() == 2
        warning.assert_called_once()
        assert os.path.normcase(str(missing)) in os.path.normcase(warning.call_args.args[2])
    finally:
        window.close()
        window.deleteLater()


def test_link_replaced_between_press_and_release_consumes_click(terminal, monkeypatch):
    terminal._mouse_mode = True
    terminal.set_mouse_click_forward_enabled(True)
    opened, sent = Mock(), Mock()
    monkeypatch.setattr(terminal, '_open_url', opened)
    monkeypatch.setattr(terminal, '_send_mouse_event', sent)
    pos = _point(terminal, 0, 1)
    terminal._process_output_text('\x1b]8;;https://example.com/one\a日志\x1b]8;;\a')
    QTest.mousePress(terminal, Qt.MouseButton.LeftButton, pos=pos)
    terminal._process_output_text('\r\x1b]8;;https://example.com/two\a日志\x1b]8;;\a')
    QTest.mouseRelease(terminal, Qt.MouseButton.LeftButton, pos=pos)
    opened.assert_not_called()
    sent.assert_not_called()

def test_scroll_away_and_back_while_holding_link_does_not_open(terminal, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(terminal, '_open_url', opened)
    row = '\x1b]8;;https://example.com/log\a日志\x1b]8;;\a\r\n'
    terminal._process_output_text(row * (terminal.term_rows + 12))
    terminal.grab()
    pos = _point(terminal, 2, 0)
    left = Qt.MouseButton.LeftButton
    QTest.mousePress(terminal, left, pos=pos)
    for angle in (240, -240):
        terminal.wheelEvent(QWheelEvent(QPointF(pos), QPointF(pos), QPoint(), QPoint(0, angle),
                                        left, Qt.KeyboardModifier.NoModifier,
                                        Qt.ScrollPhase.NoScrollPhase, False))
    assert terminal.scroll_offset == 0
    QTest.mouseRelease(terminal, left, pos=pos)
    opened.assert_not_called()


def test_fractional_pointer_opens_only_the_link_under_its_pixel(terminal, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(terminal, '_open_url', opened)
    terminal.char_width = 9.6
    terminal._process_output_text('\x1b]8;;https://example.com/a\aA'
                                  '\x1b]8;;https://example.com/b\aB\x1b]8;;\a')
    boundary, _ = terminal._col_tile(1)
    y, height = terminal._row_tile(0)
    for x in (boundary - .25, boundary, boundary + .25):
        point = QPointF(x, y + height / 2)
        for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                              (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
            QApplication.sendEvent(terminal, QMouseEvent(kind, point, Qt.MouseButton.LeftButton,
                                                        buttons, Qt.KeyboardModifier.NoModifier))
    assert [call.args[0] for call in opened.call_args_list] == [
        'https://example.com/a', 'https://example.com/b', 'https://example.com/b']

def test_last_link_does_not_extend_into_right_padding(terminal):
    terminal.char_width = 9.625
    terminal.term_cols = terminal.screen.columns = 7
    terminal._process_output_text('\x1b[1;7H\x1b]8;;https://example.com/edge\aZ\x1b]8;;\a')
    end, _ = terminal._col_tile(7)
    top, height = terminal._row_tile(0)
    assert terminal._link_at_pointer(QPoint(end - 1, top + height // 2),
                                     Qt.KeyboardModifier.NoModifier) == 'https://example.com/edge'
    assert terminal._link_at_pointer(QPoint(end, top + height // 2),
                                     Qt.KeyboardModifier.NoModifier) == ''
