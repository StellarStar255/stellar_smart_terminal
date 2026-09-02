"""系统 ssh + ControlMaster 后端 —— 给 MFA/动态码堡垒机用的远程文件管理。

为什么不用 paramiko：JumpServer 这类堡垒机同时宣告 publickey/password/
keyboard-interactive，但真正能过的只有交互认证；paramiko 要么被密钥尝试打满
MaxAuthTries 掐线，要么在多步提示上和对端对不上。而系统 ssh 天然吃
~/.ssh/config —— Host 别名、IdentityFile、ProxyJump 跳板、IdentitiesOnly、
PreferredAuthentications 全部白拿，用户在终端里能连上的机器，这里就能连上。

做法（与「一次认证、长期复用」的目标严格对应）：
1. `mfa_login()` 开一条**主连接**：`ssh -M -N -f -o ControlPersist=8h`，动态码
   经 SSH_ASKPASS 喂进去，认证一次；
2. 之后所有操作（列目录/读写/上传下载）都是 `ssh -o ControlPath=<sock>`，
   复用那条已认证的连接，**不再有任何认证交互**，也没有每次握手的开销；
3. 文件操作走远端 POSIX 命令（ls/cat/mkdir/mv/rm/find/tar），不依赖 SFTP
   子系统 —— 有些堡垒机压根不开 sftp。

对外提供 ControlMasterSession，接口与 ssh_session.SSHSession 里被 Remote
Explorer 用到的那部分一致，面板可以直接换后端。

平台：ControlMaster 是 POSIX unix socket 的能力，Windows 的 OpenSSH 不支持
（is_supported() 会返回 False，调用方回落 paramiko）。
"""
import hashlib
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from app_logging import get_logger
from ssh_session import (HostConfig, RemoteEntry,
                         _CountingReader, _worth_compressing)

logger = get_logger(__name__)

# 单条远端命令的默认超时（秒）。传输类操作自己给更长的值。
CMD_TIMEOUT = 60
CONNECT_TIMEOUT = 12
LOGIN_TIMEOUT = 45          # 认证要等用户那边的堡垒机响应，给宽一点
SERVER_ALIVE_INTERVAL = 20
DEFAULT_KEEP_HOURS = 8
_CHUNK = 256 * 1024         # 传输块大小（也是进度回调粒度）

# ControlPath 走 unix socket，sockaddr_un.sun_path 只有 104 字节，超了 ssh 直接
# 报 "too long for Unix domain socket"。macOS 的 TMPDIR 是
# /var/folders/xx/yyyy…/T/ 这种长路径，光目录就吃掉一半，再加上 ssh 建连时自己
# 追加的 .XXXXXXXXXXXXXXXX 随机后缀就爆了 —— 固定用 /tmp 下的短目录，并且自己
# 用短哈希当文件名（用 %r@%h:%p 的话主机名一长照样爆）。
_CTL_SUFFIX_BUDGET = 20     # ssh 建连时追加的随机后缀
_CTL_MAX = 100              # 留 4 字节余量


def _ctl_dir() -> str:
    for base in ("/tmp", tempfile.gettempdir()):
        d = os.path.join(base, "stellar-ssh")
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            return d
        except OSError:
            continue
    return ""


def control_path_for(cfg: HostConfig) -> str:
    """这台主机的控制套接字路径；建不出（太长/没临时目录）返回空串。"""
    d = _ctl_dir()
    if not d:
        return ""
    key = hashlib.sha1(
        f"{cfg.alias}|{cfg.hostname}|{cfg.port}|{cfg.user}".encode("utf-8")
    ).hexdigest()[:12]
    path = os.path.join(d, f"c-{key}")
    # /tmp 在 macOS 上是 /private/tmp 的软链，ssh 按解析后的真实路径算长度
    try:
        real = os.path.join(os.path.realpath(d), f"c-{key}")
    except OSError:
        real = path
    if len(real) + _CTL_SUFFIX_BUDGET > _CTL_MAX:
        return ""
    return path


def is_supported() -> bool:
    """当前平台/环境能不能用 ControlMaster 主连接。"""
    if sys.platform.startswith("win"):
        return False        # Windows 的 OpenSSH 不支持 ControlMaster
    return shutil.which("ssh") is not None


def ssh_target(cfg: HostConfig) -> str:
    """交给 ssh 的目标。

    优先用 ~/.ssh/config 里的 Host 别名——这样 HostName/User/Port/IdentityFile/
    ProxyJump 全部由 ssh 自己解析，和用户在终端里敲 `ssh <别名>` 完全一致。
    只有内存态主机（没进 config）才退回 user@host。
    """
    if cfg.raw:
        return cfg.alias
    return f"{cfg.user}@{cfg.hostname}" if cfg.user else cfg.hostname


def _q(s) -> str:
    """远端 shell 引用。所有用户输入进远端命令行的唯一入口，漏了就是命令注入。"""
    return shlex.quote("" if s is None else str(s))


def _qpath(p) -> str:
    """远端路径引用：`~` 被引号包住就不是家目录了，要换成 "$HOME"。"""
    s = "" if p is None else str(p)
    if s == "~":
        return '"$HOME"'
    if s.startswith("~/"):
        return '"$HOME"/' + shlex.quote(s[2:])
    return shlex.quote(s)


# `ls -lA --time-style=+%s` 的一行：
#   drwxr-xr-x  2 user group  4096 1712345678 名字里可以有空格
_LS_EPOCH_RE = re.compile(
    r"^([bcdlps-])(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(.+)$")
# 老的/非 GNU 的 ls 不认 --time-style，回退 `ls -lA`。时间列不能写成"随便三个
# 字段"——文件名里可以有空格，必须按已知日期格式卡死，否则名字会被吃掉一截。
_LS_DATE = (r"(?:\w{3}\s+\d{1,2}\s+(?:\d{1,2}:\d{2}|\d{4})"
            r"|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
            r"(?:\s+[+-]\d{4})?)")
_LS_PLAIN_RE = re.compile(
    r"^([bcdlps-])(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s+("
    + _LS_DATE + r")\s+(.+)$")


def parse_ls_output(text: str, parent: str) -> list[RemoteEntry]:
    """把 `ls -lA` 的输出解析成 RemoteEntry 列表。

    这是本模块最容易出错的地方，所以单独导出以便测试：文件名里的空格、
    软链的 " -> target"、GNU/BSD 两种时间列都要吃得下。
    """
    out: list[RemoteEntry] = []
    for raw in (text or "").split("\n"):
        line = raw.rstrip("\r")
        if not line.strip() or re.match(r"^total\s", line, re.IGNORECASE):
            continue
        mtime = 0.0
        m = _LS_EPOCH_RE.match(line)
        if m:
            try:
                mtime = float(m.group(7))
            except ValueError:
                mtime = 0.0
        else:
            m = _LS_PLAIN_RE.match(line)
            if not m:
                continue
            # ISO 日期能直接解出时间戳；`Jul 10 12:33` 缺年份，不猜
            date_txt = m.group(7)
            if re.match(r"^\d{4}-\d{2}-\d{2}", date_txt):
                try:
                    mtime = time.mktime(time.strptime(
                        date_txt[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    try:
                        mtime = time.mktime(time.strptime(
                            date_txt[:16], "%Y-%m-%d %H:%M"))
                    except ValueError:
                        mtime = 0.0
        type_char = m.group(1)
        try:
            size = int(m.group(6))
        except ValueError:
            size = 0
        name = m.group(8)
        if type_char == "l":
            i = name.find(" -> ")
            if i > 0:
                name = name[:i]
        if name in (".", ".."):
            continue
        # 软链当普通文件对待（面板里双击会走 stat，届时按目标类型解析），
        # 但 is_link 如实上报——与 paramiko 后端 listdir_attr 的口径一致
        out.append(RemoteEntry(
            name=name,
            path=posixpath.join(parent, name) if parent else name,
            is_dir=(type_char == "d"),
            is_link=(type_char == "l"),
            size=size,
            mtime=mtime,
        ))
    return out


class MasterNotRunning(RuntimeError):
    """主连接不在了 —— 需要用户重新做一次 MFA 登录。"""


def _login_env(askpass: str) -> dict:
    """登录那条 ssh 的环境。动态码/密码**不在**这里：`-f` 转后台的常驻主连接会
    完整继承环境变量，同用户 `ps -E` / /proc/<pid>/environ 能读，且随
    ControlPersist 活好几个小时。答案走 askpass 同目录下的 0600 文件
    （见 _write_secret_files），登录一返回整个目录就删。"""
    env = dict(os.environ)
    env.update({
        # 老版本 ssh 没有 DISPLAY 就不调 askpass
        "DISPLAY": env.get("DISPLAY") or "stellar:0",
        "SSH_ASKPASS": askpass,
        # 有/无 tty 都强制走 askpass（OpenSSH >= 8.4）
        "SSH_ASKPASS_REQUIRE": "force",
    })
    # 万一外层环境里残留了旧版的变量名，也别带进去
    env.pop("STELLAR_SSH_CODE", None)
    env.pop("STELLAR_SSH_PASSWORD", None)
    return env


_SECRET_CODE_FILE = "code"
_SECRET_PASSWORD_FILE = "password"


def _write_secret_files(tmpdir: str, code: str, password: str) -> None:
    """把动态码/密码写成 askpass 同目录下的 0600 文件（目录本身 0700）。"""
    for name, value in ((_SECRET_CODE_FILE, code or ""),
                        (_SECRET_PASSWORD_FILE, password or "")):
        path = os.path.join(tmpdir, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)      # 不带换行：askpass 用 cat 原样吐出


_ASKPASS_SCRIPT = """#!/bin/sh
# ssh 没有 tty 时用这个程序取答案；$1 是服务器的提示语。
# 认出「密码」类提示就回密码，其余（[MFA auth] / Verification code / 验证码…）
# 一律回动态码。两个值放在本脚本同目录的 0600 文件里（不走环境变量：转后台的
# 主连接会把环境带走一整个 ControlPersist 周期），登录一结束整目录即删。
d=$(dirname "$0")
case "$1" in
  *[Pp]assword*|*[Pp]assphrase*|*密码*|*口令*) cat "$d/password" ;;
  *) if [ -s "$d/code" ]; then cat "$d/code"; else cat "$d/password"; fi ;;
esac
"""


def mfa_login(cfg: HostConfig, code: str = "", password: str = "",
              hours: int = DEFAULT_KEEP_HOURS) -> str:
    """输一次动态码，开一条常驻主连接。返回 ControlPath。

    平时的调用都带 BatchMode=yes（绝不弹交互提示，否则后台线程会被挂死），
    于是要动态码的堡垒机一律 "Permission denied (keyboard-interactive)"。
    这里单独开一条 `-M -N -f` 的主连接把码喂进去认证一次，之后所有普通调用
    靠同一个 ControlPath 复用它，不再需要任何交互。
    """
    if not is_supported():
        raise RuntimeError("这个平台不支持 ssh 主连接（ControlMaster）")
    ctl = control_path_for(cfg)
    if not ctl:
        raise RuntimeError("ControlPath 建不出来（临时目录路径太长）")
    if not code and not password:
        raise ValueError("没填动态码")
    tmpdir = tempfile.mkdtemp(prefix="stellar-askpass-")   # mkdtemp 本身 0700
    askpass = os.path.join(tmpdir, "askpass.sh")
    try:
        with open(askpass, "w", encoding="utf-8") as fh:
            fh.write(_ASKPASS_SCRIPT)
        os.chmod(askpass, 0o700)
        _write_secret_files(tmpdir, code, password)
        # hours=0 是「不自动断开」→ ControlPersist=yes（一直留着，直到被显式
        # 关掉或机器重启）；其余按小时数，上限 24h
        persist = "yes" if int(hours or 0) <= 0 else f"{min(24, int(hours))}h"
        args = [
            "ssh",
            "-o", "BatchMode=no",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT + 13}",
            "-o", f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
            "-o", "NumberOfPasswordPrompts=1",
            "-o", "ControlMaster=yes",
            "-o", f"ControlPath={ctl}",
            "-o", f"ControlPersist={persist}",
        ]
        if not cfg.raw:
            # 内存态主机（没进 ~/.ssh/config）才需要我们自己补端口/密钥
            if cfg.port and cfg.port != 22:
                args += ["-o", f"Port={int(cfg.port)}"]
            if cfg.identity_file and os.path.isfile(cfg.identity_file):
                args += ["-o", f"IdentityFile={cfg.identity_file}"]
        args += ["-N", "-f", ssh_target(cfg)]
        proc = subprocess.run(
            args, env=_login_env(askpass),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=LOGIN_TIMEOUT,
        )
        if proc.returncode == 0:
            logger.info("[SSH-CTL] %s: master connection up (persist=%s)",
                        cfg.alias, persist)
            return ctl
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        tail = " · ".join([l for l in err.split("\n") if l.strip()][-3:])
        raise RuntimeError(tail or f"ssh 退出码 {proc.returncode}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("登录超时（堡垒机没有在预期时间内完成认证）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def master_socket_exists(cfg: HostConfig) -> bool:
    """控制套接字文件在不在。

    纯文件系统检查、不起进程，所以可以在 UI 线程里随便调（master_alive 要
    起一条 ssh，最坏会阻塞几秒，只能在工作线程里调）。套接字在不代表主连接
    一定活着——真要确认还得 master_alive。
    """
    ctl = control_path_for(cfg)
    return bool(ctl) and os.path.exists(ctl)


def master_alive(cfg: HostConfig) -> bool:
    """主连接还在不在（ssh -O check）。"""
    if not is_supported():
        return False
    ctl = control_path_for(cfg)
    if not ctl or not os.path.exists(ctl):
        return False
    try:
        proc = subprocess.run(
            ["ssh", "-O", "check", "-o", f"ControlPath={ctl}", ssh_target(cfg)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=8,
        )
        return proc.returncode == 0
    except Exception:
        logger.debug("master_alive check failed", exc_info=True)
        return False


def master_exit(cfg: HostConfig) -> bool:
    """关掉主连接（ssh -O exit）。下次操作需要重新 MFA 登录。"""
    if not is_supported():
        return False
    ctl = control_path_for(cfg)
    if not ctl:
        return False
    try:
        proc = subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={ctl}", ssh_target(cfg)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=8,
        )
        return proc.returncode == 0
    except Exception:
        logger.debug("master_exit failed", exc_info=True)
        return False


# ---------- 端口转发（挂在已有主连接上） ----------
# `ssh -O forward/cancel` 是对**已经建好的主连接**下指令，不重新认证 ——
# 这是要动态码的堡垒机唯一可行的路子（想临时开个端口转发，总不能再输一次码）。

_FWD_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
FORWARD_TYPES = ("L", "R", "D")


def _fwd_port(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"端口不合法: {value}")
    if not 1 <= n <= 65535:
        raise ValueError(f"端口要在 1-65535 之间: {value}")
    return str(n)


def _fwd_host(value, default: str) -> str:
    v = str(value or default or "").strip()
    if not _FWD_HOST_RE.match(v):
        raise ValueError(f"主机名/地址不合法: {v}")
    return v


def forward_args(spec: dict) -> list:
    """把一条转发规则转成 ssh 参数（-L/-R/-D）。规则不合法直接抛 ValueError。"""
    kind = str(spec.get("type") or "L").upper()
    if kind not in FORWARD_TYPES:
        raise ValueError(f"未知的转发类型: {kind}")
    bind = f'{_fwd_host(spec.get("bind_host"), "127.0.0.1")}:{_fwd_port(spec.get("bind_port"))}'
    if kind == "D":
        return ["-D", bind]
    dest = f'{_fwd_host(spec.get("dest_host"), "127.0.0.1")}:{_fwd_port(spec.get("dest_port"))}'
    return [("-R" if kind == "R" else "-L"), f"{bind}:{dest}"]


def forward_label(spec: dict) -> str:
    """一条转发的人话描述（UI 与日志共用）。"""
    bind = f'{spec.get("bind_host") or "127.0.0.1"}:{spec.get("bind_port")}'
    kind = str(spec.get("type") or "L").upper()
    if kind == "D":
        return f"SOCKS5 {bind}"
    dest = f'{spec.get("dest_host") or "127.0.0.1"}:{spec.get("dest_port")}'
    return (f"远端 {bind} → 本机 {dest}" if kind == "R"
            else f"本机 {bind} → 远端 {dest}")


def local_port_busy(host: str, port) -> bool:
    """本机端口是否已被占用（-L/-D 是在本机开监听）。

    ssh 端口占用时只会甩一句 "Port forwarding failed"，看不出是谁占的；
    自己先 bind 一下，能提前给出准确得多的报错。
    """
    import socket
    try:
        p = int(port)
    except (TypeError, ValueError):
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host or "127.0.0.1", p))
        return False
    except OSError:
        return True
    finally:
        s.close()


def who_holds_port(port) -> str:
    """查谁占着这个端口（lsof）；查不出来返回空串。"""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return ""
    names = []
    for line in (proc.stdout or b"").decode("utf-8", "replace").split("\n")[1:]:
        cols = line.split()
        if len(cols) > 1 and cols[0] and (cols[0], cols[1]) not in names:
            names.append((cols[0], cols[1]))
    return "、".join(f"{n}(pid {p})" for n, p in names)


def forward_apply(cfg: HostConfig, spec: dict, cancel: bool = False) -> str:
    """在已有主连接上加/撤一条端口转发。返回 ssh 的输出（通常为空）。

    没有主连接就直接报错——建主连接要动态码，那是 UI 该引导用户去做的事。
    """
    if not is_supported():
        raise RuntimeError("这个平台不支持 ssh 主连接（ControlMaster）")
    ctl = control_path_for(cfg)
    if not ctl:
        raise RuntimeError("ControlPath 建不出来（临时目录路径太长）")
    args = forward_args(spec)
    if not cancel and str(spec.get("type") or "L").upper() != "R":
        # -L/-D 在本机开监听：先自查端口占用，给出比 ssh 有用得多的报错
        host = spec.get("bind_host") or "127.0.0.1"
        port = spec.get("bind_port")
        if local_port_busy(host, port):
            who = who_holds_port(port)
            raise RuntimeError(
                f"本机端口 {port} 已被占用" + (f"：{who}" if who else ""))
    proc = subprocess.run(
        ["ssh", "-O", ("cancel" if cancel else "forward"),
         "-o", f"ControlPath={ctl}", *args, ssh_target(cfg)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=15)
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip() or out
        tail = " · ".join([l for l in err.split("\n") if l.strip()][-2:])
        if "control socket connect" in err.lower() or "no such file" in err.lower():
            raise MasterNotRunning("主连接不在了 —— 先做一次 MFA 登录再加转发")
        raise RuntimeError(tail or f"ssh -O {'cancel' if cancel else 'forward'} "
                                   f"退出码 {proc.returncode}")
    return out


class ControlMasterSession(QObject):
    """复用系统 ssh 主连接的远程会话。

    接口与 ssh_session.SSHSession 中被 Remote Explorer 用到的那部分一致
    （submit/listdir/stat/mkdir/rename/remove/upload/download/…），面板可以
    直接换后端。所有远端操作都是一条 `ssh -o ControlPath=<sock> … <命令>`，
    复用已认证的主连接，不再有任何认证交互。
    """

    connected = pyqtSignal()
    connect_failed = pyqtSignal(str)
    disconnected = pyqtSignal()
    host_key_check_degraded = pyqtSignal(str)

    def __init__(self, host_config: HostConfig, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.host_config = host_config
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"sshctl-{host_config.alias}")
        self._lock = threading.Lock()
        self._home: Optional[str] = None
        self._alive = False
        self._listdir_cache: dict[str, tuple[float, list[RemoteEntry]]] = {}
        self._cache_ttl = 30.0
        self._cache_lock = threading.Lock()
        self._remote_has_tar: Optional[bool] = None
        self._remote_has_gzip: Optional[bool] = None
        # 在跑的子进程：abort() 要能立刻把它们杀掉（取消卡住的传输）
        self._procs: set = set()
        self._procs_lock = threading.Lock()
        self._aborted = False

    # ---------- 生命周期 ----------

    def used_otp_auth(self) -> bool:
        """这条后端天生就是给动态码主机用的。"""
        return True

    def is_connected(self) -> bool:
        return self._alive

    def is_alive(self) -> bool:
        return self._alive

    def connect_async(self, password_provider=None, passphrase_provider=None,
                      interactive_provider=None, prefer_interactive=False,
                      mfa_code: str = "", mfa_password: str = "",
                      keep_hours: int = DEFAULT_KEEP_HOURS) -> Future:
        """建立（或复用）主连接。provider 参数只为与 SSHSession 保持同签名。

        主连接已经在跑就直接复用——不用再输码，这正是"常驻主连接"的意义。
        """
        fut = self._executor.submit(self._do_connect, mfa_code, mfa_password,
                                    keep_hours)

        def _on_done(f: Future):
            try:
                f.result()
                self.connected.emit()
            except Exception as e:      # noqa: BLE001 — 原样报给 UI
                self.connect_failed.emit(str(e))

        fut.add_done_callback(_on_done)
        return fut

    def _do_connect(self, code: str, password: str, keep_hours: int):
        if not master_alive(self.host_config):
            mfa_login(self.host_config, code=code, password=password,
                      hours=keep_hours)
        self._aborted = False
        self._alive = True
        self._home = self._run("cd ~ && pwd -P").strip() or "/"

    def disconnect(self):
        """停止使用该会话。

        注意：**不关主连接** —— 它是花了一个动态码换来的，ControlPersist 会
        管它的寿命；面板的空闲看门狗需要真正关掉时调 shutdown_master()。
        """
        self._alive = False
        self.abort()
        self._executor.shutdown(wait=False)
        self.disconnected.emit()

    def shutdown_master(self) -> bool:
        """真正关掉主连接（下次要重新输码）。"""
        return master_exit(self.host_config)

    def abort(self):
        """杀掉在跑的子进程，让卡住的传输立刻失败（取消按钮走这里）。"""
        self._aborted = True
        with self._procs_lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                logger.debug("abort: kill failed", exc_info=True)

    # ---------- 命令执行 ----------

    def _base_args(self) -> list:
        ctl = control_path_for(self.host_config)
        if not ctl:
            raise RuntimeError("ControlPath 建不出来（临时目录路径太长）")
        return [
            "ssh",
            # 绝不弹交互提示：后台线程没人能回答，弹了就是永久挂起
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
            "-o", f"ServerAliveInterval={SERVER_ALIVE_INTERVAL}",
            # 只复用已有主连接，绝不自己去建（建了也过不了 MFA）
            "-o", "ControlMaster=no",
            "-o", f"ControlPath={ctl}",
            # 主连接死了 OpenSSH 会**静默回落**成一次完整直连：BatchMode 只禁
            # 交互提示，agent 里的公钥仍会逐个试——正是把堡垒机 MaxAuthTries
            # 打满/被限速的场景。把所有认证方式关掉，回落连接零尝试即失败。
            "-o", "PubkeyAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "PasswordAuthentication=no",
        ]

    _MASTER_GONE_MSG = "主连接已断开（connection lost）——请重新 MFA 登录"

    def _spawn(self, remote_cmd: str, stdin=subprocess.DEVNULL,
               stdout=subprocess.PIPE) -> subprocess.Popen:
        ctl = control_path_for(self.host_config)
        if not ctl or not os.path.exists(ctl):
            # 套接字都没了就别起 ssh：省一次握手，也不给回落直连任何机会。
            # 文案含 "connection lost"，面板据此走「重连」提示。
            raise MasterNotRunning(self._MASTER_GONE_MSG)
        args = self._base_args() + [ssh_target(self.host_config), remote_cmd]
        proc = subprocess.Popen(args, stdin=stdin, stdout=stdout,
                                stderr=subprocess.PIPE)
        with self._procs_lock:
            self._procs.add(proc)
        return proc

    def _reap(self, proc: subprocess.Popen):
        with self._procs_lock:
            self._procs.discard(proc)

    @staticmethod
    def _explain(stderr: str, returncode: Optional[int] = None) -> str:
        """把 ssh 的报错翻译成用户能行动的说法。

        returncode 是区分「ssh 自己失败」与「远端命令失败」的唯一可靠依据：
        ssh 连接/认证层错误固定退出 255，远端命令的退出码原样透传（1/2…）。
        以前只要 stderr 含 "permission denied" 就报"主连接已断开"，远端目录
        无写权限做 mkdir/rm 也会逼用户重输动态码。
        """
        low = (stderr or "").lower()
        if ("control socket connect" in low or "no such file or directory" in low
                and "controlpath" in low) or "connection refused" in low:
            return "主连接已断开（connection lost）——请重新 MFA 登录"
        if "permission denied" in low and returncode == 255:
            return ("主连接已断开（connection lost）：ssh 又要认证了，"
                    "请重新 MFA 登录")
        return (stderr or "").strip()

    def _run(self, remote_cmd: str, timeout: int = CMD_TIMEOUT) -> str:
        """跑一条远端命令，返回 stdout；非零退出抛异常。"""
        with self._lock:
            proc = self._spawn(remote_cmd)
            try:
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise RuntimeError(f"远端命令超时: {remote_cmd[:60]}")
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            msg = self._explain((err or b"").decode("utf-8", "replace"), proc.returncode)
            raise RuntimeError(msg or f"远端命令失败 (exit {proc.returncode})")
        return (out or b"").decode("utf-8", "replace")

    def submit(self, fn: Callable, *args, **kwargs) -> Future:
        return self._executor.submit(fn, *args, **kwargs)

    # ---------- 缓存 ----------

    @staticmethod
    def _parent(path: str) -> str:
        return posixpath.dirname(path.rstrip("/")) or "/"

    def invalidate_cache(self, path: Optional[str] = None):
        with self._cache_lock:
            if path is None:
                self._listdir_cache.clear()
            else:
                self._listdir_cache.pop(path, None)

    def home(self) -> str:
        return self._home or "/"

    # ---------- 文件操作 ----------

    def listdir(self, path: str, use_cache: bool = True) -> list[RemoteEntry]:
        if use_cache:
            with self._cache_lock:
                cached = self._listdir_cache.get(path)
            if cached and (time.time() - cached[0]) < self._cache_ttl:
                return list(cached[1])
        out = self._run(
            f"cd -- {_qpath(path)} && pwd -P && "
            f"{{ LC_ALL=C ls -lA --time-style=+%s . 2>/dev/null "
            f"|| LC_ALL=C ls -lA . ; }}", timeout=90)
        nl = out.find("\n")
        abs_path = (out if nl < 0 else out[:nl]).strip() or path
        entries = parse_ls_output("" if nl < 0 else out[nl + 1:], abs_path)
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        with self._cache_lock:
            self._listdir_cache[path] = (time.time(), entries)
        return list(entries)

    def stat(self, path: str) -> RemoteEntry:
        # -L 必须有：软链上 ls -ld 报的是链接本身的大小（= 目标路径的字符数），
        # 拿它当分母算进度会算错。[ -d ] / [ -e ] 本来就跟随软链，口径才一致。
        # 首行形如 "dir" / "file link" / "none"：[ -L ] 不跟随软链，把链接
        # 身份附在同一行上报（与 paramiko 后端 stat 的 is_link 口径一致）。
        q = _qpath(path)
        out = self._run(
            f"if [ -d {q} ]; then k=dir; "
            f"elif [ -e {q} ]; then k=file; else k=none; fi; "
            f"if [ -L {q} ]; then echo \"$k link\"; else echo \"$k\"; fi; "
            f"LC_ALL=C ls -ldnL --time-style=+%s -- {q} 2>/dev/null || true")
        lines = out.split("\n")
        head = (lines[0] or "").split()
        kind = head[0] if head else ""
        is_link = "link" in head[1:]
        if kind == "none" and not is_link:
            raise FileNotFoundError(f"远端路径不存在: {path}")
        parent = self._parent(path)
        name = posixpath.basename(path.rstrip("/")) or path
        parsed = parse_ls_output("\n".join(lines[1:]), parent)
        size = parsed[0].size if parsed else 0
        mtime = parsed[0].mtime if parsed else 0.0
        return RemoteEntry(name=name, path=path, is_dir=(kind == "dir"),
                           is_link=is_link, size=size, mtime=mtime)

    def read_file(self, path: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
        st = self.stat(path)
        if st.size and st.size > max_bytes:
            raise ValueError(f"file too large ({st.size} bytes, limit {max_bytes})")
        with self._lock:
            proc = self._spawn(f"cat -- {_qpath(path)}")
            try:
                out, err = proc.communicate(timeout=CMD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise RuntimeError(f"远端读取超时: {path}")
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            raise RuntimeError(self._explain((err or b"").decode("utf-8", "replace"), proc.returncode))
        return out or b""

    def write_file(self, path: str, content: bytes):
        with self._lock:
            proc = self._spawn(f"cat > {_qpath(path)}", stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL)
            try:
                _out, err = proc.communicate(input=content, timeout=CMD_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise RuntimeError(f"远端写入超时: {path}")
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            raise RuntimeError(self._explain((err or b"").decode("utf-8", "replace"), proc.returncode))
        self.invalidate_cache(self._parent(path))

    def mkdir(self, path: str):
        self._run(f"mkdir -p -- {_qpath(path)}")
        self.invalidate_cache(self._parent(path))

    def rmdir(self, path: str):
        self._run(f"rmdir -- {_qpath(path)}")
        self.invalidate_cache(self._parent(path))
        self.invalidate_cache(path)

    def remove(self, path: str):
        self._run(f"rm -f -- {_qpath(path)}")
        self.invalidate_cache(self._parent(path))

    def remove_tree(self, path: str):
        self._run(f"rm -rf -- {_qpath(path)}", timeout=300)
        self.invalidate_cache(self._parent(path))
        self.invalidate_cache(path)

    def rename(self, old: str, new: str):
        self._run(f"mv -- {_qpath(old)} {_qpath(new)}")
        self.invalidate_cache(self._parent(old))
        self.invalidate_cache(self._parent(new))
        self.invalidate_cache(old)

    # ---------- 传输 ----------
    # 单文件走 `ssh + cat` 而不是 scp：OpenSSH 9 起 scp 默认改用 SFTP 协议，
    # 远端路径不再交给远端 shell 解析——我们加的引号会被当成文件名的一部分，
    # 传出个真带引号的文件名还不报错。cat 的引号规则由我们自己定，新旧
    # OpenSSH 行为一致，也照样复用主连接。

    def download(self, remote_path: str, local_path: str):
        self.download_with_progress(remote_path, local_path, None)

    def download_with_progress(self, remote_path: str, local_path: str,
                               progress_cb=None):
        st = self.stat(remote_path)
        total = st.size or 0
        tmp_path = local_path + ".part"
        with self._lock:
            proc = self._spawn(f"cat -- {_qpath(remote_path)}")
            done = 0
            try:
                with open(tmp_path, "wb") as fh:
                    while True:
                        chunk = proc.stdout.read(_CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if progress_cb is not None:
                            progress_cb(done, total)
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                proc.wait(timeout=CMD_TIMEOUT)
            except Exception:
                proc.kill()
                self._cleanup(tmp_path)
                raise
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            self._cleanup(tmp_path)
            raise RuntimeError(self._explain(err, proc.returncode) or f"下载失败 (exit {proc.returncode})")
        os.replace(tmp_path, local_path)
        return SimpleNamespace(st_size=st.size, st_mtime=st.mtime)

    @staticmethod
    def _cleanup(path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            logger.debug("cleanup failed: %s", path, exc_info=True)

    def upload(self, local_path: str, remote_path: str):
        self.upload_with_progress(local_path, remote_path, None)

    def upload_with_progress(self, local_path: str, remote_path: str,
                             progress_cb=None):
        total = os.path.getsize(local_path)
        tmp_remote = remote_path + ".part"
        with self._lock:
            # 先落 .part 再改名（半成品不会被别人看见），但两步放进**同一条**
            # 远端命令：每多一条 ssh 就多一次进程启动，批量上传时这笔开销很实在
            proc = self._spawn(
                f"cat > {_qpath(tmp_remote)} && mv -- {_qpath(tmp_remote)} "
                f"{_qpath(remote_path)}",
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            done = 0
            try:
                with open(local_path, "rb") as fh:
                    while True:
                        chunk = fh.read(_CHUNK)
                        if not chunk:
                            break
                        proc.stdin.write(chunk)
                        done += len(chunk)
                        if progress_cb is not None:
                            progress_cb(done, total)
                proc.stdin.close()
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                proc.wait(timeout=CMD_TIMEOUT)
            except Exception:
                proc.kill()
                raise
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            # 远端半成品清掉，别在目标目录留个 .part
            try:
                self._run(f"rm -f -- {_qpath(tmp_remote)}")
            except Exception:
                logger.debug("upload cleanup failed", exc_info=True)
            raise RuntimeError(self._explain(err, proc.returncode) or f"上传失败 (exit {proc.returncode})")
        self.invalidate_cache(self._parent(remote_path))

    def remote_has_tar(self) -> bool:
        if self._remote_has_tar is not None:
            return self._remote_has_tar
        try:
            self._run("command -v tar", timeout=20)
            self._remote_has_tar = True
        except Exception:
            self._remote_has_tar = False
        return self._remote_has_tar

    def remote_has_gzip(self) -> bool:
        """远端有没有 gzip —— 有就把 tar 流压着传（文本能省 5-10 倍字节）。"""
        if self._remote_has_gzip is not None:
            return self._remote_has_gzip
        try:
            self._run("command -v gzip", timeout=20)
            self._remote_has_gzip = True
        except Exception:
            self._remote_has_gzip = False
        return self._remote_has_gzip

    def upload_dir_tar(self, local_dir: str, remote_dir: str,
                       total_bytes: int = 0, progress_cb=None):
        """整目录上传快路径：本地打 tar 流 → 主连接 → 远端解包。

        逐文件上传每个文件都要起一条 ssh 进程，几百个小文件能拖上几分钟；
        tar 流没有按文件的往返，通常快 1-2 个数量级。
        """
        self._stream_tar(remote_dir,
                         lambda tf, _note: tf.add(local_dir, arcname="."),
                         total_bytes, progress_cb,
                         compress=self.remote_has_gzip())

    def upload_files_tar(self, local_paths: list, remote_dir: str,
                         total_bytes: int = 0, progress_cb=None,
                         should_stop=None) -> list:
        """一批文件/目录打成一条 tar 流上传（arcname 取 basename）。

        返回读不了的条目 [(路径, 原因)] —— 单个坏文件不该让整批失败。

        should_stop()：每个文件开始前问一次，True 就在**两个文件之间**停下来
        正常收尾，远端解出来的都是完整文件（用户点「取消」走这条，而不是把
        连接掐断留半截文件）。签名必须与 ssh_session.SSHSession 完全一致 ——
        面板是按同一套接口调两个后端的。
        """
        skipped: list = []

        def _add(tf, note_raw):
            for item in local_paths:
                if should_stop is not None and should_stop():
                    break
                # 每项可以是路径，也可以是 (路径, 归档名) —— 粘贴时目标可能被
                # 改过名（"x (2).txt"），归档名就得用改过的那个
                if isinstance(item, (tuple, list)):
                    p, arc = item[0], item[1]
                else:
                    p, arc = item, os.path.basename(str(item).rstrip(os.sep))
                try:
                    info = tf.gettarinfo(p, arcname=arc)
                    if info is not None and info.isreg():
                        # 自己读文件体：压缩之后线上字节 ≠ 文件字节，
                        # 进度必须按原始字节算
                        with open(p, "rb") as fh:
                            tf.addfile(info, _CountingReader(fh, note_raw))
                    else:
                        tf.add(p, arcname=arc)   # 目录 / 软链：没有数据体
                except Exception as e:      # noqa: BLE001 — 逐个跳过并回报
                    logger.warning("upload_files_tar: skipping %s: %s", p, e)
                    skipped.append((p, str(e)))

        self._stream_tar(remote_dir, _add, total_bytes, progress_cb,
                         compress=_worth_compressing(local_paths)
                         and self.remote_has_gzip())
        return skipped

    def _stream_tar(self, remote_dir: str, add_entries, total_bytes: int = 0,
                    progress_cb=None, compress: bool = False):
        """把 add_entries 塞进 tarfile 的内容经主连接灌给远端 tar 解包。

        用 Python 的 tarfile 而不是本机 tar 命令：进度按真实送出的字节算，
        入口既可以是"一个目录"也可以是"一批路径"，也不用要求本机装 tar。
        """
        import tarfile

        q_remote = _qpath(remote_dir)
        with self._lock:
            flags = "-xzpf" if compress else "-xpf"
            proc = self._spawn(
                f"mkdir -p {q_remote} && tar {flags} - -C {q_remote}",
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            sent = {"n": 0}
            # 压缩后线上字节 ≠ 文件字节，进度按原始字节算（否则条子提前
            # 跑满、逐文件定位全错）
            raw = {"n": 0, "used": False}

            def note_raw(n):
                raw["used"] = True
                raw["n"] += n
                if progress_cb is not None:
                    done = raw["n"]
                    if total_bytes:
                        done = min(done, total_bytes)
                    progress_cb(done, total_bytes)

            class _StdinWriter:
                """tarfile 的写出 → ssh stdin，顺带计数/回调。"""

                def write(self, data):
                    proc.stdin.write(data)
                    sent["n"] += len(data)
                    if progress_cb is not None and not raw["used"]:
                        done = sent["n"]
                        if total_bytes:
                            done = min(done, total_bytes)
                        progress_cb(done, total_bytes)
                    return len(data)

                def flush(self):
                    pass

            try:
                # bufsize 调大：攒到 256KB 再写一次，减少系统调用
                tf = tarfile.open(mode="w|gz" if compress else "w|",
                                  fileobj=_StdinWriter(), bufsize=_CHUNK,
                                  **({"compresslevel": 1} if compress else {}))
                try:
                    add_entries(tf, note_raw)
                finally:
                    tf.close()
                proc.stdin.close()
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                proc.wait(timeout=600)
            except Exception:
                proc.kill()
                raise
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            raise RuntimeError(self._explain(err, proc.returncode)
                               or f"上传失败 (exit {proc.returncode})")
        self.invalidate_cache(remote_dir)
        self.invalidate_cache(self._parent(remote_dir))


__all__ = [
    "ControlMasterSession", "MasterNotRunning", "FORWARD_TYPES",
    "control_path_for", "forward_apply", "forward_args", "forward_label",
    "is_supported", "local_port_busy", "mfa_login", "master_alive",
    "master_exit", "master_socket_exists", "parse_ls_output", "ssh_target",
    "who_holds_port",
]
