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
    QApplication, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QAction, QCursor

from i18n import t
from ssh_session import HostConfig, RemoteEntry, SSHSession, parse_ssh_config


# 子项的 UserRole 数据键
_ROLE_ENTRY = Qt.ItemDataRole.UserRole
_ROLE_LOADED = Qt.ItemDataRole.UserRole + 1


class RemoteExplorerPanel(QWidget):
    """远程文件浏览面板"""

    # 信号：请求在编辑器中打开一个已下载到本地临时位置的远程文件
    # 参数: (host_alias, remote_path, local_temp_path, session)
    file_open_requested = pyqtSignal(str, str, str, object)
    # 错误展示（让主窗口统一弹消息或刷新状态栏）
    error_occurred = pyqtSignal(str)

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

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        tp_layout.addWidget(self._tree, 1)

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

    def _prompt_password(self, label: str) -> Optional[str]:
        # paramiko 回调在后台线程；用 QInputDialog 必须切回 UI 线程
        result_holder: dict = {}
        done = [False]

        def show():
            text, ok = QInputDialog.getText(
                self, t("remote.password_title"),
                t("remote.password_prompt", host=label),
                QLineEdit.EchoMode.Password,
            )
            result_holder['value'] = text if ok else None
            done[0] = True

        QTimer.singleShot(0, show)
        # 等 UI 完成（粗糙但有效）
        while not done[0]:
            time.sleep(0.05)
            QApplication.processEvents()
        return result_holder.get('value')

    def _on_session_connected(self, sess: SSHSession):
        if sess is not self._session:
            return
        self._subtitle_label.setText(sess.host_config.alias)
        self._disconnect_btn.show()
        self._stack.setCurrentWidget(self._tree_page)
        self._current_path = sess.home()
        self._path_edit.setText(self._current_path)
        self._populate_tree_root()

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
        self._tree.clear()
        root = QTreeWidgetItem([self._current_path or "/"])
        root.setData(0, _ROLE_ENTRY, RemoteEntry(
            name=self._current_path, path=self._current_path, is_dir=True,
        ))
        root.setData(0, _ROLE_LOADED, False)
        self._tree.addTopLevelItem(root)
        self._tree.expandItem(root)

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
                self._toast_error(str(e))
                return
            QTimer.singleShot(0, lambda: self._apply_children(parent_item, entries))
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
                QTimer.singleShot(0, lambda: self._toast_error(str(e)))
                return
            def apply():
                if entry.is_dir:
                    self._current_path = new_path
                else:
                    # 是文件 → 用编辑器打开
                    self._open_remote_file(entry)
                self._path_edit.setText(self._current_path)
                self._populate_tree_root()
            QTimer.singleShot(0, apply)
        fut.add_done_callback(on_done)

    # ---------- 文件操作 ----------

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return
        entry: RemoteEntry = item.data(0, _ROLE_ENTRY)
        if entry is None:
            return

        menu = QMenu(self)
        if not entry.is_dir:
            act_open = QAction(t("remote.open_in_editor"), self)
            act_open.triggered.connect(lambda: self._open_remote_file(entry))
            menu.addAction(act_open)
            menu.addSeparator()

        if entry.is_dir:
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
        act_copy = QAction(t("remote.copy_path"), self)
        act_copy.triggered.connect(lambda: QApplication.clipboard().setText(entry.path))
        menu.addAction(act_copy)

        menu.exec(QCursor.pos())

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
                QTimer.singleShot(0, lambda: self._toast_error(str(e)))
                return
            def apply():
                try:
                    with open(local_path, "wb") as fh:
                        fh.write(data)
                except Exception as e:
                    self._toast_error(str(e))
                    return
                self._open_temp_map[local_path] = (host_alias, entry.path)
                self.file_open_requested.emit(host_alias, entry.path, local_path, sess)
            QTimer.singleShot(0, apply)
        fut.add_done_callback(on_done)

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
                QTimer.singleShot(0, lambda: self._toast_error(str(e)))
        fut.add_done_callback(on_done)

    def _new_file_under(self, parent_entry: RemoteEntry, parent_item: QTreeWidgetItem):
        name, ok = QInputDialog.getText(self, t("remote.new_file"), t("remote.prompt_new_name"))
        if not ok or not name.strip():
            return
        path = posixpath.join(parent_entry.path, name.strip())
        sess = self._session
        if sess is None:
            return
        fut = sess.submit(sess.write_file, path, b"")
        self._refresh_after(fut, parent_item, parent_entry.path)

    def _new_folder_under(self, parent_entry: RemoteEntry, parent_item: QTreeWidgetItem):
        name, ok = QInputDialog.getText(self, t("remote.new_folder"), t("remote.prompt_new_name"))
        if not ok or not name.strip():
            return
        path = posixpath.join(parent_entry.path, name.strip())
        sess = self._session
        if sess is None:
            return
        fut = sess.submit(sess.mkdir, path)
        self._refresh_after(fut, parent_item, parent_entry.path)

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
        parent_item = item.parent() or self._tree.topLevelItem(0)
        self._refresh_after(fut, parent_item, parent_path)

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
        parent_item = item.parent() or self._tree.topLevelItem(0)
        parent_path = posixpath.dirname(entry.path.rstrip("/")) or "/"
        self._refresh_after(fut, parent_item, parent_path)

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
                QTimer.singleShot(0, lambda: self._toast_error(str(e)))
        fut.add_done_callback(on_done)

    def _upload_into(self, dir_entry: RemoteEntry, dir_item: QTreeWidgetItem):
        local_path, _ = QFileDialog.getOpenFileName(self, t("remote.upload"))
        if not local_path:
            return
        sess = self._session
        if sess is None:
            return
        remote_path = posixpath.join(dir_entry.path, os.path.basename(local_path))
        fut = sess.submit(sess.upload, local_path, remote_path)
        self._refresh_after(fut, dir_item, dir_entry.path)

    def _refresh_after(self, fut, item: QTreeWidgetItem, path: str):
        def on_done(f):
            try:
                f.result()
            except Exception as e:
                QTimer.singleShot(0, lambda: self._toast_error(str(e)))
                return
            QTimer.singleShot(0, lambda: self._reload_subtree(item, path))
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
