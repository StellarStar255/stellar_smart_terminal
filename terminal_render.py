"""
终端渲染层 mixin（从 terminal_widget.py 拆出，行为不变）

TerminalWidget 的绘制/缓存/字符度量方法集合，纯方法搬迁：
- paintEvent / _rebuild_cache / _ensure_visible：整屏缓存 pixmap 重建、
  回看滚动 blit 平移复用、脏行增量重绘（性能守卫见
  tests/test_render_scroll_blit.py）
- _get_char_color / _compute_color / _get_256_color：颜色解析与帧级 memo
- _calculate_char_size / _probe_ascii_batch / _terminal_cell_metrics /
  _calibrate_char_width / _is_wide_char / _row_tile / _col_tile：字符度量
- _get_display_info / _invalidate_display_info / _invalidate_render_cache：
  显示区间计算与缓存失效（所有 scroll_offset/内容变化的统一入口）

状态（各类缓存、term_font、scroll_offset 等）仍由 TerminalWidget.__init__
初始化并持有；本 mixin 不可独立实例化。
"""

import unicodedata

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import (
    QColor, QFontMetrics, QFontMetricsF, QPainter, QPaintEvent, QPixmap
)

from app_logging import get_logger

logger = get_logger(__name__)


class TerminalRenderMixin:
    """TerminalWidget 的渲染方法集合（见模块 docstring）"""

    def _calculate_char_size(self):
        """计算字符尺寸"""
        # 使用 QFontMetricsF 获取浮点精度的度量
        fmf = QFontMetricsF(self.term_font)
        self.char_width, self.char_height, self.char_ascent = self._terminal_cell_metrics(fmf)
        self._font_metrics = QFontMetrics(self.term_font)
        self._ascii_batch_ok = self._probe_ascii_batch(fmf, self.char_width)

    @staticmethod
    def _probe_ascii_batch(font_metrics, cell_width: float) -> bool:
        """判断能否把同色连续 ASCII 合成一个字符串一次 drawText。

        串内字形按字体自身推进排布，只有当字体等宽（各 ASCII 推进一致）
        且推进恰好等于列宽时才与 int() 取整的列栅格对齐；cell 宽度可能被
        CJK fallback 撑大（_terminal_cell_metrics 取 max），此时必须逐格画。
        """
        try:
            adv = float(font_metrics.horizontalAdvance('W'))
            if abs(adv - cell_width) > 0.01:
                return False
            probe = 'iMl.x0!~'
            return abs(float(font_metrics.horizontalAdvance(probe)) - len(probe) * adv) < 0.5
        except Exception:
            return False

    @staticmethod
    def _terminal_cell_metrics(font_metrics):
        """返回适合终端网格的 (cell_width, cell_height, ascent)。

        终端列宽以单列字符为单位，但中文通常经由 fallback 字体绘制。
        只用拉丁等宽字体的 W 度量会低估 CJK glyph 的宽度/高度，导致
        输入提交后反色行或下一行重绘时遮住中文的一部分。
        """
        latin_width = float(font_metrics.horizontalAdvance('W'))
        cjk_width = max(
            float(font_metrics.horizontalAdvance(ch)) for ch in ('你', '好', '呀')
        ) / 2.0
        cell_width = max(1.0, latin_width, cjk_width)

        cjk_rect = font_metrics.boundingRect('你好呀')
        cell_height = max(1.0, float(font_metrics.height()), float(cjk_rect.height()) + 2.0)
        ascent = max(1.0, float(font_metrics.ascent()), float(-cjk_rect.top()) + 1.0)

        return cell_width, cell_height, ascent

    def _get_display_info(self) -> tuple:
        """获取显示信息（带缓存），返回 (history_count, total_lines, display_start)

        缓存会在以下情况自动失效：
        - scroll_offset 改变
        - _content_dirty 被设置
        - 显式调用 _invalidate_display_info()
        """
        # 检查 scroll_offset 是否改变
        cached_scroll = self._display_info.get('scroll_offset', -1)
        if self._display_info['valid'] and cached_scroll == self.scroll_offset:
            return (
                self._display_info['history_count'],
                self._display_info['total_lines'],
                self._display_info['display_start']
            )
        # 重新计算
        history_count = self._get_history_count()
        total_lines = history_count + self.term_rows
        display_start = max(0, total_lines - self.term_rows - self.scroll_offset)
        # 更新缓存
        self._display_info['history_count'] = history_count
        self._display_info['total_lines'] = total_lines
        self._display_info['display_start'] = display_start
        self._display_info['scroll_offset'] = self.scroll_offset
        self._display_info['valid'] = True
        return (history_count, total_lines, display_start)

    def _invalidate_display_info(self):
        """使显示信息缓存失效"""
        self._display_info['valid'] = False

    def _invalidate_render_cache(self):
        """使终端渲染缓存失效并安排重绘。"""
        self._content_dirty = False
        self._cache_valid = False
        self._invalidate_display_info()
        self._sync_scrollbar()
        self.update()

    # ==================== 垂直滚动条 ====================

    def _calibrate_char_width(self, painter: QPainter):
        """通过实际渲染校准字符宽度"""
        # 使用 painter 的 fontMetrics 测量
        fm = painter.fontMetrics()
        painter_width, painter_height, painter_ascent = self._terminal_cell_metrics(fm)

        # 如果 painter 的 metrics 与之前计算的不同，更新并重新计算终端大小
        if (
            abs(painter_width - self.char_width) > 1
            or abs(painter_height - self.char_height) > 1
            or abs(painter_ascent - self.char_ascent) > 1
        ):
            old_cw, old_ch = self.char_width, self.char_height
            self.char_width = painter_width
            self.char_height = painter_height
            self.char_ascent = painter_ascent
            self._ascii_batch_ok = self._probe_ascii_batch(fm, self.char_width)
            logger.debug(
                f"[Terminal] Calibration: "
                f"{old_cw:.1f}x{old_ch:.1f} -> {self.char_width:.1f}x{self.char_height:.1f}"
            )
            # 立即重新计算终端大小（不设置为0，避免当前帧渲染空白）
            QTimer.singleShot(0, self._update_terminal_size)

    def _is_wide_char(self, char: str) -> bool:
        """判断字符是否为宽字符（中文等）- 带缓存"""
        if not char:
            return False
        # 可打印 ASCII 快速路径：永远是窄字符（渲染循环里的绝大多数）
        if len(char) == 1 and ' ' <= char <= '\x7e':
            return False
        cache = self._wide_char_cache
        result = cache.get(char)
        if result is None:
            # east_asian_width 只接受单个字符，取第一个字符判断
            result = unicodedata.east_asian_width(char[0]) in ('F', 'W')
            if len(cache) >= self._WIDE_CHAR_CACHE_MAX:
                cache.clear()
            cache[char] = result
        return result

    def _row_tile(self, row, count=1):
        """返回从 row 起 count 行的 (y, height)，使相邻行像素无缝拼接

        char_height 可能是分数（如 18.5），单纯 int(char_height) 会让
        每隔几行多 1px 缝隙，导致带背景色的连续行看起来像断开的方块。
        用「下一格起点 - 当前格起点」算高度可保证逐行无缝。
        """
        top = self.PADDING + self._header_h
        y_start = int(top + row * self.char_height)
        y_end = int(top + (row + count) * self.char_height)
        return y_start, y_end - y_start

    def _col_tile(self, col, count=1):
        """返回从 col 起 count 列的 (x, width)，相邻列像素无缝拼接"""
        x_start = int(self.PADDING + col * self.char_width)
        x_end = int(self.PADDING + (col + count) * self.char_width)
        return x_start, x_end - x_start

    def paintEvent(self, event: QPaintEvent):
        """绘制终端 - 使用双缓冲提高性能"""
        # 防止在 widget 尺寸为 0 时绘制（如拖拽分离 tab 过渡期间），避免 segfault
        if self.width() <= 0 or self.height() <= 0:
            return

        painter = QPainter(self)
        if not painter.isActive():
            return

        # 检查是否需要重建缓存
        # （以前 resize 期间会跳过重建并复用旧缓存以"防重影"，但子进程不响应 SIGWINCH 时
        # 会卡住整片空白。现在直接走正常重建路径：pyte 自然 reflow，比空白可靠得多。）
        # cache pixmap 是按 size*DPR 创建的物理像素 pixmap；与 widget logical size
        # 比较时要乘 DPR 才一致。
        dpr = self.devicePixelRatioF()
        # 快速 resize 模式（弹簧/动画期间）或后台 reflow 在途：只要有可用的
        # 旧缓存，就直接贴旧图，跳过整屏重建。reflow 在途时重建会卡在
        # _screen_lock 上（worker 正持锁 mutate 屏幕/历史，可达一两百 ms），
        # 贴旧帧让 GUI 全程不冻结。无缓存时退回正常重建路径（会短暂等锁）。
        fast_scale = (
            (self._fast_resize or self._reflow_scaling)
            and self._cache_pixmap is not None
            and not self._cache_pixmap.isNull()
        )
        if not fast_scale:
            need_rebuild = (
                self._cache_pixmap is None or
                self._cache_pixmap.isNull() or
                not self._cache_valid or
                self._cache_pixmap.size() != self.size() * dpr or
                self._cache_pixmap.devicePixelRatio() != dpr
            )

            if need_rebuild:
                # 持锁重建：_rebuild_cache 会迭代 screen.buffer / history.top 的活对象，
                # 一旦 feed 移到读取线程，并发 mutate 会撕裂迭代。在调用处持锁覆盖整段
                # 重建（含绘图），代价是读取线程在重建期间(几 ms)短暂等待，可接受。
                with self._screen_lock:
                    self._rebuild_cache()

        opaque_bg = QColor(self.bg_color)
        opaque_bg.setAlpha(255)

        # 使用 Source 合成模式：完全替换像素，不做 alpha 混合。
        # 先填充背景确保终端区域自身完全不透明（防止父/兄弟 widget 内容透出）。
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), opaque_bg)
        if self._cache_pixmap and not self._cache_pixmap.isNull():
            # fast_resize（弹簧动画）期间跳过重建，旧缓存按原尺寸左上角贴图、
            # 不缩放：变宽时右侧暂露背景色、变窄时右侧裁剪，避免文字被横向
            # 拉伸/压缩；动画结束后 set_fast_resize(False) 按最终尺寸重建。
            painter.drawPixmap(0, 0, self._cache_pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 滚动指示器（右上角）。画在缓存外：回看滚动时缓存可整体平移复用，
        # 不必为了指示器重绘顶部行
        if self.scroll_offset > 0:
            painter.setPen(QColor("#667eea"))
            painter.drawText(self.width() - 180, 20,
                             f"[History: +{self.scroll_offset} lines]")

        # 选区高亮单独绘制（不在缓存中，避免拖动时重建缓存）
        if self._has_selection():
            if self._select_all_mode:
                # 全选模式：高亮所有可见行
                x, width = self._col_tile(0, self.term_cols)
                for sel_row in range(self.term_rows):
                    y, height = self._row_tile(sel_row)
                    painter.fillRect(x, y, width, height, self._selection_color)
            else:
                # 使用绝对坐标计算可见的选择范围
                visible_range = self._get_visible_selection_range()
                if visible_range:
                    start_row, start_col, end_row, end_col = visible_range

                    # 获取绝对选择范围来正确处理跨页选择的列
                    abs_start, abs_end = self._get_selection_range()
                    abs_start_row, abs_start_col = abs_start
                    abs_end_row, abs_end_col = abs_end

                    # 使用渲染时记录的 display_start，确保高亮与内容一致
                    display_start = self._rendered_display_start

                    for sel_row in range(start_row, end_row + 1):
                        abs_row = display_start + sel_row

                        # 计算该行的列范围
                        if abs_row == abs_start_row and abs_row == abs_end_row:
                            # 选择在同一行
                            col_start = abs_start_col
                            col_end = abs_end_col
                        elif abs_row == abs_start_row:
                            # 选择的第一行
                            col_start = abs_start_col
                            col_end = self.term_cols - 1
                        elif abs_row == abs_end_row:
                            # 选择的最后一行
                            col_start = 0
                            col_end = abs_end_col
                        else:
                            # 中间行，全选
                            col_start = 0
                            col_end = self.term_cols - 1

                        x, width = self._col_tile(col_start, col_end - col_start + 1)
                        y, height = self._row_tile(sel_row)

                        painter.fillRect(x, y, width, height, self._selection_color)

        # 搜索高亮单独绘制
        if self._search_matches:
            # 使用渲染时记录的 display_start，确保高亮与内容一致
            display_start = self._rendered_display_start

            for idx, (match_row, match_col, match_len) in enumerate(self._search_matches):
                display_row = match_row - display_start
                if 0 <= display_row < self.term_rows:
                    x, width = self._col_tile(match_col, match_len)
                    y, height = self._row_tile(display_row)

                    if idx == self._current_match_index:
                        painter.fillRect(x, y, width, height, self._search_current_color)
                    else:
                        painter.fillRect(x, y, width, height, self._search_highlight_color)

        # 光标单独绘制（因为会闪烁）
        if self.scroll_offset == 0 and self.cursor_visible and self.hasFocus() and not self.screen.cursor.hidden:
            cx = self.screen.cursor.x
            cy = self.screen.cursor.y

            if 0 <= cy < self.term_rows and 0 <= cx < self.term_cols:
                cursor_x, cursor_w = self._col_tile(cx)
                cursor_y, cursor_h = self._row_tile(cy)

                painter.fillRect(
                    cursor_x, cursor_y,
                    cursor_w, cursor_h,
                    self._cursor_color
                )

        # 绘制输入法预编辑文本（拼音字母）
        if self._preedit_string and self.hasFocus():
            cx = self.screen.cursor.x
            cy = self.screen.cursor.y

            if 0 <= cy < self.term_rows:
                preedit_x, _ = self._col_tile(cx)
                preedit_y, preedit_height = self._row_tile(cy)

                # 设置预编辑文本样式
                painter.setFont(self.term_font)

                # 计算预编辑文本宽度（使用缓存的 QFontMetrics）
                preedit_width = self._font_metrics.horizontalAdvance(self._preedit_string)

                # 绘制预编辑文本背景（使用缓存的颜色）
                painter.fillRect(
                    preedit_x, preedit_y,
                    preedit_width, preedit_height,
                    self._preedit_bg_color
                )

                # 绘制预编辑文本（使用缓存的颜色）
                painter.setPen(self._preedit_fg_color)
                text_y = int(preedit_y + self.char_ascent)
                painter.drawText(preedit_x, text_y, self._preedit_string)

                # 绘制下划线表示正在编辑（使用缓存的画笔）
                underline_y = preedit_y + preedit_height - 2
                painter.setPen(self._preedit_underline_pen)
                painter.drawLine(preedit_x, underline_y, preedit_x + preedit_width, underline_y)

    def _rebuild_cache(self):
        """重建缓存的pixmap"""
        # 防止在 widget 尺寸为 0 时创建无效 pixmap（如拖拽分离 tab 过渡期间）
        if self.width() <= 0 or self.height() <= 0:
            return

        # 创建或调整pixmap大小
        # 在 HiDPI（Retina）上必须显式按 devicePixelRatio 创建 pixmap，否则：
        # - QPixmap(self.size()) 拿到的是 logical pixel 大小、默认 DPR=1.0
        # - widget 实际 DPR=2.0 → 贴图时 Qt 会拉伸 / 重采样
        # - painter 在 1x pixmap 上渲染字体度量会与 logical 坐标不一致
        # - CJK glyph 视觉宽度被放大、超过 cell 分配 → 后字盖住前字（"被切半"）
        dpr = self.devicePixelRatioF()
        target_size = self.size() * dpr
        pixmap_recreated = False
        if (self._cache_pixmap is None
                or self._cache_pixmap.isNull()
                or self._cache_pixmap.size() != target_size
                or self._cache_pixmap.devicePixelRatio() != dpr):
            self._cache_pixmap = QPixmap(target_size)
            if self._cache_pixmap.isNull():
                return
            self._cache_pixmap.setDevicePixelRatio(dpr)
            pixmap_recreated = True

        opaque_bg = QColor(self.bg_color)
        opaque_bg.setAlpha(255)

        painter = QPainter(self._cache_pixmap)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(self.term_font)

        # 在第一次绘制时校准字符宽度
        if self._needs_calibration:
            self._calibrate_char_width(painter)
            self._needs_calibration = False

        # 使用缓存的显示信息（避免重复计算）
        history_count, total_lines, start_line = self._get_display_info()

        # 记录本次渲染使用的 display_start，供鼠标坐标转换使用
        self._rendered_display_start = start_line

        # 计算结束行
        end_line = min(total_lines, start_line + self.term_rows)

        # 只拷可见区间的历史行；scroll_offset == 0（绝大多数帧）时
        # start_line >= history_count，可见行全部来自 screen.buffer，零拷贝
        if start_line < history_count:
            history = self._get_history_slice(start_line, min(end_line, history_count))
        else:
            history = []

        # 准备要显示的行 - 只获取需要的部分，避免复制整个历史记录
        display_lines = []
        for line_idx in range(start_line, end_line):
            if line_idx < history_count:
                # 从历史切片中获取（切片以 start_line 为原点）
                display_lines.append(history[line_idx - start_line])
            else:
                # 从当前屏幕缓冲区获取
                buffer_row = line_idx - history_count
                if 0 <= buffer_row < self.term_rows:
                    display_lines.append(self.screen.buffer[buffer_row])

        # 计算可见区域的最大列数（防止绘制到可见区域之外）
        visible_width = self._cache_pixmap.width() - self.PADDING * 2
        max_visible_cols = int(visible_width / self.char_width) if self.char_width > 0 else self.term_cols

        # 使用 term_cols 为主限制（从 widget 尺寸计算得出），
        # max_visible_cols 为安全上限。不使用 screen.columns 因为 resize 后它可能滞后。
        num_cols = min(self.term_cols, max_visible_cols)

        # ---- 增量重绘判定 ----
        # 渲染状态快照：任何影响布局/配色的因素变化都会触发整屏重绘。
        # 快照一致 + 处于底部（scroll_offset == 0，此时 display_row == buffer_row，
        # 且可见行全部来自 screen.buffer）时，只重绘 pyte 标脏的行（screen.dirty）。
        # _total_history_lines 纳入快照：有新行推入历史即整屏重绘，
        # 避免历史 deque 滚动导致可见内容位移而未被标脏。
        screen_dirty = getattr(self.screen, 'dirty', None)
        render_state = (
            start_line, end_line, num_cols, self.term_rows,
            history_count,
            getattr(self.screen, '_total_history_lines', -1),
            getattr(self.screen, '_in_alt_screen', False),
            round(self.char_width * 1000),
            round(self.char_height * 1000),
            round(self.char_ascent * 1000),
            self._header_h,
            self.bg_color.rgba(), self.fg_color.rgba(),
            self.term_font.key(),
            self._render_epoch,
        )
        rows_to_draw = None  # None = 整屏重绘
        if (not pixmap_recreated
                and screen_dirty is not None
                and self.scroll_offset == 0
                and self._last_render_state == render_state):
            visible_rows = end_line - start_line
            dirty_rows = set()
            for dirty_r in screen_dirty:
                # 脏行上下各扩一行，防御个别字形（emoji/fallback glyph）
                # 略微越出本行像素带时留下残影
                for rr in (dirty_r - 1, dirty_r, dirty_r + 1):
                    if 0 <= rr < visible_rows:
                        dirty_rows.add(rr)
            if not dirty_rows:
                # 内容无行级变化（如仅光标移动/OSC 标题），pixmap 已是最新
                painter.end()
                self._cache_valid = True
                self._last_render_state = render_state
                screen_dirty.clear()
                return
            rows_to_draw = dirty_rows
        elif (not pixmap_recreated
                and screen_dirty is not None
                and not screen_dirty
                and self._last_render_state is not None
                and self._last_render_state[2:] == render_state[2:]):
            # ---- 回看滚动的平移复用 ----
            # 除可见窗口位置（start/end_line）外渲染状态完全一致且无脏行：
            # 内容只是整体上下平移。把已有 pixmap 内容按行距平移，只重绘
            # 新露出的行，避免滚轮每 tick 都整屏逐格重画。
            last_start, last_end = self._last_render_state[0], self._last_render_state[1]
            visible_rows = end_line - start_line
            delta = start_line - last_start
            dpr_i = round(dpr)
            if delta == 0:
                # 位置也没变（如回看期间光标闪烁触发的重绘）→ 无事可做
                rows_to_draw = set()
            elif (abs(delta) < visible_rows
                    and last_end - last_start == visible_rows
                    and abs(dpr - dpr_i) < 1e-6):
                # 行距通常是分数像素（CJK 度量撑出），各行像素带高在 ±1px 间
                # 波动：新旧带高一致的行成段 1:1 blit，带高不同的行加入重绘集，
                # 保证任何行距下都与整屏重绘逐像素一致（宁多画几行勿花屏）。
                # drawPixmap 的源矩形按【物理像素】解释（源 pixmap 需带 DPR），
                # 故源坐标乘 dpr；分数 DPR 下物理坐标不精确，回退整屏重绘。
                pad_top_f = self.PADDING + self._header_h
                ch_f = self.char_height
                if delta > 0:
                    # 向下滚（内容上移）：新露出的是底部 delta 行
                    kept = range(visible_rows - delta)
                    rows_to_draw = set(range(visible_rows - delta, visible_rows))
                else:
                    # 向上滚（内容下移）：新露出的是顶部 |delta| 行
                    kept = range(-delta, visible_rows)
                    rows_to_draw = set(range(-delta))
                src = self._cache_pixmap.copy()
                src.setDevicePixelRatio(dpr)
                w_phys = src.width()
                painter.save()
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Source)
                seg_y_new = seg_y_old = seg_h = 0
                for r in kept:
                    y_new = int(pad_top_f + r * ch_f)
                    h_new = int(pad_top_f + (r + 1) * ch_f) - y_new
                    y_old = int(pad_top_f + (r + delta) * ch_f)
                    h_old = int(pad_top_f + (r + delta + 1) * ch_f) - y_old
                    if h_new != h_old:
                        rows_to_draw.add(r)
                        if seg_h:
                            painter.drawPixmap(0, seg_y_new, src,
                                               0, seg_y_old * dpr_i,
                                               w_phys, seg_h * dpr_i)
                            seg_h = 0
                        continue
                    if seg_h == 0:
                        seg_y_new, seg_y_old = y_new, y_old
                    seg_h += h_new
                if seg_h:
                    painter.drawPixmap(0, seg_y_new, src,
                                       0, seg_y_old * dpr_i,
                                       w_phys, seg_h * dpr_i)
                painter.restore()
                self._scroll_blit_hits += 1

        char_width = self.char_width
        char_height = self.char_height
        bg_default = opaque_bg
        last_fg_color = None  # 缓存上一个前景色，减少 setPen 调用
        batch_ok = getattr(self, '_ascii_batch_ok', False)
        padding = self.PADDING  # 局部变量加速（横向用）
        pad_top = self.PADDING + self._header_h  # 纵向起点（含标题栏占用）
        # 帧级属性 memo：同帧内 (fg, bg, bold, reverse) 组合高度重复
        # （整屏通常只有个位数种），把每格 2 次颜色解析 + 1 次可见性
        # 校正折叠成一次字典查询。值：(可见前景色, 背景色, 是否非默认背景)
        attr_memo = {}
        if rows_to_draw is None:
            # 整屏重绘：清空为背景色（opaque_bg 为不透明色，fillRect 等效 fill；
            # +1 防御分数 DPR 下 logical→physical 取整在边缘留下残留像素）
            painter.fillRect(0, 0, self.width() + 1, self.height() + 1, opaque_bg)
        for display_row, buffer_line in enumerate(display_lines):
            if rows_to_draw is not None and display_row not in rows_to_draw:
                continue
            row_y = int(pad_top + display_row * char_height)
            # 用下一行起点减去本行起点作为本行高度，防止分数像素累计造成行间空隙
            row_h = int(pad_top + (display_row + 1) * char_height) - row_y
            if rows_to_draw is not None:
                # 增量重绘：仅清空本行像素带（整行宽度，含左右 padding）
                painter.fillRect(0, row_y, self.width() + 1, row_h, opaque_bg)
            text_y = int(row_y + self.char_ascent)
            row_cells = []

            # pyte 的行是稀疏 dict：没写过的列走 __missing__ 造默认空格。
            # 默认空格（无背景）画不出任何东西，按行内实际占用的最大列
            # 截断循环，行尾大段空白不再逐格全速处理。
            row_cols = num_cols
            keys = getattr(buffer_line, 'keys', None)
            if keys is not None:
                max_col = max((k for k in keys() if k < num_cols), default=-1)
                row_cols = max_col + 1

            for col in range(row_cols):
                try:
                    char = buffer_line[col]
                except (KeyError, IndexError, TypeError):
                    continue

                # 优化：一次性获取所有属性，避免多次 getattr 调用
                # pyte 的 Char 是 namedtuple，可以直接访问属性
                try:
                    char_text = char.data
                    char_fg = char.fg
                    char_bg = char.bg
                    char_bold = char.bold
                    char_reverse = char.reverse
                except AttributeError:
                    if isinstance(char, str):
                        char_text = char
                        char_fg = 'default'
                        char_bg = 'default'
                        char_bold = False
                        char_reverse = False
                    else:
                        continue

                # 计算颜色（提前到 skip 之前，以便空 data 时也能填充背景）
                try:
                    attr_key = (char_fg, char_bg, char_bold, char_reverse)
                    attrs = attr_memo.get(attr_key)
                except TypeError:
                    # 防御：不可哈希的颜色值（如 list），直连慢路径
                    attr_key = None
                    attrs = None
                if attrs is None:
                    fg_color = self._get_char_color(char_fg, char_bold)
                    bg_color = self._get_char_color(char_bg, False, is_bg=True)
                    # 处理反转
                    if char_reverse:
                        fg_color, bg_color = bg_color, fg_color
                    attrs = (self._ensure_visible(fg_color, bg_color),
                             bg_color,
                             bg_color != bg_default)
                    if attr_key is not None:
                        attr_memo[attr_key] = attrs
                vis_fg, bg_color, has_bg = attrs

                # 空 data 且无背景色 → 真的没东西可画，跳过
                if not char_text and not has_bg:
                    continue

                cell_x = int(padding + col * char_width)

                # 判断宽字符（空 data 视为单宽，等同于普通空格的填充）；
                # 可打印 ASCII 内联判窄，免去每格一次方法调用
                if not char_text or (len(char_text) == 1
                                     and ' ' <= char_text <= '\x7e'):
                    span = 1
                else:
                    span = 2 if self._is_wide_char(char_text) else 1

                # 用「下一格起点 - 当前格起点」算宽度，使背景在水平方向也无缝拼接
                char_draw_width = int(padding + (col + span) * char_width) - cell_x
                row_cells.append((
                    cell_x,
                    char_draw_width,
                    char_text,
                    vis_fg,
                    bg_color,
                    has_bg,
                ))

            # 先绘制整行背景，再绘制文字。宽字符在 pyte 中会占用后一格
            # 空 stub；如果按 cell 交替绘制，带背景的 stub 会盖住中文右半边。
            for cell_x, char_draw_width, _char_text, _fg_color, bg_color, has_bg in row_cells:
                if has_bg:
                    painter.fillRect(cell_x, row_y, char_draw_width, row_h, bg_color)

            # 字符严格按 pyte 的 cell 坐标绘制。pyte 已用 wcwidth 为 CJK
            # 留出 stub cell；额外按 glyph advance 推动 x 会在 Claude/Ink
            # 提交后局部重绘时累积偏移，导致中文只显示半个或错位。
            #
            # 合批：同色、列连续的单宽可打印 ASCII 合成一个字符串一次
            # drawText（batch_ok 保证等宽推进 == 列宽，串内不会漂离栅格），
            # 全屏重绘从数千次 drawText 降到每行个位数；CJK/制表符/emoji
            # 等一律保持逐格绘制，维持上述对齐行为。
            run_x = 0
            run_next = -1
            run_color = None
            run_parts = []
            for cell_x, cell_w, char_text, fg, _bg_color, _has_bg in row_cells:
                if not char_text or char_text == ' ':
                    # 空格无字形；恰好接在 run 尾部时并入串保持推进，避免
                    # "hello world" 因中间空格断成两次 drawText
                    if run_parts and char_text == ' ' and cell_x == run_next:
                        run_parts.append(' ')
                        run_next = cell_x + cell_w
                    continue
                # fg 已是可见性校正后的前景色（cell 循环里随属性 memo 算好）
                if batch_ok and len(char_text) == 1 and '!' <= char_text <= '~':
                    if run_parts and fg == run_color and cell_x == run_next:
                        run_parts.append(char_text)
                        run_next = cell_x + cell_w
                        continue
                    if run_parts:
                        if run_color != last_fg_color:
                            painter.setPen(run_color)
                            last_fg_color = run_color
                        painter.drawText(run_x, text_y, ''.join(run_parts))
                    run_parts = [char_text]
                    run_x = cell_x
                    run_next = cell_x + cell_w
                    run_color = fg
                    continue
                # 非合批字符：先冲刷未完成的 run，再按 cell 坐标逐格画
                if run_parts:
                    if run_color != last_fg_color:
                        painter.setPen(run_color)
                        last_fg_color = run_color
                    painter.drawText(run_x, text_y, ''.join(run_parts))
                    run_parts = []
                    run_next = -1
                if fg != last_fg_color:
                    painter.setPen(fg)
                    last_fg_color = fg
                painter.drawText(cell_x, text_y, char_text)
            if run_parts:
                if run_color != last_fg_color:
                    painter.setPen(run_color)
                    last_fg_color = run_color
                painter.drawText(run_x, text_y, ''.join(run_parts))

        # 注意：选区高亮、搜索高亮、滚动指示器已移至 paintEvent 中单独绘制，
        # 缓存里只有内容行，滚动时才能整体平移复用

        painter.end()
        self._cache_valid = True
        # 记录本次渲染状态并消费脏行集合（本 widget 是 screen.dirty 的唯一消费者）
        self._last_render_state = render_state
        if screen_dirty is not None:
            screen_dirty.clear()

    def _ensure_visible(self, fg: QColor, bg: QColor) -> QColor:
        """确保前景色在背景色上可见，支持深色和浅色背景 - 带 LRU 缓存"""
        # 构建缓存键（使用 RGB 值作为键）
        cache_key = (fg.rgb(), bg.rgb())
        cache = self._visible_color_cache
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        # 计算亮度 (perceived luminance)
        fg_lum = 0.299 * fg.red() + 0.587 * fg.green() + 0.114 * fg.blue()
        bg_lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()

        # 计算对比度差
        contrast = abs(fg_lum - bg_lum)
        min_contrast = 60  # 最小对比度要求

        if contrast < min_contrast:
            if bg_lum > 128:
                # 浅色背景：向黑色混合，保证低对比文字仍可读。
                result = fg
                for factor in (0.75, 0.55, 0.35, 0.2, 0.0):
                    candidate = QColor(
                        int(fg.red() * factor),
                        int(fg.green() * factor),
                        int(fg.blue() * factor),
                    )
                    cand_lum = 0.299 * candidate.red() + 0.587 * candidate.green() + 0.114 * candidate.blue()
                    if abs(cand_lum - bg_lum) >= min_contrast:
                        result = candidate
                        break
            else:
                # 深色背景：向白色混合。以前只做小幅 RGB 加法，黑色/深灰
                # 文字仍会停留在背景附近，Codex/ratatui 的低亮度输入行会不可见。
                result = fg
                for factor in (0.35, 0.5, 0.65, 0.8, 1.0):
                    candidate = QColor(
                        int(fg.red() + (255 - fg.red()) * factor),
                        int(fg.green() + (255 - fg.green()) * factor),
                        int(fg.blue() + (255 - fg.blue()) * factor),
                    )
                    cand_lum = 0.299 * candidate.red() + 0.587 * candidate.green() + 0.114 * candidate.blue()
                    if abs(cand_lum - bg_lum) >= min_contrast:
                        result = candidate
                        break

            result_lum = 0.299 * result.red() + 0.587 * result.green() + 0.114 * result.blue()
            if abs(result_lum - bg_lum) < min_contrast:
                result = QColor("#1f2328") if bg_lum > 128 else QColor(self._current_colors["default"])
        else:
            result = fg

        # 超限整体清空（护栏，正常打不到）
        if len(cache) >= self._COLOR_CACHE_MAX:
            cache.clear()
        cache[cache_key] = result
        return result

    def _get_char_color(self, color, bold: bool = False, is_bg: bool = False) -> QColor:
        """获取字符颜色 - 支持各种格式，带 LRU 缓存"""
        # 默认颜色 - 直接返回预设值
        if color == "default" or color is None:
            return self.bg_color if is_bg else self.fg_color

        # 构建缓存键
        if isinstance(color, (tuple, list)):
            cache_key = (tuple(color), bold, is_bg)
        else:
            cache_key = (color, bold, is_bg)

        # 检查缓存
        cache = self._color_cache
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        # 计算颜色
        result = self._compute_color(color, bold, is_bg)

        # 超限整体清空（护栏，正常打不到）
        if len(cache) >= self._COLOR_CACHE_MAX:
            cache.clear()

        cache[cache_key] = result
        return result

    def _compute_color(self, color, bold: bool, is_bg: bool) -> QColor:
        """计算颜色值（内部方法）"""
        # ANSI 黑色背景（SGR 40）：调色板里的 "black"(#5c6370) 是为了让黑色
        # **前景**在深色主题上可读而刻意调亮的灰蓝，用作**背景**会把 ncurses
        # 应用的黑底区域（如贪吃蛇尾部擦除写入的 bg=black 空格、弹窗底色）
        # 渲染成扎眼的灰块。真实终端里 ANSI 黑色背景与默认背景几乎一致，
        # 这里直接映射为终端默认背景色，黑底即"看不见"。
        if is_bg and color == 'black':
            return QColor(self.bg_color)

        # RGB元组 (真彩色 24-bit)
        if isinstance(color, (tuple, list)) and len(color) == 3:
            return QColor(color[0], color[1], color[2])

        # 字符串颜色
        if isinstance(color, str):
            if color.startswith('#'):
                return QColor(color)

            # 6位十六进制颜色
            if len(color) == 6:
                try:
                    int(color, 16)
                    return QColor(f'#{color}')
                except ValueError:
                    pass

            # 标准颜色名
            colors = self._current_bright_colors if bold and not is_bg else self._current_colors
            if color in colors:
                return QColor(colors[color])
            try:
                color = int(color)
            except ValueError:
                return self.fg_color if not is_bg else self.bg_color

        # 数字颜色 (0-255)
        if isinstance(color, int):
            return self._get_256_color(color, bold)

        return self.bg_color if is_bg else self.fg_color

    def _get_256_color(self, idx: int, bold: bool = False) -> QColor:
        """获取256色 - 保持原始色彩"""
        # 标准16色
        if idx < 8:
            names = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
            colors = self._current_bright_colors if bold else self._current_colors
            return QColor(colors.get(names[idx], "#cccccc"))
        elif idx < 16:
            names = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
            return QColor(self._current_bright_colors.get(names[idx - 8], "#f5f5f5"))
        elif idx < 232:
            # 216色立方 (6x6x6) - 保持原始颜色
            idx -= 16
            r = (idx // 36) * 51
            g = ((idx // 6) % 6) * 51
            b = (idx % 6) * 51
            return QColor(r, g, b)
        else:
            # 24级灰度 - 保持原始灰度
            gray = (idx - 232) * 10 + 8
            return QColor(gray, gray, gray)
