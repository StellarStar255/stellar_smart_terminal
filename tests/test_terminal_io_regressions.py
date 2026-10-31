"""Real PTY backpressure, escape framing, and prompt submission regressions."""
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtWidgets import QApplication
from openai_server import OpenAIRequestHandler
from terminal_sequences import TerminalSequenceBuffer
from terminal_writer import TerminalWriter
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def app():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.mark.skipif(os.name == 'nt', reason='Unix PTY')
def test_backpressure_returns_promptly_and_close_cancels_writer():
    import tty
    from terminal_backend import UnixBackend
    master, slave = os.openpty()
    tty.setraw(slave)
    backend = UnixBackend()
    backend._master_fd = master
    try:
        start = time.monotonic()
        assert backend.write(b'x' * 1024 * 1024)
        assert time.monotonic() - start < .2
        writer = backend._input_writer
        assert not writer.flush(.05)  # Child has not read anything.
        start = time.monotonic()
        backend.stop()
        assert time.monotonic() - start < .5
        assert not writer.thread.is_alive()
        assert not backend.write(b'late input')
    finally:
        backend.stop()
        os.close(slave)


def test_queue_bound_includes_inflight_data_and_preserves_input_order():
    gate, entered = threading.Event(), threading.Event()
    received = bytearray()
    def slow_write(chunk):
        entered.set()
        assert gate.wait(2)
        # Exercise partial writes as well as ordering across queued messages.
        part = chunk[:3]
        received.extend(part)
        return len(part)
    writer = TerminalWriter(slow_write, lambda: None)
    writer.MAX_PENDING_BYTES = 12
    try:
        assert writer.write(b'12345678')
        assert entered.wait(1)
        assert not writer.write(b'abcde')
        assert writer.write(b'abcd')
        gate.set()
        assert writer.flush()
        assert received == b'12345678abcd'
    finally:
        gate.set()
        writer.close()
    assert not writer.write(b'after close')


@pytest.mark.parametrize('sequence', ['\x1b]11;?\x07', '\x1b]10;?\x1b\\',
                                      '\x1b[>c', '\x1b[>0c', '\x1b[>0q'])
def test_terminal_queries_reply_once_at_every_split(app, sequence):
    for split in range(1, len(sequence)):
        widget = TerminalWidget()
        sent = []
        widget._backend = SimpleNamespace(write=lambda data: sent.append(data) or True)
        try:
            widget._process_output_text('before' + sequence[:split])
            widget._process_output_text(sequence[split:] + 'after')
            assert len(sent) == 1, (sequence, split, sent)
            assert 'beforeafter' in ''.join(widget.screen.display)
        finally:
            widget._backend = None
            widget.cleanup()
            widget.deleteLater()


def test_framer_keeps_ordinary_text_and_caps_unterminated_strings():
    framer = TerminalSequenceBuffer()
    assert framer.feed('plain text') == 'plain text'
    assert framer.feed('a\x1b]0;') == 'a'
    assert framer.feed('x' * 100_000) == ''
    assert len(framer._pending) <= framer.MAX_SEQUENCE_CHARS
    assert framer.feed('\x07tail') == 'tail'
    source = 'abc\x1b[31mred\x1b[0m\x1b(B tail'
    assert ''.join(framer.feed(char) for char in source) == source


def test_single_line_prompt_does_not_poll_screen_or_wait_five_seconds():
    event = Mock()
    event.is_set.return_value = False
    event.wait.return_value = False
    bridge = SimpleNamespace(shutdown_event=event, send_input=Mock(), get_screen_content=Mock())
    handler = SimpleNamespace(bridge=bridge)
    assert OpenAIRequestHandler._submit_input(handler, 'hello')
    assert [call.args[0] for call in bridge.send_input.call_args_list] == ['hello', '\r']
    bridge.get_screen_content.assert_not_called()
    event.wait.assert_called_once_with(.1)


def test_stop_during_paste_never_sends_enter():
    event = Mock()
    event.is_set.return_value = False
    event.wait.return_value = True
    bridge = SimpleNamespace(shutdown_event=event, send_input=Mock(), get_screen_content=lambda: '')
    assert not OpenAIRequestHandler._submit_input(SimpleNamespace(bridge=bridge), 'one\ntwo')
    assert [call.args[0] for call in bridge.send_input.call_args_list] == ['one\ntwo']


def test_windows_pipe_writer_cancels_inflight_io_before_releasing_handle(monkeypatch):
    import ctypes
    entered, cancelled = threading.Event(), threading.Event()
    released = []
    def duplicate(process, source, dest_process, output, *rest):
        output._obj.value = 101
        return True
    def blocked_write(handle, data, size, output, overlapped):
        entered.set()
        assert cancelled.wait(2)
        return False
    kernel = SimpleNamespace(
        GetCurrentProcess=Mock(return_value=1),
        DuplicateHandle=Mock(side_effect=duplicate),
        WriteFile=Mock(side_effect=blocked_write),
        CloseHandle=Mock(side_effect=lambda handle: released.append(getattr(handle, 'value', handle))),
        OpenThread=Mock(return_value=202),
        CancelSynchronousIo=Mock(side_effect=lambda _: cancelled.set()),
    )
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 995, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda code: OSError(code, 'cancelled'), raising=False)
    writer = TerminalWriter.for_windows_pipe(10)
    try:
        assert writer.write(b'input')
        assert entered.wait(1)
    finally:
        writer.close()
    assert cancelled.is_set()
    assert not writer.thread.is_alive()
    assert 101 in released  # Worker owns the duplicate until cancellation completes.
    assert 10 not in released  # Backend still owns its original handle.
