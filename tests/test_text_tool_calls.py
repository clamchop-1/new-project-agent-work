import unittest
from local_agent import parse_text_tool_call, TOOLS

class TextToolCallTests(unittest.TestCase):

    def parse(self, content):
        return parse_text_tool_call(
            {"role": "assistant", "content": content},
            TOOLS
        )

    def test_valid_read_file(self):
        calls = self.parse(
            '{"name":"read_file","arguments":{"path":"calculator.py"}}'
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(
            calls[0]["function"]["arguments"]["path"],
            "calculator.py"
        )

    def test_unknown_tool_rejected(self):
        self.assertEqual(
            self.parse('{"name":"run_shell","arguments":{}}'),
            []
        )

    def test_multiple_commands_rejected(self):
        self.assertEqual(
            self.parse('[{"name":"read_file","arguments":{}}]'),
            []
        )

    def test_extra_fields_rejected(self):
        self.assertEqual(
            self.parse(
                '{"name":"read_file","arguments":{},'
                '"extra":"unexpected"}'
            ),
            []
        )

    def test_plain_text_rejected(self):
        self.assertEqual(self.parse("Please read calculator.py"), [])

    def test_oversized_response_rejected(self):
        self.assertEqual(self.parse("x" * 9000), [])

if __name__ == "__main__":
    unittest.main()
