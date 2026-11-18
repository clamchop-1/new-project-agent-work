"""Operator CLI for reviewing and retrieving project-scoped reusable skills."""
import argparse
import json
from pathlib import Path
import sys

from agent import PROJECTS
from agent_config import config_path, legacy_name_map, load_config, selector_map
from agent_core import AgentError, Project, atomic_write, encoded
from local_agent import HUB
from skill_library import CATEGORIES, STATUSES, TRUST_CLASSES, SkillLibrary


def _project(selector: str) -> Project:
    config = load_config(config_path(HUB), PROJECTS)
    selectors = selector_map(config)
    if selector not in selectors:
        raise AgentError('Project selector is not registered in host configuration.')
    project_id = selectors[selector]
    name = config['projects'][project_id]['name']
    return Project(legacy_name_map(config)[name])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='LocalCodingAgent')
    parser.add_argument('action', choices=['list', 'show', 'history', 'validate', 'approve',
        'reject', 'correct', 'revoke', 'supersede', 'revalidate', 'retrieve',
        'attach-evidence', 'recover', 'export', 'import'])
    parser.add_argument('--skill-id')
    parser.add_argument('--operator')
    parser.add_argument('--status', choices=sorted(STATUSES))
    parser.add_argument('--category', choices=sorted(CATEGORIES))
    parser.add_argument('--problem')
    parser.add_argument('--procedure')
    parser.add_argument('--reason')
    parser.add_argument('--note')
    parser.add_argument('--scope', choices=['tests', 'procedure'])
    parser.add_argument('--trust-class', choices=sorted(TRUST_CLASSES))
    parser.add_argument('--confirm-sensitive', action='store_true')
    parser.add_argument('--replacement-id')
    parser.add_argument('--run-id')
    parser.add_argument('--task-id')
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--evidence-file', type=Path)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--recovery-action', choices=['finish', 'discard'])
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    project = _project(args.project)
    library = SkillLibrary(project)
    if args.action == 'list':
        result = library.list_skills(status=args.status, limit=args.limit)
    elif args.action == 'show':
        if not args.skill_id:
            raise AgentError('show requires --skill-id.')
        result = library.inspect(args.skill_id)
    elif args.action == 'history':
        if not args.skill_id:
            raise AgentError('history requires --skill-id.')
        result = library.history(args.skill_id)
    elif args.action == 'retrieve':
        if not args.category or not args.problem:
            raise AgentError('retrieve requires --category and --problem.')
        result = library.retrieve(category=args.category, problem=args.problem, limit=args.limit,
                                  run_id=args.run_id, task_id=args.task_id)
    elif args.action == 'recover':
        if not args.recovery_action:
            raise AgentError('recover requires --recovery-action finish or discard.')
        result = library.recover(action=args.recovery_action)
    elif args.action == 'export':
        if not args.skill_id:
            raise AgentError('export requires --skill-id.')
        package = library.export_package(args.skill_id)
        export_dir = project.root / '.local' / 'skills' / 'exports'
        if export_dir.is_symlink():
            raise AgentError('Skill export directory cannot be a symbolic link.')
        export_dir.mkdir(parents=True, exist_ok=True)
        destination = export_dir / (args.skill_id + '.json')
        if destination.exists() or destination.is_symlink():
            raise AgentError('Export target already exists; existing package preserved.')
        atomic_write(destination, encoded(package))
        result = {'path': str(destination), 'sha256': package['sha256'],
                  'status': 'exported_for_explicit_cross_project_import'}
    elif args.action == 'import':
        if args.package is None or not args.operator:
            raise AgentError('import requires --package and --operator.')
        if args.package.is_symlink() or args.package.stat().st_size > 128 * 1024:
            raise AgentError('Cross-project package must be a bounded regular file.')
        result = library.import_package(json.loads(args.package.read_bytes()), operator=args.operator)
    else:
        if not args.skill_id or not args.operator:
            raise AgentError(f'{args.action} requires --skill-id and --operator.')
        if args.action in {'validate', 'revalidate'}:
            if not args.scope or not args.note:
                raise AgentError(f'{args.action} requires --scope and --note.')
            method = library.validate if args.action == 'validate' else library.revalidate
            result = method(args.skill_id, operator=args.operator, scope=args.scope, note=args.note)
        elif args.action == 'approve':
            if not args.trust_class:
                raise AgentError('approve requires --trust-class.')
            result = library.approve(args.skill_id, operator=args.operator,
                                     trust_class=args.trust_class,
                                     confirm_sensitive=args.confirm_sensitive)
        elif args.action == 'reject':
            if not args.reason:
                raise AgentError('reject requires --reason.')
            result = library.reject(args.skill_id, operator=args.operator, reason=args.reason)
        elif args.action == 'revoke':
            if not args.reason:
                raise AgentError('revoke requires --reason.')
            result = library.revoke(args.skill_id, operator=args.operator, reason=args.reason)
        elif args.action == 'correct':
            if not args.problem or not args.procedure:
                raise AgentError('correct requires --problem and --procedure.')
            result = library.correct(args.skill_id, operator=args.operator,
                                     problem=args.problem, procedure=args.procedure,
                                     category=args.category)
        elif args.action == 'supersede':
            if not args.replacement_id or not args.reason:
                raise AgentError('supersede requires --replacement-id and --reason.')
            result = library.supersede(args.skill_id, args.replacement_id,
                                       operator=args.operator, reason=args.reason)
        elif args.action == 'attach-evidence':
            if args.evidence_file is None:
                raise AgentError('attach-evidence requires --evidence-file JSON.')
            if args.evidence_file.is_symlink() or args.evidence_file.stat().st_size > 32768:
                raise AgentError('Evidence reference file must be bounded and non-linked.')
            references = json.loads(args.evidence_file.read_bytes())
            result = library.attach_evidence(args.skill_id, references, operator=args.operator)
        else:
            raise AgentError('Unsupported skill operation.')
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (AgentError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'skill registry error: {exc}', file=sys.stderr)
        raise SystemExit(2)
