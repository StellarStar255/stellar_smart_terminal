# -*- coding: utf-8 -*-
"""静默 except 审计的守卫测试。

审查发现全仓 250 处宽泛 except、64 处只有 pass 的块，真失败被吞得无声无息。
本批把"隐藏真失败"的改成 logger.debug/warning(exc_info=True)，其余确属预期的
（Qt 对象已销毁、已断开、chmod 尽力而为…）保留 pass 但必须带注释说明预期。

守卫：以后再新增一个"裸 except: pass"（无注释）就在这里报错，逼着写清理由。
顺带覆盖 explorer 本地复制两处修正：软链文件复制保留链接；线程池随面板关闭。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_silent_except_audit.py -v
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AUDITED = [
    'remote_explorer_widget.py', 'ssh_session.py', 'ssh_control.py', 'file_editor.py',
    'terminal_widget.py', 'terminal_backend.py', 'terminal_render.py', 'explorer_widget.py',
    'explorer_common.py', 'explorer_favorites.py', 'utils.py', 'openai_server.py',
    'shell_integration.py', 'ai_completion.py', 'git_widget.py', 'git_manager.py',
    'remote_bookmarks.py', 'i18n.py', 'app.py',
]

_EXC = re.compile(r'^\s*except([^:\n]*):\s*(#.*)?$')
_PASS = re.compile(r'^\s*pass\s*(#.*)?$')


def _pass_blocks(path):
    lines = open(path, encoding='utf-8').read().split('\n')
    out = []
    for i, line in enumerate(lines[:-1]):
        m = _EXC.match(line)
        if m and _PASS.match(lines[i + 1]):
            has_comment = bool(m.group(2)) or bool(_PASS.match(lines[i + 1]).group(1))
            broad = m.group(1).strip() in ('', 'Exception', 'BaseException')
            out.append((i + 1, m.group(1).strip(), has_comment, broad))
    return out


class TestNoSilentPass(unittest.TestCase):
    def test_every_pass_block_is_narrow_and_explained(self):
        offenders = []
        for f in AUDITED:
            for lineno, exc, has_comment, broad in _pass_blocks(os.path.join(ROOT, f)):
                # 唯一允许的宽泛 pass：fork 出的子进程里任何异常都只能走 os._exit
                if broad and not (f == 'terminal_backend.py' and exc == 'BaseException'):
                    offenders.append(f"{f}:{lineno} 宽泛 except {exc or '<bare>'}: pass")
                elif not has_comment:
                    offenders.append(f"{f}:{lineno} except {exc}: pass 没有注释说明预期")
        self.assertEqual(offenders, [], "\n" + "\n".join(offenders))

    def test_pass_block_budget(self):
        """总数只许降不许升（新增 pass 块要么写日志要么在这里改预算并说明）"""
        total = sum(len(_pass_blocks(os.path.join(ROOT, f))) for f in AUDITED)
        self.assertLessEqual(total, 73, f"pass-only except 块增加到 {total}")


class TestExplorerLocalCopyFixes(unittest.TestCase):
    @unittest.skipIf(sys.platform == 'win32', 'symlink needs privileges on Windows')
    def test_symlinked_file_is_copied_as_link(self):
        from explorer_widget import copy_local_entry
        d = tempfile.mkdtemp(prefix='fav_ln_')
        target = os.path.join(d, 'real.txt')
        open(target, 'w').write('x' * 10)
        link = os.path.join(d, 'link.txt')
        os.symlink('real.txt', link)
        dst = os.path.join(d, 'copied.txt')
        copy_local_entry(link, dst)
        self.assertTrue(os.path.islink(dst), "软链文件被跟随成普通文件复制了")
        self.assertEqual(os.readlink(dst), 'real.txt')

    def test_local_pool_shuts_down_with_panel(self):
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QCloseEvent
        from PyQt6.QtCore import QEvent
        app = QApplication.instance() or QApplication([])
        from explorer_widget import ExplorerPanel
        panel = ExplorerPanel()
        pool = panel._local_executor()
        self.assertFalse(pool._shutdown)
        QApplication.sendEvent(panel, QCloseEvent())
        self.assertTrue(pool._shutdown, "关面板后本地线程池仍然活着")
        panel.deleteLater()
        for _ in range(3):
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
