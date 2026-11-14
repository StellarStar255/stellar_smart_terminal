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


@pytest.mark.skipif(os.name == 'nt', reason='POSIX PTY fixture')
@pytest.mark.parametrize('start_row', [0, 4])
def test_timer_drag_across_real_pty_pages(tmp_path, start_row):
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
        pytest.fail('PTY/timer did not reach expected state')

    def pos(row, col):
        return QPoint(int(widget.PADDING + (col + .3) * widget.char_width),
                      int(widget.PADDING + widget._header_h + (row + .5) * widget.char_height))

    def mouse(kind, point, buttons):
        app.sendEvent(widget, QMouseEvent(kind, QPointF(point), Qt.MouseButton.LeftButton,
                                         buttons, Qt.KeyboardModifier.NoModifier))

    try:
        widget.start_process([sys.executable, '-u', str(fixture), str(report)], cwd=str(tmp_path))
        wait_until(lambda: widget.screen._in_alt_screen and widget._mouse_mode
                   and 'row 0104' in widget._get_line_text(4))
        mouse(QEvent.Type.MouseButtonPress, pos(start_row, 0), Qt.MouseButton.LeftButton)
        mouse(QEvent.Type.MouseMove, pos(widget.term_rows + 3, widget.term_cols - 1),
              Qt.MouseButton.LeftButton)
        wait_until(lambda: widget._tui_selection is not None
                   and widget._tui_selection['origin'] >= 2 * widget.term_rows)
        mouse(QEvent.Type.MouseButtonRelease,
              pos(widget.term_rows + 3, widget.term_cols - 1), Qt.MouseButton.NoButton)
        origin = widget._tui_selection['origin']
        expected = '\n'.join(f'row {i:04d} 中文 sample'
                             for i in range(100 + max(1, start_row),
                                            100 + origin + widget.term_rows - 5))
        assert widget._get_selected_text() == expected
        state = json.loads(report.read_text())
        assert state['accepted'] and all(2 <= row <= widget.term_rows - 5
                                          for row in state['accepted'])
        if start_row == 4:
            assert all(row == 5 for row in state['accepted'])
        assert widget._get_history_count() == 0  # no accidental main-screen history fallback
    finally:
        widget.cleanup()
        widget.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
