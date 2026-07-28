"""
会话管理模块
负责会话的创建、存储、加载和管理
"""
import json
import re
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

from app_logging import get_logger
from utils import (
    get_sessions_dir,
    generate_session_id,
    extract_file_paths,
    format_timestamp
)

logger = get_logger(__name__)


class SessionEntry:
    """会话条目

    content 内部按分段列表存储：追加只做 list.append，读取时才 join。
    连续输出合并到同一条目时，`+=` 会把整串复制一遍（总代价 O(n²)），
    长会话大输出下会拖死 GUI 线程，故必须走 append_content。
    """

    # 单条目内容上限（字符数），超过后从最旧内容开始滚动截断
    MAX_CONTENT_CHARS = 10 * 1024 * 1024
    TRUNCATION_MARK = '[...内容过长，较早输出已截断...]\n'

    __slots__ = ('type', 'timestamp', 'files', '_parts', '_length', '_truncated')

    def __init__(self, type: str, content: str = '', timestamp: str = '',
                 files: Optional[List[str]] = None):
        self.type = type  # 'input' 或 'output'
        self.timestamp = timestamp
        self.files: List[str] = files if files is not None else []
        self._parts: List[str] = [content] if content else []
        self._length = len(content)
        self._truncated = False

    @property
    def content(self) -> str:
        if len(self._parts) > 1:
            # join 后合并为单段，缓存结果避免重复 join
            self._parts = [''.join(self._parts)]
        body = self._parts[0] if self._parts else ''
        if self._truncated:
            return self.TRUNCATION_MARK + body
        return body

    @content.setter
    def content(self, value: str):
        self._parts = [value] if value else []
        self._length = len(value)
        self._truncated = False

    @property
    def content_length(self) -> int:
        """当前内容长度（不触发 join）"""
        return self._length

    def append_content(self, text: str):
        """追加内容，超过上限时滚动丢弃最旧的分段"""
        self._parts.append(text)
        self._length += len(text)
        if self._length > self.MAX_CONTENT_CHARS:
            while self._length > self.MAX_CONTENT_CHARS and len(self._parts) > 1:
                removed = self._parts.pop(0)
                self._length -= len(removed)
                self._truncated = True
            if self._length > self.MAX_CONTENT_CHARS:
                kept = self._parts[0][-self.MAX_CONTENT_CHARS:]
                self._parts = [kept]
                self._length = len(kept)
                self._truncated = True

    def to_dict(self) -> dict:
        return {
            'type': self.type,
            'content': self.content,
            'timestamp': self.timestamp,
            'files': list(self.files),
        }

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
    # 单调递增的修订号，任何内容变更都会 +1，供 auto_save 跳过无变化的保存
    revision: int = field(default=0, compare=False)

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
        # auto_save 的序列化和磁盘写入放到后台线程，避免长会话时
        # 每 30 秒在 GUI 线程 json.dump 整个会话造成周期性卡顿
        self._save_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='session-save')
        self._pending_save: Optional[Future] = None
        self._last_saved_sig: Optional[tuple] = None
        # list_sessions 摘要缓存：文件名 -> ((mtime_ns, size), summary)。
        # 会话文件单个可达 10MB，摘要只需 5 个字段；不缓存的话每次打开
        # 历史对话框都要整文件 json.load 一遍所有会话。
        self._summary_cache: Dict[str, tuple] = {}

    @staticmethod
    def _session_signature(session: Session) -> tuple:
        """会话内容签名，用于跳过无变化的自动保存"""
        return (session.session_id, session.revision)

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
        self.current_session.revision += 1
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
            last_entry.append_content(content)
            self.current_session.revision += 1
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
        self.current_session.revision += 1
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
        """同步保存会话到文件（原子写入防止损坏）"""
        # 等待在途的后台保存，避免旧快照晚于新数据落盘
        self._wait_pending_save()
        data = session.to_dict()
        self._last_saved_sig = self._session_signature(session)
        return self._write_session_data(data, session.session_id)

    def _wait_pending_save(self, timeout: float = 10.0):
        future = self._pending_save
        self._pending_save = None
        if future is not None:
            try:
                future.result(timeout=timeout)
            except Exception:
                logger.debug("_wait_pending_save: suppressed exception", exc_info=True)

    def _write_session_data(self, data: dict, session_id: str) -> Path:
        file_path = self.sessions_dir / f"{session_id}.json"
        # Write to a temp file first, then atomically replace
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.sessions_dir, suffix='.tmp', prefix='.session_'
            )
            try:
                with open(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                Path(tmp_path).replace(file_path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except OSError:
            # Fallback to direct write if temp file fails
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
        """列出所有会话（摘要信息）；按 (mtime, size) 缓存，未变的文件不重新解析"""
        sessions = []
        seen = set()
        for file_path in sorted(self.sessions_dir.glob("*.json"), reverse=True):
            name = file_path.name
            try:
                st = file_path.stat()
            except OSError:
                continue
            seen.add(name)
            stamp = (st.st_mtime_ns, st.st_size)
            cached = self._summary_cache.get(name)
            if cached is not None and cached[0] == stamp:
                sessions.append(dict(cached[1]))
                continue
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                summary = {
                    'session_id': data['session_id'],
                    'start_time': data['start_time'],
                    'end_time': data.get('end_time', 'N/A'),
                    'command': data['command'],
                    'working_directory': data.get('working_directory', ''),
                    'entry_count': len(data.get('entries', []))
                }
            except (json.JSONDecodeError, KeyError):
                continue
            self._summary_cache[name] = (stamp, summary)
            sessions.append(dict(summary))
        # 文件已删除的缓存条目一并清掉
        for name in list(self._summary_cache):
            if name not in seen:
                del self._summary_cache[name]
        return sessions

    def search_sessions(self, query: str, max_results: int = 200,
                        per_session_limit: int = 3,
                        cancel_check=None) -> List[Dict[str, Any]]:
        """在全部历史会话的输入/输出里搜关键词（大小写不敏感子串）。

        - 匹配在 strip_ansi 之后的文本上进行：终端原始输出里的颜色转义会把
          关键词切碎（如 "cd \\x1b[32mx"），按原始串搜会漏；
        - 每个会话最多贡献 per_session_limit 条命中（避免一个 10MB 会话刷屏），
          总量 max_results 封顶；按文件名倒序 = 新会话优先；
        - cancel_check() 返回 True 时提前返回已得结果（后台线程可取消）。

        返回 [{'session_id','command','start_time','entry_type','snippet'}...]
        """
        from utils import strip_ansi
        q = (query or '').strip().lower()
        if not q:
            return []
        results: List[Dict[str, Any]] = []
        for file_path in sorted(self.sessions_dir.glob("*.json"), reverse=True):
            if len(results) >= max_results:
                break
            if cancel_check is not None and cancel_check():
                break
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            sid = data.get('session_id', '')
            command = data.get('command', '')
            start_time = data.get('start_time', '')
            hits = 0
            for entry in data.get('entries', []):
                if hits >= per_session_limit or len(results) >= max_results:
                    break
                raw = entry.get('content', '') or ''
                if not raw:
                    continue
                text = strip_ansi(raw)
                low = text.lower()
                i = low.find(q)
                if i < 0:
                    continue
                # 命中片段：前 40 / 后 60 字符，压成单行
                start = max(0, i - 40)
                snippet = text[start:i + len(q) + 60]
                snippet = ' '.join(snippet.split())
                results.append({
                    'session_id': sid,
                    'command': command,
                    'start_time': start_time,
                    'entry_type': entry.get('type', ''),
                    'snippet': snippet,
                })
                hits += 1
        return results

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
        """自动保存当前会话（不结束）

        序列化与写盘在后台线程执行；内容无变化或上一次写入未完成时跳过。
        """
        session = self.current_session
        if not session:
            return
        if self._pending_save is not None and not self._pending_save.done():
            return
        sig = self._session_signature(session)
        if sig == self._last_saved_sig:
            return
        # 快照必须在调用线程完成：to_dict 会读取 entries，
        # 后台线程只拿纯 dict/str，不再触碰会话对象
        data = session.to_dict()
        self._last_saved_sig = sig
        self._pending_save = self._save_executor.submit(
            self._save_in_background, data, session.session_id, sig)

    def _save_in_background(self, data: dict, session_id: str, sig: tuple):
        try:
            self._write_session_data(data, session_id)
        except Exception as e:
            logger.warning(f"session auto_save failed: {e}")
            # 写失败则撤销签名，让下一轮 auto_save 重试
            if self._last_saved_sig == sig:
                self._last_saved_sig = None
