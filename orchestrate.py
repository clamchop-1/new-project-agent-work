"""Operator CLI for LocalCodingAgent multi-agent orchestration."""
import argparse
import json
from pathlib import Path
import sys

from agent import PROJECTS, provider_specs
from agent_config import (config_path, legacy_name_map, load_config, selector_map,
                          DEFAULT_ENDPOINT)
from agent_core import AgentError, Project
from orchestrator import (MAX_PLAN_BYTES, OrchestrationService,
                          SchedulerLimits, TaskGraph)
from local_agent import HUB


def _service(project_selector, routing_mode='configured', provider_override=None,
             test_config=None, preferred_models=(), excluded_models=(), max_agents=2,
             memory_budget=8.0):
    config = load_config(config_path(HUB), PROJECTS)
    selectors = selector_map(config)
    if project_selector not in selectors:
        raise AgentError('Project selector is not registered in the host configuration.')
    project_id = selectors[project_selector]
    project_name = config['projects'][project_id]['name']
    project = Project(legacy_name_map(config)[project_name])
    endpoint = config['inference']['endpoint'] if config['inference']['provider'] == 'ollama' else DEFAULT_ENDPOINT
    specs = provider_specs(config, endpoint, routing_mode, provider_override)
    if test_config:
        original = Path(test_config)
        if original.is_symlink():
            raise AgentError('Test configuration cannot be a symbolic link.')
        candidate = original.resolve(strict=True)
        local = (project.root / '.local' / 'tests').resolve(strict=True)
        if not candidate.is_relative_to(local) or candidate.suffix.casefold() != '.json' or candidate.stat().st_size > 65536:
            raise AgentError('Test configuration must be a local JSON file under the selected project .local/tests directory.')
    fallback = config['inference'].get('fallback')
    scheduler = SchedulerLimits(max_concurrent_agents=max_agents, per_provider=max_agents,
                                per_model=1, queue_capacity=32,
                                max_parallel_memory_gib=memory_budget, max_retries=1)
    service = OrchestrationService(
        project, provider_specs=specs, routing_mode=routing_mode, scheduler=scheduler,
        test_config=Path(test_config) if test_config else None,
        preferred_models=tuple(preferred_models), excluded_models=tuple(excluded_models),
        fallback_policy=fallback,
    )
    return service, project


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['decompose', 'propose', 'approve', 'run', 'status', 'pause',
                                           'resume', 'cancel', 'integration-proposal', 'integrate',
                                           'integration-recovery', 'integration-reconcile'])
    parser.add_argument('--project', default='LocalCodingAgent')
    parser.add_argument('--run-id')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--objective')
    parser.add_argument('--scope-path', action='append', default=[])
    parser.add_argument('--plan-sha256')
    parser.add_argument('--task', action='append', default=[])
    parser.add_argument('--write-grant', action='append', default=[], metavar='TASK_ID=PATH')
    parser.add_argument('--proposal-sha256')
    parser.add_argument('--recovery-action', choices=['resume', 'accept_applied'])
    parser.add_argument('--routing-mode', choices=['configured', 'local_only', 'remote_allowed'], default='configured')
    parser.add_argument('--provider', choices=['ollama', 'ssh_tunnel_ollama'])
    parser.add_argument('--test-config')
    parser.add_argument('--preferred-model', action='append', default=[])
    parser.add_argument('--exclude-model', action='append', default=[])
    parser.add_argument('--max-agents', type=int, default=2)
    parser.add_argument('--memory-budget-gib', type=float, default=8.0)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    service, project = _service(
        args.project, args.routing_mode, args.provider, args.test_config,
        args.preferred_model, args.exclude_model, args.max_agents,
        args.memory_budget_gib,
    )
    if args.action == 'decompose':
        if not args.objective or not args.scope_path:
            raise AgentError('decompose requires --objective and one or more --scope-path values.')
        result = service.decompose(args.objective, tuple(args.scope_path), run_id=args.run_id)
    elif args.action == 'propose':
        if not args.plan:
            raise AgentError('propose requires --plan JSON.')
        if args.plan.is_symlink():
            raise AgentError('Plan file cannot be a symbolic link.')
        raw = args.plan.read_bytes()
        if len(raw) > MAX_PLAN_BYTES:
            raise AgentError('Plan file exceeds the bounded size limit.')
        graph = TaskGraph.from_dict(json.loads(raw))
        if graph.project_id != project.identity:
            raise AgentError('Plan project_id does not match the registered project checkout.')
        result = service.propose(graph)
    else:
        if not args.run_id:
            raise AgentError(f'{args.action} requires --run-id.')
        if args.action == 'approve':
            if not args.plan_sha256 or not args.task:
                raise AgentError('approve requires --plan-sha256 and at least one --task.')
            grants = {}
            for value in args.write_grant:
                task_id, sep, path = value.partition('=')
                if not sep or not task_id or not path:
                    raise AgentError('Write grants use TASK_ID=PROJECT_RELATIVE_PATH.')
                grants.setdefault(task_id, []).append(path)
            result = service.approve(args.run_id, expected_plan_sha256=args.plan_sha256,
                                     task_ids=tuple(args.task), write_grants=grants)
        elif args.action == 'run':
            result = service.execute(args.run_id)
        elif args.action == 'status':
            result = service.status(args.run_id)
        elif args.action == 'pause':
            result = {'paused': service.pause(args.run_id)}
        elif args.action == 'resume':
            result = {'resumed': service.resume(args.run_id)}
        elif args.action == 'cancel':
            result = {'cancellation_requested': service.cancel(args.run_id)}
        elif args.action == 'integration-proposal':
            if not args.task:
                raise AgentError('integration-proposal requires one or more --task ids.')
            result = service.prepare_integration(args.run_id, tuple(args.task))
        elif args.action == 'integration-recovery':
            result = service.integration_recovery(args.run_id)
        elif args.action == 'integration-reconcile':
            if not args.proposal_sha256 or not args.recovery_action:
                raise AgentError('integration-reconcile requires --proposal-sha256 and --recovery-action.')
            result = service.reconcile_integration(args.run_id, args.proposal_sha256, args.recovery_action)
        else:
            if not args.proposal_sha256:
                raise AgentError('integrate requires --proposal-sha256 as explicit operator approval.')
            result = service.integrate(args.run_id, args.proposal_sha256)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (AgentError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'orchestration error: {exc}', file=sys.stderr)
        raise SystemExit(2)
