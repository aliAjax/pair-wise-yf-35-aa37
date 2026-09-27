from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    Actor,
    ConflictError,
    EligibilityBlocked,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .eligibility import EligibilityEngine
from .rules import RuleEngine, _parse_date

SYSTEM_ACTOR = Actor("system", "admin")
CONFIRM_ROLES = ("admin", "panel")
RECONCILE_ROLES = ("admin", "panel")


class DomainService:
    def __init__(self, repository, rules=None, clock=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        if clock:
            self.eligibility = EligibilityEngine(
                self.list, self.get, clock=clock
            )
        else:
            self.eligibility = EligibilityEngine(self.list, self.get)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    @staticmethod
    def _require_fields(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

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
        if updated["kind"] == "case" and action in ("decide", "resolve_appeal"):
            self._sync_sanctions_from_case(updated, actor)
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

    # ---- registration eligibility workflow -------------------------------

    def confirm_registration(self, actor, data, idempotency_key=None):
        """Dedicated entry point: run the eligibility gate, then record."""
        self._ensure_role(actor, CONFIRM_ROLES)
        payload = dict(data or {})
        self._require_fields(payload, ("athlete_id", "event", "event_date"))
        event_date = _parse_date(payload.get("event_date"), "event_date")
        athlete = self.repository.get_entity(payload["athlete_id"])
        if not athlete or self.rules.normalize_kind(athlete["kind"]) != "athlete":
            raise ValidationError("registration requires an existing athlete")
        if athlete["status"] != "active":
            raise ValidationError("athlete must be active to register")

        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity

        # Merge sanctions of athlete + team + team staff; any hit blocks confirm.
        items = self.eligibility.blocks_for_athlete(athlete, event_date)
        if items:
            raise EligibilityBlocked(
                "registration blocked by %d effective sanction(s)" % len(items),
                [item.to_dict() for item in items],
            )

        event = payload["event"]
        for reg in self.list("registration"):
            if (
                reg["data"].get("athlete_id") == athlete["id"]
                and reg["data"].get("event") == event
            ):
                raise ConflictError(
                    "athlete already registered for event: %s" % event
                )

        record = {
            "athlete_id": athlete["id"],
            "athlete_name": athlete["data"].get("name", athlete["id"]),
            "team_id": athlete["data"].get("team_id"),
            "event": event,
            "event_date": str(event_date),
            "result": payload.get("result"),
        }
        entity_id = str(payload.get("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        entity = self.repository.create_entity(
            entity_id, "registration", "confirmed", record, actor.user_id
        )
        self.audit.record(
            entity_id, actor, "confirm_registration", None, "confirmed",
            {"event": event, "event_date": str(event_date)},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _sync_sanctions_from_case(self, case, actor):
        """Create/update/revoke the sanction linked to a decided case."""
        data = case["data"]
        subject_kind = data.get("subject_kind") or (
            "athlete" if data.get("athlete_id") else None
        )
        subject_id = data.get("subject_id") or data.get("athlete_id")
        if not subject_kind or not subject_id:
            return
        subject = self.repository.get_entity(subject_id)
        subject_name = subject["data"].get("name", subject_id) if subject else subject_id
        linked = None
        for sanction in self.list("sanction"):
            if sanction["data"].get("case_id") == case["id"]:
                linked = sanction
                break

        decision = data.get("decision")
        if decision == "sanction":
            record = {
                "case_id": case["id"],
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "subject_name": subject_name,
                "start_date": data.get("start_date"),
                "end_date": data.get("end_date"),
                "alleged_rule": data.get("alleged_rule", ""),
            }
            if linked is None:
                sanction = self.repository.create_entity(
                    str(uuid4()), "sanction", "active", record, actor.user_id
                )
                self.audit.record(
                    sanction["id"], actor, "sanction_issued", None, "active",
                    {"case_id": case["id"], "subject": [subject_kind, subject_id]},
                )
            elif linked["status"] != "active":
                merged = dict(linked["data"])
                merged.update(
                    {key: record[key] for key in (
                        "start_date", "end_date", "alleged_rule", "subject_name"
                    )}
                )
                sanction = self.repository.update_entity(
                    linked["id"], linked["version"], "active", merged
                )
                self.audit.record(
                    sanction["id"], actor, "sanction_reinstated", "revoked", "active",
                    {"case_id": case["id"]},
                )
        elif decision == "no_sanction" and linked is not None and linked["status"] == "active":
            sanction = self.repository.update_entity(
                linked["id"], linked["version"], "revoked", dict(linked["data"])
            )
            self.audit.record(
                sanction["id"], actor, "sanction_revoked", "active", "revoked",
                {"case_id": case["id"]},
            )

        # A changed sanction can invalidate or restore registrations.
        self.reconcile_registrations(actor=actor)

    def reconcile_registrations(self, actor=None, as_of=None, event=None):
        """Sweep registrations: block by sanctions covering the event date,
        restore once every covering sanction has expired. Original results
        are never cleared."""
        actor = actor or SYSTEM_ACTOR
        self._ensure_role(actor, RECONCILE_ROLES)
        athletes = {a["id"]: a for a in self.list("athlete") if a["status"] == "active"}
        invalidated, restored = [], []
        for reg in self.list("registration"):
            if event is not None and reg["data"].get("event") != event:
                continue
            athlete = athletes.get(reg["data"].get("athlete_id"))
            event_date = _parse_date(reg["data"].get("event_date"), "event_date")
            items = (
                self.eligibility.blocks_for_athlete(athlete, event_date, as_of)
                if athlete else []
            )
            effective_status = "invalidated" if items else "confirmed"
            if reg["status"] == effective_status:
                continue
            merged = dict(reg["data"])
            if effective_status == "invalidated":
                merged["invalidated_by"] = [item.to_dict() for item in items]
                self.repository.update_entity(
                    reg["id"], reg["version"], "invalidated", merged
                )
                self.audit.record(
                    reg["id"], actor, "registration_invalidated",
                    "confirmed", "invalidated",
                    {"blocks": [item.to_dict() for item in items]},
                )
                invalidated.append(reg["id"])
            else:
                merged.pop("invalidated_by", None)
                self.repository.update_entity(
                    reg["id"], reg["version"], "confirmed", merged
                )
                self.audit.record(
                    reg["id"], actor, "registration_restored",
                    "invalidated", "confirmed", {"result_kept": bool(merged.get("result"))},
                )
                restored.append(reg["id"])
        return {"invalidated": invalidated, "restored": restored}

    def registrations_view(self, event=None, as_of=None):
        """Registration records with their live effective status; the stored
        result field is always retained."""
        athletes = {a["id"]: a for a in self.list("athlete") if a["status"] == "active"}
        items = []
        for reg in self.list("registration"):
            if event is not None and reg["data"].get("event") != event:
                continue
            entry = dict(reg["data"])
            entry.update(
                {
                    "id": reg["id"],
                    "status": reg["status"],
                    "effective_status": reg["status"],
                    "version": reg["version"],
                }
            )
            athlete = athletes.get(reg["data"].get("athlete_id"))
            if athlete:
                event_date = _parse_date(reg["data"].get("event_date"), "event_date")
                blocks = self.eligibility.blocks_for_athlete(athlete, event_date, as_of)
                entry["effective_status"] = "invalidated" if blocks else "confirmed"
                entry["live_blocks"] = [item.to_dict() for item in blocks]
            items.append(entry)
        return {"items": items}

    def eligibility_view(self, event, event_date, as_of=None):
        """Read model separating athletes who may register from blocked ones."""
        parsed_date = _parse_date(event_date, "event_date")
        registrations = [
            reg
            for reg in self.list("registration")
            if reg["data"].get("event") == event
        ]
        return self.eligibility.roster_view(
            event, parsed_date, as_of=as_of, registrations=registrations
        )

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)
