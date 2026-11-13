"""单位折算测试。"""
import sys
import unittest

sys.path.insert(0, "/tmp/pydeps")
sys.path.insert(0, "/workspace")

from app.units import UnitError, convert_value, normalize_unit, split_quantity


class UnitTests(unittest.TestCase):
    def test_mg_to_ug(self):
        # 0.01 mg/L == 10 μg/L
        self.assertAlmostEqual(convert_value(0.01, "mg/L", "μg/L"), 10.0)

    def test_ug_to_mg(self):
        self.assertAlmostEqual(convert_value(18, "μg/L", "mg/L"), 0.018)

    def test_same_unit(self):
        self.assertAlmostEqual(convert_value(0.5, "mg/L", "mg/L"), 0.5)

    def test_micro_variants(self):
        self.assertAlmostEqual(convert_value(100, "ug/L", "mg/L"), 0.1)
        self.assertAlmostEqual(convert_value(100, "µg/L", "mg/L"), 0.1)

    def test_unsupported(self):
        with self.assertRaises(UnitError):
            convert_value(1, "g/L", "mg/L")
        with self.assertRaises(UnitError):
            convert_value(1, "mg/L", "个/升")

    def test_normalize(self):
        self.assertEqual(normalize_unit(" Mg/L "), "mg/l")
        self.assertEqual(normalize_unit("µg/L"), "μg/l")

    def test_split_quantity(self):
        self.assertEqual(split_quantity("0.012 mg/L"), (0.012, "mg/L"))
        self.assertEqual(split_quantity("<0.005μg/L"), (0.005, "μg/L"))
        value, unit = split_quantity("未检出（检出限 0.02）")
        self.assertEqual(value, 0.02)
        self.assertIn("未检出", unit)
        with self.assertRaises(ValueError):
            split_quantity("未检出")


if __name__ == "__main__":
    unittest.main()
