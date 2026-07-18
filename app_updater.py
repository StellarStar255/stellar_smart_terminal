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
- 解压用 /usr/bin/ditto 而不是 zipfile：后者会丢符号链接和可执行位，
  解出来的 .app 起不来。
- macOS 打包版一键换包（zip 解包 + 分阶段换装）；Windows 打包版一键
  静默升级（Inno 安装包 /SILENT）；Linux 打包版一键升级（pkexec 系统
  授权 + apt 装 deb，需输一次密码）；源码运行退化为打开发布页。
- 打包版的版本号来源：mac 读 Info.plist，Windows/Linux 读随包携带的
  pyproject.toml（spec datas 已包含），发版仍零额外维护。
"""
import os
import plistlib
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from app_logging import get_logger

logger = get_logger(__name__)

REPO = "StellarStar255/stellar_smart_terminal"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_TIMEOUT = 15


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
        pass
    for cafile in ('/etc/ssl/cert.pem', '/etc/ssl/certs/ca-certificates.crt'):
        if os.path.exists(cafile):
            try:
                return ssl.create_default_context(cafile=cafile)
            except Exception:
                continue
    return ssl.create_default_context()


def _urlopen(url: str, timeout: int = _TIMEOUT):
    req = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json',
                      'User-Agent': 'stellar-smart-terminal'})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


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


class UpdateDownloader(QThread):
    """下载 zip 并用 ditto 解出 .app。finished 携带解压出的 .app 路径。"""
    progress = pyqtSignal(int, int)          # done_bytes, total_bytes
    finished_ok = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._cancelled = False

    def cancel(self):
        """请求中止下载。线程在下一个读块边界收工并清理临时目录，
        之后不再发出任何信号（含已排队但未送达的除外，由调用方守卫）。"""
        self._cancelled = True

    def run(self):
        workdir = None
        try:
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
            with _urlopen(self._url) as resp, \
                    open(dl_path, 'wb') as out:
                total = int(resp.headers.get('Content-Length') or 0)
                done = 0
                while True:
                    if self._cancelled:
                        return
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    self.progress.emit(done, total)
            if self._cancelled:
                return
            if is_installer:
                # Windows：Inno 安装包本身就是安装载体，无需解包
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
            self.finished_ok.emit(str(apps[0]))
        except Exception as e:
            if self._cancelled:
                return   # 取消导致的中断不当作错误上报
            logger.exception("update download failed")
            self.error.emit(str(e))
        finally:
            if self._cancelled and workdir:
                import shutil
                shutil.rmtree(workdir, ignore_errors=True)


# 分阶段换包：等待退出 → 去 quarantine → 暂存旧包 → 换入新包 → 删暂存；
# 任何一步失败把旧包挪回来，保证不出现「没有可用 app」的中间态
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
    return _UPDATER_SCRIPT.format(pid=pid, bundle=bundle, new_app=new_app)


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
