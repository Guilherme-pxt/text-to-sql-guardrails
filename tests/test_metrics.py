import unittest

from sqlguard.metrics import auroc, brier, ece, paired_bootstrap, result_matches, value_eq


class ResultMatchTest(unittest.TestCase):
    def test_same_result(self):
        self.assertTrue(result_matches(["n"], [[11]], ["COUNT(*)"], [[11]]))

    def test_column_order_and_extra_columns(self):
        gold = [["EU", 10.0], ["US", 20.0]]
        self.assertTrue(result_matches(["r", "v"], gold, ["v", "name", "r"], [[20.0, "x", "US"], [10.0, "y", "EU"]]))

    def test_mispaired_rows_fail(self):
        gold = [["EU", 10.0], ["US", 20.0]]
        self.assertFalse(result_matches(["r", "v"], gold, ["r", "v"], [["EU", 20.0], ["US", 10.0]]))

    def test_row_count_and_missing_column(self):
        self.assertFalse(result_matches(["s"], [["a"], ["b"]], ["s"], [["a"]]))
        self.assertFalse(result_matches(["r", "v"], [["EU", 1]], ["r"], [["EU"]]))

    def test_number_rounding_rules(self):
        self.assertTrue(value_eq(7.9963, 8.0))
        self.assertFalse(value_eq(7.9963, 7.99))     # a truncation, not a rounding
        self.assertFalse(value_eq(1.43, 1))          # integer rounding only for values >= 100
        self.assertTrue(value_eq(435823.35, 435823))
        self.assertTrue(value_eq(11, "11"))
        self.assertTrue(value_eq("Pellham Labs", "pellham labs "))
        self.assertFalse(value_eq(None, 0))


class StatsTest(unittest.TestCase):
    def test_paired_bootstrap(self):
        out = paired_bootstrap([0, 0, 1, 1], [1, 1, 1, 1])
        self.assertAlmostEqual(out["delta"], 0.5)
        self.assertLessEqual(out["ci95"][0], out["delta"])
        with self.assertRaises(ValueError):
            paired_bootstrap([1], [1, 2])

    def test_auroc_brier_ece(self):
        self.assertEqual(auroc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]), 1.0)
        self.assertEqual(auroc([1, 0], [0.5, 0.5]), 0.5)
        self.assertIsNone(auroc([1, 1], [0.1, 0.2]))
        self.assertAlmostEqual(brier([1, 0], [1.0, 0.0]), 0.0)
        self.assertAlmostEqual(ece([1, 0, 1, 0], [0.9, 0.1, 0.9, 0.1]), 0.1)


if __name__ == "__main__":
    unittest.main()
