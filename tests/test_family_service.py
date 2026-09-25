import unittest
from datetime import datetime, timedelta, timezone

from festival_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from family_stories.service import FamilyStoryService


class MutableClock:
    def __init__(self, value):
        self._value = value

    def now(self):
        return self._value

    def advance(self, seconds):
        self._value += timedelta(seconds=seconds)


PERSONS = [
    {"subject_id": "grandma", "relation": "祖母", "name": "王奶奶"},
    {"subject_id": "father", "relation": "父亲", "name": "张先生"},
]


def consents(grandma_scope="community", father_scope="public"):
    return [
        {"subject_id": "grandma", "relation": "祖母", "scope": grandma_scope, "note": ""},
        {"subject_id": "father", "relation": "父亲", "scope": father_scope, "note": ""},
    ]


class FamilyStoryServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, self.clock)
        self.family = FamilyStoryService(self.database, self.clock, lease_seconds=600)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="联合体")
        self.foundation.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="a-op1", actor_id="admin1", new_actor_id="op1",
                                       display_name="采集员一", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="a-op2", actor_id="admin1", new_actor_id="op2",
                                       display_name="采集员二", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="a-rv1", actor_id="admin1", new_actor_id="rv1",
                                       display_name="核对员一", role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="a-rv2", actor_id="admin1", new_actor_id="rv2",
                                       display_name="核对员二", role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="a-au", actor_id="admin1", new_actor_id="au1",
                                       display_name="审计员", role="auditor", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="admin1", site_id="s1",
                                      organization_id="o1", name="采集点", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    # ------------------------------------------------------------ 工具

    def _submit(self, story_id="st1", persons=None, consent_list=None, actor="op1"):
        return self.family.submit_story(
            request_id=f"sub-{story_id}", actor_id=actor, site_id="s1", story_id=story_id,
            title="一封家书", outline="访谈提纲：家书来历", summary="三代守护家书",
            persons=persons if persons is not None else PERSONS,
            consents=consent_list if consent_list is not None else consents())

    def _approve(self, story_id, stage, actor):
        lease = self.family.claim_review(actor_id=actor, story_id=story_id, stage=stage)
        return self.family.decide_review(request_id=f"dec-{story_id}-{stage}", actor_id=actor,
                                         lease_id=lease["lease_id"], decision="approved", note="通过")

    def _approve_all(self, story_id):
        self._approve(story_id, "fact_check", "rv1")
        self._approve(story_id, "sensitive_review", "admin1")
        self._approve(story_id, "consent_confirm", "op2")

    def _publish(self, story_id, request_id=None):
        return self.family.publish_story(request_id=request_id or f"pub-{story_id}",
                                         actor_id="admin1", story_id=story_id)

    # ------------------------------------------------------------ 版本与修订

    def test_revision_creates_immutable_new_version(self):
        self._submit()
        receipt = self.family.revise_story(
            request_id="rev1", actor_id="op1", story_id="st1", title="一封家书（修订）",
            outline="提纲v2", summary="摘要v2", persons=PERSONS, change_reason="correction")
        self.assertFalse(receipt.replayed)
        explain = self.family.explain_story(actor_id="au1", story_id="st1")
        self.assertEqual(2, explain["story"]["current_version"])
        self.assertEqual([1, 2], [v["version"] for v in explain["version_chain"]])
        self.assertEqual(1, explain["version_chain"][1]["supersedes"])
        first = self.family.explain_story(actor_id="au1", story_id="st1", version=1)
        self.assertEqual("访谈提纲：家书来历", first["version"]["outline"])
        self.assertEqual("摘要v2", explain["version"]["summary"])

    def test_revise_rejects_invalid_reason(self):
        self._submit()
        with self.assertRaises(ValidationError):
            self.family.revise_story(request_id="rev-x", actor_id="op1", story_id="st1",
                                     title="t", outline="o", summary="s", persons=PERSONS,
                                     change_reason="initial")

    def test_review_return_revision_requires_returned_status(self):
        self._submit()
        with self.assertRaises(ConflictError):
            self.family.revise_story(request_id="rev-rr", actor_id="op1", story_id="st1",
                                     title="t", outline="o", summary="s", persons=PERSONS,
                                     change_reason="review_return")

    def test_return_generates_new_revision_and_keeps_old_opinions(self):
        self._submit()
        lease = self.family.claim_review(actor_id="rv1", story_id="st1", stage="fact_check")
        self.family.decide_review(request_id="d-ret", actor_id="rv1", lease_id=lease["lease_id"],
                                  decision="returned", note="人物关系需要补充")
        with self.assertRaises(ConflictError):
            self.family.claim_review(actor_id="rv2", story_id="st1", stage="fact_check")
        self.family.revise_story(request_id="rev-fix", actor_id="op1", story_id="st1",
                                 title="一封家书", outline="提纲补充版", summary="摘要补充版",
                                 persons=PERSONS, change_reason="review_return")
        explain = self.family.explain_story(actor_id="au1", story_id="st1")
        self.assertEqual(2, explain["story"]["current_version"])
        returned = [o for o in explain["review_opinions"] if o["decision"] == "returned"]
        self.assertEqual(1, len(returned))
        self.assertEqual("人物关系需要补充", returned[0]["note"])
        self.assertEqual(1, returned[0]["version"])
        lease2 = self.family.claim_review(actor_id="rv2", story_id="st1", stage="fact_check")
        self.assertEqual(2, lease2["version"])

    # ------------------------------------------------------------ 授权与收窄

    def test_narrow_consent_reduces_future_visibility_and_keeps_history(self):
        self._submit()
        self._approve_all("st1")
        self._publish("st1")
        before = self.family.view_story(actor_id="au1", story_id="st1", view_scope="public")
        self.assertEqual("community", before["effective_scope"])
        self.family.narrow_consent(request_id="narrow1", actor_id="op1", story_id="st1",
                                   subject_id="father", scope="class", note="本人要求收窄")
        after = self.family.view_story(actor_id="au1", story_id="st1", view_scope="public")
        self.assertEqual("class", after["effective_scope"])
        self.assertIsNone(after["outline"])
        explain = self.family.explain_story(actor_id="au1", story_id="st1")
        father_history = [c for c in explain["consent_history"] if c["subject_id"] == "father"]
        self.assertEqual(["public", "class"], [c["scope"] for c in father_history])
        self.assertIsNotNone(father_history[0]["superseded_by"])
        audits = self.family.list_access_audit(actor_id="au1", story_id="st1")
        self.assertGreaterEqual(len(audits), 3)

    def test_narrow_consent_rejects_widening(self):
        self._submit()
        with self.assertRaises(ValidationError):
            self.family.narrow_consent(request_id="n-wide", actor_id="op1", story_id="st1",
                                       subject_id="grandma", scope="public")
        with self.assertRaises(NotFoundError):
            self.family.narrow_consent(request_id="n-none", actor_id="op1", story_id="st1",
                                       subject_id="nobody", scope="private")

    def test_consent_scope_change_supersedes_and_same_scope_conflicts(self):
        self._submit()
        self.family.record_consent(request_id="c-up", actor_id="op1", story_id="st1",
                                   subject_id="grandma", relation="祖母", scope="public")
        with self.assertRaises(ConflictError):
            self.family.record_consent(request_id="c-same", actor_id="op1", story_id="st1",
                                       subject_id="grandma", relation="祖母", scope="public")
        explain = self.family.explain_story(actor_id="au1", story_id="st1")
        grandma = [c for c in explain["consent_history"] if c["subject_id"] == "grandma"]
        self.assertEqual(["community", "public"], [c["scope"] for c in grandma])

    def test_missing_consent_for_new_person_blocks_publication(self):
        self._submit()
        bigger = PERSONS + [{"subject_id": "aunt", "relation": "姑姑", "name": "张女士"}]
        self.family.revise_story(request_id="rev-add", actor_id="op1", story_id="st1",
                                 title="一封家书", outline="提纲", summary="摘要",
                                 persons=bigger, change_reason="edit")
        self._approve_all("st1")
        with self.assertRaises(ConflictError):
            self._publish("st1")
        self.family.record_consent(request_id="c-aunt", actor_id="op1", story_id="st1",
                                   subject_id="aunt", relation="姑姑", scope="community")
        receipt = self._publish("st1", request_id="pub-st1-2")
        self.assertFalse(receipt.replayed)

    def test_safe_intersection_takes_narrowest_subject(self):
        self._submit()
        self._approve_all("st1")
        receipt = self._publish("st1")
        self.assertFalse(receipt.replayed)
        view = self.family.view_story(actor_id="au1", story_id="st1", view_scope="public")
        self.assertEqual("community", view["effective_scope"])

    # ------------------------------------------------------------ 复核队列与租约

    def test_stage_order_and_role_separation(self):
        self._submit()
        with self.assertRaises(PermissionDenied):
            self.family.claim_review(actor_id="rv1", story_id="st1", stage="sensitive_review")
        with self.assertRaises(ConflictError):
            self.family.claim_review(actor_id="admin1", story_id="st1", stage="sensitive_review")
        with self.assertRaises(PermissionDenied):
            self.family.claim_review(actor_id="op1", story_id="st1", stage="fact_check")
        self._approve("st1", "fact_check", "rv1")
        self._approve("st1", "sensitive_review", "admin1")
        with self.assertRaises(PermissionDenied):
            self.family.claim_review(actor_id="op1", story_id="st1", stage="consent_confirm")
        self._approve("st1", "consent_confirm", "op2")

    def test_lease_blocks_other_reviewer_until_expiry(self):
        self._submit()
        lease = self.family.claim_review(actor_id="rv1", story_id="st1", stage="fact_check")
        with self.assertRaises(ConflictError):
            self.family.claim_review(actor_id="rv2", story_id="st1", stage="fact_check")
        self.clock.advance(601)
        taken = self.family.claim_review(actor_id="rv2", story_id="st1", stage="fact_check")
        self.assertNotEqual(lease["lease_id"], taken["lease_id"])
        with self.assertRaises((ConflictError, NotFoundError)):
            self.family.decide_review(request_id="d-old", actor_id="rv1",
                                      lease_id=lease["lease_id"], decision="approved", note="迟到")

    def test_expired_lease_cannot_decide(self):
        self._submit()
        lease = self.family.claim_review(actor_id="rv1", story_id="st1", stage="fact_check")
        self.clock.advance(601)
        with self.assertRaises(ConflictError):
            self.family.decide_review(request_id="d-exp", actor_id="rv1",
                                      lease_id=lease["lease_id"], decision="approved", note="过期")

    def test_consumed_lease_cannot_be_reused(self):
        self._submit()
        lease = self.family.claim_review(actor_id="rv1", story_id="st1", stage="fact_check")
        self.family.decide_review(request_id="d1", actor_id="rv1", lease_id=lease["lease_id"],
                                  decision="approved", note="通过")
        with self.assertRaises(ConflictError):
            self.family.decide_review(request_id="d2", actor_id="rv1", lease_id=lease["lease_id"],
                                      decision="approved", note="重复")

    def test_revision_invalidates_prior_unconsumed_lease(self):
        self._submit()
        lease = self.family.claim_review(actor_id="rv1", story_id="st1", stage="fact_check")
        self.family.revise_story(request_id="rev-race", actor_id="op1", story_id="st1",
                                 title="t", outline="o", summary="s", persons=PERSONS,
                                 change_reason="correction")
        with self.assertRaises(ConflictError):
            self.family.decide_review(request_id="d-stale", actor_id="rv1",
                                      lease_id=lease["lease_id"], decision="approved", note="旧版本")
        fresh = self.family.claim_review(actor_id="rv2", story_id="st1", stage="fact_check")
        self.assertEqual(2, fresh["version"])

    def test_review_queue_lists_pending_for_eligible_roles(self):
        self._submit()
        queue = self.family.review_queue(actor_id="rv1")
        self.assertEqual(1, len(queue))
        self.assertEqual("fact_check", queue[0]["stage"])
        self.assertEqual([], self.family.review_queue(actor_id="au1"))
        lease = self.family.claim_review(actor_id="rv1", story_id="st1")
        self.assertEqual([], self.family.review_queue(actor_id="rv2"))
        mine = self.family.review_queue(actor_id="rv1")
        self.assertEqual(lease["lease_id"], mine[0]["lease"]["lease_id"])

    # ------------------------------------------------------------ 发布与脱敏视图

    def test_view_redacts_by_scope(self):
        self._submit()
        self._approve_all("st1")
        self._publish("st1")
        class_view = self.family.view_story(actor_id="au1", story_id="st1", view_scope="class")
        self.assertIsNone(class_view["outline"])
        self.assertTrue(all(p["name_status"] == "redacted" for p in class_view["persons"]))
        community_view = self.family.view_story(actor_id="au1", story_id="st1", view_scope="community")
        self.assertIsNotNone(community_view["outline"])
        self.assertTrue(all(p["name_status"] == "redacted" for p in community_view["persons"]))
        hidden_fields = {h["field"] for h in community_view["hidden"]}
        self.assertIn("person_names", hidden_fields)

    def test_public_view_shows_names_only_when_all_public(self):
        self._submit(story_id="st-pub", consent_list=consents("public", "public"))
        self._approve_all("st-pub")
        self._publish("st-pub")
        view = self.family.view_story(actor_id="au1", story_id="st-pub", view_scope="public")
        self.assertEqual("public", view["effective_scope"])
        self.assertTrue(all(p["name_status"] == "visible" for p in view["persons"]))

    def test_audience_view_requires_publication(self):
        self._submit()
        with self.assertRaises(NotFoundError):
            self.family.view_story(actor_id="au1", story_id="st1", view_scope="class")
        staff = self.family.view_story(actor_id="rv1", story_id="st1")
        self.assertEqual("private", staff["effective_scope"])
        self.assertIsNotNone(staff["outline"])

    def test_revision_after_publish_keeps_old_publication_served(self):
        self._submit()
        self._approve_all("st1")
        self._publish("st1")
        self.family.revise_story(request_id="rev-after", actor_id="op1", story_id="st1",
                                 title="t", outline="o", summary="s", persons=PERSONS,
                                 change_reason="edit")
        view = self.family.view_story(actor_id="au1", story_id="st1", view_scope="community")
        self.assertEqual(1, view["version"])

    def test_list_published_applies_redaction(self):
        self._submit()
        self._approve_all("st1")
        self._publish("st1")
        listing = self.family.list_published(actor_id="au1", view_scope="community")
        self.assertEqual(1, len(listing["items"]))
        self.assertEqual("community", listing["items"][0]["effective_scope"])
        self.assertTrue(all(p["name_status"] == "redacted" for p in listing["items"][0]["persons"]))

    # ------------------------------------------------------------ 审计与解释

    def test_access_audit_is_append_only_and_explains_hidden(self):
        self._submit()
        self._approve_all("st1")
        self._publish("st1")
        self.family.view_story(actor_id="au1", story_id="st1", view_scope="class")
        self.family.narrow_consent(request_id="n1", actor_id="op1", story_id="st1",
                                   subject_id="grandma", scope="class")
        view = self.family.view_story(actor_id="au1", story_id="st1", view_scope="community")
        self.assertEqual("class", view["effective_scope"])
        audits = self.family.list_access_audit(actor_id="au1", story_id="st1")
        self.assertEqual(2, len([a for a in audits if a["view_scope"] in ("class", "community")]))
        explain = self.family.explain_story(actor_id="au1", story_id="st1")
        self.assertTrue(explain["redaction"]["hidden"])
        self.assertIn("grandma", explain["redaction"]["hidden"][0]["reason"])

    def test_explain_requires_auditor_or_admin(self):
        self._submit()
        with self.assertRaises(PermissionDenied):
            self.family.explain_story(actor_id="op1", story_id="st1")
        with self.assertRaises(PermissionDenied):
            self.family.list_access_audit(actor_id="rv1", story_id="st1")

    def test_auditor_cannot_write(self):
        with self.assertRaises(PermissionDenied):
            self.family.submit_story(request_id="x", actor_id="au1", site_id="s1", story_id="st-x",
                                     title="t", outline="o", summary="s",
                                     persons=PERSONS, consents=consents())

    # ------------------------------------------------------------ 幂等与批量导入

    def test_submit_idempotent_replay(self):
        first = self._submit()
        second = self.family.submit_story(
            request_id="sub-st1", actor_id="op1", site_id="s1", story_id="st1",
            title="一封家书", outline="访谈提纲：家书来历", summary="三代守护家书",
            persons=PERSONS, consents=consents())
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        with self.assertRaises(ConflictError):
            self.family.submit_story(request_id="sub-st1", actor_id="op1", site_id="s1",
                                     story_id="st1", title="篡改", outline="o", summary="s",
                                     persons=PERSONS, consents=consents())

    def _batch_items(self):
        return [
            {"story_id": "b1", "title": "外婆的账本", "outline": "提纲A", "summary": "摘要A",
             "persons": [{"subject_id": "wp", "relation": "外婆", "name": "李婆婆"}],
             "consents": [{"subject_id": "wp", "relation": "外婆", "scope": "community", "note": ""}]},
            {"story_id": "b2", "title": "修表匠", "outline": "提纲B", "summary": "摘要B",
             "persons": [{"subject_id": "uj", "relation": "舅舅", "name": "陈师傅"}],
             "consents": [{"subject_id": "uj", "relation": "舅舅", "scope": "class", "note": ""}]},
        ]

    def test_batch_import_all_or_nothing(self):
        items = self._batch_items()
        items[1]["consents"] = []
        with self.assertRaises(ValidationError):
            self.family.batch_import(request_id="batch-1", actor_id="op1", site_id="s1",
                                     source_id="drive-1", items=items)
        with self.assertRaises(NotFoundError):
            self.family.view_story(actor_id="rv1", story_id="b1")

    def test_batch_import_replay_and_tamper_detection(self):
        items = self._batch_items()
        first = self.family.batch_import(request_id="batch-1", actor_id="op1", site_id="s1",
                                         source_id="drive-1", items=items)
        replay = self.family.batch_import(request_id="batch-2", actor_id="op1", site_id="s1",
                                          source_id="drive-1", items=items)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        tampered = self._batch_items()
        tampered[0]["summary"] = "被替换的内容"
        with self.assertRaises(ConflictError):
            self.family.batch_import(request_id="batch-3", actor_id="op1", site_id="s1",
                                     source_id="drive-1", items=tampered)
        explain = self.family.explain_story(actor_id="au1", story_id="b1")
        self.assertEqual("import", explain["version_chain"][0]["change_reason"])

    # ------------------------------------------------------------ 组织隔离

    def test_cross_org_access_denied(self):
        self.foundation.register_organization(request_id="org2", actor_id="admin1",
                                              organization_id="o2", name="另一个联合体")
        self.foundation.register_actor(request_id="a-op9", actor_id="admin1", new_actor_id="op9",
                                       display_name="外部采集员", role="operator", organization_id="o2")
        self._submit()
        with self.assertRaises(PermissionDenied):
            self.family.view_story(actor_id="op9", story_id="st1")
        with self.assertRaises(PermissionDenied):
            self.family.submit_story(request_id="x-org", actor_id="op9", site_id="s1",
                                     story_id="st-y", title="t", outline="o", summary="s",
                                     persons=PERSONS, consents=consents())


if __name__ == "__main__":
    unittest.main()
