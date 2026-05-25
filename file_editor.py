"""
内置文件编辑器组件
提供在程序内部编辑文件的功能
"""
import os
import re
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QFrame, QMessageBox,
    QSplitter, QLineEdit, QTextEdit, QStackedWidget, QScrollArea,
    QSizePolicy, QMenu, QFileDialog, QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QRect, QSize
from PyQt6.QtGui import (
    QFont, QColor, QTextCharFormat, QSyntaxHighlighter,
    QKeySequence, QPalette, QShortcut, QPainter, QTextCursor,
    QPixmap, QImageReader, QCursor, QGuiApplication,
)


# 支持在编辑器面板里内联预览的图片扩展名
_IMAGE_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff',
    '.ico', '.svg', '.heic', '.heif',
}
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
            if cursor.hasSelection() and self._selection_spans_multiple_lines(cursor):
                self._indent_selection()
            else:
                # 单行：用 4 空格替换选区/插入 4 空格
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
        if obj in (self.input, self.replace_input) and event.type() == event.Type.KeyPress:
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

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._current_file = None
        self._original_content = ""  # 保存原始内容用于比较
        self._highlighter = None
        self._in_image_mode = False  # 当前显示的是否是图片预览
        self._image_pixmap: QPixmap | None = None  # 原始未缩放的 QPixmap

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
        header_layout.addWidget(self.file_label)

        # 修改状态指示
        self.modified_label = QLabel("")
        self.modified_label.setStyleSheet("color: #f59e0b;")
        header_layout.addWidget(self.modified_label)

        header_layout.addStretch()

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

        # 设置等宽字体
        font = QFont("Menlo", 13)
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
        save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        save_shortcut.activated.connect(self.save_file)

        # Cmd+/ / Ctrl+/ 切换注释（仅在编辑器获焦时触发）
        comment_shortcut = QShortcut(QKeySequence("Ctrl+/"), self.editor)
        comment_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        comment_shortcut.activated.connect(self._toggle_comment)

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
        if ext in ('.rb', '.r', '.pl', '.pm', '.tcl', '.awk', '.gitignore', '.dockerignore'):
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

    def _zoom_in(self):
        """放大字体"""
        font = self.editor.font()
        size = font.pointSize()
        if size < 48:
            font.setPointSize(size + 1)
            self.editor.setFont(font)
            self.editor.setTabStopDistance(4 * self.editor.fontMetrics().horizontalAdvance(' '))

    def _zoom_out(self):
        """缩小字体"""
        font = self.editor.font()
        size = font.pointSize()
        if size > 6:
            font.setPointSize(size - 1)
            self.editor.setFont(font)
            self.editor.setTabStopDistance(4 * self.editor.fontMetrics().horizontalAdvance(' '))

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

        self.header.setStyleSheet(f"""
            QFrame {{
                background-color: {bg_medium};
                border-bottom: 1px solid {border};
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

        self.editor.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {editor_bg};
                color: {editor_fg};
                border: none;
                padding: 4px 12px 4px 8px;
                selection-background-color: {accent};
                selection-color: white;
            }}
        """)

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
        if not self._current_file:
            self.file_label.setText(t("editor.no_file"))
        if hasattr(self, 'search_bar'):
            self.search_bar.apply_language()

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

        chosen = menu.exec(QCursor.pos())
        if chosen is copy_act:
            self._copy_image_to_clipboard()
        elif chosen is save_act:
            self._save_image_as()

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
            with open(self._current_file, 'w', encoding='utf-8') as f:
                f.write(content)
            # 更新原始内容，这样 is_modified() 会返回 False
            self._original_content = content
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
