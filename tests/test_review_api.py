import unittest
from datetime import datetime, timezone

from creative_program_foundation.api import route
from creative_program_foundation.clock import FixedClock
from creative_program_foundation.review_service import BlindReviewService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


def call(service, method, path, body=None, actor=""):
    return route(service, method, path, body, {"X-Actor-Id": actor})


class ReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = DomainService(self.database, clock)
        self.base.register_organization(request_id="r1", actor_id="bootstrap",
                                        organization_id="o1", name="组委会")
        self.base.register_actor(request_id="r2", actor_id="bootstrap", new_actor_id="admin1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="r3", actor_id="admin1", new_actor_id="sec1",
                                 display_name="秘书", role="operator", organization_id="o1")
        self.base.register_actor(request_id="r4", actor_id="admin1", new_actor_id="jw1",
                                 display_name="纪检", role="auditor", organization_id="o1")
        for reviewer_id in ["rv1", "rv2", "rv3"]:
            self.base.register_actor(request_id=f"ra-{reviewer_id}", actor_id="admin1",
                                     new_actor_id=reviewer_id, display_name=reviewer_id,
                                     role="reviewer", organization_id="o1")
        call(self.service, "POST", "/review/disciplines",
             {"request_id": "d1", "discipline_id": "design", "name": "设计"}, "sec1")

    def tearDown(self):
        self.database.close()

    def _register_reviewer(self, reviewer_id):
        status, _ = call(self.service, "POST", "/review/reviewers",
                         {"request_id": f"rr-{reviewer_id}", "reviewer_id": reviewer_id,
                          "display_name": reviewer_id, "disciplines": ["design"]}, "sec1")
        self.assertEqual(201, status)

    def _ready_batch(self):
        for reviewer_id in ["rv1", "rv2", "rv3"]:
            self._register_reviewer(reviewer_id)
        body = {"request_id": "rule", "rule_version": "v1"}
        status, payload = call(self.service, "POST", "/review/rule-versions", body, "sec1")
        self.assertEqual(201, status)
        status, _ = call(self.service, "POST", "/review/works",
                         {"request_id": "w1", "work_id": "w1", "title": "作品", "track_id": "tA",
                          "required_disciplines": ["design"], "required_reviewer_count": 3,
                          "material_ref": "m/w1.pdf"}, "sec1")
        self.assertEqual(201, status)
        status, _ = call(self.service, "POST", "/review/team-members",
                         {"request_id": "tm1", "work_id": "w1", "person_key": "p1",
                          "role_label": "作者"}, "sec1")
        self.assertEqual(201, status)
        status, _ = call(self.service, "POST", "/review/batches",
                         {"request_id": "b1", "batch_id": "b1", "rule_version": "v1"}, "sec1")
        self.assertEqual(201, status)
        status, payload = call(self.service, "POST", "/review/batches/publish",
                               {"request_id": "p1", "batch_id": "b1"}, "sec1")
        self.assertEqual(201, status)

    def test_auditor_only_audit_endpoint(self):
        status, payload = call(self.service, "GET", "/review/batches/b1/audit", actor="sec1")
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_reviewer_cannot_register_work(self):
        status, payload = call(self.service, "POST", "/review/works",
                               {"request_id": "x", "work_id": "w", "title": "t", "track_id": "t",
                                "required_disciplines": [], "required_reviewer_count": 1,
                                "material_ref": "m"}, "rv1")
        self.assertEqual(403, status)

    def test_full_flow_over_http_and_idempotent_claims(self):
        self._ready_batch()
        status, payload = call(self.service, "GET", "/review/my-tasks", actor="rv1")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        # 评委视图不得出现作者人员键。
        self.assertNotIn("p1", str(payload))
        task_id = payload["items"][0]["task_id"]
        # 未领取时材料不可见。
        self.assertIsNone(payload["items"][0]["material_ref"])
        status, first = call(self.service, "POST", "/review/tasks/claim",
                             {"request_id": "claim-1", "task_id": task_id}, "rv1")
        self.assertEqual(201, status)
        status, replay = call(self.service, "POST", "/review/tasks/claim",
                              {"request_id": "claim-1", "task_id": task_id}, "rv1")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])
        # 新 request_id 的重复领取必须冲突。
        status, payload = call(self.service, "POST", "/review/tasks/claim",
                               {"request_id": "claim-2", "task_id": task_id}, "rv1")
        self.assertEqual(409, status)
        status, _ = call(self.service, "POST", "/review/scores",
                         {"request_id": "score-1", "task_id": task_id, "score_value": 88}, "rv1")
        self.assertEqual(201, status)

        for reviewer_id in ["rv2", "rv3"]:
            status, payload = call(self.service, "GET", "/review/my-tasks", actor=reviewer_id)
            other_task = payload["items"][0]["task_id"]
            call(self.service, "POST", "/review/tasks/claim",
                 {"request_id": f"claim-{reviewer_id}", "task_id": other_task}, reviewer_id)
            status, _ = call(self.service, "POST", "/review/scores",
                             {"request_id": f"score-{reviewer_id}", "task_id": other_task,
                              "score_value": 70}, reviewer_id)
            self.assertEqual(201, status)

        status, payload = call(self.service, "POST", "/review/seal",
                               {"request_id": "seal", "batch_id": "b1", "work_id": "w1"}, "sec1")
        self.assertEqual(201, status)
        self.assertTrue(payload["sealed"])

        status, payload = call(self.service, "GET", "/review/batches/b1/audit", actor="jw1")
        self.assertEqual(200, status)
        self.assertTrue(payload["relationship_snapshot_hash_verified"])
        self.assertEqual("v1", payload["rule_version"])
        self.assertEqual(3, len(payload["scores"]))

    def test_reviewer_conflict_self_declaration_accepted(self):
        self._register_reviewer("rv1")
        status, payload = call(self.service, "POST", "/review/conflicts",
                               {"request_id": "c1", "reviewer_id": "rv1", "subject_type": "person",
                                "subject_key": "p9", "relation_type": "kinship"}, "rv1")
        self.assertEqual(201, status)
        # 评委不能替他人声明。
        status, payload = call(self.service, "POST", "/review/conflicts",
                               {"request_id": "c2", "reviewer_id": "rv2", "subject_type": "person",
                                "subject_key": "p9", "relation_type": "kinship"}, "rv1")
        self.assertEqual(403, status)

    def test_unknown_review_route_is_404(self):
        status, payload = call(self.service, "GET", "/review/nope", actor="sec1")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
