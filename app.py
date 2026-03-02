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
from i18n import t, set_language, get_language


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


def main():
    """主函数"""
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

    # Linux: 设置桌面文件名，用于dock图标匹配
    app.setDesktopFileName("smart-terminal")

    # 设置应用程序图标 (提供多个尺寸以适配 Windows 任务栏/标题栏)
    icon_path = Path(__file__).parent / "assets" / "smart_terminal.png"
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
        font = QFont("Noto Sans", 11)
        font.setFamilies(["Noto Sans", "Noto Color Emoji"])
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)

    # 创建主窗口
    window = MainWindow()
    window.show()

    # 运行
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
