"""
工具栏管理器
允许用户自定义工具栏按钮的显示和布局
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QRadioButton, QButtonGroup,
    QScrollArea, QWidget, QFrame, QListWidget, QListWidgetItem,
    QAbstractItemView
)
from PyQt6.QtCore import Qt, pyqtSignal, QPropertyAnimation, QEasingCurve, QSize, pyqtProperty, QMimeData
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QDrag
from i18n import t


class ToggleSwitch(QWidget):
    """现代风格的开关切换控件"""
    toggled = pyqtSignal(bool)

    def __init__(self, checked=True, parent=None):
        super().__init__(parent)
        self._checked = checked
        self._circle_position = 22 if checked else 2
        self._bg_color = QColor("#667eea") if checked else QColor("#4a4a6a")

        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # 延迟初始化动画（优化性能）
        self._animation = None
        self._bg_animation = None

    def _ensure_animations(self):
        """确保动画已初始化（延迟创建）"""
        if self._animation is None:
            self._animation = QPropertyAnimation(self, b"circle_position", self)
            self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._animation.setDuration(200)

            self._bg_animation = QPropertyAnimation(self, b"bg_color", self)
            self._bg_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            self._bg_animation.setDuration(200)

    def get_circle_position(self):
        return self._circle_position

    def set_circle_position(self, pos):
        self._circle_position = pos
        self.update()

    circle_position = pyqtProperty(float, get_circle_position, set_circle_position)

    def get_bg_color(self):
        return self._bg_color

    def set_bg_color(self, color):
        self._bg_color = color
        self.update()

    bg_color = pyqtProperty(QColor, get_bg_color, set_bg_color)

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        if self._checked != checked:
            self._checked = checked
            self._animate()
            self.toggled.emit(checked)

    def _animate(self):
        # 确保动画已初始化
        self._ensure_animations()

        # 圆形位置动画
        self._animation.setStartValue(self._circle_position)
        self._animation.setEndValue(22 if self._checked else 2)
        self._animation.start()

        # 背景颜色动画
        self._bg_animation.setStartValue(self._bg_color)
        self._bg_animation.setEndValue(QColor("#667eea") if self._checked else QColor("#4a4a6a"))
        self._bg_animation.start()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
            self._animate()
            self.toggled.emit(self._checked)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 绘制背景胶囊
        path = QPainterPath()
        path.addRoundedRect(0, 0, 44, 24, 12, 12)
        painter.fillPath(path, self._bg_color)

        # 绘制圆形开关
        painter.setBrush(QColor("#ffffff"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(int(self._circle_position), 2, 20, 20)


class ToggleItem(QFrame):
    """带标签的开关项，支持拖拽排序"""

    def __init__(self, label: str, checked: bool = True, draggable: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self._label_text = label
        self._draggable = draggable
        self._drag_start_pos = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)

        # 拖拽手柄 - 使用简单的 QLabel
        self.drag_handle = QLabel("⋮⋮")
        self.drag_handle.setFixedSize(24, 24)
        self.drag_handle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if not draggable:
            self.drag_handle.hide()
        layout.addWidget(self.drag_handle)

        # 标签
        self.label = QLabel(label)
        layout.addWidget(self.label)

        layout.addStretch()

        # 开关
        self.toggle = ToggleSwitch(checked)
        layout.addWidget(self.toggle)

    def isChecked(self):
        return self.toggle.isChecked()

    def setChecked(self, checked):
        self.toggle.setChecked(checked)

    def setDraggable(self, draggable: bool):
        """设置是否可拖拽"""
        self._draggable = draggable
        if draggable:
            self.drag_handle.show()
        else:
            self.drag_handle.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._draggable:
            # 检查是否在拖拽手柄区域
            handle_rect = self.drag_handle.geometry()
            if handle_rect.contains(event.pos()):
                self._drag_start_pos = event.pos()
                self.drag_handle.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is not None and self._draggable:
            # 检查是否移动了足够距离开始拖拽
            if (event.pos() - self._drag_start_pos).manhattanLength() > 10:
                self._start_drag()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_start_pos is not None:
            self._drag_start_pos = None
            self.drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def _start_drag(self):
        """开始拖拽操作"""
        self._drag_start_pos = None
        self.drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)

        drag = QDrag(self)
        mime_data = QMimeData()
        # 存储按钮名称和分组名称
        btn_name = self.property("btn_name") or ""
        group_name = self.property("group_name") or ""
        mime_data.setText(f"{btn_name}|{group_name}")
        drag.setMimeData(mime_data)

        # 创建拖拽时的预览图像（使用较小的缩放以提高性能）
        pixmap = self.grab()
        drag.setPixmap(pixmap)
        drag.setHotSpot(self.drag_handle.geometry().center())

        drag.exec(Qt.DropAction.MoveAction)


class DraggableGroupContainer(QFrame):
    """支持拖拽排序的分组容器"""
    order_changed = pyqtSignal(str, list)  # (group_name, new_order)
    cross_group_move = pyqtSignal(str, str, str, int)  # (btn_name, from_group, to_group, target_index)

    def __init__(self, group_name: str, parent=None):
        super().__init__(parent)
        self.group_name = group_name
        self.button_names = []  # 当前按钮顺序
        self.items = {}  # btn_name -> ToggleItem

        self.setObjectName("groupContainer")
        self.setAcceptDrops(True)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        self._drop_indicator = None
        self._drop_index = -1

    def addItem(self, btn_name: str, item: 'ToggleItem'):
        """添加一个项目"""
        self.button_names.append(btn_name)
        self.items[btn_name] = item
        self._layout.addWidget(item)
        # 不再每次添加都更新分隔线，由调用者在添加完所有项目后调用 finishAdding()

    def finishAdding(self):
        """完成添加项目后调用"""
        pass  # 不再需要更新分隔线

    def _update_separators(self):
        """更新分隔线 - 已禁用以提高性能"""
        pass

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            text = event.mimeData().text()
            if "|" in text:
                # 接受来自任何分组的拖拽（支持跨组移动）
                event.acceptProposedAction()
                return
        event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasText():
            text = event.mimeData().text()
            if "|" in text:
                event.acceptProposedAction()
                self._update_drop_indicator(event.position().toPoint())
                return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._clear_drop_indicator()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._clear_drop_indicator()

        if not event.mimeData().hasText():
            event.ignore()
            return

        text = event.mimeData().text()
        if "|" not in text:
            event.ignore()
            return

        btn_name, source_group = text.split("|", 1)
        target_index = self._get_drop_index(event.position().toPoint())

        if source_group != self.group_name:
            # 跨组移动
            self.cross_group_move.emit(btn_name, source_group, self.group_name, target_index)
            event.accept()
            return

        # 同组内排序
        if btn_name not in self.button_names:
            event.ignore()
            return

        current_index = self.button_names.index(btn_name)

        if target_index == current_index or target_index == current_index + 1:
            event.accept()
            return

        # 移动项目
        self.button_names.remove(btn_name)
        if target_index > current_index:
            target_index -= 1
        self.button_names.insert(target_index, btn_name)

        # 重新排列 UI
        self._reorder_items()

        # 发出顺序变更信号
        self.order_changed.emit(self.group_name, self.button_names.copy())

        event.accept()

    def _get_drop_index(self, pos) -> int:
        """根据鼠标位置计算放置索引"""
        y = pos.y()
        for i, btn_name in enumerate(self.button_names):
            if btn_name in self.items:
                item = self.items[btn_name]
                item_rect = item.geometry()
                item_center_y = item_rect.y() + item_rect.height() // 2
                if y < item_center_y:
                    return i
        return len(self.button_names)

    def _update_drop_indicator(self, pos):
        """更新放置指示器"""
        self._drop_index = self._get_drop_index(pos)
        # 高亮目标位置（可选：添加视觉反馈）
        self.update()

    def _clear_drop_indicator(self):
        """清除放置指示器"""
        self._drop_index = -1
        self.update()

    def _reorder_items(self):
        """重新排列项目"""
        # 先移除所有 widget
        for btn_name in list(self.items.keys()):
            item = self.items[btn_name]
            self._layout.removeWidget(item)

        # 按新顺序重新添加
        for btn_name in self.button_names:
            if btn_name in self.items:
                self._layout.addWidget(self.items[btn_name])

        self._update_separators()


class ToolbarManagerDialog(QDialog):
    """工具栏管理对话框"""

    config_changed = pyqtSignal(dict)  # 配置变更信号

    # 按钮定义：(内部名称, 显示名称, 默认显示, 分组)
    BUTTON_DEFINITIONS = [
        # 第一组：预设和控制
        ("preset_combo", "预设选择器", True, "预设与控制"),
        ("preset_switch_btn", "切换按钮", True, "预设与控制"),
        ("manage_preset_btn", "管理按钮", True, "预设与控制"),
        ("start_btn", "启动按钮", True, "预设与控制"),
        ("stop_btn", "停止按钮", True, "预设与控制"),

        # 第二组：选项
        ("image_prefix_checkbox", "图片加@前缀", True, "选项"),
        ("image_local_checkbox", "图片存工作目录", True, "选项"),
        ("window_nav_checkbox", "窗口快速导航", True, "选项"),

        # 第三组：操作
        ("export_btn", "导出", True, "操作"),
        ("history_btn", "历史", True, "操作"),
        ("clear_btn", "清屏", True, "操作"),

        # 第四组：分屏
        ("split_btn", "分屏", True, "分屏管理"),
        ("split_v_btn", "垂直分屏", True, "分屏管理"),
        ("close_split_btn", "关分屏", True, "分屏管理"),
        ("close_tab_btn", "关标签", True, "分屏管理"),

        # 第五组：面板
        ("explorer_toggle_btn", "资源管理器", True, "面板与编辑器"),
        ("git_toggle_btn", "Git", True, "面板与编辑器"),
        ("vscode_open_btn", "VS Code", True, "面板与编辑器"),
        ("cursor_open_btn", "Cursor", True, "面板与编辑器"),
        ("log_toggle_btn", "日志", True, "面板与编辑器"),

        # 第六组：主题
        ("theme_combo", "主题选择器", True, "主题"),
        ("icon_tint_checkbox", "染色", True, "主题"),

        # 第七组：设置
        ("llm_config_btn", "LLM配置", True, "设置"),
    ]

    # 分组定义（用于默认顺序）
    DEFAULT_GROUPS = ["预设与控制", "选项", "操作", "分屏管理", "面板与编辑器", "主题", "设置"]

    # 固定位置的分组（不可移动）
    PINNED_FIRST_GROUP = "预设与控制"
    PINNED_LAST_GROUP = "设置"

    def __init__(self, current_config: dict = None, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self.current_config = current_config or self._get_default_config()
        self.toggle_items = {}  # btn_name -> ToggleItem
        self.group_containers = {}  # group_name -> (QFrame, QVBoxLayout, [btn_names])
        self.group_order = []  # 当前分组顺序
        self._group_arrows = {}    # group_name -> (up_btn, down_btn)
        self._group_widgets = {}   # group_name -> (header_widget, container_widget)

        self.setWindowTitle(t("toolbar_mgr.title"))
        self.setMinimumSize(420, 620)
        # 禁用更新以加速 UI 构建
        self.setUpdatesEnabled(False)
        self._setup_ui()
        self._apply_theme()
        self.setUpdatesEnabled(True)

    def _get_default_config(self) -> dict:
        """获取默认配置"""
        # 默认按钮顺序
        default_order = {}
        for name, _, _, group in self.BUTTON_DEFINITIONS:
            if group not in default_order:
                default_order[group] = []
            default_order[group].append(name)

        return {
            "layout": "single",  # single 或 double
            "visible_buttons": {name: visible for name, _, visible, _ in self.BUTTON_DEFINITIONS},
            "button_order": default_order,  # 分组内按钮顺序
            "group_order": self.DEFAULT_GROUPS.copy()  # 分组顺序
        }

    def _get_button_display_name(self, btn_name: str) -> str:
        """获取按钮的显示名称"""
        for name, _, _, _ in self.BUTTON_DEFINITIONS:
            if name == btn_name:
                return t(f"toolbar_mgr.btn.{btn_name}")
        return btn_name

    def _get_effective_buttons_for_group(self, group_name: str) -> list:
        """获取某个分组的有效按钮列表（考虑跨组移动）"""
        button_groups = self.current_config.get("button_groups", {})

        # 从 BUTTON_DEFINITIONS 中获取默认属于此分组的按钮
        default_buttons = [name for name, _, _, group in self.BUTTON_DEFINITIONS
                           if group == group_name]

        # 移除已被移到其他组的按钮
        effective = [b for b in default_buttons
                     if button_groups.get(b, group_name) == group_name]

        # 添加从其他组移入的按钮
        for btn_name, target_group in button_groups.items():
            if target_group == group_name and btn_name not in effective:
                effective.append(btn_name)

        return effective

    def _get_button_order_for_group(self, group_name: str) -> list:
        """获取某个分组的按钮顺序（含兼容性处理和跨组移动支持）"""
        effective = self._get_effective_buttons_for_group(group_name)

        saved_order = self.current_config.get("button_order", {})
        if group_name not in saved_order:
            return effective

        # 合并：以 saved_order 为基础，只保留有效按钮，补充新增的
        order = [b for b in saved_order[group_name] if b in effective]
        for btn_name in effective:
            if btn_name not in order:
                order.append(btn_name)
        return order

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # 布局选择
        layout_group = QGroupBox(t("toolbar_mgr.layout_group"))
        layout_group_layout = QVBoxLayout(layout_group)
        layout_group_layout.setSpacing(12)

        # 布局选项行
        options_layout = QHBoxLayout()
        options_layout.setSpacing(20)

        self.layout_group = QButtonGroup(self)

        self.single_row_radio = QRadioButton(t("toolbar_mgr.single_row"))
        self.single_row_radio.setToolTip(t("toolbar_mgr.single_row_tooltip"))
        self.layout_group.addButton(self.single_row_radio, 0)
        options_layout.addWidget(self.single_row_radio)

        self.double_row_radio = QRadioButton(t("toolbar_mgr.double_row"))
        self.double_row_radio.setToolTip(t("toolbar_mgr.double_row_tooltip"))
        self.layout_group.addButton(self.double_row_radio, 1)
        options_layout.addWidget(self.double_row_radio)

        options_layout.addStretch()
        layout_group_layout.addLayout(options_layout)

        # 设置当前选中
        if self.current_config.get("layout") == "double":
            self.double_row_radio.setChecked(True)
        else:
            self.single_row_radio.setChecked(True)

        # 布局变更提示
        layout_note = QLabel(t("toolbar_mgr.layout_note"))
        layout_note.setObjectName("warningLabel")
        layout_group_layout.addWidget(layout_note)

        layout.addWidget(layout_group)

        # 按钮显示选择
        buttons_group = QGroupBox(t("toolbar_mgr.buttons_group"))
        buttons_layout = QVBoxLayout(buttons_group)
        buttons_layout.setContentsMargins(0, 15, 0, 10)

        # 提示文字
        hint_label = QLabel(t("toolbar_mgr.drag_hint"))
        hint_label.setObjectName("hintLabel")
        buttons_layout.addWidget(hint_label)

        # 滚动区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setSpacing(2)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)

        # 分组名称列表
        self.group_order = self.current_config.get("group_order", self.DEFAULT_GROUPS.copy())

        visible_buttons = self.current_config.get("visible_buttons", {})

        # 按保存的顺序显示分组
        for group_name in self.group_order:
            self._create_group_ui(group_name, visible_buttons)

        # 更新箭头按钮的启用/禁用状态
        self._update_arrow_states()

        self.scroll_layout.addStretch()
        scroll.setWidget(self.scroll_content)
        buttons_layout.addWidget(scroll)

        # 快捷按钮
        quick_btns_layout = QHBoxLayout()
        quick_btns_layout.setSpacing(10)
        quick_btns_layout.setContentsMargins(10, 10, 10, 0)

        select_all_btn = QPushButton(t("toolbar_mgr.show_all"))
        select_all_btn.clicked.connect(self._select_all)
        quick_btns_layout.addWidget(select_all_btn)

        deselect_all_btn = QPushButton(t("toolbar_mgr.hide_all"))
        deselect_all_btn.clicked.connect(self._deselect_all)
        quick_btns_layout.addWidget(deselect_all_btn)

        reset_btn = QPushButton(t("toolbar_mgr.reset"))
        reset_btn.clicked.connect(self._reset_to_default)
        quick_btns_layout.addWidget(reset_btn)

        quick_btns_layout.addStretch()
        buttons_layout.addLayout(quick_btns_layout)

        layout.addWidget(buttons_group, 1)

        # 底部按钮
        bottom_layout = QHBoxLayout()
        bottom_layout.setSpacing(12)
        bottom_layout.addStretch()

        cancel_btn = QPushButton(t("toolbar_mgr.cancel"))
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(self.reject)
        bottom_layout.addWidget(cancel_btn)

        apply_btn = QPushButton(t("toolbar_mgr.apply"))
        apply_btn.setFixedWidth(80)
        apply_btn.setObjectName("applyBtn")
        apply_btn.clicked.connect(self._apply_config)
        bottom_layout.addWidget(apply_btn)

        layout.addLayout(bottom_layout)

    def _create_group_ui(self, group_name: str, visible_buttons: dict):
        """创建一个分组的 UI"""
        # 分组标题行 - 包含标题和上下箭头按钮
        header_widget = QWidget()
        header_widget.setObjectName("groupHeaderWidget")
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(12, 12, 12, 4)
        header_layout.setSpacing(4)

        header_label = QLabel(t(f"toolbar_mgr.group.{group_name}"))
        header_label.setObjectName("groupHeader")
        header_label.setStyleSheet("padding: 0;")  # 移除 groupHeader 的默认 padding
        header_layout.addWidget(header_label)

        header_layout.addStretch()

        # 固定分组不显示箭头
        is_pinned = (group_name == self.PINNED_FIRST_GROUP or group_name == self.PINNED_LAST_GROUP)

        if not is_pinned:
            up_btn = QPushButton("▲")
            up_btn.setObjectName("groupArrowBtn")
            up_btn.setFixedSize(24, 20)
            up_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            up_btn.setToolTip(t("toolbar_mgr.move_up_tooltip", name=t(f"toolbar_mgr.group.{group_name}")))
            up_btn.clicked.connect(lambda checked, g=group_name: self._move_group_up(g))
            header_layout.addWidget(up_btn)

            down_btn = QPushButton("▼")
            down_btn.setObjectName("groupArrowBtn")
            down_btn.setFixedSize(24, 20)
            down_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            down_btn.setToolTip(t("toolbar_mgr.move_down_tooltip", name=t(f"toolbar_mgr.group.{group_name}")))
            down_btn.clicked.connect(lambda checked, g=group_name: self._move_group_down(g))
            header_layout.addWidget(down_btn)

            self._group_arrows[group_name] = (up_btn, down_btn)

        self.scroll_layout.addWidget(header_widget)

        # 使用支持拖拽的分组容器
        group_container = DraggableGroupContainer(group_name)
        group_container.order_changed.connect(self._on_group_order_changed)
        group_container.cross_group_move.connect(self._on_cross_group_move)

        # 禁用更新以提高批量添加性能
        group_container.setUpdatesEnabled(False)

        # 获取按钮顺序
        button_names = self._get_button_order_for_group(group_name)

        # 保存分组信息
        self.group_containers[group_name] = group_container

        for btn_name in button_names:
            display_name = self._get_button_display_name(btn_name)
            item = ToggleItem(display_name, visible_buttons.get(btn_name, True), draggable=True)
            item.setProperty("btn_name", btn_name)
            item.setProperty("group_name", group_name)

            self.toggle_items[btn_name] = item
            group_container.addItem(btn_name, item)

        # 完成添加后更新分隔线并重新启用更新
        group_container.finishAdding()
        group_container.setUpdatesEnabled(True)
        self.scroll_layout.addWidget(group_container)

        # 保存 header 和 container 的引用
        self._group_widgets[group_name] = (header_widget, group_container)

    def _on_group_order_changed(self, group_name: str, new_order: list):
        """处理分组内顺序变更"""
        # 顺序已在 DraggableGroupContainer 中维护，无需额外处理
        pass

    def _on_cross_group_move(self, btn_name: str, from_group: str, to_group: str, target_index: int):
        """处理跨组按钮移动"""
        source_container = self.group_containers.get(from_group)
        target_container = self.group_containers.get(to_group)

        if not source_container or not target_container:
            return
        if btn_name not in source_container.items:
            return

        # 从源组移除
        item = source_container.items.pop(btn_name)
        source_container.button_names.remove(btn_name)
        source_container._layout.removeWidget(item)

        # 更新 item 的分组属性（影响后续拖拽的 MIME 数据）
        item.setProperty("group_name", to_group)

        # 插入到目标组
        if target_index > len(target_container.button_names):
            target_index = len(target_container.button_names)
        target_container.button_names.insert(target_index, btn_name)
        target_container.items[btn_name] = item
        target_container._reorder_items()

    def _get_reorderable_groups(self) -> list:
        """获取可排序的分组列表（排除固定分组）"""
        return [g for g in self.group_order
                if g != self.PINNED_FIRST_GROUP and g != self.PINNED_LAST_GROUP]

    def _move_group_up(self, group_name: str):
        """在 group_order 中上移分组"""
        reorderable = self._get_reorderable_groups()
        idx = reorderable.index(group_name) if group_name in reorderable else -1
        if idx <= 0:
            return
        # 交换
        reorderable[idx], reorderable[idx - 1] = reorderable[idx - 1], reorderable[idx]
        self._rebuild_group_order(reorderable)
        self._rebuild_scroll_layout()

    def _move_group_down(self, group_name: str):
        """在 group_order 中下移分组"""
        reorderable = self._get_reorderable_groups()
        idx = reorderable.index(group_name) if group_name in reorderable else -1
        if idx < 0 or idx >= len(reorderable) - 1:
            return
        # 交换
        reorderable[idx], reorderable[idx + 1] = reorderable[idx + 1], reorderable[idx]
        self._rebuild_group_order(reorderable)
        self._rebuild_scroll_layout()

    def _rebuild_group_order(self, reorderable: list):
        """根据可排序分组列表重建完整的 group_order"""
        new_order = []
        # 固定首组
        if self.PINNED_FIRST_GROUP in self.group_order:
            new_order.append(self.PINNED_FIRST_GROUP)
        # 可排序分组
        new_order.extend(reorderable)
        # 固定末组
        if self.PINNED_LAST_GROUP in self.group_order:
            new_order.append(self.PINNED_LAST_GROUP)
        self.group_order = new_order

    def _rebuild_scroll_layout(self):
        """按新的分组顺序重排滚动区域内的 widget"""
        # 移除所有 widget（不删除）
        while self.scroll_layout.count():
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)

        # 按新顺序重新添加
        for group_name in self.group_order:
            if group_name in self._group_widgets:
                header_widget, container_widget = self._group_widgets[group_name]
                self.scroll_layout.addWidget(header_widget)
                self.scroll_layout.addWidget(container_widget)

        self.scroll_layout.addStretch()

        # 更新箭头按钮状态
        self._update_arrow_states()

    def _update_arrow_states(self):
        """更新箭头按钮的启用/禁用状态"""
        reorderable = self._get_reorderable_groups()
        for i, group_name in enumerate(reorderable):
            if group_name in self._group_arrows:
                up_btn, down_btn = self._group_arrows[group_name]
                up_btn.setEnabled(i > 0)
                down_btn.setEnabled(i < len(reorderable) - 1)

    def _apply_theme(self):
        """应用主题 - 简化样式表以提高性能"""
        bg = self.theme.get('bg_dark', '#1a1a2e')
        bg_medium = self.theme.get('bg_medium', '#232340')
        text = self.theme.get('text', '#eaeaea')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')

        self.setStyleSheet(f"""
            QDialog {{ background-color: {bg}; color: {text}; }}
            QGroupBox {{ color: {text}; border: 1px solid {border}; border-radius: 10px; margin-top: 10px; padding-top: 10px; font-weight: bold; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 15px; padding: 0 8px; }}
            QFrame#groupContainer {{ background-color: {bg_medium}; border-radius: 8px; margin: 0 8px; }}
            QLabel {{ color: {text}; }}
            QLabel#groupHeader {{ font-size: 11px; font-weight: bold; color: #888; padding: 12px 12px 4px 12px; }}
            QLabel#warningLabel {{ color: #f59e0b; font-size: 11px; }}
            QLabel#hintLabel {{ color: #888; font-size: 11px; padding: 0 12px; }}
            QRadioButton {{ color: {text}; spacing: 8px; }}
            QRadioButton::indicator {{ width: 18px; height: 18px; }}
            QRadioButton::indicator:unchecked {{ border: 2px solid #4a4a6a; border-radius: 9px; }}
            QRadioButton::indicator:checked {{ border: 2px solid {accent}; border-radius: 9px; background-color: {accent}; }}
            QPushButton {{ background-color: #3d3d5c; color: {text}; border: none; border-radius: 6px; padding: 8px 16px; }}
            QPushButton:hover {{ background-color: #4d4d6c; }}
            QPushButton#applyBtn {{ background-color: {accent}; color: white; font-weight: bold; }}
            QPushButton#groupArrowBtn {{ background-color: transparent; color: #888; border: 1px solid {border}; border-radius: 4px; padding: 0; font-size: 10px; }}
            QPushButton#groupArrowBtn:hover {{ background-color: {bg_medium}; color: {text}; border-color: {accent}; }}
            QPushButton#groupArrowBtn:disabled {{ color: #444; border-color: transparent; }}
            QScrollArea {{ border: none; background-color: transparent; }}
            QScrollBar:vertical {{ background-color: {bg}; width: 8px; }}
            QScrollBar::handle:vertical {{ background-color: #4a4a6a; border-radius: 4px; min-height: 30px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

    def _select_all(self):
        """全选"""
        for item in self.toggle_items.values():
            item.setChecked(True)

    def _deselect_all(self):
        """全不选"""
        for item in self.toggle_items.values():
            item.setChecked(False)

    def _reset_to_default(self):
        """恢复默认"""
        self.single_row_radio.setChecked(True)
        for name, _, default_visible, _ in self.BUTTON_DEFINITIONS:
            if name in self.toggle_items:
                self.toggle_items[name].setChecked(default_visible)

        # 收集所有 toggle_items（可能被跨组移动过）
        all_items = dict(self.toggle_items)

        # 清空所有容器
        for container in self.group_containers.values():
            for btn_name in list(container.items.keys()):
                container._layout.removeWidget(container.items[btn_name])
            container.button_names.clear()
            container.items.clear()

        # 按默认分组重新分配按钮
        for name, _, _, group in self.BUTTON_DEFINITIONS:
            if name in all_items and group in self.group_containers:
                item = all_items[name]
                item.setProperty("group_name", group)
                container = self.group_containers[group]
                container.button_names.append(name)
                container.items[name] = item
                container._layout.addWidget(item)

        # 恢复默认分组顺序
        self.group_order = self.DEFAULT_GROUPS.copy()
        self._rebuild_scroll_layout()

    def _collect_button_groups(self) -> dict:
        """收集按钮的自定义分组映射（仅记录与默认不同的）"""
        # 构建 btn_name -> 默认分组 的映射
        default_group_map = {name: group for name, _, _, group in self.BUTTON_DEFINITIONS}
        button_groups = {}
        for group_name, container in self.group_containers.items():
            for btn_name in container.button_names:
                default_grp = default_group_map.get(btn_name)
                if default_grp and default_grp != group_name:
                    button_groups[btn_name] = group_name
        return button_groups

    def _apply_config(self):
        """应用配置"""
        # 收集按钮顺序
        button_order = {}
        for group_name, container in self.group_containers.items():
            button_order[group_name] = container.button_names.copy()

        config = {
            "layout": "double" if self.double_row_radio.isChecked() else "single",
            "visible_buttons": {name: item.isChecked() for name, item in self.toggle_items.items()},
            "button_order": button_order,
            "group_order": self.group_order,
            "button_groups": self._collect_button_groups()
        }
        self.config_changed.emit(config)
        self.accept()

    def get_config(self) -> dict:
        """获取当前配置"""
        # 收集按钮顺序
        button_order = {}
        for group_name, container in self.group_containers.items():
            button_order[group_name] = container.button_names.copy()

        return {
            "layout": "double" if self.double_row_radio.isChecked() else "single",
            "visible_buttons": {name: item.isChecked() for name, item in self.toggle_items.items()},
            "button_order": button_order,
            "group_order": self.group_order,
            "button_groups": self._collect_button_groups()
        }

    def accept(self):
        """重写accept方法，关闭前先隐藏防止闪烁"""
        self.setWindowOpacity(0)
        super().accept()

    def reject(self):
        """重写reject方法，关闭前先隐藏防止闪烁"""
        self.setWindowOpacity(0)
        super().reject()
