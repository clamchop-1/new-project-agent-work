import unittest

from local_agent import defer_premature_finish


def tool(name):
    return {
        "function": {
            "name": name,
            "arguments": {}
        }
    }


class NativeToolSafetyTests(unittest.TestCase):

    def test_read_then_finish(self):
        calls = [
            tool("read_file"),
            tool("finish")
        ]

        result = defer_premature_finish(calls)

        self.assertEqual(
            [c["function"]["name"] for c in result],
            ["read_file"]
        )

    def test_finish_then_read(self):
        calls = [
            tool("finish"),
            tool("read_file")
        ]

        result = defer_premature_finish(calls)

        self.assertEqual(
            [c["function"]["name"] for c in result],
            ["read_file"]
        )

    def test_finish_alone_allowed(self):
        result = defer_premature_finish([tool("finish")])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["function"]["name"], "finish")

    def test_multiple_reads_preserved(self):
        result = defer_premature_finish([
            tool("list_files"),
            tool("read_file")
        ])

        self.assertEqual(len(result), 2)

    def test_empty_calls(self):
        self.assertEqual(defer_premature_finish([]), [])


if __name__ == "__main__":
    unittest.main()
