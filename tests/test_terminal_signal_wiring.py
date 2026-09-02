# -*- coding: utf-8 -*-
"""终端信号接线表的回归测试（跨窗口接管必须与新建终端走同一张表）。

审查发现：`_create_terminal` 连接 16 个终端信号，而标签页被拖到另一窗口时
`_add_new_tab` 的 external 分支自己维护一份"断开列表"和"重连列表"，两份
列表与 `_create_terminal` 不一致：

- 断开列表漏了 move_split_left/up_requested、scrollback_pressure_changed、
  alert_matched → 旧窗口仍在监听。按"分屏左移"时旧窗口的 `_move_split_left`
  也会执行一次，用旧窗口自己的 active_terminal 去交换旧窗口里的分屏（串台）。
- 重连列表漏了 alert_matched → 接管后输出提醒只剩旧窗口在听，旧窗口一关就
  彻底失效。
- 断开列表整段共用一个 try/except：第一个 disconnect() 抛 TypeError（该信号
  恰好没有连接）时其余信号全部跳过。

修复：接线/拆线收敛为 `_wire_terminal_signals` / `_unwire_terminal_signals`，
两处共用同一张表；本测试直接走真实的 external 分支验证每个信号都只剩一个
接收者，且接管方能收到 alert_matched。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_terminal_signal_wiring.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import QApplication, QSplitter


# _create_terminal 里连接的全部终端信号；表必须与实现保持一致，缺一个就是
# 又一处"接管后串台"的入口。
EXPECTED_SIGNALS = (
    'input_recorded', 'output_recorded', 'session_ended', 'image_pasted',
    'close_tab_requested', 'new_tab_requested',
    'manage_presets_requested', 'add_command_requested',
    'manage_local_presets_requested', 'add_local_command_requested',
    'close_split_requested', 'split_horizontal_requested',
    'split_vertical_requested', 'move_split_left_requested',
    'move_split_up_requested', 'rename_split_requested',
    'attention_requested', 'interaction_requested',
    'alert_matched', 'scrollback_pressure_changed',
)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw_mod = main_window
        cls.win_a = main_window.MainWindow()
        cls.win_b = main_window.MainWindow()

    @classmethod
    def tearDownClass(cls):
        for w in (cls.win_a, cls.win_b):
            w.close()
            w.deleteLater()
        del cls.win_a, cls.win_b
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def _adopt_via_real_path(self, term):
        """按真实的拖出/接管路径把 A 的终端交给 B。"""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(term)
        self.win_b._add_new_tab(
            external_splitter=splitter,
            external_terminals=[term],
            external_session=None,
            tab_name='adopted',
        )


class TestSignalTableCoverage(_Base):
    def test_every_terminal_signal_has_exactly_one_receiver_after_adopt(self):
        """接管后每个信号只能剩下接管方这一个接收者（旧窗口必须被拆干净）"""
        term = self.win_a._create_terminal()
        for name in EXPECTED_SIGNALS:
            self.assertEqual(
                term.receivers(getattr(term, name)), 1,
                f"{name}: 新建终端应恰好连接到所属窗口一次")

        self._adopt_via_real_path(term)

        for name in EXPECTED_SIGNALS:
            self.assertEqual(
                term.receivers(getattr(term, name)), 1,
                f"{name}: 接管后仍有旧窗口的连接残留（或接管方漏接）")


class TestAlertRewired(_Base):
    def test_alert_matched_reaches_new_owner(self):
        """输出提醒在接管后必须投递给新窗口，而不是仍留在旧窗口"""
        term = self.win_a._create_terminal()
        hits = {'a': [], 'b': []}
        self.win_a._on_terminal_alert = lambda t, pat: hits['a'].append(pat)
        self.win_b._on_terminal_alert = lambda t, pat: hits['b'].append(pat)

        self._adopt_via_real_path(term)
        term.alert_matched.emit('ERROR')

        self.assertEqual(hits['b'], ['ERROR'], "接管方没有收到 alert_matched")
        self.assertEqual(hits['a'], [], "旧窗口仍在接收已转移终端的 alert_matched")


class TestUnwireIsPerSignal(_Base):
    def test_unwire_tolerates_already_disconnected_signal(self):
        """某个信号已无连接时，拆线不能因 TypeError 半途而废"""
        term = self.win_a._create_terminal()
        # 人为先断开第一个信号，模拟"部分信号已无连接"的状态
        term.input_recorded.disconnect()
        self.win_a._unwire_terminal_signals(term)
        for name in EXPECTED_SIGNALS:
            self.assertEqual(
                term.receivers(getattr(term, name)), 0,
                f"{name}: 拆线在前面的信号抛错后中断了")


if __name__ == '__main__':
    unittest.main()
