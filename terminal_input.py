"""终端键盘输入层 mixin（从 terminal_widget.py 拆出，行为不变）

TerminalWidget 的按键/输入法/焦点/粘贴方法集合，纯方法搬迁：
- keyPressEvent / event / inputMethodEvent / inputMethodQuery /
  focusInEvent / focusOutEvent：按键→转义序列、IME 组合、焦点上报
- _paste_from_clipboard(_macos/_qt) / _paste_clipboard_data_macos_native /
  _write_paste / _prepare_paste_text / _is_bracketed_paste_enabled /
  _image_save_dir：粘贴文本与图片（bracketed paste、macOS 原生剪贴板）
- send_text / arm_password_autofill / _maybe_autofill_password：外部注入
  文本与 SSH 密码自动填充

状态（_ime_preedit、_pending_password 等）仍由 TerminalWidget.__init__
初始化并持有；本 mixin 不可独立实例化。
"""

import os
import re
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent, QKeySequence
from PyQt6.QtWidgets import QApplication

from app_logging import get_logger

logger = get_logger(__name__)


class TerminalInputMixin:
    """TerminalWidget 的键盘/输入法/粘贴方法集合（见模块 docstring）"""

    def keyPressEvent(self, event: QKeyEvent):
        """处理键盘输入"""
        key = event.key()
        modifiers = event.modifiers()
        text = event.text()

        # 用户按键 = 已响应当前交互提示；重置签名，让下一个确认框
        # （哪怕页脚文案与上一个完全相同）也能再次点亮导航绿点。
        self._last_interaction_sig = None

        # 调试：Ctrl+Shift+D 导出 pyte 缓冲区到文件
        if (key == Qt.Key.Key_D and
            modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self._debug_dump_buffer()
            return

        # 调试：Ctrl+Shift+R 切换原始输出诊断捕获
        if (key == Qt.Key.Key_R and
            modifiers == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.toggle_debug_capture()
            return

        # 物理 Ctrl+C 发送中断信号到终端（优先级最高）
        # macOS: 物理 Ctrl 对应 MetaModifier, Cmd 对应 ControlModifier
        # Windows/Linux: 物理 Ctrl 对应 ControlModifier
        if key == Qt.Key.Key_C:
            if sys.platform == 'darwin':
                # macOS: 检测物理 Ctrl 键 (MetaModifier)
                is_physical_ctrl = bool(modifiers & Qt.KeyboardModifier.MetaModifier)
                is_cmd = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
            else:
                # Windows/Linux: 检测物理 Ctrl 键 (ControlModifier)
                is_physical_ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
                is_cmd = False

            # 物理 Ctrl+C（不是 Cmd+C）且没有 Shift/Alt
            has_shift_or_alt = bool(modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier))
            if is_physical_ctrl and not is_cmd and not has_shift_or_alt:
                # Windows/Linux: 有选中文本时执行复制，否则发送 SIGINT
                # macOS: 物理 Ctrl+C 始终发送 SIGINT（复制由 Cmd+C 处理）
                if sys.platform != 'darwin' and self._has_selection():
                    self._copy_selection_to_clipboard()
                elif self._backend is not None:
                    self._write_to_backend(b'\x03')
                return

        # === GUI 快捷键（始终拦截，不发送到终端）===

        # Cmd+W / Ctrl+W: 关闭标签页
        if event.matches(QKeySequence.StandardKey.Close):
            self.close_tab_requested.emit()
            event.accept()
            return

        # Cmd+T / Ctrl+T: 新建标签页
        if event.matches(QKeySequence.StandardKey.AddTab):
            self.new_tab_requested.emit()
            event.accept()
            return

        # Ctrl+V / Cmd+V: 粘贴
        if event.matches(QKeySequence.StandardKey.Paste):
            self._paste_from_clipboard()
            event.accept()
            return

        # Ctrl+C / Cmd+C: 复制（仅在有选中文本时拦截，否则发送到终端）
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy_selection_to_clipboard()
            event.accept()
            return

        # Cmd+E (macOS) / Ctrl+E: 收起/展开文件编辑区 — 委托给主窗口
        # 注意：只匹配 Cmd(ControlModifier)，物理 Ctrl+E(MetaModifier) 仍发送到
        # 终端（readline 的「跳到行尾」\x05），二者互不干扰。
        if (modifiers & Qt.KeyboardModifier.ControlModifier
                and not (modifiers & (Qt.KeyboardModifier.MetaModifier
                                       | Qt.KeyboardModifier.AltModifier
                                       | Qt.KeyboardModifier.ShiftModifier))
                and key == Qt.Key.Key_E):
            main_win = self.window()
            if hasattr(main_win, '_toggle_editor_collapsed'):
                main_win._toggle_editor_collapsed()
                event.accept()
                return

        # Cmd+= 放大 / Cmd+- 缩小；Cmd+Shift+= 左右分屏 / Cmd+Shift+- 上下分屏。
        # 关键：用「字符键」本身区分是否按了 Shift —— =/- 是未加 Shift（缩放），
        # +/_ 是加了 Shift（分屏）；再叠加 Shift 修饰位兜底。这样无论 Qt 在 macOS
        # 上把「需 Shift 的字符」上报成 ⇧⌘+ / ⌘+ / ⇧⌘= 中的哪种，都能准确区分。
        # （之前这里只看 Key_Plus/Key_Equal 不看 Shift，把 Cmd+Shift+= 也吃成放大，
        #  导致分屏快捷键收不到事件。）
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            main_win = self.window()
            # 左右分屏：Cmd+Shift+=（+ 键，或 = 键且带 Shift）。直接调主窗口方法
            # （whole_tab 默认 False=只分当前小窗口），与原键盘行为一致——不能走
            # split_*_requested 信号，那条路会注入 _shift_held()，而这里 Shift 是
            # 打出快捷键必须按的，会被误当成「分裂整个标签页」。
            if key == Qt.Key.Key_Plus or (key == Qt.Key.Key_Equal and shift):
                if hasattr(main_win, '_split_current_tab'):
                    main_win._split_current_tab()
                    event.accept()
                    return
            # 放大：纯 Cmd+=
            if key == Qt.Key.Key_Equal:
                if hasattr(main_win, '_global_zoom_in'):
                    main_win._global_zoom_in()
                else:
                    self._zoom_in()
                event.accept()
                return
            # 上下分屏：Cmd+Shift+-（_ 键，或 - 键且带 Shift）
            if key == Qt.Key.Key_Underscore or (key == Qt.Key.Key_Minus and shift):
                if hasattr(main_win, '_split_vertical_current_terminal'):
                    main_win._split_vertical_current_terminal()
                    event.accept()
                    return
            # 缩小：纯 Cmd+-
            if key == Qt.Key.Key_Minus:
                if hasattr(main_win, '_global_zoom_out'):
                    main_win._global_zoom_out()
                else:
                    self._zoom_out()
                event.accept()
                return

        # macOS: Cmd+Right/Left 跳到行末/行首
        if sys.platform == 'darwin':
            if (modifiers & Qt.KeyboardModifier.ControlModifier) and key == Qt.Key.Key_Right:
                if self._backend is not None:
                    self._write_to_backend(b'\x05')
                event.accept()
                return
            if (modifiers & Qt.KeyboardModifier.ControlModifier) and key == Qt.Key.Key_Left:
                if self._backend is not None:
                    self._write_to_backend(b'\x01')
                event.accept()
                return
            # Cmd+Down / Cmd+Up：跳到历史最底部/最顶部（与 macOS「跳到文末/文首」习惯一致；
            # 排除 Shift——Ctrl+Shift+Up/Down 是窗口级的不透明度快捷键）
            if ((modifiers & Qt.KeyboardModifier.ControlModifier)
                    and not (modifiers & (Qt.KeyboardModifier.ShiftModifier
                                          | Qt.KeyboardModifier.AltModifier
                                          | Qt.KeyboardModifier.MetaModifier))
                    and key in (Qt.Key.Key_Down, Qt.Key.Key_Up)):
                if key == Qt.Key.Key_Down:
                    self.scroll_to_bottom()
                else:
                    self.scroll_offset = self._get_history_count()
                    self._invalidate_render_cache()
                event.accept()
                return

        # Alt(Option)+Up/Down：在命令输入行之间跳转（配合滚动条刻度回看长输出）。
        # 备用屏幕不拦截——TUI（vim/htop 等）可能用 Alt+方向键，原样透传。
        if (key in (Qt.Key.Key_Up, Qt.Key.Key_Down)
                and modifiers & Qt.KeyboardModifier.AltModifier
                and not (modifiers & (Qt.KeyboardModifier.ControlModifier
                                      | Qt.KeyboardModifier.ShiftModifier
                                      | Qt.KeyboardModifier.MetaModifier))
                and not getattr(self.screen, '_in_alt_screen', False)):
            self._jump_to_command_mark(-1 if key == Qt.Key.Key_Up else 1)
            event.accept()
            return

        # Escape 关闭搜索栏
        if key == Qt.Key.Key_Escape and self._search_bar and self._search_bar.isVisible():
            self._hide_search_bar()
            event.accept()
            return

        # Shift+PageUp/PageDown 滚动历史
        if modifiers == Qt.KeyboardModifier.ShiftModifier:
            history_lines = self._get_history_count()
            if key == Qt.Key.Key_PageUp:
                self.scroll_offset = min(self.scroll_offset + self.term_rows, history_lines)
                self._invalidate_render_cache()
                event.accept()
                return
            elif key == Qt.Key.Key_PageDown:
                self.scroll_offset = max(self.scroll_offset - self.term_rows, 0)
                self._invalidate_render_cache()
                event.accept()
                return
            elif key == Qt.Key.Key_Home:
                self.scroll_offset = history_lines
                self._invalidate_render_cache()
                event.accept()
                return
            elif key == Qt.Key.Key_End:
                self.scroll_offset = 0
                self._invalidate_render_cache()
                event.accept()
                return

        # === macOS 上 Cmd+A 始终触发全选（包括历史），便于复制终端内容 ===
        # Win/Linux 上保留 Ctrl+A 发送 \x01 给 shell（行首）这类终端语义
        if sys.platform == 'darwin' and event.matches(QKeySequence.StandardKey.SelectAll):
            self._select_all_content()
            event.accept()
            return

        # === macOS 上 Cmd+F 始终打开终端搜索（运行时也可用，便于翻日志/找报错）===
        # Cmd+F = ControlModifier，不与 shell 的物理 Ctrl+F（MetaModifier→\x06）冲突。
        # Win/Linux 的 StandardKey.Find = Ctrl+F 本身是 shell 的前进字符键，故运行时不抢占，
        # 仍由下方"终端未运行时"分支处理（进程退出后才用 Ctrl+F 搜索静态缓冲）。
        if sys.platform == 'darwin' and event.matches(QKeySequence.StandardKey.Find):
            self._show_search_bar()
            event.accept()
            return

        # === 终端未运行时的 GUI 快捷键 ===
        if self._backend is None:
            if event.matches(QKeySequence.StandardKey.SelectAll):
                self._select_all_content()
                event.accept()
                return
            if event.matches(QKeySequence.StandardKey.Find):
                self._show_search_bar()
                event.accept()
                return
            if event.matches(QKeySequence.StandardKey.Save):
                self._save_to_file()
                event.accept()
                return
            return

        # === 以下所有按键都发送到终端 ===

        # 单独按下的修饰键（Cmd/Ctrl/Shift/Alt…）不产生终端输入，必须在
        # 「输入时滚到底部」之前拦掉：否则在历史区选中文本后按住 Cmd 准备
        # Cmd+C 复制时，第一下 Cmd 就把视图滚回底部、选区被滚走。
        if key in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Meta,
                   Qt.Key.Key_Alt, Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock):
            event.accept()
            return

        # 输入时自动滚动到底部（走整屏重绘路径，并同步滚动条）
        if self.scroll_offset > 0:
            self.scroll_to_bottom()

        data = b''

        # 检测物理 Ctrl 键按下（不包含 Shift/Alt）
        # macOS: 物理 Ctrl → MetaModifier, Cmd → ControlModifier
        # Windows/Linux: 物理 Ctrl → ControlModifier
        has_shift_or_alt = bool(modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier))
        if sys.platform == 'darwin':
            is_physical_ctrl = bool(modifiers & Qt.KeyboardModifier.MetaModifier) and not bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        else:
            is_physical_ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier) and not bool(modifiers & Qt.KeyboardModifier.MetaModifier)

        # 物理 Control 键按下（无 Shift/Alt）→ 全部发送到终端
        if is_physical_ctrl and not has_shift_or_alt:
            if key == Qt.Key.Key_C:
                # Ctrl+C 已在上方优先处理，这里作为兜底
                data = b'\x03'
            elif key == Qt.Key.Key_D:
                data = b'\x04'
            elif key == Qt.Key.Key_Z:
                data = b'\x1a'
            elif key == Qt.Key.Key_L:
                data = b'\x0c'
            elif key == Qt.Key.Key_A:
                data = b'\x01'
            elif key == Qt.Key.Key_E:
                data = b'\x05'
            elif key == Qt.Key.Key_K:
                data = b'\x0b'
            elif key == Qt.Key.Key_U:
                data = b'\x15'
            elif key == Qt.Key.Key_W:
                data = b'\x17'
            elif key >= Qt.Key.Key_A and key <= Qt.Key.Key_Z:
                data = bytes([key - Qt.Key.Key_A + 1])
        elif key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
            # Shift+Enter: 发送换行符（用于多行输入，如 Claude Code）
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                data = b'\n'
                self.input_buffer += '\n'
            else:
                # 普通 Enter: 发送回车
                data = b'\r'
                if self.input_buffer.strip():
                    self.input_recorded.emit(self.input_buffer)
                self.input_buffer = ""
                # 记录命令标记（Alt+↑/↓ 跳转 + 滚动条刻度）
                self._record_command_mark()
        elif key == Qt.Key.Key_Backspace:
            data = b'\x7f'
            if self.input_buffer:
                self.input_buffer = self.input_buffer[:-1]
        elif key == Qt.Key.Key_Tab:
            data = b'\t'
        elif key == Qt.Key.Key_Backtab:
            # Shift+Tab 发送反向制表符转义序列
            data = b'\x1b[Z'
        elif key == Qt.Key.Key_Escape:
            data = b'\x1b'
        elif key == Qt.Key.Key_Up:
            data = b'\x1bOA' if self.screen._decckm else b'\x1b[A'
        elif key == Qt.Key.Key_Down:
            data = b'\x1bOB' if self.screen._decckm else b'\x1b[B'
        elif key == Qt.Key.Key_Right:
            data = b'\x1bOC' if self.screen._decckm else b'\x1b[C'
        elif key == Qt.Key.Key_Left:
            data = b'\x1bOD' if self.screen._decckm else b'\x1b[D'
        elif key == Qt.Key.Key_Home:
            data = b'\x1b[H'
        elif key == Qt.Key.Key_End:
            data = b'\x1b[F'
        elif key == Qt.Key.Key_PageUp:
            data = b'\x1b[5~'
        elif key == Qt.Key.Key_PageDown:
            data = b'\x1b[6~'
        elif key == Qt.Key.Key_Delete:
            data = b'\x1b[3~'
        elif key == Qt.Key.Key_Insert:
            data = b'\x1b[2~'
        elif key == Qt.Key.Key_F1:
            data = b'\x1bOP'
        elif key == Qt.Key.Key_F2:
            data = b'\x1bOQ'
        elif key == Qt.Key.Key_F3:
            data = b'\x1bOR'
        elif key == Qt.Key.Key_F4:
            data = b'\x1bOS'
        elif text:
            data = text.encode('utf-8')
            self.input_buffer += text

        if data:
            self._write_to_backend(data)
            # 用户按键 = 已在处理该终端 → 解除提醒静默，下一次事故重新提醒
            self._alert_muted_until = 0.0
            event.accept()

    def inputMethodEvent(self, event):
        """处理输入法输入（中文等）"""
        # 处理预编辑文本（正在输入的拼音字母）
        preedit_string = event.preeditString()
        if self._preedit_string != preedit_string:
            self._preedit_string = preedit_string
            self._content_dirty = True
            # 更新输入法候选框位置
            from PyQt6.QtGui import QGuiApplication
            from PyQt6.QtCore import Qt
            input_method = QGuiApplication.inputMethod()
            if input_method:
                input_method.update(Qt.InputMethodQuery.ImCursorRectangle)
            self.update()

        # 处理已确认提交的文本
        commit_string = event.commitString()
        if commit_string and self._backend is not None:
            try:
                data = commit_string.encode('utf-8')
                self._write_to_backend(data)
                self.input_buffer += commit_string
                # 提交后清除预编辑文本
                self._preedit_string = ""
            except OSError:  # 尽力而为，失败不影响主流程
                pass
        event.accept()

    # macOS 上 Cmd+字母默认全被终端抢走（当 Ctrl 发给 shell）。这几个留给菜单栏：
    # Cmd+N 新窗口、Cmd+O 打开文件夹（Cmd+Shift+O 打开文件）。物理 Ctrl+N/O
    # （MetaModifier）照旧发给 shell，不受影响。
    _MENU_RESERVED_KEYS = (Qt.Key.Key_N, Qt.Key.Key_O)

    def _is_menu_reserved_combo(self, key, modifiers) -> bool:
        if sys.platform != 'darwin' or key not in self._MENU_RESERVED_KEYS:
            return False
        is_cmd = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        is_physical_ctrl = bool(modifiers & Qt.KeyboardModifier.MetaModifier)
        return is_cmd and not is_physical_ctrl

    def event(self, event):
        """处理事件 - 确保特殊键不被Qt拦截"""
        # 处理 ShortcutOverride 事件 - 告诉 Qt 我们要自己处理这些按键，不要当作快捷键
        if event.type() == QEvent.Type.ShortcutOverride:
            key_event = event
            key = key_event.key()
            modifiers = key_event.modifiers()

            # Ctrl+字母键 需要发送到终端，不能被 Qt 快捷键系统拦截
            # 注意：macOS 上 Qt 会交换 Control 和 Command 键
            # 物理 Control 键 → MetaModifier, 物理 Command 键 → ControlModifier
            # 所以需要同时检查两者
            if (modifiers & Qt.KeyboardModifier.ControlModifier) or (modifiers & Qt.KeyboardModifier.MetaModifier):
                if Qt.Key.Key_A <= key <= Qt.Key.Key_Z and not self._is_menu_reserved_combo(key, modifiers):
                    event.accept()  # 接受事件，阻止 Qt 将其作为快捷键处理
                    return True

            # Cmd + =/+/-/_ 由终端自己处理（缩放 / 分屏，见 keyPressEvent）。
            # 必须在这里抢下来：交给 Qt 快捷键系统时，(Key_Plus, Cmd+Shift) 这种
            # 「需 Shift 的字符」匹配不到任何 QKeySequence，事件被吞、keyPressEvent
            # 也收不到，导致 Cmd+Shift+= 分屏无效。
            if (modifiers & Qt.KeyboardModifier.ControlModifier) and key in (
                    Qt.Key.Key_Plus, Qt.Key.Key_Equal,
                    Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
                event.accept()
                return True

            # Tab 键也需要自己处理
            if key == Qt.Key.Key_Tab or key == Qt.Key.Key_Backtab:
                event.accept()
                return True

        if event.type() == QEvent.Type.KeyPress:
            key_event = event
            if key_event.key() == Qt.Key.Key_Tab or key_event.key() == Qt.Key.Key_Backtab:
                # 拦截Tab键，不让Qt用于焦点切换
                self.keyPressEvent(key_event)
                return True
        return super().event(event)

    def inputMethodQuery(self, query):
        """响应输入法查询"""
        from PyQt6.QtCore import Qt, QRect
        if query == Qt.InputMethodQuery.ImEnabled:
            return True
        elif query == Qt.InputMethodQuery.ImCursorRectangle:
            # 返回光标位置，用于定位输入法候选框
            if self.screen and hasattr(self.screen, 'cursor'):
                cx = self.screen.cursor.x
                cy = self.screen.cursor.y
                x = int(self.PADDING + cx * self.char_width)
                y = int(self.PADDING + self._header_h + cy * self.char_height)
                return QRect(x, y, int(self.char_width), int(self.char_height))
        elif query == Qt.InputMethodQuery.ImFont:
            # 返回当前字体，用于输入法渲染
            return self.term_font
        elif query == Qt.InputMethodQuery.ImCursorPosition:
            # 返回光标在周围文本中的位置
            return len(self.input_buffer)
        elif query == Qt.InputMethodQuery.ImSurroundingText:
            # 返回光标周围的文本
            return self.input_buffer
        elif query == Qt.InputMethodQuery.ImHints:
            # 返回输入法提示
            return Qt.InputMethodHint.ImhNone
        return super().inputMethodQuery(query)

    def focusInEvent(self, event):
        """获取焦点时更新输入法上下文"""
        super().focusInEvent(event)
        # 强制更新输入法位置，确保候选框显示在正确的窗口
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtCore import Qt
        input_method = QGuiApplication.inputMethod()
        if input_method:
            input_method.update(Qt.InputMethodQuery.ImCursorRectangle)
        # 恢复光标闪烁
        self.cursor_visible = True
        self.update()

    def focusOutEvent(self, event):
        """失去焦点时的处理"""
        super().focusOutEvent(event)
        # 重置输入法状态，避免候选框残留
        from PyQt6.QtGui import QGuiApplication
        input_method = QGuiApplication.inputMethod()
        if input_method:
            input_method.reset()
        self.update()

    def arm_password_autofill(self, password: str, timeout: float = 30.0):
        """为接下来的 ssh 密码提示预置一次性自动填充。

        Remote 面板连接时用户已输入过该主机密码（见 RemoteExplorerPanel），
        这里把它缓存到本终端，检测到 `password:` 提示就自动回填一次，避免重复输入。
        密码只留在内存、一次性使用、超时即作废。
        """
        import time as _time
        self._pending_ssh_password = password
        self._pending_ssh_password_deadline = _time.monotonic() + timeout

    # ssh 密码提示匹配：行尾出现 "password:" 或 "passphrase ...:"（忽略大小写）
    _RE_SSH_PWD_PROMPT = re.compile(r"(?:password|passphrase[^:\n]*):\s*$", re.IGNORECASE)

    def _maybe_autofill_password(self, text: str):
        """检测到 ssh 密码提示则自动回填缓存的密码（一次性）。"""
        pw = getattr(self, '_pending_ssh_password', None)
        if not pw:
            return
        import time as _time
        if _time.monotonic() > getattr(self, '_pending_ssh_password_deadline', 0):
            self._pending_ssh_password = None
            return
        if self._RE_SSH_PWD_PROMPT.search(text):
            # 先清除再发送，避免万一回包再次触发导致重复输入错误密码
            self._pending_ssh_password = None
            self._write_to_backend((pw + '\r').encode('utf-8'))

    def send_text(self, text: str):
        """发送文本到终端"""
        if self._backend is None:
            return
        try:
            data = text.encode('utf-8')
            self._write_to_backend(data)

            # 如果文本以换行结尾，说明是提交命令
            if text.endswith('\n'):
                # 获取命令内容（不含换行）
                cmd = text.rstrip('\n')
                # 将当前缓冲区内容加上新命令一起记录
                full_input = self.input_buffer + cmd if self.input_buffer else cmd
                if full_input.strip():
                    self.input_recorded.emit(full_input)
                self.input_buffer = ""
            else:
                # 不是提交，只是追加到缓冲区
                self.input_buffer += text
        except Exception as e:
            logger.warning(f"Send text error: {e}")

    def _paste_from_clipboard(self):
        """从剪贴板粘贴（支持文本、图片和音频文件）

        macOS 上 clipboard.mimeData() 在剪贴板含有 TIFF 图片数据时
        会在 Qt C++ 层面触发 segfault，Python 的 try/except 无法捕获。
        因此在 macOS 上完全避免调用 mimeData()，
        改用 clipboard.text()（安全）+ osascript 原生命令处理图片/文件。
        """
        if self._backend is None:
            return

        try:
            clipboard = QApplication.clipboard()

            if sys.platform == 'darwin':
                # macOS: 先安全获取文本，图片/文件通过原生 API 处理
                self._paste_from_clipboard_macos(clipboard)
            else:
                # 其他平台：使用 Qt API（通常不会 segfault）
                self._paste_from_clipboard_qt(clipboard)
        except Exception:
            logger.debug("_paste_from_clipboard: suppressed exception", exc_info=True)

    def _write_paste(self, data: bytes) -> bool:
        """将粘贴内容写入后端，必要时以 Bracketed Paste 序列包裹

        当应用（如 Claude Code）启用了 DECSET 2004 时，终端需要把粘贴内容
        包裹在 ESC[200~ ... ESC[201~ 之间，应用据此识别"整块粘贴"（而不是
        逐字键入），从而可以对粘贴的图片路径进行特殊处理（例如显示 [Image #N]）。

        括起来时还要去掉「末尾换行」：_prepare_paste_text 会把换行统一成 \\r，
        这在非 bracketed 的 shell 粘贴里是对的（每行当作一条命令回车执行），但在
        bracketed 模式下，末尾那个 \\r 会被夹进 ESC[200~…ESC[201~ 里，成为粘贴
        内容的一部分。像 Claude Code 登录时「Paste code here」这种单行输入框，会把
        末尾的 \\r 一并算进 code，导致校验失败（"Invalid code / 请确认复制了完整
        code"）。用户三击整行复制 OAuth code 时剪贴板常带尾随换行，正是此症。
        Windows Terminal(cmd) 在 bracketed 粘贴时也会剥掉尾随换行 —— 所以 cmd 正常、
        本终端却报错。这里只剥「末尾」的 \\r，行内换行照旧保留（多行粘进 TUI 编辑器
        仍然逐行生效）。
        """
        if self._is_bracketed_paste_enabled():
            data = data.rstrip(b'\r\n')
            data = b'\x1b[200~' + data + b'\x1b[201~'
        return self._write_to_backend(data)

    def _is_bracketed_paste_enabled(self) -> bool:
        try:
            return bool(getattr(self.screen, '_bracketed_paste', False))
        except Exception:
            return False

    def _prepare_paste_text(self, text: str) -> bytes:
        """准备粘贴文本：将换行符转换为回车符（终端标准行为）

        在终端中，按下 Enter 键发送的是 \\r (CR)，而不是 \\n (LF)。
        粘贴多行文本时，需要将换行符转换为回车符，
        否则 PTY/ConPTY 可能无法正确处理行序，导致行倒序等问题。
        """
        # 先将 \r\n (Windows换行) 转为 \r，再将剩余 \n (Unix换行) 转为 \r
        text = text.replace('\r\n', '\r').replace('\n', '\r')
        return text.encode('utf-8')

    def _paste_from_clipboard_macos(self, clipboard):
        """macOS 粘贴处理 - 避免调用 mimeData() 防止 segfault

        重要顺序：先通过原生 JXA 检查图片/文件（public.file-url / public.tiff /
        public.png）。若剪贴板中存在图片或文件 URL，则由原生分支保存图片并插入
        生成的路径。只有当剪贴板里既没有图片也没有文件 URL 时，才回落到
        clipboard.text() 作为普通文本粘贴。

        原因：从 Finder 复制图片文件（或某些应用复制图片）时，剪贴板会同时包含
        file-url / 图片数据以及一份文本表示（文件路径字符串）。如果先读取
        clipboard.text()，就会把那段文本路径原样粘入终端，从而绕过图片保存与
        正常的图片粘贴流程。
        """
        # 先用原生 API 处理图片/文件。若处理成功（含图片/文件），直接返回。
        if self._paste_clipboard_data_macos_native():
            return

        # 剪贴板中没有图片/文件，按普通文本处理
        # clipboard.text() 在此分支是安全的（无图片数据，不会触发 TIFF segfault）
        text = clipboard.text()
        if text:
            data = self._prepare_paste_text(text)
            if self._write_paste(data):
                self.input_buffer += text

    def _image_save_dir(self) -> Path:
        """「Image to CWD」存图的目标目录（带兜底，绝不抛异常）。

        必须用终端 shell 的真实 cwd（get_cwd，lsof 取子进程目录），取不到
        再退启动时的工作目录、再退用户主目录。绝不能用 os.getcwd()——那是
        主进程的目录：打包 app 从 Finder 启动时是 `/`，mkdir `/.images` 因
        权限失败抛异常，曾把整个粘贴链路静默吞掉（v1.14 DMG 实测；源码
        运行时恰好从项目目录启动，两个 cwd 重合掩盖了此 bug）。
        目录创建失败逐级回退到临时目录——存图路径永远不该让粘贴失败。
        """
        import tempfile
        candidates = []
        if self.image_save_local:
            cwd = None
            try:
                cwd = self.get_cwd()
            except Exception:
                logger.debug("_image_save_dir: suppressed exception", exc_info=True)
            cwd = cwd or getattr(self, '_working_dir', None) or str(Path.home())
            candidates.append(Path(cwd) / ".images")
        candidates.append(Path(tempfile.gettempdir()) / "smart_terminal_images")
        for d in candidates:
            try:
                d.mkdir(exist_ok=True)
                return d
            except OSError:
                continue
        return Path(tempfile.gettempdir())   # 理论兜底：临时目录本身必然存在

    def _paste_clipboard_data_macos_native(self) -> bool:
        """macOS: 使用 osascript + JXA 原生 API 安全处理剪贴板图片/文件

        返回 True 表示剪贴板中含图片/文件并已处理；返回 False 表示剪贴板里
        没有图片或文件 URL（调用方应回落到文本粘贴）。
        """
        import subprocess
        from datetime import datetime

        # 准备图片保存路径
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        save_path = str(self._image_save_dir() / f"paste_{timestamp}.png")

        escaped_path = save_path.replace('\\', '\\\\').replace('"', '\\"')

        # JXA (JavaScript for Automation) 脚本
        # 通过 NSPasteboard 原生 API 安全读取剪贴板，在子进程中运行
        # 即使出错也不会导致主进程崩溃
        jxa_script = f'''
ObjC.import('AppKit');
var pb = $.NSPasteboard.generalPasteboard;
var types = pb.types;
var hasFileURL = types.containsObject('public.file-url');
var hasTIFF = types.containsObject('public.tiff');
var hasPNG = types.containsObject('public.png');
if (hasFileURL) {{
    var urls = pb.readObjectsForClassesOptions([$.NSURL], null);
    if (urls && urls.count > 0) {{
        var paths = [];
        for (var i = 0; i < urls.count; i++) {{
            var p = urls.objectAtIndex(i).path;
            if (p) paths.push(p.js);
        }}
        if (paths.length > 0) {{
            "FILES:" + paths.join("\\n");
        }} else {{ "NOTHING"; }}
    }} else {{ "NOTHING"; }}
}} else if (hasTIFF || hasPNG) {{
    var imgData = hasPNG ? pb.dataForType('public.png') : pb.dataForType('public.tiff');
    if (imgData && imgData.length > 0) {{
        var rep = $.NSBitmapImageRep.imageRepWithData(imgData);
        if (rep) {{
            var pngData = rep.representationUsingTypeProperties(4, $({{}}));
            if (pngData && pngData.length > 0) {{
                pngData.writeToFileAtomically("{escaped_path}", true);
                "IMAGE_OK";
            }} else {{ "NOTHING"; }}
        }} else {{ "NOTHING"; }}
    }} else {{ "NOTHING"; }}
}} else {{ "NOTHING"; }}
'''

        try:
            result = subprocess.run(
                ['osascript', '-l', 'JavaScript', '-e', jxa_script],
                capture_output=True, text=True, timeout=10
            )
            output = result.stdout.strip()

            if output == "IMAGE_OK" and os.path.isfile(save_path):
                # 粘贴图片时只发送原始路径（不加 @ 前缀、不加尾部空格）。
                # 若应用（如 Claude Code）启用了 Bracketed Paste，_write_paste
                # 会将内容包裹在 ESC[200~/ESC[201~ 之间，应用据此识别为整块粘贴
                # 并把路径显示为 [Image #N]；未启用时则直接落为原始路径文本。
                path_text = save_path
                data = path_text.encode('utf-8')
                if self._write_paste(data):
                    self.input_buffer += path_text
                    self.image_pasted.emit(save_path)
                return True

            elif output.startswith("FILES:"):
                file_paths = output[6:].split('\n')
                for fp in file_paths:
                    fp = fp.strip()
                    if not fp:
                        continue
                    ext = Path(fp).suffix.lower()
                    is_media = (ext in self._AUDIO_EXTENSIONS or
                                ext in self._VIDEO_EXTENSIONS or
                                ext in self._IMAGE_EXTENSIONS)
                    prefix = '@' if (is_media and self.image_prefix_enabled) else ''
                    if ' ' in fp and not prefix:
                        path_text = f'"{fp}" '
                    else:
                        path_text = prefix + fp + ' '
                    data = path_text.encode('utf-8')
                    if self._write_paste(data):
                        self.input_buffer += path_text
                        if is_media:
                            self.image_pasted.emit(fp)
                # 只要 JXA 检测到 file-url，就算处理完毕（即使写入失败也不应
                # 回落到文本粘贴，否则会把文件路径的文本表示重复粘一次）
                return True

            # output == "NOTHING" 或其他非预期输出：剪贴板无图片/文件
            return False
        except Exception:
            # osascript 失败时，保守地返回 False，让调用方尝试文本粘贴
            return False

    def _paste_from_clipboard_qt(self, clipboard):
        """非 macOS 平台：使用 Qt API 处理剪贴板粘贴"""
        mime_data = clipboard.mimeData()
        if mime_data is None:
            return

        # 检查是否有文件URL（可能是音频、视频、图片等文件）
        if mime_data.hasUrls():
            urls = mime_data.urls()
            for url in urls:
                if url.isLocalFile():
                    file_path = url.toLocalFile()
                    ext = Path(file_path).suffix.lower()
                    if ext in self._AUDIO_EXTENSIONS or ext in self._VIDEO_EXTENSIONS or ext in self._IMAGE_EXTENSIONS:
                        prefix = '@' if self.image_prefix_enabled else ''
                        path_text = prefix + file_path + ' '
                        data = path_text.encode('utf-8')
                        if self._write_paste(data):
                            self.input_buffer += path_text
                            self.image_pasted.emit(file_path)
                        return

        # 检查是否有图片
        if mime_data.hasImage():
            try:
                image = clipboard.image()
            except Exception:
                image = None
            if image is not None and not image.isNull():
                from datetime import datetime

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                # 目录解析统一走 _image_save_dir：终端真实 cwd + 失败兜底
                image_path = self._image_save_dir() / f"paste_{timestamp}.png"

                if image.save(str(image_path), "PNG"):
                    # 发送原始路径；若 Bracketed Paste 启用则由 _write_paste
                    # 自动包裹 ESC[200~/ESC[201~，Claude Code 等应用据此将路径
                    # 识别为图片并展示为 [Image #N]。
                    path_text = str(image_path)
                    data = path_text.encode('utf-8')
                    if self._write_paste(data):
                        self.input_buffer += path_text
                        self.image_pasted.emit(str(image_path))
                return

        # 处理文本粘贴
        text = clipboard.text()
        if not text:
            return

        data = self._prepare_paste_text(text)
        if self._write_paste(data):
            self.input_buffer += text
