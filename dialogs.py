"""
对话框
从 main_window.py 拆分出来的对话框类（及其专用辅助）
"""
import os
import sys
import json
import shutil
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeySequence, QTextCursor

from i18n import t


def get_default_shell():
    """
    获取系统默认 shell，跨平台支持：
    - macOS/Linux: 优先 $SHELL，然后 zsh -> bash -> sh
    - Windows: 优先 PowerShell，然后 cmd.exe
    """
    if sys.platform == 'win32':
        # Windows 平台
        # 优先使用 PowerShell（更现代）
        if shutil.which('pwsh'):  # PowerShell Core (跨平台版本)
            return 'pwsh'
        if shutil.which('powershell'):  # Windows PowerShell
            return 'powershell'
        # 回退到 cmd.exe
        comspec = os.environ.get('COMSPEC', '')
        if comspec and shutil.which(comspec):
            return comspec
        return 'cmd.exe'
    else:
        # macOS / Linux
        # 首先尝试用户的默认 shell
        user_shell = os.environ.get('SHELL', '')
        if user_shell and shutil.which(os.path.basename(user_shell)):
            return os.path.basename(user_shell)

        # 按优先级尝试常见 shell
        for shell in ['zsh', 'bash', 'sh']:
            if shutil.which(shell):
                return shell

        # 最后的回退
        return 'sh'


class _IndentingPlainTextEdit(QPlainTextEdit):
    """命令编辑框：像代码编辑器一样用 Tab/Shift+Tab 缩进，4 空格为一级。

    - Tab：有选区 → 选中的每一行整体右移 4 空格；无选区 → 光标处插入 4 空格。
    - Shift+Tab：选中的每一行（或当前行）左移，去掉行首最多 4 个空格（兼容已有 Tab）。
    """
    _INDENT = "    "  # 4 空格

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        # Shift+Tab 在 Qt 里以 Key_Backtab 到达
        if key == Qt.Key.Key_Backtab or (
                key == Qt.Key.Key_Tab and mods & Qt.KeyboardModifier.ShiftModifier):
            self._dedent()
            return
        if key == Qt.Key.Key_Tab and mods == Qt.KeyboardModifier.NoModifier:
            if self.textCursor().hasSelection():
                self._indent()
            else:
                self.insertPlainText(self._INDENT)
            return
        super().keyPressEvent(event)

    def _block_range(self):
        """当前选区（或光标）覆盖的 [首行号, 末行号]。"""
        cur = self.textCursor()
        start, end = cur.selectionStart(), cur.selectionEnd()
        doc = self.document()
        first = doc.findBlock(start).blockNumber()
        last = doc.findBlock(end).blockNumber()
        # 选区终点恰好落在行首（且非空选）→ 那一行不计入
        if end > start and doc.findBlock(end).position() == end:
            last = max(first, last - 1)
        return first, last

    def _reselect(self, first, last):
        doc = self.document()
        fb = doc.findBlockByNumber(first)
        lb = doc.findBlockByNumber(last)
        cur = self.textCursor()
        cur.setPosition(fb.position())
        cur.setPosition(lb.position() + len(lb.text()),
                        QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(cur)

    def _indent(self):
        first, last = self._block_range()
        doc = self.document()
        cur = self.textCursor()
        cur.beginEditBlock()
        for bn in range(first, last + 1):
            c = QTextCursor(doc.findBlockByNumber(bn))
            c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            c.insertText(self._INDENT)
        cur.endEditBlock()
        self._reselect(first, last)

    def _dedent(self):
        first, last = self._block_range()
        doc = self.document()
        cur = self.textCursor()
        cur.beginEditBlock()
        for bn in range(first, last + 1):
            block = doc.findBlockByNumber(bn)
            text = block.text()
            remove = 0
            while (remove < len(self._INDENT) and remove < len(text)
                   and text[remove] == ' '):
                remove += 1
            if remove == 0 and text.startswith('\t'):
                remove = 1  # 兼容历史上用 Tab 缩进的行
            if remove:
                c = QTextCursor(block)
                c.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                for _ in range(remove):
                    c.deleteChar()
        cur.endEditBlock()
        self._reselect(first, last)


class PresetDialog(QDialog):
    """预设命令管理对话框"""

    def __init__(self, presets: list, parent=None, auto_add: bool = False, title: str = None):
        super().__init__(parent)
        self.presets = [p.copy() for p in presets]  # 深拷贝
        self._auto_add = auto_add
        self.setWindowTitle(title or t("preset.manage_title"))
        self.setMinimumSize(600, 450)

        # 使用唯一 ID 来稳定关联 item 和 preset（避免拖拽时数据丢失）
        self._preset_id_counter = 0
        self._preset_map = {}  # id -> preset
        for p in self.presets:
            pid = self._next_preset_id()
            p['_id'] = pid
            self._preset_map[pid] = p

        # 关闭标志，防止关闭时触发信号处理
        self._closing = False

        self._setup_ui()
        self._apply_style()

        # 如果是添加模式，自动创建新预设
        if self._auto_add:
            QTimer.singleShot(0, self._add_preset)

    def _next_preset_id(self):
        """生成下一个唯一 ID"""
        self._preset_id_counter += 1
        return self._preset_id_counter

    def _get_preset_by_id(self, pid):
        """通过 ID 获取 preset"""
        return self._preset_map.get(pid)

    def _setup_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)

        # 内容区域（水平布局）
        content_layout = QHBoxLayout()

        # 左侧：预设列表
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel(t("preset.list_label")))

        self.preset_list = QListWidget()
        self.preset_list.currentRowChanged.connect(self._on_selection_changed)
        # 启用拖拽排序
        self.preset_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.preset_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.preset_list.model().rowsMoved.connect(self._on_rows_moved)
        left_layout.addWidget(self.preset_list)

        # 按钮组
        btn_layout = QHBoxLayout()
        self.add_btn = QPushButton(t("preset.new"))
        self.add_btn.clicked.connect(self._add_preset)
        self.save_btn = QPushButton(t("preset.save"))
        self.save_btn.clicked.connect(self._save_current_preset)
        self.save_btn.setToolTip(t("preset.save_tooltip"))
        self.delete_btn = QPushButton(t("preset.delete"))
        self.delete_btn.clicked.connect(self._delete_preset)
        btn_layout.addWidget(self.add_btn)
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.delete_btn)
        left_layout.addLayout(btn_layout)

        content_layout.addLayout(left_layout, 1)

        # 右侧：编辑区
        right_layout = QVBoxLayout()

        # 名称
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel(t("preset.name_label")))
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._on_name_changed)
        name_layout.addWidget(self.name_edit)
        right_layout.addLayout(name_layout)

        # 命令（多行）
        right_layout.addWidget(QLabel(t("preset.commands_label")))
        self.commands_edit = _IndentingPlainTextEdit()
        self.commands_edit.setPlaceholderText(t("preset.commands_placeholder"))
        self.commands_edit.textChanged.connect(self._on_commands_changed)
        right_layout.addWidget(self.commands_edit)

        content_layout.addLayout(right_layout, 2)

        main_layout.addLayout(content_layout)

        # 底部按钮
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # 填充列表
        self._populate_list()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
                color: #eaeaea;
            }
            QLabel {
                color: #aaa;
            }
            QLineEdit, QPlainTextEdit {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 6px;
                color: #eaeaea;
            }
            QLineEdit:focus, QPlainTextEdit:focus {
                border-color: #667eea;
            }
            QListWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                color: #eaeaea;
            }
            QListWidget::item:selected {
                background-color: #667eea;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                color: #eaeaea;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
        """)

    def _populate_list(self):
        self.preset_list.clear()
        for preset in self.presets:
            item = QListWidgetItem(preset.get('name', t("preset.unnamed")))
            item.setData(Qt.ItemDataRole.UserRole, preset.get('_id'))  # 只存储 ID（避免拖拽时数据丢失）
            self.preset_list.addItem(item)
        if self.presets:
            self.preset_list.setCurrentRow(0)

    def _on_selection_changed(self, row):
        if self._closing:
            return
        if 0 <= row < self.preset_list.count():
            item = self.preset_list.item(row)
            pid = item.data(Qt.ItemDataRole.UserRole) if item else None
            preset = self._get_preset_by_id(pid) if pid else None
            if preset:
                self.name_edit.blockSignals(True)
                self.commands_edit.blockSignals(True)
                self.name_edit.setText(preset.get('name', ''))
                self.commands_edit.setPlainText('\n'.join(preset.get('commands', [])))
                self.name_edit.blockSignals(False)
                self.commands_edit.blockSignals(False)
                return
        # 清空时也阻止信号，避免触发不必要的更新
        self.name_edit.blockSignals(True)
        self.commands_edit.blockSignals(True)
        self.name_edit.clear()
        self.commands_edit.clear()
        self.name_edit.blockSignals(False)
        self.commands_edit.blockSignals(False)

    def _on_name_changed(self, text):
        if self._closing:
            return
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            preset['name'] = text
            item.setText(text)

    def _on_commands_changed(self):
        if self._closing:
            return
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            text = self.commands_edit.toPlainText()
            commands = [line for line in text.split('\n') if line.strip()]
            preset['commands'] = commands

    def _add_preset(self):
        pid = self._next_preset_id()
        new_preset = {
            '_id': pid,
            'name': t("preset.default_name", n=len(self.presets) + 1),
            'commands': [get_default_shell()]
        }
        self.presets.append(new_preset)
        self._preset_map[pid] = new_preset
        item = QListWidgetItem(new_preset['name'])
        item.setData(Qt.ItemDataRole.UserRole, pid)  # 只存储 ID
        self.preset_list.addItem(item)
        self.preset_list.setCurrentRow(len(self.presets) - 1)

    def _delete_preset(self):
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset and preset in self.presets:
            self.presets.remove(preset)
            if pid in self._preset_map:
                del self._preset_map[pid]
            self.preset_list.takeItem(row)

    def _save_current_preset(self):
        """保存当前编辑的预设"""
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            # 确保当前编辑已同步到 preset
            preset['name'] = self.name_edit.text()
            text = self.commands_edit.toPlainText()
            commands = [line for line in text.split('\n') if line.strip()]
            preset['commands'] = commands
            # 更新列表显示
            item.setText(preset['name'])
        else:
            QMessageBox.warning(self, t("preset.cannot_save"), t("preset.select_first"))

    def _on_rows_moved(self, parent, start, end, destination, row):
        """拖拽排序后同步 presets 列表顺序"""
        if self._closing:
            return
        # 根据 item 的新顺序重新排列 presets 列表
        new_presets = []
        for i in range(self.preset_list.count()):
            item = self.preset_list.item(i)
            pid = item.data(Qt.ItemDataRole.UserRole)
            preset = self._get_preset_by_id(pid) if pid else None
            if preset:
                new_presets.append(preset)
        self.presets = new_presets

    def accept(self):
        """重写 accept 方法，确保当前编辑在关闭前被保存"""
        # 设置关闭标志，阻止信号处理
        self._closing = True

        # 先保存当前正在编辑的预设
        row = self.preset_list.currentRow()
        item = self.preset_list.item(row) if row >= 0 else None
        pid = item.data(Qt.ItemDataRole.UserRole) if item else None
        preset = self._get_preset_by_id(pid) if pid else None
        if preset:
            preset['name'] = self.name_edit.text()
            text = self.commands_edit.toPlainText()
            commands = [line for line in text.split('\n') if line.strip()]
            preset['commands'] = commands
        super().accept()

    def get_presets(self):
        # 返回不包含内部 _id 字段的 preset 列表
        result = []
        for p in self.presets:
            preset_copy = {k: v for k, v in p.items() if k != '_id'}
            result.append(preset_copy)
        return result


class LLMConfigDialog(QDialog):
    """LLM API 配置管理对话框"""

    # 默认配置模板
    DEFAULT_CONFIG = {
        'name': t("llm.new_config_name"),
        'api_base': 'https://api.openai.com/v1',
        'api_key': '',
        'model': 'gpt-4',
        'timeout': 30,
        'max_tokens': 4096,
        'temperature': 1.0,
        'top_p': 1.0,
        'proxy': ''
    }

    def __init__(self, configs: list, default_index: int = 0, parent=None):
        super().__init__(parent)
        self.configs = [c.copy() for c in configs]  # 深拷贝
        self.default_index = default_index
        self.setWindowTitle(t("llm.config_title"))
        self.setMinimumSize(680, 460)
        self.resize(760, 500)

        # 使用唯一 ID 来稳定关联 item 和 config
        self._config_id_counter = 0
        self._config_map = {}  # id -> config
        for c in self.configs:
            cid = self._next_config_id()
            c['_id'] = cid
            self._config_map[cid] = c

        # 关闭标志
        self._closing = False
        # API Key 显示状态
        self._api_key_visible = False

        self._setup_ui()
        self._apply_style()

    def _next_config_id(self):
        """生成下一个唯一 ID"""
        self._config_id_counter += 1
        return self._config_id_counter

    def _get_config_by_id(self, cid):
        """通过 ID 获取 config"""
        return self._config_map.get(cid)

    def _setup_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 14, 16, 12)
        main_layout.setSpacing(10)

        # 内容区域（水平布局）
        content_layout = QHBoxLayout()
        content_layout.setSpacing(16)

        # 左侧：配置列表，标题行内嵌「＋/－」按钮
        left_layout = QVBoxLayout()
        left_layout.setSpacing(6)

        list_header = QHBoxLayout()
        list_label = QLabel(t("llm.config_list_label"))
        list_label.setObjectName("sectionLabel")
        list_header.addWidget(list_label)
        list_header.addStretch()
        self.add_btn = QPushButton("＋")
        self.add_btn.setObjectName("iconBtn")
        self.add_btn.setToolTip(t("llm.new_tooltip"))
        self.add_btn.clicked.connect(self._add_config)
        self.delete_btn = QPushButton("－")
        self.delete_btn.setObjectName("iconBtn")
        self.delete_btn.setToolTip(t("llm.delete_tooltip"))
        self.delete_btn.clicked.connect(self._delete_config)
        list_header.addWidget(self.add_btn)
        list_header.addWidget(self.delete_btn)
        left_layout.addLayout(list_header)

        self.config_list = QListWidget()
        self.config_list.currentRowChanged.connect(self._on_selection_changed)
        # 启用拖拽排序
        self.config_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.config_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.config_list.model().rowsMoved.connect(self._on_rows_moved)
        left_layout.addWidget(self.config_list)

        # JSON 导入/导出
        json_btn_layout = QHBoxLayout()
        json_btn_layout.setSpacing(6)
        self.copy_json_btn = QPushButton(t("llm.export_json"))
        self.copy_json_btn.setObjectName("linkBtn")
        self.copy_json_btn.setToolTip(t("llm.copy_json_tooltip"))
        self.copy_json_btn.clicked.connect(self._copy_as_json)
        self.paste_json_btn = QPushButton(t("llm.import_json"))
        self.paste_json_btn.setObjectName("linkBtn")
        self.paste_json_btn.setToolTip(t("llm.paste_json_tooltip"))
        self.paste_json_btn.clicked.connect(self._paste_as_json)
        json_btn_layout.addWidget(self.copy_json_btn)
        json_btn_layout.addWidget(self.paste_json_btn)
        json_btn_layout.addStretch()
        left_layout.addLayout(json_btn_layout)

        content_layout.addLayout(left_layout, 1)

        # 右侧：编辑区（无选中配置时整体禁用）
        self.editor_panel = QWidget()
        right_layout = QVBoxLayout(self.editor_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        edit_label = QLabel(t("llm.edit_label"))
        edit_label.setObjectName("sectionLabel")
        right_layout.addWidget(edit_label)

        form_layout = QFormLayout()
        form_layout.setSpacing(10)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # 名称
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(t("llm.name_placeholder"))
        self.name_edit.textChanged.connect(self._on_name_changed)
        form_layout.addRow(t("llm.name_label"), self.name_edit)

        # 用途：默认 / 补全 / Git（可勾选胶囊，替代原先左侧的三个按钮）
        role_layout = QHBoxLayout()
        role_layout.setContentsMargins(0, 0, 0, 0)
        role_layout.setSpacing(8)
        self.default_chip = QPushButton(t("llm.chip_default"))
        self.default_chip.setToolTip(t("llm.set_default_tooltip"))
        self.default_chip.clicked.connect(self._on_default_chip)
        self.completion_chip = QPushButton(t("llm.chip_completion"))
        self.completion_chip.setToolTip(t("llm.set_completion_tooltip"))
        self.completion_chip.clicked.connect(
            lambda checked: self._on_role_chip('for_completion', checked))
        self.git_chip = QPushButton(t("llm.chip_git"))
        self.git_chip.setToolTip(t("llm.set_git_tooltip"))
        self.git_chip.clicked.connect(
            lambda checked: self._on_role_chip('for_git', checked))
        for chip in (self.default_chip, self.completion_chip, self.git_chip):
            chip.setCheckable(True)
            chip.setProperty('roleChip', True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            role_layout.addWidget(chip)
        role_layout.addStretch()
        role_widget = QWidget()
        role_widget.setLayout(role_layout)
        form_layout.addRow(t("llm.role_label"), role_widget)

        # API Base URL
        self.api_base_edit = QLineEdit()
        self.api_base_edit.setPlaceholderText("https://api.openai.com/v1")
        self.api_base_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow("API URL:", self.api_base_edit)

        # API Key (带显示/隐藏切换)
        api_key_layout = QHBoxLayout()
        api_key_layout.setContentsMargins(0, 0, 0, 0)
        api_key_layout.setSpacing(6)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.api_key_edit.textChanged.connect(self._on_field_changed)
        api_key_layout.addWidget(self.api_key_edit)

        self.toggle_key_btn = QPushButton(t("llm.show_key"))
        self.toggle_key_btn.setObjectName("linkBtn")
        self.toggle_key_btn.setMinimumWidth(56)
        self.toggle_key_btn.clicked.connect(self._toggle_api_key_visibility)
        api_key_layout.addWidget(self.toggle_key_btn)

        api_key_widget = QWidget()
        api_key_widget.setLayout(api_key_layout)
        form_layout.addRow("API Key:", api_key_widget)

        # 模型名称
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("gpt-4")
        self.model_edit.textChanged.connect(self._on_field_changed)
        form_layout.addRow(t("llm.model_label"), self.model_edit)

        right_layout.addLayout(form_layout)

        # 高级参数（默认折叠：超时/最大 Tokens/温度/Top P/代理）
        self.adv_toggle = QPushButton("▸ " + t("llm.advanced"))
        self.adv_toggle.setObjectName("advToggle")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.adv_toggle.toggled.connect(self._toggle_advanced)
        right_layout.addWidget(self.adv_toggle)

        self.timeout_edit = QLineEdit()
        self.timeout_edit.setPlaceholderText("30")
        self.timeout_edit.textChanged.connect(self._on_field_changed)
        self.max_tokens_edit = QLineEdit()
        self.max_tokens_edit.setPlaceholderText("4096")
        self.max_tokens_edit.textChanged.connect(self._on_field_changed)
        self.temperature_edit = QLineEdit()
        self.temperature_edit.setPlaceholderText(t("llm.temperature_placeholder"))
        self.temperature_edit.textChanged.connect(self._on_field_changed)
        self.top_p_edit = QLineEdit()
        self.top_p_edit.setPlaceholderText(t("llm.top_p_placeholder"))
        self.top_p_edit.textChanged.connect(self._on_field_changed)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText(t("llm.proxy_placeholder"))
        self.proxy_edit.textChanged.connect(self._on_field_changed)

        self.adv_widget = QWidget()
        adv_grid = QGridLayout(self.adv_widget)
        adv_grid.setContentsMargins(0, 2, 0, 0)
        adv_grid.setHorizontalSpacing(10)
        adv_grid.setVerticalSpacing(10)
        adv_grid.addWidget(QLabel(t("llm.timeout_label")), 0, 0)
        adv_grid.addWidget(self.timeout_edit, 0, 1)
        adv_grid.addWidget(QLabel(t("llm.max_tokens_label")), 0, 2)
        adv_grid.addWidget(self.max_tokens_edit, 0, 3)
        adv_grid.addWidget(QLabel(t("llm.temperature_label")), 1, 0)
        adv_grid.addWidget(self.temperature_edit, 1, 1)
        adv_grid.addWidget(QLabel("Top P:"), 1, 2)
        adv_grid.addWidget(self.top_p_edit, 1, 3)
        adv_grid.addWidget(QLabel(t("llm.proxy_label")), 2, 0)
        adv_grid.addWidget(self.proxy_edit, 2, 1, 1, 3)
        adv_grid.setColumnStretch(1, 1)
        adv_grid.setColumnStretch(3, 1)
        self.adv_widget.setVisible(False)
        right_layout.addWidget(self.adv_widget)

        right_layout.addStretch()
        content_layout.addWidget(self.editor_panel, 2)

        main_layout.addLayout(content_layout)

        # 底部按钮
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # 填充列表
        self._populate_list()
        if not self.configs:
            self.editor_panel.setEnabled(False)

    def _toggle_advanced(self, checked):
        """展开/折叠高级参数区"""
        self.adv_toggle.setText(("▾ " if checked else "▸ ") + t("llm.advanced"))
        self.adv_widget.setVisible(checked)

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
                color: #eaeaea;
            }
            QLabel {
                color: #9aa0b5;
            }
            QLabel#sectionLabel {
                color: #7f88a8;
                font-weight: 600;
            }
            QLineEdit {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                padding: 7px 10px;
                color: #eaeaea;
                selection-background-color: #667eea;
            }
            QLineEdit:focus {
                border-color: #667eea;
            }
            QListWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                padding: 4px;
                color: #eaeaea;
                outline: none;
            }
            QListWidget::item {
                padding: 7px 8px;
                border-radius: 4px;
            }
            QListWidget::item:hover {
                background-color: #222a4d;
            }
            QListWidget::item:selected {
                background-color: #667eea;
                color: #ffffff;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 6px;
                padding: 7px 16px;
                color: #eaeaea;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
            QPushButton:pressed {
                background-color: #35354f;
            }
            QPushButton:disabled {
                color: #777;
            }
            QPushButton#iconBtn {
                background: transparent;
                padding: 0;
                min-width: 26px;
                max-width: 26px;
                min-height: 26px;
                max-height: 26px;
                font-size: 15px;
                color: #9aa0b5;
            }
            QPushButton#iconBtn:hover {
                background-color: #3d3d5c;
                color: #ffffff;
            }
            QPushButton#linkBtn {
                background: transparent;
                border: 1px solid #3d3d5c;
                padding: 5px 10px;
                color: #9aa0b5;
            }
            QPushButton#linkBtn:hover {
                background: transparent;
                border-color: #667eea;
                color: #eaeaea;
            }
            QPushButton[roleChip="true"] {
                background: transparent;
                border: 1px solid #3d3d5c;
                border-radius: 13px;
                padding: 4px 14px;
                color: #9aa0b5;
            }
            QPushButton[roleChip="true"]:hover {
                background: transparent;
                border-color: #667eea;
                color: #eaeaea;
            }
            QPushButton[roleChip="true"]:checked {
                background-color: #667eea;
                border-color: #667eea;
                color: #ffffff;
            }
            QPushButton#advToggle {
                background: transparent;
                border: none;
                padding: 2px 0;
                color: #7f88a8;
                text-align: left;
                font-weight: 600;
            }
            QPushButton#advToggle:hover {
                background: transparent;
                color: #eaeaea;
            }
            QDialogButtonBox QPushButton {
                min-width: 72px;
            }
            QDialogButtonBox QPushButton:default {
                background-color: #667eea;
                color: #ffffff;
            }
            QDialogButtonBox QPushButton:default:hover {
                background-color: #7b8ef0;
            }
        """)

    def _item_label(self, config) -> str:
        """列表项文字：名字 + 角色标记（默认/补全/Git）。"""
        name = config.get('name', t("llm.unnamed"))
        try:
            idx = self.configs.index(config)
        except ValueError:
            idx = -1
        tags = []
        if idx == self.default_index:
            tags.append('★' + t("llm.tag_default"))
        if config.get('for_completion'):
            tags.append('✎' + t("llm.tag_completion"))
        if config.get('for_git'):
            tags.append('⎇' + t("llm.tag_git"))
        return f"{name}   [{'  '.join(tags)}]" if tags else name

    def _populate_list(self):
        self.config_list.clear()
        for config in self.configs:
            item = QListWidgetItem(self._item_label(config))
            item.setData(Qt.ItemDataRole.UserRole, config.get('_id'))
            self.config_list.addItem(item)
        if self.configs:
            self.config_list.setCurrentRow(0)

    def _current_config(self):
        """获取当前选中的配置（无选中返回 None）"""
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        return self._get_config_by_id(cid) if cid else None

    def _refresh_list_labels(self):
        """就地刷新列表项文字（不重建列表，避免选中态跳动）"""
        for i in range(self.config_list.count()):
            item = self.config_list.item(i)
            config = self._get_config_by_id(item.data(Qt.ItemDataRole.UserRole))
            if config:
                item.setText(self._item_label(config))

    def _on_default_chip(self, checked):
        """默认角色唯一且必须存在：点选其他配置可转移，不可直接取消"""
        config = self._current_config()
        if not config:
            self.default_chip.setChecked(False)
            return
        idx = self.configs.index(config)
        if idx != self.default_index:
            self.default_index = idx
        self.default_chip.setChecked(True)
        self._refresh_list_labels()

    def _on_role_chip(self, role_key, checked):
        """补全/Git 角色独占指派；取消勾选则回退到默认配置"""
        config = self._current_config()
        if not config:
            return
        for c in self.configs:
            c.pop(role_key, None)
        if checked:
            config[role_key] = True
        self._refresh_list_labels()

    def _on_selection_changed(self, row):
        if self._closing:
            return
        if 0 <= row < self.config_list.count():
            item = self.config_list.item(row)
            cid = item.data(Qt.ItemDataRole.UserRole) if item else None
            config = self._get_config_by_id(cid) if cid else None
            if config:
                # 阻止信号以避免循环更新
                self._block_signals(True)
                self.name_edit.setText(config.get('name', ''))
                self.api_base_edit.setText(config.get('api_base', ''))
                self.api_key_edit.setText(config.get('api_key', ''))
                self.model_edit.setText(config.get('model', ''))
                self.timeout_edit.setText(str(config.get('timeout', 30)))
                self.max_tokens_edit.setText(str(config.get('max_tokens', 4096)))
                self.temperature_edit.setText(str(config.get('temperature', 1.0)))
                self.top_p_edit.setText(str(config.get('top_p', 1.0)))
                self.proxy_edit.setText(config.get('proxy', ''))
                # 同步角色胶囊状态（chips 只连 clicked，setChecked 不会触发回调）
                idx = self.configs.index(config)
                self.default_chip.setChecked(idx == self.default_index)
                self.completion_chip.setChecked(bool(config.get('for_completion')))
                self.git_chip.setChecked(bool(config.get('for_git')))
                self._block_signals(False)
                self.editor_panel.setEnabled(True)
                return
        # 清空编辑区
        self._block_signals(True)
        self._clear_edit_fields()
        self._block_signals(False)
        self.editor_panel.setEnabled(False)

    def _block_signals(self, block):
        """阻止或恢复编辑控件的信号"""
        self.name_edit.blockSignals(block)
        self.api_base_edit.blockSignals(block)
        self.api_key_edit.blockSignals(block)
        self.model_edit.blockSignals(block)
        self.timeout_edit.blockSignals(block)
        self.max_tokens_edit.blockSignals(block)
        self.temperature_edit.blockSignals(block)
        self.top_p_edit.blockSignals(block)
        self.proxy_edit.blockSignals(block)

    def _clear_edit_fields(self):
        """清空编辑字段"""
        self.name_edit.clear()
        self.api_base_edit.clear()
        self.api_key_edit.clear()
        self.model_edit.clear()
        self.timeout_edit.clear()
        self.max_tokens_edit.clear()
        self.temperature_edit.clear()
        self.top_p_edit.clear()
        self.proxy_edit.clear()
        self.default_chip.setChecked(False)
        self.completion_chip.setChecked(False)
        self.git_chip.setChecked(False)

    def _on_name_changed(self, text):
        if self._closing:
            return
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        config = self._get_config_by_id(cid) if cid else None
        if config:
            config['name'] = text
            # 更新列表显示（保留默认/补全/Git 标记）
            item.setText(self._item_label(config))

    def _on_field_changed(self):
        """同步当前编辑的字段到配置"""
        if self._closing:
            return
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        config = self._get_config_by_id(cid) if cid else None
        if config:
            config['api_base'] = self.api_base_edit.text()
            config['api_key'] = self.api_key_edit.text()
            config['model'] = self.model_edit.text()
            # 数值字段需要验证和转换
            try:
                config['timeout'] = int(self.timeout_edit.text()) if self.timeout_edit.text() else 30
            except ValueError:
                config['timeout'] = 30
            try:
                config['max_tokens'] = int(self.max_tokens_edit.text()) if self.max_tokens_edit.text() else 4096
            except ValueError:
                config['max_tokens'] = 4096
            try:
                temp = float(self.temperature_edit.text()) if self.temperature_edit.text() else 1.0
                config['temperature'] = max(0.0, min(2.0, temp))  # 限制范围 0-2
            except ValueError:
                config['temperature'] = 1.0
            try:
                top_p = float(self.top_p_edit.text()) if self.top_p_edit.text() else 1.0
                config['top_p'] = max(0.0, min(1.0, top_p))  # 限制范围 0-1
            except ValueError:
                config['top_p'] = 1.0
            config['proxy'] = self.proxy_edit.text()

    def _toggle_api_key_visibility(self):
        """切换 API Key 的显示/隐藏"""
        self._api_key_visible = not self._api_key_visible
        if self._api_key_visible:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.toggle_key_btn.setText(t("llm.hide_key"))
        else:
            self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_key_btn.setText(t("llm.show_key"))

    def _add_config(self):
        cid = self._next_config_id()
        new_config = self.DEFAULT_CONFIG.copy()
        new_config['_id'] = cid
        new_config['name'] = t("llm.default_name", n=len(self.configs) + 1)
        self.configs.append(new_config)
        self._config_map[cid] = new_config
        item = QListWidgetItem(new_config['name'])
        item.setData(Qt.ItemDataRole.UserRole, cid)
        self.config_list.addItem(item)
        self.config_list.setCurrentRow(len(self.configs) - 1)

    def _delete_config(self):
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        config = self._get_config_by_id(cid) if cid else None
        if config and config in self.configs:
            name = config.get('name') or t("llm.unnamed")
            ret = QMessageBox.question(
                self, t("llm.delete_confirm_title"),
                t("llm.delete_confirm", name=name))
            if ret != QMessageBox.StandardButton.Yes:
                return
            config_index = self.configs.index(config)
            self.configs.remove(config)
            if cid in self._config_map:
                del self._config_map[cid]
            self.config_list.takeItem(row)
            # 调整默认索引
            if self.default_index == config_index:
                self.default_index = 0 if self.configs else -1
            elif self.default_index > config_index:
                self.default_index -= 1
            # 重新填充列表以更新默认标记
            self._populate_list()

    def _copy_as_json(self):
        """将当前选中的配置复制为 JSON 到剪贴板"""
        row = self.config_list.currentRow()
        item = self.config_list.item(row) if row >= 0 else None
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        config = self._get_config_by_id(cid) if cid else None
        if not config:
            QMessageBox.warning(self, t("llm.copy_success"), t("llm.select_first"))
            return
        # 同步当前编辑内容后再导出
        self._on_field_changed()
        export = {k: v for k, v in config.items() if k != '_id'}
        text = json.dumps(export, ensure_ascii=False, indent=2)
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, t("llm.copy_success"), t("llm.copy_done"))

    def _sanitize_config(self, raw):
        """将任意 dict 规范化为合法的配置（基于默认模板，做类型转换）"""
        config = self.DEFAULT_CONFIG.copy()
        # 名称
        name = str(raw.get('name', '')).strip()
        if name:
            config['name'] = name
        # 字符串字段
        for key in ('api_base', 'api_key', 'model', 'proxy'):
            if key in raw and raw[key] is not None:
                config[key] = str(raw[key])
        # 整数字段
        for key, default in (('timeout', 30), ('max_tokens', 4096)):
            try:
                config[key] = int(raw[key]) if raw.get(key) not in (None, '') else default
            except (ValueError, TypeError):
                config[key] = default
        # 浮点字段（带范围限制）
        try:
            temp = float(raw['temperature']) if raw.get('temperature') not in (None, '') else 1.0
            config['temperature'] = max(0.0, min(2.0, temp))
        except (ValueError, TypeError):
            config['temperature'] = 1.0
        try:
            top_p = float(raw['top_p']) if raw.get('top_p') not in (None, '') else 1.0
            config['top_p'] = max(0.0, min(1.0, top_p))
        except (ValueError, TypeError):
            config['top_p'] = 1.0
        return config

    def _paste_as_json(self):
        """从剪贴板的 JSON 导入配置（支持单个对象或列表）"""
        text = QApplication.clipboard().text().strip()
        if not text:
            QMessageBox.warning(self, t("llm.paste_failed"), t("llm.clipboard_empty"))
            return
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            QMessageBox.warning(self, t("llm.paste_failed"), t("llm.invalid_json"))
            return
        # 接受单个对象或对象列表
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = [d for d in data if isinstance(d, dict)]
        else:
            items = []
        if not items:
            QMessageBox.warning(self, t("llm.paste_failed"), t("llm.invalid_config"))
            return
        last_row = -1
        for raw in items:
            config = self._sanitize_config(raw)
            cid = self._next_config_id()
            config['_id'] = cid
            self.configs.append(config)
            self._config_map[cid] = config
            list_item = QListWidgetItem(config['name'])
            list_item.setData(Qt.ItemDataRole.UserRole, cid)
            self.config_list.addItem(list_item)
            last_row = self.config_list.count() - 1
        if last_row >= 0:
            self.config_list.setCurrentRow(last_row)
        QMessageBox.information(self, t("llm.paste_success"), t("llm.paste_done", n=len(items)))

    def _on_rows_moved(self, parent, start, end, destination, row):
        """拖拽排序后同步 configs 列表顺序"""
        if self._closing:
            return
        # 记录当前默认配置的 ID
        default_config_id = None
        if 0 <= self.default_index < len(self.configs):
            default_config_id = self.configs[self.default_index].get('_id')
        # 根据 item 的新顺序重新排列 configs 列表
        new_configs = []
        for i in range(self.config_list.count()):
            item = self.config_list.item(i)
            cid = item.data(Qt.ItemDataRole.UserRole)
            config = self._get_config_by_id(cid) if cid else None
            if config:
                new_configs.append(config)
        self.configs = new_configs
        # 更新默认索引
        if default_config_id:
            for i, c in enumerate(self.configs):
                if c.get('_id') == default_config_id:
                    self.default_index = i
                    break
        # 更新列表显示
        self._populate_list()

    def accept(self):
        """重写 accept 方法，确保当前编辑在关闭前被保存"""
        self._closing = True
        # 保存当前编辑
        self._on_field_changed()
        super().accept()

    def get_configs(self) -> list:
        """获取配置列表（不包含内部 _id 字段）"""
        result = []
        for c in self.configs:
            config_copy = {k: v for k, v in c.items() if k != '_id'}
            result.append(config_copy)
        return result

    def get_default_index(self) -> int:
        """获取默认配置索引"""
        return self.default_index if self.default_index >= 0 else 0


class DirectoryHistoryDialog(QDialog):
    """快速启动路径管理对话框"""

    def __init__(self, directories: list, parent=None):
        super().__init__(parent)
        self.directories = directories.copy()  # 复制列表
        self.setWindowTitle(t("dirhistory.title"))
        self.setMinimumSize(600, 400)
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        # 主布局
        main_layout = QVBoxLayout(self)

        # 说明文字
        hint_label = QLabel(t("dirhistory.hint"))
        hint_label.setStyleSheet("color: #888; margin-bottom: 8px;")
        main_layout.addWidget(hint_label)

        # 内容区域（水平布局）
        content_layout = QHBoxLayout()

        # 左侧：目录列表
        left_layout = QVBoxLayout()
        left_layout.addWidget(QLabel(t("dirhistory.list_label")))

        self.dir_list = QListWidget()
        self.dir_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.dir_list.currentRowChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.dir_list)

        content_layout.addLayout(left_layout, 3)

        # 右侧：操作按钮
        right_layout = QVBoxLayout()
        right_layout.addStretch()

        self.add_btn = QPushButton(t("dirhistory.add"))
        self.add_btn.clicked.connect(self._add_directory)
        right_layout.addWidget(self.add_btn)

        self.delete_btn = QPushButton(t("dirhistory.delete"))
        self.delete_btn.clicked.connect(self._delete_directory)
        right_layout.addWidget(self.delete_btn)

        right_layout.addSpacing(20)

        self.up_btn = QPushButton(t("dirhistory.move_up"))
        self.up_btn.clicked.connect(self._move_up)
        right_layout.addWidget(self.up_btn)

        self.down_btn = QPushButton(t("dirhistory.move_down"))
        self.down_btn.clicked.connect(self._move_down)
        right_layout.addWidget(self.down_btn)

        right_layout.addStretch()

        content_layout.addLayout(right_layout, 1)

        main_layout.addLayout(content_layout)

        # 底部按钮
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # 填充列表
        self._populate_list()
        self._update_buttons()

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
                color: #eaeaea;
            }
            QLabel {
                color: #aaa;
            }
            QListWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                color: #eaeaea;
                padding: 4px;
            }
            QListWidget::item {
                padding: 6px;
                border-radius: 3px;
            }
            QListWidget::item:selected {
                background-color: #667eea;
            }
            QPushButton {
                background-color: #3d3d5c;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                color: #eaeaea;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #4d4d6c;
            }
            QPushButton:disabled {
                background-color: #2d2d4c;
                color: #666;
            }
        """)

    def _populate_list(self):
        self.dir_list.clear()
        for dir_path in self.directories:
            # 显示目录名和完整路径
            dir_name = os.path.basename(dir_path) or dir_path
            item = QListWidgetItem(f"📁 {dir_name}")
            item.setToolTip(dir_path)
            item.setData(Qt.ItemDataRole.UserRole, dir_path)
            self.dir_list.addItem(item)
        if self.directories:
            self.dir_list.setCurrentRow(0)

    def _on_selection_changed(self, row):
        self._update_buttons()

    def _update_buttons(self):
        row = self.dir_list.currentRow()
        count = len(self.directories)
        has_selection = row >= 0

        self.delete_btn.setEnabled(has_selection)
        self.up_btn.setEnabled(has_selection and row > 0)
        self.down_btn.setEnabled(has_selection and row < count - 1)

    def _add_directory(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            t("dirhistory.select_dir"),
            str(Path.home())
        )
        if dir_path:
            # 检查是否已存在
            if dir_path in self.directories:
                QMessageBox.information(self, t("dirhistory.already_exists_title"), t("dirhistory.already_exists"))
                # 选中已存在的项
                for i, d in enumerate(self.directories):
                    if d == dir_path:
                        self.dir_list.setCurrentRow(i)
                        break
                return

            # 添加到列表开头
            self.directories.insert(0, dir_path)
            self._populate_list()
            self.dir_list.setCurrentRow(0)

    def _delete_directory(self):
        row = self.dir_list.currentRow()
        if 0 <= row < len(self.directories):
            dir_path = self.directories[row]
            reply = QMessageBox.question(
                self, t("dirhistory.confirm_delete_title"),
                t("dirhistory.confirm_delete_msg", path=dir_path),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.directories.pop(row)
                self.dir_list.takeItem(row)
                # 选中下一项或上一项
                if self.directories:
                    new_row = min(row, len(self.directories) - 1)
                    self.dir_list.setCurrentRow(new_row)
                self._update_buttons()

    def _move_up(self):
        row = self.dir_list.currentRow()
        if row > 0:
            # 交换位置
            self.directories[row], self.directories[row - 1] = \
                self.directories[row - 1], self.directories[row]
            self._populate_list()
            self.dir_list.setCurrentRow(row - 1)

    def _move_down(self):
        row = self.dir_list.currentRow()
        if row < len(self.directories) - 1:
            # 交换位置
            self.directories[row], self.directories[row + 1] = \
                self.directories[row + 1], self.directories[row]
            self._populate_list()
            self.dir_list.setCurrentRow(row + 1)

    def get_directories(self):
        return self.directories

    def accept(self):
        """重写accept方法，关闭前先隐藏防止闪烁"""
        self.setWindowOpacity(0)
        super().accept()

    def reject(self):
        """重写reject方法，关闭前先隐藏防止闪烁"""
        self.setWindowOpacity(0)
        super().reject()


class _ShortcutCaptureButton(QPushButton):
    """显示一个快捷键；点击后进入录制态，按下组合键即捕获。

    - 单独的修饰键会被忽略，直到按下真正的主键。
    - Backspace / Delete 清空该项。
    - Esc 取消本次录制，保留原值。
    """
    captured = pyqtSignal(str)  # 发出新的键序列（"" 表示清空）

    _MOD_KEYS = (Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta)

    def __init__(self, seq="", parent=None):
        super().__init__(parent)
        self._seq = seq or ""
        self._recording = False
        self.setCheckable(True)
        self.setMinimumWidth(180)
        self.clicked.connect(self._toggle_record)
        self._refresh()

    def sequence(self):
        return self._seq

    def set_sequence(self, seq):
        self._seq = seq or ""
        self._refresh()

    def _toggle_record(self):
        if self.isChecked():
            self._recording = True
            self.setText(t("shortcuts.recording"))
            self.grabKeyboard()
        else:
            self._stop_record()

    def _stop_record(self):
        self._recording = False
        self.setChecked(False)
        self.releaseKeyboard()
        self._refresh()

    def _refresh(self):
        if self._recording:
            return
        if self._seq:
            self.setText(QKeySequence(self._seq).toString(
                QKeySequence.SequenceFormat.NativeText))
        else:
            self.setText(t("shortcuts.unset"))

    def keyPressEvent(self, event):
        if not self._recording:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in self._MOD_KEYS:
            return  # 等待主键
        if key == Qt.Key.Key_Escape:
            self._stop_record()
            return
        if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            self._seq = ""
            self._stop_record()
            self.captured.emit("")
            return
        mods = event.modifiers()
        seq_text = QKeySequence(key | int(mods.value)).toString()
        if seq_text:
            self._seq = seq_text
            self._stop_record()
            self.captured.emit(self._seq)

    def focusOutEvent(self, event):
        if self._recording:
            self._stop_record()
        super().focusOutEvent(event)


class ShortcutSettingsDialog(QDialog):
    """键盘快捷键自定义对话框。

    specs: [(action_id, default_seq, label_key, slot_name), ...]
    current: {action_id: 当前生效的键序列}
    """

    def __init__(self, specs, current, parent=None):
        super().__init__(parent)
        self._specs = list(specs)
        self._defaults = {s[0]: s[1] for s in self._specs}
        self._labels = {s[0]: t(s[2]) for s in self._specs}
        self._seqs = {s[0]: current.get(s[0], s[1]) for s in self._specs}
        self._buttons = {}
        self.setWindowTitle(t("shortcuts.dialog_title"))
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        hint = QLabel(t("shortcuts.hint"))
        hint.setWordWrap(True)
        hint.setObjectName("scHint")
        layout.addWidget(hint)

        # 表头
        header = QHBoxLayout()
        h_action = QLabel(t("shortcuts.col_action"))
        h_action.setObjectName("scHeader")
        h_seq = QLabel(t("shortcuts.col_shortcut"))
        h_seq.setObjectName("scHeader")
        header.addWidget(h_action, 1)
        header.addWidget(h_seq, 0)
        layout.addLayout(header)

        # 每个操作一行
        for action_id, _default, label_key, _slot in self._specs:
            row = QHBoxLayout()
            name = QLabel(t(label_key))
            name.setObjectName("scName")
            btn = _ShortcutCaptureButton(self._seqs.get(action_id, ""))
            btn.captured.connect(lambda seq, a=action_id: self._on_captured(a, seq))
            self._buttons[action_id] = btn
            row.addWidget(name, 1)
            row.addWidget(btn, 0)
            layout.addLayout(row)

        layout.addStretch(1)

        # 底部按钮
        btn_row = QHBoxLayout()
        reset_btn = QPushButton(t("shortcuts.reset_all"))
        reset_btn.setObjectName("scReset")
        reset_btn.clicked.connect(self._reset_all)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        cancel_btn = QPushButton(t("shortcuts.cancel"))
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton(t("shortcuts.save"))
        save_btn.setObjectName("scSave")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _on_captured(self, action_id, seq):
        # 冲突检测：与其它操作重复则警告并还原
        if seq:
            for other_id, other_seq in self._seqs.items():
                if other_id != action_id and other_seq and other_seq == seq:
                    QMessageBox.warning(
                        self, t("shortcuts.dialog_title"),
                        t("shortcuts.conflict",
                          seq=QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText),
                          action=self._labels.get(other_id, other_id)))
                    self._buttons[action_id].set_sequence(self._seqs.get(action_id, ""))
                    return
        self._seqs[action_id] = seq

    def _reset_all(self):
        for action_id, default_seq in self._defaults.items():
            self._seqs[action_id] = default_seq
            self._buttons[action_id].set_sequence(default_seq)

    def get_shortcuts(self):
        return dict(self._seqs)

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog { background-color: #1a1a2e; color: #eaeaea; }
            QLabel { color: #eaeaea; }
            QLabel#scHint { color: #9aa; }
            QLabel#scHeader { color: #888; font-weight: bold; }
            QLabel#scName { color: #eaeaea; }
            QPushButton {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 6px 12px;
                color: #eaeaea;
            }
            QPushButton:hover { border-color: #667eea; }
            QPushButton:checked {
                background-color: #667eea;
                border-color: #667eea;
                color: white;
            }
            QPushButton#scSave {
                background-color: #2563eb;
                border: none;
            }
            QPushButton#scSave:hover { background-color: #3b76f0; }
            QPushButton#scReset { background-color: #3d3d5c; border: none; }
            QPushButton#scReset:hover { background-color: #4d4d6c; }
        """)


class ShortcutCheatSheetDialog(QDialog):
    """快捷键速查表（只读、可搜索、非模态）。

    groups: [(分组标题, [(快捷键显示文本, 描述), ...]), ...]
    键位文本由调用方按平台原生格式渲染好——「全局」组取的是当前生效值
    （含用户自定义覆盖），所以速查表不会和实际行为漂移。
    """

    customize_requested = pyqtSignal()

    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self._groups = list(groups)
        self.setWindowTitle(t("shortcuts.cheatsheet_title"))
        self.resize(560, 680)
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t("shortcuts.cheatsheet_search"))
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_input)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tree.setUniformRowHeights(True)
        group_font = QFont()
        group_font.setBold(True)
        for title, rows in self._groups:
            group_item = QTreeWidgetItem([title, ""])
            group_item.setFont(0, group_font)
            group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(group_item)
            for keys, desc in rows:
                child = QTreeWidgetItem([desc, keys])
                child.setFlags(Qt.ItemFlag.ItemIsEnabled)
                child.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                group_item.addChild(child)
        self.tree.expandAll()
        self.tree.setColumnWidth(0, 360)
        layout.addWidget(self.tree, 1)

        btn_row = QHBoxLayout()
        customize_btn = QPushButton(t("shortcuts.cheatsheet_customize"))
        customize_btn.setObjectName("scReset")
        customize_btn.clicked.connect(self.customize_requested.emit)
        btn_row.addWidget(customize_btn)
        btn_row.addStretch(1)
        close_btn = QPushButton(t("shortcuts.cheatsheet_close"))
        close_btn.setObjectName("scSave")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _apply_filter(self, text: str):
        """空格分词、大小写不敏感的过滤：描述或键位包含全部词条才显示；
        组内全部被滤掉时连组标题一起隐藏。"""
        tokens = [tok for tok in text.lower().split() if tok]
        for gi in range(self.tree.topLevelItemCount()):
            group_item = self.tree.topLevelItem(gi)
            visible_children = 0
            for ci in range(group_item.childCount()):
                child = group_item.child(ci)
                haystack = (child.text(0) + " " + child.text(1)).lower()
                match = all(tok in haystack for tok in tokens)
                child.setHidden(not match)
                if match:
                    visible_children += 1
            group_item.setHidden(visible_children == 0)

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog { background-color: #1a1a2e; color: #eaeaea; }
            QLineEdit {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 6px 10px;
                color: #eaeaea;
            }
            QLineEdit:focus { border-color: #667eea; }
            QTreeWidget {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                color: #eaeaea;
                outline: none;
            }
            QTreeWidget::item { padding: 4px 6px; }
            QPushButton {
                background-color: #16213e;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 6px 12px;
                color: #eaeaea;
            }
            QPushButton:hover { border-color: #667eea; }
            QPushButton#scSave { background-color: #2563eb; border: none; }
            QPushButton#scSave:hover { background-color: #3b76f0; }
            QPushButton#scReset { background-color: #3d3d5c; border: none; }
            QPushButton#scReset:hover { background-color: #4d4d6c; }
        """)
