import unittest
from local_agent import TOOLS, parse_text_tool_call, validate_native_tool_calls


class TrailingFenceRegressionTests(unittest.TestCase):

    def parse(self, content):
        calls = parse_text_tool_call(
            {"role": "assistant", "content": content},
            TOOLS,
        )
        if not calls:
            return []
        return validate_native_tool_calls(calls, TOOLS)

    def test_trailing_fence(self):
        content = (
            '{"name":"read_file","arguments":{"path":"calculator.py"}}'
            '\n```'
        )
        calls = self.parse(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")

    def test_multiple_calls_and_deferred_finish(self):
        content = (
            '{"name":"read_file","arguments":{"path":"calculator.py"}}'
            '\n{"name":"run_tests","arguments":{}}'
            '\n{"name":"finish","arguments":'
            '{"summary":"Done","unfinished":[],"next_actions":[]}}'
            '\n```'
        )
        calls = self.parse(content)
        self.assertEqual(
            [call["function"]["name"] for call in calls],
            ["read_file", "run_tests"],
        )

    def test_trailing_fence_does_not_allow_prose(self):
        content = (
            '{"name":"read_file","arguments":{"path":"calculator.py"}}'
            '\nThis is not JSON\n```'
        )
        self.assertEqual(self.parse(content), [])

    def test_trailing_fence_does_not_allow_unknown_tool(self):
        content = (
            '{"name":"delete_file","arguments":{"path":"calculator.py"}}'
            '\n```'
        )
        self.assertEqual(self.parse(content), [])

    def test_unclosed_opening_json_fence_rejected(self):
        content = (
            '```json\n'
            '{"name":"read_file","arguments":{"path":"calculator.py"}}'
        )
        self.assertEqual(self.parse(content), [])


if __name__ == "__main__":
    unittest.main()
