"""家风故事采集与分级开放服务。

在基础身份、场所、幂等回执与哈希审计链之上提供：
- 不可变版本（每次提交、更正、退回都产生新修订，旧版本与旧意见保留）；
- 分级同意（private 班级 / community 社区展览 / archive 公开资料库），
  多位叙述对象取所有有效授权的安全交集，补充人物关系后重新计算；
- 脱敏视图与发布视图，提交者可随时更正或缩小未来可见范围；
- 三步复核队列（事实核对、敏感信息复核、授权确认），带审核租约；
- 批量导入全有或全无，并按来源识别重放。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .audit import GENESIS_HASH, append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .stories_domain import (
    REDACTION_ACTIONS,
    REVIEW_STAGES,
    SCOPE_LEVELS,
    apply_redactions,
    explain_redactions,
    is_within,
    min_scope,
    normalize_scope,
    validate_content,
)

DEFAULT_LEASE_SECONDS = 15 * 60

CONSENT_TYPES = frozenset({"initial", "correction", "narrowing", "withdrawal"})


class StoryService:
    """协调家风故事的版本、同意、复核与开放规则。"""

    def __init__(self, database, base, clock: Any | None = None,
                 lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        self.database = database
        self.base = base
        self.clock = clock or base.clock
        self.lease_seconds = lease_seconds

    # ----- 通用辅助 -----

    def _now_dt(self) -> datetime:
        return self.clock.now()

    def _now(self) -> str:
        return self._now_dt().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _actor(self, connection, actor_id: str):
        return self.base._actor(connection, actor_id)

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]):
        return self.base._idempotent(connection, request_id=request_id, action=action,
                                     payload=payload, create=create)

    @staticmethod
    def _require(actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _story(self, connection, story_id: str):
        row = connection.execute("SELECT * FROM stories WHERE story_id=?", (story_id,)).fetchone()
        if row is None:
            raise NotFoundError("家风故事不存在")
        return row

    def _ensure_site_org(self, actor, site_row) -> None:
        if actor.organization_id != site_row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所资料")

    def _ensure_story_org(self, connection, actor, story_row) -> None:
        site = self._site(connection, story_row["site_id"])
        self._ensure_site_org(actor, site)

    @staticmethod
    def _revision(connection, story_id: str, version: int):
        row = connection.execute(
            "SELECT * FROM story_revisions WHERE story_id=? AND version=?", (story_id, version)
        ).fetchone()
        if row is None:
            raise NotFoundError("故事版本不存在")
        return row

    @staticmethod
    def _content(connection, story_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT content_json FROM story_revisions WHERE story_id=? AND version=?",
            (story_id, version),
        ).fetchone()
        if row is None:
            raise NotFoundError("故事版本不存在")
        return json.loads(row["content_json"])

    def _validated_content(self, content: Any) -> dict[str, Any]:
        try:
            return validate_content(content)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _create_revision(self, connection, story_row, *, revision_type: str,
                         content: dict[str, Any], change_reason: str, actor_id: str) -> int:
        """写入一个不可变修订并重置三步复核队列。"""

        story_id = story_row["story_id"]
        current = story_row["current_version"]
        previous = connection.execute(
            "SELECT content_hash FROM story_revisions WHERE story_id=? AND version=?",
            (story_id, current),
        ).fetchone()
        parent_hash = previous["content_hash"] if previous else GENESIS_HASH
        material = {"content": content, "parent_version": current, "parent_hash": parent_hash}
        content_hash = digest(material)
        next_version = current + 1
        now = self._now()
        connection.execute(
            "INSERT INTO story_revisions(story_id,version,revision_type,content_json,content_hash,"
            "change_reason,created_by,created_at,parent_version) VALUES(?,?,?,?,?,?,?,?,?)",
            (story_id, next_version, revision_type, canonical_json(content), content_hash,
             change_reason, actor_id, now, current if previous else None),
        )
        for stage in REVIEW_STAGES:
            connection.execute(
                "INSERT INTO review_tasks(story_id,version,stage,status,created_at) VALUES(?,?,?,?,?)",
                (story_id, next_version, stage, "pending", now),
            )
        connection.execute(
            "UPDATE stories SET current_version=? WHERE story_id=? AND current_version=?",
            (next_version, story_id, current),
        )
        append_event(connection, actor_id=actor_id, action="story.revision_created",
                     resource_type="story", resource_id=story_id,
                     detail={"version": next_version, "revision_type": revision_type,
                             "parent_version": current, "change_reason": change_reason,
                             "content_hash": content_hash},
                     occurred_at=now)
        return next_version

    @staticmethod
    def _latest_consent(connection, story_id: str, subject_key: str):
        return connection.execute(
            "SELECT * FROM consent_statements WHERE story_id=? AND subject_key=? "
            "ORDER BY version DESC LIMIT 1",
            (story_id, subject_key),
        ).fetchone()

    def _subject_scopes(self, connection, story_id: str, subject_keys: list[str]) -> dict[str, str | None]:
        """返回每位叙述对象最新有效授权级别；未授权或已撤回为 None。"""

        scopes: dict[str, str | None] = {}
        for key in subject_keys:
            row = self._latest_consent(connection, story_id, key)
            if row is None or not row["granted"]:
                scopes[key] = None
            else:
                scopes[key] = row["scope"]
        return scopes

    @staticmethod
    def _effective_scope(subject_scopes: dict[str, str | None]) -> str | None:
        """所有有效授权的安全交集；任一无授权即整体不得公开。"""

        if not subject_scopes or any(scope is None for scope in subject_scopes.values()):
            return None
        return min_scope(*[scope for scope in subject_scopes.values() if scope is not None])

    def _marks(self, connection, story_id: str, version: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM redaction_marks WHERE story_id=? AND version=? ORDER BY created_at, mark_id",
            (story_id, version),
        ).fetchall()
        return [dict(row) for row in rows]

    # ----- 采集与版本 -----

    def submit_story(self, *, request_id: str, actor_id: str, site_id: str,
                     external_key: str, content: dict[str, Any]) -> Any:
        payload = {"actor_id": actor_id, "site_id": site_id, "external_key": external_key,
                   "content": content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._ensure_site_org(actor, site)
            external_key = self.base._identifier(external_key, "external_key")

            def create() -> tuple[str, str, dict[str, Any]]:
                normalized = self._validated_content(content)
                duplicate = connection.execute(
                    "SELECT 1 FROM stories WHERE site_id=? AND external_key=?",
                    (site_id, external_key),
                ).fetchone()
                if duplicate:
                    raise ConflictError("同一场所下故事业务键已经存在")
                story_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO stories(story_id,site_id,external_key,submitter_actor_id,"
                    "current_version,published_version,created_at) VALUES(?,?,?,?,1,NULL,?)",
                    (story_id, site_id, external_key, actor_id, now),
                )
                content_hash = digest({"content": normalized, "parent_version": 0,
                                       "parent_hash": GENESIS_HASH})
                connection.execute(
                    "INSERT INTO story_revisions(story_id,version,revision_type,content_json,"
                    "content_hash,change_reason,created_by,created_at,parent_version) "
                    "VALUES(?,1,'submission',?,?,'首次提交',?,?,NULL)",
                    (story_id, canonical_json(normalized), content_hash, actor_id, now),
                )
                for stage in REVIEW_STAGES:
                    connection.execute(
                        "INSERT INTO review_tasks(story_id,version,stage,status,created_at) "
                        "VALUES(?,1,?,'pending',?)",
                        (story_id, stage, now),
                    )
                append_event(connection, actor_id=actor_id, action="story.submitted",
                             resource_type="story", resource_id=story_id,
                             detail={"site_id": site_id, "external_key": external_key,
                                     "subjects": [s["key"] for s in normalized["subjects"]],
                                     "content_hash": content_hash},
                             occurred_at=now)
                return "story", story_id, {"story_id": story_id, "version": 1}

            return self._idempotent(connection, request_id=request_id, action="submit_story",
                                    payload=payload, create=create)

    def submit_correction(self, *, request_id: str, actor_id: str, story_id: str,
                          content: dict[str, Any], change_reason: str) -> Any:
        change_reason = self.base._text(change_reason, "change_reason", 500)
        payload = {"actor_id": actor_id, "story_id": story_id, "content": content,
                   "change_reason": change_reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)

            def create() -> tuple[str, str, dict[str, Any]]:
                normalized = self._validated_content(content)
                version = self._create_revision(
                    connection, story, revision_type="correction", content=normalized,
                    change_reason=f"提交者更正：{change_reason}", actor_id=actor_id,
                )
                return "story", story_id, {"story_id": story_id, "version": version}

            return self._idempotent(connection, request_id=request_id, action="submit_correction",
                                    payload=payload, create=create)

    # ----- 同意管理 -----

    def record_consent(self, *, request_id: str, actor_id: str, story_id: str,
                       subject_key: str, scope: str, granted: bool,
                       statement_type: str, reason: str) -> Any:
        subject_key = str(subject_key).strip()
        if not subject_key.startswith("subject:"):
            raise ValidationError("subject_key 必须以 subject: 开头")
        if statement_type not in CONSENT_TYPES:
            raise ValidationError("statement_type 不在允许范围内")
        if not isinstance(granted, bool):
            raise ValidationError("granted 必须是布尔值")
        reason = self.base._text(reason, "reason", 500)
        normalized_scope = normalize_scope(scope) if granted else "private"
        payload = {"actor_id": actor_id, "story_id": story_id, "subject_key": subject_key,
                   "scope": normalized_scope, "granted": granted,
                   "statement_type": statement_type, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)

            def create() -> tuple[str, str, dict[str, Any]]:
                current_content = self._content(connection, story_id, story["current_version"])
                known = {subject["key"] for subject in current_content["subjects"]}
                if subject_key not in known:
                    raise ValidationError("当前版本中不存在该叙述对象；补充人物关系请先提交更正修订")
                prior = connection.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM consent_statements "
                    "WHERE story_id=? AND subject_key=?",
                    (story_id, subject_key),
                ).fetchone()
                version = prior["version"] + 1
                statement_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO consent_statements(statement_id,story_id,subject_key,scope,granted,"
                    "statement_type,reason,version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (statement_id, story_id, subject_key, normalized_scope, 1 if granted else 0,
                     statement_type, reason, version, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="story.consent_recorded",
                             resource_type="consent_statement", resource_id=statement_id,
                             detail={"story_id": story_id, "subject_key": subject_key,
                                     "scope": normalized_scope, "granted": granted,
                                     "statement_type": statement_type, "version": version},
                             occurred_at=now)
                return "consent_statement", statement_id, {
                    "statement_id": statement_id, "version": version,
                }

            return self._idempotent(connection, request_id=request_id, action="record_consent",
                                    payload=payload, create=create)

    def narrow_visibility(self, *, request_id: str, actor_id: str, story_id: str,
                          scope: str, reason: str,
                          subject_keys: list[str] | None = None) -> Any:
        """提交者请求缩小未来可见范围（不删除任何历史授权与审计）。"""

        target_scope = normalize_scope(scope)
        reason = self.base._text(reason, "reason", 500)
        payload = {"actor_id": actor_id, "story_id": story_id, "scope": target_scope,
                   "reason": reason, "subject_keys": subject_keys}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            story = self._story(connection, story_id)
            if actor.actor_id != story["submitter_actor_id"] and actor.role != "admin":
                raise PermissionDenied("只有原提交者或管理员可以请求缩小可见范围")
            self._ensure_story_org(connection, actor, story)

            def create() -> tuple[str, str, dict[str, Any]]:
                content = self._content(connection, story_id, story["current_version"])
                all_keys = [s["key"] for s in content["subjects"]]
                targets = subject_keys or all_keys
                if not isinstance(targets, list) or not targets:
                    raise ValidationError("subject_keys 必须是非空数组")
                for key in targets:
                    if key not in all_keys:
                        raise ValidationError(f"当前版本中不存在该叙述对象: {key}")
                    latest = self._latest_consent(connection, story_id, key)
                    current_scope = latest["scope"] if latest is not None and latest["granted"] else None
                    if current_scope is None:
                        raise ConflictError(f"{key} 当前没有有效授权，无需缩小；如需撤回请登记拒绝授权声明")
                    if SCOPE_LEVELS[target_scope] >= SCOPE_LEVELS[current_scope]:
                        raise ConflictError(
                            f"缩小后的级别必须严格小于 {key} 当前有效级别 {current_scope}"
                        )
                created: list[str] = []
                now = self._now()
                for key in targets:
                    statement_id = uuid.uuid4().hex
                    prior = connection.execute(
                        "SELECT COALESCE(MAX(version),0) AS version FROM consent_statements "
                        "WHERE story_id=? AND subject_key=?",
                        (story_id, key),
                    ).fetchone()
                    version = prior["version"] + 1
                    connection.execute(
                        "INSERT INTO consent_statements(statement_id,story_id,subject_key,scope,"
                        "granted,statement_type,reason,version,created_by,created_at) "
                        "VALUES(?,?,?,?,1,'narrowing',?,?,?,?)",
                        (statement_id, story_id, key, target_scope, reason, version, actor_id, now),
                    )
                    created.append(statement_id)
                # 发布标记仍然保留，但未来访问按新交集即时收窄；若已低于发布级别则不可见。
                append_event(connection, actor_id=actor_id, action="story.visibility_narrowed",
                             resource_type="story", resource_id=story_id,
                             detail={"scope": target_scope, "subject_keys": targets,
                                     "published_version": story["published_version"],
                                     "statements": created},
                             occurred_at=now)
                return "story", story_id, {"story_id": story_id, "scope": target_scope,
                                           "statement_ids": created}

            return self._idempotent(connection, request_id=request_id, action="narrow_visibility",
                                    payload=payload, create=create)

    # ----- 脱敏标注 -----

    def add_redaction_mark(self, *, request_id: str, actor_id: str, story_id: str,
                           target: str, action: str, reason: str,
                           version: int | None = None, min_scope: str | None = None,
                           replacement: str | None = None) -> Any:
        target = str(target).strip()
        if not (target == "summary" or target.startswith("citations")):
            raise ValidationError("target 只支持 summary 或 citations 下的结构化引用")
        if action not in REDACTION_ACTIONS:
            raise ValidationError("action 不在允许范围内")
        threshold = normalize_scope(min_scope) if min_scope else "community"
        if action == "replace" and not str(replacement or "").strip():
            raise ValidationError("replace 动作必须提供 replacement")
        reason = self.base._text(reason, "reason", 500)
        payload = {"actor_id": actor_id, "story_id": story_id, "target": target,
                   "action": action, "reason": reason, "version": version,
                   "min_scope": threshold, "replacement": replacement}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)
            target_version = version or story["current_version"]

            def create() -> tuple[str, str, dict[str, Any]]:
                self._revision(connection, story_id, target_version)
                content = self._content(connection, story_id, target_version)
                if target != "summary" and not self._target_exists(target, content):
                    raise ValidationError("脱敏目标在该版本中不存在")
                task = connection.execute(
                    "SELECT * FROM review_tasks WHERE story_id=? AND version=? AND stage='sensitive_check'",
                    (story_id, target_version),
                ).fetchone()
                if task is None or task["status"] != "pending":
                    raise ConflictError("该版本的敏感信息复核已经结束，不能再追加标注")
                mark_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO redaction_marks(mark_id,story_id,version,target,action,min_scope,"
                    "replacement,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (mark_id, story_id, target_version, target, action, threshold,
                     replacement, reason, actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="story.redaction_marked",
                             resource_type="redaction_mark", resource_id=mark_id,
                             detail={"story_id": story_id, "version": target_version,
                                     "target": target, "action": action, "min_scope": threshold},
                             occurred_at=now)
                return "redaction_mark", mark_id, {"mark_id": mark_id}

            return self._idempotent(connection, request_id=request_id, action="add_redaction_mark",
                                    payload=payload, create=create)

    @staticmethod
    def _target_exists(target: str, content: dict[str, Any]) -> bool:
        path = target[len("citations"):].lstrip(".")
        head, _, tail = path.partition(".")
        index_text = head.strip("[]")
        if not index_text.isdigit():
            return False
        index = int(index_text)
        citations = content.get("citations", [])
        if not isinstance(citations, list) or index >= len(citations):
            return False
        if not tail:
            return True
        return isinstance(citations[index], dict) and tail in citations[index]

    # ----- 复核队列与租约 -----

    def _task(self, connection, story_id: str, version: int, stage: str):
        if stage not in REVIEW_STAGES:
            raise ValidationError("stage 必须是 fact_check / sensitive_check / consent_confirm 之一")
        task = connection.execute(
            "SELECT * FROM review_tasks WHERE story_id=? AND version=? AND stage=?",
            (story_id, version, stage),
        ).fetchone()
        if task is None:
            raise NotFoundError("该版本没有对应的复核任务")
        return task

    def claim_review(self, *, request_id: str, actor_id: str, story_id: str, stage: str) -> Any:
        payload = {"actor_id": actor_id, "story_id": story_id, "stage": stage}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)
            version = story["current_version"]
            now_dt = self._now_dt()

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task(connection, story_id, version, stage)
                if task["status"] in ("approved", "returned"):
                    raise ConflictError("该复核步骤已经结束")
                if task["lease_holder"] and self._parse(task["lease_expires_at"]) > now_dt:
                    if task["lease_holder"] == actor_id:
                        return "review_task", f"{story_id}:{version}:{stage}", {
                            "story_id": story_id, "version": version, "stage": stage,
                            "lease_holder": actor_id, "reclaimed": True,
                        }
                    raise ConflictError("复核租约仍被其他审核人持有")
                expires = now_dt + timedelta(seconds=self.lease_seconds)
                expires_text = expires.isoformat().replace("+00:00", "Z")
                connection.execute(
                    "UPDATE review_tasks SET lease_holder=?, lease_expires_at=? "
                    "WHERE story_id=? AND version=? AND stage=?",
                    (actor_id, expires_text, story_id, version, stage),
                )
                append_event(connection, actor_id=actor_id, action="story.review_claimed",
                             resource_type="review_task",
                             resource_id=f"{story_id}:{version}:{stage}",
                             detail={"story_id": story_id, "version": version, "stage": stage,
                                     "lease_expires_at": expires_text},
                             occurred_at=self._now())
                return "review_task", f"{story_id}:{version}:{stage}", {
                    "story_id": story_id, "version": version, "stage": stage,
                    "lease_holder": actor_id, "lease_expires_at": expires_text,
                }

            return self._idempotent(connection, request_id=request_id, action="claim_review",
                                    payload=payload, create=create)

    def decide_review(self, *, request_id: str, actor_id: str, story_id: str, stage: str,
                      decision: str, opinion: str) -> Any:
        if decision not in ("approve", "return"):
            raise ValidationError("decision 必须是 approve 或 return")
        opinion = self.base._text(opinion, "opinion", 1000)
        payload = {"actor_id": actor_id, "story_id": story_id, "stage": stage,
                   "decision": decision, "opinion": opinion}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)
            version = story["current_version"]
            now_dt = self._now_dt()

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task(connection, story_id, version, stage)
                if task["status"] != "pending":
                    raise ConflictError("该复核步骤已经作出决定，旧意见不能被覆盖")
                if task["lease_holder"] != actor_id:
                    raise PermissionDenied("只有持有当前租约的审核人能作出决定")
                if not task["lease_expires_at"] or self._parse(task["lease_expires_at"]) <= now_dt:
                    raise ConflictError("审核租约已过期，旧审核人不得继续决定")
                if decision == "approve":
                    other = connection.execute(
                        "SELECT stage FROM review_tasks WHERE story_id=? AND version=? "
                        "AND status='approved' AND decided_by=?",
                        (story_id, version, actor_id),
                    ).fetchall()
                    if other:
                        raise ConflictError("三步复核必须由不同责任人分别完成")
                now = self._now()
                connection.execute(
                    "UPDATE review_tasks SET status=?, decided_by=?, decision=?, opinion_json=?, "
                    "decided_at=?, lease_holder=NULL, lease_expires_at=NULL "
                    "WHERE story_id=? AND version=? AND stage=?",
                    ("approved" if decision == "approve" else "returned", actor_id, decision,
                     canonical_json({"opinion": opinion}), now, story_id, version, stage),
                )
                append_event(connection, actor_id=actor_id,
                             action=f"story.review_{decision}d",
                             resource_type="review_task",
                             resource_id=f"{story_id}:{version}:{stage}",
                             detail={"story_id": story_id, "version": version, "stage": stage,
                                     "decision": decision, "opinion": opinion},
                             occurred_at=now)
                if decision == "return":
                    # 退回生成新的修订，原版本意见原样保留，新版本队列重置。
                    content = self._content(connection, story_id, version)
                    refreshed = self._story(connection, story_id)
                    new_version = self._create_revision(
                        connection, refreshed, revision_type="review_return", content=content,
                        change_reason=f"{stage} 退回：{opinion}", actor_id=actor_id,
                    )
                    return "story", story_id, {"story_id": story_id, "version": new_version,
                                               "returned_from": version, "stage": stage}
                return "review_task", f"{story_id}:{version}:{stage}", {
                    "story_id": story_id, "version": version, "stage": stage, "decision": decision,
                }

            return self._idempotent(connection, request_id=request_id, action="decide_review",
                                    payload=payload, create=create)

    # ----- 发布与开放视图 -----

    def publish(self, *, request_id: str, actor_id: str, story_id: str) -> Any:
        payload = {"actor_id": actor_id, "story_id": story_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)

            def create() -> tuple[str, str, dict[str, Any]]:
                version = story["current_version"]
                tasks = connection.execute(
                    "SELECT stage,status,decided_by FROM review_tasks WHERE story_id=? AND version=?",
                    (story_id, version),
                ).fetchall()
                if len(tasks) != len(REVIEW_STAGES):
                    raise ConflictError("复核任务不完整，不能发布")
                if any(row["status"] != "approved" for row in tasks):
                    raise ConflictError("仍有复核步骤未通过，不能发布")
                content = self._content(connection, story_id, version)
                subject_scopes = self._subject_scopes(
                    connection, story_id, [s["key"] for s in content["subjects"]]
                )
                missing = [key for key, scope in subject_scopes.items() if scope is None]
                if missing:
                    raise ConflictError(f"叙述对象缺少有效授权，不能发布: {', '.join(sorted(missing))}")
                effective = self._effective_scope(subject_scopes)
                connection.execute(
                    "UPDATE stories SET published_version=? WHERE story_id=?",
                    (version, story_id),
                )
                append_event(connection, actor_id=actor_id, action="story.published",
                             resource_type="story", resource_id=story_id,
                             detail={"version": version, "effective_scope": effective,
                                     "subject_scopes": subject_scopes},
                             occurred_at=self._now())
                return "story", story_id, {"story_id": story_id, "version": version,
                                           "effective_scope": effective}

            return self._idempotent(connection, request_id=request_id, action="publish_story",
                                    payload=payload, create=create)

    def published_view(self, *, actor_id: str, story_id: str, scope: str = "archive") -> dict[str, Any]:
        requested = normalize_scope(scope)
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)
            version = story["published_version"]
            if version is None:
                raise NotFoundError("该故事尚未发布")
            revision = self._revision(connection, story_id, version)
            content = json.loads(revision["content_json"])
            subject_keys = [s["key"] for s in content["subjects"]]
            subject_scopes = self._subject_scopes(connection, story_id, subject_keys)
            effective = self._effective_scope(subject_scopes)
            denied = effective is None or not is_within(effective, requested)
            if denied:
                # 拒绝访问的审计事件必须落库：先在本事务提交，事务外再抛错。
                append_event(connection, actor_id=actor_id, action="story.access_denied",
                             resource_type="story", resource_id=story_id,
                             detail={"version": version, "requested_scope": requested,
                                     "effective_scope": effective, "subject_scopes": subject_scopes},
                             occurred_at=self._now())
            else:
                served = min_scope(effective, requested)
                marks = self._marks(connection, story_id, version)
                view = apply_redactions(content, marks, served)
                applied = explain_redactions(marks, served)
                append_event(connection, actor_id=actor_id, action="story.accessed",
                             resource_type="story", resource_id=story_id,
                             detail={"version": version, "requested_scope": requested,
                                     "served_scope": served, "effective_scope": effective,
                                     "redactions_applied": len(applied)},
                             occurred_at=self._now())
                result = {
                    "story_id": story_id,
                    "version": version,
                    "requested_scope": requested,
                    "effective_scope": effective,
                    "served_scope": served,
                    "subject_scopes": subject_scopes,
                    "redactions_applied": applied,
                    "content": view,
                }
        if denied:
            raise NotFoundError("该材料在请求的可见范围内不可见")
        return result

    # ----- 审计解释 -----

    def explain_visibility(self, *, actor_id: str, story_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            story = self._story(connection, story_id)
            self._ensure_story_org(connection, actor, story)
            revisions = connection.execute(
                "SELECT * FROM story_revisions WHERE story_id=? ORDER BY version", (story_id,)
            ).fetchall()
            versions: list[dict[str, Any]] = []
            for revision in revisions:
                version = revision["version"]
                tasks = connection.execute(
                    "SELECT * FROM review_tasks WHERE story_id=? AND version=? "
                    "ORDER BY CASE stage WHEN 'fact_check' THEN 0 "
                    "WHEN 'sensitive_check' THEN 1 WHEN 'consent_confirm' THEN 2 END",
                    (story_id, version),
                ).fetchall()
                versions.append({
                    "version": version,
                    "revision_type": revision["revision_type"],
                    "change_reason": revision["change_reason"],
                    "created_by": revision["created_by"],
                    "created_at": revision["created_at"],
                    "parent_version": revision["parent_version"],
                    "content_hash": revision["content_hash"],
                    "reviews": [
                        {
                            "stage": row["stage"],
                            "status": row["status"],
                            "lease_holder": row["lease_holder"],
                            "lease_expires_at": row["lease_expires_at"],
                            "decided_by": row["decided_by"],
                            "decision": row["decision"],
                            "opinion": json.loads(row["opinion_json"])["opinion"]
                            if row["opinion_json"] else None,
                            "decided_at": row["decided_at"],
                        }
                        for row in tasks
                    ],
                    "marks": [
                        {
                            "mark_id": row["mark_id"], "target": row["target"],
                            "action": row["action"], "min_scope": row["min_scope"],
                            "replacement": row["replacement"], "reason": row["reason"],
                            "created_by": row["created_by"], "created_at": row["created_at"],
                        }
                        for row in self._marks(connection, story_id, version)
                    ],
                })
            consent_rows = connection.execute(
                "SELECT * FROM consent_statements WHERE story_id=? ORDER BY subject_key, version",
                (story_id,),
            ).fetchall()
            consents: dict[str, list[dict[str, Any]]] = {}
            for row in consent_rows:
                consents.setdefault(row["subject_key"], []).append({
                    "statement_id": row["statement_id"], "version": row["version"],
                    "scope": row["scope"], "granted": bool(row["granted"]),
                    "statement_type": row["statement_type"], "reason": row["reason"],
                    "created_by": row["created_by"], "created_at": row["created_at"],
                })
            published: dict[str, Any] | None = None
            if story["published_version"] is not None:
                pv = story["published_version"]
                content = self._content(connection, story_id, pv)
                subject_scopes = self._subject_scopes(
                    connection, story_id, [s["key"] for s in content["subjects"]]
                )
                effective = self._effective_scope(subject_scopes)
                marks = self._marks(connection, story_id, pv)
                published = {
                    "version": pv,
                    "subject_scopes": subject_scopes,
                    "effective_scope": effective,
                    "hidden_reason": None if effective is not None else "存在未授权或已撤回的叙述对象",
                    "views_at_scope": {
                        scope: {
                            "visible": effective is not None and is_within(effective, scope),
                            "served_scope": min_scope(effective, scope)
                            if effective is not None and is_within(effective, scope) else None,
                            "applied_marks": explain_redactions(
                                marks, min_scope(effective, scope)
                            ) if effective is not None and is_within(effective, scope) else [],
                        }
                        for scope in SCOPE_LEVELS
                    },
                }
            append_event(connection, actor_id=actor_id, action="story.visibility_explained",
                         resource_type="story", resource_id=story_id,
                         detail={"published_version": story["published_version"]},
                         occurred_at=self._now())
            return {
                "story_id": story_id,
                "site_id": story["site_id"],
                "external_key": story["external_key"],
                "submitter_actor_id": story["submitter_actor_id"],
                "current_version": story["current_version"],
                "published_version": story["published_version"],
                "versions": versions,
                "consents": consents,
                "published": published,
            }

    def list_stories(self, *, actor_id: str, site_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=False) as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._ensure_site_org(actor, site)
            rows = connection.execute(
                "SELECT * FROM stories WHERE site_id=? ORDER BY created_at, story_id", (site_id,)
            ).fetchall()
            items = []
            for row in rows:
                tasks = connection.execute(
                    "SELECT stage,status FROM review_tasks WHERE story_id=? AND version=?",
                    (row["story_id"], row["current_version"]),
                ).fetchall()
                items.append({
                    "story_id": row["story_id"],
                    "external_key": row["external_key"],
                    "submitter_actor_id": row["submitter_actor_id"],
                    "current_version": row["current_version"],
                    "published_version": row["published_version"],
                    "review_status": {row["stage"]: row["status"] for row in tasks},
                })
            return {"site_id": site_id, "items": items}

    # ----- 批量导入（全有或全无 + 来源重放识别） -----

    def import_batch(self, *, request_id: str, actor_id: str, site_id: str,
                     source_id: str, items: list[dict[str, Any]]) -> Any:
        source_id = self.base._identifier(source_id, "source_id")
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须是非空数组")
        normalized_items: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for raw in items:
            if not isinstance(raw, dict):
                raise ValidationError("批量条目必须是对象")
            external_key = self.base._identifier(str(raw.get("external_key", "")), "external_key")
            if external_key in seen_keys:
                raise ValidationError(f"批次内业务键重复: {external_key}")
            seen_keys.add(external_key)
            content = self._validated_content(raw.get("content"))
            consents = []
            for consent in raw.get("consents", []):
                if not isinstance(consent, dict):
                    raise ValidationError("consents 条目必须是对象")
                subject_key = str(consent.get("subject_key", "")).strip()
                if subject_key not in {s["key"] for s in content["subjects"]}:
                    raise ValidationError(f"授权引用了条目中不存在的叙述对象: {subject_key}")
                granted = bool(consent.get("granted"))
                scope = normalize_scope(consent["scope"]) if granted else "private"
                statement_type = str(consent.get("statement_type", "initial"))
                if statement_type not in CONSENT_TYPES:
                    raise ValidationError("statement_type 不在允许范围内")
                reason = self.base._text(str(consent.get("reason", "")), "consent.reason", 500)
                consents.append({"subject_key": subject_key, "scope": scope, "granted": granted,
                                 "statement_type": statement_type, "reason": reason})
            normalized_items.append({"external_key": external_key, "content": content,
                                     "consents": consents})
        batch_hash = digest({"source_id": source_id, "items": normalized_items})
        payload = {"actor_id": actor_id, "site_id": site_id, "source_id": source_id,
                   "batch_hash": batch_hash}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site(connection, site_id)
            self._ensure_site_org(actor, site)
            request_id = self.base._identifier(request_id, "request_id")
            payload_hash = digest(payload)
            receipt_row = connection.execute(
                "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
            ).fetchone()
            if receipt_row:
                if receipt_row["action"] != "import_batch" or receipt_row["payload_hash"] != payload_hash:
                    raise ConflictError("request_id 已被不同内容使用")
                return WriteReceipt(request_id, receipt_row["resource_type"],
                                    receipt_row["resource_id"], True,
                                    json.loads(receipt_row["response_json"]))

            def create() -> tuple[str, str, dict[str, Any], bool]:
                # 先校验整批，任何一条失败都在写入前抛错，整批回滚。
                existing_row = connection.execute(
                    "SELECT * FROM import_batches WHERE source_id=?", (source_id,)
                ).fetchone()
                if existing_row:
                    if existing_row["batch_hash"] != batch_hash:
                        raise ConflictError("同一来源已经上传过不同内容的批次，禁止覆盖")
                    return "import_batch", source_id, {
                        "source_id": source_id,
                        "story_ids": json.loads(existing_row["story_ids_json"]),
                        "source_replay": True,
                    }, True
                for item in normalized_items:
                    if connection.execute(
                        "SELECT 1 FROM stories WHERE site_id=? AND external_key=?",
                        (site_id, item["external_key"]),
                    ).fetchone():
                        raise ConflictError(f"业务键已经存在，整批拒绝: {item['external_key']}")

                now = self._now()
                story_ids: list[str] = []
                for item in normalized_items:
                    story_id = uuid.uuid4().hex
                    story_ids.append(story_id)
                    connection.execute(
                        "INSERT INTO stories(story_id,site_id,external_key,submitter_actor_id,"
                        "current_version,published_version,created_at) VALUES(?,?,?,?,1,NULL,?)",
                        (story_id, site_id, item["external_key"], actor_id, now),
                    )
                    content_hash = digest({"content": item["content"], "parent_version": 0,
                                           "parent_hash": GENESIS_HASH})
                    connection.execute(
                        "INSERT INTO story_revisions(story_id,version,revision_type,content_json,"
                        "content_hash,change_reason,created_by,created_at,parent_version) "
                        "VALUES(?,1,'batch_import',?,?,'批量导入',?,?,NULL)",
                        (story_id, canonical_json(item["content"]), content_hash, actor_id, now),
                    )
                    for stage in REVIEW_STAGES:
                        connection.execute(
                            "INSERT INTO review_tasks(story_id,version,stage,status,created_at) "
                            "VALUES(?,1,?,'pending',?)",
                            (story_id, stage, now),
                        )
                    for consent in item["consents"]:
                        connection.execute(
                            "INSERT INTO consent_statements(statement_id,story_id,subject_key,"
                            "scope,granted,statement_type,reason,version,created_by,created_at) "
                            "VALUES(?,?,?,?,?,?,?,1,?,?)",
                            (uuid.uuid4().hex, story_id, consent["subject_key"], consent["scope"],
                             1 if consent["granted"] else 0, consent["statement_type"],
                             consent["reason"], actor_id, now),
                        )
                    append_event(connection, actor_id=actor_id, action="story.submitted",
                                 resource_type="story", resource_id=story_id,
                                 detail={"site_id": site_id, "external_key": item["external_key"],
                                         "subjects": [s["key"] for s in item["content"]["subjects"]],
                                         "source_id": source_id, "batch_import": True},
                                 occurred_at=now)
                connection.execute(
                    "INSERT INTO import_batches(source_id,batch_hash,story_ids_json,created_by,"
                    "created_at) VALUES(?,?,?,?,?)",
                    (source_id, batch_hash, canonical_json(story_ids), actor_id, now),
                )
                append_event(connection, actor_id=actor_id, action="story.batch_imported",
                             resource_type="import_batch", resource_id=source_id,
                             detail={"source_id": source_id, "count": len(story_ids),
                                     "batch_hash": batch_hash},
                             occurred_at=now)
                return "import_batch", source_id, {"source_id": source_id,
                                                   "story_ids": story_ids,
                                                   "source_replay": False}, False

            resource_type, resource_id, response, source_replay = create()
            connection.execute(
                "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
                "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, "import_batch", payload_hash, resource_type, resource_id,
                 canonical_json(response), self._now()),
            )
            return WriteReceipt(request_id, resource_type, resource_id, source_replay)
