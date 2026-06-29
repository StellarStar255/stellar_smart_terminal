"""
历史记录对话框
显示和管理历史会话
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QLabel, QMessageBox,
    QHeaderView, QTextEdit, QSplitter, QWidget, QComboBox, QSizePolicy
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor

from session_manager import SessionManager, Session
from exporter import export_session
from utils import strip_ansi
from i18n import t


class HistoryDialog(QDialog):
    """历史记录对话框"""

    def __init__(self, session_manager: SessionManager, parent=None):
        super().__init__(parent)
        self.session_manager = session_manager
        self.current_session: Session = None

        self._setup_ui()
        self._load_sessions()

    def _setup_ui(self):
        """设置UI"""
        self.setWindowTitle(t("history.title"))
        self.setMinimumSize(900, 600)

        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
            }
            QTableWidget {
                background-color: #16213e;
                color: #eaeaea;
                border: none;
                gridline-color: #2d2d44;
                selection-background-color: #3d5a80;
            }
            QTableWidget::item {
                padding: 10px;
            }
            QHeaderView::section {
                background-color: #2d2d44;
                color: #eaeaea;
                padding: 10px;
                border: none;
                font-weight: bold;
            }
            QPushButton {
                background-color: #667eea;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 10px 20px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #764ba2;
            }
            QPushButton:disabled {
                background-color: #444;
            }
            QPushButton#deleteBtn {
                background-color: #dc3545;
            }
            QPushButton#deleteBtn:hover {
                background-color: #c82333;
            }
            QTextEdit {
                background-color: #16213e;
                color: #eaeaea;
                border: none;
                font-family: Monaco, Consolas, monospace;
                font-size: 12px;
            }
            QLabel {
                color: #eaeaea;
            }
            QComboBox {
                background-color: #2d2d44;
                color: #eaeaea;
                border: none;
                border-radius: 5px;
                padding: 8px;
            }
        """)

        layout = QVBoxLayout(self)

        # 标题（固定高度：否则它与 splitter 同为 Preferred 策略，会平分多余垂直空间，
        # 白占半个对话框，把下面的表格/预览挤到看不全）
        title = QLabel(t("history.main_title"))
        title.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #667eea; margin-bottom: 10px;")
        title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(title)

        # 分割器
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：会话列表
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels([t("history.col_session_id"), t("history.col_command"), t("history.col_start_time"), t("history.col_entry_count")])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        left_layout.addWidget(self.table)

        # 操作按钮
        btn_layout = QHBoxLayout()

        self.view_btn = QPushButton(t("history.view_detail"))
        self.view_btn.clicked.connect(self._view_session)
        self.view_btn.setEnabled(False)
        btn_layout.addWidget(self.view_btn)

        self.export_btn = QPushButton(t("history.export"))
        self.export_btn.clicked.connect(self._export_session)
        self.export_btn.setEnabled(False)
        btn_layout.addWidget(self.export_btn)

        self.delete_btn = QPushButton(t("history.delete"))
        self.delete_btn.setObjectName("deleteBtn")
        self.delete_btn.clicked.connect(self._delete_session)
        self.delete_btn.setEnabled(False)
        btn_layout.addWidget(self.delete_btn)

        left_layout.addLayout(btn_layout)

        splitter.addWidget(left_widget)

        # 右侧：预览
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        preview_label = QLabel(t("history.preview_title"))
        preview_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        right_layout.addWidget(preview_label)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText(t("history.preview_placeholder"))
        right_layout.addWidget(self.preview)

        splitter.addWidget(right_widget)
        splitter.setSizes([400, 500])

        # 伸缩因子 1：多余垂直空间全归 splitter，表格/预览撑满，标题不再抢高度
        layout.addWidget(splitter, 1)

        # 关闭按钮
        close_layout = QHBoxLayout()
        close_layout.addStretch()

        refresh_btn = QPushButton(t("history.refresh"))
        refresh_btn.clicked.connect(self._load_sessions)
        close_layout.addWidget(refresh_btn)

        close_btn = QPushButton(t("history.close"))
        close_btn.clicked.connect(self.close)
        close_layout.addWidget(close_btn)

        layout.addLayout(close_layout)

    def _load_sessions(self):
        """加载会话列表"""
        self.table.setRowCount(0)
        sessions = self.session_manager.list_sessions()

        for session in sessions:
            row = self.table.rowCount()
            self.table.insertRow(row)

            self.table.setItem(row, 0, QTableWidgetItem(session['session_id']))
            self.table.setItem(row, 1, QTableWidgetItem(session['command']))
            self.table.setItem(row, 2, QTableWidgetItem(session['start_time']))
            self.table.setItem(row, 3, QTableWidgetItem(str(session['entry_count'])))

    def _on_selection_changed(self):
        """选择变化"""
        selected = self.table.selectedItems()
        has_selection = len(selected) > 0

        self.view_btn.setEnabled(has_selection)
        self.export_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection)

        if has_selection:
            row = self.table.currentRow()
            session_id = self.table.item(row, 0).text()
            self._preview_session(session_id)

    def _preview_session(self, session_id: str):
        """预览会话"""
        session = self.session_manager.load_session(session_id)
        if not session:
            return

        self.current_session = session

        # 生成预览文本
        lines = [
            t("history.session_id_label", id=session.session_id),
            t("history.command_label", cmd=session.command),
            t("history.working_dir_label", dir=session.working_directory),
            t("history.start_time_label", time=session.start_time),
            t("history.end_time_label", time=session.end_time or 'N/A'),
            t("history.entry_count_label", n=len(session.entries)),
            "",
            "=" * 50,
            ""
        ]

        # 显示前几个条目
        for i, entry in enumerate(session.entries[:20]):
            entry_type = "[INPUT]" if entry.type == 'input' else "[OUTPUT]"
            content = strip_ansi(entry.content)
            if len(content) > 200:
                content = content[:200] + "..."

            lines.append(f"{entry_type} {entry.timestamp}")
            lines.append(content)
            lines.append("")

        if len(session.entries) > 20:
            lines.append(t("history.more_entries", n=len(session.entries) - 20))

        self.preview.setText('\n'.join(lines))

    def _view_session(self):
        """查看会话详情"""
        if not self.current_session:
            return

        # 创建详情对话框
        dialog = SessionDetailDialog(self.current_session, self)
        dialog.exec()

    def _export_session(self):
        """导出会话"""
        if not self.current_session:
            return

        from PyQt6.QtWidgets import QInputDialog

        fmt_html = t("history.format_html")
        fmt_md = t("history.format_markdown")
        fmt_json = t("history.format_json")
        fmt_all = t("history.format_all")
        formats = [fmt_html, fmt_md, fmt_json, fmt_all]
        format_choice, ok = QInputDialog.getItem(
            self, t("history.export_format_title"), t("history.export_format_prompt"),
            formats, 0, False
        )

        if ok:
            format_map = {
                fmt_html: ['html'],
                fmt_md: ['markdown'],
                fmt_json: ['json'],
                fmt_all: ['html', 'markdown', 'json']
            }

            for fmt in format_map.get(format_choice, ['html']):
                try:
                    output_path = export_session(self.current_session, fmt, open_after=(fmt == 'html'))
                    QMessageBox.information(self, t("history.export_success_title"), t("history.export_success_msg", path=output_path))
                except Exception as e:
                    QMessageBox.critical(self, t("history.export_failed_title"), str(e))

    def _delete_session(self):
        """删除会话"""
        row = self.table.currentRow()
        if row < 0:
            return

        session_id = self.table.item(row, 0).text()

        reply = QMessageBox.question(
            self, t("history.confirm_delete_title"),
            t("history.confirm_delete_msg", id=session_id),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if self.session_manager.delete_session(session_id):
                self._load_sessions()
                self.preview.clear()
                self.current_session = None
                QMessageBox.information(self, t("history.delete_success_title"), t("history.delete_success_msg"))
            else:
                QMessageBox.critical(self, t("history.delete_failed_title"), t("history.delete_failed_msg"))


class SessionDetailDialog(QDialog):
    """会话详情对话框"""

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session

        self.setWindowTitle(t("session_detail.title", id=session.session_id))
        self.setMinimumSize(800, 600)

        self.setStyleSheet("""
            QDialog {
                background-color: #1a1a2e;
            }
            QTextEdit {
                background-color: #16213e;
                color: #eaeaea;
                border: none;
                font-family: Monaco, Consolas, monospace;
                font-size: 13px;
                padding: 15px;
            }
            QLabel {
                color: #eaeaea;
            }
            QPushButton {
                background-color: #667eea;
                color: white;
                border: none;
                border-radius: 5px;
                padding: 10px 20px;
            }
            QPushButton:hover {
                background-color: #764ba2;
            }
        """)

        layout = QVBoxLayout(self)

        # 元信息
        info_text = "\n".join([
            "",
            t("session_detail.session_id", id=session.session_id),
            t("session_detail.command", cmd=session.command),
            t("session_detail.working_dir", dir=session.working_directory),
            t("session_detail.start_time", time=session.start_time),
            t("session_detail.end_time", time=session.end_time or 'N/A'),
            t("session_detail.total_entries", n=len(session.entries)),
            t("session_detail.detected_files", n=len(session.get_all_files())),
            "",
        ])
        info_label = QLabel(info_text)
        info_label.setStyleSheet("background-color: #16213e; padding: 15px; border-radius: 5px;")
        layout.addWidget(info_label)

        # 内容
        self.content = QTextEdit()
        self.content.setReadOnly(True)

        # 生成内容
        lines = []
        for i, entry in enumerate(session.entries):
            entry_type = "═══ INPUT ═══" if entry.type == 'input' else "─── OUTPUT ───"
            content = strip_ansi(entry.content)

            lines.append(f"\n{entry_type} [{i + 1}] {entry.timestamp}")
            lines.append(content)

            if entry.files:
                lines.append("\n📎 " + t("session_detail.files_label", files=', '.join(entry.files)))

        self.content.setText('\n'.join(lines))
        layout.addWidget(self.content)

        # 关闭按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        close_btn = QPushButton(t("session_detail.close"))
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)
