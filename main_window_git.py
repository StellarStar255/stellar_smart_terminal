"""MainWindow 的 Git 侧面板混入（从 main_window.py 拆出）。面板搭建、显示/隐藏、diff/output 查看、GUI 字体应用。纯方法搬迁，行为不变。"""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from git_widget import GitPanel


class GitPanelMixin:

    def _apply_gui_font_to_git_panel(self):
        """把当前 GUI 字号应用到 Git 面板：diff 查看器 + 提交 graph（默认12pt，6-32）。

        graph 是 QPainter 手绘文字，既不吃应用级 QSS 的 font-size，也不会跟随
        setFont——必须显式按当前 GUI 字号重设（这正是它之前不联动的原因）。
        在 _apply_global_zoom（改字号时）和 Git 面板创建后各调一次，保证即时联动
        且初始也正确。
        """
        if not (hasattr(self, 'git_panel') and self.git_panel is not None):
            return
        # 只认 GUI Font：显式字号钳制到 6-32；Auto 用默认 12（缩放偏移不参与）
        if self._gui_font_size > 0:
            target = max(6, min(32, self._gui_font_size))
        else:
            target = 12
        if hasattr(self.git_panel, 'diff_text'):
            font = self.git_panel.diff_text.font()
            if font.pointSize() != target:
                font.setPointSize(target)
                self.git_panel.diff_text.setFont(font)
        if getattr(self.git_panel, 'graph_widget', None) is not None:
            self.git_panel.graph_widget.set_font_size(target)

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
        current_theme = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])

        # Git 面板内容
        self.git_panel = GitPanel(theme=current_theme)
        layout.addWidget(self.git_panel)
        # 面板默认按 12pt 初始化，但 GUI 字号可能已是别的值（启动时缩放已应用过）→
        # 创建后立刻按当前 GUI 字号重设 diff/graph，避免初始字号不联动。
        self._apply_gui_font_to_git_panel()

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
        self._reconcile_spring_after_layout_change()
        QTimer.singleShot(0, self._flush_terminal_resizes)
