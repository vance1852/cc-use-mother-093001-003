import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.api import route
from creative_program_foundation.clock import FixedClock
from creative_program_foundation.review import ReviewService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.tempdir.name) / "api.sqlite3")
        clock = FixedClock(datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        self.review = ReviewService(self.database, clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="组委会")
        self.service.register_actor(request_id="admin", actor_id="bootstrap",
                                    new_actor_id="admin1", display_name="管理员",
                                    role="admin", organization_id="o1")
        for actor_id, name, role in (
            ("sec1", "秘书", "operator"), ("aud1", "纪检", "auditor"),
            ("rev1", "评委一", "reviewer"), ("rev2", "评委二", "reviewer"),
        ):
            self.service.register_actor(request_id=actor_id, actor_id="admin1",
                                        new_actor_id=actor_id, display_name=name,
                                        role=role, organization_id="o1")
        self.review.register_rule_version(
            request_id="rules", actor_id="sec1", rule_version_id="rv1",
            rules={"independent_reviewers": 1, "quorum": 1,
                   "default_track_load_cap": 5, "substitute_count": 1})
        self.review.register_track(request_id="track", actor_id="sec1",
                                   track_id="t1", name="赛道")
        self.review.register_reviewer(request_id="p1", actor_id="sec1",
                                      reviewer_id="rev1", expertise=["design"],
                                      track_ids=["t1"])
        self.review.register_reviewer(request_id="p2", actor_id="sec1",
                                      reviewer_id="rev2", expertise=[],
                                      track_ids=["t1"])
        self.review.register_work(
            request_id="work", actor_id="sec1", work_id="w1", anon_code="ANON-1",
            track_id="t1", organization_id="org-secret", title="秘密题名",
            material_uri="material://ANON-1", required_expertise=["design"],
            team=[{"person_key": "author1", "associations": []}])
        self.review.create_batch(request_id="batch", actor_id="sec1",
                                 batch_id="b1", rule_version_id="rv1")
        self.review.add_work_to_batch(request_id="add", actor_id="sec1",
                                      batch_id="b1", work_id="w1")

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def call(self, method, path, actor, body=None):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor}, self.review)

    def test_full_flow_through_http_routes(self):
        status, payload = self.call("POST", "/review/batches/b1/plan", "sec1",
                                    {"request_id": "plan"})
        self.assertEqual(201, status)
        self.assertEqual("b1", payload["resource_id"])
        status, payload = self.call("POST", "/review/batches/b1/publish", "sec1",
                                    {"request_id": "publish"})
        self.assertEqual(201, status)

        status, payload = self.call("GET", "/review/my-tasks", "rev1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("ANON-1", payload["items"][0]["anon_code"])
        self.assertNotIn("material_uri", payload["items"][0])
        # 评委视角的任何字段都不得泄露机构或真实题名。
        self.assertNotIn("org-secret", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("秘密题名", json.dumps(payload, ensure_ascii=False))

        task_id = payload["items"][0]["task_id"]
        status, payload = self.call("POST", f"/review/tasks/{task_id}/claim",
                                    "rev1", {"request_id": "claim"})
        self.assertEqual(201, status)
        status, payload = self.call("GET", f"/review/tasks/{task_id}/material",
                                    "rev1")
        self.assertEqual(200, status)
        self.assertEqual("material://ANON-1", payload["material_uri"])
        status, payload = self.call("POST", f"/review/tasks/{task_id}/scores",
                                    "rev1", {"request_id": "score",
                                             "score": {"total": 88}})
        self.assertEqual(201, status)

        status, payload = self.call("POST", "/review/batches/b1/works/w1/seal",
                                    "sec1", {"request_id": "seal"})
        self.assertEqual(201, status)

    def test_reviewer_cannot_read_other_task_material(self):
        self.call("POST", "/review/batches/b1/plan", "sec1", {"request_id": "plan"})
        self.call("POST", "/review/batches/b1/publish", "sec1",
                  {"request_id": "publish"})
        _, payload = self.call("GET", "/review/my-tasks", "rev1")
        task_id = payload["items"][0]["task_id"]
        status, payload = self.call("GET", f"/review/tasks/{task_id}/material",
                                    "rev2")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_auditor_report_route_is_complete(self):
        self.call("POST", "/review/batches/b1/plan", "sec1", {"request_id": "plan"})
        status, payload = self.call("GET", "/review/batches/b1/report", "aud1")
        self.assertEqual(200, status)
        self.assertEqual("rv1", payload["rule_version"]["rule_version_id"])
        work = payload["works"][0]
        self.assertTrue(work["snapshot"]["hash_match"])
        self.assertIn("exclusions", work)
        self.assertIn("substitute_order", work)

    def test_secretary_score_route_is_forbidden(self):
        self.call("POST", "/review/batches/b1/plan", "sec1", {"request_id": "plan"})
        self.call("POST", "/review/batches/b1/publish", "sec1",
                  {"request_id": "publish"})
        _, tasks = self.call("GET", "/review/my-tasks", "rev1")
        task_id = tasks["items"][0]["task_id"]
        status, payload = self.call("POST", f"/review/tasks/{task_id}/scores",
                                    "sec1", {"request_id": "x",
                                             "score": {"total": 50}})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_relation_route_validates_subject_mismatch(self):
        status, payload = self.call("POST", "/review/relations", "sec1", {
            "request_id": "bad", "relation_id": "bad", "reviewer_id": "rev1",
            "subject_kind": "organization", "target_key": "org-secret",
            "relation_kind": "collaboration", "detail": "",
        })
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_review_route_returns_404(self):
        status, payload = self.call("GET", "/review/unknown", "aud1")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
