"""Host-owned acceptance checks for the fixed reliability fixtures."""
import ast
import json
from pathlib import Path
import re

from agent_core import digest, encoded
from local_agent import verify_test_receipts


VERSION = '2d1-v3'


def _tree(path):
    try:
        return ast.parse(path.read_text(encoding='utf-8-sig'))
    except (OSError, SyntaxError, UnicodeError):
        return ast.Module(body=[])


def _service_fee_refactor(path):
    for node in _tree(path).body:
        if not isinstance(node, ast.FunctionDef) or node.name != 'total':
            continue
        assigned = False
        for statement in node.body:
            if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == 'service_fee'
                    for target in statement.targets):
                assigned = (isinstance(statement.value, ast.Constant)
                            and type(statement.value.value) is int and statement.value.value == 2)
            if assigned and isinstance(statement, ast.Return) and any(
                    isinstance(item, ast.Name) and item.id == 'service_fee'
                    for item in ast.walk(statement.value)):
                return True
    return False


def _receipt_sequence(rows, commands, before, paths):
    """Bind baseline, intermediate, and final tests to the write snapshots."""
    snapshots = dict(before)
    observed_paths = set()
    stages = set()
    reads = set()
    receipts = iter(commands)
    for row in rows:
        tool = row.get('tool')
        result = row.get('result', {})
        if 'error' in result or result.get('reused_result'):
            continue
        if tool == 'read_file':
            path = result.get('path')
            if isinstance(path, str):
                reads.add(Path(path).name)
        if tool == 'replace_text':
            path = result.get('path')
            if path not in paths or result.get('before') != snapshots.get(path):
                return False
            if 0 not in stages or not set(paths) <= reads:
                return False
            snapshots[path] = result.get('after')
            observed_paths.add(path)
        if tool == 'run_tests':
            receipt = next(receipts, None)
            if receipt is None:
                return False
            command = json.loads(Path(receipt['path']).read_bytes())
            stage = len(observed_paths)
            if (command.get('command') == 'tests'
                    and command.get('worktree_content_sha256') == digest(encoded(snapshots))
                    and command.get('exit_code') == (0 if stage == len(paths) else 1)):
                stages.add(stage)
    return stages == {0, 1, 2}


def _mentions_missing_inputs(summary):
    missing = ' '.join(summary.get('unfinished', ())).casefold()
    actions = ' '.join(summary.get('next_actions', ())).casefold()
    value = r'(?:(?:replacement|new)\s+)?(?:service[- ]charge\s+)?(?:amount|value)'
    rule = r'(?:business\s+)?(?:rule|criteria|conditions)'
    absent = (r'(?:missing|(?:has|have|had|was|were)\s+not\s+(?:been\s+)?'
              r'(?:provided|specified|supplied)|not\s+(?:been\s+)?'
              r'(?:provided|specified|supplied)|unknown|required|need\w*|await\w*)')
    return bool(re.search(absent, missing) and re.search(value, missing) and re.search(rule, missing)
                and re.search(r'\b(?:ask|request|clarif\w*|provid\w*|specif\w*)\b', actions)
                and re.search(value, actions) and re.search(rule, actions)
                and not re.search(r'(?:amount|value)\s*[:=]\s*\d', missing))


def score(name, writes, project, output, before, result, rows):
    """Report evidence, acceptance, and formatting independently."""
    after = project.state()['files']
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    recorded = result.get('writes', [])
    snapshots = dict(before)
    allowed = set(writes)
    chain_valid = True
    for row in recorded:
        path = row['path']
        if path not in allowed or row.get('before') != snapshots.get(path):
            chain_valid = False
        snapshots[path] = row.get('after')
    receipt_paths = {row['path'] for row in recorded}
    tests, integrity = verify_test_receipts(result.get('commands', []), project, output)
    finish = result.get('summary') or {}
    summary = finish.get('summary', '')
    checks = {
        'expected_files_changed': changed == allowed,
        'no_unauthorized_changes': changed <= allowed,
        'write_receipts_match': chain_valid and receipt_paths == changed and snapshots == after,
        'current_passing_tests': tests == 'passed' and integrity['valid'],
        'expected_completion': result.get('status') == (
            'incomplete' if name == 'insufficient' else 'complete'),
    }
    formatting = None
    if name == 'insufficient':
        checks['missing_inputs_and_clarification'] = _mentions_missing_inputs(finish)
    elif name == 'refactor':
        checks['service_fee_assigned_and_used'] = _service_fee_refactor(project.root / 'pricing.py')
    elif name in {'multi-file', 'tool-cycles'}:
        checks['ordered_test_receipts'] = integrity['valid'] and _receipt_sequence(
            rows, result.get('commands', []), before, allowed)
    elif name == 'test-generation':
        checks['exactly_two_assertions'] = len(_tree(project.root / 'test_cases.py').body) == 2
    elif name == 'unfamiliar':
        markers = {'SUBTOTAL': '21', 'TOTAL': '23', 'LIMIT_MIN': '1', 'LIMIT_MAX': '100'}
        checks['expected_reported_values'] = all(re.search(
            rf'\b{key}\s*=\s*(?:\w+\(3,\s*7\)\s*=\s*)?{value}\b(?!\.\d)', summary, re.I)
            for key, value in markers.items())
        formatting = all(re.search(rf'\b{key}={value}\b(?!\.\d)', summary) for key, value in markers.items())
    elif name == 'no-change':
        checks['correct_no_change_conclusion'] = bool(
            re.search(r'\btotal\s*\(3,\s*7\)\x60?\s*(?:(?:correctly|indeed)\s+)?'
                      r'(?:evaluates to|returns|equals|is|==|=)\s*\x60?23\b(?!\.\d)',
                      summary, re.I)
            and re.search(
                r'\b(?:NO_CHANGE_NEEDED|no (?:code )?(?:changes?|edits?) (?:are |were )?(?:needed|required|necessary))\b',
                summary, re.I))
        formatting = bool(re.search(r'\bNO_CHANGE_NEEDED\b', summary))
    return {'rubric_version': VERSION, 'passed': all(checks.values()) and formatting is not False,
            'acceptance_passed': all(checks.values()), 'checks': checks,
            'format_adherence': formatting, 'changed': sorted(changed), 'test_status': tests,
            'receipt_integrity': integrity}
