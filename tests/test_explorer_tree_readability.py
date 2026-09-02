"""本地 Explorer 深层目录的可读性改造（不碰真实网络/系统设置）。

用户反馈：多层级文件树"有点丑、不直观"——截图里近一半的行是 .DS_Store，
六层缩进又没有任何视觉线索。这里覆盖三项：
- 系统垃圾文件独立过滤（跟「显示隐藏文件」是两回事）
- 缩进参考线（每层一条竖线）
- 双击目录 = 进入那一层（换根），而不是把树越展越深

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_tree_readability.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app_config
        self._tmp_cfg = Path(tempfile.mkdtemp()) / "cfg.json"
        self._orig = app_config.get_config_path
        app_config.get_config_path = lambda: self._tmp_cfg
        self._tmp = tempfile.TemporaryDirectory()
        self._panels = []

    def tearDown(self):
        import app_config
        # 先彻底销毁面板再删临时目录：QFileSystemModel 的监视器还盯着那些
        # 目录时把目录删掉，Qt 会在半销毁的对象上触发回调直接 abort
        # （test_editor_tabs 里踩过同一个坑）。
        from PyQt6.QtCore import QEvent
        for p in self._panels:
            try:
                p.model.setRootPath("")
                p.setParent(None)
                p.deleteLater()
            except RuntimeError:
                pass
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        app_config.get_config_path = self._orig
        self._tmp.cleanup()

    def _panel(self):
        from explorer_widget import ExplorerPanel
        p = ExplorerPanel(theme={})
        p.refresh = lambda: None
        self._panels.append(p)
        return p

    def assertSamePath(self, a, b, msg=None):   # noqa: N802 — 与 unittest 同风格
        """路径比较必须归一化：Windows 上 Qt 给的是正斜杠长路径，而 Python
        的 tempfile 给的是反斜杠 + RUNNER~1 这种 8.3 短名，直接比必然不等
        （CI 上实翻过）。realpath 负责展开短名，normcase 抹平大小写。"""
        def norm(x):
            return os.path.normcase(os.path.realpath(str(x))).replace("\\", "/")
        self.assertEqual(norm(a), norm(b), msg)


class TestJunkFilter(_Base):
    def _proxy(self):
        from explorer_widget import _DotFileProxy
        return _DotFileProxy

    def test_junk_names_recognised(self):
        is_junk = self._proxy().is_junk
        for name in ('.DS_Store', 'Thumbs.db', 'desktop.ini', '__pycache__',
                     '._resources', 'module.pyc'):
            self.assertTrue(is_junk(name), name)

    def test_real_dotfiles_are_not_junk(self):
        """开着「显示隐藏文件」是为了看这些，不能被垃圾过滤连坐。"""
        is_junk = self._proxy().is_junk
        for name in ('.gitignore', '.env', '.gitlab-ci.yml', '.git',
                     'application.yml', 'pom.xml'):
            self.assertFalse(is_junk(name), name)

    def test_toggle_persists_and_reaches_the_proxy(self):
        import app_config
        p = self._panel()
        self.assertTrue(p.is_hiding_junk(), "默认隐藏")
        p.set_hide_junk(False)
        self.assertFalse(p.is_hiding_junk())
        self.assertFalse(p._proxy._hide_junk)
        self.assertIs(app_config.read_config().get(p.CONFIG_KEY_HIDE_JUNK),
                      False)
        p2 = self._panel()
        self.assertFalse(p2.is_hiding_junk(), "重开面板要记得上次的选择")

    def test_junk_rows_really_disappear_from_the_tree(self):
        """不只是分类函数对——模型里真的不能再出现这些行。"""
        p = self._panel()
        root = Path(self._tmp.name)
        (root / ".DS_Store").write_bytes(b"junk")
        (root / ".gitignore").write_text("x", encoding="utf-8")
        (root / "pom.xml").write_text("x", encoding="utf-8")
        p.set_show_hidden(True)
        p.set_root_path(str(root))
        for _ in range(20):
            self.app.processEvents()

        def visible_names():
            src_root = p.model.index(str(root))
            proxy_root = p._proxy.mapFromSource(src_root)
            return {p._proxy.fileName(p._proxy.index(r, 0, proxy_root))
                    for r in range(p._proxy.rowCount(proxy_root))}

        names = visible_names()
        self.assertNotIn(".DS_Store", names, "垃圾行该被过滤掉")
        self.assertIn(".gitignore", names, "真隐藏文件不能被连坐")
        self.assertIn("pom.xml", names)

        p.set_hide_junk(False)
        for _ in range(20):
            self.app.processEvents()
        self.assertIn(".DS_Store", visible_names(), "关掉开关就该看得见")

    def test_junk_hidden_independently_of_hidden_files(self):
        """两个开关互不影响：显示隐藏文件时 .DS_Store 仍该被挡掉。"""
        p = self._panel()
        p.set_show_hidden(True)
        self.assertTrue(p._proxy._hide_junk)
        self.assertFalse(p._proxy._hide_dot)


class TestIndentGuides(_Base):
    """参考线的几何：每个祖先层一条，等距递增。

    只测纯计算，不去 patch QTreeView.drawRow —— 给 sip 类打方法补丁会污染
    整个进程的方法表，后面用到嵌套事件循环的用例会直接 abort（实测踩过）。
    """

    def _deep_index(self, panel):
        root = Path(self._tmp.name)
        deep = root / "src" / "main" / "resources"
        deep.mkdir(parents=True)
        (deep / "application.yml").write_text("a: 1", encoding="utf-8")
        panel.set_root_path(str(root))
        for _ in range(20):
            self.app.processEvents()
        idx = panel.model.index(str(deep / "application.yml"))
        return panel._proxy.mapFromSource(idx)

    def test_one_guide_per_ancestor_level(self):
        p = self._panel()
        idx = self._deep_index(p)
        step = p.tree_view.indentation()
        xs = p.tree_view.indent_guide_xs(idx, 0)
        # src / main / resources 三层祖先 → 三条线
        self.assertEqual(xs, [step // 2, step + step // 2, 2 * step + step // 2])

    def test_top_level_row_has_no_guides(self):
        p = self._panel()
        root = Path(self._tmp.name)
        (root / "pom.xml").write_text("x", encoding="utf-8")
        p.set_root_path(str(root))
        for _ in range(20):
            self.app.processEvents()
        idx = p._proxy.mapFromSource(p.model.index(str(root / "pom.xml")))
        self.assertEqual(p.tree_view.indent_guide_xs(idx, 0), [],
                         "根下第一层不该画线")

    def test_guides_follow_the_row_offset(self):
        p = self._panel()
        idx = self._deep_index(p)
        step = p.tree_view.indentation()
        self.assertEqual(p.tree_view.indent_guide_xs(idx, 10),
                         [10 + step // 2, 10 + step + step // 2,
                          10 + 2 * step + step // 2])

    def test_theme_sets_a_guide_colour(self):
        p = self._panel()
        self.assertIsNotNone(p.tree_view._indent_guide_color,
                             "主题应用后要有参考线颜色，否则根本不画")


class TestDoubleClickEntersFolder(_Base):
    """双击目录的行为可选；无论哪种模式，都必须有路回上一级。

    用户实测：第一版默认换根、面板里又没有「上一级」，人直接被困在
    子目录里出不来。
    """

    def test_expands_in_place_by_default(self):
        p = self._panel()
        root = Path(self._tmp.name)
        sub = root / "aiem-arranger"
        sub.mkdir()
        p.set_root_path(str(root))
        self.app.processEvents()

        self.assertFalse(p.is_double_click_enter(), "默认不换根")
        idx = p._proxy.mapFromSource(p.model.index(str(sub)))
        p._on_double_click(idx)
        self.assertSamePath(p._current_path, root, "默认双击不该换根")
        self.assertTrue(p.tree_view.isExpanded(idx), "默认双击应就地展开")

    def test_double_click_folder_changes_root_when_enabled(self):
        p = self._panel()
        p.set_double_click_enter(True)
        root = Path(self._tmp.name)
        sub = root / "aiem-arranger"
        sub.mkdir()
        p.set_root_path(str(root))
        self.app.processEvents()

        idx = p._proxy.mapFromSource(p.model.index(str(sub)))
        p._on_double_click(idx)
        self.assertSamePath(p._current_path, sub,
                            "开了开关才进到那一层")

    def test_toggle_persists(self):
        p = self._panel()
        p.set_double_click_enter(True)
        p2 = self._panel()
        self.assertTrue(p2.is_double_click_enter(), "选择要记住")

    def test_go_up_gets_out_of_a_subfolder(self):
        p = self._panel()
        p.set_double_click_enter(True)
        root = Path(self._tmp.name)
        sub = root / "aiem-arranger" / "src"
        sub.mkdir(parents=True)
        p.set_root_path(str(sub))
        self.app.processEvents()

        self.assertTrue(p.go_up())
        self.assertSamePath(p._current_path, sub.parent, "得能退回上一级")
        self.assertTrue(p.up_btn.isEnabled())

    def test_up_button_disabled_at_filesystem_root(self):
        p = self._panel()
        p.set_root_path(os.path.abspath(os.sep))
        self.app.processEvents()
        self.assertFalse(p.go_up(), "已经在根上，没得可退")
        self.assertFalse(p.up_btn.isEnabled(), "点了没反应的按钮该置灰")

    def test_double_click_file_still_opens_it(self):
        p = self._panel()
        root = Path(self._tmp.name)
        f = root / "pom.xml"
        f.write_text("<project/>", encoding="utf-8")
        p.set_root_path(str(root))
        self.app.processEvents()

        opened = []
        p.file_edit_requested.connect(lambda path, line: opened.append(path))
        idx = p._proxy.mapFromSource(p.model.index(str(f)))
        p._on_double_click(idx)
        self.assertEqual(len(opened), 1)
        self.assertSamePath(opened[0], f)
        self.assertSamePath(p._current_path, root, "打开文件不该换根")


if __name__ == "__main__":
    unittest.main()
