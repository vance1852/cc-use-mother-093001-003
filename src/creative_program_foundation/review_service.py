"""盲审与利益回避后台服务。

职责边界：
- 秘书（operator/admin）登记评委、作品、团队机构关系与回避声明，发布批次、处理例外；
- 评委（reviewer）只能查看自己的匿名任务、领取并提交评分；
- 纪检（auditor）复核每次分配冻结的关系快照、规则版本、回避原因与替补顺序；
- 秘书与管理员都不能写入或修改分数，评分只能由任务所属评委提交。

所有状态都保存在 SQLite 中：进程重启不会打乱已接受任务或候补队列。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .review_models import RuleVersion
from .review_rules import (
    Snapshot, build_snapshot, default_rule_body, generate_plan, validate_rule_body,
)
from .storage import Database

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
SUBJECT_TYPES = frozenset({"person", "organization"})
RELATION_TYPES = frozenset({
    "employment", "collaboration", "kinship", "mentor", "association", "organization",
})
TASK_TERMINAL_BEFORE_SEAL = frozenset({"recused", "absent", "voided", "reassigned"})


class BlindReviewService:
    """实现盲审登记、分配、回避、评分与封存的全部用例。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ---------- 基础工具 ----------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return {"actor_id": row["actor_id"], "role": row["role"],
                "organization_id": row["organization_id"]}

    def _require(self, actor: dict[str, Any], *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    "response": json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, "response": response}

    def _audit(self, connection, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _load_disciplines(self, connection, discipline_ids: list[str]) -> list[str]:
        result: list[str] = []
        for discipline_id in discipline_ids:
            discipline_id = self._identifier(discipline_id, "discipline_id")
            if connection.execute("SELECT 1 FROM review_disciplines WHERE discipline_id=?",
                                  (discipline_id,)).fetchone() is None:
                raise ValidationError(f"专业 {discipline_id} 尚未登记")
            result.append(discipline_id)
        return result

    # ---------- 基础档案 ----------

    def register_discipline(self, *, request_id: str, actor_id: str,
                            discipline_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "discipline_id": discipline_id, "name": name}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            discipline_id = self._identifier(discipline_id, "discipline_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO review_disciplines(discipline_id,name,created_at) VALUES(?,?,?)",
                        (discipline_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("专业编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="review.discipline_registered",
                            resource_type="review_discipline", resource_id=discipline_id,
                            detail={"name": name})
                return "review_discipline", discipline_id, {"discipline_id": discipline_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.register_discipline", payload=payload, create=create)

    def register_reviewer(self, *, request_id: str, actor_id: str, reviewer_id: str,
                          display_name: str, disciplines: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "reviewer_id": reviewer_id,
                    "display_name": display_name, "disciplines": list(disciplines or [])}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reviewer_actor = connection.execute(
                "SELECT * FROM actors WHERE actor_id=?", (reviewer_id,)
            ).fetchone()
            if reviewer_actor is None:
                raise NotFoundError("评委必须先以 reviewer 角色登记为操作者")
            if reviewer_actor["role"] != "reviewer":
                raise ValidationError("该操作者的角色不是 reviewer")
            reviewer_id = self._identifier(reviewer_id, "reviewer_id")
            display_name = self._text(display_name, "display_name")
            discipline_ids = sorted(set(self._load_disciplines(connection, list(disciplines or []))))

            def create():
                try:
                    connection.execute(
                        "INSERT INTO reviewers(reviewer_id,display_name,organization_id,active,"
                        "disciplines_json,created_at) VALUES(?,?,?,1,?,?)",
                        (reviewer_id, display_name, reviewer_actor["organization_id"],
                         canonical_json(discipline_ids), self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("评委档案已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="review.reviewer_registered",
                            resource_type="reviewer", resource_id=reviewer_id,
                            detail={"display_name": display_name, "disciplines": discipline_ids,
                                    "organization_id": reviewer_actor["organization_id"]})
                return "reviewer", reviewer_id, {"reviewer_id": reviewer_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.register_reviewer", payload=payload, create=create)

    def register_work(self, *, request_id: str, actor_id: str, work_id: str, title: str,
                      track_id: str, required_disciplines: list[str],
                      required_reviewer_count: int, material_ref: str,
                      anonymized_code: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "work_id": work_id, "title": title, "track_id": track_id,
                   "required_disciplines": list(required_disciplines or []),
                   "required_reviewer_count": required_reviewer_count,
                   "material_ref": material_ref, "anonymized_code": anonymized_code}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            work_id = self._identifier(work_id, "work_id")
            title = self._text(title, "title")
            track_id = self._identifier(track_id, "track_id")
            material_ref = self._text(material_ref, "material_ref", 500)
            disciplines = sorted(set(self._load_disciplines(connection, list(required_disciplines or []))))
            if not isinstance(required_reviewer_count, int) or required_reviewer_count < 1:
                raise ValidationError("required_reviewer_count 必须是正整数")
            if required_reviewer_count < len(disciplines):
                raise ValidationError("独立评委数不能少于要求覆盖的专业数")
            anonymized_code = self._text(anonymized_code or f"ANON-{work_id}", "anonymized_code", 120)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO works(work_id,title,track_id,required_disciplines_json,"
                        "required_reviewer_count,material_ref,anonymized_code,sealed,created_at) "
                        "VALUES(?,?,?,?,?,?,?,0,?)",
                        (work_id, title, track_id, canonical_json(disciplines),
                         required_reviewer_count, material_ref, anonymized_code, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号或匿名编号已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="review.work_registered",
                            resource_type="work", resource_id=work_id,
                            detail={"track_id": track_id, "anonymized_code": anonymized_code,
                                    "required_reviewer_count": required_reviewer_count,
                                    "required_disciplines": disciplines})
                return "work", work_id, {"work_id": work_id, "anonymized_code": anonymized_code}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.register_work", payload=payload, create=create)

    def register_team_member(self, *, request_id: str, actor_id: str, work_id: str,
                             person_key: str, role_label: str,
                             organization_id: str = "") -> dict[str, Any]:
        """登记作品团队成员（作者、联合作者）或机构关系；该信息绝不进入评委视图。"""

        payload = {"actor_id": actor_id, "work_id": work_id, "person_key": person_key,
                   "role_label": role_label, "organization_id": organization_id}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            work_id = self._identifier(work_id, "work_id")
            person_key = self._identifier(person_key, "person_key")
            role_label = self._text(role_label, "role_label", 80)
            organization_id = str(organization_id or "").strip()
            if organization_id:
                organization_id = self._identifier(organization_id, "organization_id")
            if connection.execute("SELECT 1 FROM works WHERE work_id=?", (work_id,)).fetchone() is None:
                raise NotFoundError("作品不存在")
            member_id = uuid.uuid4().hex

            def create():
                try:
                    connection.execute(
                        "INSERT INTO work_team_members(member_id,work_id,person_key,role_label,"
                        "organization_id,created_at) VALUES(?,?,?,?,?,?)",
                        (member_id, work_id, person_key, role_label, organization_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("该作品已登记同一人员") from exc
                self._audit(connection, actor_id=actor_id, action="review.team_member_registered",
                            resource_type="work_team_member", resource_id=member_id,
                            detail={"work_id": work_id, "person_key": person_key,
                                    "role_label": role_label, "organization_id": organization_id})
                return "work_team_member", member_id, {"member_id": member_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.register_team_member", payload=payload, create=create)

    def declare_conflict(self, *, request_id: str, actor_id: str, reviewer_id: str,
                         subject_type: str, subject_key: str, relation_type: str,
                         detail: str = "") -> dict[str, Any]:
        """登记评委任职、合作、亲属、师生、同属协会或同机构等回避关系。"""

        payload = {"actor_id": actor_id, "reviewer_id": reviewer_id, "subject_type": subject_type,
                   "subject_key": subject_key, "relation_type": relation_type, "detail": detail}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            if actor["role"] == "reviewer":
                if actor_id != reviewer_id:
                    raise PermissionDenied("评委只能为自己声明回避关系")
            else:
                self._require(actor, "admin", "operator")
            reviewer_id = self._identifier(reviewer_id, "reviewer_id")
            if connection.execute("SELECT 1 FROM reviewers WHERE reviewer_id=?", (reviewer_id,)).fetchone() is None:
                raise NotFoundError("评委档案不存在")
            if subject_type not in SUBJECT_TYPES:
                raise ValidationError("subject_type 只能是 person 或 organization")
            subject_key = self._identifier(subject_key, "subject_key")
            if relation_type not in RELATION_TYPES:
                raise ValidationError(f"relation_type 必须是 {sorted(RELATION_TYPES)} 之一")
            detail = str(detail or "").strip()[:500]

            def create():
                existing = connection.execute(
                    "SELECT * FROM conflict_declarations WHERE reviewer_id=? AND subject_type=? "
                    "AND subject_key=? AND relation_type=?",
                    (reviewer_id, subject_type, subject_key, relation_type),
                ).fetchone()
                if existing:
                    if not existing["active"]:
                        connection.execute(
                            "UPDATE conflict_declarations SET active=1, detail=?, declared_at=? "
                            "WHERE declaration_id=?",
                            (detail, self._now(), existing["declaration_id"]),
                        )
                        declaration_id = existing["declaration_id"]
                    else:
                        raise ConflictError("该回避关系已经声明")
                else:
                    declaration_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO conflict_declarations(declaration_id,reviewer_id,subject_type,"
                        "subject_key,relation_type,detail,active,declared_at) VALUES(?,?,?,?,?,?,1,?)",
                        (declaration_id, reviewer_id, subject_type, subject_key,
                         relation_type, detail, self._now()),
                    )
                self._audit(connection, actor_id=actor_id, action="review.conflict_declared",
                            resource_type="conflict_declaration", resource_id=declaration_id,
                            detail={"reviewer_id": reviewer_id, "subject_type": subject_type,
                                    "subject_key": subject_key, "relation_type": relation_type})
                return "conflict_declaration", declaration_id, {"declaration_id": declaration_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.declare_conflict", payload=payload, create=create)

    # ---------- 规则版本 ----------

    def create_rule_version(self, *, request_id: str, actor_id: str, rule_version: str,
                            body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "rule_version": rule_version,
                   "body": body or default_rule_body()}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            rule_version = self._identifier(rule_version, "rule_version")
            rule_body = body if body is not None else default_rule_body()
            validate_rule_body(rule_body)
            body_text = canonical_json(rule_body)
            body_hash = digest(rule_body)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO rule_versions(rule_version,body_json,body_hash,created_by,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (rule_version, body_text, body_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("规则版本已经存在") from exc
                self._audit(connection, actor_id=actor_id, action="review.rule_version_created",
                            resource_type="rule_version", resource_id=rule_version,
                            detail={"body_hash": body_hash})
                return "rule_version", rule_version, {"rule_version": rule_version, "body_hash": body_hash}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.create_rule_version", payload=payload, create=create)

    def get_rule_version(self, rule_version: str) -> RuleVersion:
        row = self.database.connection.execute(
            "SELECT * FROM rule_versions WHERE rule_version=?", (rule_version,)
        ).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return RuleVersion(row["rule_version"], json.loads(row["body_json"]),
                           row["body_hash"], row["created_by"], row["created_at"])

    # ---------- 批次、快照与分配 ----------

    def _snapshot_from_db(self, connection) -> Snapshot:
        reviewers = [
            {"reviewer_id": row["reviewer_id"], "organization_id": row["organization_id"],
             "active": bool(row["active"]), "disciplines": json.loads(row["disciplines_json"])}
            for row in connection.execute("SELECT * FROM reviewers")
        ]
        works: list[dict[str, Any]] = []
        for row in connection.execute("SELECT * FROM works"):
            team = connection.execute(
                "SELECT person_key, organization_id FROM work_team_members WHERE work_id=?",
                (row["work_id"],),
            ).fetchall()
            works.append({
                "work_id": row["work_id"], "track_id": row["track_id"],
                "required_disciplines": json.loads(row["required_disciplines_json"]),
                "required_reviewer_count": row["required_reviewer_count"],
                "person_keys": [t["person_key"] for t in team],
                "organization_keys": sorted({t["organization_id"] for t in team if t["organization_id"]}),
            })
        declarations = [
            {"reviewer_id": row["reviewer_id"], "subject_type": row["subject_type"],
             "subject_key": row["subject_key"], "relation_type": row["relation_type"],
             "detail": row["detail"], "active": bool(row["active"])}
            for row in connection.execute("SELECT * FROM conflict_declarations")
        ]
        return build_snapshot(reviewers=reviewers, works=works, declarations=declarations)

    def create_batch(self, *, request_id: str, actor_id: str, batch_id: str,
                     rule_version: str) -> dict[str, Any]:
        """冻结当前关系快照并按指定规则版本生成可解释的分配方案（草稿状态）。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "rule_version": rule_version}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch_id = self._identifier(batch_id, "batch_id")
            rule_row = connection.execute(
                "SELECT * FROM rule_versions WHERE rule_version=?", (rule_version,)
            ).fetchone()
            if rule_row is None:
                raise NotFoundError("规则版本不存在")
            rule_body = json.loads(rule_row["body_json"])

            snapshot = self._snapshot_from_db(connection)

            # 作品法定人数不得低于规则下限。
            for work in snapshot.body["works"]:
                if work["required_reviewer_count"] < rule_body["min_independent_reviewers"]:
                    raise ValidationError(
                        f"作品 {work['work_id']} 的独立评委数低于规则下限 "
                        f"{rule_body['min_independent_reviewers']}"
                    )

            plan = generate_plan(snapshot, rule_body)
            explanation = {
                "rule_version": rule_version,
                "snapshot_hash": snapshot.hash,
                "works": plan.explanations,
                "infeasible": plan.infeasible,
            }

            def create():
                try:
                    connection.execute(
                        "INSERT INTO review_batches(batch_id,rule_version,status,"
                        "relationship_snapshot_json,relationship_snapshot_hash,explanation_json,"
                        "created_by,created_at) VALUES(?,?, 'draft', ?,?,?,?,?)",
                        (batch_id, rule_version, canonical_json(snapshot.body), snapshot.hash,
                         canonical_json(explanation), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                for task in plan.tasks:
                    connection.execute(
                        "INSERT INTO review_tasks(task_id,batch_id,work_id,reviewer_id,discipline_id,"
                        "slot_index,status,created_at) VALUES(?,?,?,?,?,?, 'assigned', ?)",
                        (uuid.uuid4().hex, batch_id, task.work_id, task.reviewer_id,
                         task.discipline_id or None, task.slot_index, self._now()),
                    )
                for substitute in plan.substitutes:
                    connection.execute(
                        "INSERT INTO review_substitutes(substitute_id,batch_id,work_id,reviewer_id,"
                        "rank_order,reason,status,created_at) VALUES(?,?,?,?,?,?,'queued',?)",
                        (uuid.uuid4().hex, batch_id, substitute.work_id, substitute.reviewer_id,
                         substitute.rank_order, substitute.reason, self._now()),
                    )
                self._audit(connection, actor_id=actor_id, action="review.batch_created",
                            resource_type="review_batch", resource_id=batch_id,
                            detail={"rule_version": rule_version, "snapshot_hash": snapshot.hash,
                                    "tasks": len(plan.tasks), "substitutes": len(plan.substitutes),
                                    "infeasible": plan.infeasible})
                return ("review_batch", batch_id,
                        {"batch_id": batch_id, "snapshot_hash": snapshot.hash,
                         "infeasible": plan.infeasible})

            return self._idempotent(connection, request_id=request_id,
                                    action="review.create_batch", payload=payload, create=create)

    def _batch_row(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM review_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def publish_batch(self, *, request_id: str, actor_id: str, batch_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch_row(connection, batch_id)
            if batch["status"] != "draft":
                raise ConflictError("只有草稿批次可以发布")
            explanation = json.loads(batch["explanation_json"])
            if explanation["infeasible"]:
                raise ConflictError("仍有作品无法满足分配规则，不能发布；请处理例外后重建批次")

            def create():
                connection.execute(
                    "UPDATE review_batches SET status='published', published_at=? WHERE batch_id=?",
                    (self._now(), batch_id),
                )
                self._audit(connection, actor_id=actor_id, action="review.batch_published",
                            resource_type="review_batch", resource_id=batch_id,
                            detail={"snapshot_hash": batch["relationship_snapshot_hash"]})
                return "review_batch", batch_id, {"batch_id": batch_id, "status": "published"}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.publish_batch", payload=payload, create=create)

    def close_batch(self, *, request_id: str, actor_id: str, batch_id: str) -> dict[str, Any]:
        """全部作品封存后关闭批次；关闭后不再接受任何任务操作。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch_row(connection, batch_id)
            if batch["status"] != "published":
                raise ConflictError("只有已发布批次可以关闭")
            unsealed = connection.execute(
                "SELECT COUNT(*) AS count FROM works WHERE work_id IN "
                "(SELECT DISTINCT work_id FROM review_tasks WHERE batch_id=?) AND sealed=0",
                (batch_id,),
            ).fetchone()["count"]
            if unsealed:
                raise ConflictError(f"仍有 {unsealed} 件作品未封存，不能关闭批次")

            def create():
                connection.execute("UPDATE review_batches SET status='closed' WHERE batch_id=?",
                                   (batch_id,))
                self._audit(connection, actor_id=actor_id, action="review.batch_closed",
                            resource_type="review_batch", resource_id=batch_id,
                            detail={"snapshot_hash": batch["relationship_snapshot_hash"]})
                return "review_batch", batch_id, {"batch_id": batch_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.close_batch", payload=payload, create=create)

    # ---------- 任务状态机与评分 ----------

    def _task_row(self, connection, task_id: str):
        row = connection.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("评审任务不存在")
        return row

    def _ensure_work_unsealed(self, connection, work_id: str) -> None:
        row = connection.execute("SELECT sealed FROM works WHERE work_id=?", (work_id,)).fetchone()
        if row is not None and row["sealed"]:
            raise ConflictError("作品评审已经封存，形成的评审事实不得再调整")

    def _promote_substitute(self, connection, *, batch_id: str, work_id: str,
                            vacated_task_id: str, cause: str) -> str | None:
        """把最高顺位候补提升为新任务；调用方已持有写事务。

        若被撤下的任务承担某个必修专业，而该候补具备该专业，则新任务继续锚定该专业，
        否则作为自由槽位；候补排序已把能覆盖必修专业者排在前面。
        """

        vacated = connection.execute(
            "SELECT discipline_id FROM review_tasks WHERE task_id=?", (vacated_task_id,)
        ).fetchone()
        needed_discipline = vacated["discipline_id"]
        if needed_discipline:
            # 先在候补队列中按顺位找能承接该专业者，找不到再退回队首。
            candidate = connection.execute(
                "SELECT s.* FROM review_substitutes s JOIN reviewers r ON r.reviewer_id=s.reviewer_id "
                "WHERE s.batch_id=? AND s.work_id=? AND s.status='queued' "
                "AND EXISTS (SELECT 1 FROM json_each(r.disciplines_json) WHERE value=?) "
                "ORDER BY s.rank_order LIMIT 1",
                (batch_id, work_id, needed_discipline),
            ).fetchone()
            if candidate is None:
                candidate = connection.execute(
                    "SELECT * FROM review_substitutes WHERE batch_id=? AND work_id=? AND status='queued' "
                    "ORDER BY rank_order LIMIT 1",
                    (batch_id, work_id),
                ).fetchone()
        else:
            candidate = connection.execute(
                "SELECT * FROM review_substitutes WHERE batch_id=? AND work_id=? AND status='queued' "
                "ORDER BY rank_order LIMIT 1",
                (batch_id, work_id),
            ).fetchone()
        if candidate is None:
            self._audit(connection, actor_id="system", action="review.substitute_unavailable",
                        resource_type="work", resource_id=work_id,
                        detail={"vacated_task_id": vacated_task_id, "cause": cause})
            return None
        carried_discipline = needed_discipline if needed_discipline else None
        next_slot = connection.execute(
            "SELECT COALESCE(MAX(slot_index) + 1, 0) AS slot FROM review_tasks "
            "WHERE batch_id=? AND work_id=?",
            (batch_id, work_id),
        ).fetchone()["slot"]
        new_task_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO review_tasks(task_id,batch_id,work_id,reviewer_id,discipline_id,"
            "slot_index,status,conflict_reason,created_at) VALUES(?,?,?,?,?,?,'assigned','',?)",
            (new_task_id, batch_id, work_id, candidate["reviewer_id"], carried_discipline,
             next_slot, self._now()),
        )
        connection.execute(
            "UPDATE review_substitutes SET status='promoted' WHERE substitute_id=?",
            (candidate["substitute_id"],),
        )
        self._audit(connection, actor_id="system", action="review.substitute_promoted",
                    resource_type="review_task", resource_id=new_task_id,
                    detail={"batch_id": batch_id, "work_id": work_id,
                            "reviewer_id": candidate["reviewer_id"],
                            "rank_order": candidate["rank_order"],
                            "vacated_task_id": vacated_task_id, "cause": cause,
                            "reason": candidate["reason"]})
        return new_task_id

    def _vacate(self, connection, *, task_id: str, new_status: str, cause: str,
                actor_id: str, reason: str) -> dict[str, Any]:
        task = self._task_row(connection, task_id)
        batch = self._batch_row(connection, task["batch_id"])
        if batch["status"] != "published":
            raise ConflictError("批次尚未发布，不能调整任务")
        self._ensure_work_unsealed(connection, task["work_id"])
        if task["status"] in ("submitted", "sealed"):
            raise ConflictError("已提交或已封存的任务不能这样撤销；评分请走作废流程")
        if task["status"] in TASK_TERMINAL_BEFORE_SEAL:
            raise ConflictError("任务已经处于终态")
        connection.execute(
            "UPDATE review_tasks SET status=?, conflict_reason=? WHERE task_id=?",
            (new_status, reason or cause, task_id),
        )
        self._audit(connection, actor_id=actor_id, action=f"review.task_{new_status}",
                    resource_type="review_task", resource_id=task_id,
                    detail={"work_id": task["work_id"], "reviewer_id": task["reviewer_id"],
                            "cause": cause, "reason": reason})
        new_task_id = self._promote_substitute(
            connection, batch_id=task["batch_id"], work_id=task["work_id"],
            vacated_task_id=task_id, cause=cause,
        )
        return {"task_id": task_id, "status": new_status,
                "replacement_task_id": new_task_id}

    def recuse_task(self, *, request_id: str, actor_id: str, task_id: str,
                    reason: str = "") -> dict[str, Any]:
        """评委对自己的任务提出临时回避；自动按候补顺序补位。"""

        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            task = self._task_row(connection, task_id)
            if task["reviewer_id"] != actor_id:
                raise PermissionDenied("只能回避自己的评审任务")

            def create():
                return ("review_task", task_id,
                        self._vacate(connection, task_id=task_id, new_status="recused",
                                     cause="recusal", actor_id=actor_id, reason=reason))

            return self._idempotent(connection, request_id=request_id,
                                    action="review.recuse_task", payload=payload, create=create)

    def mark_task_absent(self, *, request_id: str, actor_id: str, task_id: str,
                         reason: str = "") -> dict[str, Any]:
        """秘书记录评委缺席并触发替补。"""

        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create():
                return ("review_task", task_id,
                        self._vacate(connection, task_id=task_id, new_status="absent",
                                     cause="absence", actor_id=actor_id, reason=reason))

            return self._idempotent(connection, request_id=request_id,
                                    action="review.mark_absent", payload=payload, create=create)

    def claim_task(self, *, request_id: str, actor_id: str, task_id: str) -> dict[str, Any]:
        """评委领取任务；行级条件更新保证并发下只有一次成功。"""

        payload = {"actor_id": actor_id, "task_id": task_id}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            task = self._task_row(connection, task_id)
            if task["reviewer_id"] != actor_id:
                raise PermissionDenied("只能领取分配给自己的任务")
            batch = self._batch_row(connection, task["batch_id"])
            if batch["status"] != "published":
                raise ConflictError("批次未发布，任务尚不能领取")
            self._ensure_work_unsealed(connection, task["work_id"])

            def create():
                cursor = connection.execute(
                    "UPDATE review_tasks SET status='accepted' "
                    "WHERE task_id=? AND status='assigned'",
                    (task_id,),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("任务已被领取或状态已变化，请勿重复操作")
                self._audit(connection, actor_id=actor_id, action="review.task_claimed",
                            resource_type="review_task", resource_id=task_id,
                            detail={"work_id": task["work_id"]})
                return "review_task", task_id, {"task_id": task_id, "status": "accepted"}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.claim_task", payload=payload, create=create)

    def submit_score(self, *, request_id: str, actor_id: str, task_id: str,
                     score_value: float, comment: str = "") -> dict[str, Any]:
        """评委提交评分；唯一约束与状态条件共同防止并发重复提交。"""

        payload = {"actor_id": actor_id, "task_id": task_id,
                   "score_value": score_value, "comment": comment}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            task = self._task_row(connection, task_id)
            if task["reviewer_id"] != actor_id:
                raise PermissionDenied("只能为自己的任务提交评分")
            self._ensure_work_unsealed(connection, task["work_id"])
            if not isinstance(score_value, (int, float)) or isinstance(score_value, bool):
                raise ValidationError("score_value 必须是数字")
            score_value = float(score_value)
            if not 0 <= score_value <= 100:
                raise ValidationError("score_value 必须在 0 到 100 之间")
            comment = str(comment or "").strip()[:2000]

            def create():
                existing = connection.execute(
                    "SELECT score_id FROM review_scores WHERE task_id=?", (task_id,)
                ).fetchone()
                if existing:
                    raise ConflictError("该任务已经提交评分，评分不可覆盖")
                cursor = connection.execute(
                    "UPDATE review_tasks SET status='submitted' "
                    "WHERE task_id=? AND status='accepted'",
                    (task_id,),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("只有已领取且未提交的任务可以评分")
                score_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_scores(score_id,task_id,work_id,reviewer_id,score_value,"
                    "comment,submitted_at) VALUES(?,?,?,?,?,?,?)",
                    (score_id, task_id, task["work_id"], actor_id, score_value,
                     comment, self._now()),
                )
                self._audit(connection, actor_id=actor_id, action="review.score_submitted",
                            resource_type="review_score", resource_id=score_id,
                            detail={"task_id": task_id, "work_id": task["work_id"]})
                return "review_score", score_id, {"score_id": score_id, "task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.submit_score", payload=payload, create=create)

    def void_score(self, *, request_id: str, actor_id: str, task_id: str,
                   reason: str) -> dict[str, Any]:
        """秘书作废评分：评分事实保留（带作废标记），任务退出并提升候补。"""

        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            reason = self._text(reason, "reason", 500)
            task = self._task_row(connection, task_id)
            batch = self._batch_row(connection, task["batch_id"])
            if batch["status"] != "published":
                raise ConflictError("批次未发布")
            self._ensure_work_unsealed(connection, task["work_id"])
            score = connection.execute(
                "SELECT * FROM review_scores WHERE task_id=?", (task_id,)
            ).fetchone()
            if score is None:
                raise NotFoundError("该任务尚无评分")
            if score["voided_at"]:
                raise ConflictError("评分已经作废")

            def create():
                connection.execute(
                    "UPDATE review_scores SET voided_at=?, void_reason=? WHERE score_id=?",
                    (self._now(), reason, score["score_id"]),
                )
                connection.execute(
                    "UPDATE review_tasks SET status='voided', conflict_reason=? WHERE task_id=?",
                    (reason, task_id),
                )
                self._audit(connection, actor_id=actor_id, action="review.score_voided",
                            resource_type="review_score", resource_id=score["score_id"],
                            detail={"task_id": task_id, "work_id": task["work_id"],
                                    "reviewer_id": task["reviewer_id"], "reason": reason})
                new_task_id = self._promote_substitute(
                    connection, batch_id=task["batch_id"], work_id=task["work_id"],
                    vacated_task_id=task_id, cause="score_voided",
                )
                return ("review_score", score["score_id"],
                        {"score_id": score["score_id"], "replacement_task_id": new_task_id})

            return self._idempotent(connection, request_id=request_id,
                                    action="review.void_score", payload=payload, create=create)

    def seal_work(self, *, request_id: str, actor_id: str, batch_id: str, work_id: str) -> dict[str, Any]:
        """达到法定有效评分人数后封存作品；封存后任何任务与评分都不得再变。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "work_id": work_id}
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            batch = self._batch_row(connection, batch_id)
            if batch["status"] != "published":
                raise ConflictError("只有已发布批次可以封存")
            work = connection.execute("SELECT * FROM works WHERE work_id=?", (work_id,)).fetchone()
            if work is None:
                raise NotFoundError("作品不存在")
            if work["sealed"]:
                raise ConflictError("作品已经封存")

            valid_score_rows = connection.execute(
                "SELECT t.discipline_id AS discipline_id FROM review_scores s "
                "JOIN review_tasks t ON t.task_id=s.task_id "
                "WHERE t.batch_id=? AND s.work_id=? AND s.voided_at IS NULL",
                (batch_id, work_id),
            ).fetchall()
            valid_scores = len(valid_score_rows)
            if valid_scores < work["required_reviewer_count"]:
                raise ConflictError(
                    f"有效评分 {valid_scores} 份，未达到法定人数 {work['required_reviewer_count']}，不能封存"
                )
            required_disciplines = set(json.loads(work["required_disciplines_json"]))
            rule_body = json.loads(connection.execute(
                "SELECT body_json FROM rule_versions WHERE rule_version=?",
                (batch["rule_version"],),
            ).fetchone()["body_json"])
            covered = {row["discipline_id"] for row in valid_score_rows if row["discipline_id"]}
            missing = (sorted(required_disciplines - covered)
                       if rule_body.get("require_discipline_coverage", True) else [])
            if missing:
                raise ConflictError(
                    f"有效评分尚未覆盖专业 {missing}，不能封存；请等待专业替补完成评分"
                )

            def create():
                connection.execute("UPDATE works SET sealed=1 WHERE work_id=?", (work_id,))
                connection.execute(
                    "UPDATE review_tasks SET status='sealed' WHERE batch_id=? AND work_id=? "
                    "AND status IN ('assigned', 'accepted', 'submitted')",
                    (batch_id, work_id),
                )
                connection.execute(
                    "UPDATE review_substitutes SET status='sealed' WHERE batch_id=? AND work_id=? "
                    "AND status='queued'",
                    (batch_id, work_id),
                )
                self._audit(connection, actor_id=actor_id, action="review.work_sealed",
                            resource_type="work", resource_id=work_id,
                            detail={"batch_id": batch_id, "valid_scores": valid_scores,
                                    "required": work["required_reviewer_count"]})
                return "work", work_id, {"work_id": work_id, "sealed": True,
                                         "valid_scores": valid_scores}

            return self._idempotent(connection, request_id=request_id,
                                    action="review.seal_work", payload=payload, create=create)

    # ---------- 查询视图 ----------

    def list_my_tasks(self, actor_id: str) -> list[dict[str, Any]]:
        """评委视图：仅返回本人任务与作品匿名信息，绝不包含作者或机构身份。"""

        connection = self.database.connection
        actor = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if actor is None:
            raise NotFoundError("操作者不存在")
        if actor["role"] != "reviewer":
            raise PermissionDenied("只有评委可以查看自己的任务")
        if not actor["active"]:
            raise PermissionDenied("操作者已停用")
        rows = connection.execute(
            "SELECT t.*, w.anonymized_code, w.track_id, w.required_disciplines_json, w.material_ref "
            "FROM review_tasks t JOIN works w ON w.work_id=t.work_id "
            "JOIN review_batches b ON b.batch_id=t.batch_id "
            "WHERE t.reviewer_id=? AND b.status IN ('published','closed') "
            "ORDER BY t.batch_id, t.slot_index",
            (actor_id,),
        ).fetchall()
        items = []
        for row in rows:
            # 只有领取之后才开放匿名材料；回避/缺席/作废后立即关闭。
            material = row["material_ref"] if row["status"] in ("accepted", "submitted", "sealed") else None
            items.append({
                "task_id": row["task_id"], "batch_id": row["batch_id"],
                "anonymized_code": row["anonymized_code"], "track_id": row["track_id"],
                "required_disciplines": json.loads(row["required_disciplines_json"]),
                "status": row["status"], "material_ref": material,
            })
        return items

    def get_my_task(self, actor_id: str, task_id: str) -> dict[str, Any]:
        connection = self.database.connection
        actor = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if actor is None:
            raise NotFoundError("操作者不存在")
        if actor["role"] != "reviewer":
            raise PermissionDenied("只有评委可以查看任务")
        if not actor["active"]:
            raise PermissionDenied("操作者已停用")
        row = connection.execute(
            "SELECT t.*, w.anonymized_code, w.track_id, w.required_disciplines_json, w.material_ref, "
            "b.status AS batch_status "
            "FROM review_tasks t JOIN works w ON w.work_id=t.work_id "
            "JOIN review_batches b ON b.batch_id=t.batch_id WHERE t.task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        if row["reviewer_id"] != actor_id or row["batch_status"] not in ("published", "closed"):
            # 对非本人任务或未发布任务统一按不存在处理，避免泄露任何分配信息。
            raise NotFoundError("任务不存在")
        material = row["material_ref"] if row["status"] in ("accepted", "submitted", "sealed") else None
        return {
            "task_id": row["task_id"], "batch_id": row["batch_id"],
            "anonymized_code": row["anonymized_code"], "track_id": row["track_id"],
            "required_disciplines": json.loads(row["required_disciplines_json"]),
            "status": row["status"], "material_ref": material,
        }

    def list_batches(self, actor_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        actor = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if actor is None:
            raise NotFoundError("操作者不存在")
        if actor["role"] not in ("admin", "operator", "auditor"):
            raise PermissionDenied("当前角色不能查看批次")
        return [
            {"batch_id": row["batch_id"], "rule_version": row["rule_version"],
             "status": row["status"], "snapshot_hash": row["relationship_snapshot_hash"],
             "created_at": row["created_at"], "published_at": row["published_at"]}
            for row in connection.execute(
                "SELECT * FROM review_batches ORDER BY created_at, batch_id")
        ]

    def get_batch_audit(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """纪检复核：关系快照、规则版本与摘要、回避原因、替补顺序、完整状态轨迹。"""

        connection = self.database.connection
        actor = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if actor is None:
            raise NotFoundError("操作者不存在")
        if actor["role"] != "auditor":
            raise PermissionDenied("只有纪检人员可以复核分配依据")
        batch = connection.execute("SELECT * FROM review_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise NotFoundError("批次不存在")
        rule = self.get_rule_version(batch["rule_version"])
        snapshot = json.loads(batch["relationship_snapshot_json"])
        recomputed = digest(snapshot)
        tasks = [
            {"task_id": row["task_id"], "work_id": row["work_id"], "reviewer_id": row["reviewer_id"],
             "discipline_id": row["discipline_id"], "slot_index": row["slot_index"],
             "status": row["status"], "reason": row["conflict_reason"]}
            for row in connection.execute(
                "SELECT * FROM review_tasks WHERE batch_id=? ORDER BY work_id, slot_index, created_at",
                (batch_id,))
        ]
        substitutes = [
            {"substitute_id": row["substitute_id"], "work_id": row["work_id"],
             "reviewer_id": row["reviewer_id"], "rank_order": row["rank_order"],
             "reason": row["reason"], "status": row["status"]}
            for row in connection.execute(
                "SELECT * FROM review_substitutes WHERE batch_id=? "
                "ORDER BY work_id, rank_order", (batch_id,))
        ]
        scores = [
            {"score_id": row["score_id"], "task_id": row["task_id"], "work_id": row["work_id"],
             "reviewer_id": row["reviewer_id"], "score_value": row["score_value"],
             "submitted_at": row["submitted_at"], "voided_at": row["voided_at"],
             "void_reason": row["void_reason"]}
            for row in connection.execute(
                "SELECT * FROM review_scores WHERE work_id IN "
                "(SELECT work_id FROM review_tasks WHERE batch_id=?) ORDER BY submitted_at",
                (batch_id,))
        ]
        return {
            "batch_id": batch_id, "status": batch["status"],
            "created_by": batch["created_by"], "created_at": batch["created_at"],
            "published_at": batch["published_at"],
            "rule_version": rule.rule_version,
            "rule_body_hash": rule.body_hash,
            "rule_body_hash_verified": rule.body_hash == digest(rule.body),
            "rule_body": rule.body,
            "relationship_snapshot_hash": batch["relationship_snapshot_hash"],
            "relationship_snapshot_hash_verified": recomputed == batch["relationship_snapshot_hash"],
            "relationship_snapshot": snapshot,
            "allocation": json.loads(batch["explanation_json"]),
            "tasks": tasks, "substitutes": substitutes, "scores": scores,
        }
