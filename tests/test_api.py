"""HTTP 层测试：采集发布决策、查询、复核与冲突（爬虫被替换为固定数据）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "/tmp/pydeps")
sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient

import app.main as main_module
from app.storage import reset_storage

LIST_URL = "http://example.test/list/index.html"
DETAIL_URL = "http://example.test/detail/s003.html"
LIMITS = [
    {"name": "砷", "unit": "mg/L", "value": 0.01},
    {"name": "铅", "unit": "mg/L", "value": 0.01},
]


def base_result():
    return {
        "list_url": LIST_URL,
        "list_pages_visited": [LIST_URL],
        "detail_pages_visited": [DETAIL_URL],
        "samples": [
            {
                "sample_code": "S003",
                "sampling_point": "青石镇供水站",
                "conclusion": "待核实",
                "detail_url": DETAIL_URL,
                "indicators": [
                    {
                        "name": "砷",
                        "unit": "mg/L",
                        "raw_value": "未检出",
                        "converted_value": None,
                        "limit_value": 0.01,
                        "limit_unit": "mg/L",
                        "conclusion": "待核实",
                        "note": "未检出但未给出检出限，无法核实",
                        "source_url": DETAIL_URL,
                    }
                ],
            }
        ],
        "failed_urls": [],
        "incomplete": False,
        "counts": {"samples_total": 1},
    }


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WQ_DB_PATH"] = os.path.join(self.tmp.name, "api.db")
        reset_storage()
        self.client = TestClient(main_module.app)
        self._original_collect = main_module.collect
        self.result = base_result()

    def tearDown(self):
        main_module.collect = self._original_collect
        reset_storage()
        os.environ.pop("WQ_DB_PATH", None)
        self.tmp.cleanup()

    def _stub_collect(self, result):
        async def fake_collect(_req):
            return result

        main_module.collect = fake_collect

    def _collect(self):
        return self.client.post(
            "/api/collect", json={"list_url": LIST_URL, "limits": LIMITS}
        )

    def _review(self, revision, value=0.005, unit="mg/L"):
        return self.client.post(
            "/api/reviews",
            json={
                "list_url": LIST_URL,
                "reviews": [
                    {
                        "detail_url": DETAIL_URL,
                        "sample_code": "S003",
                        "indicator": "砷",
                        "revision": revision,
                        "approved_value": value,
                        "unit": unit,
                        "reason": "复测",
                    }
                ],
            },
        )

    def test_collect_publishes_complete_result(self):
        self._stub_collect(self.result)
        body = self._collect().json()
        self.assertTrue(body["published"])
        self.assertIsNotNone(body["run_id"])
        stored = self.client.get("/api/results", params={"list_url": LIST_URL})
        self.assertEqual(stored.status_code, 200)
        ind = stored.json()["samples"][0]["indicators"][0]
        self.assertEqual(ind["raw_value"], "未检出")
        self.assertEqual(ind["revision"], 0)

    def test_incomplete_collect_not_published(self):
        self._stub_collect(self.result)
        self._collect()
        broken = base_result()
        broken["failed_urls"] = ["http://example.test/detail/missing.html"]
        broken["incomplete"] = True
        self._stub_collect(broken)
        body = self._collect().json()
        self.assertFalse(body["published"])
        self.assertIn("不完整", body["publish_note"])
        stored = self.client.get("/api/results", params={"list_url": LIST_URL}).json()
        self.assertEqual(len(stored["samples"]), 1)
        self.assertFalse(stored["incomplete"])

    def test_duplicate_keys_not_published(self):
        self._stub_collect(self.result)
        self._collect()
        duplicate = base_result()
        duplicate["samples"][0]["indicators"].append(
            dict(duplicate["samples"][0]["indicators"][0])
        )
        self._stub_collect(duplicate)
        body = self._collect().json()
        self.assertFalse(body["published"])
        self.assertTrue(body["duplicate_keys"])
        stored = self.client.get("/api/results", params={"list_url": LIST_URL}).json()
        self.assertEqual(len(stored["samples"][0]["indicators"]), 1)

    def test_review_flow_and_conflict(self):
        self._stub_collect(self.result)
        self._collect()
        body = self._review(0).json()
        applied = body["applied"][0]
        self.assertEqual(applied["revision"], 1)
        self.assertEqual(applied["sample"]["conclusion"], "未超限")

        conflict = self._review(0)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.json()["detail"]["conflicts"][0]["current_revision"], 1
        )

        stored = self.client.get(
            "/api/results", params={"list_url": LIST_URL}
        ).json()
        ind = stored["samples"][0]["indicators"][0]
        self.assertEqual(ind["raw_value"], "未检出")
        self.assertEqual(ind["review"]["approved_value"], 0.005)

        history = self.client.get(
            "/api/reviews", params={"list_url": LIST_URL}
        ).json()
        self.assertEqual(len(history["reviews"]), 1)

    def test_review_unknown_entry_404(self):
        response = self.client.post(
            "/api/reviews",
            json={
                "list_url": "http://example.test/never.html",
                "reviews": [
                    {
                        "detail_url": DETAIL_URL,
                        "sample_code": "S003",
                        "indicator": "砷",
                        "revision": 0,
                        "approved_value": 0.005,
                        "unit": "mg/L",
                        "reason": "复测",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_unit_rejected(self):
        self._stub_collect(self.result)
        self._collect()
        response = self._review(0, value=1, unit="g/L")
        self.assertEqual(response.status_code, 422)

    def test_missing_reason_rejected(self):
        self._stub_collect(self.result)
        self._collect()
        response = self.client.post(
            "/api/reviews",
            json={
                "list_url": LIST_URL,
                "reviews": [
                    {
                        "detail_url": DETAIL_URL,
                        "sample_code": "S003",
                        "indicator": "砷",
                        "revision": 0,
                        "approved_value": 0.005,
                        "unit": "mg/L",
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_openapi_documents_endpoints(self):
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/api/collect", schema["paths"])
        self.assertIn("/api/results", schema["paths"])
        self.assertIn("/api/reviews", schema["paths"])


class ExampleSiteTests(unittest.TestCase):
    def setUp(self):
        from example_site.main import app as site_app

        self.client = TestClient(site_app)
        self.client.post("/control/reset")

    def tearDown(self):
        self.client.post("/control/reset")

    def test_serves_static_list(self):
        response = self.client.get("/list/index.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("抽检记录", response.text)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.client.get("/detail/nope.html").status_code, 404)

    def test_failure_control(self):
        self.client.post(
            "/control/fail",
            json={"path": "/detail/s006.html", "status": 503},
        )
        self.assertEqual(self.client.get("/detail/s006.html").status_code, 503)
        self.client.post("/control/reset")
        self.assertEqual(self.client.get("/detail/s006.html").status_code, 200)

    def test_value_override(self):
        self.client.post(
            "/control/value",
            json={"sample_code": "S003", "indicator": "砷", "raw_value": "0.035"},
        )
        self.assertIn("0.035", self.client.get("/detail/s003.html").text)
        self.client.post("/control/reset")
        self.assertIn("未检出（检出限 0.02）", self.client.get("/detail/s003.html").text)

    def test_slow_control_does_not_change_content(self):
        self.client.post(
            "/control/slow",
            json={"path": "/detail/s005.html", "seconds": 0.01},
        )
        self.assertEqual(self.client.get("/detail/s005.html").status_code, 200)


if __name__ == "__main__":
    unittest.main()
