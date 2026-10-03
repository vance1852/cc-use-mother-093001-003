"""运行基础服务与盲审回避后台的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .review_service import BlindReviewService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行登记链与完整盲审批次并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        review = BlindReviewService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范项目机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-auditor", actor_id="admin-001", new_actor_id="auditor-001",
                               display_name="纪检员", role="auditor", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号项目节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="program_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="program_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # ---- 盲审与利益回避后台 ----
        for reviewer_id, display_name in [("rev-1", "评委一"), ("rev-2", "评委二"), ("rev-3", "评委三"),
                                          ("rev-4", "评委四"), ("rev-5", "评委五")]:
            service.register_actor(request_id=f"req-{reviewer_id}", actor_id="admin-001",
                                   new_actor_id=reviewer_id, display_name=display_name,
                                   role="reviewer", organization_id="org-001")
        review.register_discipline(request_id="req-disc-design", actor_id="operator-001",
                                   discipline_id="design", name="设计")
        review.register_discipline(request_id="req-disc-tech", actor_id="operator-001",
                                   discipline_id="tech", name="技术")
        profiles = {"rev-1": ["design"], "rev-2": ["tech"], "rev-3": ["design", "tech"],
                    "rev-4": [], "rev-5": ["tech"]}
        for reviewer_id, disciplines in profiles.items():
            review.register_reviewer(request_id=f"req-profile-{reviewer_id}", actor_id="operator-001",
                                     reviewer_id=reviewer_id, display_name=reviewer_id,
                                     disciplines=disciplines)
        review.register_work(request_id="req-work", actor_id="operator-001", work_id="work-001",
                             title="匿名作品甲", track_id="track-a",
                             required_disciplines=["design", "tech"], required_reviewer_count=3,
                             material_ref="materials/work-001.pdf")
        review.register_team_member(request_id="req-team-1", actor_id="operator-001", work_id="work-001",
                                    person_key="person-alpha", role_label="作者", organization_id="org-alpha")
        review.register_team_member(request_id="req-team-2", actor_id="operator-001", work_id="work-001",
                                    person_key="person-beta", role_label="联合作者", organization_id="org-alpha")
        # rev-1 曾为作者机构提供顾问（任职），rev-2 与联合作者同属协会：均须回避。
        review.declare_conflict(request_id="req-conflict-1", actor_id="rev-1", reviewer_id="rev-1",
                                subject_type="organization", subject_key="org-alpha",
                                relation_type="employment", detail="曾任顾问")
        review.declare_conflict(request_id="req-conflict-2", actor_id="operator-001", reviewer_id="rev-2",
                                subject_type="person", subject_key="person-beta",
                                relation_type="association")
        review.create_rule_version(request_id="req-rules", actor_id="operator-001", rule_version="rules-1")
        review.create_batch(request_id="req-batch", actor_id="operator-001",
                            batch_id="batch-001", rule_version="rules-1")
        review.publish_batch(request_id="req-publish", actor_id="operator-001", batch_id="batch-001")

        allocation = review.get_batch_audit("auditor-001", "batch-001")
        assigned = sorted(
            item["reviewer_id"]
            for item in allocation["allocation"]["works"]["work-001"]["assignments"]
        )
        # rev-1/rev-2 被排除；任务落在 rev-3/design、rev-5/tech、rev-4 自由槽。
        scorer_map = {"rev-3": None, "rev-4": None, "rev-5": None}
        for reviewer_id in scorer_map:
            task_id = review.list_my_tasks(reviewer_id)[0]["task_id"]
            review.claim_task(request_id=f"req-claim-{reviewer_id}", actor_id=reviewer_id, task_id=task_id)
            scorer_map[reviewer_id] = task_id
        for index, (reviewer_id, task_id) in enumerate(scorer_map.items()):
            review.submit_score(request_id=f"req-score-{reviewer_id}", actor_id=reviewer_id,
                                task_id=task_id, score_value=80 + index)
        review.seal_work(request_id="req-seal", actor_id="operator-001",
                         batch_id="batch-001", work_id="work-001")

        # 封存后任何任务调整都必须失败。
        sealed_mutation_blocked = False
        try:
            review.recuse_task(request_id="req-recuse-late", actor_id="rev-3",
                               task_id=scorer_map["rev-3"], reason="迟到的回避")
        except Exception:
            sealed_mutation_blocked = True

        # 评委视图不得泄露作者身份。
        reviewer_view = json.dumps(review.list_my_tasks("rev-3"), ensure_ascii=False)
        identity_hidden = "person-alpha" not in reviewer_view and "org-alpha" not in reviewer_view

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "snapshot_verified": allocation["relationship_snapshot_hash_verified"],
                  "rule_hash_verified": allocation["rule_body_hash_verified"],
                  "assigned_reviewers": assigned,
                  "sealed_mutation_blocked": sealed_mutation_blocked,
                  "identity_hidden_from_reviewer": identity_hidden}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    required = ("status", "audit_valid", "snapshot_verified", "rule_hash_verified",
                "sealed_mutation_blocked", "identity_hidden_from_reviewer")
    return 0 if all(result[key] is True or result[key] == "ok" for key in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
