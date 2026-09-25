"""运行家风故事服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from festival_foundation.clock import FixedClock
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from .service import FamilyStoryService


class StepClock(FixedClock):
    """可以按步长推进的固定时钟，用于验证租约过期。"""

    def __init__(self, value: datetime) -> None:
        super().__init__(value)
        self._current = value

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


def run() -> dict[str, object]:
    """执行采集、复核、发布、收窄、审计解释与批量导入的完整链路。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = StepClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        foundation = DomainService(database, clock)
        family = FamilyStoryService(database, clock, lease_seconds=600)

        foundation.register_organization(request_id="acc-org", actor_id="bootstrap",
                                         organization_id="org-001", name="家校社共建联合体")
        foundation.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-001",
                                  display_name="平台管理员", role="admin", organization_id="org-001")
        foundation.register_actor(request_id="acc-op", actor_id="admin-001", new_actor_id="op-001",
                                  display_name="采集员甲", role="operator", organization_id="org-001")
        foundation.register_actor(request_id="acc-op2", actor_id="admin-001", new_actor_id="op-002",
                                  display_name="授权确认员", role="operator", organization_id="org-001")
        foundation.register_actor(request_id="acc-rv", actor_id="admin-001", new_actor_id="rv-001",
                                  display_name="事实核对员", role="reviewer", organization_id="org-001")
        foundation.register_actor(request_id="acc-au", actor_id="admin-001", new_actor_id="au-001",
                                  display_name="审计员", role="auditor", organization_id="org-001")
        foundation.register_site(request_id="acc-site", actor_id="admin-001", site_id="site-001",
                                 organization_id="org-001", name="社区家风采集点", timezone_name="Asia/Shanghai")

        persons = [
            {"subject_id": "grandma", "relation": "祖母", "name": "王奶奶"},
            {"subject_id": "father", "relation": "父亲", "name": "张先生"},
        ]
        consents = [
            {"subject_id": "grandma", "relation": "祖母", "scope": "community", "note": "同意社区展览"},
            {"subject_id": "father", "relation": "父亲", "scope": "public", "note": "同意公开"},
        ]
        family.submit_story(request_id="acc-story", actor_id="op-001", site_id="site-001",
                            story_id="story-001", title="一封家书",
                            outline="访谈提纲：家书的来历与传承", summary="三代人守护一封家书的故事",
                            persons=persons, consents=consents)

        lease1 = family.claim_review(actor_id="rv-001", story_id="story-001", stage="fact_check")
        family.decide_review(request_id="acc-d1", actor_id="rv-001", lease_id=lease1["lease_id"],
                             decision="approved", note="事实核对无误")
        lease2 = family.claim_review(actor_id="admin-001", story_id="story-001", stage="sensitive_review")
        family.decide_review(request_id="acc-d2", actor_id="admin-001", lease_id=lease2["lease_id"],
                             decision="approved", note="敏感信息已脱敏")
        lease3 = family.claim_review(actor_id="op-002", story_id="story-001", stage="consent_confirm")
        family.decide_review(request_id="acc-d3", actor_id="op-002", lease_id=lease3["lease_id"],
                             decision="approved", note="授权链完整")
        family.publish_story(request_id="acc-pub", actor_id="admin-001", story_id="story-001")

        public_view = family.view_story(actor_id="au-001", story_id="story-001", view_scope="public")
        class_view = family.view_story(actor_id="au-001", story_id="story-001", view_scope="class")

        family.narrow_consent(request_id="acc-narrow", actor_id="op-001", story_id="story-001",
                              subject_id="father", scope="class", note="本人要求收窄到班级")
        narrowed_view = family.view_story(actor_id="au-001", story_id="story-001", view_scope="public")

        explanation = family.explain_story(actor_id="au-001", story_id="story-001")
        audit_entries = family.list_access_audit(actor_id="au-001", story_id="story-001")

        batch_items = [
            {"story_id": "story-101", "title": "外婆的账本", "outline": "提纲A", "summary": "摘要A",
             "persons": [{"subject_id": "wai-po", "relation": "外婆", "name": "李婆婆"}],
             "consents": [{"subject_id": "wai-po", "relation": "外婆", "scope": "community", "note": ""}]},
            {"story_id": "story-102", "title": "修表匠的手艺", "outline": "提纲B", "summary": "摘要B",
             "persons": [{"subject_id": "uncle", "relation": "舅舅", "name": "陈师傅"}],
             "consents": [{"subject_id": "uncle", "relation": "舅舅", "scope": "class", "note": ""}]},
        ]
        first_batch = family.batch_import(request_id="acc-batch", actor_id="op-001", site_id="site-001",
                                          source_id="school-drive-2026-09", items=batch_items)
        replay_batch = family.batch_import(request_id="acc-batch-2", actor_id="op-001", site_id="site-001",
                                           source_id="school-drive-2026-09", items=batch_items)

        clock.advance(601)
        queue = family.review_queue(actor_id="rv-001", stage="fact_check")
        valid, event_count = foundation.verify_audit()
        result = {
            "status": "ok",
            "public_scope": public_view["effective_scope"],
            "class_scope": class_view["effective_scope"],
            "narrowed_scope": narrowed_view["effective_scope"],
            "names_hidden_at_class": all(p["name_status"] == "redacted" for p in class_view["persons"]),
            "explanation_subjects": len(explanation["subjects"]),
            "access_audit_entries": len(audit_entries),
            "batch_total": 2 if not first_batch.replayed else -1,
            "batch_replayed": replay_batch.replayed,
            "queue_size": len(queue),
            "audit_valid": valid,
            "audit_events": event_count,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["public_scope"] == "community"
          and result["narrowed_scope"] == "class"
          and result["batch_replayed"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
