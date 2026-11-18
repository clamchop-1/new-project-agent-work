"""Operator CLI for explicitly allowlisted project knowledge retrieval."""
import argparse
import json
from pathlib import Path
import sys

from agent import PROJECTS
from agent_config import config_path, legacy_name_map, load_config, selector_map
from agent_core import AgentError, Project
from skill_library import CATEGORIES, SkillLibrary
from knowledge_index import KnowledgeIndex


def _project(selector: str) -> Project:
    config = load_config(config_path(Path(__file__).resolve().parent), PROJECTS)
    selectors = selector_map(config)
    if selector not in selectors:
        raise AgentError('Project selector is not registered in host configuration.')
    name = config['projects'][selectors[selector]]['name']
    return Project(legacy_name_map(config)[name])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='LocalCodingAgent')
    parser.add_argument('--json', action='store_true', dest='json_output',
                        help='print complete structured JSON output')
    parser.add_argument('action', choices=['status', 'sources', 'source-add', 'source-remove',
        'index', 'rebuild', 'search', 'inspect', 'verify', 'remove', 'recover'])
    parser.add_argument('--path')
    parser.add_argument('--source-type', choices=['project_documentation', 'curated_reference'],
                        default='project_documentation')
    parser.add_argument('--query')
    parser.add_argument('--category', choices=sorted(CATEGORIES), default='general')
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--chunk-id')
    parser.add_argument('--operator')
    parser.add_argument('--confirm-stale-lock', action='store_true')
    parser.add_argument('--embedding-model', help='already installed local Ollama model; never downloaded')
    return parser


def _print(value, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return
    if isinstance(value, dict) and 'results' in value:
        print(f"Knowledge retrieval: {value.get('strategy')} ({value.get('embedding_status')})")
        for number, row in enumerate(value['results'], 1):
            print(f"\n{number}. {row['path']}:{row['line_start']}-{row['line_end']} "
                  f"[{row['source_type']}; {row['evidence_state']}; score {row['relevance']}]")
            print(row['content'][:1800])
        for number, row in enumerate(value.get('skills', []), 1):
            print(f"\nApproved skill {number}. {row['skill_id']} v{row['version']} "
                  f"[{row['trust_class']}; evidence verified]")
            print(row['procedure'][:1800])
        if not value['results'] and not value.get('skills'):
            print('No relevant approved knowledge found.')
        return
    if isinstance(value, dict):
        for key, item in value.items():
            print(f'{key}: {json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else item}')
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    project = _project(args.project)
    index = KnowledgeIndex(project)
    if args.action == 'status':
        result = index.status()
    elif args.action == 'sources':
        result = {'sources': index.list_sources()}
    elif args.action == 'source-add':
        if not args.path:
            raise AgentError('source-add requires --path.')
        result = index.add_source(args.path, source_type=args.source_type)
    elif args.action in {'source-remove', 'remove'}:
        if not args.path:
            raise AgentError(f'{args.action} requires --path (source root relative to the project).')
        result = index.remove_source(args.path)
    elif args.action in {'index', 'rebuild'}:
        provider = None
        if args.embedding_model:
            from knowledge_embeddings import OllamaEmbeddingProvider
            provider = OllamaEmbeddingProvider(args.embedding_model)
        try:
            result = index.index(provider=provider, rebuild=args.action == 'rebuild')
        finally:
            if provider:
                provider.close()
    elif args.action == 'search':
        if not args.query:
            raise AgentError('search requires --query.')
        result = index.search(args.query, limit=args.limit)
        try:
            result['skills'] = SkillLibrary(project).retrieve(category=args.category,
                problem=args.query, limit=min(5, args.limit))
        except AgentError as exc:
            result['skills'] = []
            result['skill_registry_status'] = 'unavailable_fail_closed'
    elif args.action == 'inspect':
        if not args.chunk_id:
            raise AgentError('inspect requires --chunk-id.')
        result = index.inspect(args.chunk_id)
    elif args.action == 'recover':
        if not args.operator:
            raise AgentError('recover requires --operator.')
        result = index.recover(operator=args.operator, confirm_stale_lock=args.confirm_stale_lock)
    else:
        result = index.verify()
    _print(result, args.json_output)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (AgentError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'knowledge index error: {exc}', file=sys.stderr)
        raise SystemExit(2)
