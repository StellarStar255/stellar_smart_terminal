"""Syntax highlighting rules shared by source editing and Markdown preview.

This module depends on Qt text documents, not editor panes or file persistence.
"""
import re

from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextFormat

# OneDark 调色板（与 MarkdownHighlighter 一致）
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
    """Python 规则表：编辑器里的 .py 与 markdown 预览代码块共用。

    以前编辑器用一个逐关键字 35 次正则、且没有跨行状态的 PythonHighlighter：
    大文件打开慢，多行 docstring 第二行起不是字符串色、里面的 # 被当注释。
    GenericHighlighter 的 block_rules 用 blockState 维持三引号状态。"""
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


