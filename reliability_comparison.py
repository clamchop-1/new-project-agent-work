"""Rescore saved A/B runs without editing the source reports or fixtures."""
import argparse
import json
from pathlib import Path

from agent_core import Project, atomic_write, digest, encoded, file_hash
from reliability_evaluation import SCENARIOS
from reliability_rubric import VERSION, score


METRICS = (
    'inference_requests', 'tool_calls', 'repeated_calls', 'steps', 'elapsed_seconds',
    'model_latency_seconds', 'ollama_generation_seconds', 'inference_attempt_seconds',
    'broker_dispatch_seconds', 'deadline_reached', 'retries', 'finish_schema_retries',
)


def rescore(report):
    report = report.resolve(strict=True)
    entries = json.loads(report.read_bytes())
    if len({item['task'] for item in entries}) != len(entries):
        raise ValueError('Duplicate benchmark scenarios in source report.')
    results = {}
    for entry in entries:
        name = entry['task']
        paths, task, _ = SCENARIOS[name]
        output = report.parent / name / 'execution'
        project_root = (report.parent.parent.parent / 'tests' / (report.parent.name + '-' + name)
                        / 'repo')
        project = Project(project_root)
        if project.identity != entry['project_id'] or Path(entry['evidence']).resolve() != output:
            raise ValueError('Saved project or output identity does not match its report.')
        before = {path: digest(project.git('show', 'HEAD:' + path).encode('utf-8'))
                  for path in project.git('ls-tree', '-r', '--name-only', 'HEAD').splitlines()}
        if entry.get('initial_files_sha256', before) != before:
            raise ValueError('Saved fixture baseline differs from its committed source.')
        raw = output / 'result.json'
        result = json.loads(raw.read_bytes()) if raw.is_file() else {
            'status': entry['status'], 'error': entry.get('error')}
        trace = output / 'events.jsonl'
        rows = [json.loads(line) for line in trace.read_bytes().splitlines()] if trace.is_file() else []
        results[name] = {
            'objective': task, 'acceptance': score(name, paths, project, output, before, result, rows),
            'original_passed': entry.get('passed'), 'status': result['status'],
            'classification': result.get('completion_classification'), 'error': result.get('error'),
            'recovery_run_id': result.get('recovery_run_id'),
            'recovery_state': result.get('recovery_state'),
            'metrics': {key: entry.get(key) for key in METRICS},
            'model_digest': result.get('model_digest'), 'context': result.get('context'),
            'initial_files_sha256': digest(encoded(before)), 'raw_result': str(raw),
            'raw_result_sha256': file_hash(raw) if raw.is_file() else None,
            'trace': str(trace), 'trace_sha256': file_hash(trace) if trace.is_file() else None,
        }
    return results


def compare(baseline, improved):
    pairs = {}
    for name in SCENARIOS:
        first, second = baseline.get(name), improved.get(name)
        reasons = []
        if first is None or second is None:
            reasons.append('One side has no recorded scenario.')
        else:
            for key in ('model_digest', 'context', 'initial_files_sha256'):
                if first[key] is None or second[key] is None or first[key] != second[key]:
                    reasons.append(key + ' could not be matched.')
        pairs[name] = {'baseline': first, 'improved': second,
                       'matched_evidence': not reasons, 'comparison_limits': reasons}
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('--improved', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    data = {'rubric_version': VERSION, 'baseline_report': str(args.baseline.resolve()),
            'baseline_report_sha256': file_hash(args.baseline)}
    baseline = rescore(args.baseline)
    if args.improved is None:
        data['scenarios'] = baseline
    else:
        data.update(improved_report=str(args.improved.resolve()),
                    improved_report_sha256=file_hash(args.improved),
                    scenarios=compare(baseline, rescore(args.improved)))
    if args.output.exists():
        parser.error('Choose a new output to preserve previous reports.')
    atomic_write(args.output, encoded(data))


if __name__ == '__main__':
    main()
