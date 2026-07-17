"""应用内检查更新与 macOS 自升级。

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
- 仅 macOS 打包版提供一键安装；其余平台/源码运行退化为打开发布页。
"""
import os
import plistlib
import re
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


def pick_mac_asset(assets: list[dict]) -> dict | None:
    """从 release assets 里挑 macOS 的 zip 包（DMG 换包需挂载，zip 直接解）。"""
    for a in assets or []:
        name = (a.get('name') or '').lower()
        if 'macos' in name and name.endswith('.zip'):
            return a
    return None


class UpdateChecker(QThread):
    """后台查最新 release。result 携带 dict：tag / notes / asset(或 None)。"""
    result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def run(self):
        try:
            req = urllib.request.Request(
                LATEST_API, headers={'Accept': 'application/vnd.github+json',
                                     'User-Agent': 'stellar-smart-terminal'})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                import json
                data = json.load(resp)
            self.result.emit({
                'tag': data.get('tag_name') or '',
                'notes': data.get('body') or '',
                'asset': pick_mac_asset(data.get('assets')),
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

    def run(self):
        try:
            workdir = tempfile.mkdtemp(prefix='stellar_update_')
            zip_path = os.path.join(workdir, 'update.zip')
            req = urllib.request.Request(
                self._url, headers={'User-Agent': 'stellar-smart-terminal'})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp, \
                    open(zip_path, 'wb') as out:
                total = int(resp.headers.get('Content-Length') or 0)
                done = 0
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    self.progress.emit(done, total)
            # zipfile 会丢符号链接/可执行位，必须用 ditto
            extract_dir = os.path.join(workdir, 'extracted')
            subprocess.run(['/usr/bin/ditto', '-x', '-k', zip_path, extract_dir],
                           check=True, capture_output=True)
            apps = [p for p in Path(extract_dir).iterdir()
                    if p.suffix == '.app']
            if not apps:
                # zip 里可能是嵌套一层目录
                apps = list(Path(extract_dir).glob('*/*.app'))
            if not apps:
                raise RuntimeError("no .app found in downloaded zip")
            self.finished_ok.emit(str(apps[0]))
        except Exception as e:
            logger.exception("update download failed")
            self.error.emit(str(e))


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


def install_and_restart(new_app_path: str) -> bool:
    """写换包脚本并以独立会话启动。返回 True 后调用方应立即退出应用。"""
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
