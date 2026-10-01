"""Capacity guards must apply before unbounded allocation or request fan-out."""
import subprocess
import sys
import threading
import time
import tracemalloc
from unittest.mock import patch

import pytest
from PyQt6.QtWidgets import QApplication, QPlainTextEdit
from PyQt6.QtGui import QTextCursor

from ai_completion import InlineCompletionController
from file_editor import _read_editor_bytes, _EditorFileTooLargeError
from git_manager import GitManager
from session_manager import SessionEntry
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


def wait_until(app, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Background operation did not complete'
        app.processEvents()
        time.sleep(.001)


@pytest.mark.parametrize('route', ['constructor', 'setter', 'restore'])
def test_entry_first_assignment_obeys_cap(monkeypatch, route):
    monkeypatch.setattr(SessionEntry, 'MAX_CONTENT_CHARS', 10)
    if route == 'constructor':
        entry = SessionEntry(type='output', content='old' * 10 + 'latest')
    elif route == 'setter':
        entry = SessionEntry(type='output')
        entry.content = 'old' * 10 + 'latest'
    else:
        entry = SessionEntry.from_dict({'type':'output', 'content':'old' * 10 + 'latest'})
    assert entry.content_length == 10
    assert entry.content == SessionEntry.TRUNCATION_MARK + ('old' * 10 + 'latest')[-10:]


def test_terminal_recording_keeps_recent_data_and_reports_truncation(app):
    terminal = TerminalWidget()
    terminal._OUTPUT_BUFFER_MAX_CHARS = 10
    collected = []
    terminal.output_recorded.connect(collected.append)
    try:
        terminal._buffer_output('abcd')
        terminal._buffer_output('efgh')
        terminal._buffer_output('ijkl')
        assert terminal._output_buffer_chars == 10
        assert ''.join(terminal._output_buffer) == 'cdefghijkl'
        terminal._flush_output_buffer()
        assert collected[0].endswith('\ncdefghijkl')
        assert terminal._output_buffer_chars == 0
        terminal._buffer_output('next')
        terminal._flush_output_buffer()
        assert collected[1] == 'next'
    finally:
        terminal.cleanup()
        terminal.deleteLater()


def test_terminal_one_huge_chunk_is_capped_before_retention(app):
    terminal = TerminalWidget()
    terminal._OUTPUT_BUFFER_MAX_CHARS = 32
    try:
        terminal._buffer_output('x' * 100_000 + 'newest')
        assert sum(map(len, terminal._output_buffer)) == 32
        assert ''.join(terminal._output_buffer).endswith('newest')
        assert terminal._output_buffer_truncated
    finally:
        terminal.cleanup()
        terminal.deleteLater()


def test_editor_reads_no_more_than_limit_plus_one(tmp_path, monkeypatch):
    monkeypatch.setattr('file_editor._MAX_TEXT_FILE_BYTES', 16)
    path = tmp_path / 'growing.txt'
    path.write_bytes(b'x' * 32)
    requested = []
    class Reader:
        def __init__(self): self.stream = open(path, 'rb')
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            requested.append(size)
            return self.stream.read(size)
    with patch('file_editor.open', return_value=Reader(), create=True):
        with pytest.raises(_EditorFileTooLargeError):
            _read_editor_bytes(str(path))
    assert requested == [17]


def test_ai_blocked_headers_coalesce_requests_and_use_latest_context(app):
    editor = QPlainTextEdit()
    editor.setPlainText('initial ')
    editor.hasFocus = lambda: True
    editor.ai_get_config = lambda: {'api_key':'test-key', 'api_base':'https://test.invalid/v1'}
    controller = InlineCompletionController(editor)
    controller.set_enabled(True)
    gate, entered = threading.Event(), threading.Event()
    workers = []
    import ai_completion
    original_worker = ai_completion.CompletionWorker
    def create_worker(*args):
        worker = original_worker(*args)
        workers.append(worker)
        return worker
    class Response:
        status_code = 200
        def close(self): pass
        def iter_lines(self, **kwargs): return iter(())
    def post(*args, **kwargs):
        entered.set()
        assert gate.wait(5)
        return Response()
    def at_end():
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        editor.setTextCursor(cursor)
    try:
        with patch('requests.post', side_effect=post), patch('ai_completion.CompletionWorker', side_effect=create_worker):
            at_end()
            controller.request_now()
            assert entered.wait(2)
            for i in range(10):
                editor.setPlainText(f'latest {i} ')
                at_end()
                controller.request_now()
            assert len(workers) == 1
            assert workers[0].isRunning() and workers[0]._cancelled
            gate.set()
            wait_until(app, lambda: len(workers) == 2)
            assert workers[1]._prefix == 'latest 9 '
            wait_until(app, lambda: not controller._workers)
    finally:
        gate.set()
        controller.shutdown()
        for worker in list(controller._workers):
            worker.wait(2000)
        app.processEvents()
        editor.deleteLater()


def test_ai_context_does_not_copy_entire_document(app):
    from PyQt6.QtCore import QObject, pyqtSignal
    editor = QPlainTextEdit()
    text = ('😀 line\n' * 3000) + '😀 end '
    editor.setPlainText(text)
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    editor.hasFocus = lambda: True
    editor.ai_get_config = lambda: {'api_key':'test-key'}
    controller = InlineCompletionController(editor)
    controller.set_enabled(True)
    created = []
    class Worker(QObject):
        chunk = pyqtSignal(int, str)
        done = pyqtSignal(int, str)
        failed = pyqtSignal(int, str)
        finished = pyqtSignal()
        def __init__(self, cfg, prefix, suffix, language, generation):
            super().__init__()
            created.append((prefix, suffix))
        def start(self): pass
        def cancel(self): pass
        def isRunning(self): return False
    try:
        with patch.object(editor, 'toPlainText', side_effect=AssertionError('full document copied')):
            with patch('ai_completion.CompletionWorker', Worker):
                controller.request_now()
        assert created == [(text[-8000:], '')]
        controller._workers[0].finished.emit()
    finally:
        controller.shutdown()
        editor.deleteLater()


def test_git_patch_pipes_are_drained_with_bounded_memory(app, monkeypatch, tmp_path):
    monkeypatch.setattr('git_manager.MAX_DIFF_CHARS', 4096)
    manager = GitManager()
    manager._repo_path = str(tmp_path)
    procs = []
    def spawn(args, stdin=None):
        proc = subprocess.Popen([sys.executable, '-c',
            "import sys\nfor i in range(128):\n sys.stdout.write('行' * 65536)\n sys.stderr.write('错' * 65536)\n"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding='utf-8', start_new_session=True)
        procs.append(proc)
        manager._active_procs.add(proc)
        return proc
    monkeypatch.setattr(manager, '_spawn_git', spawn)
    tracemalloc.start()
    try:
        output = manager.get_diff('fake.txt')
        _, peak = tracemalloc.get_traced_memory()
        assert output.startswith('行' * 4096)
        assert 4096 < len(output) < 4300
        assert peak < 4 * 1024 * 1024
        assert procs[0].returncode == 0
        assert not manager._active_procs
    finally:
        tracemalloc.stop()
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        manager.deleteLater()


def test_git_bounded_collection_times_out_and_reaps_child(app, monkeypatch, tmp_path):
    manager = GitManager()
    manager._repo_path = str(tmp_path)
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    manager._active_procs.add(proc)
    monkeypatch.setattr(manager, '_spawn_git', lambda *args, **kwargs: proc)
    try:
        ok, _ = manager._run_git('diff', '--', 'fake.txt', timeout=.05)
        assert not ok
        assert proc.poll() is not None
        assert not manager._active_procs
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        manager.deleteLater()
