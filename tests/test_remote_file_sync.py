"""Remote saves must upload stable versions and retain unsent content."""
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import time
from unittest.mock import patch

import pytest
from PyQt6.QtWidgets import QApplication

from remote_file_sync import RemoteFileSync


_APP = None


class Session:
    def __init__(self):
        self.connected = True
        self.jobs = []

    def is_connected(self):
        return self.connected

    def upload(self, local, remote):
        pass

    def submit(self, fn, local, remote):
        future = Future()
        self.jobs.append((Path(local), remote, future))
        return future


def _wait_until(predicate, timeout=3):
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Background snapshot did not complete')
        app.processEvents()
        time.sleep(.001)


def _save(sync, path):
    sync.save(str(path), Path(path).read_bytes())
    _wait_until(lambda: not sync._capture_jobs and not sync._capture_sources)



@pytest.fixture
def setup_sync(tmp_path):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    app = _APP
    session = Session()
    with patch('remote_file_sync.get_data_dir', return_value=str(tmp_path)):
        sync = RemoteFileSync(lambda host, original: session)
    local = tmp_path / 'cache.txt'
    local.write_bytes(b'first')
    sync.register(str(local), 'host', '/remote.txt', session)
    yield app, sync, session, local
    sync.shutdown()
    sync._capture_executor.shutdown(wait=True)
    sync.deleteLater()
    app.processEvents()


def test_upload_uses_snapshot_and_serializes_latest_save(setup_sync):
    _, sync, session, local = setup_sync
    _save(sync, local)
    first, remote, future = session.jobs[0]
    local.write_bytes(b'second')
    _save(sync, local)
    local.write_bytes(b'third')
    _save(sync, local)
    assert len(session.jobs) == 1
    assert first.read_bytes() == b'first'
    assert remote == '/remote.txt'
    future.set_result(None)
    assert not first.exists()
    assert len(session.jobs) == 2
    latest, _, future = session.jobs[1]
    assert latest.read_bytes() == b'third'
    assert sync.state(str(local))[0] == 'uploading'
    future.set_result(None)
    assert not latest.exists()
    assert not sync.pending(str(local))
    assert sync.state(str(local))[0] == 'synced'
    assert not list((local.parent / 'remote_pending').iterdir())


def test_failure_preserves_snapshot_and_retries_after_reconnect(setup_sync):
    _, sync, session, local = setup_sync
    session.connected = False
    _save(sync, local)
    assert not session.jobs
    assert sync.state(str(local))[0] == 'failed'
    session.connected = True
    sync.retry(str(local))
    snapshot, _, future = session.jobs[0]
    future.set_exception(RuntimeError('upload denied'))
    assert snapshot.read_bytes() == b'first'
    assert sync.state(str(local)) == ('failed', 'upload denied')
    sync.retry(str(local))
    assert session.jobs[1][0] == snapshot
    session.jobs[1][2].set_result(None)
    assert not sync.pending(str(local))


def test_old_failure_cannot_mark_newer_save_failed(setup_sync):
    _, sync, session, local = setup_sync
    _save(sync, local)
    local.write_bytes(b'latest')
    _save(sync, local)
    session.jobs[0][2].set_exception(RuntimeError('old upload failed'))
    assert session.jobs[1][0].read_bytes() == b'latest'
    assert sync.state(str(local))[0] == 'uploading'
    session.jobs[1][2].set_result(None)


def test_restore_unsent_save_after_restart_without_automatic_upload(setup_sync):
    _, sync, session, local = setup_sync
    session.connected = False
    _save(sync, local)
    local.write_bytes(b'fresh remote download')
    with patch('remote_file_sync.get_data_dir', return_value=str(local.parent)):
        restarted = RemoteFileSync(lambda host, original: session)
    restarted.register(str(local), 'host', '/remote.txt', session)
    assert local.read_bytes() == b'first'
    assert restarted.pending(str(local))
    assert not session.jobs
    session.connected = True
    restarted.retry(str(local))
    session.jobs[0][2].set_result(None)
    restarted.deleteLater()


def test_recovery_does_not_restore_other_host_content(setup_sync):
    _, sync, session, local = setup_sync
    session.connected = False
    _save(sync, local)
    other = local.parent / 'other.txt'
    other.write_bytes(b'other host')
    sync.register(str(other), 'different-host', '/remote.txt', session)
    assert other.read_bytes() == b'other host'
    assert not sync.pending(str(other))


def test_worker_result_is_dispatched_on_qt_thread(setup_sync):
    app, sync, session, local = setup_sync
    _save(sync, local)
    with ThreadPoolExecutor(1) as worker:
        worker.submit(session.jobs[0][2].set_result, None).result()
    assert sync.state(str(local))[0] == 'uploading'
    app.processEvents()
    assert sync.state(str(local))[0] == 'synced'


def test_snapshot_write_failure_cannot_report_old_version_synced(setup_sync):
    _, sync, session, local = setup_sync
    _save(sync, local)
    old_snapshot = session.jobs[0][0]
    local.write_bytes(b'latest saved locally')
    with patch('remote_file_sync.os.fsync', side_effect=OSError('disk full')):
        _save(sync, local)
    session.jobs[0][2].set_result(None)
    assert old_snapshot.read_bytes() == b'first'
    assert sync.state(str(local)) == ('failed', 'disk full')
    assert sync.pending(str(local))
    sync.retry(str(local))
    _wait_until(lambda: len(session.jobs) == 2)
    assert session.jobs[1][0].read_bytes() == b'latest saved locally'
    session.jobs[1][2].set_result(None)


def test_recovery_removes_older_orphaned_versions(setup_sync):
    _, sync, session, local = setup_sync
    _save(sync, local)
    old = session.jobs[0][0]
    local.write_bytes(b'latest')
    _save(sync, local)
    with patch('remote_file_sync.get_data_dir', return_value=str(local.parent)):
        restarted = RemoteFileSync(lambda host, original: session)
    restarted.register(str(local), 'host', '/remote.txt', session)
    assert local.read_bytes() == b'latest'
    assert not old.exists()
    restarted.retry(str(local))
    session.jobs[1][2].set_result(None)
    assert not list((local.parent / 'remote_pending').iterdir())
    restarted.deleteLater()


def test_reopening_pending_file_never_downloads_over_saved_changes(setup_sync):
    from types import SimpleNamespace
    from remote_explorer_widget import RemoteExplorerPanel
    _, sync, session, local = setup_sync
    session.host_config = SimpleNamespace(alias='host')
    _save(sync, local)
    session.jobs[0][2].set_exception(RuntimeError('failed'))
    opened = []
    panel = SimpleNamespace(_session=session, file_sync=sync, _open_session_map={},
                            _file_ready=SimpleNamespace(emit=lambda *args: opened.append(args)))
    entry = SimpleNamespace(path='/remote.txt', name=local.name, size=999, mtime=1)
    with patch('remote_explorer_widget._remote_cache_dir', return_value=str(local.parent)):
        RemoteExplorerPanel._open_remote_file(panel, entry)
    assert local.read_bytes() == b'first'
    assert opened == [('host', '/remote.txt', str(local))]
    assert len(session.jobs) == 1


def test_upload_keeps_original_host_session_when_panel_switches():
    from types import SimpleNamespace
    from remote_explorer_widget import RemoteExplorerPanel
    original = Session()
    other = Session()
    other.host_config = SimpleNamespace(alias='other')
    panel = SimpleNamespace(_session=other)
    assert RemoteExplorerPanel._editor_session_for(panel, 'host', original) is original
    other.host_config.alias = 'host'
    assert RemoteExplorerPanel._editor_session_for(panel, 'host', original) is other


def test_slow_snapshot_does_not_block_gui_and_uses_immutable_saved_bytes(setup_sync):
    from PyQt6.QtCore import QTimer
    import threading
    app, sync, session, local = setup_sync
    gate, entered = threading.Event(), threading.Event()
    original = sync._write_snapshot
    def blocked(*args):
        entered.set()
        assert gate.wait(3)
        return original(*args)
    ticks = []
    try:
        with patch.object(sync, '_write_snapshot', side_effect=blocked):
            sync.save(str(local), b'first saved version')
            assert entered.wait(1)
            QTimer.singleShot(0, lambda: ticks.append(True))
            app.processEvents()
            assert ticks and not session.jobs
            local.write_bytes(b'later unrelated disk contents')
            gate.set()
            _wait_until(lambda: len(session.jobs) == 1)
        assert session.jobs[0][0].read_bytes() == b'first saved version'
        session.jobs[0][2].set_result(None)
    finally:
        gate.set()


def test_snapshot_queue_coalesces_new_saves_during_slow_disk(setup_sync):
    import threading
    _, sync, session, local = setup_sync
    gate, entered = threading.Event(), threading.Event()
    original = sync._write_snapshot
    calls = []
    def blocked(*args):
        calls.append(args)
        if len(calls) == 1:
            entered.set()
            assert gate.wait(3)
        return original(*args)
    try:
        with patch.object(sync, '_write_snapshot', side_effect=blocked):
            sync.save(str(local), b'first')
            assert entered.wait(1)
            for i in range(20):
                sync.save(str(local), f'latest {i}'.encode())
            assert len(calls) == 1
            assert len(sync._capture_sources) == 1
            gate.set()
            _wait_until(lambda: len(session.jobs) == 1)
        assert len(calls) == 2
        assert session.jobs[0][0].read_bytes() == b'latest 19'
        session.jobs[0][2].set_result(None)
    finally:
        gate.set()


def test_upload_completion_waits_for_new_snapshot_before_reporting_synced(setup_sync):
    import threading
    _, sync, session, local = setup_sync
    _save(sync, local)
    gate, entered = threading.Event(), threading.Event()
    original = sync._write_snapshot
    def blocked(*args):
        entered.set()
        assert gate.wait(3)
        return original(*args)
    try:
        with patch.object(sync, '_write_snapshot', side_effect=blocked):
            sync.save(str(local), b'new version')
            assert entered.wait(1)
            session.jobs[0][2].set_result(None)
            assert sync.state(str(local))[0] == 'pending'
            assert sync.pending(str(local))
            gate.set()
            _wait_until(lambda: len(session.jobs) == 2)
        assert session.jobs[1][0].read_bytes() == b'new version'
        session.jobs[1][2].set_result(None)
        assert sync.state(str(local))[0] == 'synced'
    finally:
        gate.set()


def test_close_mid_copy_persists_latest_coalesced_version(setup_sync):
    import threading
    import json
    _, sync, session, local = setup_sync
    gate, entered = threading.Event(), threading.Event()
    original = sync._write_snapshot
    def blocked(*args):
        entered.set()
        assert gate.wait(3)
        return original(*args)
    try:
        with patch.object(sync, '_write_snapshot', side_effect=blocked):
            sync.save(str(local), b'first')
            assert entered.wait(1)
            sync.save(str(local), b'latest saved before closing')
            sync.shutdown()
            gate.set()
            sync._capture_executor.shutdown(wait=True)
        metadata = list((local.parent / 'remote_pending').glob('*.json'))
        newest = max(metadata, key=lambda p: json.loads(p.read_text())['timestamp'])
        assert Path(str(newest)[:-5]).read_bytes() == b'latest saved before closing'
        assert not session.jobs
    finally:
        gate.set()


def test_parent_destruction_stops_snapshot_executor_without_crashing(setup_sync):
    from PyQt6.QtWidgets import QWidget
    from PyQt6 import sip
    app, _, session, local = setup_sync
    for _ in range(20):
        parent = QWidget()
        with patch('remote_file_sync.get_data_dir', return_value=str(local.parent)):
            sync = RemoteFileSync(lambda host, original: session, parent)
        executor = sync._capture_executor
        sip.delete(parent)
        assert executor._shutdown
        app.processEvents()
