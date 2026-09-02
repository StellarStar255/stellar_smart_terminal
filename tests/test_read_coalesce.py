# -*- coding: utf-8 -*-
"""读取合并（按时间窗攒批）的回归测试。

审查发现：基类定义了 _READ_COALESCE_WINDOW / _READ_COALESCE_MAX，但只有 Unix
的 _read_loop 实现了攒批；Windows 的 _read_loop 每次 ReadFile 直接回调 →
远程高频输出下 Windows 仍是每秒上千次 on_output，正是攒批要解决的问题。

修复：攒批逻辑提到基类（coalesce_reads + TerminalBackend._pump_reads），
两个后端的读取循环只提供"等更多数据 / 再读一块"两个原语。这里用假时钟与
脚本化的原语直接验证攒批语义，不依赖平台。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_read_coalesce.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import terminal_backend
from terminal_backend import TerminalBackend, coalesce_reads


class _FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _Source:
    """脚本化的数据源：每次 wait_more 消耗一个 (是否有数据, 等待耗时) 条目，
    每次 read_more 弹出一块。"""

    def __init__(self, clock, chunks, waits):
        self.clock = clock
        self.chunks = list(chunks)
        self.waits = list(waits)
        self.wait_calls = []

    def wait_more(self, timeout):
        self.wait_calls.append(timeout)
        if not self.waits:
            return False
        ready, cost = self.waits.pop(0)
        self.clock.advance(min(cost, timeout) if not ready else cost)
        return ready

    def read_more(self):
        return self.chunks.pop(0) if self.chunks else b''


class TestCoalesceReads(unittest.TestCase):
    WINDOW = 0.008
    MAX = 64

    def _run(self, first, chunks, waits, running=lambda: True):
        clock = _FakeClock()
        src = _Source(clock, chunks, waits)
        out = coalesce_reads(first, src.wait_more, src.read_more,
                             window=self.WINDOW, max_bytes=self.MAX,
                             still_running=running, clock=clock)
        return out, src

    def test_chunks_inside_window_become_one_payload_in_order(self):
        out, src = self._run(b'a', [b'b', b'c'], [(True, 0.001), (True, 0.001), (False, 0.006)])
        self.assertEqual(out, b'abc')
        self.assertEqual(src.chunks, [], "窗口内的块都应被吸收")

    def test_window_expiry_stops_accumulating(self):
        # 第二次等待耗尽整个窗口且无数据 → 只交付已攒的
        out, src = self._run(b'a', [b'b', b'late'], [(True, 0.001), (False, 0.010)])
        self.assertEqual(out, b'ab')
        self.assertEqual(src.chunks, [b'late'], "窗口过期后不能再读")
        # 等待超时必须逐次缩短到剩余窗口，而不是每次都等满一个窗口
        self.assertTrue(all(t <= self.WINDOW + 1e-9 for t in src.wait_calls))
        self.assertLess(src.wait_calls[-1], self.WINDOW)

    def test_max_bytes_flushes_without_waiting(self):
        big = b'x' * self.MAX
        out, src = self._run(big, [b'y'], [(True, 0.0)])
        self.assertEqual(out, big, "首块已达上限：不等、不读")
        self.assertEqual(src.wait_calls, [])
        out2, src2 = self._run(b'a' * 40, [b'b' * 40, b'c'], [(True, 0.0), (True, 0.0)])
        self.assertEqual(out2, b'a' * 40 + b'b' * 40, "累计超上限后停止攒批")
        self.assertEqual(src2.chunks, [b'c'])

    def test_eof_mid_window_delivers_what_was_collected(self):
        out, src = self._run(b'a', [b'b', b''], [(True, 0.0), (True, 0.0), (True, 0.0)])
        self.assertEqual(out, b'ab')

    def test_stop_flag_ends_accumulation(self):
        flag = {'run': True}

        def running():
            r = flag['run']
            flag['run'] = False   # 第一次询问后即停止
            return r

        out, src = self._run(b'a', [b'b', b'c'], [(True, 0.0), (True, 0.0)], running=running)
        self.assertEqual(out, b'ab')


class _Probe(TerminalBackend):
    """最小具体后端：只为驱动基类的 _pump_reads。"""

    def start(self, command, cwd=None, cols=80, rows=24):
        return True

    def write(self, data):
        pass

    def resize(self, cols, rows):
        pass

    def stop(self):
        pass

    def is_running(self):
        return self._running


class TestPumpReads(unittest.TestCase):
    def _probe(self):
        p = _Probe()
        p._running = True
        p.on_output = lambda b: p.received.append(b)
        p.received = []
        return p

    def test_pump_batches_and_reports_eof(self):
        p = self._probe()
        clock = _FakeClock()
        src = _Source(clock, [b'2', b'3', b''], [(True, 0.0), (True, 0.0), (True, 0.0)])
        alive = p._pump_reads(lambda: b'1', src.wait_more, src.read_more, clock=clock)
        self.assertTrue(alive)
        self.assertEqual(p.received, [b'123'], "三块应合并为一次回调")
        src2 = _Source(clock, [], [])
        alive2 = p._pump_reads(lambda: b'', src2.wait_more, src2.read_more, clock=clock)
        self.assertFalse(alive2, "首读为空 = EOF")
        self.assertEqual(p.received, [b'123'])

    def test_pump_respects_running_flag(self):
        p = self._probe()
        clock = _FakeClock()
        src = _Source(clock, [b'b', b'c'], [(True, 0.0), (True, 0.0)])

        def first():
            p._running = False   # 读到首块后后端被 stop
            return b'a'

        p._pump_reads(first, src.wait_more, src.read_more, clock=clock)
        self.assertEqual(p.received, [b'a'])

    def test_class_constants_are_shared(self):
        """两个后端共用同一组常量（不再各写各的）"""
        self.assertEqual(_Probe._READ_COALESCE_WINDOW, TerminalBackend._READ_COALESCE_WINDOW)
        self.assertGreater(TerminalBackend._READ_COALESCE_MAX, 0)
        # 攒批实现只此一份：Unix 循环里不应再有内联的 deadline 逻辑
        src = open(terminal_backend.__file__, encoding='utf-8').read()
        self.assertEqual(src.count('_READ_COALESCE_WINDOW'), 2,
                         "常量应只在定义处和共用实现里出现")


if __name__ == '__main__':
    unittest.main()
