# -*- coding: utf-8 -*-
"""终端子进程环境注入的测试

Claude Code 2.x 默认进备用屏幕（1049）+ 内部虚拟滚动 + 鼠标上报，
"跨页"的历史全在 claude 进程内部，终端侧没有 scrollback，导致拖选
无法跨页复制。修复方式是给子进程注入
CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 让 claude 回到内联渲染。
本测试固化该注入行为（真实 fork 子进程验证），并验证外部显式设置
的值不被覆盖（setdefault 语义）。

仅 POSIX：Windows 后端走 ConPTY + CreateProcessW，同一处代码路径
在真机验证；CI 上 fork/pty 不可用。

子进程把 env 写到文件而不是 stdout：Linux 上子进程写完立即退出时，
pty master 端可能在 drain 前就撞上 EIO 丢掉缓冲输出（macOS 是 EOF，
不丢），按 stdout 断言在 Linux CI 上会读到空串（1.14.39 首打即崩）。

运行方式：
    python3 -m unittest tests.test_terminal_backend_env -v
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal_backend import create_backend


@unittest.skipIf(sys.platform == 'win32', 'POSIX pty backend only')
class TestChildEnvInjection(unittest.TestCase):
    """spawn 真实子进程检查注入的环境变量（经文件回传，避开 pty 竞态）"""

    def _spawn_env_output(self):
        with tempfile.TemporaryDirectory(prefix='backend_env_test_') as tmp:
            out_path = os.path.join(tmp, 'env.txt')
            os.environ['STELLAR_TEST_ENV_OUT'] = out_path
            backend = create_backend()
            try:
                self.assertTrue(backend.start(
                    ['/bin/sh', '-c', 'env > "$STELLAR_TEST_ENV_OUT"'],
                    cwd='/tmp'))
                deadline = time.time() + 5
                text = ''
                while time.time() < deadline:
                    if os.path.exists(out_path):
                        text = Path(out_path).read_text(encoding='utf-8',
                                                        errors='replace')
                        # 文件已出现且写完（env 输出必然包含 PATH=）
                        if 'PATH=' in text:
                            break
                    time.sleep(0.05)
                return text
            finally:
                backend.stop()
                os.environ.pop('STELLAR_TEST_ENV_OUT', None)

    def test_claude_alt_screen_disabled_by_default(self):
        text = self._spawn_env_output()
        self.assertIn('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1', text)

    def test_external_value_wins(self):
        # 用户在外部环境显式设置的值优先（setdefault 不覆盖）
        old = os.environ.get('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN')
        os.environ['CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN'] = '0'
        try:
            text = self._spawn_env_output()
            self.assertIn('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=0', text)
        finally:
            if old is None:
                os.environ.pop('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN', None)
            else:
                os.environ['CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN'] = old


if __name__ == '__main__':
    unittest.main()
