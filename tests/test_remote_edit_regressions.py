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
            SSHSession.upload_with_progress.__wrapped__(session, str(source), str(target),
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
        return SSHSession.upload_with_progress.__wrapped__(backing, source, target,
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
