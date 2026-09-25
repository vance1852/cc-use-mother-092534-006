"""家风故事模块的领域常量、可见级别与脱敏规则。"""

from __future__ import annotations

import copy
from typing import Any

# 开放级别由小到大：数字越大可见范围越广。
# private   仅班级内部分享
# community 允许进入社区展览
# archive   允许进入公开资料库
SCOPE_LEVELS: dict[str, int] = {
    "private": 1,
    "community": 2,
    "archive": 3,
}

# 允许的叙述对象引用键前缀
SUBJECT_PREFIX = "subject:"

REVIEW_STAGES: tuple[str, ...] = ("fact_check", "sensitive_check", "consent_confirm")

# 脱敏动作
REDACTION_ACTIONS = frozenset({"mask", "hide", "replace"})

# 修订类型
REVISION_TYPES = frozenset({"submission", "correction", "narrowing", "review_return", "batch_import"})


def normalize_scope(scope: str) -> str:
    scope = str(scope).strip()
    if scope not in SCOPE_LEVELS:
        raise ValueError(f"未知的开放级别: {scope}")
    return scope


def min_scope(*scopes: str) -> str:
    """多个授权取安全交集：可见级别最小者。"""

    if not scopes:
        return "private"
    return min(scopes, key=lambda item: SCOPE_LEVELS[item])


def is_within(granted: str, requested: str) -> bool:
    """请求的级别是否落在授权范围内。"""

    return SCOPE_LEVELS[requested] <= SCOPE_LEVELS[granted]


def validate_content(content: Any) -> dict[str, Any]:
    """校验故事内容结构，返回规范化副本。"""

    if not isinstance(content, dict) or not content:
        raise ValueError("content 必须是非空对象")
    title = str(content.get("title", "")).strip()
    if not title:
        raise ValueError("content.title 不能为空")
    if len(title) > 200:
        raise ValueError("content.title 不能超过 200 个字符")
    outline = content.get("interview_outline", [])
    if not isinstance(outline, list) or not outline:
        raise ValueError("interview_outline 必须是非空数组（访谈提纲）")
    for item in outline:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("interview_outline 的每一项必须是非空字符串")
    subjects = content.get("subjects", [])
    if not isinstance(subjects, list) or not subjects:
        raise ValueError("subjects 必须是非空数组（至少一位叙述对象）")
    normalized_subjects = []
    seen: set[str] = set()
    for subject in subjects:
        if not isinstance(subject, dict):
            raise ValueError("subjects 的每一项必须是对象")
        key = str(subject.get("key", "")).strip()
        if not key.startswith(SUBJECT_PREFIX):
            raise ValueError("subjects[].key 必须以 subject: 开头")
        if key in seen:
            raise ValueError(f"叙述对象重复: {key}")
        seen.add(key)
        name = str(subject.get("display_name", "")).strip()
        if not name:
            raise ValueError("subjects[].display_name 不能为空")
        relation = str(subject.get("relation", "")).strip()
        entry = {"key": key, "display_name": name}
        if relation:
            entry["relation"] = relation
        normalized_subjects.append(entry)
    citations = content.get("citations", [])
    if not isinstance(citations, list):
        raise ValueError("citations 必须是数组（结构化人物引用）")
    for citation in citations:
        if not isinstance(citation, dict):
            raise ValueError("citations 的每一项必须是对象")
        target = str(citation.get("subject_key", "")).strip()
        if target not in seen:
            raise ValueError(f"citation 引用了未登记的叙述对象: {target}")
        text = str(citation.get("text", "")).strip()
        if not text:
            raise ValueError("citations[].text 不能为空")
    summary = str(content.get("summary", "")).strip()
    if not summary:
        raise ValueError("summary 不能为空（资料摘要）")
    return copy.deepcopy(content)


def apply_redactions(content: dict[str, Any], marks: list[dict[str, Any]], granted_scope: str) -> dict[str, Any]:
    """按脱敏标注生成面向给定级别的脱敏视图。

    每个标注声明触发它所需的最低可见级别 min_scope：
    仅当内容将在该级别或更高级别公开时才应用，级别越低隐藏越多。
    """

    view = copy.deepcopy(content)
    for mark in sorted(marks, key=lambda item: (item["target"] != "summary", item["target"])):
        threshold = mark.get("min_scope") or "archive"
        if SCOPE_LEVELS[granted_scope] < SCOPE_LEVELS[threshold]:
            continue
        action = mark["action"]
        target = mark["target"]
        replacement = mark.get("replacement")
        if target == "summary":
            if action == "hide":
                view["summary"] = ""
            elif action == "replace":
                view["summary"] = replacement or "（摘要已替代）"
            else:
                view["summary"] = "（摘要已脱敏）"
            continue
        # 结构化人物引用：citations[index] 或 citations[index].field
        prefix = "citations"
        if target.startswith(prefix):
            path = target[len(prefix):].lstrip(".")
            head, _, tail = path.partition(".")
            index_text = head.strip("[]")
            if not index_text.isdigit():
                continue
            index = int(index_text)
            citations = view.get("citations", [])
            if not isinstance(citations, list) or index >= len(citations):
                continue
            if not tail:
                if action == "hide":
                    citations.pop(index)
                elif action == "replace":
                    citations[index] = {"text": replacement or "（引用已替代）"}
                else:
                    citations[index] = {"text": "（引用已脱敏）"}
            elif isinstance(citations[index], dict) and tail in citations[index]:
                if action == "hide":
                    citations[index].pop(tail, None)
                elif action == "replace":
                    citations[index][tail] = replacement or "（已替代）"
                else:
                    citations[index][tail] = "×××"
    return view


def explain_redactions(marks: list[dict[str, Any]], granted_scope: str) -> list[dict[str, Any]]:
    """为审计人员列出某级别下生效的脱敏标注及原因。"""

    applied = []
    for mark in marks:
        threshold = mark.get("min_scope") or "archive"
        if SCOPE_LEVELS[granted_scope] < SCOPE_LEVELS[threshold]:
            continue
        applied.append({
            "target": mark["target"],
            "action": mark["action"],
            "min_scope": threshold,
            "replacement": mark.get("replacement"),
            "reason": mark["reason"],
            "created_by": mark["created_by"],
            "created_at": mark["created_at"],
        })
    return applied
