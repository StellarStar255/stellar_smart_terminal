"""
内置文件编辑器组件
提供在程序内部编辑文件的功能
"""
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QFrame, QMessageBox,
    QSplitter, QLineEdit, QTextEdit, QTextBrowser, QStackedWidget, QScrollArea,
    QSizePolicy, QMenu, QFileDialog, QApplication,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QRect, QSize, QFileSystemWatcher, QTimer, QEvent, QUrl,
)
from PyQt6.QtGui import (
    QFont, QFontMetrics, QColor, QTextCharFormat, QSyntaxHighlighter,
    QKeySequence, QPalette, QShortcut, QPainter, QTextCursor, QTextDocument,
    QPixmap, QImage, QImageReader, QCursor, QGuiApplication, QIcon, QWheelEvent,
    QDesktopServices, QTextBlockFormat, QTextFormat, QTextFrameFormat,
    QTextTable, QFontDatabase, QTextImageFormat, QTextLength,
)


# 支持在编辑器面板里内联预览的图片扩展名
_IMAGE_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff',
    '.ico', '.icns', '.svg', '.heic', '.heif',
}

# 自动保存（崩溃恢复）：备份目录与写入周期
_AUTOSAVE_DIR = os.path.expanduser("~/.smart_terminal_autosave")
_AUTOSAVE_INTERVAL_MS = 30 * 1000

# 切换文件后再切回来时恢复离开时的视图位置（光标 + 滚动）。
# 模块级共享：同一文件在不同窗格间切换也能延续位置；仅内存态，不落盘。
_VIEW_STATE_REGISTRY: dict = {}
_VIEW_STATE_MAX = 200  # 防无限增长；超限时淘汰最早记录的文件


def _looks_binary(raw: bytes) -> bool:
    """含 NUL 字节即视为二进制（与 git 同款启发式）。

    原先靠「UTF-8 解码失败」拒绝二进制文件；引入 latin-1 兜底后解码永不失败，
    改为显式检测 NUL，保持「二进制文件拒绝打开」的既有行为不退化。
    """
    if not raw:
        return False
    return b'\x00' in raw[:8192]


def _decode_with_fallback(raw: bytes) -> tuple[str, str]:
    """按 utf-8 → utf-8-sig → gb18030 → latin-1 的顺序解码 raw。

    返回 (文本, 编码名)；latin-1 兜底永不失败。带 BOM 的 UTF-8 记作
    utf-8-sig（BOM 不进编辑器缓冲区，保存时按 utf-8-sig 写回以保留 BOM）。

    行尾统一归一为 \\n：QPlainTextEdit 载入时本就把 CRLF/CR 折成 LF，而
    is_modified()/外部改动比对拿 toPlainText()（恒为 LF）对照 _original_content，
    若这里不归一，CRLF 文件（Windows 常见）一打开就被判成"已修改"，进而误弹
    外部改动对话框（离屏测试里会卡死）。保存本就只写 LF，归一与之一致。
    """
    def _norm(text: str) -> str:
        return text.replace('\r\n', '\n').replace('\r', '\n')

    if raw.startswith(b'\xef\xbb\xbf'):
        try:
            return _norm(raw.decode('utf-8-sig')), 'utf-8-sig'
        except UnicodeDecodeError:
            pass
    # 注：曾尝试用 charset_normalizer 做"更准"的检测以避免 gb18030/latin-1 误判，
    # 但实测它在短文本上极不可靠（把 GB 编码的中文判成 big5 → 乱码），对以中文
    # 文件为主的场景反而是回归。故维持 utf-8 → gb18030 → latin-1：gb18030 能正确
    # 覆盖中文这一最常见的非 UTF-8 情形，latin-1 兜底永不失败。
    for enc in ('utf-8', 'gb18030'):
        try:
            return _norm(raw.decode(enc)), enc
        except UnicodeDecodeError:
            continue
    return _norm(raw.decode('latin-1')), 'latin-1'


# AI 补全：文件扩展名 → 语言名（用于给模型的提示词上下文）
_AI_LANG_BY_EXT = {
    '.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
    '.mjs': 'javascript', '.cjs': 'javascript', '.ts': 'typescript',
    '.tsx': 'typescript', '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash',
    '.json': 'json', '.yml': 'yaml', '.yaml': 'yaml', '.md': 'markdown',
    '.markdown': 'markdown', '.toml': 'toml', '.ini': 'ini', '.conf': 'ini',
    '.cfg': 'ini', '.html': 'html', '.css': 'css', '.c': 'c', '.h': 'c',
    '.cpp': 'cpp', '.cc': 'cpp', '.hpp': 'cpp', '.go': 'go', '.rs': 'rust',
    '.java': 'java', '.rb': 'ruby', '.php': 'php', '.sql': 'sql',
    '.xml': 'xml', '.lua': 'lua', '.swift': 'swift', '.kt': 'kotlin',
}

from ai_completion import InlineCompletionController
from i18n import t
from app_logging import get_logger

logger = get_logger(__name__)


# OneDark 调色板（与现有 PythonHighlighter / MarkdownHighlighter 一致）
_COLOR_KEYWORD = "#c678dd"
_COLOR_STRING = "#98c379"
_COLOR_COMMENT = "#5c6370"
_COLOR_NUMBER = "#d19a66"
_COLOR_FUNC = "#61afef"
_COLOR_CLASS = "#e5c07b"
_COLOR_VAR = "#e06c75"
_COLOR_OP = "#56b6c2"


def _make_format(color, bold=False, italic=False, underline=False):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Weight.Bold)
    if italic:
        f.setFontItalic(True)
    if underline:
        f.setFontUnderline(True)
    return f


class PythonHighlighter(QSyntaxHighlighter):
    """Python 语法高亮器"""

    KEYWORDS = [
        'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
        'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from',
        'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not',
        'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield',
        'True', 'False', 'None'
    ]

    def __init__(self, document, theme=None):
        super().__init__(document)
        self.theme = theme or {}
        self._init_formats()

    def _init_formats(self):
        """初始化格式"""
        # 关键字格式
        self.keyword_format = QTextCharFormat()
        self.keyword_format.setForeground(QColor("#c678dd"))
        self.keyword_format.setFontWeight(QFont.Weight.Bold)

        # 字符串格式
        self.string_format = QTextCharFormat()
        self.string_format.setForeground(QColor("#98c379"))

        # 注释格式
        self.comment_format = QTextCharFormat()
        self.comment_format.setForeground(QColor("#5c6370"))
        self.comment_format.setFontItalic(True)

        # 数字格式
        self.number_format = QTextCharFormat()
        self.number_format.setForeground(QColor("#d19a66"))

        # 函数名格式
        self.function_format = QTextCharFormat()
        self.function_format.setForeground(QColor("#61afef"))

        # 类名格式
        self.class_format = QTextCharFormat()
        self.class_format.setForeground(QColor("#e5c07b"))

        # 装饰器格式
        self.decorator_format = QTextCharFormat()
        self.decorator_format.setForeground(QColor("#e06c75"))

    def highlightBlock(self, text):
        """高亮一行文本"""
        import re

        # 高亮关键字
        for keyword in self.KEYWORDS:
            pattern = rf'\b{keyword}\b'
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), self.keyword_format)

        # 高亮装饰器
        for match in re.finditer(r'@\w+', text):
            self.setFormat(match.start(), match.end() - match.start(), self.decorator_format)

        # 高亮函数定义
        for match in re.finditer(r'\bdef\s+(\w+)', text):
            self.setFormat(match.start(1), match.end(1) - match.start(1), self.function_format)

        # 高亮类定义
        for match in re.finditer(r'\bclass\s+(\w+)', text):
            self.setFormat(match.start(1), match.end(1) - match.start(1), self.class_format)

        # 高亮数字
        for match in re.finditer(r'\b\d+\.?\d*\b', text):
            self.setFormat(match.start(), match.end() - match.start(), self.number_format)

        # 高亮字符串（简单处理，不处理跨行字符串）
        in_string = False
        string_char = None
        string_start = 0

        i = 0
        while i < len(text):
            char = text[i]

            if not in_string:
                if char in '"\'':
                    # 检查是否是三引号
                    if text[i:i+3] in ('"""', "'''"):
                        in_string = True
                        string_char = text[i:i+3]
                        string_start = i
                        i += 3
                        continue
                    else:
                        in_string = True
                        string_char = char
                        string_start = i
            else:
                if len(string_char) == 3:
                    if text[i:i+3] == string_char:
                        self.setFormat(string_start, i + 3 - string_start, self.string_format)
                        in_string = False
                        i += 3
                        continue
                elif char == string_char and (i == 0 or text[i-1] != '\\'):
                    self.setFormat(string_start, i + 1 - string_start, self.string_format)
                    in_string = False

            i += 1

        # 如果字符串延续到行尾
        if in_string:
            self.setFormat(string_start, len(text) - string_start, self.string_format)

        # 高亮注释（在字符串处理之后，避免高亮字符串中的 #）
        comment_start = -1
        in_str = False
        str_char = None
        for i, char in enumerate(text):
            if not in_str:
                if char in '"\'':
                    in_str = True
                    str_char = char
                elif char == '#':
                    comment_start = i
                    break
            else:
                if char == str_char and (i == 0 or text[i-1] != '\\'):
                    in_str = False

        if comment_start >= 0:
            self.setFormat(comment_start, len(text) - comment_start, self.comment_format)


class MarkdownHighlighter(QSyntaxHighlighter):
    """Markdown 语法高亮器"""

    def __init__(self, document, theme=None):
        super().__init__(document)
        self.theme = theme or {}
        self._init_formats()

    def _init_formats(self):
        """初始化格式"""
        import re
        self.re = re

        # 标题格式 — 红色加粗
        self.heading_format = QTextCharFormat()
        self.heading_format.setForeground(QColor("#e06c75"))
        self.heading_format.setFontWeight(QFont.Weight.Bold)

        # 粗体格式
        self.bold_format = QTextCharFormat()
        self.bold_format.setFontWeight(QFont.Weight.Bold)

        # 斜体格式
        self.italic_format = QTextCharFormat()
        self.italic_format.setFontItalic(True)

        # 代码块围栏格式 — 灰色
        self.fence_format = QTextCharFormat()
        self.fence_format.setForeground(QColor("#5c6370"))

        # 代码块内容格式 — 绿色
        self.code_block_format = QTextCharFormat()
        self.code_block_format.setForeground(QColor("#98c379"))

        # 行内代码格式 — 绿色
        self.inline_code_format = QTextCharFormat()
        self.inline_code_format.setForeground(QColor("#98c379"))

        # 链接格式 — 蓝色下划线
        self.link_format = QTextCharFormat()
        self.link_format.setForeground(QColor("#61afef"))
        self.link_format.setFontUnderline(True)

        # 列表标记格式 — 橙色
        self.list_format = QTextCharFormat()
        self.list_format.setForeground(QColor("#d19a66"))

        # 引用格式 — 灰色斜体
        self.blockquote_format = QTextCharFormat()
        self.blockquote_format.setForeground(QColor("#5c6370"))
        self.blockquote_format.setFontItalic(True)

        # 分隔线格式 — 灰色
        self.hr_format = QTextCharFormat()
        self.hr_format.setForeground(QColor("#5c6370"))

    def highlightBlock(self, text):
        """高亮一行文本"""
        re = self.re

        # --- 代码块跨行状态管理 ---
        # state: -1 = 不在代码块中, 1 = 在代码块中
        prev_state = self.previousBlockState()
        in_code_block = (prev_state == 1)

        # 检查本行是否是围栏行
        fence_match = re.match(r'^(`{3,}|~{3,})(.*)?$', text)

        if fence_match:
            # 围栏行始终用围栏格式
            self.setFormat(0, len(text), self.fence_format)
            if in_code_block:
                # 关闭代码块
                self.setCurrentBlockState(-1)
            else:
                # 打开代码块
                self.setCurrentBlockState(1)
            return

        if in_code_block:
            # 代码块内容全行绿色
            self.setFormat(0, len(text), self.code_block_format)
            self.setCurrentBlockState(1)
            return

        # 不在代码块中
        self.setCurrentBlockState(-1)

        # --- 分隔线 ---
        if re.match(r'^\s*([-*_])\s*\1\s*\1[\s\1]*$', text) and len(text.strip()) >= 3:
            self.setFormat(0, len(text), self.hr_format)
            return

        # --- 标题 ---
        heading_match = re.match(r'^(#{1,6})\s', text)
        if heading_match:
            self.setFormat(0, len(text), self.heading_format)
            return

        # --- 引用 ---
        if re.match(r'^\s*>', text):
            self.setFormat(0, len(text), self.blockquote_format)
            # 引用内部继续匹配其他元素（不 return）

        # --- 列表标记 ---
        list_match = re.match(r'^(\s*)([-*+]|\d+\.)\s', text)
        if list_match:
            start = list_match.start(2)
            length = list_match.end(2) - start
            self.setFormat(start, length, self.list_format)

        # --- 行内代码 ---
        for m in re.finditer(r'`([^`]+)`', text):
            self.setFormat(m.start(), m.end() - m.start(), self.inline_code_format)

        # --- 粗体 **text** 或 __text__ ---
        for m in re.finditer(r'(\*\*|__)(.+?)\1', text):
            self.setFormat(m.start(), m.end() - m.start(), self.bold_format)

        # --- 斜体 *text* 或 _text_ (不匹配已被粗体匹配的) ---
        for m in re.finditer(r'(?<!\*)(\*(?!\*)(.+?)(?<!\*)\*)(?!\*)', text):
            self.setFormat(m.start(), m.end() - m.start(), self.italic_format)
        for m in re.finditer(r'(?<!_)(_(?!_)(.+?)(?<!_)_)(?!_)', text):
            self.setFormat(m.start(), m.end() - m.start(), self.italic_format)

        # --- 链接 [text](url) ---
        for m in re.finditer(r'\[([^\]]*)\]\(([^)]*)\)', text):
            self.setFormat(m.start(), m.end() - m.start(), self.link_format)

        # --- 裸 URL ---
        for m in re.finditer(r'https?://[^\s<>\)]+', text):
            self.setFormat(m.start(), m.end() - m.start(), self.link_format)


class GenericHighlighter(QSyntaxHighlighter):
    """规则驱动的通用语法高亮器

    rules: list of (compiled_regex, fmt, group, claim)
        - claim=True 的匹配（通常是字符串/行注释）会标记该区间，
          后续规则在被标记区间内不再上色，避免「字符串里出现的 # 被当成注释」这类覆盖。
    block_rules: list of (start_regex, end_regex, fmt)
        - 支持跨行块（如 /* */ 或模板字符串），使用 blockState 维持状态，
          匹配到的区间自动 claim。
    """

    def __init__(self, document, rules, block_rules=None, theme=None):
        super().__init__(document)
        self.theme = theme or {}
        self.rules = rules
        self.block_rules = block_rules or []

    def highlightBlock(self, text):
        self.setCurrentBlockState(0)
        blocked = []

        for idx, (start_re, end_re, fmt) in enumerate(self.block_rules, start=1):
            state_id = idx
            cursor = 0

            if self.previousBlockState() == state_id:
                end_m = end_re.search(text)
                if end_m:
                    length = end_m.end()
                    self.setFormat(0, length, fmt)
                    blocked.append((0, length))
                    cursor = length
                else:
                    self.setFormat(0, len(text), fmt)
                    blocked.append((0, len(text)))
                    self.setCurrentBlockState(state_id)
                    continue

            while cursor < len(text):
                start_m = start_re.search(text, cursor)
                if not start_m:
                    break
                s = start_m.start()
                end_m = end_re.search(text, start_m.end())
                if end_m:
                    e = end_m.end()
                    self.setFormat(s, e - s, fmt)
                    blocked.append((s, e))
                    cursor = e
                else:
                    self.setFormat(s, len(text) - s, fmt)
                    blocked.append((s, len(text)))
                    self.setCurrentBlockState(state_id)
                    break

        def _covered(pos):
            for s, e in blocked:
                if s <= pos < e:
                    return True
            return False

        for rule in self.rules:
            if len(rule) == 4:
                pattern, fmt, group, claim = rule
            else:
                pattern, fmt, group = rule
                claim = False
            for m in pattern.finditer(text):
                try:
                    start = m.start(group)
                    end = m.end(group)
                except IndexError:
                    continue
                if start < 0 or _covered(start):
                    continue
                self.setFormat(start, end - start, fmt)
                if claim:
                    blocked.append((start, end))


# ---------- 各语言规则表 ----------

def _shell_rules():
    keywords = (
        r'\b(if|then|else|elif|fi|for|while|until|do|done|case|esac|in|'
        r'function|return|break|continue|local|export|readonly|declare|'
        r'typeset|source|alias|select|time|eval|exec|trap|set|unset|shift|'
        r'true|false|exit)\b'
    )
    builtins = (
        r'\b(echo|printf|cd|pwd|read|test|kill|wait|getopts|popd|pushd|'
        r'dirs|jobs|bg|fg|type|which|command|builtin|enable|mapfile|readarray)\b'
    )
    kw = _make_format(_COLOR_KEYWORD, bold=True)
    bt = _make_format(_COLOR_FUNC)
    st = _make_format(_COLOR_STRING)
    cm = _make_format(_COLOR_COMMENT, italic=True)
    vr = _make_format(_COLOR_VAR)
    nm = _make_format(_COLOR_NUMBER)
    fn = _make_format(_COLOR_FUNC, bold=True)

    rules = [
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r"'[^']*'"), st, 0, True),
        (re.compile(r'`[^`]*`'), st, 0, True),
        (re.compile(r'#.*$'), cm, 0, True),
        (re.compile(r'^\s*(\w+)\s*\(\s*\)'), fn, 1, False),
        (re.compile(r'\bfunction\s+(\w+)'), fn, 1, False),
        (re.compile(keywords), kw, 0, False),
        (re.compile(builtins), bt, 0, False),
        (re.compile(r'\$\{[^}]*\}'), vr, 0, False),
        (re.compile(r'\$\([^)]*\)'), vr, 0, False),
        (re.compile(r'\$\w+'), vr, 0, False),
        (re.compile(r'\b\d+\b'), nm, 0, False),
    ]
    return rules, []


def _json_rules():
    key = _make_format(_COLOR_VAR)
    st = _make_format(_COLOR_STRING)
    nm = _make_format(_COLOR_NUMBER)
    bl = _make_format(_COLOR_KEYWORD, bold=True)

    rules = [
        (re.compile(r'"(?:[^"\\]|\\.)*"\s*(?=:)'), key, 0, True),
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r'-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b'), nm, 0, False),
        (re.compile(r'\b(true|false|null)\b'), bl, 0, False),
    ]
    return rules, []


def _yaml_rules():
    key = _make_format(_COLOR_VAR)
    st = _make_format(_COLOR_STRING)
    cm = _make_format(_COLOR_COMMENT, italic=True)
    nm = _make_format(_COLOR_NUMBER)
    kw = _make_format(_COLOR_KEYWORD, bold=True)
    anchor = _make_format(_COLOR_CLASS)
    dash = _make_format(_COLOR_OP)

    rules = [
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r"'(?:[^'\\]|\\.)*'"), st, 0, True),
        (re.compile(r'#.*$'), cm, 0, True),
        (re.compile(r'^\s*-\s+([\w.\-]+)(?=\s*:)'), key, 1, False),
        (re.compile(r'^\s*([\w.\-]+)(?=\s*:)'), key, 1, False),
        (re.compile(r'^\s*(-)\s'), dash, 1, False),
        (re.compile(r'[&*][\w\-]+'), anchor, 0, False),
        (re.compile(r'\b(true|false|yes|no|on|off|null|True|False|Null|TRUE|FALSE|YES|NO)\b'), kw, 0, False),
        (re.compile(r'(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])'), nm, 0, False),
    ]
    return rules, []


def _js_ts_rules(is_ts=False):
    kws = [
        'const', 'let', 'var', 'function', 'if', 'else', 'for', 'while', 'do',
        'return', 'class', 'extends', 'import', 'export', 'from', 'as',
        'async', 'await', 'try', 'catch', 'finally', 'throw', 'new', 'this',
        'super', 'typeof', 'instanceof', 'in', 'of', 'switch', 'case',
        'default', 'break', 'continue', 'yield', 'delete', 'void',
        'true', 'false', 'null', 'undefined', 'static', 'get', 'set',
    ]
    if is_ts:
        kws += [
            'interface', 'type', 'enum', 'implements', 'public', 'private',
            'protected', 'readonly', 'namespace', 'declare', 'abstract',
            'keyof', 'infer', 'is', 'any', 'unknown', 'never', 'number',
            'string', 'boolean', 'object',
        ]
    kw = _make_format(_COLOR_KEYWORD, bold=True)
    st = _make_format(_COLOR_STRING)
    cm = _make_format(_COLOR_COMMENT, italic=True)
    nm = _make_format(_COLOR_NUMBER)
    fn = _make_format(_COLOR_FUNC)
    cls = _make_format(_COLOR_CLASS)

    block_rules = [
        (re.compile(r'/\*'), re.compile(r'\*/'), cm),
        (re.compile(r'`'), re.compile(r'`'), st),
    ]
    rules = [
        (re.compile(r'//.*$'), cm, 0, True),
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r"'(?:[^'\\]|\\.)*'"), st, 0, True),
        (re.compile(r'\b(' + '|'.join(kws) + r')\b'), kw, 0, False),
        (re.compile(r'\b([A-Z][A-Za-z0-9_]*)\b'), cls, 1, False),
        (re.compile(r'\b([a-zA-Z_]\w*)(?=\s*\()'), fn, 1, False),
        (re.compile(r'\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b'), nm, 0, False),
    ]
    return rules, block_rules


def _dockerfile_rules():
    instructions = (
        r'^\s*(FROM|RUN|CMD|LABEL|MAINTAINER|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|'
        r'VOLUME|USER|WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL)\b'
    )
    kw = _make_format(_COLOR_KEYWORD, bold=True)
    st = _make_format(_COLOR_STRING)
    cm = _make_format(_COLOR_COMMENT, italic=True)
    vr = _make_format(_COLOR_VAR)

    rules = [
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r"'(?:[^'\\]|\\.)*'"), st, 0, True),
        (re.compile(r'#.*$'), cm, 0, True),
        (re.compile(instructions), kw, 1, False),
        (re.compile(r'\$\{[^}]*\}'), vr, 0, False),
        (re.compile(r'\$\w+'), vr, 0, False),
    ]
    return rules, []


def _makefile_rules():
    directives = (
        r'^\s*(include|sinclude|-include|ifeq|ifneq|ifdef|ifndef|else|endif|'
        r'define|endef|export|unexport|override|vpath)\b'
    )
    kw = _make_format(_COLOR_KEYWORD, bold=True)
    st = _make_format(_COLOR_STRING)
    cm = _make_format(_COLOR_COMMENT, italic=True)
    vr = _make_format(_COLOR_VAR)
    target = _make_format(_COLOR_FUNC, bold=True)
    auto = _make_format(_COLOR_CLASS)

    rules = [
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r"'(?:[^'\\]|\\.)*'"), st, 0, True),
        (re.compile(r'#.*$'), cm, 0, True),
        (re.compile(r'^([A-Za-z0-9_.\-%\s]+?)(?=:[^=])'), target, 1, False),
        (re.compile(directives), kw, 1, False),
        (re.compile(r'\$\([^)]*\)'), vr, 0, False),
        (re.compile(r'\$\{[^}]*\}'), vr, 0, False),
        (re.compile(r'\$[@<^%*+?|]'), auto, 0, False),
    ]
    return rules, []


def _ini_rules():
    section = _make_format(_COLOR_CLASS, bold=True)
    key = _make_format(_COLOR_VAR)
    st = _make_format(_COLOR_STRING)
    cm = _make_format(_COLOR_COMMENT, italic=True)
    nm = _make_format(_COLOR_NUMBER)
    bl = _make_format(_COLOR_KEYWORD, bold=True)

    rules = [
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r"'(?:[^'\\]|\\.)*'"), st, 0, True),
        (re.compile(r'[#;].*$'), cm, 0, True),
        (re.compile(r'^\s*(\[[^\]]*\])'), section, 1, False),
        (re.compile(r'^\s*([\w.\-]+)(?=\s*=)'), key, 1, False),
        (re.compile(r'\b(true|false|True|False|TRUE|FALSE)\b'), bl, 0, False),
        (re.compile(r'(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])'), nm, 0, False),
    ]
    return rules, []


def _python_rules():
    """Python 的规则表版本（供 markdown 预览代码块用；编辑器仍用
    PythonHighlighter——其命令式字符串/注释处理更精细，行为保持不变）。"""
    kw = _make_format(_COLOR_KEYWORD, bold=True)
    st = _make_format(_COLOR_STRING)
    cm = _make_format(_COLOR_COMMENT, italic=True)
    nm = _make_format(_COLOR_NUMBER)
    fn = _make_format(_COLOR_FUNC)
    cls_f = _make_format(_COLOR_CLASS)
    dec = _make_format(_COLOR_VAR)
    keywords = (
        r'\b(and|as|assert|async|await|break|class|continue|def|del|elif|'
        r'else|except|finally|for|from|global|if|import|in|is|lambda|'
        r'nonlocal|not|or|pass|raise|return|try|while|with|yield|'
        r'True|False|None|self)\b'
    )
    rules = [
        (re.compile(r'"(?:[^"\\]|\\.)*"'), st, 0, True),
        (re.compile(r"'(?:[^'\\]|\\.)*'"), st, 0, True),
        (re.compile(r'#.*$'), cm, 0, True),
        (re.compile(r'\bdef\s+(\w+)'), fn, 1, False),
        (re.compile(r'\bclass\s+(\w+)'), cls_f, 1, False),
        (re.compile(r'^\s*@[\w.]+'), dec, 0, False),
        (re.compile(keywords), kw, 0, False),
        (re.compile(r'\b\d+\.?\d*\b'), nm, 0, False),
    ]
    blocks = [
        (re.compile(r'"""'), re.compile(r'"""'), st),
        (re.compile(r"'''"), re.compile(r"'''"), st),
    ]
    return rules, blocks


class _MdCodeHighlighter(GenericHighlighter):
    """markdown 预览专用：只给代码块（带 BlockCodeLanguage 属性）按语言上色。

    复用 GenericHighlighter 的规则引擎，逐 block 按 codelang 换规则表。
    用 QSyntaxHighlighter 层而不是在精修 pass 里直接改 charFormat：
    高亮作为叠加层不与精修的等宽字体/字号 merge 互相覆盖，且 setMarkdown
    重渲染后自动重跑，无需手动触发。
    """

    # codelang（含常见别名）→ 规则表工厂
    _LANG_FACTORY = {
        'python': _python_rules, 'py': _python_rules, 'python3': _python_rules,
        'sh': _shell_rules, 'bash': _shell_rules, 'shell': _shell_rules,
        'zsh': _shell_rules, 'console': _shell_rules,
        'json': _json_rules, 'jsonc': _json_rules,
        'yaml': _yaml_rules, 'yml': _yaml_rules,
        'js': lambda: _js_ts_rules(False), 'javascript': lambda: _js_ts_rules(False),
        'jsx': lambda: _js_ts_rules(False), 'mjs': lambda: _js_ts_rules(False),
        'ts': lambda: _js_ts_rules(True), 'typescript': lambda: _js_ts_rules(True),
        'tsx': lambda: _js_ts_rules(True),
        'dockerfile': _dockerfile_rules, 'docker': _dockerfile_rules,
        'makefile': _makefile_rules, 'make': _makefile_rules,
        'toml': _ini_rules, 'ini': _ini_rules, 'conf': _ini_rules,
        'properties': _ini_rules,
    }

    def __init__(self, document):
        super().__init__(document, rules=[], block_rules=[])
        self._cache = {}   # lang → (rules, block_rules)

    def highlightBlock(self, text):
        bf = self.currentBlock().blockFormat()
        lang = (bf.stringProperty(QTextFormat.Property.BlockCodeLanguage)
                or '').strip().lower()
        factory = self._LANG_FACTORY.get(lang)
        if factory is None:
            # 非代码块 / 未知语言：清状态，防止跨行块状态漏到下一个代码片
            self.setCurrentBlockState(0)
            return
        if lang not in self._cache:
            self._cache[lang] = factory()
        self.rules, self.block_rules = self._cache[lang]
        super().highlightBlock(text)


class _LineNumberArea(QWidget):
    """左侧行号条 — 由 CodeEditor 管理绘制"""

    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.line_number_area_paint_event(event)


class CodeEditor(QPlainTextEdit):
    """带左侧行号条的编辑器"""

    # 由右键菜单 / 快捷键触发，向上转发给 FileEditorWidget → EditorArea
    split_h_requested = pyqtSignal()
    split_v_requested = pyqtSignal()
    close_split_requested = pyqtSignal()
    move_left_requested = pyqtSignal()
    move_up_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_number_area = _LineNumberArea(self)
        self._line_number_fg = QColor("#5c6370")
        self._line_number_bg = QColor("#21252b")
        self._current_line_color = QColor("#2c313a")
        # 由 _SearchBar 等外部组件注入的额外高亮；与当前行高亮一起合并显示
        self._search_selections: list = []

        self.blockCountChanged.connect(lambda _=0: self._update_viewport_margin())
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self._apply_extra_selections)
        self._update_viewport_margin()
        self._apply_extra_selections()

        # AI 行内补全（灰字建议）。默认关闭，由编辑器标题栏的 ✦ 开关启用。
        self._ai = InlineCompletionController(self)
        self.textChanged.connect(self._ai.on_text_changed)
        self.cursorPositionChanged.connect(self._ai.on_cursor_moved)

    # ---------- AI 行内补全 ----------

    def paintEvent(self, event):
        """在正常文本之上叠加灰字建议。"""
        super().paintEvent(event)
        if self._ai is not None and self._ai.has_suggestion():
            painter = QPainter(self.viewport())
            try:
                self._ai.paint(painter)
            finally:
                painter.end()

    def focusOutEvent(self, event):
        # 失焦时撤掉灰字建议，避免残留
        if self._ai is not None:
            self._ai.dismiss()
        super().focusOutEvent(event)

    def set_ai_completion_enabled(self, enabled: bool):
        if self._ai is not None:
            self._ai.set_enabled(bool(enabled))

    def set_word_wrap(self, enabled: bool):
        """切换自动换行：开 → 按控件宽度整行换行；关 → 不换行（横向滚动）。"""
        self.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if enabled
            else QPlainTextEdit.LineWrapMode.NoWrap)

    def set_ai_ghost_color(self, color):
        if self._ai is not None:
            self._ai.set_color(color)

    def set_ai_ghost_bg(self, color):
        if self._ai is not None:
            self._ai.set_bg_color(color)

    def set_ai_ghost_fg(self, color):
        """编辑器正文颜色：行中触发时用于重画被灰字推后的剩余文本。"""
        if self._ai is not None:
            self._ai.set_fg_color(color)

    def ai_get_config(self):
        """向上找主窗口拿补全用的 LLM 配置（优先专用「completion/补全」配置，
        否则回退默认配置；均为 OpenAI 兼容）。"""
        win = self.window()
        getter = (getattr(win, 'get_completion_llm_config', None)
                  or getattr(win, 'get_llm_config', None))
        if getter is None:
            return None
        try:
            return getter()
        except Exception:
            return None

    def ai_get_language(self) -> str:
        """根据当前文件扩展名推断语言，供提示词使用。"""
        w = self.parent()
        while w is not None and not hasattr(w, 'get_current_file'):
            w = w.parent()
        fp = None
        if w is not None:
            try:
                fp = w.get_current_file()
            except Exception:
                fp = None
        if not fp:
            return 'text'
        name = Path(fp).name.lower()
        if name == 'dockerfile':
            return 'dockerfile'
        if name == 'makefile':
            return 'makefile'
        ext = Path(fp).suffix.lower()
        return _AI_LANG_BY_EXT.get(ext, ext.lstrip('.') or 'text')

    def wheelEvent(self, event):
        """按住 Cmd/Ctrl 滚轮：当作普通滚动，绝不缩放字体。

        QPlainTextEdit 自带「Ctrl+滚轮缩放字体」，触控板上极易误触。这里把按住
        修饰键的滚轮事件改写成「无修饰键」后再交给基类，从而保留原生滚动（含触控板
        惯性），但不会触发字体缩放。需要缩放请用 Cmd +/-。
        """
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.MetaModifier):
            stripped = QWheelEvent(
                event.position(), event.globalPosition(),
                event.pixelDelta(), event.angleDelta(),
                event.buttons(), Qt.KeyboardModifier.NoModifier,
                event.phase(), event.inverted(),
            )
            super().wheelEvent(stripped)
            event.accept()
            return
        super().wheelEvent(event)

    def contextMenuEvent(self, event):
        """在默认编辑菜单（撤销/剪切/复制/粘贴…）末尾追加分屏项。"""
        menu = self.createStandardContextMenu()
        self.append_pane_actions(menu)
        menu.exec(event.globalPos())

    def append_pane_actions(self, menu):
        """向 menu 末尾追加窗格级动作（保存/自动换行/分屏/移动）。

        源码页与 Markdown 预览页共用：预览页的 QTextBrowser 没有这些项，
        由 FileEditorWidget 把预览页的标准菜单传进来复用，信号仍从本编辑器
        冒泡到所属窗格，行为与源码页完全一致。
        """
        # 编辑器自身的 `QWidget {background-color}` 样式表会级联到这个子菜单的每个
        # item，盖住 Qt 默认的 hover 高亮。这里补回 :selected 高亮并把常态底色置透明。
        area = self._find_editor_area()
        theme = area.theme if area is not None else {}
        accent = theme.get('accent', '#667eea')
        text_dim = theme.get('text_dim', '#888888')
        menu.setStyleSheet(f"""
            QMenu::item {{ background-color: transparent; }}
            QMenu::item:selected {{ background-color: {accent}; color: #ffffff; }}
            QMenu::item:disabled {{ color: {text_dim}; background-color: transparent; }}
        """)

        menu.addSeparator()
        # 用菜单当前文字色绘制图标，自动适配深/浅主题
        icon_color = menu.palette().color(QPalette.ColorRole.WindowText)
        # 保存当前文件（与 Cmd+S 一致），仅在有打开文件时可用
        act_save = menu.addAction(self._split_menu_icon('save', icon_color), t("editor.save_menu"))
        pane_for_save = self._find_pane()
        act_save.setEnabled(bool(pane_for_save and pane_for_save.get_current_file()))
        act_save.triggered.connect(self._context_save)

        # 自动换行（全局开关，所有窗格统一、重启记住）。改动经 area 信号冒泡到主窗口落盘。
        act_wrap = menu.addAction(t("editor.word_wrap_menu"))
        act_wrap.setCheckable(True)
        act_wrap.setChecked(bool(area.is_word_wrap_enabled()) if area is not None else False)
        if area is not None:
            act_wrap.triggered.connect(
                lambda checked: area.word_wrap_toggled.emit(bool(checked)))
        else:
            act_wrap.setEnabled(False)

        menu.addSeparator()
        act_h = menu.addAction(self._split_menu_icon('h', icon_color), t("editor.split_h_menu"))
        act_h.triggered.connect(self.split_h_requested.emit)
        act_v = menu.addAction(self._split_menu_icon('v', icon_color), t("editor.split_v_menu"))
        act_v.triggered.connect(self.split_v_requested.emit)
        # 关闭当前分屏：仅在编辑器组里有多个窗格时可用（area 已在上方求得）
        act_close = menu.addAction(self._split_menu_icon('close', icon_color), t("editor.close_split_menu"))
        act_close.setEnabled(bool(area and len(area.panes) > 1))
        act_close.triggered.connect(self.close_split_requested.emit)

        # 移动当前分屏（与终端一致）：仅在有多个窗格时出现
        pane = self._find_pane()
        if area is not None and pane is not None and len(area.panes) > 1:
            menu.addSeparator()
            act_left = menu.addAction(self._split_menu_icon('left', icon_color), t("editor.move_left_menu"))
            act_left.setEnabled(area.can_move_left(pane))
            act_left.triggered.connect(self.move_left_requested.emit)
            act_up = menu.addAction(self._split_menu_icon('up', icon_color), t("editor.move_up_menu"))
            act_up.setEnabled(area.can_move_up(pane))
            act_up.triggered.connect(self.move_up_requested.emit)

    @staticmethod
    def _split_menu_icon(kind: str, color: QColor) -> QIcon:
        """画一个 16×16 的菜单图标：'h' 竖分隔、'v' 横分隔、'close' 打叉、
        'left' 左箭头、'up' 上箭头。"""
        pm = QPixmap(16, 16)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(color)
        if kind in ('h', 'v', 'close'):
            rect = QRect(3, 4, 10, 8)  # x3..13, y4..12, 中心 (8, 8)
            painter.drawRoundedRect(rect, 2, 2)
            if kind == 'h':
                painter.drawLine(8, 4, 8, 12)       # 竖向分隔 → 左右分屏
            elif kind == 'v':
                painter.drawLine(3, 8, 13, 8)       # 横向分隔 → 上下分屏
            elif kind == 'close':
                painter.drawLine(5, 6, 11, 10)      # × 关闭
                painter.drawLine(11, 6, 5, 10)
        elif kind == 'left':                        # ← 左箭头
            painter.drawLine(4, 8, 12, 8)
            painter.drawLine(4, 8, 8, 4)
            painter.drawLine(4, 8, 8, 12)
        elif kind == 'up':                          # ↑ 上箭头
            painter.drawLine(8, 4, 8, 12)
            painter.drawLine(8, 4, 4, 8)
            painter.drawLine(8, 4, 12, 8)
        elif kind == 'save':                        # 软盘（保存）
            painter.drawRoundedRect(QRect(3, 3, 10, 10), 1, 1)  # 外壳
            painter.drawRect(QRect(5, 3, 6, 3))                 # 顶部滑片
            painter.drawRect(QRect(6, 9, 4, 3))                 # 底部标签
        painter.end()
        return QIcon(pm)

    def _find_editor_area(self):
        """沿父链向上找到所属的 EditorArea（找不到返回 None）。"""
        p = self.parent()
        while p is not None:
            if type(p).__name__ == 'EditorArea':
                return p
            p = p.parent()
        return None

    def _find_pane(self):
        """沿父链向上找到所属的 FileEditorWidget 窗格（找不到返回 None）。"""
        p = self.parent()
        while p is not None:
            if type(p).__name__ == 'FileEditorWidget':
                return p
            p = p.parent()
        return None

    def _context_save(self):
        """右键菜单「保存」：存当前编辑器所属窗格的文件。"""
        pane = self._find_pane()
        if pane is not None:
            pane.save_file()

    def line_number_area_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return self.fontMetrics().horizontalAdvance('9') * digits + 12

    def _update_viewport_margin(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _on_update_request(self, rect, dy):
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0, rect.y(), self._line_number_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_viewport_margin()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def focusNextPrevChild(self, _next):
        # 禁用 Tab / Shift+Tab 的焦点切换；否则 Backtab 在到达 keyPressEvent
        # 之前就被 Qt 焦点遍历机制吃掉，导致 Shift+Tab 反缩进失效。
        return False

    def line_number_area_paint_event(self, event):
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), self._line_number_bg)
        painter.setFont(self.font())
        painter.setPen(self._line_number_fg)

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        line_height = self.fontMetrics().height()
        right_pad = 6
        width = self._line_number_area.width() - right_pad

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0, top, width, line_height,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(block_number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def set_line_number_colors(self, fg: str, bg: str):
        self._line_number_fg = QColor(fg)
        self._line_number_bg = QColor(bg)
        self._line_number_area.update()

    def set_current_line_color(self, color: str):
        self._current_line_color = QColor(color)
        self._apply_extra_selections()

    def set_search_selections(self, selections: list):
        """供 _SearchBar 调用：注入搜索高亮，由 CodeEditor 与当前行一起合并"""
        self._search_selections = list(selections or [])
        self._apply_extra_selections()

    def _apply_extra_selections(self):
        """把当前行高亮 + 搜索高亮合并后写回 setExtraSelections。
        当前行放最前，避免覆盖搜索的命中色。"""
        selections = []
        if not self.isReadOnly():
            line_sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(self._current_line_color)
            fmt.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
            line_sel.format = fmt
            cursor = self.textCursor()
            cursor.clearSelection()
            line_sel.cursor = cursor
            selections.append(line_sel)
        selections.extend(self._search_selections)
        self.setExtraSelections(selections)

    # ---- Tab / Shift+Tab：4 空格缩进，支持多行选区 ----
    INDENT_UNIT = '    '
    _AUTO_PAIRS = {'(': ')', '[': ']', '{': '}', '"': '"', "'": "'"}

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        text = event.text()
        plain_modifier = not (
            mods & (Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.MetaModifier
                    | Qt.KeyboardModifier.AltModifier)
        )

        # AI 行内补全：有灰字建议时 Tab 接受 / Esc 取消（优先于缩进等逻辑）
        if self._ai is not None and self._ai.has_suggestion():
            if key == Qt.Key.Key_Tab and plain_modifier:
                if self._ai.accept():
                    event.accept()
                    return
            if key == Qt.Key.Key_Escape:
                self._ai.dismiss()
                event.accept()
                return
        # AI 手动触发：Alt+\
        if (self._ai is not None and key == Qt.Key.Key_Backslash
                and (mods & Qt.KeyboardModifier.AltModifier)):
            self._ai.request_now()
            event.accept()
            return

        # 自动配对：开括号 / 引号
        if plain_modifier and text in self._AUTO_PAIRS:
            if self._handle_open_pair(text):
                return

        # 自动跳过：右括号在已有右括号前则跳过（避免双写）
        if plain_modifier and text in (')', ']', '}'):
            if self._handle_skip_close(text):
                return

        # Backspace：删除紧邻的空配对（光标在 `(|)` 时一次删掉两边）
        if key == Qt.Key.Key_Backspace and plain_modifier and not (
            mods & Qt.KeyboardModifier.ShiftModifier
        ):
            if self._handle_pair_backspace():
                return

        # Shift+Tab 在 Qt 中通常表现为 Key_Backtab
        if key == Qt.Key.Key_Backtab or (
            key == Qt.Key.Key_Tab and mods & Qt.KeyboardModifier.ShiftModifier
        ):
            self._unindent_selection()
            return

        if key == Qt.Key.Key_Tab and not (
            mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
                    | Qt.KeyboardModifier.AltModifier)
        ):
            cursor = self.textCursor()
            # 有任何选区都按「整行缩进」处理，否则连按第二次会把整行替换成 4 个空格
            if cursor.hasSelection():
                self._indent_selection()
            else:
                cursor.insertText(self.INDENT_UNIT)
            return

        # Enter / Return：继承上一行缩进；Python 在 `:` 结尾时多加一级
        # 仅在无任何修饰键时介入；Shift+Enter 等保持原生行为
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier
                    | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier)
        ):
            self._insert_newline_with_indent()
            return

        super().keyPressEvent(event)

    def _insert_newline_with_indent(self):
        cursor = self.textCursor()
        # 当前块（行）从行首到光标位置的文本，用于决定缩进
        block = cursor.block()
        col = cursor.positionInBlock()
        line_text = block.text()
        # 当前行行首的连续空白
        leading_ws = ''
        for ch in line_text:
            if ch in (' ', '\t'):
                leading_ws += ch
            else:
                break

        extra_indent = ''
        # Python 风格：光标前去掉行内注释/空白，若以 `:` 结尾则多缩进一级
        # 只在 .py 文件启用，避免影响其它语言
        if self._is_python_file():
            before_cursor = line_text[:col]
            # 粗略剥离行尾注释（不处理 # 在字符串中的情形，足够日常使用）
            hash_idx = self._find_unquoted_hash(before_cursor)
            if hash_idx >= 0:
                before_cursor = before_cursor[:hash_idx]
            if before_cursor.rstrip().endswith(':'):
                extra_indent = self.INDENT_UNIT

        cursor.beginEditBlock()
        try:
            cursor.insertText('\n' + leading_ws + extra_indent)
        finally:
            cursor.endEditBlock()

    def _is_python_file(self) -> bool:
        # 通过祖先 FileEditorWidget 拿到当前文件路径
        parent = self.parent()
        while parent is not None:
            cur_file = getattr(parent, '_current_file', None)
            if cur_file:
                return cur_file.lower().endswith('.py')
            parent = parent.parent()
        return False

    @staticmethod
    def _find_unquoted_hash(text: str) -> int:
        """返回首个不在字符串中的 `#` 位置，找不到返回 -1。简化处理：不解析三引号。"""
        in_single = False
        in_double = False
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == '\\' and (in_single or in_double):
                i += 2
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == '#' and not in_single and not in_double:
                return i
            i += 1
        return -1

    # ---- 括号 / 引号自动配对 ----
    def _char_at(self, cursor, offset: int) -> str:
        block = cursor.block()
        col = cursor.positionInBlock() + offset
        text = block.text()
        if 0 <= col < len(text):
            return text[col]
        return ''

    def _handle_open_pair(self, ch: str) -> bool:
        pair = self._AUTO_PAIRS[ch]
        cursor = self.textCursor()

        if cursor.hasSelection():
            # 用一对包裹选区，保留选区便于继续编辑
            sel_start = cursor.selectionStart()
            sel_end = cursor.selectionEnd()
            sel_text = cursor.selectedText()
            cursor.beginEditBlock()
            try:
                cursor.insertText(ch + sel_text + pair)
            finally:
                cursor.endEditBlock()
            new_cursor = self.textCursor()
            new_cursor.setPosition(sel_start + 1)
            new_cursor.setPosition(sel_end + 1, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(new_cursor)
            return True

        nxt = self._char_at(cursor, 0)
        prv = self._char_at(cursor, -1)

        if ch in ('"', "'"):
            # 引号 1：已紧邻同款引号 → 跳过（避免双写）
            if nxt == ch:
                cursor.movePosition(QTextCursor.MoveOperation.Right)
                self.setTextCursor(cursor)
                return True
            # 引号 2：与标识符相邻则不自动配（如 don't、前缀 f"…"）
            ident = lambda c: c.isalnum() or c == '_'
            if ident(prv) or ident(nxt):
                return False

        cursor.insertText(ch + pair)
        cursor.movePosition(QTextCursor.MoveOperation.Left)
        self.setTextCursor(cursor)
        return True

    def _handle_skip_close(self, ch: str) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        if self._char_at(cursor, 0) != ch:
            return False
        cursor.movePosition(QTextCursor.MoveOperation.Right)
        self.setTextCursor(cursor)
        return True

    def _handle_pair_backspace(self) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        prv = self._char_at(cursor, -1)
        nxt = self._char_at(cursor, 0)
        if prv in self._AUTO_PAIRS and self._AUTO_PAIRS[prv] == nxt:
            cursor.beginEditBlock()
            try:
                cursor.deletePreviousChar()
                cursor.deleteChar()
            finally:
                cursor.endEditBlock()
            return True
        return False

    def _selection_spans_multiple_lines(self, cursor) -> bool:
        doc = self.document()
        start_block = doc.findBlock(cursor.selectionStart())
        end_block = doc.findBlock(cursor.selectionEnd())
        return start_block.blockNumber() != end_block.blockNumber()

    def _selection_block_range(self, cursor):
        """返回 (start_block, end_block, had_selection)，end 在行首时回退一行"""
        doc = self.document()
        sel_start = cursor.selectionStart()
        sel_end = cursor.selectionEnd()
        had_selection = sel_start != sel_end
        start_block = doc.findBlock(sel_start)
        end_block = doc.findBlock(sel_end)
        if had_selection and sel_end == end_block.position() and end_block != start_block:
            end_block = end_block.previous()
        return start_block, end_block, had_selection

    def _restore_block_selection(self, start_blk_num, end_blk_num):
        doc = self.document()
        new_start = doc.findBlockByNumber(start_blk_num)
        new_end = doc.findBlockByNumber(end_blk_num)
        if not new_start.isValid() or not new_end.isValid():
            return
        new_cursor = self.textCursor()
        new_cursor.setPosition(new_start.position())
        end_pos = new_end.position() + max(0, new_end.length() - 1)
        new_cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(new_cursor)

    def _indent_selection(self):
        cursor = self.textCursor()
        start_block, end_block, _ = self._selection_block_range(cursor)
        start_num = start_block.blockNumber()
        end_num = end_block.blockNumber()
        doc = self.document()

        edit_cursor = QTextCursor(doc)
        edit_cursor.beginEditBlock()
        try:
            for blk_num in range(end_num, start_num - 1, -1):
                blk = doc.findBlockByNumber(blk_num)
                if not blk.isValid():
                    continue
                edit_cursor.setPosition(blk.position())
                edit_cursor.insertText(self.INDENT_UNIT)
        finally:
            edit_cursor.endEditBlock()

        self._restore_block_selection(start_num, end_num)

    def _unindent_selection(self):
        cursor = self.textCursor()
        start_block, end_block, had_selection = self._selection_block_range(cursor)
        start_num = start_block.blockNumber()
        end_num = end_block.blockNumber()
        doc = self.document()

        edit_cursor = QTextCursor(doc)
        edit_cursor.beginEditBlock()
        try:
            for blk_num in range(end_num, start_num - 1, -1):
                blk = doc.findBlockByNumber(blk_num)
                if not blk.isValid():
                    continue
                text = blk.text()
                if not text:
                    continue
                # 优先吃掉一个 Tab；否则最多吃掉 4 个前导空格
                if text.startswith('\t'):
                    remove_count = 1
                else:
                    remove_count = 0
                    for i in range(min(len(self.INDENT_UNIT), len(text))):
                        if text[i] == ' ':
                            remove_count += 1
                        else:
                            break
                if remove_count <= 0:
                    continue
                edit_cursor.setPosition(blk.position())
                edit_cursor.setPosition(
                    blk.position() + remove_count,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                edit_cursor.removeSelectedText()
        finally:
            edit_cursor.endEditBlock()

        if had_selection:
            self._restore_block_selection(start_num, end_num)


class _SearchBar(QFrame):
    """文件编辑器顶部的查找/替换栏（Cmd+F 唤出查找，Cmd+H 唤出含替换）

    - 实时搜索：输入即匹配
    - 高亮所有命中，当前命中用更深色突出
    - Enter / Shift+Enter 跳转下一个 / 上一个
    - Esc 关闭并把焦点还给编辑器
    - 左侧的 ▸/▾ 按钮可展开/收起替换行
    """

    def __init__(self, editor: QPlainTextEdit, theme: dict, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.theme = theme or {}
        self._matches = []  # list[(start, end)]
        self._current = -1
        # _matches 对应的全文快照；替换时从这里取命中文本，
        # 避免每次替换再整份 toPlainText()
        self._matched_text = ""
        # 输入防抖：大文件下每敲一键就全文 finditer + 重建高亮太重。
        # 必须连绑定方法而非 lambda：lambda 不随接收者销毁自动断开，
        # 已排队的 timeout 会投递到删掉的对象上（崩溃）
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._on_debounce_timeout)

        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 8, 4)
        outer.setSpacing(4)

        # === 第一行：查找 ===
        find_row = QHBoxLayout()
        find_row.setSpacing(6)

        # 折叠/展开替换行的小箭头
        self.toggle_replace_btn = QPushButton("▸")
        self.toggle_replace_btn.setFixedSize(18, 22)
        self.toggle_replace_btn.setToolTip(t("editor.search_toggle_replace_tooltip"))
        self.toggle_replace_btn.clicked.connect(self._toggle_replace_row)
        find_row.addWidget(self.toggle_replace_btn)

        self.input = QLineEdit()
        self.input.setPlaceholderText(t("editor.search_placeholder"))
        self.input.textChanged.connect(self._on_text_changed)
        self.input.returnPressed.connect(self._goto_next)
        self.input.installEventFilter(self)
        find_row.addWidget(self.input, 1)

        self.count_label = QLabel("")
        self.count_label.setMinimumWidth(60)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        find_row.addWidget(self.count_label)

        self.case_btn = QPushButton("Aa")
        self.case_btn.setCheckable(True)
        self.case_btn.setFixedSize(28, 22)
        self.case_btn.setToolTip(t("editor.search_case_tooltip"))
        self.case_btn.toggled.connect(lambda _: self._update_matches(reset_current=False))
        find_row.addWidget(self.case_btn)

        self.word_btn = QPushButton("│ab│")
        self.word_btn.setCheckable(True)
        self.word_btn.setFixedSize(36, 22)
        self.word_btn.setToolTip(t("editor.search_word_tooltip"))
        self.word_btn.toggled.connect(lambda _: self._update_matches(reset_current=False))
        find_row.addWidget(self.word_btn)

        self.regex_btn = QPushButton(".*")
        self.regex_btn.setCheckable(True)
        self.regex_btn.setFixedSize(28, 22)
        self.regex_btn.setToolTip(t("editor.search_regex_tooltip"))
        self.regex_btn.toggled.connect(lambda _: self._update_matches(reset_current=False))
        find_row.addWidget(self.regex_btn)

        self.prev_btn = QPushButton("↑")
        self.prev_btn.setFixedSize(28, 22)
        self.prev_btn.setToolTip(t("editor.search_prev_tooltip"))
        self.prev_btn.clicked.connect(self._goto_prev)
        find_row.addWidget(self.prev_btn)

        self.next_btn = QPushButton("↓")
        self.next_btn.setFixedSize(28, 22)
        self.next_btn.setToolTip(t("editor.search_next_tooltip"))
        self.next_btn.clicked.connect(self._goto_next)
        find_row.addWidget(self.next_btn)

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setToolTip(t("editor.search_close_tooltip"))
        self.close_btn.clicked.connect(self.close_search)
        find_row.addWidget(self.close_btn)

        outer.addLayout(find_row)

        # === 第二行：替换（默认隐藏）===
        self.replace_row_widget = QWidget()
        replace_row = QHBoxLayout(self.replace_row_widget)
        replace_row.setContentsMargins(0, 0, 0, 0)
        replace_row.setSpacing(6)

        # 占位，让替换输入框与上方查找输入框左缘对齐（对齐 toggle 按钮宽度）
        spacer = QWidget()
        spacer.setFixedWidth(18)
        replace_row.addWidget(spacer)

        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText(t("editor.replace_placeholder"))
        self.replace_input.returnPressed.connect(self._replace_one)
        self.replace_input.installEventFilter(self)
        replace_row.addWidget(self.replace_input, 1)

        # 留与上行 count_label 同宽的占位，保持按钮列对齐
        replace_spacer = QWidget()
        replace_spacer.setMinimumWidth(60)
        replace_row.addWidget(replace_spacer)

        self.replace_one_btn = QPushButton(t("editor.replace_one_label"))
        self.replace_one_btn.setFixedHeight(22)
        self.replace_one_btn.setToolTip(t("editor.replace_one_tooltip"))
        self.replace_one_btn.clicked.connect(self._replace_one)
        replace_row.addWidget(self.replace_one_btn)

        self.replace_all_btn = QPushButton(t("editor.replace_all_label"))
        self.replace_all_btn.setFixedHeight(22)
        self.replace_all_btn.setToolTip(t("editor.replace_all_tooltip"))
        self.replace_all_btn.clicked.connect(self._replace_all)
        replace_row.addWidget(self.replace_all_btn)

        outer.addWidget(self.replace_row_widget)
        self.replace_row_widget.hide()

        self.apply_theme(self.theme)
        self.hide()

    def apply_theme(self, theme: dict):
        self.theme = theme or {}
        bg_medium = self.theme.get('bg_medium', '#16213e')
        bg_dark = self.theme.get('bg_dark', '#1a1a2e')
        text = self.theme.get('text', '#eaeaea')
        text_dim = self.theme.get('text_dim', '#888888')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')
        accent_hover = self.theme.get('accent_hover', '#7a8efa')

        self.setStyleSheet(f"""
            _SearchBar, QFrame#_SearchBar {{
                background-color: {bg_medium};
                border-bottom: 1px solid {border};
            }}
            QLineEdit {{
                background-color: {bg_dark};
                color: {text};
                border: 1px solid {border};
                border-radius: 3px;
                padding: 2px 6px;
                selection-background-color: {accent};
                selection-color: white;
            }}
            QLineEdit:focus {{
                border: 1px solid {accent};
            }}
            QLabel {{
                color: {text_dim};
            }}
            QPushButton {{
                background-color: transparent;
                color: {text_dim};
                border: 1px solid transparent;
                border-radius: 3px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {bg_dark};
                color: {text};
                border: 1px solid {border};
            }}
            QPushButton:checked {{
                background-color: {accent};
                color: white;
                border: 1px solid {accent_hover};
            }}
        """)

    def apply_language(self):
        self.input.setPlaceholderText(t("editor.search_placeholder"))
        self.case_btn.setToolTip(t("editor.search_case_tooltip"))
        self.word_btn.setToolTip(t("editor.search_word_tooltip"))
        self.regex_btn.setToolTip(t("editor.search_regex_tooltip"))
        self.prev_btn.setToolTip(t("editor.search_prev_tooltip"))
        self.next_btn.setToolTip(t("editor.search_next_tooltip"))
        self.close_btn.setToolTip(t("editor.search_close_tooltip"))
        self.toggle_replace_btn.setToolTip(t("editor.search_toggle_replace_tooltip"))
        self.replace_input.setPlaceholderText(t("editor.replace_placeholder"))
        self.replace_one_btn.setText(t("editor.replace_one_label"))
        self.replace_one_btn.setToolTip(t("editor.replace_one_tooltip"))
        self.replace_all_btn.setText(t("editor.replace_all_label"))
        self.replace_all_btn.setToolTip(t("editor.replace_all_tooltip"))

    def eventFilter(self, obj, event):
        # 在 __init__ 期间 replace_input 还未创建就可能收到事件；用 getattr 保护
        replace_input = getattr(self, 'replace_input', None)
        if obj is not self.input and (replace_input is None or obj is not replace_input):
            return super().eventFilter(obj, event)
        if event.type() == event.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key == Qt.Key.Key_Escape:
                self.close_search()
                return True
            if obj is self.input and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if mods & Qt.KeyboardModifier.ShiftModifier:
                    self._goto_prev()
                else:
                    self._goto_next()
                return True
            if obj is self.replace_input and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
                    self._replace_all()
                else:
                    self._replace_one()
                return True
            if obj is self.replace_input and key == Qt.Key.Key_Tab and (
                mods & Qt.KeyboardModifier.ShiftModifier
            ):
                self.input.setFocus()
                self.input.selectAll()
                return True
            if obj is self.input and key == Qt.Key.Key_Tab and not (
                mods & Qt.KeyboardModifier.ShiftModifier
            ) and not self.replace_row_widget.isHidden():
                self.replace_input.setFocus()
                self.replace_input.selectAll()
                return True
            if key == Qt.Key.Key_F3:
                if mods & Qt.KeyboardModifier.ShiftModifier:
                    self._goto_prev()
                else:
                    self._goto_next()
                return True
        return super().eventFilter(obj, event)

    def _toggle_replace_row(self):
        self._set_replace_visible(self.replace_row_widget.isHidden())

    def _set_replace_visible(self, visible: bool):
        self.replace_row_widget.setVisible(visible)
        self.toggle_replace_btn.setText("▾" if visible else "▸")

    def open_search(self, with_replace: bool = False):
        # 用编辑器当前选中文本作为初始查找内容（若存在且单行）
        cursor = self.editor.textCursor()
        seed = ""
        if cursor.hasSelection():
            sel = cursor.selectedText()
            # Qt 用 U+2029 段落分隔符表示换行，过滤多行选中
            if ' ' not in sel and 0 < len(sel) <= 200:
                seed = sel
        self.show()
        if with_replace:
            self._set_replace_visible(True)
        if seed:
            self.input.setText(seed)
            self.input.selectAll()
        self.input.setFocus()
        self.input.selectAll()
        self._update_matches(reset_current=False)

    def close_search(self):
        self._debounce.stop()
        self.hide()
        self.editor.set_search_selections([])
        self.editor.setFocus()

    def _on_text_changed(self, _text):
        self._debounce.start()

    def _on_debounce_timeout(self):
        self._update_matches(reset_current=True)

    def _flush_pending(self):
        """有未生效的防抖更新时立即执行（Enter 跳转/替换前调用，保证结果不滞后）"""
        if self._debounce.isActive():
            self._debounce.stop()
            self._update_matches(reset_current=True)

    def _build_pattern(self, query):
        if not query:
            return None
        flags = 0 if self.case_btn.isChecked() else re.IGNORECASE
        if self.regex_btn.isChecked():
            try:
                return re.compile(query, flags)
            except re.error:
                return None
        pattern = re.escape(query)
        if self.word_btn.isChecked():
            pattern = r'\b' + pattern + r'\b'
        try:
            return re.compile(pattern, flags)
        except re.error:
            return None

    def _update_matches(self, reset_current: bool):
        self._debounce.stop()  # 显式更新后取消排队中的防抖触发
        query = self.input.text()
        text = self.editor.toPlainText()
        self._matched_text = text
        pattern = self._build_pattern(query)

        self._matches = []
        if pattern and text:
            for m in pattern.finditer(text):
                start, end = m.start(), m.end()
                if end > start:
                    self._matches.append((start, end))
                else:
                    # 防止零宽匹配死循环
                    continue

        if not self._matches:
            self._current = -1
        elif reset_current:
            cursor_pos = self.editor.textCursor().selectionStart()
            self._current = 0
            for i, (s, _e) in enumerate(self._matches):
                if s >= cursor_pos:
                    self._current = i
                    break
        else:
            if self._current < 0 or self._current >= len(self._matches):
                self._current = 0

        self._refresh_highlights()
        if self._current >= 0:
            self._scroll_to_current()
        self._update_count()
        self._update_input_validity(query, pattern)

    # 高亮条数上限：每个高亮是一个 QTextCursor + ExtraSelection，
    # 大文件搜 "e" 能命中上万，全建出来 GUI 直接冻住。超限时只高亮
    # 当前命中前后一个窗口；计数标签仍显示全部命中数。
    _MAX_HIGHLIGHTS = 500

    def _refresh_highlights(self):
        selections = []
        all_match_color = QColor(self.theme.get('search_match_bg', '#5a4a1a'))
        current_match_color = QColor(self.theme.get('search_current_bg', '#c89020'))
        text_color = QColor(self.theme.get('text', '#eaeaea'))

        lo, hi = 0, len(self._matches)
        if hi > self._MAX_HIGHLIGHTS:
            center = self._current if self._current >= 0 else 0
            lo = max(0, min(center - self._MAX_HIGHLIGHTS // 2,
                            hi - self._MAX_HIGHLIGHTS))
            hi = lo + self._MAX_HIGHLIGHTS

        doc = self.editor.document()
        for i in range(lo, hi):
            start, end = self._matches[i]
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(current_match_color if i == self._current else all_match_color)
            fmt.setForeground(text_color)
            sel.format = fmt
            cur = QTextCursor(doc)
            cur.setPosition(start)
            cur.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = cur
            selections.append(sel)
        self.editor.set_search_selections(selections)

    def _scroll_to_current(self):
        if self._current < 0 or not self._matches:
            return
        start, end = self._matches[self._current]
        # 用一个临时 cursor 让编辑器滚动到位，但不修改实际选择/焦点
        cur = QTextCursor(self.editor.document())
        cur.setPosition(start)
        cur.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cur)
        self.editor.ensureCursorVisible()
        # 把焦点还给输入框，让用户继续输入
        self.input.setFocus()

    def _goto_next(self):
        self._flush_pending()
        if not self._matches:
            return
        self._current = (self._current + 1) % len(self._matches)
        self._refresh_highlights()
        self._scroll_to_current()
        self._update_count()

    def _goto_prev(self):
        self._flush_pending()
        if not self._matches:
            return
        self._current = (self._current - 1) % len(self._matches)
        self._refresh_highlights()
        self._scroll_to_current()
        self._update_count()

    def _expand_replacement(self, repl: str, match_text: str) -> str:
        """非正则模式：原样替换；正则模式：按 re 语义展开（支持 \\1 反向引用）"""
        if not self.regex_btn.isChecked():
            return repl
        pattern = self._build_pattern(self.input.text())
        if pattern is None:
            return repl
        try:
            # 用 re.sub 在仅有一段匹配文本上处理反向引用，count=1 避免越界
            return pattern.sub(repl, match_text, count=1)
        except re.error:
            return repl

    def _replace_one(self):
        """替换当前命中，再跳到下一个；无命中时仅触发一次匹配"""
        self._flush_pending()
        if not self._matches or self._current < 0:
            self._update_matches(reset_current=True)
            return
        start, end = self._matches[self._current]
        doc = self.editor.document()
        match_text = self._matched_text[start:end]
        new_text = self._expand_replacement(self.replace_input.text(), match_text)

        edit_cursor = QTextCursor(doc)
        edit_cursor.beginEditBlock()
        try:
            edit_cursor.setPosition(start)
            edit_cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            edit_cursor.insertText(new_text)
        finally:
            edit_cursor.endEditBlock()

        # 文本变了，重算匹配；下一次 _update_matches 会按光标位置定位到下一个
        # 把光标挪到替换文本末尾，确保 reset_current=True 时选到后续匹配
        cur = self.editor.textCursor()
        cur.setPosition(start + len(new_text))
        self.editor.setTextCursor(cur)
        self._update_matches(reset_current=True)
        # 焦点回到替换输入框，方便连按 Enter
        self.replace_input.setFocus()

    def _replace_all(self):
        self._flush_pending()
        if not self._matches:
            return
        doc = self.editor.document()
        full_text = self._matched_text
        replacement = self.replace_input.text()

        edit_cursor = QTextCursor(doc)
        edit_cursor.beginEditBlock()
        try:
            # 反向遍历，避免位置偏移
            for start, end in reversed(self._matches):
                match_text = full_text[start:end]
                new_text = self._expand_replacement(replacement, match_text)
                edit_cursor.setPosition(start)
                edit_cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                edit_cursor.insertText(new_text)
        finally:
            edit_cursor.endEditBlock()

        self._update_matches(reset_current=True)
        self.replace_input.setFocus()

    def _update_count(self):
        if not self.input.text():
            self.count_label.setText("")
        elif not self._matches:
            self.count_label.setText(t("editor.search_no_results"))
        else:
            self.count_label.setText(f"{self._current + 1} / {len(self._matches)}")

    def _update_input_validity(self, query, pattern):
        # 正则模式下编译失败给出红色边框提示
        if query and self.regex_btn.isChecked() and pattern is None:
            invalid_color = self.theme.get('error', '#ef4444')
            border = self.theme.get('border', '#3d3d5c')
            self.input.setStyleSheet(
                f"QLineEdit {{ border: 1px solid {invalid_color}; }}"
                f"QLineEdit:focus {{ border: 1px solid {invalid_color}; }}"
            )
        else:
            self.input.setStyleSheet("")


class FileEditorWidget(QWidget):
    """文件编辑器组件"""

    # 信号
    file_saved = pyqtSignal(str)  # 文件保存信号
    editor_closed = pyqtSignal()  # 编辑器关闭信号
    split_h_requested = pyqtSignal()  # 请求左右分屏（并排）
    split_v_requested = pyqtSignal()  # 请求上下分屏
    move_left_requested = pyqtSignal()  # 与左侧窗格交换位置
    move_up_requested = pyqtSignal()  # 与上方窗格交换位置
    pane_focused = pyqtSignal()  # 本窗格获得焦点 / 被点击
    ai_completion_toggled = pyqtSignal(bool)  # 用户在标题栏切换 AI 补全开关

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._current_file = None
        self._original_content = ""  # 保存原始内容用于比较
        self._highlighter = None
        self._in_image_mode = False  # 当前显示的是否是图片预览
        self._image_pixmap: QPixmap | None = None  # 原始未缩放的 QPixmap
        # Markdown 预览：只读渲染页（源文本 → 预览单向转换，绝不写回缓冲区/文件）
        self._md_preview_supported = False  # 当前文件是否为 Markdown
        self._html_preview_supported = False  # 当前文件是否为 HTML（预览走系统浏览器）
        self._in_md_preview = False         # 当前是否显示渲染预览页
        self._md_img_cache = {}             # 预览图片解码/缩放缓存 {(path, mtime, w): QImage}
        # 多窗格分屏：是否高亮当前活动窗格（仅在 >1 窗格时由 EditorArea 打开）
        self._is_active = False
        self._show_active_indicator = False
        # 编辑器字号（pt）。与终端联动，由 EditorArea.set_editor_font_size 统一驱动。
        # 注意：必须写进编辑器自己的 QSS（见 _apply_theme），否则会被主窗口 GUI 字号的
        # app 级 `QWidget { font-size }` 样式表盖过——QSS 字号优先级高于 setFont()。
        self._editor_point_size = 12

        # --- 外部文件变更检测 ---
        self._file_watcher = QFileSystemWatcher(self)
        self._file_watcher.fileChanged.connect(self._on_watched_file_changed)
        self._known_mtime: float | None = None
        # 自身 save 后短暂屏蔽 watcher 触发，避免「自己写自己的文件」也弹"重载"
        # 最近一次保存写下的字节 sha1：watcher 据此识别"这是我自己的保存"，
        # 取代旧的时间窗屏蔽（旧法会漏掉保存后短时间内真实的外部改动）。
        self._last_saved_hash: str | None = None
        # 用于去抖：rename 类保存会先收到 deleted 再 created
        self._external_change_timer: QTimer | None = None

        # 打开时检测到的文件编码（utf-8 / utf-8-sig / gb18030 / latin-1），
        # 保存时按它写回；新建（另存为）文件默认 utf-8
        self._file_encoding = "utf-8"

        # --- 自动保存（崩溃恢复）---
        # 周期检查未保存修改并写入 ~/.smart_terminal_autosave/；
        # open_file 时启动、关闭/切图片时停止，QTimer 以 self 为父随窗格销毁
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(_AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave_tick)

        # markdown 预览视口 resize 防抖：停止变宽 200ms 后重渲染重算大图缩放
        self._md_refit_timer = QTimer(self)
        self._md_refit_timer.setSingleShot(True)
        self._md_refit_timer.setInterval(200)
        self._md_refit_timer.timeout.connect(self._md_refit_tick)
        self._md_last_render_width = -1  # 上次预览渲染时的视口宽（重渲染判据）
        # 上次写入备份的内容指纹：内容没变就不重复写盘
        self._last_autosave_hash: str | None = None

        self._setup_ui()
        self._setup_shortcuts()
        self._apply_theme()

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        self.header = QFrame()
        self.header.setFixedHeight(36)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        header_layout.setSpacing(8)

        # 文件名标签
        self.file_label = QLabel(t("editor.no_file"))
        self.file_label.setStyleSheet("font-weight: bold;")
        # 远程文件会把 "· [remote] host:/很长的/路径" 追加到这里。若让标签按文字
        # 首选宽度参与布局，长标题会把整个编辑窗格 / 外层 splitter 撑宽、挤压终端，
        # 表现为「打开某些文件后窗口宽度变了」。Ignored 水平策略 = 布局给多少用多少、
        # 不因文字变长而要求更宽；超出部分裁掉（文件名在最前，不影响辨认）。
        self.file_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        self.file_label.setMinimumWidth(0)
        header_layout.addWidget(self.file_label, 1)

        # 修改状态指示
        self.modified_label = QLabel("")
        self.modified_label.setStyleSheet("color: #f59e0b;")
        header_layout.addWidget(self.modified_label)

        # 不再 addStretch：file_label 已用 stretch=1 占满中间空间，按钮自然靠右。

        # Markdown 预览开关（仅 .md 文件显示）：勾选 = 渲染预览，取消 = 源码编辑
        self.md_btn = QPushButton("◎")
        self.md_btn.setFixedSize(26, 26)
        self.md_btn.setCheckable(True)
        self.md_btn.setToolTip(t("editor.md_preview_tooltip"))
        self.md_btn.clicked.connect(self._on_preview_btn_clicked)
        self.md_btn.setVisible(False)
        header_layout.addWidget(self.md_btn)

        # AI 行内补全开关（灰字建议，Tab 接受 / Esc 取消）
        # 用文字字形 ✦ 而非 emoji：emoji 是彩色位图，不随 QSS 配色，和相邻按钮不协调
        self.ai_btn = QPushButton("✦")
        self.ai_btn.setFixedSize(26, 26)
        self.ai_btn.setCheckable(True)
        self.ai_btn.setToolTip(t("editor.ai_toggle_tooltip"))
        self.ai_btn.clicked.connect(
            lambda checked: self.ai_completion_toggled.emit(bool(checked))
        )
        header_layout.addWidget(self.ai_btn)

        # 左右分屏按钮（并排查看不同文件）
        self.split_h_btn = QPushButton("◫")
        self.split_h_btn.setFixedSize(26, 26)
        self.split_h_btn.setToolTip(t("editor.split_h_tooltip"))
        self.split_h_btn.clicked.connect(self.split_h_requested.emit)
        header_layout.addWidget(self.split_h_btn)

        # 上下分屏按钮
        self.split_v_btn = QPushButton("⊟")
        self.split_v_btn.setFixedSize(26, 26)
        self.split_v_btn.setToolTip(t("editor.split_v_tooltip"))
        self.split_v_btn.clicked.connect(self.split_v_requested.emit)
        header_layout.addWidget(self.split_v_btn)

        # 关闭按钮
        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(26, 26)
        self.close_btn.setToolTip(t("editor.close_tooltip"))
        self.close_btn.clicked.connect(self._close_editor)
        header_layout.addWidget(self.close_btn)

        layout.addWidget(self.header)

        # 编辑器
        self.editor = CodeEditor()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.textChanged.connect(self._on_text_changed)
        # 点击 / 聚焦编辑器 → 通知 EditorArea 把本窗格设为活动窗格
        self.editor.installEventFilter(self)
        # 右键菜单 / 快捷键的分屏请求 → 转发为本窗格的分屏信号
        self.editor.split_h_requested.connect(self.split_h_requested.emit)
        self.editor.split_v_requested.connect(self.split_v_requested.emit)
        # 关闭当前分屏 → 走与关闭按钮相同的路径（含未保存提示）
        self.editor.close_split_requested.connect(self._close_editor)
        # 移动分屏 → 转发为本窗格的移动信号
        self.editor.move_left_requested.connect(self.move_left_requested.emit)
        self.editor.move_up_requested.connect(self.move_up_requested.emit)

        # 设置等宽字体；默认字号与终端一致（12pt），保证两者字号联动。
        # Menlo 仅 macOS 存在，用 setFamilies 提供各平台首选避免 Windows 字体回退
        font = QFont()
        font.setFamilies(["Menlo", "Consolas", "Cascadia Mono",
                          "DejaVu Sans Mono", "Courier New"])
        font.setPointSize(12)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.editor.setFont(font)

        # 设置 Tab 宽度为 4 个空格
        font_metrics = self.editor.fontMetrics()
        self.editor.setTabStopDistance(4 * font_metrics.horizontalAdvance(' '))

        # 查找栏（默认隐藏，Cmd+F 唤出）
        self.search_bar = _SearchBar(self.editor, self.theme, self)
        layout.addWidget(self.search_bar)

        # 用 QStackedWidget 在 "文本编辑器" 和 "图片预览" 之间切换
        self._stack = QStackedWidget()
        self._stack.addWidget(self.editor)  # index 0

        # 图片预览页：QScrollArea 包一个 QLabel，按视口大小等比缩放，不放大原图
        self._image_scroll = QScrollArea()
        self._image_scroll.setWidgetResizable(True)
        self._image_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        self._image_label.setText("")
        # 图片预览右键菜单：复制到剪贴板 / 另存为
        self._image_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._image_label.customContextMenuRequested.connect(self._show_image_context_menu)
        self._image_scroll.setWidget(self._image_label)
        # 点击图片预览区也把本窗格设为活动窗格
        self._image_scroll.viewport().installEventFilter(self)
        # ScrollArea 的空白处也允许右键弹同样的菜单
        self._image_scroll.viewport().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self._image_scroll.viewport().customContextMenuRequested.connect(
            self._show_image_context_menu
        )
        self._stack.addWidget(self._image_scroll)  # index 1

        # Markdown 渲染预览页：只读 QTextBrowser + Qt 原生 setMarkdown（GitHub 方言）。
        # 安全性：预览有自己独立的 QTextDocument，与编辑器缓冲区完全隔离，
        # 只做「源文本 → 预览」单向渲染，保存路径仍只序列化编辑器缓冲区原文，
        # 因此预览不可能改动文件内容。无 HTML 引擎、无 JS、不发网络请求。
        self._md_browser = QTextBrowser()
        self._md_browser.setFrameShape(QFrame.Shape.NoFrame)
        # 不让浏览器自己跳转（防止预览页被导航到别的文档）；
        # 链接点击统一走 _on_md_link_clicked，只放行 http/https 到系统浏览器
        self._md_browser.setOpenLinks(False)
        self._md_browser.setOpenExternalLinks(False)
        self._md_browser.anchorClicked.connect(self._on_md_link_clicked)
        # 点击预览区也把本窗格设为活动窗格
        self._md_browser.viewport().installEventFilter(self)
        # 预览页右键菜单：Qt 默认只有复制/全选，这里在其后追加与源码页
        # 一致的分屏/移动等窗格级动作（否则预览状态下无法从右键菜单分屏）
        self._md_browser.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._md_browser.customContextMenuRequested.connect(self._show_md_context_menu)
        # 代码块语法高亮：常驻挂在预览文档上，随每次 setMarkdown 自动重跑
        self._md_code_highlighter = _MdCodeHighlighter(self._md_browser.document())
        self._stack.addWidget(self._md_browser)  # index 2

        layout.addWidget(self._stack)

    def _setup_shortcuts(self):
        """设置快捷键"""
        # Cmd+S / Ctrl+S 保存
        # 必须限定为 WidgetWithChildrenShortcut：否则多窗格分屏时每个窗格都注册一个
        # 窗口级 Save 快捷键，Qt 判为「歧义」(activatedAmbiguously) 而不触发 save，
        # 表现为 Cmd+S 在分屏后失效。限定到本窗格焦点即只存当前编辑的窗格。
        save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        save_shortcut.activated.connect(self.save_file)

        # Cmd+/ / Ctrl+/ 切换注释：不能用 QShortcut——主窗口注册了同键位的全局
        # 「快捷键速查表」QAction，两者同时匹配时 Qt 判为歧义快捷键，双方都不触发
        # （表现为 .env 等文件里 Cmd+/ 无反应）。改在 eventFilter 的
        # ShortcutOverride 阶段声明由编辑器自行处理（见 eventFilter）。

        # Cmd+\ 左右分屏；Cmd+Shift+\ 上下分屏（与 VS Code 一致，作用于本窗格）
        split_h_sc = QShortcut(QKeySequence("Ctrl+\\"), self)
        split_h_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        split_h_sc.activated.connect(self.split_h_requested.emit)

        split_v_sc = QShortcut(QKeySequence("Ctrl+Shift+\\"), self)
        split_v_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        split_v_sc.activated.connect(self.split_v_requested.emit)

        # Cmd+Shift+M 切换 Markdown 源码/预览（VS Code 的 Cmd+Shift+V
        # 在本应用已被「上下分屏」占用，取 M=Markdown）
        md_toggle_sc = QShortcut(QKeySequence("Ctrl+Shift+M"), self)
        md_toggle_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        md_toggle_sc.activated.connect(self._toggle_md_preview)

        # Cmd+F / Ctrl+F 唤出查找栏
        find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        find_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_shortcut.activated.connect(self._open_search)

        # Cmd+H / Ctrl+H 唤出含替换的查找栏（与 VSCode / Sublime 一致）
        replace_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        replace_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        replace_shortcut.activated.connect(self._open_search_with_replace)

        # Cmd+G / Ctrl+G 跳到下一个匹配；Shift+Cmd+G 跳到上一个
        find_next_shortcut = QShortcut(QKeySequence.StandardKey.FindNext, self)
        find_next_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_next_shortcut.activated.connect(self._find_next_or_open)

        find_prev_shortcut = QShortcut(QKeySequence.StandardKey.FindPrevious, self)
        find_prev_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_prev_shortcut.activated.connect(self._find_prev_or_open)

        # Esc 在编辑器有焦点时关闭查找栏（如果可见）
        esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.editor)
        esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc_shortcut.activated.connect(self._close_search_if_open)

        # 缩放快捷键已移至主窗口全局处理（避免 ambiguous shortcut 冲突）

    def _open_search(self):
        if hasattr(self, 'search_bar'):
            # 查找作用于源码编辑器，预览模式下先切回源码
            if self._in_md_preview:
                self._set_md_preview(False)
            self.search_bar.open_search()

    def _open_search_with_replace(self):
        if hasattr(self, 'search_bar'):
            if self._in_md_preview:
                self._set_md_preview(False)
            self.search_bar.open_search(with_replace=True)

    def _find_next_or_open(self):
        if hasattr(self, 'search_bar'):
            if self._in_md_preview:
                self._set_md_preview(False)
            if self.search_bar.isHidden() or not self.search_bar.input.text():
                self.search_bar.open_search()
            else:
                self.search_bar._goto_next()

    def _find_prev_or_open(self):
        if hasattr(self, 'search_bar'):
            if self._in_md_preview:
                self._set_md_preview(False)
            if self.search_bar.isHidden() or not self.search_bar.input.text():
                self.search_bar.open_search()
            else:
                self.search_bar._goto_prev()

    def _close_search_if_open(self):
        if hasattr(self, 'search_bar') and not self.search_bar.isHidden():
            self.search_bar.close_search()

    def _get_line_comment(self):
        """根据当前文件类型返回行注释前缀，无法识别时返回 None"""
        if not self._current_file:
            return None
        ext = Path(self._current_file).suffix.lower()
        name = Path(self._current_file).name.lower()

        # 点文件（无扩展名，suffix 为空）需按文件名匹配
        # 例如 .env / .env.local / .gitignore / .dockerignore
        if name == '.env' or name.startswith('.env.'):
            return '#'
        if name in ('.gitignore', '.dockerignore', '.gitattributes',
                    '.npmignore', '.editorconfig'):
            return '#'

        if ext == '.py':
            return '#'
        if ext in ('.sh', '.bash', '.zsh', '.ksh', '.fish') or \
                name in ('.bashrc', '.bash_profile', '.zshrc', '.profile', '.zprofile'):
            return '#'
        if ext in ('.yml', '.yaml'):
            return '#'
        if ext in ('.toml', '.ini', '.conf', '.cfg', '.properties'):
            return '#'
        if ext == '.dockerfile' or name == 'dockerfile' or name.startswith('dockerfile.'):
            return '#'
        if name in ('makefile', 'gnumakefile', 'bsdmakefile') or \
                name.startswith('makefile.') or ext in ('.mk', '.make'):
            return '#'
        if ext in ('.rb', '.r', '.pl', '.pm', '.tcl', '.awk'):
            return '#'
        if ext in ('.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx',
                   '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh',
                   '.java', '.cs', '.go', '.rs', '.swift', '.kt', '.kts',
                   '.scala', '.dart', '.php', '.m', '.mm'):
            return '//'
        if ext in ('.lua', '.sql', '.hs', '.elm', '.ada'):
            return '--'
        return None

    def _toggle_comment(self):
        """切换选中行的注释（VS Code 风格）

        - 无选区时作用于光标所在行
        - 选区跨多行时统一处理
        - 全部已注释 → 取消注释；否则 → 全部注释（注释插入在最小缩进位置）
        """
        prefix = self._get_line_comment()
        if not prefix:
            return

        editor = self.editor
        cursor = editor.textCursor()
        doc = editor.document()

        sel_start = cursor.selectionStart()
        sel_end = cursor.selectionEnd()
        had_selection = sel_start != sel_end

        start_block = doc.findBlock(sel_start)
        end_block = doc.findBlock(sel_end)
        # 若选区结束于行首，则不包含该行（与编辑器常见行为一致）
        if had_selection and sel_end == end_block.position() and end_block != start_block:
            end_block = end_block.previous()

        start_blk_num = start_block.blockNumber()
        end_blk_num = end_block.blockNumber()

        # 收集块内容
        block_infos = []
        b = start_block
        while b.isValid():
            block_infos.append((b.blockNumber(), b.text()))
            if b.blockNumber() == end_blk_num:
                break
            b = b.next()

        line_re = re.compile(r'^(\s*)' + re.escape(prefix) + r'(\s?)')

        all_commented = True
        has_non_empty = False
        min_indent = None

        for _, text in block_infos:
            if not text.strip():
                continue
            has_non_empty = True
            indent_len = len(text) - len(text.lstrip())
            if min_indent is None or indent_len < min_indent:
                min_indent = indent_len
            if not line_re.match(text):
                all_commented = False

        if not has_non_empty:
            all_commented = False
            min_indent = 0

        insert_text = prefix + ' '

        edit_cursor = QTextCursor(doc)
        edit_cursor.beginEditBlock()
        try:
            if all_commented:
                # 反向遍历，避免位置失效
                for blk_num, text in reversed(block_infos):
                    blk = doc.findBlockByNumber(blk_num)
                    if not blk.isValid():
                        continue
                    m = line_re.match(blk.text())
                    if not m:
                        continue
                    remove_start = blk.position() + len(m.group(1))
                    remove_len = len(prefix) + len(m.group(2))
                    edit_cursor.setPosition(remove_start)
                    edit_cursor.setPosition(remove_start + remove_len,
                                             QTextCursor.MoveMode.KeepAnchor)
                    edit_cursor.removeSelectedText()
            else:
                for blk_num, text in reversed(block_infos):
                    if not text.strip() and has_non_empty:
                        # 混合内容时跳过空行
                        continue
                    blk = doc.findBlockByNumber(blk_num)
                    if not blk.isValid():
                        continue
                    insert_pos = blk.position() + min(min_indent, len(blk.text()))
                    edit_cursor.setPosition(insert_pos)
                    edit_cursor.insertText(insert_text)
        finally:
            edit_cursor.endEditBlock()

        # 恢复选区/光标
        new_start_block = doc.findBlockByNumber(start_blk_num)
        new_end_block = doc.findBlockByNumber(end_blk_num)
        new_cursor = editor.textCursor()
        if had_selection and new_start_block.isValid() and new_end_block.isValid():
            new_cursor.setPosition(new_start_block.position())
            end_pos = new_end_block.position() + max(0, new_end_block.length() - 1)
            new_cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
        elif new_start_block.isValid():
            col = sel_start - start_block.position()
            if all_commented:
                col = max(0, col - (len(prefix) + 1))
            else:
                if col >= (min_indent or 0):
                    col += len(insert_text)
            max_col = max(0, new_start_block.length() - 1)
            col = min(col, max_col)
            new_cursor.setPosition(new_start_block.position() + col)
        editor.setTextCursor(new_cursor)

    def set_editor_point_size(self, point_size: int):
        """设置编辑器字号（pt）并落到 QSS，保证不被 GUI 字号的 app 级样式表盖过。

        同时更新 QFont（让 fontMetrics / tab 宽度等正确）与编辑器 QSS 的 font-size。
        由 EditorArea 统一调用，使所有窗格字号与终端联动。
        """
        point_size = max(6, min(48, int(point_size)))
        self._editor_point_size = point_size
        font = self.editor.font()
        if font.pointSize() != point_size:
            font.setPointSize(point_size)
            self.editor.setFont(font)
        # 重新套用编辑器 QSS（含 font-size），并按新字号重算 tab 宽度
        self._apply_theme()
        fm = QFontMetrics(font)
        self.editor.setTabStopDistance(4 * fm.horizontalAdvance(' '))
        # 预览页字号随缩放变化后重渲染，确保标题等相对字号按新基准换算
        if self._in_md_preview:
            self._render_md_preview()

    def _zoom_in(self):
        """放大字体"""
        self.set_editor_point_size(self._editor_point_size + 1)

    def _zoom_out(self):
        """缩小字体"""
        self.set_editor_point_size(self._editor_point_size - 1)

    def _apply_theme(self):
        """应用主题"""
        bg_dark = self.theme.get('bg_dark', '#1a1a2e')
        bg_medium = self.theme.get('bg_medium', '#16213e')
        text = self.theme.get('text', '#eaeaea')
        text_dim = self.theme.get('text_dim', '#888888')
        border = self.theme.get('border', '#3d3d5c')
        accent = self.theme.get('accent', '#667eea')

        self.setStyleSheet(f"""
            QWidget {{
                background-color: {bg_dark};
                color: {text};
            }}
        """)

        # 活动窗格高亮：多窗格分屏时给活动窗格的标题栏加一道 accent 底边
        if self._show_active_indicator and self._is_active:
            header_border = f"border-bottom: 2px solid {accent};"
        else:
            header_border = f"border-bottom: 1px solid {border};"

        self.header.setStyleSheet(f"""
            QFrame {{
                background-color: {bg_medium};
                {header_border}
            }}
            QLabel {{
                color: {text};
            }}
            QPushButton {{
                background-color: {accent};
                color: white;
                border: none;
                border-radius: 4px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('accent_hover', '#7a8efa')};
            }}
            QPushButton:disabled {{
                background-color: #3d3d5c;
                color: #666;
            }}
        """)

        # 分屏按钮：透明底，hover 时显示 accent —— 与关闭按钮风格一致
        split_btn_style = f"""
            QPushButton {{
                background-color: transparent;
                color: {text_dim};
                font-size: 15px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {accent};
                color: white;
            }}
        """
        self.split_h_btn.setStyleSheet(split_btn_style)
        self.split_v_btn.setStyleSheet(split_btn_style)

        # AI 补全开关：透明底；hover/选中（开启）时显示 accent
        toggle_btn_style = f"""
            QPushButton {{
                background-color: transparent;
                color: {text_dim};
                font-size: 16px;
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {accent};
                color: white;
            }}
            QPushButton:checked {{
                background-color: {accent};
                color: white;
            }}
        """
        self.ai_btn.setStyleSheet(toggle_btn_style)
        # Markdown 预览开关与 AI 开关同款样式
        self.md_btn.setStyleSheet(toggle_btn_style)

        # 关闭按钮特殊样式
        self.close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {text_dim};
                font-size: 18px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #ef4444;
                color: white;
            }}
        """)

        # 编辑器样式
        editor_bg = self.theme.get('terminal_bg', '#282c34')
        editor_fg = self.theme.get('terminal_fg', '#abb2bf')

        # font-size 必须写在这里：它是编辑器自己的 QSS，优先级高于主窗口 GUI 字号的
        # app 级 `QWidget { font-size }`，从而保证编辑器字号跟随终端而非被 GUI 字号钉死。
        self.editor.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {editor_bg};
                color: {editor_fg};
                border: none;
                padding: 4px 12px 4px 8px;
                font-size: {self._editor_point_size}pt;
                selection-background-color: {accent};
                selection-color: white;
            }}
        """)

        # Markdown 预览页：与编辑器同底色，用系统比例字体（阅读视图），
        # 字号跟随编辑器字号联动（标题等的相对缩放由 Qt 渲染时按默认字号换算）
        self._md_browser.setStyleSheet(f"""
            QTextBrowser {{
                background-color: {editor_bg};
                color: {editor_fg};
                border: none;
                padding: 14px 22px;
                font-size: {self._editor_point_size + 1}pt;
                selection-background-color: {accent};
                selection-color: white;
            }}
        """)
        md_palette = self._md_browser.palette()
        md_palette.setColor(QPalette.ColorRole.Link, QColor(accent))
        self._md_browser.setPalette(md_palette)

        # AI 灰字建议用暗色文字 + 编辑器背景作底色（盖住下方真实文字），跟随主题
        self.editor.set_ai_ghost_color(QColor(self.theme.get('text_dim', '#6a737d')))
        self.editor.set_ai_ghost_bg(QColor(editor_bg))
        self.editor.set_ai_ghost_fg(QColor(editor_fg))

        # 行号条配色：默认用稍暗的 bg + 暗灰 fg，浅色主题则反向
        gutter_bg = self.theme.get('editor_gutter_bg')
        gutter_fg = self.theme.get('editor_gutter_fg')
        if not gutter_bg:
            gutter_bg = "#e8e8e8" if self.theme.get('is_light_theme') else "#21252b"
        if not gutter_fg:
            gutter_fg = "#888" if self.theme.get('is_light_theme') else "#5c6370"
        if isinstance(self.editor, CodeEditor):
            self.editor.set_line_number_colors(gutter_fg, gutter_bg)
            current_line = self.theme.get('editor_current_line_bg')
            if not current_line:
                current_line = "#eaeaf2" if self.theme.get('is_light_theme') else "#2c313a"
            self.editor.set_current_line_color(current_line)

    def apply_theme(self, theme: dict):
        """应用新主题"""
        self.theme = theme
        self._apply_theme()
        if hasattr(self, 'search_bar'):
            self.search_bar.apply_theme(theme)

    def apply_language(self):
        """语言切换时更新界面文本"""
        self.close_btn.setToolTip(t("editor.close_tooltip"))
        self.split_h_btn.setToolTip(t("editor.split_h_tooltip"))
        self.split_v_btn.setToolTip(t("editor.split_v_tooltip"))
        if hasattr(self, 'ai_btn'):
            self.ai_btn.setToolTip(t("editor.ai_toggle_tooltip"))
        if hasattr(self, 'md_btn'):
            self.md_btn.setToolTip(t(
                "editor.html_preview_tooltip"
                if getattr(self, '_html_preview_supported', False)
                else "editor.md_preview_tooltip"))
        if not self._current_file:
            self.file_label.setText(t("editor.no_file"))
        if hasattr(self, 'search_bar'):
            self.search_bar.apply_language()

    def set_ai_completion_enabled(self, enabled: bool):
        """设置本窗格 AI 补全开关，并同步标题栏按钮状态。"""
        enabled = bool(enabled)
        if hasattr(self, 'ai_btn') and self.ai_btn.isChecked() != enabled:
            self.ai_btn.blockSignals(True)
            self.ai_btn.setChecked(enabled)
            self.ai_btn.blockSignals(False)
        self.editor.set_ai_completion_enabled(enabled)

    def set_word_wrap(self, enabled: bool):
        """设置本窗格自动换行（下发到内部 CodeEditor）。"""
        self.editor.set_word_wrap(bool(enabled))

    def eventFilter(self, obj, event):
        """点击 / 聚焦本窗格的编辑区时，通知 EditorArea 把本窗格设为活动窗格。

        另负责 Cmd+/（注释切换）：在 ShortcutOverride 阶段把该键声明为
        「编辑器自行处理」，绕开主窗口全局 Ctrl+/（速查表）造成的歧义快捷键
        仲裁（歧义时两边都不触发）；随后按键以普通 KeyPress 落到编辑器，
        在这里直接调用 _toggle_comment。
        """
        if event.type() in (
            QEvent.Type.FocusIn,
            QEvent.Type.MouseButtonPress,
        ):
            self.pane_focused.emit()
        elif (event.type() == QEvent.Type.Resize
                and getattr(self, '_md_browser', None) is not None
                and obj is self._md_browser.viewport()):
            # 预览视口变宽/变窄 → 防抖重渲染，让大图缩放跟上新视口宽
            if self._in_md_preview:
                self._md_refit_timer.start()
        elif obj is self.editor and event.type() in (
            QEvent.Type.ShortcutOverride,
            QEvent.Type.KeyPress,
        ):
            # macOS 上 Qt 把 Cmd 映射为 ControlModifier，故 Cmd+/ 即 Ctrl+/
            if (event.key() == Qt.Key.Key_Slash
                    and event.modifiers() == Qt.KeyboardModifier.ControlModifier):
                if event.type() == QEvent.Type.KeyPress:
                    self._toggle_comment()
                event.accept()
                return True
        return super().eventFilter(obj, event)

    def set_active(self, active: bool, show_indicator: bool = True):
        """设置本窗格是否为活动窗格（仅多窗格分屏时显示高亮指示）。"""
        if active == self._is_active and show_indicator == self._show_active_indicator:
            return
        self._is_active = active
        self._show_active_indicator = show_indicator
        self._apply_theme()

    def _save_view_state(self):
        """记录当前文件的视图位置（光标 + 滚动），切回时由 _restore_view_state 恢复。"""
        if not self._current_file or self._in_image_mode:
            return
        key = os.path.abspath(self._current_file)
        # 重插到末尾实现近似 LRU；超限时淘汰最早的记录
        _VIEW_STATE_REGISTRY.pop(key, None)
        while len(_VIEW_STATE_REGISTRY) >= _VIEW_STATE_MAX:
            _VIEW_STATE_REGISTRY.pop(next(iter(_VIEW_STATE_REGISTRY)))
        _VIEW_STATE_REGISTRY[key] = {
            'cursor': self.editor.textCursor().position(),
            'scroll': self.editor.verticalScrollBar().value(),
            'md_scroll': (self._md_browser.verticalScrollBar().value()
                          if self._md_preview_supported else 0),
        }

    def _restore_view_state(self, file_path: str):
        """切回文件时恢复离开时的位置；无记录则停留在文件开头。

        光标位置对文档长度取 min 钳制（文件可能已被外部改短）；
        滚动条超范围时由 Qt 自行钳制。"""
        state = _VIEW_STATE_REGISTRY.get(os.path.abspath(file_path))
        if not state:
            # 无记录的新文件：预览页滚动清零，避免残留上一个文件的位置
            if self._in_md_preview:
                self._md_browser.verticalScrollBar().setValue(0)
            return
        doc = self.editor.document()
        cur = self.editor.textCursor()
        cur.setPosition(min(state['cursor'], max(0, doc.characterCount() - 1)))
        self.editor.setTextCursor(cur)
        vsb = self.editor.verticalScrollBar()
        vsb.setValue(state['scroll'])
        if vsb.value() != state['scroll']:
            # 自动换行等场景下布局未完成、范围还不够：下一轮事件循环补一次
            QTimer.singleShot(0, lambda v=state['scroll']: self._apply_scroll_later(v))
        if self._in_md_preview:
            self._md_browser.verticalScrollBar().setValue(state.get('md_scroll', 0))

    def _apply_scroll_later(self, value: int):
        """布局完成后的延迟滚动恢复；窗格可能已销毁，静默忽略。"""
        try:
            self.editor.verticalScrollBar().setValue(value)
        except RuntimeError:
            pass

    def open_file(self, file_path: str) -> bool:
        """打开文件"""
        if not os.path.isfile(file_path):
            QMessageBox.warning(self, t("editor.error"), t("editor.file_not_found", path=file_path))
            return False

        # 切走前记住当前文件的视图位置（若后续校验失败留在原文件，也只是白记一笔）
        self._save_view_state()

        # 图片：走内联预览（QPixmap），跳过 "5MB 文本限制 / UTF-8 解码" 那条路径
        if Path(file_path).suffix.lower() in _IMAGE_EXTENSIONS:
            return self._open_image_file(file_path)

        # 检查文件大小，太大的文件不打开
        file_size = os.path.getsize(file_path)
        if file_size > 5 * 1024 * 1024:  # 5MB 限制
            QMessageBox.warning(self, t("editor.file_too_large_title"),
                              t("editor.file_too_large_msg", size=f"{file_size / 1024 / 1024:.1f}"))
            return False

        # 读原始字节自行解码（utf-8 → utf-8-sig → gb18030 → latin-1 兜底）；
        # 含 NUL 的二进制文件保持原先「拒绝打开」的行为
        try:
            with open(file_path, 'rb') as f:
                raw = f.read()
        except Exception as e:
            QMessageBox.warning(self, t("editor.error"), t("editor.read_error", error=e))
            return False
        if _looks_binary(raw):
            QMessageBox.warning(self, t("editor.binary_title"),
                              t("editor.binary_msg"))
            return False
        content, encoding = _decode_with_fallback(raw)

        # 如果当前文件已修改，提示保存
        if self.is_modified():
            reply = QMessageBox.question(
                self, t("editor.save_changes_title"),
                t("editor.save_changes_msg", name=os.path.basename(self._current_file)),
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    return False
            elif reply == QMessageBox.StandardButton.Cancel:
                return False
            else:
                # 用户明确放弃旧文件的修改 → 它的崩溃恢复备份也一并清掉
                self._remove_autosave_backup()

        # 切到文本编辑器视图（可能此前在显示图片）
        self._in_image_mode = False
        self._image_pixmap = None
        self._stack.setCurrentIndex(0)

        # 加载文件内容
        self._current_file = file_path
        self._file_encoding = encoding
        self._original_content = content  # 保存原始内容用于比较
        # 崩溃恢复：存在更新的自动保存备份 → 询问是否恢复（恢复后为「已修改」态）
        restored = self._maybe_restore_autosave(file_path, content)
        if restored is not None:
            self.editor.setPlainText(restored)
            self._last_autosave_hash = hashlib.sha1(
                restored.encode('utf-8')).hexdigest()
        else:
            self.editor.setPlainText(content)
            self._last_autosave_hash = None
        # 启动周期性自动保存（崩溃恢复）
        self._autosave_timer.start()

        # 切换文件时关闭已打开的查找栏（旧文件的高亮已无意义）
        if hasattr(self, 'search_bar') and not self.search_bar.isHidden():
            self.search_bar.close_search()

        # 根据文件类型设置语法高亮
        self._setup_highlighter(file_path)

        # Markdown 文件默认进渲染预览（Typora 式阅读视图），点 ◎ 切回源码编辑；
        # HTML 文件复用同一个 ◎ 按钮，但走系统浏览器预览（Qt 富文本渲染不了
        # 真实网页的 CSS/JS，内嵌 WebEngine 又太重）
        suffix = Path(file_path).suffix.lower()
        is_md = suffix in ('.md', '.markdown')
        self._set_md_support(is_md)
        self._set_html_support(suffix in ('.html', '.htm'))
        if is_md:
            self._set_md_preview(True, sync_scroll=False)

        # 恢复上次离开该文件时的视图位置（光标 + 滚动）
        self._restore_view_state(file_path)

        # 更新标题
        self._update_title()

        # 开始监听外部变更
        self._start_watching(file_path)

        return True

    def _set_md_support(self, supported: bool):
        """标记当前文件是否支持 Markdown 预览，并同步按钮可见性。"""
        supported = bool(supported)
        self._md_preview_supported = supported
        self.md_btn.setVisible(supported)
        if not supported:
            self._in_md_preview = False
            if self.md_btn.isChecked():
                self.md_btn.blockSignals(True)
                self.md_btn.setChecked(False)
                self.md_btn.blockSignals(False)
            self._md_browser.clear()

    def _set_html_support(self, supported: bool):
        """标记当前文件是否为 HTML（预览走系统浏览器），并调整 ◎ 按钮语义。

        HTML 模式下按钮不是开关（浏览器打开是一次性动作），置为不可勾选；
        与 _set_md_support 共用同一个按钮，二者互斥（一个文件不会既是 md
        又是 html），谁为真谁接管按钮的可见性/提示语。
        """
        supported = bool(supported)
        self._html_preview_supported = supported
        if supported:
            self.md_btn.setVisible(True)
            self.md_btn.setCheckable(False)
            self.md_btn.setToolTip(t("editor.html_preview_tooltip"))
        else:
            # 交还给 markdown 语义（可见性已由 _set_md_support 决定）
            self.md_btn.setCheckable(True)
            self.md_btn.setToolTip(t("editor.md_preview_tooltip"))

    def _on_preview_btn_clicked(self, checked: bool):
        """◎ 按钮点击分发：HTML → 系统浏览器；Markdown → 内置渲染预览。"""
        if getattr(self, '_html_preview_supported', False):
            self._open_html_preview()
        else:
            self._set_md_preview(checked)

    def _open_html_preview(self):
        """把当前 HTML 文件交给系统默认浏览器预览。

        有未保存修改时先落盘——预览的意义就是看当前内容；不落盘的话
        浏览器打开的是旧版本，更迷惑。save_file 失败（只读/冲突弹窗被
        取消等）则不打开，避免看着旧内容误以为是新改动。
        """
        if not self._current_file:
            return
        if self.is_modified() and not self.save_file():
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._current_file))

    def _toggle_md_preview(self):
        """快捷键入口：markdown 文件上切换 源码/预览；HTML 文件打开浏览器
        预览；其他文件无操作。"""
        if getattr(self, '_html_preview_supported', False) and not self._in_image_mode:
            self._open_html_preview()
        elif self._md_preview_supported and not self._in_image_mode:
            self._set_md_preview(not self._in_md_preview)

    def _set_md_preview(self, on: bool, sync_scroll: bool = True):
        """切换 源码编辑 / 渲染预览。预览为只读，不触碰编辑器缓冲区。

        sync_scroll: 是否按滚动比例同步阅读位置。用户手动切换时要同步；
        open_file 的程序性切换传 False——那时源码还在顶部，同步的延迟校正
        会把 _restore_view_state 刚恢复的预览位置踩回去。
        """
        on = bool(on) and self._md_preview_supported
        if self.md_btn.isChecked() != on:
            self.md_btn.blockSignals(True)
            self.md_btn.setChecked(on)
            self.md_btn.blockSignals(False)
        was = self._in_md_preview
        self._in_md_preview = on
        if on:
            # 查找栏作用于源码编辑器，预览下先收起
            if hasattr(self, 'search_bar') and not self.search_bar.isHidden():
                self.search_bar.close_search()
            self._render_md_preview()
            self._stack.setCurrentIndex(2)
            if not was and sync_scroll:
                self._sync_md_scroll(to_preview=True)
        elif not self._in_image_mode:
            self._stack.setCurrentIndex(0)
            if was and sync_scroll:
                self._sync_md_scroll(to_preview=False)
            self.editor.setFocus()

    def _sync_md_scroll(self, to_preview: bool):
        """源码↔预览互切时按滚动比例近似同步阅读位置。

        两边文档结构不同（预览里图片占高、代码围栏行数不同），做不到
        精确的行级映射；按 scrollbar 比例映射对 README 类文档已足够贴近，
        且行为可预期。预览内部的重渲染（外部变更、缩放）不走这里，
        仍由 _render_md_preview 原样保留预览滚动位置。
        """
        # 预览文档布局是惰性的：先强制完成整篇布局，scrollbar 范围才有效
        self._md_browser.document().size()
        esb = self.editor.verticalScrollBar()
        psb = self._md_browser.verticalScrollBar()
        src, dst = (esb, psb) if to_preview else (psb, esb)
        frac = src.value() / src.maximum() if src.maximum() > 0 else 0.0

        def apply():
            try:
                dst.setValue(round(frac * dst.maximum()))
            except RuntimeError:
                pass   # 窗格已销毁
        # 立即设一次，再延迟一拍校正：刚从 QStackedWidget 隐藏页切出来时
        # 目标视口几何还没稳定，scrollbar 范围会在下一轮事件循环重算
        apply()
        QTimer.singleShot(0, apply)

    def _md_refit_tick(self):
        """视口 resize 防抖到点后的预览重渲染。

        两道闸：正在拖拽预览滚动条时顺延（重渲染会整个重建文档、重置
        滚动条，把拖到一半的拖拽直接打断——弹簧模式下点击预览就会触发
        resize，表现为"拖着拖着就断了"）；视口宽度没变时跳过（高度变化
        不影响图片按宽缩放，重渲染纯属浪费且会打断选区/滚动惯性）。
        """
        if not self._in_md_preview:
            return
        if (self._md_browser.verticalScrollBar().isSliderDown()
                or self._md_browser.horizontalScrollBar().isSliderDown()):
            self._md_refit_timer.start()   # 拖完这一下再渲染
            return
        if self._md_browser.viewport().width() == self._md_last_render_width:
            return
        self._render_md_preview()

    def _render_md_preview(self):
        """把编辑器缓冲区的 Markdown 源文本渲染到预览页（单向，只读）。

        baseUrl 指向文件所在目录，使相对路径的本地图片能显示（只读加载）。
        重渲染尽量保留预览页滚动位置（外部变更重载时不跳回顶部）。
        """
        self._md_last_render_width = self._md_browser.viewport().width()
        doc = self._md_browser.document()
        if self._current_file:
            doc.setBaseUrl(QUrl.fromLocalFile(
                os.path.dirname(os.path.abspath(self._current_file)) + os.sep))
        scroll_pos = self._md_browser.verticalScrollBar().value()
        # MarkdownNoHTML：禁用原始 HTML 解析。否则正文里出现 <tag> 这类未闭合
        # HTML 时，Qt 会把其后的所有内容当 HTML 吞掉（文档从那里被截断）；
        # 禁用后 HTML 按字面文本显示，同时也杜绝了 HTML 注入的渲染面。
        doc.setMarkdown(
            self.editor.toPlainText(),
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML,
        )
        self._inline_md_html_images(doc)
        self._apply_md_html_layout(doc)
        self._fit_md_images(doc)
        self._preload_md_images(doc)
        self._prepare_md_blocks(doc)
        self._polish_md_document(doc)
        self._md_browser.verticalScrollBar().setValue(scroll_pos)

    def _fit_md_images(self, doc):
        """把宽于视口的内嵌图片等比缩到视口宽（GitHub 风格），消除横向滚动。

        只缩不放：窄图保持原尺寸/显式尺寸。natural 尺寸用 QImageReader
        只读文件头，不解码整图。视口 resize 时由防抖重渲染重新计算。
        """
        avail = float(self._md_browser.viewport().width()
                      - 2 * doc.documentMargin() - 8)
        if avail < 100:   # 视口还没布局好（宽度无意义）时不缩放
            return
        base_dir = (os.path.dirname(os.path.abspath(self._current_file))
                    if self._current_file else '')

        fits = []   # (pos, len, new_fmt)
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                cf = frag.charFormat()
                if cf.isImageFormat():
                    fmt = cf.toImageFormat()
                    w, h = fmt.width(), fmt.height()
                    if w <= 0:
                        # 无显式宽度 → 读自然尺寸（本地文件才读得到）
                        name = fmt.name()
                        path = (name if os.path.isabs(name)
                                else os.path.join(base_dir, name))
                        size = QImageReader(path).size()
                        if not size.isValid():
                            it += 1
                            continue
                        w = float(size.width())
                        if h <= 0:
                            h = float(size.height())
                        else:
                            h = 0.0  # 显式高度 + 自然宽度的组合极罕见，交给 Qt
                    if w > avail:
                        nf = QTextImageFormat(fmt)
                        nf.setWidth(avail)
                        if h > 0:
                            nf.setHeight(h * avail / w)
                        fits.append((frag.position(), frag.length(), nf))
                it += 1
            block = block.next()

        for pos, ln, nf in fits:
            c = QTextCursor(doc)
            c.setPosition(pos)
            c.setPosition(pos + ln, QTextCursor.MoveMode.KeepAnchor)
            c.setCharFormat(nf)

    def _preload_md_images(self, doc):
        """渲染时一次性解码文档引用的本地图片，预缩放后注册为文档资源。

        Qt 默认在图片首次滚入视口被绘制时才同步解码文件（实测 README 里
        数 MB 的截图 PNG 首帧 19–90ms，弱机器更久），表现为滚动到图片处
        明显卡一下；且每次重渲染（setMarkdown）都清空文档资源缓存，视口
        resize 防抖重渲染后卡顿会反复出现。这里把解码挪到渲染时一次做完，
        有显式显示宽度的图按 显示宽 × DPR 预缩放（只缩不放，省内存也省
        每帧缩放），结果缓存在窗格上跨重渲染复用——滚动路径只剩贴图。
        """
        base_dir = (os.path.dirname(os.path.abspath(self._current_file))
                    if self._current_file else '')
        dpr = self._md_browser.devicePixelRatioF() or 1.0
        used = {}
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                cf = frag.charFormat()
                if cf.isImageFormat():
                    name = cf.toImageFormat().name()
                    path = (name if os.path.isabs(name)
                            else os.path.join(base_dir, name))
                    try:
                        mtime = os.stat(path).st_mtime_ns
                    except OSError:
                        it += 1
                        continue   # 网络图/不存在的路径维持原行为
                    disp_w = cf.toImageFormat().width()
                    target_w = int(disp_w * dpr) if disp_w > 0 else 0
                    key = (path, mtime, target_w)
                    img = self._md_img_cache.get(key)
                    if img is None:
                        img = QImage(path)
                        if img.isNull():
                            it += 1
                            continue
                        if 0 < target_w < img.width():
                            img = img.scaledToWidth(
                                target_w,
                                Qt.TransformationMode.SmoothTransformation)
                            # 布局尺寸由 QTextImageFormat 的显式宽高决定，
                            # DPR 标记只为让 1:1 贴图路径成立（Retina 不糊）
                            img.setDevicePixelRatio(dpr)
                    used[key] = img
                    # 同时按原始名与 baseUrl 解析后的 URL 注册：绘制端以
                    # QUrl(name) 查询，QTextDocument 内部又可能先做解析
                    doc.addResource(QTextDocument.ResourceType.ImageResource,
                                    QUrl(name), img)
                    doc.addResource(QTextDocument.ResourceType.ImageResource,
                                    doc.baseUrl().resolved(QUrl(name)), img)
                it += 1
            block = block.next()
        # 只保留本轮仍在用的条目，文件切换/图片删除后不积压内存
        self._md_img_cache = used

    # 匹配按字面显示的 <img ...> 标签（NoHTML 模式的产物）及其属性
    _MD_IMG_TAG_RE = re.compile(r'<img\b[^<>]*?>', re.IGNORECASE)
    _MD_IMG_ATTR_RE = re.compile(
        r'''(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)')''')

    def _inline_md_html_images(self, doc):
        """把 NoHTML 模式下按字面显示的 <img> 标签替换成真正的内嵌图片。

        MarkdownNoHTML 是防截断/防注入的刚需（见 _render_md_preview），但
        README 常用 <img width=...> 控制图片尺寸。这里不引入 HTML 引擎，
        只把 <img> 文本换成 QTextImageFormat 内嵌图——加载走 baseUrl 相对
        路径的只读通道，与 markdown ![]() 图片完全同一条路，width/height
        属性映射到图片格式。代码块/行内代码里的 <img> 是示例代码，不动。
        """
        def _overlaps_mono(blk, start, end):
            it = blk.begin()
            while not it.atEnd():
                frag = it.fragment()
                fs = frag.position() - blk.position()
                if (frag.charFormat().fontFixedPitch()
                        and fs < end and start < fs + frag.length()):
                    return True
                it += 1
            return False

        replacements = []
        block = doc.begin()
        while block.isValid():
            bf = block.blockFormat()
            is_code = (bf.hasProperty(QTextFormat.Property.BlockCodeFence)
                       or bf.hasProperty(
                           QTextFormat.Property.BlockCodeLanguage)
                       or bf.nonBreakableLines())
            if not is_code:
                text = block.text()
                for m in self._MD_IMG_TAG_RE.finditer(text):
                    if _overlaps_mono(block, m.start(), m.end()):
                        continue
                    attrs = {k.lower(): (v1 if v1 is not None else v2)
                             for k, v1, v2 in
                             self._MD_IMG_ATTR_RE.findall(m.group(0))}
                    src = attrs.get('src')
                    if not src:
                        continue
                    fmt = QTextImageFormat()
                    fmt.setName(src)
                    for attr, setter in (('width', fmt.setWidth),
                                         ('height', fmt.setHeight)):
                        val = (attrs.get(attr) or '').rstrip('px')
                        if val.isdigit():
                            setter(float(val))
                    replacements.append((block.position() + m.start(),
                                         block.position() + m.end(), fmt))
            block = block.next()

        # 从后往前替换，避免前面的替换使后面记录的位置失效
        for start, end, fmt in reversed(replacements):
            c = QTextCursor(doc)
            c.setPosition(start)
            c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            c.removeSelectedText()
            c.insertImage(fmt)

    # <br> 换行标签；块首的容器开标签 / 块尾的闭标签；同块内成对的“<p>内容</p>”。
    # 注意 markdown 会把相邻行合并进同一段落：
    #   <div align="center">\n<img ...> → 一个 block「<div ...> ￼」
    #   **text**\n</div>              → 一个 block「text </div>」
    # 所以容器标签按“块首前缀/块尾后缀”识别，而不是按独占一行识别。
    _MD_BR_RE = re.compile(r'<br\s*/?>', re.IGNORECASE)
    _MD_TAG_OPEN_RE = re.compile(
        r'^\s*<(div|p|center|section)\b([^<>]*)>\s*', re.IGNORECASE)
    _MD_TAG_CLOSE_RE = re.compile(
        r'\s*</(div|p|center|section)\s*>\s*$', re.IGNORECASE)
    _MD_TAG_WRAP_RE = re.compile(
        r'^\s*(<(div|p|center|section)\b[^<>]*>)\s*(\S.*?)\s*(</\2\s*>)\s*$',
        re.IGNORECASE)
    _MD_ALIGN_ATTR_RE = re.compile(
        r'''align\s*=\s*["']?(center|left|right|justify)''', re.IGNORECASE)
    # HTML 注释（README 模板里常见）。跨空行的注释开闭会落在不同 block，
    # 配不上就保持字面——与容器标签同策略
    _MD_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)

    def _apply_md_html_layout(self, doc):
        """把字面显示的 <br> 与对齐容器标签还原成对应排版（NoHTML 白名单第二批）。

        - <br> → 行分隔符 U+2028（同段内换行）
        - <div/p/center/section ...> ... </...> 容器：剥掉标签文本，按 align
          属性（<center> 视为居中）给 开块..闭块 范围内的 block 设对齐；
          没有 align 的容器只剥标签。剥完为空的 block 整行删除。
          开闭配不上对的标签保持字面显示不动。
        同样只做文本→格式的等价替换；代码块/行内代码里的标签是示例，不动。
        """
        aligns = {'center': Qt.AlignmentFlag.AlignHCenter,
                  'left': Qt.AlignmentFlag.AlignLeft,
                  'right': Qt.AlignmentFlag.AlignRight,
                  'justify': Qt.AlignmentFlag.AlignJustify}

        def _overlaps_mono(blk, start, end):
            it = blk.begin()
            while not it.atEnd():
                frag = it.fragment()
                fs = frag.position() - blk.position()
                if (frag.charFormat().fontFixedPitch()
                        and fs < end and start < fs + frag.length()):
                    return True
                it += 1
            return False

        def _container_align(open_tag_text, tag_name):
            am = self._MD_ALIGN_ATTR_RE.search(open_tag_text)
            if am:
                return aligns.get(am.group(1).lower())
            return aligns['center'] if tag_name.lower() == 'center' else None

        # 第一遍只收集，不动文档：blocks 快照、文本删改、对齐 op、容器开闭配对。
        # 开标签的剥除挂在 opens 栈上，配对成功才生效——不配对保持字面。
        blocks = []       # (pos, length, is_code, in_table)
        edits = []        # (start, end, replacement)
        align_ops = []    # (block_pos, AlignmentFlag)
        opens = []        # 栈: (tag, align_or_None, block_idx, strip_edit)
        pairs = []        # (open_idx, close_idx, align, [strip_edits])
        comment_blocks = []   # 剥掉注释后整块为空 → 整行删
        last_pos = 0

        block = doc.begin()
        while block.isValid():
            bf = block.blockFormat()
            is_code = (bf.hasProperty(QTextFormat.Property.BlockCodeFence)
                       or bf.hasProperty(
                           QTextFormat.Property.BlockCodeLanguage)
                       or bf.nonBreakableLines())
            in_table = QTextCursor(block).currentTable() is not None
            idx = len(blocks)
            blocks.append((block.position(), block.length(),
                           is_code, in_table))
            last_pos = block.position() + block.length()
            if is_code:
                block = block.next()
                continue
            text = block.text()
            p = block.position()

            # HTML 注释剥离。后续容器/br 匹配全部基于 masked（注释区替换成
            # 等长空格）：注释内的标签/<br> 是被注释掉的内容，不该生效，
            # 且等长屏蔽保证 masked 上的匹配位置与原文一一对应
            comment_spans = [
                (m.start(), m.end())
                for m in self._MD_COMMENT_RE.finditer(text)
                if not _overlaps_mono(block, m.start(), m.end())
            ]
            masked = text
            for cs, ce in comment_spans:
                masked = masked[:cs] + ' ' * (ce - cs) + masked[ce:]
            if comment_spans:
                if not masked.strip():
                    # 整块只有注释 → 整行删，不留空段落
                    comment_blocks.append(idx)
                    block = block.next()
                    continue
                for cs, ce in comment_spans:
                    edits.append((p + cs, p + ce, ''))

            # 同块既有注释又有容器标签的组合极罕见；两者的删除区间可能
            # 交叠（先删注释会使外层区间失效）→ 该块只剥注释、容器保持字面
            if comment_spans:
                wm = om = cm = None
            else:
                wm = self._MD_TAG_WRAP_RE.match(masked)
                om = self._MD_TAG_OPEN_RE.match(masked)
                cm = self._MD_TAG_CLOSE_RE.search(masked)
            if wm and not _overlaps_mono(block, 0, len(text)):
                # 同块成对：剥首尾标签（连同内侧空白），本块按 align 对齐
                flag = _container_align(wm.group(1), wm.group(2))
                if flag is not None:
                    align_ops.append((p, flag))
                edits.append((p + wm.start(1), p + wm.start(3), ''))
                edits.append((p + wm.end(3), p + wm.end(4), ''))
            elif om and not _overlaps_mono(block, om.start(), om.end()):
                flag = _container_align(om.group(0), om.group(1))
                if text[om.end():].strip():
                    strip = ('range', idx, p + om.start(), p + om.end())
                else:
                    strip = ('block', idx)   # 剥完为空 → 整行删
                opens.append((om.group(1).lower(), flag, idx, strip))
            elif cm and not _overlaps_mono(block, cm.start(), cm.end()):
                tag = cm.group(1).lower()
                # 就近配对同名开标签；配不上则保持字面
                for k in range(len(opens) - 1, -1, -1):
                    if opens[k][0] == tag:
                        if text[:cm.start()].strip():
                            strip = ('range', idx, p + cm.start(),
                                     p + cm.end())
                        else:
                            strip = ('block', idx)
                        pairs.append((opens[k][2], idx, opens[k][1],
                                      [opens[k][3], strip]))
                        del opens[k:]   # 更晚的未闭合标签一并放弃
                        break
            for m in self._MD_BR_RE.finditer(masked):
                if not _overlaps_mono(block, m.start(), m.end()):
                    edits.append((p + m.start(), p + m.end(),
                                  '\u2028'))  # 行分隔符：段内换行
            block = block.next()

        doc_end = last_pos - 1   # 最后一个 block 的 length 含文档终结符
        deleted = set()

        def _delete_block(bidx):
            """整行删（含分隔符；末 block 改吃前导分隔符）"""
            if bidx in deleted:
                return
            deleted.add(bidx)
            pos, length, _, _ = blocks[bidx]
            if pos + length > doc_end:
                edits.append((max(0, pos - 1), doc_end, ''))
            else:
                edits.append((pos, pos + length, ''))

        for bidx in comment_blocks:
            _delete_block(bidx)
        for open_idx, close_idx, flag, strips in pairs:
            for strip in strips:
                if strip[0] == 'block':
                    _delete_block(strip[1])
                else:
                    edits.append((strip[2], strip[3], ''))
            if flag is not None:
                # 开/闭标签与内容可能共享 block，范围取两端闭区间
                for bidx in range(open_idx, close_idx + 1):
                    pos, _, is_code, in_table = blocks[bidx]
                    if not is_code and not in_table and bidx not in deleted:
                        align_ops.append((pos, flag))

        # 先设对齐（不移动文本），再从后往前做文本删改
        for pos, flag in align_ops:
            abf = QTextBlockFormat()
            abf.setAlignment(flag)
            c = QTextCursor(doc)
            c.setPosition(pos)
            c.mergeBlockFormat(abf)
        for start, end, repl in sorted(edits, reverse=True):
            c = QTextCursor(doc)
            c.setPosition(start)
            c.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            c.removeSelectedText()
            if repl:
                c.insertText(repl)

    # 代码块 padding 块的标记属性（UserProperty 起始位，值 'top'/'bottom'）
    _MD_PAD_PROP = QTextFormat.Property.UserProperty

    def _prepare_md_blocks(self, doc):
        """排版结构准备（在精修之前做的文档手术，仅预览文档）。

        1. 代码块面板内边距：Qt 的块背景不含 padding，在每个代码围栏
           前后插入一个固定高度的空块（带同款底色）充当上下内边距，
           视觉上等价 GitHub 的代码面板。空块用 UserProperty 标记，
           精修 pass 据此设置高度/边距。
        2. 引用块左侧竖条：Qt 块格式没有单边 border，在每个引用块行首
           插入 U+258E（▎）字符染成边框色——GitHub 引用条的近似。
        文本插入只发生在预览文档，不触碰编辑器缓冲区。
        """
        def _is_code(blk):
            if not blk.isValid():
                return False
            f = blk.blockFormat()
            return (f.hasProperty(QTextFormat.Property.BlockCodeFence)
                    or f.hasProperty(QTextFormat.Property.BlockCodeLanguage)
                    or f.nonBreakableLines())

        def _lang(blk):
            return blk.blockFormat().stringProperty(
                QTextFormat.Property.BlockCodeLanguage)

        # 收集：代码围栏（连续同语言 code 块）与引用块，全部先收集后修改
        runs = []      # (first_pos, last_block_end_pos, indent)
        quotes = []    # block_pos
        code_line_starts = []   # 每个代码行行首位置（插入左内边距空格用）
        block = doc.begin()
        while block.isValid():
            bf = block.blockFormat()
            if bf.hasProperty(QTextFormat.Property.BlockQuoteLevel):
                quotes.append(block.position())
            if _is_code(block):
                prev = block.previous()
                if not (_is_code(prev) and _lang(prev) == _lang(block)):
                    first = block
                    last = block
                    nxt = block.next()
                    while _is_code(nxt) and _lang(nxt) == _lang(block):
                        last = nxt
                        nxt = nxt.next()
                    # 记录围栏的缩进：列表内嵌代码块 indent=1，块背景从
                    # 缩进位置才开始画——pad 块必须继承同样的缩进，否则
                    # 面板左缘错位出现黑色缺口
                    runs.append((first.position(),
                                 last.position() + last.length() - 1,
                                 first.blockFormat().indent()))
                    ln = first
                    while True:
                        code_line_starts.append(ln.position())
                        if ln == last:
                            break
                        ln = ln.next()
            block = block.next()

        # 从后往前做插入，避免位置失效
        edits = []   # (pos, kind, indent)
        for first_pos, last_end, indent in runs:
            edits.append((last_end, 'pad-bottom', indent))
            if first_pos > 0:
                edits.append((first_pos - 1, 'pad-top', indent))
        for qpos in quotes:
            edits.append((qpos, 'quote-bar', 0))
        for cpos in code_line_starts:
            edits.append((cpos, 'code-left-pad', 0))
        edits.sort(reverse=True)

        bar_fmt = QTextCharFormat()
        bar_fmt.setForeground(QColor(self.theme.get('border', '#3d3d5c')))
        # pad 块用小字号控制高度（空块行高跟随块字符格式的字体）。
        # 不能用 LineHeight FixedHeight：布局占整行高、绘制只画固定高度，
        # 面板区域会整体错位出现「代码行被裁半」的假象（实测踩过）
        pad_char = QTextCharFormat()
        pad_char.setFontPointSize(max(4.0, (self._editor_point_size + 1) * 0.42))
        for pos, kind, indent in edits:
            c = QTextCursor(doc)
            c.setPosition(pos)
            if kind == 'quote-bar':
                c.insertText('▎ ', bar_fmt)
            elif kind == 'code-left-pad':
                # 面板左内边距：textIndent 会把含 CJK 行的块背景一并右移
                # 造成左缘缺口，改插两个真实空格（仅预览文档；从预览复制
                # 会带统一的两格前导空格，相对缩进不受影响）
                c.insertText('  ')
            else:
                nbf = QTextBlockFormat()
                nbf.setProperty(self._MD_PAD_PROP,
                                'top' if kind == 'pad-top' else 'bottom')
                nbf.setIndent(indent)   # 与围栏同缩进，面板左缘对齐
                c.insertBlock(nbf, pad_char)

    # 标题排版表：级别 → (字号倍率, 上留白, 下留白)，留白以正文字号为单位。
    # 仿 GitHub 阅读视图的比例：H1 约 1.7 倍正文而非 Qt 默认的 2 倍。
    _MD_HEADING_STYLE = {
        1: (1.70, 1.5, 0.65),
        2: (1.40, 1.3, 0.55),
        3: (1.20, 1.1, 0.45),
        4: (1.05, 1.0, 0.40),
        5: (0.95, 1.0, 0.40),
        6: (0.85, 1.0, 0.40),
    }

    def _polish_md_document(self, doc):
        """setMarkdown 之后的排版精修（只改预览文档的显示格式，不碰编辑器缓冲区）。

        Qt 原生 markdown 排版直接看很粗糙：H1 是正文 2 倍大且上下零留白、
        全文无行距、代码块无底色、引用块左右各硬缩进 40px、表格是零内边距
        的默认灰线。这里逐 block 重排：标题按倍率表收敛并加留白、正文/列表
        加行距与段距、代码块加底色和等宽字号、行内代码加底色小片、引用块
        改浅色窄缩进、表格加内边距。字号全部相对正文字号计算，缩放时
        set_editor_point_size 会重渲染，各级字号随之重算。
        """
        base = float(self._editor_point_size + 1)   # 与 QSS 里预览正文字号一致
        heading_fg = QColor(self.theme.get('text', '#eaeaea'))
        dim_fg = QColor(self.theme.get('text_dim', '#888888'))
        editor_bg = QColor(self.theme.get('terminal_bg', '#282c34'))
        # 代码底色：深色主题上提亮、浅色主题上压暗，保证与正文底色可感知区分
        # 深色主题用「加法」提亮而不是 lighter()（乘法）：Midnight Black 等
        # 接近纯黑的底色乘完仍是黑，面板完全看不出来；固定增量保证任何
        # 暗度下都有可感知的对比（略偏蓝灰，贴近 GitHub 代码面板的色感）
        if editor_bg.lightness() < 128:
            code_bg = QColor(min(255, editor_bg.red() + 18),
                             min(255, editor_bg.green() + 21),
                             min(255, editor_bg.blue() + 30))
        else:
            code_bg = editor_bg.darker(107)
        mono_family = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont).family()
        prop_h = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value

        def _is_code_block(blk):
            if not blk.isValid():
                return False
            f = blk.blockFormat()
            return (f.hasProperty(QTextFormat.Property.BlockCodeFence)
                    or f.hasProperty(QTextFormat.Property.BlockCodeLanguage)
                    or f.nonBreakableLines())

        edit_cursor = QTextCursor(doc)
        edit_cursor.beginEditBlock()
        block = doc.begin()
        while block.isValid():
            bf = block.blockFormat()
            lvl = bf.headingLevel()
            sel = QTextCursor(block)
            sel.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                             QTextCursor.MoveMode.KeepAnchor)
            nbf = QTextBlockFormat()

            if lvl:
                scale, top, bottom = self._MD_HEADING_STYLE.get(
                    lvl, (1.0, 1.0, 0.4))
                # FontSizeAdjustment（h1 高达 2 倍）只要存在就会盖过显式字号，
                # 而 mergeCharFormat 删不掉属性 → 逐 fragment 复制原格式、
                # clearProperty 后整体替换（保留链接/行内代码等其余属性）。
                # 先收集再应用：改格式会分裂/合并 fragment，边遍历边改会使
                # 迭代器失效
                frags = []
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    frags.append((frag.position(), frag.length(),
                                  QTextCharFormat(frag.charFormat())))
                    it += 1
                for fpos, flen, hcf in frags:
                    hcf.clearProperty(
                        QTextFormat.Property.FontSizeAdjustment)
                    hcf.setFontPointSize(base * scale)
                    hcf.setFontWeight(QFont.Weight.Bold if lvl <= 2
                                      else QFont.Weight.DemiBold)
                    hcf.setForeground(heading_fg)
                    fsel = QTextCursor(doc)
                    fsel.setPosition(fpos)
                    fsel.setPosition(fpos + flen,
                                     QTextCursor.MoveMode.KeepAnchor)
                    fsel.setCharFormat(hcf)
                nbf.setTopMargin(base * top)
                nbf.setBottomMargin(base * bottom)
                nbf.setLineHeight(125, prop_h)
                if lvl <= 2:
                    # GitHub 风格：H1/H2 下方一条全宽分隔线
                    nbf.setProperty(
                        QTextFormat.Property.BlockTrailingHorizontalRulerWidth,
                        QTextLength(QTextLength.Type.PercentageLength, 100))
            elif block.blockFormat().hasProperty(self._MD_PAD_PROP):
                # 代码面板的上/下内边距块（_prepare_md_blocks 插入）：
                # 固定小高度 + 代码底色；外边距承接整个围栏的段间距
                kind = block.blockFormat().stringProperty(self._MD_PAD_PROP)
                nbf.setBackground(code_bg)
                nbf.setTopMargin(base * 0.7 if kind == 'top' else 0.0)
                nbf.setBottomMargin(base * 0.7 if kind == 'bottom' else 0.0)
            elif _is_code_block(block):
                # 代码块每行一个 block：整片底色相连，行首缩进当左内边距用。
                # 上下外边距由 _prepare_md_blocks 插入的 padding 块承接
                # （它们同时充当面板的上下内边距），本体全部归零。
                # 不能加比例行距——多出的行间空隙不带底色会切断底色片
                nbf.setBackground(code_bg)
                # 不用 textIndent 做左内边距：含 CJK/破折号的行会把块背景
                # 一并右移 textIndent 宽度（纯 ASCII 行不移），面板左缘出现
                # 参差缺口。左内边距改由每行行首的真实空格观感承担（无）
                # ——保持背景完全平齐优先
                nbf.setTextIndent(0.0)
                nbf.setTopMargin(0.0)
                nbf.setBottomMargin(0.0)
                ccf = QTextCharFormat()
                ccf.setFontFamilies([mono_family])
                ccf.setFontPointSize(base * 0.9)
                sel.mergeCharFormat(ccf)
            elif bf.hasProperty(QTextFormat.Property.BlockQuoteLevel):
                # GitHub 式引用：行首 ▎竖条（_prepare_md_blocks 插入）+
                # 暗色文字 + 小缩进
                qlvl = max(1, bf.intProperty(QTextFormat.Property.BlockQuoteLevel))
                nbf.setLeftMargin(base * 0.4 * qlvl)
                nbf.setRightMargin(0.0)
                nbf.setTopMargin(base * 0.4)
                nbf.setBottomMargin(base * 0.4)
                nbf.setLineHeight(150, prop_h)
                qcf = QTextCharFormat()
                qcf.setForeground(dim_fg)
                sel.mergeCharFormat(qcf)
            elif bf.hasProperty(
                    QTextFormat.Property.BlockTrailingHorizontalRulerWidth):
                nbf.setTopMargin(base * 0.9)
                nbf.setBottomMargin(base * 0.9)
            elif QTextCursor(block).currentTable() is not None:
                if not block.text():
                    # Qt 导入怪癖：表格前若紧跟引用块等结构，帧分隔的空 block
                    # 会留在第一个单元格里，把表头文字顶下去一行 → 压成 1px
                    nbf.setLineHeight(
                        1.0,
                        QTextBlockFormat.LineHeightTypes.FixedHeight.value)
                    nbf.setTopMargin(0.0)
                    nbf.setBottomMargin(0.0)
                else:
                    # 单元格正文：行距略收紧，别带上段落的大段距
                    nbf.setLineHeight(135, prop_h)
                    nbf.setTopMargin(2.0)
                    nbf.setBottomMargin(2.0)
            elif block.textList() is not None:
                nbf.setLineHeight(150, prop_h)
                nbf.setTopMargin(base * 0.15)
                nbf.setBottomMargin(base * 0.15)
                nbf.setLeftMargin(base * 0.5)   # 列表整体右移，靠近 GitHub 缩进
            elif not block.text().strip():
                # 结构分隔用的空 block（引用/表格后 Qt 会插一个）：不占空间
                nbf.setTopMargin(0.0)
                nbf.setBottomMargin(0.0)
            else:
                nbf.setLineHeight(152, prop_h)
                nbf.setTopMargin(0.0)
                nbf.setBottomMargin(base * 0.6)
            # 含内嵌图片（U+FFFC）的行禁用比例行距：Qt 按“行内最高元素×比例”
            # 算行高，多出的百分比全是空白——大图下方会凭空出现整屏空隙
            if '\ufffc' in block.text():  # ObjectReplacementCharacter 即内嵌图
                nbf.clearProperty(QTextFormat.Property.LineHeight)
                # LineHeightType 必须一起清：只清高度会留下
                # “Proportional + 0%”，整行塌成零高、图片压到后续内容上
                nbf.clearProperty(QTextFormat.Property.LineHeightType)
            sel.mergeBlockFormat(nbf)

            # 行内元素精修（整块代码已在上面处理，标题的 fragment 已整体替换）：
            # 行内代码 → 等宽小片 + 底色 + 提亮；链接 → 主题 accent 色
            # （QSS 样式表下 palette 的 Link 色不生效，锚点会渲染成默认纯蓝）
            if not _is_code_block(block) and not lvl:
                patches = []   # 先收集再应用，理由同标题分支
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    fcf = frag.charFormat()
                    patch = None
                    if fcf.fontFixedPitch():
                        patch = QTextCharFormat()
                        patch.setBackground(code_bg)
                        patch.setForeground(heading_fg)
                        patch.setFontFamilies([mono_family])
                        patch.setFontPointSize(base * 0.9)
                    elif fcf.isAnchor():
                        patch = QTextCharFormat()
                        patch.setForeground(
                            QColor(self.theme.get('accent', '#667eea')))
                    if patch is not None:
                        patches.append(
                            (frag.position(), frag.length(), patch))
                    it += 1
                for fpos, flen, patch in patches:
                    fsel = QTextCursor(doc)
                    fsel.setPosition(fpos)
                    fsel.setPosition(fpos + flen,
                                     QTextCursor.MoveMode.KeepAnchor)
                    fsel.mergeCharFormat(patch)
            block = block.next()

        # 表格：默认零内边距 + 2px 缝隙灰线 → 实线边框 + 内边距 + 上下留白
        border_col = QColor(self.theme.get('border', '#3d3d5c'))

        def _fix_tables(frame):
            for child in frame.childFrames():
                _fix_tables(child)
                if isinstance(child, QTextTable):
                    tf = child.format()
                    tf.setCellPadding(base * 0.5)
                    tf.setCellSpacing(0.0)
                    tf.setBorder(1.0)
                    tf.setBorderBrush(border_col)
                    tf.setBorderStyle(
                        QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                    tf.setTopMargin(base * 0.6)
                    tf.setBottomMargin(base * 0.6)
                    child.setFormat(tf)

        _fix_tables(doc.rootFrame())
        edit_cursor.endEditBlock()

    def _on_md_link_clicked(self, url: QUrl):
        """预览页链接点击：http/https 放行到系统浏览器；#锚点 跳到对应标题。
        其余（本地文件/相对链接/其他 scheme）一律忽略，防止预览页意外导航
        或触碰本地文件。"""
        if url.scheme() in ('http', 'https'):
            QDesktopServices.openUrl(url)
            return
        # 文档内锚点（GitHub 目录写法 [..](#features--功能)）
        if url.hasFragment() and not url.path() and not url.scheme():
            self._md_scroll_to_heading(url.fragment())

    @staticmethod
    def _md_github_slug(text: str) -> str:
        """GitHub 风格的标题锚点 slug：小写、去标点（保留字母数字/CJK/
        连字符/下划线）、每个空格换成一个连字符。"""
        t = text.strip().lower()
        t = re.sub(r'[^\w\- ]', '', t)
        return t.replace(' ', '-')

    def _md_scroll_to_heading(self, fragment: str):
        """滚动预览到 slug 匹配的标题；找不到则原地不动。"""
        want = fragment.strip().lower()
        if not want:
            return
        doc = self._md_browser.document()
        doc.size()   # 惰性布局：先完成整篇布局，blockBoundingRect 才可靠
        collapse = lambda s: re.sub(r'-{2,}', '-', s)
        block = doc.begin()
        while block.isValid():
            if block.blockFormat().headingLevel():
                slug = self._md_github_slug(block.text())
                # 宽松一档：连续连字符折叠后也算命中（slug 方言差异）
                if slug == want or collapse(slug) == collapse(want):
                    r = doc.documentLayout().blockBoundingRect(block)
                    self._md_browser.verticalScrollBar().setValue(
                        max(0, int(r.y()) - 8))
                    return
            block = block.next()

    def _show_md_context_menu(self, pos):
        """预览页右键菜单：Qt 标准项（复制/全选）+ 源码页同款窗格级动作。

        动作追加复用 CodeEditor.append_pane_actions——分屏/移动信号仍从
        源码编辑器冒泡到本窗格，与源码页路径一致。
        """
        menu = self._md_browser.createStandardContextMenu(pos)
        self.editor.append_pane_actions(menu)
        menu.exec(self._md_browser.mapToGlobal(pos))

    def goto_line(self, line_no: int):
        """把光标移动到第 line_no 行（1 基）并滚动到可见处。
        用于从内容搜索结果跳转。图片模式或无效行号时静默忽略。"""
        if self._in_image_mode or line_no is None or line_no < 1:
            return
        # 预览模式下先切回源码，行号跳转才可见
        if self._in_md_preview:
            self._set_md_preview(False)
        doc = self.editor.document()
        block = doc.findBlockByLineNumber(line_no - 1)
        if not block.isValid():
            # 行号超出范围（文件可能已变化）→ 退到末行
            block = doc.lastBlock()
        cur = QTextCursor(block)
        self.editor.setTextCursor(cur)
        self.editor.centerCursor()
        self.editor.setFocus()

    def _open_image_file(self, file_path: str) -> bool:
        """把图片读进 QPixmap 并显示在图片预览页"""
        reader = QImageReader(file_path)
        reader.setAutoTransform(True)  # 应用 EXIF 旋转
        image = reader.read()
        if image.isNull():
            QMessageBox.warning(
                self, t("editor.error"),
                t("editor.read_error", error=reader.errorString() or "decode failed")
            )
            return False

        # 当前在文本编辑器里有未保存改动 → 沿用文本路径的提示
        if not self._in_image_mode and self.is_modified():
            reply = QMessageBox.question(
                self, t("editor.save_changes_title"),
                t("editor.save_changes_msg", name=os.path.basename(self._current_file or "")),
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    return False
            elif reply == QMessageBox.StandardButton.Cancel:
                return False
            else:
                # 用户明确放弃旧文件的修改 → 它的崩溃恢复备份也一并清掉
                self._remove_autosave_backup()

        # 图片只读，不参与自动保存
        self._autosave_timer.stop()
        self._last_autosave_hash = None

        # 关闭可能正在显示文本搜索栏
        if hasattr(self, 'search_bar') and not self.search_bar.isHidden():
            self.search_bar.close_search()

        # 清掉文本编辑器内容，避免再次切回文本时还残留上一个图片之前的文本
        if self._highlighter is not None:
            self._highlighter.setDocument(None)
            self._highlighter = None
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)

        self._current_file = file_path
        self._file_encoding = "utf-8"
        self._original_content = ""  # 图片不参与文本 modified 判定
        self._set_md_support(False)
        self._image_pixmap = QPixmap.fromImage(image)
        self._in_image_mode = True
        self._stack.setCurrentIndex(1)
        self._apply_image_to_label()
        self._update_title()
        # 图片模式也跟踪：被外部覆盖时可以重新加载
        self._start_watching(file_path)
        return True

    def _apply_image_to_label(self):
        """把 self._image_pixmap 按视口尺寸等比缩放展示（不放大原图）"""
        if self._image_pixmap is None or self._image_pixmap.isNull():
            self._image_label.clear()
            return
        viewport = self._image_scroll.viewport()
        vw = max(1, viewport.width())
        vh = max(1, viewport.height())
        pw = self._image_pixmap.width()
        ph = self._image_pixmap.height()
        # 不放大；只有图片大于视口时才缩小到视口大小内
        if pw <= vw and ph <= vh:
            scaled = self._image_pixmap
        else:
            scaled = self._image_pixmap.scaled(
                vw, vh,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._image_label.setPixmap(scaled)
        self._image_label.resize(scaled.size())

    def _show_image_context_menu(self, pos):
        """图片预览的右键菜单：复制到剪贴板 / 另存为"""
        if not self._in_image_mode or self._image_pixmap is None or self._image_pixmap.isNull():
            return
        menu = QMenu(self)
        bg_medium = self.theme.get('bg_medium', '#2d2d44')
        text = self.theme.get('text', '#eaeaea')
        accent = self.theme.get('accent', '#667eea')
        border = self.theme.get('border', '#3d3d5c')
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {bg_medium};
                color: {text};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: {accent};
            }}
        """)
        copy_act = menu.addAction(t("editor.copy_image"))
        save_act = menu.addAction(t("editor.save_image_as"))
        menu.addSeparator()
        close_act = menu.addAction(t("editor.close_image"))

        chosen = menu.exec(QCursor.pos())
        if chosen is copy_act:
            self._copy_image_to_clipboard()
        elif chosen is save_act:
            self._save_image_as()
        elif chosen is close_act:
            self._close_editor()

    def _copy_image_to_clipboard(self):
        """把原图（不是缩放后的）放到系统剪贴板"""
        if self._image_pixmap is None or self._image_pixmap.isNull():
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        # setPixmap 在 macOS / Windows / Linux 上都会同时写 image MIME 数据，
        # 这样粘贴到 Slack/微信/编辑器等都能直接得到一张图片。
        clipboard.setPixmap(self._image_pixmap)

    def _save_image_as(self):
        """把原图另存到本地（默认文件名取自当前文件）"""
        if self._image_pixmap is None or self._image_pixmap.isNull():
            return
        default_name = os.path.basename(self._current_file or "image.png")
        path, _ = QFileDialog.getSaveFileName(
            self, t("editor.save_image_as"), default_name,
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All Files (*)"
        )
        if not path:
            return
        # QPixmap.save 根据扩展名自动选格式；失败时弹错误
        if not self._image_pixmap.save(path):
            QMessageBox.warning(
                self, t("editor.error"),
                t("editor.save_image_failed", path=path)
            )

    def _setup_highlighter(self, file_path: str):
        """根据文件类型设置语法高亮"""
        ext = Path(file_path).suffix.lower()
        name = Path(file_path).name.lower()

        # 移除旧的高亮器
        if self._highlighter:
            self._highlighter.setDocument(None)
            self._highlighter = None

        doc = self.editor.document()

        if ext == '.py':
            self._highlighter = PythonHighlighter(doc, self.theme)
        elif ext in ('.md', '.markdown'):
            self._highlighter = MarkdownHighlighter(doc, self.theme)
        elif ext in ('.sh', '.bash', '.zsh', '.ksh', '.fish') or \
                name in ('.bashrc', '.bash_profile', '.zshrc', '.profile', '.zprofile'):
            rules, blocks = _shell_rules()
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)
        elif ext == '.json':
            rules, blocks = _json_rules()
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)
        elif ext in ('.yml', '.yaml'):
            rules, blocks = _yaml_rules()
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)
        elif ext in ('.js', '.jsx', '.mjs', '.cjs'):
            rules, blocks = _js_ts_rules(is_ts=False)
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)
        elif ext in ('.ts', '.tsx'):
            rules, blocks = _js_ts_rules(is_ts=True)
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)
        elif name == 'dockerfile' or name.startswith('dockerfile.') or ext == '.dockerfile':
            rules, blocks = _dockerfile_rules()
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)
        elif name in ('makefile', 'gnumakefile', 'bsdmakefile') or \
                name.startswith('makefile.') or ext in ('.mk', '.make'):
            rules, blocks = _makefile_rules()
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)
        elif ext in ('.toml', '.ini', '.conf', '.cfg', '.properties'):
            rules, blocks = _ini_rules()
            self._highlighter = GenericHighlighter(doc, rules, blocks, self.theme)

    def _atomic_write_bytes(self, path: str, data: bytes):
        """原子写入：写到同目录临时文件 → fsync → os.replace 覆盖目标。

        替代 open(path,'wb').write()——后者会先把原文件 truncate 成 0 再写，
        若写到一半崩溃 / 磁盘满 / 设备掉线，原文件就被截断或写坏。原子方案
        保证：要么完整新内容、要么原文件原封不动，绝不出现半截文件。
        临时文件与目标同目录，确保 os.replace 是同一文件系统内的原子 rename。
        保存后由 _refresh_known_mtime 重新挂监听（replace 会换 inode）。
        """
        d = os.path.dirname(os.path.abspath(path)) or '.'
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # 尽量保留原文件权限位
            try:
                if os.path.exists(path):
                    os.chmod(tmp, os.stat(path).st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save_file(self) -> bool:
        """保存文件"""
        if not self._current_file:
            return self.save_file_as()

        try:
            content = self.editor.toPlainText()

            # 保存前冲突检测：文件自我们上次读取后在磁盘上变新（其它窗格保存过、
            # 或外部程序写过，而 watcher 还没来得及提示），直接覆盖会悄悄丢掉那些
            # 改动 → 先征求用户，默认「否」。这是 watcher 提示之外的最后一道防线。
            if self._known_mtime is not None and os.path.isfile(self._current_file):
                try:
                    disk_mtime = os.path.getmtime(self._current_file)
                except OSError:
                    disk_mtime = None
                if disk_mtime is not None and disk_mtime - self._known_mtime > 0.001:
                    reply = QMessageBox.question(
                        self, t("editor.save_conflict_title"),
                        t("editor.save_conflict_msg",
                          name=os.path.basename(self._current_file)),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return False
                    # 用户确认覆盖 → 推进基线，避免本次写完后又自我误判
                    self._known_mtime = disk_mtime

            # 用打开时检测到的编码写回（utf-8-sig 自动带 BOM）；新建文件默认 utf-8
            encoding = self._file_encoding or 'utf-8'
            try:
                data = content.encode(encoding)
            except (UnicodeEncodeError, LookupError):
                # 新输入的字符旧编码放不下（如往 latin-1 文件里打中文）→ 升级 utf-8
                encoding = 'utf-8'
                data = content.encode(encoding)
                self._file_encoding = encoding
            # 记下我们写下的字节指纹：watcher 据此识别"这是我自己的保存"，而不是
            # 用一个 1.5s 时间窗盲目吞掉这期间的所有事件（那会漏掉真实的外部改动）。
            self._last_saved_hash = hashlib.sha1(data).hexdigest()
            # 原子写：避免写一半崩溃截断原文件（见 _atomic_write_bytes）
            self._atomic_write_bytes(self._current_file, data)
            # 更新原始内容，这样 is_modified() 会返回 False
            self._original_content = content
            # 保存成功 → 崩溃恢复备份已无意义，删掉
            self._remove_autosave_backup()
            self._refresh_known_mtime()
            self._update_title()
            self.file_saved.emit(self._current_file)
            return True
        except Exception as e:
            QMessageBox.warning(self, t("editor.save_failed_title"), t("editor.save_failed_msg", error=e))
            return False

    def save_file_as(self) -> bool:
        """另存为"""
        from PyQt6.QtWidgets import QFileDialog

        file_path, _ = QFileDialog.getSaveFileName(
            self, t("editor.save_as_title"),
            self._current_file or "",
            t("editor.all_files")
        )

        if not file_path:
            return False

        self._current_file = file_path
        return self.save_file()

    def _on_text_changed(self):
        """文本变化时更新标题"""
        self._update_title()
        # 预览可见时缓冲区只可能被程序改动（外部变更重载/恢复备份）→ 同步重渲染
        if self._in_md_preview:
            self._render_md_preview()

    def _update_title(self):
        """更新标题"""
        if self._current_file:
            file_name = os.path.basename(self._current_file)
            enc = (self._file_encoding or 'utf-8').lower()
            if not self._in_image_mode and enc != 'utf-8':
                # 非 utf-8 编码：标题后缀 + tooltip 提示保存时按此编码写回
                self.file_label.setText(f"{file_name}  ·  {enc.upper()}")
                self.file_label.setToolTip(
                    t("editor.encoding_tooltip", encoding=enc.upper())
                )
            else:
                self.file_label.setText(file_name)
                self.file_label.setToolTip("")
            # 通过比较内容判断是否修改
            self.modified_label.setText("●" if self.is_modified() else "")
        else:
            self.file_label.setText(t("editor.no_file"))
            self.file_label.setToolTip("")
            self.modified_label.setText("")

    def is_modified(self) -> bool:
        """检查文件是否已修改（通过比较内容）"""
        if not self._current_file:
            return False
        if self._in_image_mode:
            return False  # 图片只读，不参与 modified 判定
        return self.editor.toPlainText() != self._original_content

    # ---- 自动保存（崩溃恢复）----
    def _backup_identity(self, path: str) -> str:
        """备份键：本地文件用绝对路径；远程文件用 host+远端路径。

        远程 Explorer 把远端文件下载到
        tempdir/smart_terminal_remote_<alias>/<远端路径> 再交给编辑器打开，
        识别这种临时路径并还原成 `remote://<alias>/<远端路径>`，
        跨会话 / tempdir 变化时仍能对应同一个远端文件。
        """
        abspath = os.path.abspath(path)
        real = os.path.realpath(abspath)
        tmp_root = os.path.realpath(tempfile.gettempdir())
        try:
            rel = os.path.relpath(real, tmp_root)
        except ValueError:
            rel = None
        if rel and not rel.startswith('..'):
            parts = rel.split(os.sep)
            if len(parts) >= 2 and parts[0].startswith('smart_terminal_remote_'):
                alias = parts[0][len('smart_terminal_remote_'):]
                return f"remote://{alias}/" + '/'.join(parts[1:])
        return abspath

    def _backup_paths(self, path: str | None = None) -> tuple[str, str] | None:
        """返回 (备份文件, 元数据文件) 路径；无目标文件时返回 None。

        文件名 = sha1(备份键).autosave / .meta.json
        """
        p = path or self._current_file
        if not p:
            return None
        key = hashlib.sha1(self._backup_identity(p).encode('utf-8')).hexdigest()
        base = os.path.join(_AUTOSAVE_DIR, key)
        return base + '.autosave', base + '.meta.json'

    def _autosave_tick(self):
        """30 秒周期：有未保存修改且内容自上次备份后有变化 → 写备份；
        回到未修改状态（撤销 / 外部重载等）→ 顺手清理遗留备份。"""
        if not self._current_file or self._in_image_mode:
            return
        if not self.is_modified():
            if self._last_autosave_hash is not None:
                self._remove_autosave_backup()
            return
        content = self.editor.toPlainText()
        digest = hashlib.sha1(content.encode('utf-8')).hexdigest()
        if digest == self._last_autosave_hash:
            return
        self._write_autosave_backup(content, digest)

    def _write_autosave_backup(self, content: str, digest: str):
        """把 content 写进备份目录（备份本体固定 utf-8；原文件编码记在 meta 里）"""
        paths = self._backup_paths()
        if paths is None:
            return
        backup_path, meta_path = paths
        try:
            # mode 仅在新建目录时生效：备份可能含敏感内容，仅本用户可读
            os.makedirs(_AUTOSAVE_DIR, mode=0o700, exist_ok=True)
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(content)
            meta = {
                'path': os.path.abspath(self._current_file),
                'identity': self._backup_identity(self._current_file),
                'timestamp': time.time(),
                'encoding': self._file_encoding,
            }
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
            self._last_autosave_hash = digest
        except OSError:
            # 写备份失败（磁盘满 / 权限等）不打扰用户，下个周期再试
            pass

    def _remove_autosave_backup(self, path: str | None = None):
        """删除 path（默认当前文件）对应的备份与元数据，幂等。"""
        paths = self._backup_paths(path)
        if paths is None:
            return
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        self._last_autosave_hash = None

    def _maybe_restore_autosave(self, file_path: str, disk_content: str) -> str | None:
        """打开文件时检查崩溃恢复备份。

        返回要载入编辑器的备份内容；无备份 / 与磁盘一致（删备份）/
        用户拒绝（删备份）时返回 None。
        """
        paths = self._backup_paths(file_path)
        if paths is None:
            return None
        backup_path, meta_path = paths
        if not os.path.isfile(backup_path):
            return None
        try:
            with open(backup_path, 'r', encoding='utf-8') as f:
                backup_content = f.read()
        except OSError:
            return None
        if backup_content == disk_content:
            # 上次其实已保存（或外部已同步），备份没有增量信息
            self._remove_autosave_backup(file_path)
            return None
        # 取备份时间给用户参考；meta 缺失/损坏不影响恢复
        ts_text = ""
        backup_ts = None
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            ts = meta.get('timestamp')
            if ts:
                backup_ts = float(ts)
                ts_text = time.strftime(
                    '%Y-%m-%d %H:%M:%S', time.localtime(backup_ts))
        except (OSError, ValueError, TypeError):
            pass

        # 磁盘文件是否比备份更新：若是，恢复旧备份会覆盖较新的磁盘内容（数据丢失）。
        # 这种情况下默认选「否」并改用警示文案，避免顺手回车把新内容盖掉。
        disk_newer = False
        disk_ts_text = ""
        try:
            disk_mtime = os.path.getmtime(file_path)
            if backup_ts is not None and disk_mtime - backup_ts > 1.0:
                disk_newer = True
                disk_ts_text = time.strftime(
                    '%Y-%m-%d %H:%M:%S', time.localtime(disk_mtime))
        except OSError:
            pass

        if disk_newer:
            msg = t("editor.autosave_restore_msg_stale",
                    name=os.path.basename(file_path),
                    time=ts_text or "?", disk=disk_ts_text or "?")
            default_btn = QMessageBox.StandardButton.No
        else:
            msg = t("editor.autosave_restore_msg",
                    name=os.path.basename(file_path), time=ts_text or "?")
            default_btn = QMessageBox.StandardButton.Yes
        reply = QMessageBox.question(
            self,
            t("editor.autosave_restore_title"),
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default_btn,
        )
        if reply == QMessageBox.StandardButton.Yes:
            return backup_content
        self._remove_autosave_backup(file_path)
        return None

    # ---- 外部文件变更检测 ----
    def _start_watching(self, path: str | None):
        """开始监听 path；先移除之前监听的所有文件"""
        self._stop_watching()
        if not path:
            return
        try:
            if os.path.isfile(path):
                self._file_watcher.addPath(path)
                self._known_mtime = os.path.getmtime(path)
        except OSError:
            self._known_mtime = None

    def _stop_watching(self):
        watched = self._file_watcher.files()
        if watched:
            self._file_watcher.removePaths(watched)
        self._known_mtime = None

    def _refresh_known_mtime(self):
        """自己保存后调用：刷新已知 mtime 并把路径重新加入监听
        （部分平台在文件被替换/重命名后会丢失监听）"""
        if not self._current_file:
            return
        try:
            self._known_mtime = os.path.getmtime(self._current_file)
        except OSError:
            self._known_mtime = None
        if self._current_file not in self._file_watcher.files() and os.path.isfile(
            self._current_file
        ):
            self._file_watcher.addPath(self._current_file)

    def _on_watched_file_changed(self, path: str):
        # 不再用时间窗盲目屏蔽（那会漏掉这期间真实的外部改动）。我们自己的保存
        # 由 _handle_external_change 里的字节指纹比对识别，见 _last_saved_hash。
        if path != self._current_file:
            return
        # 去抖：rename-保存模式可能短时间内多次触发，统一延后处理
        if self._external_change_timer is None:
            self._external_change_timer = QTimer(self)
            self._external_change_timer.setSingleShot(True)
            self._external_change_timer.timeout.connect(self._handle_external_change)
        self._external_change_timer.start(150)

    def _handle_external_change(self):
        path = self._current_file
        if not path or self._in_image_mode:
            return

        # 文件被删除：保留缓冲区，下次 save 会重建；不再重复弹
        if not os.path.isfile(path):
            QMessageBox.warning(
                self,
                t("editor.file_changed_title"),
                t("editor.file_deleted_msg", name=os.path.basename(path)),
            )
            self._known_mtime = None
            return

        try:
            new_mtime = os.path.getmtime(path)
        except OSError:
            return

        # mtime 未变（仅触摸/属性变更）：仅重新挂监听
        if self._known_mtime is not None and new_mtime == self._known_mtime:
            if path not in self._file_watcher.files():
                self._file_watcher.addPath(path)
            return

        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except Exception as e:
            QMessageBox.warning(self, t("editor.error"), t("editor.read_error", error=e))
            return

        # 磁盘上正是我们自己刚写下的字节 → 是自己的保存（哪怕保存后又继续打字，
        # 使缓冲区与磁盘已不同），不是外部改动。静默同步 mtime/监听即可，不弹窗、
        # 不重载。这取代了旧的 1.5s 时间窗，且不会漏掉真实的外部改动。
        if self._last_saved_hash and hashlib.sha1(raw).hexdigest() == self._last_saved_hash:
            self._known_mtime = new_mtime
            if path not in self._file_watcher.files():
                self._file_watcher.addPath(path)
            return
        if _looks_binary(raw):
            QMessageBox.warning(self, t("editor.binary_title"), t("editor.binary_msg"))
            self._known_mtime = new_mtime
            return
        new_content, new_encoding = _decode_with_fallback(raw)

        # 内容相同（不同进程写入了相同字节）：仅同步 mtime
        if new_content == self.editor.toPlainText():
            self._known_mtime = new_mtime
            if path not in self._file_watcher.files():
                self._file_watcher.addPath(path)
            return

        if self.is_modified():
            reply = QMessageBox.question(
                self,
                t("editor.file_changed_title"),
                t("editor.file_changed_with_local_msg", name=os.path.basename(path)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                # 保留本地，吞掉本轮外部变更，不再反复弹（用户后续保存会覆盖外部）
                self._known_mtime = new_mtime
                if path not in self._file_watcher.files():
                    self._file_watcher.addPath(path)
                return

        # 干净状态或用户同意丢弃：静默重载，并尽量保留光标位置
        cursor_pos = self.editor.textCursor().position()
        self.editor.setPlainText(new_content)
        self._original_content = new_content
        self._file_encoding = new_encoding
        self._known_mtime = new_mtime
        # 重载后已与磁盘一致，本地修改的崩溃恢复备份不再有意义
        self._remove_autosave_backup()
        try:
            cur = self.editor.textCursor()
            cur.setPosition(min(cursor_pos, len(new_content)))
            self.editor.setTextCursor(cur)
        except Exception:
            logger.debug("_handle_external_change: suppressed exception", exc_info=True)
        self._update_title()
        if path not in self._file_watcher.files():
            self._file_watcher.addPath(path)

    def _prompt_save_before_quit(self) -> bool:
        """退出应用前对本窗格的未保存改动弹确认。

        返回 False = 用户点了取消（应中止关闭）；True = 已保存 / 已丢弃 / 无改动，
        可继续退出。与 _close_editor 不同：不做控件级清理（应用即将退出，多余）。
        保存失败也返回 False，宁可中止退出也不丢数据。
        """
        if not self.is_modified():
            return True
        reply = QMessageBox.question(
            self, t("editor.save_changes_title"),
            t("editor.save_changes_msg", name=os.path.basename(self._current_file or "")),
            QMessageBox.StandardButton.Save |
            QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel
        )
        if reply == QMessageBox.StandardButton.Save:
            return self.save_file()
        if reply == QMessageBox.StandardButton.Cancel:
            return False
        return True  # Discard

    def _close_editor(self):
        """关闭编辑器"""
        # 如果文件已修改，提示保存
        if self.is_modified():
            reply = QMessageBox.question(
                self, t("editor.save_changes_title"),
                t("editor.save_changes_msg", name=os.path.basename(self._current_file)),
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self.save_file():
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                return

        # 关闭前记住视图位置，之后重新打开该文件仍能回到原位
        self._save_view_state()

        # 正常关闭（已保存 / 无修改 / 明确丢弃）：停自动保存定时器并清理备份。
        # 删除是幂等的；Save 分支在 save_file 里已删，这里再调无副作用。
        self._autosave_timer.stop()
        self._remove_autosave_backup()
        self._last_autosave_hash = None

        self._stop_watching()
        self._current_file = None
        self._file_encoding = "utf-8"
        self._original_content = ""
        self.editor.clear()
        # 复位图片预览与 Markdown 预览
        self._in_image_mode = False
        self._image_pixmap = None
        self._image_label.clear()
        self._set_md_support(False)
        self._stack.setCurrentIndex(0)
        self._update_title()
        self.editor_closed.emit()

    def get_current_file(self) -> str:
        """获取当前打开的文件路径"""
        return self._current_file

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._in_image_mode and self._image_pixmap is not None:
            self._apply_image_to_label()


class EditorArea(QWidget):
    """编辑器组容器：用嵌套 QSplitter 容纳多个 FileEditorWidget 窗格，
    支持无限层级的左右 / 上下分屏（仿 VS Code 编辑器组）。

    - 每个窗格是一个独立的 FileEditorWidget，拥有各自的文件、修改状态、查找栏。
    - 点击某个窗格使其成为「活动窗格」；文件树打开文件时落到活动窗格。
    - 关闭某个窗格会回收空间并塌缩只剩一个子节点的嵌套 splitter；
      关闭最后一个窗格则发 all_closed（由主窗口隐藏整个编辑器区）。

    对外它就是一个普通 widget，可被放进任意 splitter；放置 / 显隐 / indexOf
    等"容器级"操作针对 EditorArea 本身，open/save/字体等"窗格级"操作针对活动窗格。
    """

    all_closed = pyqtSignal()       # 最后一个窗格被关闭
    active_changed = pyqtSignal()   # 活动窗格发生变化
    file_saved = pyqtSignal(str)    # 任一窗格保存文件（转发）
    ai_completion_toggled = pyqtSignal(bool)  # 任一窗格切换 AI 补全（转发给主窗口）
    word_wrap_toggled = pyqtSignal(bool)  # 右键菜单切换自动换行（转发给主窗口落盘）

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._panes: list[FileEditorWidget] = []
        self._active: FileEditorWidget | None = None
        # 统一字号（与终端联动）；新建窗格——含初始/分屏/重开——都继承它
        self._editor_point_size: int = 12
        # AI 补全开关（全局一致）；新建窗格继承它
        self._ai_completion_enabled: bool = False
        # 自动换行开关（全局一致）；新建窗格继承它
        self._word_wrap_enabled: bool = False

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)

        # 初始单窗格直接由布局持有；分屏后 _root 变成顶层 QSplitter
        first = self._create_pane()
        self._root = first
        self._layout.addWidget(first)
        self._set_active(first)

    # ---------- 窗格创建 / 活动态 ----------

    def _create_pane(self) -> FileEditorWidget:
        pane = FileEditorWidget(theme=self.theme)
        pane.split_h_requested.connect(lambda p=pane: self.split_pane(p, Qt.Orientation.Horizontal))
        pane.split_v_requested.connect(lambda p=pane: self.split_pane(p, Qt.Orientation.Vertical))
        pane.move_left_requested.connect(lambda p=pane: self.move_pane_left(p))
        pane.move_up_requested.connect(lambda p=pane: self.move_pane_up(p))
        pane.editor_closed.connect(lambda p=pane: self._on_pane_close_requested(p))
        pane.pane_focused.connect(lambda p=pane: self._set_active(p))
        pane.file_saved.connect(self.file_saved)
        pane.ai_completion_toggled.connect(self.ai_completion_toggled)
        self._panes.append(pane)
        # 新窗格继承当前统一字号，保证与终端字号联动
        self._apply_point_size_to_pane(pane, self._editor_point_size)
        # 新窗格继承当前 AI 补全开关
        pane.set_ai_completion_enabled(self._ai_completion_enabled)
        # 新窗格继承当前自动换行开关
        pane.set_word_wrap(self._word_wrap_enabled)
        return pane

    def set_ai_completion_enabled(self, enabled: bool):
        """统一设置所有窗格的 AI 补全开关，并记住它供新窗格继承。"""
        self._ai_completion_enabled = bool(enabled)
        for p in self._panes:
            p.set_ai_completion_enabled(self._ai_completion_enabled)

    def set_word_wrap_enabled(self, enabled: bool):
        """统一设置所有窗格的自动换行，并记住它供新窗格继承。"""
        self._word_wrap_enabled = bool(enabled)
        for p in self._panes:
            p.set_word_wrap(self._word_wrap_enabled)

    def is_word_wrap_enabled(self) -> bool:
        """当前自动换行状态（供右键菜单显示勾选态）。"""
        return self._word_wrap_enabled

    def _apply_point_size_to_pane(self, pane: 'FileEditorWidget', point_size: int):
        pane.set_editor_point_size(point_size)

    def set_editor_font_size(self, point_size: int):
        """统一设置所有窗格字号并记住它，使后续新建窗格继承同一字号——
        这是「编辑器与终端字号联动」的单一入口，由主窗口的全局缩放驱动。"""
        self._editor_point_size = point_size
        for p in self._panes:
            self._apply_point_size_to_pane(p, point_size)

    def _set_active(self, pane: FileEditorWidget):
        if pane is self._active or pane not in self._panes:
            return
        self._active = pane
        self._refresh_active_indicators()
        self.active_changed.emit()

    def _refresh_active_indicators(self):
        multi = len(self._panes) > 1
        for p in self._panes:
            p.set_active(p is self._active and multi, show_indicator=multi)

    @property
    def active_pane(self) -> FileEditorWidget | None:
        return self._active

    def close_focused_pane(self) -> bool:
        """关闭当前活动（聚焦）的编辑窗格，行为与点该窗格的 × 按钮完全一致：
        会走未保存提示，最后一个窗格则交主窗口隐藏整个编辑区。

        供 Cmd+W 在焦点落在编辑器里时调用。成功发起关闭返回 True。
        """
        pane = self._active
        if pane is None or pane not in self._panes:
            return False
        pane._close_editor()
        return True

    @property
    def panes(self) -> list:
        return list(self._panes)

    # ---------- 退出前的未保存防护 ----------

    def has_unsaved(self) -> bool:
        """任一窗格有未保存改动。"""
        return any(p.is_modified() for p in self._panes)

    def prompt_save_all(self) -> bool:
        """退出前逐个提示保存有改动的窗格（供 MainWindow.closeEvent 调用）。

        返回 False 表示用户在某个窗格点了取消 → 调用方应中止关闭。
        """
        for pane in list(self._panes):
            try:
                if not pane.is_modified():
                    continue
                self._set_active(pane)  # 让用户看清弹的是哪个文件
                if not pane._prompt_save_before_quit():
                    return False
            except Exception:
                # 单个窗格异常不阻断其它窗格的保存提示
                logger.debug("prompt_save_all: suppressed exception", exc_info=True)
        return True

    def flush_autosave_all(self):
        """强制退出路径（Ctrl+C / 强制关闭）：不弹窗，但同步刷每个有改动窗格的
        崩溃恢复备份，保证下次打开可恢复、不丢数据。"""
        for pane in list(self._panes):
            try:
                pane._autosave_tick()
            except Exception:
                # 崩溃恢复的最后防线：一个窗格失败不能拖累其它窗格备份，
                # 但绝不能静默——否则用户以为已备份、实则丢数据无从排查
                logger.exception("crash-recovery autosave flush failed for a pane")

    def _styled_splitter(self, orientation) -> QSplitter:
        splitter = QSplitter(orientation)
        splitter.setHandleWidth(2)
        accent = self.theme.get('accent', '#667eea')
        border = self.theme.get('border', '#3d3d5c')
        splitter.setStyleSheet(f"""
            QSplitter::handle {{ background-color: {border}; }}
            QSplitter::handle:hover {{ background-color: {accent}; }}
        """)
        return splitter

    # ---------- 分屏 ----------

    def split_pane(self, pane: FileEditorWidget, orientation):
        """把 pane 一分为二，新窗格复制 pane 当前打开的文件。"""
        if pane not in self._panes:
            return
        new_pane = self._create_pane()
        # _create_pane 已让新窗格继承当前统一字号（含 font-size 样式表），
        # 与终端及其它窗格保持联动，无需再单独复制字号。
        if pane.get_current_file():
            new_pane.open_file(pane.get_current_file())

        parent = pane.parent()
        if isinstance(parent, QSplitter):
            parent_splitter = parent
            index = parent_splitter.indexOf(pane)
            if parent_splitter.orientation() == orientation:
                # 同方向：直接在 pane 后插入，把它占的空间一分为二
                sizes = parent_splitter.sizes()
                parent_splitter.insertWidget(index + 1, new_pane)
                if index < len(sizes):
                    orig = sizes[index]
                    new_sizes = list(sizes)
                    new_sizes[index] = orig // 2
                    new_sizes.insert(index + 1, orig - orig // 2)
                    if len(new_sizes) == parent_splitter.count():
                        parent_splitter.setSizes(new_sizes)
            else:
                # 异方向：把 pane 包进一个新方向的 splitter
                sizes = parent_splitter.sizes()
                new_splitter = self._styled_splitter(orientation)
                new_splitter.addWidget(pane)       # 把 pane 从父 splitter 移入新 splitter
                new_splitter.addWidget(new_pane)
                parent_splitter.insertWidget(index, new_splitter)
                if sizes and len(sizes) == parent_splitter.count():
                    parent_splitter.setSizes(sizes)
                self._split_evenly(new_splitter, orientation)
        else:
            # pane 是根（单窗格，直接被布局持有）
            self._layout.removeWidget(pane)
            new_splitter = self._styled_splitter(orientation)
            new_splitter.addWidget(pane)
            new_splitter.addWidget(new_pane)
            self._root = new_splitter
            self._layout.addWidget(new_splitter)
            self._split_evenly(new_splitter, orientation)

        pane.show()
        new_pane.show()
        self._refresh_active_indicators()
        self._set_active(new_pane)
        new_pane.editor.setFocus()

    def _split_evenly(self, splitter: QSplitter, orientation):
        if orientation == Qt.Orientation.Horizontal:
            extent = splitter.width() if splitter.width() > 0 else max(self.width(), 600)
        else:
            extent = splitter.height() if splitter.height() > 0 else max(self.height(), 400)
        splitter.setSizes([extent // 2, extent - extent // 2])

    # ---------- 关闭 / 塌缩 ----------

    def _on_pane_close_requested(self, pane: FileEditorWidget):
        """某窗格点了关闭按钮（此时它已自行清空文件并发 editor_closed）。"""
        if len(self._panes) <= 1:
            # 最后一个窗格：不销毁，交给主窗口隐藏整个编辑器区
            self.all_closed.emit()
            return
        self.close_pane(pane)

    def close_pane(self, pane: FileEditorWidget):
        if pane not in self._panes or len(self._panes) <= 1:
            return
        # 窗格销毁前记住其文件的视图位置（经 _close_editor 来的路径里文件已清空，此调用为无害空操作）
        pane._save_view_state()
        parent = pane.parent()
        parent_sizes = parent.sizes() if isinstance(parent, QSplitter) else None
        close_index = parent.indexOf(pane) if isinstance(parent, QSplitter) else -1

        was_active = pane is self._active
        self._panes.remove(pane)
        pane.setParent(None)
        pane.deleteLater()

        # 在局部父 splitter 内把空出的空间并给相邻窗格（其它窗格尺寸不变）
        if isinstance(parent, QSplitter) and parent_sizes and 0 <= close_index < len(parent_sizes):
            freed = parent_sizes[close_index]
            new_sizes = parent_sizes[:close_index] + parent_sizes[close_index + 1:]
            if new_sizes:
                give = close_index - 1 if close_index - 1 >= 0 else 0
                new_sizes[give] += freed
                if len(new_sizes) == parent.count():
                    parent.setSizes(new_sizes)

        self._collapse_singleton(parent)

        if was_active and self._panes:
            self._active = None
            self._set_active(self._panes[0])
            self._panes[0].editor.setFocus()
        self._refresh_active_indicators()

    def _collapse_singleton(self, splitter):
        """某 splitter 关闭后只剩一个子组件时，解除这层嵌套，让剩余窗格自动扩展。"""
        while isinstance(splitter, QSplitter) and splitter.count() == 1:
            if splitter is self._root:
                # 根 splitter 只剩一个子组件 → 子组件成为新根，直接由布局持有
                child = splitter.widget(0)
                self._layout.removeWidget(splitter)
                child.setParent(None)
                self._root = child
                self._layout.addWidget(child)
                child.show()
                splitter.deleteLater()
                break
            parent = splitter.parent()
            if not isinstance(parent, QSplitter):
                break
            child = splitter.widget(0)
            gp_index = parent.indexOf(splitter)
            gp_sizes = parent.sizes()
            child.setParent(None)
            parent.insertWidget(gp_index, child)
            splitter.setParent(None)
            splitter.deleteLater()
            if len(gp_sizes) == parent.count():
                parent.setSizes(gp_sizes)
            splitter = parent

    # ---------- 移动窗格（与终端 Move Left / Move Up 行为一致）----------

    def _root_child_of(self, pane: FileEditorWidget):
        """返回顶层 splitter 中包含 pane 的那个直接子组件（pane 本身或其所在的嵌套
        splitter）。顶层不是 splitter（单窗格）时返回 None。"""
        if not isinstance(self._root, QSplitter):
            return None
        w = pane
        while w is not None and w.parent() is not self._root:
            w = w.parent()
        return w

    def can_move_left(self, pane: FileEditorWidget) -> bool:
        """是否可在顶层 splitter 中把 pane 所在的列向左移（左边还有兄弟）。"""
        w = self._root_child_of(pane)
        return w is not None and self._root.indexOf(w) > 0

    def can_move_up(self, pane: FileEditorWidget) -> bool:
        """是否可在 pane 所在的垂直 splitter 中把它上移（上方还有兄弟）。"""
        parent = pane.parent()
        return (
            isinstance(parent, QSplitter)
            and parent.orientation() == Qt.Orientation.Vertical
            and parent.indexOf(pane) > 0
        )

    def move_pane_left(self, pane: FileEditorWidget):
        """在顶层 splitter 内，把 pane 所在的列与左边一列交换位置。"""
        w = self._root_child_of(pane)
        if w is None:
            return
        idx = self._root.indexOf(w)
        if idx <= 0:
            return
        sizes = self._root.sizes()
        self._root.insertWidget(idx - 1, w)
        if len(sizes) == self._root.count():
            sizes[idx], sizes[idx - 1] = sizes[idx - 1], sizes[idx]
            self._root.setSizes(sizes)
        self._set_active(pane)
        pane.editor.setFocus()

    def move_pane_up(self, pane: FileEditorWidget):
        """在 pane 所在的垂直 splitter 内，把它与上方的兄弟交换位置。"""
        parent = pane.parent()
        if not isinstance(parent, QSplitter) or parent.orientation() != Qt.Orientation.Vertical:
            return
        idx = parent.indexOf(pane)
        if idx <= 0:
            return
        sizes = parent.sizes()
        parent.insertWidget(idx - 1, pane)
        if len(sizes) == parent.count():
            sizes[idx], sizes[idx - 1] = sizes[idx - 1], sizes[idx]
            parent.setSizes(sizes)
        self._set_active(pane)
        pane.editor.setFocus()

    # ---------- 窗格级便捷转发 ----------

    def open_file_in_active(self, file_path: str) -> bool:
        if self._active is None:
            return False
        return self._active.open_file(file_path)

    def goto_line_in_active(self, line_no: int):
        if self._active is not None:
            self._active.goto_line(line_no)

    def save_active(self) -> bool:
        return bool(self._active and self._active.save_file())

    def save_active_as(self) -> bool:
        return bool(self._active and self._active.save_file_as())

    # ---------- 整组级转发 ----------

    def apply_theme(self, theme: dict):
        self.theme = theme
        for p in self._panes:
            p.apply_theme(theme)
        # 重新着色所有嵌套 splitter 手柄
        for sp in self.findChildren(QSplitter):
            accent = theme.get('accent', '#667eea')
            border = theme.get('border', '#3d3d5c')
            sp.setStyleSheet(f"""
                QSplitter::handle {{ background-color: {border}; }}
                QSplitter::handle:hover {{ background-color: {accent}; }}
            """)

    def apply_language(self):
        for p in self._panes:
            p.apply_language()

    def apply_font(self, font: QFont):
        for p in self._panes:
            p.editor.setFont(font)
            p.editor.setTabStopDistance(
                4 * p.editor.fontMetrics().horizontalAdvance(' ')
            )
