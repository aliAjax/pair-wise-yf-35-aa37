from uuid import uuid4

from . import eligibility
from .audit import AuditTrail
from .domain import ConflictError, DomainError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        self._after_create(actor, entity)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._after_transition(actor, updated, action)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # 报名资格核对

    def check_registration_eligibility(self, athlete_id=None, event_id=None):
        if not event_id:
            raise ValidationError("event_id is required")
        event = self.get(event_id)
        if event["kind"] != "event":
            raise ValidationError("event_id must reference an event")
        if athlete_id:
            athlete = self.get(athlete_id)
            if athlete["kind"] != "athlete":
                raise ValidationError("athlete_id must reference an athlete")
            ok, blockers = eligibility.check_eligibility(self._lookup, athlete, event)
            return {
                "event_id": event["id"],
                "athlete_id": athlete["id"],
                "ok": ok,
                "blockers": blockers,
            }
        registrable = []
        blocked = []
        for athlete in self.repository.list_entities(kind="athlete", status="active"):
            ok, blockers = eligibility.check_eligibility(self._lookup, athlete, event)
            item = {"athlete_id": athlete["id"], "name": athlete["data"].get("name", "")}
            if ok:
                registrable.append(item)
            else:
                item["blockers"] = blockers
                blocked.append(item)
        return {"event_id": event["id"], "registrable": registrable, "blocked": blocked}

    def refresh_registrations(self, actor, event_id=None, registration_id=None):
        if actor.role not in ("admin", "panel"):
            raise PermissionDenied("role %s is not allowed here" % actor.role)
        if registration_id:
            registration = self.get(registration_id)
            if registration["kind"] != "registration":
                raise ValidationError("registration_id must reference a registration")
            registrations = [registration]
        else:
            registrations = self.repository.list_entities(kind="registration")
            if event_id:
                registrations = [
                    item for item in registrations if item["data"].get("event_id") == event_id
                ]
        updated = []
        for registration in registrations:
            status = registration["status"]
            if status not in ("confirmed", "invalid"):
                continue
            ok, _ = self._check_registration(registration)
            try:
                if status == "confirmed" and not ok:
                    updated.append(self.transition(actor, registration["id"], "invalidate", {}))
                elif status == "invalid" and ok:
                    updated.append(self.transition(actor, registration["id"], "restore", {}))
            except DomainError:
                continue
        return updated

    # 处罚联动钩子

    def _after_create(self, actor, entity):
        if entity["kind"] == "sanction":
            self._apply_sanction(actor, entity)

    def _after_transition(self, actor, entity, action):
        if entity["kind"] == "case" and action in ("decide", "resolve_appeal"):
            data = entity["data"]
            if data.get("decision") == "sanction" and data.get("ban_start") and data.get("ban_end"):
                self.create(
                    actor,
                    "sanction",
                    {
                        "subject_kind": "athlete",
                        "subject_id": data.get("athlete_id"),
                        "case_id": entity["id"],
                        "start_date": data["ban_start"],
                        "end_date": data["ban_end"],
                        "reason": "case %s sanction" % entity["id"],
                    },
                )
        if entity["kind"] == "sanction" and action == "lift":
            self._release_sanction(actor, entity)

    def _apply_sanction(self, actor, sanction):
        for athlete in self._affected_athletes(sanction):
            for registration in self.repository.find_entities("registration", "athlete_id", athlete["id"]):
                if registration["status"] != "confirmed":
                    continue
                event = self.repository.get_entity(registration["data"].get("event_id"))
                if not event:
                    continue
                if not eligibility.sanction_covers_event(sanction, event):
                    continue
                try:
                    self.transition(
                        actor,
                        registration["id"],
                        "invalidate",
                        {"invalidated_by": sanction["id"]},
                    )
                except DomainError:
                    continue

    def _release_sanction(self, actor, sanction):
        for athlete in self._affected_athletes(sanction):
            for registration in self.repository.find_entities("registration", "athlete_id", athlete["id"]):
                if registration["status"] != "invalid":
                    continue
                try:
                    self.transition(actor, registration["id"], "restore", {})
                except DomainError:
                    continue

    def _affected_athletes(self, sanction):
        data = sanction["data"]
        subject_kind, subject_id = data.get("subject_kind"), data.get("subject_id")
        if subject_kind == "athlete":
            athlete = self.repository.get_entity(subject_id)
            return [athlete] if athlete else []
        if subject_kind == "team":
            return self.repository.find_entities("athlete", "team_id", subject_id)
        if subject_kind == "personnel":
            person = self.repository.get_entity(subject_id)
            if not person:
                return []
            return self.repository.find_entities(
                "athlete", "team_id", person["data"].get("team_id")
            )
        return []

    def _check_registration(self, registration):
        athlete = self.repository.get_entity(registration["data"].get("athlete_id"))
        event = self.repository.get_entity(registration["data"].get("event_id"))
        if not athlete or not event:
            raise ValidationError("registration references a missing athlete or event")
        return eligibility.check_eligibility(self._lookup, athlete, event)
