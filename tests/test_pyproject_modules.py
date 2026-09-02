# -*- coding: utf-8 -*-
"""pyproject.toml 的 py-modules 必须与仓库根目录的模块一致。

审查发现清单漏了 18 个模块（app_config、main_window_* 等），`pip install .`
装出来的包 import main_window 直接失败。这里把两边对上，以后新增模块忘了
登记会在测试里暴露。
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPyModulesList(unittest.TestCase):
    def test_py_modules_match_root_modules(self):
        txt = open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8").read()
        m = re.search(r'py-modules = \[\n((?:    "[^"]+",\n)+)\]', txt)
        self.assertIsNotNone(m, "pyproject.toml 里找不到 py-modules 列表")
        listed = set(re.findall(r'"([^"]+)"', m.group(1)))
        actual = {os.path.basename(f)[:-3]
                  for f in glob.glob(os.path.join(ROOT, "*.py"))}
        self.assertEqual(sorted(actual - listed), [], "py-modules 漏登记的模块")
        self.assertEqual(sorted(listed - actual), [], "py-modules 里已不存在的模块")


if __name__ == "__main__":
    unittest.main()
