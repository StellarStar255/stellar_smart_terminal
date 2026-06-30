"""
Persistent global favorites ("shortcuts") for the local Explorer.

Stored as a JSON array of absolute paths next to the app config:
  [ "/abs/path", ... ]

API is module-level (one shared store). Mutations write to disk
immediately, and the in-process cache is shared across all windows so
adding a shortcut in one window is visible in another right away.

Mirrors remote_bookmarks.py, simplified to a flat list (no host
dimension): local favorites are global, not scoped to a workspace.
"""
import json
import threading
from typing import Optional

from utils import get_data_dir


_PATH = get_data_dir() / ".smart_terminal_explorer_favorites.json"
_lock = threading.Lock()
_cache: Optional[list[str]] = None


def _load() -> list[str]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with _PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 规范化：必须是字符串列表，去掉空项与重复
        seen: set[str] = set()
        cleaned: list[str] = []
        if isinstance(data, list):
            for p in data:
                if isinstance(p, str) and p and p not in seen:
                    seen.add(p)
                    cleaned.append(p)
        _cache = cleaned
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _cache = []
    return _cache


def _save() -> None:
    if _cache is None:
        return
    try:
        with _PATH.open("w", encoding="utf-8") as fh:
            json.dump(_cache, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def list_all() -> list[str]:
    with _lock:
        return list(_load())


def is_favorite(path: str) -> bool:
    with _lock:
        return path in _load()


def add(path: str) -> None:
    with _lock:
        entries = _load()
        if path and path not in entries:
            entries.append(path)
            _save()


def remove(path: str) -> None:
    with _lock:
        entries = _load()
        if path in entries:
            entries.remove(path)
            _save()


def clear() -> None:
    with _lock:
        entries = _load()
        if entries:
            entries.clear()
            _save()
