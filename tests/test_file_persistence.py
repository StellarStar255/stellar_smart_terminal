"""Failure injection for the shared persistence boundary; no GUI required."""
import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_persistence as persistence
import utils


@pytest.mark.parametrize('failure', ['create', 'open', 'write', 'fsync', 'replace'])
def test_failure_preserves_destination(tmp_path, monkeypatch, failure):
    target = tmp_path / 'data'
    target.write_bytes(b'original')

    def fail(*args, **kwargs):
        raise OSError('injected failure')

    if failure == 'create':
        monkeypatch.setattr(persistence.tempfile, 'mkstemp', fail)
    elif failure in ('open', 'fsync', 'replace'):
        name = 'fdopen' if failure == 'open' else failure
        monkeypatch.setattr(persistence.os, name, fail)
    with pytest.raises(OSError, match='injected failure'):
        with persistence.atomic_writer(target, binary=True) as stream:
            stream.write(b'partial')
            if failure == 'write':
                fail()
    assert target.read_bytes() == b'original'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['data']


def test_destination_changes_only_after_context_completes(tmp_path):
    target = tmp_path / 'data'
    target.write_text('old', encoding='utf-8')
    with persistence.atomic_writer(target) as stream:
        stream.write('新内容')
        assert target.read_text(encoding='utf-8') == 'old'
    assert target.read_text(encoding='utf-8') == '新内容'
    if os.name == 'posix':
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != 'posix', reason='POSIX permissions and symlinks')
def test_editor_semantics_preserve_link_and_executable_mode(tmp_path):
    target = tmp_path / 'script'
    target.write_bytes(b'old')
    target.chmod(0o755)
    link = tmp_path / 'link'
    link.symlink_to(target)
    with persistence.atomic_writer(link, binary=True, preserve_mode=True,
                                   follow_symlinks=True) as stream:
        stream.write(b'new')
    assert link.is_symlink()
    assert target.read_bytes() == b'new'
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_interruption_cleans_temp_and_keeps_original(tmp_path):
    target = tmp_path / 'data'
    target.write_bytes(b'old')
    with pytest.raises(KeyboardInterrupt):
        with persistence.atomic_writer(target) as stream:
            stream.write('partial')
            raise KeyboardInterrupt
    assert target.read_bytes() == b'old'
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('failure', ['write', 'replace'])
def test_config_failure_returns_false_and_keeps_original(tmp_path, monkeypatch, failure):
    target = tmp_path / 'config.json'
    target.write_text('{"key":"original"}', encoding='utf-8')
    original = target.read_bytes()

    def partial_dump(data, stream, **kwargs):
        stream.write('{"partial":')
        raise OSError('disk full')

    def failed_replace(*args):
        raise PermissionError('destination locked')

    if failure == 'write':
        monkeypatch.setattr(utils.json, 'dump', partial_dump)
    else:
        monkeypatch.setattr(persistence.os, 'replace', failed_replace)
    assert utils.atomic_write_json(target, {'key': 'new'}) is False
    assert target.read_bytes() == original
    assert len(list(tmp_path.iterdir())) == 1


def test_hash_spans_multiple_chunks(tmp_path):
    data = b'x' * (512 * 1024 + 7)
    target = tmp_path / 'data'
    target.write_bytes(data)
    assert persistence.file_sha256(target) == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_windows_transient_replace_error_retries_complete_file(tmp_path, monkeypatch, winerror):
    target = tmp_path / 'data'
    target.write_bytes(b'original')
    replace = persistence.os.replace
    calls, delays = [], []
    def interrupted(source, dest):
        calls.append((source, dest))
        assert target.read_bytes() == b'original'
        if len(calls) < 3:
            exc = PermissionError('transient Windows sharing violation')
            exc.winerror = winerror
            raise exc
        replace(source, dest)
    monkeypatch.setattr(persistence, '_IS_WINDOWS', True)
    monkeypatch.setattr(persistence.os, 'replace', interrupted)
    monkeypatch.setattr(persistence.time, 'sleep', delays.append)
    with persistence.atomic_writer(target, binary=True) as stream:
        stream.write(b'complete replacement')
    assert target.read_bytes() == b'complete replacement'
    assert len(calls) == 3
    assert delays == [.01, .02]
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('windows,code,attempts', [(True, 5, 6), (True, 112, 1), (False, 5, 1)])
def test_replace_retry_is_bounded_and_failure_preserves_original(tmp_path, monkeypatch, windows, code, attempts):
    target = tmp_path / 'data'
    target.write_bytes(b'original')
    calls = []
    def denied(*args):
        calls.append(args)
        exc = PermissionError('replacement denied')
        exc.winerror = code
        raise exc
    monkeypatch.setattr(persistence, '_IS_WINDOWS', windows)
    monkeypatch.setattr(persistence.os, 'replace', denied)
    monkeypatch.setattr(persistence.time, 'sleep', lambda _: None)
    with pytest.raises(PermissionError):
        with persistence.atomic_writer(target, binary=True) as stream:
            stream.write(b'new')
    assert len(calls) == attempts
    assert target.read_bytes() == b'original'
    assert list(tmp_path.iterdir()) == [target]
