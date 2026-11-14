"""Summarize saved local agent runs without launching model requests."""
import argparse
import json
from pathlib import Path


def load_run(run_directory):
    run = Path(run_directory)
    execution = run / 'execution' if (run / 'execution/result.json').is_file() else run
    result = json.loads((execution / 'result.json').read_text(encoding='utf-8'))
    events_path = execution / 'events.jsonl'
    tool_calls = 0
    tool_errors = 0
    malformed_event_lines = 0
    if events_path.is_file():
        for line in events_path.read_text(encoding='utf-8').splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed_event_lines += 1
                continue
            if 'tool' in event and 'result' in event:
                tool_calls += 1
                if event['result'].get('error'):
                    tool_errors += 1
    return {
        'model': result.get('model', 'unknown'),
        'status': result.get('status', 'unknown'),
        'completion_classification': result.get('completion_classification', 'unknown'),
        'verified_test_status': result.get('verified_test_status', 'unknown'),
        'steps': result.get('steps', 0),
        'seconds': result.get('seconds', 0.0),
        'model_metrics': result.get('model_metrics', {}),
        'tool_calls': tool_calls,
        'tool_errors': tool_errors,
        'malformed_event_lines': malformed_event_lines,
        'cloud_fallback': result.get('cloud_fallback', True),
    }


def summarize_runs(run_directories):
    groups = {}
    for directory in run_directories:
        row = load_run(directory)
        group = groups.setdefault(row['model'], {
            'runs': 0, 'verified_completions': 0, 'test_passes': 0,
            'classified_runs': 0, 'test_runs': 0, 'metrics_runs': 0,
            'total_tool_calls': 0, 'tool_errors': 0, 'total_steps': 0,
            'malformed_event_lines': 0,
            'total_seconds': 0.0, 'prompt_tokens': 0, 'completion_tokens': 0,
            'max_context_utilization': 0.0, 'cloud_fallback_runs': 0,
        })
        group['runs'] += 1
        if row['completion_classification'] != 'unknown':
            group['classified_runs'] += 1
            group['verified_completions'] += row['completion_classification'] == 'completed_verified'
        if row['verified_test_status'] != 'unknown':
            group['test_runs'] += 1
            group['test_passes'] += row['verified_test_status'] == 'passed'
        group['total_tool_calls'] += row['tool_calls']
        group['tool_errors'] += row['tool_errors']
        group['malformed_event_lines'] += row['malformed_event_lines']
        group['total_steps'] += row['steps']
        group['total_seconds'] += row['seconds']
        metrics = row['model_metrics']
        if all(key in metrics for key in ('prompt_tokens', 'completion_tokens', 'max_context_utilization')):
            group['metrics_runs'] += 1
            group['prompt_tokens'] += metrics['prompt_tokens']
            group['completion_tokens'] += metrics['completion_tokens']
            group['max_context_utilization'] = max(
                group['max_context_utilization'], metrics['max_context_utilization']
            )
        group['cloud_fallback_runs'] += bool(row['cloud_fallback'])
    for group in groups.values():
        runs = group['runs']
        group['verified_completion_rate'] = group['verified_completions'] / group['classified_runs'] if group['classified_runs'] else None
        group['test_pass_rate'] = group['test_passes'] / group['test_runs'] if group['test_runs'] else None
        group['tool_error_rate'] = group['tool_errors'] / group['total_tool_calls'] if group['total_tool_calls'] else 0.0
        group['average_steps'] = group['total_steps'] / runs
        group['average_seconds'] = group['total_seconds'] / runs
        if not group['metrics_runs']:
            group['prompt_tokens'] = None
            group['completion_tokens'] = None
            group['max_context_utilization'] = None
    return {'runs': sum(group['runs'] for group in groups.values()), 'models': groups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_directories', nargs='+', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize_runs(args.run_directories), indent=2))


if __name__ == '__main__':
    main()
