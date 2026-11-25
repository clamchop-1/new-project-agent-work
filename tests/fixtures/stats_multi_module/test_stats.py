import unittest
import calculator
import stats

class StatsTests(unittest.TestCase):
    def test_average(self):
        self.assertEqual(calculator.average([2, 4, 6]), 4)
        self.assertEqual(calculator.average([-4, 0, 4]), 0)
        self.assertEqual(calculator.average([7]), 7)
    def test_empty(self):
        with self.assertRaises(ValueError): calculator.average([])
    def test_clamp(self):
        self.assertEqual(stats.clamp(5, 0, 3), 3)
        self.assertEqual(stats.clamp(-1, 0, 3), 0)
        self.assertEqual(stats.clamp(2, 0, 3), 2)
