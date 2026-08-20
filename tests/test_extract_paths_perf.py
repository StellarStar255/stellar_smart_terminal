"""extract_file_paths 在长无空格文本上的性能回归测试

背景：终端里给 Claude 贴一段 base64（图片/附件）后回车，GUI 线程要卡好几百
毫秒。根因是 _RE_UNIX_ABS_PATH 的 `/[^\\s...]+\\.ext`——字符类不排除 `/`，
于是每个 `/` 都要把后面整段文本回溯一遍，在无空格长串上退化成 O(n²)：
160KB base64 实测 585ms，而且一个路径都匹配不到，纯属白烧。

修复：字符类排除 `/`（路径段本来就不含分隔符），改为逐段匹配
`(?:/[^/\\s...]+)+\\.ext`，复杂度回到线性，匹配语义不变。
"""
import os
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import extract_file_paths


class TestExtractPathsPerformance(unittest.TestCase):
    def test_enter_with_large_base64_does_not_scan(self):
        """回车提交一大段 base64：GUI 线程侧不得扫描整段内容

        这是用户实际路径：终端里粘贴 base64 后回车 → input_recorded 同步
        直连 → SessionManager.add_input。修复前这里会同步扫描整段文本
        （80KB 约 170ms 且二次增长），现在超长输入延迟到会话结束再提取。

        断言「没有调用扫描函数」而不是「耗时小于 N 毫秒」：绝对墙钟阈值和
        机器速度绑定，CI runner 比开发机慢 2~3 倍就会崩红（本项目已因此吃过
        一次残缺 release）。行为断言在任何机器上都成立。
        """
        from unittest.mock import patch
        from session_manager import SessionManager
        sm = SessionManager()
        sm.sessions_dir = Path(tempfile.mkdtemp())
        try:
            sm.create_session('claude')
            blob = base64.b64encode(os.urandom(60000)).decode()
            with patch('session_manager.extract_file_paths') as scan:
                sm.add_input(blob)
            scan.assert_not_called()
        finally:
            sm._save_executor.shutdown(wait=False)

    def test_deferred_paths_filled_at_session_end(self):
        """延迟提取不能丢数据：会话结束时补齐超长输入里的路径"""
        from session_manager import SessionManager
        sm = SessionManager()
        sm.sessions_dir = Path(tempfile.mkdtemp())
        try:
            sm.create_session('claude')
            blob = base64.b64encode(os.urandom(60000)).decode()
            real = str(Path(__file__).resolve())
            entry = sm.add_input(f"{blob} {real}")
            self.assertEqual(entry.files, [])  # 录入时跳过
            sm.end_session()
            self.assertIn(real, entry.files)
        finally:
            sm._save_executor.shutdown(wait=False)

    def test_short_input_extracts_inline(self):
        """普通短输入仍在录入时立即提取（行为不变）"""
        from session_manager import SessionManager
        sm = SessionManager()
        sm.sessions_dir = Path(tempfile.mkdtemp())
        try:
            sm.create_session('claude')
            real = str(Path(__file__).resolve())
            entry = sm.add_input(f'open {real}')
            self.assertIn(real, entry.files)
        finally:
            sm._save_executor.shutdown(wait=False)

    def test_short_input_scans_inline(self):
        """普通短输入仍在录入时立即扫描（行为不变，与上面的跳过形成对照）"""
        from unittest.mock import patch
        from session_manager import SessionManager
        sm = SessionManager()
        sm.sessions_dir = Path(tempfile.mkdtemp())
        try:
            sm.create_session('claude')
            with patch('session_manager.extract_file_paths',
                       return_value=set()) as scan:
                sm.add_input('ls -la')
            scan.assert_called_once()
        finally:
            sm._save_executor.shutdown(wait=False)

    def test_scaling_is_not_quadratic(self):
        """规模 ×4，耗时不应涨到 8 倍以上（二次方判据）

        刻意拉大规模差并留足余量，避免机器负载把测试抖成 flaky（CI 崩红会
        留下缺产物的残缺 release）：
        · 线性（修复后）实测约 4~5 倍；
        · 二次方（修复前）实测约 13 倍。
        8 倍阈值离两侧都很远，best-of-5 再压掉单次抖动。
        """
        blob = base64.b64encode(os.urandom(180000)).decode()

        def timed(n, repeat=3):
            """单次调用耗时。自适应批量：修复后一次扫描不到 1ms，直接测单次
            会被调度噪声淹没（噪声比信号还大，比例随机化 → flaky）。这里把
            批量放大到每次测量至少 20ms 再摊平，慢实现下 iters 自然停在 1。"""
            s = blob[:n]
            best = float('inf')
            for _ in range(repeat):
                iters = 1
                while True:
                    t0 = time.perf_counter()
                    for _ in range(iters):
                        extract_file_paths(s, validate_exists=False)
                    dt = time.perf_counter() - t0
                    if dt >= 0.02 or iters >= 4096:
                        break
                    iters *= 4
                best = min(best, dt / iters)
            return best

        t_small = timed(40000)
        t_large = timed(160000)
        self.assertLess(t_large, t_small * 8.0,
                        f"疑似二次方：40KB={t_small*1000:.1f}ms "
                        f"160KB={t_large*1000:.1f}ms "
                        f"（{t_large/t_small:.1f} 倍）")


class TestExtractPathsCorrectness(unittest.TestCase):
    """修复不能改变匹配语义"""

    def test_absolute_paths_still_matched(self):
        text = "see /Users/me/project/main.py and /var/data/report.json for details"
        found = extract_file_paths(text, validate_exists=False)
        self.assertIn('/Users/me/project/main.py', found)
        self.assertIn('/var/data/report.json', found)

    def test_relative_and_windows_paths_still_matched(self):
        found = extract_file_paths("edit ./src/utils.py now", validate_exists=False)
        self.assertIn('./src/utils.py', found)
        found = extract_file_paths(r'open C:\work\notes.md please',
                                   validate_exists=False)
        self.assertIn(r'C:\work\notes.md', found)

    def test_deep_path_matched_whole(self):
        """多层深路径必须整条匹配（逐段匹配不能只取最后一段）"""
        p = '/a/b/c/d/e/f/g/deep_file.py'
        found = extract_file_paths(f'ref {p} end', validate_exists=False)
        self.assertIn(p, found)

    def test_path_with_spaces_stops_at_space(self):
        found = extract_file_paths('run /usr/share/doc/readme.md --flag',
                                   validate_exists=False)
        self.assertIn('/usr/share/doc/readme.md', found)

    def test_base64_yields_no_bogus_paths(self):
        """base64 里没有真实路径，不应产出候选（避免无谓的 exists() 磁盘调用）"""
        blob = base64.b64encode(os.urandom(4000)).decode()
        self.assertEqual(extract_file_paths(blob, validate_exists=False), set())


if __name__ == '__main__':
    unittest.main()
