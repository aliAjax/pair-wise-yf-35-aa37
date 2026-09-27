import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

TODAY = date(2026, 9, 27)
EVENT_DAY = TODAY + timedelta(days=20)


def _request(method, url, body=None, headers=None):
    data = None
    req_headers = headers or {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "http.db")
        service = DomainService(repo, RuleEngine(), clock=lambda: TODAY)
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), static_dir=".")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = "http://%s:%s" % (host, port)
        self.headers = {"X-User-Id": "admin", "X-Role": "admin"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _create_athlete(self, name):
        status, body = _request(
            "POST", self.base + "/api/athletes",
            {"name": name, "discipline": "cycling"}, self.headers,
        )
        self.assertEqual(status, 201)
        return body["id"]

    def _ban_subject(self, subject_kind, subject_id, rule="substance-1", sample_id=None):
        case_data = {"subject_kind": subject_kind, "subject_id": subject_id, "alleged_rule": rule}
        if sample_id:
            case_data["sample_id"] = sample_id
        status, case = _request("POST", self.base + "/api/cases", case_data, self.headers)
        self.assertEqual(status, 201, case)
        for action, payload in (
            ("provisional_suspend", {"reason": "r"}),
            ("schedule_hearing", {"hearing_at": str(TODAY)}),
            ("decide", {"decision": "sanction", "start_date": str(TODAY),
                        "end_date": str(EVENT_DAY + timedelta(days=5))}),
        ):
            status, body = _request(
                "POST", self.base + "/api/entities/" + case["id"] + "/actions",
                {"action": action, "data": payload}, self.headers,
            )
            self.assertEqual(status, 200, body)

    def test_eligibility_and_blocked_confirm_endpoints(self):
        # Personnel case: no sample required.
        status, team = _request(
            "POST", self.base + "/api/teams", {"name": "Team X"}, self.headers
        )
        self.assertEqual(status, 201)
        clean = self._create_athlete("Clean Rider")
        status, dirty_body = _request(
            "POST", self.base + "/api/athletes",
            {"name": "Dirty Rider", "discipline": "cycling", "team_id": team["id"]},
            self.headers,
        )
        self.assertEqual(status, 201)
        dirty = dirty_body["id"]
        status, doctor = _request(
            "POST", self.base + "/api/personnel",
            {"team_id": team["id"], "role": "doctor", "name": "Doc X"}, self.headers,
        )
        self.assertEqual(status, 201)
        self._ban_subject("personnel", doctor["id"])

        url = (
            self.base + "/api/eligibility?event=final&event_date=" + str(EVENT_DAY)
        )
        status, view = _request("GET", url)
        self.assertEqual(status, 200)
        self.assertEqual([a["athlete_id"] for a in view["eligible"]], [clean])
        self.assertEqual([a["athlete_id"] for a in view["blocked"]], [dirty])
        self.assertEqual(view["blocked"][0]["blocks"][0]["subject_kind"], "personnel")

        # Confirm clean athlete through the dedicated entry point.
        status, reg = _request(
            "POST", self.base + "/api/registrations/confirm",
            {"athlete_id": clean, "event": "final", "event_date": str(EVENT_DAY)},
            self.headers,
        )
        self.assertEqual(status, 201)
        self.assertEqual(reg["status"], "confirmed")

        # Blocked confirm returns 409 and lists the offending items.
        status, error = _request(
            "POST", self.base + "/api/registrations/confirm",
            {"athlete_id": dirty, "event": "final", "event_date": str(EVENT_DAY)},
            self.headers,
        )
        self.assertEqual(status, 409)
        self.assertEqual(error["type"], "EligibilityBlocked")
        self.assertEqual(error["items"][0]["subject_name"], "Doc X")

        status, listed = _request(
            "GET", self.base + "/api/registrations?event=final"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["items"]), 1)


if __name__ == "__main__":
    unittest.main()
