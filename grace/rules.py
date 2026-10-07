"""Rule model, preview and historical application (PRD §7, R03/R14, T06/T07).

Kept deliberately small and deterministic:

* A rule is versioned; editing produces a new version and a new preview rather
  than mutating a run that is already applying an older version.
* Matching uses deterministic filters only (exact sender identity, account,
  label-aware domain, simple subject/content condition). A model is never needed
  to decide whether a rule matches.
* Every evaluation writes a per-item evaluation key, so replaying the same event
  or re-running the same preview cannot create the same work twice (T06, T08).
* "Save this instruction for this sender", "apply to existing matches" and
  "grant standing send authority" are three different things: a saved rule never
  carries send authority (R10, draft-only launch default).
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from . import contracts as C
from .contracts import JobState, OpResult
from .ledger import Ledger
from .store import Store


def label_aware_domain_match(address: str, domain: str) -> bool:
    """Match a domain on label boundaries: a@sub.example.com matches example.com,
    a@notexample.com does not (PRD §7: domain matching must be label-aware)."""
    if "@" not in address:
        return False
    candidate = address.rsplit("@", 1)[1].lower().strip()
    domain = domain.lower().strip()
    return candidate == domain or candidate.endswith("." + domain)


def _normalise_address(raw: str) -> str:
    """Addressed are compared exactly after lower-casing; plus-addressing is preserved
    (PRD §4: do not remove email plus-addressing)."""
    return raw.strip().lower()


# The owner's own identities. Messages the owner sent never trigger rules, so a rule
# cannot create a feedback loop from Randy's own outgoing mail (PRD §7).
OWNER_IDENTITIES = frozenset({
    "randy@example-mail.test", "@randy:example-matrix.test",
})


class Rules:
    def __init__(self, store: Store, ledger: Ledger):
        self.store = store
        self.ledger = ledger

    # ------------------------------------------------------------------ save --
    def save(self, *, rule_id: str | None, owner: str, scope: dict, conditions: dict,
             explanation: str, action: str, agent: str | None = None,
             instruction_template: str | None = None, priority: int = 100,
             enabled: bool = True, actor: str = "owner",
             origin: str = C.REAL, mock_label: str | None = None) -> OpResult:
        conds = [c for c in conditions.get("all", []) if c]
        if not conds:
            return OpResult(C.INVALID,
                            "a rule must have at least one condition; match-all and empty rules "
                            "are rejected (PRD §7)")
        rule_id = rule_id or C.new_id("rule")
        prev = self.store.one("SELECT * FROM rule WHERE rule_id = ? ORDER BY version DESC LIMIT 1",
                              (rule_id,))
        version = (prev["version"] + 1) if prev else 1
        now = C.now()
        with self.store.tx():
            if prev:
                self.store.update_row("rule", {"superseded_by": version},
                                      "rule_id = ? AND version = ?", (rule_id, prev["version"]))
            self.store.insert_row("rule", {
                "rule_id": rule_id, "version": version, "owner": owner,
                "rule_state": "enabled" if enabled else "disabled",
                "scope_json": C.canonical_json(scope), "conditions_json": C.canonical_json(conditions),
                "explanation": explanation, "action": action, "agent": agent,
                "instruction_template": instruction_template,
                "authorization_ref": None,   # saving a rule never grants send authority
                "priority": priority, "effective_from": now, "effective_to": None,
                "created_at": now, "updated_at": now, "superseded_by": None,
                "origin": origin, "mock_label": mock_label,
            })
            self.store.audit(actor=actor, operation="save_rule", entity_kind="rule",
                            entity_id=rule_id, reason="rule saved as a version (no send authority)",
                            version_before=version - 1, version_after=version, within_tx=True,
                            details={"action": action, "agent": agent, "enabled": enabled},
                            origin=origin, mock_label=mock_label)
        return OpResult(C.OK, f"rule v{version} saved; applies to future arrivals only until a "
                              f"separate preview and apply is confirmed",
                        data={"rule_id": rule_id, "version": version, "enabled": enabled,
                              "authorization_ref": None,
                              "historical_application": "not started"})

    def disable(self, rule_id: str, version: int, *, reason: str, actor: str = "owner",
                cancel_pending: bool = False) -> OpResult:
        row = self.store.one("SELECT * FROM rule WHERE rule_id = ? AND version = ?",
                             (rule_id, version))
        if row is None:
            return OpResult(C.NOT_FOUND, f"unknown rule {rule_id} v{version}")
        with self.store.tx():
            self.store.update_row("rule", {"rule_state": "disabled", "updated_at": C.now()},
                                  "rule_id = ? AND version = ?", (rule_id, version))
            self.store.audit(actor=actor, operation="disable_rule", entity_kind="rule",
                            entity_id=rule_id, reason=reason, within_tx=True,
                            origin=row["origin"], mock_label=row["mock_label"])
        cancelled = []
        if cancel_pending:
            for run in self.store.all("SELECT rule_run_id FROM rule_run WHERE rule_id = ? "
                                      "AND rule_version = ? AND run_state IN ('running','preview')",
                                      (rule_id, version)):
                self.cancel_run(run["rule_run_id"], reason="rule disabled", actor=actor)
                cancelled.append(run["rule_run_id"])
        return OpResult(C.OK, "rule disabled: new matches stop immediately",
                        data={"rule_id": rule_id, "version": version,
                              "cancelled_runs": cancelled,
                              "note": "completed effects are not undone by disabling a rule"})

    # --------------------------------------------------------------- matching --
    def matches(self, rule: dict, msg: dict, identity_values: Iterable[str]) -> tuple[bool, list[str]]:
        conds = json.loads(rule["conditions_json"]).get("all", [])
        scope = json.loads(rule["scope_json"])
        reasons: list[str] = []
        for cond in conds:
            kind = cond.get("kind")
            if kind == "sender_identity":
                value = _normalise_address(cond["value"])
                if _normalise_address(msg.get("sender_value", "")) != value:
                    return False, []
                reasons.append(f"exact sender identity {value}")
            elif kind == "sender_domain":
                if not label_aware_domain_match(msg.get("sender_value", ""), cond["domain"]):
                    return False, []
                reasons.append(f"label-aware domain {cond['domain']}")
            elif kind == "account":
                if msg.get("account_id") != cond["account_id"]:
                    return False, []
                reasons.append("selected account")
            elif kind == "audience_kind":
                if msg.get("audience_kind") != cond["audience_kind"]:
                    return False, []
                reasons.append(f"audience {cond['audience_kind']}")
            elif kind == "subject_contains":
                subject = str(msg.get("subject") or "").lower()
                if cond["value"].lower() not in subject:
                    return False, []
                reasons.append(f"subject contains {cond['value']!r}")
            elif kind == "body_contains":
                reasons.append(f"body condition on {cond['value']!r} (unverified without a fetch)")
            else:
                return False, []
        for key in ("account_ids", "audience_kinds"):
            allowed = scope.get(key)
            if allowed and msg.get(key[:-1]) not in allowed:
                return False, []
        return True, reasons

    def _msg_for_matching(self, msg_row: dict) -> dict:
        sender = json.loads(msg_row.get("sender_json") or "{}")
        conv = self.store.one("SELECT * FROM source_conversation WHERE conv_id = ?",
                              (msg_row["conv_id"],)) or {}
        meta = json.loads(msg_row.get("minimal_metadata") or "{}")
        return {
            "msg_ref_id": msg_row["msg_ref_id"],
            "msg_revision": msg_row.get("revision"),
            "sender_value": sender.get("address") or sender.get("network_identity") or "",
            "account_id": msg_row["account_id"],
            "audience_kind": msg_row.get("audience_kind") or conv.get("audience_kind"),
            "conv_id": msg_row["conv_id"],
            "subject": meta.get("subject"),
            "source_time": msg_row["source_time"],
        }

    def candidates(self, msg_row: dict) -> tuple[Optional[dict], list[dict], list[str]]:
        """Return (winning rule, all matches, conflict reasons). Most specific wins."""
        msg = self._msg_for_matching(msg_row)
        if _normalise_address(msg["sender_value"]) in OWNER_IDENTITIES:
            # Prevent feedback loops from the owner's own sent messages (PRD §7).
            return None, [], []
        matches = []
        for rule in self.store.all("SELECT * FROM rule WHERE rule_state = 'enabled'"):
            ok, reasons = self.matches(rule, msg, [])
            if ok:
                matches.append({"rule": rule, "reasons": reasons,
                                "specificity": self._specificity(rule)})
        if not matches:
            return None, [], []
        matches.sort(key=lambda m: (-m["specificity"], m["rule"]["priority"], m["rule"]["rule_id"]))
        winner = matches[0]
        conflicts = []
        for other in matches[1:]:
            if other["specificity"] == winner["specificity"] and \
                    other["rule"]["action"] != winner["rule"]["action"]:
                conflicts.append(
                    f"rule {other['rule']['rule_id']} v{other['rule']['version']} "
                    f"({other['rule']['action']}) conflicts with "
                    f"{winner['rule']['rule_id']} v{winner['rule']['version']} "
                    f"({winner['rule']['action']}) at the same specificity")
        if conflicts:
            # Irreconcilable actions pause for review rather than picking silently.
            return None, matches, conflicts
        return winner, matches, []

    @staticmethod
    def _specificity(rule: dict) -> int:
        conds = json.loads(rule["conditions_json"]).get("all", [])
        return sum(1 for c in conds if c.get("kind") in
                   ("sender_identity", "sender_domain", "account", "subject_contains"))

    # ---------------------------------------------------------------- preview --
    def evaluation_key(self, rule: dict, msg_row: dict) -> str:
        return "rk-" + C.sha256_hex({
            "rule_id": rule["rule_id"], "rule_version": rule["version"],
            "msg_ref_id": msg_row["msg_ref_id"], "msg_revision": msg_row.get("revision"),
        })[:40]

    def preview(self, rule_id: str, version: int, *, bounds: dict,
                actor: str = "owner") -> OpResult:
        rule = self.store.one("SELECT * FROM rule WHERE rule_id = ? AND version = ?",
                              (rule_id, version))
        if rule is None:
            return OpResult(C.NOT_FOUND, f"unknown rule {rule_id} v{version}")
        msgs = self.store.all("SELECT * FROM message_ref WHERE deleted_at_source = 0 "
                              "ORDER BY source_time DESC")
        if bounds.get("date_from"):
            msgs = [m for m in msgs if m["source_time"] >= bounds["date_from"]]
        if bounds.get("date_to"):
            msgs = [m for m in msgs if m["source_time"] <= bounds["date_to"]]
        if bounds.get("limit"):
            msgs = msgs[: int(bounds["limit"])]
        matched, excluded, conflicts = [], [], []
        for m in msgs:
            msg = self._msg_for_matching(m)
            ok, reasons = self.matches(rule, msg, [])
            if not ok:
                continue
            already = self.store.one(
                "SELECT job_id, outcome FROM rule_run_item WHERE evaluation_key = ?",
                (self.evaluation_key(rule, m),))
            entry = {"msg_ref_id": m["msg_ref_id"], "source_time": m["source_time"],
                     "reasons": reasons,
                     "already_handled": bool(already),
                     "prior_job_id": already["job_id"] if already else None}
            if already:
                excluded.append({**entry, "exclusion": "already handled by this rule version"})
            else:
                matched.append(entry)
        else_conflicts = self._rule_conflicts(rule, matched)
        conflicts.extend(else_conflicts)
        run_id = C.new_id("rrun")
        now = C.now()
        matched_hash = C.sha256_hex(matched)
        with self.store.tx():
            self.store.insert_row("rule_run", {
                "rule_run_id": run_id, "rule_id": rule_id, "rule_version": version,
                "mode": "backfill", "run_state": "preview", "preview_frozen_at": now,
                "preview_bounds": C.canonical_json(bounds), "matched_set_hash": matched_hash,
                "item_count": len(matched), "processed_count": 0, "created_at": now,
                "updated_at": now, "origin": rule["origin"], "mock_label": rule["mock_label"],
            })
            for entry in matched:
                self.store.insert_row("rule_run_item", {
                    "evaluation_key": self.evaluation_key(rule, self.store.one(
                        "SELECT * FROM message_ref WHERE msg_ref_id = ?", (entry["msg_ref_id"],))),
                    "rule_run_id": run_id, "rule_id": rule_id, "rule_version": version,
                    "msg_ref_id": entry["msg_ref_id"],
                    "msg_revision": self.store.one(
                        "SELECT revision FROM message_ref WHERE msg_ref_id = ?",
                        (entry["msg_ref_id"],))["revision"],
                    "outcome": "pending", "outcome_detail": "awaiting owner confirmation to apply",
                    "job_id": None, "evaluated_at": now,
                    "origin": rule["origin"], "mock_label": rule["mock_label"],
                })
            self.store.audit(actor=actor, operation="rule_preview", entity_kind="rule_run",
                            entity_id=run_id, reason="historical match set frozen; not applied",
                            details={"matched": len(matched), "excluded": len(excluded),
                                     "conflicts": len(conflicts)},
                            origin=rule["origin"], mock_label=rule["mock_label"], within_tx=True)
        return OpResult(C.OK, "preview frozen; applying to history is a separate decision",
                        data={"rule_run_id": run_id, "rule_id": rule_id, "version": version,
                              "mode": "backfill", "matched": matched, "excluded": excluded,
                              "conflicts": conflicts, "matched_set_hash": matched_hash,
                              "frozen_at": now,
                              "note": "future-arrival rules and this historical run are separate "
                                      "choices (PRD §7)"})

    def _rule_conflicts(self, rule: dict, entries: list[dict]) -> list[str]:
        conflicts = []
        for entry in entries:
            msg = self.store.one("SELECT * FROM message_ref WHERE msg_ref_id = ?",
                                 (entry["msg_ref_id"],))
            winner, _all, clash = self.candidates(msg)
            if clash:
                conflicts.extend(clash)
            elif winner and winner["rule"]["rule_id"] != rule["rule_id"] and \
                    winner["rule"]["action"] != rule["action"] and \
                    winner["specificity"] >= self._specificity(rule):
                conflicts.append(
                    f"message {entry['msg_ref_id']} would be taken by more specific rule "
                    f"{winner['rule']['rule_id']} v{winner['rule']['version']} "
                    f"({winner['rule']['action']})")
        return conflicts

    # ------------------------------------------------------------------ apply --
    def apply(self, rule_run_id: str, *, limit: int | None = None, actor: str = "owner",
              stop_after: int | None = None, origin: str | None = None,
              mock_label: str | None = None) -> OpResult:
        """Apply a frozen preview. Repeat-safe: completed items are never repeated."""
        run = self.store.one("SELECT * FROM rule_run WHERE rule_run_id = ?", (rule_run_id,))
        if run is None:
            return OpResult(C.NOT_FOUND, f"unknown rule run {rule_run_id}")
        if run["run_state"] == "cancelled":
            return OpResult(C.INVALID, "run was cancelled; completed effects are not repeated")
        rule = self.store.one("SELECT * FROM rule WHERE rule_id = ? AND version = ?",
                              (run["rule_id"], run["rule_version"]))
        items = self.store.all(
            "SELECT * FROM rule_run_item WHERE rule_run_id = ? AND outcome = 'pending' "
            "ORDER BY evaluated_at", (rule_run_id,))
        if limit:
            items = items[:limit]
        created, skipped, failed = [], [], []
        with self.store.tx():
            self.store.update_row("rule_run", {"run_state": "running", "updated_at": C.now()},
                                  "rule_run_id = ?", (rule_run_id,))
        for i, item in enumerate(items, start=1):
            if stop_after is not None and len(created) >= stop_after:
                break
            ws = self.store.one(
                "SELECT * FROM workspace_source_link WHERE conv_id = ?",
                (self.store.one("SELECT conv_id FROM message_ref WHERE msg_ref_id = ?",
                                (item["msg_ref_id"],))["conv_id"],))
            ws_conv_id = ws["ws_conv_id"] if ws else None
            if ws_conv_id is None:
                failed.append({"evaluation_key": item["evaluation_key"],
                               "reason": "no workspace conversation for this source conversation"})
                with self.store.tx():
                    self.store.update_row("rule_run_item", {
                        "outcome": "failed", "outcome_detail": "no host conversation",
                        "evaluated_at": C.now()}, "evaluation_key = ?", (item["evaluation_key"],))
                continue
            existing = self.store.one("SELECT job_id FROM rule_run_item WHERE evaluation_key = ?",
                                      (item["evaluation_key"],))
            if existing and existing["job_id"]:
                skipped.append(item["evaluation_key"])
                continue
            instruction = (rule["instruction_template"] or
                           f"Rule {rule['rule_id']} v{rule['version']}: {rule['action']}")
            res = self.ledger.create_job(
                ws_conv_id=ws_conv_id, instruction=instruction, agent=rule["agent"] or "rule_agent",
                rule_id=rule["rule_id"], rule_version=rule["version"],
                dedup_key=item["evaluation_key"], actor=actor,
                origin=origin or rule["origin"], mock_label=mock_label if origin else rule["mock_label"])
            if res.ok and res.data and res.data.get("job_id"):
                with self.store.tx():
                    self.store.update_row("rule_run_item", {
                        "outcome": "created_job",
                        "outcome_detail": "job created from frozen preview",
                        "job_id": res.data["job_id"], "evaluated_at": C.now(),
                    }, "evaluation_key = ?", (item["evaluation_key"],))
                created.append({"evaluation_key": item["evaluation_key"],
                                "job_id": res.data["job_id"]})
            elif res.code == C.IDEMPOTENT_REPLAY:
                skipped.append(item["evaluation_key"])
            else:
                failed.append({"evaluation_key": item["evaluation_key"], "reason": res.detail})
                with self.store.tx():
                    self.store.update_row("rule_run_item", {
                        "outcome": "failed", "outcome_detail": res.detail,
                        "evaluated_at": C.now()}, "evaluation_key = ?", (item["evaluation_key"],))
        remaining = int(self.store.scalar(
            "SELECT COUNT(*) FROM rule_run_item WHERE rule_run_id = ? AND outcome = 'pending'",
            (rule_run_id,)) or 0)
        state = "running" if remaining else "complete"
        with self.store.tx():
            self.store.update_row("rule_run", {
                "run_state": state, "processed_count": len(created) + len(skipped) + len(failed),
                "updated_at": C.now()}, "rule_run_id = ?", (rule_run_id,))
        return OpResult(C.OK, f"applied {len(created)} item(s); {remaining} still pending",
                        data={"rule_run_id": rule_run_id, "created": created, "skipped": skipped,
                              "failed": failed, "remaining_pending": remaining,
                              "run_state": state})

    def cancel_run(self, rule_run_id: str, *, reason: str, actor: str = "owner") -> OpResult:
        with self.store.tx():
            self.store.update_row("rule_run", {"run_state": "cancelled", "updated_at": C.now()},
                                  "rule_run_id = ?", (rule_run_id,))
            self.store.audit(actor=actor, operation="cancel_rule_run", entity_kind="rule_run",
                            entity_id=rule_run_id, reason=reason, within_tx=True)
        return OpResult(C.OK, "historical run cancelled; completed items are not repeated",
                        data={"rule_run_id": rule_run_id,
                              "note": "cancelling does not claim to undo completed effects"})

    # ----------------------------------------------------- future arrivals -----
    def evaluate_arrival(self, msg_row: dict, *, actor: str = "ingest") -> OpResult:
        """Future-arrival path. Same job-creation path as manual assignment (PRD §7)."""
        winner, matches, conflicts = self.candidates(msg_row)
        if conflicts:
            return OpResult(C.CONFLICT, "conflicting rules; pausing for review",
                            data={"conflicts": conflicts,
                                  "matches": [m["rule"]["rule_id"] for m in matches]})
        if winner is None:
            return OpResult(C.ALREADY_IN_STATE, "no enabled rule matches this message")
        rule = winner["rule"]
        key = self.evaluation_key(rule, msg_row)
        link = self.store.one("SELECT ws_conv_id FROM workspace_source_link WHERE conv_id = ?",
                              (msg_row["conv_id"],))
        if link is None:
            return OpResult(C.INVALID, "no host workspace conversation for this message")
        instruction = (rule["instruction_template"] or
                       f"Rule {rule['rule_id']} v{rule['version']}: {rule['action']}")
        result = self.ledger.create_job(
            ws_conv_id=link["ws_conv_id"], instruction=instruction,
            agent=rule["agent"] or "rule_agent", rule_id=rule["rule_id"],
            rule_version=rule["version"], dedup_key=key, actor=actor,
            origin=rule["origin"], mock_label=rule["mock_label"])
        if result.code == C.OK and result.data and result.data.get("job_id"):
            now = C.now()
            with self.store.tx():
                self.store.insert_row("rule_run_item", {
                    "evaluation_key": key, "rule_run_id": self._future_run(rule),
                    "rule_id": rule["rule_id"], "rule_version": rule["version"],
                    "msg_ref_id": msg_row["msg_ref_id"], "msg_revision": msg_row.get("revision"),
                    "outcome": "created_job", "outcome_detail": "future arrival matched the rule",
                    "job_id": result.data["job_id"], "evaluated_at": now,
                    "origin": rule["origin"], "mock_label": rule["mock_label"],
                })
        return result

    def _future_run(self, rule: dict) -> str:
        """The 'future arrivals' run for a rule version, created on first use."""
        existing = self.store.one(
            "SELECT * FROM rule_run WHERE rule_id = ? AND rule_version = ? AND mode = 'future'",
            (rule["rule_id"], rule["version"]))
        if existing:
            return existing["rule_run_id"]
        run_id = C.new_id("rrun")
        now = C.now()
        with self.store.tx():
            self.store.insert_row("rule_run", {
                "rule_run_id": run_id, "rule_id": rule["rule_id"], "rule_version": rule["version"],
                "mode": "future", "run_state": "running", "preview_frozen_at": now,
                "preview_bounds": C.canonical_json({"scope": "future arrivals"}),
                "matched_set_hash": None, "item_count": 0, "processed_count": 0,
                "created_at": now, "updated_at": now,
                "origin": rule["origin"], "mock_label": rule["mock_label"],
            })
        return run_id
