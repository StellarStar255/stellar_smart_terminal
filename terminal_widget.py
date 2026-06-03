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


# pyte 0.8 未实现 REP (CSI Pn b) —— 把前一个图形字符重复 N 次。
# nvidia-smi / watch / 部分现代 CLI 会用 "─\x1b[60b" 这种方式画横线，
# 不注册这个分发会导致整段重复被静默丢弃（表格横线只剩一两格）。
# pyte.Stream 在实例化时就把 csi 表捕获进 dispatcher 协程，所以必须
# 在任何 Stream 创建之前就往类属性里写。
if 'b' not in pyte.Stream.csi:
    pyte.Stream.csi['b'] = 'repeat'


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

        # DECCKM (Application Cursor Keys) 模式追踪
        self._decckm = False

        # Bracketed Paste (mode 2004) 模式追踪
        self._bracketed_paste = False

        # REP (CSI Pn b) 需要记住最近一次 draw 的图形字符
        self._last_drawn_char = None

    def select_graphic_rendition(self, *attrs, **kwargs):
        # 移除 private 参数（新版 pyte 会传递，但基类不支持）
        kwargs.pop('private', None)
        return super().select_graphic_rendition(*attrs, **kwargs)

    # ------ 备用屏幕缓冲区 ------

    def set_mode(self, *modes, **kwargs):
        """拦截 private mode 设置，处理备用屏幕切换和 DECCKM"""
        if kwargs.get('private'):
            if not self._in_alt_screen and (self._ALT_SCREEN_MODES & set(modes)):
                self._enter_alt_screen(save_cursor=(1049 in modes))
            # DECCKM (mode 1): 应用光标键模式
            if 1 in modes:
                self._decckm = True
            # Bracketed Paste (mode 2004)
            if 2004 in modes:
                self._bracketed_paste = True
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        """拦截 private mode 重置，处理备用屏幕退出和 DECCKM"""
        if kwargs.get('private'):
            if self._in_alt_screen and (self._ALT_SCREEN_MODES & set(modes)):
                self._leave_alt_screen(restore_cursor=(1049 in modes))
            # DECCKM (mode 1): 关闭应用光标键模式
            if 1 in modes:
                self._decckm = False
            # Bracketed Paste (mode 2004)
            if 2004 in modes:
                self._bracketed_paste = False
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
            # 防御：备用屏幕期间若发生过 resize，光标可能落在新屏幕之外，强制夹紧
            self.cursor.y = max(0, min(self.cursor.y, self.lines - 1))
            self.cursor.x = max(0, min(self.cursor.x, self.columns - 1))
            self._saved_main_buffer = None
            self._saved_main_cursor = None
        self._in_alt_screen = False

    def resize(self, lines=None, columns=None):
        """重写 resize：在备用屏幕中调整大小时，同步重排被保存的主屏幕缓冲区和光标。

        否则在 claude-code 等全屏 TUI（备用屏幕）运行期间分屏/改变窗口大小后，
        退出 TUI 时会把按旧尺寸保存的主屏幕原样恢复，导致提示符错位、上方残留空行。
        """
        new_lines = lines if lines is not None else self.lines
        new_columns = columns if columns is not None else self.columns

        if self._in_alt_screen and self._saved_main_buffer is not None:
            old_lines, old_columns = self.lines, self.columns

            # 暂存当前（备用屏幕）状态
            alt_buffer = self.buffer
            alt_cx, alt_cy = self.cursor.x, self.cursor.y

            # 先借助 pyte 的 resize 逻辑重排被保存的主屏幕缓冲区/光标
            self.buffer = self._saved_main_buffer
            if self._saved_main_cursor is not None:
                self.cursor.x = self._saved_main_cursor.x
                self.cursor.y = self._saved_main_cursor.y
            else:
                self.cursor.x = self.cursor.y = 0
            super().resize(new_lines, new_columns)
            self._saved_main_buffer = self.buffer
            if self._saved_main_cursor is not None:
                self._saved_main_cursor.x = self.cursor.x
                self._saved_main_cursor.y = self.cursor.y

            # 还原旧尺寸，再对备用屏幕本身做一次正常 resize
            self.lines, self.columns = old_lines, old_columns
            self.buffer = alt_buffer
            self.cursor.x, self.cursor.y = alt_cx, alt_cy
            super().resize(new_lines, new_columns)
        else:
            super().resize(new_lines, new_columns)

    # ------ 原有功能 ------

    def draw(self, *chars):
        self._in_draw = True
        try:
            super().draw(*chars)
            # 记录最后绘制的字符，供 REP (CSI Pn b) 使用
            for piece in chars:
                if piece:
                    self._last_drawn_char = piece[-1] if isinstance(piece, str) else piece
        finally:
            self._in_draw = False

    def repeat(self, count=1, *args, **kwargs):
        """REP (CSI Pn b): 将上一个图形字符重复 N 次。"""
        if self._last_drawn_char is None:
            return
        try:
            n = int(count) if count else 1
        except (TypeError, ValueError):
            n = 1
        # 防御性上限：避免恶意/错误序列写爆一整行
        n = max(1, min(n, self.columns))
        self.draw(self._last_drawn_char * n)

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
        if hasattr(self, '_last_drawn_char'):
            self._last_drawn_char = None
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
    move_split_left_requested = pyqtSignal()  # 请求将当前分屏向左移动
    move_split_up_requested = pyqtSignal()  # 请求将当前分屏向上移动

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
        self.setMinimumSize(80, 40)
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
        self._auto_scroll_pull = 0  # 鼠标拉出边缘的距离（像素），用于自适应速度
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
        self._mouse_mode_click = False  # 当前点击是否在鼠标模式下发起

        # 快速命令提供者回调（由主窗口设置，返回预设列表）
        self.quick_commands_provider = None

        # 本地快速命令提供者回调（由主窗口设置，返回本地预设列表）
        self.local_quick_commands_provider = None



    def _calculate_char_size(self):
        """计算字符尺寸"""
        # 使用 QFontMetricsF 获取浮点精度的度量
        fmf = QFontMetricsF(self.term_font)
        self.char_width, self.char_height, self.char_ascent = self._terminal_cell_metrics(fmf)
        self._font_metrics = QFontMetrics(self.term_font)

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
            self._invalidate_render_cache()

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
        self.update()

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
        real_cw, real_ch, real_ascent = self._terminal_cell_metrics(_fm)
        _painter.end()
        if real_cw > 1 and (
            abs(real_cw - self.char_width) > 0.5
            or abs(real_ch - self.char_height) > 0.5
            or abs(real_ascent - self.char_ascent) > 0.5
        ):
            print(
                f"[Terminal] DPI/CJK metrics fix: "
                f"{self.char_width:.1f}x{self.char_height:.1f} -> {real_cw:.1f}x{real_ch:.1f}"
            )
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

            # 缩小后若光标停在空闲提示符上、上方却残留一堆空行（常见于 claude-code 等
            # 退出后清屏但把光标留在低处的情形），把这些空行去掉，让提示符回到顶部。
            if new_rows < old_rows:
                self._reflow_idle_prompt_to_top()

            # 更新PTY大小（子进程会收到 SIGWINCH，可能会发送完整重绘）
            self._update_pty_size()

            # 不主动 erase 可见 buffer：让 pyte 自然处理（保留旧内容直到子进程重写）。
            # 这样即使子进程不响应 SIGWINCH（如 bash 在提示符空闲、进程已退出等），
            # 也不会出现空白终端。少数 TUI 应用使用 CUF 增量重绘可能短暂出现"重影"，
            # 但这种情况极少且很快被新重绘覆盖，远好于内容完全消失。

            self._invalidate_render_cache()
            print(f"[Terminal] Size: {old_cols}x{old_rows} -> {new_cols}x{new_rows} (widget: {self.width()}x{self.height()}, char_w: {self.char_width:.1f})")

    def _reflow_idle_prompt_to_top(self):
        """缩小终端后，若光标处于空闲提示符（其下方没有任何内容），但上方残留若干空行，
        则把这些顶部空行去掉，使提示符回到顶部，避免提示符上方出现大片空白。

        仅作用于主屏幕（非备用屏幕），且只在“光标行是最后一行有内容”的空闲场景执行，
        以免干扰正在主屏幕内增量重绘的程序。
        """
        screen = self.screen
        if getattr(screen, '_in_alt_screen', False):
            return
        buf = screen.buffer
        lines = screen.lines
        cy = screen.cursor.y
        if cy <= 0 or cy >= lines:
            return

        def is_blank(r):
            return all(c.data == ' ' for c in buf[r].values())

        # 光标下方必须全为空行（典型的空闲提示符）
        for r in range(cy + 1, lines):
            if not is_blank(r):
                return

        # 统计顶部连续空行数（不超过光标行）
        k = 0
        while k < cy and is_blank(k):
            k += 1
        if k <= 0:
            return

        # 借助 pyte 的 delete_lines 从顶部删除 k 行（底部自动补空行），再把光标上移 k 行。
        # bash 收到 SIGWINCH 后用 \r 原地重绘提示符，处理顺序在此之后，因此光标位置保持一致。
        cx = screen.cursor.x
        saved_margins = screen.margins
        screen.margins = None  # 临时取消滚动区域限制，确保对整屏生效
        screen.cursor.y = 0
        screen.cursor.x = 0
        try:
            screen.delete_lines(k)
        finally:
            screen.margins = saved_margins
        screen.cursor.y = cy - k
        screen.cursor.x = cx
        screen.dirty.update(range(lines))

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

            # 响应 XTVERSION 查询 (\x1b[>q 或 \x1b[>0q)
            # XTVERSION 标准允许省略参数（默认 0），codex 用的 ratatui/crossterm
            # 走的就是裸 \x1b[>q 形式。Pp 任意数字都视为 XTVERSION 请求。
            # 不回复 → 上层 TUI 超时 → 降级渲染（box-drawing 边框丢失等症状）
            if re.search(r'\x1b\[>\d*q', text):
                response = '\x1bP>|SmartTerminal(1.0)\x1b\\'
                self._write_to_backend(response.encode())

            # 响应 DSR 操作状态查询 (\x1b[5n) — 回复 "OK"
            # crossterm 等库会用此查询来确认终端就绪
            if '\x1b[5n' in text:
                self._write_to_backend(b'\x1b[0n')
                text = text.replace('\x1b[5n', '')

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

            # 仅过滤纯噪声：UTF-8 解码失败产生的替换字符（不是程序真正输出的内容）
            # 注意：不要过滤可显示的 Unicode 符号（如 ⏺ U+23FA），否则 codex /
            # claude code 这类用 BMP 符号做行首/状态标记的工具会丢失关键信息。
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

    def _debug_save_cache_pixmap(self):
        """保存当前 cache pixmap 为 PNG，便于和屏幕实际显示对比。"""
        if self._cache_pixmap is None or self._cache_pixmap.isNull():
            return None
        out_path = os.path.join(os.path.dirname(__file__), 'pyte_cache_dump.png')
        ok = self._cache_pixmap.save(out_path, "PNG")
        if ok:
            return out_path
        return None

    def _debug_save_widget_screenshot(self):
        """抓取 widget 当前实际显示（包括 paintEvent 所有 overlay 之后），保存 PNG。

        这个 PNG 应该和你眼睛在 Stellar 窗口看到的内容一致；和 cache pixmap PNG 对比
        就能定位 bug 是在缓存阶段还是 paintEvent 的 overlay 阶段（cursor / selection /
        search highlight / IME preedit）。
        """
        out_path = os.path.join(os.path.dirname(__file__), 'pyte_widget_screenshot.png')
        pix = self.grab()
        if pix.isNull():
            return None
        ok = pix.save(out_path, "PNG")
        if ok:
            return out_path
        return None

    def _debug_dump_buffer(self):
        """调试：将 pyte 缓冲区内容导出到文件。

        为了诊断 CJK 渲染问题，包含两类信息：
        1) 行预览（直接 join，空 data 用 '·' 表示便于看 stub）
        2) 含 CJK 的行：逐 cell 详细输出（col / repr(data) / east_asian_width）
        3) Stellar 进程内实测字体度量（QFontInfo / QFontMetricsF / painter.fontMetrics()）
           —— 这是诊断 char_width 与 CJK glyph advance 不匹配的关键。
        """
        dump_path = os.path.join(os.path.dirname(__file__), 'pyte_buffer_dump.txt')

        # --- 进程内实测字体度量 ---
        from PyQt6.QtGui import QFontInfo, QFontMetricsF, QPixmap, QPainter
        finfo = QFontInfo(self.term_font)
        fmf = QFontMetricsF(self.term_font)
        # painter 视角度量（与 _rebuild_cache 时上下文一致）
        _pix = QPixmap(max(1, int(self.width())), max(1, int(self.height())))
        _p = QPainter(_pix)
        _p.setFont(self.term_font)
        pfm = _p.fontMetrics()

        font_metrics_lines = []
        font_metrics_lines.append(f"font requested: Monaco 12, actual family={finfo.family()!r}, "
                                  f"px={finfo.pixelSize()}, pt={finfo.pointSize()}, "
                                  f"fixedPitch={finfo.fixedPitch()}")
        font_metrics_lines.append(f"self.char_width = {self.char_width}")
        font_metrics_lines.append(f"QFontMetricsF: 'W'={fmf.horizontalAdvance('W'):.4f}, "
                                  f"'你'={fmf.horizontalAdvance('你'):.4f}, "
                                  f"'好'={fmf.horizontalAdvance('好'):.4f}")
        font_metrics_lines.append(f"painter.fontMetrics() (int): 'W'={pfm.horizontalAdvance('W')}, "
                                  f"'你'={pfm.horizontalAdvance('你')}, "
                                  f"'好'={pfm.horizontalAdvance('好')}, "
                                  f"'呀'={pfm.horizontalAdvance('呀')}")
        cjk_adv = pfm.horizontalAdvance('你')
        alloc = 2 * self.char_width
        font_metrics_lines.append(f"==> 2*char_width = {alloc:.4f}, painter CJK advance = {cjk_adv}, "
                                  f"差额 = {alloc - cjk_adv:+.4f} px "
                                  f"({'cell 比 glyph 宽（正常）' if alloc >= cjk_adv else 'CJK 溢出 cell（异常 → 被后字覆盖）'})")
        _p.end()

        def _row_has_cjk(row_obj, cols):
            for c in range(cols):
                try:
                    ch = row_obj[c]
                    t = ch.data if hasattr(ch, 'data') else str(ch)
                except (KeyError, IndexError, TypeError):
                    continue
                if t and unicodedata.east_asian_width(t[0]) in ('F', 'W'):
                    return True
            return False

        def _cell_detail(row_obj, cols):
            lines = []
            for c in range(cols):
                try:
                    ch = row_obj[c]
                except (KeyError, IndexError, TypeError):
                    continue
                if hasattr(ch, 'data'):
                    t = ch.data
                    fg = getattr(ch, 'fg', 'default')
                    bg = getattr(ch, 'bg', 'default')
                    bold = getattr(ch, 'bold', False)
                    reverse = getattr(ch, 'reverse', False)
                    attrs = []
                    if fg != 'default':
                        attrs.append(f"fg={fg}")
                    if bg != 'default':
                        attrs.append(f"bg={bg}")
                    if bold:
                        attrs.append("bold")
                    if reverse:
                        attrs.append("reverse")
                    attr_str = (' [' + ','.join(attrs) + ']') if attrs else ''
                else:
                    t = str(ch)
                    attr_str = ''
                if not t:
                    lines.append(f"    col {c:3d}: <empty/stub>{attr_str}")
                else:
                    eaw = unicodedata.east_asian_width(t[0])
                    lines.append(f"    col {c:3d}: {t!r}  eaw={eaw}{attr_str}")
            return lines

        with open(dump_path, 'w', encoding='utf-8') as f:
            f.write(f"=== PYTE BUFFER DUMP ===\n")
            f.write(f"term_cols={self.term_cols}, term_rows={self.term_rows}\n")
            f.write(f"screen.columns={self.screen.columns}, screen.lines={self.screen.lines}\n")
            f.write(f"char_width={self.char_width}, char_height={self.char_height}\n")
            f.write(f"scroll_offset={self.scroll_offset}\n")
            history_count, total_lines, start_line = self._get_display_info()
            f.write(f"history_count={history_count}, total_lines={total_lines}, start_line={start_line}\n")
            f.write(f"cursor: x={self.screen.cursor.x}, y={self.screen.cursor.y}, "
                    f"hidden={self.screen.cursor.hidden}, "
                    f"cursor_visible(blink)={self.cursor_visible}, hasFocus={self.hasFocus()}\n")
            f.write(f"widget.size={self.width()}x{self.height()}, "
                    f"devicePixelRatioF={self.devicePixelRatioF()}\n")
            if self._cache_pixmap and not self._cache_pixmap.isNull():
                f.write(f"cache_pixmap.size={self._cache_pixmap.width()}x{self._cache_pixmap.height()}, "
                        f"DPR={self._cache_pixmap.devicePixelRatio()}\n")
            else:
                f.write("cache_pixmap: <null>\n")
            f.write("\n=== FONT METRICS (in-process) ===\n")
            for line in font_metrics_lines:
                f.write(line + "\n")
            f.write("\nNote: in row preview, '·' = empty/stub cell, ' ' = real space.\n\n")

            cols = min(self.screen.columns, 200)

            f.write("--- CURRENT SCREEN BUFFER ---\n")
            for row in range(self.screen.lines):
                row_obj = self.screen.buffer[row]
                chars = []
                for col in range(cols):
                    try:
                        ch = row_obj[col]
                        t = ch.data if hasattr(ch, 'data') else str(ch)
                    except (KeyError, IndexError, TypeError):
                        t = ''
                    chars.append(t if t else '·')
                line = ''.join(chars).rstrip()
                f.write(f"  row {row:2d}: |{line}|\n")
                if _row_has_cjk(row_obj, cols):
                    f.write("    -- cell detail (CJK row) --\n")
                    for ln in _cell_detail(row_obj, cols):
                        f.write(ln + "\n")

            f.write("\n--- HISTORY (last 10 lines) ---\n")
            history = self._get_history_top()
            if history:
                h_start = max(0, len(history) - 10)
                for i in range(h_start, len(history)):
                    row_obj = history[i]
                    chars = []
                    for col in range(cols):
                        try:
                            ch = row_obj[col]
                            t = ch.data if hasattr(ch, 'data') else str(ch)
                        except (KeyError, IndexError, TypeError):
                            t = ''
                        chars.append(t if t else '·')
                    line = ''.join(chars).rstrip()
                    f.write(f"  hist {i:3d}: |{line}|\n")
                    if _row_has_cjk(row_obj, cols):
                        f.write("    -- cell detail (CJK row) --\n")
                        for ln in _cell_detail(row_obj, cols):
                            f.write(ln + "\n")

        print(f"[Terminal] Buffer dumped to {dump_path}")

        cache_png = self._debug_save_cache_pixmap()
        if cache_png:
            print(f"[Terminal] Cache pixmap saved to {cache_png}")

        screen_png = self._debug_save_widget_screenshot()
        if screen_png:
            print(f"[Terminal] Widget screenshot saved to {screen_png}")

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
            print(
                f"[Terminal] Calibration: "
                f"{old_cw:.1f}x{old_ch:.1f} -> {self.char_width:.1f}x{self.char_height:.1f}"
            )
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
        if not last_char or not first_char:
            return False
        # 边界已经有空白（前一行尾部空格或后一行首部空格），不再额外补空格
        if last_char.isspace() or first_char.isspace():
            return False
        last_wide = unicodedata.east_asian_width(last_char) in ('F', 'W')
        first_wide = unicodedata.east_asian_width(first_char) in ('F', 'W')
        if last_wide and first_wide:
            return False  # CJK + CJK: 不需要空格
        # Latin+CJK, CJK+Latin, Latin+Latin: 需要空格（词边界）
        return True

    def _row_tile(self, row, count=1):
        """返回从 row 起 count 行的 (y, height)，使相邻行像素无缝拼接

        char_height 可能是分数（如 18.5），单纯 int(char_height) 会让
        每隔几行多 1px 缝隙，导致带背景色的连续行看起来像断开的方块。
        用「下一格起点 - 当前格起点」算高度可保证逐行无缝。
        """
        y_start = int(self.PADDING + row * self.char_height)
        y_end = int(self.PADDING + (row + count) * self.char_height)
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
        need_rebuild = (
            self._cache_pixmap is None or
            self._cache_pixmap.isNull() or
            not self._cache_valid or
            self._cache_pixmap.size() != self.size() * dpr or
            self._cache_pixmap.devicePixelRatio() != dpr
        )

        if need_rebuild:
            self._rebuild_cache()

        opaque_bg = QColor(self.bg_color)
        opaque_bg.setAlpha(255)

        # 使用 Source 合成模式：完全替换像素，不做 alpha 混合。
        # 先填充背景确保终端区域自身完全不透明（防止父/兄弟 widget 内容透出）。
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.fillRect(self.rect(), opaque_bg)
        if self._cache_pixmap and not self._cache_pixmap.isNull():
            painter.drawPixmap(0, 0, self._cache_pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

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
        if (self._cache_pixmap is None
                or self._cache_pixmap.isNull()
                or self._cache_pixmap.size() != target_size
                or self._cache_pixmap.devicePixelRatio() != dpr):
            self._cache_pixmap = QPixmap(target_size)
            if self._cache_pixmap.isNull():
                return
            self._cache_pixmap.setDevicePixelRatio(dpr)

        opaque_bg = QColor(self.bg_color)
        opaque_bg.setAlpha(255)
        self._cache_pixmap.fill(opaque_bg)

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
        bg_default = opaque_bg
        last_fg_color = None  # 缓存上一个前景色，减少 setPen 调用
        padding = self.PADDING  # 局部变量加速
        for display_row, buffer_line in enumerate(display_lines):
            row_y = int(padding + display_row * char_height)
            # 用下一行起点减去本行起点作为本行高度，防止分数像素累计造成行间空隙
            row_h = int(padding + (display_row + 1) * char_height) - row_y
            text_y = int(row_y + self.char_ascent)
            row_cells = []

            for col in range(num_cols):
                try:
                    char = buffer_line[col]
                except (KeyError, IndexError, TypeError):
                    continue

                # 优化：一次性获取所有属性，避免多次 getattr 调用
                # pyte 的 Char 是 namedtuple，可以直接访问属性
                if hasattr(char, 'data'):
                    char_text = char.data
                    char_fg = char.fg
                    char_bg = char.bg
                    char_bold = char.bold
                    char_reverse = char.reverse
                elif isinstance(char, str):
                    char_text = char
                    char_fg = 'default'
                    char_bg = 'default'
                    char_bold = False
                    char_reverse = False
                else:
                    continue

                # 计算颜色（提前到 skip 之前，以便空 data 时也能填充背景）
                fg_color = self._get_char_color(char_fg, char_bold)
                bg_color = self._get_char_color(char_bg, False, is_bg=True)

                # 处理反转
                if char_reverse:
                    fg_color, bg_color = bg_color, fg_color

                has_bg = bg_color != bg_default
                # 空 data 且无背景色 → 真的没东西可画，跳过
                if not char_text and not has_bg:
                    continue

                cell_x = int(padding + col * char_width)

                # 判断宽字符（空 data 视为单宽，等同于普通空格的填充）
                is_wide = bool(char_text) and self._is_wide_char(char_text)
                span = 2 if is_wide else 1

                # 用「下一格起点 - 当前格起点」算宽度，使背景在水平方向也无缝拼接
                char_draw_width = int(padding + (col + span) * char_width) - cell_x
                row_cells.append((
                    cell_x,
                    char_draw_width,
                    char_text,
                    fg_color,
                    bg_color,
                    has_bg,
                ))

            # 先绘制整行背景，再绘制文字。宽字符在 pyte 中会占用后一格
            # 空 stub；如果按 cell 交替绘制，带背景的 stub 会盖住中文右半边。
            for cell_x, char_draw_width, _char_text, _fg_color, bg_color, has_bg in row_cells:
                if has_bg:
                    painter.fillRect(cell_x, row_y, char_draw_width, row_h, bg_color)

            for cell_x, _char_draw_width, char_text, fg_color, bg_color, _has_bg in row_cells:
                # 字符严格按 pyte 的 cell 坐标绘制。pyte 已用 wcwidth 为 CJK
                # 留出 stub cell；额外按 glyph advance 推动 x 会在 Claude/Ink
                # 提交后局部重绘时累积偏移，导致中文只显示半个或错位。
                if char_text and char_text != ' ':
                    fg_color = self._ensure_visible(fg_color, bg_color)
                    if fg_color != last_fg_color:
                        painter.setPen(fg_color)
                        last_fg_color = fg_color
                    painter.drawText(cell_x, text_y, char_text)

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

        # Cmd+B (macOS) / Ctrl+B: 切换 Explorer 面板 — 委托给主窗口
        # 注意：只匹配 Cmd(ControlModifier)，物理 Ctrl+B(MetaModifier) 仍发送到终端
        if (modifiers & Qt.KeyboardModifier.ControlModifier
                and not (modifiers & (Qt.KeyboardModifier.MetaModifier
                                       | Qt.KeyboardModifier.AltModifier
                                       | Qt.KeyboardModifier.ShiftModifier))
                and key == Qt.Key.Key_B):
            main_win = self.window()
            if hasattr(main_win, '_toggle_explorer_panel'):
                main_win._toggle_explorer_panel()
                event.accept()
                return

        # Cmd+G (macOS) / Ctrl+G: 切换 Git 面板 — 委托给主窗口
        # 注意：只匹配 Cmd(ControlModifier)，物理 Ctrl+G(MetaModifier) 仍发送到终端
        if (modifiers & Qt.KeyboardModifier.ControlModifier
                and not (modifiers & (Qt.KeyboardModifier.MetaModifier
                                       | Qt.KeyboardModifier.AltModifier
                                       | Qt.KeyboardModifier.ShiftModifier))
                and key == Qt.Key.Key_G):
            main_win = self.window()
            if hasattr(main_win, '_toggle_git_panel'):
                main_win._toggle_git_panel()
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
            self._invalidate_render_cache()
        event.accept()

    def scroll_to_bottom(self):
        """滚动到底部（最新内容）"""
        if self.scroll_offset != 0:
            self.scroll_offset = 0
            self._invalidate_render_cache()

    def _auto_scroll_tick(self):
        """自动滚动定时器回调 - 支持拖动选择时跨页

        滚动速度与「鼠标拉出边缘的距离」自适应：
        - 鼠标越远离边缘（甚至超出 widget），每次 tick 滚动行数越多
        - 用户向上猛拉时立即跨大段历史，轻微靠近边缘时则慢速精细滚动
        """
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
        鼠标模式下，单击仍然使用本地光标定位（通过方向键），
        拖动选择和其他鼠标事件则正常转发给程序。
        """
        import time
        self.setFocus(Qt.FocusReason.MouseFocusReason)

        # Shift 键或回滚历史时强制使用本地选择模式（绕过鼠标模式）
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

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
                # 双击：选「宽」词——整条路径/URL（只在空格处截断）
                if self._mouse_mode and not force_local_selection:
                    self._send_mouse_event(event, 'press')
                self._select_word_at(abs_cell)
            elif self._click_count >= 3:
                # 三击：选「窄」词——只取 / @ . : - ~ 之间的一段（如 huangqiliang）
                if self._mouse_mode and not force_local_selection:
                    self._send_mouse_event(event, 'press')
                self._select_word_at(abs_cell, self._WORD_CHARS_NARROW)
                self._click_count = 0  # 重置
            else:
                # 单击开始选择 - 使用绝对坐标
                # 即使鼠标模式启用，也记录按下位置用于后续的单击光标定位
                self._selection_start = abs_cell
                self._selection_end = abs_cell
                self._is_selecting = True
                self._select_all_mode = False  # 清除全选模式
                # 记录是否处于鼠标模式，用于 release 时决定是否也转发事件
                self._mouse_mode_click = self._mouse_mode and not force_local_selection

            self.update()
        elif self._mouse_mode and not force_local_selection:
            # 非左键在鼠标模式下转发给程序
            self._send_mouse_event(event, 'press')
        # 接受事件，确保 Qt 将后续的 mouseReleaseEvent 发送给本控件
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        """鼠标移动 - 更新选择区域（使用绝对坐标），支持拖动时自动滚动"""
        # Shift 键或回滚历史时强制使用本地选择模式
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        # 如果鼠标模式启用且没有强制本地选择，且正在拖动（按住按钮）
        # 但如果 _is_selecting 为 True（左键单击流程），继续本地选择而不转发
        if self._mouse_mode and not force_local_selection and event.buttons() and not self._is_selecting:
            self._send_mouse_event(event, 'move')
            return

        if self._is_selecting:
            self._last_mouse_pos = event.pos()
            self._selection_end = self._pos_to_absolute_cell(event.pos())

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
        # 停止自动滚动
        self._auto_scroll_timer.stop()
        self._auto_scroll_direction = 0
        self._auto_scroll_pull = 0

        # Shift 键或回滚历史时强制使用本地选择模式
        force_local_selection = (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) or self.scroll_offset > 0

        # 如果鼠标模式启用且没有强制本地选择，且不在本地选择流程中
        if self._mouse_mode and not force_local_selection and not self._is_selecting:
            self._send_mouse_event(event, 'release')
            return

        if event.button() == Qt.MouseButton.LeftButton and self._is_selecting:
            self._selection_end = self._pos_to_absolute_cell(event.pos())
            self._is_selecting = False

            # 检查是否是单击（没有拖动选择）
            # 使用容差判断：如果起止位置在同一行且列差距 ≤ 1，视为单击
            start_row, start_col = self._selection_start
            end_row, end_col = self._selection_end
            is_click = (start_row == end_row and abs(start_col - end_col) <= 1)
            if is_click:
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

        # 复制当前路径
        copy_dir_action = QAction(t("ctx.copy_current_dir"), self)
        copy_dir_action.triggered.connect(self._copy_current_directory)
        menu.addAction(copy_dir_action)

        menu.addSeparator()

        # 刷新终端：强制重绘 + 给 PTY 重发尺寸（SIGWINCH），修复偶发花屏/错位
        refresh_action = QAction(t("ctx.refresh"), self)
        refresh_action.triggered.connect(self.refresh_terminal)
        menu.addAction(refresh_action)

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

        # 分屏（左右）
        split_action = QAction(t("toolbar.split"), self)
        split_action.triggered.connect(self.split_horizontal_requested.emit)
        menu.addAction(split_action)

        # 上下分屏
        vsplit_action = QAction(t("ctx.split_vertical"), self)
        vsplit_action.triggered.connect(self.split_vertical_requested.emit)
        menu.addAction(vsplit_action)

        # 关闭当前分屏
        close_split_action = QAction(t("ctx.close_split"), self)
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

        # 每行收集: (line_text, is_soft, last_content_col, leading_space_count, can_heuristic)
        # last_content_col: 整行最后一个非空格字符的列结尾位置（用于判断行是否"填满到行尾"）
        # leading_space_count: 整行起首的空格数（用于判断下一行是否是续行缩进）
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
            leading_space_count = 0
            seen_non_space = False
            for c_col, c_ch in chars:
                if c_ch == ' ':
                    if not seen_non_space:
                        leading_space_count += 1
                else:
                    seen_non_space = True
                    last_content_col = c_col + (1 if self._is_wide_char(c_ch) else 0)

            is_soft = self.screen.is_soft_wrapped(buffer_line)
            can_heuristic = (col_start == 0 and col_end == term_cols - 1)

            if is_soft:
                # 终端软换行：保留所有字符（包括尾部空格，它们是真实内容）
                line_text = ''.join(selected_chars)
            else:
                line_text = ''.join(selected_chars).rstrip()

            rows.append((line_text, is_soft, last_content_col, leading_space_count, can_heuristic))

        # 用 look-ahead 决定每行的换行类型：0=硬换行, 1=终端软换行, 2=应用层软换行
        threshold_high = max(int(term_cols * 0.85), term_cols - 6)
        threshold_low = max(8, int(term_cols * 0.40))
        n = len(rows)
        resolved = []
        for i, (text, is_soft, last_col, _, can_h) in enumerate(rows):
            if i == n - 1:
                resolved.append((text, 0))
                continue
            if is_soft:
                resolved.append((text, 1))
                continue
            wrap_type = 0
            if can_h and last_col >= 0:
                next_leading = rows[i + 1][3]
                next_has_content = bool(rows[i + 1][0].strip())
                if last_col >= threshold_high:
                    # 高置信度：行内容直接占满到行尾附近 → 应用层 wrap
                    wrap_type = 2
                elif last_col >= threshold_low and next_leading >= 1 and next_has_content:
                    # 中等置信度：行占用过半且下一行带续行缩进 → 应用层 wrap
                    # 命中如 Claude Code 等渲染器按自己目标宽度（远小于终端列数）wrap 的场景
                    wrap_type = 2
            resolved.append((text, wrap_type))

        # 组合结果
        result = []
        for i, (line_text, wrap_type) in enumerate(resolved):
            if i > 0:
                prev_text, prev_wrap = resolved[i - 1]
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
            """提取行内容，返回 (text, is_soft, last_content_col, leading_space_count)
            最终换行类型在后续 look-ahead 阶段决定。
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
            leading_space_count = 0
            seen_non_space = False
            for c_col, c_ch in chars_with_col:
                if c_ch == ' ':
                    if not seen_non_space:
                        leading_space_count += 1
                else:
                    seen_non_space = True
                    last_content_col = c_col + (1 if is_wide(c_ch) else 0)

            is_soft = screen.is_soft_wrapped(buffer_line)
            if is_soft:
                text = ''.join(char_list)  # 终端软换行：保留尾部空格
            else:
                text = ''.join(char_list).rstrip()

            return text, is_soft, last_content_col, leading_space_count

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

        # 用 look-ahead 决定换行类型：0=硬换行, 1=终端软换行, 2=应用层软换行
        threshold_high = max(int(columns * 0.85), columns - 6)
        threshold_low = max(8, int(columns * 0.40))
        n = len(line_data)
        resolved = []
        for i, (text, is_soft, last_col, _) in enumerate(line_data):
            if i == n - 1:
                resolved.append((text, 0))
                continue
            if is_soft:
                resolved.append((text, 1))
                continue
            wrap_type = 0
            if last_col >= 0:
                next_leading = line_data[i + 1][3]
                next_has_content = bool(line_data[i + 1][0].strip())
                if last_col >= threshold_high:
                    wrap_type = 2  # 高置信度：行内容直接占满到行尾附近
                elif last_col >= threshold_low and next_leading >= 1 and next_has_content:
                    # 中等置信度：行占用过半且下一行带续行缩进 → 应用层 wrap
                    wrap_type = 2
            resolved.append((text, wrap_type))

        # 组合结果
        need_space = self._need_boundary_space
        result = []
        for i, (text, wrap_type) in enumerate(resolved):
            if i > 0:
                prev_text, prev_wrap = resolved[i - 1]
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
        self._invalidate_render_cache()

    def refresh_terminal(self):
        """刷新终端显示（非破坏性）：

        - 重建渲染缓存并强制重绘，修复本地渲染脏区造成的花屏/错位；
        - 用「收窄一列再恢复」的方式抖动 PTY 尺寸，**保证**产生一次 SIGWINCH，
          让前台 TUI 程序（vim / tmux / claude 等）整屏重画自己。
          （直接重发当前尺寸时，尺寸未变 → 内核不发 SIGWINCH → 程序不会重画，
          这正是之前"刷新没反应"的原因。）
        不清空内容、不影响滚动历史，区别于 clear_screen / Ctrl+L。
        """
        # 先本地重绘，修复纯渲染层的脏区
        self._invalidate_render_cache()

        if self._backend is None:
            return

        # 抖动 PTY 尺寸强制 SIGWINCH：收窄一列 → 等一拍 → 恢复并再重绘。
        # 分两拍（singleShot）是为了让子进程先收到"变窄"的 SIGWINCH 再收到"恢复"的，
        # 避免两次 resize 在内核里被合并成一次而失去效果。
        try:
            if self.term_cols > 1:
                self._backend.resize(self.term_cols - 1, self.term_rows)
                QTimer.singleShot(40, self._restore_pty_size_after_refresh)
            else:
                self._update_pty_size()
        except Exception as e:
            print(f"[Terminal] refresh resize error: {e}")

    def _restore_pty_size_after_refresh(self):
        """refresh_terminal 抖动后把 PTY 尺寸恢复到当前实际值，并再重绘一次。"""
        if self._backend is None:
            return
        try:
            self._backend.resize(self.term_cols, self.term_rows)
        except Exception as e:
            print(f"[Terminal] refresh restore error: {e}")
        self._invalidate_render_cache()

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
        self._visible_color_cache = {}

    def reset_to_dark_theme_colors(self):
        """重置为深色主题颜色"""
        self._current_colors = self.DEFAULT_COLORS.copy()
        self._current_bright_colors = self.BRIGHT_COLORS.copy()
        self._selection_color = QColor(100, 149, 237, 100)
        self._cursor_color = QColor(200, 200, 200, 180)
        # 清空颜色缓存
        self._color_cache = {}
        self._visible_color_cache = {}

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

    def _write_paste(self, data: bytes) -> bool:
        """将粘贴内容写入后端，必要时以 Bracketed Paste 序列包裹

        当应用（如 Claude Code）启用了 DECSET 2004 时，终端需要把粘贴内容
        包裹在 ESC[200~ ... ESC[201~ 之间，应用据此识别"整块粘贴"（而不是
        逐字键入），从而可以对粘贴的图片路径进行特殊处理（例如显示 [Image #N]）。
        """
        if self._is_bracketed_paste_enabled():
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

    def _paste_clipboard_data_macos_native(self) -> bool:
        """macOS: 使用 osascript + JXA 原生 API 安全处理剪贴板图片/文件

        返回 True 表示剪贴板中含图片/文件并已处理；返回 False 表示剪贴板里
        没有图片或文件 URL（调用方应回落到文本粘贴）。
        """
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
                handled_any = False
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
                        handled_any = True
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
        old_offset = self.scroll_offset
        row, col, length = self._search_matches[self._current_match_index]
        history_count = self._get_history_count()
        total_lines = history_count + self.term_rows

        # 计算需要的滚动偏移
        if row < history_count:
            self.scroll_offset = history_count - row
        else:
            self.scroll_offset = 0
        if old_offset != self.scroll_offset:
            self._invalidate_render_cache()

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
            self._invalidate_render_cache()

    def _zoom_out(self):
        """缩小字体"""
        current_size = self.term_font.pointSize()
        if current_size > 8:
            self.term_font.setPointSize(current_size - 1)
            self._calculate_char_size()
            self._update_terminal_size()
            self._invalidate_render_cache()

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

    # 双击用的「宽」单词字符集：纳入路径/URL 常见字符（/ . : ~ @ % + = , -），
    # 这样双击一条完整路径或 URL 能整体选中，而不是停在第一个 / 或 . 处。
    _WORD_CHARS_WIDE = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-./:~@%+=,')
    # 三击用的「窄」单词字符集：只含字母数字和下划线，遇到 / @ . : - ~ 等分隔符即截断，
    # 所以三击 /home/huangqiliang 或 huangqiliang@host 只选中其中一个 huangqiliang。
    _WORD_CHARS_NARROW = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_')

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

        # 向上扩展：单词顶到行首且上一行是被折行的续行 → 接到上一行末尾的单词部分
        while start_col == 0 and start_row > 0 and self._is_row_soft_wrapped(start_row - 1):
            prev_text = self._get_line_text(start_row - 1).rstrip('\x00')
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
        while end_row < total - 1 and self._is_row_soft_wrapped(end_row):
            cur_text = self._get_line_text(end_row)
            if end_col < len(cur_text) - 1 and cur_text[end_col + 1] in word_chars:
                # 单词其实还没到本可见行末尾（理论上不该发生），停止
                pass
            next_text = self._get_line_text(end_row + 1)
            if not next_text or next_text[0] not in word_chars:
                break
            e = 0
            while e < len(next_text) and next_text[e] in word_chars:
                e += 1
            end_row += 1
            end_col = e - 1
            if end_col != len(next_text) - 1:
                break

        self._selection_start = (start_row, start_col)
        self._selection_end = (end_row, end_col)
        self._is_selecting = False

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

    def _copy_current_directory(self):
        """复制终端当前工作目录路径到剪贴板"""
        cwd = self.get_cwd()
        if not cwd or not os.path.isdir(cwd):
            cwd = self._working_dir
        if cwd:
            clipboard = QApplication.clipboard()
            clipboard.setText(cwd)

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
