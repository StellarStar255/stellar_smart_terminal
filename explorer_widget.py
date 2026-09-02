"""
Explorer 文件浏览器面板
提供类似 Cursor/VS Code 的文件浏览器功能
"""
import os
import posixpath
import sys
import re
import subprocess
import shutil
import shlex
import threading
from pathlib import Path
from typing import Optional

from i18n import t
import explorer_clipboard
import explorer_common
import explorer_favorites
from utils import parse_search_tokens, name_matches_tokens
from app_logging import get_logger

logger = get_logger(__name__)
import app_config

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QTreeView, QMenu, QLineEdit, QMessageBox,
    QAbstractItemView, QFileDialog, QApplication, QProgressDialog,
    QStyledItemDelegate, QFileIconProvider, QListWidget, QListWidgetItem,
    QToolButton,
)
from PyQt6.QtCore import (
    Qt, QDir, QModelIndex, QPersistentModelIndex, pyqtSignal, QTimer,
    QEventLoop, QSize, QFileInfo, QSortFilterProxyModel,
)
from PyQt6.QtGui import (
    QFileSystemModel, QAction, QDesktopServices, QCursor,
    QShortcut, QKeySequence, QColor, QBrush,
    QIcon, QPixmap, QPainter, QPen,
)
from PyQt6.QtCore import QUrl
from PyQt6 import sip  # 用于检查 C++ 对象是否已被删除


class _LocalDropTreeView(QTreeView):
    """支持把 file:// URL 拖入并复制到落点目录的本地文件树

    与 QFileSystemModel 默认的"移动"语义不同：这里所有外部 URL 都做 **复制**，
    这样跨窗口/跨远程往本地拖文件不会破坏源。
    """

    def __init__(self, panel):
        super().__init__()
        self._panel = panel
        self._indent_guide_color = None   # 由 _update_style 按主题设定
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QTreeView.DragDropMode.DragDrop)

        # Finder 风格：已选中的条目再次单击 → 延迟进入原地重命名
        self._pending_rename_index = None  # QPersistentModelIndex 或 None
        self._rename_timer = QTimer(self)
        self._rename_timer.setSingleShot(True)
        self._rename_timer.timeout.connect(self._fire_pending_rename)

    def set_indent_guide_color(self, color):
        """设置缩进参考线颜色（主题切换时重设；None = 不画）。"""
        self._indent_guide_color = color
        self.viewport().update()

    def drawRow(self, painter, option, index):   # noqa: N802 — Qt 回调
        """在每层缩进处画一条竖线。

        深层目录光靠缩进量很难看出"这几个文件是同一层"，一眼望去就是一片
        阶梯。参考线是 VS Code / IDEA 同款解法，成本只有一次画线。
        """
        super().drawRow(painter, option, index)
        color = getattr(self, "_indent_guide_color", None)
        if color is None:
            return
        xs = self.indent_guide_xs(index, option.rect.left())
        if not xs:
            return
        rect = option.rect
        painter.save()
        pen = QPen(color)
        pen.setWidth(1)
        painter.setPen(pen)
        for x in xs:
            painter.drawLine(x, rect.top(), x, rect.bottom())
        painter.restore()

    def indent_guide_xs(self, index, left: int = 0) -> list:
        """这一行要在哪些 x 上画参考线（每个祖先层一条）。

        抽成纯计算是为了可测：直接测 drawRow 就得给 QTreeView.drawRow 打
        类级 monkeypatch，那会污染 sip 的方法表，后面任何用到嵌套事件循环
        的用例都会莫名 abort（实测踩过）。
        """
        depth = 0
        parent = index.parent()
        root = self.rootIndex()
        while parent.isValid() and parent != root:
            depth += 1
            parent = parent.parent()
        indent = self.indentation()
        return [left + indent * level + indent // 2 for level in range(depth)]

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
                        # 延迟到下一个事件循环再进入编辑，不在 keyPressEvent 内同步 edit()。
                        # Linux 上同步开编辑器时，刚弹出的 QLineEdit 会被“同一次回车事件继续
                        # 派发/键释放”立刻提交关闭，表现为「文件夹/zip 等回车没反应」；macOS
                        # 恰好同步也能用。改为延迟对文件、文件夹、任意扩展名都一致生效。
                        self._pending_rename_index = QPersistentModelIndex(idx)
                        self._rename_timer.start(0)
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


class _FastIconProvider(QFileIconProvider):
    """轻量图标提供器：只用通用「文件夹 / 文件」两种图标。

    默认的 QFileIconProvider 会对每个文件按类型走 macOS NSWorkspace 查询系统图标，
    几十个文件的目录展开时，这些查询全在 UI 线程同步发生 → 卡顿主因。
    这里把图标收敛成两种、各只解析一次并缓存，大目录展开/收起即时完成。
    """

    def __init__(self):
        super().__init__()
        self._folder_icon = None
        self._file_icon = None

    def icon(self, arg):
        # icon() 有两个重载：icon(IconType) 与 icon(QFileInfo)。
        # 只拦截按文件信息取图标的热路径，其余仍交给基类。
        if isinstance(arg, QFileInfo):
            if arg.isDir():
                if self._folder_icon is None:
                    self._folder_icon = super().icon(QFileIconProvider.IconType.Folder)
                return self._folder_icon
            if self._file_icon is None:
                self._file_icon = super().icon(QFileIconProvider.IconType.File)
            return self._file_icon
        return super().icon(arg)


class FilteredFileSystemModel(QFileSystemModel):
    """文件系统模型：隐藏文件/文件夹（以点开头）以浅灰色显示"""

    # 隐藏条目的前景色（浅灰，半透明感）
    HIDDEN_COLOR = QColor("#888888")
    # 隐藏条目图标的不透明度
    HIDDEN_ICON_OPACITY = 0.45

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dim_icon_cache = {}  # 原图标 cacheKey -> 变淡后的 QIcon

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        # 只有这两个角色才需要判断「是否隐藏文件」，避免在热路径（DisplayRole）
        # 上对每个单元格都调用一次 fileName()。
        if role == Qt.ItemDataRole.ForegroundRole or role == Qt.ItemDataRole.DecorationRole:
            if index.isValid() and self.fileName(index).startswith('.'):
                if role == Qt.ItemDataRole.ForegroundRole:
                    return QBrush(self.HIDDEN_COLOR)
                icon = super().data(index, role)
                if isinstance(icon, QIcon):
                    return self._dimmed_icon(icon)
        return super().data(index, role)

    def _dimmed_icon(self, icon: QIcon) -> QIcon:
        """返回降低不透明度后的图标（带缓存）。

        关键点：要为「所有可用尺寸」分别生成变淡后的位图，并保留每个位图的
        devicePixelRatio。否则在 Retina 屏上图标会被当成更小/更模糊的一档来
        渲染，看起来就比普通文件的图标小一圈。
        """
        key = icon.cacheKey()
        cached = self._dim_icon_cache.get(key)
        if cached is not None:
            return cached

        result = QIcon()
        for size in (icon.availableSizes() or [QSize(16, 16)]):
            src = icon.pixmap(size)
            if src.isNull():
                continue
            dimmed = QPixmap(src.size())
            dimmed.setDevicePixelRatio(src.devicePixelRatio())
            dimmed.fill(Qt.GlobalColor.transparent)
            painter = QPainter(dimmed)
            painter.setOpacity(self.HIDDEN_ICON_OPACITY)
            painter.drawPixmap(0, 0, src)
            painter.end()
            result.addPixmap(dimmed)

        if result.isNull():
            return icon

        self._dim_icon_cache[key] = result
        return result


class _DotFileProxy(QSortFilterProxyModel):
    """在 Windows 上过滤以 '.' 开头的隐藏文件/文件夹。

    QDir.Filter.Hidden 只识别 Windows 隐藏属性，不识别 Unix 风格的
    dot-prefix 约定。此代理补充了 dot-prefix 过滤。
    """

    # 系统/工具产生的垃圾条目：跟"隐藏文件"是两回事 —— 用户开着"显示隐藏
    # 文件"是为了看 .gitignore、.env 这类真内容，不是为了看每个目录里的
    # .DS_Store。所以这份名单独立过滤。
    _JUNK_NAMES = frozenset({
        '.DS_Store', '.AppleDouble', '.LSOverride', '.Spotlight-V100',
        '.Trashes', '.fseventsd', '.DocumentRevisions-V100', '.TemporaryItems',
        'Thumbs.db', 'ehthumbs.db', 'desktop.ini', '__pycache__',
    })
    _JUNK_SUFFIXES = ('.pyc', '.pyo')

    @classmethod
    def is_junk(cls, name: str) -> bool:
        if name in cls._JUNK_NAMES:
            return True
        if name.startswith('._'):        # macOS AppleDouble 伴随文件
            return True
        return name.endswith(cls._JUNK_SUFFIXES)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hide_dot = False
        self._hide_junk = True

    def set_hide_dot_files(self, hide: bool):
        if self._hide_dot != hide:
            self._hide_dot = hide
            self.invalidateFilter()

    def set_hide_junk(self, hide: bool):
        if self._hide_junk != bool(hide):
            self._hide_junk = bool(hide)
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        idx = model.index(source_row, 0, source_parent)
        name = model.fileName(idx)
        if self._hide_junk and self.is_junk(name):
            return False
        if self._hide_dot and name.startswith('.'):
            return False
        return True

    # --- 便捷方法：透明代理到 QFileSystemModel，自动做 index 映射 ---

    def filePath(self, proxy_index):
        return self.sourceModel().filePath(self.mapToSource(proxy_index))

    def fileName(self, proxy_index):
        return self.sourceModel().fileName(self.mapToSource(proxy_index))


class ExplorerPanel(QWidget, explorer_common.TransferJobHost):
    """Explorer 文件浏览器面板"""

    # 信号
    file_double_clicked = pyqtSignal(str)  # 文件双击信号
    # 请求在内置编辑器中打开文件；第二个参数是跳转行号（0 = 不跳转）
    file_edit_requested = pyqtSignal(str, int)
    save_file_requested = pyqtSignal()  # 请求保存当前编辑的文件
    save_file_as_requested = pyqtSignal()  # 请求另存为当前编辑的文件
    favorites_changed = pyqtSignal()  # 快捷方式（收藏）增删后通知外部刷新 ★ 菜单
    # 后台搜索结果回 UI 线程：(generation, items, truncated)
    _search_result_signal = pyqtSignal(int, list, bool)

    # 搜索上限：命中数 / 已扫描条目数（防止超大目录把内存/时间打满）
    _SEARCH_MAX_RESULTS = 2000
    _SEARCH_MAX_SCANNED = 200000

    # 始终不递归进入的大型/缓存/构建目录（文件名与内容搜索共用）。
    # 关键：即便用户开了"显示隐藏文件"，也不应该走进 .git 对象库或 Maven 本地仓库
    # (.m2，常有十万级文件)，否则会把扫描预算耗光、看起来"点了搜索没反应"。
    _SEARCH_SKIP_DIRS = frozenset({
        '.git', '.hg', '.svn', '.m2', '.gradle', 'node_modules', 'target',
        'build', 'dist', '.next', 'out', '.venv', 'venv', '__pycache__',
        '.idea', '.vscode', 'Pods', '.tox', '.mypy_cache', '.pytest_cache',
        '.cache', '.gradle',
    })

    # 内容搜索（纯 Python 回退路径用）：跳过的二进制扩展名、单文件大小上限、
    # 单文件最多取的命中行数、命中行预览长度、最多扫描的文件数（防失控）。
    _CONTENT_SKIP_EXT = frozenset({
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.icns', '.webp', '.tiff',
        '.pdf', '.zip', '.gz', '.tar', '.bz2', '.xz', '.7z', '.rar',
        '.dmg', '.pkg', '.iso', '.exe', '.dll', '.so', '.dylib', '.bin',
        '.o', '.a', '.lib', '.class', '.pyc', '.pyo', '.jar',
        '.mp3', '.mp4', '.mov', '.avi', '.mkv', '.flv', '.wav', '.flac', '.aac',
        '.woff', '.woff2', '.ttf', '.otf', '.eot', '.psd', '.sketch', '.svg',
    })
    _CONTENT_MAX_FILE_BYTES = 2 * 1024 * 1024  # 单文件超过 2MB 跳过
    _CONTENT_MAX_PER_FILE = 50  # 单文件最多展示的命中行数
    _CONTENT_PREVIEW_LEN = 200  # 命中行预览最大字符数
    _CONTENT_MAX_FILES = 60000  # 纯 Python 内容搜索最多读取的文件数

    # 配置文件里"是否显示隐藏文件"的键名（与 main_window 共用 .smart_terminal_config.json）
    CONFIG_KEY_SHOW_HIDDEN = 'explorer_show_hidden'
    # 是否隐藏 .DS_Store / __pycache__ 这类系统垃圾（默认隐藏）
    CONFIG_KEY_HIDE_JUNK = 'explorer_hide_junk'
    # 双击目录是"进入那一层（换根）"还是"就地展开"。默认展开 —— 换根是
    # 改变浏览方式的大动作，得用户自己选。
    CONFIG_KEY_DBLCLICK_ENTER = 'explorer_double_click_enter'
    # 排序方式 / 升降序的键名
    CONFIG_KEY_SORT_KEY = 'explorer_sort_key'
    CONFIG_KEY_SORT_DESC = 'explorer_sort_desc'

    # 排序键 -> QFileSystemModel 列号（0 名称 / 1 大小 / 2 类型 / 3 修改日期）
    _SORT_COLUMN = {'name': 0, 'size': 1, 'type': 2, 'modified': 3}

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._current_path = os.path.expanduser("~")
        self._editing_file = None  # 当前正在编辑的文件路径
        # 是否显示隐藏文件（以点开头），从配置读取，默认显示
        self._show_hidden = self._load_show_hidden()
        self._hide_junk = bool(app_config.read_config().get(
            self.CONFIG_KEY_HIDE_JUNK, True))
        self._dblclick_enter = bool(app_config.read_config().get(
            self.CONFIG_KEY_DBLCLICK_ENTER, False))
        # 排序方式（默认按名称升序），从配置读取
        self._sort_key, self._sort_desc = self._load_sort()

        # 文件搜索状态：generation 用于丢弃过期的后台结果
        self._search_gen = 0
        # 搜索模式：'name' 按文件名 / 'content' 按文件内容
        self._search_mode = 'name'

        self._setup_ui()
        self._connect_signals()

        # 输入防抖：停止输入 250ms 后才真正发起递归搜索
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._start_search)
        self._search_result_signal.connect(self._on_search_results)

        # 自动刷新兜底：QFileSystemModel 已经通过 FSEvents 监听本地变更；
        # 这里只是一道保险，60s 检查一次根目录 + 所有已展开目录的条目集合，
        # 差异时才 refresh。没差异 = 几次 os.listdir，开销可忽略。
        # path -> frozenset(条目名)；path 不在 dict 里表示基线未建立
        # （不能用空集合当"未初始化"哨兵——空目录的合法指纹就是空集合）。
        self._auto_refresh_fingerprints: dict = {}
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.setInterval(60_000)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_tick)
        self._auto_refresh_timer.start()

    # ---- 隐藏文件显示开关（持久化到共享配置） ----

    def _load_show_hidden(self) -> bool:
        cfg = app_config.read_config()
        return bool(cfg.get(self.CONFIG_KEY_SHOW_HIDDEN, True))

    def _save_show_hidden(self):
        app_config.update_config({self.CONFIG_KEY_SHOW_HIDDEN: self._show_hidden},
                                 description='explorer')

    # ---- 排序方式（持久化到共享配置） ----

    def _load_sort(self) -> tuple:
        cfg = app_config.read_config()
        key = cfg.get(self.CONFIG_KEY_SORT_KEY, 'name')
        if key not in self._SORT_COLUMN:
            key = 'name'
        return key, bool(cfg.get(self.CONFIG_KEY_SORT_DESC, False))

    def _save_sort(self):
        app_config.update_config({
            self.CONFIG_KEY_SORT_KEY: self._sort_key,
            self.CONFIG_KEY_SORT_DESC: self._sort_desc,
        }, description='explorer-sort')

    def _apply_sort(self):
        """把当前排序方式应用到 QFileSystemModel（会立即反映到视图）。"""
        if not hasattr(self, 'model'):
            return
        col = self._SORT_COLUMN.get(self._sort_key, 0)
        order = (Qt.SortOrder.DescendingOrder if self._sort_desc
                 else Qt.SortOrder.AscendingOrder)
        self.model.sort(col, order)

    def get_sort(self) -> tuple:
        """返回 (sort_key, descending)，供设置菜单勾选当前项。"""
        return self._sort_key, self._sort_desc

    def set_sort(self, key: str, desc: bool):
        """设置排序方式：立即应用 + 持久化。"""
        if key not in self._SORT_COLUMN:
            key = 'name'
        desc = bool(desc)
        if key == self._sort_key and desc == self._sort_desc:
            return
        self._sort_key = key
        self._sort_desc = desc
        self._apply_sort()
        self._save_sort()

    def _build_filter(self) -> "QDir.Filter":
        f = (QDir.Filter.AllDirs | QDir.Filter.Files
             | QDir.Filter.NoDotAndDotDot)
        if self._show_hidden:
            f |= QDir.Filter.Hidden
        return f

    def is_showing_hidden(self) -> bool:
        return self._show_hidden

    def is_hiding_junk(self) -> bool:
        return self._hide_junk

    def set_hide_junk(self, hide: bool):
        """开关系统垃圾文件（.DS_Store 等）的显示，并持久化。"""
        hide = bool(hide)
        if hide == self._hide_junk:
            return
        self._hide_junk = hide
        self._proxy.set_hide_junk(hide)
        app_config.update_config({self.CONFIG_KEY_HIDE_JUNK: hide})

    @staticmethod
    def _parent_dir(path: str):
        """上一级目录；已经在文件系统根则返回 None。

        不能先 rstrip 掉分隔符再取 dirname：Windows 上 "C:\\" 会变成 "C:"，
        dirname("C:") 是空串，于是"根"被当成还能再往上退（CI 实翻）。
        normpath 之后直接 dirname，根目录的 dirname 就是它自己。
        """
        if not path:
            return None
        current = os.path.normpath(os.path.abspath(path))
        parent = os.path.dirname(current)
        if not parent or parent == current or not os.path.isdir(parent):
            return None
        return parent

    def _on_path_edited(self):
        """路径栏回车：目录存在就跳过去，否则把文字还原（不静默吞掉）。"""
        text = (self.path_edit.text() or "").strip()
        path = os.path.expanduser(text)
        if path and os.path.isdir(path):
            self.set_root_path(path)
        else:
            self._sync_path_edit()

    def _sync_path_edit(self):
        """把路径栏文字/提示同步成当前根目录。"""
        edit = getattr(self, 'path_edit', None)
        if edit is None:
            return
        if edit.text() != self._current_path:
            edit.setText(self._current_path)
        # 光标顶到末尾：栏太窄时露出的是路径尾巴（当前目录及其上一两级），
        # 那才是"我在哪"的关键信息；开头的 /Users/xxx 反而没用
        edit.setCursorPosition(len(self._current_path or ""))
        edit.setToolTip(self._current_path)

    def go_up(self):
        """回到上一级目录（已经在文件系统根就什么都不做）。"""
        parent = self._parent_dir(self._current_path or os.path.expanduser("~"))
        if parent is None:
            return False
        self.set_root_path(parent)
        return True

    def _sync_up_button(self):
        """在文件系统根上就禁用「上一级」，免得点了没反应像是坏了。"""
        btn = getattr(self, 'up_btn', None)
        if btn is None:
            return
        btn.setEnabled(self._parent_dir(self._current_path or "") is not None)

    def is_double_click_enter(self) -> bool:
        return self._dblclick_enter

    def set_double_click_enter(self, enabled: bool):
        """双击目录：进入该目录（True）还是就地展开（False）。"""
        enabled = bool(enabled)
        if enabled == self._dblclick_enter:
            return
        self._dblclick_enter = enabled
        app_config.update_config({self.CONFIG_KEY_DBLCLICK_ENTER: enabled})

    def set_show_hidden(self, show: bool):
        """切换是否显示隐藏文件，并立即应用 + 持久化。"""
        show = bool(show)
        if show == self._show_hidden:
            return
        self._show_hidden = show
        if hasattr(self, 'model'):
            self.model.setFilter(self._build_filter())
        if hasattr(self, '_proxy'):
            self._proxy.set_hide_dot_files(not show)
        self._save_show_hidden()

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部搜索栏：左侧模式切换按钮（文件名 / 文件内容）+ 搜索框。
        # 文件名模式：多关键词（空格分隔）递归匹配名称；
        # 内容模式：在文件内容中递归查找（优先用 ripgrep，缺失则纯 Python 回退）。
        search_bar = QHBoxLayout()
        search_bar.setContentsMargins(4, 4, 4, 4)
        search_bar.setSpacing(4)

        # 「上一级」：换根之后必须有路可回。以前这个面板的根只由主窗口
        # 路径栏驱动，所以没这个按钮；加了双击进入之后不给回路 = 把人困住。
        self.up_btn = QToolButton()
        self.up_btn.setText("↑")
        self.up_btn.setToolTip(t("explorer.go_up"))
        self.up_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.up_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.up_btn.clicked.connect(self.go_up)
        search_bar.addWidget(self.up_btn)

        self.search_mode_btn = QToolButton()
        self.search_mode_btn.setCheckable(True)
        self.search_mode_btn.setText("Aa")
        self.search_mode_btn.setToolTip(t("search.mode_filename"))
        self.search_mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_mode_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.search_mode_btn.toggled.connect(self._on_search_mode_toggled)
        search_bar.addWidget(self.search_mode_btn)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(t("search.placeholder"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        search_bar.addWidget(self.search_edit, 1)

        # 传输进度窗口被收起时才出现：点一下把窗口叫回来
        self._transfer_chip = QPushButton(t("transfer.reopen"))
        self._transfer_chip.setFixedHeight(22)
        self._transfer_chip.setToolTip(t("transfer.reopen_tooltip"))
        self._transfer_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._transfer_chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._transfer_chip.clicked.connect(self._reopen_transfer_job)
        self._transfer_chip.hide()
        search_bar.addWidget(self._transfer_chip)

        layout.addLayout(search_bar)

        # 当前所在目录：这个面板以前从不显示自己的路径（根只由主窗口顶部
        # 那条路径栏驱动），侧边栏嵌入模式下根本看不见，进到深层就不知道
        # 自己在哪。可编辑，回车跳转，与远程面板一致。
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(t("explorer.path_placeholder"))
        self.path_edit.setText(self._current_path)
        self.path_edit.setToolTip(self._current_path)
        self.path_edit.returnPressed.connect(self._on_path_edited)
        path_bar = QHBoxLayout()
        path_bar.setContentsMargins(4, 0, 4, 4)
        path_bar.setSpacing(4)
        path_bar.addWidget(self.path_edit, 1)
        layout.addLayout(path_bar)

        # 搜索结果列表（扁平展示命中项；默认隐藏，搜索时替换 tree_view）。
        # 在此处先创建，确保 _update_style() 引用时已存在；稍后再加入布局。
        self.search_results = QListWidget()
        self.search_results.setUniformItemSizes(True)
        self.search_results.setVisible(False)
        self.search_results.itemDoubleClicked.connect(self._open_search_result)

        # 文件系统模型
        self.model = FilteredFileSystemModel()
        # 用轻量图标提供器，避免大目录展开时逐文件查询系统图标导致卡顿
        self._icon_provider = _FastIconProvider()
        self.model.setIconProvider(self._icon_provider)
        self.model.setRootPath("")
        # 是否显示以点开头的隐藏文件/文件夹由 _show_hidden 决定（始终排除 . 和 ..）
        self.model.setFilter(self._build_filter())
        # 允许通过模型对文件/文件夹原地重命名
        self.model.setReadOnly(False)

        # 代理模型：在 Windows 上补充过滤 dot-prefix 隐藏文件/文件夹
        self._proxy = _DotFileProxy(self)
        self._proxy.setSourceModel(self.model)
        self._proxy.set_hide_dot_files(not self._show_hidden)
        self._proxy.set_hide_junk(self._hide_junk)

        # 树形视图（自定义子类，支持把 file:// URL 拖入并复制）
        self.tree_view = _LocalDropTreeView(self)
        self.tree_view.setModel(self._proxy)
        self.tree_view.setRootIndex(self._proxy.mapFromSource(self.model.index(self._current_path)))

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

        # 关闭展开/收起动画：动画会对所有新出现的行反复重新布局+重绘，
        # 大目录（几十个文件）展开/收起时明显卡顿；关掉后即时完成。
        self.tree_view.setAnimated(False)
        # 12px：比默认窄，配合参考线仍然分得清层级，深层不至于挤出屏幕
        self.tree_view.setIndentation(12)

        # 允许拖拽
        self.tree_view.setDragEnabled(True)
        self.tree_view.setAcceptDrops(True)
        self.tree_view.setDropIndicatorShown(True)

        # 应用样式
        self._update_style()

        # 应用持久化的排序方式（默认名称升序）
        self._apply_sort()

        layout.addWidget(self.tree_view)
        layout.addWidget(self.search_results, 1)

    def _connect_signals(self):
        """连接信号"""
        self.tree_view.doubleClicked.connect(self._on_double_click)
        self.tree_view.customContextMenuRequested.connect(self._show_context_menu)

        # Cmd+C / Cmd+X / Cmd+V — 当 tree_view 或其子项有焦点时触发
        copy_sc = QShortcut(QKeySequence.StandardKey.Copy, self.tree_view)
        copy_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_sc.activated.connect(lambda: self._clipboard_copy_selection(None))

        cut_sc = QShortcut(QKeySequence.StandardKey.Cut, self.tree_view)
        cut_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        cut_sc.activated.connect(lambda: self._clipboard_copy_selection(None, cut=True))

        paste_sc = QShortcut(QKeySequence.StandardKey.Paste, self.tree_view)
        paste_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        paste_sc.activated.connect(self._paste_via_shortcut)

    def _paste_via_shortcut(self):
        """Cmd+V：粘贴到当前选中项目录（或当前根目录）"""
        target = None
        for idx in self.tree_view.selectionModel().selectedIndexes():
            if idx.column() != 0:
                continue
            p = self._proxy.filePath(idx)
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

        self.path_edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none;"
            f" color: {text_dim}; padding: 0 2px; font-size: 11px; }}"
            f"QLineEdit:focus {{ color: {text}; }}")

        # 缩进参考线：取边框色再压淡 —— 要能看出层级，又不能抢文件名的注意力
        guide = QColor(border)
        guide.setAlpha(140)
        self.tree_view.set_indent_guide_color(guide)

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

        # 搜索框（已置于带 4px 外边距的 search_bar 里，故自身不再加 margin）
        self.search_edit.setStyleSheet(f"""
            QLineEdit {{
                background-color: {bg_medium};
                color: {text};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {accent}; }}
        """)

        # 文件名 / 内容 搜索模式切换按钮：选中（内容模式）时高亮
        self.search_mode_btn.setStyleSheet(f"""
            QToolButton {{
                background-color: {bg_medium};
                color: {text_dim};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 4px 6px;
                font-weight: bold;
            }}
            QToolButton:hover {{ color: {text}; border: 1px solid {accent}; }}
            QToolButton:checked {{
                background-color: {accent};
                color: white;
                border: 1px solid {accent};
            }}
        """)

        # 搜索结果列表
        self.search_results.setStyleSheet(f"""
            QListWidget {{
                background-color: {bg_dark};
                color: {text};
                border: none;
                outline: none;
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
            self._sync_up_button()
            self._sync_path_edit()
            self.model.setRootPath(path)
            self.tree_view.setRootIndex(self._proxy.mapFromSource(self.model.index(path)))
            # 切换了根 → 重置自动刷新基线
            self._auto_refresh_fingerprints.clear()
            # 换了目录 → 退出可能正在进行的搜索（结果属于旧根目录）
            if hasattr(self, 'search_edit') and self.search_edit.text():
                self.search_edit.clear()

    def prewarm(self, path: str = None):
        """启动后空闲预热：提前完成首次 ⌘B 才会触发的一次性开销。

        1) 把模型指向目标目录，后台抓取条目并装上文件监听（首次扫描）。
        2) 主动取一次文件夹/文件图标，触发 macOS 系统图标库初始化——
           这是首次展开最耗时的部分，预热后真正展示时图标已按类型缓存。
        预热时面板仍隐藏，不影响启动；之后 set_root_path 路径未变会直接跳过。"""
        try:
            target = path or self._current_path
            if target and os.path.isdir(target):
                # 与 set_root_path 一致地设置根，使首次 ⌘B 命中“路径未变”快路径
                self.set_root_path(target)

            # 预热系统图标提供器（首次调用会初始化 NSWorkspace 整套图标子系统）
            provider = self.model.iconProvider()
            if provider is not None:
                folder_icon = provider.icon(QFileIconProvider.IconType.Folder)
                file_icon = provider.icon(QFileIconProvider.IconType.File)
                # 强制栅格化，确保初始化真正发生而非被惰性推迟
                folder_icon.pixmap(QSize(16, 16))
                file_icon.pixmap(QSize(16, 16))
        except Exception:
            logger.debug("prewarm: suppressed exception", exc_info=True)

    def refresh(self):
        """刷新文件树（强制全量重扫，保留展开/选中状态）。

        QFileSystemModel 对抓取过的目录节点做永久缓存：以前用的
        setRootPath("") → setRootPath(current) 技巧只会重新枚举根目录本身，
        已展开子目录里被 watcher 漏掉的变更（FSEvents 丢事件、网络卷上
        远端写入无事件等）连 fetchMore 都补不回来。Qt 没有公开的强制
        刷新 API，唯一可靠的方式是重建模型。"""
        current = self._current_path
        expanded = self._expanded_dir_paths()
        sel_path: Optional[str] = None
        idx = self.tree_view.currentIndex()
        if idx.isValid():
            sel_path = self._proxy.filePath(idx)

        old_model = self.model
        self.model = FilteredFileSystemModel()
        self.model.setIconProvider(self._icon_provider)
        self.model.setFilter(self._build_filter())
        self.model.setReadOnly(False)
        self.model.setRootPath("")
        self._proxy.setSourceModel(self.model)
        old_model.deleteLater()

        self.model.setRootPath(current)
        self.tree_view.setRootIndex(self._proxy.mapFromSource(self.model.index(current)))
        # 换源模型会重置表头，重新只留文件名列
        for col in (1, 2, 3):
            self.tree_view.hideColumn(col)
        self._apply_sort()

        # 恢复展开状态：由浅到深，父目录先展开。index(path) 会按需建节点，
        # 目录内容随 fetchMore 异步装载，行到位后自动显示在已展开的节点下。
        for p in sorted(expanded, key=len):
            if os.path.isdir(p):
                pi = self._proxy.mapFromSource(self.model.index(p))
                if pi.isValid():
                    self.tree_view.expand(pi)
        if sel_path and os.path.exists(sel_path):
            QTimer.singleShot(50, lambda p=sel_path: self._reselect_path(p))
        self._auto_refresh_fingerprints.clear()

    def _expanded_dir_paths(self, limit: int = 512) -> list:
        """收集树里当前已展开的目录路径（只走已展开分支，数量有限、开销小）。"""
        paths: list[str] = []
        stack = [self.tree_view.rootIndex()]
        while stack and len(paths) < limit:
            parent = stack.pop()
            for r in range(self._proxy.rowCount(parent)):
                child = self._proxy.index(r, 0, parent)
                if child.isValid() and self.tree_view.isExpanded(child):
                    p = self._proxy.filePath(child)
                    if p:
                        paths.append(p)
                        stack.append(child)
        return paths

    # ---------- 文件搜索（递归、多关键词组合） ----------

    def _on_search_text_changed(self, _text: str):
        """输入变化：空 → 立即退出搜索；非空 → 防抖后发起后台搜索。"""
        # 每次输入都让上一轮后台结果作废
        self._search_gen += 1
        if not self.search_edit.text().strip():
            self._search_timer.stop()
            self._exit_search()
            return
        self._search_timer.start()

    def _on_search_mode_toggled(self, checked: bool):
        """切换文件名 / 文件内容搜索：更新按钮与占位文案，并按新模式重搜。"""
        self._search_mode = 'content' if checked else 'name'
        if checked:
            self.search_mode_btn.setText("≡")
            self.search_mode_btn.setToolTip(t("search.mode_content"))
            self.search_edit.setPlaceholderText(t("search.placeholder_content"))
        else:
            self.search_mode_btn.setText("Aa")
            self.search_mode_btn.setToolTip(t("search.mode_filename"))
            self.search_edit.setPlaceholderText(t("search.placeholder"))
        # 让上一轮后台结果作废；有内容则用新模式重搜，否则退出搜索态
        self._search_gen += 1
        if self.search_edit.text().strip():
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self._exit_search()

    def _exit_search(self):
        """退出搜索态：隐藏结果列表，恢复正常文件树。"""
        self.search_results.setVisible(False)
        self.search_results.clear()
        self.tree_view.setVisible(True)

    def _start_search(self):
        """防抖结束 → 在后台线程递归扫描当前根目录。"""
        query = self.search_edit.text().strip()
        if not query:
            self._exit_search()
            return
        mode = self._search_mode
        if mode == 'name':
            tokens = parse_search_tokens(query)
            if not tokens:
                self._exit_search()
                return
        # 每次发起都占一个唯一 generation，避免重复调用时两个后台线程同 gen 并行
        self._search_gen += 1
        gen = self._search_gen
        root = self._current_path
        show_hidden = self._show_hidden
        # 切到结果视图并给个「搜索中」占位
        self.tree_view.setVisible(False)
        self.search_results.setVisible(True)
        self.search_results.clear()
        placeholder = QListWidgetItem(t("search.searching"))
        placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
        self.search_results.addItem(placeholder)

        if mode == 'content':
            target = self._run_content_search
            args = (gen, root, query, show_hidden)
        else:
            target = self._run_search
            args = (gen, root, tokens, show_hidden)
        t_thread = threading.Thread(target=target, args=args, daemon=True)
        t_thread.start()

    def _run_search(self, gen: int, root: str, tokens: list, show_hidden: bool):
        """后台线程：os.walk 递归匹配，命中/已扫描达到上限即停。"""
        results = []
        scanned = 0
        truncated = False
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if gen != self._search_gen:
                    return  # 查询已变化，丢弃本轮
                # 可见目录：隐藏开关决定是否包含点开头目录（仍可被名字匹配/展示）。
                # 注意要做成独立列表 —— 下面 dirnames[:] 原地裁剪不能反过来影响它，
                # 否则 .git/target 这类被剔除递归的目录连名字都匹配不到了。
                visible_dirs = (list(dirnames) if show_hidden
                                else [d for d in dirnames if not d.startswith('.')])
                # 但递归时再剔除重型/缓存目录（.git/.m2/target/node_modules…），
                # 避免在 Maven/Node 仓库里把扫描预算耗尽却找不到东西。
                dirnames[:] = [d for d in visible_dirs
                               if d not in self._SEARCH_SKIP_DIRS]
                # 分别遍历目录/文件，避免 `name in dirnames` 的 O(n²) 成员判断
                for is_dir, names in ((True, visible_dirs), (False, filenames)):
                    for name in names:
                        if not show_hidden and name.startswith('.'):
                            continue
                        scanned += 1
                        if name_matches_tokens(name, tokens):
                            abs_path = os.path.join(dirpath, name)
                            rel = os.path.relpath(abs_path, root)
                            # 统一为 5 元组 (abs, is_dir, rel, line_no, preview)，
                            # 文件名命中没有行号/预览，用 None 占位。
                            results.append((abs_path, is_dir, rel, None, None))
                            if len(results) >= self._SEARCH_MAX_RESULTS:
                                truncated = True
                                break
                        if scanned >= self._SEARCH_MAX_SCANNED:
                            truncated = True
                            break
                    if truncated:
                        break
                if truncated:
                    break
        except Exception:
            # 意外错误不能静默——否则搜索出错和"真的没命中"在 UI 上无法区分
            logger.exception("filename search failed under %s", root)
        if gen == self._search_gen:
            self._search_result_signal.emit(gen, results, truncated)

    # ---------- 内容搜索（在文件正文中查找，类似 grep） ----------

    def _run_content_search(self, gen: int, root: str, query: str, show_hidden: bool):
        """后台线程入口：优先用 ripgrep（快、自动跳过二进制），缺失则纯 Python 回退。
        query 支持空格分隔的多关键词：同一行需包含全部关键词（AND，忽略顺序/大小写）。"""
        tokens = parse_search_tokens(query)  # 已小写化、去重
        if not tokens:
            if gen == self._search_gen:
                self._search_result_signal.emit(gen, [], False)
            return
        rg = shutil.which('rg')
        if rg:
            ok = self._run_content_search_rg(gen, root, tokens, show_hidden, rg)
            if ok:
                return
            # rg 启动失败 / 不支持 PCRE2 → 回退纯 Python
        self._run_content_search_py(gen, root, tokens, show_hidden)

    def _run_content_search_rg(self, gen, root, tokens, show_hidden, rg) -> bool:
        """用 ripgrep 忽略大小写地搜内容；流式读取以便随时中断。
        单关键词走固定字符串；多关键词用 PCRE2 前瞻实现"同行包含全部词"。
        返回 False 表示进程没能启动/出错，调用方应回退到纯 Python。"""
        results = []
        truncated = False
        # --null：文件名后跟 NUL 分隔，避免路径里的冒号干扰解析（Windows C:\ 等）
        cmd = [
            rg, '--line-number', '--no-heading', '--color', 'never', '--null',
            '--ignore-case',
            '--max-columns', str(self._CONTENT_PREVIEW_LEN), '--max-columns-preview',
            '--max-count', str(self._CONTENT_MAX_PER_FILE),
        ]
        if len(tokens) == 1:
            cmd.append('--fixed-strings')
            pattern = tokens[0]
        else:
            # 多关键词：用前瞻把每个词都要求出现在同一行（顺序无关），再用 .* 形成实际匹配。
            # --ignore-case 已处理大小写，故不再加 (?i)；re.escape 让关键词按字面量匹配。
            cmd.append('--pcre2')
            pattern = ''.join(f'(?=.*{re.escape(tok)})' for tok in tokens) + '.*'
        if show_hidden:
            # 显示隐藏文件时也搜隐藏文件、无视 .gitignore，但仍要排除重型目录，
            # 否则 rg 会一头扎进 .git/.m2/target，慢且全是噪音。
            cmd += ['--hidden', '--no-ignore']
            for d in self._SEARCH_SKIP_DIRS:
                cmd += ['-g', f'!{d}']
        cmd += ['--', pattern, root]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, errors='replace',
            )
        except Exception:
            return False
        try:
            for line in proc.stdout:
                if gen != self._search_gen:
                    proc.kill()
                    return True  # 查询已变化，丢弃本轮（不再回退）
                parsed = self._parse_rg_line(line, root)
                if parsed is None:
                    continue
                results.append(parsed)
                if len(results) >= self._SEARCH_MAX_RESULTS:
                    truncated = True
                    proc.kill()
                    break
            proc.wait()
        except Exception:
            try:
                proc.kill()
            except Exception:
                logger.debug("_run_content_search_rg: suppressed exception", exc_info=True)
            return False  # rg 读取异常 → 回退纯 Python
        # rg 退出码：0=有命中，1=无命中（均正常），>=2 表示出错（如旧版本不识别参数）。
        # 出错且未截断时回退纯 Python，避免静默地把「报错」显示成「无结果」。
        if not truncated and (proc.returncode or 0) >= 2:
            return False
        if gen == self._search_gen:
            self._search_result_signal.emit(gen, results, truncated)
        return True

    def _parse_rg_line(self, line: str, root: str):
        """解析一行 rg --null 输出：`abs_path\\0line_no:content` → 5 元组。"""
        line = line.rstrip('\n')
        nul = line.find('\0')
        if nul < 0:
            return None
        abs_path = line[:nul]
        rest = line[nul + 1:]
        colon = rest.find(':')
        if colon < 0:
            return None
        try:
            line_no = int(rest[:colon])
        except ValueError:
            return None
        preview = rest[colon + 1:].strip()[:self._CONTENT_PREVIEW_LEN]
        try:
            rel = os.path.relpath(abs_path, root)
        except ValueError:
            rel = abs_path
        return (abs_path, False, rel, line_no, preview)

    def _run_content_search_py(self, gen, root, tokens, show_hidden):
        """纯 Python 回退：逐文件逐行查找，跳过二进制/超大文件与噪音目录。
        tokens 为已小写化的关键词列表，命中要求同一行包含全部关键词（AND）。"""
        results = []
        truncated = False
        files_scanned = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                if gen != self._search_gen:
                    return
                if not show_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                # 始终跳过 .git / .m2 / node_modules 等大型目录，避免纯 Python 扫描卡死
                dirnames[:] = [d for d in dirnames if d not in self._SEARCH_SKIP_DIRS]
                for name in filenames:
                    if not show_hidden and name.startswith('.'):
                        continue
                    if os.path.splitext(name)[1].lower() in self._CONTENT_SKIP_EXT:
                        continue
                    # 读取文件数兜底：极端情况下（巨量纯文本）也能在有限时间内收尾
                    files_scanned += 1
                    if files_scanned > self._CONTENT_MAX_FILES:
                        truncated = True
                        break
                    abs_path = os.path.join(dirpath, name)
                    try:
                        if os.path.getsize(abs_path) > self._CONTENT_MAX_FILE_BYTES:
                            continue
                    except OSError:
                        continue
                    # 快速二进制判定：头部含 NUL 字节则跳过
                    try:
                        with open(abs_path, 'rb') as bf:
                            if b'\0' in bf.read(2048):
                                continue
                    except OSError:
                        continue
                    rel = os.path.relpath(abs_path, root)
                    per_file = 0
                    try:
                        with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                            for line_no, text_line in enumerate(f, 1):
                                if gen != self._search_gen:
                                    return
                                low = text_line.lower()
                                if all(tok in low for tok in tokens):
                                    preview = text_line.strip()[:self._CONTENT_PREVIEW_LEN]
                                    results.append((abs_path, False, rel, line_no, preview))
                                    per_file += 1
                                    if len(results) >= self._SEARCH_MAX_RESULTS:
                                        truncated = True
                                        break
                                    if per_file >= self._CONTENT_MAX_PER_FILE:
                                        break
                    except (OSError, UnicodeError):
                        continue
                    if truncated:
                        break
                if truncated:
                    break
        except Exception:
            # 意外错误不能静默——否则搜索出错和"真的没命中"在 UI 上无法区分
            logger.exception("content search failed under %s", root)
        if gen == self._search_gen:
            self._search_result_signal.emit(gen, results, truncated)

    def _on_search_results(self, gen: int, results: list, truncated: bool):
        """回到 UI 线程：把命中项填进结果列表。"""
        if gen != self._search_gen:
            return  # 过期结果
        self.search_results.clear()
        if not results:
            empty = QListWidgetItem(t("search.no_results"))
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.search_results.addItem(empty)
            return
        # 文件夹在前，再按相对路径排序；内容命中再按行号排序，观感稳定
        results.sort(key=lambda r: (not r[1], r[2].lower(), r[3] or 0))
        for abs_path, is_dir, rel, line_no, preview in results:
            if line_no is None:
                # 文件名/文件夹命中
                icon = "📁  " if is_dir else "📄  "
                item = QListWidgetItem(icon + rel)
                item.setToolTip(abs_path)
            else:
                # 内容命中：展示「相对路径:行号  命中行预览」
                item = QListWidgetItem(f"{rel}:{line_no}    {preview}")
                item.setToolTip(f"{abs_path}:{line_no}")
            item.setData(Qt.ItemDataRole.UserRole, (abs_path, is_dir, line_no))
            self.search_results.addItem(item)
        if truncated:
            note = QListWidgetItem(
                t("search.truncated", count=len(results)))
            note.setFlags(Qt.ItemFlag.NoItemFlags)
            self.search_results.addItem(note)

    def _open_search_result(self, item: QListWidgetItem):
        """双击搜索结果：文件 → 打开；文件夹 → 退出搜索并在树里定位展开。"""
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        abs_path, is_dir = data[0], data[1]
        if is_dir:
            # 不改变根目录（否则会与工具栏目录脱节且无法返回），
            # 而是退出搜索、在原有文件树里定位并展开该文件夹。
            self.search_edit.clear()  # 触发 _exit_search
            idx = self._proxy.mapFromSource(self.model.index(abs_path))
            if idx.isValid():
                self.tree_view.setCurrentIndex(idx)
                self.tree_view.scrollTo(
                    idx, QAbstractItemView.ScrollHint.PositionAtCenter)
                self.tree_view.expand(idx)
        elif os.path.isfile(abs_path):
            # 内容命中带行号 → 打开后跳到该行；文件名命中 line_no 为 None/0
            line_no = data[2] if len(data) > 2 and data[2] else 0
            # 文件名命中的可能是编辑器展示不了的二进制（如 xlsx）→ 系统打开
            # （内容命中必然是文本，嗅探自然放行）
            if not self._editor_can_display(abs_path):
                self._open_file(abs_path)
                return
            self.file_double_clicked.emit(abs_path)
            self.file_edit_requested.emit(abs_path, line_no)

    def _auto_refresh_tick(self):
        """60s 安全网：QFileSystemModel + FSEvents 已经处理大多数情况，
        这里对根目录 + 所有已展开目录做条目集合对比，任何一处变了才真正
        刷一次。无变化 = 几次 os.listdir，几乎零开销。

        某个目录第一次出现在检查范围（刚换根/刚展开/刚刷新过）只建基线
        不刷新——模型此刻刚读过盘，数据是新的。"""
        if not self.isVisible():
            return
        root = self._current_path
        if not root or not os.path.isdir(root):
            return
        old = self._auto_refresh_fingerprints
        fresh: dict = {}
        changed = False
        for path in [root] + self._expanded_dir_paths(limit=64):
            try:
                fresh[path] = frozenset(os.listdir(path))
            except OSError:
                continue
            prev = old.get(path)
            if prev is not None and prev != fresh[path]:
                changed = True
        if changed:
            self.refresh()  # 内部保留展开/选中状态
        self._auto_refresh_fingerprints = fresh

    def _reselect_path(self, path: str):
        idx = self._proxy.mapFromSource(self.model.index(path))
        if idx.isValid():
            self.tree_view.setCurrentIndex(idx)
            self.tree_view.scrollTo(idx)

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()

    def apply_language(self):
        """应用语言设置（刷新可翻译的 UI 文本）

        右键菜单每次创建时会调用 t() 获取最新翻译，无需在此处理；
        但常驻的搜索框 placeholder 需要在切换语言时更新。
        """
        self.search_edit.setPlaceholderText(t("search.placeholder"))

    def set_editing_file(self, file_path: str):
        """设置当前正在编辑的文件路径"""
        self._editing_file = file_path

    def clear_editing_file(self):
        """清除当前正在编辑的文件路径"""
        self._editing_file = None

    def _editor_can_display(self, file_path: str) -> bool:
        """内置编辑器能否有效展示该文件——委托共享判定（与远程 explorer 同源）。"""
        return explorer_common.editor_can_display(file_path)

    def _on_double_click(self, index: QModelIndex):
        """双击事件处理"""
        if not index.isValid():
            return

        file_path = self._proxy.filePath(index)
        if os.path.isdir(file_path):
            if self._dblclick_enter:
                # 进到那一层去（换根）。注意：进去之后必须有路可回，
                # 见头部的「上一级」按钮 —— 否则用户就被困在里面了。
                self.set_root_path(file_path)
            else:
                # 默认：就地展开/收起，跟以前一样
                self.tree_view.setExpanded(index,
                                           not self.tree_view.isExpanded(index))
            return
        if os.path.isfile(file_path):
            if self._editor_can_display(file_path):
                # 发射信号请求在内置编辑器中打开（行号 0 = 不跳转）
                self.file_double_clicked.emit(file_path)
                self.file_edit_requested.emit(file_path, 0)
            else:
                # 编辑器展示不了的格式（Excel 等）→ 系统默认应用打开
                self._open_file(file_path)

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

    @staticmethod
    def _applescript_terminal_run(dir_path: str, command: str) -> str:
        """构造在 Terminal.app 执行 `cd <dir> && <command>` 的 AppleScript。

        安全：路径已由调用方用 shlex.quote 做 shell 引用，这里再对整条命令做
        AppleScript 字符串转义（反斜杠、双引号），双重防止恶意文件名注入命令。
        """
        shell_cmd = f"cd {shlex.quote(dir_path)} && {command}"
        esc = shell_cmd.replace('\\', '\\\\').replace('"', '\\"')
        return (
            'tell application "Terminal"\n'
            '    activate\n'
            f'    do script "{esc}"\n'
            'end tell'
        )

    def _run_file(self, file_path: str):
        """运行文件"""
        # 获取文件扩展名
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        # 扩展名 → 解释器；其余走系统默认应用
        interp_map = {
            '.py': 'python3', '.sh': 'bash', '.bash': 'bash',
            '.js': 'node', '.mjs': 'node',
        }
        interp = interp_map.get(ext)
        if interp is None:
            self._open_file(file_path)
            return

        dir_path = os.path.dirname(file_path)
        try:
            if sys.platform == 'darwin':
                # 文件路径用 shlex.quote 引用，杜绝文件名内的引号/元字符注入
                command = f"{interp} {shlex.quote(file_path)}"
                script = self._applescript_terminal_run(dir_path, command)
                subprocess.Popen(['osascript', '-e', script])
            elif sys.platform == 'win32':
                # Windows 无 bash；.sh 退回系统默认应用
                if interp == 'bash':
                    self._open_file(file_path)
                else:
                    exe = 'python' if interp == 'python3' else interp
                    subprocess.Popen([exe, file_path], cwd=dir_path,
                                     creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                # Linux：-e 接收命令字符串，对路径做 shell 引用
                subprocess.Popen(
                    ['x-terminal-emulator', '-e', f"{interp} {shlex.quote(file_path)}"],
                    cwd=dir_path,
                )
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
            file_path = self._proxy.filePath(index)
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
            # 注意：triggered 会发出 checked(bool)，必须用第一个形参吃掉它，
            # 否则 paths 会被 False 覆盖 → _delete_paths(False) 报错、删除「没反应」。
            delete_action.triggered.connect(
                lambda checked=False, paths=delete_paths: self._delete_paths(paths)
            )

            menu.addSeparator()

            # 复制 / 剪切 / 粘贴（跨面板、跨窗口）
            copy_action = menu.addAction(t("explorer.copy"))
            copy_action.triggered.connect(lambda: self._clipboard_copy_selection(file_path))

            cut_action = menu.addAction(t("explorer.cut"))
            cut_action.triggered.connect(
                lambda: self._clipboard_copy_selection(file_path, cut=True))

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

            # 快捷方式（收藏）：文件夹点了切换目录、文件点了在编辑器打开（见 ★ 下拉）
            if explorer_favorites.is_favorite(file_path):
                fav_action = menu.addAction(t("explorer.favorite_remove"))
                fav_action.triggered.connect(
                    lambda checked=False, p=file_path: self._toggle_favorite(p, add=False))
            else:
                fav_action = menu.addAction(t("explorer.favorite_add"))
                fav_action.triggered.connect(
                    lambda checked=False, p=file_path: self._toggle_favorite(p, add=True))

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

    def _toggle_favorite(self, path: str, add: bool):
        """把文件/文件夹加入或移出快捷方式（收藏）。落盘即生效，跨窗口共享。"""
        if add:
            explorer_favorites.add(path)
        else:
            explorer_favorites.remove(path)
        self.favorites_changed.emit()

    def _new_file(self, target_dir: str):
        """创建新文件并直接在文件树里原地重命名（不弹窗）"""
        # 默认无扩展名：用户在原地重命名时自己决定后缀（不强加 .txt）
        file_path = self._unique_new_path(target_dir, "untitled", "")
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
            parent_idx = self._proxy.mapFromSource(self.model.index(target_dir))
            if parent_idx.isValid():
                self.tree_view.expand(parent_idx)
        idx = self._proxy.mapFromSource(self.model.index(new_path))
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
        idx = self._proxy.mapFromSource(self.model.index(file_path))
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
            p = self._proxy.filePath(idx)
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

    @staticmethod
    def _macos_trash_via_finder(file_path: str) -> bool:
        """让 Finder 把路径移到废纸篓。成功返回 True，被拦截/失败返回 False。

        用 AppleScript 变量传路径并转义引号/反斜杠，避免路径里带特殊字符时
        脚本被截断。20s 超时防止 Finder 卡死时一直挂着。
        """
        escaped = file_path.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'set p to POSIX file "{escaped}"\n'
            f'tell application "Finder" to delete p'
        )
        try:
            subprocess.run(
                ['osascript', '-e', script],
                check=True, capture_output=True, timeout=20,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False

    def _send_to_trash(self, file_path: str):
        """把单个本地路径移到回收站（平台分发）；失败时回退到永久删除。"""
        is_dir = os.path.isdir(file_path)
        if sys.platform == 'darwin':
            # 首选 Finder 把它丢进废纸篓（可还原）。但这条路依赖「自动化」权限
            # （系统设置 › 隐私与安全性 › 自动化 › 允许本 App 控制 Finder）——
            # 没授权 / Finder 繁忙时 osascript 会报错。以前这里直接 check=True 抛出、
            # 没有任何兜底，于是表现成「右键删不掉文件夹」。现在失败就逐级回退。
            if self._macos_trash_via_finder(file_path):
                return
            # 回退 1：send2trash（走原生 API，不需要 Finder 自动化权限）
            try:
                from send2trash import send2trash
                send2trash(file_path)
                return
            except Exception:
                logger.debug("_send_to_trash: suppressed exception", exc_info=True)
            # 回退 2：永久删除（不进废纸篓，但保证「删得掉」）
            if is_dir:
                shutil.rmtree(file_path)
            else:
                os.remove(file_path)
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

    def _clipboard_copy_selection(self, fallback_path: str = None, cut: bool = False):
        """把当前选中的文件 / 文件夹放入跨面板剪贴板（同时写到系统剪贴板）。

        若 fallback_path 不在选中范围内，则只复制 fallback_path（右键单项）。
        cut=True 表示剪切：粘贴时移动而非复制。
        """
        indexes = self.tree_view.selectionModel().selectedIndexes()
        paths: list[str] = []
        seen = set()
        for idx in indexes:
            if idx.column() != 0:
                continue
            p = self._proxy.filePath(idx)
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
            cut=cut,
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
        # 剪切（Cmd+X）→ 本地项按"移动"处理；粘贴后剪贴板一次性失效
        move_mode = explorer_clipboard.is_cut()
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

        # 一批粘贴 = 一个统一窗口：每个条目一行（等待 / 进行中 / 完成 / 失败），
        # 而不是逐条目、逐阶段各弹一个进度框（远端 → 本地那条路尤其明显）。
        job = self._begin_transfer_job(
            [self._clipboard_item_name(it) for it in items],
            header=t("explorer.pasting_into", dst=target_dir))

        for row, it in enumerate(items):
            if cancel_all or (job is not None and job.was_canceled()):
                cancel_all = True
                break
            kind = it[0]
            if job is not None:
                job.set_active_rows([row])
                # 本地复制是同步阻塞的：先让窗口把这一行画出来，
                # 否则整批看着像卡住。只放重绘，不派发用户输入（防重入）
                QApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            try:
                if kind == "local":
                    src = it[1]
                    name = os.path.basename(src.rstrip("/"))
                    src_dir_abs = os.path.abspath(os.path.dirname(src.rstrip("/")))
                    same_folder = (src_dir_abs == target_abs)
                    dst = os.path.join(target_dir, name)
                    # 剪切到原文件夹 = 原地移动，无事可做
                    if move_mode and same_folder:
                        self._finish_job_row(job, row)
                        continue
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
                    if move_mode:
                        shutil.move(src, dst)
                    elif os.path.isdir(src) and not os.path.islink(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                    self._finish_job_row(job, row)

                elif kind == "remote":
                    _, host_alias, remote_path, session = it
                    if session is None or not session.is_connected():
                        errors.append(f"{remote_path}: {t('remote.session_lost')}")
                        self._finish_job_row(job, row, t("remote.session_lost"))
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
                    self._finish_job_row(job, row)
                else:
                    errors.append(f"Unknown clipboard item: {it!r}")
                    self._finish_job_row(job, row, f"Unknown clipboard item: {it!r}")
            except Exception as e:
                errors.append(f"{it}: {e}")
                self._finish_job_row(job, row, str(e))

        failures = self._end_transfer_job(job)
        if move_mode:
            # 剪切是一次性的：来源已被移走，残留的剪贴板路径已失效
            explorer_clipboard.clear()
        # 失败已经逐行写在统一窗口里了，就不再叠一个弹窗；没有窗口
        # （单条目粘贴）或映射不上的错误仍照旧弹出来
        if errors and not failures:
            QMessageBox.warning(self, t("explorer.error"), "\n".join(errors))
        self.refresh()

    def _resolve_paste_conflict(self, name: str, sticky: Optional[str]):
        """跨文件夹冲突时的三选一对话框（收敛到 explorer_common 单点维护）。"""
        return explorer_common.resolve_paste_conflict(self, name, sticky)

    def _await_remote(self, session, fn, *args, label: str):
        """提交单个远端操作，在事件循环等待中返回结果。

        远端 → 本地粘贴流程里的 stat/listdir 一律走这里，
        禁止直接 fut.result()——网络一慢就是整窗无限期冻结。
        """
        fut = session.submit(fn, *args)
        self._wait_future_with_progress([fut], label, abort_sessions=[session])
        return fut.result()

    def _download_remote_recursive(self, session, remote_path: str, local_path: str):
        """通过 SSH session 把远程文件 / 目录递归下载到 local_path（阻塞，含进度对话框）"""
        # 先 stat 看是不是目录
        entry = self._await_remote(session, session.stat, remote_path,
                                   label=t("explorer.pasting"))
        if not entry.is_dir:
            fut = session.submit(session.download, remote_path, local_path)
            self._wait_future_with_progress(
                [fut], t("explorer.pasting"), abort_sessions=[session],
            )
            return
        # 递归：先 mkdir，再列出，再为每个子项递归
        os.makedirs(local_path, exist_ok=True)
        children = self._await_remote(session, session.listdir, remote_path,
                                      label=t("explorer.pasting"))
        # 一层一层下载，文件并发用同一 session executor 排队即可
        for child in children:
            child_local = os.path.join(local_path, child.name)
            if child.is_dir and not child.is_link:
                self._download_remote_recursive(session, child.path, child_local)
            else:
                fut = session.submit(session.download, child.path, child_local)
                self._wait_future_with_progress([fut], t("explorer.pasting"),
                                                abort_sessions=[session])

    def _wait_future_with_progress(self, futures: list, label: str,
                                   abort_sessions: Optional[list] = None):
        """阻塞等待 futures，但跑一个事件循环让 UI 不卡。

        abort_sessions：给出时进度框带「取消」按钮，点击后 abort 这些
        会话直接关 socket，让卡住的传输立刻失败返回。

        批量粘贴期间 self._transfer_job 是那一批的统一进度窗口：这里就不再
        新开 QProgressDialog，而是把阶段文案/比例画进窗口里当前那一行。
        """
        if not futures:
            return
        job = self._active_transfer_job()
        progress = None
        if job is not None:
            job.set_stage(label)
            self._register_job_abort(job, abort_sessions)
        else:
            cancel_text = t("remote.cancel_transfer") if abort_sessions else None
            progress = QProgressDialog(label, cancel_text, 0, len(futures), self)
            # 非模态：传输期间应用可继续正常使用（后台传输），与远程面板一致
            progress.setWindowModality(Qt.WindowModality.NonModal)
            progress.setMinimumDuration(300)
            progress.setValue(0)
            if abort_sessions:
                def _on_cancel(_sessions=list(abort_sessions)):
                    for s in _sessions:
                        if s is not None:
                            try:
                                s.abort()
                            except Exception:
                                logger.debug("_on_cancel: suppressed exception", exc_info=True)
                progress.canceled.connect(_on_cancel)
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
            # 父 widget 可能在等待期间被销毁（如用户关掉 panel/窗口），
            # 此时 progress 也已 deleteLater'd → 任何访问都会段错误
            try:
                ui = progress if progress is not None else job
                if sip.isdeleted(ui):
                    # 面板在传输中被销毁：abort 会话让 pending futures 快速
                    # 失败，否则调用方紧接着的 fut.result() 会在 GUI 线程上
                    # 无限期阻塞（与 remote_explorer_widget 同一处理）
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
                if progress is not None:
                    progress.setValue(done["n"])
                else:
                    # 统一窗口：进度按「已完成条目 + 当前条目比例」推进
                    job.set_stage_progress(done["n"] / len(futures))
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
            if progress is not None and not sip.isdeleted(progress):
                progress.setValue(len(futures))
            elif progress is None and not sip.isdeleted(job):
                job.set_stage_progress(1.0)
        except RuntimeError:
            pass
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
