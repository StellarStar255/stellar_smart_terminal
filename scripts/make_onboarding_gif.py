"""把新手教程 / 终端右键菜单录成 GIF，给 README 用。

跑真实 MainWindow（cocoa 平台，渲染与用户所见一致），按剧本自动操作：
模拟光标移到目标 → 点击 → 真的触发对应动作，逐帧 window.grab()；
顶层弹层（⚡ 列表、命令面板结果、右键菜单及其子菜单）按位置合成进画面。

用法：
    python scripts/make_onboarding_gif.py zh assets/onboarding-zh.gif          # 新手教程
    python scripts/make_onboarding_gif.py en assets/onboarding-en.gif
    python scripts/make_onboarding_gif.py zh assets/context-menu-zh.gif menu   # 右键菜单
    python scripts/make_onboarding_gif.py en assets/context-menu-en.gif menu

剧本是生成器，由一个 5ms 的 QTimer 推进（yield 毫秒数 = 等多久）。这样右键菜单
menu.exec() 的嵌套事件循环里剧本照样往下走，可以在菜单里悬停、点子菜单。

数据目录/工作目录/shell 提示符都指向一次性演示环境，不会露出本机路径和用户名。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LANG = sys.argv[1] if len(sys.argv) > 1 else 'zh'
OUT = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else f'onboarding-{LANG}.gif')
MODE = sys.argv[3] if len(sys.argv) > 3 else 'tour'
ZH = LANG == 'zh'
WIN_W, WIN_H = (1280, 800) if MODE == 'tour' else (1280, 840)
OUT_W = 1000          # GIF 输出宽度（README 里够清楚）
MOVE_FRAMES = 14      # 光标一次移动的帧数
MOVE_MS = 30
# 停留时长系数：教程 GIF 放 README，整段压到 45 秒左右（光标移动不压，只压卡片停留）
PACE = 0.6 if MODE == 'tour' else 1.0

# ---- 演示环境：数据目录、演示项目（带 git 历史和本地快速命令）、干净的 zsh 提示符 ----
DEMO = tempfile.mkdtemp(prefix='stellar-demo-')
os.environ['STELLAR_DATA_DIR'] = os.path.join(DEMO, 'data')
os.makedirs(os.environ['STELLAR_DATA_DIR'])
PROJECTS = '/tmp/stellar-demo'
shutil.rmtree(PROJECTS, ignore_errors=True)
DIRS = [os.path.join(PROJECTS, n) for n in ('my-project', 'api-server', 'blog', 'dotfiles')]
for d in DIRS:
    os.makedirs(d, exist_ok=True)
PROJ = DIRS[0]
_FILES = {
    'README.md': '# my-project\n',
    'main.py': 'from src.app import run\n\n# TODO: parse CLI args\nrun()\n',
    'src/app.py': 'def run():\n    # TODO: load config from env\n    print("hello")\n',
    'src/utils.py': 'def slugify(s):\n    return s.lower().replace(" ", "-")\n',
    'tests/test_app.py': 'def test_run():\n    # TODO: real assertions\n    assert True\n',
    'requirements.txt': 'requests\n',
}
for rel, body in _FILES.items():
    os.makedirs(os.path.dirname(os.path.join(PROJ, rel)) or PROJ, exist_ok=True)
    with open(os.path.join(PROJ, rel), 'w') as f:
        f.write(body)
os.makedirs(os.path.join(PROJ, '.sterminal'), exist_ok=True)
with open(os.path.join(PROJ, '.sterminal', 'quick_commands.json'), 'w') as f:
    json.dump({'presets': [
        {'name': '查找 TODO' if ZH else 'Find TODOs', 'commands': ["grep -rn TODO --include='*.py' ."]},
        {'name': '统计代码行数' if ZH else 'Count lines', 'commands': ['wc -l *.py src/*.py tests/*.py']},
    ]}, f, ensure_ascii=False)
_git_env = dict(os.environ, GIT_AUTHOR_NAME='Demo', GIT_AUTHOR_EMAIL='demo@example.com',
                GIT_COMMITTER_NAME='Demo', GIT_COMMITTER_EMAIL='demo@example.com',
                GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')


def _git(*args):
    subprocess.run(['git', '-c', 'init.defaultBranch=main', *args], cwd=PROJ, env=_git_env,
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_git('init')
for msg, paths in (('init project', ['README.md', 'requirements.txt', '.sterminal']),
                   ('add app skeleton', ['main.py', 'src']),
                   ('add tests', ['tests'])):
    _git('add', *paths)
    _git('commit', '-m', msg)
with open(os.path.join(PROJ, 'src/utils.py'), 'a') as f:
    f.write('\n\ndef title(s):\n    return s.title()\n')

zdot = os.path.join(DEMO, 'zdot')
os.makedirs(zdot)
with open(os.path.join(zdot, '.zshrc'), 'w') as f:
    f.write("PROMPT='%F{cyan}%1~%f %F{green}❯%f '\nunsetopt PROMPT_SP\n")
os.environ['ZDOTDIR'] = zdot
os.environ['GIT_CONFIG_GLOBAL'] = '/dev/null'
os.environ['GIT_PAGER'] = 'cat'
os.environ.pop('PS1', None)
os.chdir(PROJ)

import app as appmod  # noqa: E402

appmod.setup_qt_plugin_path()

from PyQt6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QColor, QContextMenuEvent, QFont, QPainter, QPainterPath, QPen, QPixmap,
)
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QWidget  # noqa: E402

import app_config  # noqa: E402

with open(app_config.get_config_path(), 'w') as f:
    json.dump({
        'onboarding_shown': True, 'language': LANG,
        'working_dir_history': DIRS,
        'working_dir_freq': {d: 10 - i for i, d in enumerate(DIRS)},
        'presets': [
            {'name': 'zsh', 'commands': ['zsh']},
            {'name': 'Git 状态' if ZH else 'Git status',
             'commands': ['git status -s', 'git log --oneline -3']},
            {'name': 'Claude Code', 'commands': ['zsh', 'claude']},
        ],
    }, f, ensure_ascii=False)

qa = QApplication(sys.argv)
appmod.setup_app_style(qa)
qa.setFont(QFont(".AppleSystemUIFont", 13))
import i18n  # noqa: E402

i18n.set_language(LANG)
from main_window import MainWindow  # noqa: E402

win = MainWindow()
win.resize(WIN_W, WIN_H)
win.show()


def _activate():
    """把本进程拉到前台：命令面板那步按"输入框有焦点"判完成，窗口必须是活动窗口。
    从终端后台拉起的进程 activateWindow() 不一定生效，走 AppKit 强制激活。"""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass
    win.raise_()
    win.activateWindow()


_activate()

# ---- 帧采集 ----
frames: list[tuple[QPixmap, int]] = []   # (画面, 停留毫秒)
cursor = QPointF(WIN_W * 0.55, WIN_H * 0.6)
click_ring = 0.0     # >0 时画点击波纹
caption = ''         # 底部字幕（右键菜单演示用）


def _extra_popups() -> list[QWidget]:
    out = []
    p = getattr(win, '_ql_popup', None)
    if p is not None and p.isVisible():
        out.append(p)
    pal = getattr(win, 'command_palette', None)
    if pal is not None and pal.popup.isVisible():
        out.append(pal.popup)
    # 右键菜单及子菜单：父菜单先画、子菜单叠在上面（子菜单总在父菜单右侧）
    menus = [w for w in QApplication.topLevelWidgets()
             if isinstance(w, QMenu) and w.isVisible()]
    out += sorted(menus, key=lambda m: m.mapToGlobal(QPoint(0, 0)).x())
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


def _draw_caption(p: QPainter, text: str):
    font = QFont(".AppleSystemUIFont", 17)
    font.setWeight(QFont.Weight.DemiBold)
    p.setFont(font)
    fm = p.fontMetrics()
    w = fm.horizontalAdvance(text) + 44
    h = fm.height() + 20
    rect = QRectF(WIN_W - w - 28, WIN_H - h - 60, w, h)   # 右下角：菜单在左侧弹出，互不遮挡
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(124, 92, 255, 235))
    p.drawRoundedRect(rect, h / 2, h / 2)
    p.setPen(QColor('white'))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def snap(hold_ms: int = MOVE_MS):
    QApplication.processEvents()
    pm = win.grab()
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    origin = win.mapToGlobal(QPoint(0, 0))
    for pop in _extra_popups():
        painter.drawPixmap(pop.mapToGlobal(QPoint(0, 0)) - origin, pop.grab())
    if caption:
        _draw_caption(painter, caption)
    _draw_cursor(painter, cursor)
    painter.end()
    frames.append((pm, hold_ms))


def hold(ms: int):
    """停留 ms（按 PACE 缩放）：拍一帧并把这段时间让给事件循环（输出、动画照常进行）。"""
    ms = int(ms * PACE)
    snap(ms)
    yield ms


def film(ms: int, step: int = 250):
    """持续拍摄 ms（画面会变，比如命令输出正在刷）。"""
    t = 0
    while t < ms:
        snap(step)
        yield step
        t += step


def center_of(w: QWidget) -> QPointF:
    c = w.mapTo(win, QPoint(w.width() // 2, w.height() // 2))
    return QPointF(c)


def to_win(w: QWidget, local: QPoint) -> QPointF:
    return QPointF(win.mapFromGlobal(w.mapToGlobal(local)))


def move_to(target: QPointF):
    global cursor
    start = QPointF(cursor)
    for i in range(1, MOVE_FRAMES + 1):
        s = i / MOVE_FRAMES
        e = 1 - (1 - s) ** 3        # ease-out
        cursor = start + (target - start) * e
        snap(MOVE_MS)
        yield MOVE_MS


def click(action=None):
    global click_ring
    for v in (1.0, 0.66, 0.33):
        click_ring = v
        snap(MOVE_MS)
        yield MOVE_MS
    click_ring = 0.0
    if action:
        action()


def type_text(edit, text: str):
    for ch in text:
        edit.insert(ch)
        yield 60
        snap(110)


# ---- 右键菜单操作 ----

def open_context_menu(term, local: QPoint):
    """在终端 local 处右键。菜单 exec() 是阻塞的，挂到下一轮事件里开，剧本继续推进。"""
    yield from move_to(to_win(term, local))
    yield from click()
    gp = term.mapToGlobal(local)
    QTimer.singleShot(0, lambda: term.contextMenuEvent(
        QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local, gp)))
    yield 250
    snap(MOVE_MS)
    yield MOVE_MS


def visible_menus() -> list[QMenu]:
    return sorted([w for w in QApplication.topLevelWidgets()
                   if isinstance(w, QMenu) and w.isVisible()],
                  key=lambda m: m.mapToGlobal(QPoint(0, 0)).x())


def find_action(menu: QMenu, text: str):
    for a in menu.actions():
        if a.text().startswith(text):
            return a
    raise LookupError(f"menu has no {text!r}: {[a.text() for a in menu.actions()]}")


def hover_item(menu: QMenu, text: str):
    act = find_action(menu, text)
    pos = menu.actionGeometry(act).center()
    yield from move_to(to_win(menu, pos))
    QTest.mouseMove(menu, pos)
    menu.setActiveAction(act)
    yield 60
    snap(MOVE_MS)
    yield MOVE_MS
    return act


def open_submenu(menu: QMenu, text: str):
    act = yield from hover_item(menu, text)
    QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(act).center())
    yield 350
    sub = act.menu()
    if not sub.isVisible():     # 合成点击没展开子菜单时，按原生位置手动弹出
        sub.popup(menu.mapToGlobal(menu.actionGeometry(act).topRight()))
        yield 200
    snap(MOVE_MS)
    yield MOVE_MS
    return act.menu()


def click_item(menu: QMenu, text: str):
    act = yield from hover_item(menu, text)
    yield from click()
    for m in visible_menus():
        m.close()
    act.trigger()
    yield 200


def close_menus():
    for m in visible_menus():
        m.close()
    yield 150


def T(zh: str, en: str) -> str:
    return zh if ZH else en


# ---- 剧本：新手教程 ----

def tour_script():
    yield 1200
    win._start_session(cwd=PROJ)
    yield 1200
    term = win.active_terminal
    term._write_to_backend(b'clear; ls\r')
    yield 800

    tour = win.start_onboarding_tour()
    yield 400

    class _Targets:     # 与教程同一套定位规则：先查注册表再查属性
        def __getitem__(self, name):
            return tour._resolve_widget(name)
    tb = _Targets()

    def next_btn():
        return tour.overlay.next_btn

    def press_next(read_ms: int):
        yield from hold(read_ms)
        yield from move_to(center_of(next_btn()))
        yield from click(lambda: next_btn().click())
        yield 250

    def interact(read_ms, target, action, settle=300, after=1300):
        yield from hold(read_ms)
        yield from move_to(center_of(target))
        yield from click(action)
        yield settle
        yield from hold(after)       # 效果 + 卡片打勾
        yield 700                    # 等教程自动前进

    seen = set()
    while tour.is_active():
        key = tour.current_step.key
        print(f"step {tour.index + 1}: {key}  frames={len(frames)}", flush=True)
        if key in seen:     # 互动没生效、没自动前进：别死循环，直接点下一步
            tour.next()
            continue
        seen.add(key)
        if key == 'quick_launch':
            btn = tb['quick_launch_btn']
            yield from interact(2600, btn, btn.click, after=1800)
        elif key == 'new_tab':
            btn = tb['new_tab_btn']
            yield from interact(2000, btn, btn.click)
        elif key == 'split':
            btn = tb['split_btn']
            yield from interact(2000, btn, btn.click, settle=400)
        elif key == 'panels':
            btn = tb['explorer_toggle_btn']
            yield from interact(2200, btn, btn.click, settle=500, after=1500)
        elif key == 'palette':
            yield from hold(2200)
            _activate()
            yield 200
            pal = win.command_palette
            yield from move_to(center_of(pal))
            yield from click(pal.focus_search)
            yield 300
            yield from type_text(pal.line_edit, T('分屏', 'split'))
            if not tour._done_flag:
                # 后台拉起的进程抢不到前台（macOS 不让），输入框拿不到真焦点，完成条件
                # 永远不满足。录像里照教程自己满足条件时的路径打勾、自动前进
                tour._done_flag = True
                tour._render()
                tour._advancing = True
                tour._advance_timer.start(tour.ADVANCE_DELAY_MS)
            yield from hold(1500)
            yield 700
            pal.line_edit.clear()
            pal._hide_popup()
            win.setFocus()
        elif key == 'context_menu':
            yield from hold(4200)
            term = win.active_terminal
            yield from open_context_menu(term, QPoint(term.width() - 260, 12))
            yield from hold(2600)        # 菜单开着，卡片已打勾
            yield from close_menus()
            yield 900
        elif key == 'done':
            yield from hold(2600)
            yield from move_to(center_of(next_btn()))
            yield from click(lambda: next_btn().click())
            yield 300
            yield from hold(1500)
        else:
            body = len(i18n.t(f"onboarding.{key}.body"))
            yield from press_next(min(4200, 1800 + body * (22 if ZH else 9)))


# ---- 剧本：终端右键菜单 ----

def menu_script():
    global caption
    yield 1200
    win._start_session(cwd=PROJ)
    yield 1500
    term = win.active_terminal
    term._write_to_backend(b'clear; ls\r')
    yield 900
    spot = QPoint(150, 8)     # 贴近终端顶部右键，整张菜单都落在窗口里

    # 1. 右键看全貌
    caption = T('在终端里点右键，常用功能都在这', 'Right-click in the terminal for handy tools')
    yield from hold(1200)
    yield from open_context_menu(term, spot)
    yield from hold(1800)
    menu = visible_menus()[0]

    # 2. 快速命令：一键执行预设
    caption = T('快速命令：一键执行任意预设', 'Quick Commands: run any preset in one click')
    sub = yield from open_submenu(menu, i18n.t('ctx.quick_commands'))
    yield from hold(900)
    yield from click_item(sub, T('Git 状态', 'Git status'))
    yield from film(1800)

    # 3. 本地快速命令：跟着项目目录走
    caption = T('本地快速命令：存在项目里，换目录自动切换',
                'Local Quick Commands: saved per project, switch with the folder')
    yield from open_context_menu(term, spot + QPoint(0, 4))
    menu = visible_menus()[0]
    sub = yield from open_submenu(menu, i18n.t('ctx.local_quick_commands'))
    yield from hold(1100)
    yield from click_item(sub, T('查找 TODO', 'Find TODOs'))
    yield from film(1800)

    # 4. 搜索回滚历史
    caption = T('搜索：整个回滚历史里找', 'Search: find anything in the scrollback')
    yield from open_context_menu(term, spot)
    yield from click_item(visible_menus()[0], i18n.t('ctx.search'))
    yield 300
    bar = term._search_bar
    from PyQt6.QtWidgets import QLineEdit
    edit = bar.findChild(QLineEdit)
    yield from move_to(center_of(edit))
    yield from type_text(edit, 'TODO')
    yield 500
    yield from hold(2200)
    term._hide_search_bar()
    yield 200

    # 5. 复制当前路径
    caption = T('复制当前路径 / 在访达中打开当前目录',
                'Copy the current path / open it in Finder')
    yield from open_context_menu(term, spot + QPoint(0, 4))
    yield from click_item(visible_menus()[0], i18n.t('ctx.copy_current_dir'))
    yield 200
    # 真粘贴会先走 osascript 查剪贴板图片（慢），录像里直接把剪贴板文本敲进去，效果一样
    path_text = QApplication.clipboard().text()
    term._write_to_backend(b'echo ')
    yield from film(500)
    term._write_to_backend(f"'{path_text}'".encode())
    yield from film(900)
    term._write_to_backend(b'\r')
    yield from film(1200)

    # 6. 分屏
    caption = T('分屏、移动分屏、重命名分屏', 'Split, move and rename panes')
    yield from open_context_menu(term, spot)
    yield from click_item(visible_menus()[0], i18n.t('ctx.split_vertical'))
    yield 600
    yield from film(1600)

    # 7. 滚动灵敏度
    caption = T('还能单独调滚动灵敏度', 'Tune the scroll speed per terminal')
    term2 = win.active_terminal
    yield from open_context_menu(term2, QPoint(150, 8))
    menu = visible_menus()[0]
    yield from open_submenu(menu, i18n.t('ctx.scroll_sensitivity'))
    yield from hold(1800)
    yield from close_menus()
    caption = ''
    yield from hold(1200)


# ---- 推进器 ----

def drive(gen):
    state = {'due': 0.0, 'busy': False}
    timer = QTimer()
    timer.setInterval(5)

    def tick():
        if state['busy'] or time.monotonic() < state['due']:
            return
        state['busy'] = True
        try:
            ms = next(gen)
            state['due'] = time.monotonic() + (ms or 0) / 1000
        except StopIteration:
            timer.stop()
            for m in visible_menus():
                m.close()
            # exit 而非 quit：Qt6 的 quit() 会先逐个关窗，主窗口 closeEvent 可以拒绝，进程就挂住
            QTimer.singleShot(0, lambda: qa.exit(0))
        except Exception:
            import traceback
            traceback.print_exc()
            timer.stop()
            for m in visible_menus():
                m.close()
            QTimer.singleShot(0, lambda: qa.exit(1))
        finally:
            state['busy'] = False

    timer.timeout.connect(tick)
    timer.start()
    return timer


_clip_backup = QApplication.clipboard().text()   # 演示会用「复制当前路径」，结束后还原
_driver = drive(tour_script() if MODE == 'tour' else menu_script())
rc = qa.exec()
QApplication.clipboard().setText(_clip_backup)
QApplication.processEvents()
if rc:
    os._exit(rc)

# ---- 合成 GIF ----
from PIL import Image  # noqa: E402

imgs, durs = [], []
print('encoding', len(frames), 'frames', flush=True)
path = os.path.join(DEMO, 'f.png')
for pm, ms in frames:
    pm.toImage().save(path)
    im = Image.open(path).convert('RGB')
    h = round(im.height * OUT_W / im.width)
    imgs.append(im.resize((OUT_W, h), Image.LANCZOS))
    durs.append(ms)
# 共享调色板：均匀抽若干帧量化出全局调色板，所有帧套它，避免闪色
picks = imgs[::max(1, len(imgs) // 12)]
sample = Image.new('RGB', (OUT_W, imgs[0].height * len(picks)))
for i, im in enumerate(picks):
    sample.paste(im, (0, imgs[0].height * i))
pal_img = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
q = [im.quantize(palette=pal_img, dither=Image.Dither.NONE) for im in imgs]
q[0].save(OUT, save_all=True, append_images=q[1:], duration=durs, loop=0,
          optimize=True, disposal=1)
print(f"{OUT}: {len(q)} frames, {sum(durs)/1000:.1f}s, "
      f"{os.path.getsize(OUT)/1e6:.2f} MB")
shutil.rmtree(DEMO, ignore_errors=True)
shutil.rmtree(PROJECTS, ignore_errors=True)
os._exit(0)
