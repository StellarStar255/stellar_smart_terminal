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
import sys
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


# ---------------------------------------------------------------------------
# 每个测试模块跑完，销毁它留下的顶层控件。
#
# 理由：测试造的 TerminalWidget / RemoteExplorerPanel / GitPanel / MainWindow
# 都带后台线程（PTY 读取线程、reflow/search 线程池、SSH executor、git worker）。
# 留给后面的模块，Python 在任意时刻回收其中一个（线程还在跑）就会让 Qt
# qFatal —— 整个 pytest 进程在**随机位置**硬中止，看起来像"某个无关测试偶发
# 崩溃"。历史上被咬三次：v1.25.0 的 macOS 发版 job、v1.26.2 的 Windows 发版
# job（0xC0000409）、以及本地全量跑。
#
# 按模块（而不是按用例）销毁：setUpClass 造的窗口本就只服务于该类/模块，
# 模块结束即到期；按用例销毁会把它们提前拆掉。
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


def _dispose_widget(app, widget):
    from PyQt6 import sip
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QCloseEvent
    if sip.isdeleted(widget):
        return
    from PyQt6.QtWidgets import QMainWindow
    if isinstance(widget, QMainWindow):
        # 只给主窗口发关闭事件：它的 closeEvent 才是真正的清理入口（停线程、
        # 落配置）。给普通控件发合成 QCloseEvent 会走进它自己的 event()
        # 重载——TerminalWidget 就在那里中止了整个进程。
        try:
            # closeEvent 在有终端运行时会弹模态确认框，离屏下 exec 直接卡死；
            # 强制路径跳过弹窗，只做清理。
            widget._force_closing = True
            app.sendEvent(widget, QCloseEvent())
        except (RuntimeError, AttributeError):
            pass  # 控件已销毁
    try:
        cleanup = getattr(widget, 'cleanup', None)
        if callable(cleanup):
            cleanup()          # TerminalWidget：停线程、关 PTY
    except Exception as e:     # noqa: BLE001
        # 收尾兜底绝不能让用例失败：测试常把控件的成员换成假对象
        # （SimpleNamespace 等），cleanup() 在上面会抛各种 AttributeError。
        print(f"conftest: cleanup({type(widget).__name__}) skipped: {e!r}",
              file=sys.stderr)
    try:
        widget.deleteLater()
    except RuntimeError:
        pass  # 已销毁
    for _ in range(3):
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.fixture(autouse=True, scope="module")
def _dispose_module_toplevels():
    from PyQt6.QtWidgets import QApplication
    from PyQt6 import sip
    app = QApplication.instance()
    before = {id(w) for w in app.topLevelWidgets()} if app is not None else set()
    yield
    app = QApplication.instance()
    if app is None:
        return
    for w in list(app.topLevelWidgets()):
        if id(w) in before or sip.isdeleted(w):
            continue
        _dispose_widget(app, w)
