import tempfile
import hashlib
import json
from pathlib import Path
import unittest

from patch_validation import (PatchValidationError, normalize_model_diff_response,
                              build_unified_diff_from_proposal, validate_unified_diff)


class StrictUnifiedDiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/progress.py').write_bytes(b'def old(value):\n    return value\n')

    def tearDown(self):
        self.temp.cleanup()

    def patch(self, body, *, path='src/progress.py', old='a/src/progress.py', new='b/src/progress.py'):
        return (f'diff --git {old} {new}\n--- {old}\n+++ {new}\n'.encode()
                + body.encode())

    def test_valid_hunk_reconstructs_exact_expected_bytes(self):
        raw = self.patch('@@ -1,2 +1,2 @@\n-def old(value):\n+def new(value):\n     return value\n')
        parsed = validate_unified_diff(raw, root=self.root, allowed_paths={'src/progress.py'})
        self.assertEqual(parsed.files[0].new_bytes, b'def new(value):\n    return value\n')

    def test_standard_index_header_is_accepted_only_in_canonical_form(self):
        raw = (b'diff --git a/src/progress.py b/src/progress.py\n'
               b'index 1111111..2222222 100644\n'
               b'--- a/src/progress.py\n+++ b/src/progress.py\n'
               b'@@ -1,2 +1,2 @@\n-def old(value):\n+def new(value):\n     return value\n')
        self.assertEqual(len(validate_unified_diff(
            raw, root=self.root, allowed_paths={'src/progress.py'}).files), 1)

    def test_one_fenced_standalone_new_file_diff_normalizes_without_prose(self):
        response = (b'```diff\n--- /dev/null\n+++ b/src/new_progress.py\n'
                    b'@@ -0,0 +1 @@\n+x\n```')
        normalized = normalize_model_diff_response(response, allowed_paths={'src/new_progress.py'})
        self.assertTrue(normalized.startswith(b'diff --git a/src/new_progress.py b/src/new_progress.py\nnew file mode 100644\n'))
        parsed = validate_unified_diff(normalized, root=self.root,
                                       allowed_paths={'src/new_progress.py'})
        self.assertEqual(parsed.files[0].new_bytes, b'x\n')

    def test_fenced_response_with_prose_or_extra_fence_is_rejected(self):
        with self.assertRaisesRegex(PatchValidationError, 'trailing prose'):
            normalize_model_diff_response(b'```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\nextra',
                                          allowed_paths={'x'})
        with self.assertRaisesRegex(PatchValidationError, 'one complete'):
            normalize_model_diff_response(b'```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n```\n```',
                                          allowed_paths={'x'})

    def test_new_file_path_cannot_escape_through_symlink_parent(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + '-outside')
        outside.mkdir()
        try:
            (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            outside.rmdir()
            self.skipTest('Directory symlinks unavailable in this environment')
        raw = (b'diff --git a/linked/new.py b/linked/new.py\n'
               b'new file mode 100644\n'
               b'--- /dev/null\n+++ b/linked/new.py\n@@ -0,0 +1 @@\n+x\n')
        try:
            with self.assertRaisesRegex(PatchValidationError, 'symlink'):
                validate_unified_diff(raw, root=self.root, allowed_paths={'linked/new.py'})
        finally:
            (self.root / 'linked').unlink(missing_ok=True)
            outside.rmdir()

    def test_underdeclared_hunk_matches_milestone2_failure_and_is_rejected(self):
        # The Milestone 2 proposal declared fewer source lines than its body.
        raw = (b'diff --git a/src/new_progress.py b/src/new_progress.py\n'
               b'new file mode 100644\n'
               b'--- /dev/null\n+++ b/src/new_progress.py\n@@ -0,0 +1,7 @@\n'
               b'+def clamp_percent(value):\n+    if type(value) is not int:\n'
               b'+        raise TypeError\n+    if value < 0:\n+        return 0\n'
               b'+    if value > 100:\n+        return 100\n+    return value\n')
        with self.assertRaisesRegex(PatchValidationError, 'count mismatch'):
            validate_unified_diff(raw, root=self.root, allowed_paths={'src/new_progress.py'})

    def test_trailing_unconsumed_content_is_rejected(self):
        raw = self.patch('@@ -1,2 +1,2 @@\n-def old(value):\n+def new(value):\n     return value\n+extra\n')
        with self.assertRaisesRegex(PatchValidationError, 'count mismatch'):
            validate_unified_diff(raw, root=self.root, allowed_paths={'src/progress.py'})

    def test_traversal_and_unauthorized_paths_are_rejected(self):
        raw = self.patch('@@ -1 +1 @@\n-def old(value):\n+def new(value):\n',
                         path='../outside.py', old='a/../outside.py', new='b/../outside.py')
        with self.assertRaises(PatchValidationError):
            validate_unified_diff(raw, root=self.root, allowed_paths={'../outside.py'})
        raw = self.patch('@@ -1 +1 @@\n-def old(value):\n+def new(value):\n')
        with self.assertRaisesRegex(PatchValidationError, 'unauthorized'):
            validate_unified_diff(raw, root=self.root, allowed_paths={'src/other.py'})

    def test_rename_delete_and_ambiguous_boundaries_are_rejected(self):
        renamed = self.patch('@@ -1 +1 @@\n-def old(value):\n+def new(value):\n',
                             old='a/src/progress.py', new='b/src/other.py')
        with self.assertRaises(PatchValidationError):
            validate_unified_diff(renamed, root=self.root, allowed_paths={'src/progress.py'})
        deleted = b'diff --git a/src/progress.py b/src/progress.py\n--- a/src/progress.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def old(value):\n-    return value\n'
        with self.assertRaises(PatchValidationError):
            validate_unified_diff(deleted, root=self.root, allowed_paths={'src/progress.py'})

    def test_symlink_target_is_rejected(self):
        linked = self.root / 'src/linked.py'
        try:
            linked.symlink_to(self.root / 'src/progress.py')
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable in this environment')
        raw = self.patch('@@ -1,2 +1,2 @@\n-def old(value):\n+def new(value):\n     return value\n',
                         path='src/linked.py', old='a/src/linked.py', new='b/src/linked.py')
        with self.assertRaisesRegex(PatchValidationError, 'symlink'):
            validate_unified_diff(raw, root=self.root, allowed_paths={'src/linked.py'})

    def structured(self, rows):
        return json.dumps({'files': rows}).encode('utf-8')

    def row(self, path, content, old=None):
        return {'path': path,
                'base_sha256': hashlib.sha256(old).hexdigest() if old is not None else None,
                'content': content}

    def test_structured_proposal_builds_multiple_files_with_exact_hunk_counts(self):
        old = b'def old(value):\n    return value\n'
        (self.root / 'src/progress.py').write_bytes(old)
        proposal = self.structured([
            self.row('src/progress.py', 'def new(value):\n    return value + 1\n', old),
            self.row('src/helper.py', 'def double(value):\n    return value * 2\n'),
        ])
        validated = build_unified_diff_from_proposal(
            proposal, root=self.root, allowed_paths={'src/progress.py', 'src/helper.py'})
        self.assertEqual([f.path for f in validated.files], ['src/progress.py', 'src/helper.py'])
        self.assertEqual(validated.files[0].new_bytes, b'def new(value):\n    return value + 1\n')
        self.assertEqual(validated.files[1].new_bytes, b'def double(value):\n    return value * 2\n')
        self.assertEqual(validate_unified_diff(validated.raw, root=self.root,
            allowed_paths={'src/progress.py', 'src/helper.py'}).files, validated.files)

    def test_structured_proposal_preserves_crlf_bom_and_missing_final_newline(self):
        old = b'\xef\xbb\xbfdef old(value):\r\n    return value'
        (self.root / 'src/progress.py').write_bytes(old)
        proposal = self.structured([self.row('src/progress.py',
            'def new(value):\n    return value + 1', old)])
        validated = build_unified_diff_from_proposal(
            proposal, root=self.root, allowed_paths={'src/progress.py'})
        self.assertEqual(validated.files[0].new_bytes,
                         b'\xef\xbb\xbfdef new(value):\r\n    return value + 1')
        self.assertIn(b'\\ No newline at end of file\n', validated.raw)
        applied = self.root / 'simulated'
        applied.mkdir()
        (applied / 'src').mkdir()
        (applied / 'src/progress.py').write_bytes(old)
        import subprocess
        checked = subprocess.run(['git', '-C', str(applied), 'apply', '--check'],
            input=validated.raw, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors='replace'))

    def test_structured_proposal_rejects_stale_hash_and_untrusted_paths(self):
        old = b'def old(value):\n    return value\n'
        stale = self.structured([{'path': 'src/progress.py', 'base_sha256': '0' * 64,
                                  'content': 'def new(value):\n    return value\n'}])
        with self.assertRaisesRegex(PatchValidationError, 'Stale source hash'):
            build_unified_diff_from_proposal(stale, root=self.root,
                                              allowed_paths={'src/progress.py'})
        for path in ('../outside.py', 'tests/test_bad.py', '.local/pinned-tests.json'):
            bad = self.structured([self.row(path, 'def x():\n    return 1\n')])
            with self.assertRaises(PatchValidationError):
                build_unified_diff_from_proposal(bad, root=self.root,
                                                  allowed_paths={path})

    def test_structured_proposal_rejects_binary_mixed_newline_and_symlink_targets(self):
        old = (self.root / 'src/progress.py').read_bytes()
        bad_encoding = self.structured([self.row('src/progress.py', 'bad\x00text', old)])
        with self.assertRaisesRegex(PatchValidationError, 'NUL-free'):
            build_unified_diff_from_proposal(bad_encoding, root=self.root,
                                              allowed_paths={'src/progress.py'})
        old = b'a\r\nb\n'
        (self.root / 'src/progress.py').write_bytes(old)
        mixed = self.structured([self.row('src/progress.py', 'new\ntext\n', old)])
        with self.assertRaisesRegex(PatchValidationError, 'Mixed or unsupported'):
            build_unified_diff_from_proposal(mixed, root=self.root,
                                              allowed_paths={'src/progress.py'})
        normal = b'def old(value):\n    return value\n'
        (self.root / 'src/progress.py').write_bytes(normal)
        mixed_model = self.structured([self.row('src/progress.py',
            'def new(value):\r\n    return value\n', normal)])
        with self.assertRaisesRegex(PatchValidationError, 'proposal line endings'):
            build_unified_diff_from_proposal(mixed_model, root=self.root,
                                              allowed_paths={'src/progress.py'})

        outside = self.root.parent / (self.root.name + '-outside-structured')
        outside.mkdir()
        try:
            (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            outside.rmdir()
            self.skipTest('Directory symlinks unavailable in this environment')
        try:
            linked_proposal = self.structured([self.row('linked/new.py', 'def x():\n    return 1\n')])
            with self.assertRaisesRegex(PatchValidationError, 'symlink'):
                build_unified_diff_from_proposal(linked_proposal, root=self.root,
                    allowed_paths={'linked/new.py'})
        finally:
            (self.root / 'linked').unlink(missing_ok=True)
            outside.rmdir()

    def test_milestone4_underdeclared_response_is_rejected_and_rebuilt_structured(self):
        # Preserved 102e7c... response-2 declared +3 but supplied six added lines.
        historical = (b'diff --git a/stats.py b/stats.py\nnew file mode 100644\n'
                      b'--- /dev/null\n+++ b/stats.py\n@@ -0,0 +1,3 @@\n'
                      b'+def clamp(value, low, high):\n+    if value < low:\n'
                      b'+        return low\n+    if value > high:\n'
                      b'+        return high\n+    return value\n')
        with self.assertRaisesRegex(PatchValidationError, 'count mismatch'):
            validate_unified_diff(historical, root=self.root, allowed_paths={'stats.py'})
        good = self.structured([self.row('src/progress.py',
            'def new(value):\n    return value\n', (self.root / 'src/progress.py').read_bytes())])
        self.assertTrue(build_unified_diff_from_proposal(good, root=self.root,
            allowed_paths={'src/progress.py'}).raw.startswith(b'diff --git'))


class PreservedMilestone4ProposalRegressionTests(unittest.TestCase):
    root = (Path(__file__).resolve().parents[1] /
        '.local/tests/milestone4-review-20261123-df64251f96a44e2287ba2357e171420d/repo')

    def test_first_live_workflow_keeps_rejecting_the_underdeclared_stats_hunk(self):
        run = self.root / '.local/coding-workflows/102e7c662a0d44c1a3f3d5c7f68b1a6e'
        response = (run / 'response-2.txt').read_bytes()
        state = json.loads((run / 'state.json').read_bytes())
        self.assertEqual(hashlib.sha256(response).hexdigest(),
                         state['coder_attempts'][1]['response_sha256'])
        boundary = b'diff --git a/stats.py b/stats.py'
        section = boundary + response.split(boundary, 1)[1]
        section = section.split(b'```', 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(PatchValidationError, 'count mismatch'):
                validate_unified_diff(section, root=Path(temp), allowed_paths={'stats.py'})

    def test_second_live_workflow_crlf_target_rejects_old_diff_but_structured_builder_preserves_it(self):
        run = self.root / '.local/coding-workflows/08fa9808f4c84eeba1dfbeb0afb1798f'
        response = (run / 'response-2.txt').read_bytes()
        state = json.loads((run / 'state.json').read_bytes())
        self.assertEqual(hashlib.sha256(response).hexdigest(),
                         state['coder_attempts'][1]['response_sha256'])
        old = (self.root / 'calculator.py').read_bytes()
        self.assertIn(b'\r\n', old)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'calculator.py').write_bytes(old)
            normalized = normalize_model_diff_response(response,
                allowed_paths={'calculator.py', 'stats.py'})
            with self.assertRaises(PatchValidationError):
                validate_unified_diff(normalized, root=root,
                                      allowed_paths={'calculator.py', 'stats.py'})
            structured = json.dumps({'files': [{
                'path': 'calculator.py', 'base_sha256': hashlib.sha256(old).hexdigest(),
                'content': 'def average(values):\n    return sum(values) / len(values)\n'}]}).encode()
            rebuilt = build_unified_diff_from_proposal(structured, root=root,
                allowed_paths={'calculator.py'})
            self.assertEqual(rebuilt.files[0].new_bytes,
                b'def average(values):\r\n    return sum(values) / len(values)\r\n')


if __name__ == '__main__':
    unittest.main()
