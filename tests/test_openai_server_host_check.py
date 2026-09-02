"""OpenAI 兼容服务器的 Host 头校验（DNS rebinding 防护）

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_openai_server_host_check.py -v

_check_csrf 只看 Sec-Fetch-Site / Origin，攻击者把自己域名的 DNS 重绑到
127.0.0.1 后，浏览器发出的是「同源」请求，两道检查都放行。浏览器无法伪造
Host 头，所以 Host 不是本机回环名 + 实际端口就必须 403，且不能碰 PTY。
用裸 socket 发请求以便精确控制（乃至省略）Host 头。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_openai_server_lifecycle import _FakeTerminal, _free_port  # noqa: E402


def _raw_request(port: int, request: bytes, timeout: float = 5.0):
    """发送原始 HTTP 请求，返回 (status_code, body_bytes)"""
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as s:
        s.sendall(request)
        chunks = []
        while True:
            try:
                data = s.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
    raw = b''.join(chunks)
    head, _, body = raw.partition(b'\r\n\r\n')
    status = int(head.split(b' ', 2)[1])
    return status, body


def _get(port: int, host_header, path: str = '/health'):
    host_line = b'' if host_header is None else f'Host: {host_header}\r\n'.encode()
    req = (f'GET {path} HTTP/1.1\r\n'.encode() + host_line
           + b'Connection: close\r\n\r\n')
    return _raw_request(port, req)


def _post_chat(port: int, host_header: str):
    body = b'{"model":"x","messages":[{"role":"user","content":"rm -rf /"}]}'
    req = (b'POST /v1/chat/completions HTTP/1.1\r\n'
           + f'Host: {host_header}\r\n'.encode()
           + b'Content-Type: text/plain\r\n'
           + f'Content-Length: {len(body)}\r\n'.encode()
           + b'Connection: close\r\n\r\n' + body)
    return _raw_request(port, req)


class TestHostHeaderCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from openai_server import OpenAICompatServer, ServerConfig
        self.port = _free_port()
        self.terminal = _FakeTerminal()
        self.server = OpenAICompatServer(self.terminal, ServerConfig(port=self.port))
        self._listening, self._stopped, self._errors = [], [], []
        self.server.listening.connect(lambda p: self._listening.append(p))
        self.server.stopped.connect(lambda: self._stopped.append(True))
        self.server.error.connect(lambda e: self._errors.append(e))
        self.server.start()
        deadline = time.monotonic() + 10.0
        while not self._listening and not self._errors and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(self._errors, f"server failed to start: {self._errors}")
        self.assertTrue(self._listening, "server did not start listening")

    def tearDown(self):
        self.server.stop()
        deadline = time.monotonic() + 5.0
        while not self._stopped and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.server.wait(3000)

    # --- 放行 ---
    def test_loopback_hosts_pass(self):
        for host in (f'127.0.0.1:{self.port}', f'localhost:{self.port}', f'[::1]:{self.port}'):
            with self.subTest(host=host):
                status, body = _get(self.port, host)
                self.assertEqual(status, 200, body)
                self.assertIn(b'"ok"', body)

    # --- 拒绝 ---
    def test_foreign_host_rejected(self):
        status, body = _get(self.port, f'evil.com:{self.port}')
        self.assertEqual(status, 403, body)
        self.assertIn(b'Host', body)

    def test_missing_host_rejected(self):
        status, body = _get(self.port, None)
        self.assertEqual(status, 403, body)

    def test_wrong_port_rejected(self):
        status, body = _get(self.port, f'127.0.0.1:{self.port + 1}')
        self.assertEqual(status, 403, body)

    def test_rebound_post_never_reaches_terminal(self):
        """DNS rebinding 场景：同源 POST 带外域 Host，必须 403 且不写 PTY"""
        status, body = _post_chat(self.port, f'evil.com:{self.port}')
        self.assertEqual(status, 403, body)
        for _ in range(20):
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(self.terminal.writes, [], "被拒绝的请求仍写入了终端")

    def test_options_preflight_rejected(self):
        req = (b'OPTIONS /v1/chat/completions HTTP/1.1\r\n'
               + f'Host: evil.com:{self.port}\r\n'.encode()
               + b'Connection: close\r\n\r\n')
        status, _ = _raw_request(self.port, req)
        self.assertEqual(status, 403)


if __name__ == '__main__':
    unittest.main()
