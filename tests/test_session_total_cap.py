"""会话总量上限与自动保存序列化开销的回归测试

背景：会话录制无总量上限，长会话（如跑一整天 Claude）内存与每 30s 的
全量 json 重写随总输出线性增长；indent=2 还使 json.dump 走纯 Python
编码器（比 C 编码器慢 3~5 倍）。修复：
1. add_input/add_output 后滚动丢弃最旧条目，总量不超过 MAX_SESSION_CHARS；
2. 会话文件改为紧凑 JSON（indent=None + 紧凑分隔符，C 编码器）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_manager import Session, SessionManager


@pytest.fixture
def manager(tmp_path):
    sm = SessionManager()
    sm.sessions_dir = tmp_path
    yield sm
    sm._save_executor.shutdown(wait=True)


def _total_chars(session):
    return sum(e.content_length for e in session.entries)


class TestSessionTotalCap:
    def test_cap_constant_exists(self):
        assert SessionManager.MAX_SESSION_CHARS > 0

    def test_output_growth_capped(self, manager, monkeypatch):
        """持续 add_output 超过总上限后，最旧条目被滚动丢弃"""
        monkeypatch.setattr(SessionManager, 'MAX_SESSION_CHARS', 10000)
        manager.create_session('claude')
        # 交替 input/output，制造多个条目（连续 output 会合并进同一条目）
        for i in range(50):
            manager.add_input(f'cmd {i}\n')
            manager.add_output('x' * 500)
        session = manager.current_session
        assert _total_chars(session) <= 10000
        # 最旧的条目应已被丢弃（第一条 input 不再是 cmd 0）
        first_input = next(e for e in session.entries if e.type == 'input')
        assert first_input.content != 'cmd 0\n'
        # 会话被标记为截断过
        assert session.truncated is True

    def test_input_growth_capped(self, manager, monkeypatch):
        monkeypatch.setattr(SessionManager, 'MAX_SESSION_CHARS', 5000)
        manager.create_session('claude')
        for i in range(100):
            manager.add_input('y' * 200)
        assert _total_chars(manager.current_session) <= 5000

    def test_under_cap_untouched(self, manager):
        """未超上限时条目一个不丢、不打截断标记"""
        manager.create_session('claude')
        for i in range(5):
            manager.add_input(f'cmd {i}\n')
            manager.add_output('out' * 10)
        session = manager.current_session
        assert len(session.entries) == 10
        assert session.truncated is False

    def test_last_entry_never_dropped(self, manager, monkeypatch):
        """即使单个条目超过总上限也至少保留它（条目级 10MB 截断兜底）"""
        monkeypatch.setattr(SessionManager, 'MAX_SESSION_CHARS', 100)
        manager.create_session('claude')
        manager.add_output('z' * 500)
        session = manager.current_session
        assert len(session.entries) == 1
        assert session.entries[0].content_length == 500

    def test_truncated_flag_roundtrip(self, manager, monkeypatch):
        """truncated 标记随保存/加载往返；旧文件无此字段时默认 False"""
        monkeypatch.setattr(SessionManager, 'MAX_SESSION_CHARS', 1000)
        manager.create_session('claude')
        for i in range(20):
            manager.add_input('w' * 200)
        manager.end_session()
        manager.flush()  # 落盘在后台线程
        loaded = manager.load_session(
            json.loads(next(manager.sessions_dir.glob('*.json')).read_text())['session_id'])
        assert loaded.truncated is True
        # 旧格式（无 truncated 字段）
        old = Session.from_dict({'session_id': 's', 'start_time': 't',
                                 'command': 'c', 'entries': []})
        assert old.truncated is False


class TestCompactJson:
    def test_saved_file_is_compact(self, manager):
        """会话文件不再带 indent 缩进（走 C 编码器，序列化快 3~5 倍）"""
        manager.create_session('claude')
        manager.add_input('hello\n')
        manager.add_output('world\n')
        path = manager.save_session(manager.current_session)
        text = path.read_text(encoding='utf-8')
        # 紧凑格式：无缩进换行
        assert '\n  "' not in text
        # 内容仍完整可读
        data = json.loads(text)
        assert data['entries'][0]['content'] == 'hello\n'
