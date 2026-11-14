import unittest

from local_agent import completion_classification


class CompletionClassificationTests(unittest.TestCase):
    def test_completion_requires_current_passing_test_receipt_for_verified_label(self):
        self.assertEqual(
            completion_classification('complete', 'passed'),
            'completed_verified',
        )
        self.assertEqual(
            completion_classification('complete', 'failed'),
            'tests_failed',
        )
        self.assertEqual(
            completion_classification('complete', 'stale_after_edit'),
            'tests_stale_after_edit',
        )
        self.assertEqual(
            completion_classification('complete', 'not_run'),
            'completed_not_tested',
        )
        self.assertEqual(
            completion_classification('complete', 'receipt_invalid'),
            'test_receipt_invalid',
        )

    def test_noncompletion_statuses_remain_explicit(self):
        cases = {
            'step_limit': 'partial_step_limit',
            'repeated_unhandled_response': 'model_unhandled_response',
            'failed': 'execution_failed',
            'authorization_blocked': 'incomplete',
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(
                    completion_classification(status, 'not_run'),
                    expected,
                )

    def test_noncompletion_exposes_authorization_and_security_blocks(self):
        self.assertEqual(
            completion_classification(
                'step_limit', 'not_run', [{'category': 'authorization'}],
            ),
            'blocked_by_authorization',
        )
        self.assertEqual(
            completion_classification(
                'repeated_unhandled_response', 'not_run', [{'category': 'security'}],
            ),
            'blocked_by_security',
        )


if __name__ == '__main__':
    unittest.main()
