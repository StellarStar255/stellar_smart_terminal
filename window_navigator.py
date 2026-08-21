"""窗口快速导航面板（从 main_window.py 拆出）。

永远置顶的小窗口：列出所有打开的 Stellar 主窗口，支持切换 / 重命名 / 关闭，
并用绿点显示"执行完毕"提醒。与 MainWindow 是双向关系——MainWindow 实例化
本面板，本面板反向调用 MainWindow 的静态方法与类型判断。为打破 import 环，
这里对 MainWindow 用延迟模块引用（只在方法内访问 main_window.MainWindow）。
"""
import os

from PyQt6 import sip
from PyQt6.QtCore import Qt, QEvent, QPoint, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QStyle,
    QStyleOptionViewItem, QStyledItemDelegate, QVBoxLayout, QWidget,
)

import main_window  # 延迟引用 MainWindow，打破 import 环（仅方法内访问 .MainWindow）
import app_config
from git_widget import _make_git_tool_icon
from i18n import t
from app_logging import get_logger

logger = get_logger(__name__)


# 导航条目的「执行完毕」提醒标记角色（绿点）
NAV_ATTENTION_ROLE = Qt.ItemDataRole.UserRole + 1

# 未收到主题前的兜底配色（与 themes.THEMES['深蓝'] 一致）。
# 面板样式一律从 self._theme 取色，MainWindow._apply_theme 会调 apply_theme()
# 把当前主题字典（themes.py 的一项）灌进来，浅色主题下不再是一块深色补丁。
_FALLBACK_THEME = {
    'bg_dark': '#1a1a2e',
    'bg_medium': '#16213e',
    'bg_light': '#2d2d44',
    'bg_lighter': '#3d3d5c',
    'bg_hover': '#4d4d6c',
    'border': '#3d3d5c',
    'accent': '#667eea',
    'text': '#eaeaea',
    'text_dim': '#888888',
}


def _window_screen_key(w):
    """窗口所在屏幕的标识（同 MainWindow._screen_key 分桶）；
    构造中/已销毁等取不到时归 None。"""
    try:
        return w._screen_key()
    except Exception:
        return None


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

        # 「执行完毕」提醒：在条目右侧画一个绿色小圆点。
        # 右端取「条目右端」与「可视区右缘」的较小者：窄侧栏出横向滚动条时
        # 条目右端在可视区外，不钳制的话绿点会画到屏幕外看不见。
        if index.data(NAV_ATTENTION_ROLE):
            r = option.rect
            d = 8
            right = r.right()
            view = option.widget
            if view is not None:
                right = min(right, view.viewport().rect().right())
            cx = right - d - 6
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

    def __init__(self, parent=None, embedded=False, pin_to=None):
        # embedded=True：作为普通子控件嵌入到窗口左侧栏（无独立窗口标志、无标题栏）；
        # embedded=False：原来的浮动置顶小窗口。
        # pin_to：高亮钉死在指定窗口上（内嵌面板传宿主窗口）——列表高亮永远标识
        # 「本面板属于哪个窗口」，不跟随全局活动窗口；浮动面板不传，保持跟随。
        self._embedded = embedded
        import weakref as _weakref_pin
        self._pinned_ref = _weakref_pin.ref(pin_to) if pin_to is not None else None
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

        self._theme = dict(_FALLBACK_THEME)
        self._setup_ui()
        self._apply_style()
        self._load_navigator_config()

        # 新建面板时继承当前已存在面板的排序模式与手动顺序，
        # 使多窗口列表保持一致（_manual_order 存 id(window)，跨窗口可直接复用）。
        try:
            for nav in main_window.MainWindow._iter_navigators():
                if nav is self:
                    continue
                self._sort_mode = nav._sort_mode
                self._manual_order = list(nav._manual_order)
                self.drag_hint_label.setVisible(self._sort_mode == 'manual')
                break
        except Exception:
            logger.debug("__init__: suppressed exception", exc_info=True)

        # 缓存上次的窗口信息，避免不必要的刷新
        self._last_window_info = []  # [(title, color), ...]
        self._cached_windows = []  # 缓存窗口引用

        # 刷新以事件驱动为主：建/关窗、换色、换目录、切 tab 走
        # _broadcast_navigator_refresh 主动推；标题变化/异常销毁由每个窗口的
        # windowTitleChanged/destroyed 信号即时触发（_refresh_window_list 里挂钩）。
        # 轮询只留低频兜底，接住不经上述任何路径的漏网变化（如窗口被隐藏）。
        self._hooked_window_ids = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._check_and_refresh)
        self._refresh_timer.start(30000)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 标题
        self.title_label = QLabel(t("window.navigator_list_title"))
        layout.addWidget(self.title_label)

        # 搜索框
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t("window.search_placeholder"))
        self.search_input.setClearButtonEnabled(True)
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
        # 列表样式由 _apply_list_font_size() 按当前主题 + 字号统一设置
        # （不设置悬停样式，由代码动态控制）
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

        self.compact_checkbox = QCheckBox(t("window.compact_display"))
        self.compact_checkbox.setChecked(True)  # 默认开启简洁显示
        self.compact_checkbox.setToolTip(t("window.compact_tooltip"))
        self.compact_checkbox.stateChanged.connect(self._toggle_compact_mode)
        compact_row.addWidget(self.compact_checkbox)

        # Quick Close 勾选框：勾选后右键"强制关闭"不再弹确认窗
        self.quick_close_checkbox = QCheckBox(t("window.quick_close"))
        self.quick_close_checkbox.setChecked(self._quick_close)
        self.quick_close_checkbox.setToolTip(t("window.quick_close_tooltip"))
        self.quick_close_checkbox.stateChanged.connect(self._on_quick_close_changed)
        compact_row.addWidget(self.quick_close_checkbox)

        # 「嵌入到侧栏」勾选框：勾选=内嵌到各窗口左侧栏；取消=独立浮动窗口。自动记住。
        self.embed_checkbox = QCheckBox(t("window.embed_checkbox"))
        self.embed_checkbox.setToolTip(t("window.embed_checkbox_tooltip"))
        self.embed_checkbox.blockSignals(True)
        self.embed_checkbox.setChecked(self._embedded)
        self.embed_checkbox.blockSignals(False)
        self.embed_checkbox.stateChanged.connect(self._on_embed_checkbox_changed)
        compact_row.addWidget(self.embed_checkbox)

        compact_row.addStretch()

        # 列表字体大小：固定值（此前的可调下拉已移除）。仍读取历史持久化值，
        # 让老用户外观不突变；_apply_list_font_size() 据此渲染。
        self._font_size = 12  # 默认字体大小
        # GUI Font 缩放比例（以 12px 为基准，1.0=Auto）。导航面板是全局单例、不在
        # 主窗口 findChildren 的缩放遍历里，需由主窗口显式下发，否则同一 GUI Font 下
        # 列表字号不跟着控制栏一起变大，看着偏小。见 apply_gui_font_scale()。
        self._gui_font_scale = 1.0

        # 设置按钮（小齿轮）：与 Compact/Quick Close/Embed/字号 同在一行，靠最右。
        # 用矢量绘制的齿轮图标（_make_git_tool_icon），避免 macOS 上 ⚙ 字形被渲染成
        # 彩色 emoji / 小点，保证清晰统一。
        self.settings_btn = QPushButton()
        self.settings_btn.setIconSize(QSize(16, 16))
        self.settings_btn.setToolTip(t("window.settings_tooltip"))
        self.settings_btn.setFixedSize(28, 24)
        self.settings_btn.clicked.connect(self._show_settings_menu)
        compact_row.addWidget(self.settings_btn)

        layout.addLayout(compact_row)

        # 拖拽提示标签（默认隐藏）
        self.drag_hint_label = QLabel(t("window.drag_hint"))
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
        main_window.MainWindow._set_navigator_dock_mode('embed' if checked else 'float')

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
        for nav in main_window.MainWindow._iter_navigators():
            try:
                if nav is not self:
                    nav._sort_mode = self._sort_mode
                    nav._manual_order = list(self._manual_order)
                    try:
                        nav.drag_hint_label.setVisible(self._sort_mode == 'manual')
                    except Exception:
                        logger.debug("_broadcast_sort_state: suppressed exception", exc_info=True)
                nav._force_refresh()
            except Exception:
                logger.debug("_broadcast_sort_state: suppressed exception", exc_info=True)

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
        """保存当前列表顺序。

        钉死的内嵌面板只显示本屏窗口——列表里看不到的（异屏）窗口不能被
        挤出全局手动顺序：可见项按新顺序排前，其余 id 保持原相对顺序接后。"""
        visible = []
        for i in range(self.window_list.count()):
            item = self.window_list.item(i)
            wid = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(wid, int):
                visible.append(wid)
        rest = [wid for wid in self._manual_order if wid not in visible]
        self._manual_order = visible + rest

    def _toggle_compact_mode(self, state):
        """切换简洁显示模式（所有窗口的导航面板共用此设置）"""
        compact = (state == Qt.CheckState.Checked.value)
        self._compact_mode = compact
        self._force_refresh()
        self._save_navigator_config()
        # 广播到其它导航面板（浮动 + 各窗口内嵌），保持全局一致
        for nav in main_window.MainWindow._iter_navigators():
            if nav is self:
                continue
            try:
                nav._apply_compact_mode(compact)
            except Exception:
                logger.debug("_toggle_compact_mode: suppressed exception", exc_info=True)

    def _apply_compact_mode(self, compact: bool):
        """同步外部设置的简洁显示状态：更新复选框、内部标志并刷新列表。"""
        if self._compact_mode == compact and self.compact_checkbox.isChecked() == compact:
            return
        self._compact_mode = compact
        self.compact_checkbox.blockSignals(True)
        self.compact_checkbox.setChecked(compact)
        self.compact_checkbox.blockSignals(False)
        self._force_refresh()

    def _sf(self, px: int) -> int:
        """按 GUI Font 缩放一个像素字号，最小 8px（与主窗口 _scale_gui_font_sizes 同思路）。"""
        return max(8, round(px * self._gui_font_scale))

    def apply_gui_font_scale(self, scale: float):
        """接收主窗口下发的 GUI Font 缩放比例并重刷整个面板字号。

        导航面板是全局单例，不在主窗口 findChildren 的缩放遍历内，必须显式下发，
        否则列表字号不跟随 GUI Font，与控制栏字号对不上（看着偏小）。"""
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            return
        if scale <= 0:
            scale = 1.0
        if abs(scale - self._gui_font_scale) < 1e-3:
            return
        self._gui_font_scale = scale
        # 整块面板样式（标题/搜索/勾选框/按钮/列表）都按新比例重算
        self._apply_style()

    def _apply_list_font_size(self):
        """按当前主题 + 字号设置列表样式"""
        th = self._theme
        # 列表项基准 16px：比控制栏按钮字号（14px）大两号（用户点名要的），
        # 再按 GUI Font 缩放（14pt 时约 19px）。
        list_px = self._sf(max(self._font_size, 14) + 2)
        self.window_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {th['bg_medium']};
                border: 1px solid {th['border']};
                border-radius: 4px;
                font-size: {list_px}px;
                outline: none;
            }}
            QListWidget::item {{
                padding: 8px;
                border-bottom: 1px solid {th['bg_light']};
                border-radius: 4px;
            }}
            QListWidget::item:selected {{
                background-color: {th['bg_light']};
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
                if isinstance(w, main_window.MainWindow) and not sip.isdeleted(w) and w.isVisible():
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

        # 检查窗口标题/颜色/所在屏幕是否变化
        try:
            current_info = [(w.windowTitle(), w.get_window_color(),
                             _window_screen_key(w)) for w in current_windows]
        except Exception:
            # 窗口在遍历过程中被删除
            self._refresh_window_list()
            return
        if current_info != self._last_window_info:
            self._refresh_window_list()

    def _on_hooked_window_destroyed(self, obj=None):
        """被挂钩的窗口销毁：把 id 从已挂钩集合摘除（防 id 复用导致新窗口漏挂），
        并让出到下一轮事件循环再刷新 —— destroyed 回调时对象半死，
        立刻遍历窗口列表有段错误风险。"""
        if obj is not None:
            self._hooked_window_ids.discard(id(obj))
        QTimer.singleShot(
            0, lambda: None if sip.isdeleted(self) else self._check_and_refresh())

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

    def apply_theme(self, theme: dict):
        """应用主题配色（themes.THEMES 的一项）并重刷所有子控件样式。"""
        if not theme:
            return
        self._theme = theme
        self._apply_style()

    def _apply_style(self):
        """按 self._theme 重设面板及所有子控件的样式。"""
        th = self._theme
        # hover 落在 accent 背景上时文字固定用白色，深浅主题下都有足够对比度
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {th['bg_dark']};
                border: 1px solid {th['border']};
                border-radius: 8px;
            }}
            QLabel {{
                border: none;
            }}
            QPushButton {{
                background-color: {th['bg_lighter']};
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                color: {th['text']};
                font-size: {self._sf(12)}px;
            }}
            QPushButton:hover {{
                background-color: {th['accent']};
                color: #ffffff;
            }}
        """)

        self.title_label.setStyleSheet(
            f"color: {th['accent']}; font-weight: bold; font-size: {self._sf(13)}px;")

        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {th['bg_medium']};
                border: 1px solid {th['border']};
                border-radius: 4px;
                padding: 4px 8px;
                color: {th['text']};
                font-size: {self._sf(12)}px;
            }}
            QLineEdit:focus {{
                border-color: {th['accent']};
            }}
        """)

        # 勾选框：必须把 ::indicator 的外观（边框/背景/选中态）全部定义掉，
        # 否则 Qt 会退回原生风格画指示器，与主题背景叠在一起非常突兀。
        nav_checkbox_style = f"""
            QCheckBox {{
                color: {th['text_dim']};
                font-size: {self._sf(11)}px;
                border: none;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 2px solid {th['border']};
                border-radius: 3px;
                background-color: {th['bg_medium']};
            }}
            QCheckBox::indicator:hover {{
                border-color: {th['accent']};
            }}
            QCheckBox::indicator:checked {{
                border-color: {th['accent']};
                background-color: {th['accent']};
            }}
        """
        for cb in (self.compact_checkbox, self.quick_close_checkbox, self.embed_checkbox):
            cb.setStyleSheet(nav_checkbox_style)

        # 齿轮图标颜色：深色主题用固定浅灰，浅色主题跟随次要文字色
        icon_color = th['text_dim'] if th.get('is_light_theme') else '#c8c8d8'
        self.settings_btn.setIcon(_make_git_tool_icon('gear', icon_color, 16))
        self.settings_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {th['bg_lighter']};
                border: none;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {th['bg_hover']};
            }}
        """)

        self.drag_hint_label.setStyleSheet(
            f"color: {th['accent']}; font-size: {self._sf(10)}px; border: none;")

        # 列表样式依赖主题 + 字号，统一走 _apply_list_font_size
        self._apply_list_font_size()

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
                if not isinstance(w, main_window.MainWindow):
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

        # 给新出现的窗口挂标题/销毁信号（每窗口只挂一次），把变化即时推过来；
        # 接收方是本面板，面板销毁时 Qt 自动断开这些连接
        for w in windows:
            wid = id(w)
            if wid in self._hooked_window_ids:
                continue
            try:
                w.windowTitleChanged.connect(self._check_and_refresh)
                w.destroyed.connect(self._on_hooked_window_destroyed)
                self._hooked_window_ids.add(wid)
            except Exception:
                logger.debug("_refresh_window_list: suppressed exception", exc_info=True)

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

        # 检查是否有变化（标题/颜色/提醒/所在屏幕——窗口跨屏挪动也要重建，
        # 因为钉死的内嵌面板只显示本屏窗口）
        try:
            current_info = [(w.windowTitle(), w.get_window_color(),
                             bool(getattr(w, '_nav_attention', False)),
                             _window_screen_key(w)) for w in windows]
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
        # 钉死的内嵌面板只显示与宿主同屏的窗口：分散在不同显示器时，各屏的
        # 列表只关心本屏的工作区（跨屏跳转用浮动导航面板）。编号仍按全局
        # 顺序生成，同一窗口在每块屏上的序号一致。
        pinned = self._pinned_window()
        pin_key = _window_screen_key(pinned) if pinned is not None else None
        # 重建 id → weakref 映射；旧的失效项会自然淘汰
        import weakref as _weakref
        new_refs: dict = {}
        for idx, window in enumerate(windows, 1):
            try:
                if sip.isdeleted(window):
                    continue
                if (pinned is not None and window is not pinned
                        and _window_screen_key(window) != pin_key):
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

        # 恢复之前选中（活动）窗口的高亮，找不到时回退到第一项；
        # 钉死模式（内嵌面板）永远高亮宿主窗口自己
        if self.window_list.count() > 0:
            pinned = self._pinned_window()
            if pinned is not None:
                prev_id = id(pinned)
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
        """生成淡化的背景色（与列表背景混合）- 带缓存

        Args:
            color_hex: 颜色的十六进制字符串，如 '#667eea'

        Returns:
            淡化后的 QColor 对象
        """
        list_bg = self._theme['bg_medium']  # 列表背景色（跟随主题）
        cache_key = (color_hex, list_bg)
        if cache_key in self._faded_bg_cache:
            return self._faded_bg_cache[cache_key]

        theme_color = QColor(color_hex)
        bg_color = QColor(list_bg)

        # 混合主题色和背景色，比例约 40:60（更明显的效果）
        r = int(theme_color.red() * 0.4 + bg_color.red() * 0.6)
        g = int(theme_color.green() * 0.4 + bg_color.green() * 0.6)
        b = int(theme_color.blue() * 0.4 + bg_color.blue() * 0.6)

        result = QColor(r, g, b)
        # 缓存结果（限制缓存大小）
        if len(self._faded_bg_cache) < 50:
            self._faded_bg_cache[cache_key] = result
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
            logger.debug("eventFilter: suppressed exception", exc_info=True)
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

        th = self._theme
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {th['bg_light']};
                color: {th['text']};
                border: 1px solid {th['border']};
                border-radius: 4px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: {th['accent']};
                color: #ffffff;
            }}
            QMenu::item:disabled {{
                color: {th['text_dim']};
            }}
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
        for nav in main_window.MainWindow._iter_navigators():
            if nav is self:
                continue
            try:
                nav._apply_quick_close(self._quick_close)
            except Exception:
                logger.debug("_on_quick_close_changed: suppressed exception", exc_info=True)

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
            # 父窗口（导航面板）有 QWidget 全局 QSS，会级联进 QMessageBox 把
            # 文字染得看不清。这里：
            # 1. 不传 parent，避免 QSS 级联
            # 2. 自己组合一套跟随当前主题的 QSS，与导航面板风格一致
            th = self._theme
            msg_box = QMessageBox()
            msg_box.setWindowTitle(t("window.force_close_confirm_title"))
            msg_box.setText(t("window.force_close_confirm_msg", title=title))
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            msg_box.setDefaultButton(QMessageBox.StandardButton.No)
            msg_box.setStyleSheet(f"""
                QMessageBox {{
                    background-color: {th['bg_dark']};
                }}
                QMessageBox QLabel {{
                    color: {th['text']};
                    background-color: transparent;
                    font-size: 13px;
                    border: none;
                }}
                QMessageBox QPushButton {{
                    background-color: {th['bg_lighter']};
                    color: {th['text']};
                    border: 1px solid {th['border']};
                    border-radius: 4px;
                    padding: 6px 18px;
                    min-width: 72px;
                    font-size: 12px;
                }}
                QMessageBox QPushButton:hover {{
                    background-color: {th['accent']};
                    border-color: {th['accent']};
                    color: #ffffff;
                }}
                QMessageBox QPushButton:default {{
                    background-color: {th['bg_light']};
                    border-color: {th['accent']};
                }}
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
                        logger.debug("safe_refresh: suppressed exception", exc_info=True)
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
        # 钉死模式：点击/双击切走后把高亮吸回宿主窗口自己
        # （点击本身会把 QListWidget 的 currentRow 挪到被点的行）
        if self._pinned_window() is not None:
            self.select_window(None)  # select_window 会重定向到钉死窗口

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
        """保存导航面板设置到主配置文件（app_config 单点：锁 + 原子写 + 失败日志）"""
        try:
            patch = {
                'navigator_font_size': self._font_size,
                'navigator_quick_close': bool(self._quick_close),
                'navigator_compact': bool(self._compact_mode),
            }
            # 内嵌模式下 self.x()/y() 是控件在父布局里的坐标，没有意义，不写几何
            if not self._embedded:
                patch['navigator_geometry'] = [self.x(), self.y(), self.width(), self.height()]
            app_config.update_config(patch, description='navigator')
        except Exception:
            logger.debug("_save_navigator_config: suppressed exception", exc_info=True)

    def _load_navigator_config(self):
        """从主配置文件加载导航面板设置"""
        try:
            config = app_config.read_config()
            if config:
                # 恢复字体大小（下拉控件已移除，仅沿用历史值渲染列表，避免外观突变）
                font_size = config.get('navigator_font_size', 12)
                if 8 <= font_size <= 24:
                    self._font_size = font_size
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
            logger.debug("_load_navigator_config: suppressed exception", exc_info=True)

    def closeEvent(self, event):
        """关闭时保存设置、停止定时器并发送关闭信号"""
        self._save_navigator_config()
        self._refresh_timer.stop()
        self.panel_closed.emit()
        super().closeEvent(event)

    def _pinned_window(self):
        """高亮钉死的窗口（内嵌面板的宿主）；未钉死或已销毁时返回 None。"""
        if self._pinned_ref is None:
            return None
        w = self._pinned_ref()
        if w is None or sip.isdeleted(w):
            return None
        return w

    def select_window(self, window):
        """选中指定的窗口项

        当某个窗口被激活时调用此方法，更新列表的选中状态。
        钉死模式下忽略传入目标，永远高亮宿主窗口自己——在 A 窗口的列表里
        高亮别的窗口没有意义，高亮应标识「本列表属于谁」。
        """
        pinned = self._pinned_window()
        if pinned is not None:
            window = pinned
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
