# -*- coding: utf-8 -*-
"""Remote 面板交互式 ssh 终端命令构造的测试

build_ssh_terminal_command 的关键行为：
- 远程命令统一注入 claude 相关环境（export 在 exec 前，登录 shell 继承），
  远程 claude 与本地体验一致（跨页复制修复的 SSH 侧补全）；
- 别名走 ssh_config；手工 user@host:port 拆参数；
- cd 路径安全引用；${SHELL:-/bin/bash} 不被本地展开（单引号传递）。

纯函数无 Qt，可直接运行：
    python3 -m unittest tests.test_ssh_terminal_command -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ssh_session import HostConfig, build_ssh_terminal_command


class TestBuildSshTerminalCommand(unittest.TestCase):
    def test_alias_host_injects_claude_env(self):
        cfg = HostConfig(alias='mybox', hostname='10.0.0.1')
        cmd = build_ssh_terminal_command(cfg)
        self.assertTrue(cmd.startswith('ssh mybox -t '))
        self.assertIn('export CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1; ', cmd)
        self.assertIn('exec ${SHELL:-/bin/bash} -l', cmd)
        # 远程命令必须整体单引号传递，本地 shell 不展开 ${SHELL}
        self.assertIn("'export CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1; "
                      "exec ${SHELL:-/bin/bash} -l'", cmd)

    def test_manual_host_with_port(self):
        cfg = HostConfig(alias='zy@10.0.0.9:2222', hostname='10.0.0.9',
                         user='zy', port=2222)
        cmd = build_ssh_terminal_command(cfg)
        self.assertIn('-p 2222', cmd)
        self.assertIn('zy@10.0.0.9', cmd)
        self.assertIn('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1', cmd)

    def test_cd_path_is_quoted(self):
        cfg = HostConfig(alias='mybox', hostname='10.0.0.1')
        cmd = build_ssh_terminal_command(cfg, "/data/my dir/proj")
        # 路径含空格必须被引用，且顺序为 export → cd → exec
        self.assertIn("my dir", cmd)
        self.assertLess(cmd.index('export CLAUDE_CODE'), cmd.index('cd '))
        self.assertLess(cmd.index('cd '), cmd.index('exec ${SHELL'))

    def test_local_env_override_follows(self):
        old = os.environ.get('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN')
        os.environ['CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN'] = '0'
        try:
            cmd = build_ssh_terminal_command(
                HostConfig(alias='mybox', hostname='h'))
            self.assertIn('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=0', cmd)
        finally:
            if old is None:
                os.environ.pop('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN', None)
            else:
                os.environ['CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN'] = old

    def test_weird_env_value_falls_back_to_safe_default(self):
        # 值会拼进 shell 命令：未知取值必须回退 '1'，不得注入
        old = os.environ.get('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN')
        os.environ['CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN'] = "1'; rm -rf /; '"
        try:
            cmd = build_ssh_terminal_command(
                HostConfig(alias='mybox', hostname='h'))
            self.assertIn('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1; ', cmd)
            self.assertNotIn('rm -rf', cmd)
        finally:
            if old is None:
                os.environ.pop('CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN', None)
            else:
                os.environ['CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN'] = old


if __name__ == '__main__':
    unittest.main()
