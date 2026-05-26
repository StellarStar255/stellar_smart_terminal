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
- Ctrl+E: 快速导出
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
        pass

setup_qt_plugin_path()

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QPalette, QColor, QIcon, QPixmap
from PyQt6.QtCore import Qt, QSize

from main_window import MainWindow
from i18n import t


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

    log_path = Path(__file__).parent / ".smart_terminal_crash.log"
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
            pass
        # 追加写入崩溃日志，方便复盘真正抛异常的位置
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now().isoformat()} =====\n")
                f.write(tb_text)
        except Exception:
            pass

    sys.excepthook = _hook


def install_sigint_handler(app: QApplication):
    """让在终端里运行时按 Ctrl+C (SIGINT) 能“两步退出”：
    第一次只提示，短时间内再按第二次才真正保存并退出。

    背景：Qt 的事件循环（app.exec()）运行在 C++ 层，不会回到 Python 解释器，
    所以 Python 的信号处理器平时根本得不到执行——Ctrl+C 只能偶尔在某个
    QTimer 回调里抛出 KeyboardInterrupt，既杀不掉事件循环，又在终端里刷一堆
    traceback（正是用户遇到的现象）。

    解决办法两点：
    1. 用 signal.signal 注册自己的 SIGINT 处理器（注册后 Python 不再抛
       KeyboardInterrupt，而是调用我们的函数）；
    2. 起一个空转的 QTimer 定期把控制权交回解释器，确保信号能在 ~200ms 内
       被处理。
    """
    import signal
    from PyQt6.QtCore import QTimer

    state = {"armed": False}

    def _disarm():
        state["armed"] = False

    def _graceful_quit():
        # 优先走带“保存会话+配置”的强制关闭；任何异常都不能阻断退出
        try:
            from main_window import MainWindow
            for w in list(app.topLevelWidgets()):
                if isinstance(w, MainWindow):
                    try:
                        w.force_close_with_save()
                    except Exception:
                        pass
        except Exception:
            pass
        # 兜底：无论保存/关闭是否成功，都确保事件循环退出
        QTimer.singleShot(300, app.quit)

    def _notify(text: str):
        # 写到 stderr：和 Ctrl+C 回显的 ^C 在同一处，默认无缓冲，确保立即可见
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            pass

    def _handler(signum, frame):
        if state["armed"]:
            _notify("\n\033[1;32m正在退出 Smart Terminal…\033[0m\n")
            _graceful_quit()
        else:
            state["armed"] = True
            # 醒目的黄色高亮提醒（终端不支持 ANSI 颜色时也能读出文字）
            _notify(
                "\n\033[1;33m⚠️  再按一次 Ctrl+C 退出程序 "
                "(Press Ctrl+C again to quit)\033[0m\n"
            )
            # 一段时间内没有再次按下则重置，避免之后误触退出
            QTimer.singleShot(4000, _disarm)

    signal.signal(signal.SIGINT, _handler)

    # 空转定时器：周期性唤醒解释器，让 SIGINT 处理器能被及时调用。
    # 用属性持有引用，防止被垃圾回收。
    app._sigint_keepalive_timer = QTimer()
    app._sigint_keepalive_timer.timeout.connect(lambda: None)
    app._sigint_keepalive_timer.start(200)


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


def _ensure_linux_desktop_file(icon_path: Path):
    """在 Linux 上自动创建/更新 .desktop 文件，使任务栏/dock 能显示应用图标"""
    desktop_dir = Path.home() / ".local" / "share" / "applications"
    desktop_file = desktop_dir / "smart-terminal.desktop"
    icon_abs = str(icon_path.resolve()) if icon_path.exists() else ""

    content = f"""[Desktop Entry]
Name=Smart Terminal
Comment=Smart Terminal with AI Integration
Exec="{sys.executable}" "{Path(__file__).resolve()}"
Icon={icon_abs}
Terminal=false
Type=Application
Categories=Development;Utility;
StartupWMClass=smart-terminal
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


def main():
    """主函数"""
    # 尽早安装全局异常钩子：把 Qt 回调中的未捕获异常从“闪退(abort)”降级为
    # “记录日志并继续运行”，必须在创建 QApplication / 进入事件循环之前完成。
    install_global_excepthook()

    # Windows: 设置 AppUserModelID 以便任务栏显示自定义图标而非 Python 图标
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SmartTerminal.SmartTerminal")

    # 创建应用
    app = QApplication(sys.argv)
    app.setApplicationName(t("app.name"))
    app.setApplicationDisplayName("Smart Terminal")
    app.setOrganizationName("SmartTerminal")
    app.setOrganizationDomain("smart-terminal")

    # macOS: 确保支持多窗口在 Mission Control 中正确显示
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)

    # 设置应用程序图标路径
    icon_path = Path(__file__).parent / "assets" / "smart_terminal.png"

    # Linux: 自动创建 .desktop 文件，用于dock/任务栏图标匹配
    if sys.platform == "linux":
        _ensure_linux_desktop_file(icon_path)
    app.setDesktopFileName("smart-terminal")

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

    # 创建主窗口
    window = MainWindow()
    window.show()

    # 安装 Ctrl+C (SIGINT) 处理器：在终端里按两次 Ctrl+C 可保存并退出
    install_sigint_handler(app)

    # 运行
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
