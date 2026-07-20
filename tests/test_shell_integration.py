"""系统右键菜单集成（shell_integration）与 --working-dir 解析测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_shell_integration.py -v

安装/卸载全部重定向到临时目录，不碰真实 ~/Library / 注册表。
"""
import os
import plistlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shell_integration as si
from app import parse_cli_args


class TestParseCliArgs:
    def test_valid_dir(self, tmp_path):
        wd, rest = parse_cli_args(['app.py', '--working-dir', str(tmp_path)])
        assert wd == str(tmp_path)
        assert rest == ['app.py']

    def test_equals_form(self, tmp_path):
        wd, rest = parse_cli_args(['app.py', f'--working-dir={tmp_path}'])
        assert wd == str(tmp_path)
        assert rest == ['app.py']

    def test_invalid_dir_ignored(self):
        wd, rest = parse_cli_args(['app.py', '--working-dir', '/no/such/dir'])
        assert wd is None
        assert rest == ['app.py']

    def test_other_args_passthrough(self, tmp_path):
        wd, rest = parse_cli_args(
            ['app.py', '-platform', 'offscreen', '--working-dir', str(tmp_path)])
        assert wd == str(tmp_path)
        assert rest == ['app.py', '-platform', 'offscreen']

    def test_missing_value(self):
        wd, rest = parse_cli_args(['app.py', '--working-dir'])
        assert wd is None
        # 悬空参数不留给 Qt
        assert rest == ['app.py']


@pytest.fixture
def fake_services(tmp_path, monkeypatch):
    monkeypatch.setattr(si, '_macos_services_dir', lambda: tmp_path)
    monkeypatch.setattr(si, '_macos_refresh_services', lambda: None)
    return tmp_path


class TestMacosQuickAction:
    def test_install_creates_valid_workflow(self, fake_services):
        ok, err = si._macos_install()
        assert ok, err
        contents = fake_services / si._WORKFLOW_NAME / 'Contents'

        with open(contents / 'Info.plist', 'rb') as f:
            info = plistlib.load(f)
        svc = info['NSServices'][0]
        assert svc['NSSendFileTypes'] == ['public.folder']
        assert svc['NSMessage'] == 'runWorkflowAsService'
        assert svc['NSMenuItem']['default']  # 菜单文案非空

        with open(contents / 'document.wflow', 'rb') as f:
            doc = plistlib.load(f)
        params = doc['actions'][0]['action']['ActionParameters']
        assert params['inputMethod'] == 1  # $@ 作为参数
        # 源码形态：直接拉起 python 进程
        assert 'app.py' in params['COMMAND_STRING']
        assert '--working-dir' in params['COMMAND_STRING']

    def test_frozen_uses_open_a(self, fake_services, monkeypatch, tmp_path):
        bundle = tmp_path / 'Fake.app' / 'Contents' / 'MacOS'
        bundle.mkdir(parents=True)
        exe = bundle / 'fake'
        exe.touch()
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, 'executable', str(exe))
        script = si._macos_shell_script()
        assert 'open -a' in script
        assert 'Fake.app' in script

    def test_installed_and_uninstall(self, fake_services):
        assert not si._macos_installed()
        si._macos_install()
        assert si._macos_installed()
        ok, err = si._macos_uninstall()
        assert ok, err
        assert not si._macos_installed()
        # 重复卸载幂等
        assert si._macos_uninstall()[0]


class TestLinuxNautilusScript:
    @pytest.fixture
    def fake_script(self, tmp_path, monkeypatch):
        path = tmp_path / 'scripts' / si._NAUTILUS_SCRIPT_NAME
        monkeypatch.setattr(si, '_linux_script_path', lambda: path)
        return path

    def test_install_creates_executable_script(self, fake_script):
        ok, err = si._linux_install()
        assert ok, err
        assert fake_script.exists()
        if os.name == 'posix':
            # Windows 上 chmod 不产生可执行位（st_mode 恒为 0o666），只在
            # POSIX 校验；脚本内容断言仍全平台跑
            assert fake_script.stat().st_mode & 0o111
        body = fake_script.read_text(encoding='utf-8')
        assert body.startswith('#!/bin/bash')
        assert '--working-dir' in body
        assert 'NAUTILUS_SCRIPT_SELECTED_FILE_PATHS' in body

    def test_uninstall_idempotent(self, fake_script):
        si._linux_install()
        assert si._linux_uninstall()[0]
        assert not fake_script.exists()
        assert si._linux_uninstall()[0]


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows only')
class TestWindowsRegistry:
    def test_roundtrip(self):
        assert si._win_install()[0]
        assert si._win_installed()
        assert si._win_uninstall()[0]
        assert not si._win_installed()


class TestWinCommand:
    def test_command_quotes_and_arg(self):
        cmd = si._win_command()
        assert '--working-dir "%V"' in cmd
        assert cmd.startswith('"')
