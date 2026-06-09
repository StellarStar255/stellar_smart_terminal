"""AI 行内代码补全（Cursor 风格的灰字建议）。

设计要点：
- 复用主窗口已有的 LLM API 配置（OpenAI 兼容 /chat/completions），后台 QThread 请求，不阻塞 UI。
- 输入停顿后防抖触发；返回的建议以灰字“幽灵文本”叠加在光标处，Tab 接受、Esc 取消。
- 用 generation 计数让过期请求结果自动作废（用户继续输入即失效）。

本模块只依赖 PyQt6 与 requests，不反向依赖 file_editor，便于复用与测试。
"""

import re

from PyQt6.QtCore import Qt, QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor


# 单次请求上下文上限（字符数），避免把整份大文件塞进去浪费 token
_PREFIX_LIMIT = 4000
_SUFFIX_LIMIT = 1500


class CompletionWorker(QThread):
    """在后台线程里向 OpenAI 兼容接口请求一次补全。"""

    done = pyqtSignal(int, str)      # (generation, 建议文本)
    failed = pyqtSignal(int, str)    # (generation, 错误信息)

    def __init__(self, cfg: dict, prefix: str, suffix: str, language: str,
                 generation: int, parent=None):
        super().__init__(parent)
        self._cfg = dict(cfg or {})
        self._prefix = prefix
        self._suffix = suffix
        self._language = language or 'text'
        self._gen = generation

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

            system = (
                "You are an inline code completion engine, similar to GitHub Copilot. "
                "You are given the code before the cursor (<PREFIX>) and after the cursor "
                "(<SUFFIX>). Output ONLY the raw text that should be inserted at the cursor "
                "to continue the code naturally. Do NOT repeat the prefix or the suffix, do "
                "NOT add explanations, and do NOT wrap the output in markdown code fences. "
                "Keep the indentation consistent with the surrounding code. If no completion "
                "is appropriate, output nothing."
            )
            user = (
                f"Language: {self._language}\n\n"
                f"<PREFIX>\n{self._prefix}\n</PREFIX>\n"
                f"<SUFFIX>\n{self._suffix}\n</SUFFIX>\n\n"
                "Insertion text at the cursor:"
            )

            payload = {
                'model': cfg.get('model') or 'gpt-4',
                'messages': [
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user},
                ],
                'temperature': 0.1,
                'stream': False,
            }
            mt = cfg.get('max_tokens')
            try:
                mt = int(mt) if mt else 0
            except (TypeError, ValueError):
                mt = 0
            payload['max_tokens'] = min(mt, 256) if mt > 0 else 256

            proxy = (cfg.get('proxy') or '').strip()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            timeout = cfg.get('timeout') or 30

            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=timeout, proxies=proxies)
            if resp.status_code != 200:
                self.failed.emit(self._gen, f'HTTP {resp.status_code}: {resp.text[:200]}')
                return
            data = resp.json()
            text = (data.get('choices') or [{}])[0].get('message', {}).get('content', '')
            self.done.emit(self._gen, text or '')
        except Exception as e:
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

    def dismiss(self):
        """清除当前建议，并让在途请求结果作废。"""
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

        self._gen += 1
        gen = self._gen
        self._anchor_pos = pos

        worker = CompletionWorker(cfg, prefix, suffix, language, gen, self)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda w=worker: self._reap(w))
        self._workers.append(worker)
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

    def _on_done(self, gen: int, text: str):
        if gen != self._gen or not self._enabled:
            return  # 过期或已关闭
        ed = self._editor
        # 期间光标移动了则作废
        if ed.textCursor().position() != self._anchor_pos:
            return
        cleaned = self._clean(text)
        if not cleaned:
            return
        self._suggestion = cleaned
        ed.viewport().update()

    # 单条建议最多展示/插入的行数，避免模型偶尔长篇大论盖满屏幕
    MAX_LINES = 12

    @staticmethod
    def _clean(text: str) -> str:
        if not text:
            return ''
        t = text
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
