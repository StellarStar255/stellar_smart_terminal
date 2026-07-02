"""OpenAI 兼容服务器的并发与停止行为测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_openai_server_lifecycle.py -v

覆盖三个修复：
1. ThreadingHTTPServer 并发处理 —— 一个慢请求不再独占服务（/health 保持可用）
2. stop() 非阻塞 —— 不在 GUI 线程 wait 3 秒；线程随后自行退出并清理
3. HTTP 线程输入统一经 bridge 信号排队到 GUI 线程写入（不再直写 master_fd）
"""
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _get(url, timeout=5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


class _FakeTerminal:
    """最小终端替身：无 master_fd（chat 请求会快速失败），有可记录的后端写入"""

    def __init__(self):
        self._backend = object()
        self.writes = []  # (thread_name, text)

    def _write_to_backend(self, data: bytes):
        self.writes.append((threading.current_thread().name, data))


class TestServerLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _start_server(self):
        from openai_server import OpenAICompatServer, ServerConfig
        port = _free_port()
        server = OpenAICompatServer(_FakeTerminal(), ServerConfig(port=port))
        listening = []
        stopped = []
        server.listening.connect(lambda p: listening.append(p))
        server.stopped.connect(lambda: stopped.append(True))
        server.start()
        deadline = time.monotonic() + 5.0
        while not listening and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(listening, "server did not start listening")
        return server, port, stopped

    def _stop_and_join(self, server, stopped):
        server.stop()
        deadline = time.monotonic() + 5.0
        while not stopped and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        server.wait(3000)

    def test_health_and_concurrency(self):
        """慢请求进行中 /health 仍然可用（单线程 HTTPServer 会挂住）"""
        from openai_server import OpenAIRequestHandler
        server, port, stopped = self._start_server()
        orig = OpenAIRequestHandler._handle_models

        def slow_models(handler):
            time.sleep(2.0)
            return orig(handler)

        OpenAIRequestHandler._handle_models = slow_models
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                slow_fut = pool.submit(_get, f'http://127.0.0.1:{port}/v1/models', 10.0)
                time.sleep(0.3)  # 确保慢请求已在处理中
                t0 = time.monotonic()
                status, _ = _get(f'http://127.0.0.1:{port}/health', 5.0)
                health_elapsed = time.monotonic() - t0
                self.assertEqual(status, 200)
                self.assertLess(health_elapsed, 1.0,
                                "/health 被慢请求阻塞——服务器没有并发处理")
                self.assertEqual(slow_fut.result()[0], 200)
        finally:
            OpenAIRequestHandler._handle_models = orig
            self._stop_and_join(server, stopped)

    def test_stop_is_non_blocking_and_cleans_up(self):
        server, port, stopped = self._start_server()

        t0 = time.monotonic()
        server.stop()
        stop_elapsed = time.monotonic() - t0
        self.assertLess(stop_elapsed, 0.5, "stop() 阻塞了调用线程")

        deadline = time.monotonic() + 5.0
        while not stopped and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(stopped, "server 未发出 stopped 信号")
        self.assertTrue(server.wait(3000), "server 线程未退出")

        # 端口已释放（可重新绑定）
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('127.0.0.1', port))

    def test_send_input_routes_to_gui_thread(self):
        """HTTP 线程的 send_input 必须经信号排队到 GUI 线程执行"""
        from openai_server import TerminalBridge, ServerConfig
        fake = _FakeTerminal()
        bridge = TerminalBridge(fake, ServerConfig(port=_free_port()))

        worker = threading.Thread(
            target=lambda: bridge.send_input('hello\r'), name='http-worker')
        worker.start()
        worker.join()

        deadline = time.monotonic() + 3.0
        while not fake.writes and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(len(fake.writes), 1)
        thread_name, data = fake.writes[0]
        self.assertEqual(data, b'hello\r')
        self.assertEqual(thread_name, 'MainThread',
                         "输入没有排队回 GUI 线程执行")

    def test_shutdown_event_set_on_stop(self):
        """stop() 置位 shutdown_event，长收集循环据此尽快退出"""
        server, port, stopped = self._start_server()
        self.assertFalse(server.bridge.shutdown_event.is_set())
        self._stop_and_join(server, stopped)
        self.assertTrue(server.bridge.shutdown_event.is_set())


if __name__ == '__main__':
    unittest.main()
