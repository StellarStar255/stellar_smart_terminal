"""Bastion initialization must observe real output and stay with its session."""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtWidgets import QApplication

from main_window_remote import RemotePanelMixin
from terminal_widget import TerminalWidget

_APP = None


@pytest.fixture
def terminal():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    widget = TerminalWidget()
    sent = []
    widget._backend = SimpleNamespace(is_running=True, stop=Mock(),
                                      write=lambda data: sent.append(bytes(data)) or True)
    widget._process_start_token = object()
    try:
        yield widget, sent
    finally:
        widget.cleanup()
        widget.deleteLater()


def test_bastion_initialization_observes_output_without_api_server(terminal):
    widget, sent = terminal
    assert not widget._api_output_enabled
    callbacks = []
    with patch('main_window_remote.QTimer.singleShot',
               side_effect=lambda delay, cb: callbacks.append(cb)):
        RemotePanelMixin._send_bastion_boot_line(None, widget, 'export X=1', settle_ms=0)
        assert not sent and not callbacks
        widget._process_output_text('Remote shell ready\r\n')
        assert len(callbacks) == 1
        callbacks.pop()()
        widget._process_output_text('More output\r\n')
    assert not widget._api_output_enabled
    assert sent == [b'export X=1\n']
    assert not callbacks


@pytest.mark.parametrize('replace_before_output', [False, True])
def test_bastion_initialization_cannot_type_into_replacement_session(terminal, replace_before_output):
    widget, sent = terminal
    widget._api_output_enabled = True  # Exercise the old listener as well.
    callbacks = []
    with patch('main_window_remote.QTimer.singleShot',
               side_effect=lambda delay, cb: callbacks.append(cb)):
        RemotePanelMixin._send_bastion_boot_line(None, widget, 'cd /old/session', settle_ms=0)
        if replace_before_output:
            widget._process_start_token = object()
        widget._process_output_text('Ready\r\n')
        if not replace_before_output:
            widget._process_start_token = object()
        for callback in callbacks:
            callback()
    assert sent == []


@pytest.mark.parametrize('after_output', [False, True])
def test_session_exit_cancels_bastion_initialization_and_disconnects_observers(terminal, after_output):
    widget, sent = terminal
    listeners = widget.receivers(widget._output_activity)
    callbacks = []
    with patch('main_window_remote.QTimer.singleShot',
               side_effect=lambda delay, cb: callbacks.append(cb)):
        RemotePanelMixin._send_bastion_boot_line(None, widget, 'cd /remote', settle_ms=0)
        if after_output:
            widget._process_output_text('Ready\r\n')
        widget._backend.is_running = False
        widget._on_process_finished(0)
        for callback in callbacks:
            callback()
    assert sent == []
    assert widget.receivers(widget._output_activity) == listeners
    assert widget.receivers(widget.session_ended) == 0
