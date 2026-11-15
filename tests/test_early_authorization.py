import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"


def invoke(*args):
    with tempfile.TemporaryDirectory() as temporary:
        config_path = Path(temporary) / 'agent-config.json'
        config_path.write_text(json.dumps({
            'version': 1,
            'projects': {
                'kronosstage': {'name': 'KronosStage', 'root': str(ROOT)},
                'localagentsandbox': {'name': 'LocalAgentSandbox', 'root': str(ROOT)},
            },
            'inference': {
                'provider': 'ollama',
                'model': 'qwen3.5:4b',
                'endpoint': 'http://127.0.0.1:11434',
            },
        }), encoding='utf-8')
        environment = dict(os.environ)
        environment['LOCAL_CODING_AGENT_CONFIG'] = str(config_path)
        return subprocess.run(
            [sys.executable, str(AGENT), *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )


class EarlyAuthorizationTests(unittest.TestCase):

    def assert_blocked_early(self, result, expected):
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, output)
        self.assertNotIn("LOCAL MODEL ROUTING", output)
        self.assertNotIn('"run":', output)

    def test_conversation_with_write_path_blocked(self):
        result = invoke(
            "run", "--project", "LocalAgentSandbox", "--conversation",
            "--write-path", "calculator.py", "--task", "Do not modify anything."
        )
        self.assert_blocked_early(
            result, "Conversation history cannot be combined with project write permits."
        )

    def test_conversation_with_permit_blocked(self):
        result = invoke(
            "run", "--project", "LocalAgentSandbox", "--conversation",
            "--permit", "nonexistent-permit.json",
            "--task", "Do not modify anything."
        )
        self.assert_blocked_early(
            result, "Conversation history cannot be combined with project write permits."
        )

    def test_conversation_on_inspect_blocked(self):
        result = invoke(
            "inspect", "--project", "LocalAgentSandbox", "--conversation"
        )
        self.assert_blocked_early(
            result, "Conversation history is supported only for run actions."
        )


if __name__ == "__main__":
    unittest.main()
