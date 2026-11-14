import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

import interactive_agent
import conversation_session as cs


class FailedRunMemoryTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session_patch = patch.object(
            cs, "SESSION_DIR", Path(self.temp.name)
        )
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)

    @patch.object(interactive_agent.subprocess, "run")
    def test_failed_status_not_saved(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "status": "repeated_unhandled_response",
                "summary": None
            }),
            stderr=""
        )

        interactive_agent.run_task(
            "LocalAgentSandbox",
            "auto",
            "Explain calculator.py"
        )

        self.assertEqual(cs.load_session("LocalAgentSandbox"), [])

    @patch.object(interactive_agent.subprocess, "run")
    def test_nonzero_exit_not_saved(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Agent failed"
        )

        interactive_agent.run_task(
            "LocalAgentSandbox",
            "auto",
            "Explain calculator.py"
        )

        self.assertEqual(cs.load_session("LocalAgentSandbox"), [])

    @patch.object(interactive_agent.subprocess, "run")
    def test_successful_response_saved(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "status": "complete",
                "summary": {
                    "summary": "The file contains two functions."
                }
            }),
            stderr=""
        )

        interactive_agent.run_task(
            "LocalAgentSandbox",
            "auto",
            "Explain calculator.py"
        )

        turns = cs.load_session("LocalAgentSandbox")
        self.assertEqual(len(turns), 1)
        self.assertEqual(
            turns[0]["assistant"],
            "The file contains two functions."
        )


if __name__ == "__main__":
    unittest.main()
