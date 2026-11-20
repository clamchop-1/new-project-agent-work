import tempfile
import unittest
from pathlib import Path

from reliability_comparison import compare
from reliability_rubric import _mentions_missing_inputs, _service_fee_refactor


class ReliabilityRubricTests(unittest.TestCase):
    def test_archived_insufficient_claim_is_not_accepted(self):
        self.assertFalse(_mentions_missing_inputs({
            'unfinished': ['Service charge value: 2', 'Business rule: returns a fixed value of 2'],
            'next_actions': [],
        }))
        self.assertFalse(_mentions_missing_inputs({
            'unfinished': ['The new service charge amount is missing'],
            'next_actions': ['Ask for the amount and business rule'],
        }))
        self.assertTrue(_mentions_missing_inputs({
            'unfinished': ['Missing replacement service charge amount and business rule'],
            'next_actions': ['Ask the operator to provide the amount and business rule'],
        }))
        self.assertTrue(_mentions_missing_inputs({
            'unfinished': [
                'The service charge amount has not been specified.',
                'The business rule has not been specified.',
            ],
            'next_actions': [
                'Please provide the requested service charge amount.',
                'Please provide the business rule for the service charge.',
            ],
        }))

    def test_refactor_requires_assignment_and_use_in_total(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'pricing.py'
            for code in (
                'def total(q, p):\n    # service_fee\n    return q * p + 2\n',
                'def total(q, p):\n    service_fee = 2\n    return q * p + 2\n',
            ):
                source.write_text(code)
                self.assertFalse(_service_fee_refactor(source))
            source.write_text('def total(q, p):\n    service_fee = 2\n    return q * p + service_fee\n')
            self.assertTrue(_service_fee_refactor(source))

    def test_comparison_keeps_missing_and_mismatched_scenarios(self):
        baseline = {'single-file': {'model_digest': 'a', 'context': 16, 'initial_files_sha256': 'b'}}
        improved = {'single-file': {**baseline['single-file'], 'model_digest': 'c'}}
        report = compare(baseline, improved)
        self.assertEqual(len(report), 11)
        self.assertFalse(report['single-file']['matched_evidence'])
        self.assertFalse(report['insufficient']['matched_evidence'])
        self.assertIsNone(report['insufficient']['improved'])
