# -*- coding: utf-8 -*-
"""弹簧模式在外部重排后的自愈回归测试

背景（Ubuntu 实测）：从别的程序切回窗口时，changeEvent 的跨窗口左侧栏
对齐会走 _update_splitter_sizes → 按记忆/默认比例 setSizes，把弹簧展开
的编辑器打回窄宽度；而该重排不挪键盘焦点，点击已聚焦的编辑器不再发
focusChanged，弹簧无法自愈——表现为「切回来点编辑框反而更窄」。

守卫两道修复：
1. _update_splitter_sizes 在编辑器于 main_splitter 时，重排后调用
   _reconcile_spring_after_layout_change 无动画恢复弹簧比例；
2. _on_focus_changed_for_spring 在「目标侧与记录一致」时改按实际布局
   校验（_spring_actual_side），记录与实际脱节则重新展开自愈。

运行方式：
    python3 -m pytest tests/test_spring_reconcile.py -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class TestSpringReconcile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        global main_window
        import main_window
        # 共用一个 MainWindow：每用例新建/销毁完整主窗口的延迟销毁残留
        # 会在 CI 上段错误（与 test_update_restore_windows 同一约定）
        cls.w = main_window.MainWindow()
        cls.w.resize(1400, 900)
        cls.w.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtCore import QEvent
        cls.w.close()
        cls.w.deleteLater()
        del cls.w
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def setUp(self):
        w = self.w
        # 进入「左右分屏 + 弹簧」形态：编辑器停在 main_splitter 且可见
        w._explorer_split_horizontal = True
        if not (w.explorer_panel_visible or w.git_panel_visible
                or getattr(w, 'remote_panel_visible', False)):
            w.left_panel_container.show()
        w._place_editor_in_main_splitter()
        w.editor_area.show()
        w._spring_mode_enabled = True
        w._spring_width_gate = True
        self.app.processEvents()
        self.assertTrue(w._spring_applicable(),
                        "前置失败：弹簧应处于可生效状态")

    def _sizes(self):
        w = self.w
        sizes = w.main_splitter.sizes()
        ed = sizes[w.main_splitter.indexOf(w.editor_area)]
        tm = sizes[w.main_splitter.indexOf(w._main_content_stack)]
        return ed, tm

    def test_update_splitter_sizes_keeps_spring_side(self):
        w = self.w
        w._apply_spring('editor', animate=False)
        ed, tm = self._sizes()
        self.assertGreater(ed, tm, "前置失败：编辑器应已被弹宽")

        # 模拟切回窗口时的激活对齐 / 其它窗口广播（不挪焦点的整体重排）
        w._update_splitter_sizes()
        self.app.processEvents()

        ed2, tm2 = self._sizes()
        self.assertGreater(
            ed2, tm2,
            f"外部重排后编辑器被打回窄宽度且未自愈: editor={ed2}, terminal={tm2}")
        self.assertEqual(w._spring_actual_side(), 'editor')

    def test_focus_self_heals_when_record_desynced(self):
        w = self.w
        # 制造「记录 editor、实际 terminal 宽」的脱节：绕过 _apply_spring 直接 setSizes
        w._apply_spring('editor', animate=False)
        sizes = list(w.main_splitter.sizes())
        ed_idx = w.main_splitter.indexOf(w.editor_area)
        tm_idx = w.main_splitter.indexOf(w._main_content_stack)
        combined = sizes[ed_idx] + sizes[tm_idx]
        sizes[ed_idx], sizes[tm_idx] = combined // 4, combined - combined // 4
        w._applying_spring = True
        try:
            w.main_splitter.setSizes(sizes)
        finally:
            w._applying_spring = False
        self.assertEqual(w._spring_current_side, 'editor')
        self.assertEqual(w._spring_actual_side(), 'terminal',
                         "前置失败：应处于记录与实际脱节状态")

        # 焦点落回编辑器（旧逻辑因 target==记录 直接返回，卡在窄状态）
        w._on_focus_changed_for_spring(None, w.editor_area)
        # 消化可能的展开动画
        anim = w._spring_anim
        if anim is not None:
            anim.stop()
            w._apply_spring('editor', animate=False)
        self.app.processEvents()

        ed, tm = self._sizes()
        self.assertGreater(ed, tm, f"焦点自愈失败: editor={ed}, terminal={tm}")

    def test_narrow_combined_still_springs_visibly(self):
        """窄窗口回归：合计宽度低于 ~660px 时，220px 地板曾把 inactive 顶到
        与 active 相差无几（如 500px → 220/280），点击后布局几乎不动，
        表现为「多窗口很窄时 spring 失灵」。修复后应放弃地板、按纯比例
        分配，被点侧拿到 ~70%。"""
        w = self.w
        ed_idx = w.main_splitter.indexOf(w.editor_area)
        tm_idx = w.main_splitter.indexOf(w._main_content_stack)
        sizes = list(w.main_splitter.sizes())
        combined = sizes[ed_idx] + sizes[tm_idx]
        # 把编辑器+终端合计压到 500px（余量挪给第一个面板），模拟很窄的窗口
        squeeze = combined - 500
        self.assertGreater(squeeze, 0, "前置失败：测试窗口应宽于 500px")
        other_idx = next(i for i in range(len(sizes))
                         if i not in (ed_idx, tm_idx))
        sizes[other_idx] += squeeze
        sizes[ed_idx], sizes[tm_idx] = 250, 250
        w._applying_spring = True
        try:
            w.main_splitter.setSizes(sizes)
        finally:
            w._applying_spring = False
        self.app.processEvents()

        w._apply_spring('terminal', animate=False)
        ed, tm = self._sizes()
        self.assertGreater(
            tm, ed * 2,
            f"窄窗口下被点侧应占 ~70%: editor={ed}, terminal={tm}")


class TestSpringInnerPanes(unittest.TestCase):
    """分屏窗格级弹簧：点哪个窄窗格，哪个就变宽。

    旧实现只在「编辑器 ↔ 终端区」之间弹，终端内部 split 出来的多个窗格
    互相之间不会弹——多分几屏后每个都很窄，点了也没反应。
    现在判据改为窗格的**实际宽度**：窄于 SPRING_PANE_ENABLE 才弹，
    已经够宽就不动（所以重复点同一个不会抖）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        global main_window
        import main_window
        cls.w = main_window.MainWindow()
        cls.w.resize(1600, 900)
        cls.w.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtCore import QEvent
        cls.w.close()
        cls.w.deleteLater()
        del cls.w
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def setUp(self):
        self.w._spring_mode_enabled = True
        self._splitters = []

    def tearDown(self):
        for sp in self._splitters:
            sp.deleteLater()
        self.app.processEvents()

    def _hsplit(self, sizes):
        """造一个横向 splitter，按 sizes 分配等量的占位窗格。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QSplitter, QWidget
        sp = QSplitter(Qt.Orientation.Horizontal)
        panes = []
        for _ in sizes:
            p = QWidget()
            sp.addWidget(p)
            panes.append(p)
        sp.resize(sum(sizes), 600)
        sp.show()
        sp.setSizes(list(sizes))
        self.app.processEvents()
        self._splitters.append(sp)
        return sp, panes

    def _settle(self, sp):
        """快进内层弹簧动画到终点。"""
        anim = getattr(sp, '_spring_anim', None)
        if anim is not None:
            anim.setCurrentTime(anim.duration())
        self.app.processEvents()

    def test_clicked_narrow_pane_expands(self):
        sp, panes = self._hsplit([400, 400, 400])
        self.w._spring_expand_child(sp, panes[1])
        self._settle(sp)
        s = sp.sizes()
        self.assertGreater(s[1], s[0], f"被点窗格应最宽: {s}")
        self.assertGreater(s[1], s[2], f"被点窗格应最宽: {s}")

    def test_clicking_already_wide_pane_is_noop(self):
        """已经够宽就不动——否则每点一次都重排，视觉上是抖动。"""
        sp, panes = self._hsplit([1200, 200])
        before = sp.sizes()
        self.w._spring_expand_child(sp, panes[0])
        self._settle(sp)
        self.assertEqual(sp.sizes(), before)

    def test_focus_moves_expansion_to_other_pane(self):
        sp, panes = self._hsplit([400, 400, 400])
        self.w._spring_expand_child(sp, panes[1])
        self._settle(sp)
        self.w._spring_expand_child(sp, panes[2])
        self._settle(sp)
        s = sp.sizes()
        self.assertGreater(s[2], s[0], f"应换成第三个最宽: {s}")
        self.assertGreater(s[2], s[1], f"应换成第三个最宽: {s}")

    def test_narrow_total_width_still_visibly_springs(self):
        """整体很窄时，最小宽度地板会把两侧压得几乎等宽 —— 必须退化为纯比例。"""
        sp, panes = self._hsplit([250, 250])
        self.w._spring_expand_child(sp, panes[0])
        self._settle(sp)
        s = sp.sizes()
        self.assertGreater(s[0], s[1] * 2, f"窄窗口下被点侧应明显更宽: {s}")

    def test_disabled_spring_does_nothing(self):
        self.w._spring_mode_enabled = False
        sp, panes = self._hsplit([400, 400])
        before = sp.sizes()
        self.w._spring_expand_inner(panes[0])
        self.app.processEvents()
        self.assertEqual(sp.sizes(), before)

    def test_hidden_pane_excluded_from_allocation(self):
        """隐藏窗格不该分到宽度。"""
        sp, panes = self._hsplit([400, 400, 400])
        panes[2].hide()
        self.app.processEvents()
        self.w._spring_expand_child(sp, panes[0])
        self._settle(sp)
        s = sp.sizes()
        self.assertGreater(s[0], s[1], f"被点窗格应更宽: {s}")

    def test_sidebar_panels_never_participate(self):
        """左侧栏（窗口导航 / Explorer / Git / Remote）绝不能被弹簧改宽。

        这是 v1.16.8 引入的重大回归：内层弹簧当时用「排除 main_splitter」的
        黑名单，Git 面板内部本来就有横向 splitter，点侧栏就会把控制面板撑开、
        把终端挤没。改为白名单——只有终端区/编辑器区内部的 splitter 才参与。
        """
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QSplitter

        w = self.w
        # 直接在侧栏里放一个横向 splitter（等价于 Git 面板内部本来就有的那个，
        # 它在离屏测试里未必已构建）——策略必须把它挡住。
        from PyQt6.QtWidgets import QWidget
        sidebar_split = QSplitter(Qt.Orientation.Horizontal)
        sidebar_split.addWidget(QWidget())
        sidebar_split.addWidget(QWidget())
        w.left_panel_layout.addWidget(sidebar_split)
        self.app.processEvents()
        try:
            self.assertTrue(w.left_panel_container.isAncestorOf(sidebar_split),
                            "前置失败：构造的 splitter 应位于侧栏内")
            self.assertFalse(
                w._spring_allowed_splitter(sidebar_split),
                "左侧栏内的 splitter 不得参与弹簧（导航/Explorer/Git 会被撑开）")
            # main_splitter 也不走内层逻辑（左侧栏与编辑器/终端同属它）
            self.assertFalse(w._spring_allowed_splitter(w.main_splitter))

            # 端到端：点侧栏里的控件，该 splitter 尺寸不得变化
            sidebar_split.setSizes([200, 200])
            self.app.processEvents()
            before = sidebar_split.sizes()
            w._spring_expand_inner(sidebar_split.widget(0))
            self._settle(sidebar_split)
            self.assertEqual(sidebar_split.sizes(), before,
                             "点击侧栏控件不应改动侧栏布局")
        finally:
            sidebar_split.setParent(None)
            sidebar_split.deleteLater()
            self.app.processEvents()

    def test_clicking_sidebar_changes_no_layout(self):
        """点侧栏里的控件不应改动任何 splitter 尺寸。"""
        from PyQt6.QtWidgets import QSplitter, QTreeView

        w = self.w
        if not w.explorer_panel_visible:
            w._toggle_explorer_panel()
        self.app.processEvents()
        before = {id(sp): sp.sizes() for sp in w.findChildren(QSplitter)}
        for wdg in (w.explorer_panel.findChild(QTreeView),
                    getattr(w, 'nav_panel', None)):
            if wdg is not None:
                w._spring_expand_inner(wdg)
        self.app.processEvents()
        changed = [sp for sp in w.findChildren(QSplitter)
                   if before.get(id(sp)) != sp.sizes()]
        self.assertFalse(changed, "点击侧栏不应触发任何重排")

    def test_terminal_splits_still_allowed(self):
        """白名单不能误伤正常功能：终端区内部的分屏仍要参与弹簧。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QSplitter

        w = self.w
        w._split_current_tab()
        self.app.processEvents()
        try:
            inner = [sp for sp in w.findChildren(QSplitter)
                     if sp.orientation() == Qt.Orientation.Horizontal
                     and w._main_content_stack.isAncestorOf(sp)]
            self.assertTrue(inner, "前置失败：应存在终端分屏 splitter")
            self.assertTrue(any(w._spring_allowed_splitter(sp) for sp in inner),
                            "终端分屏应当仍能弹簧")
        finally:
            idx = w.tab_widget.currentIndex()
            terms = w.tab_terminals.get(idx, [])
            while len(terms) > 1:
                w._close_current_split()
                self.app.processEvents()
                terms = w.tab_terminals.get(idx, [])

    def test_vertical_splitter_untouched(self):
        """纵向分屏不参与横向弹簧（上下排列没有变窄的问题）。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QSplitter, QWidget
        sp = QSplitter(Qt.Orientation.Vertical)
        a, b = QWidget(), QWidget()
        sp.addWidget(a)
        sp.addWidget(b)
        sp.resize(800, 600)
        sp.show()
        sp.setSizes([300, 300])
        self.app.processEvents()
        self._splitters.append(sp)
        before = sp.sizes()
        self.w._spring_expand_inner(a)
        self.app.processEvents()
        self.assertEqual(sp.sizes(), before)


class TestSpringDeferWhileMouseDown(unittest.TestCase):
    """鼠标按住期间不得触发 spring 重排（松开后再执行）。

    用户报告：spring 弹开的瞬间容易「自己选上内容」——按下鼠标的同时窗格
    在指针下方移动/文本换行，文本控件把这段相对位移当成拖拽选择。修复为
    按住期间挂起重排、松开后执行。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window as mw
        cls.mw = mw
        cls.w = mw.MainWindow()
        cls.w.resize(1400, 900)
        cls.w.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtCore import QEvent
        cls.w.close()
        cls.w.deleteLater()
        del cls.w
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def setUp(self):
        w = self.w
        w._explorer_split_horizontal = True
        if not (w.explorer_panel_visible or w.git_panel_visible
                or getattr(w, 'remote_panel_visible', False)):
            w.left_panel_container.show()
        w._place_editor_in_main_splitter()
        w.editor_area.show()
        w._spring_mode_enabled = True
        w._spring_width_gate = True
        self.app.processEvents()
        self.assertTrue(w._spring_applicable(),
                        "前置失败：弹簧应处于可生效状态")
        self._orig_buttons = QApplication.mouseButtons

    def tearDown(self):
        QApplication.mouseButtons = self._orig_buttons

    def _hold_mouse(self, held: bool):
        from PyQt6.QtCore import Qt
        btn = Qt.MouseButton.LeftButton if held else Qt.MouseButton.NoButton
        QApplication.mouseButtons = staticmethod(lambda: btn)

    def _sizes(self):
        w = self.w
        sizes = w.main_splitter.sizes()
        ed = sizes[w.main_splitter.indexOf(w.editor_area)]
        tm = sizes[w.main_splitter.indexOf(w._main_content_stack)]
        return ed, tm

    def test_no_reflow_while_button_held_then_runs_on_release(self):
        w = self.w
        # 起始：终端侧展开，编辑器窄
        self._hold_mouse(False)
        w._apply_spring('terminal', animate=False)
        ed0, tm0 = self._sizes()
        self.assertGreater(tm0, ed0, "前置失败：终端应先被弹宽")

        # 鼠标按住期间请求展开编辑器 → 布局必须纹丝不动
        self._hold_mouse(True)
        w._apply_spring('editor', animate=False)
        self.app.processEvents()
        self.assertEqual(self._sizes(), (ed0, tm0),
                         "鼠标按住期间发生了 spring 重排（会造成误选内容）")

        # 松开后：挂起的重排应自动执行
        self._hold_mouse(False)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.app.processEvents()
            ed, tm = self._sizes()
            if ed > tm:
                break
            time.sleep(0.02)
        ed, tm = self._sizes()
        self.assertGreater(ed, tm, "松开鼠标后挂起的 spring 重排未执行")

    def test_press_during_animation_freezes_then_resumes(self):
        """v1.17.6 残余场景：松开后动画播放期间再次按下（双击/连点/点完即拖选），
        窗格仍在指针下移动 → 依旧误选。修复：任何按下立刻冻结动画，
        松开并安静 _SPRING_QUIET_MS 后续播至原目标。"""
        w = self.w
        self._hold_mouse(False)
        w._apply_spring('terminal', animate=False)
        ed0, _ = self._sizes()

        # 无按键 → 展开编辑器的动画立即开始播放
        w._apply_spring('editor', animate=True)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            self.app.processEvents()
            if self._sizes()[0] != ed0:
                break
            time.sleep(0.01)
        self.assertIsNotNone(w._spring_anim, "前置失败：动画应在进行中")

        # 动画中按下鼠标 → 布局立刻冻结
        self._hold_mouse(True)
        w._pause_spring_anims_for_press()
        frozen = self._sizes()
        for _ in range(8):
            self.app.processEvents()
            time.sleep(0.02)
        self.assertEqual(self._sizes(), frozen,
                         "按下后 spring 动画应立刻冻结（否则拖选仍会被带偏）")

        # 松开并安静 → 动画续播至完成
        self._hold_mouse(False)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            self.app.processEvents()
            ed, tm = self._sizes()
            if ed > tm and w._spring_anim is None:
                break
            time.sleep(0.02)
        ed, tm = self._sizes()
        self.assertGreater(ed, tm, "松开鼠标后动画应续播至原目标")

    def test_latest_request_wins_within_one_hold(self):
        w = self.w
        self._hold_mouse(False)
        w._apply_spring('terminal', animate=False)

        self._hold_mouse(True)
        w._apply_spring('editor', animate=False)
        w._apply_spring('terminal', animate=False)   # 后到的意图覆盖先到的
        self._hold_mouse(False)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not getattr(w, '_pending_spring_reflows', None):
                break
            time.sleep(0.02)
        ed, tm = self._sizes()
        self.assertGreater(tm, ed, "同一次按住内应只执行最后一次请求")


if __name__ == '__main__':
    unittest.main()
