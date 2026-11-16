import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

import interactive_agent
import conversation_session as cs
from fixture_repo import make_fixture


class FailedRunMemoryTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session_patch = patch.object(
            cs, "SESSION_DIR", Path(self.temp.name)
        )
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.project, _ = make_fixture(Path(self.temp.name) / 'project')
        self.assistant_root = Path(self.temp.name) / 'assistant'
        self.assistant_root.mkdir()
        self.config_patch = patch.object(interactive_agent, 'CONFIG', {
            'projects': {'localagentsandbox': {
                'name': 'LocalAgentSandbox', 'root': str(self.project.root),
            }},
            'inference': {'provider': 'ollama', 'model': 'qwen3:8b',
                          'endpoint': 'http://127.0.0.1:11434'},
        })
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    @patch.object(interactive_agent, "subprocess")
    def test_failed_status_not_saved(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "status": "repeated_unhandled_response",
                "summary": None
            }),
            stderr=""
        )

        # The launcher gets its own mocked process boundary; Project.git keeps
        # using the real subprocess module imported by agent_core.
        self.assertEqual(
            Path(self.project.git("rev-parse", "--show-toplevel").strip()).resolve(),
            self.project.root,
        )
        mock_subprocess.run.assert_not_called()
        interactive_agent.run_task(
            "LocalAgentSandbox",
            "auto",
            "Explain calculator.py",
            self.assistant_root
        )
        mock_subprocess.run.assert_called_once()
        self.assertIn("agent.py", " ".join(mock_subprocess.run.call_args.args[0]))

        self.assertEqual(cs.load_session("LocalAgentSandbox"), [])

    @patch.object(interactive_agent, "subprocess")
    def test_nonzero_exit_not_saved(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Agent failed"
        )

        self.assertEqual(
            Path(self.project.git("rev-parse", "--show-toplevel").strip()).resolve(),
            self.project.root,
        )
        mock_subprocess.run.assert_not_called()
        interactive_agent.run_task(
            "LocalAgentSandbox",
            "auto",
            "Explain calculator.py",
            self.assistant_root
        )
        mock_subprocess.run.assert_called_once()
        self.assertIn("agent.py", " ".join(mock_subprocess.run.call_args.args[0]))

        self.assertEqual(cs.load_session("LocalAgentSandbox"), [])

    @patch.object(interactive_agent, "subprocess")
    def test_successful_response_saved(self, mock_subprocess):
        mock_subprocess.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "status": "complete",
                "summary": {
                    "summary": "The file contains two functions."
                }
            }),
            stderr=""
        )

        self.assertEqual(
            Path(self.project.git("rev-parse", "--show-toplevel").strip()).resolve(),
            self.project.root,
        )
        mock_subprocess.run.assert_not_called()
        interactive_agent.run_task(
            "LocalAgentSandbox",
            "auto",
            "Explain calculator.py",
            self.assistant_root
        )
        mock_subprocess.run.assert_called_once()
        self.assertIn("agent.py", " ".join(mock_subprocess.run.call_args.args[0]))

        turns = cs.load_session("LocalAgentSandbox")
        self.assertEqual(len(turns), 1)
        self.assertEqual(
            turns[0]["assistant"],
            "The file contains two functions."
        )


if __name__ == "__main__":
    unittest.main()
