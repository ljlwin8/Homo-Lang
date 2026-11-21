from __future__ import annotations

import unittest

import homo_editor


class HighlightTests(unittest.TestCase):
    def spans(self, source: str):
        return {(source[a:b], kind)
                for a, b, kind in homo_editor.highlight_tokens(source)}

    def test_imported_function_call_is_function_color(self) -> None:
        spans = self.spans(
            "inm 工具\n"
            "会员制餐厅(){\n"
            "    平方(9)噗叽啪\n"
            "    最大(3, 8)噗叽啪\n"
            "    (0)人幸终\n"
            "}\n"
        )
        self.assertIn(("平方", "fn"), spans)
        self.assertIn(("最大", "fn"), spans)

    def test_constructor_and_method_calls(self) -> None:
        spans = self.spans(
            "会员制餐厅(){\n"
            "    你是一个一个一个 计 事 新建 计数器(10) 罢\n"
            "    计.加(5)噗叽啪\n"
            "}\n"
        )
        self.assertIn(("计数器", "cls"), spans)
        self.assertIn(("加", "fn"), spans)


if __name__ == "__main__":
    unittest.main()
