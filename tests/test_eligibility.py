import unittest
from datetime import date, timedelta

from src.eligibility import EligibilityEngine

TODAY = date(2026, 9, 27)
EVENT_DAY = date(2026, 10, 1)


def _entity(id, kind, status="active", **data):
    return {"id": id, "kind": kind, "status": status, "version": 1, "data": data}


def _sanction(id, subject_kind, subject_id, start, end, name=None, case="case-1"):
    return _entity(
        id,
        "sanction",
        "active",
        case_id=case,
        subject_kind=subject_kind,
        subject_id=subject_id,
        subject_name=name or subject_id,
        start_date=str(start),
        end_date=str(end),
        alleged_rule="substance-1",
    )


class EligibilityEngineTest(unittest.TestCase):
    def setUp(self):
        self.athlete = _entity("a1", "athlete", name="Rider A", team_id="t1")
        self.lonely = _entity("a9", "athlete", name="Rider Z")
        self.team = _entity("t1", "team", name="Team One")
        self.coach = _entity("p1", "personnel", role="coach", name="Coach C", team_id="t1")
        self.doctor = _entity("p2", "personnel", role="doctor", name="Doc D", team_id="t1")
        rows = [self.athlete, self.lonely, self.team, self.coach, self.doctor]
        self.engine = EligibilityEngine(
            lambda kind, status=None: [r for r in rows if r["kind"] == kind],
            clock=lambda: TODAY,
        )

    def test_no_sanction_means_eligible(self):
        self.assertEqual(self.engine.blocks_for_athlete(self.athlete, EVENT_DAY), [])

    def test_athlete_own_sanction_blocks(self):
        sanction = _sanction("s1", "athlete", "a1", TODAY, TODAY + timedelta(days=30))
        self.engine._list = lambda kind, status=None: [self.athlete, sanction]
        items = self.engine.blocks_for_athlete(self.athlete, EVENT_DAY)
        self.assertEqual([item.sanction_id for item in items], ["s1"])
        self.assertEqual(items[0].subject_kind, "athlete")

    def test_team_sanction_blocks_entire_team_but_not_outsiders(self):
        sanction = _sanction("s2", "team", "t1", TODAY, TODAY + timedelta(days=30), name="Team One")
        rows = [self.athlete, self.lonely, self.team, sanction]
        engine = EligibilityEngine(lambda kind, status=None: [r for r in rows if r["kind"] == kind])
        self.assertEqual(len(engine.blocks_for_athlete(self.athlete, EVENT_DAY)), 1)
        self.assertEqual(engine.blocks_for_athlete(self.lonely, EVENT_DAY), [])

    def test_coach_or_doctor_sanction_blocks_team(self):
        coach_ban = _sanction("s3", "personnel", "p1", TODAY, TODAY + timedelta(days=30), name="Coach C")
        rows = [self.athlete, self.coach, self.doctor, coach_ban]
        engine = EligibilityEngine(lambda kind, status=None: [r for r in rows if r["kind"] == kind])
        items = engine.blocks_for_athlete(self.athlete, EVENT_DAY)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].subject_kind, "personnel")
        self.assertEqual(items[0].subject_name, "Coach C")

    def test_expired_sanction_does_not_block(self):
        # Sanction period ended yesterday: entry is restored.
        expired = _sanction("s4", "athlete", "a1", TODAY - timedelta(days=40), TODAY - timedelta(days=1))
        engine = EligibilityEngine(lambda kind, status=None: [self.athlete, expired])
        self.assertEqual(engine.blocks_for_athlete(self.athlete, EVENT_DAY), [])

    def test_sanction_outside_event_date_does_not_block(self):
        later = _sanction("s5", "athlete", "a1", EVENT_DAY + timedelta(days=2), EVENT_DAY + timedelta(days=9))
        engine = EligibilityEngine(lambda kind, status=None: [self.athlete, later])
        self.assertEqual(engine.blocks_for_athlete(self.athlete, EVENT_DAY), [])

    def test_revoked_sanction_ignored(self):
        sanction = _sanction("s6", "athlete", "a1", TODAY, TODAY + timedelta(days=30))
        sanction["status"] = "revoked"
        engine = EligibilityEngine(lambda kind, status=None: [self.athlete, sanction])
        self.assertEqual(engine.blocks_for_athlete(self.athlete, EVENT_DAY), [])

    def test_roster_view_separates_eligible_and_blocked(self):
        ban = _sanction("s7", "athlete", "a1", TODAY, TODAY + timedelta(days=30))
        rows = [self.athlete, self.lonely, ban]
        engine = EligibilityEngine(lambda kind, status=None: [r for r in rows if r["kind"] == kind])
        view = engine.roster_view("national-final", EVENT_DAY)
        self.assertEqual([a["athlete_id"] for a in view["eligible"]], ["a9"])
        blocked = view["blocked"]
        self.assertEqual([a["athlete_id"] for a in blocked], ["a1"])
        self.assertEqual(blocked[0]["blocks"][0]["sanction_id"], "s7")
        self.assertEqual(view["event"], "national-final")


if __name__ == "__main__":
    unittest.main()
