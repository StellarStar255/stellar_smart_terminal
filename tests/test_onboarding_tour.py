"""首次启动互动教程（onboarding_tour）。

覆盖：首启判断只看标记；步骤定位 + 隐藏目标跳过；互动步骤条件满足后自动前进；
遮罩在目标处开洞、卡片区域不被 mask 裁掉；退出/完成写 onboarding_shown；
帮助菜单有入口；中英文案对每一步都齐全。
"""
import unittest

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtWidgets import QApplication

import app_config
from i18n import TRANSLATIONS, t, set_language, get_language
import onboarding_tour
from onboarding_tour import (
    build_steps, _Baseline, should_show_onboarding, CONFIG_KEY,
)


def _pump(n=5):
    for _ in range(n):
        QApplication.processEvents()


class ShouldShowTest(unittest.TestCase):
    def test_flag_decides_not_config_existence(self):
        self.assertTrue(should_show_onboarding({}))
        self.assertTrue(should_show_onboarding({'other': 1}))
        self.assertFalse(should_show_onboarding({CONFIG_KEY: True}))
        self.assertTrue(should_show_onboarding({CONFIG_KEY: False}))
        self.assertTrue(should_show_onboarding(None))  # 损坏配置按首启处理


class I18nCoverageTest(unittest.TestCase):
    def test_every_step_has_bilingual_texts(self):
        for step in build_steps(_Baseline()):
            for suffix in ('title', 'body') + (('hint',) if step.interactive else ()):
                key = f"onboarding.{step.key}.{suffix}"
                self.assertIn(key, TRANSLATIONS, key)
                self.assertTrue(TRANSLATIONS[key]['zh'].strip(), key)
                self.assertTrue(TRANSLATIONS[key]['en'].strip(), key)
        for key in ('onboarding.menu_item', 'onboarding.counter', 'onboarding.next',
                    'onboarding.prev', 'onboarding.skip_step', 'onboarding.skip_tour',
                    'onboarding.finish', 'onboarding.hint_done'):
            self.assertIn(key, TRANSLATIONS, key)
        self.assertEqual(t('onboarding.counter', current=2, total=9).count('2'), 1)


class TourOnMainWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window
        cls.win = main_window.MainWindow()
        cls.win.resize(1400, 900)
        cls.win.show()
        _pump()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtGui import QCloseEvent
        for w in [w for w in QApplication.topLevelWidgets()
                  if isinstance(w, cls.mw.MainWindow) and not sip.isdeleted(w)]:
            QApplication.sendEvent(w, QCloseEvent())
        _pump()

    def setUp(self):
        self._written = []
        self._orig_update = app_config.update_config
        onboarding_tour.app_config.update_config = lambda patch, **kw: self._written.append(patch) or True
        old = getattr(self.win, '_onboarding_tour', None)
        if old is not None and old.is_active():
            old.skip()
        _pump()

    def tearDown(self):
        onboarding_tour.app_config.update_config = self._orig_update
        tour = getattr(self.win, '_onboarding_tour', None)
        if tour is not None and tour.is_active():
            tour.skip()
        _pump()

    def _start(self):
        tour = self.win.start_onboarding_tour()
        _pump()
        return tour

    # ----- 基本流程 -----

    def test_start_shows_overlay_covering_window_and_centered_welcome(self):
        tour = self._start()
        ov = tour.overlay
        self.assertTrue(ov.isVisible())
        self.assertEqual(ov.geometry(), self.win.rect())
        self.assertEqual(tour.current_step.key, 'welcome')
        self.assertIsNone(ov.hole())
        card = ov.card.geometry()
        self.assertLess(abs(card.center().x() - ov.width() // 2), 4)

    def test_hole_matches_target_and_card_outside_hole(self):
        tour = self._start()
        tour.next()  # dir
        tour.next()  # quick_launch
        _pump()
        self.assertEqual(tour.current_step.key, 'quick_launch')
        btn = self.win.quick_launch_btn
        expect = QRect(btn.mapTo(self.win, QPoint(0, 0)), btn.size())
        hole = tour.overlay.hole()
        self.assertTrue(hole.contains(expect), (hole, expect))
        self.assertFalse(hole.intersects(tour.overlay.card.geometry()))
        # mask 在目标处是空的（点击穿透），在卡片处是实的
        mask = tour.overlay.mask()
        self.assertFalse(mask.contains(expect.center()))
        self.assertTrue(mask.contains(tour.overlay.card.geometry().center()))

    def test_big_target_puts_card_inside_hole_and_keeps_it_in_mask(self):
        tour = self._start()
        idx = [s.key for s in tour.steps].index('terminal')
        tour._goto(idx)
        _pump()
        self.assertEqual(tour.current_step.key, 'terminal')
        card = tour.overlay.card.geometry()
        self.assertTrue(tour.overlay.hole().contains(card))
        self.assertTrue(tour.overlay.mask().contains(card.center()))

    def test_hidden_targets_skip_step(self):
        btn = self.win.quick_launch_btn
        btn.hide()
        try:
            tour = self._start()
            tour.next()
            tour.next()
            _pump()
            self.assertNotEqual(tour.current_step.key, 'quick_launch')
            self.assertEqual(tour.current_step.key, 'new_tab')
        finally:
            btn.show()

    def test_card_tall_enough_for_long_wrapped_body(self):
        """正文很长（英文右键菜单那步）时卡片高度要按换行后的实际高度算，不能被截字。"""
        tour = self._start()
        ov = tour.overlay
        long_body = ' '.join(['Quick Commands run any preset in one click;'] * 14)
        ov.set_content(counter='Step 1 of 2', title='Title', body=long_body, hint='hint',
                       can_prev=True, next_text='Next', skip_text='Exit', prev_text='Back')
        ov.set_hole(None)
        _pump()
        need = ov.card.layout().totalHeightForWidth(ov.card.width())
        self.assertGreaterEqual(ov.card.height(), need)
        body = ov.body_label
        self.assertGreaterEqual(body.height(), body.heightForWidth(body.width()))

    # ----- 互动 -----

    def test_quick_launch_step_advances_when_popup_opens(self):
        tour = self._start()
        tour.ADVANCE_DELAY_MS = 1
        tour.next()
        tour.next()
        self.assertEqual(tour.current_step.key, 'quick_launch')
        self.assertEqual(tour.overlay.next_btn.text(), t('onboarding.skip_step'))
        self.win._show_quick_launch_menu()
        _pump()
        tour._tick()
        self.assertTrue(tour._done_flag)
        self.assertEqual(tour.overlay.hint_label.text(), t('onboarding.hint_done'))
        tour._advance_after_done()
        _pump()
        self.assertEqual(tour.current_step.key, 'new_tab')
        # 离开时把弹窗收掉
        self.assertFalse(self.win._ql_popup.isVisible())

    def test_new_tab_step_advances_after_adding_tab(self):
        tour = self._start()
        idx = [s.key for s in tour.steps].index('new_tab')
        tour._goto(idx)
        before = self.win.tab_widget.count()
        tour._tick()
        self.assertFalse(tour._done_flag)
        self.win._add_new_tab()
        _pump()
        self.assertEqual(self.win.tab_widget.count(), before + 1)
        tour._tick()
        self.assertTrue(tour._done_flag)

    def test_context_menu_step_advances_when_window_menu_pops_up(self):
        from PyQt6.QtWidgets import QMenu
        tour = self._start()
        keys = [s.key for s in tour.steps]
        self.assertEqual(keys.index('context_menu'), keys.index('terminal') + 1)
        tour._goto(keys.index('context_menu'))
        tour._tick()
        self.assertFalse(tour._done_flag)
        # 别的窗口/无主的菜单不算
        stray = QMenu()
        stray.addAction('x')
        stray.popup(QPoint(10, 10))
        _pump()
        tour._tick()
        self.assertFalse(tour._done_flag)
        stray.close()
        # 终端右键菜单以主窗口为 parent
        menu = QMenu(self.win)
        menu.addAction('x')
        menu.popup(self.win.mapToGlobal(QPoint(200, 300)))
        _pump()
        try:
            tour._tick()
            self.assertTrue(tour._done_flag)
        finally:
            menu.close()
            _pump()

    # ----- 收尾 -----

    def test_skip_persists_flag_and_removes_overlay(self):
        tour = self._start()
        ov = tour.overlay
        tour.skip()
        _pump()
        self.assertFalse(tour.is_active())
        self.assertIn({CONFIG_KEY: True}, self._written)
        self.assertTrue(sip.isdeleted(ov) or not ov.isVisible())
        self.assertIsNone(self.win._onboarding_tour)

    def test_finish_via_next_on_last_step(self):
        tour = self._start()
        for _ in range(len(tour.steps) + 2):
            if not tour.is_active():
                break
            tour.next()
        self.assertFalse(tour.is_active())
        self.assertIn({CONFIG_KEY: True}, self._written)

    def test_restart_replaces_active_tour(self):
        first = self._start()
        second = self._start()
        self.assertFalse(first.is_active())
        self.assertTrue(second.is_active())
        self.assertIs(self.win._onboarding_tour, second)

    # ----- 接线 -----

    def test_help_menu_has_tutorial_action(self):
        texts = [a.text() for a in self.win._help_menu.actions()]
        self.assertIn(t('onboarding.menu_item'), texts)

    def test_language_switch_retranslates_card(self):
        tour = self._start()
        lang = get_language()
        try:
            set_language('en' if lang == 'zh' else 'zh')
            self.win._apply_language()
            self.assertEqual(tour.overlay.title_label.text(), t('onboarding.welcome.title'))
        finally:
            set_language(lang)
            self.win._apply_language()

    def test_schedule_uses_window_parented_timer(self):
        self.win.schedule_onboarding_tour(60_000)
        timer = self.win._onboarding_timer
        self.assertIs(timer.parent(), self.win)
        self.assertTrue(timer.isActive())
        timer.stop()


if __name__ == '__main__':
    unittest.main()
