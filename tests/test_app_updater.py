"""应用内更新（app_updater）单元测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_app_updater.py -v

只测纯逻辑：版本解析比较、产物挑选、换包脚本内容、非打包环境的守卫。
网络与真实换包不在单测范围（发版后手动验证一次）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_updater


class TestVersionParse(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(app_updater.parse_version("v1.13.0"), (1, 13, 0))
        self.assertEqual(app_updater.parse_version("1.2.30"), (1, 2, 30))
        self.assertIsNone(app_updater.parse_version("garbage"))
        self.assertIsNone(app_updater.parse_version(""))
        self.assertIsNone(app_updater.parse_version(None))

    def test_compare_semantics(self):
        # 元组比较即版本比较：1.10.0 > 1.9.9
        self.assertGreater(app_updater.parse_version("v1.10.0"),
                           app_updater.parse_version("v1.9.9"))

    def test_current_version_from_pyproject(self):
        # 源码运行：读 pyproject.toml，应能解析出三段式版本
        v = app_updater.get_current_version()
        self.assertIsNotNone(app_updater.parse_version(v))

    def test_current_version_from_bundle_plist(self):
        # 打包形态：伪造 .app 结构，验证从自身 Info.plist 读版本
        if sys.platform != "darwin":
            self.skipTest("mac only")
        import plistlib
        import tempfile
        d = tempfile.mkdtemp()
        macos_dir = os.path.join(d, "Fake.app", "Contents", "MacOS")
        os.makedirs(macos_dir)
        with open(os.path.join(d, "Fake.app", "Contents", "Info.plist"),
                  "wb") as f:
            plistlib.dump({"CFBundleShortVersionString": "9.8.7"}, f)
        exe = os.path.join(macos_dir, "fake")
        open(exe, "w").close()

        old_frozen = getattr(sys, "frozen", None)
        old_exe = sys.executable
        try:
            sys.frozen = True
            sys.executable = exe
            self.assertTrue(app_updater.is_frozen_mac_app())
            self.assertEqual(str(app_updater.bundle_path()),
                             os.path.realpath(os.path.join(d, "Fake.app")))
            self.assertEqual(app_updater.get_current_version(), "9.8.7")
        finally:
            sys.executable = old_exe
            if old_frozen is None:
                del sys.frozen
            else:
                sys.frozen = old_frozen


_ASSETS = [
    {"name": "Stellar-Smart-Terminal-v1.14.0-windows-x64-setup.exe"},
    {"name": "Stellar-Smart-Terminal-v1.14.0-windows-x64.zip"},
    {"name": "Stellar-Smart-Terminal-v1.14.0-macOS-arm64.dmg"},
    {"name": "Stellar-Smart-Terminal-v1.14.0-macOS-arm64.zip"},
    {"name": "stellar-smart-terminal-v1.14.0-linux-amd64.deb"},
]


class _PlatformPatch:
    """临时改 sys.platform（pick_update_asset 按它分流）。"""

    def __init__(self, platform):
        self.platform = platform

    def __enter__(self):
        self._old = sys.platform
        sys.platform = self.platform

    def __exit__(self, *a):
        sys.platform = self._old


class TestAssetPick(unittest.TestCase):
    def test_pick_mac_zip(self):
        with _PlatformPatch('darwin'):
            picked = app_updater.pick_update_asset(_ASSETS)
        self.assertTrue(picked["name"].endswith("macOS-arm64.zip"))

    def test_pick_windows_installer(self):
        # Windows 必须选 Inno 安装包（-setup.exe），不能选 onedir zip
        with _PlatformPatch('win32'):
            picked = app_updater.pick_update_asset(_ASSETS)
        self.assertTrue(picked["name"].endswith("windows-x64-setup.exe"))

    def test_linux_picks_deb(self):
        with _PlatformPatch('linux'):
            picked = app_updater.pick_update_asset(_ASSETS)
        self.assertTrue(picked["name"].endswith("linux-amd64.deb"))

    def test_no_asset_edge_cases(self):
        with _PlatformPatch('darwin'):
            self.assertIsNone(app_updater.pick_update_asset(
                [{"name": "only-windows.exe"}]))
            self.assertIsNone(app_updater.pick_update_asset([]))
            self.assertIsNone(app_updater.pick_update_asset(None))


class TestUpdaterScript(unittest.TestCase):
    def test_script_contains_staged_swap_and_guards(self):
        script = app_updater.build_updater_script(
            12345, "/Applications/X.app", "/tmp/new/X.app")
        # 等待退出 + 超时守卫：应用没退就绝不动包
        self.assertIn('kill -0 "$PID" 2>/dev/null && exit 1', script)
        # 去 quarantine 兜底
        self.assertIn("xattr -dr com.apple.quarantine", script)
        # 分阶段换包 + 失败回滚
        self.assertIn('mv "$BUNDLE" "$STAGE"', script)
        self.assertIn('mv "$STAGE" "$BUNDLE"', script)
        # 换完重启
        self.assertIn('open "$BUNDLE"', script)
        self.assertIn("12345", script)

    def test_install_refuses_outside_frozen_mac(self):
        # 源码运行（非打包 .app）：不允许触发换包
        self.assertFalse(app_updater.is_frozen_mac_app())
        self.assertFalse(app_updater.can_self_update())
        self.assertFalse(app_updater.install_and_restart("/tmp/whatever.app"))


class TestWindowsUpdaterScript(unittest.TestCase):
    def test_bat_contains_wait_silent_install_and_relaunch(self):
        bat = app_updater.build_updater_bat(
            4242, r"C:\Temp\update.exe", r"C:\Apps\StellarSmartTerminal.exe")
        # 等待退出 + 超时守卫
        self.assertIn('tasklist /FI "PID eq 4242"', bat)
        self.assertIn("GEQ 120 exit /b 1", bat)
        # 静默原地升级 + 重启
        self.assertIn('/SILENT /NORESTART', bat)
        self.assertIn('start "" "C:\\Apps\\StellarSmartTerminal.exe"', bat)

    def test_can_self_update_frozen_windows(self):
        old_frozen = getattr(sys, "frozen", None)
        old_platform = sys.platform
        try:
            sys.frozen = True
            sys.platform = "win32"
            self.assertTrue(app_updater.can_self_update())
            sys.platform = "linux"
            self.assertTrue(app_updater.can_self_update())
            sys.platform = "freebsd"
            self.assertFalse(app_updater.can_self_update())
        finally:
            sys.platform = old_platform
            if old_frozen is None:
                del sys.frozen
            else:
                sys.frozen = old_frozen




class TestWindowsUpdaterRegression(unittest.TestCase):
    """v1.14.5 Windows 实战翻车的两个回归守卫。"""

    def test_bat_has_no_parenthesized_blocks(self):
        # cmd 的 %var% 在解析括号块时一次性展开——块内计数器读旧值，
        # 超时护卫失效。脚本必须是 goto 结构，且超时判断先于 tasklist。
        bat = app_updater.build_updater_bat(1, "s", "e")
        self.assertNotIn("(\n", bat)
        self.assertNotIn("if not errorlevel 1 (", bat)
        self.assertIn(":install", bat)
        self.assertLess(bat.index("GEQ 120"), bat.index("tasklist"))

    def test_spawn_uses_hidden_console_not_detached(self):
        # DETACHED_PROCESS 的 cmd 没有控制台，tasklist/find/ping 会各自
        # 弹出可见控制台窗口（Windows Terminal 窗口刷屏）。必须只用
        # CREATE_NO_WINDOW（隐形控制台，子命令继承）。
        import subprocess as sp
        recorded = {}

        class _FakePopen:
            def __init__(self, *a, **k):
                recorded['args'] = a
                recorded['flags'] = k.get('creationflags')

        old_frozen = getattr(sys, "frozen", None)
        old_platform = sys.platform
        old_popen = sp.Popen
        had_detached = hasattr(sp, 'DETACHED_PROCESS')
        had_no_window = hasattr(sp, 'CREATE_NO_WINDOW')
        try:
            sys.frozen = True
            sys.platform = "win32"
            sp.Popen = _FakePopen
            sp.DETACHED_PROCESS = 0x8
            sp.CREATE_NO_WINDOW = 0x08000000
            self.assertTrue(
                app_updater.install_and_restart("C:/Temp/update.exe"))
            self.assertEqual(recorded['flags'], 0x08000000,
                             "must be CREATE_NO_WINDOW only")
            self.assertEqual(list(recorded['args'][0][:2]), ['cmd', '/c'])
        finally:
            sp.Popen = old_popen
            if not had_detached:
                del sp.DETACHED_PROCESS
            if not had_no_window:
                del sp.CREATE_NO_WINDOW
            sys.platform = old_platform
            if old_frozen is None:
                del sys.frozen
            else:
                sys.frozen = old_frozen



class TestLinuxUpdaterScript(unittest.TestCase):
    def test_sh_waits_authorizes_installs_and_relaunches(self):
        sh = app_updater.build_updater_sh_linux(
            7777, "/tmp/update.deb", "/opt/StellarSmartTerminal/StellarSmartTerminal")
        # 等待退出 + 超时守卫：没退就绝不动系统
        self.assertIn('kill -0 "$PID" 2>/dev/null && exit 1', sh)
        # pkexec 缺失（无 polkit 环境）时放弃，旧版保持完好
        self.assertIn("command -v pkexec", sh)
        # 系统授权 + apt 原地升级失败即停，不做半截安装
        self.assertIn('pkexec apt-get install -y "$DEB" || exit 1', sh)
        # 升级成功后以独立会话重启
        self.assertIn('setsid "$EXE"', sh)
        self.assertIn("7777", sh)


class _FakeResponse:
    """替身 HTTP 响应：按块吐出预置数据，不碰网络。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.headers = {'Content-Length': str(sum(len(c) for c in chunks))}

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b''

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDownloadCancel(unittest.TestCase):
    """关掉进度窗必须真正中止下载（回归：曾经关窗后线程继续下载并弹重启确认）。"""

    def _run_downloader(self, dl, chunks):
        orig = app_updater._urlopen
        app_updater._urlopen = lambda url, timeout=0: _FakeResponse(chunks)
        try:
            dl.run()   # 直接同步跑线程体，不依赖 Qt 事件循环
        finally:
            app_updater._urlopen = orig

    def test_cancel_mid_download_emits_nothing_and_cleans_up(self):
        dl = app_updater.UpdateDownloader("https://example.com/x.zip")
        events = []
        dl.finished_ok.connect(lambda p: events.append(('ok', p)))
        dl.error.connect(lambda e: events.append(('err', e)))
        # 第一块下载进度回调里取消（对应用户中途关窗）
        dl.progress.connect(lambda done, total: dl.cancel())
        self._run_downloader(dl, [b'a' * 1024, b'b' * 1024])
        self.assertEqual(events, [])

    def test_cancel_before_completion_removes_workdir(self):
        import glob
        import tempfile as _tf
        before = set(glob.glob(os.path.join(_tf.gettempdir(), 'stellar_update_*')))
        dl = app_updater.UpdateDownloader("https://example.com/x.zip")
        dl.progress.connect(lambda done, total: dl.cancel())
        self._run_downloader(dl, [b'a' * 1024, b'b' * 1024])
        after = set(glob.glob(os.path.join(_tf.gettempdir(), 'stellar_update_*')))
        self.assertEqual(after - before, set())

    def test_uncancelled_download_still_completes(self):
        dl = app_updater.UpdateDownloader("https://example.com/setup-Setup.exe")
        events = []
        dl.finished_ok.connect(lambda p: events.append(('ok', p)))
        dl.error.connect(lambda e: events.append(('err', e)))
        self._run_downloader(dl, [b'a' * 1024])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'ok')
        # 清理这次测试产生的临时目录
        import shutil
        shutil.rmtree(os.path.dirname(events[0][1]), ignore_errors=True)


class TestLocalProxyFallback(unittest.TestCase):
    """环境没配代理时探测本机代理端口（回归：桌面启动的 GUI 继承不到
    终端 http_proxy，直连 GitHub 下载中途被掐断 SSL UNEXPECTED_EOF）。"""

    def _patch_ports(self, ports):
        orig = app_updater._LOCAL_PROXY_PORTS
        app_updater._LOCAL_PROXY_PORTS = ports
        self.addCleanup(setattr, app_updater, '_LOCAL_PROXY_PORTS', orig)

    def test_detects_listening_port(self):
        import socket
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        port = srv.getsockname()[1]
        self._patch_ports((port,))
        self.assertEqual(app_updater._detect_local_proxy(),
                         f'http://127.0.0.1:{port}')

    def test_returns_none_when_nothing_listens(self):
        import socket
        # 先 bind 拿一个空闲端口再关掉，保证探测时它必然没人监听
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        self._patch_ports((port,))
        self.assertIsNone(app_updater._detect_local_proxy())


class TestDownloadWorkdirCleanup(unittest.TestCase):
    """下载临时目录的生命周期：失败/取消清理，成功保留给安装步骤"""

    @classmethod
    def setUpClass(cls):
        # 必须用 QApplication 而不是 QCoreApplication：Qt 应用单例全进程唯一，
        # 同进程后续 GUI 测试会复用它，QCoreApplication 会让它们在
        # QApplication 专属 API 上直接 abort
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _tracking_mkdtemp(self, created):
        real_mkdtemp = app_updater.tempfile.mkdtemp

        def wrapper(*a, **kw):
            d = real_mkdtemp(*a, **kw)
            created['dir'] = d
            return d
        return wrapper

    def test_failed_download_cleans_workdir(self):
        from unittest import mock
        created = {}
        errors = []
        dl = app_updater.UpdateDownloader('https://example.invalid/pkg.zip')
        dl.error.connect(errors.append)
        with mock.patch.object(app_updater.tempfile, 'mkdtemp',
                               self._tracking_mkdtemp(created)), \
             mock.patch.object(app_updater, '_urlopen',
                               side_effect=OSError('boom')):
            dl.run()
        self.assertTrue(errors)
        self.assertIn('dir', created)
        self.assertFalse(os.path.exists(created['dir']))

    def test_success_keeps_workdir_for_install(self):
        import shutil
        from unittest import mock

        class _FakeResp:
            headers = {}
            _chunks = [b'payload']

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, _n):
                return self._chunks.pop() if self._chunks else b''

        created = {}
        results = []
        # .exe 走安装包分支：不解包直接 finished_ok
        dl = app_updater.UpdateDownloader('https://example.invalid/setup.exe')
        dl.finished_ok.connect(results.append)
        with mock.patch.object(app_updater.tempfile, 'mkdtemp',
                               self._tracking_mkdtemp(created)), \
             mock.patch.object(app_updater, '_urlopen',
                               return_value=_FakeResp()):
            dl.run()
        try:
            self.assertTrue(results)
            self.assertTrue(os.path.exists(results[0]))
        finally:
            shutil.rmtree(created['dir'], ignore_errors=True)


class TestDownloadTruncation(unittest.TestCase):
    """回归：下载被代理中途掐断成「干净 EOF」时，半截安装包绝不能当成功交给
    Inno 安装器（否则弹 "The setup files are corrupted"）。落盘字节数不足
    权威大小 → 重试 → 仍不足则上报错误并清理临时目录，绝不发 finished_ok。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _short_response(self, delivered: bytes):
        """一个「声称更长、实际吐一半就 EOF」的响应替身。expected_size 由
        UpdateDownloader 构造参数（asset size）给出，模拟代理丢了
        Content-Length 的情形——headers 为空。"""
        class _Resp:
            headers = {}

            def __init__(self):
                self._sent = False

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, _n):
                if self._sent:
                    return b''
                self._sent = True
                return delivered
        return _Resp

    def test_truncated_download_errors_and_cleans_up(self):
        import glob
        import tempfile as _tf
        from unittest import mock
        before = set(glob.glob(os.path.join(_tf.gettempdir(),
                                            'stellar_update_*')))
        errors, results = [], []
        # 权威大小 1000，实际每趟只吐 100 → 三次都不足
        dl = app_updater.UpdateDownloader(
            'https://example.invalid/x-setup.exe', 1000)
        dl.error.connect(errors.append)
        dl.finished_ok.connect(results.append)
        with mock.patch.object(app_updater, '_urlopen',
                               side_effect=self._short_response(b'a' * 100)):
            dl.run()
        self.assertEqual(results, [])          # 绝不交出半截包
        self.assertTrue(errors)                # 明确报错让用户重试
        after = set(glob.glob(os.path.join(_tf.gettempdir(),
                                           'stellar_update_*')))
        self.assertEqual(after - before, set())  # 残包清理干净

    def test_retry_recovers_from_transient_truncation(self):
        import shutil
        from unittest import mock

        created = {}
        _real_mkdtemp = app_updater.tempfile.mkdtemp

        def _tracking_mkdtemp(*a, **kw):
            d = _real_mkdtemp(*a, **kw)
            created['dir'] = d
            return d

        # 前两趟截断、第三趟完整
        responses = [self._short_response(b'a' * 100)(),
                     self._short_response(b'a' * 100)(),
                     self._short_response(b'a' * 1000)()]
        results, errors = [], []
        dl = app_updater.UpdateDownloader(
            'https://example.invalid/x-setup.exe', 1000)
        dl.finished_ok.connect(results.append)
        dl.error.connect(errors.append)
        with mock.patch.object(app_updater.tempfile, 'mkdtemp',
                               _tracking_mkdtemp), \
             mock.patch.object(app_updater, '_urlopen',
                               side_effect=lambda *a, **k: responses.pop(0)):
            dl.run()
        try:
            self.assertEqual(errors, [])
            self.assertTrue(results)             # 抖动过去后正常完成
            self.assertTrue(os.path.exists(results[0]))
        finally:
            if 'dir' in created:
                shutil.rmtree(created['dir'], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
