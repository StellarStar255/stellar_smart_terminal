"""
Shared clipboard for Explorer / Remote Explorer copy-paste.

Internal items
--------------
Stored as tuples so Local + Remote panels can both produce and consume them:
  ("local",  abs_path)                          # local filesystem
  ("remote", host_alias, remote_path, session)  # SFTP-reachable

The SSH session is held by strong reference so it stays usable for the
paste even after the source panel switches host. Callers should still
check `session.is_connected()` before relying on it.

System-clipboard bridging
-------------------------
Cmd+C in our panels also writes file URLs to the OS clipboard so the user
can paste into Finder / other apps. Conversely, Cmd+V honors external
file URLs that other apps (Finder, Chrome, etc.) put on the system
clipboard, treating them as local sources.

Tracking which is "newer" is done by snapshotting the system clipboard's
URLs at the moment we last set our internal state. On paste, if the
current system URLs differ from that snapshot, an external app has
updated the clipboard since — use those URLs. Otherwise prefer our
internal state (which may include remote info the OS can't represent).
"""
import os
from typing import Optional


_items: list[tuple] = []
_last_system_urls: list[str] = []  # abs paths we last saw/wrote on system clipboard


def _system_urls() -> list[str]:
    """Read current local file URLs off the OS clipboard. Empty if none."""
    from PyQt6.QtWidgets import QApplication
    cb = QApplication.clipboard()
    md = cb.mimeData()
    if md is None or not md.hasUrls():
        return []
    out: list[str] = []
    for u in md.urls():
        if u.isLocalFile():
            p = u.toLocalFile()
            if p:
                out.append(p)
    return out


def _norm(paths: list[str]) -> list[str]:
    return [os.path.abspath(p) for p in paths]


def _push_to_system(paths: list[str]) -> None:
    """Write paths as file URLs onto the OS clipboard."""
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QUrl, QMimeData
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    QApplication.clipboard().setMimeData(mime)


def set_items(items: list[tuple], push_local_paths: Optional[list[str]] = None) -> None:
    """Set internal clipboard. If push_local_paths is given, mirror those to OS clipboard.

    If push_local_paths is None we don't touch the system clipboard, but we
    snapshot what's currently on it so future paste calls can detect if an
    external app has overwritten it.
    """
    global _items, _last_system_urls
    _items = list(items or [])
    if push_local_paths is not None:
        _push_to_system(push_local_paths)
        _last_system_urls = _norm(push_local_paths)
    else:
        _last_system_urls = _norm(_system_urls())


def clear() -> None:
    global _items, _last_system_urls
    _items = []
    _last_system_urls = _norm(_system_urls())


def _system_was_updated_externally() -> bool:
    sys_urls = _system_urls()
    if not sys_urls:
        return False
    return _norm(sys_urls) != _last_system_urls


def effective_items() -> list[tuple]:
    """The items that Paste should act on.

    Returns external system URLs (as local items) if the OS clipboard was
    updated by another app since we last set ours; otherwise our internal
    items.
    """
    if _system_was_updated_externally():
        return [("local", p) for p in _system_urls()]
    return list(_items)


def has_items() -> bool:
    return bool(_items)


def has_pastable() -> bool:
    return bool(effective_items())


def describe() -> str:
    """Short label for menus / tooltips, e.g. 'foo.txt (+2 more)'."""
    items = effective_items()
    if not items:
        return ""
    names: list[str] = []
    for it in items:
        if it[0] == "local":
            p = it[1]
            names.append(p.rstrip("/").rsplit("/", 1)[-1] or p)
        elif it[0] == "remote":
            p = it[2]
            names.append(p.rstrip("/").rsplit("/", 1)[-1] or p)
    if len(names) == 1:
        return names[0]
    return f"{names[0]} (+{len(names) - 1} more)"
