"""内置终端 spawn 前的打包环境变量清洗测试

    python3 -m pytest tests/test_env_scrub.py -v

背景：打包 app 的 PyInstaller PyQt6 hook 会设 QT_PLUGIN_PATH 等指向
bundle 内部，泄漏给子 shell 后，子进程里的 Qt/Python 程序会混载两份 Qt
崩溃（实测）。scrub_packaging_env 在两条 spawn 路径共用。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terminal_backend import scrub_packaging_env


APP_QT = "/Applications/Stellar Smart Terminal.app/Contents/Frameworks/PyQt6/Qt6/plugins"


class TestScrubUnfrozen(unittest.TestCase):
    """源码运行形态（sys.frozen 不存在）。"""

    def test_bundle_pointing_qt_vars_removed(self):
        env = {'QT_PLUGIN_PATH': APP_QT,
               'QT_QPA_PLATFORM_PLUGIN_PATH': APP_QT + '/platforms',
               'QML2_IMPORT_PATH': APP_QT,
               'PATH': '/usr/bin'}
        scrub_packaging_env(env)
        self.assertNotIn('QT_PLUGIN_PATH', env)
        self.assertNotIn('QT_QPA_PLATFORM_PLUGIN_PATH', env)
        self.assertNotIn('QML2_IMPORT_PATH', env)
        self.assertEqual(env['PATH'], '/usr/bin')

    def test_meipass_temp_paths_removed(self):
        env = {'QT_PLUGIN_PATH': r'C:\Users\x\AppData\Local\Temp\_MEI1234\plugins'}
        scrub_packaging_env(env)
        self.assertNotIn('QT_PLUGIN_PATH', env)

    def test_user_own_qt_vars_kept(self):
        # 开发者自己设的、不指向任何 bundle 的路径要保留
        env = {'QT_PLUGIN_PATH': '/opt/homebrew/qt/plugins'}
        scrub_packaging_env(env)
        self.assertEqual(env['QT_PLUGIN_PATH'], '/opt/homebrew/qt/plugins')

    def test_bootloader_bookkeeping_always_removed(self):
        env = {'_MEIPASS2': '/tmp/x', '_PYI_ARCHIVE_FILE': '/tmp/y',
               'PYINSTALLER_RESET_ENVIRONMENT': '1'}
        scrub_packaging_env(env)
        self.assertEqual(env, {})

    def test_ld_path_orig_restored(self):
        env = {'LD_LIBRARY_PATH': '/inside/bundle',
               'LD_LIBRARY_PATH_ORIG': '/usr/local/lib'}
        scrub_packaging_env(env)
        self.assertEqual(env['LD_LIBRARY_PATH'], '/usr/local/lib')
        self.assertNotIn('LD_LIBRARY_PATH_ORIG', env)


class TestScrubFrozen(unittest.TestCase):
    """打包形态（sys.frozen=True）：Qt 变量无条件清。"""

    def setUp(self):
        self._old = getattr(sys, 'frozen', None)
        sys.frozen = True

    def tearDown(self):
        if self._old is None:
            del sys.frozen
        else:
            sys.frozen = self._old

    def test_qt_vars_removed_unconditionally(self):
        env = {'QT_PLUGIN_PATH': '/opt/homebrew/qt/plugins',
               'DYLD_LIBRARY_PATH': '/inside/bundle',
               'TCL_LIBRARY': '/bundle/tcl'}
        scrub_packaging_env(env)
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
