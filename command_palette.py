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

from PyQt6.QtCore import Qt, QEvent, QPoint, pyqtSignal
from PyQt6.QtGui import QKeyEvent, QFont, QFocusEvent
from PyQt6.QtWidgets import (
    QWidget, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QLabel,
    QHBoxLayout, QFrame
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
        self._setup_ui(placeholder)

    def _setup_ui(self, placeholder: str):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.line_edit = QLineEdit(self)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setMinimumWidth(280)
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

        # 弹出列表：独立顶层窗口，使用 Popup 类型，便于点击外部关闭
        self.popup = QFrame(self.line_edit, Qt.WindowType.Popup)
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
            self.popup.hide()
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
        # macOS 上 Qt::Popup 被外部点击关闭后，内部 visible 标志和窗口实际状态
        # 容易脱钩 —— 再 show() 时表现为"逻辑已显示但渲染不出"，导致用户点击
        # 输入框毫无反应。先 hide 再 show 强制走一次完整的隐藏→显示流程。
        self.popup.hide()
        self.popup.show()
        self.popup.raise_()

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
        self.popup.hide()
        try:
            cmd.run()
        except Exception as e:
            # 失败信息打到控制台即可；UI 不弹窗以免干扰
            print(f"[CommandPalette] command failed: {cmd.title}: {e}")

    def eventFilter(self, obj, ev):
        if obj is self.line_edit:
            if ev.type() == QEvent.Type.FocusIn:
                # 只在用户"主动"聚焦时弹出列表。窗口被重新激活、菜单/弹层关闭、
                # 控件 reparent 等场景下，Qt 也会给输入框补发 FocusIn —— 这些
                # 不是用户意图，不能弹层。典型 bug：右键菜单选 V-Split 后窗口
                # raise_()/activateWindow()，焦点被以 ActiveWindowFocusReason
                # 还给搜索框，导致搜索菜单莫名弹出。
                # 鼠标点击由 MouseButtonPress 分支处理，快捷键由 focus_search()
                # 直接处理，所以这里只需放行 Tab 键导航这种真实用户操作。
                reason = ev.reason() if isinstance(ev, QFocusEvent) else None
                if reason in (
                    Qt.FocusReason.MouseFocusReason,
                    Qt.FocusReason.TabFocusReason,
                    Qt.FocusReason.BacktabFocusReason,
                    Qt.FocusReason.ShortcutFocusReason,
                ):
                    self._refresh_popup()
            elif ev.type() == QEvent.Type.FocusOut:
                # popup 自身是独立窗口，焦点移到 popup/list 时也会触发 FocusOut；
                # 简单处理：延迟一拍隐藏，避免点击 list 时来不及触发 itemClicked。
                # 用 QTimer.singleShot 也行，这里靠 popup 的 Popup 类型本身会在外点关闭。
                pass
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
                    self.line_edit.blockSignals(True)
                    try:
                        self.line_edit.clear()
                    finally:
                        self.line_edit.blockSignals(False)
                    self.line_edit.clearFocus()
                    self.popup.hide()
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
