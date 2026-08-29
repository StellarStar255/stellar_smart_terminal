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
import threading
import time
from collections import deque
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QTreeWidget, QTreeWidgetItem, QMenu,
    QInputDialog, QMessageBox, QStackedWidget, QFileDialog, QLineEdit,
    QApplication, QSizePolicy, QProgressDialog, QStyledItemDelegate,
    QAbstractItemView, QDialog, QComboBox, QCheckBox,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QMimeData, QUrl, QSize
from PyQt6.QtGui import (QAction, QActionGroup, QCursor, QDrag, QShortcut,
                         QKeySequence, QDesktopServices)
from PyQt6 import sip  # 用于检查 C++ 对象是否已被删除

from i18n import t
import explorer_clipboard
import explorer_common
import remote_bookmarks
from ssh_session import (
    HostConfig, RemoteEntry, SSHSession, parse_ssh_config, append_ssh_config_host,
    rename_ssh_config_host, remove_ssh_config_host, update_ssh_config_host,
    looks_like_password_prompt,
)
from git_widget import _make_git_tool_icon  # 复用统一风格的矢量线条图标
from utils import parse_search_tokens, name_matches_tokens
import app_config
from app_logging import get_logger

logger = get_logger(__name__)


# 子项的 UserRole 数据键
_ROLE_ENTRY = Qt.ItemDataRole.UserRole
_ROLE_LOADED = Qt.ItemDataRole.UserRole + 1
# 目录展开请求的递增 generation：响应回来时 gen 失配 → 该节点其间被
# 刷新/重新请求过，旧结果直接丢弃（防竞态串台）
_ROLE_REQ_GEN = Qt.ItemDataRole.UserRole + 2

# 传输进度条按字节推进时的刻度（千分比）：总字节已知时用它做 QProgressDialog
# 的最大值，条子才会随字节平滑推进；否则刻度只能是「完成的任务数」，
# 单任务传输（整目录 tar 流 / 单个大文件）会全程停在 0 直到结束才跳满。
_BYTE_BAR_SCALE = 1000


class _TransferRateTracker:
    """传输测速：滑动窗口（最近 ~2s 的 (时刻, 累计字节) 采样）算瞬时速率，
    用全程平均速率估算剩余时间。按 key（当前文件的远端路径等）自动重置，
    多文件批量传输时显示的总是"当前这个文件"的速率。"""

    WINDOW_SECS = 2.0

    def __init__(self):
        self._samples: deque = deque()        # (monotonic_ts, bytes_done)
        self._key = None
        self._start: Optional[tuple] = None   # 首个采样 (ts, bytes)

    def reset(self, key=None):
        self._samples.clear()
        self._key = key
        self._start = None

    def update(self, key, bytes_done: int):
        if key != self._key:
            self.reset(key)
        now = time.monotonic()
        if self._start is None:
            self._start = (now, bytes_done)
        self._samples.append((now, bytes_done))
        cutoff = now - self.WINDOW_SECS
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def rate(self) -> float:
        """瞬时速率（bytes/s）；样本不足时返回 0（调用方不显示速率）。"""
        if len(self._samples) < 2:
            return 0.0
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        if t1 <= t0:
            return 0.0
        return max(0.0, (b1 - b0) / (t1 - t0))

    def eta_secs(self, bytes_done: int, bytes_total: int) -> Optional[float]:
        """按全程平均速率估算剩余秒数；速率未知 / 已完成返回 None。"""
        if self._start is None or bytes_total <= 0 or bytes_done >= bytes_total:
            return None
        t0, b0 = self._start
        elapsed = time.monotonic() - t0
        if elapsed <= 0 or bytes_done <= b0:
            return None
        avg = (bytes_done - b0) / elapsed
        if avg <= 0:
            return None
        return (bytes_total - bytes_done) / avg


class _AddHostDialog(QDialog):
    """SSH 主机文本输入对话框 —— 暗色主题，替代原生 QInputDialog。

    复用项目里其它对话框的配色（#1e1e2e 背景 / #667eea 强调色 / #3d3d5c 边框）。
    默认用于「添加主机」；传入 title/hint/placeholder/initial/ok_label 可复用为
    「重命名」等场景。返回值通过 `value()` 取。
    """

    def __init__(self, parent=None, *, title=None, hint=None,
                 placeholder="deploy@10.0.0.5:22", initial="", ok_label=None,
                 with_alias=False, initial_alias=""):
        super().__init__(parent)
        self._with_alias = with_alias
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

        # 可选别名输入（仅「添加主机」用）：留空则后端按主机名生成
        self._alias_edit = None
        if with_alias:
            alias_lbl = QLabel(t("remote.add_host_alias_label"))
            alias_lbl.setObjectName("hint")
            layout.addWidget(alias_lbl)
            self._alias_edit = QLineEdit()
            self._alias_edit.setPlaceholderText(
                t("remote.add_host_alias_placeholder"))
            self._alias_edit.setClearButtonEnabled(True)
            if initial_alias:
                self._alias_edit.setText(initial_alias)
            self._alias_edit.returnPressed.connect(
                lambda: self.accept() if self._edit.text().strip() else None)
            layout.addWidget(self._alias_edit)

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

    def alias(self) -> str:
        return self._alias_edit.text().strip() if self._alias_edit else ""


class _MfaLoginDialog(QDialog):
    """MFA / 动态码登录对话框（配色沿用 _AddHostDialog）。

    两种用法：
    - 完整模式（reauth=False）：连接前先把动态码（以及可选的登录密码）收齐，
      连上后这条 SFTP 主连接一直复用 —— 列目录、上传下载都不再要码。还能选
      主连接的空闲保持时长，以及要不要顺带开一个 SSH 终端标签（终端是另一条
      连接，会再要一次码，默认不开）。
    - 追问模式（reauth=True）：连接过程中服务器又问了一步（换了新码 / 主连接
      断了要重认证），把服务器的原始提示照原样显示，只收这一个答案。

    取值：code() / password() / keep_secs() / open_terminal()。
    """

    # (秒, i18n key)；0 = 不自动断开
    KEEP_CHOICES = (
        (3600, "remote.mfa_keep_1h"),
        (4 * 3600, "remote.mfa_keep_4h"),
        (8 * 3600, "remote.mfa_keep_8h"),
        (24 * 3600, "remote.mfa_keep_24h"),
        (0, "remote.mfa_keep_never"),
    )
    DEFAULT_KEEP_SECS = 8 * 3600

    def __init__(self, parent=None, *, alias: str, reauth: bool = False,
                 prompt: Optional[str] = None, echo: bool = True,
                 keep_secs: Optional[int] = None):
        super().__init__(parent)
        self._reauth = reauth
        self._keep_combo = None
        self._password_edit = None
        self._terminal_check = None
        title = (t("remote.mfa_reauth_title", host=alias) if reauth
                 else t("remote.mfa_title", host=alias))
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setStyleSheet("""
            QDialog { background-color: #1e1e2e; }
            QLabel#title { color: #eaeaea; font-size: 15px; font-weight: 600; }
            QLabel#hint { color: #8a8aa0; font-size: 12px; }
            QLabel#field { color: #b8b8cc; font-size: 12px; }
            QLineEdit {
                background-color: #2d2d44; color: #eaeaea;
                border: 1px solid #3d3d5c; border-radius: 6px;
                padding: 8px 10px; font-size: 13px;
                selection-background-color: #667eea;
            }
            QLineEdit:focus { border: 1px solid #667eea; }
            QComboBox {
                background-color: #2d2d44; color: #eaeaea;
                border: 1px solid #3d3d5c; border-radius: 6px;
                padding: 6px 10px; font-size: 13px;
            }
            QComboBox QAbstractItemView {
                background-color: #2d2d44; color: #eaeaea;
                selection-background-color: #667eea;
                border: 1px solid #3d3d5c;
            }
            QCheckBox { color: #b8b8cc; font-size: 12px; spacing: 6px; }
            QPushButton {
                background-color: #2d2d44; color: #eaeaea;
                border: 1px solid #3d3d5c; border-radius: 6px;
                padding: 7px 18px; font-size: 13px;
            }
            QPushButton:hover { border: 1px solid #6a5d8a; }
            QPushButton#ok {
                background-color: #667eea; border: 1px solid #667eea;
                color: white; font-weight: 600;
            }
            QPushButton#ok:hover {
                background-color: #764ba2; border: 1px solid #764ba2;
            }
            QPushButton#ok:disabled {
                background-color: #3a3a52; border: 1px solid #3a3a52; color: #777;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("title")
        layout.addWidget(title_lbl)

        hint_lbl = QLabel(t("remote.mfa_reauth_hint") if reauth
                          else t("remote.mfa_hint"))
        hint_lbl.setObjectName("hint")
        hint_lbl.setWordWrap(True)
        layout.addWidget(hint_lbl)

        code_lbl = QLabel(prompt if (reauth and prompt) else t("remote.mfa_code_label"))
        code_lbl.setObjectName("field")
        code_lbl.setWordWrap(True)
        layout.addWidget(code_lbl)

        self._code_edit = QLineEdit()
        self._code_edit.setPlaceholderText(t("remote.mfa_code_placeholder"))
        # 追问模式按服务器的 echo 标志决定明/暗文；完整模式的动态码明文可见，
        # 方便用户核对自己有没有抄错位
        if reauth and not echo:
            self._code_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._code_edit)

        if not reauth:
            pw_lbl = QLabel(t("remote.mfa_password_label"))
            pw_lbl.setObjectName("field")
            pw_lbl.setWordWrap(True)
            layout.addWidget(pw_lbl)
            self._password_edit = QLineEdit()
            self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._password_edit.setPlaceholderText(
                t("remote.mfa_password_placeholder"))
            layout.addWidget(self._password_edit)

            keep_lbl = QLabel(t("remote.mfa_keep_label"))
            keep_lbl.setObjectName("field")
            layout.addWidget(keep_lbl)
            self._keep_combo = QComboBox()
            want = self.DEFAULT_KEEP_SECS if keep_secs is None else keep_secs
            for i, (secs, key) in enumerate(self.KEEP_CHOICES):
                self._keep_combo.addItem(t(key), secs)
                if secs == want:
                    self._keep_combo.setCurrentIndex(i)
            layout.addWidget(self._keep_combo)

            self._terminal_check = QCheckBox(t("remote.mfa_open_terminal"))
            self._terminal_check.setChecked(False)
            layout.addWidget(self._terminal_check)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        cancel_btn = QPushButton(t("remote.add_host_cancel"))
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self._ok_btn = QPushButton(t("remote.mfa_ok"))
        self._ok_btn.setObjectName("ok")
        self._ok_btn.setDefault(True)
        self._ok_btn.setEnabled(False)
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

        self._code_edit.textChanged.connect(self._sync_ok_state)
        self._code_edit.returnPressed.connect(self._submit_if_ready)
        if self._password_edit is not None:
            self._password_edit.textChanged.connect(self._sync_ok_state)
            self._password_edit.returnPressed.connect(self._submit_if_ready)
        self._code_edit.setFocus()

    def _sync_ok_state(self, *_):
        # 有些堡垒机只问密码不问码，反之亦然 —— 任一格填了就允许提交
        self._ok_btn.setEnabled(bool(self.code() or self.password()))

    def _submit_if_ready(self):
        if self._ok_btn.isEnabled():
            self.accept()

    def code(self) -> str:
        return self._code_edit.text().strip()

    def password(self) -> str:
        return self._password_edit.text() if self._password_edit else ""

    def keep_secs(self) -> int:
        if self._keep_combo is None:
            return self.DEFAULT_KEEP_SECS
        val = self._keep_combo.currentData()
        return int(val) if val is not None else self.DEFAULT_KEEP_SECS

    def open_terminal(self) -> bool:
        return bool(self._terminal_check and self._terminal_check.isChecked())


class _HostAliasDelegate(QStyledItemDelegate):
    """主机列表原地重命名：编辑框只显示/编辑「别名」而非整条 "🖥 alias  target"。

    提交时不写回 item 文本（显示是带图标和地址的组合），而是交给 panel 改写
    ~/.ssh/config（或内存主机）并刷新列表。
    """

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        # 深色主题下必须显式给编辑框配色，否则默认是深底深字，回车后完全看不清。
        editor.setStyleSheet("""
            QLineEdit {
                background-color: #2d2d44;
                color: #ffffff;
                border: 1px solid #667eea;
                border-radius: 3px;
                padding: 1px 4px;
                selection-background-color: #667eea;
                selection-color: #ffffff;
            }
        """)
        return editor

    def setEditorData(self, editor, index):
        host = index.data(_ROLE_ENTRY)
        if isinstance(editor, QLineEdit) and host is not None:
            editor.setText(host.alias)
            # 全选，方便直接输入覆盖（延后一拍，绕开 view 打开编辑器后的内部 selectAll）
            QTimer.singleShot(0, editor.selectAll)
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        host = index.data(_ROLE_ENTRY)
        if not isinstance(editor, QLineEdit) or host is None:
            super().setModelData(editor, model, index)
            return
        new_alias = editor.text().strip()
        if new_alias and new_alias != host.alias:
            panel = self._panel
            # 延后执行：重建列表会删掉正在关闭的编辑器/条目，放到下一轮事件循环更安全
            QTimer.singleShot(0, lambda: panel._apply_host_rename(host, new_alias))


class _HostListWidget(QListWidget):
    """主机列表：手动排序模式下支持内部拖拽改顺序；Enter/F2 或 Finder 式单击原地
    重命名别名；双击保留给「连接」。

    用 dropEvent 覆写在拖放完成后发 rows_reordered，比连 model().rowsMoved
    可靠——QListWidget 的 InternalMove 在不少 Qt 版本里是「删除+插入」实现，
    根本不发 rowsMoved。
    """
    rows_reordered = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # 关掉自动编辑触发：否则条目一旦可编辑，双击/点已选中项会「自动进入编辑」，
        # 和「双击=连接」冲突。改为完全由我们显式控制（Enter/F2 + 延迟单击）。
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Finder 风格：单击已选中的条目 → 延迟进入原地重命名；双击则取消、走连接。
        self._pending_rename_item = None
        self._rename_timer = QTimer(self)
        self._rename_timer.setSingleShot(True)
        self._rename_timer.timeout.connect(self._fire_pending_rename)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.rows_reordered.emit()

    # ----- 原地重命名交互 -----

    def _cancel_pending_rename(self):
        self._rename_timer.stop()
        self._pending_rename_item = None

    def _fire_pending_rename(self):
        item = self._pending_rename_item
        self._pending_rename_item = None
        if item is None:
            return
        try:
            if sip.isdeleted(self) or sip.isdeleted(item):
                return
        except Exception:
            logger.debug("_fire_pending_rename: suppressed exception", exc_info=True)
        # 仍是唯一选中项才进入编辑（延迟期间用户可能已切走 / 双击已连接）
        if self.row(item) >= 0 and item.isSelected() and len(self.selectedItems()) == 1:
            self.editItem(item)

    def keyPressEvent(self, event):
        # 选中单台主机时，Enter / F2 → 原地编辑别名（配合 _HostAliasDelegate 只改 alias）。
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2):
            items = self.selectedItems()
            if len(items) == 1 and items[0].data(_ROLE_ENTRY) is not None:
                item = items[0]
                self._cancel_pending_rename()
                # 延迟到下一个事件循环再开编辑器：在 Linux 上，keyPressEvent 内同步开的
                # 编辑器会被同一次回车事件继续派发/键释放立刻关掉（表现为回车没反应）。
                QTimer.singleShot(0, lambda it=item: self._safe_edit(it))
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # Finder 式：左键无修饰点中「本次点击前已是唯一选中项」的主机 → 等双击窗口
        # 过后再进入重命名。这样第一次点只选中，再点（单击）才编辑；双击则由
        # mouseDoubleClickEvent 取消，走「连接」。
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            item = self.itemAt(event.position().toPoint())
            if item is not None and item.data(_ROLE_ENTRY) is not None:
                if item.isSelected() and len(self.selectedItems()) == 1:
                    self._pending_rename_item = item
                    self._rename_timer.start(QApplication.doubleClickInterval() + 80)
                else:
                    self._cancel_pending_rename()
            else:
                self._cancel_pending_rename()
        else:
            self._cancel_pending_rename()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # 双击 → 取消延迟重命名，让 itemDoubleClicked 走「连接」。
        self._cancel_pending_rename()
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        # 按住左键拖动（可能是拖拽改序）→ 取消延迟重命名。
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._pending_rename_item is not None):
            self._cancel_pending_rename()
        super().mouseMoveEvent(event)

    def _safe_edit(self, item):
        try:
            if sip.isdeleted(self) or sip.isdeleted(item):
                return
        except Exception:
            logger.debug("_safe_edit: suppressed exception", exc_info=True)
        # 条目仍存在且仍是当前选中项才编辑
        if self.row(item) >= 0:
            self.editItem(item)


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
            logger.debug("_fire_pending_rename: suppressed exception", exc_info=True)
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

    # 隐藏条目（含 emoji 图标 + 文字）整体降到此透明度，和本地 Explorer 的
    # 隐藏图标透明度保持一致
    HIDDEN_OPACITY = 0.45

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel

    def paint(self, painter, option, index):
        # 隐藏文件/文件夹（名字以点开头）整行变淡：emoji 是彩色字形，
        # 无法用前景色染淡，只能靠降低绘制透明度让图标和文字一起变浅。
        item = self._panel._tree.itemFromIndex(index) if index.isValid() else None
        entry: Optional[RemoteEntry] = (
            item.data(0, _ROLE_ENTRY) if item is not None else None
        )
        if entry is not None and entry.name.startswith('.'):
            painter.save()
            painter.setOpacity(self.HIDDEN_OPACITY)
            super().paint(painter, option, index)
            painter.restore()
            return
        super().paint(painter, option, index)

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
    # 在新独立窗口中连接该主机（右键菜单触发）
    # 参数: HostConfig
    open_in_new_window = pyqtSignal(object)
    # —— 内部信号：用于把 SSH 工作线程的结果安全派发回 UI 线程
    # （直接 QTimer.singleShot 在没有事件循环的工作线程里不会触发）
    _top_level_ready = pyqtSignal(list)
    _subtree_ready = pyqtSignal(object, int, list)    # (parent_item, req_gen, entries)
    _subtree_failed = pyqtSignal(object, int, str)    # (parent_item, req_gen, error_msg)
    _error_signal = pyqtSignal(str)
    _file_downloaded = pyqtSignal(str, str, str, bytes)  # host_alias, remote_path, local_path, data
    _file_ready = pyqtSignal(str, str, str)  # host_alias, remote_path, local_path (流式下载完成)
    _download_progress = pyqtSignal(str, int, int)  # remote_path, bytes_done, bytes_total
    _stat_resolved = pyqtSignal(object, str)          # (entry, requested_path)
    _refresh_root_signal = pyqtSignal()
    _refresh_subtree_signal = pyqtSignal(object, str)  # (item, path)
    _auto_refresh_result = pyqtSignal(str, object)    # (path, entries or None on error)
    _search_result_signal = pyqtSignal(int, list, bool)  # (generation, items, truncated)
    # 剪贴板预下载（worker → UI）：进度变化 / 全部完成。必须用信号跨线程，
    # 不能用 QTimer.singleShot —— 后者在无事件循环的 worker 线程里不会触发。
    _clipboard_stage_tick = pyqtSignal()
    _clipboard_stage_finalize = pyqtSignal()

    # 远程递归搜索上限：命中数 / 已扫描目录数 / 最大深度。
    # SFTP 是单工作线程串行的，每个目录一次网络往返，所以上限取得保守，
    # 避免一次搜索把会话占用太久（命中/目录/深度任一触顶即停并提示已截断）。
    _SEARCH_MAX_RESULTS = 500
    _SEARCH_MAX_DIRS = 600
    _SEARCH_MAX_DEPTH = 6

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
        # 用户在「Password Required」对话框里输入的密码，按主机别名缓存在内存里，
        # 供同一主机的 SSH 终端 tab 自动回填，避免二次输入。只存内存、不落盘、
        # 断开连接即清除。
        self._cached_passwords: dict[str, str] = {}
        # MFA（一次性动态码）登录：登录框里预先收好的答案，认证回调直接拿它作答，
        # 不再在认证中途弹框——人在框里翻手机的几十秒足够撞上 SSH 认证超时。
        # 只在内存里活到本次认证结束，用过即擦，绝不落盘。
        self._pending_mfa: Optional[dict] = None
        self._mfa_lock = threading.Lock()   # 认证回调在 SSH 工作线程上读它
        # 主连接空闲保持：0 = 不自动断开；>0 时空闲超过该秒数就断开，
        # 下次操作需要重新 MFA 登录（动态码有效期只撑一次认证）
        self._mfa_keep_secs = 0
        # 连上后是否照常开 SSH 终端标签。终端是另一条连接，MFA 主机会再要一次
        # 动态码 —— 只在用户于 MFA 登录框里明确勾选时才开（普通主机不受影响）。
        self._mfa_open_terminal = True
        self._last_activity = time.monotonic()
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
        # 是否显示隐藏文件（以点开头），从配置读取，默认显示
        self._show_hidden = self._load_show_hidden()
        # 排序方式（默认按名称升序，文件夹始终置顶），从配置读取
        self._sort_key, self._sort_desc = self._load_sort()
        # 主机列表排序模式（manual/alias/host），从配置读取
        self._host_sort = self._load_host_sort()
        # 文件搜索状态：generation 用于丢弃过期/已取消的后台结果
        self._search_gen = 0

        self._setup_ui()
        self._apply_theme()
        self._reload_hosts()

        # 内部跨线程信号 → UI 线程槽（QueuedConnection 自动应用）
        self._top_level_ready.connect(self._apply_top_level)
        self._subtree_ready.connect(self._apply_children)
        self._subtree_failed.connect(self._on_subtree_failed)
        self._error_signal.connect(self._toast_error)
        self._file_downloaded.connect(self._on_file_downloaded)
        self._file_ready.connect(self._on_file_ready)
        self._download_progress.connect(self._on_download_progress)
        self._clipboard_stage_tick.connect(self._on_clipboard_stage_tick)
        self._clipboard_stage_finalize.connect(self._on_clipboard_stage_finalize)
        # 节流下载进度文案：paramiko 回调每个 chunk 都触发，UI 100ms 一次足够
        self._last_progress_emit_ts = 0.0
        # 下载测速：滑动窗口算瞬时速率 + 平均速率估剩余时间（按当前文件）
        self._dl_rate = _TransferRateTracker()
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
        # 主连接空闲看门狗（只对设了保持时长的 MFA 会话生效）：每分钟看一眼，
        # 空闲超时就主动断开，避免一条已认证的堡垒机连接无限期挂着。
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(60_000)
        self._idle_timer.timeout.connect(self._check_idle_timeout)

        self._refresh_root_signal.connect(self._populate_tree_root)
        self._refresh_subtree_signal.connect(self._reload_subtree)
        self._password_prompt_signal.connect(self._on_password_prompt)
        self._interactive_prompt_signal.connect(self._on_interactive_prompt)
        self._search_result_signal.connect(self._on_search_results)

        # 搜索输入防抖：停止输入 300ms 后才发起递归搜索
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._start_search)

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

        # 连接后切回主机列表，连接其它主机（不断开当前会话）
        self._hosts_btn = QPushButton()
        self._hosts_btn.setFixedSize(28, 28)
        self._hosts_btn.setIconSize(QSize(16, 16))
        self._hosts_btn.setToolTip(t("remote.hosts_view"))
        self._hosts_btn.clicked.connect(self._toggle_hosts_view)
        self._hosts_btn.hide()
        h_layout.addWidget(self._hosts_btn)

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

        self._hosts_list = _HostListWidget()
        # 原地重命名别名（Enter/F2）：编辑框只改 alias，提交后走 _apply_host_rename
        self._hosts_list.setItemDelegate(_HostAliasDelegate(self))
        self._hosts_list.itemDoubleClicked.connect(self._on_host_activated)
        self._hosts_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._hosts_list.customContextMenuRequested.connect(self._on_hosts_context_menu)
        # 手动排序模式下允许拖拽改顺序；拖完把新顺序持久化。
        # InternalMove 由 _populate_hosts_list 按当前模式启停。
        self._hosts_list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self._hosts_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._hosts_list.rows_reordered.connect(self._on_hosts_reordered)
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
        # 在默认的编辑右键菜单（撤销/复制/粘贴等）之上补一个「在此打开终端」
        self._path_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._path_edit.customContextMenuRequested.connect(self._on_path_edit_context_menu)
        pb_layout.addWidget(self._path_edit, 1)

        # 视图设置按钮（齿轮）：弹出菜单，含"显示隐藏文件"开关
        # 放在最右侧，避免干扰路径栏左侧的常用导航按钮
        self._settings_btn = QPushButton()
        self._settings_btn.setFixedSize(26, 26)
        self._settings_btn.setIconSize(QSize(16, 16))
        self._settings_btn.setToolTip(t("remote.settings_tooltip"))
        self._settings_btn.clicked.connect(self._show_settings_menu)
        pb_layout.addWidget(self._settings_btn)

        tp_layout.addWidget(self._path_bar)

        # 搜索框：多关键词（空格分隔）递归搜索当前目录下的远程文件/文件夹
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(t("search.placeholder"))
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        tp_layout.addWidget(self._search_edit)

        self._tree = _RemoteTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        # 原地重命名的编辑器代理（去掉 emoji 前缀，由 panel 异步走 SFTP rename）
        self._tree.setItemDelegate(_RemoteItemDelegate(self, self._tree))
        tp_layout.addWidget(self._tree, 1)

        # 搜索结果列表（扁平展示命中项；默认隐藏，搜索时替换 _tree）
        self._search_results = QListWidget()
        self._search_results.setUniformItemSizes(True)
        self._search_results.setVisible(False)
        self._search_results.itemDoubleClicked.connect(self._open_search_result)
        tp_layout.addWidget(self._search_results, 1)

        # Cmd+C / Cmd+V — 当 tree 或其子项有焦点时触发
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self._tree)
        copy_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_sc.activated.connect(lambda: self._clipboard_copy_selection(None))

        paste_sc = QShortcut(QKeySequence.StandardKey.Paste, self._tree)
        paste_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        paste_sc.activated.connect(self._paste_via_shortcut)

        self._stack.addWidget(self._tree_page)

        root.addWidget(self._stack, 1)

        # Cmd+R（macOS）/ Ctrl+R（其它平台）刷新当前视图：
        # 文件树页 → 刷新目录；主机列表页 → 重新读取主机。
        refresh_sc = QShortcut(QKeySequence("Ctrl+R"), self)
        refresh_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        refresh_sc.activated.connect(self._on_refresh_shortcut)

    def _on_refresh_shortcut(self):
        """Cmd+R：按当前所在页刷新——文件树刷新目录，主机列表重新读取主机。"""
        if self._stack.currentWidget() is self._tree_page and self._session is not None:
            self._on_refresh()
        else:
            self._reload_hosts()

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
        self._hosts_btn.setIcon(_make_git_tool_icon('list', text))
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
        self._settings_btn.setIcon(_make_git_tool_icon('gear', text))
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

        self._search_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {bg_medium}; color: {text};
                border: 1px solid {border}; border-radius: 4px;
                padding: 4px 8px; margin: 4px;
            }}
            QLineEdit:focus {{ border: 1px solid {accent}; }}
        """)

        self._search_results.setStyleSheet(f"""
            QListWidget {{
                background-color: {bg_dark}; color: {text};
                border: none; outline: none;
            }}
            QListWidget::item {{ padding: 4px 8px; }}
            QListWidget::item:hover {{ background-color: {bg_hover}; }}
            QListWidget::item:selected {{ background-color: {accent}; color: white; }}
            QScrollBar:vertical {{
                background-color: {bg_dark}; width: 10px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background-color: {border}; border-radius: 5px; min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{ background-color: {text_dim}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
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
        self._settings_btn.setToolTip(t("remote.settings_tooltip"))
        self._search_edit.setPlaceholderText(t("search.placeholder"))
        self._empty_hint.setText(t("remote.no_hosts"))
        # 重建主机列表（条目文本本身是别名+真实地址，不用国际化）
        if self._session is None:
            self._populate_hosts_list()

    # ---------- 主机列表逻辑 ----------

    def _reload_hosts(self):
        self._hosts = parse_ssh_config()
        self._populate_hosts_list()

    def _populate_hosts_list(self):
        # 仅 manual 模式开启内部拖拽；切换前先按当前模式设好拖拽策略
        manual = (self._host_sort == 'manual')
        self._hosts_list.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove if manual
            else QAbstractItemView.DragDropMode.NoDragDrop)
        self._hosts_list.clear()
        combined = list(self._hosts) + list(self._extra_hosts)
        if not combined:
            self._empty_hint.show()
        else:
            self._empty_hint.hide()
            # 一次读出 MFA 主机集合（逐台读配置太亏），🔑 标出"这台要动态码"
            mfa_hosts = self._load_mfa_hosts()
            for h in self._sorted_hosts(combined):
                target = f"{h.user + '@' if h.user else ''}{h.hostname}:{h.port}"
                icon = "🔑" if h.alias in mfa_hosts else "🖥"
                item = QListWidgetItem(f"{icon}  {h.alias}    {target}")
                item.setData(_ROLE_ENTRY, h)
                # 允许 Enter/F2 原地重命名（编辑框由 _HostAliasDelegate 只显示 alias）
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                self._hosts_list.addItem(item)

    def _on_add_host_clicked(self):
        dlg = _AddHostDialog(self, with_alias=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = dlg.value()
        if not text:
            return
        alias = dlg.alias()
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
        # alias 留空时后端按主机名生成（并自动避让重名）
        try:
            append_ssh_config_host(hostname=host, user=user, port=port,
                                   alias=alias or None)
            self._reload_hosts()  # 从 config 重新读取，新主机会出现在列表里
        except Exception as e:
            # 写盘失败（权限等）→ 退回到仅本会话内存，避免直接丢失
            QMessageBox.warning(
                self, t("remote.add_host_title"),
                t("remote.add_host_save_failed").format(error=e),
            )
            cfg = HostConfig(alias=(alias or text), hostname=host,
                             user=user, port=port)
            self._extra_hosts.append(cfg)
            self._populate_hosts_list()

    def _on_host_activated(self, item: QListWidgetItem):
        host: HostConfig = item.data(_ROLE_ENTRY)
        if not host:
            return
        # 已经连着这台、只是从主机列表切回来 → 直接回到文件树，不必重连
        if (self._session is not None and self._session.is_connected()
                and self._session.host_config.alias == host.alias):
            self._show_tree_view()
            return
        self._connect_to(host)

    def _toggle_hosts_view(self):
        """连接状态下，在「文件树」与「主机列表」间切换。

        切到主机列表后可双击/右键连接其它主机（_connect_to 会先断开当前会话再连），
        从而在不关闭已打开的 SSH 终端 tab 的情况下开启新的远程连接。
        """
        if self._session is None:
            return
        if self._stack.currentWidget() is self._tree_page:
            self._stack.setCurrentWidget(self._hosts_page)
            self._reload_btn.show()   # 主机列表态：可刷新 / 新增主机
            self._add_btn.show()
        else:
            self._show_tree_view()

    def _show_tree_view(self):
        """回到当前会话的文件树页，并恢复对应的头部按钮可见性。"""
        self._stack.setCurrentWidget(self._tree_page)
        self._reload_btn.hide()
        self._add_btn.hide()

    def _on_hosts_context_menu(self, pos):
        """主机列表右键菜单：连接 / 重命名 / 排序方式。

        右键空白处也能弹（仅排序子菜单），方便没有主机或想改排序时用。
        """
        item = self._hosts_list.itemAt(pos)
        host: HostConfig = item.data(_ROLE_ENTRY) if item is not None else None
        bg_medium = self.theme.get('bg_medium', '#16213e')
        text = self.theme.get('text', '#eaeaea')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')
        menu_css = f"""
            QMenu {{
                background-color: {bg_medium}; color: {text};
                border: 1px solid {border}; border-radius: 4px; padding: 4px;
            }}
            QMenu::item {{ padding: 6px 20px; border-radius: 3px; }}
            QMenu::item:selected {{ background-color: {accent}; }}
            QMenu::separator {{ height: 1px; background-color: {border}; margin: 4px 10px; }}
        """
        menu = QMenu(self)
        menu.setStyleSheet(menu_css)
        # 「添加主机」始终可用（点空白处或点主机都在，方便随时新增）
        add_act = menu.addAction(t("remote.add_host_menu"))
        add_act.triggered.connect(lambda checked=False: self._on_add_host_clicked())
        menu.addSeparator()
        if host is not None:
            connect_act = menu.addAction(t("remote.connect"))
            connect_act.triggered.connect(lambda checked=False, h=host: self._connect_to(h))
            # MFA 登录：先收动态码再连，之后所有文件操作复用这条主连接
            mfa_act = menu.addAction(t("remote.mfa_login_menu"))
            mfa_act.triggered.connect(lambda checked=False, h=host: self._mfa_login(h))
            if self._is_mfa_host(host.alias):
                forget_act = menu.addAction(t("remote.mfa_forget"))
                forget_act.triggered.connect(
                    lambda checked=False, h=host: self._set_mfa_host(h.alias, None))
            new_win_act = menu.addAction(t("remote.connect_in_new_window"))
            new_win_act.triggered.connect(lambda checked=False, h=host: self.open_in_new_window.emit(h))
            menu.addSeparator()
            edit_act = menu.addAction(t("remote.edit_host"))
            edit_act.triggered.connect(lambda checked=False, h=host: self._edit_host(h))
            rename_act = menu.addAction(t("remote.rename_host"))
            rename_act.triggered.connect(lambda checked=False, h=host: self._rename_host(h))
            delete_act = menu.addAction(t("remote.delete_host"))
            delete_act.triggered.connect(lambda checked=False, h=host: self._delete_host(h))
            menu.addSeparator()

        sort_menu = menu.addMenu(t("remote.sort_by"))
        sort_menu.setStyleSheet(menu_css)
        for mode, label_key in (('manual', 'remote.sort_manual'),
                                ('alias', 'remote.sort_alias'),
                                ('host', 'remote.sort_host')):
            act = sort_menu.addAction(t(label_key))
            act.setCheckable(True)
            act.setChecked(self._host_sort == mode)
            act.triggered.connect(lambda checked=False, m=mode: self._set_host_sort(m))
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
        self._apply_host_rename(host, dlg.value())

    def _apply_host_rename(self, host: HostConfig, new_alias: str):
        """把某主机别名改为 new_alias 并落盘/刷新。供弹窗重命名与 Enter/F2 原地
        重命名共用。别名为空或未变则忽略；重名/写盘失败弹一次提示。"""
        new_alias = (new_alias or "").strip()
        if not new_alias or new_alias == host.alias:
            return
        if self._is_memory_host(host):
            host.alias = new_alias          # 仅内存中的主机（未落 config）→ 直接改
            self._populate_hosts_list()
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
            host.alias = new_alias          # config 里没匹配到（内存态）→ 直接改
            self._populate_hosts_list()

    def _is_memory_host(self, host: HostConfig) -> bool:
        """该主机是否只存在于本会话内存（写盘失败退回的 _extra_hosts）而非 ~/.ssh/config。"""
        return any(h is host for h in self._extra_hosts)

    def _edit_host(self, host: HostConfig):
        """编辑主机：可改 [user@]host[:port] 与别名。config 里的主机就地改写
        （保留 IdentityFile / ProxyJump 等其它设置），内存主机直接改字段。"""
        target = f"{host.user + '@' if host.user else ''}{host.hostname}:{host.port}"
        dlg = _AddHostDialog(
            self,
            title=t("remote.edit_host_title"),
            hint=t("remote.edit_host_hint"),
            initial=target,
            initial_alias=host.alias,
            ok_label=t("remote.edit_host_ok"),
            with_alias=True,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = dlg.value()
        if not text:
            return

        # 解析 [user@]host[:port]
        user, port, rest = "", 22, text
        if "@" in rest:
            user, rest = rest.split("@", 1)
        if ":" in rest:
            host_s, port_s = rest.rsplit(":", 1)
            try:
                port = int(port_s)
            except ValueError:
                QMessageBox.warning(self, t("remote.edit_host_title"), "Invalid port")
                return
        else:
            host_s = rest
        host_s = host_s.strip()
        if not host_s:
            return
        user = user.strip()
        new_alias = dlg.alias() or host.alias

        if self._is_memory_host(host):
            host.alias, host.hostname, host.user, host.port = new_alias, host_s, user, port
            self._populate_hosts_list()
            return

        try:
            # 先改别名（若变了），再就地更新 HostName/User/Port
            if new_alias != host.alias:
                try:
                    rename_ssh_config_host(host.alias, new_alias)
                except ValueError:
                    QMessageBox.warning(
                        self, t("remote.edit_host_title"),
                        t("remote.rename_host_exists").format(alias=new_alias))
                    return
            update_ssh_config_host(new_alias, hostname=host_s, user=user, port=port)
            self._reload_hosts()
        except Exception as e:
            QMessageBox.warning(
                self, t("remote.edit_host_title"),
                t("remote.edit_host_failed").format(error=e))

    def _delete_host(self, host: HostConfig):
        """从列表删除主机：config 主机从 ~/.ssh/config 移除，内存主机直接摘掉。"""
        resp = QMessageBox.question(
            self, t("remote.delete_host_title"),
            t("remote.delete_host_confirm").format(alias=host.alias),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return

        if self._is_memory_host(host):
            self._extra_hosts = [h for h in self._extra_hosts if h is not host]
            self._populate_hosts_list()
            return

        try:
            removed = remove_ssh_config_host(host.alias)
        except Exception as e:
            QMessageBox.warning(
                self, t("remote.delete_host_title"),
                t("remote.delete_host_failed").format(error=e))
            return
        if removed:
            self._reload_hosts()
        else:
            # config 里没找到（可能是内存态）→ 兜底也从内存列表摘掉
            self._extra_hosts = [h for h in self._extra_hosts if h is not host]
            self._populate_hosts_list()

    # ---------- 连接 ----------

    def _connect_to(self, host: HostConfig, *, mfa_answers: Optional[dict] = None):
        """连接主机。

        该主机被记过「需要动态码」而调用方又没带答案时，先弹 MFA 登录框把码收齐
        再连（_mfa_login 会带着 mfa_answers 回到这里），这样认证回调有现成答案可
        用，不必在认证中途弹框干等用户翻手机。
        """
        if mfa_answers is None and self._is_mfa_host(host.alias):
            self._mfa_login(host)
            return
        if self._session is not None:
            self._disconnect()   # 注意：会清掉 _pending_mfa，所以答案在它之后再放

        self._pending_mfa = mfa_answers
        if mfa_answers is None:
            # 普通连接：恢复默认行为（连上即开 SSH 终端标签、不做空闲断开），
            # 免得上一次 MFA 登录的选择粘到别的主机上
            self._mfa_open_terminal = True
            self._mfa_keep_secs = 0
        self._subtitle_label.setText(t("remote.connecting", host=host.alias))
        self._add_btn.hide()
        self._reload_btn.hide()

        sess = SSHSession(host, parent=self)
        sess.connected.connect(lambda: self._on_session_connected(sess))
        sess.connect_failed.connect(lambda msg: self._on_session_connect_failed(sess, msg))
        sess.host_key_check_degraded.connect(self._on_host_key_degraded)
        sess.connect_async(
            password_provider=self._prompt_password,
            passphrase_provider=self._prompt_password,
            interactive_provider=self._prompt_interactive,
        )
        self._session = sess

    # ---------- MFA / 动态码登录 ----------

    def _mfa_login(self, host: HostConfig):
        """弹 MFA 登录框收动态码（+可选密码），然后带着答案去连。

        取消 → 什么都不做（不留半连接状态）。
        """
        dlg = _MfaLoginDialog(
            self, alias=host.alias,
            keep_secs=self._get_mfa_keep(host.alias),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        code = dlg.code()
        password = dlg.password()
        self._mfa_keep_secs = dlg.keep_secs()
        self._mfa_open_terminal = dlg.open_terminal()
        # 密码可以缓存（SSH 终端标签自动回填、重连自动作答）；动态码是一次性的，
        # 只放在 _pending_mfa 里，用掉即擦，永不进 _cached_passwords、永不落盘。
        if password:
            self._cached_passwords[host.alias] = password
        answers = {
            'alias': host.alias,
            'code': code or None,
            'code_used': False,
            'password': password or None,
            'password_used': False,
        }
        # 记住这台主机走 MFA（含本次选的保持时长），下次连接直接弹这个框
        self._set_mfa_host(host.alias, self._mfa_keep_secs)
        self._touch_activity()
        self._connect_to(host, mfa_answers=answers)

    def _take_pre_answer(self, alias: str, prompt: str) -> Optional[str]:
        """从 MFA 登录框预收的答案里取一条来回答服务器的提示（没有则 None）。

        规则：密码类提示吃密码、其它（动态码/验证码）吃动态码；两者都只自动作答
        一次——服务器再问同一类，说明刚才那个被拒了或还要第二个码，此时必须回到
        弹框问用户，否则会拿同一个错答案把 MaxAuthTries 打满。
        """
        with self._mfa_lock:
            bundle = self._pending_mfa
            if not bundle or bundle.get('alias') != alias:
                return None
            if looks_like_password_prompt(prompt):
                if bundle.get('password') and not bundle.get('password_used'):
                    bundle['password_used'] = True
                    return bundle['password']
                return None
            if bundle.get('code') and not bundle.get('code_used'):
                bundle['code_used'] = True
                code = bundle['code']
                bundle['code'] = None      # 一次性：用完立刻从内存里抹掉
                return code
            return None

    def _clear_pending_mfa(self):
        """认证结束（成功/失败/断开）后擦掉预收答案。"""
        with self._mfa_lock:
            self._pending_mfa = None

    # ---- 「该主机需要动态码」标记（按别名持久化；只存标记与保持时长）----

    CONFIG_KEY_MFA_HOSTS = 'remote_explorer_mfa_hosts'

    def _load_mfa_hosts(self) -> dict:
        hosts = app_config.read_config().get(self.CONFIG_KEY_MFA_HOSTS)
        return dict(hosts) if isinstance(hosts, dict) else {}

    def _is_mfa_host(self, alias: str) -> bool:
        return bool(alias) and alias in self._load_mfa_hosts()

    def _get_mfa_keep(self, alias: str) -> Optional[int]:
        """该主机上次选的主连接保持时长（秒）；没记过返回 None（用默认值）。"""
        rec = self._load_mfa_hosts().get(alias)
        if isinstance(rec, dict):
            val = rec.get('keep')
            if isinstance(val, int) and val >= 0:
                return val
        return None

    def _set_mfa_host(self, alias: str, keep_secs: Optional[int]):
        """记住/清除「这台主机用 MFA 登录」。keep_secs=None 表示取消标记。"""
        if not alias:
            return

        def _apply(cfg):
            hosts = cfg.get(self.CONFIG_KEY_MFA_HOSTS)
            if not isinstance(hosts, dict):
                hosts = {}
            if keep_secs is None:
                hosts.pop(alias, None)
            else:
                hosts[alias] = {'keep': int(keep_secs)}
            cfg[self.CONFIG_KEY_MFA_HOSTS] = hosts

        app_config.update_config_with(_apply, description='remote-mfa-hosts')
        self._populate_hosts_list()   # 🔑 标记立刻反映到列表上

    # ---- 主连接空闲保持 ----

    def _touch_activity(self):
        """记一次「用户还在用这条连接」。自动刷新轮询不算（否则永远不空闲）。"""
        self._last_activity = time.monotonic()

    def _check_idle_timeout(self):
        """空闲看门狗：超过保持时长就断开主连接，提示需要重新 MFA 登录。"""
        if self._session is None or self._mfa_keep_secs <= 0:
            return
        idle = time.monotonic() - self._last_activity
        if idle < self._mfa_keep_secs:
            return
        alias = self._session.host_config.alias
        logger.info("[RemoteExplorerPanel] MFA session idle %.0fs > %ds, "
                    "closing master connection to %s",
                    idle, self._mfa_keep_secs, alias)
        self._disconnect()
        self.error_occurred.emit(t(
            "remote.mfa_idle_disconnected", host=alias,
            minutes=max(1, self._mfa_keep_secs // 60)))

    _password_prompt_signal = pyqtSignal(str)  # 在 UI 线程触发输入框

    def _prompt_password(self, label: str) -> Optional[str]:
        # paramiko 回调在后台线程；用 QInputDialog 必须切回 UI 线程。
        # 这里通过自定义事件 + 信号触发，主线程显示对话框并把结果放回 holder。
        result_holder: dict = {'done': False, 'value': None, 'label': label}
        self._pending_password_request = result_holder
        try:
            self._password_prompt_signal.emit(label)
        except Exception as e:
            logger.warning(f"[RemoteExplorerPanel] password prompt emit failed: {e}")
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
        # 缓存密码（仅密码认证、非 passphrase 提示）：label 即主机别名时才存，
        # 供同一主机的 SSH 终端 tab 一次性自动回填。
        if ok and text:
            self._cached_passwords[label] = text

    _interactive_prompt_signal = pyqtSignal(str, str, bool)  # alias, 服务器提示, echo

    def _prompt_interactive(self, alias: str, prompt: str, echo: bool) -> Optional[str]:
        # keyboard-interactive（OTP/2FA）的逐步提示；跨线程模式同 _prompt_password
        # MFA 登录框已经把答案收齐时直接作答，不再弹框——认证等待有硬超时，
        # 让用户在这一步现翻手机很容易超时失败。
        pre = self._take_pre_answer(alias, prompt)
        if pre is not None:
            return pre
        result_holder: dict = {'done': False, 'value': None}
        self._pending_interactive_request = result_holder
        try:
            self._interactive_prompt_signal.emit(alias, prompt, echo)
        except Exception as e:
            logger.warning(f"[RemoteExplorerPanel] interactive prompt emit failed: {e}")
            return None
        while not result_holder['done']:
            time.sleep(0.05)
        return result_holder['value']

    def _on_interactive_prompt(self, alias: str, prompt: str, echo: bool):
        holder = getattr(self, '_pending_interactive_request', None)
        if holder is None:
            return
        # 展示服务器原始提示（Password: / Verification code: …），echo 决定明暗文。
        # 已知走 MFA 的主机用专门的追问框（说清"要一个新码"），其余保持原样。
        if self._is_mfa_host(alias) or self._pending_mfa is not None:
            dlg = _MfaLoginDialog(self, alias=alias, reauth=True,
                                  prompt=prompt, echo=echo)
            ok = dlg.exec() == QDialog.DialogCode.Accepted
            text = dlg.code() if ok else ""
        else:
            text, ok = QInputDialog.getText(
                self, t("remote.interactive_title"),
                t("remote.interactive_prompt", host=alias, prompt=prompt),
                QLineEdit.EchoMode.Normal if echo else QLineEdit.EchoMode.Password,
            )
        holder['value'] = text if ok else None
        holder['done'] = True
        # 只有密码类回答按 alias 缓存（供 SSH 终端 tab 自动回填）；
        # OTP 验证码是一次性的，绝不缓存
        if ok and text and looks_like_password_prompt(prompt):
            self._cached_passwords[alias] = text

    def get_cached_password(self, alias: str) -> Optional[str]:
        """返回某主机此前在「Password Required」里输入过的密码（无则 None）。
        供主窗口打开 SSH 终端时自动回填，避免二次输入。"""
        return self._cached_passwords.get(alias)

    def prime_cached_password(self, alias: str, password: Optional[str]):
        """预置某主机的密码（内存）。用于「扩展远程终端到新窗口」时把原窗口
        已缓存的密码带给新窗口的 Remote 面板，避免自动连接 SFTP 时再次弹框。"""
        if alias and password:
            self._cached_passwords[alias] = password

    def _on_host_key_degraded(self, reason: str):
        """known_hosts 加载失败 → MITM 拦截降级，非模态警告横幅提示用户。"""
        self.error_occurred.emit(reason)

    def _on_session_connected(self, sess: SSHSession):
        if sess is not self._session:
            return
        self._subtitle_label.setText(sess.host_config.alias)
        self._disconnect_btn.show()
        self._hosts_btn.show()
        self._reload_btn.hide()
        self._add_btn.hide()
        self._stack.setCurrentWidget(self._tree_page)
        # 若为该主机设过默认启动目录就跳过去，否则回 home
        # （目录若已失效，_populate_tree_root 的 listdir 会通过 _error_signal 提示）
        default_dir = self._get_default_dir(sess.host_config.alias)
        self._current_path = default_dir or sess.home()
        self._path_edit.setText(self._current_path)
        self._populate_tree_root()
        self._auto_refresh_timer.start()
        # 记住最近连上的 host，供"断线后一键重连"使用
        self._last_connected_host = sess.host_config
        self._last_connected_cwd = self._current_path
        # 认证过程中真的要过一次性动态码 → 记住这台主机走 MFA，下次直接弹
        # MFA 登录框先收码（用户不必自己去菜单里选）
        otp_host = False
        try:
            otp_host = sess.used_otp_auth()
        except Exception:
            logger.debug("_on_session_connected: used_otp_auth failed",
                         exc_info=True)
        if otp_host and not self._is_mfa_host(sess.host_config.alias):
            self._set_mfa_host(sess.host_config.alias,
                               _MfaLoginDialog.DEFAULT_KEEP_SECS)
        # 动态码已经用掉，答案不必再留在内存里
        self._clear_pending_mfa()
        self._touch_activity()
        if self._mfa_keep_secs > 0:
            self._idle_timer.start()
        else:
            self._idle_timer.stop()
        # 通知主窗口：可以开一个 SSH 终端 tab 进去。MFA 登录时用户没勾"同时开
        # 终端"就不开——终端是另一条连接，会再要一次动态码。
        if self._mfa_open_terminal:
            self.host_connected.emit(sess.host_config)

    def _on_session_connect_failed(self, sess: SSHSession, msg: str):
        if sess is not self._session:
            return
        self._clear_pending_mfa()
        self._idle_timer.stop()
        QMessageBox.warning(
            self, t("remote.connect_failed_title"),
            t("remote.connect_failed_msg", host=sess.host_config.alias, error=msg),
        )
        self._subtitle_label.setText("")
        self._add_btn.show()
        self._reload_btn.show()
        self._hosts_btn.hide()
        self._session = None

    def _disconnect(self):
        # 先作废在途搜索并停掉防抖：递归搜索独占 SSH 单工作线程，
        # 否则 disconnect() 的 .result(timeout=5) 会排在搜索后面，最长卡 UI 5 秒。
        self._search_gen += 1
        self._search_timer.stop()
        self._exit_search()
        if self._session is not None:
            try:
                self._session.disconnect()
            except Exception as e:
                logger.debug(f"[RemoteExplorerPanel] disconnect failed: {e}")
            self._session = None
        self._auto_refresh_timer.stop()
        self._idle_timer.stop()
        self._auto_refresh_fingerprints.clear()
        self._auto_refresh_pending = 0
        # 断开即清空内存里的密码缓存与未用完的动态码，缩短敏感数据驻留时间
        self._cached_passwords.clear()
        self._clear_pending_mfa()
        self._stack.setCurrentWidget(self._hosts_page)
        self._tree.clear()
        self._subtitle_label.setText("")
        self._add_btn.show()
        self._reload_btn.show()
        self._hosts_btn.hide()
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
                    logger.warning(f"[RemoteExplorerPanel] async disconnect failed: {e}")
            threading.Thread(target=_bg_disconnect, name="ssh-bg-disconnect", daemon=True).start()
        super().closeEvent(event)

    # ---------- 文件树 ----------

    def _populate_tree_root(self):
        """加载当前路径的内容到顶层（不再包一层 path 节点）

        和本地 Explorer 行为一致：path 在路径栏里显示，文件/目录直接平铺在树根。
        """
        # 任何导航（上一级/主目录/路径栏/刷新/双击进目录）都退出搜索态：
        # 清掉搜索框、作废在途搜索、恢复显示文件树。
        if self._search_edit.text():
            self._search_edit.blockSignals(True)
            self._search_edit.clear()
            self._search_edit.blockSignals(False)
            self._search_gen += 1
        self._exit_search()
        self._touch_activity()   # 导航/刷新算用户活动（自动刷新轮询不算）
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

    # ---- 隐藏文件显示开关（持久化到共享配置） ----

    CONFIG_KEY_SHOW_HIDDEN = 'remote_explorer_show_hidden'

    def _load_show_hidden(self) -> bool:
        cfg = app_config.read_config()
        return bool(cfg.get(self.CONFIG_KEY_SHOW_HIDDEN, True))

    def _save_show_hidden(self):
        app_config.update_config({self.CONFIG_KEY_SHOW_HIDDEN: self._show_hidden},
                                 description='remote-explorer')

    # ---- 每台主机的默认启动目录（按别名持久化到共享配置） ----

    CONFIG_KEY_DEFAULT_DIRS = 'remote_explorer_default_dirs'

    def _load_default_dirs(self) -> dict:
        dirs = app_config.read_config().get(self.CONFIG_KEY_DEFAULT_DIRS)
        return dict(dirs) if isinstance(dirs, dict) else {}

    def _get_default_dir(self, alias: str) -> Optional[str]:
        """取某台主机记住的默认启动目录；没有则返回 None。"""
        if not alias:
            return None
        val = self._load_default_dirs().get(alias)
        return val if isinstance(val, str) and val else None

    def _set_default_dir(self, alias: str, path: Optional[str]):
        """记住 / 清除某台主机的默认启动目录（path=None 表示清除）。"""
        if not alias:
            return

        def _apply(cfg):
            dirs = cfg.get(self.CONFIG_KEY_DEFAULT_DIRS)
            if not isinstance(dirs, dict):
                dirs = {}
            if path:
                dirs[alias] = path
            else:
                dirs.pop(alias, None)
            cfg[self.CONFIG_KEY_DEFAULT_DIRS] = dirs

        app_config.update_config_with(_apply, description='remote-default-dir')

    def _set_current_as_default_dir(self):
        """把当前所在目录设为当前主机的默认启动目录。"""
        if self._session is None:
            return
        path = self._current_path or self._session.home()
        self._set_default_dir(self._session.host_config.alias, path)

    def _clear_default_dir(self):
        """清除当前主机的默认启动目录（下次连接回到 home）。"""
        if self._session is None:
            return
        self._set_default_dir(self._session.host_config.alias, None)

    # ---- 排序方式（持久化到共享配置） ----

    CONFIG_KEY_SORT_KEY = 'remote_explorer_sort_key'
    CONFIG_KEY_SORT_DESC = 'remote_explorer_sort_desc'
    _SORT_KEYS = ('name', 'modified', 'size', 'type')

    def _load_sort(self) -> tuple:
        cfg = app_config.read_config()
        key = cfg.get(self.CONFIG_KEY_SORT_KEY, 'name')
        if key not in self._SORT_KEYS:
            key = 'name'
        return key, bool(cfg.get(self.CONFIG_KEY_SORT_DESC, False))

    def _save_sort(self):
        app_config.update_config({
            self.CONFIG_KEY_SORT_KEY: self._sort_key,
            self.CONFIG_KEY_SORT_DESC: self._sort_desc,
        }, description='remote-sort')

    # ---- 主机列表排序（持久化到共享配置） ----
    # 'manual'：用户自定顺序（可拖拽），存别名列表；'alias'/'host'：按字段升序。
    CONFIG_KEY_HOST_SORT = 'remote_explorer_host_sort'
    CONFIG_KEY_HOST_ORDER = 'remote_explorer_host_order'
    _HOST_SORT_MODES = ('manual', 'alias', 'host')

    def _load_host_sort(self) -> str:
        mode = app_config.read_config().get(self.CONFIG_KEY_HOST_SORT, 'manual')
        return mode if mode in self._HOST_SORT_MODES else 'manual'

    def _save_host_sort(self):
        app_config.update_config({self.CONFIG_KEY_HOST_SORT: self._host_sort},
                                 description='remote-host-sort')

    def _load_host_order(self) -> list:
        order = app_config.read_config().get(self.CONFIG_KEY_HOST_ORDER)
        return [a for a in order if isinstance(a, str)] if isinstance(order, list) else []

    def _save_host_order(self, order: list):
        app_config.update_config({self.CONFIG_KEY_HOST_ORDER: list(order)},
                                 description='remote-host-order')

    def _sorted_hosts(self, hosts: list) -> list:
        """按当前主机排序模式排列。manual 用保存的别名顺序（新主机/未记录的
        排到末尾，保持彼此相对顺序）；alias/host 按对应字段不区分大小写升序。"""
        if self._host_sort == 'alias':
            return sorted(hosts, key=lambda h: (h.alias or '').lower())
        if self._host_sort == 'host':
            return sorted(hosts, key=lambda h: ((h.hostname or '').lower(), h.port))
        # manual
        order = self._load_host_order()
        rank = {a: i for i, a in enumerate(order)}
        # 未在保存顺序里的主机排到末尾，按原有相对顺序稳定保留
        return sorted(hosts, key=lambda h: rank.get(h.alias, len(order)))

    def _set_host_sort(self, mode: str):
        if mode not in self._HOST_SORT_MODES or mode == self._host_sort:
            return
        self._host_sort = mode
        self._save_host_sort()
        self._populate_hosts_list()

    def _on_hosts_reordered(self, *args):
        """拖拽改变顺序后（仅 manual 模式）：把当前列表顺序存成新的手动顺序。"""
        if self._host_sort != 'manual':
            return
        order = []
        for i in range(self._hosts_list.count()):
            h = self._hosts_list.item(i).data(_ROLE_ENTRY)
            if h is not None:
                order.append(h.alias)
        self._save_host_order(order)

    def _sorted_entries(self, entries):
        """按当前排序方式排序，文件夹始终置顶（保持远程一贯行为）。

        用 Python 稳定排序分层处理：先按名称做基准，再按主键排序，
        最后用稳定排序把目录拎到前面 —— 这样目录组、文件组内部都按主键有序。
        """
        if not entries:
            return entries
        key = self._sort_key
        reverse = self._sort_desc
        out = sorted(entries, key=lambda e: e.name.lower())
        if key == 'modified':
            out.sort(key=lambda e: e.mtime, reverse=reverse)
        elif key == 'size':
            out.sort(key=lambda e: e.size, reverse=reverse)
        elif key == 'type':
            out.sort(key=lambda e: posixpath.splitext(e.name)[1].lower(),
                     reverse=reverse)
        elif reverse:  # name 降序
            out.sort(key=lambda e: e.name.lower(), reverse=True)
        # 目录始终置顶（稳定排序保留上面的组内顺序）
        out.sort(key=lambda e: not e.is_dir)
        return out

    def get_sort(self) -> tuple:
        return self._sort_key, self._sort_desc

    def set_sort(self, key: str, desc: bool):
        """设置排序方式：持久化 + 重建当前可见目录（已展开子树会收起）。"""
        if key not in self._SORT_KEYS:
            key = 'name'
        desc = bool(desc)
        if key == self._sort_key and desc == self._sort_desc:
            return
        self._sort_key = key
        self._sort_desc = desc
        self._save_sort()
        self._auto_refresh_fingerprints.clear()
        if self._session is not None:
            self._populate_tree_root()

    def _visible_entries(self, entries):
        """根据开关过滤掉以点开头的隐藏条目（在条目刚到达 UI 线程时统一过滤，
        这样指纹比对、建项、增量刷新看到的都是同一份"可见集合"）。"""
        if self._show_hidden or not entries:
            return entries
        return [e for e in entries if not e.name.startswith('.')]

    def _show_settings_menu(self):
        """齿轮按钮：弹出视图设置菜单（含"显示隐藏文件"开关）。"""
        menu = QMenu(self)
        accent = self.theme.get('accent_color', '#667eea')
        border = self.theme.get('border_color', '#3d3d5c')
        bg = self.theme.get('bg_medium', '#2d2d44')
        text = self.theme.get('text_color', '#eaeaea')
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {bg}; color: {text};
                border: 1px solid {border}; border-radius: 6px; padding: 4px;
            }}
            QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
            QMenu::item:selected {{ background-color: {accent}; }}
        """)
        act = menu.addAction(t("remote.show_hidden_files"))
        act.setCheckable(True)
        act.setChecked(self._show_hidden)
        act.toggled.connect(self._set_show_hidden)

        # 排序方式子菜单（名称 / 修改日期 / 大小 / 类型 + 升/降序）
        menu.addSeparator()
        sort_menu = menu.addMenu(t("sort.by"))
        sort_menu.setStyleSheet(menu.styleSheet())
        sort_group = QActionGroup(sort_menu)
        sort_group.setExclusive(True)
        for key, label in (('name', t("sort.name")), ('modified', t("sort.modified")),
                           ('size', t("sort.size")), ('type', t("sort.type"))):
            a = sort_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == self._sort_key)
            sort_group.addAction(a)
            a.triggered.connect(
                lambda checked=False, k=key: self.set_sort(k, self._sort_desc))
        sort_menu.addSeparator()
        order_group = QActionGroup(sort_menu)
        order_group.setExclusive(True)
        for desc, label in ((False, t("sort.ascending")), (True, t("sort.descending"))):
            a = sort_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(desc == self._sort_desc)
            order_group.addAction(a)
            a.triggered.connect(
                lambda checked=False, d=desc: self.set_sort(self._sort_key, d))

        # 已连接时：把当前目录设为该主机默认启动目录 / 清除
        if self._session is not None:
            menu.addSeparator()
            alias = self._session.host_config.alias
            set_act = menu.addAction(t("remote.set_default_dir"))
            set_act.triggered.connect(self._set_current_as_default_dir)
            current_default = self._get_default_dir(alias)
            if current_default:
                clear_act = menu.addAction(
                    t("remote.clear_default_dir", path=current_default)
                )
                clear_act.triggered.connect(self._clear_default_dir)

        menu.exec(self._settings_btn.mapToGlobal(
            self._settings_btn.rect().bottomLeft()
        ))

    def _set_show_hidden(self, show: bool):
        """切换是否显示隐藏文件：持久化 + 重建当前可见目录。"""
        show = bool(show)
        if show == self._show_hidden:
            return
        self._show_hidden = show
        self._save_show_hidden()
        # 重新拉取根目录与所有已展开子树，让过滤结果立即生效
        self._auto_refresh_fingerprints.clear()
        if self._session is not None:
            self._populate_tree_root()

    @classmethod
    def _make_item(cls, e: RemoteEntry) -> QTreeWidgetItem:
        """根据一个远端条目构建树节点（含图标、占位子项）。

        三处建项逻辑（顶层 / 展开子项 / 自动刷新新增）共用此方法，避免样式漏改。
        隐藏文件（以点开头）的"变淡"由 _RemoteItemDelegate.paint 统一处理 ——
        因为图标是彩色 emoji，setForeground 只能改文字颜色、改不动 emoji，
        所以整行用降低透明度的方式让图标和文字一起变淡。
        """
        icon = "📁 " if e.is_dir else "📄 "
        item = QTreeWidgetItem([icon + e.name])
        item.setData(0, _ROLE_ENTRY, e)
        item.setData(0, _ROLE_LOADED, False)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        if e.is_dir:
            # 占位让箭头出现，展开时才真正去 listdir
            item.addChild(QTreeWidgetItem(["…"]))
        return item

    def _apply_top_level(self, entries: list[RemoteEntry]):
        """把目录内容直接放到树根（批量插入 + 关闭重绘减少卡顿）"""
        entries = self._sorted_entries(self._visible_entries(entries))
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
            return  # 已加载 / 请求在途：重复展开不重复发请求
        entry: RemoteEntry = item.data(0, _ROLE_ENTRY)
        if not entry or not entry.is_dir:
            return
        if self._session is None:
            return  # 未连接：保持未加载态，连上后再展开会重试
        # 先标记，再把占位改成"加载中…"——listdir 在后台 executor 跑，
        # 结果通过 _subtree_ready/_subtree_failed 信号回 UI 线程
        item.setData(0, _ROLE_LOADED, True)
        self._set_dir_placeholder(item, t("remote.loading"))
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
            # 与建项/基线保持一致：隐藏开关关闭时先滤掉点开头的条目
            entries = self._visible_entries(entries)
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
        # 让新增项按当前排序顺序彼此相邻插入（整体顺序在下次手动刷新时归位）
        entries = self._sorted_entries(entries)
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

    @staticmethod
    def _set_dir_placeholder(item: QTreeWidgetItem, text: str):
        """把目录项下的占位子项（没挂 RemoteEntry 的）统一改成指定文案；
        没有占位时补一个。占位不可选中/编辑，仅作状态展示。"""
        try:
            placeholders = [item.child(i) for i in range(item.childCount())
                            if item.child(i).data(0, _ROLE_ENTRY) is None]
            if not placeholders:
                ph = QTreeWidgetItem([text])
                ph.setFlags(Qt.ItemFlag.NoItemFlags)
                item.addChild(ph)
                return
            for ph in placeholders:
                ph.setText(0, text)
                ph.setFlags(Qt.ItemFlag.NoItemFlags)
        except RuntimeError:
            pass  # item 已被销毁

    def _fill_children(self, parent_item: QTreeWidgetItem, path: str):
        if self._session is None:
            # 拿不到会话：还原未加载态，下次展开重试
            parent_item.setData(0, _ROLE_LOADED, False)
            return
        sess = self._session
        self._touch_activity()   # 展开目录算用户活动
        # 每次请求给该节点发一个递增的 generation；响应回来时 gen 失配
        # 说明节点其间被刷新/重新请求过，旧结果直接丢弃（防竞态串台）。
        gen = (parent_item.data(0, _ROLE_REQ_GEN) or 0) + 1
        parent_item.setData(0, _ROLE_REQ_GEN, gen)
        fut = sess.submit(sess.listdir, path)

        def on_done(f):
            try:
                entries: list[RemoteEntry] = f.result()
            except Exception as e:
                self._subtree_failed.emit(parent_item, gen, str(e))
                return
            self._subtree_ready.emit(parent_item, gen, entries)
        fut.add_done_callback(on_done)

    def _on_subtree_failed(self, parent_item: QTreeWidgetItem, gen: int, msg: str):
        """展开目录的 listdir 失败：占位项显示错误文案；
        还原未加载态，收起再展开即可重试。"""
        try:
            if sip.isdeleted(parent_item):
                return
            if gen != (parent_item.data(0, _ROLE_REQ_GEN) or 0):
                return  # 过期响应：其间已发起过新请求，由新请求负责收尾
            parent_item.setData(0, _ROLE_LOADED, False)
            self._set_dir_placeholder(
                parent_item, t("remote.load_failed", error=msg))
        except RuntimeError:
            return  # item 已被销毁（整树重建/断开）
        # 断线类错误仍走统一提示（带"一键重连"对话框）
        if self._looks_like_disconnect(msg):
            self._toast_error(msg)

    def _apply_children(self, parent_item: QTreeWidgetItem, gen: int,
                        entries: list[RemoteEntry]):
        # 父项可能已被释放（断开/整树重建），守一下；gen 失配说明该节点
        # 其间被刷新/重新请求过，这份结果已过期，直接丢弃。
        entries = self._sorted_entries(self._visible_entries(entries))
        try:
            if sip.isdeleted(parent_item):
                return
            if gen != (parent_item.data(0, _ROLE_REQ_GEN) or 0):
                return
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

    # ---------- 文件搜索（递归、多关键词组合） ----------

    def _on_search_text_changed(self, _text: str):
        """输入变化：空 → 立即退出搜索；非空 → 防抖后发起递归搜索。"""
        self._search_gen += 1  # 让上一轮后台结果作废
        if self._session is None or not self._search_edit.text().strip():
            self._search_timer.stop()
            self._exit_search()
            return
        self._search_timer.start()

    def _exit_search(self):
        """退出搜索态：隐藏结果列表，恢复正常文件树。"""
        self._search_results.setVisible(False)
        self._search_results.clear()
        self._tree.setVisible(True)

    def _start_search(self):
        """防抖结束 → 在 SSH 工作线程上递归扫描当前目录（逐目录链式 submit）。

        不再用一个大任务跑完整棵子树（那样会独占单工作线程，把导航/自动刷新/
        下载等其它 SFTP 操作全堵在后面）。改为每次只提交「一个目录」的 listdir，
        在它的回调里再提交下一个目录 —— 这样其它操作可以插在两次目录列举之间
        执行，搜索不再独占会话。

        注意：SSH 会话本身是单工作线程 + 全局锁，无法真正并发 SFTP 请求，所以
        任何时刻只保持一个在途 listdir；这里要的是"让位"（interleave）而非"并发"。
        """
        if self._session is None:
            return
        query = self._search_edit.text().strip()
        tokens = parse_search_tokens(query)
        if not tokens:
            self._exit_search()
            return
        # 每次发起都占一个唯一 generation：万一本方法被重复调用，旧链条会因
        # gen 失配自动停下，绝不会出现两条同 gen 链并行、重复列举/重复出结果。
        self._search_gen += 1
        gen = self._search_gen
        sess = self._session
        root = self._current_path or "/"
        # 切到结果视图并给个「搜索中」占位
        self._tree.setVisible(False)
        self._search_results.setVisible(True)
        self._search_results.clear()
        placeholder = QListWidgetItem(t("search.searching"))
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        self._search_results.addItem(placeholder)

        # 遍历状态通过参数显式贯穿各回调（不放 self.*），这样即便用户又发起了
        # 新搜索，旧链条也只会改它自己那份 state，绝不会污染新搜索。
        state = {
            'queue': deque([(root, 0)]),
            'results': [],
            'dirs': 0,
            'tokens': tokens,
            'show_hidden': self._show_hidden,
            'sess': sess,
        }
        self._search_step(gen, state)

    def _search_step(self, gen, state):
        """提交队列里下一个目录的 listdir；队列空则收尾。

        可在 UI 线程（首次）或工作线程（回调里）调用 —— submit 会把任务排到单
        工作线程队列尾部，因此期间排进来的导航/刷新任务会被先执行（让位）。"""
        if gen != self._search_gen:
            return  # 已被新搜索 / 导航 / 断开取消
        queue = state['queue']
        if not queue:
            self._search_finalize(gen, state, False)
            return
        path, depth = queue.popleft()
        sess = state['sess']
        try:
            fut = sess.submit(sess.listdir, path)
        except Exception:
            return  # 会话已关闭 / executor 已 shutdown → 直接停
        fut.add_done_callback(
            lambda f: self._on_dir_listed(f, gen, depth, state))

    def _on_dir_listed(self, fut, gen, depth, state):
        """单个目录 listdir 完成（工作线程回调）：匹配 + 入队子目录，再推进链条。

        ThreadPoolExecutor 在工作线程里同步调用本回调；这里 submit 下一个目录后
        立即返回，由 executor 取队列里的下一个任务执行 —— 不是嵌套调用，无栈增长。"""
        if gen != self._search_gen:
            return  # 已取消：在途的这次结果直接丢弃
        try:
            entries = fut.result()
        except Exception:
            entries = []
        state['dirs'] += 1
        results = state['results']
        queue = state['queue']
        tokens = state['tokens']
        show_hidden = state['show_hidden']
        truncated = False
        for e in entries:
            if not show_hidden and e.name.startswith('.'):
                continue  # 既不匹配也不深入隐藏项
            if name_matches_tokens(e.name, tokens):
                results.append(e)
                if len(results) >= self._SEARCH_MAX_RESULTS:
                    truncated = True
                    break
            if e.is_dir and not e.is_link and depth < self._SEARCH_MAX_DEPTH:
                queue.append((e.path, depth + 1))
        if not truncated and state['dirs'] >= self._SEARCH_MAX_DIRS:
            truncated = True
        if truncated:
            self._search_finalize(gen, state, True)
        else:
            self._search_step(gen, state)

    def _search_finalize(self, gen, state, truncated):
        """收尾：把结果发回 UI 线程（仅当仍是当前搜索）。每条链恰好调用一次。"""
        if gen != self._search_gen:
            return
        self._search_result_signal.emit(gen, state['results'], truncated)

    def _on_search_results(self, gen: int, results: list, truncated: bool):
        """回到 UI 线程：把命中项填进结果列表。"""
        if gen != self._search_gen:
            return  # 过期结果
        self._search_results.clear()
        if not results:
            empty = QListWidgetItem(t("search.no_results"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._search_results.addItem(empty)
            return
        root = self._current_path or "/"
        results.sort(key=lambda e: (not e.is_dir, e.path.lower()))
        for e in results:
            icon = "📁  " if e.is_dir else "📄  "
            rel = e.path[len(root):].lstrip("/") if e.path.startswith(root) else e.path
            item = QListWidgetItem(icon + (rel or e.name))
            item.setData(Qt.ItemDataRole.UserRole, e)
            item.setToolTip(e.path)
            self._search_results.addItem(item)
        if truncated:
            note = QListWidgetItem(t("search.truncated", count=len(results)))
            note.setFlags(Qt.ItemFlag.NoItemFlags)
            self._search_results.addItem(note)

    def _open_search_result(self, item: QListWidgetItem):
        """双击搜索结果：文件 → 打开；文件夹 → 进入该目录并退出搜索。"""
        e: RemoteEntry = item.data(Qt.ItemDataRole.UserRole)
        if not e:
            return
        if e.is_dir:
            self._search_edit.clear()  # 触发 _exit_search
            self._current_path = e.path
            self._path_edit.setText(self._current_path)
            self._populate_tree_root()
        else:
            self._open_remote_file(e)

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
        # 设过默认启动目录就回默认目录，否则回 SSH home —— 与连接时的行为保持一致
        default_dir = self._get_default_dir(self._session.host_config.alias)
        self._current_path = default_dir or self._session.home()
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
            # 文件没有自身目录，终端开在它所在的父目录
            file_dir = posixpath.dirname(entry.path.rstrip("/")) or "/"
            act_term = QAction(t("remote.open_terminal_here"), self)
            act_term.triggered.connect(lambda: self._open_terminal_at_path(file_dir))
            menu.addAction(act_term)
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

        Cmd+C 立刻给反馈（内部剪贴板 + 系统文字），同时在后台把选中项
        下载到临时目录（文件直接下，目录递归下），全部就绪后把系统剪贴板
        替换成本地文件 URL —— 这样用户切到 Finder / Slack / Notes 等任意应用
        Cmd+V 都能直接拿到真实文件 / 文件夹。
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

        # 3) 后台把选中项预下载到稳定的临时路径，结束后把剪贴板替换为
        #    file:// URL（这是 Finder / Slack / 大多数 macOS 应用接受的格式）。
        #    文件和目录都纳入：文件直接下载，目录递归下载。
        ready_paths: list[str] = []
        pending: list[tuple] = []
        for e in entries:
            local_path = self._temp_local_path_for(host_alias, e.path, e.name)
            # 文件且本地缓存大小一致 → 秒级就绪，直接进 URLs；
            # 目录无法校验完整性，一律当 pending 重新拉。
            if (not e.is_dir and e.size and os.path.isfile(local_path)
                    and os.path.getsize(local_path) == e.size):
                ready_paths.append(local_path)
            else:
                pending.append((host_alias, e, local_path))
        if not pending and not ready_paths:
            return

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
        # staging 状态挂在 self 上，供信号槽（UI 线程）读取。一次只跑一个 staging，
        # 新的复制会直接覆盖；剪贴板正确性由 _push 里的文字快照比对兜底。
        self._stage_total = len(pending)
        self._stage_has_dir = any(e.is_dir for _, e, _ in pending)
        self._stage_done = 0       # 顶层项完成数（触发 finalize）
        self._stage_files = 0      # 已下载文件数（含目录内）——给用户细粒度进度
        self._stage_completed: list[str] = list(ready_paths)
        self._stage_text = text_snapshot
        self._stage_ui_pending = False
        # 标记：这次 staging 的剪贴板文字快照。完成时只在剪贴板仍是该文字时才覆盖
        # （否则说明用户已经 Cmd+C 复制了别的东西，不应该越权改剪贴板）。
        self._clipboard_staging_snapshot = text_snapshot

        self._on_clipboard_stage_tick()  # 初始显示（UI 线程直调）

        def tick():
            # worker 线程：合并刷新——已有一帧在排队时不再重复发，避免灌爆 UI。
            if not self._stage_ui_pending:
                self._stage_ui_pending = True
                self._clipboard_stage_tick.emit()

        def on_file():
            self._stage_files += 1
            tick()

        for host_alias, entry, local_path in pending:
            def make_cb(_lp=local_path, _name=entry.name, _is_dir=entry.is_dir):
                def cb(f):
                    try:
                        f.result()
                        self._stage_completed.append(_lp)
                        # 文件项的“+1 文件”在这里记（目录项已在 on_file 里逐个记过）
                        if not _is_dir:
                            self._stage_files += 1
                    except Exception as e:
                        self._error_signal.emit(f"{_name}: {e}")
                    self._stage_done += 1
                    tick()
                    if self._stage_done >= self._stage_total:
                        self._clipboard_stage_finalize.emit()
                return cb
            try:
                # 目录递归下载作为单个 worker 任务（内部不再 submit，避免单
                # worker 串行执行时自我死锁）；文件走单次 download。
                if entry.is_dir:
                    fut = sess.submit(self._download_tree_sync, sess,
                                      entry.path, local_path, on_file)
                else:
                    fut = sess.submit(sess.download, entry.path, local_path)
                fut.add_done_callback(make_cb())
            except Exception as e:
                self._error_signal.emit(f"{entry.path}: {e}")
                self._stage_done += 1
                if self._stage_done >= self._stage_total:
                    self._clipboard_stage_finalize.emit()

    def _on_clipboard_stage_tick(self):
        """UI 线程：刷新预下载进度副标题。"""
        self._stage_ui_pending = False
        try:
            if getattr(self, "_stage_has_dir", False):
                txt = t("remote.clipboard_preparing_files", done=self._stage_files)
            else:
                txt = t("remote.clipboard_preparing",
                        done=self._stage_done, total=self._stage_total)
            self._subtitle_label.setText(txt)
        except RuntimeError:
            pass

    def _on_clipboard_stage_finalize(self):
        """UI 线程：预下载全部完成 → 把 file:// URL 写入系统剪贴板、还原副标题。"""
        try:
            if self._stage_completed:
                self._push_files_to_system_clipboard(self._stage_completed,
                                                     self._stage_text)
            if self._session is not None:
                self._subtitle_label.setText(self._session.host_config.alias)
        except RuntimeError:
            pass

    @staticmethod
    def _download_tree_sync(sess: SSHSession, remote_path: str, local_path: str,
                            on_file=None):
        """在 SSH worker 线程内同步把远端文件 / 目录递归下载到 local_path。

        只能从 sess.submit 的任务里调用（此时已在那个单 worker 线程上）：内部
        直接调用 sess.stat / listdir / download，**绝不再 sess.submit**——否则会
        排到同一个串行 worker 后面、把自己永远卡住。供剪贴板预下载用，不弹进度框。

        on_file()：每下完一个文件回调一次（在 worker 线程），供调用方更新进度。
        软链接目录不递归（避免环），按普通项交给 download 处理。
        """
        entry: RemoteEntry = sess.stat(remote_path)
        if not entry.is_dir:
            sess.download(remote_path, local_path)
            if on_file:
                on_file()
            return
        os.makedirs(local_path, exist_ok=True)
        for child in sess.listdir(remote_path):
            child_local = os.path.join(local_path, child.name)
            if child.is_dir and not child.is_link:
                RemoteExplorerPanel._download_tree_sync(sess, child.path,
                                                        child_local, on_file)
            else:
                sess.download(child.path, child_local)
                if on_file:
                    on_file()

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

        # 本地 → 远端的普通文件先攒成一批，冲突全部解析完后一次性提交，
        # 共用一个整体字节进度框（多文件粘贴不再一个文件弹一个进度）。
        # 目录仍走 _upload_local_dir：tar 快路径本身就是单一整体进度。
        pending_files: list[tuple[str, str]] = []  # (本地 src, 远端 dst)

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
                        pending_files.append((src, dst))
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

        # 批量上传攒下的本地文件：单 worker 线程串行执行，所有文件共用一个
        # live 字节计数，进度框显示「已完成文件累计 + 当前文件已传字节」
        if pending_files:
            futures = []
            sizes = []
            live = {"bytes": 0}
            live_cb = self._make_live_progress_cb(live)
            for src, dst in pending_files:
                try:
                    sizes.append(os.path.getsize(src))
                except OSError:
                    sizes.append(0)
                futures.append(sess.submit(sess.upload_with_progress,
                                           src, dst, live_cb))
            try:
                self._wait_future_with_progress(
                    futures, t("remote.pasting_progress", dst=target_dir),
                    sizes=sizes, live=live, abort_sessions=[sess])
            except Exception as e:
                errors.append(str(e))

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
            entries = self._await_remote(sess, sess.listdir, path,
                                         label=t("remote.pasting_progress", dst=path))
            return {e.name for e in entries}
        except Exception:
            return set()

    def _resolve_paste_conflict(self, name: str, sticky: Optional[str]):
        """跨目录/跨主机冲突时的三选一对话框（收敛到 explorer_common 单点维护）。"""
        return explorer_common.resolve_paste_conflict(self, name, sticky)

    def _refresh_subtree_by_path(self, path: str):
        """如果 tree 顶层有 path 对应的目录项，把它重刷一下；否则刷整棵树"""
        for i in range(self._tree.topLevelItemCount()):
            it = self._tree.topLevelItem(i)
            entry: RemoteEntry = it.data(0, _ROLE_ENTRY)
            if entry and entry.is_dir and entry.path == path:
                self._reload_subtree(it, path)
                return
        self._populate_tree_root()

    def _await_remote(self, sess: SSHSession, fn, *args, label: str):
        """提交单个远端操作，在事件循环等待中返回结果。

        粘贴/覆盖删除流程里的 stat/listdir/remove 一律走这里，
        禁止直接 fut.result()——网络一慢就是整窗无限期冻结。
        """
        self._touch_activity()   # 文件操作（stat/listdir/remove/…）算用户活动
        fut = sess.submit(fn, *args)
        self._wait_future_with_progress([fut], label, abort_sessions=[sess])
        return fut.result()

    def _remote_remove(self, sess: SSHSession, path: str):
        """删除远端文件或目录（递归）"""
        label = t("remote.pasting_progress", dst=path)
        try:
            entry: RemoteEntry = self._await_remote(sess, sess.stat, path, label=label)
        except Exception:
            return
        if entry.is_dir and not entry.is_link:
            self._await_remote(sess, sess.remove_tree, path, label=label)
        else:
            self._await_remote(sess, sess.remove, path, label=label)

    def _upload_local_dir(self, sess: SSHSession, local_dir: str, remote_dir: str):
        """把本地目录递归上传到 remote_dir。

        快路径：远端有 tar（Linux 基本必有）→ 本地打 tar 流经单条 SSH 通道
        直灌、远端边收边解。逐文件 SFTP 每个文件 5-6 次网络往返且串行，
        小文件多的目录慢 1-2 个数量级，仅在远端无 tar 时兜底。
        """
        fut = sess.submit(sess.remote_has_tar)
        self._wait_future_with_progress(
            [fut], t("remote.pasting_progress", dst=remote_dir),
            tolerate_errors=True, abort_sessions=[sess])
        try:
            has_tar = bool(fut.result())
        except Exception:
            has_tar = False
        if has_tar:
            total = 0
            for root, _dirs, files in os.walk(local_dir):
                for fname in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fname))
                    except OSError:
                        pass
            live = {"bytes": 0}
            fut = sess.submit(sess.upload_dir_tar, local_dir, remote_dir,
                              total, self._make_live_progress_cb(live))
            self._wait_future_with_progress(
                [fut], t("remote.pasting_progress", dst=remote_dir),
                sizes=[total], live=live, abort_sessions=[sess])
            return

        # 必须走 _wait_future_with_progress：裸 fut.result() 会在 GUI 线程上
        # 无限期阻塞（断线重连、SFTP 卡死），且此刻连进度框都还没弹，用户
        # 连「取消」都点不到 —— 整窗连同所有终端标签一起冻死。
        fut = sess.submit(sess.mkdir, remote_dir)
        self._wait_future_with_progress(
            [fut], t("remote.pasting_progress", dst=remote_dir),
            tolerate_errors=True, abort_sessions=[sess])
        try:
            fut.result()
        except Exception as e:
            # 可能已存在，忽略；真实权限问题会在后续上传时显式报错
            logger.debug(f"[RemoteExplorerPanel] mkdir {remote_dir} failed: {e}")
        futures = []
        sizes = []  # 与 futures 对齐：upload 记文件字节数，mkdir 记 0
        # 单 worker 线程串行执行 → 所有文件共用一个 live 字节计数即可，
        # 进度框显示"已完成文件累计 + 当前文件已传字节"
        live = {"bytes": 0}
        live_cb = self._make_live_progress_cb(live)
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
                sizes.append(0)
            for fname in files:
                local_path = os.path.join(root, fname)
                r_path = posixpath.join(remote_root, fname)
                futures.append(sess.submit(sess.upload_with_progress,
                                           local_path, r_path, live_cb))
                try:
                    sizes.append(os.path.getsize(local_path))
                except OSError:
                    sizes.append(0)
        # 等所有 future 完成；mkdir 的失败吞掉
        self._wait_future_with_progress(futures, t("remote.pasting_progress",
                                                   dst=remote_dir),
                                        tolerate_errors=True, sizes=sizes,
                                        live=live, abort_sessions=[sess])

    def _remote_to_remote(self, src_sess: SSHSession, dst_sess: SSHSession,
                          src_path: str, dst_path: str):
        """跨/同 session 远程 → 远程：经本地 temp 中转"""
        # 先 stat 源判断是不是目录
        entry: RemoteEntry = self._await_remote(
            src_sess, src_sess.stat, src_path,
            label=t("remote.pasting_progress", dst=dst_path))
        tmp_root = tempfile.mkdtemp(prefix="smart_terminal_paste_")
        try:
            tmp_local = os.path.join(tmp_root, os.path.basename(src_path.rstrip("/")) or "item")
            if entry.is_dir:
                self._download_remote_recursive(src_sess, src_path, tmp_local)
                self._upload_local_dir(dst_sess, tmp_local, dst_path)
            else:
                fut = src_sess.submit(src_sess.download, src_path, tmp_local)
                self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                         dst=dst_path),
                                                abort_sessions=[src_sess])
                try:
                    nbytes = os.path.getsize(tmp_local)
                except OSError:
                    nbytes = 0
                live = {"bytes": 0}
                fut2 = dst_sess.submit(dst_sess.upload_with_progress,
                                       tmp_local, dst_path,
                                       self._make_live_progress_cb(live))
                self._wait_future_with_progress([fut2], t("remote.pasting_progress",
                                                          dst=dst_path),
                                                sizes=[nbytes], live=live,
                                                abort_sessions=[dst_sess])
        finally:
            try:
                import shutil as _shutil
                _shutil.rmtree(tmp_root, ignore_errors=True)
                if os.path.exists(tmp_root):
                    logger.warning(f"[RemoteExplorerPanel] temp dir not fully removed: {tmp_root}")
            except Exception as e:
                logger.warning(f"[RemoteExplorerPanel] temp dir cleanup failed: {tmp_root}: {e}")

    def _download_remote_recursive(self, sess: SSHSession, remote_path: str, local_path: str):
        """通过 src session 把远程文件 / 目录递归下载到 local_path（阻塞）"""
        entry: RemoteEntry = self._await_remote(
            sess, sess.stat, remote_path,
            label=t("remote.pasting_progress", dst=local_path))
        if not entry.is_dir:
            fut = sess.submit(sess.download, remote_path, local_path)
            self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                     dst=local_path),
                                            abort_sessions=[sess])
            return
        os.makedirs(local_path, exist_ok=True)
        children = self._await_remote(
            sess, sess.listdir, remote_path,
            label=t("remote.pasting_progress", dst=local_path))
        for child in children:
            child_local = os.path.join(local_path, child.name)
            if child.is_dir and not child.is_link:
                self._download_remote_recursive(sess, child.path, child_local)
            else:
                fut = sess.submit(sess.download, child.path, child_local)
                self._wait_future_with_progress([fut], t("remote.pasting_progress",
                                                         dst=local_path),
                                                abort_sessions=[sess])

    def _wait_future_with_progress(self, futures: list, label: str,
                                    tolerate_errors: bool = False,
                                    sizes: Optional[list] = None,
                                    live: Optional[dict] = None,
                                    abort_sessions: Optional[list] = None):
        """阻塞等待 futures 完成，跑事件循环避免 UI 卡死。

        sizes：与 futures 一一对应的字节数（未知填 0/None）。给出时进度框
        文案追加 "x MB / y MB · 速率 · 剩余时间"。

        live：可选的 {"bytes": int} 共享计数 —— upload_with_progress /
        download_with_progress 的字节级回调（worker 线程）往里写"当前
        正在传的这个文件已完成的字节数"，这里在主线程轮询时读出来叠加到
        已完成 future 的累计字节上，让单个大文件传输期间速率/ETA 也会动。
        不给 live 时退化为旧行为（按已完成文件粒度累计）。"""
        if not futures:
            return
        # 有可中断的会话时给一个「取消」按钮：点了就 abort 这些会话，直接关 socket，
        # 让卡在 recv 上的传输立刻失败、对话框随即关闭，避免网络切换时一直卡在传输框里。
        cancel_text = t("remote.cancel_transfer") if abort_sessions else None
        total_bytes = sum(s or 0 for s in sizes) if sizes else 0
        # 进度条刻度：知道总字节时按字节走（千分比），否则退化为「完成的任务数」。
        # 按任务数在单任务传输（tar 整目录快路径、单个大文件）时永远是 0/1 ——
        # 条子空着直到传完瞬间跳满，看不出任何进度。
        bar_max = _BYTE_BAR_SCALE if total_bytes > 0 else len(futures)
        progress = QProgressDialog(label, cancel_text, 0, bar_max, self)
        progress.setWindowTitle(t("remote.title"))
        # 非模态：大文件粘贴/传输期间应用可继续正常使用（后台传输），
        # 进度框只悬浮展示进度。等待仍走下面的局部事件循环，传输间的
        # 用户操作在嵌套循环里正常处理；同一 session 的操作由其单 worker
        # 线程天然串行，不会并发冲突。
        progress.setWindowModality(Qt.WindowModality.NonModal)
        progress.setMinimumDuration(300)
        progress.setValue(0)
        if abort_sessions:
            def _on_cancel(_sessions=list(abort_sessions)):
                for s in _sessions:
                    if s is not None:
                        try:
                            s.abort()
                        except Exception as e:
                            logger.debug(f"[RemoteExplorerPanel] session abort failed: {e}")
            progress.canceled.connect(_on_cancel)
        done = {"n": 0, "errors": [], "bytes": 0}
        tracker = _TransferRateTracker() if total_bytes > 0 else None
        # 进度文案节流（与 subtitle 路径的 350ms 节流一致）：QTimer 80ms
        # 一跳只更新进度条数值，label setText 限到 ~3Hz，避免布局抖动
        label_ts = {"t": 0.0}

        def make_cb(nbytes=0):
            def cb(f):
                try:
                    f.result()
                except Exception as e:
                    done["errors"].append(str(e))
                done["n"] += 1
                # 当前文件收尾：live 里的"进行中字节"换成整文件累计，
                # 先清零再累加（瞬时少算一帧，比多算一帧不会倒退）
                if live is not None:
                    live["bytes"] = 0
                done["bytes"] += nbytes or 0
            return cb

        for i, fut in enumerate(futures):
            nbytes = sizes[i] if sizes and i < len(sizes) else 0
            fut.add_done_callback(make_cb(nbytes))

        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(80)
        def tick():
            # 父 widget 可能在等待期间被销毁（如用户关掉 panel/窗口），
            # 此时 progress 也已 deleteLater'd → 任何访问都会段错误
            try:
                if sip.isdeleted(progress):
                    # 面板在传输中被销毁：abort 会话让 pending futures 快速
                    # 失败，避免调用方后续 fut.result() 隐形阻塞主线程
                    for s in (abort_sessions or []):
                        if s is not None:
                            try:
                                s.abort()
                            except Exception:
                                logger.debug("abort on progress deletion failed",
                                             exc_info=True)
                    timer.stop()
                    loop.quit()
                    return
                # 传输进行中一律算活动，别让空闲看门狗掐掉正在传数据的连接
                self._touch_activity()
                cur_bytes = done["bytes"] + (live["bytes"] if live else 0)
                if total_bytes > 0:
                    progress.setValue(min(_BYTE_BAR_SCALE,
                                          cur_bytes * _BYTE_BAR_SCALE // total_bytes))
                else:
                    progress.setValue(done["n"])
                now = time.monotonic()
                if (tracker is not None and cur_bytes < total_bytes
                        and now - label_ts["t"] >= 0.35):
                    label_ts["t"] = now
                    tracker.update("batch", cur_bytes)
                    text = (f"{label} · {self._fmt_size(cur_bytes)}"
                            f" / {self._fmt_size(total_bytes)}")
                    stats = self._transfer_stats_text(
                        tracker, cur_bytes, total_bytes)
                    if stats:
                        text += f" · {stats}"
                    progress.setLabelText(text)
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
                progress.setValue(bar_max)   # 收尾拉满（关闭对话框）
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
        fut = sess.submit(sess.download_with_progress, remote_path, local_path,
                          self._make_progress_cb(remote_path))

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

    def _make_progress_cb(self, remote_path: str):
        """构造给 paramiko 的字节级进度回调（worker 线程触发，节流后发信号回 UI）。"""
        def progress_cb(done, total):
            # paramiko 在 worker 线程调用 —— 节流后通过信号回 UI 线程
            now = time.monotonic()
            # 350ms 节流（~3Hz）：肉眼看着仍流畅，但 setText 不会高频触发
            # 任何 layout 重算，避免给用户"窗口一直在抖"的错觉。
            if now - self._last_progress_emit_ts < 0.35 and done < total:
                return
            self._last_progress_emit_ts = now
            self._download_progress.emit(remote_path, done, total)
        return progress_cb

    @staticmethod
    def _make_live_progress_cb(live: dict):
        """构造给 upload_with_progress 的字节级回调（worker 线程触发）。

        只往共享 dict 写一个 int（GIL 保证原子），不碰任何 UI / 信号；
        主线程的轮询 timer（_wait_future_with_progress / _handle_drop_upload）
        负责读出来展示。批量上传在单 worker 线程上串行执行，所以多个文件
        共用同一个 live 计数也不会互相踩。"""
        def cb(bytes_done, _bytes_total):
            live["bytes"] = bytes_done
        return cb

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

    @classmethod
    def _fmt_rate(cls, bps: float) -> str:
        """速率文案：1.5 MB/s / 320.0 KB/s"""
        return f"{cls._fmt_size(int(bps))}/s"

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        """剩余时间 mm:ss（向最近秒取整）"""
        secs = max(0, int(seconds + 0.5))
        return f"{secs // 60}:{secs % 60:02d}"

    def _transfer_stats_text(self, tracker: "_TransferRateTracker",
                             bytes_done: int, bytes_total: int) -> str:
        """根据 tracker 生成 "1.5 MB/s · 剩余 0:04" 尾缀；速率未知返回空串。"""
        rate = tracker.rate()
        if rate <= 0:
            return ""
        eta = tracker.eta_secs(bytes_done, bytes_total)
        if eta is None:
            return self._fmt_rate(rate)
        return t("remote.transfer_stats",
                 rate=self._fmt_rate(rate), eta=self._fmt_eta(eta))

    def _on_download_progress(self, remote_path: str, done: int, total: int):
        """节流后的下载进度更新 —— 写到 subtitle 标签上（含速率/剩余时间）"""
        # 传输本身算活动：几小时的大文件下载期间用户可能一次都不点面板，
        # 空闲看门狗不能把正在传数据的主连接掐掉
        self._touch_activity()
        if done < 0:
            # 出错/收尾信号：还原 subtitle
            sess = self._session
            self._subtitle_label.setText(sess.host_config.alias if sess else "")
            self._dl_rate.reset()
            return
        name = posixpath.basename(remote_path)
        # 按 remote_path 维护滑动窗口；批量传输切换文件时自动重置 → 按当前文件显示
        self._dl_rate.update(remote_path, done)
        if total > 0:
            text = t("remote.downloading", name=name,
                     done=self._fmt_size(done), total=self._fmt_size(total))
            if done < total:
                stats = self._transfer_stats_text(self._dl_rate, done, total)
                if stats:
                    text += f" · {stats}"
        else:
            text = t("remote.downloading_unknown", name=name)
            rate = self._dl_rate.rate()
            if rate > 0:
                text += f" · {self._fmt_rate(rate)}"
        self._subtitle_label.setText(text)

    def _on_file_ready(self, host_alias: str, remote_path: str, local_path: str):
        """流式下载完成（或缓存命中）→ 通知主窗口在编辑器/预览里打开"""
        # 还原 subtitle 显示
        sess = self._session
        if sess is not None:
            self._subtitle_label.setText(sess.host_config.alias)
        self._open_temp_map[local_path] = (host_alias, remote_path)
        # 用发起下载时的 session，而不是当前 self._session（期间可能已切换/断开）
        sess_for_open = self._open_session_map.pop(local_path, self._session)
        if not explorer_common.editor_can_display(local_path):
            # 编辑器展示不了的格式（xlsx 等二进制）→ 系统默认应用打开临时
            # 文件。只读查看语义：系统程序里的改动不会回传远端。
            QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))
            return
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
        if not explorer_common.editor_can_display(local_path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(local_path))
            return
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
        # 默认无扩展名：用户在原地重命名时自己决定后缀（不强加 .txt）
        name = self._unique_new_name(parent_item, "untitled", "")
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
        # download_with_progress：内部先写 <目标>.part、完成后原子 rename ——
        # 中途失败/断线不会留下半成品覆盖用户文件；顺带拿到字节级进度。
        size_hint = entry.size or 0
        self._subtitle_label.setText(
            t("remote.downloading", name=entry.name, done=self._fmt_size(0),
              total=self._fmt_size(size_hint)) if size_hint
            else t("remote.downloading_unknown", name=entry.name)
        )
        self._last_progress_emit_ts = 0.0
        fut = sess.submit(sess.download_with_progress, entry.path, save_path,
                          self._make_progress_cb(entry.path))

        def on_done(f):
            try:
                f.result()
            except Exception as e:
                self._error_signal.emit(str(e))
            # 成功/失败都通过信号回 UI 线程还原 subtitle
            self._download_progress.emit(entry.path, -1, -1)
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

        self._last_progress_emit_ts = 0.0
        for ent, _item in entries:
            try:
                name = explorer_clipboard.next_free_name(ent.name, exists_fn)
                dst = os.path.join(target_dir, name)
                if ent.is_dir:
                    # 目录：递归下载（沿用 paste 走的路径），逐文件报字节进度
                    fut = sess.submit(self._sftp_download_dir_blocking, sess,
                                      ent.path, dst, self._make_progress_cb)
                else:
                    fut = sess.submit(sess.download_with_progress, ent.path, dst,
                                      self._make_progress_cb(ent.path))

                def on_done(f, _name=name, _rp=ent.path):
                    try:
                        f.result()
                    except Exception as e:
                        self._error_signal.emit(f"{_name}: {e}")
                    # 单工作线程串行执行：本条收尾后下一条的进度才会出现，
                    # 不会互相覆盖；最后一条收尾把 subtitle 还原成主机名。
                    self._download_progress.emit(_rp, -1, -1)
                fut.add_done_callback(on_done)
            except Exception as e:
                self._error_signal.emit(f"{ent.path}: {e}")

    @staticmethod
    def _sftp_download_dir_blocking(sess, remote_dir: str, local_dir: str,
                                    cb_factory=None):
        """递归下载远端目录到本地（在 SSH worker 线程里跑，阻塞）。

        cb_factory(remote_path) → paramiko 字节级进度回调；每个文件都走
        download_with_progress（.part + 原子 rename），进度按当前文件展示。"""
        os.makedirs(local_dir, exist_ok=True)
        entries = sess.listdir(remote_dir)
        for e in entries:
            child_remote = e.path
            child_local = os.path.join(local_dir, e.name)
            if e.is_dir:
                RemoteExplorerPanel._sftp_download_dir_blocking(
                    sess, child_remote, child_local, cb_factory)
            else:
                cb = cb_factory(child_remote) if cb_factory is not None else None
                sess.download_with_progress(child_remote, child_local, cb)

    def _open_terminal_here(self, entry: RemoteEntry):
        self._open_terminal_at_path(entry.path)

    def _open_terminal_at_path(self, path: str):
        if self._session is None:
            return
        self.open_terminal_at.emit(self._session.host_config, path)

    def _on_path_edit_context_menu(self, pos):
        """路径输入框右键：保留默认编辑项，并追加「在此打开终端」。"""
        menu = self._path_edit.createStandardContextMenu()
        path = self._path_edit.text().strip()
        if self._session is not None and path:
            menu.addSeparator()
            act_term = QAction(t("remote.open_terminal_here"), menu)
            act_term.triggered.connect(lambda: self._open_terminal_at_path(path))
            menu.addAction(act_term)
        menu.exec(self._path_edit.mapToGlobal(pos))

    def _upload_at(self, parent_path: str, parent_item: Optional[QTreeWidgetItem]):
        local_path, _ = QFileDialog.getOpenFileName(self, t("remote.upload"))
        if not local_path:
            return
        sess = self._session
        if sess is None:
            return
        remote_path = posixpath.join(parent_path, os.path.basename(local_path))
        try:
            nbytes = os.path.getsize(local_path)
        except OSError:
            nbytes = 0
        live = {"bytes": 0}
        fut = sess.submit(sess.upload_with_progress, local_path, remote_path,
                          self._make_live_progress_cb(live))
        # 错误经 _refresh_after 的 error_signal 报告，这里 tolerate 掉避免重复弹窗
        self._refresh_after(fut, parent_item, parent_path)
        self._wait_future_with_progress([fut], t("remote.uploading_to",
                                                 dst=parent_path),
                                        tolerate_errors=True,
                                        sizes=[nbytes], live=live,
                                        abort_sessions=[sess])

    def _upload_into(self, dir_entry: RemoteEntry, dir_item: QTreeWidgetItem):
        self._upload_at(dir_entry.path, dir_item)

    def _handle_drop_upload(self, local_paths: list[str], target_dir: str,
                              target_item: Optional[QTreeWidgetItem]):
        """处理拖入：把每个本地文件上传到 target_dir，完成后刷新对应子树"""
        if self._session is None:
            return
        sess = self._session
        total = len(local_paths)
        base_label = t("remote.uploading_to", dst=target_dir)

        # 让 progress 通过信号在主线程里更新
        done_counter = {"n": 0, "errors": [], "bytes": 0}
        size_by_path: dict[str, int] = {}
        for lp in local_paths:
            try:
                size_by_path[lp] = os.path.getsize(lp) if os.path.isfile(lp) else 0
            except OSError:
                size_by_path[lp] = 0
        total_bytes = sum(size_by_path.values())
        tracker = _TransferRateTracker() if total_bytes > 0 else None
        # 进度条按字节推进（总字节已知时），否则退化为完成的文件数 —— 拖入
        # 单个大文件时按文件数会全程停在 0/1，看不出进度
        bar_max = _BYTE_BAR_SCALE if total_bytes > 0 else total
        progress = QProgressDialog(base_label, "Cancel", 0, bar_max, self)
        progress.setWindowTitle(t("remote.title"))
        progress.setMinimumDuration(300)
        progress.setValue(0)
        # upload_with_progress 的字节级回调（worker 线程）写这个共享计数：
        # 当前文件已传字节。单 worker 串行执行，所有文件共用一个计数即可。
        live = {"bytes": 0}
        live_cb = self._make_live_progress_cb(live)
        # label setText 节流到 ~3Hz（350ms），与 subtitle 路径一致
        label_ts = {"t": 0.0}

        def make_upload_done(local_path):
            def cb(f):
                try:
                    f.result()
                except Exception as e:
                    done_counter["errors"].append(f"{os.path.basename(local_path)}: {e}")
                done_counter["n"] += 1
                # 当前文件的进行中字节换算成整文件累计（先清零再累加）
                live["bytes"] = 0
                done_counter["bytes"] += size_by_path.get(local_path, 0)
                # 不能在工作线程更新 UI，靠 progress.setValue 在主循环里查询
            return cb

        for lp in local_paths:
            remote_name = os.path.basename(lp)
            remote_path = posixpath.join(target_dir, remote_name)
            fut = sess.submit(sess.upload_with_progress, lp, remote_path, live_cb)
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
                cur_bytes = done_counter["bytes"] + live["bytes"]
                if total_bytes > 0:
                    progress.setValue(min(_BYTE_BAR_SCALE,
                                          cur_bytes * _BYTE_BAR_SCALE // total_bytes))
                else:
                    progress.setValue(done_counter["n"])
                now = time.monotonic()
                if (tracker is not None and cur_bytes < total_bytes
                        and now - label_ts["t"] >= 0.35):
                    label_ts["t"] = now
                    tracker.update("upload", cur_bytes)
                    text = (f"{base_label} · {self._fmt_size(cur_bytes)}"
                            f" / {self._fmt_size(total_bytes)}")
                    stats = self._transfer_stats_text(
                        tracker, cur_bytes, total_bytes)
                    if stats:
                        text += f" · {stats}"
                    progress.setLabelText(text)
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
                progress.setValue(bar_max)   # 收尾拉满（关闭对话框）
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
            box.addButton(
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
                logger.debug("restore_once: suppressed exception", exc_info=True)
            if sess is self._session and saved_cwd:
                self._current_path = saved_cwd
                self._path_edit.setText(saved_cwd)
                self._populate_tree_root()
        sess.connected.connect(restore_once)
