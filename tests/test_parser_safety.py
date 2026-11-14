import json
import unittest

from local_agent import parse_text_tool_call


ALLOWED = [
    {"function": {"name": name}}
    for name in ("list_files", "read_file", "finish")
]


def call(name, arguments):
    return json.dumps({
        "name": name,
        "arguments": arguments
    })


class ParserSafetyTests(unittest.TestCase):

    def test_single_read(self):
        result = parse_text_tool_call(
            {"content": call("read_file", {"path": "calculator.py"})},
            ALLOWED
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["function"]["name"], "read_file")

    def test_multiple_reads(self):
        content = "\n".join([
            call("list_files", {}),
            call("read_file", {"path": "calculator.py"})
        ])
        result = parse_text_tool_call({"content": content}, ALLOWED)
        self.assertEqual(
            [x["function"]["name"] for x in result],
            ["list_files", "read_file"]
        )

    def test_premature_finish_deferred(self):
        content = "\n".join([
            call("read_file", {"path": "calculator.py"}),
            call("finish", {"summary": "I read the file"})
        ])
        result = parse_text_tool_call({"content": content}, ALLOWED)
        self.assertEqual(
            [x["function"]["name"] for x in result],
            ["read_file"]
        )

    def test_unknown_tool_rejected(self):
        result = parse_text_tool_call(
            {"content": call("delete_file", {"path": "calculator.py"})},
            ALLOWED
        )
        self.assertEqual(result, [])

    def test_mixed_unknown_tool_rejected(self):
        content = "\n".join([
            call("read_file", {"path": "calculator.py"}),
            call("delete_file", {"path": "calculator.py"})
        ])
        self.assertEqual(
            parse_text_tool_call({"content": content}, ALLOWED),
            []
        )

    def test_malformed_json_rejected(self):
        self.assertEqual(
            parse_text_tool_call({"content": '{"name":'}, ALLOWED),
            []
        )

    def test_excessive_calls_rejected(self):
        content = "\n".join([call("list_files", {})] * 9)
        self.assertEqual(
            parse_text_tool_call({"content": content}, ALLOWED),
            []
        )

    def test_extra_fields_rejected(self):
        content = json.dumps({
            "name": "read_file",
            "arguments": {},
            "override_permissions": True
        })
        self.assertEqual(
            parse_text_tool_call({"content": content}, ALLOWED),
            []
        )


if __name__ == "__main__":
    unittest.main()
