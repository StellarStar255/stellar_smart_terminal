"""exporter._merge_messages 的合并语义守卫（去 deepcopy 改为原地合并后）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exporter import Exporter


def merge(messages):
    return Exporter.__new__(Exporter)._merge_messages(messages)


class TestMergeMessages(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(merge([]), [])

    def test_alternating_roles_untouched(self):
        msgs = [{'role': 'user', 'content': 'a'},
                {'role': 'assistant', 'content': 'b'}]
        self.assertEqual(merge(msgs), msgs)

    def test_text_text_merge(self):
        merged = merge([{'role': 'user', 'content': 'a'},
                        {'role': 'user', 'content': 'b'},
                        {'role': 'assistant', 'content': 'c'}])
        self.assertEqual(merged, [{'role': 'user', 'content': 'a\nb'},
                                  {'role': 'assistant', 'content': 'c'}])

    def test_list_list_merge(self):
        p1 = {'type': 'text', 'text': '1'}
        p2 = {'type': 'text', 'text': '2'}
        merged = merge([{'role': 'user', 'content': [p1]},
                        {'role': 'user', 'content': [p2]}])
        self.assertEqual(merged, [{'role': 'user', 'content': [p1, p2]}])

    def test_text_then_list_merge(self):
        img = {'type': 'image_url', 'image_url': {'url': 'u'}}
        merged = merge([{'role': 'user', 'content': 'hi'},
                        {'role': 'user', 'content': [img]}])
        self.assertEqual(merged, [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'hi'}, img]}])

    def test_list_then_text_merge(self):
        img = {'type': 'image_url', 'image_url': {'url': 'u'}}
        merged = merge([{'role': 'user', 'content': [img]},
                        {'role': 'user', 'content': 'hi'}])
        self.assertEqual(merged, [{'role': 'user', 'content': [
            img, {'type': 'text', 'text': 'hi'}]}])


if __name__ == '__main__':
    unittest.main()
