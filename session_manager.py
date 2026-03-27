"""
会话管理模块
负责会话的创建、存储、加载和管理
"""
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict

from utils import (
    get_sessions_dir,
    generate_session_id,
    extract_file_paths,
    format_timestamp
)


@dataclass
class SessionEntry:
    """会话条目"""
    type: str  # 'input' 或 'output'
    content: str
    timestamp: str
    files: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SessionEntry':
        # Filter to known fields only for forward compatibility
        known = {'type', 'content', 'timestamp', 'files'}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Session:
    """会话数据"""
    session_id: str
    start_time: str
    command: str
    entries: List[SessionEntry] = field(default_factory=list)
    end_time: Optional[str] = None
    working_directory: str = ""

    def to_dict(self) -> dict:
        return {
            'session_id': self.session_id,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'command': self.command,
            'working_directory': self.working_directory,
            'entries': [e.to_dict() for e in self.entries]
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Session':
        entries = [SessionEntry.from_dict(e) for e in data.get('entries', [])]
        return cls(
            session_id=data.get('session_id', ''),
            start_time=data.get('start_time', ''),
            end_time=data.get('end_time'),
            command=data.get('command', ''),
            working_directory=data.get('working_directory', ''),
            entries=entries
        )

    def get_all_files(self) -> List[str]:
        """获取会话中所有引用的文件"""
        files = set()
        for entry in self.entries:
            files.update(entry.files)
        return list(files)


class SessionManager:
    """会话管理器"""

    def __init__(self):
        self.sessions_dir = get_sessions_dir()
        self.current_session: Optional[Session] = None

    def create_session(self, command: str = "claude") -> Session:
        """创建新会话"""
        import os
        session = Session(
            session_id=generate_session_id(),
            start_time=format_timestamp(),
            command=command,
            working_directory=os.getcwd()
        )
        self.current_session = session
        return session

    def add_input(self, content: str) -> SessionEntry:
        """添加输入记录"""
        if not self.current_session:
            raise RuntimeError("No active session")

        files = list(extract_file_paths(content))
        entry = SessionEntry(
            type='input',
            content=content,
            timestamp=format_timestamp(),
            files=files
        )
        self.current_session.entries.append(entry)
        return entry

    def add_output(self, content: str) -> SessionEntry:
        """添加输出记录 - 合并连续的输出（优化：延迟文件路径提取）"""
        if not self.current_session:
            raise RuntimeError("No active session")

        # 性能优化：输出时不提取文件路径，减少 I/O 操作
        # 文件路径将在会话结束时统一提取

        # 如果上一个条目也是output，合并到一起
        if (self.current_session.entries and
            self.current_session.entries[-1].type == 'output'):
            last_entry = self.current_session.entries[-1]
            last_entry.content += content
            # 性能优化：合并时不更新时间戳，减少函数调用
            return last_entry

        # 否则创建新条目（不提取文件路径）
        entry = SessionEntry(
            type='output',
            content=content,
            timestamp=format_timestamp(),
            files=[]  # 延迟提取
        )
        self.current_session.entries.append(entry)
        return entry

    def end_session(self) -> Optional[Session]:
        """结束当前会话并保存"""
        if not self.current_session:
            return None

        self.current_session.end_time = format_timestamp()

        # 延迟处理：在会话结束时提取输出中的文件路径
        for entry in self.current_session.entries:
            if entry.type == 'output' and not entry.files:
                # 只对输出条目提取文件路径
                entry.files = list(extract_file_paths(entry.content))

        self.save_session(self.current_session)
        session = self.current_session
        self.current_session = None
        return session

    _SESSION_ID_RE = re.compile(r'^[\w\-]+$')

    def _validate_session_id(self, session_id: str) -> bool:
        """Validate session_id to prevent path traversal."""
        return bool(self._SESSION_ID_RE.match(session_id))

    def save_session(self, session: Session) -> Path:
        """保存会话到文件（原子写入防止损坏）"""
        file_path = self.sessions_dir / f"{session.session_id}.json"
        # Write to a temp file first, then atomically replace
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.sessions_dir, suffix='.tmp', prefix='.session_'
            )
            try:
                with open(fd, 'w', encoding='utf-8') as f:
                    json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
                Path(tmp_path).replace(file_path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except OSError:
            # Fallback to direct write if temp file fails
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
        return file_path

    def load_session(self, session_id: str) -> Optional[Session]:
        """加载指定会话"""
        if not self._validate_session_id(session_id):
            return None

        file_path = self.sessions_dir / f"{session_id}.json"
        if not file_path.exists():
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return Session.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def list_sessions(self) -> List[Dict[str, Any]]:
        """列出所有会话（摘要信息）"""
        sessions = []
        for file_path in sorted(self.sessions_dir.glob("*.json"), reverse=True):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                sessions.append({
                    'session_id': data['session_id'],
                    'start_time': data['start_time'],
                    'end_time': data.get('end_time', 'N/A'),
                    'command': data['command'],
                    'entry_count': len(data.get('entries', []))
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return sessions

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话"""
        if not self._validate_session_id(session_id):
            return False

        file_path = self.sessions_dir / f"{session_id}.json"
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    def auto_save(self):
        """自动保存当前会话（不结束）"""
        if self.current_session:
            self.save_session(self.current_session)
