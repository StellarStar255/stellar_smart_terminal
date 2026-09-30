from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ssh_session import SSHSession, HostConfig


@pytest.mark.parametrize('method', ['download', 'download_with_progress'])
@pytest.mark.parametrize('failure', [False, True])
def test_sftp_download_preserves_existing_part_and_destination(tmp_path, method, failure):
    local = tmp_path / 'download.bin'
    part = Path(str(local) + '.part')
    local.write_bytes(b'original')
    part.write_bytes(b'unrelated partial download')

    class SFTP:
        def stat(self, remote):
            return SimpleNamespace(st_size=8, st_mtime=123)

        def getfo(self, remote, stream, callback=None):
            assert remote == '/remote.bin'
            stream.write(b'new data')
            if failure:
                raise RuntimeError('transfer interrupted')
            if callback:
                callback(8, 8)

    session = SSHSession(HostConfig(alias='test', hostname='test'))
    session._sftp = SFTP()
    try:
        if failure:
            with pytest.raises(RuntimeError, match='interrupted'):
                getattr(session, method)('/remote.bin', str(local))
        else:
            getattr(session, method)('/remote.bin', str(local))
        assert local.read_bytes() == (b'original' if failure else b'new data')
        assert part.read_bytes() == b'unrelated partial download'
        assert sorted(p.name for p in tmp_path.iterdir()) == ['download.bin', 'download.bin.part']
    finally:
        session._executor.shutdown()


def test_sftp_local_publication_failure_preserves_original(tmp_path):
    local = tmp_path / 'file'
    local.write_bytes(b'original')
    session = SSHSession(HostConfig(alias='test', hostname='test'))
    session._sftp = SimpleNamespace(stat=lambda p: None,
                                   getfo=lambda p, f, callback=None: f.write(b'new'))
    try:
        with patch('file_persistence.os.replace', side_effect=PermissionError('denied')):
            with pytest.raises(PermissionError):
                session.download('/file', str(local))
        assert local.read_bytes() == b'original'
        assert list(tmp_path.iterdir()) == [local]
    finally:
        session._executor.shutdown()
