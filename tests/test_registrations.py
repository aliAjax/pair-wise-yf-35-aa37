import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    EligibilityBlocked,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

ADMIN = Actor("admin", "admin")
TODAY = date(2026, 9, 27)
EVENT_DAY = TODAY + timedelta(days=20)


class RegistrationWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(
            self.repo, RuleEngine(), clock=lambda: TODAY
        )

    def tearDown(self):
        self.tmp.cleanup()

    # ---- builders --------------------------------------------------------

    def create_athlete(self, name="Rider A", team_id=None):
        data = {"name": name, "discipline": "cycling"}
        if team_id:
            data["team_id"] = team_id
        return self.service.create(ADMIN, "athlete", data)

    def create_team(self, name="Team One"):
        return self.service.create(ADMIN, "team", {"name": name})

    def create_personnel(self, team_id, role="doctor", name="Doc D"):
        return self.service.create(
            ADMIN, "personnel", {"team_id": team_id, "role": role, "name": name}
        )

    def _hearing_and_decide(self, case_id, decision, start, end):
        self.service.transition(
            ADMIN, case_id, "provisional_suspend", {"reason": "review"}
        )
        self.service.transition(
            ADMIN, case_id, "schedule_hearing", {"hearing_at": str(TODAY)}
        )
        data = {"decision": decision}
        if decision == "sanction":
            data.update({"start_date": str(start), "end_date": str(end)})
        return self.service.transition(ADMIN, case_id, "decide", data)

    def sanction_athlete(self, athlete_id, start=TODAY, end=EVENT_DAY + timedelta(days=10)):
        sample = self.service.create(
            ADMIN,
            "sample",
            {"athlete_id": athlete_id, "sample_code": "S-1", "event": "national-final"},
        )
        for action, payload in (
            ("collect", {"collected_at": "2026-01-01T08:00:00Z"}),
            ("seal", {"seal_id": "SEAL-1"}),
            ("ship", {"carrier": "C"}),
            ("receive", {"lab_id": "LAB-1"}),
            ("analyze", {"result": "adverse"}),
            ("report_adverse", {}),
        ):
            sample = self.service.transition(ADMIN, sample["id"], action, payload)
        case = self.service.create(
            ADMIN,
            "case",
            {
                "subject_kind": "athlete",
                "subject_id": athlete_id,
                "sample_id": sample["id"],
                "alleged_rule": "substance-1",
            },
        )
        self._hearing_and_decide(case["id"], "sanction", start, end)
        return case["id"]

    def sanction_subject(self, subject_kind, subject_id, start=TODAY,
                         end=EVENT_DAY + timedelta(days=10), rule="anti-tampering"):
        case = self.service.create(
            ADMIN,
            "case",
            {"subject_kind": subject_kind, "subject_id": subject_id, "alleged_rule": rule},
        )
        self._hearing_and_decide(case["id"], "sanction", start, end)
        return case["id"]

    def confirm(self, athlete_id, event="national-final", event_day=EVENT_DAY, result=None):
        data = {"athlete_id": athlete_id, "event": event, "event_date": str(event_day)}
        if result:
            data["result"] = result
        return self.service.confirm_registration(ADMIN, data)

    # ---- confirm gate ----------------------------------------------------

    def test_athlete_sanction_blocks_confirmation_and_lists_items(self):
        athlete = self.create_athlete()
        self.sanction_athlete(athlete["id"])
        with self.assertRaises(EligibilityBlocked) as caught:
            self.confirm(athlete["id"])
        items = caught.exception.items
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["subject_kind"], "athlete")
        self.assertEqual(items[0]["subject_id"], athlete["id"])
        self.assertEqual(items[0]["reason"], "substance-1")
        # No registration record was created.
        self.assertEqual(self.service.list("registration"), [])

    def test_team_doctor_sanction_blocks_whole_team(self):
        team = self.create_team()
        rider1 = self.create_athlete("Rider 1", team_id=team["id"])
        rider2 = self.create_athlete("Rider 2", team_id=team["id"])
        outsider = self.create_athlete("Outsider")
        doctor = self.create_personnel(team["id"], "doctor", "Doc D")
        self.sanction_subject("personnel", doctor["id"])
        for rider in (rider1, rider2):
            with self.assertRaises(EligibilityBlocked) as caught:
                self.confirm(rider["id"])
            self.assertEqual(caught.exception.items[0]["subject_kind"], "personnel")
            self.assertEqual(caught.exception.items[0]["subject_name"], "Doc D")
        # Athletes outside the team still register normally.
        reg = self.confirm(outsider["id"])
        self.assertEqual(reg["status"], "confirmed")

    def test_team_sanction_blocks_all_members(self):
        team = self.create_team()
        rider = self.create_athlete(team_id=team["id"])
        self.sanction_subject("team", team["id"], rule="team-violation")
        with self.assertRaises(EligibilityBlocked) as caught:
            self.confirm(rider["id"])
        self.assertEqual(caught.exception.items[0]["subject_kind"], "team")

    def test_expired_sanction_allows_registration(self):
        athlete = self.create_athlete()
        self.sanction_athlete(
            athlete["id"], start=TODAY - timedelta(days=40), end=TODAY - timedelta(days=1)
        )
        reg = self.confirm(athlete["id"])
        self.assertEqual(reg["status"], "confirmed")

    # ---- post-registration effects ---------------------------------------

    def test_later_sanction_invalidates_registration_but_keeps_result(self):
        athlete = self.create_athlete()
        reg = self.confirm(athlete["id"], result="gold")
        self.assertEqual(reg["status"], "confirmed")
        # Sanction is decided after the entry was confirmed; the decision
        # triggers a reconcile sweep automatically.
        self.sanction_athlete(athlete["id"])
        stored = self.service.get(reg["id"])
        self.assertEqual(stored["status"], "invalidated")
        self.assertEqual(stored["data"]["result"], "gold")
        self.assertTrue(stored["data"]["invalidated_by"])

        view = self.service.registrations_view("national-final")
        entry = view["items"][0]
        self.assertEqual(entry["status"], "invalidated")
        self.assertEqual(entry["effective_status"], "invalidated")
        self.assertEqual(entry["result"], "gold")

    def test_appeal_overturn_restores_registration_with_result(self):
        athlete = self.create_athlete()
        reg = self.confirm(athlete["id"], result="silver")
        case_id = self.sanction_athlete(athlete["id"])
        self.assertEqual(self.service.get(reg["id"])["status"], "invalidated")

        self.service.transition(ADMIN, case_id, "appeal", {"grounds": "new evidence"})
        self.service.transition(
            ADMIN, case_id, "resolve_appeal", {"decision": "no_sanction"}
        )
        stored = self.service.get(reg["id"])
        self.assertEqual(stored["status"], "confirmed")
        self.assertEqual(stored["data"]["result"], "silver")
        self.assertNotIn("invalidated_by", stored["data"])
        sanction = self.service.list("sanction")[0]
        self.assertEqual(sanction["status"], "revoked")

    def test_restored_only_after_sanction_period_expires(self):
        athlete = self.create_athlete()
        event_day = TODAY + timedelta(days=5)
        end = TODAY + timedelta(days=10)
        reg = self.confirm(athlete["id"], event_day=event_day, result="bronze")
        self.sanction_athlete(athlete["id"], start=TODAY, end=end)
        self.assertEqual(self.service.get(reg["id"])["status"], "invalidated")

        # Still inside the sanction period: no recovery.
        before = self.service.reconcile_registrations(
            ADMIN, as_of=TODAY + timedelta(days=8)
        )
        self.assertEqual(before, {"invalidated": [], "restored": []})
        self.assertEqual(self.service.get(reg["id"])["status"], "invalidated")

        # Period ended: entry recovers, original result survives.
        after = self.service.reconcile_registrations(
            ADMIN, as_of=end + timedelta(days=1)
        )
        self.assertEqual(after["restored"], [reg["id"]])
        stored = self.service.get(reg["id"])
        self.assertEqual(stored["status"], "confirmed")
        self.assertEqual(stored["data"]["result"], "bronze")

    # ---- views, guards and access ----------------------------------------

    def test_eligibility_view_separates_lists(self):
        clean = self.create_athlete("Clean Rider")
        dirty = self.create_athlete("Dirty Rider")
        self.sanction_athlete(dirty["id"])
        view = self.service.eligibility_view("national-final", EVENT_DAY)
        self.assertEqual([a["athlete_id"] for a in view["eligible"]], [clean["id"]])
        blocked = view["blocked"]
        self.assertEqual([a["athlete_id"] for a in blocked], [dirty["id"]])
        self.assertEqual(blocked[0]["blocks"][0]["subject_kind"], "athlete")

    def test_managed_kinds_reject_generic_create(self):
        with self.assertRaises(ValidationError):
            self.service.create(ADMIN, "sanction", {"subject_id": "x"})
        with self.assertRaises(ValidationError):
            self.service.create(ADMIN, "registration", {"athlete_id": "x"})

    def test_confirm_requires_permission(self):
        athlete = self.create_athlete()
        with self.assertRaises(PermissionDenied):
            self.service.confirm_registration(
                Actor("v", "viewer"),
                {"athlete_id": athlete["id"], "event": "e", "event_date": str(EVENT_DAY)},
            )

    def test_duplicate_registration_conflicts(self):
        athlete = self.create_athlete()
        self.confirm(athlete["id"])
        with self.assertRaises(ConflictError):
            self.confirm(athlete["id"])

    def test_confirm_idempotent(self):
        athlete = self.create_athlete()
        first = self.service.confirm_registration(
            ADMIN,
            {"athlete_id": athlete["id"], "event": "e", "event_date": str(EVENT_DAY)},
            idempotency_key="reg-1",
        )
        second = self.service.confirm_registration(
            ADMIN,
            {"athlete_id": athlete["id"], "event": "e", "event_date": str(EVENT_DAY)},
            idempotency_key="reg-1",
        )
        self.assertEqual(first["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
