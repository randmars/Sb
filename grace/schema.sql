-- Switchboard / Grace service foundation — SQLite logical model.
-- Implements the logical records of PRD v1.0 §12 ("Application data contracts").
-- Rules honoured here:
--   * PRD §12 §1: read/unread, hidden, mute, assignment, review, job, rule and
--     memory-validity states are INDEPENDENT columns. No overloaded "status".
--   * PRD §12: every stored value also carries `origin` ('mock' | 'real') and, when
--     origin='mock', a `mock_label` that starts with 'MOCK'. Nothing produced by a
--     mock adapter can be stored or rendered without that label (see labels.py).
--   * PRD §12: source timestamps (`source_time*`) are separate from ingestion
--     timestamps (`ingested_at`); all times are UTC.
--   * PRD §12/§8: no mandatory permanent body column. Message bodies and attachment
--     bytes live behind `retrieval_pointer` / `body_state`, fetched on demand.

CREATE TABLE IF NOT EXISTS schema_meta (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL
);

-- ---------------------------------------------------------------- sources ----

-- §12 SourceAccount and Capability (PRD §6 common source contract)
CREATE TABLE IF NOT EXISTS source_account (
  account_id        TEXT PRIMARY KEY,
  adapter           TEXT NOT NULL,
  adapter_version   TEXT NOT NULL,
  account_identity  TEXT NOT NULL,
  host_role         TEXT NOT NULL,              -- 'grace' | 'mini' (PRD §1)
  display_name      TEXT NOT NULL,
  enabled_operations TEXT NOT NULL DEFAULT '[]',-- JSON array of capability names
  health_state      TEXT NOT NULL,              -- connected|syncing|current|delayed|permission_denied|offline|error|partial_history
  health_detail     TEXT,
  permission_state  TEXT NOT NULL,              -- granted|denied|unknown|not_required
  last_success_at   TEXT,
  last_probe_at     TEXT,
  origin            TEXT NOT NULL,              -- mock|real
  mock_label        TEXT
);

CREATE TABLE IF NOT EXISTS capability (
  account_id      TEXT NOT NULL REFERENCES source_account(account_id),
  name            TEXT NOT NULL,                -- e.g. 'enumerate','retrieve','history_poll',
                                                -- 'materialize_attachment','prepare_draft','dispatch','reconcile'
  supported       INTEGER NOT NULL,             -- 1|0  (unsupported must be explicit, PRD §6)
  state           TEXT NOT NULL,                -- ok|unsupported|permission_denied|unverified
  limitation      TEXT,
  probe_method    TEXT,
  observed_at     TEXT,
  origin          TEXT NOT NULL,
  mock_label      TEXT,
  PRIMARY KEY (account_id, name)
);

-- §12 SourceConversation and MessageRef
CREATE TABLE IF NOT EXISTS source_conversation (
  conv_id             TEXT PRIMARY KEY,
  account_id          TEXT NOT NULL REFERENCES source_account(account_id),
  adapter             TEXT NOT NULL,            -- which adapter owns this conversation
  namespaced_id       TEXT NOT NULL UNIQUE,     -- '<adapter>:<account>:<provider id>' (PRD §4)
  provider_thread_id  TEXT,
  provider_chat_id    TEXT,
  audience_kind       TEXT NOT NULL,            -- direct|group|broadcast|unknown
  audience_json       TEXT NOT NULL DEFAULT '[]',
  provider_is_merged  INTEGER NOT NULL DEFAULT 0,
  revision            TEXT,
  availability        TEXT NOT NULL,            -- available|unavailable|partial
  availability_reason TEXT,
  retrieval_pointer   TEXT,
  minimal_metadata    TEXT NOT NULL DEFAULT '{}',
  source_time_first   TEXT,
  source_time_last    TEXT,
  ingested_at         TEXT NOT NULL,
  origin              TEXT NOT NULL,
  mock_label          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_conv_namespaced ON source_conversation(namespaced_id);

CREATE TABLE IF NOT EXISTS message_ref (
  msg_ref_id          TEXT PRIMARY KEY,
  conv_id             TEXT NOT NULL REFERENCES source_conversation(conv_id),
  account_id          TEXT NOT NULL,
  namespaced_id       TEXT NOT NULL UNIQUE,
  provider_message_id TEXT NOT NULL,
  reply_headers_json  TEXT NOT NULL DEFAULT '{}',
  sender_json         TEXT NOT NULL DEFAULT '{}',
  recipients_json     TEXT NOT NULL DEFAULT '[]',
  cc_json             TEXT NOT NULL DEFAULT '[]',
  bcc_json            TEXT NOT NULL DEFAULT '[]',
  audience_kind       TEXT NOT NULL DEFAULT 'direct',
  source_time         TEXT NOT NULL,            -- provider timestamp (UTC)
  ingested_at         TEXT NOT NULL,            -- our observation time (UTC)
  revision            TEXT,
  availability        TEXT NOT NULL,            -- available|unavailable|partial|deleted
  availability_reason TEXT,
  body_state          TEXT NOT NULL,            -- fetched|not_fetched|missing|empty|too_large
  retrieval_pointer   TEXT,                     -- how to fetch authoritative body; no body column
  minimal_metadata    TEXT NOT NULL DEFAULT '{}',
  -- independent states, PRD §12 first paragraph:
  read_state          TEXT NOT NULL DEFAULT 'unread',    -- unread|read|read_elsewhere
  hidden_state        TEXT NOT NULL DEFAULT 'visible',   -- visible|hidden
  mute_state          TEXT NOT NULL DEFAULT 'unmuted',   -- unmuted|muted
  deleted_at_source   INTEGER NOT NULL DEFAULT 0,
  origin              TEXT NOT NULL,
  mock_label          TEXT
);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON message_ref(conv_id, source_time);

-- ----------------------------------------------------------------- people ----

CREATE TABLE IF NOT EXISTS person (
  person_id     TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  notes         TEXT,
  user_override INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  mock_label    TEXT
);

-- §12 Person Group and IdentityLink — reversible, evidence-backed (R13, T02, T03)
CREATE TABLE IF NOT EXISTS identity_link (
  link_id            TEXT PRIMARY KEY,
  person_id          TEXT NOT NULL REFERENCES person(person_id),
  kind               TEXT NOT NULL,             -- email|phone|network_identity|native_contact
  normalized_value   TEXT NOT NULL,
  adapter            TEXT NOT NULL,
  account_id         TEXT,
  source_namespaced_id TEXT,
  evidence           TEXT NOT NULL,             -- e.g. 'exact_normalized_address_match'
  confidence         REAL NOT NULL,
  actor              TEXT NOT NULL,
  evidence_time      TEXT NOT NULL,
  user_override      INTEGER NOT NULL DEFAULT 0,
  state              TEXT NOT NULL,             -- proposed|confirmed|revoked
  reversible         INTEGER NOT NULL DEFAULT 1,
  superseded_by      TEXT,
  created_at         TEXT NOT NULL,
  revoked_at         TEXT,
  revoke_reason      TEXT,
  origin             TEXT NOT NULL,
  mock_label         TEXT
);
CREATE INDEX IF NOT EXISTS ix_identity_value ON identity_link(adapter, normalized_value);
CREATE INDEX IF NOT EXISTS ix_identity_person ON identity_link(person_id, state);

CREATE TABLE IF NOT EXISTS person_merge (
  merge_id            TEXT PRIMARY KEY,
  source_person_id    TEXT NOT NULL,
  target_person_id    TEXT NOT NULL,
  actor               TEXT NOT NULL,
  reason              TEXT,
  at                  TEXT NOT NULL,
  reversed_at         TEXT,
  reversal_actor      TEXT,
  evidence_json       TEXT NOT NULL DEFAULT '{}',
  origin              TEXT NOT NULL,
  mock_label          TEXT
);

CREATE TABLE IF NOT EXISTS grp (
  group_id                TEXT PRIMARY KEY,
  stable_source_identity  TEXT NOT NULL,
  account_id              TEXT NOT NULL,
  title                   TEXT NOT NULL,
  membership_version      TEXT NOT NULL,
  member_identity_json    TEXT NOT NULL DEFAULT '[]',
  audience_json           TEXT NOT NULL DEFAULT '[]',
  user_created            INTEGER NOT NULL DEFAULT 0,
  created_at              TEXT NOT NULL,
  origin                  TEXT NOT NULL,
  mock_label              TEXT
);

-- ------------------------------------------------------ workspace objects ----

-- §12 WorkspaceConversation: host for all related work; holds the independent
-- queue/review/assignment states and the current input revision.
CREATE TABLE IF NOT EXISTS workspace_conversation (
  ws_conv_id              TEXT PRIMARY KEY,
  association_kind        TEXT NOT NULL,        -- person|group
  association_id          TEXT NOT NULL,
  title                   TEXT NOT NULL,
  -- queue_state/review_state/needs_me_reason are the conversation's own aggregate of
  -- its work items (see job.queue_state). They are DERIVED from the jobs it hosts, so
  -- two jobs in one conversation cannot overwrite each other's state; a conversation
  -- with no jobs keeps its own state (e.g. an untriaged source message).
  queue_state             TEXT NOT NULL,        -- needs_me|working|idle  (application filter only, PRD §5)
  assignment_state        TEXT NOT NULL,        -- unassigned|assigned  (never archives the source)
  review_state            TEXT NOT NULL,        -- none|awaiting_review|awaiting_input|awaiting_approval|blocked
  current_input_revision  INTEGER NOT NULL DEFAULT 0,
  needs_me_reason         TEXT,                 -- question|draft|result|blocked|failure|uncertain_effect|null
  -- Archive is a state, not a delete (PRD §8: the source stays authoritative). An
  -- archived item is out of the default triage surfaces, remains stored, stays
  -- retrievable and is never the same thing as a deleted/forgotten one.
  archived_at             TEXT,                 -- set = archived; NULL = active
  archive_reason          TEXT,
  deleted_at              TEXT,                 -- application tombstone; no source row is removed
  deletion_reason         TEXT,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL,
  origin                  TEXT NOT NULL,
  mock_label              TEXT
);

CREATE TABLE IF NOT EXISTS workspace_source_link (
  ws_conv_id  TEXT NOT NULL REFERENCES workspace_conversation(ws_conv_id),
  conv_id     TEXT NOT NULL REFERENCES source_conversation(conv_id),
  relevance   TEXT NOT NULL DEFAULT 'primary',
  added_at    TEXT NOT NULL,
  PRIMARY KEY (ws_conv_id, conv_id)
);

-- ------------------------------------------------------------------ rules ----

-- §12 Rule and RuleRun; PRD §7 (R03, R14, T06, T07)
CREATE TABLE IF NOT EXISTS rule (
  rule_id             TEXT NOT NULL,
  version             INTEGER NOT NULL,
  owner               TEXT NOT NULL,
  rule_state          TEXT NOT NULL,            -- draft|enabled|disabled
  scope_json          TEXT NOT NULL,            -- sender identities, account/channel/conversation bounds
  conditions_json     TEXT NOT NULL,            -- structured conditions (label-aware domain match)
  explanation         TEXT NOT NULL,            -- readable form stored beside the executable form
  action              TEXT NOT NULL,            -- e.g. 'delegate_instruction','draft_reply','flag_for_review'
  agent               TEXT,
  instruction_template TEXT,
  authorization_ref   TEXT,                     -- never a standing send authority by default (R10)
  priority            INTEGER NOT NULL DEFAULT 100,
  effective_from      TEXT NOT NULL,
  effective_to        TEXT,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  superseded_by       INTEGER,
  origin              TEXT NOT NULL,
  mock_label          TEXT,
  PRIMARY KEY (rule_id, version)
);

CREATE TABLE IF NOT EXISTS rule_run (
  rule_run_id       TEXT PRIMARY KEY,
  rule_id           TEXT NOT NULL,
  rule_version      INTEGER NOT NULL,
  mode              TEXT NOT NULL,              -- future|backfill  (kept separate, PRD §7)
  run_state         TEXT NOT NULL,              -- preview|running|paused|cancelled|complete|failed
  preview_frozen_at TEXT,
  preview_bounds    TEXT NOT NULL DEFAULT '{}',
  matched_set_hash  TEXT,
  item_count        INTEGER NOT NULL DEFAULT 0,
  processed_count   INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  origin            TEXT NOT NULL,
  mock_label        TEXT
);

CREATE TABLE IF NOT EXISTS rule_run_item (
  evaluation_key  TEXT PRIMARY KEY,             -- §7 per-item evaluation key: repeat-safe
  rule_run_id     TEXT NOT NULL REFERENCES rule_run(rule_run_id),
  rule_id         TEXT NOT NULL,
  rule_version    INTEGER NOT NULL,
  msg_ref_id      TEXT NOT NULL,
  msg_revision    TEXT,
  outcome         TEXT NOT NULL,                -- pending|created_job|skipped_already_handled|excluded|failed
  outcome_detail  TEXT,
  job_id          TEXT,
  evaluated_at    TEXT NOT NULL,
  origin          TEXT NOT NULL,
  mock_label      TEXT
);

-- ------------------------------------------------------------------- jobs ----

-- §12 Job and Attempt; PRD §9 lifecycle.
CREATE TABLE IF NOT EXISTS job (
  job_id                 TEXT PRIMARY KEY,
  ws_conv_id             TEXT NOT NULL REFERENCES workspace_conversation(ws_conv_id),
  instruction_version    INTEGER NOT NULL,
  instruction            TEXT NOT NULL,
  agent                  TEXT NOT NULL,
  job_state              TEXT NOT NULL,         -- §9 states (see contracts.JobState)
  -- The queue/review/needs-me state is per JOB, derived from job_state by
  -- ledger.JOB_FILTER_STATES. Deriving it per conversation was the Gate 1 defect: a
  -- second job in the same conversation overwrote the first job's state and one job's
  -- draft disappeared from the surfaces (PRD §5 independent states, §12, T04/T05/T12).
  queue_state            TEXT NOT NULL DEFAULT 'working',   -- needs_me|working|idle
  review_state           TEXT NOT NULL DEFAULT 'none',      -- contracts.ReviewState
  needs_me_reason        TEXT,
  archived_at            TEXT,                  -- archived job: out of triage, still stored
  archive_reason         TEXT,
  deleted_at             TEXT,                  -- tombstone; the job row is never removed
  deletion_reason        TEXT,
  state_version          INTEGER NOT NULL DEFAULT 0,
  capability_plan        TEXT NOT NULL DEFAULT '{}',
  dependencies_json      TEXT NOT NULL DEFAULT '[]',
  source_context_revision TEXT,
  attempt_count          INTEGER NOT NULL DEFAULT 0,
  lease_owner            TEXT,
  lease_expires_at       TEXT,
  lease_heartbeat_at     TEXT,
  stall_count            INTEGER NOT NULL DEFAULT 0,
  last_stall_at          TEXT,
  checkpoint             TEXT NOT NULL DEFAULT '{}',
  cancellation_requested_at TEXT,
  superseded_by          TEXT,
  supersedes             TEXT,
  rule_id                TEXT,
  rule_version           INTEGER,
  origin                 TEXT NOT NULL,
  mock_label             TEXT,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_job_state ON job(job_state);
CREATE INDEX IF NOT EXISTS ix_job_ws ON job(ws_conv_id, created_at);
-- The triage lists read (queue_state, archived_at, deleted_at) per job.
CREATE INDEX IF NOT EXISTS ix_job_filter ON job(queue_state, ws_conv_id);

CREATE TABLE IF NOT EXISTS job_transition (
  transition_id   TEXT PRIMARY KEY,
  job_id          TEXT NOT NULL REFERENCES job(job_id),
  from_state      TEXT NOT NULL,
  to_state        TEXT NOT NULL,
  actor           TEXT NOT NULL,                -- owner|service|worker:<id>|adapter:<name>
  at              TEXT NOT NULL,
  reason          TEXT NOT NULL,
  expected_version INTEGER NOT NULL,
  resulting_version INTEGER NOT NULL,
  details_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_transition_job ON job_transition(job_id, at);

-- PRD §5: user input while a job runs is a NEW input version / follow-up turn,
-- never a mutation of the instruction already executing.
CREATE TABLE IF NOT EXISTS job_input (
  job_input_id  TEXT PRIMARY KEY,
  job_id        TEXT NOT NULL REFERENCES job(job_id),
  version       INTEGER NOT NULL,
  kind          TEXT NOT NULL,                  -- instruction|answer|follow_up|information
  content       TEXT NOT NULL,
  author        TEXT NOT NULL,
  at            TEXT NOT NULL,
  delivered_to_worker_at TEXT,
  superseded_by INTEGER,
  origin        TEXT NOT NULL,
  mock_label    TEXT,
  UNIQUE (job_id, version)
);

CREATE TABLE IF NOT EXISTS attempt (
  attempt_id    TEXT PRIMARY KEY,
  job_id        TEXT NOT NULL REFERENCES job(job_id),
  attempt_no    INTEGER NOT NULL,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  outcome_code  TEXT,
  outcome_detail TEXT,
  lease_owner   TEXT,
  checkpoint    TEXT NOT NULL DEFAULT '{}',
  host_run_ref  TEXT,
  origin        TEXT NOT NULL,
  mock_label    TEXT,
  UNIQUE (job_id, attempt_no)
);

-- §12 SessionBinding; PRD §9 Hermes boundary. Session/run identifiers are
-- application-persisted and never inferred from a "latest session".
CREATE TABLE IF NOT EXISTS session_binding (
  binding_id         TEXT PRIMARY KEY,
  ws_conv_id         TEXT NOT NULL,
  job_id             TEXT,
  hermes_profile     TEXT NOT NULL,
  session_key        TEXT NOT NULL,             -- stable memory scope
  session_id         TEXT,
  run_id             TEXT,
  successor_of       TEXT,
  parent_binding_id  TEXT,
  compression_lineage TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  origin             TEXT NOT NULL,
  mock_label         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_binding_key ON session_binding(hermes_profile, session_key);

-- ---------------------------------------------------------------- drafts -----

-- §12 Draft and AttachmentRef. Immutable versions; edits create a new version.
CREATE TABLE IF NOT EXISTS draft (
  draft_id              TEXT PRIMARY KEY,
  ws_conv_id            TEXT NOT NULL,
  job_id                TEXT,
  version               INTEGER NOT NULL,
  parent_version        INTEGER,
  immutable             INTEGER NOT NULL DEFAULT 1,
  account_id            TEXT NOT NULL,
  channel               TEXT NOT NULL,
  destination_conv_id   TEXT NOT NULL,
  destination_provider_id TEXT,
  destination_resolved  INTEGER NOT NULL DEFAULT 1,
  mode                  TEXT NOT NULL,          -- reply|reply_all|new
  subject               TEXT,
  body                  TEXT NOT NULL,
  recipient_snapshot    TEXT NOT NULL DEFAULT '[]',  -- frozen at creation
  audience_hash         TEXT NOT NULL,
  membership_version    TEXT,
  body_hash             TEXT NOT NULL,
  attachment_hash       TEXT NOT NULL,
  content_hash          TEXT NOT NULL,
  purpose               TEXT NOT NULL,
  sender_identity       TEXT NOT NULL,
  source_refs_json      TEXT NOT NULL DEFAULT '[]',
  author                TEXT NOT NULL,
  superseded_by         TEXT,
  invalidated_at        TEXT,
  invalidation_reason   TEXT,
  blocking_limitations  TEXT,                   -- e.g. unavailable attachment required for review
  created_at            TEXT NOT NULL,
  origin                TEXT NOT NULL,
  mock_label            TEXT,
  UNIQUE (ws_conv_id, job_id, version)
);
-- A draft's version is unique within its (conversation, job) scope. SQLite treats every
-- NULL as distinct, so the table constraint above does NOT apply when job_id IS NULL and
-- two drafts with the same idempotency key (explicit version, no job) could both exist.
-- This expression index closes that hole in the SCHEMA, not only in the code path that
-- happens to have a job (PRD §12 immutable append-only versions; §10/T13 approve an
-- exact draft_version).
CREATE UNIQUE INDEX IF NOT EXISTS ux_draft_version_scope
  ON draft(ws_conv_id, COALESCE(job_id, ''), version);

CREATE TABLE IF NOT EXISTS attachment_ref (
  attachment_ref_id  TEXT PRIMARY KEY,
  draft_id           TEXT,
  job_id             TEXT,
  msg_ref_id         TEXT,
  namespaced_id      TEXT NOT NULL,
  filename           TEXT NOT NULL,
  media_type         TEXT NOT NULL,
  size_bytes         INTEGER,
  content_hash       TEXT,
  download_state     TEXT NOT NULL,             -- downloaded|not_downloaded|unavailable|quarantined
  availability       TEXT NOT NULL,             -- available|unavailable
  limitation         TEXT,
  quarantine_state   TEXT NOT NULL DEFAULT 'none',
  created_at         TEXT NOT NULL,
  origin             TEXT NOT NULL,
  mock_label         TEXT
);

-- ------------------------------------------------- approvals and effects ------

-- §12 Approval and EffectOperation; PRD §10 (R10, R15, T13, T14, T15, T22).
CREATE TABLE IF NOT EXISTS approval (
  approval_id            TEXT PRIMARY KEY,
  owner                  TEXT NOT NULL,
  operation_id           TEXT NOT NULL UNIQUE,
  draft_id               TEXT NOT NULL,
  draft_version          INTEGER NOT NULL,
  bound_account_id       TEXT NOT NULL,
  bound_sender_identity  TEXT NOT NULL,
  bound_destination      TEXT NOT NULL,
  bound_mode             TEXT NOT NULL,
  bound_recipient_snapshot TEXT NOT NULL,
  bound_audience_hash    TEXT NOT NULL,
  bound_body_hash        TEXT NOT NULL,
  bound_attachment_hash  TEXT NOT NULL,
  bound_content_hash     TEXT NOT NULL,
  bound_purpose          TEXT NOT NULL,
  scope_json             TEXT NOT NULL DEFAULT '{}',  -- recorded authorization scope
  approval_state         TEXT NOT NULL,         -- granted|consumed|invalidated|expired|revoked
  issued_at              TEXT NOT NULL,
  expires_at             TEXT NOT NULL,
  consumed_at            TEXT,
  invalidated_at         TEXT,
  invalidation_reason    TEXT,
  superseded_by          TEXT,
  origin                 TEXT NOT NULL,
  mock_label             TEXT
);
CREATE INDEX IF NOT EXISTS ix_approval_draft ON approval(draft_id, draft_version);

CREATE TABLE IF NOT EXISTS effect_operation (
  effect_id             TEXT PRIMARY KEY,
  operation_id          TEXT NOT NULL UNIQUE,
  job_id                TEXT,
  ws_conv_id            TEXT,
  approval_id           TEXT,
  adapter               TEXT NOT NULL,
  account_id            TEXT NOT NULL,
  target_conv_id        TEXT NOT NULL,
  target_provider_id    TEXT,
  purpose               TEXT NOT NULL,
  idempotency_key       TEXT NOT NULL UNIQUE,   -- source idempotency key where available
  request_hash          TEXT NOT NULL,
  effect_state          TEXT NOT NULL,          -- contracts.EffectState
  attempt_count         INTEGER NOT NULL DEFAULT 0,
  provider_message_id   TEXT,
  source_operation_id   TEXT,
  actual_routed_destination TEXT,
  provider_status       TEXT,
  requires_reconciliation INTEGER NOT NULL DEFAULT 0,
  last_error_category   TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  origin                TEXT NOT NULL,
  mock_label            TEXT
);

CREATE TABLE IF NOT EXISTS effect_attempt (
  effect_attempt_id     TEXT PRIMARY KEY,
  effect_id             TEXT NOT NULL REFERENCES effect_operation(effect_id),
  attempt_no            INTEGER NOT NULL,
  phase                 TEXT NOT NULL,          -- pre_submission|post_submission
  submitted             INTEGER NOT NULL DEFAULT 0,
  started_at            TEXT NOT NULL,
  ended_at              TEXT,
  outcome_code          TEXT NOT NULL,
  error_category        TEXT,
  provider_message_id   TEXT,
  actual_routed_destination TEXT,
  retry_allowed         INTEGER NOT NULL DEFAULT 0,
  authorization_ref     TEXT,
  note                  TEXT,
  origin                TEXT NOT NULL,
  mock_label            TEXT,
  UNIQUE (effect_id, attempt_no)
);

-- §12 Result and Receipt
CREATE TABLE IF NOT EXISTS result (
  result_id     TEXT PRIMARY KEY,
  job_id        TEXT,
  ws_conv_id    TEXT NOT NULL,
  version       INTEGER NOT NULL,
  kind          TEXT NOT NULL,                  -- draft|question|answer|classification|evidence|failure
  summary       TEXT NOT NULL,
  detail_json   TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  created_at    TEXT NOT NULL,
  origin        TEXT NOT NULL,
  mock_label    TEXT,
  UNIQUE (ws_conv_id, job_id, version)
);

CREATE TABLE IF NOT EXISTS receipt (
  receipt_id            TEXT PRIMARY KEY,
  effect_id             TEXT NOT NULL REFERENCES effect_operation(effect_id),
  result_id             TEXT,
  effect_state          TEXT NOT NULL,
  provider_message_id   TEXT,
  source_operation_id   TEXT,
  actual_routed_destination TEXT,
  verification_level    TEXT NOT NULL,          -- provider_accepted|provider_pending|confirmed_sent|unverified|none
  delivery_state        TEXT NOT NULL DEFAULT 'unknown',  -- unknown|not_applicable|delivered|failed
  read_state            TEXT NOT NULL DEFAULT 'unknown',
  verified_against_real_source INTEGER NOT NULL DEFAULT 0,
  limitations           TEXT,
  evidence_json         TEXT NOT NULL DEFAULT '[]',
  observed_at           TEXT NOT NULL,
  origin                TEXT NOT NULL,
  mock_label            TEXT
);

-- ----------------------------------------------------------------- memory -----

-- §12 MemoryFact and Relation; PRD §8 (three separate concepts, provenance, supersession)
CREATE TABLE IF NOT EXISTS memory_fact (
  fact_id        TEXT PRIMARY KEY,
  content        TEXT NOT NULL,
  category       TEXT NOT NULL,                 -- personal|relationship|commitment|preference|domain
  provenance_json TEXT NOT NULL DEFAULT '{}',
  sensitivity    TEXT NOT NULL DEFAULT 'normal',
  confidence     REAL NOT NULL DEFAULT 0.5,
  validity_state TEXT NOT NULL,                 -- active|superseded|forgotten|stale_source|unavailable_source
  observed_at    TEXT NOT NULL,
  valid_from     TEXT,
  valid_to       TEXT,
  superseded_by  TEXT,
  source_refs_json TEXT NOT NULL DEFAULT '[]',
  created_at     TEXT NOT NULL,
  origin         TEXT NOT NULL,
  mock_label     TEXT
);

CREATE TABLE IF NOT EXISTS relation (
  relation_id   TEXT PRIMARY KEY,
  from_person_id TEXT NOT NULL,
  to_person_id  TEXT NOT NULL,
  kind          TEXT NOT NULL,
  confidence    REAL NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  valid_from    TEXT,
  valid_to      TEXT,
  superseded_by TEXT,
  origin        TEXT NOT NULL,
  mock_label    TEXT
);

-- ------------------------------------------- ingestion, sync and audit -------

-- §12 SyncCheckpoint and AuditEvent; §6 coverage map.
CREATE TABLE IF NOT EXISTS sync_checkpoint (
  checkpoint_id     TEXT PRIMARY KEY,
  account_id        TEXT NOT NULL,
  scope             TEXT NOT NULL,              -- mailbox|chat|contacts
  scope_ref         TEXT NOT NULL,
  cursor_value      TEXT,                       -- opaque source cursor, committed only with its updates
  overlap_state     TEXT,                       -- e.g. 'overlap_msgs=50'
  oldest_observed_time TEXT,
  last_success_at   TEXT,
  last_attempt_at   TEXT,
  pagination_state  TEXT,                       -- idle|paginating|exhausted
  coverage_state    TEXT NOT NULL,              -- verified_scan|partial_history|unknown
  coverage_json     TEXT NOT NULL DEFAULT '{}', -- observed counts, missing bodies/attachments, gaps
  gap_reason        TEXT,
  committed_updates INTEGER NOT NULL DEFAULT 0,
  updated_at        TEXT NOT NULL,
  origin            TEXT NOT NULL,
  mock_label        TEXT,
  UNIQUE (account_id, scope, scope_ref)
);

-- §12: durable deduplication keys; at-least-once ingest, idempotent projection (R08, T08)
CREATE TABLE IF NOT EXISTS event_dedup (
  dedup_key        TEXT PRIMARY KEY,
  account_id       TEXT NOT NULL,
  event_kind       TEXT NOT NULL,
  trigger_hash     TEXT NOT NULL,
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  seen_count       INTEGER NOT NULL DEFAULT 1,
  projection_state TEXT NOT NULL,               -- applied|deferred
  projected_at     TEXT
);

-- Transactional outbox: the intended job/effect is persisted in the same
-- transaction as the domain change; publication happens afterwards (PRD §12).
CREATE TABLE IF NOT EXISTS outbox (
  outbox_id     TEXT PRIMARY KEY,
  dedup_key     TEXT NOT NULL UNIQUE,
  topic         TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  outbox_state  TEXT NOT NULL,                  -- pending|published|failed
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  created_at    TEXT NOT NULL,
  committed_at  TEXT NOT NULL,
  published_at  TEXT,
  origin        TEXT NOT NULL,
  mock_label    TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_state ON outbox(outbox_state, created_at);

CREATE TABLE IF NOT EXISTS audit_event (
  audit_id       TEXT PRIMARY KEY,
  at             TEXT NOT NULL,
  actor          TEXT NOT NULL,
  operation      TEXT NOT NULL,
  entity_kind    TEXT NOT NULL,
  entity_id      TEXT NOT NULL,
  version_before INTEGER,
  version_after  INTEGER,
  operation_id   TEXT,
  reason         TEXT,
  trace_ref      TEXT,
  details_json   TEXT NOT NULL DEFAULT '{}',    -- must never contain secrets or message bodies
  secret_redacted INTEGER NOT NULL DEFAULT 1,
  origin         TEXT NOT NULL,
  mock_label     TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_event(at);
