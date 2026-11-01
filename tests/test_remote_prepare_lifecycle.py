"""Recovery must keep Qt responsive and retain edits while editor tabs change."""
from concurrent.futures import Future
import hashlib
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtWidgets import QApplication

from file_editor import FileEditorWidget
from remote_file_sync import RemoteFileSync

_APP = None


def wait_for(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        assert time.monotonic() < deadline, 'Background operation did not finish'
        _APP.processEvents()
        time.sleep(.001)


@pytest.fixture
def setup(tmp_path):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    jobs = []
    def submit(fn, *args):
        future = Future()
        jobs.append((fn, args, future))
        return future
    session = SimpleNamespace(is_connected=lambda: True, submit=submit)
    with patch('remote_file_sync.get_data_dir', return_value=tmp_path):
        sync = RemoteFileSync(lambda *_: session)
    cache = tmp_path/'cached.txt'
    cache.write_bytes(b'cached original')
    yield sync, cache, session, jobs
    sync.shutdown()
    sync._capture_executor.shutdown(wait=True)
    sync._recovery_executor.shutdown(wait=True)
    sync.deleteLater()
    _APP.processEvents()


def test_slow_recovery_keeps_gui_responsive_and_coalesces_repeated_open(setup):
    sync, cache, session, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = sync._recover_file
    calls, opened, ticks = [], [], []
    def slow(*args):
        calls.append(args)
        entered.set()
        assert release.wait(3)
        return original(*args)
    try:
        with patch.object(sync, '_recover_file', side_effect=slow):
            for index in range(20):
                sync.prepare_async(str(cache), 'host', '/remote', session,
                                   lambda value=index: opened.append(value), request_key='same-editor')
            assert entered.wait(1)
            QTimer.singleShot(0, lambda: ticks.append(True))
            _APP.processEvents()
            assert ticks and not opened
            assert len(calls) == 1
            release.set()
            wait_for(lambda: opened)
            sync.prepare_async(str(cache), 'host', '/remote', session, lambda: opened.append('cached'))
        assert opened == [19, 'cached']
        assert len(calls) == 1
    finally:
        release.set()


def test_recovery_completes_before_callback_reads_cache(setup):
    sync, cache, session, _ = setup
    sync._write_snapshot(sync._pending_dir, str(cache), 'host', '/remote', b'recovered edits', 1,
                         hashlib.sha256(b'cached original').hexdigest())
    opened = []
    sync.prepare_async(str(cache), 'host', '/remote', session, lambda: opened.append(cache.read_bytes()))
    wait_for(lambda: opened)
    assert opened == [b'recovered edits']
    assert sync.pending(str(cache))


def test_shutdown_during_recovery_never_opens_editor_and_retains_snapshot(setup):
    sync, cache, session, _ = setup
    snapshot = sync._write_snapshot(sync._pending_dir, str(cache), 'host', '/remote', b'unsent', 1)
    entered, release = threading.Event(), threading.Event()
    original = sync._recover_file
    def slow(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    opened = []
    try:
        with patch.object(sync, '_recover_file', side_effect=slow):
            sync.prepare_async(str(cache), 'host', '/remote', session, lambda: opened.append(True))
            assert entered.wait(1)
            sync.shutdown()
            release.set()
            sync._capture_executor.shutdown(wait=True)
            sync._recovery_executor.shutdown(wait=True)
        _APP.processEvents()
        assert not opened
        assert not sync._preparations
        assert Path(snapshot).read_bytes() == b'unsent'
    finally:
        release.set()


def test_recovery_scan_failure_does_not_open_or_overwrite_cache(setup):
    sync, cache, session, _ = setup
    opened, failed = [], []
    sync.preparation_failed.connect(lambda *args: failed.append(args))
    with patch.object(sync, '_recover_file', side_effect=PermissionError('recovery directory denied')):
        sync.prepare_async(str(cache), 'host', '/remote', session, lambda: opened.append(True))
        wait_for(lambda: failed)
    assert not opened
    assert cache.read_bytes() == b'cached original'
    assert str(cache) not in sync._targets
    assert not sync._preparations


def bind(sync, pane, path, session):
    sync.register(str(path), 'host', str(path), session, recover=False)
    assert pane.open_file(str(path))
    sync.track_editor(pane, str(path))
    pane.set_remote_sync_state(str(path), 'synced')


def test_switching_tabs_keeps_sync_but_closing_inactive_tab_releases_it(setup, tmp_path):
    sync, cache, session, _ = setup
    other = tmp_path/'other.txt'
    other.write_text('other', encoding='utf-8')
    pane = FileEditorWidget()
    try:
        bind(sync, pane, cache, session)
        bind(sync, pane, other, session)
        assert sync._references == {str(cache): 1, str(other): 1}
        assert pane.open_file(str(cache))
        assert str(other) in sync._targets
        pane._on_file_tab_close_requested(pane._tab_paths().index(str(other)))
        assert str(other) not in sync._targets
        assert str(other) not in pane._remote_sync_states
        assert str(cache) in sync._targets
    finally:
        pane.deleteLater()


def test_multiple_panes_release_only_after_last_tab_closes(setup):
    sync, cache, session, _ = setup
    first, second = FileEditorWidget(), FileEditorWidget()
    try:
        bind(sync, first, cache, session)
        bind(sync, second, cache, session)
        first._close_editor()
        assert sync._references[str(cache)] == 1
        assert str(cache) in sync._targets
        second._close_editor()
        assert str(cache) not in sync._targets
        assert not sync._references
        assert not first._remote_sync_states and not second._remote_sync_states
    finally:
        first.deleteLater()
        second.deleteLater()


def test_closing_uploading_file_retains_edits_until_upload_succeeds(setup):
    sync, cache, session, jobs = setup
    pane = FileEditorWidget()
    try:
        bind(sync, pane, cache, session)
        sync.save(str(cache), b'new saved bytes')
        wait_for(lambda: jobs)
        snapshot = sync._latest[str(cache)]
        pane._close_editor()
        assert str(cache) in sync._targets
        assert Path(snapshot).read_bytes() == b'new saved bytes'
        jobs[0][2].set_result(None)
        assert str(cache) not in sync._targets
        assert not Path(snapshot).exists()
        assert not pane._remote_sync_states
    finally:
        pane.deleteLater()


def test_failed_unsent_edits_survive_last_editor_closing(setup):
    sync, cache, session, jobs = setup
    pane = FileEditorWidget()
    try:
        bind(sync, pane, cache, session)
        sync.save(str(cache), b'unsent local data')
        wait_for(lambda: jobs)
        jobs[0][2].set_exception(RuntimeError('network disconnected'))
        snapshot = sync._latest[str(cache)]
        pane._close_editor()
        assert sync.pending(str(cache))
        assert str(cache) in sync._targets
        assert Path(snapshot).read_bytes() == b'unsent local data'
    finally:
        pane.deleteLater()


def test_destroying_pane_releases_synced_references(setup):
    sync, cache, session, _ = setup
    pane = FileEditorWidget()
    bind(sync, pane, cache, session)
    pane.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert str(cache) not in sync._targets
    assert not sync._references


def test_unrelated_file_states_do_not_accumulate_in_pane(setup):
    sync, cache, session, _ = setup
    pane = FileEditorWidget()
    try:
        bind(sync, pane, cache, session)
        for index in range(100):
            pane.set_remote_sync_state(f'/closed-file-{index}', 'synced')
        assert list(pane._remote_sync_states) == [str(cache)]
    finally:
        pane.deleteLater()


def test_slow_recovery_cannot_delay_other_file_snapshot_capture(setup, tmp_path):
    sync, cache, session, jobs = setup
    other = tmp_path/'edited.txt'
    other.write_bytes(b'local saved data')
    sync.register(str(other), 'host', '/edited', session, recover=False)
    entered, release = threading.Event(), threading.Event()
    original = sync._recover_file
    def slow(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    try:
        with patch.object(sync, '_recover_file', side_effect=slow):
            sync.prepare_async(str(cache), 'host', '/remote', session, lambda: None)
            assert entered.wait(1)
            sync.save(str(other), b'latest saved content')
            wait_for(lambda: jobs)
            assert Path(sync._latest[str(other)]).read_bytes() == b'latest saved content'
            assert not release.is_set()
            release.set()
    finally:
        release.set()
