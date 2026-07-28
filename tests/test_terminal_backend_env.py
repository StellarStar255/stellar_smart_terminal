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

运行方式：
    python3 -m unittest tests.test_terminal_backend_env -v
"""

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from terminal_backend import create_backend


@unittest.skipIf(sys.platform == 'win32', 'POSIX pty backend only')
class TestChildEnvInjection(unittest.TestCase):
    """spawn 真实子进程（/usr/bin/env）检查注入的环境变量"""

    def _spawn_env_output(self):
        out = []
        backend = create_backend()
        backend.on_output = lambda d: out.append(d)
        self.assertTrue(backend.start(['/usr/bin/env'], cwd='/tmp'))
        deadline = time.time() + 5
        while time.time() < deadline and backend.is_running:
            time.sleep(0.05)
        backend.stop()
        return b''.join(out).decode('utf-8', 'replace')

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
