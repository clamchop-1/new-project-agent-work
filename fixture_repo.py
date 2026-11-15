"""Create disposable validation repositories, never touch an existing path."""
import json
from pathlib import Path
import subprocess
from agent_core import Project, file_hash


def make_fixture(root):
    root = Path(root);root.mkdir(parents=True, exist_ok=False)
    files = {
        '.gitignore': '.local/\n__pycache__/\n',
        'AGENTS.md': '# Fixture project\n\nRead README.md. Never delete files or edit instructions. Run the pinned tests. Report marker MAPLE-427.\n',
        'README.md': '# Statistics library\n\nPython functions in calculator.py and stats.py; tests in tests/test_stats.py. No runtime dependencies.\n',
        'calculator.py': 'def average(values):\n    return sum(values) / (len(values) + 1)\n',
        'tests/test_stats.py': '''import unittest
import calculator
import stats

class StatsTests(unittest.TestCase):
    def test_average(self):
        self.assertEqual(calculator.average([2, 4, 6]), 4)
        self.assertEqual(calculator.average([-4, 0, 4]), 0)
        self.assertEqual(calculator.average([7]), 7)
    def test_empty(self):
        with self.assertRaises(ValueError): calculator.average([])
    def test_clamp(self):
        self.assertEqual(stats.clamp(5, 0, 3), 3)
        self.assertEqual(stats.clamp(-1, 0, 3), 0)
        self.assertEqual(stats.clamp(2, 0, 3), 2)
'''}
    for name, text in files.items():
        path = root/name;path.parent.mkdir(exist_ok=True);path.write_text(text, encoding='utf-8')
    subprocess.run(['git', 'init', '--initial-branch=main', str(root)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), 'add', '-A'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Local Agent Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-m', 'Seed controlled validation fixture'], check=True, capture_output=True)
    local = root/'.local';local.mkdir()
    for name in ('PROJECT_CONTEXT.md', 'DURABLE_MEMORY.md'):
        (local/name).write_text('## CURRENT CODEX HANDOFF\n\nOriginating agent: codex\nTask marker: CEDAR-994. Fix average and add clamp. Tests are unfinished.\nPreserve instructions and all unrelated work.\n\n## OLDER HANDOFF\n\nHISTORY_ONLY_826; never automatically load this section.\n', encoding='utf-8')
    config = {'test_file': 'tests/test_stats.py', 'test_sha256': file_hash(root/'tests/test_stats.py'),
              'modules': ['calculator', 'stats']}
    path = local/'pinned-tests.json';path.write_text(json.dumps(config), encoding='utf-8')
    return Project(root), path
