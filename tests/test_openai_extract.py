# -*- coding: utf-8 -*-
"""openai_server 屏幕响应提取纯函数的单元测试

运行方式（标准库 unittest，无需 pytest）：
    python3 -m unittest discover tests -v

预期值来源：重构前用原 OpenAIRequestHandler._extract_response_from_screen
在相同样例上跑出的实际结果（characterization tests），保证重构行为一致。
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# 确保可以从仓库根目录 import openai_server
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openai_server
from openai_server import (
    extract_response_from_screen,
    _is_empty_prompt_line,
    _is_input_line,
    _is_extract_ui_line,
    _has_response_marker,
    _clean_response_line,
    _clean_final_result,
    _find_empty_prompt_line,
    _find_input_boundary,
    _find_last_marker_line,
)


def extract(screen: str, input_text: str = "") -> str:
    """调用提取函数并吞掉调试 print 输出，保持测试日志干净"""
    with redirect_stdout(io.StringIO()):
        return extract_response_from_screen(screen, input_text)


class TestEmptyPromptLine(unittest.TestCase):
    """空输入提示符行检测"""

    def test_bare_prompts(self):
        for p in ('>', '›', '❯'):
            self.assertTrue(_is_empty_prompt_line(p))
            self.assertTrue(_is_empty_prompt_line(p + ' '))

    def test_short_line_counts_as_empty_prompt(self):
        # 边界行为：提示符开头且总长 <= 3 也算空提示符（如 "> q"）
        self.assertTrue(_is_empty_prompt_line('> q'))
        self.assertTrue(_is_empty_prompt_line('>>x'))

    def test_non_prompts(self):
        self.assertFalse(_is_empty_prompt_line(''))
        self.assertFalse(_is_empty_prompt_line('> 这是一条用户输入'))
        self.assertFalse(_is_empty_prompt_line('hello'))


class TestInputLine(unittest.TestCase):
    """用户输入行检测（提示符后有内容，总长 > 3）"""

    def test_input_lines(self):
        self.assertTrue(_is_input_line('> 问题'))
        self.assertTrue(_is_input_line('❯ 提示符输入'))

    def test_short_or_plain_lines(self):
        self.assertFalse(_is_input_line('> q'))  # len == 3，归为空提示符
        self.assertFalse(_is_input_line('普通文本'))
        self.assertFalse(_is_input_line(''))


class TestUiLine(unittest.TestCase):
    """UI / thinking 行检测"""

    def test_ui_keywords(self):
        self.assertTrue(_is_extract_ui_line('? for shortcuts'))
        self.assertTrue(_is_extract_ui_line('✻ Synthesizing… (esc to interrupt)'))
        self.assertTrue(_is_extract_ui_line('Thinking hard'))
        self.assertTrue(_is_extract_ui_line('⎿ Read image.png'))
        self.assertTrue(_is_extract_ui_line('How is Claude doing this session? (optional)'))
        self.assertTrue(_is_extract_ui_line('Press up to edit queued messages'))

    def test_decoration_lines(self):
        self.assertTrue(_is_extract_ui_line('──────────'))
        self.assertTrue(_is_extract_ui_line('━━━ ━━━'))
        self.assertTrue(_is_extract_ui_line(')'))  # 纯括号/装饰字符行

    def test_normal_lines(self):
        self.assertFalse(_is_extract_ui_line('这是正常的回答内容'))
        self.assertFalse(_is_extract_ui_line('plain answer text'))


class TestResponseMarker(unittest.TestCase):
    """响应标记检测与清理"""

    def test_has_marker(self):
        for m in ('⏺', '●', '✻', '✶', '*', '·', '•', '✳'):
            self.assertTrue(_has_response_marker(m + ' 内容'))

    def test_no_marker(self):
        self.assertFalse(_has_response_marker('> 输入'))
        self.assertFalse(_has_response_marker('普通行'))
        self.assertFalse(_has_response_marker(''))

    def test_clean_response_line(self):
        self.assertEqual(_clean_response_line('⏺ 回答内容'), '回答内容')
        self.assertEqual(_clean_response_line('* 星号回答'), '星号回答')
        self.assertEqual(_clean_response_line('●● double'), 'double')


class TestCleanFinalResult(unittest.TestCase):
    """最终结果清理：孤立括号移除"""

    def test_empty(self):
        self.assertEqual(_clean_final_result(''), '')

    def test_lone_paren_line_removed(self):
        self.assertEqual(_clean_final_result('内容\n)'), '内容')
        self.assertEqual(_clean_final_result('内容\n）'), '内容')

    def test_trailing_spaced_paren_removed(self):
        self.assertEqual(_clean_final_result('详见文档  )'), '详见文档')

    def test_trailing_paren_stripped(self):
        # 注意：合法的右括号也会被剥掉（原逻辑如此，characterization）
        self.assertEqual(_clean_final_result('答案是 (42)'), '答案是 (42')

    def test_normal_text_untouched(self):
        self.assertEqual(_clean_final_result('正常内容'), '正常内容')


class TestFindHelpers(unittest.TestCase):
    """行定位辅助函数"""

    def test_find_empty_prompt_from_bottom(self):
        lines = ['>', '内容', '> ']
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_find_empty_prompt_line(lines), 2)

    def test_find_empty_prompt_not_found(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_find_empty_prompt_line(['内容', '更多内容']), -1)

    def test_find_input_boundary_input_line(self):
        lines = ['> 这是输入内容', '⏺ 回答', '>']
        with redirect_stdout(io.StringIO()):
            image, inp = _find_input_boundary(lines, 2)
        self.assertEqual(image, -1)
        self.assertEqual(inp, 0)

    def test_find_input_boundary_image_marker_wins(self):
        # 图片标记（⎿ / [image #）在向上扫描中先于输入行命中
        lines = ['> 分析这张图片输入', '⎿ Read image.png', '⏺ 回答', '>']
        with redirect_stdout(io.StringIO()):
            image, inp = _find_input_boundary(lines, 3)
        self.assertEqual(image, 1)
        self.assertEqual(inp, -1)

    def test_find_input_boundary_prompt_at_top(self):
        # empty_prompt_line <= 0 时不扫描
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_find_input_boundary(['>'], 0), (-1, -1))

    def test_find_last_marker_line(self):
        lines = ['⏺ 第一个', '文本', '● 最后一个', '尾巴']
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_find_last_marker_line(lines), 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_find_last_marker_line(['无标记', '> 输入']), -1)


class TestExtractResponseFromScreen(unittest.TestCase):
    """调度函数端到端：预期值全部来自重构前原函数的实际输出"""

    def test_empty_screen(self):
        self.assertEqual(extract(''), '')

    def test_input_based_with_marker(self):
        # 策略1：输入行 -> 标记响应 -> 空提示符
        screen = ("> 你好，请介绍一下自己\n\n"
                  "⏺ 我是 Claude，一个 AI 助手。\n"
                  "  可以帮你写代码。\n\n"
                  "> \n  ? for shortcuts")
        self.assertEqual(extract(screen), "我是 Claude，一个 AI 助手。\n可以帮你写代码。")

    def test_image_marker_boundary(self):
        # 策略1：以 ⎿ 图片标记作为用户输入结束的分界线
        screen = ("> 看看这张图\n\n"
                  "  ⎿ Read image1.png\n\n"
                  "⏺ 这是一张猫的图片。\n\n>")
        self.assertEqual(extract(screen), "这是一张猫的图片。")

    def test_image_marker_priority_over_input_line(self):
        # 图片标记优先：输入行回显（含 [Image #1]）不会混进结果
        screen = ("> 分析这张图 [Image #1]\n"
                  "  ⎿ Read image.png (120KB)\n\n"
                  "⏺ 图片显示一只狗。\n\n>")
        self.assertEqual(extract(screen), "图片显示一只狗。")

    def test_marker_based_fallback(self):
        # 策略2：没有空提示符，从最后一个响应标记向下提取
        screen = "⏺ 答案是 42。\n  详细解释如下。"
        self.assertEqual(extract(screen), "答案是 42。\n详细解释如下。")

    def test_thinking_only_screen_yields_empty(self):
        # 只有 thinking 动画 + 空提示符 -> 空响应
        screen = "✶ Pondering… (esc to interrupt)\n> "
        self.assertEqual(extract(screen), "")

    def test_thinking_line_filtered_from_response(self):
        # thinking 行夹在输入和回答之间，被过滤掉
        screen = ("> 问题\n\n"
                  "✻ Thinking…\n\n"
                  "⏺ 回答内容\n  第二行\n\n>")
        self.assertEqual(extract(screen), "回答内容\n第二行")

    def test_session_feedback_filtered(self):
        screen = ("> 问题\n\n⏺ 回答X\n\n"
                  "  How is Claude doing this session? (optional)\n"
                  "  1: bad  2: fine  3: good  0: dismiss\n\n>")
        self.assertEqual(extract(screen), "回答X")

    def test_queued_messages_filtered(self):
        screen = "> 问题\n\n⏺ 回答Y\n  Press up to edit queued messages\n\n>"
        self.assertEqual(extract(screen), "回答Y")

    def test_multiline_with_blank_preserved(self):
        # 响应中间的空行保留为空字符串
        screen = "> 问题\n\n⏺ 第一段\n\n  第二段继续\n\n>"
        self.assertEqual(extract(screen), "第一段\n\n第二段继续")

    def test_prompt_variant_arrow(self):
        screen = "❯ 问\n\n● 用 ❯ 提示符的回答\n\n❯"
        self.assertEqual(extract(screen), "用 ❯ 提示符的回答")

    def test_no_response_between_input_and_prompt(self):
        # 输入和空提示符之间没有内容 -> 空
        screen = "> q\n\n>"
        self.assertEqual(extract(screen), "")

    def test_trailing_paren_stripped(self):
        screen = "> 测试\n\n⏺ 答案是 (42)\n\n>"
        self.assertEqual(extract(screen), "答案是 (42")

    def test_lone_paren_suffix_line(self):
        screen = "> 问题\n\n⏺ 第一行内容\n  详见文档  )\n\n>"
        self.assertEqual(extract(screen), "第一行内容\n详见文档")

    def test_star_and_dot_markers_short_input(self):
        # "> q" 长度 3 被当作空提示符，策略1失效，落入策略2：
        # 最后一个标记是 "· 圆点回答"，所以星号行不在结果中（characterization）
        screen = "> q\n\n* 星号回答\n· 圆点回答\n\n>"
        self.assertEqual(extract(screen), "圆点回答")

    def test_marker_priority_last_marker_wins(self):
        # 策略2 取最底部的标记行；后面的 thinking 标记行会让真实回答丢失
        # （已知脆弱行为，characterization）
        screen = ("> q\n\n⏺ 真实回答\n"
                  "  ✻ Synthesizing… (esc to interrupt)\n"
                  "  ? for shortcuts\n  ──────────\n\n>")
        self.assertEqual(extract(screen), "")

    def test_marker_fallback_leaks_old_answer(self):
        # 策略1找不到分界线时回退策略2，旧回答+新输入会一起被提取
        # （已知脆弱行为，characterization）
        screen = "⏺ 旧回答\n\n> 新问题\n\n>"
        self.assertEqual(extract(screen), "旧回答\n\n> 新问题")

    def test_marker_based_stops_at_prompt(self):
        screen = "⏺ 回答A\n> a\n\n后面的内容不该出现"
        # "> a" 长度 3 视为空提示符，策略2在此截断
        self.assertEqual(extract(screen), "回答A")

    def test_input_text_param_ignored(self):
        # input_text 参数在原逻辑中未被使用，传入任何值结果一致
        screen = "> 问题\n\n⏺ 回答内容\n\n>"
        self.assertEqual(extract(screen, "问题"), extract(screen, ""))


class TestWrapperDelegation(unittest.TestCase):
    """原方法已是薄包装：与模块级纯函数结果一致（方法体不使用 self）"""

    def test_wrapper_matches_pure_function(self):
        samples = [
            "",
            "> 你好，请介绍一下自己\n\n⏺ 我是助手。\n\n>",
            "⏺ 答案是 42。",
            "✶ Pondering… (esc to interrupt)\n> ",
        ]
        for screen in samples:
            with redirect_stdout(io.StringIO()):
                via_method = openai_server.OpenAIRequestHandler._extract_response_from_screen(
                    None, screen, "")
                via_func = extract_response_from_screen(screen, "")
            self.assertEqual(via_method, via_func)


if __name__ == '__main__':
    unittest.main()
