"""实现盲审分配、利益回避、替补封存与纪检复核能力。

角色映射复用基础服务的操作者角色：

- ``operator``：评审秘书，登记资料、发布批次、处理例外，但不能改分；
- ``reviewer``：评委，只能看到自己的匿名任务；
- ``auditor``：纪检人员，复核关系快照、规则版本、回避原因与替补顺序。

所有写入都在 SQLite 短事务内完成，关键状态迁移使用条件 UPDATE，
配合 ``BEGIN IMMEDIATE`` 防止并发重复领取；任务、分数与事件均为只增记录，
进程重启后已接受任务和候补顺序原样保留。
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database, register_schema


REVIEW_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS review_rule_versions (
    rule_version_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_tracks (
    track_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_reviewer_profiles (
    reviewer_id TEXT PRIMARY KEY,
    expertise_json TEXT NOT NULL,
    track_ids_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_works (
    work_id TEXT PRIMARY KEY,
    anon_code TEXT NOT NULL UNIQUE,
    track_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    title TEXT NOT NULL,
    material_uri TEXT NOT NULL,
    required_expertise_json TEXT NOT NULL,
    team_json TEXT NOT NULL,
    identity_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_relations (
    relation_id TEXT PRIMARY KEY,
    reviewer_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    relation_kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_relations_reviewer ON review_relations(reviewer_id);
CREATE TABLE IF NOT EXISTS review_relation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    relation_count INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_batches (
    batch_id TEXT PRIMARY KEY,
    rule_version_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'planned', 'published', 'sealed')),
    generated_snapshot_id TEXT,
    published_at TEXT,
    sealed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_batch_works (
    batch_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'sealed')),
    sealed_at TEXT,
    PRIMARY KEY(batch_id, work_id)
);
CREATE TABLE IF NOT EXISTS review_work_plans (
    batch_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    rule_version_id TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    exclusions_json TEXT NOT NULL,
    substitutes_json TEXT NOT NULL,
    uncovered_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY(batch_id, work_id)
);
CREATE TABLE IF NOT EXISTS review_tasks (
    task_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    slot_type TEXT NOT NULL CHECK(slot_type IN ('primary', 'substitute')),
    position INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'offered', 'waitlist', 'claimed', 'scored', 'recused', 'absent', 'invalidated'
    )),
    reason TEXT,
    claimed_at TEXT,
    scored_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, work_id, reviewer_id)
);
CREATE INDEX IF NOT EXISTS idx_review_tasks_reviewer ON review_tasks(reviewer_id);
CREATE INDEX IF NOT EXISTS idx_review_tasks_work ON review_tasks(batch_id, work_id, status);
CREATE TABLE IF NOT EXISTS review_scores (
    score_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    batch_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    total REAL NOT NULL CHECK(total >= 0 AND total <= 100),
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_task_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    promoted_task_id TEXT,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_task_events_task ON review_task_events(task_id, occurred_at);
CREATE TABLE IF NOT EXISTS review_exceptions (
    exception_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    work_id TEXT NOT NULL,
    reviewer_id TEXT,
    exception_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_batch_absences (
    batch_id TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    marked_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(batch_id, reviewer_id)
);
"""
register_schema(REVIEW_SCHEMA)

# 关系声明允许的类型及其主体类别：与机构相关、与具体人员相关、与协会相关。
RELATION_SUBJECTS = {
    "employment": "organization",
    "advisory": "organization",
    "collaboration": "person",
    "kinship": "person",
    "teacher_student": "person",
    "association": "association",
}
RELATION_LABELS = {
    "employment": "任职",
    "advisory": "顾问",
    "collaboration": "合作",
    "kinship": "亲属",
    "teacher_student": "师生",
    "association": "同属协会",
}

TASK_OPEN_STATUSES = ("offered", "claimed")
TASK_END_STATUSES = ("recused", "absent", "invalidated")
# 材料只在评委领取之后开放：offered 仅表示任务已派发、尚未接受。
MATERIAL_VISIBLE_STATUSES = ("claimed", "scored")
SECRETARY_ROLES = ("admin", "operator")


class ReviewService:
    """提供盲审后台的登记、分配、评审流转与复核接口。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        # 触发当前线程连接的惰性建表；其他线程的连接会在首次使用时自动建表。
        self.database.connection  # noqa: B018

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _reason_text(self, value: Any, field: str = "reason", limit: int = 500) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValidationError(f"{field} 不能为空")
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def _string_list(self, value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
        if not isinstance(value, list) or (not value and not allow_empty):
            raise ValidationError(f"{field} 必须是{'非空' if not allow_empty else ''}字符串数组")
        result = []
        for item in value:
            item = str(item).strip()
            if not item:
                raise ValidationError(f"{field} 不能包含空值")
            result.append(item)
        if len(set(result)) != len(result):
            raise ValidationError(f"{field} 不能包含重复值")
        return result

    def _replay_if_present(self, connection, *, request_id: str, action: str,
                           payload: dict[str, Any]) -> WriteReceipt | None:
        """在业务状态校验之前识别幂等重放，保证状态迁移接口可安全重试。"""

        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (str(request_id).strip(),)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(str(request_id).strip(), row["resource_type"],
                            row["resource_id"], True)

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = str(request_id).strip()
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _add_task_event(self, connection, *, task_row, to_status: str, actor_id: str,
                        reason: str | None = None, promoted_task_id: str | None = None,
                        detail: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO review_task_events(event_id,task_id,batch_id,work_id,from_status,"
            "to_status,reason,promoted_task_id,actor_id,occurred_at,detail_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, task_row["task_id"], task_row["batch_id"], task_row["work_id"],
             task_row["status"], to_status, reason, promoted_task_id, actor_id, self._now(),
             canonical_json(detail or {})),
        )

    def _ensure_work_open(self, connection, batch_id: str, work_id: str):
        row = connection.execute(
            "SELECT status FROM review_batch_works WHERE batch_id=? AND work_id=?",
            (batch_id, work_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("批次中没有该作品")
        if row["status"] == "sealed":
            raise ConflictError("作品评审已经封存，不能再调整任务或分数")
        return row

    def _promote(self, connection, batch_id: str, work_id: str, actor_id: str) -> str | None:
        """按候补位置把第一位候补评委提升为正式任务，返回任务编号。"""

        candidate = connection.execute(
            "SELECT * FROM review_tasks WHERE batch_id=? AND work_id=? AND status='waitlist' "
            "ORDER BY position LIMIT 1",
            (batch_id, work_id),
        ).fetchone()
        if candidate is None:
            return None
        updated = connection.execute(
            "UPDATE review_tasks SET status='offered' WHERE task_id=? AND status='waitlist'",
            (candidate["task_id"],),
        )
        if updated.rowcount == 0:  # 极端并发下被其他事务抢先
            return None
        self._add_task_event(connection, task_row=candidate, to_status="offered",
                             actor_id=actor_id, reason="按候补顺序递补",
                             detail={"promoted_from_position": candidate["position"]})
        return candidate["task_id"]

    # ------------------------------------------------------------------
    # 基础登记：规则版本、赛道、评委、作品、关系声明
    # ------------------------------------------------------------------

    def register_rule_version(self, *, request_id: str, actor_id: str,
                              rule_version_id: str, rules: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "rule_version_id": rule_version_id, "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            rule_version_id = str(rule_version_id).strip()
            normalized = self._normalize_rules(rules)
            payload_hash = digest(normalized)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT rule_version_id FROM review_rule_versions WHERE payload_hash=?",
                    (payload_hash,),
                ).fetchone()
                if existing is not None:
                    if existing["rule_version_id"] != rule_version_id:
                        raise ConflictError("相同规则内容已经以其他规则版本编号登记")
                    return "review_rule_version", rule_version_id, {"rule_version_id": rule_version_id}
                try:
                    connection.execute(
                        "INSERT INTO review_rule_versions(rule_version_id,payload_json,payload_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?)",
                        (rule_version_id, canonical_json(normalized), payload_hash,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("规则版本编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.rule_version_registered",
                             resource_type="review_rule_version", resource_id=rule_version_id,
                             detail={"payload_hash": payload_hash}, occurred_at=self._now())
                return "review_rule_version", rule_version_id, {
                    "rule_version_id": rule_version_id, "payload_hash": payload_hash,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="register_rule_version", payload=payload, create=create)

    def _normalize_rules(self, rules: Any) -> dict[str, Any]:
        if not isinstance(rules, dict):
            raise ValidationError("rules 必须是对象")
        independent = rules.get("independent_reviewers")
        if not isinstance(independent, int) or independent < 1:
            raise ValidationError("independent_reviewers 必须是不小于 1 的整数")
        quorum = rules.get("quorum", independent)
        if not isinstance(quorum, int) or quorum < 1 or quorum > independent:
            raise ValidationError("quorum 必须是 1 到 independent_reviewers 之间的整数")
        default_cap = rules.get("default_track_load_cap", 5)
        if not isinstance(default_cap, int) or default_cap < 1:
            raise ValidationError("default_track_load_cap 必须是不小于 1 的整数")
        caps = rules.get("track_load_caps", {})
        if not isinstance(caps, dict) or any(
            not isinstance(v, int) or v < 1 for v in caps.values()
        ):
            raise ValidationError("track_load_caps 必须是赛道到正整数的映射")
        substitute_count = rules.get("substitute_count", 2)
        if not isinstance(substitute_count, int) or substitute_count < 0:
            raise ValidationError("substitute_count 必须是非负整数")
        return {
            "independent_reviewers": independent,
            "quorum": quorum,
            "default_track_load_cap": default_cap,
            "track_load_caps": {str(k): int(v) for k, v in caps.items()},
            "substitute_count": substitute_count,
        }

    def _load_rules(self, connection, rule_version_id: str) -> tuple[dict[str, Any], str]:
        row = connection.execute(
            "SELECT * FROM review_rule_versions WHERE rule_version_id=?", (rule_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        import json as _json
        return _json.loads(row["payload_json"]), row["payload_hash"]

    def register_track(self, *, request_id: str, actor_id: str,
                       track_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "track_id": track_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            track_id = str(track_id).strip()
            name = self._reason_text(name, "name", 200)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_tracks(track_id,name,created_by,created_at) "
                        "VALUES(?,?,?,?)",
                        (track_id, name, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("赛道编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.track_registered",
                             resource_type="review_track", resource_id=track_id,
                             detail={"name": name}, occurred_at=self._now())
                return "review_track", track_id, {"track_id": track_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_track", payload=payload, create=create)

    def register_reviewer(self, *, request_id: str, actor_id: str, reviewer_id: str,
                          expertise: list[str], track_ids: list[str]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "reviewer_id": reviewer_id,
                   "expertise": expertise, "track_ids": track_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            target = self._actor(connection, str(reviewer_id).strip())
            if target.role != "reviewer":
                raise ValidationError("评委档案只能绑定 reviewer 角色的操作者")
            expertise = self._string_list(expertise, "expertise", allow_empty=True)
            track_ids = self._string_list(track_ids, "track_ids")
            for track_id in track_ids:
                if connection.execute("SELECT 1 FROM review_tracks WHERE track_id=?",
                                      (track_id,)).fetchone() is None:
                    raise NotFoundError(f"赛道不存在: {track_id}")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_reviewer_profiles(reviewer_id,expertise_json,"
                        "track_ids_json,active,created_by,created_at) VALUES(?,?,?,1,?,?)",
                        (target.actor_id, canonical_json(expertise), canonical_json(track_ids),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("评委档案已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.reviewer_registered",
                             resource_type="review_reviewer", resource_id=target.actor_id,
                             detail={"expertise": expertise, "track_ids": track_ids},
                             occurred_at=self._now())
                return "review_reviewer", target.actor_id, {"reviewer_id": target.actor_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_reviewer", payload=payload, create=create)

    def register_work(self, *, request_id: str, actor_id: str, work_id: str, anon_code: str,
                      track_id: str, organization_id: str, title: str, material_uri: str,
                      required_expertise: list[str], team: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "work_id": work_id, "anon_code": anon_code,
                   "track_id": track_id, "organization_id": organization_id, "title": title,
                   "material_uri": material_uri, "required_expertise": required_expertise,
                   "team": team}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            work_id = str(work_id).strip()
            anon_code = self._reason_text(anon_code, "anon_code", 100)
            title = self._reason_text(title, "title", 200)
            material_uri = self._reason_text(material_uri, "material_uri", 500)
            if connection.execute("SELECT 1 FROM review_tracks WHERE track_id=?",
                                  (track_id,)).fetchone() is None:
                raise NotFoundError("赛道不存在")
            required_expertise = self._string_list(required_expertise, "required_expertise",
                                                   allow_empty=True)
            team_normalized = self._normalize_team(team)
            identity_hash = digest({
                "work_id": work_id, "organization_id": organization_id, "team": team_normalized,
            })

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_works(work_id,anon_code,track_id,organization_id,"
                        "title,material_uri,required_expertise_json,team_json,identity_hash,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (work_id, anon_code, track_id, str(organization_id).strip(), title,
                         material_uri, canonical_json(required_expertise),
                         canonical_json(team_normalized), identity_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("作品编号或匿名编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.work_registered",
                             resource_type="review_work", resource_id=work_id,
                             detail={"anon_code": anon_code, "track_id": track_id,
                                     "identity_hash": identity_hash},
                             occurred_at=self._now())
                return "review_work", work_id, {"work_id": work_id, "anon_code": anon_code}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_work", payload=payload, create=create)

    def _normalize_team(self, team: Any) -> list[dict[str, Any]]:
        if not isinstance(team, list) or not team:
            raise ValidationError("team 必须是非空数组")
        normalized = []
        seen = set()
        for member in team:
            if not isinstance(member, dict):
                raise ValidationError("team 成员必须是对象")
            person_key = str(member.get("person_key", "")).strip()
            if not person_key:
                raise ValidationError("team 成员缺少 person_key")
            if person_key in seen:
                raise ValidationError(f"team 中成员重复: {person_key}")
            seen.add(person_key)
            associations = member.get("associations", [])
            if associations is None:
                associations = []
            if not isinstance(associations, list):
                raise ValidationError("team 成员的 associations 必须是数组")
            associations = sorted({str(a).strip() for a in associations if str(a).strip()})
            normalized.append({"person_key": person_key, "associations": associations})
        normalized.sort(key=lambda item: item["person_key"])
        return normalized

    def declare_relation(self, *, request_id: str, actor_id: str, relation_id: str,
                         reviewer_id: str, subject_kind: str, target_key: str,
                         relation_kind: str, detail: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "relation_id": relation_id, "reviewer_id": reviewer_id,
                   "subject_kind": subject_kind, "target_key": target_key,
                   "relation_kind": relation_kind, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            reviewer_id = str(reviewer_id).strip()
            if actor.role == "reviewer":
                if actor.actor_id != reviewer_id:
                    raise PermissionDenied("评委只能申报本人的关系声明")
            else:
                self._require(actor, *SECRETARY_ROLES)
            if connection.execute(
                "SELECT 1 FROM review_reviewer_profiles WHERE reviewer_id=?", (reviewer_id,)
            ).fetchone() is None:
                raise NotFoundError("评委档案不存在")
            relation_kind = str(relation_kind).strip()
            if relation_kind not in RELATION_SUBJECTS:
                raise ValidationError(f"relation_kind 必须是 {sorted(RELATION_SUBJECTS)} 之一")
            subject_kind = str(subject_kind).strip()
            if subject_kind != RELATION_SUBJECTS[relation_kind]:
                raise ValidationError(
                    f"{relation_kind} 关系的 subject_kind 必须是 {RELATION_SUBJECTS[relation_kind]}"
                )
            target_key = self._reason_text(target_key, "target_key", 200)
            detail = str(detail or "").strip()[:500]

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_relations(relation_id,reviewer_id,subject_kind,"
                        "target_key,relation_kind,detail,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (str(relation_id).strip(), reviewer_id, subject_kind, target_key,
                         relation_kind, detail, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("关系声明编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.relation_declared",
                             resource_type="review_relation", resource_id=str(relation_id).strip(),
                             detail={"reviewer_id": reviewer_id, "relation_kind": relation_kind,
                                     "subject_kind": subject_kind, "target_key": target_key},
                             occurred_at=self._now())
                return "review_relation", str(relation_id).strip(), {
                    "relation_id": str(relation_id).strip(),
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_relation", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 批次、方案生成与发布
    # ------------------------------------------------------------------

    def create_batch(self, *, request_id: str, actor_id: str,
                     batch_id: str, rule_version_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id,
                   "rule_version_id": rule_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            self._load_rules(connection, str(rule_version_id).strip())

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_batches(batch_id,rule_version_id,status,created_by,"
                        "created_at) VALUES(?,?, 'draft', ?,?)",
                        (str(batch_id).strip(), str(rule_version_id).strip(),
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="review.batch_created",
                             resource_type="review_batch", resource_id=str(batch_id).strip(),
                             detail={"rule_version_id": str(rule_version_id).strip()},
                             occurred_at=self._now())
                return "review_batch", str(batch_id).strip(), {"batch_id": str(batch_id).strip()}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_batch", payload=payload, create=create)

    def add_work_to_batch(self, *, request_id: str, actor_id: str,
                          batch_id: str, work_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "work_id": work_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            batch = self._batch(connection, batch_id)
            if batch["status"] != "draft":
                raise ConflictError("批次已经生成方案，不能再增减作品")
            work = connection.execute("SELECT track_id FROM review_works WHERE work_id=?",
                                      (work_id,)).fetchone()
            if work is None:
                raise NotFoundError("作品不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO review_batch_works(batch_id,work_id,status) VALUES(?,?,'active')",
                        (batch["batch_id"], work_id),
                    )
                except Exception as exc:
                    raise ConflictError("作品已经加入该批次") from exc
                append_event(connection, actor_id=actor_id, action="review.work_added_to_batch",
                             resource_type="review_batch", resource_id=batch["batch_id"],
                             detail={"work_id": work_id}, occurred_at=self._now())
                return "review_batch_work", f"{batch['batch_id']}:{work_id}", {
                    "batch_id": batch["batch_id"], "work_id": work_id,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="add_work_to_batch", payload=payload, create=create)

    def _batch(self, connection, batch_id: str):
        row = connection.execute("SELECT * FROM review_batches WHERE batch_id=?",
                                 (str(batch_id).strip(),)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return row

    def grant_exception(self, *, request_id: str, actor_id: str, batch_id: str, work_id: str,
                        exception_type: str, reason: str, reviewer_id: str | None = None) -> WriteReceipt:
        """秘书在发布前登记例外：只能豁免负载上限或专业覆盖，绝不豁免利益冲突。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "work_id": work_id,
                   "exception_type": exception_type, "reason": reason,
                   "reviewer_id": reviewer_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            batch = self._batch(connection, batch_id)
            if batch["status"] != "draft":
                raise ConflictError("例外只能在批次发布前登记")
            self._ensure_work_open(connection, batch["batch_id"], work_id)
            exception_type = str(exception_type).strip()
            if exception_type not in ("load_cap_waiver", "coverage_waiver"):
                raise ValidationError("exception_type 只能是 load_cap_waiver 或 coverage_waiver")
            reason = self._reason_text(reason)
            if exception_type == "load_cap_waiver":
                reviewer_id = str(reviewer_id or "").strip()
                if not reviewer_id:
                    raise ValidationError("load_cap_waiver 必须指定 reviewer_id")
                if connection.execute(
                    "SELECT 1 FROM review_reviewer_profiles WHERE reviewer_id=?", (reviewer_id,)
                ).fetchone() is None:
                    raise NotFoundError("评委档案不存在")
            else:
                reviewer_id = None
            exception_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO review_exceptions(exception_id,batch_id,work_id,reviewer_id,"
                    "exception_type,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (exception_id, batch["batch_id"], work_id, reviewer_id, exception_type,
                     reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="review.exception_granted",
                             resource_type="review_batch", resource_id=batch["batch_id"],
                             detail={"work_id": work_id, "exception_type": exception_type,
                                     "reviewer_id": reviewer_id, "reason": reason},
                             occurred_at=self._now())
                return "review_exception", exception_id, {"exception_id": exception_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="grant_exception", payload=payload, create=create)

    def _load_work_view(self, connection, work_id: str) -> dict[str, Any]:
        import json as _json
        row = connection.execute("SELECT * FROM review_works WHERE work_id=?",
                                 (work_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"作品不存在: {work_id}")
        team = _json.loads(row["team_json"])
        return {
            "work_id": row["work_id"],
            "track_id": row["track_id"],
            "organization_id": row["organization_id"],
            "required_expertise": _json.loads(row["required_expertise_json"]),
            "team_keys": {member["person_key"] for member in team},
            "team_associations": {
                association for member in team for association in member["associations"]
            },
            "identity_hash": row["identity_hash"],
        }

    def _conflict_reasons(self, reviewer_id: str, work: dict[str, Any],
                          relations: list[dict[str, Any]]) -> list[dict[str, str]]:
        """返回评委与作品之间的全部利益冲突原因；空列表表示无冲突。"""

        reasons: list[dict[str, str]] = []
        if reviewer_id in work["team_keys"]:
            reasons.append({"code": "conflict_self", "relation_id": "",
                            "detail": "评委本人是作品团队成员"})
        for relation in relations:
            if relation["reviewer_id"] != reviewer_id:
                continue
            kind = relation["relation_kind"]
            target = relation["target_key"]
            label = RELATION_LABELS[kind]
            if kind in ("employment", "advisory") and target == work["organization_id"]:
                reasons.append({
                    "code": f"conflict_{kind}",
                    "relation_id": relation["relation_id"],
                    "detail": f"评委与参赛机构存在{label}关系: {target}",
                })
            elif kind in ("collaboration", "kinship", "teacher_student") and target in work["team_keys"]:
                reasons.append({
                    "code": f"conflict_{kind}",
                    "relation_id": relation["relation_id"],
                    "detail": f"评委与团队成员 {target} 存在{label}关系",
                })
            elif kind == "association" and target in work["team_associations"]:
                reasons.append({
                    "code": "conflict_association",
                    "relation_id": relation["relation_id"],
                    "detail": f"评委与团队成员同属协会: {target}",
                })
        return reasons

    def generate_plan(self, *, request_id: str, actor_id: str, batch_id: str) -> WriteReceipt:
        """依据当前关系快照生成可解释分配方案，约束无法满足时整体失败且不落库。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="generate_plan", payload=payload)
            if replay is not None:
                return replay
            batch = self._batch(connection, batch_id)
            if batch["status"] != "draft":
                raise ConflictError("方案只能在草稿批次上生成一次")
            import json as _json

            rules, _ = self._load_rules(connection, batch["rule_version_id"])
            work_rows = connection.execute(
                "SELECT work_id FROM review_batch_works WHERE batch_id=? ORDER BY work_id",
                (batch["batch_id"],),
            ).fetchall()
            if not work_rows:
                raise ValidationError("批次中还没有作品")
            works = [self._load_work_view(connection, row["work_id"]) for row in work_rows]

            profiles = {}
            for row in connection.execute("SELECT * FROM review_reviewer_profiles"):
                profiles[row["reviewer_id"]] = {
                    "reviewer_id": row["reviewer_id"],
                    "active": bool(row["active"]),
                    "expertise": set(_json.loads(row["expertise_json"])),
                    "track_ids": set(_json.loads(row["track_ids_json"])),
                }
            relations = [dict(row) for row in connection.execute(
                "SELECT relation_id,reviewer_id,subject_kind,target_key,relation_kind,detail "
                "FROM review_relations ORDER BY relation_id"
            )]
            relations_by_reviewer: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for relation in relations:
                relations_by_reviewer[relation["reviewer_id"]].append(relation)

            waivers = {
                "coverage": set(),
                "load": defaultdict(set),  # work_id -> reviewer_ids
            }
            for row in connection.execute(
                "SELECT work_id,reviewer_id,exception_type FROM review_exceptions WHERE batch_id=?",
                (batch["batch_id"],),
            ):
                if row["exception_type"] == "coverage_waiver":
                    waivers["coverage"].add(row["work_id"])
                else:
                    waivers["load"][row["work_id"]].add(row["reviewer_id"])

            # 关系快照：一旦生成方案，当时的全部输入就被冻结并可复算。
            snapshot_content = {
                "scope": "review_batch",
                "batch_id": batch["batch_id"],
                "rule_version_id": batch["rule_version_id"],
                "relations": [{key: relation[key] for key in
                               ("relation_id", "reviewer_id", "subject_kind",
                                "target_key", "relation_kind", "detail")}
                              for relation in relations],
                "reviewers": sorted((
                    {"reviewer_id": reviewer_id,
                     "active": profile["active"],
                     "expertise": sorted(profile["expertise"]),
                     "track_ids": sorted(profile["track_ids"])}
                    for reviewer_id, profile in profiles.items()
                ), key=lambda item: item["reviewer_id"]),
                "works": sorted((
                    {"work_id": work["work_id"], "track_id": work["track_id"],
                     "organization_id": work["organization_id"],
                     "team": sorted(
                         ({"person_key": key} for key in work["team_keys"]),
                         key=lambda item: item["person_key"],
                     ),
                     "team_associations": sorted(work["team_associations"]),
                     "required_expertise": sorted(work["required_expertise"]),
                     "identity_hash": work["identity_hash"]}
                    for work in works
                ), key=lambda item: item["work_id"]),
            }
            snapshot_hash = digest(snapshot_content)
            snapshot_row = connection.execute(
                "SELECT snapshot_id FROM review_relation_snapshots WHERE content_hash=?",
                (snapshot_hash,),
            ).fetchone()
            if snapshot_row is None:
                snapshot_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_relation_snapshots(snapshot_id,content_json,content_hash,"
                    "relation_count,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (snapshot_id, canonical_json(snapshot_content), snapshot_hash,
                     len(relations), actor_id, self._now()),
                )
            else:
                snapshot_id = snapshot_row["snapshot_id"]

            required_count = rules["independent_reviewers"]
            substitute_count = rules["substitute_count"]
            load: dict[str, int] = defaultdict(int)
            plan_rows: list[dict[str, Any]] = []
            task_specs: list[tuple[str, str, str, int]] = []
            failures: list[dict[str, Any]] = []

            for work in works:
                cap = rules["track_load_caps"].get(
                    work["track_id"], rules["default_track_load_cap"]
                )
                candidates: list[str] = []
                exclusions: list[dict[str, Any]] = []
                for reviewer_id in sorted(profiles):
                    profile = profiles[reviewer_id]
                    if not profile["active"]:
                        exclusions.append({"reviewer_id": reviewer_id,
                                           "reason_code": "reviewer_inactive",
                                           "detail": "评委账号已停用"})
                        continue
                    if work["track_id"] not in profile["track_ids"]:
                        exclusions.append({"reviewer_id": reviewer_id,
                                           "reason_code": "track_ineligible",
                                           "detail": f"评委不具备赛道 {work['track_id']} 评审资格"})
                        continue
                    conflict = self._conflict_reasons(
                        reviewer_id, work, relations_by_reviewer[reviewer_id]
                    )
                    if conflict:
                        exclusions.append({"reviewer_id": reviewer_id,
                                           "reason_code": "conflict",
                                           "detail": conflict})
                        continue
                    if load[reviewer_id] >= cap and reviewer_id not in waivers["load"][work["work_id"]]:
                        exclusions.append({"reviewer_id": reviewer_id,
                                           "reason_code": "track_load_cap",
                                           "detail": f"赛道负载已达上限 {cap}"})
                        continue
                    candidates.append(reviewer_id)

                def ordering(reviewer_id: str) -> tuple[int, str]:
                    return (load[reviewer_id], reviewer_id)

                chosen: list[tuple[str, str]] = []
                chosen_set: set[str] = set()
                uncovered: list[str] = []
                for tag in sorted(work["required_expertise"]):
                    if any(tag in profiles[reviewer_id]["expertise"]
                           for reviewer_id in chosen_set):
                        continue
                    pool = [reviewer_id for reviewer_id in candidates
                            if reviewer_id not in chosen_set
                            and tag in profiles[reviewer_id]["expertise"]]
                    if pool:
                        picked = min(pool, key=ordering)
                        chosen.append((picked, f"coverage:{tag}"))
                        chosen_set.add(picked)
                    else:
                        uncovered.append(tag)
                if uncovered and work["work_id"] not in waivers["coverage"]:
                    failures.append({"work_id": work["work_id"], "issue": "uncovered_expertise",
                                     "required_expertise": sorted(work["required_expertise"]),
                                     "missing": uncovered})
                    continue

                for reviewer_id in sorted(candidates, key=ordering):
                    if len(chosen) >= required_count:
                        break
                    if reviewer_id in chosen_set:
                        continue
                    chosen.append((reviewer_id, "load_balance"))
                    chosen_set.add(reviewer_id)

                if len(chosen) < required_count:
                    failures.append({
                        "work_id": work["work_id"], "issue": "insufficient_independent_reviewers",
                        "required": required_count, "available": len(candidates),
                        "conflict_or_excluded": len(exclusions),
                    })
                    continue

                substitutes = [
                    reviewer_id for reviewer_id in sorted(candidates, key=ordering)
                    if reviewer_id not in chosen_set
                ][:substitute_count]

                selection = []
                for position, (reviewer_id, reason) in enumerate(chosen):
                    selection.append({"reviewer_id": reviewer_id, "slot": "primary",
                                      "position": position, "reason": reason})
                    task_specs.append((work["work_id"], reviewer_id, "primary", position))
                    load[reviewer_id] += 1
                for offset, reviewer_id in enumerate(substitutes):
                    position = required_count + offset
                    task_specs.append((work["work_id"], reviewer_id, "substitute", position))
                plan_rows.append({
                    "work_id": work["work_id"],
                    "selection": selection,
                    "exclusions": exclusions,
                    "substitutes": [
                        {"reviewer_id": reviewer_id,
                         "position": required_count + offset}
                        for offset, reviewer_id in enumerate(substitutes)
                    ],
                    "uncovered": uncovered,
                })

            if failures:
                raise ValidationError("存在无法满足约束的作品: " + canonical_json(
                    sorted(failures, key=lambda item: item["work_id"])
                ))

            generated_at = self._now()
            for plan in plan_rows:
                connection.execute(
                    "INSERT INTO review_work_plans(batch_id,work_id,snapshot_id,rule_version_id,"
                    "selection_json,exclusions_json,substitutes_json,uncovered_json,generated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (batch["batch_id"], plan["work_id"], snapshot_id, batch["rule_version_id"],
                     canonical_json(plan["selection"]), canonical_json(plan["exclusions"]),
                     canonical_json(plan["substitutes"]), canonical_json(plan["uncovered"]),
                     generated_at),
                )
            for work_id, reviewer_id, slot_type, position in task_specs:
                task_id = uuid.uuid4().hex
                status = "offered" if slot_type == "primary" else "waitlist"
                connection.execute(
                    "INSERT INTO review_tasks(task_id,batch_id,work_id,reviewer_id,slot_type,"
                    "position,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (task_id, batch["batch_id"], work_id, reviewer_id, slot_type, position,
                     status, generated_at),
                )
            connection.execute(
                "UPDATE review_batches SET status='planned', generated_snapshot_id=? "
                "WHERE batch_id=?",
                (snapshot_id, batch["batch_id"]),
            )
            plan_summary = [{
                "work_id": plan["work_id"],
                "primary_reviewers": [item["reviewer_id"] for item in plan["selection"]],
                "substitutes": [item["reviewer_id"] for item in plan["substitutes"]],
                "excluded": [{"reviewer_id": item["reviewer_id"],
                              "reason_code": item["reason_code"]}
                             for item in plan["exclusions"]],
                "uncovered": plan["uncovered"],
            } for plan in plan_rows]
            append_event(connection, actor_id=actor_id, action="review.plan_generated",
                         resource_type="review_batch", resource_id=batch["batch_id"],
                         detail={"snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash,
                                 "rule_version_id": batch["rule_version_id"],
                                 "works": plan_summary},
                         occurred_at=generated_at)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_batch", batch["batch_id"], {
                    "batch_id": batch["batch_id"], "status": "planned",
                    "snapshot_id": snapshot_id, "snapshot_hash": snapshot_hash,
                    "works": plan_summary,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="generate_plan", payload=payload, create=create)

    def publish_batch(self, *, request_id: str, actor_id: str, batch_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="publish_batch", payload=payload)
            if replay is not None:
                return replay
            batch = self._batch(connection, batch_id)
            if batch["status"] != "planned":
                raise ConflictError("只有已生成方案的批次才能发布")
            now = self._now()
            connection.execute(
                "UPDATE review_batches SET status='published', published_at=? WHERE batch_id=?",
                (now, batch["batch_id"]),
            )
            append_event(connection, actor_id=actor_id, action="review.batch_published",
                         resource_type="review_batch", resource_id=batch["batch_id"],
                         detail={"rule_version_id": batch["rule_version_id"],
                                 "snapshot_id": batch["generated_snapshot_id"]},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_batch", batch["batch_id"], {
                    "batch_id": batch["batch_id"], "status": "published",
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_batch", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 评委：领取、查看材料、提交分数、自行回避
    # ------------------------------------------------------------------

    def list_my_tasks(self, actor_id: str) -> dict[str, Any]:
        """评委视角只含匿名编号与自身任务，绝不返回机构、团队或真实题名。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            items = []
            rows = connection.execute(
                "SELECT t.task_id,t.batch_id,t.work_id,t.slot_type,t.position,t.status,"
                "w.anon_code,w.track_id,w.required_expertise_json,w.material_uri "
                "FROM review_tasks t JOIN review_works w ON w.work_id=t.work_id "
                "WHERE t.reviewer_id=? ORDER BY t.batch_id,t.work_id,t.position",
                (actor.actor_id,),
            ).fetchall()
            import json as _json
            for row in rows:
                item = {
                    "task_id": row["task_id"],
                    "batch_id": row["batch_id"],
                    "anon_code": row["anon_code"],
                    "track_id": row["track_id"],
                    "slot_type": row["slot_type"],
                    "position": row["position"],
                    "status": row["status"],
                    "required_expertise": _json.loads(row["required_expertise_json"]),
                }
                if row["status"] in MATERIAL_VISIBLE_STATUSES:
                    item["material_uri"] = row["material_uri"]
                items.append(item)
            return {"items": items}

    def get_task_material(self, actor_id: str, task_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?",
                                      (task_id,)).fetchone()
            if task is None or task["reviewer_id"] != actor.actor_id:
                # 对无权评委与冲突评委统一返回不存在，避免侧写作者信息。
                raise NotFoundError("任务不存在")
            work = connection.execute(
                "SELECT anon_code,material_uri FROM review_works WHERE work_id=?",
                (task["work_id"],),
            ).fetchone()
            self._ensure_work_open(connection, task["batch_id"], task["work_id"])
            if task["status"] not in MATERIAL_VISIBLE_STATUSES:
                raise PermissionDenied("任务当前状态不能查看材料")
            return {"task_id": task["task_id"], "anon_code": work["anon_code"],
                    "material_uri": work["material_uri"]}

    def claim_task(self, *, request_id: str, actor_id: str, task_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="claim_task", payload=payload)
            if replay is not None:
                return replay
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?",
                                      (task_id,)).fetchone()
            if task is None or task["reviewer_id"] != actor.actor_id:
                raise NotFoundError("任务不存在")
            self._ensure_work_open(connection, task["batch_id"], task["work_id"])
            batch = self._batch(connection, task["batch_id"])
            if batch["status"] not in ("published",):
                raise ConflictError("批次未发布，任务还不能领取")
            now = self._now()
            updated = connection.execute(
                "UPDATE review_tasks SET status='claimed', claimed_at=? "
                "WHERE task_id=? AND reviewer_id=? AND status='offered'",
                (now, task_id, actor.actor_id),
            )
            if updated.rowcount == 0:
                current = connection.execute(
                    "SELECT status FROM review_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if current["status"] in ("claimed", "scored"):
                    raise ConflictError("任务已经被领取，不能重复领取")
                raise ConflictError(f"任务当前状态 {current['status']} 不能领取")
            self._add_task_event(connection, task_row=task, to_status="claimed",
                                 actor_id=actor.actor_id)
            append_event(connection, actor_id=actor.actor_id, action="review.task_claimed",
                         resource_type="review_task", resource_id=task_id,
                         detail={"batch_id": task["batch_id"], "work_id": task["work_id"]},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_task", task_id, {"task_id": task_id, "status": "claimed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="claim_task", payload=payload, create=create)

    def submit_score(self, *, request_id: str, actor_id: str, task_id: str,
                     score: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "score": score}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "reviewer")
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="submit_score", payload=payload)
            if replay is not None:
                return replay
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?",
                                      (task_id,)).fetchone()
            if task is None or task["reviewer_id"] != actor.actor_id:
                raise NotFoundError("任务不存在")
            self._ensure_work_open(connection, task["batch_id"], task["work_id"])
            if not isinstance(score, dict):
                raise ValidationError("score 必须是对象")
            total = score.get("total")
            if isinstance(total, bool) or not isinstance(total, (int, float)):
                raise ValidationError("score.total 必须是 0 到 100 的数值")
            total = float(total)
            if not 0 <= total <= 100:
                raise ValidationError("score.total 必须在 0 到 100 之间")
            dimensions = score.get("dimensions", {})
            if not isinstance(dimensions, dict):
                raise ValidationError("score.dimensions 必须是对象")
            comment = str(score.get("comment", "")).strip()[:2000]
            stored_score = {"total": total, "dimensions": dimensions, "comment": comment}
            score_hash = digest(stored_score)
            now = self._now()
            updated = connection.execute(
                "UPDATE review_tasks SET status='scored', scored_at=? "
                "WHERE task_id=? AND reviewer_id=? AND status='claimed'",
                (now, task_id, actor.actor_id),
            )
            if updated.rowcount == 0:
                current = connection.execute(
                    "SELECT status FROM review_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                if current["status"] == "scored":
                    raise ConflictError("评分已经提交且不可修改")
                raise ConflictError(f"任务当前状态 {current['status']} 不能提交评分")
            score_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO review_scores(score_id,task_id,batch_id,work_id,reviewer_id,total,"
                "payload_json,payload_hash,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (score_id, task_id, task["batch_id"], task["work_id"], actor.actor_id, total,
                 canonical_json(stored_score), score_hash, now),
            )
            self._add_task_event(connection, task_row=task, to_status="scored",
                                 actor_id=actor.actor_id,
                                 detail={"score_id": score_id, "total": total})
            append_event(connection, actor_id=actor.actor_id, action="review.score_submitted",
                         resource_type="review_score", resource_id=score_id,
                         detail={"task_id": task_id, "batch_id": task["batch_id"],
                                 "work_id": task["work_id"], "score_hash": score_hash},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_score", score_id, {"score_id": score_id, "task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_score", payload=payload, create=create)

    def recuse_task(self, *, request_id: str, actor_id: str, task_id: str,
                    reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="recuse_task", payload=payload)
            if replay is not None:
                return replay
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?",
                                      (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("任务不存在")
            if actor.role == "reviewer":
                if task["reviewer_id"] != actor.actor_id:
                    raise NotFoundError("任务不存在")
            else:
                self._require(actor, *SECRETARY_ROLES)
            self._ensure_work_open(connection, task["batch_id"], task["work_id"])
            reason = self._reason_text(reason)
            now = self._now()
            updated = connection.execute(
                "UPDATE review_tasks SET status='recused', reason=?, ended_at=? "
                "WHERE task_id=? AND status IN ('offered','claimed','waitlist')",
                (reason, now, task_id),
            )
            if updated.rowcount == 0:
                current = connection.execute(
                    "SELECT status FROM review_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                raise ConflictError(f"任务当前状态 {current['status']} 不能回避")
            refreshed = dict(task)
            refreshed["status"] = "recused"
            promoted = None
            if task["status"] in TASK_OPEN_STATUSES:
                promoted = self._promote(connection, task["batch_id"], task["work_id"],
                                         actor.actor_id)
            self._add_task_event(connection, task_row=task, to_status="recused",
                                 actor_id=actor.actor_id, reason=reason,
                                 promoted_task_id=promoted,
                                 detail={"promoted_task_id": promoted})
            append_event(connection, actor_id=actor.actor_id, action="review.task_recused",
                         resource_type="review_task", resource_id=task_id,
                         detail={"batch_id": task["batch_id"], "work_id": task["work_id"],
                                 "reviewer_id": task["reviewer_id"], "reason": reason,
                                 "promoted_task_id": promoted},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_task", task_id, {"task_id": task_id, "status": "recused",
                                                "promoted_task_id": promoted}

            return self._idempotent(connection, request_id=request_id,
                                    action="recuse_task", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 秘书：缺席、作废、紧急替补与封存
    # ------------------------------------------------------------------

    def mark_absent(self, *, request_id: str, actor_id: str, batch_id: str,
                    reviewer_id: str, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "reviewer_id": reviewer_id,
                   "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="mark_absent", payload=payload)
            if replay is not None:
                return replay
            batch = self._batch(connection, batch_id)
            if batch["status"] != "published":
                raise ConflictError("缺席登记只能在已发布批次上使用")
            reason = self._reason_text(reason)
            if connection.execute(
                "SELECT 1 FROM review_batch_absences WHERE batch_id=? AND reviewer_id=?",
                (batch["batch_id"], reviewer_id),
            ).fetchone() is not None:
                raise ConflictError("该评委在本批次已登记缺席")
            now = self._now()
            connection.execute(
                "INSERT INTO review_batch_absences(batch_id,reviewer_id,reason,marked_by,"
                "created_at) VALUES(?,?,?,?,?)",
                (batch["batch_id"], reviewer_id, reason, actor.actor_id, now),
            )
            open_tasks = connection.execute(
                "SELECT * FROM review_tasks WHERE batch_id=? AND reviewer_id=? "
                "AND status IN ('offered','claimed','waitlist')",
                (batch["batch_id"], reviewer_id),
            ).fetchall()
            affected_works: set[str] = set()
            for task in open_tasks:
                connection.execute(
                    "UPDATE review_tasks SET status='absent', reason=?, ended_at=? "
                    "WHERE task_id=?",
                    (reason, now, task["task_id"]),
                )
                if task["status"] in TASK_OPEN_STATUSES:
                    affected_works.add(task["work_id"])
                self._add_task_event(connection, task_row=task, to_status="absent",
                                     actor_id=actor.actor_id, reason=reason)
            promoted_map: dict[str, str | None] = {}
            for work_id in sorted(affected_works):
                self._ensure_work_open(connection, batch["batch_id"], work_id)
                promoted_map[work_id] = self._promote(
                    connection, batch["batch_id"], work_id, actor.actor_id
                )
            append_event(connection, actor_id=actor.actor_id, action="review.reviewer_absent",
                         resource_type="review_batch", resource_id=batch["batch_id"],
                         detail={"reviewer_id": reviewer_id, "reason": reason,
                                 "task_count": len(open_tasks), "promoted": promoted_map},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_batch_absence", f"{batch['batch_id']}:{reviewer_id}", {
                    "batch_id": batch["batch_id"], "reviewer_id": reviewer_id,
                    "ended_tasks": len(open_tasks), "promoted": promoted_map,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_absent", payload=payload, create=create)

    def invalidate_score(self, *, request_id: str, actor_id: str, task_id: str,
                         reason: str) -> WriteReceipt:
        """秘书可作废旧评分但永不修改或删除分数，事实记录完整保留。"""

        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="invalidate_score", payload=payload)
            if replay is not None:
                return replay
            task = connection.execute("SELECT * FROM review_tasks WHERE task_id=?",
                                      (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("任务不存在")
            self._ensure_work_open(connection, task["batch_id"], task["work_id"])
            reason = self._reason_text(reason)
            now = self._now()
            updated = connection.execute(
                "UPDATE review_tasks SET status='invalidated', reason=?, ended_at=? "
                "WHERE task_id=? AND status='scored'",
                (reason, now, task_id),
            )
            if updated.rowcount == 0:
                current = connection.execute(
                    "SELECT status FROM review_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                raise ConflictError(f"任务当前状态 {current['status']}，没有可作废的评分")
            score = connection.execute(
                "SELECT score_id FROM review_scores WHERE task_id=?", (task_id,)
            ).fetchone()
            # 分数行原样保留（连同 payload_hash），只改变任务有效性状态；
            # 纪检报告通过 valid_for_quorum=False 标明它不计入法定人数。
            promoted = self._promote(connection, task["batch_id"], task["work_id"],
                                     actor.actor_id)
            self._add_task_event(connection, task_row=task, to_status="invalidated",
                                 actor_id=actor.actor_id, reason=reason,
                                 promoted_task_id=promoted,
                                 detail={"score_id": score["score_id"],
                                         "promoted_task_id": promoted})
            append_event(connection, actor_id=actor.actor_id, action="review.score_invalidated",
                         resource_type="review_task", resource_id=task_id,
                         detail={"batch_id": task["batch_id"], "work_id": task["work_id"],
                                 "reviewer_id": task["reviewer_id"], "reason": reason,
                                 "score_id": score["score_id"], "promoted_task_id": promoted},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_task", task_id, {"task_id": task_id, "status": "invalidated",
                                                "promoted_task_id": promoted}

            return self._idempotent(connection, request_id=request_id,
                                    action="invalidate_score", payload=payload, create=create)

    def emergency_assign(self, *, request_id: str, actor_id: str, batch_id: str, work_id: str,
                         reviewer_id: str, reason: str) -> WriteReceipt:
        """发布后例外：正式有效席位不足时，秘书凭理由临时追加无冲突评委。"""

        payload = {"actor_id": actor_id, "batch_id": batch_id, "work_id": work_id,
                   "reviewer_id": reviewer_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="emergency_assign", payload=payload)
            if replay is not None:
                return replay
            batch = self._batch(connection, batch_id)
            if batch["status"] != "published":
                raise ConflictError("紧急替补只能在已发布批次上使用")
            self._ensure_work_open(connection, batch["batch_id"], work_id)
            reason = self._reason_text(reason)
            profile = connection.execute(
                "SELECT * FROM review_reviewer_profiles WHERE reviewer_id=?", (reviewer_id,)
            ).fetchone()
            if profile is None or not profile["active"]:
                raise NotFoundError("评委档案不存在或已停用")
            import json as _json
            work = self._load_work_view(connection, work_id)
            if work["track_id"] not in set(_json.loads(profile["track_ids_json"])):
                raise ValidationError("评委不具备该赛道评审资格")
            relations = [dict(row) for row in connection.execute(
                "SELECT relation_id,reviewer_id,subject_kind,target_key,relation_kind,detail "
                "FROM review_relations WHERE reviewer_id=?", (reviewer_id,)
            )]
            conflict = self._conflict_reasons(reviewer_id, work, relations)
            if conflict:
                raise PermissionDenied("该评委与作品存在利益冲突，例外不能突破回避红线: "
                                       + canonical_json(conflict))
            existing = connection.execute(
                "SELECT 1 FROM review_tasks WHERE batch_id=? AND work_id=? AND reviewer_id=?",
                (batch["batch_id"], work_id, reviewer_id),
            ).fetchone()
            if existing is not None:
                raise ConflictError("该评委在这件作品上已有任务记录")
            absent = connection.execute(
                "SELECT 1 FROM review_batch_absences WHERE batch_id=? AND reviewer_id=?",
                (batch["batch_id"], reviewer_id),
            ).fetchone()
            if absent is not None:
                raise ConflictError("该评委在本批次已登记缺席")
            effective = connection.execute(
                "SELECT COUNT(*) AS count FROM review_tasks WHERE batch_id=? AND work_id=? "
                "AND status IN ('offered','claimed','scored')",
                (batch["batch_id"], work_id),
            ).fetchone()["count"]
            rules, _ = self._load_rules(connection, batch["rule_version_id"])
            if effective >= rules["independent_reviewers"]:
                raise ConflictError("正式评委席位未满员缺失时不能使用紧急替补")

            max_position = connection.execute(
                "SELECT COALESCE(MAX(position), -1) AS max_position FROM review_tasks "
                "WHERE batch_id=? AND work_id=?",
                (batch["batch_id"], work_id),
            ).fetchone()["max_position"]
            task_id = uuid.uuid4().hex
            now = self._now()
            connection.execute(
                "INSERT INTO review_tasks(task_id,batch_id,work_id,reviewer_id,slot_type,"
                "position,status,created_at) VALUES(?,?,?,?,?,?,'offered',?)",
                (task_id, batch["batch_id"], work_id, reviewer_id, "primary",
                 max_position + 1, now),
            )
            exception_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO review_exceptions(exception_id,batch_id,work_id,reviewer_id,"
                "exception_type,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (exception_id, batch["batch_id"], work_id, reviewer_id,
                 "emergency_substitution", reason, actor.actor_id, now),
            )
            append_event(connection, actor_id=actor.actor_id,
                         action="review.emergency_assignment",
                         resource_type="review_task", resource_id=task_id,
                         detail={"batch_id": batch["batch_id"], "work_id": work_id,
                                 "reviewer_id": reviewer_id, "reason": reason,
                                 "live_relation_check": True},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_task", task_id, {"task_id": task_id, "status": "offered",
                                                "exception_id": exception_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="emergency_assign", payload=payload, create=create)

    def seal_work(self, *, request_id: str, actor_id: str, batch_id: str,
                  work_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "work_id": work_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="seal_work", payload=payload)
            if replay is not None:
                return replay
            batch = self._batch(connection, batch_id)
            if batch["status"] != "published":
                raise ConflictError("批次未发布，不能封存")
            self._ensure_work_open(connection, batch["batch_id"], work_id)
            rules, _ = self._load_rules(connection, batch["rule_version_id"])
            scored = connection.execute(
                "SELECT COUNT(*) AS count FROM review_tasks WHERE batch_id=? AND work_id=? "
                "AND status='scored'",
                (batch["batch_id"], work_id),
            ).fetchone()["count"]
            if scored < rules["quorum"]:
                raise ConflictError(
                    f"有效评分 {scored} 份，未达到法定人数 {rules['quorum']}，不能封存"
                )
            now = self._now()
            connection.execute(
                "UPDATE review_batch_works SET status='sealed', sealed_at=? "
                "WHERE batch_id=? AND work_id=?",
                (now, batch["batch_id"], work_id),
            )
            append_event(connection, actor_id=actor.actor_id, action="review.work_sealed",
                         resource_type="review_work", resource_id=work_id,
                         detail={"batch_id": batch["batch_id"], "scored": scored,
                                 "quorum": rules["quorum"]},
                         occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_work", work_id, {"batch_id": batch["batch_id"],
                                                "work_id": work_id, "status": "sealed",
                                                "scored": scored}

            return self._idempotent(connection, request_id=request_id,
                                    action="seal_work", payload=payload, create=create)

    def seal_batch(self, *, request_id: str, actor_id: str, batch_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *SECRETARY_ROLES)
            replay = self._replay_if_present(connection, request_id=request_id,
                                             action="seal_batch", payload=payload)
            if replay is not None:
                return replay
            batch = self._batch(connection, batch_id)
            if batch["status"] != "published":
                raise ConflictError("只有已发布批次可以整体封存")
            unsealed = connection.execute(
                "SELECT COUNT(*) AS count FROM review_batch_works WHERE batch_id=? "
                "AND status!='sealed'",
                (batch["batch_id"],),
            ).fetchone()["count"]
            if unsealed:
                raise ConflictError(f"还有 {unsealed} 件作品未封存，不能封存批次")
            now = self._now()
            connection.execute(
                "UPDATE review_batches SET status='sealed', sealed_at=? WHERE batch_id=?",
                (now, batch["batch_id"]),
            )
            append_event(connection, actor_id=actor.actor_id, action="review.batch_sealed",
                         resource_type="review_batch", resource_id=batch["batch_id"],
                         detail={"sealed_at": now}, occurred_at=now)

            def create() -> tuple[str, str, dict[str, Any]]:
                return "review_batch", batch["batch_id"], {"batch_id": batch["batch_id"],
                                                           "status": "sealed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="seal_batch", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 纪检复核
    # ------------------------------------------------------------------

    def get_snapshot(self, actor_id: str, snapshot_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "auditor", *SECRETARY_ROLES)
            row = connection.execute(
                "SELECT * FROM review_relation_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("关系快照不存在")
            import json as _json
            content = _json.loads(row["content_json"])
            return {"snapshot_id": row["snapshot_id"], "relation_count": row["relation_count"],
                    "content": content, "content_hash": row["content_hash"],
                    "hash_match": digest(content) == row["content_hash"],
                    "created_at": row["created_at"]}

    def get_batch_report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """纪检复核：每件作品的快照、规则版本、回避原因、替补顺序与事实链。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "auditor", *SECRETARY_ROLES)
            import json as _json
            batch = self._batch(connection, batch_id)
            rules, rules_hash = self._load_rules(connection, batch["rule_version_id"])
            works_report = []
            plan_rows = connection.execute(
                "SELECT * FROM review_work_plans WHERE batch_id=? ORDER BY work_id",
                (batch["batch_id"],),
            ).fetchall()
            for plan in plan_rows:
                snapshot = connection.execute(
                    "SELECT * FROM review_relation_snapshots WHERE snapshot_id=?",
                    (plan["snapshot_id"],),
                ).fetchone()
                snapshot_content = _json.loads(snapshot["content_json"])
                work = connection.execute(
                    "SELECT * FROM review_works WHERE work_id=?", (plan["work_id"],)
                ).fetchone()
                bw = connection.execute(
                    "SELECT status, sealed_at FROM review_batch_works WHERE batch_id=? AND work_id=?",
                    (batch["batch_id"], plan["work_id"]),
                ).fetchone()
                tasks = []
                for task in connection.execute(
                    "SELECT * FROM review_tasks WHERE batch_id=? AND work_id=? "
                    "ORDER BY position, task_id",
                    (batch["batch_id"], plan["work_id"]),
                ):
                    score = connection.execute(
                        "SELECT score_id,total,payload_json,payload_hash,submitted_at "
                        "FROM review_scores WHERE task_id=?",
                        (task["task_id"],),
                    ).fetchone()
                    events = [
                        {"event_id": event["event_id"], "from_status": event["from_status"],
                         "to_status": event["to_status"], "reason": event["reason"],
                         "promoted_task_id": event["promoted_task_id"],
                         "actor_id": event["actor_id"], "occurred_at": event["occurred_at"],
                         "detail": _json.loads(event["detail_json"])}
                        for event in connection.execute(
                            "SELECT * FROM review_task_events WHERE task_id=? "
                            "ORDER BY rowid",
                            (task["task_id"],),
                        )
                    ]
                    tasks.append({
                        "task_id": task["task_id"], "reviewer_id": task["reviewer_id"],
                        "slot_type": task["slot_type"], "position": task["position"],
                        "status": task["status"], "reason": task["reason"],
                        "claimed_at": task["claimed_at"], "scored_at": task["scored_at"],
                        "ended_at": task["ended_at"],
                        "score": None if score is None else {
                            "score_id": score["score_id"], "total": score["total"],
                            "payload": _json.loads(score["payload_json"]),
                            "payload_hash": score["payload_hash"],
                            "submitted_at": score["submitted_at"],
                            "valid_for_quorum": task["status"] == "scored",
                        },
                        "events": events,
                    })
                exceptions = [
                    {"exception_id": row["exception_id"], "reviewer_id": row["reviewer_id"],
                     "exception_type": row["exception_type"], "reason": row["reason"],
                     "created_by": row["created_by"], "created_at": row["created_at"]}
                    for row in connection.execute(
                        "SELECT * FROM review_exceptions WHERE batch_id=? AND work_id=? "
                        "ORDER BY created_at, exception_id",
                        (batch["batch_id"], plan["work_id"]),
                    )
                ]
                works_report.append({
                    "work_id": plan["work_id"],
                    "anon_code": work["anon_code"],
                    "title": work["title"],
                    "organization_id": work["organization_id"],
                    "team": _json.loads(work["team_json"]),
                    "seal_status": bw["status"],
                    "sealed_at": bw["sealed_at"],
                    "selection": _json.loads(plan["selection_json"]),
                    "exclusions": _json.loads(plan["exclusions_json"]),
                    "substitute_order": _json.loads(plan["substitutes_json"]),
                    "uncovered_expertise": _json.loads(plan["uncovered_json"]),
                    "exceptions": exceptions,
                    "tasks": tasks,
                    "snapshot": {
                        "snapshot_id": snapshot["snapshot_id"],
                        "content_hash": snapshot["content_hash"],
                        "relation_count": snapshot["relation_count"],
                        "hash_match": digest(snapshot_content) == snapshot["content_hash"],
                        "created_at": snapshot["created_at"],
                    },
                })
            return {
                "batch_id": batch["batch_id"],
                "status": batch["status"],
                "rule_version": {
                    "rule_version_id": batch["rule_version_id"],
                    "payload_hash": rules_hash,
                    "rules": rules,
                },
                "generated_snapshot_id": batch["generated_snapshot_id"],
                "published_at": batch["published_at"],
                "sealed_at": batch["sealed_at"],
                "works": works_report,
            }
