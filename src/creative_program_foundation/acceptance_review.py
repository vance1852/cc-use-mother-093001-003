"""运行盲审与利益回避后台的离线端到端验收。

场景要点：

- rv-d 曾为 w1 参赛机构提供顾问、rv-c 与 w1 作者合作且有亲属关系、
  rv-d 与 w1 作者存在师生关系、rv-b 与 w2 作者同属协会、rv-j 任职于 w3 机构；
- w3 要求的 music 专业无人覆盖，秘书凭 coverage_waiver 例外放行并留痕；
- 发布后经历并发领取、评分作废、评委缺席、候补递补与紧急替补；
- 达到法定人数才允许封存，封存后任何任务与分数调整被拒绝；
- 中途关闭并重开数据库，已接受任务和候补队列不被打乱；
- 纪检人员复核关系快照哈希、规则版本、回避原因、替补顺序和作废但保留的分数。
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError, PermissionDenied, ValidationError
from .review import ReviewService
from .service import DomainService
from .storage import Database


ORG = "org-festival"
TRACK = "t-creative"


def _seed(service: DomainService, review: ReviewService) -> None:
    service.register_organization(request_id="a-org", actor_id="bootstrap",
                                  organization_id=ORG, name="文创大赛组委会")
    actors = [
        ("admin-1", "系统管理员", "admin"),
        ("sec-1", "评审秘书", "operator"),
        ("aud-1", "纪检员", "auditor"),
    ]
    for index, (actor_id, name, role) in enumerate(actors):
        service.register_actor(request_id=f"a-actor-{actor_id}",
                               actor_id="bootstrap" if index == 0 else "admin-1",
                               new_actor_id=actor_id, display_name=name, role=role,
                               organization_id=ORG)
    review.register_rule_version(
        request_id="a-rules", actor_id="sec-1", rule_version_id="rules-2026-final",
        rules={"independent_reviewers": 2, "quorum": 2, "default_track_load_cap": 1,
               "substitute_count": 1},
    )
    review.register_track(request_id="a-track", actor_id="sec-1", track_id=TRACK,
                          name="创意设计赛道")

    reviewer_rows = [
        ("rv-a", ["design"]),
        ("rv-b", ["technology"]),
        ("rv-c", ["design", "technology"]),
        ("rv-d", ["technology"]),
        ("rv-e", ["design"]),
        ("rv-f", ["technology"]),
        ("rv-g", ["design"]),
        ("rv-h", ["design", "technology"]),
        ("rv-i", ["design"]),
        ("rv-j", ["technology"]),
    ]
    for reviewer_id, expertise in reviewer_rows:
        service.register_actor(request_id=f"a-actor-{reviewer_id}", actor_id="admin-1",
                               new_actor_id=reviewer_id, display_name=f"评委{reviewer_id}",
                               role="reviewer", organization_id=ORG)
        review.register_reviewer(request_id=f"a-profile-{reviewer_id}", actor_id="sec-1",
                                 reviewer_id=reviewer_id, expertise=expertise,
                                 track_ids=[TRACK])

    works = [
        # (work_id, anon_code, org, title, team, required_expertise, material)
        ("w-001", "ANON-1001", "org-x", "隐匿题名甲",
         [{"person_key": "p-author1", "associations": []}],
         ["design", "technology"], "material://anon-1001"),
        ("w-002", "ANON-1002", "org-y", "隐匿题名乙",
         [{"person_key": "p-author2", "associations": ["asn-001"]}],
         ["technology"], "material://anon-1002"),
        ("w-003", "ANON-1003", "org-z", "隐匿题名丙",
         [{"person_key": "p-author3", "associations": []}],
         ["music"], "material://anon-1003"),
    ]
    for work_id, anon, org, title, team, tags, material in works:
        review.register_work(request_id=f"a-work-{work_id}", actor_id="sec-1",
                             work_id=work_id, anon_code=anon, track_id=TRACK,
                             organization_id=org, title=title, material_uri=material,
                             required_expertise=tags, team=team)

    relations = [
        # rv-d 曾在 w1 参赛机构任职顾问，且与 w1 作者有师生关系
        ("rel-1", "rv-d", "organization", "org-x", "advisory", "2024 年顾问服务"),
        ("rel-2", "rv-d", "person", "p-author1", "teacher_student", "硕导"),
        # rv-c 与 w1 作者合作论文且为亲属
        ("rel-3", "rv-c", "person", "p-author1", "collaboration", "联合署名"),
        ("rel-4", "rv-c", "person", "p-author1", "kinship", "配偶"),
        # rv-b 与 w2 作者同属协会
        ("rel-5", "rv-b", "association", "asn-001", "association", "协会成员"),
        # rv-j 任职于 w3 机构
        ("rel-6", "rv-j", "organization", "org-z", "employment", "兼职设计师"),
    ]
    for relation_id, reviewer_id, kind_subject, target, kind, detail in relations:
        subject_kind = {"organization": "organization", "person": "person",
                        "association": "association"}[kind_subject]
        review.declare_relation(request_id=f"a-{relation_id}", actor_id="sec-1",
                                relation_id=relation_id, reviewer_id=reviewer_id,
                                subject_kind=subject_kind, target_key=target,
                                relation_kind=kind, detail=detail)

    review.create_batch(request_id="a-batch", actor_id="sec-1", batch_id="b-2026",
                        rule_version_id="rules-2026-final")
    for work_id, *_ in works:
        review.add_work_to_batch(request_id=f"a-add-{work_id}", actor_id="sec-1",
                                 batch_id="b-2026", work_id=work_id)
    # w3 的 music 专业无评委覆盖，秘书登记专业覆盖例外（利益冲突不可豁免）。
    review.grant_exception(request_id="a-waiver-w3-coverage", actor_id="sec-1",
                           batch_id="b-2026", work_id="w-003",
                           exception_type="coverage_waiver",
                           reason="music 为跨赛道参考项，经组委会批准豁免专业覆盖")


def _work_report(review: ReviewService, actor_id: str, work_id: str) -> dict:
    report = review.get_batch_report(actor_id, "b-2026")
    work, = [item for item in report["works"] if item["work_id"] == work_id]
    return work


def _task_id(work_report: dict, reviewer_id: str) -> str:
    task, = [task for task in work_report["tasks"] if task["reviewer_id"] == reviewer_id]
    return task["task_id"]


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "review.sqlite3"
        database = Database(db_path)
        clock = FixedClock(datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        review = ReviewService(database, clock)
        _seed(service, review)

        plan_receipt = review.generate_plan(request_id="a-plan", actor_id="sec-1",
                                            batch_id="b-2026")
        assert not plan_receipt.replayed
        review.publish_batch(request_id="a-publish", actor_id="sec-1", batch_id="b-2026")

        # 发布后立即重启：已接受状态与候补队列必须原样保留。
        database.close()
        database = Database(db_path)
        service = DomainService(database, clock)
        review = ReviewService(database, clock)

        report = review.get_batch_report("aud-1", "b-2026")
        assert report["rule_version"]["rule_version_id"] == "rules-2026-final"
        assert report["status"] == "published"
        w1 = _work_report(review, "aud-1", "w-001")
        w2 = _work_report(review, "aud-1", "w-002")
        w3 = _work_report(review, "aud-1", "w-003")
        primary_w1 = {item["reviewer_id"] for item in w1["selection"]}
        assert primary_w1 == {"rv-a", "rv-b"}, primary_w1
        assert [item["reviewer_id"] for item in w1["substitute_order"]] == ["rv-e"]
        primary_w2 = {item["reviewer_id"] for item in w2["selection"]}
        assert primary_w2 == {"rv-c", "rv-d"}, primary_w2
        primary_w3 = {item["reviewer_id"] for item in w3["selection"]}
        assert primary_w3 == {"rv-e", "rv-f"}, primary_w3

        def exclusion_codes(work_report: dict) -> dict[str, list[str]]:
            codes: dict[str, list[str]] = {}
            for item in work_report["exclusions"]:
                if item["reason_code"] == "conflict":
                    codes[item["reviewer_id"]] = [
                        reason["code"] for reason in item["detail"]
                    ]
            return codes

        assert exclusion_codes(w1)["rv-d"] == ["conflict_advisory",
                                               "conflict_teacher_student"]
        assert exclusion_codes(w1)["rv-c"] == ["conflict_collaboration",
                                               "conflict_kinship"]
        assert exclusion_codes(w2)["rv-b"] == ["conflict_association"]
        assert "rv-j" in {item["reviewer_id"] for item in w3["exclusions"]
                          if item["reason_code"] == "conflict"}
        assert w3["uncovered_expertise"] == ["music"]

        # 评委视角只见匿名任务：无机构、无题名、无团队信息。
        my_view = review.list_my_tasks("rv-a")
        my_w1, = [item for item in my_view["items"] if item["anon_code"] == "ANON-1001"]
        assert "material_uri" not in my_w1  # 未领取前不暴露材料
        serialized = json.dumps(my_view, ensure_ascii=False)
        for secret in ("org-x", "隐匿题名甲", "p-author1"):
            assert secret not in serialized

        # 并发领取同一任务：四个不同 request_id 只有一个请求成功，其余冲突。
        task_a = _task_id(w1, "rv-a")
        outcomes: list[tuple[str, str]] = []
        barrier = threading.Barrier(4)

        def claim(request_id: str) -> None:
            barrier.wait()
            try:
                receipt = review.claim_task(request_id=request_id, actor_id="rv-a",
                                            task_id=task_a)
                outcomes.append((request_id,
                                 "replayed" if receipt.replayed else "claimed"))
            except ConflictError:
                outcomes.append((request_id, "conflict"))

        request_ids = [f"a-claim-a-{i}" for i in range(4)]
        threads = [threading.Thread(target=claim, args=(request_id,))
                   for request_id in request_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert [result for _, result in outcomes].count("claimed") == 1, outcomes
        assert [result for _, result in outcomes].count("conflict") == 3, outcomes
        winning_request = next(request_id for request_id, result in outcomes
                               if result == "claimed")
        # 获胜请求在并发结束后用同一 request_id 重试，安全返回原回执。
        replay_receipt = review.claim_task(request_id=winning_request, actor_id="rv-a",
                                           task_id=task_a)
        assert replay_receipt.replayed

        # 领取后可查看匿名材料，但仍不含身份字段。
        material = review.get_task_material("rv-a", task_a)
        assert material["material_uri"] == "material://anon-1001"
        # 冲突评委访问他人/冲突作品任务一律“不存在”，避免侧写。
        try:
            review.get_task_material("rv-c", task_a)
        except Exception as exc:  # noqa: BLE001 - 断言类型在下方统一判断
            assert type(exc).__name__ == "NotFoundError"
        else:
            raise AssertionError("冲突评委不应读到任务材料")

        review.submit_score(request_id="a-score-a", actor_id="rv-a", task_id=task_a,
                            score={"total": 88, "dimensions": {"idea": 44},
                                   "comment": "匿名评审意见 A"})
        task_b = _task_id(w1, "rv-b")
        review.claim_task(request_id="a-claim-b", actor_id="rv-b", task_id=task_b)
        review.submit_score(request_id="a-score-b", actor_id="rv-b", task_id=task_b,
                            score={"total": 90, "comment": "匿名评审意见 B"})

        # 法定人数不足不能封存。
        try:
            review.seal_work(request_id="a-seal-w2-early", actor_id="sec-1",
                             batch_id="b-2026", work_id="w-002")
        except ConflictError:
            pass
        else:
            raise AssertionError("未达法定人数不应允许封存")

        # 秘书作废 rv-b 的评分：分数原样保留，第一候补 rv-e 自动递补。
        review.invalidate_score(request_id="a-invalidate-b", actor_id="sec-1",
                                task_id=task_b, reason="发现评分表填写错误，按规定作废")
        w1 = _work_report(review, "aud-1", "w-001")
        score_b = [task for task in w1["tasks"]
                   if task["reviewer_id"] == "rv-b"][0]["score"]
        assert score_b["total"] == 90 and score_b["valid_for_quorum"] is False

        task_e_w1 = _task_id(w1, "rv-e")
        review.claim_task(request_id="a-claim-e-w1", actor_id="rv-e", task_id=task_e_w1)
        review.submit_score(request_id="a-score-e", actor_id="rv-e", task_id=task_e_w1,
                            score={"total": 78})
        review.seal_work(request_id="a-seal-w1", actor_id="sec-1",
                         batch_id="b-2026", work_id="w-001")

        # 封存后形成的评审事实冻结：评委不能再回避，秘书不能再追加替补。
        try:
            review.recuse_task(request_id="a-recuse-a-late", actor_id="rv-a",
                               task_id=task_a, reason="封存后试图回避")
        except ConflictError:
            pass
        else:
            raise AssertionError("封存后不应允许调整任务")

        # w2：rv-c 正常评分；rv-d 缺席 → rv-e 递补；rv-e 随后缺席 → 紧急替补 rv-h。
        w2 = _work_report(review, "aud-1", "w-002")
        task_c = _task_id(w2, "rv-c")
        review.claim_task(request_id="a-claim-c", actor_id="rv-c", task_id=task_c)
        review.submit_score(request_id="a-score-c", actor_id="rv-c", task_id=task_c,
                            score={"total": 92})
        review.mark_absent(request_id="a-absent-d", actor_id="sec-1", batch_id="b-2026",
                           reviewer_id="rv-d", reason="突发疾病无法参评")
        # 利益冲突红线：紧急替补也不能指派与作品同属协会的 rv-b。
        try:
            review.emergency_assign(request_id="a-emergency-conflict", actor_id="sec-1",
                                    batch_id="b-2026", work_id="w-002",
                                    reviewer_id="rv-b", reason="试图突破回避红线")
        except PermissionDenied:
            pass
        else:
            raise AssertionError("存在利益冲突时紧急替补必须被拒绝")
        review.mark_absent(request_id="a-absent-e", actor_id="sec-1", batch_id="b-2026",
                           reviewer_id="rv-e", reason="交通中断无法继续")
        emergency = review.emergency_assign(
            request_id="a-emergency-h", actor_id="sec-1", batch_id="b-2026",
            work_id="w-002", reviewer_id="rv-h",
            reason="两名评委先后退出，按候补耗尽后的例外程序补位",
        )
        assert not emergency.replayed
        w2 = _work_report(review, "aud-1", "w-002")
        task_h = _task_id(w2, "rv-h")
        review.claim_task(request_id="a-claim-h", actor_id="rv-h", task_id=task_h)
        review.submit_score(request_id="a-score-h", actor_id="rv-h", task_id=task_h,
                            score={"total": 83})
        review.seal_work(request_id="a-seal-w2", actor_id="sec-1",
                         batch_id="b-2026", work_id="w-002")

        # w3：rv-e 的正式任务因批次缺席终止，候补 rv-g 递补，rv-f 正常评分。
        w3 = _work_report(review, "aud-1", "w-003")
        task_f = _task_id(w3, "rv-f")
        review.claim_task(request_id="a-claim-f", actor_id="rv-f", task_id=task_f)
        review.submit_score(request_id="a-score-f", actor_id="rv-f", task_id=task_f,
                            score={"total": 76})
        task_g = _task_id(w3, "rv-g")
        assert [task for task in w3["tasks"]
                if task["reviewer_id"] == "rv-g"][0]["status"] == "offered"
        review.claim_task(request_id="a-claim-g", actor_id="rv-g", task_id=task_g)
        review.submit_score(request_id="a-score-g", actor_id="rv-g", task_id=task_g,
                            score={"total": 81})
        review.seal_work(request_id="a-seal-w3", actor_id="sec-1",
                         batch_id="b-2026", work_id="w-003")
        review.seal_batch(request_id="a-seal-batch", actor_id="sec-1", batch_id="b-2026")

        # 角色边界：纪检不能写、秘书不能改分、评委不能排批次。
        for call, args in (
            (review.register_track,
             {"request_id": "x", "actor_id": "aud-1", "track_id": "t-x", "name": "x"}),
        ):
            try:
                call(**args)
            except PermissionDenied:
                pass
            else:
                raise AssertionError("纪检人员不应有写入权限")
        try:
            review.submit_score(request_id="x", actor_id="sec-1", task_id=task_a,
                                score={"total": 60})
        except PermissionDenied:
            pass
        else:
            raise AssertionError("秘书不应能提交或修改分数")
        try:
            review.generate_plan(request_id="x", actor_id="rv-a", batch_id="b-2026")
        except (PermissionDenied, ConflictError):
            pass
        else:
            raise AssertionError("评委不应能生成分配方案")

        # 纪检复核：快照哈希自洽、规则版本/回避原因/替补顺序/事件链齐全。
        snapshot = review.get_snapshot("aud-1", w1["snapshot"]["snapshot_id"])
        assert snapshot["hash_match"] is True
        assert snapshot["relation_count"] >= 6
        final = review.get_batch_report("aud-1", "b-2026")
        assert final["status"] == "sealed"
        final_w1, = [item for item in final["works"] if item["work_id"] == "w-001"]
        assert final_w1["snapshot"]["hash_match"] is True
        event_chains = {
            task["reviewer_id"]: [event["to_status"] for event in task["events"]]
            for task in final_w1["tasks"]
        }
        assert event_chains["rv-b"] == ["claimed", "scored", "invalidated"]
        assert event_chains["rv-e"][0] == "offered"  # 候补递补事件留痕
        final_w2, = [item for item in final["works"] if item["work_id"] == "w-002"]
        exception_types = {item["exception_type"] for item in final_w2["exceptions"]}
        assert "emergency_substitution" in exception_types
        final_w3, = [item for item in final["works"] if item["work_id"] == "w-003"]
        assert any(item["exception_type"] == "coverage_waiver"
                   for item in final_w3["exceptions"])

        valid, event_count = service.verify_audit()
        assert valid
        database.close()
        return {
            "status": "ok",
            "works": 3,
            "concurrent_claim_outcomes": sorted(outcomes),
            "sealed_batch": final["status"],
            "invalidated_score_preserved": score_b["total"],
            "snapshot_hash_match": final_w1["snapshot"]["hash_match"],
            "rule_version": final["rule_version"]["rule_version_id"],
            "audit_events": event_count,
            "audit_valid": valid,
        }


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
