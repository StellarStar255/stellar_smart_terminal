"""终端鼠标与选区层 mixin（从 terminal_widget.py 拆出，行为不变）

TerminalWidget 的鼠标事件/选区/文本提取/URL/拖放方法集合，纯方法搬迁：
- mousePressEvent / mouseMoveEvent / mouseReleaseEvent / contextMenuEvent /
  dragEnterEvent / dropEvent：事件入口；_send_mouse_event / _send_wheel_to_app
  把鼠标转发给开启了鼠标上报的应用（vim/tmux 等）
- _pos_to_cell / _pos_to_absolute_cell / _absolute_to_display_row：像素↔格坐标
- _select_word_at / _select_line_at / _get_line_text / _is_row_soft_wrapped /
  _local_wrap_width / _row_fills_to_edge：双击选词/三击选行与折行判定
  （@_history_gesture 让一次手势只拍一次历史快照）
- _get_selection_range / _get_selected_text(_locked) / _get_all_content(_locked) /
  _copy_selection_to_clipboard / _select_all / _clear_selection：选区与复制
- _get_url_at_pos / _open_url / _move_cursor_to_click / _auto_scroll_tick

状态（selection_start/end、_auto_scroll_timer 等）仍由 TerminalWidget.__init__
初始化并持有；本 mixin 不可独立实例化。
"""

import functools
import sys

from PyQt6.QtCore import QPoint, QUrl, Qt
from PyQt6.QtGui import (
    QAction, QActionGroup, QDesktopServices, QDragEnterEvent, QDropEvent, QKeySequence, QMouseEvent
)
from PyQt6.QtWidgets import QApplication, QMenu
from i18n import t


def _history_gesture(method):
    """一次用户手势（双击选词/三击选行/Cmd 悬停 URL）只拍一次历史快照。

    这些手势内部逐行调用 _get_line_text / _is_row_soft_wrapped /
    _row_count_total，每个都经 _get_history_top() 整拷 scrollback deque；
    双击一次可达数百次 O(N) 拷贝。装饰后手势期间 _get_history_top 复用同一份
    快照（历史行对象推入后不再 mutate，快照内容与实时一致）。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._history_snapshot_scope():
            return method(self, *args, **kwargs)
    return wrapper


class TerminalMouseMixin:
    """TerminalWidget 的鼠标/选区方法集合（见模块 docstring）"""

    def _auto_scroll_tick(self):
        """自动滚动定时器回调 - 支持拖动选择时跨页

        滚动速度与「鼠标拉出边缘的距离」自适应：
        - 鼠标越远离边缘（甚至超出 widget），每次 tick 滚动行数越多
        - 用户向上猛拉时立即跨大段历史，轻微靠近边缘时则慢速精细滚动
        """
        if self._cleaned_up or self._auto_scroll_timer is None:
            return
        if not self._is_selecting or self._auto_scroll_direction == 0:
            self._auto_scroll_timer.stop()
            return

        # 自适应步长：边缘 1 行/tick，越远越快
        pull = max(0, self._auto_scroll_pull)
        step = max(1, 1 + pull // 6)
        # 上限避免一次跨太远导致选择终点跳动失控
        step = min(step, max(self.term_rows, 8))

        history_lines = self._get_history_count()
        max_scroll = history_lines

        if self._auto_scroll_direction < 0:
            # 向上滚动（查看历史）
            if self.scroll_offset < max_scroll:
                self.scroll_offset = min(self.scroll_offset + step, max_scroll)
                # 更新选择终点（扩展到新滚动位置的顶部）
                if self._last_mouse_pos:
                    self._selection_end = self._pos_to_absolute_cell(self._last_mouse_pos)
                self._invalidate_render_cache()
        else:
            # 向下滚动（回到最新）
            if self.scroll_offset > 0:
                self.scroll_offset = max(self.scroll_offset - step, 0)
                # 更新选择终点（扩展到新滚动位置的底部）
                if self._last_mouse_pos:
                    self._selection_end = self._pos_to_absolute_cell(self._last_mouse_pos)
                self._invalidate_render_cache()

    def _pos_to_cell(self, pos: QPoint) -> tuple:
        """将鼠标位置转换为终端单元格坐标 (row, col) - 返回显示区域内的相对行号"""
        x = pos.x() - self.PADDING
        y = pos.y() - self.PADDING - self._header_h

        col = max(0, min(int(x / self.char_width), self.term_cols - 1))
        row = max(0, min(int(y / self.char_height), self.term_rows - 1))

        return (row, col)

    def _pos_to_absolute_cell(self, pos: QPoint) -> tuple:
        """将鼠标位置转换为绝对行号坐标 (absolute_row, col) - 用于跨页选择

        使用上次渲染时记录的 display_start，确保鼠标坐标与屏幕显示内容一致。
        避免因新输出导致 history_count 变化而产生偏移。
        """
        x = pos.x() - self.PADDING
        y = pos.y() - self.PADDING - self._header_h

        col = max(0, min(int(x / self.char_width), self.term_cols - 1))
        display_row = max(0, min(int(y / self.char_height), self.term_rows - 1))

        # 使用上次实际渲染的 display_start，保证与屏幕内容一致
        absolute_row = self._rendered_display_start + display_row

        return (absolute_row, col)

    def _absolute_to_display_row(self, absolute_row: int) -> int:
        """将绝对行号转换为显示区域内的相对行号，如果不在显示区域则返回 -1"""
        display_start = self._rendered_display_start
        display_end = display_start + self.term_rows

        if display_start <= absolute_row < display_end:
            return absolute_row - display_start
        return -1

    def _mouse_fwd(self, force_local_selection):
        """当前是否应把鼠标点击/拖动转发给程序。

        需同时满足：程序开启了鼠标上报、未强制本地选择、且用户打开了「点击转发」
        开关。开关默认关闭，避免在 Claude Code 选项菜单里误点触发。滚轮上报走单独
        路径，不受此开关影响。
        """
        return (self._mouse_mode and not force_local_selection
                and self._mouse_click_forward_enabled)

    def set_mouse_click_forward_enabled(self, enabled: bool):
        """设置是否把鼠标点击转发给开启鼠标上报的 TUI 程序（默认关闭）。"""
        self._mouse_click_forward_enabled = bool(enabled)

    def _send_mouse_event(self, event: QMouseEvent, event_type: str):
        """发送鼠标事件到终端程序（SGR 1006 格式）"""
        if self._backend is None:
            return

        cell = self._pos_to_cell(event.pos())
        row, col = cell
        # SGR 格式: \x1b[<Cb;Cx;CyM (按下) 或 \x1b[<Cb;Cx;Cym (释放)
        # Cb: 按钮编码 (0=左键, 1=中键, 2=右键, 32=移动, 64=滚轮上, 65=滚轮下)
        button = 0
        if event.button() == Qt.MouseButton.RightButton:
            button = 2
        elif event.button() == Qt.MouseButton.MiddleButton:
            button = 1

        if event_type == 'move':
            button = 32  # 移动时加32

        suffix = 'M' if event_type in ('press', 'move') else 'm'
        # 坐标是1-based
        seq = f'\x1b[<{button};{col + 1};{row + 1}{suffix}'
        self._write_to_backend(seq.encode())

    def _send_wheel_to_app(self, going_up: bool, pos: QPoint, notches: int):
        """鼠标模式下把滚轮转发给程序（SGR 1006：64=上滚 / 65=下滚，按下用 M）。

        全屏 TUI（Claude Code / vim / less / tmux 等）切到备用屏幕后没有本地
        scrollback，滚轮必须交给程序让它滚自己的内容，否则会落空、看起来"无法
        回看历史"。按滚动格数发送多次，使程序滚动量与用户滚轮一致。
        """
        if self._backend is None:
            return
        row, col = self._pos_to_cell(pos)
        button = 64 if going_up else 65
        seq = f'\x1b[<{button};{col + 1};{row + 1}M'
        self._write_to_backend(seq.encode() * max(1, notches))

    def mousePressEvent(self, event: QMouseEvent):
        """鼠标按下 - 开始选择，支持双击选词、三击选行、Cmd+点击URL

        重要：按住 Shift 键可以强制使用本地选择模式（绕过程序的鼠标捕获）
        选择坐标使用绝对行号，支持跨页选择。
        鼠标模式下，单击仍然使用本地光标定位（通过方向键），
        拖动选择和其他鼠标事件则正常转发给程序。
        """
        if self._cleaned_up:
            return
        import time
        self.setFocus(Qt.FocusReason.MouseFocusReason)

        # Shift 键或回滚历史时强制使用本地选择模式（绕过鼠标模式）
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        if event.button() == Qt.MouseButton.LeftButton:
            current_time = time.time()
            cell = self._pos_to_cell(event.pos())  # 相对坐标，用于点击检测
            abs_cell = self._pos_to_absolute_cell(event.pos())  # 绝对坐标，用于选择
            # 每次按下先复位 Shift 扩展状态（仅下面的扩展分支会重新置位）
            self._shift_extend_click = False
            self._shift_extend_pending = False

            # 检测多击
            if (self._last_click_pos and
                self._last_click_pos == cell and
                current_time - self._last_click_time < self._double_click_interval):
                self._click_count += 1
            else:
                self._click_count = 1

            self._last_click_time = current_time
            self._last_click_pos = cell

            # Cmd/Alt+点击检测URL并打开
            if event.modifiers() & self._URL_CLICK_MODIFIERS:
                url = self._get_url_at_pos(cell)
                if url:
                    self._open_url(url)
                    return

            if self._click_count == 2:
                # 双击：选「宽」词——整条路径/URL（只在空格处截断）
                if self._mouse_fwd(force_local_selection):
                    self._send_mouse_event(event, 'press')
                self._select_word_at(abs_cell)
            elif self._click_count >= 3:
                # 三击：选「窄」词——只取 / @ . : - ~ 之间的一段（如 huangqiliang）
                if self._mouse_fwd(force_local_selection):
                    self._send_mouse_event(event, 'press')
                self._select_word_at(abs_cell, self._WORD_CHARS_NARROW)
                self._click_count = 0  # 重置
            elif (event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                  and self._selection_start is not None
                  and not self._select_all_mode):
                # Shift+按下且已有选区：先暂定为「扩展」——保留旧锚点 _selection_start，
                # 把终点移到此处。支持「先选中一段 → 滚动若干页 → 按住 Shift 点击新位置
                # → 中间整段连选」。但若紧接着发生拖动，说明是 Shift+拖动 → 由 mouseMove
                # 改为从按下点新建选区，保留「按住 Shift 在鼠标模式程序里本地框选」的用法。
                self._shift_press_cell = abs_cell
                self._selection_end = abs_cell
                self._is_selecting = True
                self._shift_extend_click = True
                self._shift_extend_pending = True
                self._mouse_mode_click = self._mouse_fwd(force_local_selection)
            else:
                # 单击开始选择 - 使用绝对坐标
                # 即使鼠标模式启用，也记录按下位置用于后续的单击光标定位
                self._selection_start = abs_cell
                self._selection_end = abs_cell
                self._is_selecting = True
                self._select_all_mode = False  # 清除全选模式
                # 记录是否处于鼠标模式，用于 release 时决定是否也转发事件
                self._mouse_mode_click = self._mouse_fwd(force_local_selection)

            self.update()
        elif self._mouse_fwd(force_local_selection):
            # 非左键在鼠标模式下转发给程序
            self._send_mouse_event(event, 'press')
        # 接受事件，确保 Qt 将后续的 mouseReleaseEvent 发送给本控件
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        """鼠标移动 - 更新选择区域（使用绝对坐标），支持拖动时自动滚动"""
        if self._cleaned_up:
            return
        # 悬停反馈：未按下按钮且按住 Cmd/Alt 悬停在 URL 上时显示手型光标
        if not event.buttons():
            hovering_url = bool(
                (event.modifiers() & self._URL_CLICK_MODIFIERS)
                and self._get_url_at_pos(self._pos_to_cell(event.pos()))
            )
            if hovering_url != self._hover_cursor_on:
                self._hover_cursor_on = hovering_url
                self.setCursor(
                    Qt.CursorShape.PointingHandCursor if hovering_url
                    else Qt.CursorShape.IBeamCursor
                )

        # Shift 键或回滚历史时强制使用本地选择模式
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        # 如果鼠标模式启用且没有强制本地选择，且正在拖动（按住按钮）
        # 但如果 _is_selecting 为 True（左键单击流程），继续本地选择而不转发
        if self._mouse_fwd(force_local_selection) and event.buttons() and not self._is_selecting:
            self._send_mouse_event(event, 'move')
            return

        if self._is_selecting:
            self._last_mouse_pos = event.pos()
            cur = self._pos_to_absolute_cell(event.pos())
            # Shift+按下后发生真正的拖动 → 是 Shift+拖动而非 Shift+点击 → 从按下点
            # 新建选区（不再扩展旧锚点），保留鼠标模式程序里的本地框选用法。
            if self._shift_extend_pending and cur != self._shift_press_cell:
                self._selection_start = self._shift_press_cell
                self._shift_extend_pending = False
                self._shift_extend_click = False
                self._select_all_mode = False
            self._selection_end = cur

            # 检测是否需要自动滚动（鼠标在边缘区域）— 拉得越远滚得越快
            y = event.pos().y()
            edge_zone = 24  # 进入边缘的阈值（像素）
            widget_height = self.height()

            if y < edge_zone:
                # 鼠标在顶部边缘 / 已拉出 widget 上方 - 向上滚动（查看历史）
                self._auto_scroll_direction = -1
                self._auto_scroll_pull = edge_zone - y  # y 越小（甚至为负）值越大
                if not self._auto_scroll_timer.isActive():
                    self._auto_scroll_timer.start(30)  # 间隔更小，反馈更跟手
            elif y > widget_height - edge_zone:
                # 鼠标在底部边缘 / 已拉出 widget 下方 - 向下滚动（回到最新）
                self._auto_scroll_direction = 1
                self._auto_scroll_pull = y - (widget_height - edge_zone)
                if not self._auto_scroll_timer.isActive():
                    self._auto_scroll_timer.start(30)
            else:
                # 不在边缘，停止自动滚动
                self._auto_scroll_direction = 0
                self._auto_scroll_pull = 0
                self._auto_scroll_timer.stop()

            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        """鼠标释放 - 完成选择或移动光标"""
        if self._cleaned_up:
            return
        # 停止自动滚动
        self._auto_scroll_timer.stop()
        self._auto_scroll_direction = 0
        self._auto_scroll_pull = 0

        # Shift 键或回滚历史时强制使用本地选择模式
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        # 如果鼠标模式启用且没有强制本地选择，且不在本地选择流程中
        if self._mouse_fwd(force_local_selection) and not self._is_selecting:
            self._send_mouse_event(event, 'release')
            return

        if event.button() == Qt.MouseButton.LeftButton and self._is_selecting:
            self._selection_end = self._pos_to_absolute_cell(event.pos())
            self._is_selecting = False

            # 检查是否是单击（没有拖动选择）
            # 使用容差判断：如果起止位置在同一行且列差距 ≤ 1，视为单击
            start_row, start_col = self._selection_start
            end_row, end_col = self._selection_end
            # Shift+点击扩展：起点是远处的锚点而非本次按下点，is_click 的"无拖动"判定
            # 不适用，且用户本就是要扩展选区——不要当成单击去清掉选区/移光标。
            is_click = (not self._shift_extend_click
                        and start_row == end_row and abs(start_col - end_col) <= 1)
            if is_click:
                if self._mouse_mode_click:
                    # 程序启用了鼠标上报模式（mouse mode）：把这次单击作为
                    # press+release 转发给程序，让 TUI 里的可点击界面能响应点击
                    # ——例如 Claude Code 的选项菜单、lazygit、fzf、htop 等。
                    # 否则单击会被吞掉（既不转发、又错发方向键），表现为“点了选项
                    # 没反应、菜单还被打乱”。本地行编辑光标定位只适用于未开鼠标
                    # 模式的普通 shell / REPL。
                    self._send_mouse_event(event, 'press')
                    self._send_mouse_event(event, 'release')
                else:
                    # 非鼠标模式 - 尝试移动光标到点击位置（使用相对坐标）
                    display_cell = self._pos_to_cell(event.pos())
                    self._move_cursor_to_click(display_cell)
                # 清除选择状态
                self._selection_start = None
                self._selection_end = None

            self._mouse_mode_click = False
            self.update()
        super().mouseReleaseEvent(event)

    def _move_cursor_to_click(self, click_pos: tuple):
        """通过发送方向键移动光标到点击位置

        仅当点击在光标所在行时生效。
        支持 Python REPL 等交互式程序中的光标定位。
        """
        if not click_pos or self._backend is None:
            return

        # _pos_to_cell 返回 (row, col) 格式
        click_row, click_col = click_pos
        cursor_col = self.screen.cursor.x
        cursor_row = self.screen.cursor.y

        # 只有点击在光标所在行才移动
        if click_row != cursor_row:
            return

        # 获取当前行内容，确定可编辑区域的起始位置（跳过提示符）
        # 例如 Python REPL 的 ">>> " 或 "... "
        line = self.screen.display[cursor_row]
        # 找到行中最后一个不可编辑的位置（光标不应移动到提示符区域之前）
        # 通过使用 Home 键的位置来限制：光标不能移到比提示符末尾更左的位置
        # 简单方法：限制 click_col 不小于提示符后的第一个位置
        # 检测常见提示符模式
        prompt_end = 0
        stripped = line.rstrip()
        for prefix in ['>>> ', '... ', '$ ', '% ', '# ', '> ', '→ ']:
            if stripped.startswith(prefix):
                prompt_end = len(prefix)
                break

        # 确保点击位置不在提示符区域内
        if click_col < prompt_end:
            click_col = prompt_end

        # 计算需要移动的距离
        diff = click_col - cursor_col

        if diff == 0:
            return

        # 发送方向键（根据 DECCKM 模式选择正确的转义序列）
        right_key = b'\x1bOC' if self.screen._decckm else b'\x1b[C'
        left_key = b'\x1bOD' if self.screen._decckm else b'\x1b[D'
        if diff > 0:
            # 向右移动
            for _ in range(diff):
                self._write_to_backend(right_key)
        else:
            # 向左移动
            for _ in range(-diff):
                self._write_to_backend(left_key)

    def _shortcut_hint(self, action_id, default_seq):
        """返回某分屏操作当前生效快捷键的「 (原生格式)」后缀，用于右键菜单标签。

        向主窗口读取用户覆盖后的键序列（与速查表/实际绑定保持一致），找不到主窗口
        或该项被清空时回退到默认值/空字符串。"""
        seq = default_seq
        window = self.window()
        getter = getattr(window, '_effective_shortcut', None)
        if callable(getter):
            seq = getter(action_id, default_seq)
        if not seq:
            return ""
        native = QKeySequence(seq).toString(QKeySequence.SequenceFormat.NativeText)
        return f"  ({native or seq})"

    def contextMenuEvent(self, event):
        """右键菜单"""
        from PyQt6.QtWidgets import QApplication

        # 强制激活窗口并处理事件队列，确保窗口状态正确
        # 这修复了从其他窗口拖拽过来的 terminal 右键菜单无法正常操作的问题
        window = self.window()
        if window:
            window.raise_()
            window.activateWindow()
            QApplication.processEvents()

        # 确保当前终端获得焦点，使其成为活动终端
        self.setFocus()

        # 使用窗口作为父对象，确保菜单与正确的顶级窗口关联
        menu = QMenu(window if window else self)
        menu.setToolTipsVisible(True)
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

        # 重命名/清除分屏标签（放在最上方，方便快速改名）
        rename_split_action = QAction(t("ctx.rename_split"), self)
        rename_split_action.triggered.connect(self.rename_split_requested.emit)
        menu.addAction(rename_split_action)

        # 滚动灵敏度（触控板尤其需要：手感因人、因设备差别很大）
        scroll_menu = QMenu(t("ctx.scroll_sensitivity"), menu)
        scroll_menu.setStyleSheet(menu.styleSheet())
        cur_sens = self.scroll_sensitivity()
        sens_group = QActionGroup(scroll_menu)
        sens_group.setExclusive(True)
        for factor in self.SCROLL_SENSITIVITY_CHOICES:
            act = QAction(t("ctx.scroll_sensitivity_item", factor=f"{factor:g}"),
                          self)
            act.setCheckable(True)
            act.setChecked(abs(cur_sens - factor) < 0.01)
            sens_group.addAction(act)
            act.triggered.connect(
                lambda checked=False, f=factor: self.set_scroll_sensitivity(f))
            scroll_menu.addAction(act)
        menu.addMenu(scroll_menu)

        menu.addSeparator()

        # 复制选中内容
        # 注意：不设置 setShortcut，因为 Ctrl+C 需要发送到终端进程
        # 复制功能通过 Cmd+C (macOS) 或在 keyPressEvent 中用 QKeySequence.StandardKey.Copy 处理
        copy_label = t("ctx.copy") if sys.platform == 'darwin' else t("ctx.copy_win")
        copy_action = QAction(copy_label, self)
        copy_action.triggered.connect(self._copy_selection_to_clipboard)
        menu.addAction(copy_action)

        # 粘贴
        paste_label = t("ctx.paste") if sys.platform == 'darwin' else t("ctx.paste_win")
        paste_action = QAction(paste_label, self)
        paste_action.triggered.connect(self._paste_from_clipboard)
        menu.addAction(paste_action)

        menu.addSeparator()

        # 快速命令子菜单
        if self.quick_commands_provider:
            presets = self.quick_commands_provider()
            if presets:
                quick_cmd_menu = QMenu(t("ctx.quick_commands"), menu)
                quick_cmd_menu.setStyleSheet(menu.styleSheet())

                for preset in presets:
                    preset_name = preset.get('name', t("common.unnamed"))
                    commands = preset.get('commands', [])
                    if commands:
                        # 点击预设名称直接执行全部命令
                        preset_action = QAction(preset_name, self)
                        preset_action.triggered.connect(
                            lambda checked, cmds=commands: self._execute_commands(cmds)
                        )
                        quick_cmd_menu.addAction(preset_action)

                quick_cmd_menu.addSeparator()

                # 添加"添加命令..."选项
                add_cmd_action = QAction(t("ctx.add_command"), self)
                add_cmd_action.triggered.connect(self.add_command_requested.emit)
                quick_cmd_menu.addAction(add_cmd_action)

                # 添加"管理预设..."选项
                manage_action = QAction(t("ctx.manage_presets"), self)
                manage_action.triggered.connect(self.manage_presets_requested.emit)
                quick_cmd_menu.addAction(manage_action)

                menu.addMenu(quick_cmd_menu)

        # 本地快速命令子菜单
        local_cmd_menu = QMenu(t("ctx.local_quick_commands"), menu)
        local_cmd_menu.setStyleSheet(menu.styleSheet())

        if self.local_quick_commands_provider:
            local_presets = self.local_quick_commands_provider()
            if local_presets:
                for preset in local_presets:
                    preset_name = preset.get('name', t("common.unnamed"))
                    commands = preset.get('commands', [])
                    if commands:
                        # 点击预设名称直接执行全部命令
                        preset_action = QAction(preset_name, self)
                        preset_action.triggered.connect(
                            lambda checked, cmds=commands: self._execute_commands(cmds)
                        )
                        local_cmd_menu.addAction(preset_action)

                local_cmd_menu.addSeparator()
            else:
                # 无本地命令时显示提示
                no_cmd_action = QAction(t("ctx.no_local_commands"), self)
                no_cmd_action.setEnabled(False)
                local_cmd_menu.addAction(no_cmd_action)
                local_cmd_menu.addSeparator()
        else:
            # 无本地命令时显示提示
            no_cmd_action = QAction(t("ctx.no_local_commands"), self)
            no_cmd_action.setEnabled(False)
            local_cmd_menu.addAction(no_cmd_action)
            local_cmd_menu.addSeparator()

        # 添加"添加命令..."选项
        add_local_cmd_action = QAction(t("ctx.add_command"), self)
        add_local_cmd_action.triggered.connect(self.add_local_command_requested.emit)
        local_cmd_menu.addAction(add_local_cmd_action)

        # 添加"管理本地预设..."选项
        manage_local_action = QAction(t("ctx.manage_local_presets"), self)
        manage_local_action.triggered.connect(self.manage_local_presets_requested.emit)
        local_cmd_menu.addAction(manage_local_action)

        menu.addMenu(local_cmd_menu)
        menu.addSeparator()

        # 打开当前目录
        open_dir_action = QAction(t("ctx.open_current_dir"), self)
        open_dir_action.triggered.connect(self._open_current_directory)
        menu.addAction(open_dir_action)

        # 复制当前路径
        copy_dir_action = QAction(t("ctx.copy_current_dir"), self)
        copy_dir_action.triggered.connect(self._copy_current_directory)
        menu.addAction(copy_dir_action)

        menu.addSeparator()

        # 刷新终端：强制重绘 + 给 PTY 重发尺寸（SIGWINCH），修复偶发花屏/错位
        refresh_action = QAction(t("ctx.refresh"), self)
        refresh_action.setToolTip(t("ctx.refresh.tip"))
        refresh_action.triggered.connect(self.refresh_terminal)
        menu.addAction(refresh_action)

        # 清空回滚历史：丢弃上方积累的历史行、释放内存（保留当前可见屏幕）。
        # 无历史时置灰。
        hist_n = self._get_history_count()
        clear_sb_action = QAction(t("ctx.clear_scrollback"), self)
        clear_sb_action.setToolTip(t("ctx.clear_scrollback.tip"))
        clear_sb_action.setEnabled(hist_n > 0)
        clear_sb_action.triggered.connect(self.clear_scrollback)
        menu.addAction(clear_sb_action)

        menu.addSeparator()

        # 搜索（终端历史，含回滚）
        search_action = QAction(t("ctx.search"), self)
        search_action.triggered.connect(self._show_search_bar)
        menu.addAction(search_action)

        # 全选
        select_all_action = QAction(t("ctx.select_all"), self)
        select_all_action.triggered.connect(self._select_all)
        menu.addAction(select_all_action)

        # 清除选择
        if self._has_selection():
            clear_selection_action = QAction(t("ctx.clear_selection"), self)
            clear_selection_action.triggered.connect(self._clear_selection)
            menu.addAction(clear_selection_action)

        menu.addSeparator()

        # 分屏（左右）—— 标签追加当前生效快捷键提示
        split_action = QAction(t("toolbar.split") + self._shortcut_hint("split_h", "Ctrl+Shift++"), self)
        split_action.triggered.connect(self.split_horizontal_requested.emit)
        menu.addAction(split_action)

        # 上下分屏
        vsplit_action = QAction(t("ctx.split_vertical") + self._shortcut_hint("split_v", "Ctrl+Shift+-"), self)
        vsplit_action.triggered.connect(self.split_vertical_requested.emit)
        menu.addAction(vsplit_action)

        # 关闭当前分屏
        # 提示用 Cmd+W：实际关闭分屏走 close_tab（多分屏时 Cmd+W 优先关当前分屏），
        # 比少有人用的 close_split(Ctrl+Shift+X) 更贴近用户真实操作
        close_split_action = QAction(t("ctx.close_split") + self._shortcut_hint("close_tab", "Ctrl+W"), self)
        close_split_action.triggered.connect(self.close_split_requested.emit)
        menu.addAction(close_split_action)

        # 向左移动分屏
        move_left_action = QAction(t("ctx.move_split_left"), self)
        move_left_action.triggered.connect(self.move_split_left_requested.emit)
        menu.addAction(move_left_action)

        # 向上移动分屏（垂直分屏内）
        move_up_action = QAction(t("ctx.move_split_up"), self)
        move_up_action.triggered.connect(self.move_split_up_requested.emit)
        menu.addAction(move_up_action)

        menu.exec(event.globalPos())

    def _has_selection(self) -> bool:
        """检查是否有选中的内容"""
        if self._select_all_mode:
            return True
        if self._selection_start is None or self._selection_end is None:
            return False
        return self._selection_start != self._selection_end

    def _get_selection_range(self) -> tuple:
        """获取选择范围（绝对坐标，确保 start <= end）"""
        if not self._has_selection():
            return None, None

        start = self._selection_start
        end = self._selection_end

        # 确保 start 在 end 之前
        if (start[0] > end[0]) or (start[0] == end[0] and start[1] > end[1]):
            start, end = end, start

        return start, end

    def _get_visible_selection_range(self) -> tuple:
        """获取当前显示区域内可见的选择范围（用于绘制高亮）

        返回 (start_display_row, start_col, end_display_row, end_col) 或 None
        """
        if not self._has_selection() or self._select_all_mode:
            return None

        start, end = self._get_selection_range()
        if start is None:
            return None

        abs_start_row, start_col = start
        abs_end_row, end_col = end

        # 使用渲染时记录的 display_start，确保高亮位置与内容一致
        display_start = self._rendered_display_start
        display_end = display_start + self.term_rows

        # 检查选择是否与显示区域重叠
        if abs_end_row < display_start or abs_start_row >= display_end:
            return None  # 选择完全在显示区域外

        # 计算可见部分的显示行号
        visible_start_row = max(0, abs_start_row - display_start)
        visible_end_row = min(self.term_rows - 1, abs_end_row - display_start)

        # 调整列范围
        visible_start_col = start_col if abs_start_row >= display_start else 0
        visible_end_col = end_col if abs_end_row < display_end else self.term_cols - 1

        return (visible_start_row, visible_start_col, visible_end_row, visible_end_col)

    def _get_selected_text(self) -> str:
        """获取选中的文本（自持锁，可安全并发于读取线程 feed）。

        _screen_lock 是 RLock：内部再调 _get_all_content / _get_history_top
        会重入同一把锁，安全。读 live screen.buffer 全程持锁，避免
        "dict changed size during iteration"。
        """
        with self._screen_lock:
            return self._get_selected_text_locked()

    def _get_selected_text_locked(self) -> str:
        """获取选中的文本（使用绝对行号，支持跨页选择）。调用方需持 _screen_lock。"""
        from terminal_widget import merge_extracted_lines  # 延迟引用：拼接逻辑刻意留在 widget 侧
        # 如果是全选模式，返回所有内容（包括历史记录）
        if self._select_all_mode:
            return self._get_all_content()

        start, end = self._get_selection_range()
        if start is None:
            return ""

        # start_row 和 end_row 现在是绝对行号
        start_row, start_col = start
        end_row, end_col = end

        # 直接引用历史记录（不复制）
        history = self._get_history_top()
        history_count = len(history)
        total_lines = history_count + self.term_rows
        term_cols = self.term_cols

        # 每行收集: (line_text, is_soft, last_content_col, spaceless, can_heuristic)
        # last_content_col: 整行最后一个非空格字符的列结尾位置（用于判断行是否"填满到行尾"）
        # spaceless: 整行内容是否「无内部空格」（单一连续 token，如 URL/路径/哈希）
        # can_heuristic: 是否允许对该行启用应用层软换行启发式（仅当选择覆盖整行宽度时）
        rows = []

        # 直接使用绝对行号遍历
        for abs_row in range(start_row, end_row + 1):
            if abs_row >= total_lines:
                break

            # 直接索引访问，避免创建完整列表
            if abs_row < history_count:
                buffer_line = history[abs_row]
            else:
                buffer_row = abs_row - history_count
                if 0 <= buffer_row < self.term_rows:
                    buffer_line = self.screen.buffer[buffer_row]
                else:
                    continue

            # 确定该行的列范围
            col_start = start_col if abs_row == start_row else 0
            col_end = end_col if abs_row == end_row else term_cols - 1

            # 使用与 _get_all_content 一致的逻辑提取行内容
            # 跳过宽字符的第二列（空占位符）
            chars = []
            col = 0
            while col < term_cols:
                try:
                    char = buffer_line[col]
                    char_data = getattr(char, 'data', None)
                    if char_data is None:
                        char_data = char if isinstance(char, str) else ' '

                    if char_data:
                        chars.append((col, char_data))
                        # 如果是宽字符，跳过下一列（占位符）
                        if self._is_wide_char(char_data):
                            col += 2
                        else:
                            col += 1
                    else:
                        # 空字符可能是宽字符的第二列，跳过
                        col += 1
                except (KeyError, IndexError, TypeError):
                    col += 1

            # 提取选中范围的字符
            selected_chars = []
            for col, char_data in chars:
                if col_start <= col <= col_end:
                    selected_chars.append(char_data)
                # 宽字符占用两列，如果选择范围包含第二列也应该包含该字符
                elif col < col_start and self._is_wide_char(char_data) and col + 1 >= col_start:
                    selected_chars.append(char_data)

            # 行级元数据（基于整行而非选区，用于换行类型判断）
            last_content_col = -1
            for c_col, c_ch in chars:
                if c_ch != ' ':
                    last_content_col = c_col + (1 if self._is_wide_char(c_ch) else 0)

            # 整行内容是否「无内部空格」（单一连续 token，如 URL/路径/哈希）
            core = ''.join(c for _, c in chars).strip()
            spaceless = bool(core) and (' ' not in core)

            is_soft = self.screen.is_soft_wrapped(buffer_line)
            can_heuristic = (col_start == 0 and col_end == term_cols - 1)

            if is_soft:
                # 终端软换行：保留所有字符（包括尾部空格，它们是真实内容）
                line_text = ''.join(selected_chars)
                # 行尾「空洞」（缺失格而非显式空格）是宽字符折行痕迹，不是内容
                try:
                    if (line_text.endswith(' ') and col_end >= term_cols - 1
                            and (term_cols - 1) not in buffer_line):
                        line_text = line_text[:-1]
                except TypeError:  # 非 dict 行对象
                    pass
            else:
                line_text = ''.join(selected_chars).rstrip()

            rows.append((line_text, is_soft, last_content_col, spaceless, can_heuristic))

        # 软换行/应用层折行启发式合并（与 _get_all_content 共用同一纯函数）。
        # Windows(ConPTY) 下软换行行的尾部空格是重绘填充、不可信，拼接前剥掉
        return merge_extracted_lines(rows, term_cols,
                                     strip_soft_trailing=(sys.platform == 'win32'))

    def _copy_selection_to_clipboard(self):
        """复制选中内容到剪贴板"""
        if self._has_selection():
            # _get_selected_text 自持 _screen_lock，可安全并发于读取线程 feed
            text = self._get_selected_text()
            # 兜底再去一次尾部空白：软换行行会保留尾部空格，整段末尾的空白
            # 没有意义且会污染剪贴板（复制登录 URL / 授权码时末尾多空格会导致粘贴失效）
            text = text.rstrip()
            # TUI 应用（如 Claude Code/Ink）用 U+00A0 不换行空格做布局缩进，
            # 粘贴进 shell/代码会引发肉眼不可见的报错，复制时归一化为普通空格
            text = text.replace('\xa0', ' ')
            if text:
                clipboard = QApplication.clipboard()
                clipboard.setText(text)
                return
        # 如果没有选择或选中文本为空，复制整个屏幕内容
        self._copy_to_clipboard()

    def _select_all(self):
        """全选当前屏幕（使用绝对坐标）"""
        # 使用渲染时记录的 display_start，确保与显示内容一致
        display_start = self._rendered_display_start
        display_end = display_start + self.term_rows - 1

        self._selection_start = (display_start, 0)
        self._selection_end = (display_end, self.term_cols - 1)
        self.update()

    def _select_all_content(self):
        """全选所有内容（包括历史记录）"""
        # 启用全选模式
        self._select_all_mode = True
        # 设置选择范围为所有内容的绝对坐标范围
        history_count = self._get_history_count()
        total_lines = history_count + self.term_rows
        self._selection_start = (0, 0)
        self._selection_end = (total_lines - 1, self.term_cols - 1)
        self.update()

    def _get_all_content(self) -> str:
        """获取所有内容（历史记录 + 当前屏幕，自持锁）。

        _screen_lock 是 RLock，重入安全；读 live screen.buffer 全程持锁，
        避免与读取线程 feed 并发导致 "dict changed size during iteration"。
        """
        with self._screen_lock:
            return self._get_all_content_locked()

    def _get_all_content_locked(self) -> str:
        """获取所有内容（历史记录 + 当前屏幕）- 优化版本。调用方需持 _screen_lock。"""
        from terminal_widget import merge_extracted_lines  # 延迟引用：拼接逻辑刻意留在 widget 侧
        columns = self.screen.columns
        is_wide = self._is_wide_char
        screen = self.screen

        def extract_line(buffer_line):
            """提取行内容，返回 (text, is_soft, last_content_col, spaceless, can_heuristic)
            最终换行类型在 merge_extracted_lines 的 look-ahead 阶段决定。
            """
            chars_with_col = []  # (col, char_data)
            char_list = []
            col = 0
            while col < columns:
                try:
                    char = buffer_line[col]
                    char_data = getattr(char, 'data', None)
                    if char_data is None:
                        char_data = char if isinstance(char, str) else ' '

                    if char_data:
                        chars_with_col.append((col, char_data))
                        char_list.append(char_data)
                        if is_wide(char_data):
                            col += 2
                        else:
                            col += 1
                    else:
                        col += 1
                except (KeyError, IndexError, TypeError):
                    col += 1

            # 行级元数据
            last_content_col = -1
            for c_col, c_ch in chars_with_col:
                if c_ch != ' ':
                    last_content_col = c_col + (1 if is_wide(c_ch) else 0)

            # 整行内容是否「无内部空格」（单一连续 token，如 URL/路径/哈希）
            core = ''.join(char_list).strip()
            spaceless = bool(core) and (' ' not in core)

            is_soft = screen.is_soft_wrapped(buffer_line)
            if is_soft:
                text = ''.join(char_list)  # 终端软换行：保留尾部空格
                # 软换行行行尾的「空洞」（缺失格而非显式空格）是宽字符放不下
                # 整体折到下一行留下的痕迹，不是内容 —— 读出来的默认空格要去掉
                try:
                    if text.endswith(' ') and (columns - 1) not in buffer_line:
                        text = text[:-1]
                except TypeError:  # 非 dict 行对象
                    pass
            else:
                text = ''.join(char_list).rstrip()

            # 全量内容提取始终覆盖整行宽度 → can_heuristic 恒为 True
            return text, is_soft, last_content_col, spaceless, True

        # 收集所有行
        line_data = []

        history_top = self._get_history_top()
        if history_top:
            line_data.extend(extract_line(hl) for hl in history_top)

        buffer = screen.buffer
        for row in range(self.term_rows):
            line_data.append(extract_line(buffer[row]))

        # 移除末尾空行
        while line_data and not line_data[-1][0]:
            line_data.pop()

        # 软换行/应用层折行启发式合并（与 _get_selected_text 共用同一纯函数）。
        # Windows(ConPTY) 下软换行行的尾部空格是重绘填充、不可信，拼接前剥掉
        return merge_extracted_lines(line_data, columns,
                                     strip_soft_trailing=(sys.platform == 'win32'))

    def _clear_selection(self):
        """清除选择"""
        self._selection_start = None
        self._selection_end = None
        self._select_all_mode = False
        self.update()

    def _copy_to_clipboard(self):
        """复制终端内容到剪贴板（复制当前可见区域的内容）"""
        content = self._get_visible_content()
        if content.strip():
            clipboard = QApplication.clipboard()
            # 与选区复制一致：NBSP 归一化为普通空格
            clipboard.setText(content.replace('\xa0', ' '))

    # ==================== 双击选词、三击选行 ====================

    def _row_count_total(self) -> int:
        """历史 + 当前屏幕的总行数（绝对行号上界）。"""
        return len(self._get_history_top()) + self.term_rows

    def _is_row_soft_wrapped(self, abs_row: int) -> bool:
        """该绝对行是否被软换行（内容续接到下一可见行，中间没有真实换行符）。

        长路径/长命令超过终端宽度时会被 pyte 自动折行并打上软换行标记；
        双击选词/选行需要据此把选区跨过这些视觉换行，才能选中完整的单行。
        """
        history = self._get_history_top()
        history_count = len(history)
        if abs_row < 0:
            return False
        if abs_row < history_count:
            buffer_line = history[abs_row]
        else:
            buffer_row = abs_row - history_count
            if 0 <= buffer_row < self.term_rows:
                buffer_line = self.screen.buffer[buffer_row]
            else:
                return False
        try:
            return bool(self.screen.is_soft_wrapped(buffer_line))
        except Exception:
            return False

    @_history_gesture
    def _local_wrap_width(self, abs_row: int) -> int:
        """估算 abs_row 所在「连续非空、非软换行行块」的应用层折行宽度（块内最大内容列+1）。

        像 Claude Code(Ink) 这类 TUI 会在盒子/边距内按比终端更窄的宽度折行，被折断的续行
        填满的是这个折行宽度而非终端右边缘。用块内最大内容宽度来估算它，从而正确识别续行。
        """
        total = self._row_count_total()

        def content_last_col(r):
            return len(self._get_line_text(r).rstrip()) - 1

        lo = abs_row
        # 向上扩展到块首（上一行非空、非软换行）
        while lo - 1 >= 0 and abs_row - lo < 80:
            if self._is_row_soft_wrapped(lo - 1) or content_last_col(lo - 1) < 0:
                break
            lo -= 1
        hi = abs_row
        # 向下扩展到块尾（本行非软换行且下一行非空）
        while hi + 1 < total and hi - abs_row < 80:
            if self._is_row_soft_wrapped(hi) or content_last_col(hi + 1) < 0:
                break
            hi += 1
        return max((content_last_col(r) for r in range(lo, hi + 1)), default=-1) + 1

    def _row_fills_to_edge(self, abs_row: int) -> bool:
        """该绝对行是否是「被宽度折断的长 token 续行」（如 Claude Code 窄窗口下的 URL）。

        判定：行内容无内部空格（单一连续 token），且填满到本行块的应用层折行宽度。
        双击/选词时据此把选区跨过这些硬折行，才能选中完整的 URL。
        """
        text = self._get_line_text(abs_row)
        core = text.strip()
        if not core or ' ' in core:
            return False  # 空行或含内部空格（散文）→ 不是单一长 token
        last_col = len(text.rstrip()) - 1
        threshold_low = max(8, int(self.term_cols * 0.40))
        if last_col < threshold_low:
            return False
        wrap_width = self._local_wrap_width(abs_row)
        return last_col >= wrap_width - 2

    # 双击用的「宽」单词字符集：纳入路径/URL 常见字符（/ . : ~ @ % + = , - 以及查询串 ? & # ;），
    # 这样双击一条完整路径或 URL 能整体选中，而不是停在第一个 / . 或 ? 处。
    _WORD_CHARS_WIDE = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./:~@%+=,?&#;')

    # 三击用的「窄」单词字符集：只含字母数字和下划线，遇到 / @ . : - ~ 等分隔符即截断，
    # 所以三击 /home/huangqiliang 或 huangqiliang@host 只选中其中一个 huangqiliang。
    _WORD_CHARS_NARROW = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_')

    @_history_gesture
    def _select_word_at(self, cell: tuple, word_chars: set = None):
        """选中指定位置的单词。

        word_chars 决定单词边界：默认用「宽」集（双击，整条路径/URL），传入
        _WORD_CHARS_NARROW 则按「窄」集（三击，只选 / @ . 之间的一段）。

        若单词因终端宽度被自动折行（中间无真实换行符）而跨越多个可见行，
        则连续选中完整单词，避免只复制到折行处的一半。复制时
        _get_selected_text 会把软换行行无缝拼接，得到完整字符串。
        """
        if word_chars is None:
            word_chars = self._WORD_CHARS_WIDE
        row, col = cell
        line_text = self._get_line_text(row)
        if not line_text:
            return

        # 向左找边界
        start = col
        while start > 0 and (start - 1 < len(line_text)) and line_text[start - 1] in word_chars:
            start -= 1
        # 向右找边界
        end = col
        while end < len(line_text) and line_text[end] in word_chars:
            end += 1

        if start >= end:
            return

        start_row, start_col = row, start
        end_row, end_col = row, end - 1
        total = self._row_count_total()

        # 续行判定：终端软换行，或应用层把长 token 硬折到行尾（如 Claude Code 窄窗口下的 URL）
        def _is_continuation(r):
            return self._is_row_soft_wrapped(r) or self._row_fills_to_edge(r)

        # 向上扩展：单词顶到行首且上一行是被折行的续行 → 接到上一行末尾的单词部分
        # 用 rstrip() 去掉行尾填充空格（应用层按盒子边距折行时，内容并不顶到终端右边缘）
        while start_col == 0 and start_row > 0 and _is_continuation(start_row - 1):
            prev_text = self._get_line_text(start_row - 1).rstrip()
            if not prev_text or prev_text[-1] not in word_chars:
                break
            s = len(prev_text)
            while s > 0 and prev_text[s - 1] in word_chars:
                s -= 1
            start_row -= 1
            start_col = s
            if start_col != 0:
                break

        # 向下扩展：单词顶到行尾且本行被折行 → 接到下一行开头的单词部分
        while end_row < total - 1 and _is_continuation(end_row):
            cur_text = self._get_line_text(end_row)
            # 选中的词必须延伸到本可见行内容末尾，才可能是被折行截断的同一个 token；
            # 否则（双击的是行中间的词）不应跨行，避免把下一行的词错误并入。
            if end_col < len(cur_text.rstrip()) - 1:
                break
            next_text = self._get_line_text(end_row + 1)
            if not next_text or next_text[0] not in word_chars:
                break
            next_content_end = len(next_text.rstrip()) - 1  # 下一行内容末列（非填充空格）
            e = 0
            while e < len(next_text) and next_text[e] in word_chars:
                e += 1
            end_row += 1
            end_col = e - 1
            # 词没填满下一行的内容宽度 → 它就是 token 的最后一段，停止
            if end_col != next_content_end:
                break

        self._selection_start = (start_row, start_col)
        self._selection_end = (end_row, end_col)
        self._is_selecting = False

    @_history_gesture
    def _select_line_at(self, cell: tuple):
        """选中整条逻辑行。

        长行因终端宽度不足被自动折成多个可见行（之间无真实换行符）。
        这里向上/向下扩展到同一逻辑行的首尾可见行，让三击/双击选行选中完整内容；
        复制时 _get_selected_text 会把软换行行无缝拼接。
        """
        row, _ = cell
        total = self._row_count_total()

        start_row = row
        while start_row > 0 and self._is_row_soft_wrapped(start_row - 1):
            start_row -= 1
        end_row = row
        while end_row < total - 1 and self._is_row_soft_wrapped(end_row):
            end_row += 1

        self._selection_start = (start_row, 0)
        self._selection_end = (end_row, self.term_cols - 1)
        self._is_selecting = False

    def _get_line_text(self, abs_row: int) -> str:
        """获取指定绝对行号的文本

        Args:
            abs_row: 绝对行号（包括历史记录）
        """
        # 持锁迭代历史/缓冲行对象，避免与读取线程的 feed mutate 撕裂
        with self._screen_lock:
            history = self._get_history_top()
            history_count = len(history)
            total_lines = history_count + self.term_rows

            if abs_row >= total_lines or abs_row < 0:
                return ""

            if abs_row < history_count:
                buffer_line = history[abs_row]
            else:
                buffer_row = abs_row - history_count
                if 0 <= buffer_row < self.term_rows:
                    buffer_line = self.screen.buffer[buffer_row]
                else:
                    return ""

            chars = []
            for col in range(self.screen.columns):
                try:
                    char = buffer_line[col]
                    if hasattr(char, 'data'):
                        chars.append(char.data if char.data else ' ')
                    elif isinstance(char, str):
                        chars.append(char)
                    else:
                        chars.append(' ')
                except (KeyError, IndexError, TypeError):
                    chars.append(' ')
            return ''.join(chars)

    # ==================== URL检测和打开 ====================

    @_history_gesture
    def _get_url_at_pos(self, cell: tuple) -> str:
        """获取指定位置的URL

        Args:
            cell: 相对坐标 (display_row, col)
        """
        display_row, col = cell

        # 将显示行号转换为绝对行号
        history_count = self._get_history_count()
        total_lines = history_count + self.term_rows
        display_start = max(0, total_lines - self.term_rows - self.scroll_offset)
        abs_row = display_start + display_row

        line_text = self._get_line_text(abs_row)
        if not line_text:
            return ""

        for match in self._url_pattern.finditer(line_text):
            start, end = match.span()
            if start <= col < end:
                url = match.group()
                if url.startswith('www.'):
                    url = 'https://' + url
                return url
        return ""

    def _open_url(self, url: str):
        """在浏览器中打开URL"""
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ==================== 拖拽文件 ====================

    def dragEnterEvent(self, event: QDragEnterEvent):
        """拖拽进入事件"""
        mime_data = event.mimeData()
        if mime_data is not None and mime_data.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        """拖拽放下事件 - 插入文件路径"""
        if self._backend is None:
            return

        mime_data = event.mimeData()
        if mime_data is None:
            return

        urls = mime_data.urls()
        paths = []
        for url in urls:
            if url.isLocalFile():
                path = url.toLocalFile()
                # 如果路径包含空格，加引号
                if ' ' in path:
                    path = f'"{path}"'
                paths.append(path)

        if paths:
            text = ' '.join(paths) + ' '
            if self._write_to_backend(text.encode('utf-8')):
                self.input_buffer += text

        event.acceptProposedAction()
