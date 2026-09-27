from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    RegistrationBlocked,
    ValidationError,
)
from .eligibility import check_eligibility, parse_date


def _parse_or_raise(value, field):
    try:
        return parse_date(value)
    except (TypeError, ValueError):
        raise ValidationError("invalid date for %s: %s" % (field, value))


def _validate_athlete(actor, data, lookup):
    if len(data.get("discipline", "")) < 2:
        raise ValidationError("discipline is too short")
    team_id = data.get("team_id")
    if team_id and not _find_one(lookup, "team", "id", team_id):
        raise ValidationError("athlete team does not exist")


def _validate_sample(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("sample requires an active athlete")
    if not data.get("sample_code", "").strip():
        raise ValidationError("sample_code is required")


def _validate_case(actor, data, lookup):
    sample = _find_one(lookup, "sample", "id", data.get("sample_id"))
    if not sample or sample["status"] != "adverse":
        raise ValidationError("case requires an adverse sample")


def _validate_team(actor, data, lookup):
    if len(data.get("name", "")) < 2:
        raise ValidationError("team name is too short")


def _validate_personnel(actor, data, lookup):
    team = _find_one(lookup, "team", "id", data.get("team_id"))
    if not team or team["status"] != "active":
        raise ValidationError("personnel requires an active team")


def _validate_sanction(actor, data, lookup):
    subject_kind = data.get("subject_kind")
    if subject_kind not in ("athlete", "team", "personnel"):
        raise ValidationError("subject_kind must be athlete, team or personnel")
    if not _find_one(lookup, subject_kind, "id", data.get("subject_id")):
        raise ValidationError("sanction subject does not exist")
    start = _parse_or_raise(data.get("start_date"), "start_date")
    end = _parse_or_raise(data.get("end_date"), "end_date")
    if start > end:
        raise ValidationError("sanction start_date must not be after end_date")


def _validate_event(actor, data, lookup):
    start = _parse_or_raise(data.get("start_date"), "start_date")
    if data.get("end_date"):
        end = _parse_or_raise(data.get("end_date"), "end_date")
        if start > end:
            raise ValidationError("event start_date must not be after end_date")


def _validate_registration(actor, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", data.get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("registration requires an active athlete")
    event = _find_one(lookup, "event", "id", data.get("event_id"))
    if not event or event["status"] != "open":
        raise ValidationError("registration requires an open event")
    for row in lookup("registration", "athlete_id", athlete["id"]) or []:
        if row["data"].get("event_id") == event["id"] and row["status"] in ("pending", "confirmed"):
            raise ConflictError("registration already exists for this athlete and event")


def _validate_report_adverse(actor, entity, data, lookup):
    if entity["data"].get("result") != "adverse":
        raise ValidationError("only an adverse lab result can open a case")
    return {"confirmed_by": actor.user_id}


def _validate_case_decision(actor, entity, data, lookup):
    if data.get("decision") not in ("sanction", "no_sanction"):
        raise ValidationError("decision must be sanction or no_sanction")
    if data.get("decision") == "sanction":
        ban_start, ban_end = data.get("ban_start"), data.get("ban_end")
        if (ban_start or ban_end) and not (ban_start and ban_end):
            raise ValidationError("ban_start and ban_end must be provided together")
        if ban_start and ban_end:
            if _parse_or_raise(ban_start, "ban_start") > _parse_or_raise(ban_end, "ban_end"):
                raise ValidationError("ban_start must not be after ban_end")
    return {"decided_by": actor.user_id}


def _validate_registration_clear(actor, entity, data, lookup):
    athlete = _find_one(lookup, "athlete", "id", entity["data"].get("athlete_id"))
    if not athlete or athlete["status"] != "active":
        raise ValidationError("registration requires an active athlete")
    event = _find_one(lookup, "event", "id", entity["data"].get("event_id"))
    if not event or event["status"] != "open":
        raise ValidationError("registration requires an open event")
    ok, blockers = check_eligibility(lookup, athlete, event)
    if not ok:
        raise RegistrationBlocked(blockers)
    return {"cleared_by": actor.user_id}


CUSTOM_CREATE = {'athlete': _validate_athlete, 'sample': _validate_sample, 'case': _validate_case, 'team': _validate_team, 'personnel': _validate_personnel, 'sanction': _validate_sanction, 'event': _validate_event, 'registration': _validate_registration}
CUSTOM_TRANSITIONS = {('sample', 'report_adverse'): _validate_report_adverse, ('case', 'decide'): _validate_case_decision, ('case', 'resolve_appeal'): _validate_case_decision, ('registration', 'confirm'): _validate_registration_clear, ('registration', 'restore'): _validate_registration_clear}


class RuleEngine:
    ALIASES = {'athletes': 'athlete', 'samples': 'sample', 'cases': 'case', 'teams': 'team', 'staff': 'personnel', 'sanctions': 'sanction', 'events': 'event', 'registrations': 'registration'}
    INITIAL_STATUS = {'athlete': 'active', 'sample': 'scheduled', 'case': 'open', 'team': 'active', 'personnel': 'active', 'sanction': 'active', 'event': 'open', 'registration': 'pending'}
    TRANSITIONS = {'athlete': {'retire': (('active',), 'retired')}, 'sample': {'collect': (('scheduled',), 'collected'), 'seal': (('collected',), 'sealed'), 'ship': (('sealed',), 'in_transit'), 'receive': (('in_transit',), 'received'), 'analyze': (('received',), 'analyzed'), 'report_adverse': (('analyzed',), 'adverse'), 'clear': (('analyzed',), 'cleared')}, 'case': {'provisional_suspend': (('open',), 'suspended'), 'schedule_hearing': (('suspended',), 'hearing'), 'decide': (('hearing',), 'closed'), 'appeal': (('closed',), 'appeal'), 'resolve_appeal': (('appeal',), 'closed')}, 'team': {'disband': (('active',), 'disbanded')}, 'personnel': {'deactivate': (('active',), 'inactive')}, 'sanction': {'lift': (('active',), 'lifted')}, 'event': {'close': (('open',), 'closed')}, 'registration': {'confirm': (('pending',), 'confirmed'), 'reject': (('pending',), 'rejected'), 'record_result': (('confirmed',), 'confirmed'), 'invalidate': (('confirmed',), 'invalid'), 'restore': (('invalid',), 'confirmed'), 'withdraw': (('pending', 'confirmed'), 'withdrawn')}}
    CREATE_REQUIRED = {'athlete': ('name', 'discipline'), 'sample': ('athlete_id', 'sample_code', 'event'), 'case': ('athlete_id', 'sample_id', 'alleged_rule'), 'team': ('name',), 'personnel': ('name', 'role', 'team_id'), 'sanction': ('subject_kind', 'subject_id', 'start_date', 'end_date'), 'event': ('name', 'start_date'), 'registration': ('athlete_id', 'event_id')}
    ACTION_REQUIRED = {('sample', 'collect'): ('collected_at',), ('sample', 'seal'): ('seal_id',), ('sample', 'ship'): ('carrier',), ('sample', 'receive'): ('lab_id',), ('sample', 'analyze'): ('result',), ('sample', 'clear'): ('reason',), ('case', 'provisional_suspend'): ('reason',), ('case', 'schedule_hearing'): ('hearing_at',), ('case', 'decide'): ('decision',), ('case', 'appeal'): ('grounds',), ('case', 'resolve_appeal'): ('decision',), ('registration', 'record_result'): ('result',)}
    CREATE_ROLES = {'athlete': ('admin', 'panel'), 'sample': ('admin', 'inspector'), 'case': ('admin', 'panel'), 'team': ('admin', 'panel'), 'personnel': ('admin', 'panel'), 'sanction': ('admin', 'panel'), 'event': ('admin', 'panel'), 'registration': ('admin', 'panel')}
    ROLE_ACTIONS = {'retire': ('admin', 'panel'), 'collect': ('admin', 'inspector'), 'seal': ('admin', 'inspector'), 'ship': ('admin', 'inspector'), 'receive': ('admin', 'lab'), 'analyze': ('admin', 'lab'), 'report_adverse': ('admin', 'lab'), 'clear': ('admin', 'lab'), 'provisional_suspend': ('admin', 'panel'), 'schedule_hearing': ('admin', 'panel'), 'decide': ('admin', 'panel'), 'appeal': ('admin', 'panel'), 'resolve_appeal': ('admin', 'panel'), 'disband': ('admin', 'panel'), 'deactivate': ('admin', 'panel'), 'lift': ('admin', 'panel'), 'close': ('admin', 'panel'), 'confirm': ('admin', 'panel'), 'reject': ('admin', 'panel'), 'record_result': ('admin', 'panel'), 'invalidate': ('admin', 'panel'), 'restore': ('admin', 'panel'), 'withdraw': ('admin', 'panel')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
