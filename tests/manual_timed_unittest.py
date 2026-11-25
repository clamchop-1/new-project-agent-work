"""Run the existing unittest suite with per-test timings; installs nothing."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TimedResult(unittest.TextTestResult):
    def startTest(self, test):
        self._test_started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        elapsed = time.perf_counter() - self._test_started
        self.durations.append((elapsed, self.getDescription(test)))
        super().stopTest(test)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.durations = []

    def printErrors(self):
        super().printErrors()
        self.stream.writeln('\nSlowest tests:')
        for elapsed, description in sorted(self.durations, reverse=True)[:30]:
            self.stream.writeln(f'{elapsed:8.3f}s  {description}')


def main():
    suite = unittest.defaultTestLoader.discover('tests')
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2,
                                     resultclass=TimedResult)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
