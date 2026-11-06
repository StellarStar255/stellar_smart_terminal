"""默认不录制；手动录制仅采集目标终端；导出当前解析文本而非旧历史。"""
import os
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication
from main_window import MainWindow
from history_dialog import HistoryDialog

_app = None


@pytest.fixture
def window():
    global _app
    _app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.show()
    _app.processEvents()
    yield w
    w._force_closing = True
    w.close()
    w.deleteLater()
    for _ in range(3):
        _app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        _app.processEvents()


def begin(w):
    term = w.terminal
    with patch.object(term, 'is_running', return_value=True):
        w._toggle_terminal_recording()
    assert w._recording_terminal is term
    return term


def test_starting_terminal_does_not_record_or_save(window):
    w = window
    term = w.terminal
    w.presets = [{'name': 'test', 'commands': ['/bin/zsh']}]
    w.preset_combo.clear()
    w.preset_combo.addItem('test')
    with patch.object(term, 'start_process'), patch.object(w.session_manager, 'create_session') as create:
        w._start_session()
        term.input_recorded.emit('ignored input')
        term.output_recorded.emit('ignored output')
        w._auto_save()
        create.assert_not_called()
    assert w.session_manager.current_session is None
    assert not w.auto_save_timer.isActive()


def test_record_only_target_and_flush_tail_when_stopping(window):
    w = window
    a = w.terminal
    a._buffer_output('before start')
    begin(w)
    session = w.session_manager.current_session
    a.input_recorded.emit('wanted input')
    a.output_recorded.emit('wanted output')
    w._add_new_tab()
    b = w.terminal
    b.input_recorded.emit('other input')
    b.output_recorded.emit('other output')
    a._buffer_output('final tail')
    w._auto_save()  # Changing tabs must not interrupt saving the target.
    w._toggle_terminal_recording()  # Stop from the other tab.
    w.session_manager.flush()
    saved = w.session_manager.load_session(session.session_id)
    content = '\n'.join(e.content for e in saved.entries)
    assert 'wanted input' in content and 'wanted output' in content and 'final tail' in content
    assert 'before start' not in content
    assert 'other input' not in content and 'other output' not in content
    assert w._recording_terminal is None
    assert not w.auto_save_timer.isActive()


def test_process_end_stops_target_recording(window):
    term = begin(window)
    session = window.session_manager.current_session
    term.output_recorded.emit('tail')
    window._on_terminal_ended(term)
    assert window._recording_terminal is None
    assert session.end_time is not None
    assert window.session_manager.current_session is None


def test_unwiring_target_saves_before_signal_disconnect(window):
    term = begin(window)
    session = window.session_manager.current_session
    term._buffer_output('before transfer')
    window._unwire_terminal_signals(term)
    window.session_manager.flush()
    saved = window.session_manager.load_session(session.session_id)
    assert saved.entries[-1].content == 'before transfer'
    assert window._recording_terminal is None


def test_export_uses_current_parsed_screen_without_history_write(window):
    w = window
    a = w.terminal
    a.stream.feed('obsolete\rreplacement\x1b[K\r\ncurrent text')
    with patch.object(w.session_manager, 'list_sessions', side_effect=AssertionError('old history fallback')), \
            patch.object(w.session_manager, 'create_session', side_effect=AssertionError('snapshot must not start recording')):
        session = w._session_for_export()
    text = session.entries[0].content
    assert 'replacement' in text and 'current text' in text
    assert 'obsolete' not in text and '\x1b' not in text
    assert w.session_manager.current_session is None
    w._add_new_tab()
    assert w._session_for_export() is None


def test_empty_current_terminal_does_not_export_old_history(window):
    with patch.object(window.session_manager, 'list_sessions', side_effect=AssertionError('old history fallback')), \
            patch('main_window.QMessageBox.information'), \
            patch('main_window.export_session') as export:
        window._quick_export()
        window._show_export_dialog()
        export.assert_not_called()


def test_history_can_still_browse_old_records_and_offer_manual_controls(window):
    manager = window.session_manager
    old = manager.create_session('older recording')
    manager.add_output('old content')
    manager.end_session()
    manager.flush()
    assert any(s['session_id'] == old.session_id for s in manager.list_sessions())
    dialog = HistoryDialog(manager, window)
    try:
        assert dialog.recording_btn.text()
        dialog.set_recording(True)
        assert dialog.recording_status.text()
        assert window._recording_terminal is None
    finally:
        dialog.close()
        dialog.deleteLater()


def test_stopping_another_tab_keeps_recording_and_saving_target(window):
    w = window
    a = begin(w)
    w._add_new_tab()
    b = w.terminal
    with patch.object(a, 'is_running', return_value=True), \
            patch.object(b, 'stop_process'), patch.object(w, '_close_current_tab'):
        w._stop_session()
    assert w._recording_terminal is a
    assert w.auto_save_timer.isActive()
    assert w.session_manager.current_session is not None


def test_closing_recorded_split_saves_tail_and_stops_recording(window):
    w = window
    idx = w.tab_widget.currentIndex()
    target = w.terminal
    sibling = w._create_terminal()
    w.tab_splitters[idx].addWidget(sibling)
    w.tab_terminals[idx].append(sibling)
    begin(w)
    session = w.session_manager.current_session
    target._buffer_output('split tail')
    w._close_current_split()
    w.session_manager.flush()
    saved = w.session_manager.load_session(session.session_id)
    assert saved.entries[-1].content == 'split tail'
    assert w._recording_terminal is None
    assert target not in w.tab_terminals[idx]


def test_stopping_process_includes_output_arriving_during_stop(window):
    w = window
    target = begin(w)
    session = w.session_manager.current_session
    with patch.object(target, 'stop_process', side_effect=lambda: target._buffer_output('stop tail')), \
            patch.object(w, '_close_current_tab'):
        w._stop_session()
    w.session_manager.flush()
    saved = w.session_manager.load_session(session.session_id)
    assert saved.entries[-1].content == 'stop tail'
