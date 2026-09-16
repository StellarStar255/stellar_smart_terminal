# -*- coding: utf-8 -*-
"""回归（2026-09 审查 E）：更新下载完成后的临时目录必须有人清。

三处泄漏：① 重启确认点「取消」什么都不做，几百 MB 的 update.zip + 解出的
.app 永远留在 /tmp；② mac 换包脚本只 mv 新 .app，不删 workdir；
③ Windows bat 装完不删 setup。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_update_workdir_cancel_2026_09_win.py -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox

from _qt_quit_reset_2026_09_win import dispose_windows

import app_updater

_TRUSTED = f"https://github.com/{app_updater.REPO}/releases/download/v9.9.9/"


class _StubDownloader(QThread):
    """替身：start() 不下载，直接造一个 workdir/extracted/X.app 并宣告完成。"""
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    error = pyqtSignal(str)
    last = None

    def __init__(self, url, expected_size=0, parent=None, digest=None):
        super().__init__(parent)
        self.workdir = None
        _StubDownloader.last = self

    def cancel(self):
        pass

    def start(self, *a, **k):
        self.workdir = tempfile.mkdtemp(prefix='stellar_update_')
        # 清理后 dl.workdir 会被置 None，测试用这份副本查目录是否还在
        _StubDownloader.last_workdir = self.workdir
        app_path = os.path.join(self.workdir, 'extracted', 'X.app')
        os.makedirs(app_path)
        with open(os.path.join(self.workdir, 'update.zip'), 'wb') as f:
            f.write(b'zip')
        self.finished_ok.emit(app_path)


class _ReplyBox(QMessageBox):
    reply = QMessageBox.StandardButton.Cancel

    def exec(self):
        return _ReplyBox.reply


class TestRestartCancelCleansWorkdir(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window

    def _run_download(self, reply, install_ret):
        w = self.mw.MainWindow()
        try:
            def fake_box(self_, icon, title, text, buttons=None):
                return _ReplyBox(self_)
            _ReplyBox.reply = reply
            with mock.patch.object(app_updater, 'UpdateDownloader', _StubDownloader), \
                 mock.patch.object(self.mw.MainWindow, '_make_styled_message_box', fake_box), \
                 mock.patch.object(app_updater, 'install_and_restart',
                                   return_value=install_ret) as install, \
                 mock.patch.object(QApplication, 'closeAllWindows'):
                w._start_update_download(
                    {'browser_download_url': _TRUSTED + 'X.zip', 'size': 3})
            return _StubDownloader.last_workdir, install
        finally:
            dispose_windows(self.app, [w])

    def test_cancel_at_restart_confirm_removes_workdir(self):
        workdir, install = self._run_download(QMessageBox.StandardButton.Cancel, True)
        install.assert_not_called()
        self.assertFalse(os.path.exists(workdir), workdir)

    def test_install_refused_removes_workdir(self):
        # 源码运行 / 不支持的平台：install_and_restart 返回 False，包也用不上了
        workdir, install = self._run_download(QMessageBox.StandardButton.Ok, False)
        install.assert_called_once()
        self.assertFalse(os.path.exists(workdir), workdir)

    def test_ok_keeps_workdir_for_installer_and_passes_it_along(self):
        workdir, install = self._run_download(QMessageBox.StandardButton.Ok, True)
        try:
            install.assert_called_once()
            self.assertEqual(install.call_args.kwargs.get('workdir'), workdir)
            self.assertTrue(os.path.exists(workdir))
        finally:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


class TestUpdaterScriptsCleanUp(unittest.TestCase):
    def test_mac_script_removes_workdir_after_swap(self):
        s = app_updater.build_updater_script(
            1, "/Applications/X.app", "/tmp/stellar_update_ab/extracted/X.app",
            workdir="/tmp/stellar_update_ab")
        self.assertIn('WORKDIR="/tmp/stellar_update_ab"', s)
        self.assertIn('rm -rf "$WORKDIR"', s)
        # 清理挂在 EXIT trap 上：换包/回滚/放弃哪条路都会清
        self.assertIn('trap', s)

    def test_mac_script_infers_workdir_from_new_app_path(self):
        s = app_updater.build_updater_script(
            1, "/Applications/X.app", "/tmp/stellar_update_ab/extracted/X.app")
        self.assertIn('WORKDIR="/tmp/stellar_update_ab"', s)

    def test_mac_script_never_removes_unknown_dirs(self):
        # 推断不出 stellar_update_ 目录时 WORKDIR 留空，脚本里有守卫不乱删
        s = app_updater.build_updater_script(1, "/Applications/X.app", "/tmp/new/X.app")
        self.assertIn('WORKDIR=""', s)
        self.assertIn('stellar_update_', s)

    def test_windows_bat_deletes_setup_and_workdir(self):
        bat = app_updater.build_updater_bat(
            1, r"C:\Temp\stellar_update_ab\update.exe", r"C:\Apps\S.exe",
            workdir=r"C:\Temp\stellar_update_ab")
        self.assertIn('del /q "C:\\Temp\\stellar_update_ab\\update.exe"', bat)
        self.assertIn('rd /s /q "C:\\Temp\\stellar_update_ab"', bat)
        # 先拉起新版再清理
        self.assertLess(bat.index('start ""'), bat.index('del /q'))
        # 老守卫：仍不能有括号块
        self.assertNotIn("(\n", bat)

    def test_linux_sh_removes_workdir(self):
        sh = app_updater.build_updater_sh_linux(
            1, "/tmp/stellar_update_ab/update.deb", "/opt/S/S")
        self.assertIn('WORKDIR="/tmp/stellar_update_ab"', sh)
        self.assertIn('rm -rf "$WORKDIR"', sh)


if __name__ == '__main__':
    unittest.main()
