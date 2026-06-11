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
import re
from typing import Callable, Optional


# ---------- 命名冲突自动加序号尾缀（同文件夹复制粘贴时用）----------

# "foo (3).py" / ".tar.gz" 这种已存在的 "(N)" 后缀检测：避免重复粘贴时
# 累积成 "foo (1) (1) (1).py"，而是直接从 N+1 继续。
_SUFFIX_RE = re.compile(r"^(?P<base>.+) \((?P<n>\d+)\)$")


def _split_basename(name: str) -> tuple[str, str]:
    """把 'foo.tar.gz' 拆成 ('foo.tar', '.gz')；仅按最后一个点切分，
    和 macOS Finder/VS Code 的行为一致。隐藏文件 (.bashrc) 不拆。"""
    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    idx = name.rfind(".")
    if idx <= 0:
        return name, ""
    return name[:idx], name[idx:]


def next_free_name(basename: str, exists_fn: Callable[[str], bool]) -> str:
    """在调用方提供的命名空间里挑下一个不冲突的名字。

    exists_fn(candidate_name) → 用 os.path.exists / SFTP stat 判断；
    本地和远端粘贴都能用同一段逻辑。

    'foo.png'      → 'foo.png'（不冲突就原样返回）
                  → 'foo (1).png' → 'foo (2).png' ...
    'foo (3).png'  → 'foo (4).png'（识别已有后缀，避免堆叠）
    """
    if not exists_fn(basename):
        return basename
    stem, ext = _split_basename(basename)
    m = _SUFFIX_RE.match(stem)
    base_stem = m.group("base") if m else stem
    i = (int(m.group("n")) + 1) if m else 1
    while i < 10_000:
        cand = f"{base_stem} ({i}){ext}"
        if not exists_fn(cand):
            return cand
        i += 1
    # 兜底：极端情况返回带时间戳的名字，避免死循环
    import time as _time
    return f"{base_stem}-{int(_time.time())}{ext}"


_items: list[tuple] = []
_cut = False  # True → Paste 按"移动"处理（Cmd+X 剪切）
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


def set_items(items: list[tuple], push_local_paths: Optional[list[str]] = None,
              cut: bool = False) -> None:
    """Set internal clipboard. If push_local_paths is given, mirror those to OS clipboard.

    If push_local_paths is None we don't touch the system clipboard, but we
    snapshot what's currently on it so future paste calls can detect if an
    external app has overwritten it.

    cut=True 表示剪切（Cmd+X）：随后的 Paste 应把来源**移动**到目标处。
    """
    global _items, _last_system_urls, _cut
    _items = list(items or [])
    _cut = bool(cut)
    if push_local_paths is not None:
        _push_to_system(push_local_paths)
        _last_system_urls = _norm(push_local_paths)
    else:
        _last_system_urls = _norm(_system_urls())


def clear() -> None:
    global _items, _last_system_urls, _cut
    _items = []
    _cut = False
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


def is_cut() -> bool:
    """Paste 是否应按"移动"处理。外部应用覆盖系统剪贴板后，剪切语义随之失效
    （此时 Paste 的来源已是外部内容，按复制处理）。"""
    return _cut and not _system_was_updated_externally()


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
