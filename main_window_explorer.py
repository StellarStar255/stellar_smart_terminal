"""MainWindow 的 Explorer（文件浏览）侧面板混入（从 main_window.py 拆出）。面板搭建、显示/隐藏、设置菜单、收藏夹。纯方法搬迁，行为不变。分屏布局管道方法（_capture/_place/_resolve/_on_split_orientation）留在主类，随 tab/split 一并处理。"""
import explorer_favorites
import os
from PyQt6.QtCore import QSize, QTimer, Qt
from PyQt6.QtGui import QActionGroup
from PyQt6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSplitter, QVBoxLayout
from explorer_widget import ExplorerPanel
from file_editor import EditorArea
from git_widget import _make_git_tool_icon
from i18n import t


class ExplorerPanelMixin:

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
            self._explorer_split_horizontal = True  # 默认左右并排（side by side）
        self._explorer_split_checkbox.stateChanged.connect(self._on_explorer_split_orientation_changed)
        explorer_header_layout.addWidget(self._explorer_split_checkbox)

        # 弹簧模式 checkbox：编辑器与终端左右并排时，点哪边哪边自动展宽
        self._spring_checkbox = QCheckBox(t("explorer.spring_mode"))
        self._spring_checkbox.setToolTip(t("explorer.spring_tooltip"))
        self._spring_checkbox.setStyleSheet(self._explorer_split_checkbox.styleSheet())
        self._spring_checkbox.setChecked(bool(getattr(self, '_spring_mode_enabled', False)))
        self._spring_checkbox.stateChanged.connect(self._on_spring_mode_toggled)
        explorer_header_layout.addWidget(self._spring_checkbox)

        # 快捷方式（收藏）★ 按钮：下拉列出收藏，文件夹点了切换目录、文件点了打开
        self._explorer_favorites_btn = QPushButton()
        self._explorer_favorites_btn.setFixedSize(24, 24)
        self._explorer_favorites_btn.setIconSize(QSize(16, 16))
        self._explorer_favorites_btn.setIcon(_make_git_tool_icon('star', '#888'))
        self._explorer_favorites_btn.setToolTip(t("explorer.favorites_tooltip"))
        self._explorer_favorites_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.08);
                border-radius: 4px;
            }
        """)
        self._explorer_favorites_btn.clicked.connect(self._show_explorer_favorites_menu)
        explorer_header_layout.addWidget(self._explorer_favorites_btn)

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
        refresh_btn.clicked.connect(self._refresh_explorer_panel)
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
        current_theme = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])

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
        # 快捷方式增删后刷新 ★ 按钮提示（菜单本身每次打开都重读，故无需更多同步）
        self.explorer_panel.favorites_changed.connect(self._on_explorer_favorites_changed)

        # 内置文件编辑器（编辑器组：支持无限层级 split / v-split 并排查看不同文件）
        self.editor_area = EditorArea(theme=current_theme)
        self.editor_area.all_closed.connect(self._on_editor_closed)
        self.editor_area.active_changed.connect(self._on_active_pane_changed)
        self.editor_area.ai_completion_toggled.connect(self._on_ai_completion_toggled)
        self.editor_area.word_wrap_toggled.connect(self._on_word_wrap_toggled)
        # 应用已记忆的 AI 补全开关到编辑器
        self.editor_area.set_ai_completion_enabled(getattr(self, '_ai_completion_enabled', False))
        # 应用已记忆的自动换行开关到编辑器
        self.editor_area.set_word_wrap_enabled(getattr(self, '_editor_word_wrap', False))
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

    def _on_explorer_favorites_changed(self):
        """快捷方式增删后的钩子：★ 下拉每次打开都重读，这里仅刷新提示气泡。"""
        if hasattr(self, '_explorer_favorites_btn'):
            n = len(explorer_favorites.list_all())
            tip = t("explorer.favorites_tooltip")
            self._explorer_favorites_btn.setToolTip(
                f"{tip} ({n})" if n else tip)

    def _show_explorer_favorites_menu(self):
        """Explorer ★ 按钮：下拉列出所有快捷方式。

        文件夹 → 切换工作目录（Explorer 根目录随之移动，且不会被后续 cwd 同步撤销）；
        文件 → 在内置编辑器打开。底部可一键清空。
        """
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
            QMenu::item:disabled {
                color: #666;
            }
            QMenu::separator {
                height: 1px;
                background-color: #3d3d5c;
                margin: 4px 10px;
            }
        """)

        entries = explorer_favorites.list_all()
        if not entries:
            empty = menu.addAction(t("explorer.favorites_empty"))
            empty.setEnabled(False)
        else:
            for p in entries:
                is_dir = os.path.isdir(p)
                missing = not os.path.exists(p)
                base = os.path.basename(p.rstrip(os.sep)) or p
                label = base + (os.sep if is_dir else "")
                if missing:
                    label += "  (?)"
                act = menu.addAction(label)
                act.setToolTip(p)
                act.triggered.connect(
                    lambda checked=False, path=p: self._open_explorer_favorite(path))
            menu.addSeparator()
            clear_act = menu.addAction(t("explorer.favorites_clear"))
            clear_act.triggered.connect(self._clear_explorer_favorites)

        menu.exec(self._explorer_favorites_btn.mapToGlobal(
            self._explorer_favorites_btn.rect().bottomLeft()
        ))

    def _open_explorer_favorite(self, path: str):
        """打开一个快捷方式：文件夹切换工作目录，文件在编辑器打开。"""
        if not os.path.exists(path):
            # 失效（被移动/删除）：移除并提示，避免留下死链
            explorer_favorites.remove(path)
            self._on_explorer_favorites_changed()
            self.statusbar.showMessage(
                t("explorer.favorite_removed_missing", path=path), 4000)
            return
        if os.path.isdir(path):
            # 复用工作目录切换逻辑：Explorer 根目录、Git 仓库、终端 cwd 一并对齐
            self.working_dir_combo.setCurrentText(path)
            self._apply_working_dir()
        else:
            self._open_file_in_editor(path)

    def _clear_explorer_favorites(self):
        """清空全部快捷方式。"""
        explorer_favorites.clear()
        self._on_explorer_favorites_changed()

    def _refresh_explorer_panel(self):
        """刷新文件浏览器（等价于 Explorer 头部的 ↻ 按钮，绑定 Cmd+R）。"""
        if hasattr(self, 'explorer_panel'):
            self.explorer_panel.refresh()

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
        self._reconcile_spring_after_layout_change()
        QTimer.singleShot(0, self._flush_terminal_resizes)
