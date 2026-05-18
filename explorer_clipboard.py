"""
Shared clipboard for Explorer / Remote Explorer copy-paste.

Holds a list of items the user has "copied" from a Local or Remote Explorer
panel. Any panel (in any window) can paste from this clipboard into its
current target directory.

Item shape:
  ("local",  abs_path)                          # local filesystem
  ("remote", host_alias, remote_path, session)  # SFTP-reachable

The SSH session is held by strong reference so it stays usable for the
paste even after the source panel switches host. Callers should still
check `session.is_connected()` before relying on it — the user may have
disconnected since copying.
"""
from typing import Any


_items: list[tuple] = []


def set_items(items: list[tuple]) -> None:
    global _items
    _items = list(items or [])


def get_items() -> list[tuple]:
    return list(_items)


def clear() -> None:
    global _items
    _items = []


def has_items() -> bool:
    return bool(_items)


def describe() -> str:
    """Short label for menus / tooltips, e.g. 'foo.txt (+2 more)'."""
    if not _items:
        return ""
    names: list[str] = []
    for it in _items:
        if it[0] == "local":
            p = it[1]
            names.append(p.rstrip("/").rsplit("/", 1)[-1] or p)
        elif it[0] == "remote":
            p = it[2]
            names.append(p.rstrip("/").rsplit("/", 1)[-1] or p)
    if len(names) == 1:
        return names[0]
    return f"{names[0]} (+{len(names) - 1} more)"
