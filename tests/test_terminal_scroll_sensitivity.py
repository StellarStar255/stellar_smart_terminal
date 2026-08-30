"""终端滚轮/触控板滚动量（不依赖真实 PTY）：

用户报告 macOS 触控板在终端里"太灵敏"。根因：以前不看事件幅度，每个 wheel
事件一律算 1.5 行 —— 鼠标滚轮一格一个事件没问题，但触控板一次轻扫会发出
几十个高分辨率小事件，于是轻轻一划就窜出去几十行。

这里锁住修好后的语义：
- 滚动量按事件自身幅度换算（触控板按像素/行高，滚轮按角度/120 格）
- 一次触控板轻扫的总行数 ≈ 手指划过的像素 / 行高，而不是"事件个数 × 1.5"
- 灵敏度倍数可调、可持久化，并且被卡在合理区间
- 备用屏幕转发给 TUI（vim/less/Claude Code）时攒够一行才发一格滚轮

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_terminal_scroll_sensitivity.py -q
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeWheel:
    """够 _wheel_lines / wheelEvent 用的假滚轮事件。"""

    def __init__(self, *, pixels=0, degrees=0, phase=None):
        from PyQt6.QtCore import QPoint, QPointF, Qt
        self._pixels = QPoint(0, pixels)
        self._degrees = QPoint(0, degrees)
        self._phase = phase if phase is not None else Qt.ScrollPhase.NoScrollPhase
        self._pos = QPointF(10.0, 10.0)
        self.accepted = False

    def pixelDelta(self):
        return self._pixels

    def angleDelta(self):
        return self._degrees

    def phase(self):
        return self._phase

    def position(self):
        return self._pos

    def accept(self):
        self.accepted = True


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app_config
        from terminal_widget import TerminalWidget
        self._tmp_cfg = Path(tempfile.mkdtemp()) / "cfg.json"
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._tmp_cfg
        TerminalWidget._scroll_sensitivity_cache = None

    def tearDown(self):
        import app_config
        from terminal_widget import TerminalWidget
        app_config.get_config_path = self._orig_get_path
        TerminalWidget._scroll_sensitivity_cache = None

    def _widget(self):
        """不真起 PTY：只造一个够 _wheel_lines / wheelEvent 用的壳。"""
        from terminal_widget import TerminalWidget
        w = TerminalWidget.__new__(TerminalWidget)
        w.char_height = 16.0
        w._scroll_accum = 0.0
        w._app_wheel_accum = 0.0
        w.scroll_offset = 0
        w._mouse_mode = False
        return w


class TestWheelLines(_Base):
    def test_trackpad_scrolls_by_pixels_not_by_event_count(self):
        """触控板：滚多少行取决于手指划过的像素，不是事件个数。"""
        w = self._widget()
        # 一次轻扫 = 40 个 4px 的小事件，共 160px = 10 行（行高 16）
        total = sum(w._wheel_lines(_FakeWheel(pixels=4)) for _ in range(40))
        self.assertAlmostEqual(total, 10.0, places=3)
        # 旧实现是 40 × 1.5 = 60 行 —— 就是"太灵敏"的由来
        self.assertLess(total, 20.0)

    def test_mouse_wheel_notch_keeps_its_old_feel(self):
        """鼠标滚轮一格仍是 1.5 行，手感不变。"""
        w = self._widget()
        self.assertAlmostEqual(w._wheel_lines(_FakeWheel(degrees=120)), 1.5)
        self.assertAlmostEqual(w._wheel_lines(_FakeWheel(degrees=-120)), -1.5)
        self.assertAlmostEqual(w._wheel_lines(_FakeWheel(degrees=240)), 3.0)

    def test_direction_is_preserved(self):
        w = self._widget()
        self.assertLess(w._wheel_lines(_FakeWheel(pixels=-32)), 0)
        self.assertGreater(w._wheel_lines(_FakeWheel(pixels=32)), 0)

    def test_empty_event_scrolls_nothing(self):
        self.assertEqual(self._widget()._wheel_lines(_FakeWheel()), 0.0)

    def test_sensitivity_scales_both_devices(self):
        from terminal_widget import TerminalWidget
        w = self._widget()
        TerminalWidget.set_scroll_sensitivity(0.5)
        self.assertAlmostEqual(w._wheel_lines(_FakeWheel(pixels=32)), 1.0)
        self.assertAlmostEqual(w._wheel_lines(_FakeWheel(degrees=120)), 0.75)
        TerminalWidget.set_scroll_sensitivity(2.0)
        self.assertAlmostEqual(w._wheel_lines(_FakeWheel(pixels=32)), 4.0)

    def test_zero_char_height_does_not_explode(self):
        """字体量测还没跑完时 char_height 可能是 0 —— 不能除零。"""
        w = self._widget()
        w.char_height = 0
        self.assertNotEqual(w._wheel_lines(_FakeWheel(pixels=32)), 0.0)


class TestSensitivityPersistence(_Base):
    def test_round_trip_and_clamped(self):
        from terminal_widget import TerminalWidget
        TerminalWidget.set_scroll_sensitivity(1.5)
        TerminalWidget._scroll_sensitivity_cache = None      # 强制重读配置
        self.assertAlmostEqual(TerminalWidget.scroll_sensitivity(), 1.5)
        # 手改配置成 0/负数也不能滚不动或反着滚
        TerminalWidget.set_scroll_sensitivity(0)
        self.assertGreaterEqual(TerminalWidget.scroll_sensitivity(), 0.1)
        TerminalWidget.set_scroll_sensitivity(999)
        self.assertLessEqual(TerminalWidget.scroll_sensitivity(), 4.0)

    def test_default_is_one_when_unset(self):
        from terminal_widget import TerminalWidget
        self.assertAlmostEqual(TerminalWidget.scroll_sensitivity(), 1.0)

    def test_corrupt_config_value_falls_back(self):
        import app_config
        from terminal_widget import TerminalWidget
        app_config.update_config(
            {TerminalWidget.CONFIG_KEY_SCROLL_SENSITIVITY: "not a number"})
        TerminalWidget._scroll_sensitivity_cache = None
        self.assertAlmostEqual(TerminalWidget.scroll_sensitivity(), 1.0)


class TestLocalScrollback(_Base):
    """本地回滚：滚动的行数应与手指划过的距离成比例。"""

    def _scroll(self, w, events):
        from terminal_widget import TerminalWidget
        with mock.patch.object(TerminalWidget, '_get_history_count',
                               lambda self_: 10_000), \
                mock.patch.object(TerminalWidget, '_invalidate_render_cache',
                                  lambda self_: None):
            for ev in events:
                TerminalWidget.wheelEvent(w, ev)
        return w.scroll_offset

    def test_gentle_swipe_moves_a_few_lines(self):
        w = self._widget()
        # 10 个 8px 事件 = 80px = 5 行
        offset = self._scroll(w, [_FakeWheel(pixels=8) for _ in range(10)])
        self.assertEqual(offset, 5)

    def test_fractional_pixels_accumulate_instead_of_being_lost(self):
        """不足一行的滚动要攒起来，而不是每次取整丢掉。"""
        w = self._widget()
        offset = self._scroll(w, [_FakeWheel(pixels=4) for _ in range(8)])
        self.assertEqual(offset, 2)      # 32px = 2 行

    def test_wheel_notches_still_scroll(self):
        w = self._widget()
        offset = self._scroll(w, [_FakeWheel(degrees=120) for _ in range(4)])
        self.assertEqual(offset, 6)      # 4 格 × 1.5 行


class TestAltScreenForwarding(_Base):
    """备用屏幕（vim/less/Claude Code）：攒够一行才发一格滚轮。"""

    def _forward(self, events, sensitivity=1.0):
        from terminal_widget import TerminalWidget
        w = self._widget()
        w._mouse_mode = True
        w.screen = type('S', (), {'_in_alt_screen': True})()
        TerminalWidget.set_scroll_sensitivity(sensitivity)
        sent = []
        with mock.patch.object(TerminalWidget, '_send_wheel_to_app',
                               lambda self_, up, pos, n: sent.append((up, n))):
            for ev in events:
                TerminalWidget.wheelEvent(w, ev)
        return sent

    def test_tiny_trackpad_events_do_not_each_send_a_notch(self):
        """一次轻扫 160px（10 行）应该发 10 格，而不是 40 格。"""
        sent = self._forward([_FakeWheel(pixels=4) for _ in range(40)])
        self.assertEqual(sum(n for _up, n in sent), 10)
        self.assertTrue(all(up for up, _n in sent))

    def test_wheel_notch_forwards_immediately(self):
        sent = self._forward([_FakeWheel(degrees=120)])
        self.assertEqual(sent, [(True, 1)])

    def test_direction_change_drops_leftover(self):
        """换方向时上一方向攒的余量必须作废，否则会抢跑一格。"""
        from terminal_widget import TerminalWidget
        w = self._widget()
        w._mouse_mode = True
        w.screen = type('S', (), {'_in_alt_screen': True})()
        sent = []
        with mock.patch.object(TerminalWidget, '_send_wheel_to_app',
                               lambda self_, up, pos, n: sent.append((up, n))):
            TerminalWidget.wheelEvent(w, _FakeWheel(pixels=12))   # 0.75 行，不发
            self.assertEqual(sent, [])
            TerminalWidget.wheelEvent(w, _FakeWheel(pixels=-12))  # 反向 0.75 行
            self.assertEqual(sent, [], '反向后不该立刻凑出一格')


if __name__ == '__main__':
    unittest.main()
