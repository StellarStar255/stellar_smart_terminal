"""
OpenAI API 兼容的 HTTP 服务器
将终端中运行的 AI CLI 暴露为标准 OpenAI API
"""

import hmac
import json
import uuid
import time
import base64
import threading
import contextlib
import re
import socket
import os
from pathlib import Path
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field
from queue import Queue, Empty
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal, QThread, QTimer

from app_logging import get_logger

logger = get_logger(__name__)


# ============================================================
# 屏幕响应提取（模块级纯函数，只依赖标准库，便于无 GUI 单元测试）
# 逻辑与原 OpenAIRequestHandler._extract_response_from_screen 完全一致
# ============================================================

# 响应开始标记（Claude 等 AI 工具常用）
RESPONSE_MARKERS = ('✢', '✶', '✷', '✸', '✹', '✺', '⏺', '●', '✽', '✻', '✼', '❋', '❊', '❉', '✳', '✴', '✵', '*', '·', '•')
# 输入提示符模式
INPUT_PROMPT_PATTERNS = ('>', '›', '❯')
# UI 关键词（需要跳过的行）
EXTRACT_UI_KEYWORDS = ['for shortcuts', 'shift+tab', 'ctrl+', 'claude code', 'opus',
                       'enter to send', 'esc to', 'tab to cycle', 'thought for',
                       'shimmying', 'shimmy', 'analyzing', 'processing', 'thinking',
                       'read image', 'read(', '⎿', '[image #', 'l  [image',
                       'mulling', 'pondering', 'considering', 'reasoning', 'seasoning',
                       'cogitating', 'forming', 'actioning', 'simmering', 'misting',
                       'brewing', 'steeping', 'percolating', 'distilling', 'filtering',
                       'crystallizing', 'synthesizing', 'composing', 'crafting',
                       'weaving', 'spinning', 'churning', 'mixing', 'blending',
                       'stirring', 'whisking',
                       # 更多 thinking 动画词
                       'pollinating', 'germinating', 'blooming', 'budding', 'sprouting',
                       'fermenting', 'marinating', 'roasting', 'baking',
                       'incubating', 'hatching', 'nurturing', 'cultivating', 'growing',
                       'doodling', 'spelunking', 'exploring', 'wandering', 'meandering',
                       'contemplating', 'reflecting', 'musing', 'deliberating',
                       'ruminating', 'meditating', 'digesting', 'absorbing',
                       # Claude 会话反馈提示
                       'how is claude doing', 'this session', '(optional)',
                       '1: bad', '2: fine', '3: good', '0: dismiss',
                       # 队列消息提示
                       'press up to edit', 'queued messages']


def _is_empty_prompt_line(s: str) -> bool:
    """检查是否是空的输入提示符行（等待输入状态）"""
    for p in INPUT_PROMPT_PATTERNS:
        if s == p or s == p + ' ' or (s.startswith(p) and len(s) <= 3):
            return True
    return False


def _is_input_line(s: str) -> bool:
    """检查是否是用户输入行（提示符后有内容）"""
    for p in INPUT_PROMPT_PATTERNS:
        if s.startswith(p) and len(s) > 3:
            return True
    return False


def _is_extract_ui_line(s: str) -> bool:
    """检查是否是 UI 元素行"""
    lower = s.lower()
    if any(kw in lower for kw in EXTRACT_UI_KEYWORDS):
        return True
    clean_check = s.replace(' ', '')
    if clean_check and all(c in '─━═┌┐└┘├┤┬┴┼│┃║·•*-_=—–―‐‑‒⁃)(' for c in clean_check):
        return True
    return False


def _has_response_marker(s: str) -> bool:
    """检查行是否以响应标记开头"""
    return any(s.startswith(m) for m in RESPONSE_MARKERS)


def _clean_response_line(s: str) -> str:
    """清理响应行，移除开头的标记"""
    cleaned = s
    for m in RESPONSE_MARKERS:
        cleaned = cleaned.lstrip(m)
    return cleaned.strip()


def _clean_final_result(text: str) -> str:
    """清理最终结果，移除尾部的孤立括号等 UI 残留"""
    if not text:
        return text
    # 按行处理，移除只包含 ) 的行
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        # 跳过只有 ) 或空白+) 的行
        if stripped == ')' or stripped == '）':
            continue
        # 移除行尾的孤立 )（前面有多个空格的情况）
        if line.rstrip().endswith('  )') or line.rstrip().endswith('  ）'):
            line = line.rstrip()[:-1].rstrip()
        cleaned_lines.append(line)
    result = '\n'.join(cleaned_lines).strip()
    # 移除结果末尾的孤立 )
    while result.endswith(')') or result.endswith('）'):
        result = result[:-1].rstrip()
    return result.strip()


def _find_empty_prompt_line(lines: List[str]) -> int:
    """从底部向上找空提示符行，找不到返回 -1"""
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if _is_empty_prompt_line(stripped):
            logger.debug(f"[Extract] Found empty prompt at line {i}")
            return i
    return -1


def _find_input_boundary(lines: List[str], empty_prompt_line: int):
    """从空提示符向上找 [Image #...] 标记（用户输入的结束）或用户输入行

    返回 (image_marker_line, last_input_line)，未找到的为 -1
    """
    image_marker_line = -1
    last_input_line = -1
    if empty_prompt_line > 0:
        for i in range(empty_prompt_line - 1, -1, -1):
            stripped = lines[i].strip()
            lower = stripped.lower()
            # 找到图片标记
            if '[image #' in lower or '⎿' in stripped:
                image_marker_line = i
                logger.debug(f"[Extract] Found image marker at line {i}: {stripped[:50]}")
                break
            # 如果遇到用户输入行，记录位置
            if _is_input_line(stripped):
                last_input_line = i
                logger.debug(f"[Extract] Found input line at {i}: {stripped[:50]}")
                break
    return image_marker_line, last_input_line


def _extract_input_based_response(lines: List[str], start_line: int, empty_prompt_line: int) -> Optional[str]:
    """策略1：提取分界线之后到空提示符之间的 AI 回答；无内容时返回 None"""
    response_lines = []
    for i in range(start_line, empty_prompt_line):
        stripped = lines[i].strip()
        if not stripped:
            if response_lines:
                response_lines.append('')
            continue

        # 跳过 UI 元素
        if _is_extract_ui_line(stripped):
            continue

        # 跳过空提示符
        if _is_empty_prompt_line(stripped):
            continue

        # 跳过用户输入行
        if _is_input_line(stripped):
            continue

        # 处理响应标记行
        if _has_response_marker(stripped):
            cleaned = _clean_response_line(stripped)
            if cleaned:
                response_lines.append(cleaned)
        else:
            response_lines.append(stripped)

    if response_lines:
        result = '\n'.join(response_lines).strip()
        result = _clean_final_result(result)
        logger.debug(f"[Extract] Input-based response: '{result[:100]}...' ({len(result)} chars)")
        return result
    return None


def _find_last_marker_line(lines: List[str]) -> int:
    """从底部向上找到最后一个响应标记行，找不到返回 -1"""
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if _has_response_marker(stripped):
            logger.debug(f"[Extract] Found last response marker at line {i}: {stripped[:50]}")
            return i
    return -1


def _extract_marker_based_response(lines: List[str], last_marker_line: int) -> Optional[str]:
    """备用方法：从最后一个响应标记开始提取到空提示符；无内容时返回 None"""
    response_lines = []
    for i in range(last_marker_line, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            if response_lines:
                response_lines.append('')
            continue

        # 检测空提示符（结束）
        if _is_empty_prompt_line(stripped):
            logger.debug(f"[Extract] Found prompt at line {i}, ending")
            break

        # 跳过 UI 元素
        if _is_extract_ui_line(stripped):
            continue

        # 处理响应标记行
        if _has_response_marker(stripped):
            cleaned = _clean_response_line(stripped)
            if cleaned:
                response_lines.append(cleaned)
        else:
            response_lines.append(stripped)

    if response_lines:
        result = '\n'.join(response_lines).strip()
        result = _clean_final_result(result)
        logger.debug(f"[Extract] Marker-based response: '{result[:100]}...' ({len(result)} chars)")
        return result
    return None


# C0 控制字符（除 \t \n \r）+ DEL + C1；ESC 也在内 → 阻断 ANSI/OSC 转义序列。
# 请求正文会被粘贴进能执行命令的终端，恶意本地进程可借控制序列做键盘注入 /
# 终端标题改写 / 伪造粘贴括号等。合法聊天正文不含这些字节，直接剥离。
_PTY_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')


def _sanitize_pty_input(text: str) -> str:
    """剥离注入到终端的用户正文里的控制字符，保留 \\t 与换行。

    仅用于外部 HTTP 请求正文；内部生成的控制序列（ESC 中断 / \\r / /clear）
    不经此函数，走 bridge.send_input 原样发送。
    """
    if not text:
        return text
    return _PTY_CONTROL_RE.sub('', text)


def extract_response_from_screen(screen_content: str, input_text: str = "") -> str:
    """从屏幕内容中提取 AI 响应 - 只提取最后一次用户输入之后的 AI 回答（调度函数）"""
    if not screen_content:
        logger.debug("[Extract] Screen content is empty")
        return ""

    lines = screen_content.split('\n')

    # 策略1：找到空提示符，然后找 [Image #...] 标记（用户输入结束），提取之后的内容
    # AI 回答可能有标记（* · •）也可能没有
    empty_prompt_line = _find_empty_prompt_line(lines)
    image_marker_line, last_input_line = _find_input_boundary(lines, empty_prompt_line)

    # 优先使用图片标记作为分界线
    start_line = -1
    if image_marker_line >= 0:
        start_line = image_marker_line + 1
    elif last_input_line >= 0:
        start_line = last_input_line + 1

    # 提取 AI 回答（从分界线之后到空提示符之间）
    if start_line >= 0 and empty_prompt_line > start_line:
        result = _extract_input_based_response(lines, start_line, empty_prompt_line)
        if result is not None:
            return result

    # 备用方法：从底部向上找到最后一个响应标记，然后提取
    logger.debug(f"[Extract] Trying marker-based extraction")
    last_marker_line = _find_last_marker_line(lines)
    if last_marker_line >= 0:
        result = _extract_marker_based_response(lines, last_marker_line)
        if result is not None:
            return result

    logger.debug(f"[Extract] No response found")
    return ""


@dataclass
class ServerConfig:
    """服务器配置"""
    port: int
    host: str = "127.0.0.1"
    timeout: float = 300.0  # 等待AI响应的超时时间（秒）
    idle_timeout: float = 8.0  # 静默超时（秒）- 备用检测方式，当提示符检测失败时使用
    min_content_for_end: int = 20  # 检测结束前需要的最小内容长度
    prompt_patterns: List[str] = field(default_factory=list)
    clear_after_query: bool = False  # 每次 query 后清除会话（发送 /clear）
    rate_limit_queries: int = 10  # 每多少次请求后暂停
    rate_limit_pause: float = 10.0  # 暂停时间（秒）
    api_key: str = ""  # Optional API key for authentication (empty = no auth)
    allowed_origins: List[str] = field(default_factory=list)  # CORS allowed origins (empty = localhost only)

    def __post_init__(self):
        if not self.prompt_patterns:
            self.prompt_patterns = [
                r'^\s*>\s*$',
                r'^\s*›\s*$',
                r'^\s*❯\s*$',
            ]


class TerminalBridge(QObject):
    """终端桥接器：连接 HTTP 服务器与 TerminalWidget"""
    send_input_requested = pyqtSignal(str)

    # Queue 最大大小，防止内存泄漏
    MAX_QUEUE_SIZE = 10000
    # 屏幕内容缓存有效期（秒）
    SCREEN_CACHE_TTL = 0.05

    # 每多少次查询后强制重置会话（发送 /clear）
    AUTO_RESET_INTERVAL = 50

    def __init__(self, terminal_widget, config: ServerConfig = None):
        super().__init__()
        self.terminal = terminal_widget
        self.config = config
        self.output_buffer = Queue(maxsize=self.MAX_QUEUE_SIZE)
        self.is_collecting = False
        # 服务器停止时置位；HTTP 线程的长收集循环轮询它以便尽快退出
        self.shutdown_event = threading.Event()
        self._lock = threading.Lock()
        self._connected = False
        self._request_active = False  # 是否有活跃请求
        self._last_screen_content = ""  # 上次屏幕内容（用于差异检测）
        self._request_count = 0  # 请求计数器，用于定期清理
        self._rate_limit_count = 0  # 速率限制计数器
        self._last_clear_time = 0.0  # 上次 /clear 命令时间
        self._min_clear_interval = 5.0  # /clear 命令最小间隔（秒）
        self._queries_since_reset = 0  # 自上次重置以来的查询次数
        self._consecutive_clear_failures = 0  # 连续 /clear 失败次数
        # 屏幕内容缓存（性能优化）
        self._screen_cache = ""
        self._screen_cache_time = 0.0

        self.send_input_requested.connect(self._do_send_input)

        if hasattr(terminal_widget, 'raw_output_received'):
            terminal_widget.raw_output_received.connect(self.on_output)
            terminal_widget._api_output_enabled = True
            self._connected = True

    def cleanup(self):
        """清理资源"""
        with self._lock:
            if self._connected and self.terminal and hasattr(self.terminal, 'raw_output_received'):
                try:
                    self.terminal.raw_output_received.disconnect(self.on_output)
                    self.terminal._api_output_enabled = False
                except (TypeError, RuntimeError):
                    pass
                self._connected = False
            self.is_collecting = False
            self._request_active = False

        # 彻底清空 Queue
        while not self.output_buffer.empty():
            try:
                self.output_buffer.get_nowait()
            except Empty:
                break

        # 释放大字符串
        self._last_screen_content = ""
        logger.debug(f"[TerminalBridge] Cleanup completed after {self._request_count} requests")

    def send_input(self, data):
        """线程安全地向终端发送输入：经信号排队到 GUI 线程执行。

        HTTP 工作线程禁止直接 os.write(master_fd)：与 GUI 线程的并发写会
        字节交错；tab 关闭后 fd 编号被复用还可能写进别的终端。
        """
        if isinstance(data, bytes):
            data = data.decode('utf-8', errors='replace')
        self.send_input_requested.emit(data)

    def _do_send_input(self, text: str):
        """在主线程中发送输入到终端"""
        if self.terminal and hasattr(self.terminal, '_backend') and self.terminal._backend is not None:
            try:
                self.terminal._write_to_backend(text.encode('utf-8'))
            except (OSError, AttributeError):
                pass

    def start_request(self) -> bool:
        """开始新请求，返回是否成功"""
        # 先获取锁检查状态，但不要在锁内 sleep
        with self._lock:
            if self._request_active:
                logger.debug("[TerminalBridge] Request already active, rejecting new request")
                return False  # 拒绝新请求，避免重复触发
            self._request_active = True
            self.is_collecting = False
            self._request_count += 1
            self._rate_limit_count += 1

        # 速率限制：每 N 次请求后暂停
        if self.config and self.config.rate_limit_queries > 0:
            if self._rate_limit_count >= self.config.rate_limit_queries:
                pause_time = self.config.rate_limit_pause
                logger.debug(f"[TerminalBridge] Rate limit reached ({self._rate_limit_count} requests), pausing for {pause_time}s...")
                time.sleep(pause_time)
                self._rate_limit_count = 0  # 重置计数器
                logger.debug(f"[TerminalBridge] Rate limit pause completed, resuming")

        # 在锁外执行清空操作（避免死锁）
        cleared = 0
        for _ in range(3):  # 多次清空尝试
            while not self.output_buffer.empty():
                try:
                    self.output_buffer.get_nowait()
                    cleared += 1
                except Empty:
                    break
            time.sleep(0.05)  # 短暂等待，在锁外

        if cleared > 0:
            logger.debug(f"[TerminalBridge] Cleared {cleared} items from buffer")

        # 每500次请求进行一次深度清理（降低频率，避免干扰）
        if self._request_count % 500 == 0:
            self._deep_cleanup()
            logger.debug(f"[TerminalBridge] Deep cleanup at request #{self._request_count}")

        with self._lock:
            self.is_collecting = True

        logger.debug("[TerminalBridge] Request started, collecting output")
        return True

    def _deep_cleanup(self):
        """深度清理资源，防止长期运行后的内存泄漏"""
        # 重置屏幕跟踪（释放可能很大的字符串）
        self._last_screen_content = ""
        # 确保 Queue 为空
        while not self.output_buffer.empty():
            try:
                self.output_buffer.get_nowait()
            except Empty:
                break

    def end_request(self):
        """结束当前请求"""
        with self._lock:
            self.is_collecting = False
            self._request_active = False

        # 在锁外清空残留数据，防止累积
        cleared = 0
        while not self.output_buffer.empty():
            try:
                self.output_buffer.get_nowait()
                cleared += 1
            except Empty:
                break

        if cleared > 0:
            logger.debug(f"[TerminalBridge] Request ended, cleared {cleared} residual items")
        else:
            logger.debug("[TerminalBridge] Request ended")

    def on_output(self, text: str):
        """接收终端输出"""
        if self.is_collecting and text:
            try:
                # 使用 put_nowait 避免阻塞，如果 Queue 满则丢弃旧数据
                self.output_buffer.put_nowait(text)
            except Exception:
                # Queue 满时，丢弃最旧的数据并添加新数据
                try:
                    self.output_buffer.get_nowait()
                    self.output_buffer.put_nowait(text)
                except Exception:
                    logger.debug("on_output: suppressed exception", exc_info=True)

    def get_output(self, timeout: float = 0.1) -> Optional[str]:
        """获取输出（带超时）"""
        try:
            return self.output_buffer.get(timeout=timeout)
        except Empty:
            return None

    def get_screen_content(self, use_cache: bool = True) -> str:
        """获取当前 pyte 屏幕内容（已渲染的文本）

        Args:
            use_cache: 是否使用缓存（默认True，提高性能）
        """
        # 检查缓存是否有效
        current_time = time.time()
        if use_cache and self._screen_cache and (current_time - self._screen_cache_time) < self.SCREEN_CACHE_TTL:
            return self._screen_cache

        if not self.terminal or not hasattr(self.terminal, 'screen'):
            return ""
        try:
            # 本方法跑在 HTTP worker 线程，而 GUI 线程会 mutate 同一个 pyte 屏幕。
            # 持终端的屏幕锁再读，避免读到 feed/reflow 到一半的状态（撕裂/越界）。
            # 旧终端对象可能没有该锁 → 退化为 nullcontext 保持兼容。
            lock = getattr(self.terminal, '_screen_lock', None)
            with (lock if lock is not None else contextlib.nullcontext()):
                screen = self.terminal.screen
                # 使用 pyte 的 display 属性（如果可用）更高效
                if hasattr(screen, 'display'):
                    result = '\n'.join(line.rstrip() for line in screen.display)
                else:
                    # 回退到手动读取
                    lines = []
                    buffer = screen.buffer
                    columns = screen.columns
                    for row in range(screen.lines):
                        row_buffer = buffer[row]
                        line = ''.join(row_buffer[col].data for col in range(columns))
                        lines.append(line.rstrip())
                    result = '\n'.join(lines)

            # 更新缓存
            self._screen_cache = result
            self._screen_cache_time = current_time
            return result
        except Exception as e:
            return self._screen_cache if self._screen_cache else ""

    def get_screen_diff(self) -> str:
        """获取屏幕内容的差异（新增部分）"""
        current = self.get_screen_content()
        if not current:
            return ""

        # 如果没有上次内容，返回空（等待初始化）
        if not self._last_screen_content:
            self._last_screen_content = current
            return ""

        # 计算差异
        if current == self._last_screen_content:
            return ""

        # 找到新增的内容
        old_len = len(self._last_screen_content)
        if current.startswith(self._last_screen_content):
            # 简单追加的情况
            diff = current[old_len:]
        else:
            # 内容被修改的情况，尝试找到共同前缀
            common_len = 0
            min_len = min(len(current), len(self._last_screen_content))
            for i in range(min_len):
                if current[i] == self._last_screen_content[i]:
                    common_len = i + 1
                else:
                    break
            diff = current[common_len:]

        self._last_screen_content = current
        return diff

    def reset_screen_tracking(self):
        """重置屏幕跟踪状态"""
        self._last_screen_content = self.get_screen_content()


class ResponseParser:
    """响应解析器：处理终端输出，检测 AI 回复结束"""

    RE_ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
    RE_OSC = re.compile(r'\x1b\][^\x07]*\x07')
    RE_CHARSET = re.compile(r'\x1b[()][AB012]')
    RE_KEYMODE = re.compile(r'\x1b=|\x1b>')
    RE_CTRL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
    RE_DECORATION_LINE = re.compile(r'^[\s─━═\-_─]+$')
    # 加载动画模式检测（Shimmying, Shimmy 等的片段）
    RE_LOADING_PATTERN = re.compile(r'^[Ss]h[im]*y?i?n?g?\.{0,3}$|^[Ss]h[im]+$|^[iy]+n?g?$|^m+[iy]*$|^n?g?\.{0,3}$')

    # 需要过滤的 UI 关键词
    UI_KEYWORDS = [
        'for shortcuts', 'ctrl+c to interrupt', 'ctrl+g to edit',
        'esc to cancel', 'enter to send', '↵ send', '↩ send',
        'shift+tab to cycle', 'accept edits on', 'tab to cycle',
        'forming', 'cogitating', 'analyzing', 'processing',
        'claude code', 'claude max', 'opus 4', 'sonnet', 'haiku',
        'thought for', 'seasoning', 'reasoning',
        'shimmying', 'shimmy',  # 加载动画
    ]

    # 装饰字符和加载动画字符
    # 注意：✢✶✷✸✹✺✽✻✼❋❊❉ 等可能是 AI 响应标记，不要移除
    CHARS_TO_REMOVE = set('─━═┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬│┃║▀▄█▌▐░▒▓⏵⏴⏶⏷◆◇▶►▷◀◁▲△▼▽○◉◎★☆♦♢\ufffd'
                         '✾✿❀❁❂❃❄❅❆❇❈✣✤✥✦✧✩✪✫✬✭✮✯✰✱✲✳✴✵·…❯›>')
    BOX_CHARS = set('─━═┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬│┃║▀▄█▌▐░▒▓')

    def __init__(self, config: ServerConfig):
        self.config = config
        self._last_content_hash = ""  # 用于检测重复内容
        self._parse_count = 0  # 解析计数器

    def strip_ansi(self, text: str) -> str:
        """移除 ANSI 转义序列"""
        text = self.RE_ANSI.sub('', text)
        text = self.RE_OSC.sub('', text)
        text = self.RE_CHARSET.sub('', text)
        text = self.RE_KEYMODE.sub('', text)
        text = self.RE_CTRL.sub('', text)
        return text.replace('\r', '')

    def is_ui_line(self, line: str) -> bool:
        """检测是否为 UI 元素行"""
        clean = self.strip_ansi(line).strip()
        if not clean:
            return True  # 空行视为UI

        # 单独的提示符
        normalized = clean.replace('\u00a0', ' ').strip()
        if normalized in ['>', '›', '❯', '']:
            return True

        # 装饰线
        if self.RE_DECORATION_LINE.match(clean):
            return True

        # box drawing 字符占比过高
        box_count = sum(1 for c in clean if c in self.BOX_CHARS)
        if len(clean) > 5 and box_count > len(clean) * 0.4:
            return True

        # UI 关键词
        lower = clean.lower()
        for kw in self.UI_KEYWORDS:
            if kw in lower:
                return True

        # 特殊前缀
        if clean[0] in '⏵⏺◆◇▶►➜➤':
            return True

        # 加载动画片段检测（如 "Sh", "imm", "ying" 等）
        if self.RE_LOADING_PATTERN.match(clean):
            return True

        # 只包含加载动画字符（排除可能的响应标记：✢✶✷✸✹✺✽✻✼❋❊❉●⏺）
        loading_chars = set('✾✿❀❁❂❃❄❅❆❇❈✣✤✥✦✧✩✪✫✬✭✮✯✰✱✲✳✴✵·…')
        if all(c in loading_chars or c.isspace() for c in clean):
            return True

        return False

    # 加载动画词汇的正则模式
    RE_LOADING_WORDS = re.compile(
        r'\b(?:Shimmying|Shimmy|Shim+y?i?n?g?|thinking|thought\s+for|'
        r'seasoning|reasoning|cogitating|analyzing|processing|forming)\b',
        re.IGNORECASE
    )
    # 检测混乱的加载动画片段（如 "Sbmotontgi"）
    RE_GARBLED_LOADING = re.compile(r'[Ss][bh]?[mio]+[tny]+[goi]*')

    def filter_output(self, text: str, input_text: str = "") -> str:
        """过滤输出，移除UI元素和输入回显"""
        clean = self.strip_ansi(text)

        # 首先移除加载动画相关词汇
        clean = self.RE_LOADING_WORDS.sub('', clean)

        # 移除混乱的加载动画片段（连续的无意义字母组合）
        # 检测类似 "Sbmotontgi" 的模式
        words = clean.split()
        filtered_words = []
        for word in words:
            # 如果词只包含加载动画特征字符且长度较短，跳过
            if len(word) <= 12 and self.RE_GARBLED_LOADING.fullmatch(word):
                continue
            filtered_words.append(word)
        clean = ' '.join(filtered_words)

        lines = clean.split('\n')
        filtered = []

        input_prefix = input_text.strip()[:80] if input_text else ""

        for line in lines:
            # 跳过包含输入文本的行（输入回显）
            if input_prefix and input_prefix in line:
                continue

            # 跳过 UI 行
            if self.is_ui_line(line):
                continue

            # 清理特殊字符
            cleaned = line.lstrip('⏺⏵⏴⏶⏷ ')
            cleaned = ''.join(c for c in cleaned if c not in self.CHARS_TO_REMOVE)

            if cleaned.strip():
                filtered.append(cleaned)

        return '\n'.join(filtered)

    def is_new_content(self, content: str) -> bool:
        """检测是否为新内容（非重复）"""
        if not content.strip():
            return False

        # 使用内容hash检测重复
        content_hash = hash(content.strip()[-200:])  # 只比较最后200字符
        if content_hash == self._last_content_hash:
            return False

        self._last_content_hash = content_hash
        return True

    def reset_content_tracker(self):
        """重置内容追踪器"""
        self._last_content_hash = ""
        self._parse_count += 1
        # 每100次解析输出一次状态（用于调试）
        if self._parse_count % 100 == 0:
            logger.debug(f"[ResponseParser] Parse count: {self._parse_count}")

    def is_thinking_status(self, text: str) -> bool:
        """检测是否在思考状态"""
        lower = text.lower()
        # Claude thinking 状态会显示各种动词 + "... (esc to interrupt)"
        thinking_keywords = [
            'thought for', 'thinking', 'seasoning', 'reasoning',
            'cogitating', 'analyzing', 'processing', 'forming',
            'mulling', 'esc to interrupt', 'pondering', 'considering',
            # Claude 的各种 thinking 动画词
            'actioning', 'simmering', 'misting', 'brewing', 'steeping',
            'percolating', 'distilling', 'filtering', 'crystallizing',
            'synthesizing', 'composing', 'crafting', 'weaving', 'spinning',
            'churning', 'mixing', 'blending', 'stirring', 'whisking',
            # 更多 Claude thinking 动画词
            'pollinating', 'germinating', 'blooming', 'budding', 'sprouting',
            'fermenting', 'marinating', 'roasting', 'baking',
            'incubating', 'hatching', 'nurturing', 'cultivating', 'growing',
            # 更多发现的 thinking 动画词
            'doodling', 'spelunking', 'exploring', 'wandering', 'meandering',
            'contemplating', 'reflecting', 'musing', 'deliberating',
            'ruminating', 'meditating', 'digesting', 'absorbing',
            # 新发现的 thinking 动画词
            'nebulizing', 'nebuliz',
        ]
        for kw in thinking_keywords:
            if kw in lower:
                return True
        return False

    def is_stuck_thinking(self, text: str) -> bool:
        """检测是否卡在思考状态（思考但没有输出 token）"""
        lower = text.lower()
        # 检测 "0 tokens" 或 "↓ 0 tokens" 或 "↑ 0 tokens" 模式
        if '0 tokens' in lower and self.is_thinking_status(text):
            return True
        return False

    def is_response_complete(self, screen_content: str) -> bool:
        """检测终端响应是否完成（通过检测输入提示符）"""
        if not screen_content:
            return False

        # 如果还在 thinking 状态，不认为完成
        if self.is_thinking_status(screen_content):
            return False

        lines = screen_content.split('\n')
        prompt_patterns = ('>', '›', '❯')
        # UI 元素关键词（需要跳过）
        ui_skip_keywords = ['for shortcuts', 'shift+tab', 'ctrl+', '───', '━━━', '═══']

        def is_ui_or_empty(s: str) -> bool:
            if not s:
                return True
            lower = s.lower()
            if any(kw in lower for kw in ui_skip_keywords):
                return True
            # 分隔线
            clean = s.replace(' ', '')
            if clean and all(c in '─━═┌┐└┘├┤┬┴┼│┃║' for c in clean):
                return True
            return False

        # 从底部向上查找，跳过 UI 元素
        for i in range(len(lines) - 1, max(len(lines) - 20, -1), -1):
            line = lines[i].strip()

            # 跳过空行和 UI 元素
            if is_ui_or_empty(line):
                continue

            # 检查是否是输入提示符（空的等待输入状态）
            for p in prompt_patterns:
                if line == p or line == p + ' ' or (line.startswith(p) and len(line) <= 3):
                    return True

            # 找到了非 UI 非提示符的行，说明还在输出
            return False

        return False


class OpenAIRequestHandler(BaseHTTPRequestHandler):
    """处理 OpenAI 兼容 API 请求"""

    server_version = "SmartTerminal/1.0"

    # socket 读写超时（秒）。BaseHTTPRequestHandler 默认 None：客户端连上后不发
    # 请求行，handle() 里的 rfile.readline() 会永久阻塞并占住一个线程（slowloris）。
    # SSE 长响应不受影响：那是写方向，且每 5s 有心跳；写阻塞 30s 说明客户端
    # 早已不读，按断开处理（见各处 except TimeoutError）。
    timeout = 30

    # 客户端断开标志（SSE 写入失败时置位，停止继续等待生成）
    _client_disconnected = False

    def __init__(self, *args, bridge: TerminalBridge, parser: ResponseParser,
                 config: ServerConfig, **kwargs):
        self.bridge = bridge
        self.parser = parser
        self.config = config
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        pass

    def _get_cors_origin(self) -> str:
        """Return the CORS origin header value based on config."""
        if self.config.allowed_origins:
            origin = self.headers.get('Origin', '')
            if origin in self.config.allowed_origins:
                return origin
            return ''  # Origin not allowed
        # Default: only allow localhost origins
        origin = self.headers.get('Origin', '')
        if origin:
            from urllib.parse import urlparse
            try:
                parsed = urlparse(origin)
                if parsed.hostname in ('localhost', '127.0.0.1', '::1'):
                    return origin
            except Exception:
                logger.debug("_get_cors_origin: suppressed exception", exc_info=True)
            return ''  # Non-localhost origin blocked
        return ''  # No origin header

    def _check_auth(self) -> bool:
        """Check API key authentication if configured."""
        if not self.config.api_key:
            return True  # No auth configured
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            return hmac.compare_digest(auth_header[7:].encode('utf-8'),
                                       self.config.api_key.encode('utf-8'))
        return False

    _LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')

    def _check_host(self) -> bool:
        """Reject requests whose Host header is not this loopback server（DNS rebinding 防护）.

        _check_csrf 只看 Sec-Fetch-Site / Origin：攻击者把 evil.com 的 DNS 重绑到
        127.0.0.1 之后，浏览器发出的是「同源」请求（Sec-Fetch-Site: same-origin、
        无 Origin），两道检查全部放行，请求正文随即被写进 PTY 执行。浏览器无法
        伪造 Host 头，它永远是地址栏里的域名，所以 Host 不是本机回环名就一律拒绝。
        非浏览器客户端（curl / openai SDK）按 URL 自动带 127.0.0.1:port，照常放行。
        """
        raw = (self.headers.get('Host') or '').strip()
        if not raw:
            return False
        try:
            port = int(self.server.server_address[1])
        except Exception:
            port = self.config.port
        try:
            # urlsplit 统一处理 host:port / [::1]:port
            from urllib.parse import urlsplit
            parts = urlsplit('//' + raw)
            hostname, hport = parts.hostname, parts.port
        except ValueError:
            return False
        if not hostname or hport != port:
            return False
        allowed = set(self._LOOPBACK_HOSTS)
        cfg_host = (self.config.host or '').strip().lower()
        # 用户显式绑到某个具体地址（非通配）时，也接受该地址作为 Host
        if cfg_host and cfg_host not in ('0.0.0.0', '::', ''):
            allowed.add(cfg_host)
        return hostname.lower() in allowed

    def _check_csrf(self) -> bool:
        """Reject browser-driven cross-site requests (CSRF防护).

        本服务器把请求内容直接写入终端 PTY 并执行，且默认无鉴权。仅绑定 127.0.0.1
        并不足以防御 CSRF：恶意网页用 text/plain「简单请求」即可免预检 POST 过来，
        CORS 只挡读响应、挡不住已经发生的注入副作用。
        这里据浏览器自带的 Sec-Fetch-Site / Origin 头拒绝跨站调用；非浏览器客户端
        （curl、openai SDK 等）不带这些头，照常放行。
        """
        # 现代浏览器：跨站/跨源调用会带 cross-site / same-site；同源或非浏览器为 same-origin/none
        sfs = self.headers.get('Sec-Fetch-Site', '')
        if sfs and sfs not in ('same-origin', 'none'):
            return False
        # 跨源请求必带 Origin；只允许 localhost 来源
        origin = self.headers.get('Origin', '')
        if origin:
            from urllib.parse import urlparse
            try:
                host = urlparse(origin).hostname
            except Exception:
                return False
            if host not in ('localhost', '127.0.0.1', '::1'):
                return False
        return True

    def do_OPTIONS(self):
        if not self._check_host():
            self._send_error(403, "Host not allowed")
            return
        self.send_response(200)
        cors_origin = self._get_cors_origin()
        if cors_origin:
            self.send_header('Access-Control-Allow-Origin', cors_origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.end_headers()

    def do_GET(self):
        if not self._check_host():
            self._send_error(403, "Host not allowed")
            return
        if not self._check_csrf():
            self._send_error(403, "Cross-site request forbidden")
            return
        if not self._check_auth():
            self._send_error(401, "Invalid or missing API key")
            return
        if self.path == '/v1/models':
            self._handle_models()
        elif self.path == '/health':
            self._send_json({"status": "ok"})
        else:
            self._send_error(404, "Not Found")

    def do_POST(self):
        if not self._check_host():
            self._send_error(403, "Host not allowed")
            return
        if not self._check_csrf():
            self._send_error(403, "Cross-site request forbidden")
            return
        if not self._check_auth():
            self._send_error(401, "Invalid or missing API key")
            return
        if self.path == '/v1/chat/completions':
            self._handle_chat_completions()
        else:
            self._send_error(404, "Not Found")

    def _handle_models(self):
        self._send_json({
            "object": "list",
            "data": [{
                "id": "terminal-ai",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "terminal"
            }]
        })

    MAX_REQUEST_SIZE = 10 * 1024 * 1024

    def _handle_chat_completions(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length <= 0:
                self._send_error(400, "Empty request body")
                return
            if content_length > self.MAX_REQUEST_SIZE:
                self._send_error(413, "Request too large")
                return

            body_buf = bytearray()
            remaining = content_length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                body_buf.extend(chunk)
                remaining -= len(chunk)
            body_bytes = bytes(body_buf)

            try:
                request = json.loads(body_bytes.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_error(400, f"Invalid request: {e}")
                return

            messages = request.get('messages', [])
            stream = request.get('stream', False)
            input_text = self._build_input(messages)

            # 不打印消息/输入正文，避免 prompt 内容泄露到 stdout/日志；只记录元信息
            logger.debug(f"[OpenAI Server] Messages count: {len(messages)}")
            for i, msg in enumerate(messages):
                role = msg.get('role', 'unknown')
                content = msg.get('content', '')
                logger.debug(f"[OpenAI Server]   [{i}] role={role}, content_len={len(str(content))}")
            logger.debug(f"[OpenAI Server] Built input_text: {len(input_text)} chars")

            if stream:
                self._handle_stream_response(input_text, request)
            else:
                self._handle_blocking_response(input_text, request)

        except BrokenPipeError:
            pass
        except Exception as e:
            logger.error(f"[OpenAI Server] Error: {e}")
            try:
                self._send_error(500, str(e))
            except Exception:
                logger.debug("_handle_chat_completions: suppressed exception", exc_info=True)

    def _build_input(self, messages: list) -> str:
        """提取当前 query 的 system prompt + 最后一条 user prompt"""
        parts = []

        # 1. 提取 system prompt
        for msg in messages:
            if msg.get('role') == 'system':
                content = msg.get('content', '')
                if isinstance(content, str) and content.strip():
                    parts.append(content.strip())

        # 2. 只提取最后一条 user 消息（当前问题）
        last_user_msg = None
        for msg in reversed(messages):
            if msg.get('role') == 'user':
                last_user_msg = msg
                break

        if last_user_msg:
            content = last_user_msg.get('content', '')
            if isinstance(content, str):
                if content.strip():
                    parts.append(content.strip())
            elif isinstance(content, list):
                for item in content:
                    if item.get('type') == 'text':
                        text = item.get('text', '')
                        if text.strip():
                            parts.append(text.strip())
                    elif item.get('type') == 'image_url':
                        image_path = self._save_image(item.get('image_url', {}))
                        if image_path:
                            parts.append(image_path)

        return _sanitize_pty_input('\n'.join(parts))

    def _save_image(self, image_url: dict) -> Optional[str]:
        url = image_url.get('url', '')
        if not url.startswith('data:image'):
            return url

        try:
            header, data = url.split(',', 1)
            ext = '.png'
            if 'jpeg' in header or 'jpg' in header:
                ext = '.jpg'
            elif 'gif' in header:
                ext = '.gif'
            elif 'webp' in header:
                ext = '.webp'

            image_data = base64.b64decode(data)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            # 源码运行：沿用进程工作目录（行为不变）；打包运行：cwd 通常是 "/"
            # （Finder 启动）不可写，改存到用户数据目录。
            from utils import is_frozen, get_data_dir
            if is_frozen():
                images_dir = get_data_dir() / ".images"
            else:
                images_dir = Path(os.getcwd()) / ".images"
            images_dir.mkdir(exist_ok=True)
            file_path = images_dir / f"api_{timestamp}{ext}"
            with open(file_path, 'wb') as f:
                f.write(image_data)
            return str(file_path)
        except Exception as e:
            logger.error(f"[OpenAI Server] Error saving image: {e}")
            return None

    def _handle_blocking_response(self, input_text: str, request: dict):
        """处理阻塞式响应"""
        logger.debug(f"[OpenAI Server] Blocking request, input: {len(input_text)} chars")

        master_fd = self.bridge.terminal.master_fd if self.bridge.terminal else None
        if master_fd is None:
            self._send_error(500, "Terminal not available")
            return

        # 开始请求
        if not self.bridge.start_request():
            self._send_error(429, "Another request is already in progress")
            return

        try:
            self.parser.reset_content_tracker()

            # 检查输入是否为空
            if not input_text.strip():
                logger.warning("[OpenAI Server] ERROR: input_text is empty!")
                self._send_error(400, "No user message content")
                return

            # 如果启用了清除会话，先发送 /clear 命令
            if self.config.clear_after_query:
                self._send_clear_command_sync()

            # 发送输入文本（不带回车）——经 bridge 信号排队到 GUI 线程写入
            logger.debug(f"[OpenAI Server] Sending input to terminal: {len(input_text)} chars")
            self.bridge.send_input(input_text)
            logger.debug(f"[OpenAI Server] Text sent: {len(input_text)} chars, waiting for paste detection...")

            # 等待检测到 [Pasted text 出现，然后发送回车
            paste_detected = False
            for _ in range(50):  # 最多等待 5 秒
                time.sleep(0.1)
                screen = self.bridge.get_screen_content()
                if '[Pasted text' in screen or 'Pasted text' in screen:
                    paste_detected = True
                    logger.debug("[OpenAI Server] Paste detected, sending Enter...")
                    time.sleep(0.1)
                    self.bridge.send_input('\r')
                    break

            if not paste_detected:
                # 如果没检测到粘贴提示（可能是单行输入），直接发送回车
                logger.debug("[OpenAI Server] No paste prompt, sending Enter directly...")
                self.bridge.send_input('\r')

            # 收集响应
            response_content = self._collect_response(input_text)

            # 构建响应
            response = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": request.get('model', 'terminal-ai'),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": response_content},
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": len(input_text.split()),
                    "completion_tokens": len(response_content.split()),
                    "total_tokens": len(input_text.split()) + len(response_content.split())
                }
            }
            self._send_json(response)
            logger.debug(f"[OpenAI Server] Response sent: {len(response_content)} chars")

        except Exception as e:
            logger.error(f"[OpenAI Server] Error: {e}")
            self._send_error(500, str(e))
        finally:
            self.bridge.end_request()

    def _collect_response(self, input_text: str) -> str:
        """收集并处理响应（等待完成后从屏幕提取）"""
        start_time = time.time()
        last_activity_time = time.time()
        last_significant_change_time = time.time()  # 上次有意义的内容变化时间
        stuck_thinking_start = None  # 卡住思考的开始时间
        stuck_interrupt_sent = False  # 是否已发送中断

        # 记录初始屏幕内容，用于检测新内容
        initial_screen = self.bridge.get_screen_content()
        last_screen = initial_screen
        last_screen_hash = hash(initial_screen)  # 使用 hash 比较，更高效
        initial_screen_len = len(initial_screen)
        has_new_content = False  # 是否有新内容（相对于初始屏幕）
        iteration_count = 0  # 迭代计数，防止无限循环

        logger.debug(f"[OpenAI Server] Starting to collect response...")
        logger.debug(f"[OpenAI Server] Initial screen length: {initial_screen_len}")

        # 保存确认完成时的屏幕内容，用于提取响应
        confirmed_screen = None

        # 获取 master_fd 用于发送中断
        master_fd = self.bridge.terminal.master_fd if self.bridge.terminal else None

        while True:
            iteration_count += 1

            # 服务器停止：立刻放弃等待，让 stop() 不被长请求拖住
            if self.bridge.shutdown_event.is_set():
                logger.debug("[OpenAI Server] Shutdown requested, aborting collect")
                break

            # 安全措施：防止无限循环（最多 30000 次迭代，约 50 分钟）
            if iteration_count > 30000:
                logger.debug(f"[OpenAI Server] Max iterations reached: {iteration_count}")
                break

            # 自适应轮询间隔：活跃时快，空闲时慢
            time_since_activity = time.time() - last_activity_time
            poll_interval = 0.08 if time_since_activity < 1.0 else (0.15 if time_since_activity < 3.0 else 0.25)
            output = self.bridge.get_output(timeout=poll_interval)
            current_time = time.time()
            elapsed = current_time - start_time
            idle_time = current_time - last_activity_time
            significant_idle_time = current_time - last_significant_change_time

            if output:
                last_activity_time = current_time

            # 检测屏幕变化（使用缓存提高效率）
            # 如果没有输出且空闲超过1秒，每隔几次迭代才检查屏幕（降低CPU占用）
            should_check_screen = output or (iteration_count % 3 == 0) or idle_time < 1.0
            if not should_check_screen:
                continue

            current_screen = self.bridge.get_screen_content()
            current_hash = hash(current_screen)
            if current_hash != last_screen_hash:
                last_activity_time = current_time
                # 检测是否是有意义的变化（内容长度显著增加）
                if len(current_screen) > len(last_screen) + 5:
                    last_significant_change_time = current_time
                last_screen = current_screen
                last_screen_hash = current_hash
                # 检测是否有新内容（屏幕长度增加或内容变化）
                if len(current_screen) > initial_screen_len or current_screen != initial_screen:
                    has_new_content = True

            # 检测卡住状态：思考中但 0 tokens
            if self.parser.is_stuck_thinking(current_screen):
                if stuck_thinking_start is None:
                    stuck_thinking_start = current_time
                    logger.debug(f"[OpenAI Server] Detected stuck thinking (0 tokens)...")
                else:
                    stuck_duration = current_time - stuck_thinking_start
                    # 如果卡住超过 60 秒，发送 Escape 中断
                    if stuck_duration > 60 and not stuck_interrupt_sent and master_fd:
                        logger.debug(f"[OpenAI Server] Claude stuck for {stuck_duration:.0f}s, sending Escape to interrupt...")
                        try:
                            self.bridge.send_input('\x1b')  # Send Escape
                            time.sleep(0.5)
                            stuck_interrupt_sent = True
                        except Exception as e:
                            logger.warning(f"[OpenAI Server] Failed to send interrupt: {e}")
                    # 如果发送中断后还是卡住超过 90 秒，强制结束
                    if stuck_duration > 90:
                        logger.debug(f"[OpenAI Server] Claude still stuck after interrupt, forcing end...")
                        confirmed_screen = current_screen
                        break
            else:
                # 不再卡住，重置计时器
                if stuck_thinking_start is not None:
                    logger.debug(f"[OpenAI Server] No longer stuck, resuming normal operation")
                stuck_thinking_start = None

            # 主要检测：通过提示符判断响应是否完成
            if has_new_content and self.parser.is_response_complete(current_screen):
                # 立即保存当前屏幕内容
                first_complete_screen = current_screen

                # 短暂确认：检查 2 次，确保不是误判
                confirm_count = 0
                for _ in range(2):
                    time.sleep(0.1)
                    check_screen = self.bridge.get_screen_content()

                    # 如果屏幕内容变化了（可能是新请求开始或继续输出）
                    if check_screen != first_complete_screen:
                        # 检查是否是新请求开始（屏幕内容增加且有 thinking 状态）
                        if len(check_screen) > len(first_complete_screen) or self.parser.is_thinking_status(check_screen):
                            # 新请求开始了，使用之前保存的内容
                            logger.debug(f"[OpenAI Server] Response complete (new request started)")
                            confirmed_screen = first_complete_screen
                            break
                        # 屏幕还在更新，更新保存的内容并继续确认
                        if self.parser.is_response_complete(check_screen):
                            first_complete_screen = check_screen
                        confirm_count = 0
                        continue

                    # 屏幕内容稳定且仍然显示完成状态
                    if self.parser.is_response_complete(check_screen):
                        confirm_count += 1
                    else:
                        confirm_count = 0

                if confirmed_screen:
                    break

                if confirm_count >= 1:
                    logger.debug(f"[OpenAI Server] Response complete (confirmed)")
                    confirmed_screen = first_complete_screen
                    break

            # 备用检测：如果长时间没有变化（用于提示符检测失败的情况）
            if has_new_content and idle_time > self.config.idle_timeout:
                # 再次确认是否真的完成
                if self.parser.is_response_complete(current_screen):
                    logger.debug(f"[OpenAI Server] Response complete (idle + prompt)")
                    confirmed_screen = current_screen  # 保存确认时的屏幕内容
                    break
                logger.debug(f"[OpenAI Server] Idle timeout after receiving new content: {idle_time:.1f}s")
                confirmed_screen = current_screen
                break

            # 如果有新内容但长时间没有有意义的变化，可能是响应结束
            if has_new_content and significant_idle_time > self.config.idle_timeout * 1.5:
                logger.debug(f"[OpenAI Server] No significant change for {significant_idle_time:.1f}s")
                confirmed_screen = current_screen
                break

            # 如果还没收到新内容，使用更长的等待时间（等待 AI 开始响应）
            if not has_new_content and idle_time > 30.0:
                logger.debug(f"[OpenAI Server] No new content received after 30s, giving up")
                confirmed_screen = current_screen
                break

            # 总超时
            if elapsed > self.config.timeout:
                logger.debug(f"[OpenAI Server] Total timeout: {elapsed:.1f}s")
                confirmed_screen = current_screen
                break

        # 从屏幕内容提取响应（使用确认时保存的屏幕内容，避免被后续请求覆盖）
        screen_content = confirmed_screen if confirmed_screen else self.bridge.get_screen_content()
        logger.debug(f"[OpenAI Server] Final screen length: {len(screen_content)}")
        response = self._extract_response_from_screen(screen_content, input_text)
        logger.debug(f"[OpenAI Server] Extracted response: {len(response)} chars")
        return response

    def _handle_stream_response(self, input_text: str, request: dict):
        """处理流式响应"""
        logger.debug(f"[OpenAI Server] Stream request, input: {len(input_text)} chars")

        master_fd = self.bridge.terminal.master_fd if self.bridge.terminal else None
        if master_fd is None:
            self._send_error(500, "Terminal not available")
            return

        # 先检查是否可以开始请求，避免重复触发
        if not self.bridge.start_request():
            self._send_error(429, "Another request is already in progress")
            return

        self._client_disconnected = False  # 重置客户端断开标志

        try:  # 包装整个流程，确保 end_request 被调用
            # 发送 SSE 头
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')  # 使用 close 确保响应完成后连接关闭
            cors_origin = self._get_cors_origin()
            if cors_origin:
                self.send_header('Access-Control-Allow-Origin', cors_origin)
            self.end_headers()

            completion_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
            model = request.get('model', 'terminal-ai')

            # 发送初始 chunk
            self._send_sse_event({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]
            })

            self.parser.reset_content_tracker()

            # 检查输入是否为空
            if not input_text.strip():
                logger.warning("[OpenAI Server] ERROR: input_text is empty!")
                # 发送错误并结束
                self._send_sse_event({
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": "Error: No user message"}, "finish_reason": "stop"}]
                })
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            # 如果启用了清除会话，先发送 /clear 命令
            if self.config.clear_after_query:
                self._send_clear_command_sync()

            # 发送输入文本（不带回车）——经 bridge 信号排队到 GUI 线程写入
            logger.debug(f"[OpenAI Server] Stream: Sending input to terminal: {len(input_text)} chars")
            self.bridge.send_input(input_text)
            logger.debug(f"[OpenAI Server] Stream: Text sent: {len(input_text)} chars, waiting for paste detection...")

            # 等待检测到 [Pasted text 出现，然后发送回车
            paste_detected = False
            for _ in range(50):  # 最多等待 5 秒
                time.sleep(0.1)
                screen = self.bridge.get_screen_content()
                if '[Pasted text' in screen or 'Pasted text' in screen:
                    paste_detected = True
                    logger.debug("[OpenAI Server] Stream: Paste detected, sending Enter...")
                    time.sleep(0.1)
                    self.bridge.send_input('\r')
                    break

            if not paste_detected:
                # 如果没检测到粘贴提示（可能是单行输入），直接发送回车
                logger.debug("[OpenAI Server] Stream: No paste prompt, sending Enter directly...")
                self.bridge.send_input('\r')

            # 流式收集：等待响应完成，然后从屏幕读取
            last_activity_time = time.time()
            last_heartbeat_time = time.time()
            last_significant_change_time = time.time()
            start_time = time.time()
            stuck_thinking_start = None  # 卡住思考的开始时间
            stuck_interrupt_sent = False  # 是否已发送中断

            # 记录初始屏幕内容，用于检测新内容
            initial_screen = self.bridge.get_screen_content()
            last_screen = initial_screen
            last_screen_hash = hash(initial_screen)
            initial_screen_len = len(initial_screen)
            has_new_content = False  # 是否有新内容
            iteration_count = 0  # 迭代计数

            logger.debug(f"[OpenAI Server] Stream: Initial screen length: {initial_screen_len}")

            # 保存确认完成时的屏幕内容，用于提取响应
            confirmed_screen = None

            while True:
                iteration_count += 1

                # 服务器停止：立刻放弃等待，让 stop() 不被长请求拖住
                if self.bridge.shutdown_event.is_set():
                    logger.debug("[OpenAI Server] Stream: Shutdown requested, aborting collect")
                    break

                # 安全措施：防止无限循环
                if iteration_count > 30000:
                    logger.debug(f"[OpenAI Server] Stream: Max iterations reached: {iteration_count}")
                    break

                # 客户端已断开（SSE 写入失败），停止继续等待生成
                if self._client_disconnected:
                    break

                # 自适应轮询间隔：活跃时快，空闲时慢
                time_since_activity = time.time() - last_activity_time
                poll_interval = 0.08 if time_since_activity < 1.0 else (0.15 if time_since_activity < 3.0 else 0.25)
                output = self.bridge.get_output(timeout=poll_interval)
                current_time = time.time()
                elapsed = current_time - start_time
                idle_time = current_time - last_activity_time
                significant_idle_time = current_time - last_significant_change_time

                # 心跳（同时用于探测客户端是否断开）
                if current_time - last_heartbeat_time > 5.0:
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_heartbeat_time = current_time
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        self._client_disconnected = True
                        break

                if output:
                    last_activity_time = current_time

                # 检测屏幕变化（使用缓存提高效率）
                # 如果没有输出且空闲超过1秒，每隔几次迭代才检查屏幕（降低CPU占用）
                should_check_screen = output or (iteration_count % 3 == 0) or idle_time < 1.0
                if not should_check_screen:
                    continue

                current_screen = self.bridge.get_screen_content()
                current_hash = hash(current_screen)
                if current_hash != last_screen_hash:
                    last_activity_time = current_time
                    # 检测有意义的变化
                    if len(current_screen) > len(last_screen) + 5:
                        last_significant_change_time = current_time
                    last_screen = current_screen
                    last_screen_hash = current_hash
                    # 检测是否有新内容
                    if len(current_screen) > initial_screen_len or current_screen != initial_screen:
                        has_new_content = True

                # 检测卡住状态：思考中但 0 tokens
                if self.parser.is_stuck_thinking(current_screen):
                    if stuck_thinking_start is None:
                        stuck_thinking_start = current_time
                        logger.debug(f"[OpenAI Server] Stream: Detected stuck thinking (0 tokens)...")
                    else:
                        stuck_duration = current_time - stuck_thinking_start
                        # 如果卡住超过 60 秒，发送 Escape 中断
                        if stuck_duration > 60 and not stuck_interrupt_sent:
                            logger.debug(f"[OpenAI Server] Stream: Claude stuck for {stuck_duration:.0f}s, sending Escape...")
                            try:
                                self.bridge.send_input('\x1b')  # Send Escape
                                time.sleep(0.5)
                                stuck_interrupt_sent = True
                            except Exception as e:
                                logger.warning(f"[OpenAI Server] Stream: Failed to send interrupt: {e}")
                        # 如果发送中断后还是卡住超过 90 秒，强制结束
                        if stuck_duration > 90:
                            logger.debug(f"[OpenAI Server] Stream: Claude still stuck after interrupt, forcing end...")
                            confirmed_screen = current_screen
                            break
                else:
                    # 不再卡住，重置计时器
                    if stuck_thinking_start is not None:
                        logger.debug(f"[OpenAI Server] Stream: No longer stuck, resuming")
                    stuck_thinking_start = None

                # 主要检测：通过提示符判断响应是否完成
                if has_new_content and self.parser.is_response_complete(current_screen):
                    # 立即保存当前屏幕内容
                    first_complete_screen = current_screen

                    # 短暂确认：检查 2 次，确保不是误判
                    confirm_count = 0
                    for _ in range(2):
                        time.sleep(0.1)
                        check_screen = self.bridge.get_screen_content()

                        # 如果屏幕内容变化了（可能是新请求开始或继续输出）
                        if check_screen != first_complete_screen:
                            # 检查是否是新请求开始（屏幕内容增加且有 thinking 状态）
                            if len(check_screen) > len(first_complete_screen) or self.parser.is_thinking_status(check_screen):
                                # 新请求开始了，使用之前保存的内容
                                logger.debug(f"[OpenAI Server] Stream response complete (new request started)")
                                confirmed_screen = first_complete_screen
                                break
                            # 屏幕还在更新，更新保存的内容并继续确认
                            if self.parser.is_response_complete(check_screen):
                                first_complete_screen = check_screen
                            confirm_count = 0
                            continue

                        # 屏幕内容稳定且仍然显示完成状态
                        if self.parser.is_response_complete(check_screen):
                            confirm_count += 1
                        else:
                            confirm_count = 0

                    if confirmed_screen:
                        break

                    if confirm_count >= 1:
                        logger.debug(f"[OpenAI Server] Stream response complete (confirmed)")
                        confirmed_screen = first_complete_screen
                        break

                # 备用检测：如果长时间没有变化（用于提示符检测失败的情况）
                if has_new_content and idle_time > self.config.idle_timeout:
                    # 再次确认是否真的完成
                    if self.parser.is_response_complete(current_screen):
                        logger.debug(f"[OpenAI Server] Stream response complete (idle + prompt)")
                        confirmed_screen = current_screen  # 保存确认时的屏幕内容
                        break
                    logger.debug(f"[OpenAI Server] Stream idle timeout after new content: {idle_time:.1f}s")
                    confirmed_screen = current_screen
                    break

                # 如果有新内容但长时间没有有意义的变化
                if has_new_content and significant_idle_time > self.config.idle_timeout * 1.5:
                    logger.debug(f"[OpenAI Server] Stream: No significant change for {significant_idle_time:.1f}s")
                    confirmed_screen = current_screen
                    break

                # 如果还没收到新内容，使用更长的等待时间
                if not has_new_content and idle_time > 30.0:
                    logger.debug(f"[OpenAI Server] Stream: No new content after 30s")
                    confirmed_screen = current_screen
                    break

                if elapsed > self.config.timeout:
                    logger.debug(f"[OpenAI Server] Stream timeout: {elapsed:.1f}s")
                    confirmed_screen = current_screen
                    break

            # 客户端已断开：不再提取/发送，直接结束（finally 中 end_request 清理资源）
            if self._client_disconnected:
                logger.debug("[OpenAI Server] Client disconnected, aborting stream response")
                return

            # 从屏幕内容提取最终响应（使用确认时保存的屏幕内容，避免被后续请求覆盖）
            screen_content = confirmed_screen if confirmed_screen else self.bridge.get_screen_content()
            logger.debug(f"[OpenAI Server] Screen content length: {len(screen_content)}")
            final_content = self._extract_response_from_screen(screen_content, input_text)
            logger.debug(f"[OpenAI Server] Extracted content: {len(final_content)} chars")

            if final_content.strip():
                # 发送最终内容
                self._send_sse_event({
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": final_content}, "finish_reason": None}]
                })
                logger.debug(f"[OpenAI Server] Sent final content: {len(final_content)} chars")

            # 发送结束
            self._send_sse_event({
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
            })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

            logger.debug(f"[OpenAI Server] Stream complete: {len(final_content)} chars")

        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self._client_disconnected = True
            logger.debug("[OpenAI Server] Client disconnected")
        except Exception as e:
            logger.error(f"[OpenAI Server] Stream error: {e}")
        finally:
            self.bridge.end_request()

    def _send_clear_command_sync(self, force: bool = False):
        """同步发送 /clear 命令清除 Claude 会话（在新 query 开始前执行）

        所有写入经 bridge 信号排队到 GUI 线程执行（禁止直接写 master_fd）。

        Args:
            force: 是否强制执行（跳过时间间隔检查）
        """
        try:
            # 增加查询计数
            self.bridge._queries_since_reset += 1
            queries = self.bridge._queries_since_reset

            # 检查是否需要强制重置（每 N 次查询）
            need_force_reset = queries >= self.bridge.AUTO_RESET_INTERVAL
            if need_force_reset:
                logger.debug(f"[OpenAI Server] Auto-reset triggered after {queries} queries")
                force = True
                self.bridge._queries_since_reset = 0

            # 检查距离上次 /clear 的时间间隔（除非强制执行）
            if not force:
                current_time = time.time()
                time_since_last_clear = current_time - self.bridge._last_clear_time
                if time_since_last_clear < self.bridge._min_clear_interval:
                    logger.debug(f"[OpenAI Server] Skipping /clear (only {time_since_last_clear:.1f}s since last clear)")
                    return

            # 先检查 Claude 是否还在思考
            screen = self.bridge.get_screen_content()
            is_thinking = self.parser.is_thinking_status(screen)

            # 如果在思考状态，先发送 Escape 中断
            if is_thinking:
                logger.debug("[OpenAI Server] Claude is thinking, sending Escape to interrupt...")
                self.bridge.send_input('\x1b')  # Send Escape
                time.sleep(1.0)

                # 再次检查是否还在思考
                screen = self.bridge.get_screen_content()
                if self.parser.is_thinking_status(screen):
                    # 第二次发送 Escape
                    logger.debug("[OpenAI Server] Still thinking, sending Escape again...")
                    self.bridge.send_input('\x1b')
                    time.sleep(1.0)

                # 等待 Claude 响应中断
                max_wait = 10  # 最多等待 10 秒
                wait_start = time.time()
                while time.time() - wait_start < max_wait:
                    screen = self.bridge.get_screen_content()
                    if not self.parser.is_thinking_status(screen):
                        logger.debug("[OpenAI Server] Claude stopped thinking after interrupt")
                        break
                    time.sleep(0.5)

                # 如果还在思考但是强制模式，继续发送 /clear
                screen = self.bridge.get_screen_content()
                if self.parser.is_thinking_status(screen):
                    if force:
                        logger.debug("[OpenAI Server] Force mode: proceeding with /clear despite thinking state")
                        # 再发一次 Escape 然后继续
                        self.bridge.send_input('\x1b')
                        time.sleep(0.5)
                    else:
                        logger.warning("[OpenAI Server] WARNING: Claude still thinking, skipping /clear")
                        self.bridge._consecutive_clear_failures += 1
                        return

            logger.debug(f"[OpenAI Server] Sending /clear command (query #{queries})...")
            self.bridge.send_input('/clear')
            time.sleep(0.5)
            self.bridge.send_input('\r')  # 发送回车确认选择
            time.sleep(0.5)
            self.bridge.send_input('\r')  # 再发送一次回车执行命令
            time.sleep(1.5)  # 等待 clear 完成

            # 等待终端回到空闲状态
            clear_success = False
            for _ in range(10):
                screen = self.bridge.get_screen_content()
                if self.parser.is_response_complete(screen):
                    clear_success = True
                    break
                time.sleep(0.3)

            # 记录 /clear 完成时间
            self.bridge._last_clear_time = time.time()

            if clear_success:
                self.bridge._consecutive_clear_failures = 0
                logger.debug("[OpenAI Server] /clear command completed")
            else:
                self.bridge._consecutive_clear_failures += 1
                logger.debug(f"[OpenAI Server] /clear may not have completed (failures: {self.bridge._consecutive_clear_failures})")

        except Exception as e:
            self.bridge._consecutive_clear_failures += 1
            logger.error(f"[OpenAI Server] Error sending /clear: {e}")

    def _send_sse_event(self, data: dict):
        try:
            self.wfile.write(f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode('utf-8'))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # 客户端已断开，置位标志让流式循环停止等待生成
            self._client_disconnected = True

    def _send_json(self, data: dict, status: int = 200):
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            cors_origin = self._get_cors_origin()
            if cors_origin:
                self.send_header('Access-Control-Allow-Origin', cors_origin)
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        except BrokenPipeError:
            pass

    def _send_error(self, status: int, message: str):
        self._send_json({"error": {"message": message, "type": "server_error", "code": status}}, status)

    def _extract_response_from_screen(self, screen_content: str, input_text: str) -> str:
        """从屏幕内容中提取 AI 响应 - 只提取最后一次用户输入之后的 AI 回答

        实际逻辑在模块级纯函数 extract_response_from_screen 中（便于单元测试）。
        """
        return extract_response_from_screen(screen_content, input_text)


class _FastBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer，但跳过 server_bind 里的 socket.getfqdn() 反向 DNS。

    标准 HTTPServer.server_bind 会用 getfqdn(host) 设 server_name，在反向 DNS
    不可用/慢的网络（很多 CI runner、公司内网）上会阻塞数秒，把服务器启动
    连带 listening 信号拖慢。server_name 对本地 API 无用，直接取 host 即可。
    """
    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class OpenAICompatServer(QThread):
    """OpenAI 兼容服务器线程

    用 ThreadingHTTPServer（daemon 线程）并发处理请求：单线程 HTTPServer
    会被一个长 chat 请求（最长约 50 分钟）独占，期间 /health、新请求、
    stop() 全部无响应。聊天请求本身仍由 bridge.start_request() 互斥。
    """

    listening = pyqtSignal(int)  # 注意别叫 started —— 会遮蔽 QThread 内建同名信号
    stopped = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, terminal_widget, config: ServerConfig):
        super().__init__()
        self.config = config
        self.bridge = TerminalBridge(terminal_widget, config)
        self.parser = ResponseParser(config)
        self.server: Optional[ThreadingHTTPServer] = None

    def run(self):
        try:
            bridge = self.bridge
            parser = self.parser
            config = self.config

            class HandlerFactory(OpenAIRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, bridge=bridge, parser=parser, config=config, **kwargs)

            self.server = _FastBindHTTPServer(
                (self.config.host, self.config.port), HandlerFactory)
            self.server.daemon_threads = True
            self.listening.emit(self.config.port)

            # serve_forever 由 stop() 里的 server.shutdown() 结束
            self.server.serve_forever(poll_interval=0.5)
            self.server.server_close()

            # 给在途请求一点时间退出（shutdown_event 已置位，收集循环会
            # 尽快返回），再清理 bridge —— 清理与在途请求并发会竞态
            deadline = time.monotonic() + 2.0
            while self.bridge._request_active and time.monotonic() < deadline:
                time.sleep(0.05)
            self.bridge.cleanup()
            self.stopped.emit()

        except Exception as e:
            self.error.emit(str(e))

    def stop(self):
        """非阻塞停止：置停止标志并结束 accept 循环，清理在 run() 尾部完成。

        旧实现的两个问题：在 GUI 线程 wait(3000) 卡界面；bridge.cleanup()
        在此处执行会与仍在跑的请求处理竞态。
        """
        self.bridge.shutdown_event.set()
        srv = self.server
        if srv is not None:
            # shutdown() 会阻塞到 serve_forever 退出（最多一个 poll_interval），
            # 放到一次性线程里执行，GUI 线程完全不等
            threading.Thread(target=srv.shutdown, daemon=True).start()


class PortAllocator:
    """端口分配器"""

    def __init__(self, start_port: int = 8100, end_port: int = 8199):
        self.start_port = start_port
        self.end_port = end_port
        self.allocated: Dict[int, int] = {}

    def allocate(self, tab_index: int, preferred_port: int = None) -> int:
        if preferred_port and self._is_available(preferred_port):
            self.allocated[tab_index] = preferred_port
            return preferred_port

        for port in range(self.start_port, self.end_port + 1):
            if self._is_available(port):
                self.allocated[tab_index] = port
                return port

        raise RuntimeError("没有可用端口")

    def _is_available(self, port: int) -> bool:
        if port in self.allocated.values():
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return True
        except OSError:
            return False

    def release(self, tab_index: int):
        if tab_index in self.allocated:
            del self.allocated[tab_index]

    def get_port(self, tab_index: int) -> Optional[int]:
        return self.allocated.get(tab_index)

    def remap_indices(self, old_to_new: Dict[int, int]):
        """按 old→new 重新映射端口分配的 key；不在 old_to_new 中的（已关闭 tab）丢弃，
        相当于释放其端口。"""
        new_allocated = {}
        for old_idx, port in self.allocated.items():
            new_idx = old_to_new.get(old_idx)
            if new_idx is not None:
                new_allocated[new_idx] = port
        self.allocated = new_allocated


class OpenAIServerManager(QObject):
    """服务器管理器"""

    server_started = pyqtSignal(int, int)
    server_stopped = pyqtSignal(int)
    server_error = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.servers: Dict[int, OpenAICompatServer] = {}
        self.port_allocator = PortAllocator()

    def start_server(self, tab_index: int, terminal_widget, port: int = None) -> int:
        if tab_index in self.servers:
            raise RuntimeError(f"服务器已在 tab {tab_index} 上运行")

        actual_port = self.port_allocator.allocate(tab_index, port)
        # 可选鉴权：设了 STELLAR_OPENAI_API_KEY 就要求 Bearer 校验。默认空=沿用
        # 旧行为（无鉴权，仅 localhost 绑定 + CSRF 头防护），避免切断已有外部工具；
        # 安全意识强的用户可用它把本地任意进程注入终端的入口关掉。
        api_key = (os.environ.get('STELLAR_OPENAI_API_KEY') or '').strip()
        config = ServerConfig(port=actual_port, api_key=api_key)
        server = OpenAICompatServer(terminal_widget, config)
        # tab 索引是位置性的，关闭/分离其他 tab 后会变化；把当前索引存在 server 上，
        # 信号回调动态读取 server.tab_index，配合 remap_indices() 重新映射，避免指向错误 tab。
        server.tab_index = tab_index
        server.listening.connect(lambda p, s=server: self.server_started.emit(s.tab_index, p))
        server.stopped.connect(lambda s=server: self._on_server_stopped(s.tab_index))
        server.error.connect(lambda e, s=server: self.server_error.emit(s.tab_index, e))
        self.servers[tab_index] = server
        server.start()
        return actual_port

    def stop_server(self, tab_index: int):
        if tab_index in self.servers:
            self.servers[tab_index].stop()

    def _on_server_stopped(self, tab_index: int):
        server = self.servers.get(tab_index)
        if server is not None:
            # stopped 信号在 run() 末尾发出，此刻线程即将退出；等它真正结束
            # 再丢弃引用（QThread 对象必须活得比线程久），通常瞬时完成
            server.wait(1000)
            del self.servers[tab_index]
        self.port_allocator.release(tab_index)
        self.server_stopped.emit(tab_index)

    def is_running(self, tab_index: int) -> bool:
        return tab_index in self.servers

    def get_port(self, tab_index: int) -> Optional[int]:
        return self.port_allocator.get_port(tab_index)

    def stop_all(self):
        """停掉所有服务器（应用退出路径）：先全部发停止，再有界等待线程退出。

        单个 stop() 是非阻塞的；这里统一等待可避免进程退出时 QThread
        仍在运行（"Destroyed while thread is still running"）。
        """
        servers = list(self.servers.values())
        for tab_index in list(self.servers.keys()):
            self.stop_server(tab_index)
        deadline = time.monotonic() + 3.0
        for server in servers:
            remaining = max(0.05, deadline - time.monotonic())
            server.wait(int(remaining * 1000))

    def remap_indices(self, old_to_new: Dict[int, int]):
        """tab 关闭/分离后位置变化，按 old→new 重新映射 server 与端口分配的 key。

        old_to_new 只包含仍存活的 tab。不在其中的 server 说明对应 tab 已关闭/分离
        （调用方已先 stop_server），标记为孤儿（tab_index=-1）使其后续 stopped 信号
        成为无害空操作，并从字典中移除。
        """
        new_servers = {}
        for old_idx, server in self.servers.items():
            new_idx = old_to_new.get(old_idx)
            if new_idx is None:
                server.tab_index = -1
                continue
            server.tab_index = new_idx
            new_servers[new_idx] = server
        self.servers = new_servers
        self.port_allocator.remap_indices(old_to_new)
