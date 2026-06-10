# -*- coding: utf-8 -*-
"""i18n.py 的 characterization 测试

覆盖 t() 的取值/格式化/缺键回退、set_language 的合法性校验与热切换。
模块级语言状态在 setUp/tearDown 中保存恢复，避免影响其它测试。
不需要 Qt。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import i18n
from i18n import t, set_language, get_language


class I18nBase(unittest.TestCase):
    def setUp(self):
        self._saved_lang = get_language()

    def tearDown(self):
        # 直接还原模块全局，绕过 set_language 的合法性过滤
        i18n._current_language = self._saved_lang


class TestTranslate(I18nBase):
    def test_zh_lookup(self):
        set_language("zh")
        self.assertEqual(t("toolbar.start"), "启动")

    def test_en_lookup(self):
        set_language("en")
        self.assertEqual(t("toolbar.start"), "Start")

    def test_missing_key_returns_key_itself(self):
        set_language("zh")
        self.assertEqual(t("no.such.key"), "no.such.key")

    def test_missing_key_with_kwargs_still_returns_key(self):
        # 缺键时 kwargs 被忽略（直接返回 key，不会尝试 format）
        self.assertEqual(t("no.such.key", n=3), "no.such.key")

    def test_format_kwargs_zh(self):
        set_language("zh")
        self.assertEqual(t("pasted_images.count", n=3), "共 3 张")

    def test_format_kwargs_en(self):
        set_language("en")
        self.assertEqual(t("pasted_images.count", n=3), "3 image(s)")

    def test_missing_format_kwarg_returns_unformatted(self):
        # 占位符缺参 → KeyError 被吞掉，返回带 {name} 的原文
        set_language("zh")
        self.assertEqual(t("theme.switched"), "已切换到 {name} 主题")
        self.assertEqual(t("theme.switched", wrong_kwarg=1), "已切换到 {name} 主题")

    def test_extra_kwargs_on_text_without_placeholder_ignored(self):
        set_language("zh")
        self.assertEqual(t("toolbar.start", n=99), "启动")

    def test_multiple_placeholders(self):
        set_language("en")
        self.assertEqual(
            t("explorer.open_in_editor_failed", editor="VS Code", error="boom"),
            "Cannot open in VS Code: boom",
        )


class TestSetLanguage(I18nBase):
    def test_switch_to_en_and_back(self):
        set_language("en")
        self.assertEqual(get_language(), "en")
        self.assertEqual(t("explorer.refresh"), "Refresh")
        set_language("zh")
        self.assertEqual(get_language(), "zh")
        self.assertEqual(t("explorer.refresh"), "刷新")

    def test_invalid_language_ignored(self):
        set_language("zh")
        set_language("fr")  # 不支持 → 保持原语言
        self.assertEqual(get_language(), "zh")
        set_language("")  # 空串同样被忽略
        self.assertEqual(get_language(), "zh")

    def test_hot_switch_affects_subsequent_t_calls(self):
        set_language("zh")
        before = t("shortcuts.save")
        set_language("en")
        after = t("shortcuts.save")
        self.assertEqual((before, after), ("保存", "Save"))


class TestTranslationTableShape(I18nBase):
    def test_every_entry_has_zh_and_en(self):
        # 表完整性：每个键都同时提供 zh/en（防止热切换后出现混杂语言）
        missing = [
            key
            for key, entry in i18n.TRANSLATIONS.items()
            if "zh" not in entry or "en" not in entry
        ]
        self.assertEqual(missing, [])

    def test_unknown_language_falls_back_to_zh(self):
        # 直接篡改全局为不存在的语言（绕过 set_language 校验）→ 回退 zh
        i18n._current_language = "jp"
        self.assertEqual(t("toolbar.start"), "启动")


if __name__ == "__main__":
    unittest.main()
