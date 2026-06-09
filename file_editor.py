"""
内置文件编辑器组件
提供在程序内部编辑文件的功能
"""
import os
import re
import time
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QFrame, QMessageBox,
    QSplitter, QLineEdit, QTextEdit, QStackedWidget, QScrollArea,
    QSizePolicy, QMenu, QFileDialog, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QRect, QSize, QFileSystemWatcher, QTimer, QEvent
from PyQt6.QtGui import (
    QFont, QFontMetrics, QColor, QTextCharFormat, QSyntaxHighlighter,
    QKeySequence, QPalette, QShortcut, QPainter, QTextCursor,
    QPixmap, QImageReader, QCursor, QGuiApplication, QIcon, QWheelEvent,
)


# 支持在编辑器面板里内联预览的图片扩展名
_IMAGE_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff',
    '.ico', '.svg', '.heic', '.heif',
}

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

        # AI 行内补全（灰字建议）。默认关闭，由编辑器标题栏的 🤖 开关启用。
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

    def set_ai_ghost_color(self, color):
        if self._ai is not None:
            self._ai.set_color(color)

    def set_ai_ghost_bg(self, color):
        if self._ai is not None:
            self._ai.set_bg_color(color)

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

        menu.exec(event.globalPos())

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
        self.hide()
        self.editor.set_search_selections([])
        self.editor.setFocus()

    def _on_text_changed(self, _text):
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
        query = self.input.text()
        text = self.editor.toPlainText()
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

    def _refresh_highlights(self):
        selections = []
        all_match_color = QColor(self.theme.get('search_match_bg', '#5a4a1a'))
        current_match_color = QColor(self.theme.get('search_current_bg', '#c89020'))
        text_color = QColor(self.theme.get('text', '#eaeaea'))

        for i, (start, end) in enumerate(self._matches):
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(current_match_color if i == self._current else all_match_color)
            fmt.setForeground(text_color)
            sel.format = fmt
            cur = QTextCursor(self.editor.document())
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
        if not self._matches:
            return
        self._current = (self._current + 1) % len(self._matches)
        self._refresh_highlights()
        self._scroll_to_current()
        self._update_count()

    def _goto_prev(self):
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
        if not self._matches or self._current < 0:
            self._update_matches(reset_current=True)
            return
        start, end = self._matches[self._current]
        doc = self.editor.document()
        match_text = self.editor.toPlainText()[start:end]
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
        if not self._matches:
            return
        doc = self.editor.document()
        full_text = self.editor.toPlainText()
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
        self._suppress_external_until: float = 0.0
        # 用于去抖：rename 类保存会先收到 deleted 再 created
        self._external_change_timer: QTimer | None = None

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

        # AI 行内补全开关（灰字建议，Tab 接受 / Esc 取消）
        self.ai_btn = QPushButton("🤖")
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

        # 设置等宽字体；默认字号与终端一致（12pt），保证两者字号联动
        font = QFont("Menlo", 12)
        font.setStyleHint(QFont.StyleHint.Monospace)
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

        # Cmd+/ / Ctrl+/ 切换注释（仅在编辑器获焦时触发）
        comment_shortcut = QShortcut(QKeySequence("Ctrl+/"), self.editor)
        comment_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        comment_shortcut.activated.connect(self._toggle_comment)

        # Cmd+\ 左右分屏；Cmd+Shift+\ 上下分屏（与 VS Code 一致，作用于本窗格）
        split_h_sc = QShortcut(QKeySequence("Ctrl+\\"), self)
        split_h_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        split_h_sc.activated.connect(self.split_h_requested.emit)

        split_v_sc = QShortcut(QKeySequence("Ctrl+Shift+\\"), self)
        split_v_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        split_v_sc.activated.connect(self.split_v_requested.emit)

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
            self.search_bar.open_search()

    def _open_search_with_replace(self):
        if hasattr(self, 'search_bar'):
            self.search_bar.open_search(with_replace=True)

    def _find_next_or_open(self):
        if hasattr(self, 'search_bar'):
            if self.search_bar.isHidden() or not self.search_bar.input.text():
                self.search_bar.open_search()
            else:
                self.search_bar._goto_next()

    def _find_prev_or_open(self):
        if hasattr(self, 'search_bar'):
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
        self.ai_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {text_dim};
                font-size: 14px;
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
        """)

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

        # AI 灰字建议用暗色文字 + 编辑器背景作底色（盖住下方真实文字），跟随主题
        self.editor.set_ai_ghost_color(QColor(self.theme.get('text_dim', '#6a737d')))
        self.editor.set_ai_ghost_bg(QColor(editor_bg))

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

    def eventFilter(self, obj, event):
        """点击 / 聚焦本窗格的编辑区时，通知 EditorArea 把本窗格设为活动窗格。"""
        if event.type() in (
            QEvent.Type.FocusIn,
            QEvent.Type.MouseButtonPress,
        ):
            self.pane_focused.emit()
        return super().eventFilter(obj, event)

    def set_active(self, active: bool, show_indicator: bool = True):
        """设置本窗格是否为活动窗格（仅多窗格分屏时显示高亮指示）。"""
        if active == self._is_active and show_indicator == self._show_active_indicator:
            return
        self._is_active = active
        self._show_active_indicator = show_indicator
        self._apply_theme()

    def open_file(self, file_path: str) -> bool:
        """打开文件"""
        if not os.path.isfile(file_path):
            QMessageBox.warning(self, t("editor.error"), t("editor.file_not_found", path=file_path))
            return False

        # 图片：走内联预览（QPixmap），跳过 "5MB 文本限制 / UTF-8 解码" 那条路径
        if Path(file_path).suffix.lower() in _IMAGE_EXTENSIONS:
            return self._open_image_file(file_path)

        # 检查文件大小，太大的文件不打开
        file_size = os.path.getsize(file_path)
        if file_size > 5 * 1024 * 1024:  # 5MB 限制
            QMessageBox.warning(self, t("editor.file_too_large_title"),
                              t("editor.file_too_large_msg", size=f"{file_size / 1024 / 1024:.1f}"))
            return False

        # 检查是否是二进制文件
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            QMessageBox.warning(self, t("editor.binary_title"),
                              t("editor.binary_msg"))
            return False
        except Exception as e:
            QMessageBox.warning(self, t("editor.error"), t("editor.read_error", error=e))
            return False

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

        # 切到文本编辑器视图（可能此前在显示图片）
        self._in_image_mode = False
        self._image_pixmap = None
        self._stack.setCurrentIndex(0)

        # 加载文件内容
        self._current_file = file_path
        self._original_content = content  # 保存原始内容用于比较
        self.editor.setPlainText(content)

        # 切换文件时关闭已打开的查找栏（旧文件的高亮已无意义）
        if hasattr(self, 'search_bar') and not self.search_bar.isHidden():
            self.search_bar.close_search()

        # 根据文件类型设置语法高亮
        self._setup_highlighter(file_path)

        # 更新标题
        self._update_title()

        # 开始监听外部变更
        self._start_watching(file_path)

        return True

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
        self._original_content = ""  # 图片不参与文本 modified 判定
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

    def save_file(self) -> bool:
        """保存文件"""
        if not self._current_file:
            return self.save_file_as()

        try:
            content = self.editor.toPlainText()
            # 屏蔽 watcher：我们自己写文件不应弹"外部已改动"
            self._suppress_external_until = time.time() + 1.5
            with open(self._current_file, 'w', encoding='utf-8') as f:
                f.write(content)
            # 更新原始内容，这样 is_modified() 会返回 False
            self._original_content = content
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

    def _update_title(self):
        """更新标题"""
        if self._current_file:
            file_name = os.path.basename(self._current_file)
            self.file_label.setText(file_name)
            # 通过比较内容判断是否修改
            self.modified_label.setText("●" if self.is_modified() else "")
        else:
            self.file_label.setText(t("editor.no_file"))
            self.modified_label.setText("")

    def is_modified(self) -> bool:
        """检查文件是否已修改（通过比较内容）"""
        if not self._current_file:
            return False
        if self._in_image_mode:
            return False  # 图片只读，不参与 modified 判定
        return self.editor.toPlainText() != self._original_content

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
        # 自己刚保存时屏蔽，避免我们自己写入触发 reload 提示
        if time.time() < self._suppress_external_until:
            self._refresh_known_mtime()
            return
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
            with open(path, 'r', encoding='utf-8') as f:
                new_content = f.read()
        except UnicodeDecodeError:
            QMessageBox.warning(self, t("editor.binary_title"), t("editor.binary_msg"))
            self._known_mtime = new_mtime
            return
        except Exception as e:
            QMessageBox.warning(self, t("editor.error"), t("editor.read_error", error=e))
            return

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
        self._known_mtime = new_mtime
        try:
            cur = self.editor.textCursor()
            cur.setPosition(min(cursor_pos, len(new_content)))
            self.editor.setTextCursor(cur)
        except Exception:
            pass
        self._update_title()
        if path not in self._file_watcher.files():
            self._file_watcher.addPath(path)

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

        self._stop_watching()
        self._current_file = None
        self._original_content = ""
        self.editor.clear()
        # 复位图片预览
        self._in_image_mode = False
        self._image_pixmap = None
        self._image_label.clear()
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

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._panes: list[FileEditorWidget] = []
        self._active: FileEditorWidget | None = None
        # 统一字号（与终端联动）；新建窗格——含初始/分屏/重开——都继承它
        self._editor_point_size: int = 12
        # AI 补全开关（全局一致）；新建窗格继承它
        self._ai_completion_enabled: bool = False

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
        return pane

    def set_ai_completion_enabled(self, enabled: bool):
        """统一设置所有窗格的 AI 补全开关，并记住它供新窗格继承。"""
        self._ai_completion_enabled = bool(enabled)
        for p in self._panes:
            p.set_ai_completion_enabled(self._ai_completion_enabled)

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
