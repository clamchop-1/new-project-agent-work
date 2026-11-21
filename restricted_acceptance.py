"""A bounded AST interpreter for the fixed calculator acceptance exercise.

This is an evaluator, not a Python sandbox. Submitted source is parsed and interpreted
node-by-node; it is never compiled or executed as Python. Empty input must raise
``ValueError``. Only the syntax and primitive operations listed below are accepted.
"""
import ast
import math


MAX_SOURCE_BYTES = 8192
MAX_AST_NODES = 160
MAX_AST_DEPTH = 24
MAX_NUMBER_MAGNITUDE = 1_000_000_000
MAX_INPUT_LIST_LENGTH = 64
MAX_OPERATIONS = 500
MAX_FUNCTION_CALLS = 8
MAX_OUTPUT_CHARS = 256
MAX_ERROR_MESSAGE_CHARS = 120

# Tuples keep the operator-owned specification immutable. Each row is
# (case label, input list, expected result, expected error type).
ACCEPTANCE_CASES = (
    ('typical', (2, 4, 6), 4.0, None),
    ('single', (5,), 5.0, None),
    ('empty', (), None, 'ValueError'),
    ('zeros', (0, 0), 0.0, None),
    ('negative', (-2, 0), -1.0, None),
    ('fractional', (1.5, 2.5), 2.0, None),
    ('boundary_magnitude', (1_000_000, 1_000_000), 1_000_000.0, None),
)


class AcceptanceRejected(ValueError):
    """Submitted source or input violated the restricted evaluator contract."""


class _DeclaredValueError(Exception):
    pass


_ALLOWED_NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg,
    ast.Return, ast.Assign, ast.If, ast.Raise,
    ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call,
    ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
}
_BINARY = {ast.Add, ast.Sub, ast.Mult, ast.Div}
_UNARY = {ast.UAdd, ast.USub, ast.Not}
_COMPARE = {ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE}
_BUILTINS = {'len', 'sum'}


def _bounded_tree(source):
    if type(source) is not str:
        raise AcceptanceRejected('source must be text')
    try:
        raw = source.encode('utf-8')
    except UnicodeError as exc:
        raise AcceptanceRejected('source encoding is invalid') from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise AcceptanceRejected('source exceeds the size limit')
    try:
        tree = ast.parse(source, mode='exec')
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise AcceptanceRejected('source syntax is invalid') from exc

    count = 0
    stack = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_AST_NODES:
            raise AcceptanceRejected('source exceeds the AST node limit')
        if depth > MAX_AST_DEPTH:
            raise AcceptanceRejected('source exceeds the AST depth limit')
        if type(node) not in _ALLOWED_NODES:
            raise AcceptanceRejected('unsupported syntax: ' + type(node).__name__)
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))

    if len(tree.body) != 1 or type(tree.body[0]) is not ast.FunctionDef:
        raise AcceptanceRejected('source must define exactly one function')
    function = tree.body[0]
    if (function.name != 'calculate_average' or function.decorator_list
            or function.returns is not None or function.type_comment is not None):
        raise AcceptanceRejected('only undecorated calculate_average is allowed')
    args = function.args
    if (len(args.args) != 1 or args.args[0].arg != 'numbers'
            or args.posonlyargs or args.kwonlyargs or args.vararg is not None
            or args.kwarg is not None or args.defaults or args.kw_defaults):
        raise AcceptanceRejected('calculate_average must take only positional numbers')
    if not function.body:
        raise AcceptanceRejected('function body is empty')

    local_names = {'numbers'}
    for node in ast.walk(function):
        if isinstance(node, ast.Name):
            if node.id.startswith('__'):
                raise AcceptanceRejected('dunder identifiers are forbidden')
            if isinstance(node.ctx, ast.Store):
                if node.id in _BUILTINS or node.id == 'numbers':
                    raise AcceptanceRejected('argument and primitive names cannot be reassigned')
                local_names.add(node.id)
        elif isinstance(node, ast.arg):
            if node.arg.startswith('__') or node.arg != 'numbers':
                raise AcceptanceRejected('unexpected parameter')
        elif isinstance(node, ast.Constant):
            value = node.value
            if type(value) in (int, float):
                _number(value)
            elif isinstance(value, str):
                if len(value) > MAX_ERROR_MESSAGE_CHARS:
                    raise AcceptanceRejected('error text exceeds the size limit')
                parent_is_error = False
                # Strings are permitted only as the single message argument to
                # the explicitly supported ValueError raise.
                for parent in ast.walk(function):
                    if isinstance(parent, ast.Raise) and isinstance(parent.exc, ast.Call):
                        call = parent.exc
                        if (isinstance(call.func, ast.Name) and call.func.id == 'ValueError'
                                and len(call.args) == 1 and call.args[0] is node
                                and not call.keywords):
                            parent_is_error = True
                            break
                if not parent_is_error:
                    raise AcceptanceRejected('text values are allowed only in ValueError')
            else:
                raise AcceptanceRejected('only numeric constants are supported')
        elif isinstance(node, ast.Call):
            allowed_primitive = isinstance(node.func, ast.Name) and node.func.id in _BUILTINS
            allowed_error = (
                isinstance(node.func, ast.Name) and node.func.id == 'ValueError'
                and len(node.args) == 1 and not node.keywords
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and isinstance(next((parent for parent in ast.walk(function)
                                     if isinstance(parent, ast.Raise) and parent.exc is node), None), ast.Raise)
            )
            if (not (allowed_primitive or allowed_error)
                    or (allowed_primitive and (len(node.args) != 1 or node.keywords))):
                raise AcceptanceRejected('only len(numbers) and sum(numbers) calls are allowed')
        elif isinstance(node, ast.Raise):
            call = node.exc
            if (not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name)
                    or call.func.id != 'ValueError' or len(call.args) != 1
                    or not isinstance(call.args[0], ast.Constant)
                    or not isinstance(call.args[0].value, str) or call.keywords
                    or node.cause is not None):
                raise AcceptanceRejected('only raise ValueError with fixed text is allowed')
        elif isinstance(node, ast.Assign):
            if (len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)
                    or node.type_comment is not None):
                raise AcceptanceRejected('only simple local assignment is allowed')
        elif isinstance(node, ast.BinOp) and type(node.op) not in _BINARY:
            raise AcceptanceRejected('arithmetic operator is unsupported')
        elif isinstance(node, ast.UnaryOp) and type(node.op) not in _UNARY:
            raise AcceptanceRejected('unary operator is unsupported')
        elif isinstance(node, ast.BoolOp) and type(node.op) not in (ast.And, ast.Or):
            raise AcceptanceRejected('boolean operator is unsupported')
        elif isinstance(node, ast.Compare):
            if any(type(op) not in _COMPARE for op in node.ops):
                raise AcceptanceRejected('comparison operator is unsupported')
        elif isinstance(node, ast.If) and node.orelse and any(
                type(child) not in _ALLOWED_NODES for child in node.orelse):
            raise AcceptanceRejected('conditional syntax is unsupported')

    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in local_names and node.id not in _BUILTINS and node.id != 'ValueError':
                raise AcceptanceRejected('name is not a parameter, local, or approved primitive')
    return function


def _number(value):
    if type(value) not in (int, float) or abs(value) > MAX_NUMBER_MAGNITUDE:
        raise AcceptanceRejected('numeric value exceeds the supported bound')
    if isinstance(value, float) and not math.isfinite(value):
        raise AcceptanceRejected('non-finite numbers are unsupported')
    return value


class _Machine:
    def __init__(self):
        self.operations = 0
        self.calls = 0

    def tick(self, call=False):
        self.operations += 1
        if self.operations > MAX_OPERATIONS:
            raise AcceptanceRejected('evaluation operation limit exceeded')
        if call:
            self.calls += 1
            if self.calls > MAX_FUNCTION_CALLS:
                raise AcceptanceRejected('primitive call limit exceeded')

    def expression(self, node, env, depth=1):
        self.tick()
        if depth > MAX_AST_DEPTH:
            raise AcceptanceRejected('evaluation depth limit exceeded')
        child = lambda item: self.expression(item, env, depth + 1)
        if isinstance(node, ast.Constant):
            return _number(node.value)
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise AcceptanceRejected('local variable used before assignment')
            return env[node.id]
        if isinstance(node, ast.Call):
            self.tick(call=True)
            value = child(node.args[0])
            if type(value) not in (tuple, list) or len(value) > MAX_INPUT_LIST_LENGTH:
                raise AcceptanceRejected('len and sum require a bounded input list')
            if node.func.id == 'len':
                return len(value)
            total = 0
            for item in value:
                self.tick()
                _number(item)
                total += item
                _number(total)
            return total
        if isinstance(node, ast.BinOp):
            left, right = child(node.left), child(node.right)
            _number(left)
            _number(right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if isinstance(node.op, ast.Add) and abs(left) + abs(right) > MAX_NUMBER_MAGNITUDE:
                    raise AcceptanceRejected('arithmetic magnitude limit exceeded')
                if isinstance(node.op, ast.Sub) and abs(left) + abs(right) > MAX_NUMBER_MAGNITUDE:
                    raise AcceptanceRejected('arithmetic magnitude limit exceeded')
                result = left + right if isinstance(node.op, ast.Add) else left - right
            elif isinstance(node.op, ast.Mult):
                if left and abs(right) > MAX_NUMBER_MAGNITUDE / abs(left):
                    raise AcceptanceRejected('arithmetic magnitude limit exceeded')
                result = left * right
            else:
                if right == 0:
                    raise AcceptanceRejected('division by zero')
                result = left / right
            return _number(result)
        if isinstance(node, ast.UnaryOp):
            value = child(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            return _number(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BoolOp):
            result = child(node.values[0])
            for item in node.values[1:]:
                if isinstance(node.op, ast.And) and not result:
                    break
                if isinstance(node.op, ast.Or) and result:
                    break
                result = child(item)
            return result
        if isinstance(node, ast.Compare):
            left = _number(child(node.left))
            for op, comparator in zip(node.ops, node.comparators):
                right = _number(child(comparator))
                if isinstance(op, ast.Eq): ok = left == right
                elif isinstance(op, ast.NotEq): ok = left != right
                elif isinstance(op, ast.Lt): ok = left < right
                elif isinstance(op, ast.LtE): ok = left <= right
                elif isinstance(op, ast.Gt): ok = left > right
                else: ok = left >= right
                if not ok:
                    return False
                left = right
            return True
        raise AcceptanceRejected('unsupported expression node')

    def statements(self, body, env, depth=1):
        if depth > MAX_AST_DEPTH:
            raise AcceptanceRejected('evaluation depth limit exceeded')
        for statement in body:
            self.tick()
            if isinstance(statement, ast.Return):
                return True, self.expression(statement.value, env, depth + 1)
            if isinstance(statement, ast.Assign):
                env[statement.targets[0].id] = self.expression(statement.value, env, depth + 1)
                continue
            if isinstance(statement, ast.If):
                branch = statement.body if self.expression(statement.test, env, depth + 1) else statement.orelse
                returned, value = self.statements(branch, env, depth + 1)
                if returned:
                    return True, value
                continue
            if isinstance(statement, ast.Raise):
                raise _DeclaredValueError()
            raise AcceptanceRejected('unsupported statement node')
        return False, None


def evaluate(source, numbers):
    """Interpret calculate_average for one bounded numeric input list."""
    function = _bounded_tree(source)
    if type(numbers) not in (list, tuple) or len(numbers) > MAX_INPUT_LIST_LENGTH:
        raise AcceptanceRejected('input must be a bounded list')
    values = tuple(_number(value) for value in numbers)
    machine = _Machine()
    try:
        returned, result = machine.statements(function.body, {'numbers': values})
    except _DeclaredValueError as exc:
        raise ValueError('submitted function raised ValueError') from exc
    if not returned:
        raise AcceptanceRejected('function did not return a value')
    result = _number(result)
    if len(repr(result)) > MAX_OUTPUT_CHARS:
        raise AcceptanceRejected('result exceeds the output limit')
    return result


def run_acceptance(source):
    """Run the immutable operator acceptance cases against interpreted source."""
    outcomes = []
    for label, values, expected, expected_error in ACCEPTANCE_CASES:
        try:
            actual = evaluate(source, values)
        except AcceptanceRejected:
            passed = False
            actual_kind = 'rejected'
        except ValueError:
            passed = expected_error == 'ValueError'
            actual_kind = 'ValueError'
        else:
            passed = expected_error is None and actual == expected
            actual_kind = type(actual).__name__
        outcomes.append((label, passed, actual_kind))
    return {
        'passed': all(row[1] for row in outcomes),
        'case_count': len(outcomes),
        'passed_count': sum(1 for row in outcomes if row[1]),
        'cases': tuple(outcomes),
    }
