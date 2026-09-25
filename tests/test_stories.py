import unittest
from datetime import datetime, timedelta, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied
from festival_foundation.service import DomainService
from festival_foundation.stories_domain import min_scope
from festival_foundation.stories_service import StoryService
from festival_foundation.storage import Database


class MutableClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value = self.value + timedelta(seconds=seconds)


def make_content():
    return {
        "title": "奶奶的节俭账本",
        "interview_outline": ["奶奶的童年", "家庭账本故事"],
        "summary": "奶奶用账本教育后代勤俭持家。",
        "subjects": [
            {"key": "subject:grandma", "display_name": "王奶奶", "relation": "祖母"},
            {"key": "subject:father", "display_name": "李先生", "relation": "父亲"},
        ],
        "citations": [
            {"subject_key": "subject:grandma", "text": "一分钱掰成两半花，家住东大街88号。"},
            {"subject_key": "subject:father", "text": "母亲记账四十多年。"},
        ],
    }


class StoryCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.database, self.clock)
        self.service = StoryService(self.database, self.base, self.clock, lease_seconds=900)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="家校联盟")
        for rid, aid, name, role in [
            ("a-admin", "admin1", "管理员", "admin"),
            ("a-op", "op1", "采集员", "operator"),
            ("a-r1", "rv1", "核对员甲", "reviewer"),
            ("a-r2", "rv2", "核对员乙", "reviewer"),
            ("a-r3", "rv3", "核对员丙", "reviewer"),
            ("a-au", "au1", "审计员", "auditor"),
        ]:
            self.base.register_actor(request_id=rid, actor_id="bootstrap" if aid == "admin1" else "admin1",
                                     new_actor_id=aid, display_name=name, role=role,
                                     organization_id="o1")
        self.base.register_site(request_id="site", actor_id="op1", site_id="s1",
                                organization_id="o1", name="实验学校", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def submit(self, request_id="st-1", external_key="story-1"):
        return self.service.submit_story(request_id=request_id, actor_id="op1",
                                         site_id="s1", external_key=external_key,
                                         content=make_content())

    def consent(self, rid, subject, scope, granted=True, st="initial", reason="已签署授权书"):
        return self.service.record_consent(request_id=rid, actor_id="op1", story_id=self.story_id,
                                           subject_key=subject, scope=scope, granted=granted,
                                           statement_type=st, reason=reason)

    def approve_all(self, prefix):
        """三位不同复核人顺序通过三步（v1 无标注）。"""
        for i, (reviewer, stage, opinion) in enumerate([
            ("rv1", "fact_check", "事实无误"),
            ("rv2", "sensitive_check", "脱敏到位"),
            ("rv3", "consent_confirm", "授权齐全"),
        ]):
            self.clock.advance(1)
            self.service.claim_review(request_id=f"{prefix}-claim-{i}", actor_id=reviewer,
                                      story_id=self.story_id, stage=stage)
            self.service.decide_review(request_id=f"{prefix}-dec-{i}", actor_id=reviewer,
                                       story_id=self.story_id, stage=stage,
                                       decision="approve", opinion=opinion)

    @property
    def story_id(self):
        if not hasattr(self, "_story_id"):
            self._story_id = self.submit().resource_id
        return self._story_id


class TestScopeIntersection(StoryCase):
    def test_min_scope_is_safe_intersection(self):
        self.assertEqual("private", min_scope("archive", "community", "private"))
        self.assertEqual("community", min_scope("archive", "community"))

    def test_missing_consent_blocks_publication(self):
        self.consent("c1", "subject:grandma", "archive")
        # father 无授权
        self.approve_all("rv")
        with self.assertRaises(ConflictError):
            self.service.publish(request_id="pub", actor_id="op1", story_id=self.story_id)

    def test_withdrawal_tightens_intersection(self):
        self.consent("c1", "subject:grandma", "community")
        self.consent("c2", "subject:father", "archive")
        self.approve_all("rv")
        self.service.publish(request_id="pub", actor_id="op1", story_id=self.story_id)
        view = self.service.published_view(actor_id="op1", story_id=self.story_id,
                                           scope="community")
        self.assertEqual("community", view["served_scope"])
        # 奶奶撤回：整体不得在任何级别公开
        self.consent("c3", "subject:grandma", "private", granted=False,
                     st="withdrawal", reason="改变主意撤回授权")
        with self.assertRaises(NotFoundError):
            self.service.published_view(actor_id="op1", story_id=self.story_id, scope="private")


class TestVersioningAndReview(StoryCase):
    def test_return_creates_new_revision_and_keeps_old_opinions(self):
        sid = self.story_id
        self.service.claim_review(request_id="l1", actor_id="rv1", story_id=sid,
                                  stage="fact_check")
        self.service.decide_review(request_id="d1", actor_id="rv1", story_id=sid,
                                   stage="fact_check", decision="approve", opinion="事实无误")
        self.service.claim_review(request_id="l2", actor_id="rv2", story_id=sid,
                                  stage="sensitive_check")
        result = self.service.decide_review(request_id="d2", actor_id="rv2", story_id=sid,
                                            stage="sensitive_check", decision="return",
                                            opinion="摘要需补充来源")
        self.assertEqual(2, result.response["version"])
        explanation = self.service.explain_visibility(actor_id="au1", story_id=sid)
        v1 = explanation["versions"][0]
        self.assertEqual("approved", v1["reviews"][0]["status"])
        self.assertEqual("returned", v1["reviews"][1]["status"])
        self.assertEqual("事实无误", v1["reviews"][0]["opinion"])
        v2 = explanation["versions"][1]
        self.assertEqual("review_return", v2["revision_type"])
        self.assertTrue(all(task["status"] == "pending" for task in v2["reviews"]))

    def test_old_decision_cannot_be_overwritten(self):
        sid = self.story_id
        self.service.claim_review(request_id="l1", actor_id="rv1", story_id=sid,
                                  stage="fact_check")
        self.service.decide_review(request_id="d1", actor_id="rv1", story_id=sid,
                                   stage="fact_check", decision="approve", opinion="通过")
        self.clock.advance(10000)
        with self.assertRaises(ConflictError):
            self.service.claim_review(request_id="l2", actor_id="rv2", story_id=sid,
                                      stage="fact_check")

    def test_lease_expiry_blocks_old_reviewer(self):
        sid = self.story_id
        self.service.claim_review(request_id="l1", actor_id="rv1", story_id=sid,
                                  stage="fact_check")
        self.clock.advance(901)
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="d1", actor_id="rv1", story_id=sid,
                                       stage="fact_check", decision="approve", opinion="迟来")
        # 过期后可被他人重新认领
        self.service.claim_review(request_id="l2", actor_id="rv2", story_id=sid,
                                  stage="fact_check")

    def test_lease_cannot_be_stolen_while_valid(self):
        sid = self.story_id
        self.service.claim_review(request_id="l1", actor_id="rv1", story_id=sid,
                                  stage="fact_check")
        with self.assertRaises(ConflictError):
            self.service.claim_review(request_id="l2", actor_id="rv2", story_id=sid,
                                      stage="fact_check")

    def test_three_stages_must_have_distinct_reviewers(self):
        sid = self.story_id
        self.service.claim_review(request_id="l1", actor_id="rv1", story_id=sid,
                                  stage="fact_check")
        self.service.decide_review(request_id="d1", actor_id="rv1", story_id=sid,
                                   stage="fact_check", decision="approve", opinion="x")
        self.service.claim_review(request_id="l2", actor_id="rv1", story_id=sid,
                                  stage="sensitive_check")
        with self.assertRaises(ConflictError):
            self.service.decide_review(request_id="d2", actor_id="rv1", story_id=sid,
                                       stage="sensitive_check", decision="approve",
                                       opinion="同一人不能做两步")

    def test_non_lease_holder_cannot_decide(self):
        sid = self.story_id
        self.service.claim_review(request_id="l1", actor_id="rv1", story_id=sid,
                                  stage="fact_check")
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(request_id="d1", actor_id="rv2", story_id=sid,
                                       stage="fact_check", decision="approve", opinion="代签")

    def test_publish_requires_all_approvals(self):
        self.consent("c1", "subject:grandma", "private")
        self.consent("c2", "subject:father", "private")
        with self.assertRaises(ConflictError):
            self.service.publish(request_id="pub", actor_id="op1", story_id=self.story_id)

    def test_correction_adds_immutable_version(self):
        sid = self.story_id
        content = make_content()
        content["summary"] = "更正后的摘要，补充了细节。"
        receipt = self.service.submit_correction(request_id="corr1", actor_id="op1",
                                                 story_id=sid, content=content,
                                                 change_reason="摘要笔误")
        self.assertEqual(2, receipt.response["version"])
        rows = self.database.connection.execute(
            "SELECT version FROM story_revisions WHERE story_id=? ORDER BY version", (sid,)
        ).fetchall()
        self.assertEqual([1, 2], [row["version"] for row in rows])
        # v1 内容保持不变
        v1 = self.service.explain_visibility(actor_id="au1", story_id=sid)["versions"][0]
        self.assertEqual("submission", v1["revision_type"])


class TestRedaction(StoryCase):
    def _prepare_v1_marked(self):
        sid = self.story_id
        self.service.claim_review(request_id="l1", actor_id="rv1", story_id=sid,
                                  stage="fact_check")
        self.service.decide_review(request_id="d1", actor_id="rv1", story_id=sid,
                                   stage="fact_check", decision="approve", opinion="ok")
        self.service.claim_review(request_id="l2", actor_id="rv2", story_id=sid,
                                  stage="sensitive_check")
        self.service.add_redaction_mark(request_id="m1", actor_id="rv2", story_id=sid,
                                        target="citations[0].text", action="replace",
                                        replacement="一分钱掰成两半花。", min_scope="community",
                                        reason="去除真实住址")
        self.service.decide_review(request_id="d2", actor_id="rv2", story_id=sid,
                                   stage="sensitive_check", decision="approve", opinion="ok")
        self.service.claim_review(request_id="l3", actor_id="rv3", story_id=sid,
                                  stage="consent_confirm")
        self.service.decide_review(request_id="d3", actor_id="rv3", story_id=sid,
                                   stage="consent_confirm", decision="approve", opinion="ok")

    def test_threshold_scope_applies_mark_only_at_or_above(self):
        sid = self.story_id
        self.consent("c1", "subject:grandma", "archive")
        self.consent("c2", "subject:father", "archive")
        self._prepare_v1_marked()
        self.service.publish(request_id="pub", actor_id="op1", story_id=sid)
        private_view = self.service.published_view(actor_id="op1", story_id=sid,
                                                   scope="private")
        self.assertIn("东大街88号", private_view["content"]["citations"][0]["text"])
        self.assertEqual([], private_view["redactions_applied"])
        public_view = self.service.published_view(actor_id="op1", story_id=sid,
                                                  scope="archive")
        self.assertNotIn("东大街88号", public_view["content"]["citations"][0]["text"])
        self.assertEqual("replace", public_view["redactions_applied"][0]["action"])

    def test_mark_after_stage_closed_is_rejected(self):
        sid = self.story_id
        self.approve_all("rv")
        with self.assertRaises(ConflictError):
            self.service.add_redaction_mark(request_id="m-late", actor_id="rv2",
                                            story_id=sid, target="summary", action="mask",
                                            reason="事后追加")

    def test_auditor_can_explain_why_hidden_or_replaced(self):
        sid = self.story_id
        self.consent("c1", "subject:grandma", "private")
        self.consent("c2", "subject:father", "archive")
        self._prepare_v1_marked()
        self.service.publish(request_id="pub", actor_id="op1", story_id=sid)
        explanation = self.service.explain_visibility(actor_id="au1", story_id=sid)
        views = explanation["published"]["views_at_scope"]
        self.assertFalse(views["archive"]["visible"])
        self.assertFalse(views["community"]["visible"])
        self.assertTrue(views["private"]["visible"])
        # 班级级别下标注不生效，解释为空；提升到 community 才会替代
        self.assertEqual([], views["private"]["applied_marks"])
        self.consent("c3", "subject:grandma", "archive", st="correction", reason="追加授权")
        explanation = self.service.explain_visibility(actor_id="au1", story_id=sid)
        views = explanation["published"]["views_at_scope"]
        self.assertTrue(views["community"]["visible"])
        self.assertEqual("citations[0].text", views["community"]["applied_marks"][0]["target"])


class TestNarrowing(StoryCase):
    def test_narrowing_takes_effect_future_access_and_keeps_history(self):
        sid = self.story_id
        self.consent("c1", "subject:grandma", "archive")
        self.consent("c2", "subject:father", "archive")
        self.approve_all("rv")
        self.service.publish(request_id="pub", actor_id="op1", story_id=sid)
        self.service.published_view(actor_id="op1", story_id=sid, scope="archive")
        self.service.narrow_visibility(request_id="n1", actor_id="op1", story_id=sid,
                                       scope="community", reason="家属要求收窄")
        with self.assertRaises(NotFoundError):
            self.service.published_view(actor_id="op1", story_id=sid, scope="archive")
        still = self.service.published_view(actor_id="op1", story_id=sid, scope="community")
        self.assertEqual("community", still["served_scope"])
        explanation = self.service.explain_visibility(actor_id="au1", story_id=sid)
        grandma = explanation["consents"]["subject:grandma"]
        self.assertEqual(["archive", "community"], [row["scope"] for row in grandma])
        self.assertEqual("narrowing", grandma[-1]["statement_type"])

    def test_narrowing_must_be_strictly_smaller(self):
        sid = self.story_id
        self.consent("c1", "subject:grandma", "community")
        self.consent("c2", "subject:father", "archive")
        with self.assertRaises(ConflictError):
            self.service.narrow_visibility(request_id="n1", actor_id="op1", story_id=sid,
                                           scope="archive", reason="试图扩大")

    def test_non_submitter_cannot_narrow(self):
        with self.assertRaises(PermissionDenied):
            self.service.narrow_visibility(request_id="n2", actor_id="au1",
                                           story_id=self.story_id, scope="private",
                                           reason="审计员无权收窄")
        # 管理员可代提交者处理
        self.consent("c1", "subject:grandma", "archive")
        self.consent("c2", "subject:father", "archive")
        self.service.narrow_visibility(request_id="n3", actor_id="admin1",
                                       story_id=self.story_id, scope="community",
                                       reason="管理员代为收窄")


class TestBatchImport(StoryCase):
    def _items(self):
        return [
            {
                "external_key": "b-1",
                "content": {
                    "title": "故事一", "interview_outline": ["q1"], "summary": "摘要一",
                    "subjects": [{"key": "subject:a", "display_name": "甲"}],
                    "citations": [],
                },
                "consents": [{"subject_key": "subject:a", "scope": "archive", "granted": True,
                              "statement_type": "initial", "reason": "同意"}],
            },
            {
                "external_key": "b-2",
                "content": {
                    "title": "故事二", "interview_outline": ["q1"], "summary": "摘要二",
                    "subjects": [{"key": "subject:b", "display_name": "乙"}],
                    "citations": [{"subject_key": "subject:b", "text": "原话"}],
                },
                "consents": [],
            },
        ]

    def test_batch_is_all_or_nothing(self):
        items = self._items()
        items[1]["external_key"] = "story-1"  # 与单条提交冲突
        self.story_id  # 先创建 story-1
        before = len(self.service.list_stories(actor_id="op1", site_id="s1")["items"])
        with self.assertRaises(ConflictError):
            self.service.import_batch(request_id="ib1", actor_id="op1", site_id="s1",
                                      source_id="src-1", items=items)
        after = len(self.service.list_stories(actor_id="op1", site_id="s1")["items"])
        self.assertEqual(before, after)

    def test_source_replay_is_recognized(self):
        items = self._items()
        first = self.service.import_batch(request_id="ib1", actor_id="op1", site_id="s1",
                                          source_id="src-1", items=items)
        self.assertFalse(first.replayed)
        replay = self.service.import_batch(request_id="ib2", actor_id="op1", site_id="s1",
                                           source_id="src-1", items=items)
        self.assertTrue(replay.replayed)
        self.assertEqual(2, len(self.service.list_stories(actor_id="op1", site_id="s1")["items"]))

    def test_same_source_different_content_rejected(self):
        items = self._items()
        self.service.import_batch(request_id="ib1", actor_id="op1", site_id="s1",
                                  source_id="src-1", items=items)
        items[0]["content"]["summary"] = "被篡改"
        with self.assertRaises(ConflictError):
            self.service.import_batch(request_id="ib2", actor_id="op1", site_id="s1",
                                      source_id="src-1", items=items)

    def test_duplicate_key_within_batch_rejected(self):
        items = self._items()
        items[0]["external_key"] = "same"
        items[1]["external_key"] = "same"
        with self.assertRaises(Exception):
            self.service.import_batch(request_id="ib1", actor_id="op1", site_id="s1",
                                      source_id="src-1", items=items)


class TestPermissionsAndAudit(StoryCase):
    def test_auditor_cannot_submit(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_story(request_id="x", actor_id="au1", site_id="s1",
                                      external_key="x1", content=make_content())

    def test_reviewer_cannot_publish(self):
        with self.assertRaises(PermissionDenied):
            self.service.publish(request_id="pub", actor_id="rv1", story_id=self.story_id)

    def test_explain_visibility_reserved_for_auditor_and_admin(self):
        self.service.explain_visibility(actor_id="au1", story_id=self.story_id)
        with self.assertRaises(PermissionDenied):
            self.service.explain_visibility(actor_id="op1", story_id=self.story_id)

    def test_denied_access_is_audited_and_chain_intact(self):
        self.consent("c1", "subject:grandma", "private")
        self.consent("c2", "subject:father", "private")
        self.approve_all("rv")
        self.service.publish(request_id="pub", actor_id="op1", story_id=self.story_id)
        with self.assertRaises(NotFoundError):
            self.service.published_view(actor_id="op1", story_id=self.story_id,
                                        scope="archive")
        events = self.base.audit_events()
        actions = [event["action"] for event in events]
        self.assertIn("story.access_denied", actions)
        valid, _ = self.base.verify_audit()
        self.assertTrue(valid)

    def test_correction_with_new_subject_blocks_republish_until_consent(self):
        sid = self.story_id
        self.consent("c1", "subject:grandma", "archive")
        self.consent("c2", "subject:father", "archive")
        self.approve_all("rv")
        self.service.publish(request_id="pub", actor_id="op1", story_id=sid)
        content = make_content()
        content["subjects"].append({"key": "subject:uncle", "display_name": "叔父",
                                    "relation": "叔父"})
        self.service.submit_correction(request_id="corr1", actor_id="op1", story_id=sid,
                                       content=content, change_reason="补充人物关系")
        # 新版本未复核也无新对象授权
        with self.assertRaises(ConflictError):
            self.service.publish(request_id="pub2", actor_id="op1", story_id=sid)


if __name__ == "__main__":
    unittest.main()
