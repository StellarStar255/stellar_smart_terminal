"""
嵌入式终端控件
基于PyQt6 + pyte实现的完整终端模拟器
支持 Windows (pywinpty) 和 Unix (pty) 平台
"""
import os
import re
import sys
import shutil
import codecs
from typing import Optional, List
from pathlib import Path
import unicodedata

import pyte

from terminal_backend import create_backend, TerminalBackend
from i18n import t


class CompatibleHistoryScreen(pyte.HistoryScreen):
    """兼容性修复：处理新版 pyte 传递的 private 参数
    并实现备用屏幕缓冲区（mode 1049/47/1047），pyte 0.8 原生不支持。
    """

    # 备用屏幕相关的私有模式号
    _ALT_SCREEN_MODES = frozenset({47, 1047, 1049})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 累计推入历史的总行数（用于滚动位置稳定化）
        self._total_history_lines = 0
        # 软换行追踪：存储自动换行的行对象 id
        self._soft_wrapped_ids = set()
        self._in_draw = False

        # 备用屏幕缓冲区支持
        self._in_alt_screen = False
        self._saved_main_buffer = None
        self._saved_main_cursor = None
        self._saved_main_history_lines = 0

    def select_graphic_rendition(self, *attrs, **kwargs):
        # 移除 private 参数（新版 pyte 会传递，但基类不支持）
        kwargs.pop('private', None)
        return super().select_graphic_rendition(*attrs, **kwargs)

    # ------ 备用屏幕缓冲区 ------

    def set_mode(self, *modes, **kwargs):
        """拦截 private mode 设置，处理备用屏幕切换"""
        if kwargs.get('private') and not self._in_alt_screen:
            if self._ALT_SCREEN_MODES & set(modes):
                self._enter_alt_screen(save_cursor=(1049 in modes))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        """拦截 private mode 重置，处理备用屏幕退出"""
        if kwargs.get('private') and self._in_alt_screen:
            if self._ALT_SCREEN_MODES & set(modes):
                self._leave_alt_screen(restore_cursor=(1049 in modes))
        super().reset_mode(*modes, **kwargs)

    def _enter_alt_screen(self, save_cursor=True):
        """进入备用屏幕：保存主缓冲区，创建空的备用缓冲区"""
        import copy
        # 保存主屏幕的缓冲区和光标
        self._saved_main_buffer = copy.deepcopy(self.buffer)
        self._saved_main_history_lines = self._total_history_lines
        if save_cursor:
            self._saved_main_cursor = copy.copy(self.cursor)
        self._in_alt_screen = True
        # 清空当前缓冲区作为备用屏幕
        for row in range(self.lines):
            self.buffer[row].clear()
        self.cursor.x = 0
        self.cursor.y = 0

    def _leave_alt_screen(self, restore_cursor=True):
        """退出备用屏幕：恢复主缓冲区"""
        if self._saved_main_buffer is not None:
            self.buffer = self._saved_main_buffer
            self._total_history_lines = self._saved_main_history_lines
            if restore_cursor and self._saved_main_cursor is not None:
                self.cursor.x = self._saved_main_cursor.x
                self.cursor.y = self._saved_main_cursor.y
            self._saved_main_buffer = None
            self._saved_main_cursor = None
        self._in_alt_screen = False

    # ------ 原有功能 ------

    def draw(self, *chars):
        self._in_draw = True
        try:
            super().draw(*chars)
        finally:
            self._in_draw = False

    def linefeed(self):
        if not self._in_draw:
            # 显式换行(\n)：确保当前行不被标记为软换行
            self._soft_wrapped_ids.discard(id(self.buffer[self.cursor.y]))
        super().linefeed()

    def index(self):
        """重写以追踪推入历史的行数和软换行状态"""
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom and not self._in_alt_screen:
            # 备用屏幕上不追踪主屏幕的历史行数
            self._total_history_lines += 1
        if self._in_draw:
            # draw() 触发的 index 是自动换行（软换行）
            self._soft_wrapped_ids.add(id(self.buffer[self.cursor.y]))
        if self._in_alt_screen:
            # 备用屏幕上不推入主屏幕历史，直接调用 Screen.index
            pyte.Screen.index(self)
        else:
            super().index()

    def erase_in_display(self, how=0, *args, **kwargs):
        # 清屏时清理软换行标记
        if how == 2 or how == 3:
            self._soft_wrapped_ids.clear()
        super().erase_in_display(how, *args, **kwargs)

    def reset(self):
        # reset() 可能在 __init__ 中被 super().__init__() 调用
        if hasattr(self, '_soft_wrapped_ids'):
            self._soft_wrapped_ids.clear()
            self._in_draw = False
        if hasattr(self, '_in_alt_screen') and self._in_alt_screen:
            self._in_alt_screen = False
            self._saved_main_buffer = None
            self._saved_main_cursor = None
        super().reset()

    def is_soft_wrapped(self, buffer_line) -> bool:
        """检查指定行是否因自动换行而换行"""
        return id(buffer_line) in self._soft_wrapped_ids


from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QApplication, QMenu, QLineEdit,
    QHBoxLayout, QPushButton, QLabel, QFileDialog, QSizePolicy
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer, QEvent, QPoint, QUrl, QMimeData
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPen, QFontMetrics, QFontMetricsF, QFontInfo,
    QKeyEvent, QPaintEvent, QResizeEvent, QShortcut, QKeySequence,
    QMouseEvent, QAction, QDesktopServices, QDragEnterEvent, QDropEvent,
    QPixmap, QImage
)


class TerminalSignalBridge(QThread):
    """信号桥：将后端回调转换为 Qt 信号（线程安全）"""
    output_received = pyqtSignal(bytes)
    process_finished = pyqtSignal(int)


class TerminalWidget(QWidget):
    """
    完整终端模拟器控件
    使用pyte进行终端转义序列处理
    """
    # 信号
    input_recorded = pyqtSignal(str)
    output_recorded = pyqtSignal(str)
    session_ended = pyqtSignal()
    image_pasted = pyqtSignal(str)  # 图片粘贴信号，参数为图片路径
    raw_output_received = pyqtSignal(str)  # 原始输出信号（用于 OpenAI API 服务器）
    close_tab_requested = pyqtSignal()  # 请求关闭当前标签页 (Cmd+W)
    new_tab_requested = pyqtSignal()  # 请求新建标签页 (Cmd+T)
    manage_presets_requested = pyqtSignal()  # 请求打开预设管理对话框
    add_command_requested = pyqtSignal()  # 请求添加新命令
    manage_local_presets_requested = pyqtSignal()  # 请求打开本地预设管理对话框
    add_local_command_requested = pyqtSignal()  # 请求添加新本地命令
    split_horizontal_requested = pyqtSignal()  # 请求左右分屏
    split_vertical_requested = pyqtSignal()  # 请求上下分屏
    close_split_requested = pyqtSignal()  # 请求关闭当前分屏

    # 鲜艳的终端颜色 - One Dark Pro 风格
    DEFAULT_COLORS = {
        "black": "#5c6370",
        "red": "#e06c75",
        "green": "#98c379",
        "brown": "#e5c07b",
        "yellow": "#e5c07b",
        "blue": "#61afef",
        "magenta": "#c678dd",
        "cyan": "#56b6c2",
        "white": "#abb2bf",
        "default": "#abb2bf",
    }

    BRIGHT_COLORS = {
        "black": "#7f848e",
        "red": "#f44747",
        "green": "#b5cea8",
        "brown": "#dcdcaa",
        "yellow": "#dcdcaa",
        "blue": "#9cdcfe",
        "magenta": "#d4a0ff",
        "cyan": "#4ec9b0",
        "white": "#ffffff",
    }

    # 预编译正则表达式（用于过滤不支持的转义序列）
    _RE_SYNC_OUTPUT = re.compile(r'\x1b\[\?2026[hl]')
    _RE_KITTY_KEYBOARD = re.compile(r'\x1b\[[\?<>=]+u')
    _RE_TERMINAL_QUERY = re.compile(r'\x1b\[>[\d;]*[cmuq]')
    _RE_FOCUS_REPORT = re.compile(r'\x1b\[\?1004[hl]')
    _RE_CURSOR_STYLE = re.compile(r'\x1b\[\d* q')  # DECSCUSR: CSI Ps SP q
    _RE_OSC_HYPERLINK = re.compile(r'\x1b\]8;[^;\x07\x1b]*;[^\x07\x1b]*(?:\x07|\x1b\\)')
    _RE_OSC_HYPERLINK_END = re.compile(r'\x1b\]8;;(?:\x07|\x1b\\)')
    _RE_OSC_TITLE = re.compile(r'\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)')
    # 捕获所有其他未处理的 OSC 序列 (OSC 7=CWD, 133=shell integration 等)
    # 防止 Linux 上 shell 发送的 OSC 序列中的数字泄漏到显示缓冲区
    _RE_OSC_OTHER = re.compile(r'\x1b\]\d+;[^\x07\x1b]*(?:\x07|\x1b\\)')
    _RE_DA_QUERY = re.compile(r'\x1b\[0?c')
    # DCS (Device Control String): \x1bP ... ST — pyte 不支持，内容会泄漏到显示缓冲区
    # APC (Application Program Command): \x1b_ ... ST
    # PM (Privacy Message): \x1b^ ... ST
    # ST = \x07 或 \x1b\\
    _RE_DCS = re.compile(r'\x1bP[^\x1b]*(?:\x1b\x5c|\x07)')
    _RE_APC = re.compile(r'\x1b_[^\x1b]*(?:\x1b\x5c|\x07)')
    _RE_PM = re.compile(r'\x1b\^[^\x1b]*(?:\x1b\x5c|\x07)')

    # 终端内容边距（像素），左右各 PADDING，上下各 PADDING
    PADDING = 8

    # 原始输出诊断捕获（调试用）
    _debug_capture_enabled = False
    _debug_capture_file = None

    # 媒体文件扩展名集合（类级别，避免重复创建）
    _AUDIO_EXTENSIONS = frozenset({'.wav', '.mp3', '.m4a', '.ogg', '.flac', '.webm', '.aac'})
    _VIDEO_EXTENSIONS = frozenset({'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.m4v', '.mpeg', '.mpg'})
    _IMAGE_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.ico'})

    def __init__(self, parent=None):
        super().__init__(parent)

        # 跨平台终端后端
        self._backend: Optional[TerminalBackend] = None
        self._signal_bridge: Optional[TerminalSignalBridge] = None

        # 工作目录（用于自动启动终端时）
        self._working_dir: Optional[str] = None

        # 终端尺寸（限制行数减少空白）
        self.term_cols = 120
        self.term_rows = 30

        # pyte终端模拟 - 历史记录限制（20000行足够，减少内存占用）
        # 使用兼容性修复类，解决新版 pyte 的参数问题
        self.screen = CompatibleHistoryScreen(self.term_cols, self.term_rows, history=20000)
        self.screen.set_mode(pyte.modes.LNM)  # 换行模式
        self.screen.set_mode(pyte.modes.DECAWM)  # 自动换行模式
        self.screen.set_mode(pyte.modes.DECTCEM)  # 显示光标

        # 恢复正常的pyte行为 - 不再干预清除操作
        # Claude Code的TUI需要正常的清除功能才能正确显示
        self.stream = pyte.Stream(self.screen)

        # 字体设置 - 使用真正的等宽字体，按平台选择最佳字体
        if sys.platform == "darwin":
            # macOS: Monaco → Menlo → SF Mono
            self.term_font = QFont("Monaco", 12)
            self.term_font.setStyleHint(QFont.StyleHint.Monospace)
            self.term_font.setFixedPitch(True)
            font_info = QFontInfo(self.term_font)
            if font_info.family().lower() != "monaco":
                self.term_font = QFont("Menlo", 12)
                self.term_font.setStyleHint(QFont.StyleHint.Monospace)
                self.term_font.setFixedPitch(True)
        elif sys.platform == "win32":
            # Windows: Cascadia Mono → Consolas
            self.term_font = QFont("Cascadia Mono", 12)
            self.term_font.setStyleHint(QFont.StyleHint.Monospace)
            self.term_font.setFixedPitch(True)
            font_info = QFontInfo(self.term_font)
            if "cascadia" not in font_info.family().lower():
                self.term_font = QFont("Consolas", 12)
                self.term_font.setStyleHint(QFont.StyleHint.Monospace)
                self.term_font.setFixedPitch(True)
        else:
            # Linux: 使用 setFamilies 设置字体优先级列表，确保找到真正的等宽字体
            linux_mono_fonts = [
                "DejaVu Sans Mono",   # 几乎所有 Linux 发行版都有
                "Liberation Mono",     # Fedora/RHEL 常见
                "Noto Sans Mono",      # Noto 字体族
                "Ubuntu Mono",         # Ubuntu 默认
                "Fira Code",           # 流行的编程字体
                "Source Code Pro",     # Adobe 出品
                "Hack",               # 编程字体
                "Droid Sans Mono",    # Android/早期 Linux
                "Consolas",           # 如果安装了 Windows 字体
                "Courier New",        # 最后的后备
            ]
            self.term_font = QFont(linux_mono_fonts[0], 12)
            self.term_font.setStyleHint(QFont.StyleHint.Monospace)
            self.term_font.setFixedPitch(True)
            self.term_font.setFamilies(linux_mono_fonts)

        # 验证字体是否真正等宽（数字和字母宽度一致）
        fmf = QFontMetricsF(self.term_font)
        w_advance = fmf.horizontalAdvance('W')
        digit_advance = fmf.horizontalAdvance('0')
        if abs(w_advance - digit_advance) > 1.0:
            # 字体不是真正等宽，强制使用 monospace 通用族
            self.term_font = QFont("monospace", 12)
            self.term_font.setStyleHint(QFont.StyleHint.Monospace)
            self.term_font.setFixedPitch(True)

        # 缓存 QFontMetrics（避免 paintEvent 中重复创建）
        self._font_metrics = QFontMetrics(self.term_font)

        # 计算字符尺寸（初始值，showEvent时会重新计算）
        self._calculate_char_size()

        # UTF-8 增量解码器 - 正确处理跨数据块的多字节字符
        self._utf8_decoder = codecs.getincrementaldecoder('utf-8')('replace')

        # 光标是否由应用自己管理（TUI模式）
        self.app_cursor_mode = False

        # 输入缓冲
        self.input_buffer = ""

        # 输入法预编辑文本（正在输入的拼音等）
        self._preedit_string = ""

        # 光标闪烁
        self.cursor_visible = True
        self.cursor_timer = QTimer()
        self.cursor_timer.timeout.connect(self._toggle_cursor)
        self.cursor_timer.start(530)  # 略微错开避免与其他定时器同步

        # 内容变化标志（脏标记）
        self._content_dirty = False

        # resize 后等待子进程重绘的标志
        self._awaiting_resize_redraw = False
        self._resize_data_timer = None

        # 双缓冲缓存 - 避免每次paintEvent都重绘
        self._cache_pixmap: Optional[QPixmap] = None
        self._cache_valid = False

        # 显示信息缓存 - 避免 paintEvent 中重复计算
        self._display_info = {
            'history_count': 0,
            'total_lines': 0,
            'display_start': 0,
            'valid': False
        }

        # 刷新定时器 - 只在有变化时重绘，降低频率
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self._conditional_update)
        self.refresh_timer.start(50)  # 20fps 足够流畅

        # 输出记录缓冲区 - 批量发送以减少信号开销
        self._output_buffer = []
        self._output_buffer_timer = QTimer()
        self._output_buffer_timer.timeout.connect(self._flush_output_buffer)
        self._output_buffer_timer.start(100)  # 每100ms刷新一次

        # Resize 防抖定时器 - 避免拖拽时频繁重绘导致闪烁
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._do_resize_update)
        self._resize_pending = False  # 是否有待处理的 resize
        self._last_resize_size = None  # 记录最后的窗口大小

        # 设置
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(400, 300)
        # 防止 sizeHint 与 term_cols/term_rows 形成反馈循环导致 resize 震荡
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # 启用输入法支持（中文等）
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        # 声明本 widget 完全不透明，避免 Qt 在 paintEvent 前绘制父/兄弟 widget 的内容
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        # 背景色和前景色 - One Dark Pro 风格
        self.bg_color = QColor("#282c34")
        self.fg_color = QColor("#abb2bf")

        # 滚动支持
        self.scroll_offset = 0  # 向上滚动的行数（0表示在底部）
        self._rendered_display_start = 0  # 上次实际渲染时使用的 display_start

        # 图片路径前缀设置（用于 Gemini 等需要 @ 前缀的工具）
        self.image_prefix_enabled = False

        # 图片保存位置设置（True=工作目录，False=系统临时目录）
        self.image_save_local = True

        # API 服务器输出监听（只有启用时才发射 raw_output_received 信号）
        self._api_output_enabled = False

        # 字符宽度校准标志
        self._needs_calibration = True

        # 双击 Ctrl+C 检测（用于强制退出 TUI 应用）
        self._last_ctrl_c_time = 0
        self._ctrl_c_interval = 0.5  # 500ms 内按两次视为双击

        # 颜色缓存（避免重复创建 QColor 对象）
        self._color_cache = {}
        # 宽字符缓存（避免重复调用 unicodedata.east_asian_width）
        self._wide_char_cache = {}
        # 可见性调整后的颜色缓存
        self._visible_color_cache = {}

        # 实例级别的终端颜色（支持主题切换）
        self._current_colors = self.DEFAULT_COLORS.copy()
        self._current_bright_colors = self.BRIGHT_COLORS.copy()

        # 鼠标选择相关（使用绝对行号，支持跨页选择）
        self._selection_start = None  # (absolute_row, col) 选择起点
        self._selection_end = None    # (absolute_row, col) 选择终点
        self._is_selecting = False    # 是否正在选择
        self._select_all_mode = False  # 是否为全选模式（包括历史记录）
        self._selection_color = QColor(100, 149, 237, 100)  # 选区高亮颜色（半透明蓝色）
        self._cursor_color = QColor(200, 200, 200, 180)  # 光标颜色

        # 设置鼠标光标为文本选择样式
        self.setCursor(Qt.CursorShape.IBeamCursor)

        # 搜索相关
        self._search_bar = None
        self._search_matches = []  # [(row, col, length), ...]
        self._current_match_index = -1
        self._search_highlight_color = QColor(255, 255, 0, 150)  # 黄色高亮
        self._search_current_color = QColor(255, 165, 0, 180)  # 当前匹配项橙色

        # 预编辑文本颜色缓存（避免 paintEvent 中重复创建）
        self._preedit_bg_color = QColor(60, 70, 90, 220)
        self._preedit_fg_color = QColor(255, 255, 100)
        self._preedit_underline_pen = QPen(QColor(255, 255, 100), 2)

        # 双击/三击选择
        self._click_count = 0
        self._last_click_time = 0
        self._last_click_pos = None
        self._double_click_interval = 0.4  # 400ms

        # 自动滚动（拖动选择时）
        self._auto_scroll_timer = QTimer()
        self._auto_scroll_timer.timeout.connect(self._auto_scroll_tick)
        self._auto_scroll_direction = 0  # -1: 向上, 0: 不滚动, 1: 向下
        self._last_mouse_pos = None  # 记录最后的鼠标位置

        # URL检测正则
        self._url_pattern = re.compile(
            r'https?://[^\s<>"{}|\\^`\[\]]+|'
            r'www\.[^\s<>"{}|\\^`\[\]]+'
        )

        # 启用拖拽
        self.setAcceptDrops(True)

        # 鼠标模式跟踪（TUI 程序如 Gemini 会启用鼠标模式）
        self._mouse_mode = False  # 是否启用了鼠标报告模式

        # 快速命令提供者回调（由主窗口设置，返回预设列表）
        self.quick_commands_provider = None

        # 本地快速命令提供者回调（由主窗口设置，返回本地预设列表）
        self.local_quick_commands_provider = None



    def _calculate_char_size(self):
        """计算字符尺寸"""
        # 使用 QFontMetricsF 获取浮点精度的度量
        fmf = QFontMetricsF(self.term_font)

        # 使用 horizontalAdvance，这是字符实际占用的水平空间
        self.char_width = fmf.horizontalAdvance('W')
        self.char_height = fmf.height()
        self.char_ascent = fmf.ascent()

        # 确保 char_width 至少为 1，防止除零错误
        self.char_width = max(1.0, self.char_width)
        self.char_height = max(1.0, self.char_height)

    def showEvent(self, event):
        """窗口显示时重新计算字符尺寸"""
        super().showEvent(event)
        # 窗口显示后重新计算，这时DPI等信息更准确
        self._calculate_char_size()
        self._update_terminal_size()

    def sizeHint(self):
        """返回固定的默认大小建议，不依赖当前 term_cols/term_rows。
        避免 sizeHint ↔ term_cols 反馈循环导致的 resize 震荡。
        实际大小由父布局（QSplitter/QTabWidget）决定。
        """
        from PyQt6.QtCore import QSize
        return QSize(800, 600)

    def _toggle_cursor(self):
        """切换光标可见性 - 只更新光标区域，不重建整个缓存"""
        self.cursor_visible = not self.cursor_visible
        # 光标单独绘制，不需要使缓存失效，只触发重绘
        self.update()

    def _conditional_update(self):
        """条件刷新 - 只在内容变化时重绘"""
        if self._content_dirty:
            self._content_dirty = False
            self._cache_valid = False  # 内容变化时使缓存失效
            self._display_info['valid'] = False  # 显示信息也需要更新
            self.update()

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

    def _update_terminal_size(self):
        """根据窗口大小更新终端尺寸"""
        if self.width() <= 0 or self.height() <= 0:
            return

        # 在高 DPI 屏幕上，__init__ 中通过 QFontMetricsF(font) 计算的 char_width
        # 可能使用默认 96 DPI，与实际渲染 DPI 不符（如 200% 缩放时差 2 倍）。
        # 每次 resize 都通过临时 QPixmap + QPainter 验证真实字符宽度。
        from PyQt6.QtGui import QPixmap, QPainter
        _pix = QPixmap(1, 1)
        _painter = QPainter(_pix)
        _painter.setFont(self.term_font)
        _fm = _painter.fontMetrics()
        real_cw = float(_fm.horizontalAdvance('W'))
        real_ch = float(_fm.height())
        real_ascent = float(_fm.ascent())
        _painter.end()
        if real_cw > 1 and abs(real_cw - self.char_width) > 0.5:
            print(f"[Terminal] DPI fix: char_width {self.char_width:.1f} -> {real_cw:.1f}")
            self.char_width = real_cw
            self.char_height = real_ch
            self.char_ascent = real_ascent

        p2 = self.PADDING * 2  # 左右/上下边距总和
        # 计算可用宽度
        available_width = self.width() - p2
        # 允许更小的列数以支持分屏（最小 20 列）
        new_cols = max(20, int(available_width / self.char_width))
        # 使用实际窗口计算的行数
        available_height = self.height() - p2
        # 允许更小的行数以支持分屏（最小 5 行）
        new_rows = max(5, int(available_height / self.char_height))

        if new_cols != self.term_cols or new_rows != self.term_rows:
            old_cols, old_rows = self.term_cols, self.term_rows
            self.term_cols = new_cols
            self.term_rows = new_rows

            # 重新创建screen（HistoryScreen的resize参数顺序是lines, columns）
            self.screen.resize(self.term_rows, self.term_cols)

            # resize 后 pyte 会按新尺寸重排旧内容，而像 Claude Code (Ink) 这样的 TUI
            # 收到 SIGWINCH 后会发送完整重绘，使用 CUF (\x1b[nC]) 创建间距。
            # CUF 不会清除经过的单元格——如果这些单元格包含 resize 重排的旧内容，
            # 渲染时就会看到"重影"（旧文字透过空隙显示）。
            # 解决方案：resize 后清空可见屏幕，保留光标位置。
            # 子进程的完整重绘会在 SIGWINCH 后到达，覆盖所有内容。
            saved_cursor = (self.screen.cursor.x, self.screen.cursor.y)
            self.screen.erase_in_display(2)
            self.screen.cursor.x, self.screen.cursor.y = saved_cursor

            # 更新PTY大小
            self._update_pty_size()

            # resize 后 pyte 会按新尺寸重排旧内容，但子进程（如 Claude Code 的 Ink TUI）
            # 会在收到 SIGWINCH 后发送完整重绘。在重绘数据到达前，pyte 的 buffer 处于
            # "中间态"（旧内容被错误折行），直接渲染会导致显示混乱。
            # 因此：设置标志位，让 paintEvent 暂时复用旧缓存，等新数据到达后再重建。
            self._awaiting_resize_redraw = True
            # 安全超时：如果 500ms 内没有收到新数据（无子进程运行等），强制解除等待
            QTimer.singleShot(500, self._clear_resize_wait)

            self._cache_valid = False
            self.update()
            print(f"[Terminal] Size: {old_cols}x{old_rows} -> {new_cols}x{new_rows} (widget: {self.width()}x{self.height()}, char_w: {self.char_width:.1f})")

    def _clear_resize_wait(self):
        """超时后强制解除 resize 重绘等待"""
        if self._awaiting_resize_redraw:
            self._awaiting_resize_redraw = False
            self._cache_valid = False
            self.update()

    def _finish_resize_redraw(self):
        """子进程重绘数据到齐后，解除 resize 等待并刷新显示"""
        self._awaiting_resize_redraw = False
        self._cache_valid = False
        self._display_info['valid'] = False
        self.update()

    def _update_pty_size(self):
        """更新PTY终端大小"""
        if self._backend is None:
            return

        try:
            self._backend.resize(self.term_cols, self.term_rows)
        except Exception as e:
            print(f"[Terminal] Error updating PTY size: {e}")

    def _write_to_backend(self, data: bytes) -> bool:
        """写入数据到终端后端"""
        if self._backend is None:
            return False
        try:
            return self._backend.write(data)
        except Exception:
            return False

    def resizeEvent(self, event: QResizeEvent):
        """窗口大小变化 - 使用防抖机制避免频繁重绘导致闪烁"""
        super().resizeEvent(event)

        # 重新定位搜索栏（这个可以立即执行，很轻量）
        if hasattr(self, '_search_bar') and self._search_bar:
            self._position_search_bar()

        # 记录当前大小
        self._last_resize_size = self.size()
        self._resize_pending = True

        # 使用防抖：重启定时器，等待用户停止拖拽后再更新
        # 150ms 是个平衡点：足够短以保持响应性，足够长以避免频繁重绘
        self._resize_timer.start(150)

        # 立即触发一次轻量级重绘（只是缩放旧缓存，不重建）
        self.update()

    def _do_resize_update(self):
        """防抖后真正执行 resize 更新"""
        if not self._resize_pending:
            return

        self._resize_pending = False
        self._update_terminal_size()

    def flush_resize(self):
        """立即处理待定的 resize，跳过防抖定时器"""
        self._resize_timer.stop()
        if self._resize_pending:
            self._resize_pending = False
            self._update_terminal_size()

    def set_working_dir(self, path: str):
        """设置工作目录（用于自动启动终端时）"""
        self._working_dir = path

    def start_process(self, command: List[str], cwd: Optional[str] = None):
        """启动子进程

        Args:
            command: 要执行的命令
            cwd: 工作目录，如果为None则使用当前进程的工作目录
        """
        if self._backend is not None and self._backend.is_running:
            return

        # 更新终端大小
        self._update_terminal_size()

        # 创建后端
        self._backend = create_backend()

        # 断开旧信号桥的连接（防止信号泄漏）
        if hasattr(self, '_signal_bridge') and self._signal_bridge:
            try:
                self._signal_bridge.output_received.disconnect()
                self._signal_bridge.process_finished.disconnect()
            except (TypeError, RuntimeError):
                pass  # 信号可能已断开

        # 创建信号桥（用于线程安全的 Qt 信号发射）
        self._signal_bridge = TerminalSignalBridge()
        self._signal_bridge.output_received.connect(self._on_output)
        self._signal_bridge.process_finished.connect(self._on_process_finished)

        # 设置后端回调
        def on_output(data: bytes):
            self._signal_bridge.output_received.emit(data)

        def on_exit(status: int):
            self._signal_bridge.process_finished.emit(status)

        self._backend.on_output = on_output
        self._backend.on_exit = on_exit

        # 启动进程
        if self._backend.start(command, cwd, self.term_cols, self.term_rows):
            # 延迟再次更新终端大小（确保窗口布局完成）
            QTimer.singleShot(100, self._update_terminal_size)
            QTimer.singleShot(500, self._update_terminal_size)
        else:
            print("[Terminal] Failed to start process")
            self._backend = None
            self._signal_bridge = None

    def _on_output(self, data: bytes):
        """处理输出数据"""
        try:
            text = self._utf8_decoder.decode(data)

            # 发送原始输出信号（只有启用 API 服务器时才发射）
            if self._api_output_enabled:
                self.raw_output_received.emit(text)

            # 诊断捕获：记录过滤前的原始文本
            if self._debug_capture_enabled and self._debug_capture_file:
                from datetime import datetime
                ts = datetime.now().strftime('%H:%M:%S.%f')[:12]
                self._debug_capture_file.write(f"\n{'='*60}\n")
                self._debug_capture_file.write(f"[{ts}] RAW ({len(text)} chars):\n")
                self._debug_capture_file.write(repr(text) + '\n')

            # 响应光标位置查询 (DSR - Device Status Report)
            # 当应用发送 \x1b[6n 时，终端应回复 \x1b[{row};{col}R
            if '\x1b[6n' in text:
                row = self.screen.cursor.y + 1  # 1-based
                col = self.screen.cursor.x + 1  # 1-based
                response = f'\x1b[{row};{col}R'
                self._write_to_backend(response.encode())
                # 移除查询序列，不需要显示
                text = text.replace('\x1b[6n', '')

            # 响应设备属性查询 (DA - Device Attributes)
            # \x1b[c 或 \x1b[0c 查询终端类型
            if '\x1b[c' in text or '\x1b[0c' in text:
                # 回复为 VT220 兼容终端
                response = '\x1b[?62;c'
                self._write_to_backend(response.encode())
                text = self._RE_DA_QUERY.sub('', text)

            # 响应 Secondary DA 查询 (\x1b[>c 或 \x1b[>0c)
            # Claude Code / Ink 用此检测终端类型和版本来决定渲染模式。
            # 不回复会导致超时→回退到精简布局（无 box-drawing 边框）。
            if re.search(r'\x1b\[>0?c', text):
                # 回复为 VT520 兼容 (65), 版本 100
                response = '\x1b[>65;100;0c'
                self._write_to_backend(response.encode())

            # 响应 XTVERSION 查询 (\x1b[>0q)
            if '\x1b[>0q' in text:
                response = '\x1bP>|SmartTerminal(1.0)\x1b\\'
                self._write_to_backend(response.encode())

            # 只过滤pyte完全不支持且会导致问题的序列（使用预编译正则）
            text = self._RE_SYNC_OUTPUT.sub('', text)      # Sync output (不支持)
            text = self._RE_KITTY_KEYBOARD.sub('', text)   # Kitty keyboard protocol
            text = self._RE_TERMINAL_QUERY.sub('', text)   # 终端查询响应（已回复，过滤掉不传给pyte）
            text = self._RE_FOCUS_REPORT.sub('', text)     # Focus reporting
            text = self._RE_CURSOR_STYLE.sub('', text)     # 光标样式

            # OSC序列（使用预编译正则）
            text = self._RE_OSC_HYPERLINK.sub('', text)     # OSC 8 超链接开始
            text = self._RE_OSC_HYPERLINK_END.sub('', text) # OSC 8 超链接结束
            text = self._RE_OSC_TITLE.sub('', text)         # OSC 0,1,2 标题
            text = self._RE_OSC_OTHER.sub('', text)         # 其他 OSC 序列 (7, 133 等)

            # pyte 不支持的字符串序列（内容会泄漏到显示缓冲区）
            text = self._RE_DCS.sub('', text)               # DCS 设备控制字符串
            text = self._RE_APC.sub('', text)               # APC 应用程序命令
            text = self._RE_PM.sub('', text)                # PM 隐私消息

            # 检测鼠标模式启用/禁用
            # 鼠标模式序列: \x1b[?1000h (启用), \x1b[?1000l (禁用)
            # 也有 1002, 1003, 1006 等变体
            if '\x1b[?1000h' in text or '\x1b[?1002h' in text or '\x1b[?1003h' in text or '\x1b[?1006h' in text:
                self._mouse_mode = True
            if '\x1b[?1000l' in text or '\x1b[?1002l' in text or '\x1b[?1003l' in text or '\x1b[?1006l' in text:
                self._mouse_mode = False

            # 过滤有问题的字符
            text = text.replace('\u23FA', '')  # ⏺ (录制符号)
            text = text.replace('\uFFFD', '')  # � (替换字符)

            # 诊断捕获：记录过滤后的文本
            if self._debug_capture_enabled and self._debug_capture_file:
                self._debug_capture_file.write(f"FILTERED ({len(text)} chars):\n")
                self._debug_capture_file.write(repr(text) + '\n')
                self._debug_capture_file.flush()

            # 记录 feed 前的历史行数，用于滚动位置稳定化
            old_total = self.screen._total_history_lines

            # 送入pyte处理（带错误恢复）
            try:
                self.stream.feed(text)
            except Exception as feed_err:
                # pyte 处理异常时尝试逐字符恢复，避免丢失整块数据
                print(f"[Terminal] stream.feed error: {feed_err}, attempting char-by-char recovery")
                if self._debug_capture_enabled and self._debug_capture_file:
                    self._debug_capture_file.write(f"FEED ERROR: {feed_err}\n")
                for ch in text:
                    try:
                        self.stream.feed(ch)
                    except Exception:
                        pass  # 跳过无法处理的单个字符

            # 滚动位置稳定化：当用户处于回滚浏览状态时，新输出不应导致显示内容跳动
            # 通过增加 scroll_offset 来补偿新增的历史行，保持 display_start 不变
            new_total = self.screen._total_history_lines
            lines_added = new_total - old_total
            if self.scroll_offset > 0 and lines_added > 0:
                self.scroll_offset += lines_added
                max_scroll = self._get_history_count()
                self.scroll_offset = min(self.scroll_offset, max_scroll)

            # 标记内容已变化，让定时器统一处理重绘（避免高频输出时的重绘风暴）
            self._content_dirty = True

            # 收到新数据，说明子进程已开始重绘。
            # 但子进程（如 Claude Code/Ink TUI）的完整重绘会分多个数据块到达，
            # 不能在第一个块就解除等待，否则会渲染不完整的中间态导致重影。
            # 改用短延时：每次收到数据时重置一个 80ms 计时器，
            # 让所有数据块到齐后再解除等待并重建缓存。
            if self._awaiting_resize_redraw:
                if hasattr(self, '_resize_data_timer') and self._resize_data_timer is not None:
                    self._resize_data_timer.stop()
                else:
                    self._resize_data_timer = QTimer()
                    self._resize_data_timer.setSingleShot(True)
                    self._resize_data_timer.timeout.connect(self._finish_resize_redraw)
                self._resize_data_timer.start(80)

            # 缓冲输出，由定时器统一发送（避免高频输出时的信号风暴）
            self._output_buffer.append(text)
        except Exception as e:
            import traceback
            print(f"Output error: {e}")
            traceback.print_exc()

    def toggle_debug_capture(self):
        """切换原始输出诊断捕获（用于排查内容过滤问题）"""
        if self._debug_capture_enabled:
            # 关闭捕获
            self._debug_capture_enabled = False
            if self._debug_capture_file:
                self._debug_capture_file.close()
                self._debug_capture_file = None
            print("[Terminal] Debug capture DISABLED")
        else:
            # 开启捕获
            capture_path = os.path.join(os.path.dirname(__file__), 'terminal_raw_capture.log')
            self._debug_capture_file = open(capture_path, 'w', encoding='utf-8')
            self._debug_capture_enabled = True
            print(f"[Terminal] Debug capture ENABLED → {capture_path}")

    def _on_process_finished(self, status: int):
        """进程结束"""
        self.session_ended.emit()
        # 关闭诊断捕获文件
        if self._debug_capture_file:
            self._debug_capture_file.close()
            self._debug_capture_file = None

    def _flush_output_buffer(self):
        """刷新输出缓冲区，批量发送 output_recorded 信号"""
        if self._output_buffer:
            # 合并缓冲区内容一次性发送
            combined = ''.join(self._output_buffer)
            self._output_buffer.clear()
            self.output_recorded.emit(combined)

    def _debug_dump_buffer(self):
        """调试：将 pyte 缓冲区内容导出到文件"""
        dump_path = os.path.join(os.path.dirname(__file__), 'pyte_buffer_dump.txt')
        with open(dump_path, 'w', encoding='utf-8') as f:
            f.write(f"=== PYTE BUFFER DUMP ===\n")
            f.write(f"term_cols={self.term_cols}, term_rows={self.term_rows}\n")
            f.write(f"screen.columns={self.screen.columns}, screen.lines={self.screen.lines}\n")
            f.write(f"scroll_offset={self.scroll_offset}\n")
            history_count, total_lines, start_line = self._get_display_info()
            f.write(f"history_count={history_count}, total_lines={total_lines}, start_line={start_line}\n\n")

            f.write("--- CURRENT SCREEN BUFFER ---\n")
            for row in range(self.screen.lines):
                chars = []
                for col in range(min(self.screen.columns, 200)):
                    try:
                        ch = self.screen.buffer[row][col]
                        t = ch.data if hasattr(ch, 'data') else str(ch)
                        chars.append(t if t else ' ')
                    except (KeyError, IndexError, TypeError):
                        chars.append(' ')
                line = ''.join(chars).rstrip()
                if line:
                    f.write(f"  row {row:2d}: |{line}|\n")
                else:
                    f.write(f"  row {row:2d}: |\n")

            f.write("\n--- HISTORY (last 10 lines) ---\n")
            history = self._get_history_top()
            if history:
                h_start = max(0, len(history) - 10)
                for i in range(h_start, len(history)):
                    chars = []
                    for col in range(min(self.screen.columns, 200)):
                        try:
                            ch = history[i][col]
                            t = ch.data if hasattr(ch, 'data') else str(ch)
                            chars.append(t if t else ' ')
                        except (KeyError, IndexError, TypeError):
                            chars.append(' ')
                    line = ''.join(chars).rstrip()
                    f.write(f"  hist {i:3d}: |{line}|\n")

        print(f"[Terminal] Buffer dumped to {dump_path}")

    def _calibrate_char_width(self, painter: QPainter):
        """通过实际渲染校准字符宽度"""
        # 使用 painter 的 fontMetrics 测量
        fm = painter.fontMetrics()
        painter_advance = fm.horizontalAdvance('W')

        # 如果 painter 的 metrics 与之前计算的不同，更新并重新计算终端大小
        if abs(painter_advance - self.char_width) > 1:
            old_cw = self.char_width
            self.char_width = float(painter_advance)
            self.char_height = float(fm.height())
            self.char_ascent = float(fm.ascent())
            print(f"[Terminal] Calibration: char_width {old_cw:.1f} -> {self.char_width:.1f}")
            # 立即重新计算终端大小（不设置为0，避免当前帧渲染空白）
            QTimer.singleShot(0, self._update_terminal_size)

    @property
    def _screen_history(self):
        """获取屏幕历史（避免重复 hasattr 检查）"""
        return getattr(self.screen, 'history', None)

    def _get_history_top(self):
        """获取历史记录顶部列表（避免重复 hasattr 检查）
        备用屏幕上不显示主屏幕的历史记录。
        """
        if self.screen._in_alt_screen:
            return []
        history = self._screen_history
        return history.top if history else []

    def _get_history_count(self) -> int:
        """获取历史记录行数（避免重复 hasattr 检查）
        备用屏幕上不显示主屏幕的历史记录。
        """
        if self.screen._in_alt_screen:
            return 0
        history = self._screen_history
        return len(history.top) if history else 0

    def _is_wide_char(self, char: str) -> bool:
        """判断字符是否为宽字符（中文等）- 带缓存"""
        if not char or len(char) == 0:
            return False
        # 检查缓存
        if char in self._wide_char_cache:
            return self._wide_char_cache[char]
        # 计算并缓存 - east_asian_width 只接受单个字符，取第一个字符判断
        result = unicodedata.east_asian_width(char[0]) in ('F', 'W')
        # 限制缓存大小（保留一半常用项而不是全部清空）
        if len(self._wide_char_cache) > 2000:
            # 使用 popitem() 高效删除，避免创建临时列表
            for _ in range(1000):
                self._wide_char_cache.popitem()
        self._wide_char_cache[char] = result
        return result

    @staticmethod
    def _need_boundary_space(last_char: str, first_char: str) -> bool:
        """判断应用层软换行拼接时是否需要在边界处插入空格"""
        last_wide = unicodedata.east_asian_width(last_char) in ('F', 'W')
        first_wide = unicodedata.east_asian_width(first_char) in ('F', 'W')
        if last_wide and first_wide:
            return False  # CJK + CJK: 不需要空格
        # Latin+CJK, CJK+Latin, Latin+Latin: 需要空格（词边界）
        return True

    def paintEvent(self, event: QPaintEvent):
        """绘制终端 - 使用双缓冲提高性能"""
        # 防止在 widget 尺寸为 0 时绘制（如拖拽分离 tab 过渡期间），避免 segfault
        if self.width() <= 0 or self.height() <= 0:
            return

        painter = QPainter(self)
        if not painter.isActive():
            return

        # resize 期间继续显示旧缓存内容（而非空白），避免频繁 resize 时内容"消失"。
        # 之前显示空白是为了防止重影，但 resize 震荡会导致内容长时间不可见。
        # 旧缓存可能尺寸不匹配，但仍比空白好——内容至少可读。
        if self._resize_pending or self._awaiting_resize_redraw:
            painter.fillRect(self.rect(), self.bg_color)
            if self._cache_pixmap and not self._cache_pixmap.isNull():
                painter.drawPixmap(0, 0, self._cache_pixmap)
            return

        # 检查是否需要重建缓存
        need_rebuild = (
            self._cache_pixmap is None or
            self._cache_pixmap.isNull() or
            not self._cache_valid or
            self._cache_pixmap.size() != self.size()
        )

        if need_rebuild:
            self._rebuild_cache()

        # 先填充背景确保完全不透明（防止其他 tab/widget 内容透出）
        painter.fillRect(self.rect(), self.bg_color)
        # 使用 Source 合成模式：完全替换像素，不做 alpha 混合
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        if self._cache_pixmap and not self._cache_pixmap.isNull():
            painter.drawPixmap(0, 0, self._cache_pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 选区高亮单独绘制（不在缓存中，避免拖动时重建缓存）
        if self._has_selection():
            if self._select_all_mode:
                # 全选模式：高亮所有可见行
                for sel_row in range(self.term_rows):
                    x = self.PADDING
                    y = int(self.PADDING + sel_row * self.char_height)
                    width = int(self.term_cols * self.char_width)
                    height = int(self.char_height)
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

                        x = int(self.PADDING + col_start * self.char_width)
                        y = int(self.PADDING + sel_row * self.char_height)
                        width = int((col_end - col_start + 1) * self.char_width)
                        height = int(self.char_height)

                        painter.fillRect(x, y, width, height, self._selection_color)

        # 搜索高亮单独绘制
        if self._search_matches:
            # 使用渲染时记录的 display_start，确保高亮与内容一致
            display_start = self._rendered_display_start

            for idx, (match_row, match_col, match_len) in enumerate(self._search_matches):
                display_row = match_row - display_start
                if 0 <= display_row < self.term_rows:
                    x = int(self.PADDING + match_col * self.char_width)
                    y = int(self.PADDING + display_row * self.char_height)
                    width = int(match_len * self.char_width)
                    height = int(self.char_height)

                    if idx == self._current_match_index:
                        painter.fillRect(x, y, width, height, self._search_current_color)
                    else:
                        painter.fillRect(x, y, width, height, self._search_highlight_color)

        # 光标单独绘制（因为会闪烁）
        if self.scroll_offset == 0 and self.cursor_visible and self.hasFocus() and not self.screen.cursor.hidden:
            cx = self.screen.cursor.x
            cy = self.screen.cursor.y

            if 0 <= cy < self.term_rows and 0 <= cx < self.term_cols:
                cursor_x = int(self.PADDING + cx * self.char_width)
                cursor_y = int(self.PADDING + cy * self.char_height)

                painter.fillRect(
                    cursor_x, cursor_y,
                    int(self.char_width), int(self.char_height),
                    self._cursor_color
                )

        # 绘制输入法预编辑文本（拼音字母）
        if self._preedit_string and self.hasFocus():
            cx = self.screen.cursor.x
            cy = self.screen.cursor.y

            if 0 <= cy < self.term_rows:
                preedit_x = int(self.PADDING + cx * self.char_width)
                preedit_y = int(self.PADDING + cy * self.char_height)

                # 设置预编辑文本样式
                painter.setFont(self.term_font)

                # 计算预编辑文本宽度（使用缓存的 QFontMetrics）
                preedit_width = self._font_metrics.horizontalAdvance(self._preedit_string)
                preedit_height = int(self.char_height)

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
        if self._cache_pixmap is None or self._cache_pixmap.isNull() or self._cache_pixmap.size() != self.size():
            self._cache_pixmap = QPixmap(self.size())
            if self._cache_pixmap.isNull():
                return

        self._cache_pixmap.fill(self.bg_color)

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

        # 获取历史记录的引用（不转换为列表以提高性能）
        history = self._get_history_top()

        # 计算结束行
        end_line = min(total_lines, start_line + self.term_rows)

        # 准备要显示的行 - 只获取需要的部分，避免复制整个历史记录
        display_lines = []
        for line_idx in range(start_line, end_line):
            if line_idx < history_count:
                # 从历史记录中获取（直接索引访问）
                display_lines.append(history[line_idx])
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
        char_width = self.char_width
        char_height = self.char_height
        int_char_height = int(char_height)
        bg_default = self.bg_color
        last_fg_color = None  # 缓存上一个前景色，减少 setPen 调用
        padding = self.PADDING  # 局部变量加速

        for display_row, buffer_line in enumerate(display_lines):
            row_y = int(padding + display_row * char_height)
            text_y = int(row_y + self.char_ascent)

            for col in range(num_cols):
                try:
                    char = buffer_line[col]
                except (KeyError, IndexError, TypeError):
                    continue

                # 优化：一次性获取所有属性，避免多次 getattr 调用
                # pyte 的 Char 是 namedtuple，可以直接访问属性
                if hasattr(char, 'data'):
                    # pyte Char 对象 - 直接访问属性更快
                    char_text = char.data
                    if not char_text:
                        continue
                    char_fg = char.fg
                    char_bg = char.bg
                    char_bold = char.bold
                    char_reverse = char.reverse
                elif isinstance(char, str):
                    char_text = char
                    if not char_text:
                        continue
                    char_fg = 'default'
                    char_bg = 'default'
                    char_bold = False
                    char_reverse = False
                else:
                    continue

                x = int(padding + col * char_width)

                # 获取颜色（使用已提取的属性）
                fg_color = self._get_char_color(char_fg, char_bold)
                bg_color = self._get_char_color(char_bg, False, is_bg=True)

                # 处理反转
                if char_reverse:
                    fg_color, bg_color = bg_color, fg_color

                # 确保前景色可见
                fg_color = self._ensure_visible(fg_color, bg_color)

                # 判断宽字符
                is_wide = self._is_wide_char(char_text)
                char_draw_width = int(char_width * 2) if is_wide else int(char_width)

                # 绘制背景
                if bg_color != bg_default:
                    painter.fillRect(x, row_y, char_draw_width, int_char_height, bg_color)

                # 绘制字符
                if char_text != ' ':
                    # 只在颜色变化时调用 setPen
                    if fg_color != last_fg_color:
                        painter.setPen(fg_color)
                        last_fg_color = fg_color
                    painter.drawText(x, text_y, char_text)

        # 注意：选区高亮和搜索高亮已移至 paintEvent 中单独绘制，
        # 这样拖动选择时不会触发缓存重建，大幅提升性能

        # 滚动指示器（显示在右上角）
        if self.scroll_offset > 0:
            painter.setPen(QColor("#667eea"))
            indicator_text = f"[History: +{self.scroll_offset} lines]"
            painter.drawText(self.width() - 180, 20, indicator_text)

        painter.end()
        self._cache_valid = True

    def _ensure_visible(self, fg: QColor, bg: QColor) -> QColor:
        """确保前景色在背景色上可见，支持深色和浅色背景 - 带缓存"""
        # 构建缓存键（使用 RGB 值作为键）
        cache_key = (fg.rgb(), bg.rgb())
        if cache_key in self._visible_color_cache:
            return self._visible_color_cache[cache_key]

        # 计算亮度 (perceived luminance)
        fg_lum = 0.299 * fg.red() + 0.587 * fg.green() + 0.114 * fg.blue()
        bg_lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()

        # 计算对比度差
        contrast = abs(fg_lum - bg_lum)
        min_contrast = 60  # 最小对比度要求

        if contrast < min_contrast:
            # 判断是深色背景还是浅色背景
            if bg_lum > 128:
                # 浅色背景 - 降低前景色亮度（使其更深）
                reduction = min_contrast - contrast
                r = max(0, fg.red() - int(reduction * 1.2))
                g = max(0, fg.green() - int(reduction * 1.2))
                b = max(0, fg.blue() - int(reduction * 1.2))
                result = QColor(r, g, b)
            else:
                # 深色背景 - 提升前景色亮度（使其更亮）
                boost = min_contrast - contrast
                r = min(255, fg.red() + int(boost * 1.2))
                g = min(255, fg.green() + int(boost * 1.2))
                b = min(255, fg.blue() + int(boost * 1.2))
                result = QColor(r, g, b)
        else:
            result = fg

        # 限制缓存大小（使用 popitem() 高效删除）
        if len(self._visible_color_cache) > 1000:
            for _ in range(500):
                self._visible_color_cache.popitem()
        self._visible_color_cache[cache_key] = result
        return result

    def _get_char_color(self, color, bold: bool = False, is_bg: bool = False) -> QColor:
        """获取字符颜色 - 支持各种格式，带缓存"""
        # 默认颜色 - 直接返回预设值
        if color == "default" or color is None:
            return self.bg_color if is_bg else self.fg_color

        # 构建缓存键
        if isinstance(color, (tuple, list)):
            cache_key = (tuple(color), bold, is_bg)
        else:
            cache_key = (color, bold, is_bg)

        # 检查缓存
        if cache_key in self._color_cache:
            return self._color_cache[cache_key]

        # 计算颜色
        result = self._compute_color(color, bold, is_bg)

        # 限制缓存大小（使用 popitem() 高效删除）
        if len(self._color_cache) > 1000:
            for _ in range(500):
                self._color_cache.popitem()

        self._color_cache[cache_key] = result
        return result

    def _compute_color(self, color, bold: bool, is_bg: bool) -> QColor:
        """计算颜色值（内部方法）"""
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

    def keyPressEvent(self, event: QKeyEvent):
        """处理键盘输入"""
        key = event.key()
        modifiers = event.modifiers()
        text = event.text()

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

        # Ctrl+Plus/Minus/= 字体缩放 — 委托给主窗口全局缩放
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                main_win = self.window()
                if hasattr(main_win, '_global_zoom_in'):
                    main_win._global_zoom_in()
                else:
                    self._zoom_in()
                event.accept()
                return
            if key == Qt.Key.Key_Minus:
                main_win = self.window()
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
                self._content_dirty = True
                self.update()
                event.accept()
                return
            elif key == Qt.Key.Key_PageDown:
                self.scroll_offset = max(self.scroll_offset - self.term_rows, 0)
                self._content_dirty = True
                self.update()
                event.accept()
                return
            elif key == Qt.Key.Key_Home:
                self.scroll_offset = history_lines
                self._content_dirty = True
                self.update()
                event.accept()
                return
            elif key == Qt.Key.Key_End:
                self.scroll_offset = 0
                self._content_dirty = True
                self.update()
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

        # 输入时自动滚动到底部
        if self.scroll_offset > 0:
            self.scroll_offset = 0

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
            data = b'\x1b[A'
        elif key == Qt.Key.Key_Down:
            data = b'\x1b[B'
        elif key == Qt.Key.Key_Right:
            data = b'\x1b[C'
        elif key == Qt.Key.Key_Left:
            data = b'\x1b[D'
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
            except OSError:
                pass
        event.accept()

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
                if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                    event.accept()  # 接受事件，阻止 Qt 将其作为快捷键处理
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
                y = int(self.PADDING + cy * self.char_height)
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

    def wheelEvent(self, event):
        """鼠标滚轮事件 - 滚动历史"""
        delta = event.angleDelta().y()

        # 计算可滚动的最大行数
        # 历史记录 + 当前屏幕缓冲区的总行数
        history_lines = self._get_history_count()
        # 最大scroll_offset应该是历史记录的行数（这样可以滚动到最顶部）
        max_scroll = history_lines

        old_offset = self.scroll_offset
        if delta > 0:
            # 向上滚动（查看历史）- 增加scroll_offset
            self.scroll_offset = min(self.scroll_offset + 3, max_scroll)
        else:
            # 向下滚动（回到最新）- 减少scroll_offset
            self.scroll_offset = max(self.scroll_offset - 3, 0)

        # 只有在滚动位置实际改变时才更新
        if old_offset != self.scroll_offset:
            self._cache_valid = False  # 直接使缓存失效，避免等待定时器
            self.update()
        event.accept()

    def scroll_to_bottom(self):
        """滚动到底部（最新内容）"""
        if self.scroll_offset != 0:
            self.scroll_offset = 0
            self._cache_valid = False  # 直接使缓存失效
            self.update()

    def _auto_scroll_tick(self):
        """自动滚动定时器回调 - 支持拖动选择时跨页"""
        if not self._is_selecting or self._auto_scroll_direction == 0:
            self._auto_scroll_timer.stop()
            return

        # 获取历史记录行数
        history_lines = self._get_history_count()
        max_scroll = history_lines

        if self._auto_scroll_direction < 0:
            # 向上滚动（查看历史）
            if self.scroll_offset < max_scroll:
                self.scroll_offset = min(self.scroll_offset + 2, max_scroll)
                self._cache_valid = False  # 直接使缓存失效
                # 更新选择终点（扩展到新滚动位置的顶部）
                if self._last_mouse_pos:
                    self._selection_end = self._pos_to_absolute_cell(self._last_mouse_pos)
                self.update()
        else:
            # 向下滚动（回到最新）
            if self.scroll_offset > 0:
                self.scroll_offset = max(self.scroll_offset - 2, 0)
                self._cache_valid = False  # 直接使缓存失效
                # 更新选择终点（扩展到新滚动位置的底部）
                if self._last_mouse_pos:
                    self._selection_end = self._pos_to_absolute_cell(self._last_mouse_pos)
                self.update()

    def _pos_to_cell(self, pos: QPoint) -> tuple:
        """将鼠标位置转换为终端单元格坐标 (row, col) - 返回显示区域内的相对行号"""
        x = pos.x() - self.PADDING
        y = pos.y() - self.PADDING

        col = max(0, min(int(x / self.char_width), self.term_cols - 1))
        row = max(0, min(int(y / self.char_height), self.term_rows - 1))

        return (row, col)

    def _pos_to_absolute_cell(self, pos: QPoint) -> tuple:
        """将鼠标位置转换为绝对行号坐标 (absolute_row, col) - 用于跨页选择

        使用上次渲染时记录的 display_start，确保鼠标坐标与屏幕显示内容一致。
        避免因新输出导致 history_count 变化而产生偏移。
        """
        x = pos.x() - self.PADDING
        y = pos.y() - self.PADDING

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

    def mousePressEvent(self, event: QMouseEvent):
        """鼠标按下 - 开始选择，支持双击选词、三击选行、Cmd+点击URL

        重要：按住 Shift 键可以强制使用本地选择模式（绕过程序的鼠标捕获）
        选择坐标使用绝对行号，支持跨页选择。
        """
        import time
        self.setFocus(Qt.FocusReason.MouseFocusReason)

        # Shift 键或回滚历史时强制使用本地选择模式（绕过鼠标模式）
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        # 如果鼠标模式启用且没有强制本地选择，将事件发送给程序
        if self._mouse_mode and not force_local_selection:
            self._send_mouse_event(event, 'press')
            return

        if event.button() == Qt.MouseButton.LeftButton:
            current_time = time.time()
            cell = self._pos_to_cell(event.pos())  # 相对坐标，用于点击检测
            abs_cell = self._pos_to_absolute_cell(event.pos())  # 绝对坐标，用于选择

            # 检测多击
            if (self._last_click_pos and
                self._last_click_pos == cell and
                current_time - self._last_click_time < self._double_click_interval):
                self._click_count += 1
            else:
                self._click_count = 1

            self._last_click_time = current_time
            self._last_click_pos = cell

            # Cmd+点击检测URL并打开
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                url = self._get_url_at_pos(cell)
                if url:
                    self._open_url(url)
                    return

            if self._click_count == 2:
                # 双击选词
                self._select_word_at(abs_cell)
            elif self._click_count >= 3:
                # 三击选行
                self._select_line_at(abs_cell)
                self._click_count = 0  # 重置
            else:
                # 单击开始选择 - 使用绝对坐标
                self._selection_start = abs_cell
                self._selection_end = abs_cell
                self._is_selecting = True
                self._select_all_mode = False  # 清除全选模式

            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        """鼠标移动 - 更新选择区域（使用绝对坐标），支持拖动时自动滚动"""
        # Shift 键或回滚历史时强制使用本地选择模式
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        # 如果鼠标模式启用且没有强制本地选择，且正在拖动（按住按钮）
        if self._mouse_mode and not force_local_selection and event.buttons():
            self._send_mouse_event(event, 'move')
            return

        if self._is_selecting:
            self._last_mouse_pos = event.pos()
            self._selection_end = self._pos_to_absolute_cell(event.pos())

            # 检测是否需要自动滚动（鼠标在边缘区域）
            y = event.pos().y()
            edge_zone = 20  # 边缘区域大小（像素）
            widget_height = self.height()

            if y < edge_zone:
                # 鼠标在顶部边缘 - 向上滚动（查看历史）
                self._auto_scroll_direction = -1
                if not self._auto_scroll_timer.isActive():
                    self._auto_scroll_timer.start(50)  # 50ms 间隔
            elif y > widget_height - edge_zone:
                # 鼠标在底部边缘 - 向下滚动（回到最新）
                self._auto_scroll_direction = 1
                if not self._auto_scroll_timer.isActive():
                    self._auto_scroll_timer.start(50)
            else:
                # 不在边缘，停止自动滚动
                self._auto_scroll_direction = 0
                self._auto_scroll_timer.stop()

            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        """鼠标释放 - 完成选择或移动光标"""
        # 停止自动滚动
        self._auto_scroll_timer.stop()
        self._auto_scroll_direction = 0

        # Shift 键或回滚历史时强制使用本地选择模式
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        # 如果鼠标模式启用且没有强制本地选择
        if self._mouse_mode and not force_local_selection:
            self._send_mouse_event(event, 'release')
            return

        if event.button() == Qt.MouseButton.LeftButton and self._is_selecting:
            self._selection_end = self._pos_to_absolute_cell(event.pos())
            self._is_selecting = False

            # 检查是否是单击（没有拖动选择）
            if self._selection_start == self._selection_end:
                # 单击 - 尝试移动光标到点击位置（使用相对坐标）
                display_cell = self._pos_to_cell(event.pos())
                self._move_cursor_to_click(display_cell)
                # 清除选择状态
                self._selection_start = None
                self._selection_end = None

            self.update()
        super().mouseReleaseEvent(event)

    def _move_cursor_to_click(self, click_pos: tuple):
        """通过发送方向键移动光标到点击位置

        仅当点击在光标所在行时生效
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

        # 计算需要移动的距离
        diff = click_col - cursor_col

        if diff == 0:
            return

        # 发送方向键
        if diff > 0:
            # 向右移动
            for _ in range(diff):
                self._write_to_backend(b'\x1b[C')  # Right arrow
        else:
            # 向左移动
            for _ in range(-diff):
                self._write_to_backend(b'\x1b[D')  # Left arrow

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
        """)

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

        menu.addSeparator()

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

        # 分屏
        split_action = QAction(t("toolbar.split"), self)
        split_action.triggered.connect(self.split_horizontal_requested.emit)
        menu.addAction(split_action)

        # 关闭当前分屏
        close_split_action = QAction(t("ctx.close_split"), self)
        close_split_action.triggered.connect(self.close_split_requested.emit)
        menu.addAction(close_split_action)

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
        """获取选中的文本（使用绝对行号，支持跨页选择）"""
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

        selected_lines = []

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
            # 检测换行类型：0=硬换行, 1=终端软换行(pyte), 2=应用层软换行(启发式)
            wrap_type = 0
            if abs_row < end_row:
                if self.screen.is_soft_wrapped(buffer_line):
                    wrap_type = 1
                else:
                    # 启发式：检查行内容是否填满到行尾附近
                    # （捕捉应用层文字排版换行，如 Claude Code 的 markdown 渲染器）
                    # 阈值：内容占用超过约 85% 的行宽，或距行尾 ≤5 列
                    is_full_line = (col_start == 0 and col_end == term_cols - 1)
                    if is_full_line:
                        wrap_threshold = max(term_cols - 5, int(term_cols * 0.85))
                        for rev_col, rev_char in reversed(chars):
                            if rev_char != ' ':
                                effective_end = rev_col + (1 if self._is_wide_char(rev_char) else 0)
                                if effective_end >= wrap_threshold:
                                    wrap_type = 2
                                break

            if wrap_type == 1:
                # 终端软换行：保留所有字符（包括尾部空格，它们是真实内容）
                line_text = ''.join(selected_chars)
            else:
                line_text = ''.join(selected_chars).rstrip()

            selected_lines.append((line_text, wrap_type))

        # 组合结果
        result = []
        for i, (line_text, wrap_type) in enumerate(selected_lines):
            if i > 0:
                prev_text, prev_wrap = selected_lines[i - 1]
                if prev_wrap == 1:
                    # 终端软换行：直接拼接（无需处理缩进）
                    pass
                elif prev_wrap == 2:
                    # 应用层软换行：去除续行的相同缩进
                    prev_indent = len(prev_text) - len(prev_text.lstrip())
                    if prev_indent > 0:
                        curr_indent = len(line_text) - len(line_text.lstrip())
                        if curr_indent >= prev_indent:
                            line_text = line_text[prev_indent:]
                    # 恢复词边界的空格（rstrip 可能移除了换行点的空格）
                    if result and line_text:
                        last_ch = result[-1][-1] if result[-1] else ''
                        first_ch = line_text[0]
                        if last_ch and first_ch and self._need_boundary_space(last_ch, first_ch):
                            result.append(' ')
                else:
                    result.append('\n')
            result.append(line_text)

        return ''.join(result)

    def _copy_selection_to_clipboard(self):
        """复制选中内容到剪贴板"""
        if self._has_selection():
            text = self._get_selected_text()
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
        """获取所有内容（历史记录 + 当前屏幕）- 优化版本"""
        columns = self.screen.columns
        is_wide = self._is_wide_char
        screen = self.screen

        def extract_line(buffer_line):
            """提取行内容，返回 (text, wrap_type)
            wrap_type: 0=硬换行, 1=终端软换行, 2=应用层软换行
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

            # 确定换行类型
            wrap_type = 0
            if screen.is_soft_wrapped(buffer_line):
                wrap_type = 1
            else:
                # 启发式：检查行是否填满到行尾附近（应用层换行）
                wrap_threshold = max(columns - 5, int(columns * 0.85))
                for rev_col, rev_char in reversed(chars_with_col):
                    if rev_char != ' ':
                        effective_end = rev_col + (1 if is_wide(rev_char) else 0)
                        if effective_end >= wrap_threshold:
                            wrap_type = 2
                        break

            if wrap_type == 1:
                text = ''.join(char_list)  # 终端软换行：保留尾部空格
            else:
                text = ''.join(char_list).rstrip()

            return text, wrap_type

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

        # 组合结果
        need_space = self._need_boundary_space
        result = []
        for i, (text, wrap_type) in enumerate(line_data):
            if i > 0:
                prev_text, prev_wrap = line_data[i - 1]
                if prev_wrap == 1:
                    pass  # 终端软换行：直接拼接
                elif prev_wrap == 2:
                    # 应用层软换行：去除续行的相同缩进
                    prev_indent = len(prev_text) - len(prev_text.lstrip())
                    if prev_indent > 0:
                        curr_indent = len(text) - len(text.lstrip())
                        if curr_indent >= prev_indent:
                            text = text[prev_indent:]
                    # 恢复词边界空格
                    if result and text:
                        last_ch = result[-1][-1] if result[-1] else ''
                        first_ch = text[0]
                        if last_ch and first_ch and need_space(last_ch, first_ch):
                            result.append(' ')
                else:
                    result.append('\n')
            result.append(text)

        return ''.join(result)

    def _clear_selection(self):
        """清除选择"""
        self._selection_start = None
        self._selection_end = None
        self._select_all_mode = False
        self.update()

    def stop_process(self):
        """停止进程并清理资源"""
        # 先断开信号连接，防止在清理过程中触发回调
        if self._signal_bridge:
            try:
                self._signal_bridge.output_received.disconnect()
                self._signal_bridge.process_finished.disconnect()
            except (TypeError, RuntimeError):
                pass  # 信号可能已断开
            self._signal_bridge.deleteLater()
            self._signal_bridge = None

        # 清理后端回调引用（打破循环引用）
        if self._backend:
            self._backend.on_output = None
            self._backend.on_exit = None
            self._backend.stop()
            self._backend = None

    def cleanup(self):
        """完整清理所有资源（在销毁前调用）"""
        # 停止所有定时器
        if hasattr(self, 'cursor_timer') and self.cursor_timer:
            self.cursor_timer.stop()
            self.cursor_timer.deleteLater()
            self.cursor_timer = None

        if hasattr(self, 'refresh_timer') and self.refresh_timer:
            self.refresh_timer.stop()
            self.refresh_timer.deleteLater()
            self.refresh_timer = None

        if hasattr(self, '_auto_scroll_timer') and self._auto_scroll_timer:
            self._auto_scroll_timer.stop()
            self._auto_scroll_timer.deleteLater()
            self._auto_scroll_timer = None

        if hasattr(self, '_output_buffer_timer') and self._output_buffer_timer:
            self._output_buffer_timer.stop()
            self._output_buffer_timer.deleteLater()
            self._output_buffer_timer = None

        # 刷新并清空输出缓冲区
        if hasattr(self, '_output_buffer') and self._output_buffer:
            self._flush_output_buffer()

        # 停止进程
        self.stop_process()

        # 清理搜索栏
        if hasattr(self, '_search_bar') and self._search_bar:
            self._search_bar.deleteLater()
            self._search_bar = None

        # 清空缓存
        if hasattr(self, '_color_cache'):
            self._color_cache.clear()
        if hasattr(self, '_wide_char_cache'):
            self._wide_char_cache.clear()
        if hasattr(self, '_visible_color_cache'):
            self._visible_color_cache.clear()
        if hasattr(self, '_search_matches'):
            self._search_matches.clear()

    def closeEvent(self, event):
        """窗口关闭事件 - 清理资源"""
        self.cleanup()
        super().closeEvent(event)

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
            print(f"Send text error: {e}")

    def clear_screen(self):
        """清屏"""
        self.screen.reset()
        self.update()

    def is_running(self) -> bool:
        """检查是否有进程在运行"""
        return self._backend is not None and self._backend.is_running

    def pause_timers(self):
        """暂停/降速定时器（用于窗口失活时减少开销）"""
        if hasattr(self, 'cursor_timer') and self.cursor_timer:
            self.cursor_timer.stop()
        # 降低 refresh_timer 频率到 200ms（5fps），保持后台更新但减少开销
        if hasattr(self, 'refresh_timer') and self.refresh_timer:
            self.refresh_timer.setInterval(200)

    def resume_timers(self):
        """恢复所有定时器（用于窗口激活时）"""
        if hasattr(self, 'cursor_timer') and self.cursor_timer:
            self.cursor_timer.start(530)
        # 恢复 refresh_timer 到 50ms（20fps）
        if hasattr(self, 'refresh_timer') and self.refresh_timer:
            self.refresh_timer.setInterval(50)

    def set_light_theme_colors(self, colors: dict, bright_colors: dict,
                                selection_color: tuple, cursor_color: tuple):
        """设置浅色主题颜色"""
        if colors:
            self._current_colors = colors.copy()
        if bright_colors:
            self._current_bright_colors = bright_colors.copy()
        if selection_color:
            self._selection_color = QColor(*selection_color)
        if cursor_color:
            self._cursor_color = QColor(*cursor_color)
        # 清空颜色缓存
        self._color_cache = {}

    def reset_to_dark_theme_colors(self):
        """重置为深色主题颜色"""
        self._current_colors = self.DEFAULT_COLORS.copy()
        self._current_bright_colors = self.BRIGHT_COLORS.copy()
        self._selection_color = QColor(100, 149, 237, 100)
        self._cursor_color = QColor(200, 200, 200, 180)
        # 清空颜色缓存
        self._color_cache = {}

    def get_cwd(self) -> Optional[str]:
        """获取子进程的当前工作目录"""
        if self._backend is None or not self._backend.is_running:
            return None

        # 在 Unix/macOS 上尝试使用 lsof 获取子进程的当前工作目录
        if sys.platform != 'win32':
            try:
                # 获取后端的进程 ID（仅 Unix 后端支持）
                child_pid = getattr(self._backend, '_child_pid', None)
                if child_pid:
                    import subprocess
                    result = subprocess.run(
                        ['lsof', '-a', '-d', 'cwd', '-p', str(child_pid), '-Fn'],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    if result.returncode == 0:
                        for line in result.stdout.split('\n'):
                            if line.startswith('n/'):
                                path = line[1:]  # 去掉开头的 'n'
                                if os.path.isdir(path):
                                    return path
            except Exception:
                pass

        # 无法获取实际的子进程工作目录时返回 None，让调用方使用其自己的回退逻辑
        # 不再回退到 os.getcwd()，因为那是主进程的工作目录，不是终端的工作目录
        return None

    def get_content(self) -> str:
        """获取终端当前内容"""
        lines = []
        for row in range(self.screen.lines):
            line = ''.join(self.screen.buffer[row][col].data
                          for col in range(self.screen.columns))
            lines.append(line.rstrip())
        return '\n'.join(lines)

    def debug_special_chars(self) -> str:
        """调试：找出屏幕上所有特殊字符及其Unicode码点"""
        special_chars = {}
        for row in range(min(self.screen.lines, self.term_rows)):
            for col in range(min(self.screen.columns, self.term_cols)):
                char = self.screen.buffer[row][col].data
                if char and char != ' ':
                    # 检查是否是特殊字符（非ASCII或特殊符号）
                    if ord(char) > 127 or char in '_-|+':
                        if char not in special_chars:
                            special_chars[char] = {
                                'unicode': f'U+{ord(char):04X}',
                                'name': char,
                                'positions': []
                            }
                        special_chars[char]['positions'].append((row, col))

        # 格式化输出
        result = "=== 特殊字符列表 ===\n"
        for char, info in sorted(special_chars.items(), key=lambda x: ord(x[0])):
            result += f"'{char}' {info['unicode']} - 出现 {len(info['positions'])} 次\n"
        return result

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
            pass

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
        """macOS 粘贴处理 - 避免调用 mimeData() 防止 segfault"""
        # clipboard.text() 是安全的，即使剪贴板含有图片也不会崩溃
        text = clipboard.text()

        if text:
            # 有文本内容，转换换行符后粘贴
            data = self._prepare_paste_text(text)
            if self._write_to_backend(data):
                self.input_buffer += text
            return

        # 文本为空，可能是图片或文件
        # 使用 macOS 原生 API (通过 osascript 在子进程中运行) 安全处理
        self._paste_clipboard_data_macos_native()

    def _paste_clipboard_data_macos_native(self):
        """macOS: 使用 osascript + JXA 原生 API 安全处理剪贴板图片/文件"""
        import subprocess
        from datetime import datetime
        import tempfile

        # 准备图片保存路径
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if self.image_save_local:
            try:
                cwd = os.getcwd()
            except (FileNotFoundError, OSError):
                cwd = str(Path.home())
            images_dir = Path(cwd) / ".images"
            images_dir.mkdir(exist_ok=True)
            save_path = str(images_dir / f"paste_{timestamp}.png")
        else:
            temp_dir = Path(tempfile.gettempdir()) / "smart_terminal_images"
            temp_dir.mkdir(exist_ok=True)
            save_path = str(temp_dir / f"paste_{timestamp}.png")

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
                prefix = '@' if self.image_prefix_enabled else ''
                path_text = prefix + save_path + ' '
                data = path_text.encode('utf-8')
                if self._write_to_backend(data):
                    self.input_buffer += path_text
                    self.image_pasted.emit(save_path)

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
                    if self._write_to_backend(data):
                        self.input_buffer += path_text
                        if is_media:
                            self.image_pasted.emit(fp)
        except Exception:
            pass

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
                        if self._write_to_backend(data):
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
                import tempfile

                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

                if self.image_save_local:
                    try:
                        cwd = os.getcwd()
                    except (FileNotFoundError, OSError):
                        cwd = str(Path.home())
                    images_dir = Path(cwd) / ".images"
                    images_dir.mkdir(exist_ok=True)
                    image_path = images_dir / f"paste_{timestamp}.png"
                else:
                    temp_dir = Path(tempfile.gettempdir()) / "smart_terminal_images"
                    temp_dir.mkdir(exist_ok=True)
                    image_path = temp_dir / f"paste_{timestamp}.png"

                if image.save(str(image_path), "PNG"):
                    prefix = '@' if self.image_prefix_enabled else ''
                    path_text = prefix + str(image_path) + ' '
                    data = path_text.encode('utf-8')
                    if self._write_to_backend(data):
                        self.input_buffer += path_text
                        self.image_pasted.emit(str(image_path))
                return

        # 处理文本粘贴
        text = clipboard.text()
        if not text:
            return

        data = self._prepare_paste_text(text)
        if self._write_to_backend(data):
            self.input_buffer += text

    def _copy_to_clipboard(self):
        """复制终端内容到剪贴板（复制当前可见区域的内容）"""
        content = self._get_visible_content()
        if content.strip():
            clipboard = QApplication.clipboard()
            clipboard.setText(content)

    def _get_visible_content(self) -> str:
        """获取当前可见区域的内容（包括历史记录部分）- 优化版本"""
        history = self._get_history_top()
        history_count = len(history)
        total_lines = history_count + self.term_rows
        display_start = max(0, total_lines - self.term_rows - self.scroll_offset)
        term_cols = self.term_cols
        buffer = self.screen.buffer

        lines = []
        for display_row in range(self.term_rows):
            line_idx = display_start + display_row
            if line_idx >= total_lines:
                break

            if line_idx < history_count:
                buffer_line = history[line_idx]
            else:
                buffer_row = line_idx - history_count
                if 0 <= buffer_row < self.term_rows:
                    buffer_line = buffer[buffer_row]
                else:
                    continue

            # 预分配列表，减少动态扩展开销
            chars = [' '] * term_cols
            for col in range(term_cols):
                try:
                    char = buffer_line[col]
                    char_data = getattr(char, 'data', None)
                    if char_data is None:
                        char_data = char if isinstance(char, str) else ' '
                    chars[col] = char_data if char_data else ' '
                except (KeyError, IndexError, TypeError):
                    pass  # 保持默认空格
            lines.append(''.join(chars).rstrip())

        # 移除末尾空行
        while lines and not lines[-1]:
            lines.pop()

        return '\n'.join(lines)

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
            self._search_matches = []
            self._current_match_index = -1
            self.setFocus()
            self.update()

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
        """搜索文本变化"""
        self._search_matches = []
        self._current_match_index = -1

        if not text:
            self._match_label.setText("0/0")
            self.update()
            return

        # 限制搜索结果数量，防止内存暴涨
        MAX_SEARCH_RESULTS = 5000

        # 在所有内容中搜索
        all_content = self._get_all_content()
        lines = all_content.split('\n')

        search_lower = text.lower()
        for row, line in enumerate(lines):
            col = 0
            line_lower = line.lower()
            while True:
                idx = line_lower.find(search_lower, col)
                if idx == -1:
                    break
                self._search_matches.append((row, idx, len(text)))
                col = idx + 1
                # 检查是否达到上限
                if len(self._search_matches) >= MAX_SEARCH_RESULTS:
                    break
            if len(self._search_matches) >= MAX_SEARCH_RESULTS:
                break

        if self._search_matches:
            self._current_match_index = 0

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
        """滚动到当前匹配位置"""
        if not self._search_matches or self._current_match_index < 0:
            return
        row, col, length = self._search_matches[self._current_match_index]
        history_count = self._get_history_count()
        total_lines = history_count + self.term_rows

        # 计算需要的滚动偏移
        if row < history_count:
            self.scroll_offset = history_count - row
        else:
            self.scroll_offset = 0

    def _update_match_label(self):
        """更新匹配计数标签"""
        total = len(self._search_matches)
        current = self._current_match_index + 1 if total > 0 else 0
        self._match_label.setText(f"{current}/{total}")

    # ==================== 字体缩放 ====================

    def _zoom_in(self):
        """放大字体"""
        current_size = self.term_font.pointSize()
        if current_size < 32:
            self.term_font.setPointSize(current_size + 1)
            self._calculate_char_size()
            self._update_terminal_size()
            self.update()

    def _zoom_out(self):
        """缩小字体"""
        current_size = self.term_font.pointSize()
        if current_size > 8:
            self.term_font.setPointSize(current_size - 1)
            self._calculate_char_size()
            self._update_terminal_size()
            self.update()

    # ==================== 双击选词、三击选行 ====================

    def _select_word_at(self, cell: tuple):
        """选中指定位置的单词"""
        row, col = cell
        line_text = self._get_line_text(row)
        if not line_text:
            return

        # 找到单词边界
        word_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')

        # 向左找边界
        start = col
        while start > 0 and (start - 1 < len(line_text)) and line_text[start - 1] in word_chars:
            start -= 1

        # 向右找边界
        end = col
        while end < len(line_text) and line_text[end] in word_chars:
            end += 1

        if start < end:
            self._selection_start = (row, start)
            self._selection_end = (row, end - 1)
            self._is_selecting = False

    def _select_line_at(self, cell: tuple):
        """选中指定位置的整行"""
        row, _ = cell
        self._selection_start = (row, 0)
        self._selection_end = (row, self.term_cols - 1)
        self._is_selecting = False

    def _get_line_text(self, abs_row: int) -> str:
        """获取指定绝对行号的文本

        Args:
            abs_row: 绝对行号（包括历史记录）
        """
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

    # ==================== 保存到文件 ====================

    def _save_to_file(self):
        """保存终端内容到文件"""
        from datetime import datetime
        default_name = f"terminal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        file_path, _ = QFileDialog.getSaveFileName(
            self, "保存终端内容", default_name,
            "文本文件 (*.txt);;所有文件 (*)"
        )

        if file_path:
            content = self._get_all_content()
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
            except Exception as e:
                print(f"Save error: {e}")

    # ==================== 清屏 ====================

    def _clear_screen_keep_history(self):
        """清除屏幕内容但保留历史（发送 clear 命令）"""
        # 发送 Ctrl+L（清屏快捷键）
        self._write_to_backend(b'\x0c')

    def _open_current_directory(self):
        """在文件管理器中打开终端当前工作目录"""
        cwd = self.get_cwd()
        if not cwd or not os.path.isdir(cwd):
            cwd = self._working_dir
        if cwd and os.path.isdir(cwd):
            QDesktopServices.openUrl(QUrl.fromLocalFile(cwd))

    def _get_default_shell(self) -> str:
        """获取系统默认 shell"""
        if sys.platform == 'win32':
            if shutil.which('pwsh'):
                return 'pwsh'
            if shutil.which('powershell'):
                return 'powershell'
            return os.environ.get('COMSPEC', 'cmd.exe')
        else:
            user_shell = os.environ.get('SHELL', '')
            if user_shell and shutil.which(os.path.basename(user_shell)):
                return os.path.basename(user_shell)
            for shell in ['zsh', 'bash', 'sh']:
                if shutil.which(shell):
                    return shell
            return 'sh'

    def _execute_command(self, command: str):
        """执行单个命令"""
        if self._backend is None or not self._backend.is_running:
            # 终端未运行，先启动终端再执行命令
            self._start_and_execute([command])
            return
        # 发送命令并加换行
        data = (command + '\n').encode('utf-8')
        if self._write_to_backend(data):
            # 记录输入
            if command.strip():
                self.input_recorded.emit(command)

    def _execute_commands(self, commands: list):
        """执行多个命令（依次执行）"""
        if not commands:
            return
        if self._backend is None or not self._backend.is_running:
            # 终端未运行，先启动终端再执行命令
            self._start_and_execute(commands)
            return
        for cmd in commands:
            # 发送命令并加换行
            data = (cmd + '\n').encode('utf-8')
            if self._write_to_backend(data):
                # 记录输入
                if cmd.strip():
                    self.input_recorded.emit(cmd)

    def _start_and_execute(self, commands: list):
        """启动终端并执行命令"""
        # 获取当前工作目录
        cwd = getattr(self, '_working_dir', None) or os.getcwd()
        # 启动默认 shell
        shell = self._get_default_shell()
        self.start_process([shell], cwd=cwd)
        # 延迟执行命令，等待终端启动完成
        def delayed_execute():
            if self._backend and self._backend.is_running:
                for cmd in commands:
                    data = (cmd + '\n').encode('utf-8')
                    if self._write_to_backend(data):
                        if cmd.strip():
                            self.input_recorded.emit(cmd)
        QTimer.singleShot(300, delayed_execute)
