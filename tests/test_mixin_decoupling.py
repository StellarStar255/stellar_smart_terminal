# -*- coding: utf-8 -*-
"""main_window 各 mixin 解耦的回归测试。

审查发现拆分是 cosmetic split：六个 mixin 顶层 `import main_window as _mw`
（循环 import）去摸 MainWindow 的类属性 / 构造新窗口；同一份实例状态在多处
用 `hasattr` 兜底给默认值（如 `_explorer_split_horizontal` 的默认值散落三份）。

修复：mixin 经 `window_host.host_class(self)` 取宿主类，不再 import main_window；
每个 mixin 的实例状态由 `_init_<mixin>_state()` 显式初始化、且只在那里给默认值。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_mixin_decoupling.py -v
"""
import glob
import inspect
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

MIXIN_FILES = sorted(
    f for f in glob.glob(os.path.join(ROOT, "main_window_*.py")))


class TestNoCircularImport(unittest.TestCase):
    def test_mixin_sources_do_not_import_main_window(self):
        self.assertTrue(MIXIN_FILES)
        for path in MIXIN_FILES:
            src = open(path, encoding="utf-8").read()
            hits = re.findall(r"^\s*(?:import main_window\b|from main_window import)",
                              src, flags=re.M)
            self.assertEqual(
                hits, [], f"{os.path.basename(path)} 仍然 import main_window（循环 import）")

    def test_each_mixin_imports_standalone(self):
        """单独 import 任一 mixin 模块不应连带把 main_window 拉进来"""
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONPATH=ROOT)
        for path in MIXIN_FILES:
            mod = os.path.basename(path)[:-3]
            code = (f"import sys, {mod}; "
                    f"sys.exit(0 if 'main_window' not in sys.modules else 3)")
            r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(
                r.returncode, 0,
                f"import {mod} 失败或连带导入了 main_window: rc={r.returncode}\n"
                f"{r.stderr[-800:]}")


class _WindowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw_mod = main_window
        cls.MW = main_window.MainWindow

    def _new_window(self, **kw):
        win = self.MW(**kw)
        self.addCleanup(self._dispose, win)
        return win

    def _dispose(self, win):
        try:
            QApplication.sendEvent(win, QCloseEvent())
        finally:
            win.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


def _state_attrs_by_init(MW):
    """{init 方法名: [该方法里 `self._x = ...` 的属性名]}"""
    out = {}
    for name, fn in inspect.getmembers(MW, predicate=inspect.isfunction):
        if not re.fullmatch(r"_init_\w+_state", name):
            continue
        src = inspect.getsource(fn)
        attrs = re.findall(r"^\s*self\.(\w+)\s*=", src, flags=re.M)
        out[name] = list(dict.fromkeys(attrs))
    return out


class TestExplicitStateInit(_WindowBase):
    def test_init_methods_exist_and_cover_mixins(self):
        inits = _state_attrs_by_init(self.MW)
        for expected in ("_init_config_state", "_init_explorer_state",
                         "_init_remote_state", "_init_tabs_state",
                         "_init_theme_state", "_init_toolbar_state",
                         "_init_update_state"):
            self.assertIn(expected, inits, f"缺少 {expected}")
            self.assertTrue(inits[expected], f"{expected} 没有初始化任何属性")

    def test_every_state_attr_exists_after_construction(self):
        win = self._new_window()
        inits = _state_attrs_by_init(self.MW)
        missing = [f"{m}:{a}" for m, attrs in inits.items()
                   for a in attrs if not hasattr(win, a)]
        self.assertEqual(missing, [], "构造后仍缺少 _init_*_state 声明的属性")

    def test_each_state_attr_has_exactly_one_default(self):
        inits = _state_attrs_by_init(self.MW)
        seen = {}
        for m, attrs in inits.items():
            for a in attrs:
                seen.setdefault(a, []).append(m)
        dups = {a: ms for a, ms in seen.items() if len(ms) > 1}
        self.assertEqual(dups, {}, "同一状态在多个 _init_*_state 里给了默认值")

    def test_no_hasattr_fallback_for_split_prefs(self):
        """曾经三处给默认值的 _explorer_split_horizontal / _remote_split_horizontal"""
        for path in MIXIN_FILES:
            src = open(path, encoding="utf-8").read()
            for attr in ("_explorer_split_horizontal", "_remote_split_horizontal"):
                self.assertNotRegex(
                    src, rf"hasattr\(self, '{attr}'\)|getattr\(self, '{attr}'",
                    f"{os.path.basename(path)} 仍用 hasattr/getattr 兜底 {attr}")


class TestSharedClassState(_WindowBase):
    def test_window_counter_lives_on_the_class(self):
        """mixin 递增窗口计数必须写在宿主类上，不能变成实例影子属性"""
        from window_host import host_class
        win = self._new_window()
        self.assertIs(host_class(win), self.MW)
        before = self.MW._window_counter
        # 走 mixin 里真实的递增语句（与 _detach_tab / _open_ssh_in_new_window 同款）
        host_class(win)._window_counter += 1
        self.assertEqual(self.MW._window_counter, before + 1)
        self.assertNotIn('_window_counter', win.__dict__)

    def test_host_class_survives_subclassing(self):
        """子类实例也要落到定义共享类属性的那个类上（type(self) 会写成影子属性）"""
        from window_host import host_class

        class Host:
            _window_counter = 0

        class Sub(Host):
            pass

        self.assertIs(host_class(Sub()), Host)
        host_class(Sub())._window_counter += 1
        self.assertEqual(Host._window_counter, 1)
        self.assertNotIn('_window_counter', Sub.__dict__)

        class Fake:  # 单元测试里的假 self：没有标记属性就回退到自身类型
            pass
        self.assertIs(host_class(Fake()), Fake)


class TestDeadCodeRemoved(unittest.TestCase):
    def test_detached_window_gone(self):
        import widgets
        self.assertFalse(hasattr(widgets, "DetachedWindow"),
                         "widgets.DetachedWindow 无任何调用者，应已删除")


if __name__ == '__main__':
    unittest.main()
