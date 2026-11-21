import unittest

from restricted_acceptance import (
    ACCEPTANCE_CASES,
    MAX_AST_DEPTH,
    MAX_INPUT_LIST_LENGTH,
    MAX_NUMBER_MAGNITUDE,
    MAX_SOURCE_BYTES,
    AcceptanceRejected,
    evaluate,
    run_acceptance,
)


GOOD = '''
def calculate_average(numbers):
    if len(numbers) == 0:
        raise ValueError("numbers must not be empty")
    total = sum(numbers)
    return total / len(numbers)
'''

BAD = '''
def calculate_average(numbers):
    return sum(numbers) / (len(numbers) + 1)
'''


class RestrictedAcceptanceTests(unittest.TestCase):
    def test_correct_implementation_passes_fixed_operator_spec(self):
        report = run_acceptance(GOOD)
        self.assertTrue(report['passed'])
        self.assertEqual(report['case_count'], 7)
        self.assertEqual(report['passed_count'], 7)

    def test_incorrect_implementation_fails_fixed_operator_spec(self):
        report = run_acceptance(BAD)
        self.assertFalse(report['passed'])
        self.assertLess(report['passed_count'], report['case_count'])

    def test_empty_input_requires_explicit_value_error(self):
        with self.assertRaisesRegex(AcceptanceRejected, 'division by zero'):
            evaluate('def calculate_average(numbers):\n    return sum(numbers) / len(numbers)\n', [])
        self.assertTrue(run_acceptance(GOOD)['cases'][2][1])

    def test_legitimate_assignment_and_conditional_syntax(self):
        source = '''
def calculate_average(numbers):
    count = len(numbers)
    if count == 0:
        raise ValueError("empty")
    total = sum(numbers)
    return total / count
'''
        self.assertTrue(run_acceptance(source)['passed'])

    def test_results_are_deterministic(self):
        self.assertEqual(run_acceptance(GOOD), run_acceptance(GOOD))

    def test_specification_is_immutable(self):
        self.assertIsInstance(ACCEPTANCE_CASES, tuple)
        with self.assertRaises(TypeError):
            ACCEPTANCE_CASES[0] = ('changed', (), 0, None)

    def test_rejects_hostile_or_unsupported_syntax(self):
        hostile = (
            'import os\ndef calculate_average(numbers):\n    return 0\n',
            'def calculate_average(numbers):\n    return numbers.__class__\n',
            'def calculate_average(numbers):\n    return open("x")\n',
            'def calculate_average(numbers):\n    return socket.connect(numbers)\n',
            'def calculate_average(numbers):\n    return subprocess.run(numbers)\n',
            'def calculate_average(numbers):\n    return arbitrary(numbers)\n',
            'def calculate_average(numbers):\n    return eval("1")\n',
            'def calculate_average(numbers):\n    while True:\n        pass\n',
            'class X:\n    pass\n',
            'def calculate_average(numbers):\n    return [x for x in numbers]\n',
            'def calculate_average(numbers):\n    try:\n        return 1\n    except:\n        return 0\n',
            'def calculate_average(numbers):\n    global x\n    return 0\n',
            'def calculate_average(numbers):\n    return (lambda: 1)()\n',
        )
        for source in hostile:
            with self.subTest(source=source[:48]), self.assertRaises(AcceptanceRejected):
                evaluate(source, [1])

    def test_rejects_malformed_source_and_wrong_function_shape(self):
        for source in ('def calculate_average(:',
                       'def another(numbers):\n    return 1\n',
                       'def calculate_average(numbers, other):\n    return 1\n'):
            with self.subTest(source=source), self.assertRaises(AcceptanceRejected):
                evaluate(source, [1])

    def test_rejects_excessive_source_and_ast_depth(self):
        with self.assertRaisesRegex(AcceptanceRejected, 'size limit'):
            evaluate(' ' * (MAX_SOURCE_BYTES + 1), [1])
        deep = 'def calculate_average(numbers):\n    return ' + ('+' * (MAX_AST_DEPTH + 4)) + '1\n'
        with self.assertRaises(AcceptanceRejected):
            evaluate(deep, [1])

    def test_rejects_oversized_input_and_numeric_values(self):
        with self.assertRaisesRegex(AcceptanceRejected, 'bounded list'):
            evaluate(GOOD, [1] * (MAX_INPUT_LIST_LENGTH + 1))
        with self.assertRaisesRegex(AcceptanceRejected, 'numeric value'):
            evaluate(GOOD, [MAX_NUMBER_MAGNITUDE + 1])
        with self.assertRaises(AcceptanceRejected):
            evaluate(GOOD, [float('inf')])

    def test_rejects_host_objects_before_calling_overridden_methods(self):
        class HostileList(list):
            def __len__(self):
                raise AssertionError('hostile method must not run')

        class HostileSource(str):
            def encode(self, *args, **kwargs):
                raise AssertionError('hostile method must not run')

        with self.assertRaises(AcceptanceRejected):
            evaluate(GOOD, HostileList([1]))
        with self.assertRaises(AcceptanceRejected):
            evaluate(HostileSource(GOOD), [1])

    def test_rejects_excessive_arithmetic_before_large_result(self):
        source = 'def calculate_average(numbers):\n    return 900000000 * 900000000\n'
        with self.assertRaisesRegex(AcceptanceRejected, 'magnitude'):
            evaluate(source, [1])

    def test_rejects_primitive_call_limit(self):
        statements = '\n'.join(f'    local_{i} = len(numbers)' for i in range(9))
        source = ('def calculate_average(numbers):\n' + statements
                  + '\n    return local_0\n')
        with self.assertRaisesRegex(AcceptanceRejected, 'primitive call limit'):
            evaluate(source, [1])

    def test_rejects_cumulative_operation_limit(self):
        expression = ' + '.join(['sum(numbers)'] * 8)
        source = 'def calculate_average(numbers):\n    return ' + expression + '\n'
        with self.assertRaisesRegex(AcceptanceRejected, 'operation limit'):
            evaluate(source, [1] * MAX_INPUT_LIST_LENGTH)

    def test_rejects_unexpected_runtime_values_and_division_by_zero(self):
        with self.assertRaises(AcceptanceRejected):
            evaluate('def calculate_average(numbers):\n    return numbers / 0\n', [1])
        with self.assertRaisesRegex(AcceptanceRejected, 'division by zero'):
            evaluate('def calculate_average(numbers):\n    return 1 / (len(numbers) - 1)\n', [1])
        with self.assertRaises(AcceptanceRejected):
            evaluate('def calculate_average(numbers):\n    if numbers < 1:\n        return 0\n    return 1\n', [1])


if __name__ == '__main__':
    unittest.main()
