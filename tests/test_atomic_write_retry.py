import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_core


class AtomicWriteRetryTests(unittest.TestCase):

    @staticmethod
    def sharing_error():
        error = PermissionError(
            errno.EACCES, "File is being used by another process"
        )
        error.winerror = 32
        return error

    def test_success_after_transient_lock(self):
        calls = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise self.sharing_error()
            return "success"

        with patch.object(agent_core.os, "name", "nt"), \
             patch.object(agent_core.time, "sleep") as sleep:
            result = agent_core._retry_windows_sharing(
                operation, attempts=4, delay=0.01
            )

        self.assertEqual(result, "success")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_exhausted_retries_raise(self):
        calls = []

        def operation():
            calls.append(1)
            raise self.sharing_error()

        with patch.object(agent_core.os, "name", "nt"), \
             patch.object(agent_core.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                agent_core._retry_windows_sharing(
                    operation, attempts=3, delay=0.01
                )

        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_unrelated_error_not_retried(self):
        calls = []

        def operation():
            calls.append(1)
            raise FileNotFoundError("Unrelated failure")

        with patch.object(agent_core.time, "sleep") as sleep:
            with self.assertRaises(FileNotFoundError):
                agent_core._retry_windows_sharing(operation)

        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_atomic_write_replaces_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.json"
            path.write_bytes(b"old")
            agent_core.atomic_write(path, b"new")

            self.assertEqual(path.read_bytes(), b"new")
            self.assertEqual(
                list(Path(directory).glob("sample.json.new-*")),
                []
            )


if __name__ == "__main__":
    unittest.main()
