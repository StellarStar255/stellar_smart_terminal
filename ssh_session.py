"""
SSH/SFTP session wrapper for Remote Explorer.

Design notes:
- One SSHSession per remote host. Holds a paramiko Transport + SFTPClient.
- All blocking SFTP calls are dispatched to a single-threaded executor so the
  Qt event loop never blocks. UI code submits a callable and gets back a
  QFuture-like wrapper that fires a Qt signal when done.
- ~/.ssh/config is parsed once at startup; we hand back a list of resolved
  host configs (with HostName/User/Port/IdentityFile already resolved) so the
  UI doesn't have to know about ssh_config quirks.
"""
import os
import posixpath
import stat
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import paramiko
from paramiko.config import SSHConfig

from PyQt6.QtCore import QObject, pyqtSignal


# ---------- ssh_config parsing ----------

@dataclass
class HostConfig:
    """已解析的 SSH 主机配置（来自 ~/.ssh/config 或手工添加）"""
    alias: str                       # ssh_config Host 名（用户看到的）
    hostname: str                    # 真实主机名/IP
    user: str = ""                   # 留空 → paramiko 用当前用户
    port: int = 22
    identity_file: Optional[str] = None  # 私钥路径
    proxy_jump: Optional[str] = None     # ProxyJump 跳板机别名
    raw: dict = field(default_factory=dict)  # 原始 ssh_config dict


def parse_ssh_config(path: Optional[str] = None) -> list[HostConfig]:
    """读 ~/.ssh/config，返回所有非通配符主机的解析后配置

    通配符条目（Host *、Host *.example.com 等）作为模板使用而不单列。
    """
    config_path = Path(path or os.path.expanduser("~/.ssh/config"))
    if not config_path.is_file():
        return []

    cfg = SSHConfig()
    try:
        with config_path.open("r", encoding="utf-8", errors="replace") as fh:
            cfg.parse(fh)
    except Exception:
        return []

    hosts: list[HostConfig] = []
    seen = set()
    for entry in cfg.get_hostnames():
        # 跳过纯通配符
        if "*" in entry or "?" in entry or "!" in entry:
            continue
        if entry in seen:
            continue
        seen.add(entry)
        resolved = cfg.lookup(entry)
        hostname = resolved.get("hostname", entry)
        user = resolved.get("user", "")
        try:
            port = int(resolved.get("port", 22))
        except (TypeError, ValueError):
            port = 22
        identity_files = resolved.get("identityfile") or []
        identity = identity_files[0] if identity_files else None
        if identity:
            identity = os.path.expanduser(identity)
        proxy_jump = resolved.get("proxyjump")
        hosts.append(HostConfig(
            alias=entry,
            hostname=hostname,
            user=user,
            port=port,
            identity_file=identity,
            proxy_jump=proxy_jump,
            raw=dict(resolved),
        ))
    hosts.sort(key=lambda h: h.alias.lower())
    return hosts


# ---------- SFTP entry ----------

@dataclass
class RemoteEntry:
    """远程文件/目录的元信息"""
    name: str
    path: str          # 绝对路径
    is_dir: bool
    is_link: bool = False
    size: int = 0
    mtime: float = 0.0
    mode: int = 0


def _attr_to_entry(parent: str, attr) -> RemoteEntry:
    name = attr.filename
    path = parent.rstrip("/") + "/" + name if parent != "/" else "/" + name
    mode = attr.st_mode or 0
    return RemoteEntry(
        name=name,
        path=path,
        is_dir=stat.S_ISDIR(mode),
        is_link=stat.S_ISLNK(mode),
        size=attr.st_size or 0,
        mtime=float(attr.st_mtime or 0.0),
        mode=mode,
    )


# ---------- The session itself ----------

class SSHSession(QObject):
    """单台主机的 SSH/SFTP 会话

    线程模型：一个后台线程跑所有 paramiko 调用，UI 线程通过 submit() 派发，
    通过 Qt 信号回到 UI 线程。
    """

    connected = pyqtSignal()
    connect_failed = pyqtSignal(str)        # error message
    disconnected = pyqtSignal()

    def __init__(self, host_config: HostConfig, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.host_config = host_config
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ssh-{host_config.alias}")
        self._lock = threading.Lock()  # serializes paramiko calls just in case
        self._home: Optional[str] = None
        # 路径 → (timestamp, entries) 的 listdir 缓存，节省重复网络往返
        self._listdir_cache: dict[str, tuple[float, list["RemoteEntry"]]] = {}
        self._cache_ttl = 30.0  # seconds
        self._cache_lock = threading.Lock()

    # --- lifecycle ---

    def is_connected(self) -> bool:
        return self._sftp is not None

    def connect_async(self, password_provider: Optional[Callable[[str], Optional[str]]] = None,
                      passphrase_provider: Optional[Callable[[str], Optional[str]]] = None) -> Future:
        """异步连接。完成后发射 connected / connect_failed 信号"""
        fut = self._executor.submit(self._do_connect, password_provider, passphrase_provider)
        def _on_done(f: Future):
            try:
                f.result()
                self.connected.emit()
            except Exception as e:
                self.connect_failed.emit(str(e))
        fut.add_done_callback(_on_done)
        return fut

    def _do_connect(self, password_provider, passphrase_provider):
        cfg = self.host_config
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # 用户主机：不严格校验 host key，自动接受（与桌面 SSH 体验一致）
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict[str, Any] = {
            "hostname": cfg.hostname,
            "port": cfg.port,
            "timeout": 15,
            "allow_agent": True,
            "look_for_keys": True,
            "compress": True,
        }
        if cfg.user:
            connect_kwargs["username"] = cfg.user
        if cfg.identity_file and os.path.isfile(cfg.identity_file):
            connect_kwargs["key_filename"] = cfg.identity_file

        try:
            client.connect(**connect_kwargs)
        except paramiko.PasswordRequiredException:
            if not passphrase_provider:
                raise
            phrase = passphrase_provider(f"{cfg.alias} key passphrase")
            if phrase is None:
                raise
            connect_kwargs["passphrase"] = phrase
            client.connect(**connect_kwargs)
        except paramiko.AuthenticationException:
            if not password_provider:
                raise
            pwd = password_provider(cfg.alias)
            if pwd is None:
                raise
            connect_kwargs["password"] = pwd
            connect_kwargs["allow_agent"] = False
            connect_kwargs["look_for_keys"] = False
            client.connect(**connect_kwargs)

        sftp = client.open_sftp()
        try:
            home = sftp.normalize(".")
        except Exception:
            home = "/"
        self._client = client
        self._sftp = sftp
        self._home = home

    def disconnect(self):
        """关闭连接（在 UI 线程调用安全）"""
        try:
            self._executor.submit(self._do_disconnect).result(timeout=5)
        except Exception:
            pass
        self._executor.shutdown(wait=False)
        self.disconnected.emit()

    def _do_disconnect(self):
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # --- generic submit ---

    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        """在后台线程跑一个使用 self._sftp 的 callable。返回 concurrent.futures.Future"""
        return self._executor.submit(self._wrap, fn, *args, **kwargs)

    def _wrap(self, fn, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)

    # --- SFTP operations (run on background thread) ---

    def home(self) -> str:
        return self._home or "/"

    # --- cache helpers ---

    @staticmethod
    def _parent(path: str) -> str:
        return posixpath.dirname(path.rstrip("/")) or "/"

    def invalidate_cache(self, path: Optional[str] = None):
        """清除某个路径（或全部）的 listdir 缓存。在改动远端后调用。"""
        with self._cache_lock:
            if path is None:
                self._listdir_cache.clear()
            else:
                self._listdir_cache.pop(path, None)

    def listdir(self, path: str, use_cache: bool = True) -> list[RemoteEntry]:
        if use_cache:
            with self._cache_lock:
                cached = self._listdir_cache.get(path)
            if cached and (time.time() - cached[0]) < self._cache_ttl:
                return list(cached[1])
        sftp = self._require()
        attrs = sftp.listdir_attr(path)
        entries = [_attr_to_entry(path, a) for a in attrs]
        # 隐藏 .开头 默认不过滤（UI 自己决定），这里只排个序
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        with self._cache_lock:
            self._listdir_cache[path] = (time.time(), entries)
        return list(entries)

    def stat(self, path: str) -> RemoteEntry:
        sftp = self._require()
        attr = sftp.stat(path)
        # construct a synthetic name from path
        name = os.path.basename(path.rstrip("/")) or path
        parent = os.path.dirname(path.rstrip("/")) or "/"
        attr.filename = name
        return _attr_to_entry(parent, attr)

    def read_file(self, path: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
        """读取远程文件。max_bytes 防止误开超大文件"""
        sftp = self._require()
        try:
            st = sftp.stat(path)
        except Exception:
            st = None
        if st is not None and st.st_size and st.st_size > max_bytes:
            raise ValueError(f"file too large ({st.st_size} bytes, limit {max_bytes})")
        with sftp.open(path, "rb") as fh:
            return fh.read()

    def write_file(self, path: str, content: bytes):
        sftp = self._require()
        # paramiko 的 put_fo 走原子重命名，但这里我们只是覆盖写
        with sftp.open(path, "wb") as fh:
            fh.write(content)
        self.invalidate_cache(self._parent(path))

    def mkdir(self, path: str):
        sftp = self._require()
        sftp.mkdir(path)
        self.invalidate_cache(self._parent(path))

    def rmdir(self, path: str):
        sftp = self._require()
        sftp.rmdir(path)
        self.invalidate_cache(self._parent(path))
        self.invalidate_cache(path)

    def remove(self, path: str):
        sftp = self._require()
        sftp.remove(path)
        self.invalidate_cache(self._parent(path))

    def rename(self, old: str, new: str):
        sftp = self._require()
        sftp.rename(old, new)
        self.invalidate_cache(self._parent(old))
        self.invalidate_cache(self._parent(new))
        self.invalidate_cache(old)

    def upload(self, local_path: str, remote_path: str):
        sftp = self._require()
        sftp.put(local_path, remote_path)
        self.invalidate_cache(self._parent(remote_path))

    def download(self, remote_path: str, local_path: str):
        sftp = self._require()
        sftp.get(remote_path, local_path)

    def remove_tree(self, path: str):
        """递归删除目录（不跨链接）"""
        sftp = self._require()
        for entry in sftp.listdir_attr(path):
            child = path.rstrip("/") + "/" + entry.filename
            if entry.st_mode and stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode):
                self.remove_tree(child)
            else:
                sftp.remove(child)
        sftp.rmdir(path)
        self.invalidate_cache(self._parent(path))
        self.invalidate_cache(path)

    def _require(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise RuntimeError("not connected")
        return self._sftp
