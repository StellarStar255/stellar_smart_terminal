"""MainWindow 的工具栏混入（从 main_window.py 拆出）。

搭建主/浮动工具栏、固定项流式布局、工具栏配置的应用与持久化。纯方法
搬迁，行为不变；对类级 _navigator_dock_mode / _global_window_navigator /
_current_embed_enabled 的引用改用 type(self)。（部分 Qt 控件名只出现在
QSS 字符串里、非符号，不 import；_make_git_tool_icon 在方法内局部导入。）
"""
from PyQt6.QtCore import QSize, QTimer, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QToolBar, QWidget,
)
from command_palette import CommandPalette
from flow_layout import FlowLayout
from i18n import get_language, t
from toolbar_manager import ToolbarManagerDialog
from widgets import (
    CenteredComboBox, MultiKeywordCompleter, QuietPopupComboBox,
    SelectAllLineEdit, _FlowSeparator, _ToolbarCheckBox,
)


class ToolbarMixin:

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

        # 命令搜索框（Cmd+K 聚焦）。只创建不入列：它注册在「预设与控制」组的
        # 分组按钮里（默认排在 Stop 之后），位置可在工具栏布局管理器里调整
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
        if type(self)._navigator_dock_mode == 'embed':
            # 内嵌模式：跟随其它窗口是否已启用导航条（stateChanged 尚未连接，不会触发回调）
            enabled = type(self)._current_embed_enabled()
            self.nav_embed_enabled = enabled
            self.window_nav_checkbox.setChecked(enabled)
        elif type(self)._global_window_navigator is not None and type(self)._global_window_navigator.isVisible():
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

        # 矢量绘制齿轮（复用 git 面板的图标工具）：⚙ 字形在 Windows 的
        # Segoe 字体族里走回退、渲染成一截虚线，macOS 上则可能变彩色 emoji
        from git_widget import _make_git_tool_icon
        self.toolbar_settings_btn = QPushButton("")
        self.toolbar_settings_btn.setIcon(_make_git_tool_icon('gear', '#eaeaea', 16))
        self.toolbar_settings_btn.setObjectName("toolbarSettingsBtn")
        self.toolbar_settings_btn.setToolTip(t("toolbar.settings_tooltip"))
        self.toolbar_settings_btn.setFixedSize(32, 32)
        self.toolbar_settings_btn.setStyleSheet("""
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
        """)
        self.toolbar_settings_btn.clicked.connect(self._show_settings_popup_menu)

        # ===== 定义每组的按钮和默认顺序 =====
        self._group_button_dicts = {
            # 搜索框走分组按钮体系（而非核心控件）：默认紧跟 Stop 之后，
            # 可在工具栏布局管理器里改顺序/移到其他组/隐藏
            "预设与控制": {
                "command_palette": self.command_palette,
            },
            "选项": {
                "image_prefix_checkbox": self.image_prefix_checkbox,
                "image_local_checkbox": self.image_local_checkbox,
                "window_nav_checkbox": self.window_nav_checkbox,
            },
            "操作": {
                "export_btn": self.export_btn,
                "history_btn": self.history_btn,
                "clear_btn": self.clear_btn,
                "images_btn": self.images_btn,
            },
            "分屏管理": {
                "split_btn": self.split_btn,
                "split_v_btn": self.split_v_btn,
                "close_split_btn": self.close_split_btn,
                "close_tab_btn": self.close_tab_btn,
            },
            "面板": {
                "explorer_toggle_btn": self.explorer_toggle_btn,
                "git_toggle_btn": self.git_toggle_btn,
                "remote_toggle_btn": self.remote_toggle_btn,
            },
            "面板与编辑器": {
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
            "预设与控制": ["command_palette"],
            "选项": ["image_prefix_checkbox", "image_local_checkbox", "window_nav_checkbox"],
            "操作": ["export_btn", "history_btn", "clear_btn", "images_btn"],
            "分屏管理": ["split_btn", "split_v_btn", "close_split_btn", "close_tab_btn"],
            "面板": ["explorer_toggle_btn", "git_toggle_btn", "remote_toggle_btn"],
            "面板与编辑器": ["vscode_open_btn", "cursor_open_btn", "log_toggle_btn"],
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
            "command_palette": self.command_palette,
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
        # 左右对齐 unpin QToolBar 的 12px padding（需减去 QToolBar/QWidgetAction
        # 内部约 3px 的隐式边距）
        self._flow_layout = FlowLayout(self._pinned_flow_widget, h_spacing=5, v_spacing=5)
        self._flow_layout.setContentsMargins(9, 2, 9, 2)
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
                padding: 4px 12px;
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
        # 目录历史多关键词补全：输入 "llm train" 即过滤出同时含两词的历史目录
        # （与文件 quick-open 一致），不必记住完整前缀。候选在 _populate_working_dirs
        # 里随历史更新；用整条 history（不止下拉里显示的前 N 条）做候选。
        self._dir_completer = MultiKeywordCompleter(parent=self)
        self._dir_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._dir_completer.popup().setStyleSheet("""
            QListView {
                background-color: #1a1a2e;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                outline: none;
            }
            QListView::item { padding: 3px 6px; }
            QListView::item:selected { background-color: #667eea; color: white; }
        """)
        self.working_dir_combo.setCompleter(self._dir_completer)
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
        t = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])
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

    def _show_toolbar_manager(self):
        """显示工具栏管理对话框"""
        current_theme = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])
        dialog = ToolbarManagerDialog(self.toolbar_config, current_theme, self)
        dialog.config_changed.connect(self._on_toolbar_config_changed)
        dialog.exec()

    def _on_toolbar_config_changed(self, config: dict):
        """工具栏配置变更回调"""
        # 对话框重新保存的配置视为用户显式摆放：盖上当前顺序版本号，
        # 避免下次启动时一次性迁移又把 remote/images 挪回锚点位置
        from toolbar_manager import TOOLBAR_ORDER_VERSION
        config["order_version"] = TOOLBAR_ORDER_VERSION
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
