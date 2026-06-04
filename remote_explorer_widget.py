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
    QApplication, QSizePolicy, QProgressDialog, QStyledItemDelegate,
    QAbstractItemView, QDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QMimeData, QUrl, QSize
from PyQt6.QtGui import QAction, QCursor, QDrag, QShortcut, QKeySequence, QColor, QBrush
from PyQt6 import sip  # 用于检查 C++ 对象是否已被删除

from i18n import t
import explorer_clipboard
import remote_bookmarks
from ssh_session import (
    HostConfig, RemoteEntry, SSHSession, parse_ssh_config, append_ssh_config_host,
    rename_ssh_config_host,
)
from git_widget import _make_git_tool_icon  # 复用统一风格的矢量线条图标


# 子项的 UserRole 数据键
_ROLE_ENTRY = Qt.ItemDataRole.UserRole
_ROLE_LOADED = Qt.ItemDataRole.UserRole + 1


class _AddHostDialog(QDialog):
    """SSH 主机文本输入对话框 —— 暗色主题，替代原生 QInputDialog。

    复用项目里其它对话框的配色（#1e1e2e 背景 / #667eea 强调色 / #3d3d5c 边框）。
    默认用于「添加主机」；传入 title/hint/placeholder/initial/ok_label 可复用为
    「重命名」等场景。返回值通过 `value()` 取。
    """

    def __init__(self, parent=None, *, title=None, hint=None,
                 placeholder="deploy@10.0.0.5:22", initial="", ok_label=None):
        super().__init__(parent)
        title = title or t("remote.add_host_title")
        hint = hint if hint is not None else t("remote.add_host_hint")
        ok_label = ok_label or t("remote.add_host_ok")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(360)
        self.setStyleSheet("""
            QDialog {
                background-color: #1e1e2e;
            }
            QLabel#title {
                color: #eaeaea;
                font-size: 15px;
                font-weight: 600;
            }
            QLabel#hint {
                color: #8a8aa0;
                font-size: 12px;
            }
            QLineEdit {
                background-color: #2d2d44;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                padding: 8px 10px;
                font-size: 13px;
                selection-background-color: #667eea;
            }
            QLineEdit:focus {
                border: 1px solid #667eea;
            }
            QPushButton {
                background-color: #2d2d44;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
                padding: 7px 18px;
                font-size: 13px;
            }
            QPushButton:hover {
                border: 1px solid #6a5d8a;
            }
            QPushButton#ok {
                background-color: #667eea;
                border: 1px solid #667eea;
                color: white;
                font-weight: 600;
            }
            QPushButton#ok:hover {
                background-color: #764ba2;
                border: 1px solid #764ba2;
            }
            QPushButton#ok:disabled {
                background-color: #3a3a52;
                border: 1px solid #3a3a52;
                color: #777;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("title")
        layout.addWidget(title_lbl)

        if hint:
            hint_lbl = QLabel(hint)
            hint_lbl.setObjectName("hint")
            hint_lbl.setWordWrap(True)
            layout.addWidget(hint_lbl)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(placeholder)
        self._edit.setClearButtonEnabled(True)
        if initial:
            self._edit.setText(initial)
            self._edit.selectAll()
        layout.addWidget(self._edit)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        cancel_btn = QPushButton(t("remote.add_host_cancel"))
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._ok_btn = QPushButton(ok_label)
        self._ok_btn.setObjectName("ok")
        self._ok_btn.setDefault(True)
        self._ok_btn.setEnabled(bool(initial.strip()))
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._ok_btn)

        layout.addLayout(btn_row)

        # 输入为空时禁用 OK，回车直接提交
        self._edit.textChanged.connect(
            lambda s: self._ok_btn.setEnabled(bool(s.strip()))
        )
        self._edit.returnPressed.connect(
            lambda: self.accept() if self._edit.text().strip() else None
        )
        self._edit.setFocus()

    def value(self) -> str:
        return self._edit.text().strip()


class _RemoteTreeWidget(QTreeWidget):
    """支持文件拖入（上传）/ 拖出（下载到临时文件后给外部 URL）的远程文件树。
    并支持**内部**拖拽（同一棵远程树里把文件/目录移动到另一个文件夹）。

    panel: 反向引用 RemoteExplorerPanel，用于实际执行 SFTP 上传/下载/move。
    """

    # 内部拖拽用的私有 MIME 类型：标识这次 drag 起源于自己这棵树
    REMOTE_PATHS_MIME = "application/x-stellar-remote-paths"

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeWidget.DragDropMode.DragDrop)
        # 多选：单击=选中一个，Shift+点击=连续区间选择，
        # Cmd（macOS）/ Ctrl（其他平台）+ 点击=切换单个选中
        self.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)

        # 编辑触发：只允许 F2 / 代码触发，双击保留给"进入目录 / 打开文件"
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)

        # Finder 风格：单击已选中的条目 → 延迟进入原地重命名
        self._pending_rename_item = None  # QTreeWidgetItem 或 None
        self._rename_timer = QTimer(self)
        self._rename_timer.setSingleShot(True)
        self._rename_timer.timeout.connect(self._fire_pending_rename)

    # ----- 原地重命名键鼠交互 -----

    def _cancel_pending_rename(self):
        self._rename_timer.stop()
        self._pending_rename_item = None

    def _fire_pending_rename(self):
        item = self._pending_rename_item
        self._pending_rename_item = None
        if item is None:
            return
        try:
            if sip.isdeleted(item):
                return
        except Exception:
            pass
        # 仍是唯一选中项时才进入编辑，避免延迟期间用户已切走
        if item.isSelected() and len(self.selectedItems()) == 1:
            self.editItem(item, 0)

    def _editable_single_selection(self) -> Optional["QTreeWidgetItem"]:
        sel = self.selectedItems()
        if len(sel) != 1:
            return None
        item = sel[0]
        # 仅当条目挂着 RemoteEntry 时才允许重命名（占位 "…" 没有 entry）
        if item.data(0, _ROLE_ENTRY) is None:
            return None
        return item

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        # Delete 或 Cmd+Backspace（macOS Finder 风格）→ 批量删除选中项
        # Qt 在 macOS 上把 Cmd 映射到 ControlModifier，跨平台都用 Control 判定。
        is_delete = (key == Qt.Key.Key_Delete) or (
            key == Qt.Key.Key_Backspace
            and bool(mods & Qt.KeyboardModifier.ControlModifier)
        )
        if is_delete:
            entries = []
            seen = set()
            for it in self.selectedItems():
                e: RemoteEntry = it.data(0, _ROLE_ENTRY)
                if e is None or e.path in seen:
                    continue
                seen.add(e.path)
                entries.append((e, it))
            if entries:
                self._cancel_pending_rename()
                self._panel._delete_entries(entries)
                event.accept()
                return

        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2):
            item = self._editable_single_selection()
            if item is not None:
                self._cancel_pending_rename()
                self.editItem(item, 0)
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            item = self.itemAt(event.position().toPoint())
            if item is not None and item.data(0, _ROLE_ENTRY) is not None:
                # 该项在本次点击前已是唯一选中项 → 等双击窗口结束后进入重命名
                was_only_selected = (
                    item.isSelected() and len(self.selectedItems()) == 1
                )
                if was_only_selected:
                    self._pending_rename_item = item
                    self._rename_timer.start(
                        QApplication.doubleClickInterval() + 80
                    )
                else:
                    self._cancel_pending_rename()
            else:
                self._cancel_pending_rename()
        else:
            self._cancel_pending_rename()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # 双击 → 取消延迟重命名，让 itemDoubleClicked 走打开/进入目录
        self._cancel_pending_rename()
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        # 按住左键拖动（可能是拖拽）→ 取消延迟重命名
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._pending_rename_item is not None):
            self._cancel_pending_rename()
        super().mouseMoveEvent(event)

    # ----- 工具 -----

    def _alive(self) -> bool:
        """C++ 对象是否仍存活。PyQt6 在 widget 被销毁后，留下的 Python wrapper
        访问任何成员都会抛 `RuntimeError: wrapped C/C++ object ... has been deleted`，
        而且这个异常如果发生在 Qt 内部回调（如 startDrag）里，PyQt 会 abort 进程。
        所有的事件回调入口先 check 一下。"""
        try:
            return not sip.isdeleted(self)
        except Exception:
            return False

    def _is_internal_drag(self, event) -> bool:
        try:
            return event.source() is self and event.mimeData().hasFormat(self.REMOTE_PATHS_MIME)
        except RuntimeError:
            return False

    # ----- 接收外部文件 → 上传 / 内部拖拽 → 移动 -----

    def dragEnterEvent(self, event):
        if not self._alive():
            return
        try:
            if self._is_internal_drag(event):
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
            elif event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                super().dragEnterEvent(event)
        except RuntimeError:
            # widget 在 event 处理过程中被销毁
            return

    def dragMoveEvent(self, event):
        if not self._alive():
            return
        try:
            if self._is_internal_drag(event):
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
            elif event.mimeData().hasUrls():
                event.acceptProposedAction()
            else:
                super().dragMoveEvent(event)
        except RuntimeError:
            return

    def dropEvent(self, event):
        if not self._alive():
            return
        try:
            # 解析落点目标目录（内部、外部都要用）
            target_item = self.itemAt(event.position().toPoint())
            target_dir = None
            if target_item is not None:
                entry: RemoteEntry = target_item.data(0, _ROLE_ENTRY)
                if entry and entry.is_dir:
                    target_dir = entry.path
                elif entry:
                    target_dir = posixpath.dirname(entry.path.rstrip("/")) or "/"
            if not target_dir:
                target_dir = self._panel._current_path

            # ---- 内部拖拽：远端 → 远端 移动 ----
            if self._is_internal_drag(event):
                raw = bytes(event.mimeData().data(self.REMOTE_PATHS_MIME)).decode("utf-8", "replace")
                src_paths = [p for p in raw.splitlines() if p]
                if not src_paths:
                    event.ignore()
                    return
                event.setDropAction(Qt.DropAction.MoveAction)
                event.accept()
                self._panel._handle_internal_move(src_paths, target_dir, target_item)
                return

            # ---- 外部拖入：本地文件 → 上传 ----
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
            event.acceptProposedAction()
            self._panel._handle_drop_upload(local_paths, target_dir, target_item)
        except RuntimeError:
            return

    # ----- 拖出 -----

    def startDrag(self, supported_actions):
        """启动拖拽 —— 必须立即返回，且必须对 widget 已销毁场景免疫。

        触发场景：用户关掉了 tab/窗口时，Qt 可能已经把 _RemoteTreeWidget 的
        C++ 对象 deleteLater，但仍有一个 pending mouseMoveEvent 触发了
        startDrag —— Python wrapper 还在，但 self.selectedItems() 等任何方法
        都会抛 RuntimeError，未捕获就会让 PyQt abort 进程，所有窗口跟着崩。
        """
        if not self._alive():
            return
        try:
            items = self.selectedItems()
            if not items:
                return
            entries = [it.data(0, _ROLE_ENTRY) for it in items]
            entries = [e for e in entries if e is not None]
            if not entries:
                return

            mime = QMimeData()
            paths_text = "\n".join(e.path for e in entries)
            # 私有 MIME：dropEvent 用来识别"这次 drag 来自本树"
            mime.setData(self.REMOTE_PATHS_MIME, paths_text.encode("utf-8"))
            # 顺手提供 plain text，把远端路径拖进终端/编辑器即得到字符串
            mime.setText(paths_text)

            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
        except RuntimeError:
            # widget 在 drag 启动过程中被销毁
            return


class _RemoteItemDelegate(QStyledItemDelegate):
    """原地重命名时只把 entry.name 喂给编辑框，避免用户看到/编辑到 emoji 前缀；
    提交时绕过 model 直接走 SFTP rename，由 panel 在成功后刷新条目文本。"""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    def setEditorData(self, editor, index):
        item = self._panel._tree.itemFromIndex(index) if index.isValid() else None
        entry: Optional[RemoteEntry] = (
            item.data(0, _ROLE_ENTRY) if item is not None else None
        )
        if entry is not None and isinstance(editor, QLineEdit):
            editor.setText(entry.name)
            # 默认只选中"基名"部分，避免误改扩展名；目录或无扩展名时全选
            if entry.is_dir:
                editor.selectAll()
                return
            stem, ext = posixpath.splitext(entry.name)
            if not ext or not stem:
                editor.selectAll()
                return
            sel_len = len(stem)

            def _apply():
                try:
                    editor.setSelection(0, sel_len)
                except RuntimeError:
                    pass

            # 推到事件队列末尾，避免被 Qt item-view 内部的 show/focus 流程覆盖
            QTimer.singleShot(0, _apply)
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        item = self._panel._tree.itemFromIndex(index) if index.isValid() else None
        entry: Optional[RemoteEntry] = (
            item.data(0, _ROLE_ENTRY) if item is not None else None
        )
        if entry is None or not isinstance(editor, QLineEdit):
            super().setModelData(editor, model, index)
            return
        # 不直接写回 model（防止把不带 emoji 的纯名字盖到显示文本上）；
        # 让 panel 异步走 SFTP rename，成功后会刷新父目录把条目重建出来。
        new_name = editor.text().strip()
        self._panel._do_inline_rename(entry, item, new_name)


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
    _file_ready = pyqtSignal(str, str, str)  # host_alias, remote_path, local_path (流式下载完成)
    _download_progress = pyqtSignal(str, int, int)  # remote_path, bytes_done, bytes_total
    _stat_resolved = pyqtSignal(object, str)          # (entry, requested_path)
    _refresh_root_signal = pyqtSignal()
    _refresh_subtree_signal = pyqtSignal(object, str)  # (item, path)
    _auto_refresh_result = pyqtSignal(str, object)    # (path, entries or None on error)

    def __init__(self, theme: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme = theme or {}
        self._session: Optional[SSHSession] = None
        # 上一次成功连上的主机和当时的目录；socket 掉了 → 直接拿这两个一键重连
        self._last_connected_host: Optional[HostConfig] = None
        self._last_connected_cwd: Optional[str] = None
        # 同一会话只弹一次重连框（多个并发 future 都 fail 时不刷屏）
        self._reconnect_dialog_open: bool = False
        self._current_path: str = "/"
        self._hosts: list[HostConfig] = []
        # 主窗口可用 set_extra_hosts() 注入手工添加的主机
        self._extra_hosts: list[HostConfig] = []
        # 维护已下载的临时文件 -> (host_alias, remote_path) 映射，
        # 便于编辑器保存时调度上传
        self._open_temp_map: dict[str, tuple[str, str]] = {}
        # local_path -> 发起下载时所用的 session（回调时 self._session 可能已切换/断开）
        self._open_session_map: dict[str, "SSHSession"] = {}
        # 新建文件/文件夹后，等刷新把它装进树里再原地重命名（不弹窗）
        self._pending_edit_path: Optional[str] = None

        self._setup_ui()
        self._apply_theme()
        self._reload_hosts()

        # 内部跨线程信号 → UI 线程槽（QueuedConnection 自动应用）
        self._top_level_ready.connect(self._apply_top_level)
        self._subtree_ready.connect(self._apply_children)
        self._error_signal.connect(self._toast_error)
        self._file_downloaded.connect(self._on_file_downloaded)
        self._file_ready.connect(self._on_file_ready)
        self._download_progress.connect(self._on_download_progress)
        # 节流下载进度文案：paramiko 回调每个 chunk 都触发，UI 100ms 一次足够
        self._last_progress_emit_ts = 0.0
        self._stat_resolved.connect(self._on_stat_resolved)
        self._auto_refresh_result.connect(self._on_auto_refresh_result)

        # 自动刷新（轮询）：连接后启动，断开/隐藏时停。
        # - 周期 10s；每次只重发那些当前可见（根 + 已展开）的目录的 listdir
        # - 同 fingerprint（name+is_dir 集合）则不动；变化时做增量 add/remove，
        #   绝不重建整棵子树，避免毁掉用户的展开/选中状态。
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(10_000)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_tick)
        self._auto_refresh_pending: int = 0  # 本轮还在 in-flight 的 listdir 个数
        self._auto_refresh_fingerprints: dict[str, frozenset] = {}
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
        # 关键：下载进度文案频繁更新到这个 label。如果让它的 sizeHint 随文字
        # 变长 / 变短，整个 header / 父 splitter 都会跟着 reflow，看起来像窗口
        # 一直在轻微抖动。Ignored 水平策略 = "我不在意我的首选宽度，layout 给我
        # 多少我用多少"，文字变化不再触发布局重算。
        self._subtitle_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        self._subtitle_label.setMinimumWidth(0)
        h_layout.addWidget(self._subtitle_label, 1)  # 占据中间剩余空间
        # （原来这里有 addStretch；现在交给上面 stretch=1 的 label 接管）

        # 三个统一风格的矢量线条图标按钮（图标在 _apply_theme 里按主题色绘制）
        self._reload_btn = QPushButton()
        self._reload_btn.setFixedSize(28, 28)
        self._reload_btn.setIconSize(QSize(16, 16))
        self._reload_btn.setToolTip(t("remote.refresh_hosts"))
        self._reload_btn.clicked.connect(self._reload_hosts)
        h_layout.addWidget(self._reload_btn)

        self._add_btn = QPushButton()
        self._add_btn.setFixedSize(28, 28)
        self._add_btn.setIconSize(QSize(16, 16))
        self._add_btn.setToolTip(t("remote.add_host"))
        self._add_btn.clicked.connect(self._on_add_host_clicked)
        h_layout.addWidget(self._add_btn)

        self._disconnect_btn = QPushButton()
        self._disconnect_btn.setFixedSize(28, 28)
        self._disconnect_btn.setIconSize(QSize(16, 16))
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
        self._hosts_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._hosts_list.customContextMenuRequested.connect(self._on_hosts_context_menu)
        hp_layout.addWidget(self._hosts_list, 1)

        self._stack.addWidget(self._hosts_page)

        # --- 文件树页 ---
        self._tree_page = QWidget()
        tp_layout = QVBoxLayout(self._tree_page)
        tp_layout.setContentsMargins(0, 0, 0, 0)
        tp_layout.setSpacing(0)

        # 路径栏 + 操作按钮
        self._path_bar = QFrame()
        self._path_bar.setFixedHeight(32)
        pb_layout = QHBoxLayout(self._path_bar)
        pb_layout.setContentsMargins(6, 2, 6, 2)
        pb_layout.setSpacing(2)

        # 四个统一风格的矢量线条图标按钮（图标在 _apply_theme 里按主题色绘制）
        self._up_btn = QPushButton()
        self._up_btn.setFixedSize(26, 26)
        self._up_btn.setIconSize(QSize(16, 16))
        self._up_btn.setToolTip(t("remote.up"))
        self._up_btn.clicked.connect(self._on_up)
        pb_layout.addWidget(self._up_btn)

        self._home_btn = QPushButton()
        self._home_btn.setFixedSize(26, 26)
        self._home_btn.setIconSize(QSize(16, 16))
        self._home_btn.setToolTip(t("remote.go_home"))
        self._home_btn.clicked.connect(self._on_home)
        pb_layout.addWidget(self._home_btn)

        self._refresh_btn = QPushButton()
        self._refresh_btn.setFixedSize(26, 26)
        self._refresh_btn.setIconSize(QSize(16, 16))
        self._refresh_btn.setToolTip(t("remote.refresh"))
        self._refresh_btn.clicked.connect(self._on_refresh)
        pb_layout.addWidget(self._refresh_btn)

        # 书签按钮：弹出菜单管理 / 跳转到已保存的远端路径（图标随收藏状态切换实/空心）
        self._bookmark_btn = QPushButton()
        self._bookmark_btn.setFixedSize(26, 26)
        self._bookmark_btn.setIconSize(QSize(16, 16))
        self._bookmark_btn.setToolTip(t("remote.bookmarks_tooltip"))
        self._bookmark_btn.clicked.connect(self._show_bookmark_menu)
        pb_layout.addWidget(self._bookmark_btn)

        self._path_edit = QLineEdit()
        self._path_edit.returnPressed.connect(self._on_path_edited)
        pb_layout.addWidget(self._path_edit, 1)

        tp_layout.addWidget(self._path_bar)

        self._tree = _RemoteTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        # 原地重命名的编辑器代理（去掉 emoji 前缀，由 panel 异步走 SFTP rename）
        self._tree.setItemDelegate(_RemoteItemDelegate(self, self._tree))
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
                background-color: transparent;
                border: 1px solid transparent; border-radius: 5px;
            }}
            QPushButton:hover {{ background-color: {bg_hover}; }}
            QPushButton:pressed {{ background-color: {border}; }}
        """)
        # 用主题前景色重绘头部三个线条图标，保证大小/粗细/对齐统一
        self._icon_color = text  # 书签按钮按收藏状态切换图标时复用
        self._reload_btn.setIcon(_make_git_tool_icon('refresh', text))
        self._add_btn.setIcon(_make_git_tool_icon('plus', text))
        self._disconnect_btn.setIcon(_make_git_tool_icon('close', text))

        # 文件树页的导航工具栏：同一套矢量图标 + 一致的悬停样式
        self._path_bar.setStyleSheet(f"""
            QFrame {{ background-color: {bg_medium}; border-bottom: 1px solid {border}; }}
            QPushButton {{
                background-color: transparent;
                border: 1px solid transparent; border-radius: 5px;
            }}
            QPushButton:hover {{ background-color: {bg_hover}; }}
            QPushButton:pressed {{ background-color: {border}; }}
        """)
        self._up_btn.setIcon(_make_git_tool_icon('up', text))
        self._home_btn.setIcon(_make_git_tool_icon('home', text))
        self._refresh_btn.setIcon(_make_git_tool_icon('refresh', text))
        self._update_bookmark_btn_state()  # 按当前收藏状态画 ★/☆
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
            /* 原地重命名编辑框：显式配色和边距，避免文本被裁切 */
            QTreeWidget QLineEdit {{
                background-color: {bg_dark};
                color: {text};
                border: 1px solid {accent};
                padding: 0 4px;
                margin: 0;
                min-height: 18px;
                selection-background-color: {accent};
                selection-color: white;
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
        self._bookmark_btn.setToolTip(t("remote.bookmarks_tooltip"))
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
        dlg = _AddHostDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = dlg.value()
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
        user = user.strip()
        # 持久化到 ~/.ssh/config，使其成为系统记录、重启后仍在
        try:
            append_ssh_config_host(hostname=host, user=user, port=port)
            self._reload_hosts()  # 从 config 重新读取，新主机会出现在列表里
        except Exception as e:
            # 写盘失败（权限等）→ 退回到仅本会话内存，避免直接丢失
            QMessageBox.warning(
                self, t("remote.add_host_title"),
                t("remote.add_host_save_failed").format(error=e),
            )
            cfg = HostConfig(alias=text, hostname=host, user=user, port=port)
            self._extra_hosts.append(cfg)
            self._populate_hosts_list()

    def _on_host_activated(self, item: QListWidgetItem):
        host: HostConfig = item.data(_ROLE_ENTRY)
        if not host:
            return
        self._connect_to(host)

    def _on_hosts_context_menu(self, pos):
        """主机列表右键菜单：连接 / 重命名（改别名，方便管理）。"""
        item = self._hosts_list.itemAt(pos)
        if item is None:
            return
        host: HostConfig = item.data(_ROLE_ENTRY)
        if not host:
            return
        bg_medium = self.theme.get('bg_medium', '#16213e')
        text = self.theme.get('text', '#eaeaea')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {bg_medium}; color: {text};
                border: 1px solid {border}; border-radius: 4px; padding: 4px;
            }}
            QMenu::item {{ padding: 6px 20px; border-radius: 3px; }}
            QMenu::item:selected {{ background-color: {accent}; }}
            QMenu::separator {{ height: 1px; background-color: {border}; margin: 4px 10px; }}
        """)
        connect_act = menu.addAction(t("remote.connect"))
        connect_act.triggered.connect(lambda checked=False, h=host: self._connect_to(h))
        rename_act = menu.addAction(t("remote.rename_host"))
        rename_act.triggered.connect(lambda checked=False, h=host: self._rename_host(h))
        menu.exec(self._hosts_list.viewport().mapToGlobal(pos))

    def _rename_host(self, host: HostConfig):
        """给主机起个好记的别名（rename），并写回 ~/.ssh/config。"""
        dlg = _AddHostDialog(
            self,
            title=t("remote.rename_host_title"),
            hint=t("remote.rename_host_hint"),
            placeholder=t("remote.rename_host_placeholder"),
            initial=host.alias,
            ok_label=t("remote.rename_host_ok"),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_alias = dlg.value()
        if not new_alias or new_alias == host.alias:
            return

        try:
            renamed = rename_ssh_config_host(host.alias, new_alias)
        except ValueError:
            QMessageBox.warning(
                self, t("remote.rename_host_title"),
                t("remote.rename_host_exists").format(alias=new_alias),
            )
            return
        except Exception as e:
            QMessageBox.warning(
                self, t("remote.rename_host_title"),
                t("remote.rename_host_failed").format(error=e),
            )
            return

        if renamed:
            self._reload_hosts()            # config 改了 → 重新读取
        else:
            host.alias = new_alias          # 仅内存中的主机（未落 config）→ 直接改
            self._populate_hosts_list()

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
        self._auto_refresh_timer.start()
        # 记住最近连上的 host，供"断线后一键重连"使用
        self._last_connected_host = sess.host_config
        self._last_connected_cwd = self._current_path
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
        self._auto_refresh_timer.stop()
        self._auto_refresh_fingerprints.clear()
        self._auto_refresh_pending = 0
        self._stack.setCurrentWidget(self._hosts_page)
        self._tree.clear()
        self._subtitle_label.setText("")
        self._add_btn.show()
        self._reload_btn.show()
        self._disconnect_btn.hide()

    def closeEvent(self, event):
        """窗口被销毁时关掉 SSH 会话，避免后台线程在 panel 已销毁后还回调
        Qt slot → C++ 侧 use-after-free。
        注意：直接 disconnect() 会阻塞最多 5 秒（paramiko 关 sftp 走 5s timeout）。
        在 closeEvent 里阻塞会让窗口关闭看起来卡死。这里把 session 引用先摘下
        来交给一个 daemon 线程异步关闭。
        """
        if self._session is not None:
            sess = self._session
            self._session = None
            import threading
            def _bg_disconnect():
                try:
                    sess.disconnect()
                except Exception as e:
                    print(f"[RemoteExplorerPanel] async disconnect failed: {e}")
            threading.Thread(target=_bg_disconnect, name="ssh-bg-disconnect", daemon=True).start()
        super().closeEvent(event)

    # ---------- 文件树 ----------

    def _populate_tree_root(self):
        """加载当前路径的内容到顶层（不再包一层 path 节点）

        和本地 Explorer 行为一致：path 在路径栏里显示，文件/目录直接平铺在树根。
        """
        # 整树重建 → 旧的自动刷新指纹全部失效，等 _apply_top_level 后重新基线
        self._auto_refresh_fingerprints.clear()
        self._tree.clear()
        if self._session is None:
            return
        # path 已变 → 更新 ★/☆ 指示
        self._update_bookmark_btn_state()
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

    # 隐藏条目（以点开头）的前景色，和本地 Explorer 保持一致
    HIDDEN_COLOR = QColor("#888888")

    @classmethod
    def _make_item(cls, e: RemoteEntry) -> QTreeWidgetItem:
        """根据一个远端条目构建树节点（含图标、隐藏文件浅灰、占位子项）。

        三处建项逻辑（顶层 / 展开子项 / 自动刷新新增）共用此方法，避免样式漏改。
        """
        icon = "📁 " if e.is_dir else "📄 "
        item = QTreeWidgetItem([icon + e.name])
        item.setData(0, _ROLE_ENTRY, e)
        item.setData(0, _ROLE_LOADED, False)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        # 隐藏文件/文件夹（以点开头）以浅灰显示
        if e.name.startswith('.'):
            item.setForeground(0, QBrush(cls.HIDDEN_COLOR))
        if e.is_dir:
            # 占位让箭头出现，展开时才真正去 listdir
            item.addChild(QTreeWidgetItem(["…"]))
        return item

    def _apply_top_level(self, entries: list[RemoteEntry]):
        """把目录内容直接放到树根（批量插入 + 关闭重绘减少卡顿）"""
        try:
            self._tree.setUpdatesEnabled(False)
            self._tree.clear()
            items: list[QTreeWidgetItem] = []
            for e in entries:
                items.append(self._make_item(e))
            if items:
                self._tree.addTopLevelItems(items)
            # 把这次 populate 的内容立即记成自动刷新的基线 —— 否则下一次 poll
            # 会把 "manual populate 之后服务器又写了新文件" 错过去
            self._auto_refresh_fingerprints[self._current_path] = (
                self._entries_fingerprint(entries)
            )
        except RuntimeError:
            return
        finally:
            try:
                self._tree.setUpdatesEnabled(True)
            except RuntimeError:
                pass
        # 刚新建的条目若已进树 → 直接进入原地重命名
        self._maybe_start_pending_edit()

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
        # 用户点刷新 → 绕过缓存，重新拉
        if self._session is not None:
            self._session.invalidate_cache(self._current_path)
        self._populate_tree_root()

    # ---------- 自动刷新（轮询）----------

    def _auto_refresh_tick(self):
        """每 10s 触发一次：拉根 + 已展开目录，做指纹对比 + 增量更新"""
        if self._session is None or not self._session.is_connected():
            return
        if not self.isVisible():
            return  # 看不见的 panel 不浪费带宽
        if self._auto_refresh_pending > 0:
            return  # 上一轮还没回来，跳过这次
        targets = self._collect_auto_refresh_paths()
        if not targets:
            return
        sess = self._session
        self._auto_refresh_pending = len(targets)
        for path in targets:
            self._submit_auto_refresh(sess, path)

    def _collect_auto_refresh_paths(self) -> list[str]:
        """根 + 所有已展开且已加载的子目录的路径，去重，cap 在 8 个以内"""
        out: list[str] = [self._current_path]

        def walk(item: QTreeWidgetItem):
            if not item.isExpanded():
                return
            if not item.data(0, _ROLE_LOADED):
                return  # 占位状态 → 真展开时才会拉 listdir
            entry: RemoteEntry = item.data(0, _ROLE_ENTRY)
            if entry is not None and entry.is_dir:
                out.append(entry.path)
                for i in range(item.childCount()):
                    walk(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))

        # 去重 + 限流
        seen = set()
        deduped: list[str] = []
        for p in out:
            if p in seen:
                continue
            seen.add(p)
            deduped.append(p)
            if len(deduped) >= 8:
                break
        return deduped

    def _submit_auto_refresh(self, sess: SSHSession, path: str):
        # use_cache=False —— 强制从服务器拉，否则 30s TTL 内永远拿到旧的
        fut = sess.submit(sess.listdir, path, False)

        def on_done(f):
            try:
                entries = f.result()
            except Exception:
                entries = None
            # 通过信号回 UI 线程
            self._auto_refresh_result.emit(path, entries)

        fut.add_done_callback(on_done)

    def _on_auto_refresh_result(self, path: str, entries):
        """worker 线程 listdir 完成后回到 UI 线程：指纹对比 + 增量更新。

        基线由 _apply_top_level / _apply_children 在 populate 时建立，所以
        即使首次 poll，也能与"已经展示的内容"做差异比较 —— 不会再像之前
        那样把"manual populate 之后但首次 poll 之前出现的新文件"白白吃掉。
        """
        try:
            self._auto_refresh_pending = max(0, self._auto_refresh_pending - 1)
            if entries is None:
                # 网络错误或目录不存在了：清掉指纹，下次会重新建基线
                self._auto_refresh_fingerprints.pop(path, None)
                return
            fp = self._entries_fingerprint(entries)
            old_fp = self._auto_refresh_fingerprints.get(path)
            self._auto_refresh_fingerprints[path] = fp
            if fp == old_fp:
                return
            self._auto_refresh_apply(path, entries)
        except RuntimeError:
            # widget 已销毁
            return

    def _auto_refresh_apply(self, path: str, entries: list):
        """增量更新一个目录的子项：删消失的、加新出现的，不动已存在的"""
        # 找到父节点
        if path == self._current_path:
            parent_item = None
            existing = [self._tree.topLevelItem(i)
                        for i in range(self._tree.topLevelItemCount())]
        else:
            parent_item = self._find_item_by_path(path)
            if parent_item is None:
                return
            if not parent_item.data(0, _ROLE_LOADED):
                return  # 父还是占位状态，避免越权填充
            existing = [parent_item.child(i)
                        for i in range(parent_item.childCount())]

        existing_by_name: dict[str, QTreeWidgetItem] = {}
        for it in existing:
            e: RemoteEntry = it.data(0, _ROLE_ENTRY)
            if e is not None:
                existing_by_name[e.name] = it

        new_names = {e.name for e in entries}

        # 删掉已不存在的（保留当前选中项的展开状态等都不受影响）
        for name in list(existing_by_name):
            if name in new_names:
                continue
            it = existing_by_name.pop(name)
            if parent_item is None:
                idx = self._tree.indexOfTopLevelItem(it)
                if idx >= 0:
                    self._tree.takeTopLevelItem(idx)
            else:
                parent_item.removeChild(it)

        # 把新增的按字典序插进去（保持 listdir 顺序即可）
        for e in entries:
            if e.name in existing_by_name:
                continue
            child = self._make_item(e)
            if parent_item is None:
                self._tree.addTopLevelItem(child)
            else:
                parent_item.addChild(child)

    def _find_item_by_path(self, path: str) -> Optional[QTreeWidgetItem]:
        """在树里 DFS 找 entry.path == path 的 QTreeWidgetItem"""
        def dfs(item: QTreeWidgetItem) -> Optional[QTreeWidgetItem]:
            e: RemoteEntry = item.data(0, _ROLE_ENTRY)
            if e is not None and e.path == path:
                return item
            for i in range(item.childCount()):
                hit = dfs(item.child(i))
                if hit is not None:
                    return hit
            return None

        for i in range(self._tree.topLevelItemCount()):
            hit = dfs(self._tree.topLevelItem(i))
            if hit is not None:
                return hit
        return None

    # ---------- 书签 ----------

    def _show_bookmark_menu(self):
        """弹出书签菜单：添加/移除当前路径 + 已保存书签列表"""
        if self._session is None:
            return
        host = self._session.host_config.alias
        cwd = self._current_path or "/"
        menu = self._make_menu()

        # 顶部：加/删 当前路径
        if remote_bookmarks.is_bookmarked(host, cwd):
            act = QAction(t("remote.bookmark_remove", path=cwd), self)
            act.triggered.connect(lambda: self._toggle_bookmark(cwd, add=False))
        else:
            act = QAction(t("remote.bookmark_add", path=cwd), self)
            act.triggered.connect(lambda: self._toggle_bookmark(cwd, add=True))
        menu.addAction(act)
        menu.addSeparator()

        entries = remote_bookmarks.list_for(host)
        if not entries:
            placeholder = QAction(t("remote.bookmarks_empty"), self)
            placeholder.setEnabled(False)
            menu.addAction(placeholder)
        else:
            for p in entries:
                act_jump = QAction(p, self)
                act_jump.triggered.connect(lambda checked=False, path=p: self._goto_bookmark(path))
                menu.addAction(act_jump)
            menu.addSeparator()
            act_clear = QAction(t("remote.bookmarks_clear"), self)
            act_clear.triggered.connect(lambda: self._clear_bookmarks(host))
            menu.addAction(act_clear)

        # 弹在按钮正下方
        pos = self._bookmark_btn.mapToGlobal(self._bookmark_btn.rect().bottomLeft())
        menu.exec(pos)
        # 状态可能变了 → 更新按钮显示
        self._update_bookmark_btn_state()

    def _toggle_bookmark(self, path: str, add: bool):
        if self._session is None:
            return
        host = self._session.host_config.alias
        if add:
            remote_bookmarks.add(host, path)
        else:
            remote_bookmarks.remove(host, path)
        self._update_bookmark_btn_state()

    def _goto_bookmark(self, path: str):
        """跳到一个已保存的书签路径（先 stat 防止路径已失效）"""
        if self._session is None:
            return
        sess = self._session
        fut = sess.submit(sess.stat, path)

        def on_done(f):
            try:
                entry: RemoteEntry = f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
                return
            self._stat_resolved.emit(entry, path)
        fut.add_done_callback(on_done)

    def _clear_bookmarks(self, host: str):
        # 二次确认：清空书签不可撤销，避免误点
        entries = remote_bookmarks.list_for(host)
        if not entries:
            return  # 本来就没书签，没什么可清的
        reply = QMessageBox.question(
            self,
            t("remote.bookmarks_clear_confirm_title"),
            t("remote.bookmarks_clear_confirm_msg", host=host, count=len(entries)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,  # 默认聚焦 No，避免回车误清
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        remote_bookmarks.clear_for(host)
        self._update_bookmark_btn_state()

    def _update_bookmark_btn_state(self):
        """根据当前路径是否已收藏，切换实心 ★ / 空心 ☆ 图标"""
        color = getattr(self, '_icon_color', '#eaeaea')
        if self._session is None:
            self._bookmark_btn.setIcon(_make_git_tool_icon('star', color))
            return
        host = self._session.host_config.alias
        cwd = self._current_path or "/"
        starred = remote_bookmarks.is_bookmarked(host, cwd)
        kind = 'star_filled' if starred else 'star'
        self._bookmark_btn.setIcon(_make_git_tool_icon(kind, color))

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
            self._tree.setUpdatesEnabled(False)
            parent_item.takeChildren()
            children: list[QTreeWidgetItem] = []
            for e in entries:
                children.append(self._make_item(e))
            if children:
                parent_item.addChildren(children)
            # 同 _apply_top_level：把这次 populate 立刻当作自动刷新基线
            parent_entry: RemoteEntry = parent_item.data(0, _ROLE_ENTRY)
            if parent_entry is not None:
                self._auto_refresh_fingerprints[parent_entry.path] = (
                    self._entries_fingerprint(entries)
                )
        except RuntimeError:
            return
        finally:
            try:
                self._tree.setUpdatesEnabled(True)
            except RuntimeError:
                pass
        # 刚新建的条目若已进树 → 直接进入原地重命名
        self._maybe_start_pending_edit()

    @staticmethod
    def _entries_fingerprint(entries) -> frozenset:
        return frozenset((e.name, bool(e.is_dir)) for e in entries)

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
            # 空白区域：新建 / 上传 / 终端 / 路径 / 粘贴 / 刷新（都作用在当前 path）
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

            act_term = QAction(t("remote.open_terminal_here"), self)
            act_term.triggered.connect(lambda: self._open_terminal_at_path(cwd))
            menu.addAction(act_term)

            act_copy_path = QAction(t("remote.copy_path"), self)
            act_copy_path.triggered.connect(lambda: QApplication.clipboard().setText(cwd))
            menu.addAction(act_copy_path)

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

        # 删除：右键项在选中里 → 批量删整批选中；否则只删该项
        delete_targets = self._selection_entries_including(entry, item)
        act_delete = QAction(t("remote.delete"), self)
        # triggered 会发出 checked(bool)，用第一个形参吃掉它，否则 targets 会被 False 覆盖
        act_delete.triggered.connect(
            lambda checked=False, targets=delete_targets: self._delete_entries(targets)
        )
        menu.addAction(act_delete)

        # 下载到本地：右键项在选中里 → 批量保存到一个目录；否则单文件另存为
        download_targets = self._selection_entries_including(entry, item)
        # 当 anchor 在选中里时是整批；否则只有 anchor 自己
        if len(download_targets) > 1:
            act_download = QAction(t("remote.download"), self)
            act_download.triggered.connect(
                lambda targets=download_targets: self._download_entries_to_local(targets)
            )
            menu.addAction(act_download)
        elif not entry.is_dir:
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

        Cmd+C 立刻给反馈（内部剪贴板 + 系统文字），同时在后台把选中**文件**
        下载到临时目录，全部就绪后把系统剪贴板替换成本地文件 URL —— 这样
        用户切到 Finder / Slack / Notes 等任意应用 Cmd+V 都能直接拿到文件。
        目录因为体量不可控，仍只放路径文本，不做预下载。
        """
        if self._session is None:
            return
        sess = self._session
        host_alias = sess.host_config.alias
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
            payload.append(("remote", host_alias, e.path, sess))
        if not payload:
            return

        # 1) 内部剪贴板：含 session 信息，用于跨面板/同应用粘贴
        explorer_clipboard.set_items(payload, push_local_paths=None)

        # 2) 系统剪贴板：立即给一个文本快照，免得用户立刻 Cmd+V 啥也没拿到
        cb = QApplication.clipboard()
        text_snapshot = "\n".join(it[2] for it in payload)
        cb.setText(text_snapshot)

        # 3) 后台把文件部分预下载到稳定的临时路径，结束后把剪贴板替换为
        #    file:// URL（这是 Finder / Slack / 大多数 macOS 应用接受的格式）
        file_entries = [(host_alias, e) for e in entries if not e.is_dir]
        if not file_entries:
            return
        # 已经缓存的（之前打开过的）秒级就绪 → 先把它们直接写进 URLs；
        # 还没下载的等下载完一起更新。
        ready_paths: list[str] = []
        pending: list[tuple] = []
        for ha, e in file_entries:
            local_path = self._temp_local_path_for(ha, e.path, e.name)
            if (e.size and os.path.isfile(local_path)
                    and os.path.getsize(local_path) == e.size):
                ready_paths.append(local_path)
            else:
                pending.append((ha, e, local_path))

        if ready_paths and not pending:
            self._push_files_to_system_clipboard(ready_paths, text_snapshot)
            return

        self._begin_clipboard_staging(payload, text_snapshot, ready_paths, pending)

    # ---------- 系统剪贴板预下载（让 Cmd+V 在 Finder 等地方拿到真实文件）----------

    def _temp_local_path_for(self, host_alias: str, remote_path: str, name: str) -> str:
        """与 _open_remote_file 共用的稳定 temp 路径：同一文件再被复制不会重复下载。"""
        safe_alias = "".join(c if c.isalnum() or c in '-._' else '_' for c in host_alias)
        local_dir = os.path.join(
            tempfile.gettempdir(),
            f"smart_terminal_remote_{safe_alias}",
            *remote_path.strip("/").split("/")[:-1],
        )
        os.makedirs(local_dir, exist_ok=True)
        return os.path.join(local_dir, name)

    def _begin_clipboard_staging(self, payload: list, text_snapshot: str,
                                   ready_paths: list, pending: list):
        """后台拉取 pending 里所有文件，全部完成后把系统剪贴板替换为 file:// URL。
        过程中在 subtitle 显示 "Preparing N/M files…"。"""
        sess = self._session
        if sess is None:
            return
        total = len(pending)
        done_count = [0]
        completed: list[str] = list(ready_paths)
        # 标记：这次 staging 的剪贴板文字快照。完成时只在剪贴板仍是该文字时才覆盖
        # （否则说明用户已经 Cmd+C 复制了别的东西，不应该越权改剪贴板）。
        self._clipboard_staging_snapshot = text_snapshot

        def update_progress():
            try:
                self._subtitle_label.setText(
                    t("remote.clipboard_preparing", done=done_count[0], total=total)
                )
            except RuntimeError:
                pass

        def finalize():
            try:
                if completed:
                    self._push_files_to_system_clipboard(completed, text_snapshot)
                # 还原 subtitle
                if self._session is not None:
                    self._subtitle_label.setText(self._session.host_config.alias)
            except RuntimeError:
                pass

        update_progress()

        for host_alias, entry, local_path in pending:
            def make_cb(_lp=local_path, _name=entry.name):
                def cb(f):
                    try:
                        f.result()
                        completed.append(_lp)
                    except Exception as e:
                        self._error_signal.emit(f"{_name}: {e}")
                    done_count[0] += 1
                    # 进度和最终化都必须切回 UI 线程
                    QTimer.singleShot(0, update_progress)
                    if done_count[0] >= total:
                        QTimer.singleShot(0, finalize)
                return cb
            try:
                fut = sess.submit(sess.download, entry.path, local_path)
                fut.add_done_callback(make_cb())
            except Exception as e:
                self._error_signal.emit(f"{entry.path}: {e}")
                done_count[0] += 1
                if done_count[0] >= total:
                    QTimer.singleShot(0, finalize)

    def _push_files_to_system_clipboard(self, local_paths: list, expected_text: str):
        """把 file:// URL 写到系统剪贴板，前提是剪贴板还是我们当初放的文字。
        否则说明用户已经复制了别的东西，不能覆盖。"""
        cb = QApplication.clipboard()
        try:
            current = cb.text()
        except Exception:
            current = ""
        if current != expected_text:
            return  # 用户已经 Cmd+C 了别的内容
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in local_paths])
        # 同时保留文本，避免有些应用只读文本
        mime.setText(expected_text)
        cb.setMimeData(mime)

    def _clipboard_paste_into(self, target_dir: str):
        """把跨面板剪贴板里的项目粘贴到当前远端 target_dir。

        语义（和本地 Explorer 一致）：
        - 源 = 目标同目录（同一 SSH 会话且同 dir）→ 自动加 "(N)" 尾缀，不弹窗。
        - 跨目录 / 跨主机 / 本地→远端 冲突 → 弹一次三选一对话框，可"应用到剩余"。
        """
        if self._session is None:
            return
        items = explorer_clipboard.effective_items()
        if not items or not target_dir:
            return
        sess = self._session

        errors: list[str] = []
        sticky_decision: Optional[str] = None
        cancel_all = False

        # 把目标目录的现有条目名预拉一次，避免每次起名都来回 stat
        existing = self._remote_listing_names(sess, target_dir)

        def remote_name_exists(name: str) -> bool:
            return name in existing

        for it in items:
            if cancel_all:
                break
            kind = it[0]
            try:
                if kind == "local":
                    src = it[1]
                    name = os.path.basename(src.rstrip("/"))
                    dst = posixpath.join(target_dir, name)
                    # 本地 → 远端，永远算跨存储
                    if remote_name_exists(name):
                        decision = self._resolve_paste_conflict(name, sticky_decision)
                        if decision is None:
                            cancel_all = True
                            break
                        action, sticky = decision
                        if sticky:
                            sticky_decision = action
                        if action == "overwrite":
                            self._remote_remove(sess, dst)
                            existing.discard(name)
                        else:
                            name = explorer_clipboard.next_free_name(name, remote_name_exists)
                            dst = posixpath.join(target_dir, name)
                    if os.path.isdir(src) and not os.path.islink(src):
                        self._upload_local_dir(sess, src, dst)
                    else:
                        fut = sess.submit(sess.upload, src, dst)
                        self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                                 dst=target_dir))
                    existing.add(name)

                elif kind == "remote":
                    _, host_alias, remote_src, src_sess = it
                    if src_sess is None or not src_sess.is_connected():
                        errors.append(f"{remote_src}: {t('remote.session_lost')}")
                        continue
                    name = posixpath.basename(remote_src.rstrip("/")) or host_alias
                    dst = posixpath.join(target_dir, name)
                    src_dir = posixpath.dirname(remote_src.rstrip("/")) or "/"
                    same_folder = (src_sess is sess and src_dir == target_dir)
                    # 同源同路径粘到同一目录 → 必走 (N) 序号尾缀
                    if same_folder:
                        if remote_name_exists(name):
                            name = explorer_clipboard.next_free_name(name, remote_name_exists)
                            dst = posixpath.join(target_dir, name)
                    elif remote_name_exists(name):
                        decision = self._resolve_paste_conflict(name, sticky_decision)
                        if decision is None:
                            cancel_all = True
                            break
                        action, sticky = decision
                        if sticky:
                            sticky_decision = action
                        if action == "overwrite":
                            self._remote_remove(sess, dst)
                            existing.discard(name)
                        else:
                            name = explorer_clipboard.next_free_name(name, remote_name_exists)
                            dst = posixpath.join(target_dir, name)
                    # 走临时文件中转：远程源 → 本地 temp → 远程目标
                    self._remote_to_remote(src_sess, sess, remote_src, dst)
                    existing.add(name)
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

    def _remote_listing_names(self, sess: SSHSession, path: str) -> set:
        """拉远端 target_dir 下的现有条目名集合，用来快速做命名冲突检查。

        失败时返回空 set，调用方会回落到单次 stat 判定（next_free_name 内会 stat）。
        """
        try:
            fut = sess.submit(sess.listdir, path)
            entries = fut.result()
            return {e.name for e in entries}
        except Exception:
            return set()

    def _resolve_paste_conflict(self, name: str, sticky: Optional[str]):
        """跨目录/跨主机冲突时的三选一对话框。返回与本地 Explorer 同语义。"""
        if sticky in ("overwrite", "keep"):
            return (sticky, True)
        box = QMessageBox(self)
        box.setWindowTitle(t("paste.conflict_title"))
        box.setText(t("paste.conflict_msg", name=name))
        box.setIcon(QMessageBox.Icon.Question)
        keep_btn = box.addButton(t("paste.btn_keep_both"), QMessageBox.ButtonRole.AcceptRole)
        overwrite_btn = box.addButton(t("paste.btn_overwrite"), QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(t("paste.btn_cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_btn)
        from PyQt6.QtWidgets import QCheckBox
        apply_all = QCheckBox(t("paste.apply_to_all"))
        box.setCheckBox(apply_all)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return None
        action = "overwrite" if clicked is overwrite_btn else "keep"
        return (action, apply_all.isChecked())

    def _refresh_subtree_by_path(self, path: str):
        """如果 tree 顶层有 path 对应的目录项，把它重刷一下；否则刷整棵树"""
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            entry: RemoteEntry = it.data(0, _ROLE_ENTRY)
            if entry and entry.is_dir and entry.path == path:
                self._reload_subtree(it, path)
                return
        self._populate_tree_root()

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
            # 父 widget 可能在等待期间被销毁（如用户关掉 panel/窗口），
            # 此时 progress 也已 deleteLater'd → 任何访问都会段错误
            try:
                if sip.isdeleted(progress):
                    timer.stop()
                    loop.quit()
                    return
                progress.setValue(done["n"])
                if done["n"] >= len(futures):
                    timer.stop()
                    loop.quit()
            except RuntimeError:
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        try:
            if not sip.isdeleted(progress):
                progress.setValue(len(futures))
        except RuntimeError:
            pass
        if done["errors"] and not tolerate_errors:
            raise RuntimeError("; ".join(done["errors"]))

    def _open_remote_file(self, entry: RemoteEntry):
        """打开远端文件 —— 流式下载到本地临时文件，带进度，自动缓存。

        与 VS Code 一致的关键点：
        - 不把整文件塞内存（之前 read_file 是 bytes → 信号 → UI 写盘，5MB 硬上限）
        - 本地缓存命中（size + mtime 都匹配）直接打开，秒开
        - 缓存未命中时显示 X.X MB / Y.Y MB 进度文案
        """
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

        # 缓存命中判定：本地存在 + 文件大小 + mtime 都和 entry 匹配 → 直接复用
        try:
            if (os.path.isfile(local_path)
                    and entry.size
                    and os.path.getsize(local_path) == entry.size
                    and entry.mtime
                    and abs(os.path.getmtime(local_path) - float(entry.mtime)) < 1.0):
                self._open_temp_map[local_path] = (host_alias, entry.path)
                self._open_session_map[local_path] = sess
                self._file_ready.emit(host_alias, entry.path, local_path)
                return
        except OSError:
            pass  # stat 失败就当未命中

        remote_path = entry.path
        size_hint = entry.size or 0
        # 立刻给用户视觉反馈，免得点完看不出动静
        self._subtitle_label.setText(
            t("remote.downloading", name=entry.name, done=self._fmt_size(0),
              total=self._fmt_size(size_hint)) if size_hint
            else t("remote.downloading_unknown", name=entry.name)
        )
        self._last_progress_emit_ts = 0.0

        def progress_cb(done, total):
            # paramiko 在 worker 线程调用 —— 节流后通过信号回 UI 线程
            now = time.monotonic()
            # 350ms 节流（~3Hz）：肉眼看着仍流畅，但 setText 不会高频触发
            # 任何 layout 重算，避免给用户"窗口一直在抖"的错觉。
            if now - self._last_progress_emit_ts < 0.35 and done < total:
                return
            self._last_progress_emit_ts = now
            self._download_progress.emit(remote_path, done, total)

        fut = sess.submit(sess.download_with_progress, remote_path, local_path, progress_cb)

        def on_done(f):
            try:
                attr = f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
                # 还原 subtitle
                self._download_progress.emit(remote_path, -1, -1)
                return
            # 把本地 mtime 同步成远端的 mtime —— 下次再开同一文件可以走缓存秒开
            try:
                if attr is not None and attr.st_mtime:
                    os.utime(local_path, (float(attr.st_mtime), float(attr.st_mtime)))
            except OSError:
                pass
            self._open_session_map[local_path] = sess
            self._file_ready.emit(host_alias, remote_path, local_path)
        fut.add_done_callback(on_done)

    @staticmethod
    def _fmt_size(n: int) -> str:
        if n is None or n < 0:
            return "?"
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        if n < 1024 * 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB"
        return f"{n / (1024 * 1024 * 1024):.2f} GB"

    def _on_download_progress(self, remote_path: str, done: int, total: int):
        """节流后的下载进度更新 —— 写到 subtitle 标签上"""
        if done < 0:
            # 出错信号：还原 subtitle
            sess = self._session
            self._subtitle_label.setText(sess.host_config.alias if sess else "")
            return
        name = posixpath.basename(remote_path)
        if total > 0:
            self._subtitle_label.setText(
                t("remote.downloading", name=name,
                  done=self._fmt_size(done), total=self._fmt_size(total))
            )
        else:
            self._subtitle_label.setText(
                t("remote.downloading_unknown", name=name)
            )

    def _on_file_ready(self, host_alias: str, remote_path: str, local_path: str):
        """流式下载完成（或缓存命中）→ 通知主窗口在编辑器/预览里打开"""
        # 还原 subtitle 显示
        sess = self._session
        if sess is not None:
            self._subtitle_label.setText(sess.host_config.alias)
        self._open_temp_map[local_path] = (host_alias, remote_path)
        # 用发起下载时的 session，而不是当前 self._session（期间可能已切换/断开）
        sess_for_open = self._open_session_map.pop(local_path, self._session)
        self.file_open_requested.emit(host_alias, remote_path, local_path, sess_for_open)

    def _on_file_downloaded(self, host_alias: str, remote_path: str,
                              local_path: str, data: bytes):
        try:
            with open(local_path, "wb") as fh:
                fh.write(data)
        except Exception as e:
            self._toast_error(str(e))
            return
        self._open_temp_map[local_path] = (host_alias, remote_path)
        # 用发起下载时的 session，而不是当前 self._session（期间可能已切换/断开）
        sess_for_open = self._open_session_map.pop(local_path, self._session)
        self.file_open_requested.emit(host_alias, remote_path, local_path, sess_for_open)

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
        """新建文件：用不冲突的默认名建好，刷新后直接进入原地重命名（不弹窗）"""
        sess = self._session
        if sess is None:
            return
        name = self._unique_new_name(parent_item, "untitled", ".txt")
        path = posixpath.join(parent_path, name)
        self._pending_edit_path = path
        fut = sess.submit(sess.write_file, path, b"")
        self._refresh_after(fut, parent_item, parent_path)
        self._clear_pending_on_error(fut, path)

    def _new_folder_at(self, parent_path: str, parent_item: Optional[QTreeWidgetItem]):
        """新建文件夹：用不冲突的默认名建好，刷新后直接进入原地重命名（不弹窗）"""
        sess = self._session
        if sess is None:
            return
        name = self._unique_new_name(parent_item, t("explorer.default_folder_name"), "")
        path = posixpath.join(parent_path, name)
        self._pending_edit_path = path
        fut = sess.submit(sess.mkdir, path)
        self._refresh_after(fut, parent_item, parent_path)
        self._clear_pending_on_error(fut, path)

    def _unique_new_name(self, parent_item: Optional[QTreeWidgetItem],
                         base: str, ext: str) -> str:
        """基于树里当前显示的同级条目生成一个不冲突的名字：base+ext，已存在则
        base2/base3…+ext。parent_item 为 None 时看顶层条目。"""
        existing: set[str] = set()
        if parent_item is None:
            items = [self._tree.topLevelItem(i)
                     for i in range(self._tree.topLevelItemCount())]
        else:
            items = [parent_item.child(i) for i in range(parent_item.childCount())]
        for it in items:
            e: Optional[RemoteEntry] = it.data(0, _ROLE_ENTRY)
            if e is not None:
                existing.add(e.name)
        if base + ext not in existing:
            return base + ext
        i = 2
        while f"{base}{i}{ext}" in existing:
            i += 1
        return f"{base}{i}{ext}"

    def _clear_pending_on_error(self, fut, path: str):
        """新建失败时清掉待编辑标记，避免后续无关刷新误触发重命名。"""
        def on_done(f):
            try:
                f.result()
            except Exception:
                if self._pending_edit_path == path:
                    self._pending_edit_path = None
        fut.add_done_callback(on_done)

    def _maybe_start_pending_edit(self):
        """populate（_apply_top_level / _apply_children）之后调用：若刚新建的条目
        已经进树，则选中并打开原地重命名编辑框。"""
        path = self._pending_edit_path
        if not path:
            return
        item = self._find_item_by_path(path)
        if item is None:
            return  # 还没进树（或新建失败）→ 等下一次 populate 或保持不动
        self._pending_edit_path = None

        def go():
            try:
                # 确保祖先都展开，条目才可见、可编辑
                parent = item.parent()
                while parent is not None:
                    self._tree.expandItem(parent)
                    parent = parent.parent()
                self._tree.setCurrentItem(item)
                self._tree.scrollToItem(item)
                self._tree.editItem(item, 0)
            except RuntimeError:
                pass

        QTimer.singleShot(0, go)

    def _new_file_under(self, parent_entry: RemoteEntry, parent_item: QTreeWidgetItem):
        self._new_file_at(parent_entry.path, parent_item)

    def _new_folder_under(self, parent_entry: RemoteEntry, parent_item: QTreeWidgetItem):
        self._new_folder_at(parent_entry.path, parent_item)

    def _rename_entry(self, entry: RemoteEntry, item: QTreeWidgetItem):
        """从右键菜单触发：在文件树中原地编辑（不弹窗）"""
        if item is None:
            return
        self._tree.setCurrentItem(item)
        self._tree.scrollToItem(item)
        self._tree.editItem(item, 0)

    def _do_inline_rename(self, entry: RemoteEntry, item: QTreeWidgetItem, new_name: str):
        """delegate 提交时调用：校验新名字并异步走 SFTP rename。"""
        new_name = (new_name or "").strip()
        if not new_name or new_name == entry.name:
            return
        # 不允许通过重命名跨目录移动
        if "/" in new_name:
            self._error_signal.emit(t("remote.rename"))
            return
        parent_path = posixpath.dirname(entry.path.rstrip("/")) or "/"
        new_path = posixpath.join(parent_path, new_name)
        sess = self._session
        if sess is None:
            return
        fut = sess.submit(sess.rename, entry.path, new_path)
        self._refresh_after(fut, item.parent(), parent_path)

    def _selection_entries_including(self, anchor_entry: RemoteEntry,
                                       anchor_item: QTreeWidgetItem) -> list[tuple]:
        """右键菜单使用：若 anchor 在当前选中里，返回 [(entry, item), ...] 整批；
        否则只返回 [(anchor_entry, anchor_item)]。"""
        out: list[tuple] = []
        seen = set()
        anchor_in = False
        for it in self._tree.selectedItems():
            e: RemoteEntry = it.data(0, _ROLE_ENTRY)
            if e is None or e.path in seen:
                continue
            seen.add(e.path)
            out.append((e, it))
            if e.path == anchor_entry.path:
                anchor_in = True
        if anchor_in and out:
            return out
        return [(anchor_entry, anchor_item)]

    def _delete_entry(self, entry: RemoteEntry, item: QTreeWidgetItem):
        """单项删除（兼容老调用方），转发到批量删除"""
        self._delete_entries([(entry, item)])

    def _delete_entries(self, entries: list[tuple]):
        """批量删除选中的远端文件/文件夹（一次确认 → 多个 SFTP 操作）。
        entries: [(RemoteEntry, QTreeWidgetItem), ...]
        """
        if not entries:
            return
        sess = self._session
        if sess is None:
            return

        if len(entries) == 1:
            msg = t("remote.confirm_delete_msg", name=entries[0][0].name)
        else:
            msg = t("remote.confirm_delete_many_msg", count=len(entries))

        confirm = QMessageBox.question(
            self, t("remote.confirm_delete_title"), msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # 每个项独立 submit + 在自己的回调里刷新所在父目录；
        # SSHSession 是单线程 executor，会串行执行这些 remove，不会撞车。
        for entry, item in entries:
            try:
                if entry.is_dir:
                    fut = sess.submit(sess.remove_tree, entry.path)
                else:
                    fut = sess.submit(sess.remove, entry.path)
                parent_path = posixpath.dirname(entry.path.rstrip("/")) or "/"
                self._refresh_after(fut, item.parent(), parent_path)
            except Exception as e:
                self._error_signal.emit(f"{entry.path}: {e}")

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

    def _download_entries_to_local(self, entries: list[tuple]):
        """批量下载到本地：让用户选一个目标文件夹，每个条目保留原名落进去。
        entries: [(RemoteEntry, QTreeWidgetItem), ...]（item 暂未使用，保持签名一致）
        重名时自动 (N) 后缀，避免覆盖用户已有文件。
        """
        if not entries:
            return
        sess = self._session
        if sess is None:
            return
        target_dir = QFileDialog.getExistingDirectory(
            self, t("remote.download_to_folder", count=len(entries))
        )
        if not target_dir:
            return

        def exists_fn(name: str, _target=target_dir) -> bool:
            return os.path.exists(os.path.join(_target, name))

        for ent, _item in entries:
            try:
                name = explorer_clipboard.next_free_name(ent.name, exists_fn)
                dst = os.path.join(target_dir, name)
                if ent.is_dir:
                    # 目录：递归下载（沿用 paste 走的路径）
                    fut = sess.submit(self._sftp_download_dir_blocking, sess, ent.path, dst)
                else:
                    fut = sess.submit(sess.download, ent.path, dst)

                def on_done(f, _name=name):
                    try:
                        f.result()
                    except Exception as e:
                        self._error_signal.emit(f"{_name}: {e}")
                fut.add_done_callback(on_done)
            except Exception as e:
                self._error_signal.emit(f"{ent.path}: {e}")

    @staticmethod
    def _sftp_download_dir_blocking(sess, remote_dir: str, local_dir: str):
        """递归下载远端目录到本地（在 SSH worker 线程里跑，阻塞）"""
        os.makedirs(local_dir, exist_ok=True)
        entries = sess.listdir(remote_dir)
        for e in entries:
            child_remote = e.path
            child_local = os.path.join(local_dir, e.name)
            if e.is_dir:
                RemoteExplorerPanel._sftp_download_dir_blocking(sess, child_remote, child_local)
            else:
                sess.download(child_remote, child_local)

    def _open_terminal_here(self, entry: RemoteEntry):
        self._open_terminal_at_path(entry.path)

    def _open_terminal_at_path(self, path: str):
        if self._session is None:
            return
        self.open_terminal_at.emit(self._session.host_config, path)

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
            try:
                if sip.isdeleted(progress):
                    timer.stop()
                    loop.quit()
                    return
                progress.setValue(done_counter["n"])
                if done_counter["n"] >= total or progress.wasCanceled():
                    timer.stop()
                    loop.quit()
            except RuntimeError:
                # progress 或 panel 在事件循环里被销毁
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        # panel 还活着才继续做后续 UI 更新
        if sip.isdeleted(self):
            return
        try:
            if not sip.isdeleted(progress):
                progress.setValue(total)
        except RuntimeError:
            pass

        if done_counter["errors"]:
            try:
                QMessageBox.warning(
                    self, t("remote.op_failed_title"),
                    "\n".join(done_counter["errors"]),
                )
            except RuntimeError:
                pass
        # 刷新目标目录视图（panel 已被销毁时跳过）
        try:
            if target_item is not None and not sip.isdeleted(target_item):
                entry: RemoteEntry = target_item.data(0, _ROLE_ENTRY)
                if entry and entry.is_dir:
                    self._reload_subtree(target_item, target_dir)
                else:
                    self._populate_tree_root()
            else:
                self._populate_tree_root()
        except RuntimeError:
            pass

    # ---------- 内部拖拽：远端 → 远端 移动 ----------

    def _handle_internal_move(self, src_paths: list[str], target_dir: str,
                              target_item: Optional[QTreeWidgetItem]):
        """把源路径(都是同一台主机的远端路径)用 SFTP rename 移到 target_dir。
        sftp.rename 是服务器端原子操作，不需要下载+重传。
        """
        if self._session is None or not src_paths or not target_dir:
            return
        sess = self._session

        # 1) 过滤掉 no-op 和危险情况
        moves: list[tuple[str, str, str]] = []  # (src, dst, name)
        for src in src_paths:
            src = src.rstrip("/") or "/"
            name = posixpath.basename(src) or src
            parent = posixpath.dirname(src) or "/"
            # 已经在目标目录里 → 跳过
            if parent == target_dir.rstrip("/") or parent == target_dir:
                continue
            # 不能移动到自己里/自己子目录里
            target_norm = target_dir.rstrip("/") + "/"
            if target_norm == src + "/" or target_norm.startswith(src + "/"):
                QMessageBox.warning(
                    self, t("remote.op_failed_title"),
                    t("remote.move_into_self", name=name),
                )
                return
            dst = posixpath.join(target_dir, name)
            moves.append((src, dst, name))

        if not moves:
            return  # 全是 no-op

        # 2) 确认
        if len(moves) == 1:
            msg = t("remote.move_confirm_msg_one", name=moves[0][2], target=target_dir)
        else:
            msg = t("remote.move_confirm_msg_many", count=len(moves), target=target_dir)
        reply = QMessageBox.question(
            self, t("remote.move_confirm_title"), msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 3) 进度对话框 + 后台 rename
        total = len(moves)
        progress = QProgressDialog(
            t("remote.move_progress", target=target_dir), "Cancel", 0, total, self
        )
        progress.setWindowTitle(t("remote.title"))
        progress.setMinimumDuration(300)
        progress.setValue(0)

        done_counter = {"n": 0, "errors": []}
        # 记录受影响的父目录，最后一次性刷新
        affected_parents: set[str] = {target_dir}

        def make_done(src_, name_):
            def cb(f):
                try:
                    f.result()
                except Exception as e:
                    done_counter["errors"].append(f"{name_}: {e}")
                done_counter["n"] += 1
            return cb

        for src, dst, name in moves:
            affected_parents.add(posixpath.dirname(src) or "/")
            fut = sess.submit(sess.rename, src, dst)
            fut.add_done_callback(make_done(src, name))

        # 主线程轮询进度
        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(80)
        def tick():
            try:
                if sip.isdeleted(progress):
                    timer.stop()
                    loop.quit()
                    return
                progress.setValue(done_counter["n"])
                if done_counter["n"] >= total or progress.wasCanceled():
                    timer.stop()
                    loop.quit()
            except RuntimeError:
                # progress 或 panel 在事件循环里被销毁
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()

        # panel 已销毁就直接收手；移动操作本身在后台线程已完成，
        # 用户已经关掉了 panel/窗口，没必要再做 UI 更新
        if sip.isdeleted(self):
            return
        try:
            if not sip.isdeleted(progress):
                progress.setValue(total)
        except RuntimeError:
            pass

        if done_counter["errors"]:
            try:
                QMessageBox.warning(
                    self, t("remote.op_failed_title"),
                    "\n".join(done_counter["errors"]),
                )
            except RuntimeError:
                pass

        # 4) 刷新受影响的所有父目录（源们 + 目标）
        try:
            for p in affected_parents:
                sess.invalidate_cache(p)
            # 简单粗暴：整树重刷一次，省得逐个找 item
            self._populate_tree_root()
        except RuntimeError:
            pass

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
            try:
                if sip.isdeleted(progress):
                    timer.stop()
                    loop.quit()
                    return
                progress.setValue(done_counter["n"])
                if done_counter["n"] >= len(entries) or progress.wasCanceled():
                    timer.stop()
                    loop.quit()
            except RuntimeError:
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        try:
            if not sip.isdeleted(progress):
                progress.setValue(len(entries))
        except RuntimeError:
            pass

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
        # 显式刷新 → 绕过缓存
        if self._session is not None:
            self._session.invalidate_cache(path)
        self._on_item_expanded(item)
        # 强制展开
        try:
            self._tree.expandItem(item)
        except RuntimeError:
            pass

    # ---------- utils ----------

    # paramiko / socket 断连时常见的错误片段。命中其中任一 → 提示一键重连
    _DISCONNECT_HINTS = (
        "socket is closed",
        "connection lost",
        "connection reset",
        "connection aborted",
        "connection refused",
        "broken pipe",
        "eof",
        "server connection dropped",
        "transport endpoint is not connected",
        "no existing session",
        "channel closed",
    )

    def _looks_like_disconnect(self, msg: str) -> bool:
        if not msg:
            return False
        low = msg.lower()
        return any(h in low for h in self._DISCONNECT_HINTS)

    def _toast_error(self, msg: str):
        self.error_occurred.emit(msg)
        # 断线类错误 → 弹"重连/取消"对话框，而不是冷冰冰的 OK
        if (self._looks_like_disconnect(msg)
                and self._last_connected_host is not None
                and not self._reconnect_dialog_open):
            self._prompt_reconnect(msg)
            return
        QMessageBox.warning(self, t("remote.op_failed_title"), msg)

    def _prompt_reconnect(self, msg: str):
        """断线提示 + 一键重连。同一时刻只允许一个对话框存活。"""
        self._reconnect_dialog_open = True
        host = self._last_connected_host
        try:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(t("remote.disconnected_title"))
            box.setText(t("remote.disconnected_msg",
                          host=host.alias if host else "?", error=msg))
            reconnect_btn = box.addButton(
                t("remote.reconnect"), QMessageBox.ButtonRole.AcceptRole,
            )
            cancel_btn = box.addButton(
                t("paste.btn_cancel"), QMessageBox.ButtonRole.RejectRole,
            )
            box.setDefaultButton(reconnect_btn)
            box.exec()
            clicked = box.clickedButton()
        finally:
            self._reconnect_dialog_open = False
        if clicked is reconnect_btn and host is not None:
            self._reconnect_to(host)

    def _reconnect_to(self, host: HostConfig):
        """复用 _connect_to，但断线前若停在某个子目录则尝试还原回去"""
        saved_cwd = self._last_connected_cwd
        self._connect_to(host)
        # _connect_to 内部会在 _on_session_connected 跳到 home()；
        # 这里挂一个 one-shot：等下次 connected 时把目录还原回旧位置。
        if not saved_cwd:
            return
        sess = self._session
        if sess is None:
            return
        def restore_once():
            try:
                sess.connected.disconnect(restore_once)
            except Exception:
                pass
            if sess is self._session and saved_cwd:
                self._current_path = saved_cwd
                self._path_edit.setText(saved_cwd)
                self._populate_tree_root()
        sess.connected.connect(restore_once)
