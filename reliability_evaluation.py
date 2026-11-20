"""Reproducible, isolated Phase 2D workload evaluation with pinned host tests."""
import argparse
import json
from pathlib import Path
import threading
from time import monotonic
import uuid

from agent_config import load_config
from agent_core import Project, atomic_write, digest, encoded, file_hash
from fixture_repo import _git
from local_agent import HUB, run_agent
from reliability_rubric import score


PRICING = '''def subtotal(quantity, unit_price):
    if quantity < 0:
        raise ValueError("negative quantity")
    return quantity * unit_price

def total(quantity, unit_price):
    return quantity * unit_price + 2
'''
RULES = '''def service_charge():
    return 2

def valid_limit(value):
    return 1 <= value <= 100
'''
CASES = 'assert subtotal(0, 7) == 0\nassert subtotal(3, 7) == 21\n'

SCENARIOS = {
    'single-file': (('pricing.py',), 'Fix subtotal so it multiplies quantity by unit price.',
                    (('pricing.py', 'quantity * unit_price\n', 'quantity + unit_price\n'),)),
    'multi-file': (('pricing.py', 'rules.py'), 'Fix the service charge in both modules to the required value of 2. Inspect both files, run tests, repair one module and retest, then repair the other and retest.',
                   (('pricing.py', '+ 2', '+ 4'), ('rules.py', 'return 2', 'return 4'))),
    'test-generation': (('test_cases.py',), 'Generate two standalone Python assert cases for subtotal: zero quantity and a positive quantity greater than one with price greater than one. The host statically checks these assertions. Do not import anything.',
                        (('test_cases.py', CASES, '# Add cases here.\n'),)),
    'refactor': (('pricing.py',), 'Refactor total to name the service charge in a local variable named service_fee, and preserve behavior.',
                 ()),
    'api-contract': (('pricing.py',), 'Repair total(quantity, unit_price). The API contract includes the fixed two-unit service charge, for example total(1, 5) == 7.',
                     (('pricing.py', '+ 2', '+ 0'),)),
    'error-handling': (('pricing.py',), 'subtotal must reject negative quantities with ValueError. Preserve valid totals.',
                       (('pricing.py', 'quantity < 0', 'quantity < -1'),)),
    'configuration': (('rules.py',), 'Fix valid_limit to allow the inclusive range 1 through 100. Reject values outside it.',
                      (('rules.py', '<= 100', '< 100'),)),
    'unfamiliar': ((), 'Read both modules and explain the difference between subtotal and total and what valid_limit accepts. Report your findings as SUBTOTAL=<subtotal(3,7)> TOTAL=<total(3,7)> LIMIT_MIN=<minimum> LIMIT_MAX=<maximum> in the summary. Include the pinned test results.', ()),
    'tool-cycles': (('pricing.py', 'rules.py'), 'Resolve the subtotal, total, and configuration errors. Read both modules, get a failing test receipt, edit pricing and retest, then edit rules and retest. Report the intermediate and final outcomes.',
                    (('pricing.py', 'quantity * unit_price', 'quantity + unit_price'), ('rules.py', '<= 100', '< 100'))),
    'no-change': ((), 'Determine if total(3, 7) is 23. Change a file only if it is wrong. Verify using the pinned tests. If it is correct, include NO_CHANGE_NEEDED in the summary.', ()),
    'insufficient': ((), 'A new service charge is requested, but its value and business rule were not supplied. Read the existing rule, run the pinned tests, and return finish with the missing information in unfinished. Make no changes.', ()),
}


def fixture(batch, name, faults):
    root = HUB / '.local' / 'tests' / (batch + '-' + name) / 'repo'
    root.mkdir(parents=True, exist_ok=False)
    files = {'pricing.py': PRICING, 'rules.py': RULES, 'test_cases.py': CASES,
             '.gitignore': '.local/\n',
             'README.md': 'Pricing fixture. pricing.py calculates totals. rules.py defines service and configuration rules.\n'}
    for path, before, after in faults:
        if before not in files[path]:
            raise ValueError('Fixture fault does not match its baseline.')
        files[path] = files[path].replace(before, after)
    for path, content in files.items():
        (root / path).write_bytes(content.encode())
    test = root / 'tests' / 'test_reliability.py'
    test.parent.mkdir()
    test.write_bytes((HUB / 'reliability_suite.txt').read_bytes().replace(b'\r\n', b'\n'))
    local = root / '.local'
    local.mkdir()
    pinned = local / 'pinned-tests.json'
    atomic_write(pinned, encoded({'test_file': 'tests/test_reliability.py',
                                 'test_sha256': file_hash(test), 'modules': ['pricing', 'rules']}))
    _git('init', '--initial-branch=main', str(root))
    _git('-C', str(root), 'add', '-A', retry_object_write=True)
    _git('-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
         'commit', '-m', 'Seed reliability fixture', retry_object_write=True)
    return Project(root), pinned


def evaluate(batch, name, inference, wall_seconds, progress_guidance=False):
    writes, task, faults = SCENARIOS[name]
    project, pinned = fixture(batch, name, faults)
    before = project.state()['files']
    out = HUB / '.local' / 'runs' / batch / name / 'execution'
    started = monotonic()
    cancel = threading.Event()
    deadline = threading.Timer(wall_seconds, cancel.set)
    deadline.daemon = True
    deadline.start()
    try:
        result = run_agent(
            project, inference['model'], task + ' Read only the relevant fixture files. Use relative paths. '
            'The pinned executor allows import-free pure functions and local assignments. '
            'Keep files independent and do not call other fixture functions from a function. '
            'Report test evidence in finish using arrays for unfinished and next_actions.',
            out, inference['endpoint'], writes=writes, test_config=pinned,
            provider_name=inference['provider'], provider_profile=inference['remote'],
            allowed_tools=('read_file', 'list_files', 'git_diff', 'run_tests', 'finish')
            + (('replace_text',) if writes else ()),
            required_operations=('read_file', 'run_tests') + (('replace_text',) if writes else ()),
            completion_policy='file_modification' if writes else 'read_only_analysis',
            cancel_event=cancel,
            progress_guidance=progress_guidance,
            required_clarifications=(
                ('service_charge_amount', 'service_charge_rule')
                if name == 'insufficient' else ()
            ),
            require_test_after_each_write=name in {'multi-file', 'tool-cycles'},
        )
    except Exception as exc:
        result_file = out / 'result.json'
        result = json.loads(result_file.read_bytes()) if result_file.is_file() else {
            'status': 'failed', 'error': str(exc)[:300],
        }
    finally:
        deadline.cancel()
    trace = out / 'events.jsonl'
    rows = [json.loads(line) for line in trace.read_bytes().splitlines()] if trace.is_file() else []
    tools = [row for row in rows if 'tool' in row]
    acceptance = score(name, writes, project, out, before, result, rows)
    lesson = None
    if acceptance['passed'] and writes and result.get('completion_classification') == 'completed_verified':
        procedure = 'Use the pinned suite to verify the final bytes of every authorized edit.'
        lesson = {
            'status': 'candidate', 'project_id': project.identity,
            'run_id': result['recovery_run_id'], 'procedure': procedure,
            'semantic_sha256': digest(encoded([project.identity, procedure])),
            'evidence': result['commands'][-1],
        }
    return {
        'task': name, 'project_id': project.identity, 'status': result.get('status'),
        **acceptance,
        'objective': task, 'authorized_writes': writes, 'project_root': str(project.root),
        'initial_files_sha256': before,
        'model_digest': result.get('model_digest'), 'context': result.get('context'),
        'write_receipts': result.get('writes', []), 'command_receipts': result.get('commands', []),
        'actual_operations': [row['tool'] for row in tools if row.get('broker_dispatch_seconds') is not None],
        'tool_calls': len(tools),
        'rejected_calls': sum('error' in row['result'] for row in tools),
        'rejected_before_dispatch': sum(row.get('tool_batch_rejected', 0) for row in rows),
        'repeated_calls': sum(row['result'].get('reused_result', False)
                              or 'Repeated identical tool call' in row['result'].get('error', '') for row in tools),
        'reused_calls': sum(row['result'].get('reused_result', False) for row in tools),
        'inference_requests': result.get('inference_attempts', 0),
        'steps': result.get('steps', 0),
        'elapsed_seconds': round(monotonic() - started, 3), 'deadline_reached': cancel.is_set(),
        'model_latency_seconds': round(sum(row['seconds'] for row in rows if 'message' in row), 3),
        'inference_attempt_seconds': result.get('inference_attempt_seconds'),
        'broker_dispatch_seconds': result.get('broker_dispatch_seconds'),
        'ollama_generation_seconds': round(sum(
            (row.get('timings', {}).get('eval_duration') or 0) / 1e9 for row in rows), 3),
        'peak_context_utilization': result.get('model_metrics', {}).get('max_context_utilization'),
        'retries': result.get('inference_retries', 0),
        'finish_schema_retries': result.get('finish_schema_retries', 0),
        'classification': result.get('completion_classification'),
        'recovery_state': result.get('recovery_state'),
        'recovery_run_id': result.get('recovery_run_id'),
        'candidate_lesson': lesson,
        'error': result.get('error'), 'evidence': str(out),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenarios', nargs='*', choices=None)
    parser.add_argument('--wall-seconds', type=int, default=75)
    parser.add_argument('--prompt', choices=('baseline', 'progress'), default='baseline')
    args = parser.parse_args()
    cases = args.scenarios or list(SCENARIOS)
    if set(cases) - SCENARIOS.keys() or len(cases) != len(set(cases)):
        parser.error('Choose distinct scenarios: ' + ', '.join(SCENARIOS))
    if not 1 <= args.wall_seconds <= 180:
        parser.error('Choose a wall budget from 1 through 180 seconds per scenario.')
    inference = load_config(HUB / '.local' / 'agent-config.json', {})['inference']
    if inference['provider'] != 'ssh_tunnel_ollama':
        parser.error('This evaluation requires the configured managed Mac inference provider.')
    batch = 'phase2d-' + uuid.uuid4().hex[:12]
    report = HUB / '.local' / 'runs' / batch / 'evaluation.json'
    results = []
    for name in cases:
        row = evaluate(batch, name, inference, args.wall_seconds, args.prompt == 'progress')
        row.update(prompt=args.prompt, wall_seconds=args.wall_seconds)
        results.append(row)
        atomic_write(report, encoded(results))
        print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()
