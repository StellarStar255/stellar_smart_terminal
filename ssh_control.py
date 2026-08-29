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
import stat as stat_mod
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
from ssh_session import HostConfig, RemoteEntry

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
        # 软链当普通文件对待（面板里双击会走 stat，届时按目标类型解析）
        out.append(RemoteEntry(
            name=name,
            path=posixpath.join(parent, name) if parent else name,
            is_dir=(type_char == "d"),
            size=size,
            mtime=mtime,
        ))
    return out


class MasterNotRunning(RuntimeError):
    """主连接不在了 —— 需要用户重新做一次 MFA 登录。"""


def _login_env(code: str, password: str, askpass: str) -> dict:
    env = dict(os.environ)
    env.update({
        # 老版本 ssh 没有 DISPLAY 就不调 askpass
        "DISPLAY": env.get("DISPLAY") or "stellar:0",
        "SSH_ASKPASS": askpass,
        # 有/无 tty 都强制走 askpass（OpenSSH >= 8.4）
        "SSH_ASKPASS_REQUIRE": "force",
        "STELLAR_SSH_CODE": code or "",
        "STELLAR_SSH_PASSWORD": password or "",
    })
    return env


_ASKPASS_SCRIPT = """#!/bin/sh
# ssh 没有 tty 时用这个程序取答案；$1 是服务器的提示语。
# 认出「密码」类提示就回密码，其余（[MFA auth] / Verification code / 验证码…）
# 一律回动态码。两个值只经环境变量传进来，不落盘。
case "$1" in
  *[Pp]assword*|*[Pp]assphrase*|*密码*|*口令*) printf %s "${STELLAR_SSH_PASSWORD}" ;;
  *) printf %s "${STELLAR_SSH_CODE:-$STELLAR_SSH_PASSWORD}" ;;
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
    tmpdir = tempfile.mkdtemp(prefix="stellar-askpass-")
    askpass = os.path.join(tmpdir, "askpass.sh")
    try:
        with open(askpass, "w", encoding="utf-8") as fh:
            fh.write(_ASKPASS_SCRIPT)
        os.chmod(askpass, 0o700)
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
            args, env=_login_env(code, password, askpass),
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
        ]

    def _spawn(self, remote_cmd: str, stdin=subprocess.DEVNULL,
               stdout=subprocess.PIPE) -> subprocess.Popen:
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
    def _explain(stderr: str) -> str:
        """把 ssh 的报错翻译成用户能行动的说法。"""
        low = (stderr or "").lower()
        if ("control socket connect" in low or "no such file or directory" in low
                and "controlpath" in low) or "connection refused" in low:
            return "主连接已断开（connection lost）——请重新 MFA 登录"
        if "permission denied" in low:
            return ("主连接已断开（connection lost）：ssh 又要认证了，"
                    "请重新 MFA 登录")
        return stderr.strip()

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
            msg = self._explain((err or b"").decode("utf-8", "replace"))
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
        out = self._run(
            f"if [ -d {_qpath(path)} ]; then echo dir; "
            f"elif [ -e {_qpath(path)} ]; then echo file; else echo none; fi; "
            f"LC_ALL=C ls -ldnL --time-style=+%s -- {_qpath(path)} 2>/dev/null || true")
        lines = out.split("\n")
        kind = (lines[0] or "").strip()
        if kind == "none":
            raise FileNotFoundError(f"远端路径不存在: {path}")
        parent = self._parent(path)
        name = posixpath.basename(path.rstrip("/")) or path
        parsed = parse_ls_output("\n".join(lines[1:]), parent)
        size = parsed[0].size if parsed else 0
        mtime = parsed[0].mtime if parsed else 0.0
        return RemoteEntry(name=name, path=path, is_dir=(kind == "dir"),
                           size=size, mtime=mtime)

    def read_file(self, path: str, max_bytes: int = 5 * 1024 * 1024) -> bytes:
        st = self.stat(path)
        if st.size and st.size > max_bytes:
            raise ValueError(f"file too large ({st.size} bytes, limit {max_bytes})")
        with self._lock:
            proc = self._spawn(f"cat -- {_qpath(path)}")
            try:
                out, err = proc.communicate(timeout=CMD_TIMEOUT)
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            raise RuntimeError(self._explain((err or b"").decode("utf-8", "replace")))
        return out or b""

    def write_file(self, path: str, content: bytes):
        with self._lock:
            proc = self._spawn(f"cat > {_qpath(path)}", stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL)
            try:
                _out, err = proc.communicate(input=content, timeout=CMD_TIMEOUT)
            finally:
                self._reap(proc)
        if proc.returncode != 0:
            raise RuntimeError(self._explain((err or b"").decode("utf-8", "replace")))
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
            raise RuntimeError(self._explain(err) or f"下载失败 (exit {proc.returncode})")
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
            proc = self._spawn(f"cat > {_qpath(tmp_remote)}",
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
            raise RuntimeError(self._explain(err) or f"上传失败 (exit {proc.returncode})")
        self._run(f"mv -- {_qpath(tmp_remote)} {_qpath(remote_path)}")
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

    def upload_dir_tar(self, local_dir: str, remote_dir: str,
                       total_bytes: int = 0, progress_cb=None):
        """整目录上传快路径：本地打 tar 流 → 主连接 → 远端解包。

        逐文件上传每个文件都要起一条 ssh 通道，几百个小文件能拖上几分钟；
        tar 流没有按文件的往返，通常快 1-2 个数量级。
        """
        tar_bin = shutil.which("tar")
        if not tar_bin:
            raise RuntimeError("本机没有 tar")
        q_remote = _qpath(remote_dir)
        with self._lock:
            src = subprocess.Popen(
                [tar_bin, "-cf", "-", "-C", local_dir, "."],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc = self._spawn(f"mkdir -p {q_remote} && tar -xpf - -C {q_remote}",
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            with self._procs_lock:
                self._procs.add(src)
            sent = 0
            try:
                while True:
                    chunk = src.stdout.read(_CHUNK)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    sent += len(chunk)
                    if progress_cb is not None:
                        progress_cb(min(sent, total_bytes or sent), total_bytes or sent)
                proc.stdin.close()
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                proc.wait(timeout=600)
                src.wait(timeout=30)
            except Exception:
                proc.kill()
                src.kill()
                raise
            finally:
                self._reap(proc)
                with self._procs_lock:
                    self._procs.discard(src)
        if proc.returncode != 0:
            raise RuntimeError(self._explain(err) or f"目录上传失败 (exit {proc.returncode})")
        self.invalidate_cache(remote_dir)
        self.invalidate_cache(self._parent(remote_dir))


__all__ = [
    "ControlMasterSession", "MasterNotRunning", "control_path_for",
    "is_supported", "mfa_login", "master_alive", "master_exit",
    "master_socket_exists", "parse_ls_output", "ssh_target",
]
