"""A remote file growing after stat must still obey the comparison read cap."""
import io
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from ssh_session import SSHSession
from ssh_control import ControlMasterSession


@pytest.mark.parametrize('stat_fails', [False, True])
def test_sftp_read_is_bounded_even_when_size_is_unknown(stat_fails):
    requested = []
    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            requested.append(size)
            return super().read(size)
    def stat(path):
        if stat_fails:
            raise OSError('stat unavailable')
        return SimpleNamespace(st_size=0)
    sftp = SimpleNamespace(stat=stat, open=lambda *args: GrowingFile(b'x' * 100_000))
    session = SimpleNamespace(_require=lambda: sftp)
    with pytest.raises(ValueError, match='too large'):
        SSHSession.read_file.__wrapped__(session, '/growing', max_bytes=16)
    assert requested == [17]


@pytest.mark.skipif(os.name == 'nt', reason='ControlMaster requires POSIX shell')
def test_controlmaster_limits_bytes_at_source_after_stale_stat(tmp_path):
    path = tmp_path/"growing ' file"
    path.write_bytes(b'x' * 100_000)
    outputs = []
    def spawn(command):
        proc = subprocess.Popen(['/bin/sh', '-c', command], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        original = proc.communicate
        def communicate(**kwargs):
            out, err = original(**kwargs)
            outputs.append(len(out))
            return out, err
        proc.communicate = communicate
        return proc
    session = SimpleNamespace(stat=lambda _: SimpleNamespace(size=0), _lock=threading.Lock(),
                              _spawn=spawn, _reap=lambda _: None)
    with pytest.raises(ValueError, match='too large'):
        ControlMasterSession.read_file(session, str(path), max_bytes=16)
    assert outputs == [17]


@pytest.mark.parametrize('limit', [-2, -1, 1.5, '16'])
@pytest.mark.parametrize('backend', ['sftp', 'controlmaster'])
def test_invalid_limit_is_rejected_before_read(backend, limit):
    method = SSHSession.read_file.__wrapped__ if backend == 'sftp' else ControlMasterSession.read_file
    with pytest.raises(ValueError, match='nonnegative integer'):
        method(None, '/remote', max_bytes=limit)
