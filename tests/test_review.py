import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)
from creative_program_foundation.review import ReviewService
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


ORG = "org-fest"
TRACK = "t1"


class ReviewTestBase(unittest.TestCase):
    rules = {"independent_reviewers": 2, "quorum": 2,
             "default_track_load_cap": 3, "substitute_count": 2}

    def setUp(self, file_path: str | None = None):
        self.database = Database(file_path or ":memory:")
        self.clock = FixedClock(datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.review = ReviewService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id=ORG, name="大赛组委会")
        self.service.register_actor(request_id="admin", actor_id="bootstrap",
                                    new_actor_id="admin1", display_name="管理员",
                                    role="admin", organization_id=ORG)
        for actor_id, name, role in (
            ("sec1", "秘书", "operator"),
            ("aud1", "纪检", "auditor"),
        ):
            self.service.register_actor(request_id=actor_id, actor_id="admin1",
                                        new_actor_id=actor_id, display_name=name,
                                        role=role, organization_id=ORG)
        self.review.register_rule_version(request_id="rules", actor_id="sec1",
                                          rule_version_id="rv1", rules=self.rules)
        self.review.register_track(request_id="track", actor_id="sec1", track_id=TRACK,
                                   name="主赛道")
        self._reviewer_seq = 0
        self._work_seq = 0

    def tearDown(self):
        self.database.close()

    def make_reviewer(self, expertise=None, actor_id=None):
        self._reviewer_seq += 1
        actor_id = actor_id or f"r{self._reviewer_seq}"
        self.service.register_actor(
            request_id=f"actor-{actor_id}", actor_id="admin1", new_actor_id=actor_id,
            display_name=f"评委{actor_id}", role="reviewer", organization_id=ORG)
        self.review.register_reviewer(
            request_id=f"profile-{actor_id}", actor_id="sec1", reviewer_id=actor_id,
            expertise=expertise or [], track_ids=[TRACK])
        return actor_id

    def make_work(self, *, work_id=None, organization_id="org-a",
                  team=None, required_expertise=None, anon=None):
        self._work_seq += 1
        work_id = work_id or f"w{self._work_seq}"
        self.review.register_work(
            request_id=f"work-{work_id}", actor_id="sec1", work_id=work_id,
            anon_code=anon or f"ANON-{work_id}", track_id=TRACK,
            organization_id=organization_id, title=f"题名{work_id}",
            material_uri=f"material://{work_id}",
            required_expertise=required_expertise or [],
            team=team or [{"person_key": f"person-{work_id}", "associations": []}])
        return work_id

    def plan(self, batch_id="b1", work_ids=None):
        self.review.create_batch(request_id=f"batch-{batch_id}", actor_id="sec1",
                                 batch_id=batch_id, rule_version_id="rv1")
        for work_id in work_ids or []:
            self.review.add_work_to_batch(
                request_id=f"add-{batch_id}-{work_id}", actor_id="sec1",
                batch_id=batch_id, work_id=work_id)
        self.review.generate_plan(request_id=f"plan-{batch_id}", actor_id="sec1",
                                  batch_id=batch_id)
        self.review.publish_batch(request_id=f"publish-{batch_id}", actor_id="sec1",
                                  batch_id=batch_id)
        return self.review.get_batch_report("aud1", batch_id)

    def work_report(self, report, work_id):
        return next(work for work in report["works"] if work_id == work_id)

    def task_for(self, report, work_id, reviewer_id):
        work = self.work_report(report, work_id)
        return next(task for task in work["tasks"]
                    if task["reviewer_id"] == reviewer_id)


class PlanningTest(ReviewTestBase):
    def test_all_relation_kinds_are_excluded_with_reasons(self):
        r_adv = self.make_reviewer(actor_id="radv")
        r_emp = self.make_reviewer(actor_id="remp")
        r_col = self.make_reviewer(actor_id="rcol")
        r_kin = self.make_reviewer(actor_id="rkin")
        r_tea = self.make_reviewer(actor_id="rtea")
        r_asn = self.make_reviewer(actor_id="rasn")
        self.make_reviewer(actor_id="rok1")
        self.make_reviewer(actor_id="rok2")
        work_id = self.make_work(
            organization_id="org-consult",
            team=[{"person_key": "p1", "associations": ["asn-design"]}])
        self.review.declare_relation(request_id="rel1", actor_id="sec1",
                                     relation_id="rel1", reviewer_id=r_adv,
                                     subject_kind="organization", target_key="org-consult",
                                     relation_kind="advisory", detail="顾问")
        self.review.declare_relation(request_id="rel2", actor_id="sec1",
                                     relation_id="rel2", reviewer_id=r_emp,
                                     subject_kind="organization", target_key="org-consult",
                                     relation_kind="employment")
        self.review.declare_relation(request_id="rel3", actor_id="sec1",
                                     relation_id="rel3", reviewer_id=r_col,
                                     subject_kind="person", target_key="p1",
                                     relation_kind="collaboration")
        self.review.declare_relation(request_id="rel4", actor_id="sec1",
                                     relation_id="rel4", reviewer_id=r_kin,
                                     subject_kind="person", target_key="p1",
                                     relation_kind="kinship")
        self.review.declare_relation(request_id="rel5", actor_id="sec1",
                                     relation_id="rel5", reviewer_id=r_tea,
                                     subject_kind="person", target_key="p1",
                                     relation_kind="teacher_student")
        self.review.declare_relation(request_id="rel6", actor_id="sec1",
                                     relation_id="rel6", reviewer_id=r_asn,
                                     subject_kind="association", target_key="asn-design",
                                     relation_kind="association")
        report = self.plan(work_ids=[work_id])
        work = self.work_report(report, work_id)
        conflicts = {item["reviewer_id"]: item["detail"]
                     for item in work["exclusions"] if item["reason_code"] == "conflict"}
        self.assertEqual({code["code"] for code in conflicts[r_adv]},
                         {"conflict_advisory"})
        self.assertEqual({code["code"] for code in conflicts[r_emp]},
                         {"conflict_employment"})
        self.assertEqual({code["code"] for code in conflicts[r_col]},
                         {"conflict_collaboration"})
        self.assertEqual({code["code"] for code in conflicts[r_kin]},
                         {"conflict_kinship"})
        self.assertEqual({code["code"] for code in conflicts[r_tea]},
                         {"conflict_teacher_student"})
        self.assertEqual({code["code"] for code in conflicts[r_asn]},
                         {"conflict_association"})
        chosen = {item["reviewer_id"] for item in work["selection"]}
        self.assertEqual(chosen, {"rok1", "rok2"})

    def test_invalid_relation_subject_kind_rejected(self):
        rid = self.make_reviewer()
        with self.assertRaises(ValidationError):
            self.review.declare_relation(
                request_id="bad", actor_id="sec1", relation_id="bad",
                reviewer_id=rid, subject_kind="organization", target_key="org-a",
                relation_kind="collaboration")

    def test_reviewer_can_only_declare_own_relations(self):
        r1 = self.make_reviewer(actor_id="r-own")
        r2 = self.make_reviewer(actor_id="r-other")
        with self.assertRaises(PermissionDenied):
            self.review.declare_relation(
                request_id="notmine", actor_id=r2, relation_id="notmine",
                reviewer_id=r1, subject_kind="organization", target_key="org-a",
                relation_kind="advisory")
        receipt = self.review.declare_relation(
            request_id="mine", actor_id=r1, relation_id="mine", reviewer_id=r1,
            subject_kind="organization", target_key="org-a",
            relation_kind="advisory")
        self.assertFalse(receipt.replayed)

    def test_generation_fails_when_independent_count_unreachable(self):
        # cap=3、需要 2 名独立评委，但只有一人无冲突。
        self.make_reviewer(actor_id="free")
        conflicted = self.make_reviewer(actor_id="blocked")
        work_id = self.make_work()
        self.review.declare_relation(
            request_id="c1", actor_id="sec1", relation_id="c1",
            reviewer_id=conflicted, subject_kind="organization",
            target_key="org-a", relation_kind="employment")
        self.review.create_batch(request_id="b", actor_id="sec1", batch_id="b",
                                 rule_version_id="rv1")
        self.review.add_work_to_batch(request_id="bw", actor_id="sec1",
                                      batch_id="b", work_id=work_id)
        with self.assertRaises(ValidationError):
            self.review.generate_plan(request_id="p", actor_id="sec1", batch_id="b")
        # 整体失败：批次仍是草稿，没有遗留任务或方案。
        batch = self.database.connection.execute(
            "SELECT status FROM review_batches WHERE batch_id='b'").fetchone()
        self.assertEqual(batch["status"], "draft")
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM review_tasks").fetchone()["c"])
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM review_work_plans").fetchone()["c"])

    def test_load_cap_balances_and_is_enforced(self):
        # cap=1：每位评委在赛道内最多一件作品。
        self.review.register_rule_version(
            request_id="rules-cap", actor_id="sec1", rule_version_id="rv-cap",
            rules={"independent_reviewers": 1, "quorum": 1,
                   "default_track_load_cap": 1, "substitute_count": 0})
        for i in range(4):
            self.make_reviewer(actor_id=f"cap{i}")
        w1 = self.make_work(work_id="capw1")
        w2 = self.make_work(work_id="capw2")
        self.review.create_batch(request_id="bcap", actor_id="sec1", batch_id="bcap",
                                 rule_version_id="rv-cap")
        for work_id in (w1, w2):
            self.review.add_work_to_batch(
                request_id=f"add-{work_id}", actor_id="sec1", batch_id="bcap",
                work_id=work_id)
        self.review.generate_plan(request_id="pcap", actor_id="sec1", batch_id="bcap")
        report = self.review.get_batch_report("aud1", "bcap")
        chosen = {work["work_id"]: work["selection"][0]["reviewer_id"]
                  for work in report["works"]}
        self.assertNotEqual(chosen[w1], chosen[w2])

    def test_expertise_coverage_drives_selection(self):
        # design 专家排在 technology 专家之后创建，但必须因覆盖要求入选。
        self.make_reviewer(actor_id="gen1")
        self.make_reviewer(actor_id="gen2")
        self.make_reviewer(actor_id="designer", expertise=["design"])
        work_id = self.make_work(required_expertise=["design"])
        report = self.plan(work_ids=[work_id])
        chosen = {item["reviewer_id"] for item
                  in self.work_report(report, work_id)["selection"]}
        self.assertIn("designer", chosen)

    def test_coverage_gap_requires_waiver_and_is_recorded(self):
        self.make_reviewer(actor_id="g1")
        self.make_reviewer(actor_id="g2")
        work_id = self.make_work(required_expertise=["music"])
        self.review.create_batch(request_id="bw", actor_id="sec1", batch_id="bw",
                                 rule_version_id="rv1")
        self.review.add_work_to_batch(request_id="add", actor_id="sec1",
                                      batch_id="bw", work_id=work_id)
        with self.assertRaises(ValidationError):
            self.review.generate_plan(request_id="plan", actor_id="sec1", batch_id="bw")
        self.review.grant_exception(
            request_id="waiver", actor_id="sec1", batch_id="bw", work_id=work_id,
            exception_type="coverage_waiver", reason="跨赛道参考项，批准豁免")
        self.review.generate_plan(request_id="plan2", actor_id="sec1", batch_id="bw")
        self.review.publish_batch(request_id="pub", actor_id="sec1", batch_id="bw")
        report = self.review.get_batch_report("aud1", "bw")
        work = self.work_report(report, work_id)
        self.assertEqual(work["uncovered_expertise"], ["music"])
        self.assertEqual(work["exceptions"][0]["exception_type"], "coverage_waiver")

    def test_invalid_rule_versions_rejected(self):
        with self.assertRaises(ValidationError):
            self.review.register_rule_version(
                request_id="bad1", actor_id="sec1", rule_version_id="bad1",
                rules={"independent_reviewers": 2, "quorum": 3})
        with self.assertRaises(ValidationError):
            self.review.register_rule_version(
                request_id="bad2", actor_id="sec1", rule_version_id="bad2",
                rules={"independent_reviewers": 0})


class TaskFlowTest(ReviewTestBase):
    def _prepare_one_work(self):
        r1 = self.make_reviewer(actor_id="flow1")
        r2 = self.make_reviewer(actor_id="flow2")
        sub = self.make_reviewer(actor_id="flowsub")
        work_id = self.make_work()
        report = self.plan(work_ids=[work_id])
        return work_id, r1, r2, sub, report

    def test_reviewer_sees_only_anonymized_own_tasks(self):
        work_id, r1, r2, sub, report = self._prepare_one_work()
        view = self.review.list_my_tasks(r1)
        self.assertEqual(len(view["items"]), 1)
        item = view["items"][0]
        self.assertEqual(item["anon_code"], f"ANON-{work_id}")
        self.assertNotIn("material_uri", item)  # 未领取不可见材料
        serialized = json.dumps(view, ensure_ascii=False)
        for secret in ("org-a", f"题名{work_id}", f"person-{work_id}"):
            self.assertNotIn(secret, serialized)
        # 另一名评委看不到不属于自己的任务。
        other_task = self.task_for(report, work_id, r1)["task_id"]
        with self.assertRaises(NotFoundError):
            self.review.get_task_material(r2, other_task)
        # 纪检报告含完整身份信息，供复核而非评审使用。
        full = self.work_report(self.review.get_batch_report("aud1", "b1"), work_id)
        self.assertEqual(full["organization_id"], "org-a")

    def test_claim_and_score_lifecycle(self):
        work_id, r1, r2, sub, report = self._prepare_one_work()
        task = self.task_for(report, work_id, r1)["task_id"]
        receipt = self.review.claim_task(request_id="claim", actor_id=r1, task_id=task)
        self.assertFalse(receipt.replayed)
        self.review.claim_task(request_id="claim", actor_id=r1, task_id=task)  # 重放
        with self.assertRaises(ConflictError):
            self.review.claim_task(request_id="claim-again", actor_id=r1,
                                   task_id=task)
        material = self.review.get_task_material(r1, task)
        self.assertEqual(material["material_uri"], f"material://{work_id}")
        with self.assertRaises(ValidationError):
            self.review.submit_score(request_id="score-bad", actor_id=r1,
                                     task_id=task, score={"total": 120})
        self.review.submit_score(
            request_id="score", actor_id=r1, task_id=task,
            score={"total": 80.5, "dimensions": {"idea": 40}})
        with self.assertRaises(ConflictError):
            self.review.submit_score(
                request_id="score2", actor_id=r1, task_id=task,
                score={"total": 70})  # 评分不可修改

    def test_recuse_promotes_first_substitute(self):
        work_id, r1, r2, sub, report = self._prepare_one_work()
        task_r1 = self.task_for(report, work_id, r1)["task_id"]
        self.review.recuse_task(request_id="recuse", actor_id=r1,
                                task_id=task_r1, reason="临时发现利益关联")
        report = self.review.get_batch_report("aud1", "b1")
        sub_task = self.task_for(report, work_id, sub)
        self.assertEqual(sub_task["status"], "offered")
        self.assertEqual(sub_task["slot_type"], "substitute")
        events = [event["to_status"] for event in sub_task["events"]]
        self.assertEqual(events, ["offered"])

    def test_absence_ends_tasks_and_promotes_per_work(self):
        r1 = self.make_reviewer(actor_id="ab1")
        r2 = self.make_reviewer(actor_id="ab2")
        r3 = self.make_reviewer(actor_id="ab3")
        r4 = self.make_reviewer(actor_id="ab4")
        sub = self.make_reviewer(actor_id="absub")
        w1 = self.make_work(work_id="abw1")
        w2 = self.make_work(work_id="abw2")
        report = self.plan(work_ids=[w1, w2])
        # 找出 r1 担任正式评委的那件作品即可。
        self.review.mark_absent(request_id="absent", actor_id="sec1", batch_id="b1",
                                reviewer_id=r1, reason="突发疾病")
        report = self.review.get_batch_report("aud1", "b1")
        for work in report["works"]:
            r1_task = next((task for task in work["tasks"]
                            if task["reviewer_id"] == r1), None)
            if r1_task is not None:
                self.assertEqual(r1_task["status"], "absent")
        # r1 担任正式评委的作品上，第一候补按位置自动递补为 offered。
        promoted = [
            task for work in report["works"] for task in work["tasks"]
            if task["slot_type"] == "substitute" and task["status"] == "offered"
        ]
        self.assertEqual(len(promoted), 1)
        self.assertEqual([event["to_status"] for event in promoted[0]["events"]],
                         ["offered"])

    def test_invalidated_score_is_preserved_but_excluded_from_quorum(self):
        work_id, r1, r2, sub, report = self._prepare_one_work()
        task_r1 = self.task_for(report, work_id, r1)["task_id"]
        task_r2 = self.task_for(report, work_id, r2)["task_id"]
        self.review.claim_task(request_id="c1", actor_id=r1, task_id=task_r1)
        self.review.submit_score(request_id="s1", actor_id=r1, task_id=task_r1,
                                 score={"total": 90})
        self.review.claim_task(request_id="c2", actor_id=r2, task_id=task_r2)
        self.review.submit_score(request_id="s2", actor_id=r2, task_id=task_r2,
                                 score={"total": 60})
        # 两份有效评分时可以封存；作废后只剩一份，不能封存。
        self.review.invalidate_score(request_id="inv", actor_id="sec1",
                                     task_id=task_r2, reason="评分表作废")
        report = self.review.get_batch_report("aud1", "b1")
        invalidated = self.task_for(report, work_id, r2)
        self.assertEqual(invalidated["score"]["total"], 60)
        self.assertFalse(invalidated["score"]["valid_for_quorum"])
        with self.assertRaises(ConflictError):
            self.review.seal_work(request_id="seal", actor_id="sec1",
                                  batch_id="b1", work_id=work_id)

    def test_emergency_assignment_cannot_break_conflict_line(self):
        # 五名合格评委中选出两名正式、两名候补，留下一名从未入列的评委；
        # 另有一名因顾问关系被排除的评委。
        conflicted = self.make_reviewer(actor_id="em-blocked")
        em1 = self.make_reviewer(actor_id="em1")
        em2 = self.make_reviewer(actor_id="em2")
        em3 = self.make_reviewer(actor_id="em3")
        em4 = self.make_reviewer(actor_id="em4")
        em5 = self.make_reviewer(actor_id="em5")
        work_id = self.make_work()
        self.review.declare_relation(
            request_id="emrel", actor_id="sec1", relation_id="emrel",
            reviewer_id=conflicted, subject_kind="organization",
            target_key="org-a", relation_kind="advisory")
        report = self.plan(work_ids=[work_id])
        work = self.work_report(report, work_id)
        self.assertEqual(
            [item["reviewer_id"] for item in work["substitute_order"]],
            ["em3", "em4"])
        self.assertNotIn("em5", [task["reviewer_id"] for task in work["tasks"]])

        def task_of(reviewer_id):
            return self.task_for(self.review.get_batch_report("aud1", "b1"),
                                 work_id, reviewer_id)["task_id"]

        # 正式与候补依次退出，直到正式有效席位只剩一人。
        self.review.recuse_task(request_id="r1", actor_id=em1,
                                task_id=task_of(em1), reason="退出")
        self.review.recuse_task(request_id="r3", actor_id="sec1",
                                task_id=task_of(em3), reason="候补递补后退出")
        self.review.recuse_task(request_id="r4", actor_id="sec1",
                                task_id=task_of(em4), reason="候补耗尽")
        # 此时紧急替补仍不得突破利益冲突红线。
        with self.assertRaises(PermissionDenied):
            self.review.emergency_assign(
                request_id="embad", actor_id="sec1", batch_id="b1",
                work_id=work_id, reviewer_id=conflicted, reason="试图突破回避")
        # 没有任务记录的无冲突评委可以例外补位。
        receipt = self.review.emergency_assign(
            request_id="emok", actor_id="sec1", batch_id="b1", work_id=work_id,
            reviewer_id=em5, reason="候补耗尽后的例外补位")
        self.assertFalse(receipt.replayed)

    def test_sealed_work_is_immutable(self):
        work_id, r1, r2, sub, report = self._prepare_one_work()
        task_r1 = self.task_for(report, work_id, r1)["task_id"]
        task_r2 = self.task_for(report, work_id, r2)["task_id"]
        self.review.claim_task(request_id="c1", actor_id=r1, task_id=task_r1)
        self.review.submit_score(request_id="s1", actor_id=r1, task_id=task_r1,
                                 score={"total": 90})
        self.review.claim_task(request_id="c2", actor_id=r2, task_id=task_r2)
        self.review.submit_score(request_id="s2", actor_id=r2, task_id=task_r2,
                                 score={"total": 80})
        self.review.seal_work(request_id="seal", actor_id="sec1", batch_id="b1",
                              work_id=work_id)
        with self.assertRaises(ConflictError):
            self.review.recuse_task(request_id="late", actor_id=r1,
                                    task_id=task_r1, reason="封存后回避")
        with self.assertRaises(ConflictError):
            self.review.invalidate_score(request_id="lateinv", actor_id="sec1",
                                         task_id=task_r2, reason="封存后作废")
        with self.assertRaises(ConflictError):
            self.review.seal_work(request_id="seal-again", actor_id="sec1",
                                  batch_id="b1", work_id=work_id)
        # 同一 request_id 重放则安全返回原回执。
        replay = self.review.seal_work(request_id="seal", actor_id="sec1",
                                       batch_id="b1", work_id=work_id)
        self.assertTrue(replay.replayed)

    def test_quorum_blocks_seal(self):
        work_id, r1, r2, sub, report = self._prepare_one_work()
        task_r1 = self.task_for(report, work_id, r1)["task_id"]
        self.review.claim_task(request_id="c1", actor_id=r1, task_id=task_r1)
        self.review.submit_score(request_id="s1", actor_id=r1, task_id=task_r1,
                                 score={"total": 90})
        with self.assertRaises(ConflictError):
            self.review.seal_work(request_id="seal", actor_id="sec1",
                                  batch_id="b1", work_id=work_id)


class PermissionTest(ReviewTestBase):
    def test_secretary_cannot_submit_scores(self):
        r1 = self.make_reviewer(actor_id="p1")
        r2 = self.make_reviewer(actor_id="p2")
        work_id = self.make_work()
        report = self.plan(work_ids=[work_id])
        task = self.task_for(report, work_id, r1)["task_id"]
        with self.assertRaises(PermissionDenied):
            self.review.submit_score(request_id="x", actor_id="sec1", task_id=task,
                                     score={"total": 50})

    def test_auditor_is_read_only(self):
        with self.assertRaises(PermissionDenied):
            self.review.register_track(request_id="x", actor_id="aud1",
                                       track_id="tnope", name="x")
        with self.assertRaises(PermissionDenied):
            self.review.create_batch(request_id="x", actor_id="aud1",
                                     batch_id="bnope", rule_version_id="rv1")

    def test_reviewer_cannot_publish_or_seal(self):
        r1 = self.make_reviewer(actor_id="q1")
        self.make_reviewer(actor_id="q2")
        work_id = self.make_work()
        self.review.create_batch(request_id="b", actor_id="sec1", batch_id="b",
                                 rule_version_id="rv1")
        self.review.add_work_to_batch(request_id="a", actor_id="sec1",
                                      batch_id="b", work_id=work_id)
        with self.assertRaises(PermissionDenied):
            self.review.generate_plan(request_id="g", actor_id=r1, batch_id="b")


class PersistenceTest(ReviewTestBase):
    def test_restart_keeps_claimed_tasks_and_waitlist(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "persist.sqlite3")
            self.setUp(file_path=db_path)
            r1 = self.make_reviewer(actor_id="pt1")
            r2 = self.make_reviewer(actor_id="pt2")
            sub = self.make_reviewer(actor_id="ptsub")
            work_id = self.make_work()
            report = self.plan(work_ids=[work_id])
            task = self.task_for(report, work_id, r1)["task_id"]
            self.review.claim_task(request_id="claim", actor_id=r1, task_id=task)

            self.database.close()
            database = Database(db_path)
            service = DomainService(database, self.clock)
            review = ReviewService(database, self.clock)
            view = review.list_my_tasks(r1)
            self.assertEqual(view["items"][0]["status"], "claimed")
            report2 = review.get_batch_report("aud1", "b1")
            sub_task = self.task_for(report2, work_id, sub)
            self.assertEqual(sub_task["status"], "waitlist")
            # 重放旧 request_id 仍返回同一回执。
            replay = review.claim_task(request_id="claim", actor_id=r1, task_id=task)
            self.assertTrue(replay.replayed)
            valid, _ = service.verify_audit()
            self.assertTrue(valid)
            database.close()


if __name__ == "__main__":
    unittest.main()
