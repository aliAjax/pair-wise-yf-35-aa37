"""Event entry eligibility adjudication.

Pure read-side logic, kept separate from registration records and HTTP
entry points: given the current entity set it merges the effective
sanctions of an athlete, the athlete's team and the team's staff
(coaches / doctors). Any sanction covering the event date blocks entry.
"""

from dataclasses import dataclass
from datetime import date

from .rules import _parse_date

SUBJECT_KINDS = ("athlete", "team", "personnel")


def _today():
    return date.today()


@dataclass
class BlockItem:
    sanction_id: str
    case_id: str
    subject_kind: str
    subject_id: str
    subject_name: str
    start_date: str
    end_date: str
    reason: str

    def to_dict(self):
        return {
            "sanction_id": self.sanction_id,
            "case_id": self.case_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "subject_name": self.subject_name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "reason": self.reason,
        }


class EligibilityEngine:
    def __init__(self, list_entities, get_entity=None, clock=_today):
        self._list = list_entities
        self._get = get_entity
        self._clock = clock

    # ---- subject / sanction resolution ----------------------------------

    def subject_refs_for_athlete(self, athlete):
        """Refs whose sanctions count against this athlete."""
        refs = [("athlete", athlete["id"])]
        team_id = athlete["data"].get("team_id")
        if team_id:
            refs.append(("team", team_id))
            for member in self._list("personnel"):
                if member["status"] == "active" and member["data"].get("team_id") == team_id:
                    refs.append(("personnel", member["id"]))
        return refs

    def active_sanctions(self):
        return [s for s in self._list("sanction") if s["status"] == "active"]

    @staticmethod
    def sanction_covers(sanction, event_date, as_of):
        data = sanction["data"]
        try:
            start = _parse_date(data.get("start_date"), "start_date")
            end = _parse_date(data.get("end_date"), "end_date")
        except Exception:
            return False
        return start <= event_date <= end and end >= as_of

    def _to_item(self, sanction, event_date, as_of):
        data = sanction["data"]
        subject_kind = data.get("subject_kind", "athlete")
        subject_id = data.get("subject_id", "")
        return BlockItem(
            sanction_id=sanction["id"],
            case_id=data.get("case_id", ""),
            subject_kind=subject_kind,
            subject_id=subject_id,
            subject_name=data.get("subject_name", subject_id),
            start_date=str(data.get("start_date", "")),
            end_date=str(data.get("end_date", "")),
            reason=data.get("alleged_rule", ""),
        )

    def blocks_for_athlete(self, athlete, event_date, as_of=None):
        """All effective block items for one athlete on a date."""
        as_of = as_of or self._clock()
        refs = set(self.subject_refs_for_athlete(athlete))
        items = []
        seen = set()
        for sanction in self.active_sanctions():
            data = sanction["data"]
            ref = (data.get("subject_kind"), data.get("subject_id"))
            if ref in refs and self.sanction_covers(sanction, event_date, as_of):
                if sanction["id"] not in seen:
                    seen.add(sanction["id"])
                    items.append(self._to_item(sanction, event_date, as_of))
        items.sort(key=lambda item: (item.end_date, item.subject_kind, item.subject_id))
        return items

    # ---- roster / registration views ------------------------------------

    def roster_view(self, event, event_date, as_of=None, registrations=None):
        """Split athletes into eligible vs blocked, with reg references."""
        as_of = as_of or self._clock()
        registrations = registrations or []
        reg_by_athlete = {r["data"].get("athlete_id"): r for r in registrations}
        eligible, blocked = [], []
        for athlete in self._list("athlete"):
            if athlete["status"] != "active":
                continue
            items = self.blocks_for_athlete(athlete, event_date, as_of)
            reg = reg_by_athlete.get(athlete["id"])
            entry = {
                "athlete_id": athlete["id"],
                "name": athlete["data"].get("name", athlete["id"]),
                "team_id": athlete["data"].get("team_id"),
                "registration_id": reg["id"] if reg else None,
                "registration_status": reg["status"] if reg else None,
            }
            if items:
                entry["blocks"] = [item.to_dict() for item in items]
                blocked.append(entry)
            else:
                eligible.append(entry)
        eligible.sort(key=lambda item: item["name"])
        blocked.sort(key=lambda item: item["name"])
        return {
            "event": event,
            "event_date": str(event_date),
            "as_of": str(as_of),
            "eligible": eligible,
            "blocked": blocked,
        }
