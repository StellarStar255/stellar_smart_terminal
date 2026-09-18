"""explorer_common 共享逻辑测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_common.py -v

resolve_paste_conflict 原来在本地/远程两个 explorer 里逐字复制，收敛后
在此单点测试三选一语义 + sticky 短路。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeButton:
    def __init__(self, role):
        self.role = role


class _FakeCheckBox:
    def __init__(self, checked=False):
        self._checked = checked

    def isChecked(self):
        return self._checked


class _FakeMsgBox:
    """记录 addButton 顺序，exec 时返回预设点击。"""
    _click = None          # 'keep' / 'overwrite' / 'skip' / 'cancel' / None
    _apply_all = False
    last_default = None    # 最近一次 setDefaultButton 对应的动作名

    def __init__(self, parent=None):
        self._buttons = {}
        self._checkbox = None

    def setWindowTitle(self, *a): pass
    def setText(self, *a): pass
    def setIcon(self, *a): pass

    def setDefaultButton(self, btn):
        self.default_btn = btn
        _FakeMsgBox.last_default = next(
            (n for n, b in self._buttons.items() if b is btn), None)

    def addButton(self, text, role):
        btn = _FakeButton(role)
        # 用 role 的字符串区分 keep/skip/overwrite/cancel
        name = {'AcceptRole': 'keep', 'ActionRole': 'skip',
                'DestructiveRole': 'overwrite',
                'RejectRole': 'cancel'}[role.name if hasattr(role, 'name') else str(role)]
        self._buttons[name] = btn
        return btn

    def setCheckBox(self, cb):
        self._checkbox = cb

    def exec(self):
        if self._checkbox is not None:
            self._checkbox._checked = _FakeMsgBox._apply_all

    def clickedButton(self):
        if _FakeMsgBox._click is None:
            return None
        return self._buttons.get(_FakeMsgBox._click)


class TestResolvePasteConflict(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import explorer_common
        from PyQt6.QtWidgets import QMessageBox
        self.mod = explorer_common
        self._orig_box = explorer_common.QMessageBox
        self._orig_cb = explorer_common.QCheckBox
        # fake 复用真枚举，让 QMessageBox.Icon.* / ButtonRole.* 引用正常解析
        _FakeMsgBox.Icon = QMessageBox.Icon
        _FakeMsgBox.ButtonRole = QMessageBox.ButtonRole
        explorer_common.QMessageBox = _FakeMsgBox
        explorer_common.QCheckBox = lambda *a, **k: _FakeCheckBox()
        _FakeMsgBox._click = None
        _FakeMsgBox._apply_all = False
        _FakeMsgBox.last_default = None
        # 「记住上次选择」是模块级状态 + 配置落盘：每个用例从干净状态开始，
        # 且不真写配置文件
        explorer_common._last_conflict_action = None
        self._cfg_writes = []
        import app_config
        self._orig_update = app_config.update_config
        self._orig_read = app_config.read_config
        app_config.update_config = lambda patch, **k: self._cfg_writes.append(patch) or True
        app_config.read_config = lambda: {}

    def tearDown(self):
        self.mod.QMessageBox = self._orig_box
        self.mod.QCheckBox = self._orig_cb
        import app_config
        app_config.update_config = self._orig_update
        app_config.read_config = self._orig_read
        explorer_common = self.mod
        explorer_common._last_conflict_action = None

    def test_sticky_shortcircuits_without_dialog(self):
        # sticky 已定 → 不弹窗，直接复用，且 apply-all=True
        _FakeMsgBox._click = 'cancel'  # 即便设了点击也不该走对话框
        self.assertEqual(self.mod.resolve_paste_conflict(None, 'a.txt', 'overwrite'),
                         ('overwrite', True))
        self.assertEqual(self.mod.resolve_paste_conflict(None, 'a.txt', 'keep'),
                         ('keep', True))

    def test_dialog_overwrite(self):
        _FakeMsgBox._click = 'overwrite'
        self.assertEqual(self.mod.resolve_paste_conflict(None, 'a.txt', None),
                         ('overwrite', False))

    def test_dialog_keep_with_apply_all(self):
        _FakeMsgBox._click = 'keep'
        _FakeMsgBox._apply_all = True
        self.assertEqual(self.mod.resolve_paste_conflict(None, 'a.txt', None),
                         ('keep', True))

    def test_sticky_skip_shortcircuits(self):
        _FakeMsgBox._click = 'cancel'
        self.assertEqual(self.mod.resolve_paste_conflict(None, 'a.txt', 'skip'),
                         ('skip', True))

    def test_dialog_skip(self):
        """用户要求：冲突时可以选「跳过」——不覆盖、不加尾缀、也不中止剩余。"""
        _FakeMsgBox._click = 'skip'
        self.assertEqual(self.mod.resolve_paste_conflict(None, 'a.txt', None),
                         ('skip', False))
        _FakeMsgBox._apply_all = True
        self.assertEqual(self.mod.resolve_paste_conflict(None, 'a.txt', None),
                         ('skip', True))

    # ---- 记住上次选择：默认按钮跟着用户上次的动作走 ----

    def test_default_button_is_keep_on_first_use(self):
        _FakeMsgBox._click = 'keep'
        self.mod.resolve_paste_conflict(None, 'a.txt', None)
        self.assertEqual(_FakeMsgBox.last_default, 'keep')

    def test_default_button_follows_last_choice(self):
        for choice in ('skip', 'overwrite', 'keep'):
            _FakeMsgBox._click = choice
            self.mod.resolve_paste_conflict(None, 'a.txt', None)
            _FakeMsgBox._click = 'cancel'
            self.mod.resolve_paste_conflict(None, 'b.txt', None)
            self.assertEqual(_FakeMsgBox.last_default, choice,
                             f"上次选了 {choice}，这次默认按钮应是它")

    def test_cancel_never_becomes_default(self):
        _FakeMsgBox._click = 'skip'
        self.mod.resolve_paste_conflict(None, 'a.txt', None)
        _FakeMsgBox._click = 'cancel'
        self.mod.resolve_paste_conflict(None, 'b.txt', None)
        _FakeMsgBox._click = None      # 直接关掉对话框
        self.mod.resolve_paste_conflict(None, 'c.txt', None)
        _FakeMsgBox._click = 'keep'
        self.mod.resolve_paste_conflict(None, 'd.txt', None)
        self.assertEqual(_FakeMsgBox.last_default, 'skip')

    def test_last_choice_is_persisted_and_restored(self):
        import app_config
        _FakeMsgBox._click = 'overwrite'
        self.mod.resolve_paste_conflict(None, 'a.txt', None)
        self.assertIn({'paste_conflict_default': 'overwrite'}, self._cfg_writes)
        # 新进程：模块状态为空，从配置读回
        self.mod._last_conflict_action = None
        app_config.read_config = lambda: {'paste_conflict_default': 'skip'}
        _FakeMsgBox._click = 'keep'
        self.mod.resolve_paste_conflict(None, 'b.txt', None)
        self.assertEqual(_FakeMsgBox.last_default, 'skip')

    def test_garbage_persisted_value_falls_back_to_keep(self):
        import app_config
        app_config.read_config = lambda: {'paste_conflict_default': 'cancel'}
        _FakeMsgBox._click = 'keep'
        self.mod.resolve_paste_conflict(None, 'a.txt', None)
        self.assertEqual(_FakeMsgBox.last_default, 'keep')

    def test_dialog_cancel_returns_none(self):
        _FakeMsgBox._click = 'cancel'
        self.assertIsNone(self.mod.resolve_paste_conflict(None, 'a.txt', None))

    def test_dialog_closed_returns_none(self):
        _FakeMsgBox._click = None  # 直接关掉对话框
        self.assertIsNone(self.mod.resolve_paste_conflict(None, 'a.txt', None))

    def test_both_explorers_delegate_here(self):
        """两个 explorer 的 _resolve_paste_conflict 都应委托到共享实现"""
        import explorer_widget
        import remote_explorer_widget
        import inspect
        for cls_mod, cls_name in (
            (explorer_widget, 'ExplorerPanel'),
            (remote_explorer_widget, 'RemoteExplorerPanel'),
        ):
            cls = getattr(cls_mod, cls_name)
            src = inspect.getsource(cls._resolve_paste_conflict)
            self.assertIn('explorer_common.resolve_paste_conflict', src,
                          f"{cls_name} 未委托到共享实现")


if __name__ == '__main__':
    unittest.main()
