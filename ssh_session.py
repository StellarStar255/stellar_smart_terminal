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
import functools
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
import paramiko.sftp_file  # noqa: F401 — 用于全局调高 SFTP prefetch 块大小
from paramiko.config import SSHConfig

# paramiko 默认每个 SFTP 读请求只要 32KB。这是 SFTP 协议层的客户端参数，
# 不影响 SSH 握手 / channel 协商；只在 prefetch() 把文件切成多少个 read
# 请求时被用到。把它推到 256KB —— 1.6MB 文件从 50 次往返降到 7 次，下载
# 速度通常快 5–10 倍，且 OpenSSH/Dropbear/sftp-server 都支持 256KB。
paramiko.sftp_file.SFTPFile.MAX_REQUEST_SIZE = 1024 * 256

from PyQt6.QtCore import QObject, pyqtSignal

from i18n import t

# --- 连接稳定性参数 ---
KEEPALIVE_INTERVAL = 30        # transport 层 keep-alive 间隔（秒），防 NAT/网关空闲断连
CONNECT_TIMEOUT = 15           # 单次 TCP/SSH 握手超时（秒）
RECONNECT_MAX_ATTEMPTS = 3     # 自动重连最多尝试次数
RECONNECT_BACKOFF_BASE = 1.0   # 指数退避基数（秒）：1s → 2s → 4s

# 这些异常通常意味着底层连接断了（而非业务错误）。注意 OSError 也可能是
# SFTP 状态错误（FileNotFoundError/PermissionError 等），所以判定时还要
# 结合 transport.is_active()，见 SSHSession._should_reconnect。
_RECONNECTABLE_ERRORS = (paramiko.SSHException, EOFError, OSError)


def _auto_reconnect(method):
    """装饰 SFTP 操作：连接确实断开导致失败时，自动重连并把该操作重试一次。

    所有被装饰的方法都经由 submit() 在单线程 executor 上运行，因此这里的
    重连不会和其它操作并发；_reconnect 内部再用锁兜底（防止外部线程直调）。
    """
    @functools.wraps(method)
    def wrapper(self: "SSHSession", *args, **kwargs):
        # 上一次重连失败后连接已被拆掉：先试着把连接重新建起来
        if self._sftp is None and self._was_connected:
            self._reconnect()
        try:
            return method(self, *args, **kwargs)
        except _RECONNECTABLE_ERRORS as exc:
            if not self._should_reconnect(exc):
                raise
            self._reconnect()
            return method(self, *args, **kwargs)
    return wrapper


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


def _existing_aliases(config_path: Path) -> set:
    """返回 config 里已有的 Host 别名集合（含通配符），用于避免重名。"""
    if not config_path.is_file():
        return set()
    try:
        cfg = SSHConfig()
        with config_path.open("r", encoding="utf-8", errors="replace") as fh:
            cfg.parse(fh)
        return set(cfg.get_hostnames())
    except Exception:
        return set()


def append_ssh_config_host(
    hostname: str,
    user: str = "",
    port: int = 22,
    identity_file: Optional[str] = None,
    alias: Optional[str] = None,
    path: Optional[str] = None,
) -> HostConfig:
    """把一台主机追加写入 ~/.ssh/config，持久化为系统记录。

    - 生成一个干净的 Host 别名（默认用 hostname），与已有别名冲突时自动加数字后缀。
    - 文件/目录权限收敛到 600/700（ssh 对宽松权限会拒绝读取）。
    返回写入后的 HostConfig（alias 为最终采用的别名）。
    """
    config_path = Path(path or os.path.expanduser("~/.ssh/config"))
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_path.parent, 0o700)
    except OSError:
        pass

    existing = _existing_aliases(config_path)
    base = (alias or hostname).strip() or hostname
    candidate = base
    i = 2
    while candidate in existing:
        candidate = f"{base}-{i}"
        i += 1

    lines = [f"Host {candidate}", f"    HostName {hostname}"]
    if user:
        lines.append(f"    User {user}")
    if port and int(port) != 22:
        lines.append(f"    Port {int(port)}")
    if identity_file:
        lines.append(f"    IdentityFile {identity_file}")

    # 与已有内容之间留一个空行，避免粘到上一个块
    prefix = ""
    if config_path.is_file() and config_path.stat().st_size > 0:
        try:
            with config_path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                last = fh.read(1)
            prefix = "\n" if last == b"\n" else "\n\n"
        except OSError:
            prefix = "\n"

    block = prefix + "\n".join(lines) + "\n"
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass

    return HostConfig(
        alias=candidate,
        hostname=hostname,
        user=user,
        port=int(port),
        identity_file=identity_file,
    )


def rename_ssh_config_host(old_alias: str, new_alias: str,
                           path: Optional[str] = None) -> bool:
    """把 ~/.ssh/config 里某个 Host 的别名从 old_alias 改成 new_alias。

    只改 `Host` 行里匹配到的那个别名 token（一行可能有多个别名，其余不动），
    其它配置项保持原样。成功改写返回 True；config 不存在 / 没匹配到返回 False；
    新别名与现有别名冲突时抛 ValueError。
    """
    config_path = Path(path or os.path.expanduser("~/.ssh/config"))
    if not config_path.is_file():
        return False
    new_alias = (new_alias or "").strip()
    if not new_alias or new_alias == old_alias:
        return False
    if new_alias in _existing_aliases(config_path):
        raise ValueError(f"alias '{new_alias}' already exists")

    try:
        with config_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return False

    changed = False
    for i, line in enumerate(lines):
        body = line.rstrip("\n")
        newline = line[len(body):]            # 保留原行尾（\n / \r\n / 无）
        tokens = body.split()
        if len(tokens) >= 2 and tokens[0].lower() == "host":
            replaced = False
            for j in range(1, len(tokens)):
                if tokens[j] == old_alias:
                    tokens[j] = new_alias
                    replaced = True
            if replaced:
                indent = body[:len(body) - len(body.lstrip())]
                lines[i] = indent + " ".join(tokens) + newline
                changed = True

    if changed:
        with config_path.open("w", encoding="utf-8") as fh:
            fh.writelines(lines)
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
    return changed


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

class _PersistOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    """首次见到的主机：记录其 host key（TOFU 信任），并标记需要写回 known_hosts。

    安全意义：相比 AutoAddPolicy「每次连接都临时接受、从不持久化」（导致已知主机
    密钥被替换时也永远不会被发现），这里把首次接受的 key 写回 ~/.ssh/known_hosts。
    一旦持久化，后续连接时 paramiko 会在调用本策略之前就比对已知 key，密钥变更
    （典型 MITM 特征）会直接抛 BadHostKeyException，从而真正拦截中间人攻击。
    """

    def __init__(self):
        self.added = False

    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)
        self.added = True


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
        self._jump_clients: list[paramiko.SSHClient] = []  # ProxyJump 跳板连接（按跳序）
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ssh-{host_config.alias}")
        self._lock = threading.Lock()  # serializes paramiko calls just in case
        self._reconnect_lock = threading.Lock()  # 保证同一时刻只有一个重连在进行
        self._was_connected = False               # 曾成功连接过（区分「从未连上」和「断线」）
        # 认证上下文：初次连接用的 provider + 成功过的密码/口令缓存（按 alias），
        # 重连时优先复用缓存，避免在后台重连流程里再次弹框
        self._password_provider: Optional[Callable[[str], Optional[str]]] = None
        self._passphrase_provider: Optional[Callable[[str], Optional[str]]] = None
        self._auth_secrets: dict[str, dict[str, str]] = {}
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
        # 记住认证 provider，自动重连时复用同一套认证逻辑
        self._password_provider = password_provider
        self._passphrase_provider = passphrase_provider
        self._establish()

    def _establish(self):
        """真正建立连接：先按 ProxyJump 链路连跳板，再连目标，最后开 SFTP。

        初次连接和自动重连共用这一条路径，保证认证/host key 策略完全一致。
        """
        cfg = self.host_config
        jump_clients, sock = self._connect_jump_chain(cfg)
        try:
            client = self._connect_client(cfg, sock=sock)
        except Exception:
            self._close_clients(jump_clients)
            raise
        try:
            sftp = client.open_sftp()
        except Exception:
            self._close_clients([client] + jump_clients)
            raise
        try:
            home = sftp.normalize(".")
        except Exception:
            home = "/"
        self._client = client
        self._sftp = sftp
        self._jump_clients = jump_clients
        self._home = home
        self._was_connected = True

    def _connect_client(self, cfg: HostConfig,
                        sock: Optional[Any] = None) -> paramiko.SSHClient:
        """连接单台主机（目标机或跳板机），返回已认证的 SSHClient。

        host key 策略、认证回退（agent/key → passphrase → password）对目标机
        和跳板机一视同仁；成功用过的密码/口令按 alias 缓存，供自动重连复用。
        """
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        # 持久化 TOFU：加载用户 known_hosts，使「已知主机密钥变更」能被 paramiko 在
        # 握手阶段直接拦截（抛 BadHostKeyException），从而防御中间人攻击；首次连接的
        # 未知主机记录指纹后写回 known_hosts，后续连接即受校验。
        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        try:
            if os.path.isfile(known_hosts):
                client.load_host_keys(known_hosts)
        except Exception:
            pass
        host_key_policy = _PersistOnFirstUsePolicy()
        client.set_missing_host_key_policy(host_key_policy)

        connect_kwargs: dict[str, Any] = {
            "hostname": cfg.hostname,
            "port": cfg.port,
            "timeout": CONNECT_TIMEOUT,
            "allow_agent": True,
            "look_for_keys": True,
            "compress": True,
        }
        if sock is not None:
            connect_kwargs["sock"] = sock
        if cfg.user:
            connect_kwargs["username"] = cfg.user
        if cfg.identity_file and os.path.isfile(cfg.identity_file):
            connect_kwargs["key_filename"] = cfg.identity_file

        # 重连时优先复用上次成功的密码/口令，避免后台重连再次弹框
        cached = self._auth_secrets.get(cfg.alias, {})
        if cached.get("passphrase") is not None:
            connect_kwargs["passphrase"] = cached["passphrase"]
        if cached.get("password") is not None:
            connect_kwargs["password"] = cached["password"]
            connect_kwargs["allow_agent"] = False
            connect_kwargs["look_for_keys"] = False

        try:
            try:
                client.connect(**connect_kwargs)
            except paramiko.PasswordRequiredException:
                if not self._passphrase_provider:
                    raise
                phrase = self._passphrase_provider(f"{cfg.alias} key passphrase")
                if phrase is None:
                    raise
                connect_kwargs["passphrase"] = phrase
                client.connect(**connect_kwargs)
                self._auth_secrets.setdefault(cfg.alias, {})["passphrase"] = phrase
            except paramiko.AuthenticationException:
                if not self._password_provider:
                    raise
                pwd = self._password_provider(cfg.alias)
                if pwd is None:
                    raise
                connect_kwargs["password"] = pwd
                connect_kwargs["allow_agent"] = False
                connect_kwargs["look_for_keys"] = False
                client.connect(**connect_kwargs)
                self._auth_secrets.setdefault(cfg.alias, {})["password"] = pwd
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise

        # 首次接受的未知主机 key 写回 known_hosts，让后续连接受 MITM 校验保护
        if host_key_policy.added:
            try:
                os.makedirs(os.path.dirname(known_hosts), exist_ok=True)
                client.save_host_keys(known_hosts)
            except Exception:
                pass

        # keep-alive：定期发 transport 层心跳，防止 NAT/防火墙把空闲连接掐掉
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(KEEPALIVE_INTERVAL)
        return client

    # --- ProxyJump ---

    @staticmethod
    def _parse_jump_token(spec: str) -> tuple[str, str, Optional[int]]:
        """解析单个 ProxyJump 片段：[user@]host[:port]，支持 [IPv6]:port。

        返回 (user, host, port)，未显式给出的字段为 ""/None。
        """
        user = ""
        host = spec.strip()
        port: Optional[int] = None
        if "@" in host:
            user, host = host.rsplit("@", 1)
        if host.startswith("["):
            end = host.find("]")
            if end != -1:
                rest = host[end + 1:]
                host = host[1:end]
                if rest.startswith(":"):
                    try:
                        port = int(rest[1:])
                    except ValueError:
                        port = None
        elif host.count(":") == 1:
            h, _, p = host.partition(":")
            try:
                port = int(p)
                host = h
            except ValueError:
                pass
        return user, host, port

    def _resolve_jump_spec(self, spec: str) -> HostConfig:
        """把 ProxyJump 片段解析成 HostConfig：先查 ~/.ssh/config（别名 →
        HostName/User/Port/IdentityFile），spec 里显式写的 user/port 优先。
        """
        user, host, port = self._parse_jump_token(spec)
        resolved: dict = {}
        config_path = Path(os.path.expanduser("~/.ssh/config"))
        if config_path.is_file():
            try:
                cfg = SSHConfig()
                with config_path.open("r", encoding="utf-8", errors="replace") as fh:
                    cfg.parse(fh)
                resolved = cfg.lookup(host)
            except Exception:
                resolved = {}
        hostname = resolved.get("hostname", host)
        if not user:
            user = resolved.get("user", "")
        if port is None:
            try:
                port = int(resolved.get("port", 22))
            except (TypeError, ValueError):
                port = 22
        identity_files = resolved.get("identityfile") or []
        identity = os.path.expanduser(identity_files[0]) if identity_files else None
        return HostConfig(
            alias=host,
            hostname=hostname,
            user=user,
            port=port,
            identity_file=identity,
            raw=dict(resolved),
        )

    def _connect_jump_chain(self, cfg: HostConfig) -> tuple[list[paramiko.SSHClient], Optional[Any]]:
        """按 ProxyJump 链路（逗号分隔，依次跳）连接所有跳板机。

        返回 (跳板 client 列表, 指向最终目标的 direct-tcpip channel)；没有
        配置 ProxyJump 时返回 ([], None)。任一跳失败会把已连上的跳板全部关掉。
        注：只展开目标机自身的 ProxyJump 链，跳板机配置里再嵌套的 ProxyJump
        不递归处理（与多数场景一致，避免环路）。
        """
        spec = (cfg.proxy_jump or "").strip()
        if not spec or spec.lower() == "none":
            return [], None
        hops = [s for s in (p.strip() for p in spec.split(",")) if s]
        if not hops:
            return [], None
        hop_cfgs = [self._resolve_jump_spec(h) for h in hops]
        clients: list[paramiko.SSHClient] = []
        sock: Optional[Any] = None
        try:
            for i, hop_cfg in enumerate(hop_cfgs):
                client = self._connect_client(hop_cfg, sock=sock)
                clients.append(client)
                if i + 1 < len(hop_cfgs):
                    dest = (hop_cfgs[i + 1].hostname, hop_cfgs[i + 1].port)
                else:
                    dest = (cfg.hostname, cfg.port)
                transport = client.get_transport()
                if transport is None:
                    raise paramiko.SSHException(
                        f"{t('ssh.jump_channel_failed')}: {hop_cfg.alias}")
                sock = transport.open_channel(
                    "direct-tcpip", dest, ("127.0.0.1", 0))
            return clients, sock
        except Exception:
            self._close_clients(clients)
            raise

    @staticmethod
    def _close_clients(clients: list):
        """静默关闭一组 client（按倒序，先关靠近目标的一端）"""
        for c in reversed(clients):
            try:
                c.close()
            except Exception:
                pass

    def disconnect(self):
        """关闭连接（在 UI 线程调用安全）"""
        try:
            self._executor.submit(self._do_disconnect).result(timeout=5)
        except Exception:
            pass
        self._executor.shutdown(wait=False)
        self.disconnected.emit()

    def _do_disconnect(self):
        self._was_connected = False  # 主动断开后不再自动重连
        self._teardown_connection()

    def _teardown_connection(self):
        """静默关闭当前连接（含跳板机），不发信号、不关 executor。

        供主动断开和自动重连前的清理共用。
        """
        sftp, self._sftp = self._sftp, None
        client, self._client = self._client, None
        jumps, self._jump_clients = self._jump_clients, []
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        self._close_clients(([client] if client is not None else []) + jumps)

    # --- liveness & auto-reconnect ---

    def is_alive(self) -> bool:
        """底层 transport 是否仍然存活（不额外发请求）"""
        client = self._client
        if client is None or self._sftp is None:
            return False
        transport = client.get_transport()
        return bool(transport is not None and transport.is_active())

    def _should_reconnect(self, exc: BaseException) -> bool:
        """判断一次操作失败是否应该走自动重连。

        - 从未成功连接 / 已主动断开 → 不重连；
        - SSHException / EOFError → 基本可断定是连接层故障；
        - OSError 既可能是连接断了，也可能是 SFTP 状态错误（文件不存在、
          权限不足等），只有 transport 确实死了才视为断线。
        """
        if not self._was_connected:
            return False
        if isinstance(exc, (paramiko.SSHException, EOFError)):
            return True
        return not self.is_alive()

    def _reconnect(self):
        """自动重连：指数退避，最多 RECONNECT_MAX_ATTEMPTS 次。

        用 _reconnect_lock 保证同一时刻只有一个重连在跑（操作本身都在单线程
        executor 上串行，这把锁主要防外部线程直调时的并发重连风暴）。
        全部失败时抛出带 i18n 文案的 RuntimeError（链上保留原始异常）。
        """
        with self._reconnect_lock:
            if self.is_alive():
                return  # 别的调用已经把连接修好了
            last_exc: Optional[BaseException] = None
            for attempt in range(RECONNECT_MAX_ATTEMPTS):
                self._teardown_connection()
                try:
                    self._establish()
                    self.invalidate_cache()  # 断线期间远端可能已变化
                    return
                except Exception as e:
                    last_exc = e
                    if attempt + 1 < RECONNECT_MAX_ATTEMPTS:
                        time.sleep(RECONNECT_BACKOFF_BASE * (2 ** attempt))
            raise RuntimeError(
                f"{t('ssh.reconnect_failed')} ({self.host_config.alias}): {last_exc}"
            ) from last_exc

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

    @_auto_reconnect
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

    @_auto_reconnect
    def stat(self, path: str) -> RemoteEntry:
        sftp = self._require()
        attr = sftp.stat(path)
        # construct a synthetic name from path
        name = os.path.basename(path.rstrip("/")) or path
        parent = os.path.dirname(path.rstrip("/")) or "/"
        attr.filename = name
        return _attr_to_entry(parent, attr)

    @_auto_reconnect
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

    @_auto_reconnect
    def write_file(self, path: str, content: bytes):
        sftp = self._require()
        # paramiko 的 put_fo 走原子重命名，但这里我们只是覆盖写
        with sftp.open(path, "wb") as fh:
            fh.write(content)
        self.invalidate_cache(self._parent(path))

    @_auto_reconnect
    def mkdir(self, path: str):
        sftp = self._require()
        sftp.mkdir(path)
        self.invalidate_cache(self._parent(path))

    @_auto_reconnect
    def rmdir(self, path: str):
        sftp = self._require()
        sftp.rmdir(path)
        self.invalidate_cache(self._parent(path))
        self.invalidate_cache(path)

    @_auto_reconnect
    def remove(self, path: str):
        sftp = self._require()
        sftp.remove(path)
        self.invalidate_cache(self._parent(path))

    @_auto_reconnect
    def rename(self, old: str, new: str):
        sftp = self._require()
        sftp.rename(old, new)
        self.invalidate_cache(self._parent(old))
        self.invalidate_cache(self._parent(new))
        self.invalidate_cache(old)

    @_auto_reconnect
    def upload(self, local_path: str, remote_path: str):
        sftp = self._require()
        sftp.put(local_path, remote_path)
        self.invalidate_cache(self._parent(remote_path))

    @_auto_reconnect
    def download(self, remote_path: str, local_path: str):
        sftp = self._require()
        sftp.get(remote_path, local_path)

    @_auto_reconnect
    def download_with_progress(self, remote_path: str, local_path: str,
                               progress_cb=None) -> "paramiko.SFTPAttributes":
        """流式下载到本地，paramiko 会按 32K 块边读边写盘，不会把整个文件塞内存。

        progress_cb(bytes_done, bytes_total) 会被 paramiko 从 worker 线程里
        高频回调（每个 chunk 一次）。调用方需要自己做节流，并把更新切回 UI 线程。
        返回远端 stat，供调用方做 size/mtime 缓存判定。
        """
        sftp = self._require()
        attr = sftp.stat(remote_path)
        # 先下到 .part，再原子改名 —— 避免 UI 看到一个尚在写入的半成品图片
        tmp_path = local_path + ".part"
        try:
            sftp.get(remote_path, tmp_path, callback=progress_cb)
            os.replace(tmp_path, local_path)
        except Exception:
            # 失败时清理半成品
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise
        return attr

    @_auto_reconnect
    def remove_tree(self, path: str):
        """递归删除目录（不跨链接）"""
        # 递归走不带重连装饰的内部实现，避免断线时逐层嵌套重试
        self._remove_tree_impl(path)
        self.invalidate_cache(self._parent(path))
        self.invalidate_cache(path)

    def _remove_tree_impl(self, path: str):
        sftp = self._require()
        for entry in sftp.listdir_attr(path):
            child = path.rstrip("/") + "/" + entry.filename
            if entry.st_mode and stat.S_ISDIR(entry.st_mode) and not stat.S_ISLNK(entry.st_mode):
                self._remove_tree_impl(child)
            else:
                sftp.remove(child)
        sftp.rmdir(path)

    def _require(self) -> paramiko.SFTPClient:
        if self._sftp is None:
            raise RuntimeError("not connected")
        return self._sftp
