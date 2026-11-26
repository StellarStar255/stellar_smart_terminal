"""Real PTY output + Qt timer + Codex-compatible viewport hit testing."""
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication
from terminal_widget import TerminalWidget

_APP = None


@pytest.mark.skipif(os.name != 'nt', reason='requires the Windows ConPTY API')
def test_conpty_retries_unsupported_passthrough(tmp_path, monkeypatch):
    import ctypes
    from terminal_backend import WindowsBackend, kernel32, PSEUDOCONSOLE_PASSTHROUGH

    create = kernel32.CreatePseudoConsole
    # Exercise ctypes' actual return conversion, including the FAILED bit.
    stub = ctypes.WINFUNCTYPE(ctypes.c_uint32)(lambda: 0x80070057)
    reject = ctypes.cast(stub, ctypes.WINFUNCTYPE(create.restype))
    flags_seen = []

    def create_console(size, reader, writer, flags, result):
        flags_seen.append(flags)
        if flags:
            return reject()
        return create(size, reader, writer, flags, result)

    monkeypatch.setattr(kernel32, 'CreatePseudoConsole', create_console)
    backend = WindowsBackend()
    marker = tmp_path / 'started'
    try:
        assert backend.start([sys.executable, '-c', "open('started','w').close()"], cwd=str(tmp_path))
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        assert flags_seen == [PSEUDOCONSOLE_PASSTHROUGH, 0]
    finally:
        backend.stop()


@pytest.mark.parametrize(('start_row', 'going_up', 'reverse', 'delayed_down'), [
    (0, False, False, False), (4, False, False, False), (4, True, False, False),
    (4, False, True, False), (4, False, False, True)])
def test_timer_drag_across_real_pty_pages(tmp_path, start_row, going_up, reverse, delayed_down):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    app = _APP
    widget = TerminalWidget()
    widget.resize(640, 420)
    widget.show()
    app.processEvents()
    widget.flush_resize()
    report = tmp_path / 'wheel.json'
    fixture = Path(__file__).parent / 'fixtures/transcript_mouse_viewer.py'

    def wait_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        pytest.fail(f'PTY/timer did not reach expected state ({predicate.__name__})')

    def pos(row, col):
        return QPoint(int(widget.PADDING + (col + .3) * widget.char_width),
                      int(widget.PADDING + widget._header_h + (row + .5) * widget.char_height))

    def mouse(kind, point, buttons):
        app.sendEvent(widget, QMouseEvent(kind, QPointF(point), Qt.MouseButton.LeftButton,
                                         buttons, Qt.KeyboardModifier.NoModifier))

    try:
        # The viewer needs only the standard library. Start Python directly
        # so the Windows venv redirector does not interpose another process.
        interpreter = sys._base_executable if os.name == 'nt' else sys.executable
        command = [interpreter, '-u', str(fixture), str(report)]
        if delayed_down:
            command.append('--delay-down-frame')
        late_frame = going_up and not reverse
        if late_frame:
            command.append('--delay-last-frame')
        widget.start_process(command, cwd=str(tmp_path))
        wait_until(lambda: widget.screen._in_alt_screen and widget._mouse_mode
                   and 'row 0104' in widget._get_line_text(4))
        widget.set_mouse_click_forward_enabled(start_row == 4)
        if delayed_down:
            from scripts.installed_drag_visual import band_moved

            def column():
                image = widget.grab().toImage()
                x = int(pos(0, 30).x() * image.devicePixelRatio())
                return [image.pixelColor(x, y).rgb() for y in range(image.height())]

            baseline = column()
        mouse(QEvent.Type.MouseButtonPress, pos(start_row, 0), Qt.MouseButton.LeftButton)
        end_row = widget.term_rows - 4 if start_row == 4 else widget.term_rows + 3
        end = pos(0 if going_up else end_row, 2)
        direction = -1 if going_up else 1
        mouse(QEvent.Type.MouseMove, end,
              Qt.MouseButton.LeftButton)
        wait_until(lambda: widget._tui_selection is not None
                   and direction * widget._tui_selection['origin'] >= 2 * widget.term_rows)
        if reverse:
            # Keep the button held, and scroll back past the original anchor.
            going_up = True
            end = pos(0, 2)
            mouse(QEvent.Type.MouseMove, end, Qt.MouseButton.LeftButton)
            wait_until(lambda: widget._tui_selection['origin'] <= -2 * widget.term_rows)
        if late_frame:
            wait_until(report.with_suffix('.pending').exists)
            before = widget.grab().toImage()
            sample = pos(6, 30) * before.devicePixelRatio()
            highlighted = before.pixelColor(sample)
        if delayed_down:
            wait_until(report.with_suffix('.pending').exists)
            end = pos(widget.term_rows // 2, 35)
            mouse(QEvent.Type.MouseMove, end, Qt.MouseButton.LeftButton)
            selected = column()
            sample = int(end.y() * widget.devicePixelRatioF())
            color = selected[sample]
            assert color != baseline[sample]
            edge = next(y for y in range(sample, len(selected)) if selected[y] != color) - 1
        mouse(QEvent.Type.MouseButtonRelease,
              end, Qt.MouseButton.NoButton)
        origin = widget._tui_selection['origin']
        expected = '\n'.join(f'row {i:04d} 中文 sample'
                             for i in range(100 + max(1, start_row),
                                            100 + origin + widget.term_rows - 5))
        if delayed_down:
            # Release the pending three-row scroll after freezing the endpoint.
            end_line = 100 + origin + widget.term_rows // 2
            expected = '\n'.join(f'row {i:04d} 中文 sample'
                                 for i in range(100 + start_row, end_line + 1))
            report.with_suffix('.release').touch()
            wait_until(lambda: band_moved((edge, color), baseline, column()))
        if going_up:
            # The pinned header lies above the transcript. Dragging over it
            # includes the first visible row from column zero to the anchor.
            complete_rows = [f'row {i:04d} 中文 sample'
                             for i in range(101 + origin, 100 + start_row)]
            expected = '\n'.join([*complete_rows, 'r'])
        if late_frame:
            assert len(expected.splitlines()) > widget.term_rows
            report.with_suffix('.release').touch()
            # Observe the child process's repaint in the terminal buffer.
            # The selection state itself must not be used to detect arrival.
            wait_until(lambda: f'row {100 + origin - 3 + 1:04d}' in widget._get_line_text(1))
            def highlight_has_moved():
                after = widget.grab().toImage()
                ratio = after.devicePixelRatio()
                return (all(after.pixelColor(pos(row, 30) * ratio) == highlighted for row in (5, 6, 9))
                        and after.pixelColor(pos(2, 30) * ratio) != highlighted)

            # Reading the PTY and painting its next Qt frame are asynchronous.
            wait_until(highlight_has_moved)
        assert widget._get_selected_text() == expected
        widget._copy_selection_to_clipboard()
        assert app.clipboard().text() == expected
        state = json.loads(report.read_text())
        assert state['accepted'] and all(2 <= row <= widget.term_rows - 5
                                          for row in state['accepted'])
        if start_row == 4:
            # A delayed first frame can trigger a probe before the transcript
            # bounds are known. Subsequent wheels keep the confirmed position.
            assert state['accepted'][0] == 5
            assert len(set(state['accepted'][-3:])) == 1
        assert widget._get_history_count() == 0  # no accidental main-screen history fallback
    finally:
        widget.cleanup()
        widget.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
