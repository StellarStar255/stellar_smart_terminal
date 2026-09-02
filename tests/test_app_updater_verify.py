"""自升级链路的来源与完整性校验

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_app_updater_verify.py -v

回归背景（2026-09 审计）：下载 URL 完全信任 GitHub API 响应、落盘只比字节数、
macOS 换包脚本不验签就剥 quarantine。任何能篡改 API 响应的一方都能让
Linux 用户以 root 装任意 .deb、Windows 用户执行任意 exe。

这里锁定三道闸：
1. 产物 URL 必须钉在官方仓库的 releases/download/ 路径下（https + github.com）；
2. API 给了 sha256 digest 时，流式计算的哈希必须一致，否则丢弃残包并报错；
3. macOS 换包脚本在 mv/去 quarantine 之前必须 codesign --verify 并核对 Team ID，
   Python 侧解包后也先验签，失败走 error 信号而不是静默。
"""
import hashlib
import os
import shutil
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_updater

_GOOD = (f"https://github.com/{app_updater.REPO}/releases/download/"
         "v1.99.0/Stellar-Smart-Terminal-v1.99.0-macOS-arm64.zip")


class TestTrustedAssetUrl(unittest.TestCase):
    def test_official_release_asset_is_trusted(self):
        self.assertTrue(app_updater.is_trusted_asset_url(_GOOD))

    def test_plain_http_rejected(self):
        self.assertFalse(app_updater.is_trusted_asset_url(
            _GOOD.replace("https://", "http://", 1)))

    def test_other_host_rejected(self):
        self.assertFalse(app_updater.is_trusted_asset_url(
            _GOOD.replace("github.com", "evil.example", 1)))
        # 子域 / 前缀伪装
        self.assertFalse(app_updater.is_trusted_asset_url(
            _GOOD.replace("github.com", "github.com.evil.example", 1)))
        self.assertFalse(app_updater.is_trusted_asset_url(
            _GOOD.replace("github.com", "notgithub.com", 1)))

    def test_userinfo_trick_rejected(self):
        # https://github.com@evil.example/... 的真实主机是 evil.example
        self.assertFalse(app_updater.is_trusted_asset_url(
            _GOOD.replace("https://github.com", "https://github.com@evil.example", 1)))

    def test_other_repo_rejected(self):
        self.assertFalse(app_updater.is_trusted_asset_url(
            "https://github.com/someone/else/releases/download/v1/x.zip"))
        # 同 owner 不同仓库
        owner = app_updater.REPO.split("/")[0]
        self.assertFalse(app_updater.is_trusted_asset_url(
            f"https://github.com/{owner}/other-repo/releases/download/v1/x.zip"))
        # 仓库名只是前缀匹配
        self.assertFalse(app_updater.is_trusted_asset_url(
            f"https://github.com/{app_updater.REPO}-fork/releases/download/v1/x.zip"))

    def test_path_traversal_rejected(self):
        self.assertFalse(app_updater.is_trusted_asset_url(
            f"https://github.com/{app_updater.REPO}/releases/download/../../x/y.zip"))
        self.assertFalse(app_updater.is_trusted_asset_url(
            f"https://github.com/{app_updater.REPO}/releases/download/v1/..%2F..%2Fx.zip"))

    def test_non_release_path_rejected(self):
        self.assertFalse(app_updater.is_trusted_asset_url(
            f"https://github.com/{app_updater.REPO}/archive/refs/tags/v1.zip"))
        self.assertFalse(app_updater.is_trusted_asset_url(
            f"https://github.com/{app_updater.REPO}/releases/download"))

    def test_garbage_rejected(self):
        for bad in ("", None, "not a url", "https://", "file:///etc/passwd",
                    "https://github.com:8443/" + app_updater.REPO
                    + "/releases/download/v1/x.zip"):
            self.assertFalse(app_updater.is_trusted_asset_url(bad), bad)


class TestParseDigest(unittest.TestCase):
    def test_sha256_parsed(self):
        h = "a" * 64
        self.assertEqual(app_updater.parse_sha256_digest(f"sha256:{h}"), h)
        self.assertEqual(app_updater.parse_sha256_digest(f"SHA256:{h.upper()}"), h)

    def test_unknown_or_malformed_ignored(self):
        self.assertIsNone(app_updater.parse_sha256_digest(None))
        self.assertIsNone(app_updater.parse_sha256_digest(""))
        self.assertIsNone(app_updater.parse_sha256_digest("md5:" + "a" * 32))
        self.assertIsNone(app_updater.parse_sha256_digest("sha256:" + "a" * 63))
        self.assertIsNone(app_updater.parse_sha256_digest("sha256:" + "g" * 64))


class _FakeResponse:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.headers = {}

    def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b''

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _tmp_dirs():
    import glob
    import tempfile
    return set(glob.glob(os.path.join(tempfile.gettempdir(), 'stellar_update_*')))


class TestDownloaderGates(unittest.TestCase):
    """下载器：来源不可信直接拒绝；digest 不符丢弃残包。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _EXE = (f"https://github.com/{app_updater.REPO}/releases/download/"
            "v1.99.0/Stellar-Smart-Terminal-v1.99.0-windows-x64-setup.exe")

    def _run(self, dl, chunks):
        errors, results = [], []
        dl.error.connect(errors.append)
        dl.finished_ok.connect(results.append)
        opened = []

        def _fake_urlopen(url, timeout=0):
            opened.append(url)
            return _FakeResponse(chunks)

        with mock.patch.object(app_updater, '_urlopen', _fake_urlopen):
            dl.run()
        return errors, results, opened

    def test_untrusted_url_never_fetched(self):
        before = _tmp_dirs()
        dl = app_updater.UpdateDownloader("https://evil.example/setup.exe")
        errors, results, opened = self._run(dl, [b'x'])
        self.assertEqual(results, [])
        self.assertEqual(opened, [])          # 连请求都不该发
        self.assertEqual(len(errors), 1)
        self.assertEqual(_tmp_dirs() - before, set())

    def test_digest_match_completes(self):
        payload = b'installer-bytes' * 100
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        dl = app_updater.UpdateDownloader(self._EXE, len(payload), digest=digest)
        errors, results, _ = self._run(dl, [payload[:700], payload[700:]])
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            with open(results[0], 'rb') as f:
                self.assertEqual(f.read(), payload)
        finally:
            if results:
                shutil.rmtree(os.path.dirname(results[0]), ignore_errors=True)

    def test_digest_mismatch_rejects_and_cleans(self):
        payload = b'tampered-bytes!' * 100
        digest = "sha256:" + hashlib.sha256(b'what the API promised').hexdigest()
        before = _tmp_dirs()
        dl = app_updater.UpdateDownloader(self._EXE, len(payload), digest=digest)
        errors, results, opened = self._run(dl, [payload])
        self.assertEqual(results, [])         # 绝不交出被篡改的包
        self.assertEqual(len(errors), 1)
        self.assertIn('sha256', errors[0].lower())
        self.assertEqual(len(opened), 1)      # 篡改不是网络抖动，不重试
        self.assertEqual(_tmp_dirs() - before, set())

    def test_missing_digest_still_installs(self):
        # 旧 release 的 asset 没有 digest 字段：不能因此把升级堵死
        payload = b'legacy' * 10
        dl = app_updater.UpdateDownloader(self._EXE, len(payload))
        errors, results, _ = self._run(dl, [payload])
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
        finally:
            if results:
                shutil.rmtree(os.path.dirname(results[0]), ignore_errors=True)

    def test_unknown_digest_algorithm_ignored(self):
        payload = b'legacy' * 10
        dl = app_updater.UpdateDownloader(self._EXE, len(payload),
                                          digest="md5:" + "0" * 32)
        errors, results, _ = self._run(dl, [payload])
        try:
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
        finally:
            if results:
                shutil.rmtree(os.path.dirname(results[0]), ignore_errors=True)


class TestMacSignatureGate(unittest.TestCase):
    def test_script_verifies_before_touching_bundle(self):
        s = app_updater.build_updater_script(4242, "/Applications/X.app", "/tmp/new/X.app")
        self.assertIn("codesign --verify --deep --strict", s)
        self.assertIn(f"TeamIdentifier={app_updater.MAC_TEAM_ID}", s)
        verify_at = s.index("codesign --verify")
        team_at = s.index("TeamIdentifier=")
        # 验签必须发生在剥 quarantine 与换包之前
        self.assertLess(verify_at, s.index("xattr -dr"))
        self.assertLess(team_at, s.index("xattr -dr"))
        self.assertLess(verify_at, s.index('mv "$BUNDLE"'))
        # 验签失败要把旧包重新拉起，不能留用户一个关着的 app
        fail_branch = s[verify_at:s.index("xattr -dr")]
        self.assertIn('open "$BUNDLE"', fail_branch)
        self.assertIn("exit 1", fail_branch)

    def test_team_id_matches_ci(self):
        # Team ID 单一来源：与发版流水线一致（release.yml NOTARY_TEAM_ID）
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        yml = os.path.join(root, '.github', 'workflows', 'release.yml')
        if not os.path.exists(yml):
            self.skipTest("no release.yml")
        with open(yml, encoding='utf-8') as f:
            self.assertIn(f"NOTARY_TEAM_ID: {app_updater.MAC_TEAM_ID}", f.read())

    def test_python_side_verify_uses_codesign_and_team_id(self):
        calls = []

        class _Done:
            def __init__(self, rc, out=b''):
                self.returncode = rc
                self.stdout = out
                self.stderr = b''

        def _fake_run(cmd, **kw):
            calls.append(cmd)
            if '--verify' in cmd:
                return _Done(0)
            return _Done(0, f"TeamIdentifier={app_updater.MAC_TEAM_ID}\n".encode())

        with mock.patch.object(app_updater.subprocess, 'run', _fake_run):
            self.assertIsNone(app_updater.verify_mac_app_signature("/tmp/X.app"))
        self.assertTrue(any('--verify' in c for c in calls))

        # 验签失败 → 返回原因字符串
        def _fail_verify(cmd, **kw):
            if '--verify' in cmd:
                return _Done(1)
            return _Done(0, f"TeamIdentifier={app_updater.MAC_TEAM_ID}\n".encode())

        with mock.patch.object(app_updater.subprocess, 'run', _fail_verify):
            self.assertTrue(app_updater.verify_mac_app_signature("/tmp/X.app"))

        # 签名有效但 Team ID 不对（别人签的包）→ 拒绝
        def _wrong_team(cmd, **kw):
            if '--verify' in cmd:
                return _Done(0)
            return _Done(0, b"TeamIdentifier=ZZZZZZZZZZ\n")

        with mock.patch.object(app_updater.subprocess, 'run', _wrong_team):
            self.assertTrue(app_updater.verify_mac_app_signature("/tmp/X.app"))

        # codesign 不存在 → 视为失败
        def _missing(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        with mock.patch.object(app_updater.subprocess, 'run', _missing):
            self.assertTrue(app_updater.verify_mac_app_signature("/tmp/X.app"))


if __name__ == '__main__':
    unittest.main()
