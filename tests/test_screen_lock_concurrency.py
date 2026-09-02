"""屏幕锁并发压力测试（增量 1-3 的回归网）。

模拟 PARSE_ON_READER_THREAD 开启后的真实场景：feed 在读取线程跑，GUI 线程同时
读屏幕（render/选择/搜索走的那些 helper）。验证 _screen_lock 下：
- 不崩（迭代/索引活的 pyte 结构时不会因并发 mutate 抛异常）；
- 不死锁（可重入锁 + 持锁不跨线程等待）；
- 最终状态一致（喂进去的内容都在）。

为检测并发问题（非吞吐基准），读取线程做有界工作并主动让出 GIL。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_screen_lock_concurrency -v
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestScreenLockConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @unittest.skipIf(os.environ.get('STELLAR_PARSE_OFF_GUI') is not None,
                     "env override forces the flag explicitly")
    def test_default_flag_on(self):
        """生产默认走读取线程解析（2026-07 并发审计 + 压测后翻转，
        根治多窗口 tmux 卡顿）。误改回 False 会让卡顿回归。"""
        from terminal_widget import TerminalWidget
        self.assertTrue(TerminalWidget.PARSE_ON_READER_THREAD)

    def test_concurrent_feed_and_reads(self):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        N = 800
        errors = []
        stop = threading.Event()

        def feeder():
            try:
                for i in range(N):
                    # 直接调用 _on_output(在工作线程) = 读取线程解析的等价场景。
                    # 避免查询序列(\x1b[6n 等)以免触碰 None backend。
                    w._on_output(f"line {i:05d} hello world\r\n".encode())
            except Exception as e:  # noqa: BLE001
                errors.append(("feeder", repr(e)))
            finally:
                stop.set()

        def reader(tag):
            try:
                it = 0
                while not stop.is_set():
                    it += 1
                    hc = w._get_history_count()
                    top = w._get_history_top()
                    self.assertIsInstance(top, list)  # 快照而非活引用
                    if hc > 0:
                        w._get_line_text(0)
                        w._get_line_text(hc - 1)
                    if it % 100 == 0:
                        # 偶尔做一次重的全选内容提取（迭代历史快照 + 活缓冲）
                        w._select_all_mode = True
                        w._get_selected_text()
                        w._select_all_mode = False
                    # 必须真让出：读取侧每圈都持锁整拷历史（O(N)），sleep(0) 在 macOS 上
                    # 不换线程且 RLock 不公平，feeder 会被饿死几十秒（join 超时误报死锁）
                    time.sleep(0.002)
            except Exception as e:  # noqa: BLE001
                errors.append((tag, repr(e)))

        threads = [
            threading.Thread(target=feeder, name="feeder"),
            threading.Thread(target=reader, args=("reader1",), name="reader1"),
            threading.Thread(target=reader, args=("reader2",), name="reader2"),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertFalse([t.name for t in threads if t.is_alive()],
                         "线程未在 20s 内结束 —— 疑似死锁")
        self.assertEqual(errors, [], f"并发访问出现异常: {errors}")

        # 最终一致性：喂进去的行数应进入历史（屏幕 30 行，N 远大于此）
        self.assertGreater(w._get_history_count(), N - w.term_rows - 5)
        self.assertIn("line", '\n'.join(w.screen.display))

    def test_resize_during_feed(self):
        """resize/reflow（持锁 mutate）与并发读取不冲突、不崩。"""
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        errors = []
        stop = threading.Event()

        def feeder():
            try:
                for i in range(600):
                    w._on_output((f"row {i} " + "x" * 50 + "\r\n").encode())
                    if i % 50 == 0:
                        time.sleep(0)
            except Exception as e:  # noqa: BLE001
                errors.append(("feeder", repr(e)))
            finally:
                stop.set()

        def reflower():
            try:
                cols = [80, 100, 60, 120]
                k = 0
                while not stop.is_set():
                    w.term_cols = cols[k % len(cols)]
                    k += 1
                    with w._screen_lock:
                        w.screen.resize(w.term_rows, w.term_cols)
                    # 同上：每次 resize 是持锁的 O(历史) reflow，必须真让出
                    time.sleep(0.005)
            except Exception as e:  # noqa: BLE001
                errors.append(("reflower", repr(e)))

        threads = [threading.Thread(target=feeder), threading.Thread(target=reflower)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertFalse(any(t.is_alive() for t in threads), "疑似死锁")
        self.assertEqual(errors, [], f"resize 并发异常: {errors}")


if __name__ == '__main__':
    unittest.main()
