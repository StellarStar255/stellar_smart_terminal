"""MainWindow 的标签页与分屏混入（从 main_window.py 拆出，上帝类拆分最后一刀）。

新建/关闭/切换标签、水平·垂直分屏、关闭·移动分屏、分离标签为新窗口
(detach)及跟随动画、内联重命名、分屏布局管道(capture/place/resolve/
orientation/drag)。纯方法搬迁，行为不变；detach 构造新窗口/进程级共享
属性经 host_class(self) 落到 MainWindow。
"""
import os
import time
from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRect, QTimer, Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (QApplication, QMenu, QMessageBox, QPushButton,
                             QSplitter, QTabBar, QWidget)
from dialogs import get_default_shell
from i18n import t
from widgets import InlineRenameEdit, TabDragPreview
from app_logging import get_logger
# 进程级共享类属性 / 构造新窗口经 window_host.host_class(self) 落到真 MainWindow
from window_host import host_class

logger = get_logger(__name__)


class TabSplitMixin:

    def _init_tabs_state(self):
        """TabSplitMixin 的实例状态（唯一默认值）"""
        self._tab_rename_editor = None  # 进行中的标签就地重命名编辑框

    @staticmethod
    def _ql_detach_modifier_held() -> bool:
        """快速启动激活瞬间是否按着 Shift/Cmd（Qt 在 macOS 上把 Cmd 映射为 Control）。

        按住即表示「启动后直接扩展为新窗口」。
        """
        mods = QApplication.keyboardModifiers()
        return bool(mods & (Qt.KeyboardModifier.ShiftModifier
                            | Qt.KeyboardModifier.ControlModifier
                            | Qt.KeyboardModifier.MetaModifier))

    @staticmethod
    def _tab_name_for_dir(dir_path: str) -> str:
        """由目录路径取标签名：末级文件夹名；根目录等无末级名时退回整条路径。

        约定入参已 normpath（去掉尾斜杠），否则 basename 可能为空。
        """
        return os.path.basename(dir_path) or dir_path

    def _add_new_tab(self, external_splitter=None, external_terminals=None, external_session=None, tab_name=None, tab_cwd=None):
        """添加新的终端标签页

        Args:
            external_splitter: 外部传入的 splitter（用于接收分离的 tab）
            external_terminals: 外部传入的 terminal 列表
            external_session: 外部传入的 session
            tab_name: 自定义标签名
            tab_cwd: 该标签页独立的工作目录
        """
        self.tab_counter += 1
        if tab_name is None:
            tab_name = t("terminal.default_name", n=self.tab_counter)

        if external_splitter and external_terminals:
            # 使用外部传入的 splitter 和 terminals
            splitter = external_splitter
            terminals = external_terminals
            session = external_session

            # 重新设置 parent
            splitter.setParent(self.tab_widget)
            self._absorb_terminals(terminals)
            for _t in terminals:
                _t.set_pane_handle_visible(len(terminals) > 1)
        else:
            # 创建新的分屏容器
            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setHandleWidth(2)
            splitter.setStyleSheet("""
                QSplitter::handle {
                    background-color: #3d3d5c;
                }
                QSplitter::handle:hover {
                    background-color: #667eea;
                }
            """)

            # 创建第一个终端
            terminal = self._create_terminal()
            splitter.addWidget(terminal)
            terminals = [terminal]
            session = None

        # 添加到标签页
        idx = self.tab_widget.addTab(splitter, tab_name)
        self.tab_splitters[idx] = splitter
        self.tab_terminals[idx] = terminals
        self.tab_sessions[idx] = session
        self.tab_cwds[idx] = tab_cwd if tab_cwd else self._window_cwd  # 存储独立工作目录

        # 添加自定义关闭按钮到标签页
        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                padding: 0;
                margin: 0;
            }
            QPushButton:hover {
                background-color: #ff6b6b;
            }
        """)
        close_btn.clicked.connect(lambda checked, i=idx: self._close_tab_by_button(i))
        self.tab_widget.tabBar().setTabButton(idx, QTabBar.ButtonPosition.RightSide, close_btn)

        # 切换到新标签页
        self.tab_widget.setCurrentIndex(idx)
        self.active_terminal = terminals[0]
        terminals[0].setFocus()

        # 如果有外部会话且终端正在运行，更新状态
        if external_session and any(t.is_running() for t in terminals):
            self.current_session = external_session
            self._update_running_state(True)

        # 工作区快照：标签结构变化，限流补写
        self._checkpoint_workspace()
        return idx

    def _absorb_terminals(self, terminals):
        """把来自别的窗口（或刚被摘下）的终端接进本窗口：重接信号、转移归属、
        指回本窗口的预设，并恢复绘制。跨窗口接管标签与并入分屏共用。"""
        for terminal in terminals:
            # 重新连接 terminal 信号：先拆掉原窗口的全部接线，再按同一张表
            # 接到本窗口（表在 MainWindow._TERMINAL_SIGNAL_NAMES，两处共用）
            self._unwire_terminal_signals(terminal)
            self._wire_terminal_signals(terminal)
            # 归属转移：务必走 _adopt_terminal（内部会摘掉原窗口的事件
            # 过滤器），否则原窗口的 active_terminal 会被本窗口的终端污染
            self._adopt_terminal(terminal)

            # 重新设置快速命令提供者，指向当前窗口的预设
            terminal.quick_commands_provider = lambda: self.presets
            terminal.local_quick_commands_provider = lambda: self.local_presets

            # 确保 terminal 正确显示（修复从其他窗口拖拽后的显示问题）
            terminal.setUpdatesEnabled(True)  # 恢复绘制更新（detach 时会暂停）
            terminal.show()
            terminal.update()

    def _close_tab_by_button(self, index):
        """通过按钮关闭标签页（需要找到正确的索引）"""
        # 由于标签页可能被移动，需要找到按钮对应的实际索引
        sender = self.sender()
        for i in range(self.tab_widget.count()):
            if self.tab_widget.tabBar().tabButton(i, QTabBar.ButtonPosition.RightSide) is sender:
                self._close_tab(i)
                return
        # 如果找不到，尝试使用原始索引
        if index < self.tab_widget.count():
            self._close_tab(index)

    def _on_tab_moved(self, _from_idx, _to_idx):
        """拖动标签重排后重建索引映射。

        QTabWidget 收到 tabMoved 会先同步内部页面顺序（本槽在其后执行），
        但 tab_splitters/tab_terminals/tab_cwds 这些按旧索引存的字典不会
        自动跟随；不重建的话，分屏会回退到错位列表的 terminals[0]——
        用户表现为「在最后一个标签分屏，新终端跑到前面的标签里去了」。
        """
        self._rebuild_tab_mappings()
        self._checkpoint_workspace()

    def _synced_tab_splitter(self, idx):
        """取第 idx 页的 splitter，先以 tab_widget 里的真实页面校验映射。

        任何遗漏 _rebuild_tab_mappings 的路径（历史上：拖动标签重排没接
        tabMoved）都会让按索引存的映射整体错位；这里兜底修正，保证
        分屏/关闭分屏永远作用在当前页自己的终端上。
        """
        page = self.tab_widget.widget(idx)
        if page is not None and self.tab_splitters.get(idx) is not page:
            logger.warning(
                "[Tabs] tab_splitters[%s] 与真实页面不一致，已重建映射", idx)
            self._rebuild_tab_mappings()
        return self.tab_splitters.get(idx)

    def _target_terminal_in_tab(self, terminals, fallback_first=True):
        """本页里「当前选中」的终端：分屏/关闭分屏的作用对象。

        解析顺序（越靠前越可信）：
        1. 真正持有键盘焦点的那个终端 —— 用户眼里的「当前」就是它；
        2. 经归属校验的 active_terminal（`_current_active_terminal`）；
        3. 本页第一个终端 —— 仅在 fallback_first 时启用。

        不直接用 self.active_terminal：它由 FocusIn 事件过滤器更新，多窗口下
        曾被别的窗口的终端污染（见 tests/test_cross_window_active_terminal.py），
        用户表现为「分屏分裂了错误的窗格」「之前的终端被关掉了」。

        fallback_first=False 用于**销毁性**操作（关闭分屏）：宁可什么都不做
        也不能挑一个终端顶包——关错终端不可撤销。
        """
        if not terminals:
            return None
        focused = QApplication.focusWidget()
        while focused is not None:
            if focused in terminals:
                return focused
            focused = focused.parent()
        active = self._current_active_terminal()
        if active in terminals:
            return active
        return terminals[0] if fallback_first else None

    def _styled_splitter(self, orientation):
        """创建一个带统一手柄样式的 QSplitter"""
        splitter = QSplitter(orientation)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #3d3d5c;
            }
            QSplitter::handle:hover {
                background-color: #667eea;
            }
        """)
        return splitter

    def _restore_tab_close_button(self, idx):
        """为第 idx 个标签页重新创建右上角的关闭按钮（removeTab 会丢弃原按钮）"""
        close_btn = QPushButton("×")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 16px;
                font-family: 'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji', Arial, sans-serif;
                padding: 0;
                margin: 0;
            }
            QPushButton:hover {
                background-color: #ff6b6b;
            }
        """)
        close_btn.clicked.connect(lambda checked, i=idx: self._close_tab_by_button(i))
        self.tab_widget.tabBar().setTabButton(idx, QTabBar.ButtonPosition.RightSide, close_btn)

    def _wrap_tab_page(self, idx, orientation, new_terminal, before=False):
        """把整个标签页内容包进一个新的 orientation 方向的 splitter，并加入 new_terminal。

        用于对整个标签页（而非单个小窗口）进行分屏：让 new_terminal 贯穿整个宽（垂直分屏）
        或整个高（水平分屏）。会更新 tab_splitters[idx] 指向新的外层 splitter，
        以保持 “标签页页面控件 == tab_splitters[idx]” 的不变式（detach / 重建映射依赖它）。
        before=True 时新控件放在旧页面**前面**（左/上），并入分屏按落点方向用。
        new_terminal 可以是任何控件（并入分屏时是另一页的整个 splitter）。
        """
        # 以 tab_widget 里的**真实页面**为准，而不是 tab_splitters[idx]。
        # 二者一旦不同步（跨窗口接管、重排后未及时重建映射），原实现会
        # removeTab 掉真实页面却把另一个页面插回来 —— 真实页面连同它的终端
        # 从界面上彻底消失，用户看到的就是「分屏把之前的终端关掉了」。
        actual_page = self.tab_widget.widget(idx)
        old_page = actual_page if actual_page is not None else self.tab_splitters.get(idx)
        if old_page is None:
            return None
        if self.tab_splitters.get(idx) is not old_page:
            logger.warning(
                "[Tabs] tab_splitters[%s] 与真实页面不一致，已按真实页面修正", idx)
            self.tab_splitters[idx] = old_page
        outer = self._styled_splitter(orientation)
        title = self.tab_widget.tabText(idx)
        was_current = self.tab_widget.currentIndex() == idx

        # 先把旧页面从 tab 中摘下（removeTab 不销毁控件，Python 引用仍在），再重组
        self.tab_widget.removeTab(idx)
        if before:
            outer.addWidget(new_terminal)
            outer.addWidget(old_page)
        else:
            outer.addWidget(old_page)
            outer.addWidget(new_terminal)
        old_page.show()

        self.tab_widget.insertTab(idx, outer, title)
        self._restore_tab_close_button(idx)
        if was_current:
            self.tab_widget.setCurrentIndex(idx)

        self.tab_splitters[idx] = outer

        if orientation == Qt.Orientation.Horizontal:
            size = outer.width() if outer.width() > 0 else 800
        else:
            size = outer.height() if outer.height() > 0 else 600
        outer.setSizes([size // 2, size // 2])
        return outer

    def _split_current_tab(self, whole_tab=False):
        """左右分屏。

        默认只分裂当前活动终端所在的小窗口；按住 Shift（whole_tab=True）时
        对整个标签页进行左右分屏，新终端贯穿整个高度。
        """
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        splitter = self._synced_tab_splitter(idx)
        if not splitter:
            return

        terminals = self.tab_terminals.get(idx, [])

        # 目标窗格先定下来：新分屏的工作目录要继承**它**的 cwd。
        # 用未校验的 active_terminal 会在多窗口串台时继承到别的窗口的目录。
        target = self._target_terminal_in_tab(terminals)

        current_cwd = None
        if target is not None and target.is_running():
            current_cwd = target.get_cwd()
        if not current_cwd:
            current_cwd = self.tab_cwds.get(idx, self._window_cwd)

        new_terminal = self._create_terminal()

        # 目标窗格：以真正持有焦点的终端为准（见 _target_terminal_in_tab）。
        # 不能直接信 self.active_terminal —— 多窗口下它可能被别的窗口的终端
        # 污染，那会导致「分裂了错误的窗格」，甚至误判成整页重组把真实页面
        # removeTab 掉（表现为「之前的终端被关掉了」）。
        split_whole = whole_tab or target is None

        if split_whole:
            # 对整个标签页左右分屏：新终端成为贯穿整高的一列
            top = splitter
            if top.orientation() == Qt.Orientation.Horizontal:
                top.addWidget(new_terminal)
                count = top.count()
                total_width = top.width()
                top.setSizes([total_width // count] * count)
            else:
                # 顶层是垂直方向，需要包裹整页才能让新列贯穿整高
                self._wrap_tab_page(idx, Qt.Orientation.Horizontal, new_terminal)
        else:
            # 只分裂当前活动终端所在的小窗口
            parent_widget = target.parent()
            if not isinstance(parent_widget, QSplitter):
                self.statusbar.showMessage(t("msg.cannot_find_container"), 3000)
                new_terminal.deleteLater()
                return

            parent_splitter = parent_widget
            terminal_index = parent_splitter.indexOf(target)

            if parent_splitter.orientation() == Qt.Orientation.Horizontal:
                # 父级已是水平方向：直接在活动终端右侧插入，把原终端的空间一分为二
                parent_sizes = parent_splitter.sizes()
                parent_splitter.insertWidget(terminal_index + 1, new_terminal)
                if terminal_index < len(parent_sizes):
                    orig = parent_sizes[terminal_index]
                    new_sizes = list(parent_sizes)
                    new_sizes[terminal_index] = orig // 2
                    new_sizes.insert(terminal_index + 1, orig - orig // 2)
                    parent_splitter.setSizes(new_sizes)
            else:
                # 父级是垂直方向：把原终端包裹进一个新的水平 splitter
                parent_sizes = parent_splitter.sizes()
                original_terminal = target
                horizontal_splitter = self._styled_splitter(Qt.Orientation.Horizontal)
                horizontal_splitter.addWidget(original_terminal)
                horizontal_splitter.addWidget(new_terminal)
                parent_splitter.insertWidget(terminal_index, horizontal_splitter)
                if parent_sizes and len(parent_sizes) == parent_splitter.count():
                    parent_splitter.setSizes(parent_sizes)
                h_width = horizontal_splitter.width() if horizontal_splitter.width() > 0 else 400
                horizontal_splitter.setSizes([h_width // 2, h_width // 2])

        # 更新终端列表
        self.tab_terminals[idx].append(new_terminal)
        self._refresh_pane_handles(idx)

        # 启动 shell 在当前终端的工作目录
        new_terminal.start_process([get_default_shell()], cwd=current_cwd)

        # 设置新终端为活动终端
        self.active_terminal = new_terminal
        new_terminal.setFocus()

        count = len(self.tab_terminals[idx])
        msg = "status.split_tab_done" if split_whole else "status.split_done"
        self.statusbar.showMessage(t(msg, count=count), 3000)

    def _split_vertical_current_terminal(self, whole_tab=False):
        """上下分屏。

        默认只分裂当前活动终端所在的小窗口；按住 Shift（whole_tab=True）时
        对整个标签页进行上下分屏，新终端贯穿整个宽度。
        """
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        splitter = self._synced_tab_splitter(idx)
        if not splitter:
            return

        terminals = self.tab_terminals.get(idx, [])

        # 获取当前终端的工作目录，回退到标签页的工作目录，再回退到窗口级别的工作目录
        # 目标窗格先定下来：新分屏的工作目录要继承**它**的 cwd。
        # 用未校验的 active_terminal 会在多窗口串台时继承到别的窗口的目录。
        target = self._target_terminal_in_tab(terminals)

        current_cwd = None
        if target is not None and target.is_running():
            current_cwd = target.get_cwd()
        if not current_cwd:
            current_cwd = self.tab_cwds.get(idx, self._window_cwd)

        new_terminal = self._create_terminal()

        # 目标窗格：以真正持有焦点的终端为准（见 _target_terminal_in_tab）。
        # 不能直接信 self.active_terminal —— 多窗口下它可能被别的窗口的终端
        # 污染，那会导致「分裂了错误的窗格」，甚至误判成整页重组把真实页面
        # removeTab 掉（表现为「之前的终端被关掉了」）。
        split_whole = whole_tab or target is None

        if split_whole:
            # 对整个标签页上下分屏：新终端成为贯穿整宽的一行
            top = splitter
            if top.orientation() == Qt.Orientation.Vertical:
                top.addWidget(new_terminal)
                count = top.count()
                total_height = top.height()
                top.setSizes([total_height // count] * count)
            else:
                # 顶层是水平方向，需要包裹整页才能让新行贯穿整宽
                self._wrap_tab_page(idx, Qt.Orientation.Vertical, new_terminal)
        else:
            # 只分裂当前活动终端所在的小窗口
            parent_widget = target.parent()
            if not isinstance(parent_widget, QSplitter):
                self.statusbar.showMessage(t("msg.cannot_find_container"), 3000)
                new_terminal.deleteLater()
                return

            parent_splitter = parent_widget
            terminal_index = parent_splitter.indexOf(target)
            parent_sizes = parent_splitter.sizes()

            if parent_splitter.orientation() == Qt.Orientation.Vertical:
                # 父级已是垂直方向：直接在活动终端下方插入，把原终端的空间一分为二
                parent_splitter.insertWidget(terminal_index + 1, new_terminal)
                if terminal_index < len(parent_sizes):
                    orig = parent_sizes[terminal_index]
                    new_sizes = list(parent_sizes)
                    new_sizes[terminal_index] = orig // 2
                    new_sizes.insert(terminal_index + 1, orig - orig // 2)
                    parent_splitter.setSizes(new_sizes)
            else:
                # 父级是水平方向：把原终端包裹进一个新的垂直 splitter
                original_terminal = target
                vertical_splitter = self._styled_splitter(Qt.Orientation.Vertical)
                vertical_splitter.addWidget(original_terminal)
                vertical_splitter.addWidget(new_terminal)
                parent_splitter.insertWidget(terminal_index, vertical_splitter)
                if parent_sizes and len(parent_sizes) == parent_splitter.count():
                    parent_splitter.setSizes(parent_sizes)
                v_height = vertical_splitter.height() if vertical_splitter.height() > 0 else 400
                vertical_splitter.setSizes([v_height // 2, v_height // 2])

        # 更新终端列表
        self.tab_terminals[idx].append(new_terminal)
        self._refresh_pane_handles(idx)

        # 启动新终端
        new_terminal.start_process([get_default_shell()], cwd=current_cwd)

        # 设置新终端为活动终端
        self.active_terminal = new_terminal
        new_terminal.setFocus()

        count = len(self.tab_terminals[idx])
        msg = "status.vsplit_tab_done" if split_whole else "status.vsplit_done"
        self.statusbar.showMessage(t(msg, count=count), 3000)

    def _collapse_singleton_splitter(self, splitter, idx):
        """若某个嵌套 splitter 关闭后只剩一个子组件，则解除这层嵌套：

        用唯一的子组件替换该 splitter，并继承它在父 splitter 中的位置和尺寸。
        这样剩下的分屏会自动扩展占满原来的区域，同时**完全不影响**父 splitter
        里其它分屏的尺寸。顶层标签页 splitter 不会被解除。
        """
        top = self.tab_splitters.get(idx)
        while (
            isinstance(splitter, QSplitter)
            and splitter is not top
            and splitter.count() == 1
        ):
            grandparent = splitter.parent()
            if not isinstance(grandparent, QSplitter):
                break
            child = splitter.widget(0)
            gp_index = grandparent.indexOf(splitter)
            gp_sizes = grandparent.sizes()  # 关闭前父级各分屏的尺寸，需原样保留
            # 把唯一子组件移动到父 splitter 中 splitter 原来的位置
            child.setParent(None)
            grandparent.insertWidget(gp_index, child)
            # 删除已空的嵌套 splitter
            splitter.setParent(None)
            splitter.deleteLater()
            # 恢复父 splitter 的尺寸分配（其它分屏宽/高保持不变）
            if len(gp_sizes) == grandparent.count():
                grandparent.setSizes(gp_sizes)
            # 继续向上检查（一般一层即可）
            splitter = grandparent

    def _close_current_split(self):
        """关闭当前聚焦的分屏终端。

        只在该终端所在的局部 splitter 范围内回收空间，空出的空间交给相邻分屏
        自动扩展，**不影响**其它 splitter / 分屏的尺寸。
        """
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        # 先校验索引映射（错位时 terminals 会是别的标签页的列表 → 关错终端）
        self._synced_tab_splitter(idx)
        terminals = self.tab_terminals.get(idx, [])
        if len(terminals) <= 1:
            # 只有一个终端时不能关闭，提示用户
            self.statusbar.showMessage(t("msg.cannot_close_only_terminal"), 3000)
            return

        # 找到当前活动的终端。绝不拿 terminals[-1] 顶包：活动终端不在本页
        # （典型是多窗口下的串台，或焦点在编辑器/文件树里）时那等于随机关掉
        # 一个别的终端——用户报告的「分屏把之前的终端关掉了」正是这条路径。
        terminal_to_close = self._target_terminal_in_tab(
            terminals, fallback_first=False)
        if terminal_to_close not in terminals:
            self.statusbar.showMessage(t("msg.cannot_close_only_terminal"), 3000)
            return

        # 记录被关闭终端所在的父 splitter 及其尺寸（只在这个局部范围内重新分配空间）
        parent = terminal_to_close.parent()
        parent_sizes = parent.sizes() if isinstance(parent, QSplitter) else None
        close_index = parent.indexOf(terminal_to_close) if isinstance(parent, QSplitter) else -1

        # 完整清理终端资源
        terminal_to_close.cleanup()

        # 从列表中移除
        terminals.remove(terminal_to_close)

        # 从分屏容器中移除并销毁
        terminal_to_close.setParent(None)
        terminal_to_close.deleteLater()

        # 在局部父 splitter 内，把空出的空间合并给相邻分屏（其它分屏尺寸不变）
        if isinstance(parent, QSplitter) and parent_sizes and 0 <= close_index < len(parent_sizes):
            freed = parent_sizes[close_index]
            new_sizes = parent_sizes[:close_index] + parent_sizes[close_index + 1:]
            if new_sizes:
                # 优先把空间给前一个分屏，否则给后一个
                give = close_index - 1 if close_index - 1 >= 0 else 0
                new_sizes[give] += freed
                if len(new_sizes) == parent.count():
                    parent.setSizes(new_sizes)

        # 若父 splitter 因此只剩一个子组件，解除这层嵌套，让剩余分屏自动扩展
        self._collapse_singleton_splitter(parent, idx)
        self._refresh_pane_handles(idx)

        # 更新活动终端为剩余的第一个
        if terminals:
            self.active_terminal = terminals[0]
            terminals[0].setFocus()

        self.statusbar.showMessage(t("status.close_split_done", count=len(terminals)), 3000)

    def _move_split_left(self):
        """将当前分屏与左边的分屏交换位置"""
        idx = self.tab_widget.currentIndex()
        if idx < 0:
            return

        splitter = self._synced_tab_splitter(idx)
        if not splitter:
            return

        # 找到当前活动终端在 splitter 中的索引
        terminal = self.active_terminal
        if not terminal:
            return

        # 查找终端（或其父 vertical splitter）在主 splitter 中的位置
        widget_in_splitter = terminal
        while widget_in_splitter.parent() != splitter:
            widget_in_splitter = widget_in_splitter.parent()
            if widget_in_splitter is None:
                return

        current_index = splitter.indexOf(widget_in_splitter)
        if current_index <= 0:
            self.statusbar.showMessage(t("status.move_split_left_fail"), 3000)
            return

        # 保存当前 sizes
        sizes = splitter.sizes()

        # 交换：把当前 widget 插入到左边位置
        splitter.insertWidget(current_index - 1, widget_in_splitter)

        # 交换 sizes
        sizes[current_index], sizes[current_index - 1] = sizes[current_index - 1], sizes[current_index]
        splitter.setSizes(sizes)

        # 同步 tab_terminals 列表中的顺序
        terminals = self.tab_terminals.get(idx, [])
        term_idx = terminals.index(terminal) if terminal in terminals else -1
        if term_idx > 0:
            terminals[term_idx], terminals[term_idx - 1] = terminals[term_idx - 1], terminals[term_idx]

        terminal.setFocus()
        self.statusbar.showMessage(t("status.move_split_left_done"), 3000)

    def _move_split_up(self):
        """在垂直分屏内，把当前终端与上方的兄弟交换位置"""
        terminal = self.active_terminal
        if not terminal:
            return
        parent_splitter = terminal.parent()
        if not isinstance(parent_splitter, QSplitter):
            self.statusbar.showMessage(t("status.move_split_up_fail"), 3000)
            return
        if parent_splitter.orientation() != Qt.Orientation.Vertical:
            self.statusbar.showMessage(t("status.move_split_up_fail"), 3000)
            return
        current_index = parent_splitter.indexOf(terminal)
        if current_index <= 0:
            self.statusbar.showMessage(t("status.move_split_up_fail"), 3000)
            return
        sizes = parent_splitter.sizes()
        parent_splitter.insertWidget(current_index - 1, terminal)
        sizes[current_index], sizes[current_index - 1] = sizes[current_index - 1], sizes[current_index]
        parent_splitter.setSizes(sizes)
        terminal.setFocus()
        self.statusbar.showMessage(t("status.move_split_up_done"), 3000)

    def _close_tab(self, index, auto_create_new=True):
        """关闭指定标签页

        Args:
            index: 要关闭的标签页索引
            auto_create_new: 如果关闭后没有标签页了，是否自动创建新的
        """
        # 杀终端前先校验索引映射：映射错位时 tab_terminals[index] 是**别的
        # 标签页**的终端列表，直接 cleanup 会把后面标签的 shell 杀掉、
        # 再被打上「已停止」标记——用户表现为「关掉前面的标签后，
        # 后面的标签命名/内容全错位」。
        self._synced_tab_splitter(index)

        # 先停止 OpenAI API 服务器（如果有）
        if self.openai_server_manager.is_running(index):
            self.openai_server_manager.stop_server(index)

        terminals = self.tab_terminals.get(index, [])
        for terminal in terminals:
            # 完整清理终端资源
            terminal.cleanup()

        # 结束会话
        session = self.tab_sessions.get(index)
        if session:
            self.session_manager.end_session()

        # 移除标签页。removeTab 会同步发出 currentChanged，而此时 tab_cwds 等映射
        # 还是旧索引 → _on_tab_changed 会用新索引查到被关 tab 的目录，导致
        # Directory/Current 回退到旧路径。先屏蔽信号，重建映射后再手动同步一次。
        self.tab_widget.blockSignals(True)
        try:
            self.tab_widget.removeTab(index)
        finally:
            self.tab_widget.blockSignals(False)

        # 更新映射（重建索引）
        self._rebuild_tab_mappings()

        # 映射已就绪，手动触发一次 tab 切换回调，让目录栏/导航面板同步到新当前 tab
        current = self.tab_widget.currentIndex()
        if current >= 0:
            self._on_tab_changed(current)

        # 如果没有标签页了，根据参数决定是否创建新的
        if self.tab_widget.count() == 0 and auto_create_new:
            self._add_new_tab()
            # 确保新 tab 的 UI 状态正确（启动按钮可用）
            self._update_running_state(False)

    def _close_current_tab(self):
        """关闭当前标签页"""
        self._close_tab(self.tab_widget.currentIndex())

    def _close_tab_or_window(self):
        """关闭当前分屏/标签页/窗口 (Cmd+W)

        优先级：
        0. 如果焦点在编辑器窗格里，关闭当前选中的编辑器窗格
        1. 如果当前标签页有多个分屏，关闭当前选中的分屏
        2. 如果只有一个分屏，关闭整个标签页
        3. 如果没有标签页了，关闭窗口
        """
        # 焦点在编辑器里 → Cmd+W 关闭当前选中的编辑器窗格（而不是终端标签）
        if self._focus_in_editor_area():
            if self.editor_area.close_focused_pane():
                return

        idx = self.tab_widget.currentIndex()

        if idx >= 0:
            terminals = self.tab_terminals.get(idx, [])
            if len(terminals) > 1:
                # 有多个分屏，关闭当前选中的分屏
                self._close_current_split()
            else:
                # 只有一个分屏，关闭整个标签页。
                # 若这是最后一个标签页，这一步会退出整个窗口 → 一律二次确认，
                # 避免一次误触把整个窗口（布局/会话）丢掉。有进程在跑时用更强措辞。
                if self.tab_widget.count() == 1:
                    has_running_process = any(t.is_running() for t in terminals)
                    msg = (t("msg.confirm_close_last_tab") if has_running_process
                           else t("msg.confirm_close_window"))
                    reply = QMessageBox.question(
                        self, t("msg.confirm_close_title"), msg,
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No  # 默认选择"否"，回车不会误关
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                    # 已经确认过了，self.close() 触发的 closeEvent 不必再问一次
                    self._force_closing = True

                self._close_tab(idx, auto_create_new=False)
                # 如果关闭后没有标签页了，关闭窗口
                if self.tab_widget.count() == 0:
                    self.close()
        else:
            # 没有标签页了，关闭整个窗口
            self.close()

    def _align_child_with_parent_geometry(self, new_window, abort_check=None):
        """让子窗口与本窗口逐像素重合（位置+尺寸），并持续校正 macOS 的异步微调。"""
        self._align_window_to_geometry(
            new_window, self.geometry(), self.isMaximized(), abort_check)

    @staticmethod
    def _align_window_to_geometry(new_window, target_geo, target_maximized,
                                  abort_check=None):
        """把窗口对齐到目标几何（位置+尺寸），并持续校正 macOS 的异步微调。

        拖拽分离（对齐父窗口）与升级/工作区恢复（对齐快照几何）共用。

        abort_check: 可选回调，返回 True 时立即停止校正并显形——拖拽分离场景
        下用户继续拖动（接管窗口位置）后，校正循环不能再和用户抢窗口。

        macOS 对新建原生窗口可能自行级联偏移、约束到屏幕（位置右移/尺寸压窄），
        且调整常发生在首帧之后——单次 setGeometry（或固定次数的少量重试）会被
        悄悄覆盖，这正是「新窗口宽度与父窗口对不齐」的根因。这里在 ~3s 内反复
        断言目标几何，连续 3 次确认无偏差才收手；每次几何就位后还把左侧栏宽度
        按共享值对齐——窗口被压窄再校正回来时 QSplitter 的比例缩放会破坏左侧栏
        的绝对像素宽度，而 _prime_left_panel_sync 的 300ms 兜底可能跑在几何
        稳定之前，必须在这里补一次。
        """
        parent_maximized = target_maximized
        logger.debug(
            "[align] start: parent_geo=%s parent_max=%s child_visible=%s",
            target_geo, parent_maximized, new_window.isVisible())

        # 未显示的子窗口（菜单 expand 路径）：先以全透明显示，几何对齐后再显形。
        # 系统（台前调度等）会先把新窗口放到错误位置、我们再纠正——这一来一回
        # 在隐形期间完成，用户看到的就是窗口直接出现在正确位置，没有跳变。
        # 拖拽路径窗口本就可见（要跟随光标），不走隐形逻辑。
        reveal_state = {'revealed': new_window.isVisible()}

        def _reveal():
            if reveal_state['revealed'] or sip.isdeleted(new_window):
                return
            reveal_state['revealed'] = True
            op = getattr(new_window, '_window_opacity', 100)
            if not (isinstance(op, int) and 10 <= op <= 100):
                op = 100
            new_window.setWindowOpacity(op / 100.0)

        if not reveal_state['revealed']:
            new_window.setWindowOpacity(0.0)
            # 兜底：即使始终对不齐，450ms 后也必须显形，绝不留下隐形窗口
            QTimer.singleShot(450, _reveal)

        loop_delay = 0
        if parent_maximized:
            # 先尝试直接继承最大化状态（对最大化几何做 setGeometry 经常不生效）。
            # 注意 showMaximized 可能被台前调度（Stage Manager）拦下而静默失败
            # ——子窗口拿到被压窄的普通几何且从未进入最大化状态。校正循环里
            # 会检测这种情况并退回逐像素几何断言。
            new_window.showMaximized()
        else:
            if new_window.isVisible():
                # 已显示的窗口（拖拽松手路径）：吸附从瞬移改为短促平滑滑移。
                # 尺寸差异（被系统压窄等）在动画前一次性补齐，动画只动位置；
                # 滑移结束后由校正循环断言精确几何，修掉可能的残余偏差。
                if new_window.size() != target_geo.size():
                    new_window.resize(target_geo.size())
                cur = new_window.geometry()
                fx = new_window.x() + (target_geo.x() - cur.x())
                fy = new_window.y() + (target_geo.y() - cur.y())
                if (fx, fy) != (new_window.x(), new_window.y()):
                    host_class(new_window)._slide_window_to(new_window, fx, fy)
                    loop_delay = 160  # 等滑移（140ms）结束再开始校正
                else:
                    new_window.setGeometry(target_geo)
            else:
                # 未显示的窗口（菜单 expand 路径）：不能在 show 前就把几何设成
                # 目标值——macOS 可能在首次显示时自行挪动/压窄原生窗口，而 Qt
                # 侧缓存仍等于目标值，之后的 setGeometry 全被当作「无变化」
                # 跳过，一次都不会真正下发，窗口永远校不回来。拖拽路径之所以
                # 可靠，正是因为窗口先显示在别处、对齐时必然发生一次真实的
                # 几何变化。这里模仿它：刻意偏移一点显示，让校正循环的首次
                # setGeometry 成为真实变化。
                ox, oy = host_class(new_window)._clamp_window_pos(
                    target_geo.x() + 24, target_geo.y() + 24,
                    target_geo.width(), target_geo.height(),
                    target_geo.center())
                if (ox, oy) == (target_geo.x(), target_geo.y()):
                    # 父窗口贴满可视区时偏移会被钳回原位，强制保留 1px 差异
                    oy += 1
                new_window.setGeometry(
                    ox, oy, target_geo.width(), target_geo.height())
                new_window.show()

        def _fix_left_width():
            """左侧栏宽度对齐到共享值（偏差 >2px 才动，避免抖动）。
            共享值按屏幕分桶，属性读取自动取新窗口所在屏的值。"""
            try:
                sw = new_window._saved_left_panel_width
                if isinstance(sw, int) and sw > 0 and hasattr(new_window, 'main_splitter'):
                    sizes = new_window.main_splitter.sizes()
                    if sizes and sizes[0] > 0 and abs(sizes[0] - sw) > 2:
                        new_window._apply_shared_left_panel_width(sw)
            except Exception:
                logger.debug("_fix_left_width: suppressed exception", exc_info=True)

        def _realign(attempt=0, stable=0):
            if sip.isdeleted(new_window):
                return
            if abort_check is not None and abort_check():
                # 用户已接管拖拽：停止校正并立即显形，不和用户抢窗口
                logger.debug("[align] aborted by user drag at tick %d", attempt)
                _reveal()
                return
            if new_window.isMaximized():
                # 子窗口确已最大化：几何由系统接管，只校左侧栏
                _fix_left_width()
                _reveal()
                stable += 1
            elif parent_maximized and attempt < 2:
                # showMaximized 可能尚未生效（异步/动画），先等两个快速 tick。
                # 若被台前调度拦下（子窗口始终进不了最大化状态），随即退回
                # 下面的逐像素几何断言——等待期越短，窗口停在系统给的错误
                # 位置上的可见时间就越短。
                stable = 0
            elif new_window.geometry() != target_geo:
                new_window.setGeometry(target_geo)
                stable = 0
            elif attempt < 8:
                # 前几个 tick 即使 Qt 侧已读到目标几何，也强制重新下发一次：
                # 刚显示的窗口可能被系统（台前调度 Stage Manager、屏幕约束等）
                # 挪走而 Qt 几何缓存未同步——看似已对齐实则没有，直接 setGeometry
                # 会被 Qt 当作「无变化」跳过。先把高度收 1px 制造真实变化再设回
                # 目标（向屏幕内收缩永远合法，不会反过来触发系统约束；位置偏移
                # 则可能顶到菜单栏被再次约束）。两次调用在同一事件循环内完成，
                # 不会渲染出中间态。
                new_window.resize(target_geo.width(), target_geo.height() - 1)
                new_window.setGeometry(target_geo)
                # 几何已到位（即便还在强制下发确认期）：左侧栏与显形都尽早做，
                # 不必等稳定期——同一事件循环内完成，显形时画面已是最终状态
                _fix_left_width()
                _reveal()
                stable = 0
            else:
                stable += 1
                _fix_left_width()
                _reveal()
            if stable >= 3:
                logger.debug("[align] settled at tick %d: child_geo=%s", attempt, new_window.geometry())
                return
            if attempt < 24:
                # 前期密集校正（30ms）让窗口尽快吸附到位，减少停在系统给的
                # 错误位置上的可见时间；后期放缓到 120ms 守护偶发的迟到微调。
                interval = 30 if attempt < 8 else 120
                QTimer.singleShot(interval, lambda: _realign(attempt + 1, stable))
            else:
                logger.debug("[align] gave up after tick %d: child_geo=%s target=%s",
                             attempt, new_window.geometry(), target_geo)
        QTimer.singleShot(loop_delay, _realign)

    def _detach_tab(self, index, global_pos=None, follow_drag=False, drop_pos=None):
        """将标签页分离为独立窗口（创建完整的 host_class(self)）

        drop_pos=None（右键菜单）：新窗口与父窗口逐像素重合地"原地出现"；
        drop_pos=光标位置（拖拽松手在空白处）：新窗口出现在松手处，标签栏
        正好落在光标下，尺寸取父窗口尺寸（父窗口最大化时取屏幕 60%）。
        follow_drag 参数已无作用，保留只为兼容旧调用。
        """
        # 唯一的标签页拆不出新窗口（拖拽路径里松手在空白处什么都不发生）
        if self.tab_widget.count() <= 1:
            return

        # 获取标签页标题
        title = self.tab_widget.tabText(index)

        taken = self._take_tab_out(index)
        if taken is None:
            return
        splitter = taken['splitter']
        terminals = taken['terminals']
        session = taken['session']
        tab_cwd = taken['cwd']

        # 创建完整的新 host_class(self)，传入 tab 数据
        initial_tab_data = {
            'splitter': splitter,
            'terminals': terminals,
            'session': session,
            'tab_name': title,
            'cwd': tab_cwd  # 传递工作目录
        }

        # 生成唯一的窗口标题
        host_class(self)._window_counter += 1
        window_title = f"{title} - Smart Terminal #{host_class(self)._window_counter}"

        new_window = host_class(self)(initial_tab_data=initial_tab_data, window_title=window_title)

        # 自动为新窗口选择一个未使用的颜色，方便区分
        available_color = self._get_available_window_color()
        new_window._set_window_color(available_color)

        # 继承父窗口面板的开关状态（Explorer / Git / Remote 互斥，开一个即可；
        # Log 独立），让分离出的窗口与父窗口外观一致，不造成认知负担
        try:
            if getattr(self, 'explorer_panel_visible', False):
                new_window._toggle_explorer_panel()
            elif getattr(self, 'git_panel_visible', False):
                new_window._toggle_git_panel()
            elif getattr(self, 'remote_panel_visible', False):
                new_window._toggle_remote_panel()
            if getattr(self, 'log_panel_visible', False):
                new_window._toggle_log_panel()
        except Exception:
            logger.debug("_detach_tab: suppressed exception", exc_info=True)

        # 扩展的是远程 SSH 终端 → 让新窗口的 Remote 面板自动连到同一主机，
        # 这样新窗口里终端 + SFTP 文件树都指向这台远端（终端已随 tab 搬过去）。
        ssh_host = None
        for _term in terminals:
            hc = getattr(_term, '_ssh_host_config', None)
            if hc is not None:
                ssh_host = hc
                break
        if ssh_host is not None:
            self._auto_connect_remote_in_window(new_window, ssh_host)

        # 新窗口初始尺寸先继承父窗口像素尺寸，让隐形对齐期间的首次显示就在
        # 正确大小附近；最大化状态等几何细节由下面的对齐流程接管
        try:
            new_window.resize(self.size())
        except Exception:
            logger.debug("_detach_tab: suppressed exception", exc_info=True)

        # 拖拽与菜单 expand 共用「原地出现」语义：新窗口直接与父窗口逐像素
        # 重合（隐形对齐后显形，见 _align_child_with_parent_geometry）。
        if drop_pos is not None:
            self._place_detached_at(new_window, drop_pos)
        else:
            self._align_child_with_parent_geometry(new_window)

        # 激活窗口
        new_window.raise_()
        new_window.activateWindow()

        # 添加到列表以跟踪（销毁后自动摘除）
        self._track_detached_window(new_window)

        if new_window.active_terminal:
            new_window.active_terminal.setFocus()

        # 如果主窗口没有标签页了，创建一个新的
        if self.tab_widget.count() == 0:
            self._add_new_tab()
            self._update_running_state(False)

    def _take_tab_out(self, index):
        """把第 index 页从本窗口摘下来（不销毁内容），返回它的全部随身数据。

        拆成新窗口（_detach_tab）与并入别的窗口（_adopt_tab_from）共用这一段：
        摘下后本窗口的映射已重建、目录栏已同步，终端处于暂停绘制状态，由
        接收方的 _add_new_tab（external 分支）恢复。摘不出来返回 None。
        """
        # 映射错位时 tab_terminals[index] 是别的标签页的终端，先校正
        self._synced_tab_splitter(index)
        splitter = self.tab_splitters.get(index)
        terminals = self.tab_terminals.get(index, [])
        session = self.tab_sessions.get(index)
        # 优先使用存储的工作目录（在删除映射之前获取）
        tab_cwd = self.tab_cwds.get(index)
        if not splitter or not terminals:
            return None

        # 停止 OpenAI API 服务器（如果有）
        if self.openai_server_manager.is_running(index):
            self.openai_server_manager.stop_server(index)

        # 在移除 tab 前，暂停所有终端的绘制更新，防止过渡期间在零尺寸 widget 上触发 paintEvent 导致 segfault
        for terminal in terminals:
            terminal.setUpdatesEnabled(False)
            terminal._cache_valid = False
            terminal._cache_pixmap = None

        # 从标签页移除（但不销毁内容）
        self.tab_widget.removeTab(index)

        # 清理映射
        for mapping in (self.tab_splitters, self.tab_terminals,
                        self.tab_sessions, self.tab_cwds):
            mapping.pop(index, None)

        # 重建映射
        self._rebuild_tab_mappings()

        # removeTab 触发的 currentChanged 发生在重建映射「之前」，那时读到的是错位的
        # tab_cwds（可能正好读成被分离标签的目录）。这里按重建后的正确索引再同步一次，
        # 让残留窗口的 Directory 输入框与 Current 标签都回到真正的当前标签目录。
        cur_idx = self.tab_widget.currentIndex()
        if cur_idx >= 0:
            self._on_tab_changed(cur_idx)

        # 如果没有存储的工作目录，尝试从终端获取或使用窗口默认值
        if not tab_cwd:
            tab_cwd = terminals[0].get_cwd() or self._window_cwd

        return {'splitter': splitter, 'terminals': terminals,
                'session': session, 'cwd': tab_cwd}

    # ---------- 跨窗口移动标签页：拖到另一个窗口的标签栏上松手即并入 ----------

    # 标签栏那一条的最小命中高度：标签本身只有二十几像素，拖着一整个窗口
    # 瞄准太费劲，放宽一点
    _TAB_DROP_STRIP_MIN_H = 40

    def _tab_drop_strip_rect(self) -> QRect:
        """本窗口标签栏那一整行（全局坐标）：拖着标签松手落在这里就并进来。"""
        tw = self.tab_widget
        bar = tw.tabBar()
        origin = tw.mapToGlobal(QPoint(0, 0))
        bar_top = bar.mapToGlobal(QPoint(0, 0)).y()
        h = max(bar.height(), self._TAB_DROP_STRIP_MIN_H)
        return QRect(origin.x(), bar_top, tw.width(), h)

    def _tab_insert_index_at(self, global_pos):
        """按光标横向位置算插入位置：落在某个标签的左半边插它前面，右半边插它
        后面；不在任何标签上则追加到末尾（None）。"""
        bar = self.tab_widget.tabBar()
        local = bar.mapFromGlobal(global_pos)
        i = bar.tabAt(local)
        if i < 0:
            return None
        return i if local.x() < bar.tabRect(i).center().x() else i + 1

    def _show_tab_drop_hint(self, kind='strip', zone=None):
        """盖一层半透明高亮：并成标签时罩住标签栏，并成分屏时罩住目标页的那半边。"""
        hint = getattr(self, '_tab_drop_hint', None)
        if hint is None or sip.isdeleted(hint):
            hint = QWidget(self.tab_widget)
            hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            hint.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            hint.setStyleSheet(
                "background-color: rgba(102, 126, 234, 0.35);"
                "border: 2px solid #667eea;")
            self._tab_drop_hint = hint
        if kind == 'split' and zone is not None:
            geo = self._split_zone_rect(zone)
            if geo is None:
                return
        elif kind == 'pane' and zone is not None:
            geo = self._pane_zone_rect(zone)
            if geo is None:
                return
        else:
            strip = self._tab_drop_strip_rect()
            top_left = self.tab_widget.mapFromGlobal(strip.topLeft())
            geo = QRect(top_left.x(), top_left.y(), strip.width(), strip.height())
        hint.setGeometry(geo)
        hint.show()
        hint.raise_()

    def _hide_tab_drop_hint(self):
        hint = getattr(self, '_tab_drop_hint', None)
        if hint is not None and not sip.isdeleted(hint):
            hint.hide()

    def _adopt_tab_from(self, src, src_index, insert_index=None):
        """把 src 窗口的第 src_index 页整个搬进本窗口（终端不重建、shell 不断）。

        返回并入后的索引，失败 -1。src 因此空了就把它关掉——它唯一的标签
        已经在这儿了，留个空壳没意义（没有终端在跑，关闭不会弹确认）。
        """
        if src is None or src is self:
            return -1
        title = src.tab_widget.tabText(src_index)
        taken = src._take_tab_out(src_index)
        if taken is None:
            return -1
        idx = self._add_new_tab(
            external_splitter=taken['splitter'],
            external_terminals=taken['terminals'],
            external_session=taken['session'],
            tab_name=title,
            tab_cwd=taken['cwd'],
        )
        if insert_index is not None and 0 <= insert_index < idx:
            # tabMoved → _on_tab_moved 会重建映射
            self.tab_widget.tabBar().moveTab(idx, insert_index)
            idx = insert_index
        if src.tab_widget.count() == 0:
            src._close_emptied_window()
        else:
            src._checkpoint_workspace()
        return idx

    def _close_emptied_window(self):
        """标签全被搬走后的窗口：不留空壳。推迟到下一轮事件循环，别在拖拽计
        时器的回调里同步销毁自己。"""
        self._checkpoint_workspace()

        def _close():
            if sip.isdeleted(self):
                return
            # 关闭被拒（编辑器里有未保存改动、用户点了取消）→ 别留一个零标签
            # 的死壳，补一个空白标签让窗口还能用
            if not self.close() and self.tab_widget.count() == 0:
                self._add_new_tab()
                self._update_running_state(False)

        QTimer.singleShot(0, _close)

    # ---------- 把另一个标签页并进来做分屏（同窗口 / 跨窗口） ----------

    def _merge_tab_into_split(self, src, src_index, dst_index, orientation,
                              before=False):
        """把 src 窗口的第 src_index 页整页并入本窗口第 dst_index 页做分屏。

        orientation=Horizontal 是左右、Vertical 是上下；before=True 时并入的
        那页放左/上。源页的终端原样搬过来（shell 不断、它自己的分屏结构保留），
        源标签消失；源窗口因此空了就关掉。返回是否成功。
        """
        if src is None or src_index < 0 or dst_index < 0:
            return False
        if src is self and src_index == dst_index:
            return False
        dst_page = self.tab_widget.widget(dst_index)
        if dst_page is None:
            return False
        title = self.tab_widget.tabText(dst_index)
        taken = src._take_tab_out(src_index)
        if taken is None:
            return False
        # 同窗口摘掉一页后，目标页的索引可能前移了
        dst_index = self.tab_widget.indexOf(dst_page)
        if dst_index < 0:
            return False
        top = self._synced_tab_splitter(dst_index)
        page = taken['splitter']
        terminals = taken['terminals']
        if src is not self:
            self._absorb_terminals(terminals)
        else:
            for term in terminals:
                term.setUpdatesEnabled(True)

        # 源页只有一个窗格时把它解包出来，别把单子 splitter 套进去
        # （关闭分屏后的 _collapse_singleton_splitter 只认嵌套层级）
        if isinstance(page, QSplitter) and page.count() == 1:
            child = page.widget(0)
            child.setParent(None)
            page.setParent(None)
            page.deleteLater()
            page = child

        if top is not None and top.orientation() == orientation:
            top.insertWidget(0 if before else top.count(), page)
            count = top.count()
            total = top.width() if orientation == Qt.Orientation.Horizontal else top.height()
            if total > 0:
                top.setSizes([total // count] * count)
        else:
            if self._wrap_tab_page(dst_index, orientation, page, before=before) is None:
                return False
        page.show()

        dst_terms = self.tab_terminals.setdefault(dst_index, [])
        if before:
            dst_terms[0:0] = terminals
        else:
            dst_terms.extend(terminals)
        if self.tab_sessions.get(dst_index) is None and taken['session'] is not None:
            self.tab_sessions[dst_index] = taken['session']
        self.tab_widget.setTabText(dst_index, title)
        self.tab_widget.setCurrentIndex(dst_index)
        self._refresh_pane_handles(dst_index)
        if terminals:
            self.active_terminal = terminals[0]
            terminals[0].setFocus()
        if src is not self and src.tab_widget.count() == 0:
            src._close_emptied_window()
        elif src is not self:
            src._checkpoint_workspace()
        self._checkpoint_workspace()
        self.statusbar.showMessage(
            t("status.merge_split_done", count=len(dst_terms)), 3000)
        return True

    def _merge_tab_into_current(self, tab_index, orientation):
        """右键菜单：把第 tab_index 页并入当前页做分屏。"""
        cur = self.tab_widget.currentIndex()
        if cur < 0 or cur == tab_index:
            return
        self._merge_tab_into_split(self, tab_index, cur, orientation)

    # 页面区四边各占这么大比例算"落到这一侧做分屏"（离哪条边近算哪边），
    # 只留正中一小块不算——落在那里 = 拆成新窗口
    _SPLIT_DROP_EDGE_RATIO = 0.4

    def _tab_page_rect(self):
        """当前标签页内容区（全局坐标）；没有页面则 None。"""
        page = self.tab_widget.currentWidget()
        if page is None or not page.isVisible():
            return None
        return QRect(page.mapToGlobal(QPoint(0, 0)), page.size())

    def _split_zone_at(self, global_pos):
        """global_pos 落在本窗口当前页的哪一侧：返回 (orientation, before)，
        中间区域或不在页面上返回 None。"""
        rect = self._tab_page_rect()
        if rect is None or not rect.contains(global_pos):
            return None
        r = self._SPLIT_DROP_EDGE_RATIO
        x = (global_pos.x() - rect.left()) / max(1, rect.width())
        y = (global_pos.y() - rect.top()) / max(1, rect.height())
        # 离哪条边最近就算哪一侧；都不够近（在中间）就不算
        cands = [(x, Qt.Orientation.Horizontal, True),
                 (1 - x, Qt.Orientation.Horizontal, False),
                 (y, Qt.Orientation.Vertical, True),
                 (1 - y, Qt.Orientation.Vertical, False)]
        dist, orientation, before = min(cands, key=lambda c: c[0])
        if dist > r:
            return None
        return (orientation, before)

    def _split_zone_rect(self, zone):
        """zone 对应的高亮区域（tab_widget 坐标）：目标页的那半边。"""
        rect = self._tab_page_rect()
        if rect is None:
            return None
        orientation, before = zone
        local = QRect(self.tab_widget.mapFromGlobal(rect.topLeft()), rect.size())
        if orientation == Qt.Orientation.Horizontal:
            half = QRect(local.left(), local.top(), local.width() // 2, local.height())
            if not before:
                half.moveLeft(local.left() + local.width() - half.width())
        else:
            half = QRect(local.left(), local.top(), local.width(), local.height() // 2)
            if not before:
                half.moveTop(local.top() + local.height() - half.height())
        return half

    def _tab_drop_hit_at(self, global_pos, dragging_index=None):
        """拖着标签松手会发生什么：(window, 'strip', None) 并成标签 / 同窗口重排，
        (window, 'split', (orientation, before)) 并入分屏，None 什么都不发生。

        以光标下**最上层**的窗口为准（QApplication.topLevelAt）——几何相交的
        窗口可能被别的窗口盖着；离屏/拿不到时退回按几何找。dragging_index 是
        本窗口正被拖的标签：落回自己当前页做分屏没有意义，不算命中。
        """
        cls = host_class(self)
        top = None
        try:
            top = QApplication.topLevelAt(global_pos)
        except Exception:
            top = None
        if isinstance(top, cls):
            candidates = [top]
        elif top is None:
            app = QApplication.instance()
            candidates = [w for w in (app.topLevelWidgets() if app else [])
                          if isinstance(w, cls)]
        else:
            return None   # 光标下是别的窗口（对话框/其它应用）
        for w in candidates:
            try:
                if (sip.isdeleted(w) or not w.isVisible() or w.isMinimized()
                        or getattr(w, '_closing_in_progress', False)):
                    continue
                if w._tab_drop_strip_rect().contains(global_pos):
                    return (w, 'strip', None)
                zone = w._split_zone_at(global_pos)
                if zone is not None:
                    if (w is self and dragging_index is not None
                            and self.tab_widget.currentIndex() == dragging_index):
                        return None
                    return (w, 'split', zone)
            except RuntimeError:
                continue   # C++ 对象已销毁
        return None

    # ---------- 拖标签：跟着光标的是一张标签「影子」，松手才决定去处 ----------
    #
    # 以前拖出阈值的一瞬间就整个建出一个新窗口跟手：建窗口要几百毫秒、
    # 满屏的窗口把目标全挡住、拖回原窗口还得穿过自己刚拆出的窗口——手感
    # 极差。现在拖动期间只有一张半透明的标签影子跟着光标，光标下是哪个窗口
    # 的标签栏 / 页面边缘就高亮哪里；松手时：标签栏 → 并成标签（同窗口 =
    # 重排），页面边缘 → 并入分屏，空白处 → 在松手处拆成新窗口。

    _TAB_DRAG_PREVIEW_OFFSET = QPoint(14, 10)

    def _begin_tab_drag(self, index, global_pos):
        """标签栏拖出阈值后进入影子拖拽，直到松开鼠标。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        bar = self.tab_widget.tabBar()
        try:
            pixmap = bar.grab(bar.tabRect(index))
        except Exception:
            pixmap = None
        preview = TabDragPreview(pixmap) if pixmap is not None and not pixmap.isNull() else None
        # 拖的是当前页：页面区先切到邻页——正在拖的这页不能和自己分屏，
        # 显示出来的必须是"要并进去的那页"。松手后它若还留在本窗口再切回来。
        dragged_page = self.tab_widget.widget(index)
        state = {'hit': None,
                 'restore': self._drag_switch_to_neighbor(index),
                 'dragged_page': dragged_page,
                 'hover_tab': -1, 'hover_since': 0.0}
        timer = QTimer()
        timer.setInterval(16)
        self._tab_drag_timer = timer   # prevent GC

        def _tick():
            if sip.isdeleted(self):
                timer.stop()
                if preview is not None:
                    preview.close()
                return
            pos = QCursor.pos()
            if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
                if preview is not None:
                    preview.move(pos + self._TAB_DRAG_PREVIEW_OFFSET)
                    if not preview.isVisible():
                        preview.show()
                hit = self._tab_drop_hit_at(pos, dragging_index=index)
                self._set_drag_hit(state, hit)
                self._drag_hover_switch_tab(state, hit, pos, index)
                return
            timer.stop()
            if preview is not None:
                preview.close()
            hit = state['hit']
            self._set_drag_hit(state, None)
            try:
                self._finish_tab_drag(index, pos, hit)
            finally:
                if state['restore']:
                    self._drag_restore_current(state['dragged_page'])

        timer.timeout.connect(_tick)
        timer.start()

    def _drag_switch_to_neighbor(self, index) -> bool:
        """拖的是当前页且还有别的页 → 页面区切到邻页（优先左边）。返回是否切了。"""
        count = self.tab_widget.count()
        if count <= 1 or self.tab_widget.currentIndex() != index:
            return False
        neighbor = index - 1 if index > 0 else index + 1
        self.tab_widget.setCurrentIndex(neighbor)
        return True

    def _drag_restore_current(self, dragged_page):
        """松手后被拖的页还在本窗口（重排 / 什么都没发生）→ 切回它。"""
        try:
            idx = self.tab_widget.indexOf(dragged_page)
        except RuntimeError:
            return
        if idx >= 0:
            self.tab_widget.setCurrentIndex(idx)

    _DRAG_HOVER_SWITCH_SECS = 0.35

    def _drag_hover_switch_tab(self, state, hit, pos, dragging_index):
        """拖着标签在本窗口标签栏上悬停到某个别的标签 ≥0.35s → 页面区切到它，
        这样可以先选"跟哪一页分屏"，再往下拖到页面边缘松手。"""
        tab = -1
        if hit is not None and hit[0] is self and hit[1] == 'strip':
            bar = self.tab_widget.tabBar()
            tab = bar.tabAt(bar.mapFromGlobal(pos))
            if tab == dragging_index:
                tab = -1
        now = time.monotonic()
        if tab != state['hover_tab']:
            state['hover_tab'] = tab
            state['hover_since'] = now
            return
        if (tab >= 0 and now - state['hover_since'] >= self._DRAG_HOVER_SWITCH_SECS
                and self.tab_widget.currentIndex() != tab):
            self.tab_widget.setCurrentIndex(tab)

    @staticmethod
    def _set_drag_hit(state, hit):
        """更新悬停命中：换目标时前一个的高亮收掉、新的亮起。"""
        prev = state.get('hit')
        if prev == hit:
            return
        if prev is not None and not sip.isdeleted(prev[0]):
            prev[0]._hide_tab_drop_hint()
        state['hit'] = hit
        if hit is not None:
            try:
                hit[0]._show_tab_drop_hint(hit[1], hit[2])
            except RuntimeError:
                pass

    def _finish_tab_drag(self, index, pos, hit):
        """松手：按命中位置决定去处。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        if hit is None:
            # 空白处：拆成新窗口，出现在松手处（唯一的标签拆不了，什么都不做）
            if self.tab_widget.count() > 1:
                self._detach_tab(index, pos, drop_pos=pos)
            return
        target, kind, zone = hit
        if sip.isdeleted(target):
            return
        if kind == 'strip':
            insert_at = target._tab_insert_index_at(pos)
            if target is self:
                count = self.tab_widget.count()
                ins = count if insert_at is None else insert_at
                to = ins - 1 if ins > index else ins
                if 0 <= to < count and to != index:
                    self.tab_widget.tabBar().moveTab(index, to)   # tabMoved → 重建映射
                return
            if target._adopt_tab_from(self, index, insert_at) < 0:
                return
        else:
            orientation, before = zone
            dst = target.tab_widget.currentIndex()
            if target is self and dst == index:
                return
            if not target._merge_tab_into_split(self, index, dst, orientation, before):
                return
        target.raise_()
        target.activateWindow()
        if target.active_terminal:
            target.active_terminal.setFocus()

    _DETACH_SHRINK_RATIO = 0.6

    def _place_detached_at(self, new_window, drop_pos):
        """把拆出的新窗口摆到松手处：标签栏落在光标下，尺寸随父窗口
        （父窗口最大化/全屏时取屏幕 60%，不然一出来又是满屏）。"""
        scr = QApplication.screenAt(drop_pos) or QApplication.primaryScreen()
        avail = scr.availableGeometry()
        if self.isMaximized() or self.isFullScreen():
            w = max(new_window.minimumWidth(), int(avail.width() * self._DETACH_SHRINK_RATIO))
            h = max(new_window.minimumHeight(), int(avail.height() * self._DETACH_SHRINK_RATIO))
        else:
            w, h = self.width(), self.height()
        # 光标到窗口顶部的距离 = 父窗口标签栏到父窗口顶部的距离（含标题栏）
        try:
            dy = self._tab_drop_strip_rect().center().y() - self.frameGeometry().y()
        except Exception:
            dy = 80
        x, y = host_class(self)._clamp_window_pos(
            drop_pos.x() - 60, drop_pos.y() - max(20, dy), w, h, drop_pos)
        new_window.resize(w, h)
        new_window.move(x, y)
        new_window.show()

    # ---------- 窗格把手：拖着把手把窗格挪到任意位置 ----------
    #
    # 标签页里有 ≥2 个窗格时每个窗格顶部有一条把手。拖它和拖标签一样是影子
    # 拖拽：光标下是哪个窗格就按离哪条边近高亮那半边，松手落到那一侧
    # （同窗口、跨窗口、跨标签页都行）；落到标签栏 → 变成独立标签页。

    def _refresh_pane_handles(self, idx):
        """第 idx 页：多窗格 → 每个窗格显示把手；单窗格 → 收起。"""
        terminals = self.tab_terminals.get(idx, [])
        for term in terminals:
            try:
                term.set_pane_handle_visible(len(terminals) > 1)
            except RuntimeError:
                pass  # 终端 C++ 对象已销毁

    def _pane_at(self, global_pos, exclude=None):
        """本窗口当前页里光标下的那个窗格（TerminalWidget），没有则 None。"""
        page = self.tab_widget.currentWidget()
        if page is None:
            return None
        from terminal_widget import TerminalWidget
        for term in page.findChildren(TerminalWidget):
            if term is exclude or not term.isVisible():
                continue
            rect = QRect(term.mapToGlobal(QPoint(0, 0)), term.size())
            if rect.contains(global_pos):
                return term
        return None

    @staticmethod
    def _edge_zone_in_rect(rect, global_pos):
        """rect 内离哪条边近：(orientation, before)。"""
        x = (global_pos.x() - rect.left()) / max(1, rect.width())
        y = (global_pos.y() - rect.top()) / max(1, rect.height())
        cands = [(x, Qt.Orientation.Horizontal, True),
                 (1 - x, Qt.Orientation.Horizontal, False),
                 (y, Qt.Orientation.Vertical, True),
                 (1 - y, Qt.Orientation.Vertical, False)]
        _, orientation, before = min(cands, key=lambda c: c[0])
        return (orientation, before)

    def _pane_zone_rect(self, zone):
        """(target_terminal, orientation, before) → 高亮区域（tab_widget 坐标）。"""
        target, orientation, before = zone
        if sip.isdeleted(target):
            return None
        local = QRect(self.tab_widget.mapFromGlobal(target.mapToGlobal(QPoint(0, 0))),
                      target.size())
        if orientation == Qt.Orientation.Horizontal:
            half = QRect(local.left(), local.top(), local.width() // 2, local.height())
            if not before:
                half.moveLeft(local.left() + local.width() - half.width())
        else:
            half = QRect(local.left(), local.top(), local.width(), local.height() // 2)
            if not before:
                half.moveTop(local.top() + local.height() - half.height())
        return half

    def _pane_drop_hit_at(self, global_pos, dragging):
        """拖着窗格松手会发生什么：(window, 'strip', None) 变独立标签，
        (window, 'pane', (目标窗格, orientation, before)) 挪到目标窗格那一侧，None 不动。"""
        cls = host_class(self)
        try:
            top = QApplication.topLevelAt(global_pos)
        except Exception:
            top = None
        if isinstance(top, cls):
            candidates = [top]
        elif top is None:
            app = QApplication.instance()
            candidates = [w for w in (app.topLevelWidgets() if app else []) if isinstance(w, cls)]
        else:
            return None
        for w in candidates:
            try:
                if (sip.isdeleted(w) or not w.isVisible() or w.isMinimized()
                        or getattr(w, '_closing_in_progress', False)):
                    continue
                if w._tab_drop_strip_rect().contains(global_pos):
                    return (w, 'strip', None)
                target = w._pane_at(global_pos, exclude=dragging)
                if target is not None:
                    rect = QRect(target.mapToGlobal(QPoint(0, 0)), target.size())
                    orientation, before = self._edge_zone_in_rect(rect, global_pos)
                    return (w, 'pane', (target, orientation, before))
            except RuntimeError:
                continue
        return None

    def _begin_pane_drag(self, terminal, global_pos):
        """把手拖过阈值：影子跟着光标，松手决定去处。"""
        if terminal is None or sip.isdeleted(terminal):
            return
        try:
            pixmap = terminal.grab().scaledToWidth(
                220, Qt.TransformationMode.SmoothTransformation)
        except Exception:
            pixmap = None
        preview = TabDragPreview(pixmap) if pixmap is not None and not pixmap.isNull() else None
        state = {'hit': None, 'hover_tab': -1, 'hover_since': 0.0}
        timer = QTimer()
        timer.setInterval(16)
        self._pane_drag_timer = timer   # prevent GC

        def _tick():
            if sip.isdeleted(self) or sip.isdeleted(terminal):
                timer.stop()
                if preview is not None:
                    preview.close()
                return
            pos = QCursor.pos()
            if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
                if preview is not None:
                    preview.move(pos + self._TAB_DRAG_PREVIEW_OFFSET)
                    if not preview.isVisible():
                        preview.show()
                hit = self._pane_drop_hit_at(pos, terminal)
                self._set_drag_hit(state, hit)
                # 悬停在本窗口标签栏的某个标签上 → 页面切过去，可以挪到别的标签页里
                self._drag_hover_switch_tab(state, hit, pos, -1)
                return
            timer.stop()
            if preview is not None:
                preview.close()
            hit = state['hit']
            self._set_drag_hit(state, None)
            try:
                self._finish_pane_drag(terminal, pos, hit)
            except Exception:
                logger.exception("[Tabs] finishing pane drag failed")

        timer.timeout.connect(_tick)
        timer.start()

    def _finish_pane_drag(self, terminal, pos, hit):
        if hit is None or sip.isdeleted(terminal):
            return
        target_win, kind, zone = hit
        if sip.isdeleted(target_win):
            return
        if kind == 'strip':
            ok = target_win._pop_pane_to_tab(self, terminal,
                                             target_win._tab_insert_index_at(pos))
        else:
            target, orientation, before = zone
            ok = target_win._move_pane_next_to(self, terminal, target, orientation, before)
        if ok:
            target_win.raise_()
            target_win.activateWindow()
            if not sip.isdeleted(terminal):
                target_win.active_terminal = terminal
                terminal.setFocus()

    def _find_tab_of_terminal_widget(self, terminal):
        for idx, terms in self.tab_terminals.items():
            if terminal in terms:
                return idx
        return -1

    def _detach_pane(self, terminal):
        """把窗格从它现在的位置摘出来（不销毁）：从 splitter 树、tab_terminals
        里都拿掉，空出来的嵌套层解掉。返回 (src_idx, 该页剩余终端数)。"""
        src_idx = self._find_tab_of_terminal_widget(terminal)
        parent = terminal.parent()
        parent_sizes = parent.sizes() if isinstance(parent, QSplitter) else None
        close_index = parent.indexOf(terminal) if isinstance(parent, QSplitter) else -1
        terminal.setParent(None)
        if isinstance(parent, QSplitter) and parent_sizes and 0 <= close_index < len(parent_sizes):
            freed = parent_sizes[close_index]
            new_sizes = parent_sizes[:close_index] + parent_sizes[close_index + 1:]
            if new_sizes:
                give = close_index - 1 if close_index - 1 >= 0 else 0
                new_sizes[give] += freed
                if len(new_sizes) == parent.count():
                    parent.setSizes(new_sizes)
        if src_idx >= 0:
            terms = self.tab_terminals.get(src_idx, [])
            if terminal in terms:
                terms.remove(terminal)
            if isinstance(parent, QSplitter):
                self._collapse_singleton_splitter(parent, src_idx)
            self._refresh_pane_handles(src_idx)
            return src_idx, len(terms)
        return -1, 0

    def _finish_pane_source(self, src_win, src_idx, remaining):
        """源页一个窗格都不剩了（跨窗口把最后一个挪走）→ 关掉那一页。"""
        if src_idx >= 0 and remaining == 0:
            try:
                src_win._close_tab(src_idx, auto_create_new=(src_win.tab_widget.count() <= 1))
            except Exception:
                logger.debug("_finish_pane_source: close tab failed", exc_info=True)

    def _move_pane_next_to(self, src_win, terminal, target, orientation, before) -> bool:
        """把 terminal 挪到 target 窗格的 orientation/before 那一侧（本窗口）。"""
        if terminal is target or sip.isdeleted(target):
            return False
        dst_idx = self._find_tab_of_terminal_widget(target)
        if dst_idx < 0:
            return False
        tparent = target.parent()
        if not isinstance(tparent, QSplitter):
            return False
        src_idx, remaining = src_win._detach_pane(terminal)
        if src_win is not self:
            self._absorb_terminals([terminal])
        # 重新找一次：摘除引起的嵌套解除可能换掉了 target 的父 splitter
        tparent = target.parent()
        if not isinstance(tparent, QSplitter):
            return False
        i = tparent.indexOf(target)
        if tparent.orientation() == orientation:
            sizes = tparent.sizes()
            tparent.insertWidget(i + (0 if before else 1), terminal)
            if i < len(sizes):
                orig = sizes[i]
                new_sizes = list(sizes)
                new_sizes[i] = orig // 2
                new_sizes.insert(i + 1, orig - orig // 2)
                if len(new_sizes) == tparent.count():
                    tparent.setSizes(new_sizes)
        else:
            sizes = tparent.sizes()
            inner = self._styled_splitter(orientation)
            target.setParent(None)
            if before:
                inner.addWidget(terminal)
                inner.addWidget(target)
            else:
                inner.addWidget(target)
                inner.addWidget(terminal)
            tparent.insertWidget(i, inner)
            if len(sizes) == tparent.count():
                tparent.setSizes(sizes)
            span = inner.width() if orientation == Qt.Orientation.Horizontal else inner.height()
            span = span if span > 0 else 400
            inner.setSizes([span // 2, span - span // 2])
            target.show()
        terminal.show()
        self.tab_terminals.setdefault(dst_idx, []).append(terminal)
        self._refresh_pane_handles(dst_idx)
        src_win._finish_pane_source(src_win, src_idx, remaining)
        self.statusbar.showMessage(t("status.pane_moved"), 3000)
        return True

    def _pop_pane_to_tab(self, src_win, terminal, insert_index=None) -> bool:
        """把窗格变成本窗口的一个独立标签页。"""
        if sip.isdeleted(terminal):
            return False
        src_idx = src_win._find_tab_of_terminal_widget(terminal)
        if src_idx >= 0 and len(src_win.tab_terminals.get(src_idx, [])) <= 1 and src_win is self:
            return False   # 本来就是独占一页
        title = terminal.get_split_label() or t("split.pane_default")
        cwd = None
        try:
            cwd = terminal.get_cwd()
        except Exception:
            pass  # 拿不到就用窗口目录
        src_idx, remaining = src_win._detach_pane(terminal)
        page = self._styled_splitter(Qt.Orientation.Horizontal)
        page.addWidget(terminal)
        idx = self._add_new_tab(external_splitter=page, external_terminals=[terminal],
                                external_session=None, tab_name=title, tab_cwd=cwd)
        if insert_index is not None and 0 <= insert_index < idx:
            self.tab_widget.tabBar().moveTab(idx, insert_index)
        src_win._finish_pane_source(src_win, src_idx, remaining)
        self.statusbar.showMessage(t("status.pane_popped"), 3000)
        return True

    def _rebuild_tab_mappings(self):
        """重建标签页映射"""
        new_splitters = {}
        new_terminals = {}
        new_sessions = {}
        new_cwds = {}
        old_to_new = {}  # 旧索引 -> 新索引，用于同步按 tab 索引存储的其他状态
        for i in range(self.tab_widget.count()):
            widget = self.tab_widget.widget(i)
            if widget:
                # 找到对应的旧映射
                for old_idx, splitter in self.tab_splitters.items():
                    if splitter is widget:
                        old_to_new[old_idx] = i
                        new_splitters[i] = splitter
                        new_terminals[i] = self.tab_terminals.get(old_idx, [])
                        new_sessions[i] = self.tab_sessions.get(old_idx)
                        new_cwds[i] = self.tab_cwds.get(old_idx, self._window_cwd)
                        break
        self.tab_splitters = new_splitters
        self.tab_terminals = new_terminals
        self.tab_sessions = new_sessions
        self.tab_cwds = new_cwds

        # 同步同样按 tab 索引存储的 OpenAI 服务器状态与 "查询后清除会话" 设置，
        # 否则关闭/分离左侧 tab 后这些 key 会指向错误的 tab。
        if hasattr(self, 'api_server_clear_after_query'):
            self.api_server_clear_after_query = {
                old_to_new[old_idx]: val
                for old_idx, val in self.api_server_clear_after_query.items()
                if old_idx in old_to_new
            }
        if hasattr(self, 'openai_server_manager'):
            self.openai_server_manager.remap_indices(old_to_new)

    def _on_tab_changed(self, index):
        """标签页切换时的回调"""
        # 切到别的 tab 也算"已查看"，清除提醒小标
        if self.isActiveWindow():
            self._clear_nav_attention()
        # 标签页徽章：切到即视为已查看，挂起的 等确认/已完成 回落为运行状态点
        self._clear_tab_pending_badge(index)
        terminals = self.tab_terminals.get(index, [])
        if terminals:
            # 设置第一个终端为活动终端
            self.active_terminal = terminals[0]
            terminals[0].setFocus()
            # 更新状态栏 - 检查是否有任何终端在运行
            any_running = any(t.is_running() for t in terminals)
            self._update_running_state(any_running)
            self._update_stats()

        # 更新窗口标题为当前 tab 名称
        self._update_window_title_from_tab(index)

        # 同步导航面板到当前标签页的工作目录
        tab_cwd = self.tab_cwds.get(index)
        if not tab_cwd and terminals:
            tab_cwd = terminals[0].get_cwd()
        if not tab_cwd:
            tab_cwd = self._window_cwd
        if tab_cwd and os.path.isdir(tab_cwd):
            cwd_changed = (tab_cwd != self._window_cwd)
            self._window_cwd = tab_cwd
            if hasattr(self, 'current_dir_label'):
                self.current_dir_label.setText(t("dir.current", cwd=tab_cwd))
                self.current_dir_label.setToolTip(tab_cwd)
            # 让 "Directory:" 输入框也跟随当前标签页（之前只更新 Current 标签，
            # 导致分离标签页后输入框仍停留在被分离标签的目录上、与 Current 不一致）。
            if hasattr(self, 'working_dir_combo'):
                self.working_dir_combo.blockSignals(True)
                self.working_dir_combo.setCurrentText(tab_cwd)
                self.working_dir_combo.blockSignals(False)
            if hasattr(self, 'explorer_panel') and self.explorer_panel_visible:
                self.explorer_panel.set_root_path(tab_cwd)
            if hasattr(self, 'git_panel') and self.git_panel_visible:
                self.git_panel.set_repository(tab_cwd)
            # 本地命令是「目录级」的：tab 切到不同目录时必须重载，否则 local_presets
            # 仍是上一个目录的内容，而保存路径已指向当前目录 → 跨文件夹串写/覆盖。
            # （local_presets 始终是「磁盘加载」或「刚保存」的状态，无未落盘的内存修改，
            #   故重载是安全的，不会丢失编辑。）
            if cwd_changed:
                self._load_local_commands()
        # 工作区快照：当前标签变化（恢复时要记住停在哪个 tab）
        self._checkpoint_workspace()

    def _on_tab_session_ended(self, terminal):
        """某个标签页的会话结束"""
        # 找到对应的标签页索引
        for idx, terminals in self.tab_terminals.items():
            if terminal in terminals:
                # 检查是否所有终端都停止了
                all_stopped = all(not t.is_running() for t in terminals)
                if all_stopped:
                    # 更新标签页标题，保留原名称，添加已停止标记
                    current_title = self.tab_widget.tabText(idx)
                    stopped_mark = t("status.tab_stopped")
                    if stopped_mark not in current_title:
                        self.tab_widget.setTabText(idx, f"{current_title} {stopped_mark}")
                    # 徽章：后台标签的会话结束 → 绿点提醒；正在看的 → 清点
                    page = self.tab_widget.widget(idx)
                    if page is not None:
                        if idx != self.tab_widget.currentIndex() or not self.isActiveWindow():
                            page._badge_pending = 'done'
                        else:
                            page._badge_pending = None
                    self._refresh_tab_badge(idx)
                break

        # 如果是当前标签页，更新状态
        if terminal is self.terminal:
            self._on_session_ended()

    def _update_window_title_from_tab(self, index=None):
        """根据当前 tab 更新窗口标题"""
        if index is None:
            index = self.tab_widget.currentIndex()
        if index >= 0:
            tab_name = self.tab_widget.tabText(index)
            # 去掉 stopped 后缀
            stopped_mark = t("status.tab_stopped")
            if f" {stopped_mark}" in tab_name:
                tab_name = tab_name.replace(f" {stopped_mark}", "")
            new_title = f"{tab_name} - Smart Terminal"
            if new_title != self.windowTitle():
                self.setWindowTitle(new_title)
                # 立即刷新导航面板，让列表项即时跟随当前激活的 tab（本地/远程），
                # 不必等 5 秒轮询。
                try:
                    host_class(self)._broadcast_navigator_refresh()
                except Exception:
                    logger.debug("_update_window_title_from_tab: suppressed exception", exc_info=True)

    def _next_tab(self):
        """切换到下一个标签页"""
        count = self.tab_widget.count()
        if count > 1:
            current = self.tab_widget.currentIndex()
            self.tab_widget.setCurrentIndex((current + 1) % count)

    def _prev_tab(self):
        """切换到上一个标签页"""
        count = self.tab_widget.count()
        if count > 1:
            current = self.tab_widget.currentIndex()
            self.tab_widget.setCurrentIndex((current - 1) % count)

    def _capture_explorer_layout(self):
        """记录当前资源管理器/编辑器的尺寸用于下次还原

        - 仅在用户能看到完整布局时记录（相关 widget 都未折叠）
        - 通过 splitterMoved 信号触发，由 setSizes 引发的程序性变更也会进入此处，
          但目标布局各项均 > 0，记录无害
        """
        if not hasattr(self, 'editor_area'):
            return

        # 弹簧动画/程序性设置尺寸期间不记忆，避免把临时的偏置布局写进记忆值
        if self._applying_spring:
            return

        editor_in_main = self.main_splitter.indexOf(self.editor_area) >= 0
        editor_in_internal = self.explorer_splitter.indexOf(self.editor_area) >= 0

        # 1) 编辑器在 explorer_splitter 中（上下分屏）— 记录内部分屏尺寸
        if editor_in_internal and self.editor_area.isVisible():
            isizes = self.explorer_splitter.sizes()
            if len(isizes) == 2 and isizes[0] > 0 and isizes[1] > 0:
                self._saved_explorer_internal_sizes = list(isizes)

        # 2) main_splitter 处理（左面板宽度始终是 sizes[0]）
        msizes = self.main_splitter.sizes()
        left_visible = (
            getattr(self, 'explorer_panel_visible', False)
            or getattr(self, 'git_panel_visible', False)
            or getattr(self, 'remote_panel_visible', False)
        )

        if editor_in_main and self.editor_area.isVisible() and len(msizes) == 4:
            # 4 widget: 左面板 + 编辑器 + 终端 + 日志
            if msizes[0] > 0 and msizes[1] > 0 and msizes[2] > 0:
                self._saved_explorer_main_sizes = list(msizes)
                self._set_left_panel_width(msizes[0])
        elif (not editor_in_main) and left_visible and len(msizes) >= 3 and msizes[0] > 0:
            # 3 widget: 左面板 + 终端 + 日志（无编辑器）
            self._set_left_panel_width(msizes[0])

    def _resolve_main_splitter_sizes_with_editor(self):
        """计算编辑器在 main_splitter 中时的目标尺寸（优先使用记忆值）

        QSplitter 会按实际宽度对 setSizes 入参做比例归一化，因此各项之和必须
        等于 splitter 的实际宽度，才能让记忆的绝对像素值被原样还原。
        """
        log_width = 300 if self.log_panel_visible else 0
        saved_left = self._saved_left_panel_width
        saved_left = saved_left if isinstance(saved_left, int) and saved_left > 0 else None
        total = max(self.main_splitter.width(), 1000)

        saved = self._saved_explorer_main_sizes
        if saved and len(saved) == 4 and saved[0] > 0 and saved[1] > 0 and saved[2] > 0:
            left = saved_left if saved_left is not None else saved[0]
            editor = saved[1]
            terminal = max(100, total - left - editor - log_width)
            return [left, editor, terminal, log_width]
        # 默认值：左面板 300（或记忆值）, 编辑器 400, 其余给终端
        left = saved_left if saved_left is not None else 300
        editor = 400
        terminal = max(100, total - left - editor - log_width)
        return [left, editor, terminal, log_width]

    def _resolve_explorer_splitter_sizes_with_editor(self):
        """计算编辑器在 explorer_splitter 中时的目标尺寸（优先使用记忆值）"""
        saved = self._saved_explorer_internal_sizes
        if saved and len(saved) == 2 and saved[0] > 0 and saved[1] > 0:
            return list(saved)
        return [200, 400]

    def _place_editor_in_main_splitter(self):
        """将编辑器放到 main_splitter 中（左右分屏模式）"""
        if self.main_splitter.indexOf(self.editor_area) >= 0:
            # 已经在 main_splitter 中，只需确保可见并调整大小
            self.editor_area.show()
        else:
            # 从 explorer_splitter 中取出
            self.editor_area.setParent(None)
            self.editor_area.show()
            # 插入到 main_splitter 的 index 1（left_panel 和 tab_widget 之间）
            self.main_splitter.insertWidget(1, self.editor_area)

        self.main_splitter.setSizes(self._resolve_main_splitter_sizes_with_editor())

        # explorer_splitter 中只剩文件树，让它占满
        self.explorer_splitter.setSizes([400, 0])

    def _place_editor_in_explorer_splitter(self):
        """将编辑器放到 explorer_splitter 中（上下分屏模式）"""
        if self.explorer_splitter.indexOf(self.editor_area) >= 0:
            # 已经在 explorer_splitter 中，只需确保可见并调整大小
            self.editor_area.show()
        else:
            # 从 main_splitter 中取出
            self.editor_area.setParent(None)
            self.editor_area.show()
            # 放回 explorer_splitter
            self.explorer_splitter.addWidget(self.editor_area)

        self.explorer_splitter.setSizes(self._resolve_explorer_splitter_sizes_with_editor())

        # 恢复 main_splitter 正常比例
        self._update_splitter_sizes()

    def _toggle_editor_collapsed(self):
        """收起 / 展开已打开的文件区（Ctrl+E，可在「键盘快捷键」里改）。

        与「关闭」不同：收起只是隐藏 editor_area 腾出屏幕空间，已打开的文件和
        split 分屏结构仍保留在内存中，再次触发即原样展开。没有任何已打开文件
        时不做切换，仅在状态栏提示。
        """
        if not hasattr(self, 'editor_area'):
            return
        if not self._editor_has_any_file():
            self.statusbar.showMessage(t("status.editor_no_file"), 2000)
            return

        if self.editor_area.isVisible():
            # 收起：隐藏并把空间还给资源管理器/终端（保留文件，不清除编辑标记）
            self.editor_area.hide()
            if self.explorer_splitter.indexOf(self.editor_area) < 0:
                self.editor_area.setParent(None)
                self.explorer_splitter.addWidget(self.editor_area)
                self.editor_area.hide()
            self.explorer_splitter.setSizes([400, 0])
            self._update_splitter_sizes()
        else:
            # 展开：按当前分屏方向重新放置并显示
            if self._explorer_split_horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_explorer_splitter()
            # 先弹宽编辑器，再移焦点。顺序很重要：_apply_spring 会先把
            # _spring_current_side 置为 'editor'，这样紧接着 setFocus 触发的
            # focusChanged → _on_focus_changed_for_spring 会因「目标侧已是 editor」
            # 提前返回，不会再 stop/重启一次动画（否则动画「起步即被打断」会卡一下）。
            if self._spring_applicable():
                self._apply_spring('editor')
            # 把键盘焦点移到编辑器活动窗格。否则从终端用 Cmd+E 展开后焦点仍留在
            # 终端，与「编辑器被弹宽」的状态不一致：随后点击终端因焦点未变化而不
            # 触发 focusChanged，弹簧无法把终端展宽。聚焦编辑器后状态一致，再点
            # 终端会正常 focusChanged → 弹宽终端。
            pane = self.editor_area.active_pane
            if pane is not None:
                pane.editor.setFocus()

    def _on_explorer_split_orientation_changed(self, state):
        """切换资源管理器与编辑器的分屏方向"""
        horizontal = (state == Qt.CheckState.Checked.value)
        self._explorer_split_horizontal = horizontal

        # 如果编辑器正在显示，立即切换位置
        if hasattr(self, 'editor_area') and self.editor_area.isVisible():
            if horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_explorer_splitter()
        # 切回上下分屏时弹簧失去意义，重置已展开侧标记
        if not horizontal:
            self._spring_current_side = None

    def _on_splitter_drag_tick(self):
        """splitterMoved 的拖拽流识别：首拍开启终端快速渲染，之后每拍续命静默定时器。

        手动拖分隔条没有「开始/结束」信号，只能由连续的 splitterMoved 推断：
        静默 160ms 视为松手。弹簧动画期间的 setSizes 也会发 splitterMoved，
        但动画自己管理 fast_resize（_applying_spring 置位），跳过。
        被其它窗口同步宽度时（_applying_shared_left_width）同样是连续 setSizes 流,
        正需要快速渲染，故不跳过。
        """
        if self._applying_spring:
            return
        if not self._splitter_drag_active:
            self._splitter_drag_active = True
            self._set_terminals_fast_resize(True)
        self._splitter_drag_settle.start()

    def _end_splitter_drag_fast_resize(self):
        """拖拽流静默：恢复终端清晰渲染（按最终尺寸整屏重建一次）。"""
        if not self._splitter_drag_active:
            return
        self._splitter_drag_active = False
        # 拖拽触发 spring 门控翻转时弹簧动画可能正在进行并已接管 fast_resize，
        # 让动画的 finished 回调去恢复，这里不抢着关。
        if self._spring_anim is None:
            self._set_terminals_fast_resize(False)
        # 拖拽期间挂起的左侧栏宽度在此一次性广播给其它窗口
        # （见 _set_left_panel_width：拖拽中不实时联动，避免堵死事件循环）
        self._left_width_broadcast_timer.stop()
        self._flush_left_width_broadcast()
        # 被同步窗口在同步期间跳过了 spring 门控判定（_update_spring_width_gate
        # 对 _applying_shared_left_width 早退），静默后按最终宽度补判一次
        self._update_spring_width_gate()

    def _resolve_remote_splitter_sizes_with_editor(self):
        """计算编辑器在 remote_splitter 中时的目标尺寸（优先使用记忆值）"""
        saved = self._saved_remote_internal_sizes
        if saved and len(saved) == 2 and saved[0] > 0 and saved[1] > 0:
            return list(saved)
        return [200, 400]

    def _place_editor_in_remote_splitter(self):
        """将编辑器放到 remote_splitter 中（Remote 上下分屏模式）"""
        if self.remote_splitter.indexOf(self.editor_area) >= 0:
            self.editor_area.show()
        else:
            self.editor_area.setParent(None)
            self.editor_area.show()
            self.remote_splitter.addWidget(self.editor_area)

        self.remote_splitter.setSizes(self._resolve_remote_splitter_sizes_with_editor())
        # 恢复 main_splitter 正常比例（编辑器不在 main_splitter 里）
        self._update_splitter_sizes()

    def _capture_remote_layout(self):
        """记录 remote_splitter 的内部尺寸（上下分屏），供下次还原。"""
        if not hasattr(self, 'editor_area') or not hasattr(self, 'remote_splitter'):
            return
        if self.remote_splitter.indexOf(self.editor_area) >= 0 and self.editor_area.isVisible():
            isizes = self.remote_splitter.sizes()
            if len(isizes) == 2 and isizes[0] > 0 and isizes[1] > 0:
                self._saved_remote_internal_sizes = list(isizes)

    def _on_remote_split_orientation_changed(self, state):
        """切换 Remote 树与编辑器的分屏方向"""
        horizontal = (state == Qt.CheckState.Checked.value)
        self._remote_split_horizontal = horizontal

        # 仅当编辑器正显示且 Remote 面板可见时，立即切换位置
        if (hasattr(self, 'editor_area') and self.editor_area.isVisible()
                and getattr(self, 'remote_panel_visible', False)):
            if horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_remote_splitter()

    def _update_splitter_sizes(self):
        """更新分割器大小"""
        left_visible = (
            self.explorer_panel_visible
            or self.git_panel_visible
            or getattr(self, 'remote_panel_visible', False)
        )
        saved_left = self._saved_left_panel_width
        saved_left = saved_left if isinstance(saved_left, int) and saved_left > 0 else None
        if left_visible:
            left_width = saved_left if saved_left is not None else 300
        else:
            left_width = 0
        log_width = 300 if self.log_panel_visible else 0

        # 检查编辑器是否在 main_splitter 中（左右分屏模式，splitter 有 4 个 widget）
        editor_in_main = hasattr(self, 'editor_area') and self.main_splitter.indexOf(self.editor_area) >= 0
        if editor_in_main:
            self.main_splitter.setSizes(self._resolve_main_splitter_sizes_with_editor())
            # 上面的 setSizes 用的是记忆/默认比例，会把弹簧展开的一侧打回去。
            # 这条路径不止面板切换会走：从别的程序切回窗口时 changeEvent 的
            # 跨窗口左侧栏对齐、其它窗口拖侧栏的广播同样到这。而此类重排不挪
            # 键盘焦点——点击已聚焦的编辑器不会再发 focusChanged，弹簧无法
            # 自愈，表现为「切回窗口后点编辑框反而更窄」（Ubuntu 点击即激活
            # 尤其常见）。统一在重排后按原侧无动画恢复弹簧比例。
            self._reconcile_spring_after_layout_change()
        elif left_width > 0 or log_width > 0:
            # 用 splitter 实际宽度作为总和，让 left_width 被原样保留（参见 _resolve... 的注释）
            total = max(self.main_splitter.width(), 1000)
            terminal_width = max(100, total - left_width - log_width)
            self.main_splitter.setSizes([left_width, terminal_width, log_width])
        else:
            # 如果都隐藏，让终端占满
            self.main_splitter.setSizes([0, 1000, 0])

    def open_directory_tab(self, dir_path: str):
        """在本窗口新开一个标签并在指定目录直接起会话。

        入口：macOS FileOpen 事件（Finder 快速操作/拖到 Dock）。
        复用工作目录历史的快速启动流程（建 tab + 自动启动当前预设）。
        """
        if not dir_path or not os.path.isdir(dir_path):
            return
        self._quick_launch_with_dir(dir_path)
        # 从 Finder 触发时应用可能在后台，把窗口带到前面
        self.raise_()
        self.activateWindow()

    def _show_tab_context_menu(self, pos):
        """显示 Tab 右键菜单"""
        tab_bar = self.tab_widget.tabBar()
        tab_index = tab_bar.tabAt(pos)

        if tab_index < 0:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2d2d44;
                color: #eaeaea;
                border: 1px solid #3d3d5c;
                border-radius: 4px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 3px;
            }
            QMenu::item:selected {
                background-color: #667eea;
            }
            QMenu::item:disabled {
                color: #666;
            }
        """)

        # 扩展为新窗口（等同拖出标签，但不需要手动拖拽）—— 放最上面，最常用
        detach_action = menu.addAction(t("tab.detach"))
        detach_action.setEnabled(self.tab_widget.count() > 1)
        detach_action.triggered.connect(
            lambda: self._detach_tab(tab_index, None, follow_drag=False))

        # 把这个标签并入当前标签做分屏（左右 / 上下）——两个标签合成一页看
        merge_menu = menu.addMenu(t("tab.merge_split_menu"))
        merge_menu.setEnabled(self.tab_widget.count() > 1
                              and tab_index != self.tab_widget.currentIndex())
        merge_h = merge_menu.addAction(t("tab.merge_split_h"))
        merge_h.triggered.connect(
            lambda: self._merge_tab_into_current(tab_index, Qt.Orientation.Horizontal))
        merge_v = merge_menu.addAction(t("tab.merge_split_v"))
        merge_v.triggered.connect(
            lambda: self._merge_tab_into_current(tab_index, Qt.Orientation.Vertical))

        menu.addSeparator()

        # 切换工作目录到该 tab 终端的当前路径
        switch_path_action = menu.addAction(t("tab.switch_to_path"))
        switch_path_action.triggered.connect(lambda: self._switch_dir_to_tab_path(tab_index))

        # OpenAI API 服务器选项
        is_server_running = self.openai_server_manager.is_running(tab_index)

        if is_server_running:
            port = self.openai_server_manager.get_port(tab_index)
            stop_action = menu.addAction(t("openai.stop_server", port=port))
            stop_action.triggered.connect(lambda: self._stop_openai_server(tab_index))

            # 复制 API URL
            copy_url_action = menu.addAction(t("openai.copy_url"))
            copy_url_action.triggered.connect(lambda: self._copy_api_url(port))

            # 每次 Query 后清除会话
            clear_after = self.api_server_clear_after_query.get(tab_index, False)
            clear_action = menu.addAction(t("openai.clear_session"))
            clear_action.setCheckable(True)
            clear_action.setChecked(clear_after)
            clear_action.triggered.connect(lambda checked: self._toggle_clear_after_query(tab_index, checked))
        else:
            start_action = menu.addAction(t("openai.set_as_server"))
            start_action.triggered.connect(lambda: self._show_openai_server_dialog(tab_index))

        menu.addSeparator()

        # 重命名标签页（可复用历史名称）
        rename_action = menu.addAction(t("tab.rename"))
        rename_action.triggered.connect(lambda: self._rename_tab(tab_index))

        menu.addSeparator()

        # 关闭标签页
        close_action = menu.addAction(t("tab.close"))
        close_action.triggered.connect(lambda: self._close_tab(tab_index))

        menu.exec(tab_bar.mapToGlobal(pos))

    def _apply_tab_name(self, index, name):
        """统一应用标签名：非空则「锁定」为自定义名，留空则解除锁定恢复默认编号。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        name = (name or "").strip()
        page = self.tab_widget.widget(index)
        if name:
            if page is not None:
                page._custom_tab_name = name  # 锁定标记：存在该属性即视为用户自定义
            self.tab_widget.setTabText(index, name)
            self._remember_label_name(name)
        else:
            # 清除自定义名 → 解除锁定，恢复默认编号命名
            if page is not None:
                page._custom_tab_name = None
            self.tab_widget.setTabText(index, t("terminal.default_name", n=index + 1))
        self._update_window_title_from_tab(index)
        self._save_config()

    def _switch_dir_to_tab_path(self, index):
        """把工作目录切换到该 tab 终端进程的当前路径（右键菜单入口）。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        terminals = self.tab_terminals.get(index, [])
        # 优先用该 tab 内当前激活的终端，否则退回第一个
        terminal = None
        if self.active_terminal and self.active_terminal in terminals:
            terminal = self.active_terminal
        elif terminals:
            terminal = terminals[0]
        cwd = terminal.get_cwd() if terminal else None
        if not cwd or not os.path.isdir(cwd):
            self.statusbar.showMessage(t("tab.switch_to_path_unavailable"), 3000)
            return
        # 复用现有切换逻辑：填入输入框后应用
        self.working_dir_combo.setCurrentText(cwd)
        self._apply_working_dir()

    def _rename_tab(self, index):
        """通过对话框重命名标签页（右键菜单入口，可从历史复用名称）。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        page = self.tab_widget.widget(index)
        current = getattr(page, '_custom_tab_name', None) or self.tab_widget.tabText(index)
        name, ok = self._prompt_label_name(t("tab.rename_title"), t("tab.rename_prompt"), current)
        if not ok:
            return
        self._apply_tab_name(index, name)

    def _begin_inline_tab_rename(self, index):
        """双击标签 → 在标签上就地弹出输入框直接编辑。"""
        if index < 0 or index >= self.tab_widget.count():
            return
        # 已有正在编辑的输入框先收掉，避免重叠
        self._discard_inline_tab_rename()
        tab_bar = self.tab_widget.tabBar()
        page = self.tab_widget.widget(index)
        current = getattr(page, '_custom_tab_name', None) or tab_bar.tabText(index)

        editor = InlineRenameEdit(tab_bar)
        editor.setText(current)
        editor.selectAll()
        rect = tab_bar.tabRect(index)
        # 右侧留出关闭按钮的空间
        editor.setGeometry(rect.adjusted(4, 3, -26, -3))
        editor.setStyleSheet(
            "QLineEdit{background:#282c34;color:#ffffff;border:1px solid #667eea;"
            "border-radius:3px;padding:0px 4px;font-weight:bold;}"
        )
        editor.committed.connect(lambda text, i=index: self._finish_inline_tab_rename(i, text))
        editor.cancelled.connect(self._discard_inline_tab_rename)
        self._tab_rename_editor = editor
        editor.show()
        editor.raise_()
        editor.setFocus()

    def _finish_inline_tab_rename(self, index, text):
        """就地编辑提交"""
        ed = self._tab_rename_editor
        self._tab_rename_editor = None
        if ed is not None:
            ed.deleteLater()
        self._apply_tab_name(index, text)

    def _discard_inline_tab_rename(self):
        """取消就地编辑（Esc 或被新的编辑取代）"""
        ed = self._tab_rename_editor
        self._tab_rename_editor = None
        if ed is not None:
            ed.deleteLater()

    def _rename_split(self, terminal):
        """重命名某个分屏（窗格）。名称非空时在窗格顶部显示标题栏，留空则清除。"""
        if terminal is None:
            return
        current = terminal.get_split_label() or ""
        name, ok = self._prompt_label_name(t("split.rename_title"), t("split.rename_prompt"), current)
        if not ok:
            return
        name = (name or "").strip()
        terminal.set_split_label(name)
        if name:
            self._remember_label_name(name)
        self._save_config()
