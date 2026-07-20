#!/usr/bin/env python3
"""
智能终端 (Smart Terminal)
一个带GUI的终端工具，用于运行claude code并记录所有交互内容

功能:
- 嵌入式终端，运行任意命令（默认claude）
- 自动记录所有输入输出
- 检测并保存引用的图片/文件
- 支持HTML/Markdown/JSON格式导出
- 历史会话管理

快捷键:
- Ctrl+N: 新建会话
- Ctrl+E: 收起/展开文件区
- Ctrl+H: 查看历史
"""
import sys
import os
from pathlib import Path

# 添加当前目录到path
sys.path.insert(0, str(Path(__file__).parent))

# 设置Qt插件路径 (解决macOS上的cocoa插件问题)
def setup_qt_plugin_path():
    try:
        import PyQt6
        qt_path = Path(PyQt6.__path__[0]) / "Qt6" / "plugins"
        if qt_path.exists():
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(qt_path / "platforms")
            os.environ['QT_PLUGIN_PATH'] = str(qt_path)
    except Exception:
        logger.debug("setup_qt_plugin_path: suppressed exception", exc_info=True)

setup_qt_plugin_path()

from PyQt6.QtWidgets import QApplication, QDialog, QWidget
from PyQt6.QtGui import QFont, QPalette, QColor, QIcon, QPixmap
from PyQt6.QtCore import Qt, QSize, QObject, QEvent, QTimer


class DialogCloseShortcutFilter(QObject):
    """让所有 QDialog 弹窗都能用 Cmd+W（Windows/Linux 为 Ctrl+W）关闭。

    装在 QApplication 上的全局过滤器：KeyPress 只派发给焦点控件一次，
    从接收对象上溯到所在顶层窗口，是 QDialog 才拦截关闭——主窗口的
    Ctrl+W 不受影响（继续走既有的「关标签页」快捷键）。一处规则覆盖
    现有与未来所有对话框（Pasted Images / History / 预设管理 / 各设置
    对话框等），无需逐个注册。
    """

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_W
                and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
            w = obj.window() if isinstance(obj, QWidget) else None
            if isinstance(w, QDialog):
                w.close()
                return True
        return super().eventFilter(obj, event)

from app_logging import setup_logging, get_logger
from main_window import MainWindow
from i18n import t

logger = get_logger(__name__)


def install_global_excepthook():
    """安装全局异常钩子，防止 Qt 回调中未捕获的 Python 异常导致进程闪退。

    背景：PyQt6 在从 C++ 侧调用的槽函数 / 虚函数（如 QTimer 的 timeout、
    eventFilter 等）里遇到未处理的 Python 异常时，默认行为是直接调用
    qFatal() → abort() 把整个进程杀掉（崩溃报告里表现为 SIGABRT，
    调用栈为 pyqt6_err_print → QMessageLogger::fatal → abort）。
    典型触发场景：导航面板有多个窗口时强制关闭其中一个，刷新定时器 /
    eventFilter 在窗口销毁的间隙访问到不稳定对象抛出异常 → 整个程序闪退。

    只要安装了一个“非默认”的 sys.excepthook，PyQt6 就会改为调用我们的
    钩子并让事件循环继续运行，而不会 abort。这里把异常完整记录到 stderr
    和崩溃日志文件，便于后续定位真正的根因。
    """
    import traceback
    from datetime import datetime
    from utils import get_data_dir

    log_path = get_data_dir() / ".smart_terminal_crash.log"
    default_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        # 保留 Ctrl+C 的默认行为（允许正常中断退出）
        if issubclass(exc_type, KeyboardInterrupt):
            default_hook(exc_type, exc_value, exc_tb)
            return
        try:
            tb_text = "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)
            )
        except Exception:
            tb_text = f"{exc_type.__name__}: {exc_value}\n"
        # 输出到 stderr（保持与默认行为一致的可见性）
        try:
            sys.stderr.write(tb_text)
            sys.stderr.flush()
        except Exception:
            logger.debug("_hook: suppressed exception", exc_info=True)
        # 追加写入崩溃日志，方便复盘真正抛异常的位置
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now().isoformat()} =====\n")
                f.write(tb_text)
        except Exception:
            logger.debug("_hook: suppressed exception", exc_info=True)
        # 同步记录到统一日志系统
        try:
            logger.error(
                "Uncaught exception",
                exc_info=(exc_type, exc_value, exc_tb),
            )
        except Exception:
            logger.debug("_hook: suppressed exception", exc_info=True)

    sys.excepthook = _hook


def install_sigint_handler(app: QApplication):
    """让在终端里运行时按 Ctrl+C (SIGINT) 能“两步退出”：
    第一次只提示，短时间内再按第二次才真正保存并退出。

    背景：Qt 的事件循环（app.exec()）运行在 C++ 层，不会回到 Python 解释器，
    所以 Python 的信号处理器平时根本得不到执行——Ctrl+C 只能偶尔在某个
    QTimer 回调里抛出 KeyboardInterrupt，既杀不掉事件循环，又在终端里刷一堆
    traceback（正是用户遇到的现象）。

    实现要点（都很关键，否则会出现“按了没反应 / 提示只剩半行”的现象）：
    1. 用 signal.signal 注册 SIGINT 处理器（注册后 Python 不再抛
       KeyboardInterrupt，而是调用我们的函数）；
    2. 信号处理器里【只做最小动作】——累加一个计数。绝不在信号上下文里写
       sys.stderr：缓冲流是带锁的，在信号上下文里写极易被截断（就是之前“只
       打印出一个 ⚠、后面文字全没了”的根因）；
    3. 真正的提示文本 / 退出逻辑放进一个常驻 QTimer 回调，在正常事件循环上
       下文中执行；这个定时器同时承担“周期性唤醒解释器让信号被及时处理”的
       职责（Qt 事件循环在 C++ 层，不轮询就收不到信号）；
    4. 输出用底层无缓冲的 os.write(fd=2)，绕开 TextIOWrapper 的缓冲与锁，
       保证整行一次性、完整地打到终端。
    """
    import signal
    import time
    from PyQt6.QtCore import QTimer

    # 信号处理器与定时器都在主线程、字节码边界执行，二者天然互斥，无需加锁
    pending = {"count": 0}
    state = {"armed": False, "disarm_at": 0.0}

    def _handler(signum, frame):
        # 只记录“收到一次 Ctrl+C”，其余一概不做
        pending["count"] += 1

    signal.signal(signal.SIGINT, _handler)

    def _notify(text: str):
        # 底层无缓冲写 stderr(fd=2)：和 ^C 回显同处，且不受缓冲/编码包装影响
        try:
            os.write(2, text.encode("utf-8", "replace"))
        except Exception:
            logger.debug("_notify: suppressed exception", exc_info=True)

    def _graceful_quit():
        # 优先走带“保存会话+配置”的强制关闭；任何异常都不能阻断退出
        try:
            from main_window import MainWindow
            for w in list(app.topLevelWidgets()):
                if isinstance(w, MainWindow):
                    try:
                        w.force_close_with_save()
                    except Exception:
                        logger.debug("_graceful_quit: suppressed exception", exc_info=True)
        except Exception:
            logger.debug("_graceful_quit: suppressed exception", exc_info=True)
        # 兜底：无论保存/关闭是否成功，都确保事件循环退出
        QTimer.singleShot(300, app.quit)

    def _on_sigint():
        if state["armed"]:
            _notify("\n\033[1;32m正在退出 Smart Terminal…\033[0m\n")
            _graceful_quit()
        else:
            state["armed"] = True
            state["disarm_at"] = time.monotonic() + 4.0
            # 醒目的黄色高亮提醒（终端不支持 ANSI 颜色时也能读出文字）
            _notify(
                "\n\033[1;33m[!] 再按一次 Ctrl+C 退出程序 "
                "(Press Ctrl+C again to quit)\033[0m\n"
            )

    def _tick():
        # 1) 处理累计到的 Ctrl+C（多次合并为一次处理即可）
        if pending["count"] > 0:
            pending["count"] = 0
            _on_sigint()
        # 2) 超过时间窗仍未再次按下 → 复位，避免之后误触退出
        if state["armed"] and time.monotonic() >= state["disarm_at"]:
            state["armed"] = False

    # 常驻定时器：既负责唤醒解释器及时收信号，又在正常上下文里处理提示/退出。
    # 用属性持有引用，防止被垃圾回收。
    app._sigint_timer = QTimer()
    app._sigint_timer.timeout.connect(_tick)
    app._sigint_timer.start(120)


def setup_app_style(app: QApplication):
    """设置应用程序样式"""
    # 深色主题
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#1a1a2e"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#eaeaea"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#16213e"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#1a1a2e"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#2d2d44"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#eaeaea"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#eaeaea"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#2d2d44"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#eaeaea"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#667eea"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#667eea"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))

    app.setPalette(palette)

    # 全局勾选框样式：统一深色方块，勾选态 accent 底 + 白色对勾。
    # 此前各处 QSS 只把底色涂成 accent、没有对勾图形，勾选后是一坨实心
    # 色块。放在 app 级 stylesheet 的原因：各 widget 级 QSS 没设 image
    # 属性，本条的对勾按 Qt 层叠透下去，一处规则覆盖全应用（含对话框）。
    # 必须在任何 MainWindow 创建之前设置——GUI 字号缩放会以此刻的
    # app stylesheet 为基准快照（_apply_application_font）。
    from utils import checkbox_checkmark_url
    check_url = checkbox_checkmark_url()
    if check_url:
        app.setStyleSheet(app.styleSheet() + f"""
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 2px solid #3d3d5c; border-radius: 3px;
    background-color: #16213e;
}}
QCheckBox::indicator:hover {{ border-color: #667eea; }}
QCheckBox::indicator:checked {{
    border-color: #667eea; background-color: #667eea;
    image: url({check_url});
}}
""")


# .deb 打包安装自带的 desktop 条目文件名（见 build.sh 的 $PKG）。
# 源码运行时生成同名文件，靠 XDG「~/.local 覆盖 /usr/share」collapse 成
# 一个图标，而不是多出一个 Name 不同的重复项。
_LINUX_DESKTOP_ID = "stellar-smart-terminal"


def _ensure_linux_desktop_file(icon_path: Path):
    """在 Linux 上维护 .desktop 桌面入口，避免出现重复图标。

    - 打包(frozen)安装：.deb 已装了 /usr/share/applications 下的条目，
      再生成一个就会多出一个图标 → 直接跳过。
    - 源码运行：用与 .deb 一致的文件名/Name 生成到 ~/.local，二者同名，
      XDG 覆盖规则下只显示一个。
    - 顺手删掉历史遗留的旧文件名 smart-terminal.desktop（Name=Smart
      Terminal 的那个重复项，删了会被旧版重新生成，此处一并清掉）。
    """
    desktop_dir = Path.home() / ".local" / "share" / "applications"
    try:
        (desktop_dir / "smart-terminal.desktop").unlink()
    except OSError:
        pass  # 不存在或删不掉都无所谓

    # 打包安装：desktop 条目由 .deb 提供，别再生成第二个
    if getattr(sys, 'frozen', False):
        return

    desktop_file = desktop_dir / f"{_LINUX_DESKTOP_ID}.desktop"
    icon_abs = str(icon_path.resolve()) if icon_path.exists() else ""
    content = f"""[Desktop Entry]
Type=Application
Name=Stellar Smart Terminal
Comment=Smart terminal with file explorer, Git GUI, and LLM proxy
Exec="{sys.executable}" "{Path(__file__).resolve()}"
Icon={icon_abs}
Terminal=false
Categories=Development;Utility;
StartupWMClass={_LINUX_DESKTOP_ID}
"""
    try:
        # 只在内容变化或文件不存在时写入
        if desktop_file.exists():
            if desktop_file.read_text(encoding='utf-8') == content:
                return
        desktop_dir.mkdir(parents=True, exist_ok=True)
        desktop_file.write_text(content, encoding='utf-8')
    except OSError:
        pass  # 静默失败，不影响应用启动


def parse_cli_args(argv):
    """解析自有命令行参数，返回 (working_dir 或 None, 剩余 argv 给 Qt)。

    --working-dir PATH 由系统右键菜单集成（shell_integration）传入；
    路径无效时忽略（比如目录在点菜单和启动之间被删了），按正常启动走。
    """
    working_dir = None
    rest = [argv[0]] if argv else []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == '--working-dir':
            if i + 1 < len(argv):
                candidate = argv[i + 1]
                if os.path.isdir(candidate):
                    working_dir = os.path.abspath(candidate)
                i += 2
            else:
                i += 1  # 悬空参数：吞掉，不留给 Qt 报错
            continue
        if arg.startswith('--working-dir='):
            candidate = arg.split('=', 1)[1]
            if os.path.isdir(candidate):
                working_dir = os.path.abspath(candidate)
            i += 1
            continue
        rest.append(arg)
        i += 1
    return working_dir, rest


def _single_instance_server_name() -> str:
    """单实例 IPC 的本地 socket 名。

    按用户 + 数据目录隔离：不同用户、或显式换了 STELLAR_DATA_DIR 的实例
    互不并入（后者是有意分身，测试也走独立数据目录）。
    """
    try:
        uid = str(os.getuid())
    except AttributeError:
        import getpass
        uid = getpass.getuser()  # Windows 无 getuid
    try:
        from utils import get_data_dir
        import hashlib
        tag = hashlib.md5(str(get_data_dir()).encode('utf-8')).hexdigest()[:8]
    except Exception:
        tag = "default"
    return f"stellar-smart-terminal-{uid}-{tag}"


class SmartTerminalApplication(QApplication):
    """单实例协调 + 打开目录请求分发。

    - macOS：Finder 快速操作 / 拖目录到 Dock → FileOpen 事件 → 现有窗口开
      新标签（macOS 本身单实例，不会起第二个进程）。
    - Linux / Windows：系统右键菜单是 subprocess 拉起新进程，没有 FileOpen
      语义。用 QLocalServer/QLocalSocket 做单实例：第二个进程把请求转发给
      主实例并退出，从而并入现有窗口与导航器，而不是起一个孤立实例。
    """

    def __init__(self, argv):
        super().__init__(argv)
        self._file_open_window = None   # 主窗口就绪后由 main() 注入
        self._pending_open_dirs = []    # 窗口就绪前收到的事件先排队
        self._local_server = None

    # ---------- 单实例 IPC ----------

    def try_forward_to_primary(self, working_dir) -> bool:
        """已有主实例在跑 → 把请求（工作目录，空=普通启动）发过去，返回 True；
        没有主实例（或 QtNetwork 不可用）→ 返回 False，本进程应成为主实例。"""
        try:
            from PyQt6.QtNetwork import QLocalSocket
        except ImportError:
            return False
        sock = QLocalSocket()
        sock.connectToServer(_single_instance_server_name())
        if not sock.waitForConnected(300):
            return False
        payload = ((working_dir or "") + "\n").encode("utf-8")
        sock.write(payload)
        sock.waitForBytesWritten(1000)
        sock.flush()
        sock.waitForDisconnected(300)
        return True

    def start_single_instance_server(self) -> bool:
        """成为主实例：监听本地 socket，接收后续进程转发的打开请求。"""
        try:
            from PyQt6.QtNetwork import QLocalServer
        except ImportError:
            return False
        name = _single_instance_server_name()
        # 清理崩溃残留的 socket 文件（此前已确认无主实例连得上，删除安全）
        QLocalServer.removeServer(name)
        self._local_server = QLocalServer(self)
        self._local_server.newConnection.connect(self._on_ipc_connection)
        if not self._local_server.listen(name):
            self._local_server = None
            return False
        return True

    def _on_ipc_connection(self):
        conn = self._local_server.nextPendingConnection()
        if conn is None:
            return
        if not conn.waitForReadyRead(1000):
            conn.disconnectFromServer()
            return
        data = bytes(conn.readAll()).decode("utf-8", "ignore").strip()
        conn.disconnectFromServer()
        if data and os.path.isdir(data):
            self._open_dir(data)
        else:
            self._focus_existing_window()

    def _focus_existing_window(self):
        for widget in self.topLevelWidgets():
            if isinstance(widget, MainWindow):
                widget.raise_()
                widget.activateWindow()
                return

    def set_file_open_window(self, window):
        self._file_open_window = window
        pending, self._pending_open_dirs = self._pending_open_dirs, []
        for path in pending:
            self._open_dir(path)

    def _open_dir(self, path: str):
        win = self._file_open_window
        if win is not None:
            try:
                win.open_directory_tab(path)
                return
            except RuntimeError:
                # 记录的窗口已销毁，向下扫描其它存活窗口
                self._file_open_window = None
        for widget in self.topLevelWidgets():
            if isinstance(widget, MainWindow):
                try:
                    widget.open_directory_tab(path)
                    self._file_open_window = widget
                    return
                except RuntimeError:
                    continue
        self._pending_open_dirs.append(path)

    def event(self, e):
        if e.type() == QEvent.Type.FileOpen:
            path = e.file()
            if path:
                # 传进来的是文件就打开它所在的目录
                if not os.path.isdir(path):
                    path = os.path.dirname(path)
                if os.path.isdir(path):
                    self._open_dir(path)
            return True
        return super().event(e)


def main():
    """主函数"""
    # 尽早初始化日志系统，保证后续所有模块的 logger 输出有处可去
    setup_logging()

    # 尽早安装全局异常钩子：把 Qt 回调中的未捕获异常从“闪退(abort)”降级为
    # “记录日志并继续运行”，必须在创建 QApplication / 进入事件循环之前完成。
    install_global_excepthook()

    # Windows: 设置 AppUserModelID 以便任务栏显示自定义图标而非 Python 图标
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SmartTerminal.SmartTerminal")

    # 自有参数先摘出来（--working-dir 来自系统右键菜单集成）
    cli_working_dir, qt_argv = parse_cli_args(sys.argv)

    # 创建应用
    app = SmartTerminalApplication(qt_argv)

    # 单实例：已有实例在跑就把打开请求转发过去并退出，避免起一个孤立的
    # 第二进程（系统右键菜单在 Linux/Windows 是 subprocess 拉起，没有 macOS
    # 的 FileOpen 单实例语义，必须自己做 IPC 才能并入现有窗口/导航器）。
    if app.try_forward_to_primary(cli_working_dir):
        return

    app.setApplicationName(t("app.name"))
    app.setApplicationDisplayName("Smart Terminal")
    app.setOrganizationName("SmartTerminal")
    app.setOrganizationDomain("smart-terminal")

    # macOS: 确保支持多窗口在 Mission Control 中正确显示
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)

    # 设置应用程序图标路径（macOS 用带留白的 Dock 母版，见 utils.app_icon_path）
    from utils import app_icon_path
    icon_path = app_icon_path()

    # Linux: 自动创建 .desktop 文件，用于dock/任务栏图标匹配
    if sys.platform == "linux":
        _ensure_linux_desktop_file(icon_path)
    app.setDesktopFileName(_LINUX_DESKTOP_ID)

    # 设置应用程序图标 (提供多个尺寸以适配 Windows 任务栏/标题栏)
    if icon_path.exists():
        icon = QIcon()
        original = QPixmap(str(icon_path))
        for size in [16, 24, 32, 48, 64, 128, 256]:
            icon.addPixmap(original.scaled(
                QSize(size, size),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
        app.setWindowIcon(icon)

    # 设置样式
    setup_app_style(app)

    # 全局字体 - 根据平台使用系统默认字体
    if sys.platform == "win32":
        font = QFont("Segoe UI", 10)
        font.setFamilies(["Segoe UI", "Segoe UI Emoji", "Segoe UI Symbol"])
    elif sys.platform == "darwin":
        font = QFont(".AppleSystemUIFont", 13)
    else:
        # Linux: 提供完整的字体回退链，避免因缺少字体而使用 CJK 变体导致数字显示异常
        font = QFont("Noto Sans", 11)
        font.setFamilies([
            "Noto Sans",          # Google Noto 标准无衬线
            "Ubuntu",             # Ubuntu 默认
            "DejaVu Sans",        # 几乎所有 Linux 发行版都有
            "Liberation Sans",    # Fedora/RHEL 常见
            "Cantarell",          # GNOME 默认
            "Droid Sans",         # Android/早期 Linux
            "Noto Color Emoji",   # Emoji 支持
        ])
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)

    # 成为主实例：监听本地 socket，接收后续进程转发的打开请求
    app.start_single_instance_server()

    # 创建主窗口（--working-dir 指定时首个标签在该目录直接起会话）
    initial_tab_data = {'cwd': cli_working_dir} if cli_working_dir else None
    window = MainWindow(initial_tab_data=initial_tab_data)
    window.show()
    app.set_file_open_window(window)
    if cli_working_dir:
        # 等窗口布局稳定后在首个标签起会话（与目录历史快速启动同款时序）
        QTimer.singleShot(150, lambda: window.launch_initial_session(cli_working_dir))

    # 升级重启后恢复升级前打开的窗口（仅当上次「下载并安装」时勾选了
    # 「升级后自动重新打开当前这些窗口」，快照一次性消费，无快照零开销）
    MainWindow.restore_windows_after_update(window)

    # 安装 Ctrl+C (SIGINT) 处理器：在终端里按两次 Ctrl+C 可保存并退出
    install_sigint_handler(app)

    # 所有 QDialog 弹窗支持 Cmd+W / Ctrl+W 关闭（保持引用防 GC）
    app._dialog_close_filter = DialogCloseShortcutFilter(app)
    app.installEventFilter(app._dialog_close_filter)

    # 运行
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
