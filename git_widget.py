"""
Git 面板 UI 组件
提供类似 Cursor IDE 的 Git 管理界面
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QComboBox, QScrollArea, QListWidget,
    QListWidgetItem, QPlainTextEdit, QSizePolicy,
    QAbstractItemView, QMessageBox, QDialog, QTextEdit, QSplitter
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QThread
from PyQt6.QtGui import QFont, QColor

from git_manager import GitManager, GitFile, FileStatus
from i18n import t, get_language


# 文件状态颜色
STATUS_COLORS = {
    FileStatus.MODIFIED: '#e5c07b',     # 黄色
    FileStatus.ADDED: '#98c379',        # 绿色
    FileStatus.DELETED: '#e06c75',      # 红色
    FileStatus.RENAMED: '#61afef',      # 蓝色
    FileStatus.COPIED: '#61afef',       # 蓝色
    FileStatus.UNTRACKED: '#98c379',    # 绿色
    FileStatus.UNMERGED: '#e06c75',     # 红色
}

# 文件状态图标
STATUS_ICONS = {
    FileStatus.MODIFIED: 'M',
    FileStatus.ADDED: 'A',
    FileStatus.DELETED: 'D',
    FileStatus.RENAMED: 'R',
    FileStatus.COPIED: 'C',
    FileStatus.UNTRACKED: 'U',
    FileStatus.UNMERGED: '!',
}


class GitFileItem(QWidget):
    """Git 文件列表项"""

    # 信号
    stage_clicked = pyqtSignal(str)         # 暂存按钮点击
    unstage_clicked = pyqtSignal(str)       # 取消暂存按钮点击
    discard_clicked = pyqtSignal(str)       # 放弃更改按钮点击
    diff_clicked = pyqtSignal(str, bool)    # 查看 diff（路径, 是否暂存区）

    def __init__(self, git_file: GitFile, is_staged: bool = False, theme: dict = None, parent=None):
        super().__init__(parent)
        self.git_file = git_file
        self.is_staged = is_staged
        self.theme = theme or {}

        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # 状态标签
        status_icon = STATUS_ICONS.get(self.git_file.status, '?')
        status_color = STATUS_COLORS.get(self.git_file.status, '#888')

        self.status_label = QLabel(status_icon)
        self.status_label.setFixedWidth(20)
        self.status_label.setStyleSheet(f"""
            QLabel {{
                color: {status_color};
                font-weight: bold;
                font-family: monospace;
            }}
        """)
        layout.addWidget(self.status_label)

        # 文件名
        self.filename_label = QLabel(self.git_file.path)
        self.filename_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
            }}
        """)
        layout.addWidget(self.filename_label, 1)

        # 操作按钮容器
        self.btn_container = QWidget()
        btn_layout = QHBoxLayout(self.btn_container)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(4)

        btn_style = f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 3px;
                padding: 2px 6px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """

        if self.is_staged:
            # 取消暂存按钮
            self.unstage_btn = QPushButton("-")
            self.unstage_btn.setToolTip(t("git.unstage_tooltip"))
            self.unstage_btn.setFixedSize(24, 20)
            self.unstage_btn.setStyleSheet(btn_style)
            self.unstage_btn.clicked.connect(lambda: self.unstage_clicked.emit(self.git_file.path))
            btn_layout.addWidget(self.unstage_btn)
        else:
            # 暂存按钮
            self.stage_btn = QPushButton("+")
            self.stage_btn.setToolTip(t("git.stage_tooltip"))
            self.stage_btn.setFixedSize(24, 20)
            self.stage_btn.setStyleSheet(btn_style)
            self.stage_btn.clicked.connect(lambda: self.stage_clicked.emit(self.git_file.path))
            btn_layout.addWidget(self.stage_btn)

            # 放弃更改按钮
            self.discard_btn = QPushButton("x")
            self.discard_btn.setToolTip(t("git.discard_tooltip"))
            self.discard_btn.setFixedSize(24, 20)
            discard_style = f"""
                QPushButton {{
                    background-color: {self.theme.get('danger', '#ef4444')};
                    color: white;
                    border: none;
                    border-radius: 3px;
                    padding: 2px 6px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: {self.theme.get('danger_hover', '#dc2626')};
                }}
            """
            self.discard_btn.setStyleSheet(discard_style)
            self.discard_btn.clicked.connect(lambda: self.discard_clicked.emit(self.git_file.path))
            btn_layout.addWidget(self.discard_btn)

        # 初始隐藏按钮
        self.btn_container.setVisible(False)
        layout.addWidget(self.btn_container)

        # 设置鼠标跟踪
        self.setMouseTracking(True)

    def enterEvent(self, event):
        """鼠标进入"""
        self.btn_container.setVisible(True)
        bg_color = self.theme.get('bg_hover', '#4d4d6c')
        self.setStyleSheet(f"background-color: {bg_color};")
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开"""
        self.btn_container.setVisible(False)
        self.setStyleSheet("")
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击查看 diff"""
        self.diff_clicked.emit(self.git_file.path, self.is_staged)
        super().mouseDoubleClickEvent(event)


class CollapsibleSection(QWidget):
    """可折叠的分区"""

    def __init__(self, title: str, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._is_expanded = True

        self._setup_ui(title)

    def _setup_ui(self, title: str):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        self.header = QFrame()
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(8, 6, 8, 6)

        # 展开/折叠图标
        self.toggle_icon = QLabel("▼")
        self.toggle_icon.setFixedWidth(16)
        header_layout.addWidget(self.toggle_icon)

        # 标题
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
                font-weight: bold;
                font-size: 12px;
            }}
        """)
        header_layout.addWidget(self.title_label)

        header_layout.addStretch()

        # 操作按钮容器
        self.action_container = QWidget()
        self.action_layout = QHBoxLayout(self.action_container)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(4)
        header_layout.addWidget(self.action_container)

        layout.addWidget(self.header)

        # 内容区域
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        layout.addWidget(self.content)

        # 点击标题栏折叠/展开
        self.header.mousePressEvent = self._toggle

        self._update_style()

    def _toggle(self, event=None):
        """切换折叠状态"""
        self._is_expanded = not self._is_expanded
        self.content.setVisible(self._is_expanded)
        self.toggle_icon.setText("▼" if self._is_expanded else "▶")

    def _update_style(self):
        """更新样式"""
        self.header.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)
        self.toggle_icon.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text_dim', '#888')};
            }}
        """)

    def set_title(self, title: str):
        """设置标题"""
        self.title_label.setText(title)

    def add_action_button(self, text: str, tooltip: str = "") -> QPushButton:
        """添加操作按钮"""
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setFixedSize(24, 20)
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 3px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """)
        self.action_layout.addWidget(btn)
        return btn

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()


class GitChangesWidget(QScrollArea):
    """Git 变更列表组件"""

    # 信号
    stage_file = pyqtSignal(str)
    unstage_file = pyqtSignal(str)
    discard_file = pyqtSignal(str)
    stage_all = pyqtSignal()
    unstage_all = pyqtSignal()
    view_diff = pyqtSignal(str, bool)

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
        """)

        # 内容容器
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 已暂存区
        self.staged_section = CollapsibleSection(t("git.staged_changes", n=0), self.theme)
        self.unstage_all_btn = self.staged_section.add_action_button("-", t("git.unstage_all_tooltip"))
        self.unstage_all_btn.clicked.connect(self.unstage_all.emit)

        self.staged_list = QWidget()
        self.staged_layout = QVBoxLayout(self.staged_list)
        self.staged_layout.setContentsMargins(0, 0, 0, 0)
        self.staged_layout.setSpacing(0)
        self.staged_section.content_layout.addWidget(self.staged_list)
        layout.addWidget(self.staged_section)

        # 未暂存区
        self.unstaged_section = CollapsibleSection(t("git.changes", n=0), self.theme)
        self.stage_all_btn = self.unstaged_section.add_action_button("+", t("git.stage_all_tooltip"))
        self.stage_all_btn.clicked.connect(self.stage_all.emit)

        self.unstaged_list = QWidget()
        self.unstaged_layout = QVBoxLayout(self.unstaged_list)
        self.unstaged_layout.setContentsMargins(0, 0, 0, 0)
        self.unstaged_layout.setSpacing(0)
        self.unstaged_section.content_layout.addWidget(self.unstaged_list)
        layout.addWidget(self.unstaged_section)

        layout.addStretch()
        self.setWidget(content)

    def update_files(self, staged: list, unstaged: list):
        """更新文件列表"""
        # 清空现有列表
        self._clear_layout(self.staged_layout)
        self._clear_layout(self.unstaged_layout)

        # 更新已暂存文件
        self.staged_section.set_title(t("git.staged_changes", n=len(staged)))
        for git_file in staged:
            item = GitFileItem(git_file, is_staged=True, theme=self.theme)
            item.unstage_clicked.connect(self.unstage_file.emit)
            item.diff_clicked.connect(self.view_diff.emit)
            self.staged_layout.addWidget(item)

        # 更新未暂存文件
        self.unstaged_section.set_title(t("git.changes", n=len(unstaged)))
        for git_file in unstaged:
            item = GitFileItem(git_file, is_staged=False, theme=self.theme)
            item.stage_clicked.connect(self.stage_file.emit)
            item.discard_clicked.connect(self.discard_file.emit)
            item.diff_clicked.connect(self.view_diff.emit)
            self.unstaged_layout.addWidget(item)

    def _clear_layout(self, layout):
        """清空布局中的所有组件"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: {theme.get('bg_dark', '#1a1a2e')};
            }}
        """)
        self.staged_section.apply_theme(theme)
        self.unstaged_section.apply_theme(theme)

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.staged_section.set_title(t("git.staged_changes", n=self.staged_layout.count()))
        self.unstage_all_btn.setToolTip(t("git.unstage_all_tooltip"))
        self.unstaged_section.set_title(t("git.changes", n=self.unstaged_layout.count()))
        self.stage_all_btn.setToolTip(t("git.stage_all_tooltip"))


class _CommitMessageWorker(QThread):
    """后台线程：调用 OpenAI 兼容的 /chat/completions，根据 diff 生成提交信息。

    放到独立线程里跑，避免网络请求阻塞 UI。结果通过信号回到 UI 线程。
    """
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, config: dict, diff: str, lang: str, parent=None):
        super().__init__(parent)
        self._config = config or {}
        self._diff = diff
        self._lang = lang

    def run(self):
        try:
            import requests
        except Exception as e:  # pragma: no cover - requests 是已有依赖
            self.failed.emit(str(e))
            return

        cfg = self._config
        base = (cfg.get('api_base') or 'https://api.openai.com/v1').rstrip('/')
        url = base + '/chat/completions'

        headers = {'Content-Type': 'application/json'}
        key = (cfg.get('api_key') or '').strip()
        if key:
            headers['Authorization'] = f'Bearer {key}'

        lang_line = ('请用简体中文写提交信息。' if self._lang == 'zh'
                     else 'Write the commit message in English.')
        system = (
            "You are an expert software engineer writing a git commit message. "
            "Follow the Conventional Commits style (feat:, fix:, refactor:, docs:, "
            "style:, test:, chore:). Keep the subject line under 72 characters. "
            "If the change is non-trivial, add a blank line then a short body with "
            "bullet points describing what changed and why. " + lang_line +
            " Respond with ONLY the commit message text — no markdown code fences, "
            "no preamble, no quotes, no explanations."
        )
        user = ("Here are the repository changes. Write a single commit message "
                "for them:\n\n" + self._diff)

        payload = {
            'model': cfg.get('model') or 'gpt-4',
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'temperature': 0.3,
            'stream': False,
        }
        mt = cfg.get('max_tokens')
        if isinstance(mt, int) and mt > 0:
            payload['max_tokens'] = min(mt, 1024)  # 提交信息不需要太长

        proxies = None
        proxy = (cfg.get('proxy') or '').strip()
        if proxy:
            proxies = {'http': proxy, 'https': proxy}

        timeout = cfg.get('timeout') or 30

        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=timeout, proxies=proxies)
        except Exception as e:
            self.failed.emit(str(e))
            return

        if resp.status_code != 200:
            snippet = (resp.text or '')[:300]
            self.failed.emit(f"HTTP {resp.status_code}: {snippet}")
            return

        try:
            data = resp.json()
            content = data['choices'][0]['message']['content']
        except Exception as e:
            self.failed.emit(f"unexpected response: {e}")
            return

        self.succeeded.emit(self._clean(content))

    @staticmethod
    def _clean(text: str) -> str:
        """清洗模型输出：去掉推理模型的 <think>/<thinking> 块、``` 代码围栏和首尾空白。"""
        import re
        text = text or ''
        # 1) 去掉成对的推理块（DeepSeek/Qwen 等推理模型会内联在 content 里）
        text = re.sub(r'(?is)<think(?:ing)?>.*?</think(?:ing)?>', '', text)
        # 2) 有时只回传了闭合标签（推理在前、答案在后）→ 取最后一个闭合标签之后的内容
        for tag in ('</think>', '</thinking>'):
            if tag in text:
                text = text.rsplit(tag, 1)[-1]
        # 3) 残留的未闭合开标签
        text = re.sub(r'(?is)<think(?:ing)?>', '', text)
        text = text.strip()
        # 4) 去掉 ``` 代码围栏
        if text.startswith('```'):
            lines = text.splitlines()
            if lines and lines[0].startswith('```'):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith('```'):
                lines = lines[:-1]
            text = '\n'.join(lines).strip()
        return text


class GitCommitWidget(QFrame):
    """Git 提交区组件"""

    # 信号
    commit_requested = pyqtSignal(str)
    push_requested = pyqtSignal()
    pull_requested = pyqtSignal()
    generate_requested = pyqtSignal()

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # 提交信息输入框（高度可随上方分隔条拖拽变化）
        self.message_input = QPlainTextEdit()
        self.message_input.setPlaceholderText(t("git.commit_placeholder"))
        self.message_input.setMinimumHeight(70)
        self.message_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.message_input.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 6px;
                font-size: 13px;
            }}
            QPlainTextEdit:focus {{
                border-color: {self.theme.get('accent', '#667eea')};
            }}
        """)
        layout.addWidget(self.message_input)

        # ✨ 用大模型生成提交信息
        self.generate_btn = QPushButton(t("git.generate_msg"))
        self.generate_btn.setToolTip(t("git.generate_msg_tooltip"))
        self.generate_btn.setStyleSheet(self._generate_btn_style(self.theme))
        self.generate_btn.clicked.connect(self._on_generate)
        layout.addWidget(self.generate_btn)

        # 提交按钮
        self.commit_btn = QPushButton("Commit")
        self.commit_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('accent', '#667eea')};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('accent_hover', '#7a8efa')};
            }}
            QPushButton:pressed {{
                background-color: {self.theme.get('accent_pressed', '#5a6fd6')};
            }}
            QPushButton:disabled {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text_dim', '#888')};
            }}
        """)
        self.commit_btn.clicked.connect(self._on_commit)
        layout.addWidget(self.commit_btn)

        # Push/Pull 按钮行
        sync_layout = QHBoxLayout()
        sync_layout.setSpacing(8)

        # Pull 按钮
        self.pull_btn = QPushButton("↓ Pull")
        self.pull_btn.setToolTip(t("git.pull_tooltip"))
        self.pull_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """)
        self.pull_btn.clicked.connect(self.pull_requested.emit)
        sync_layout.addWidget(self.pull_btn)

        # Push 按钮
        self.push_btn = QPushButton("↑ Push")
        self.push_btn.setToolTip(t("git.push_tooltip"))
        self.push_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('success', '#4ade80')};
                color: #000;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('success_hover', '#22c55e')};
            }}
        """)
        self.push_btn.clicked.connect(self.push_requested.emit)
        sync_layout.addWidget(self.push_btn)

        layout.addLayout(sync_layout)

        self._update_style()

    def _update_style(self):
        """更新样式"""
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
                border-top: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)

    def _on_commit(self):
        """提交按钮点击"""
        message = self.message_input.toPlainText().strip()
        if message:
            self.commit_requested.emit(message)
            self.message_input.clear()

    def _on_generate(self):
        """生成提交信息按钮点击 —— 由 GitPanel 接管（它持有 GitManager 和 LLM 配置）"""
        self.generate_requested.emit()

    def set_generating(self, generating: bool):
        """切换生成中状态：禁用按钮并改文案，避免重复点击。"""
        self.generate_btn.setEnabled(not generating)
        self.generate_btn.setText(t("git.generating") if generating else t("git.generate_msg"))

    def set_message(self, text: str):
        """把生成的提交信息填进输入框。"""
        self.message_input.setPlainText(text)
        self.message_input.setFocus()

    @staticmethod
    def _generate_btn_style(theme: dict) -> str:
        return f"""
            QPushButton {{
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #8b5cf6;
            }}
            QPushButton:pressed {{
                background-color: #6d28d9;
            }}
            QPushButton:disabled {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text_dim', '#888')};
            }}
        """

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()

        self.message_input.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {theme.get('bg_medium', '#16213e')};
                color: {theme.get('text', '#eaeaea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 6px;
                font-size: 13px;
            }}
            QPlainTextEdit:focus {{
                border-color: {theme.get('accent', '#667eea')};
            }}
        """)

        self.commit_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('accent', '#667eea')};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {theme.get('accent_hover', '#7a8efa')};
            }}
            QPushButton:pressed {{
                background-color: {theme.get('accent_pressed', '#5a6fd6')};
            }}
            QPushButton:disabled {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text_dim', '#888')};
            }}
        """)

        self.pull_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

        self.push_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('success', '#4ade80')};
                color: #000;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {theme.get('success_hover', '#22c55e')};
            }}
        """)

        self.generate_btn.setStyleSheet(self._generate_btn_style(theme))

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.message_input.setPlaceholderText(t("git.commit_placeholder"))
        self.pull_btn.setToolTip(t("git.pull_tooltip"))
        self.push_btn.setToolTip(t("git.push_tooltip"))
        self.generate_btn.setText(t("git.generate_msg"))
        self.generate_btn.setToolTip(t("git.generate_msg_tooltip"))


class GitDiffDialog(QDialog):
    """Diff 查看对话框"""

    def __init__(self, title: str, diff_content: str, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self.setWindowTitle(title)
        self.setMinimumSize(600, 400)

        self._setup_ui(diff_content)

    def _setup_ui(self, diff_content: str):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Diff 显示区域
        self.diff_text = QTextEdit()
        self.diff_text.setReadOnly(True)
        self.diff_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.diff_text.setFont(QFont("Menlo", 12))

        # 设置 diff 内容（带语法高亮）
        self._set_diff_content(diff_content)

        self.diff_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
            }}
        """)
        layout.addWidget(self.diff_text)

        # 关闭按钮
        close_btn = QPushButton(t("git.close"))
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                margin: 8px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
        """)

    def _set_diff_content(self, diff_content: str):
        """设置 diff 内容（带简单的颜色高亮）"""
        html_lines = []

        for line in diff_content.splitlines():
            escaped_line = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            if line.startswith('+') and not line.startswith('+++'):
                color = '#98c379'  # 绿色 - 新增行
            elif line.startswith('-') and not line.startswith('---'):
                color = '#e06c75'  # 红色 - 删除行
            elif line.startswith('@@'):
                color = '#61afef'  # 蓝色 - 位置信息
            elif line.startswith('diff ') or line.startswith('index '):
                color = '#c678dd'  # 紫色 - 头部信息
            else:
                color = self.theme.get('text', '#eaeaea')

            html_lines.append(f'<span style="color: {color};">{escaped_line}</span>')

        self.diff_text.setHtml('<pre style="margin: 8px;">' + '<br>'.join(html_lines) + '</pre>')


class GitHeaderWidget(QFrame):
    """Git 面板头部"""

    # 信号
    branch_changed = pyqtSignal(str)
    refresh_clicked = pyqtSignal()

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)

        # 标题
        self.title_label = QLabel(t("git.source_control"))
        self.title_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
                font-weight: bold;
                font-size: 13px;
            }}
        """)
        layout.addWidget(self.title_label)

        layout.addStretch()

        # 分支选择器
        self.branch_combo = QComboBox()
        self.branch_combo.setMinimumWidth(120)
        self.branch_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QComboBox:hover {{
                border-color: {self.theme.get('accent', '#667eea')};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                selection-background-color: {self.theme.get('accent', '#667eea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)
        self.branch_combo.currentTextChanged.connect(self.branch_changed.emit)
        layout.addWidget(self.branch_combo)

        # 刷新按钮
        self.refresh_btn = QPushButton("⟳")
        self.refresh_btn.setToolTip(t("git.refresh_tooltip"))
        self.refresh_btn.setFixedSize(28, 28)
        self.refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                font-size: 16px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """)
        self.refresh_btn.clicked.connect(self.refresh_clicked.emit)
        layout.addWidget(self.refresh_btn)

        self._update_style()

    def _update_style(self):
        """更新样式"""
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)

    def update_branches(self, branches: list, current_branch: str):
        """更新分支列表"""
        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()

        for branch in branches:
            if not branch.is_remote:
                self.branch_combo.addItem(branch.name)

        # 选中当前分支
        index = self.branch_combo.findText(current_branch)
        if index >= 0:
            self.branch_combo.setCurrentIndex(index)

        self.branch_combo.blockSignals(False)

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()

        self.branch_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {theme.get('bg_medium', '#16213e')};
                color: {theme.get('text', '#eaeaea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QComboBox:hover {{
                border-color: {theme.get('accent', '#667eea')};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {theme.get('bg_medium', '#16213e')};
                color: {theme.get('text', '#eaeaea')};
                selection-background-color: {theme.get('accent', '#667eea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)

        self.refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                font-size: 16px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.title_label.setText(t("git.source_control"))
        self.refresh_btn.setToolTip(t("git.refresh_tooltip"))


class GitPanel(QWidget):
    """Git 管理面板"""

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._git_manager = GitManager(self)

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 头部
        self.header = GitHeaderWidget(self.theme)
        layout.addWidget(self.header)

        # 变更列表 + 提交区放进一个竖直分隔器：拖拽中间的分隔条即可上下调整
        # 二者高度（把提交信息框拉大/拉小）。
        self.body_splitter = QSplitter(Qt.Orientation.Vertical)
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.setHandleWidth(6)

        self.changes_widget = GitChangesWidget(self.theme)
        self.body_splitter.addWidget(self.changes_widget)

        self.commit_widget = GitCommitWidget(self.theme)
        self.body_splitter.addWidget(self.commit_widget)

        # 变更列表吃掉多余空间，提交区默认停在它的自然高度
        self.body_splitter.setStretchFactor(0, 1)
        self.body_splitter.setStretchFactor(1, 0)
        self.body_splitter.setSizes([320, 180])

        layout.addWidget(self.body_splitter, 1)

        # 无仓库提示
        self.no_repo_label = QLabel(t("git.no_repo"))
        self.no_repo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_repo_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text_dim', '#888')};
                font-size: 13px;
                padding: 20px;
            }}
        """)
        self.no_repo_label.hide()
        layout.addWidget(self.no_repo_label)

        self._update_style()

    def _update_style(self):
        """更新样式"""
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
            QSplitter::handle:vertical {{
                background-color: {self.theme.get('border', '#3d3d5c')};
                margin: 0 8px;
                border-radius: 2px;
            }}
            QSplitter::handle:vertical:hover {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
        """)

    def _connect_signals(self):
        """连接信号"""
        # Git 管理器信号
        self._git_manager.status_changed.connect(self._refresh_status)
        self._git_manager.error_occurred.connect(self._show_error)

        # 头部信号
        self.header.branch_changed.connect(self._on_branch_changed)
        self.header.refresh_clicked.connect(self._refresh_status)

        # 变更列表信号
        self.changes_widget.stage_file.connect(self._git_manager.stage_file)
        self.changes_widget.unstage_file.connect(self._git_manager.unstage_file)
        self.changes_widget.discard_file.connect(self._on_discard_file)
        self.changes_widget.stage_all.connect(self._git_manager.stage_all)
        self.changes_widget.unstage_all.connect(self._git_manager.unstage_all)
        self.changes_widget.view_diff.connect(self._show_diff)

        # 提交信号
        self.commit_widget.commit_requested.connect(self._on_commit)
        self.commit_widget.push_requested.connect(self._on_push)
        self.commit_widget.pull_requested.connect(self._on_pull)
        self.commit_widget.generate_requested.connect(self._on_generate_message)

    def set_repository(self, path: str):
        """设置仓库路径"""
        is_repo = self._git_manager.set_repository(path)

        if is_repo:
            self.no_repo_label.hide()
            self.header.show()
            self.changes_widget.show()
            self.commit_widget.show()
            self._refresh_status()
            self._refresh_branches()
        else:
            self.no_repo_label.show()
            self.header.hide()
            self.changes_widget.hide()
            self.commit_widget.hide()

    def _refresh_status(self):
        """刷新文件状态"""
        staged, unstaged = self._git_manager.get_status()
        self.changes_widget.update_files(staged, unstaged)

    def _refresh_branches(self):
        """刷新分支列表"""
        branches = self._git_manager.get_branches()
        current = self._git_manager.get_current_branch()
        self.header.update_branches(branches, current)

    def _on_branch_changed(self, branch_name: str):
        """分支切换处理"""
        current = self._git_manager.get_current_branch()
        if branch_name != current:
            self._git_manager.checkout_branch(branch_name)
            self._refresh_branches()

    def _on_discard_file(self, path: str):
        """放弃更改确认"""
        reply = QMessageBox.question(
            self,
            t("git.confirm_discard_title"),
            t("git.confirm_discard_msg", path=path),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._git_manager.discard_changes(path)

    def _on_commit(self, message: str):
        """提交处理"""
        if self._git_manager.commit(message):
            self._refresh_status()

    def _on_push(self):
        """推送处理"""
        if self._git_manager.push():
            QMessageBox.information(self, t("git.push_success_title"), t("git.push_success_msg"))

    def _on_pull(self):
        """拉取处理"""
        if self._git_manager.pull():
            QMessageBox.information(self, t("git.pull_success_title"), t("git.pull_success_msg"))
            self._refresh_status()

    # ---------- ✨ 用大模型生成提交信息 ----------

    def _on_generate_message(self):
        """根据当前改动调用大模型生成提交信息，填进输入框。"""
        diff = self._collect_diff_for_message()
        if not diff.strip():
            QMessageBox.information(
                self, t("git.generate_no_changes_title"),
                t("git.generate_no_changes_msg")
            )
            return

        main_window = self._find_main_window()
        config = main_window.get_llm_config() if main_window is not None else None
        if not config or not config.get('api_base') or not config.get('model'):
            QMessageBox.warning(
                self, t("git.generate_no_config_title"),
                t("git.generate_no_config_msg")
            )
            return

        self.commit_widget.set_generating(True)
        worker = _CommitMessageWorker(config, diff, get_language(), self)
        self._commit_msg_worker = worker  # 持有引用，防止被 GC 提前回收
        worker.succeeded.connect(self._on_generate_done)
        worker.failed.connect(self._on_generate_failed)
        worker.finished.connect(lambda: self.commit_widget.set_generating(False))
        worker.start()

    def _on_generate_done(self, message: str):
        if message:
            self.commit_widget.set_message(message)

    def _on_generate_failed(self, error: str):
        QMessageBox.warning(
            self, t("git.generate_failed_title"),
            t("git.generate_failed_msg", error=error)
        )

    def _collect_diff_for_message(self, max_chars: int = 16000) -> str:
        """收集要喂给模型的改动：优先暂存区 diff，没有就用工作区 diff，
        并附上 `git status --short` 摘要（这样新增/未跟踪文件也能体现）。"""
        gm = self._git_manager
        ok, staged = gm._run_git('diff', '--cached')
        body = staged if (ok and staged.strip()) else ''
        if not body.strip():
            ok2, unstaged = gm._run_git('diff')
            if ok2 and unstaged.strip():
                body = unstaged

        ok3, status = gm._run_git('status', '--short')
        status = status.strip() if ok3 else ''

        sections = []
        if status:
            sections.append("# Changed files (git status --short)\n" + status)
        if body.strip():
            sections.append("# Diff\n" + body)
        text = "\n\n".join(sections)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]..."
        return text

    def _find_main_window(self):
        """向上找到持有 LLM 配置的主窗口（get_llm_config）。"""
        w = self.parent()
        while w is not None:
            if hasattr(w, 'get_llm_config'):
                return w
            w = w.parent()
        win = self.window()
        if win is not None and hasattr(win, 'get_llm_config'):
            return win
        return None

    def _show_diff(self, path: str, staged: bool):
        """显示 diff 对话框"""
        diff_content = self._git_manager.get_diff(path, staged)
        title = f"Diff: {path}" + (" (staged)" if staged else "")
        dialog = GitDiffDialog(title, diff_content, self.theme, self)
        dialog.exec()

    def _show_error(self, message: str):
        """显示错误消息"""
        QMessageBox.warning(self, t("git.error_title"), message)

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()
        self.header.apply_theme(theme)
        self.changes_widget.apply_theme(theme)
        self.commit_widget.apply_theme(theme)

        self.no_repo_label.setStyleSheet(f"""
            QLabel {{
                color: {theme.get('text_dim', '#888')};
                font-size: 13px;
                padding: 20px;
            }}
        """)

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.header.apply_language()
        self.changes_widget.apply_language()
        self.commit_widget.apply_language()
        self.no_repo_label.setText(t("git.no_repo"))

    def refresh(self):
        """手动刷新"""
        self._refresh_status()
        self._refresh_branches()
