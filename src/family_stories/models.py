"""定义家风故事模块在边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StoryVersion:
    """一次不可变的故事修订；旧版本永不覆盖。"""

    story_id: str
    version: int
    title: str
    outline: str
    summary: str
    persons: list[dict[str, Any]]
    source_category: str
    source_record_id: str | None
    change_reason: str
    created_by: str
    created_at: str
    supersedes: int | None
    content_hash: str


@dataclass(frozen=True)
class ConsentGrant:
    """针对一位叙述对象的一条有效/历史授权。"""

    consent_id: str
    story_id: str
    subject_id: str
    relation: str
    scope: str
    channel: str
    note: str
    granted_by: str
    created_at: str
    superseded_by: str | None


@dataclass(frozen=True)
class ReviewOpinion:
    """一个复核阶段的不可覆盖意见。"""

    review_id: str
    story_id: str
    version: int
    stage: str
    reviewer_id: str
    decision: str
    note: str
    created_at: str


@dataclass(frozen=True)
class AccessAuditEntry:
    """一条不可变访问审计：谁在什么视野下看了什么、看到多少。"""

    access_id: str
    story_id: str
    version: int
    actor_id: str
    view_scope: str
    effective_scope: str
    visible_fields: list[str]
    hidden_fields: list[str]
    purpose: str
    created_at: str
