"""
主窗口
智能终端的GUI主界面
"""
import os
import re
import json
import sys
from pathlib import Path
from datetime import datetime

# macOS 原生窗口支持
if sys.platform == 'darwin':
    try:
        import objc
        from ctypes import c_void_p
        from AppKit import (
            NSApp, NSWindow,
            NSWindowCollectionBehaviorFullScreenPrimary,
            NSWindowCollectionBehaviorManaged,
            NSWindowCollectionBehaviorParticipatesInCycle
        )
        MACOS_NATIVE_AVAILABLE = True
    except ImportError:
        MACOS_NATIVE_AVAILABLE = False
else:
    MACOS_NATIVE_AVAILABLE = False

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QToolBar, QStatusBar,
    QMessageBox, QFileDialog, QComboBox, QSplitter,
    QTextEdit, QFrame, QDialog, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QDialogButtonBox,
    QFormLayout, QGroupBox, QCheckBox, QTabWidget, QTabBar,
    QApplication, QInputDialog, QMenu, QStyledItemDelegate, QStyle,
    QStyleOptionViewItem, QSpinBox, QSizePolicy
)
from PyQt6 import sip  # 用于检查 C++ 对象是否已被删除
from PyQt6.QtCore import Qt, QTimer, QEvent, QPoint, QMimeData, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QIcon, QFont, QColor, QPixmap, QPainter, QPen, QDrag, QCursor, QBrush, QPalette
from PyQt6.QtWidgets import QWidgetAction

from terminal_widget import TerminalWidget
from session_manager import SessionManager
from exporter import export_session
from history_dialog import HistoryDialog
from openai_server import OpenAIServerManager
from git_widget import GitPanel
from explorer_widget import ExplorerPanel
from toolbar_manager import ToolbarManagerDialog
from file_editor import FileEditorWidget
from i18n import t, set_language, get_language
from flow_layout import FlowLayout
import shutil
import subprocess



def get_default_shell():
    """
    获取系统默认 shell，跨平台支持：
    - macOS/Linux: 优先 $SHELL，然后 zsh -> bash -> sh
    - Windows: 优先 PowerShell，然后 cmd.exe
    """
    if sys.platform == 'win32':
        # Windows 平台
        # 优先使用 PowerShell（更现代）
        if shutil.which('pwsh'):  # PowerShell Core (跨平台版本)
            return 'pwsh'
        if shutil.which('powershell'):  # Windows PowerShell
            return 'powershell'
        # 回退到 cmd.exe
        comspec = os.environ.get('COMSPEC', '')
        if comspec and shutil.which(comspec):
            return comspec
        return 'cmd.exe'
    else:
        # macOS / Linux
        # 首先尝试用户的默认 shell
        user_shell = os.environ.get('SHELL', '')
        if user_shell and shutil.which(os.path.basename(user_shell)):
            return os.path.basename(user_shell)

        # 按优先级尝试常见 shell
        for shell in ['zsh', 'bash', 'sh']:
            if shutil.which(shell):
                return shell

        # 最后的回退
        return 'sh'


class SelectAllLineEdit(QLineEdit):
    """点击时自动全选的输入框"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._first_click = True

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._first_click = True
        # Linux 上需要延迟执行全选，确保在所有事件处理完成后执行
        QTimer.singleShot(0, self.selectAll)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self._first_click:
            self._first_click = False
            # 使用延迟执行确保在鼠标释放事件之后执行全选
            # 这在 Linux 上尤其重要，因为鼠标事件可能会取消选择
            QTimer.singleShot(0, self.selectAll)


class DetachableTabBar(QTabBar):
    """可拖拽分离的标签栏"""

    tab_detach_requested = pyqtSignal(int, QPoint)  # 发送要分离的tab索引和全局坐标

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_start_pos = None
        self._drag_tab_index = -1
        self._is_dragging = False
        self._detach_threshold = 50  # 拖拽分离的距离阈值
        self._original_cursor = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
            self._drag_tab_index = self.tabAt(event.pos())
            self._original_cursor = self.cursor()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is None or self._drag_tab_index < 0:
            super().mouseMoveEvent(event)
            return

        # 计算拖拽距离
        diff = event.pos() - self._drag_start_pos

        # 当接近阈值时，改变鼠标光标提供视觉反馈
        if abs(diff.y()) > self._detach_threshold * 0.6:
            self.setCursor(Qt.CursorShape.DragMoveCursor)
        else:
            if self._original_cursor:
                self.setCursor(self._original_cursor)

        # 如果垂直方向拖拽超过阈值，触发分离
        if abs(diff.y()) > self._detach_threshold:
            self._is_dragging = True
            global_pos = self.mapToGlobal(event.pos())
            self.tab_detach_requested.emit(self._drag_tab_index, global_pos)
            self._reset_drag_state()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._reset_drag_state()
        super().mouseReleaseEvent(event)

    def _reset_drag_state(self):
        self._drag_start_pos = None
        self._drag_tab_index = -1
        self._is_dragging = False
        if self._original_cursor:
            self.setCursor(self._original_cursor)
            self._original_cursor = None


class DetachedWindow(QMainWindow):
    """分离出的独立终端窗口"""

    window_closed = pyqtSignal(object)  # 窗口关闭时发送信号，传递自身

    def __init__(self, title, splitter, terminals, session, main_window, parent=None):
        super().__init__(parent)

        self.splitter = splitter
        self.terminals = terminals
        self.session = session
        self.main_window = main_window

        self.setWindowTitle(f"{title} - Smart Terminal")
        self.setMinimumSize(600, 400)
        self.resize(900, 650)  # 合理的默认大小

        # 从主窗口复制图标
        if main_window:
            self.setWindowIcon(main_window.windowIcon())

        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        # 设置中心部件
        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 将 splitter 添加到新窗口
        if self.splitter:
            self.splitter.setParent(central)
            layout.addWidget(self.splitter)

        # 底部状态栏
        self.statusBar().showMessage(t("window.detached_status"))

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0f0f1a;
            }
            QWidget {
                background-color: #1a1a2e;
            }
            QStatusBar {
                background-color: #16213e;
                color: #888;
                font-size: 12px;
            }
        """)

    def closeEvent(self, event):
        # 完整清理所有终端资源
        for terminal in self.terminals:
            terminal.cleanup()

        # 发送关闭信号
        self.window_closed.emit(self)
        super().closeEvent(event)


class NoHighlightDelegate(QStyledItemDelegate):
    """自定义 delegate，禁用默认的选中高亮，使用 item 的背景色"""

    def paint(self, painter, option, index):
        # 先绘制 item 的背景色
        bg_brush = index.data(Qt.ItemDataRole.BackgroundRole)
        if bg_brush:
            painter.fillRect(option.rect, bg_brush)

        # 移除选中状态标志，防止绘制默认高亮
        opt = QStyleOptionViewItem(option)
        opt.state = opt.state & ~QStyle.StateFlag.State_Selected
        opt.state = opt.state & ~QStyle.StateFlag.State_HasFocus

        # 绘制文本和其他内容
        super().paint(painter, opt, index)


class WindowNavigatorPanel(QWidget):
    """窗口快速导航面板 - 永远在最前的小窗口"""

    window_switch_requested = pyqtSignal(object)  # 请求切换到某个窗口
    panel_closed = pyqtSignal()  # 面板关闭信号

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setWindowTitle(t("window.navigator_title"))
        self.setMinimumSize(200, 150)
        self.resize(250, 300)

        # 排序方式: 'time' (按创建时间) / 'name' (按名称) / 'manual' (手动排序)
        self._sort_mode = 'time'
        # 简洁显示模式：只显示文件夹名
        self._compact_mode = True
        # 手动排序的窗口顺序（存储窗口ID）
        self._manual_order = []

        self._setup_ui()
        self._apply_style()

        # 缓存上次的窗口信息，避免不必要的刷新
        self._last_window_info = []  # [(title, color), ...]
        self._cached_windows = []  # 缓存窗口引用

        # 定时刷新窗口列表（降低频率以减少开销）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._check_and_refresh)
        self._refresh_timer.start(5000)  # 每5秒检查一次（进一步降低频率）

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 标题
        title_label = QLabel(t("window.navigator_list_title"))
        title_label.setStyleSheet("color: #667eea; font-weight: bold; font-size: 13px;")
        layout.addWidget(title_label)

        # 搜索框
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t("window.search_placeholder"))
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 4px 8px;
                color: #eaeaea;
                font-size: 12px;
            }
            QLineEdit:focus {
                border-color: #667eea;
            }
        """)
        self.search_input.textChanged.connect(self._filter_window_list)
        layout.addWidget(self.search_input)

        # 窗口列表
        self.window_list = QListWidget()
        # 使用自定义 delegate 禁用默认选中高亮
        self.window_list.setItemDelegate(NoHighlightDelegate(self.window_list))
        # 启用拖拽排序
        self.window_list.setDragEnabled(True)
        self.window_list.setAcceptDrops(True)
        self.window_list.setDropIndicatorShown(True)
        self.window_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.window_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        # 直接在 window_list 上设置样式（不设置悬停样式，由代码动态控制）
        self.window_list.setStyleSheet("""
            QListWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                font-size: 12px;
                outline: none;
            }
            QListWidget::item {
                padding: 8px;
                border-bottom: 1px solid #2d2d44;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #2d2d44;
            }
        """)
        # 启用鼠标追踪以支持悬停效果
        self.window_list.setMouseTracking(True)
        self.window_list.itemEntered.connect(self._on_item_entered)
        self._hovered_item = None
        # 安装事件过滤器处理鼠标离开
        self.window_list.viewport().installEventFilter(self)
        self.window_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.window_list.itemClicked.connect(self._on_item_clicked)
        self.window_list.currentItemChanged.connect(self._on_current_item_changed)
        # 监听拖拽完成，自动切换到手动排序模式
        self.window_list.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self.window_list)

        # 简洁显示复选框
        self.compact_checkbox = QCheckBox(t("window.compact_display"))
        self.compact_checkbox.setChecked(True)  # 默认开启简洁显示
        self.compact_checkbox.setToolTip(t("window.compact_tooltip"))
        self.compact_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaaaaa;
                font-size: 11px;
                border: none;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
            }
        """)
        self.compact_checkbox.stateChanged.connect(self._toggle_compact_mode)
        layout.addWidget(self.compact_checkbox)

        # 拖拽提示标签（默认隐藏）
        self.drag_hint_label = QLabel(t("window.drag_hint"))
        self.drag_hint_label.setStyleSheet("color: #667eea; font-size: 10px; border: none;")
        self.drag_hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drag_hint_label.setVisible(False)
        layout.addWidget(self.drag_hint_label)

        # 底部按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        # 排序切换按钮
        self.sort_btn = QPushButton(t("window.sort_time"))
        self.sort_btn.setToolTip(t("window.sort_toggle_tooltip"))
        self.sort_btn.clicked.connect(self._toggle_sort_mode)
        btn_layout.addWidget(self.sort_btn)

        # 刷新按钮
        refresh_btn = QPushButton(t("window.refresh"))
        refresh_btn.clicked.connect(self._force_refresh)
        btn_layout.addWidget(refresh_btn)

        layout.addLayout(btn_layout)

    def _toggle_sort_mode(self):
        """切换排序方式: 时间 -> 名称 -> 手动 -> 时间"""
        if self._sort_mode == 'time':
            self._sort_mode = 'name'
            self.sort_btn.setText(t("window.sort_name"))
            self.sort_btn.setToolTip(t("window.sort_tooltip_name"))
            self.drag_hint_label.setVisible(False)
        elif self._sort_mode == 'name':
            self._sort_mode = 'manual'
            self.sort_btn.setText(t("window.sort_manual"))
            self.sort_btn.setToolTip(t("window.sort_tooltip_manual"))
            self.drag_hint_label.setVisible(True)
            self._save_manual_order()
        else:
            self._sort_mode = 'time'
            self.sort_btn.setText(t("window.sort_time"))
            self.sort_btn.setToolTip(t("window.sort_tooltip_time"))
            self.drag_hint_label.setVisible(False)
        self._force_refresh()

    def _on_rows_moved(self):
        """拖拽排序完成后自动切换到手动模式并保存顺序"""
        if self._sort_mode != 'manual':
            self._sort_mode = 'manual'
            self.sort_btn.setText(t("window.sort_manual"))
            self.sort_btn.setToolTip(t("window.sort_tooltip_manual"))
            self.drag_hint_label.setVisible(True)
        self._save_manual_order()

    def _save_manual_order(self):
        """保存当前列表顺序"""
        self._manual_order = []
        for i in range(self.window_list.count()):
            item = self.window_list.item(i)
            window = item.data(Qt.ItemDataRole.UserRole)
            if window:
                self._manual_order.append(id(window))

    def _toggle_compact_mode(self, state):
        """切换简洁显示模式"""
        self._compact_mode = (state == Qt.CheckState.Checked.value)
        self._force_refresh()

    def _extract_folder_name(self, title: str, window=None) -> str:
        """从窗口标题中提取文件夹名

        标题格式通常为: "预设名-文件夹名 - Smart Terminal #N" 或 "预设名-文件夹名"
        提取最后一个有效的文件夹名部分。
        如果无法提取，则从窗口的工作目录获取。
        """
        # 先去掉 " - Smart Terminal" 后缀
        if " - Smart Terminal" in title:
            title = title.split(" - Smart Terminal")[0]

        # 尝试找到最后一个 "-" 或 ")-" 之后的部分作为文件夹名
        # 格式如 "Claude Opus (with proxy)-stellar_markdown"
        if ")-" in title:
            folder_name = title.split(")-")[-1].strip()
        elif "-" in title:
            # 可能没有括号，直接用最后一个 "-" 分割
            folder_name = title.split("-")[-1].strip()
        else:
            folder_name = None

        # 如果无法从标题提取，尝试从窗口工作目录获取
        if not folder_name and window and hasattr(window, '_window_cwd'):
            cwd = window._window_cwd
            if cwd:
                folder_name = os.path.basename(cwd) or cwd

        return folder_name if folder_name else title.strip()

    def _force_refresh(self):
        """强制刷新窗口列表"""
        self._last_window_info = []
        self._cached_windows = []
        self._refresh_window_list()

    def _check_and_refresh(self):
        """轻量级检查，只在必要时刷新"""
        app = QApplication.instance()
        if not app:
            return

        # 快速检查：获取当前窗口数量（排除已删除的窗口）
        current_windows = []
        for w in app.topLevelWidgets():
            try:
                if isinstance(w, MainWindow) and not sip.isdeleted(w) and w.isVisible():
                    current_windows.append(w)
            except RuntimeError:
                continue
        current_count = len(current_windows)
        cached_count = len(self._cached_windows)

        # 窗口数量变化时立即刷新（新增或关闭窗口）
        if current_count != cached_count:
            self._refresh_window_list()
            return

        # 检查缓存的窗口是否仍然有效
        for w in self._cached_windows:
            try:
                if sip.isdeleted(w) or not w.isVisible():
                    # 有窗口关闭了，需要刷新
                    self._refresh_window_list()
                    return
            except RuntimeError:
                # 窗口已被删除
                self._refresh_window_list()
                return

        # 检查窗口标题或颜色是否变化
        try:
            current_info = [(w.windowTitle(), w.get_window_color()) for w in current_windows]
        except RuntimeError:
            # 窗口在遍历过程中被删除
            self._refresh_window_list()
            return
        if current_info != self._last_window_info:
            self._refresh_window_list()

    def _filter_window_list(self, text: str):
        """根据搜索文本过滤窗口列表（支持空格分隔的多关键词）"""
        keywords = text.lower().split()
        for i in range(self.window_list.count()):
            item = self.window_list.item(i)
            if not keywords:
                item.setHidden(False)
            else:
                item_text = item.text().lower()
                # 所有关键词都必须匹配
                item.setHidden(not all(kw in item_text for kw in keywords))

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #1a1a2e;
                border: 1px solid #3d3d5c;
                border-radius: 8px;
            }
            QLabel {
                border: none;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                color: #eaeaea;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #667eea;
            }
        """)

    def _refresh_window_list(self):
        """刷新窗口列表（优化：只在变化时更新）"""
        app = QApplication.instance()
        if not app:
            return

        # 获取所有 MainWindow 实例（排除已删除的窗口）
        windows = []
        for w in app.topLevelWidgets():
            try:
                if isinstance(w, MainWindow) and not sip.isdeleted(w) and w.isVisible():
                    windows.append(w)
            except RuntimeError:
                continue  # 窗口已被删除，跳过

        # 根据排序模式排序
        if self._sort_mode == 'time':
            windows.sort(key=lambda w: w.get_created_time())
        elif self._sort_mode == 'name':
            windows.sort(key=lambda w: w.windowTitle())
        elif self._sort_mode == 'manual' and self._manual_order:
            # 手动排序：按保存的顺序排列，新窗口放到末尾
            order_map = {wid: idx for idx, wid in enumerate(self._manual_order)}
            windows.sort(key=lambda w: order_map.get(id(w), 9999))

        # 检查是否有变化（标题或颜色）
        try:
            current_info = [(w.windowTitle(), w.get_window_color()) for w in windows]
        except RuntimeError:
            # 窗口在遍历过程中被删除，重新刷新
            self._last_window_info = []
            return
        if current_info == self._last_window_info:
            return  # 没有变化，跳过更新

        # 更新缓存
        self._last_window_info = current_info
        self._cached_windows = windows

        # 阻止信号以避免不必要的 UI 更新
        self.window_list.blockSignals(True)

        # 更新列表
        self.window_list.clear()
        for window in windows:
            try:
                if sip.isdeleted(window):
                    continue
                title = window.windowTitle()
                # 简洁模式：提取文件夹名
                if self._compact_mode:
                    display_title = self._extract_folder_name(title, window)
                else:
                    display_title = title
                color = window.get_window_color()
                item = QListWidgetItem(display_title)
                item.setData(Qt.ItemDataRole.UserRole, window)
                item.setForeground(QColor(color))
                self.window_list.addItem(item)
            except RuntimeError:
                continue  # 窗口在处理过程中被删除，跳过

        self.window_list.blockSignals(False)

        # 选中第一项并设置颜色
        if self.window_list.count() > 0:
            self.window_list.setCurrentRow(0)
            current_item = self.window_list.currentItem()
            if current_item:
                self._update_item_colors(current_item)

    # 淡化背景色缓存（类级别缓存）
    _faded_bg_cache = {}

    def _get_faded_bg_color(self, color_hex: str) -> QColor:
        """生成淡化的背景色（与深色背景混合）- 带缓存

        Args:
            color_hex: 颜色的十六进制字符串，如 '#667eea'

        Returns:
            淡化后的 QColor 对象
        """
        # 检查缓存
        if color_hex in self._faded_bg_cache:
            return self._faded_bg_cache[color_hex]

        theme_color = QColor(color_hex)
        bg_color = QColor("#16213e")  # 列表背景色

        # 混合主题色和背景色，比例约 40:60（更明显的效果）
        r = int(theme_color.red() * 0.4 + bg_color.red() * 0.6)
        g = int(theme_color.green() * 0.4 + bg_color.green() * 0.6)
        b = int(theme_color.blue() * 0.4 + bg_color.blue() * 0.6)

        result = QColor(r, g, b)
        # 缓存结果（限制缓存大小）
        if len(self._faded_bg_cache) < 50:
            self._faded_bg_cache[color_hex] = result
        return result

    def _on_current_item_changed(self, current, previous):
        """选中项变化时更新颜色"""
        self._update_all_item_colors()

    def _on_item_clicked(self, item):
        """单击切换窗口"""
        # 确保选中项颜色正确显示
        self._update_all_item_colors()
        window = item.data(Qt.ItemDataRole.UserRole)
        if window and not sip.isdeleted(window):
            self._switch_to_window(window)

    def _on_item_entered(self, item):
        """鼠标进入某个项时"""
        self._hovered_item = item
        self._update_all_item_colors()

    def eventFilter(self, obj, event):
        """事件过滤器，处理鼠标离开列表"""
        if obj == self.window_list.viewport() and event.type() == QEvent.Type.Leave:
            self._hovered_item = None
            self._update_all_item_colors()
        return super().eventFilter(obj, event)

    def _update_item_colors(self, selected_item):
        """兼容旧方法"""
        self._update_all_item_colors()

    def _update_all_item_colors(self):
        """更新所有项的颜色，根据选中和悬停状态显示主题色背景 - 优化版本"""
        selected_item = self.window_list.currentItem()
        hovered_item = getattr(self, '_hovered_item', None)

        # 缓存无背景画刷，避免重复创建
        no_brush = QBrush(Qt.BrushStyle.NoBrush)

        # 只更新需要更新的项（选中项、悬停项、以及之前的选中/悬停项）
        for i in range(self.window_list.count()):
            item = self.window_list.item(i)
            window = item.data(Qt.ItemDataRole.UserRole)
            if not window or sip.isdeleted(window):
                continue

            try:
                is_highlighted = (item == selected_item or item == hovered_item)
                color = window.get_window_color()

                # 只在颜色变化时更新前景色
                current_fg = item.foreground().color().name()
                if current_fg != color:
                    item.setForeground(QColor(color))

                # 根据高亮状态设置背景
                if is_highlighted:
                    bg_color = self._get_faded_bg_color(color)
                    item.setBackground(QBrush(bg_color))
                else:
                    # 只在有背景时清除
                    if item.background().style() != Qt.BrushStyle.NoBrush:
                        item.setBackground(no_brush)
            except RuntimeError:
                continue  # 窗口已被删除，跳过

    def _on_item_double_clicked(self, item):
        """双击切换窗口"""
        window = item.data(Qt.ItemDataRole.UserRole)
        if window and not sip.isdeleted(window):
            self._switch_to_window(window)

    def _switch_to_window(self, window):
        """切换到指定窗口"""
        if window and not sip.isdeleted(window):
            try:
                window.raise_()
                window.activateWindow()
                # 如果窗口最小化，恢复它
                if window.isMinimized():
                    window.showNormal()
                self.window_switch_requested.emit(window)
            except RuntimeError:
                # 窗口已被删除，刷新列表
                self._refresh_window_list()

    def showEvent(self, event):
        """显示时刷新列表"""
        super().showEvent(event)
        # 强制刷新
        self._force_refresh()

    def closeEvent(self, event):
        """关闭时停止定时器并发送关闭信号"""
        self._refresh_timer.stop()
        self.panel_closed.emit()
        super().closeEvent(event)

    def select_window(self, window):
        """选中指定的窗口项

        当某个窗口被激活时调用此方法，更新列表的选中状态。
        """
        for i in range(self.window_list.count()):
            item = self.window_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) is window:
                self.window_list.blockSignals(True)
                self.window_list.setCurrentRow(i)
                self.window_list.blockSignals(False)
                self._update_all_item_colors()
                break


class PresetDialog(QDialog):
    """预设命令管理对话框"""

    def __init__(self, presets: list, parent=None, auto_add: bool = False, title: str = None):
        super().__init__(parent)
        self.presets = [p.copy() for p in presets]  # 深拷贝
        self._auto_add = auto_add
        self.setWindowTitle(title or t("preset.manage_title"))
        self.setMinimumSize(600, 450)

        # 使用唯一 ID 来稳定关联 item 和 preset（避免拖拽时数据丢失）
        self._preset_id_counter = 0
        self._preset_map = {}  # id -> preset
        for p in self.presets:
            pid = self._next_preset_id()
            p['_id'] = pid
            self._preset_map[pid] = p

        # 关闭标志，防止关闭时触发信号处理
        self._closing = False

        self._setup_ui()
        self._apply_style()

        # 如果是添加模式，自动创建新预设
        if self._auto_add:
            QTimer.singleShot(0, self._add_preset)

    def _next_preset_id(self):
        """生成下一个唯一 ID"""
        self._preset_id_counter += 1
        return self._preset_id_counter

    def _get_preset_by_id(self, pid):
        """通过 ID 获取 preset"""
        return self._preset_map.get(pid)

    def _setup_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)

        # 内容区域（水平布局）
        content_layout = QHBoxLayout()

        # 左侧：预设列表
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel(t("preset.list_label")))

        self.preset_list = QListWidget()
        self.preset_list.currentRowChanged.connect(self._on_selection_changed)
        # 启用拖拽排序
        self.preset_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.preset_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.preset_list.model().rowsMoved.connect(self._on_rows_moved)
        left_layout.addWidget(self.preset_list)

        # 按钮组
        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton(t("preset.new"))
        self.add_btn.clicked.connect(self._add_preset)
        self.save_btn = QPushButton(t("preset.save"))
        self.save_btn.clicked.connect(self._save_current_preset)
        self.save_btn.setToolTip(t("preset.save_tooltip"))
        self.delete_btn = QPushButton(t("preset.delete"))
        self.delete_btn.clicked.connect(self._delete_preset)
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.delete_btn)
        left_layout.addLayout(btn_layout)

        content_layout.addLayout(left_layout, 1)

        # 右侧：编辑区
        right_layout = QVBoxLayout()

        # 名称
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel(t("preset.name_label")))
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._on_name_changed)
        name_layout.addWidget(self.name_edit)
        right_layout.addLayout(name_layout)

        # 命令（多行）
        right_layout.addWidget(QLabel(t("preset.commands_label")))
        self.commands_edit = QPlainTextEdit()
        self.commands_edit.setPlaceholderText(t("preset.commands_placeholder"))
        self.commands_edit.textChanged.connect(self._on_commands_changed)
        right_layout.addWidget(self.commands_edit)

        content_layout.addLayout(right_layout, 2)

        main_layout.addLayout(content_layout)

        # 底部按钮
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # 填充列表
        self._populate_list()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
                color: #eaeaea;
            }
            QLabel {
                color: #aaa;
            }
            QLineEdit, QPlainTextEdit {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 6px;
                color: #eaeaea;
            }
            QLineEdit:focus, QPlainTextEdit:focus {
                border-color: #667eea;
            }
            QListWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                color: #eaeaea;
            }
            QListWidget::item:selected {
                background-color: #667eea;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                color: #eaeaea;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
        """)

    def _populate_list(self):
        self.preset_list.clear()
        for preset in self.presets:
            item = QListWidgetItem(preset.get('name', t("preset.unnamed")))
            item.setData(Qt.ItemDataRole.UserRole, preset.get('_id'))  # 只存储 ID（避免拖拽时数据丢失）
            self.preset_list.addItem(item)
        if self.presets:
            self.preset_list.setCurrentRow(0)

    def _on_selection_changed(self, row):
        if self._closing:
            return
        if 0 <= row < self.preset_list.count():
            item = self.preset_list.item(row)
            pid = item.data(Qt.ItemDataRole.UserRole) if item else None
            preset = self._get_preset_by_id(pid) if pid else None
            if preset:
                self.name_edit.blockSignals(True)
                self.commands_edit.blockSignals(True)
                self.name_edit.setText(preset.get('name', ''))
                self.commands_edit.setPlainText('\n'.join(preset.get('commands', [])))
                self.name_edit.blockSignals(False)
                self.commands_edit.blockSignals(False)
                return
        # 清空时也阻止信号，避免触发不必要的更新
        self.name_edit.blockSignals(True)
        self.commands_edit.blockSignals(True)
        self.name_edit.clear()
        self.commands_edit.clear()
        self.name_edit.blockSignals(False)
        self.commands_edit.blockSignals(False)

    def _on_name_changed(self, text):
        if self._closing:
            return
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            preset['name'] = text
            item.setText(text)

    def _on_commands_changed(self):
        if self._closing:
            return
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            text = self.commands_edit.toPlainText()
            commands = [line for line in text.split('\n') if line.strip()]
            preset['commands'] = commands

    def _add_preset(self):
        pid = self._next_preset_id()
        new_preset = {
            '_id': pid,
            'name': t("preset.default_name", n=len(self.presets) + 1),
            'commands': [get_default_shell()]
        }
        self.presets.append(new_preset)
        self._preset_map[pid] = new_preset
        item = QListWidgetItem(new_preset['name'])
        item.setData(Qt.ItemDataRole.UserRole, pid)  # 只存储 ID
        self.preset_list.addItem(item)
        self.preset_list.setCurrentRow(len(self.presets) - 1)

    def _delete_preset(self):
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset and preset in self.presets:
            self.presets.remove(preset)
            if pid in self._preset_map:
                del self._preset_map[pid]
            self.preset_list.takeItem(row)

    def _save_current_preset(self):
        """保存当前编辑的预设"""
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            # 确保当前编辑已同步到 preset
            preset['name'] = self.name_edit.text()
            text = self.commands_edit.toPlainText()
            commands = [line for line in text.split('\n') if line.strip()]
            preset['commands'] = commands
            # 更新列表显示
            item.setText(preset['name'])
        else:
            QMessageBox.warning(self, t("preset.cannot_save"), t("preset.select_first"))

    def _on_rows_moved(self, parent, start, end, destination, row):
        """拖拽排序后同步 presets 列表顺序"""
        if self._closing:
            return
        # 根据 item 的新顺序重新排列 presets 列表
        new_presets = []
        for i in range(self.preset_list.count()):
            item = self.preset_list.item(i)
            pid = item.data(Qt.ItemDataRole.UserRole)
            preset = self._get_preset_by_id(pid) if pid else None
            if preset:
                new_presets.append(preset)
        self.presets = new_presets

    def accept(self):
        """重写 accept 方法，确保当前编辑在关闭前被保存"""
        # 设置关闭标志，阻止信号处理
        self._closing = True

        # 先保存当前正在编辑的预设
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            preset['name'] = self.name_edit.text()
            text = self.commands_edit.toPlainText()
            commands = [line for line in text.split('\n') if line.strip()]
            preset['commands'] = commands
        super().accept()

    def get_presets(self):
        # 返回不包含内部 _id 字段的 preset 列表
        result = []
        for p in self.presets:
            preset_copy = {k: v for k, v in p.items() if k != '_id'}
            result.append(preset_copy)
        return result


class LLMConfigDialog(QDialog):
    """LLM API 配置管理对话框"""

    # 默认配置模板
    DEFAULT_CONFIG = {
        'name': t("llm.new_config_name"),
        'api_base': 'https://api.openai.com/v1',
        'api_key': '',
        'model': 'gpt-4',
        'timeout': 30,
        'max_tokens': 4096,
        'temperature': 1.0,
        'top_p': 1.0,
        'proxy': ''
    }

    def __init__(self, configs: list, default_index: int = 0, parent=None):
        super().__init__(parent)
        self.configs = [c.copy() for c in configs]  # 深拷贝
        self.default_index = default_index
        self.setWindowTitle(t("llm.config_title"))
        self.setMinimumSize(800, 550)

        # 使用唯一 ID 来稳定关联 item 和 config
        self._config_id_counter = 0
        self._config_map = {}  # id -> config
        for c in self.configs:
            cid = self._next_config_id()
            c['_id'] = cid
            self._config_map[cid] = c

        # 关闭标志
        self._closing = False
        # API Key 显示状态
        self._api_key_visible = False

        self._setup_ui()
        self._apply_style()

    def _next_config_id(self):
        """生成下一个唯一 ID"""
        self._config_id_counter += 1
        return self._config_id_counter

    def _get_config_by_id(self, cid):
        """通过 ID 获取 config"""
        return self._config_map.get(cid)

    def _setup_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)

        # 内容区域（水平布局）
        content_layout = QHBoxLayout()

        # 左侧：配置列表
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel(t("llm.config_list_label")))

        self.config_list = QListWidget()
        self.config_list.currentRowChanged.connect(self._on_selection_changed)
        # 启用拖拽排序
        self.config_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.config_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.config_list.model().rowsMoved.connect(self._on_rows_moved)
        left_layout.addWidget(self.config_list)

        # 按钮组
        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton(t("llm.new"))
        self.add_btn.clicked.connect(self._add_config)
        self.save_btn = QPushButton(t("llm.save"))
        self.save_btn.clicked.connect(self._save_current_config)
        self.save_btn.setToolTip(t("llm.save_tooltip"))
        self.delete_btn = QPushButton(t("llm.delete"))
        self.delete_btn.clicked.connect(self._delete_config)
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.delete_btn)
        left_layout.addLayout(btn_layout)

        # 设为默认按钮
        self.set_default_btn = QPushButton(t("llm.set_default"))
        self.set_default_btn.setToolTip(t("llm.set_default_tooltip"))
        self.set_default_btn.clicked.connect(self._set_as_default)
        left_layout.addWidget(self.set_default_btn)

        content_layout.addLayout(left_layout, 1)

        # 右侧：编辑区
        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel(t("llm.edit_label")))

        # 使用表单布局
        form_widget = QWidget()
        form_layout = QFormLayout(form_widget)
        form_layout.setSpacing(10)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # 名称
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(t("llm.name_placeholder"))
        self.name_edit.setMinimumWidth(280)
        self.name_edit.textChanged.connect(self._on_name_changed)
        form_layout.addRow(t("llm.name_label"), self.name_edit)

        # API Base URL
        self.api_base_edit = QLineEdit()
        self.api_base_edit.setPlaceholderText("https://api.openai.com/v1")
        self.api_base_edit.setMinimumWidth(280)
        self.api_base_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow("API URL:", self.api_base_edit)

        # API Key (带显示/隐藏切换)
        api_key_layout = QHBoxLayout()
        api_key_layout.setContentsMargins(0, 0, 0, 0)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.api_key_edit.setMinimumWidth(220)
        self.api_key_edit.textChanged.connect(self._on_field_changed)
        api_key_layout.addWidget(self.api_key_edit)

        self.toggle_key_btn = QPushButton(t("llm.show_key"))
        self.toggle_key_btn.setFixedWidth(60)
        self.toggle_key_btn.clicked.connect(self._toggle_api_key_visibility)
        api_key_layout.addWidget(self.toggle_key_btn)

        api_key_widget = QWidget()
        api_key_widget.setLayout(api_key_layout)
        form_layout.addRow("API Key:", api_key_widget)

        # 模型名称
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("gpt-4")
        self.model_edit.setMinimumWidth(280)
        self.model_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow(t("llm.model_label"), self.model_edit)

        # 超时时间
        self.timeout_edit = QLineEdit()
        self.timeout_edit.setPlaceholderText("30")
        self.timeout_edit.setMaximumWidth(100)
        self.timeout_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow(t("llm.timeout_label"), self.timeout_edit)

        # 最大 tokens
        self.max_tokens_edit = QLineEdit()
        self.max_tokens_edit.setPlaceholderText("4096")
        self.max_tokens_edit.setMaximumWidth(100)
        self.max_tokens_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow(t("llm.max_tokens_label"), self.max_tokens_edit)

        # 温度
        self.temperature_edit = QLineEdit()
        self.temperature_edit.setPlaceholderText(t("llm.temperature_placeholder"))
        self.temperature_edit.setMaximumWidth(150)
        self.temperature_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow(t("llm.temperature_label"), self.temperature_edit)

        # Top P
        self.top_p_edit = QLineEdit()
        self.top_p_edit.setPlaceholderText(t("llm.top_p_placeholder"))
        self.top_p_edit.setMaximumWidth(150)
        self.top_p_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow("Top P:", self.top_p_edit)

        # 代理设置
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(t("llm.proxy_placeholder"))
        self.proxy_edit.setMinimumWidth(280)
        self.proxy_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow(t("llm.proxy_label"), self.proxy_edit)

        right_layout.addWidget(form_widget)
        right_layout.addStretch()

        content_layout.addLayout(right_layout, 2)

        main_layout.addLayout(content_layout)

        # 底部按钮
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # 填充列表
        self._populate_list()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
                color: #eaeaea;
            }
            QLabel {
                color: #aaa;
            }
            QLineEdit {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 6px;
                color: #eaeaea;
            }
            QLineEdit:focus {
                border-color: #667eea;
            }
            QListWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                color: #eaeaea;
            }
            QListWidget::item:selected {
                background-color: #667eea;
            }
            QListWidget::item {
                padding: 4px;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                color: #eaeaea;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
        """)

    def _populate_list(self):
        self.config_list.clear()
        for i, config in enumerate(self.configs):
            name = config.get('name', t("llm.unnamed"))
            # 标记默认配置
            if i == self.default_index:
                name = f"★ {name}"
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, config.get('_id'))
            self.config_list.addItem(item)
        if self.configs:
            self.config_list.setCurrentRow(0)

    def _on_selection_changed(self, row):
        if self._closing:
            return
        if 0 <= row < self.config_list.count():
            item = self.config_list.item(row)
            cid = item.data(Qt.ItemDataRole.UserRole) if item else None
            config = self._get_config_by_id(cid) if cid else None
            if config:
                # 阻止信号以避免循环更新
                self._block_signals(True)
                self.name_edit.setText(config.get('name', ''))
                self.api_base_edit.setText(config.get('api_base', ''))
                self.api_key_edit.setText(config.get('api_key', ''))
                self.model_edit.setText(config.get('model', ''))
                self.timeout_edit.setText(str(config.get('timeout', 30)))
                self.max_tokens_edit.setText(str(config.get('max_tokens', 4096)))
                self.temperature_edit.setText(str(config.get('temperature', 1.0)))
                self.top_p_edit.setText(str(config.get('top_p', 1.0)))
                self.proxy_edit.setText(config.get('proxy', ''))
                self._block_signals(False)
                return
        # 清空编辑区
        self._block_signals(True)
        self._clear_edit_fields()
        self._block_signals(False)

    def _block_signals(self, block):
        """阻止或恢复编辑控件的信号"""
        self.name_edit.blockSignals(block)
        self.api_base_edit.blockSignals(block)
        self.api_key_edit.blockSignals(block)
        self.model_edit.blockSignals(block)
        self.timeout_edit.blockSignals(block)
        self.max_tokens_edit.blockSignals(block)
        self.temperature_edit.blockSignals(block)
        self.top_p_edit.blockSignals(block)
        self.proxy_edit.blockSignals(block)

    def _clear_edit_fields(self):
        """清空编辑字段"""
        self.name_edit.clear()
        self.api_base_edit.clear()
        self.api_key_edit.clear()
        self.model_edit.clear()
        self.timeout_edit.clear()
        self.max_tokens_edit.clear()
        self.temperature_edit.clear()
        self.top_p_edit.clear()
        self.proxy_edit.clear()

    def _on_name_changed(self, text):
        if self._closing:
            return
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        config = self._get_config_by_id(cid) if cid else None
        if config:
            config['name'] = text
            # 更新列表显示（保留默认标记）
            display_name = text
            if self.configs.index(config) == self.default_index:
                display_name = f"★ {text}"
            item.setText(display_name)

    def _on_field_changed(self):
        """同步当前编辑的字段到配置"""
        if self._closing:
            return
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        config = self._get_config_by_id(cid) if cid else None
        if config:
            config['api_base'] = self.api_base_edit.text()
            config['api_key'] = self.api_key_edit.text()
            config['model'] = self.model_edit.text()
            # 数值字段需要验证和转换
            try:
                config['timeout'] = int(self.timeout_edit.text()) if self.timeout_edit.text() else 30
            except ValueError:
                config['timeout'] = 30
            try:
                config['max_tokens'] = int(self.max_tokens_edit.text()) if self.max_tokens_edit.text() else 4096
            except ValueError:
                config['max_tokens'] = 4096
            try:
                temp = float(self.temperature_edit.text()) if self.temperature_edit.text() else 1.0
                config['temperature'] = max(0.0, min(2.0, temp))  # 限制范围 0-2
            except ValueError:
                config['temperature'] = 1.0
            try:
                top_p = float(self.top_p_edit.text()) if self.top_p_edit.text() else 1.0
                config['top_p'] = max(0.0, min(1.0, top_p))  # 限制范围 0-1
            except ValueError:
                config['top_p'] = 1.0
            config['proxy'] = self.proxy_edit.text()

    def _toggle_api_key_visibility(self):
        """切换 API Key 的显示/隐藏"""
        self._api_key_visible = not self._api_key_visible
        if self._api_key_visible:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.toggle_key_btn.setText(t("llm.hide_key"))
        else:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_key_btn.setText(t("llm.show_key"))

    def _add_config(self):
        cid = self._next_config_id()
        new_config = self.DEFAULT_CONFIG.copy()
        new_config['_id'] = cid
        new_config['name'] = t("llm.default_name", n=len(self.configs) + 1)
        self.configs.append(new_config)
        self._config_map[cid] = new_config
        item = QListWidgetItem(new_config['name'])
        item.setData(Qt.ItemDataRole.UserRole, cid)
        self.config_list.addItem(item)
        self.config_list.setCurrentRow(len(self.configs) - 1)

    def _delete_config(self):
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        config = self._get_config_by_id(cid) if cid else None
        if config and config in self.configs:
            config_index = self.configs.index(config)
            self.configs.remove(config)
            if cid in self._config_map:
                del self._config_map[cid]
            self.config_list.takeItem(row)
            # 调整默认索引
            if self.default_index == config_index:
                self.default_index = 0 if self.configs else -1
            elif self.default_index > config_index:
                self.default_index -= 1
            # 重新填充列表以更新默认标记
            self._populate_list()

    def _save_current_config(self):
        """保存当前编辑的配置"""
        row = self.config_list.currentRow()
        if row < 0:
            QMessageBox.warning(self, t("llm.cannot_save"), t("llm.select_first"))
            return
        # 验证必要字段
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, t("llm.validation_failed"), t("llm.name_required"))
            return
        if not self.api_base_edit.text().strip():
            QMessageBox.warning(self, t("llm.validation_failed"), t("llm.url_required"))
            return
        # 触发一次字段同步
        self._on_field_changed()
        QMessageBox.information(self, t("llm.save_success"), t("llm.config_saved"))

    def _set_as_default(self):
        """设为默认配置"""
        row = self.config_list.currentRow()
        if row >= 0:
            self.default_index = row
            self._populate_list()
            self.config_list.setCurrentRow(row)

    def _on_rows_moved(self, parent, start, end, destination, row):
        """拖拽排序后同步 configs 列表顺序"""
        if self._closing:
            return
        # 记录当前默认配置的 ID
        default_config_id = None
        if 0 <= self.default_index < len(self.configs):
            default_config_id = self.configs[self.default_index].get('_id')
        # 根据 item 的新顺序重新排列 configs 列表
        new_configs = []
        for i in range(self.config_list.count()):
            item = self.config_list.item(i)
            cid = item.data(Qt.ItemDataRole.UserRole)
            config = self._get_config_by_id(cid) if cid else None
            if config:
                new_configs.append(config)
        self.configs = new_configs
        # 更新默认索引
        if default_config_id:
            for i, c in enumerate(self.configs):
                if c.get('_id') == default_config_id:
                    self.default_index = i
                    break
        # 更新列表显示
        self._populate_list()

    def accept(self):
        """重写 accept 方法，确保当前编辑在关闭前被保存"""
        self._closing = True
        # 保存当前编辑
        self._on_field_changed()
        super().accept()

    def get_configs(self) -> list:
        """获取配置列表（不包含内部 _id 字段）"""
        result = []
        for c in self.configs:
            config_copy = {k: v for k, v in c.items() if k != '_id'}
            result.append(config_copy)
        return result

    def get_default_index(self) -> int:
        """获取默认配置索引"""
        return self.default_index if self.default_index >= 0 else 0


class DirectoryHistoryDialog(QDialog):
    """快速启动路径管理对话框"""

    def __init__(self, directories: list, parent=None):
        super().__init__(parent)
        self.directories = directories.copy()  # 复制列表
        self.setWindowTitle(t("dirhistory.title"))
        self.setMinimumSize(600, 400)
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)

        # 说明文字
        hint_label = QLabel(t("dirhistory.hint"))
        hint_label.setStyleSheet("color: #888; margin-bottom: 8px;")
        main_layout.addWidget(hint_label)

        # 内容区域（水平布局）
        content_layout = QHBoxLayout()

        # 左侧：目录列表
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel(t("dirhistory.list_label")))

        self.dir_list = QListWidget()
        self.dir_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.dir_list.currentRowChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.dir_list)

        content_layout.addLayout(left_layout, 3)

        # 右侧：操作按钮
        right_layout = QVBoxLayout()
        right_layout.addStretch()

        self.add_btn = QPushButton(t("dirhistory.add"))
        self.add_btn.clicked.connect(self._add_directory)
        right_layout.addWidget(self.add_btn)

        self.delete_btn = QPushButton(t("dirhistory.delete"))
        self.delete_btn.clicked.connect(self._delete_directory)
        right_layout.addWidget(self.delete_btn)

        right_layout.addSpacing(20)

        self.up_btn = QPushButton(t("dirhistory.move_up"))
        self.up_btn.clicked.connect(self._move_up)
        right_layout.addWidget(self.up_btn)

        self.down_btn = QPushButton(t("dirhistory.move_down"))
        self.down_btn.clicked.connect(self._move_down)
        right_layout.addWidget(self.down_btn)

        right_layout.addStretch()

        content_layout.addLayout(right_layout, 1)

        main_layout.addLayout(content_layout)

        # 底部按钮
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # 填充列表
        self._populate_list()
        self._update_buttons()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
                color: #eaeaea;
            }
            QLabel {
                color: #aaa;
            }
            QListWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                color: #eaeaea;
                padding: 4px;
            }
            QListWidget::item {
                padding: 6px;
                border-radius: 3px;
            }
            QListWidget::item:selected {
                background-color: #667eea;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                color: #eaeaea;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
            QPushButton:disabled {
                background-color: #2d2d4c;
                color: #666;
            }
        """)

    def _populate_list(self):
        self.dir_list.clear()
        for dir_path in self.directories:
            # 显示目录名和完整路径
            dir_name = os.path.basename(dir_path) or dir_path
            item = QListWidgetItem(f"📁 {dir_name}")
            item.setToolTip(dir_path)
            item.setData(Qt.ItemDataRole.UserRole, dir_path)
            self.dir_list.addItem(item)
        if self.directories:
            self.dir_list.setCurrentRow(0)

    def _on_selection_changed(self, row):
        self._update_buttons()

    def _update_buttons(self):
        row = self.dir_list.currentRow()
        count = len(self.directories)
        has_selection = row >= 0

        self.delete_btn.setEnabled(has_selection)
        self.up_btn.setEnabled(has_selection and row > 0)
        self.down_btn.setEnabled(has_selection and row < count - 1)

    def _add_directory(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            t("dirhistory.select_dir"),
            str(Path.home())
        )
        if dir_path:
            # 检查是否已存在
            if dir_path in self.directories:
                QMessageBox.information(self, t("dirhistory.already_exists_title"), t("dirhistory.already_exists"))
                # 选中已存在的项
                for i, d in enumerate(self.directories):
                    if d == dir_path:
                        self.dir_list.setCurrentRow(i)
                        break
                return

            # 添加到列表开头
            self.directories.insert(0, dir_path)
            self._populate_list()
            self.dir_list.setCurrentRow(0)

    def _delete_directory(self):
        row = self.dir_list.currentRow()
        if 0 <= row < len(self.directories):
            dir_path = self.directories[row]
            reply = QMessageBox.question(
                self, t("dirhistory.confirm_delete_title"),
                t("dirhistory.confirm_delete_msg", path=dir_path),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.directories.pop(row)
                self.dir_list.takeItem(row)
                # 选中下一项或上一项
                if self.directories:
                    new_row = min(row, len(self.directories) - 1)
                    self.dir_list.setCurrentRow(new_row)
                self._update_buttons()

    def _move_up(self):
        row = self.dir_list.currentRow()
        if row > 0:
            # 交换位置
            self.directories[row], self.directories[row - 1] = \
                self.directories[row - 1], self.directories[row]
            self._populate_list()
            self.dir_list.setCurrentRow(row - 1)

    def _move_down(self):
        row = self.dir_list.currentRow()
        if row < len(self.directories) - 1:
            # 交换位置
            self.directories[row], self.directories[row + 1] = \
                self.directories[row + 1], self.directories[row]
            self._populate_list()
            self.dir_list.setCurrentRow(row + 1)

    def get_directories(self):
        return self.directories

    def accept(self):
        """重写accept方法，关闭前先隐藏防止闪烁"""
        self.setWindowOpacity(0)
        super().accept()

    def reject(self):
        """重写reject方法，关闭前先隐藏防止闪烁"""
        self.setWindowOpacity(0)
        super().reject()


class MainWindow(QMainWindow):
    """主窗口"""

    # 配置文件路径
    CONFIG_FILE = Path(__file__).parent / ".smart_terminal_config.json"

    # 本地快速命令配置
    LOCAL_CONFIG_DIR = ".sterminal"
    LOCAL_COMMANDS_FILE = "quick_commands.json"

    # 主题定义
    THEMES = {
        "深蓝": {
            "name": "深蓝",
            "bg_darkest": "#0f0f1a",
            "bg_dark": "#1a1a2e",
            "bg_medium": "#16213e",
            "bg_light": "#2d2d44",
            "bg_lighter": "#3d3d5c",
            "bg_hover": "#4d4d6c",
            "accent": "#667eea",
            "accent_hover": "#7a8efa",
            "accent_pressed": "#5a6fd6",
            "text": "#eaeaea",
            "text_dim": "#888888",
            "border": "#3d3d5c",
            "success": "#4ade80",
            "success_hover": "#22c55e",
            "danger": "#ef4444",
            "danger_hover": "#dc2626",
            "terminal_bg": "#282c34",
            "terminal_fg": "#abb2bf",
        },
        "暗夜紫": {
            "name": "暗夜紫",
            "bg_darkest": "#13111a",
            "bg_dark": "#1e1a2e",
            "bg_medium": "#2a2440",
            "bg_light": "#3a3350",
            "bg_lighter": "#4a4360",
            "bg_hover": "#5a5370",
            "accent": "#a855f7",
            "accent_hover": "#c084fc",
            "accent_pressed": "#9333ea",
            "text": "#f0e6ff",
            "text_dim": "#9988aa",
            "border": "#4a4360",
            "success": "#4ade80",
            "success_hover": "#22c55e",
            "danger": "#f43f5e",
            "danger_hover": "#e11d48",
            "terminal_bg": "#1a1625",
            "terminal_fg": "#e0d6f0",
        },
        "森林绿": {
            "name": "森林绿",
            "bg_darkest": "#0a1410",
            "bg_dark": "#12201a",
            "bg_medium": "#1a3025",
            "bg_light": "#254035",
            "bg_lighter": "#305545",
            "bg_hover": "#406555",
            "accent": "#22c55e",
            "accent_hover": "#4ade80",
            "accent_pressed": "#16a34a",
            "text": "#e6f4ea",
            "text_dim": "#88aa99",
            "border": "#305545",
            "success": "#4ade80",
            "success_hover": "#86efac",
            "danger": "#ef4444",
            "danger_hover": "#dc2626",
            "terminal_bg": "#0f1a14",
            "terminal_fg": "#c8e6d0",
        },
        "暖橙": {
            "name": "暖橙",
            "bg_darkest": "#1a1008",
            "bg_dark": "#2a1a10",
            "bg_medium": "#3a2818",
            "bg_light": "#4a3828",
            "bg_lighter": "#5a4838",
            "bg_hover": "#6a5848",
            "accent": "#f97316",
            "accent_hover": "#fb923c",
            "accent_pressed": "#ea580c",
            "text": "#fff4e6",
            "text_dim": "#aa9988",
            "border": "#5a4838",
            "success": "#84cc16",
            "success_hover": "#a3e635",
            "danger": "#ef4444",
            "danger_hover": "#dc2626",
            "terminal_bg": "#1f1610",
            "terminal_fg": "#f0e0d0",
        },
        "午夜黑": {
            "name": "午夜黑",
            "bg_darkest": "#000000",
            "bg_dark": "#0a0a0a",
            "bg_medium": "#141414",
            "bg_light": "#1e1e1e",
            "bg_lighter": "#2a2a2a",
            "bg_hover": "#3a3a3a",
            "accent": "#3b82f6",
            "accent_hover": "#60a5fa",
            "accent_pressed": "#2563eb",
            "text": "#f5f5f5",
            "text_dim": "#888888",
            "border": "#2a2a2a",
            "success": "#22c55e",
            "success_hover": "#4ade80",
            "danger": "#ef4444",
            "danger_hover": "#f87171",
            "terminal_bg": "#000000",
            "terminal_fg": "#e0e0e0",
        },
        "浅色": {
            "name": "浅色",
            "bg_darkest": "#e8ebef",      # 主背景 - 浅灰色
            "bg_dark": "#dde1e6",         # 工具栏背景 - 稍深灰
            "bg_medium": "#d0d5dc",       # 输入框/状态栏背景 - 中等灰
            "bg_light": "#c1c7cf",        # 边框高亮
            "bg_lighter": "#b0b8c2",      # 装饰元素
            "bg_hover": "#c8cdd4",        # 悬停状态
            "accent": "#0d6efd",
            "accent_hover": "#0b5ed7",
            "accent_pressed": "#0a58ca",
            "text": "#1a1d21",            # 主文字 - 深色
            "text_dim": "#3d4450",        # 次要文字 - 也要深一点
            "border": "#9aa3af",          # 边框 - 深一点
            "success": "#198754",
            "success_hover": "#157347",
            "danger": "#dc3545",
            "danger_hover": "#bb2d3b",
            "terminal_bg": "#f0f2f5",     # 终端背景 - 非常浅的灰
            "terminal_fg": "#1e1e1e",
            "is_light_theme": True,  # 标记这是浅色主题
            # 浅色主题专用的 ANSI 终端颜色（深色文字）
            "terminal_colors": {
                "black": "#1e1e1e",
                "red": "#c41a16",
                "green": "#007400",
                "brown": "#a85400",
                "yellow": "#a85400",
                "blue": "#0451a5",
                "magenta": "#bc05bc",
                "cyan": "#0598bc",
                "white": "#6e6e6e",
                "default": "#1e1e1e",
            },
            "terminal_bright_colors": {
                "black": "#4e4e4e",
                "red": "#de3124",
                "green": "#00a800",
                "brown": "#cc6600",
                "yellow": "#cc6600",
                "blue": "#2f86d2",
                "magenta": "#d416d4",
                "cyan": "#00a8a8",
                "white": "#3e3e3e",
            },
            "selection_color": (0, 90, 180, 80),  # 深蓝色选区
            "cursor_color": (50, 50, 50, 200),  # 深色光标
        },
        "粉红": {
            "name": "粉红",
            "bg_darkest": "#1a0a14",
            "bg_dark": "#2e1a28",
            "bg_medium": "#3e2838",
            "bg_light": "#4e3848",
            "bg_lighter": "#5e4858",
            "bg_hover": "#6e5868",
            "accent": "#ec4899",
            "accent_hover": "#f472b6",
            "accent_pressed": "#db2777",
            "text": "#fce7f3",
            "text_dim": "#aa8899",
            "border": "#5e4858",
            "success": "#4ade80",
            "success_hover": "#22c55e",
            "danger": "#ef4444",
            "danger_hover": "#dc2626",
            "terminal_bg": "#1f1018",
            "terminal_fg": "#f0d8e8",
        },
    }

    # 预编译正则表达式（用于日志清理）
    _RE_ANSI = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
    _RE_OSC = re.compile(r'\x1b\][^\x07]*\x07')
    _RE_CHARSET = re.compile(r'\x1b[()][AB012]')
    _RE_KEYMODE = re.compile(r'\x1b=|\x1b>')
    _RE_CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

    # 窗口计数器（用于生成唯一窗口标题）
    _window_counter = 0

    # 全局共享的窗口导航面板
    _global_window_navigator = None

    def __init__(self, initial_tab_data=None, window_title=None):
        """初始化主窗口

        Args:
            initial_tab_data: 可选的初始 tab 数据字典，包含:
                - splitter: QSplitter 实例
                - terminals: terminal 列表
                - session: session 实例
                - tab_name: tab 名称
                - cwd: 工作目录（用于拖拽分离的 tab）
            window_title: 自定义窗口标题
        """
        super().__init__()

        # macOS: 确保窗口是独立的原生窗口，能被 Mission Control 识别
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # 关闭时删除
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMinMaxButtonsHint |
            Qt.WindowType.WindowCloseButtonHint |
            Qt.WindowType.WindowTitleHint
        )

        self._initial_tab_data = initial_tab_data  # 保存初始 tab 数据
        self._custom_window_title = window_title  # 自定义窗口标题
        self._macos_window_configured = False  # macOS 原生窗口标志（需在 showEvent 之前初始化）

        self.session_manager = SessionManager()
        self.auto_save_timer = QTimer()
        self.command_history = []
        self.presets = []  # 预设命令列表
        self.local_presets = []  # 本地快速命令列表（目录级别）
        self.pending_commands = []  # 待执行的命令队列
        self.current_theme = "深蓝"  # 当前主题名称
        self._use_icon_tint = False  # 是否给图标添加主题色蒙版
        self._global_zoom_delta = 0  # 全局缩放偏移量（相对于默认字体大小）
        self._gui_font_size = 0  # GUI 字体大小（0 表示跟随全局缩放）
        self._original_widget_styles = {}  # {id(widget): (weakref, original_stylesheet)}
        self._pin_toolbar_row2 = False  # 是否固定显示第二排工具栏

        # 多标签页支持
        self.tab_counter = 0  # 标签页计数器
        self.tab_sessions = {}  # {tab_index: session} 映射
        self.tab_splitters = {}  # {tab_index: QSplitter} 映射
        self.tab_terminals = {}  # {tab_index: [terminal_list]} 映射
        self.tab_cwds = {}  # {tab_index: str} 每个标签页独立的工作目录
        self.active_terminal = None  # 当前活动的终端
        self.detached_windows = []  # 分离出的独立窗口列表

        # 窗口创建时间 (用于排序)
        self._created_time = datetime.now()

        # 窗口颜色标识 (用于快速区分不同窗口)
        self._window_color = "#667eea"  # 默认颜色（与主题一致）
        # 预设颜色列表 - 第一行（常用）
        self.WINDOW_COLORS_PRIMARY = [
            "#667eea",  # 蓝色 (默认)
            "#22c55e",  # 绿色
            "#f59e0b",  # 橙色
            "#ef4444",  # 红色
            "#ec4899",  # 粉色
            "#a855f7",  # 紫色
        ]
        # 扩展颜色列表 - 第二行（更多选择）
        self.WINDOW_COLORS_EXTENDED = [
            "#06b6d4",  # 青色
            "#8b5cf6",  # 紫罗兰
            "#14b8a6",  # 蓝绿色
            "#f97316",  # 深橙色
            "#84cc16",  # 黄绿色
            "#6366f1",  # 靛蓝色
        ]
        # 扩展颜色列表 - 第三行（特殊色）
        self.WINDOW_COLORS_SPECIAL = [
            "#0ea5e9",  # 天蓝色
            "#d946ef",  # 品红色
            "#facc15",  # 金黄色
            "#78716c",  # 灰褐色
            "#fb7185",  # 珊瑚红
            "#38bdf8",  # 浅蓝色
        ]
        # 合并所有颜色（兼容旧代码）
        self.WINDOW_COLORS = self.WINDOW_COLORS_PRIMARY + self.WINDOW_COLORS_EXTENDED + self.WINDOW_COLORS_SPECIAL

        # 窗口级别的工作目录（不使用 os.chdir()，避免影响其他窗口）
        # 如果是从拖拽分离的 tab 创建的窗口，使用 tab 的工作目录
        self._cwd_from_detached_tab = False  # 标志：工作目录是否来自拖拽的 tab
        if initial_tab_data and initial_tab_data.get('cwd'):
            self._window_cwd = initial_tab_data.get('cwd')
            self._cwd_from_detached_tab = True
        else:
            self._window_cwd = os.getcwd()  # 初始化为当前进程的工作目录

        # OpenAI API 服务器管理器
        self.openai_server_manager = OpenAIServerManager()
        self.openai_server_manager.server_started.connect(self._on_openai_server_started)
        self.openai_server_manager.server_stopped.connect(self._on_openai_server_stopped)
        self.openai_server_manager.server_error.connect(self._on_openai_server_error)
        # API 服务器设置：每个 tab 是否启用 "每次 Query 后清除会话"
        self.api_server_clear_after_query: Dict[int, bool] = {}

        # 日志缓冲区（批量更新以提高性能）
        self._log_buffer = []
        self._log_timer = QTimer()
        self._log_timer.timeout.connect(self._flush_log_buffer)
        self._log_timer.start(200)  # 每200ms刷新一次日志（降低频率减少开销）

        # 统计更新节流
        self._stats_dirty = False

        # 加载配置（包括预设）
        self._load_config()

        self._setup_ui()
        self._setup_toolbar()
        self._setup_statusbar()
        self._setup_shortcuts()
        self._connect_signals()

        # 恢复上次的工作目录
        self._restore_last_working_dir()

        # 加载本地快速命令
        self._load_local_commands()

        # 自动保存定时器（会话开始后才启动）
        self.auto_save_timer.timeout.connect(self._auto_save)

        # 初始化完成后，检查是否有正在运行的终端并更新状态
        self._check_initial_running_state()

        # 应用保存的主题
        if hasattr(self, 'theme_combo') and self.current_theme in self.THEMES:
            self.theme_combo.blockSignals(True)
            idx = self.theme_combo.findData(self.current_theme)
            if idx >= 0:
                self.theme_combo.setCurrentIndex(idx)
            self.theme_combo.blockSignals(False)
            self._apply_theme(self.current_theme)

        # 恢复图标蒙版checkbox状态
        if hasattr(self, 'icon_tint_checkbox'):
            self.icon_tint_checkbox.blockSignals(True)
            self.icon_tint_checkbox.setChecked(self._use_icon_tint)
            self.icon_tint_checkbox.blockSignals(False)

        # 恢复 GUI 字体大小 SpinBox
        if hasattr(self, 'gui_font_spin') and self._gui_font_size != 0:
            self.gui_font_spin.blockSignals(True)
            self.gui_font_spin.setValue(self._gui_font_size)
            self.gui_font_spin.blockSignals(False)

        # 恢复固定第二排工具栏 checkbox
        if hasattr(self, 'pin_row2_checkbox') and self._pin_toolbar_row2:
            self.pin_row2_checkbox.blockSignals(True)
            self.pin_row2_checkbox.setChecked(True)
            self.pin_row2_checkbox.blockSignals(False)

        # 恢复全局缩放
        if self._global_zoom_delta != 0 or self._gui_font_size != 0:
            self._apply_global_zoom()

        # 恢复左右分屏偏好
        if hasattr(self, '_explorer_split_checkbox') and self._explorer_split_horizontal:
            self._explorer_split_checkbox.blockSignals(True)
            self._explorer_split_checkbox.setChecked(True)
            self._explorer_split_checkbox.blockSignals(False)

        # 恢复窗口几何（仅主窗口，不用于拖拽分离的 tab 窗口）
        if not initial_tab_data:
            if self._saved_window_geometry:
                self.setGeometry(*self._saved_window_geometry)
            if self._saved_window_maximized:
                self.showMaximized()

        # 恢复面板可见性
        if not initial_tab_data:
            if self._saved_explorer_panel_visible:
                self._toggle_explorer_panel()
            elif self._saved_git_panel_visible:
                self._toggle_git_panel()
            if self._saved_log_panel_visible:
                self._toggle_log_panel()

        # macOS 原生窗口标志（已在 __init__ 开头初始化）

        # 延迟强制固定工具栏可见性（确保 Qt 布局完成后生效）
        if self._pin_toolbar_row2:
            def _force_pinned_rows_visible():
                if not sip.isdeleted(self):
                    self._set_pinned_toolbars_visible(True)
                    self._update_flow_toolbar_height()
            QTimer.singleShot(0, _force_pinned_rows_visible)

    def showEvent(self, event):
        """窗口显示事件 - 设置 macOS 原生窗口属性"""
        super().showEvent(event)
        # 再次强制固定工具栏可见性（show 事件可能重置可见性）
        if self._pin_toolbar_row2:
            self._set_pinned_toolbars_visible(True)
            QTimer.singleShot(0, self._update_flow_toolbar_height)
            # 窗口完全显示后重新布局（此时所有控件已完成渲染，尺寸准确）
            def _post_show_relayout():
                if not sip.isdeleted(self) and self._flow_layout:
                    # 清除缓存的尺寸，强制重新捕获
                    self._flow_layout._item_sizes.clear()
                    # 使布局失效，触发 setGeometry 重新计算
                    self._flow_layout.invalidate()
                    self._pinned_flow_widget.updateGeometry()
                    self._update_flow_toolbar_height()
            QTimer.singleShot(50, _post_show_relayout)
        if not self._macos_window_configured:
            self._macos_window_configured = True
            # 延迟设置，确保窗口在 macOS 中完全注册
            def setup_macos():
                if not sip.isdeleted(self):
                    self._setup_macos_window()
            QTimer.singleShot(100, setup_macos)

        # 立即刷新窗口导航（新窗口建立时）
        if MainWindow._global_window_navigator is not None:
            MainWindow._global_window_navigator._refresh_window_list()

    def changeEvent(self, event):
        """窗口状态变化事件 - 优化窗口切换时的性能"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                # 窗口激活时恢复定时器
                if hasattr(self, '_log_timer') and self._log_timer:
                    self._log_timer.start(200)
                # 恢复所有终端的定时器（错开启动以避免雷鸣群效应）
                if hasattr(self, 'tab_terminals'):
                    delay = 0
                    for terminals in self.tab_terminals.values():
                        for terminal in terminals:
                            # 每个终端延迟 20ms 启动，避免同时触发
                            # 使用闭包捕获终端引用，确保安全访问
                            def resume_terminal_timers(t=terminal):
                                if not sip.isdeleted(t):
                                    t.resume_timers()
                            QTimer.singleShot(delay, resume_terminal_timers)
                            delay += 20
                # 更新窗口导航面板的选中项
                if MainWindow._global_window_navigator is not None:
                    MainWindow._global_window_navigator.select_window(self)
            else:
                # 窗口失活时暂停日志刷新定时器（减少后台开销）
                if hasattr(self, '_log_timer') and self._log_timer:
                    self._log_timer.stop()
                # 暂停所有终端的定时器
                if hasattr(self, 'tab_terminals'):
                    for terminals in self.tab_terminals.values():
                        for terminal in terminals:
                            terminal.pause_timers()

    def _setup_macos_window(self):
        """设置 macOS 原生窗口属性，使其在 Mission Control 中正确显示"""
        if not MACOS_NATIVE_AVAILABLE:
            return

        try:
            window_title = self.windowTitle()

            # 遍历所有 NSApp 窗口，找到匹配的并设置属性
            for ns_window in NSApp.windows():
                try:
                    # 通过标题匹配
                    if ns_window.title() == window_title:
                        self._apply_macos_window_behavior(ns_window)
                        print(f"已设置窗口属性: {window_title}")
                        return
                except:
                    continue

            # 如果没找到匹配的，对所有标准窗口应用设置
            for ns_window in NSApp.windows():
                try:
                    # 跳过没有标题的窗口（可能是系统窗口）
                    if ns_window.title():
                        self._apply_macos_window_behavior(ns_window)
                except:
                    continue

        except Exception as e:
            print(f"设置 macOS 窗口属性失败: {e}")

    def _apply_macos_window_behavior(self, ns_window):
        """应用 macOS 窗口行为设置"""
        if not ns_window:
            return
        try:
            # 设置窗口集合行为：
            # - NSWindowCollectionBehaviorManaged (4): 被 Mission Control 管理
            # - NSWindowCollectionBehaviorParticipatesInCycle (32): 参与 Cmd+` 窗口循环和 Dock 窗口预览
            # - NSWindowCollectionBehaviorFullScreenPrimary (128): 支持全屏
            new_behavior = (
                NSWindowCollectionBehaviorManaged |
                NSWindowCollectionBehaviorParticipatesInCycle |
                NSWindowCollectionBehaviorFullScreenPrimary
            )
            ns_window.setCollectionBehavior_(new_behavior)

            # 确保窗口可以成为 key window 和 main window
            if hasattr(ns_window, 'setCanBecomeKeyWindow_'):
                ns_window.setCanBecomeKeyWindow_(True)
            if hasattr(ns_window, 'setCanBecomeMainWindow_'):
                ns_window.setCanBecomeMainWindow_(True)

        except Exception as e:
            print(f"应用 macOS 窗口行为失败: {e}")

    def _check_initial_running_state(self):
        """检查初始化后是否有正在运行的终端，并更新状态"""
        current_idx = self.tab_widget.currentIndex()
        terminals = self.tab_terminals.get(current_idx, [])
        session = self.tab_sessions.get(current_idx)

        if terminals and any(t.is_running() for t in terminals):
            self.current_session = session
            self._update_running_state(True)

        # 如果没有自定义窗口标题，根据当前 tab 更新窗口标题
        if not self._custom_window_title:
            self._update_window_title_from_tab(current_idx)

    def _setup_ui(self):
        """设置UI"""
        # 设置窗口标题
        if self._custom_window_title:
            self.setWindowTitle(self._custom_window_title)
        else:
            self.setWindowTitle(t("window.title"))
        self.setMinimumSize(1000, 700)

        # 设置窗口图标
        self._icon_path = Path(__file__).parent / "assets" / "smart_terminal.png"
        if self._icon_path.exists():
            self.setWindowIcon(QIcon(str(self._icon_path)))

        # 设置窗口样式
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0f0f1a;
            }
            QToolBar {
                background-color: #1a1a2e;
                border: none;
                padding: 8px;
                spacing: 8px;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 6px;
                padding: 10px 20px;
                color: #eaeaea;
                font-size: 13px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
            QPushButton:pressed {
                background-color: #5d5d7c;
            }
            QPushButton:disabled {
                background-color: #2a2a3a;
                color: #666;
            }
            QPushButton#startBtn {
                background-color: #4ade80;
                color: #000;
            }
            QPushButton#startBtn:hover {
                background-color: #22c55e;
            }
            QPushButton#stopBtn {
                background-color: #ef4444;
            }
            QPushButton#stopBtn:hover {
                background-color: #dc2626;
            }
            QPushButton#logToggleBtn {
                background-color: #667eea;
            }
            QPushButton#logToggleBtn:hover {
                background-color: #7c8eea;
            }
            QStatusBar {
                background-color: #1a1a2e;
                color: #888;
                border-top: 1px solid #2d2d44;
                padding: 5px;
            }
            QLineEdit {
                background-color: #16213e;
                border: 2px solid #3d3d5c;
                border-radius: 6px;
                padding: 8px 12px;
                color: #eaeaea;
                font-size: 14px;
                min-width: 200px;
            }
            QLineEdit:focus {
                border-color: #667eea;
            }
            QLabel {
                color: #eaeaea;
            }
            QTextEdit#logPanel {
                background-color: #1a1a2e;
                color: #98c379;
                border: none;
                font-family: Menlo, Monaco, Consolas, 'DejaVu Sans Mono', 'Liberation Mono', monospace;
                font-size: 11px;
            }
        """)

        # 中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 使用分割器实现可调整的终端和日志面板
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(3)
        self.main_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #3d3d5c;
            }
            QSplitter::handle:hover {
                background-color: #667eea;
            }
        """)

        # 终端标签页容器
        self.tab_widget = QTabWidget()

        # 使用自定义可分离的标签栏
        self.detachable_tab_bar = DetachableTabBar(self.tab_widget)
        self.tab_widget.setTabBar(self.detachable_tab_bar)
        self.detachable_tab_bar.tab_detach_requested.connect(self._detach_tab)

        self.tab_widget.setTabsClosable(False)  # 禁用内置关闭按钮，使用自定义
        self.tab_widget.setMovable(True)
        self.tab_widget.setDocumentMode(True)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        self.tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: none;
                background-color: #1a1a2e;
            }
            QTabBar::tab {
                background-color: #16213e;
                color: #888;
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                min-width: 100px;
            }
            QTabBar::tab:selected {
                background-color: #1a1a2e;
                color: #fff;
            }
            QTabBar::tab:hover:!selected {
                background-color: #1e2a4a;
                color: #aaa;
            }
        """)

        # Tab 右键菜单支持
        self.detachable_tab_bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.detachable_tab_bar.customContextMenuRequested.connect(self._show_tab_context_menu)

        # 添加新标签页按钮容器（避免被裁剪）
        self.new_tab_container = QWidget()
        self.new_tab_container.setAutoFillBackground(True)
        self.new_tab_container.setStyleSheet("background-color: #1a1a2e;")
        new_tab_layout = QHBoxLayout(self.new_tab_container)
        new_tab_layout.setContentsMargins(4, 2, 8, 2)
        new_tab_layout.setSpacing(4)

        # 快速启动按钮 - 点击显示工作目录列表
        self.quick_launch_btn = QPushButton("⚡")
        self.quick_launch_btn.setFixedSize(28, 24)
        self.quick_launch_btn.setToolTip(t("toolbar.quick_launch_tooltip"))
        self.quick_launch_btn.setStyleSheet("""
            QPushButton {
                background-color: #667eea;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                font-size: 14px;
                font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                font-weight: bold;
                padding: 0;
                margin: 0;
            }
            QPushButton:hover {
                background-color: #5a6fd6;
            }
        """)
        self.quick_launch_btn.clicked.connect(self._show_quick_launch_menu)
        new_tab_layout.addWidget(self.quick_launch_btn)

        self.new_tab_btn = QPushButton("+")
        self.new_tab_btn.setFixedSize(28, 24)
        self.new_tab_btn.setToolTip(t("toolbar.new_tab_tooltip"))
        self.new_tab_btn.setStyleSheet("""
            QPushButton {
                background-color: #4ade80;
                color: #000000;
                border: none;
                border-radius: 4px;
                font-size: 18px;
                font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                font-weight: bold;
                padding: 0;
                margin: 0;
            }
            QPushButton:hover {
                background-color: #22c55e;
            }
        """)
        self.new_tab_btn.clicked.connect(self._add_new_tab)
        new_tab_layout.addWidget(self.new_tab_btn)
        self.tab_widget.setCornerWidget(self.new_tab_container, Qt.Corner.TopRightCorner)

        # 创建第一个终端标签页
        if self._initial_tab_data:
            # 使用传入的 tab 数据（包括工作目录）
            self._add_new_tab(
                external_splitter=self._initial_tab_data.get('splitter'),
                external_terminals=self._initial_tab_data.get('terminals'),
                external_session=self._initial_tab_data.get('session'),
                tab_name=self._initial_tab_data.get('tab_name'),
                tab_cwd=self._initial_tab_data.get('cwd')  # 传递工作目录
            )
            self._initial_tab_data = None  # 清除，避免重复使用
        else:
            self._add_new_tab()

        # 左侧面板容器（Git + Extensions）
        self.left_panel_container = QWidget()
        self.left_panel_layout = QVBoxLayout(self.left_panel_container)
        self.left_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.left_panel_layout.setSpacing(0)

        # Explorer 面板容器
        self.explorer_panel_container = QWidget()
        self._setup_explorer_panel()
        self.left_panel_layout.addWidget(self.explorer_panel_container)
        self.explorer_panel_container.hide()
        self.explorer_panel_visible = False

        # Git 面板容器
        self.git_panel_container = QWidget()
        self._setup_git_panel()
        self.left_panel_layout.addWidget(self.git_panel_container)
        self.git_panel_container.hide()
        self.git_panel_visible = False

        self.main_splitter.addWidget(self.left_panel_container)
        self.left_panel_container.hide()  # 默认隐藏

        self.main_splitter.addWidget(self.tab_widget)

        # 原始输出日志面板
        self.log_panel_container = QWidget()
        log_layout = QVBoxLayout(self.log_panel_container)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        # 日志面板标题栏
        self.log_header = QFrame()
        self.log_header.setStyleSheet("""
            QFrame {
                background-color: #16213e;
                border-bottom: 1px solid #3d3d5c;
            }
        """)
        log_header_layout = QHBoxLayout(self.log_header)
        log_header_layout.setContentsMargins(10, 5, 10, 5)

        self.log_title = QLabel(t("log.title"))
        self.log_title.setStyleSheet("color: #667eea; font-weight: bold;")
        log_header_layout.addWidget(self.log_title)

        log_header_layout.addStretch()

        # 清空日志按钮
        self.clear_log_btn = QPushButton(t("log.clear"))
        self.clear_log_btn.setStyleSheet("""
            QPushButton {
                padding: 4px 10px;
                font-size: 11px;
                background-color: #2a2a3a;
            }
        """)
        self.clear_log_btn.clicked.connect(self._clear_log)
        log_header_layout.addWidget(self.clear_log_btn)

        log_layout.addWidget(self.log_header)

        # 日志文本区域
        self.log_text = QTextEdit()
        self.log_text.setObjectName("logPanel")
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        log_layout.addWidget(self.log_text)

        self.main_splitter.addWidget(self.log_panel_container)

        # 设置初始比例（Git 面板隐藏，终端占满，日志隐藏）
        self.main_splitter.setSizes([0, 1000, 0])

        # 日志面板默认隐藏
        self.log_panel_visible = False
        self.log_panel_container.hide()

        # （_setup_toolbar 通过 addToolBar 添加 row2）

        layout.addWidget(self.main_splitter, 1)

        # 底部信息栏
        self.info_frame = QFrame()
        self.info_frame.setStyleSheet("""
            QFrame {
                background-color: #16213e;
                padding: 8px 15px;
            }
            QLabel {
                color: #888;
                font-size: 12px;
            }
        """)
        info_layout = QHBoxLayout(self.info_frame)

        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet("color: #666; font-size: 16px;")
        info_layout.addWidget(self.status_indicator)

        self.session_label = QLabel(t("status.not_started"))
        info_layout.addWidget(self.session_label)

        info_layout.addStretch()

        self.entries_label = QLabel(t("status.entries", n=0))
        info_layout.addWidget(self.entries_label)

        info_layout.addWidget(QLabel("  |  "))

        self.files_label = QLabel(t("status.files", n=0))
        info_layout.addWidget(self.files_label)

        layout.addWidget(self.info_frame)

    def _get_button_order(self, group_name: str) -> list:
        """获取指定分组的按钮顺序"""
        if not self.toolbar_config:
            return None
        button_order = self.toolbar_config.get("button_order", {})
        return button_order.get(group_name)

    def _add_buttons_in_order(self, toolbar, buttons_dict: dict, group_name: str, default_order: list):
        """按配置顺序添加按钮到工具栏"""
        saved_order = self._get_button_order(group_name)
        if saved_order:
            order = saved_order.copy()
            # 添加新按钮（在 default_order 中但不在 saved_order 中的按钮）
            for btn_name in default_order:
                if btn_name not in order and btn_name in buttons_dict:
                    # 找到该按钮在 default_order 中的位置，插入到相应位置
                    idx = default_order.index(btn_name)
                    # 找到合适的插入位置
                    insert_pos = 0
                    for i, existing_btn in enumerate(order):
                        if existing_btn in default_order:
                            existing_idx = default_order.index(existing_btn)
                            if existing_idx < idx:
                                insert_pos = i + 1
                    order.insert(insert_pos, btn_name)
        else:
            order = default_order
        for btn_name in order:
            if btn_name in buttons_dict:
                toolbar.addWidget(buttons_dict[btn_name])

    def _get_effective_group_order(self) -> list:
        """获取有效的分组顺序（含兼容性处理）"""
        from toolbar_manager import ToolbarManagerDialog
        default_groups = ToolbarManagerDialog.DEFAULT_GROUPS

        if not self.toolbar_config:
            return default_groups.copy()

        saved_order = self.toolbar_config.get("group_order", None)
        if not saved_order:
            return default_groups.copy()

        # 兼容性处理：确保所有默认分组都存在
        effective = saved_order.copy()
        for group in default_groups:
            if group not in effective:
                effective.append(group)
        # 移除已不存在的分组
        effective = [g for g in effective if g in default_groups]

        return effective

    def _setup_toolbar(self):
        """设置工具栏"""
        # 检查布局配置
        is_double_row = self._pin_toolbar_row2 or (self.toolbar_config and self.toolbar_config.get("layout") == "double")

        # 创建第一行工具栏
        self.main_toolbar = QToolBar()
        self.main_toolbar.setMovable(False)
        self.main_toolbar.setFloatable(False)
        self.addToolBar(self.main_toolbar)
        self.main_toolbar.toggleViewAction().setVisible(False)  # 禁止右键隐藏
        toolbar = self.main_toolbar  # 保持向后兼容

        # 固定模式下的流式布局工具栏（单个 QToolBar 内嵌 FlowLayout，自动换行）
        self._pinned_flow_toolbar = None  # QToolBar
        self._pinned_flow_widget = None   # QWidget (FlowLayout container)
        self._flow_layout = None          # FlowLayout instance
        self._flow_btn_widgets = {}       # btn_name -> widget (在 flow 中的按钮)
        self._updating_flow_height = False  # 防止 resizeEvent 重入
        self._core_toolbar_widgets = []    # 核心工具栏控件列表（用于 pin 时移到 flow）

        # 标题和颜色指示器
        self.title_label = QLabel(t("toolbar.title_label"))
        self._update_title_label_color()
        toolbar.addWidget(self.title_label)

        # 颜色选择按钮
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(24, 24)
        self.color_btn.setToolTip(t("toolbar.color_tooltip"))
        self.color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_color_btn_style()
        self.color_btn.clicked.connect(self._show_color_picker)
        toolbar.addWidget(self.color_btn)

        toolbar.addSeparator()

        # 预设选择
        self.preset_label = QLabel(t("toolbar.preset_label"))
        self.preset_label.setStyleSheet("color: #888;")
        toolbar.addWidget(self.preset_label)

        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(200)
        self.preset_combo.setStyleSheet("""
            QComboBox {
                background-color: #16213e;
                border: 2px solid #3d3d5c;
                border-radius: 6px;
                padding: 8px 12px;
                padding-right: 36px;
                color: #eaeaea;
                font-size: 14px;
            }
            QComboBox:focus {
                border-color: #667eea;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 32px;
                border: none;
                background: transparent;
            }
            QComboBox::down-arrow {
                image: none;
                width: 18px;
                height: 18px;
                background: transparent;
            }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #eaeaea;
                selection-background-color: #667eea;
                border: 1px solid #3d3d5c;
            }
        """)

        # 添加切换预设按钮
        self.preset_switch_btn = QPushButton(t("toolbar.switch_preset"))
        self.preset_switch_btn.setFixedSize(60, 32)
        self.preset_switch_btn.setToolTip(t("toolbar.switch_preset_tooltip"))
        self.preset_switch_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #667eea, stop:1 #5a6fd6);
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 14px;
                padding: 4px 8px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #7a8efa, stop:1 #667eea);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #5a6fd6, stop:1 #4a5fc6);
            }
        """)
        self.preset_switch_btn.clicked.connect(lambda: self.preset_combo.showPopup())
        self._populate_presets()
        toolbar.addWidget(self.preset_combo)
        toolbar.addWidget(self.preset_switch_btn)

        # 管理预设按钮
        self.manage_preset_btn = QPushButton(t("toolbar.manage_preset"))
        self.manage_preset_btn.setStyleSheet("""
            QPushButton {
                background-color: #3d3d5c;
                padding: 8px 12px;
            }
        """)
        self.manage_preset_btn.clicked.connect(self._manage_presets)
        toolbar.addWidget(self.manage_preset_btn)

        toolbar.addSeparator()

        # 启动按钮
        self.start_btn = QPushButton(t("toolbar.start"))
        self.start_btn.setObjectName("startBtn")
        self.start_btn.clicked.connect(self._start_session)
        toolbar.addWidget(self.start_btn)

        # 停止按钮
        self.stop_btn = QPushButton(t("toolbar.stop"))
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.clicked.connect(self._stop_session)
        self.stop_btn.setEnabled(False)
        toolbar.addWidget(self.stop_btn)

        # 记录核心控件顺序（pin 时移到 flow layout 用）
        # None 表示分隔符位置
        self._core_toolbar_widgets = [
            self.title_label,
            self.color_btn,
            None,  # separator
            self.preset_label,
            self.preset_combo,
            self.preset_switch_btn,
            self.manage_preset_btn,
            None,  # separator
            self.start_btn,
            self.stop_btn,
        ]

        # ===== 创建所有分组按钮（不添加到工具栏）=====

        # --- 选项组 ---
        self.image_prefix_checkbox = QCheckBox(t("toolbar.image_prefix"))
        self.image_prefix_checkbox.setToolTip(t("toolbar.image_prefix_tooltip"))
        self.image_prefix_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaa;
                font-size: 12px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator:unchecked {
                border: 2px solid #3d3d5c;
                border-radius: 3px;
                background-color: #16213e;
            }
            QCheckBox::indicator:checked {
                border: 2px solid #667eea;
                border-radius: 3px;
                background-color: #667eea;
            }
        """)
        self.image_prefix_checkbox.setChecked(self.image_prefix_enabled)
        self.image_prefix_checkbox.stateChanged.connect(self._on_image_prefix_changed)

        self.image_local_checkbox = QCheckBox(t("toolbar.image_local"))
        self.image_local_checkbox.setToolTip(t("toolbar.image_local_tooltip"))
        self.image_local_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaa;
                font-size: 12px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator:unchecked {
                border: 2px solid #3d3d5c;
                border-radius: 3px;
                background-color: #16213e;
            }
            QCheckBox::indicator:checked {
                border: 2px solid #667eea;
                border-radius: 3px;
                background-color: #667eea;
            }
        """)
        self.image_local_checkbox.setChecked(self.image_save_local)
        self.image_local_checkbox.stateChanged.connect(self._on_image_local_changed)

        self.window_nav_checkbox = QCheckBox(t("toolbar.window_nav"))
        self.window_nav_checkbox.setToolTip(t("toolbar.window_nav_tooltip"))
        self.window_nav_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaa;
                font-size: 12px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QCheckBox::indicator:unchecked {
                border: 2px solid #3d3d5c;
                border-radius: 3px;
                background-color: #16213e;
            }
            QCheckBox::indicator:checked {
                border: 2px solid #667eea;
                border-radius: 3px;
                background-color: #667eea;
            }
        """)
        # 同步全局导航面板状态
        if MainWindow._global_window_navigator is not None and MainWindow._global_window_navigator.isVisible():
            self.window_nav_checkbox.setChecked(True)
        self.window_nav_checkbox.stateChanged.connect(self._on_window_nav_changed)

        # --- 操作组 ---
        self.export_btn = QPushButton(t("toolbar.export"))
        self.export_btn.clicked.connect(self._show_export_dialog)

        self.history_btn = QPushButton(t("toolbar.history"))
        self.history_btn.clicked.connect(self._show_history)

        self.clear_btn = QPushButton(t("toolbar.clear"))
        self.clear_btn.clicked.connect(self._clear_terminal)

        # --- 分屏管理组 ---
        self.split_btn = QPushButton(t("toolbar.split"))
        self.split_btn.setToolTip(t("toolbar.split_tooltip"))
        self.split_btn.setStyleSheet("""
            QPushButton {
                background-color: #5a4d7a;
            }
            QPushButton:hover {
                background-color: #6a5d8a;
            }
        """)
        self.split_btn.clicked.connect(self._split_current_tab)

        self.split_v_btn = QPushButton(t("toolbar.split_v"))
        self.split_v_btn.setToolTip(t("toolbar.split_v_tooltip"))
        self.split_v_btn.setStyleSheet("""
            QPushButton {
                background-color: #4d5a7a;
            }
            QPushButton:hover {
                background-color: #5d6a8a;
            }
        """)
        self.split_v_btn.clicked.connect(self._split_vertical_current_terminal)

        self.close_split_btn = QPushButton(t("toolbar.close_split"))
        self.close_split_btn.setToolTip(t("toolbar.close_split_tooltip"))
        self.close_split_btn.setStyleSheet("""
            QPushButton {
                background-color: #8b5a3a;
            }
            QPushButton:hover {
                background-color: #9b6a4a;
            }
        """)
        self.close_split_btn.clicked.connect(self._close_current_split)

        self.close_tab_btn = QPushButton(t("toolbar.close_tab"))
        self.close_tab_btn.setToolTip(t("toolbar.close_tab_tooltip"))
        self.close_tab_btn.setStyleSheet("""
            QPushButton {
                background-color: #8b3a3a;
            }
            QPushButton:hover {
                background-color: #9b4a4a;
            }
        """)
        self.close_tab_btn.clicked.connect(self._close_current_tab)

        # --- 面板与编辑器组 ---
        self.explorer_toggle_btn = QPushButton(t("toolbar.explorer"))
        self.explorer_toggle_btn.setObjectName("explorerToggleBtn")
        self.explorer_toggle_btn.setCheckable(True)
        self.explorer_toggle_btn.setToolTip(t("toolbar.explorer_tooltip"))
        self.explorer_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #22c55e;
            }
            QPushButton:hover {
                background-color: #4ade80;
            }
            QPushButton:checked {
                background-color: #16a34a;
            }
        """)
        self.explorer_toggle_btn.clicked.connect(self._toggle_explorer_panel)

        self.git_toggle_btn = QPushButton("Git")
        self.git_toggle_btn.setObjectName("gitToggleBtn")
        self.git_toggle_btn.setCheckable(True)
        self.git_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #f97316;
            }
            QPushButton:hover {
                background-color: #fb923c;
            }
            QPushButton:checked {
                background-color: #ea580c;
            }
        """)
        self.git_toggle_btn.clicked.connect(self._toggle_git_panel)

        self.vscode_open_btn = QPushButton("VS Code")
        self.vscode_open_btn.setObjectName("vscodeOpenBtn")
        self.vscode_open_btn.setToolTip(t("toolbar.vscode_tooltip"))
        self.vscode_open_btn.setStyleSheet("""
            QPushButton {
                background-color: #007ACC;
            }
            QPushButton:hover {
                background-color: #1a8ad4;
            }
        """)
        self.vscode_open_btn.clicked.connect(self._open_in_vscode)

        self.cursor_open_btn = QPushButton("Cursor")
        self.cursor_open_btn.setObjectName("cursorOpenBtn")
        self.cursor_open_btn.setToolTip(t("toolbar.cursor_tooltip"))
        self.cursor_open_btn.setStyleSheet("""
            QPushButton {
                background-color: #7c3aed;
            }
            QPushButton:hover {
                background-color: #8b5cf6;
            }
        """)
        self.cursor_open_btn.clicked.connect(self._open_in_cursor)

        self.log_toggle_btn = QPushButton(t("toolbar.log"))
        self.log_toggle_btn.setObjectName("logToggleBtn")
        self.log_toggle_btn.setCheckable(True)
        self.log_toggle_btn.clicked.connect(self._toggle_log_panel)

        # --- 主题组 ---
        self.theme_label = QLabel(t("theme.label"))
        self.theme_label.setStyleSheet("color: #888; margin-left: 5px;")

        self.theme_combo = QComboBox()
        self.theme_combo.setMinimumWidth(80)
        for theme_key in self.THEMES.keys():
            self.theme_combo.addItem(t(f"theme.{theme_key}"), theme_key)
        self.theme_combo.setStyleSheet("""
            QComboBox {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 4px 8px;
                color: #eaeaea;
                min-width: 70px;
            }
            QComboBox:hover {
                border-color: #667eea;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #eaeaea;
                selection-background-color: #667eea;
                border: 1px solid #3d3d5c;
            }
        """)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)

        # --- 语言选择 ---
        self.lang_combo = QComboBox()
        self.lang_combo.setFixedWidth(70)
        self.lang_combo.addItem("中文", "zh")
        self.lang_combo.addItem("English", "en")
        # 设置当前语言
        lang_idx = self.lang_combo.findData(get_language())
        if lang_idx >= 0:
            self.lang_combo.setCurrentIndex(lang_idx)
        self.lang_combo.setStyleSheet("""
            QComboBox {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 2px 4px;
                color: #eaeaea;
            }
            QComboBox:hover {
                border-color: #667eea;
            }
            QComboBox::drop-down {
                border: none;
                width: 18px;
            }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #eaeaea;
                selection-background-color: #667eea;
                border: 1px solid #3d3d5c;
            }
        """)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)

        self.icon_tint_checkbox = QCheckBox(t("toolbar.icon_tint"))
        self.icon_tint_checkbox.setToolTip(t("toolbar.icon_tint_tooltip"))
        self.icon_tint_checkbox.setChecked(self._use_icon_tint)
        self.icon_tint_checkbox.stateChanged.connect(self._on_icon_tint_changed)

        # --- 设置组 ---
        self.llm_config_btn = QPushButton("✨")
        self.llm_config_btn.setObjectName("llmConfigBtn")
        self.llm_config_btn.setToolTip(t("toolbar.llm_config_tooltip"))
        self.llm_config_btn.setFixedSize(42, 32)
        self.llm_config_btn.setStyleSheet("""
            QPushButton {
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 20px;
            }
            QPushButton:hover {
                background-color: #8b5cf6;
            }
        """)
        self.llm_config_btn.clicked.connect(self._show_llm_config)

        # --- GUI 字体大小（容器：标签 + SpinBox） ---
        self.gui_font_container = QWidget()
        gui_font_layout = QHBoxLayout(self.gui_font_container)
        gui_font_layout.setContentsMargins(4, 0, 0, 0)
        gui_font_layout.setSpacing(4)

        self.gui_font_label = QLabel(t("toolbar.gui_font_label"))
        self.gui_font_label.setStyleSheet("color: #888;")
        gui_font_layout.addWidget(self.gui_font_label)

        self.gui_font_spin = QSpinBox()
        self.gui_font_spin.setRange(0, 32)
        self.gui_font_spin.setValue(self._gui_font_size)
        self.gui_font_spin.setSpecialValueText(t("toolbar.gui_font_auto"))
        self.gui_font_spin.setToolTip(t("toolbar.gui_font_tooltip"))
        self.gui_font_spin.setSuffix(" pt")
        self.gui_font_spin.setFixedWidth(90)
        self.gui_font_spin.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.gui_font_spin.setStyleSheet("""
            QSpinBox {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 4px 6px;
                color: #eaeaea;
            }
            QSpinBox:hover {
                border-color: #667eea;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                background-color: #3d3d5c;
                border: none;
                width: 18px;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background-color: #667eea;
            }
            QSpinBox::up-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-bottom: 5px solid #eaeaea;
                width: 0px;
                height: 0px;
            }
            QSpinBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid #eaeaea;
                width: 0px;
                height: 0px;
            }
        """)
        self.gui_font_spin.valueChanged.connect(self._on_gui_font_size_changed)
        gui_font_layout.addWidget(self.gui_font_spin)

        # 固定第二排工具栏 checkbox
        self.pin_row2_checkbox = QCheckBox(t("toolbar.pin_row2"))
        self.pin_row2_checkbox.setToolTip(t("toolbar.pin_row2_tooltip"))
        self.pin_row2_checkbox.setChecked(self._pin_toolbar_row2)
        self.pin_row2_checkbox.setStyleSheet("""
            QCheckBox {
                color: #eaeaea;
                font-size: 11px;
                spacing: 4px;
            }
            QCheckBox:hover {
                color: #667eea;
            }
        """)
        self.pin_row2_checkbox.stateChanged.connect(self._on_pin_row2_changed)

        self.toolbar_settings_btn = QPushButton("⚙")
        self.toolbar_settings_btn.setObjectName("toolbarSettingsBtn")
        self.toolbar_settings_btn.setToolTip(t("toolbar.settings_tooltip"))
        self.toolbar_settings_btn.setFixedSize(32, 32)
        self.toolbar_settings_btn.setStyleSheet("""
            QPushButton {
                background-color: #3d3d5c;
                color: #eaeaea;
                border: none;
                border-radius: 4px;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
        """)
        self.toolbar_settings_btn.clicked.connect(self._show_toolbar_manager)

        # ===== 定义每组的按钮和默认顺序 =====
        self._group_button_dicts = {
            "选项": {
                "image_prefix_checkbox": self.image_prefix_checkbox,
                "image_local_checkbox": self.image_local_checkbox,
                "window_nav_checkbox": self.window_nav_checkbox,
            },
            "操作": {
                "export_btn": self.export_btn,
                "history_btn": self.history_btn,
                "clear_btn": self.clear_btn,
            },
            "分屏管理": {
                "split_btn": self.split_btn,
                "split_v_btn": self.split_v_btn,
                "close_split_btn": self.close_split_btn,
                "close_tab_btn": self.close_tab_btn,
            },
            "面板与编辑器": {
                "explorer_toggle_btn": self.explorer_toggle_btn,
                "git_toggle_btn": self.git_toggle_btn,
                "vscode_open_btn": self.vscode_open_btn,
                "cursor_open_btn": self.cursor_open_btn,
                "log_toggle_btn": self.log_toggle_btn,
            },
            "主题": {
                "theme_combo": self.theme_combo,
                "icon_tint_checkbox": self.icon_tint_checkbox,
            },
            "设置": {
                "llm_config_btn": self.llm_config_btn,
                "lang_combo": self.lang_combo,
                "gui_font_spin": self.gui_font_container,
            },
        }

        self._group_default_orders = {
            "选项": ["image_prefix_checkbox", "image_local_checkbox", "window_nav_checkbox"],
            "操作": ["export_btn", "history_btn", "clear_btn"],
            "分屏管理": ["split_btn", "split_v_btn", "close_split_btn", "close_tab_btn"],
            "面板与编辑器": ["explorer_toggle_btn", "git_toggle_btn", "vscode_open_btn", "cursor_open_btn", "log_toggle_btn"],
            "主题": ["theme_combo", "icon_tint_checkbox"],
            "设置": ["llm_config_btn", "gui_font_spin", "lang_combo"],
        }

        # 主题组的装饰前缀
        self._group_prefix_widgets = {
            "主题": self.theme_label,
        }

        # ===== 应用跨组移动配置 =====
        button_groups = self.toolbar_config.get("button_groups", {}) if self.toolbar_config else {}
        if button_groups:
            # 收集所有按钮 widget 引用
            all_btn_widgets = {}
            for group_dict in self._group_button_dicts.values():
                all_btn_widgets.update(group_dict)

            for btn_name, target_group in button_groups.items():
                if btn_name not in all_btn_widgets:
                    continue
                widget = all_btn_widgets[btn_name]
                # 从原分组移除
                for gname in list(self._group_button_dicts.keys()):
                    if btn_name in self._group_button_dicts[gname]:
                        del self._group_button_dicts[gname][btn_name]
                        if btn_name in self._group_default_orders.get(gname, []):
                            self._group_default_orders[gname].remove(btn_name)
                        break
                # 添加到目标分组
                if target_group not in self._group_button_dicts:
                    self._group_button_dicts[target_group] = {}
                    self._group_default_orders[target_group] = []
                self._group_button_dicts[target_group][btn_name] = widget
                if btn_name not in self._group_default_orders[target_group]:
                    self._group_default_orders[target_group].append(btn_name)

        # ===== 按 group_order 添加按钮到工具栏 =====
        effective_group_order = self._get_effective_group_order()

        for group_name in effective_group_order:
            # "预设与控制"组的核心按钮已在上方直接添加到 toolbar
            if group_name == "预设与控制":
                if group_name in self._group_button_dicts and self._group_button_dicts[group_name]:
                    if not is_double_row:
                        self._add_buttons_in_order(
                            toolbar,
                            self._group_button_dicts[group_name],
                            group_name,
                            self._group_default_orders.get(group_name, [])
                        )
                continue

            if is_double_row:
                pass  # 固定模式：所有分组按钮稍后通过 _populate_pinned_flow 添加到 flow layout
            else:
                toolbar.addSeparator()
                if group_name in self._group_prefix_widgets:
                    toolbar.addWidget(self._group_prefix_widgets[group_name])
                if group_name in self._group_button_dicts:
                    self._add_buttons_in_order(
                        toolbar,
                        self._group_button_dicts[group_name],
                        group_name,
                        self._group_default_orders.get(group_name, [])
                    )

        # Pin 和设置按钮（非固定模式直接加到 main_toolbar）
        if not is_double_row:
            toolbar.addWidget(self.pin_row2_checkbox)
            toolbar.addWidget(self.toolbar_settings_btn)

        # 保存所有工具栏按钮的引用，用于显示/隐藏
        self._toolbar_buttons = {}
        self._toolbar_actions = {}

        button_widgets = {
            "preset_combo": self.preset_combo,
            "preset_switch_btn": self.preset_switch_btn,
            "manage_preset_btn": self.manage_preset_btn,
            "start_btn": self.start_btn,
            "stop_btn": self.stop_btn,
            "image_prefix_checkbox": self.image_prefix_checkbox,
            "image_local_checkbox": self.image_local_checkbox,
            "window_nav_checkbox": self.window_nav_checkbox,
            "export_btn": self.export_btn,
            "history_btn": self.history_btn,
            "clear_btn": self.clear_btn,
            "split_btn": self.split_btn,
            "split_v_btn": self.split_v_btn,
            "close_split_btn": self.close_split_btn,
            "close_tab_btn": self.close_tab_btn,
            "explorer_toggle_btn": self.explorer_toggle_btn,
            "git_toggle_btn": self.git_toggle_btn,
            "vscode_open_btn": self.vscode_open_btn,
            "cursor_open_btn": self.cursor_open_btn,
            "log_toggle_btn": self.log_toggle_btn,
            "theme_combo": self.theme_combo,
            "icon_tint_checkbox": self.icon_tint_checkbox,
            "llm_config_btn": self.llm_config_btn,
            "lang_combo": self.lang_combo,
            "gui_font_spin": self.gui_font_container,
        }

        # 建立 action 映射（仅 main_toolbar 上的按钮）
        for btn_name, widget in button_widgets.items():
            self._toolbar_buttons[btn_name] = widget
            for action in self.main_toolbar.actions():
                if self.main_toolbar.widgetForAction(action) == widget:
                    self._toolbar_actions[btn_name] = action
                    break

        # 创建固定模式的流式布局工具栏
        self._pinned_flow_toolbar = QToolBar()
        self._pinned_flow_toolbar.setMovable(False)
        self._pinned_flow_toolbar.setFloatable(False)
        self._pinned_flow_toolbar.toggleViewAction().setVisible(False)

        self._pinned_flow_widget = QWidget()
        self._pinned_flow_widget.setObjectName("pinnedFlowWidget")
        self._pinned_flow_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._flow_layout = FlowLayout(self._pinned_flow_widget, h_spacing=5, v_spacing=3)
        self._flow_layout.setContentsMargins(5, 2, 5, 2)
        self._pinned_flow_toolbar.addWidget(self._pinned_flow_widget)

        pinned = is_double_row or self._pin_toolbar_row2
        if pinned:
            self._populate_pinned_flow(effective_group_order)

        # flow toolbar 与 main_toolbar 同行（同一时刻只显示其一）
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._pinned_flow_toolbar)
        self._pinned_flow_toolbar.setVisible(pinned)
        if pinned:
            self.main_toolbar.setVisible(False)

        # 为 flow 中的按钮建立 _flow_btn_widgets 映射（全部分组）
        for group_name in effective_group_order:
            if group_name == "预设与控制":
                continue
            if group_name in self._group_button_dicts:
                for btn_name, widget in self._group_button_dicts[group_name].items():
                    self._flow_btn_widgets[btn_name] = widget

        if self.toolbar_config:
            self._apply_toolbar_config(self.toolbar_config)

        # 工作目录工具栏
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)  # 换行
        self.dir_toolbar = QToolBar()
        self.dir_toolbar.setMovable(False)
        self.dir_toolbar.setFloatable(False)
        self.dir_toolbar.setStyleSheet("""
            QToolBar {
                background-color: #16213e;
                border: none;
                padding: 4px 8px;
                spacing: 6px;
            }
        """)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.dir_toolbar)
        self.dir_toolbar.toggleViewAction().setVisible(False)  # 禁止右键隐藏

        # 工作目录标签
        self.dir_label = QLabel(t("dir.label"))
        self.dir_label.setStyleSheet("color: #888; font-size: 12px;")
        self.dir_toolbar.addWidget(self.dir_label)

        # 工作目录下拉框（可编辑）
        self.working_dir_combo = QComboBox()
        self.working_dir_combo.setEditable(True)
        self.working_dir_combo.setMinimumWidth(400)
        self.working_dir_combo.setStyleSheet("""
            QComboBox {
                background-color: #1a1a2e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 4px 8px;
                padding-right: 25px;
                color: #eaeaea;
                font-size: 12px;
            }
            QComboBox:focus {
                border-color: #667eea;
            }
            QComboBox::drop-down {
                border: none;
                width: 25px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1a2e;
                color: #eaeaea;
                selection-background-color: #667eea;
                border: 1px solid #3d3d5c;
            }
            QComboBox QLineEdit {
                background-color: #1a1a2e;
                color: #eaeaea;
                border: none;
                padding: 0;
            }
        """)
        # 添加下拉箭头按钮（历史记录图标）
        self.dir_dropdown_btn = QPushButton("🕘")
        self.dir_dropdown_btn.setFixedSize(32, 28)
        self.dir_dropdown_btn.setToolTip(t("status.dir_history_tooltip"))
        self.dir_dropdown_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #6b5b95, stop:1 #5b4b85);
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 14px;
                padding: 2px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #7b6ba5, stop:1 #6b5b95);
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #5b4b85, stop:1 #4b3b75);
            }
        """)
        self.dir_dropdown_btn.clicked.connect(lambda: self.working_dir_combo.showPopup())
        # 使用自定义 LineEdit，点击时自动全选
        select_all_edit = SelectAllLineEdit()
        self.working_dir_combo.setLineEdit(select_all_edit)
        self._populate_working_dirs()
        # 从下拉列表选择时自动切换目录
        self.working_dir_combo.activated.connect(self._on_working_dir_selected)
        # 按回车时应用目录（手动输入时）
        self.working_dir_combo.lineEdit().returnPressed.connect(self._apply_working_dir)
        self.dir_toolbar.addWidget(self.working_dir_combo)
        self.dir_toolbar.addWidget(self.dir_dropdown_btn)

        # 浏览按钮
        self.browse_btn = QPushButton(t("dir.browse"))
        self.browse_btn.setStyleSheet("""
            QPushButton {
                background-color: #3d3d5c;
                padding: 4px 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
        """)
        self.browse_btn.clicked.connect(self._browse_working_dir)
        self.dir_toolbar.addWidget(self.browse_btn)

        # 应用目录按钮
        self.apply_dir_btn = QPushButton(t("dir.switch"))
        self.apply_dir_btn.setToolTip(t("status.apply_dir_tooltip"))
        self.apply_dir_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d5a27;
                padding: 4px 12px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #3d6a37;
            }
        """)
        self.apply_dir_btn.clicked.connect(self._apply_working_dir)
        self.dir_toolbar.addWidget(self.apply_dir_btn)

        # 当前目录显示
        self.dir_toolbar.addSeparator()
        self.current_dir_label = QLabel(t("dir.current", cwd=self._window_cwd))
        self.current_dir_label.setStyleSheet("color: #667eea; font-size: 11px;")
        self.current_dir_label.setToolTip(self._window_cwd)
        self.dir_toolbar.addWidget(self.current_dir_label)

    def _populate_working_dirs(self):
        """填充工作目录历史到下拉框"""
        self.working_dir_combo.clear()
        for dir_path in self.working_dir_history:
            self.working_dir_combo.addItem(dir_path)
        # 设置当前目录（使用窗口级别的工作目录）
        current_dir = self._window_cwd
        index = self.working_dir_combo.findText(current_dir)
        if index >= 0:
            self.working_dir_combo.setCurrentIndex(index)
        else:
            self.working_dir_combo.setCurrentText(current_dir)

    def _browse_working_dir(self):
        """浏览选择工作目录"""
        current = self.working_dir_combo.currentText() or self._window_cwd
        dir_path = QFileDialog.getExistingDirectory(
            self,
            t("msg.select_working_dir"),
            current,
            QFileDialog.Option.ShowDirsOnly
        )
        if dir_path:
            self._add_to_dir_history(dir_path)
            self.working_dir_combo.setCurrentText(dir_path)
            self._apply_working_dir()

    def _add_to_dir_history(self, dir_path: str):
        """添加目录到历史记录（按使用频率排序）"""
        # 增加使用次数
        self._working_dir_freq[dir_path] = self._working_dir_freq.get(dir_path, 0) + 1
        # 确保在列表中
        if dir_path not in self.working_dir_history:
            self.working_dir_history.append(dir_path)
        # 按频率倒序排列
        self.working_dir_history.sort(key=lambda p: self._working_dir_freq.get(p, 0), reverse=True)
        # 更新下拉框
        self._populate_working_dirs()
        self._save_config()

    def _on_working_dir_selected(self, index: int):
        """从下拉列表选择工作目录时自动切换"""
        if index >= 0:
            self._apply_working_dir()

    def _restore_last_working_dir(self):
        """启动时恢复上次的工作目录（窗口级别，不影响其他窗口）"""
        # 如果是从拖拽分离的 tab 创建的窗口，不恢复上次的工作目录
        # 而是使用 tab 的工作目录（已在 __init__ 中设置）
        if self._cwd_from_detached_tab:
            # 更新 UI 显示当前的工作目录
            self.working_dir_combo.setCurrentText(self._window_cwd)
            self.current_dir_label.setText(t("dir.current", cwd=self._window_cwd))
            self.current_dir_label.setToolTip(self._window_cwd)
            return

        if self.last_working_dir and os.path.isdir(self.last_working_dir):
            # 使用窗口级别的工作目录，不调用 os.chdir()
            self._window_cwd = self.last_working_dir
            self.working_dir_combo.setCurrentText(self.last_working_dir)
            self.current_dir_label.setText(t("dir.current", cwd=self.last_working_dir))
            self.current_dir_label.setToolTip(self.last_working_dir)

    def _apply_working_dir(self):
        """应用选中的工作目录（窗口级别，不影响其他窗口）"""
        dir_path = self.working_dir_combo.currentText().strip()
        if not dir_path:
            return

        # 展开用户目录
        dir_path = os.path.expanduser(dir_path)

        if not os.path.isdir(dir_path):
            self._styled_message_box(QMessageBox.Icon.Warning, t("msg.error"), t("msg.dir_not_found", path=dir_path))
            return

        # 使用窗口级别的工作目录，不调用 os.chdir()
        self._window_cwd = dir_path
        self._add_to_dir_history(dir_path)
        self.current_dir_label.setText(t("dir.current", cwd=dir_path))
        self.current_dir_label.setToolTip(dir_path)
        self.statusbar.showMessage(t("status.switched_dir", path=dir_path), 3000)

        # 更新 Explorer 面板根目录
        if hasattr(self, 'explorer_panel') and self.explorer_panel_visible:
            self.explorer_panel.set_root_path(dir_path)

        # 更新 Git 面板仓库路径
        if hasattr(self, 'git_panel') and self.git_panel_visible:
            self.git_panel.set_repository(dir_path)

        # 更新所有终端的工作目录（用于自动启动时）
        current_tab = self.tab_widget.currentIndex()
        if current_tab in self.tab_terminals:
            for terminal in self.tab_terminals[current_tab]:
                terminal.set_working_dir(dir_path)

        # 加载新目录的本地快速命令
        self._load_local_commands()

    def _update_title_label_color(self):
        """更新标题标签颜色"""
        self.title_label.setStyleSheet(
            f"color: {self._window_color}; font-size: 16px; font-weight: bold;"
        )

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
        menu.setStyleSheet("""
            QMenu {
                background-color: #2d2d44;
                border: 1px solid #3d3d5c;
                border-radius: 10px;
                padding: 12px;
            }
        """)

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

    def _set_window_color(self, color: str, menu: QMenu = None):
        """设置窗口颜色"""
        self._window_color = color
        self._update_title_label_color()
        self._update_color_btn_style()
        if menu:
            menu.close()
        # 触发窗口导航面板立即刷新
        if MainWindow._global_window_navigator:
            MainWindow._global_window_navigator._last_window_info = []  # 使缓存失效
            MainWindow._global_window_navigator._refresh_window_list()  # 立即刷新

    def get_window_color(self) -> str:
        """获取窗口颜色"""
        return self._window_color

    def _get_available_window_color(self) -> str:
        """获取一个当前未被其他窗口使用的颜色

        Returns:
            未使用的颜色，如果所有颜色都被使用则返回默认颜色
        """
        from PyQt6.QtWidgets import QApplication

        # 获取所有主窗口正在使用的颜色
        used_colors = set()
        app = QApplication.instance()
        if app:
            for widget in app.topLevelWidgets():
                if isinstance(widget, MainWindow) and widget.isVisible():
                    used_colors.add(widget.get_window_color())

        # 从颜色列表中找到第一个未使用的颜色
        for color in self.WINDOW_COLORS:
            if color not in used_colors:
                return color

        # 如果所有颜色都被使用，返回默认颜色
        return self.WINDOW_COLORS[0]

    def get_created_time(self):
        """获取窗口创建时间"""
        return self._created_time

    def _setup_statusbar(self):
        """设置状态栏"""
        self.statusbar = QStatusBar()
        self.setStatusBar(self.statusbar)
        self.statusbar.showMessage(t("status.ready"))

    def _setup_shortcuts(self):
        """设置快捷键"""
        # Ctrl+Shift+E 导出（避免和终端Ctrl+E冲突）
        export_action = QAction(self)
        export_action.setShortcut("Ctrl+Shift+E")
        export_action.triggered.connect(self._quick_export)
        self.addAction(export_action)

        # Ctrl+Shift+H 历史
        history_action = QAction(self)
        history_action.setShortcut("Ctrl+Shift+H")
        history_action.triggered.connect(self._show_history)
        self.addAction(history_action)

        # Ctrl+Shift+N 新会话
        new_action = QAction(self)
        new_action.setShortcut("Ctrl+Shift+N")
        new_action.triggered.connect(self._start_session)
        self.addAction(new_action)

        # Ctrl+Shift+D 调试特殊字符
        debug_action = QAction(self)
        debug_action.setShortcut("Ctrl+Shift+D")
        debug_action.triggered.connect(self._debug_special_chars)
        self.addAction(debug_action)

        # 标签页快捷键
        new_tab_action = QAction(self)
        new_tab_action.setShortcut("Ctrl+T")
        new_tab_action.triggered.connect(self._add_new_tab)
        self.addAction(new_tab_action)

        close_tab_action = QAction(self)
        close_tab_action.setShortcut("Ctrl+W")
        close_tab_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        close_tab_action.triggered.connect(self._close_tab_or_window)
        self.addAction(close_tab_action)

        next_tab_action = QAction(self)
        next_tab_action.setShortcut("Ctrl+Tab")
        next_tab_action.triggered.connect(self._next_tab)
        self.addAction(next_tab_action)

        prev_tab_action = QAction(self)
        prev_tab_action.setShortcut("Ctrl+Shift+Tab")
        prev_tab_action.triggered.connect(self._prev_tab)
        self.addAction(prev_tab_action)

        # 分屏快捷键
        split_action = QAction(self)
        split_action.setShortcut("Ctrl+Shift+S")
        split_action.triggered.connect(self._split_current_tab)
        self.addAction(split_action)

        # 关闭分屏快捷键
        close_split_action = QAction(self)
        close_split_action.setShortcut("Ctrl+Shift+X")
        close_split_action.triggered.connect(self._close_current_split)
        self.addAction(close_split_action)

        # 垂直分屏快捷键
        split_v_action = QAction(self)
        split_v_action.setShortcut("Ctrl+Shift+V")
        split_v_action.triggered.connect(self._split_vertical_current_terminal)
        self.addAction(split_v_action)

        # 全局缩放快捷键 (Cmd+= 和 Cmd+Shift+= 都放大, Cmd+- 缩小)
        zoom_in_action1 = QAction(self)
        zoom_in_action1.setShortcut("Ctrl+=")
        zoom_in_action1.triggered.connect(self._global_zoom_in)
        self.addAction(zoom_in_action1)

        zoom_in_action2 = QAction(self)
        zoom_in_action2.setShortcut("Ctrl++")
        zoom_in_action2.triggered.connect(self._global_zoom_in)
        self.addAction(zoom_in_action2)

        zoom_out_action = QAction(self)
        zoom_out_action.setShortcut("Ctrl+-")
        zoom_out_action.triggered.connect(self._global_zoom_out)
        self.addAction(zoom_out_action)

    # ==================== 全局字体缩放 ====================

    def _on_gui_font_size_changed(self, value: int):
        """GUI 字体大小调整（跳过 1-7 的无意义区间）"""
        if 1 <= value <= 7:
            # 从 0(Auto) 向上 → 跳到 8；从 8 向下 → 跳到 0(Auto)
            self.gui_font_spin.blockSignals(True)
            new_val = 8 if value > self._gui_font_size else 0
            self.gui_font_spin.setValue(new_val)
            self.gui_font_spin.blockSignals(False)
            value = new_val
        self._gui_font_size = value
        self._apply_global_zoom()

    def _on_pin_row2_changed(self, state):
        """固定/取消固定第二排工具栏"""
        self._pin_toolbar_row2 = bool(state)
        self._save_config()
        self._relayout_toolbars()

    def _relayout_toolbars(self):
        """运行时重新分配工具栏按钮（固定/取消固定切换）"""
        effective_group_order = self._get_effective_group_order()

        if self._pin_toolbar_row2:
            # === 固定：所有按钮移到 flow layout，隐藏 main_toolbar ===

            # 1. 清空 main_toolbar 的所有 action（防止残留引用）
            for action in list(self.main_toolbar.actions()):
                self.main_toolbar.removeAction(action)

            # 2. 填充 flow layout（核心控件 + 全部分组）
            self._populate_pinned_flow(effective_group_order)

            # 3. 切换可见性
            self.main_toolbar.setVisible(False)
            self._pinned_flow_toolbar.setVisible(True)
            QTimer.singleShot(0, self._update_flow_toolbar_height)

            # 重新应用可见性配置
            if self.toolbar_config:
                self._apply_toolbar_config(self.toolbar_config)
        else:
            # === 取消固定：从 flow layout 移回 main_toolbar ===

            # 1. 清空 flow layout（widgets 变成 orphan）
            self._clear_flow_layout()

            # 2. 隐藏 flow toolbar
            self._pinned_flow_toolbar.setVisible(False)

            # 3. 恢复核心控件到 main_toolbar
            for w in self._core_toolbar_widgets:
                if w is None:
                    self.main_toolbar.addSeparator()
                else:
                    self.main_toolbar.addWidget(w)

            # 4. 按分组顺序将所有组按钮添加回 main_toolbar
            for group_name in effective_group_order:
                if group_name == "预设与控制":
                    continue
                if group_name not in self._group_button_dicts:
                    continue
                buttons_dict = self._group_button_dicts[group_name]
                if not buttons_dict:
                    continue
                self.main_toolbar.addSeparator()
                if group_name in self._group_prefix_widgets:
                    self.main_toolbar.addWidget(self._group_prefix_widgets[group_name])
                saved_order = self._get_button_order(group_name)
                order = saved_order if saved_order else self._group_default_orders.get(group_name, [])
                for btn_name in order:
                    if btn_name in buttons_dict:
                        new_action = self.main_toolbar.addWidget(buttons_dict[btn_name])
                        self._toolbar_actions[btn_name] = new_action

            # 5. pin 和 settings 放最后
            pin_action = self.main_toolbar.addWidget(self.pin_row2_checkbox)
            settings_action = self.main_toolbar.addWidget(self.toolbar_settings_btn)
            self._cleanup_toolbar_separators(self.main_toolbar, pin_action)

            # 6. 显示 main_toolbar
            self.main_toolbar.setVisible(True)

            # 重新应用可见性配置
            if self.toolbar_config:
                self._apply_toolbar_config(self.toolbar_config)

    def _set_pinned_toolbars_visible(self, visible: bool):
        """设置固定流式工具栏的可见性"""
        if self._pinned_flow_toolbar:
            self._pinned_flow_toolbar.setVisible(visible)
            if visible:
                self.main_toolbar.setVisible(False)
                QTimer.singleShot(0, self._update_flow_toolbar_height)

    def _get_button_group(self, btn_name: str) -> str:
        """获取按钮所属分组名"""
        from toolbar_manager import ToolbarManagerDialog
        button_groups = self.toolbar_config.get("button_groups", {}) if self.toolbar_config else {}
        group = button_groups.get(btn_name)
        if not group:
            for name, _, _, g in ToolbarManagerDialog.BUTTON_DEFINITIONS:
                if name == btn_name:
                    group = g
                    break
        return group or ""

    def _cleanup_toolbar_separators(self, toolbar, before_action):
        """清理工具栏中多余的分隔符（开头、结尾、连续）"""
        actions = list(toolbar.actions())
        to_remove = []

        end_idx = len(actions)
        if before_action and before_action in actions:
            end_idx = actions.index(before_action)

        # 标记需要删除的：开头、连续分隔符
        prev_was_sep = True
        for i in range(end_idx):
            if actions[i].isSeparator():
                if prev_was_sep:
                    to_remove.append(actions[i])
                prev_was_sep = True
            else:
                prev_was_sep = False

        # before_action 前面的尾部分隔符
        if before_action and before_action in actions:
            idx = actions.index(before_action)
            while idx > 0 and actions[idx - 1].isSeparator() and actions[idx - 1] not in to_remove:
                to_remove.append(actions[idx - 1])
                idx -= 1

        # 末尾分隔符（无 before_action 时）
        if not before_action:
            for i in range(len(actions) - 1, -1, -1):
                if actions[i].isSeparator() and actions[i] not in to_remove:
                    to_remove.append(actions[i])
                else:
                    break

        for action in to_remove:
            toolbar.removeAction(action)

    def _populate_pinned_flow(self, effective_group_order):
        """将所有按钮添加到 flow layout（核心控件 + 全部分组 + pin/settings）"""
        self._clear_flow_layout()

        # 1. 先添加核心控件（title, color, preset, start/stop 等）
        for w in self._core_toolbar_widgets:
            if w is None:
                # 分隔符
                sep = self._create_flow_separator()
                self._flow_layout.addWidget(sep)
            else:
                w.setParent(self._pinned_flow_widget)
                self._flow_layout.addWidget(w)
                w.show()

        # 2. 按分组顺序添加所有组的按钮
        for group_name in effective_group_order:
            if group_name == "预设与控制":
                continue
            if group_name not in self._group_button_dicts:
                continue
            buttons_dict = self._group_button_dicts[group_name]
            if not buttons_dict:
                continue
            # 每组前加分隔符
            sep = self._create_flow_separator()
            self._flow_layout.addWidget(sep)
            if group_name in self._group_prefix_widgets:
                pw = self._group_prefix_widgets[group_name]
                pw.setParent(self._pinned_flow_widget)
                self._flow_layout.addWidget(pw)
            saved_order = self._get_button_order(group_name)
            order = saved_order if saved_order else self._group_default_orders.get(group_name, [])
            for btn_name in self._group_default_orders.get(group_name, []):
                if btn_name not in order:
                    order.append(btn_name)
            for btn_name in order:
                if btn_name in buttons_dict:
                    w = buttons_dict[btn_name]
                    w.setParent(self._pinned_flow_widget)
                    self._flow_layout.addWidget(w)
                    w.show()

        # 3. pin checkbox 和 settings 按钮放在最后
        sep = self._create_flow_separator()
        self._flow_layout.addWidget(sep)
        self.pin_row2_checkbox.setParent(self._pinned_flow_widget)
        self._flow_layout.addWidget(self.pin_row2_checkbox)
        self.pin_row2_checkbox.show()
        self.toolbar_settings_btn.setParent(self._pinned_flow_widget)
        self._flow_layout.addWidget(self.toolbar_settings_btn)
        self.toolbar_settings_btn.show()

    def _clear_flow_layout(self):
        """清空 flow layout 中的所有 widget（不删除按钮 widget 本身）"""
        if not self._flow_layout:
            return
        while self._flow_layout.count():
            item = self._flow_layout.takeAt(0)
            widget = item.widget()
            if widget:
                # 分隔符是临时创建的，可以销毁
                if widget.objectName() == "_flow_separator":
                    widget.setParent(None)
                    widget.deleteLater()
                else:
                    widget.setParent(None)

    def _create_flow_separator(self):
        """创建流式布局中的垂直分隔线"""
        sep = QFrame(self._pinned_flow_widget)
        sep.setObjectName("_flow_separator")
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setFixedWidth(2)
        sep.setFixedHeight(24)
        sep.setStyleSheet("QFrame { color: #3d3d5c; }")
        sep.show()
        return sep

    def _update_flow_toolbar_height(self):
        """根据 flow layout 计算并设置 flow toolbar 高度"""
        if self._updating_flow_height:
            return
        if not self._pinned_flow_toolbar or not self._pinned_flow_toolbar.isVisible():
            return
        if not self._flow_layout:
            return

        self._updating_flow_height = True
        try:
            width = self._pinned_flow_toolbar.width()
            if width <= 0:
                return
            # 计算 flow layout 需要的高度
            h = self._flow_layout.heightForWidth(width)
            padding = 6
            total_h = h + padding
            if total_h < 10:
                total_h = 36  # 最小高度
            self._pinned_flow_toolbar.setFixedHeight(total_h)
            self._pinned_flow_widget.setMinimumHeight(h)
        finally:
            self._updating_flow_height = False

    def resizeEvent(self, event):
        """窗口大小变化时更新 flow toolbar 高度"""
        super().resizeEvent(event)
        self._update_flow_toolbar_height()

    def _global_zoom_in(self):
        """全局放大字体 — 同步缩放所有区域"""
        self._global_zoom_delta += 1
        self._apply_global_zoom()

    def _global_zoom_out(self):
        """全局缩小字体 — 同步缩放所有区域"""
        self._global_zoom_delta -= 1
        self._apply_global_zoom()

    def _apply_global_zoom(self):
        """应用全局缩放到所有组件"""
        delta = self._global_zoom_delta
        # GUI 字体大小：0 表示跟随全局缩放，>0 表示固定大小
        gui_font_size = self._gui_font_size

        # 1. 所有终端 (默认12pt, 范围8-32) — 始终跟随全局缩放
        for terminals in self.tab_terminals.values():
            for term in terminals:
                target_size = max(8, min(32, 12 + delta))
                if term.term_font.pointSize() != target_size:
                    term.term_font.setPointSize(target_size)
                    term._calculate_char_size()
                    term._update_terminal_size()
                    term.update()

        # GUI 组件字体大小：使用固定值或跟随全局缩放
        def gui_target(default, lo, hi):
            if gui_font_size > 0:
                return max(lo, min(hi, gui_font_size))
            return max(lo, min(hi, default + delta))

        # 2. 全局 GUI 字体（工具栏、标签栏、状态栏等）— 通过缩放样式表中的 font-size
        self._scale_gui_font_sizes(gui_font_size, delta)

        # 3. 文件编辑器 (默认13pt, 范围6-48)
        if hasattr(self, 'file_editor') and self.file_editor is not None:
            target_size = gui_target(13, 6, 48)
            font = self.file_editor.editor.font()
            if font.pointSize() != target_size:
                font.setPointSize(target_size)
                self.file_editor.editor.setFont(font)
                self.file_editor.editor.setTabStopDistance(
                    4 * self.file_editor.editor.fontMetrics().horizontalAdvance(' ')
                )

        # 4. 资源管理器文件树 (默认13pt, 范围8-28)
        if hasattr(self, 'explorer_panel') and self.explorer_panel is not None:
            target_size = gui_target(13, 8, 28)
            if hasattr(self.explorer_panel, 'tree_view'):
                tree = self.explorer_panel.tree_view
                font = tree.font()
                if font.pointSize() != target_size:
                    font.setPointSize(target_size)
                    tree.setFont(font)

        # 5. Git diff 查看器 (默认12pt, 范围6-32)
        if hasattr(self, 'git_panel') and self.git_panel is not None:
            target_size = gui_target(12, 6, 32)
            if hasattr(self.git_panel, 'diff_text'):
                font = self.git_panel.diff_text.font()
                if font.pointSize() != target_size:
                    font.setPointSize(target_size)
                    self.git_panel.diff_text.setFont(font)

        # 保存缩放偏移到配置
        self._save_config()

    def _scale_gui_font_sizes(self, gui_font_size: int, delta: int):
        """缩放所有 GUI 组件样式表中的 font-size 值"""
        # 计算缩放比例：以 12px 为基准
        base_px = 12
        if gui_font_size > 0:
            scale = gui_font_size / base_px
        elif delta != 0:
            scale = (base_px + delta) / base_px
        else:
            # 恢复原始样式
            self._restore_original_styles()
            return

        _font_size_re = re.compile(r'font-size:\s*(\d+)px')

        from PyQt6.QtWidgets import QWidget
        for widget in self.findChildren(QWidget):
            wid = id(widget)
            ss = widget.styleSheet()
            if not ss or 'font-size' not in ss:
                continue

            # 首次遇到：记录原始样式表
            if wid not in self._original_widget_styles:
                self._original_widget_styles[wid] = (widget, ss)
            else:
                # 使用存储的原始样式表作为缩放基准
                _, ss = self._original_widget_styles[wid]

            new_ss = _font_size_re.sub(
                lambda m: f'font-size: {max(7, round(int(m.group(1)) * scale))}px',
                ss
            )
            if new_ss != widget.styleSheet():
                widget.setStyleSheet(new_ss)

        # 清理已删除的 widget
        dead_ids = []
        for wid, (widget, _) in self._original_widget_styles.items():
            try:
                widget.objectName()  # 测试 widget 是否还存在
            except RuntimeError:
                dead_ids.append(wid)
        for wid in dead_ids:
            del self._original_widget_styles[wid]

    def _restore_original_styles(self):
        """恢复所有 widget 的原始样式表"""
        dead_ids = []
        for wid, (widget, original_ss) in self._original_widget_styles.items():
            try:
                if widget.styleSheet() != original_ss:
                    widget.setStyleSheet(original_ss)
            except RuntimeError:
                dead_ids.append(wid)
        for wid in dead_ids:
            del self._original_widget_styles[wid]

    @property
    def terminal(self):
        """获取当前激活的终端"""
        # 优先返回活动终端
        if self.active_terminal and self.active_terminal.isVisible():
            return self.active_terminal
        # 否则返回当前标签页的第一个终端
        idx = self.tab_widget.currentIndex()
        terminals = self.tab_terminals.get(idx, [])
        return terminals[0] if terminals else None

    @property
    def current_session(self):
        """获取当前标签页的会话"""
        idx = self.tab_widget.currentIndex()
        return self.tab_sessions.get(idx)

    @current_session.setter
    def current_session(self, session):
        """设置当前标签页的会话"""
        idx = self.tab_widget.currentIndex()
        self.tab_sessions[idx] = session

    def _show_quick_launch_menu(self):
        """显示快速启动菜单 - 支持搜索过滤"""
        # 创建弹出窗口 - 使用Tool类型以支持键盘焦点
        popup = QDialog(self, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # 关闭时自动删除
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # 支持透明背景，防止圆角闪烁

        # 使用内部容器来承载背景，因为WA_TranslucentBackground会使QDialog背景透明
        container = QWidget(popup)
        container.setStyleSheet("""
            QWidget {
                background-color: #1a1a2e;
                border: 1px solid #3d3d5c;
                border-radius: 8px;
            }
        """)

        popup_layout = QVBoxLayout(popup)
        popup_layout.setContentsMargins(0, 0, 0, 0)
        popup_layout.addWidget(container)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 搜索输入框
        search_input = QLineEdit()
        search_input.setPlaceholderText(t("quick_launch.placeholder"))
        search_input.setMinimumWidth(350)
        search_input.setStyleSheet("""
            QLineEdit {
                background-color: #2a2a4e;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 8px 12px;
                font-size: 14px;
            }
            QLineEdit:focus {
                border-color: #667eea;
            }
        """)
        layout.addWidget(search_input)

        # 目录列表
        list_widget = QListWidget()
        list_widget.setStyleSheet("""
            QListWidget {
                background-color: transparent;
                border: none;
                outline: none;
            }
            QListWidget::item {
                color: #eaeaea;
                padding: 8px 12px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #667eea;
            }
            QListWidget::item:hover {
                background-color: #3d3d5c;
            }
        """)
        list_widget.setMaximumHeight(400)
        layout.addWidget(list_widget)

        # 收集所有目录
        current_dir = self._window_cwd
        all_dirs = []

        # 当前目录
        current_dir_name = os.path.basename(current_dir) or current_dir
        all_dirs.append(("current", current_dir, t("quick_launch.current", name=current_dir_name)))

        # 历史目录（不检查 isdir 以避免阻塞，让用户选择时再验证）
        for dir_path in self.working_dir_history:
            if dir_path != current_dir:
                dir_name = os.path.basename(dir_path) or dir_path
                all_dirs.append(("history", dir_path, f"📁 {dir_name}"))

        # 特殊选项
        all_dirs.append(("browse", "", t("quick_launch.browse")))
        all_dirs.append(("manage", "", t("quick_launch.manage_paths")))

        def matches_search(dir_path: str, display: str, keywords: list) -> bool:
            """检查目录是否匹配所有关键词"""
            if not keywords:
                return True
            search_text = (dir_path + " " + display).lower()
            return all(kw in search_text for kw in keywords)

        def update_list(search_text: str = ""):
            """更新列表显示"""
            # 禁用更新以防止闪烁
            list_widget.setUpdatesEnabled(False)
            list_widget.clear()
            keywords = [kw.lower() for kw in search_text.split() if kw]

            for item_type, dir_path, display in all_dirs:
                # 特殊选项始终显示（如果没有搜索词）
                if item_type in ("browse", "manage"):
                    if not keywords:
                        item = QListWidgetItem(display)
                        item.setData(Qt.ItemDataRole.UserRole, (item_type, dir_path))
                        list_widget.addItem(item)
                elif matches_search(dir_path, display, keywords):
                    item = QListWidgetItem(display)
                    item.setData(Qt.ItemDataRole.UserRole, (item_type, dir_path))
                    item.setToolTip(dir_path)
                    list_widget.addItem(item)

            # 如果有匹配项，选中第一个
            if list_widget.count() > 0:
                list_widget.setCurrentRow(0)
            # 重新启用更新
            list_widget.setUpdatesEnabled(True)

        def on_item_activated(item):
            """处理选项激活"""
            item_type, dir_path = item.data(Qt.ItemDataRole.UserRole)
            # 先隐藏弹出窗口防止关闭时闪烁
            popup.setWindowOpacity(0)
            popup.close()

            if item_type == "browse":
                self._quick_launch_browse()
            elif item_type == "manage":
                self._manage_quick_launch_dirs()
            else:
                self._quick_launch_with_dir(dir_path)

        def do_launch():
            """执行启动操作"""
            text = search_input.text().strip()

            # 如果输入的是路径，直接启动
            # 支持: Unix路径(/xxx, ~/xxx), Windows路径(C:\xxx, \\server)
            is_path = False
            if text:
                # Unix路径：包含 / 或以 ~ 开头
                if '/' in text or text.startswith('~'):
                    is_path = True
                # Windows路径：包含 \ 或以驱动器字母开头(如 C:)
                elif '\\' in text or (len(text) >= 2 and text[1] == ':' and text[0].isalpha()):
                    is_path = True
            if is_path:
                # 先隐藏弹出窗口防止关闭时闪烁
                popup.setWindowOpacity(0)
                popup.close()
                self._quick_launch_with_dir(text)
                return

            # 否则激活当前选中的列表项
            current_item = list_widget.currentItem()
            if current_item:
                on_item_activated(current_item)

        # 安装事件过滤器处理键盘导航和窗口失焦
        popup_ready = [False]  # 用列表包装以便在闭包中修改

        class PopupEventFilter(QObject):
            def eventFilter(self, obj, event):
                # 处理窗口失去激活状态时关闭（仅在popup准备好后）
                if event.type() == QEvent.Type.WindowDeactivate and popup_ready[0]:
                    popup.setWindowOpacity(0)  # 先隐藏防止闪烁
                    popup.close()
                    return True

                if event.type() == QEvent.Type.KeyPress:
                    key = event.key()

                    # 上下键导航
                    if key == Qt.Key.Key_Down:
                        current_row = list_widget.currentRow()
                        if current_row < list_widget.count() - 1:
                            list_widget.setCurrentRow(current_row + 1)
                        return True
                    elif key == Qt.Key.Key_Up:
                        current_row = list_widget.currentRow()
                        if current_row > 0:
                            list_widget.setCurrentRow(current_row - 1)
                        return True
                    elif key == Qt.Key.Key_Escape:
                        popup.setWindowOpacity(0)  # 先隐藏防止闪烁
                        popup.close()
                        return True

                return False

        popup_filter = PopupEventFilter(popup)
        popup.installEventFilter(popup_filter)
        search_input.installEventFilter(popup_filter)

        # 连接信号
        search_input.textChanged.connect(update_list)
        search_input.returnPressed.connect(do_launch)  # 回车键执行（IME处理完成后才触发）
        list_widget.itemClicked.connect(on_item_activated)  # 单击执行

        # 初始化列表
        update_list()

        # 显示弹出窗口 - 确保在主窗口内部
        # 先隐藏窗口计算布局，避免在错误位置短暂显示
        popup.setWindowOpacity(0)
        popup.show()  # 需要先 show 才能正确计算大小
        popup.adjustSize()
        popup_size = popup.size()

        # 获取按钮的全局位置
        btn_global_pos = self.quick_launch_btn.mapToGlobal(QPoint(0, self.quick_launch_btn.height()))

        # 获取主窗口的几何信息
        window_rect = self.geometry()
        window_global_pos = self.mapToGlobal(QPoint(0, 0))

        # 计算弹出窗口位置，确保在窗口内
        x = btn_global_pos.x()
        y = btn_global_pos.y()

        # 右边界检查：如果超出窗口右边界，向左调整
        right_edge = window_global_pos.x() + window_rect.width()
        if x + popup_size.width() > right_edge:
            x = right_edge - popup_size.width() - 10

        # 下边界检查：如果超出窗口下边界，向上弹出
        bottom_edge = window_global_pos.y() + window_rect.height()
        if y + popup_size.height() > bottom_edge:
            # 在按钮上方显示
            y = btn_global_pos.y() - self.quick_launch_btn.height() - popup_size.height()

        # 确保不超出窗口左边界
        if x < window_global_pos.x():
            x = window_global_pos.x() + 10

        # 移动到正确位置后再显示
        popup.move(x, y)
        popup.setWindowOpacity(1)  # 现在显示窗口
        popup.activateWindow()  # 激活窗口以获取键盘焦点
        popup.raise_()  # 确保在最前面
        search_input.setFocus(Qt.FocusReason.PopupFocusReason)  # 明确设置焦点原因

        # 延迟启用失焦关闭，避免刚打开就触发关闭
        QTimer.singleShot(100, lambda: popup_ready.__setitem__(0, True))

    def _quick_launch_with_dir(self, dir_path: str):
        """以指定目录快速启动新终端标签页并自动启动预设

        注意：不改变当前窗口的工作目录，新tab使用指定目录
        """
        dir_path = os.path.expanduser(dir_path)

        if not os.path.isdir(dir_path):
            self._styled_message_box(QMessageBox.Icon.Warning, t("msg.error"), t("msg.dir_not_found", path=dir_path))
            return

        try:
            # 添加到目录历史
            self._add_to_dir_history(dir_path)

            # 获取目录名作为标签名
            dir_name = os.path.basename(dir_path) or dir_path

            # 创建新标签页，并存储独立的工作目录
            self._add_new_tab(tab_name=dir_name, tab_cwd=dir_path)

            # 自动启动当前预设，传递目标工作目录
            # 使用闭包捕获 dir_path，避免时序问题
            def start_session_delayed(cwd=dir_path):
                if not sip.isdeleted(self):
                    self._start_session(cwd=cwd)
            QTimer.singleShot(100, start_session_delayed)

        except Exception as e:
            self._styled_message_box(QMessageBox.Icon.Warning, t("msg.error"), t("msg.terminal_launch_error", error=str(e)))

    def _quick_launch_browse(self):
        """浏览选择目录并快速启动"""
        current = self.working_dir_combo.currentText() or self._window_cwd
        dir_path = QFileDialog.getExistingDirectory(
            self,
            t("msg.select_working_dir"),
            current
        )
        if dir_path:
            self._quick_launch_with_dir(dir_path)

    def _manage_quick_launch_dirs(self):
        """打开快速启动路径管理对话框"""
        dialog = DirectoryHistoryDialog(self.working_dir_history, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 更新目录历史
            self.working_dir_history = dialog.get_directories()
            # 同步更新下拉框
            self._populate_working_dirs()
            # 保存到配置文件
            self._save_config()

    def _add_new_tab(self, external_splitter=None, external_terminals=None, external_session=None, tab_name=None, tab_cwd=None):
        """添加新的终端标签页

        Args:
            external_splitter: 外部传入的 splitter（用于接收分离的 tab）
            external_terminals: 外部传入的 terminal 列表
            external_session: 外部传入的 session
            tab_name: 自定义标签名
            tab_cwd: 该标签页独立的工作目录
        """
        self.tab_counter += 1
        if tab_name is None:
            tab_name = t("terminal.default_name", n=self.tab_counter)

        if external_splitter and external_terminals:
            # 使用外部传入的 splitter 和 terminals
            splitter = external_splitter
            terminals = external_terminals
            session = external_session

            # 重新设置 parent
            splitter.setParent(self.tab_widget)

            # 重新连接 terminal 信号
            for terminal in terminals:
                # 断开旧连接（如果有的话）
                try:
                    terminal.input_recorded.disconnect()
                    terminal.output_recorded.disconnect()
                    terminal.session_ended.disconnect()
                    terminal.image_pasted.disconnect()
                    terminal.close_tab_requested.disconnect()
                    terminal.new_tab_requested.disconnect()
                    terminal.manage_presets_requested.disconnect()
                    terminal.add_command_requested.disconnect()
                    terminal.manage_local_presets_requested.disconnect()
                    terminal.add_local_command_requested.disconnect()
                    terminal.close_split_requested.disconnect()
                except:
                    pass

                # 重新连接到当前窗口
                terminal.input_recorded.connect(self._on_input)
                terminal.output_recorded.connect(self._on_output)
                terminal.session_ended.connect(lambda t=terminal: self._on_terminal_ended(t))
                terminal.image_pasted.connect(self._on_image_pasted)
                terminal.close_tab_requested.connect(self._close_tab_or_window)
                terminal.new_tab_requested.connect(self._add_new_tab)
                terminal.manage_presets_requested.connect(self._manage_presets)
                terminal.add_command_requested.connect(self._add_new_preset)
                terminal.manage_local_presets_requested.connect(self._manage_local_presets)
                terminal.add_local_command_requested.connect(self._add_new_local_preset)
                terminal.close_split_requested.connect(self._close_current_split)
                terminal.installEventFilter(self)

                # 重新设置快速命令提供者，指向当前窗口的预设
                terminal.quick_commands_provider = lambda: self.presets
                terminal.local_quick_commands_provider = lambda: self.local_presets

                # 确保 terminal 正确显示（修复从其他窗口拖拽后的显示问题）
                terminal.setUpdatesEnabled(True)  # 恢复绘制更新（detach 时会暂停）
                terminal.show()
                terminal.update()
        else:
            # 创建新的分屏容器
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setHandleWidth(2)
            splitter.setStyleSheet("""
                QSplitter::handle {
                    background-color: #3d3d5c;
                }
                QSplitter::handle:hover {
                    background-color: #667eea;
                }
            """)

            # 创建第一个终端
            terminal = self._create_terminal()
            splitter.addWidget(terminal)
            terminals = [terminal]
            session = None

        # 添加到标签页
        idx = self.tab_widget.addTab(splitter, tab_name)
        self.tab_splitters[idx] = splitter
        self.tab_terminals[idx] = terminals
        self.tab_sessions[idx] = session
        self.tab_cwds[idx] = tab_cwd if tab_cwd else self._window_cwd  # 存储独立工作目录

        # 添加自定义关闭按钮到标签页
        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                padding: 0;
                margin: 0;
            }
            QPushButton:hover {
                background-color: #ff6b6b;
            }
        """)
        close_btn.clicked.connect(lambda checked, i=idx: self._close_tab_by_button(i))
        self.tab_widget.tabBar().setTabButton(idx, QTabBar.ButtonPosition.RightSide, close_btn)

        # 切换到新标签页
        self.tab_widget.setCurrentIndex(idx)
        self.active_terminal = terminals[0]
        terminals[0].setFocus()

        # 如果有外部会话且终端正在运行，更新状态
        if external_session and any(t.is_running() for t in terminals):
            self.current_session = external_session
            self._update_running_state(True)

        return idx

    def _close_tab_by_button(self, index):
        """通过按钮关闭标签页（需要找到正确的索引）"""
        # 由于标签页可能被移动，需要找到按钮对应的实际索引
        sender = self.sender()
        for i in range(self.tab_widget.count()):
            if self.tab_widget.tabBar().tabButton(i, QTabBar.ButtonPosition.RightSide) is sender:
                self._close_tab(i)
                return
        # 如果找不到，尝试使用原始索引
        if index < self.tab_widget.count():
            self._close_tab(index)

    def _create_terminal(self) -> TerminalWidget:
        """创建一个新终端并连接信号"""
        terminal = TerminalWidget()
        terminal.image_prefix_enabled = self.image_prefix_enabled
        terminal.image_save_local = self.image_save_local

        # 设置快速命令提供者回调
        terminal.quick_commands_provider = lambda: self.presets

        # 设置本地快速命令提供者回调
        terminal.local_quick_commands_provider = lambda: self.local_presets

        # 应用当前主题颜色
        t = self.THEMES.get(self.current_theme, self.THEMES["深蓝"])
        terminal.bg_color = QColor(t['terminal_bg'])
        terminal.fg_color = QColor(t['terminal_fg'])
        if t.get('is_light_theme'):
            terminal.set_light_theme_colors(
                t.get('terminal_colors'),
                t.get('terminal_bright_colors'),
                t.get('selection_color'),
                t.get('cursor_color')
            )

        # 连接信号
        terminal.input_recorded.connect(self._on_input)
        terminal.output_recorded.connect(self._on_output)
        terminal.session_ended.connect(lambda t=terminal: self._on_terminal_ended(t))
        terminal.image_pasted.connect(self._on_image_pasted)
        terminal.close_tab_requested.connect(self._close_tab_or_window)
        terminal.new_tab_requested.connect(self._add_new_tab)
        terminal.manage_presets_requested.connect(self._manage_presets)
        terminal.add_command_requested.connect(self._add_new_preset)
        terminal.manage_local_presets_requested.connect(self._manage_local_presets)
        terminal.add_local_command_requested.connect(self._add_new_local_preset)
        terminal.close_split_requested.connect(self._close_current_split)

        # 设置工作目录（用于自动启动时）
        terminal.set_working_dir(self._window_cwd)

        # 应用全局缩放偏移
        if self._global_zoom_delta != 0:
            target_size = max(8, min(32, 12 + self._global_zoom_delta))
            terminal.term_font.setPointSize(target_size)
            terminal._calculate_char_size()

        # 安装事件过滤器来监听焦点变化
        terminal.installEventFilter(self)

        return terminal

    def eventFilter(self, obj, event):
        """事件过滤器 - 监听终端焦点变化"""
        # 处理终端焦点变化
        if event.type() == QEvent.Type.FocusIn:
            # 检查是否是终端控件
            if isinstance(obj, TerminalWidget):
                self.active_terminal = obj
        return super().eventFilter(obj, event)

    def _split_current_tab(self):
        """在当前标签页中分屏添加新终端"""
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        splitter = self.tab_splitters.get(idx)
        if not splitter:
            return

        # 获取当前活动终端的工作目录，回退到标签页的工作目录，再回退到窗口级别的工作目录
        current_cwd = None
        if self.active_terminal and self.active_terminal.is_running():
            current_cwd = self.active_terminal.get_cwd()
        if not current_cwd:
            current_cwd = self.tab_cwds.get(idx, self._window_cwd)

        # 创建新终端
        terminal = self._create_terminal()

        # 添加到分屏容器
        splitter.addWidget(terminal)

        # 更新终端列表
        self.tab_terminals[idx].append(terminal)

        # 均分空间
        count = splitter.count()
        total_width = splitter.width()
        sizes = [total_width // count] * count
        splitter.setSizes(sizes)

        # 启动 shell 在当前终端的工作目录
        terminal.start_process([get_default_shell()], cwd=current_cwd)

        # 设置为活动终端
        self.active_terminal = terminal
        terminal.setFocus()

        self.statusbar.showMessage(t("status.split_done", count=count), 3000)

    def _split_vertical_current_terminal(self):
        """对当前选中的终端进行上下分屏"""
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        # 必须有活动终端
        if not self.active_terminal:
            self.statusbar.showMessage(t("msg.no_selected_terminal"), 3000)
            return

        terminals = self.tab_terminals.get(idx, [])
        if self.active_terminal not in terminals:
            self.statusbar.showMessage(t("msg.active_terminal_not_in_tab"), 3000)
            return

        # 获取当前终端的工作目录，回退到标签页的工作目录，再回退到窗口级别的工作目录
        current_cwd = None
        if self.active_terminal.is_running():
            current_cwd = self.active_terminal.get_cwd()
        if not current_cwd:
            current_cwd = self.tab_cwds.get(idx, self._window_cwd)

        # 找到当前终端所在的父 splitter
        parent_widget = self.active_terminal.parent()
        if not isinstance(parent_widget, QSplitter):
            self.statusbar.showMessage(t("msg.cannot_find_container"), 3000)
            return

        parent_splitter = parent_widget
        terminal_index = parent_splitter.indexOf(self.active_terminal)

        # 记录父 splitter 的所有尺寸
        parent_sizes = parent_splitter.sizes()
        original_size = parent_sizes[terminal_index] if terminal_index < len(parent_sizes) else 0

        # 保存对原终端的引用
        original_terminal = self.active_terminal

        # 创建垂直方向的 splitter
        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        vertical_splitter.setHandleWidth(2)
        vertical_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #3d3d5c;
            }
            QSplitter::handle:hover {
                background-color: #667eea;
            }
        """)

        # 关键步骤：先将原终端添加到垂直 splitter
        # 这会自动将原终端从父 splitter 中移除
        vertical_splitter.addWidget(original_terminal)

        # 创建新终端并添加到垂直 splitter
        new_terminal = self._create_terminal()
        vertical_splitter.addWidget(new_terminal)

        # 将垂直 splitter 插入到父 splitter 的原位置
        parent_splitter.insertWidget(terminal_index, vertical_splitter)

        # 恢复父 splitter 的尺寸分配
        if parent_sizes and len(parent_sizes) == parent_splitter.count():
            parent_splitter.setSizes(parent_sizes)

        # 垂直 splitter 内部均分空间（使用高度）
        v_height = vertical_splitter.height() if vertical_splitter.height() > 0 else 400
        half_height = v_height // 2
        vertical_splitter.setSizes([half_height, half_height])

        # 更新终端列表
        self.tab_terminals[idx].append(new_terminal)

        # 启动新终端
        new_terminal.start_process([get_default_shell()], cwd=current_cwd)

        # 设置新终端为活动终端
        self.active_terminal = new_terminal
        new_terminal.setFocus()

        count = len(self.tab_terminals[idx])
        self.statusbar.showMessage(t("status.vsplit_done", count=count), 3000)

    def _close_current_split(self):
        """关闭当前聚焦的分屏终端"""
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        terminals = self.tab_terminals.get(idx, [])
        if len(terminals) <= 1:
            # 只有一个终端时不能关闭，提示用户
            self.statusbar.showMessage(t("msg.cannot_close_only_terminal"), 3000)
            return

        # 找到当前活动的终端
        terminal_to_close = self.active_terminal
        if terminal_to_close not in terminals:
            # 如果活动终端不在当前标签页，关闭最后一个
            terminal_to_close = terminals[-1]

        # 完整清理终端资源
        terminal_to_close.cleanup()

        # 从列表中移除
        terminals.remove(terminal_to_close)

        # 从分屏容器中移除并销毁
        terminal_to_close.setParent(None)
        terminal_to_close.deleteLater()

        # 更新活动终端为剩余的第一个
        if terminals:
            self.active_terminal = terminals[0]
            terminals[0].setFocus()

        # 重新均分空间
        splitter = self.tab_splitters.get(idx)
        if splitter:
            count = splitter.count()
            total_width = splitter.width()
            sizes = [total_width // count] * count
            splitter.setSizes(sizes)

        self.statusbar.showMessage(t("status.close_split_done", count=len(terminals)), 3000)

    def _on_terminal_ended(self, terminal):
        """单个终端进程结束"""
        # 找到对应的标签页
        for idx, terminals in self.tab_terminals.items():
            if terminal in terminals:
                # 如果所有终端都停止了，更新标签页标题
                all_stopped = all(not t.is_running() for t in terminals)
                if all_stopped:
                    self._on_tab_session_ended(terminal)
                break

    def _close_tab(self, index, auto_create_new=True):
        """关闭指定标签页

        Args:
            index: 要关闭的标签页索引
            auto_create_new: 如果关闭后没有标签页了，是否自动创建新的
        """
        # 先停止 OpenAI API 服务器（如果有）
        if self.openai_server_manager.is_running(index):
            self.openai_server_manager.stop_server(index)

        terminals = self.tab_terminals.get(index, [])
        for terminal in terminals:
            # 完整清理终端资源
            terminal.cleanup()

        # 结束会话
        session = self.tab_sessions.get(index)
        if session:
            self.session_manager.end_session()

        # 移除标签页
        self.tab_widget.removeTab(index)

        # 更新映射（重建索引）
        self._rebuild_tab_mappings()

        # 如果没有标签页了，根据参数决定是否创建新的
        if self.tab_widget.count() == 0 and auto_create_new:
            self._add_new_tab()
            # 确保新 tab 的 UI 状态正确（启动按钮可用）
            self._update_running_state(False)

    def _close_current_tab(self):
        """关闭当前标签页"""
        self._close_tab(self.tab_widget.currentIndex())

    def _close_tab_or_window(self):
        """关闭当前分屏/标签页/窗口 (Cmd+W)

        优先级：
        1. 如果当前标签页有多个分屏，关闭当前选中的分屏
        2. 如果只有一个分屏，关闭整个标签页
        3. 如果没有标签页了，关闭窗口
        """
        idx = self.tab_widget.currentIndex()

        if idx >= 0:
            terminals = self.tab_terminals.get(idx, [])
            if len(terminals) > 1:
                # 有多个分屏，关闭当前选中的分屏
                self._close_current_split()
            else:
                # 只有一个分屏，关闭整个标签页
                # 如果这是最后一个标签页且有活动进程，需要二次确认
                if self.tab_widget.count() == 1:
                    # 检查是否有活动进程在运行
                    has_running_process = any(
                        t.is_running() for t in terminals
                    )
                    if has_running_process:
                        reply = QMessageBox.question(
                            self, t("msg.confirm_close_title"),
                            t("msg.confirm_close_last_tab"),
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No  # 默认选择"否"
                        )
                        if reply != QMessageBox.StandardButton.Yes:
                            return

                self._close_tab(idx, auto_create_new=False)
                # 如果关闭后没有标签页了，关闭窗口
                if self.tab_widget.count() == 0:
                    self.close()
        else:
            # 没有标签页了，关闭整个窗口
            self.close()

    def _detach_tab(self, index, global_pos):
        """将标签页分离为独立窗口（创建完整的 MainWindow）"""
        # 至少保留一个标签页
        if self.tab_widget.count() <= 1:
            return

        # 获取标签页标题
        title = self.tab_widget.tabText(index)

        # 获取相关数据
        splitter = self.tab_splitters.get(index)
        terminals = self.tab_terminals.get(index, [])
        session = self.tab_sessions.get(index)
        # 优先使用存储的工作目录（在删除映射之前获取）
        tab_cwd = self.tab_cwds.get(index)

        if not splitter or not terminals:
            return

        # 停止 OpenAI API 服务器（如果有）
        if self.openai_server_manager.is_running(index):
            self.openai_server_manager.stop_server(index)

        # 在移除 tab 前，暂停所有终端的绘制更新，防止过渡期间在零尺寸 widget 上触发 paintEvent 导致 segfault
        for terminal in terminals:
            terminal.setUpdatesEnabled(False)
            terminal._cache_valid = False
            terminal._cache_pixmap = None

        # 从标签页移除（但不销毁内容）
        self.tab_widget.removeTab(index)

        # 清理映射
        if index in self.tab_splitters:
            del self.tab_splitters[index]
        if index in self.tab_terminals:
            del self.tab_terminals[index]
        if index in self.tab_sessions:
            del self.tab_sessions[index]
        if index in self.tab_cwds:
            del self.tab_cwds[index]

        # 重建映射
        self._rebuild_tab_mappings()

        # 如果没有存储的工作目录，尝试从终端获取或使用窗口默认值
        if not tab_cwd:
            if terminals:
                # 尝试从第一个 terminal 获取当前工作目录
                tab_cwd = terminals[0].get_cwd()
            if not tab_cwd:
                # 如果获取不到，使用当前窗口的工作目录
                tab_cwd = self._window_cwd

        # 创建完整的新 MainWindow，传入 tab 数据
        initial_tab_data = {
            'splitter': splitter,
            'terminals': terminals,
            'session': session,
            'tab_name': title,
            'cwd': tab_cwd  # 传递工作目录
        }

        # 生成唯一的窗口标题
        MainWindow._window_counter += 1
        window_title = f"{title} - Smart Terminal #{MainWindow._window_counter}"

        new_window = MainWindow(initial_tab_data=initial_tab_data, window_title=window_title)

        # 自动为新窗口选择一个未使用的颜色，方便区分
        available_color = self._get_available_window_color()
        new_window._set_window_color(available_color)

        # 调整窗口位置让鼠标正好在标题栏上
        # X: 鼠标在窗口内约 200px 处（标题栏中间偏左）
        # Y: 鼠标在标题栏中间（约 15px 处）
        drag_offset_x = 200
        drag_offset_y = 15
        window_x = global_pos.x() - drag_offset_x
        window_y = global_pos.y() - drag_offset_y

        new_window.move(window_x, window_y)
        new_window.show()

        # 激活窗口
        new_window.raise_()
        new_window.activateWindow()

        # 添加到列表以跟踪
        self.detached_windows.append(new_window)

        # 使用 timer 轮询鼠标位置实现拖拽跟随
        # （macOS 上 startSystemMove() 因鼠标按下事件不在新窗口上而导致窗口漂移）
        drag_timer = QTimer()
        drag_timer.setInterval(16)  # ~60fps 平滑跟随
        new_window._detach_drag_timer = drag_timer  # prevent GC

        def _follow_mouse():
            if sip.isdeleted(new_window):
                drag_timer.stop()
                return
            from PyQt6.QtWidgets import QApplication
            from PyQt6.QtGui import QCursor
            buttons = QApplication.mouseButtons()
            if buttons & Qt.MouseButton.LeftButton:
                cursor_pos = QCursor.pos()
                new_window.move(
                    cursor_pos.x() - drag_offset_x,
                    cursor_pos.y() - drag_offset_y
                )
            else:
                # 鼠标释放，停止拖拽跟随
                drag_timer.stop()
                if not sip.isdeleted(new_window) and new_window.isVisible():
                    new_window.raise_()
                    new_window.activateWindow()
                    if new_window.active_terminal:
                        new_window.active_terminal.setFocus()

        drag_timer.timeout.connect(_follow_mouse)
        drag_timer.start()

        # 如果主窗口没有标签页了，创建一个新的
        if self.tab_widget.count() == 0:
            self._add_new_tab()
            self._update_running_state(False)

    def _rebuild_tab_mappings(self):
        """重建标签页映射"""
        new_splitters = {}
        new_terminals = {}
        new_sessions = {}
        new_cwds = {}
        for i in range(self.tab_widget.count()):
            widget = self.tab_widget.widget(i)
            if widget:
                # 找到对应的旧映射
                for old_idx, splitter in self.tab_splitters.items():
                    if splitter is widget:
                        new_splitters[i] = splitter
                        new_terminals[i] = self.tab_terminals.get(old_idx, [])
                        new_sessions[i] = self.tab_sessions.get(old_idx)
                        new_cwds[i] = self.tab_cwds.get(old_idx, self._window_cwd)
                        break
        self.tab_splitters = new_splitters
        self.tab_terminals = new_terminals
        self.tab_sessions = new_sessions
        self.tab_cwds = new_cwds

    def _on_tab_changed(self, index):
        """标签页切换时的回调"""
        terminals = self.tab_terminals.get(index, [])
        if terminals:
            # 设置第一个终端为活动终端
            self.active_terminal = terminals[0]
            terminals[0].setFocus()
            # 更新状态栏 - 检查是否有任何终端在运行
            any_running = any(t.is_running() for t in terminals)
            self._update_running_state(any_running)
            self._update_stats()

        # 更新窗口标题为当前 tab 名称
        self._update_window_title_from_tab(index)

        # 同步导航面板到当前标签页的工作目录
        tab_cwd = self.tab_cwds.get(index)
        if not tab_cwd and terminals:
            tab_cwd = terminals[0].get_cwd()
        if not tab_cwd:
            tab_cwd = getattr(self, '_window_cwd', None)
        if tab_cwd and os.path.isdir(tab_cwd):
            self._window_cwd = tab_cwd
            if hasattr(self, 'current_dir_label'):
                self.current_dir_label.setText(t("dir.current", cwd=tab_cwd))
                self.current_dir_label.setToolTip(tab_cwd)
            if hasattr(self, 'explorer_panel') and self.explorer_panel_visible:
                self.explorer_panel.set_root_path(tab_cwd)
            if hasattr(self, 'git_panel') and self.git_panel_visible:
                self.git_panel.set_repository(tab_cwd)

    def _on_tab_session_ended(self, terminal):
        """某个标签页的会话结束"""
        # 找到对应的标签页索引
        for idx, terminals in self.tab_terminals.items():
            if terminal in terminals:
                # 检查是否所有终端都停止了
                all_stopped = all(not t.is_running() for t in terminals)
                if all_stopped:
                    # 更新标签页标题，保留原名称，添加已停止标记
                    current_title = self.tab_widget.tabText(idx)
                    stopped_mark = t("status.tab_stopped")
                    if stopped_mark not in current_title:
                        self.tab_widget.setTabText(idx, f"{current_title} {stopped_mark}")
                break

        # 如果是当前标签页，更新状态
        if terminal is self.terminal:
            self._on_session_ended()

    def _update_window_title_from_tab(self, index=None):
        """根据当前 tab 更新窗口标题"""
        if index is None:
            index = self.tab_widget.currentIndex()
        if index >= 0:
            tab_name = self.tab_widget.tabText(index)
            # 去掉 stopped 后缀
            stopped_mark = t("status.tab_stopped")
            if f" {stopped_mark}" in tab_name:
                tab_name = tab_name.replace(f" {stopped_mark}", "")
            self.setWindowTitle(f"{tab_name} - Smart Terminal")

    def _next_tab(self):
        """切换到下一个标签页"""
        count = self.tab_widget.count()
        if count > 1:
            current = self.tab_widget.currentIndex()
            self.tab_widget.setCurrentIndex((current + 1) % count)

    def _prev_tab(self):
        """切换到上一个标签页"""
        count = self.tab_widget.count()
        if count > 1:
            current = self.tab_widget.currentIndex()
            self.tab_widget.setCurrentIndex((current - 1) % count)

    def _connect_signals(self):
        """连接信号 - 注意：终端信号在 _add_new_tab 中连接"""
        # 终端信号已在 _add_new_tab 中连接
        pass

    def _start_session(self, cwd=None):
        """启动会话

        Args:
            cwd: 可选的工作目录，如果不指定则使用窗口级别的工作目录
        """
        if not self.terminal:
            return

        # 防止重复点击：检查启动按钮是否被禁用
        if not self.start_btn.isEnabled():
            return

        # 临时禁用启动按钮，防止重复点击
        self.start_btn.setEnabled(False)

        # 如果当前tab有正在运行的会话，自动新建一个tab来启动
        if self.terminal.is_running():
            # 新建tab
            self._add_new_tab()
            # 此时 self.terminal 已经指向新tab的终端

        # 获取选中的预设
        preset_idx = self.preset_combo.currentIndex()
        if preset_idx < 0 or preset_idx >= len(self.presets):
            self._styled_message_box(QMessageBox.Icon.Warning, t("msg.hint"), t("msg.select_or_create_preset"))
            self.start_btn.setEnabled(True)  # 恢复按钮
            return

        preset = self.presets[preset_idx]
        default_shell = get_default_shell()
        commands = preset.get('commands', [default_shell])
        if not commands:
            commands = [default_shell]

        # 第一条命令作为 shell（如 zsh, bash）
        shell_cmd = commands[0]
        # 后续命令需要在 shell 启动后发送
        self.pending_commands = commands[1:] if len(commands) > 1 else []

        # 创建新会话
        self.current_session = self.session_manager.create_session(preset.get('name', shell_cmd))

        # 清空终端
        self.terminal.clear_screen()

        # 使用传入的 cwd 或窗口级别的工作目录
        working_dir = cwd if cwd else self._window_cwd

        # 启动 shell 进程
        self.terminal.start_process(shell_cmd.split(), cwd=working_dir)

        # 更新标签页名称 (预设名-文件夹名)
        tab_idx = self.tab_widget.currentIndex()

        # 更新该标签页的工作目录记录（用于拖拽分离时传递给新窗口）
        self.tab_cwds[tab_idx] = working_dir
        preset_name = preset.get('name', shell_cmd)
        folder_name = os.path.basename(working_dir)
        tab_title = f"{preset_name}-{folder_name}"
        self.tab_widget.setTabText(tab_idx, tab_title)
        # 同步更新窗口标题
        self._update_window_title_from_tab(tab_idx)

        # 更新UI
        self._update_running_state(True)
        self.statusbar.showMessage(t("status.session_started", name=preset_name))

        # 启动自动保存
        self.auto_save_timer.start(30000)

        # 延迟发送后续命令（等待 shell 初始化）
        # 重要：使用闭包绑定当前终端和命令列表，避免快速启动多个 tab 时命令发送到错误的终端
        if self.pending_commands:
            target_terminal = self.terminal
            commands_to_send = self.pending_commands.copy()
            self.pending_commands = []  # 清空，避免干扰其他启动
            self._send_commands_to_terminal(target_terminal, commands_to_send)

        # 延迟重新启用启动按钮（给进程一些时间完全启动）
        def enable_start_btn():
            if not sip.isdeleted(self):
                self.start_btn.setEnabled(True)
        QTimer.singleShot(800, enable_start_btn)

    def _send_commands_to_terminal(self, terminal, commands):
        """向指定终端发送命令（使用闭包绑定终端引用）"""
        if not terminal or not commands or not terminal.is_running():
            return

        def send_next():
            if not commands or not terminal.is_running():
                return
            cmd = commands.pop(0)
            terminal.send_text(cmd + '\n')
            if commands:
                QTimer.singleShot(300, send_next)

        # 延迟 500ms 等待 shell 初始化
        QTimer.singleShot(500, send_next)

    def _stop_session(self):
        """停止会话并关闭当前标签页"""
        if not self.terminal:
            return

        session_id = self.current_session.session_id if self.current_session else None

        self.terminal.stop_process()

        if self.current_session:
            self.session_manager.end_session()

        self._update_running_state(False)
        self.auto_save_timer.stop()

        if session_id:
            self.statusbar.showMessage(t("status.session_stopped_saved", session_id=session_id), 3000)

        # 直接关闭当前标签页
        self._close_current_tab()

    def _on_session_ended(self):
        """会话结束回调"""
        session_id = self.current_session.session_id if self.current_session else None

        if self.current_session:
            self.session_manager.end_session()

        self._update_running_state(False)
        self.auto_save_timer.stop()

        if session_id:
            self.statusbar.showMessage(t("status.process_exited", session_id=session_id))

    def _on_image_pasted(self, file_path: str):
        """文件粘贴回调 - 仅提示，不单独创建记录

        文件路径已作为输入的一部分发送到终端，
        当用户按Enter提交时，add_input会自动从内容中提取文件路径
        支持图片和音频文件
        """
        # 判断文件类型
        audio_extensions = {'.wav', '.mp3', '.m4a', '.ogg', '.flac', '.webm', '.aac'}
        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.m4v', '.mpeg', '.mpg'}
        image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.ico'}
        ext = Path(file_path).suffix.lower()

        if ext in video_extensions:
            self.statusbar.showMessage(t("status.pasted_video", path=file_path), 3000)
        elif ext in audio_extensions:
            self.statusbar.showMessage(t("status.pasted_audio", path=file_path), 3000)
        elif ext in image_extensions:
            self.statusbar.showMessage(t("status.pasted_image", path=file_path), 3000)
        else:
            self.statusbar.showMessage(t("status.pasted_file", path=file_path), 3000)

    def _update_running_state(self, running: bool):
        """更新运行状态UI"""
        # 检查 UI 控件是否已创建（在初始化期间可能还未创建）
        if not hasattr(self, 'start_btn'):
            return

        # 启动按钮始终可用，运行时点击会自动新建tab
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(running)
        # 预设选择和管理始终可用
        self.preset_combo.setEnabled(True)
        self.manage_preset_btn.setEnabled(True)

        if running:
            self.status_indicator.setStyleSheet("color: #4ade80; font-size: 16px;")
            if self.current_session:
                self.session_label.setText(t("status.running_session", session_id=self.current_session.session_id))
            else:
                self.session_label.setText(t("status.running"))
        else:
            self.status_indicator.setStyleSheet("color: #ef4444; font-size: 16px;")  # 红色表示已停止
            self.session_label.setText(t("status.stopped"))
            self.entries_label.setText(t("status.entries", n=0))
            self.files_label.setText(t("status.files", n=0))
            self.statusbar.showMessage(t("status.session_stopped"))
            self.current_session = None

    def _on_input(self, text: str):
        """输入回调"""
        # 使用 session_manager 的状态来判断，避免竞态条件
        if self.session_manager.current_session:
            try:
                self.session_manager.add_input(text)
                self._update_stats()
            except RuntimeError:
                # 会话可能在输入过程中结束，忽略此错误
                pass

    def _on_output(self, text: str):
        """输出回调"""
        # 使用 session_manager 的状态来判断，避免竞态条件
        if self.session_manager.current_session:
            try:
                self.session_manager.add_output(text)
                self._stats_dirty = True  # 标记需要更新统计
            except RuntimeError:
                # 会话可能在输出过程中结束，忽略此错误
                pass

        # 只有日志面板可见时才处理日志（避免不必要的正则表达式操作）
        if not self.log_panel_visible:
            return

        # 清理文本并添加到缓冲区
        clean_text = self._RE_ANSI.sub('', text)
        clean_text = self._RE_OSC.sub('', clean_text)
        clean_text = self._RE_CHARSET.sub('', clean_text)
        clean_text = self._RE_KEYMODE.sub('', clean_text)
        clean_text = self._RE_CTRL.sub('', clean_text)

        if clean_text.strip():
            self._log_buffer.append(clean_text)

    # 日志面板最大字符数（约 500KB，超过时截断前面的内容）
    _MAX_LOG_SIZE = 500000

    def _flush_log_buffer(self):
        """刷新日志缓冲区"""
        # 更新统计信息
        if self._stats_dirty:
            self._stats_dirty = False
            self._update_stats()

        # 刷新日志 - 仅当日志面板可见或有缓冲内容时处理
        if not self._log_buffer:
            return

        # 日志面板隐藏时，只保留最近的缓冲内容，避免内存无限增长
        if not self.log_panel_visible:
            # 限制隐藏时的缓冲区大小（保留最后10条）
            if len(self._log_buffer) > 10:
                self._log_buffer = self._log_buffer[-10:]
            return

        # 合并缓冲区内容
        combined = ''.join(self._log_buffer)
        self._log_buffer.clear()

        # 一次性更新日志面板
        cursor = self.log_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)
        self.log_text.insertPlainText(combined)

        # 限制日志大小，防止内存无限增长（仅在需要时检查）
        doc = self.log_text.document()
        char_count = doc.characterCount()
        if char_count > self._MAX_LOG_SIZE:
            # 保留后半部分，删除前半部分
            cursor = self.log_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(cursor.MoveOperation.Right, cursor.MoveMode.KeepAnchor,
                               char_count - self._MAX_LOG_SIZE // 2)
            cursor.removeSelectedText()

        # 自动滚动到底部
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _update_stats(self):
        """更新统计信息"""
        if self.current_session:
            entry_count = len(self.current_session.entries)
            file_count = len(self.current_session.get_all_files())
            self.entries_label.setText(t("status.entries", n=entry_count))
            self.files_label.setText(t("status.files", n=file_count))

    def _auto_save(self):
        """自动保存"""
        if self.current_session:
            self.session_manager.auto_save()

    def _clear_terminal(self):
        """清屏"""
        if self.terminal:
            self.terminal.clear_screen()

    def _toggle_log_panel(self):
        """切换日志面板显示"""
        self.log_panel_visible = not self.log_panel_visible
        self.main_splitter.setUpdatesEnabled(False)
        if self.log_panel_visible:
            self.log_panel_container.show()
        else:
            self.log_panel_container.hide()
        self._update_splitter_sizes()
        self.main_splitter.setUpdatesEnabled(True)
        QTimer.singleShot(0, self._flush_terminal_resizes)

    def _setup_explorer_panel(self):
        """设置 Explorer 面板"""
        layout = QVBoxLayout(self.explorer_panel_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Explorer 面板标题栏（保存引用以便主题切换时直接访问）
        self._explorer_header = QFrame()
        self._explorer_header.setStyleSheet("""
            QFrame {
                background-color: #16213e;
                border-bottom: 1px solid #3d3d5c;
            }
        """)
        explorer_header_layout = QHBoxLayout(self._explorer_header)
        explorer_header_layout.setContentsMargins(10, 5, 10, 5)

        self._explorer_title = QLabel(t("explorer.title"))
        self._explorer_title.setStyleSheet("color: #22c55e; font-weight: bold;")
        explorer_header_layout.addWidget(self._explorer_title)

        explorer_header_layout.addStretch()

        # 分屏方向切换 checkbox
        self._explorer_split_checkbox = QCheckBox(t("explorer.left_right_split"))
        self._explorer_split_checkbox.setToolTip(t("explorer.split_tooltip"))
        self._explorer_split_checkbox.setStyleSheet("""
            QCheckBox {
                color: #888;
                font-size: 11px;
                spacing: 4px;
            }
            QCheckBox:hover {
                color: #eaeaea;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
            }
            QCheckBox::indicator:unchecked {
                border: 1px solid #3d3d5c;
                border-radius: 2px;
                background-color: #16213e;
            }
            QCheckBox::indicator:checked {
                border: 1px solid #667eea;
                border-radius: 2px;
                background-color: #667eea;
            }
        """)
        if not hasattr(self, '_explorer_split_horizontal'):
            self._explorer_split_horizontal = False  # 默认上下分屏
        self._explorer_split_checkbox.stateChanged.connect(self._on_explorer_split_orientation_changed)
        explorer_header_layout.addWidget(self._explorer_split_checkbox)

        # 刷新按钮
        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedSize(24, 24)
        refresh_btn.setToolTip(t("explorer.refresh_tooltip"))
        refresh_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #888;
                border: none;
                font-size: 14px;
            }
            QPushButton:hover {
                color: #eaeaea;
            }
        """)
        refresh_btn.clicked.connect(lambda: self.explorer_panel.refresh() if hasattr(self, 'explorer_panel') else None)
        explorer_header_layout.addWidget(refresh_btn)

        # 隐藏按钮
        hide_btn = QPushButton("×")
        hide_btn.setFixedSize(24, 24)
        hide_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #888;
                border: none;
                font-size: 16px;
            }
            QPushButton:hover {
                color: #eaeaea;
            }
        """)
        hide_btn.clicked.connect(self._toggle_explorer_panel)
        explorer_header_layout.addWidget(hide_btn)

        layout.addWidget(self._explorer_header)

        # 获取当前主题
        current_theme = self.THEMES.get(self.current_theme, self.THEMES["深蓝"])

        # 使用 QSplitter 分隔资源管理器和编辑器
        self.explorer_splitter = QSplitter(Qt.Orientation.Vertical)
        self.explorer_splitter.setHandleWidth(3)
        self.explorer_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #3d3d5c;
            }
            QSplitter::handle:hover {
                background-color: #667eea;
            }
        """)

        # Explorer 面板内容
        self.explorer_panel = ExplorerPanel(theme=current_theme)
        self.explorer_splitter.addWidget(self.explorer_panel)

        # 连接文件编辑请求信号
        self.explorer_panel.file_edit_requested.connect(self._open_file_in_editor)

        # 内置文件编辑器
        self.file_editor = FileEditorWidget(theme=current_theme)
        self.file_editor.editor_closed.connect(self._on_editor_closed)
        self.file_editor.hide()  # 默认隐藏
        self.explorer_splitter.addWidget(self.file_editor)

        # 连接资源管理器的保存信号到文件编辑器
        self.explorer_panel.save_file_requested.connect(self.file_editor.save_file)
        self.explorer_panel.save_file_as_requested.connect(self.file_editor.save_file_as)

        # 设置初始比例（资源管理器占更多空间）
        self.explorer_splitter.setSizes([400, 0])

        layout.addWidget(self.explorer_splitter)

    def _open_file_in_editor(self, file_path: str):
        """在内置编辑器中打开文件"""
        if not hasattr(self, 'file_editor'):
            return

        # 打开文件
        if not self.file_editor.open_file(file_path):
            return

        # 通知资源管理器当前正在编辑的文件
        if hasattr(self, 'explorer_panel'):
            self.explorer_panel.set_editing_file(file_path)

        if self._explorer_split_horizontal:
            # 左右分屏：编辑器放到 main_splitter 中（左面板和终端之间）
            self._place_editor_in_main_splitter()
        else:
            # 上下分屏：编辑器在 explorer_splitter 中（默认行为）
            self._place_editor_in_explorer_splitter()

    def _place_editor_in_main_splitter(self):
        """将编辑器放到 main_splitter 中（左右分屏模式）"""
        if self.main_splitter.indexOf(self.file_editor) >= 0:
            # 已经在 main_splitter 中，只需确保可见并调整大小
            self.file_editor.show()
        else:
            # 从 explorer_splitter 中取出
            self.file_editor.setParent(None)
            self.file_editor.show()
            # 插入到 main_splitter 的 index 1（left_panel 和 tab_widget 之间）
            self.main_splitter.insertWidget(1, self.file_editor)

        # 调整比例：左面板 300, 编辑器 400, 终端 600, 日志 0
        left_width = 300
        log_width = 300 if self.log_panel_visible else 0
        self.main_splitter.setSizes([left_width, 400, 600, log_width])

        # explorer_splitter 中只剩文件树，让它占满
        self.explorer_splitter.setSizes([400, 0])

    def _place_editor_in_explorer_splitter(self):
        """将编辑器放到 explorer_splitter 中（上下分屏模式）"""
        if self.explorer_splitter.indexOf(self.file_editor) >= 0:
            # 已经在 explorer_splitter 中，只需确保可见并调整大小
            self.file_editor.show()
        else:
            # 从 main_splitter 中取出
            self.file_editor.setParent(None)
            self.file_editor.show()
            # 放回 explorer_splitter
            self.explorer_splitter.addWidget(self.file_editor)

        self.explorer_splitter.setSizes([200, 400])

        # 恢复 main_splitter 正常比例
        self._update_splitter_sizes()

    def _on_editor_closed(self):
        """编辑器关闭时"""
        if hasattr(self, 'file_editor'):
            self.file_editor.hide()
            # 确保编辑器回到 explorer_splitter（归位）
            if self.explorer_splitter.indexOf(self.file_editor) < 0:
                self.file_editor.setParent(None)
                self.explorer_splitter.addWidget(self.file_editor)
                self.file_editor.hide()
            # 恢复资源管理器占据全部空间
            self.explorer_splitter.setSizes([400, 0])
            # 恢复 main_splitter 正常比例
            self._update_splitter_sizes()
        # 清除资源管理器中的编辑文件标记
        if hasattr(self, 'explorer_panel'):
            self.explorer_panel.clear_editing_file()

    def _on_explorer_split_orientation_changed(self, state):
        """切换资源管理器与编辑器的分屏方向"""
        horizontal = (state == Qt.CheckState.Checked.value)
        self._explorer_split_horizontal = horizontal

        # 如果编辑器正在显示，立即切换位置
        if hasattr(self, 'file_editor') and self.file_editor.isVisible():
            if horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_explorer_splitter()

    def _toggle_explorer_panel(self):
        """切换 Explorer 面板显示"""
        self.explorer_panel_visible = not self.explorer_panel_visible
        self.explorer_toggle_btn.setChecked(self.explorer_panel_visible)

        # 暂停所有相关容器的绘制，避免中间态闪烁
        self.main_splitter.setUpdatesEnabled(False)
        self.left_panel_container.setUpdatesEnabled(False)

        if self.explorer_panel_visible:
            # 隐藏 Git 面板
            self.git_panel_visible = False
            self.git_toggle_btn.setChecked(False)
            self.git_panel_container.hide()

            self.explorer_panel_container.show()
            self.left_panel_container.show()
            # 设置根目录为当前工作目录（路径未变时内部会跳过重扫描）
            self.explorer_panel.set_root_path(self._window_cwd)

            # 恢复文件编辑器（如果之前有打开的文件）
            if hasattr(self, 'file_editor') and self.file_editor._current_file:
                if self._explorer_split_horizontal:
                    self._place_editor_in_main_splitter()
                else:
                    self._place_editor_in_explorer_splitter()
            else:
                self._update_splitter_sizes()
        else:
            self.explorer_panel_container.hide()

            # 同时隐藏文件编辑器
            if hasattr(self, 'file_editor') and self.file_editor.isVisible():
                self.file_editor.hide()

            # 如果编辑器在 main_splitter 中（水平分屏模式），归位到 explorer_splitter
            if hasattr(self, 'file_editor') and self.main_splitter.indexOf(self.file_editor) >= 0:
                self.file_editor.setParent(None)
                self.explorer_splitter.addWidget(self.file_editor)
                self.file_editor.hide()
                self.explorer_splitter.setSizes([400, 0])

            # 如果其他面板也隐藏，则隐藏整个左侧容器
            if not self.git_panel_visible:
                self.left_panel_container.hide()

            self._update_splitter_sizes()

        # 先恢复绘制让 UI 立即呈现，终端 resize 延迟到下一事件循环
        self.left_panel_container.setUpdatesEnabled(True)
        self.main_splitter.setUpdatesEnabled(True)
        QTimer.singleShot(0, self._flush_terminal_resizes)

    def _setup_git_panel(self):
        """设置 Git 面板"""
        layout = QVBoxLayout(self.git_panel_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Git 面板标题栏（保存引用以便主题切换时直接访问）
        self._git_header = QFrame()
        self._git_header.setStyleSheet("""
            QFrame {
                background-color: #16213e;
                border-bottom: 1px solid #3d3d5c;
            }
        """)
        git_header_layout = QHBoxLayout(self._git_header)
        git_header_layout.setContentsMargins(10, 5, 10, 5)

        self._git_title = QLabel("Git")
        self._git_title.setStyleSheet("color: #f97316; font-weight: bold;")
        git_header_layout.addWidget(self._git_title)

        git_header_layout.addStretch()

        # 隐藏按钮
        hide_btn = QPushButton("×")
        hide_btn.setFixedSize(24, 24)
        hide_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #888;
                border: none;
                font-size: 16px;
            }
            QPushButton:hover {
                color: #eaeaea;
            }
        """)
        hide_btn.clicked.connect(self._toggle_git_panel)
        git_header_layout.addWidget(hide_btn)

        layout.addWidget(self._git_header)

        # 获取当前主题
        current_theme = self.THEMES.get(self.current_theme, self.THEMES["深蓝"])

        # Git 面板内容
        self.git_panel = GitPanel(theme=current_theme)
        layout.addWidget(self.git_panel)

    def _toggle_git_panel(self):
        """切换 Git 面板显示"""
        self.git_panel_visible = not self.git_panel_visible
        self.git_toggle_btn.setChecked(self.git_panel_visible)

        self.main_splitter.setUpdatesEnabled(False)

        if self.git_panel_visible:
            # 隐藏 Explorer 面板
            self.explorer_panel_visible = False
            self.explorer_toggle_btn.setChecked(False)
            self.explorer_panel_container.hide()

            # 隐藏文件编辑器（如果在 main_splitter 中归位）
            if hasattr(self, 'file_editor'):
                if self.file_editor.isVisible():
                    self.file_editor.hide()
                if self.main_splitter.indexOf(self.file_editor) >= 0:
                    self.file_editor.setParent(None)
                    self.explorer_splitter.addWidget(self.file_editor)
                    self.file_editor.hide()
                    self.explorer_splitter.setSizes([400, 0])

            self.git_panel_container.show()
            self.left_panel_container.show()
            # 设置仓库路径
            self.git_panel.set_repository(self._window_cwd)
        else:
            self.git_panel_container.hide()
            # 如果其他面板也隐藏，则隐藏整个左侧容器
            if not self.explorer_panel_visible:
                self.left_panel_container.hide()

        self._update_splitter_sizes()
        self.main_splitter.setUpdatesEnabled(True)
        QTimer.singleShot(0, self._flush_terminal_resizes)

    def _update_splitter_sizes(self):
        """更新分割器大小"""
        left_width = 300 if (self.explorer_panel_visible or self.git_panel_visible) else 0
        log_width = 300 if self.log_panel_visible else 0

        # 检查编辑器是否在 main_splitter 中（左右分屏模式，splitter 有 4 个 widget）
        editor_in_main = hasattr(self, 'file_editor') and self.main_splitter.indexOf(self.file_editor) >= 0
        if editor_in_main:
            editor_width = 400
            terminal_width = 1000 - left_width - editor_width - log_width
            self.main_splitter.setSizes([left_width, editor_width, terminal_width, log_width])
        elif left_width > 0 or log_width > 0:
            terminal_width = 1000 - left_width - log_width
            self.main_splitter.setSizes([left_width, terminal_width, log_width])
        else:
            # 如果都隐藏，让终端占满
            self.main_splitter.setSizes([0, 1000, 0])

    def _flush_terminal_resizes(self):
        """强制当前活动标签页的终端立即完成 resize（跳过防抖），避免面板切换时闪烁"""
        tab = self.tab_widget.currentWidget()
        if tab is None:
            return
        # 只处理当前活动标签页，其他标签页切换时会自然 resize
        if isinstance(tab, QSplitter):
            for i in range(tab.count()):
                w = tab.widget(i)
                if isinstance(w, TerminalWidget) and w.isVisible():
                    w.flush_resize()
        elif isinstance(tab, TerminalWidget) and tab.isVisible():
            tab.flush_resize()

    # 缓存编辑器路径，避免重复搜索
    _vscode_path_cache = None
    _cursor_path_cache = None

    def _open_in_vscode(self):
        """在 VS Code 中打开当前工作目录"""
        # 使用缓存的路径
        if MainWindow._vscode_path_cache is None:
            # 首次查找并缓存
            code_path = shutil.which("code")
            if not code_path:
                # 尝试常见路径
                possible_paths = [
                    "/usr/local/bin/code",
                    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
                    os.path.expanduser("~/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
                ]
                for path in possible_paths:
                    if os.path.isfile(path):
                        code_path = path
                        break
            MainWindow._vscode_path_cache = code_path or ""  # 空字符串表示未找到

        code_path = MainWindow._vscode_path_cache
        if not code_path:
            QMessageBox.warning(
                self,
                t("msg.vscode_not_found"),
                t("msg.vscode_not_found")
            )
            return

        try:
            # 打开当前工作目录
            subprocess.Popen([code_path, self._window_cwd])
            self.statusbar.showMessage(t("status.opened_in_vscode", cwd=self._window_cwd), 3000)
        except Exception as e:
            QMessageBox.warning(self, t("msg.open_failed"), t("msg.vscode_open_error", error=str(e)))

    def _open_in_cursor(self):
        """在 Cursor 中打开当前工作目录"""
        # 使用缓存的路径
        if MainWindow._cursor_path_cache is None:
            # 首次查找并缓存
            cursor_path = shutil.which("cursor")
            if not cursor_path:
                # 尝试常见路径
                possible_paths = [
                    "/usr/local/bin/cursor",
                    "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
                    os.path.expanduser("~/Applications/Cursor.app/Contents/Resources/app/bin/cursor"),
                ]
                for path in possible_paths:
                    if os.path.isfile(path):
                        cursor_path = path
                        break
            MainWindow._cursor_path_cache = cursor_path or ""  # 空字符串表示未找到

        cursor_path = MainWindow._cursor_path_cache
        if not cursor_path:
            QMessageBox.warning(
                self,
                t("msg.cursor_not_found"),
                t("msg.cursor_not_found")
            )
            return

        try:
            # 打开当前工作目录
            subprocess.Popen([cursor_path, self._window_cwd])
            self.statusbar.showMessage(t("status.opened_in_cursor", cwd=self._window_cwd), 3000)
        except Exception as e:
            QMessageBox.warning(self, t("msg.open_failed"), t("msg.cursor_open_error", error=str(e)))

    def _show_toolbar_manager(self):
        """显示工具栏管理对话框"""
        current_theme = self.THEMES.get(self.current_theme, self.THEMES["深蓝"])
        dialog = ToolbarManagerDialog(self.toolbar_config, current_theme, self)
        dialog.config_changed.connect(self._on_toolbar_config_changed)
        dialog.exec()

    def _show_llm_config(self):
        """显示 LLM API 配置对话框"""
        dialog = LLMConfigDialog(self.llm_configs, self.default_llm_config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.llm_configs = dialog.get_configs()
            self.default_llm_config = dialog.get_default_index()
            self._save_config()
            self.statusbar.showMessage(t("status.llm_config_saved"), 3000)

    def _on_toolbar_config_changed(self, config: dict):
        """工具栏配置变更回调"""
        old_layout = self.toolbar_config.get("layout", "single") if self.toolbar_config else "single"
        new_layout = config.get("layout", "single")

        # 检查按钮顺序是否变更
        old_order = self.toolbar_config.get("button_order", {}) if self.toolbar_config else {}
        new_order = config.get("button_order", {})
        order_changed = old_order != new_order

        # 检查分组顺序是否变更
        old_group_order = self.toolbar_config.get("group_order", []) if self.toolbar_config else []
        new_group_order = config.get("group_order", [])
        group_order_changed = old_group_order != new_group_order

        # 检查跨组按钮分配是否变更
        old_button_groups = self.toolbar_config.get("button_groups", {}) if self.toolbar_config else {}
        new_button_groups = config.get("button_groups", {})
        button_groups_changed = old_button_groups != new_button_groups

        self.toolbar_config = config
        self._apply_toolbar_config(config)
        self._save_config()

        # 检查布局、按钮顺序、分组顺序或跨组分配是否变更
        if old_layout != new_layout or order_changed or group_order_changed or button_groups_changed:
            self.statusbar.showMessage(t("status.toolbar_saved_restart"), 5000)
        else:
            self.statusbar.showMessage(t("status.toolbar_saved"), 3000)

    def _apply_toolbar_config(self, config: dict):
        """应用工具栏配置"""
        if not config:
            return
        if not hasattr(self, '_toolbar_actions') or not self._toolbar_actions:
            return
        visible_buttons = config.get("visible_buttons", {})
        # Row1 按钮通过 action 控制可见性
        for btn_name, action in self._toolbar_actions.items():
            if btn_name in visible_buttons:
                action.setVisible(visible_buttons[btn_name])
        # Flow 中的 row2 按钮直接控制 widget 可见性
        for btn_name, widget in self._flow_btn_widgets.items():
            if btn_name in visible_buttons:
                widget.setVisible(visible_buttons[btn_name])
        # 更新 flow toolbar 高度
        if hasattr(self, '_pinned_flow_toolbar') and self._pinned_flow_toolbar and self._pinned_flow_toolbar.isVisible():
            QTimer.singleShot(0, self._update_flow_toolbar_height)

    def _get_toolbar_layout(self) -> str:
        """获取当前工具栏布局配置"""
        if self.toolbar_config:
            return self.toolbar_config.get("layout", "single")
        return "single"

    def _get_button_row(self, btn_name: str) -> int:
        """判断按钮应该在第几行（1=row1, 2=pinned area）
        动态查询按钮所属分组（含跨组移动）"""
        from toolbar_manager import ToolbarManagerDialog
        ROW1_GROUPS = {"预设与控制", "选项"}

        # 获取按钮所属分组
        button_groups = self.toolbar_config.get("button_groups", {}) if self.toolbar_config else {}
        group = button_groups.get(btn_name)
        if not group:
            for name, _, _, g in ToolbarManagerDialog.BUTTON_DEFINITIONS:
                if name == btn_name:
                    group = g
                    break

        if group in ROW1_GROUPS:
            return 1
        else:
            return 2

    def _is_button_in_row2(self, btn_name: str) -> bool:
        """兼容旧调用：判断按钮是否不在第一行"""
        return self._get_button_row(btn_name) != 1

    def _on_theme_changed(self, index: int):
        """主题变更处理"""
        theme_key = self.theme_combo.currentData()
        if theme_key and theme_key in self.THEMES:
            self.current_theme = theme_key
            self._apply_theme(theme_key)
            self._save_config()
            display_name = t(f"theme.{theme_key}")
            self.statusbar.showMessage(t("theme.switched", name=display_name), 3000)

    def _on_language_changed(self, index: int):
        """语言变更处理"""
        lang_code = self.lang_combo.currentData()
        if lang_code:
            set_language(lang_code)
            self._apply_language()
            self._save_config()

    def _apply_language(self):
        """更新所有 UI 文本以反映当前语言"""
        # 标题栏
        self.title_label.setText(t("toolbar.title_label"))
        self.preset_label.setText(t("toolbar.preset_label"))

        # 工具栏按钮
        if hasattr(self, 'start_btn'):
            self.start_btn.setText(t("toolbar.start"))
        if hasattr(self, 'stop_btn'):
            self.stop_btn.setText(t("toolbar.stop"))

        # 工具栏选项组
        if hasattr(self, 'image_prefix_checkbox'):
            self.image_prefix_checkbox.setText(t("toolbar.image_prefix"))
            self.image_prefix_checkbox.setToolTip(t("toolbar.image_prefix_tooltip"))
        if hasattr(self, 'image_local_checkbox'):
            self.image_local_checkbox.setText(t("toolbar.image_local"))
            self.image_local_checkbox.setToolTip(t("toolbar.image_local_tooltip"))
        if hasattr(self, 'window_nav_checkbox'):
            self.window_nav_checkbox.setText(t("toolbar.window_nav"))
            self.window_nav_checkbox.setToolTip(t("toolbar.window_nav_tooltip"))

        # 工具栏操作组
        if hasattr(self, 'export_btn'):
            self.export_btn.setText(t("toolbar.export"))
        if hasattr(self, 'history_btn'):
            self.history_btn.setText(t("toolbar.history"))
        if hasattr(self, 'clear_btn'):
            self.clear_btn.setText(t("toolbar.clear"))

        # 分屏管理组
        if hasattr(self, 'split_btn'):
            self.split_btn.setText(t("toolbar.split"))
            self.split_btn.setToolTip(t("toolbar.split_tooltip"))
        if hasattr(self, 'split_v_btn'):
            self.split_v_btn.setText(t("toolbar.split_v"))
            self.split_v_btn.setToolTip(t("toolbar.split_v_tooltip"))
        if hasattr(self, 'close_split_btn'):
            self.close_split_btn.setText(t("toolbar.close_split"))
            self.close_split_btn.setToolTip(t("toolbar.close_split_tooltip"))
        if hasattr(self, 'close_tab_btn'):
            self.close_tab_btn.setText(t("toolbar.close_tab"))
            self.close_tab_btn.setToolTip(t("toolbar.close_tab_tooltip"))

        # 面板与编辑器组
        if hasattr(self, 'explorer_toggle_btn'):
            self.explorer_toggle_btn.setText(t("toolbar.explorer"))
            self.explorer_toggle_btn.setToolTip(t("toolbar.explorer_tooltip"))
        if hasattr(self, 'git_toggle_btn'):
            self.git_toggle_btn.setText(t("toolbar.git"))
        if hasattr(self, 'vscode_open_btn'):
            self.vscode_open_btn.setText(t("toolbar.vscode"))
            self.vscode_open_btn.setToolTip(t("toolbar.vscode_tooltip"))
        if hasattr(self, 'cursor_open_btn'):
            self.cursor_open_btn.setText(t("toolbar.cursor"))
            self.cursor_open_btn.setToolTip(t("toolbar.cursor_tooltip"))
        if hasattr(self, 'log_toggle_btn'):
            self.log_toggle_btn.setText(t("toolbar.log"))

        # 管理预设按钮
        if hasattr(self, 'manage_preset_btn'):
            self.manage_preset_btn.setText(t("toolbar.manage_preset"))
        if hasattr(self, 'preset_switch_btn'):
            self.preset_switch_btn.setText(t("toolbar.switch_preset"))
            self.preset_switch_btn.setToolTip(t("toolbar.switch_preset_tooltip"))

        # 主题组
        if hasattr(self, 'theme_label'):
            self.theme_label.setText(t("theme.label"))
        if hasattr(self, 'theme_combo'):
            # 更新主题 combo 的显示文本（保持内部 key 不变）
            for i in range(self.theme_combo.count()):
                theme_key = self.theme_combo.itemData(i)
                if theme_key:
                    self.theme_combo.setItemText(i, t(f"theme.{theme_key}"))
        if hasattr(self, 'icon_tint_checkbox'):
            self.icon_tint_checkbox.setText(t("toolbar.icon_tint"))
            self.icon_tint_checkbox.setToolTip(t("toolbar.icon_tint_tooltip"))

        # 语言组（lang_combo 直接作为独立 widget，无 label）

        # LLM 配置
        if hasattr(self, 'llm_config_btn'):
            self.llm_config_btn.setToolTip(t("toolbar.llm_config_tooltip"))

        # GUI 字体大小
        if hasattr(self, 'gui_font_label'):
            self.gui_font_label.setText(t("toolbar.gui_font_label"))
        if hasattr(self, 'gui_font_spin'):
            self.gui_font_spin.setSpecialValueText(t("toolbar.gui_font_auto"))
            self.gui_font_spin.setToolTip(t("toolbar.gui_font_tooltip"))

        # 固定第二排
        if hasattr(self, 'pin_row2_checkbox'):
            self.pin_row2_checkbox.setText(t("toolbar.pin_row2"))
            self.pin_row2_checkbox.setToolTip(t("toolbar.pin_row2_tooltip"))

        # 日志面板
        if hasattr(self, 'log_title'):
            self.log_title.setText(t("log.title"))
        if hasattr(self, 'clear_log_btn'):
            self.clear_log_btn.setText(t("log.clear"))

        # 底部状态栏
        if hasattr(self, 'session_label'):
            # 保持当前状态文本不变（可能是运行中/已停止等）
            pass
        self.statusbar.showMessage(t("status.ready"), 3000)

        # 目录工具栏
        if hasattr(self, 'dir_label'):
            self.dir_label.setText(t("dir.label"))
        if hasattr(self, 'browse_btn'):
            self.browse_btn.setText(t("dir.browse"))
        if hasattr(self, 'apply_dir_btn'):
            self.apply_dir_btn.setText(t("dir.switch"))
            self.apply_dir_btn.setToolTip(t("status.apply_dir_tooltip"))
        if hasattr(self, 'current_dir_label'):
            self.current_dir_label.setText(t("dir.current", cwd=self._window_cwd))

        # 新建标签页按钮
        if hasattr(self, 'new_tab_btn'):
            self.new_tab_btn.setToolTip(t("toolbar.new_tab_tooltip"))
        if hasattr(self, 'color_btn'):
            self.color_btn.setToolTip(t("toolbar.color_tooltip"))
        if hasattr(self, 'dir_dropdown_btn'):
            self.dir_dropdown_btn.setToolTip(t("status.dir_history_tooltip"))

        # Explorer 面板标题
        if hasattr(self, '_explorer_title'):
            self._explorer_title.setText(t("explorer.title"))
        if hasattr(self, '_explorer_split_checkbox'):
            self._explorer_split_checkbox.setText(t("explorer.left_right_split"))
            self._explorer_split_checkbox.setToolTip(t("explorer.split_tooltip"))

        # Git 面板标题
        if hasattr(self, '_git_title'):
            self._git_title.setText(t("git.source_control"))

        # 子组件 apply_language
        if hasattr(self, 'explorer_panel'):
            if hasattr(self.explorer_panel, 'apply_language'):
                self.explorer_panel.apply_language()
        if hasattr(self, 'git_panel'):
            if hasattr(self.git_panel, 'apply_language'):
                self.git_panel.apply_language()
        if hasattr(self, 'file_editor'):
            if hasattr(self.file_editor, 'apply_language'):
                self.file_editor.apply_language()

    def _on_icon_tint_changed(self, state):
        """图标蒙版开关变更"""
        self._use_icon_tint = (state == Qt.CheckState.Checked.value)
        self._update_app_icon_by_theme()
        self._save_config()

    def _update_app_icon_by_theme(self):
        """根据当前主题和蒙版设置更新图标"""
        if self._use_icon_tint:
            t = self.THEMES.get(self.current_theme, self.THEMES["深蓝"])
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

        # 主窗口样式
        self.setStyleSheet(f"""
            QMainWindow {{
                background-color: {t['bg_darkest']};
            }}
            QToolBar {{
                background-color: {t['bg_dark']};
                border: none;
                spacing: 5px;
                padding: 5px;
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

        # 标签页样式
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background-color: {t['bg_dark']};
            }}
            QTabBar::tab {{
                background-color: {t['bg_medium']};
                color: {t['text']};
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }}
            QTabBar::tab:selected {{
                background-color: {t['bg_dark']};
            }}
            QTabBar::tab:hover:!selected {{
                background-color: {t['bg_light']};
            }}
        """)

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
                padding: 4px 8px;
                spacing: 6px;
            }}
        """)

        # 固定流式工具栏样式
        if self._pinned_flow_toolbar:
            self._pinned_flow_widget.setStyleSheet(f"""
                QWidget#pinnedFlowWidget {{
                    background-color: {t['bg_dark']};
                }}
            """)
            # 更新分隔符颜色
            if self._flow_layout:
                for i in range(self._flow_layout.count()):
                    item = self._flow_layout.itemAt(i)
                    if item and item.widget() and item.widget().objectName() == "_flow_separator":
                        item.widget().setStyleSheet(f"QFrame {{ color: {t['border']}; }}")

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
                    font-size: 14px;
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
                    font-size: 14px;
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

        # 内置文件编辑器样式
        if hasattr(self, 'file_editor'):
            self.file_editor.apply_theme(t)

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
            self.explorer_toggle_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #22c55e;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }}
                QPushButton:hover {{
                    background-color: #4ade80;
                }}
                QPushButton:checked {{
                    background-color: #16a34a;
                }}
            """)

        # Git 面板样式
        if hasattr(self, 'git_panel'):
            self.git_panel.apply_theme(t)

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
            self.git_toggle_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #f97316;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }}
                QPushButton:hover {{
                    background-color: #fb923c;
                }}
                QPushButton:checked {{
                    background-color: #ea580c;
                }}
            """)

        # VS Code 打开按钮样式
        if hasattr(self, 'vscode_open_btn'):
            self.vscode_open_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #007ACC;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }}
                QPushButton:hover {{
                    background-color: #1a8ad4;
                }}
            """)

        # Cursor 打开按钮样式
        if hasattr(self, 'cursor_open_btn'):
            self.cursor_open_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: #7c3aed;
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 8px 15px;
                }}
                QPushButton:hover {{
                    background-color: #8b5cf6;
                }}
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

        # 主题切换后重新应用 GUI 字体缩放（因为样式表被覆盖）
        self._original_widget_styles.clear()
        if self._gui_font_size != 0 or self._global_zoom_delta != 0:
            self._scale_gui_font_sizes(self._gui_font_size, self._global_zoom_delta)

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

    def _clear_log(self):
        """清空日志"""
        self.log_text.clear()

    def _debug_special_chars(self):
        """调试：显示屏幕上的特殊字符"""
        if self.terminal:
            debug_info = self.terminal.debug_special_chars()
            QMessageBox.information(self, t("msg.debug_special_chars"), debug_info)

    def _quick_export(self):
        """快速导出（HTML）"""
        session = self.current_session
        if session:
            self.session_manager.auto_save()
        else:
            sessions = self.session_manager.list_sessions()
            if sessions:
                session = self.session_manager.load_session(sessions[0]['session_id'])

        if not session:
            QMessageBox.information(self, t("msg.hint"), t("msg.no_session_to_export"))
            return

        try:
            output_path = export_session(session, 'html', open_after=True)
            self.statusbar.showMessage(t("status.exported", path=output_path))
        except Exception as e:
            QMessageBox.critical(self, t("msg.export_failed"), str(e))

    def _show_export_dialog(self):
        """显示导出对话框"""
        session = self.current_session
        if session:
            self.session_manager.auto_save()
        else:
            sessions = self.session_manager.list_sessions()
            if not sessions:
                QMessageBox.information(self, t("msg.hint"), t("msg.no_session_to_export"))
                return
            session = self.session_manager.load_session(sessions[0]['session_id'])

        if not session:
            return

        from PyQt6.QtWidgets import QInputDialog

        formats = [t("export.format_html"), "Markdown", "JSON", t("export.format_all")]
        format_choice, ok = QInputDialog.getItem(
            self, t("export.format_title"), t("export.format_prompt"),
            formats, 0, False
        )

        if ok:
            format_map = {
                t("export.format_html"): ['html'],
                "Markdown": ['markdown'],
                "JSON": ['json'],
                t("export.format_all"): ['html', 'markdown', 'json']
            }

            for fmt in format_map.get(format_choice, ['html']):
                try:
                    output_path = export_session(session, fmt, open_after=(fmt == 'html'))
                    self.statusbar.showMessage(t("status.exported", path=output_path))
                except Exception as e:
                    QMessageBox.critical(self, t("msg.export_failed"), f"{fmt}: {e}")

    def _show_history(self):
        """显示历史对话框"""
        dialog = HistoryDialog(self.session_manager, self)
        dialog.exec()

    def _styled_message_box(self, icon, title, text, buttons=QMessageBox.StandardButton.Ok):
        """创建带明确样式的消息框，避免深色主题导致文字不可见"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setIcon(icon)
        msg_box.setStandardButtons(buttons)
        msg_box.setStyleSheet("""
            QMessageBox {
                background-color: #f0f0f0;
            }
            QMessageBox QLabel {
                color: #333333;
                font-size: 14px;
            }
            QMessageBox QPushButton {
                background-color: #e0e0e0;
                color: #333333;
                border: 1px solid #999999;
                padding: 5px 15px;
                border-radius: 3px;
                min-width: 60px;
            }
            QMessageBox QPushButton:hover {
                background-color: #d0d0d0;
            }
            QMessageBox QPushButton:pressed {
                background-color: #c0c0c0;
            }
        """)
        return msg_box.exec()

    def _load_config(self):
        """加载配置（预设命令等）"""
        # 迁移旧配置文件（从用户目录迁移到程序目录）
        old_config = Path.home() / ".smart_terminal_config.json"
        if not self.CONFIG_FILE.exists() and old_config.exists():
            shutil.copy2(old_config, self.CONFIG_FILE)

        self.last_preset_index = 0  # 默认选中第一个
        self.image_prefix_enabled = False  # 图片路径是否加@前缀
        self.image_save_local = True  # 图片是否保存到工作目录（默认开启，方便Gemini访问）
        self.working_dir_history = []  # 工作目录历史
        self._working_dir_freq = {}  # 工作目录使用频率 {path: count}
        self.last_working_dir = None  # 上次使用的工作目录
        self.toolbar_config = None  # 工具栏配置
        self.llm_configs = []  # LLM API 配置列表
        self.default_llm_config = 0  # 默认 LLM 配置索引
        self._saved_window_geometry = None  # 窗口位置和大小 [x, y, w, h]
        self._saved_window_maximized = False  # 窗口是否最大化
        self._saved_explorer_panel_visible = False  # Explorer 面板可见性
        self._saved_git_panel_visible = False  # Git 面板可见性
        self._saved_log_panel_visible = False  # 日志面板可见性
        try:
            if self.CONFIG_FILE.exists():
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.presets = config.get('presets', [])
                    self.last_preset_index = config.get('last_preset_index', 0)
                    self.image_prefix_enabled = config.get('image_prefix_enabled', False)
                    self.image_save_local = config.get('image_save_local', True)
                    self.working_dir_history = config.get('working_dir_history', [])
                    self._working_dir_freq = config.get('working_dir_freq', {})
                    # 兼容旧配置：为没有频率记录的历史路径补默认值
                    for p in self.working_dir_history:
                        if p not in self._working_dir_freq:
                            self._working_dir_freq[p] = 1
                    # 按频率倒序排列
                    self.working_dir_history.sort(key=lambda p: self._working_dir_freq.get(p, 0), reverse=True)
                    self.last_working_dir = config.get('last_working_dir', None)
                    # 加载主题设置
                    saved_theme = config.get('theme', '深蓝')
                    if saved_theme in self.THEMES:
                        self.current_theme = saved_theme
                    # 加载图标蒙版设置
                    self._use_icon_tint = config.get('icon_tint', False)
                    # 加载工具栏配置
                    self.toolbar_config = config.get('toolbar_config', None)
                    # 加载 LLM 配置
                    self.llm_configs = config.get('llm_configs', [])
                    self.default_llm_config = config.get('default_llm_config', 0)
                    # 加载全局缩放偏移
                    self._global_zoom_delta = config.get('global_zoom_delta', 0)
                    # 加载 GUI 字体大小
                    self._gui_font_size = config.get('gui_font_size', 0)
                    # 加载固定第二排工具栏设置
                    self._pin_toolbar_row2 = config.get('pin_toolbar_row2', False)
                    # 加载左右分屏偏好
                    self._explorer_split_horizontal = config.get('explorer_split_horizontal', False)
                    # 加载语言设置
                    saved_lang = config.get('language', 'zh')
                    if saved_lang in ('zh', 'en'):
                        set_language(saved_lang)
                    # 加载窗口几何与面板可见性
                    self._saved_window_geometry = config.get('window_geometry', None)
                    self._saved_window_maximized = config.get('window_maximized', False)
                    self._saved_explorer_panel_visible = config.get('explorer_panel_visible', False)
                    self._saved_git_panel_visible = config.get('git_panel_visible', False)
                    self._saved_log_panel_visible = config.get('log_panel_visible', False)
        except Exception:
            self.presets = []

        # 确保当前目录在历史中
        current_dir = os.getcwd()
        if current_dir not in self.working_dir_history:
            self.working_dir_history.append(current_dir)
        if current_dir not in self._working_dir_freq:
            self._working_dir_freq[current_dir] = 1
        # 按频率倒序排列
        self.working_dir_history.sort(key=lambda p: self._working_dir_freq.get(p, 0), reverse=True)

        # 确保有默认预设
        if not self.presets:
            default_shell = get_default_shell()
            # Windows 使用 set，Unix 使用 export 设置环境变量
            if sys.platform == 'win32':
                proxy_cmds = [
                    'set http_proxy=http://127.0.0.1:1081/',
                    'set https_proxy=http://127.0.0.1:1081/'
                ]
            else:
                proxy_cmds = [
                    'export http_proxy=http://127.0.0.1:1081/',
                    'export https_proxy=http://127.0.0.1:1081/'
                ]
            self.presets = [
                {
                    'name': default_shell,
                    'commands': [default_shell]
                },
                {
                    'name': 'Claude Opus (with proxy)',
                    'commands': [default_shell] + proxy_cmds + ['claude --model opus']
                },
                {
                    'name': 'Claude Sonnet',
                    'commands': [
                        default_shell,
                        'claude --model sonnet'
                    ]
                }
            ]

        # 确保有默认 LLM 配置
        if not self.llm_configs:
            self.llm_configs = [
                {
                    'name': 'OpenAI GPT-4',
                    'api_base': 'https://api.openai.com/v1',
                    'api_key': '',
                    'model': 'gpt-4',
                    'timeout': 30,
                    'max_tokens': 4096,
                    'temperature': 1.0,
                    'top_p': 1.0,
                    'proxy': ''
                }
            ]
            self.default_llm_config = 0

    def _save_config(self):
        """保存配置"""
        try:
            # 获取当前选中的预设索引
            current_index = self.preset_combo.currentIndex() if hasattr(self, 'preset_combo') else 0
            image_prefix = self.image_prefix_checkbox.isChecked() if hasattr(self, 'image_prefix_checkbox') else False
            image_local = self.image_local_checkbox.isChecked() if hasattr(self, 'image_local_checkbox') else True
            # 限制历史记录数量
            dir_history = self.working_dir_history if hasattr(self, 'working_dir_history') else []
            # 使用窗口级别的工作目录
            last_cwd = self._window_cwd if hasattr(self, '_window_cwd') else os.getcwd()
            config = {
                'presets': self.presets,
                'last_preset_index': current_index,
                'image_prefix_enabled': image_prefix,
                'image_save_local': image_local,
                'working_dir_history': dir_history,
                'working_dir_freq': self._working_dir_freq if hasattr(self, '_working_dir_freq') else {},
                'last_working_dir': last_cwd,
                'theme': self.current_theme,  # 保存主题设置
                'icon_tint': self._use_icon_tint,  # 保存图标蒙版设置
                'toolbar_config': self.toolbar_config,  # 保存工具栏配置
                'llm_configs': self.llm_configs,  # 保存 LLM 配置
                'default_llm_config': self.default_llm_config,  # 保存默认 LLM 配置索引
                'global_zoom_delta': self._global_zoom_delta,  # 保存全局缩放偏移
                'gui_font_size': self._gui_font_size,  # 保存 GUI 字体大小
                'pin_toolbar_row2': self._pin_toolbar_row2,  # 保存固定第二排工具栏
                'explorer_split_horizontal': getattr(self, '_explorer_split_horizontal', False),  # 保存左右分屏偏好
                'language': get_language(),  # 保存语言设置
                'window_geometry': [self.x(), self.y(), self.width(), self.height()],
                'window_maximized': self.isMaximized(),
                'explorer_panel_visible': getattr(self, 'explorer_panel_visible', False),
                'git_panel_visible': getattr(self, 'git_panel_visible', False),
                'log_panel_visible': getattr(self, 'log_panel_visible', False),
            }
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_llm_config(self, name: str = None) -> dict:
        """获取指定名称的 LLM 配置，若不指定则返回默认配置

        Args:
            name: 配置名称，若为 None 则返回默认配置

        Returns:
            LLM 配置字典，若未找到则返回 None
        """
        if not self.llm_configs:
            return None

        if name is None:
            # 返回默认配置
            if 0 <= self.default_llm_config < len(self.llm_configs):
                return self.llm_configs[self.default_llm_config].copy()
            return self.llm_configs[0].copy() if self.llm_configs else None

        # 按名称查找
        for config in self.llm_configs:
            if config.get('name') == name:
                return config.copy()
        return None

    def get_all_llm_configs(self) -> list:
        """获取所有 LLM 配置列表

        Returns:
            LLM 配置列表的副本
        """
        return [c.copy() for c in self.llm_configs]

    def _on_image_prefix_changed(self, state):
        """图片前缀选项变化"""
        self.image_prefix_enabled = (state == Qt.CheckState.Checked.value)
        # 更新所有终端的设置
        for terminals in self.tab_terminals.values():
            for terminal in terminals:
                terminal.image_prefix_enabled = self.image_prefix_enabled
        self._save_config()

    def _on_image_local_changed(self, state):
        """图片保存位置选项变化"""
        self.image_save_local = (state == Qt.CheckState.Checked.value)
        # 更新所有终端的设置
        for terminals in self.tab_terminals.values():
            for terminal in terminals:
                terminal.image_save_local = self.image_save_local
        self._save_config()

    def _on_window_nav_changed(self, state):
        """窗口快速导航选项变化"""
        if state == Qt.CheckState.Checked.value:
            # 创建或显示全局导航面板
            if MainWindow._global_window_navigator is None:
                MainWindow._global_window_navigator = WindowNavigatorPanel()
                # 监听导航面板关闭事件，同步所有窗口的 checkbox 状态
                MainWindow._global_window_navigator.panel_closed.connect(MainWindow._on_navigator_closed_global)
            MainWindow._global_window_navigator.show()
            MainWindow._global_window_navigator.raise_()
            # 同步所有窗口的 checkbox 状态为勾选
            MainWindow._sync_nav_checkbox_state(True)
        else:
            # 隐藏导航面板
            if MainWindow._global_window_navigator is not None:
                MainWindow._global_window_navigator.hide()
            # 同步所有窗口的 checkbox 状态为取消勾选
            MainWindow._sync_nav_checkbox_state(False)

    @staticmethod
    def _on_navigator_closed_global():
        """导航面板被关闭时的全局回调"""
        MainWindow._global_window_navigator = None
        # 同步所有窗口的 checkbox 状态为取消勾选
        MainWindow._sync_nav_checkbox_state(False)

    @staticmethod
    def _sync_nav_checkbox_state(checked: bool):
        """同步所有 MainWindow 实例的窗口导航 checkbox 状态"""
        app = QApplication.instance()
        if not app:
            return
        for widget in app.topLevelWidgets():
            if isinstance(widget, MainWindow) and hasattr(widget, 'window_nav_checkbox'):
                widget.window_nav_checkbox.blockSignals(True)
                widget.window_nav_checkbox.setChecked(checked)
                widget.window_nav_checkbox.blockSignals(False)

    def _populate_presets(self):
        """填充预设到下拉框"""
        self.preset_combo.clear()
        for preset in self.presets:
            self.preset_combo.addItem(preset.get('name', t('common.unnamed')))
        # 恢复上次选中的预设
        if self.presets:
            index = min(self.last_preset_index, len(self.presets) - 1)
            self.preset_combo.setCurrentIndex(max(0, index))

    def _manage_presets(self):
        """打开预设管理对话框"""
        dialog = PresetDialog(self.presets, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.presets = dialog.get_presets()
            self._populate_presets()
            self._save_config()
            self.statusbar.showMessage(t("status.preset_saved"), 3000)

    def _add_new_preset(self):
        """打开预设管理对话框并自动添加新预设"""
        dialog = PresetDialog(self.presets, self, auto_add=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.presets = dialog.get_presets()
            self._populate_presets()
            self._save_config()
            self.statusbar.showMessage(t("status.preset_saved"), 3000)

    # ==================== 本地快速命令相关方法 ====================

    def _get_local_commands_path(self) -> Path:
        """获取本地配置文件路径"""
        return Path(self._window_cwd) / self.LOCAL_CONFIG_DIR / self.LOCAL_COMMANDS_FILE

    def _ensure_local_config_dir(self) -> bool:
        """确保 .sterminal 目录存在

        Returns:
            bool: 成功创建或已存在返回 True，失败返回 False
        """
        # 检查是否为特殊目录（禁止创建配置）
        forbidden_dirs = ['/', '/usr', '/bin', '/sbin', '/etc', '/var', '/tmp', '/private']
        if self._window_cwd in forbidden_dirs:
            self.statusbar.showMessage(t("status.cannot_create_config", cwd=self._window_cwd), 3000)
            return False

        config_dir = Path(self._window_cwd) / self.LOCAL_CONFIG_DIR
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            return True
        except PermissionError:
            self._styled_message_box(
                QMessageBox.Icon.Warning,
                t("msg.permission_denied"),
                t("msg.cannot_create_config_dir", cwd=self._window_cwd)
            )
            return False
        except Exception as e:
            self.statusbar.showMessage(t("status.config_dir_error", error=str(e)), 3000)
            return False

    def _load_local_commands(self):
        """加载本地命令配置"""
        self.local_presets = []
        config_path = self._get_local_commands_path()

        if not config_path.exists():
            return

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # 验证数据结构
            if isinstance(data, dict) and 'presets' in data:
                presets = data.get('presets', [])
                if isinstance(presets, list):
                    self.local_presets = presets
        except json.JSONDecodeError:
            self.statusbar.showMessage(t("status.local_config_format_error"), 3000)
            self.local_presets = []
        except Exception as e:
            self.statusbar.showMessage(t("status.load_local_commands_error", error=str(e)), 3000)
            self.local_presets = []

    def _save_local_commands(self):
        """保存本地命令配置"""
        if not self._ensure_local_config_dir():
            return False

        config_path = self._get_local_commands_path()
        data = {
            "version": 1,
            "presets": self.local_presets,
            "updated_at": datetime.now().isoformat()
        }

        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except PermissionError:
            self._styled_message_box(
                QMessageBox.Icon.Warning,
                t("msg.save_failed"),
                t("msg.cannot_write_config", path=config_path)
            )
            return False
        except Exception as e:
            self.statusbar.showMessage(t("status.save_local_commands_error", error=str(e)), 3000)
            return False

    def _manage_local_presets(self):
        """打开本地预设管理对话框"""
        dialog = PresetDialog(self.local_presets, self, title=t("msg.manage_local_commands"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.local_presets = dialog.get_presets()
            if self._save_local_commands():
                self.statusbar.showMessage(t("status.local_preset_saved"), 3000)

    def _add_new_local_preset(self):
        """添加新本地预设"""
        dialog = PresetDialog(self.local_presets, self, auto_add=True, title=t("msg.manage_local_commands"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.local_presets = dialog.get_presets()
            if self._save_local_commands():
                self.statusbar.showMessage(t("status.local_preset_saved"), 3000)

    # ==================== 本地快速命令相关方法结束 ====================

    def closeEvent(self, event):
        """窗口关闭事件"""
        # 检查是否有任何终端在运行
        any_running = any(
            t.is_running()
            for terminals in self.tab_terminals.values()
            for t in terminals
        )

        if any_running:
            # 创建自定义样式的消息框，避免深色主题导致文字不可见
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle(t("msg.confirm_exit_title"))
            msg_box.setText(t("msg.confirm_exit_msg"))
            msg_box.setIcon(QMessageBox.Icon.Question)
            msg_box.setStandardButtons(
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No |
                QMessageBox.StandardButton.Cancel
            )
            # 设置明确的样式，确保文字可读
            msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #f0f0f0;
                }
                QMessageBox QLabel {
                    color: #333333;
                    font-size: 14px;
                }
                QMessageBox QPushButton {
                    background-color: #e0e0e0;
                    color: #333333;
                    border: 1px solid #999999;
                    padding: 5px 15px;
                    border-radius: 3px;
                    min-width: 60px;
                }
                QMessageBox QPushButton:hover {
                    background-color: #d0d0d0;
                }
                QMessageBox QPushButton:pressed {
                    background-color: #c0c0c0;
                }
            """)
            reply = msg_box.exec()

            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return

        # 停止所有 OpenAI API 服务器
        self.openai_server_manager.stop_all()

        # 停止定时器
        self.auto_save_timer.stop()
        self._log_timer.stop()

        # 完整清理所有终端资源
        for terminals in self.tab_terminals.values():
            for terminal in terminals:
                terminal.cleanup()

        # 保存配置（包括最后选中的预设）
        self._save_config()

        # 立即刷新窗口导航（窗口关闭时）
        if MainWindow._global_window_navigator is not None:
            navigator = MainWindow._global_window_navigator
            def refresh_navigator():
                # 安全检查：确保导航面板对象未被删除
                if MainWindow._global_window_navigator is not None and not sip.isdeleted(navigator):
                    navigator._refresh_window_list()
            QTimer.singleShot(100, refresh_navigator)

        event.accept()

    # ================== OpenAI API 服务器相关方法 ==================

    def _show_tab_context_menu(self, pos):
        """显示 Tab 右键菜单"""
        tab_bar = self.tab_widget.tabBar()
        tab_index = tab_bar.tabAt(pos)

        if tab_index < 0:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2d2d44;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 3px;
            }
            QMenu::item:selected {
                background-color: #667eea;
            }
            QMenu::item:disabled {
                color: #666;
            }
        """)

        # OpenAI API 服务器选项
        is_server_running = self.openai_server_manager.is_running(tab_index)

        if is_server_running:
            port = self.openai_server_manager.get_port(tab_index)
            stop_action = menu.addAction(t("openai.stop_server", port=port))
            stop_action.triggered.connect(lambda: self._stop_openai_server(tab_index))

            # 复制 API URL
            copy_url_action = menu.addAction(t("openai.copy_url"))
            copy_url_action.triggered.connect(lambda: self._copy_api_url(port))

            menu.addSeparator()

            # 每次 Query 后清除会话
            clear_after = self.api_server_clear_after_query.get(tab_index, False)
            clear_action = menu.addAction(t("openai.clear_session"))
            clear_action.setCheckable(True)
            clear_action.setChecked(clear_after)
            clear_action.triggered.connect(lambda checked: self._toggle_clear_after_query(tab_index, checked))
        else:
            start_action = menu.addAction(t("openai.set_as_server"))
            start_action.triggered.connect(lambda: self._show_openai_server_dialog(tab_index))

        menu.addSeparator()

        # 关闭标签页
        close_action = menu.addAction(t("tab.close"))
        close_action.triggered.connect(lambda: self._close_tab(tab_index))

        menu.exec(tab_bar.mapToGlobal(pos))

    def _show_openai_server_dialog(self, tab_index: int):
        """显示 OpenAI 服务器配置对话框"""
        # 获取建议的端口
        suggested_port = 8100 + tab_index

        port, ok = QInputDialog.getInt(
            self,
            t("openai.start_title"),
            t("openai.port_label"),
            suggested_port,  # value
            8100,  # min
            8199   # max
        )

        if ok:
            self._start_openai_server(tab_index, port)

    def _start_openai_server(self, tab_index: int, port: int = None):
        """启动 OpenAI 服务器"""
        terminals = self.tab_terminals.get(tab_index, [])
        if not terminals:
            QMessageBox.warning(self, t("msg.error"), t("msg.no_terminal_in_tab"))
            return

        terminal = terminals[0]  # 使用第一个终端

        try:
            actual_port = self.openai_server_manager.start_server(tab_index, terminal, port)
            self.statusbar.showMessage(
                t("status.openai_server_started", port=actual_port),
                10000
            )
        except Exception as e:
            QMessageBox.critical(self, t("msg.start_failed"), str(e))

    def _stop_openai_server(self, tab_index: int):
        """停止 OpenAI 服务器"""
        self.openai_server_manager.stop_server(tab_index)

    def _copy_api_url(self, port: int):
        """复制 API URL 到剪贴板"""
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        QApplication.clipboard().setText(url)
        self.statusbar.showMessage(t("status.url_copied", url=url), 3000)

    def _toggle_clear_after_query(self, tab_index: int, checked: bool):
        """切换是否每次 Query 后清除会话"""
        self.api_server_clear_after_query[tab_index] = checked
        # 更新服务器配置
        if tab_index in self.openai_server_manager.servers:
            server = self.openai_server_manager.servers[tab_index]
            server.config.clear_after_query = checked
        status = t("status.enabled") if checked else t("status.disabled")
        self.statusbar.showMessage(t("status.session_clear", status=status), 3000)

    def _on_openai_server_started(self, tab_index: int, port: int):
        """服务器启动回调"""
        # 更新标签页标题，添加服务器标记
        current_title = self.tab_widget.tabText(tab_index)
        if "[API:" not in current_title:
            self.tab_widget.setTabText(tab_index, f"[API:{port}] {current_title}")
        self.statusbar.showMessage(t("status.openai_server_running", port=port), 5000)

    def _on_openai_server_stopped(self, tab_index: int):
        """服务器停止回调"""
        # 移除标签页标题中的服务器标记
        if tab_index < self.tab_widget.count():
            current_title = self.tab_widget.tabText(tab_index)
            new_title = re.sub(r'\[API:\d+\]\s*', '', current_title)
            self.tab_widget.setTabText(tab_index, new_title)
        self.statusbar.showMessage(t("status.openai_server_stopped"), 3000)

    def _on_openai_server_error(self, tab_index: int, error: str):
        """服务器错误回调"""
        QMessageBox.warning(self, t("msg.server_error"), f"Tab {tab_index}: {error}")
