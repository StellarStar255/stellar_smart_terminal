"""session_manager 的录制与保存行为测试

重点覆盖长会话录制路径：分段追加（替代 O(n²) 的 += 拼接）、
滚动截断上限、后台 auto_save 的脏检查与写入顺序。
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_manager as sm_mod
from session_manager import SessionEntry, SessionManager


@pytest.fixture
def manager(tmp_path):
    sm = SessionManager()
    sm.sessions_dir = tmp_path
    yield sm
    sm._save_executor.shutdown(wait=True)


def _wait_saved(manager, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        f = manager._pending_save
        if f is None or f.done():
            return
        time.sleep(0.01)
    raise TimeoutError("auto_save did not finish")


class TestSessionEntryContent:
    def test_append_and_read(self):
        entry = SessionEntry(type='output', content='a', timestamp='t')
        entry.append_content('b')
        entry.append_content('c')
        assert entry.content == 'abc'
        # 读取后再追加仍正确
        entry.append_content('d')
        assert entry.content == 'abcd'
        assert entry.content_length == 4

    def test_content_setter_resets(self):
        entry = SessionEntry(type='output', content='xyz', timestamp='t')
        entry.append_content('123')
        entry.content = 'new'
        assert entry.content == 'new'
        assert entry.content_length == 3

    def test_rolling_truncation(self, monkeypatch):
        monkeypatch.setattr(SessionEntry, 'MAX_CONTENT_CHARS', 10)
        entry = SessionEntry(type='output', content='aaaa', timestamp='t')
        entry.append_content('bbbb')
        entry.append_content('cccc')  # 总 12 > 10，最旧分段 aaaa 被丢弃
        assert entry.content == SessionEntry.TRUNCATION_MARK + 'bbbbcccc'
        assert entry.content_length == 8

    def test_single_oversized_part_sliced(self, monkeypatch):
        monkeypatch.setattr(SessionEntry, 'MAX_CONTENT_CHARS', 10)
        entry = SessionEntry(type='output', content='', timestamp='t')
        entry.append_content('x' * 25)
        assert entry.content == SessionEntry.TRUNCATION_MARK + 'x' * 10

    def test_to_dict_round_trip(self):
        entry = SessionEntry(type='output', content='a', timestamp='t', files=['f'])
        entry.append_content('b')
        data = entry.to_dict()
        assert data == {'type': 'output', 'content': 'ab',
                        'timestamp': 't', 'files': ['f']}
        restored = SessionEntry.from_dict(data)
        assert restored.content == 'ab'
        assert restored.files == ['f']

    def test_from_dict_ignores_unknown_fields(self):
        restored = SessionEntry.from_dict(
            {'type': 'input', 'content': 'c', 'timestamp': 't',
             'files': [], 'future_field': 1})
        assert restored.content == 'c'

    def test_many_appends_are_fast(self):
        # += 拼接在 20k 段 × 1KB 下会走向分钟级；分段 append 应在亚秒完成
        entry = SessionEntry(type='output', content='', timestamp='t')
        chunk = 'x' * 1024
        start = time.monotonic()
        for _ in range(20000):
            entry.append_content(chunk)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0
        # 总量 20MB 超过 10MB 上限，滚动截断应生效
        assert entry.content_length <= SessionEntry.MAX_CONTENT_CHARS
        assert entry.content.startswith(SessionEntry.TRUNCATION_MARK)


class TestRecording:
    def test_consecutive_outputs_merge(self, manager):
        manager.create_session('cmd')
        e1 = manager.add_output('hello ')
        e2 = manager.add_output('world')
        assert e1 is e2
        assert len(manager.current_session.entries) == 1
        assert e1.content == 'hello world'

    def test_input_breaks_merge(self, manager):
        manager.create_session('cmd')
        manager.add_output('out1')
        manager.add_input('ls /tmp')
        manager.add_output('out2')
        entries = manager.current_session.entries
        assert [e.type for e in entries] == ['output', 'input', 'output']

    def test_revision_increments(self, manager):
        session = manager.create_session('cmd')
        assert session.revision == 0
        manager.add_output('a')
        manager.add_output('b')  # 合并也应递增
        manager.add_input('c')
        assert session.revision == 3

    def test_end_session_extracts_files_and_saves(self, manager, tmp_path):
        manager.create_session('cmd')
        manager.add_output('see /etc/hosts for details')
        session = manager.end_session()
        assert manager.current_session is None
        manager.flush()  # 收尾（路径提取 + 落盘）在后台线程
        saved = json.loads(
            (tmp_path / f"{session.session_id}.json").read_text(encoding='utf-8'))
        assert saved['entries'][0]['content'] == 'see /etc/hosts for details'
        assert saved['end_time']


class TestAutoSave:
    def test_auto_save_writes_in_background(self, manager, tmp_path):
        session = manager.create_session('cmd')
        manager.add_output('data')
        manager.auto_save()
        _wait_saved(manager)
        saved = json.loads(
            (tmp_path / f"{session.session_id}.json").read_text(encoding='utf-8'))
        assert saved['entries'][0]['content'] == 'data'
        assert 'revision' not in saved  # 内部字段不落盘

    def test_auto_save_skips_when_unchanged(self, manager):
        manager.create_session('cmd')
        manager.add_output('data')
        manager.auto_save()
        _wait_saved(manager)
        manager._pending_save = None
        manager.auto_save()  # 无新内容
        assert manager._pending_save is None

    def test_auto_save_resumes_after_change(self, manager, tmp_path):
        session = manager.create_session('cmd')
        manager.add_output('one')
        manager.auto_save()
        _wait_saved(manager)
        manager.add_output(' two')
        manager.auto_save()
        _wait_saved(manager)
        manager._wait_pending_save()
        saved = json.loads(
            (tmp_path / f"{session.session_id}.json").read_text(encoding='utf-8'))
        assert saved['entries'][0]['content'] == 'one two'

    def test_sync_save_waits_for_pending(self, manager, tmp_path):
        """end_session 的同步保存不能被在途的旧快照覆盖"""
        session = manager.create_session('cmd')
        manager.add_output('old')
        manager.auto_save()
        manager.add_output(' new')
        manager.end_session()
        manager.flush()
        saved = json.loads(
            (tmp_path / f"{session.session_id}.json").read_text(encoding='utf-8'))
        assert saved['entries'][0]['content'] == 'old new'

    def test_auto_save_without_session_is_noop(self, manager):
        manager.auto_save()
        assert manager._pending_save is None


class TestListSessionsCache:
    """list_sessions 摘要缓存：未变的会话文件不重新整份解析"""

    @staticmethod
    def _write_session(manager, sid, command='claude'):
        data = {
            'session_id': sid,
            'start_time': 't0',
            'command': command,
            'entries': [{'type': 'input', 'content': 'x', 'timestamp': 't'}],
        }
        path = manager.sessions_dir / f"{sid}.json"
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_unchanged_files_not_reparsed(self, manager, monkeypatch):
        self._write_session(manager, 's1')
        self._write_session(manager, 's2')
        first = manager.list_sessions()
        assert {s['session_id'] for s in first} == {'s1', 's2'}

        calls = []
        real_load = json.load
        monkeypatch.setattr(sm_mod.json, 'load',
                            lambda f: (calls.append(1), real_load(f))[1])
        second = manager.list_sessions()
        assert second == first
        assert calls == []  # 全部命中缓存，零次解析

    def test_modified_file_reparsed(self, manager):
        self._write_session(manager, 's1', command='old')
        assert manager.list_sessions()[0]['command'] == 'old'
        self._write_session(manager, 's1', command='newer-cmd')
        assert manager.list_sessions()[0]['command'] == 'newer-cmd'

    def test_deleted_file_pruned_from_cache(self, manager):
        path = self._write_session(manager, 's1')
        manager.list_sessions()
        assert 's1.json' in manager._summary_cache
        path.unlink()
        assert manager.list_sessions() == []
        assert 's1.json' not in manager._summary_cache

    def test_returned_dict_mutation_does_not_pollute_cache(self, manager):
        self._write_session(manager, 's1')
        result = manager.list_sessions()
        result[0]['command'] = 'hacked'
        assert manager.list_sessions()[0]['command'] == 'claude'
