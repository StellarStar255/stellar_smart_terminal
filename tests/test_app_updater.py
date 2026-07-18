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

    def test_linux_has_no_one_click_asset(self):
        with _PlatformPatch('linux'):
            self.assertIsNone(app_updater.pick_update_asset(_ASSETS))

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
            self.assertFalse(app_updater.can_self_update())
        finally:
            sys.platform = old_platform
            if old_frozen is None:
                del sys.frozen
            else:
                sys.frozen = old_frozen


if __name__ == "__main__":
    unittest.main()
