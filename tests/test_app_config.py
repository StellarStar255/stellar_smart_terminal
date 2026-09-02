"""app_config 单点配置读写测试

    python3 -m pytest tests/test_app_config.py -v

覆盖：patch 合并不清空他人字段、损坏文件拒绝写入、mutator 跳过写、
以及关键的多进程并发读-改-写不丢更新（进程间文件锁）。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_config


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / '.smart_terminal_config.json'
    monkeypatch.setattr(app_config, 'get_config_path', lambda: p)
    return p


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding='utf-8'))


class TestUpdateConfig:
    def test_creates_file_when_missing(self, cfg_path):
        assert app_config.update_config({'theme': 'dark'})
        assert _read(cfg_path) == {'theme': 'dark'}

    def test_merge_preserves_other_keys(self, cfg_path):
        # 模拟另一个组件已写入的字段（git_proxy 清零事故的场景）
        cfg_path.write_text(json.dumps({'git_proxy': 'http://127.0.0.1:1081'}),
                            encoding='utf-8')
        assert app_config.update_config({'theme': 'dark'})
        data = _read(cfg_path)
        assert data['git_proxy'] == 'http://127.0.0.1:1081'
        assert data['theme'] == 'dark'

    def test_corrupted_file_refuses_write(self, cfg_path):
        cfg_path.write_text('{"half": ', encoding='utf-8')  # 半截 JSON
        assert app_config.update_config({'theme': 'dark'}) is False
        # 原文件内容原样保留，没有被"当作损坏"覆盖
        assert cfg_path.read_text(encoding='utf-8') == '{"half": '

    def test_empty_patch_is_noop(self, cfg_path):
        assert app_config.update_config({}) is True
        assert not cfg_path.exists()

    def test_read_config_tolerant(self, cfg_path):
        assert app_config.read_config() == {}
        cfg_path.write_text('broken', encoding='utf-8')
        assert app_config.read_config() == {}
        cfg_path.write_text(json.dumps({'a': 1}), encoding='utf-8')
        assert app_config.read_config() == {'a': 1}

    def test_mutator_false_skips_write(self, cfg_path):
        cfg_path.write_text(json.dumps({'a': 1}), encoding='utf-8')
        before = cfg_path.stat().st_mtime_ns

        def no_change(cfg):
            return False

        assert app_config.update_config_with(no_change) is True
        assert cfg_path.stat().st_mtime_ns == before

    def test_mutator_exception_returns_false(self, cfg_path):
        cfg_path.write_text(json.dumps({'a': 1}), encoding='utf-8')

        def boom(cfg):
            raise ValueError('x')

        assert app_config.update_config_with(boom) is False
        assert _read(cfg_path) == {'a': 1}  # 未被破坏

    def test_update_config_with_nested_delete(self, cfg_path):
        cfg_path.write_text(json.dumps({'dirs': {'a': '/x', 'b': '/y'}}),
                            encoding='utf-8')

        def drop_a(cfg):
            cfg['dirs'].pop('a', None)

        assert app_config.update_config_with(drop_a)
        assert _read(cfg_path) == {'dirs': {'b': '/y'}}


class TestInterProcessLock:
    def test_concurrent_processes_do_not_lose_updates(self, tmp_path):
        """4 进程各自 +25 次计数：无锁时后写覆盖先写会丢更新，有锁应恰为 100"""
        p = tmp_path / 'cfg.json'
        script = tmp_path / 'inc.py'
        script.write_text(f'''
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from pathlib import Path
import app_config
app_config.get_config_path = lambda: Path({str(p)!r})

def inc(cfg):
    cfg['counter'] = cfg.get('counter', 0) + 1

for _ in range(25):
    assert app_config.update_config_with(inc)
''', encoding='utf-8')

        procs = [subprocess.Popen([sys.executable, str(script)])
                 for _ in range(4)]
        for proc in procs:
            self_ok = proc.wait(timeout=60)
            assert self_ok == 0
        assert _read(p)['counter'] == 100
