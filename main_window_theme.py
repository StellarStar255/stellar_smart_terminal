"""MainWindow 的主题/配色混入（从 main_window.py 拆出）。

纯方法搬迁，行为不变；_apply_theme 里对类级 _global_window_navigator
的引用走延迟 _mw.MainWindow（见下方 import 注释）。（大量 Qt 控件名只出现在 QSS 选择器字符串里、
并非 Python 符号，故不 import。）
"""
import re
from PyQt6 import sip
from PyQt6.QtCore import QPoint, QTimer, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QMenu, QPushButton, QVBoxLayout, QWidget,
    QWidgetAction,
)
from i18n import t

# 弹出菜单 / 消息框的 QSS 由主题推导并按用到的颜色值缓存：以前每次 popup 都
# 重建一段写死深色的字符串，主题遍历也触不到它们。
_QSS_CACHE: dict = {}


def _theme_key(theme: dict, *names) -> tuple:
    return tuple(str(theme.get(n, '')) for n in names)


def menu_qss(theme: dict, padding: str = "4px", radius: str = "6px") -> str:
    """临时 QMenu（设置菜单、排序子菜单、★ 快捷方式、颜色选择器）的统一样式。"""
    key = ("menu", padding, radius) + _theme_key(
        theme, 'bg_light', 'text', 'border', 'accent', 'text_dim')
    qss = _QSS_CACHE.get(key)
    if qss is None:
        bg = theme.get('bg_light', '#2d2d44')
        qss = f"""
            QMenu {{
                background-color: {bg};
                color: {theme.get('text', '#eaeaea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
                border-radius: {radius};
                padding: {padding};
            }}
            QMenu::item {{
                padding: 6px 24px 6px 12px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background-color: {theme.get('accent', '#667eea')};
                color: #ffffff;
            }}
            QMenu::item:disabled {{
                color: {theme.get('text_dim', '#666')};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {theme.get('border', '#3d3d5c')};
                margin: 4px 10px;
            }}
        """
        _QSS_CACHE[key] = qss
    return qss


def message_box_qss(theme: dict, check_image: str = "") -> str:
    """QMessageBox 样式：底色/文字/按钮全部取自主题，浅色主题是浅盒子、深色
    主题是深盒子（以前一律写死 #f0f0f0 浅色，深色主题下突兀）。"""
    key = ("msgbox", check_image) + _theme_key(
        theme, 'bg_dark', 'bg_medium', 'bg_lighter', 'bg_hover', 'bg_light',
        'text', 'border', 'accent')
    qss = _QSS_CACHE.get(key)
    if qss is None:
        qss = f"""
            QMessageBox {{
                background-color: {theme.get('bg_dark', '#f0f0f0')};
            }}
            QMessageBox QLabel {{
                color: {theme.get('text', '#333333')};
                font-size: 14px;
            }}
            QMessageBox QCheckBox {{
                color: {theme.get('text', '#333333')};
                spacing: 6px;
            }}
            QMessageBox QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {theme.get('border', '#b6b6bd')}; border-radius: 4px;
                background-color: {theme.get('bg_medium', '#ffffff')};
            }}
            QMessageBox QCheckBox::indicator:hover {{
                border-color: {theme.get('accent', '#667eea')};
            }}
            QMessageBox QCheckBox::indicator:checked {{
                border-color: {theme.get('accent', '#667eea')};
                background-color: {theme.get('accent', '#667eea')};
                {check_image}
            }}
            QMessageBox QPushButton {{
                background-color: {theme.get('bg_lighter', '#e0e0e0')};
                color: {theme.get('text', '#333333')};
                border: 1px solid {theme.get('border', '#999999')};
                padding: 5px 15px;
                border-radius: 3px;
                min-width: 60px;
            }}
            QMessageBox QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#d0d0d0')};
            }}
            QMessageBox QPushButton:pressed {{
                background-color: {theme.get('bg_light', '#c0c0c0')};
            }}
        """
        _QSS_CACHE[key] = qss
    return qss


# 延迟引用宿主类：进程级共享类属性（跨窗口导航器/停靠方式/一次性
# 标志）必须落在真正的 MainWindow 上，而非 type(self)（对假 self /
# 子类会取错）。只在方法内访问 .MainWindow，循环 import 安全。
import main_window as _mw
from app_logging import get_logger

logger = get_logger(__name__)


class ThemeMixin:

    def _on_icon_tint_changed(self, state):
        """图标蒙版开关变更"""
        self._use_icon_tint = (state == Qt.CheckState.Checked.value)
        self._update_app_icon_by_theme()
        self._save_config()

    def _update_app_icon_by_theme(self):
        """根据当前主题和蒙版设置更新图标"""
        if self._use_icon_tint:
            t = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])
            self._update_app_icon(t['accent'])
        else:
            # 恢复原始图标
            if hasattr(self, '_icon_path') and self._icon_path.exists():
                original_icon = QIcon(str(self._icon_path))
                self.setWindowIcon(original_icon)
                from PyQt6.QtWidgets import QApplication
                QApplication.instance().setWindowIcon(original_icon)

    def _apply_theme(self, theme_name: str):
        """应用主题到整个界面"""
        if theme_name not in self.THEMES:
            return

        t = self.THEMES[theme_name]

        # 主题会重写大量控件样式表。先把上次缩放过的控件还原成"未缩放基准"再清缓存：
        # _scale_gui_font_sizes 缓存未命中时会把控件当前样式当基准，若此时还停在已缩放
        # 的字号上，就会逐次放大（如 Switch 12→14→16）。先还原再清，保证下面重新缩放
        # 始终从未缩放基准算起。
        self._restore_original_styles()
        self._original_widget_styles.clear()

        # 主窗口样式
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {t['bg_darkest']};
            }}
            QToolBar {{
                background-color: {t['bg_dark']};
                border: none;
                spacing: 5px;
                padding: 5px 12px;
            }}
            QToolBar::separator {{
                background-color: {t['border']};
                width: 1px;
                margin-left: 0px;
                margin-right: 0px;
                margin-top: 6px;
                margin-bottom: 6px;
            }}
            QToolBar QPushButton {{
                background-color: {t['bg_lighter']};
                color: {t['text']};
                border: none;
                padding: 8px 15px;
                border-radius: 4px;
                font-size: 13px;
            }}
            QToolBar QPushButton:hover {{
                background-color: {t['bg_hover']};
            }}
            QToolBar QPushButton#startBtn {{
                background-color: {t['success']};
            }}
            QToolBar QPushButton#startBtn:hover {{
                background-color: {t['success_hover']};
            }}
            QToolBar QPushButton#stopBtn {{
                background-color: {t['danger']};
            }}
            QToolBar QPushButton#stopBtn:hover {{
                background-color: {t['danger_hover']};
            }}
            QToolBar QPushButton#logToggleBtn {{
                background-color: {t['bg_lighter'] if t.get('is_light_theme') else t['accent']};
            }}
            QToolBar QPushButton#logToggleBtn:hover {{
                background-color: {t['bg_hover'] if t.get('is_light_theme') else t['accent_hover']};
            }}
            QToolBar QLabel {{
                color: {t['text_dim']};
            }}
            QStatusBar {{
                background-color: {t['bg_medium']};
                color: {t['text']};
                border-top: 1px solid {t['bg_light']};
            }}
            QLineEdit {{
                background-color: {t['bg_medium']};
                border: 2px solid {t['border']};
                border-radius: 4px;
                padding: 6px;
                color: {t['text']};
                font-size: 14px;
            }}
            QLineEdit:focus {{
                border-color: {t['accent']};
            }}
            QComboBox {{
                background-color: {t['bg_medium']};
                border: 1px solid {t['border']};
                border-radius: 4px;
                padding: 4px 8px;
                color: {t['text']};
            }}
            QComboBox:hover {{
                border-color: {t['accent']};
            }}
            QComboBox QAbstractItemView {{
                background-color: {t['bg_medium']};
                color: {t['text']};
                selection-background-color: {t['accent']};
                border: 1px solid {t['border']};
            }}
            QCheckBox {{
                color: {t['text']};
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border: 2px solid {t['border']};
                border-radius: 3px;
                background-color: {t['bg_medium']};
            }}
            QCheckBox::indicator:checked {{
                border: 2px solid {t['accent']};
                background-color: {t['accent']};
            }}
        """)

        # 主分割器样式
        self.main_splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background-color: {t['border']};
            }}
            QSplitter::handle:hover {{
                background-color: {t['accent']};
            }}
        """)

        # 标签页样式：选中态与下方 pane 同色融合，靠顶部高亮条 + 加粗白字区分
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background-color: {t['bg_dark']};
            }}
            QTabWidget::tab-bar {{
                alignment: left;
            }}
            QTabBar {{
                background-color: {t['bg_darkest']};
                border: none;
            }}
            QTabBar::tab {{
                background-color: {t['bg_medium']};
                color: {t['text_dim']};
                padding: 7px 18px;
                margin-right: 0px;
                margin-top: 3px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                border-top: 3px solid transparent;
            }}
            QTabBar::tab:selected {{
                background-color: {t['bg_dark']};
                color: {t['text']};
                font-weight: bold;
                margin-top: 0px;
                padding-top: 10px;
                border-top: 3px solid {t['accent']};
            }}
            QTabBar::tab:hover:!selected {{
                background-color: {t['bg_light']};
                color: {t['text']};
            }}
        """)
        # 应用级 QPalette 跟随主题：documentMode 标签栏空白区等"原生绘制"
        # 区域用的是 QPalette 而非 QSS（app.py 启动时设的是硬编码深色调色板，
        # 浅色主题下这些区域会留下一条深色，且给 QTabBar 设 QSS 会禁用
        # autoFillBackground，局部填充救不回来）。多窗口共享同一主题，改
        # 应用级调色板是安全的。
        _pal = QPalette()
        _pal.setColor(QPalette.ColorRole.Window, QColor(t['bg_dark']))
        _pal.setColor(QPalette.ColorRole.WindowText, QColor(t['text']))
        _pal.setColor(QPalette.ColorRole.Base, QColor(t['bg_medium']))
        _pal.setColor(QPalette.ColorRole.AlternateBase, QColor(t['bg_dark']))
        _pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(t['bg_light']))
        _pal.setColor(QPalette.ColorRole.ToolTipText, QColor(t['text']))
        _pal.setColor(QPalette.ColorRole.Text, QColor(t['text']))
        _pal.setColor(QPalette.ColorRole.Button, QColor(t['bg_light']))
        _pal.setColor(QPalette.ColorRole.ButtonText, QColor(t['text']))
        _pal.setColor(QPalette.ColorRole.Link, QColor(t['accent']))
        _pal.setColor(QPalette.ColorRole.Highlight, QColor(t['accent']))
        _pal.setColor(QPalette.ColorRole.HighlightedText, QColor('#ffffff'))
        _app = QApplication.instance()
        if _app is not None:
            _app.setPalette(_pal)
            # 强制原生外观（NSAppearance）跟随主题（Qt 6.8+）：conda 版 Qt 在
            # macOS 深色系统外观下会按原生外观画 documentMode 标签栏空白区等
            # 区域，连 QPalette 都绕过；colorScheme 是唯一对所有构建都生效的开关
            try:
                _app.styleHints().setColorScheme(
                    Qt.ColorScheme.Light if t.get('is_light_theme')
                    else Qt.ColorScheme.Dark)
            except AttributeError:
                pass  # Qt < 6.8 没有 setColorScheme，保持系统外观

        # 信息栏样式
        self.info_frame.setStyleSheet(f"""
            QFrame {{
                background-color: {t['bg_medium']};
                border-top: 1px solid {t['bg_light']};
            }}
            QLabel {{
                color: {t['text_dim']};
                font-size: 12px;
            }}
        """)

        # 工作目录工具栏样式
        self.dir_toolbar.setStyleSheet(f"""
            QToolBar {{
                background-color: {t['bg_medium']};
                border: none;
                padding: 4px 12px;
                spacing: 6px;
            }}
            QToolBar::separator {{
                background-color: {t['border']};
                width: 1px;
                margin-left: 0px;
                margin-right: 0px;
                margin-top: 5px;
                margin-bottom: 5px;
            }}
        """)

        # 固定流式工具栏样式
        if self._pinned_flow_toolbar:
            self._pinned_flow_toolbar.setStyleSheet(f"""
                QToolBar {{
                    padding: 0px;
                    margin: 0px;
                    spacing: 0px;
                    background-color: {t['bg_dark']};
                    border: none;
                }}
            """)
            self._pinned_flow_widget.setStyleSheet(f"""
                QWidget#pinnedFlowWidget {{
                    background-color: {t['bg_dark']};
                }}
            """)
            # 更新分隔符颜色（_FlowSeparator 自绘，需调用 set_line_color）
            if self._flow_layout:
                for i in range(self._flow_layout.count()):
                    item = self._flow_layout.itemAt(i)
                    w = item.widget() if item else None
                    if w is not None and w.objectName() == "_flow_separator":
                        w.set_line_color(t['border'])

        # 自绘控件的颜色不吃 QSS，要显式下发：下拉三角、导航分隔条抓手
        try:
            from widgets import CenteredComboBox
            for combo in self.findChildren(CenteredComboBox):
                combo.set_arrow_color(t['text'])
        except Exception:
            logger.debug("_apply_theme: combo arrow colour failed", exc_info=True)
        handle = getattr(self, 'nav_resize_handle', None)
        if handle is not None and hasattr(handle, 'set_colors'):
            handle.set_colors(t['border'], t['accent'])

        # 工作目录标签样式
        self.dir_label.setStyleSheet(f"color: {t['text_dim']}; font-size: 12px;")

        # 工作目录下拉框样式
        self.working_dir_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {t['bg_dark']};
                border: 1px solid {t['border']};
                border-radius: 4px;
                padding: 4px 8px;
                padding-right: 25px;
                color: {t['text']};
                font-size: 12px;
            }}
            QComboBox:focus {{
                border-color: {t['accent']};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 25px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
            }}
            QComboBox QAbstractItemView {{
                background-color: {t['bg_dark']};
                color: {t['text']};
                selection-background-color: {t['accent']};
                border: 1px solid {t['border']};
            }}
            QComboBox QLineEdit {{
                background-color: {t['bg_dark']};
                color: {t['text']};
                border: none;
                padding: 0;
            }}
        """)

        # 浏览按钮样式
        self.browse_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['bg_lighter']};
                color: {t['text']};
                padding: 4px 12px;
                font-size: 12px;
                border: none;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {t['bg_hover']};
            }}
        """)

        # 切换按钮样式
        self.apply_dir_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['success']};
                color: white;
                padding: 4px 12px;
                font-size: 12px;
                border: none;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {t['success_hover']};
            }}
        """)

        # 当前目录标签样式
        self.current_dir_label.setStyleSheet(f"color: {t['accent']}; font-size: 11px;")

        # 历史目录按钮样式
        self.dir_dropdown_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t['accent']};
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 14px;
                padding: 2px;
            }}
            QPushButton:hover {{
                background-color: {t['accent_hover']};
            }}
        """)

        # 预设切换按钮样式 - 浅色主题使用浅灰色
        if t.get('is_light_theme'):
            self.preset_switch_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {t['bg_lighter']};
                    color: {t['text']};
                    border: none;
                    border-radius: 6px;
                    font-size: 12px;
                    padding: 4px 8px;
                }}
                QPushButton:hover {{
                    background-color: {t['bg_hover']};
                }}
                QPushButton:pressed {{
                    background-color: {t['bg_light']};
                }}
            """)
        else:
            self.preset_switch_btn.setStyleSheet(f"""
                QPushButton {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 {t['accent']}, stop:1 {t['accent_pressed']});
                    color: white;
                    border: none;
                    border-radius: 6px;
                    font-size: 12px;
                    padding: 4px 8px;
                }}
                QPushButton:hover {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 {t['accent_hover']}, stop:1 {t['accent']});
                }}
                QPushButton:pressed {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 {t['accent_pressed']}, stop:1 {t['accent']});
                }}
            """)

        # 右上角按钮容器背景
        self.new_tab_container.setStyleSheet(f"background-color: {t['bg_dark']};")

        # 快速启动按钮样式 - 浅色主题使用浅灰色
        if t.get('is_light_theme'):
            self.quick_launch_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {t['bg_lighter']};
                    color: {t['text']};
                    border: none;
                    border-radius: 4px;
                    font-size: 14px;
                    font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                    font-weight: bold;
                    padding: 0;
                    margin: 0;
                }}
                QPushButton:hover {{
                    background-color: {t['bg_hover']};
                }}
            """)
            self.new_tab_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {t['bg_lighter']};
                    color: {t['text']};
                    border: none;
                    border-radius: 4px;
                    font-size: 16px;
                    font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                    font-weight: bold;
                    padding: 0;
                    margin: 0;
                }}
                QPushButton:hover {{
                    background-color: {t['bg_hover']};
                }}
            """)
        else:
            self.quick_launch_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {t['accent']};
                    color: #ffffff;
                    border: none;
                    border-radius: 4px;
                    font-size: 14px;
                    font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                    font-weight: bold;
                    padding: 0;
                    margin: 0;
                }}
                QPushButton:hover {{
                    background-color: {t['accent_hover']};
                }}
            """)
            self.new_tab_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {t['success']};
                    color: #000000;
                    border: none;
                    border-radius: 4px;
                    font-size: 16px;
                    font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                    font-weight: bold;
                    padding: 0;
                    margin: 0;
                }}
                QPushButton:hover {{
                    background-color: {t['success_hover']};
                }}
            """)

        # 日志面板标题栏样式
        self.log_header.setStyleSheet(f"""
            QFrame {{
                background-color: {t['bg_medium']};
                border-bottom: 1px solid {t['border']};
            }}
        """)

        # 日志标题样式
        self.log_title.setStyleSheet(f"color: {t['accent']}; font-weight: bold;")

        # 清空日志按钮样式
        self.clear_log_btn.setStyleSheet(f"""
            QPushButton {{
                padding: 4px 10px;
                font-size: 11px;
                background-color: {t['bg_lighter']};
                color: {t['text']};
                border: none;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {t['bg_hover']};
            }}
        """)

        # 日志文本区域样式
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: {t['bg_darkest']};
                color: {t['text']};
                border: none;
            }}
        """)

        # Explorer 面板样式
        if hasattr(self, 'explorer_panel'):
            self.explorer_panel.apply_theme(t)

        # Remote Explorer 面板样式
        if hasattr(self, 'remote_panel'):
            self.remote_panel.apply_theme(t)

        # 内置文件编辑器样式
        if hasattr(self, 'editor_area'):
            self.editor_area.apply_theme(t)

        # Explorer 面板容器标题栏样式（直接使用保存的引用，避免 findChildren 搜索）
        if hasattr(self, '_explorer_header'):
            self._explorer_header.setStyleSheet(f"""
                QFrame {{
                    background-color: {t['bg_medium']};
                    border-bottom: 1px solid {t['border']};
                }}
            """)
        if hasattr(self, '_explorer_title'):
            self._explorer_title.setStyleSheet("color: #22c55e; font-weight: bold;")

        # Explorer 切换按钮样式
        if hasattr(self, 'explorer_toggle_btn'):
            self.explorer_toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #22c55e;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }
                QPushButton:hover {
                    background-color: #4ade80;
                }
                QPushButton:checked {
                    background-color: #16a34a;
                }
            """)

        # Git 面板样式
        if hasattr(self, 'git_panel'):
            self.git_panel.apply_theme(t)
        if hasattr(self, 'git_diff_view'):
            self.git_diff_view.apply_theme(t)
        if hasattr(self, 'git_output_view'):
            self.git_output_view.apply_theme(t)

        # Git 面板容器标题栏样式（直接使用保存的引用，避免 findChildren 搜索）
        if hasattr(self, '_git_header'):
            self._git_header.setStyleSheet(f"""
                QFrame {{
                    background-color: {t['bg_medium']};
                    border-bottom: 1px solid {t['border']};
                }}
            """)
        if hasattr(self, '_git_title'):
            self._git_title.setStyleSheet("color: #f97316; font-weight: bold;")

        # Git 切换按钮样式
        if hasattr(self, 'git_toggle_btn'):
            self.git_toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #f97316;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }
                QPushButton:hover {
                    background-color: #fb923c;
                }
                QPushButton:checked {
                    background-color: #ea580c;
                }
            """)

        # VS Code 打开按钮样式
        if hasattr(self, 'vscode_open_btn'):
            self.vscode_open_btn.setStyleSheet("""
                QPushButton {
                    background-color: #007ACC;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }
                QPushButton:hover {
                    background-color: #1a8ad4;
                }
            """)

        # Cursor 打开按钮样式
        if hasattr(self, 'cursor_open_btn'):
            self.cursor_open_btn.setStyleSheet("""
                QPushButton {
                    background-color: #7c3aed;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }
                QPushButton:hover {
                    background-color: #8b5cf6;
                }
            """)

        # 工具栏设置按钮样式
        if hasattr(self, 'toolbar_settings_btn'):
            self.toolbar_settings_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {t['bg_lighter']};
                    color: {t['text']};
                    border: none;
                    border-radius: 4px;
                    font-size: 16px;
                }}
                QPushButton:hover {{
                    background-color: {t['bg_hover']};
                }}
            """)

        # ===== 以下控件构造时写死了深色（默认主题），必须按主题重设，
        # 否则浅色主题下会残留一块块深色补丁 =====

        # 工具栏灰色小标签（Preset:/Theme:/Language:/GUI Font:/Opacity:）
        for _name in ('preset_label', 'theme_label', 'lang_label',
                      'gui_font_label', 'opacity_label'):
            _lbl = getattr(self, _name, None)
            if _lbl is not None:
                _lbl.setStyleSheet(f"color: {t['text_dim']};")

        # 预设下拉框（构造样式见 _setup_toolbar，这里仅替换颜色）
        if hasattr(self, 'preset_combo'):
            self.preset_combo.setStyleSheet(f"""
                QComboBox {{
                    background-color: {t['bg_medium']};
                    border: 2px solid {t['border']};
                    border-radius: 6px;
                    padding: 8px 12px;
                    padding-right: 36px;
                    color: {t['text']};
                    font-size: 12px;
                    combobox-popup: 0;
                }}
                QComboBox:focus {{
                    border-color: {t['accent']};
                }}
                QComboBox::drop-down {{
                    subcontrol-origin: padding;
                    subcontrol-position: center right;
                    width: 32px;
                    border: none;
                    background: transparent;
                }}
                QComboBox::down-arrow {{
                    image: none;
                    width: 18px;
                    height: 18px;
                    background: transparent;
                }}
                QComboBox QAbstractItemView {{
                    background-color: {t['bg_medium']};
                    color: {t['text']};
                    selection-background-color: {t['accent']};
                    selection-color: #ffffff;
                    border: 1px solid {t['border']};
                    border-radius: 4px;
                    outline: none;
                    padding: 4px;
                }}
                QComboBox QAbstractItemView::item {{
                    min-height: 28px;
                    padding: 0px 6px;
                    border-radius: 4px;
                }}
            """)

        # 主题下拉框
        if hasattr(self, 'theme_combo'):
            self.theme_combo.setStyleSheet(f"""
                QComboBox {{
                    background-color: {t['bg_medium']};
                    border: 1px solid {t['border']};
                    border-radius: 4px;
                    padding: 4px 8px;
                    color: {t['text']};
                    min-width: 70px;
                    margin-right: 6px;
                    combobox-popup: 0;
                }}
                QComboBox:hover {{
                    border-color: {t['accent']};
                }}
                QComboBox::drop-down {{
                    border: none;
                    width: 20px;
                }}
                QComboBox QAbstractItemView {{
                    background-color: {t['bg_medium']};
                    color: {t['text']};
                    selection-background-color: {t['accent']};
                    selection-color: #ffffff;
                    border: 1px solid {t['border']};
                    border-radius: 4px;
                    outline: none;
                    padding: 4px;
                }}
                QComboBox QAbstractItemView::item {{
                    min-height: 28px;
                    padding: 0px 6px;
                    border-radius: 4px;
                }}
            """)

        # 语言 / GUI 字号 / 透明度下拉框（构造时共用 _COMBO_STYLE）
        _combo_qss = f"""
            QComboBox {{
                background-color: {t['bg_medium']};
                border: 1px solid {t['border']};
                border-radius: 4px;
                padding: 4px 8px;
                color: {t['text']};
                combobox-popup: 0;
            }}
            QComboBox:hover {{
                border-color: {t['accent']};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {t['bg_medium']};
                color: {t['text']};
                selection-background-color: {t['accent']};
                selection-color: #ffffff;
                border: 1px solid {t['border']};
                border-radius: 4px;
                outline: none;
                padding: 4px;
            }}
            QComboBox QAbstractItemView::item {{
                min-height: 28px;
                padding: 0px 6px;
                border-radius: 4px;
            }}
        """
        for _name in ('lang_combo', 'gui_font_spin', 'opacity_spin'):
            _combo = getattr(self, _name, None)
            if _combo is not None:
                _combo.setStyleSheet(_combo_qss)

        # 管理预设按钮（bg_lighter 深浅主题下都是中性的按钮底色）
        if hasattr(self, 'manage_preset_btn'):
            self.manage_preset_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {t['bg_lighter']};
                    padding: 8px 12px;
                    font-size: 12px;
                }}
            """)

        # 工具栏勾选框（Image @prefix / Image to CWD / Window Navigator / Tint）：
        # 指示器的 unchecked/checked 伪类必须整套重设，通用 QCheckBox 规则穿不透
        _checkbox_qss = f"""
            QCheckBox {{
                color: {t['text_dim']};
                font-size: 11px;
                spacing: 8px;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
            }}
            QCheckBox::indicator:unchecked {{
                border: 2px solid {t['border']};
                border-radius: 3px;
                background-color: {t['bg_medium']};
            }}
            QCheckBox::indicator:checked {{
                border: 2px solid {t['accent']};
                border-radius: 3px;
                background-color: {t['accent']};
            }}
        """
        for _name in ('image_prefix_checkbox', 'image_local_checkbox',
                      'window_nav_checkbox', 'icon_tint_checkbox',
                      'sidebar_sync_checkbox'):
            _cb = getattr(self, _name, None)
            if _cb is not None:
                _cb.setStyleSheet(_checkbox_qss)

        # Remote 切换按钮（品牌色固定，但文字需要固定白色 ——
        # 否则浅色主题下会继承 QToolBar QPushButton 的深色文字）
        if hasattr(self, 'remote_toggle_btn'):
            self.remote_toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #38bdf8;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }
                QPushButton:hover {
                    background-color: #7dd3fc;
                }
                QPushButton:checked {
                    background-color: #0284c7;
                }
            """)

        # 命令搜索框（Cmd+K）
        if hasattr(self, 'command_palette'):
            self.command_palette.apply_theme(t)

        # 窗口导航面板：内嵌面板 + 全局浮动面板
        if getattr(self, 'nav_panel', None) is not None:
            self.nav_panel.apply_theme(t)
        _global_nav = _mw.MainWindow._global_window_navigator
        if _global_nav is not None:
            try:
                if not sip.isdeleted(_global_nav):
                    _global_nav.apply_theme(t)
            except Exception:
                logger.debug("_apply_theme: suppressed exception", exc_info=True)

        # 更新所有终端的颜色
        for terminals in self.tab_terminals.values():
            for terminal in terminals:
                terminal.bg_color = QColor(t['terminal_bg'])
                terminal.fg_color = QColor(t['terminal_fg'])
                # 应用浅色主题专用颜色
                if t.get('is_light_theme'):
                    terminal.set_light_theme_colors(
                        t.get('terminal_colors'),
                        t.get('terminal_bright_colors'),
                        t.get('selection_color'),
                        t.get('cursor_color')
                    )
                else:
                    terminal.reset_to_dark_theme_colors()
                terminal._cache_valid = False
                terminal._content_dirty = True
                terminal.update()

        # 更新窗口和Dock图标（根据设置决定是否使用蒙版）
        self._update_app_icon_by_theme()

        # 主题切换后重新应用 GUI 字体缩放（样式表已被主题覆盖；缓存已在方法开头
        # 还原+清空，此时所有控件都处于未缩放基准，缩放不会复合放大）。
        if self._gui_font_size != 0:
            self._scale_gui_font_sizes(self._gui_font_size)

        # 样式变更后刷新 flow layout 高度
        if hasattr(self, '_pinned_flow_toolbar') and self._pinned_flow_toolbar and self._pinned_flow_toolbar.isVisible():
            QTimer.singleShot(0, self._update_flow_toolbar_height)

    def _create_themed_icon(self, theme_color: str) -> QIcon:
        """创建带主题色蒙版的图标

        给原始图标叠加一层半透明主题色蒙版，使整个图标带有主题色调
        使用 QPainter CompositionMode 实现高效的颜色叠加
        """
        if not hasattr(self, '_icon_path') or not self._icon_path.exists():
            return QIcon()

        # 加载原始图标
        original_pixmap = QPixmap(str(self._icon_path))
        if original_pixmap.isNull():
            return QIcon()

        # 创建结果 pixmap
        result = QPixmap(original_pixmap.size())
        result.fill(Qt.GlobalColor.transparent)

        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 先绘制原始图标
        painter.drawPixmap(0, 0, original_pixmap)

        # 使用 SourceAtop 模式叠加半透明主题色（只影响非透明区域）
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        theme_qcolor = QColor(theme_color)
        theme_qcolor.setAlpha(60)  # 约 23% 透明度，颜色比较淡
        painter.fillRect(result.rect(), theme_qcolor)

        painter.end()

        return QIcon(result)

    def _update_app_icon(self, accent_color: str):
        """更新应用程序图标（窗口和Dock）"""
        themed_icon = self._create_themed_icon(accent_color)
        if not themed_icon.isNull():
            self.setWindowIcon(themed_icon)
            # 同时更新应用程序级别的图标（影响Dock）
            from PyQt6.QtWidgets import QApplication
            QApplication.instance().setWindowIcon(themed_icon)

    def _update_title_label_color(self):
        """更新标题标签颜色

        字号必须跟随当前 GUI 字体缩放：本方法在改窗口颜色时会被重复调用，
        若直接写死 13px 会把 _scale_gui_font_sizes 放大过的字号打回原形，
        导致「改过颜色的窗口」和「没改过的窗口」标题字号不一致。
        """
        base_ss = f"color: {self._window_color}; font-size: 13px; font-weight: bold;"
        scale = self._current_gui_font_scale()
        scaled_ss = re.sub(
            r'font-size:\s*(\d+)px',
            lambda m: f'font-size: {max(7, round(int(m.group(1)) * scale))}px',
            base_ss,
        )
        self.title_label.setStyleSheet(scaled_ss)
        # 始终把"未缩放"的 base_ss 登记为缩放基准：_scale_gui_font_sizes 命中缓存时
        # 会用 base_ss（13px）而非当前已缩放的样式去算，避免 _apply_theme 清缓存后
        # 把已缩放值（15px）误当基准导致二次缩放（15→18px）。
        self._original_widget_styles[id(self.title_label)] = (self.title_label, base_ss)

    def _update_color_btn_style(self):
        """更新颜色按钮样式"""
        self.color_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self._window_color};
                border: 2px solid #3d3d5c;
                border-radius: 12px;
            }}
            QPushButton:hover {{
                border-color: #eaeaea;
            }}
        """)

    def _show_color_picker(self):
        """显示颜色选择器弹窗（支持展开更多颜色）"""
        menu = QMenu(self)
        menu.setStyleSheet(menu_qss(
            self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]),
            padding="12px", radius="10px"))

        # 状态记录
        self._color_picker_expanded = getattr(self, '_color_picker_expanded', False)

        # 主容器
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(10)

        def create_color_btn(color):
            """创建单个颜色按钮"""
            btn = QPushButton()
            btn.setFixedSize(36, 36)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            border = "3px solid #ffffff" if color == self._window_color else "2px solid #555"
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {color};
                    border: {border};
                    border-radius: 18px;
                }}
                QPushButton:hover {{
                    border: 3px solid #ffffff;
                }}
            """)
            btn.clicked.connect(lambda checked, c=color: self._set_window_color(c, menu))
            return btn

        def create_color_row(colors):
            """创建一行颜色按钮"""
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)
            for color in colors:
                row_layout.addWidget(create_color_btn(color))
            return row_widget

        # 第一行：常用颜色
        main_layout.addWidget(create_color_row(self.WINDOW_COLORS_PRIMARY))

        # 如果已展开，显示所有颜色
        if self._color_picker_expanded:
            main_layout.addWidget(create_color_row(self.WINDOW_COLORS_EXTENDED))
            main_layout.addWidget(create_color_row(self.WINDOW_COLORS_SPECIAL))

        # 展开/收起按钮
        expand_btn = QPushButton(t("color_picker.collapse") if self._color_picker_expanded else t("color_picker.expand"))
        expand_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        expand_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #aaa;
                border: none;
                font-size: 12px;
                padding: 4px 8px;
            }
            QPushButton:hover {
                color: #ffffff;
            }
        """)

        def toggle_expand():
            self._color_picker_expanded = not self._color_picker_expanded
            menu.close()
            # 重新打开菜单以显示新布局
            def show_picker():
                if not sip.isdeleted(self):
                    self._show_color_picker()
            QTimer.singleShot(50, show_picker)

        expand_btn.clicked.connect(toggle_expand)
        main_layout.addWidget(expand_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        action = QWidgetAction(menu)
        action.setDefaultWidget(main_widget)
        menu.addAction(action)

        # 显示在按钮下方
        menu.exec(self.color_btn.mapToGlobal(QPoint(0, self.color_btn.height())))
