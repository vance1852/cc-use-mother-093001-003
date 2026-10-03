"""定义盲审与利益回避后台的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Reviewer:
    """评委档案：专业领域以 discipline_id 列表表示。"""

    reviewer_id: str
    display_name: str
    organization_id: str
    active: bool
    disciplines: tuple[str, ...]


@dataclass(frozen=True)
class Work:
    """参赛作品；作者信息只保存在团队关系中，不进入评委视图。"""

    work_id: str
    title: str
    track_id: str
    required_disciplines: tuple[str, ...]
    required_reviewer_count: int
    material_ref: str
    anonymized_code: str
    sealed: bool


@dataclass(frozen=True)
class TeamMember:
    """作品团队成员或所属机构关系（身份信息，对评委不可见）。"""

    member_id: str
    work_id: str
    person_key: str
    role_label: str
    organization_id: str


@dataclass(frozen=True)
class ConflictDeclaration:
    """评委主动声明的任职、合作、亲属或师生等关系。"""

    declaration_id: str
    reviewer_id: str
    subject_type: str  # person / organization
    subject_key: str
    relation_type: str  # employment / collaboration / kinship / mentor / association
    detail: str
    active: bool


@dataclass(frozen=True)
class RuleVersion:
    """可版本化的分配规则（独立人数、专业覆盖、赛道负载上限等）。"""

    rule_version: str
    body: dict[str, Any]
    body_hash: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class ReviewTask:
    """一件作品与一位评委之间的匿名评审任务。"""

    task_id: str
    batch_id: str
    work_id: str
    reviewer_id: str
    discipline_id: str
    slot_index: int
    status: str
    conflict_reason: str
    locked_for_review: bool


@dataclass(frozen=True)
class ReviewScore:
    """评委提交的评分事实；封存或作废后不可再改。"""

    score_id: str
    task_id: str
    work_id: str
    reviewer_id: str
    score_value: float
    comment: str
    submitted_at: str
    voided_at: str | None
    void_reason: str


@dataclass(frozen=True)
class SubstituteEntry:
    """候补评委队列，按 rank_order 顺序依次晋升。"""

    substitute_id: str
    batch_id: str
    work_id: str
    reviewer_id: str
    rank_order: int
    reason: str
    status: str


@dataclass(frozen=True)
class AssignmentPlan:
    """一次分配的可解释结果。"""

    batch_id: str
    rule_version: str
    snapshot_hash: str
    tasks: list[ReviewTask] = field(default_factory=list)
    substitutes: list[SubstituteEntry] = field(default_factory=list)
    explanations: dict[str, Any] = field(default_factory=dict)
    infeasible: list[dict[str, str]] = field(default_factory=list)
