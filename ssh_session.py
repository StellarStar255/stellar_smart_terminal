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
import getpass
import os
import posixpath
import re
import shlex
import stat
import tarfile
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

from app_logging import get_logger

logger = get_logger(__name__)

from i18n import t

# --- 连接稳定性参数 ---
KEEPALIVE_INTERVAL = 30        # transport 层 keep-alive 间隔（秒），防 NAT/网关空闲断连
CONNECT_TIMEOUT = 15           # 单次 TCP/SSH 握手超时（秒）
# SFTP 数据通道读超时（秒）：网络突然切换时，socket 上的 recv 会无限期阻塞 ——
# 没有它，sftp.get/put/listdir/stat 会永远卡住，future 永不完成，UI 随之卡死
# （尤其上传/下载）。设了它，停顿超过该时长即抛 socket.timeout（OSError 子类），
# 操作快速失败 → future 完成 → UI 解冻、并可走自动重连。取值要远大于活跃传输里
# 相邻数据块的间隔（活跃传输每隔几毫秒就有数据，永远不会误触发），只在真正断流时命中。
SFTP_OP_TIMEOUT = 20
# 打开 SFTP 通道（子系统握手 + 版本协商）的整体超时。paramiko 这一段没有
# 任何超时可设，只能由 _open_sftp_bounded 从外部兜底，见该方法注释。
SFTP_OPEN_TIMEOUT = 20
RECONNECT_MAX_ATTEMPTS = 3     # 自动重连最多尝试次数
RECONNECT_BACKOFF_BASE = 1.0   # 指数退避基数（秒）：1s → 2s → 4s
# 重连失败后的冷却期：连接确实断了时，排队中的每个操作都各自跑一轮完整
# 重连（3 次尝试、最坏数十秒），N 个排队操作 = N 倍等待，表现为「一个远程
# 操作挂了，整个面板长时间没反应」。冷却期内直接快速失败，只让第一个操作
# 承担重连成本，后面的立刻返回错误。
RECONNECT_COOLDOWN = 20.0

# 终端里跑的 ssh CLI 的应用层保活（build_ssh_terminal_command 注入）。
# 30s × 3 次未响应 ≈ 90s 判定链路已死并退出：足以扛过短暂抖动、睡眠唤醒，
# 又不会让用户对着一个永远卡死的标签页干等。
SSH_ALIVE_INTERVAL = 30
SSH_ALIVE_COUNT_MAX = 3

# 这些异常通常意味着底层连接断了（而非业务错误）。注意 OSError 也可能是
# SFTP 状态错误（FileNotFoundError/PermissionError 等），所以判定时还要
# 结合 transport.is_active()，见 SSHSession._should_reconnect。
_RECONNECTABLE_ERRORS = (paramiko.SSHException, EOFError, OSError)

# keyboard-interactive 提示里「在要登录密码」的判定（区别于 OTP/验证码）。
# 密码类回答可以缓存复用（自动重连、SSH 终端 tab 回填）；验证码是一次性的，
# 缓存了也没用，还有泄漏面。
_PASSWORD_PROMPT_RE = re.compile(r"password|passphrase|密码|口令", re.IGNORECASE)


def looks_like_password_prompt(prompt: str) -> bool:
    return bool(_PASSWORD_PROMPT_RE.search(prompt))


def _auto_reconnect(method):
    """装饰 SFTP 操作：连接确实断开导致失败时，自动重连并把该操作重试一次。

    所有被装饰的方法都经由 submit() 在单线程 executor 上运行，因此这里的
    重连不会和其它操作并发；_reconnect 内部再用锁兜底（防止外部线程直调）。
    """
    @functools.wraps(method)
    def wrapper(self: "SSHSession", *args, **kwargs):
        # 上一次重连失败后连接已被拆掉：先试着把连接重新建起来
        if self._sftp is None and self._was_connected:
            self._reconnect_or_fail_fast()
        try:
            return method(self, *args, **kwargs)
        except _RECONNECTABLE_ERRORS as exc:
            if not self._should_reconnect(exc):
                raise
            self._reconnect_or_fail_fast()
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


def _host_line_aliases(line: str):
    """若该行是 `Host a b c` 形式，返回别名列表 [a, b, c]，否则 None。"""
    body = line.rstrip("\n").strip()
    toks = body.split()
    if len(toks) >= 2 and toks[0].lower() == "host":
        return toks[1:]
    return None


def _find_host_block(lines: list, alias: str):
    """定位包含 alias 的 Host 块，返回 (host_idx, end)（半开区间 [host_idx, end)），
    end 为下一个 Host/Match 行或 EOF。找不到返回 None。"""
    host_idx = None
    for i, line in enumerate(lines):
        aliases = _host_line_aliases(line)
        if aliases and alias in aliases:
            host_idx = i
            break
    if host_idx is None:
        return None
    end = len(lines)
    for j in range(host_idx + 1, len(lines)):
        toks = lines[j].strip().split()
        if toks and toks[0].lower() in ("host", "match"):
            end = j
            break
    return host_idx, end


def remove_ssh_config_host(alias: str, path: Optional[str] = None) -> bool:
    """从 ~/.ssh/config 删除某个 Host。

    - Host 行只有这一个别名 → 删除整块（Host 行 + 其后配置行，直到下一个 Host/Match/EOF），
      连带块前的一个分隔空行一起收掉，避免留下多余空行。
    - Host 行有多个别名 → 只从 Host 行摘掉该别名 token，块保留给其它别名。
    成功改写返回 True；config 不存在 / 没匹配到返回 False。
    """
    config_path = Path(path or os.path.expanduser("~/.ssh/config"))
    if not config_path.is_file():
        return False
    try:
        with config_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return False

    found = _find_host_block(lines, alias)
    if found is None:
        return False
    host_idx, end = found

    aliases = _host_line_aliases(lines[host_idx]) or []
    if len(aliases) > 1:
        body = lines[host_idx].rstrip("\n")
        newline = lines[host_idx][len(body):]
        indent = body[:len(body) - len(body.lstrip())]
        toks = body.split()
        kept = [toks[0]] + [a for a in toks[1:] if a != alias]
        lines[host_idx] = indent + " ".join(kept) + newline
    else:
        start = host_idx
        # 连带吃掉块前紧邻的一个空行（append 时会写入的分隔空行）
        if start > 0 and lines[start - 1].strip() == "":
            start -= 1
        del lines[start:end]
        # 收敛：把连续多空行压成一个、去掉文件开头的空行
        cleaned = []
        for ln in lines:
            if ln.strip() == "" and cleaned and cleaned[-1].strip() == "":
                continue
            cleaned.append(ln)
        while cleaned and cleaned[0].strip() == "":
            cleaned.pop(0)
        lines = cleaned

    with config_path.open("w", encoding="utf-8") as fh:
        fh.writelines(lines)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    return True


def update_ssh_config_host(alias: str, hostname: str, user: str = "",
                           port: int = 22, path: Optional[str] = None) -> bool:
    """就地更新某 Host 块的 HostName/User/Port，其它指令（IdentityFile/ProxyJump 等）
    原样保留。约定与 append_ssh_config_host 一致：User 为空则不写、Port 为 22 则不写
    （已存在的对应行会被移除）。成功返回 True；config 不存在 / 没匹配到返回 False。"""
    config_path = Path(path or os.path.expanduser("~/.ssh/config"))
    if not config_path.is_file():
        return False
    try:
        with config_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return False

    found = _find_host_block(lines, alias)
    if found is None:
        return False
    host_idx, end = found

    # 沿用块内已有的缩进；没有正文行则默认 4 空格
    body_indent = "    "
    for j in range(host_idx + 1, end):
        if lines[j].strip():
            lead = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
            body_indent = lead or "    "
            break

    # 保留除 HostName/User/Port 之外的所有正文行
    kept_body = []
    for j in range(host_idx + 1, end):
        toks = lines[j].strip().split()
        key = toks[0].lower() if toks else ""
        if key in ("hostname", "user", "port"):
            continue
        kept_body.append(lines[j])

    fresh = [f"{body_indent}HostName {hostname}\n"]
    if user and user.strip():
        fresh.append(f"{body_indent}User {user.strip()}\n")
    if port and int(port) != 22:
        fresh.append(f"{body_indent}Port {int(port)}\n")

    lines[host_idx:end] = [lines[host_idx]] + fresh + kept_body
    with config_path.open("w", encoding="utf-8") as fh:
        fh.writelines(lines)
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    return True


# ---------- 交互式 ssh 终端命令构造 ----------

def build_ssh_terminal_command(host_config: "HostConfig",
                               remote_cd_path: Optional[str] = None) -> str:
    """构造在本地终端里运行的交互式 ssh 命令串（Remote 面板「打开终端」用）。

    远程命令统一为 "export <注入>; [cd <dir> && ] exec ${SHELL:-/bin/bash} -l"：
    - export 写在 exec 之前，登录 shell 经 exec 继承这些变量——把本地终端
      注入的 claude 相关环境（见 terminal_backend.remote_env_export_prefix）
      带到远端，远程跑 claude 与本地体验一致，且不依赖服务端 AcceptEnv；
    - ${SHELL:-/bin/bash} 兜底远端未设置 $SHELL；整条远程命令经单引号传递，
      本地 shell 不展开；
    - 代价：显式远程命令会跳过 motd/lastlog 一类登录横幅。

    另外注入 ServerAlive* 保活（用户未自行配置时）：ssh 默认不发应用层
    保活，NAT/防火墙静默回收空闲连接、或笔记本睡眠唤醒后链路已死时，
    客户端察觉不到，表现为标签页永久卡死、敲什么都没反应且无法退出。
    有了保活，链路真死会在约 90 秒内以「Connection closed」正常收场。
    """
    import shlex
    from terminal_backend import remote_env_export_prefix

    alias = host_config.alias
    is_config_alias = "@" not in alias and ":" not in alias
    ssh_args = ["ssh"]
    # 命令行 -o 优先级高于 ssh_config，因此只在用户没写过时才注入，
    # 不覆盖用户对具体主机的显式调优（raw 的键由 paramiko 归一为小写）
    raw = {str(k).lower() for k in (host_config.raw or {})}
    for opt, val in (("ServerAliveInterval", SSH_ALIVE_INTERVAL),
                     ("ServerAliveCountMax", SSH_ALIVE_COUNT_MAX)):
        if opt.lower() not in raw:
            ssh_args.extend(["-o", f"{opt}={val}"])
    if is_config_alias:
        # ssh CLI 自动应用 ~/.ssh/config（含 ProxyJump/IdentityFile 等）
        ssh_args.append(alias)
    else:
        if host_config.identity_file and os.path.isfile(host_config.identity_file):
            ssh_args.extend(["-i", host_config.identity_file])
        if host_config.port and host_config.port != 22:
            ssh_args.extend(["-p", str(host_config.port)])
        target = (f"{host_config.user}@{host_config.hostname}"
                  if host_config.user else host_config.hostname)
        ssh_args.append(target)

    remote_cmd = remote_env_export_prefix()
    if remote_cd_path:
        remote_cmd += f"cd {shlex.quote(remote_cd_path)} && "
    remote_cmd += "exec ${SHELL:-/bin/bash} -l"
    # -t 强制分配 tty（带远程命令时 ssh 默认不分配）
    ssh_args.extend(["-t", remote_cmd])
    return " ".join(shlex.quote(a) for a in ssh_args)


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
    # known_hosts 加载失败：MITM 防护（主机密钥变更拦截）已降级，UI 应提示用户
    host_key_check_degraded = pyqtSignal(str)  # 降级原因描述

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
        # keyboard-interactive（OTP/2FA）提示回调：(alias, prompt, echo) -> 回答
        self._interactive_provider: Optional[Callable[[str, str, bool], Optional[str]]] = None
        self._auth_secrets: dict[str, dict[str, str]] = {}
        self._home: Optional[str] = None
        self._host_key_degraded = False  # known_hosts 加载失败 → MITM 拦截降级
        # 路径 → (timestamp, entries) 的 listdir 缓存，节省重复网络往返
        self._listdir_cache: dict[str, tuple[float, list["RemoteEntry"]]] = {}
        self._cache_ttl = 30.0  # seconds
        self._cache_lock = threading.Lock()
        # 远端是否有 tar（目录上传快路径的探测结果，按会话缓存）
        self._remote_has_tar: Optional[bool] = None
        # 上次重连失败的时刻（monotonic）；冷却期内的操作快速失败，
        # 见 _reconnect_or_fail_fast
        self._last_reconnect_failure: Optional[float] = None

    # --- lifecycle ---

    def is_connected(self) -> bool:
        return self._sftp is not None

    def connect_async(self, password_provider: Optional[Callable[[str], Optional[str]]] = None,
                      passphrase_provider: Optional[Callable[[str], Optional[str]]] = None,
                      interactive_provider: Optional[Callable[[str, str, bool], Optional[str]]] = None) -> Future:
        """异步连接。完成后发射 connected / connect_failed 信号"""
        fut = self._executor.submit(self._do_connect, password_provider,
                                    passphrase_provider, interactive_provider)
        def _on_done(f: Future):
            try:
                f.result()
                self.connected.emit()
            except Exception as e:
                self.connect_failed.emit(str(e))
        fut.add_done_callback(_on_done)
        return fut

    def _do_connect(self, password_provider, passphrase_provider,
                    interactive_provider=None):
        # 记住认证 provider，自动重连时复用同一套认证逻辑
        self._password_provider = password_provider
        self._passphrase_provider = passphrase_provider
        self._interactive_provider = interactive_provider
        self._establish()

    def _open_sftp_bounded(self, client: paramiko.SSHClient) -> paramiko.SFTPClient:
        """打开 SFTP 通道，带整体超时。

        paramiko 的 open_sftp() 内部 invoke_subsystem 走 Channel._wait_for_event()，
        那个等待**没有超时**：服务器 TCP 与认证都通、却迟迟不响应 sftp 子系统
        请求时（远端磁盘满 / sshd fork 失败 / 中间设备半开连接），调用线程会
        永久卡住——而它是本会话唯一的 worker，于是排队中的所有远程操作、以及
        任何在 GUI 线程上等待这些操作的代码一并冻住。

        这里把打开动作放到临时线程里跑并设截止时间；超时就关掉 transport
        （卡住的等待会立刻抛错退出，线程随即回收），保证 _establish 一定
        有界返回。通道读超时在返回前设好，后续 normalize/listdir 等即受
        SFTP_OP_TIMEOUT 保护。
        """
        result: dict = {}

        def _work():
            try:
                sftp = client.open_sftp()
                chan = sftp.get_channel()
                if chan is not None:
                    chan.settimeout(SFTP_OP_TIMEOUT)
                result['sftp'] = sftp
            except Exception as e:      # noqa: BLE001 — 原样回传给调用线程
                result['exc'] = e

        th = threading.Thread(
            target=_work, daemon=True,
            name=f"sftp-open-{self.host_config.alias}")
        th.start()
        th.join(timeout=SFTP_OPEN_TIMEOUT)
        if th.is_alive():
            try:
                transport = client.get_transport()
                if transport is not None:
                    transport.close()   # 让卡住的 _wait_for_event 抛错退出
            except Exception:
                logger.debug("_open_sftp_bounded: transport close failed",
                             exc_info=True)
            raise paramiko.SSHException(
                f"SFTP subsystem did not respond within {SFTP_OPEN_TIMEOUT}s")
        if 'exc' in result:
            raise result['exc']
        sftp = result.get('sftp')
        if sftp is None:
            raise paramiko.SSHException("failed to open SFTP channel")
        return sftp

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
            sftp = self._open_sftp_bounded(client)
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
        except Exception as e:
            # known_hosts 损坏/不可读 → paramiko 拿不到历史指纹，主机密钥变更
            # （典型 MITM 特征）不再被拦截。绝不能静默：记日志 + 标记降级，
            # 由 UI 提示用户，让"防护已失效"变得可见而非无声通过。
            self._host_key_degraded = True
            logger.warning(
                "known_hosts 加载失败，主机密钥变更拦截已降级 (%s): %s",
                known_hosts, e,
            )
            try:
                self.host_key_check_degraded.emit(
                    t("ssh.host_key_degraded", path=known_hosts))
            except Exception:
                logger.debug("_connect_client: suppressed exception", exc_info=True)
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
                # 先在同一条 transport 上试 keyboard-interactive：OTP/2FA 主机
                # （密码 + 验证码多步提示）只有这条路能过；纯密码主机若开着
                # PAM 交互式认证也能走通。服务器不支持时再回落单密码。
                if not self._auth_keyboard_interactive(client, cfg):
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
                logger.debug("_connect_client: suppressed exception", exc_info=True)
            raise

        # 首次接受的未知主机 key 写回 known_hosts，让后续连接受 MITM 校验保护
        if host_key_policy.added:
            try:
                os.makedirs(os.path.dirname(known_hosts), exist_ok=True)
                client.save_host_keys(known_hosts)
            except Exception:
                logger.debug("_connect_client: suppressed exception", exc_info=True)

        # keep-alive：定期发 transport 层心跳，防止 NAT/防火墙把空闲连接掐掉
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(KEEPALIVE_INTERVAL)
        return client

    def _auth_keyboard_interactive(self, client: paramiko.SSHClient,
                                   cfg: HostConfig) -> bool:
        """在已失败的连接上补试 keyboard-interactive 认证（OTP/2FA 主机的唯一通路）。

        SSH 协议允许同一 transport 上多次认证尝试，所以 client.connect() 抛
        AuthenticationException 之后 transport 通常还活着，可以直接续认证。
        服务器的每步提示（Password: / Verification code: …）转给 interactive
        provider 在 UI 弹框；密码类回答按 alias 缓存，自动重连时自动作答、
        只把一次性的验证码留给用户重输。

        返回 True = 认证成功；False = 走不了这条路（transport 已关 / 没有
        provider / 服务器不支持 keyboard-interactive），调用方回落单密码逻辑。
        认证被用户取消或回答被服务器拒绝时直接抛 AuthenticationException。
        """
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            return False
        if self._interactive_provider is None and self._password_provider is None:
            return False
        username = cfg.user or getpass.getuser()
        secrets = self._auth_secrets.setdefault(cfg.alias, {})
        # cached_password_used：本次认证是否已经消费过密码（缓存自动作答或用户
        # 刚输入）。既防止错误的缓存密码被反复自动作答打满 MaxAuthTries，也让
        # 服务器再次要密码时能落回用户弹框。
        state = {"cached_password_used": False, "cancelled": False,
                 "password_candidate": None}

        def _ask(prompt: str, echo: bool) -> Optional[str]:
            if self._interactive_provider is not None:
                return self._interactive_provider(cfg.alias, prompt, echo)
            return self._password_provider(f"{cfg.alias} {prompt}")

        def handler(title, instructions, prompts):
            answers = []
            for prompt_text, echo in prompts:
                text = str(prompt_text).strip()
                if (looks_like_password_prompt(text)
                        and not state["cached_password_used"]
                        and secrets.get("password") is not None):
                    state["cached_password_used"] = True
                    answers.append(secrets["password"])
                    continue
                ans = _ask(text, bool(echo))
                if ans is None:
                    state["cancelled"] = True
                    raise paramiko.AuthenticationException(
                        "authentication cancelled by user")
                if looks_like_password_prompt(text):
                    state["cached_password_used"] = True
                    state["password_candidate"] = ans
                answers.append(ans)
            return answers

        try:
            transport.auth_interactive(username, handler)
        except paramiko.BadAuthenticationType:
            return False  # 服务器不收 keyboard-interactive → 回落单密码
        except Exception:
            if state["cancelled"]:
                # handler 里的取消异常经 transport 线程转手后类型可能变，统一收口
                raise paramiko.AuthenticationException(
                    "authentication cancelled by user")
            raise
        if state["password_candidate"] is not None:
            secrets["password"] = state["password_candidate"]
        return True

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
                logger.debug("_close_clients: suppressed exception", exc_info=True)

    def disconnect(self):
        """关闭连接（在 UI 线程调用安全，且不阻塞）。

        以前是 `submit(_do_disconnect).result(timeout=5)`：清理任务被排到单
        worker 队列末尾，只要前面还有在跑的传输/递归搜索就轮不到执行，于是
        必然等满 5 秒——GUI 硬冻 5s，超时后清理还没做，socket 反而泄漏。
        改为直接走 abort()：它绕开 executor 关 transport/socket，瞬间返回，
        同时让卡在 recv 上的 worker 立即抛错退出，是更彻底的清理。
        """
        self._was_connected = False   # 阻止 abort 之后的自动重连
        try:
            self.abort()
        except Exception as e:
            logger.debug("disconnect cleanup failed for %s: %s",
                         self.host_config.alias, e)
        # abort 已关掉所有 transport/client，这里只丢引用即可；不能再走
        # _teardown_connection 的 sftp.close()/client.close()——那是可能
        # 阻塞的调用，而本方法承诺在 UI 线程瞬时返回。
        self._sftp = None
        self._client = None
        self._jump_clients = []
        self._executor.shutdown(wait=False)
        self.disconnected.emit()

    def abort(self):
        """从任意线程强制中断当前连接，立刻让卡在 recv 上的 worker 操作失败。

        网络切换后 worker 可能阻塞在 SFTP recv 上，此时 disconnect() 把任务排到
        同一个单 worker 后面根本轮不到。这里**绕开 executor 直接关 transport/socket**：
        关闭底层 socket 会让阻塞中的 recv 立即抛错，对应 future 随即完成，UI 解冻。
        用于「取消」卡死的上传/下载。置 _was_connected=False，避免随后自动重连。
        """
        self._was_connected = False
        clients = list(self._jump_clients or [])
        if self._client is not None:
            clients.append(self._client)
        for c in clients:
            try:
                tr = c.get_transport()
                if tr is not None:
                    tr.close()      # 关 socket → 阻塞的 recv 立刻抛错
            except Exception:
                logger.debug("abort: suppressed exception", exc_info=True)
            try:
                c.close()
            except Exception:
                logger.debug("abort: suppressed exception", exc_info=True)

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
                logger.debug("_teardown_connection: suppressed exception", exc_info=True)
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

    def _reconnect_or_fail_fast(self):
        """重连入口：刚失败过就直接快速失败，不再重复承担重连成本。

        断线时会话的单 worker 队列里往往积着几十上百个操作（目录粘贴一次
        submit 几百个 upload）。若每个都各自跑一轮完整重连（3 次尝试、最坏
        数十秒），总时长就是 N 倍——用户看到的是「一个远程操作挂了以后，
        整个面板长时间毫无反应」。这里加一层冷却：只有第一个操作真正尝试
        重连，冷却期内的后续操作立刻抛错，让批量任务快速排空并把错误呈现
        给用户。冷却期过后允许再试一次，网络恢复后不需要用户手动重连。
        """
        last = self._last_reconnect_failure
        if last is not None and time.monotonic() - last < RECONNECT_COOLDOWN:
            raise RuntimeError(
                f"{t('ssh.reconnect_failed')} ({self.host_config.alias})")
        try:
            self._reconnect()
        except Exception:
            self._last_reconnect_failure = time.monotonic()
            raise
        else:
            self._last_reconnect_failure = None

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
    def upload_with_progress(self, local_path: str, remote_path: str,
                             progress_cb=None):
        """流式上传到远端，带字节级进度（与 download_with_progress 对称）。

        progress_cb(bytes_done, bytes_total) 由 paramiko 从 worker 线程里
        高频回调（每个 chunk 一次）。调用方需要自己做节流，并把更新切回
        UI 线程 —— 线程语义与 download_with_progress 完全一致。

        远端侧原子性：先传到 <目标>.part，传完再 posix_rename 改名；服务器
        不支持 posix-rename@openssh.com 扩展时回退普通 rename（SFTP rename
        在目标已存在时会失败，所以先删一次目标）。失败时清理远端 .part。

        注意：被 _auto_reconnect 装饰 —— 断线重连后整个文件从头重传，
        progress_cb 的 bytes_done 会从 0 重新涨（可接受）。
        """
        sftp = self._require()
        tmp_path = remote_path + ".part"
        try:
            sftp.put(local_path, tmp_path, callback=progress_cb)
            try:
                sftp.posix_rename(tmp_path, remote_path)
            except OSError:
                # 服务器不支持 posix_rename 扩展（或目标状态导致失败）→
                # 回退到「先删目标再 rename」。目标不存在的删除失败忽略。
                try:
                    sftp.remove(remote_path)
                except OSError:
                    pass
                sftp.rename(tmp_path, remote_path)
        except Exception:
            # 失败时清理远端半成品（连接已断时删不掉也只能算了）
            try:
                sftp.remove(tmp_path)
            except Exception:
                logger.debug("upload_with_progress: suppressed exception", exc_info=True)
            raise
        self.invalidate_cache(self._parent(remote_path))

    def remote_has_tar(self) -> bool:
        """探测远端是否可用 tar + POSIX shell（目录上传快路径的前置条件）。

        结果按会话缓存；探测过程本身出错（连接抖动等）不缓存，下次再试。
        `command -v` 返回 0 同时证明了默认 shell 是 POSIX 系（Windows sshd
        的 cmd.exe 会直接失败），所以后续拼 shell 命令是安全的。
        """
        if self._remote_has_tar is not None:
            return self._remote_has_tar
        if self._client is None:
            return False
        try:
            transport = self._client.get_transport()
            if transport is None or not transport.is_active():
                return False
            chan = transport.open_session(timeout=CONNECT_TIMEOUT)
            try:
                chan.settimeout(CONNECT_TIMEOUT)
                chan.exec_command("command -v tar")
                rc = chan.recv_exit_status()
            finally:
                chan.close()
            self._remote_has_tar = (rc == 0)
            return self._remote_has_tar
        except Exception:
            logger.debug("remote_has_tar probe failed", exc_info=True)
            return False

    @_auto_reconnect
    def upload_dir_tar(self, local_dir: str, remote_dir: str,
                       total_bytes: int = 0, progress_cb=None):
        """整目录上传快路径：本地打 tar 流 → 单条 SSH 通道 → 远端 tar 解包。

        逐文件 SFTP 上传每个文件要 5-6 次网络往返（open/write/close/rename），
        且在会话单 worker 上串行——几百个小文件的目录在几十 ms RTT 的链路上
        光往返就要几分钟。tar 流没有按文件的往返，吞吐只受带宽/加密限制，
        通常快 1-2 个数量级。语义与逐文件路径一致：解包进已存在/新建的
        remote_dir；符号链接原样保留（不展开）。

        progress_cb(bytes_done, total_bytes)：按已送入通道的字节回调（含
        tar 头部开销，显示用 total_bytes 封顶）。取消走 session.abort()
        关 socket → sendall 立刻抛错；abort 已置 _was_connected=False，
        _auto_reconnect 不会把被取消的传输再重跑一遍。
        """
        client = self._client
        transport = client.get_transport() if client is not None else None
        if transport is None:
            raise RuntimeError("not connected")
        chan = transport.open_session(timeout=CONNECT_TIMEOUT)
        err_buf = bytearray()

        def _drain():
            # 及时排空远端 stdout/stderr，防止远端输出填满通道窗口互相等死
            while chan.recv_stderr_ready():
                err_buf.extend(chan.recv_stderr(4096))
            while chan.recv_ready():
                chan.recv(4096)

        try:
            chan.settimeout(SFTP_OP_TIMEOUT)
            q = shlex.quote(remote_dir)
            chan.exec_command(f"mkdir -p {q} && tar -xpf - -C {q}")

            sent = {"n": 0}

            class _ChanWriter:
                """把 tarfile 的写出转成 channel.sendall，顺带计数/回调/排空。"""
                def write(self, data):
                    chan.sendall(data)
                    sent["n"] += len(data)
                    if progress_cb is not None:
                        done = sent["n"]
                        if total_bytes:
                            done = min(done, total_bytes)
                        progress_cb(done, total_bytes)
                    _drain()
                    return len(data)

                def flush(self):
                    pass

            # bufsize 调大：tar 块攒到 256KB 再 sendall，减少系统调用次数
            tf = tarfile.open(mode="w|", fileobj=_ChanWriter(),
                              bufsize=256 * 1024)
            try:
                tf.add(local_dir, arcname=".")
            finally:
                tf.close()
            chan.shutdown_write()

            # 等远端 tar 退出。流式解包（边收边解），EOF 后收尾极快；
            # recv_exit_status() 无超时参数，这里自己带截止轮询，
            # 避免远端异常挂死时 worker 永久阻塞。
            deadline = time.monotonic() + 60
            while not chan.exit_status_ready():
                _drain()
                if time.monotonic() > deadline:
                    raise RuntimeError("remote tar did not finish in time")
                time.sleep(0.05)
            _drain()
            rc = chan.recv_exit_status()
            if rc != 0:
                detail = bytes(err_buf).decode("utf-8", "replace").strip()
                raise RuntimeError(
                    f"remote tar exited {rc}: {detail[:400]}")
        finally:
            try:
                chan.close()
            except Exception:
                logger.debug("upload_dir_tar: channel close failed", exc_info=True)
        self.invalidate_cache(remote_dir)
        self.invalidate_cache(self._parent(remote_dir))

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
                logger.debug("download_with_progress: suppressed exception", exc_info=True)
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
