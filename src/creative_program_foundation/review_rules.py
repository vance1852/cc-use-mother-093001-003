"""规则版本、关系快照与可解释的盲审分配引擎。

引擎只依赖不可变的快照数据运行：批次创建时冻结当时的评委、作品团队与
回避声明，之后声明的增改不会影响已发布批次，便于纪检复核“当时依据什么分配”。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .audit import digest

RULE_VERSION_LAYOUT = 1

DEFAULT_RULE_BODY: dict[str, Any] = {
    "layout": RULE_VERSION_LAYOUT,
    # 每件作品至少需要的独立评委数（作品要求不得低于该值）。
    "min_independent_reviewers": 3,
    # 同一评委在同一赛道内本批次最多承担的任务数。
    "track_load_cap": 5,
    # 是否强制每件作品要求的专业领域全部被覆盖。
    "require_discipline_coverage": True,
    # 每件作品冻结的候补评委数量。
    "substitute_pool_size": 2,
    # 触发回避的关系类型：任职、合作、亲属、师生、协会、同机构。
    "conflict_relation_types": [
        "employment", "collaboration", "kinship", "mentor", "association", "organization"
    ],
    # 评委所属机构与参赛机构重合时自动回避，无需另行声明。
    "auto_conflict_same_organization": True,
}

CONFLICT_RELATION_LABELS = {
    "employment": "任职服务",
    "collaboration": "合作关系",
    "kinship": "亲属关系",
    "mentor": "师生关系",
    "association": "同属协会",
    "organization": "同属机构",
}


def default_rule_body() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_RULE_BODY)


def validate_rule_body(body: dict[str, Any]) -> None:
    if not isinstance(body, dict):
        raise ValueError("规则必须是对象")
    required = ("min_independent_reviewers", "track_load_cap",
                "require_discipline_coverage", "substitute_pool_size", "conflict_relation_types")
    for key in required:
        if key not in body:
            raise ValueError(f"规则缺少字段 {key}")
    if not isinstance(body["min_independent_reviewers"], int) or body["min_independent_reviewers"] < 1:
        raise ValueError("min_independent_reviewers 必须是正整数")
    if not isinstance(body["track_load_cap"], int) or body["track_load_cap"] < 1:
        raise ValueError("track_load_cap 必须是正整数")
    if not isinstance(body["substitute_pool_size"], int) or body["substitute_pool_size"] < 0:
        raise ValueError("substitute_pool_size 必须是非负整数")
    if not isinstance(body["conflict_relation_types"], list) or not body["conflict_relation_types"]:
        raise ValueError("conflict_relation_types 必须是非空数组")
    if "auto_conflict_same_organization" in body and not isinstance(
            body["auto_conflict_same_organization"], bool):
        raise ValueError("auto_conflict_same_organization 必须是布尔值")


def relation_label(relation_type: str) -> str:
    return CONFLICT_RELATION_LABELS.get(relation_type, relation_type)


@dataclass(frozen=True)
class Snapshot:
    """批次冻结的关系快照。"""

    body: dict[str, Any]

    @property
    def hash(self) -> str:
        return digest(self.body)


def build_snapshot(*, reviewers: list[dict[str, Any]], works: list[dict[str, Any]],
                   declarations: list[dict[str, Any]]) -> Snapshot:
    """组装规范化快照。works 需预先聚合作者人员键与机构键。"""

    body = {
        "layout": 1,
        "reviewers": sorted(
            ({"reviewer_id": r["reviewer_id"], "organization_id": r.get("organization_id") or "",
              "active": bool(r["active"]), "disciplines": sorted(r.get("disciplines") or [])}
             for r in reviewers),
            key=lambda r: r["reviewer_id"],
        ),
        "works": sorted(
            ({"work_id": w["work_id"], "track_id": w["track_id"],
              "required_disciplines": list(w.get("required_disciplines") or []),
              "required_reviewer_count": w["required_reviewer_count"],
              "person_keys": sorted(w.get("person_keys") or []),
              "organization_keys": sorted(w.get("organization_keys") or [])}
             for w in works),
            key=lambda w: w["work_id"],
        ),
        "declarations": sorted(
            ({"reviewer_id": d["reviewer_id"], "subject_type": d["subject_type"],
              "subject_key": d["subject_key"], "relation_type": d["relation_type"],
              "detail": d.get("detail") or ""}
             for d in declarations if d.get("active", True)),
            key=lambda d: (d["reviewer_id"], d["subject_type"], d["subject_key"], d["relation_type"]),
        ),
    }
    return Snapshot(body=body)


@dataclass(frozen=True)
class PlannedTask:
    work_id: str
    reviewer_id: str
    discipline_id: str
    slot_index: int
    reason: str


@dataclass(frozen=True)
class PlannedSubstitute:
    work_id: str
    reviewer_id: str
    rank_order: int
    reason: str


@dataclass
class PlanResult:
    tasks: list[PlannedTask]
    substitutes: list[PlannedSubstitute]
    explanations: dict[str, Any]
    infeasible: list[dict[str, str]]


def _conflict_reasons(declarations: list[dict[str, Any]], work: dict[str, Any],
                      enabled_types: frozenset[str], auto_same_organization: bool,
                      reviewers: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """返回 reviewer_id -> 回避原因列表。"""

    person_keys = set(work.get("person_keys") or [])
    org_keys = set(work.get("organization_keys") or [])
    reasons: dict[str, list[str]] = {}
    for declaration in declarations:
        if declaration["relation_type"] not in enabled_types:
            continue
        matched = False
        if declaration["subject_type"] == "person" and declaration["subject_key"] in person_keys:
            matched = True
        elif declaration["subject_type"] == "organization" and declaration["subject_key"] in org_keys:
            matched = True
        if matched:
            label = relation_label(declaration["relation_type"])
            reasons.setdefault(declaration["reviewer_id"], []).append(
                f"{label}:{declaration['subject_type']}:{declaration['subject_key']}"
            )
    if auto_same_organization:
        for reviewer_id, reviewer in reviewers.items():
            org = reviewer.get("organization_id") or ""
            if org and org in org_keys:
                reasons.setdefault(reviewer_id, []).append(f"同属机构自动回避:organization:{org}")
    return reasons


def generate_plan(snapshot: Snapshot, rules: dict[str, Any]) -> PlanResult:
    """依据快照和规则版本生成确定性、可解释的分配方案。

    选择策略：先为每个必修专业锚定槽位，再补自由槽位；候选评委按
    （赛道当前负载、评委编号）排序，确保同一份快照与规则永远得到同一方案。
    """

    reviewers = {r["reviewer_id"]: r for r in snapshot.body["reviewers"]}
    works = snapshot.body["works"]
    declarations = snapshot.body["declarations"]
    load_cap = rules["track_load_cap"]
    pool_size = rules["substitute_pool_size"]
    enabled_types = frozenset(rules["conflict_relation_types"])
    auto_same_organization = rules.get("auto_conflict_same_organization", True)
    require_coverage = rules.get("require_discipline_coverage", True)

    # track_id -> reviewer_id -> 已分配任务数
    track_load: dict[str, dict[str, int]] = {}
    tasks: list[PlannedTask] = []
    substitutes: list[PlannedSubstitute] = []
    explanations: dict[str, Any] = {}
    infeasible: list[dict[str, str]] = []

    for work in sorted(works, key=lambda w: w["work_id"]):
        work_id = work["work_id"]
        track_id = work["track_id"]
        required_count = work["required_reviewer_count"]
        required_disciplines = list(work.get("required_disciplines") or [])
        if not require_coverage:
            required_disciplines = []
        load = track_load.setdefault(track_id, {})
        conflict = _conflict_reasons(declarations, work, enabled_types,
                                     auto_same_organization, reviewers)

        excluded: dict[str, list[str]] = {}
        eligible: set[str] = set()
        for reviewer_id, reviewer in reviewers.items():
            reasons: list[str] = []
            if not reviewer["active"]:
                reasons.append("inactive")
            reasons.extend(conflict.get(reviewer_id, ()))
            if reasons:
                excluded[reviewer_id] = reasons
            else:
                eligible.add(reviewer_id)

        chosen: list[PlannedTask] = []
        used: set[str] = set()
        slots: list[tuple[int, str]] = []
        # 前若干槽位锚定必修专业，其余为自由槽位。
        for index, discipline in enumerate(required_disciplines):
            slots.append((index, discipline))
        for index in range(len(required_disciplines), required_count):
            slots.append((index, ""))

        work_infeasible = ""
        for slot_index, discipline in slots:
            if discipline:
                candidates = [r for r in eligible - used
                              if discipline in reviewers[r]["disciplines"]
                              and load.get(r, 0) < load_cap]
                reason = f"discipline_slot:{discipline}"
            else:
                candidates = [r for r in eligible - used if load.get(r, 0) < load_cap]
                reason = "independent_slot"
            candidates.sort(key=lambda r: (load.get(r, 0), r))
            if not candidates:
                if discipline and any(r in (eligible - used)
                                      for r in reviewers
                                      if discipline in reviewers[r]["disciplines"]):
                    work_infeasible = f"专业 {discipline} 的候选评委均已达到赛道负载上限"
                elif discipline:
                    work_infeasible = f"缺少具备专业 {discipline} 且无冲突的评委"
                else:
                    work_infeasible = f"无法凑齐 {required_count} 名无冲突评委"
                break
            picked = candidates[0]
            used.add(picked)
            load[picked] = load.get(picked, 0) + 1
            chosen.append(PlannedTask(work_id, picked, discipline, slot_index, reason))

        explanation: dict[str, Any] = {
            "track_id": track_id,
            "required_reviewer_count": required_count,
            "required_disciplines": required_disciplines,
            "assignments": [
                {"reviewer_id": t.reviewer_id, "slot_index": t.slot_index,
                 "discipline_id": t.discipline_id, "reason": t.reason}
                for t in sorted(chosen, key=lambda t: t.slot_index)
            ],
            "excluded": [
                {"reviewer_id": reviewer_id, "reasons": reasons}
                for reviewer_id, reasons in sorted(excluded.items())
            ],
            "substitutes": [],
        }

        if work_infeasible:
            infeasible.append({"work_id": work_id, "reason": work_infeasible})
            explanation["feasible"] = False
            explanation["deficit"] = work_infeasible
            explanations[work_id] = explanation
            continue

        tasks.extend(chosen)

        # 候补：未入选且无冲突的评委，优先能覆盖必修专业者，按（可覆盖专业数、负载、编号）排序。
        required_set = set(required_disciplines)
        remaining = sorted(
            eligible - used,
            key=lambda r: (
                -len(set(reviewers[r]["disciplines"]) & required_set),
                load.get(r, 0), r,
            ),
        )
        rank = 0
        for reviewer_id in remaining:
            if rank >= pool_size:
                break
            spare_disciplines = sorted(set(reviewers[reviewer_id]["disciplines"]) & required_set)
            reason = ("substitute_discipline:" + ",".join(spare_disciplines)
                      if spare_disciplines else "substitute_general")
            substitutes.append(PlannedSubstitute(work_id, reviewer_id, rank, reason))
            explanation["substitutes"].append(
                {"reviewer_id": reviewer_id, "rank_order": rank, "reason": reason})
            rank += 1
        explanation["feasible"] = True
        explanations[work_id] = explanation

    return PlanResult(tasks=tasks, substitutes=substitutes,
                      explanations=explanations, infeasible=infeasible)
