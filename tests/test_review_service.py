import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from creative_program_foundation.clock import FixedClock
from creative_program_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from creative_program_foundation.review_service import BlindReviewService
from creative_program_foundation.review_rules import build_snapshot, default_rule_body, generate_plan
from creative_program_foundation.service import DomainService
from creative_program_foundation.storage import Database


class BlindReviewFixture(unittest.TestCase):
    """建立一个含 6 名评委、2 个专业、1 件作品的标准场景。"""

    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.service = BlindReviewService(self.database, clock)
        self.base.register_organization(request_id="r1", actor_id="bootstrap",
                                        organization_id="o1", name="组委会")
        self.base.register_actor(request_id="r2", actor_id="bootstrap", new_actor_id="admin1",
                                 display_name="管理员", role="admin", organization_id="o1")
        self.base.register_actor(request_id="r3", actor_id="admin1", new_actor_id="sec1",
                                 display_name="评审秘书", role="operator", organization_id="o1")
        self.base.register_actor(request_id="r4", actor_id="admin1", new_actor_id="jw1",
                                 display_name="纪检", role="auditor", organization_id="o1")
        self.reviewers = ["rv1", "rv2", "rv3", "rv4", "rv5", "rv6"]
        for index, reviewer_id in enumerate(self.reviewers):
            self.base.register_actor(request_id=f"ra-{reviewer_id}", actor_id="admin1",
                                     new_actor_id=reviewer_id, display_name=f"评委{index}",
                                     role="reviewer", organization_id="o1")
        self.service.register_discipline(request_id="d1", actor_id="sec1",
                                         discipline_id="design", name="设计")
        self.service.register_discipline(request_id="d2", actor_id="sec1",
                                         discipline_id="tech", name="技术")
        profiles = {"rv1": ["design"], "rv2": ["tech"], "rv3": ["design", "tech"],
                    "rv4": [], "rv5": [], "rv6": ["tech"]}
        for reviewer_id, disciplines in profiles.items():
            self.service.register_reviewer(request_id=f"rr-{reviewer_id}", actor_id="sec1",
                                           reviewer_id=reviewer_id, display_name=reviewer_id,
                                           disciplines=disciplines)

    def tearDown(self):
        self.database.close()

    def register_work_w1(self, reviewer_count=3, pool_size=2, track_load_cap=5):
        body = default_rule_body()
        body["substitute_pool_size"] = pool_size
        body["track_load_cap"] = track_load_cap
        self.service.create_rule_version(request_id="rule-v1", actor_id="sec1",
                                         rule_version="v1", body=body)
        self.service.register_work(request_id="w1", actor_id="sec1", work_id="w1", title="作品一",
                                   track_id="tA", required_disciplines=["design", "tech"],
                                   required_reviewer_count=reviewer_count, material_ref="m/w1.pdf")
        self.service.register_team_member(request_id="tm1", actor_id="sec1", work_id="w1",
                                          person_key="author-x", role_label="作者",
                                          organization_id="orgX")
        self.service.register_team_member(request_id="tm2", actor_id="sec1", work_id="w1",
                                          person_key="author-y", role_label="联合作者",
                                          organization_id="orgX")

    def publish_default_batch(self, batch_id="b1"):
        self.register_work_w1()
        self.service.declare_conflict(request_id="c1", actor_id="rv1", reviewer_id="rv1",
                                      subject_type="organization", subject_key="orgX",
                                      relation_type="employment", detail="顾问服务")
        self.service.declare_conflict(request_id="c2", actor_id="sec1", reviewer_id="rv2",
                                      subject_type="person", subject_key="author-y",
                                      relation_type="association")
        self.service.create_batch(request_id="b1", actor_id="sec1", batch_id=batch_id,
                                  rule_version="v1")
        self.service.publish_batch(request_id="p1", actor_id="sec1", batch_id=batch_id)

    def tasks_by_reviewer(self, reviewer_id):
        return {item["task_id"]: item for item in self.service.list_my_tasks(reviewer_id)}

    def score_everyone(self, expected_reviewers):
        tasks = {}
        for reviewer_id in expected_reviewers:
            task_id = next(iter(self.tasks_by_reviewer(reviewer_id)))
            self.service.claim_task(request_id=f"claim-{reviewer_id}", actor_id=reviewer_id,
                                    task_id=task_id)
            tasks[reviewer_id] = task_id
        for index, (reviewer_id, task_id) in enumerate(tasks.items()):
            self.service.submit_score(request_id=f"score-{reviewer_id}", actor_id=reviewer_id,
                                      task_id=task_id, score_value=70 + index)
        return tasks


class AllocationTest(BlindReviewFixture):
    def test_conflicted_reviewers_are_excluded_with_reasons(self):
        self.publish_default_batch()
        audit = self.service.get_batch_audit("jw1", "b1")
        excluded = {item["reviewer_id"]: item["reasons"]
                    for item in audit["allocation"]["works"]["w1"]["excluded"]}
        self.assertIn("rv1", excluded)
        self.assertTrue(any("任职服务" in reason for reason in excluded["rv1"]))
        self.assertIn("rv2", excluded)
        self.assertTrue(any("同属协会" in reason for reason in excluded["rv2"]))
        assigned = {a["reviewer_id"] for a in audit["allocation"]["works"]["w1"]["assignments"]}
        self.assertEqual({"rv3", "rv4", "rv6"}, assigned)

    def test_plan_is_deterministic_on_same_snapshot(self):
        self.register_work_w1()
        snapshot = self.service._snapshot_from_db(self.database.connection)
        rules = default_rule_body()
        first = generate_plan(snapshot, rules)
        second = generate_plan(snapshot, rules)
        self.assertEqual(
            [(t.work_id, t.reviewer_id, t.slot_index) for t in first.tasks],
            [(t.work_id, t.reviewer_id, t.slot_index) for t in second.tasks],
        )

    def test_infeasible_when_discipline_cannot_be_covered(self):
        # 让唯一可选的 design 评委 rv3 与作者冲突，则 design 无人可评。
        self.register_work_w1()
        self.service.declare_conflict(request_id="c1", actor_id="sec1", reviewer_id="rv1",
                                      subject_type="organization", subject_key="orgX",
                                      relation_type="employment")
        self.service.declare_conflict(request_id="c3", actor_id="sec1", reviewer_id="rv3",
                                      subject_type="person", subject_key="author-x",
                                      relation_type="kinship")
        self.service.create_batch(request_id="b1", actor_id="sec1", batch_id="b1",
                                  rule_version="v1")
        with self.assertRaises(ConflictError):
            self.service.publish_batch(request_id="p1", actor_id="sec1", batch_id="b1")
        audit = self.service.get_batch_audit("jw1", "b1")
        self.assertTrue(audit["allocation"]["infeasible"])

    def test_track_load_cap_blocks_overflow(self):
        self.register_work_w1(track_load_cap=1)
        # 第二件作品同样锚定 design/tech：w1 已占满 rv1/rv2 等专业评委的赛道额度。
        self.service.register_work(request_id="w2", actor_id="sec1", work_id="w2", title="作品二",
                                   track_id="tA", required_disciplines=["design", "tech"],
                                   required_reviewer_count=3, material_ref="m/w2.pdf")
        self.service.register_team_member(request_id="tm3", actor_id="sec1", work_id="w2",
                                          person_key="author-z", role_label="作者")
        # w1 已占用 rv3 的赛道额度；rv1 是另一名 design 评委，让其也与 w2 冲突，设计槽无人可补。
        self.service.declare_conflict(request_id="c-w2-1", actor_id="sec1", reviewer_id="rv1",
                                      subject_type="person", subject_key="author-z",
                                      relation_type="mentor")
        self.service.declare_conflict(request_id="c-w2-3", actor_id="sec1", reviewer_id="rv3",
                                      subject_type="person", subject_key="author-z",
                                      relation_type="kinship")
        self.service.create_batch(request_id="b1", actor_id="sec1", batch_id="b1",
                                  rule_version="v1")
        with self.assertRaises(ConflictError):
            self.service.publish_batch(request_id="p1", actor_id="sec1", batch_id="b1")

    def test_snapshot_freezes_declarations_after_batch_created(self):
        self.publish_default_batch()
        before = self.service.get_batch_audit("jw1", "b1")["relationship_snapshot_hash"]
        # 批次之后新增的回避关系不得改变已冻结批次。
        self.service.declare_conflict(request_id="c9", actor_id="sec1", reviewer_id="rv4",
                                      subject_type="person", subject_key="author-x",
                                      relation_type="kinship")
        after = self.service.get_batch_audit("jw1", "b1")["relationship_snapshot_hash"]
        self.assertEqual(before, after)

    def test_same_organization_auto_conflict(self):
        # 所有评委默认属于 o1；作品团队机构写成 o1 时，未声明冲突的评委也应被自动排除。
        self.register_work_w1()
        self.service.register_team_member(request_id="tm3", actor_id="sec1", work_id="w1",
                                          person_key="author-z", role_label="合作机构成员",
                                          organization_id="o1")
        self.service.create_batch(request_id="b1", actor_id="sec1", batch_id="b1",
                                  rule_version="v1")
        # 六名评委全部因同属机构被排除，方案必然不可行。
        with self.assertRaises(ConflictError):
            self.service.publish_batch(request_id="p1", actor_id="sec1", batch_id="b1")
        audit = self.service.get_batch_audit("jw1", "b1")
        reasons = audit["allocation"]["works"]["w1"]["excluded"]
        self.assertTrue(any("同属机构自动回避" in r
                            for item in reasons for r in item["reasons"]))

    def test_discipline_coverage_off_with_enough_general_reviewers(self):
        body = default_rule_body()
        body["require_discipline_coverage"] = False
        body["min_independent_reviewers"] = 2
        self.service.create_rule_version(request_id="rule-off2", actor_id="sec1",
                                         rule_version="voff2", body=body)
        # 在基础库内无专业评委只有 rv4/rv5，直接要求 2 名评委即可验证不强制专业。
        self.service.register_work(request_id="w1c", actor_id="sec1", work_id="w1c", title="作品丙",
                                   track_id="tA", required_disciplines=["design", "tech"],
                                   required_reviewer_count=2, material_ref="m/w1c.pdf")
        self.service.register_team_member(request_id="tm1c", actor_id="sec1", work_id="w1c",
                                          person_key="p1", role_label="作者")
        for index, reviewer_id in enumerate(["rv1", "rv2", "rv3", "rv6"]):
            self.service.declare_conflict(request_id=f"c-off2-{index}", actor_id="sec1",
                                          reviewer_id=reviewer_id, subject_type="person",
                                          subject_key="p1", relation_type="kinship")
        self.service.create_batch(request_id="b3", actor_id="sec1", batch_id="b3",
                                  rule_version="voff2")
        self.service.publish_batch(request_id="p3", actor_id="sec1", batch_id="b3")
        audit = self.service.get_batch_audit("jw1", "b3")
        assignments = audit["allocation"]["works"]["w1c"]["assignments"]
        self.assertEqual({"rv4", "rv5"}, {a["reviewer_id"] for a in assignments})
        self.assertTrue(all(not a["discipline_id"] for a in assignments))

    def test_reviewer_count_below_rule_floor_rejected(self):
        body = default_rule_body()
        self.service.create_rule_version(request_id="rule-low", actor_id="sec1",
                                         rule_version="vlow", body=body)
        # 作品登记本身允许 1 人；在以规则 vlow（默认下限 3）建批次时才拒绝。
        self.service.register_work(request_id="wbad", actor_id="sec1", work_id="wbad",
                                   title="不足人数", track_id="tA",
                                   required_disciplines=[], required_reviewer_count=1,
                                   material_ref="m/bad.pdf")
        with self.assertRaises(ValidationError):
            self.service.create_batch(request_id="b-bad", actor_id="sec1", batch_id="bbad",
                                      rule_version="vlow")


class TaskLifecycleTest(BlindReviewFixture):
    def test_reviewer_only_sees_own_anonymized_tasks(self):
        self.publish_default_batch()
        mine = self.service.list_my_tasks("rv3")
        self.assertEqual(1, len(mine))
        self.assertEqual("ANON-w1", mine[0]["anonymized_code"])
        self.assertIsNone(mine[0]["material_ref"])
        rendered = repr(mine)
        self.assertNotIn("author-x", rendered)
        self.assertNotIn("orgX", rendered)
        foreign = self.tasks_by_reviewer("rv4")
        foreign_task = next(iter(foreign))
        with self.assertRaises(NotFoundError):
            self.service.get_my_task("rv3", foreign_task)
        with self.assertRaises(PermissionDenied):
            self.service.list_my_tasks("sec1")

    def test_material_opens_after_claim_and_closes_after_recusal(self):
        self.publish_default_batch()
        task_id = next(iter(self.tasks_by_reviewer("rv4")))
        self.assertIsNone(self.service.get_my_task("rv4", task_id)["material_ref"])
        self.service.claim_task(request_id="claim", actor_id="rv4", task_id=task_id)
        self.assertEqual("m/w1.pdf", self.service.get_my_task("rv4", task_id)["material_ref"])
        self.service.recuse_task(request_id="recuse", actor_id="rv4", task_id=task_id,
                                 reason="临时发现亲属关系")
        self.assertIsNone(self.service.get_my_task("rv4", task_id)["material_ref"])

    def test_recusal_promotes_substitute_in_rank_order(self):
        # pool=3 时 rv4 是自由槽，候补按专业覆盖优先：rv2 有 tech 但被回避，rv1 有 design 被回避，
        # 所以无冲突候补 rv5(无专业) 排首位。
        self.publish_default_batch()
        task_id = next(iter(self.tasks_by_reviewer("rv4")))
        self.service.claim_task(request_id="claim", actor_id="rv4", task_id=task_id)
        result = self.service.recuse_task(request_id="recuse", actor_id="rv4",
                                          task_id=task_id, reason="突发冲突")
        new_task_id = result["response"]["replacement_task_id"]
        self.assertIsNotNone(new_task_id)
        promoted = self.service.get_my_task("rv5", new_task_id)
        self.assertEqual("assigned", promoted["status"])
        audit = self.service.get_batch_audit("jw1", "b1")
        queue = [(s["reviewer_id"], s["status"]) for s in audit["substitutes"]]
        self.assertIn(("rv5", "promoted"), queue)

    def test_discipline_anchor_recusal_prefers_disciplined_substitute(self):
        # 作品只锚定 design 与 tech，候补池放大，tech 评委 rv2 无冲突时可承接 tech 锚点。
        self.register_work_w1(pool_size=4)
        self.service.declare_conflict(request_id="c1", actor_id="sec1", reviewer_id="rv1",
                                      subject_type="organization", subject_key="orgX",
                                      relation_type="employment")
        # rv2 不设置冲突，是 tech 评委；撤下 rv2 自己（tech 锚点之一）时应优先 tech 候补。
        self.service.create_batch(request_id="b1", actor_id="sec1", batch_id="b1",
                                  rule_version="v1")
        self.service.publish_batch(request_id="p1", actor_id="sec1", batch_id="b1")
        audit = self.service.get_batch_audit("jw1", "b1")
        assignments = {a["reviewer_id"]: a for a in audit["allocation"]["works"]["w1"]["assignments"]}
        tech_reviewer = "rv6" if "rv6" in assignments else "rv2"
        tech_task = next(iter(self.tasks_by_reviewer(tech_reviewer)))
        self.service.claim_task(request_id="claim-tech", actor_id=tech_reviewer,
                                task_id=tech_task)
        result = self.service.mark_task_absent(request_id="absent", actor_id="sec1",
                                               task_id=tech_task, reason="联系不上")
        new_task_id = result["response"]["replacement_task_id"]
        self.assertIsNotNone(new_task_id)
        new_owner = self.service.get_batch_audit("jw1", "b1")
        created = next(t for t in new_owner["tasks"] if t["task_id"] == new_task_id)
        self.assertEqual("tech", created["discipline_id"])
        self.assertIn(created["reviewer_id"], {"rv2", "rv3", "rv6"})

    def test_absence_void_and_seal_rules(self):
        self.publish_default_batch()
        tasks = self.score_everyone({"rv3": None, "rv4": None, "rv6": None})
        # 三份有效评分，封存前作废 rv4 的分：事实保留，候补 rv5 补位。
        void_result = self.service.void_score(request_id="void", actor_id="sec1",
                                              task_id=tasks["rv4"], reason="发现漏报利益关系")
        new_task_id = void_result["response"]["replacement_task_id"]
        audit = self.service.get_batch_audit("jw1", "b1")
        voided = [s for s in audit["scores"] if s["voided_at"]]
        self.assertEqual(1, len(voided))
        self.assertEqual("发现漏报利益关系", voided[0]["void_reason"])
        # 有效评分回到 2 份，封存必须失败。
        with self.assertRaises(ConflictError):
            self.service.seal_work(request_id="seal-fail", actor_id="sec1",
                                   batch_id="b1", work_id="w1")
        # 候补补评后达到法定人数，且 design/tech 仍覆盖（rv3=design 锚点、rv6=tech 锚点）。
        self.service.claim_task(request_id="claim-sub", actor_id="rv5", task_id=new_task_id)
        self.service.submit_score(request_id="score-sub", actor_id="rv5",
                                  task_id=new_task_id, score_value=92)
        self.service.seal_work(request_id="seal", actor_id="sec1", batch_id="b1", work_id="w1")
        # 封存后回避、作废、补评全部禁止，事实原样保留。
        with self.assertRaises(ConflictError):
            self.service.recuse_task(request_id="recuse-late", actor_id="rv3",
                                     task_id=tasks["rv3"], reason="x")
        with self.assertRaises(ConflictError):
            self.service.void_score(request_id="void-late", actor_id="sec1",
                                    task_id=tasks["rv6"], reason="x")

    def test_secretary_and_admin_cannot_touch_scores(self):
        self.publish_default_batch()
        task_id = next(iter(self.tasks_by_reviewer("rv3")))
        with self.assertRaises(PermissionDenied):
            self.service.claim_task(request_id="hack-claim", actor_id="sec1", task_id=task_id)
        with self.assertRaises(PermissionDenied):
            self.service.submit_score(request_id="hack-score", actor_id="sec1",
                                      task_id=task_id, score_value=100)
        with self.assertRaises(PermissionDenied):
            self.service.seal_work(request_id="hack-seal", actor_id="jw1",
                                   batch_id="b1", work_id="w1")

    def test_batch_close_requires_all_works_sealed(self):
        self.publish_default_batch()
        with self.assertRaises(ConflictError):
            self.service.close_batch(request_id="close-early", actor_id="sec1", batch_id="b1")
        tasks = self.score_everyone({"rv3": None, "rv4": None, "rv6": None})
        self.service.seal_work(request_id="seal", actor_id="sec1", batch_id="b1", work_id="w1")
        result = self.service.close_batch(request_id="close", actor_id="sec1", batch_id="b1")
        self.assertEqual("closed", result["response"]["status"])
        batches = {b["batch_id"]: b["status"] for b in self.service.list_batches("jw1")}
        self.assertEqual("closed", batches["b1"])
        # 关闭后任务操作不再允许。
        with self.assertRaises(ConflictError):
            self.service.claim_task(request_id="late-claim", actor_id="rv3",
                                    task_id=tasks["rv3"])

    def test_auditor_cannot_publish_or_mutate(self):
        self.register_work_w1()
        self.service.create_batch(request_id="b1", actor_id="sec1", batch_id="b1",
                                  rule_version="v1")
        with self.assertRaises(PermissionDenied):
            self.service.publish_batch(request_id="p-hack", actor_id="jw1", batch_id="b1")


class ConcurrencyAndPersistenceTest(BlindReviewFixture):
    def test_concurrent_claims_only_one_wins(self):
        self.publish_default_batch()
        task_id = next(iter(self.tasks_by_reviewer("rv3")))
        outcomes: list[str] = []

        def claim(index):
            try:
                self.service.claim_task(request_id=f"parallel-{index}", actor_id="rv3",
                                        task_id=task_id)
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(7, outcomes.count("conflict"))

    def test_concurrent_scores_only_one_wins(self):
        self.publish_default_batch()
        task_id = next(iter(self.tasks_by_reviewer("rv3")))
        self.service.claim_task(request_id="claim", actor_id="rv3", task_id=task_id)
        outcomes: list[str] = []

        def score(index):
            try:
                self.service.submit_score(request_id=f"score-{index}", actor_id="rv3",
                                          task_id=task_id, score_value=80 + index)
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=score, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("ok"))
        audit = self.service.get_batch_audit("jw1", "b1")
        self.assertEqual(1, len(audit["scores"]))

    def test_restart_keeps_accepted_tasks_and_substitute_queue(self):
        """进程重启（重新打开同一 SQLite 文件）不得打乱已接受任务或候补队列。"""

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "restart.sqlite3"
            database = Database(database_path)
            clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
            base = DomainService(database, clock)
            service = BlindReviewService(database, clock)
            base.register_organization(request_id="r1", actor_id="bootstrap",
                                       organization_id="o1", name="组委会")
            base.register_actor(request_id="r2", actor_id="bootstrap", new_actor_id="admin1",
                                display_name="管理员", role="admin", organization_id="o1")
            base.register_actor(request_id="r3", actor_id="admin1", new_actor_id="sec1",
                                display_name="秘书", role="operator", organization_id="o1")
            base.register_actor(request_id="r4", actor_id="admin1", new_actor_id="jw1",
                                display_name="纪检", role="auditor", organization_id="o1")
            for reviewer_id in ["rv3", "rv4", "rv5"]:
                base.register_actor(request_id=f"ra-{reviewer_id}", actor_id="admin1",
                                    new_actor_id=reviewer_id, display_name=reviewer_id,
                                    role="reviewer", organization_id="o1")
            service.register_discipline(request_id="d1", actor_id="sec1",
                                        discipline_id="design", name="设计")
            for reviewer_id in ["rv3", "rv4", "rv5"]:
                service.register_reviewer(request_id=f"rr-{reviewer_id}", actor_id="sec1",
                                          reviewer_id=reviewer_id, display_name=reviewer_id,
                                          disciplines=["design"] if reviewer_id == "rv3" else [])
            rule_body = default_rule_body()
            rule_body["min_independent_reviewers"] = 1
            rule_body["substitute_pool_size"] = 2
            service.create_rule_version(request_id="rule", actor_id="sec1", rule_version="v1",
                                        body=rule_body)
            service.register_work(request_id="w1", actor_id="sec1", work_id="w1", title="作品",
                                  track_id="tA", required_disciplines=["design"],
                                  required_reviewer_count=1, material_ref="m/w1.pdf")
            service.register_team_member(request_id="tm1", actor_id="sec1", work_id="w1",
                                         person_key="p1", role_label="作者")
            service.create_batch(request_id="b1", actor_id="sec1", batch_id="b1",
                                 rule_version="v1")
            service.publish_batch(request_id="p1", actor_id="sec1", batch_id="b1")
            before_task = service.list_my_tasks("rv3")[0]["task_id"]
            service.claim_task(request_id="claim", actor_id="rv3", task_id=before_task)
            database.close()

            database = Database(database_path)
            service = BlindReviewService(database, clock)
            self.assertEqual("accepted", service.list_my_tasks("rv3")[0]["status"])
            self.assertEqual(before_task, service.list_my_tasks("rv3")[0]["task_id"])
            queue = service.get_batch_audit("jw1", "b1")["substitutes"]
            self.assertTrue(queue)
            self.assertEqual("queued", queue[0]["status"])
            database.close()


class SnapshotRuleTest(unittest.TestCase):
    def test_snapshot_hash_stable_and_covers_declarations(self):
        snapshot = build_snapshot(
            reviewers=[{"reviewer_id": "r1", "organization_id": "o", "active": True,
                        "disciplines": ["d1"]}],
            works=[{"work_id": "w1", "track_id": "t", "required_disciplines": ["d1"],
                    "required_reviewer_count": 2, "person_keys": ["p1"],
                    "organization_keys": ["o1"]}],
            declarations=[{"reviewer_id": "r1", "subject_type": "person", "subject_key": "p1",
                           "relation_type": "kinship", "active": True, "detail": ""}],
        )
        again = build_snapshot(
            reviewers=[{"reviewer_id": "r1", "organization_id": "o", "active": True,
                        "disciplines": ["d1"]}],
            works=[{"work_id": "w1", "track_id": "t", "required_disciplines": ["d1"],
                    "required_reviewer_count": 2, "person_keys": ["p1"],
                    "organization_keys": ["o1"]}],
            declarations=[{"reviewer_id": "r1", "subject_type": "person", "subject_key": "p1",
                           "relation_type": "kinship", "active": True, "detail": ""}],
        )
        self.assertEqual(snapshot.hash, again.hash)
        plan = generate_plan(snapshot, default_rule_body())
        self.assertTrue(plan.infeasible)


if __name__ == "__main__":
    unittest.main()
