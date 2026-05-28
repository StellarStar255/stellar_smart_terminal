"""
Git 面板 UI 组件
提供类似 Cursor IDE 的 Git 管理界面
"""
import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QComboBox, QScrollArea, QListWidget,
    QListWidgetItem, QPlainTextEdit, QSizePolicy,
    QAbstractItemView, QMessageBox, QDialog, QTextEdit, QSplitter,
    QLineEdit, QDialogButtonBox, QMenu, QInputDialog
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QThread, QTimer, QPoint, QRect
from PyQt6.QtGui import (
    QFont, QColor, QBrush, QTextCursor, QTextCharFormat, QTextBlockFormat,
    QFontMetrics, QPainter, QPen, QAction
)

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
        try:
            self.diff_clicked.emit(self.git_file.path, self.is_staged)
            super().mouseDoubleClickEvent(event)
        except RuntimeError:
            # 条目可能在刷新中已被销毁，忽略本次事件即可
            pass


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
        # 列表没变就不重建：避免每次刷新（5s 定时 / fetch 后）都销毁重建条目，
        # 否则用户正在双击的条目可能在事件处理途中被删 → RuntimeError。
        fp = (
            tuple((f.path, getattr(f.status, 'value', f.status)) for f in staged),
            tuple((f.path, getattr(f.status, 'value', f.status)) for f in unstaged),
        )
        if fp == getattr(self, '_files_fingerprint', None):
            return
        self._files_fingerprint = fp

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


class _GitOpWorker(QThread):
    """后台执行 push/pull 等网络操作，避免阻塞 UI 线程（否则会卡死）。

    GitManager 内部通过 Qt 信号回报错误/状态，跨线程会自动排队到 UI 线程，
    所以在工作线程里直接调用是安全的。
    """
    done = pyqtSignal(bool, str)  # (success, kind)

    def __init__(self, fn, kind: str, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._kind = kind

    def run(self):
        try:
            ok = bool(self._fn())
        except Exception:
            ok = False
        self.done.emit(ok, self._kind)


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
        # 领先/落后远程的提交数，以及 push/pull 是否进行中（决定按钮文案）
        self._ahead = 0
        self._behind = 0
        self._push_busy = False
        self._pull_busy = False
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

    def set_busy(self, kind: str, busy: bool):
        """push/pull 进行中：禁用相关按钮并显示忙碌文案，避免重复点击。"""
        if kind == 'push':
            self._push_busy = busy
            self.push_btn.setEnabled(not busy)
        elif kind == 'pull':
            self._pull_busy = busy
            self.pull_btn.setEnabled(not busy)
        self._update_sync_button_text()

    def set_ahead_behind(self, ahead: int, behind: int):
        """更新领先/落后远程的提交数，反映到 Push/Pull 按钮文案上。"""
        self._ahead = max(0, int(ahead))
        self._behind = max(0, int(behind))
        self._update_sync_button_text()

    def _update_sync_button_text(self):
        """按当前 ahead/behind 与忙碌状态刷新 Push/Pull 按钮文字。"""
        if self._push_busy:
            self.push_btn.setText("↑ Pushing…")
        else:
            self.push_btn.setText(f"↑ Push ({self._ahead})" if self._ahead else "↑ Push")
        if self._pull_busy:
            self.pull_btn.setText("↓ Pulling…")
        else:
            self.pull_btn.setText(f"↓ Pull ({self._behind})" if self._behind else "↓ Pull")

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


class GitDiffView(QWidget):
    """左右并排的 diff 视图：左栏=旧/删除行，右栏=新/增加行，竖直滚动联动。

    内嵌在 Git 面板里（不弹窗）。把 `git diff` 的统一格式解析成两栏对齐显示。
    """
    closed = pyqtSignal()

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._syncing = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部：返回 + 文件名
        self._header = QFrame()
        h = QHBoxLayout(self._header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)
        self.back_btn = QPushButton(t("git.diff_back"))
        self.back_btn.clicked.connect(self.closed.emit)
        self.title_label = QLabel("")
        h.addWidget(self.back_btn)
        h.addWidget(self.title_label, 1)
        layout.addWidget(self._header)

        # 两栏并排
        self._split = QSplitter(Qt.Orientation.Horizontal)
        self.left_edit = self._make_edit()
        self.right_edit = self._make_edit()
        # 统一固定行高（代码字体行距 + 余量，容纳 CJK 回退字体），两栏每行严格等高
        self._fixed_line_height = QFontMetrics(self.left_edit.font()).height() + 4
        self._split.addWidget(self.left_edit)
        self._split.addWidget(self.right_edit)
        self._split.setSizes([500, 500])
        layout.addWidget(self._split, 1)

        # 竖直滚动联动
        self.left_edit.verticalScrollBar().valueChanged.connect(self._sync_from_left)
        self.right_edit.verticalScrollBar().valueChanged.connect(self._sync_from_right)

        self.apply_theme(self.theme)

    def _make_edit(self) -> QPlainTextEdit:
        # 用 QPlainTextEdit：竖直滚动以「行」为单位，两栏行数相同时按值同步 = 逐行精确对齐，
        # 不会像 QTextEdit 那样因像素高度细微差异（如横向滚动条）导致错位。
        e = QPlainTextEdit()
        e.setReadOnly(True)
        e.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        e.setFont(QFont("Menlo", 12))
        return e

    def _sync_from_left(self, value: int):
        if self._syncing:
            return
        self._syncing = True
        self.right_edit.verticalScrollBar().setValue(value)
        self._syncing = False

    def _sync_from_right(self, value: int):
        if self._syncing:
            return
        self._syncing = True
        self.left_edit.verticalScrollBar().setValue(value)
        self._syncing = False

    def set_diff(self, title: str, diff_content: str):
        self.title_label.setText(title)
        left_rows, right_rows = self._parse(diff_content or "")
        if not left_rows and not right_rows:
            left_rows = [(None, t("git.diff_no_content"), 'ctx')]
            right_rows = [(None, '', 'ctx')]
        self._fill_edit(self.left_edit, left_rows)
        self._fill_edit(self.right_edit, right_rows)
        self.left_edit.verticalScrollBar().setValue(0)
        self.right_edit.verticalScrollBar().setValue(0)

    def _line_bg_brush(self, kind: str):
        """整行背景：删除=半透明红、新增=半透明绿、对齐占位=半透明斜条纹、
        hunk 头=淡蓝；上下文无背景。"""
        if kind == 'del':
            return QBrush(QColor(229, 83, 75, 60))      # 红 ~0.23
        if kind == 'add':
            return QBrush(QColor(63, 185, 80, 60))       # 绿 ~0.23
        if kind == 'hunk':
            return QBrush(QColor(97, 175, 239, 38))      # 淡蓝
        if kind == 'pad':
            # 斜条纹半透明背景，标记"另一边有增/删"，方便对齐
            return QBrush(QColor(140, 140, 140, 70), Qt.BrushStyle.BDiagPattern)
        return None  # ctx：透明

    def _fill_edit(self, edit, rows):
        """用 QTextBlockFormat 逐行写入：整行背景填满（不是只染文字），
        行号灰色，代码用主题前景色。

        关键：给每一行设固定行高。否则含中文/emoji 的行会因回退字体更高而变高，
        两栏逐行累积错位。固定行高让两栏每行严格等高、精确对齐。
        """
        edit.clear()
        num_color = QColor('#6a6a7a')
        text_color = QColor(self.theme.get('text', '#eaeaea'))
        # 行高取代码字体行距 + 余量，足以容纳 CJK 回退字体，避免裁切
        line_h = self._fixed_line_height
        cursor = QTextCursor(edit.document())
        cursor.beginEditBlock()
        first = True
        for (ln, text, kind) in rows:
            if not first:
                cursor.insertBlock()
            first = False
            block_fmt = QTextBlockFormat()
            block_fmt.setLineHeight(
                line_h, QTextBlockFormat.LineHeightTypes.FixedHeight.value
            )
            brush = self._line_bg_brush(kind)
            if brush is not None:
                block_fmt.setBackground(brush)
            cursor.setBlockFormat(block_fmt)
            num_fmt = QTextCharFormat()
            num_fmt.setForeground(num_color)
            cursor.insertText((f'{ln:>5} ' if ln else '      '), num_fmt)
            txt_fmt = QTextCharFormat()
            txt_fmt.setForeground(text_color)
            cursor.insertText(text or '', txt_fmt)
        cursor.endEditBlock()

    def _parse(self, diff_content: str):
        """把统一 diff 解析成左右两列对齐的行列表。

        每行是 (lineno, text, kind)，kind ∈ {ctx, del, add, hunk, pad}。
        删除/新增成对时左右对齐，数量不等时短的一侧补 pad 空行。
        """
        import re
        left, right = [], []
        old_ln = new_ln = 0
        pend_del, pend_add = [], []
        MAX_ROWS = 6000

        def flush():
            n = max(len(pend_del), len(pend_add))
            for k in range(n):
                left.append(pend_del[k] if k < len(pend_del) else (None, '', 'pad'))
                right.append(pend_add[k] if k < len(pend_add) else (None, '', 'pad'))
            pend_del.clear()
            pend_add.clear()

        skip_prefixes = ('diff ', 'index ', '--- ', '+++ ', 'new file', 'deleted file',
                         'old mode', 'new mode', 'similarity ', 'rename ', '\\ No newline')
        for line in diff_content.splitlines():
            if line.startswith(skip_prefixes):
                continue
            if line.startswith('@@'):
                flush()
                m = re.search(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if m:
                    old_ln, new_ln = int(m.group(1)), int(m.group(2))
                left.append((None, line, 'hunk'))
                right.append((None, line, 'hunk'))
                continue
            if line.startswith('-'):
                pend_del.append((old_ln, line[1:], 'del'))
                old_ln += 1
            elif line.startswith('+'):
                pend_add.append((new_ln, line[1:], 'add'))
                new_ln += 1
            else:
                flush()
                text = line[1:] if line.startswith(' ') else line
                left.append((old_ln, text, 'ctx'))
                right.append((new_ln, text, 'ctx'))
                old_ln += 1
                new_ln += 1
            if len(left) > MAX_ROWS:
                break
        flush()
        return left, right

    def apply_theme(self, theme: dict):
        self.theme = theme
        edit_css = f"""
            QPlainTextEdit {{
                background-color: {theme.get('bg_dark', '#1a1a2e')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
            }}
        """
        self.left_edit.setStyleSheet(edit_css)
        self.right_edit.setStyleSheet(edit_css)
        self._header.setStyleSheet(f"""
            QFrame {{
                background-color: {theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)
        self.title_label.setStyleSheet(
            f"color: {theme.get('text', '#eaeaea')}; font-weight: bold;"
        )
        self.back_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

    def apply_language(self):
        self.back_btn.setText(t("git.diff_back"))


class GitOutputView(QWidget):
    """单栏只读输出视图：展示 push/pull 的完整 git 输出（进度、fast-forward、文件统计）。

    内嵌在主内容区（不弹窗），带返回按钮回到终端。
    """
    closed = pyqtSignal()

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QFrame()
        h = QHBoxLayout(self._header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)
        self.back_btn = QPushButton(t("git.diff_back"))
        self.back_btn.clicked.connect(self.closed.emit)
        self.title_label = QLabel("")
        h.addWidget(self.back_btn)
        h.addWidget(self.title_label, 1)
        layout.addWidget(self._header)

        self.output_edit = QTextEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.output_edit.setFont(QFont("Menlo", 12))
        layout.addWidget(self.output_edit, 1)

        self.apply_theme(self.theme)

    def set_output(self, title: str, text: str):
        self.title_label.setText(title)
        # 轻量着色：diff 增删行红绿、远程/合并信息蓝绿、其余普通
        fg = self.theme.get('text', '#eaeaea')
        lines = []
        for line in (text or '').splitlines():
            etext = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            c = fg
            stripped = line.strip()
            if line.startswith('+') and not line.startswith('+++'):
                c = '#98c379'   # 新增行 绿
            elif line.startswith('-') and not line.startswith('---'):
                c = '#e06c75'   # 删除行 红
            elif line.startswith('@@'):
                c = '#61afef'
            elif line.startswith('commit ') or line.startswith('Author:') or line.startswith('Date:'):
                c = '#e5c07b'   # 提交头 黄
            elif stripped.startswith('remote:') or stripped.startswith('来自') or '->' in stripped:
                c = '#61afef'
            elif 'Fast-forward' in line or 'Updating' in line or '更新' in line:
                c = '#98c379'
            lines.append(f'<span style="color:{c};">{etext or "&nbsp;"}</span>')
        self.output_edit.setHtml('<pre style="margin:0; padding:6px;">' + '\n'.join(lines) + '</pre>')
        self.output_edit.verticalScrollBar().setValue(0)

    def apply_theme(self, theme: dict):
        self.theme = theme
        self.output_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {theme.get('bg_dark', '#1a1a2e')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
            }}
        """)
        self._header.setStyleSheet(f"""
            QFrame {{
                background-color: {theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)
        self.title_label.setStyleSheet(
            f"color: {theme.get('text', '#eaeaea')}; font-weight: bold;"
        )
        self.back_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

    def apply_language(self):
        self.back_btn.setText(t("git.diff_back"))


class _GraphCanvas(QWidget):
    """提交历史 graph 画布：左侧 lane 点线（仿 VS Code），右侧 引用徽标 + 标题 + 作者。"""

    commit_clicked = pyqtSignal(str)  # 提交 hash

    LANE_COLORS = ['#e06c75', '#61afef', '#98c379', '#c678dd',
                   '#e5c07b', '#56b6c2', '#d19a66', '#abb2bf']

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._rows = []          # 每行 lane 布局
        self._max_cols = 1
        self._row_h = 24
        self._lane_w = 14
        self._dot_r = 4
        self._left_pad = 8
        self._hover = -1
        self._font = QFont("Menlo", 12)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_commits(self, commits: list):
        self._rows = self._compute_lanes(commits)
        self._max_cols = max((r['width'] for r in self._rows), default=1)
        self.setMinimumHeight(len(self._rows) * self._row_h)
        self.updateGeometry()
        self.update()

    @staticmethod
    def _compute_lanes(commits: list) -> list:
        """把提交序列排成 lane：返回每行 {commit, above, my, below, parents, width}。
        above/below 是该行上/下边的活动 lane（hash 列表），my 是该提交的 dot 所在列。"""
        rows = []
        above = []  # 进入当前行的 lane（= 上一行的 below）
        for c in commits:
            h, parents = c['hash'], c['parents']
            my = above.index(h) if h in above else len(above)
            below_src = list(above)
            while len(below_src) <= my:
                below_src.append(None)
            if parents:
                below_src[my] = parents[0]
                for p in parents[1:]:
                    if p not in below_src:
                        for j in range(len(below_src)):
                            if below_src[j] is None:
                                below_src[j] = p
                                break
                        else:
                            below_src.append(p)
            else:
                below_src[my] = None
            # 压缩：去掉 None 和重复（lane 合并）
            below, seen = [], set()
            for hh in below_src:
                if hh is None or hh in seen:
                    continue
                seen.add(hh)
                below.append(hh)
            width = max(len(above), len(below), my + 1)
            rows.append({'commit': c, 'above': list(above), 'my': my,
                         'below': below, 'parents': parents, 'width': width})
            above = below
        return rows

    def _lane_color(self, col: int) -> str:
        return self.LANE_COLORS[col % len(self.LANE_COLORS)]

    def _row_at(self, y: int) -> int:
        idx = y // self._row_h
        return idx if 0 <= idx < len(self._rows) else -1

    def mouseMoveEvent(self, event):
        idx = self._row_at(int(event.position().y()))
        if idx != self._hover:
            self._hover = idx
            self.update()

    def leaveEvent(self, event):
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event):
        idx = self._row_at(int(event.position().y()))
        if idx >= 0:
            self.commit_clicked.emit(self._rows[idx]['commit']['hash'])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._font)
        fm = QFontMetrics(self._font)
        w = self.width()
        row_h, lane_w, r = self._row_h, self._lane_w, self._dot_r

        def col_x(c):
            return self._left_pad + c * lane_w + lane_w // 2

        rect = event.rect()
        first = max(0, rect.top() // row_h - 1)
        last = min(len(self._rows) - 1, rect.bottom() // row_h + 1)
        text_color = QColor(self.theme.get('text', '#eaeaea'))
        dim_color = QColor(self.theme.get('text_dim', '#888'))

        for i in range(first, last + 1):
            row = self._rows[i]
            y_top = i * row_h
            y_mid = y_top + row_h // 2
            y_bot = y_top + row_h
            above, below, my = row['above'], row['below'], row['my']
            h = row['commit']['hash']

            if i == self._hover:
                painter.fillRect(0, y_top, w, row_h,
                                 QColor(self.theme.get('bg_hover', '#2d2d44')))

            # 上半：来自上方的 lane（合并进 dot 或穿过到下方）
            for a_idx, ah in enumerate(above):
                painter.setPen(QPen(QColor(self._lane_color(a_idx)), 2))
                if ah == h:
                    painter.drawLine(col_x(a_idx), y_top, col_x(my), y_mid)
                else:
                    bcol = below.index(ah) if ah in below else a_idx
                    painter.drawLine(col_x(a_idx), y_top, col_x(bcol), y_bot)

            # 下半：dot 连向各父提交
            for p in row['parents']:
                if p in below:
                    bcol = below.index(p)
                    painter.setPen(QPen(QColor(self._lane_color(bcol)), 2))
                    painter.drawLine(col_x(my), y_mid, col_x(bcol), y_bot)

            # dot
            dot = QColor(self._lane_color(my))
            painter.setBrush(QBrush(dot))
            painter.setPen(QPen(dot, 1))
            painter.drawEllipse(QPoint(col_x(my), y_mid), r, r)

            # 文本起点
            tx = self._left_pad + self._max_cols * lane_w + 8
            # 引用徽标（branch / HEAD / tag）
            for ref in row['commit']['refs']:
                label = ref.replace('HEAD -> ', '')
                is_head = ref.startswith('HEAD')
                is_remote = label.startswith('origin/') or '/' in label
                badge_bg = QColor(self.theme.get('accent', '#667eea')) if is_head \
                    else (QColor('#4b5263') if is_remote else QColor('#3a7a3a'))
                bw = fm.horizontalAdvance(label) + 12
                if tx + bw > w - 8:
                    break
                painter.setBrush(QBrush(badge_bg))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(QRect(tx, y_mid - fm.height() // 2 - 1,
                                              bw, fm.height() + 2), 4, 4)
                painter.setPen(QColor('#ffffff'))
                painter.drawText(QRect(tx, y_top, bw, row_h),
                                 Qt.AlignmentFlag.AlignCenter, label)
                tx += bw + 4

            # 标题 + 作者（作者右对齐、暗色）
            author = row['commit']['author']
            aw = fm.horizontalAdvance(author) + 12
            subj_w = max(20, w - tx - aw - 12)
            painter.setPen(text_color)
            subj = fm.elidedText(row['commit']['subject'],
                                 Qt.TextElideMode.ElideRight, subj_w)
            painter.drawText(QRect(tx, y_top, subj_w, row_h),
                             Qt.AlignmentFlag.AlignVCenter, subj)
            painter.setPen(dim_color)
            painter.drawText(QRect(w - aw - 8, y_top, aw, row_h),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             author)
        painter.end()


class GitGraphWidget(QWidget):
    """提交历史 graph 面板：顶部 "GRAPH" 标题 + 可滚动的 graph 画布。"""

    commit_clicked = pyqtSignal(str)

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._title = QLabel(t("git.graph_title"))
        layout.addWidget(self._title)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._canvas = _GraphCanvas(self.theme)
        self._canvas.commit_clicked.connect(self.commit_clicked.emit)
        self._scroll.setWidget(self._canvas)
        layout.addWidget(self._scroll, 1)

        self.apply_theme(self.theme)

    def set_commits(self, commits: list):
        self._canvas.set_commits(commits)

    def apply_theme(self, theme: dict):
        self.theme = theme
        self._canvas.theme = theme
        self._title.setStyleSheet(f"""
            QLabel {{
                color: {theme.get('text_dim', '#888')};
                background-color: {theme.get('bg_medium', '#16213e')};
                font-size: 11px; font-weight: bold;
                padding: 4px 10px;
                border-bottom: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)
        self._scroll.setStyleSheet(f"background-color: {theme.get('bg_dark', '#1a1a2e')}; border: none;")
        self._canvas.setStyleSheet(f"background-color: {theme.get('bg_dark', '#1a1a2e')};")
        self._canvas.update()

    def apply_language(self):
        self._title.setText(t("git.graph_title"))


class GitHeaderWidget(QFrame):
    """Git 面板头部"""

    # 信号
    branch_changed = pyqtSignal(str)              # 兼容保留：仅本地分支切换时触发
    ref_changed = pyqtSignal(str, str)            # (kind, name)；kind ∈ {'local','remote','tag'}
    refresh_clicked = pyqtSignal()
    settings_clicked = pyqtSignal()
    create_branch_clicked = pyqtSignal()

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
        self.branch_combo.currentIndexChanged.connect(self._on_combo_changed)
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

        # 新建分支按钮
        self.create_branch_btn = QPushButton("+")
        self.create_branch_btn.setToolTip(t("git.create_branch_tooltip"))
        self.create_branch_btn.setFixedSize(28, 28)
        self.create_branch_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                font-size: 18px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """)
        self.create_branch_btn.clicked.connect(self.create_branch_clicked.emit)
        layout.addWidget(self.create_branch_btn)

        # 设置按钮（齿轮）→ 打开 Git 代理等设置
        self.settings_btn = QPushButton("⚙")
        self.settings_btn.setToolTip(t("git.settings_tooltip"))
        self.settings_btn.setFixedSize(28, 28)
        self.settings_btn.setStyleSheet(f"""
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
        self.settings_btn.clicked.connect(self.settings_clicked.emit)
        layout.addWidget(self.settings_btn)

        self._update_style()

    def _update_style(self):
        """更新样式"""
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)

    def update_branches(self, branches: list, head_ref, tags: list = None):
        """更新分支/Tag 列表

        Args:
            branches: GitBranch 列表（本地 + 远程）
            head_ref: 当前 HEAD 引用 (kind, name)；亦可传入 str 表示本地分支名（兼容旧调用）
            tags: tag 名列表
        """
        tags = tags or []
        if isinstance(head_ref, str):
            head_ref = ('local', head_ref)
        head_kind, head_name = head_ref

        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()

        locals_ = [b for b in branches if not b.is_remote]
        remotes = [b for b in branches if b.is_remote]

        # 过滤掉"和本地同名"的远程项：点了等于切回本地，没有独立价值
        local_names = {b.name for b in locals_}
        remotes = [
            b for b in remotes
            if (b.name.split('/', 1)[1] if '/' in b.name else b.name) not in local_names
        ]

        # detached 且不在 tag 上：插入一个占位项指示当前状态
        if head_kind == 'detached':
            self.branch_combo.addItem(f"(detached {head_name})", ('detached', head_name))

        # 本地分支
        for b in locals_:
            self.branch_combo.addItem(b.name, ('local', b.name))

        # 远程独有分支
        if remotes:
            if self.branch_combo.count() > 0:
                self.branch_combo.insertSeparator(self.branch_combo.count())
            for b in remotes:
                self.branch_combo.addItem(b.name, ('remote', b.name))

        # Tags
        if tags:
            if self.branch_combo.count() > 0:
                self.branch_combo.insertSeparator(self.branch_combo.count())
            for tag in tags:
                self.branch_combo.addItem(f"tag: {tag}", ('tag', tag))

        # 按 HEAD 引用类型精确选中对应项
        for i in range(self.branch_combo.count()):
            data = self.branch_combo.itemData(i)
            if data and data[0] == head_kind and data[1] == head_name:
                self.branch_combo.setCurrentIndex(i)
                break

        self.branch_combo.blockSignals(False)

    def _on_combo_changed(self, index: int):
        """combo 选择变更 → 派发 ref_changed / branch_changed"""
        if index < 0:
            return
        data = self.branch_combo.itemData(index)
        if not data:
            return
        kind, name = data
        self.ref_changed.emit(kind, name)
        if kind == 'local':
            self.branch_changed.emit(name)

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

        btn_style = f"""
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
        """
        self.refresh_btn.setStyleSheet(btn_style)
        self.settings_btn.setStyleSheet(btn_style)
        self.create_branch_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                font-size: 18px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.title_label.setText(t("git.source_control"))
        self.refresh_btn.setToolTip(t("git.refresh_tooltip"))
        self.settings_btn.setToolTip(t("git.settings_tooltip"))
        self.create_branch_btn.setToolTip(t("git.create_branch_tooltip"))


class GitPanel(QWidget):
    """Git 管理面板"""

    # 用户拖拽分隔条改变提交区高度时发出（主窗口据此持久化）
    commit_height_changed = pyqtSignal(int)
    # 用户拖拽分隔条改变 body 各栏高度时发出完整 sizes（主窗口据此持久化）
    body_sizes_changed = pyqtSignal(list)
    # 双击文件请求查看 diff，交给主窗口在右侧大空间显示 (title, diff_content)
    diff_requested = pyqtSignal(str, str)
    # pull 等操作完成后，把 git 输出交给主窗口在右侧大空间显示 (title, output)
    output_requested = pyqtSignal(str, str)

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._desired_commit_height = 0  # 记忆的提交区高度（0=未设置，用默认）
        self._desired_body_sizes = None   # 记忆的 body splitter 各栏高度（None=未设置）
        self._git_manager = GitManager(self)
        self._last_fetch_ts = 0.0  # 上次后台 fetch 的时间（节流用）
        self._active_workers = set()  # 在跑的后台线程，关闭时统一等待，避免被销毁时 abort
        self._fetch_running = False

        self._setup_ui()
        self._connect_signals()

        # 从配置文件恢复用户设置的 git 代理（如果有）
        self._apply_persisted_git_proxy()

        # 定时后台 fetch：刷新远程跟踪分支，让"可 pull 条数"保持最新（仅面板可见时）
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setInterval(180_000)  # 3 分钟
        self._fetch_timer.timeout.connect(self._tick_fetch)
        self._fetch_timer.start()

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

        # 提交历史 graph（仿 VS Code）放在最下方，可拖拽分隔条调整高度
        self.graph_widget = GitGraphWidget(self.theme)
        self.graph_widget.commit_clicked.connect(self._on_commit_clicked)
        self.body_splitter.addWidget(self.graph_widget)

        # 变更列表 + graph 吃掉多余空间，提交区默认停在它的自然高度
        self.body_splitter.setStretchFactor(0, 1)
        self.body_splitter.setStretchFactor(1, 0)
        self.body_splitter.setStretchFactor(2, 2)
        self.body_splitter.setSizes([200, 180, 360])
        # 记忆用户拖拽过的提交区高度
        self.body_splitter.splitterMoved.connect(self._on_splitter_moved)

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

    def _on_splitter_moved(self, *_):
        """用户拖动分隔条 → 记住 body 各栏高度并通知外部持久化。"""
        sizes = self.body_splitter.sizes()
        if not sizes or not all(s >= 0 for s in sizes) or not any(s > 0 for s in sizes):
            return
        self._desired_body_sizes = list(sizes)
        self.body_sizes_changed.emit(list(sizes))
        # 兼容旧持久化字段：单独再记一次提交区高度
        idx = self.body_splitter.indexOf(self.commit_widget)
        if 0 <= idx < len(sizes) and sizes[idx] > 0:
            self._desired_commit_height = sizes[idx]
            self.commit_height_changed.emit(sizes[idx])

    def apply_commit_height(self, height: int):
        """由主窗口在加载配置后调用：设定要恢复的提交区高度。"""
        if isinstance(height, int) and height > 0:
            self._desired_commit_height = height
            self._apply_commit_height()

    def apply_body_sizes(self, sizes):
        """由主窗口在加载配置后调用：设定要恢复的 body 各栏高度。"""
        if isinstance(sizes, list) and sizes and all(isinstance(s, int) and s >= 0 for s in sizes):
            self._desired_body_sizes = list(sizes)
            self._apply_body_sizes()

    def _apply_body_sizes(self, _attempts: int = 0):
        """把记忆的 body 各栏高度套用到分隔器；栏数变化时按比例缩放再回填。"""
        sizes = self._desired_body_sizes
        if not sizes:
            return
        total = self.body_splitter.height()
        if total <= 1:
            if _attempts < 40:
                QTimer.singleShot(50, lambda: self._apply_body_sizes(_attempts + 1))
            return
        cur = self.body_splitter.sizes()
        n = len(cur)
        if n == 0:
            return
        # 栏数对得上就直接用；对不上则按比例填充到当前栏数
        if len(sizes) == n:
            src = list(sizes)
        else:
            src = (list(sizes) + [max(1, sum(sizes) // max(1, len(sizes)))] * n)[:n]
        s_total = sum(src) or 1
        new_sizes = [max(1, int(total * s / s_total)) for s in src]
        self.body_splitter.setSizes(new_sizes)

    def _apply_commit_height(self, _attempts: int = 0):
        """把记忆的提交区高度套用到分隔器（提交区在 splitter 中的实际索引），
        其余栏按原比例分剩余空间；布局还没就绪时稍后重试。"""
        height = self._desired_commit_height
        if height <= 0:
            return
        total = self.body_splitter.height()
        if total <= 1:
            # 面板还没排好版，等一下再试
            if _attempts < 40:
                QTimer.singleShot(50, lambda: self._apply_commit_height(_attempts + 1))
            return
        sizes = self.body_splitter.sizes()
        idx = self.body_splitter.indexOf(self.commit_widget)
        if idx < 0 or idx >= len(sizes):
            return
        commit = max(80, min(height, total - 120))
        rest = max(1, total - commit)
        other_sizes = [s for i, s in enumerate(sizes) if i != idx]
        other_total = sum(other_sizes) or 1
        # 其余栏按当前比例分摊剩余空间，再把 commit 塞回原索引
        scaled = [max(1, int(rest * s / other_total)) for s in other_sizes]
        new_sizes = scaled[:idx] + [commit] + scaled[idx:]
        # setSizes 不会触发 splitterMoved，不会回写循环
        self.body_splitter.setSizes(new_sizes)

    def _connect_signals(self):
        """连接信号"""
        # Git 管理器信号
        self._git_manager.status_changed.connect(self._refresh_status)
        self._git_manager.error_occurred.connect(self._show_error)
        self._git_manager.op_output.connect(self._on_op_output)

        # 头部信号
        self.header.ref_changed.connect(self._on_ref_changed)
        self.header.refresh_clicked.connect(self._on_refresh_clicked)
        self.header.settings_clicked.connect(self._on_settings_clicked)
        self.header.create_branch_clicked.connect(self._on_create_branch)

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
            self.graph_widget.show()
            self.commit_widget.show()
            self._refresh_status()
            self._refresh_branches()
            self._refresh_graph()
            # 面板每次显示时，恢复用户记忆的各栏高度
            if self._desired_body_sizes:
                self._apply_body_sizes()
            else:
                self._apply_commit_height()
            # 后台抓一次远程，刷新"可 pull 条数"
            self._fetch_async()
        else:
            self.no_repo_label.show()
            self.header.hide()
            self.changes_widget.hide()
            self.graph_widget.hide()
            self.commit_widget.hide()

    def _refresh_graph(self):
        """刷新提交历史 graph（commit/pull/fetch/切分支后调用）。"""
        commits = self._git_manager.get_log(limit=150, all_branches=True)
        self.graph_widget.set_commits(commits)

    def _on_commit_clicked(self, commit_hash: str):
        """点击 graph 上的提交 → 在右侧大空间展示该提交详情（git show）。"""
        text = self._git_manager.get_commit_show(commit_hash)
        self.output_requested.emit(
            t("git.commit_show_title", short=commit_hash[:7]), text
        )

    def _refresh_status(self):
        """刷新文件状态"""
        staged, unstaged = self._git_manager.get_status()
        self.changes_widget.update_files(staged, unstaged)
        # 同步本地领先/落后远程的提交数到 Push/Pull 按钮（纯本地比较，不联网）
        ahead, behind = self._git_manager.get_ahead_behind()
        self.commit_widget.set_ahead_behind(ahead, behind)

    def _refresh_branches(self):
        """刷新分支/Tag 列表"""
        branches = self._git_manager.get_branches()
        tags = self._git_manager.get_tags()
        head_ref = self._git_manager.get_head_ref()
        self.header.update_branches(branches, head_ref, tags)

    def _on_refresh_clicked(self):
        """点击 ↻：刷新状态 + 强制抓取一次远程（更新可 pull 条数）"""
        self._refresh_status()
        self._refresh_branches()
        self._refresh_graph()
        self._fetch_async(force=True)

    def _on_create_branch(self):
        """点击 +：从当前 HEAD 创建新分支并切换过去。"""
        text, ok = QInputDialog.getText(
            self,
            t("git.create_branch_title"),
            t("git.create_branch_prompt"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            return
        name = (text or '').strip()
        if not name:
            return
        if self._git_manager.create_branch(name):
            self._refresh_status()
            self._refresh_branches()
            self._refresh_graph()

    # ---------- Git 设置（代理等） ----------

    def _config_file_path(self) -> Path:
        """主配置文件位置（与 main_window 共用 .smart_terminal_config.json）。"""
        return Path(__file__).parent / ".smart_terminal_config.json"

    def _load_config(self) -> dict:
        try:
            p = self._config_file_path()
            if p.exists():
                with open(p, 'r', encoding='utf-8') as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    def _save_config(self, patch: dict):
        try:
            p = self._config_file_path()
            cfg = {}
            if p.exists():
                with open(p, 'r', encoding='utf-8') as f:
                    try:
                        cfg = json.load(f) or {}
                    except Exception:
                        cfg = {}
            cfg.update(patch)
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_persisted_git_proxy(self):
        """启动时从配置文件读取 git_proxy 并应用到 GitManager。

        历史列表保存在 git_proxies（list[str]），当前激活的在 git_proxy（str）。
        - 启动时把历史 / 激活值统一归一化（旧版可能存了不带 http:// 的裸地址）。
        - 若当前激活不在历史里，自动补进历史。
        """
        cfg = self._load_config()
        active = self._normalize_proxy(cfg.get('git_proxy') or '')
        history_raw = cfg.get('git_proxies') or []
        history = self._sanitize_proxy_history(
            [self._normalize_proxy(x) for x in history_raw]
        )
        patch = {}
        if active and active not in history:
            history.append(active)
        # 仅在归一化后真正变化时写回，避免无谓的文件写
        if active != (cfg.get('git_proxy') or '').strip():
            patch['git_proxy'] = active
        if history != history_raw:
            patch['git_proxies'] = history
        if patch:
            self._save_config(patch)
        self._git_manager.set_proxy(active)

    @staticmethod
    def _sanitize_proxy_history(items) -> list:
        """去重 + 去空白 + 保持原顺序。"""
        if not isinstance(items, list):
            return []
        out = []
        seen = set()
        for x in items:
            s = str(x or '').strip()
            if s and s not in seen:
                out.append(s)
                seen.add(s)
        return out

    def _get_proxy_history(self) -> list:
        return self._sanitize_proxy_history(self._load_config().get('git_proxies') or [])

    @staticmethod
    def _normalize_proxy(url: str) -> str:
        """补全 proxy URL：缺 scheme 时自动加 http://。

        - 'http://127.0.0.1:7897'      → 不变
        - '127.0.0.1:7897'             → 'http://127.0.0.1:7897'
        - 'socks5://127.0.0.1:1080'    → 不变（其它 scheme 不动）
        - 空串                          → 空串
        """
        url = (url or '').strip()
        if not url:
            return ''
        if '://' in url:
            return url
        return 'http://' + url

    def _apply_proxy_choice(self, url: str):
        """切换激活代理：写 GitManager + 持久化（输入会被归一化）。"""
        url = self._normalize_proxy(url)
        self._git_manager.set_proxy(url)
        patch = {'git_proxy': url}
        if url:
            history = self._get_proxy_history()
            if url not in history:
                history.append(url)
                patch['git_proxies'] = history
        self._save_config(patch)

    def _on_settings_clicked(self):
        """齿轮按钮 → 弹出快速切换菜单。"""
        active = self._git_manager.get_proxy()
        history = self._get_proxy_history()

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 24px;
            }}
            QMenu::item:selected {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
        """)

        # (No proxy) 项
        act_none = QAction(t("git.proxy_none"), self)
        act_none.setCheckable(True)
        act_none.setChecked(active == '')
        act_none.triggered.connect(lambda: self._apply_proxy_choice(''))
        menu.addAction(act_none)

        # 历史代理
        if history:
            menu.addSeparator()
            for url in history:
                act = QAction(url, self)
                act.setCheckable(True)
                act.setChecked(url == active)
                act.triggered.connect(lambda _checked, u=url: self._apply_proxy_choice(u))
                menu.addAction(act)

        # 添加 / 管理
        menu.addSeparator()
        act_add = QAction(t("git.proxy_add_new"), self)
        act_add.triggered.connect(self._add_new_proxy)
        menu.addAction(act_add)

        if history:
            act_manage = QAction(t("git.proxy_manage"), self)
            act_manage.triggered.connect(self._manage_proxies)
            menu.addAction(act_manage)

        # 紧贴齿轮按钮下方弹出
        btn = self.header.settings_btn
        pos = btn.mapToGlobal(QPoint(0, btn.height()))
        menu.exec(pos)

    def _add_new_proxy(self):
        """弹出输入框添加新代理，加入历史并激活。"""
        text, ok = QInputDialog.getText(
            self,
            t("git.proxy_add_title"),
            t("git.proxy_add_prompt"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            return
        url = (text or '').strip()
        if not url:
            return
        self._apply_proxy_choice(url)

    def _manage_proxies(self):
        """管理代理历史：列表 + 编辑/删除按钮。

        - 编辑：弹输入框预填当前 URL；保存时归一化、保持原顺序、并同步当前激活。
        - 删除：从历史移除；若是当前激活则切到 (No proxy)。
        - 双击列表项 = 快捷编辑。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(t("git.proxy_manage_title"))
        dlg.setMinimumWidth(420)
        v = QVBoxLayout(dlg)

        list_widget = QListWidget(dlg)
        for url in self._get_proxy_history():
            QListWidgetItem(url, list_widget)
        v.addWidget(list_widget)

        btns = QHBoxLayout()
        edit_btn = QPushButton(t("git.proxy_edit_btn"))
        remove_btn = QPushButton(t("git.proxy_remove_btn"))
        close_btn = QPushButton(t("git.proxy_close_btn"))
        btns.addWidget(edit_btn)
        btns.addWidget(remove_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        v.addLayout(btns)

        def _persist_from_list(old_url: str = '', new_url: str = ''):
            """从列表当前内容回写配置；如果 old→new 是当前激活的修改，同步激活值。"""
            history = [list_widget.item(i).text() for i in range(list_widget.count())]
            patch = {'git_proxies': history}
            active = self._git_manager.get_proxy()
            if old_url and old_url == active:
                # 当前激活被改名/删除
                self._git_manager.set_proxy(new_url)
                patch['git_proxy'] = new_url
            self._save_config(patch)

        def do_remove():
            item = list_widget.currentItem()
            if not item:
                return
            url = item.text()
            row = list_widget.row(item)
            list_widget.takeItem(row)
            _persist_from_list(old_url=url, new_url='')

        def do_edit():
            item = list_widget.currentItem()
            if not item:
                return
            old = item.text()
            text, ok = QInputDialog.getText(
                dlg,
                t("git.proxy_edit_title"),
                t("git.proxy_add_prompt"),
                QLineEdit.EchoMode.Normal,
                old,
            )
            if not ok:
                return
            new = self._normalize_proxy(text)
            if not new or new == old:
                return
            # 去重：若新值已存在于其他行，删除原行并选中已存在那行
            for i in range(list_widget.count()):
                if i != list_widget.row(item) and list_widget.item(i).text() == new:
                    list_widget.takeItem(list_widget.row(item))
                    list_widget.setCurrentRow(i if i < list_widget.row(item) else i - 1)
                    _persist_from_list(old_url=old, new_url=new)
                    return
            item.setText(new)
            _persist_from_list(old_url=old, new_url=new)

        edit_btn.clicked.connect(do_edit)
        remove_btn.clicked.connect(do_remove)
        close_btn.clicked.connect(dlg.accept)
        list_widget.itemDoubleClicked.connect(lambda _it: do_edit())
        dlg.exec()

    def _tick_fetch(self):
        """定时器：仅在面板可见时后台 fetch，更新可 pull 条数。"""
        if self.isVisible():
            self._fetch_async()

    def _fetch_async(self, force: bool = False):
        """后台 git fetch（不阻塞 UI、不弹窗）。默认 60s 内只抓一次，避免频繁联网。"""
        import time
        if self._git_manager._repo_path is None:
            return
        if self._fetch_running:
            return
        now = time.monotonic()
        if not force and (now - self._last_fetch_ts) < 60:
            return
        self._last_fetch_ts = now
        self._fetch_running = True
        worker = _GitOpWorker(self._git_manager.fetch, 'fetch', self)
        worker.done.connect(self._on_fetch_done)
        worker.finished.connect(self._on_fetch_finished)
        self._register_worker(worker)
        worker.start()

    def _on_fetch_finished(self):
        self._fetch_running = False

    def _on_fetch_done(self, ok: bool, _kind: str):
        # 抓取成功后远程跟踪分支已更新 → 重算 ahead/behind，刷新 Pull 计数 + graph
        if ok:
            self._refresh_status()
            self._refresh_graph()

    def _register_worker(self, worker):
        """登记后台线程：跑完自动从集合移除并 deleteLater；关闭时统一等待。"""
        self._active_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._active_workers.discard(w))
        worker.finished.connect(worker.deleteLater)

    def shutdown(self):
        """关闭前调用：停掉定时器并等待在跑的后台线程，避免线程仍在运行时被销毁导致 abort。

        关键：先 kill 正在跑的 git 子进程，否则 push/pull 卡在网络上时
        worker 一直阻塞在 subprocess.communicate()，QThread.terminate() 对
        阻塞在 C 库里的 Python 线程不可靠，会导致整个程序无法退出。
        """
        try:
            self._fetch_timer.stop()
        except RuntimeError:
            pass
        # 先放掉子进程，worker 线程随即从 communicate() 返回，wait() 几乎瞬间完成
        try:
            self._git_manager.cancel_running()
        except Exception:
            pass
        for worker in list(self._active_workers):
            try:
                if worker.isRunning():
                    if not worker.wait(3000):
                        worker.terminate()
                        worker.wait(1000)
            except RuntimeError:
                pass
        self._active_workers.clear()

    def _on_ref_changed(self, kind: str, name: str):
        """引用切换处理（本地/远程分支或 tag）"""
        # detached 占位项仅用于显示，不触发任何操作
        if kind == 'detached':
            return
        head_kind, head_name = self._git_manager.get_head_ref()
        # 已经在目标引用上，无需切换
        if (kind, name) == (head_kind, head_name):
            return
        if self._git_manager.checkout_ref(kind, name):
            self._refresh_status()
        # 不论成功失败都刷新一次，以同步 UI 选中态
        self._refresh_branches()
        self._refresh_graph()

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
            self._refresh_graph()

    def _on_push(self):
        """推送处理（后台线程，避免网络阻塞卡死 UI）"""
        self._run_git_op_async('push')

    def _on_pull(self):
        """拉取处理（后台线程，避免网络阻塞卡死 UI）"""
        self._run_git_op_async('pull')

    def _run_git_op_async(self, kind: str):
        # 操作期间对应按钮已被 set_busy 禁用，足以防重复点击
        self.commit_widget.set_busy(kind, True)
        fn = self._git_manager.push if kind == 'push' else self._git_manager.pull
        worker = _GitOpWorker(fn, kind, self)
        worker.done.connect(self._on_git_op_done)
        self._register_worker(worker)
        worker.start()

    def _on_git_op_done(self, ok: bool, kind: str):
        self.commit_widget.set_busy(kind, False)
        if not ok:
            # 失败信息已由 GitManager.error_occurred 弹出
            return
        # 成功不再弹窗打扰：push 后 ahead 计数归零、pull 后列表刷新，按钮本身就是反馈
        self._refresh_status()
        self._refresh_graph()

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
        worker.succeeded.connect(self._on_generate_done)
        worker.failed.connect(self._on_generate_failed)
        worker.finished.connect(lambda: self.commit_widget.set_generating(False))
        self._register_worker(worker)
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
        """双击文件 → 交给主窗口在右侧大空间以左右并排方式显示 diff（不弹窗）"""
        diff_content = self._git_manager.get_diff(path, staged)
        title = path + (" (staged)" if staged else "")
        self.diff_requested.emit(title, diff_content)

    def _on_op_output(self, kind: str, output: str):
        """pull 等操作的 git 输出 → 交给主窗口在右侧大空间展示（不弹窗）"""
        if kind == 'pull':
            self.output_requested.emit(t("git.pull_output_title"), output)

    def _show_error(self, message: str):
        """显示错误消息"""
        QMessageBox.warning(self, t("git.error_title"), message)

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()
        self.header.apply_theme(theme)
        self.changes_widget.apply_theme(theme)
        self.graph_widget.apply_theme(theme)
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
        self.graph_widget.apply_language()
        self.commit_widget.apply_language()
        self.no_repo_label.setText(t("git.no_repo"))

    def refresh(self):
        """手动刷新"""
        self._refresh_status()
        self._refresh_branches()
