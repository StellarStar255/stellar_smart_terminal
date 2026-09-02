"""应用内检查更新与自升级（macOS / Windows / Linux）。

流程：设置 ⚙ 菜单「检查更新」→ GitHub Releases API 查最新版 →
有新版则弹窗（可看说明）→「下载并安装」下载 zip、ditto 解压 →
分阶段换包脚本（等待退出 → 暂存旧包 → 换入新包 → 失败回滚）→ 重启。

要点：
- 运行时版本单一来源：打包的 mac app 读自身 Info.plist 的
  CFBundleShortVersionString；源码运行读 pyproject.toml。不为升级功能
  新增一处需要发版时同步 bump 的版本号。
- 未签名应用的换包不会再触发 Gatekeeper：quarantine 标记只在浏览器等
  声明了 LSFileQuarantineEnabled 的进程下载时添加，应用自身进程下载的
  文件没有该标记；换包脚本再补一次 `xattr -dr` 兜底。
- 来源与完整性三道闸（2026-09）：下载 URL 必须钉在官方仓库的
  releases/download/ 下（is_trusted_asset_url）；API 给了 sha256 digest
  就流式校验（不符即丢弃、不重试）；macOS 解包后与换包脚本里各验一次
  codesign + Team ID（MAC_TEAM_ID），不是本项目证书签的包一律不装。
- 解压用 /usr/bin/ditto 而不是 zipfile：后者会丢符号链接和可执行位，
  解出来的 .app 起不来。
- macOS 打包版一键换包（zip 解包 + 分阶段换装）；Windows 打包版一键
  静默升级（Inno 安装包 /SILENT）；Linux 打包版一键升级（pkexec 系统
  授权 + apt 装 deb，需输一次密码）；源码运行退化为打开发布页。
- 打包版的版本号来源：mac 读 Info.plist，Windows/Linux 读随包携带的
  pyproject.toml（spec datas 已包含），发版仍零额外维护。
"""
import hashlib
import hmac
import http.client
import os
import plistlib
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app_logging import get_logger

logger = get_logger(__name__)

REPO = "StellarStar255/stellar_smart_terminal"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_TIMEOUT = 15
# 发版签名证书的 Team ID（Developer ID Application: … (3QCL9WNFBB)），与
# release.yml 的 NOTARY_TEAM_ID 一致；换包前核对它，别人签的包一律不装
MAC_TEAM_ID = "3QCL9WNFBB"
# 官方 release 产物的固定路径前缀；下载 URL 不在这下面的一律不碰
_ASSET_PATH_PREFIX = f"/{REPO}/releases/download/"


def is_trusted_asset_url(url) -> bool:
    """下载 URL 是否钉在官方仓库的 release 产物路径上。

    URL 来自 GitHub API 的响应，而不是我们自己拼的：企业代理 CA、账号被盗
    后的恶意 release、被劫持的 API 都能改它。这里只认
    https://github.com/<REPO>/releases/download/…，主机名精确匹配（防
    github.com.evil / user@ 伪装），路径规范化后不得逃出前缀（防 ..）。
    只校验起始 URL：github.com 到 objects.githubusercontent.com 的跳转由
    urllib 在 TLS 下完成，不受 API 响应控制。
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    # hostname 已去掉 userinfo 与端口；显式端口一律拒绝（官方 URL 没有）
    if parts.hostname != "github.com" or parts.port is not None:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    path = urllib.parse.unquote(parts.path)
    if "\\" in path:
        return False
    normalized = os.path.normpath(path).replace(os.sep, "/")
    if not normalized.startswith(_ASSET_PATH_PREFIX):
        return False
    # 前缀之后必须还有 <tag>/<file>，且任何一段都不是 .. / .
    rest = normalized[len(_ASSET_PATH_PREFIX):].split("/")
    if len(rest) < 2 or any(seg in ("", ".", "..") for seg in rest):
        return False
    return True


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def parse_sha256_digest(digest) -> str | None:
    """GitHub asset 的 `digest` 字段（'sha256:<hex>'）→ 小写 hex；
    缺失/不是 sha256/格式不对返回 None（旧 release 没有这个字段，
    不能因此把升级堵死）。"""
    if not isinstance(digest, str):
        return None
    algo, sep, hexdigest = digest.strip().partition(":")
    if not sep or algo.lower() != "sha256":
        return None
    hexdigest = hexdigest.lower()
    return hexdigest if _SHA256_HEX.match(hexdigest) else None


def verify_mac_app_signature(app_path: str) -> str | None:
    """解包出的 .app 必须是我们自己签的：codesign 验签通过且 Team ID 匹配。
    返回 None 表示通过，否则返回可展示的失败原因。codesign 不存在或调用
    失败一律按不通过处理——这里装的是可执行代码，宁可不升级。"""
    try:
        r = subprocess.run(
            ['/usr/bin/codesign', '--verify', '--deep', '--strict', app_path],
            capture_output=True, timeout=120)
        if r.returncode != 0:
            return ("codesign verification failed: "
                    + (r.stderr or b'').decode('utf-8', 'replace').strip())
        r = subprocess.run(
            ['/usr/bin/codesign', '-dv', '--verbose=2', app_path],
            capture_output=True, timeout=60)
        # codesign 把详情打到 stderr；这里 stdout/stderr 都看
        info = ((r.stdout or b'') + (r.stderr or b'')).decode('utf-8', 'replace')
        if f"TeamIdentifier={MAC_TEAM_ID}" not in info:
            return f"unexpected signer (TeamIdentifier != {MAC_TEAM_ID})"
        return None
    except Exception as e:
        return f"codesign unavailable: {e}"


def _ssl_context() -> ssl.SSLContext:
    """带可用 CA 的 SSL 上下文。

    macOS 上 python.org/conda 构建的 Python 不读系统钥匙串，默认上下文常因
    找不到 CA 而 CERTIFICATE_VERIFY_FAILED（打包后同样如此）。按序尝试：
    certifi（requests 的传递依赖，打包时一并带上）→ 系统 /etc/ssl/cert.pem
    → 默认。绝不关闭验证——这里下载的是可执行代码。
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        logger.debug("_ssl_context: suppressed exception", exc_info=True)
    for cafile in ('/etc/ssl/cert.pem', '/etc/ssl/certs/ca-certificates.crt'):
        if os.path.exists(cafile):
            try:
                return ssl.create_default_context(cafile=cafile)
            except Exception:
                continue
    return ssl.create_default_context()


# Clash Verge Rev / Clash 系默认混合端口，按本机常见程度排序
_LOCAL_PROXY_PORTS = (7897, 7890)


def _detect_local_proxy() -> str | None:
    """探测本机是否跑着常见端口的代理，命中返回 'http://127.0.0.1:端口'。

    GUI 从桌面启动继承不到终端的 http_proxy；本机代理开着 TUN 又没生效时，
    直连 GitHub 下载大文件常被中途掐断（SSL: UNEXPECTED_EOF_WHILE_READING）。
    只做 TCP connect 探测（127.0.0.1 上失败即刻返回，不产生可感知延迟），
    全部不通返回 None、维持直连。
    """
    import socket
    for port in _LOCAL_PROXY_PORTS:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.3):
                return f'http://127.0.0.1:{port}'
        except OSError:
            continue
    return None


def _urlopen(url: str, timeout: int = _TIMEOUT):
    req = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json',
                      'User-Agent': 'stellar-smart-terminal'})
    ctx = _ssl_context()
    # 环境变量/系统设置里已配代理（getproxies 都能读到）时 urlopen 自带
    # ProxyHandler 会用上；两处都空才探测本机代理端口兜底
    if not urllib.request.getproxies():
        proxy = _detect_local_proxy()
        if proxy:
            logger.info("no proxy configured, falling back to %s", proxy)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({'http': proxy, 'https': proxy}),
                urllib.request.HTTPSHandler(context=ctx))
            return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def parse_version(text: str):
    """'v1.13.0' / '1.13.0' → (1, 13, 0)；解析不出返回 None。"""
    m = re.search(r'(\d+)\.(\d+)\.(\d+)', text or '')
    return tuple(int(g) for g in m.groups()) if m else None


def is_frozen_mac_app() -> bool:
    """是否是打包后的 macOS .app（只有这种形态支持一键换包）。"""
    return bool(getattr(sys, 'frozen', False)) and sys.platform == 'darwin'


def bundle_path() -> Path | None:
    """打包 mac app 时返回 .app 包路径（…/X.app/Contents/MacOS/exe 上溯两级）。"""
    if not is_frozen_mac_app():
        return None
    p = Path(sys.executable).resolve()
    for parent in p.parents:
        if parent.suffix == '.app':
            return parent
    return None


def get_current_version() -> str:
    """当前版本号字符串；读不到返回 ''。"""
    try:
        if is_frozen_mac_app():
            bundle = bundle_path()
            if bundle is not None:
                with open(bundle / 'Contents' / 'Info.plist', 'rb') as f:
                    return plistlib.load(f).get('CFBundleShortVersionString', '')
        # 源码运行 / 其他平台：读仓库根的 pyproject.toml
        pyproject = Path(__file__).resolve().parent / 'pyproject.toml'
        m = re.search(r'^version\s*=\s*"([^"]+)"',
                      pyproject.read_text(encoding='utf-8'), re.M)
        return m.group(1) if m else ''
    except Exception:
        logger.exception("failed to read current version")
        return ''


def can_self_update() -> bool:
    """当前形态是否支持一键自升级（mac/Windows/Linux 的打包版；
    源码运行走 git，不做换包）。"""
    return (bool(getattr(sys, 'frozen', False))
            and sys.platform in ('darwin', 'win32', 'linux'))


def pick_update_asset(assets: list[dict]) -> dict | None:
    """挑当前平台可用于一键安装的 release 产物。

    macOS 用 zip（直接解包换 .app；DMG 需挂载不适合自动化）；
    Windows 用 Inno 安装包（installer.iss 固定 AppId + PrivilegesRequired=
    lowest，`/SILENT` 静默原地升级、默认无 UAC）；
    Linux 用 .deb（pkexec 授权后 apt 原地升级）。其余平台返回 None。
    """
    for a in assets or []:
        name = (a.get('name') or '').lower()
        if sys.platform == 'darwin' and 'macos' in name and name.endswith('.zip'):
            return a
        if sys.platform == 'win32' and name.endswith('-setup.exe'):
            return a
        if sys.platform == 'linux' and name.endswith('.deb'):
            return a
    return None


class UpdateChecker(QThread):
    """后台查最新 release。result 携带 dict：tag / notes / asset(或 None)。"""
    result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def run(self):
        try:
            with _urlopen(LATEST_API) as resp:
                import json
                data = json.load(resp)
            self.result.emit({
                'tag': data.get('tag_name') or '',
                'notes': data.get('body') or '',
                'asset': pick_update_asset(data.get('assets')),
            })
        except Exception as e:
            logger.warning("update check failed: %s", e)
            self.error.emit(str(e))


# 下载中途被掐断时的重试次数（一次抖动不该毁掉整次升级）
_DOWNLOAD_ATTEMPTS = 3


class _IncompleteDownload(Exception):
    """落盘字节数不足权威大小：包被截断，绝不能交给安装器。"""


class _DigestMismatch(Exception):
    """落盘内容的 sha256 与 API 声明不符：被篡改或损坏，不重试、不安装。"""


class UpdateDownloader(QThread):
    """下载 zip 并用 ditto 解出 .app。finished 携带解压出的 .app 路径。"""
    progress = pyqtSignal(int, int)          # done_bytes, total_bytes
    finished_ok = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, url: str, expected_size: int = 0, parent=None,
                 digest: str | None = None):
        super().__init__(parent)
        self._url = url
        # GitHub API 给的 'sha256:<hex>'；有就必须对上，没有只记 warning
        self._sha256 = parse_sha256_digest(digest)
        if digest and self._sha256 is None:
            logger.warning("unsupported asset digest %r, skipping hash check",
                           digest)
        # GitHub release asset 的权威字节数（API 给的，非代理转发的
        # Content-Length）。>0 时用它校验落盘是否完整；0 表示未知，
        # 退回 Content-Length 校验。
        self._expected_size = int(expected_size or 0)
        self._cancelled = False

    def cancel(self):
        """请求中止下载。线程在下一个读块边界收工并清理临时目录，
        之后不再发出任何信号（含已排队但未送达的除外，由调用方守卫）。"""
        self._cancelled = True

    def _fetch_once(self, dl_path: str):
        """下载一次到 dl_path。落盘字节数不足权威大小时抛 _IncompleteDownload；
        被取消则提前返回（由 run 再判 self._cancelled）。每次都以 'wb' 从头
        写，不复用上一趟的残包。"""
        with _urlopen(self._url) as resp, open(dl_path, 'wb') as out:
            # 权威大小优先用 asset size；没有再退回 Content-Length（代理可能
            # 丢掉或伪造它，故不作为首选）
            expected = self._expected_size or int(
                resp.headers.get('Content-Length') or 0)
            done = 0
            hasher = hashlib.sha256() if self._sha256 else None
            while True:
                if self._cancelled:
                    return
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                if hasher is not None:
                    hasher.update(chunk)
                done += len(chunk)
                self.progress.emit(done, expected)
        if self._cancelled:
            return
        # 关键校验：代理常用 connection-close 重帧、丢掉 Content-Length，
        # 中途掐断会表现为「干净 EOF」——read 返回空、不抛异常。没有这一步，
        # 半截 update.exe 会被当成功交给 Inno 安装器，弹「setup files are
        # corrupted」。不足即抛，交由 run 的重试/清理处理，绝不交出残包。
        if expected and done != expected:
            raise _IncompleteDownload(f"got {done} of {expected} bytes")
        # 完整性：字节数对了不代表内容对。API 声明了 sha256 就必须一致，
        # 否则是篡改或损坏——不重试，由 run 的 finally 清掉残包
        if hasher is not None and not hmac.compare_digest(
                hasher.hexdigest(), self._sha256):
            raise _DigestMismatch(
                f"sha256 mismatch: expected {self._sha256[:12]}…, "
                f"got {hasher.hexdigest()[:12]}…")

    def run(self):
        workdir = None
        keep = False  # 只有成功发出 finished_ok 时保留（安装步骤要用里面的文件）
        try:
            # 来源闸：URL 来自 API 响应，不在官方 release 路径下的绝不下载
            if not is_trusted_asset_url(self._url):
                raise ValueError(
                    f"refusing to download update from untrusted URL: {self._url}")
            workdir = tempfile.mkdtemp(prefix='stellar_update_')
            # Windows 安装包 / Linux deb 本身就是安装载体，落盘即用；
            # 只有 macOS 的 zip 需要解包
            lower = self._url.lower()
            if lower.endswith('.exe'):
                fname, is_installer = 'update.exe', True
            elif lower.endswith('.deb'):
                fname, is_installer = 'update.deb', True
            else:
                fname, is_installer = 'update.zip', False
            dl_path = os.path.join(workdir, fname)
            # 下载被中途掐断（截断/连接错误）时重试几次再放弃——一次网络抖动
            # 不该让本次乃至后续升级都失败（见用户反馈：中断一次后再也升不上去）
            last_err = None
            for attempt in range(_DOWNLOAD_ATTEMPTS):
                if self._cancelled:
                    return
                try:
                    self._fetch_once(dl_path)
                    last_err = None
                    break
                except (_IncompleteDownload, OSError,
                        http.client.HTTPException) as e:
                    last_err = e
                    logger.warning(
                        "update download incomplete (attempt %d/%d): %s",
                        attempt + 1, _DOWNLOAD_ATTEMPTS, e)
            if self._cancelled:
                return
            if last_err is not None:
                raise last_err
            if is_installer:
                # Windows：Inno 安装包本身就是安装载体，无需解包
                keep = True
                self.finished_ok.emit(dl_path)
                return
            # macOS zip：zipfile 会丢符号链接/可执行位，必须用 ditto
            extract_dir = os.path.join(workdir, 'extracted')
            subprocess.run(['/usr/bin/ditto', '-x', '-k', dl_path, extract_dir],
                           check=True, capture_output=True)
            apps = [p for p in Path(extract_dir).iterdir()
                    if p.suffix == '.app']
            if not apps:
                # zip 里可能是嵌套一层目录
                apps = list(Path(extract_dir).glob('*/*.app'))
            if not apps:
                raise RuntimeError("no .app found in downloaded zip")
            if self._cancelled:
                return
            # 签名闸：解出来的 .app 必须是本项目证书签的，换包脚本里还会
            # 再验一次（这里先验是为了能弹出明确的错误而不是静默不装）
            reason = verify_mac_app_signature(str(apps[0]))
            if reason:
                raise RuntimeError(f"update package rejected: {reason}")
            keep = True
            self.finished_ok.emit(str(apps[0]))
        except Exception as e:
            if self._cancelled:
                return   # 取消导致的中断不当作错误上报
            logger.exception("update download failed")
            self.error.emit(str(e))
        finally:
            # 取消或失败都清掉临时目录，否则半截的下载包（可上百 MB）会留在 /tmp
            if workdir and not keep:
                import shutil
                shutil.rmtree(workdir, ignore_errors=True)


# 分阶段换包：等待退出 → 验签+核对 Team ID → 去 quarantine → 暂存旧包 →
# 换入新包 → 删暂存；任何一步失败把旧包挪回来，保证不出现「没有可用 app」
# 的中间态。验签放在剥 quarantine 之前：剥掉后 Gatekeeper 不再评估新包，
# 所以这里必须自己把关——不是我们证书签的包一律不装、拉起旧包退出。
_UPDATER_SCRIPT = """#!/bin/bash
PID={pid}
BUNDLE="{bundle}"
NEW_APP="{new_app}"
for _ in $(seq 1 120); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
# 60s 后应用仍未退出（用户取消了关闭等）→ 放弃本次换包，绝不动运行中的包
kill -0 "$PID" 2>/dev/null && exit 1
if ! /usr/bin/codesign --verify --deep --strict "$NEW_APP" 2>/dev/null \\
   || ! /usr/bin/codesign -dv --verbose=2 "$NEW_APP" 2>&1 | grep -q "TeamIdentifier={team_id}"; then
  rm -rf "$NEW_APP"
  open "$BUNDLE"
  exit 1
fi
xattr -dr com.apple.quarantine "$NEW_APP" 2>/dev/null
STAGE="$BUNDLE.updating.$$"
rm -rf "$STAGE"
if mv "$BUNDLE" "$STAGE" && mv "$NEW_APP" "$BUNDLE"; then
  rm -rf "$STAGE"
else
  mv "$STAGE" "$BUNDLE" 2>/dev/null
fi
open "$BUNDLE"
"""


def build_updater_script(pid: int, bundle: str, new_app: str) -> str:
    return _UPDATER_SCRIPT.format(pid=pid, bundle=bundle, new_app=new_app,
                                  team_id=MAC_TEAM_ID)


# Windows：等应用退出 → 静默跑 Inno 安装包原地升级 → 重启应用。
# /SILENT 只显示进度条；PrivilegesRequired=lowest 的用户级安装无 UAC。
# `ping -n 2` 是无控制台 cmd 里的 sleep 1s 替代（timeout 需要可交互控制台）。
# 注意两个实战踩过的坑：
# 1) 不用括号块——cmd 的 %var% 在解析括号块时一次性展开，块内计数器
#    永远读到旧值，超时护卫失效（须 goto 结构或 delayed expansion）。
# 2) 必须由「带隐形控制台」的 cmd 运行（见 _install_and_restart_windows
#    的 CREATE_NO_WINDOW）：无控制台的 cmd 会让 tasklist/find/ping 各自
#    弹出可见控制台窗口，每秒一轮、关不完。
_UPDATER_BAT = """@echo off
set tries=0
:wait
set /a tries+=1
if %tries% GEQ 120 exit /b 1
tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if errorlevel 1 goto install
ping -n 2 127.0.0.1 >NUL
goto wait
:install
"{setup}" /SILENT /NORESTART
start "" "{exe}"
"""


def build_updater_bat(pid: int, setup: str, exe: str) -> str:
    return _UPDATER_BAT.format(pid=pid, setup=setup, exe=exe)


def _install_and_restart_windows(setup_path: str) -> bool:
    bat = build_updater_bat(os.getpid(), setup_path, sys.executable)
    fd, bat_path = tempfile.mkstemp(prefix='stellar_updater_', suffix='.bat')
    with os.fdopen(fd, 'w') as f:
        f.write(bat)
    # 只用 CREATE_NO_WINDOW，绝不能加 DETACHED_PROCESS：detached 的 cmd
    # 没有控制台，循环里的 tasklist/find/ping 会各自弹出可见控制台窗口
    # （Windows 11 下表现为不断冒 Windows Terminal 窗口）。CREATE_NO_WINDOW
    # 给 cmd 一个隐形控制台，子命令全部继承，全程无窗。cmd 是独立进程，
    # 本进程退出后它继续运行，不需要 detach。
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.Popen(['cmd', '/c', bat_path], creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# Linux：等应用退出 → pkexec 弹系统授权（deb 装进 /opt 与 /usr 需要
# root，这是发行版安全模型的要求，GNOME Software 同样要密码）→ apt
# 原地升级 → 重启。pkexec 找不到（无 polkit 的极简环境）则放弃，
# 旧版本保持完好。
_UPDATER_SH_LINUX = """#!/bin/bash
PID={pid}
DEB="{deb}"
EXE="{exe}"
for _ in $(seq 1 120); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
kill -0 "$PID" 2>/dev/null && exit 1
command -v pkexec >/dev/null 2>&1 || exit 1
pkexec apt-get install -y "$DEB" || exit 1
setsid "$EXE" >/dev/null 2>&1 &
"""


def build_updater_sh_linux(pid: int, deb: str, exe: str) -> str:
    return _UPDATER_SH_LINUX.format(pid=pid, deb=deb, exe=exe)


def _install_and_restart_linux(deb_path: str) -> bool:
    script = build_updater_sh_linux(os.getpid(), deb_path, sys.executable)
    fd, sh_path = tempfile.mkstemp(prefix='stellar_updater_', suffix='.sh')
    with os.fdopen(fd, 'w') as f:
        f.write(script)
    os.chmod(sh_path, 0o755)
    subprocess.Popen(['/bin/bash', sh_path], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def install_and_restart(new_app_path: str) -> bool:
    """写换装脚本并以独立进程启动。返回 True 后调用方应立即退出应用。

    macOS 传解包出的 .app 路径（分阶段换包）；Windows 传 Inno 安装包
    路径（静默原地升级）；Linux 传 .deb 路径（pkexec + apt 原地升级）。
    """
    if not can_self_update():
        return False   # 源码运行 / 不支持的平台：绝不触发换装
    if sys.platform == 'win32':
        return _install_and_restart_windows(new_app_path)
    if sys.platform == 'linux':
        return _install_and_restart_linux(new_app_path)
    bundle = bundle_path()
    if bundle is None:
        return False
    script = build_updater_script(os.getpid(), str(bundle), new_app_path)
    fd, script_path = tempfile.mkstemp(prefix='stellar_updater_', suffix='.sh')
    with os.fdopen(fd, 'w') as f:
        f.write(script)
    os.chmod(script_path, 0o755)
    subprocess.Popen(['/bin/bash', script_path],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
