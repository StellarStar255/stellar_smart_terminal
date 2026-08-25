"""
嵌入式终端控件
基于PyQt6 + pyte实现的完整终端模拟器
支持 Windows (pywinpty) 和 Unix (pty) 平台
"""
import os
import re
import sys
import time
import shutil
import codecs
import itertools
import threading
from collections import OrderedDict
from typing import Optional, List
from pathlib import Path
import unicodedata

import pyte
from pyte.screens import wcwidth

# screen 层已拆到 terminal_screen（此处再导出，保持既有 import 路径可用）
from terminal_screen import (
    CompatibleHistoryScreen, reflow_rows, map_reflow_position, _cell_is_blank
)

from terminal_render import TerminalRenderMixin
from terminal_backend import create_backend, TerminalBackend
from i18n import t
from app_logging import get_logger
from utils import get_data_dir

logger = get_logger(__name__)

# --- 取进程 cwd 的原生实现（不 fork） ---
# 旧实现只有 `lsof -a -d cwd -p <pid>`：每次约 27ms，且在 GUI 线程同步调用
# （切标签页/分屏/粘贴图片都会触发）。更糟的是 cwd 落在挂死的网络挂载点上时
# lsof 会一路顶到 2s 超时——多标签场景下表现为「别的终端也跟着卡住」。
# 原生接口约 0.2ms 且不会阻塞：Linux 读 /proc，macOS 走 libproc。
# lsof 仅作为两者都失败时的兜底。
_PROC_PIDVNODEPATHINFO = 9      # <sys/proc_info.h>
_VNODE_PATH_OFFSET = 152        # offsetof(struct vnode_info_path, vip_path)
_PROC_VNODEPATHINFO_SIZE = 2352  # sizeof(struct proc_vnodepathinfo)
_libproc = None                 # 懒加载；None=未尝试，False=不可用


def _cwd_via_native(pid: int) -> Optional[str]:
    """取指定进程的当前工作目录，不创建子进程。失败返回 None。"""
    if sys.platform.startswith('linux'):
        try:
            return os.readlink(f'/proc/{pid}/cwd')
        except OSError:
            return None
    if sys.platform != 'darwin':
        return None
    global _libproc
    if _libproc is None:
        try:
            import ctypes
            import ctypes.util
            _libproc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
        except Exception:
            logger.debug("libproc unavailable", exc_info=True)
            _libproc = False
    if not _libproc:
        return None
    try:
        import ctypes
        buf = ctypes.create_string_buffer(_PROC_VNODEPATHINFO_SIZE)
        n = _libproc.proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO,
                                  ctypes.c_uint64(0), buf,
                                  _PROC_VNODEPATHINFO_SIZE)
        if n != _PROC_VNODEPATHINFO_SIZE:
            return None
        raw = buf.raw[_VNODE_PATH_OFFSET:]
        path = raw.split(b'\0', 1)[0].decode('utf-8', 'replace')
        return path or None
    except Exception:
        logger.debug("proc_pidinfo failed", exc_info=True)
        return None


# 复制拼接时用于识别「新的结构行」（列表项/有序列表/标题/引用）。
# 这类行即使上一行被填满，也不应被并入上一行，必须保留换行。
_LIST_MARKER_RE = re.compile(r'^(?:[-*+•‣◦·]|\d{1,3}[.)]|[A-Za-z][.)]|#{1,6}|>)\s')

# 强 token 分隔符：URL/路径/命令/参数里常见，散文里几乎不出现。断点处的串含这些
# 字符即可判定为「被行宽截断的长 token」而非散文折行（'.'、'-' 二者皆有歧义，故
# 不收入此集合，改由「整段长度放不下一行」这一信号兜底）。
_TOKEN_SEP_RE = re.compile(r'[/:@=?&%#~\\]')


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


def merge_extracted_lines(rows, columns: int,
                          strip_soft_trailing: bool = False) -> str:
    """把从终端缓冲区提取的逐行内容合并成复制用文本（纯函数，无 Qt 依赖）。

    rows: [(text, is_soft, last_content_col, spaceless, can_heuristic), ...]
        text             —— 该行（或选区内）的文本；软换行行保留尾部空格
        is_soft          —— 终端层软换行（DECAWM 自动换行）标记
        last_content_col —— 整行最后一个非空格字符的列结尾位置（-1 表示空行）
        spaceless        —— 整行内容无内部空格（单一连续 token，如 URL/路径/哈希）
        can_heuristic    —— 是否允许应用层折行启发式（选区未覆盖整行宽度时为 False）
    columns: 终端实际列宽
    strip_soft_trailing: 拼接前去掉软换行行的尾部空格。Windows ConPTY 重绘
        时会把行尾清空区写成真实空格、且不发换行地连续绘制——pyte 在右缘
        自动折行，把应用层折行的行误标成「终端软换行」，行尾填充空格随
        无缝拼接混进结果（复制跨行 URL 平白多出几个空格）。ConPTY 之下
        软换行行的尾部空格不可信，一律剥掉。Unix pty 不开启：真实软换行
        行的尾部空格是内容（字符串内多个空格恰跨折行处），必须保留。

    换行类型（look-ahead 决定）：
        0=硬换行（保留换行）；1=终端软换行（无缝拼接）；
        2=应用层「散文」折行（按词边界折 → 拼接时补回词间空格）；
        3=应用层「token 截断」折行（长 token 被强行从中间截断，如 URL/路径/哈希 →
          必须无缝直接拼接，绝不能补空格，否则会把 URL 拦腰插入空格导致登录失效）
    """
    rows = list(rows)
    n = len(rows)
    if n == 0:
        return ""

    if strip_soft_trailing:
        rows = [(t.rstrip(), s, lc, sp, ch) if s else (t, s, lc, sp, ch)
                for (t, s, lc, sp, ch) in rows]

    # 估算每行所在「连续非软换行行块」的应用层折行宽度（块内最大内容列）。
    # 这样即使应用（如 Claude Code 的盒子边距）按比终端更窄的宽度折行，也能正确识别
    # 散文折行点，而不是依赖「填满到终端右边缘」这个会被边距破坏的假设。
    run_max = [r[2] for r in rows]
    i = 0
    while i < n:
        if rows[i][1]:  # 软换行行不参与应用层折行块
            i += 1
            continue
        j = i
        while j < n and not rows[j][1]:
            j += 1
        block_max = max((rows[k][2] for k in range(i, j)), default=-1)
        for k in range(i, j):
            run_max[k] = block_max
        i = j

    # 判断每行是「被宽度折断的续行」（拼接）还是「故意的新行」（保留换行）：
    #  · 无内部空格的长 token（URL/路径/哈希）：只有写满（或因行尾放不下宽字符而差
    #    1 列写满）终端实际宽度才视为被截断 —— 用 columns 而非块内 run_max 判断，
    #    否则块内最长的那行恒满足「last_col >= run_max - 2」，会把 ls -1 输出的
    #    多条长短不一的独立路径误并成一行。
    #  · 散文：用经典 unwrap 判据——下一行第一个词在本行放不下 → 是被折断的续行
    #    （折行宽度用本块实测的 run_max，兼容应用按盒子边距比终端更窄折行的情况）。
    #  · 列表项/标题等结构行（_LIST_MARKER_RE）即使上一行填满也强制保留换行。
    threshold_low = max(8, int(columns * 0.40))
    resolved = []
    for i, (text, is_soft, last_col, spaceless, can_h) in enumerate(rows):
        if i == n - 1:
            resolved.append((text, 0))
            continue
        if is_soft:
            resolved.append((text, 1))
            continue
        wrap_type = 0
        nxt = rows[i + 1][0] if can_h else ''
        next_core = nxt.strip()
        if (can_h and last_col >= 0 and next_core
                and run_max[i] >= threshold_low
                and not _LIST_MARKER_RE.match(next_core)):
            # 折行宽度参照：整行单 token（spaceless，如 ls -1 的独立路径）必须真的写满
            # 终端右边缘(columns)才算被宽度截断，避免把多条独立路径误并；带前缀的行
            # （如 "cd <path>"、"docker push <url>"）用本块实测折行宽 run_max —— 兼容
            # 应用按比终端更窄的盒子边距折行，以及「Claude Code 折完行后终端又被拉宽
            # （弹簧展宽等）」导致行尾早于终端右缘的常见情形。
            wrap_edge = columns if spaceless else run_max[i]
            filled_to_edge = last_col >= wrap_edge - 2
            # 断点两侧各自的连续非空白串（拼起来即被截断的 token 候选）
            tail = text[text.rfind(' ') + 1:] if text else ''      # 本行末尾连续非空白
            head = next_core.split(None, 1)[0] if next_core else ''  # 下一行开头连续非空白
            # 用显示列宽（CJK 全角占 2 列）与 wrap_edge/threshold_low 比较，
            # 否则 20 个全角字符顶满 40 列的行会因 len==20 够不着门槛而漏判。
            combined = sum(max(wcwidth(ch), 0) for ch in tail + head)
            has_sep = bool(_TOKEN_SEP_RE.search(tail) or _TOKEN_SEP_RE.search(head))
            # 续行是否带前导缩进：Claude Code 等把列表项 "- <url>" 折行时，续行会缩进
            # 对齐到列表内容起点（悬挂缩进），此时 nxt[0] 是空格。
            next_indented = bool(nxt) and nxt[0].isspace()
            # 断点是否把一个连续 token 从中间截断：填满到折行宽、本行结尾非空白，且拼起来
            # 确是「一个较长的 token」——含强分隔符(URL/路径/命令)且够长(≥threshold_low)，
            # 或长到铺满整行宽(即便无分隔符也必是被宽度截断)。满足则无缝拼接、绝不补空格/
            # 换行。修复 "docker push <长URL>" / "cd <长路径>" / 列表项 "- 下载源:<长URL>"
            # 折行(续行带悬挂缩进)这类被误当散文折行而插入空格的老问题。
            # 关键：带悬挂缩进的续行只有在断点确含强分隔符(URL/路径)时才认定为 token 续行
            # ——否则(缩进的散文续行、无分隔符)照旧按散文处理，避免把缩进散文误粘成一坨。
            boundary_mid_token = (
                filled_to_edge
                and text[-1:] and not text[-1].isspace()
                and (not next_indented or has_sep)
                and ((has_sep and combined >= threshold_low)
                     or combined >= wrap_edge)
            )
            if boundary_mid_token:
                wrap_type = 3
            elif not spaceless:
                # 散文按词边界折行：下一行首词在本行放不下（本行未填满到边缘，留有
                # 空位）→ 是被折断的续行，拼接时按需补回一个词间空格。
                first_word = next_core.split(None, 1)[0]
                gap = run_max[i] - last_col  # 本行尾部到折行宽度的剩余列数
                if gap <= len(first_word) + 1:
                    wrap_type = 2
        resolved.append((text, wrap_type))

    # 组合结果
    result = []
    for i, (text, wrap_type) in enumerate(resolved):
        if i > 0:
            prev_wrap = resolved[i - 1][1]
            if prev_wrap == 1:
                pass  # 终端软换行：无缝拼接，保留原字符（含真实的前导空格）
            elif prev_wrap == 3:
                # 应用层 token 截断折行：续行可能带对齐悬挂缩进（列表项 "- <url>" 折行
                # 对齐），去掉后无缝拼接，绝不补空格——否则 URL 里会凭空多出一个空格。
                text = text.lstrip()
            elif prev_wrap == 2:
                # 散文续行：把对齐缩进折叠掉，必要时补回一个词间空格
                text = text.lstrip()
                if result and text:
                    last_ch = result[-1][-1] if result[-1] else ''
                    first_ch = text[0]
                    if last_ch and first_ch and _need_boundary_space(last_ch, first_ch):
                        result.append(' ')
            else:
                result.append('\n')
        result.append(text)

    # 去除整体结果的尾部空白：软换行行会保留尾部空格（它们是续行的真实内容），
    # 但整段末尾的空白没有任何意义，且会污染剪贴板（例如复制登录 URL / 授权码时
    # 末尾多出一个空格导致粘贴失效）。
    return ''.join(result).rstrip()


# ==================== 终端 resize reflow（重排）====================
#
# pyte 0.8 原生 resize 在列数变小时对每行 line.pop(x) 永久删除超宽内容，
# 拉宽后内容丢失；行数变化时只在底部加空行/从顶部删行，不与历史交互。
# 下面的纯函数（无 Qt/pyte Screen 依赖，便于直接单测）实现 iTerm2 式 reflow：
# 把屏幕+历史中的软换行物理行拼回逻辑行，按新宽度重新折行。

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QApplication, QMenu, QLineEdit,
    QHBoxLayout, QPushButton, QLabel, QFileDialog, QSizePolicy, QScrollBar
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer, QEvent, QPoint, QUrl, QMimeData
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QPen, QFontMetrics, QFontMetricsF, QFontInfo,
    QKeyEvent, QResizeEvent, QShortcut, QKeySequence,
    QMouseEvent, QAction, QDesktopServices, QDragEnterEvent, QDropEvent,
    QPixmap, QImage
)


class _MarkScrollBar(QScrollBar):
    """终端右缘滚动条 + 命令标记刻度。

    marks_fraction_provider 返回 0..1 的比例列表（命令输入行在全部内容中的
    位置），在槽轨道上画细刻度线，配合 Alt+↑/↓ 的命令间跳转做视觉锚点。
    """

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.marks_fraction_provider = None

    def paintEvent(self, event):
        super().paintEvent(event)
        provider = self.marks_fraction_provider
        if provider is None:
            return
        try:
            fractions = provider()
        except Exception:
            return
        if not fractions:
            return
        painter = QPainter(self)
        color = QColor(120, 180, 255, 170)
        h = self.height()
        w = self.width()
        for f in fractions:
            y = 1 + int(f * (h - 3))
            painter.fillRect(2, y, w - 4, 2, color)
        painter.end()


class TerminalSignalBridge(QThread):
    """信号桥：将后端回调转换为 Qt 信号（线程安全）"""
    output_received = pyqtSignal(bytes)
    process_finished = pyqtSignal(int)


class TerminalWidget(TerminalRenderMixin, QWidget):
    """
    完整终端模拟器控件
    使用pyte进行终端转义序列处理
    """

    # 判断应用层软换行拼接时是否需要在边界处插入空格
    # （实现在模块级，供 merge_extracted_lines 纯函数共用；保留静态方法别名，
    #  test_terminal_text_extract 经 widget 属性直测）
    _need_boundary_space = staticmethod(_need_boundary_space)

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
    rename_split_requested = pyqtSignal()  # 请求重命名当前分屏（显示/修改顶部标题栏）
    attention_requested = pyqtSignal()  # 疑似本轮命令/Claude 执行完毕（输出停顿），请求导航提醒
    interaction_requested = pyqtSignal()  # 终端响铃（BEL）：程序正在等待用户操作（如 Claude 确认提示）
    alert_matched = pyqtSignal(str)  # 输出命中提醒规则（参数=命中的模式文本）
    _output_activity = pyqtSignal(int)  # 一批输出已处理（参数=字节数）：在 GUI 线程重置空闲计时器
    _history_grew = pyqtSignal(int)  # feed 新增历史行数：读取线程 marshal 回 GUI 线程做回滚位置补偿
    scrollback_pressure_changed = pyqtSignal(int)  # 回滚历史"卡顿压力"等级变化（0 无 / 1 琥珀 / 2 红）
    _reflow_finished = pyqtSignal(int, float)  # 后台 reflow 完成（行数, 毫秒）：worker 线程 marshal 回 GUI 收尾

    # scrollback 压力：用"预测 reflow 耗时 = 历史行数 × 实测每行毫秒"判定，超阈值才提示，
    # 因此内容窄 / 机器快时即使顶到上限也不会误亮（避免按行数比例造成的常驻红点）。
    _SCROLLBACK_AMBER_MS = 80    # 预测 reflow ≥ 80ms → 琥珀（开始有感）
    _SCROLLBACK_RED_MS = 200     # 预测 reflow ≥ 200ms → 红（明显卡，建议清空）

    # 大历史 reflow 移出 GUI 线程：预测耗时 ≥ 阈值时提交到共享后台线程
    # （与 feed 用同一把 _screen_lock 互斥），期间 paintEvent 沿用旧缓存
    # 贴图不阻塞；完成后经 _reflow_finished 信号回 GUI 线程收尾。
    # 低于阈值保持原同步路径（几 ms 内完成，语义不变、测试友好）。
    # 共享单 worker 还顺带把"全局缩放对多个分屏各来一次 reflow"串行到
    # 后台排队执行，GUI 线程全程不冻结。
    # STELLAR_SYNC_REFLOW=1 → 强制全部同步（排障回退开关）。
    _ASYNC_REFLOW_MIN_MS = 30.0
    _ASYNC_REFLOW_ENABLED = os.environ.get('STELLAR_SYNC_REFLOW', '0') != '1'
    _reflow_executor = None            # 全类共享的单线程 executor（懒创建）
    _reflow_executor_lock = threading.Lock()

    # 是否在后端读取线程上直接解析（feed），而不是 marshal 到 GUI 线程。
    # 默认 True（2026-07 起）：多个终端的 pyte 解析在各自读取线程并行，
    # 不再争抢唯一的 GUI 线程——多窗口 tmux/远程高频输出卡顿的根治。
    # 前提是所有 screen 读取点持 _screen_lock、QTimer 类操作经信号回 GUI，
    # 已经完整并发审计 + 压测（见 tests/test_parse_reader_thread.py、
    # tests/test_screen_lock_concurrency.py）。
    # 环境变量可强制两个方向（优先级最高，便于 A/B 回退）：
    #   STELLAR_PARSE_OFF_GUI=0 → 强制关（回到 GUI 线程解析）
    #   STELLAR_PARSE_OFF_GUI=1 → 强制开
    PARSE_ON_READER_THREAD = os.environ.get('STELLAR_PARSE_OFF_GUI', '1') != '0'

    # 输出规则提醒（进程级共享，由 MainWindow 从配置载入）：输出命中任一
    # 正则 → alert_matched 信号（标签橙点 + 导航提醒）。典型用途：后台
    # 标签里 claude 跑测试崩了（Traceback/FAILED），人在别的 tab 不知道。
    ALERT_RULES_ENABLED = True
    _ALERT_COMPILED = []          # [(pattern_str, compiled_regex)]
    _ALERT_MUTE_SECONDS = 30      # 命中后静默窗口，避免同一事故连环提醒

    @classmethod
    def set_output_alert_rules(cls, patterns, enabled: bool):
        """编译并安装提醒规则（对所有终端立即生效）。非法正则跳过并记日志。"""
        compiled = []
        for p in patterns or []:
            p = (p or '').strip()
            if not p:
                continue
            try:
                compiled.append((p, re.compile(p)))
            except re.error as e:
                logger.warning("invalid alert pattern %r skipped: %s", p, e)
        cls._ALERT_COMPILED = compiled
        cls.ALERT_RULES_ENABLED = bool(enabled)

    # 每个终端的 scrollback（历史回滚）行数上限。pyte 用 deque(maxlen) 在构造时定死，
    # 所以只对「之后新建」的终端生效。值越大越占内存、resize reflow 越慢（O(历史行数)）。
    # 进程级共享：由 MainWindow 从配置读出后覆盖；可在设置里改。
    SCROLLBACK_LINES = 5000

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

    # 复制拼接时用于识别「新的结构行」（列表项/有序列表/标题/引用）。
    # 这类行即使上一行被填满，也不应被并入上一行，必须保留换行。
    # （实现移至模块级，供 merge_extracted_lines 纯函数共用；此处保留类属性别名）
    _LIST_MARKER_RE = _LIST_MARKER_RE

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

    # 分屏自定义标题栏高度（仅在用户设置了名称后才占用此高度，默认 0）
    HEADER_HEIGHT = 22

    # 右缘覆盖式垂直滚动条宽度（像素）
    SCROLLBAR_WIDTH = 10

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

        # 分屏自定义标签：None 表示未命名（默认不显示标题栏，不占用顶部空间）
        self._split_label: Optional[str] = None
        self._header_h = 0           # 顶部标题栏当前占用的像素高度（0 = 隐藏）
        self._header_bar = None      # 标题栏 overlay 控件（首次命名时才创建）
        self._header_label = None

        # 终端尺寸（限制行数减少空白）
        self.term_cols = 120
        self.term_rows = 30

        # 屏幕状态锁：保护 self.screen / self.stream 的并发访问。当前所有 pyte
        # 解析(feed)与多数读取都在 GUI 线程，唯一的跨线程读取者是 openai_server 的
        # HTTP worker（读 screen.display/buffer）——它过去无锁，与 GUI 的 feed 竞争。
        # 用可重入锁(RLock)：同线程内嵌套读取(render→helper)不会自锁；后续把 feed
        # 挪到读取线程时，这把锁即提供真正的互斥。
        self._screen_lock = threading.RLock()

        # scrollback 压力指示：实测每行 reflow 毫秒（首次 resize 前用保守默认），
        # 与当前严重度等级。预测耗时 = 历史行数 × _reflow_ms_per_line。
        self._reflow_ms_per_line = 0.04
        # 异步 reflow 状态（详见类属性 _ASYNC_REFLOW_* 的注释）
        self._reflow_inflight = False   # 后台 worker 是否在跑（GUI 线程独占读写）
        self._reflow_scaling = False    # paintEvent 是否走"旧缓存贴图"模式
        self._pending_shrink_rows = False  # 行数变小待处理（收尾时提示符归顶）
        self._reflow_finished.connect(self._on_reflow_finished)
        self._scrollback_level = 0
        self._scrollback_dot_btn = None  # 右上角"清空 scrollback"指示点（懒创建）
        self._scrollback_dot_shown_level = 0  # 指示点当前已渲染的等级（避免每帧重设样式）

        # pyte终端模拟 - 历史记录限制（默认 5000 行，可在设置里改；越大越占内存、
        # resize reflow 越慢）。使用兼容性修复类，解决新版 pyte 的参数问题。
        self.screen = CompatibleHistoryScreen(self.term_cols, self.term_rows,
                                              history=TerminalWidget.SCROLLBACK_LINES)
        self.screen.set_mode(pyte.modes.LNM)  # 换行模式
        self.screen.set_mode(pyte.modes.DECAWM)  # 自动换行模式
        self.screen.set_mode(pyte.modes.DECTCEM)  # 显示光标

        # 恢复正常的pyte行为 - 不再干预清除操作
        # Claude Code的TUI需要正常的清除功能才能正确显示
        self.stream = pyte.Stream(self.screen)
        # pyte 在 UTF-8 模式（默认）下会刻意忽略 DEC 特殊图形字符集
        # （ESC(0 / ESC)0 指定与 SI/SO 切换），而 ncurses 在
        # TERM=xterm-256color 下画边框/线条（curses ACS_*、stdscr.border()）
        # 全靠这套序列 → 表现为边框显示成字母 lqkxmj。真实终端（xterm/iTerm2）
        # 在 UTF-8 下也支持该字符集。我们喂给 Stream 的本来就是已解码的 str
        # （解码在 _on_output 之前完成），关掉 use_utf8 只是放行字符集处理，
        # 不影响任何解码行为。
        self.stream.use_utf8 = False

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
        # 保护 _output_buffer：读取线程 append、GUI 定时器换出，二者需互斥，
        # 否则 join 与 clear 之间 append 的块会被 clear 抹掉 → 录制丢字
        self._output_buffer_lock = threading.Lock()
        self._output_buffer_timer = QTimer()
        self._output_buffer_timer.timeout.connect(self._flush_output_buffer)
        self._output_buffer_timer.start(100)  # 每100ms刷新一次

        # Resize 防抖定时器 - 避免拖拽时频繁重绘导致闪烁
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._do_resize_update)
        self._resize_pending = False  # 是否有待处理的 resize
        self._last_resize_size = None  # 记录最后的窗口大小
        # 弹簧/动画等「连续 resize」期间：只把旧缓存缩放贴图，不每帧重建整屏，
        # 避免全屏字符重绘造成的卡顿。动画结束由 set_fast_resize(False) 触发一次重建。
        self._fast_resize = False

        # 「执行完毕」检测：输出持续一阵后停顿 ≥ IDLE_MS 视为本轮命令/Claude 执行完毕。
        # Claude 思考/工作时 spinner 会持续刷新输出 → 计时器不断重置，不会误判；
        # 真正停下来才触发。响铃（BEL）则即时触发（见 _on_output）。
        self._activity_idle_timer = QTimer()
        self._activity_idle_timer.setSingleShot(True)
        self._activity_idle_timer.timeout.connect(self._on_activity_settled)
        # AutoConnection：同线程发射→直连；读取线程发射→自动排队到 GUI 线程，
        # 保证 _activity_idle_timer.start() 始终在 GUI 线程执行。
        self._output_activity.connect(self._on_output_activity)
        # scroll_offset 只由 GUI 线程读写：读取线程 feed 新增历史行时经此信号
        # marshal 回 GUI 做回滚位置补偿，避免两线程并发改 scroll_offset
        self._history_grew.connect(self._on_history_grew)
        self._had_output_activity = False
        self._activity_bytes = 0
        self._ACTIVITY_IDLE_MS = 1500
        # 是否有"待结算"的用户命令（提交过回车后置位，提醒发出时消费）。
        # 没有它时输出停顿不算执行完毕，避免 TUI 闲置动画产生假提醒。
        self._pending_user_command = False
        # 上次检测到的交互提示签名：同一个确认框在 TUI 周期性重绘下会反复触发
        # 停顿结算，凭签名去重，只对"新出现"的提示发 interaction_requested。
        self._last_interaction_sig = None

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
        self._scroll_accum = 0.0  # 滚轮滚动的小数累加器（用于半行滚动）
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
        # 普通 dict + 满员整体清空：这些键空间实际都很小（调色板颜色、
        # 屏上出现过的字符），远到不了上限；LRU 的 move_to_end 反而是
        # 渲染热循环里每次命中的白付开销（profile 实测占比可观）
        self._color_cache = {}
        # 宽字符缓存（避免重复调用 unicodedata.east_asian_width）
        self._wide_char_cache = {}
        # 可见性调整后的颜色缓存
        self._visible_color_cache = {}
        # 缓存上限（超限整体清空，护栏用，正常永远打不到）
        self._WIDE_CHAR_CACHE_MAX = 8192
        self._COLOR_CACHE_MAX = 4096

        # 增量重绘状态：上次整屏渲染的几何/内容快照 + 主题代号。
        # 状态完全一致且在底部（无回滚）时，只重绘 pyte 标脏的行。
        self._last_render_state = None
        self._render_epoch = 0
        self._scroll_blit_hits = 0  # 回看滚动走 pixmap 平移复用的次数（调试/测试用）

        # 实例级别的终端颜色（支持主题切换）
        self._current_colors = self.DEFAULT_COLORS.copy()
        self._current_bright_colors = self.BRIGHT_COLORS.copy()

        # 鼠标选择相关（使用绝对行号，支持跨页选择）
        self._selection_start = None  # (absolute_row, col) 选择起点
        self._selection_end = None    # (absolute_row, col) 选择终点
        self._is_selecting = False    # 是否正在选择
        self._shift_extend_click = False  # 本次按下是否为 Shift+点击扩展选区
        self._shift_extend_pending = False  # Shift+按下后待定：拖动→新建，纯点击→扩展
        self._shift_press_cell = None     # Shift+按下时的绝对格（拖动时作为新选区起点）
        self._select_all_mode = False  # 是否为全选模式（包括历史记录）
        self._selection_color = QColor(100, 149, 237, 100)  # 选区高亮颜色（半透明蓝色）
        self._cursor_color = QColor(200, 200, 200, 180)  # 光标颜色

        # 设置鼠标光标为文本选择样式
        self.setCursor(Qt.CursorShape.IBeamCursor)
        # 启用鼠标跟踪，使按住修饰键悬停在 URL 上时能显示手型光标
        self.setMouseTracking(True)
        self._hover_cursor_on = False  # 当前是否处于 URL 手型悬停状态
        # 打开 URL 的修饰键：macOS 上 ControlModifier 对应 ⌘(Cmd)，再额外支持 Alt/Option
        self._URL_CLICK_MODIFIERS = (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
        )

        # 搜索相关
        self._search_bar = None
        self._search_matches = []  # [(abs_row, col, col_span), ...] 绝对行号（含历史）
        self._current_match_index = -1
        self._search_highlight_color = QColor(255, 255, 0, 150)  # 黄色高亮
        self._search_current_color = QColor(255, 165, 0, 180)  # 当前匹配项橙色
        # 全历史搜索：输入防抖 + 历史行文本提取缓存
        self._search_pending_text = ""
        self._search_debounce_timer = QTimer()
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.timeout.connect(self._perform_search)
        # 历史行提取缓存：id(line) -> (line_ref, text, col_map)
        # 值里保留 line 引用以「钉住」对象，保证缓存有效期内 id 不会被复用
        self._search_line_cache = OrderedDict()
        self._SEARCH_LINE_CACHE_MAX = 30000

        # 命令标记：用户按 Enter 提交命令时记录的行位置（累计行号坐标，
        # 不随历史 deque 丢头失效；换算见 _current_mark_positions）。
        # Alt+↑/↓ 在标记间跳转，滚动条画刻度。
        self._command_marks = []
        self._COMMAND_MARKS_MAX = 500

        # 输出规则提醒：跨块尾缓冲（模式可能被读取分块切开）+ 静默截止时刻。
        # 只由输出线程读写（单读取线程），按键解除静默来自 GUI 线程——竞态无害
        self._alert_tail = ''
        self._alert_muted_until = 0.0

        # 垂直滚动条：覆盖在右缘的子控件，不参与布局（终端为自绘 widget）
        self._scrollbar_syncing = False  # 防止 setValue/setRange 触发信号回环
        self._v_scrollbar = _MarkScrollBar(Qt.Orientation.Vertical, self)
        self._v_scrollbar.marks_fraction_provider = self._mark_fractions
        self._v_scrollbar.setFixedWidth(self.SCROLLBAR_WIDTH)
        self._v_scrollbar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._v_scrollbar.hide()
        self._v_scrollbar.valueChanged.connect(self._on_scrollbar_value_changed)
        self._apply_scrollbar_style()

        # 预编辑文本颜色缓存（避免 paintEvent 中重复创建）
        self._preedit_bg_color = QColor(60, 70, 90, 220)
        self._preedit_fg_color = QColor(255, 255, 100)
        self._preedit_underline_pen = QPen(QColor(255, 255, 100), 2)

        # 双击/三击选择
        self._click_count = 0
        self._last_click_time = 0
        self._last_click_pos = None
        self._double_click_interval = 0.4  # 400ms

        # 已清理标记：cleanup() 后置 True，鼠标事件处理器据此提前返回
        self._cleaned_up = False

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
        # 是否把鼠标「点击/拖动」转发给开启鼠标上报的程序（如 Claude Code 选项菜单、
        # lazygit、fzf、htop）。默认关闭：避免在 Claude Code 里误点选项。滚轮上报不受此
        # 开关影响（另有单独路径）。由主窗口按用户设置下发。
        self._mouse_click_forward_enabled = False

        # 快速命令提供者回调（由主窗口设置，返回预设列表）
        self.quick_commands_provider = None

        # 本地快速命令提供者回调（由主窗口设置，返回本地预设列表）
        self.local_quick_commands_provider = None



    def showEvent(self, event):
        """窗口显示时重新计算字符尺寸"""
        super().showEvent(event)
        # 窗口显示后重新计算，这时DPI等信息更准确
        self._calculate_char_size()
        self._update_terminal_size()
        # 分屏时新窗格可能在 splitter 布局完成前就收到首个 paintEvent（此时尺寸仍为 0，
        # paintEvent 会提前 return 而不填背景）；由于设了 WA_OpaquePaintEvent，未填的区域
        # 会透出桌面。这里在布局稳定后再强制重绘一次兜底——只是一次额外 paint，绝对安全。
        QTimer.singleShot(0, self.update)

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
            # 内容增长可能改变 scrollback 压力等级（节流到刷新频率，开销仅一次乘法）
            self._update_scrollback_level()

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
            logger.debug(
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
        # 使用实际窗口计算的行数（顶部标题栏会额外占用 _header_h）
        available_height = self.height() - p2 - self._header_h
        # 允许更小的行数以支持分屏（最小 5 行）
        new_rows = max(5, int(available_height / self.char_height))

        if new_cols != self.term_cols or new_rows != self.term_rows:
            old_cols, old_rows = self.term_cols, self.term_rows
            self.term_cols = new_cols
            self.term_rows = new_rows
            if new_rows < old_rows:
                # 缩小后的"提示符归顶"整理延迟到 reflow 收尾（同步/异步共用）
                self._pending_shrink_rows = True

            # 参与 reflow 的行数（粗略）。用真实历史长度而非 _get_history_count：
            # 备用屏幕(tmux/vim)期间 resize 同样会 reflow 被保存的主屏+历史
            reflow_lines = self._raw_history_count() + old_rows
            logger.debug(f"[Terminal] Size: {old_cols}x{old_rows} -> {new_cols}x{new_rows} (widget: {self.width()}x{self.height()}, char_w: {self.char_width:.1f})")

            if self._reflow_inflight:
                # 后台 reflow 在途：目标尺寸（term_cols/rows）已更新，worker
                # 完成后发现 screen 尺寸仍不匹配会自动续跑到最新目标
                return
            predicted_ms = reflow_lines * self._reflow_ms_per_line
            if self._ASYNC_REFLOW_ENABLED and predicted_ms >= self._ASYNC_REFLOW_MIN_MS:
                self._start_async_reflow(reflow_lines)
                return

            # 同步路径（小缓冲，几 ms 内完成）
            # 重新创建screen（HistoryScreen的resize参数顺序是lines, columns）
            # reflow 整屏 mutate（重建行对象/历史），与跨线程读取者互斥
            with self._screen_lock:
                _t0 = time.perf_counter()
                self.screen.resize(self.term_rows, self.term_cols)
                reflow_ms = (time.perf_counter() - _t0) * 1000.0
            self._finish_reflow(reflow_lines, reflow_ms)

    def _finish_reflow(self, reflow_lines: int, reflow_ms: float):
        """reflow 完成后的 GUI 线程收尾（同步路径与异步 worker 共用）。"""
        # 用本次实测校准"每行毫秒"（EMA）；样本够大才更新，避免小缓冲噪声
        if reflow_lines >= 300 and reflow_ms > 0:
            per = reflow_ms / reflow_lines
            self._reflow_ms_per_line = 0.6 * self._reflow_ms_per_line + 0.4 * per
        self._update_scrollback_level()

        # reflow 重建了行对象、改变了历史长度：
        # - 渲染快照失配 → epoch 自增，保证下次走整屏重绘（增量重绘按
        #   _last_render_state 比对，行对象内容变化不会反映在 screen.dirty 里）
        # - 全历史搜索缓存按 id(line) 钉住旧行对象，全部失效，主动清空
        # - 回滚浏览位置 clamp 到新历史范围
        self._render_epoch += 1
        self._search_line_cache.clear()
        if self.scroll_offset > 0:
            self.scroll_offset = min(self.scroll_offset, self._get_history_count())

        # 缩小后若光标停在空闲提示符上、上方却残留一堆空行（常见于 claude-code 等
        # 退出后清屏但把光标留在低处的情形），把这些空行去掉，让提示符回到顶部。
        if self._pending_shrink_rows:
            self._pending_shrink_rows = False
            self._reflow_idle_prompt_to_top()

        # 更新PTY大小（子进程会收到 SIGWINCH，可能会发送完整重绘）
        self._update_pty_size()

        # 不主动 erase 可见 buffer：让 pyte 自然处理（保留旧内容直到子进程重写）。
        # 这样即使子进程不响应 SIGWINCH（如 bash 在提示符空闲、进程已退出等），
        # 也不会出现空白终端。少数 TUI 应用使用 CUF 增量重绘可能短暂出现"重影"，
        # 但这种情况极少且很快被新重绘覆盖，远好于内容完全消失。

        self._invalidate_render_cache()

    @classmethod
    def _get_reflow_executor(cls):
        """全类共享的 reflow 单线程 executor（懒创建）。

        单 worker：多个终端（如全局缩放时的各分屏）的 reflow 串行排队，
        避免并发 reflow 争抢 CPU；每个任务各自持有目标 widget 的
        _screen_lock，互不干扰。"""
        with cls._reflow_executor_lock:
            if cls._reflow_executor is None:
                from concurrent.futures import ThreadPoolExecutor
                cls._reflow_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix='reflow')
            return cls._reflow_executor

    def _start_async_reflow(self, reflow_lines: int):
        """把 screen.resize 提交到后台线程；期间 paintEvent 走旧缓存贴图。"""
        self._reflow_inflight = True
        self._reflow_scaling = True
        TerminalWidget._get_reflow_executor().submit(
            self._reflow_worker, reflow_lines)

    def _reflow_worker(self, reflow_lines: int):
        """后台线程：持 _screen_lock 执行 reflow（与读取线程 feed 互斥）。

        目标尺寸在锁内读 term_rows/term_cols（GUI 线程可能已更新到更新的
        目标——直接 reflow 到最新值，减少续跑轮次）。"""
        reflow_ms = 0.0
        try:
            with self._screen_lock:
                rows, cols = self.term_rows, self.term_cols
                if (self.screen.lines, self.screen.columns) != (rows, cols):
                    _t0 = time.perf_counter()
                    self.screen.resize(rows, cols)
                    reflow_ms = (time.perf_counter() - _t0) * 1000.0
        except Exception:
            logger.exception("[Terminal] async reflow failed")
        finally:
            try:
                self._reflow_finished.emit(reflow_lines, reflow_ms)
            except RuntimeError:
                pass  # widget 已销毁（C++ 对象没了），静默丢弃

    def _on_reflow_finished(self, reflow_lines: int, reflow_ms: float):
        """GUI 线程：后台 reflow 完成。尺寸已过期则续跑，否则收尾。"""
        self._reflow_inflight = False
        if (self.screen.lines, self.screen.columns) != (self.term_rows, self.term_cols):
            # worker 执行期间用户又改了尺寸 → 续跑（保持贴图模式，不闪烁）
            self._start_async_reflow(self._raw_history_count() + self.screen.lines)
            return
        self._reflow_scaling = False
        self._finish_reflow(reflow_lines, reflow_ms)

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
        # 整段 mutate 在锁内一次完成，避免跨线程读取者看到删行/光标移动的中间态。
        cx = screen.cursor.x
        with self._screen_lock:
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
            logger.warning(f"[Terminal] Error updating PTY size: {e}")

    def _write_to_backend(self, data: bytes) -> bool:
        """写入数据到终端后端"""
        if self._backend is None:
            return False
        try:
            ok = self._backend.write(data)
        except Exception:
            return False
        # 用户提交了命令（回车/换行）→ 此后第一次输出停顿才视为"执行完毕"。
        # 自动应答（设备属性、光标位置上报等）不含换行，不会误触发；
        # 这样 claude code 等 TUI 闲置时的周期性重绘不会再点亮导航绿点。
        if ok and (b'\r' in data or b'\n' in data):
            self._pending_user_command = True
        return ok

    def resizeEvent(self, event: QResizeEvent):
        """窗口大小变化 - 使用防抖机制避免频繁重绘导致闪烁"""
        super().resizeEvent(event)

        # 重新定位搜索栏（这个可以立即执行，很轻量）
        if hasattr(self, '_search_bar') and self._search_bar:
            self._position_search_bar()

        # 重新定位右缘滚动条（覆盖式子控件，轻量）
        self._position_scrollbar()

        # 重新定位 scrollback 指示点
        if self._scrollback_dot_btn is not None and self._scrollback_dot_btn.isVisible():
            self._position_scrollback_dot()

        # 重新定位分屏标题栏
        if self._header_bar is not None and self._header_h:
            self._position_header_bar()

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

    def set_fast_resize(self, on: bool):
        """开启/关闭「快速 resize」模式。

        开启时 paintEvent 不再每帧重建整屏，而是把上一帧的缓存缩放贴到当前尺寸
        （便宜很多），用于弹簧/动画等连续 resize 场景，避免卡顿。关闭时立即让缓存
        失效并重绘一次，按最终尺寸渲染出清晰文本。期间 PTY 尺寸不变（SIGWINCH 由
        150ms 防抖在动画结束后才发），故缩放仅为临时显示、不影响内容。
        """
        on = bool(on)
        if on == self._fast_resize:
            return
        self._fast_resize = on
        if not on:
            self._invalidate_render_cache()

    def flush_resize(self):
        """立即处理待定的 resize，跳过防抖定时器。

        注意：大历史时网格重算内部仍可能转入后台线程（异步 reflow），
        本方法只保证"立刻开始"，不保证返回时 screen 已收敛。"""
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

        # 设置后端回调（在后端读取线程上调用）
        def on_output(data: bytes):
            if TerminalWidget.PARSE_ON_READER_THREAD:
                # 直接在读取线程解析：pyte feed 不再占用 GUI 线程。screen 读写由
                # _screen_lock 互斥，QTimer 类操作经 _output_activity 信号回 GUI。
                self._on_output(data)
            else:
                # 原行为：marshal 到 GUI 线程解析。
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
            logger.error("[Terminal] Failed to start process")
            self._backend = None
            self._signal_bridge = None

    def _on_output(self, data: bytes):
        """处理输出数据"""
        try:
            text = self._utf8_decoder.decode(data)

            # ssh 密码提示一次性自动回填（Remote 面板里已输入过该主机密码）
            if getattr(self, '_pending_ssh_password', None):
                self._maybe_autofill_password(text)

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

            # 终端响铃（BEL）：Claude Code / CLI 完成一轮或需要注意时常会响铃。
            # 此时 OSC 序列里作为终止符的 BEL 已被上面剥掉，残留的 \x07 即真正的响铃。
            # 响铃视为"需要用户操作"→ 发 interaction_requested，即使是正在看的
            # 活动终端也点亮导航绿点（用户要求：需要指令时必提示，避免错过）。
            if '\x07' in text:
                self.interaction_requested.emit()

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

            # 输出规则提醒：命中 Traceback/FAILED 等模式 → 标签橙点+导航提醒
            if text and self.ALERT_RULES_ENABLED and self._ALERT_COMPILED:
                try:
                    self._scan_output_alerts(text)
                except Exception:
                    logger.debug("_scan_output_alerts: suppressed exception", exc_info=True)

            # 诊断捕获：记录过滤后的文本
            if self._debug_capture_enabled and self._debug_capture_file:
                self._debug_capture_file.write(f"FILTERED ({len(text)} chars):\n")
                self._debug_capture_file.write(repr(text) + '\n')
                self._debug_capture_file.flush()

            # feed 及其直接依赖的历史行数读取在锁内完成，保证与跨线程读取者
            # （openai_server worker）互斥，不会读到 mutate 到一半的屏幕。
            with self._screen_lock:
                # 记录 feed 前的历史行数，用于滚动位置稳定化
                old_total = self.screen._total_history_lines

                # 送入pyte处理（带错误恢复）
                try:
                    self.stream.feed(text)
                except Exception as feed_err:
                    # pyte 处理异常时尝试逐字符恢复，避免丢失整块数据
                    logger.warning(f"[Terminal] stream.feed error: {feed_err}, attempting char-by-char recovery")
                    if self._debug_capture_enabled and self._debug_capture_file:
                        self._debug_capture_file.write(f"FEED ERROR: {feed_err}\n")
                    for ch in text:
                        try:
                            self.stream.feed(ch)
                        except Exception:
                            logger.debug("_on_output: suppressed exception", exc_info=True)

                new_total = self.screen._total_history_lines
                lines_added = new_total - old_total

            # 滚动位置稳定化：用户回滚浏览时新输出不应让显示内容跳动。补偿逻辑
            # 改 scroll_offset，而 scroll_offset 由 GUI 线程独占读写——读取线程
            # 经信号 marshal 回 GUI 执行（同线程直连、跨线程排队），避免并发改。
            # 常见情形（在底部 scroll_offset==0）不发信号，零开销。
            if lines_added > 0 and self.scroll_offset > 0:
                self._history_grew.emit(lines_added)

            # 标记内容已变化，让定时器统一处理重绘（避免高频输出时的重绘风暴）
            self._content_dirty = True

            # 缓冲输出，由定时器统一发送（避免高频输出时的信号风暴）
            with self._output_buffer_lock:
                self._output_buffer.append(text)

            # 记录输出活动并重置空闲计时器：停顿超过阈值即视为本轮执行完毕。
            # _activity_idle_timer 是 QTimer，只能在 GUI 线程操作；当本方法跑在读取
            # 线程时(PARSE_ON_READER_THREAD)，经 _output_activity 信号 marshal 回 GUI。
            # 同线程发射时是直连(等价于原地调用)，无额外延迟。
            if text:
                self._output_activity.emit(len(text))
        except Exception as e:
            logger.exception(f"Output error: {e}")

    def _on_output_activity(self, n: int):
        """GUI 线程：标记有输出活动并重置“执行完毕”空闲计时器。"""
        self._had_output_activity = True
        self._activity_bytes += n
        self._activity_idle_timer.start(self._ACTIVITY_IDLE_MS)

    def _on_history_grew(self, lines_added: int):
        """GUI 线程：读取线程 feed 新增历史行后，补偿回滚浏览位置。

        仅在用户回滚浏览（scroll_offset>0）时把 scroll_offset 上移等量行数，
        保持 display_start 不变、可见内容不跳动。scroll_offset 全程 GUI 独占。
        """
        if lines_added <= 0 or self.scroll_offset <= 0:
            return
        max_scroll = self._get_history_count()
        self.scroll_offset = min(self.scroll_offset + lines_added, max_scroll)

    def toggle_debug_capture(self):
        """切换原始输出诊断捕获（用于排查内容过滤问题）"""
        if self._debug_capture_enabled:
            # 关闭捕获
            self._debug_capture_enabled = False
            if self._debug_capture_file:
                self._debug_capture_file.close()
                self._debug_capture_file = None
            logger.info("[Terminal] Debug capture DISABLED")
        else:
            # 开启捕获
            capture_path = str(get_data_dir() / 'terminal_raw_capture.log')
            self._debug_capture_file = open(capture_path, 'w', encoding='utf-8')
            self._debug_capture_enabled = True
            logger.info(f"[Terminal] Debug capture ENABLED → {capture_path}")

    # 交互等待页脚标记：只在程序等待用户操作期间显示、回答后即消失的文案
    # （Claude Code 确认框的 "Esc to cancel"、CLI 的 y/n 询问等）。
    # 不能用问题正文（如 "Do you want"）做标记——回答后正文会残留在回显里。
    # 注意不要加 "esc to interrupt"，那是 Claude 运行中显示的，不是等待操作。
    _INTERACTION_MARKERS = (
        'esc to cancel',
        'esc to exit',
        'enter to confirm',
        'press enter to continue',
        '(y/n',
        '[y/n',
        'y/n)',
        'yes/no',
    )

    def _detect_interaction_prompt(self):
        """检测可见屏幕底部是否有等待用户操作的交互提示。

        返回匹配行拼接成的签名字符串（供调用方去重），没有提示则返回 None。
        """
        try:
            # screen.display 会遍历各行 cell dict 生成字符串；读取线程并发 feed
            # 会往行 dict 插键 → "dict changed size during iteration"，故持锁快照
            with self._screen_lock:
                lines = self.screen.display[-15:]
        except Exception:
            return None
        matched = [
            line.strip() for line in lines
            if any(marker in line.lower() for marker in self._INTERACTION_MARKERS)
        ]
        if not matched:
            return None
        return '\n'.join(matched)

    def _on_activity_settled(self):
        """输出停顿超过阈值：疑似本轮命令/Claude 执行完毕 → 请求导航提醒。

        若屏幕上出现等待操作的交互提示（确认框 / y/n 询问），发
        interaction_requested——该信号即使在活动终端也会点亮导航绿点，且不依赖
        _pending_user_command（Claude 跑到一半弹确认框时并没有新的用户回车）。
        否则按"执行完毕"处理：是否真正打标由 MainWindow 决定（正在看的活动
        终端不打扰）。只在确有一定量输出后才上报，过滤掉纯按键回显这类微小活动。
        """
        if not self._had_output_activity:
            return
        substantial = self._activity_bytes >= 4
        self._had_output_activity = False
        self._activity_bytes = 0
        if not substantial:
            return
        sig = self._detect_interaction_prompt()
        if sig is not None:
            if sig != self._last_interaction_sig:
                self._last_interaction_sig = sig
                self._pending_user_command = False
                self.interaction_requested.emit()
            return
        self._last_interaction_sig = None
        if self._pending_user_command:
            self._pending_user_command = False
            self.attention_requested.emit()

    def _on_process_finished(self, status: int):
        """进程结束"""
        self.session_ended.emit()
        # 关闭诊断捕获文件
        if self._debug_capture_file:
            self._debug_capture_file.close()
            self._debug_capture_file = None

    def _flush_output_buffer(self):
        """刷新输出缓冲区，批量发送 output_recorded 信号"""
        # 原子换出：持锁把整个 buffer 换成新列表，锁外再 join/emit，
        # 避免 join 与 clear 之间读取线程 append 的块丢失
        with self._output_buffer_lock:
            if not self._output_buffer:
                return
            buf = self._output_buffer
            self._output_buffer = []
        self.output_recorded.emit(''.join(buf))

    def _debug_save_cache_pixmap(self):
        """保存当前 cache pixmap 为 PNG，便于和屏幕实际显示对比。"""
        if self._cache_pixmap is None or self._cache_pixmap.isNull():
            return None
        out_path = str(get_data_dir() / 'pyte_cache_dump.png')
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
        out_path = str(get_data_dir() / 'pyte_widget_screenshot.png')
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
        dump_path = str(get_data_dir() / 'pyte_buffer_dump.txt')

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

        logger.info(f"[Terminal] Buffer dumped to {dump_path}")

        cache_png = self._debug_save_cache_pixmap()
        if cache_png:
            logger.info(f"[Terminal] Cache pixmap saved to {cache_png}")

        screen_png = self._debug_save_widget_screenshot()
        if screen_png:
            logger.info(f"[Terminal] Widget screenshot saved to {screen_png}")

    @property
    def _screen_history(self):
        """获取屏幕历史（避免重复 hasattr 检查）"""
        return getattr(self.screen, 'history', None)

    def _get_history_top(self):
        """获取历史记录顶部的【快照列表】（备用屏幕不显示主屏历史）。

        返回 list(history.top) 的拷贝而非活引用：history.top 是有 maxlen 的 deque，
        feed（未来在读取线程）追加/淘汰会移动元素、令调用方迭代时索引错位甚至越界。
        拷贝是浅拷贝——行对象本身不变（历史里的旧行 pyte 不再 mutate，id 稳定，
        软换行判定仍有效），只把 deque 的并发变动隔离掉。持锁拍快照。
        """
        with self._screen_lock:
            if self.screen._in_alt_screen:
                return []
            history = self._screen_history
            return list(history.top) if history else []

    def _get_history_slice(self, start: int, stop: int) -> list:
        """按 [start, stop) 区间拷贝历史行快照（语义同 _get_history_top，只拷区间）。

        渲染每帧都会取历史行；整拷 5000 行 deque 在 20fps 内容帧下是白白的
        每帧开销，绝大多数时间（scroll_offset == 0）可见行全部来自
        screen.buffer，根本不需要碰历史。
        """
        if stop <= start:
            return []
        with self._screen_lock:
            if self.screen._in_alt_screen:
                return []
            history = self._screen_history
            if not history:
                return []
            return list(itertools.islice(history.top, start, stop))

    def _get_history_count(self) -> int:
        """获取历史记录行数（避免重复 hasattr 检查）
        备用屏幕上不显示主屏幕的历史记录。
        """
        with self._screen_lock:
            if self.screen._in_alt_screen:
                return 0
            history = self._screen_history
            return len(history.top) if history else 0

    def _raw_history_count(self) -> int:
        """真实历史行数——不做备用屏幕掩蔽。

        _get_history_count() 在备用屏幕返回 0 是显示语义（滚动条/压力点
        不显示主屏历史）；但 resize 在备用屏幕仍会全量 reflow 被保存的
        主屏+历史，预测 reflow 工作量必须用这个真实值，否则 tmux/vim 里
        resize 会被误判为"便宜"而漏回同步路径。
        """
        with self._screen_lock:
            history = self._screen_history
            return len(history.top) if history else 0

    def clear_scrollback(self):
        """一键清空当前终端的回滚历史，保留可见屏幕。

        丢弃上方积累的历史行并释放其内存,然后复位滚动位置、刷新滚动条与重绘。
        对运行中的终端立即生效。
        """
        screen = getattr(self, 'screen', None)
        if screen is None or not hasattr(screen, 'clear_scrollback'):
            return
        # 持锁清空：读取线程可能正 feed 向 history deque append，
        # 无锁清空会触发 "deque mutated during iteration" 崩溃
        with self._screen_lock:
            screen.clear_scrollback()
            self.scroll_offset = 0
        self._search_line_cache.clear()
        self._invalidate_render_cache()
        self._update_scrollback_level()  # 历史归零 → 压力等级降回 0，指示点消失

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

    def wheelEvent(self, event):
        """鼠标滚轮事件 - 滚动历史"""
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept()
            return

        going_up = delta > 0

        # 备用屏幕里运行的全屏 TUI（Claude Code / vim / less / tmux 等）若启用了
        # 鼠标报告，就把滚轮作为鼠标事件转发给程序，让它滚动自己的内容。备用屏幕
        # 没有本地 scrollback，不转发滚轮就会落空，表现为"无法向上回看历史"。
        # 仅限备用屏幕：主屏幕始终保留本地回滚，绝不把滚轮从用户手里夺走。
        if self._mouse_mode and getattr(self.screen, '_in_alt_screen', False):
            notches = max(1, int(round(abs(delta) / 120.0)))
            self._send_wheel_to_app(going_up, event.position().toPoint(), notches)
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
        # 每次滚轮事件滚动的行数（原为3，减半为1.5）
        # 使用小数累加器，避免整数取整丢失半行
        scroll_step = 1.5
        if going_up:
            # 向上滚动（查看历史）- 增加scroll_offset
            self._scroll_accum += scroll_step
        else:
            # 向下滚动（回到最新）- 减少scroll_offset
            self._scroll_accum -= scroll_step

        # 取整数部分应用到scroll_offset，保留小数部分到下次累加
        lines = int(self._scroll_accum)
        if lines != 0:
            self._scroll_accum -= lines
            self.scroll_offset = max(0, min(self.scroll_offset + lines, max_scroll))

        # 撞到顶/底边界后丢弃残留的小数部分：否则上次累积的半行会在下次反向
        # 滚动时抢跑一行，造成边界处的轻微抽动。
        if self.scroll_offset >= max_scroll or self.scroll_offset <= 0:
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

    # ---------- 输出规则提醒 ----------

    def _scan_output_alerts(self, text: str):
        """在过滤后的输出上做增量模式匹配（与 feed 同线程，可能是读取线程）。

        - strip_ansi 后再匹配：颜色转义会把关键词切碎；
        - 保留 256 字符尾缓冲拼接下一块：模式被分块切开也不漏；
        - 备用屏幕（vim 等全屏 TUI）不扫描；
        - 命中后静默 _ALERT_MUTE_SECONDS 或直到用户按键，避免连环提醒。
        """
        if getattr(self.screen, '_in_alt_screen', False):
            self._alert_tail = ''
            return
        now = time.time()
        if now < self._alert_muted_until:
            self._alert_tail = ''
            return
        from utils import strip_ansi
        buf = self._alert_tail + strip_ansi(text)
        for pattern_str, rx in self._ALERT_COMPILED:
            if rx.search(buf):
                self._alert_muted_until = now + self._ALERT_MUTE_SECONDS
                self._alert_tail = ''
                self.alert_matched.emit(pattern_str)
                return
        self._alert_tail = buf[-256:]

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
                except TypeError:
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
                except TypeError:
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
        # 标记已清理：Qt 可能在控件销毁前仍投递排队中的鼠标事件，
        # 事件处理器据此提前返回，避免访问已置 None 的定时器导致闪退。
        self._cleaned_up = True

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
        if hasattr(self, '_search_debounce_timer') and self._search_debounce_timer:
            self._search_debounce_timer.stop()
            self._search_debounce_timer.deleteLater()
            self._search_debounce_timer = None
        if hasattr(self, '_search_line_cache'):
            self._search_line_cache.clear()

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

    def clear_screen(self):
        """清屏"""
        with self._screen_lock:
            self.screen.reset()
        self._invalidate_render_cache()

    def refresh_terminal(self):
        """刷新终端显示。按前台是否为全屏 TUI 程序自适应：

        - 普通 shell（非 alt-screen）：顺手发一个 Ctrl+L，清空可见屏幕并把提示符
          重画到顶部（保留滚动历史、保留已输入但未回车的命令）。这样刷新有明确的
          可见反馈，而不是"什么反应都没有"。
        - 前台是全屏 TUI（vim / tmux / claude 等，处于 alt-screen）：**不**清屏、
          **不**注入按键，只走下面的「重绘 + 抖动 PTY 触发 SIGWINCH」，让程序自己
          整屏重画——对它们而言清屏是错的。

        两种情况都会重建渲染缓存，修复本地渲染脏区造成的花屏/错位；都不丢滚动历史，
        区别于 clear_screen（整屏复位）。
        """
        # 先本地重绘，修复纯渲染层的脏区
        self._invalidate_render_cache()

        if self._backend is None:
            return

        # 普通 shell：发 Ctrl+L 清屏并重画提示符，给出可见反馈。用 _write_to_backend
        # 原始写入而非 send_text，避免把这个控制键记进输入历史。alt-screen 下跳过，
        # 以免往 TUI 程序里注入 ^L。
        in_alt_screen = getattr(getattr(self, 'screen', None), '_in_alt_screen', False)
        if not in_alt_screen:
            try:
                self._write_to_backend(b'\x0c')
            except Exception as e:
                logger.warning(f"[Terminal] refresh clear (Ctrl+L) error: {e}")

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
            logger.warning(f"[Terminal] refresh resize error: {e}")

    def _restore_pty_size_after_refresh(self):
        """refresh_terminal 抖动后把 PTY 尺寸恢复到当前实际值，并再重绘一次。"""
        if self._backend is None:
            return
        try:
            self._backend.resize(self.term_cols, self.term_rows)
        except Exception as e:
            logger.warning(f"[Terminal] refresh restore error: {e}")
        self._invalidate_render_cache()

    def is_running(self) -> bool:
        """检查是否有进程在运行"""
        return self._backend is not None and self._backend.is_running

    def has_started(self) -> bool:
        """该终端是否曾经真正启动过 shell（backend 已创建）。

        新建但未启动的空白终端返回 False，可被复用（如远程 SSH 复用空闲终端）。
        """
        return self._backend is not None

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
        # 颜色映射变化 → 渲染状态失配，强制下次整屏重绘
        self._render_epoch += 1

    def reset_to_dark_theme_colors(self):
        """重置为深色主题颜色"""
        self._current_colors = self.DEFAULT_COLORS.copy()
        self._current_bright_colors = self.BRIGHT_COLORS.copy()
        self._selection_color = QColor(100, 149, 237, 100)
        self._cursor_color = QColor(200, 200, 200, 180)
        # 清空颜色缓存
        self._color_cache = {}
        self._visible_color_cache = {}
        # 颜色映射变化 → 渲染状态失配，强制下次整屏重绘
        self._render_epoch += 1

    def get_cwd(self) -> Optional[str]:
        """获取子进程的当前工作目录"""
        if self._backend is None or not self._backend.is_running:
            return None

        # 在 Unix/macOS 上获取子进程的当前工作目录
        if sys.platform != 'win32':
            try:
                # 获取后端的进程 ID（仅 Unix 后端支持）
                child_pid = getattr(self._backend, '_child_pid', None)
                if child_pid:
                    # 优先原生接口：不 fork、约 0.2ms、不会被挂死的挂载点拖住
                    native = _cwd_via_native(child_pid)
                    if native and os.path.isdir(native):
                        return native
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
                logger.debug("get_cwd: suppressed exception", exc_info=True)

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

    def _copy_to_clipboard(self):
        """复制终端内容到剪贴板（复制当前可见区域的内容）"""
        content = self._get_visible_content()
        if content.strip():
            clipboard = QApplication.clipboard()
            # 与选区复制一致：NBSP 归一化为普通空格
            clipboard.setText(content.replace('\xa0', ' '))

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

    # ==================== 分屏自定义标签 ====================

    def get_split_label(self):
        """返回当前分屏的自定义名称（未命名时为 None）"""
        return self._split_label

    def set_split_label(self, name):
        """设置或清除分屏自定义名称。

        传入非空字符串：在窗格顶部显示一条标题栏（占据布局空间，内容相应下移）。
        传入 None 或空串：隐藏标题栏，恢复默认的「无标签」状态。
        """
        name = (name or "").strip()
        self._split_label = name or None
        if self._split_label:
            if self._header_bar is None:
                self._create_header_bar()
            self._header_label.setText(self._split_label)
            self._header_bar.setToolTip(self._split_label)
            self._header_h = self.HEADER_HEIGHT
            self._position_header_bar()
            self._header_bar.show()
            self._header_bar.raise_()
        else:
            self._header_h = 0
            if self._header_bar is not None:
                self._header_bar.hide()
        # 顶部留白变化 → 重算行数并整屏重绘，滚动条/指示点随标题栏高度重新定位
        self._position_scrollbar()
        if self._scrollback_dot_btn is not None and self._scrollback_dot_btn.isVisible():
            self._position_scrollback_dot()
        self._invalidate_render_cache()
        self._update_terminal_size()
        self.update()

    def _create_header_bar(self):
        """创建分屏标题栏 overlay（带名称文字 + 清除按钮）"""
        self._header_bar = QWidget(self)
        lay = QHBoxLayout(self._header_bar)
        lay.setContentsMargins(8, 0, 4, 0)
        lay.setSpacing(4)

        self._header_label = QLabel("")
        self._header_label.setStyleSheet(
            "color:#eaeaea;background:transparent;font-size:12px;font-weight:bold;"
        )

        rename_btn = QPushButton("✎")
        rename_btn.setFixedSize(18, 18)
        rename_btn.setToolTip(t("ctx.rename_split"))
        rename_btn.setStyleSheet(
            "QPushButton{color:#cfcfe6;background:transparent;border:none;font-size:12px;}"
            "QPushButton:hover{color:#667eea;}"
        )
        rename_btn.clicked.connect(self.rename_split_requested.emit)

        clear_btn = QPushButton("×")
        clear_btn.setFixedSize(18, 18)
        clear_btn.setToolTip(t("split.clear_name"))
        clear_btn.setStyleSheet(
            "QPushButton{color:#cfcfe6;background:transparent;border:none;font-size:15px;}"
            "QPushButton:hover{color:#ff6b6b;}"
        )
        clear_btn.clicked.connect(lambda: self.set_split_label(None))

        lay.addWidget(self._header_label)
        lay.addStretch(1)
        lay.addWidget(rename_btn)
        lay.addWidget(clear_btn)

        self._header_bar.setStyleSheet("QWidget{background-color:#3d3d5c;}")

    def _position_header_bar(self):
        """把标题栏铺满窗格顶部"""
        if self._header_bar is not None:
            self._header_bar.setGeometry(0, 0, self.width(), self.HEADER_HEIGHT)

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

    def _extract_search_line(self, buffer_line) -> tuple:
        """提取单行用于搜索的 (text, col_map)。

        text 中宽字符只占 1 个字符（跳过 pyte 的 stub 格），col_map[i] 为
        text[i] 对应的 buffer 列号，用于把匹配的字符串下标映射回高亮列区间。
        """
        # 完全空行（无任何已写入格子）直接短路，避免 2 万行历史逐格扫描
        if not buffer_line:
            return "", ()
        columns = self.screen.columns
        try:
            columns = max(columns, max(buffer_line.keys()) + 1)
        except (ValueError, AttributeError, TypeError):
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

    def _perform_search(self):
        """执行全量搜索：全部 history 行 + 当前屏幕行（匹配为搜索时刻的快照）"""
        text = self._search_pending_text
        self._search_matches = []
        self._current_match_index = -1

        if not text:
            self._update_match_label()
            self.update()
            return

        # 限制搜索结果数量，防止内存暴涨
        MAX_SEARCH_RESULTS = 5000

        search_lower = text.lower()
        qlen = len(search_lower)
        is_wide = self._is_wide_char
        matches = self._search_matches

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
                if len(matches) >= MAX_SEARCH_RESULTS:
                    return
                pos = idx + 1

        # 1) 历史行：行对象推入历史后内容不再变化，可按 id 缓存提取结果。
        #    缓存值中保留行对象引用，钉住对象保证 id 在缓存有效期内不被复用。
        cache = self._search_line_cache
        history = self._get_history_top()
        for abs_row, line in enumerate(history):
            key = id(line)
            cached = cache.get(key)
            if cached is not None and cached[0] is line:
                cache.move_to_end(key)
                line_text, col_map = cached[1], cached[2]
            else:
                line_text, col_map = self._extract_search_line(line)
                cache[key] = (line, line_text, col_map)
                if len(cache) > self._SEARCH_LINE_CACHE_MAX:
                    cache.popitem(last=False)
            if line_text:
                find_in_line(line_text, col_map, abs_row)
                if len(matches) >= MAX_SEARCH_RESULTS:
                    break

        # 2) 当前屏幕行（可变，不缓存）
        history_count = len(history)
        if len(matches) < MAX_SEARCH_RESULTS:
            buffer = self.screen.buffer
            for row in range(self.term_rows):
                line_text, col_map = self._extract_search_line(buffer[row])
                if line_text:
                    find_in_line(line_text, col_map, history_count + row)
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        break

        if matches:
            # 初始选中：离当前可见区域中心最近的匹配，避免每次都跳到最顶部
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
        self._match_label.setText(f"{current}/{total}")

    # ==================== 字体缩放 ====================

    def _zoom_in(self):
        """放大字体"""
        current_size = self.term_font.pointSize()
        if current_size < 32:
            self.apply_font_size(current_size + 1)

    def _zoom_out(self):
        """缩小字体"""
        current_size = self.term_font.pointSize()
        if current_size > 8:
            self.apply_font_size(current_size - 1)

    def apply_font_size(self, point_size: int):
        """设置终端字号：字体度量与重绘立即生效，网格重算走 resize 防抖。

        网格重算（_update_terminal_size）在满历史时是一次 O(历史) 的全量
        reflow（真实内容实测 100~200ms），以前每按一次 Cmd+± 就同步执行一次，
        多分屏下还会对每个终端串行来一遍。改为复用 150ms 的 resize 防抖：
        连按只在停手后 reflow 一次。防抖窗口内文本按新字号、旧网格渲染，
        右/下缘可能短暂溢出或留白，150ms 后收敛。
        """
        if self.term_font.pointSize() == point_size:
            return
        self.term_font.setPointSize(point_size)
        self._calculate_char_size()
        self._resize_pending = True
        self._resize_timer.start(150)
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
            # _get_all_content 自持 _screen_lock，防与读取线程 feed 竞态
            content = self._get_all_content()
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
            except Exception as e:
                logger.error(f"Save error: {e}")

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
            # 与 dialogs.get_default_shell 保持一致：默认 cmd.exe，
            # PowerShell 偶发启动失败会导致终端起不来
            if shutil.which('cmd.exe'):
                return 'cmd.exe'
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
            # 发送命令并加回车（终端中 Enter 键发送 \r 而非 \n）
            data = (cmd + '\r').encode('utf-8')
            if self._write_to_backend(data):
                # 记录输入
                if cmd.strip():
                    self.input_recorded.emit(cmd)

    def _start_and_execute(self, commands: list):
        """启动终端并执行命令"""
        # 获取当前工作目录。不回退 os.getcwd()：打包 app 从 Finder 启动时
        # 主进程 cwd 是 /，shell 会起在根目录，改退用户主目录
        cwd = getattr(self, '_working_dir', None) or str(Path.home())
        # 启动默认 shell
        shell = self._get_default_shell()
        self.start_process([shell], cwd=cwd)
        # 延迟执行命令，等待终端启动完成
        def delayed_execute():
            if self._backend and self._backend.is_running:
                for cmd in commands:
                    data = (cmd + '\r').encode('utf-8')
                    if self._write_to_backend(data):
                        if cmd.strip():
                            self.input_recorded.emit(cmd)
        QTimer.singleShot(300, delayed_execute)
