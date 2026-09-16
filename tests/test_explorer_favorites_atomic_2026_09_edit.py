"""explorer_favorites 的 JSON 必须原子写：写一半失败不能把原文件截成空/半截。

审查发现 _save 直接 `_PATH.open("w")` 截断重写；崩溃/磁盘满时收藏全丢。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('STELLAR_DATA_DIR', tempfile.mkdtemp(prefix='fav_atomic_'))

import explorer_favorites as fav  # noqa: E402


class TestFavoritesAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fav_')
        self.path = Path(self.tmp.name) / 'favorites.json'
        self.path.write_text(json.dumps(["/a"]), encoding='utf-8')
        self._orig_path, self._orig_cache = fav._PATH, fav._cache
        fav._PATH = self.path
        fav._cache = None

    def tearDown(self):
        fav._PATH, fav._cache = self._orig_path, self._orig_cache
        self.tmp.cleanup()

    def _on_disk(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_replace_failure_leaves_original_intact(self):
        with mock.patch.object(os, 'replace', side_effect=OSError('disk full')):
            fav.add("/b")
        self.assertEqual(self._on_disk(), ["/a"], "写入失败却改坏了原文件")
        # 临时文件不能残留
        leftovers = [p.name for p in Path(self.tmp.name).iterdir()
                     if p.name != 'favorites.json']
        self.assertEqual(leftovers, [])

    def test_normal_save_round_trips(self):
        fav.add("/b")
        self.assertEqual(self._on_disk(), ["/a", "/b"])
        fav.remove("/a")
        self.assertEqual(self._on_disk(), ["/b"])
        leftovers = [p.name for p in Path(self.tmp.name).iterdir()
                     if p.name != 'favorites.json']
        self.assertEqual(leftovers, [])


if __name__ == '__main__':
    unittest.main()
