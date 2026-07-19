"""测试全局隔离：把应用数据目录指到一次性临时目录。

背景：部分测试（如 test_window_navigator）会创建真实 MainWindow 并 close()，
关闭路径会触发 _save_config 写配置文件。没有隔离时，源码模式下写的就是
项目目录里开发者的真实 .smart_terminal_config.json（实测污染过）。

必须用环境变量而不是 monkeypatch：MainWindow.CONFIG_FILE、
remote_bookmarks._PATH、explorer_favorites._PATH 等路径在模块导入时就已
固化，事后 patch 不到。conftest 在 pytest 导入任何测试模块之前加载，
是唯一可靠的注入点。外部已显式设置 STELLAR_DATA_DIR 时尊重外部值。
"""
import atexit
import os
import shutil
import tempfile

if 'STELLAR_DATA_DIR' not in os.environ:
    _tmp_data_dir = tempfile.mkdtemp(prefix='stellar_test_data_')
    os.environ['STELLAR_DATA_DIR'] = _tmp_data_dir
    atexit.register(shutil.rmtree, _tmp_data_dir, ignore_errors=True)
