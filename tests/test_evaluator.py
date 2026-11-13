"""判定逻辑测试：超限 / 未超限 / 待核实，含单位折算与检出限规则。"""
import sys
import unittest

sys.path.insert(0, "/tmp/pydeps")
sys.path.insert(0, "/workspace")

from app.evaluator import (
    OVER,
    REVIEW,
    WITHIN,
    aggregate_samples,
    evaluate_indicator,
)
from app.parser import DetailRecord
from app.schemas import LimitSpec

LIMITS = [
    LimitSpec(name="砷", unit="mg/L", value=0.01),
    LimitSpec(name="铅", unit="mg/L", value=0.01),
    LimitSpec(name="浊度", unit="mg/L", value=1.0),
]
URL = "http://example.test/detail/a.html"


def rec(indicator, unit, raw, code="SX", point="测点"):
    return DetailRecord(
        sample_code=code, sampling_point=point,
        indicator=indicator, unit=unit, raw_value=raw,
    )


class EvaluatorTests(unittest.TestCase):
    def test_over_after_unit_conversion(self):
        # 18 μg/L = 0.018 mg/L > 0.01
        r = evaluate_indicator(rec("砷", "μg/L", "18"), LIMITS[0], URL)
        self.assertEqual(r.conclusion, OVER)
        self.assertAlmostEqual(r.converted_value, 0.018)

    def test_within_normal(self):
        r = evaluate_indicator(rec("浊度", "mg/L", "0.4"), LIMITS[2], URL)
        self.assertEqual(r.conclusion, WITHIN)

    def test_below_detection_limit_within(self):
        # <2.5 μg/L = <0.0025 mg/L，检出限不高于 0.01
        r = evaluate_indicator(rec("铅", "μg/L", "<2.5"), LIMITS[1], URL)
        self.assertEqual(r.conclusion, WITHIN)
        self.assertAlmostEqual(r.converted_value, 0.0025)

    def test_below_detection_limit_too_high(self):
        # <0.02 mg/L，检出限高于上限 0.01 → 待核实
        r = evaluate_indicator(rec("砷", "mg/L", "<0.02"), LIMITS[0], URL)
        self.assertEqual(r.conclusion, REVIEW)

    def test_not_detected_with_limit_within(self):
        r = evaluate_indicator(
            rec("铅", "μg/L", "未检出（检出限 2）"), LIMITS[1], URL
        )
        self.assertEqual(r.conclusion, WITHIN)
        self.assertAlmostEqual(r.converted_value, 0.002)

    def test_not_detected_limit_too_high_review(self):
        r = evaluate_indicator(
            rec("砷", "mg/L", "未检出（检出限 0.02）"), LIMITS[0], URL
        )
        self.assertEqual(r.conclusion, REVIEW)

    def test_not_detected_missing_limit_review(self):
        r = evaluate_indicator(rec("砷", "mg/L", "未检出"), LIMITS[0], URL)
        self.assertEqual(r.conclusion, REVIEW)
        self.assertIn("检出限", r.note)

    def test_unsupported_unit_review(self):
        r = evaluate_indicator(rec("砷", "g/L", "0.001"), LIMITS[0], URL)
        self.assertEqual(r.conclusion, REVIEW)

    def test_missing_value_review(self):
        r = evaluate_indicator(rec("砷", "mg/L", ""), LIMITS[0], URL)
        self.assertEqual(r.conclusion, REVIEW)

    def test_aggregate_recheck_wins(self):
        records = [
            rec("砷", "μg/L", "18", code="S1"),   # 超限
            rec("浊度", "mg/L", "0.4", code="S1"),  # 未超限
        ]
        samples = aggregate_samples(records, LIMITS, URL)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["conclusion"], "复检")
        self.assertEqual(samples[0]["sampling_point"], "测点")

    def test_aggregate_review_wins_over_within(self):
        records = [
            rec("砷", "mg/L", "未检出", code="S2"),  # 待核实
            rec("浊度", "mg/L", "0.4", code="S2"),   # 未超限
        ]
        samples = aggregate_samples(records, LIMITS, URL)
        self.assertEqual(samples[0]["conclusion"], "待核实")

    def test_aggregate_all_within(self):
        records = [
            rec("砷", "mg/L", "0.006", code="S3"),
            rec("浊度", "mg/L", "0.4", code="S3"),
        ]
        samples = aggregate_samples(records, LIMITS, URL)
        self.assertEqual(samples[0]["conclusion"], "未超限")

    def test_indicator_without_limit_is_review(self):
        records = [rec("硝酸盐", "mg/L", "8", code="S4")]
        samples = aggregate_samples(records, LIMITS, URL)
        self.assertEqual(samples[0]["conclusion"], "待核实")
        self.assertIn("未提供该指标的限值", samples[0]["indicators"][0]["note"])


if __name__ == "__main__":
    unittest.main()
