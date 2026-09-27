import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src import eligibility
from src.domain import Actor, ConflictError, RegistrationBlocked, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _d(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


class RegistrationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.team = self.service.create(self.admin, "team", {"name": "Speed Club"})
        self.athlete = self.service.create(
            self.admin, "athlete",
            {"name": "A. Rider", "discipline": "cycling", "team_id": self.team["id"]},
        )
        self.teammate = self.service.create(
            self.admin, "athlete",
            {"name": "B. Sprinter", "discipline": "cycling", "team_id": self.team["id"]},
        )
        self.outsider = self.service.create(
            self.admin, "athlete", {"name": "C. Solo", "discipline": "cycling"}
        )
        self.doctor = self.service.create(
            self.admin, "personnel",
            {"name": "Dr. D", "role": "doctor", "team_id": self.team["id"]},
        )
        self.event = self.service.create(
            self.admin, "event",
            {"name": "National Final", "start_date": _d(10), "end_date": _d(12)},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _sanction(self, **data):
        payload = {"start_date": _d(-5), "end_date": _d(20), "reason": "ADRV"}
        payload.update(data)
        return self.service.create(self.admin, "sanction", payload)

    def _register(self, athlete):
        return self.service.create(
            self.admin, "registration",
            {"athlete_id": athlete["id"], "event_id": self.event["id"]},
        )

    def _confirm(self, registration):
        return self.service.transition(self.admin, registration["id"], "confirm", {})

    def test_athlete_sanction_blocks_confirm_and_lists_items(self):
        self._sanction(subject_kind="athlete", subject_id=self.athlete["id"])
        with self.assertRaises(RegistrationBlocked) as ctx:
            self._confirm(self._register(self.athlete))
        self.assertEqual(len(ctx.exception.blockers), 1)
        self.assertEqual(ctx.exception.blockers[0]["subject_kind"], "athlete")
        self.assertEqual(
            self._confirm(self._register(self.teammate))["status"], "confirmed"
        )
        self.assertEqual(
            self._confirm(self._register(self.outsider))["status"], "confirmed"
        )

    def test_team_sanction_blocks_whole_team(self):
        self._sanction(subject_kind="team", subject_id=self.team["id"])
        for athlete in (self.athlete, self.teammate):
            with self.assertRaises(RegistrationBlocked):
                self._confirm(self._register(athlete))
        self.assertEqual(
            self._confirm(self._register(self.outsider))["status"], "confirmed"
        )

    def test_personnel_sanction_blocks_team(self):
        self._sanction(subject_kind="personnel", subject_id=self.doctor["id"])
        with self.assertRaises(RegistrationBlocked) as ctx:
            self._confirm(self._register(self.teammate))
        self.assertEqual(ctx.exception.blockers[0]["subject_kind"], "personnel")
        self.assertEqual(
            self._confirm(self._register(self.outsider))["status"], "confirmed"
        )

    def test_non_covering_or_expired_sanction_allows_confirm(self):
        self._sanction(
            subject_kind="athlete", subject_id=self.athlete["id"],
            start_date=_d(-30), end_date=_d(-10),
        )
        self._sanction(
            subject_kind="team", subject_id=self.team["id"],
            start_date=_d(30), end_date=_d(40),
        )
        self.assertEqual(
            self._confirm(self._register(self.athlete))["status"], "confirmed"
        )

    def test_covering_sanction_invalidates_existing_and_preserves_result(self):
        registration = self._register(self.athlete)
        self._confirm(registration)
        self.service.transition(
            self.admin, registration["id"], "record_result", {"result": "1:23:45"}
        )
        self._sanction(subject_kind="personnel", subject_id=self.doctor["id"])
        updated = self.service.get(registration["id"])
        self.assertEqual(updated["status"], "invalid")
        self.assertEqual(updated["data"]["result"], "1:23:45")

    def test_restore_only_after_sanction_ends(self):
        registration = self._register(self.athlete)
        self._confirm(registration)
        sanction = self._sanction(subject_kind="athlete", subject_id=self.athlete["id"])
        self.assertEqual(self.service.get(registration["id"])["status"], "invalid")
        with self.assertRaises(RegistrationBlocked):
            self.service.transition(self.admin, registration["id"], "restore", {})
        self.service.transition(self.admin, sanction["id"], "lift", {})
        self.assertEqual(self.service.get(registration["id"])["status"], "confirmed")

    def test_refresh_restores_after_expiry(self):
        event = {"data": {"start_date": "2026-10-01", "end_date": "2026-10-03"}}
        sanction = {
            "status": "active",
            "data": {"start_date": "2026-09-25", "end_date": "2026-10-02"},
        }
        self.assertTrue(
            eligibility.sanction_covers_event(sanction, event, today=date(2026, 9, 27))
        )
        self.assertFalse(
            eligibility.sanction_covers_event(sanction, event, today=date(2026, 10, 3))
        )

    def test_eligibility_summary_splits_registrable_and_blocked(self):
        self._sanction(subject_kind="personnel", subject_id=self.doctor["id"])
        summary = self.service.check_registration_eligibility(event_id=self.event["id"])
        self.assertEqual([x["name"] for x in summary["registrable"]], ["C. Solo"])
        self.assertEqual(
            sorted(x["name"] for x in summary["blocked"]), ["A. Rider", "B. Sprinter"]
        )
        self.assertTrue(summary["blocked"][0]["blockers"])

    def test_case_decision_with_ban_dates_sanctions_and_invalidates(self):
        registration = self._register(self.athlete)
        self._confirm(registration)
        sample = self.service.create(
            self.admin, "sample",
            {"athlete_id": self.athlete["id"], "sample_code": "S-1", "event": "national-final"},
        )
        for action, data in [
            ("collect", {"collected_at": "2026-01-01"}),
            ("seal", {"seal_id": "SEAL-1"}),
            ("ship", {"carrier": "Courier-A"}),
            ("receive", {"lab_id": "LAB-1"}),
            ("analyze", {"result": "adverse"}),
            ("report_adverse", {}),
        ]:
            self.service.transition(self.admin, sample["id"], action, data)
        case = self.service.create(
            self.admin, "case",
            {"athlete_id": self.athlete["id"], "sample_id": sample["id"], "alleged_rule": "substance-1"},
        )
        self.service.transition(self.admin, case["id"], "provisional_suspend", {"reason": "adverse"})
        self.service.transition(self.admin, case["id"], "schedule_hearing", {"hearing_at": "2026-02-01"})
        self.service.transition(
            self.admin, case["id"], "decide",
            {"decision": "sanction", "ban_start": _d(-1), "ban_end": _d(30)},
        )
        sanctions = self.service.list("sanction")
        self.assertEqual(len(sanctions), 1)
        self.assertEqual(sanctions[0]["data"]["case_id"], case["id"])
        self.assertEqual(self.service.get(registration["id"])["status"], "invalid")

    def test_duplicate_registration_rejected(self):
        self._register(self.athlete)
        with self.assertRaises(ConflictError):
            self._register(self.athlete)

    def test_sanction_validation(self):
        with self.assertRaises(ValidationError):
            self._sanction(subject_kind="event", subject_id=self.event["id"])
        with self.assertRaises(ValidationError):
            self._sanction(
                subject_kind="athlete", subject_id=self.athlete["id"],
                start_date=_d(10), end_date=_d(-10),
            )
        with self.assertRaises(ValidationError):
            self._sanction(subject_kind="athlete", subject_id="missing")


if __name__ == "__main__":
    unittest.main()
