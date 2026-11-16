import argparse
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
from statistics import mean

from agent_config import DEFAULT_ENDPOINT, DEFAULT_MODEL, validate_endpoint
from local_agent import create_inference_provider
from inference_provider import ProviderFailure
from model_registry import CAPABILITY_NAMES, load_performance_evidence, validate_registry


TASK_PATTERNS = {
    'code_generation': ('implement', 'write code', 'create a function', 'add a function',
                        'generate code', 'refactor', 'build a feature', 'write a script'),
    'debugging': ('debug', 'fix', 'bug', 'traceback', 'exception', 'failing', 'broken'),
    'code_review': ('review', 'audit', 'inspect the diff', 'review changes'),
    'architecture_planning': ('architecture', 'design', 'plan', 'trade-off', 'tradeoff'),
    'test_generation': ('write tests', 'generate tests', 'unit test', 'test case', 'test suite'),
    'qa_analysis': ('qa', 'quality assurance', 'verify', 'regression', 'validation'),
    'documentation': ('document', 'documentation', 'readme', 'release notes', 'api reference'),
    'research_extraction': ('research', 'extract information', 'source material', 'compare sources'),
    'summarization': ('summarize', 'summary', 'condense', 'key points'),
    'general_reasoning': ('reason', 'explain', 'analyze', 'analyse', 'investigate'),
}
DEFAULT_CAPABILITY_THRESHOLDS = {name: 0.55 for name in TASK_PATTERNS}
MIN_OUTCOME_SAMPLES_FOR_RANKING = 3


def classify_task(task, agent_role=None):
    """Deterministically classify a task without another model request."""
    text = (str(task) + ' ' + (agent_role or '')).casefold()
    matches = [category for category, phrases in TASK_PATTERNS.items()
               if any(phrase in text for phrase in phrases)]
    if not matches:
        matches = ['general_reasoning']
    return {
        'categories': matches,
        'requirements': {category: DEFAULT_CAPABILITY_THRESHOLDS[category]
                         for category in matches},
        'method': 'deterministic_keyword_rules_v1',
        'confidence': 'medium' if matches != ['general_reasoning'] else 'low',
    }


def discover_candidates(provider_specs):
    """Discover installed models only from caller-authorized provider specs."""
    candidates, errors = [], []
    for provider_id, spec in provider_specs.items():
        endpoint = spec['endpoint']
        profile = spec.get('profile')
        provider = create_inference_provider(provider_id, endpoint, profile) if profile is not None else create_inference_provider(provider_id, endpoint)
        try:
            provider.health_check()
            for row in provider.models():
                if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                    continue
                if (not 1 <= len(row['name']) <= 160
                        or any(ord(char) < 32 or ord(char) == 127 for char in row['name'])):
                    continue
                if row.get('remote_host') or row.get('remote_model') or 'cloud' in row['name'].casefold():
                    continue
                digest = row.get('digest')
                if digest is not None and (not isinstance(digest, str) or len(digest) > 256
                                           or any(ord(char) < 32 for char in digest)):
                    continue
                candidates.append({
                    'provider_id': provider_id,
                    'model_id': row['name'],
                    'model_digest': digest,
                    'available': True,
                    'authorized': True,
                })
        except ProviderFailure as exc:
            if exc.security or exc.code == 'authentication':
                raise
            errors.append({'provider_id': provider_id, 'code': exc.code,
                           'retryable': exc.retryable})
        finally:
            provider.close()
    return candidates, errors


def _profile_for(candidate, registry):
    matches = [profile for profile in registry['models']
               if profile['provider_id'] == candidate['provider_id']
               and profile['model_id'] == candidate['model_id']
               and profile['model_digest'] == candidate.get('model_digest')]
    return matches[0] if len(matches) == 1 else None


def _fresh_profile(profile, now=None, max_age_days=90):
    """Remove stale measured claims while retaining explicit provenance."""
    if profile is None:
        return None, False
    now = now or datetime.now(timezone.utc)
    fresh = dict(profile)
    fresh['capabilities'] = dict(profile['capabilities'])
    stale = False
    def current(reference):
        observed = datetime.fromisoformat(reference['observed_at'].replace('Z', '+00:00'))
        return now - observed <= timedelta(days=max_age_days)
    for name, value in list(fresh['capabilities'].items()):
        if value['source'] == 'verified_measurement' and not all(current(ref) for ref in value['evidence_refs']):
            del fresh['capabilities'][name]
            stale = True
    for key in ('tool_calling', 'context_window', 'estimated_memory_gib'):
        value = fresh[key]
        if (value and value['source'] == 'verified_measurement'
                and not all(current(ref) for ref in value['evidence_refs'])):
            fresh[key] = None
            stale = True
    fresh['benchmark_evidence'] = [ref for ref in profile['benchmark_evidence'] if current(ref)]
    stale |= len(fresh['benchmark_evidence']) != len(profile['benchmark_evidence'])
    return fresh, stale


def _evidence_for(candidate, records, task_categories):
    return [row for row in records
            if row['provider_id'] == candidate['provider_id']
            and row['model_id'] == candidate['model_id']
            and row['task_category'] in task_categories
            and (row['model_digest'] is None
                 or row['model_digest'] == candidate.get('model_digest'))]


def _profile_confidence(profile, requirements):
    if profile is None:
        return 'low'
    sources = [profile['capabilities'].get(name, {}).get('source')
               for name in requirements]
    context = profile['context_window']
    tool_calling = profile['tool_calling']
    if ('unverified_estimate' in sources
            or any(item is not None and item['source'] == 'unverified_estimate'
                   for item in (context, tool_calling))):
        return 'low'
    if (all(source == 'verified_measurement' for source in sources)
            and context and context['source'] == 'verified_measurement'
            and tool_calling and tool_calling['source'] == 'verified_measurement'
            and profile['benchmark_evidence']):
        return 'high'
    return 'medium' if any(sources) else 'low'


def route_model(task, candidates, registry, *, performance_evidence=(),
                requested_context=16384, max_context=32768,
                provider_mode='local_only', authorized_providers=('ollama',),
                provider_filter=None, minimum_capabilities=None,
                preferred_models=(), excluded_models=(),
                conservative_default=None, model_override=None, agent_role=None,
                priority='normal', resource_limits=None, provider_errors=(),
                estimated_workload=None):
    """Rank pre-discovered candidates after authorization and hard constraints."""
    registry = validate_registry(registry)
    if provider_mode not in ('configured', 'local_only', 'remote_allowed'):
        raise ValueError('Unsupported routing provider mode.')
    if priority not in ('low', 'normal', 'high', 'critical'):
        raise ValueError('Routing priority is unsupported.')
    estimated_workload = dict(estimated_workload or {})
    if (estimated_workload.keys() - {'prompt_tokens', 'output_tokens'}
            or any(type(value) is not int or not 0 <= value <= 131072
                   for value in estimated_workload.values())):
        raise ValueError('Estimated workload must contain bounded token counts.')
    candidates = list(candidates)
    if len(candidates) > 512 or len(preferred_models) > 128 or len(excluded_models) > 128:
        raise ValueError('Routing candidate and preference lists exceed supported bounds.')
    if type(requested_context) is not int or type(max_context) is not int or not 1 <= requested_context <= max_context <= 131072:
        raise ValueError('Requested context must fit the configured context ceiling.')
    task_info = classify_task(task, agent_role)
    resource_limits = dict(resource_limits or {})
    memory_cap = resource_limits.get('max_memory_gib')
    if (memory_cap is not None and
            (type(memory_cap) not in (int, float) or not 0 < memory_cap <= 1024)):
        raise ValueError('Maximum memory limit must be between zero and 1024 GiB.')
    requirements = dict(task_info['requirements'])
    explicit_requirements = set()
    if minimum_capabilities:
        for capability, threshold in minimum_capabilities.items():
            if capability not in CAPABILITY_NAMES or type(threshold) not in (int, float) or not 0 <= threshold <= 1:
                raise ValueError('Minimum capability requirements are invalid.')
            requirements[capability] = max(requirements.get(capability, 0), threshold)
            explicit_requirements.add(capability)
    allowed = set(authorized_providers)
    if provider_mode == 'local_only':
        allowed &= {'ollama'}
    considered, eligible = [], []
    for original in candidates:
        candidate = dict(original) if isinstance(original, Mapping) else {}
        model_label = candidate.get('model_id')
        provider_id = candidate.get('provider_id')
        row = {'provider_id': provider_id, 'model_id': model_label,
               'model_digest': candidate.get('model_digest'), 'available': candidate.get('available') is True,
               'status': 'eligible', 'excluded_reasons': [], 'evidence': []}
        considered.append(row)
        reason = row['excluded_reasons']
        if (not isinstance(model_label, str) or not 1 <= len(model_label) <= 160
                or any(ord(char) < 32 or ord(char) == 127 for char in model_label)
                or not isinstance(provider_id, str)):
            reason.append('malformed_candidate')
        if not isinstance(provider_id, str) or provider_id not in allowed or candidate.get('authorized') is not True:
            reason.append('provider_not_authorized')
        if provider_filter and provider_id != provider_filter:
            reason.append('provider_override_mismatch')
        if candidate.get('available') is not True:
            reason.append('model_unavailable')
        if not isinstance(model_label, str) or 'cloud' in model_label.casefold():
            reason.append('unsupported_model_identity')
        digest = candidate.get('model_digest')
        if (digest is not None and (not isinstance(digest, str) or len(digest) > 256
                                    or any(ord(char) < 32 for char in digest))):
            reason.append('malformed_model_digest')
        if isinstance(model_label, str) and (
                model_label in set(excluded_models)
                or f'{provider_id}/{model_label}' in set(excluded_models)):
            reason.append('operator_excluded')
        if model_override and model_label != model_override:
            reason.append('model_override_mismatch')
        matched_profile = _profile_for(candidate, registry) if not reason else None
        profile, stale_profile_evidence = _fresh_profile(matched_profile)
        if stale_profile_evidence:
            row['profile_stale_evidence'] = True
        if (not reason and matched_profile is None and any(
                item['provider_id'] == provider_id and item['model_id'] == model_label
                for item in registry['models'])):
            row['profile_stale_or_digest_mismatch'] = True
        if not reason and profile is not None:
            context_profile = profile['context_window']
            if context_profile and context_profile['value'] < requested_context:
                reason.append('context_window_too_small')
            tool_profile = profile['tool_calling']
            if tool_profile and tool_profile['value'] is False:
                reason.append('tool_calling_unsupported')
            for capability, threshold in requirements.items():
                value = profile['capabilities'].get(capability)
                if value and value['value'] < threshold:
                    reason.append('capability_below_minimum:' + capability)
                elif value is None and capability in explicit_requirements:
                    reason.append('capability_evidence_missing:' + capability)
            limits = resource_limits or {}
            memory_cap = limits.get('max_memory_gib')
            memory_profile = profile['estimated_memory_gib']
            if memory_cap is not None and memory_profile and memory_profile['value'] > memory_cap:
                reason.append('memory_limit_exceeded')
        elif not reason and explicit_requirements:
            reason.extend('capability_evidence_missing:' + capability
                          for capability in sorted(explicit_requirements))
        elif not reason:
            context_profile = tool_profile = memory_profile = None
        if reason:
            row['status'] = 'excluded'
            continue
        unknown = []
        if profile is None:
            unknown.append('capabilities_context_tool_calling_memory')
        else:
            for capability in requirements:
                if capability not in profile['capabilities']:
                    unknown.append('capability:' + capability)
                    if capability in explicit_requirements:
                        reason.append('capability_evidence_missing:' + capability)
            if profile['context_window'] is None:
                unknown.append('context_window')
            elif profile['context_window']['source'] == 'unverified_estimate':
                unknown.append('unverified_estimate:context_window')
            if profile['tool_calling'] is None:
                unknown.append('tool_calling')
            elif profile['tool_calling']['source'] == 'unverified_estimate':
                unknown.append('unverified_estimate:tool_calling')
            if (resource_limits or {}).get('max_memory_gib') is not None and profile['estimated_memory_gib'] is None:
                unknown.append('memory_estimate')
            elif ((resource_limits or {}).get('max_memory_gib') is not None
                  and profile['estimated_memory_gib']['source'] == 'unverified_estimate'):
                unknown.append('unverified_estimate:memory_estimate')
        row['uncertainties'] = unknown
        if reason:
            row['status'] = 'excluded'
            row['excluded_reasons'] = reason
            continue
        row['profile_source_summary'] = sorted({
            value['source'] for value in (profile['capabilities'].values() if profile else [])
        })
        if profile:
            for capability in requirements:
                value = profile['capabilities'].get(capability)
                if value and value['source'] == 'unverified_estimate':
                    unknown.append('unverified_estimate:' + capability)
        row['_profile'] = profile
        row['_records'] = _evidence_for(
            candidate, performance_evidence, task_info['categories']
        )
        eligible.append(row)

    if not eligible:
        return {'status': 'no_eligible_model', 'task_classification': task_info,
                'candidate_models': considered, 'selected': None,
                'confidence': 'none', 'approval_required': False,
                'provider_discovery_errors': list(provider_errors),
                'constraints': {'provider_mode': provider_mode, 'requested_context': requested_context,
                                'max_context': max_context, 'priority': priority}}

    preferred = {name: index for index, name in enumerate(preferred_models)}
    def rank(row):
        profile = row['_profile']
        cap_values = [profile['capabilities'][name]['value'] for name in requirements
                      if profile and name in profile['capabilities']]
        records = row['_records']
        successes = [item for item in records if item['outcome'] == 'success']
        enough_outcomes = len(records) >= MIN_OUTCOME_SAMPLES_FOR_RANKING
        success_rate = len(successes) / len(records) if enough_outcomes else -1
        recent_latency = (mean(item['latency_ms'] for item in records)
                          if enough_outcomes else float('inf'))
        preference = preferred.get(
            f"{row['provider_id']}/{row['model_id']}",
            preferred.get(row['model_id'], len(preferred)),
        )
        return (preference,
                len(row['uncertainties']), -success_rate,
                -(mean(cap_values) if cap_values else -1), recent_latency,
                row['provider_id'], row['model_id'], row['model_digest'] or '')

    # Prefer supported capability evidence. If none is sufficient, only the
    # explicitly configured conservative default may be chosen as uncertain.
    evidenced = [row for row in eligible if row['_profile'] and all(
        name in row['_profile']['capabilities'] for name in requirements)]
    if evidenced:
        pool = evidenced
    elif conservative_default:
        defaults = [row for row in eligible if row['model_id'] == conservative_default]
        pool = defaults or eligible
    else:
        pool = eligible
    selected = min(pool, key=rank)
    selected_profile = selected.pop('_profile')
    selected_records = selected.pop('_records')
    for row in eligible:
        row.pop('_profile', None)
        row.pop('_records', None)
    recent_records = [item for item in selected_records]
    capability_sources = {}
    if selected_profile:
        capability_sources = {key: value['source'] for key, value in selected_profile['capabilities'].items()}
    complete_verified = bool(selected_profile) and all(
        selected_profile['capabilities'].get(name, {}).get('source') == 'verified_measurement'
        for name in requirements)
    sources_for_required = [selected_profile['capabilities'].get(name, {}).get('source')
                            for name in requirements] if selected_profile else []
    profile_confidence = _profile_confidence(selected_profile, requirements)
    if profile_confidence == 'low':
        confidence = 'low'
    elif complete_verified and selected_profile['context_window'] and selected_profile['tool_calling'] and recent_records:
        confidence = 'high'
    elif selected_profile and selected_profile['capabilities']:
        confidence = 'medium'
    else:
        confidence = 'low'
    status = 'selected' if not selected['uncertainties'] else 'uncertain'
    selected['status'] = status
    selected['selected_evidence'] = {
        'capability_sources': capability_sources,
        'profile_field_sources': ({
            key: (value['source'] if value else None)
            for key, value in {
                'tool_calling': selected_profile['tool_calling'],
                'context_window': selected_profile['context_window'],
                'estimated_memory_gib': selected_profile['estimated_memory_gib'],
            }.items()
        } if selected_profile else {}),
        'profile_confidence': profile_confidence,
        'benchmark_reference_count': len(selected_profile['benchmark_evidence']) if selected_profile else 0,
        'verified_outcome_count': len(recent_records),
        'verified_successes': sum(item['outcome'] == 'success' for item in recent_records),
        'verified_failures': sum(item['outcome'] == 'failure' for item in recent_records),
        'benchmark_ids': sorted({item['test_or_benchmark_id'] for item in recent_records}),
        'known_limitations': selected_profile['known_limitations'] if selected_profile else [],
        'outcomes_used_for_ranking': len(recent_records) >= MIN_OUTCOME_SAMPLES_FOR_RANKING,
    }
    return {
        'status': status,
        'task_classification': task_info,
        'candidate_models': considered,
        'selected': {'provider_id': selected['provider_id'], 'model_id': selected['model_id'],
                     'model_digest': selected['model_digest']},
        'reason': ('Best eligible profile and verified outcomes under deterministic tie-breaking.'
                   if status == 'selected' else
                   'Capability or resource evidence is incomplete; selected only within explicit provider and context constraints.'),
        'confidence': confidence,
        'selected_evidence': selected['selected_evidence'],
        'constraints': {'provider_mode': provider_mode,
                        'authorized_providers': sorted(allowed),
                        'requested_context': requested_context, 'max_context': max_context,
                        'minimum_capabilities': requirements,
                        'preferred_models': list(preferred_models),
                        'excluded_models': list(excluded_models),
                        'agent_role': agent_role, 'priority': priority,
                        'resource_limits': dict(resource_limits or {}),
                        'estimated_workload': estimated_workload},
        'approval_required': False,
        'provider_discovery_errors': list(provider_errors),
        'model_override': model_override,
    }


def read_recent_performance(path):
    """Corrupt/stale performance rows are ignored; profiles remain authoritative."""
    return load_performance_evidence(path, now=datetime.now(timezone.utc))


def get_models(endpoint, provider_name='ollama', provider_profile=None):
    if provider_name == 'ollama':
        endpoint = validate_endpoint(endpoint)
    provider = (create_inference_provider(provider_name, endpoint)
                if provider_profile is None else
                create_inference_provider(provider_name, endpoint, provider_profile))
    try:
        health_check = getattr(provider, 'health_check', None)
        if health_check is not None:
            health_check()
        return [
            item['name'] for item in provider.models()
            if not item.get('remote_host') and not item.get('remote_model')
            and 'cloud' not in item.get('name', '').casefold()
        ]
    finally:
        close = getattr(provider, 'close', None)
        if close is not None:
            close()

def choose_model(models, task, provider_name='ollama', registry=None,
                 conservative_default=DEFAULT_MODEL):
    """Compatibility wrapper that now uses evidence-aware routing.

    Name-only callers get an explicitly uncertain decision and the established
    configured default where available; model names are never treated as
    capability evidence.
    """
    candidates = []
    for item in models:
        model_id = item if isinstance(item, str) else item.get('name')
        digest = None if isinstance(item, str) else item.get('digest')
        if isinstance(model_id, str):
            candidates.append({'provider_id': provider_name, 'model_id': model_id,
                               'model_digest': digest, 'available': True,
                               'authorized': True})
    decision = route_model(
        task, candidates, registry or {'schema_version': 1, 'models': []},
        authorized_providers=(provider_name,), provider_mode='configured',
        conservative_default=conservative_default,
    )
    if decision['selected'] is None:
        raise RuntimeError('No available model satisfies the routing constraints.')
    return {
        **decision,
        'task_category': ', '.join(decision['task_classification']['categories']),
        'provider': decision['selected']['provider_id'],
        'model': decision['selected']['model_id'],
        'cloud_used': False,
    }

def main():
    parser = argparse.ArgumentParser(description="LocalCodingAgent model router")
    parser.add_argument("--task", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--provider", choices=['ollama'], default='ollama')
    parser.add_argument('--model', default='auto')
    parser.add_argument('--context', type=int, default=16384)
    parser.add_argument('--max-context', type=int, default=32768)
    parser.add_argument('--preferred-model', action='append', default=[])
    parser.add_argument('--exclude-model', action='append', default=[])
    args = parser.parse_args()
    from pathlib import Path
    from model_registry import load_registry
    hub = Path(__file__).resolve().parent
    candidates, provider_errors = discover_candidates({
        args.provider: {'endpoint': validate_endpoint(args.endpoint)},
    })
    registry = load_registry(hub / '.local' / 'model-capabilities.json')
    performance = read_recent_performance(hub / '.local' / 'model-performance.jsonl')
    decision = route_model(
        args.task, candidates, registry, performance_evidence=performance,
        requested_context=args.context, max_context=args.max_context,
        authorized_providers=(args.provider,), provider_mode='configured',
        model_override=None if args.model == 'auto' else args.model,
        conservative_default=DEFAULT_MODEL if args.model == 'auto' else None,
        preferred_models=args.preferred_model, excluded_models=args.exclude_model,
        provider_errors=provider_errors,
    )
    print(json.dumps(decision, indent=2))

if __name__ == "__main__":
    main()
