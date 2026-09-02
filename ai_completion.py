"""AI 行内代码补全（Cursor 风格的灰字建议）。

设计要点：
- 复用主窗口已有的 LLM API 配置（OpenAI 兼容 /chat/completions），后台 QThread 请求，不阻塞 UI。
- 输入停顿后防抖触发；返回的建议以灰字“幽灵文本”叠加在光标处，Tab 接受、Esc 取消。
- 用 generation 计数让过期请求结果自动作废（用户继续输入即失效）。

本模块只依赖 PyQt6、requests 与 app_logging，不反向依赖 file_editor，便于复用与测试。
"""

import json
import re

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor

from app_logging import get_logger

logger = get_logger(__name__)


# 单次请求上下文上限（字符数），避免把整份大文件塞进去浪费 token。
# 模块级常量，便于以后做成可配置项。
# 截断方向：prefix 取尾部（保留靠近光标的内容），suffix 取头部（同理）。
# suffix 不宜过长：弱模型常把 suffix 里的代码原样抄回来当“补全”。
PREFIX_LIMIT = 8000
SUFFIX_LIMIT = 2000

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
        self._resp = None  # 在途的 requests.Response；cancel() 时直接关掉底层连接

    def cancel(self):
        """请求作废：用户继续输入时调用，让流式循环尽快退出。

        除了置标志位（流式循环逐行轮询），还直接 close() 在途的 Response，
        关闭底层 socket，让阻塞在网络读上的 iter_lines 立刻抛错退出，
        不再占用连接资源、也不再回填过期结果。
        """
        self._cancelled = True
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                logger.debug("cancel: suppressed exception", exc_info=True)

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
            headers = {'Content-Type': 'application/json'}
            key = (cfg.get('api_key') or '').strip()
            if key:
                headers['Authorization'] = f'Bearer {key}'

            mt = cfg.get('max_tokens')
            try:
                mt = int(mt) if mt else 0
            except (TypeError, ValueError):
                mt = 0

            api_l = api_base.lower()
            model_l = (cfg.get('model') or '').lower()

            if 'deepseek' in api_l:
                # DeepSeek 原生 FIM（fill-in-the-middle）：专为补全、非思考模式、最准最快。
                # 端点固定在 /beta/completions（不在 /v1 下），用 prompt+suffix，不走 chat。
                root = re.match(r'(https?://[^/]+)', api_base)
                base_root = root.group(1) if root else api_base
                url = base_root + '/beta/completions'
                payload = {
                    'model': cfg.get('model') or 'deepseek-chat',
                    'prompt': self._prefix,
                    'suffix': self._suffix,
                    'temperature': 0.1,
                    'max_tokens': min(mt, 256) if mt > 0 else 256,  # FIM 上限 4K，补全压短
                    'stream': True,
                }
            else:
                # 其它厂商：用 chat 接口模拟 FIM —— 光标 marker + few-shot 示范。
                url = api_base + '/chat/completions'
                m = _CURSOR_MARKER
                system = (
                    "You are an inline code autocomplete engine, like GitHub Copilot. "
                    f"The user's file is shown with a {m} marker at the caret. "
                    f"Reply with ONLY the exact characters to insert at {m}, as if you were "
                    "the user's very next keystrokes.\n"
                    "Rules:\n"
                    "- Output raw text only: no explanation, no markdown fences, no quotes, "
                    "no <think>.\n"
                    f"- Continue from the characters right before {m}; never restate them.\n"
                    f"- NEVER reproduce text that already appears right after {m} — that code "
                    "already exists; if you would only be echoing it, reply with nothing.\n"
                    "- Keep it short: the rest of the current line, or a few lines, then stop "
                    "at a natural boundary. Never dump a large block.\n"
                    "- Inside a comment / string / docstring, briefly continue that prose "
                    "instead of writing code.\n"
                    "- If nothing sensible can be added, reply with an empty string."
                )
                context = f"Language: {self._language}\n\n{self._prefix}{m}{self._suffix}"
                messages = [
                    {'role': 'system', 'content': system},
                    {'role': 'user',
                     'content': f"Language: python\n\ndef add(a, b):\n    return {m}"},
                    {'role': 'assistant', 'content': 'a + b'},
                    {'role': 'user',
                     'content': f"Language: python\n\nitems = [1, 2, 3]\nfor x in {m}:\n    print(x)"},
                    {'role': 'assistant', 'content': 'items'},
                    {'role': 'user',
                     'content': f"Language: python\n\n\"\"\"\n计算两个数的{m}\n\"\"\"\ndef add(a, b):"},
                    {'role': 'assistant', 'content': '和'},
                    {'role': 'user', 'content': context},
                ]
                payload = {
                    'model': cfg.get('model') or 'gpt-4',
                    'messages': messages,
                    'temperature': 0.1,
                    'stream': True,
                }
                if 'minimax' in api_l:
                    if 'm3' in model_l:
                        payload['thinking'] = {'type': 'disabled'}
                        payload['max_tokens'] = min(mt, 120) if mt > 0 else 120
                    else:
                        payload['reasoning_split'] = True
                        payload['max_tokens'] = min(mt, 1024) if mt > 0 else 1024
                else:
                    payload['max_tokens'] = min(mt, 120) if mt > 0 else 120

            proxy = (cfg.get('proxy') or '').strip()
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            timeout = cfg.get('timeout') or 30
            # (连接, 读取) 分开设超时：cancel() 只能关已建立的 Response，
            # 连接建立阶段无法打断，若用单一超时最长会白等 30s
            timeout = (3.05, timeout)

            if self._cancelled:
                return
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=timeout, proxies=proxies, stream=True)
            self._resp = resp  # 暴露给 cancel()，可从主线程关闭底层连接
            if self._cancelled:
                try:
                    resp.close()
                except Exception:
                    logger.debug("run: suppressed exception", exc_info=True)
                return
            if resp.status_code != 200:
                body = ''
                try:
                    body = resp.text[:200]
                except Exception:
                    logger.debug("run: suppressed exception", exc_info=True)
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
                        logger.debug("run: suppressed exception", exc_info=True)
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
                    # chat 流式在 delta.content；FIM/completions 流式在 choices[].text
                    piece = delta.get('content')
                    if piece is None:
                        piece = ch.get('text')
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
                    ch0 = (obj.get('choices') or [{}])[0]
                    acc = (ch0.get('text')
                           or ch0.get('message', {}).get('content', '') or '')
                except Exception:
                    logger.debug("run: suppressed exception", exc_info=True)

            if self._cancelled:
                return
            self.done.emit(self._gen, acc)
        except Exception as e:
            # cancel() 关闭连接会让阻塞读抛错，这属于预期退出，静默即可
            if not self._cancelled:
                self.failed.emit(self._gen, str(e))
        finally:
            resp = self._resp
            self._resp = None
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    logger.debug("run: suppressed exception", exc_info=True)


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
        self._anchor_midline = False   # 触发时光标后本行是否还有内容（行中触发）
        self._gen = 0                  # generation：过期结果作废
        self._color = QColor('#6a737d')
        self._bg_color = QColor('#282c34')  # 灰字底色（= 编辑器背景），用于盖住下方真实文字
        self._fg_color = QColor('#abb2bf')  # 正文颜色：行中触发时重画被“推后”的剩余文本
        self._workers = []             # 持有运行中的 worker，防止被 GC
        self._active_worker = None     # 当前在途请求；新请求/撤销时取消它
        self._req_suffix = ''

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_request)

        # 编辑器销毁前取消所有在途请求：destroyed 在其子对象（含本
        # controller）析构之前发出，此时调用自身方法仍安全
        editor.destroyed.connect(self._on_editor_destroyed)

    def _on_editor_destroyed(self):
        self.shutdown(wait_ms=0)

    def shutdown(self, wait_ms: int = 1000):
        """取消所有在途请求；wait_ms > 0 时等待线程退出。

        worker 未设 Qt 父对象，不会被编辑器析构递归销毁；cancel() 会关闭
        底层 socket，run() 随即返回，无需依赖等待也能安全退出。
        """
        try:
            self._timer.stop()
        except RuntimeError:  # Qt 对象已销毁（窗口/面板已关）
            pass
        self._active_worker = None
        for w in list(self._workers):
            try:
                w.cancel()
            except Exception:
                logger.debug("shutdown: suppressed exception", exc_info=True)
        if wait_ms > 0:
            for w in list(self._workers):
                try:
                    if w.isRunning():
                        w.wait(wait_ms)
                except RuntimeError:  # Qt 对象已销毁（窗口/面板已关）
                    pass

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

    def set_fg_color(self, color: QColor):
        """编辑器正文颜色：行中触发时用它重画被灰字“推后”的剩余文本。"""
        if color is not None:
            self._fg_color = QColor(color)

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
                logger.debug("_cancel_active: suppressed exception", exc_info=True)

    def dismiss(self):
        """清除当前建议，并让在途请求结果作废。"""
        self._cancel_active()
        self._anchor_midline = False
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
        self._anchor_midline = False
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
        # 行尾、行中都可触发（FIM 模型本就支持任意 suffix）。
        # 唯一限制：光标正卡在单词中间（前后字符都是标识符字符）时不触发，
        # 避免用户打字打到一半就疯狂发请求。
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        prev_ch = block_text[col - 1] if col > 0 else ''
        next_ch = block_text[col] if col < len(block_text) else ''
        if self._is_word_char(prev_ch) and self._is_word_char(next_ch):
            return
        # 行中触发：光标后本行还有非空白内容 → 只渲染/插入单行建议
        midline = block_text[col:].strip() != ''
        cfg = None
        try:
            cfg = ed.ai_get_config()
        except Exception:
            cfg = None
        if not cfg or not (cfg.get('api_key') or '').strip():
            return

        pos = cursor.position()
        full = ed.toPlainText()
        # prefix 截尾部（保留靠近光标的），suffix 截头部（保留靠近光标的）
        prefix = full[:pos][-PREFIX_LIMIT:]
        suffix = full[pos:pos + SUFFIX_LIMIT]
        language = 'text'
        try:
            language = ed.ai_get_language()
        except Exception:
            logger.debug("_start_request: suppressed exception", exc_info=True)

        self._cancel_active()  # 取消上一个在途请求
        self._gen += 1
        gen = self._gen
        self._anchor_pos = pos
        self._anchor_midline = midline
        self._req_suffix = suffix  # 用于在结果里识别“抄 suffix”的回声

        # 不设 Qt 父对象：worker 若挂在 controller→editor 父链上，编辑器
        # 关闭时 Qt 会递归销毁仍在运行的 QThread（"QThread: Destroyed
        # while thread is still running"，可致崩溃）。由 _workers 持引用
        # 防 GC，销毁编辑器时走 shutdown() 取消。
        worker = CompletionWorker(cfg, prefix, suffix, language, gen)
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
        except ValueError:  # 非法值按未提供处理
            pass
        worker.deleteLater()

    def _on_failed(self, gen: int, msg: str):
        # 静默失败（不打扰编辑），但记一行日志，否则配置错误无从排查
        logger.debug(f"[AICompletion] request failed (gen={gen}): {msg}")

    def _on_chunk(self, gen: int, acc: str):
        """流式过程中边收边显示，灰字尽快出现、逐步变长。"""
        if gen != self._gen or not self._enabled:
            return
        ed = self._editor
        if ed.textCursor().position() != self._anchor_pos:
            return
        cleaned = self._clean(acc)
        if self._anchor_midline:
            # 行中触发只展示单行 ghost（overlay 渲染多行会盖住下方真实代码）
            cleaned = cleaned.split('\n', 1)[0]
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
        if self._anchor_midline:
            # 行中触发只展示/插入单行（与 _on_chunk 保持一致）
            cleaned = cleaned.split('\n', 1)[0]
        req_suffix = getattr(self, '_req_suffix', '')
        # 反回声（截断版）：建议结尾把 suffix 开头抄回来 → 截掉重复部分
        cleaned = self._trim_suffix_echo(cleaned, req_suffix)
        # 反回声（整体版）：若建议只是把光标后的 suffix 原样抄回来，丢弃（弱模型常见失败）
        if not cleaned or self._is_echo(cleaned, req_suffix):
            if self._suggestion:  # 撤掉流式中途显示出来的回声/空内容
                self._suggestion = ''
                ed.viewport().update()
            return
        self._suggestion = cleaned
        ed.viewport().update()

    @staticmethod
    def _is_word_char(ch: str) -> bool:
        return bool(ch) and (ch.isalnum() or ch == '_')

    @staticmethod
    def _is_echo(cand: str, suffix: str) -> bool:
        c = cand.strip()
        s = (suffix or '').lstrip()
        if not c or not s:
            return False
        head = c[:40]
        # suffix 紧接着就是这段建议，或建议开头大段出现在 suffix 前部 → 判为回声
        return s.startswith(head) or (len(head) >= 12 and head in s[:300])

    @staticmethod
    def _trim_suffix_echo(cand: str, suffix: str) -> str:
        """若建议结尾把光标后已有内容（suffix 开头）原样抄了一遍，截掉重复部分。

        典型场景：行中触发时光标在 `foo(|)` 里，模型返回 `bar)` —— 末尾的 `)`
        与 suffix 开头重复，截掉后接受补全才不会出现 `bar))`。
        取最长重叠，整段都是回声时返回空串（调用方按无建议处理）。
        """
        s = suffix or ''
        if not cand or not s:
            return cand
        for k in range(min(len(cand), len(s)), 0, -1):
            if cand.endswith(s[:k]):
                return cand[:-k]
        return cand

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

        # 行中触发：光标后本行还有内容，需要把它“推后”显示。
        # 实现：先用底色盖住光标右侧整段原文，画完灰字后再用正文色把剩余文本
        # 画回灰字之后（视觉上等价于剩余文本被建议推开；该段暂不带语法高亮）。
        block_text = cursor.block().text()
        col = cursor.positionInBlock()
        rest = block_text[col:]

        painter.save()
        painter.setFont(ed.font())
        for i, ln in enumerate(lines):
            if i == 0:
                x = cr.left()       # 第一行紧接光标
                top = cr.top()
            else:
                x = left_x          # 续行从行首列开始
                top = cr.top() + i * line_h
            w = fm.horizontalAdvance(ln) if ln else fm.horizontalAdvance(' ')
            if i == 0 and rest:
                # 盖住光标右侧到视口右缘的原文，避免灰字与已有代码重叠
                cover_w = ed.viewport().width() - int(x)
                painter.fillRect(int(x), int(top), cover_w, int(line_h), self._bg_color)
                painter.setPen(self._color)
                painter.drawText(int(x), int(top + ascent), ln)
                # 把被盖住的剩余文本用正文色画回灰字之后（“推后”效果）
                painter.setPen(self._fg_color)
                painter.drawText(int(x + w), int(top + ascent), rest)
            else:
                # 关键：先用不透明底色盖住下方真实文字，再画灰字，避免与已有代码重叠看不清
                painter.fillRect(int(x), int(top), int(w) + 4, int(line_h), self._bg_color)
                painter.setPen(self._color)
                painter.drawText(int(x), int(top + ascent), ln)
        painter.restore()
