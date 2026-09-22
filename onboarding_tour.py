"""首次启动的互动式新手教程（聚光灯引导）。

在主窗口上盖一层半透明遮罩，把当前要讲的控件"挖亮"，旁边挂一张说明卡片。
关键步骤要求用户真的做一下（点 ⚡、点 +、点 Split、按 ⌘K …），做到了自动进
下一步；做不到也能用「跳过此步」继续，绝不把人卡住。

设计要点：
- 遮罩是 MainWindow 的直接子控件（不是 top-level 窗口），盖住整个窗口含工具栏。
  用 setMask 把"洞"抠出遮罩，洞里的点击直接落到底下的真实控件上——这就是
  "互动"的实现方式，不需要转发事件。
- 目标控件按名字从 MainWindow._toolbar_buttons 注册表取（工具栏按钮可能被用户
  隐藏、也可能在单行/双行两种布局里），不可见的目标自动跳过整步。
- 每 150ms 轮询一次：目标位置变了就重新排版（打开侧栏、新建标签都会改布局），
  互动步骤的完成条件满足了就打勾并自动前进。
- 看完或跳过都写 onboarding_shown=True 到配置，之后从「帮助 › 新手教程」重看。
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, QEvent, QObject, QPoint, QRect, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QRegion, QKeyEvent
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

import app_config
from app_logging import get_logger
from i18n import t
from themes import is_light

logger = get_logger(__name__)

CONFIG_KEY = 'onboarding_shown'


def should_show_onboarding(config: dict) -> bool:
    """首次启动判断：配置里没有"已看过"标记就弹。

    按标记而不是"有没有配置文件"判断：老用户升级到带教程的版本也弹一次
    （⚡ 这类入口正是老用户也不知道的）。
    """
    try:
        return not bool(config.get(CONFIG_KEY, False))
    except Exception:
        return True


def mark_onboarding_shown() -> None:
    app_config.update_config({CONFIG_KEY: True}, description='onboarding shown')


# ---------------------------------------------------------------------------
# 步骤定义
# ---------------------------------------------------------------------------

class TourStep:
    """一步教程。

    key: i18n 前缀，标题/正文/提示分别取 onboarding.<key>.title / .body / .hint
    targets: MainWindow 上的目标控件名（先查 _toolbar_buttons 注册表再查属性）；
             空列表 = 无目标，卡片居中显示
    done_when: 互动完成条件（接收 MainWindow，返回 bool）；None = 非互动步骤
    on_leave: 离开这一步时的清理（比如把 ⚡ 弹窗收起来）
    """

    def __init__(self, key: str, targets: list[str] = (),
                 done_when: Optional[Callable] = None,
                 on_leave: Optional[Callable] = None):
        self.key = key
        self.targets = list(targets)
        self.done_when = done_when
        self.on_leave = on_leave

    @property
    def interactive(self) -> bool:
        return self.done_when is not None


def _ql_popup_visible(win) -> bool:
    popup = getattr(win, '_ql_popup', None)
    try:
        return popup is not None and popup.isVisible()
    except RuntimeError:
        return False


def _hide_ql_popup(win) -> None:
    popup = getattr(win, '_ql_popup', None)
    try:
        if popup is not None and popup.isVisible():
            popup.hide()
    except RuntimeError:
        pass


def _tab_count(win) -> int:
    try:
        return win.tab_widget.count()
    except Exception:
        return 0


def _current_tab_pane_count(win) -> int:
    try:
        idx = win.tab_widget.currentIndex()
        return len(win.tab_terminals.get(idx, []))
    except Exception:
        return 0


def _any_side_panel_visible(win) -> bool:
    return bool(getattr(win, 'explorer_panel_visible', False)
                or getattr(win, 'git_panel_visible', False)
                or getattr(win, 'remote_panel_visible', False))


def _palette_focused(win) -> bool:
    palette = getattr(win, 'command_palette', None)
    edit = getattr(palette, 'line_edit', None)
    try:
        return edit is not None and edit.hasFocus()
    except RuntimeError:
        return False


class _Baseline:
    """互动条件里"比开始时多了一个"这类判断，需要记住进入该步时的基线。"""

    def __init__(self):
        self.tabs = 0
        self.panes = 0


def build_steps(baseline: _Baseline) -> list[TourStep]:
    return [
        TourStep('welcome'),
        TourStep('dir', ['dir_toolbar']),
        TourStep('quick_launch', ['quick_launch_btn'],
                 done_when=_ql_popup_visible, on_leave=_hide_ql_popup),
        TourStep('new_tab', ['new_tab_btn'],
                 done_when=lambda w: _tab_count(w) > baseline.tabs),
        TourStep('split', ['split_btn', 'split_v_btn', 'close_split_btn'],
                 done_when=lambda w: _current_tab_pane_count(w) > max(baseline.panes, 1)),
        TourStep('panels', ['explorer_toggle_btn', 'git_toggle_btn', 'remote_toggle_btn'],
                 done_when=_any_side_panel_visible),
        TourStep('navigator', ['window_nav_checkbox']),
        TourStep('palette', ['command_palette'], done_when=_palette_focused),
        TourStep('presets', ['preset_combo', 'preset_switch_btn', 'manage_preset_btn',
                             'start_btn', 'stop_btn']),
        TourStep('images', ['image_prefix_checkbox', 'image_local_checkbox']),
        TourStep('tools', ['vscode_open_btn', 'cursor_open_btn', 'log_toggle_btn',
                           'export_btn', 'history_btn', 'clear_btn', 'images_btn']),
        TourStep('theme', ['theme_combo', 'icon_tint_checkbox']),
        TourStep('settings', ['gui_font_spin', 'opacity_spin', 'lang_combo',
                              'pin_row2_checkbox', 'toolbar_settings_btn']),
        TourStep('terminal', ['tab_widget']),
        TourStep('done'),
    ]


# ---------------------------------------------------------------------------
# 遮罩 + 卡片
# ---------------------------------------------------------------------------

_HOLE_PAD = 6
_HOLE_RADIUS = 8
_CARD_WIDTH = 380
_CARD_GAP = 14


class OnboardingOverlay(QWidget):
    """盖在主窗口上的聚光灯遮罩，自带说明卡片。"""

    next_requested = pyqtSignal()
    prev_requested = pyqtSignal()
    skip_requested = pyqtSignal()

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.setObjectName('onboardingOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hole: Optional[QRect] = None
        self._dim = QColor(0, 0, 0, 150)
        self._ring = QColor('#7c5cff')
        self._build_card()

    # ----- 构建 -----

    def _build_card(self):
        self.card = QFrame(self)
        self.card.setObjectName('onboardingCard')
        self.card.setFixedWidth(_CARD_WIDTH)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)

        self.counter_label = QLabel(self.card)
        self.counter_label.setObjectName('onboardingCounter')
        self.title_label = QLabel(self.card)
        self.title_label.setObjectName('onboardingTitle')
        self.title_label.setWordWrap(True)
        self.body_label = QLabel(self.card)
        self.body_label.setObjectName('onboardingBody')
        self.body_label.setWordWrap(True)
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.hint_label = QLabel(self.card)
        self.hint_label.setObjectName('onboardingHint')
        self.hint_label.setWordWrap(True)
        self.hint_label.hide()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.skip_btn = QPushButton(self.card)
        self.skip_btn.setObjectName('onboardingSkip')
        self.skip_btn.setFlat(True)
        self.skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_btn.clicked.connect(self.skip_requested.emit)
        self.prev_btn = QPushButton(self.card)
        self.prev_btn.setObjectName('onboardingPrev')
        self.prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_btn.clicked.connect(self.prev_requested.emit)
        self.next_btn = QPushButton(self.card)
        self.next_btn.setObjectName('onboardingNext')
        self.next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self.next_requested.emit)
        btn_row.addWidget(self.skip_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.prev_btn)
        btn_row.addWidget(self.next_btn)

        lay.addWidget(self.counter_label)
        lay.addWidget(self.title_label)
        lay.addWidget(self.body_label)
        lay.addWidget(self.hint_label)
        lay.addSpacing(4)
        lay.addLayout(btn_row)

    # ----- 主题 -----

    def apply_theme(self, theme: dict):
        light = is_light(theme)
        accent = theme.get('accent', '#7c5cff')
        self._ring = QColor(accent)
        self._dim = QColor(0, 0, 0, 110 if light else 160)
        bg = theme.get('bg_dark', '#2b2b2b')
        text = theme.get('text', '#eeeeee')
        text_dim = theme.get('text_dim', '#999999')
        border = theme.get('border', '#444444')
        hover = theme.get('accent_hover', accent)
        pressed = theme.get('accent_pressed', accent)
        neutral = theme.get('bg_lighter', bg)
        neutral_hover = theme.get('bg_hover', neutral)
        self.card.setStyleSheet(f"""
            QFrame#onboardingCard {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 12px;
            }}
            QLabel {{ background: transparent; border: none; }}
            QLabel#onboardingCounter {{ color: {text_dim}; font-size: 11px; }}
            QLabel#onboardingTitle {{ color: {text}; font-size: 16px; font-weight: 600; }}
            QLabel#onboardingBody {{ color: {text}; font-size: 13px; line-height: 140%; }}
            QLabel#onboardingHint {{
                color: {accent}; font-size: 13px; font-weight: 600;
                padding: 6px 8px; border-radius: 6px;
                background-color: {neutral};
            }}
            QPushButton {{
                padding: 6px 14px; border-radius: 6px; font-size: 13px;
                border: 1px solid {border};
                background-color: {neutral}; color: {text};
            }}
            QPushButton:hover {{ background-color: {neutral_hover}; }}
            QPushButton#onboardingNext {{
                background-color: {accent}; color: #ffffff; border: none; font-weight: 600;
            }}
            QPushButton#onboardingNext:hover {{ background-color: {hover}; }}
            QPushButton#onboardingNext:pressed {{ background-color: {pressed}; }}
            QPushButton#onboardingSkip {{
                background: transparent; border: none; color: {text_dim}; padding: 6px 4px;
            }}
            QPushButton#onboardingSkip:hover {{ color: {text}; }}
            QPushButton:disabled {{ color: {text_dim}; }}
        """)
        self.update()

    # ----- 内容 -----

    def set_content(self, counter: str, title: str, body: str, hint: str,
                    can_prev: bool, next_text: str, skip_text: str, prev_text: str):
        self.counter_label.setText(counter)
        self.title_label.setText(title)
        self.body_label.setText(body)
        if hint:
            self.hint_label.setText(hint)
            self.hint_label.show()
        else:
            self.hint_label.hide()
        self.prev_btn.setText(prev_text)
        self.prev_btn.setVisible(can_prev)
        self.next_btn.setText(next_text)
        self.skip_btn.setText(skip_text)
        self.card.adjustSize()

    def set_hole(self, rect: Optional[QRect]):
        """rect 为窗口坐标系下的目标区域（None = 无目标，卡片居中）。"""
        self._hole = rect.adjusted(-_HOLE_PAD, -_HOLE_PAD, _HOLE_PAD, _HOLE_PAD) if rect else None
        self._relayout()

    def hole(self) -> Optional[QRect]:
        return QRect(self._hole) if self._hole else None

    def _relayout(self):
        full = self.rect()
        self._place_card()
        if self._hole is not None:
            hole = self._hole.intersected(full)
            # 卡片有时只能落在洞里（目标是整个终端区时），mask 要把卡片区域加回来，
            # 否则卡片被裁掉、点击也会穿透到底下
            mask = QRegion(full) - QRegion(hole)
            mask = mask.united(QRegion(self.card.geometry()))
            self.setMask(mask)
        else:
            self.clearMask()
        self.update()

    def _place_card(self):
        self.card.adjustSize()
        cw, ch = self.card.width(), self.card.sizeHint().height()
        self.card.resize(cw, ch)
        full = self.rect()
        if self._hole is None:
            x = (full.width() - cw) // 2
            y = (full.height() - ch) // 2
            self.card.move(max(0, x), max(0, y))
            return
        hole = self._hole
        # 目标占了大半个窗口（比如整个终端区）：卡片直接放在洞的正中
        if hole.width() * hole.height() > full.width() * full.height() * 0.5:
            self.card.move(max(8, hole.center().x() - cw // 2),
                           max(8, hole.center().y() - ch // 2))
            return
        # 优先放在洞的下方，放不下就上方，再不行放左右；水平方向对齐洞的左边
        x = hole.left()
        if hole.bottom() + _CARD_GAP + ch <= full.height():
            y = hole.bottom() + _CARD_GAP
        elif hole.top() - _CARD_GAP - ch >= 0:
            y = hole.top() - _CARD_GAP - ch
        else:
            y = max(0, min(hole.top(), full.height() - ch))
            if hole.right() + _CARD_GAP + cw <= full.width():
                x = hole.right() + _CARD_GAP
            else:
                x = max(0, hole.left() - _CARD_GAP - cw)
        x = max(8, min(x, full.width() - cw - 8))
        y = max(8, min(y, full.height() - ch - 8))
        self.card.move(x, y)

    # ----- 事件 -----

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        full = self.rect()
        if self._hole is None:
            painter.fillRect(full, self._dim)
            return
        hole = self._hole
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        path.addRect(float(full.x()), float(full.y()), float(full.width()), float(full.height()))
        path.addRoundedRect(float(hole.x()), float(hole.y()), float(hole.width()),
                            float(hole.height()), _HOLE_RADIUS, _HOLE_RADIUS)
        painter.fillPath(path, self._dim)
        # 高亮环画在洞的外沿（洞内被 mask 裁掉，画进去也看不见）
        pen = QPen(self._ring, 3)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        ring = hole.adjusted(-2, -2, 2, 2)
        painter.drawRoundedRect(ring, _HOLE_RADIUS + 2, _HOLE_RADIUS + 2)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.skip_requested.emit()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.next_requested.emit()
        elif key == Qt.Key.Key_Left:
            self.prev_requested.emit()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # 点遮罩（洞外、卡片外）什么都不做，但吃掉事件以免穿透到底下控件
        event.accept()


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------

class OnboardingTour(QObject):
    """驱动教程步骤：定位目标、轮询互动条件、翻页、收尾记账。"""

    finished = pyqtSignal()

    POLL_MS = 150
    ADVANCE_DELAY_MS = 700

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._baseline = _Baseline()
        self.steps = build_steps(self._baseline)
        self.index = -1
        self._done_flag = False      # 当前互动步骤已完成
        self._finished = False       # 整个教程已结束
        self._advancing = False
        self.overlay = OnboardingOverlay(window)
        self.overlay.next_requested.connect(self.next)
        self.overlay.prev_requested.connect(self.prev)
        self.overlay.skip_requested.connect(self.skip)
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._tick)
        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance_after_done)
        window.installEventFilter(self)
        self.apply_theme()

    # ----- 对外 -----

    def start(self):
        self.overlay.setGeometry(self.window.rect())
        self.overlay.show()
        self.overlay.raise_()
        self.overlay.setFocus()
        self._poll.start()
        self._goto(0)

    def next(self):
        self._advance_timer.stop()
        self._goto(self.index + 1)

    def prev(self):
        self._advance_timer.stop()
        self._goto(self.index - 1, backwards=True)

    def skip(self):
        self._finish()

    def is_active(self) -> bool:
        return self.index >= 0 and not self._finished

    @property
    def current_step(self) -> Optional[TourStep]:
        if 0 <= self.index < len(self.steps):
            return self.steps[self.index]
        return None

    def apply_theme(self, theme: Optional[dict] = None):
        if theme is None:
            getter = getattr(self.window, '_current_theme_dict', None)
            theme = getter() if getter else {}
        self.overlay.apply_theme(theme or {})

    def retranslate(self):
        if self.is_active():
            self._render()

    # ----- 步骤跳转 -----

    def _leave_current(self):
        step = self.current_step
        if step is not None and step.on_leave is not None:
            try:
                step.on_leave(self.window)
            except Exception:
                logger.debug("onboarding on_leave failed", exc_info=True)

    def _goto(self, index: int, backwards: bool = False):
        self._leave_current()
        self._done_flag = False
        self._advancing = False
        if index >= len(self.steps):
            self._finish()
            return
        if index < 0:
            index = 0
        # 目标全不可见的步骤直接跳过（工具栏按钮可被用户隐藏）
        direction = -1 if backwards else 1
        while 0 <= index < len(self.steps):
            step = self.steps[index]
            if not step.targets or self._target_rect(step) is not None:
                break
            index += direction
        if index >= len(self.steps):
            self._finish()
            return
        if index < 0:
            index = 0
        self.index = index
        self._baseline.tabs = _tab_count(self.window)
        self._baseline.panes = _current_tab_pane_count(self.window)
        self._render()
        self.overlay.raise_()

    def _render(self):
        step = self.current_step
        if step is None:
            return
        rect = self._target_rect(step) if step.targets else None
        total = len(self.steps)
        counter = t("onboarding.counter", current=self.index + 1, total=total)
        title = t(f"onboarding.{step.key}.title")
        body = t(f"onboarding.{step.key}.body")
        hint = ''
        if step.interactive:
            hint = (t("onboarding.hint_done") if self._done_flag
                    else t(f"onboarding.{step.key}.hint"))
        last = self.index == total - 1
        if last:
            next_text = t("onboarding.finish")
        elif step.interactive and not self._done_flag:
            next_text = t("onboarding.skip_step")
        else:
            next_text = t("onboarding.next")
        self.overlay.set_content(
            counter=counter, title=title, body=body, hint=hint,
            can_prev=self.index > 0, next_text=next_text,
            skip_text=t("onboarding.skip_tour"), prev_text=t("onboarding.prev"))
        self.overlay.set_hole(rect)
        self._last_rect = rect

    # ----- 目标定位 -----

    def _resolve_widget(self, name: str) -> Optional[QWidget]:
        registry = getattr(self.window, '_toolbar_buttons', {}) or {}
        w = registry.get(name)
        if w is None:
            w = getattr(self.window, name, None)
        if not isinstance(w, QWidget):
            return None
        try:
            if not w.isVisible():
                return None
        except RuntimeError:
            return None
        return w

    def _target_rect(self, step: TourStep) -> Optional[QRect]:
        union: Optional[QRect] = None
        for name in step.targets:
            w = self._resolve_widget(name)
            if w is None:
                continue
            try:
                top_left = w.mapTo(self.window, QPoint(0, 0))
            except Exception:
                continue
            r = QRect(top_left, w.size())
            if r.width() <= 0 or r.height() <= 0:
                continue
            union = r if union is None else union.united(r)
        return union

    # ----- 轮询 -----

    def _tick(self):
        step = self.current_step
        if step is None:
            return
        try:
            if self.overlay.geometry() != self.window.rect():
                self.overlay.setGeometry(self.window.rect())
            if step.targets:
                rect = self._target_rect(step)
                if rect != getattr(self, '_last_rect', None):
                    self._last_rect = rect
                    if rect is None:
                        # 目标在这一步里消失了（比如面板切换）：整体重排
                        self.overlay.set_hole(None)
                    else:
                        self.overlay.set_hole(rect)
            if step.interactive and not self._done_flag and step.done_when(self.window):
                self._done_flag = True
                self._render()
                self._advancing = True
                self._advance_timer.start(self.ADVANCE_DELAY_MS)
        except RuntimeError:
            # 窗口/控件已销毁
            self._poll.stop()

    def _advance_after_done(self):
        if self._advancing and self.is_active():
            self.next()

    # ----- 收尾 -----

    def _finish(self):
        if self._finished:
            return
        self._leave_current()
        self._finished = True
        self._poll.stop()
        self._advance_timer.stop()
        self.index = -1
        try:
            self.window.removeEventFilter(self)
        except RuntimeError:
            pass
        try:
            self.overlay.hide()
            self.overlay.deleteLater()
        except RuntimeError:
            pass
        try:
            mark_onboarding_shown()
        except Exception:
            logger.debug("onboarding: failed to persist flag", exc_info=True)
        self.finished.emit()

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            if self.is_active():
                try:
                    self.overlay.setGeometry(self.window.rect())
                    self.overlay.raise_()
                except RuntimeError:
                    pass
        return False

