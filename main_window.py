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
        from AppKit import (
            NSApp,
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
    QPushButton, QLabel, QStatusBar,
    QMessageBox, QFileDialog, QSplitter,
    QTextEdit, QFrame, QDialog, QLineEdit, QListWidget,
    QListWidgetItem, QTabWidget,
    QApplication, QInputDialog, QMenu, QSizePolicy,
    QStackedWidget, QCheckBox
)
from PyQt6 import sip  # 用于检查 C++ 对象是否已被删除
from PyQt6.QtCore import (Qt, QTimer, QEvent, QPoint, QRect, QObject,
                          QVariantAnimation, QEasingCurve, QAbstractAnimation)
from PyQt6.QtGui import QAction, QIcon, QColor, QPixmap, QPainter, QKeySequence

from terminal_widget import TerminalWidget
from session_manager import SessionManager
from exporter import export_session
from history_dialog import HistoryDialog
from openai_server import OpenAIServerManager
from git_widget import GitDiffView, GitOutputView
from window_navigator import WindowNavigatorPanel  # 从 main_window 拆出的导航面板
from main_window_update import UpdateMixin  # 应用内更新流程（从 main_window 拆出）
from main_window_theme import ThemeMixin  # 主题/配色（从 main_window 拆出）
from main_window_toolbar import ToolbarMixin  # 工具栏搭建/布局（从 main_window 拆出）
from main_window_config import ConfigMixin  # 配置读写（从 main_window 拆出）
from main_window_explorer import ExplorerPanelMixin  # Explorer 侧面板（从 main_window 拆出）
from main_window_git import GitPanelMixin  # Git 侧面板（从 main_window 拆出）
from main_window_remote import RemotePanelMixin  # 远程 SSH/SFTP 侧面板（从 main_window 拆出）
from main_window_tabs import TabSplitMixin  # 标签页与分屏（从 main_window 拆出）
import themes  # 主题配色表（纯数据，从 main_window 拆出）
from i18n import t, set_language
from utils import get_config_path, list_notify_sounds, play_notify_sound
import app_config
from app_logging import get_logger

logger = get_logger(__name__)
import shutil
import subprocess
from widgets import (
    DetachableTabBar, _NavResizeHandle,
)
from dialogs import (
    get_default_shell, PresetDialog, LLMConfigDialog,
    DirectoryHistoryDialog, ShortcutSettingsDialog,
    ShortcutCheatSheetDialog,
)


class _SpringPressWatcher(QObject):
    """应用级鼠标按下探针：spring 动画进行中若再次按下鼠标，立刻冻结动画。

    只监听 MouseButtonPress 并转调宿主窗口的 _pause_spring_anims_for_press,
    绝不消费事件。没有进行中的动画时该调用是 O(1) 空操作，不影响任何交互。
    """

    def __init__(self, win):
        super().__init__(win)
        self._win = win

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            try:
                self._win._pause_spring_anims_for_press()
            except RuntimeError:
                pass  # 宿主窗口已销毁，探针随 parent 一起被回收前的空窗
        return False


class MainWindow(ThemeMixin, ToolbarMixin, ConfigMixin, ExplorerPanelMixin,
                 GitPanelMixin, RemotePanelMixin, TabSplitMixin, UpdateMixin,
                 QMainWindow):
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
    # 主题配色表（纯数据，见 themes.py）
    THEMES = themes.THEMES

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
    # 导航面板停靠方式：'embed'=嵌入每个窗口左侧栏（默认）；'float'=独立浮动窗口
    _navigator_dock_mode = 'embed'

    # 左侧栏宽度（按屏幕分桶，进程级共享）：同一块屏幕上的窗口共用同一宽度，
    # 在一个窗口里拖动调宽，同屏的其它窗口也用这个宽度，减轻窗口间切换的
    # 认知负担；分散在不同显示器上的窗口尺寸语境不同（分辨率/缩放各异），
    # 互不联动、各记各的。None 键存磁盘播种的兜底值，屏幕还没有自己的值时
    # 用它。通过 _saved_left_panel_width 属性读写（自动按本窗口所在屏分桶）。
    _left_width_by_screen = {}

    # 侧栏高度联动（侧栏底部 checkbox，全局记忆）：开启后拖动任一窗口
    # 侧栏里「导航列表 / 文件面板」之间的分隔条，同一屏幕上的其它窗口
    # 同步跟随，免去逐个窗口重复拖拽；跨屏窗口不联动。按屏幕分桶同上。
    _sidebar_height_sync = False
    _nav_height_by_screen = {}

    # QApplication 全局 stylesheet 原值快照（进程级共享，仅初始化一次）
    _original_app_stylesheet = None

    def _screen_key(self):
        """联动分桶用的屏幕标识（QScreen.name()）；拿不到屏幕时归入 None
        （与磁盘播种值同桶）。跨窗口联动只发生在同一 key 的窗口之间。"""
        try:
            s = self.screen()
            return s.name() if s is not None else None
        except Exception:
            return None

    @property
    def _saved_left_panel_width(self):
        """左侧栏记忆宽度：同屏窗口共用一份，见 _left_width_by_screen。"""
        d = MainWindow._left_width_by_screen
        return d.get(self._screen_key(), d.get(None))

    @_saved_left_panel_width.setter
    def _saved_left_panel_width(self, value):
        MainWindow._left_width_by_screen[self._screen_key()] = value

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

        # 各 mixin 的实例状态显式初始化（每个状态只在自己的 _init_*_state 里给默认值，
        # 不再靠散落各处的 hasattr/getattr 兜底）；必须早于 _load_config / _setup_ui
        self._init_window_state()
        self._init_config_state()
        self._init_explorer_state()
        self._init_remote_state()
        self._init_tabs_state()
        self._init_theme_state()
        self._init_toolbar_state()
        self._init_update_state()

        self.session_manager = SessionManager()
        self.auto_save_timer = QTimer()
        self.command_history = []
        self.presets = []  # 预设命令列表
        self.local_presets = []  # 本地快速命令列表（目录级别）
        self.pending_commands = []  # 待执行的命令队列
        self.current_theme = "午夜黑"  # 当前主题名称（初装默认：Midnight Black）
        self._use_icon_tint = False  # 是否给图标添加主题色蒙版
        self._global_zoom_delta = 0  # 全局缩放偏移量（相对于默认字体大小）
        self._gui_font_size = 0  # GUI 字体大小（0 = Auto，跟随系统默认字号）
        self._original_widget_styles = {}  # {id(widget): (weakref, original_stylesheet)}
        self._pin_toolbar_row2 = True  # 是否固定显示第二排工具栏（默认开启）
        self._window_opacity = 100  # 窗口透明度百分比（10-100）

        # 多标签页支持
        self.tab_counter = 0  # 标签页计数器
        self.tab_sessions = {}  # {tab_index: session} 映射
        self.tab_splitters = {}  # {tab_index: QSplitter} 映射
        self.tab_terminals = {}  # {tab_index: [terminal_list]} 映射
        self.tab_cwds = {}  # {tab_index: str} 每个标签页独立的工作目录
        self.active_terminal = None  # 当前活动的终端
        self.detached_windows = []  # 分离出的独立窗口列表（见 _track_detached_window）

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

        # 恢复全局缩放（无偏移也要跑一次：显式 GUI 字号需要在启动时应用；
        # Auto 且无偏移时等价于清掉 app 级 font-size，跟随系统默认字号）
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
                if sip.isdeleted(self):
                    return
                # 面板本体是懒建的：预热即在空闲时把它建出来并扫一遍目录
                self._ensure_explorer_panel().prewarm(getattr(self, '_window_cwd', None))
            QTimer.singleShot(1200, _prewarm_explorer)

        # 启动 5s 后静默检查更新（每日最多一次、设置里可关；不打扰启动性能）。
        # 必须用挂在 self 下的 QTimer 而不是裸 singleShot：窗口销毁后裸
        # singleShot 仍会触发、回调打在已删对象上段错误（CI 上曾以此崩掉
        # 整个测试进程）。测试进程里不调度——避免单测创建窗口时发起真实网络。
        if 'pytest' not in sys.modules:
            self._auto_update_timer = QTimer(self)
            self._auto_update_timer.setSingleShot(True)
            self._auto_update_timer.timeout.connect(
                self._maybe_auto_check_updates)
            self._auto_update_timer.start(5000)

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

    def moveEvent(self, event):
        """跨屏拖动检测：窗口挪到另一块显示器时，各窗口内嵌导航列表
        （只列本屏窗口）需要立即重建，不等兜底轮询。"""
        super().moveEvent(event)
        try:
            key = self._screen_key()
        except Exception:
            return
        if key != getattr(self, '_nav_screen_key_seen', '__unset__'):
            first = not hasattr(self, '_nav_screen_key_seen')
            self._nav_screen_key_seen = key
            # 首次落位不广播：启动阶段窗口逐个就位，避免无谓的批量刷新
            if not first:
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
                            def resume_terminal_timers(term=terminal):
                                if not sip.isdeleted(term):
                                    term.resume_timers()
                            QTimer.singleShot(delay, resume_terminal_timers)
                            delay += 20
                # 更新所有导航面板（浮动 + 内嵌）的选中项
                for nav in MainWindow._iter_navigators():
                    try:
                        nav.select_window(self)
                    except Exception:
                        logger.debug("changeEvent: suppressed exception", exc_info=True)
                # 跨窗口左侧栏联动：激活时窗口已铺满稳定，按共享宽度对齐一次。
                # 修正启动时因窗口尚未到最终尺寸、setSizes 被当成比例缩放而导致各
                # 窗口左侧栏宽度不一致的问题（无需用户先手动拖一次才联动）。
                # 仅在确有偏差(>2px)时应用，避免稳态下无谓跳动。
                # 共享宽度按屏幕分桶（属性读取自动取本窗口所在屏的值），
                # 挪到别的显示器上的窗口不会被这里拉回其它屏的宽度。
                try:
                    sw = self._saved_left_panel_width
                    if isinstance(sw, int) and sw > 0 and hasattr(self, 'main_splitter'):
                        sizes = self.main_splitter.sizes()
                        if sizes and sizes[0] > 0 and abs(sizes[0] - sw) > 2:
                            self._apply_shared_left_panel_width(sw)
                except Exception:
                    logger.debug("changeEvent: suppressed exception", exc_info=True)
            else:
                # 切走前把编辑器的未保存改动落盘。焦点留在窗口内部时
                # focusChanged 不会触发，切到别的应用只走这条路径。
                if self._editor_auto_save:
                    area = getattr(self, 'editor_area', None)
                    if area is not None and not sip.isdeleted(area):
                        area.auto_save_all_dirty()
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
        # macOS 下是带留白的 Dock 母版（满幅图会让 Dock 图标偏大），
        # Tint 主题上色也从这份源图出发，保持留白
        from utils import app_icon_path
        self._icon_path = app_icon_path()
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
                padding: 8px 12px;
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
        # 关掉 documentMode 标签栏的原生"基座"绘制：macOS 深色系统外观下它会
        # 无视 QSS、按 NSAppearance 横贯整行画一条深灰底（浅色主题下极其突兀），
        # 而且 widget.grab() 复现不出来（只有真实屏幕合成才可见）。
        self.detachable_tab_bar.setDrawBase(False)
        self.detachable_tab_bar.tab_detach_requested.connect(self._detach_tab)
        self.detachable_tab_bar.tab_rename_requested.connect(self._begin_inline_tab_rename)

        self.tab_widget.setTabsClosable(False)  # 禁用内置关闭按钮，使用自定义
        self.tab_widget.setMovable(True)
        # 拖动标签重排时 QTabWidget 只同步自己内部的页面顺序；tab_splitters /
        # tab_terminals / tab_cwds 这些按索引存的映射必须跟着重建，否则分屏、
        # 关闭分屏会作用到**别的标签页**的终端上（分屏「串页」）。
        self.detachable_tab_bar.tabMoved.connect(self._on_tab_moved)
        # 不用 documentMode：它让标签栏走 macOS 原生外观绘制（NSAppearance），
        # 空白区颜色无视 QSS/QPalette（conda 版 Qt 连 colorScheme 都只能改成
        # 原生浅灰而非主题色）。关掉后标签栏完全由下面的 QSS 控制；
        # 需配合 QTabWidget::tab-bar { alignment: left } 保持标签靠左。
        self.tab_widget.setDocumentMode(False)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        self.tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: none;
                background-color: #1a1a2e;
            }
            QTabWidget::tab-bar {
                alignment: left;
            }
            QTabBar {
                background-color: #0f0f1a;
                border: none;
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
        # pin_to=self：内嵌列表的高亮永远标识本窗口自己，不跟随全局活动窗口
        self.nav_panel = WindowNavigatorPanel(embedded=True, pin_to=self)
        _nav_layout.addWidget(self.nav_panel)
        self.left_panel_layout.addWidget(self.nav_panel_container)
        # 恢复导航列表高度：联动开启且本屏已有共享值时优先对齐同屏窗口，
        # 否则用本窗口上次拖拽记忆的高度
        _shared_nav_h = MainWindow._nav_height_by_screen.get(self._screen_key())
        if MainWindow._sidebar_height_sync and isinstance(_shared_nav_h, int):
            self._saved_nav_list_height = self.nav_panel.set_embedded_list_height(
                _shared_nav_h)
        elif isinstance(getattr(self, '_saved_nav_list_height', None), int):
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

        # 侧栏底部：跨窗口高度联动开关。只随嵌入式导航条（即那根可拖的
        # 分隔条）一起显示/隐藏，见 _sync_embedded_nav。
        self.sidebar_sync_row = QWidget()
        _sync_layout = QHBoxLayout(self.sidebar_sync_row)
        _sync_layout.setContentsMargins(10, 2, 8, 4)
        _sync_layout.setSpacing(0)
        self.sidebar_sync_checkbox = QCheckBox(t("sidebar.sync_heights"))
        self.sidebar_sync_checkbox.setToolTip(t("sidebar.sync_heights_tooltip"))
        self.sidebar_sync_checkbox.setChecked(MainWindow._sidebar_height_sync)
        self.sidebar_sync_checkbox.stateChanged.connect(self._on_sidebar_sync_toggled)
        _sync_layout.addWidget(self.sidebar_sync_checkbox)
        _sync_layout.addStretch()
        self.left_panel_layout.addWidget(self.sidebar_sync_row)
        self.sidebar_sync_row.hide()
        # 拖拽中的联动广播节流：与左侧栏宽度联动同理，拖动流里只挂起最新值，
        # 80ms 一批推给其它窗口，避免逐像素触发全部窗口重排
        self._nav_height_broadcast_pending = None
        self._nav_height_broadcast_timer = QTimer(self)
        self._nav_height_broadcast_timer.setSingleShot(True)
        self._nav_height_broadcast_timer.setInterval(80)
        self._nav_height_broadcast_timer.timeout.connect(self._broadcast_nav_list_height)

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
            # 同一信号驱动编辑器失焦自动保存（切到终端/其它文件/其它窗口）
            _app.focusChanged.connect(self._auto_save_editors_on_leave)
            # spring 动画期间的任何鼠标按下 → 立刻冻结动画（防止窗格在
            # 指针下移动被文本控件当成拖选），松开+静默后续播
            self._spring_press_watcher = _SpringPressWatcher(self)
            _app.installEventFilter(self._spring_press_watcher)

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
        from toolbar_manager import ToolbarManagerDialog, merge_group_order
        default_groups = ToolbarManagerDialog.DEFAULT_GROUPS

        saved_order = self.toolbar_config.get("group_order", None) if self.toolbar_config else None
        # 版本升级新增的默认分组按默认相对位置插入（而不是堆到末尾）
        return merge_group_order(saved_order, default_groups)


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

    # 下拉框最多显示多少条历史目录。完整历史不受影响（仍存盘、仍可在
    # 「History」对话框里管理）——只是限制弹窗里的条目数，避免历史越攒越多
    # 时下拉弹窗变大变慢。history 已按频率倒序，取前 N 条即最常用的那些。
    _DIR_DROPDOWN_MAX = 30

    def _populate_working_dirs(self):
        """填充工作目录历史到下拉框（只放最常用的前 N 条 + 当前目录）"""
        # 补全候选用「完整历史」——下拉只显示前 N 条，但搜索能命中全部历史目录
        if hasattr(self, '_dir_completer'):
            self._dir_completer.set_items(self.working_dir_history)
        self.working_dir_combo.clear()
        shown = list(self.working_dir_history[:self._DIR_DROPDOWN_MAX])
        # 当前目录若被截断在 N 条之外，也补进来，保证它能被选中/高亮
        current_dir = self._window_cwd
        if current_dir and current_dir not in shown and current_dir in self.working_dir_history:
            shown.append(current_dir)
        for dir_path in shown:
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
        # 用户显式选用的路径 → 解除拉黑；readded 标记让保存时把磁盘黑名单
        # 里的这条也一并清除（否则并集时会被磁盘版本重新拉黑）
        self._dir_history_removed.discard(dir_path)
        self._dir_history_readded.add(dir_path)
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

        # 立即刷新导航面板：列表项名按窗口工作目录显示（标题无文件夹名时回退到
        # _window_cwd basename），改了目录就该马上跟着变，不必等 5 秒轮询。
        try:
            MainWindow._broadcast_navigator_refresh(invalidate_cache=True)
        except Exception:
            logger.debug("_apply_working_dir: suppressed exception", exc_info=True)




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
        ("toggle_word_wrap","Alt+Z",          "shortcuts.act.toggle_word_wrap","_toggle_word_wrap"),
        ("history",         "Ctrl+Shift+H",   "shortcuts.act.history",         "_show_history"),
        ("new_session",     "Ctrl+Shift+N",   "shortcuts.act.new_session",     "_start_session"),
        ("new_tab",         "Ctrl+T",         "shortcuts.act.new_tab",         "_add_new_tab"),
        ("close_tab",       "Ctrl+W",         "shortcuts.act.close_tab",       "_close_tab_or_window"),
        ("next_tab",        "Ctrl+Tab",       "shortcuts.act.next_tab",        "_next_tab"),
        ("prev_tab",        "Ctrl+Shift+Tab", "shortcuts.act.prev_tab",        "_prev_tab"),
        # 拆分键位：Cmd+Shift++ 左右并排、Cmd+Shift+- 上下叠放（Windows 上即 Ctrl+Shift+ +/-）。
        # 放大已不再绑定 Cmd 加号（去掉了 Ctrl++ 别名，放大只剩 Cmd+=），腾出 + 给分屏。
        # +/- 都是「需 Shift 才打出/会被 Shift 改写」的键，Qt 上报 Shift 的方式不一，
        # 故 _setup_shortcuts 里给每个方向补了几种等价写法，保证稳定触发。
        ("split_h",         "Ctrl+Shift++",   "shortcuts.act.split_h",         "_split_current_tab"),
        ("split_v",         "Ctrl+Shift+-",   "shortcuts.act.split_v",         "_split_vertical_current_terminal"),
        ("close_split",     "Ctrl+Shift+X",   "shortcuts.act.close_split",     "_close_current_split"),
        ("toggle_explorer", "Ctrl+1",         "shortcuts.act.toggle_explorer", "_toggle_explorer_panel"),
        ("toggle_git",      "Ctrl+2",         "shortcuts.act.toggle_git",      "_toggle_git_panel"),
        ("toggle_remote",   "Ctrl+3",         "shortcuts.act.toggle_remote",   "_toggle_remote_panel"),
        # Ctrl+R 在 macOS 上由 Qt 自动映射为 Cmd+R，等价于资源管理器头部的 ↻ 刷新按钮
        ("refresh",         "Ctrl+R",         "shortcuts.act.refresh",         "_refresh_explorer_panel"),
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
        overrides = self._custom_shortcuts or {}
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

        # 放大只保留 Cmd+=（不再注册 Cmd 加号别名），把 Cmd 加号这个物理键让给分屏。
        # 分屏的实际触发在终端 keyPressEvent 里（terminal_widget.event() 的
        # ShortcutOverride 已把 Cmd+ =/+/-/_ 抢给终端自己处理）；这里 split_h/split_v
        # 仍保留在 _SHORTCUT_SPECS 中，仅用于速查表展示与自定义。

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
            logger.debug("_cycle_windows: suppressed exception", exc_info=True)
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
                logger.debug("_handler: suppressed exception", exc_info=True)
            return event

        try:
            cls._backtick_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _handler)
        except Exception as e:
            logger.warning(f"[backtick] 监听器安装失败: {e}")

    def _effective_shortcut(self, action_id, default_seq):
        """返回某操作当前生效的键序列（用户覆盖优先，否则默认）。"""
        overrides = self._custom_shortcuts or {}
        return overrides.get(action_id, default_seq)

    def _show_shortcut_settings(self):
        """打开「键盘快捷键」自定义对话框，保存后即时生效。"""
        current = {
            action_id: self._effective_shortcut(action_id, default_seq)
            for action_id, default_seq, _lk, _slot in self._SHORTCUT_SPECS
        }
        dialog = ShortcutSettingsDialog(self._SHORTCUT_SPECS, current, self, theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
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
            (nat("Alt+Up") + " / " + nat("Alt+Down"), t("shortcuts.sc.term_cmd_marks")),
            (nat("Ctrl+Left") + " / " + nat("Ctrl+Right"), t("shortcuts.sc.term_line_ends")),
            (nat("Ctrl+F"), t("shortcuts.sc.term_open_search")),
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
            (nat("Ctrl+Shift+M"), t("shortcuts.sc.edit_md_preview")),
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
                logger.debug("_show_shortcut_cheatsheet: suppressed exception", exc_info=True)
        dialog = ShortcutCheatSheetDialog(self._shortcut_cheatsheet_groups(), self, theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
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
        """GUI 字体大小调整（下拉框：Auto=0 或 8–32pt）— 同步到所有窗口"""
        value = self.gui_font_spin.currentData()
        if value is None:
            return
        self._gui_font_size = int(value)
        self._apply_gui_font_to_all_windows()
        self._save_config_debounced()

    def _apply_gui_font_to_all_windows(self):
        """将当前 GUI 字号应用到所有 MainWindow 窗口。

        仅在导航栏「内嵌」模式下跨窗口联动：此时各窗口共用同一套侧栏布局，
        字号也随之全局同步，并静默对齐其他窗口的下拉框，避免信号回环。
        浮动模式下字号是 per-window 设置，只应用到当前窗口。
        每个窗口各自调用 _apply_global_zoom()——该方法用 self.findChildren 只能
        缩放本窗口的控件，所以必须逐窗口分别应用，不能只在当前窗口跑一次。
        """
        app = QApplication.instance()
        if not app or MainWindow._navigator_dock_mode != 'embed':
            self._apply_global_zoom()
            return
        for widget in app.topLevelWidgets():
            if not isinstance(widget, MainWindow):
                continue
            if widget is not self:
                widget._gui_font_size = self._gui_font_size
                if hasattr(widget, 'gui_font_spin'):
                    self._select_combo_value(widget.gui_font_spin, self._gui_font_size)
            widget._apply_global_zoom()

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






    def resizeEvent(self, event):
        """窗口大小变化时更新 flow toolbar 高度"""
        super().resizeEvent(event)
        self._update_flow_toolbar_height()
        # 按窗口宽度自动让 spring 生效/失效（仅在跨阈值翻转时动一次）
        self._update_spring_width_gate()

    def _global_zoom_in(self):
        """放大内容区字体（终端/编辑器/文件树）；界面字体只由 GUI Font 控制"""
        self._global_zoom_delta += 1
        self._apply_global_zoom()
        self._save_config_debounced()

    def _global_zoom_out(self):
        """缩小内容区字体（终端/编辑器/文件树）；界面字体只由 GUI Font 控制"""
        self._global_zoom_delta -= 1
        self._apply_global_zoom()
        self._save_config_debounced()

    def _opacity_increase(self):
        """增加窗口透明度（更不透明）"""
        new_val = min(100, self._window_opacity + 5)
        self._window_opacity = new_val
        if hasattr(self, 'opacity_spin'):
            self._select_combo_value(self.opacity_spin, new_val)
        self._apply_opacity_to_all_windows()
        self._save_config_debounced()

    def _opacity_decrease(self):
        """减少窗口透明度（更透明）"""
        new_val = max(10, self._window_opacity - 5)
        self._window_opacity = new_val
        if hasattr(self, 'opacity_spin'):
            self._select_combo_value(self.opacity_spin, new_val)
        self._apply_opacity_to_all_windows()
        self._save_config_debounced()

    def _apply_global_zoom(self):
        """应用字体设置：缩放偏移（Cmd+±）只动内容区（终端/编辑器/文件树），
        界面（工具栏/标签/按钮/Git 面板）字体只由 GUI Font 下拉框控制——
        二者解耦，互不影响（用户明确要求分开）。"""
        delta = self._global_zoom_delta
        # GUI 字体大小：0 = Auto（跟随系统默认字号），>0 = 固定大小
        gui_font_size = self._gui_font_size

        # 1. 所有终端 (默认12pt, 范围8-32) — 始终跟随全局缩放。
        #    apply_font_size：字号立即生效，O(历史) 的网格 reflow 走 150ms
        #    防抖——连按 Cmd+± 时每个终端只在停手后 reflow 一次，不再每按
        #    一次就对所有分屏同步串行 reflow（满历史时曾达秒级冻结）。
        for terminals in self.tab_terminals.values():
            for term in terminals:
                target_size = max(8, min(32, 12 + delta))
                term.apply_font_size(target_size)

        # 2. 全局 GUI 字体（工具栏、标签栏、状态栏等）——只由 GUI Font 控制，
        #    与 Cmd+± 的缩放偏移无关：
        #    - 显式选了字号：缩放样式表里写死 font-size 的控件 + 下发 app 级
        #      font-size（覆盖 Start/Stop/Switch 等未显式设字号的控件）
        #    - Auto（=0）：恢复原始样式、不下发 app 级 font-size，跟随系统
        #      默认字号（mac 上强制 12px 反而比系统字号显大、挤出省略号）
        effective_px = gui_font_size if gui_font_size > 0 else None
        self._scale_gui_font_sizes(gui_font_size)
        self._apply_application_font(effective_px)

        # 3. 文件编辑器 — 与终端字号完全联动（同一字号、同一范围 8-32），不受 GUI 字号影响
        if hasattr(self, 'editor_area') and self.editor_area is not None:
            target_size = max(8, min(32, 12 + delta))
            self.editor_area.set_editor_font_size(target_size)

        # 4. 资源管理器文件树 (默认13pt, 范围8-28) — 跟随终端缩放，不受 GUI 字号影响
        self._apply_zoom_to_explorer_tree()

        # 5. Git 面板（diff 查看器 + 提交 graph，默认12pt, 范围6-32）
        self._apply_gui_font_to_git_panel()

        # 6. 导航面板（全局单例 + 各窗口内嵌）——不在 findChildren 缩放遍历内，
        #    必须显式下发 GUI Font 比例，否则列表字号与控制栏对不上（偏小）。
        nav_scale = self._current_gui_font_scale()
        for nav in MainWindow._iter_navigators():
            try:
                nav.apply_gui_font_scale(nav_scale)
            except Exception:
                logger.debug("_apply_global_zoom: nav font scale failed", exc_info=True)
        # 不在这里落盘：__init__ 也会走到此处（那时几何/面板状态尚未恢复），
        # 且 Cmd+± 连按会逐次写盘。持久化由各用户动作入口按防抖触发。


    def _current_gui_font_scale(self) -> float:
        """当前 GUI 字体缩放比例（以 12px 为基准）。

        与 _scale_gui_font_sizes 同一套公式，供需要单独重算某个控件字号的地方
        （如 _update_title_label_color）复用，避免硬编码字号在重设样式时丢掉缩放。
        只认 GUI Font 显式字号；缩放偏移（Cmd+±）不影响界面字体。
        """
        base_px = 12
        if self._gui_font_size > 0:
            return self._gui_font_size / base_px
        return 1.0

    def _scale_gui_font_sizes(self, gui_font_size: int):
        """按 GUI Font 缩放所有 GUI 组件样式表中的 font-size 值。
        只认显式 GUI 字号；Auto（=0）恢复原始样式（Cmd+± 缩放偏移不再影响界面）。"""
        # 计算缩放比例：以 12px 为基准
        base_px = 12
        if gui_font_size > 0:
            scale = gui_font_size / base_px
        else:
            # Auto：恢复原始样式
            self._restore_original_styles()
            return

        _font_size_re = re.compile(r'font-size:\s*(\d+)px')

        from PyQt6.QtWidgets import QWidget
        # 含主窗口自身：主题写在窗口级样式表里的字号（QToolBar QPushButton 13px、
        # QLineEdit 14px）也要参与缩放。findChildren 不含 self，漏掉这层会导致
        # 工具栏按钮永远停在 13px、与输入框字号脱节。
        # 内嵌导航面板除外：它经 apply_gui_font_scale 自管缩放，样式表里已是
        # 缩放后的字号。若在此把它当基准会二次放大（_apply_theme 清缓存后
        # 16px 被当原始值再乘比例 → 19px，各窗口字号漂移不一致）。
        nav = getattr(self, 'nav_panel', None)
        for widget in (self, *self.findChildren(QWidget)):
            if nav is not None and (widget is nav or nav.isAncestorOf(widget)):
                continue
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
            config = app_config.read_config()
            if config:
                saved_history = config.get('working_dir_history', [])
                saved_freq = config.get('working_dir_freq', {})
                # 同步其它窗口的显式删除：黑名单与磁盘取并集，剔除本窗口
                # 显式选回的路径，合并结果里不得出现黑名单路径
                disk_removed = set(config.get('working_dir_removed', []) or [])
                self._dir_history_removed = (
                    (self._dir_history_removed | disk_removed)
                    - self._dir_history_readded)
                # 合并：以文件中的数据为基础，同时保留本窗口新增但尚未保存的条目
                merged_history = [p for p in saved_history
                                  if p not in self._dir_history_removed]
                merged_freq = dict(saved_freq)
                for p in self.working_dir_history:
                    if p in self._dir_history_removed:
                        continue
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
            logger.debug("_reload_dir_history_from_config: suppressed exception", exc_info=True)

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

        # 弹层是惰性创建的：若此时 GUI Font 已选了显式字号，上一轮缩放没赶上它，
        # 这里补跑一次，让搜索框的 14px 基准立刻按当前字号缩放（缓存会记下基准，
        # 之后的字号切换正常复用）。
        if self._gui_font_size > 0:
            self._scale_gui_font_sizes(self._gui_font_size)

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
        detach = self._ql_detach_modifier_held()
        self._ql_hide()
        if item_type == "browse":
            self._quick_launch_browse()
        elif item_type == "manage":
            self._manage_quick_launch_dirs()
        else:
            self._quick_launch_with_dir(dir_path, detach=detach)

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
            detach = self._ql_detach_modifier_held()
            self._ql_hide()
            self._quick_launch_with_dir(text, detach=detach)
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


    def _quick_launch_with_dir(self, dir_path: str, detach: bool = False):
        """以指定目录快速启动新终端标签页并自动启动预设

        注意：不改变当前窗口的工作目录，新tab使用指定目录

        detach=True（Shift/Cmd + 点击）：会话启动后立即执行「扩展为新窗口」。
        在本窗口先建 tab 再启动再分离，而不是直接开新窗口里启动，是为了沿用
        本窗口当前选中的预设（新窗口的预设选择是各自独立的）。
        """
        dir_path = os.path.expanduser(dir_path)
        # 归一化：Finder（工具栏/Dock 启动器）传来的路径带尾部斜杠，
        # basename('.../foo/') == '' 会让标签名退回整条路径。normpath 去掉
        # 尾斜杠后 basename 才拿得到真正的文件夹名，也让目录历史条目一致。
        dir_path = os.path.normpath(dir_path)

        if not os.path.isdir(dir_path):
            self._styled_message_box(QMessageBox.Icon.Warning, t("msg.error"), t("msg.dir_not_found", path=dir_path))
            return

        try:
            # 添加到目录历史
            self._add_to_dir_history(dir_path)

            # 获取目录名作为标签名
            dir_name = self._tab_name_for_dir(dir_path)

            # 创建新标签页，并存储独立的工作目录
            idx = self._add_new_tab(tab_name=dir_name, tab_cwd=dir_path)
            # 用 page 引用而非索引定位待分离的 tab：分离前索引可能因增删 tab 变化
            detach_page = self.tab_widget.widget(idx) if detach else None

            # 自动启动当前预设，传递目标工作目录
            # 使用闭包捕获 dir_path，避免时序问题
            def start_session_delayed(cwd=dir_path):
                if sip.isdeleted(self):
                    return
                self._start_session(cwd=cwd)
                if detach_page is not None:
                    # 会话已启动（后续预设命令经闭包绑定终端对象发送，
                    # 跟着 tab 一起搬走不受影响），直接扩展为新窗口
                    i = self.tab_widget.indexOf(detach_page)
                    if i >= 0:
                        self._detach_tab(i, None, follow_drag=False)
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
        dialog = DirectoryHistoryDialog(self.working_dir_history, self, theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_dirs = dialog.get_directories()
            # 记录被显式删除的路径：进持久黑名单，保存时从磁盘并集中剔除，
            # 启动时也不再被 cwd 自动加入复活（如 Linux 桌面启动 cwd=$HOME）
            removed = set(self.working_dir_history) - set(new_dirs)
            if removed:
                self._dir_history_removed |= removed
                self._dir_history_readded -= removed
            # 更新目录历史
            self.working_dir_history = new_dirs
            # 同步更新下拉框
            self._populate_working_dirs()
            # 保存到配置文件
            self._save_config()



    def _init_window_state(self):
        """MainWindow 本体（非 mixin）的关窗/联动标志，唯一默认值"""
        self._closing_in_progress = False       # closeEvent 已进入清理阶段（防重入）
        self._force_closing = False             # 强制关闭路径：跳过确认弹窗
        self._applying_shared_left_width = False  # 正在应用跨窗口同步的侧栏宽度
        self._nav_attention = False             # 导航面板"需要注意"标记
        self._nav_attention_source = None       # 触发注意标记的终端

    def _track_detached_window(self, win):
        """登记一个由本窗口派生的顶层窗口：持强引用防 GC，销毁后自动摘除。

        以前只 append 不 remove：WA_DeleteOnClose 删掉 C++ 侧后，Python
        包装对象（连同其 SessionManager 线程池、日志缓冲）被本窗口永久持有。
        """
        self.detached_windows.append(win)
        try:
            win.destroyed.connect(lambda *_: self._forget_detached_window(win))
        except Exception:
            logger.debug("_track_detached_window: destroyed hook failed", exc_info=True)

    def _forget_detached_window(self, win):
        self.detached_windows = [w for w in self.detached_windows if w is not win]

    # 终端 → 窗口的全部信号。新建终端（_create_terminal）与跨窗口接管
    # （_add_new_tab 的 external 分支）必须走同一张表：以前两处各维护一份
    # 列表，漏断的信号让旧窗口继续响应已转移的终端（分屏左移在两个窗口各
    # 执行一次），漏接的信号（alert_matched）让输出提醒在接管后失效。
    _TERMINAL_SIGNAL_NAMES = (
        'input_recorded', 'output_recorded', 'session_ended', 'image_pasted',
        'close_tab_requested', 'new_tab_requested',
        'manage_presets_requested', 'add_command_requested',
        'manage_local_presets_requested', 'add_local_command_requested',
        'close_split_requested', 'split_horizontal_requested',
        'split_vertical_requested', 'move_split_left_requested',
        'move_split_up_requested', 'rename_split_requested',
        'attention_requested', 'interaction_requested',
        'alert_matched', 'scrollback_pressure_changed',
    )

    def _terminal_signal_slots(self, terminal):
        """返回 {信号名: 槽} —— 唯一的接线真相源。"""
        return {
            'input_recorded': self._on_input,
            'output_recorded': self._on_output,
            'session_ended': lambda t=terminal: self._on_terminal_ended(t),
            'image_pasted': self._on_image_pasted,
            'close_tab_requested': self._close_tab_or_window,
            'new_tab_requested': self._add_new_tab,
            'manage_presets_requested': self._manage_presets,
            'add_command_requested': self._add_new_preset,
            'manage_local_presets_requested': self._manage_local_presets,
            'add_local_command_requested': self._add_new_local_preset,
            'close_split_requested': self._close_current_split,
            'split_horizontal_requested':
                lambda: self._split_current_tab(self._shift_held()),
            'split_vertical_requested':
                lambda: self._split_vertical_current_terminal(self._shift_held()),
            'move_split_left_requested': self._move_split_left,
            'move_split_up_requested': self._move_split_up,
            'rename_split_requested': lambda t=terminal: self._rename_split(t),
            'attention_requested': lambda t=terminal: self._on_terminal_attention(t),
            'interaction_requested': lambda t=terminal: self._on_terminal_interaction(t),
            'alert_matched': lambda pat, t=terminal: self._on_terminal_alert(t, pat),
            'scrollback_pressure_changed':
                lambda lv, t=terminal: self._on_scrollback_pressure(t),
        }

    def _wire_terminal_signals(self, terminal):
        """把终端的全部信号接到本窗口。"""
        slots = self._terminal_signal_slots(terminal)
        assert set(slots) == set(self._TERMINAL_SIGNAL_NAMES)
        for name in self._TERMINAL_SIGNAL_NAMES:
            getattr(terminal, name).connect(slots[name])

    def _unwire_terminal_signals(self, terminal):
        """断开终端全部信号的所有接收者（接管前由新窗口调用）。

        逐个信号各自 try：某个信号恰好没有连接时 disconnect() 抛 TypeError，
        不能让它把后面的信号全部跳过。
        """
        for name in self._TERMINAL_SIGNAL_NAMES:
            try:
                getattr(terminal, name).disconnect()
            except (TypeError, RuntimeError):
                pass  # 该信号本就没有连接

    def _create_terminal(self) -> TerminalWidget:
        """创建一个新终端并连接信号"""
        terminal = TerminalWidget()
        terminal.image_prefix_enabled = self.image_prefix_enabled
        terminal.image_save_local = self.image_save_local
        terminal.set_mouse_click_forward_enabled(
            self._mouse_click_forward_enabled)

        # 设置快速命令提供者回调
        terminal.quick_commands_provider = lambda: self.presets

        # 设置本地快速命令提供者回调
        terminal.local_quick_commands_provider = lambda: self.local_presets

        # 应用当前主题颜色
        t = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])
        terminal.bg_color = QColor(t['terminal_bg'])
        terminal.fg_color = QColor(t['terminal_fg'])
        if t.get('is_light_theme'):
            terminal.set_light_theme_colors(
                t.get('terminal_colors'),
                t.get('terminal_bright_colors'),
                t.get('selection_color'),
                t.get('cursor_color')
            )

        # 连接信号（与跨窗口接管共用同一张表，见 _wire_terminal_signals）
        self._wire_terminal_signals(terminal)

        # 设置工作目录（用于自动启动时）
        terminal.set_working_dir(self._window_cwd)

        # 应用全局缩放偏移
        if self._global_zoom_delta != 0:
            target_size = max(8, min(32, 12 + self._global_zoom_delta))
            terminal.term_font.setPointSize(target_size)
            terminal._calculate_char_size()

        # 安装事件过滤器来监听焦点变化（并登记归属窗口）
        self._adopt_terminal(terminal)

        return terminal

    def _adopt_terminal(self, terminal):
        """把 terminal 的归属转移到本窗口：摘掉原窗口的事件过滤器，装上自己的。

        必须摘除旧的：事件过滤器不会随 setParent 自动解绑。标签页被拖到别的
        窗口后若旧窗口的过滤器还在，该终端每次获得焦点都会同时把**旧窗口**的
        active_terminal 设成一个已经不属于它的终端；旧窗口后续的关闭分屏会
        因此关错终端、分屏会误判成整页重组（见
        tests/test_cross_window_active_terminal.py）。
        """
        old = getattr(terminal, '_owner_window', None)
        if old is not None and old is not self:
            try:
                terminal.removeEventFilter(old)
            except RuntimeError:
                pass  # 旧窗口的 C++ 对象已销毁，过滤器随之失效
            if getattr(old, 'active_terminal', None) is terminal:
                old.active_terminal = None
        terminal._owner_window = self
        terminal.installEventFilter(self)

    def _owns_terminal(self, terminal) -> bool:
        """terminal 是否归本窗口所有（跨窗口串台的统一判据）。"""
        return (terminal is not None
                and getattr(terminal, '_owner_window', None) is self)

    def _current_active_terminal(self):
        """本窗口可信的活动终端；串台（属于别的窗口）时返回 None。"""
        term = self.active_terminal
        if term is None:
            return None
        if not self._owns_terminal(term):
            self.active_terminal = None
            return None
        try:
            term.isVisible()   # 探测 C++ 对象是否还活着
        except RuntimeError:
            self.active_terminal = None
            return None
        return term

    def eventFilter(self, obj, event):
        """事件过滤器 - 监听终端焦点变化"""
        # 处理终端焦点变化
        if event.type() == QEvent.Type.FocusIn:
            # 检查是否是终端控件。只认归本窗口所有的终端：过滤器万一有残留
            # （旧窗口未摘除），也不会把别窗口的终端记成自己的活动终端。
            if isinstance(obj, TerminalWidget) and self._owns_terminal(obj):
                self.active_terminal = obj
        elif event.type() == QEvent.Type.MouseButtonPress:
            # 点击终端时「确定性」触发弹簧。focusChanged 在终端已持有键盘焦点时
            # 不会再次发出（Ubuntu/X11 上尤其常见——运行 Claude Code 时终端长期
            # 持焦），仅靠 focusChanged 会导致点了也不展宽。这里按点击目标侧补一次
            # 重排；若该侧已展开则内部提前返回，不产生多余动画。
            if isinstance(obj, TerminalWidget):
                self._spring_to_side_on_click('terminal')
                # 再在终端内部的分屏里把被点的那个窗格展宽
                self._spring_expand_inner(obj)
        elif event.type() == QEvent.Type.KeyPress:
            # 用户在点亮绿点的来源终端中按键 → 视为已响应该交互提示，清除绿点。
            # （来自其他终端/Git 面板的提醒不受影响，仍靠切窗口/切 tab 清除）
            if obj is self._nav_attention_source:
                self._clear_nav_attention()
            # 标签页徽章：在该终端按键 = 已响应，挂起的橙/绿点回落
            if isinstance(obj, TerminalWidget):
                _idx = self._find_tab_of_terminal(obj)
                if _idx is not None:
                    self._clear_tab_pending_badge(_idx)
        return super().eventFilter(obj, event)

    def _shift_held(self):
        """是否按住 Shift —— 按住时分屏作用于整个标签页，而非当前小窗口"""
        return bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)










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



    def _focus_in_editor_area(self) -> bool:
        """当前键盘焦点是否落在编辑器区域（某个文件编辑窗格）里。"""
        if not hasattr(self, 'editor_area') or not self.editor_area.isVisible():
            return False
        fw = QApplication.focusWidget()
        return fw is not None and (
            fw is self.editor_area or self.editor_area.isAncestorOf(fw)
        )


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
            logger.debug("_clamp_window_pos: suppressed exception", exc_info=True)
        return x, y

    @staticmethod
    def _slide_window_to(window, x, y, duration=140):
        """把顶层窗口平滑滑移到 frame 坐标 (x, y)（move 语义），代替瞬移吸附。

        只动位置不动尺寸：逐帧 resize 会触发终端 reflow 造成卡顿。动画对象
        挂在窗口上防 GC；重复调用会先停掉上一段动画。
        """
        sx, sy = window.x(), window.y()
        if (sx, sy) == (x, y):
            return
        prev = getattr(window, '_slide_anim', None)
        if prev is not None:
            prev.stop()
        anim = QVariantAnimation(window)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _step(t):
            if sip.isdeleted(window):
                anim.stop()
                return
            window.move(round(sx + (x - sx) * t), round(sy + (y - sy) * t))

        anim.valueChanged.connect(_step)
        anim.finished.connect(lambda: setattr(window, '_slide_anim', None))
        window._slide_anim = anim
        anim.start()






    def _scrollback_dot_icon(self, level: int) -> QIcon:
        """生成/缓存 scrollback 压力指示点图标：0=空 / 1=琥珀 / 2=红。"""
        if level <= 0:
            return QIcon()
        cache = getattr(self, '_scrollback_icon_cache', None)
        if cache is None:
            cache = self._scrollback_icon_cache = {}
        if level in cache:
            return cache[level]
        color = QColor('#e0a83a') if level == 1 else QColor('#e0524c')  # 琥珀 / 红
        sz = 14
        pm = QPixmap(sz, sz)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(3, 3, sz - 6, sz - 6)
        p.end()
        icon = QIcon(pm)
        cache[level] = icon
        return icon

    def _on_scrollback_pressure(self, terminal):
        """某终端的 scrollback 压力变化 → 更新其所在 tab 的指示点（取该 tab 内最高等级）。"""
        try:
            for idx, terminals in self.tab_terminals.items():
                if terminal not in terminals:
                    continue
                worst = None
                level = 0
                for term in terminals:
                    try:
                        lv = term.scrollback_level()
                    except Exception:
                        lv = 0
                    if lv > level:
                        level = lv
                        worst = term
                self.tab_widget.setTabIcon(idx, self._scrollback_dot_icon(level))
                self.tab_widget.setTabToolTip(
                    idx, worst.scrollback_tooltip() if worst is not None else "")
                break
        except Exception as e:
            logger.warning(f"[Scrollback] update tab indicator failed: {e}")





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
        # 标签页徽章：新会话启动，清掉上一会话残留的提醒点
        self._refresh_tab_badge(tab_idx)

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

    def _any_terminal_running(self) -> bool:
        """窗口内是否还有终端在运行（跨全部标签页与分屏）。"""
        for terminals in getattr(self, 'tab_terminals', {}).values():
            for term in terminals:
                try:
                    if isinstance(term, TerminalWidget) and sip.isdeleted(term):
                        continue        # 控件已析构，视为未运行
                    if term.is_running():
                        return True
                except RuntimeError:
                    continue
        return False

    def _on_session_ended(self):
        """会话结束回调。

        录制会话是**窗口级共享**的（所有标签页/分屏的输出都进同一个
        session）。因此只有当窗口里再没有终端在跑时才真正结束它——否则
        一个标签的 SSH 掉线就会把整窗停录：其它标签仍在正常工作，输出却
        不再进历史/导出（add_output 抛 "No active session"），状态栏还
        误报「已停止」，自动保存也被关掉。
        """
        session_id = self.current_session.session_id if self.current_session else None

        if self._any_terminal_running():
            # 只提示该进程已退出，保留窗口会话与自动保存
            if session_id:
                self.statusbar.showMessage(
                    t("status.process_exited", session_id=session_id))
            return

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

    def _open_file_in_editor(self, file_path: str, line_no: int = 0):
        """在内置编辑器中打开文件（落到当前活动窗格）。
        line_no > 0 时（来自内容搜索结果）打开后跳转到该行。"""
        if not hasattr(self, 'editor_area'):
            return

        # 在活动窗格中打开文件
        if not self.editor_area.open_file_in_active(file_path):
            return

        # 内容搜索：打开后跳到命中行
        if line_no and line_no > 0:
            self.editor_area.goto_line_in_active(line_no)

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


    def _set_left_panel_width(self, width):
        """记录左侧栏宽度（进程级共享）并在拖动时实时联动到其它已打开窗口。

        拖动本窗口的左侧栏分隔条会经 splitterMoved → _capture_explorer_layout
        进到这里：宽度有变化时立刻推给其它展开了侧边栏的窗口，让它们同步变宽，
        减少窗口间切换的认知负担。被其它窗口同步过来时（_applying_shared_left_width）
        不再回传，避免来回触发。
        """
        if not isinstance(width, int) or width <= 0:
            return
        changed = (self._saved_left_panel_width != width)
        self._saved_left_panel_width = width
        if changed and not self._applying_shared_left_width:
            # 拖拽期间不实时联动：所有窗口共用同一个 GUI 线程，周期性让其它
            # 5 个窗口同步整窗重排会堵死事件循环、拖拽掉帧（窗口越多越卡）。
            # 只挂起最新值，待拖拽流静默（_end_splitter_drag_fast_resize，
            # 160ms 视为松手）一次性广播；非拖拽来源仍走 80ms 节流定时器。
            self._left_width_broadcast_pending = width
            if (not self._splitter_drag_active
                    and not self._left_width_broadcast_timer.isActive()):
                self._left_width_broadcast_timer.start()

    def _broadcast_left_panel_width(self, width):
        """把左侧栏宽度实时应用到其它所有已打开窗口（仅限同一屏幕）。"""
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            if w is self or not isinstance(w, MainWindow) or sip.isdeleted(w):
                continue
            # 分散在不同显示器上的窗口不联动：尺寸语境不同，强行对齐没有意义
            if w._screen_key() != self._screen_key():
                continue
            # 看不见的窗口不值得为同步付整窗重排的代价；
            # 恢复/激活时 changeEvent 里有按共享宽度对齐的兜底
            if not w.isVisible() or w.isMinimized():
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
        # 宽度已一致时跳过：setSizes 即使数值不变也会走整窗重排，纯浪费
        sizes = self.main_splitter.sizes()
        if sizes and sizes[0] > 0 and abs(sizes[0] - width) <= 1:
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
        shared = self._saved_left_panel_width  # 本屏共享值，或磁盘播种的兜底
        if isinstance(shared, int) and shared > 0:
            # 采用本屏其它窗口已确立的共享宽度
            self._applying_shared_left_width = True
            try:
                self._update_splitter_sizes()
            finally:
                self._applying_shared_left_width = False
        else:
            # 本窗口作为种子：确立本屏共享宽度并强制广播（广播只达同屏窗口）
            sizes = self.main_splitter.sizes()
            left_width = sizes[0] if sizes else 0
            if left_width > 0:
                self._saved_left_panel_width = left_width
                self._broadcast_left_panel_width(left_width)



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
                    logger.debug("_on_ai_completion_toggled: suppressed exception", exc_info=True)

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

    def _on_word_wrap_toggled(self, enabled: bool):
        """切换编辑器自动换行（右键菜单或 Alt+Z）：应用到本窗口所有窗格、广播、落盘。"""
        enabled = bool(enabled)
        self._editor_word_wrap = enabled
        if hasattr(self, 'editor_area') and self.editor_area is not None:
            self.editor_area.set_word_wrap_enabled(enabled)
        self._broadcast_word_wrap_state()
        self._save_config()

    def _broadcast_word_wrap_state(self):
        """把自动换行开关同步到所有 MainWindow 窗口（含其编辑器窗格），
        避免多窗口下旧窗口退出时把 editor_word_wrap 覆盖回旧值。"""
        enabled = self._editor_word_wrap
        app = QApplication.instance()
        if not app:
            return
        for widget in app.topLevelWidgets():
            if widget is self or not isinstance(widget, MainWindow):
                continue
            widget._editor_word_wrap = enabled
            if hasattr(widget, 'editor_area') and widget.editor_area is not None:
                widget.editor_area.set_word_wrap_enabled(enabled)

    def _toggle_word_wrap(self):
        """Alt+Z：翻转全局自动换行（走与右键菜单相同的落盘/广播路径）。"""
        self._on_word_wrap_toggled(not self._editor_word_wrap)

    def _on_auto_save_toggled(self, enabled: bool):
        """切换编辑器失焦自动保存：应用到本窗口、广播、落盘。

        刚打开时立刻存一次当前的未保存改动——用户此刻的意图就是"别再让我忘"，
        等下一次失焦才生效会显得没反应。
        """
        enabled = bool(enabled)
        self._editor_auto_save = enabled
        if hasattr(self, 'editor_area') and self.editor_area is not None:
            self.editor_area.set_auto_save_enabled(enabled)
            if enabled:
                self.editor_area.auto_save_all_dirty()
        self._broadcast_auto_save_state()
        self._save_config()

    def _broadcast_auto_save_state(self):
        """把自动保存开关同步到所有 MainWindow 窗口（含其编辑器），
        避免多窗口下旧窗口退出时把 editor_auto_save 覆盖回旧值。"""
        enabled = self._editor_auto_save
        app = QApplication.instance()
        if not app:
            return
        for widget in app.topLevelWidgets():
            if widget is self or not isinstance(widget, MainWindow):
                continue
            widget._editor_auto_save = enabled
            if hasattr(widget, 'editor_area') and widget.editor_area is not None:
                widget.editor_area.set_auto_save_enabled(enabled)

    def _auto_save_editors_on_leave(self, old, new):
        """焦点离开编辑器区域时自动保存（QApplication.focusChanged 驱动）。

        只在「从编辑器内部移到编辑器外部」这一次跳变时保存：编辑器内部的
        焦点流转（编辑区↔查找框↔窗格间）不触发，避免高频写盘/上传。
        """
        if not self._editor_auto_save:
            return
        area = getattr(self, 'editor_area', None)
        if area is None or sip.isdeleted(area):
            return

        def _inside(w):
            return w is not None and (w is area or area.isAncestorOf(w))

        if _inside(old) and not _inside(new):
            area.auto_save_all_dirty()

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
        if self._applying_spring:
            return
        # 被其它窗口同步左侧栏宽度期间也跳过：此刻翻转门控会启动 _animate_main_sizes
        # 的逐帧 setSizes 动画，与同步流叠加成重排风暴。同步结束由
        # _end_splitter_drag_fast_resize 按最终宽度补判一次。
        if self._applying_shared_left_width:
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
        old_gate = self._spring_width_gate
        if old_gate:
            new_gate = w <= self.SPRING_PANE_DISABLE   # 单列宽于 DISABLE 才关闭
        else:
            new_gate = w < self.SPRING_PANE_ENABLE     # 单列窄于 ENABLE 才重新允许
        if new_gate == old_gate:
            return
        self._spring_width_gate = new_gate
        # 仅当用户开了 spring 才需要联动布局
        if not self._spring_mode_enabled:
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
        if not self._spring_mode_enabled:
            return False
        if not hasattr(self, 'editor_area') or not self.editor_area.isVisible():
            return False
        if self.main_splitter.indexOf(self.editor_area) < 0:
            return False
        if self.main_splitter.indexOf(self._main_content_stack) < 0:
            return False
        # 窗口太宽时 spring 自动失效（两边都能舒服铺开）。门控值在 resize 时按滞回更新，
        # 这里只读，避免每次查询都重算/抖动。
        if not self._spring_width_gate:
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
        # 内层分屏的展宽只看窗格自身实际宽度，不受编辑器↔终端那道门控影响：
        # 窗口整体够宽、但某个分屏窗格仍然很窄时，点它照样应该弹开。
        self._spring_expand_inner(new)
        # 先按当前合计宽度刷新门控：窗口本来就很宽 / 没经历过 resize 时，门控可能仍停在
        # 旧值，这里确保「点编辑器」前门控是新鲜的（够宽则此处即触发失效+恢复均衡）。
        self._update_spring_width_gate()
        if not self._spring_applicable():
            return
        target = self._spring_target_for_widget(new)
        if target is None:
            return
        if target == self._spring_current_side:
            # 目标侧与记录一致时通常无需重排。但记录可能与实际布局脱节
            # （外部 setSizes 重排后记录未更新），此时按实际布局自愈：
            # 点击/聚焦哪侧就展宽哪侧。动画进行中（正朝记录侧过渡）不打断，
            # 否则「展开动画起步即被重启」会卡一下。
            if self._spring_anim is not None or self._spring_actual_side() == target:
                return
        self._apply_spring(target)

    def _spring_actual_side(self):
        """按 main_splitter 的实际尺寸判断当前哪侧更宽：'editor'/'terminal'/None。

        用「实际布局」而非记录值 _spring_current_side 判断，能自愈各种脱节：从别的
        程序切回窗口（deactivate/activate 往返不发 focusChanged）后，_spring_current_side
        可能停在旧值，与真实布局不符——若仍按记录值判断，会出现「点了那侧却因记录里
        已是那侧而不重排」。None 表示大致均衡（留出 15% 迟滞，避免均衡态反复抖动）。
        """
        if not hasattr(self, 'editor_area'):
            return None
        sizes = self.main_splitter.sizes()
        ed_idx = self.main_splitter.indexOf(self.editor_area)
        term_idx = self.main_splitter.indexOf(self._main_content_stack)
        if ed_idx < 0 or term_idx < 0 or max(ed_idx, term_idx) >= len(sizes):
            return None
        ed, tm = sizes[ed_idx], sizes[term_idx]
        if ed + tm <= 0:
            return None
        if tm >= ed * 1.15:
            return 'terminal'
        if ed >= tm * 1.15:
            return 'editor'
        return None

    def _spring_to_side_on_click(self, side):
        """鼠标点击某一侧时确定性地触发弹簧——不依赖 focusChanged，也不依赖记录值。

        目标侧由点击对象直接给出，并按「实际布局」判断是否已展开：
          · 覆盖「该侧已持有键盘焦点、点击不再发出 focusChanged」（Ubuntu 点终端常见）；
          · 覆盖「从别的程序切回窗口后 _spring_current_side 与真实布局脱节」——此时
            terminal 明明是窄的，却因记录里已是 'terminal' 而点了不弹。改用实际更宽的
            那一侧判断即可自愈：只要点击侧当前不是更宽的那侧，就展开它。
        """
        self._update_spring_width_gate()
        if not self._spring_applicable():
            return
        if self._spring_actual_side() == side:
            # 实际布局已在该侧展开：仅校正记录值、不重排（避免多余动画）
            self._spring_current_side = side
            return
        self._apply_spring(side)

    # 松开鼠标后需持续无按键这么久，挂起的 spring 重排/被暂停的动画才继续。
    # 覆盖双击、连点、点完马上去拖选的场景：这期间布局必须纹丝不动。
    _SPRING_QUIET_MS = 180

    def _defer_spring_while_mouse_down(self, slot_name, fn) -> bool:
        """鼠标按住期间挂起 spring 重排，松开并安静 _SPRING_QUIET_MS 后执行。

        按下瞬间就重排会让窗格在指针下方移动/文本换行，文本控件把这段
        相对位移当成拖拽——表现为「点一下就自己选中了一片内容」，误选后
        接着输入还会误改内容。挂起到松开+静默后执行则完全避开；正常拖拽
        选择不受影响（重排发生在其后，已做的选择按文本位置保留）。
        同一槽位后到的请求覆盖先到的。返回是否已挂起。
        """
        if QApplication.mouseButtons() == Qt.MouseButton.NoButton:
            return False
        pending = getattr(self, '_pending_spring_reflows', None)
        if pending is None:
            pending = self._pending_spring_reflows = {}
        pending[slot_name] = fn
        self._start_spring_release_timer()
        return True

    def _start_spring_release_timer(self):
        timer = getattr(self, '_spring_release_timer', None)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(30)
            timer.timeout.connect(self._spring_release_tick)
            self._spring_release_timer = timer
        self._spring_quiet_since = None
        timer.start()

    def _spring_release_tick(self):
        """轮询等待「无按键且安静满 _SPRING_QUIET_MS」，然后续播被暂停的
        动画并执行挂起的重排。任何一次再按下都会重置静默计时。"""
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            self._spring_quiet_since = None
            return
        now = time.monotonic()
        if getattr(self, '_spring_quiet_since', None) is None:
            self._spring_quiet_since = now
            return
        if (now - self._spring_quiet_since) * 1000 < self._SPRING_QUIET_MS:
            return
        self._spring_release_timer.stop()
        self._spring_quiet_since = None
        # 先续播被按下暂停的动画（布局继续走向原目标）
        paused = getattr(self, '_spring_paused_anims', None) or []
        for anim in list(paused):
            try:
                if anim.state() == QAbstractAnimation.State.Paused:
                    anim.resume()
            except RuntimeError:
                pass  # 动画对象已销毁
        if paused:
            paused.clear()
        # 再执行挂起的重排（后到的意图已覆盖先到的）
        pending = getattr(self, '_pending_spring_reflows', None) or {}
        jobs = list(pending.values())
        pending.clear()
        for job in jobs:
            try:
                job()
            except RuntimeError:
                pass  # 目标控件在等待期间被销毁

    def _register_spring_anim(self, anim):
        """登记进行中的 spring 动画，供「按下即冻结」查询。"""
        reg = getattr(self, '_live_spring_anims', None)
        if reg is None:
            reg = self._live_spring_anims = set()
        reg.add(anim)
        anim.finished.connect(lambda a=anim: reg.discard(a))

    def _pause_spring_anims_for_press(self):
        """鼠标按下瞬间冻结所有进行中的 spring 动画。

        v1.17.6 把重排挪到松开后执行，但松开后动画播放的 170ms 里如果紧接
        着再次按下（双击/连点/点完马上拖选），窗格仍在指针下移动——文本
        控件照样把位移当成拖选。这里让任何按下都立刻暂停动画：按住期间
        布局静止，松开并安静 _SPRING_QUIET_MS 后由 _spring_release_tick 续播。
        """
        reg = getattr(self, '_live_spring_anims', None)
        if not reg:
            return
        paused = getattr(self, '_spring_paused_anims', None)
        if paused is None:
            paused = self._spring_paused_anims = []
        froze_any = False
        for anim in list(reg):
            try:
                if anim.state() == QAbstractAnimation.State.Running:
                    anim.pause()
                    paused.append(anim)
                    froze_any = True
            except RuntimeError:
                reg.discard(anim)
        if froze_any:
            self._start_spring_release_timer()

    def _apply_spring(self, target, animate=True):
        """把 main_splitter 中编辑器/终端的合计宽度按弹簧比例分配给指定一侧。"""
        if self._defer_spring_while_mouse_down(
                'main', lambda: self._apply_spring(target, animate)):
            return
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
        inactive = max(self.SPRING_INACTIVE_MIN, int(combined * self.SPRING_INACTIVE_RATIO))
        if combined - inactive < inactive * 2:
            # 合计太窄（约 <660px）时 220px 地板会反噬：active 被压到与
            # inactive 相差无几，点击后布局几乎不动，表现为「spring 失灵」。
            # 此时放弃地板、改纯比例，保证被点的一侧始终拿到 ~70%——
            # 窄窗口下优先让正在看的一侧有足够阅读区域。
            inactive = max(1, int(combined * self.SPRING_INACTIVE_RATIO))
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

    def _spring_expand_inner(self, widget):
        """在 widget 所在的每一层「横向 splitter」里把它这一支展宽。

        补齐旧实现只在编辑器↔终端之间弹的不足：终端内部（以及编辑器内部）
        的分屏窗格同样生效——点哪个窄窗格，哪个就变宽，方便直接使用。

        判据是该分支的**实际宽度**（不是按合计宽度÷列数估算的单列宽度）：
        窄于 SPRING_PANE_ENABLE 才弹，已经够宽就不动。因此反复点同一个窗格
        不会抖动（第二次点时它已经宽了），点另一个窄窗格才会把焦点让过去。

        只有「终端区」与「编辑器区」内部的 splitter 参与——左侧栏那一整列
        （窗口导航 / Explorer / Git / Remote）是控制面板，绝不能被弹簧改宽。
        main_splitter 本身也不在此处理（左侧栏与编辑器、终端同属它），
        编辑器↔终端仍由 _apply_spring 走专门逻辑。
        """
        if not self._spring_mode_enabled:
            return
        if self._defer_spring_while_mouse_down(
                'inner', lambda: self._spring_expand_inner(widget)):
            return
        node = widget
        parent = node.parentWidget() if node is not None else None
        while parent is not None and node is not self:
            if (isinstance(parent, QSplitter)
                    and parent.orientation() == Qt.Orientation.Horizontal
                    and self._spring_allowed_splitter(parent)):
                self._spring_expand_child(parent, node)
            node = parent
            parent = parent.parentWidget()

    def _spring_allowed_splitter(self, splitter) -> bool:
        """该 splitter 是否允许参与弹簧重分配。

        白名单：必须严格位于终端区（_main_content_stack）或编辑器区
        （editor_area）**内部**。用白名单而不是「排除 main_splitter」，
        这样左侧栏的任何面板——包括 Git 面板内部本来就有的横向 splitter、
        以及日后新增的面板——都不会被误伤。
        """
        for name in ('_main_content_stack', 'editor_area'):
            anchor = getattr(self, name, None)
            if anchor is None:
                continue
            try:
                if sip.isdeleted(anchor):
                    continue
                if anchor is not splitter and anchor.isAncestorOf(splitter):
                    return True
            except RuntimeError:
                continue
        return False

    def _spring_expand_child(self, splitter, child):
        """把 splitter 中 child 这一支展宽，其余各支收窄（仅在 child 偏窄时）。"""
        idx = splitter.indexOf(child)
        if idx < 0:
            return
        sizes = splitter.sizes()
        # 隐藏的窗格不参与分配，否则会把宽度分给看不见的东西
        parts = [i for i in range(min(splitter.count(), len(sizes)))
                 if splitter.widget(i) is not None
                 and not splitter.widget(i).isHidden()]
        if idx not in parts or len(parts) < 2:
            return
        combined = sum(sizes[i] for i in parts)
        if combined <= 0 or sizes[idx] >= self.SPRING_PANE_ENABLE:
            return      # 已经够宽 → 不打扰，避免点一次抖一次
        n_inactive = len(parts) - 1
        each = max(self.SPRING_INACTIVE_MIN,
                   int(combined * self.SPRING_INACTIVE_RATIO / n_inactive))
        if combined - each * n_inactive < each * 2:
            # 合计太窄时最小宽度地板会反噬（各支被压得几乎等宽，点了看不出
            # 变化）——改纯比例分配，保证被点的一支明显更宽
            each = max(1, int(combined * self.SPRING_INACTIVE_RATIO / n_inactive))
        new_sizes = list(sizes)
        for i in parts:
            new_sizes[i] = each
        new_sizes[idx] = combined - each * n_inactive
        if new_sizes != sizes:
            self._animate_inner_splitter(splitter, new_sizes)

    def _animate_inner_splitter(self, splitter, target_sizes, duration=170):
        """平滑过渡内层 splitter 到目标尺寸。

        动画对象挂在 splitter 自身上（而非 self._spring_anim）：内层与
        main_splitter 的弹簧可能同时进行，共用一个引用会互相打断。
        """
        start = splitter.sizes()
        if len(start) != len(target_sizes):
            splitter.setSizes(target_sizes)
            return
        prev = getattr(splitter, '_spring_anim', None)
        if prev is not None:
            prev.stop()
        terms = [term for term in splitter.findChildren(TerminalWidget)
                 if hasattr(term, 'set_fast_resize')]
        for term in terms:
            term.set_fast_resize(True)   # 动画期间只缩放旧缓存，避免逐帧重建整屏

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _on_val(f):
            if sip.isdeleted(splitter):
                return
            splitter.setSizes(
                [int(round(s + (e - s) * f)) for s, e in zip(start, target_sizes)])

        def _on_done():
            if not sip.isdeleted(splitter):
                splitter.setSizes(target_sizes)
                splitter._spring_anim = None
            for term in terms:
                if not sip.isdeleted(term):
                    term.set_fast_resize(False)   # 按最终尺寸重建一次，文本恢复清晰

        anim.valueChanged.connect(_on_val)
        anim.finished.connect(_on_done)
        self._register_spring_anim(anim)
        splitter._spring_anim = anim
        anim.start()

    def _reconcile_spring_after_layout_change(self):
        """侧栏 Git/Explorer 切换会把 editor/终端重排回默认比例（重新 dock 编辑器
        的副作用），并不挪键盘焦点。若不处理，用户之前弹宽的一侧会被切换重置掉；
        这里把弹簧比例「原样恢复」到切换前那一侧。

        关键：用 animate=False 无动画瞬时恢复。否则会看到布局先跳到默认比例、再
        动画弹回——表现为「切个侧栏也在触发弹簧动画」（用户并没点 editor/terminal）。
        无动画恢复后，净比例与切换前一致，也不再有可见的弹跳。
        """
        self._update_spring_width_gate()
        if not self._spring_applicable():
            return
        target = (self._spring_target_for_widget(QApplication.focusWidget())
                  or self._spring_current_side or 'editor')
        self._apply_spring(target, animate=False)

    def _set_terminals_fast_resize(self, on: bool):
        """弹簧动画期间让 main_splitter 里的终端走「缩放旧缓存」而非每帧整屏重建，
        消除连续 resize 的卡顿；动画结束再恢复并重建为清晰文本。"""
        for term in self.main_splitter.findChildren(TerminalWidget):
            if hasattr(term, 'set_fast_resize'):
                term.set_fast_resize(on)



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
        self._register_spring_anim(anim)
        self._spring_anim = anim
        anim.start()

    # ---------- Remote 面板的分屏（与 Explorer 行为一致） ----------





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
















    @staticmethod
    def _shell_quote(s: str) -> str:
        """简单的 POSIX shell 引用（仅在 ssh 命令拼接时用）"""
        if s and all(c.isalnum() or c in "@/_.-+:=,%" for c in s):
            return s
        return "'" + s.replace("'", "'\\''") + "'"



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


    def launch_initial_session(self, cwd: str):
        """--working-dir 启动路径：首个标签仍是启动页时直接在该目录起会话"""
        if os.path.isdir(cwd):
            self._start_session(cwd=cwd)

    def _toggle_shell_context_menu(self, enabled: bool):
        import shell_integration
        ok, err = (shell_integration.install() if enabled
                   else shell_integration.uninstall())
        if not ok:
            QMessageBox.warning(
                self, t("settings.shell_menu_failed_title"),
                t("settings.shell_menu_failed_msg", error=err))
            return
        # Linux：扩展要 python3-nautilus + 重启 Nautilus 才生效，给一次性图文指引
        if enabled and sys.platform == "linux":
            QMessageBox.information(
                self, t("settings.shell_menu_linux_title"),
                t("settings.shell_menu_linux_guide"))
            return
        self.statusBar().showMessage(
            t("settings.shell_menu_installed") if enabled
            else t("settings.shell_menu_removed"), 5000)

    def _install_finder_toolbar_launcher(self):
        """生成 Finder 工具栏启动器并引导用户拖入工具栏（仅 macOS）。

        解决空白处右键无入口：工具栏按钮任意窗口点一下即在当前目录开终端。
        app 无法代替用户拖入工具栏（系统限制），生成后在 Finder 里选中它、
        给出图文指引。
        """
        import shell_integration
        ok, path = shell_integration.install_toolbar_launcher()
        if not ok:
            QMessageBox.warning(
                self, t("settings.shell_menu_failed_title"),
                t("settings.shell_menu_failed_msg", error=path))
            return
        # 在 Finder 里选中刚生成的 app，方便用户直接 ⌘ 拖到工具栏
        try:
            subprocess.run(["/usr/bin/open", "-R", path],
                           capture_output=True, timeout=10)
        except Exception:
            logger.debug("_install_finder_toolbar_launcher: suppressed exception", exc_info=True)
        QMessageBox.information(
            self, t("settings.toolbar_launcher_title"),
            t("settings.toolbar_launcher_guide"))

    def _show_settings_popup_menu(self):
        """⚙ 按钮弹出菜单：工具栏布局 / 键盘快捷键。"""
        menu = QMenu(self)
        toolbar_act = menu.addAction(t("shortcuts.toolbar_menu_item"))
        toolbar_act.triggered.connect(self._show_toolbar_manager)
        shortcuts_act = menu.addAction(t("shortcuts.menu_item"))
        shortcuts_act.triggered.connect(self._show_shortcut_settings)
        cheatsheet_act = menu.addAction(t("shortcuts.cheatsheet_menu_item"))
        cheatsheet_act.triggered.connect(self._show_shortcut_cheatsheet)
        # 完成提示音子菜单（仅 macOS 有系统音可选）
        sounds = list_notify_sounds()
        if sounds:
            self._build_notify_sound_menu(menu.addMenu(t("notify.sound_menu")), sounds)
        # 终端历史行数（scrollback）子菜单
        self._build_scrollback_menu(menu.addMenu(t("scrollback.menu")))
        # 终端解析放到后台线程（实验）：减轻高频/远程输出造成的全局卡顿
        parse_act = menu.addAction(t("settings.parse_off_gui"))
        parse_act.setCheckable(True)
        parse_act.setChecked(TerminalWidget.PARSE_ON_READER_THREAD)
        parse_act.toggled.connect(self._set_parse_off_gui)
        # 鼠标点击转发给 TUI（默认关闭，避免在 Claude Code 选项里误点）
        click_fwd_act = menu.addAction(t("settings.mouse_click_forward"))
        click_fwd_act.setCheckable(True)
        click_fwd_act.setChecked(self._mouse_click_forward_enabled)
        click_fwd_act.setToolTip(t("settings.mouse_click_forward_tooltip"))
        click_fwd_act.toggled.connect(self._set_mouse_click_forward)
        # 系统右键菜单：从文件管理器中「在 Stellar 终端中打开」目录
        import shell_integration
        if shell_integration.is_supported():
            ctx_act = menu.addAction(t("settings.shell_context_menu"))
            ctx_act.setCheckable(True)
            ctx_act.setChecked(shell_integration.is_installed())
            ctx_act.setToolTip(t("settings.shell_context_menu_tooltip"))
            ctx_act.toggled.connect(self._toggle_shell_context_menu)
        # macOS 专属：Finder 工具栏启动器（空白处右键无入口的替代，任意窗口
        # 点一下即在当前目录开终端）
        if shell_integration.toolbar_launcher_supported():
            tb_act = menu.addAction(t("settings.toolbar_launcher_menu"))
            tb_act.setToolTip(t("settings.toolbar_launcher_tooltip"))
            tb_act.triggered.connect(self._install_finder_toolbar_launcher)
        menu.addSeparator()
        export_act = menu.addAction(t("settings.export_menu"))
        export_act.triggered.connect(self._export_settings_clicked)
        import_act = menu.addAction(t("settings.import_menu"))
        import_act.triggered.connect(self._import_settings_clicked)
        menu.addSeparator()
        update_act = menu.addAction(t("update.menu_item"))
        update_act.triggered.connect(self._check_for_updates)
        auto_update_act = menu.addAction(t("update.auto_check"))
        auto_update_act.setCheckable(True)
        auto_update_act.setChecked(
            app_config.read_config().get('auto_update_check', True))
        auto_update_act.toggled.connect(
            lambda on: app_config.update_config(
                {'auto_update_check': bool(on)},
                description='auto update toggle'))
        ws_act = menu.addAction(t("settings.workspace_restore"))
        ws_act.setCheckable(True)
        ws_act.setChecked(MainWindow.workspace_restore_enabled())
        ws_act.setToolTip(t("settings.workspace_restore_tooltip"))
        ws_act.toggled.connect(
            lambda on: app_config.update_config(
                {'workspace_restore_enabled': bool(on)},
                description='workspace restore toggle'))
        alert_act = menu.addAction(t("alert.menu_toggle"))
        alert_act.setCheckable(True)
        alert_act.setChecked(TerminalWidget.ALERT_RULES_ENABLED)
        alert_act.setToolTip(t("alert.menu_toggle_tooltip"))
        alert_act.toggled.connect(self._set_output_alerts_enabled)
        alert_rules_act = menu.addAction(t("alert.menu_edit"))
        alert_rules_act.triggered.connect(self._edit_output_alert_rules)
        btn = self.toolbar_settings_btn
        menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))


    # 升级重启的窗口恢复快照有效期。换包脚本最多等 60 秒，正常升级几分钟内
    # 必然重启完成；超过视为陈旧快照（比如用户在退出确认里点了取消、几天后
    # 才再启动），启动时只消费不恢复。
    _UPDATE_RESTORE_MAX_AGE = 30 * 60

    # 输出规则提醒的默认模式（保守取值，降低 claude 叙述性文本的误报）：
    # 仅匹配强失败信号；用户可在 ⚙ →「编辑输出提醒规则…」增删（正则，每行一条）
    DEFAULT_ALERT_PATTERNS = [
        r'Traceback \(most recent call last\)',
        r'\bFAILED\b',
        r'Segmentation fault',
        r'\bpanic:',
        r'^fatal:',
    ]

    @staticmethod
    def _collect_windows_snapshot():
        """收集所有可见窗口的快照（目录/几何/颜色/侧栏/标签页列表）。

        升级重启恢复与工作区恢复共用。首条对应主窗口，其余按创建顺序。
        """
        wins = [w for w in QApplication.instance().topLevelWidgets()
                if isinstance(w, MainWindow) and not sip.isdeleted(w)
                and w.isVisible()]
        wins.sort(key=lambda w: getattr(w, '_created_time', datetime.now()))
        entries = []
        for w in wins:
            geo = w.geometry()
            tabs = []
            try:
                for i in range(w.tab_widget.count()):
                    page = w.tab_widget.widget(i)
                    tabs.append({
                        'cwd': w.tab_cwds.get(i) or '',
                        'name': getattr(page, '_custom_tab_name', None) or '',
                    })
            except Exception:
                logger.debug("_collect_windows_snapshot: suppressed exception", exc_info=True)
            entries.append({
                'cwd': getattr(w, '_window_cwd', '') or '',
                'geometry': [geo.x(), geo.y(), geo.width(), geo.height()],
                'maximized': bool(w.isMaximized()),
                'color': getattr(w, '_window_color', None),
                'panel': w._current_sidebar_panel(),
                'tabs': tabs,
                'current_tab': w.tab_widget.currentIndex(),
            })
        return entries

    def _stash_windows_for_restore(self):
        """升级重启前把所有打开的窗口快照进配置（工作目录/几何/颜色/标签页），
        供新版本首次启动时恢复（见 restore_windows_after_update）。"""
        entries = MainWindow._collect_windows_snapshot()
        app_config.update_config(
            {'update_restore_windows': {'ts': time.time(), 'windows': entries}},
            description='stash windows for update restore')

    # ---------- 工作区恢复（正常退出 → 重启找回窗口/标签布局） ----------
    # 与升级恢复不同：快照不是一次性的，而是运行期持续刷新（30s 配置自动
    # 保存搭车 + 标签结构变化时限流补写）。窗口关闭中不刷新——否则退出
    # 级联里每关一个窗口快照就缩水一格，最后只剩最后关的那个；存活窗口的
    # 周期刷新自然会收敛「用户手动关掉某窗口后继续用」的场景。

    @staticmethod
    def workspace_restore_enabled() -> bool:
        return bool(app_config.read_config().get('workspace_restore_enabled', True))

    def _checkpoint_workspace(self):
        """限流刷新工作区快照（2s 合并窗口）。标签增删/切换等结构变化时调用。"""
        if self._closing_in_progress or not self.isVisible():
            return
        timer = getattr(self, '_ws_checkpoint_timer', None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(2000)
            timer.timeout.connect(self._write_workspace_snapshot)
            self._ws_checkpoint_timer = timer
        timer.start()

    def _write_workspace_snapshot(self):
        if self._closing_in_progress:
            return
        if not MainWindow.workspace_restore_enabled():
            return
        entries = MainWindow._collect_windows_snapshot()
        if not entries:
            return
        app_config.update_config(
            {'workspace_snapshot': {'ts': time.time(), 'windows': entries}},
            description='workspace checkpoint')

    @staticmethod
    def restore_workspace_on_start(primary):
        """启动时恢复上次工作区（app.py 在升级恢复未发生时调用）。

        快照非一次性消费：运行期会持续刷新覆盖。首条套用到主窗口，
        其余各开新窗口；标签页（目录/自定义名）一并恢复，会话不自动启动。
        """
        if not MainWindow.workspace_restore_enabled():
            return
        snap = app_config.read_config().get('workspace_snapshot')
        if not isinstance(snap, dict):
            return
        entries = [e for e in (snap.get('windows') or []) if isinstance(e, dict)]
        if not entries:
            return
        try:
            primary._apply_restored_window_state(entries[0])
        except Exception:
            logger.exception("failed to restore primary window workspace")
        if len(entries) <= 1:
            return

        def _open_rest():
            if sip.isdeleted(primary):
                return
            for entry in entries[1:]:
                try:
                    primary._open_restored_window(entry)
                except Exception:
                    logger.exception("failed to reopen workspace window")
        QTimer.singleShot(300, _open_rest)

    @staticmethod
    def restore_windows_after_update(primary):
        """新版本首次启动时恢复升级前打开的窗口（一次性消费快照）。

        由 app.py 在主窗口 show() 之后调用。首条快照套用到主窗口
        （目录/几何/颜色），其余各开一个新窗口。快照消费即删，失败或
        过期都不会反复触发。
        """
        pending = app_config.read_config().get('update_restore_windows')
        if not pending:
            return False

        def _consume(cfg):
            if 'update_restore_windows' not in cfg:
                return False
            del cfg['update_restore_windows']
        app_config.update_config_with(
            _consume, description='consume update restore stash')

        if not isinstance(pending, dict):
            return False
        if time.time() - float(pending.get('ts') or 0) \
                > MainWindow._UPDATE_RESTORE_MAX_AGE:
            return False
        entries = [e for e in (pending.get('windows') or [])
                   if isinstance(e, dict)]
        if not entries:
            return False
        try:
            primary._apply_restored_window_state(entries[0])
        except Exception:
            logger.exception("failed to restore primary window after update")
        if len(entries) <= 1:
            return True

        # 其余窗口延后创建：先让主窗口完成首次显示/布局，避免启动期
        # 多个窗口同时初始化互相抢占
        def _open_rest():
            if sip.isdeleted(primary):
                return
            for entry in entries[1:]:
                try:
                    primary._open_restored_window(entry)
                except Exception:
                    logger.exception("failed to reopen window after update")
        QTimer.singleShot(300, _open_rest)
        return True

    def _apply_restored_window_state(self, entry: dict, apply_geometry: bool = True):
        """把一条窗口快照套用到本窗口：目录 → 颜色 → 几何/最大化。

        apply_geometry=False 时跳过几何/最大化——新开的恢复窗口由
        _align_window_to_geometry 的校正循环负责（show 前 setGeometry 到
        目标值会让 macOS 首显挪动后 Qt 缓存与真实几何脱节，后续 setGeometry
        被当作「无变化」跳过，窗口永远校不回来）。
        """
        cwd = entry.get('cwd') or ''
        if cwd and os.path.isdir(cwd) and cwd != self._window_cwd:
            self.working_dir_combo.setCurrentText(cwd)
            self._apply_working_dir()
        color = entry.get('color')
        if color:
            try:
                self._set_window_color(color)
            except Exception:
                logger.debug("_apply_restored_window_state: suppressed exception", exc_info=True)
        if apply_geometry:
            geo = entry.get('geometry')
            if isinstance(geo, (list, tuple)) and len(geo) == 4:
                try:
                    self.setGeometry(*[int(v) for v in geo])
                except Exception:
                    logger.debug("_apply_restored_window_state: suppressed exception", exc_info=True)
            if entry.get('maximized'):
                self.showMaximized()
        # 侧边栏保持升级前的开关状态。旧版本写的快照没有 panel 键，
        # 此时不动——沿用启动时按全局配置恢复出的状态
        if 'panel' in entry:
            try:
                self._apply_sidebar_panel(entry.get('panel'))
            except Exception:
                logger.exception("failed to restore sidebar panel after update")
        # 标签页恢复（目录/自定义名，不自动起会话）。旧快照没有 tabs 键则跳过
        tabs = entry.get('tabs')
        if isinstance(tabs, list) and tabs:
            try:
                self._restore_tabs_from_snapshot(tabs, entry.get('current_tab', 0))
            except Exception:
                logger.exception("failed to restore tabs from snapshot")

    def _restore_tabs_from_snapshot(self, tabs, current_idx):
        """重启后**不再重建多标签**：会话无法恢复，成排的空标签没有用
        （用户点名去掉，2026-08-25）。只把「重启前正在看的那个标签」的
        目录/自定义名套到现有的单个标签上，保住工作上下文；也不自动
        启动会话——避免重启后未经确认就批量拉起 claude/ssh。
        """
        if not tabs:
            return
        try:
            cur = int(current_idx)
        except (TypeError, ValueError):
            cur = 0
        entry = tabs[cur] if 0 <= cur < len(tabs) else tabs[0]
        if not isinstance(entry, dict):
            return
        cwd = entry.get('cwd') or ''
        if cwd and os.path.isdir(cwd):
            self.tab_cwds[0] = cwd
            terms = self.tab_terminals.get(0, [])
            if terms and not terms[0].has_started():
                terms[0].set_working_dir(cwd)
        name = entry.get('name') or ''
        if name:
            page0 = self.tab_widget.widget(0)
            if page0 is not None:
                page0._custom_tab_name = name
            self.tab_widget.setTabText(0, name)

    def _current_sidebar_panel(self):
        """当前打开的侧边栏面板名（explorer/git/remote 互斥），全关则 None。"""
        if getattr(self, 'explorer_panel_visible', False):
            return 'explorer'
        if getattr(self, 'git_panel_visible', False):
            return 'git'
        if getattr(self, 'remote_panel_visible', False):
            return 'remote'
        return None

    def _apply_sidebar_panel(self, panel):
        """把侧边栏调到指定面板（None=全关）。toggle 打开时会自动关掉
        互斥的其它面板，所以目标面板直接 toggle 一次即可。"""
        current = self._current_sidebar_panel()
        if current == panel:
            return
        if panel == 'explorer':
            self._toggle_explorer_panel()
        elif panel == 'git':
            self._toggle_git_panel()
        elif panel == 'remote':
            self._toggle_remote_panel()
        elif current == 'explorer':
            self._toggle_explorer_panel()
        elif current == 'git':
            self._toggle_git_panel()
        elif current == 'remote':
            self._toggle_remote_panel()

    def _open_restored_window(self, entry: dict):
        """按快照新开一个独立窗口（目录随快照；目录已不存在则用默认目录）。

        几何/最大化不在 show 前套用，而是交给拖拽分离同款的
        _align_window_to_geometry 校正循环：先隐形显示在偏移位置、再逐拍
        断言目标几何直到系统（台前调度等）不再乱动，显形时已是最终尺寸。
        show 前 setGeometry 的老做法会因 macOS 首显挪动 + Qt 几何缓存脱节
        而永远校不回来（表现为「要手动动一下窗口才恢复原尺寸」）。
        """
        cwd = entry.get('cwd') or ''
        initial = {'cwd': cwd} if cwd and os.path.isdir(cwd) else None
        win = MainWindow(initial_tab_data=initial)
        win._apply_restored_window_state(entry, apply_geometry=False)
        # 保持引用防 GC，与拖拽分离/远程新窗口同一跟踪列表
        self._track_detached_window(win)

        geo = entry.get('geometry')
        if isinstance(geo, (list, tuple)) and len(geo) == 4:
            target = QRect(*[int(v) for v in geo])
            MainWindow._align_window_to_geometry(
                win, target, bool(entry.get('maximized')))
        else:
            # 旧快照无几何：按默认几何直接显示
            win.show()
            if entry.get('maximized'):
                win.showMaximized()
        win.raise_()
        return win

    def _set_output_alerts_enabled(self, enabled: bool):
        """切换输出规则提醒（对所有终端立即生效）并落盘。"""
        TerminalWidget.set_output_alert_rules(
            [p for p, _ in TerminalWidget._ALERT_COMPILED] or
            MainWindow.DEFAULT_ALERT_PATTERNS, enabled)
        self._save_config()

    def _edit_output_alert_rules(self):
        """编辑输出提醒规则：每行一条正则，空行忽略；清空恢复默认。"""
        from PyQt6.QtWidgets import QInputDialog
        current = [p for p, _ in TerminalWidget._ALERT_COMPILED]
        text, ok = QInputDialog.getMultiLineText(
            self, t("alert.edit_title"), t("alert.edit_label"),
            "\n".join(current))
        if not ok:
            return
        patterns = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not patterns:
            patterns = list(MainWindow.DEFAULT_ALERT_PATTERNS)
        TerminalWidget.set_output_alert_rules(
            patterns, TerminalWidget.ALERT_RULES_ENABLED)
        self._save_config()
        self.statusbar.showMessage(
            t("alert.rules_saved", count=len(TerminalWidget._ALERT_COMPILED)), 4000)

    def _set_parse_off_gui(self, enabled: bool):
        """切换"终端解析放到后台线程"。立即对所有终端生效（on_output 每次按此分流），
        并落盘。把 pyte 解析移出唯一的 GUI 线程，是多窗口/远程高频输出卡顿的根治向。"""
        TerminalWidget.PARSE_ON_READER_THREAD = bool(enabled)
        self._save_config()
        self.statusbar.showMessage(t("settings.parse_off_gui_applied"), 4000)

    def _set_mouse_click_forward(self, enabled: bool):
        """切换「鼠标点击转发给 TUI」。立即对所有窗口的所有终端生效并落盘。

        关闭时（默认）：终端里单击不再转发给开启鼠标上报的程序——Claude Code 的
        选项菜单不会被误点触发；文本选择、滚轮上报不受影响。打开时：恢复点击
        lazygit / fzf / htop 等界面的能力。
        """
        self._mouse_click_forward_enabled = bool(enabled)
        self._apply_mouse_click_forward_to_terminals()
        # 广播到其它窗口，避免多窗口下旧值回写覆盖
        app = QApplication.instance()
        if app:
            for widget in app.topLevelWidgets():
                if widget is self or not isinstance(widget, MainWindow):
                    continue
                widget._mouse_click_forward_enabled = bool(enabled)
                widget._apply_mouse_click_forward_to_terminals()
        self._save_config()
        self.statusbar.showMessage(
            t("settings.mouse_click_forward_on" if enabled
              else "settings.mouse_click_forward_off"), 4000)

    def _apply_mouse_click_forward_to_terminals(self):
        """把当前「点击转发」开关下发给本窗口所有已存在的终端。"""
        enabled = self._mouse_click_forward_enabled
        for term in self.findChildren(TerminalWidget):
            term.set_mouse_click_forward_enabled(enabled)

    @staticmethod
    def _clamp_scrollback(value) -> int:
        """把 scrollback 行数夹到合理区间 [500, 100000]，非法值回退默认 5000。"""
        try:
            v = int(value)
        except (TypeError, ValueError):
            return 5000
        return max(500, min(100000, v))

    def _build_scrollback_menu(self, submenu):
        """填充「终端历史行数」子菜单：几档预设，当前值打勾。仅对之后新建的终端生效。"""
        from PyQt6.QtGui import QActionGroup
        group = QActionGroup(submenu)
        group.setExclusive(True)
        current = TerminalWidget.SCROLLBACK_LINES
        presets = [1000, 2000, 5000, 10000, 20000]
        # 当前值若不在预设里（用户手改过配置），也插进去保证有勾
        if current not in presets:
            presets = sorted(set(presets + [current]))
        for n in presets:
            act = submenu.addAction(f"{n:,}")
            act.setCheckable(True)
            act.setChecked(n == current)
            group.addAction(act)
            act.triggered.connect(lambda _checked, v=n: self._set_scrollback(v))

    def _set_scrollback(self, value: int):
        """改终端 scrollback 上限：记忆、落盘并提示「对之后新建的终端生效」。"""
        TerminalWidget.SCROLLBACK_LINES = self._clamp_scrollback(value)
        self._save_config()
        self.statusbar.showMessage(t("scrollback.applied"), 4000)

    def _build_notify_sound_menu(self, submenu, sounds):
        """填充「完成提示音」子菜单：无 + 各系统音，当前项打勾，点击即选即试听。"""
        from PyQt6.QtGui import QActionGroup
        group = QActionGroup(submenu)
        group.setExclusive(True)
        current = self._notify_sound

        def add(name, label):
            act = submenu.addAction(label)
            act.setCheckable(True)
            act.setChecked(name == current)
            group.addAction(act)
            act.triggered.connect(lambda _checked, n=name: self._set_notify_sound(n))

        add('', t("notify.sound_none"))
        submenu.addSeparator()
        for name in sounds:
            add(name, name)

    def _set_notify_sound(self, name: str):
        """选中某个提示音：记忆、落盘并立即试听（静音项不试听）。"""
        self._notify_sound = name
        self._save_config()
        play_notify_sound(name)


    def _show_llm_config(self):
        """显示 LLM API 配置对话框"""
        dialog = LLMConfigDialog(self.llm_configs, self.default_llm_config, self, theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.llm_configs = dialog.get_configs()
            self.default_llm_config = dialog.get_default_index()
            # 标记本窗口改过 LLM 配置 → _save_config 才会用本窗口的内存值落盘，
            # 否则默认从磁盘取最新值，避免覆盖其它窗口的改动。
            self._llm_configs_modified = True
            self._save_config()
            self.statusbar.showMessage(t("status.llm_config_saved"), 3000)




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
                logger.debug("_apply_language: suppressed exception", exc_info=True)
        if hasattr(self, 'editor_area'):
            self.editor_area.apply_language()






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

    def _make_styled_message_box(self, icon, title, text, buttons=QMessageBox.StandardButton.Ok):
        """构造带明确样式的消息框（不 exec），供需要附加勾选框等定制的调用方使用"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setIcon(icon)
        msg_box.setStandardButtons(buttons)
        # 底色/文字/按钮全部按当前主题推导（浅色主题浅盒子、深色主题深盒子）；
        # 勾选框的对勾图仍需显式给，app 级规则的样式在消息框里不生效
        from utils import checkbox_checkmark_url
        from main_window_theme import message_box_qss
        check_url = checkbox_checkmark_url()
        check_image = f"image: url({check_url});" if check_url else ""
        theme = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])
        msg_box.setStyleSheet(message_box_qss(theme, check_image))
        return msg_box

    def _styled_message_box(self, icon, title, text, buttons=QMessageBox.StandardButton.Ok):
        """创建带明确样式的消息框，避免深色主题导致文字不可见"""
        return self._make_styled_message_box(icon, title, text, buttons).exec()


    def _merge_dir_history_for_save(self):
        """写配置前，把磁盘上的目录历史并入本窗口，避免多窗口互相覆盖。

        背景：所有窗口共享同一份配置文件，而 _save_config 会把整份配置（含
        working_dir_history）原样写回。若直接用本窗口内存里的历史覆盖磁盘，
        其它窗口新增的路径就会被冲掉 —— 表现为"只有最后退出的窗口的路径被
        保存"。这里在写入前先与磁盘做并集，确保任意窗口新增的路径都不丢。

        删除处理：用户显式删除的路径记在持久黑名单 self._dir_history_removed
        （落盘为 working_dir_removed），并集时主动剔除，否则会被磁盘版本或
        启动时的 cwd 自动加入复活。黑名单与磁盘版本取并集（让删除跨窗口
        生效），再减去本窗口显式选回的路径（_dir_history_readded）。
        """
        try:
            cfg = app_config.read_config()
            saved_history = cfg.get('working_dir_history', []) or []
            saved_freq = cfg.get('working_dir_freq', {}) or {}

            mem_removed = self._dir_history_removed
            disk_removed = set(cfg.get('working_dir_removed', []) or [])
            readded = self._dir_history_readded
            removals = (mem_removed | disk_removed) - readded
            self._dir_history_removed = removals
            mem_history = self.working_dir_history if hasattr(self, 'working_dir_history') else []
            mem_freq = self._working_dir_freq

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
        except Exception:
            # 合并失败不应阻断保存：保持内存中的历史原样写出
            logger.debug("_merge_dir_history_for_save: suppressed exception", exc_info=True)




    # 兼容旧约定：名字叫 completion / 补全 等的配置也当作补全配置
    _COMPLETION_CONFIG_NAMES = {'completion', '补全', 'autocomplete', 'complete', 'copilot'}




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
                # 面板是延迟创建的，补上当前主题（否则保持默认深色样式）
                _t = self.THEMES.get(self.current_theme)
                if _t:
                    MainWindow._global_window_navigator.apply_theme(_t)
                # 补上当前 GUI Font 比例，避免新建的浮动面板字号偏小
                MainWindow._global_window_navigator.apply_gui_font_scale(
                    self._current_gui_font_scale())
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
        # 高度联动开启时把新高度（节流）推给同屏的其它窗口
        if MainWindow._sidebar_height_sync:
            MainWindow._nav_height_by_screen[self._screen_key()] = \
                self._saved_nav_list_height
            self._nav_height_broadcast_pending = self._saved_nav_list_height
            if not self._nav_height_broadcast_timer.isActive():
                self._nav_height_broadcast_timer.start()

    def _on_sidebar_sync_toggled(self, state):
        """侧栏底部「联动高度」勾选框：全局开关 + 立即以本窗口当前布局对齐所有窗口。"""
        enabled = (state == Qt.CheckState.Checked.value)
        MainWindow._sidebar_height_sync = enabled
        MainWindow._set_sidebar_sync_checkboxes(enabled)
        if enabled and hasattr(self, 'nav_panel'):
            h = self.nav_panel.embedded_list_height()
            if isinstance(h, int) and h > 0:
                MainWindow._nav_height_by_screen[self._screen_key()] = h
                self._nav_height_broadcast_pending = h
                self._broadcast_nav_list_height()
        self._save_config()  # 记忆开关状态，下次启动恢复

    @staticmethod
    def _set_sidebar_sync_checkboxes(enabled: bool):
        """把「联动高度」勾选框状态同步到所有窗口（不触发各自的 toggled 逻辑）。"""
        app = QApplication.instance()
        for w in (app.topLevelWidgets() if app else []):
            if not isinstance(w, MainWindow) or sip.isdeleted(w):
                continue
            cb = getattr(w, 'sidebar_sync_checkbox', None)
            if cb is not None:
                cb.blockSignals(True)
                cb.setChecked(enabled)
                cb.blockSignals(False)

    def _broadcast_nav_list_height(self):
        """把挂起的导航列表高度应用到同屏的其它窗口。

        跨屏窗口不联动（尺寸语境不同）。setFixedHeight 对隐藏窗口几乎零
        成本（不触发重绘），所以同屏窗口不区分可见性统一应用，省去激活时
        的兜底对齐。"""
        h = self._nav_height_broadcast_pending
        self._nav_height_broadcast_pending = None
        if not isinstance(h, int) or h <= 0:
            return
        app = QApplication.instance()
        if app is None:
            return
        for w in app.topLevelWidgets():
            if w is self or not isinstance(w, MainWindow) or sip.isdeleted(w):
                continue
            if w._screen_key() != self._screen_key():
                continue
            w._apply_shared_nav_list_height(h)

    def _apply_shared_nav_list_height(self, h: int):
        """收到其它窗口联动过来的导航列表高度：对齐并更新本窗口的落盘记忆。"""
        if not hasattr(self, 'nav_panel'):
            return
        if self.nav_panel.embedded_list_height() == h:
            return
        self._saved_nav_list_height = self.nav_panel.set_embedded_list_height(h)

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
        # 「联动高度」开关只在分隔条存在（导航条内嵌显示）时才有意义
        if hasattr(self, 'sidebar_sync_row'):
            self.sidebar_sync_row.setVisible(show)
        if show:
            try:
                self.nav_panel._force_refresh()
            except Exception:
                logger.debug("_sync_embedded_nav: suppressed exception", exc_info=True)

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
                    logger.debug("_set_navigator_dock_mode: suppressed exception", exc_info=True)
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
                    # 面板是延迟创建的，补上当前主题（否则保持默认深色样式）
                    if wins:
                        _t = wins[0].THEMES.get(wins[0].current_theme)
                        if _t:
                            MainWindow._global_window_navigator.apply_theme(_t)
                        # 补上当前 GUI Font 比例，避免新建的浮动面板字号偏小
                        MainWindow._global_window_navigator.apply_gui_font_scale(
                            wins[0]._current_gui_font_scale())
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
                logger.debug("_set_navigator_dock_mode: suppressed exception", exc_info=True)
        MainWindow._persist_navigator_dock_mode()

    @staticmethod
    def _persist_navigator_dock_mode():
        """把当前停靠方式写入主配置文件。"""
        app_config.update_config(
            {'navigator_dock_mode': MainWindow._navigator_dock_mode},
            description='navigator-dock-mode')

    @staticmethod
    def _persist_navigator_enabled(enabled: bool):
        """把 Window Navigator 开关状态写入主配置文件（供下次启动恢复）。"""
        app_config.update_config({'navigator_enabled': bool(enabled)},
                                 description='navigator-enabled')

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
                logger.debug("_broadcast_navigator_refresh: suppressed exception", exc_info=True)

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
        dialog = PresetDialog(self.presets, self, theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.presets = dialog.get_presets()
            self._presets_modified = True  # 标记预设已修改
            self._populate_presets()
            self._save_config()
            self.statusbar.showMessage(t("status.preset_saved"), 3000)

    def _add_new_preset(self):
        """打开预设管理对话框并自动添加新预设"""
        dialog = PresetDialog(self.presets, self, auto_add=True, theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
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


    def _manage_local_presets(self):
        """打开本地预设管理对话框"""
        dialog = PresetDialog(self.local_presets, self, title=t("msg.manage_local_commands"), theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.local_presets = dialog.get_presets()
            if self._save_local_commands():
                self.statusbar.showMessage(t("status.local_preset_saved"), 3000)
                self._broadcast_local_commands_reload()

    def _add_new_local_preset(self):
        """添加新本地预设"""
        dialog = PresetDialog(self.local_presets, self, auto_add=True, title=t("msg.manage_local_commands"), theme=self.THEMES.get(self.current_theme, self.THEMES["午夜黑"]))
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
        my_cwd = self._window_cwd
        if not my_cwd:
            return
        for w in app.topLevelWidgets():
            if w is self or not isinstance(w, MainWindow) or sip.isdeleted(w):
                continue
            if getattr(w, '_window_cwd', None) == my_cwd:
                try:
                    w._load_local_commands()
                except Exception:
                    logger.debug("_broadcast_local_commands_reload: suppressed exception", exc_info=True)

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
        if self._force_closing:
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
            # 编辑器未保存改动：强制路径不弹窗，但同步刷崩溃恢复备份，保证可恢复不丢
            try:
                if getattr(self, 'editor_area', None) is not None:
                    self.editor_area.flush_autosave_all()
            except Exception as e:
                logger.warning(f"[ForceClose] editor autosave flush failed: {e}")
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
                pass  # C++ 对象已在两拍之间被销毁：本就无需再 close
            except Exception as e:
                logger.warning(f"[ForceClose] close failed: {e}")
        QTimer.singleShot(0, _do_close)

    def closeEvent(self, event):
        """窗口关闭事件"""
        # 防止重入：closeEvent 在 macOS 上可能因 native 事件链被多次触发，
        # 第二次触发时已经清理过的资源会再次被访问 → 段错误
        if self._closing_in_progress:
            event.accept()
            return

        # 强制关闭路径：跳过确认弹窗（保存已在 force_close_with_save 中完成）
        force_closing = self._force_closing

        # 退出前保护编辑器里的未保存改动：逐个有改动的窗格弹 保存/丢弃/取消。
        # 取消则中止关闭。强制路径不在此弹窗（force_close_with_save 已刷自动保存）。
        if not force_closing and getattr(self, 'editor_area', None) is not None:
            try:
                if not self.editor_area.prompt_save_all():
                    event.ignore()
                    return
            except Exception as e:
                logger.warning(f"[Close] editor save prompt failed: {e}")

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
            msg_box = self._make_styled_message_box(
                QMessageBox.Icon.Question,
                t("msg.confirm_exit_title"),
                t("msg.confirm_exit_msg"),
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No |
                QMessageBox.StandardButton.Cancel,
            )
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
                logger.debug("closeEvent: suppressed exception", exc_info=True)

        # 停止所有 OpenAI API 服务器（独立 try：服务器停不掉不该阻塞关窗）
        try:
            self.openai_server_manager.stop_all()
        except Exception as e:
            logger.warning(f"[Close] stop openai servers failed: {e}")

        # 停止定时器
        try:
            self.auto_save_timer.stop()
        except Exception:
            logger.debug("closeEvent: suppressed exception", exc_info=True)
        try:
            self._log_timer.stop()
        except Exception:
            logger.debug("closeEvent: suppressed exception", exc_info=True)

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
                pass  # 导航面板已随最后一个窗口销毁：无需刷新
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
                    logger.debug("_quit_if_last_main_window: suppressed exception", exc_info=True)
            # 推迟一拍退出，避免在 closeEvent / native 事件链内同步退出
            QTimer.singleShot(0, app.quit)
        except Exception as e:
            # closeEvent 内异常绝不能逃逸到 Qt C++ 侧（否则可能 abort）
            logger.warning(f"[Close] quit-if-last check failed: {e}")

    # ================== OpenAI API 服务器相关方法 ==================


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








    # ==================== 导航提醒小标（Claude/命令执行完毕） ====================

    def _on_terminal_attention(self, terminal):
        """某个终端疑似执行完毕。若该终端不是你正在看的活动终端，就在导航条目上打绿点。"""
        # 正在前台看着的活动终端不打扰
        if self.isActiveWindow() and terminal is self.active_terminal:
            return
        self._request_nav_attention(terminal)
        # 标签页徽章：完成（绿点）
        self._set_tab_pending_badge(terminal, 'done')

    def _on_terminal_interaction(self, terminal):
        """某个终端正在等待用户操作（响铃 / 确认框 / y/n 询问）。

        与"执行完毕"不同：哪怕是当前激活窗口里正在看的活动终端，也点亮导航
        绿点（用户要求：每次需要指令都提示一次，避免错过）。用户在该终端
        按键响应后由 eventFilter 清除。
        """
        self._request_nav_attention(terminal)
        # 标签页徽章：等待确认（橙点）
        self._set_tab_pending_badge(terminal, 'waiting')

    def _on_terminal_alert(self, terminal, pattern: str):
        """输出命中提醒规则（Traceback/FAILED 等）：橙点 + 导航提醒 + 状态栏。

        与 interaction 同级对待——事故比"等确认"更需要人来看。终端侧已做
        30s 静默去重，这里无需再防抖。
        """
        self._request_nav_attention(terminal)
        self._set_tab_pending_badge(terminal, 'waiting')
        try:
            self.statusbar.showMessage(
                t("alert.output_matched", pattern=pattern), 6000)
        except Exception:
            logger.debug("_on_terminal_alert: suppressed exception", exc_info=True)

    # ---------- 标签页状态徽章（运行中/等确认/已完成） ----------
    # 多标签同时跑 claude 时一眼定位：灰点=会话运行中，橙点=等你确认
    # （BEL/询问），绿点=已完成（输出停顿）。切到该标签或在其终端按键即
    # 视为已查看，回落到运行状态点。

    # 只保留瞬态提醒徽章；常驻的「运行中」灰点没有信息量（会话几乎总在跑），
    # 用户点名去掉（2026-08-04）。
    _TAB_BADGE_COLORS = {
        'waiting': '#f59e0b',
        'done': '#22c55e',
    }

    def _tab_badge_icon(self, state) -> QIcon:
        color = self._TAB_BADGE_COLORS.get(state)
        if color is None:
            return QIcon()
        cache = getattr(self, '_tab_badge_icon_cache', None)
        if cache is None:
            cache = self._tab_badge_icon_cache = {}
        icon = cache.get(state)
        if icon is None:
            pm = QPixmap(12, 12)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            p.drawEllipse(2, 2, 8, 8)
            p.end()
            icon = QIcon(pm)
            cache[state] = icon
        return icon

    def _find_tab_of_terminal(self, terminal):
        for idx, terminals in self.tab_terminals.items():
            if terminal in terminals:
                return idx
        return None

    def _set_tab_pending_badge(self, terminal, state):
        """给终端所在标签页挂起 waiting/done 徽章并刷新。"""
        idx = self._find_tab_of_terminal(terminal)
        if idx is None:
            return
        page = self.tab_widget.widget(idx)
        if page is not None:
            page._badge_pending = state
        self._refresh_tab_badge(idx)

    def _clear_tab_pending_badge(self, idx):
        """用户已查看（切到该标签/按键）：清挂起态，图标清空。"""
        page = self.tab_widget.widget(idx)
        if page is not None and getattr(page, '_badge_pending', None) is not None:
            page._badge_pending = None
        self._refresh_tab_badge(idx)

    def _refresh_tab_badge(self, idx):
        """按挂起提醒态（waiting/done）刷新一个标签页的图标；无挂起则清空。"""
        page = self.tab_widget.widget(idx)
        if page is None:
            return
        state = getattr(page, '_badge_pending', None)
        self.tab_widget.setTabIcon(
            idx, self._tab_badge_icon(state) if state else QIcon())

    def _request_nav_attention(self, source=None):
        """点亮本窗口的导航绿点（后台任务完成提醒的通用入口）。

        终端命令结束之外的来源（如 Git 面板生成提交信息完成）也可调用。
        「前台时是否不打扰」的判断由各来源自行决定后再调用——终端"执行完毕"
        要求「不是正在看的活动终端」，"等待操作"则无条件点亮，Git 生成要求
        「窗口不在前台」。source 记录点亮来源终端：用户在该终端按键即视为
        已响应并清除绿点（非终端来源传 None，只靠切窗口/切 tab 清除）。
        """
        if self._nav_attention:
            return  # 已经在提醒，避免重复刷新
        self._nav_attention = True
        self._nav_attention_source = source
        # 完成提示音：每个提醒「点亮」时响一次（dedup 守卫保证不会连环响）。
        # 声音内容由设置里的「完成提示音」决定，'' 表示静音。
        try:
            play_notify_sound(self._notify_sound)
        except Exception:
            logger.debug("_request_nav_attention: suppressed exception", exc_info=True)
        try:
            MainWindow._broadcast_navigator_refresh(invalidate_cache=True)
        except Exception:
            logger.debug("_request_nav_attention: suppressed exception", exc_info=True)

    def _clear_nav_attention(self):
        """清除本窗口的导航提醒小标（用户已查看）"""
        self._nav_attention_source = None
        if self._nav_attention:
            self._nav_attention = False
            try:
                MainWindow._broadcast_navigator_refresh(invalidate_cache=True)
            except Exception:
                logger.debug("_clear_nav_attention: suppressed exception", exc_info=True)

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
