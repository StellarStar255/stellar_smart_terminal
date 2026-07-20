"""系统右键菜单集成：在文件管理器里右键目录 → 在 Stellar 终端中打开。

三平台机制各不相同，全部用户级安装（不需要管理员/签名）：
- macOS: ~/Library/Services/ 下的快速操作 .workflow（Finder 右键 → 快速操作）。
  workflow 用 `open -a <app>` 拉起（已运行实例会收到 FileOpen 事件开新标签）；
  源码运行没有 .app 可 open，退化为直接拉起 python 进程。
- Windows: HKCU\\Software\\Classes\\Directory\\shell 注册表（含 Background\\shell
  的空白处右键），command 带 --working-dir "%V"。
- Linux: ~/.local/share/nautilus/scripts/ 可执行脚本（GNOME/Nautilus；
  其它文件管理器不覆盖）。

应用侧的接收端在 app.py：--working-dir 参数与 macOS FileOpen 事件。
"""
import plistlib
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from app_logging import get_logger
from i18n import t

logger = get_logger(__name__)

_WORKFLOW_NAME = "Open in Stellar Terminal.workflow"
_WIN_KEY_NAME = "StellarSmartTerminal"
_NAUTILUS_SCRIPT_NAME = "Open in Stellar Terminal"


# ---------- 启动命令解析（打包/源码两种形态） ----------

def _repo_dir() -> Path:
    return Path(__file__).resolve().parent


def _macos_app_bundle() -> Path | None:
    """打包形态下返回 .app 根目录，否则 None"""
    if not getattr(sys, 'frozen', False):
        return None
    # sys.executable = .../Stellar Smart Terminal.app/Contents/MacOS/<exe>
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == '.app':
            return parent
    return None


def _launch_argv() -> list:
    """直接拉起应用进程的 argv（不含 --working-dir 参数部分）"""
    if getattr(sys, 'frozen', False):
        return [sys.executable]
    return [sys.executable, str(_repo_dir() / 'app.py')]


# ---------- macOS ----------

def _macos_services_dir() -> Path:
    return Path.home() / "Library" / "Services"


def _macos_workflow_path() -> Path:
    return _macos_services_dir() / _WORKFLOW_NAME


def _macos_shell_script() -> str:
    """workflow 里执行的 zsh 片段。$@ 是 Finder 选中的条目列表。

    接受 public.item（文件+文件夹）：选中文件时打开其所在目录，
    等价于在空白处开当前目录（服务机制基于选中项，空白处右键
    本身不会出现快速操作，只能靠这个途径覆盖）。
    """
    bundle = _macos_app_bundle()
    if bundle is not None:
        open_target = shlex.quote(str(bundle))
        return (f'for f in "$@"; do\n'
                f'  [ -d "$f" ] || f="$(dirname "$f")"\n'
                f'  open -a {open_target} "$f"\n'
                f'done')
    argv = ' '.join(shlex.quote(a) for a in _launch_argv())
    return (f'for f in "$@"; do\n'
            f'  [ -d "$f" ] || f="$(dirname "$f")"\n'
            f'  nohup {argv} --working-dir "$f" >/dev/null 2>&1 &\n'
            f'done')


def _macos_install() -> tuple:
    wf = _macos_workflow_path()
    contents = wf / "Contents"
    try:
        contents.mkdir(parents=True, exist_ok=True)
        info = {
            "NSServices": [{
                "NSMenuItem": {"default": t("shell_menu.entry_label")},
                "NSMessage": "runWorkflowAsService",
                "NSRequiredContext": {
                    "NSApplicationIdentifier": "com.apple.finder"},
                # public.item = 文件+文件夹：右键文件时打开其所在目录
                "NSSendFileTypes": ["public.item"],
            }],
        }
        with open(contents / "Info.plist", 'wb') as f:
            plistlib.dump(info, f)

        in_uuid, out_uuid, act_uuid = (str(uuid.uuid4()).upper()
                                       for _ in range(3))
        doc = {
            "AMApplicationBuild": "523",
            "AMApplicationVersion": "2.10",
            "AMDocumentVersion": "2",
            "actions": [{
                "action": {
                    "AMAccepts": {"Container": "List", "Optional": True,
                                  "Types": ["com.apple.cocoa.string"]},
                    "AMActionVersion": "2.0.3",
                    "AMApplication": ["Automator"],
                    "AMParameterProperties": {},
                    "AMProvides": {"Container": "List",
                                   "Types": ["com.apple.cocoa.string"]},
                    "ActionBundlePath":
                        "/System/Library/Automator/Run Shell Script.action",
                    "ActionName": "Run Shell Script",
                    "ActionParameters": {
                        "COMMAND_STRING": _macos_shell_script(),
                        "CheckedForUserDefaultShell": True,
                        "inputMethod": 1,  # 1 = 作为参数传入（$@）
                        "shell": "/bin/zsh",
                        "source": "",
                    },
                    "BundleIdentifier": "com.apple.RunShellScript",
                    "CFBundleVersion": "2.0.3",
                    "CanShowSelectedItemsWhenRun": False,
                    "CanShowWhenRun": True,
                    "Class Name": "RunShellScriptAction",
                    "InputUUID": in_uuid,
                    "Keywords": [],
                    "OutputUUID": out_uuid,
                    "UUID": act_uuid,
                    "ShowWhenRun": False,
                },
            }],
            "connectors": {},
            "workflowMetaData": {
                "serviceInputTypeIdentifier":
                    "com.apple.Automator.fileSystemObject.folder",
                "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
                "serviceProcessesInput": 0,
                "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
            },
        }
        with open(contents / "document.wflow", 'wb') as f:
            plistlib.dump(doc, f)
        _macos_refresh_services()
        return True, ""
    except OSError as e:
        logger.warning(f"install quick action failed: {e}")
        return False, str(e)


def _macos_refresh_services():
    """让 Finder 尽快看到新服务（失败无妨，系统迟早会自己扫）"""
    try:
        subprocess.run(
            ["/System/Library/CoreServices/pbs", "-update"],
            capture_output=True, timeout=10)
    except Exception as e:
        logger.debug(f"pbs -update failed: {e}")


def _macos_uninstall() -> tuple:
    try:
        wf = _macos_workflow_path()
        if wf.exists():
            shutil.rmtree(wf)
        _macos_refresh_services()
        return True, ""
    except OSError as e:
        logger.warning(f"uninstall quick action failed: {e}")
        return False, str(e)


def _macos_installed() -> bool:
    return (_macos_workflow_path() / "Contents" / "document.wflow").exists()


# ---------- Windows ----------

def _win_command() -> str:
    argv = _launch_argv()
    if not getattr(sys, 'frozen', False):
        # 源码运行优先 pythonw.exe，避免闪黑色控制台窗
        pyw = Path(argv[0]).with_name('pythonw.exe')
        if pyw.exists():
            argv[0] = str(pyw)
    quoted = ' '.join(f'"{a}"' for a in argv)
    return f'{quoted} --working-dir "%V"'


def _win_key_paths() -> list:
    # 右键文件夹本身 + 文件夹内空白处
    return [rf"Software\Classes\Directory\shell\{_WIN_KEY_NAME}",
            rf"Software\Classes\Directory\Background\shell\{_WIN_KEY_NAME}"]


def _win_install() -> tuple:
    try:
        import winreg
        label = t("shell_menu.entry_label")
        command = _win_command()
        for base in _win_key_paths():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, label)
                if getattr(sys, 'frozen', False):
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ,
                                      f'"{sys.executable}",0')
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                                  base + r"\command") as key:
                winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
        return True, ""
    except OSError as e:
        logger.warning(f"install registry menu failed: {e}")
        return False, str(e)


def _win_uninstall() -> tuple:
    try:
        import winreg
        for base in _win_key_paths():
            for sub in (base + r"\command", base):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                except FileNotFoundError:
                    pass
        return True, ""
    except OSError as e:
        logger.warning(f"uninstall registry menu failed: {e}")
        return False, str(e)


def _win_installed() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            _win_key_paths()[0] + r"\command"):
            return True
    except OSError:
        return False


# ---------- Linux (Nautilus) ----------

def _linux_script_path() -> Path:
    return (Path.home() / ".local" / "share" / "nautilus" / "scripts"
            / _NAUTILUS_SCRIPT_NAME)


def _linux_script_body() -> str:
    argv = ' '.join(shlex.quote(a) for a in _launch_argv())
    return f'''#!/bin/bash
# Stellar Smart Terminal — 由应用设置里的「系统右键菜单」开关生成/移除
IFS=$'\\n'
paths=($NAUTILUS_SCRIPT_SELECTED_FILE_PATHS)
if [ ${{#paths[@]}} -eq 0 ]; then paths=("$PWD"); fi
for p in "${{paths[@]}}"; do
  [ -d "$p" ] || p=$(dirname "$p")
  nohup {argv} --working-dir "$p" >/dev/null 2>&1 &
done
'''


def _linux_install() -> tuple:
    try:
        path = _linux_script_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_linux_script_body(), encoding='utf-8')
        path.chmod(0o755)
        return True, ""
    except OSError as e:
        logger.warning(f"install nautilus script failed: {e}")
        return False, str(e)


def _linux_uninstall() -> tuple:
    try:
        _linux_script_path().unlink(missing_ok=True)
        return True, ""
    except OSError as e:
        logger.warning(f"uninstall nautilus script failed: {e}")
        return False, str(e)


def _linux_installed() -> bool:
    return _linux_script_path().exists()


# ---------- 公共入口 ----------

def is_supported() -> bool:
    return sys.platform in ("darwin", "win32", "linux")


def is_installed() -> bool:
    if sys.platform == "darwin":
        return _macos_installed()
    if sys.platform == "win32":
        return _win_installed()
    if sys.platform == "linux":
        return _linux_installed()
    return False


def install() -> tuple:
    """返回 (ok, 错误信息)。重复安装等价于覆盖更新（命令路径随形态刷新）。"""
    if sys.platform == "darwin":
        return _macos_install()
    if sys.platform == "win32":
        return _win_install()
    if sys.platform == "linux":
        return _linux_install()
    return False, "unsupported platform"


def uninstall() -> tuple:
    if sys.platform == "darwin":
        return _macos_uninstall()
    if sys.platform == "win32":
        return _win_uninstall()
    if sys.platform == "linux":
        return _linux_uninstall()
    return False, "unsupported platform"
