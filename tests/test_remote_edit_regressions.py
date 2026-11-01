"""Exercise both upload implementations against a real local filesystem."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PyQt6.QtWidgets import QApplication

from file_persistence import file_sha256
from remote_edit import atomic_upload_command, RemoteFileConflictError
from remote_file_sync import RemoteFileSync
from ssh_session import SSHSession

_APP = None


class LocalSFTP:
    def __init__(self):
        self.after_put = lambda: None

    def lstat(self, path):
        return os.lstat(path)

    def stat(self, path):
        return os.stat(path)

    def normalize(self, path):
        return os.path.realpath(path)

    def chmod(self, path, mode):
        os.chmod(path, mode)

    def open(self, path, mode):
        return open(path, 'xb' if mode == 'wx' else mode)

    def put(self, source, target, callback=None):
        with open(source, 'rb') as src, open(target, 'wb') as dst:
            shutil.copyfileobj(src, dst)
        self.after_put()

    def posix_rename(self, source, target):
        os.replace(source, target)

    rename = posix_rename

    def remove(self, path):
        os.unlink(path)


@pytest.fixture(params=['sftp', 'shell'])
def uploader(request, tmp_path):
    if request.param == 'shell' and os.name == 'nt':
        pytest.skip('ControlMaster uploads require a POSIX shell')
    if request.param == 'sftp':
        fs = LocalSFTP()
        session = SimpleNamespace(_require=lambda: fs, _parent=lambda p: str(Path(p).parent),
                                  invalidate_cache=lambda _: None)
        def upload(source, target, expected=None):
            SSHSession.upload_with_progress.__wrapped__(session, Path(source).as_posix(), Path(target).as_posix(),
                                                        expected_hash=expected)
    else:
        fs = None
        def upload(source, target, expected=None):
            temporary = tmp_path / '.stellar-upload-test.part'
            command = atomic_upload_command(shlex.quote(str(target)), shlex.quote(str(temporary)),
                                            expected, file_sha256(source) if expected else None)
            result = subprocess.run(['/bin/sh', '-c', command], input=Path(source).read_bytes(),
                                    capture_output=True, timeout=5)
            if result.returncode:
                if b'STELLAR_EDIT_CONFLICT' in result.stderr:
                    raise RemoteFileConflictError()
                raise OSError(result.stderr.decode())
    return upload, fs


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission bits')
def test_save_keeps_executable_mode(uploader, tmp_path):
    upload, _ = uploader
    source, remote = tmp_path/'source', tmp_path/'remote'
    source.write_bytes(b'new')
    remote.write_bytes(b'old')
    remote.chmod(0o751)
    upload(source, remote)
    assert remote.read_bytes() == b'new'
    assert stat.S_IMODE(remote.stat().st_mode) == 0o751
    assert not list(tmp_path.glob('.stellar-upload-*'))


@pytest.mark.skipif(os.name == 'nt', reason='Local POSIX symlink and permission semantics')
def test_save_follows_relative_symlink_chain_without_replacing_links(uploader, tmp_path):
    upload, _ = uploader
    sub = tmp_path/'other directory'
    sub.mkdir()
    target = sub/"target ' file"
    target.write_bytes(b'old')
    target.chmod(0o750)
    middle = sub/'middle'
    middle.symlink_to(target.name)
    link = tmp_path/'link'
    link.symlink_to('other directory/middle')
    source = tmp_path/'source'
    source.write_bytes(b'new')
    upload(source, link, hashlib.sha256(b'old').hexdigest())
    assert link.is_symlink() and middle.is_symlink()
    assert target.read_bytes() == b'new'
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert not list(sub.glob('.stellar-upload-*'))


def test_conflict_retains_external_changes_and_removes_owned_temp(uploader, tmp_path):
    upload, _ = uploader
    source, remote = tmp_path/'source', tmp_path/'remote'
    source.write_bytes(b'local changes')
    remote.write_bytes(b'external changes')
    with pytest.raises(RemoteFileConflictError):
        upload(source, remote, hashlib.sha256(b'original').hexdigest())
    assert remote.read_bytes() == b'external changes'
    assert source.read_bytes() == b'local changes'
    assert not list(tmp_path.glob('.stellar-upload-*'))


def test_retry_accepts_previously_published_content(uploader, tmp_path):
    upload, _ = uploader
    source, remote = tmp_path/'source', tmp_path/'remote'
    source.write_bytes(b'new')
    remote.write_bytes(b'new')
    upload(source, remote, hashlib.sha256(b'old').hexdigest())
    assert remote.read_bytes() == b'new'


def test_sftp_rechecks_after_transfer(uploader, tmp_path):
    upload, fs = uploader
    if fs is None:
        pytest.skip('SFTP-specific transfer hook')
    source, remote = tmp_path/'source', tmp_path/'remote'
    source.write_bytes(b'new')
    remote.write_bytes(b'old')
    fs.after_put = lambda: remote.write_bytes(b'changed during transfer')
    with pytest.raises(RemoteFileConflictError):
        upload(source, remote, hashlib.sha256(b'old').hexdigest())
    assert remote.read_bytes() == b'changed during transfer'
    assert not list(tmp_path.glob('.stellar-upload-*'))


def test_shell_digest_cannot_inject_commands():
    with pytest.raises(ValueError):
        atomic_upload_command('/remote', '/tmp/file', '$(touch /tmp/unwanted)', 'a'*64)


@pytest.fixture
def sync_setup(tmp_path):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    fs = LocalSFTP()
    executor = ThreadPoolExecutor(1)
    backing = SimpleNamespace(_require=lambda: fs, _parent=lambda p: str(Path(p).parent),
                              invalidate_cache=lambda _: None)
    def upload(source, target, expected):
        return SSHSession.upload_with_progress.__wrapped__(backing, Path(source).as_posix(), Path(target).as_posix(),
                                                           expected_hash=expected)
    session = SimpleNamespace(is_connected=lambda: True, submit=executor.submit,
                              upload_for_edit=upload,
                              read_file=lambda path, max_bytes: Path(path).read_bytes())
    with patch('remote_file_sync.get_data_dir', return_value=tmp_path):
        sync = RemoteFileSync(lambda *_: session)
    remote, local = tmp_path/'remote', tmp_path/'local'
    remote.write_bytes(b'original')
    local.write_bytes(b'original')
    sync.register(str(local), 'host', str(remote), session, baseline_hash=file_sha256(local))
    yield sync, local, remote, session
    sync.shutdown()
    sync._capture_executor.shutdown(wait=True)
    executor.shutdown(wait=True)
    sync.deleteLater()
    _APP.processEvents()


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        _APP.processEvents()
        time.sleep(.001)


def test_sync_conflict_compare_merge_and_followup_save(sync_setup):
    sync, local, remote, _ = sync_setup
    remote.write_bytes(b'external')
    local.write_bytes(b'local')
    sync.save(str(local), b'local')
    wait_for(lambda: sync.state(str(local))[0] == 'conflict')
    assert remote.read_bytes() == b'external'
    assert sync.pending(str(local))
    sync.retry(str(local))
    assert sync.state(str(local))[0] == 'conflict'
    comparisons = []
    sync.comparison_ready.connect(lambda path, data: comparisons.append((path, data)))
    sync.compare(str(local))
    wait_for(lambda: comparisons)
    assert comparisons == [(str(local), b'external')]
    sync.resolve(str(local), hashlib.sha256(comparisons[0][1]).hexdigest())
    local.write_bytes(b'merged')
    sync.save(str(local), b'merged')
    wait_for(lambda: sync.state(str(local))[0] == 'synced')
    assert remote.read_bytes() == b'merged'
    # Baseline must advance after success, allowing the next normal save.
    local.write_bytes(b'merged again')
    sync.save(str(local), b'merged again')
    wait_for(lambda: sync.state(str(local))[0] == 'synced')
    assert remote.read_bytes() == b'merged again'
    assert not sync.pending(str(local))


def test_recovery_retains_original_remote_baseline(sync_setup, tmp_path):
    sync, local, remote, session = sync_setup
    remote.write_bytes(b'external')
    sync.save(str(local), b'local edits')
    wait_for(lambda: sync.state(str(local))[0] == 'conflict')
    with patch('remote_file_sync.get_data_dir', return_value=tmp_path):
        recovered = RemoteFileSync(lambda *_: session)
    try:
        recovered.register(str(local), 'host', str(remote), session,
                           baseline_hash=hashlib.sha256(b'external').hexdigest())
        assert local.read_bytes() == b'local edits'
        recovered.retry(str(local))
        wait_for(lambda: recovered.state(str(local))[0] == 'conflict')
        assert remote.read_bytes() == b'external'
    finally:
        recovered.shutdown()
        recovered._capture_executor.shutdown(wait=True)
        recovered.deleteLater()


def test_conflict_comparison_cancel_keeps_editor_and_baseline(sync_setup):
    from file_editor import FileEditorWidget
    sync, local, _, _ = sync_setup
    pane = FileEditorWidget()
    try:
        assert pane.open_file(str(local))
        pane.editor.setPlainText('unsaved local work')
        resolved = []
        pane.remote_sync_resolved.connect(lambda *args: resolved.append(args))
        with patch('remote_conflict_dialog.merge_remote_contents', return_value=None):
            pane.show_remote_comparison(str(local), b'remote work')
        assert pane.editor.toPlainText() == 'unsaved local work'
        assert not resolved
        assert local.read_bytes() == b'original'
    finally:
        pane.deleteLater()


def test_conflict_merge_sets_remote_baseline_before_saving(sync_setup):
    from file_editor import FileEditorWidget
    _, local, _, _ = sync_setup
    pane = FileEditorWidget()
    try:
        assert pane.open_file(str(local))
        calls = []
        pane.remote_sync_resolved.connect(lambda path, baseline: calls.append(('baseline', baseline)))
        pane.remote_save_requested.connect(lambda path, data: calls.append(('save', data)))
        with patch('remote_conflict_dialog.merge_remote_contents', return_value='merged'):
            pane.show_remote_comparison(str(local), b'remote work')
        assert calls == [('baseline', hashlib.sha256(b'remote work').hexdigest()), ('save', b'merged')]
        assert local.read_bytes() == b'merged'
    finally:
        pane.deleteLater()


def test_comparison_requests_coalesce_until_worker_completes(sync_setup):
    from concurrent.futures import Future
    sync, local, _, session = sync_setup
    path = str(local)
    sync._conflicts.add(path)
    future = Future()
    original_submit = session.submit
    session.submit = lambda *args, **kwargs: future
    try:
        ready = []
        sync.comparison_ready.connect(lambda *args: ready.append(args))
        for _ in range(25):
            sync.compare(path)
        assert len(sync._comparison_jobs) == 1
        future.set_result(b'current remote')
        assert ready == [(path, b'current remote')]
        assert not sync._comparison_jobs
    finally:
        session.submit = original_submit


def test_comparison_completion_is_ignored_after_shutdown(sync_setup):
    from concurrent.futures import Future
    sync, local, _, session = sync_setup
    path = str(local)
    sync._conflicts.add(path)
    future = Future()
    original_submit = session.submit
    session.submit = lambda *args, **kwargs: future
    try:
        ready = []
        sync.comparison_ready.connect(lambda *args: ready.append(args))
        sync.compare(path)
        sync.shutdown()
        future.set_result(b'late remote response')
        assert not ready
        assert not sync._comparison_jobs
    finally:
        session.submit = original_submit


def test_comparison_failure_is_reported_on_gui_and_can_retry(sync_setup):
    from concurrent.futures import Future
    sync, local, _, session = sync_setup
    path = str(local)
    sync._conflicts.add(path)
    future = Future()
    original_submit = session.submit
    session.submit = lambda *args, **kwargs: future
    try:
        sync.compare(path)
        future.set_exception(RuntimeError('connection lost'))
        assert sync.state(path) == ('conflict', 'connection lost')
        assert not sync._comparison_jobs
    finally:
        session.submit = original_submit


def test_recovery_is_loaded_before_remote_editor_reads_cache(sync_setup):
    from file_editor import EditorArea
    from main_window_remote import RemotePanelMixin
    sync, local, _, session = sync_setup
    # Simulate a fresh UI and a stale on-disk cache with an unsent durable edit.
    RemoteFileSync._write_snapshot(sync._pending_dir, str(local), 'host', '/remote.txt',
                                   b'unsent recovered edits', time.time(), file_sha256(local))
    area = EditorArea(theme={})
    class Host(RemotePanelMixin):
        editor_area = area
        _remote_split_horizontal = False
        remote_splitter = object()
        def _ensure_remote_panel(self):
            return SimpleNamespace(file_sync=sync)
        def _editor_placed_and_visible(self, target):
            return True
        def _spring_applicable(self):
            return False
    try:
        Host()._open_remote_file_in_editor('host', '/remote.txt', str(local), session)
        assert area.active_pane.editor.toPlainText() == 'unsent recovered edits'
        assert local.read_bytes() == b'unsent recovered edits'
        assert sync.pending(str(local))
    finally:
        area.deleteLater()


def test_remote_save_connections_are_per_pane_and_rebind_cleanly(sync_setup):
    from file_editor import FileEditorWidget
    from main_window_remote import RemotePanelMixin
    from unittest.mock import Mock
    sync, local, _, _ = sync_setup
    with patch('remote_file_sync.get_data_dir', return_value=local.parent):
        other_sync = RemoteFileSync(lambda *_: None)
    first, second = FileEditorWidget(), FileEditorWidget()
    host = RemotePanelMixin()
    sync.save = Mock()
    other_sync.save = Mock()
    panel, other_panel = SimpleNamespace(file_sync=sync), SimpleNamespace(file_sync=other_sync)
    try:
        for _ in range(10):
            host._connect_remote_editor(first, panel)
            host._connect_remote_editor(second, panel)
        first.remote_save_requested.emit(str(local), b'first pane')
        second.remote_save_requested.emit(str(local), b'second pane')
        assert sync.save.call_count == 2
        host._connect_remote_editor(first, other_panel)
        first.remote_save_requested.emit(str(local), b'new panel')
        assert sync.save.call_count == 2
        other_sync.save.assert_called_once_with(str(local), b'new panel')
    finally:
        other_sync.shutdown()
        other_sync._capture_executor.shutdown(wait=True)
        first.deleteLater()
        second.deleteLater()
        other_sync.deleteLater()


def test_remote_deletion_is_a_conflict_until_explicit_restore(uploader, tmp_path):
    from remote_edit import REMOTE_MISSING
    upload, _ = uploader
    source, remote = tmp_path/'source', tmp_path/'deleted'
    source.write_bytes(b'local changes')
    with pytest.raises(RemoteFileConflictError):
        upload(source, remote, hashlib.sha256(b'original').hexdigest())
    assert not remote.exists()
    upload(source, remote, REMOTE_MISSING)
    assert remote.read_bytes() == b'local changes'


def test_restore_cannot_overwrite_newly_recreated_empty_file(uploader, tmp_path):
    from remote_edit import REMOTE_MISSING
    upload, _ = uploader
    source, remote = tmp_path/'source', tmp_path/'recreated'
    source.write_bytes(b'local changes')
    remote.write_bytes(b'')
    with pytest.raises(RemoteFileConflictError):
        upload(source, remote, REMOTE_MISSING)
    assert remote.read_bytes() == b''
    assert not list(tmp_path.glob('.stellar-upload-*'))


def test_restore_retry_accepts_previously_restored_content(uploader, tmp_path):
    from remote_edit import REMOTE_MISSING
    upload, _ = uploader
    source, remote = tmp_path/'source', tmp_path/'restored'
    source.write_bytes(b'restored')
    remote.write_bytes(b'restored')
    upload(source, remote, REMOTE_MISSING)
    assert remote.read_bytes() == b'restored'


def test_sync_delete_compare_and_explicit_restore(sync_setup):
    from remote_edit import REMOTE_MISSING
    sync, local, remote, _ = sync_setup
    remote.unlink()
    local.write_bytes(b'local work')
    sync.save(str(local), b'local work')
    wait_for(lambda: sync.state(str(local))[0] == 'conflict')
    assert not remote.exists()
    comparisons = []
    sync.comparison_ready.connect(lambda *args: comparisons.append(args))
    sync.compare(str(local))
    wait_for(lambda: comparisons)
    assert comparisons == [(str(local), None)]
    sync.resolve(str(local), REMOTE_MISSING)
    sync.save(str(local), b'local work')
    wait_for(lambda: sync.state(str(local))[0] == 'synced')
    assert remote.read_bytes() == b'local work'
    assert not sync.pending(str(local))


@pytest.mark.parametrize('accepted', [False, True])
def test_deleted_remote_dialog_requires_explicit_restore(sync_setup, accepted):
    from file_editor import FileEditorWidget
    from remote_edit import REMOTE_MISSING
    _, local, _, _ = sync_setup
    pane = FileEditorWidget()
    try:
        assert pane.open_file(str(local))
        resolved, saves = [], []
        pane.remote_sync_resolved.connect(lambda *args: resolved.append(args))
        pane.remote_save_requested.connect(lambda *args: saves.append(args))
        with patch('remote_conflict_dialog.merge_remote_contents', return_value='original' if accepted else None) as dialog:
            pane.show_remote_comparison(str(local), None)
        assert dialog.call_args.kwargs['remote_missing'] is True
        if accepted:
            assert resolved == [(str(local), REMOTE_MISSING)]
            assert saves == [(str(local), b'original')]
        else:
            assert not resolved and not saves
        assert local.read_bytes() == b'original'
    finally:
        pane.deleteLater()


def test_snapshot_damaged_after_capture_is_never_uploaded(sync_setup):
    import threading
    sync, local, remote, session = sync_setup
    release, entered = threading.Event(), threading.Event()
    def block_executor():
        entered.set()
        assert release.wait(3)
    gate = session.submit(block_executor)
    try:
        assert entered.wait(1)
        sync.save(str(local), b'local saved edit')
        wait_for(lambda: sync.state(str(local))[0] == 'uploading')
        snapshot = sync._latest[str(local)]
        Path(snapshot).write_bytes(b'damaged after snapshot capture')
        release.set()
        gate.result()
        wait_for(lambda: sync.state(str(local))[0] == 'failed')
        assert remote.read_bytes() == b'original'
        assert Path(snapshot).exists()
        sync.save(str(local), b'reviewed local content')
        wait_for(lambda: sync.state(str(local))[0] == 'synced')
        assert remote.read_bytes() == b'reviewed local content'
    finally:
        release.set()
