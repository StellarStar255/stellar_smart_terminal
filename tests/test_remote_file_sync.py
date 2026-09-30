"""Remote saves must upload stable versions and retain unsent content."""
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
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
    sync.deleteLater()
    app.processEvents()


def test_upload_uses_snapshot_and_serializes_latest_save(setup_sync):
    _, sync, session, local = setup_sync
    sync.save(str(local))
    first, remote, future = session.jobs[0]
    local.write_bytes(b'second')
    sync.save(str(local))
    local.write_bytes(b'third')
    sync.save(str(local))
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
    sync.save(str(local))
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
    sync.save(str(local))
    local.write_bytes(b'latest')
    sync.save(str(local))
    session.jobs[0][2].set_exception(RuntimeError('old upload failed'))
    assert session.jobs[1][0].read_bytes() == b'latest'
    assert sync.state(str(local))[0] == 'uploading'
    session.jobs[1][2].set_result(None)


def test_restore_unsent_save_after_restart_without_automatic_upload(setup_sync):
    _, sync, session, local = setup_sync
    session.connected = False
    sync.save(str(local))
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
    sync.save(str(local))
    other = local.parent / 'other.txt'
    other.write_bytes(b'other host')
    sync.register(str(other), 'different-host', '/remote.txt', session)
    assert other.read_bytes() == b'other host'
    assert not sync.pending(str(other))


def test_worker_result_is_dispatched_on_qt_thread(setup_sync):
    app, sync, session, local = setup_sync
    sync.save(str(local))
    with ThreadPoolExecutor(1) as worker:
        worker.submit(session.jobs[0][2].set_result, None).result()
    assert sync.state(str(local))[0] == 'uploading'
    app.processEvents()
    assert sync.state(str(local))[0] == 'synced'


def test_snapshot_write_failure_cannot_report_old_version_synced(setup_sync):
    _, sync, session, local = setup_sync
    sync.save(str(local))
    old_snapshot = session.jobs[0][0]
    local.write_bytes(b'latest saved locally')
    with patch('remote_file_sync.shutil.copyfileobj', side_effect=OSError('disk full')):
        sync.save(str(local))
    session.jobs[0][2].set_result(None)
    assert old_snapshot.read_bytes() == b'first'
    assert sync.state(str(local)) == ('failed', 'disk full')
    assert sync.pending(str(local))
    sync.retry(str(local))
    assert session.jobs[1][0].read_bytes() == b'latest saved locally'
    session.jobs[1][2].set_result(None)


def test_recovery_removes_older_orphaned_versions(setup_sync):
    _, sync, session, local = setup_sync
    sync.save(str(local))
    old = session.jobs[0][0]
    local.write_bytes(b'latest')
    sync.save(str(local))
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
    sync.save(str(local))
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
