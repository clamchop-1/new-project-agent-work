"""Restricted pure-Python fixture executor, not a general OS sandbox.

Only pinned tests and AST-validated, import-free pure functions are supported.
General repository test/shell execution is deliberately refused without a qualified
external sandbox. This narrow executor proves the edit/test loop safely here.
"""
import ast
import hashlib
import json
from pathlib import Path
import sys
import types
import unittest


SAFE_BUILTINS = {'sum': sum, 'len': len, 'min': min, 'max': max, 'abs': abs,
                 'ValueError': ValueError, 'TypeError': TypeError, 'type': type,
                 'int': int}
SAFE_CALLS = set(SAFE_BUILTINS) - {'int'}
HUB = Path(__file__).resolve().parent
FIXTURE_TESTS_ROOT = HUB / '.local' / 'tests'
MILESTONE4_CODING_FIXTURE = (
    FIXTURE_TESTS_ROOT / 'milestone4-live-8a12f7d4216a4ceea723c1dd0b60f89e' / 'repo'
).resolve()
CODING_TEST_CONFIG = {
    'test_file': 'tests/test_progress.py',
    'test_sha256': '989f840b6db6d9f65823a7fa0725867b0abd8bc28554746d8b020bdc6c8a2af7',
    'modules': ['src.progress'],
}
ALLOWED = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.If,
           ast.Raise, ast.Expr, ast.Constant, ast.Name, ast.Load,
           ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call,
           ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
           ast.Mod, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq,
           ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot)


def validate_source(text, *, allow_local_assignment=False):
    tree = ast.parse(text)
    if len(list(ast.walk(tree))) > 1000:
        raise ValueError('Pure fixture source exceeds AST limit')
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    if functions & set(SAFE_BUILTINS):
        raise ValueError('Pure fixtures cannot shadow approved builtins')
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            raise ValueError('Only pure function definitions and a module docstring are allowed')
    allowed = ALLOWED + (ast.Assign, ast.Store) if allow_local_assignment else ALLOWED
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError('Unsupported pure-fixture syntax: ' + type(node).__name__)
        if isinstance(node, ast.Assign) and (
                len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)
                or node.targets[0].id in SAFE_BUILTINS):
            raise ValueError('Fixture assignment requires a single local non-builtin name')
        if isinstance(node, ast.Name) and node.id.startswith('__'):
            raise ValueError('Dunder access rejected')
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list or node.returns or any(a.annotation for a in node.args.args):
                raise ValueError('Decorators/annotations are outside the pure-fixture contract')
        if isinstance(node, ast.Constant):
            if type(node.value) in (int, float) and abs(node.value) > 1000000:
                raise ValueError('Pure-fixture constant too large')
            if isinstance(node.value, str) and len(node.value) > 2000:
                raise ValueError('Pure-fixture string too large')
            if isinstance(node.value, str):
                parent = parents.get(node)
                is_doc = isinstance(parent, ast.Expr)
                is_error = isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in ('ValueError', 'TypeError')
                if not is_doc and not is_error:
                    raise ValueError('String/container construction is outside the numeric fixture contract')
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in SAFE_CALLS:
                raise ValueError('Only explicitly safe builtins may be called; no recursion/import/IO')
    return tree


def _contains_reparse_point(path):
    """Check every existing component, including the leaf, without following links."""
    path = Path(path)
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if component.is_symlink() or getattr(metadata, 'st_file_attributes', 0) & 0x400:
            return True
    return False


def _canonical_root(raw_path):
    raw = Path(raw_path)
    if not raw.is_absolute() or '..' in raw.parts:
        raise ValueError('Fixture root must be an absolute canonical path without traversal.')
    if _contains_reparse_point(raw):
        raise ValueError('Fixture root contains a symlink, junction, or reparse point.')
    try:
        root = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError('Fixture root cannot be resolved safely.') from exc
    if not root.is_dir():
        raise ValueError('Fixture root is not a directory.')
    return root


def _is_milestone4_coding_fixture(root):
    """Milestone 4 coding execution is registered by one exact canonical identity."""
    return root == MILESTONE4_CODING_FIXTURE


def _validate_coding_fixture_file(root, relative):
    candidate = root.joinpath(*Path(relative).parts)
    if not candidate.resolve(strict=True).is_relative_to(root):
        raise ValueError('Coding fixture file escapes the registered repository.')
    if _contains_reparse_point(candidate):
        raise ValueError('Coding fixture file path contains a symlink or reparse point.')
    if not candidate.is_file():
        raise ValueError('Coding fixture file is missing or not regular.')
    return candidate


def _expected_profile(root, config):
    """Return the pinned profile and mode allowed for a canonical fixture."""
    tests_root = FIXTURE_TESTS_ROOT.resolve()
    if root.name.casefold() == 'localagentsandbox':
        return False, False, {
            'test_file': 'tests/test_verified_calculator.py',
            'test_sha256': 'ea91fa9f1bb1b6f984df3e7ace8a7faf8062ab2bb58e926ce02e10054547c98d',
            'modules': ['calculator'],
        }
    if (root.parent.parent == tests_root and root.name == 'repo'
            and root.parent.name.startswith('phase2d-')
            and config.get('test_file') == 'tests/test_reliability.py'):
        return True, False, {
            'test_file': 'tests/test_reliability.py',
            'test_sha256': 'b4c8b7da38eaa3366fac6d01f20c6c06deda7949338dd52708d190bd909b0846',
            'modules': ['pricing', 'rules'],
        }
    if _is_milestone4_coding_fixture(root):
        return False, True, CODING_TEST_CONFIG
    if (root.parent.parent == tests_root and root.name == 'repo'
            and root.parent.name.startswith('milestone3-')
            and config.get('test_file') == 'tests/test_progress.py'):
        return False, True, CODING_TEST_CONFIG
    if root.parent.parent == tests_root:
        return False, False, {
            'test_file': 'tests/test_stats.py',
            'test_sha256': '6a9f34017eb9574fc9f415368eb9c845bb4643f9633ccc9e98de7b7bc98d3181',
            'modules': ['calculator', 'stats'],
        }
    raise ValueError('Unapproved pinned test project')


def _validated_profile(root, config):
    reliability, coding, expected = _expected_profile(root, config)
    if config != expected:
        raise ValueError('Pinned test configuration differs from approved values')
    return reliability, coding


def _load_pinned_config(path):
    """Decode the pinned JSON as UTF-8, accepting an optional Windows BOM."""
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def main():
    root = _canonical_root(sys.argv[1])
    config_argument = Path(sys.argv[2])
    if not config_argument.is_absolute() or '..' in config_argument.parts:
        raise ValueError('Pinned test configuration path must be absolute and traversal-free.')
    if _contains_reparse_point(config_argument):
        raise ValueError('Pinned test configuration path contains a symlink or reparse point.')
    config_path = config_argument.resolve(strict=True)
    expected_config = root / '.local' / 'pinned-tests.json'

    if config_path != expected_config:
        raise ValueError('Unexpected pinned test configuration path')

    config = _load_pinned_config(config_path)

    reliability, coding = _validated_profile(root, config)

    if reliability or coding:
        for relative in (config['test_file'], 'pricing.py', 'rules.py', 'test_cases.py'):
            if coding and relative != config['test_file']:
                continue
            candidate = root / relative
            for part in (candidate, *candidate.parents):
                if part == root:
                    break
                metadata = part.lstat()
                if part.is_symlink() or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('A reliability fixture path is linked')
            if (not candidate.is_file() or candidate.stat().st_nlink != 1
                    or candidate.stat().st_size > 65536):
                raise ValueError('A reliability fixture file is outside the safe size or link limits')

    test = root / config['test_file']
    if coding:
        test = _validate_coding_fixture_file(root, config['test_file'])
    if hashlib.sha256(test.read_bytes()).hexdigest() != config['test_sha256']:
        raise ValueError('Pinned test changed; refusing execution')
    for name in config['modules']:
        source = root.joinpath(*name.split('.')).with_suffix('.py')
        if coding and source.exists():
            source = _validate_coding_fixture_file(root, source.relative_to(root))
        if not source.is_file():
            # Keep an approved-but-missing module importable so the pinned
            # unittest can report the missing implementation as an assertion
            # failure instead of aborting test discovery with ImportError.
            sys.modules[name] = types.ModuleType(name)
            continue
        tree = validate_source(source.read_text(encoding='utf-8-sig'),
                               allow_local_assignment=reliability)
        module = types.ModuleType(name)
        namespace = {'__builtins__': dict(SAFE_BUILTINS), '__name__': name}
        exec(compile(tree, str(source), 'exec'), namespace)
        module.__dict__.update(namespace)
        if coding:
            package = types.ModuleType('src')
            package.__path__ = []
            sys.modules['src'] = package
            module.__package__ = 'src'
            package.progress = module
        sys.modules[name] = module
    # Tests are authored/reviewed by the operator and pinned before model execution.
    # Models cannot edit this file or choose another test module through this tool.
    spec = types.ModuleType('approved_fixture_tests')
    exec(compile(test.read_bytes(), str(test), 'exec'), spec.__dict__)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(spec))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
