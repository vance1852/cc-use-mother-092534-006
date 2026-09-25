"""家风故事采集与分级开放领域服务。

在基础层（身份、场所、幂等、事务、哈希审计）之上实现：
- 不可变故事版本与结构化人物引用；
- 按叙述对象登记、可收窄且全程留痕的授权；
- 事实核对 / 敏感信息复核 / 授权确认三阶段复核队列与租约；
- 面向不同开放范围的脱敏视图与不可变访问审计；
- 全有或全无、可识别来源重放的批量导入。
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Callable

from festival_foundation.audit import append_event, canonical_json, digest
from festival_foundation.clock import Clock, SystemClock
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.models import Actor, WriteReceipt
from festival_foundation.storage import Database

from .domain import (
    CHANGE_REASONS,
    CONSENT_CHANNELS,
    SCOPES,
    SCOPE_RANK,
    STAGES,
    allows,
    is_scope,
    next_stage,
    scope_rank,
)
from .storage import FamilyStorage

# 各复核阶段允许持有的角色：三个阶段由不同角色承担，审计员只读不裁决。
STAGE_ROLES = {
    "fact_check": ("reviewer", "admin"),
    "sensitive_review": ("admin",),
    "consent_confirm": ("operator", "admin"),
}
VIEW_FIELDS = ("summary", "outline", "person_names")
AUDIENCE_SCOPES = ("class", "community", "public")


class FamilyStoryService:
    """协调家风故事的版本、同意、复核、脱敏与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None, lease_seconds: int = 900) -> None:
        self.database = database
        self.storage = FamilyStorage(database)
        self.connection = database.connection
        self.clock = clock or SystemClock()
        self.lease_seconds = lease_seconds

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not value or len(value) > 64:
            raise ValidationError(f"{field} 不能为空且不能超过 64 个字符")
        return value

    def _text(self, value: str, field: str, limit: int) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _site_scope(self, connection, actor: Actor, site_id: str):
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的场所")
        return site

    def _story_scope(self, connection, actor: Actor, story_id: str):
        story = connection.execute("SELECT * FROM stories WHERE story_id=?", (story_id,)).fetchone()
        if story is None:
            raise NotFoundError("家风故事不存在")
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (story["site_id"],)).fetchone()
        if actor.organization_id != site["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的故事")
        return story, site

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # ------------------------------------------------------------------ 校验

    def _validate_persons(self, persons: Any) -> list[dict[str, str]]:
        if not isinstance(persons, list) or not persons:
            raise ValidationError("persons 必须是非空数组，每位叙述对象一条结构化引用")
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, person in enumerate(persons):
            if not isinstance(person, dict):
                raise ValidationError(f"persons[{index}] 必须是对象")
            subject_id = self._id(person.get("subject_id", ""), f"persons[{index}].subject_id")
            if subject_id in seen:
                raise ValidationError(f"persons 中 subject_id 重复：{subject_id}")
            seen.add(subject_id)
            relation = self._text(person.get("relation", ""), f"persons[{index}].relation", 100)
            name = str(person.get("name", "")).strip()
            if len(name) > 100:
                raise ValidationError(f"persons[{index}].name 不能超过 100 个字符")
            normalized.append({"subject_id": subject_id, "relation": relation, "name": name})
        return normalized

    def _validate_consents(self, consents: Any, persons: list[dict[str, str]]) -> dict[str, dict[str, str]]:
        if not isinstance(consents, list) or not consents:
            raise ValidationError("consents 必须是非空数组，每位叙述对象一条授权")
        by_subject: dict[str, dict[str, str]] = {}
        for index, consent in enumerate(consents):
            if not isinstance(consent, dict):
                raise ValidationError(f"consents[{index}] 必须是对象")
            subject_id = self._id(consent.get("subject_id", ""), f"consents[{index}].subject_id")
            if subject_id in by_subject:
                raise ValidationError(f"consents 中 subject_id 重复：{subject_id}")
            scope = str(consent.get("scope", "")).strip()
            if not is_scope(scope):
                raise ValidationError(f"consents[{index}].scope 必须是 {','.join(SCOPES)} 之一")
            relation = self._text(consent.get("relation", ""), f"consents[{index}].relation", 100)
            note = str(consent.get("note", "")).strip()
            if len(note) > 1000:
                raise ValidationError(f"consents[{index}].note 不能超过 1000 个字符")
            by_subject[subject_id] = {"scope": scope, "relation": relation, "note": note}
        known = {p["subject_id"] for p in persons}
        missing = known - set(by_subject)
        if missing:
            raise ValidationError(f"以下叙述对象缺少授权登记：{','.join(sorted(missing))}")
        unknown = set(by_subject) - known
        if unknown:
            raise ValidationError(f"授权引用了不在人物列表中的对象：{','.join(sorted(unknown))}")
        return by_subject

    def _content(self, title: str, outline: str, summary: str) -> tuple[str, str, str]:
        return (self._text(title, "title", 200),
                self._text(outline, "outline", 20000),
                self._text(summary, "summary", 2000))

    # ------------------------------------------------------------------ 版本写入

    def _insert_version(self, connection, *, story_id: str, version: int, title: str, outline: str,
                        summary: str, persons: list[dict[str, str]], change_reason: str,
                        created_by: str, supersedes: int | None,
                        source_category: str, source_record_id: str | None) -> str:
        content_hash = digest({"title": title, "outline": outline, "summary": summary, "persons": persons})
        connection.execute(
            "INSERT INTO story_versions(story_id,version,title,outline,summary,persons_json,source_category,"
            "source_record_id,change_reason,created_by,created_at,supersedes,content_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (story_id, version, title, outline, summary, canonical_json(persons), source_category,
             source_record_id, change_reason, created_by, self._now(), supersedes, content_hash),
        )
        return content_hash

    def _insert_consent(self, connection, *, story_id: str, subject_id: str, relation: str,
                        scope: str, channel: str, note: str, granted_by: str) -> str:
        consent_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO story_consents(consent_id,story_id,subject_id,relation,scope,channel,note,"
            "granted_by,created_at,superseded_by) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
            (consent_id, story_id, subject_id, relation, scope, channel, note, granted_by, self._now()),
        )
        return consent_id

    def submit_story(self, *, request_id: str, actor_id: str, site_id: str, story_id: str,
                     title: str, outline: str, summary: str, persons: list[dict[str, Any]],
                     consents: list[dict[str, Any]], source_category: str = "manual",
                     source_record_id: str | None = None) -> WriteReceipt:
        """采集一则家风故事，保存首版内容、人物引用与逐条授权。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "story_id": story_id, "title": title,
                   "outline": outline, "summary": summary, "persons": persons, "consents": consents}
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_scope(connection, actor, site_id)
            story_id = self._id(story_id, "story_id")
            persons = self._validate_persons(persons)
            consents_by_subject = self._validate_consents(consents, persons)
            title, outline, summary = self._content(title, outline, summary)
            source_category = self._text(source_category, "source_category", 40)

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM stories WHERE story_id=?", (story_id,)).fetchone():
                    raise ConflictError("故事编号已经存在")
                now = self._now()
                connection.execute(
                    "INSERT INTO stories(story_id,site_id,title,current_version,status,created_by,created_at) "
                    "VALUES(?,?,?,1,'in_review',?,?)",
                    (story_id, site_id, title, actor_id, now),
                )
                content_hash = self._insert_version(
                    connection, story_id=story_id, version=1, title=title, outline=outline, summary=summary,
                    persons=persons, change_reason="initial", created_by=actor_id, supersedes=None,
                    source_category=source_category, source_record_id=source_record_id,
                )
                for person in persons:
                    entry = consents_by_subject[person["subject_id"]]
                    self._insert_consent(
                        connection, story_id=story_id, subject_id=person["subject_id"],
                        relation=entry["relation"], scope=entry["scope"], channel="recorded",
                        note=entry["note"], granted_by=actor_id,
                    )
                append_event(connection, actor_id=actor_id, action="story.submitted",
                             resource_type="story", resource_id=story_id,
                             detail={"site_id": site_id, "version": 1, "content_hash": content_hash,
                                     "subjects": [p["subject_id"] for p in persons]},
                             occurred_at=self._now())
                return "story", story_id, {"story_id": story_id, "version": 1}

            return self._idempotent(connection, request_id=request_id, action="submit_story",
                                    payload=payload, create=create)

    def revise_story(self, *, request_id: str, actor_id: str, story_id: str,
                     title: str, outline: str, summary: str, persons: list[dict[str, Any]],
                     change_reason: str = "edit") -> WriteReceipt:
        """提交修订：生成不可变新版本，旧版本及其复核意见原样保留。"""

        if change_reason not in CHANGE_REASONS or change_reason in ("initial", "import"):
            raise ValidationError("change_reason 必须是 edit、correction 或 review_return")
        payload = {"actor_id": actor_id, "story_id": story_id, "title": title, "outline": outline,
                   "summary": summary, "persons": persons, "change_reason": change_reason}
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            story, _ = self._story_scope(connection, actor, story_id)
            persons = self._validate_persons(persons)
            title, outline, summary = self._content(title, outline, summary)
            current_version = story["current_version"]
            if change_reason == "review_return" and story["status"] != "returned":
                raise ConflictError("只有被复核退回的故事才能以 review_return 原因修订")
            new_version = current_version + 1

            def create() -> tuple[str, str, dict[str, Any]]:
                content_hash = self._insert_version(
                    connection, story_id=story_id, version=new_version, title=title, outline=outline,
                    summary=summary, persons=persons, change_reason=change_reason,
                    created_by=actor_id, supersedes=current_version,
                    source_category="manual", source_record_id=None,
                )
                # 新版本进入全新的复核流水线；旧版本意见保留在旧版本上，不覆盖。
                connection.execute(
                    "UPDATE stories SET title=?, current_version=?, status='in_review' WHERE story_id=?",
                    (title, new_version, story_id),
                )
                append_event(connection, actor_id=actor_id, action="story.revised",
                             resource_type="story", resource_id=story_id,
                             detail={"version": new_version, "supersedes": current_version,
                                     "reason": change_reason, "content_hash": content_hash,
                                     "subjects": [p["subject_id"] for p in persons]},
                             occurred_at=self._now())
                return "story", story_id, {"story_id": story_id, "version": new_version}

            return self._idempotent(connection, request_id=request_id, action="revise_story",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 授权

    def _current_grant(self, connection, story_id: str, subject_id: str):
        return connection.execute(
            "SELECT * FROM story_consents WHERE story_id=? AND subject_id=? AND superseded_by IS NULL",
            (story_id, subject_id),
        ).fetchone()

    def record_consent(self, *, request_id: str, actor_id: str, story_id: str, subject_id: str,
                       relation: str, scope: str, note: str = "", channel: str = "recorded") -> WriteReceipt:
        """登记或更新某位叙述对象的授权；范围变化以新行取代旧行，历史不删除。"""

        if channel not in CONSENT_CHANNELS:
            raise ValidationError("channel 不合法")
        if not is_scope(scope):
            raise ValidationError(f"scope 必须是 {','.join(SCOPES)} 之一")
        payload = {"actor_id": actor_id, "story_id": story_id, "subject_id": subject_id,
                   "relation": relation, "scope": scope, "note": note, "channel": channel}
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._story_scope(connection, actor, story_id)
            subject_id = self._id(subject_id, "subject_id")
            relation = self._text(relation, "relation", 100)
            note = str(note).strip()[:1000]
            current = self._current_grant(connection, story_id, subject_id)
            if channel == "narrow_request":
                if current is None:
                    raise NotFoundError("该叙述对象尚无授权记录，无法收窄")
                if scope_rank(scope) >= scope_rank(current["scope"]):
                    raise ValidationError("收窄后的范围必须严格小于当前有效授权范围")

            def create() -> tuple[str, str, dict[str, Any]]:
                if current is not None:
                    if current["scope"] == scope:
                        raise ConflictError("该叙述对象的有效授权已经是该范围，无需重复登记")
                    consent_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO story_consents(consent_id,story_id,subject_id,relation,scope,channel,note,"
                        "granted_by,created_at,superseded_by) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                        (consent_id, story_id, subject_id, relation, scope, channel, note, actor_id, self._now()),
                    )
                    connection.execute(
                        "UPDATE story_consents SET superseded_by=? WHERE consent_id=?",
                        (consent_id, current["consent_id"]),
                    )
                    narrowed = scope_rank(scope) < scope_rank(current["scope"])
                    detail = {"subject_id": subject_id, "old_scope": current["scope"],
                              "new_scope": scope, "channel": channel, "narrowed": narrowed}
                    action = "consent.narrowed" if narrowed else "consent.scope_changed"
                else:
                    consent_id = self._insert_consent(
                        connection, story_id=story_id, subject_id=subject_id, relation=relation,
                        scope=scope, channel=channel, note=note, granted_by=actor_id,
                    )
                    detail = {"subject_id": subject_id, "new_scope": scope, "channel": channel}
                    action = "consent.recorded"
                append_event(connection, actor_id=actor_id, action=action, resource_type="story",
                             resource_id=story_id, detail=detail, occurred_at=self._now())
                return "consent", consent_id, {"consent_id": consent_id, "scope": scope}

            return self._idempotent(connection, request_id=request_id, action="record_consent",
                                    payload=payload, create=create)

    def narrow_consent(self, *, request_id: str, actor_id: str, story_id: str, subject_id: str,
                       scope: str, note: str = "") -> WriteReceipt:
        """提交者请求缩小未来可见范围：新范围必须严格窄于当前有效授权。"""

        subject_id = self._id(subject_id, "subject_id")
        with self.storage.transaction() as connection:
            current = self._current_grant(connection, story_id, subject_id)
            if current is None:
                raise NotFoundError("该叙述对象尚无授权记录，无法收窄")
            relation = current["relation"]
        return self.record_consent(request_id=request_id, actor_id=actor_id, story_id=story_id,
                                   subject_id=subject_id, relation=relation, scope=scope, note=note,
                                   channel="narrow_request")

    # ------------------------------------------------------------------ 有效范围

    def _version(self, connection, story_id: str, version: int | None) -> dict[str, Any]:
        current = connection.execute("SELECT current_version FROM stories WHERE story_id=?",
                                     (story_id,)).fetchone()
        if current is None:
            raise NotFoundError("家风故事不存在")
        version = version if version is not None else current["current_version"]
        row = connection.execute("SELECT * FROM story_versions WHERE story_id=? AND version=?",
                                 (story_id, version)).fetchone()
        if row is None:
            raise NotFoundError("故事版本不存在")
        return dict(row)

    def _effective_scope(self, connection, story_id: str, version_row: dict[str, Any]
                         ) -> tuple[str, list[dict[str, Any]]]:
        """公开范围取当前版本所有叙述对象有效授权的安全交集（最窄者决定）。"""

        persons = json.loads(version_row["persons_json"])
        subjects: list[dict[str, Any]] = []
        result_rank = SCOPE_RANK["public"]
        for person in persons:
            grant = self._current_grant(connection, story_id, person["subject_id"])
            if grant is None:
                # 后续补充的人物关系尚无授权：按最窄范围处理，立即拉低交集。
                scope = "private"
                subjects.append({"subject_id": person["subject_id"], "relation": person["relation"],
                                 "scope": scope, "has_valid_consent": False, "consent_id": None})
            else:
                scope = grant["scope"]
                subjects.append({"subject_id": person["subject_id"], "relation": person["relation"],
                                 "scope": scope, "has_valid_consent": True,
                                 "consent_id": grant["consent_id"]})
            result_rank = min(result_rank, SCOPE_RANK[scope])
        return SCOPES[result_rank], subjects

    # ------------------------------------------------------------------ 复核队列

    def _opinions(self, connection, story_id: str, version: int) -> dict[str, dict[str, Any]]:
        return {row["stage"]: dict(row) for row in connection.execute(
            "SELECT * FROM review_opinions WHERE story_id=? AND version=?", (story_id, version))}

    @staticmethod
    def _pending_stage(opinions: dict[str, dict[str, Any]]) -> str | None:
        for stage in STAGES:
            opinion = opinions.get(stage)
            if opinion is None or opinion["decision"] != "approved":
                return stage
        return None

    def claim_review(self, *, actor_id: str, story_id: str, stage: str | None = None) -> dict[str, Any]:
        """持有某阶段的审核租约；过期租约可被接管，旧持有人不得继续裁决。"""

        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            story, _ = self._story_scope(connection, actor, story_id)
            if story["created_by"] == actor_id:
                raise PermissionDenied("提交者不能复核自己采集的故事")
            version = story["current_version"]
            if story["status"] == "published":
                raise ConflictError("故事已经发布，无需复核")
            opinions = self._opinions(connection, story_id, version)
            if any(o["decision"] == "returned" for o in opinions.values()):
                raise ConflictError("当前版本已被退回，请等待提交者修订后再领取")
            pending = self._pending_stage(opinions)
            if pending is None:
                raise ConflictError("当前版本三个阶段均已通过")
            if stage is None:
                stage = pending
            if stage not in STAGES:
                raise ValidationError("stage 不合法")
            self._require(actor, *STAGE_ROLES[stage])
            ordered = STAGES.index(stage)
            for prior in STAGES[:ordered]:
                if opinions.get(prior, {}).get("decision") != "approved":
                    raise ConflictError(f"前置阶段 {prior} 尚未通过，不能进入 {stage}")
            if opinions.get(stage, {}).get("decision") == "approved":
                raise ConflictError(f"{stage} 已经通过")

            now = self._now()
            lease = connection.execute(
                "SELECT * FROM review_leases WHERE story_id=? AND version=? AND stage=?",
                (story_id, version, stage),
            ).fetchone()
            if lease is not None and not lease["consumed"] and lease["expires_at"] > now:
                if lease["reviewer_id"] != actor_id:
                    raise ConflictError("该阶段已被其他审核人持有，租约尚未过期")
                return {"lease_id": lease["lease_id"], "story_id": story_id, "version": version,
                        "stage": stage, "reviewer_id": actor_id, "expires_at": lease["expires_at"]}
            if lease is not None:
                expired = not lease["consumed"] and lease["expires_at"] <= now
                connection.execute("DELETE FROM review_leases WHERE lease_id=?", (lease["lease_id"],))
                if expired:
                    append_event(connection, actor_id=actor_id, action="review.lease_expired",
                                 resource_type="story", resource_id=story_id,
                                 detail={"version": version, "stage": stage,
                                         "previous_reviewer": lease["reviewer_id"],
                                         "lease_id": lease["lease_id"]}, occurred_at=now)
            lease_id = uuid.uuid4().hex
            expires_at = (self.clock.now() + timedelta(seconds=self.lease_seconds)).isoformat().replace("+00:00", "Z")
            connection.execute(
                "INSERT INTO review_leases(lease_id,story_id,version,stage,reviewer_id,"
                "leased_at,expires_at,consumed) VALUES(?,?,?,?,?,?,?,0)",
                (lease_id, story_id, version, stage, actor_id, now, expires_at),
            )
            append_event(connection, actor_id=actor_id, action="review.claimed",
                         resource_type="story", resource_id=story_id,
                         detail={"version": version, "stage": stage, "lease_id": lease_id,
                                 "expires_at": expires_at}, occurred_at=now)
            return {"lease_id": lease_id, "story_id": story_id, "version": version,
                    "stage": stage, "reviewer_id": actor_id, "expires_at": expires_at}

    def decide_review(self, *, request_id: str, actor_id: str, lease_id: str,
                      decision: str, note: str) -> WriteReceipt:
        """在有效租约内给出通过或退回；退回即终止该版本，修订另开不可变新版本。"""

        if decision not in ("approved", "returned"):
            raise ValidationError("decision 必须是 approved 或 returned")
        note = str(note).strip()
        if not note or len(note) > 2000:
            raise ValidationError("note 不能为空且不能超过 2000 个字符")
        payload = {"actor_id": actor_id, "lease_id": lease_id, "decision": decision, "note": note}
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            lease = connection.execute("SELECT * FROM review_leases WHERE lease_id=?",
                                       (lease_id,)).fetchone()
            if lease is None:
                raise NotFoundError("审核租约不存在")
            if lease["reviewer_id"] != actor_id:
                raise PermissionDenied("租约不属于当前操作者")
            if lease["consumed"]:
                raise ConflictError("该租约已经使用，不能重复决定")
            if self._now() > lease["expires_at"]:
                raise ConflictError("审核租约已过期，旧审核人不得继续决定")
            self._require(actor, *STAGE_ROLES[lease["stage"]])
            story_id, version, stage = lease["story_id"], lease["version"], lease["stage"]
            current = connection.execute("SELECT current_version FROM stories WHERE story_id=?",
                                         (story_id,)).fetchone()
            if current is not None and current["current_version"] != version:
                raise ConflictError("故事已有新修订，该租约对应的旧版本不再接受决定")

            def create() -> tuple[str, str, dict[str, Any]]:
                review_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO review_opinions(review_id,story_id,version,stage,reviewer_id,"
                    "decision,note,lease_id,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (review_id, story_id, version, stage, actor_id, decision, note, lease_id, self._now()),
                )
                connection.execute("UPDATE review_leases SET consumed=1 WHERE lease_id=?", (lease_id,))
                if decision == "returned":
                    connection.execute("UPDATE stories SET status='returned' WHERE story_id=?", (story_id,))
                    append_event(connection, actor_id=actor_id, action="review.returned",
                                 resource_type="story", resource_id=story_id,
                                 detail={"version": version, "stage": stage, "review_id": review_id,
                                         "note": note}, occurred_at=self._now())
                elif next_stage(stage) is None:
                    connection.execute("UPDATE stories SET status='approved' WHERE story_id=?", (story_id,))
                    append_event(connection, actor_id=actor_id, action="review.approved_final",
                                 resource_type="story", resource_id=story_id,
                                 detail={"version": version, "review_id": review_id}, occurred_at=self._now())
                else:
                    append_event(connection, actor_id=actor_id, action="review.approved",
                                 resource_type="story", resource_id=story_id,
                                 detail={"version": version, "stage": stage, "next_stage": next_stage(stage),
                                         "review_id": review_id}, occurred_at=self._now())
                return "review_decision", review_id, {"review_id": review_id, "decision": decision}

            return self._idempotent(connection, request_id=request_id, action="decide_review",
                                    payload=payload, create=create)

    def review_queue(self, *, actor_id: str, stage: str | None = None) -> list[dict[str, Any]]:
        """列出当前角色可领取的复核项：前置已过、本阶段无结论且无他人有效租约。"""

        with self.storage.transaction() as connection:
            actor = self._actor(connection, actor_id)
            eligible = {s for s, roles in STAGE_ROLES.items() if actor.role in roles}
            if stage:
                if stage not in STAGES:
                    raise ValidationError("stage 不合法")
                if stage not in eligible:
                    raise PermissionDenied("当前角色不能处理该复核阶段")
                eligible = {stage}
            now = self._now()
            items: list[dict[str, Any]] = []
            stories = connection.execute(
                "SELECT * FROM stories WHERE status IN ('in_review','returned') ORDER BY created_at"
            ).fetchall()
            for story in stories:
                if story["created_by"] == actor_id:
                    continue
                site = connection.execute("SELECT * FROM sites WHERE site_id=?", (story["site_id"],)).fetchone()
                if actor.organization_id != site["organization_id"] and actor.role != "admin":
                    continue
                version = story["current_version"]
                opinions = self._opinions(connection, story["story_id"], version)
                if any(o["decision"] == "returned" for o in opinions.values()):
                    continue
                pending = self._pending_stage(opinions)
                if pending is None or pending not in eligible:
                    continue
                lease = connection.execute(
                    "SELECT * FROM review_leases WHERE story_id=? AND version=? AND stage=?",
                    (story["story_id"], version, pending),
                ).fetchone()
                if lease and not lease["consumed"] and lease["expires_at"] > now \
                        and lease["reviewer_id"] != actor_id:
                    continue
                items.append({"story_id": story["story_id"], "version": version, "stage": pending,
                              "title": story["title"],
                              "lease": None if lease is None or lease["consumed"]
                              else {"lease_id": lease["lease_id"], "reviewer_id": lease["reviewer_id"],
                                    "expires_at": lease["expires_at"],
                                    "expired": lease["expires_at"] <= now}})
            return items

    # ------------------------------------------------------------------ 发布

    def publish_story(self, *, request_id: str, actor_id: str, story_id: str) -> WriteReceipt:
        """三阶段全部通过后发布；发布范围固定为当时授权交集，内容快照不可变。"""

        payload = {"actor_id": actor_id, "story_id": story_id}
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            story, _ = self._story_scope(connection, actor, story_id)
            version = story["current_version"]
            opinions = self._opinions(connection, story_id, version)
            missing = [s for s in STAGES if opinions.get(s, {}).get("decision") != "approved"]
            if missing:
                raise ConflictError(f"以下复核阶段尚未通过：{','.join(missing)}")
            version_row = self._version(connection, story_id, version)
            effective, subjects = self._effective_scope(connection, story_id, version_row)
            if effective == "private":
                raise ConflictError("所有有效授权的安全交集为 private，不具备公开发布条件")
            resource_id = f"{story_id}:{version}"
            if connection.execute("SELECT 1 FROM publications WHERE story_id=? AND version=?",
                                  (story_id, version)).fetchone():
                return WriteReceipt(request_id, "publication", resource_id, True)

            def create() -> tuple[str, str, dict[str, Any]]:
                snapshot = self._redacted_view(version_row, subjects, effective, staff=False)
                connection.execute(
                    "INSERT INTO publications(story_id,version,scope,content_json,published_by,published_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (story_id, version, effective, canonical_json(snapshot), actor_id, self._now()),
                )
                connection.execute("UPDATE stories SET status='published' WHERE story_id=?", (story_id,))
                append_event(connection, actor_id=actor_id, action="story.published",
                             resource_type="story", resource_id=story_id,
                             detail={"version": version, "scope": effective,
                                     "content_hash": version_row["content_hash"],
                                     "subject_scopes": {s["subject_id"]: s["scope"] for s in subjects}},
                             occurred_at=self._now())
                return "publication", resource_id, {"story_id": story_id, "version": version,
                                                    "scope": effective}

            return self._idempotent(connection, request_id=request_id, action="publish_story",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 脱敏视图

    def _redacted_view(self, version_row: dict[str, Any], subjects: list[dict[str, Any]],
                       effective_scope: str, *, staff: bool) -> dict[str, Any]:
        persons = json.loads(version_row["persons_json"])
        scope_by_subject = {s["subject_id"]: s for s in subjects}
        viewed_persons: list[dict[str, Any]] = []
        for person in persons:
            subject = scope_by_subject[person["subject_id"]]
            name_visible = staff or (
                allows(effective_scope, "person_names") and subject["scope"] == "public")
            viewed_persons.append({
                "subject_id": person["subject_id"],
                "relation": person["relation"],
                "name": person["name"] if name_visible else None,
                "name_status": "visible" if name_visible else "redacted",
                "subject_scope": subject["scope"],
            })
        return {"title": version_row["title"], "summary": version_row["summary"],
                "outline": version_row["outline"], "persons": viewed_persons}

    @staticmethod
    def _visibility(effective_scope: str, subjects: list[dict[str, Any]], *, staff: bool
                    ) -> tuple[list[str], list[dict[str, str]]]:
        if staff:
            return list(VIEW_FIELDS), []
        visible = [field for field in VIEW_FIELDS if allows(effective_scope, field)]
        narrowest = min(subjects, key=lambda s: SCOPE_RANK[s["scope"]], default=None)
        hidden: list[dict[str, str]] = []
        for field in VIEW_FIELDS:
            if field in visible:
                continue
            reason = f"有效授权交集为 {effective_scope}，未达到 {field} 的开放门槛"
            if narrowest is not None:
                reason += f"；最窄授权来自叙述对象 {narrowest['subject_id']}（{narrowest['scope']}）"
            hidden.append({"field": field, "reason": reason})
        return visible, hidden

    def _log_access(self, connection, *, story_id: str, version: int, actor_id: str,
                    view_scope: str, effective_scope: str, visible_fields: list[str],
                    hidden_fields: list[str], purpose: str) -> str:
        access_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO access_audit(access_id,story_id,version,actor_id,view_scope,effective_scope,"
            "visible_fields_json,hidden_fields_json,purpose,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (access_id, story_id, version, actor_id, view_scope, effective_scope,
             canonical_json(visible_fields), canonical_json(hidden_fields), purpose, self._now()),
        )
        return access_id

    def _latest_publication(self, connection, story_id: str):
        return connection.execute(
            "SELECT * FROM publications WHERE story_id=? ORDER BY version DESC LIMIT 1",
            (story_id,)).fetchone()

    def view_story(self, *, actor_id: str, story_id: str, version: int | None = None,
                   view_scope: str | None = None, purpose: str = "view") -> dict[str, Any]:
        """按视野返回脱敏内容；每次访问（包括内容被隐藏）都写入不可变访问审计。

        内部视野（不传 view_scope）仅供工作人员，看到当前版本全文；
        受众视野（class/community/public）只看到最新已发布版本，且实际开放度
        取「请求视野、当前授权交集、发布时范围」三者的最窄值。
        """

        purpose = self._text(purpose, "purpose", 200)
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            story, _ = self._story_scope(connection, actor, story_id)

            if view_scope is None:
                self._require(actor, "admin", "operator", "reviewer", "auditor")
                version_row = self._version(connection, story_id, version)
                _, subjects = self._effective_scope(connection, story_id, version_row)
                content = self._redacted_view(version_row, subjects, "private", staff=True)
                visible, hidden = list(VIEW_FIELDS), []
                access_id = self._log_access(
                    connection, story_id=story_id, version=version_row["version"], actor_id=actor_id,
                    view_scope="private", effective_scope="private", visible_fields=visible,
                    hidden_fields=[], purpose=purpose)
                return {"story_id": story_id, "version": version_row["version"],
                        "status": story["status"], "requested_scope": "private",
                        "effective_scope": "private", "accessible": True,
                        "visible_fields": visible, "hidden": hidden,
                        "title": content["title"], "summary": content["summary"],
                        "outline": content["outline"], "persons": content["persons"],
                        "access_id": access_id}

            if view_scope not in AUDIENCE_SCOPES:
                raise ValidationError("view_scope 必须是 class、community 或 public")
            publication = self._latest_publication(connection, story_id)
            if publication is None:
                raise NotFoundError("故事尚未发布，当前视野不可见")
            version_row = self._version(connection, story_id, publication["version"])
            consent_scope, subjects = self._effective_scope(connection, story_id, version_row)
            ceiling = SCOPES[min(SCOPE_RANK[consent_scope], SCOPE_RANK[publication["scope"]])]
            effective = SCOPES[min(SCOPE_RANK[view_scope], SCOPE_RANK[ceiling])]

            content = self._redacted_view(version_row, subjects, effective, staff=False)
            visible, hidden = self._visibility(effective, subjects, staff=False)
            access_id = self._log_access(
                connection, story_id=story_id, version=version_row["version"], actor_id=actor_id,
                view_scope=view_scope, effective_scope=effective, visible_fields=visible,
                hidden_fields=[h["field"] for h in hidden], purpose=purpose)
            return {"story_id": story_id, "version": version_row["version"],
                    "status": story["status"], "requested_scope": view_scope,
                    "effective_scope": effective, "accessible": bool(visible),
                    "visible_fields": visible, "hidden": hidden,
                    "title": content["title"] if visible else None,
                    "summary": content["summary"] if allows(effective, "summary") else None,
                    "outline": content["outline"] if allows(effective, "outline") else None,
                    "persons": content["persons"], "access_id": access_id}

    def list_published(self, *, actor_id: str, view_scope: str, purpose: str = "list") -> dict[str, Any]:
        """列出某开放视野下可发布的故事及其脱敏内容，每次访问计入审计。"""

        if view_scope not in AUDIENCE_SCOPES:
            raise ValidationError("view_scope 必须是 class、community 或 public")
        purpose = self._text(purpose, "purpose", 200)
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            rows = connection.execute(
                "SELECT s.story_id, s.site_id, p.version, p.scope AS published_scope "
                "FROM stories s JOIN publications p ON p.story_id=s.story_id "
                "WHERE p.version=(SELECT MAX(version) FROM publications WHERE story_id=s.story_id) "
                "ORDER BY p.published_at"
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                site = connection.execute("SELECT * FROM sites WHERE site_id=?",
                                          (row["site_id"],)).fetchone()
                if actor.organization_id != site["organization_id"] and actor.role != "admin":
                    continue
                version_row = self._version(connection, row["story_id"], row["version"])
                consent_scope, subjects = self._effective_scope(connection, row["story_id"], version_row)
                ceiling = SCOPES[min(SCOPE_RANK[consent_scope], SCOPE_RANK[row["published_scope"]])]
                effective = SCOPES[min(SCOPE_RANK[view_scope], SCOPE_RANK[ceiling])]
                if not allows(effective, "summary"):
                    continue
                content = self._redacted_view(version_row, subjects, effective, staff=False)
                visible, hidden = self._visibility(effective, subjects, staff=False)
                self._log_access(
                    connection, story_id=row["story_id"], version=row["version"], actor_id=actor_id,
                    view_scope=view_scope, effective_scope=effective, visible_fields=visible,
                    hidden_fields=[h["field"] for h in hidden], purpose=purpose)
                items.append({"story_id": row["story_id"], "version": row["version"],
                              "effective_scope": effective, "title": content["title"],
                              "summary": content["summary"],
                              "outline": content["outline"] if allows(effective, "outline") else None,
                              "persons": content["persons"], "hidden": hidden})
            return {"view_scope": view_scope, "items": items}

    # ------------------------------------------------------------------ 审计解释

    def explain_story(self, *, actor_id: str, story_id: str, version: int | None = None,
                      purpose: str = "audit_explain") -> dict[str, Any]:
        """审计员解释材料为何被隐藏、收窄或替代：授权链、意见链、版本链、访问史。"""

        purpose = self._text(purpose, "purpose", 200)
        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            story, _ = self._story_scope(connection, actor, story_id)
            version_row = self._version(connection, story_id, version)
            version = version_row["version"]
            effective, subjects = self._effective_scope(connection, story_id, version_row)

            consents = [{"consent_id": r["consent_id"], "subject_id": r["subject_id"],
                         "relation": r["relation"], "scope": r["scope"], "channel": r["channel"],
                         "note": r["note"], "granted_by": r["granted_by"],
                         "created_at": r["created_at"], "superseded_by": r["superseded_by"]}
                        for r in connection.execute(
                            "SELECT * FROM story_consents WHERE story_id=? ORDER BY created_at",
                            (story_id,))]
            opinions = [{"review_id": r["review_id"], "version": r["version"], "stage": r["stage"],
                         "reviewer_id": r["reviewer_id"], "decision": r["decision"], "note": r["note"],
                         "created_at": r["created_at"]}
                        for r in connection.execute(
                            "SELECT * FROM review_opinions WHERE story_id=? ORDER BY version, created_at",
                            (story_id,))]
            versions = [{"version": r["version"], "change_reason": r["change_reason"],
                         "supersedes": r["supersedes"], "created_by": r["created_by"],
                         "created_at": r["created_at"], "content_hash": r["content_hash"],
                         "source_category": r["source_category"]}
                        for r in connection.execute(
                            "SELECT * FROM story_versions WHERE story_id=? ORDER BY version", (story_id,))]
            publication = connection.execute(
                "SELECT * FROM publications WHERE story_id=? AND version=?", (story_id, version)).fetchone()
            access = [{"access_id": r["access_id"], "actor_id": r["actor_id"],
                       "view_scope": r["view_scope"], "effective_scope": r["effective_scope"],
                       "visible_fields": json.loads(r["visible_fields_json"]),
                       "hidden_fields": json.loads(r["hidden_fields_json"]),
                       "purpose": r["purpose"], "created_at": r["created_at"]}
                      for r in connection.execute(
                          "SELECT * FROM access_audit WHERE story_id=? AND version=? ORDER BY created_at",
                          (story_id, version))]
            visible, hidden = self._visibility(effective, subjects, staff=False)
            self._log_access(connection, story_id=story_id, version=version, actor_id=actor_id,
                             view_scope="private", effective_scope="private",
                             visible_fields=list(VIEW_FIELDS), hidden_fields=[], purpose=purpose)
            return {
                "story": {"story_id": story_id, "site_id": story["site_id"], "status": story["status"],
                          "current_version": story["current_version"]},
                "version": {"version": version, "title": version_row["title"],
                            "summary": version_row["summary"], "outline": version_row["outline"],
                            "persons": json.loads(version_row["persons_json"]),
                            "content_hash": version_row["content_hash"],
                            "change_reason": version_row["change_reason"],
                            "supersedes": version_row["supersedes"]},
                "effective_scope": effective,
                "subjects": subjects,
                "redaction": {"visible_fields": visible, "hidden": hidden},
                "consent_history": consents,
                "review_opinions": opinions,
                "version_chain": versions,
                "publication": None if publication is None
                else {"version": publication["version"], "scope": publication["scope"],
                      "published_by": publication["published_by"],
                      "published_at": publication["published_at"],
                      "content": json.loads(publication["content_json"])},
                "access_audit": access,
            }

    def list_access_audit(self, *, actor_id: str, story_id: str | None = None) -> list[dict[str, Any]]:
        """只读查询访问审计；记录只增不改。"""

        with self.storage.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "auditor")
            if story_id:
                rows = connection.execute(
                    "SELECT * FROM access_audit WHERE story_id=? ORDER BY created_at", (story_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM access_audit ORDER BY created_at").fetchall()
            return [{"access_id": r["access_id"], "story_id": r["story_id"], "version": r["version"],
                     "actor_id": r["actor_id"], "view_scope": r["view_scope"],
                     "effective_scope": r["effective_scope"],
                     "visible_fields": json.loads(r["visible_fields_json"]),
                     "hidden_fields": json.loads(r["hidden_fields_json"]),
                     "purpose": r["purpose"], "created_at": r["created_at"]} for r in rows]

    # ------------------------------------------------------------------ 批量导入

    def batch_import(self, *, request_id: str, actor_id: str, site_id: str, source_id: str,
                     items: list[dict[str, Any]]) -> WriteReceipt:
        """同一来源整批导入：全部成功或全部回滚；source_id 重放可识别。"""

        with self.storage.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_scope(connection, actor, site_id)
            source_id = self._id(source_id, "source_id")
            if not isinstance(items, list) or not items:
                raise ValidationError("items 必须是非空数组")
            # 重放指纹只覆盖业务内容，与操作者、请求编号无关。
            fingerprint = digest({"site_id": site_id, "source_id": source_id, "items": items})
            replay = connection.execute("SELECT * FROM batch_imports WHERE source_id=?",
                                        (source_id,)).fetchone()
            if replay is not None:
                if replay["payload_hash"] != fingerprint:
                    raise ConflictError("同一 source_id 已导入不同内容，拒绝重放覆盖")
                return WriteReceipt(request_id, "batch", source_id, True)

            payload = {"actor_id": actor_id, "site_id": site_id, "source_id": source_id, "items": items}
            prepared: list[dict[str, Any]] = []
            seen_stories: set[str] = set()
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValidationError(f"items[{index}] 必须是对象")
                story_id = self._id(item.get("story_id", ""), f"items[{index}].story_id")
                if story_id in seen_stories:
                    raise ValidationError(f"批次内故事编号重复：{story_id}")
                seen_stories.add(story_id)
                if connection.execute("SELECT 1 FROM stories WHERE story_id=?", (story_id,)).fetchone():
                    raise ConflictError(f"故事编号已经存在：{story_id}")
                persons = self._validate_persons(item.get("persons"))
                consents = self._validate_consents(item.get("consents"), persons)
                title, outline, summary = self._content(item.get("title", ""), item.get("outline", ""),
                                                        item.get("summary", ""))
                prepared.append({"story_id": story_id, "title": title, "outline": outline,
                                 "summary": summary, "persons": persons, "consents": consents})

            def create() -> tuple[str, str, dict[str, Any]]:
                now = self._now()
                imported: list[str] = []
                for entry in prepared:
                    story_id = entry["story_id"]
                    connection.execute(
                        "INSERT INTO stories(story_id,site_id,title,current_version,status,created_by,created_at) "
                        "VALUES(?,?,?,1,'in_review',?,?)",
                        (story_id, site_id, entry["title"], actor_id, now),
                    )
                    content_hash = self._insert_version(
                        connection, story_id=story_id, version=1, title=entry["title"],
                        outline=entry["outline"], summary=entry["summary"], persons=entry["persons"],
                        change_reason="import", created_by=actor_id, supersedes=None,
                        source_category="batch_import", source_record_id=source_id,
                    )
                    for person in entry["persons"]:
                        grant = entry["consents"][person["subject_id"]]
                        self._insert_consent(
                            connection, story_id=story_id, subject_id=person["subject_id"],
                            relation=grant["relation"], scope=grant["scope"], channel="import",
                            note=grant["note"], granted_by=actor_id,
                        )
                    append_event(connection, actor_id=actor_id, action="story.submitted",
                                 resource_type="story", resource_id=story_id,
                                 detail={"site_id": site_id, "version": 1, "content_hash": content_hash,
                                         "source": "batch_import", "source_id": source_id,
                                         "subjects": [p["subject_id"] for p in entry["persons"]]},
                                 occurred_at=now)
                    imported.append(story_id)
                connection.execute(
                    "INSERT INTO batch_imports(source_id,request_id,payload_hash,total,imported_at) "
                    "VALUES(?,?,?,?,?)",
                    (source_id, request_id, fingerprint, len(prepared), now),
                )
                append_event(connection, actor_id=actor_id, action="batch.imported",
                             resource_type="batch", resource_id=source_id,
                             detail={"total": len(prepared), "story_ids": imported}, occurred_at=now)
                return "batch", source_id, {"source_id": source_id, "total": len(prepared),
                                            "story_ids": imported}

            return self._idempotent(connection, request_id=request_id, action="batch_import",
                                    payload=payload, create=create)
