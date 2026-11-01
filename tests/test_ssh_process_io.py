"""Use real pipes to exercise read/write stalls, cancellation and stderr pressure."""
import os
import subprocess
import sys
import time

import pytest

from ssh_process_io import ProcessPipeIO, TransferCancelledError, TransferTimeoutError

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='ControlMaster pipe I/O is POSIX-only')


def spawn(code, *, stdin=None):
    return subprocess.Popen([sys.executable, '-u', '-c', code], stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def assert_closed_and_reaped(proc):
    assert proc.poll() is not None
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            assert stream.closed


def test_silent_download_times_out_before_pipe_read_can_hang():
    proc = spawn('import time; time.sleep(10)')
    start = time.monotonic()
    with pytest.raises(TransferTimeoutError):
        with ProcessPipeIO(proc, timeout=.2) as pipes:
            pipes.read(1024)
    assert time.monotonic() - start < 2
    assert_closed_and_reaped(proc)


def test_upload_to_process_not_reading_stdin_times_out():
    proc = spawn('import time; time.sleep(10)', stdin=subprocess.PIPE)
    with pytest.raises(TransferTimeoutError):
        with ProcessPipeIO(proc, timeout=.2) as pipes:
            pipes.write(b'x' * (4 * 1024 * 1024))
    assert_closed_and_reaped(proc)


def test_process_exit_and_stderr_eof_share_transfer_deadline():
    proc = spawn('import os,time; os.close(1); time.sleep(10)')
    with pytest.raises(TransferTimeoutError):
        with ProcessPipeIO(proc, timeout=.2) as pipes:
            assert pipes.read(1024) == b''
            pipes.finish()
    assert_closed_and_reaped(proc)


def test_stderr_flood_does_not_deadlock_or_allocate_unbounded_memory():
    proc = spawn("import os; os.write(2,b'e'*4000000); os.write(1,b'payload')")
    with ProcessPipeIO(proc, timeout=3) as pipes:
        received = bytearray()
        while True:
            chunk = pipes.read(64)
            if not chunk:
                break
            received.extend(chunk)
        error = pipes.finish()
        assert len(error.encode()) <= pipes.MAX_STDERR_BYTES
    assert received == b'payload'
    assert proc.returncode == 0
    assert_closed_and_reaped(proc)


def test_periodic_progress_allows_transfer_longer_than_idle_limit():
    proc = spawn("import os,time\nfor _ in range(6):\n os.write(1,b'x'); time.sleep(.08)")
    start = time.monotonic()
    with ProcessPipeIO(proc, timeout=.3) as pipes:
        received = bytearray()
        while True:
            chunk = pipes.read(1024)
            if not chunk:
                break
            received.extend(chunk)
        pipes.finish()
    assert received == b'xxxxxx'
    assert time.monotonic() - start > .3
    assert_closed_and_reaped(proc)


def test_cancellation_unblocks_read_and_reaps_process():
    proc = spawn('import time; time.sleep(10)')
    start = time.monotonic()
    with pytest.raises(TransferCancelledError):
        with ProcessPipeIO(proc, timeout=5, cancelled=lambda: time.monotonic() - start > .15) as pipes:
            pipes.read(1024)
    assert time.monotonic() - start < 2
    assert_closed_and_reaped(proc)


@pytest.fixture
def session():
    from PyQt6.QtWidgets import QApplication
    from ssh_control import ControlMasterSession
    from ssh_session import HostConfig
    global _APP
    _APP = QApplication.instance() or QApplication([])
    session = ControlMasterSession(HostConfig(alias='test', hostname='test.invalid'))
    yield session
    session.abort()
    session._executor.shutdown(wait=True)


def local_processes(session, monkeypatch, code):
    processes = []
    def start(command, **kwargs):
        proc = subprocess.Popen([sys.executable, '-u', '-c', code],
                                stdin=kwargs.get('stdin', subprocess.DEVNULL),
                                stdout=kwargs.get('stdout', subprocess.PIPE), stderr=subprocess.PIPE)
        proc._stellar_generation = session._abort_generation
        with session._procs_lock:
            session._procs.add(proc)
        processes.append(proc)
        return proc
    monkeypatch.setattr(session, '_spawn', start)
    return processes


def test_download_timeout_preserves_destination_and_reaps_child(session, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ssh_control
    target = tmp_path/'download'
    target.write_bytes(b'original')
    unrelated = tmp_path/'download.part'
    unrelated.write_bytes(b'unrelated')
    monkeypatch.setattr(session, 'stat', lambda _: SimpleNamespace(size=100, mtime=0))
    monkeypatch.setattr(ssh_control, 'CMD_TIMEOUT', .2)
    processes = local_processes(session, monkeypatch,
                                "import os,time; os.write(1,b'partial'); time.sleep(10)")
    with pytest.raises(TransferTimeoutError):
        session.download('/remote', str(target))
    assert target.read_bytes() == b'original'
    assert unrelated.read_bytes() == b'unrelated'
    assert sorted(path.name for path in tmp_path.iterdir()) == ['download', 'download.part']
    assert not session._procs
    assert_closed_and_reaped(processes[0])


def test_upload_timeout_releases_session_for_next_operation(session, tmp_path, monkeypatch):
    import ssh_control
    source = tmp_path/'upload'
    source.write_bytes(b'x' * (4 * 1024 * 1024))
    monkeypatch.setattr(ssh_control, 'CMD_TIMEOUT', .2)
    processes = local_processes(session, monkeypatch, 'import time; time.sleep(10)')
    with pytest.raises(TransferTimeoutError):
        session.upload(str(source), '/remote')
    assert not session._procs
    assert_closed_and_reaped(processes[0])
    assert session.submit(lambda: 'next operation').result(timeout=2) == 'next operation'


def test_abort_discards_old_queue_but_allows_new_tasks(session):
    import threading
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(3)
    running = session.submit(blocker)
    assert entered.wait(1)
    ran = []
    queued = session.submit(lambda: ran.append('stale'))
    try:
        session.abort()
        release.set()
        running.result(timeout=2)
        with pytest.raises(TransferCancelledError):
            queued.result(timeout=2)
        session.submit(lambda: ran.append('fresh')).result(timeout=2)
        assert ran == ['fresh']
    finally:
        release.set()


def test_abort_running_download_is_prompt_and_next_job_can_run(session, tmp_path, monkeypatch):
    from ssh_session import RemoteEntry
    import threading
    monkeypatch.setattr(session, 'stat', lambda _: RemoteEntry('file', '/remote', False))
    processes = local_processes(session, monkeypatch, 'import time; time.sleep(10)')
    entered = threading.Event()
    original = session._spawn
    def spawn_and_signal(*args, **kwargs):
        proc = original(*args, **kwargs)
        entered.set()
        return proc
    monkeypatch.setattr(session, '_spawn', spawn_and_signal)
    future = session.submit(session.download, '/remote', str(tmp_path/'target'))
    assert entered.wait(1)
    start = time.monotonic()
    session.abort()
    with pytest.raises(TransferCancelledError):
        future.result(timeout=2)
    assert time.monotonic() - start < 1
    assert not session._procs
    assert_closed_and_reaped(processes[0])
    assert session.submit(lambda: 123).result(timeout=2) == 123


def test_tar_upload_uses_streaming_pipes_and_extracts_correctly(session, tmp_path, monkeypatch):
    source = tmp_path/'source'
    source.write_bytes(b'payload' * 50000)
    remote = tmp_path/'remote'
    def start(command, **kwargs):
        proc = subprocess.Popen(['/bin/sh', '-c', command],
                                stdin=kwargs.get('stdin', subprocess.DEVNULL),
                                stdout=kwargs.get('stdout', subprocess.PIPE), stderr=subprocess.PIPE)
        proc._stellar_generation = session._abort_generation
        with session._procs_lock:
            session._procs.add(proc)
        return proc
    monkeypatch.setattr(session, '_spawn', start)
    session._stream_tar(str(remote), lambda tf, notify: tf.add(source, arcname='file with space'))
    assert (remote/'file with space').read_bytes() == source.read_bytes()
    assert not session._procs
