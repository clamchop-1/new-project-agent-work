import unittest

import agent
from agent_core import AgentError


class RepairFixtureAuthorizationTests(unittest.TestCase):
    def test_registration_points_to_only_the_dedicated_fixture(self):
        self.assertEqual(
            agent.PROJECTS['LocalAgentRepairFixture'],
            str((agent.HUB / '.local/tests/live-repair-001/repo').resolve()),
        )

    def test_repair_fixture_permits_are_limited_to_the_two_source_files(self):
        self.assertEqual(
            agent.validate_write_paths(
                'LocalAgentRepairFixture',
                ['calculator.py', 'stats.py'],
            ),
            ('calculator.py', 'stats.py'),
        )
        for paths in (
            ['tests/test_stats.py'],
            ['AGENTS.md'],
            ['calculator.py', '../outside.py'],
            ['calculator.py', 'calculator.py'],
            [],
        ):
            with self.subTest(paths=paths), self.assertRaises(AgentError):
                agent.validate_write_paths('LocalAgentRepairFixture', paths)

    def test_other_registered_projects_keep_existing_path_policy(self):
        self.assertEqual(
            agent.validate_write_paths('LocalAgentSandbox', ['calculator.py']),
            ('calculator.py',),
        )


if __name__ == '__main__':
    unittest.main()
