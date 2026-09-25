import unittest

from festival_foundation.api import route
from festival_foundation.service import DomainService
from festival_foundation.stories_service import StoryService
from festival_foundation.storage import Database


CONTENT = {
    "title": "爷爷的修书摊",
    "interview_outline": ["q1", "q2"],
    "summary": "爷爷摆摊修书四十年，街坊都认得他。",
    "subjects": [{"key": "subject:grandpa", "display_name": "赵爷爷"}],
    "citations": [{"subject_key": "subject:grandpa", "text": "书坏了能修，人心不能坏。"}],
}


class StoryApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.stories = StoryService(self.database, self.service)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "家校联盟"},
              {"X-Actor-Id": "bootstrap"})
        status, _ = route(self.service, "POST", "/actors",
                          {"request_id": "admin", "new_actor_id": "admin1",
                           "display_name": "管理员", "role": "admin",
                           "organization_id": "o1"},
                          {"X-Actor-Id": "bootstrap"})
        self.assertIn(status, (200, 201))
        for rid, aid, role in [
            ("a1", "op1", "operator"), ("a2", "rv1", "reviewer"),
            ("a3", "rv2", "reviewer"), ("a4", "rv3", "reviewer"),
            ("a5", "au1", "auditor"),
        ]:
            status, _ = route(self.service, "POST", "/actors",
                              {"request_id": rid, "new_actor_id": aid,
                               "display_name": aid, "role": role,
                               "organization_id": "o1"},
                              {"X-Actor-Id": "admin1"})
            self.assertIn(status, (200, 201))
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "实验学校", "timezone_name": "Asia/Shanghai"},
              {"X-Actor-Id": "op1"})

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="op1", query=None):
        full_path = path if query is None else f"{path}?{query}"
        return route(self.service, method, full_path, body or {},
                     {"X-Actor-Id": actor}, stories=self.stories)

    def test_full_publish_flow_over_http(self):
        status, payload = self.call("POST", "/stories", {
            "request_id": "st1", "site_id": "s1", "external_key": "k1",
            "content": CONTENT,
        })
        self.assertEqual(201, status)
        story_id = payload["resource_id"]

        # 重放返回 200
        status, replay = self.call("POST", "/stories", {
            "request_id": "st1", "site_id": "s1", "external_key": "k1",
            "content": CONTENT,
        })
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        status, _ = self.call("POST", "/story-consents", {
            "request_id": "cs1", "story_id": story_id,
            "subject_key": "subject:grandpa", "scope": "archive", "granted": True,
            "statement_type": "initial", "reason": "本人签署",
        })
        self.assertIn(status, (200, 201))

        for i, (rid, reviewer, stage) in enumerate([
            ("c1", "rv1", "fact_check"), ("c2", "rv2", "sensitive_check"),
            ("c3", "rv3", "consent_confirm"),
        ]):
            status, _ = self.call("POST", "/review-claims",
                                  {"request_id": rid, "story_id": story_id, "stage": stage},
                                  actor=reviewer)
            self.assertIn(status, (200, 201))
            status, _ = self.call("POST", "/review-decisions", {
                "request_id": f"d{i}", "story_id": story_id, "stage": stage,
                "decision": "approve", "opinion": "通过",
            }, actor=reviewer)
            self.assertIn(status, (200, 201))

        status, payload = self.call("POST", "/story-publications",
                                    {"request_id": "pub", "story_id": story_id})
        self.assertIn(status, (200, 201))

        status, payload = self.call("GET", "/published-story", query=f"story_id={story_id}")
        self.assertEqual(200, status)
        self.assertEqual("archive", payload["served_scope"])
        self.assertIn("修书", payload["content"]["summary"])

    def test_visibility_explanation_requires_auditor_role(self):
        status, payload = self.call("POST", "/stories", {
            "request_id": "st1", "site_id": "s1", "external_key": "k1",
            "content": CONTENT,
        })
        story_id = payload["resource_id"]
        status, _ = self.call("GET", "/story-visibility-explanation",
                              query=f"story_id={story_id}", actor="rv1")
        self.assertEqual(403, status)

    def test_validation_error_for_bad_content(self):
        status, payload = self.call("POST", "/stories", {
            "request_id": "bad", "site_id": "s1", "external_key": "k2",
            "content": {"title": ""},
        })
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
