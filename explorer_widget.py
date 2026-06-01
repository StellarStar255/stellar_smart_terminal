"""
Explorer 文件浏览器面板
提供类似 Cursor/VS Code 的文件浏览器功能
"""
import os
import posixpath
import sys
import subprocess
import shutil
from pathlib import Path
from typing import Optional

from i18n import t
import explorer_clipboard

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QTreeView, QMenu, QLineEdit, QMessageBox,
    QAbstractItemView, QFileDialog, QApplication, QProgressDialog,
    QStyledItemDelegate
)
from PyQt6.QtCore import Qt, QDir, QModelIndex, QPersistentModelIndex, pyqtSignal, QTimer, QEventLoop
from PyQt6.QtGui import (
    QFileSystemModel, QAction, QDesktopServices, QCursor,
    QShortcut, QKeySequence,
)
from PyQt6.QtCore import QUrl


class _LocalDropTreeView(QTreeView):
    """支持把 file:// URL 拖入并复制到落点目录的本地文件树

    与 QFileSystemModel 默认的"移动"语义不同：这里所有外部 URL 都做 **复制**，
    这样跨窗口/跨远程往本地拖文件不会破坏源。
    """

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeView.DragDropMode.DragDrop)

        # Finder 风格：已选中的条目再次单击 → 延迟进入原地重命名
        self._pending_rename_index = None  # QPersistentModelIndex 或 None
        self._rename_timer = QTimer(self)
        self._rename_timer.setSingleShot(True)
        self._rename_timer.timeout.connect(self._fire_pending_rename)

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

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()

        # Delete 键 或 Cmd+Backspace（macOS Finder 风格）→ 删除选中（支持多选）
        # Qt 在 macOS 上把 Cmd 映射到 ControlModifier，因此跨平台都用 Control 判定。
        is_delete = (key == Qt.Key.Key_Delete) or (
            key == Qt.Key.Key_Backspace
            and bool(mods & Qt.KeyboardModifier.ControlModifier)
        )
        if is_delete:
            sel = self.selectionModel()
            if sel is not None:
                paths: list[str] = []
                seen = set()
                model = self.model()
                for idx in sel.selectedIndexes():
                    if idx.column() != 0:
                        continue
                    if idx == self.rootIndex():
                        continue
                    p = model.filePath(idx) if hasattr(model, "filePath") else None
                    if p and p not in seen:
                        seen.add(p)
                        paths.append(p)
                if paths:
                    self._cancel_pending_rename()
                    self._panel._delete_paths(paths)
                    event.accept()
                    return

        # 选中单个条目时，Enter / F2 进入原地重命名
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_F2):
            sel = self.selectionModel()
            if sel is not None:
                rows = {i.row(): i for i in sel.selectedIndexes() if i.column() == 0}
                if len(rows) == 1:
                    idx = next(iter(rows.values()))
                    if idx.isValid() and idx != self.rootIndex():
                        self._cancel_pending_rename()
                        self.setCurrentIndex(idx)
                        self.edit(idx)
                        event.accept()
                        return
        super().keyPressEvent(event)

    def _cancel_pending_rename(self):
        self._rename_timer.stop()
        self._pending_rename_index = None

    def _fire_pending_rename(self):
        pidx = self._pending_rename_index
        self._pending_rename_index = None
        if pidx is None:
            return
        idx = QModelIndex(pidx)
        if not idx.isValid() or idx == self.rootIndex():
            return
        sel = self.selectionModel()
        if sel is None or not sel.isSelected(idx):
            return
        self.edit(idx)

    def mousePressEvent(self, event):
        # 只在左键、无修饰键、点中实际条目时考虑 Finder 式延迟重命名
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            idx = self.indexAt(event.position().toPoint())
            if idx.isValid() and idx.column() == 0 and idx != self.rootIndex():
                sel = self.selectionModel()
                was_only_selected = False
                if sel is not None:
                    selected_rows = {(i.row(), i.parent()) for i in sel.selectedIndexes() if i.column() == 0}
                    was_only_selected = (
                        sel.isSelected(idx) and len(selected_rows) == 1
                    )
                if was_only_selected:
                    # 该项在本次点击前已是唯一选中项 → 等待双击窗口结束后再进入重命名
                    self._pending_rename_index = QPersistentModelIndex(idx)
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
        # 双击 → 取消延迟重命名（让 doubleClicked 走打开文件的逻辑）
        self._cancel_pending_rename()
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        # 按住左键移动（可能是拖拽）→ 取消延迟重命名
        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._pending_rename_index is not None):
            self._cancel_pending_rename()
        super().mouseMoveEvent(event)

    def dropEvent(self, event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        local_paths = [u.toLocalFile() for u in urls if u.isLocalFile() and u.toLocalFile()]
        if not local_paths:
            super().dropEvent(event)
            return
        # 找到落点目录
        idx = self.indexAt(event.position().toPoint())
        target_dir = None
        if idx.isValid():
            model = self.model()
            path = model.filePath(idx)
            if path:
                if os.path.isdir(path):
                    target_dir = path
                else:
                    target_dir = os.path.dirname(path)
        if not target_dir:
            target_dir = self._panel._current_path or os.path.expanduser("~")
        event.acceptProposedAction()
        self._panel._handle_drop_copy(local_paths, target_dir)


class _RenameNameOnlyDelegate(QStyledItemDelegate):
    """原地重命名时只选中文件"基名"部分，避免误改扩展名。

    目录、无扩展名文件（含以点开头的隐藏文件）保持全选。
    用 singleShot(0) 推到事件队列末尾，绕开 Qt item-view 内部 show/focus
    之后偶尔会触发的 selectAll。"""

    def setEditorData(self, editor, index):
        super().setEditorData(editor, index)
        if not isinstance(editor, QLineEdit) or not index.isValid():
            return
        is_dir = False
        model = index.model()
        if isinstance(model, QFileSystemModel):
            is_dir = model.isDir(index)
        text = editor.text()
        if is_dir or not text:
            return
        stem, ext = os.path.splitext(text)
        if not ext or not stem:
            return
        sel_len = len(stem)

        def _apply():
            try:
                editor.setSelection(0, sel_len)
            except RuntimeError:
                pass  # editor 已销毁

        QTimer.singleShot(0, _apply)


class FilteredFileSystemModel(QFileSystemModel):
    """过滤隐藏文件的文件系统模型"""

    # 默认隐藏的文件/文件夹
    HIDDEN_PATTERNS = {
        '.git', '.svn', '.hg',
        '__pycache__', '.pytest_cache', '.mypy_cache',
        'node_modules', '.npm', '.yarn',
        '.DS_Store', 'Thumbs.db', '.idea',
        '.vscode', '.vs', '*.pyc', '*.pyo',
        '.env', '.venv', 'venv', 'env',
        '.eggs', '*.egg-info', 'dist', 'build',
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._show_hidden = False

    def filterAcceptsRow(self, source_row, source_parent):
        """过滤隐藏文件"""
        if self._show_hidden:
            return True

        index = self.index(source_row, 0, source_parent)
        if not index.isValid():
            return True

        name = self.fileName(index)

        # 过滤以点开头的文件（隐藏文件）
        if name.startswith('.'):
            return False

        # 过滤特定模式
        if name in self.HIDDEN_PATTERNS:
            return False

        return True

    def set_show_hidden(self, show: bool):
        """设置是否显示隐藏文件"""
        self._show_hidden = show
        # 刷新视图
        self.setRootPath(self.rootPath())


class ExplorerPanel(QWidget):
    """Explorer 文件浏览器面板"""

    # 信号
    file_double_clicked = pyqtSignal(str)  # 文件双击信号
    file_edit_requested = pyqtSignal(str)  # 请求在内置编辑器中打开文件
    save_file_requested = pyqtSignal()  # 请求保存当前编辑的文件
    save_file_as_requested = pyqtSignal()  # 请求另存为当前编辑的文件

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._current_path = os.path.expanduser("~")
        self._editing_file = None  # 当前正在编辑的文件路径

        self._setup_ui()
        self._connect_signals()

        # 自动刷新兜底：QFileSystemModel 已经通过 FSEvents 监听本地变更；
        # 这里只是一道保险，60s 检查一次当前目录的条目集合，差异时才 refresh。
        # 没差异 = 一次 os.listdir，开销可忽略。
        self._auto_refresh_fingerprint: frozenset = frozenset()
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(60_000)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_tick)
        self._auto_refresh_timer.start()

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 文件系统模型
        self.model = QFileSystemModel()
        self.model.setRootPath("")
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        # 允许通过模型对文件/文件夹原地重命名
        self.model.setReadOnly(False)

        # 树形视图（自定义子类，支持把 file:// URL 拖入并复制）
        self.tree_view = _LocalDropTreeView(self)
        self.tree_view.setModel(self.model)
        self.tree_view.setRootIndex(self.model.index(self._current_path))

        # 隐藏不需要的列（只显示文件名）
        self.tree_view.setHeaderHidden(True)
        self.tree_view.hideColumn(1)  # Size
        self.tree_view.hideColumn(2)  # Type
        self.tree_view.hideColumn(3)  # Date Modified

        # 设置选择模式：ExtendedSelection 让 Cmd/Shift 多选生效，方便批量删除/复制
        self.tree_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        # 编辑触发：仅 F2 / 代码触发，避免双击/单击意外进入重命名（双击仍用于打开文件）
        self.tree_view.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)

        # 重命名时默认只选中"基名"，扩展名保留不选
        self.tree_view.setItemDelegate(_RenameNameOnlyDelegate(self.tree_view))

        # 设置动画效果
        self.tree_view.setAnimated(True)
        self.tree_view.setIndentation(16)

        # 允许拖拽
        self.tree_view.setDragEnabled(True)
        self.tree_view.setAcceptDrops(True)
        self.tree_view.setDropIndicatorShown(True)

        # 应用样式
        self._update_style()

        layout.addWidget(self.tree_view)

    def _connect_signals(self):
        """连接信号"""
        self.tree_view.doubleClicked.connect(self._on_double_click)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)

        # Cmd+C / Cmd+V — 当 tree_view 或其子项有焦点时触发
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self.tree_view)
        copy_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_sc.activated.connect(lambda: self._clipboard_copy_selection(None))

        paste_sc = QShortcut(QKeySequence.StandardKey.Paste, self.tree_view)
        paste_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        paste_sc.activated.connect(self._paste_via_shortcut)

    def _paste_via_shortcut(self):
        """Cmd+V：粘贴到当前选中项目录（或当前根目录）"""
        target = None
        for idx in self.tree_view.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            p = self.model.filePath(idx)
            if not p:
                continue
            target = p if os.path.isdir(p) else os.path.dirname(p)
            break
        if not target:
            target = self._current_path or os.path.expanduser("~")
        self._clipboard_paste_into(target)

    def _update_style(self):
        """更新样式"""
        bg_dark = self.theme.get('bg_dark', '#1a1a2e')
        bg_medium = self.theme.get('bg_medium', '#16213e')
        bg_hover = self.theme.get('bg_hover', '#4d4d6c')
        text = self.theme.get('text', '#eaeaea')
        text_dim = self.theme.get('text_dim', '#888888')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')

        self.setStyleSheet(f"""
            QWidget {{
                background-color: {bg_dark};
            }}
        """)

        self.tree_view.setStyleSheet(f"""
            QTreeView {{
                background-color: {bg_dark};
                color: {text};
                border: none;
                outline: none;
            }}
            QTreeView::item {{
                padding: 4px 8px;
                border: none;
            }}
            QTreeView::item:hover {{
                background-color: {bg_hover};
            }}
            QTreeView::item:selected {{
                background-color: {accent};
                color: white;
            }}
            /* 原地重命名时的编辑框 —— 显式控制配色和边距以避免文本被裁切 */
            QTreeView QLineEdit {{
                background-color: {bg_dark};
                color: {text};
                border: 1px solid {accent};
                padding: 0 4px;
                margin: 0;
                min-height: 18px;
                selection-background-color: {accent};
                selection-color: white;
            }}
            QTreeView::branch {{
                background-color: {bg_dark};
            }}
            QTreeView::branch:has-siblings:!adjoins-item {{
                border-image: none;
            }}
            QTreeView::branch:has-siblings:adjoins-item {{
                border-image: none;
            }}
            QTreeView::branch:!has-children:!has-siblings:adjoins-item {{
                border-image: none;
            }}
            QTreeView::branch:has-children:!has-siblings:closed,
            QTreeView::branch:closed:has-children:has-siblings {{
                border-image: none;
                image: url(none);
            }}
            QTreeView::branch:open:has-children:!has-siblings,
            QTreeView::branch:open:has-children:has-siblings {{
                border-image: none;
                image: url(none);
            }}
            QScrollBar:vertical {{
                background-color: {bg_dark};
                width: 10px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background-color: {border};
                border-radius: 5px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: {text_dim};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar:horizontal {{
                background-color: {bg_dark};
                height: 10px;
                border: none;
            }}
            QScrollBar::handle:horizontal {{
                background-color: {border};
                border-radius: 5px;
                min-width: 20px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background-color: {text_dim};
            }}
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {{
                width: 0px;
            }}
        """)

    def _handle_drop_copy(self, src_paths: list, target_dir: str):
        """把从其他面板/Finder 拖入的文件复制到 target_dir。

        - 已存在的目标默认询问覆盖
        - 目录递归复制
        """
        copied = 0
        errors = []
        skipped = 0
        for src in src_paths:
            try:
                if not os.path.exists(src):
                    errors.append(f"{os.path.basename(src)}: not found")
                    continue
                dst = os.path.join(target_dir, os.path.basename(src))
                if os.path.abspath(src) == os.path.abspath(dst):
                    skipped += 1
                    continue
                if os.path.exists(dst):
                    reply = QMessageBox.question(
                        self, t("explorer.overwrite_title"),
                        f"{os.path.basename(dst)} already exists in this folder. Overwrite?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        skipped += 1
                        continue
                    if os.path.isdir(dst) and not os.path.islink(dst):
                        shutil.rmtree(dst)
                    else:
                        os.remove(dst)
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                copied += 1
            except Exception as e:
                errors.append(f"{os.path.basename(src)}: {e}")
        if errors:
            QMessageBox.warning(self, "Copy failed", "\n".join(errors))
        # QFileSystemModel 监听文件系统变化，会自动刷新；保底再 refresh 一次
        self.refresh()

    def set_root_path(self, path: str):
        """设置根目录（路径未变时跳过，避免重复扫描目录）"""
        if path and os.path.isdir(path):
            if path == self._current_path and self.model.rootPath() == path:
                # 路径未变，无需重新加载
                return
            self._current_path = path
            self.model.setRootPath(path)
            self.tree_view.setRootIndex(self.model.index(path))
            # 切换了根 → 重置自动刷新基线
            self._auto_refresh_fingerprint = frozenset()

    def refresh(self):
        """刷新文件树"""
        current = self._current_path
        # 重新设置根路径以触发目录重新扫描
        self.model.setRootPath("")
        self.model.setRootPath(current)
        self.tree_view.setRootIndex(self.model.index(current))
        self._auto_refresh_fingerprint = frozenset()

    def _auto_refresh_tick(self):
        """60s 安全网：QFileSystemModel + FSEvents 已经处理大多数情况，
        这里只在当前根目录条目集合发生过变化时才真正刷一次。
        无变化 = 只做一次 os.listdir，几乎零开销。"""
        if not self.isVisible():
            return
        path = self._current_path
        if not path or not os.path.isdir(path):
            return
        try:
            names = frozenset(os.listdir(path))
        except OSError:
            return
        if names == self._auto_refresh_fingerprint:
            return
        # 第一次见这个目录 → 只建基线，不刷新（模型已经有数据了）
        if not self._auto_refresh_fingerprint:
            self._auto_refresh_fingerprint = names
            return
        # 有变化 → 保存当前选中再刷新，刷完恢复选中
        sel_path: Optional[str] = None
        idx = self.tree_view.currentIndex()
        if idx.isValid():
            sel_path = self.model.filePath(idx)
        self.refresh()
        self._auto_refresh_fingerprint = names
        if sel_path and os.path.exists(sel_path):
            QTimer.singleShot(50, lambda p=sel_path: self._reselect_path(p))

    def _reselect_path(self, path: str):
        idx = self.model.index(path)
        if idx.isValid():
            self.tree_view.setCurrentIndex(idx)
            self.tree_view.scrollTo(idx)

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()

    def apply_language(self):
        """应用语言设置（刷新可翻译的 UI 文本）

        ExplorerPanel 没有持久的文本标签（右键菜单每次创建时
        会调用 t() 获取最新翻译），因此此方法目前无需额外操作。
        保留此方法以便将来添加持久 UI 元素时使用。
        """
        pass

    def set_editing_file(self, file_path: str):
        """设置当前正在编辑的文件路径"""
        self._editing_file = file_path

    def clear_editing_file(self):
        """清除当前正在编辑的文件路径"""
        self._editing_file = None

    def _on_double_click(self, index: QModelIndex):
        """双击事件处理"""
        if not index.isValid():
            return

        file_path = self.model.filePath(index)
        if os.path.isfile(file_path):
            # 双击文件，发射信号请求在内置编辑器中打开
            self.file_double_clicked.emit(file_path)
            self.file_edit_requested.emit(file_path)

    def _open_file(self, file_path: str):
        """使用系统默认应用打开文件"""
        url = QUrl.fromLocalFile(file_path)
        QDesktopServices.openUrl(url)

    def _open_for_editing(self, file_path: str):
        """使用编辑器打开文件进行查看/编辑"""
        # 尝试使用 VS Code 或 Cursor 打开
        if sys.platform == 'darwin':
            # macOS 上优先尝试 Cursor，然后 VS Code
            editors_to_try = [
                ('/Applications/Cursor.app/Contents/Resources/app/bin/cursor', 'cursor'),
                ('/usr/local/bin/cursor', 'cursor'),
                ('/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code', 'code'),
                ('/usr/local/bin/code', 'code'),
            ]
            for editor_path, _ in editors_to_try:
                if editor_path and os.path.exists(editor_path):
                    try:
                        subprocess.Popen([editor_path, file_path])
                        return
                    except Exception:
                        continue
            # 尝试 which 查找
            for cmd in ['cursor', 'code']:
                cmd_path = shutil.which(cmd)
                if cmd_path:
                    try:
                        subprocess.Popen([cmd_path, file_path])
                        return
                    except Exception:
                        continue
        else:
            # 其他平台尝试 cursor 或 code 命令
            for cmd in ['cursor', 'code']:
                cmd_path = shutil.which(cmd)
                if cmd_path:
                    try:
                        subprocess.Popen([cmd_path, file_path])
                        return
                    except Exception:
                        continue
        # 如果都失败了，使用系统默认应用
        self._open_file(file_path)

    def _run_file(self, file_path: str):
        """运行文件"""
        # 获取文件扩展名
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        try:
            if ext == '.py':
                # Python 文件
                if sys.platform == 'darwin':
                    # macOS: 在 Terminal 中运行
                    script = f'''
                    tell application "Terminal"
                        activate
                        do script "cd \\"{os.path.dirname(file_path)}\\" && python3 \\"{file_path}\\""
                    end tell
                    '''
                    subprocess.Popen(['osascript', '-e', script])
                elif sys.platform == 'win32':
                    subprocess.Popen(['python', file_path], cwd=os.path.dirname(file_path),
                                   creationflags=subprocess.CREATE_NEW_CONSOLE)
                else:
                    subprocess.Popen(['x-terminal-emulator', '-e', f'python3 "{file_path}"'],
                                   cwd=os.path.dirname(file_path))
            elif ext in ['.sh', '.bash']:
                # Shell 脚本
                if sys.platform == 'darwin':
                    script = f'''
                    tell application "Terminal"
                        activate
                        do script "cd \\"{os.path.dirname(file_path)}\\" && bash \\"{file_path}\\""
                    end tell
                    '''
                    subprocess.Popen(['osascript', '-e', script])
                elif sys.platform != 'win32':
                    subprocess.Popen(['x-terminal-emulator', '-e', f'bash "{file_path}"'],
                                   cwd=os.path.dirname(file_path))
            elif ext in ['.js', '.mjs']:
                # JavaScript/Node.js 文件
                if sys.platform == 'darwin':
                    script = f'''
                    tell application "Terminal"
                        activate
                        do script "cd \\"{os.path.dirname(file_path)}\\" && node \\"{file_path}\\""
                    end tell
                    '''
                    subprocess.Popen(['osascript', '-e', script])
                elif sys.platform == 'win32':
                    subprocess.Popen(['node', file_path], cwd=os.path.dirname(file_path),
                                   creationflags=subprocess.CREATE_NEW_CONSOLE)
                else:
                    subprocess.Popen(['x-terminal-emulator', '-e', f'node "{file_path}"'],
                                   cwd=os.path.dirname(file_path))
            else:
                # 其他文件使用系统默认应用运行
                self._open_file(file_path)
        except Exception as e:
            QMessageBox.warning(self, t("explorer.error"), t("explorer.run_failed", error=e))

    def _show_context_menu(self, position):
        """显示右键菜单"""
        index = self.tree_view.indexAt(position)

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {self.theme.get('border', '#3d3d5c')};
                margin: 4px 10px;
            }}
        """)

        if index.isValid():
            file_path = self.model.filePath(index)
            is_dir = os.path.isdir(file_path)

            # 打开（编辑）
            if not is_dir:
                open_action = menu.addAction(t("explorer.open"))
                open_action.triggered.connect(lambda: self._open_for_editing(file_path))

                # 运行（针对可执行脚本）
                _, ext = os.path.splitext(file_path)
                ext = ext.lower()
                if ext in ['.py', '.sh', '.bash', '.js', '.mjs']:
                    run_action = menu.addAction(t("explorer.run"))
                    run_action.triggered.connect(lambda: self._run_file(file_path))

                # 保存选项（仅当文件正在编辑时显示）
                if self._editing_file and os.path.normpath(file_path) == os.path.normpath(self._editing_file):
                    menu.addSeparator()
                    save_action = menu.addAction(t("explorer.save"))
                    save_action.triggered.connect(self.save_file_requested.emit)

                    save_as_action = menu.addAction(t("explorer.save_as"))
                    save_as_action.triggered.connect(self.save_file_as_requested.emit)

            menu.addSeparator()

            # 新建文件
            new_file_action = menu.addAction(t("explorer.new_file"))
            target_dir = file_path if is_dir else os.path.dirname(file_path)
            new_file_action.triggered.connect(lambda: self._new_file(target_dir))

            # 新建文件夹
            new_folder_action = menu.addAction(t("explorer.new_folder"))
            new_folder_action.triggered.connect(lambda: self._new_folder(target_dir))

            menu.addSeparator()

            # 重命名
            rename_action = menu.addAction(t("explorer.rename"))
            rename_action.triggered.connect(lambda: self._rename_item(file_path))

            # 删除：如果右键的对象在当前选中里，则删除整批选中；否则只删该项
            delete_paths = self._selection_paths_including(file_path)
            delete_action = menu.addAction(t("explorer.delete"))
            delete_action.triggered.connect(lambda paths=delete_paths: self._delete_paths(paths))

            menu.addSeparator()

            # 复制 / 粘贴（跨面板、跨窗口）
            copy_action = menu.addAction(t("explorer.copy"))
            copy_action.triggered.connect(lambda: self._clipboard_copy_selection(file_path))

            paste_target = file_path if is_dir else os.path.dirname(file_path)
            if explorer_clipboard.has_pastable():
                paste_action = menu.addAction(
                    t("explorer.paste_with_label", label=explorer_clipboard.describe())
                )
                paste_action.triggered.connect(lambda: self._clipboard_paste_into(paste_target))

            menu.addSeparator()

            # 复制路径
            copy_path_action = menu.addAction(t("explorer.copy_path"))
            copy_path_action.triggered.connect(lambda: self._copy_path(file_path))

            # 复制相对路径
            copy_rel_path_action = menu.addAction(t("explorer.copy_relative_path"))
            copy_rel_path_action.triggered.connect(lambda: self._copy_relative_path(file_path))

            menu.addSeparator()

            # 在 Finder 中显示
            if sys.platform == 'darwin':
                reveal_action = menu.addAction(t("explorer.reveal_in_finder"))
                reveal_action.triggered.connect(lambda: self._reveal_in_finder(file_path))
            elif sys.platform == 'win32':
                reveal_action = menu.addAction(t("explorer.reveal_in_explorer"))
                reveal_action.triggered.connect(lambda: self._reveal_in_explorer(file_path))
            else:
                reveal_action = menu.addAction(t("explorer.reveal_in_file_manager"))
                reveal_action.triggered.connect(lambda: self._reveal_in_file_manager(file_path))

            menu.addSeparator()

            # 在编辑器中打开
            vscode_action = menu.addAction(t("explorer.open_in_vscode"))
            vscode_action.triggered.connect(lambda: self._open_in_editor(file_path, 'code'))

            cursor_action = menu.addAction(t("explorer.open_in_cursor"))
            cursor_action.triggered.connect(lambda: self._open_in_editor(file_path, 'cursor'))
        else:
            # 点击空白区域
            new_file_action = menu.addAction(t("explorer.new_file"))
            new_file_action.triggered.connect(lambda: self._new_file(self._current_path))

            new_folder_action = menu.addAction(t("explorer.new_folder"))
            new_folder_action.triggered.connect(lambda: self._new_folder(self._current_path))

            if explorer_clipboard.has_pastable():
                menu.addSeparator()
                paste_action = menu.addAction(
                    t("explorer.paste_with_label", label=explorer_clipboard.describe())
                )
                paste_action.triggered.connect(
                    lambda: self._clipboard_paste_into(self._current_path)
                )

            menu.addSeparator()

            copy_current_path_action = menu.addAction(t("explorer.copy_current_path"))
            copy_current_path_action.triggered.connect(
                lambda: self._copy_path(self._current_path)
            )

            open_folder_action = menu.addAction(t("explorer.open_current_folder"))
            open_folder_action.triggered.connect(
                lambda: self._open_folder(self._current_path)
            )

            menu.addSeparator()

            refresh_action = menu.addAction(t("explorer.refresh"))
            refresh_action.triggered.connect(self.refresh)

        menu.exec(QCursor.pos())

    def _new_file(self, target_dir: str):
        """创建新文件并直接在文件树里原地重命名（不弹窗）"""
        file_path = self._unique_new_path(target_dir, "untitled", ".txt")
        try:
            Path(file_path).touch()
        except Exception as e:
            QMessageBox.warning(self, t("explorer.error"), t("explorer.create_file_failed", error=e))
            return
        self._begin_inline_edit_for_new(target_dir, file_path)

    def _new_folder(self, target_dir: str):
        """创建新文件夹并直接在文件树里原地重命名（不弹窗）"""
        folder_path = self._unique_new_path(target_dir, t("explorer.default_folder_name"), "")
        try:
            os.makedirs(folder_path, exist_ok=False)
        except Exception as e:
            QMessageBox.warning(self, t("explorer.error"), t("explorer.create_folder_failed", error=e))
            return
        self._begin_inline_edit_for_new(target_dir, folder_path)

    @staticmethod
    def _unique_new_path(target_dir: str, base: str, ext: str) -> str:
        """在 target_dir 里生成一个不冲突的路径：base+ext，已存在则 base2/base3…+ext"""
        candidate = os.path.join(target_dir, base + ext)
        if not os.path.exists(candidate):
            return candidate
        i = 2
        while True:
            candidate = os.path.join(target_dir, f"{base}{i}{ext}")
            if not os.path.exists(candidate):
                return candidate
            i += 1

    def _begin_inline_edit_for_new(self, target_dir: str, new_path: str, _attempts: int = 0):
        """新建后进入原地重命名。QFileSystemModel 装载目录是异步的，所以这里轮询
        直到新条目在视图里排好版、编辑框真的打开为止。"""
        # 在子目录里新建 → 先展开父目录，新条目才会出现在视图里
        if os.path.normpath(target_dir) != os.path.normpath(self._current_path):
            parent_idx = self.model.index(target_dir)
            if parent_idx.isValid():
                self.tree_view.expand(parent_idx)
        idx = self.model.index(new_path)
        if idx.isValid():
            self.tree_view.setCurrentIndex(idx)
            self.tree_view.scrollTo(idx)
            # 仅当条目已在视图里排好版（visualRect 非空）才调用 edit，
            # 否则 edit 会失败并打日志，留给下一轮重试
            if not self.tree_view.visualRect(idx).isEmpty():
                self.tree_view.edit(idx)
                if self.tree_view.state() == QAbstractItemView.State.EditingState:
                    self._select_basename_in_editor()
                    return
        if _attempts < 60:
            QTimer.singleShot(
                25, lambda: self._begin_inline_edit_for_new(target_dir, new_path, _attempts + 1)
            )

    def _select_basename_in_editor(self):
        """编辑框打开后，文件选中主名（不含扩展名），文件夹/无扩展名则全选 —— 仿 Finder。"""
        editor = QApplication.focusWidget()
        if not isinstance(editor, QLineEdit):
            return
        name = editor.text()
        base, ext = os.path.splitext(name)
        if ext and base:
            editor.setSelection(0, len(base))
        else:
            editor.selectAll()

    def _rename_item(self, file_path: str):
        """在文件树中原地重命名文件/文件夹（不弹窗）"""
        idx = self.model.index(file_path)
        if not idx.isValid():
            return
        self.tree_view.setCurrentIndex(idx)
        self.tree_view.scrollTo(idx)
        self.tree_view.edit(idx)

    def _selection_paths_including(self, anchor_path: str) -> list[str]:
        """右键菜单使用：如果 anchor_path 在当前选中里，返回所有选中路径；
        否则只返回 anchor_path 自己（用户点了一个未选中的项）。"""
        sel = self.tree_view.selectionModel()
        if sel is None:
            return [anchor_path]
        paths: list[str] = []
        seen = set()
        anchor_abs = os.path.abspath(anchor_path) if anchor_path else None
        anchor_in_selection = False
        for idx in sel.selectedIndexes():
            if idx.column() != 0:
                continue
            p = self.model.filePath(idx)
            if not p:
                continue
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            seen.add(ap)
            paths.append(p)
            if anchor_abs and ap == anchor_abs:
                anchor_in_selection = True
        if anchor_in_selection and paths:
            return paths
        return [anchor_path]

    def _delete_item(self, file_path: str):
        """单项删除（保留以兼容已有调用方），转发到多项删除"""
        self._delete_paths([file_path])

    def _delete_paths(self, paths: list[str]):
        """删除一个或多个本地文件/文件夹，统一确认 + 批量执行。"""
        # 去重并过滤掉不存在的
        seen = set()
        valid: list[str] = []
        for p in paths:
            if not p:
                continue
            ap = os.path.abspath(p)
            if ap in seen or not os.path.exists(p):
                continue
            seen.add(ap)
            valid.append(p)
        if not valid:
            return

        if len(valid) == 1:
            p = valid[0]
            is_dir = os.path.isdir(p)
            item_type = t("explorer.folder") if is_dir else t("explorer.file")
            msg = t("explorer.confirm_delete_msg",
                    type=item_type, name=os.path.basename(p))
        else:
            msg = t("explorer.confirm_delete_many_msg", count=len(valid))

        reply = QMessageBox.question(
            self, t("explorer.confirm_delete_title"), msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        errors: list[str] = []
        for p in valid:
            try:
                self._send_to_trash(p)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")

        self.refresh()
        if errors:
            QMessageBox.warning(
                self, t("explorer.error"),
                t("explorer.delete_failed", error="\n".join(errors)),
            )

    def _send_to_trash(self, file_path: str):
        """把单个本地路径移到回收站（平台分发）；失败时回退到永久删除。"""
        is_dir = os.path.isdir(file_path)
        if sys.platform == 'darwin':
            subprocess.run(
                ['osascript', '-e',
                 f'tell app "Finder" to delete POSIX file "{file_path}"'],
                check=True, capture_output=True,
            )
        elif sys.platform == 'win32':
            try:
                from send2trash import send2trash
                send2trash(file_path)
            except ImportError:
                if is_dir:
                    shutil.rmtree(file_path)
                else:
                    os.remove(file_path)
        else:
            result = subprocess.run(['gio', 'trash', file_path], capture_output=True)
            if result.returncode != 0:
                if is_dir:
                    shutil.rmtree(file_path)
                else:
                    os.remove(file_path)

    # ---------- 跨面板复制 / 粘贴 ----------

    def _clipboard_copy_selection(self, fallback_path: str = None):
        """把当前选中的文件 / 文件夹放入跨面板剪贴板（同时写到系统剪贴板）。

        若 fallback_path 不在选中范围内，则只复制 fallback_path（右键单项）。
        """
        indexes = self.tree_view.selectionModel().selectedIndexes()
        paths: list[str] = []
        seen = set()
        for idx in indexes:
            if idx.column() != 0:
                continue
            p = self.model.filePath(idx)
            if p and p not in seen:
                seen.add(p)
                paths.append(p)
        if fallback_path and fallback_path not in seen:
            paths = [fallback_path]
        if not paths:
            return
        explorer_clipboard.set_items(
            [("local", p) for p in paths],
            push_local_paths=paths,
        )

    def _clipboard_paste_into(self, target_dir: str):
        """把剪贴板里的项目粘贴到 target_dir。

        语义（与 Finder/VS Code 风格一致）：
        - 源文件夹 == 目标文件夹 → 自动加 "(N)" 序号尾缀，不弹窗。
        - 跨文件夹 / 跨面板冲突 → 弹一次窗，三选一：覆盖 / 保留二者（加尾缀）/ 取消，
          可勾选 "对剩余冲突应用相同操作" 一次性处理多个文件。
        """
        items = explorer_clipboard.effective_items()
        if not items or not target_dir:
            return
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.warning(self, t("explorer.error"),
                                t("explorer.paste_failed", error=e))
            return

        target_abs = os.path.abspath(target_dir)
        errors: list[str] = []
        # 跨文件夹冲突时记住用户的选择
        sticky_decision: Optional[str] = None  # 'overwrite' / 'keep' / None
        cancel_all = False

        def local_name_exists(name: str) -> bool:
            return os.path.exists(os.path.join(target_dir, name))

        for it in items:
            if cancel_all:
                break
            kind = it[0]
            try:
                if kind == "local":
                    src = it[1]
                    name = os.path.basename(src.rstrip("/"))
                    src_dir_abs = os.path.abspath(os.path.dirname(src.rstrip("/")))
                    same_folder = (src_dir_abs == target_abs)
                    dst = os.path.join(target_dir, name)
                    # 同源同目标：原地复制 → 自动 (N) 后缀，绝不弹窗
                    if same_folder:
                        if os.path.exists(dst):
                            name = explorer_clipboard.next_free_name(name, local_name_exists)
                            dst = os.path.join(target_dir, name)
                    elif os.path.exists(dst):
                        # 跨文件夹冲突：问一次
                        decision = self._resolve_paste_conflict(name, sticky_decision)
                        if decision is None:
                            cancel_all = True
                            break
                        # decision == ('overwrite'|'keep', sticky)
                        action, sticky = decision
                        if sticky:
                            sticky_decision = action
                        if action == "overwrite":
                            if os.path.isdir(dst) and not os.path.islink(dst):
                                shutil.rmtree(dst)
                            else:
                                os.remove(dst)
                        else:  # keep
                            name = explorer_clipboard.next_free_name(name, local_name_exists)
                            dst = os.path.join(target_dir, name)
                    if os.path.isdir(src) and not os.path.islink(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)

                elif kind == "remote":
                    _, host_alias, remote_path, session = it
                    if session is None or not session.is_connected():
                        errors.append(f"{remote_path}: {t('remote.session_lost')}")
                        continue
                    name = posixpath.basename(remote_path.rstrip("/")) or host_alias
                    dst = os.path.join(target_dir, name)
                    # 远端 → 本地，永远算 "跨文件夹"（不同存储）
                    if os.path.exists(dst):
                        decision = self._resolve_paste_conflict(name, sticky_decision)
                        if decision is None:
                            cancel_all = True
                            break
                        action, sticky = decision
                        if sticky:
                            sticky_decision = action
                        if action == "overwrite":
                            if os.path.isdir(dst) and not os.path.islink(dst):
                                shutil.rmtree(dst)
                            else:
                                os.remove(dst)
                        else:  # keep
                            name = explorer_clipboard.next_free_name(name, local_name_exists)
                            dst = os.path.join(target_dir, name)
                    self._download_remote_recursive(session, remote_path, dst)
                else:
                    errors.append(f"Unknown clipboard item: {it!r}")
            except Exception as e:
                errors.append(f"{it}: {e}")

        if errors:
            QMessageBox.warning(self, t("explorer.error"), "\n".join(errors))
        self.refresh()

    def _resolve_paste_conflict(self, name: str, sticky: Optional[str]):
        """跨文件夹冲突时的三选一对话框。

        Returns:
            ('overwrite', sticky_bool) — 覆盖
            ('keep',      sticky_bool) — 保留二者（加 (N) 尾缀）
            None                        — 取消，中止剩余粘贴
        sticky 已有值时直接复用，不再弹窗。
        """
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

    def _download_remote_recursive(self, session, remote_path: str, local_path: str):
        """通过 SSH session 把远程文件 / 目录递归下载到 local_path（阻塞，含进度对话框）"""
        # 先 stat 看是不是目录
        fut_stat = session.submit(session.stat, remote_path)
        entry = fut_stat.result()
        if not entry.is_dir:
            fut = session.submit(session.download, remote_path, local_path)
            self._wait_future_with_progress(
                [fut], t("explorer.pasting"),
            )
            return
        # 递归：先 mkdir，再列出，再为每个子项递归
        os.makedirs(local_path, exist_ok=True)
        fut_list = session.submit(session.listdir, remote_path)
        children = fut_list.result()
        # 一层一层下载，文件并发用同一 session executor 排队即可
        for child in children:
            child_local = os.path.join(local_path, child.name)
            if child.is_dir and not child.is_link:
                self._download_remote_recursive(session, child.path, child_local)
            else:
                fut = session.submit(session.download, child.path, child_local)
                self._wait_future_with_progress([fut], t("explorer.pasting"))

    def _wait_future_with_progress(self, futures: list, label: str):
        """阻塞等待 futures，但跑一个事件循环让 UI 不卡。"""
        if not futures:
            return
        progress = QProgressDialog(label, None, 0, len(futures), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        progress.setValue(0)
        done = {"n": 0, "errors": []}

        def make_cb(_):
            def cb(f):
                try:
                    f.result()
                except Exception as e:
                    done["errors"].append(str(e))
                done["n"] += 1
            return cb

        for fut in futures:
            fut.add_done_callback(make_cb(fut))

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
        if done["errors"]:
            raise RuntimeError("; ".join(done["errors"]))

    def _copy_path(self, file_path: str):
        """复制完整路径到剪贴板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(file_path)

    def _copy_relative_path(self, file_path: str):
        """复制相对路径到剪贴板"""
        try:
            rel_path = os.path.relpath(file_path, self._current_path)
            clipboard = QApplication.clipboard()
            clipboard.setText(rel_path)
        except ValueError:
            # 不同驱动器时，使用完整路径
            self._copy_path(file_path)

    def _reveal_in_finder(self, file_path: str):
        """在 Finder 中显示（macOS）"""
        subprocess.run(['open', '-R', file_path])

    def _reveal_in_explorer(self, file_path: str):
        """在资源管理器中显示（Windows）"""
        subprocess.run(['explorer', '/select,', file_path.replace('/', '\\')])

    def _reveal_in_file_manager(self, file_path: str):
        """在文件管理器中显示（Linux）"""
        folder = file_path if os.path.isdir(file_path) else os.path.dirname(file_path)
        subprocess.run(['xdg-open', folder])

    def _open_folder(self, folder_path: str):
        """在系统文件管理器中打开文件夹（不是 reveal/select 在父级里）"""
        if not os.path.isdir(folder_path):
            folder_path = os.path.dirname(folder_path)
        if sys.platform == 'darwin':
            subprocess.run(['open', folder_path])
        elif sys.platform == 'win32':
            subprocess.run(['explorer', folder_path.replace('/', '\\')])
        else:
            subprocess.run(['xdg-open', folder_path])

    def _open_in_editor(self, file_path: str, editor: str):
        """在指定编辑器中打开"""
        try:
            if editor == 'code':
                # VS Code
                if sys.platform == 'darwin':
                    # macOS 上尝试多种路径
                    paths_to_try = [
                        '/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code',
                        '/usr/local/bin/code',
                        shutil.which('code')
                    ]
                    for code_path in paths_to_try:
                        if code_path and os.path.exists(code_path):
                            subprocess.Popen([code_path, file_path])
                            return
                else:
                    subprocess.Popen(['code', file_path])
            elif editor == 'cursor':
                # Cursor
                if sys.platform == 'darwin':
                    paths_to_try = [
                        '/Applications/Cursor.app/Contents/Resources/app/bin/cursor',
                        '/usr/local/bin/cursor',
                        shutil.which('cursor')
                    ]
                    for cursor_path in paths_to_try:
                        if cursor_path and os.path.exists(cursor_path):
                            subprocess.Popen([cursor_path, file_path])
                            return
                else:
                    subprocess.Popen(['cursor', file_path])
        except Exception as e:
            QMessageBox.warning(self, t("explorer.error"), t("explorer.open_in_editor_failed", editor=editor, error=e))
