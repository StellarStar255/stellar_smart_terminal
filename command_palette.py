"""
Command Palette
工具栏里的搜索框 + 下拉命令列表，类似 VS Code 顶部的命令面板。
- register(title, callable_, group=None, tooltip=None) 注册一条命令
- clear() 清空注册表（重新注册时用）
- 用户聚焦输入框或按 Cmd+K 时弹出列表；↑↓ 导航；Enter 执行；Esc 关闭

设计原则：完全独立，不依赖具体业务；只依赖 PyQt6。
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from PyQt6.QtCore import Qt, QEvent, QPoint, QRect, pyqtSignal, QTimer
from PyQt6.QtGui import QKeyEvent, QFont
from PyQt6.QtWidgets import (
    QWidget, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QLabel,
    QHBoxLayout, QFrame, QApplication
)


@dataclass
class Command:
    title: str
    run: Callable[[], None]
    group: Optional[str] = None
    tooltip: Optional[str] = None

    def haystack(self) -> str:
        bits = [self.title]
        if self.group:
            bits.append(self.group)
        if self.tooltip:
            bits.append(self.tooltip)
        return " · ".join(bits).lower()


def _fuzzy_score(query: str, hay: str) -> int:
    """简单评分：完全子串 > 词起始子串 > 分散字符匹配。
    返回值越大越好；-1 表示不匹配。

    分散匹配只在 query 长度 ≥ 3 时启用，否则短 query 会"乱命中"过多。
    """
    q = query.strip().lower()
    if not q:
        return 0
    h = hay
    # 完整子串
    idx = h.find(q)
    if idx >= 0:
        # 词起始位置（前一个字符是非字母数字）给额外加分
        bonus = 100 if (idx == 0 or not h[idx - 1].isalnum()) else 0
        return 1000 - idx + bonus
    # query 太短，不做分散匹配
    if len(q) < 3:
        return -1
    # 分散字符匹配：每个字符按顺序出现即可；匹配跨度越紧凑分越高
    pos = 0
    first = -1
    last = -1
    for ch in q:
        i = h.find(ch, pos)
        if i < 0:
            return -1
        if first < 0:
            first = i
        last = i
        pos = i + 1
    span = max(1, last - first + 1)
    # 跨度越小越好；query 越长在跨度内越紧密，分越高
    return 100 + int(len(q) * 50 / span)


class CommandPalette(QWidget):
    """搜索框 + 弹出命令列表组件。"""

    def __init__(self, placeholder: str = "Search commands…", parent=None):
        super().__init__(parent)
        self._commands: List[Command] = []
        # 弹层可见期间是否已挂上应用级事件过滤器（用于点外部/Esc 关闭）
        self._app_filter_on = False
        # 首选宽度（像素）。0 表示不强制，沿用布局算出的自然宽度。
        # 工具栏在 pin 后用 FlowLayout 摆放，而 FlowLayout 只读 sizeHint /
        # minimumSizeHint，不读 minimumWidth —— 所以必须把宽度写进这两个 hint。
        self._preferred_width = 0
        self._setup_ui(placeholder)

    def set_box_width(self, width: int):
        """设置搜索框首选宽度（会被 FlowLayout 与普通工具栏共同遵守）。"""
        self._preferred_width = max(0, int(width))
        self.line_edit.setMinimumWidth(min(self._preferred_width, 120) if self._preferred_width else 120)
        self.updateGeometry()

    def sizeHint(self):
        s = super().sizeHint()
        if self._preferred_width:
            s.setWidth(max(s.width(), self._preferred_width))
        return s

    def minimumSizeHint(self):
        s = super().minimumSizeHint()
        if self._preferred_width:
            s.setWidth(max(s.width(), self._preferred_width))
        return s

    def _setup_ui(self, placeholder: str):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.line_edit = QLineEdit(self)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setMinimumWidth(360)
        self.line_edit.setFixedHeight(28)
        self.line_edit.setStyleSheet("""
            QLineEdit {
                background-color: #16213e;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 2px 8px;
                selection-background-color: #667eea;
            }
            QLineEdit:focus {
                border-color: #667eea;
            }
        """)
        layout.addWidget(self.line_edit)

        # 弹出列表：独立顶层窗口。
        # 关键：用 Tool + WA_ShowWithoutActivating，而不是 Qt::Popup。
        # Qt::Popup 在 show() 时会抢占键盘/鼠标 grab，导致输入框收不到后续按键
        # （打几个字弹出列表后就再也打不了字）。Tool 顶层不抢 grab，输入框保持
        # 焦点可继续输入；列表导航/回车/Esc 由 line_edit 的 eventFilter 处理。
        # 代价：Tool 不会随外部点击自动关闭，靠下方 FocusOut 延迟收起来兜底。
        self.popup = QFrame(
            self.line_edit,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.popup.setFrameShape(QFrame.Shape.NoFrame)
        self.popup.setStyleSheet("""
            QFrame {
                background-color: #1e1e2e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
            }
        """)
        pv = QVBoxLayout(self.popup)
        pv.setContentsMargins(2, 2, 2, 2)
        pv.setSpacing(0)

        self.list_widget = QListWidget(self.popup)
        self.list_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_widget.setStyleSheet("""
            QListWidget {
                background-color: transparent;
                color: #eaeaea;
                border: none;
                outline: none;
            }
            QListWidget::item {
                padding: 5px 10px;
            }
            QListWidget::item:selected {
                background-color: #667eea;
                color: white;
                border-radius: 3px;
            }
            QListWidget::item:hover {
                background-color: #2a2a3e;
                border-radius: 3px;
            }
        """)
        self.list_widget.itemActivated.connect(self._on_item_activated)
        self.list_widget.itemClicked.connect(self._on_item_activated)
        pv.addWidget(self.list_widget)

        self.empty_label = QLabel("No matches", self.popup)
        self.empty_label.setStyleSheet("color: #888; padding: 10px;")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        pv.addWidget(self.empty_label)

        # 事件
        self.line_edit.textChanged.connect(self._refresh_popup)
        self.line_edit.installEventFilter(self)

    # ---------- public API ----------

    def register(self, title: str, run: Callable[[], None],
                 group: Optional[str] = None, tooltip: Optional[str] = None):
        if not title or not callable(run):
            return
        self._commands.append(Command(title=title.strip(), run=run, group=group, tooltip=tooltip))

    def clear(self):
        self._commands.clear()

    def focus_search(self):
        """Cmd+K 入口：聚焦并选中已有文本。"""
        self.line_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.line_edit.selectAll()
        self._refresh_popup(force_show=True)

    def set_placeholder(self, text: str):
        self.line_edit.setPlaceholderText(text)

    def set_empty_text(self, text: str):
        self.empty_label.setText(text)

    # ---------- internals ----------

    def _refresh_popup(self, force_show: bool = False):
        query = self.line_edit.text()
        # 当输入为空且未聚焦时，不显示弹层；聚焦但空 → 显示所有命令。
        # force_show 用于鼠标点击/快捷键路径 —— 这些时机调用本函数时，
        # 焦点可能还没切到 line_edit（MousePress 早于 FocusIn），不能拿
        # hasFocus 作为门槛，否则会错误地把刚要弹的层隐藏掉。
        if not force_show and not self.line_edit.hasFocus():
            self._hide_popup()
            return

        items = self._rank(query)
        self.list_widget.clear()

        if not items:
            self.empty_label.show()
            self.list_widget.hide()
        else:
            self.empty_label.hide()
            self.list_widget.show()
            for cmd in items:
                label = cmd.title
                if cmd.group:
                    label = f"{cmd.group}  ›  {cmd.title}"
                qi = QListWidgetItem(label)
                if cmd.tooltip:
                    qi.setToolTip(cmd.tooltip)
                qi.setData(Qt.ItemDataRole.UserRole, cmd)
                self.list_widget.addItem(qi)
            self.list_widget.setCurrentRow(0)

        # 定位弹层在输入框正下方
        self._position_popup()
        # 不抢焦点地显示，确保 line_edit 始终保留键盘焦点以便继续输入。
        self.popup.show()
        self.popup.raise_()
        # 弹层是不抢焦点的 Tool 窗口，自身不会随外部点击/Esc 关闭；
        # 挂一个应用级过滤器来兜底（见 eventFilter 顶部分支）。
        self._install_global_filter()

    def _install_global_filter(self):
        if not self._app_filter_on:
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
                self._app_filter_on = True

    def _remove_global_filter(self):
        if self._app_filter_on:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._app_filter_on = False

    def _hide_popup(self):
        """统一的收起入口：隐藏弹层并卸掉应用级过滤器。"""
        self.popup.hide()
        self._remove_global_filter()

    def _clear_and_hide(self):
        """清空输入、释放焦点并收起弹层（Esc / 选中命令后用）。"""
        self.line_edit.blockSignals(True)
        try:
            self.line_edit.clear()
        finally:
            self.line_edit.blockSignals(False)
        self.line_edit.clearFocus()
        self._hide_popup()

    def _point_in_popup_or_input(self, global_pt: QPoint) -> bool:
        """全局坐标点是否落在弹层或输入框内（用于判断"点了外部"）。"""
        if self.popup.isVisible() and self.popup.geometry().contains(global_pt):
            return True
        le = self.line_edit
        le_rect = QRect(le.mapToGlobal(QPoint(0, 0)), le.size())
        return le_rect.contains(global_pt)

    def _hide_popup_if_idle(self):
        """输入框失焦后的兜底收起：除非输入框仍持焦、或弹层正被鼠标交互。"""
        if self.line_edit.hasFocus():
            return
        if self.popup.isActiveWindow():
            return
        self._hide_popup()

    def _position_popup(self):
        le = self.line_edit
        bl = le.mapToGlobal(QPoint(0, le.height() + 2))
        width = max(le.width(), 360)
        height = 320
        self.popup.setGeometry(bl.x(), bl.y(), width, height)

    def _rank(self, query: str, limit: int = 12) -> List[Command]:
        query = query.strip().lower()
        if not query:
            # 空 query 显示全部，按 group/title 排序
            items = sorted(self._commands, key=lambda c: ((c.group or '').lower(), c.title.lower()))
            return items[:limit]
        scored = []
        for c in self._commands:
            s = _fuzzy_score(query, c.haystack())
            if s >= 0:
                scored.append((s, c))
        scored.sort(key=lambda t: (-t[0], t[1].title.lower()))
        return [c for _, c in scored[:limit]]

    def _on_item_activated(self, item: QListWidgetItem):
        cmd: Command = item.data(Qt.ItemDataRole.UserRole)
        if cmd is None:
            return
        # 顺序很关键：必须先把 textChanged 信号屏蔽，再 clear() ——
        # 否则 clear() 触发 _refresh_popup，而此刻 line_edit 仍然 hasFocus()，
        # 会把刚 hide 的 popup 立刻 show 回来，导致后续 cmd.run() 切焦点时
        # popup 进入"逻辑可见但视觉不可见"的死状态，下次再点输入框就没反应。
        self.line_edit.blockSignals(True)
        try:
            self.line_edit.clear()
        finally:
            self.line_edit.blockSignals(False)
        self.line_edit.clearFocus()
        self._hide_popup()
        try:
            cmd.run()
        except Exception as e:
            # 失败信息打到控制台即可；UI 不弹窗以免干扰
            print(f"[CommandPalette] command failed: {cmd.title}: {e}")

    def eventFilter(self, obj, ev):
        # ① 应用级兜底（弹层可见时挂上）：点击弹层/输入框之外 → 关闭；Esc → 关闭。
        #    放在最前面，不依赖输入框是否持有键盘焦点，确保始终能收起列表。
        if self._app_filter_on and self.popup.isVisible():
            et = ev.type()
            if et == QEvent.Type.MouseButtonPress:
                gp = ev.globalPosition().toPoint()
                if not self._point_in_popup_or_input(gp):
                    # 仅收起，不吞掉事件，让这次点击照常作用到目标控件
                    self._hide_popup()
            elif et == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape:
                self._clear_and_hide()
                return True

        if obj is self.line_edit:
            if ev.type() == QEvent.Type.FocusIn:
                # 不在 FocusIn 上自动弹层。所有用户"主动"打开命令面板的入口都已
                # 单独处理：鼠标点击 → MouseButtonPress（force_show）；Cmd+K →
                # focus_search()；打字 → textChanged；方向键 → 下方 KeyPress。
                # FocusIn 反而会在程序性/意外的焦点转移时误触发 —— 典型 bug：
                # V-Split 把当前持焦点的终端 reparent 进新 splitter，Qt 用
                # focusNextChild(TabFocusReason) 把焦点甩给工具栏搜索框，导致
                # 搜索菜单莫名弹出。横向 Split 不 reparent 原终端，故无此问题。
                pass
            elif ev.type() == QEvent.Type.FocusOut:
                # Tool 顶层弹层不会随外部点击自动关闭，这里在输入框真正失焦后收起。
                # 延迟一拍：点击列表项会短暂把弹层激活成活动窗口，过早隐藏会丢掉
                # itemClicked；延迟后在 _hide_popup_if_idle 里再判断是否真的该收。
                QTimer.singleShot(150, self._hide_popup_if_idle)
            elif ev.type() == QEvent.Type.KeyPress:
                ke: QKeyEvent = ev
                key = ke.key()
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                    if not self.popup.isVisible():
                        self._refresh_popup()
                    row = self.list_widget.currentRow()
                    n = self.list_widget.count()
                    if n > 0:
                        if key == Qt.Key.Key_Down:
                            self.list_widget.setCurrentRow((row + 1) % n)
                        else:
                            self.list_widget.setCurrentRow((row - 1) % n)
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    item = self.list_widget.currentItem()
                    if item is not None:
                        self._on_item_activated(item)
                    return True
                if key == Qt.Key.Key_Escape:
                    self._clear_and_hide()
                    return True
            elif ev.type() == QEvent.Type.MouseButtonPress:
                # 三种"点了没反应"的情形都靠这里兜底：
                # 1) popup 经外部点击关闭后 line_edit 仍持有焦点 → 无 FocusIn
                # 2) macOS 上 Qt::Popup 关闭后焦点状态紊乱 → FocusIn 不可靠
                # 3) MousePress 早于 FocusIn → 此刻 hasFocus()=False
                # 主动 setFocus 把焦点拿回来，force_show 跳过 hasFocus 检查。
                self.line_edit.setFocus(Qt.FocusReason.MouseFocusReason)
                self._refresh_popup(force_show=True)
        return super().eventFilter(obj, ev)
