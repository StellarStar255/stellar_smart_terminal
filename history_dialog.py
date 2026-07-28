"""
历史记录对话框
显示和管理历史会话
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QLabel, QLineEdit, QMessageBox,
    QHeaderView, QTextEdit, QSplitter, QWidget, QSizePolicy
)
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from session_manager import SessionManager, Session
from exporter import export_session
from utils import strip_ansi
from i18n import t


# 正在运行的后台加载线程的强引用：对话框中途关闭时 worker 可能仍在跑，
# 只靠对话框成员引用会被 GC 连带销毁运行中的 QThread（崩溃）
_ACTIVE_WORKERS = set()

# 预览区每条内容的展示长度，以及 strip_ansi 的处理片段上限
# （单条合并 output 可达 10MB，全量去 ANSI 只为了看前 200 字不值）
_PREVIEW_SNIPPET_CHARS = 200
_PREVIEW_STRIP_SLICE = 4096
_PREVIEW_MAX_ENTRIES = 20

# 详情对话框的截断上限：QTextEdit 塞多兆文本本身就会卡死界面，
# 详情用于快速目检，完整内容走导出
_DETAIL_ENTRY_CAP = 20_000
_DETAIL_TOTAL_CAP = 500_000


def _build_preview_text(session: Session) -> str:
    """生成左侧预览文本（纯逻辑，供后台线程调用）"""
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

    for entry in session.entries[:_PREVIEW_MAX_ENTRIES]:
        entry_type = "[INPUT]" if entry.type == 'input' else "[OUTPUT]"
        raw = entry.content
        content = strip_ansi(raw[:_PREVIEW_STRIP_SLICE])
        if len(content) > _PREVIEW_SNIPPET_CHARS or len(raw) > _PREVIEW_STRIP_SLICE:
            content = content[:_PREVIEW_SNIPPET_CHARS] + "..."

        lines.append(f"{entry_type} {entry.timestamp}")
        lines.append(content)
        lines.append("")

    if len(session.entries) > _PREVIEW_MAX_ENTRIES:
        lines.append(t("history.more_entries",
                       n=len(session.entries) - _PREVIEW_MAX_ENTRIES))
    return '\n'.join(lines)


def _build_detail_text(session: Session) -> str:
    """生成详情对话框正文，带单条/总量截断（纯逻辑，可单测）"""
    lines = []
    total = 0
    for i, entry in enumerate(session.entries):
        entry_type = "═══ INPUT ═══" if entry.type == 'input' else "─── OUTPUT ───"
        raw = entry.content
        omitted = len(raw) - _DETAIL_ENTRY_CAP
        if omitted > 0:
            content = (strip_ansi(raw[:_DETAIL_ENTRY_CAP]) + '\n'
                       + t("session_detail.entry_truncated", n=omitted))
        else:
            content = strip_ansi(raw)

        lines.append(f"\n{entry_type} [{i + 1}] {entry.timestamp}")
        lines.append(content)

        if entry.files:
            lines.append("\n📎 " + t("session_detail.files_label",
                                     files=', '.join(entry.files)))

        total += len(content)
        if total >= _DETAIL_TOTAL_CAP and i + 1 < len(session.entries):
            lines.append(t("session_detail.entries_omitted",
                           n=len(session.entries) - i - 1))
            break
    return '\n'.join(lines)


class _SessionListWorker(QThread):
    """后台加载会话摘要列表。

    list_sessions 首次要整文件解析所有会话（单个可达 10MB），
    放 GUI 线程会让历史对话框打开时明显卡顿。
    """
    loaded = pyqtSignal(list)

    def __init__(self, manager: SessionManager):
        super().__init__()
        self._manager = manager

    def run(self):
        try:
            sessions = self._manager.list_sessions()
        except Exception:
            sessions = []
        self.loaded.emit(sessions)


class _SessionPreviewWorker(QThread):
    """后台加载单个会话并生成预览文本（点选表格行触发，整文件解析可达 10MB）"""
    loaded = pyqtSignal(str, object, str)  # (session_id, Session|None, preview_text)

    def __init__(self, manager: SessionManager, session_id: str):
        super().__init__()
        self._manager = manager
        self._session_id = session_id

    def run(self):
        try:
            session = self._manager.load_session(self._session_id)
            text = _build_preview_text(session) if session else ""
        except Exception:
            session, text = None, ""
        self.loaded.emit(self._session_id, session, text)


class _SessionSearchWorker(QThread):
    """后台全局搜索：在全部历史会话的输入/输出里搜关键词。

    可达数百 MB 的会话目录整体扫描必须离开 GUI 线程；seq 用于
    latest-wins（结果到达时若已发起新搜索则丢弃），_cancelled 让
    被淘汰的旧扫描尽快退出。
    """
    done = pyqtSignal(int, list)  # (seq, results)

    def __init__(self, manager: SessionManager, query: str, seq: int):
        super().__init__()
        self._manager = manager
        self._query = query
        self._seq = seq
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            results = self._manager.search_sessions(
                self._query, cancel_check=lambda: self._cancelled)
        except Exception:
            results = []
        self.done.emit(self._seq, results)


class HistoryDialog(QDialog):
    """历史记录对话框"""

    def __init__(self, session_manager: SessionManager, parent=None):
        super().__init__(parent)
        self.session_manager = session_manager
        self.current_session: Session = None
        self._list_worker = None
        self._reload_pending = False
        self._sessions_cache = []   # 最近一次加载的会话摘要（退出搜索时恢复列表）
        self._search_mode = False
        self._search_seq = 0
        self._search_worker = None

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

        # 全局搜索行：关键词在全部历史会话的命令与输出里找
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(t("history.search_placeholder"))
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setStyleSheet(
            "QLineEdit { background-color: #2d2d44; color: #eaeaea; border: none;"
            " border-radius: 5px; padding: 8px; }")
        self.search_input.textChanged.connect(self._on_search_text_changed)
        search_row.addWidget(self.search_input, 1)
        self.search_hits_label = QLabel("")
        self.search_hits_label.setStyleSheet("color: #888;")
        search_row.addWidget(self.search_hits_label)
        left_layout.addLayout(search_row)
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(400)
        self._search_debounce.timeout.connect(self._run_search)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            t("history.col_session_id"), t("history.col_command"),
            t("history.col_source"), t("history.col_start_time"),
            t("history.col_entry_count"),
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        # 列宽可拖拽（Interactive），给出合理初始宽度；来源目录列(路径长)设为 Stretch
        # 吸收剩余宽度，避免右侧留白，也不会把「条目数」这种小列拉宽。
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)  # 来源目录
        for col, width in ((0, 150), (1, 150), (3, 150), (4, 80)):
            self.table.setColumnWidth(col, width)
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
        """加载会话列表（后台线程读盘，完成后回 GUI 线程填表）"""
        if self._list_worker is not None:
            self._reload_pending = True
            return
        worker = _SessionListWorker(self.session_manager)
        self._list_worker = worker
        _ACTIVE_WORKERS.add(worker)
        worker.loaded.connect(self._on_sessions_loaded)
        worker.finished.connect(
            lambda w=worker: (_ACTIVE_WORKERS.discard(w), w.deleteLater()))
        worker.start()

    def _row_session_id(self, row):
        """取某行对应的会话 id。搜索结果模式下第 0 列显示的是时间，
        真实 id 存在 UserRole；普通列表模式两者一致。"""
        if row is None or row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if data else item.text()

    def _on_sessions_loaded(self, sessions):
        self._list_worker = None
        if self._reload_pending:
            # 加载期间又有删除/刷新请求，用新数据再跑一轮
            self._reload_pending = False
            self._load_sessions()
        self._sessions_cache = list(sessions)
        # 搜索模式下不去覆盖搜索结果表格，缓存留待退出搜索时恢复
        if self._search_mode:
            return
        self._populate_session_rows(sessions)

    def _populate_session_rows(self, sessions):
        # 记住当前选中的会话，重建后恢复
        selected_id = self._row_session_id(self.table.currentRow())

        self.table.setUpdatesEnabled(False)
        try:
            self.table.setColumnCount(5)
            self.table.setHorizontalHeaderLabels([
                t("history.col_session_id"), t("history.col_command"),
                t("history.col_source"), t("history.col_start_time"),
                t("history.col_entry_count"),
            ])
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.table.setRowCount(0)
            self.table.setRowCount(len(sessions))
            restore_row = -1
            for row, session in enumerate(sessions):
                id_item = QTableWidgetItem(session['session_id'])
                id_item.setData(Qt.ItemDataRole.UserRole, session['session_id'])
                self.table.setItem(row, 0, id_item)
                self.table.setItem(row, 1, QTableWidgetItem(session['command']))
                src = session.get('working_directory', '') or t("history.source_unknown")
                src_item = QTableWidgetItem(src)
                src_item.setToolTip(src)  # 路径较长时悬停看全
                self.table.setItem(row, 2, src_item)
                self.table.setItem(row, 3, QTableWidgetItem(session['start_time']))
                self.table.setItem(row, 4, QTableWidgetItem(str(session['entry_count'])))
                if session['session_id'] == selected_id:
                    restore_row = row
            if restore_row >= 0:
                self.table.selectRow(restore_row)
        finally:
            self.table.setUpdatesEnabled(True)

    # ---------- 全局搜索（在全部历史会话的命令与输出里找） ----------

    def _on_search_text_changed(self, text):
        if not text.strip():
            self._search_debounce.stop()
            self._exit_search_mode()
            return
        self._search_debounce.start()

    def _run_search(self):
        query = self.search_input.text().strip()
        if len(query) < 2:
            return
        self._search_seq += 1
        # 旧扫描尽快退出（结果也会因 seq 不匹配被丢弃）
        if self._search_worker is not None:
            self._search_worker.cancel()
        self.search_hits_label.setText(t("history.search_running"))
        worker = _SessionSearchWorker(self.session_manager, query, self._search_seq)
        self._search_worker = worker
        _ACTIVE_WORKERS.add(worker)
        worker.done.connect(self._on_search_done)
        worker.finished.connect(
            lambda w=worker: (_ACTIVE_WORKERS.discard(w), w.deleteLater()))
        worker.start()

    def _on_search_done(self, seq, results):
        if seq != self._search_seq or not self.search_input.text().strip():
            return  # 已有更新的搜索/已清空 → 丢弃过期结果
        self._search_worker = None
        self._search_mode = True
        self.search_hits_label.setText(t("history.search_hits", count=len(results)))
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setColumnCount(3)
            self.table.setHorizontalHeaderLabels([
                t("history.col_start_time"), t("history.col_command"),
                t("history.col_snippet"),
            ])
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.table.setColumnWidth(0, 150)
            self.table.setColumnWidth(1, 120)
            self.table.setRowCount(0)
            self.table.setRowCount(len(results))
            for row, hit in enumerate(results):
                time_item = QTableWidgetItem(hit['start_time'])
                time_item.setData(Qt.ItemDataRole.UserRole, hit['session_id'])
                self.table.setItem(row, 0, time_item)
                self.table.setItem(row, 1, QTableWidgetItem(hit['command']))
                snip_item = QTableWidgetItem(hit['snippet'])
                snip_item.setToolTip(hit['snippet'])
                self.table.setItem(row, 2, snip_item)
        finally:
            self.table.setUpdatesEnabled(True)

    def _exit_search_mode(self):
        self._search_seq += 1  # 使在途搜索的结果全部过期
        if self._search_worker is not None:
            self._search_worker.cancel()
            self._search_worker = None
        self.search_hits_label.setText("")
        if not self._search_mode:
            return
        self._search_mode = False
        self._populate_session_rows(self._sessions_cache)

    def _on_selection_changed(self):
        """选择变化"""
        selected = self.table.selectedItems()
        has_selection = len(selected) > 0

        self.delete_btn.setEnabled(has_selection)
        # 会话对象在后台加载，加载完成前查看/导出不可用
        # （否则点得快会拿到上一个会话的对象）
        self.view_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.current_session = None

        if has_selection:
            session_id = self._row_session_id(self.table.currentRow())
            if session_id:
                self._preview_session(session_id)

    def _preview_session(self, session_id: str):
        """预览会话：后台线程整文件解析 + 生成文本，完成后回填"""
        self.preview.setPlainText(t("history.preview_loading"))
        worker = _SessionPreviewWorker(self.session_manager, session_id)
        _ACTIVE_WORKERS.add(worker)
        worker.loaded.connect(self._on_preview_loaded)
        worker.finished.connect(
            lambda w=worker: (_ACTIVE_WORKERS.discard(w), w.deleteLater()))
        worker.start()

    def _on_preview_loaded(self, session_id: str, session, text: str):
        # 到达时选中已经变了 → 丢弃过期结果（快速换行选择时 latest-wins）
        if self._row_session_id(self.table.currentRow()) != session_id:
            return
        if session is None:
            self.preview.setPlainText("")
            return
        self.current_session = session
        self.preview.setPlainText(text)
        self.view_btn.setEnabled(True)
        self.export_btn.setEnabled(True)

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
        session_id = self._row_session_id(self.table.currentRow())
        if not session_id:
            return

        reply = QMessageBox.question(
            self, t("history.confirm_delete_title"),
            t("history.confirm_delete_msg", id=session_id),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            if self.session_manager.delete_session(session_id):
                self._load_sessions()
                # 搜索模式下重跑当前搜索，清掉已删会话的命中行
                if self._search_mode:
                    self._run_search()
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

        # 内容（单条/总量截断，超长会话完整内容走导出）
        self.content = QTextEdit()
        self.content.setReadOnly(True)
        self.content.setPlainText(_build_detail_text(session))
        layout.addWidget(self.content)

        # 关闭按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        close_btn = QPushButton(t("session_detail.close"))
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)
