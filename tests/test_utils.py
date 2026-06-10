# -*- coding: utf-8 -*-
"""utils.py 纯函数的 characterization 测试

预期值均为当前实现实际行为的固化（characterization），
包括几个可疑但保持现状的行为（见各测试注释）。
不需要 Qt，不依赖网络。
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import utils


class TestSearchTokens(unittest.TestCase):
    def test_lowercase_dedup_keep_order(self):
        self.assertEqual(
            utils.parse_search_tokens("  Stellar  TERMINAL stellar "),
            ["stellar", "terminal"],
        )

    def test_empty_query_means_no_filter(self):
        self.assertEqual(utils.parse_search_tokens(""), [])
        self.assertEqual(utils.parse_search_tokens("   "), [])

    def test_name_matches_all_tokens_and(self):
        tokens = utils.parse_search_tokens("stellar terminal")
        self.assertTrue(utils.name_matches_tokens("stellar_smart_terminal", tokens))
        self.assertFalse(utils.name_matches_tokens("stellar_only", tokens))

    def test_empty_tokens_match_everything(self):
        self.assertTrue(utils.name_matches_tokens("anything", []))

    def test_match_case_insensitive(self):
        self.assertTrue(utils.name_matches_tokens("README.MD", ["readme"]))


class TestExtractFilePaths(unittest.TestCase):
    """extract_file_paths（validate_exists=False 走纯正则逻辑）"""

    def test_unix_and_windows_absolute(self):
        got = utils.extract_file_paths(
            "see /tmp/foo.py and C:\\Users\\x\\a.js end", validate_exists=False
        )
        self.assertEqual(got, {"/tmp/foo.py", "C:\\Users\\x\\a.js"})

    def test_relative_path_also_matched_as_bogus_absolute(self):
        # 【可疑行为，固化为现状】"./scripts/build.py" 同时被
        # 相对路径正则和 Unix 绝对路径正则命中，后者从中间的 '/'
        # 开始截出并不存在的 "/scripts/build.py"。
        got = utils.extract_file_paths("run ./scripts/build.py now", validate_exists=False)
        self.assertEqual(got, {"./scripts/build.py", "/scripts/build.py"})

    def test_no_paths(self):
        self.assertEqual(utils.extract_file_paths("no paths here", validate_exists=False), set())

    def test_python_traceback_line(self):
        got = utils.extract_file_paths(
            'File "/usr/lib/python3.13/json/decoder.py", line 5', validate_exists=False
        )
        self.assertEqual(got, {"/usr/lib/python3.13/json/decoder.py"})

    def test_validate_exists_filters_to_real_files(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "real_file.py"
            real.write_text("pass\n")
            text = f"a {real} b /definitely/not/there.py"
            got = utils.extract_file_paths(text, validate_exists=True)
            self.assertEqual(got, {str(real.absolute())})


class TestStripAnsi(unittest.TestCase):
    def test_csi_color_sequences(self):
        self.assertEqual(utils.strip_ansi("\x1b[31mred\x1b[0m normal"), "red normal")

    def test_osc_title_and_carriage_return(self):
        self.assertEqual(
            utils.strip_ansi("\x1b]0;my title\x07hello\r\nworld"), "hello\nworld"
        )

    def test_control_chars_removed_but_tab_newline_kept(self):
        self.assertEqual(utils.strip_ansi("a\x07b\tc\nd"), "ab\tc\nd")

    def test_private_mode_and_clear(self):
        self.assertEqual(utils.strip_ansi("\x1b[?25hcursor\x1b[2J"), "cursor")

    def test_plain_text_untouched(self):
        self.assertEqual(utils.strip_ansi("plain text"), "plain text")


class TestFormatFileSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(utils.format_file_size(0), "0 B")
        self.assertEqual(utils.format_file_size(1023), "1023 B")

    def test_kb_mb_gb(self):
        self.assertEqual(utils.format_file_size(1024), "1.0 KB")
        self.assertEqual(utils.format_file_size(1536), "1.5 KB")
        self.assertEqual(utils.format_file_size(1048576), "1.0 MB")
        self.assertEqual(utils.format_file_size(1024 ** 3), "1.0 GB")

    def test_tb_is_off_by_factor_1024(self):
        # 【可疑行为，固化为现状】循环只除到 GB 就退出，TB 分支
        # 少除一次 1024：1 TiB 显示成 "1024.0 TB" 而不是 "1.0 TB"。
        self.assertEqual(utils.format_file_size(1024 ** 4), "1024.0 TB")
        self.assertEqual(utils.format_file_size(5 * 1024 ** 4), "5120.0 TB")


class TestMiscPureHelpers(unittest.TestCase):
    def test_is_image_file(self):
        self.assertTrue(utils.is_image_file("/a/b/pic.PNG"))
        self.assertTrue(utils.is_image_file("shot.webp"))
        self.assertFalse(utils.is_image_file("doc.pdf"))
        self.assertFalse(utils.is_image_file("noext"))

    def test_format_timestamp_explicit(self):
        dt = datetime(2026, 6, 10, 12, 34, 56)
        self.assertEqual(utils.format_timestamp(dt), "2026-06-10 12:34:56")

    def test_format_timestamp_default_now_shape(self):
        s = utils.format_timestamp()
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_generate_session_id_shape_and_uniqueness(self):
        a = utils.generate_session_id()
        b = utils.generate_session_id()
        self.assertRegex(a, r"^\d{8}_\d{6}_\d{6}$")
        self.assertNotEqual(a, b)  # 微秒级时间戳，连续两次不应碰撞

    def test_get_project_root_is_repo_dir(self):
        root = utils.get_project_root()
        self.assertTrue((root / "utils.py").exists())


class TestConfigJson(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="stellar_test_cfg_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = Path(self.dir) / "config.json"

    def test_read_missing_file_is_ok_empty(self):
        data, ok = utils.read_config_json(self.path)
        self.assertEqual(data, {})
        self.assertTrue(ok)

    def test_read_valid_json(self):
        self.path.write_text('{"a": 1}', encoding="utf-8")
        data, ok = utils.read_config_json(self.path)
        self.assertEqual(data, {"a": 1})
        self.assertTrue(ok)

    def test_read_corrupt_json_not_ok(self):
        # 半截 JSON（模拟另一个进程写到一半）→ ok=False，调用方应放弃保存
        self.path.write_text('{"a": 1, "b"', encoding="utf-8")
        data, ok = utils.read_config_json(self.path)
        self.assertEqual(data, {})
        self.assertFalse(ok)

    def test_read_json_null_normalized_to_empty_dict(self):
        self.path.write_text("null", encoding="utf-8")
        data, ok = utils.read_config_json(self.path)
        self.assertEqual(data, {})
        self.assertTrue(ok)

    def test_atomic_write_roundtrip_and_permissions(self):
        payload = {"key": "值", "list": [1, 2, 3]}
        self.assertTrue(utils.atomic_write_json(self.path, payload))
        data, ok = utils.read_config_json(self.path)
        self.assertTrue(ok)
        self.assertEqual(data, payload)
        # 强制 0o600（配置可能含明文密钥）
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        # 没有遗留 .tmp 临时文件
        leftovers = [p for p in Path(self.dir).iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])

    def test_atomic_write_creates_parent_dirs(self):
        nested = Path(self.dir) / "sub" / "dir" / "cfg.json"
        self.assertTrue(utils.atomic_write_json(nested, {"x": 1}))
        self.assertEqual(json.loads(nested.read_text(encoding="utf-8")), {"x": 1})

    def test_atomic_write_preserves_unicode(self):
        utils.atomic_write_json(self.path, {"名字": "终端"})
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("终端", raw)  # ensure_ascii=False


class TestCopyFilesToExport(unittest.TestCase):
    def setUp(self):
        self.src_dir = tempfile.mkdtemp(prefix="stellar_test_src_")
        self.export_dir = Path(tempfile.mkdtemp(prefix="stellar_test_exp_"))
        self.addCleanup(shutil.rmtree, self.src_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, str(self.export_dir), ignore_errors=True)

    def _mk(self, name, content="x"):
        p = Path(self.src_dir) / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_copies_into_assets_with_relative_mapping(self):
        f = self._mk("pic.png")
        mapping = utils.copy_files_to_export([f], self.export_dir)
        self.assertEqual(mapping, {f: os.path.join("assets", "pic.png")})
        self.assertTrue((self.export_dir / "assets" / "pic.png").exists())

    def test_name_conflict_appends_counter(self):
        f1 = self._mk("pic.png", "one")
        sub = Path(self.src_dir) / "sub"
        sub.mkdir()
        f2 = str(sub / "pic.png")
        Path(f2).write_text("two", encoding="utf-8")
        mapping = utils.copy_files_to_export([f1, f2], self.export_dir)
        self.assertEqual(mapping[f1], os.path.join("assets", "pic.png"))
        self.assertEqual(mapping[f2], os.path.join("assets", "pic_1.png"))

    def test_missing_files_skipped(self):
        mapping = utils.copy_files_to_export(["/no/such/file.png"], self.export_dir)
        self.assertEqual(mapping, {})


if __name__ == "__main__":
    unittest.main()
