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

# Qt 平台插件路径：与 app.py 的 setup_qt_plugin_path 同一处理。
# anaconda 等发行版的 qt.conf 会把 PluginsPath 指到自己的 Qt（如
# /opt/anaconda3/plugins），PyQt6 找不到 offscreen/cocoa 插件时
# QApplication 构造直接 abort。以前只有"全量跑且某个早收集的测试恰好
# import 了 app.py"才被顺带修好——单跑一个测试文件必崩。在 conftest
# 里确定性地设好，外部已显式设置时尊重外部值。
if 'QT_QPA_PLATFORM_PLUGIN_PATH' not in os.environ:
    try:
        import PyQt6
        _qt_plugins = os.path.join(
            os.path.dirname(PyQt6.__file__), 'Qt6', 'plugins')
        if os.path.isdir(os.path.join(_qt_plugins, 'platforms')):
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.join(
                _qt_plugins, 'platforms')
            os.environ.setdefault('QT_PLUGIN_PATH', _qt_plugins)
    except ImportError:
        pass
