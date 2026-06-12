"""
主窗口
智能终端的GUI主界面
"""
import os
import re
import json
import sys
import time
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
    QStyleOptionViewItem, QStyleOptionButton, QSpinBox, QSizePolicy,
    QStackedWidget
)
from PyQt6 import sip  # 用于检查 C++ 对象是否已被删除
from PyQt6.QtCore import Qt, QTimer, QEvent, QPoint, QMimeData, pyqtSignal, QObject, QSize, QRectF, QVariantAnimation, QEasingCurve
from PyQt6.QtGui import QAction, QActionGroup, QIcon, QFont, QColor, QPixmap, QPainter, QPainterPath, QPen, QDrag, QCursor, QBrush, QPalette, QShortcut, QKeySequence
from PyQt6.QtWidgets import QWidgetAction, QStylePainter, QStyleOptionComboBox

from terminal_widget import TerminalWidget
from session_manager import SessionManager
from exporter import export_session
from history_dialog import HistoryDialog
from openai_server import OpenAIServerManager
from git_widget import GitPanel, GitDiffView, GitOutputView, _make_git_tool_icon
from remote_explorer_widget import RemoteExplorerPanel
from explorer_widget import ExplorerPanel
from toolbar_manager import ToolbarManagerDialog
from command_palette import CommandPalette
from file_editor import FileEditorWidget, EditorArea
from i18n import t, set_language, get_language
from flow_layout import FlowLayout
from utils import read_config_json, atomic_write_json, get_config_path
from app_logging import get_logger

logger = get_logger(__name__)
import shutil
import subprocess
from widgets import (
    SelectAllLineEdit, QuietPopupComboBox, CenteredComboBox,
    InlineRenameEdit, DetachableTabBar, DetachedWindow,
    _ToolbarCheckBox, _FlowSeparator, _NavResizeHandle,
)
from dialogs import (
    get_default_shell, PresetDialog, LLMConfigDialog,
    DirectoryHistoryDialog, _ShortcutCaptureButton, ShortcutSettingsDialog,
    ShortcutCheatSheetDialog,
)


# 导航条目的「执行完毕」提醒标记角色（绿点）
NAV_ATTENTION_ROLE = Qt.ItemDataRole.UserRole + 1


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

        # 「执行完毕」提醒：在条目右侧画一个绿色小圆点
        if index.data(NAV_ATTENTION_ROLE):
            r = option.rect
            d = 8
            cx = r.right() - d - 6
            cy = r.center().y()
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#2ecc71'))
            painter.drawEllipse(QPoint(cx, cy), d // 2, d // 2)
            painter.restore()


class WindowNavigatorPanel(QWidget):
    """窗口快速导航面板 - 永远在最前的小窗口"""

    window_switch_requested = pyqtSignal(object)  # 请求切换到某个窗口
    panel_closed = pyqtSignal()  # 面板关闭信号

    def __init__(self, parent=None, embedded=False):
        # embedded=True：作为普通子控件嵌入到窗口左侧栏（无独立窗口标志、无标题栏）；
        # embedded=False：原来的浮动置顶小窗口。
        self._embedded = embedded
        if embedded:
            super().__init__(parent)
        else:
            super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
            self.setWindowTitle(t("window.navigator_title"))
            self.setMinimumSize(200, 150)
            self.resize(250, 300)

        # 排序方式: 'time' (按创建时间) / 'name' (按名称) / 'manual' (手动排序)
        self._sort_mode = 'time'
        # 简洁显示模式：只显示文件夹名
        self._compact_mode = True
        # 快速关闭：勾选后右键"强制关闭"跳过确认弹窗
        self._quick_close = False
        # 手动排序的窗口顺序（存储窗口ID）
        self._manual_order = []

        # QListWidgetItem 的 UserRole 只存 id(window)（Python int），不直接存 QObject 指针。
        # 否则 force-close 时 Qt 在 dispatchEnterLeave 等回调里读取 item.data() 会
        # 触发 sip 把已释放的 C++ 指针转回 Python → EXC_BAD_ACCESS。
        # 这个 dict 把 id 映射回当前活着的 MainWindow（弱引用，避免阻止 GC）。
        import weakref as _weakref
        self._window_refs: "dict[int, _weakref.ReferenceType]" = {}

        self._setup_ui()
        self._apply_style()
        self._load_navigator_config()

        # 新建面板时继承当前已存在面板的排序模式与手动顺序，
        # 使多窗口列表保持一致（_manual_order 存 id(window)，跨窗口可直接复用）。
        try:
            for nav in MainWindow._iter_navigators():
                if nav is self:
                    continue
                self._sort_mode = nav._sort_mode
                self._manual_order = list(nav._manual_order)
                self.drag_hint_label.setVisible(self._sort_mode == 'manual')
                break
        except Exception:
            pass

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
        # 内嵌模式下让列表保持紧凑（顶部一小条），把纵向空间留给下方的文件面板；
        # 窗口很多时列表内部滚动。高度可由底部手柄拖拽调整（见 set_embedded_list_height）。
        self.EMBED_LIST_MIN_H = 60
        self._embedded_list_height = 180
        if self._embedded:
            self.window_list.setFixedHeight(self._embedded_list_height)
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
        # 右键菜单：强制关闭等
        self.window_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.window_list.customContextMenuRequested.connect(self._show_window_context_menu)
        layout.addWidget(self.window_list)

        # 简洁显示 + 字体大小行
        compact_row = QHBoxLayout()
        compact_row.setSpacing(8)

        # 三个勾选框共用的深色样式：必须把 ::indicator 的外观（边框/背景/选中态）
        # 全部定义掉，否则 Qt 会退回 Windows 原生风格画指示器，浅色高亮叠在
        # 深色主题上非常突兀（hover 时尤其明显）。
        nav_checkbox_style = """
            QCheckBox {
                color: #aaaaaa;
                font-size: 11px;
                border: none;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
                border: 2px solid #3d3d5c;
                border-radius: 3px;
                background-color: #16213e;
            }
            QCheckBox::indicator:hover {
                border-color: #667eea;
            }
            QCheckBox::indicator:checked {
                border-color: #667eea;
                background-color: #667eea;
            }
        """

        self.compact_checkbox = QCheckBox(t("window.compact_display"))
        self.compact_checkbox.setChecked(True)  # 默认开启简洁显示
        self.compact_checkbox.setToolTip(t("window.compact_tooltip"))
        self.compact_checkbox.setStyleSheet(nav_checkbox_style)
        self.compact_checkbox.stateChanged.connect(self._toggle_compact_mode)
        compact_row.addWidget(self.compact_checkbox)

        # Quick Close 勾选框：勾选后右键"强制关闭"不再弹确认窗
        self.quick_close_checkbox = QCheckBox(t("window.quick_close"))
        self.quick_close_checkbox.setChecked(self._quick_close)
        self.quick_close_checkbox.setToolTip(t("window.quick_close_tooltip"))
        self.quick_close_checkbox.setStyleSheet(nav_checkbox_style)
        self.quick_close_checkbox.stateChanged.connect(self._on_quick_close_changed)
        compact_row.addWidget(self.quick_close_checkbox)

        # 「嵌入到侧栏」勾选框：勾选=内嵌到各窗口左侧栏；取消=独立浮动窗口。自动记住。
        self.embed_checkbox = QCheckBox(t("window.embed_checkbox"))
        self.embed_checkbox.setToolTip(t("window.embed_checkbox_tooltip"))
        self.embed_checkbox.setStyleSheet(nav_checkbox_style)
        self.embed_checkbox.blockSignals(True)
        self.embed_checkbox.setChecked(self._embedded)
        self.embed_checkbox.blockSignals(False)
        self.embed_checkbox.stateChanged.connect(self._on_embed_checkbox_changed)
        compact_row.addWidget(self.embed_checkbox)

        compact_row.addStretch()

        # 字体大小调节
        self._font_size = 12  # 默认字体大小
        font_size_label = QLabel("A")
        font_size_label.setStyleSheet("color: #aaaaaa; font-size: 11px; border: none;")
        compact_row.addWidget(font_size_label)

        # 用下拉框（CenteredComboBox）代替 SpinBox：8–24px
        self.font_size_spin = CenteredComboBox()
        self.font_size_spin.setToolTip(t("window.font_size_tooltip"))
        self.font_size_spin.setFixedWidth(68)
        self.font_size_spin.setMinimumPopupWidth(68)
        for _px in range(8, 25):
            self.font_size_spin.addItem(f"{_px}px", _px)
        # 弹窗高度取选项总数的 2/3（其余项靠滚轮/触控板滚动访问）。
        self.font_size_spin.setMaxVisibleItems(
            max(1, self.font_size_spin.count() * 2 // 3))
        _fs_idx = self.font_size_spin.findData(self._font_size)
        self.font_size_spin.setCurrentIndex(_fs_idx if _fs_idx >= 0 else 0)
        self.font_size_spin.setStyleSheet("""
            QComboBox {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 3px;
                padding: 1px 6px;
                color: #eaeaea;
                font-size: 11px;
                combobox-popup: 0;
            }
            QComboBox:hover {
                border-color: #667eea;
            }
            QComboBox::drop-down {
                border: none;
                width: 16px;
            }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #eaeaea;
                selection-background-color: #667eea;
                selection-color: #ffffff;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                outline: none;
                padding: 4px;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 0px 6px;
                border-radius: 4px;
            }
        """)
        self.font_size_spin.currentIndexChanged.connect(self._on_font_size_changed)
        compact_row.addWidget(self.font_size_spin)

        # 设置按钮（小齿轮）：与 Compact/Quick Close/Embed/字号 同在一行，靠最右。
        # 用矢量绘制的齿轮图标（_make_git_tool_icon），避免 macOS 上 ⚙ 字形被渲染成
        # 彩色 emoji / 小点，保证清晰统一。
        self.settings_btn = QPushButton()
        self.settings_btn.setIcon(_make_git_tool_icon('gear', '#c8c8d8', 16))
        self.settings_btn.setIconSize(QSize(16, 16))
        self.settings_btn.setToolTip(t("window.settings_tooltip"))
        self.settings_btn.setFixedSize(28, 24)
        self.settings_btn.setStyleSheet("""
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
        """)
        self.settings_btn.clicked.connect(self._show_settings_menu)
        compact_row.addWidget(self.settings_btn)

        layout.addLayout(compact_row)

        # 拖拽提示标签（默认隐藏）
        self.drag_hint_label = QLabel(t("window.drag_hint"))
        self.drag_hint_label.setStyleSheet("color: #667eea; font-size: 10px; border: none;")
        self.drag_hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drag_hint_label.setVisible(False)
        layout.addWidget(self.drag_hint_label)

    def _show_settings_menu(self):
        """弹出设置菜单：排序方式（时间/名称/手动）+ 刷新。"""
        menu = QMenu(self)
        title = menu.addAction(t("window.sort_menu_label"))
        title.setEnabled(False)
        for mode, label in (
            ('time', t("window.sort_time")),
            ('name', t("window.sort_name")),
            ('manual', t("window.sort_manual")),
        ):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._sort_mode == mode)
            act.triggered.connect(lambda _checked=False, m=mode: self._set_sort_mode(m))
        menu.addSeparator()
        refresh_act = menu.addAction(t("window.refresh"))
        refresh_act.triggered.connect(self._force_refresh)
        menu.exec(self.settings_btn.mapToGlobal(QPoint(0, self.settings_btn.height())))

    def _on_embed_checkbox_changed(self, state):
        """勾选/取消「嵌入到侧栏」：在内嵌与浮动之间切换停靠方式（全局，自动记住）。"""
        checked = (state == Qt.CheckState.Checked.value)
        MainWindow._set_navigator_dock_mode('embed' if checked else 'float')

    def _set_sort_mode(self, mode: str):
        """设置排序方式（时间/名称/手动），更新拖拽提示并刷新列表。"""
        if mode not in ('time', 'name', 'manual'):
            return
        self._sort_mode = mode
        self.drag_hint_label.setVisible(mode == 'manual')
        if mode == 'manual':
            self._save_manual_order()
        # 同步排序模式到所有窗口的导航面板并刷新（_broadcast_sort_state 也会刷新自身）
        self._broadcast_sort_state()

    def _on_rows_moved(self):
        """拖拽排序完成后自动切换到手动模式并保存顺序"""
        if self._sort_mode != 'manual':
            self._sort_mode = 'manual'
            self.drag_hint_label.setVisible(True)
        self._save_manual_order()
        # 同步到所有窗口的导航面板（含自身，用于刷新序号前缀）。
        # 延迟到事件循环下一拍执行，避免在 rowsMoved 回调内 clear/重建模型导致重入。
        QTimer.singleShot(0, self._broadcast_sort_state)

    def _broadcast_sort_state(self):
        """把当前排序模式 + 手动顺序同步到所有导航面板并刷新，使多窗口列表保持一致。"""
        for nav in MainWindow._iter_navigators():
            try:
                if nav is not self:
                    nav._sort_mode = self._sort_mode
                    nav._manual_order = list(self._manual_order)
                    try:
                        nav.drag_hint_label.setVisible(self._sort_mode == 'manual')
                    except Exception:
                        pass
                nav._force_refresh()
            except Exception:
                pass

    def _resolve_window(self, item):
        """从 QListWidgetItem 安全地拿回对应的 MainWindow。

        永远只读 item.data() 中的 id（Python int），再用 weakref 字典查活的对象。
        这样即使底层 C++ 已被释放，也只会拿到 None 而不会段错误。
        """
        if item is None:
            return None
        wid = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(wid, int):
            return None
        ref = self._window_refs.get(wid)
        if ref is None:
            return None
        window = ref()
        if window is None:
            # 弱引用已失效 → 清理映射
            self._window_refs.pop(wid, None)
            return None
        try:
            if sip.isdeleted(window):
                self._window_refs.pop(wid, None)
                return None
        except Exception:
            return None
        return window

    def _save_manual_order(self):
        """保存当前列表顺序"""
        self._manual_order = []
        for i in range(self.window_list.count()):
            item = self.window_list.item(i)
            wid = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(wid, int):
                self._manual_order.append(wid)

    def _toggle_compact_mode(self, state):
        """切换简洁显示模式（所有窗口的导航面板共用此设置）"""
        compact = (state == Qt.CheckState.Checked.value)
        self._compact_mode = compact
        self._force_refresh()
        self._save_navigator_config()
        # 广播到其它导航面板（浮动 + 各窗口内嵌），保持全局一致
        for nav in MainWindow._iter_navigators():
            if nav is self:
                continue
            try:
                nav._apply_compact_mode(compact)
            except Exception:
                pass

    def _apply_compact_mode(self, compact: bool):
        """同步外部设置的简洁显示状态：更新复选框、内部标志并刷新列表。"""
        if self._compact_mode == compact and self.compact_checkbox.isChecked() == compact:
            return
        self._compact_mode = compact
        self.compact_checkbox.blockSignals(True)
        self.compact_checkbox.setChecked(compact)
        self.compact_checkbox.blockSignals(False)
        self._force_refresh()

    def _on_font_size_changed(self, _index=None):
        """字体大小变更"""
        size = self.font_size_spin.currentData()
        if size is None:
            return
        self._font_size = size
        self._apply_list_font_size()
        self._save_navigator_config()

    def _apply_list_font_size(self):
        """应用字体大小到列表"""
        self.window_list.setStyleSheet(f"""
            QListWidget {{
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                font-size: {self._font_size}px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 8px;
                border-bottom: 1px solid #2d2d44;
                border-radius: 4px;
            }}
            QListWidget::item:selected {{
                background-color: #2d2d44;
            }}
        """)

    def _extract_folder_name(self, title: str, window=None) -> str:
        """从窗口标题中提取文件夹名

        标题格式通常为: "预设名-文件夹名 - Smart Terminal #N" 或 "预设名-文件夹名"
        提取最后一个有效的文件夹名部分。
        如果无法提取，则从窗口的工作目录获取。
        SSH 远程会话格式为 "SSH: {host}"，需保留完整远程主机名。
        """
        # 先去掉 " - Smart Terminal" 后缀
        if " - Smart Terminal" in title:
            title = title.split(" - Smart Terminal")[0]

        # SSH 远程会话：标题格式 "SSH: {host}"，host 本身可能含 "-"
        # （例如 Zhiyuan-Ubuntu-Server），必须整段保留，不能再按 "-" 拆。
        if title.startswith("SSH: "):
            return title[len("SSH: "):].strip() or title.strip()

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

    def set_embedded_list_height(self, h):
        """内嵌模式下设置窗口列表高度（拖拽手柄调用）。返回实际生效的高度。"""
        if not self._embedded:
            return self._embedded_list_height
        h = max(self.EMBED_LIST_MIN_H, int(h))
        self._embedded_list_height = h
        self.window_list.setFixedHeight(h)
        return h

    def embedded_list_height(self):
        return self._embedded_list_height

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
            except Exception:
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
            except Exception:
                # 窗口已被删除（或其它读取异常）
                self._refresh_window_list()
                return

        # 检查窗口标题或颜色是否变化
        try:
            current_info = [(w.windowTitle(), w.get_window_color()) for w in current_windows]
        except Exception:
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

        # 获取所有 MainWindow 实例（排除已删除/正在关闭的窗口）
        # 强制关闭中的窗口已 hide() 但 sip 对象还未销毁 —— 它正处于不稳定状态，
        # 任何读取（windowTitle、x()、get_window_color()）都可能与底层
        # NSWindow detach/deleteLater 重入 → 段错误。
        windows = []
        for w in app.topLevelWidgets():
            try:
                if not isinstance(w, MainWindow):
                    continue
                if sip.isdeleted(w):
                    continue
                # 跳过正在关闭/已隐藏的窗口
                if getattr(w, '_closing_in_progress', False):
                    continue
                if getattr(w, '_force_closing', False):
                    continue
                if not w.isVisible():
                    continue
                windows.append(w)
            except Exception:
                continue  # 窗口已被删除/不稳定，跳过

        # 根据排序模式排序（包一层 try：window 可能在 sort key 取值时被销毁）
        try:
            if self._sort_mode == 'time':
                windows.sort(key=lambda w: w.get_created_time())
            elif self._sort_mode == 'name':
                windows.sort(key=lambda w: w.windowTitle())
            elif self._sort_mode == 'manual' and self._manual_order:
                # 手动排序：按保存的顺序排列，新窗口放到末尾
                order_map = {wid: idx for idx, wid in enumerate(self._manual_order)}
                windows.sort(key=lambda w: order_map.get(id(w), 9999))
        except Exception:
            # 排序过程中有窗口被删除，重置缓存并下次再刷
            self._last_window_info = []
            return

        # 检查是否有变化（标题或颜色）
        try:
            current_info = [(w.windowTitle(), w.get_window_color(), bool(getattr(w, '_nav_attention', False))) for w in windows]
        except Exception:
            # 窗口在遍历过程中被删除，重新刷新
            self._last_window_info = []
            return
        if current_info == self._last_window_info:
            return  # 没有变化，跳过更新

        # 更新缓存
        self._last_window_info = current_info
        self._cached_windows = windows

        # 记录当前选中（活动）窗口的 id，重建后据此恢复高亮，
        # 否则像切换 Compact 这类强制刷新会把高亮重置到第一项
        prev_item = self.window_list.currentItem()
        prev_id = prev_item.data(Qt.ItemDataRole.UserRole) if prev_item else None

        # 阻止信号以避免不必要的 UI 更新
        self.window_list.blockSignals(True)

        # 更新列表
        self.window_list.clear()
        # 重建 id → weakref 映射；旧的失效项会自然淘汰
        import weakref as _weakref
        new_refs: dict = {}
        for idx, window in enumerate(windows, 1):
            try:
                if sip.isdeleted(window):
                    continue
                title = window.windowTitle()
                # 简洁模式：提取文件夹名
                if self._compact_mode:
                    display_title = self._extract_folder_name(title, window)
                else:
                    display_title = title
                display_title = f"{idx}. {display_title}"
                color = window.get_window_color()
                item = QListWidgetItem(display_title)
                wid = id(window)
                # 只把 id（Python int）塞进 UserRole；真正的对象通过 weakref 解
                item.setData(Qt.ItemDataRole.UserRole, wid)
                item.setData(NAV_ATTENTION_ROLE, bool(getattr(window, '_nav_attention', False)))
                new_refs[wid] = _weakref.ref(window)
                item.setForeground(QColor(color))
                self.window_list.addItem(item)
            except Exception:
                continue  # 窗口在处理过程中被删除/不稳定，跳过
        self._window_refs = new_refs

        self.window_list.blockSignals(False)

        # 恢复之前选中（活动）窗口的高亮，找不到时回退到第一项
        if self.window_list.count() > 0:
            target_row = 0
            if prev_id is not None:
                for i in range(self.window_list.count()):
                    it = self.window_list.item(i)
                    if it and it.data(Qt.ItemDataRole.UserRole) == prev_id:
                        target_row = i
                        break
            self.window_list.setCurrentRow(target_row)
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
        window = self._resolve_window(item)
        if window is not None:
            self._switch_to_window(window)

    def _on_item_entered(self, item):
        """鼠标进入某个项时"""
        self._hovered_item = item
        self._update_all_item_colors()

    def eventFilter(self, obj, event):
        """事件过滤器，处理鼠标离开列表

        注意：eventFilter 由 Qt 从 C++ 侧直接调用，一旦抛出未捕获异常，
        PyQt6 会调用 qFatal() abort 整个进程（闪退）。所以这里整体兜底，
        颜色刷新失败绝不能让事件过滤器抛出异常。
        """
        try:
            if obj == self.window_list.viewport() and event.type() == QEvent.Type.Leave:
                self._hovered_item = None
                self._update_all_item_colors()
        except Exception:
            pass
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
            window = self._resolve_window(item)
            if window is None:
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
            except Exception:
                # 窗口已被删除或处于不稳定状态（不止 RuntimeError）——
                # 这是非关键的 UI 刷新，任何异常都直接跳过该项，
                # 绝不能让它逃逸到 Qt 回调外触发 abort 闪退
                continue

    def _on_item_double_clicked(self, item):
        """双击切换窗口"""
        window = self._resolve_window(item)
        if window is not None:
            self._switch_to_window(window)

    def _show_window_context_menu(self, pos):
        """窗口列表右键菜单：提供强制关闭等操作"""
        item = self.window_list.itemAt(pos)
        if item is None:
            return
        window = self._resolve_window(item)
        if window is None:
            return
        if not hasattr(window, 'force_close_with_save'):
            return  # 非 MainWindow 类型，不支持

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

        force_close_action = menu.addAction(t("window.force_close"))
        force_close_action.setToolTip(t("window.force_close_tooltip"))

        chosen = menu.exec(self.window_list.viewport().mapToGlobal(pos))
        if chosen is force_close_action:
            self._force_close_window(window, item.text())

    def _on_quick_close_changed(self, state):
        """Quick Close 勾选状态变化：更新内部标志、保存配置并广播到所有导航面板。

        与 Compact 一样是全窗口公用的设置：内嵌模式下每个窗口各有一个导航面板，
        必须广播，否则只有当前窗口的勾选会变。
        """
        self._quick_close = self.quick_close_checkbox.isChecked()
        self._save_navigator_config()
        # 广播到其它导航面板（浮动 + 各窗口内嵌），保持全局一致
        for nav in MainWindow._iter_navigators():
            if nav is self:
                continue
            try:
                nav._apply_quick_close(self._quick_close)
            except Exception:
                pass

    def _apply_quick_close(self, quick_close: bool):
        """同步外部设置的 Quick Close 状态：更新复选框与内部标志。"""
        if self._quick_close == quick_close and self.quick_close_checkbox.isChecked() == quick_close:
            return
        self._quick_close = quick_close
        self.quick_close_checkbox.blockSignals(True)
        self.quick_close_checkbox.setChecked(quick_close)
        self.quick_close_checkbox.blockSignals(False)

    def _force_close_window(self, window, title):
        """对一个窗口执行强制关闭（自动保存）。
        - 未勾选 Quick Close：弹窗确认后再执行
        - 勾选了 Quick Close：直接执行，不弹窗

        关闭动作通过 QTimer.singleShot(80, ...) 推迟执行 —— 否则在 Quick Close
        路径下我们仍处于右键菜单 exec() 的调用栈内，同步触发窗口销毁链
        （terminal.cleanup() 会 deleteLater 大量子 widget）会在 macOS Qt 上
        发生重入崩溃（菜单尚未完全清理 → 子 widget 提前销毁 → 段错误）。

        早先用过 singleShot(0)，但 macOS 上 native menu 的 NSPopupMenu 清理
        会跨越多个 runloop 迭代，0ms 仍可能与菜单清理重叠 → 段错误。
        80ms 足够让 NSApp 完成菜单 dismiss + widget deferred deletion 后再启动
        关窗流程；force_close_with_save 内部还会再做一次 hide + 异步真关闭，
        把 macOS NSWindow detach 与 Qt widget cleanup 隔离开。
        """
        if not window or sip.isdeleted(window):
            return

        if not self._quick_close:
            # 父窗口（导航面板）有深色 QWidget 全局 QSS，会级联进 QMessageBox 把
            # 文字染暗、看不清。这里：
            # 1. 不传 parent，避免 QSS 级联
            # 2. 自己组合一套深色主题的 QSS，与导航面板风格一致
            msg_box = QMessageBox()
            msg_box.setWindowTitle(t("window.force_close_confirm_title"))
            msg_box.setText(t("window.force_close_confirm_msg", title=title))
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            msg_box.setDefaultButton(QMessageBox.StandardButton.No)
            msg_box.setStyleSheet("""
                QMessageBox {
                    background-color: #1a1a2e;
                }
                QMessageBox QLabel {
                    color: #eaeaea;
                    background-color: transparent;
                    font-size: 13px;
                    border: none;
                }
                QMessageBox QPushButton {
                    background-color: #3d3d5c;
                    color: #eaeaea;
                    border: 1px solid #3d3d5c;
                    border-radius: 4px;
                    padding: 6px 18px;
                    min-width: 72px;
                    font-size: 12px;
                }
                QMessageBox QPushButton:hover {
                    background-color: #667eea;
                    border-color: #667eea;
                }
                QMessageBox QPushButton:default {
                    background-color: #2d2d44;
                    border-color: #667eea;
                }
            """)
            # 让弹窗显示在导航面板附近
            geo = self.geometry()
            msg_box.move(geo.x() + max(0, (geo.width() - 360) // 2),
                         geo.y() + max(0, (geo.height() - 180) // 2))
            if msg_box.exec() != QMessageBox.StandardButton.Yes:
                return

        # 推迟一拍：等当前事件处理（包括右键菜单的 exec）完全退栈再真正关窗
        # 用 weakref 持有 navigator，避免 do_close 持有强引用导致刷新逻辑
        # 在 navigator 已被销毁时仍试图访问
        import weakref
        nav_ref = weakref.ref(self)

        def do_close():
            try:
                if window and not sip.isdeleted(window):
                    window.force_close_with_save()
            except RuntimeError:
                # 窗口已被销毁
                pass
            except Exception as e:
                logger.warning(f"[ForceClose] do_close failed: {e}")
            # 关闭完成后再刷新列表（navigator 仍存活时）
            def safe_refresh():
                nav = nav_ref()
                if nav is not None and not sip.isdeleted(nav):
                    try:
                        nav._refresh_window_list()
                    except Exception:
                        # 由 QTimer 回调，异常绝不能逃逸（否则 PyQt6 abort 闪退）
                        pass
            QTimer.singleShot(200, safe_refresh)

        # 80ms 足以让 macOS native menu 完成 dismiss 与 popup widget 的
        # deferred deletion 后再启动关窗。0ms 在 macOS 上仍可能与菜单清理重叠。
        QTimer.singleShot(80, do_close)

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

    def hideEvent(self, event):
        """隐藏时保存设置"""
        self._save_navigator_config()
        super().hideEvent(event)

    def _save_navigator_config(self):
        """保存导航面板设置到主配置文件"""
        try:
            config_file = get_config_path()
            existing, ok = read_config_json(config_file)
            # 文件存在但解析失败：可能正被别的进程写到一半，放弃本次保存，
            # 否则会把对方的更改（如 git_proxy）当作"已损坏"全部覆盖掉。
            if not ok:
                return
            # 内嵌模式下 self.x()/y() 是控件在父布局里的坐标，没有意义，不写几何
            if not self._embedded:
                existing['navigator_geometry'] = [self.x(), self.y(), self.width(), self.height()]
            existing['navigator_font_size'] = self._font_size
            existing['navigator_quick_close'] = bool(self._quick_close)
            existing['navigator_compact'] = bool(self._compact_mode)
            atomic_write_json(config_file, existing)
        except Exception:
            pass

    def _load_navigator_config(self):
        """从主配置文件加载导航面板设置"""
        try:
            config_file = get_config_path()
            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                # 恢复字体大小
                font_size = config.get('navigator_font_size', 12)
                if 8 <= font_size <= 24:
                    self._font_size = font_size
                    _idx = self.font_size_spin.findData(font_size)
                    if _idx >= 0:
                        self.font_size_spin.blockSignals(True)
                        self.font_size_spin.setCurrentIndex(_idx)
                        self.font_size_spin.blockSignals(False)
                    self._apply_list_font_size()
                # 恢复 Quick Close 偏好
                quick_close = bool(config.get('navigator_quick_close', False))
                self._quick_close = quick_close
                self.quick_close_checkbox.blockSignals(True)
                self.quick_close_checkbox.setChecked(quick_close)
                self.quick_close_checkbox.blockSignals(False)
                # 恢复简洁显示偏好（所有窗口共用，默认开启）
                compact = bool(config.get('navigator_compact', True))
                self._compact_mode = compact
                self.compact_checkbox.blockSignals(True)
                self.compact_checkbox.setChecked(compact)
                self.compact_checkbox.blockSignals(False)
                # 恢复窗口位置和大小（仅浮动模式）
                geo = config.get('navigator_geometry')
                if not self._embedded and geo and len(geo) == 4:
                    x, y, w, h = geo
                    # 确保窗口在屏幕可见范围内
                    from PyQt6.QtWidgets import QApplication
                    screen = QApplication.primaryScreen()
                    if screen:
                        screen_rect = screen.availableGeometry()
                        if (x + w > 0 and x < screen_rect.width() and
                                y + h > 0 and y < screen_rect.height()):
                            self.setGeometry(x, y, max(w, 200), max(h, 150))
        except Exception:
            pass

    def closeEvent(self, event):
        """关闭时保存设置、停止定时器并发送关闭信号"""
        self._save_navigator_config()
        self._refresh_timer.stop()
        self.panel_closed.emit()
        super().closeEvent(event)

    def select_window(self, window):
        """选中指定的窗口项

        当某个窗口被激活时调用此方法，更新列表的选中状态。
        """
        target_id = id(window) if window is not None else None
        if target_id is None:
            return
        for i in range(self.window_list.count()):
            item = self.window_list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == target_id:
                self.window_list.blockSignals(True)
                self.window_list.setCurrentRow(i)
                self.window_list.blockSignals(False)
                self._update_all_item_colors()
                break


class MainWindow(QMainWindow):
    """主窗口"""

    # 配置文件路径（源码运行=项目目录；打包运行=平台用户数据目录，见 utils.get_data_dir）
    CONFIG_FILE = get_config_path()

    # 本地快速命令配置
    LOCAL_CONFIG_DIR = ".sterminal"
    LOCAL_COMMANDS_FILE = "quick_commands.json"

    # 工具栏小下拉框（GUI 字号 / 透明度等）通用样式，与主题下拉框一致
    _COMBO_STYLE = """
        QComboBox {
            background-color: #16213e;
            border: 1px solid #3d3d5c;
            border-radius: 4px;
            padding: 4px 8px;
            color: #eaeaea;
            combobox-popup: 0;
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
            selection-color: #ffffff;
            border: 1px solid #3d3d5c;
            border-radius: 4px;
            outline: none;
            padding: 4px;
        }
        QComboBox QAbstractItemView::item {
            min-height: 28px;
            padding: 0px 6px;
            border-radius: 4px;
        }
    """

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

    # 拖拽分离时鼠标在新窗口内的抓取点：X 在标题栏中间偏左，Y 在标题栏中间。
    # 初始摆放与跟随轮询共用，必须一致，否则窗口会在拖拽开始时跳一下。
    _DETACH_GRAB_X = 200
    _DETACH_GRAB_Y = 15

    # 全局共享的窗口导航面板
    _global_window_navigator = None
    # 导航面板停靠方式：'float'=独立浮动窗口（默认）；'embed'=嵌入每个窗口左侧栏
    _navigator_dock_mode = 'float'

    # 左侧栏宽度（进程级共享）：只要打开侧边栏，所有窗口共用同一宽度，
    # 在一个窗口里拖动调宽，其它已打开窗口下次展开侧边栏时也用这个宽度，
    # 减轻窗口间切换的认知负担。通过 _saved_left_panel_width 属性读写。
    _shared_left_panel_width = None

    # QApplication 全局 stylesheet 原值快照（进程级共享，仅初始化一次）
    _original_app_stylesheet = None

    @property
    def _saved_left_panel_width(self):
        """左侧栏记忆宽度：所有窗口共用同一份（进程级），见 _shared_left_panel_width。"""
        return MainWindow._shared_left_panel_width

    @_saved_left_panel_width.setter
    def _saved_left_panel_width(self, value):
        MainWindow._shared_left_panel_width = value

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
        self._presets_modified = False  # 标记预设是否在本窗口中被修改（防止多窗口覆盖）
        self._llm_configs_modified = False  # 标记 LLM API 配置是否在本窗口中被修改（防止多窗口覆盖）
        self.local_presets = []  # 本地快速命令列表（目录级别）
        self.pending_commands = []  # 待执行的命令队列
        self.current_theme = "深蓝"  # 当前主题名称
        self._use_icon_tint = False  # 是否给图标添加主题色蒙版
        self._global_zoom_delta = 0  # 全局缩放偏移量（相对于默认字体大小）
        self._gui_font_size = 0  # GUI 字体大小（0 表示跟随全局缩放）
        self._original_widget_styles = {}  # {id(widget): (weakref, original_stylesheet)}
        self._pin_toolbar_row2 = False  # 是否固定显示第二排工具栏
        self._window_opacity = 100  # 窗口透明度百分比（10-100）

        # 多标签页支持
        self.tab_counter = 0  # 标签页计数器
        self.tab_sessions = {}  # {tab_index: session} 映射
        self.tab_splitters = {}  # {tab_index: QSplitter} 映射
        self.tab_terminals = {}  # {tab_index: [terminal_list]} 映射
        self.tab_cwds = {}  # {tab_index: str} 每个标签页独立的工作目录
        self.active_terminal = None  # 当前活动的终端
        self.detached_windows = []  # 分离出的独立窗口列表

        # 分隔条拖拽期间的终端快速渲染：连续 splitterMoved 视为一次「拖拽流」，
        # 期间终端缩放旧缓存（与弹簧动画同一机制），静默 160ms 后恢复清晰渲染。
        # 否则每拖一像素都整屏重建字符，拖侧边栏明显卡顿。
        self._splitter_drag_active = False
        self._splitter_drag_settle = QTimer(self)
        self._splitter_drag_settle.setSingleShot(True)
        self._splitter_drag_settle.setInterval(160)
        self._splitter_drag_settle.timeout.connect(self._end_splitter_drag_fast_resize)

        # 左侧栏宽度跨窗口同步的节流：拖拽时每像素都广播会让其它窗口每像素整窗重排,
        # 改为合并 80ms 内的变更、只推最新值（松手后最终宽度仍会被推到）。
        self._left_width_broadcast_pending = None
        self._left_width_broadcast_timer = QTimer(self)
        self._left_width_broadcast_timer.setSingleShot(True)
        self._left_width_broadcast_timer.setInterval(80)
        self._left_width_broadcast_timer.timeout.connect(self._flush_left_width_broadcast)

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

        # 本会话粘贴过的图片路径（按粘贴顺序，供“图片”按钮查看）
        self._pasted_images = []

        # OpenAI API 服务器管理器
        self.openai_server_manager = OpenAIServerManager()
        self.openai_server_manager.server_started.connect(self._on_openai_server_started)
        self.openai_server_manager.server_stopped.connect(self._on_openai_server_stopped)
        self.openai_server_manager.server_error.connect(self._on_openai_server_error)
        # API 服务器设置：每个 tab 是否启用 "每次 Query 后清除会话"
        self.api_server_clear_after_query = {}  # type: dict[int, bool]

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
        self._setup_window_menu()
        MainWindow._install_backtick_monitor()  # AppKit 级截获 Cmd+`，覆盖系统不稳定的原生循环
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

        # 恢复 GUI 字体大小下拉框
        if hasattr(self, 'gui_font_spin') and self._gui_font_size != 0:
            self._select_combo_value(self.gui_font_spin, self._gui_font_size)

        # 恢复窗口透明度
        if hasattr(self, 'opacity_spin') and self._window_opacity != 100:
            self._select_combo_value(self.opacity_spin, self._window_opacity)
            self.setWindowOpacity(self._window_opacity / 100.0)

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
        if hasattr(self, '_remote_split_checkbox') and getattr(self, '_remote_split_horizontal', False):
            self._remote_split_checkbox.blockSignals(True)
            self._remote_split_checkbox.setChecked(True)
            self._remote_split_checkbox.blockSignals(False)

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
            # 恢复 Window Navigator 开关状态：勾选复选框即触发 _on_window_nav_changed，
            # 按浮动/内嵌模式打开对应导航。延迟到事件循环就绪后再开，避免浮动面板
            # 在主窗口几何尚未稳定时定位异常。
            if self._saved_navigator_enabled and hasattr(self, 'window_nav_checkbox'):
                def _restore_nav():
                    if sip.isdeleted(self) or not hasattr(self, 'window_nav_checkbox'):
                        return
                    if not self.window_nav_checkbox.isChecked():
                        self.window_nav_checkbox.setChecked(True)
                QTimer.singleShot(0, _restore_nav)

        # macOS 原生窗口标志（已在 __init__ 开头初始化）

        # 延迟强制固定工具栏可见性（确保 Qt 布局完成后生效）
        if self._pin_toolbar_row2:
            def _force_pinned_rows_visible():
                if not sip.isdeleted(self):
                    self._set_pinned_toolbars_visible(True)
                    self._update_flow_toolbar_height()
            QTimer.singleShot(0, _force_pinned_rows_visible)

        # 启动后空闲预热 Explorer：把“首次 ⌘1 才触发”的目录扫描 + macOS 系统
        # 图标库初始化提前在后台做掉，使首次展开瞬开。若面板已恢复为打开状态则跳过。
        if not getattr(self, 'explorer_panel_visible', False):
            def _prewarm_explorer():
                if sip.isdeleted(self) or not hasattr(self, 'explorer_panel'):
                    return
                self.explorer_panel.prewarm(getattr(self, '_window_cwd', None))
            QTimer.singleShot(1200, _prewarm_explorer)

    def showEvent(self, event):
        """窗口显示事件 - 设置 macOS 原生窗口属性"""
        super().showEvent(event)
        # 再次强制固定工具栏可见性（show 事件可能重置可见性）
        if self._pin_toolbar_row2:
            self._set_pinned_toolbars_visible(True)
            QTimer.singleShot(0, self._update_flow_toolbar_height)
        if not self._macos_window_configured:
            self._macos_window_configured = True
            # 延迟设置，确保窗口在 macOS 中完全注册
            def setup_macos():
                if not sip.isdeleted(self):
                    self._setup_macos_window()
            QTimer.singleShot(100, setup_macos)

        # __init__ 里基于 main_splitter.width() 计算左面板宽度时，窗口几何刚
        # 通过 setGeometry 设上但布局还没把真实宽度传到 main_splitter；此时
        # setSizes 等同设比例，等窗口最终铺到目标宽度，左面板会按比例放大。
        # show 之后宽度已稳定，这里再 apply 一次记忆值。仅做一次。
        if not getattr(self, '_splitter_sizes_restored_on_show', False):
            self._splitter_sizes_restored_on_show = True
            def _reapply():
                if sip.isdeleted(self):
                    return
                if (getattr(self, 'explorer_panel_visible', False)
                        or getattr(self, 'git_panel_visible', False)
                        or getattr(self, 'remote_panel_visible', False)
                        or getattr(self, 'log_panel_visible', False)):
                    self._update_splitter_sizes()
            QTimer.singleShot(0, _reapply)
            # 跨窗口左侧栏联动：延迟到窗口最大化/布局稳定后再对齐共享宽度。
            # singleShot(0) 时窗口往往尚未铺到最终尺寸，此刻 setSizes 会被当成比例
            # 缩放，导致各窗口左侧栏绝对像素不一致（正是「要先拖一下才联动」的根因）。
            def _prime_delayed():
                if not sip.isdeleted(self):
                    self._prime_left_panel_sync()
            QTimer.singleShot(300, _prime_delayed)

        # 立即刷新窗口导航（新窗口建立时）—— 广播到浮动与所有内嵌面板
        MainWindow._broadcast_navigator_refresh()

    def changeEvent(self, event):
        """窗口状态变化事件 - 优化窗口切换时的性能"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                # 用户切到本窗口 → 视为已查看，清除导航提醒小标
                self._clear_nav_attention()
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
                # 更新所有导航面板（浮动 + 内嵌）的选中项
                for nav in MainWindow._iter_navigators():
                    try:
                        nav.select_window(self)
                    except Exception:
                        pass
                # 跨窗口左侧栏联动：激活时窗口已铺满稳定，按共享宽度对齐一次。
                # 修正启动时因窗口尚未到最终尺寸、setSizes 被当成比例缩放而导致各
                # 窗口左侧栏宽度不一致的问题（无需用户先手动拖一次才联动）。
                # 仅在确有偏差(>2px)时应用，避免稳态下无谓跳动。
                try:
                    sw = MainWindow._shared_left_panel_width
                    if isinstance(sw, int) and sw > 0 and hasattr(self, 'main_splitter'):
                        sizes = self.main_splitter.sizes()
                        if sizes and sizes[0] > 0 and abs(sizes[0] - sw) > 2:
                            self._apply_shared_left_panel_width(sw)
                except Exception:
                    pass
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
                        logger.debug(f"已设置窗口属性: {window_title}")
                        return
                except Exception:
                    continue

            # 如果没找到匹配的，对所有标准窗口应用设置
            for ns_window in NSApp.windows():
                try:
                    # 跳过没有标题的窗口（可能是系统窗口）
                    if ns_window.title():
                        self._apply_macos_window_behavior(ns_window)
                except Exception:
                    continue

        except Exception as e:
            logger.warning(f"设置 macOS 窗口属性失败: {e}")

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
            logger.warning(f"应用 macOS 窗口行为失败: {e}")

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
            QToolBar::separator {
                background-color: #3d3d5c;
                width: 1px;
                margin-left: 0px;
                margin-right: 0px;
                margin-top: 6px;
                margin-bottom: 6px;
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
        self.detachable_tab_bar.tab_rename_requested.connect(self._begin_inline_tab_rename)

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
                background-color: #0f1626;
                color: #888;
                padding: 7px 18px;
                margin-right: 0px;
                margin-top: 3px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                border-top: 3px solid transparent;
                min-width: 100px;
            }
            QTabBar::tab:selected {
                background-color: #1a1a2e;
                color: #ffffff;
                font-weight: bold;
                margin-top: 0px;
                padding-top: 10px;
                border-top: 3px solid #667eea;
            }
            QTabBar::tab:hover:!selected {
                background-color: #1e2a4a;
                color: #cccccc;
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
        # 按下时记录时刻（与 macOS 失焦关闭弹窗几乎同刻），松开后据此做「恰好一次」开/关，
        # 避免弹窗已开时再点 ⚡ 出现「关掉→又重开」的闪烁。
        self.quick_launch_btn.pressed.connect(
            lambda: setattr(self, '_ql_press_at', time.monotonic()))
        self.quick_launch_btn.clicked.connect(self._toggle_quick_launch)
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

        # 窗口导航面板（内嵌模式）容器：固定在左侧栏顶部，只与 Explorer/Git/Remote 一起出现，
        # 自身保持紧凑（取内容高度，不抢占文件面板的纵向空间）。
        self.nav_panel_container = QWidget()
        self.nav_panel_container.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        _nav_layout = QVBoxLayout(self.nav_panel_container)
        _nav_layout.setContentsMargins(0, 0, 0, 0)
        _nav_layout.setSpacing(0)
        self.nav_panel = WindowNavigatorPanel(embedded=True)
        _nav_layout.addWidget(self.nav_panel)
        self.left_panel_layout.addWidget(self.nav_panel_container)
        # 恢复上次拖拽记忆的导航列表高度
        if isinstance(getattr(self, '_saved_nav_list_height', None), int):
            self.nav_panel.set_embedded_list_height(self._saved_nav_list_height)
        # 导航面板底部的可拖拽手柄：上下拖动改变列表高度并记住
        self.nav_resize_handle = _NavResizeHandle(
            self._on_nav_resize_drag, self._save_config)
        self.left_panel_layout.addWidget(self.nav_resize_handle)
        self.nav_resize_handle.hide()
        self.nav_panel_container.hide()
        self.nav_panel_visible = False
        self.nav_embed_enabled = False  # 内嵌导航条是否启用（勾选框控制，per-window）

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

        # Remote Explorer 面板容器（SSH/SFTP 文件浏览）
        self.remote_panel_container = QWidget()
        self._setup_remote_panel()
        self.left_panel_layout.addWidget(self.remote_panel_container)
        self.remote_panel_container.hide()
        self.remote_panel_visible = False

        self.main_splitter.addWidget(self.left_panel_container)
        # 左侧栏不允许被拖拽折叠到 0：往左拖到极限时停在最小宽度而不是「啪」地消失，
        # 要彻底隐藏请用面板右上角的 × 关闭按钮（更可控，状态也不会错乱）。
        self.left_panel_container.setMinimumWidth(200)
        self.main_splitter.setCollapsible(0, False)
        self.left_panel_container.hide()  # 默认隐藏

        # 主内容区用堆叠：第 0 页是终端，第 1 页是 Git 的左右并排 diff
        # （双击文件查看 diff 时切到第 1 页，占用整块右侧大空间，返回时切回终端）
        self._main_content_stack = QStackedWidget()
        self._main_content_stack.addWidget(self.tab_widget)  # index 0: 终端
        _diff_theme = self.THEMES.get(self.current_theme, next(iter(self.THEMES.values())))
        self.git_diff_view = GitDiffView(theme=_diff_theme)
        self.git_diff_view.closed.connect(self._hide_git_diff)
        self._main_content_stack.addWidget(self.git_diff_view)  # index 1: diff
        self.git_output_view = GitOutputView(theme=_diff_theme)
        self.git_output_view.closed.connect(self._hide_git_diff)
        self._main_content_stack.addWidget(self.git_output_view)  # index 2: pull 输出
        self.main_splitter.addWidget(self._main_content_stack)

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

        # 记忆用户手动调整后的尺寸（仅记录有意义的状态）；
        # 同时按合计宽度刷新 spring 门控（拖侧栏改变 editor+终端可用宽度时也能联动）
        self.main_splitter.splitterMoved.connect(
            lambda *_: (self._on_splitter_drag_tick(),
                        self._capture_explorer_layout(),
                        self._update_spring_width_gate())
        )

        # 弹簧模式：监听全局焦点变化，点击编辑器/终端时自动展宽对应一侧
        _app = QApplication.instance()
        if _app is not None:
            _app.focusChanged.connect(self._on_focus_changed_for_spring)

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

        # 标题和颜色指示器（合并为一个容器，避免 QToolBar 在小控件间插入多余间距）
        self.title_label = QLabel(t("toolbar.title_label"))
        self._update_title_label_color()

        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(24, 24)
        self.color_btn.setToolTip(t("toolbar.color_tooltip"))
        self.color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_color_btn_style()
        self.color_btn.clicked.connect(self._show_color_picker)

        self._title_color_container = QWidget()
        _tc_layout = QHBoxLayout(self._title_color_container)
        _tc_layout.setContentsMargins(0, 0, 0, 0)
        _tc_layout.setSpacing(4)
        _tc_layout.addWidget(self.title_label)
        _tc_layout.addWidget(self.color_btn)
        toolbar.addWidget(self._title_color_container)

        # 预设选择
        self.preset_label = QLabel(t("toolbar.preset_label"))
        self.preset_label.setStyleSheet("color: #888;")
        toolbar.addWidget(self.preset_label)

        self.preset_combo = CenteredComboBox()
        self.preset_combo.setMinimumWidth(200)
        self.preset_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.preset_combo.setStyleSheet("""
            QComboBox {
                background-color: #16213e;
                border: 2px solid #3d3d5c;
                border-radius: 6px;
                padding: 8px 12px;
                padding-right: 36px;
                color: #eaeaea;
                font-size: 12px;
                combobox-popup: 0;
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
                selection-color: #ffffff;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                outline: none;
                padding: 4px;
            }
            QComboBox QAbstractItemView::item {
                min-height: 28px;
                padding: 0px 6px;
                border-radius: 4px;
            }
        """)

        # 添加切换预设按钮
        self.preset_switch_btn = QPushButton(t("toolbar.switch_preset"))
        # 高度固定为 32，宽度只给最小值——英文 "Switch" 在 60 宽下会被裁成 "S..."
        self.preset_switch_btn.setFixedHeight(32)
        self.preset_switch_btn.setMinimumWidth(72)
        self.preset_switch_btn.setToolTip(t("toolbar.switch_preset_tooltip"))
        self.preset_switch_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #667eea, stop:1 #5a6fd6);
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 12px;
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

        # 用容器包裹 preset_combo，使其在工具栏中自动扩展填满可用空间
        self._preset_combo_container = QWidget()
        _pc_layout = QHBoxLayout(self._preset_combo_container)
        _pc_layout.setContentsMargins(0, 0, 0, 0)
        _pc_layout.setSpacing(0)
        _pc_layout.addWidget(self.preset_combo)
        self._preset_combo_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self._preset_combo_container)
        toolbar.addWidget(self.preset_switch_btn)

        # 管理预设按钮
        self.manage_preset_btn = QPushButton(t("toolbar.manage_preset"))
        # 显式 font-size 让它走与 Switch 等按钮一致的缩放（12px→GUI 字号），
        # 否则会落到主窗口 QToolBar QPushButton 的 13px 默认值、与 Switch 不一致。
        self.manage_preset_btn.setStyleSheet("""
            QPushButton {
                background-color: #3d3d5c;
                padding: 8px 12px;
                font-size: 12px;
            }
        """)
        self.manage_preset_btn.clicked.connect(self._manage_presets)
        toolbar.addWidget(self.manage_preset_btn)

        toolbar.addSeparator()

        # 命令搜索框（Cmd+K 聚焦）
        self.command_palette = CommandPalette(t("palette.placeholder"))
        self.command_palette.set_empty_text(t("palette.no_results"))
        # 两倍宽，保证占位文字 "Search commands… (⌘K)" 完整可见、不被截断。
        # 注意：必须用 set_box_width（写进 sizeHint），普通 setMinimumWidth 在 pin 后的
        # FlowLayout 工具栏里不生效——FlowLayout 只读 sizeHint/minimumSizeHint。
        self.command_palette.set_box_width(400)
        # 让搜索框尽可能占用空闲宽度，避免 placeholder 被截断
        self.command_palette.line_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.command_palette.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        toolbar.addWidget(self.command_palette)

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
            self._title_color_container,
            self.preset_label,
            self._preset_combo_container,
            self.preset_switch_btn,
            self.manage_preset_btn,
            None,  # separator
            self.command_palette,
            None,  # separator
            self.start_btn,
            self.stop_btn,
        ]

        # ===== 创建所有分组按钮（不添加到工具栏）=====

        # --- 选项组 ---
        self.image_prefix_checkbox = _ToolbarCheckBox(t("toolbar.image_prefix"))
        self.image_prefix_checkbox.setToolTip(t("toolbar.image_prefix_tooltip"))
        self.image_prefix_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaa;
                font-size: 11px;
                spacing: 8px;
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

        self.image_local_checkbox = _ToolbarCheckBox(t("toolbar.image_local"))
        self.image_local_checkbox.setToolTip(t("toolbar.image_local_tooltip"))
        self.image_local_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaa;
                font-size: 11px;
                spacing: 8px;
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

        self.window_nav_checkbox = _ToolbarCheckBox(t("toolbar.window_nav"))
        self.window_nav_checkbox.setToolTip(t("toolbar.window_nav_tooltip"))
        self.window_nav_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaa;
                font-size: 11px;
                spacing: 8px;
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
        # 同步导航开关状态（新窗口与现有窗口联动）
        if MainWindow._navigator_dock_mode == 'embed':
            # 内嵌模式：跟随其它窗口是否已启用导航条（stateChanged 尚未连接，不会触发回调）
            enabled = MainWindow._current_embed_enabled()
            self.nav_embed_enabled = enabled
            self.window_nav_checkbox.setChecked(enabled)
        elif MainWindow._global_window_navigator is not None and MainWindow._global_window_navigator.isVisible():
            self.window_nav_checkbox.setChecked(True)
        self.window_nav_checkbox.stateChanged.connect(self._on_window_nav_changed)

        # --- 操作组 ---
        self.export_btn = QPushButton(t("toolbar.export"))
        self.export_btn.clicked.connect(self._show_export_dialog)

        self.history_btn = QPushButton(t("toolbar.history"))
        self.history_btn.clicked.connect(self._show_history)

        self.images_btn = QPushButton(t("toolbar.images"))
        self.images_btn.setToolTip(t("toolbar.images_tooltip"))
        self.images_btn.clicked.connect(self._show_pasted_images)

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
        self.split_btn.clicked.connect(lambda: self._split_current_tab(self._shift_held()))

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
        self.split_v_btn.clicked.connect(lambda: self._split_vertical_current_terminal(self._shift_held()))

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
        self.explorer_toggle_btn.setToolTip(t("toolbar.explorer_tooltip") + "  (⌘1)")
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
        self.git_toggle_btn.setToolTip("Git  (⌘2)")
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

        self.remote_toggle_btn = QPushButton("Remote")
        self.remote_toggle_btn.setObjectName("remoteToggleBtn")
        self.remote_toggle_btn.setToolTip("Remote  (⌘3)")
        self.remote_toggle_btn.setCheckable(True)
        self.remote_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #38bdf8;
            }
            QPushButton:hover {
                background-color: #7dd3fc;
            }
            QPushButton:checked {
                background-color: #0284c7;
            }
        """)
        self.remote_toggle_btn.clicked.connect(self._toggle_remote_panel)

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
        # 不加左边距：负值会把首字母“T”挤出控件左边界被裁掉（macOS 字体文字本就贴边）
        self.theme_label.setStyleSheet("color: #888; margin-left: 0px;")

        self.theme_combo = CenteredComboBox()
        self.theme_combo.setFixedWidth(130)
        for theme_key in self.THEMES.keys():
            # CenteredComboBox.addItem 已自动为新增项设置居中对齐
            self.theme_combo.addItem(t(f"theme.{theme_key}"), theme_key)
        self.theme_combo.setStyleSheet("""
            QComboBox {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 4px 8px;
                color: #eaeaea;
                min-width: 70px;
                margin-right: 6px;
                combobox-popup: 0;
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
                selection-color: #ffffff;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                outline: none;
                padding: 4px;
            }
            QComboBox QAbstractItemView::item {
                min-height: 28px;
                padding: 0px 6px;
                border-radius: 4px;
            }
        """)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)

        # --- 语言选择 ---
        # 用 CenteredComboBox 让显示文本在 edit-field 居中（不依赖 editable hack）
        self.lang_combo = CenteredComboBox()
        self.lang_combo.setFixedWidth(94)
        self.lang_combo.setMinimumPopupWidth(100)
        self.lang_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.lang_combo.setMinimumContentsLength(7)
        self.lang_combo.addItem("中文", "zh")
        self.lang_combo.addItem("English", "en")
        # 让下拉列表里的选项也居中
        for i in range(self.lang_combo.count()):
            self.lang_combo.setItemData(
                i, Qt.AlignmentFlag.AlignCenter, Qt.ItemDataRole.TextAlignmentRole
            )
        # 设置当前语言
        lang_idx = self.lang_combo.findData(get_language())
        if lang_idx >= 0:
            self.lang_combo.setCurrentIndex(lang_idx)
        # 用与 theme/GUI 字号/透明度一致的共享样式（drop-down 宽 20px，
        # 避免之前 width:0px 让箭头子区域塌缩到右边框、三角被挤出去）
        self.lang_combo.setStyleSheet(self._COMBO_STYLE)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)

        # 语言容器：标签 "Language:" + 下拉框（与 GUI 字号/透明度一致）
        self.lang_container = QWidget()
        lang_layout = QHBoxLayout(self.lang_container)
        lang_layout.setContentsMargins(4, 0, 0, 0)
        lang_layout.setSpacing(4)
        self.lang_label = QLabel(t("toolbar.lang_label"))
        self.lang_label.setStyleSheet("color: #888;")
        lang_layout.addWidget(self.lang_label)
        lang_layout.addWidget(self.lang_combo)
        self.lang_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        self.icon_tint_checkbox = _ToolbarCheckBox(t("toolbar.icon_tint"))
        self.icon_tint_checkbox.setToolTip(t("toolbar.icon_tint_tooltip"))
        self.icon_tint_checkbox.setStyleSheet("""
            QCheckBox {
                color: #aaa;
                font-size: 11px;
                spacing: 8px;
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
        self.icon_tint_checkbox.setChecked(self._use_icon_tint)
        self.icon_tint_checkbox.stateChanged.connect(self._on_icon_tint_changed)

        # --- 设置组 ---
        self.llm_config_btn = QPushButton("✨")
        self.llm_config_btn.setObjectName("llmConfigBtn")
        self.llm_config_btn.setToolTip(t("toolbar.llm_config_tooltip"))
        # 只固定宽度，高度交给布局（沿用全局 QPushButton 的 padding/font-size），
        # 这样它的高度始终和其它工具栏按钮一致，GUI 字号缩放时也同步变化。
        self.llm_config_btn.setFixedWidth(42)
        self.llm_config_btn.setStyleSheet("""
            QPushButton {
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 6px;
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

        # 用下拉框（CenteredComboBox）代替 SpinBox：Auto(0) + 8–32pt
        self.gui_font_spin = CenteredComboBox()
        self.gui_font_spin.setToolTip(t("toolbar.gui_font_tooltip"))
        self.gui_font_spin.setFixedWidth(90)
        self.gui_font_spin.setMinimumPopupWidth(90)
        self.gui_font_spin.addItem(t("toolbar.gui_font_auto"), 0)
        for _pt in range(8, 33):
            self.gui_font_spin.addItem(f"{_pt} pt", _pt)
        _gf_idx = self.gui_font_spin.findData(self._gui_font_size)
        self.gui_font_spin.setCurrentIndex(_gf_idx if _gf_idx >= 0 else 0)
        self.gui_font_spin.setStyleSheet(self._COMBO_STYLE)
        self.gui_font_spin.currentIndexChanged.connect(self._on_gui_font_size_changed)
        gui_font_layout.addWidget(self.gui_font_spin)

        # 窗口透明度控件
        self.opacity_container = QWidget()
        opacity_layout = QHBoxLayout(self.opacity_container)
        opacity_layout.setContentsMargins(4, 0, 0, 0)
        opacity_layout.setSpacing(4)

        self.opacity_label = QLabel(t("toolbar.opacity_label"))
        self.opacity_label.setStyleSheet("color: #888;")
        opacity_layout.addWidget(self.opacity_label)

        # 用下拉框（CenteredComboBox）代替 SpinBox：100% → 10%（步进 5）
        self.opacity_spin = CenteredComboBox()
        self.opacity_spin.setToolTip(t("toolbar.opacity_tooltip"))
        self.opacity_spin.setFixedWidth(80)
        self.opacity_spin.setMinimumPopupWidth(80)
        for _pct in range(100, 9, -5):
            self.opacity_spin.addItem(f"{_pct}%", _pct)
        _op_idx = self.opacity_spin.findData(self._window_opacity)
        self.opacity_spin.setCurrentIndex(_op_idx if _op_idx >= 0 else 0)
        self.opacity_spin.setStyleSheet(self._COMBO_STYLE)
        self.opacity_spin.currentIndexChanged.connect(self._on_opacity_changed)
        opacity_layout.addWidget(self.opacity_spin)

        # 固定第二排工具栏 checkbox
        self.pin_row2_checkbox = QCheckBox(t("toolbar.pin_row2"))
        self.pin_row2_checkbox.setToolTip(t("toolbar.pin_row2_tooltip"))
        self.pin_row2_checkbox.setChecked(self._pin_toolbar_row2)
        self.pin_row2_checkbox.setStyleSheet("""
            QCheckBox {
                color: #eaeaea;
                font-size: 11px;
                spacing: 8px;
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
        self.toolbar_settings_btn.clicked.connect(self._show_settings_popup_menu)

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
                "images_btn": self.images_btn,
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
                "remote_toggle_btn": self.remote_toggle_btn,
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
                "lang_combo": self.lang_container,
                "gui_font_spin": self.gui_font_container,
                "opacity_spin": self.opacity_container,
            },
        }

        self._group_default_orders = {
            "选项": ["image_prefix_checkbox", "image_local_checkbox", "window_nav_checkbox"],
            "操作": ["export_btn", "history_btn", "images_btn", "clear_btn"],
            "分屏管理": ["split_btn", "split_v_btn", "close_split_btn", "close_tab_btn"],
            "面板与编辑器": ["explorer_toggle_btn", "git_toggle_btn", "remote_toggle_btn", "vscode_open_btn", "cursor_open_btn", "log_toggle_btn"],
            "主题": ["theme_combo", "icon_tint_checkbox"],
            "设置": ["llm_config_btn", "gui_font_spin", "opacity_spin", "lang_combo"],
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
            "preset_combo": self._preset_combo_container,
            "preset_switch_btn": self.preset_switch_btn,
            "manage_preset_btn": self.manage_preset_btn,
            "start_btn": self.start_btn,
            "stop_btn": self.stop_btn,
            "image_prefix_checkbox": self.image_prefix_checkbox,
            "image_local_checkbox": self.image_local_checkbox,
            "window_nav_checkbox": self.window_nav_checkbox,
            "export_btn": self.export_btn,
            "history_btn": self.history_btn,
            "images_btn": self.images_btn,
            "clear_btn": self.clear_btn,
            "split_btn": self.split_btn,
            "split_v_btn": self.split_v_btn,
            "close_split_btn": self.close_split_btn,
            "close_tab_btn": self.close_tab_btn,
            "explorer_toggle_btn": self.explorer_toggle_btn,
            "git_toggle_btn": self.git_toggle_btn,
            "remote_toggle_btn": self.remote_toggle_btn,
            "vscode_open_btn": self.vscode_open_btn,
            "cursor_open_btn": self.cursor_open_btn,
            "log_toggle_btn": self.log_toggle_btn,
            "theme_combo": self.theme_combo,
            "icon_tint_checkbox": self.icon_tint_checkbox,
            "llm_config_btn": self.llm_config_btn,
            "lang_combo": self.lang_container,
            "gui_font_spin": self.gui_font_container,
            "opacity_spin": self.opacity_container,
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
        # 去掉 QToolBar 自身所有间距，由 FlowLayout 完全控制
        self._pinned_flow_toolbar.setStyleSheet("QToolBar { padding: 0px; margin: 0px; spacing: 0px; }")
        self._pinned_flow_toolbar.setContentsMargins(0, 0, 0, 0)

        self._pinned_flow_widget = QWidget()
        self._pinned_flow_widget.setObjectName("pinnedFlowWidget")
        self._pinned_flow_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._pinned_flow_widget.setContentsMargins(0, 0, 0, 0)
        # h_spacing 匹配 unpin QToolBar 的 CSS spacing: 5px
        # contentsMargins 需减去 QToolBar/QWidgetAction 内部约 3px 的隐式边距
        self._flow_layout = FlowLayout(self._pinned_flow_widget, h_spacing=5, v_spacing=5)
        self._flow_layout.setContentsMargins(2, 2, 2, 2)
        self._pinned_flow_toolbar.addWidget(self._pinned_flow_widget)

        pinned = is_double_row or self._pin_toolbar_row2
        if pinned:
            self._populate_pinned_flow(effective_group_order)

        # flow toolbar 与 main_toolbar 同行（同一时刻只显示其一）
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._pinned_flow_toolbar)
        self._pinned_flow_toolbar.setVisible(pinned)
        if pinned:
            self.main_toolbar.setVisible(False)

        # 为 flow 中的按钮建立 _flow_btn_widgets 映射（全部分组，含跨组移入"预设与控制"的按钮）
        for group_name in effective_group_order:
            if group_name in self._group_button_dicts:
                for btn_name, widget in self._group_button_dicts[group_name].items():
                    # 核心控件不需要映射（它们不通过 _apply_toolbar_config 控制可见性）
                    if widget not in self._core_toolbar_widgets:
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
        self.working_dir_combo = QuietPopupComboBox()
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
        # 历史记录下拉按钮：简洁的向下箭头（下拉指示），与工具栏矢量图标风格一致。
        # 高度与 Browse 对齐（见下方 setFixedHeight）。
        self.dir_dropdown_btn = QPushButton()
        self.dir_dropdown_btn.setIcon(_make_git_tool_icon('caret_down', 'white'))
        self.dir_dropdown_btn.setIconSize(QSize(16, 16))
        self.dir_dropdown_btn.setFixedWidth(36)
        self.dir_dropdown_btn.setToolTip(t("status.dir_history_tooltip"))
        self.dir_dropdown_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #6b5b95, stop:1 #5b4b85);
                color: white;
                border: none;
                border-radius: 6px;
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
        # 使用自定义 LineEdit：点击文字附近全选，点击右侧空白处弹出历史列表
        select_all_edit = SelectAllLineEdit(popup_owner=self.working_dir_combo)
        select_all_edit.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.working_dir_combo.setLineEdit(select_all_edit)
        # 🕘 按钮与输入框空白点击共用同一套精确的开/关逻辑，避免无条件 showPopup()
        # 在弹窗已开/刚被抓取关闭时反弹重开造成闪烁。按下时补记按下时刻（与原生抓取
        # 关闭弹窗同刻），松开后推迟到事件循环空闲再据此做恰好一次开/关切换。
        self.dir_dropdown_btn.pressed.connect(select_all_edit.note_external_press)
        self.dir_dropdown_btn.clicked.connect(
            lambda: QTimer.singleShot(0, select_all_edit.toggle_popup)
        )
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
        # 历史按钮高度与 Browse 对齐（Browse 高度由内容+padding 决定，取其 sizeHint）
        self.dir_dropdown_btn.setFixedHeight(self.browse_btn.sizeHint().height())

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

        # 工具栏所有按钮已就绪 → 注册到命令面板
        self._register_palette_commands()

        # ⌘K / Ctrl+K 聚焦搜索框
        sc = QShortcut(QKeySequence("Ctrl+K"), self)
        sc.activated.connect(self.command_palette.focus_search)

    def _register_palette_commands(self):
        """把工具栏可点击的控件注册成可搜索命令。"""
        palette = self.command_palette
        palette.clear()

        def _label_for(w, fallback: str = '') -> str:
            """按钮显示文本：纯符号/emoji 时退回 tooltip，避免命令列里出现裸图标。"""
            txt = ''
            if hasattr(w, 'text') and callable(w.text):
                txt = (w.text() or '').strip()
            # 文本太短或不含字母数字 → 用 tooltip
            if not txt or not any(c.isalnum() for c in txt):
                tip = (w.toolTip() if hasattr(w, 'toolTip') else '') or ''
                if tip:
                    return tip.splitlines()[0].strip()
            return txt or fallback

        # 核心动作（不在 _group_button_dicts 里）
        core = [
            (self.preset_switch_btn, "Preset"),
            (self.manage_preset_btn, "Preset"),
            (self.start_btn, "Session"),
            (self.stop_btn, "Session"),
        ]
        for btn, group in core:
            label = _label_for(btn, fallback=btn.objectName())
            palette.register(label, btn.click, group=group, tooltip=btn.toolTip() or None)

        # 分组里的按钮 / 复选框
        for group_name, btns in self._group_button_dicts.items():
            for btn_name, w in btns.items():
                if w is None or not hasattr(w, 'click'):
                    continue
                label = _label_for(w, fallback=btn_name)
                tooltip = (w.toolTip() if hasattr(w, 'toolTip') else '') or None
                palette.register(label, w.click, group=group_name, tooltip=tooltip)

    def _populate_working_dirs(self):
        """填充工作目录历史到下拉框"""
        self.working_dir_combo.clear()
        for dir_path in self.working_dir_history:
            self.working_dir_combo.addItem(dir_path)
            self.working_dir_combo.setItemData(
                self.working_dir_combo.count() - 1,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                Qt.ItemDataRole.TextAlignmentRole,
            )
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
            # 首个 tab 在 _setup_ui() 里先于本方法创建，其 tab_cwds 记录的是启动时的
            # os.getcwd()（即 app.py 所在目录）。这里恢复成实际工作目录后必须同步覆盖，
            # 否则切换/分离 tab 时 _on_tab_changed 会读到这条陈旧记录，把窗口目录拉回
            # 启动目录。
            cur_idx = self.tab_widget.currentIndex()
            if cur_idx >= 0:
                self.tab_cwds[cur_idx] = self.last_working_dir

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

        # 同步当前 tab 的工作目录记录，否则切换/分离 tab 时 _on_tab_changed 会读到
        # 陈旧的 tab_cwds，把刚手动设置的目录覆盖掉。
        if current_tab >= 0:
            self.tab_cwds[current_tab] = dir_path

        # 加载新目录的本地快速命令
        self._load_local_commands()

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
        # 触发所有导航面板（浮动 + 内嵌）立即刷新（颜色/名称变化）
        MainWindow._broadcast_navigator_refresh(invalidate_cache=True)

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

    # 可在「键盘快捷键」设置里自定义的 GUI 快捷键。
    # (action_id, 默认键序列, i18n标签key, 触发方法名)
    _SHORTCUT_SPECS = [
        ("toggle_editor",   "Ctrl+E",         "shortcuts.act.toggle_editor",   "_toggle_editor_collapsed"),
        ("history",         "Ctrl+Shift+H",   "shortcuts.act.history",         "_show_history"),
        ("new_session",     "Ctrl+Shift+N",   "shortcuts.act.new_session",     "_start_session"),
        ("new_tab",         "Ctrl+T",         "shortcuts.act.new_tab",         "_add_new_tab"),
        ("close_tab",       "Ctrl+W",         "shortcuts.act.close_tab",       "_close_tab_or_window"),
        ("next_tab",        "Ctrl+Tab",       "shortcuts.act.next_tab",        "_next_tab"),
        ("prev_tab",        "Ctrl+Shift+Tab", "shortcuts.act.prev_tab",        "_prev_tab"),
        ("split_h",         "Ctrl+Shift+S",   "shortcuts.act.split_h",         "_split_current_tab"),
        ("split_v",         "Ctrl+Shift+V",   "shortcuts.act.split_v",         "_split_vertical_current_terminal"),
        ("close_split",     "Ctrl+Shift+X",   "shortcuts.act.close_split",     "_close_current_split"),
        ("toggle_explorer", "Ctrl+1",         "shortcuts.act.toggle_explorer", "_toggle_explorer_panel"),
        ("toggle_git",      "Ctrl+2",         "shortcuts.act.toggle_git",      "_toggle_git_panel"),
        ("toggle_remote",   "Ctrl+3",         "shortcuts.act.toggle_remote",   "_toggle_remote_panel"),
        ("zoom_in",         "Ctrl+=",         "shortcuts.act.zoom_in",         "_global_zoom_in"),
        ("zoom_out",        "Ctrl+-",         "shortcuts.act.zoom_out",        "_global_zoom_out"),
        ("opacity_up",      "Ctrl+Shift+Up",  "shortcuts.act.opacity_up",      "_opacity_increase"),
        ("opacity_down",    "Ctrl+Shift+Down","shortcuts.act.opacity_down",    "_opacity_decrease"),
        # 注意：别用 Ctrl+/ —— 会与编辑器的注释切换同键位，构成歧义快捷键
        ("cheatsheet",      "Ctrl+Shift+/",   "shortcuts.act.cheatsheet",      "_show_shortcut_cheatsheet"),
    ]

    def _setup_shortcuts(self):
        """设置快捷键（数据驱动，可在「键盘快捷键」设置里自定义）"""
        self.shortcut_actions = {}
        overrides = getattr(self, '_custom_shortcuts', None) or {}
        for action_id, default_seq, _label_key, slot_name in self._SHORTCUT_SPECS:
            seq = overrides.get(action_id, default_seq)
            action = QAction(self)
            if seq:  # 允许用户把某项清空（seq == ""）
                action.setShortcut(QKeySequence(seq))
            if action_id == "close_tab":
                # Ctrl+W 用窗口级上下文，避免在子控件里被吞掉
                action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(getattr(self, slot_name))
            self.addAction(action)
            self.shortcut_actions[action_id] = action

        # Ctrl++ 作为「放大」的固定别名（无需按 Shift），不参与自定义
        zoom_in_alias = QAction(self)
        zoom_in_alias.setShortcut(QKeySequence("Ctrl++"))
        zoom_in_alias.triggered.connect(self._global_zoom_in)
        self.addAction(zoom_in_alias)

        # Ctrl+Shift+D 调试特殊字符（开发用，不在自定义列表中）
        debug_action = QAction(self)
        debug_action.setShortcut("Ctrl+Shift+D")
        debug_action.triggered.connect(self._debug_special_chars)
        self.addAction(debug_action)

    def _setup_window_menu(self):
        """原生「窗口」菜单：可点击的「下一个/上一个窗口」，并展示 Cmd+` / Cmd+Shift+`。

        说明：实际的 Cmd+` 按键处理由 _install_backtick_monitor 的 AppKit 级 keyDown
        监听器完成（见那里的说明）——菜单项这里主要用于可发现性与鼠标点击触发。
        注意菜单项的快捷键本身不会拦截系统的 Cmd+`：macOS 的「移动焦点到下一窗口」是
        系统级保留快捷键，优先级高于应用菜单/QShortcut，需用户在「系统设置 → 键盘 →
        键盘快捷键 → 键盘」中禁用后，Cmd+` 才会落到我们的监听器里稳定切换。
        """
        menubar = self.menuBar()
        window_menu = menubar.addMenu(t("window.menu"))

        next_action = QAction(t("window.next_window"), self)
        next_action.setShortcut(QKeySequence("Ctrl+`"))  # macOS 上 Qt 自动映射为 Cmd+`
        next_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        next_action.triggered.connect(lambda: self._cycle_to_window(1))
        window_menu.addAction(next_action)

        prev_action = QAction(t("window.prev_window"), self)
        prev_action.setShortcut(QKeySequence("Ctrl+Shift+`"))  # Cmd+Shift+`
        prev_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        prev_action.triggered.connect(lambda: self._cycle_to_window(-1))
        window_menu.addAction(prev_action)

        self._window_menu_actions = (next_action, prev_action)  # 防 GC

    def _cycle_to_window(self, direction):
        """菜单点击入口：委托给类级的窗口循环实现。"""
        MainWindow._cycle_windows(direction)

    @classmethod
    def _cycle_windows(cls, direction):
        """在所有可见 MainWindow 之间按稳定顺序（创建时间）循环切换。

        direction: +1 下一个 / -1 上一个。由 NSEvent 监听器（Cmd+`）或菜单触发，
        绕开 macOS 原生 Cmd+` 的间歇性失灵。
        """
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if not app:
            return
        wins = [w for w in app.topLevelWidgets()
                if isinstance(w, MainWindow) and not sip.isdeleted(w) and w.isVisible()]
        if len(wins) < 2:
            return
        try:
            wins.sort(key=lambda w: w.get_created_time())
        except Exception:
            pass
        active_idx = [i for i, w in enumerate(wins) if w.isActiveWindow()]
        cur = active_idx[0] if active_idx else 0
        target = wins[(cur + direction) % len(wins)]
        if sip.isdeleted(target):
            return
        if target.isMinimized():
            target.showNormal()
        target.raise_()
        target.activateWindow()

    _backtick_monitor = None  # NSEvent 本地监听器句柄（防 GC）

    @classmethod
    def _install_backtick_monitor(cls):
        """安装 AppKit 级 keyDown 监听器，截获 Cmd+`（及 Cmd+Shift+`）。

        macOS 原生「移动焦点到下一窗口」(Cmd+`) 在多窗口下会间歇性失灵，且其优先级
        高于应用菜单快捷键（QAction 根本不触发）。NSEvent 本地监听器在事件分发到窗口/
        原生处理【之前】就能拿到 keyDown，处理后返回 None 吞掉事件，从而用我们自己的
        _cycle_windows 稳定替换系统那条不稳定的循环。只装一次（全应用共享）。
        """
        if not MACOS_NATIVE_AVAILABLE or cls._backtick_monitor is not None:
            return
        try:
            from AppKit import NSEvent, NSEventMaskKeyDown
            try:
                from AppKit import NSEventModifierFlagCommand, NSEventModifierFlagShift
            except ImportError:  # 旧版 pyobjc 常量名
                from AppKit import NSCommandKeyMask as NSEventModifierFlagCommand
                from AppKit import NSShiftKeyMask as NSEventModifierFlagShift
        except Exception as e:
            logger.warning(f"[backtick] 监听器导入失败: {e}")
            return

        def _handler(event):
            try:
                # keyCode 50 = kVK_ANSI_Grave（` 键的物理位置，与键盘布局无关）
                if event.keyCode() == 50:
                    flags = int(event.modifierFlags())
                    if flags & int(NSEventModifierFlagCommand):
                        # Cmd+` 切到上一个、Cmd+Shift+` 切到下一个（与系统原生方向一致）
                        direction = 1 if (flags & int(NSEventModifierFlagShift)) else -1
                        cls._cycle_windows(direction)
                        return None  # 吞掉，阻止系统原生 Cmd+` 再处理一次
            except Exception:
                pass
            return event

        try:
            cls._backtick_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _handler)
        except Exception as e:
            logger.warning(f"[backtick] 监听器安装失败: {e}")

    def _effective_shortcut(self, action_id, default_seq):
        """返回某操作当前生效的键序列（用户覆盖优先，否则默认）。"""
        overrides = getattr(self, '_custom_shortcuts', None) or {}
        return overrides.get(action_id, default_seq)

    def _show_shortcut_settings(self):
        """打开「键盘快捷键」自定义对话框，保存后即时生效。"""
        current = {
            action_id: self._effective_shortcut(action_id, default_seq)
            for action_id, default_seq, _lk, _slot in self._SHORTCUT_SPECS
        }
        dialog = ShortcutSettingsDialog(self._SHORTCUT_SPECS, current, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.get_shortcuts()  # {action_id: seq}
        # 只把和默认值不同的项记成覆盖，保持配置精简、默认值可随版本演进
        overrides = {}
        for action_id, default_seq, _lk, _slot in self._SHORTCUT_SPECS:
            seq = result.get(action_id, default_seq)
            if seq != default_seq:
                overrides[action_id] = seq
            # 运行时即时套用到已注册的 QAction
            action = self.shortcut_actions.get(action_id)
            if action is not None:
                action.setShortcut(QKeySequence(seq) if seq else QKeySequence())
        self._custom_shortcuts = overrides
        self._shortcuts_modified = True
        self._save_config()
        self.statusbar.showMessage(t("shortcuts.saved"), 3000)

    def _shortcut_cheatsheet_groups(self):
        """组装速查表数据：全局组读 _SHORTCUT_SPECS 的当前生效值（含用户覆盖，
        不会和实际行为漂移）；终端/编辑器组是 keyPressEvent 里硬编码键位的镜像，
        改那边记得同步这里。键位按平台原生格式渲染（macOS 显示 ⌘⇧⌃ 符号）。"""
        def nat(seq):
            s = QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText)
            return s or seq

        global_rows = []
        for action_id, default_seq, label_key, _slot in self._SHORTCUT_SPECS:
            seq = self._effective_shortcut(action_id, default_seq)
            global_rows.append((nat(seq) if seq else t("shortcuts.unset"), t(label_key)))
        global_rows += [
            (nat("Ctrl+K"), t("shortcuts.sc.cmd_search")),
            (nat("Ctrl+`") + " / " + nat("Ctrl+Shift+`"),
             t("window.next_window") + " / " + t("window.prev_window")),
        ]

        # 终端键位（terminal_widget.keyPressEvent；SIGINT 是物理 Ctrl，故用 Meta 渲染）
        sigint = nat("Meta+C") if sys.platform == "darwin" else nat("Ctrl+C")
        terminal_rows = [
            (nat("Ctrl+C"), t("shortcuts.sc.term_copy")),
            (sigint, t("shortcuts.sc.term_interrupt")),
            (nat("Ctrl+V"), t("shortcuts.sc.term_paste")),
            (nat("Ctrl+A"), t("shortcuts.sc.term_select_all")),
            (nat("Shift+PgUp") + " / " + nat("Shift+PgDown"), t("shortcuts.sc.term_page")),
            (nat("Shift+Home") + " / " + nat("Shift+End"), t("shortcuts.sc.term_home_end")),
            (nat("Ctrl+Up") + " / " + nat("Ctrl+Down"), t("shortcuts.sc.term_jump")),
            (nat("Ctrl+Left") + " / " + nat("Ctrl+Right"), t("shortcuts.sc.term_line_ends")),
            (nat("Esc"), t("shortcuts.sc.term_close_search")),
        ]

        # 编辑器键位（file_editor.py）
        editor_rows = [
            (nat("Ctrl+S"), t("shortcuts.sc.edit_save")),
            (nat("Ctrl+/"), t("shortcuts.sc.edit_comment")),
            (nat("Ctrl+F"), t("shortcuts.sc.edit_find")),
            (nat("Ctrl+H"), t("shortcuts.sc.edit_replace")),
            (nat("Ctrl+G") + " / " + nat("Ctrl+Shift+G"), t("shortcuts.sc.edit_find_next")),
            (nat("Ctrl+\\") + " / " + nat("Ctrl+Shift+\\"), t("shortcuts.sc.edit_split")),
            ("Tab", t("shortcuts.sc.edit_ai_accept")),
            (nat("Esc"), t("shortcuts.sc.edit_ai_dismiss")),
            (nat("Alt+\\"), t("shortcuts.sc.edit_ai_trigger")),
        ]

        return [
            (t("shortcuts.group.global"), global_rows),
            (t("shortcuts.group.terminal"), terminal_rows),
            (t("shortcuts.group.editor"), editor_rows),
        ]

    def _show_shortcut_cheatsheet(self):
        """打开快捷键速查表（非模态；重复触发时刷新数据并提到前台）。"""
        old = getattr(self, '_cheatsheet_dialog', None)
        if old is not None:
            try:
                old.close()
                old.deleteLater()
            except Exception:
                pass
        dialog = ShortcutCheatSheetDialog(self._shortcut_cheatsheet_groups(), self)
        dialog.customize_requested.connect(self._show_shortcut_settings)
        self._cheatsheet_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    # ==================== 全局字体缩放 ====================

    @staticmethod
    def _select_combo_value(combo, value):
        """按存储的数据值选中下拉项（静默，不触发 currentIndexChanged）。"""
        idx = combo.findData(value)
        combo.blockSignals(True)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_gui_font_size_changed(self, _index: int = -1):
        """GUI 字体大小调整（下拉框：Auto=0 或 8–32pt）"""
        value = self.gui_font_spin.currentData()
        if value is None:
            return
        self._gui_font_size = int(value)
        self._apply_global_zoom()

    def _on_opacity_changed(self, _index: int = -1):
        """窗口透明度调整 — 同步到所有窗口"""
        value = self.opacity_spin.currentData()
        if value is None:
            return
        self._window_opacity = int(value)
        self._apply_opacity_to_all_windows()
        self._save_config()

    def _apply_opacity_to_all_windows(self):
        """将当前透明度设置应用到所有 MainWindow 窗口"""
        opacity = self._window_opacity / 100.0
        app = QApplication.instance()
        if app:
            for widget in app.topLevelWidgets():
                if isinstance(widget, MainWindow):
                    widget.setWindowOpacity(opacity)
                    for terminals in getattr(widget, 'tab_terminals', {}).values():
                        for terminal in terminals:
                            if hasattr(terminal, '_invalidate_render_cache'):
                                terminal._invalidate_render_cache()
                    # 同步其他窗口的透明度下拉框（避免信号循环）
                    if widget is not self and hasattr(widget, 'opacity_spin'):
                        self._select_combo_value(widget.opacity_spin, self._window_opacity)
                        widget._window_opacity = self._window_opacity

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

            # 2. 重置 flow toolbar 高度约束，防止上一次的 setFixedHeight 影响新布局
            self._pinned_flow_toolbar.setMinimumHeight(0)
            self._pinned_flow_toolbar.setMaximumHeight(16777215)  # QWIDGETSIZE_MAX

            # 3. 先切换可见性，再填充 flow layout
            #    （确保 parent 可见，separator 等子控件的 isVisible() 才正确）
            self.main_toolbar.setVisible(False)
            self._pinned_flow_toolbar.setVisible(True)

            # 4. 填充 flow layout（核心控件 + 全部分组）
            self._populate_pinned_flow(effective_group_order)
            # 延迟更新高度：让 Qt 完成布局后再计算
            QTimer.singleShot(0, self._update_flow_toolbar_height)
            QTimer.singleShot(100, self._update_flow_toolbar_height)

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
                    w.show()

            # 4. 按分组顺序将所有组按钮添加回 main_toolbar
            for group_name in effective_group_order:
                if group_name not in self._group_button_dicts:
                    continue
                buttons_dict = self._group_button_dicts[group_name]
                if group_name == "预设与控制":
                    # 核心按钮已在步骤3恢复，只处理被跨组移入的额外按钮
                    extra_dict = {k: v for k, v in buttons_dict.items() if v not in self._core_toolbar_widgets}
                    if not extra_dict:
                        continue
                    buttons_dict = extra_dict
                if not buttons_dict:
                    continue
                self.main_toolbar.addSeparator()
                if group_name in self._group_prefix_widgets:
                    pw = self._group_prefix_widgets[group_name]
                    self.main_toolbar.addWidget(pw)
                    pw.show()
                saved_order = self._get_button_order(group_name)
                order = saved_order if saved_order else self._group_default_orders.get(group_name, [])
                for btn_name in order:
                    if btn_name in buttons_dict:
                        w = buttons_dict[btn_name]
                        new_action = self.main_toolbar.addWidget(w)
                        w.show()
                        self._toolbar_actions[btn_name] = new_action

            # 5. pin 和 settings 放最后
            pin_action = self.main_toolbar.addWidget(self.pin_row2_checkbox)
            self.pin_row2_checkbox.show()
            self.main_toolbar.addWidget(self.toolbar_settings_btn)
            self.toolbar_settings_btn.show()
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
            if group_name not in self._group_button_dicts:
                continue
            buttons_dict = self._group_button_dicts[group_name]
            if group_name == "预设与控制":
                # 核心按钮已在步骤1添加，只处理被跨组移入的额外按钮
                core_names = {w.objectName() if hasattr(w, 'objectName') else '' for w in self._core_toolbar_widgets if w is not None}
                extra_dict = {k: v for k, v in buttons_dict.items() if k not in core_names and v not in self._core_toolbar_widgets}
                if not extra_dict:
                    continue
                buttons_dict = extra_dict
            if not buttons_dict:
                continue
            # 每组前加分隔符
            sep = self._create_flow_separator()
            self._flow_layout.addWidget(sep)
            if group_name in self._group_prefix_widgets:
                pw = self._group_prefix_widgets[group_name]
                pw.setParent(self._pinned_flow_widget)
                self._flow_layout.addWidget(pw)
                pw.show()
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

        # 3. pin checkbox 和 settings 按钮放在最后（设置在最末尾）
        sep = self._create_flow_separator()
        self._flow_layout.addWidget(sep)
        self.pin_row2_checkbox.setParent(self._pinned_flow_widget)
        self._flow_layout.addWidget(self.pin_row2_checkbox)
        self.pin_row2_checkbox.show()
        sep2 = self._create_flow_separator()
        self._flow_layout.addWidget(sep2)
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
        """创建流式布局中的垂直分隔符，颜色取当前主题，外观与 QToolBar::separator 一致"""
        t = self.THEMES.get(self.current_theme, self.THEMES["深蓝"])
        sep = _FlowSeparator(self._pinned_flow_widget, color=t['border'])
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
            # 使用 flow widget 的实际宽度（已扣除 QToolBar 内部 margins）
            # 这样 heightForWidth 才能正确判断哪些 items 需要换行
            width = self._pinned_flow_widget.width()
            if width <= 0:
                width = self._pinned_flow_toolbar.width()
            if width <= 0:
                return
            # 计算 flow layout 需要的高度（FlowLayout 的 contentsMargins 已包含边距）
            h = self._flow_layout.heightForWidth(width)
            if h < 10:
                h = 36  # 最小高度
            self._pinned_flow_toolbar.setFixedHeight(h)
            self._pinned_flow_widget.setMinimumHeight(h)
        finally:
            self._updating_flow_height = False

    def resizeEvent(self, event):
        """窗口大小变化时更新 flow toolbar 高度"""
        super().resizeEvent(event)
        self._update_flow_toolbar_height()
        # 按窗口宽度自动让 spring 生效/失效（仅在跨阈值翻转时动一次）
        self._update_spring_width_gate()

    def _global_zoom_in(self):
        """全局放大字体 — 同步缩放所有区域"""
        self._global_zoom_delta += 1
        self._apply_global_zoom()

    def _global_zoom_out(self):
        """全局缩小字体 — 同步缩放所有区域"""
        self._global_zoom_delta -= 1
        self._apply_global_zoom()

    def _opacity_increase(self):
        """增加窗口透明度（更不透明）"""
        new_val = min(100, self._window_opacity + 5)
        self._window_opacity = new_val
        if hasattr(self, 'opacity_spin'):
            self._select_combo_value(self.opacity_spin, new_val)
        self._apply_opacity_to_all_windows()
        self._save_config()

    def _opacity_decrease(self):
        """减少窗口透明度（更透明）"""
        new_val = max(10, self._window_opacity - 5)
        self._window_opacity = new_val
        if hasattr(self, 'opacity_spin'):
            self._select_combo_value(self.opacity_spin, new_val)
        self._apply_opacity_to_all_windows()
        self._save_config()

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

        # 2. 全局 GUI 字体（工具栏、标签栏、状态栏等）
        #    - 缩放样式表中显式写了 font-size 的控件
        #    - 同时更新 QApplication 默认字体，让未显式设置 font-size 的控件（Start/Stop/Switch 等）也等比缩放
        if gui_font_size > 0:
            effective_px = gui_font_size
        elif delta != 0:
            effective_px = max(8, min(32, 12 + delta))
        else:
            effective_px = None
        self._scale_gui_font_sizes(gui_font_size, delta)
        self._apply_application_font(effective_px)

        # 3. 文件编辑器 — 与终端字号完全联动（同一字号、同一范围 8-32），不受 GUI 字号影响
        if hasattr(self, 'editor_area') and self.editor_area is not None:
            target_size = max(8, min(32, 12 + delta))
            self.editor_area.set_editor_font_size(target_size)

        # 4. 资源管理器文件树 (默认13pt, 范围8-28) — 跟随终端缩放，不受 GUI 字号影响
        if hasattr(self, 'explorer_panel') and self.explorer_panel is not None:
            target_size = max(8, min(28, 13 + delta))
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

    def _current_gui_font_scale(self) -> float:
        """当前 GUI 字体缩放比例（以 12px 为基准）。

        与 _scale_gui_font_sizes 同一套公式，供需要单独重算某个控件字号的地方
        （如 _update_title_label_color）复用，避免硬编码字号在重设样式时丢掉缩放。
        """
        base_px = 12
        if self._gui_font_size > 0:
            return self._gui_font_size / base_px
        if self._global_zoom_delta != 0:
            return (base_px + self._global_zoom_delta) / base_px
        return 1.0

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

    def _apply_application_font(self, effective_px):
        """让 QApplication 层面的 stylesheet 承担通用字号缩放。

        原因：Qt6 下 QApplication.setFont(...) 对已存在的 widget 并不会可靠地
        重新解析字体；而 app 级 stylesheet 通过 Qt 的层叠（cascade）机制，能把
        通配的 ``QWidget { font-size: Npx; }`` 应用到所有没有自己显式设 font-size
        的控件，同时被控件局部 stylesheet 的更具体规则覆盖。

        作用范围：工具栏里 Start/Stop/Switch/Manage 等未显式设字号的按钮。
        _scale_gui_font_sizes() 不会触到它们，这里补齐。

        effective_px 为 None 时恢复 app 级 stylesheet 的原值（通常为空）。
        """
        app = QApplication.instance()
        if app is None:
            return

        if MainWindow._original_app_stylesheet is None:
            MainWindow._original_app_stylesheet = app.styleSheet() or ''

        base = MainWindow._original_app_stylesheet
        if effective_px is None:
            new_ss = base
        else:
            px = max(7, int(effective_px))
            new_ss = (base + '\nQWidget { font-size: %dpx; }' % px).strip()

        if app.styleSheet() != new_ss:
            app.setStyleSheet(new_ss)

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

    def _reload_dir_history_from_config(self):
        """从配置文件重新加载目录历史（确保多窗口间同步）"""
        try:
            if self.CONFIG_FILE.exists():
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                saved_history = config.get('working_dir_history', [])
                saved_freq = config.get('working_dir_freq', {})
                # 合并：以文件中的数据为基础，同时保留本窗口新增但尚未保存的条目
                merged_history = list(saved_history)
                merged_freq = dict(saved_freq)
                for p in self.working_dir_history:
                    if p not in merged_freq:
                        merged_freq[p] = self._working_dir_freq.get(p, 1)
                    if p not in merged_history:
                        merged_history.append(p)
                # 为没有频率记录的路径补默认值
                for p in merged_history:
                    if p not in merged_freq:
                        merged_freq[p] = 1
                # 按频率倒序排列
                merged_history.sort(key=lambda p: merged_freq.get(p, 0), reverse=True)
                self.working_dir_history = merged_history
                self._working_dir_freq = merged_freq
        except Exception:
            pass

    def _mark_quick_launch_closed(self, *args):
        """记录快速启动弹窗关闭时刻（供 ⚡ 的开/关判定使用）。弹窗复用，不销毁。"""
        self._ql_hidden_at = time.monotonic()

    def _ql_hide(self):
        """隐藏快速启动弹窗（复用，不销毁），并记下关闭时刻。"""
        self._mark_quick_launch_closed()
        p = getattr(self, '_ql_popup', None)
        if p is not None and not sip.isdeleted(p) and p.isVisible():
            p.setWindowOpacity(0)
            p.hide()

    def _toggle_quick_launch(self):
        """点击 ⚡：做「恰好一次」的开/关，避免弹窗已开时再点出现关→重开的闪烁。

        - 弹窗此刻仍开着 → 干净地关掉它（不反弹重开）；
        - 弹窗不可见，但它是被「本次点击」关掉的（macOS 点 ⚡ 会让 Tool 弹窗失焦自关）
          → 什么都不做，避免立刻又重开；
        - 否则 → 正常打开。
        """
        p = getattr(self, '_ql_popup', None)
        if p is not None and not sip.isdeleted(p) and p.isVisible():
            self._ql_hide()
            return
        press_at = getattr(self, '_ql_press_at', 0.0)
        hidden_at = getattr(self, '_ql_hidden_at', 0.0)
        if hidden_at >= press_at - 0.05:
            # 弹窗刚被这次点击关掉 → 不要再重开
            return
        self._show_quick_launch_menu()

    def _ensure_quick_launch_popup(self):
        """惰性创建快速启动弹窗（只建一次，之后复用）。

        复用而非每次新建：新建一个 WA_TranslucentBackground 的 Tool 窗口在 macOS 上
        有可感知的原生窗口创建 + 淡入延迟（表现为「点了 ⚡ 要等一下才出现」）；复用后
        再次打开只是 hide→show，几乎瞬时，也不再有重建闪烁。
        """
        if getattr(self, '_ql_popup', None) is not None and not sip.isdeleted(self._ql_popup):
            return
        popup = QDialog(self, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # 透明背景，圆角不闪

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
            QLineEdit:focus { border-color: #667eea; }
        """)
        layout.addWidget(search_input)

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
            QListWidget::item:selected { background-color: #667eea; }
            /* 仅对「非选中项」应用悬停高亮：划过当前选中项保持蓝色不变，避免闪烁。 */
            QListWidget::item:hover:!selected { background-color: #3d3d5c; }
        """)
        list_widget.setMaximumHeight(400)
        layout.addWidget(list_widget)

        search_input.textChanged.connect(self._ql_update_list)
        search_input.returnPressed.connect(self._ql_do_launch)
        list_widget.itemClicked.connect(self._ql_on_item_activated)

        # 事件过滤器：失焦关闭 + 键盘上下/Esc
        mw = self

        class _QuickLaunchFilter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Type.WindowDeactivate and getattr(mw, '_ql_ready', False):
                    mw._ql_hide()
                    return True
                if event.type() == QEvent.Type.KeyPress:
                    key = event.key()
                    lst = mw._ql_list
                    if key == Qt.Key.Key_Down:
                        r = lst.currentRow()
                        if r < lst.count() - 1:
                            lst.setCurrentRow(r + 1)
                        return True
                    elif key == Qt.Key.Key_Up:
                        r = lst.currentRow()
                        if r > 0:
                            lst.setCurrentRow(r - 1)
                        return True
                    elif key == Qt.Key.Key_Escape:
                        mw._ql_hide()
                        return True
                return False

        filt = _QuickLaunchFilter(popup)
        popup.installEventFilter(filt)
        search_input.installEventFilter(filt)

        self._ql_popup = popup
        self._ql_search = search_input
        self._ql_list = list_widget
        self._ql_filter = filt
        self._ql_dirs = []
        self._ql_ready = False

    def _ql_rebuild_dirs(self):
        """重建快速启动目录数据（当前目录 + 历史 + 特殊项）。"""
        current_dir = self._window_cwd
        dirs = []
        name = os.path.basename(current_dir) or current_dir
        dirs.append(("current", current_dir, t("quick_launch.current", name=name)))
        for dir_path in self.working_dir_history:
            if dir_path != current_dir:
                dn = os.path.basename(dir_path) or dir_path
                dirs.append(("history", dir_path, f"📁 {dn}"))
        dirs.append(("browse", "", t("quick_launch.browse")))
        dirs.append(("manage", "", t("quick_launch.manage_paths")))
        self._ql_dirs = dirs

    def _ql_update_list(self, search_text: str = ""):
        """按搜索词刷新快速启动列表。"""
        lst = self._ql_list
        lst.setUpdatesEnabled(False)
        lst.clear()
        keywords = [kw.lower() for kw in search_text.split() if kw]
        for item_type, dir_path, display in getattr(self, '_ql_dirs', []):
            if item_type in ("browse", "manage"):
                if not keywords:
                    it = QListWidgetItem(display)
                    it.setData(Qt.ItemDataRole.UserRole, (item_type, dir_path))
                    lst.addItem(it)
            else:
                txt = (dir_path + " " + display).lower()
                if not keywords or all(kw in txt for kw in keywords):
                    it = QListWidgetItem(display)
                    it.setData(Qt.ItemDataRole.UserRole, (item_type, dir_path))
                    it.setToolTip(dir_path)
                    lst.addItem(it)
        if lst.count() > 0:
            lst.setCurrentRow(0)
        lst.setUpdatesEnabled(True)

    def _ql_on_item_activated(self, item):
        """列表项被点击/回车选中。"""
        item_type, dir_path = item.data(Qt.ItemDataRole.UserRole)
        self._ql_hide()
        if item_type == "browse":
            self._quick_launch_browse()
        elif item_type == "manage":
            self._manage_quick_launch_dirs()
        else:
            self._quick_launch_with_dir(dir_path)

    def _ql_do_launch(self):
        """回车：输入是路径则直接启动，否则激活当前选中项。"""
        text = self._ql_search.text().strip()
        is_path = False
        if text:
            if '/' in text or text.startswith('~'):
                is_path = True
            elif '\\' in text or (len(text) >= 2 and text[1] == ':' and text[0].isalpha()):
                is_path = True
        if is_path:
            self._ql_hide()
            self._quick_launch_with_dir(text)
            return
        cur = self._ql_list.currentItem()
        if cur:
            self._ql_on_item_activated(cur)

    def _show_quick_launch_menu(self):
        """显示快速启动菜单（复用同一个弹窗，避免每次新建造成的延迟/闪烁）。"""
        # 从配置文件重新加载目录历史，确保多窗口间同步
        self._reload_dir_history_from_config()
        self._ensure_quick_launch_popup()
        popup = self._ql_popup

        # 重建内容（当前目录可能已变）+ 清空搜索
        self._ql_rebuild_dirs()
        self._ql_search.blockSignals(True)
        self._ql_search.clear()
        self._ql_search.blockSignals(False)
        self._ql_update_list()
        self._ql_ready = False

        # 定位到按钮下方（先算尺寸再定位，避免位移闪烁）
        btn_global_pos = self.quick_launch_btn.mapToGlobal(QPoint(0, self.quick_launch_btn.height()))
        window_rect = self.geometry()
        window_global_pos = self.mapToGlobal(QPoint(0, 0))
        popup.adjustSize()
        popup.move(btn_global_pos.x(), btn_global_pos.y())
        if not popup.isVisible():
            popup.setWindowOpacity(0)
            popup.show()
        popup.adjustSize()
        popup_size = popup.size()

        x = btn_global_pos.x()
        y = btn_global_pos.y()
        right_edge = window_global_pos.x() + window_rect.width()
        if x + popup_size.width() > right_edge:
            x = right_edge - popup_size.width() - 10
        bottom_edge = window_global_pos.y() + window_rect.height()
        if y + popup_size.height() > bottom_edge:
            y = btn_global_pos.y() - self.quick_launch_btn.height() - popup_size.height()
        if x < window_global_pos.x():
            x = window_global_pos.x() + 10

        popup.move(x, y)
        popup.setWindowOpacity(1)
        popup.activateWindow()
        popup.raise_()
        self._ql_search.setFocus(Qt.FocusReason.PopupFocusReason)
        QTimer.singleShot(100, lambda: setattr(self, '_ql_ready', True))

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
            new_dirs = dialog.get_directories()
            # 记录被显式删除的路径：保存时会与磁盘做并集，必须把这些路径剔除，
            # 否则它们会被磁盘上的旧版本（或其它窗口）复活
            removed = set(self.working_dir_history) - set(new_dirs)
            if removed:
                pending = getattr(self, '_dir_history_pending_removals', None) or set()
                self._dir_history_pending_removals = pending | removed
            # 更新目录历史
            self.working_dir_history = new_dirs
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
                    terminal.split_horizontal_requested.disconnect()
                    terminal.split_vertical_requested.disconnect()
                    terminal.rename_split_requested.disconnect()
                    terminal.attention_requested.disconnect()
                    terminal.interaction_requested.disconnect()
                except (TypeError, RuntimeError):
                    pass  # Signal may already be disconnected

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
                terminal.split_horizontal_requested.connect(lambda: self._split_current_tab(self._shift_held()))
                terminal.split_vertical_requested.connect(lambda: self._split_vertical_current_terminal(self._shift_held()))
                terminal.move_split_left_requested.connect(self._move_split_left)
                terminal.move_split_up_requested.connect(self._move_split_up)
                terminal.rename_split_requested.connect(lambda t=terminal: self._rename_split(t))
                terminal.attention_requested.connect(lambda t=terminal: self._on_terminal_attention(t))
                terminal.interaction_requested.connect(lambda t=terminal: self._on_terminal_interaction(t))
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
        terminal.split_horizontal_requested.connect(lambda: self._split_current_tab(self._shift_held()))
        terminal.split_vertical_requested.connect(lambda: self._split_vertical_current_terminal(self._shift_held()))
        terminal.move_split_left_requested.connect(self._move_split_left)
        terminal.move_split_up_requested.connect(self._move_split_up)
        terminal.rename_split_requested.connect(lambda t=terminal: self._rename_split(t))
        terminal.attention_requested.connect(lambda t=terminal: self._on_terminal_attention(t))
        terminal.interaction_requested.connect(lambda t=terminal: self._on_terminal_interaction(t))

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
        elif event.type() == QEvent.Type.KeyPress:
            # 用户在点亮绿点的来源终端中按键 → 视为已响应该交互提示，清除绿点。
            # （来自其他终端/Git 面板的提醒不受影响，仍靠切窗口/切 tab 清除）
            if obj is getattr(self, '_nav_attention_source', None):
                self._clear_nav_attention()
        return super().eventFilter(obj, event)

    def _shift_held(self):
        """是否按住 Shift —— 按住时分屏作用于整个标签页，而非当前小窗口"""
        return bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)

    def _styled_splitter(self, orientation):
        """创建一个带统一手柄样式的 QSplitter"""
        splitter = QSplitter(orientation)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #3d3d5c;
            }
            QSplitter::handle:hover {
                background-color: #667eea;
            }
        """)
        return splitter

    def _restore_tab_close_button(self, idx):
        """为第 idx 个标签页重新创建右上角的关闭按钮（removeTab 会丢弃原按钮）"""
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

    def _wrap_tab_page(self, idx, orientation, new_terminal):
        """把整个标签页内容包进一个新的 orientation 方向的 splitter，并加入 new_terminal。

        用于对整个标签页（而非单个小窗口）进行分屏：让 new_terminal 贯穿整个宽（垂直分屏）
        或整个高（水平分屏）。会更新 tab_splitters[idx] 指向新的外层 splitter，
        以保持 “标签页页面控件 == tab_splitters[idx]” 的不变式（detach / 重建映射依赖它）。
        """
        old_page = self.tab_splitters.get(idx)
        outer = self._styled_splitter(orientation)
        title = self.tab_widget.tabText(idx)
        was_current = self.tab_widget.currentIndex() == idx

        # 先把旧页面从 tab 中摘下（removeTab 不销毁控件，Python 引用仍在），再重组
        self.tab_widget.removeTab(idx)
        outer.addWidget(old_page)
        outer.addWidget(new_terminal)
        old_page.show()

        self.tab_widget.insertTab(idx, outer, title)
        self._restore_tab_close_button(idx)
        if was_current:
            self.tab_widget.setCurrentIndex(idx)

        self.tab_splitters[idx] = outer

        if orientation == Qt.Orientation.Horizontal:
            size = outer.width() if outer.width() > 0 else 800
        else:
            size = outer.height() if outer.height() > 0 else 600
        outer.setSizes([size // 2, size // 2])
        return outer

    def _split_current_tab(self, whole_tab=False):
        """左右分屏。

        默认只分裂当前活动终端所在的小窗口；按住 Shift（whole_tab=True）时
        对整个标签页进行左右分屏，新终端贯穿整个高度。
        """
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

        terminals = self.tab_terminals.get(idx, [])
        new_terminal = self._create_terminal()

        # 没有有效活动终端时，也按整页分屏处理
        split_whole = whole_tab or not self.active_terminal or self.active_terminal not in terminals

        if split_whole:
            # 对整个标签页左右分屏：新终端成为贯穿整高的一列
            top = splitter
            if top.orientation() == Qt.Orientation.Horizontal:
                top.addWidget(new_terminal)
                count = top.count()
                total_width = top.width()
                top.setSizes([total_width // count] * count)
            else:
                # 顶层是垂直方向，需要包裹整页才能让新列贯穿整高
                self._wrap_tab_page(idx, Qt.Orientation.Horizontal, new_terminal)
        else:
            # 只分裂当前活动终端所在的小窗口
            parent_widget = self.active_terminal.parent()
            if not isinstance(parent_widget, QSplitter):
                self.statusbar.showMessage(t("msg.cannot_find_container"), 3000)
                new_terminal.deleteLater()
                return

            parent_splitter = parent_widget
            terminal_index = parent_splitter.indexOf(self.active_terminal)

            if parent_splitter.orientation() == Qt.Orientation.Horizontal:
                # 父级已是水平方向：直接在活动终端右侧插入，把原终端的空间一分为二
                parent_sizes = parent_splitter.sizes()
                parent_splitter.insertWidget(terminal_index + 1, new_terminal)
                if terminal_index < len(parent_sizes):
                    orig = parent_sizes[terminal_index]
                    new_sizes = list(parent_sizes)
                    new_sizes[terminal_index] = orig // 2
                    new_sizes.insert(terminal_index + 1, orig - orig // 2)
                    parent_splitter.setSizes(new_sizes)
            else:
                # 父级是垂直方向：把原终端包裹进一个新的水平 splitter
                parent_sizes = parent_splitter.sizes()
                original_terminal = self.active_terminal
                horizontal_splitter = self._styled_splitter(Qt.Orientation.Horizontal)
                horizontal_splitter.addWidget(original_terminal)
                horizontal_splitter.addWidget(new_terminal)
                parent_splitter.insertWidget(terminal_index, horizontal_splitter)
                if parent_sizes and len(parent_sizes) == parent_splitter.count():
                    parent_splitter.setSizes(parent_sizes)
                h_width = horizontal_splitter.width() if horizontal_splitter.width() > 0 else 400
                horizontal_splitter.setSizes([h_width // 2, h_width // 2])

        # 更新终端列表
        self.tab_terminals[idx].append(new_terminal)

        # 启动 shell 在当前终端的工作目录
        new_terminal.start_process([get_default_shell()], cwd=current_cwd)

        # 设置新终端为活动终端
        self.active_terminal = new_terminal
        new_terminal.setFocus()

        count = len(self.tab_terminals[idx])
        msg = "status.split_tab_done" if split_whole else "status.split_done"
        self.statusbar.showMessage(t(msg, count=count), 3000)

    def _split_vertical_current_terminal(self, whole_tab=False):
        """上下分屏。

        默认只分裂当前活动终端所在的小窗口；按住 Shift（whole_tab=True）时
        对整个标签页进行上下分屏，新终端贯穿整个宽度。
        """
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        splitter = self.tab_splitters.get(idx)
        if not splitter:
            return

        terminals = self.tab_terminals.get(idx, [])

        # 获取当前终端的工作目录，回退到标签页的工作目录，再回退到窗口级别的工作目录
        current_cwd = None
        if self.active_terminal and self.active_terminal.is_running():
            current_cwd = self.active_terminal.get_cwd()
        if not current_cwd:
            current_cwd = self.tab_cwds.get(idx, self._window_cwd)

        new_terminal = self._create_terminal()

        # 没有有效活动终端时，也按整页分屏处理
        split_whole = whole_tab or not self.active_terminal or self.active_terminal not in terminals

        if split_whole:
            # 对整个标签页上下分屏：新终端成为贯穿整宽的一行
            top = splitter
            if top.orientation() == Qt.Orientation.Vertical:
                top.addWidget(new_terminal)
                count = top.count()
                total_height = top.height()
                top.setSizes([total_height // count] * count)
            else:
                # 顶层是水平方向，需要包裹整页才能让新行贯穿整宽
                self._wrap_tab_page(idx, Qt.Orientation.Vertical, new_terminal)
        else:
            # 只分裂当前活动终端所在的小窗口
            parent_widget = self.active_terminal.parent()
            if not isinstance(parent_widget, QSplitter):
                self.statusbar.showMessage(t("msg.cannot_find_container"), 3000)
                new_terminal.deleteLater()
                return

            parent_splitter = parent_widget
            terminal_index = parent_splitter.indexOf(self.active_terminal)
            parent_sizes = parent_splitter.sizes()

            if parent_splitter.orientation() == Qt.Orientation.Vertical:
                # 父级已是垂直方向：直接在活动终端下方插入，把原终端的空间一分为二
                parent_splitter.insertWidget(terminal_index + 1, new_terminal)
                if terminal_index < len(parent_sizes):
                    orig = parent_sizes[terminal_index]
                    new_sizes = list(parent_sizes)
                    new_sizes[terminal_index] = orig // 2
                    new_sizes.insert(terminal_index + 1, orig - orig // 2)
                    parent_splitter.setSizes(new_sizes)
            else:
                # 父级是水平方向：把原终端包裹进一个新的垂直 splitter
                original_terminal = self.active_terminal
                vertical_splitter = self._styled_splitter(Qt.Orientation.Vertical)
                vertical_splitter.addWidget(original_terminal)
                vertical_splitter.addWidget(new_terminal)
                parent_splitter.insertWidget(terminal_index, vertical_splitter)
                if parent_sizes and len(parent_sizes) == parent_splitter.count():
                    parent_splitter.setSizes(parent_sizes)
                v_height = vertical_splitter.height() if vertical_splitter.height() > 0 else 400
                vertical_splitter.setSizes([v_height // 2, v_height // 2])

        # 更新终端列表
        self.tab_terminals[idx].append(new_terminal)

        # 启动新终端
        new_terminal.start_process([get_default_shell()], cwd=current_cwd)

        # 设置新终端为活动终端
        self.active_terminal = new_terminal
        new_terminal.setFocus()

        count = len(self.tab_terminals[idx])
        msg = "status.vsplit_tab_done" if split_whole else "status.vsplit_done"
        self.statusbar.showMessage(t(msg, count=count), 3000)

    def _collapse_singleton_splitter(self, splitter, idx):
        """若某个嵌套 splitter 关闭后只剩一个子组件，则解除这层嵌套：

        用唯一的子组件替换该 splitter，并继承它在父 splitter 中的位置和尺寸。
        这样剩下的分屏会自动扩展占满原来的区域，同时**完全不影响**父 splitter
        里其它分屏的尺寸。顶层标签页 splitter 不会被解除。
        """
        top = self.tab_splitters.get(idx)
        while (
            isinstance(splitter, QSplitter)
            and splitter is not top
            and splitter.count() == 1
        ):
            grandparent = splitter.parent()
            if not isinstance(grandparent, QSplitter):
                break
            child = splitter.widget(0)
            gp_index = grandparent.indexOf(splitter)
            gp_sizes = grandparent.sizes()  # 关闭前父级各分屏的尺寸，需原样保留
            # 把唯一子组件移动到父 splitter 中 splitter 原来的位置
            child.setParent(None)
            grandparent.insertWidget(gp_index, child)
            # 删除已空的嵌套 splitter
            splitter.setParent(None)
            splitter.deleteLater()
            # 恢复父 splitter 的尺寸分配（其它分屏宽/高保持不变）
            if len(gp_sizes) == grandparent.count():
                grandparent.setSizes(gp_sizes)
            # 继续向上检查（一般一层即可）
            splitter = grandparent

    def _close_current_split(self):
        """关闭当前聚焦的分屏终端。

        只在该终端所在的局部 splitter 范围内回收空间，空出的空间交给相邻分屏
        自动扩展，**不影响**其它 splitter / 分屏的尺寸。
        """
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

        # 记录被关闭终端所在的父 splitter 及其尺寸（只在这个局部范围内重新分配空间）
        parent = terminal_to_close.parent()
        parent_sizes = parent.sizes() if isinstance(parent, QSplitter) else None
        close_index = parent.indexOf(terminal_to_close) if isinstance(parent, QSplitter) else -1

        # 完整清理终端资源
        terminal_to_close.cleanup()

        # 从列表中移除
        terminals.remove(terminal_to_close)

        # 从分屏容器中移除并销毁
        terminal_to_close.setParent(None)
        terminal_to_close.deleteLater()

        # 在局部父 splitter 内，把空出的空间合并给相邻分屏（其它分屏尺寸不变）
        if isinstance(parent, QSplitter) and parent_sizes and 0 <= close_index < len(parent_sizes):
            freed = parent_sizes[close_index]
            new_sizes = parent_sizes[:close_index] + parent_sizes[close_index + 1:]
            if new_sizes:
                # 优先把空间给前一个分屏，否则给后一个
                give = close_index - 1 if close_index - 1 >= 0 else 0
                new_sizes[give] += freed
                if len(new_sizes) == parent.count():
                    parent.setSizes(new_sizes)

        # 若父 splitter 因此只剩一个子组件，解除这层嵌套，让剩余分屏自动扩展
        self._collapse_singleton_splitter(parent, idx)

        # 更新活动终端为剩余的第一个
        if terminals:
            self.active_terminal = terminals[0]
            terminals[0].setFocus()

        self.statusbar.showMessage(t("status.close_split_done", count=len(terminals)), 3000)

    def _move_split_left(self):
        """将当前分屏与左边的分屏交换位置"""
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        splitter = self.tab_splitters.get(idx)
        if not splitter:
            return

        # 找到当前活动终端在 splitter 中的索引
        terminal = self.active_terminal
        if not terminal:
            return

        # 查找终端（或其父 vertical splitter）在主 splitter 中的位置
        widget_in_splitter = terminal
        while widget_in_splitter.parent() != splitter:
            widget_in_splitter = widget_in_splitter.parent()
            if widget_in_splitter is None:
                return

        current_index = splitter.indexOf(widget_in_splitter)
        if current_index <= 0:
            self.statusbar.showMessage(t("status.move_split_left_fail"), 3000)
            return

        # 保存当前 sizes
        sizes = splitter.sizes()

        # 交换：把当前 widget 插入到左边位置
        splitter.insertWidget(current_index - 1, widget_in_splitter)

        # 交换 sizes
        sizes[current_index], sizes[current_index - 1] = sizes[current_index - 1], sizes[current_index]
        splitter.setSizes(sizes)

        # 同步 tab_terminals 列表中的顺序
        terminals = self.tab_terminals.get(idx, [])
        term_idx = terminals.index(terminal) if terminal in terminals else -1
        if term_idx > 0:
            terminals[term_idx], terminals[term_idx - 1] = terminals[term_idx - 1], terminals[term_idx]

        terminal.setFocus()
        self.statusbar.showMessage(t("status.move_split_left_done"), 3000)

    def _move_split_up(self):
        """在垂直分屏内，把当前终端与上方的兄弟交换位置"""
        terminal = self.active_terminal
        if not terminal:
            return
        parent_splitter = terminal.parent()
        if not isinstance(parent_splitter, QSplitter):
            self.statusbar.showMessage(t("status.move_split_up_fail"), 3000)
            return
        if parent_splitter.orientation() != Qt.Orientation.Vertical:
            self.statusbar.showMessage(t("status.move_split_up_fail"), 3000)
            return
        current_index = parent_splitter.indexOf(terminal)
        if current_index <= 0:
            self.statusbar.showMessage(t("status.move_split_up_fail"), 3000)
            return
        sizes = parent_splitter.sizes()
        parent_splitter.insertWidget(current_index - 1, terminal)
        sizes[current_index], sizes[current_index - 1] = sizes[current_index - 1], sizes[current_index]
        parent_splitter.setSizes(sizes)
        terminal.setFocus()
        self.statusbar.showMessage(t("status.move_split_up_done"), 3000)

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

        # 移除标签页。removeTab 会同步发出 currentChanged，而此时 tab_cwds 等映射
        # 还是旧索引 → _on_tab_changed 会用新索引查到被关 tab 的目录，导致
        # Directory/Current 回退到旧路径。先屏蔽信号，重建映射后再手动同步一次。
        self.tab_widget.blockSignals(True)
        try:
            self.tab_widget.removeTab(index)
        finally:
            self.tab_widget.blockSignals(False)

        # 更新映射（重建索引）
        self._rebuild_tab_mappings()

        # 映射已就绪，手动触发一次 tab 切换回调，让目录栏/导航面板同步到新当前 tab
        current = self.tab_widget.currentIndex()
        if current >= 0:
            self._on_tab_changed(current)

        # 如果没有标签页了，根据参数决定是否创建新的
        if self.tab_widget.count() == 0 and auto_create_new:
            self._add_new_tab()
            # 确保新 tab 的 UI 状态正确（启动按钮可用）
            self._update_running_state(False)

    def _close_current_tab(self):
        """关闭当前标签页"""
        self._close_tab(self.tab_widget.currentIndex())

    def _focus_in_editor_area(self) -> bool:
        """当前键盘焦点是否落在编辑器区域（某个文件编辑窗格）里。"""
        if not hasattr(self, 'editor_area') or not self.editor_area.isVisible():
            return False
        fw = QApplication.focusWidget()
        return fw is not None and (
            fw is self.editor_area or self.editor_area.isAncestorOf(fw)
        )

    def _close_tab_or_window(self):
        """关闭当前分屏/标签页/窗口 (Cmd+W)

        优先级：
        0. 如果焦点在编辑器窗格里，关闭当前选中的编辑器窗格
        1. 如果当前标签页有多个分屏，关闭当前选中的分屏
        2. 如果只有一个分屏，关闭整个标签页
        3. 如果没有标签页了，关闭窗口
        """
        # 焦点在编辑器里 → Cmd+W 关闭当前选中的编辑器窗格（而不是终端标签）
        if self._focus_in_editor_area():
            if self.editor_area.close_focused_pane():
                return

        idx = self.tab_widget.currentIndex()

        if idx >= 0:
            terminals = self.tab_terminals.get(idx, [])
            if len(terminals) > 1:
                # 有多个分屏，关闭当前选中的分屏
                self._close_current_split()
            else:
                # 只有一个分屏，关闭整个标签页。
                # 若这是最后一个标签页，这一步会退出整个窗口 → 一律二次确认，
                # 避免一次误触把整个窗口（布局/会话）丢掉。有进程在跑时用更强措辞。
                if self.tab_widget.count() == 1:
                    has_running_process = any(t.is_running() for t in terminals)
                    msg = (t("msg.confirm_close_last_tab") if has_running_process
                           else t("msg.confirm_close_window"))
                    reply = QMessageBox.question(
                        self, t("msg.confirm_close_title"), msg,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No  # 默认选择"否"，回车不会误关
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                    # 已经确认过了，self.close() 触发的 closeEvent 不必再问一次
                    self._force_closing = True

                self._close_tab(idx, auto_create_new=False)
                # 如果关闭后没有标签页了，关闭窗口
                if self.tab_widget.count() == 0:
                    self.close()
        else:
            # 没有标签页了，关闭整个窗口
            self.close()

    @staticmethod
    def _clamp_window_pos(x, y, w, h, ref_point):
        """把窗口左上角 (x, y) 限制在 ref_point 所在屏幕的可视区内，确保 w×h 的窗口
        完整可见。否则窗口越界部分会被 macOS 裁掉，导致「与父窗口同尺寸」的新窗口被压小。
        若窗口比屏幕还大，则贴住可视区左上角。"""
        try:
            from PyQt6.QtWidgets import QApplication
            scr = QApplication.screenAt(ref_point) or QApplication.primaryScreen()
            avail = scr.availableGeometry()
            if w >= avail.width():
                x = avail.left()
            else:
                x = max(avail.left(), min(x, avail.right() - w + 1))
            if h >= avail.height():
                y = avail.top()
            else:
                y = max(avail.top(), min(y, avail.bottom() - h + 1))
        except Exception:
            pass
        return x, y

    def _align_child_with_parent_geometry(self, new_window):
        """让子窗口与本窗口逐像素重合（位置+尺寸），并持续校正 macOS 的异步微调。

        macOS 对新建原生窗口可能自行级联偏移、约束到屏幕（位置右移/尺寸压窄），
        且调整常发生在首帧之后——单次 setGeometry（或固定次数的少量重试）会被
        悄悄覆盖，这正是「新窗口宽度与父窗口对不齐」的根因。这里在 ~3s 内反复
        断言目标几何，连续 3 次确认无偏差才收手；每次几何就位后还把左侧栏宽度
        按共享值对齐——窗口被压窄再校正回来时 QSplitter 的比例缩放会破坏左侧栏
        的绝对像素宽度，而 _prime_left_panel_sync 的 300ms 兜底可能跑在几何
        稳定之前，必须在这里补一次。
        """
        parent_maximized = self.isMaximized()
        target_geo = self.geometry()
        logger.info(
            "[align] start: parent_geo=%s parent_max=%s child_visible=%s child_geo=%s",
            target_geo, parent_maximized, new_window.isVisible(), new_window.geometry())
        if parent_maximized:
            # 先尝试直接继承最大化状态（对最大化几何做 setGeometry 经常不生效）。
            # 注意 showMaximized 可能被台前调度（Stage Manager）拦下而静默失败
            # ——子窗口拿到被压窄的普通几何且从未进入最大化状态。校正循环里
            # 会检测这种情况并退回逐像素几何断言。
            new_window.showMaximized()
        else:
            if new_window.isVisible():
                # 已显示的窗口（拖拽松手路径）：当前几何必然 ≠ 目标，直接断言
                new_window.setGeometry(target_geo)
            else:
                # 未显示的窗口（菜单 expand 路径）：不能在 show 前就把几何设成
                # 目标值——macOS 可能在首次显示时自行挪动/压窄原生窗口，而 Qt
                # 侧缓存仍等于目标值，之后的 setGeometry 全被当作「无变化」
                # 跳过，一次都不会真正下发，窗口永远校不回来。拖拽路径之所以
                # 可靠，正是因为窗口先显示在别处、对齐时必然发生一次真实的
                # 几何变化。这里模仿它：刻意偏移一点显示，让校正循环的首次
                # setGeometry 成为真实变化。
                ox, oy = MainWindow._clamp_window_pos(
                    target_geo.x() + 24, target_geo.y() + 24,
                    target_geo.width(), target_geo.height(),
                    target_geo.center())
                if (ox, oy) == (target_geo.x(), target_geo.y()):
                    # 父窗口贴满可视区时偏移会被钳回原位，强制保留 1px 差异
                    oy += 1
                new_window.setGeometry(
                    ox, oy, target_geo.width(), target_geo.height())
                new_window.show()

        def _fix_left_width():
            """左侧栏宽度对齐到共享值（偏差 >2px 才动，避免抖动）"""
            try:
                sw = MainWindow._shared_left_panel_width
                if isinstance(sw, int) and sw > 0 and hasattr(new_window, 'main_splitter'):
                    sizes = new_window.main_splitter.sizes()
                    if sizes and sizes[0] > 0 and abs(sizes[0] - sw) > 2:
                        new_window._apply_shared_left_panel_width(sw)
            except Exception:
                pass

        def _realign(attempt=0, stable=0):
            if sip.isdeleted(new_window):
                return
            logger.info(
                "[align] tick %d: stable=%d child_max=%s child_geo=%s frame=%s target=%s",
                attempt, stable, new_window.isMaximized(),
                new_window.geometry(), new_window.frameGeometry(), target_geo)
            if new_window.isMaximized():
                # 子窗口确已最大化：几何由系统接管，只校左侧栏
                _fix_left_width()
                stable += 1
            elif parent_maximized and attempt < 3:
                # showMaximized 可能尚未生效（异步/动画），先等几个 tick。
                # 若被台前调度拦下（子窗口始终进不了最大化状态），从第 3 个
                # tick 起退回下面的逐像素几何断言。
                stable = 0
            elif new_window.geometry() != target_geo:
                new_window.setGeometry(target_geo)
                stable = 0
            elif attempt < 8:
                # 前几个 tick 即使 Qt 侧已读到目标几何，也强制重新下发一次：
                # 刚显示的窗口可能被系统（台前调度 Stage Manager、屏幕约束等）
                # 挪走而 Qt 几何缓存未同步——看似已对齐实则没有，直接 setGeometry
                # 会被 Qt 当作「无变化」跳过。先把高度收 1px 制造真实变化再设回
                # 目标（向屏幕内收缩永远合法，不会反过来触发系统约束；位置偏移
                # 则可能顶到菜单栏被再次约束）。两次调用在同一事件循环内完成，
                # 不会渲染出中间态。
                new_window.resize(target_geo.width(), target_geo.height() - 1)
                new_window.setGeometry(target_geo)
                stable = 0
            else:
                stable += 1
                _fix_left_width()
            if stable >= 3:
                logger.info("[align] settled at tick %d: child_geo=%s", attempt, new_window.geometry())
                return
            if attempt < 24:
                QTimer.singleShot(120, lambda: _realign(attempt + 1, stable))
            else:
                logger.info("[align] gave up after tick %d: child_geo=%s target=%s",
                            attempt, new_window.geometry(), target_geo)
        QTimer.singleShot(0, _realign)

    def _detach_tab(self, index, global_pos, follow_drag=True):
        """将标签页分离为独立窗口（创建完整的 MainWindow）

        follow_drag=True 时新窗口跟随鼠标拖拽（拖出标签触发）；
        False 时直接在父窗口附近层叠展开（右键菜单触发）。
        """
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

        # removeTab 触发的 currentChanged 发生在重建映射「之前」，那时读到的是错位的
        # tab_cwds（可能正好读成被分离标签的目录）。这里按重建后的正确索引再同步一次，
        # 让残留窗口的 Directory 输入框与 Current 标签都回到真正的当前标签目录。
        cur_idx = self.tab_widget.currentIndex()
        if cur_idx >= 0:
            self._on_tab_changed(cur_idx)

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

        # 继承父窗口面板的开关状态（Explorer / Git / Remote 互斥，开一个即可；
        # Log 独立），让分离出的窗口与父窗口外观一致，不造成认知负担
        try:
            if getattr(self, 'explorer_panel_visible', False):
                new_window._toggle_explorer_panel()
            elif getattr(self, 'git_panel_visible', False):
                new_window._toggle_git_panel()
            elif getattr(self, 'remote_panel_visible', False):
                new_window._toggle_remote_panel()
            if getattr(self, 'log_panel_visible', False):
                new_window._toggle_log_panel()
        except Exception:
            pass

        # 新窗口初始尺寸继承产生它的父窗口（拖拽过程中作为可移动窗口跟随光标，
        # 故不沿用最大化状态，只复制像素尺寸）
        try:
            new_window.resize(self.size())
        except Exception:
            pass

        if follow_drag:
            # 调整窗口位置让鼠标正好在标题栏上（抓取点见 _DETACH_GRAB_*）
            window_x = global_pos.x() - self._DETACH_GRAB_X
            window_y = global_pos.y() - self._DETACH_GRAB_Y
            # 保证窗口完整留在屏幕内：否则越界部分会被 macOS 裁掉，使「与父窗口
            # 同尺寸」的新窗口被压窄变小。
            window_x, window_y = MainWindow._clamp_window_pos(
                window_x, window_y, self.width(), self.height(), global_pos)
            new_window.move(window_x, window_y)
            new_window.show()
        else:
            # 菜单触发：与父窗口逐像素重合（持续校正 macOS 的异步微调，
            # 见 _align_child_with_parent_geometry）。
            self._align_child_with_parent_geometry(new_window)

        # 激活窗口
        new_window.raise_()
        new_window.activateWindow()

        # 添加到列表以跟踪
        self.detached_windows.append(new_window)

        # 菜单触发时无拖拽，直接聚焦新窗口终端即可
        if not follow_drag:
            if new_window.active_terminal:
                new_window.active_terminal.setFocus()
        else:
            self._start_detach_drag_follow(new_window)

        # 如果主窗口没有标签页了，创建一个新的
        if self.tab_widget.count() == 0:
            self._add_new_tab()
            self._update_running_state(False)

    def _start_detach_drag_follow(self, new_window):
        """拖拽分离后让新窗口跟随鼠标，松手时做吸附对齐。

        （macOS 上 startSystemMove() 因鼠标按下事件不在新窗口上而导致窗口漂移，
        故用 timer 轮询鼠标位置实现跟随。）
        """
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QCursor

        drag_offset_x = self._DETACH_GRAB_X
        drag_offset_y = self._DETACH_GRAB_Y
        drag_timer = QTimer()
        drag_timer.setInterval(16)  # ~60fps 平滑跟随
        new_window._detach_drag_timer = drag_timer  # prevent GC

        def _follow_mouse():
            if sip.isdeleted(new_window):
                drag_timer.stop()
                return
            buttons = QApplication.mouseButtons()
            if buttons & Qt.MouseButton.LeftButton:
                cursor_pos = QCursor.pos()
                mx, my = MainWindow._clamp_window_pos(
                    cursor_pos.x() - drag_offset_x,
                    cursor_pos.y() - drag_offset_y,
                    new_window.width(), new_window.height(), cursor_pos)
                new_window.move(mx, my)
            else:
                # 鼠标释放，停止拖拽跟随
                drag_timer.stop()
                if not sip.isdeleted(new_window) and new_window.isVisible():
                    # 吸附对齐：松手时若与父窗口的边缘只差一点（肉眼想对齐但差
                    # 几十像素），自动贴齐父窗口，消除"差一丁点错位"。
                    if not sip.isdeleted(self) and self.isVisible():
                        pf = self.frameGeometry()
                        nf = new_window.frameGeometry()
                        inter = pf.intersected(nf)
                        overlap = inter.width() * inter.height()
                        if overlap >= 0.6 * nf.width() * nf.height():
                            # 大面积叠在父窗口上 → 视为想完全重合，继承父窗口
                            # 几何（位置+尺寸逐像素一致，持续校正 macOS 微调）
                            self._align_child_with_parent_geometry(new_window)
                        else:
                            # 拖拽期间 macOS 可能把越界窗口悄悄压小（级联/约束
                            # 到屏幕），松手时先把尺寸还原成父窗口尺寸再做吸附，
                            # 位置按还原后的尺寸重新约束在屏幕内。
                            if (not self.isMaximized()
                                    and new_window.size() != self.size()):
                                new_window.resize(self.size())
                                cx, cy = MainWindow._clamp_window_pos(
                                    new_window.x(), new_window.y(),
                                    self.width(), self.height(),
                                    new_window.frameGeometry().center())
                                new_window.move(cx, cy)
                                nf = new_window.frameGeometry()
                            SNAP = 56
                            nx, ny = new_window.x(), new_window.y()
                            # 上对齐 / 贴在父窗口正下方
                            if abs(ny - pf.y()) <= SNAP:
                                ny = pf.y()
                            elif abs(ny - pf.bottom()) <= SNAP:
                                ny = pf.bottom() + 1
                            # 左对齐 / 贴在父窗口右侧 / 贴在父窗口左侧
                            if abs(nx - pf.x()) <= SNAP:
                                nx = pf.x()
                            elif abs(nx - (pf.right() + 1)) <= SNAP:
                                nx = pf.right() + 1
                            elif abs((nx + nf.width()) - pf.x()) <= SNAP:
                                nx = pf.x() - nf.width()
                            if (nx, ny) != (new_window.x(), new_window.y()):
                                new_window.move(nx, ny)
                    new_window.raise_()
                    new_window.activateWindow()
                    if new_window.active_terminal:
                        new_window.active_terminal.setFocus()

        drag_timer.timeout.connect(_follow_mouse)
        drag_timer.start()

    def _rebuild_tab_mappings(self):
        """重建标签页映射"""
        new_splitters = {}
        new_terminals = {}
        new_sessions = {}
        new_cwds = {}
        old_to_new = {}  # 旧索引 -> 新索引，用于同步按 tab 索引存储的其他状态
        for i in range(self.tab_widget.count()):
            widget = self.tab_widget.widget(i)
            if widget:
                # 找到对应的旧映射
                for old_idx, splitter in self.tab_splitters.items():
                    if splitter is widget:
                        old_to_new[old_idx] = i
                        new_splitters[i] = splitter
                        new_terminals[i] = self.tab_terminals.get(old_idx, [])
                        new_sessions[i] = self.tab_sessions.get(old_idx)
                        new_cwds[i] = self.tab_cwds.get(old_idx, self._window_cwd)
                        break
        self.tab_splitters = new_splitters
        self.tab_terminals = new_terminals
        self.tab_sessions = new_sessions
        self.tab_cwds = new_cwds

        # 同步同样按 tab 索引存储的 OpenAI 服务器状态与 "查询后清除会话" 设置，
        # 否则关闭/分离左侧 tab 后这些 key 会指向错误的 tab。
        if hasattr(self, 'api_server_clear_after_query'):
            self.api_server_clear_after_query = {
                old_to_new[old_idx]: val
                for old_idx, val in self.api_server_clear_after_query.items()
                if old_idx in old_to_new
            }
        if hasattr(self, 'openai_server_manager'):
            self.openai_server_manager.remap_indices(old_to_new)

    def _on_tab_changed(self, index):
        """标签页切换时的回调"""
        # 切到别的 tab 也算"已查看"，清除提醒小标
        if self.isActiveWindow():
            self._clear_nav_attention()
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
            cwd_changed = (tab_cwd != getattr(self, '_window_cwd', None))
            self._window_cwd = tab_cwd
            if hasattr(self, 'current_dir_label'):
                self.current_dir_label.setText(t("dir.current", cwd=tab_cwd))
                self.current_dir_label.setToolTip(tab_cwd)
            # 让 "Directory:" 输入框也跟随当前标签页（之前只更新 Current 标签，
            # 导致分离标签页后输入框仍停留在被分离标签的目录上、与 Current 不一致）。
            if hasattr(self, 'working_dir_combo'):
                self.working_dir_combo.blockSignals(True)
                self.working_dir_combo.setCurrentText(tab_cwd)
                self.working_dir_combo.blockSignals(False)
            if hasattr(self, 'explorer_panel') and self.explorer_panel_visible:
                self.explorer_panel.set_root_path(tab_cwd)
            if hasattr(self, 'git_panel') and self.git_panel_visible:
                self.git_panel.set_repository(tab_cwd)
            # 本地命令是「目录级」的：tab 切到不同目录时必须重载，否则 local_presets
            # 仍是上一个目录的内容，而保存路径已指向当前目录 → 跨文件夹串写/覆盖。
            # （local_presets 始终是「磁盘加载」或「刚保存」的状态，无未落盘的内存修改，
            #   故重载是安全的，不会丢失编辑。）
            if cwd_changed:
                self._load_local_commands()

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
            new_title = f"{tab_name} - Smart Terminal"
            if new_title != self.windowTitle():
                self.setWindowTitle(new_title)
                # 立即刷新导航面板，让列表项即时跟随当前激活的 tab（本地/远程），
                # 不必等 5 秒轮询。
                try:
                    MainWindow._broadcast_navigator_refresh()
                except Exception:
                    pass

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
        # 用户自定义命名的标签不被自动改名覆盖
        _page = self.tab_widget.widget(tab_idx)
        if not getattr(_page, '_custom_tab_name', None):
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
            terminal.send_text(cmd + '\r')
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
            # 记录到会话粘贴图片列表（去重、保持最新在后），供“图片”按钮查看
            if file_path in self._pasted_images:
                self._pasted_images.remove(file_path)
            self._pasted_images.append(file_path)
            # 防止无限增长：只保留最近 200 张
            if len(self._pasted_images) > 200:
                self._pasted_images = self._pasted_images[-200:]
        else:
            self.statusbar.showMessage(t("status.pasted_file", path=file_path), 3000)

    def _show_pasted_images(self):
        """打开“已粘贴图片”画廊，双击缩略图可用系统看图打开。"""
        from pasted_images_dialog import PastedImagesDialog

        def _on_clear():
            self._pasted_images = []

        def _on_remove(path):
            if path in self._pasted_images:
                self._pasted_images.remove(path)

        dialog = PastedImagesDialog(
            self._pasted_images,
            on_clear=_on_clear,
            on_remove=_on_remove,
            parent=self,
        )
        dialog.exec()

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

        # 弹簧模式 checkbox：编辑器与终端左右并排时，点哪边哪边自动展宽
        self._spring_checkbox = QCheckBox(t("explorer.spring_mode"))
        self._spring_checkbox.setToolTip(t("explorer.spring_tooltip"))
        self._spring_checkbox.setStyleSheet(self._explorer_split_checkbox.styleSheet())
        self._spring_checkbox.setChecked(bool(getattr(self, '_spring_mode_enabled', False)))
        self._spring_checkbox.stateChanged.connect(self._on_spring_mode_toggled)
        explorer_header_layout.addWidget(self._spring_checkbox)

        # 视图设置按钮（齿轮）：弹出菜单，含"显示隐藏文件"开关
        self._explorer_settings_btn = QPushButton()
        self._explorer_settings_btn.setFixedSize(24, 24)
        self._explorer_settings_btn.setIconSize(QSize(16, 16))
        self._explorer_settings_btn.setIcon(_make_git_tool_icon('gear', '#888'))
        self._explorer_settings_btn.setToolTip(t("explorer.settings_tooltip"))
        self._explorer_settings_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.08);
                border-radius: 4px;
            }
        """)
        self._explorer_settings_btn.clicked.connect(self._show_explorer_settings_menu)
        explorer_header_layout.addWidget(self._explorer_settings_btn)

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

        # 内置文件编辑器（编辑器组：支持无限层级 split / v-split 并排查看不同文件）
        self.editor_area = EditorArea(theme=current_theme)
        self.editor_area.all_closed.connect(self._on_editor_closed)
        self.editor_area.active_changed.connect(self._on_active_pane_changed)
        self.editor_area.ai_completion_toggled.connect(self._on_ai_completion_toggled)
        # 应用已记忆的 AI 补全开关到编辑器
        self.editor_area.set_ai_completion_enabled(getattr(self, '_ai_completion_enabled', False))
        self.editor_area.hide()  # 默认隐藏
        self.explorer_splitter.addWidget(self.editor_area)

        # 连接资源管理器的保存信号到活动窗格
        self.explorer_panel.save_file_requested.connect(self.editor_area.save_active)
        self.explorer_panel.save_file_as_requested.connect(self.editor_area.save_active_as)

        # 设置初始比例（资源管理器占更多空间）
        self.explorer_splitter.setSizes([400, 0])

        # 记忆用户手动拖拽过的尺寸
        self.explorer_splitter.splitterMoved.connect(
            lambda *_: (self._on_splitter_drag_tick(),
                        self._capture_explorer_layout())
        )

        layout.addWidget(self.explorer_splitter)

    def _show_explorer_settings_menu(self):
        """Explorer 齿轮按钮：弹出视图设置菜单（含"显示隐藏文件"开关）。"""
        if not hasattr(self, 'explorer_panel'):
            return
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2d2d44;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 24px 6px 12px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #667eea;
            }
        """)
        act = menu.addAction(t("explorer.show_hidden_files"))
        act.setCheckable(True)
        act.setChecked(self.explorer_panel.is_showing_hidden())
        act.toggled.connect(self.explorer_panel.set_show_hidden)

        # 排序方式子菜单（名称 / 修改日期 / 大小 / 类型 + 升/降序）
        menu.addSeparator()
        cur_key, cur_desc = self.explorer_panel.get_sort()
        sort_menu = menu.addMenu(t("sort.by"))
        sort_menu.setStyleSheet(menu.styleSheet())
        sort_group = QActionGroup(sort_menu)
        sort_group.setExclusive(True)
        for key, label in (('name', t("sort.name")), ('modified', t("sort.modified")),
                           ('size', t("sort.size")), ('type', t("sort.type"))):
            a = sort_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == cur_key)
            sort_group.addAction(a)
            a.triggered.connect(
                lambda checked=False, k=key: self.explorer_panel.set_sort(
                    k, self.explorer_panel.get_sort()[1]))
        sort_menu.addSeparator()
        order_group = QActionGroup(sort_menu)
        order_group.setExclusive(True)
        for desc, label in ((False, t("sort.ascending")), (True, t("sort.descending"))):
            a = sort_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(desc == cur_desc)
            order_group.addAction(a)
            a.triggered.connect(
                lambda checked=False, d=desc: self.explorer_panel.set_sort(
                    self.explorer_panel.get_sort()[0], d))

        menu.exec(self._explorer_settings_btn.mapToGlobal(
            self._explorer_settings_btn.rect().bottomLeft()
        ))

    @property
    def file_editor(self):
        """兼容旧调用：返回编辑器组的活动窗格（窗格级操作用）。

        容器级操作（放置 / 显隐 / indexOf / setParent）请直接用 self.editor_area。
        editor_area 尚未创建时抛 AttributeError，使外部 hasattr 检查返回 False。
        """
        return self.editor_area.active_pane

    def _editor_has_any_file(self) -> bool:
        """编辑器组里是否有任意窗格打开了文件。"""
        if not hasattr(self, 'editor_area'):
            return False
        return any(p.get_current_file() for p in self.editor_area.panes)

    def _on_active_pane_changed(self):
        """活动窗格变化时，让资源管理器高亮跟随活动窗格当前文件。"""
        if not hasattr(self, 'explorer_panel'):
            return
        pane = self.editor_area.active_pane
        cur = pane.get_current_file() if pane else None
        if cur:
            self.explorer_panel.set_editing_file(cur)
        else:
            self.explorer_panel.clear_editing_file()

    def _open_file_in_editor(self, file_path: str):
        """在内置编辑器中打开文件（落到当前活动窗格）"""
        if not hasattr(self, 'editor_area'):
            return

        # 在活动窗格中打开文件
        if not self.editor_area.open_file_in_active(file_path):
            return

        # 通知资源管理器当前正在编辑的文件
        if hasattr(self, 'explorer_panel'):
            self.explorer_panel.set_editing_file(file_path)

        # 编辑器若已显示在正确的 splitter 里，打开新文件不再重新放置，
        # 避免扰动其它编辑窗格 / 分屏的尺寸。
        target = self.main_splitter if self._explorer_split_horizontal else self.explorer_splitter
        if not self._editor_placed_and_visible(target):
            if self._explorer_split_horizontal:
                # 左右分屏：编辑器放到 main_splitter 中（左面板和终端之间）
                self._place_editor_in_main_splitter()
            else:
                # 上下分屏：编辑器在 explorer_splitter 中（默认行为）
                self._place_editor_in_explorer_splitter()

        # 弹簧模式下打开文件：自动把编辑器展宽（用户刚要看这个文件）
        if self._spring_applicable():
            self._apply_spring('editor')

    def _capture_explorer_layout(self):
        """记录当前资源管理器/编辑器的尺寸用于下次还原

        - 仅在用户能看到完整布局时记录（相关 widget 都未折叠）
        - 通过 splitterMoved 信号触发，由 setSizes 引发的程序性变更也会进入此处，
          但目标布局各项均 > 0，记录无害
        """
        if not hasattr(self, 'editor_area'):
            return

        # 弹簧动画/程序性设置尺寸期间不记忆，避免把临时的偏置布局写进记忆值
        if getattr(self, '_applying_spring', False):
            return

        editor_in_main = self.main_splitter.indexOf(self.editor_area) >= 0
        editor_in_internal = self.explorer_splitter.indexOf(self.editor_area) >= 0

        # 1) 编辑器在 explorer_splitter 中（上下分屏）— 记录内部分屏尺寸
        if editor_in_internal and self.editor_area.isVisible():
            isizes = self.explorer_splitter.sizes()
            if len(isizes) == 2 and isizes[0] > 0 and isizes[1] > 0:
                self._saved_explorer_internal_sizes = list(isizes)

        # 2) main_splitter 处理（左面板宽度始终是 sizes[0]）
        msizes = self.main_splitter.sizes()
        left_visible = (
            getattr(self, 'explorer_panel_visible', False)
            or getattr(self, 'git_panel_visible', False)
            or getattr(self, 'remote_panel_visible', False)
        )

        if editor_in_main and self.editor_area.isVisible() and len(msizes) == 4:
            # 4 widget: 左面板 + 编辑器 + 终端 + 日志
            if msizes[0] > 0 and msizes[1] > 0 and msizes[2] > 0:
                self._saved_explorer_main_sizes = list(msizes)
                self._set_left_panel_width(msizes[0])
        elif (not editor_in_main) and left_visible and len(msizes) >= 3 and msizes[0] > 0:
            # 3 widget: 左面板 + 终端 + 日志（无编辑器）
            self._set_left_panel_width(msizes[0])

    def _set_left_panel_width(self, width):
        """记录左侧栏宽度（进程级共享）并在拖动时实时联动到其它已打开窗口。

        拖动本窗口的左侧栏分隔条会经 splitterMoved → _capture_explorer_layout
        进到这里：宽度有变化时立刻推给其它展开了侧边栏的窗口，让它们同步变宽，
        减少窗口间切换的认知负担。被其它窗口同步过来时（_applying_shared_left_width）
        不再回传，避免来回触发。
        """
        if not isinstance(width, int) or width <= 0:
            return
        changed = (MainWindow._shared_left_panel_width != width)
        self._saved_left_panel_width = width
        if changed and not getattr(self, '_applying_shared_left_width', False):
            # 节流：拖拽时宽度每像素都在变，逐像素广播会让其它窗口每像素整窗重排。
            # 合并 80ms 内的变更只推最新值；定时器不重启，保证拖拽中也有周期性同步,
            # 最后一次 start 覆盖松手后的最终宽度。
            self._left_width_broadcast_pending = width
            if not self._left_width_broadcast_timer.isActive():
                self._left_width_broadcast_timer.start()

    def _broadcast_left_panel_width(self, width):
        """把左侧栏宽度实时应用到其它所有已打开窗口。"""
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            if w is self or not isinstance(w, MainWindow) or sip.isdeleted(w):
                continue
            w._apply_shared_left_panel_width(width)

    def _apply_shared_left_panel_width(self, width):
        """收到其它窗口同步过来的左侧栏宽度：仅当本窗口已展开侧边栏时实时重排。"""
        left_visible = (
            getattr(self, 'explorer_panel_visible', False)
            or getattr(self, 'git_panel_visible', False)
            or getattr(self, 'remote_panel_visible', False)
        )
        if not left_visible or not hasattr(self, 'main_splitter'):
            return
        # 标记「正在被同步」，使本窗口因 setSizes 触发的 splitterMoved 不再回传
        self._applying_shared_left_width = True
        try:
            self._update_splitter_sizes()
        finally:
            self._applying_shared_left_width = False

    def _prime_left_panel_sync(self):
        """启动/显示后主动建立跨窗口左侧栏联动，无需用户先手动拖一次才生效。

        若已存在进程级共享宽度（其它窗口确立的），本窗口直接采用；否则把本窗口当前
        左面板宽度确立为共享值，并强制广播给其它已展开侧栏的窗口，使各窗口立即对齐。
        强制广播绕开 _set_left_panel_width 里「宽度无变化则跳过」的判断——启动时各窗口
        宽度往往本就相同，那条判断会使首次广播被跳过，正是「要先拖一下才联动」的根因。
        """
        if not hasattr(self, 'main_splitter'):
            return
        left_visible = (
            getattr(self, 'explorer_panel_visible', False)
            or getattr(self, 'git_panel_visible', False)
            or getattr(self, 'remote_panel_visible', False)
        )
        if not left_visible:
            return
        shared = MainWindow._shared_left_panel_width
        if isinstance(shared, int) and shared > 0:
            # 采用其它窗口已确立的共享宽度
            self._applying_shared_left_width = True
            try:
                self._update_splitter_sizes()
            finally:
                self._applying_shared_left_width = False
        else:
            # 本窗口作为种子：确立共享宽度并强制广播
            sizes = self.main_splitter.sizes()
            left_width = sizes[0] if sizes else 0
            if left_width > 0:
                MainWindow._shared_left_panel_width = left_width
                self._broadcast_left_panel_width(left_width)

    def _resolve_main_splitter_sizes_with_editor(self):
        """计算编辑器在 main_splitter 中时的目标尺寸（优先使用记忆值）

        QSplitter 会按实际宽度对 setSizes 入参做比例归一化，因此各项之和必须
        等于 splitter 的实际宽度，才能让记忆的绝对像素值被原样还原。
        """
        log_width = 300 if self.log_panel_visible else 0
        saved_left = getattr(self, '_saved_left_panel_width', None)
        saved_left = saved_left if isinstance(saved_left, int) and saved_left > 0 else None
        total = max(self.main_splitter.width(), 1000)

        saved = getattr(self, '_saved_explorer_main_sizes', None)
        if saved and len(saved) == 4 and saved[0] > 0 and saved[1] > 0 and saved[2] > 0:
            left = saved_left if saved_left is not None else saved[0]
            editor = saved[1]
            terminal = max(100, total - left - editor - log_width)
            return [left, editor, terminal, log_width]
        # 默认值：左面板 300（或记忆值）, 编辑器 400, 其余给终端
        left = saved_left if saved_left is not None else 300
        editor = 400
        terminal = max(100, total - left - editor - log_width)
        return [left, editor, terminal, log_width]

    def _resolve_explorer_splitter_sizes_with_editor(self):
        """计算编辑器在 explorer_splitter 中时的目标尺寸（优先使用记忆值）"""
        saved = getattr(self, '_saved_explorer_internal_sizes', None)
        if saved and len(saved) == 2 and saved[0] > 0 and saved[1] > 0:
            return list(saved)
        return [200, 400]

    def _editor_placed_and_visible(self, splitter) -> bool:
        """编辑器已经在目标 splitter 里且可见。

        这种情况下「打开另一个文件」只需把内容换到活动窗格即可，不应再调用
        _place_editor_in_*（它会对外层 splitter setSizes，从而等比重排内部各编辑
        窗格、扰动其它分屏的宽高）。仅首次打开 / 切换分屏方向时才需要重新放置。
        """
        return (
            hasattr(self, 'editor_area')
            and self.editor_area.isVisible()
            and splitter.indexOf(self.editor_area) >= 0
        )

    def _place_editor_in_main_splitter(self):
        """将编辑器放到 main_splitter 中（左右分屏模式）"""
        if self.main_splitter.indexOf(self.editor_area) >= 0:
            # 已经在 main_splitter 中，只需确保可见并调整大小
            self.editor_area.show()
        else:
            # 从 explorer_splitter 中取出
            self.editor_area.setParent(None)
            self.editor_area.show()
            # 插入到 main_splitter 的 index 1（left_panel 和 tab_widget 之间）
            self.main_splitter.insertWidget(1, self.editor_area)

        self.main_splitter.setSizes(self._resolve_main_splitter_sizes_with_editor())

        # explorer_splitter 中只剩文件树，让它占满
        self.explorer_splitter.setSizes([400, 0])

    def _place_editor_in_explorer_splitter(self):
        """将编辑器放到 explorer_splitter 中（上下分屏模式）"""
        if self.explorer_splitter.indexOf(self.editor_area) >= 0:
            # 已经在 explorer_splitter 中，只需确保可见并调整大小
            self.editor_area.show()
        else:
            # 从 main_splitter 中取出
            self.editor_area.setParent(None)
            self.editor_area.show()
            # 放回 explorer_splitter
            self.explorer_splitter.addWidget(self.editor_area)

        self.explorer_splitter.setSizes(self._resolve_explorer_splitter_sizes_with_editor())

        # 恢复 main_splitter 正常比例
        self._update_splitter_sizes()

    def _on_editor_closed(self):
        """编辑器关闭时"""
        if hasattr(self, 'editor_area'):
            self.editor_area.hide()
            # 确保编辑器回到 explorer_splitter（归位）
            if self.explorer_splitter.indexOf(self.editor_area) < 0:
                self.editor_area.setParent(None)
                self.explorer_splitter.addWidget(self.editor_area)
                self.editor_area.hide()
            # 恢复资源管理器占据全部空间
            self.explorer_splitter.setSizes([400, 0])
            # 恢复 main_splitter 正常比例
            self._update_splitter_sizes()
        # 清除资源管理器中的编辑文件标记
        if hasattr(self, 'explorer_panel'):
            self.explorer_panel.clear_editing_file()

    def _toggle_editor_collapsed(self):
        """收起 / 展开已打开的文件区（Ctrl+E，可在「键盘快捷键」里改）。

        与「关闭」不同：收起只是隐藏 editor_area 腾出屏幕空间，已打开的文件和
        split 分屏结构仍保留在内存中，再次触发即原样展开。没有任何已打开文件
        时不做切换，仅在状态栏提示。
        """
        if not hasattr(self, 'editor_area'):
            return
        if not self._editor_has_any_file():
            self.statusbar.showMessage(t("status.editor_no_file"), 2000)
            return

        if self.editor_area.isVisible():
            # 收起：隐藏并把空间还给资源管理器/终端（保留文件，不清除编辑标记）
            self.editor_area.hide()
            if self.explorer_splitter.indexOf(self.editor_area) < 0:
                self.editor_area.setParent(None)
                self.explorer_splitter.addWidget(self.editor_area)
                self.editor_area.hide()
            self.explorer_splitter.setSizes([400, 0])
            self._update_splitter_sizes()
        else:
            # 展开：按当前分屏方向重新放置并显示
            if self._explorer_split_horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_explorer_splitter()
            # 先弹宽编辑器，再移焦点。顺序很重要：_apply_spring 会先把
            # _spring_current_side 置为 'editor'，这样紧接着 setFocus 触发的
            # focusChanged → _on_focus_changed_for_spring 会因「目标侧已是 editor」
            # 提前返回，不会再 stop/重启一次动画（否则动画「起步即被打断」会卡一下）。
            if self._spring_applicable():
                self._apply_spring('editor')
            # 把键盘焦点移到编辑器活动窗格。否则从终端用 Cmd+E 展开后焦点仍留在
            # 终端，与「编辑器被弹宽」的状态不一致：随后点击终端因焦点未变化而不
            # 触发 focusChanged，弹簧无法把终端展宽。聚焦编辑器后状态一致，再点
            # 终端会正常 focusChanged → 弹宽终端。
            pane = self.editor_area.active_pane
            if pane is not None:
                pane.editor.setFocus()

    def _on_explorer_split_orientation_changed(self, state):
        """切换资源管理器与编辑器的分屏方向"""
        horizontal = (state == Qt.CheckState.Checked.value)
        self._explorer_split_horizontal = horizontal

        # 如果编辑器正在显示，立即切换位置
        if hasattr(self, 'editor_area') and self.editor_area.isVisible():
            if horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_explorer_splitter()
        # 切回上下分屏时弹簧失去意义，重置已展开侧标记
        if not horizontal:
            self._spring_current_side = None

    # ---------- 弹簧模式：编辑器 / 终端 左右并排时点哪边哪边展宽 ----------
    # 比例：被聚焦的一侧占编辑器+终端合计宽度的大头，另一侧收窄但保留约 30%
    # （并设最小像素，避免在大屏上收得过窄、小屏上又超出合计宽度）。
    SPRING_INACTIVE_RATIO = 0.30
    SPRING_INACTIVE_MIN = 220
    # 弹簧按「单列宽度 = 编辑器+终端合计宽度 / 横向并排窗格列数」自动生效/失效
    # （滞回双阈值，防止边界反复横跳）：单列窄于 ENABLE → spring 生效；宽于 DISABLE → 失效；之间维持。
    # 为什么用单列宽度而非合计宽度：分屏越多，合计宽度被瓜分得越细、每个窗格越挤，spring 越该保留。
    # 合计与列数都不随 spring 挪分界而变（spring 只在编辑器/终端间移动分界），故不会反馈震荡。
    # 取值：超宽屏（3440）半屏窗口不分屏时单列 ≈715 应生效；单列 >1000（足够舒服）才自动失效恢复均衡。
    SPRING_PANE_ENABLE = 900
    SPRING_PANE_DISABLE = 1000

    def _set_spring_checkboxes(self, enabled: bool):
        """把本窗口两处弹簧复选框设为指定状态（屏蔽信号，避免回环触发）。"""
        for cb in (getattr(self, '_spring_checkbox', None),
                   getattr(self, '_remote_spring_checkbox', None)):
            if cb is not None and cb.isChecked() != enabled:
                cb.blockSignals(True)
                cb.setChecked(enabled)
                cb.blockSignals(False)

    def _broadcast_spring_state(self):
        """把弹簧模式开关同步到所有 MainWindow 窗口。

        常开多窗口时，若只改当前窗口，其它"旧"窗口仍持有旧值，等它们退出/保存
        配置时会把 spring_mode_enabled 覆盖回旧值，导致"下次启动没记住"。这里像
        排序/透明度一样把状态广播到每个窗口（含其复选框），保证配置落盘一致。
        """
        enabled = self._spring_mode_enabled
        app = QApplication.instance()
        if not app:
            return
        for widget in app.topLevelWidgets():
            if widget is self or not isinstance(widget, MainWindow):
                continue
            widget._spring_mode_enabled = enabled
            widget._set_spring_checkboxes(enabled)

    def _on_ai_completion_toggled(self, enabled: bool):
        """编辑器标题栏切换 AI 行内补全：应用到本窗口所有窗格、广播、落盘。"""
        enabled = bool(enabled)
        self._ai_completion_enabled = enabled
        if hasattr(self, 'editor_area') and self.editor_area is not None:
            self.editor_area.set_ai_completion_enabled(enabled)
        self._broadcast_ai_completion_state()
        self._save_config()
        # 开启但没有可用的 LLM 配置时，提示去配置（不阻断）
        if enabled:
            cfg = None
            try:
                cfg = self.get_llm_config()
            except Exception:
                cfg = None
            if not cfg or not (cfg.get('api_key') or '').strip():
                try:
                    self.statusBar().showMessage(
                        t("status.ai_need_llm_config"), 6000)
                except Exception:
                    pass

    def _broadcast_ai_completion_state(self):
        """把 AI 补全开关同步到所有 MainWindow 窗口（含其编辑器窗格），
        避免多窗口下旧窗口退出时把 ai_completion_enabled 覆盖回旧值。"""
        enabled = self._ai_completion_enabled
        app = QApplication.instance()
        if not app:
            return
        for widget in app.topLevelWidgets():
            if widget is self or not isinstance(widget, MainWindow):
                continue
            widget._ai_completion_enabled = enabled
            if hasattr(widget, 'editor_area') and widget.editor_area is not None:
                widget.editor_area.set_ai_completion_enabled(enabled)

    def _on_spring_mode_toggled(self, state):
        """弹簧模式开关（Explorer 与 Remote 两处复选框共用，保持同步）。"""
        enabled = (state == Qt.CheckState.Checked.value)
        self._spring_mode_enabled = enabled
        # 同步另一处复选框，避免两个面板状态不一致
        self._set_spring_checkboxes(enabled)
        # 同步到所有其它窗口，避免旧窗口退出时把配置覆盖回旧值
        self._broadcast_spring_state()
        if enabled:
            # 立即按当前焦点展开一侧；焦点不在两区时默认展开编辑器
            target = self._spring_target_for_widget(QApplication.focusWidget()) or 'editor'
            self._apply_spring(target)
        else:
            # 关闭：编辑器/终端像弹簧回弹一样均分宽度（带轻微过冲），之后用户手动拖拽定比例。
            # 注意不能用 _spring_applicable() 判断——它首先检查 _spring_mode_enabled，
            # 取消勾选时恒为 False，旧写法的恢复动画从未执行过，布局会停在偏置状态。
            self._spring_current_side = None
            if (hasattr(self, 'editor_area') and self.editor_area.isVisible()
                    and self.main_splitter.indexOf(self.editor_area) >= 0
                    and self.main_splitter.indexOf(self._main_content_stack) >= 0):
                sizes = self.main_splitter.sizes()
                ed_idx = self.main_splitter.indexOf(self.editor_area)
                term_idx = self.main_splitter.indexOf(self._main_content_stack)
                combined = sizes[ed_idx] + sizes[term_idx]
                if combined > 0:
                    new_sizes = list(sizes)
                    new_sizes[ed_idx] = combined // 2
                    new_sizes[term_idx] = combined - combined // 2
                    self._animate_main_sizes(
                        new_sizes, duration=300,
                        easing=QEasingCurve.Type.OutBack)
        self._save_config()

    def _spring_h_columns(self, container) -> int:
        """估算 container 内「横向并排」的叶子窗格列数（合计宽度由这些列瓜分）。

        水平 splitter → 各子列数累加；垂直 splitter → 取最大（堆叠不减宽）；叶子 → 1。
        分屏越多列数越大、单列越窄，从而让 spring 在更宽的窗口下也保持生效。
        """
        from PyQt6.QtWidgets import QSplitter
        if container is None:
            return 1
        sp = container if isinstance(container, QSplitter) else container.findChild(QSplitter)
        if sp is None:
            return 1

        def cols(w):
            if isinstance(w, QSplitter):
                cs = [cols(w.widget(i)) for i in range(w.count())
                      if w.widget(i) is not None and w.widget(i).isVisible()]
                if not cs:
                    return 1
                return sum(cs) if w.orientation() == Qt.Orientation.Horizontal else max(cs)
            return 1

        return cols(sp)

    def _update_spring_width_gate(self):
        """按「单列宽度 = 编辑器+终端合计宽度 / 横向并排列数」（滞回双阈值）更新 spring 门控，
        仅在门控翻转时联动调整布局。

        由 resizeEvent / 焦点切换 / 分隔条拖动驱动。只在跨阈值「生效↔失效」翻转时动一次，避免
        每像素都 setSizes。分屏越多 → 列数越大 → 单列越窄 → spring 在更宽窗口下也保持生效。
        合计宽度与列数都不随 spring 挪分界而变，故不会反馈震荡。取不到合计宽度时不动门控。
        """
        if not hasattr(self, 'main_splitter'):
            return
        # spring 自身动画期间 setSizes 会触发 splitterMoved → 误重入；合计宽度此时恒定，
        # 跳过即可，避免在动画帧里再调一次 setSizes。
        if getattr(self, '_applying_spring', False):
            return
        ed_idx = self.main_splitter.indexOf(self.editor_area) if hasattr(self, 'editor_area') else -1
        term_idx = self.main_splitter.indexOf(getattr(self, '_main_content_stack', None))
        sizes = self.main_splitter.sizes()
        if ed_idx < 0 or term_idx < 0 or max(ed_idx, term_idx) >= len(sizes):
            return
        combined = sizes[ed_idx] + sizes[term_idx]
        if combined <= 0:
            return
        # 横向并排列数 = 编辑器列数 + 当前标签页终端列数；合计宽度被这些列瓜分
        ed_cols = self._spring_h_columns(self.editor_area)
        term_cols = 1
        try:
            tsp = self.tab_splitters.get(self.tab_widget.currentIndex()) \
                if hasattr(self, 'tab_splitters') else None
            term_cols = self._spring_h_columns(tsp)
        except Exception:
            term_cols = 1
        n_cols = max(1, ed_cols + term_cols)
        w = combined / n_cols   # 单列宽度
        old_gate = getattr(self, '_spring_width_gate', True)
        if old_gate:
            new_gate = w <= self.SPRING_PANE_DISABLE   # 单列宽于 DISABLE 才关闭
        else:
            new_gate = w < self.SPRING_PANE_ENABLE     # 单列窄于 ENABLE 才重新允许
        if new_gate == old_gate:
            return
        self._spring_width_gate = new_gate
        # 仅当用户开了 spring 才需要联动布局
        if not getattr(self, '_spring_mode_enabled', False):
            return
        if new_gate:
            # 重新生效：按当前焦点展开一侧（_apply_spring 内部会再查 applicable）
            target = (self._spring_target_for_widget(QApplication.focusWidget())
                      or self._spring_current_side or 'editor')
            self._apply_spring(target)
        else:
            # 失效：恢复记忆中的均衡尺寸（此时 _spring_applicable 已因门控为 False，
            # 故这里直接校验结构条件后自行恢复）
            self._spring_current_side = None
            if (hasattr(self, 'editor_area') and self.editor_area.isVisible()
                    and self.main_splitter.indexOf(self.editor_area) >= 0
                    and self.main_splitter.indexOf(self._main_content_stack) >= 0):
                self._animate_main_sizes(self._resolve_main_splitter_sizes_with_editor())

    def _spring_applicable(self) -> bool:
        """仅当编辑器与终端在 main_splitter 中左右并排、且都可见时弹簧才生效。

        编辑器是否在 main_splitter 由「左右分屏」决定，本地 Explorer 与 Remote
        共用同一个 editor_area / 同一条放置路径，故这里只看 splitter 归属即可，
        Explorer / Remote 两种来源都自动适用。
        """
        if not getattr(self, '_spring_mode_enabled', False):
            return False
        if not hasattr(self, 'editor_area') or not self.editor_area.isVisible():
            return False
        if self.main_splitter.indexOf(self.editor_area) < 0:
            return False
        if self.main_splitter.indexOf(self._main_content_stack) < 0:
            return False
        # 窗口太宽时 spring 自动失效（两边都能舒服铺开）。门控值在 resize 时按滞回更新，
        # 这里只读，避免每次查询都重算/抖动。
        if not getattr(self, '_spring_width_gate', True):
            return False
        return True

    def _spring_target_for_widget(self, w):
        """判断焦点落在哪一区：返回 'editor' / 'terminal' / None。"""
        if w is None or not hasattr(self, 'editor_area'):
            return None
        if self.editor_area is w or self.editor_area.isAncestorOf(w):
            return 'editor'
        stack = getattr(self, '_main_content_stack', None)
        if stack is not None and (stack is w or stack.isAncestorOf(w)):
            return 'terminal'
        return None

    def _on_focus_changed_for_spring(self, old, new):
        """焦点在编辑器与终端之间切换时，自动展宽被点击的一侧。"""
        # 先按当前合计宽度刷新门控：窗口本来就很宽 / 没经历过 resize 时，门控可能仍停在
        # 旧值，这里确保「点编辑器」前门控是新鲜的（够宽则此处即触发失效+恢复均衡）。
        self._update_spring_width_gate()
        if not self._spring_applicable():
            return
        target = self._spring_target_for_widget(new)
        if target is None or target == self._spring_current_side:
            return
        self._apply_spring(target)

    def _apply_spring(self, target, animate=True):
        """把 main_splitter 中编辑器/终端的合计宽度按弹簧比例分配给指定一侧。"""
        if not self._spring_applicable():
            return
        sizes = self.main_splitter.sizes()
        ed_idx = self.main_splitter.indexOf(self.editor_area)
        term_idx = self.main_splitter.indexOf(self._main_content_stack)
        if ed_idx < 0 or term_idx < 0 or max(ed_idx, term_idx) >= len(sizes):
            return
        combined = sizes[ed_idx] + sizes[term_idx]
        if combined <= 0:
            return
        inactive = min(max(self.SPRING_INACTIVE_MIN, int(combined * self.SPRING_INACTIVE_RATIO)),
                       combined - self.SPRING_INACTIVE_MIN)
        if inactive < 1:
            inactive = combined // 3  # 合计太窄时退化为均分式分配
        active = combined - inactive

        new_sizes = list(sizes)
        if target == 'editor':
            new_sizes[ed_idx], new_sizes[term_idx] = active, inactive
        else:
            new_sizes[ed_idx], new_sizes[term_idx] = inactive, active

        self._spring_current_side = target
        if animate:
            self._animate_main_sizes(new_sizes)
        else:
            self._applying_spring = True
            try:
                self.main_splitter.setSizes(new_sizes)
            finally:
                self._applying_spring = False

    def _set_terminals_fast_resize(self, on: bool):
        """弹簧动画期间让 main_splitter 里的终端走「缩放旧缓存」而非每帧整屏重建，
        消除连续 resize 的卡顿；动画结束再恢复并重建为清晰文本。"""
        for term in self.main_splitter.findChildren(TerminalWidget):
            if hasattr(term, 'set_fast_resize'):
                term.set_fast_resize(on)

    def _on_splitter_drag_tick(self):
        """splitterMoved 的拖拽流识别：首拍开启终端快速渲染，之后每拍续命静默定时器。

        手动拖分隔条没有「开始/结束」信号，只能由连续的 splitterMoved 推断：
        静默 160ms 视为松手。弹簧动画期间的 setSizes 也会发 splitterMoved，
        但动画自己管理 fast_resize（_applying_spring 置位），跳过。
        被其它窗口同步宽度时（_applying_shared_left_width）同样是连续 setSizes 流,
        正需要快速渲染，故不跳过。
        """
        if getattr(self, '_applying_spring', False):
            return
        if not self._splitter_drag_active:
            self._splitter_drag_active = True
            self._set_terminals_fast_resize(True)
        self._splitter_drag_settle.start()

    def _end_splitter_drag_fast_resize(self):
        """拖拽流静默：恢复终端清晰渲染（按最终尺寸整屏重建一次）。"""
        if not self._splitter_drag_active:
            return
        self._splitter_drag_active = False
        # 拖拽触发 spring 门控翻转时弹簧动画可能正在进行并已接管 fast_resize，
        # 让动画的 finished 回调去恢复，这里不抢着关。
        if getattr(self, '_spring_anim', None) is None:
            self._set_terminals_fast_resize(False)

    def _flush_left_width_broadcast(self):
        """节流定时器到点：把最新的左侧栏宽度推给其它窗口。"""
        width = self._left_width_broadcast_pending
        self._left_width_broadcast_pending = None
        if isinstance(width, int) and width > 0:
            self._broadcast_left_panel_width(width)

    def _animate_main_sizes(self, target_sizes, duration=170,
                            easing=QEasingCurve.Type.OutCubic):
        """平滑过渡 main_splitter 到目标尺寸（弹簧手感），期间不记忆尺寸。

        duration/easing 可定制：关闭弹簧模式的「回弹均分」用 OutBack 加一点过冲,
        日常的展开切换保持默认 OutCubic。
        """
        start = self.main_splitter.sizes()
        if len(start) != len(target_sizes):
            self.main_splitter.setSizes(target_sizes)
            return
        # 停止上一段未完成的动画
        if self._spring_anim is not None:
            self._spring_anim.stop()
            self._spring_anim = None

        # 动画期间终端只缩放旧缓存，避免每帧重建整屏导致卡顿
        self._set_terminals_fast_resize(True)

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(easing)

        def _on_val(t):
            cur = [int(round(s + (e - s) * t)) for s, e in zip(start, target_sizes)]
            self._applying_spring = True
            try:
                self.main_splitter.setSizes(cur)
            finally:
                self._applying_spring = False

        def _on_done():
            self._applying_spring = True
            try:
                self.main_splitter.setSizes(target_sizes)
            finally:
                self._applying_spring = False
            self._spring_anim = None
            # 恢复终端正常渲染并按最终尺寸重建一次（清晰文本）
            self._set_terminals_fast_resize(False)

        anim.valueChanged.connect(_on_val)
        anim.finished.connect(_on_done)
        self._spring_anim = anim
        anim.start()

    # ---------- Remote 面板的分屏（与 Explorer 行为一致） ----------

    def _resolve_remote_splitter_sizes_with_editor(self):
        """计算编辑器在 remote_splitter 中时的目标尺寸（优先使用记忆值）"""
        saved = getattr(self, '_saved_remote_internal_sizes', None)
        if saved and len(saved) == 2 and saved[0] > 0 and saved[1] > 0:
            return list(saved)
        return [200, 400]

    def _place_editor_in_remote_splitter(self):
        """将编辑器放到 remote_splitter 中（Remote 上下分屏模式）"""
        if self.remote_splitter.indexOf(self.editor_area) >= 0:
            self.editor_area.show()
        else:
            self.editor_area.setParent(None)
            self.editor_area.show()
            self.remote_splitter.addWidget(self.editor_area)

        self.remote_splitter.setSizes(self._resolve_remote_splitter_sizes_with_editor())
        # 恢复 main_splitter 正常比例（编辑器不在 main_splitter 里）
        self._update_splitter_sizes()

    def _capture_remote_layout(self):
        """记录 remote_splitter 的内部尺寸（上下分屏），供下次还原。"""
        if not hasattr(self, 'editor_area') or not hasattr(self, 'remote_splitter'):
            return
        if self.remote_splitter.indexOf(self.editor_area) >= 0 and self.editor_area.isVisible():
            isizes = self.remote_splitter.sizes()
            if len(isizes) == 2 and isizes[0] > 0 and isizes[1] > 0:
                self._saved_remote_internal_sizes = list(isizes)

    def _on_remote_split_orientation_changed(self, state):
        """切换 Remote 树与编辑器的分屏方向"""
        horizontal = (state == Qt.CheckState.Checked.value)
        self._remote_split_horizontal = horizontal

        # 仅当编辑器正显示且 Remote 面板可见时，立即切换位置
        if (hasattr(self, 'editor_area') and self.editor_area.isVisible()
                and getattr(self, 'remote_panel_visible', False)):
            if horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_remote_splitter()

    def _home_editor_hidden(self):
        """把编辑器归位到 explorer_splitter 并隐藏（编辑器的默认家）。

        编辑器可能停在 main_splitter（左右）或 remote_splitter（Remote 上下），
        切换/隐藏面板时统一收回到 explorer_splitter，避免被遗留在隐藏容器里。
        """
        if not hasattr(self, 'editor_area'):
            return
        self.editor_area.hide()
        if self.explorer_splitter.indexOf(self.editor_area) < 0:
            self.editor_area.setParent(None)
            self.explorer_splitter.addWidget(self.editor_area)
            self.editor_area.hide()
            self.explorer_splitter.setSizes([400, 0])

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
            self._hide_git_diff()  # 切到 Explorer 时若在看 diff，回到终端
            # 隐藏 Remote 面板
            if getattr(self, 'remote_panel_visible', False):
                self.remote_panel_visible = False
                if hasattr(self, 'remote_toggle_btn'):
                    self.remote_toggle_btn.setChecked(False)
                self.remote_panel_container.hide()

            self.explorer_panel_container.show()
            self.left_panel_container.show()
            # 设置根目录为当前工作目录（路径未变时内部会跳过重扫描）
            self.explorer_panel.set_root_path(self._window_cwd)

            # 恢复文件编辑器（如果之前有打开的文件）
            if hasattr(self, 'editor_area') and self._editor_has_any_file():
                if self._explorer_split_horizontal:
                    self._place_editor_in_main_splitter()
                else:
                    self._place_editor_in_explorer_splitter()
            else:
                self._update_splitter_sizes()
        else:
            # 有打开文件时编辑器不消失：关面板只影响左侧栏，不清掉用户正在看的
            # 文件（减少认知负担）。必须在 hide 容器前判断可见性——编辑器若内嵌在
            # explorer_splitter 里，容器一藏 isVisible() 就恒为 False 了。
            keep_editor = (hasattr(self, 'editor_area')
                           and self.editor_area.isVisible()
                           and self._editor_has_any_file())
            self.explorer_panel_container.hide()

            if keep_editor:
                # 内嵌在 explorer_splitter 里的迁到 main_splitter 继续显示
                if self.main_splitter.indexOf(self.editor_area) < 0:
                    self._place_editor_in_main_splitter()
            elif hasattr(self, 'editor_area'):
                # 没有打开文件：隐藏并归位到默认家
                self._home_editor_hidden()

            # 如果其他面板也隐藏，则隐藏整个左侧容器
            if not self.git_panel_visible and not getattr(self, 'remote_panel_visible', False):
                self.left_panel_container.hide()

            self._update_splitter_sizes()

        # 内嵌导航条只与文件面板同时出现：随每次开关同步
        self._sync_embedded_nav()
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

        # 持久化提交区高度 + body 各栏尺寸：拖拽时记下，加载配置时恢复
        self.git_panel.commit_height_changed.connect(self._on_git_commit_height_changed)
        self.git_panel.body_sizes_changed.connect(self._on_git_body_sizes_changed)
        if isinstance(self._saved_git_body_sizes, list) and self._saved_git_body_sizes:
            self.git_panel.apply_body_sizes(self._saved_git_body_sizes)
        elif isinstance(self._saved_git_commit_height, int) and self._saved_git_commit_height > 0:
            self.git_panel.apply_commit_height(self._saved_git_commit_height)

        # 双击文件查看 diff → 在右侧大空间显示左右并排对比（不挤在左面板里）
        self.git_panel.diff_requested.connect(self._show_git_diff)
        # pull 完成 → 在右侧大空间显示 git 输出（进度 / fast-forward / 文件统计）
        self.git_panel.output_requested.connect(self._show_git_output)

    def _on_git_commit_height_changed(self, height: int):
        """记住用户拖拽出的 Git 提交区高度（关闭时随配置一起落盘）。"""
        if isinstance(height, int) and height > 0:
            self._saved_git_commit_height = height

    def _on_git_body_sizes_changed(self, sizes: list):
        """记住用户拖拽出的 Git body 各栏高度（关闭时随配置一起落盘）。"""
        if isinstance(sizes, list) and sizes and all(isinstance(s, int) and s >= 0 for s in sizes):
            self._saved_git_body_sizes = list(sizes)

    def _show_git_diff(self, title: str, diff_content: str,
                       file_path: str = "", staged: bool = False):
        """在主内容区（右侧大空间）显示左右并排 diff，暂时盖住终端。

        file_path/staged 来自 GitPanel.diff_requested，连同 GitManager 一起
        交给 GitDiffView，使其支持 hunk 级暂存/取消暂存；为空则纯展示。
        """
        if file_path:
            self.git_diff_view.set_context(self.git_panel.git_manager, file_path, staged)
        else:
            self.git_diff_view.set_context(None, None, False)
        self.git_diff_view.set_diff(title, diff_content)
        self._main_content_stack.setCurrentWidget(self.git_diff_view)

    def _show_git_output(self, title: str, output: str):
        """在主内容区显示 pull 的 git 输出（进度 / fast-forward / 文件统计）。"""
        self.git_output_view.set_output(title, output)
        self._main_content_stack.setCurrentWidget(self.git_output_view)

    def _hide_git_diff(self):
        """关闭 diff / 输出视图，回到终端。"""
        self._main_content_stack.setCurrentWidget(self.tab_widget)

    def _toggle_git_panel(self):
        """切换 Git 面板显示"""
        self.git_panel_visible = not self.git_panel_visible
        self.git_toggle_btn.setChecked(self.git_panel_visible)

        self.main_splitter.setUpdatesEnabled(False)

        if self.git_panel_visible:
            # 有打开的文件时不收起编辑器——切左侧面板不该牵连中间编辑区，
            # 大面积布局跳变会打断视觉注意力（与 Remote 面板的恢复逻辑对齐）：
            # - 编辑器已在 main_splitter（左右分屏）→ 原地不动
            # - 停在 explorer/remote_splitter（上下分屏，宿主面板即将隐藏）→
            #   迁到 main_splitter 继续以左右分屏显示，文件不消失
            # 必须在 hide 任何容器之前判断可见性：编辑器若内嵌在 explorer/
            # remote 面板里，容器一藏 isVisible() 就恒为 False 了。
            keep_editor = (hasattr(self, 'editor_area')
                           and self.editor_area.isVisible()
                           and self._editor_has_any_file())

            # 隐藏 Explorer 面板
            self.explorer_panel_visible = False
            self.explorer_toggle_btn.setChecked(False)
            self.explorer_panel_container.hide()

            if keep_editor:
                if self.main_splitter.indexOf(self.editor_area) < 0:
                    self._place_editor_in_main_splitter()
            else:
                # 没有打开的文件：照旧收回默认家并隐藏
                self._home_editor_hidden()

            self.git_panel_container.show()
            self.left_panel_container.show()
            # 设置仓库路径
            self.git_panel.set_repository(self._window_cwd)
            # 同时隐藏 Remote 面板
            if getattr(self, 'remote_panel_visible', False):
                self.remote_panel_visible = False
                if hasattr(self, 'remote_toggle_btn'):
                    self.remote_toggle_btn.setChecked(False)
                self.remote_panel_container.hide()
        else:
            self.git_panel_container.hide()
            # 关 Git 面板时若正显示 diff，回到终端，别把终端盖住
            self._hide_git_diff()
            # 如果其他面板也隐藏，则隐藏整个左侧容器
            if not self.explorer_panel_visible and not getattr(self, 'remote_panel_visible', False):
                self.left_panel_container.hide()

        self._sync_embedded_nav()
        self._update_splitter_sizes()
        self.main_splitter.setUpdatesEnabled(True)
        QTimer.singleShot(0, self._flush_terminal_resizes)

    def _setup_remote_panel(self):
        """设置 Remote Explorer 面板（SSH/SFTP）"""
        from PyQt6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton, QCheckBox, QSplitter
        )

        layout = QVBoxLayout(self.remote_panel_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        self._remote_header = QFrame()
        self._remote_header.setStyleSheet("""
            QFrame { background-color: #16213e; border-bottom: 1px solid #3d3d5c; }
        """)
        rh_layout = QHBoxLayout(self._remote_header)
        rh_layout.setContentsMargins(10, 5, 10, 5)

        self._remote_title = QLabel(t("remote.title"))
        self._remote_title.setStyleSheet("color: #38bdf8; font-weight: bold;")
        rh_layout.addWidget(self._remote_title)
        rh_layout.addStretch()

        # 分屏方向切换 checkbox（与 Explorer 行为一致：勾选=左右分屏，不勾=上下分屏）
        self._remote_split_checkbox = QCheckBox(t("explorer.left_right_split"))
        self._remote_split_checkbox.setToolTip(t("explorer.split_tooltip"))
        self._remote_split_checkbox.setStyleSheet("""
            QCheckBox { color: #888; font-size: 11px; spacing: 4px; }
            QCheckBox:hover { color: #eaeaea; }
            QCheckBox::indicator { width: 14px; height: 14px; }
            QCheckBox::indicator:unchecked {
                border: 1px solid #3d3d5c; border-radius: 2px; background-color: #16213e;
            }
            QCheckBox::indicator:checked {
                border: 1px solid #667eea; border-radius: 2px; background-color: #667eea;
            }
        """)
        if not hasattr(self, '_remote_split_horizontal'):
            self._remote_split_horizontal = False  # 默认上下分屏
        self._remote_split_checkbox.stateChanged.connect(self._on_remote_split_orientation_changed)
        rh_layout.addWidget(self._remote_split_checkbox)

        # 弹簧模式 checkbox（与 Explorer 共用同一个全局开关，两处自动同步）
        self._remote_spring_checkbox = QCheckBox(t("explorer.spring_mode"))
        self._remote_spring_checkbox.setToolTip(t("explorer.spring_tooltip"))
        self._remote_spring_checkbox.setStyleSheet(self._remote_split_checkbox.styleSheet())
        self._remote_spring_checkbox.setChecked(bool(getattr(self, '_spring_mode_enabled', False)))
        self._remote_spring_checkbox.stateChanged.connect(self._on_spring_mode_toggled)
        rh_layout.addWidget(self._remote_spring_checkbox)

        hide_btn = QPushButton("×")
        hide_btn.setFixedSize(24, 24)
        hide_btn.setStyleSheet("""
            QPushButton { background-color: transparent; color: #888; border: none; font-size: 16px; }
            QPushButton:hover { color: #eaeaea; }
        """)
        hide_btn.clicked.connect(self._toggle_remote_panel)
        rh_layout.addWidget(hide_btn)

        layout.addWidget(self._remote_header)

        current_theme = self.THEMES.get(self.current_theme, self.THEMES["深蓝"])
        self.remote_panel = RemoteExplorerPanel(theme=current_theme)
        # 远程文件打开 → 注入到本地编辑器（透明处理远程保存）
        self.remote_panel.file_open_requested.connect(self._open_remote_file_in_editor)
        # 连接成功后自动开一个 SSH 终端 tab
        self.remote_panel.host_connected.connect(
            lambda host: self._open_ssh_terminal_tab(host, None)
        )
        # 右键 "在此处打开终端" → 同样开一个 SSH tab，且 cd 进指定目录
        self.remote_panel.open_terminal_at.connect(self._open_ssh_terminal_tab)
        # 右键 "在新窗口中连接" → 新开一个独立窗口并 SSH 进该主机
        self.remote_panel.open_in_new_window.connect(self._open_ssh_in_new_window)

        # 用竖直 QSplitter 包住远程树，以便上下分屏时把编辑器放到树下方
        # （编辑器 editor_area 为共享单例，按需在 remote_splitter / main_splitter 间移动）
        self.remote_splitter = QSplitter(Qt.Orientation.Vertical)
        self.remote_splitter.setHandleWidth(3)
        self.remote_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #3d3d5c; }
            QSplitter::handle:hover { background-color: #667eea; }
        """)
        self.remote_splitter.addWidget(self.remote_panel)
        self.remote_splitter.setSizes([400, 0])
        self.remote_splitter.splitterMoved.connect(lambda *_: self._capture_remote_layout())
        layout.addWidget(self.remote_splitter)

    def _toggle_remote_panel(self):
        """切换 Remote Explorer 面板显示（与 Explorer / Git 互斥）"""
        self.remote_panel_visible = not getattr(self, 'remote_panel_visible', False)
        if hasattr(self, 'remote_toggle_btn'):
            self.remote_toggle_btn.setChecked(self.remote_panel_visible)

        self.main_splitter.setUpdatesEnabled(False)

        if self.remote_panel_visible:
            # 隐藏 Explorer / Git
            if self.explorer_panel_visible:
                self.explorer_panel_visible = False
                self.explorer_toggle_btn.setChecked(False)
                self.explorer_panel_container.hide()
            if self.git_panel_visible:
                self.git_panel_visible = False
                self.git_toggle_btn.setChecked(False)
                self.git_panel_container.hide()
                self._hide_git_diff()  # 切到 Remote 时若在看 diff，回到终端

            # 先把编辑器收回默认家（避免它停在 explorer/main_splitter 里）
            self._home_editor_hidden()

            self.remote_panel_container.show()
            self.left_panel_container.show()

            # 若之前有打开的文件，按 Side-by-Side 偏好恢复编辑器位置（与 Explorer 一致）
            if hasattr(self, 'editor_area') and self._editor_has_any_file():
                if self._remote_split_horizontal:
                    self._place_editor_in_main_splitter()
                else:
                    self._place_editor_in_remote_splitter()
        else:
            # 有打开文件（含远程文件）时编辑器不消失——在 hide 容器前判断,
            # 内嵌在 remote_splitter 里的迁到 main_splitter 继续显示
            keep_editor = (hasattr(self, 'editor_area')
                           and self.editor_area.isVisible()
                           and self._editor_has_any_file())
            self.remote_panel_container.hide()
            if keep_editor:
                if self.main_splitter.indexOf(self.editor_area) < 0:
                    self._place_editor_in_main_splitter()
            else:
                # 没有打开文件：收回默认家，避免遗留在隐藏的 remote_splitter 内
                self._home_editor_hidden()
            if not self.explorer_panel_visible and not self.git_panel_visible:
                self.left_panel_container.hide()

        self._sync_embedded_nav()
        self._update_splitter_sizes()
        self.main_splitter.setUpdatesEnabled(True)
        QTimer.singleShot(0, self._flush_terminal_resizes)

    def _open_ssh_terminal_tab(self, host_config, remote_cd_path):
        """新开一个 tab 跑 ssh 到远端

        Args:
            host_config: ssh_session.HostConfig（用别名/host/user/port/key/proxyjump）
            remote_cd_path: 可选，连接后在远程 cd 到该目录；为 None 则去 $HOME
        """
        # 构造 ssh 命令
        # 优先用别名（ssh CLI 会自动应用 ~/.ssh/config）；
        # 别名形如 user@host:port 这种手工加的，就拆开来拼参数。
        alias = host_config.alias
        is_config_alias = "@" not in alias and ":" not in alias
        ssh_args = ["ssh"]
        if is_config_alias:
            ssh_args.append(alias)
        else:
            if host_config.identity_file and os.path.isfile(host_config.identity_file):
                ssh_args.extend(["-i", host_config.identity_file])
            if host_config.port and host_config.port != 22:
                ssh_args.extend(["-p", str(host_config.port)])
            target = f"{host_config.user}@{host_config.hostname}" if host_config.user else host_config.hostname
            ssh_args.append(target)
        # 交互式 shell + 可选 cd
        if remote_cd_path:
            # -t 强制分配 tty；cd 后 exec 登录 shell 进入交互。
            # 整条 arg 之后会被 shlex.quote 用单引号包起来传给本地 ssh，
            # 本地 shell 不会展开 $SHELL，故这里不能写 \$SHELL（会让远端收到
            # 字面量 \$SHELL → exec: $SHELL: not found）。${SHELL:-/bin/bash}
            # 兜底远端未设置 $SHELL 的情况。
            ssh_args.extend(["-t", f"cd {self._shell_quote(remote_cd_path)} && exec ${{SHELL:-/bin/bash}} -l"])

        # 标签名标记 SSH host
        tab_name = t("remote.terminal_tab_name", host=alias)
        # 若当前 tab 只有一个、且 shell 还没真正启动的空白终端，直接复用它，
        # 避免每次远程连接都新建一个 tab 造成浪费；否则新开一个 tab。
        cur_idx = self.tab_widget.currentIndex()
        cur_terms = self.tab_terminals.get(cur_idx, [])
        if cur_idx >= 0 and len(cur_terms) == 1 and not cur_terms[0].has_started():
            idx = cur_idx
            _page = self.tab_widget.widget(idx)
            if not getattr(_page, '_custom_tab_name', None):
                self.tab_widget.setTabText(idx, tab_name)
        else:
            idx = self._add_new_tab(tab_name=tab_name)
        # 获取这个 tab 的第一个终端，启动 ssh
        terms = self.tab_terminals.get(idx, [])
        if not terms:
            return
        term = terms[0]
        self.tab_widget.setCurrentIndex(idx)
        self.active_terminal = term
        term.setFocus()
        # 复用当前 tab 时 currentChanged 不会触发，显式同步窗口标题 + 导航面板，
        # 确保 Navigator 立即显示这台新连上的远程主机。
        self._update_window_title_from_tab(idx)
        # 若用户刚在 Remote 面板里为该主机输入过密码，预置一次性自动回填，
        # 这样终端里的 ssh 密码提示就不用再输一遍。
        try:
            cached_pw = self.remote_panel.get_cached_password(alias)
            if cached_pw:
                term.arm_password_autofill(cached_pw)
        except Exception:
            pass
        cmd_string = " ".join(self._shell_quote(a) for a in ssh_args)
        # 用 _start_and_execute：先起 shell，再回车跑 ssh；ssh 退出后用户回到本地 shell
        try:
            term._start_and_execute([cmd_string])
        except Exception as e:
            self.statusbar.showMessage(f"Failed to start SSH: {e}", 5000)

    def _open_ssh_in_new_window(self, host_config):
        """新开一个独立窗口，并在其中完整连接该主机（Remote 右键「在新窗口中连接」）。

        走和普通 Connect 完全一样的链路：调用新窗口 Remote 面板的 _connect_to()，
        面板连上后会自动通过 host_connected 在新窗口里开 SSH 终端 tab。这样新窗口的
        Remote Explorer（SFTP 文件浏览）和终端都是连着的，而不是只有终端。
        """
        alias = getattr(host_config, 'alias', '') or 'SSH'
        MainWindow._window_counter += 1
        window_title = (f"{t('remote.terminal_tab_name', host=alias)} "
                        f"- Smart Terminal #{MainWindow._window_counter}")
        # 不传 initial_tab_data → 新窗口自建一个空白（未启动）tab，待连上后复用它跑 ssh
        new_window = MainWindow(window_title=window_title)

        # 自动配色，方便和其它窗口区分
        try:
            new_window._set_window_color(self._get_available_window_color())
        except Exception:
            pass

        # 相对当前窗口稍作偏移，避免完全盖住
        new_window.move(self.x() + 48, self.y() + 48)
        new_window.show()
        new_window.raise_()
        new_window.activateWindow()
        self.detached_windows.append(new_window)

        # 等窗口初始化 / 首次显示稳定后再连接
        def _connect_after_init():
            if sip.isdeleted(new_window):
                return
            try:
                # 显示新窗口的 Remote 面板，让用户看到连上的文件树
                if not getattr(new_window, 'remote_panel_visible', False):
                    new_window._toggle_remote_panel()
                # 完整连接：连上 Remote Explorer，并经 host_connected 自动开 SSH 终端
                new_window.remote_panel._connect_to(host_config)
            except Exception as e:
                new_window.statusbar.showMessage(f"Failed to connect: {e}", 5000)
        QTimer.singleShot(120, _connect_after_init)

    @staticmethod
    def _shell_quote(s: str) -> str:
        """简单的 POSIX shell 引用（仅在 ssh 命令拼接时用）"""
        if s and all(c.isalnum() or c in "@/_.-+:=,%" for c in s):
            return s
        return "'" + s.replace("'", "'\\''") + "'"

    def _open_remote_file_in_editor(self, host_alias: str, remote_path: str,
                                      local_temp_path: str, session):
        """远程 Explorer 双击文件后由本方法打开编辑器，并把保存事件转换成上传"""
        if not hasattr(self, 'editor_area'):
            return
        ok = self.editor_area.open_file_in_active(local_temp_path)
        if not ok:
            return
        # 在打开该远程文件的那个窗格上挂保存->上传逻辑（精确到窗格，避免活动窗格切换后错挂）
        pane = self.editor_area.active_pane
        # 把编辑器的「已保存」信号转成上传调用（only this file）
        # 用一个一次性的连接，文件切换时自动清理
        if not hasattr(self, '_remote_save_connections'):
            self._remote_save_connections = {}
        # 断开旧的连接（如果有）
        old = self._remote_save_connections.pop(local_temp_path, None)
        if old:
            try:
                pane.file_saved.disconnect(old)
            except Exception:
                pass
        def on_saved(saved_path: str):
            if saved_path != local_temp_path:
                return
            # 把本地临时文件 push 回远端
            self.remote_panel.upload_after_save(local_temp_path)
        pane.file_saved.connect(on_saved)
        self._remote_save_connections[local_temp_path] = on_saved

        # 让编辑器标题显示远程身份（在 file_label 后追加）
        try:
            current = pane.file_label.text()
            pane.file_label.setText(
                f"{current}  ·  {t('remote.editing_remote', host=host_alias, path=remote_path)}"
            )
        except Exception:
            pass

        # 按 Side-by-Side 开关放置编辑器，行为与本地 Explorer 一致：
        # 勾选=左右分屏（编辑器进 main_splitter，紧邻 Remote 树）；
        # 不勾=上下分屏（编辑器进 remote_splitter，落在 Remote 树下方，
        # 中间有可拖拽的分隔条调整两者高度）。
        # 编辑器若已显示在正确的 splitter 里，打开新文件不再重新放置，
        # 避免扰动其它编辑窗格 / 分屏的尺寸。
        target = self.main_splitter if self._remote_split_horizontal else self.remote_splitter
        if not self._editor_placed_and_visible(target):
            if self._remote_split_horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_remote_splitter()

        # 弹簧模式下打开远程文件：自动把编辑器展宽（与本地 Explorer 一致）
        if self._spring_applicable():
            self._apply_spring('editor')

    def _update_splitter_sizes(self):
        """更新分割器大小"""
        left_visible = (
            self.explorer_panel_visible
            or self.git_panel_visible
            or getattr(self, 'remote_panel_visible', False)
        )
        saved_left = getattr(self, '_saved_left_panel_width', None)
        saved_left = saved_left if isinstance(saved_left, int) and saved_left > 0 else None
        if left_visible:
            left_width = saved_left if saved_left is not None else 300
        else:
            left_width = 0
        log_width = 300 if self.log_panel_visible else 0

        # 检查编辑器是否在 main_splitter 中（左右分屏模式，splitter 有 4 个 widget）
        editor_in_main = hasattr(self, 'editor_area') and self.main_splitter.indexOf(self.editor_area) >= 0
        if editor_in_main:
            self.main_splitter.setSizes(self._resolve_main_splitter_sizes_with_editor())
        elif left_width > 0 or log_width > 0:
            # 用 splitter 实际宽度作为总和，让 left_width 被原样保留（参见 _resolve... 的注释）
            total = max(self.main_splitter.width(), 1000)
            terminal_width = max(100, total - left_width - log_width)
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
        import sys as _sys
        # macOS: 优先使用 open -a 启动，确保应用获得完整文件系统权限
        if _sys.platform == "darwin":
            app_paths = [
                "/Applications/Visual Studio Code.app",
                os.path.expanduser("~/Applications/Visual Studio Code.app"),
            ]
            for app_path in app_paths:
                if os.path.isdir(app_path):
                    try:
                        subprocess.Popen(["open", "-a", app_path, self._window_cwd])
                        self.statusbar.showMessage(t("status.opened_in_vscode", cwd=self._window_cwd), 3000)
                        return
                    except Exception as e:
                        QMessageBox.warning(self, t("msg.open_failed"), t("msg.vscode_open_error", error=str(e)))
                        return

        # Windows / Linux: 使用 CLI 命令
        if MainWindow._vscode_path_cache is None:
            code_path = shutil.which("code")
            if not code_path:
                possible_paths = [
                    "/usr/local/bin/code",
                    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
                    os.path.expanduser("~/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code"),
                ]
                for path in possible_paths:
                    if os.path.isfile(path):
                        code_path = path
                        break
            MainWindow._vscode_path_cache = code_path or ""

        code_path = MainWindow._vscode_path_cache
        if not code_path:
            QMessageBox.warning(
                self,
                t("msg.vscode_not_found"),
                t("msg.vscode_not_found")
            )
            return

        try:
            subprocess.Popen([code_path, self._window_cwd])
            self.statusbar.showMessage(t("status.opened_in_vscode", cwd=self._window_cwd), 3000)
        except Exception as e:
            QMessageBox.warning(self, t("msg.open_failed"), t("msg.vscode_open_error", error=str(e)))

    def _open_in_cursor(self):
        """在 Cursor 中打开当前工作目录"""
        import sys as _sys
        # macOS: 优先使用 open -a 启动，确保应用获得完整文件系统权限
        if _sys.platform == "darwin":
            app_paths = [
                "/Applications/Cursor.app",
                os.path.expanduser("~/Applications/Cursor.app"),
            ]
            for app_path in app_paths:
                if os.path.isdir(app_path):
                    try:
                        subprocess.Popen(["open", "-a", app_path, self._window_cwd])
                        self.statusbar.showMessage(t("status.opened_in_cursor", cwd=self._window_cwd), 3000)
                        return
                    except Exception as e:
                        QMessageBox.warning(self, t("msg.open_failed"), t("msg.cursor_open_error", error=str(e)))
                        return

        # Windows / Linux: 使用 CLI 命令
        if MainWindow._cursor_path_cache is None:
            cursor_path = shutil.which("cursor")
            if not cursor_path:
                possible_paths = [
                    "/usr/local/bin/cursor",
                    "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
                    os.path.expanduser("~/Applications/Cursor.app/Contents/Resources/app/bin/cursor"),
                ]
                for path in possible_paths:
                    if os.path.isfile(path):
                        cursor_path = path
                        break
            MainWindow._cursor_path_cache = cursor_path or ""

        cursor_path = MainWindow._cursor_path_cache
        if not cursor_path:
            QMessageBox.warning(
                self,
                t("msg.cursor_not_found"),
                t("msg.cursor_not_found")
            )
            return

        try:
            subprocess.Popen([cursor_path, self._window_cwd])
            self.statusbar.showMessage(t("status.opened_in_cursor", cwd=self._window_cwd), 3000)
        except Exception as e:
            QMessageBox.warning(self, t("msg.open_failed"), t("msg.cursor_open_error", error=str(e)))

    def _show_settings_popup_menu(self):
        """⚙ 按钮弹出菜单：工具栏布局 / 键盘快捷键。"""
        menu = QMenu(self)
        toolbar_act = menu.addAction(t("shortcuts.toolbar_menu_item"))
        toolbar_act.triggered.connect(self._show_toolbar_manager)
        shortcuts_act = menu.addAction(t("shortcuts.menu_item"))
        shortcuts_act.triggered.connect(self._show_shortcut_settings)
        cheatsheet_act = menu.addAction(t("shortcuts.cheatsheet_menu_item"))
        cheatsheet_act.triggered.connect(self._show_shortcut_cheatsheet)
        btn = self.toolbar_settings_btn
        menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))

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
            # 标记本窗口改过 LLM 配置 → _save_config 才会用本窗口的内存值落盘，
            # 否则默认从磁盘取最新值，避免覆盖其它窗口的改动。
            self._llm_configs_modified = True
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
        if hasattr(self, 'images_btn'):
            self.images_btn.setText(t("toolbar.images"))
            self.images_btn.setToolTip(t("toolbar.images_tooltip"))
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
            self.explorer_toggle_btn.setToolTip(t("toolbar.explorer_tooltip") + "  (⌘1)")
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
            self.gui_font_spin.setItemText(0, t("toolbar.gui_font_auto"))
            self.gui_font_spin.setToolTip(t("toolbar.gui_font_tooltip"))

        # 语言
        if hasattr(self, 'lang_label'):
            self.lang_label.setText(t("toolbar.lang_label"))

        # 窗口透明度
        if hasattr(self, 'opacity_label'):
            self.opacity_label.setText(t("toolbar.opacity_label"))
        if hasattr(self, 'opacity_spin'):
            self.opacity_spin.setToolTip(t("toolbar.opacity_tooltip"))

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
        if hasattr(self, '_spring_checkbox'):
            self._spring_checkbox.setText(t("explorer.spring_mode"))
            self._spring_checkbox.setToolTip(t("explorer.spring_tooltip"))
        if hasattr(self, '_remote_split_checkbox'):
            self._remote_split_checkbox.setText(t("explorer.left_right_split"))
            self._remote_split_checkbox.setToolTip(t("explorer.split_tooltip"))
        if hasattr(self, '_remote_spring_checkbox'):
            self._remote_spring_checkbox.setText(t("explorer.spring_mode"))
            self._remote_spring_checkbox.setToolTip(t("explorer.spring_tooltip"))

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
        if hasattr(self, 'remote_panel'):
            if hasattr(self.remote_panel, 'apply_language'):
                self.remote_panel.apply_language()
            try:
                self._remote_title.setText(t("remote.title"))
            except Exception:
                pass
        if hasattr(self, 'editor_area'):
            self.editor_area.apply_language()

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
                padding: 5px;
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
                color: #ffffff;
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

        # 主题切换后重新应用 GUI 字体缩放（样式表已被主题覆盖；缓存已在方法开头
        # 还原+清空，此时所有控件都处于未缩放基准，缩放不会复合放大）。
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
        self._dir_history_pending_removals = set()  # 用户显式删除、待从共享配置剔除的路径
        self.last_working_dir = None  # 上次使用的工作目录
        self.toolbar_config = None  # 工具栏配置
        self.llm_configs = []  # LLM API 配置列表
        self.default_llm_config = 0  # 默认 LLM 配置索引
        self._ai_completion_enabled = False  # AI 行内补全开关（默认关闭）
        self._saved_window_geometry = None  # 窗口位置和大小 [x, y, w, h]
        self._saved_window_maximized = False  # 窗口是否最大化
        self._saved_explorer_panel_visible = False  # Explorer 面板可见性
        self._saved_git_panel_visible = False  # Git 面板可见性
        self._saved_log_panel_visible = False  # 日志面板可见性
        self._saved_navigator_enabled = False  # Window Navigator 开关状态
        # 记忆资源管理器/编辑器拖拽过的尺寸，避免每次重新打开都重置
        self._saved_explorer_main_sizes = None  # main_splitter 4 项尺寸（左右分屏）
        self._saved_explorer_internal_sizes = None  # explorer_splitter 2 项尺寸（上下分屏）
        # 弹簧模式：编辑器与终端左右并排时，点哪边哪边自动变宽，另一边收窄但不收起
        self._spring_mode_enabled = False
        self._spring_current_side = None   # 'editor' / 'terminal'，当前已展开的一侧
        self._spring_width_gate = True     # 窗口宽度是否允许 spring 生效（resize 时按滞回更新）
        self._applying_spring = False      # setSizes 期间置位，避免污染记忆尺寸
        self._spring_anim = None           # 进行中的尺寸过渡动画（持引用防 GC）
        self._saved_remote_internal_sizes = None  # remote_splitter 2 项尺寸（上下分屏）
        # 左侧栏宽度是进程级共享的（见 _shared_left_panel_width）：新窗口初始化时
        # 不要把已打开窗口设过的宽度清成 None，仅在还没有任何窗口设过时才置默认。
        if MainWindow._shared_left_panel_width is None:
            self._saved_left_panel_width = None  # 仅左面板可见时的宽度（无编辑器场景）
        self._saved_git_commit_height = None  # Git 面板提交区高度（拖拽记忆，兼容旧版）
        self._saved_git_body_sizes = None     # Git 面板 body splitter 各栏尺寸（拖拽记忆）
        self._saved_nav_list_height = None    # 内嵌导航列表高度（拖拽记忆）
        self._custom_shortcuts = {}           # 用户自定义快捷键覆盖 {action_id: seq}
        self.used_label_names = []            # 用过的 标签/分屏 名称历史（可复用）
        try:
            if self.CONFIG_FILE.exists():
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.presets = config.get('presets', [])
                    self.last_preset_index = config.get('last_preset_index', 0)
                    self.image_prefix_enabled = config.get('image_prefix_enabled', False)
                    self.image_save_local = config.get('image_save_local', True)
                    self.used_label_names = config.get('used_label_names', [])
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
                    # 加载窗口透明度
                    self._window_opacity = config.get('window_opacity', 100)
                    # 加载左右分屏偏好（Explorer / Remote 各自记忆）
                    self._explorer_split_horizontal = config.get('explorer_split_horizontal', False)
                    self._remote_split_horizontal = config.get('remote_split_horizontal', False)
                    # 加载弹簧模式偏好
                    self._spring_mode_enabled = config.get('spring_mode_enabled', False)
                    # 加载 AI 行内补全开关
                    self._ai_completion_enabled = config.get('ai_completion_enabled', False)
                    # 加载导航面板停靠方式（'float' / 'embed'，全局记忆）
                    _dock_mode = config.get('navigator_dock_mode', 'float')
                    if _dock_mode in ('float', 'embed'):
                        MainWindow._navigator_dock_mode = _dock_mode
                    # 加载用户自定义快捷键覆盖
                    ks = config.get('keyboard_shortcuts', {})
                    if isinstance(ks, dict):
                        self._custom_shortcuts = {
                            str(k): str(v) for k, v in ks.items() if isinstance(v, str)
                        }
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
                    self._saved_navigator_enabled = config.get('navigator_enabled', False)
                    # 加载记忆的资源管理器/编辑器尺寸
                    main_sizes = config.get('explorer_main_splitter_sizes', None)
                    if isinstance(main_sizes, list) and len(main_sizes) == 4 and all(isinstance(s, int) and s >= 0 for s in main_sizes):
                        self._saved_explorer_main_sizes = main_sizes
                    internal_sizes = config.get('explorer_internal_splitter_sizes', None)
                    if isinstance(internal_sizes, list) and len(internal_sizes) == 2 and all(isinstance(s, int) and s >= 0 for s in internal_sizes):
                        self._saved_explorer_internal_sizes = internal_sizes
                    remote_internal = config.get('remote_internal_splitter_sizes', None)
                    if isinstance(remote_internal, list) and len(remote_internal) == 2 and all(isinstance(s, int) and s >= 0 for s in remote_internal):
                        self._saved_remote_internal_sizes = remote_internal
                    # 左侧栏宽度是进程级共享的：只让第一个窗口从磁盘播种，之后开的
                    # 窗口沿用已有的实时共享值，避免用磁盘上的旧值覆盖别的窗口刚拖出的新宽度。
                    left_width = config.get('left_panel_width', None)
                    if (isinstance(left_width, int) and left_width > 0
                            and MainWindow._shared_left_panel_width is None):
                        self._saved_left_panel_width = left_width
                    git_commit_h = config.get('git_commit_height', None)
                    if isinstance(git_commit_h, int) and git_commit_h > 0:
                        self._saved_git_commit_height = git_commit_h
                    git_body_sizes = config.get('git_body_splitter_sizes', None)
                    if isinstance(git_body_sizes, list) and git_body_sizes and all(isinstance(s, int) and s >= 0 for s in git_body_sizes):
                        self._saved_git_body_sizes = git_body_sizes
                    nav_list_h = config.get('nav_list_height', None)
                    if isinstance(nav_list_h, int) and nav_list_h > 0:
                        self._saved_nav_list_height = nav_list_h
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

    def _merge_dir_history_for_save(self):
        """写配置前，把磁盘上的目录历史并入本窗口，避免多窗口互相覆盖。

        背景：所有窗口共享同一份配置文件，而 _save_config 会把整份配置（含
        working_dir_history）原样写回。若直接用本窗口内存里的历史覆盖磁盘，
        其它窗口新增的路径就会被冲掉 —— 表现为"只有最后退出的窗口的路径被
        保存"。这里在写入前先与磁盘做并集，确保任意窗口新增的路径都不丢。

        删除处理：用户在"管理路径"对话框里显式删除的路径会先记到
        self._dir_history_pending_removals，并集时主动剔除，否则会被磁盘版本复活。
        """
        try:
            saved_history, saved_freq = [], {}
            if self.CONFIG_FILE.exists():
                try:
                    with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                        cfg = json.load(f)
                    saved_history = cfg.get('working_dir_history', []) or []
                    saved_freq = cfg.get('working_dir_freq', {}) or {}
                except Exception:
                    saved_history, saved_freq = [], {}

            removals = getattr(self, '_dir_history_pending_removals', None) or set()
            mem_history = self.working_dir_history if hasattr(self, 'working_dir_history') else []
            mem_freq = self._working_dir_freq if hasattr(self, '_working_dir_freq') else {}

            merged_freq = {}
            merged_history = []
            seen = set()
            # 并集：磁盘在前（保留其它窗口已落盘的路径），再补本窗口内存中的新路径
            for p in list(saved_history) + list(mem_history):
                if p in removals:
                    continue
                if p not in seen:
                    seen.add(p)
                    merged_history.append(p)
                # 频率取磁盘与内存中的较大值，避免跨窗口计数被重置
                merged_freq[p] = max(saved_freq.get(p, 0), mem_freq.get(p, 0), 1)

            merged_history.sort(key=lambda p: merged_freq.get(p, 0), reverse=True)
            self.working_dir_history = merged_history
            self._working_dir_freq = merged_freq
            # 删除意图已在本次写盘中落实，清空待删除集合
            self._dir_history_pending_removals = set()
        except Exception:
            # 合并失败不应阻断保存：保持内存中的历史原样写出
            pass

    def _save_config(self):
        """保存配置"""
        try:
            # 先把磁盘上其它窗口新增的目录历史并入本窗口，避免后写覆盖先写
            self._merge_dir_history_for_save()
            # 一次性读取磁盘上的现有配置：既用于"未修改预设时回退到磁盘版本"，
            # 又用于把本函数没列出的字段（如 git_widget 写入的 git_proxy /
            # git_proxies）原样保留下来。两段逻辑共用同一次读取，避免重复 I/O
            # 也减少与其它进程的写入交错窗口。
            existing_config, read_ok = read_config_json(self.CONFIG_FILE)
            # 文件存在但解析失败：很可能是另一个 Smart Terminal 进程正在写到
            # 一半（多窗口共用同一份配置文件）。此时若强行用内存里"没有
            # git_proxy"的配置覆盖，对方刚保存的代理 / 预设就会被清空。放弃
            # 本次保存让对方的写入留下来，等下次稳定状态再保存即可。
            if not read_ok:
                return

            # 获取当前选中的预设索引
            current_index = self.preset_combo.currentIndex() if hasattr(self, 'preset_combo') else 0
            image_prefix = self.image_prefix_checkbox.isChecked() if hasattr(self, 'image_prefix_checkbox') else False
            image_local = self.image_local_checkbox.isChecked() if hasattr(self, 'image_local_checkbox') else True
            # 限制历史记录数量
            dir_history = self.working_dir_history if hasattr(self, 'working_dir_history') else []
            # 使用窗口级别的工作目录
            last_cwd = self._window_cwd if hasattr(self, '_window_cwd') else os.getcwd()

            # 防止多窗口覆盖：如果本窗口没有修改预设，从磁盘加载最新的预设
            # 这样关闭窗口时不会覆盖其他窗口保存的预设
            if getattr(self, '_presets_modified', False):
                presets_to_save = self.presets
            else:
                presets_to_save = existing_config.get('presets', self.presets)

            # 同理保护 LLM API 配置：本窗口没改过就用磁盘上的最新值，避免一个持有
            # 旧副本的窗口（不是最后关闭的那个）在退出时把别的窗口新存的 API 配置覆盖掉。
            if getattr(self, '_llm_configs_modified', False):
                llm_configs_to_save = self.llm_configs
                default_llm_to_save = self.default_llm_config
            else:
                llm_configs_to_save = existing_config.get('llm_configs', self.llm_configs)
                default_llm_to_save = existing_config.get('default_llm_config', self.default_llm_config)

            # 同理保护自定义快捷键：本窗口没改过就用磁盘最新值，避免覆盖其它窗口的改动
            if getattr(self, '_shortcuts_modified', False):
                shortcuts_to_save = getattr(self, '_custom_shortcuts', {})
            else:
                shortcuts_to_save = existing_config.get('keyboard_shortcuts',
                                                        getattr(self, '_custom_shortcuts', {}))

            config = {
                'presets': presets_to_save,
                'last_preset_index': current_index,
                'image_prefix_enabled': image_prefix,
                'image_save_local': image_local,
                'working_dir_history': dir_history,
                'used_label_names': self._merged_label_names(existing_config),
                'working_dir_freq': self._working_dir_freq if hasattr(self, '_working_dir_freq') else {},
                'last_working_dir': last_cwd,
                'theme': self.current_theme,  # 保存主题设置
                'icon_tint': self._use_icon_tint,  # 保存图标蒙版设置
                'toolbar_config': self.toolbar_config,  # 保存工具栏配置
                'llm_configs': llm_configs_to_save,  # 保存 LLM 配置（带多窗口防覆盖）
                'default_llm_config': default_llm_to_save,  # 保存默认 LLM 配置索引
                'global_zoom_delta': self._global_zoom_delta,  # 保存全局缩放偏移
                'gui_font_size': self._gui_font_size,  # 保存 GUI 字体大小
                'pin_toolbar_row2': self._pin_toolbar_row2,  # 保存固定第二排工具栏
                'window_opacity': self._window_opacity,  # 保存窗口透明度
                'explorer_split_horizontal': getattr(self, '_explorer_split_horizontal', False),  # 保存左右分屏偏好
                'remote_split_horizontal': getattr(self, '_remote_split_horizontal', False),  # Remote 左右分屏偏好
                'spring_mode_enabled': getattr(self, '_spring_mode_enabled', False),  # 保存弹簧模式偏好
                'ai_completion_enabled': getattr(self, '_ai_completion_enabled', False),  # 保存 AI 行内补全开关
                'language': get_language(),  # 保存语言设置
                'keyboard_shortcuts': shortcuts_to_save,  # 保存自定义快捷键（带多窗口防覆盖）
                'window_geometry': [self.x(), self.y(), self.width(), self.height()],
                'window_maximized': self.isMaximized(),
                'explorer_panel_visible': getattr(self, 'explorer_panel_visible', False),
                'git_panel_visible': getattr(self, 'git_panel_visible', False),
                'log_panel_visible': getattr(self, 'log_panel_visible', False),
                'navigator_enabled': self._navigator_is_enabled(),  # 记忆 Window Navigator 开关状态

                'explorer_main_splitter_sizes': getattr(self, '_saved_explorer_main_sizes', None),
                'explorer_internal_splitter_sizes': getattr(self, '_saved_explorer_internal_sizes', None),
                'remote_internal_splitter_sizes': getattr(self, '_saved_remote_internal_sizes', None),
                'left_panel_width': getattr(self, '_saved_left_panel_width', None),
                'git_commit_height': getattr(self, '_saved_git_commit_height', None),
                'git_body_splitter_sizes': getattr(self, '_saved_git_body_sizes', None),
                'nav_list_height': getattr(self, '_saved_nav_list_height', None),
            }
            # 保存窗口导航面板设置
            nav = MainWindow._global_window_navigator
            if nav is not None and not sip.isdeleted(nav):
                config['navigator_geometry'] = [nav.x(), nav.y(), nav.width(), nav.height()]
                config['navigator_font_size'] = nav._font_size
            else:
                # 导航面板已关闭，保留之前已落盘的几何 / 字号
                if 'navigator_geometry' in existing_config:
                    config['navigator_geometry'] = existing_config['navigator_geometry']
                if 'navigator_font_size' in existing_config:
                    config['navigator_font_size'] = existing_config['navigator_font_size']
            # 合并写入：保留由其它组件维护、本函数未列出的字段（如 git_widget
            # 写入的 git_proxy / git_proxies）。直接整体覆盖会把这些键清空，
            # 导致退出后再次打开时丢失代理等设置。
            merged = dict(existing_config)
            merged.update(config)
            # 原子写：先写临时文件再 rename，避免多进程并发写入时另一个进程
            # 读到半截 JSON。直接 `open(...'w')` 会先 truncate，期间另一个进程
            # 解析失败 → 按"已损坏"处理 → 反过来覆盖掉本次刚写的内容，造成
            # git_proxy 等字段莫名清零。
            atomic_write_json(self.CONFIG_FILE, merged)
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

    # 兼容旧约定：名字叫 completion / 补全 等的配置也当作补全配置
    _COMPLETION_CONFIG_NAMES = {'completion', '补全', 'autocomplete', 'complete', 'copilot'}

    def get_completion_llm_config(self) -> dict:
        """AI 行内补全用的 LLM 配置。

        优先级：① 在 ✨ 里用「设为补全模型」显式指派(for_completion) →
        ② 兼容旧约定：名字叫 completion/补全 等 → ③ 回退默认配置。
        """
        if self.llm_configs:
            for config in self.llm_configs:
                if config.get('for_completion'):
                    return config.copy()
            for config in self.llm_configs:
                if (config.get('name') or '').strip().lower() in self._COMPLETION_CONFIG_NAMES:
                    return config.copy()
        return self.get_llm_config()

    def get_git_llm_config(self) -> dict:
        """Git 提交信息生成用的 LLM 配置：
        优先用「设为 Git 模型」显式指派(for_git)，否则回退默认配置。"""
        if self.llm_configs:
            for config in self.llm_configs:
                if config.get('for_git'):
                    return config.copy()
        return self.get_llm_config()

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
        """窗口快速导航选项变化（区分浮动 / 内嵌两种停靠方式）"""
        checked = (state == Qt.CheckState.Checked.value)
        if MainWindow._navigator_dock_mode == 'embed':
            # 内嵌模式：勾选 = 启用左侧栏顶部的导航条；它只与 Explorer/Git/Remote 一起出现，
            # 不会单独占满左侧栏。该开关在所有窗口间联动（与浮动模式一致）。
            MainWindow._set_embed_enabled_all(checked)
            self._save_config()  # 记忆开关状态，下次启动恢复
            return
        # 浮动模式（原行为）
        if checked:
            if MainWindow._global_window_navigator is None:
                MainWindow._global_window_navigator = WindowNavigatorPanel()
                MainWindow._global_window_navigator.panel_closed.connect(MainWindow._on_navigator_closed_global)
            MainWindow._global_window_navigator.show()
            MainWindow._global_window_navigator.raise_()
            MainWindow._sync_nav_checkbox_state(True)
        else:
            if MainWindow._global_window_navigator is not None:
                MainWindow._global_window_navigator.hide()
            MainWindow._sync_nav_checkbox_state(False)
        self._save_config()  # 记忆开关状态，下次启动恢复

    def _on_nav_resize_drag(self, delta):
        """拖拽导航面板底部手柄：按鼠标位移调整列表高度（上限留出下方面板空间）。"""
        if not hasattr(self, 'nav_panel'):
            return
        cur = self.nav_panel.embedded_list_height()
        # 上限：不超过左侧栏高度减去给下方文件面板预留的空间
        max_h = max(120, self.left_panel_container.height() - 200)
        new_h = min(cur + delta, max_h)
        self._saved_nav_list_height = self.nav_panel.set_embedded_list_height(new_h)

    def _sync_embedded_nav(self):
        """内嵌模式下让导航条「只与 Explorer/Git/Remote 同时出现，不单独出现」。

        导航条可见 = 已启用(nav_embed_enabled) 且 左侧有文件面板(Explorer/Git/Remote)正显示。
        其余情况一律隐藏，避免它单独占满整个左侧栏。
        """
        if not hasattr(self, 'nav_panel_container'):
            return
        file_panel_open = (
            self.explorer_panel_visible
            or self.git_panel_visible
            or getattr(self, 'remote_panel_visible', False)
        )
        show = (MainWindow._navigator_dock_mode == 'embed'
                and bool(getattr(self, 'nav_embed_enabled', False))
                and file_panel_open)
        self.nav_panel_visible = show
        self.nav_panel_container.setVisible(show)
        if hasattr(self, 'nav_resize_handle'):
            self.nav_resize_handle.setVisible(show)
        if show:
            try:
                self.nav_panel._force_refresh()
            except Exception:
                pass

    @staticmethod
    def _set_embed_enabled_all(enabled: bool):
        """内嵌模式下把「启用导航条」状态联动到所有窗口（开关、可见性、checkbox 一起更新）。"""
        app = QApplication.instance()
        wins = [w for w in (app.topLevelWidgets() if app else [])
                if isinstance(w, MainWindow) and not sip.isdeleted(w)]
        for w in wins:
            w.nav_embed_enabled = enabled
            w.main_splitter.setUpdatesEnabled(False)
            w._sync_embedded_nav()
            w._update_splitter_sizes()
            w.main_splitter.setUpdatesEnabled(True)
            if hasattr(w, 'window_nav_checkbox'):
                w.window_nav_checkbox.blockSignals(True)
                w.window_nav_checkbox.setChecked(enabled)
                w.window_nav_checkbox.blockSignals(False)
            QTimer.singleShot(0, w._flush_terminal_resizes)

    @staticmethod
    def _current_embed_enabled() -> bool:
        """是否已有窗口启用了内嵌导航条（新窗口创建时据此联动初始状态）。"""
        app = QApplication.instance()
        if app:
            for w in app.topLevelWidgets():
                if isinstance(w, MainWindow) and getattr(w, 'nav_embed_enabled', False):
                    return True
        return False

    def _navigator_is_enabled(self) -> bool:
        """当前 Window Navigator 是否开启（用于落盘记忆，下次启动恢复）。
        内嵌模式看是否有窗口启用了导航条；浮动模式看全局浮动面板是否可见。"""
        if MainWindow._navigator_dock_mode == 'embed':
            return MainWindow._current_embed_enabled()
        nav = MainWindow._global_window_navigator
        return nav is not None and not sip.isdeleted(nav) and nav.isVisible()

    @staticmethod
    def _set_navigator_dock_mode(mode: str):
        """切换导航面板停靠方式（'float' / 'embed'），并把选择写入配置自动记住。"""
        if mode not in ('float', 'embed'):
            return
        MainWindow._navigator_dock_mode = mode
        app = QApplication.instance()
        wins = [w for w in (app.topLevelWidgets() if app else [])
                if isinstance(w, MainWindow) and not sip.isdeleted(w)]
        if mode == 'embed':
            g = MainWindow._global_window_navigator
            floating_on = (g is not None and not sip.isdeleted(g) and g.isVisible())
            # 关闭浮动面板
            if g is not None:
                MainWindow._global_window_navigator = None
                try:
                    if not sip.isdeleted(g):
                        g._save_navigator_config()
                        g.close()
                        g.deleteLater()
                except Exception:
                    pass
            # 启用内嵌导航条（所有窗口联动；与已打开的文件面板一起显示）
            was_on = floating_on or any(
                hasattr(w, 'window_nav_checkbox') and w.window_nav_checkbox.isChecked()
                for w in wins)
            MainWindow._set_embed_enabled_all(bool(was_on))
        else:  # float
            any_on = MainWindow._current_embed_enabled()
            MainWindow._set_embed_enabled_all(False)
            if any_on:
                if MainWindow._global_window_navigator is None:
                    MainWindow._global_window_navigator = WindowNavigatorPanel()
                    MainWindow._global_window_navigator.panel_closed.connect(MainWindow._on_navigator_closed_global)
                MainWindow._global_window_navigator.show()
                MainWindow._global_window_navigator.raise_()
                MainWindow._sync_nav_checkbox_state(True)
        # 回写所有存活导航面板的 Embed 勾选框：内嵌面板在隐藏期间不会销毁，
        # 用户在它上面取消勾选留下的状态会一直残留，重新切回内嵌时必须同步回来
        for nav in MainWindow._iter_navigators():
            cb = getattr(nav, 'embed_checkbox', None)
            if cb is None:
                continue
            try:
                cb.blockSignals(True)
                cb.setChecked(mode == 'embed')
                cb.blockSignals(False)
            except Exception:
                pass
        MainWindow._persist_navigator_dock_mode()

    @staticmethod
    def _persist_navigator_dock_mode():
        """把当前停靠方式写入主配置文件。"""
        try:
            existing, ok = read_config_json(MainWindow.CONFIG_FILE)
            if not ok:
                return
            existing['navigator_dock_mode'] = MainWindow._navigator_dock_mode
            atomic_write_json(MainWindow.CONFIG_FILE, existing)
        except Exception:
            pass

    @staticmethod
    def _persist_navigator_enabled(enabled: bool):
        """把 Window Navigator 开关状态写入主配置文件（供下次启动恢复）。"""
        try:
            existing, ok = read_config_json(MainWindow.CONFIG_FILE)
            if not ok:
                return
            existing['navigator_enabled'] = bool(enabled)
            atomic_write_json(MainWindow.CONFIG_FILE, existing)
        except Exception:
            pass

    @staticmethod
    def _iter_navigators():
        """返回当前所有存活的导航面板（浮动 + 各窗口内嵌），用于广播刷新/选中。"""
        navs = []
        g = MainWindow._global_window_navigator
        if g is not None and not sip.isdeleted(g):
            navs.append(g)
        app = QApplication.instance()
        if app:
            for w in app.topLevelWidgets():
                if isinstance(w, MainWindow):
                    nav = getattr(w, 'nav_panel', None)
                    if nav is not None and not sip.isdeleted(nav):
                        navs.append(nav)
        return navs

    @staticmethod
    def _broadcast_navigator_refresh(invalidate_cache: bool = False):
        """刷新所有导航面板的窗口列表。"""
        for nav in MainWindow._iter_navigators():
            try:
                if invalidate_cache:
                    nav._last_window_info = []
                nav._refresh_window_list()
            except Exception:
                pass

    @staticmethod
    def _on_navigator_closed_global():
        """浮动导航面板被关闭时的全局回调"""
        MainWindow._global_window_navigator = None
        # 仅浮动模式下需要同步取消勾选（内嵌模式 checkbox 由各窗口自己管理）
        if MainWindow._navigator_dock_mode != 'embed':
            MainWindow._sync_nav_checkbox_state(False)
            # 用关闭按钮关掉时 _sync 用了 blockSignals，不会触发 _save_config，
            # 这里直接落盘记忆"关闭"状态，避免下次启动又自动打开。
            MainWindow._persist_navigator_enabled(False)

    @staticmethod
    def _sync_nav_checkbox_state(checked: bool):
        """同步所有 MainWindow 实例的窗口导航 checkbox 状态（仅浮动模式使用）"""
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
            self._presets_modified = True  # 标记预设已修改
            self._populate_presets()
            self._save_config()
            self.statusbar.showMessage(t("status.preset_saved"), 3000)

    def _add_new_preset(self):
        """打开预设管理对话框并自动添加新预设"""
        dialog = PresetDialog(self.presets, self, auto_add=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.presets = dialog.get_presets()
            self._presets_modified = True  # 标记预设已修改
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
                self._broadcast_local_commands_reload()

    def _add_new_local_preset(self):
        """添加新本地预设"""
        dialog = PresetDialog(self.local_presets, self, auto_add=True, title=t("msg.manage_local_commands"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.local_presets = dialog.get_presets()
            if self._save_local_commands():
                self.statusbar.showMessage(t("status.local_preset_saved"), 3000)
                self._broadcast_local_commands_reload()

    def _broadcast_local_commands_reload(self):
        """保存本地命令后，通知其它「当前工作目录相同」的窗口重新加载，避免它们用过期
        的内存数据在之后保存时覆盖刚写入的内容（多窗口同目录并发竞争）。"""
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return
        my_cwd = getattr(self, '_window_cwd', None)
        if not my_cwd:
            return
        for w in app.topLevelWidgets():
            if w is self or not isinstance(w, MainWindow) or sip.isdeleted(w):
                continue
            if getattr(w, '_window_cwd', None) == my_cwd:
                try:
                    w._load_local_commands()
                except Exception:
                    pass

    # ==================== 本地快速命令相关方法结束 ====================

    def force_close_with_save(self):
        """强制关闭：先自动保存会话+配置，再跳过确认弹窗关闭窗口。
        由窗口导航面板的右键菜单调用。

        关键点（防 macOS 段错误）：
        1. 重复进入保护：避免导航面板快速双击触发两次 close 链。
        2. 先 hide()：让 macOS 立刻把窗口从 NSApp.windows() 中摘除，
           其他窗口/导航刷新逻辑此时再扫 topLevelWidgets 就不会碰到它。
        3. 把真正的 close() 推到下一个事件循环：避免 hide → close 与
           native NSWindow detach 同步重入。
        """
        if getattr(self, '_force_closing', False):
            return
        self._force_closing = True

        # 1) 自动保存（任何异常都不能阻断关窗）
        try:
            if getattr(self, 'session_manager', None) is not None:
                try:
                    self.session_manager.auto_save()
                except Exception as e:
                    logger.warning(f"[ForceClose] session auto_save failed: {e}")
            try:
                self._save_config()
            except Exception as e:
                logger.warning(f"[ForceClose] _save_config failed: {e}")
        except Exception as e:
            logger.warning(f"[ForceClose] save phase failed: {e}")

        # 2) 立刻隐藏窗口：让 macOS / 导航面板都不再看到它
        try:
            self.hide()
        except Exception as e:
            logger.warning(f"[ForceClose] hide failed: {e}")

        # 3) 推迟真正的 close() 到下一拍事件循环
        def _do_close():
            try:
                if not sip.isdeleted(self):
                    self.close()
            except RuntimeError:
                pass
            except Exception as e:
                logger.warning(f"[ForceClose] close failed: {e}")
        QTimer.singleShot(0, _do_close)

    def closeEvent(self, event):
        """窗口关闭事件"""
        # 防止重入：closeEvent 在 macOS 上可能因 native 事件链被多次触发，
        # 第二次触发时已经清理过的资源会再次被访问 → 段错误
        if getattr(self, '_closing_in_progress', False):
            event.accept()
            return

        # 强制关闭路径：跳过确认弹窗（保存已在 force_close_with_save 中完成）
        force_closing = getattr(self, '_force_closing', False)

        # 检查是否有任何终端在运行
        try:
            any_running = any(
                term.is_running()
                for terminals in self.tab_terminals.values()
                for term in terminals
            )
        except Exception:
            any_running = False

        if any_running and not force_closing:
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

        # 标记进入清理阶段，防止重入
        self._closing_in_progress = True

        # 强制关闭路径下窗口已 hide()，此处再保险确认一次
        if force_closing:
            try:
                self.hide()
            except Exception:
                pass

        # 停止所有 OpenAI API 服务器（独立 try：服务器停不掉不该阻塞关窗）
        try:
            self.openai_server_manager.stop_all()
        except Exception as e:
            logger.warning(f"[Close] stop openai servers failed: {e}")

        # 停止定时器
        try:
            self.auto_save_timer.stop()
        except Exception:
            pass
        try:
            self._log_timer.stop()
        except Exception:
            pass

        # 等待 Git 面板的后台线程（fetch/push/pull/生成提交信息）跑完，
        # 否则线程仍在运行时被销毁会触发 QThread abort
        try:
            if hasattr(self, 'git_panel') and self.git_panel is not None:
                self.git_panel.shutdown()
        except Exception as e:
            logger.warning(f"[Close] git panel shutdown failed: {e}")

        # 完整清理所有终端资源
        # 注意：terminal.cleanup() 内部会 join 后端 reader thread (最长 2s)，
        # 这是阻塞 GUI 线程的同步操作。逐个 try 包住，避免一个终端清理失败
        # 让整个 closeEvent 抛 Python 异常 → Qt 在 C++ 侧不预期 → 段错误
        try:
            for terminals in self.tab_terminals.values():
                for terminal in terminals:
                    try:
                        terminal.cleanup()
                    except Exception as e:
                        logger.warning(f"[Close] terminal cleanup failed: {e}")
        except Exception as e:
            logger.warning(f"[Close] terminal cleanup loop failed: {e}")

        # 保存配置（包括最后选中的预设）
        try:
            self._save_config()
        except Exception as e:
            logger.warning(f"[Close] save_config failed: {e}")

        # 立即刷新窗口导航（窗口关闭时）—— 广播到浮动与所有内嵌面板
        # 延迟到本窗口真正从 topLevelWidgets 移除后再刷新，使列表不再含本窗口
        def refresh_navigator():
            try:
                MainWindow._broadcast_navigator_refresh(invalidate_cache=True)
            except RuntimeError:
                pass
            except Exception as e:
                logger.warning(f"[Close] navigator refresh failed: {e}")
        QTimer.singleShot(200, refresh_navigator)

        event.accept()

        # 兜底退出判定：正常点 X 关闭最后一个窗口时 Qt 会自动退出（lastWindowClosed
        # → quitOnLastWindowClosed）。但强制关闭走的是 force_close_with_save 里的
        # hide() → close()，Qt 对"已隐藏后再关闭"的窗口不会发 lastWindowClosed
        # （该信号只在关闭一个可见窗口时触发），于是当这是最后一个 MainWindow 时
        # 应用不会退出，只剩置顶的导航面板(Tool 窗口)挂在后台 → 进程不结束。
        # 这里在已无其它存活主窗口时显式退出。
        self._quit_if_last_main_window()

    def _quit_if_last_main_window(self):
        """若已无其它存活的 MainWindow，则关闭导航面板并显式退出应用。

        参见 closeEvent 末尾的说明：force_close 的 hide()→close() 不会触发 Qt 的
        自动退出，需要这里兜底，否则关掉最后一个窗口后进程仍在运行。
        """
        try:
            app = QApplication.instance()
            if app is None:
                return
            for w in app.topLevelWidgets():
                if w is self or not isinstance(w, MainWindow):
                    continue
                if sip.isdeleted(w):
                    continue
                # 正在关闭/强制关闭中的窗口视为即将消失，不计入存活窗口
                if getattr(w, '_closing_in_progress', False):
                    continue
                if getattr(w, '_force_closing', False):
                    continue
                # 仍有存活的主窗口，无需退出
                return
            # 已无其它主窗口：先保存并关闭导航面板，再退出事件循环
            nav = MainWindow._global_window_navigator
            if nav is not None and not sip.isdeleted(nav):
                try:
                    nav._save_navigator_config()
                    nav.close()
                except Exception:
                    pass
            # 推迟一拍退出，避免在 closeEvent / native 事件链内同步退出
            QTimer.singleShot(0, app.quit)
        except Exception as e:
            # closeEvent 内异常绝不能逃逸到 Qt C++ 侧（否则可能 abort）
            logger.warning(f"[Close] quit-if-last check failed: {e}")

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

        # 扩展为新窗口（等同拖出标签，但不需要手动拖拽）—— 放最上面，最常用
        detach_action = menu.addAction(t("tab.detach"))
        detach_action.setEnabled(self.tab_widget.count() > 1)
        detach_action.triggered.connect(
            lambda: self._detach_tab(tab_index, None, follow_drag=False))

        menu.addSeparator()

        # 切换工作目录到该 tab 终端的当前路径
        switch_path_action = menu.addAction(t("tab.switch_to_path"))
        switch_path_action.triggered.connect(lambda: self._switch_dir_to_tab_path(tab_index))

        # OpenAI API 服务器选项
        is_server_running = self.openai_server_manager.is_running(tab_index)

        if is_server_running:
            port = self.openai_server_manager.get_port(tab_index)
            stop_action = menu.addAction(t("openai.stop_server", port=port))
            stop_action.triggered.connect(lambda: self._stop_openai_server(tab_index))

            # 复制 API URL
            copy_url_action = menu.addAction(t("openai.copy_url"))
            copy_url_action.triggered.connect(lambda: self._copy_api_url(port))

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

        # 重命名标签页（可复用历史名称）
        rename_action = menu.addAction(t("tab.rename"))
        rename_action.triggered.connect(lambda: self._rename_tab(tab_index))

        menu.addSeparator()

        # 关闭标签页
        close_action = menu.addAction(t("tab.close"))
        close_action.triggered.connect(lambda: self._close_tab(tab_index))

        menu.exec(tab_bar.mapToGlobal(pos))

    # ==================== 标签 / 分屏自定义命名 ====================

    def _prompt_label_name(self, title, prompt, current=""):
        """弹出名称输入框，下拉列表为历史用过的名称（便于快速复用）。

        返回 (name, ok)。name 可能为空串（表示清除/恢复默认）。
        """
        cur = (current or "").strip()
        # 历史名称去重，把当前名置顶
        items = []
        if cur:
            items.append(cur)
        for n in getattr(self, 'used_label_names', []):
            if n and n not in items:
                items.append(n)
        if items:
            # 可编辑下拉框：既能从历史里选，也能直接输入新名
            name, ok = QInputDialog.getItem(self, title, prompt, items, 0, True)
        else:
            name, ok = QInputDialog.getText(self, title, prompt, text=cur)
        return (name, ok)

    def _remember_label_name(self, name):
        """把刚用过的名称记入历史（最近优先，去重，上限 30）"""
        name = (name or "").strip()
        if not name:
            return
        lst = getattr(self, 'used_label_names', None)
        if lst is None:
            lst = self.used_label_names = []
        if name in lst:
            lst.remove(name)
        lst.insert(0, name)
        del lst[30:]

    def _merged_label_names(self, existing_config):
        """合并磁盘与内存里的历史名称（多窗口共用配置时不互相覆盖）"""
        disk = existing_config.get('used_label_names', []) if existing_config else []
        out = []
        for n in list(getattr(self, 'used_label_names', [])) + list(disk):
            if n and n not in out:
                out.append(n)
        return out[:30]

    def _apply_tab_name(self, index, name):
        """统一应用标签名：非空则「锁定」为自定义名，留空则解除锁定恢复默认编号。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        name = (name or "").strip()
        page = self.tab_widget.widget(index)
        if name:
            if page is not None:
                page._custom_tab_name = name  # 锁定标记：存在该属性即视为用户自定义
            self.tab_widget.setTabText(index, name)
            self._remember_label_name(name)
        else:
            # 清除自定义名 → 解除锁定，恢复默认编号命名
            if page is not None:
                page._custom_tab_name = None
            self.tab_widget.setTabText(index, t("terminal.default_name", n=index + 1))
        self._update_window_title_from_tab(index)
        self._save_config()

    def _switch_dir_to_tab_path(self, index):
        """把工作目录切换到该 tab 终端进程的当前路径（右键菜单入口）。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        terminals = self.tab_terminals.get(index, [])
        # 优先用该 tab 内当前激活的终端，否则退回第一个
        terminal = None
        if self.active_terminal and self.active_terminal in terminals:
            terminal = self.active_terminal
        elif terminals:
            terminal = terminals[0]
        cwd = terminal.get_cwd() if terminal else None
        if not cwd or not os.path.isdir(cwd):
            self.statusbar.showMessage(t("tab.switch_to_path_unavailable"), 3000)
            return
        # 复用现有切换逻辑：填入输入框后应用
        self.working_dir_combo.setCurrentText(cwd)
        self._apply_working_dir()

    def _rename_tab(self, index):
        """通过对话框重命名标签页（右键菜单入口，可从历史复用名称）。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        page = self.tab_widget.widget(index)
        current = getattr(page, '_custom_tab_name', None) or self.tab_widget.tabText(index)
        name, ok = self._prompt_label_name(t("tab.rename_title"), t("tab.rename_prompt"), current)
        if not ok:
            return
        self._apply_tab_name(index, name)

    def _begin_inline_tab_rename(self, index):
        """双击标签 → 在标签上就地弹出输入框直接编辑。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        # 已有正在编辑的输入框先收掉，避免重叠
        self._discard_inline_tab_rename()
        tab_bar = self.tab_widget.tabBar()
        page = self.tab_widget.widget(index)
        current = getattr(page, '_custom_tab_name', None) or tab_bar.tabText(index)

        editor = InlineRenameEdit(tab_bar)
        editor.setText(current)
        editor.selectAll()
        rect = tab_bar.tabRect(index)
        # 右侧留出关闭按钮的空间
        editor.setGeometry(rect.adjusted(4, 3, -26, -3))
        editor.setStyleSheet(
            "QLineEdit{background:#282c34;color:#ffffff;border:1px solid #667eea;"
            "border-radius:3px;padding:0px 4px;font-weight:bold;}"
        )
        editor.committed.connect(lambda text, i=index: self._finish_inline_tab_rename(i, text))
        editor.cancelled.connect(self._discard_inline_tab_rename)
        self._tab_rename_editor = editor
        editor.show()
        editor.raise_()
        editor.setFocus()

    def _finish_inline_tab_rename(self, index, text):
        """就地编辑提交"""
        ed = getattr(self, '_tab_rename_editor', None)
        self._tab_rename_editor = None
        if ed is not None:
            ed.deleteLater()
        self._apply_tab_name(index, text)

    def _discard_inline_tab_rename(self):
        """取消就地编辑（Esc 或被新的编辑取代）"""
        ed = getattr(self, '_tab_rename_editor', None)
        self._tab_rename_editor = None
        if ed is not None:
            ed.deleteLater()

    def _rename_split(self, terminal):
        """重命名某个分屏（窗格）。名称非空时在窗格顶部显示标题栏，留空则清除。"""
        if terminal is None:
            return
        current = terminal.get_split_label() or ""
        name, ok = self._prompt_label_name(t("split.rename_title"), t("split.rename_prompt"), current)
        if not ok:
            return
        name = (name or "").strip()
        terminal.set_split_label(name)
        if name:
            self._remember_label_name(name)
        self._save_config()

    # ==================== 导航提醒小标（Claude/命令执行完毕） ====================

    def _on_terminal_attention(self, terminal):
        """某个终端疑似执行完毕。若该终端不是你正在看的活动终端，就在导航条目上打绿点。"""
        # 正在前台看着的活动终端不打扰
        if self.isActiveWindow() and terminal is self.active_terminal:
            return
        self._request_nav_attention(terminal)

    def _on_terminal_interaction(self, terminal):
        """某个终端正在等待用户操作（响铃 / 确认框 / y/n 询问）。

        与"执行完毕"不同：哪怕是当前激活窗口里正在看的活动终端，也点亮导航
        绿点（用户要求：每次需要指令都提示一次，避免错过）。用户在该终端
        按键响应后由 eventFilter 清除。
        """
        self._request_nav_attention(terminal)

    def _request_nav_attention(self, source=None):
        """点亮本窗口的导航绿点（后台任务完成提醒的通用入口）。

        终端命令结束之外的来源（如 Git 面板生成提交信息完成）也可调用。
        「前台时是否不打扰」的判断由各来源自行决定后再调用——终端"执行完毕"
        要求「不是正在看的活动终端」，"等待操作"则无条件点亮，Git 生成要求
        「窗口不在前台」。source 记录点亮来源终端：用户在该终端按键即视为
        已响应并清除绿点（非终端来源传 None，只靠切窗口/切 tab 清除）。
        """
        if getattr(self, '_nav_attention', False):
            return  # 已经在提醒，避免重复刷新
        self._nav_attention = True
        self._nav_attention_source = source
        try:
            MainWindow._broadcast_navigator_refresh(invalidate_cache=True)
        except Exception:
            pass

    def _clear_nav_attention(self):
        """清除本窗口的导航提醒小标（用户已查看）"""
        self._nav_attention_source = None
        if getattr(self, '_nav_attention', False):
            self._nav_attention = False
            try:
                MainWindow._broadcast_navigator_refresh(invalidate_cache=True)
            except Exception:
                pass

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
