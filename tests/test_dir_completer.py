"""目录历史多关键词补全（MultiKeywordCompleter）—— 与文件 quick-open 一致。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_dir_completer -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class TestMultiKeywordCompleter(_Base):
    ITEMS = [
        "/Users/a/zhiyuan_llm_8_gpu_machine_model_training",
        "/Users/a/stellar_smart_terminal",
        "/Users/a/zhiyuan_llm_ali_train",
        "/Users/a/zhiyuan_datasets_evolution_center",
    ]

    def _filtered(self, completer, query):
        completer.splitPath(query)
        return [s.rsplit("/", 1)[-1] for s in completer.model().stringList()]

    def test_single_keyword_substring(self):
        from widgets import MultiKeywordCompleter
        c = MultiKeywordCompleter(self.ITEMS)
        self.assertEqual(
            set(self._filtered(c, "llm")),
            {"zhiyuan_llm_8_gpu_machine_model_training", "zhiyuan_llm_ali_train"},
        )

    def test_multi_keyword_and_order_independent(self):
        from widgets import MultiKeywordCompleter
        c = MultiKeywordCompleter(self.ITEMS)
        a = self._filtered(c, "llm train")
        b = self._filtered(c, "train llm")
        self.assertEqual(set(a), set(b))
        # 同时含 llm 和 train
        self.assertEqual(
            set(a),
            {"zhiyuan_llm_8_gpu_machine_model_training", "zhiyuan_llm_ali_train"},
        )

    def test_case_insensitive(self):
        from widgets import MultiKeywordCompleter
        c = MultiKeywordCompleter(self.ITEMS)
        self.assertEqual(self._filtered(c, "STELLAR"), ["stellar_smart_terminal"])

    def test_empty_shows_all(self):
        from widgets import MultiKeywordCompleter
        c = MultiKeywordCompleter(self.ITEMS)
        self.assertEqual(len(self._filtered(c, "")), len(self.ITEMS))

    def test_no_match(self):
        from widgets import MultiKeywordCompleter
        c = MultiKeywordCompleter(self.ITEMS)
        self.assertEqual(self._filtered(c, "nonexistentkeyword"), [])

    def test_set_items_updates_candidates(self):
        from widgets import MultiKeywordCompleter
        c = MultiKeywordCompleter([])
        c.set_items(["/x/alpha_proj", "/x/beta_proj"])
        self.assertEqual(self._filtered(c, "beta"), ["beta_proj"])


if __name__ == "__main__":
    unittest.main()
