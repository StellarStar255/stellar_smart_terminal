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


if __name__ == '__main__':
    unittest.main()
