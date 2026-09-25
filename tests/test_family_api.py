import unittest
from datetime import datetime, timezone

from festival_foundation.clock import FixedClock
from festival_foundation.service import DomainService
from festival_foundation.storage import Database

from family_stories.api import route
from family_stories.service import FamilyStoryService

PERSONS = [{"subject_id": "grandma", "relation": "祖母", "name": "王奶奶"}]
CONSENTS = [{"subject_id": "grandma", "relation": "祖母", "scope": "community", "note": ""}]


class FamilyApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.family = FamilyStoryService(self.database, clock)

    def tearDown(self):
        self.database.close()

    def _register_all(self):
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="联合体")
        self.foundation.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin1",
                                       display_name="管理员", role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="a-op1", actor_id="admin1", new_actor_id="op1",
                                       display_name="采集员一", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="a-op2", actor_id="admin1", new_actor_id="op2",
                                       display_name="采集员二", role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="a-rv1", actor_id="admin1", new_actor_id="rv1",
                                       display_name="核对员", role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="a-au", actor_id="admin1", new_actor_id="au1",
                                       display_name="审计员", role="auditor", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="admin1", site_id="s1",
                                      organization_id="o1", name="采集点", timezone_name="Asia/Shanghai")

    def _submit_and_publish(self):
        status, _ = route(self.family, self.foundation, "POST", "/stories",
                          {"request_id": "st1", "site_id": "s1", "story_id": "st1",
                           "title": "一封家书", "outline": "提纲", "summary": "摘要",
                           "persons": PERSONS, "consents": CONSENTS},
                          {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        for actor, stage, req in [("rv1", "fact_check", "d1"), ("admin1", "sensitive_review", "d2"),
                                  ("op2", "consent_confirm", "d3")]:
            status, claim = route(self.family, self.foundation, "POST", "/stories/st1/review-claims",
                                  {"stage": stage}, {"X-Actor-Id": actor})
            self.assertEqual(200, status)
            status, _ = route(self.family, self.foundation, "POST", "/stories/st1/review-decisions",
                              {"request_id": req, "lease_id": claim["lease_id"],
                               "decision": "approved", "note": "通过"},
                              {"X-Actor-Id": actor})
            self.assertEqual(201, status)
        status, _ = route(self.family, self.foundation, "POST", "/stories/st1/publication",
                          {"request_id": "pub1"}, {"X-Actor-Id": "admin1"})
        self.assertEqual(201, status)

    def test_full_flow_over_http(self):
        self._register_all()
        self._submit_and_publish()
        status, view = route(self.family, self.foundation, "GET",
                             "/stories/st1/view?view_scope=community", None,
                             {"X-Actor-Id": "au1"})
        self.assertEqual(200, status)
        self.assertEqual("community", view["effective_scope"])
        self.assertEqual("redacted", view["persons"][0]["name_status"])
        status, explanation = route(self.family, self.foundation, "GET",
                                    "/stories/st1/explain", None, {"X-Actor-Id": "au1"})
        self.assertEqual(200, status)
        self.assertEqual(3, len(explanation["review_opinions"]))
        status, audits = route(self.family, self.foundation, "GET",
                               "/access-audit?story_id=st1", None, {"X-Actor-Id": "au1"})
        self.assertEqual(200, status)
        self.assertTrue(audits["items"])

    def test_foundation_routes_still_work(self):
        self._register_all()
        status, payload = route(self.family, self.foundation, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_returns_404(self):
        status, payload = route(self.family, self.foundation, "GET", "/stories/x/unknown", None,
                                {"X-Actor-Id": "admin1"})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_invalid_body_returns_400(self):
        self._register_all()
        status, payload = route(self.family, self.foundation, "POST", "/stories",
                                {"request_id": "bad"}, {"X-Actor-Id": "op1"})
        self.assertEqual(400, status)
        self.assertEqual("invalid_request", payload["error"])

    def test_review_queue_endpoint(self):
        self._register_all()
        route(self.family, self.foundation, "POST", "/stories",
              {"request_id": "st1", "site_id": "s1", "story_id": "st1", "title": "t",
               "outline": "o", "summary": "s", "persons": PERSONS, "consents": CONSENTS},
              {"X-Actor-Id": "op1"})
        status, queue = route(self.family, self.foundation, "GET", "/review-queue", None,
                              {"X-Actor-Id": "rv1"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(queue["items"]))
        self.assertEqual("fact_check", queue["items"][0]["stage"])


if __name__ == "__main__":
    unittest.main()
