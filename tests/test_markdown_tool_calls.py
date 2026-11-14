import unittest

from local_agent import TOOLS, parse_text_tool_call


class MarkdownToolCallTests(unittest.TestCase):

    def parse(self, content):
        return parse_text_tool_call(
            {"role": "assistant", "content": content},
            TOOLS,
        )

    def test_single_fenced_call(self):
        content = '```json\n{"name":"read_file","arguments":{"path":"calculator.py"}}\n```'
        calls = self.parse(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")

    def test_multiple_calls_with_fences(self):
        content = (
            '{"name":"read_file","arguments":{"path":"calculator.py"}}\n'
            '```\n\n'
            '{"name":"read_file","arguments":{"path":"test_calculator.py"}}\n'
            '```'
        )
        calls = self.parse(content)
        self.assertEqual(len(calls), 2)

    def test_reject_incomplete_json(self):
        self.assertEqual(
            self.parse('{"name":"read_file","arguments":'),
            [],
        )

    def test_reject_surrounding_prose(self):
        self.assertEqual(
            self.parse(
                'Please execute:\n'
                '{"name":"read_file","arguments":{"path":"calculator.py"}}'
            ),
            [],
        )

    def test_reject_unknown_tool(self):
        self.assertEqual(
            self.parse('```json\n{"name":"delete_file","arguments":{}}\n```'),
            [],
        )

    def test_reject_unclosed_fence(self):
        self.assertEqual(
            self.parse('```json\n{"name":"read_file","arguments":{"path":"calculator.py"}}'),
            [],
        )


if __name__ == "__main__":
    unittest.main()
