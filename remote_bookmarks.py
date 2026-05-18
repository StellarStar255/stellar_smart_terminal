"""
Persistent per-host bookmarks for Remote Explorer paths.

Stored as JSON next to the app's config file:
  { host_alias: [absolute_path, ...] }

API is module-level (one shared store). Mutations write to disk
immediately so cross-window state stays consistent.
"""
import json
import threading
from pathlib import Path
from typing import Optional


_PATH = Path(__file__).parent / ".smart_terminal_remote_bookmarks.json"
_lock = threading.Lock()
_cache: Optional[dict[str, list[str]]] = None


def _load() -> dict[str, list[str]]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with _PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
        # 规范化：值必须是字符串列表
        cleaned: dict[str, list[str]] = {}
        for host, paths in data.items():
            if isinstance(host, str) and isinstance(paths, list):
                cleaned[host] = [p for p in paths if isinstance(p, str) and p]
        _cache = cleaned
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _cache = {}
    return _cache


def _save() -> None:
    if _cache is None:
        return
    try:
        with _PATH.open("w", encoding="utf-8") as fh:
            json.dump(_cache, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def list_for(host: str) -> list[str]:
    with _lock:
        return list(_load().get(host, []))


def is_bookmarked(host: str, path: str) -> bool:
    with _lock:
        return path in _load().get(host, [])


def add(host: str, path: str) -> None:
    with _lock:
        data = _load()
        entries = data.setdefault(host, [])
        if path not in entries:
            entries.append(path)
            _save()


def remove(host: str, path: str) -> None:
    with _lock:
        data = _load()
        entries = data.get(host)
        if entries and path in entries:
            entries.remove(path)
            if not entries:
                data.pop(host, None)
            _save()


def clear_for(host: str) -> None:
    with _lock:
        data = _load()
        if data.pop(host, None) is not None:
            _save()
