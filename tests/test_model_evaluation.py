import json
import tempfile
import unittest
from pathlib import Path

from model_evaluation import summarize_runs


class ModelEvaluationTests(unittest.TestCase):
    def test_summarizes_saved_runs_by_model_without_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directories = []
            samples = [
                ('qwen-local', 'completed_verified', 'passed', 10, False),
                ('qwen-local', 'tests_failed', 'failed', 20, False),
                ('other-local', 'completed_not_tested', 'not_run', 4, False),
            ]
            for index, (model, classification, tests, seconds, cloud) in enumerate(samples):
                execution = root / str(index) / 'execution'
                execution.mkdir(parents=True)
                (execution / 'result.json').write_text(json.dumps({
                    'model': model,
                    'status': 'complete',
                    'completion_classification': classification,
                    'verified_test_status': tests,
                    'steps': 4,
                    'seconds': seconds,
                    'cloud_fallback': cloud,
                    'model_metrics': {
                        'prompt_tokens': 100,
                        'completion_tokens': 20,
                        'max_context_utilization': 0.25,
                    },
                }), encoding='utf-8')
                (execution / 'events.jsonl').write_text(
                    '{"tool":"read_file","result":{"ok":true}}\n'
                    '{"tool":"run_tests","result":{"error":"failed"}}\n'
                    + ('{"partial":' if index == 1 else ''),
                    encoding='utf-8',
                )
                run_directories.append(execution.parent)

            summary = summarize_runs(run_directories)
            self.assertEqual(summary['runs'], 3)
            self.assertEqual(summary['models']['qwen-local']['runs'], 2)
            self.assertEqual(summary['models']['qwen-local']['verified_completion_rate'], 0.5)
            self.assertEqual(summary['models']['qwen-local']['test_pass_rate'], 0.5)
            self.assertEqual(summary['models']['qwen-local']['tool_error_rate'], 0.5)
            self.assertEqual(summary['models']['qwen-local']['prompt_tokens'], 200)
            self.assertEqual(summary['models']['qwen-local']['average_seconds'], 15)
            self.assertEqual(summary['models']['qwen-local']['malformed_event_lines'], 1)
            self.assertEqual(summary['models']['other-local']['verified_completion_rate'], 0)

    def test_missing_historical_metrics_are_reported_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = Path(temporary) / 'execution'
            execution.mkdir()
            (execution / 'result.json').write_text(json.dumps({
                'model': 'older-run',
                'status': 'complete',
                'verified_test_status': 'passed',
                'steps': 3,
                'seconds': 2.0,
            }), encoding='utf-8')
            group = summarize_runs([execution])['models']['older-run']
            self.assertIsNone(group['verified_completion_rate'])
            self.assertEqual(group['test_pass_rate'], 1.0)
            self.assertIsNone(group['prompt_tokens'])
            self.assertEqual(group['metrics_runs'], 0)


if __name__ == '__main__':
    unittest.main()
