"""Linux .desktop 生成去重守卫（回归 v1.14.26 双图标问题）

- 打包(frozen)不生成（.deb 自带条目，否则多一个图标）
- 源码生成与 .deb 同名的 stellar-smart-terminal.desktop
- 删掉历史遗留的旧 smart-terminal.desktop
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod


class TestLinuxDesktopFile(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.home = Path(tempfile.mkdtemp())
        self.apps = self.home / ".local" / "share" / "applications"
        self.apps.mkdir(parents=True)
        self._home_patch = mock.patch.object(Path, 'home', return_value=self.home)
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def _icon(self):
        p = self.home / "icon.png"
        p.write_bytes(b"x")
        return p

    def test_source_writes_matching_id(self):
        with mock.patch.object(app_mod.sys, 'frozen', False, create=True):
            app_mod._ensure_linux_desktop_file(self._icon())
        f = self.apps / "stellar-smart-terminal.desktop"
        self.assertTrue(f.exists())
        body = f.read_text(encoding='utf-8')
        self.assertIn("Name=Stellar Smart Terminal", body)
        self.assertIn("StartupWMClass=stellar-smart-terminal", body)

    def test_frozen_writes_nothing(self):
        with mock.patch.object(app_mod.sys, 'frozen', True, create=True):
            app_mod._ensure_linux_desktop_file(self._icon())
        self.assertFalse((self.apps / "stellar-smart-terminal.desktop").exists())

    def test_removes_legacy_duplicate(self):
        legacy = self.apps / "smart-terminal.desktop"
        legacy.write_text("[Desktop Entry]\nName=Smart Terminal\n", encoding='utf-8')
        with mock.patch.object(app_mod.sys, 'frozen', False, create=True):
            app_mod._ensure_linux_desktop_file(self._icon())
        self.assertFalse(legacy.exists())

    def test_legacy_removed_even_when_frozen(self):
        legacy = self.apps / "smart-terminal.desktop"
        legacy.write_text("x", encoding='utf-8')
        with mock.patch.object(app_mod.sys, 'frozen', True, create=True):
            app_mod._ensure_linux_desktop_file(self._icon())
        self.assertFalse(legacy.exists())

    def test_idempotent_no_rewrite(self):
        with mock.patch.object(app_mod.sys, 'frozen', False, create=True):
            app_mod._ensure_linux_desktop_file(self._icon())
            f = self.apps / "stellar-smart-terminal.desktop"
            app_mod._ensure_linux_desktop_file(self._icon())
            self.assertEqual(f.read_text(encoding='utf-8').count("[Desktop Entry]"), 1)


if __name__ == '__main__':
    unittest.main()
