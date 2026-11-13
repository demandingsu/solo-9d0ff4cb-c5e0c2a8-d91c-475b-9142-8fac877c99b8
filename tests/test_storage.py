"""SQLite 持久化与人工复核的行为测试（不依赖网络）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "/tmp/pydeps")
sys.path.insert(0, "/workspace")

from app.storage import (
    KeyConflict,
    NotFound,
    ReviewItem,
    RevisionConflict,
    Storage,
    canonical_url,
)

LIMITS = [{"name": "砷", "unit": "mg/L", "value": 0.01}]


def make_samples(raw="未检出", unit="mg/L", url="http://example.test/detail/s003.html"):
    try:
        converted = float(raw) if unit == "mg/L" else float(raw) / 1000
        conclusion = "超限" if converted > 0.01 else "未超限"
    except ValueError:
        converted, conclusion = None, "待核实"
    return [
        {
            "sample_code": "S003",
            "sampling_point": "青石镇供水站",
            "conclusion": conclusion,
            "detail_url": url,
            "indicators": [
                {
                    "name": "砷",
                    "unit": unit,
                    "raw_value": raw,
                    "converted_value": converted,
                    "limit_value": 0.01,
                    "limit_unit": "mg/L",
                    "conclusion": conclusion,
                    "note": "测试数据",
                    "source_url": url,
                }
            ],
        }
    ]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.db")
        self.storage = Storage(self.path)
        self.list_url = "http://example.test/list/index.html"

    def tearDown(self):
        self.tmp.cleanup()

    def _publish(self, samples=None):
        return self.storage.publish(
            list_url=self.list_url,
            limits=LIMITS,
            samples=make_samples() if samples is None else samples,
            failed_urls=[],
            list_pages=[self.list_url],
            detail_pages=["http://example.test/detail/s003.html"],
            counts={},
        )

    def _indicator(self):
        return self.storage.get_results(self.list_url)["samples"][0]["indicators"][0]

    def test_publish_and_query_raw_row(self):
        run_id = self._publish()
        result = self.storage.get_results(self.list_url)
        self.assertEqual(result["run_id"], run_id)
        sample = result["samples"][0]
        self.assertEqual(sample["sample_code"], "S003")
        self.assertEqual(sample["conclusion"], "待核实")
        ind = sample["indicators"][0]
        self.assertEqual(ind["raw_value"], "未检出")
        self.assertEqual(ind["revision"], 0)
        self.assertIsNone(ind["review"])

    def test_review_keeps_raw_and_recomputes_sample(self):
        self._publish()
        rev = self._indicator()["revision"]
        outcomes = self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    detail_url="http://example.test/detail/s003.html",
                    sample_code="S003",
                    indicator="砷",
                    revision=rev,
                    approved_value=0.005,
                    unit="mg/L",
                    reason="复测结果",
                    reviewer="复核员甲",
                )
            ],
        )
        self.assertEqual(outcomes[0].revision, rev + 1)
        self.assertEqual(outcomes[0].sample["conclusion"], "未超限")
        ind = self._indicator()
        self.assertEqual(ind["raw_value"], "未检出")  # 原值保留
        self.assertEqual(ind["review"]["approved_value"], 0.005)
        self.assertEqual(ind["review"]["reviewer"], "复核员甲")
        self.assertEqual(ind["conclusion"], "未超限")
        self.assertAlmostEqual(ind["effective_converted_value"], 0.005)

    def test_review_above_limit_recheck(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    0.02,
                    "mg/L",
                    "复测超标",
                )
            ],
        )
        sample = self.storage.get_results(self.list_url)["samples"][0]
        self.assertEqual(sample["conclusion"], "复检")
        self.assertEqual(sample["collected_conclusion"], "待核实")

    def test_ug_l_approved_value_is_converted(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    5,
                    "μg/L",
                    "以 μg/L 核准",
                )
            ],
        )
        ind = self._indicator()
        self.assertEqual(ind["review"]["approved_unit"], "μg/L")
        self.assertAlmostEqual(ind["effective_converted_value"], 0.005)
        self.assertEqual(ind["conclusion"], "未超限")

    def test_stale_revision_conflict_and_no_write(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    0.005,
                    "mg/L",
                    "第一次",
                )
            ],
        )
        with self.assertRaises(RevisionConflict) as ctx:
            self.storage.apply_reviews(
                self.list_url,
                [
                    ReviewItem(
                        "http://example.test/detail/s003.html",
                        "S003",
                        "砷",
                        rev,
                        0.001,
                        "mg/L",
                        "过期修订号",
                    )
                ],
            )
        self.assertEqual(ctx.exception.conflicts[0]["current_revision"], rev + 1)
        # 原复核值未被覆盖
        self.assertEqual(self._indicator()["review"]["approved_value"], 0.005)

    def test_batch_is_atomic_on_stale_revision(self):
        self._publish()
        rev = self._indicator()["revision"]
        with self.assertRaises(RevisionConflict):
            self.storage.apply_reviews(
                self.list_url,
                [
                    ReviewItem(
                        "http://example.test/detail/s003.html",
                        "S003",
                        "砷",
                        rev,
                        0.001,
                        "mg/L",
                        "有效",
                    ),
                    ReviewItem(
                        "http://example.test/detail/s003.html",
                        "S003",
                        "砷",
                        rev + 99,
                        0.002,
                        "mg/L",
                        "过期",
                    ),
                ],
            )
        self.assertEqual(self._indicator()["revision"], rev)
        self.assertIsNone(self._indicator()["review"])

    def test_unknown_locator_not_found(self):
        self._publish()
        with self.assertRaises(NotFound):
            self.storage.apply_reviews(
                self.list_url,
                [
                    ReviewItem(
                        "http://example.test/detail/s003.html",
                        "S404",
                        "砷",
                        0,
                        0.001,
                        "mg/L",
                        "不存在",
                    )
                ],
            )

    def test_republish_unchanged_carries_latest_review(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    0.005,
                    "mg/L",
                    "复测",
                )
            ],
        )
        self._publish()
        ind = self._indicator()
        self.assertEqual(ind["revision"], rev + 1)
        self.assertEqual(ind["review"]["approved_value"], 0.005)
        self.assertEqual(ind["conclusion"], "未超限")

    def test_republish_changed_raw_drops_review(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    0.005,
                    "mg/L",
                    "复测",
                )
            ],
        )
        self._publish(make_samples(raw="0.02"))
        ind = self._indicator()
        self.assertEqual(ind["raw_value"], "0.02")
        self.assertIsNone(ind["review"])
        self.assertEqual(ind["revision"], rev + 2)
        self.assertEqual(ind["collected_conclusion"], "超限")

    def test_vanished_then_reappeared_row_does_not_reuse_review(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    0.005,
                    "mg/L",
                    "复测",
                )
            ],
        )
        self._publish(samples=[])
        self.assertEqual(self.storage.get_results(self.list_url)["samples"], [])
        self._publish()
        ind = self._indicator()
        self.assertIsNone(ind["review"])
        self.assertGreater(ind["revision"], rev + 1)

    def test_duplicate_keys_reject_without_publishing(self):
        self._publish()
        before = self.storage.get_results(self.list_url)
        with self.assertRaises(KeyConflict) as ctx:
            self._publish(make_samples() + make_samples())
        self.assertTrue(ctx.exception.keys)
        after = self.storage.get_results(self.list_url)
        self.assertEqual(before["published_at"], after["published_at"])
        self.assertEqual(
            [s["sample_code"] for s in after["samples"]], ["S003"]
        )

    def test_duplicate_indicator_within_page_rejected(self):
        samples = make_samples()
        samples[0]["indicators"].append(dict(samples[0]["indicators"][0]))
        with self.assertRaises(KeyConflict):
            self._publish(samples)

    def test_review_history_kept_after_row_changed(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    0.005,
                    "mg/L",
                    "复测",
                    "复核员乙",
                )
            ],
        )
        self._publish(make_samples(raw="0.02"))
        history = self.storage.get_reviews(self.list_url)["reviews"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["raw_value"], "未检出")
        self.assertEqual(history[0]["reviewer"], "复核员乙")

    def test_new_storage_instance_keeps_data_and_reviews(self):
        self._publish()
        rev = self._indicator()["revision"]
        self.storage.apply_reviews(
            self.list_url,
            [
                ReviewItem(
                    "http://example.test/detail/s003.html",
                    "S003",
                    "砷",
                    rev,
                    0.005,
                    "mg/L",
                    "复测",
                )
            ],
        )
        reopened = Storage(self.path)  # 模拟进程重启
        result = reopened.get_results(self.list_url)
        ind = result["samples"][0]["indicators"][0]
        self.assertEqual(ind["raw_value"], "未检出")
        self.assertEqual(ind["review"]["approved_value"], 0.005)
        self.assertEqual(result["samples"][0]["conclusion"], "未超限")
        self.assertEqual(len(reopened.get_reviews(self.list_url)["reviews"]), 1)

    def test_unpublished_incomplete_run_keeps_previous_data(self):
        self._publish()
        # 调用方在采集不完整时不会 publish；这里验证已有数据不因重复键失败而受损
        with self.assertRaises(KeyConflict):
            self._publish(make_samples() + make_samples())
        result = self.storage.get_results(self.list_url)
        self.assertFalse(result["incomplete"])
        self.assertEqual(len(result["samples"]), 1)

    def test_canonical_url_and_unknown_entry(self):
        self.assertEqual(
            canonical_url("HTTP://Example.TEST/detail/s003.html#top"),
            "http://example.test/detail/s003.html",
        )
        with self.assertRaises(NotFound):
            self.storage.get_results("http://example.test/other.html")


if __name__ == "__main__":
    unittest.main()
