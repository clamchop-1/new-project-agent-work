from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pure_tests
from coding_workflow import TEST_COMMANDS


class RegisteredFixtureTests(unittest.TestCase):
    def setUp(self):
        self.m4_root = pure_tests.MILESTONE4_CODING_FIXTURE
        self.config = dict(pure_tests.CODING_TEST_CONFIG)

    def test_exact_milestone4_fixture_and_fixed_profile_are_registered(self):
        root = pure_tests._canonical_root(str(self.m4_root))
        self.assertEqual(root, self.m4_root)
        self.assertEqual(pure_tests._validated_profile(root, self.config), (False, True))

    def test_existing_milestone3_fixture_layout_remains_supported(self):
        with tempfile.TemporaryDirectory(prefix='milestone3-compat-',
                                         dir=pure_tests.FIXTURE_TESTS_ROOT) as parent:
            root_path = Path(parent) / 'repo'
            root_path.mkdir()
            root = pure_tests._canonical_root(str(root_path))
            self.assertEqual(pure_tests._validated_profile(root, self.config), (False, True))

    def test_unregistered_repository_is_rejected_for_coding_profile(self):
        with tempfile.TemporaryDirectory(prefix='unregistered-',
                                         dir=pure_tests.FIXTURE_TESTS_ROOT) as parent:
            root_path = Path(parent) / 'repo'
            root_path.mkdir()
            root = pure_tests._canonical_root(str(root_path))
            with self.assertRaisesRegex(ValueError, 'Pinned test configuration'):
                pure_tests._validated_profile(root, self.config)

    def test_similar_milestone4_prefix_is_not_registered(self):
        with tempfile.TemporaryDirectory(prefix='milestone4-live-lookalike-',
                                         dir=pure_tests.FIXTURE_TESTS_ROOT) as parent:
            root_path = Path(parent) / 'repo'
            root_path.mkdir()
            root = pure_tests._canonical_root(str(root_path))
            self.assertFalse(pure_tests._is_milestone4_coding_fixture(root))
            with self.assertRaisesRegex(ValueError, 'Pinned test configuration'):
                pure_tests._validated_profile(root, self.config)

    def test_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'traversal'):
            pure_tests._canonical_root(str(self.m4_root.parent / 'intermediate' / '..' / 'repo'))

    def test_reparse_point_in_fixture_path_is_rejected(self):
        with patch.object(pure_tests, '_contains_reparse_point', return_value=True):
            with self.assertRaisesRegex(ValueError, 'reparse point'):
                pure_tests._canonical_root(str(self.m4_root))

    def test_symlink_alias_of_registered_fixture_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='milestone4-link-',
                                         dir=pure_tests.FIXTURE_TESTS_ROOT) as parent:
            alias = Path(parent) / 'repo'
            try:
                alias.symlink_to(self.m4_root, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest('Symlink creation is unavailable: ' + str(exc))
            with self.assertRaisesRegex(ValueError, 'reparse point'):
                pure_tests._canonical_root(str(alias))

    def test_test_profile_configuration_cannot_be_substituted(self):
        altered = dict(self.config, test_file='tests/other.py')
        with self.assertRaisesRegex(ValueError, 'Pinned test configuration'):
            pure_tests._validated_profile(self.m4_root, altered)
        self.assertEqual(set(TEST_COMMANDS), {
            'clamp_percent_v1', 'stats_multi_module_v1'})
        self.assertIn('Broker.execute("tests")', TEST_COMMANDS['clamp_percent_v1'])
        self.assertEqual(TEST_COMMANDS['stats_multi_module_v1'],
                         TEST_COMMANDS['clamp_percent_v1'])

    def test_registered_fixture_config_accepts_preserved_utf8_bom(self):
        raw = (self.m4_root / '.local' / 'pinned-tests.json').read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(pure_tests._load_pinned_config(
            self.m4_root / '.local' / 'pinned-tests.json'), self.config)


if __name__ == '__main__':
    unittest.main()
