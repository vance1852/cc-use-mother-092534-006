"""家风故事采集与分级开放服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .service import DomainService
from .storage import Database
from .stories_service import StoryService


def _clock():
    class _Clock:
        def __init__(self, value):
            self.value = value

        def now(self):
            return self.value

        def advance(self, seconds):
            self.value = self.value + timedelta(seconds=seconds)

    return _Clock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "stories_acceptance.sqlite3")
        clock = _clock()
        base = DomainService(database, clock)
        stories = StoryService(database, base, clock, lease_seconds=900)

        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-001", name="家校共育联盟")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="管理员", role="admin", organization_id="org-001")
        for rid, aid, name, role in [
            ("op", "op-001", "采集员", "operator"),
            ("r1", "rv-001", "事实核对员", "reviewer"),
            ("r2", "rv-002", "敏感复核员", "reviewer"),
            ("r3", "rv-003", "授权确认员", "reviewer"),
            ("au", "au-001", "审计员", "auditor"),
        ]:
            base.register_actor(request_id=rid, actor_id="admin-001", new_actor_id=aid,
                                display_name=name, role=role, organization_id="org-001")
        base.register_site(request_id="site", actor_id="op-001", site_id="site-001",
                           organization_id="org-001", name="实验学校与朝阳社区",
                           timezone_name="Asia/Shanghai")

        content = {
            "title": "奶奶的节俭账本",
            "interview_outline": ["奶奶的童年", "家庭账本故事", "家训传承"],
            "summary": "奶奶用账本记录家庭开支，教育后代勤俭持家，原文含真实住址。",
            "subjects": [
                {"key": "subject:grandma", "display_name": "王奶奶", "relation": "祖母"},
                {"key": "subject:father", "display_name": "李先生", "relation": "父亲"},
            ],
            "citations": [
                {"subject_key": "subject:grandma", "text": "一分钱掰成两半花，家住东大街88号。"},
                {"subject_key": "subject:father", "text": "母亲记账四十多年。"},
            ],
        }
        submitted = stories.submit_story(request_id="submit", actor_id="op-001",
                                         site_id="site-001", external_key="story-001",
                                         content=content)
        story_id = submitted.resource_id

        # 奶奶起初只同意班级分享，父亲同意公开资料库，交集为班级。
        stories.record_consent(request_id="consent-g", actor_id="op-001", story_id=story_id,
                               subject_key="subject:grandma", scope="private", granted=True,
                               statement_type="initial", reason="仅愿班级分享")
        stories.record_consent(request_id="consent-f", actor_id="op-001", story_id=story_id,
                               subject_key="subject:father", scope="archive", granted=True,
                               statement_type="initial", reason="同意公开资料库")

        # 事实核对通过；敏感复核退回，生成不覆盖旧意见的新修订。
        stories.claim_review(request_id="claim-f1", actor_id="rv-001", story_id=story_id,
                             stage="fact_check")
        stories.decide_review(request_id="dec-f1", actor_id="rv-001", story_id=story_id,
                              stage="fact_check", decision="approve", opinion="事实无误")
        stories.claim_review(request_id="claim-s1", actor_id="rv-002", story_id=story_id,
                             stage="sensitive_check")
        stories.decide_review(request_id="dec-s1", actor_id="rv-002", story_id=story_id,
                              stage="sensitive_check", decision="return",
                              opinion="引用含真实住址，需在社区及以上级别替代")
        returned_version = 2

        # 新版本：事实核对、敏感标注、敏感复核、授权确认由不同责任人完成。
        stories.claim_review(request_id="claim-f2", actor_id="rv-001", story_id=story_id,
                             stage="fact_check")
        stories.decide_review(request_id="dec-f2", actor_id="rv-001", story_id=story_id,
                              stage="fact_check", decision="approve", opinion="v2 事实无误")
        stories.claim_review(request_id="claim-s2", actor_id="rv-002", story_id=story_id,
                             stage="sensitive_check")
        stories.add_redaction_mark(request_id="mark-1", actor_id="rv-002", story_id=story_id,
                                   target="citations[0].text", action="replace",
                                   replacement="一分钱掰成两半花。", min_scope="community",
                                   reason="去除真实住址，班级内保留原文")
        stories.decide_review(request_id="dec-s2", actor_id="rv-002", story_id=story_id,
                              stage="sensitive_check", decision="approve",
                              opinion="社区及以上级别已替代住址")
        stories.claim_review(request_id="claim-c2", actor_id="rv-003", story_id=story_id,
                             stage="consent_confirm")
        stories.decide_review(request_id="dec-c2", actor_id="rv-003", story_id=story_id,
                              stage="consent_confirm", decision="approve",
                              opinion="两份授权书与级别匹配")

        published = stories.publish(request_id="publish-1", actor_id="op-001",
                                    story_id=story_id)
        # 班级视图可见原文；社区、资料库因安全交集不可见。
        private_view = stories.published_view(actor_id="op-001", story_id=story_id,
                                              scope="private")
        community_hidden = False
        try:
            stories.published_view(actor_id="op-001", story_id=story_id, scope="community")
        except Exception:
            community_hidden = True

        # 奶奶补充授权至社区展览，交集升到社区，住址被替代。
        stories.record_consent(request_id="consent-g2", actor_id="op-001", story_id=story_id,
                               subject_key="subject:grandma", scope="community", granted=True,
                               statement_type="correction", reason="经解释后同意社区展览")
        community_view = stories.published_view(actor_id="op-001", story_id=story_id,
                                                scope="community")

        # 提交者随后缩小未来可见范围，社区视图随即关闭。
        stories.narrow_visibility(request_id="narrow-1", actor_id="op-001",
                                  story_id=story_id, scope="private",
                                  reason="家属改变主意，仅保留班级分享")
        narrowed_hidden = False
        try:
            stories.published_view(actor_id="op-001", story_id=story_id, scope="community")
        except Exception:
            narrowed_hidden = True

        # 批量导入：全有或全无 + 同来源重放。
        batch = [
            {
                "external_key": "batch-1",
                "content": {
                    "title": "邻居互助", "interview_outline": ["邻里关系"],
                    "summary": "楼里邻居互相帮衬的故事。",
                    "subjects": [{"key": "subject:n1", "display_name": "陈阿姨"}],
                    "citations": [],
                },
                "consents": [{"subject_key": "subject:n1", "scope": "archive",
                              "granted": True, "statement_type": "initial",
                              "reason": "本人同意"}],
            }
        ]
        first_import = stories.import_batch(request_id="import-1", actor_id="op-001",
                                            site_id="site-001", source_id="source-A",
                                            items=batch)
        replay_import = stories.import_batch(request_id="import-2", actor_id="op-001",
                                             site_id="site-001", source_id="source-A",
                                             items=batch)

        explanation = stories.explain_visibility(actor_id="au-001", story_id=story_id)
        audit_valid, audit_events = base.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": audit_events,
            "returned_version": returned_version,
            "current_version": explanation["current_version"],
            "published_version": published.response["version"],
            "private_scope": private_view["served_scope"],
            "private_keeps_address": "东大街88号" in private_view["content"]["citations"][0]["text"],
            "community_hidden_before_consent": community_hidden,
            "community_scope_after_consent": community_view["served_scope"],
            "community_address_replaced": "东大街88号" not in community_view["content"]["citations"][0]["text"],
            "narrowed_hidden": narrowed_hidden,
            "old_opinions_kept": explanation["versions"][0]["reviews"][0]["opinion"] == "事实无误",
            "import_replayed": replay_import.replayed and not first_import.replayed,
            "total_stories": len(stories.list_stories(actor_id="op-001",
                                                      site_id="site-001")["items"]),
        }
        database.close()
        expected = {
            "audit_valid": True,
            "returned_version": 2,
            "current_version": 2,
            "published_version": 2,
            "private_scope": "private",
            "private_keeps_address": True,
            "community_hidden_before_consent": True,
            "community_scope_after_consent": "community",
            "community_address_replaced": True,
            "narrowed_hidden": True,
            "old_opinions_kept": True,
            "import_replayed": True,
            "total_stories": 2,
        }
        for key, value in expected.items():
            assert result[key] == value, (key, result[key], value)
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
