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



    def test_prose_followed_by_json_tool_call_rejected(self):
        content = (
            "Let's start by listing the available files in the project.\n"
            "\n"
            '{"name": "list_files", "arguments": {}}'
        )
        calls = self.parse(content)
        self.assertEqual(calls, [])

    def test_prose_followed_by_unknown_tool_rejected(self):
        content = (
            "I'll run a command.\n\n"
            '{"name": "run_shell", "arguments": {}}'
        )
        self.assertEqual(self.parse(content), [])

    def test_prose_followed_by_ambiguous_json_rejected(self):
        content = (
            "Choose one of these.\n\n"
            '{"name": "list_files", "arguments": {}}\n'
            '{"name": "run_tests", "arguments": {}}'
        )
        self.assertEqual(self.parse(content), [])

    def test_inline_json_in_prose_rejected(self):
        content = (
            'For example, {"name": "list_files", '
            '"arguments": {}} would list files.'
        )
        self.assertEqual(self.parse(content), [])

if __name__ == "__main__":
    unittest.main()
