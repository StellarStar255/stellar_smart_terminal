"""PARSE_ON_READER_THREAD 并发压测

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_parse_reader_thread.py -v

模拟"解析在读取线程"场景：一个后台线程持续调用 _on_output（等价于
PARSE_ON_READER_THREAD=True 时后端读取线程的行为），同时 GUI 线程并发做
渲染快照 / 清空回滚 / 复制 / 交互检测 / 滚动。目标是复现并回归以下已修竞态：

- clear_scrollback 无锁清 deque → "deque mutated during iteration" 崩溃
- 复制/交互检测无锁读 live screen → "dict changed size during iteration" 崩溃
- _output_buffer append vs flush 的 join+clear → 录制丢块
- scroll_offset 两线程并发写 → 位置错乱

若这些竞态复活，本测试会以异常/崩溃或计数不符失败。
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.resize(800, 480)
        w._update_terminal_size()
        return w

    def _feed_bytes(self, i):
        # 混合：普通文本 + 换行制造历史 + 偶尔宽字符 + 偶尔查询序列
        s = f"row {i:06d} the quick brown fox 中文 \r\n"
        if i % 50 == 0:
            s += "\x1b[6n"          # DSR：会触发读取线程 _write_to_backend
        if i % 137 == 0:
            s += "\x07"             # BEL：interaction_requested
        return s.encode('utf-8')


class TestReaderThreadRaces(_Base):
    def test_feed_thread_vs_gui_operations(self):
        from PyQt6.QtCore import QEventLoop
        w = self._widget()
        errors = []
        stop = threading.Event()
        GUI_ITERS = 200

        def feeder():
            # 节流模拟真实后端的 8ms 批合并：不加节流会每秒 emit 上万个
            # _output_activity 信号淹没 Qt 事件队列，使 processEvents 排不空
            # 而 livelock（生产中不会发生）。这里只为压测 screen 并发访问。
            i = 0
            try:
                while not stop.is_set():
                    w._on_output(self._feed_bytes(i))
                    i += 1
                    time.sleep(0.0005)
            except Exception as e:  # 任何解析线程异常都是回归
                errors.append(('feeder', repr(e)))

        t = threading.Thread(target=feeder, name='reader-sim')
        t.start()
        try:
            # GUI 侧做固定次数的读 screen 操作，全程与读取线程 feed 重叠。
            # 这些正是审计中会崩的点：渲染快照 / 读 display / 读 live buffer /
            # 清 history deque / 回滚补偿。
            for ops in range(GUI_ITERS):
                if errors:
                    break
                try:
                    with w._screen_lock:
                        w._rebuild_cache()
                    w._detect_interaction_prompt()
                    w._get_all_content()
                    if ops % 20 == 0:
                        w.clear_scrollback()
                    if ops % 7 == 0:
                        w.scroll_offset = 5
                        w._on_history_grew(3)
                    # 带时限，避免被后台信号流 livelock
                    self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 2)
                    time.sleep(0.001)  # 给 feeder 调度机会，制造真实交错
                except Exception as e:
                    errors.append(('gui', repr(e)))
                    break
        finally:
            stop.set()
            t.join(timeout=10)

        self.assertFalse(t.is_alive(), "feeder 线程未退出")
        self.assertEqual(errors, [], f"并发出现异常: {errors}")

    def test_output_buffer_no_lost_chunks(self):
        """append(读线程) 与 flush 换出(GUI) 并发，录制不丢块。

        每块内容唯一，读线程持续 append，GUI 持续 flush 收集，最后总收到的
        块数必须等于 append 的块数——旧的 join+clear 竞态会丢块。
        """
        w = self._widget()
        collected = []
        w.output_recorded.connect(lambda s: collected.append(s))

        TOTAL = 20000
        done = threading.Event()

        def appender():
            for n in range(TOTAL):
                with w._output_buffer_lock:
                    w._output_buffer.append(f"<{n}>")
                if n % 256 == 0:
                    time.sleep(0)  # 让出，增大与 flush 的交错概率
            done.set()

        t = threading.Thread(target=appender, name='appender')
        t.start()
        try:
            # 持续 flush 直到 appender 跑完
            while not done.is_set():
                w._flush_output_buffer()
        finally:
            t.join(timeout=10)
        # 收尾把残余也 flush 出来
        w._flush_output_buffer()

        total_appended = TOTAL
        # 每个唯一标记应恰好出现一次
        joined = ''.join(collected)
        markers = joined.count('<')
        self.assertEqual(markers, total_appended,
                         f"录制丢块：append {total_appended} 个，收到 {markers} 个")
        # 且无重复/错位：标记序号连续
        import re
        nums = [int(x) for x in re.findall(r'<(\d+)>', joined)]
        self.assertEqual(nums, list(range(total_appended)),
                         "录制块顺序错乱或有重复")


if __name__ == '__main__':
    unittest.main()
