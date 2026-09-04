"""终端滚动与搜索层 mixin（从 terminal_widget.py 拆出，行为不变）

TerminalWidget 的滚动条/滚轮/命令标记/搜索栏方法集合，纯方法搬迁：
- _apply_scrollbar_style / _position_scrollbar / _sync_scrollbar /
  _on_scrollbar_value_changed / scroll_to_bottom：自绘滚动条与 scroll_offset 同步
- wheelEvent / _wheel_lines / scroll_sensitivity / set_scroll_sensitivity：
  滚轮（按事件幅度缩放）与灵敏度配置
- _record_command_mark / _current_mark_positions / _mark_fractions /
  _jump_to_command_mark：命令标记（滚动条刻度 + 跳转）
- _update_scrollback_level / _update_scrollback_dot_widget /
  _position_scrollback_dot / scrollback_level / scrollback_tooltip：历史压力指示
- _create_search_bar / _show_search_bar / _hide_search_bar / eventFilter /
  _perform_search / _search_worker / _match_search_snapshot /
  _on_search_finished / _search_next / _search_prev / _scroll_to_match：
  搜索栏（后台线程匹配快照，latest-wins）

状态（scroll_offset、_command_marks、_search_* 等）仍由 TerminalWidget.__init__
初始化并持有；本 mixin 不可独立实例化。
"""

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget
from i18n import t
from app_logging import get_logger

logger = get_logger(__name__)


class TerminalScrollSearchMixin:
    """TerminalWidget 的滚动/命令标记/搜索方法集合（见模块 docstring）"""

    def _update_scrollback_level(self):
        """按"预测 reflow 耗时 = 历史行数 × 实测每行毫秒"算严重度，变化时发信号。

        0 = 无压力（不显示点）/ 1 = 琥珀 / 2 = 红。备用屏（tmux/vim）历史不参与，
        _get_history_count() 返回 0 → 自然为 0。
        """
        predicted_ms = self._get_history_count() * self._reflow_ms_per_line
        if predicted_ms >= self._SCROLLBACK_RED_MS:
            level = 2
        elif predicted_ms >= self._SCROLLBACK_AMBER_MS:
            level = 1
        else:
            level = 0
        if level != self._scrollback_level:
            self._scrollback_level = level
            self.scrollback_pressure_changed.emit(level)
        # 每次都同步右上角指示点（tooltip 里的行数/耗时会随内容增长刷新）
        self._update_scrollback_dot_widget()

    def _update_scrollback_dot_widget(self):
        """右上角的 scrollback 压力指示点（覆盖式小圆点，点击即清空该终端历史）。

        高频输出下本方法每刷新帧都会被调用，故只在【等级变化】时做重样式/重定位/
        show 这类较重操作；tooltip 里的行数/耗时随内容变，但很便宜，每次刷新。
        """
        level = self._scrollback_level
        if level <= 0:
            if self._scrollback_dot_btn is not None:
                self._scrollback_dot_btn.hide()
            self._scrollback_dot_shown_level = 0
            return
        btn = self._scrollback_dot_btn
        if btn is None:
            btn = QPushButton(self)
            btn.setFixedSize(12, 12)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # 不抢键盘焦点：点它清空 scrollback 后焦点仍留在终端，可继续打字
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(self.clear_scrollback)
            self._scrollback_dot_btn = btn
        if getattr(self, '_scrollback_dot_shown_level', 0) != level:
            color = '#e0a83a' if level == 1 else '#e0524c'  # 琥珀 / 红
            btn.setStyleSheet(
                f"QPushButton{{background:{color};border:1px solid rgba(0,0,0,90);"
                f"border-radius:6px;}} QPushButton:hover{{border:1px solid #ffffff;}}")
            self._position_scrollback_dot()
            btn.show()
            btn.raise_()
            self._scrollback_dot_shown_level = level
        btn.setToolTip(self.scrollback_tooltip())

    def _position_scrollback_dot(self):
        btn = self._scrollback_dot_btn
        if btn is None:
            return
        margin = 6
        x = self.width() - self.SCROLLBAR_WIDTH - btn.width() - margin
        y = self._header_h + margin
        btn.move(max(0, x), max(0, y))

    def scrollback_level(self) -> int:
        """当前 scrollback 压力等级（0/1/2），供 tab/标题栏查询。"""
        return self._scrollback_level

    def scrollback_tooltip(self) -> str:
        """指示点的 tooltip：历史行数 + 预测 reflow 耗时 + 操作提示。"""
        n = self._get_history_count()
        ms = int(n * self._reflow_ms_per_line)
        return t("term.scrollback_pressure_tip", lines=n, ms=ms)

    def _apply_scrollbar_style(self):
        """低调样式：窄、半透明、跟随前景色"""
        fg = self.fg_color
        self._v_scrollbar.setStyleSheet(f"""
            QScrollBar:vertical {{
                background: transparent;
                width: {self.SCROLLBAR_WIDTH}px;
                margin: 0;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({fg.red()}, {fg.green()}, {fg.blue()}, 35%);
                border-radius: 4px;
                min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba({fg.red()}, {fg.green()}, {fg.blue()}, 55%);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; background: none; border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """)

    def _position_scrollbar(self):
        """把滚动条定位到 widget 右缘（标题栏以下），覆盖式、不参与布局"""
        sb = getattr(self, '_v_scrollbar', None)
        if sb is None:
            return
        w = self.SCROLLBAR_WIDTH
        sb.setGeometry(self.width() - w, self._header_h,
                       w, max(0, self.height() - self._header_h))

    def _sync_scrollbar(self):
        """让滚动条的 range/pageStep/value 与 scrollback 状态保持一致。

        在所有 scroll_offset / 历史行数变化的入口（统一经由
        _invalidate_render_cache）调用；用 _scrollbar_syncing + blockSignals
        防止 setValue 反过来触发 _on_scrollbar_value_changed 形成回环。
        无历史或处于备用屏幕（_get_history_count() == 0）时隐藏。
        """
        sb = getattr(self, '_v_scrollbar', None)
        if sb is None:
            return
        max_scroll = self._get_history_count()  # 备用屏幕返回 0
        if max_scroll <= 0:
            if sb.isVisible():
                sb.hide()
            return
        self._scrollbar_syncing = True
        sb.blockSignals(True)
        try:
            sb.setRange(0, max_scroll)
            sb.setPageStep(self.term_rows)
            sb.setSingleStep(1)
            # 滚动条 value=0 表示顶部；scroll_offset=0 表示底部，方向相反
            sb.setValue(max_scroll - self.scroll_offset)
        finally:
            sb.blockSignals(False)
            self._scrollbar_syncing = False
        if not sb.isVisible():
            self._position_scrollbar()
            sb.show()
            sb.raise_()
        else:
            # range/value 未变时 Qt 不会重绘，但命令标记可能新增了 → 刷新刻度
            sb.update()

    def _on_scrollbar_value_changed(self, value: int):
        """拖动滚动条 → 更新 scroll_offset 并整屏重绘"""
        if self._scrollbar_syncing:
            return
        max_scroll = self._get_history_count()
        new_offset = max(0, min(max_scroll - value, max_scroll))
        if new_offset != self.scroll_offset:
            self.scroll_offset = new_offset
            self._scroll_accum = 0.0
            # scroll_offset 变化使 render_state 中的 start_line 失配 → 整屏重绘
            self._invalidate_render_cache()

    # 鼠标滚轮一"格"（angleDelta=120）滚多少行。触控板不走这个常数，
    # 它按手指移动的像素换算，见 _wheel_lines。
    WHEEL_LINES_PER_NOTCH = 1.5

    # 滚动灵敏度倍数（右键菜单可调，落配置）。1.0 = 触控板滑过多少像素就滚多少行
    CONFIG_KEY_SCROLL_SENSITIVITY = 'terminal_scroll_sensitivity'

    SCROLL_SENSITIVITY_CHOICES = (0.5, 0.75, 1.0, 1.5, 2.0)

    _scroll_sensitivity_cache = None      # 进程内缓存，别让每个滚轮事件都读配置

    @classmethod
    def scroll_sensitivity(cls) -> float:
        """当前滚动灵敏度倍数（读一次配置后进程内缓存）。"""
        if cls._scroll_sensitivity_cache is None:
            try:
                import app_config
                val = float(app_config.read_config().get(
                    cls.CONFIG_KEY_SCROLL_SENSITIVITY))
            except Exception:
                val = 1.0
            # 卡在合理区间：配置被手改成 0 或负数时不至于滚不动/反着滚
            cls._scroll_sensitivity_cache = min(4.0, max(0.1, val))
        return cls._scroll_sensitivity_cache

    @classmethod
    def set_scroll_sensitivity(cls, value: float):
        """设置滚动灵敏度并持久化（对所有终端立即生效）。"""
        value = min(4.0, max(0.1, float(value)))
        cls._scroll_sensitivity_cache = value
        try:
            import app_config
            app_config.update_config({cls.CONFIG_KEY_SCROLL_SENSITIVITY: value},
                                     description='terminal-scroll-sensitivity')
        except Exception:
            logger.debug("set_scroll_sensitivity: save failed", exc_info=True)

    def _wheel_lines(self, event) -> float:
        """这次滚轮事件该滚多少行（带符号，正=向上看历史）。

        以前不论事件幅度一律算 1.5 行 —— 鼠标滚轮没问题（一格一个事件），
        但 macOS 触控板一次轻扫会发出几十个高分辨率小事件，每个也当 1.5 行，
        于是轻轻一划就窜出去几十行，就是"太灵敏"的由来。
        现在按事件自身的幅度换算：触控板有像素增量就按「像素 / 行高」，
        鼠标滚轮按「角度 / 120 格」，再乘用户设定的灵敏度。
        """
        sens = self.scroll_sensitivity()
        pixels = event.pixelDelta().y()
        if pixels:
            row_h = float(getattr(self, 'char_height', 0) or 0) or 16.0
            return pixels / row_h * sens
        degrees = event.angleDelta().y()
        if not degrees:
            return 0.0
        return degrees / 120.0 * self.WHEEL_LINES_PER_NOTCH * sens

    def wheelEvent(self, event):
        """鼠标滚轮事件 - 滚动历史"""
        step_lines = self._wheel_lines(event)
        if step_lines == 0:
            event.accept()
            return

        going_up = step_lines > 0

        # 备用屏幕里运行的全屏 TUI（Claude Code / vim / less / tmux 等）若启用了
        # 鼠标报告，就把滚轮作为鼠标事件转发给程序，让它滚动自己的内容。备用屏幕
        # 没有本地 scrollback，不转发滚轮就会落空，表现为"无法向上回看历史"。
        # 仅限备用屏幕：主屏幕始终保留本地回滚，绝不把滚轮从用户手里夺走。
        if self._mouse_mode and getattr(self.screen, '_in_alt_screen', False):
            # 攒够一整行才发一格：触控板的高分辨率小事件逐个发格子，
            # 在 vim/less/Claude Code 里同样会快得没法用
            if (self._app_wheel_accum > 0) != going_up:
                self._app_wheel_accum = 0.0     # 换方向：上一方向的余量作废
            self._app_wheel_accum += step_lines
            notches = int(abs(self._app_wheel_accum))
            if notches:
                self._app_wheel_accum -= notches if going_up else -notches
                self._send_wheel_to_app(going_up, event.position().toPoint(),
                                        notches)
            self._scroll_accum = 0.0
            event.accept()
            return

        # 计算可滚动的最大行数
        # 历史记录 + 当前屏幕缓冲区的总行数
        history_lines = self._get_history_count()
        # 最大scroll_offset应该是历史记录的行数（这样可以滚动到最顶部）
        max_scroll = history_lines

        at_top = self.scroll_offset >= max_scroll
        at_bottom = self.scroll_offset <= 0

        # 触控板惯性（momentum）撞墙处理：macOS 在滑到顶/底后会继续发送一串
        # 惯性滚动事件，并伴随橡皮筋回弹（方向来回反复）。若照常按固定 1.5 行
        # 累加，就会在边界处被反复整数化成「上一行又下一行」的来回抖动。
        # 已经顶到墙且仍是惯性阶段时直接吞掉事件，并清空小数累加器。
        if event.phase() == Qt.ScrollPhase.ScrollMomentum and (
            (going_up and at_top) or (not going_up and at_bottom)
        ):
            self._scroll_accum = 0.0
            event.accept()
            return

        old_offset = self.scroll_offset
        # 按事件真实幅度累加（触控板按像素、滚轮按格），小数累加器保证
        # 不足一行的滚动不会被取整丢掉
        self._app_wheel_accum = 0.0
        self._scroll_accum += step_lines

        # 取整数部分应用到scroll_offset，保留小数部分到下次累加
        lines = int(self._scroll_accum)
        if lines != 0:
            self._scroll_accum -= lines
            self.scroll_offset = max(0, min(self.scroll_offset + lines, max_scroll))

        # 撞到顶/底后只丢弃"继续往墙外推"的那部分残留小数（它会在下次反向
        # 滚动时抢跑一行，造成边界处的抽动）；指回可滚区间的残留必须留着——
        # 触控板每个事件都不足一行，在底部一律清零的话轻扫会完全滚不动。
        if ((self.scroll_offset >= max_scroll and self._scroll_accum > 0)
                or (self.scroll_offset <= 0 and self._scroll_accum < 0)):
            self._scroll_accum = 0.0

        # 只有在滚动位置实际改变时才更新
        if old_offset != self.scroll_offset:
            self._invalidate_render_cache()
        event.accept()

    def scroll_to_bottom(self):
        """滚动到底部（最新内容）"""
        if self.scroll_offset != 0:
            self.scroll_offset = 0
            self._invalidate_render_cache()

    # ---------- 命令标记（Alt+↑/↓ 跳转 + 滚动条刻度） ----------

    def _record_command_mark(self):
        """按 Enter 提交命令时记录当前输入行的位置。

        用「累计行号」坐标（_total_history_lines + cursor.y）：单调递增，
        历史 deque 满员丢头后仍可换算，不会指错行。备用屏幕（TUI 内部回车）
        不记录。
        """
        screen = getattr(self, 'screen', None)
        if screen is None or getattr(screen, '_in_alt_screen', False):
            return
        cum = screen._total_history_lines + screen.cursor.y
        marks = self._command_marks
        if marks and marks[-1] == cum:
            return
        marks.append(cum)
        if len(marks) > self._COMMAND_MARKS_MAX:
            del marks[:len(marks) - self._COMMAND_MARKS_MAX]

    def _current_mark_positions(self):
        """把累计行号换算为当前绝对行号（0 = 现存历史最顶行），剪掉已被
        deque 丢弃的旧标记。返回升序列表。"""
        screen = getattr(self, 'screen', None)
        if screen is None:
            return []
        shift = screen._total_history_lines - self._get_history_count()
        return [cum - shift for cum in self._command_marks if cum - shift >= 0]

    def _mark_fractions(self):
        """标记在全部内容（历史+屏幕）中的比例位置，供滚动条画刻度。"""
        total = self._get_history_count() + self.term_rows
        if total <= 0:
            return []
        return [m / total for m in self._current_mark_positions()]

    def _jump_to_command_mark(self, direction: int):
        """Alt+↑/↓：跳到上一条/下一条命令输入行（该行置于视口顶部）。

        以当前视口顶部的绝对行号为基准找相邻标记；向下越过最后一个标记时
        回到底部（最新输出），与用户「跳完看结果」的预期一致。
        """
        hist = self._get_history_count()
        view_top = hist - self.scroll_offset
        marks = self._current_mark_positions()
        if direction < 0:
            cands = [m for m in marks if m < view_top]
            target = cands[-1] if cands else None
        else:
            cands = [m for m in marks if m > view_top]
            target = cands[0] if cands else None
        if target is None:
            if direction > 0:
                self.scroll_to_bottom()
            return
        new_offset = max(0, min(hist - target, hist))
        if new_offset != self.scroll_offset:
            self.scroll_offset = new_offset
            self._scroll_accum = 0.0
            self._invalidate_render_cache()

    # ==================== 搜索功能 ====================

    def _show_search_bar(self):
        """显示搜索栏"""
        if not self._search_bar:
            self._create_search_bar()
        self._search_bar.show()
        self._search_bar.raise_()
        self._search_bar.findChild(QLineEdit).setFocus()
        self._search_bar.findChild(QLineEdit).selectAll()

    def _hide_search_bar(self):
        """隐藏搜索栏"""
        if self._search_bar:
            self._search_bar.hide()
            self._search_debounce_timer.stop()
            self._search_matches = []
            self._current_match_index = -1
            self._search_seq += 1  # 在途的 worker 结果作废
            with self._search_cache_lock:
                self._search_line_cache.clear()  # 释放被钉住的历史行引用
            self.setFocus()
            self.update()

    def eventFilter(self, obj, event):
        """搜索输入框：Shift+Enter 跳转上一个匹配（Enter 由 returnPressed 处理）"""
        if (self._search_bar is not None
                and event.type() == QEvent.Type.KeyPress
                and isinstance(obj, QLineEdit)
                and obj.parent() is self._search_bar
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._search_prev()
            return True
        return super().eventFilter(obj, event)

    def _create_search_bar(self):
        """创建搜索栏"""
        self._search_bar = QWidget(self)
        layout = QHBoxLayout(self._search_bar)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(6)

        # 搜索输入框
        search_input = QLineEdit()
        search_input.setPlaceholderText("搜索...")
        search_input.setMinimumWidth(200)
        search_input.textChanged.connect(self._on_search_text_changed)
        search_input.returnPressed.connect(self._search_next)
        # Shift+Enter 跳转上一个匹配（returnPressed 不区分修饰键，用 eventFilter 拦截）
        search_input.installEventFilter(self)

        # 匹配计数标签
        self._match_label = QLabel("0/0")
        self._match_label.setMinimumWidth(50)

        # 上一个/下一个按钮
        prev_btn = QPushButton("▲")
        prev_btn.setFixedWidth(30)
        prev_btn.clicked.connect(self._search_prev)

        next_btn = QPushButton("▼")
        next_btn.setFixedWidth(30)
        next_btn.clicked.connect(self._search_next)

        # 关闭按钮
        close_btn = QPushButton("✕")
        close_btn.setFixedWidth(30)
        close_btn.clicked.connect(self._hide_search_bar)

        layout.addWidget(search_input)
        layout.addWidget(self._match_label)
        layout.addWidget(prev_btn)
        layout.addWidget(next_btn)
        layout.addWidget(close_btn)

        # 样式
        self._search_bar.setStyleSheet("""
            QWidget { background-color: #3d3d5c; border-radius: 6px; }
            QLineEdit {
                background-color: #282c34; color: #abb2bf; border: 1px solid #555;
                border-radius: 4px; padding: 4px 8px;
            }
            QLabel { color: #888; }
            QPushButton {
                background-color: #555; color: #eee; border: none; border-radius: 4px;
            }
            QPushButton:hover { background-color: #667eea; }
        """)

        # 定位在右上角
        self._search_bar.adjustSize()
        self._position_search_bar()

    def _position_search_bar(self):
        """定位搜索栏到右上角"""
        if self._search_bar:
            bar_width = self._search_bar.sizeHint().width()
            self._search_bar.move(self.width() - bar_width - 20, 10)

    def _on_search_text_changed(self, text: str):
        """搜索文本变化：300ms 防抖后才执行全量（历史 + 当前屏幕）搜索"""
        self._search_pending_text = text

        if not text:
            self._search_debounce_timer.stop()
            self._search_matches = []
            self._current_match_index = -1
            self._match_label.setText("0/0")
            self.update()
            return

        self._search_debounce_timer.start(300)

    def _extract_search_line(self, buffer_line, columns: int = None) -> tuple:
        """提取单行用于搜索的 (text, col_map)。

        text 中宽字符只占 1 个字符（跳过 pyte 的 stub 格），col_map[i] 为
        text[i] 对应的 buffer 列号，用于把匹配的字符串下标映射回高亮列区间。
        columns 显式传入时不碰 self.screen（worker 线程调用）。
        """
        # 完全空行（无任何已写入格子）直接短路，避免 2 万行历史逐格扫描
        if not buffer_line:
            return "", ()
        if columns is None:
            columns = self.screen.columns
        try:
            columns = max(columns, max(buffer_line.keys()) + 1)
        except (ValueError, AttributeError, TypeError):  # 空行/异常行按屏宽
            pass
        is_wide = self._is_wide_char
        chars = []
        cols = []
        col = 0
        while col < columns:
            try:
                char = buffer_line[col]
                data = getattr(char, 'data', None)
                if data is None:
                    data = char if isinstance(char, str) else ' '
            except (KeyError, IndexError, TypeError):
                data = ' '
            if data:
                chars.append(data)
                cols.append(col)
                col += 2 if is_wide(data) else 1
            else:
                # 宽字符占位 stub，跳过
                col += 1
        return ''.join(chars), tuple(cols)

    @classmethod
    def _get_search_executor(cls):
        """全类共享的搜索单线程 executor（懒创建）。与 reflow 分开排队：
        一次几万行的搜索不该把窗口缩放的 reflow 卡在后面。"""
        with cls._reflow_executor_lock:
            if cls._search_executor is None:
                from concurrent.futures import ThreadPoolExecutor
                cls._search_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix='search')
            return cls._search_executor

    def _perform_search(self):
        """发起全量搜索：全部 history 行 + 当前屏幕行（匹配为搜索时刻的快照）。

        逐格提取 + 匹配是纯 Python 循环，2 万行 × 120 列就是数百万次迭代，
        以前在 GUI 线程同步跑，搜索一次冻结秒级。现在：GUI 线程持锁拍快照
        （历史行列表 + 当前屏幕行的提取结果），匹配交给 worker，结果经
        _search_finished 回 GUI；期间查询变了（序号不符）结果直接丢弃。
        """
        import terminal_widget as _tw  # 延迟引用，避免循环 import
        text = self._search_pending_text
        self._search_seq += 1
        seq = self._search_seq
        self._search_matches = []
        self._current_match_index = -1

        if not text:
            self._update_match_label()
            self.update()
            return

        history = self._get_history_top()
        with self._screen_lock:
            columns = self.screen.columns
            buffer = self.screen.buffer
            # 当前屏幕行可变：提取必须在锁内完成；历史行已冻结，交给 worker
            screen_lines = [
                self._extract_search_line(buffer[row], columns)
                for row in range(self.term_rows)
            ]
        _tw.TerminalWidget._get_search_executor().submit(
            self._search_worker, seq, text, history, screen_lines, columns,
            self.term_rows, self.scroll_offset)

    # 限制搜索结果数量，防止内存暴涨
    _MAX_SEARCH_RESULTS = 5000

    def _search_worker(self, seq, text, history, screen_lines, columns,
                       term_rows, scroll_offset):
        """后台线程：在快照上匹配，结果带序号发回 GUI。"""
        matches = []
        try:
            matches = self._match_search_snapshot(
                text, history, screen_lines, columns,
                lambda: self._search_seq != seq)
        except Exception:
            logger.exception("[Terminal] search worker failed")
        try:
            self._search_finished.emit(seq, matches)
        except RuntimeError:
            pass  # widget 已销毁

    def _match_search_snapshot(self, text, history, screen_lines, columns,
                               is_stale) -> list:
        search_lower = text.lower()
        qlen = len(search_lower)
        is_wide = self._is_wide_char
        matches = []
        limit = self._MAX_SEARCH_RESULTS

        def find_in_line(line_text, col_map, abs_row):
            line_lower = line_text.lower()
            pos = 0
            while True:
                idx = line_lower.find(search_lower, pos)
                if idx == -1:
                    return
                last_i = idx + qlen - 1
                if last_i >= len(col_map):
                    return
                start_col = col_map[idx]
                end_col = col_map[last_i] + (2 if is_wide(line_text[last_i]) else 1)
                matches.append((abs_row, start_col, end_col - start_col))
                if len(matches) >= limit:
                    return
                pos = idx + 1

        # 1) 历史行：行对象推入历史后内容不再变化，可按 id 缓存提取结果。
        #    缓存值中保留行对象引用，钉住对象保证 id 在缓存有效期内不被复用。
        cache = self._search_line_cache
        cache_lock = self._search_cache_lock
        for abs_row, line in enumerate(history):
            if (abs_row & 1023) == 0 and is_stale():
                return []
            key = id(line)
            with cache_lock:
                cached = cache.get(key)
                if cached is not None and cached[0] is line:
                    cache.move_to_end(key)
                    line_text, col_map = cached[1], cached[2]
                    cached_hit = True
                else:
                    cached_hit = False
            if not cached_hit:
                line_text, col_map = self._extract_search_line(line, columns)
                with cache_lock:
                    cache[key] = (line, line_text, col_map)
                    if len(cache) > self._SEARCH_LINE_CACHE_MAX:
                        cache.popitem(last=False)
            if line_text:
                find_in_line(line_text, col_map, abs_row)
                if len(matches) >= limit:
                    return matches

        # 2) 当前屏幕行（已在 GUI 线程持锁提取好）
        history_count = len(history)
        for row, (line_text, col_map) in enumerate(screen_lines):
            if line_text:
                find_in_line(line_text, col_map, history_count + row)
                if len(matches) >= limit:
                    break
        return matches

    def _on_search_finished(self, seq: int, matches: list):
        """GUI 线程：worker 结果到达。序号过期（查询已变/搜索栏已关）则丢弃。"""
        if seq != self._search_seq or getattr(self, '_cleaned_up', False):
            return
        self._search_matches = matches
        self._current_match_index = -1
        if matches:
            # 初始选中：离当前可见区域中心最近的匹配，避免每次都跳到最顶部
            history_count = self._get_history_count()
            total_lines = history_count + self.term_rows
            display_start = max(0, total_lines - self.term_rows - self.scroll_offset)
            center = display_start + self.term_rows // 2
            self._current_match_index = min(
                range(len(matches)), key=lambda i: abs(matches[i][0] - center)
            )
            self._scroll_to_match()

        self._update_match_label()
        self.update()

    def _search_next(self):
        """跳转到下一个匹配"""
        if self._search_matches:
            self._current_match_index = (self._current_match_index + 1) % len(self._search_matches)
            self._scroll_to_match()
            self._update_match_label()
            self.update()

    def _search_prev(self):
        """跳转到上一个匹配"""
        if self._search_matches:
            self._current_match_index = (self._current_match_index - 1) % len(self._search_matches)
            self._scroll_to_match()
            self._update_match_label()
            self.update()

    def _scroll_to_match(self):
        """滚动使当前匹配进入可见区域（已可见则不动）。

        匹配基于搜索时刻的快照：搜索后历史继续增长时，行号可能位移，
        这里只保证 clamp 在合法范围内、不崩溃不跳飞；新输出后需重新搜索。
        """
        if not self._search_matches:
            return
        if not (0 <= self._current_match_index < len(self._search_matches)):
            self._current_match_index = 0
        row, _col, _length = self._search_matches[self._current_match_index]
        history_count = self._get_history_count()
        total_lines = history_count + self.term_rows
        row = max(0, min(row, total_lines - 1))

        display_start = max(0, total_lines - self.term_rows - self.scroll_offset)
        if display_start <= row < display_start + self.term_rows:
            return  # 已在可见区域内

        # 让匹配行尽量居中
        desired_start = max(0, row - self.term_rows // 2)
        new_offset = max(0, min(history_count, history_count - desired_start))
        if new_offset != self.scroll_offset:
            self.scroll_offset = new_offset
            self._scroll_accum = 0.0
            # scroll_offset 变化 → render_state 失配 → 整屏重绘（并同步滚动条）
            self._invalidate_render_cache()

    def _update_match_label(self):
        """更新匹配计数标签"""
        total = len(self._search_matches)
        current = self._current_match_index + 1 if total > 0 else 0
        label = getattr(self, '_match_label', None)
        if label is not None:
            label.setText(f"{current}/{total}")
