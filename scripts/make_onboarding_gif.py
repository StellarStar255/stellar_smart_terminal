"""把新手教程（onboarding_tour）录成 GIF，给 README 用。

跑真实 MainWindow（cocoa 平台，渲染与用户所见一致），自动走完教程：
模拟光标移到目标 → 点击 → 真的触发对应动作（⚡、+、分屏、侧栏、⌘K），
逐帧 window.grab()，顶层弹窗（⚡ 列表、命令面板结果）按位置合成进画面。

用法：
    python scripts/make_onboarding_gif.py zh assets/onboarding-zh.gif
    python scripts/make_onboarding_gif.py en assets/onboarding-en.gif

数据目录/工作目录/shell 提示符都指向一次性演示环境，不会露出本机路径和用户名。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LANG = sys.argv[1] if len(sys.argv) > 1 else 'zh'
OUT = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else f'onboarding-{LANG}.gif')
WIN_W, WIN_H = 1280, 760
OUT_W = 1000          # GIF 输出宽度（逻辑像素 1280 → 缩到 1000，README 里够清楚）
MOVE_FRAMES = 14      # 光标一次移动的帧数
MOVE_MS = 30

# ---- 演示环境：数据目录、演示项目目录、干净的 zsh 提示符 ----
DEMO = tempfile.mkdtemp(prefix='stellar-demo-')
os.environ['STELLAR_DATA_DIR'] = os.path.join(DEMO, 'data')
os.makedirs(os.environ['STELLAR_DATA_DIR'])
PROJECTS = '/tmp/stellar-demo'
DIRS = [os.path.join(PROJECTS, n) for n in ('my-project', 'api-server', 'blog', 'dotfiles')]
for d in DIRS:
    os.makedirs(d, exist_ok=True)
for name in ('README.md', 'main.py', 'requirements.txt', 'pyproject.toml'):
    open(os.path.join(DIRS[0], name), 'a').close()
for sub in ('src', 'tests', 'docs'):
    os.makedirs(os.path.join(DIRS[0], sub), exist_ok=True)
zdot = os.path.join(DEMO, 'zdot')
os.makedirs(zdot)
with open(os.path.join(zdot, '.zshrc'), 'w') as f:
    f.write("PROMPT='%F{cyan}%1~%f %F{green}❯%f '\nunsetopt PROMPT_SP\n")
os.environ['ZDOTDIR'] = zdot
os.environ.pop('PS1', None)
os.chdir(DIRS[0])

import app as appmod  # noqa: E402

appmod.setup_qt_plugin_path()

from PyQt6.QtCore import QEventLoop, QPoint, QPointF, QTimer, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

import app_config  # noqa: E402

with open(app_config.get_config_path(), 'w') as f:
    json.dump({'onboarding_shown': True, 'language': LANG,
               'working_dir_history': DIRS,
               'working_dir_freq': {d: 10 - i for i, d in enumerate(DIRS)}}, f)

qa = QApplication(sys.argv)
appmod.setup_app_style(qa)
qa.setFont(QFont(".AppleSystemUIFont", 13))
import i18n  # noqa: E402

i18n.set_language(LANG)
from main_window import MainWindow  # noqa: E402

win = MainWindow()
win.resize(WIN_W, WIN_H)
win.show()


def wait(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# ---- 帧采集 ----
frames: list[tuple[QPixmap, int]] = []   # (画面, 停留毫秒)
cursor = QPointF(WIN_W * 0.55, WIN_H * 0.6)
click_ring = 0.0   # >0 时画点击波纹


def _extra_popups() -> list[QWidget]:
    out = []
    for name in ('_ql_popup',):
        p = getattr(win, name, None)
        if p is not None and p.isVisible():
            out.append(p)
    pal = getattr(win, 'command_palette', None)
    if pal is not None and pal.popup.isVisible():
        out.append(pal.popup)
    return out


def _draw_cursor(p: QPainter, pos: QPointF):
    if click_ring > 0:
        r = 10 + 16 * (1 - click_ring)
        p.setPen(QPen(QColor(255, 255, 255, int(220 * click_ring)), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(pos, r, r)
    path = QPainterPath()
    x, y = pos.x(), pos.y()
    pts = [(0, 0), (0, 17), (4.2, 13.2), (7, 19.5), (9.6, 18.4), (6.9, 12.3), (12.4, 12.3)]
    path.moveTo(x + pts[0][0], y + pts[0][1])
    for dx, dy in pts[1:]:
        path.lineTo(x + dx, y + dy)
    path.closeSubpath()
    p.setPen(QPen(QColor('white'), 1.6))
    p.setBrush(QColor('black'))
    p.drawPath(path)


def snap(hold_ms: int = MOVE_MS, show_cursor: bool = True):
    QApplication.processEvents()
    pm = win.grab()
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    origin = win.mapToGlobal(QPoint(0, 0))
    for pop in _extra_popups():
        painter.drawPixmap(pop.mapToGlobal(QPoint(0, 0)) - origin, pop.grab())
    if show_cursor:
        _draw_cursor(painter, cursor)
    painter.end()
    frames.append((pm, hold_ms))


def hold(ms: int):
    snap(ms)


def center_of(w: QWidget) -> QPointF:
    c = w.mapTo(win, QPoint(w.width() // 2, w.height() // 2))
    return QPointF(c)


def move_to(target: QPointF):
    global cursor
    start = QPointF(cursor)
    for i in range(1, MOVE_FRAMES + 1):
        s = i / MOVE_FRAMES
        e = 1 - (1 - s) ** 3        # ease-out
        cursor = start + (target - start) * e
        snap(MOVE_MS)


def click(action=None):
    global click_ring
    for v in (1.0, 0.66, 0.33):
        click_ring = v
        snap(MOVE_MS)
    click_ring = 0.0
    if action:
        action()


def tour_card_next():
    return win._onboarding_tour.overlay.next_btn


def press_next(read_ms: int):
    """读卡片 → 光标移到「下一步」→ 点。"""
    hold(read_ms)
    move_to(center_of(tour_card_next()))
    click(lambda: tour_card_next().click())
    wait(250)


def type_text(edit, text: str):
    for ch in text:
        edit.insert(ch)
        wait(60)
        snap(110)


# ---- 剧本 ----
def run():
    wait(1200)
    win._start_session(cwd=DIRS[0])
    wait(1200)
    term = getattr(win, 'active_terminal', None)
    if term is not None:
        term._write_to_backend(b'clear; ls\r')
    wait(800)

    tour = win.start_onboarding_tour()
    wait(400)

    class _Targets:     # 与教程同一套定位规则：先查注册表再查属性
        def __getitem__(self, name):
            return tour._resolve_widget(name)
    tb = _Targets()

    seen = set()
    while tour.is_active():
        step = tour.current_step
        key = step.key
        print(f"step {tour.index + 1}: {key}  frames={len(frames)}", flush=True)
        if key in seen:     # 互动没生效、没自动前进：别死循环，直接点下一步
            tour.next()
            continue
        seen.add(key)
        if key == 'quick_launch':
            hold(2600)
            move_to(center_of(tb['quick_launch_btn']))
            click(lambda: tb['quick_launch_btn'].click())
            wait(300)
            hold(1800)          # 弹窗 + 打勾
            wait(700)
        elif key == 'new_tab':
            hold(2000)
            move_to(center_of(tb['new_tab_btn']))
            click(lambda: tb['new_tab_btn'].click())
            wait(300)
            hold(1300)
            wait(700)
        elif key == 'split':
            hold(2000)
            move_to(center_of(tb['split_btn']))
            click(lambda: tb['split_btn'].click())
            wait(400)
            hold(1300)
            wait(700)
        elif key == 'panels':
            hold(2200)
            move_to(center_of(tb['explorer_toggle_btn']))
            click(lambda: tb['explorer_toggle_btn'].click())
            wait(500)
            hold(1500)
            wait(700)
        elif key == 'palette':
            hold(2200)
            pal = win.command_palette
            move_to(center_of(pal))
            click(lambda: pal.focus_search())
            wait(300)
            type_text(pal.line_edit, 'split' if LANG == 'en' else '分屏')
            hold(1500)
            wait(700)
            pal.line_edit.clear()
            pal._hide_popup()
            win.setFocus()
        elif key == 'done':
            hold(2600)
            move_to(center_of(tour_card_next()))
            click(lambda: tour_card_next().click())
            wait(300)
            hold(1500)
        else:
            body = len(i18n.t(f"onboarding.{key}.body"))
            press_next(min(4200, 1800 + body * (22 if LANG == 'zh' else 9)))
        if not tour.is_active():
            break
    qa.quit()


QTimer.singleShot(0, run)
qa.exec()

# ---- 合成 GIF ----
from PIL import Image  # noqa: E402

imgs, durs = [], []
print('encoding', len(frames), 'frames', flush=True)
for pm, ms in frames:
    img = pm.toImage()
    path = os.path.join(DEMO, 'f.png')
    img.save(path)
    im = Image.open(path).convert('RGB')
    h = round(im.height * OUT_W / im.width)
    imgs.append(im.resize((OUT_W, h), Image.LANCZOS))
    durs.append(ms)
# 共享调色板：先用首帧+中间几帧量化出全局调色板，所有帧套它，避免闪色
sample = Image.new('RGB', (OUT_W, imgs[0].height * 3))
for i, k in enumerate((0, len(imgs) // 2, len(imgs) - 1)):
    sample.paste(imgs[k], (0, imgs[0].height * i))
pal_img = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
q = [im.quantize(palette=pal_img, dither=Image.Dither.NONE) for im in imgs]
q[0].save(OUT, save_all=True, append_images=q[1:], duration=durs, loop=0,
          optimize=True, disposal=1)
print(f"{OUT}: {len(q)} frames, {sum(durs)/1000:.1f}s, "
      f"{os.path.getsize(OUT)/1e6:.2f} MB")
shutil.rmtree(DEMO, ignore_errors=True)
shutil.rmtree(PROJECTS, ignore_errors=True)
os._exit(0)
