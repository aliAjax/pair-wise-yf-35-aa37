"""报名资格判定：合并运动员本人、所属队伍与团队人员的有效处罚。"""

from datetime import date, datetime

SUBJECT_KINDS = ("athlete", "team", "personnel")


def parse_date(value):
    return datetime.fromisoformat(str(value)[:10]).date()


def event_range(event):
    start = parse_date(event["data"].get("start_date"))
    end = parse_date(event["data"].get("end_date") or event["data"].get("start_date"))
    return start, end


def related_subjects(find, athlete):
    """运动员本人、所属队伍以及该队现役团队人员。"""
    subjects = [
        {
            "kind": "athlete",
            "id": athlete["id"],
            "name": athlete["data"].get("name") or athlete["id"],
        }
    ]
    team_id = athlete["data"].get("team_id")
    if not team_id:
        return subjects
    teams = find("team", "id", team_id) or []
    if not teams:
        return subjects
    team = teams[0]
    subjects.append(
        {
            "kind": "team",
            "id": team["id"],
            "name": team["data"].get("name") or team["id"],
        }
    )
    for person in find("personnel", "team_id", team_id) or []:
        if person["status"] != "active":
            continue
        subjects.append(
            {
                "kind": "personnel",
                "id": person["id"],
                "name": person["data"].get("name") or person["id"],
            }
        )
    return subjects


def sanction_covers_event(sanction, event, today=None):
    """处罚未解除、未期满且处罚期覆盖比赛日期。"""
    if sanction["status"] != "active":
        return False
    today = today or date.today()
    start = parse_date(sanction["data"].get("start_date"))
    end = parse_date(sanction["data"].get("end_date"))
    if end < today:
        return False
    event_start, event_end = event_range(event)
    return start <= event_end and event_start <= end


def check_eligibility(find, athlete, event, today=None):
    """合并三方有效处罚，返回 (ok, blockers)；blockers 列出命中的处罚事项。"""
    blockers = []
    for subject in related_subjects(find, athlete):
        for sanction in find("sanction", "subject_id", subject["id"]) or []:
            data = sanction["data"]
            if data.get("subject_kind") != subject["kind"]:
                continue
            if not sanction_covers_event(sanction, event, today=today):
                continue
            blockers.append(
                {
                    "sanction_id": sanction["id"],
                    "case_id": data.get("case_id"),
                    "subject_kind": subject["kind"],
                    "subject_id": subject["id"],
                    "subject_name": subject["name"],
                    "reason": data.get("reason", ""),
                    "start_date": data.get("start_date"),
                    "end_date": data.get("end_date"),
                }
            )
    return (not blockers, blockers)
