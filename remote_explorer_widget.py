"""
Remote Explorer 面板 — VS Code Remote Explorer 风格的 SSH/SFTP 文件浏览器。

UI 有两个状态：
1) 主机列表（未连接）：列出 ~/.ssh/config 中的主机，点击 → 连接
2) 文件树（已连接）：懒加载的远程目录树，右键有文件操作菜单

文件打开走「下载到临时目录 → 用本地 FileEditorWidget 编辑 → 保存时上传回去」
的模式（透明对编辑器），UI 这边通过 file_open_requested 信号把
(host_config, remote_path, local_temp_path) 抛给主窗口。
"""
import os
import posixpath
import tempfile
import time
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem, QMenu,
    QInputDialog, QMessageBox, QStackedWidget, QFileDialog, QLineEdit,
    QApplication, QSizePolicy, QProgressDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QMimeData, QUrl
from PyQt6.QtGui import QAction, QCursor, QDrag, QShortcut, QKeySequence

from i18n import t
import explorer_clipboard
from ssh_session import HostConfig, RemoteEntry, SSHSession, parse_ssh_config


# 子项的 UserRole 数据键
_ROLE_ENTRY = Qt.ItemDataRole.UserRole
_ROLE_LOADED = Qt.ItemDataRole.UserRole + 1


class _RemoteTreeWidget(QTreeWidget):
    """支持文件拖入（上传）/ 拖出（下载到临时文件后给外部 URL）的远程文件树

    panel: 反向引用 RemoteExplorerPanel，用于实际执行 SFTP 上传/下载。
    """

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeWidget.DragDropMode.DragDrop)

    # ----- 接收外部文件 → 上传 -----

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return
        urls = event.mimeData().urls()
        local_paths = []
        for u in urls:
            if u.isLocalFile():
                p = u.toLocalFile()
                if p:
                    local_paths.append(p)
        if not local_paths:
            event.ignore()
            return
        # 找到落点对应的目标目录：落在目录上 → 进入它；落在文件 / 空白上 → 当前路径
        target_item = self.itemAt(event.position().toPoint())
        target_dir = None
        if target_item is not None:
            entry: RemoteEntry = target_item.data(0, _ROLE_ENTRY)
            if entry and entry.is_dir:
                target_dir = entry.path
            elif entry:
                # 文件 → 用其所在目录
                target_dir = posixpath.dirname(entry.path.rstrip("/")) or "/"
        if not target_dir:
            target_dir = self._panel._current_path
        event.acceptProposedAction()
        self._panel._handle_drop_upload(local_paths, target_dir, target_item)

    # ----- 拖出 → 把远程文件下载到本地临时并发出 file URL -----

    def startDrag(self, supported_actions):
        items = self.selectedItems()
        if not items:
            return
        entries = [it.data(0, _ROLE_ENTRY) for it in items]
        entries = [e for e in entries if e is not None and not e.is_dir]
        # MVP：只支持拖出文件，不支持目录（避免递归大下载）
        if not entries:
            return
        local_paths = self._panel._sync_download_for_drag(entries)
        if not local_paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in local_paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class RemoteExplorerPanel(QWidget):
    """远程文件浏览面板"""

    # 信号：请求在编辑器中打开一个已下载到本地临时位置的远程文件
    # 参数: (host_alias, remote_path, local_temp_path, session)
    file_open_requested = pyqtSignal(str, str, str, object)
    # 错误展示（让主窗口统一弹消息或刷新状态栏）
    error_occurred = pyqtSignal(str)
    # 主机连接成功 → 主窗口可以开一个 SSH 终端 tab 进去
    # 参数: HostConfig
    host_connected = pyqtSignal(object)
    # 在指定远程目录打开 SSH 终端（右键菜单触发）
    # 参数: (HostConfig, remote_path)
    open_terminal_at = pyqtSignal(object, str)
    # —— 内部信号：用于把 SSH 工作线程的结果安全派发回 UI 线程
    # （直接 QTimer.singleShot 在没有事件循环的工作线程里不会触发）
    _top_level_ready = pyqtSignal(list)
    _subtree_ready = pyqtSignal(object, list)         # (parent_item, entries)
    _error_signal = pyqtSignal(str)
    _file_downloaded = pyqtSignal(str, str, str, bytes)  # host_alias, remote_path, local_path, data
    _stat_resolved = pyqtSignal(object, str)          # (entry, requested_path)
    _refresh_root_signal = pyqtSignal()
    _refresh_subtree_signal = pyqtSignal(object, str)  # (item, path)

    def __init__(self, theme: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme = theme or {}
        self._session: Optional[SSHSession] = None
        self._current_path: str = "/"
        self._hosts: list[HostConfig] = []
        # 主窗口可用 set_extra_hosts() 注入手工添加的主机
        self._extra_hosts: list[HostConfig] = []
        # 维护已下载的临时文件 -> (host_alias, remote_path) 映射，
        # 便于编辑器保存时调度上传
        self._open_temp_map: dict[str, tuple[str, str]] = {}

        self._setup_ui()
        self._apply_theme()
        self._reload_hosts()

        # 内部跨线程信号 → UI 线程槽（QueuedConnection 自动应用）
        self._top_level_ready.connect(self._apply_top_level)
        self._subtree_ready.connect(self._apply_children)
        self._error_signal.connect(self._toast_error)
        self._file_downloaded.connect(self._on_file_downloaded)
        self._stat_resolved.connect(self._on_stat_resolved)
        self._refresh_root_signal.connect(self._populate_tree_root)
        self._refresh_subtree_signal.connect(self._reload_subtree)
        self._password_prompt_signal.connect(self._on_password_prompt)

    # ---------- UI ----------

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部标题栏
        self._header = QFrame()
        self._header.setFixedHeight(36)
        h_layout = QHBoxLayout(self._header)
        h_layout.setContentsMargins(10, 0, 8, 0)
        h_layout.setSpacing(6)

        self._title_label = QLabel(t("remote.title"))
        self._title_label.setStyleSheet("font-weight: bold;")
        h_layout.addWidget(self._title_label)

        self._subtitle_label = QLabel("")
        self._subtitle_label.setStyleSheet("color: #888;")
        h_layout.addWidget(self._subtitle_label)
        h_layout.addStretch()

        self._reload_btn = QPushButton("⟳")
        self._reload_btn.setFixedSize(24, 24)
        self._reload_btn.setToolTip(t("remote.refresh_hosts"))
        self._reload_btn.clicked.connect(self._reload_hosts)
        h_layout.addWidget(self._reload_btn)

        self._add_btn = QPushButton("+")
        self._add_btn.setFixedSize(24, 24)
        self._add_btn.setToolTip(t("remote.add_host"))
        self._add_btn.clicked.connect(self._on_add_host_clicked)
        h_layout.addWidget(self._add_btn)

        self._disconnect_btn = QPushButton("×")
        self._disconnect_btn.setFixedSize(24, 24)
        self._disconnect_btn.setToolTip(t("remote.disconnect"))
        self._disconnect_btn.clicked.connect(self._disconnect)
        self._disconnect_btn.hide()
        h_layout.addWidget(self._disconnect_btn)

        root.addWidget(self._header)

        # 两个状态：主机列表 / 文件树
        self._stack = QStackedWidget()

        # --- 主机列表页 ---
        self._hosts_page = QWidget()
        hp_layout = QVBoxLayout(self._hosts_page)
        hp_layout.setContentsMargins(0, 0, 0, 0)
        hp_layout.setSpacing(0)

        self._empty_hint = QLabel(t("remote.no_hosts"))
        self._empty_hint.setWordWrap(True)
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setContentsMargins(20, 20, 20, 10)
        self._empty_hint.setStyleSheet("color: #888;")
        self._empty_hint.hide()
        hp_layout.addWidget(self._empty_hint)

        self._hosts_list = QListWidget()
        self._hosts_list.itemDoubleClicked.connect(self._on_host_activated)
        hp_layout.addWidget(self._hosts_list, 1)

        self._stack.addWidget(self._hosts_page)

        # --- 文件树页 ---
        self._tree_page = QWidget()
        tp_layout = QVBoxLayout(self._tree_page)
        tp_layout.setContentsMargins(0, 0, 0, 0)
        tp_layout.setSpacing(0)

        # 路径栏 + 操作按钮
        path_bar = QFrame()
        path_bar.setFixedHeight(30)
        pb_layout = QHBoxLayout(path_bar)
        pb_layout.setContentsMargins(6, 2, 6, 2)
        pb_layout.setSpacing(4)

        self._up_btn = QPushButton("↑")
        self._up_btn.setFixedSize(22, 22)
        self._up_btn.setToolTip(t("remote.up"))
        self._up_btn.clicked.connect(self._on_up)
        pb_layout.addWidget(self._up_btn)

        self._home_btn = QPushButton("⌂")
        self._home_btn.setFixedSize(22, 22)
        self._home_btn.setToolTip(t("remote.go_home"))
        self._home_btn.clicked.connect(self._on_home)
        pb_layout.addWidget(self._home_btn)

        self._refresh_btn = QPushButton("⟳")
        self._refresh_btn.setFixedSize(22, 22)
        self._refresh_btn.setToolTip(t("remote.refresh"))
        self._refresh_btn.clicked.connect(self._on_refresh)
        pb_layout.addWidget(self._refresh_btn)

        self._path_edit = QLineEdit()
        self._path_edit.returnPressed.connect(self._on_path_edited)
        pb_layout.addWidget(self._path_edit, 1)

        tp_layout.addWidget(path_bar)

        self._tree = _RemoteTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        tp_layout.addWidget(self._tree, 1)

        # Cmd+C / Cmd+V — 当 tree 或其子项有焦点时触发
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self._tree)
        copy_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_sc.activated.connect(lambda: self._clipboard_copy_selection(None))

        paste_sc = QShortcut(QKeySequence.StandardKey.Paste, self._tree)
        paste_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        paste_sc.activated.connect(self._paste_via_shortcut)

        self._stack.addWidget(self._tree_page)

        root.addWidget(self._stack, 1)

    def _apply_theme(self):
        bg_dark = self.theme.get('bg_dark', '#1a1a2e')
        bg_medium = self.theme.get('bg_medium', '#16213e')
        bg_hover = self.theme.get('bg_hover', '#2a2a44')
        text = self.theme.get('text', '#eaeaea')
        text_dim = self.theme.get('text_dim', '#888888')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')

        self.setStyleSheet(f"""
            QWidget {{ background-color: {bg_dark}; color: {text}; }}
        """)
        self._header.setStyleSheet(f"""
            QFrame {{ background-color: {bg_medium}; border-bottom: 1px solid {border}; }}
            QLabel {{ color: {text}; }}
            QPushButton {{
                background-color: transparent; color: {text_dim};
                border: 1px solid transparent; border-radius: 3px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {bg_hover}; color: {text}; }}
        """)
        list_tree_css = f"""
            QListWidget, QTreeWidget {{
                background-color: {bg_dark}; color: {text};
                border: none; outline: none;
            }}
            QListWidget::item, QTreeWidget::item {{
                padding: 4px 6px;
            }}
            QListWidget::item:selected, QTreeWidget::item:selected {{
                background-color: {accent}; color: white;
            }}
            QListWidget::item:hover, QTreeWidget::item:hover {{
                background-color: {bg_hover};
            }}
        """
        self._hosts_list.setStyleSheet(list_tree_css)
        self._tree.setStyleSheet(list_tree_css)
        self._path_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {bg_medium}; color: {text};
                border: 1px solid {border}; border-radius: 3px;
                padding: 2px 6px;
            }}
            QLineEdit:focus {{ border: 1px solid {accent}; }}
        """)

    def apply_theme(self, theme: dict):
        self.theme = theme or {}
        self._apply_theme()

    def apply_language(self):
        self._title_label.setText(t("remote.title"))
        self._reload_btn.setToolTip(t("remote.refresh_hosts"))
        self._add_btn.setToolTip(t("remote.add_host"))
        self._disconnect_btn.setToolTip(t("remote.disconnect"))
        self._up_btn.setToolTip(t("remote.up"))
        self._home_btn.setToolTip(t("remote.go_home"))
        self._refresh_btn.setToolTip(t("remote.refresh"))
        self._empty_hint.setText(t("remote.no_hosts"))
        # 重建主机列表（条目文本本身是别名+真实地址，不用国际化）
        if self._session is None:
            self._populate_hosts_list()

    # ---------- 主机列表逻辑 ----------

    def _reload_hosts(self):
        self._hosts = parse_ssh_config()
        self._populate_hosts_list()

    def _populate_hosts_list(self):
        self._hosts_list.clear()
        combined = list(self._hosts) + list(self._extra_hosts)
        if not combined:
            self._empty_hint.show()
        else:
            self._empty_hint.hide()
            for h in combined:
                target = f"{h.user + '@' if h.user else ''}{h.hostname}:{h.port}"
                item = QListWidgetItem(f"🖥  {h.alias}    {target}")
                item.setData(_ROLE_ENTRY, h)
                self._hosts_list.addItem(item)

    def _on_add_host_clicked(self):
        text, ok = QInputDialog.getText(
            self, t("remote.add_host_title"),
            t("remote.add_host_hint"),
            QLineEdit.EchoMode.Normal,
            ""
        )
        if not ok:
            return
        text = text.strip()
        if not text:
            return
        # 解析 [user@]host[:port]
        user = ""
        port = 22
        rest = text
        if "@" in rest:
            user, rest = rest.split("@", 1)
        if ":" in rest:
            host, port_s = rest.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                QMessageBox.warning(self, t("remote.add_host_title"), "Invalid port")
                return
        else:
            host = rest
        host = host.strip()
        if not host:
            return
        cfg = HostConfig(alias=text, hostname=host, user=user.strip(), port=port)
        self._extra_hosts.append(cfg)
        self._populate_hosts_list()

    def _on_host_activated(self, item: QListWidgetItem):
        host: HostConfig = item.data(_ROLE_ENTRY)
        if not host:
            return
        self._connect_to(host)

    # ---------- 连接 ----------

    def _connect_to(self, host: HostConfig):
        if self._session is not None:
            self._disconnect()

        self._subtitle_label.setText(t("remote.connecting", host=host.alias))
        self._add_btn.hide()
        self._reload_btn.hide()

        sess = SSHSession(host, parent=self)
        sess.connected.connect(lambda: self._on_session_connected(sess))
        sess.connect_failed.connect(lambda msg: self._on_session_connect_failed(sess, msg))
        sess.connect_async(
            password_provider=self._prompt_password,
            passphrase_provider=self._prompt_password,
        )
        self._session = sess

    _password_prompt_signal = pyqtSignal(str)  # 在 UI 线程触发输入框

    def _prompt_password(self, label: str) -> Optional[str]:
        # paramiko 回调在后台线程；用 QInputDialog 必须切回 UI 线程。
        # 这里通过自定义事件 + 信号触发，主线程显示对话框并把结果放回 holder。
        result_holder: dict = {'done': False, 'value': None, 'label': label}
        self._pending_password_request = result_holder
        try:
            self._password_prompt_signal.emit(label)
        except Exception:
            return None
        # 不能在工作线程里 processEvents（会和主线程冲突），单纯轮询就行
        while not result_holder['done']:
            time.sleep(0.05)
        return result_holder['value']

    def _on_password_prompt(self, label: str):
        # 真正在 UI 线程里跑的输入框
        holder = getattr(self, '_pending_password_request', None)
        if holder is None:
            return
        text, ok = QInputDialog.getText(
            self, t("remote.password_title"),
            t("remote.password_prompt", host=label),
            QLineEdit.EchoMode.Password,
        )
        holder['value'] = text if ok else None
        holder['done'] = True

    def _on_session_connected(self, sess: SSHSession):
        if sess is not self._session:
            return
        self._subtitle_label.setText(sess.host_config.alias)
        self._disconnect_btn.show()
        self._stack.setCurrentWidget(self._tree_page)
        self._current_path = sess.home()
        self._path_edit.setText(self._current_path)
        self._populate_tree_root()
        # 通知主窗口：可以开一个 SSH 终端 tab 进去
        self.host_connected.emit(sess.host_config)

    def _on_session_connect_failed(self, sess: SSHSession, msg: str):
        if sess is not self._session:
            return
        QMessageBox.warning(
            self, t("remote.connect_failed_title"),
            t("remote.connect_failed_msg", host=sess.host_config.alias, error=msg),
        )
        self._subtitle_label.setText("")
        self._add_btn.show()
        self._reload_btn.show()
        self._session = None

    def _disconnect(self):
        if self._session is not None:
            try:
                self._session.disconnect()
            except Exception:
                pass
            self._session = None
        self._stack.setCurrentWidget(self._hosts_page)
        self._tree.clear()
        self._subtitle_label.setText("")
        self._add_btn.show()
        self._reload_btn.show()
        self._disconnect_btn.hide()

    # ---------- 文件树 ----------

    def _populate_tree_root(self):
        """加载当前路径的内容到顶层（不再包一层 path 节点）

        和本地 Explorer 行为一致：path 在路径栏里显示，文件/目录直接平铺在树根。
        """
        self._tree.clear()
        if self._session is None:
            return
        sess = self._session
        path = self._current_path or "/"
        fut = sess.submit(sess.listdir, path)

        def on_done(f):
            try:
                entries: list[RemoteEntry] = f.result()
            except Exception as e:
                # 在 SSH 工作线程里只能通过信号回 UI 线程，QTimer 在这里不灵
                self._error_signal.emit(str(e))
                return
            self._top_level_ready.emit(entries)
        fut.add_done_callback(on_done)

    def _apply_top_level(self, entries: list[RemoteEntry]):
        """把目录内容直接放到树根"""
        try:
            self._tree.clear()
        except RuntimeError:
            return
        for e in entries:
            icon = "📁 " if e.is_dir else "📄 "
            item = QTreeWidgetItem([icon + e.name])
            item.setData(0, _ROLE_ENTRY, e)
            item.setData(0, _ROLE_LOADED, False)
            if e.is_dir:
                # 占位让箭头出现，展开时才真正去 listdir
                placeholder = QTreeWidgetItem(["…"])
                item.addChild(placeholder)
            self._tree.addTopLevelItem(item)

    def _on_item_expanded(self, item: QTreeWidgetItem):
        if item.data(0, _ROLE_LOADED):
            return
        entry: RemoteEntry = item.data(0, _ROLE_ENTRY)
        if not entry or not entry.is_dir:
            return
        # 加一个占位避免展开时空闪
        item.setData(0, _ROLE_LOADED, True)
        self._fill_children(item, entry.path)

    def _on_refresh(self):
        # 重新加载当前 path
        self._populate_tree_root()

    def _fill_children(self, parent_item: QTreeWidgetItem, path: str):
        if self._session is None:
            return
        sess = self._session
        fut = sess.submit(sess.listdir, path)

        def on_done(f):
            try:
                entries: list[RemoteEntry] = f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
                return
            self._subtree_ready.emit(parent_item, entries)
        fut.add_done_callback(on_done)

    def _apply_children(self, parent_item: QTreeWidgetItem, entries: list[RemoteEntry]):
        # 父项可能已被释放（用户断开了），守一下
        try:
            parent_item.takeChildren()
        except RuntimeError:
            return
        for e in entries:
            icon = "📁 " if e.is_dir else "📄 "
            child = QTreeWidgetItem([icon + e.name])
            child.setData(0, _ROLE_ENTRY, e)
            child.setData(0, _ROLE_LOADED, False)
            if e.is_dir:
                # 加一个占位让箭头出现
                placeholder = QTreeWidgetItem(["…"])
                child.addChild(placeholder)
            parent_item.addChild(child)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _col: int):
        entry: RemoteEntry = item.data(0, _ROLE_ENTRY)
        if not entry:
            return
        if entry.is_dir:
            # 双击目录 = 进入它，重置树根
            self._current_path = entry.path
            self._path_edit.setText(self._current_path)
            self._populate_tree_root()
        else:
            self._open_remote_file(entry)

    def _on_up(self):
        if not self._current_path or self._current_path == "/":
            return
        parent = posixpath.dirname(self._current_path.rstrip("/")) or "/"
        self._current_path = parent
        self._path_edit.setText(self._current_path)
        self._populate_tree_root()

    def _on_home(self):
        if self._session is None:
            return
        self._current_path = self._session.home()
        self._path_edit.setText(self._current_path)
        self._populate_tree_root()

    def _on_path_edited(self):
        if self._session is None:
            return
        new_path = self._path_edit.text().strip() or "/"
        sess = self._session
        # 先 stat 一下确认存在且是目录
        fut = sess.submit(sess.stat, new_path)

        def on_done(f):
            try:
                entry: RemoteEntry = f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
                return
            self._stat_resolved.emit(entry, new_path)
        fut.add_done_callback(on_done)

    def _on_stat_resolved(self, entry: RemoteEntry, requested_path: str):
        if entry.is_dir:
            self._current_path = requested_path
        else:
            self._open_remote_file(entry)
        self._path_edit.setText(self._current_path)
        self._populate_tree_root()

    # ---------- 文件操作 ----------

    def _make_menu(self) -> QMenu:
        """与本地 Explorer 一致：高亮当前 hover 的菜单项"""
        bg_medium = self.theme.get('bg_medium', '#16213e')
        text = self.theme.get('text', '#eaeaea')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {bg_medium};
                color: {text};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: {accent};
                color: white;
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {border};
                margin: 4px 10px;
            }}
        """)
        return menu

    def _on_context_menu(self, pos):
        if self._session is None:
            return
        item = self._tree.itemAt(pos)
        entry: Optional[RemoteEntry] = item.data(0, _ROLE_ENTRY) if item is not None else None

        menu = self._make_menu()

        if entry is None:
            # 空白区域：新建文件 / 新建文件夹 / 上传 / 粘贴 / 刷新（全都作用在当前 path）
            cwd = self._current_path
            act_new_file = QAction(t("remote.new_file"), self)
            act_new_file.triggered.connect(lambda: self._new_file_at(cwd, None))
            menu.addAction(act_new_file)

            act_new_dir = QAction(t("remote.new_folder"), self)
            act_new_dir.triggered.connect(lambda: self._new_folder_at(cwd, None))
            menu.addAction(act_new_dir)

            act_upload = QAction(t("remote.upload"), self)
            act_upload.triggered.connect(lambda: self._upload_at(cwd, None))
            menu.addAction(act_upload)

            menu.addSeparator()

            if explorer_clipboard.has_pastable():
                act_paste = QAction(
                    t("remote.paste_with_label", label=explorer_clipboard.describe()), self,
                )
                act_paste.triggered.connect(lambda: self._clipboard_paste_into(cwd))
                menu.addAction(act_paste)
                menu.addSeparator()

            act_refresh = QAction(t("remote.refresh"), self)
            act_refresh.triggered.connect(self._on_refresh)
            menu.addAction(act_refresh)
            menu.exec(QCursor.pos())
            return

        if not entry.is_dir:
            act_open = QAction(t("remote.open_in_editor"), self)
            act_open.triggered.connect(lambda: self._open_remote_file(entry))
            menu.addAction(act_open)
            menu.addSeparator()

        if entry.is_dir:
            act_term = QAction(t("remote.open_terminal_here"), self)
            act_term.triggered.connect(lambda: self._open_terminal_here(entry))
            menu.addAction(act_term)
            act_new_file = QAction(t("remote.new_file"), self)
            act_new_file.triggered.connect(lambda: self._new_file_under(entry, item))
            menu.addAction(act_new_file)
            act_new_dir = QAction(t("remote.new_folder"), self)
            act_new_dir.triggered.connect(lambda: self._new_folder_under(entry, item))
            menu.addAction(act_new_dir)
            act_upload = QAction(t("remote.upload"), self)
            act_upload.triggered.connect(lambda: self._upload_into(entry, item))
            menu.addAction(act_upload)
            menu.addSeparator()

        # 跨面板复制 / 粘贴
        act_copy = QAction(t("remote.copy"), self)
        # 选中里若包含右键项则按选中复制；否则只复制右键项
        act_copy.triggered.connect(lambda: self._clipboard_copy_selection(entry))
        menu.addAction(act_copy)

        if explorer_clipboard.has_pastable():
            # 文件项目 → 粘贴到其父目录；目录项目 → 粘贴到它里面
            paste_dir = entry.path if entry.is_dir else (
                posixpath.dirname(entry.path.rstrip("/")) or "/"
            )
            act_paste = QAction(
                t("remote.paste_with_label", label=explorer_clipboard.describe()), self,
            )
            act_paste.triggered.connect(lambda: self._clipboard_paste_into(paste_dir))
            menu.addAction(act_paste)
        menu.addSeparator()

        act_rename = QAction(t("remote.rename"), self)
        act_rename.triggered.connect(lambda: self._rename_entry(entry, item))
        menu.addAction(act_rename)

        act_delete = QAction(t("remote.delete"), self)
        act_delete.triggered.connect(lambda: self._delete_entry(entry, item))
        menu.addAction(act_delete)

        if not entry.is_dir:
            act_download = QAction(t("remote.download"), self)
            act_download.triggered.connect(lambda: self._download_to_local(entry))
            menu.addAction(act_download)

        menu.addSeparator()
        act_copy_path = QAction(t("remote.copy_path"), self)
        act_copy_path.triggered.connect(lambda: QApplication.clipboard().setText(entry.path))
        menu.addAction(act_copy_path)

        menu.exec(QCursor.pos())

    # ---------- 跨面板复制 / 粘贴 ----------

    def _paste_via_shortcut(self):
        """Cmd+V：粘贴到当前选中目录（或当前路径）"""
        if self._session is None:
            return
        target = None
        for it in self._tree.selectedItems():
            e: RemoteEntry = it.data(0, _ROLE_ENTRY)
            if e is None:
                continue
            target = e.path if e.is_dir else (
                posixpath.dirname(e.path.rstrip("/")) or "/"
            )
            break
        if not target:
            target = self._current_path or "/"
        self._clipboard_paste_into(target)

    def _clipboard_copy_selection(self, fallback_entry: Optional[RemoteEntry] = None):
        """把当前选中的远程文件 / 目录放入跨面板剪贴板。

        - 选中里包含 fallback_entry 时按整体选中复制；否则只复制 fallback_entry
        - 同时把可下载的文件下载到本地 temp，写到系统剪贴板，
          这样 Finder 等外部程序可以直接粘贴
        """
        if self._session is None:
            return
        host_alias = self._session.host_config.alias
        selected_entries: list[RemoteEntry] = []
        for it in self._tree.selectedItems():
            e: RemoteEntry = it.data(0, _ROLE_ENTRY)
            if e is not None:
                selected_entries.append(e)

        if fallback_entry is not None and fallback_entry not in selected_entries:
            entries = [fallback_entry]
        else:
            entries = selected_entries

        payload: list[tuple] = []
        seen = set()
        for e in entries:
            if e.path in seen:
                continue
            seen.add(e.path)
            payload.append(("remote", host_alias, e.path, self._session))
        if not payload:
            return

        # 同步下载文件项到本地 temp 供系统剪贴板使用（目录跳过避免大下载）
        file_entries = [e for e in entries if not e.is_dir]
        local_paths_for_system: list[str] = []
        if file_entries:
            try:
                local_paths_for_system = self._sync_download_for_drag(file_entries)
            except Exception:
                local_paths_for_system = []

        explorer_clipboard.set_items(
            payload,
            push_local_paths=local_paths_for_system or None,
        )

    def _clipboard_paste_into(self, target_dir: str):
        """把跨面板剪贴板里的项目粘贴到当前远端 target_dir"""
        if self._session is None:
            return
        items = explorer_clipboard.effective_items()
        if not items or not target_dir:
            return
        sess = self._session

        errors: list[str] = []
        skip_all = False
        overwrite_all = False

        for it in items:
            kind = it[0]
            try:
                if kind == "local":
                    src = it[1]
                    name = os.path.basename(src.rstrip("/"))
                    dst = posixpath.join(target_dir, name)
                    if self._remote_exists(sess, dst):
                        decision = self._ask_overwrite(name, skip_all, overwrite_all)
                        if decision == "skip":
                            continue
                        if decision == "skip_all":
                            skip_all = True
                            continue
                        if decision == "overwrite_all":
                            overwrite_all = True
                        self._remote_remove(sess, dst)
                    if os.path.isdir(src) and not os.path.islink(src):
                        self._upload_local_dir(sess, src, dst)
                    else:
                        fut = sess.submit(sess.upload, src, dst)
                        self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                                 dst=target_dir))

                elif kind == "remote":
                    _, host_alias, remote_src, src_sess = it
                    if src_sess is None or not src_sess.is_connected():
                        errors.append(f"{remote_src}: {t('remote.session_lost')}")
                        continue
                    name = posixpath.basename(remote_src.rstrip("/")) or host_alias
                    dst = posixpath.join(target_dir, name)
                    # 同源同路径粘到同一目录 → 跳过避免覆盖自己
                    if src_sess is sess and remote_src == dst:
                        continue
                    if self._remote_exists(sess, dst):
                        decision = self._ask_overwrite(name, skip_all, overwrite_all)
                        if decision == "skip":
                            continue
                        if decision == "skip_all":
                            skip_all = True
                            continue
                        if decision == "overwrite_all":
                            overwrite_all = True
                        self._remote_remove(sess, dst)
                    # 走临时文件中转：远程源 → 本地 temp → 远程目标
                    self._remote_to_remote(src_sess, sess, remote_src, dst)
                else:
                    errors.append(f"Unknown clipboard item: {it!r}")
            except Exception as e:
                errors.append(f"{it}: {e}")

        if errors:
            QMessageBox.warning(self, t("remote.op_failed_title"), "\n".join(errors))
        # 刷新当前树根（target_dir 通常就是 _current_path 或它的子目录）
        if target_dir == self._current_path:
            self._populate_tree_root()
        else:
            # 找到对应的树节点刷新；找不到就重刷整棵树
            self._refresh_subtree_by_path(target_dir)

    def _refresh_subtree_by_path(self, path: str):
        """如果 tree 顶层有 path 对应的目录项，把它重刷一下；否则刷整棵树"""
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            entry: RemoteEntry = it.data(0, _ROLE_ENTRY)
            if entry and entry.is_dir and entry.path == path:
                self._reload_subtree(it, path)
                return
        self._populate_tree_root()

    def _ask_overwrite(self, name: str, skip_all: bool, overwrite_all: bool) -> str:
        if overwrite_all:
            return "overwrite"
        if skip_all:
            return "skip"
        reply = QMessageBox.question(
            self, t("remote.overwrite_title"),
            t("remote.overwrite_msg", name=name),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.YesToAll
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.NoToAll,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            return "overwrite"
        if reply == QMessageBox.StandardButton.YesToAll:
            return "overwrite_all"
        if reply == QMessageBox.StandardButton.NoToAll:
            return "skip_all"
        return "skip"

    def _remote_exists(self, sess: SSHSession, path: str) -> bool:
        fut = sess.submit(sess.stat, path)
        try:
            fut.result()
            return True
        except Exception:
            return False

    def _remote_remove(self, sess: SSHSession, path: str):
        """删除远端文件或目录（递归）"""
        fut_stat = sess.submit(sess.stat, path)
        try:
            entry: RemoteEntry = fut_stat.result()
        except Exception:
            return
        if entry.is_dir and not entry.is_link:
            fut = sess.submit(sess.remove_tree, path)
        else:
            fut = sess.submit(sess.remove, path)
        fut.result()

    def _upload_local_dir(self, sess: SSHSession, local_dir: str, remote_dir: str):
        """把本地目录递归上传到 remote_dir"""
        fut = sess.submit(sess.mkdir, remote_dir)
        try:
            fut.result()
        except Exception:
            pass  # 可能已存在，忽略
        futures = []
        for root, dirs, files in os.walk(local_dir):
            rel = os.path.relpath(root, local_dir)
            remote_root = remote_dir if rel == "." else posixpath.join(
                remote_dir, *rel.split(os.sep)
            )
            for d in dirs:
                r_path = posixpath.join(remote_root, d)
                f = sess.submit(sess.mkdir, r_path)
                # mkdir 失败（已存在）不致命，等会回收
                futures.append(f)
            for fname in files:
                local_path = os.path.join(root, fname)
                r_path = posixpath.join(remote_root, fname)
                futures.append(sess.submit(sess.upload, local_path, r_path))
        # 等所有 future 完成；mkdir 的失败吞掉
        self._wait_future_with_progress(futures, t("remote.pasting_progress",
                                                   dst=remote_dir),
                                        tolerate_errors=True)

    def _remote_to_remote(self, src_sess: SSHSession, dst_sess: SSHSession,
                          src_path: str, dst_path: str):
        """跨/同 session 远程 → 远程：经本地 temp 中转"""
        # 先 stat 源判断是不是目录
        entry: RemoteEntry = src_sess.submit(src_sess.stat, src_path).result()
        tmp_root = tempfile.mkdtemp(prefix="smart_terminal_paste_")
        try:
            tmp_local = os.path.join(tmp_root, os.path.basename(src_path.rstrip("/")) or "item")
            if entry.is_dir:
                self._download_remote_recursive(src_sess, src_path, tmp_local)
                self._upload_local_dir(dst_sess, tmp_local, dst_path)
            else:
                fut = src_sess.submit(src_sess.download, src_path, tmp_local)
                self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                         dst=dst_path))
                fut2 = dst_sess.submit(dst_sess.upload, tmp_local, dst_path)
                self._wait_future_with_progress([fut2], t("remote.pasting_progress",
                                                          dst=dst_path))
        finally:
            try:
                import shutil as _shutil
                _shutil.rmtree(tmp_root, ignore_errors=True)
            except Exception:
                pass

    def _download_remote_recursive(self, sess: SSHSession, remote_path: str, local_path: str):
        """通过 src session 把远程文件 / 目录递归下载到 local_path（阻塞）"""
        entry: RemoteEntry = sess.submit(sess.stat, remote_path).result()
        if not entry.is_dir:
            fut = sess.submit(sess.download, remote_path, local_path)
            self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                     dst=local_path))
            return
        os.makedirs(local_path, exist_ok=True)
        children = sess.submit(sess.listdir, remote_path).result()
        for child in children:
            child_local = os.path.join(local_path, child.name)
            if child.is_dir and not child.is_link:
                self._download_remote_recursive(sess, child.path, child_local)
            else:
                fut = sess.submit(sess.download, child.path, child_local)
                self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                         dst=local_path))

    def _wait_future_with_progress(self, futures: list, label: str,
                                    tolerate_errors: bool = False):
        """阻塞等待 futures 完成，跑事件循环避免 UI 卡死"""
        if not futures:
            return
        progress = QProgressDialog(label, None, 0, len(futures), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        progress.setValue(0)
        done = {"n": 0, "errors": []}

        def make_cb():
            def cb(f):
                try:
                    f.result()
                except Exception as e:
                    done["errors"].append(str(e))
                done["n"] += 1
            return cb

        for fut in futures:
            fut.add_done_callback(make_cb())

        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(80)
        def tick():
            progress.setValue(done["n"])
            if done["n"] >= len(futures):
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        progress.setValue(len(futures))
        if done["errors"] and not tolerate_errors:
            raise RuntimeError("; ".join(done["errors"]))

    def _open_remote_file(self, entry: RemoteEntry):
        if self._session is None:
            return
        sess = self._session
        host_alias = sess.host_config.alias

        # 下载到 临时目录 / host_alias / ...remote_path
        safe_alias = "".join(c if c.isalnum() or c in '-._' else '_' for c in host_alias)
        local_dir = os.path.join(
            tempfile.gettempdir(),
            f"smart_terminal_remote_{safe_alias}",
            *entry.path.strip("/").split("/")[:-1],
        )
        os.makedirs(local_dir, exist_ok=True)
        local_path = os.path.join(local_dir, entry.name)

        fut = sess.submit(sess.read_file, entry.path)

        def on_done(f):
            try:
                data = f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
                return
            self._file_downloaded.emit(host_alias, entry.path, local_path, data)
        fut.add_done_callback(on_done)

    def _on_file_downloaded(self, host_alias: str, remote_path: str,
                              local_path: str, data: bytes):
        try:
            with open(local_path, "wb") as fh:
                fh.write(data)
        except Exception as e:
            self._toast_error(str(e))
            return
        self._open_temp_map[local_path] = (host_alias, remote_path)
        # session 总是当前活动的 session（下载就是用它发起的）
        self.file_open_requested.emit(host_alias, remote_path, local_path, self._session)

    def remote_mapping_for(self, local_path: str) -> Optional[tuple[str, str]]:
        """主窗口的编辑器保存时调，查 local_path 对应哪个 (host_alias, remote_path)"""
        return self._open_temp_map.get(local_path)

    def upload_after_save(self, local_path: str):
        """编辑器保存完本地临时文件后，把它推回远端"""
        mapping = self._open_temp_map.get(local_path)
        if not mapping:
            return
        host_alias, remote_path = mapping
        if self._session is None or self._session.host_config.alias != host_alias:
            self._toast_error(f"Session for {host_alias} not active; remote save skipped.")
            return
        sess = self._session
        fut = sess.submit(sess.upload, local_path, remote_path)

        def on_done(f):
            try:
                f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
        fut.add_done_callback(on_done)

    def _new_file_at(self, parent_path: str, parent_item: Optional[QTreeWidgetItem]):
        name, ok = QInputDialog.getText(self, t("remote.new_file"), t("remote.prompt_new_name"))
        if not ok or not name.strip():
            return
        path = posixpath.join(parent_path, name.strip())
        sess = self._session
        if sess is None:
            return
        fut = sess.submit(sess.write_file, path, b"")
        self._refresh_after(fut, parent_item, parent_path)

    def _new_folder_at(self, parent_path: str, parent_item: Optional[QTreeWidgetItem]):
        name, ok = QInputDialog.getText(self, t("remote.new_folder"), t("remote.prompt_new_name"))
        if not ok or not name.strip():
            return
        path = posixpath.join(parent_path, name.strip())
        sess = self._session
        if sess is None:
            return
        fut = sess.submit(sess.mkdir, path)
        self._refresh_after(fut, parent_item, parent_path)

    def _new_file_under(self, parent_entry: RemoteEntry, parent_item: QTreeWidgetItem):
        self._new_file_at(parent_entry.path, parent_item)

    def _new_folder_under(self, parent_entry: RemoteEntry, parent_item: QTreeWidgetItem):
        self._new_folder_at(parent_entry.path, parent_item)

    def _rename_entry(self, entry: RemoteEntry, item: QTreeWidgetItem):
        new_name, ok = QInputDialog.getText(
            self, t("remote.rename"), t("remote.prompt_rename_to"),
            QLineEdit.EchoMode.Normal, entry.name,
        )
        if not ok or not new_name.strip() or new_name.strip() == entry.name:
            return
        parent_path = posixpath.dirname(entry.path.rstrip("/")) or "/"
        new_path = posixpath.join(parent_path, new_name.strip())
        sess = self._session
        if sess is None:
            return
        fut = sess.submit(sess.rename, entry.path, new_path)
        self._refresh_after(fut, item.parent(), parent_path)

    def _delete_entry(self, entry: RemoteEntry, item: QTreeWidgetItem):
        confirm = QMessageBox.question(
            self, t("remote.confirm_delete_title"),
            t("remote.confirm_delete_msg", name=entry.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        sess = self._session
        if sess is None:
            return
        if entry.is_dir:
            fut = sess.submit(sess.remove_tree, entry.path)
        else:
            fut = sess.submit(sess.remove, entry.path)
        parent_path = posixpath.dirname(entry.path.rstrip("/")) or "/"
        self._refresh_after(fut, item.parent(), parent_path)

    def _download_to_local(self, entry: RemoteEntry):
        save_path, _ = QFileDialog.getSaveFileName(self, t("remote.download"), entry.name)
        if not save_path:
            return
        sess = self._session
        if sess is None:
            return
        fut = sess.submit(sess.download, entry.path, save_path)
        def on_done(f):
            try:
                f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
        fut.add_done_callback(on_done)

    def _open_terminal_here(self, entry: RemoteEntry):
        if self._session is None:
            return
        self.open_terminal_at.emit(self._session.host_config, entry.path)

    def _upload_at(self, parent_path: str, parent_item: Optional[QTreeWidgetItem]):
        local_path, _ = QFileDialog.getOpenFileName(self, t("remote.upload"))
        if not local_path:
            return
        sess = self._session
        if sess is None:
            return
        remote_path = posixpath.join(parent_path, os.path.basename(local_path))
        fut = sess.submit(sess.upload, local_path, remote_path)
        self._refresh_after(fut, parent_item, parent_path)

    def _upload_into(self, dir_entry: RemoteEntry, dir_item: QTreeWidgetItem):
        self._upload_at(dir_entry.path, dir_item)

    def _handle_drop_upload(self, local_paths: list[str], target_dir: str,
                              target_item: Optional[QTreeWidgetItem]):
        """处理拖入：把每个本地文件上传到 target_dir，完成后刷新对应子树"""
        if self._session is None:
            return
        sess = self._session
        total = len(local_paths)
        progress = QProgressDialog(
            f"Uploading to {target_dir}...", "Cancel", 0, total, self
        )
        progress.setWindowTitle(t("remote.title"))
        progress.setMinimumDuration(300)
        progress.setValue(0)

        # 让 progress 通过信号在主线程里更新
        done_counter = {"n": 0, "errors": []}

        def make_upload_done(local_path):
            def cb(f):
                try:
                    f.result()
                except Exception as e:
                    done_counter["errors"].append(f"{os.path.basename(local_path)}: {e}")
                done_counter["n"] += 1
                # 不能在工作线程更新 UI，靠 progress.setValue 在主循环里查询
            return cb

        for lp in local_paths:
            remote_name = os.path.basename(lp)
            remote_path = posixpath.join(target_dir, remote_name)
            fut = sess.submit(sess.upload, lp, remote_path)
            fut.add_done_callback(make_upload_done(lp))

        # 主线程轮询进度（避免 progress dialog 卡死）
        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(80)
        def tick():
            progress.setValue(done_counter["n"])
            if done_counter["n"] >= total or progress.wasCanceled():
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        progress.setValue(total)

        if done_counter["errors"]:
            QMessageBox.warning(
                self, t("remote.op_failed_title"),
                "\n".join(done_counter["errors"]),
            )
        # 刷新目标目录视图
        if target_item is not None:
            entry: RemoteEntry = target_item.data(0, _ROLE_ENTRY)
            if entry and entry.is_dir:
                self._reload_subtree(target_item, target_dir)
            else:
                # 落在文件上 → 父其实是 target_dir 所在容器（顶层或某个目录项）
                self._populate_tree_root()
        else:
            # 落在空白 → 当前路径
            self._populate_tree_root()

    def _sync_download_for_drag(self, entries: list[RemoteEntry]) -> list[str]:
        """同步下载选中文件用于拖出。返回本地临时路径列表。

        拖出操作必须在 startDrag 内完成（QDrag 需要 mime 数据立即就绪），
        所以这里阻塞主线程并跑进度条。文件大小 > 5MB 会提示确认。
        """
        if self._session is None:
            return []
        sess = self._session

        # 体积合理性检查
        big = [e for e in entries if e.size and e.size > 10 * 1024 * 1024]
        if big:
            names = ", ".join(e.name for e in big[:3])
            confirm = QMessageBox.question(
                self, t("remote.title"),
                f"Drag will download large files first ({names}...). Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return []

        safe_alias = "".join(c if c.isalnum() or c in '-._' else '_'
                             for c in sess.host_config.alias)
        local_dir = os.path.join(
            tempfile.gettempdir(),
            f"smart_terminal_remote_drag_{safe_alias}",
        )
        os.makedirs(local_dir, exist_ok=True)

        progress = QProgressDialog(
            "Downloading for drag...", "Cancel", 0, len(entries), self
        )
        progress.setMinimumDuration(300)
        progress.setValue(0)

        done_counter = {"n": 0, "paths": [], "errors": []}

        def make_cb(entry, local_path):
            def cb(f):
                try:
                    f.result()
                    done_counter["paths"].append(local_path)
                except Exception as e:
                    done_counter["errors"].append(f"{entry.name}: {e}")
                done_counter["n"] += 1
            return cb

        for e in entries:
            local_path = os.path.join(local_dir, e.name)
            fut = sess.submit(sess.download, e.path, local_path)
            fut.add_done_callback(make_cb(e, local_path))

        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(80)
        def tick():
            progress.setValue(done_counter["n"])
            if done_counter["n"] >= len(entries) or progress.wasCanceled():
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        progress.setValue(len(entries))

        if done_counter["errors"]:
            QMessageBox.warning(
                self, t("remote.op_failed_title"),
                "\n".join(done_counter["errors"]),
            )
        return done_counter["paths"]

    def _refresh_after(self, fut, item: Optional[QTreeWidgetItem], path: str):
        """操作成功后刷新对应子树。item 为 None 时（顶层操作）重刷整棵树。"""
        def on_done(f):
            try:
                f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
                return
            if item is None:
                self._refresh_root_signal.emit()
            else:
                self._refresh_subtree_signal.emit(item, path)
        fut.add_done_callback(on_done)

    def _reload_subtree(self, item: QTreeWidgetItem, path: str):
        try:
            item.setData(0, _ROLE_LOADED, False)
            item.takeChildren()
        except RuntimeError:
            return
        self._on_item_expanded(item)
        # 强制展开
        try:
            self._tree.expandItem(item)
        except RuntimeError:
            pass

    # ---------- utils ----------

    def _toast_error(self, msg: str):
        self.error_occurred.emit(msg)
        QMessageBox.warning(self, t("remote.op_failed_title"), msg)
