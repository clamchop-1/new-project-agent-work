"""Shared, read-only coding workflow status projection for CLI and dashboard."""

from __future__ import annotations


_ACTIVE = {'created', 'planning', 'generating', 'validating', 'reviewing',
           'approved', 'applying', 'testing'}
_KNOWN = _ACTIVE | {'awaiting_approval', 'completed', 'failed', 'rejected',
                    'cancelled', 'timed_out', 'interrupted'}


def status_projection(row: dict, *, patch_sha256=None, completion_verified=False,
                      candidate_status=None, recovery_status=None) -> dict:
    """Return consistent user-facing status without changing persisted workflow data."""
    state = row.get('state') if isinstance(row, dict) else None
    state = state if isinstance(state, str) and state in _KNOWN else 'unknown'
    approval = row.get('approval') if isinstance(row.get('approval'), dict) else None
    result = row.get('test_result') if isinstance(row.get('test_result'), dict) else None
    exit_code = result.get('exit_code') if result else None
    test_status = ('passed' if type(exit_code) is int and exit_code == 0 else
                   'failed' if type(exit_code) is int else
                   'running' if state == 'testing' else 'not_run')
    if state == 'completed' and not completion_verified:
        test_status = 'receipt_unverified'
    if state == 'failed' and test_status == 'not_run':
        test_status = 'failed_or_infrastructure_error'
    if state == 'cancelled' and test_status == 'not_run':
        test_status = 'cancelled'
    application_status = ('verified' if state == 'completed' and completion_verified else
                          'ambiguous_review_required' if state == 'interrupted' else
                          'in_progress' if state in {'applying', 'testing'} else
                          'not_started' if state in {'created', 'planning', 'generating',
                                                     'validating', 'reviewing',
                                                     'awaiting_approval', 'rejected',
                                                     'cancelled', 'failed', 'timed_out'} else
                          'unverified')
    calls = row.get('role_calls', [])
    attempts = row.get('coder_attempts', [])
    return {
        'workflow_id': row.get('run_id'), 'state': state,
        'stage': state.replace('_', ' '),
        'active': state in _ACTIVE,
        'approval_required': state == 'awaiting_approval',
        'approval_recorded': approval is not None,
        'approval_consumed': bool(approval and approval.get('used') is True),
        'application_status': application_status,
        'test_status': test_status,
        'completion_verified': bool(completion_verified),
        'candidate_status': candidate_status,
        'recovery_journal_status': recovery_status,
        'patch_sha256': patch_sha256 or row.get('patch_sha256'),
        'model_invocation_count': len(calls) if isinstance(calls, list) else 0,
        'retry_count': max(0, len(attempts) - 1) if isinstance(attempts, list) else 0,
        'created_at': row.get('created_at'), 'updated_at': row.get('updated_at'),
        'failure': row.get('failure'),
    }
