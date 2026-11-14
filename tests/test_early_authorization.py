import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"


def invoke(*args):
    return subprocess.run(
        [sys.executable, str(AGENT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15
    )


class EarlyAuthorizationTests(unittest.TestCase):

    def assert_blocked_early(self, result, expected):
        output = result.stdout + result.stderr

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, output)
        self.assertNotIn("LOCAL MODEL ROUTING", output)
        self.assertNotIn('"run":', output)

    def test_production_conversation_blocked(self):
        result = invoke(
            "run",
            "--project", "KronosStage",
            "--model", "auto",
            "--conversation",
            "--task", "Do not modify anything."
        )
        self.assert_blocked_early(
            result,
            "Conversation history is currently sandbox-only."
        )

    def test_conversation_with_write_path_blocked(self):
        result = invoke(
            "run",
            "--project", "LocalAgentSandbox",
            "--conversation",
            "--write-path", "calculator.py",
            "--task", "Do not modify anything."
        )
        self.assert_blocked_early(
            result,
            "Conversation history cannot be combined with write permits."
        )

    def test_conversation_with_permit_blocked(self):
        result = invoke(
            "run",
            "--project", "LocalAgentSandbox",
            "--conversation",
            "--permit", "nonexistent-permit.json",
            "--task", "Do not modify anything."
        )
        self.assert_blocked_early(
            result,
            "Conversation history cannot be combined with write permits."
        )

    def test_conversation_on_inspect_blocked(self):
        result = invoke(
            "inspect",
            "--project", "LocalAgentSandbox",
            "--conversation"
        )
        self.assert_blocked_early(
            result,
            "Conversation history is supported only for run actions."
        )


if __name__ == "__main__":
    unittest.main()
