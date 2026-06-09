"""AI 行内代码补全（Cursor 风格的灰字建议）。

设计要点：
- 复用主窗口已有的 LLM API 配置（OpenAI 兼容 /chat/completions），后台 QThread 请求，不阻塞 UI。
- 输入停顿后防抖触发；返回的建议以灰字“幽灵文本”叠加在光标处，Tab 接受、Esc 取消。
- 用 generation 计数让过期请求结果自动作废（用户继续输入即失效）。

本模块只依赖 PyQt6 与 requests，不反向依赖 file_editor，便于复用与测试。
"""

import json
import re

from PyQt6.QtCore import Qt, QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor


# 单次请求上下文上限（字符数），避免把整份大文件塞进去浪费 token。
# suffix 不宜过长：弱模型常把 suffix 里的代码原样抄回来当“补全”。
_PREFIX_LIMIT = 4000
_SUFFIX_LIMIT = 600

# 光标占位符：放进真实代码里标记插入点。选一个几乎不可能出现在代码里的串。
_CURSOR_MARKER = '<|CURSOR|>'


class CompletionWorker(QThread):
    """在后台线程里向 OpenAI 兼容接口请求一次补全（流式）。"""

    done = pyqtSignal(int, str)      # (generation, 完整建议文本)
    chunk = pyqtSignal(int, str)     # (generation, 累计到目前的建议文本) —— 边收边显示
    failed = pyqtSignal(int, str)    # (generation, 错误信息)

    def __init__(self, cfg: dict, prefix: str, suffix: str, language: str,
                 generation: int, parent=None):
        super().__init__(parent)
        self._cfg = dict(cfg or {})
        self._prefix = prefix
        self._suffix = suffix
        self._language = language or 'text'
        self._gen = generation
        self._cancelled = False

    def cancel(self):
        """请求作废：用户继续输入时调用，让流式循环尽快退出。"""
        self._cancelled = True

    def run(self):
        try:
            import requests
        except Exception as e:  # requests 不可用
            self.failed.emit(self._gen, f'requests unavailable: {e}')
            return
        try:
            cfg = self._cfg
            api_base = (cfg.get('api_base') or '').strip().rstrip('/')
            if not api_base:
                self.failed.emit(self._gen, 'no api_base')
                return
            url = api_base + '/chat/completions'

            headers = {'Content-Type': 'application/json'}
            key = (cfg.get('api_key') or '').strip()
            if key:
                headers['Authorization'] = f'Bearer {key}'

            m = _CURSOR_MARKER
            system = (
                "You are an inline code autocomplete engine, like GitHub Copilot. "
                f"The user's file is shown with a {m} marker at the caret. "
                f"Reply with ONLY the exact characters to insert at {m}, as if you were the "
                "user's very next keystrokes.\n"
                "Rules:\n"
                "- Output raw text only: no explanation, no markdown fences, no quotes, no "
                "<think>.\n"
                f"- Continue from the characters right before {m}; never restate them.\n"
                f"- NEVER reproduce text that already appears right after {m} — that code "
                "already exists; if you would only be echoing it, reply with nothing.\n"
                "- Keep it short: the rest of the current line, or a few lines, then stop at a "
                "natural boundary. Never dump a large block.\n"
                "- Inside a comment / string / docstring, briefly continue that prose instead "
                "of writing code.\n"
                "- If nothing sensible can be added, reply with an empty string."
            )

            # 把光标位置用单个 marker 放进真实代码里——比 PREFIX/SUFFIX 标签更贴近
            # chat 模型的直觉；再配两个 few-shot 例子，明确「续写、别抄后文」。
            context = f"Language: {self._language}\n\n{self._prefix}{m}{self._suffix}"
            messages = [
                {'role': 'system', 'content': system},
                # 例1：行尾续写
                {'role': 'user',
                 'content': f"Language: python\n\ndef add(a, b):\n    return {m}"},
                {'role': 'assistant', 'content': 'a + b'},
                # 例2：光标在已有后文之前——只补该补的，不抄后面的 `:` 行
                {'role': 'user',
                 'content': f"Language: python\n\nitems = [1, 2, 3]\nfor x in {m}:\n    print(x)"},
                {'role': 'assistant', 'content': 'items'},
                # 例3：光标在注释/docstring 里——续写文字，而不是写代码
                {'role': 'user',
                 'content': f"Language: python\n\n\"\"\"\n计算两个数的{m}\n\"\"\"\ndef add(a, b):"},
                {'role': 'assistant', 'content': '和'},
                # 真正要补全的内容
                {'role': 'user', 'content': context},
            ]

            payload = {
                'model': cfg.get('model') or 'gpt-4',
                'messages': messages,
                'temperature': 0.1,
                'stream': True,   # 流式：边收边显示，避免等整段返回才出现
            }
            mt = cfg.get('max_tokens')
            try:
                mt = int(mt) if mt else 0
            except (TypeError, ValueError):
                mt = 0

            model_l = (cfg.get('model') or '').lower()
            if 'minimax' in api_base.lower():
                if 'm3' in model_l:
                    # MiniMax M3 支持关思考：直接作答，快，最适合补全
                    payload['thinking'] = {'type': 'disabled'}
                    payload['max_tokens'] = min(mt, 120) if mt > 0 else 120
                else:
                    # M2.x 无法关思考；reasoning_split=true 让思考走单独的
                    # reasoning_content 字段，content 里是干净答案（否则混入 <think>），
                    # 思考会吃 token，给足额度，否则只思考没答案。
                    payload['reasoning_split'] = True
                    payload['max_tokens'] = min(mt, 1024) if mt > 0 else 1024
            else:
                # 普通模型：补全要短，上限压到 120 token，不容易跑题成大段
                payload['max_tokens'] = min(mt, 120) if mt > 0 else 120

            proxy = (cfg.get('proxy') or '').strip()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            timeout = cfg.get('timeout') or 30

            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=timeout, proxies=proxies, stream=True)
            if resp.status_code != 200:
                body = ''
                try:
                    body = resp.text[:200]
                except Exception:
                    pass
                self.failed.emit(self._gen, f'HTTP {resp.status_code}: {body}')
                return

            acc = ''
            got_delta = False
            raw_lines = []
            for line in resp.iter_lines(decode_unicode=True):
                if self._cancelled:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    return
                if not line:
                    continue
                line = line.strip()
                if line.startswith('data:'):
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    ch = (obj.get('choices') or [{}])[0]
                    delta = ch.get('delta') or {}
                    piece = delta.get('content')
                    if piece is None and ch.get('message'):
                        piece = ch['message'].get('content')
                    if piece:
                        got_delta = True
                        acc += piece
                        self.chunk.emit(self._gen, acc)
                else:
                    # 某些服务即使 stream=true 也回整段 JSON，先存着兜底
                    raw_lines.append(line)

            if not got_delta and raw_lines:
                try:
                    obj = json.loads('\n'.join(raw_lines))
                    acc = (obj.get('choices') or [{}])[0].get(
                        'message', {}).get('content', '') or ''
                except Exception:
                    pass

            self.done.emit(self._gen, acc)
        except Exception as e:
            if not self._cancelled:
                self.failed.emit(self._gen, str(e))


class InlineCompletionController(QObject):
    """挂在一个 CodeEditor 上，负责防抖触发、渲染灰字、接受/取消。

    需要宿主 editor 提供：
      - ai_get_config() -> dict|None     当前可用的 LLM 配置（含 api_key）
      - ai_get_language() -> str         当前文件语言（用于提示词）
    """

    DEBOUNCE_MS = 400

    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor
        self._enabled = False
        self._suggestion = ''          # 当前灰字建议（可能多行）
        self._anchor_pos = -1          # 建议锚定的光标绝对位置
        self._gen = 0                  # generation：过期结果作废
        self._color = QColor('#6a737d')
        self._bg_color = QColor('#282c34')  # 灰字底色（= 编辑器背景），用于盖住下方真实文字
        self._workers = []             # 持有运行中的 worker，防止被 GC
        self._active_worker = None     # 当前在途请求；新请求/撤销时取消它
        self._req_suffix = ''

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_request)

    # ---------- 开关 / 外观 ----------

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        if not self._enabled:
            self._timer.stop()
            self.dismiss()

    def is_enabled(self) -> bool:
        return self._enabled

    def set_color(self, color: QColor):
        if color is not None:
            self._color = QColor(color)

    def set_bg_color(self, color: QColor):
        if color is not None:
            self._bg_color = QColor(color)

    # ---------- 建议状态 ----------

    def has_suggestion(self) -> bool:
        return bool(self._suggestion)

    def current_suggestion(self) -> str:
        return self._suggestion

    def _cancel_active(self):
        """取消在途的流式请求（继续输入/撤销时调用）。"""
        w = self._active_worker
        self._active_worker = None
        if w is not None:
            try:
                w.cancel()
            except Exception:
                pass

    def dismiss(self):
        """清除当前建议，并让在途请求结果作废。"""
        self._cancel_active()
        if self._suggestion:
            self._suggestion = ''
            self._anchor_pos = -1
            self._gen += 1
            self._editor.viewport().update()
        else:
            self._anchor_pos = -1

    def accept(self) -> bool:
        """把建议插入到光标处。成功返回 True。"""
        if not self._suggestion:
            return False
        text = self._suggestion
        # 先清状态，避免插入触发的信号又把它当成新输入
        self._cancel_active()
        self._suggestion = ''
        self._anchor_pos = -1
        self._gen += 1
        cursor = self._editor.textCursor()
        cursor.insertText(text)
        self._editor.setTextCursor(cursor)
        self._editor.viewport().update()
        return True

    # ---------- 触发 ----------

    def on_text_changed(self):
        if not self._enabled:
            return
        # 任何文本变化都先撤掉旧建议，再防抖请求新的
        if self._suggestion:
            self.dismiss()
        self._timer.start(self.DEBOUNCE_MS)

    def on_cursor_moved(self):
        # 光标离开锚点（方向键等）则撤销建议
        if self._suggestion and self._editor.textCursor().position() != self._anchor_pos:
            self.dismiss()

    def request_now(self):
        """手动触发（绕过防抖）。"""
        if not self._enabled:
            return
        self._timer.stop()
        self._start_request()

    def _start_request(self):
        if not self._enabled:
            return
        ed = self._editor
        # 仅在编辑器获焦时请求，避免打开文件/后台窗格也发请求浪费 API
        if not ed.hasFocus():
            return
        cursor = ed.textCursor()
        if cursor.hasSelection():
            return
        # 仅在“行尾”补全：光标之后本行只剩空白时才请求，避免灰字与已有文字重叠
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        if block_text[col:].strip() != '':
            return
        cfg = None
        try:
            cfg = ed.ai_get_config()
        except Exception:
            cfg = None
        if not cfg or not (cfg.get('api_key') or '').strip():
            return

        pos = cursor.position()
        full = ed.toPlainText()
        prefix = full[:pos][-_PREFIX_LIMIT:]
        suffix = full[pos:pos + _SUFFIX_LIMIT]
        language = 'text'
        try:
            language = ed.ai_get_language()
        except Exception:
            pass

        self._cancel_active()  # 取消上一个在途请求
        self._gen += 1
        gen = self._gen
        self._anchor_pos = pos
        self._req_suffix = suffix  # 用于在结果里识别“抄 suffix”的回声

        worker = CompletionWorker(cfg, prefix, suffix, language, gen, self)
        worker.chunk.connect(self._on_chunk)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda w=worker: self._reap(w))
        self._workers.append(worker)
        self._active_worker = worker
        worker.start()

    def _reap(self, worker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def _on_failed(self, gen: int, msg: str):
        # 静默失败（不打扰编辑）；如需调试可在此打印
        pass

    def _on_chunk(self, gen: int, acc: str):
        """流式过程中边收边显示，灰字尽快出现、逐步变长。"""
        if gen != self._gen or not self._enabled:
            return
        ed = self._editor
        if ed.textCursor().position() != self._anchor_pos:
            return
        cleaned = self._clean(acc)
        if not cleaned or cleaned == self._suggestion:
            return
        # 流式中途暂不做反回声判断（内容还没收全），仅最终态严格过滤
        self._suggestion = cleaned
        ed.viewport().update()

    def _on_done(self, gen: int, text: str):
        if gen != self._gen or not self._enabled:
            return  # 过期或已关闭
        ed = self._editor
        # 期间光标移动了则作废
        if ed.textCursor().position() != self._anchor_pos:
            self.dismiss()
            return
        cleaned = self._clean(text)
        # 反回声：若建议只是把光标后的 suffix 原样抄回来，丢弃（弱模型常见失败）
        if not cleaned or self._is_echo(cleaned, getattr(self, '_req_suffix', '')):
            if self._suggestion:  # 撤掉流式中途显示出来的回声/空内容
                self._suggestion = ''
                ed.viewport().update()
            return
        self._suggestion = cleaned
        ed.viewport().update()

    @staticmethod
    def _is_echo(cand: str, suffix: str) -> bool:
        c = cand.strip()
        s = (suffix or '').lstrip()
        if not c or not s:
            return False
        head = c[:40]
        # suffix 紧接着就是这段建议，或建议开头大段出现在 suffix 前部 → 判为回声
        return s.startswith(head) or (len(head) >= 12 and head in s[:300])

    # 单条建议最多展示/插入的行数，避免模型偶尔长篇大论盖满屏幕
    MAX_LINES = 12

    @staticmethod
    def _clean(text: str) -> str:
        if not text:
            return ''
        t = text
        # 0) 模型偶尔把光标 marker 也抄回来，去掉
        t = t.replace(_CURSOR_MARKER, '')
        # 1) 去掉推理模型的 <think>...</think> 块（含未闭合：截断时只剩开标签）
        t = re.sub(r'(?is)<think>.*?</think>', '', t)
        t = re.sub(r'(?is)<think>.*$', '', t)
        # 2) 去掉整段 ``` 代码围栏
        s = t.strip()
        if s.startswith('```'):
            nl = s.find('\n')
            if nl != -1:
                s = s[nl + 1:]
            s = s.rstrip()
            if s.endswith('```'):
                s = s[:-3]
            t = s
        # 3) 去掉首尾多余换行，但保留内部换行与行首缩进
        t = t.strip('\n')
        # 4) 限制行数，过长的多半是模型跑题，截断更安全
        lines = t.split('\n')
        if len(lines) > InlineCompletionController.MAX_LINES:
            t = '\n'.join(lines[:InlineCompletionController.MAX_LINES])
        return t

    # ---------- 渲染灰字 ----------

    def paint(self, painter):
        if not self._enabled or not self._suggestion:
            return
        ed = self._editor
        cursor = ed.textCursor()
        if cursor.hasSelection() or cursor.position() != self._anchor_pos:
            return
        lines = self._suggestion.split('\n')
        fm = ed.fontMetrics()
        cr = ed.cursorRect(cursor)
        ascent = fm.ascent()
        line_h = fm.lineSpacing()
        # 续行起点 x（行首列），对齐文本左边界
        bc = QTextCursor(cursor)
        bc.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        left_x = ed.cursorRect(bc).left()

        painter.save()
        painter.setFont(ed.font())
        for i, ln in enumerate(lines):
            if i == 0:
                x = cr.left()       # 第一行紧接光标（仅在行尾触发，右侧无文字）
                top = cr.top()
            else:
                x = left_x          # 续行从行首列开始
                top = cr.top() + i * line_h
            w = fm.horizontalAdvance(ln) if ln else fm.horizontalAdvance(' ')
            # 关键：先用不透明底色盖住下方真实文字，再画灰字，避免与已有代码重叠看不清
            painter.fillRect(int(x), int(top), int(w) + 4, int(line_h), self._bg_color)
            painter.setPen(self._color)
            painter.drawText(int(x), int(top + ascent), ln)
        painter.restore()
