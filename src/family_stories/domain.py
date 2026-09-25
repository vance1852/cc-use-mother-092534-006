"""定义家风故事分级开放的范围等级、复核阶段与脱敏策略。"""

from __future__ import annotations

# 开放范围由窄到宽：仅内部 < 班级分享 < 社区展览 < 公开资料库。
SCOPES = ("private", "class", "community", "public")
SCOPE_RANK = {scope: rank for rank, scope in enumerate(SCOPES)}

# 发布前必须依次完成的复核阶段，各阶段由不同资格的角色承担。
STAGES = ("fact_check", "sensitive_review", "consent_confirm")

# 故事版本的来源类型。
CHANGE_REASONS = ("initial", "edit", "correction", "review_return", "import")

# 授权记录的登记渠道。
CONSENT_CHANNELS = ("recorded", "narrow_request", "import")

# 各开放范围对字段的脱敏策略，供视图与审计解释共用。
REDACTION_POLICY = (
    {"scope": "class", "visible": ["summary"], "hidden": ["outline", "person_names"]},
    {"scope": "community", "visible": ["summary", "outline"], "hidden": ["person_names"]},
    {"scope": "public", "visible": ["summary", "outline", "person_names"], "hidden": []},
)


def is_scope(value: str) -> bool:
    return value in SCOPE_RANK


def scope_rank(scope: str) -> int:
    return SCOPE_RANK[scope]


def next_stage(stage: str) -> str | None:
    """返回复核流水线的下一阶段，末阶段返回 None。"""

    index = STAGES.index(stage)
    return STAGES[index + 1] if index + 1 < len(STAGES) else None


def allows(scope: str, field: str) -> bool:
    """判断在给定开放范围下某字段是否可见。"""

    rank = SCOPE_RANK[scope]
    if field == "summary":
        return rank >= SCOPE_RANK["class"]
    if field == "outline":
        return rank >= SCOPE_RANK["community"]
    if field == "person_names":
        return rank >= SCOPE_RANK["public"]
    raise ValueError(f"未知字段 {field}")
