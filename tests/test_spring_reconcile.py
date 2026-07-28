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


if __name__ == '__main__':
    unittest.main()
