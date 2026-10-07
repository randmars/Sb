/* Switchboard review client — vanilla ES2020, no build step, no CDN.
 *
 * Rules this file keeps (PRD §5 "Mobile and accessibility", §10, §13, R11, R15):
 *  - Everything from a source conversation is UNTRUSTED TASK DATA (PRD §13). It is
 *    rendered through escapeHtml()/textContent only; never as HTML, never as a link
 *    target, never as an instruction to this client.
 *  - A mutation is never retried automatically. Reads retry a couple of times;
 *    writes need a human pressing the button again, because a send that may already
 *    have reached a source must not be repeated by a client.
 *  - Nothing is shown as a confirmed send unless the effect ledger says
 *    send_state.is_green_sent, which requires a receipt verified against a real
 *    source. On this deployment that is always false, and the UI says so.
 *  - Draft text and instruction text are kept in local storage, keyed by object id,
 *    so a navigation or a temporary disconnection cannot lose them.
 */
'use strict';

const TOKEN_HEADER = 'X-Switchboard-Token';
const LS_PREFIX = 'switchboard:';
const state = {
  token: sessionStorage.getItem('switchboard:token') || null,
  route: { name: 'needs', arg: null, query: '' },
  overview: null,
  lastFocus: null,
  offline: !navigator.onLine,
};

/* ------------------------------------------------------------------ auth --- */

function captureToken() {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get('token');
  if (fromUrl) {
    state.token = fromUrl;
    sessionStorage.setItem('switchboard:token', fromUrl);
    params.delete('token');
    const clean = window.location.pathname + (params.toString() ? '?' + params : '') + window.location.hash;
    window.history.replaceState({}, document.title, clean);
  }
}

/* -------------------------------------------------------------- utilities --- */

function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtTime(iso) {
  if (!iso) return 'unknown time';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString();
}

function pill(text, cls) {
  return `<span class="pill ${cls}">${escapeHtml(text)}</span>`;
}

function statePill(queueState) {
  return pill(queueState, 'state-' + escapeHtml(queueState));
}

function sendStatePill(send) {
  const cls = send.is_green_sent ? 'state-ok'
    : (send.state === 'outcome_unknown' || send.state === 'failed') ? 'state-danger'
      : 'state-warn';
  return pill(send.label, cls);
}

function kv(pairs) {
  const rows = pairs.filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`).join('');
  return rows ? `<dl class="kv">${rows}</dl>` : '';
}

function listOr(value) {
  if (Array.isArray(value)) return value.length ? value.join(', ') : '(none recorded)';
  if (value === null || value === undefined || value === '') return '(none recorded)';
  return String(value);
}

function mockLine(label) {
  return label ? `<span class="pill state-mock">${escapeHtml(label)}</span>` : '';
}

/* ------------------------------------------------------------------ api ---- */

class OfflineError extends Error {}

function authExpired() {
  sessionStorage.removeItem('switchboard:token');
  state.token = null;
  document.getElementById('health-banner').hidden = false;
  document.getElementById('health-banner').textContent =
    'Not authorised. Open the URL printed by `grace serve --print-url` (it carries ?token=…) ' +
    'to get a session on this device. Nothing is shown without it.';
}

async function api(path, options) {
  const opts = options || {};
  const headers = {};
  if (state.token) headers[TOKEN_HEADER] = state.token;
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, {
      method: opts.method || 'GET',
      headers: headers,
      credentials: 'same-origin',
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    });
  } catch (err) {
    throw new OfflineError('Could not reach Grace: ' + err.message);
  }
  if (response.status === 401) { authExpired(); throw new Error('unauthorised'); }
  let payload = null;
  try { payload = await response.json(); } catch (err) { payload = null; }
  if (payload === null) throw new Error('Grace returned a non-JSON response (' + response.status + ')');
  if (!response.ok) throw new Error(payload.detail || payload.error || ('HTTP ' + response.status));
  return payload;
}

/* reads are safe to repeat; a bounded retry is honest and cheap */
async function apiRead(path, attempts) {
  const tries = attempts || 2;
  let last = null;
  for (let i = 0; i < tries; i++) {
    try { return await api(path); } catch (err) {
      last = err;
      if (err instanceof OfflineError && i < tries - 1) {
        await new Promise((r) => setTimeout(r, 600));
        continue;
      }
      throw err;
    }
  }
  throw last;
}

/* --------------------------------------------------------------- storage --- */

function draftKey(kind, id) { return LS_PREFIX + kind + ':' + id; }

function savedText(kind, id) {
  try { return localStorage.getItem(draftKey(kind, id)); } catch (e) { return null; }
}

function saveText(kind, id, value) {
  try {
    if (value && value.trim()) localStorage.setItem(draftKey(kind, id), value);
    else localStorage.removeItem(draftKey(kind, id));
  } catch (e) { /* private mode: persistence is best effort, never fatal */ }
}

function clearText(kind, id) {
  try { localStorage.removeItem(draftKey(kind, id)); } catch (e) { /* ignore */ }
}

/* ---------------------------------------------------------------- toast ---- */

let toastTimer = null;

function toast(message, kind, retry) {
  const el = document.getElementById('toast');
  el.hidden = false;
  el.className = 'toast' + (kind === 'error' ? ' error' : '');
  el.innerHTML = `<span>${escapeHtml(message)}</span>`;
  const actions = document.createElement('div');
  actions.className = 'toast-actions';
  if (retry) {
    const again = document.createElement('button');
    again.type = 'button';
    again.className = 'primary';
    again.textContent = 'Retry now';
    again.addEventListener('click', () => { hideToast(); retry(); });
    actions.appendChild(again);
  }
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'quiet';
  close.textContent = 'Dismiss';
  close.addEventListener('click', hideToast);
  actions.appendChild(close);
  el.appendChild(actions);
  if (toastTimer) clearTimeout(toastTimer);
  if (kind !== 'error') toastTimer = setTimeout(hideToast, 6000);
}

function hideToast() { document.getElementById('toast').hidden = true; }

/* --------------------------------------------------------------- routing --- */

function parseHash() {
  const raw = window.location.hash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const parts = pathPart.split('/').filter(Boolean);
  const query = new URLSearchParams(queryPart || '');
  const from = query.get('from') || null;
  const q = query.get('q') || '';
  const stateFilter = query.get('state') || 'all';
  if (parts[0] === 'c' && parts[1]) {
    return { name: 'conversation', arg: decodeURIComponent(parts[1]), query: q, from: from, stateFilter: stateFilter };
  }
  if (parts[0] === 'working') return { name: 'working', arg: null, query: q, stateFilter: stateFilter };
  if (parts[0] === 'all') return { name: 'all', arg: null, query: q, stateFilter: stateFilter };
  if (parts[0] === 'rules') return { name: 'rules', arg: null, query: q, stateFilter: stateFilter };
  return { name: 'needs', arg: null, query: q, stateFilter: stateFilter };
}

function markTabs() {
  document.querySelectorAll('.tab').forEach((tab) => {
    const target = tab.getAttribute('data-tab');
    const active = (state.route.name === target)
      || (state.route.name === 'conversation' && target === state.route.from);
    if (active) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  });
}

function navigate(hash) {
  if (window.location.hash === hash) render();
  else window.location.hash = hash;
}

/* ------------------------------------------------------------- rendering --- */

function setCounts(counts) {
  if (!counts) return;
  document.getElementById('count-needs').textContent = counts.needs_me;
  document.getElementById('count-working').textContent = counts.working;
  document.getElementById('count-all').textContent = counts.all;
}

function renderBanner(overview) {
  const banner = document.getElementById('mock-banner');
  if (overview.mocked) {
    banner.hidden = false;
    banner.innerHTML = escapeHtml(overview.mock_label)
      + ' — every source value on this screen is a labelled mock.'
      + `<span class="disclaimer">${escapeHtml(overview.mock_disclaimer || '')}</span>`
      + '<span class="disclaimer">verified_against_real_source = false. '
      + escapeHtml(overview.labelling_note || '') + '</span>';
  } else {
    banner.hidden = true;
  }
  const health = document.getElementById('health-banner');
  const disconnected = (overview.data && overview.data.disconnected_states) || [];
  if (disconnected.length) {
    health.hidden = false;
    health.innerHTML = escapeHtml(
      disconnected.length + ' source state(s) are not healthy. ' +
      'The Rules & health surface lists the reason and the smallest next action for each.');
  } else {
    health.hidden = true;
  }
  const status = document.getElementById('service-status');
  const schema = (overview.data && overview.data.schema) || {};
  status.textContent = (state.offline ? 'Offline — showing what Grace last sent. ' : '')
    + 'Grace ledger v' + listOr(schema.schema_version) + ' · ' + listOr(schema.db_path);
}

function itemCard(item, opts) {
  const options = opts || {};
  const reason = item.reason ? `<p class="item-reason">${escapeHtml(item.reason)}</p>` : '';
  const job = item.job
    ? `<p class="item-meta">${escapeHtml(item.job.agent || 'unassigned')} · `
      + `${escapeHtml(item.job.job_state)} · age ${escapeHtml(item.job.age || 'unknown')} · `
      + `${escapeHtml(item.job.progress || '')}</p>`
    : '<p class="item-meta">No job attached</p>';
  const accounts = (item.source_accounts || [])
    .map((a) => `${escapeHtml(a.display_name || a.adapter)} (${escapeHtml(a.account_identity)})`)
    .join(' · ');
  const audience = (item.source_links || [])
    .map((l) => `${escapeHtml(l.adapter)} ${escapeHtml(l.audience_kind)}: ${escapeHtml(l.audience_text)}`)
    .join(' | ');
  const send = item.send_state && item.send_state.state !== 'none'
    ? `<p class="item-meta">Outbound: ${sendStatePill(item.send_state)}</p>` : '';
  const envelope = options.showEnvelope !== false && item.mock_label
    ? `<p class="item-meta">${mockLine(item.mock_label)}</p>` : '';
  return `
    <article class="item">
      <a class="item-title" href="#/c/${encodeURIComponent(item.ws_conv_id)}?from=${escapeHtml(options.fromTab || '')}">
        ${escapeHtml(item.title)}
      </a>
      <p class="item-meta">
        ${statePill(item.queue_state)}
        ${item.review_state !== 'none' ? pill(item.review_state, 'state-warn') : ''}
        ${item.assignment_state === 'assigned' ? pill('assigned', 'state-working') : ''}
        <span>age ${escapeHtml(item.age || 'unknown')}</span>
      </p>
      ${reason}
      ${job}
      <p class="item-meta">Sending account: ${accounts || '(none recorded)'}</p>
      <p class="item-meta">Audience: ${audience || '(none recorded)'}</p>
      ${send}
      ${envelope}
    </article>`;
}

function emptyState(payload, view) {
  const states = (payload.data.disconnected_states || []);
  const labels = {
    needs_me: 'Nothing needs you right now',
    working: 'No work is in flight',
    all: 'No conversations are stored',
  };
  let html = `<div class="empty"><p><strong>${escapeHtml(labels[view] || 'Nothing to show')}</strong></p>
    <p>This is a real empty list from the durable ledger, not a failed read.</p>`;
  if (states.length) {
    html += '<p>Some sources are not healthy, so a list here can be incomplete rather than empty. '
      + 'Each one is listed with its reason and the smallest next action:</p><ul>';
    states.forEach((s) => {
      html += `<li class="wrap-anywhere">${escapeHtml(s.display_name || s.adapter)}
        ${s.capability ? '(' + escapeHtml(s.capability) + ')' : ''}:
        <strong>${escapeHtml(s.state)}</strong> — ${escapeHtml(s.reason || '')}
        <br><em>Next: ${escapeHtml(s.next_action || 'record a capability probe')}</em></li>`;
    });
    html += '</ul>';
  }
  return html + '</div>';
}

function sourceHealthBlock(payload) {
  const states = payload.data.disconnected_states || [];
  const sources = payload.data.sources || [];
  if (!states.length) {
    return `<div class="callout ok">All ${sources.length} source accounts report a healthy, current
      state in this deployment. That says nothing about Mail, Beeper, Contacts or Hermes on
      Randy's Mac — those have never been contacted.</div>`;
  }
  return states.map((s) => `
    <article class="empty state-${escapeHtml(s.state)}">
      <p><strong>${escapeHtml(s.state)}</strong> — ${escapeHtml(s.display_name || s.adapter)}
      ${s.capability ? `<span class="pill">${escapeHtml(s.capability)}</span>` : ''}
      ${mockLine(s.mock_label)}</p>
      <p>${escapeHtml(s.reason || '')}</p>
      <p><em>Smallest next action: ${escapeHtml(s.next_action || 'record a capability probe on the Mini worker')}</em></p>
      ${s.probe_method ? `<p class="tiny">Probe method: ${escapeHtml(s.probe_method)}</p>` : ''}
      ${s.freshness ? `<p class="tiny">Last success observed ${escapeHtml(s.freshness)} ago.</p>` : ''}
    </article>`).join('');
}

/* ------------------------------------------------------------------ views -- */

async function renderNeeds(payload, tab) {
  const items = payload.data.items || [];
  return `
    <section class="card">
      <h2>Needs me</h2>
      <p class="muted">${escapeHtml(payload.data.note)}</p>
      <div class="row">
        ${pill('needs me ' + payload.data.counts.needs_me, 'state-needs_me')}
        ${pill('working ' + payload.data.counts.working, 'state-working')}
        ${pill('all ' + payload.data.counts.all, 'state-idle')}
        ${payload.data.counts.stalled_jobs ? pill('stalled ' + payload.data.counts.stalled_jobs, 'state-danger') : ''}
      </div>
      <p class="tiny">Counts are independent: an assignment moves the item out of this queue by an
      application filter, and that never changes the Working or All totals for any other reason.</p>
    </section>
    ${items.length ? items.map((i) => itemCard(i, { fromTab: tab })).join('')
      : emptyState(payload, tab)}`;
}

async function renderWorking(payload, tab) {
  const items = payload.data.items || [];
  return `
    <section class="card">
      <h2>Working</h2>
      <p class="muted">${escapeHtml(payload.data.note)}</p>
      <div class="row">
        ${pill('working ' + payload.data.counts.working, 'state-working')}
        ${pill('needs me ' + payload.data.counts.needs_me, 'state-needs_me')}
        ${pill('all ' + payload.data.counts.all, 'state-idle')}
      </div>
    </section>
    ${items.length ? items.map((i) => itemCard(i, { fromTab: tab })).join('')
      : emptyState(payload, tab)}`;
}

async function renderAll(payload, tab, query) {
  const items = payload.data.items || [];
  const states = ['all', 'needs_me', 'working', 'idle'].map((s) =>
    `<option value="${s}" ${payload.data.state_filter === s ? 'selected' : ''}>${s === 'all' ? 'Every state' : s}</option>`
  ).join('');
  return `
    <section class="card">
      <h2>All conversations</h2>
      <p class="muted">${escapeHtml(payload.data.note)}</p>
      <form id="search-form" class="stack" role="search">
        <label for="q">Search people, groups, source threads and drafts</label>
        <input id="q" name="q" type="search" value="${escapeHtml(query || '')}"
               placeholder="name, address, account, network identity, subject, phrase">
        <div class="row">
          <label for="state-filter" class="vh">Queue state filter</label>
          <select id="state-filter" name="state">${states}</select>
          <button class="primary" type="submit">Search</button>
          <button type="button" class="quiet" id="clear-search">Clear</button>
        </div>
      </form>
      <p class="tiny">Showing ${payload.data.shown} of ${payload.data.counts.all}. Completed and
      application-hidden conversations stay here: hidden is not deleted (PRD §6).</p>
    </section>
    ${items.length ? items.map((i) => itemCard(i, { fromTab: tab })).join('')
      : emptyState(payload, tab)}`;
}

/* ------------------------------------------------------- rules and health -- */

async function renderRulesHealth(payload) {
  const d = payload.data;
  const cobertura = (d.coverage || []).map((c) => `
    <tr>
      <td>${escapeHtml(c.account_id)}</td>
      <td>${escapeHtml(c.scope)}${c.scope_ref ? ':' + escapeHtml(c.scope_ref) : ''}</td>
      <td>${escapeHtml(c.coverage_state)}</td>
      <td>${escapeHtml(c.oldest_observed_time || 'unknown')}</td>
      <td>${c.gap_reason ? escapeHtml(c.gap_reason) : '—'}</td>
      <td>${escapeHtml(c.last_success_at || 'never')}</td>
    </tr>`).join('') || '<tr><td colspan="6">No checkpoints recorded.</td></tr>';

  const sources = (d.sources || []).map((s) => {
    const caps = (s.capabilities || []).map((c) => `
      <tr>
        <td>${escapeHtml(c.name)}</td>
        <td>${c.supported ? pill('supported', 'state-ok') : pill('unsupported', 'state-warn')}</td>
        <td>${escapeHtml(c.state)}</td>
        <td>${escapeHtml(c.limitation || '—')}</td>
      </tr>`).join('');
    return `
      <article class="card">
        <h3>${escapeHtml(s.display_name)} ${mockLine(s.mock_label)}</h3>
        ${kv([
      ['adapter', escapeHtml(s.adapter) + ' ' + escapeHtml(s.adapter_version)],
      ['account', escapeHtml(s.account_identity)],
      ['host role', escapeHtml(s.host_role)],
      ['health', escapeHtml(s.health_state) + (s.health_detail ? ' — ' + escapeHtml(s.health_detail) : '')],
      ['permission', escapeHtml(s.permission_state)],
      ['last success', escapeHtml(s.last_success_at || 'never')],
      ['last probe', escapeHtml(s.last_probe_at || 'never')],
      ['freshness', escapeHtml(s.freshness || 'unknown')],
    ])}
        <p class="tiny">${escapeHtml(s.disclosure || '')}</p>
        <h4>Capabilities</h4>
        <table>
          <caption class="vh">Declared capabilities for ${escapeHtml(s.display_name)}</caption>
          <thead><tr><th scope="col">Capability</th><th scope="col">Declared</th><th scope="col">State</th><th scope="col">Limitation</th></tr></thead>
          <tbody>${caps}</tbody>
        </table>
      </article>`;
  }).join('');

  const rules = (d.rules || []).map((r) => {
    const runs = (r.runs || []).map((run) => `
      <div>
        <p class="muted">Run ${escapeHtml(run.rule_run_id)} · ${escapeHtml(run.mode)} ·
        ${escapeHtml(run.run_state)} · frozen ${escapeHtml(run.preview_frozen_at || 'not frozen')} ·
        ${run.item_count} matched, ${run.processed_count} processed</p>
        <p class="tiny">Bounds: ${escapeHtml(JSON.stringify(run.preview_bounds))} ·
        Outcomes: ${escapeHtml(JSON.stringify(run.outcomes))} ·
        matched_set_hash ${escapeHtml(run.matched_set_hash || 'none')}</p>
        <table>
          <caption class="vh">Per-item outcomes for run ${escapeHtml(run.rule_run_id)}</caption>
          <thead><tr><th scope="col">Evaluation key</th><th scope="col">Item</th><th scope="col">Outcome</th><th scope="col">Job</th></tr></thead>
          <tbody>${(run.items || []).map((it) => `<tr>
            <td class="tiny">${escapeHtml(it.evaluation_key)}</td>
            <td class="tiny">${escapeHtml(it.msg_ref_id)} v${escapeHtml(it.msg_revision || '?')}</td>
            <td>${escapeHtml(it.outcome)}</td>
            <td class="tiny">${escapeHtml(it.job_id || '—')}</td></tr>`).join('')}</tbody>
        </table>
      </div>`).join('') || '<p class="muted">No historical run has been previewed or applied.</p>';
    return `
      <article class="card">
        <h3>${escapeHtml(r.rule_id)} v${r.version}
          ${pill(r.rule_state, r.rule_state === 'enabled' ? 'state-ok' : 'state-idle')}
          ${mockLine(r.mock_label)}</h3>
        <p>${escapeHtml(r.explanation)}</p>
        ${kv([
      ['action', escapeHtml(r.action)],
      ['agent', escapeHtml(r.agent || 'none')],
      ['instruction', escapeHtml(r.instruction_template || '—')],
      ['conditions', escapeHtml(JSON.stringify(r.conditions))],
      ['scope', escapeHtml(JSON.stringify(r.scope))],
      ['authorization', escapeHtml(r.authorization)],
      ['priority', escapeHtml(r.priority)],
      ['versions', escapeHtml(r.versions.join(', '))],
      ['send authority', 'none — a rule can request drafting work only'],
    ])}
        <h4>Historical application</h4>
        ${runs}
      </article>`;
  }).join('') || `<div class="empty"><p><strong>No rules are saved.</strong></p>
        <p>A rule is versioned and inspectable, and is never a standing send authority (R10, R14).</p></div>`;

  return `
    <section class="card">
      <h2>Rules and source health</h2>
      <p class="muted">${escapeHtml(d.rule_note)}</p>
      <p class="tiny">${escapeHtml(d.manifest_note)}</p>
      <p class="tiny">${escapeHtml(d.freshness_note)}</p>
    </section>
    <h2>Rules</h2>
    ${rules}
    <h2>Sources, capabilities and freshness</h2>
    ${sourceHealthBlock({ data: d })}
    ${sources}
    <h2>Coverage</h2>
    <section class="card">
      <table>
        <caption class="vh">Coverage map per account and scope</caption>
        <thead><tr><th scope="col">Account</th><th scope="col">Scope</th><th scope="col">Coverage</th>
        <th scope="col">Oldest observed</th><th scope="col">Gap reason</th><th scope="col">Last success</th></tr></thead>
        <tbody>${cobertura}</tbody>
      </table>
    </section>`;
}

/* ----------------------------------------------------------- conversation -- */

function sourceThread(thread) {
  const account = thread.account || {};
  const messages = (thread.messages || []).map((m) => `
    <div class="thread-message">
      <p class="row">
        <strong>${escapeHtml(m.sender_text)}</strong>
        <span class="tiny">${escapeHtml(fmtTime(m.source_time))}</span>
        ${pill(m.read_state, m.read_state === 'unread' ? 'state-needs_me' : 'state-idle')}
        ${m.hidden_state === 'hidden' ? pill('application-hidden', 'state-warn') : ''}
        ${m.mute_state === 'muted' ? pill('muted', 'state-warn') : ''}
        ${m.availability !== 'available' ? pill(m.availability, 'state-warn') : ''}
        ${mockLine(m.mock_label)}
      </p>
      ${m.subject ? `<p><strong>${escapeHtml(m.subject)}</strong></p>` : ''}
      ${m.snippet ? `<p>${escapeHtml(m.snippet)}</p>` : ''}
      <p class="tiny">${escapeHtml(m.body_note || '')}</p>
    </div>`).join('');
  return `
    <article class="thread">
      <h3>${escapeHtml(thread.adapter)} · ${escapeHtml(thread.audience_kind)} ·
        <span class="muted">${escapeHtml(thread.relevance)}</span></h3>
      ${kv([
    ['sending account', escapeHtml(account.display_name || account.adapter || 'unknown')
      + ' — ' + escapeHtml(account.account_identity || '')],
    ['host role', escapeHtml(account.host_role || '')],
    ['audience', escapeHtml(thread.audience_text)],
    ['provider id', escapeHtml(thread.namespaced_id)],
    ['destination', escapeHtml(thread.provider_thread_id || thread.provider_chat_id || '—')
      + (thread.provider_is_merged ? ' (merged chat: routing must be confirmed)' : '')],
    ['availability', escapeHtml(thread.availability)
      + (thread.availability_reason ? ' — ' + escapeHtml(thread.availability_reason) : '')],
    ['source times', escapeHtml(thread.source_time_first || '?') + ' → '
      + escapeHtml(thread.source_time_last || '?') + ' (observed ' + escapeHtml(thread.freshness) + ' ago)'],
    ['retrieval pointer', escapeHtml(thread.retrieval_pointer || '—')],
  ])}
      ${messages || '<p class="muted">No messages are recorded for this conversation.</p>'}
    </article>`;
}

function agentPicker(conversation, agents) {
  const items = (agents.items || []).map((a) => `
    <label class="row" for="agent-${escapeHtml(a.name)}">
      <input type="radio" id="agent-${escapeHtml(a.name)}" name="assign-agent" value="${escapeHtml(a.name)}"
             ${a.name === 'researcher' ? 'checked' : ''}>
      <span><strong>${escapeHtml(a.name)}</strong>
      ${a.description ? '<br><span class="tiny">' + escapeHtml(a.description) + '</span>' : ''}
      <br><span class="tiny">${escapeHtml(a.source === 'named_in_ledger'
        ? 'named in the ledger' : 'declared catalogue')}</span></span>
    </label>`).join('');
  return `
    <div class="agent-picker stack" id="agent-picker" hidden>
      <p class="tiny">${escapeHtml(agents.note)} ${mockLine(agents.mock_label)}</p>
      <fieldset>
        <legend>Choose the agent that will author the result</legend>
        <div class="agents">${items}</div>
      </fieldset>
      <div class="actions">
        <button type="button" class="primary" id="confirm-assign">Assign to this agent</button>
        <button type="button" class="quiet" id="cancel-assign">Cancel</button>
      </div>
    </div>`;
}

function instructionBox(conversation, agents) {
  const wsId = conversation.ws_conv_id;
  const stored = savedText('instruction', wsId);
  return `
    <section class="card" aria-labelledby="instruction-heading">
      <h2 id="instruction-heading">Give an instruction</h2>
      <p class="muted">Stored as a new input revision before anything runs. The instruction box stays
      primary: direct instructions are always available, not hidden behind suggestions.</p>
      <form id="instruction-form" class="stack">
        <label for="instruction-text">Instruction</label>
        <textarea id="instruction-text" name="instruction" rows="4"
          placeholder="e.g. Check the order status and draft a reply with the current delivery date."
          aria-describedby="instruction-help">${escapeHtml(stored || '')}</textarea>
        <p class="tiny" id="instruction-help">Your text is kept on this device, so navigating away or
        losing the network does not lose it.</p>
        <div class="actions">
          <button type="button" class="primary" id="assign-button"
                  aria-expanded="false" aria-controls="agent-picker">Assign to an agent ▸</button>
          <button type="submit" class="quiet">Save text on this device</button>
        </div>
      </form>
      ${agentPicker(conversation, agents)}
    </section>`;
}

function jobBlock(job) {
  const inputs = (job.inputs || []).map((i) => `
    <li><span class="when">${escapeHtml(fmtTime(i.at))} · v${i.version} · ${escapeHtml(i.kind)}
      · ${escapeHtml(i.author)}${i.delivered_to_worker_at
        ? ' · delivered to worker' : ' · not delivered to a worker'}</span><br>
      ${escapeHtml(i.content)}</li>`).join('');
  const transitions = (job.transitions || []).map((t) => `
    <li><span class="when">${escapeHtml(fmtTime(t.at))} · ${escapeHtml(t.actor)}</span><br>
      ${escapeHtml(t.from_state || '(new)')} → ${escapeHtml(t.to_state)}: ${escapeHtml(t.reason)}</li>`).join('');
  const attempts = (job.attempts || []).map((a) => `
    <li><span class="when">${escapeHtml(fmtTime(a.started_at))} · attempt ${a.attempt_no}
      · worker ${escapeHtml(a.lease_owner || 'unknown')}</span><br>
      ${a.ended_at ? 'closed as ' + escapeHtml(a.outcome_code || '') + ' — ' + escapeHtml(a.outcome_detail || '')
        : 'still open'}</li>`).join('');
  return `
    <article class="card" id="job-${escapeHtml(job.job_id)}">
      <div class="spread">
        <h3>Job ${escapeHtml(job.job_id.slice(0, 12))}…</h3>
        ${pill(job.job_state, job.job_state === 'failed' ? 'state-danger' : 'state-working')}
      </div>
      ${kv([
    ['agent', escapeHtml(job.agent)],
    ['progress', escapeHtml(job.progress || '')],
    ['age', escapeHtml(job.age || 'unknown') + ' since last change, '
      + escapeHtml(job.age_created || '?') + ' since creation'],
    ['attempts', escapeHtml(job.attempt_count)],
    ['stalls', escapeHtml(job.stall_count || 0)],
    ['lease', escapeHtml(job.lease && job.lease.state || 'none')
      + (job.lease && job.lease.owner ? ' held by ' + escapeHtml(job.lease.owner) : '')],
    ['last error', escapeHtml(job.last_error_category || '—')],
    ['capability plan', escapeHtml(JSON.stringify(job.capability_plan || {}))],
  ])}
      <div class="actions">
        <button type="button" class="quiet" data-inspect="${escapeHtml(job.job_id)}"
                aria-expanded="false" aria-controls="job-cancel-${escapeHtml(job.job_id)}">Inspect</button>
        <button type="button" class="warn" data-cancel="${escapeHtml(job.job_id)}">Cancel job</button>
      </div>
      <details>
        <summary>Job ledger detail (${(job.transitions || []).length} transitions,
          ${(job.attempts || []).length} attempts)</summary>
        <h4>Input versions</h4>
        <ul class="history">${inputs || '<li>No inputs recorded.</li>'}</ul>
        <h4>State transitions</h4>
        <ul class="history">${transitions || '<li>No transitions recorded.</li>'}</ul>
        <h4>Worker attempts</h4>
        <ul class="history">${attempts || '<li>No attempts recorded.</li>'}</ul>
      </details>
    </article>`;
}

function addInformationBox(conversation) {
  const running = (conversation.jobs || []).filter((j) =>
    !['succeeded', 'failed', 'cancelled', 'superseded'].includes(j.job_state));
  if (!running.length) return '';
  const job = running[0];
  const stored = savedText('addinfo', job.job_id);
  const waiting = job.job_state === 'waiting_for_user';
  return `
    <section class="card" aria-labelledby="addinfo-heading">
      <h2 id="addinfo-heading">Add information${waiting ? ' — a question is waiting' : ''}</h2>
      <p class="muted">This is saved as a new input version or follow-up turn on
      <strong>${escapeHtml(job.job_id.slice(0, 12))}…</strong>. A prompt that is already executing is
      never mutated. ${waiting
        ? 'An informational answer is not an approval of a separate external action.'
        : ''}</p>
      <form id="addinfo-form" class="stack" data-job="${escapeHtml(job.job_id)}">
        <label for="addinfo-text">${waiting ? 'Your answer' : 'Additional information'}</label>
        <textarea id="addinfo-text" rows="3">${escapeHtml(stored || '')}</textarea>
        <div class="row">
          <label for="addinfo-kind">Record as</label>
          <select id="addinfo-kind">
            ${waiting ? '<option value="answer">answer</option>' : ''}
            <option value="information">information</option>
            <option value="follow_up">follow-up turn</option>
          </select>
          <button class="primary" type="submit">Add to job</button>
        </div>
      </form>
    </section>`;
}

function reviewPanel(conversation) {
  const draft = conversation.drafts.find((d) => !d.superseded_by) || null;
  const approval = conversation.approval;
  const results = conversation.conversation.results || [];
  const job = conversation.conversation.job;
  const evidence = conversation.source_evidence || [];
  if (!draft && !results.length) {
    return `<section class="card"><h2>Review</h2>
      <p class="muted">No result has been returned for this conversation yet.</p></section>`;
  }
  const stored = draft ? savedText('draft', draft.draft_id) : null;
  const draftBody = stored !== null ? stored : (draft ? draft.body : '');
  const edited = stored !== null && draft && stored !== draft.body;
  const approvalLine = approval
    ? `${pill(approval.approval_state, approval.state_binding.invalid ? 'state-danger' : 'state-ok')}
       approval ${escapeHtml(approval.approval_id.slice(0, 14))}… bound to
       ${escapeHtml(approval.draft_version_key)}
       ${approval.state_binding.invalid
        ? '— INVALID (' + escapeHtml(approval.state_binding.reason || 'superseded') + ')'
        : (approval.state_binding.expired ? '— expired' : '')}`
    : 'No approval exists for this draft version.';

  const draftCard = draft ? `
    <section class="card mock-card" aria-labelledby="draft-heading">
      <h2 id="draft-heading">Draft for review (v${draft.version})</h2>
      <p class="row">
        ${mockLine(draft.mock_label)}
        ${pill('authored by ' + draft.author, 'state-ok')}
        ${pill('immutable version ' + draft.draft_version_key, 'state-idle')}
      </p>
      <p class="tiny">The agent that authored it: <strong>${escapeHtml(draft.author)}</strong>. Editing
      creates a new immutable version and invalidates any approval of this one.</p>
      ${kv([
    ['sending account', escapeHtml(draft.sender_account.display_name || '')
      + ' — ' + escapeHtml(draft.sender_identity || draft.sender_account.identity || '')],
    ['channel / adapter', escapeHtml(draft.channel)],
    ['destination', escapeHtml(draft.destination_view.adapter || '') + ' '
      + escapeHtml(draft.destination_view.audience_kind || '') + ' — '
      + escapeHtml(draft.destination_view.namespaced_id || draft.destination.conv_id)
      + (draft.destination.resolved ? '' : ' (UNRESOLVED)')],
    ['mode', escapeHtml(draft.mode_label)],
    ['subject', draft.subject ? escapeHtml(draft.subject) : '(none)'],
    ['purpose', escapeHtml(draft.purpose)],
    ['body hash', escapeHtml(draft.hashes.body)],
    ['audience hash', escapeHtml(draft.hashes.audience)],
    ['attachment hash', escapeHtml(draft.hashes.attachment)],
  ])}
      ${contentDigest(draft)}
      <h3>Recipients and audience snapshot — shown in full</h3>
      <ul class="recipients">${(draft.recipient_lines || []).length
    ? draft.recipient_lines.map((l) => `<li><span class="role">${escapeHtml(l.role)}</span>
        ${escapeHtml(l.text)}</li>`).join('')
    : '<li>(no recipients recorded in this audience snapshot)</li>'}</ul>
      ${recap('Audience snapshot as recorded', draft.audience_snapshot)}
      <h3>Attachments</h3>
      ${attachmentsBlock(draft.attachments)}
      ${draft.blocking_limitations
        ? `<div class="callout danger">Approval is blocked: ${escapeHtml(draft.blocking_limitations)}</div>`
        : ''}
      <h3>Actions</h3>
      <p class="tiny">${approvalLine}</p>
      <form id="draft-form" class="stack" data-draft="${escapeHtml(draft.draft_id)}"
            data-version="${draft.version}">
        <label for="draft-body">Full draft text — readable and editable</label>
        <textarea id="draft-body" class="draft" rows="14"
                  aria-describedby="draft-help">${escapeHtml(draftBody)}</textarea>
        <p class="tiny" id="draft-help">${edited
          ? 'You have unsaved edits on this device. Saving creates a new version and invalidates the approval.'
          : 'No edits on this device.'}</p>
        <div class="actions">
          <button type="submit" class="primary" id="save-draft">Save as new version</button>
          <button type="button" class="warn" id="request-changes">Request changes</button>
          <button type="button" class="danger" id="reject-draft">Reject</button>
          ${approval && !approval.state_binding.invalid && !approval.state_binding.expired
            ? `<button type="button" class="primary" id="dispatch-approval"
                 data-approval="${escapeHtml(approval.approval_id)}">Dispatch approved draft</button>`
            : `<button type="button" class="primary" id="approve-draft"
                 data-draft="${escapeHtml(draft.draft_id)}"
                 ${draft.blocking_limitations ? 'disabled aria-describedby="blocked-note"' : ''}>
                 Approve and bind this version</button>`}
        </div>
        ${draft.blocking_limitations
          ? '<p class="tiny" id="blocked-note">Approval stays disabled until the blocking limitation is resolved or explicitly acknowledged by Randy.</p>'
          : ''}
        <div class="row" id="approve-options" hidden>
          <label for="operation-id">Operation ID (one dispatch decision per ID)</label>
          <input type="text" id="operation-id" value="${escapeHtml('op-' + Date.now())}">
          <label for="ack-limitations"><input type="checkbox" id="ack-limitations">
            Acknowledge the stated limitation so approval may proceed</label>
        </div>
      </form>
    </section>` : '';

  const mockWorker = job && ['queued', 'running', 'waiting_for_source'].includes(job.job_state)
    ? `<section class="card">
        <h2>Run the simulated worker pass</h2>
        <div class="callout mock">MOCK: no agent, model or Hermes run exists in this deployment.
        This button exercises the application-owned part of a job (lease, draft, review state) so the
        ledger and this client can be demonstrated. It creates a labelled draft and never sends.</div>
        <div class="actions">
          <button type="button" class="warn" data-job="${escapeHtml(job.job_id)}" id="mock-run-draft">MOCK: produce a draft</button>
          <button type="button" class="quiet" data-job="${escapeHtml(job.job_id)}" id="mock-run-question">MOCK: return a question</button>
          <button type="button" class="quiet" data-job="${escapeHtml(job.job_id)}" id="mock-run-fail">MOCK: report a failure</button>
        </div>
      </section>` : '';

  return `
    <section class="card">
      <h2>Review</h2>
      <p class="muted">Reason, result, draft and evidence come first; the raw ledger is collapsed at
      the bottom of this page.</p>
      ${kv([
    ['why this is here', escapeHtml(conversation.conversation.reason || '')],
    ['task status', escapeHtml(conversation.conversation.review_label || 'no review state')
      + ' · job ' + escapeHtml((job && job.job_state) || 'none')],
    ['agent credited', escapeHtml((job && job.agent) || (draft && draft.author) || 'none')],
  ])}
      <h3>Results</h3>
      <ul class="history">${results.length ? results.map((r) => `
        <li><span class="when">${escapeHtml(fmtTime(r.created_at))} · ${escapeHtml(r.kind)}
          · v${r.version}</span><br>${escapeHtml(r.summary)}
          ${r.evidence && r.evidence.length
            ? '<br><span class="tiny">evidence: ' + escapeHtml(JSON.stringify(r.evidence)) + '</span>' : ''}</li>`).join('')
    : '<li>No result recorded.</li>'}</ul>
      <h3>Source evidence (before any low-level log)</h3>
      <ul class="history">${evidence.length ? evidence.map((e) => `
        <li><span class="when">${escapeHtml(fmtTime(e.observed_at))} · ${escapeHtml(e.kind)}</span><br>
        ${escapeHtml(e.detail)}${e.limitation
          ? '<br><span class="tiny">limitation: ' + escapeHtml(e.limitation) + '</span>' : ''}
        <br><span class="tiny">${escapeHtml(e.ref || '')}</span></li>`).join('')
    : '<li>No source evidence recorded.</li>'}</ul>
    </section>
    ${mockWorker}
    ${draftCard}
    ${effectPanel(conversation)}`;
}

function contentDigest(draft) {
  return `<details><summary>Content digests bound into any approval</summary>
    <pre>${escapeHtml(JSON.stringify(draft.hashes, null, 2))}</pre></details>`;
}

function attachmentsBlock(attachments) {
  if (!attachments || !attachments.length) return '<p class="muted">No attachments.</p>';
  return `<table>
    <caption class="vh">Attachments referenced by this draft</caption>
    <thead><tr><th scope="col">File</th><th scope="col">Type</th><th scope="col">Size</th>
    <th scope="col">Download</th><th scope="col">Availability</th><th scope="col">Limitation</th></tr></thead>
    <tbody>${attachments.map((a) => `<tr>
      <td class="wrap-anywhere">${escapeHtml(a.filename)}<br><span class="tiny">${escapeHtml(a.namespaced_id)}</span></td>
      <td class="wrap-anywhere">${escapeHtml(a.media_type)}</td>
      <td>${a.size_bytes === null || a.size_bytes === undefined ? 'unknown' : escapeHtml(a.size_bytes)}</td>
      <td>${escapeHtml(a.download_state)}</td>
      <td>${a.availability === 'available' ? pill('available', 'state-ok') : pill(a.availability, 'state-warn')}</td>
      <td class="wrap-anywhere">${escapeHtml(a.limitation || '—')}</td></tr>`).join('')}</tbody>
  </table>`;
}

function recap(label, snapshot) {
  if (!snapshot || !Object.keys(snapshot).length) return '';
  return `<details><summary>${escapeHtml(label)}</summary>
    <pre>${escapeHtml(JSON.stringify(snapshot, null, 2))}</pre></details>`;
}

function effectPanel(conversation) {
  const effect = conversation.effect;
  const send = conversation.send_state;
  if (!effect) {
    return `<section class="card"><h2>Send state</h2>
      <p class="muted">Nothing has been dispatched for this conversation. Dispatch is only possible
      from an approval bound to one immutable draft version.</p></section>`;
  }
  const receipts = (effect.receipts || []).map((r) => `
    <li><span class="when">${escapeHtml(fmtTime(r.observed_at))}</span><br>
      state ${escapeHtml(r.effect_state)} · verification ${escapeHtml(r.verification_level)} ·
      delivery ${escapeHtml(r.delivery_state)} ·
      <strong>verified_against_real_source=${escapeHtml(r.verified_against_real_source)}</strong>
      <br><span class="tiny">${escapeHtml(r.limitations || '')}</span></li>`).join('');
  const attempts = (effect.attempts || []).map((a) => `
    <li><span class="when">${escapeHtml(fmtTime(a.started_at))} · attempt ${a.attempt_no} ·
      ${escapeHtml(a.phase)}${a.submitted ? ' (submitted)' : ' (not submitted)'}</span><br>
      ${escapeHtml(a.outcome_code)}${a.error_category ? ' [' + escapeHtml(a.error_category) + ']' : ''}
      ${a.retry_allowed ? ' · retry allowed under the same authorization' : ' · no retry'}
      <br><span class="tiny">idempotency key ${escapeHtml(effect.idempotency_key)}</span></li>`).join('');
  const cls = send.is_green_sent ? 'ok' : (send.state === 'outcome_unknown' || send.state === 'failed')
    ? 'danger' : 'warn';
  return `
    <section class="card" aria-labelledby="send-heading">
      <h2 id="send-heading">Send state (from the effect ledger)</h2>
      <div class="callout ${cls}">
        <p><strong>${escapeHtml(send.label)}</strong></p>
        <p>${escapeHtml(send.detail || '')}</p>
        ${send.is_green_sent
    ? '<p>Confirmed by a read-back against a real source.</p>'
    : '<p><strong>This is not a confirmed send.</strong> No green "sent" is shown because the ledger '
      + 'has not confirmed it and verified_against_real_source is false.</p>'}
      </div>
      ${kv([
    ['operation ID', escapeHtml(effect.operation_id)],
    ['target', escapeHtml(effect.target.conv_id) + ' · provider '
      + escapeHtml(effect.target.provider_id || '—')],
    ['actual routed destination', escapeHtml(String(send.actual_routed_destination || 'not reported'))],
    ['provider message id', escapeHtml(String(send.provider_message_id || 'not reported'))],
    ['provider status', escapeHtml(String(send.provider_status || 'not reported'))],
    ['requires reconciliation', send.requires_reconciliation ? 'yes' : 'no'],
    ['verification level', escapeHtml(String(send.verification_level || 'none recorded'))],
    ['verified_against_real_source', String(send.verified_against_real_source)],
    ['last error category', escapeHtml(String(send.last_error_category || '—'))],
    ['attempts', escapeHtml(effect.attempt_count)],
    ['receipt limitations', escapeHtml(send.receipt_limitations || '—')],
  ])}
      <div class="actions">
        ${send.requires_reconciliation || ['dispatching', 'provider_pending', 'outcome_unknown',
      'provider_accepted'].includes(send.state)
      ? `<button type="button" class="primary" data-reconcile="${escapeHtml(effect.effect_id)}">
           Reconcile against the source</button>` : ''}
        <button type="button" class="quiet" data-retry="${escapeHtml(effect.effect_id)}">
          Retry (pre-submission failures only)</button>
      </div>
      <details open><summary>Receipts and dispatch attempts</summary>
        <ul class="history">${receipts || '<li>No receipt recorded.</li>'}</ul>
        <ul class="history">${attempts || '<li>No dispatch attempt recorded.</li>'}</ul>
      </details>
    </section>`;
}

function historyBlock(conversation) {
  const history = conversation.operational_history || [];
  return `
    <section class="card">
      <h2>Operational history</h2>
      <p class="muted">${escapeHtml(conversation.privacy_note || '')}</p>
      <ul class="history">${history.length ? history.map((h) => `
        <li><span class="when">${escapeHtml(fmtTime(h.at))} · ${escapeHtml(h.actor || 'service')}
          · ${escapeHtml(h.kind)}</span><br>${escapeHtml(h.text)}
          ${h.limitations ? '<br><span class="tiny">' + escapeHtml(h.limitations) + '</span>' : ''}</li>`).join('')
    : '<li>No history recorded.</li>'}</ul>
    </section>`;
}

function lowLevelBlock(conversation) {
  const rows = conversation.low_level_log || [];
  return `
    <details>
      <summary>Raw ledger log (${rows.length} audit rows) — shown last, for diagnosis</summary>
      <pre>${escapeHtml(JSON.stringify(rows, null, 2))}</pre>
    </details>`;
}

async function renderConversation(payload, wsId) {
  const c = payload.data;
  const conversation = c.conversation;
  const threads = (c.source_conversations || []).map(sourceThread).join('')
    || '<div class="empty"><p>No source conversation is linked. Work can still be tracked here.</p></div>';
  return `
    <p><a href="#/${escapeHtml(state.route.from || 'needs')}">← Back to ${escapeHtml(state.route.from || 'needs')}</a></p>
    <section class="card">
      <h2>${escapeHtml(conversation.title)}</h2>
      <p class="row">
        ${statePill(conversation.queue_state)}
        ${conversation.review_state !== 'none' ? pill(conversation.review_state, 'state-warn') : ''}
        ${pill(conversation.association.kind + ': ' + conversation.association.id, 'state-idle')}
        ${mockLine(conversation.mock_label)}
      </p>
      <p>${escapeHtml(conversation.reason || '')}</p>
      <p class="tiny">Input revision ${conversation.current_input_revision} · updated
        ${escapeHtml(fmtTime(conversation.updated_at))} · provider boundaries preserved
        (aggregation never merges provider ids or changes recipients).</p>
    </section>
    ${reviewPanel(c)}
    ${addInformationBox(c)}
    ${instructionBox(conversation, c.agents)}
    <h2>Source conversations</h2>
    <p class="tiny">Each thread keeps its own sending account, audience and provider identity. Nothing
    is merged across channels, and reading this page never marks a source message read.</p>
    ${threads}
    <h2>Jobs</h2>
    ${(c.jobs && c.jobs.length) ? c.jobs.map(jobBlock).join('') : '<div class="empty"><p>No job has been created for this conversation.</p></div>'}
    ${historyBlock(c)}
    ${lowLevelBlock(c)}`;
}

/* ------------------------------------------------------------- data loads -- */

async function loadOverview() {
  const payload = await apiRead('/api/overview');
  state.overview = payload;
  renderBanner(payload);
  setCounts(payload.data.counts);
  return payload;
}

async function render() {
  const view = document.getElementById('view');
  const loading = document.getElementById('loading');
  loading.hidden = false;
  markTabs();
  try {
    if (!state.overview) await loadOverview();
    let html = '';
    if (state.route.name === 'working') {
      const payload = await apiRead('/api/working');
      setCounts(payload.data.counts);
      html = await renderWorking(payload, 'working');
    } else if (state.route.name === 'all') {
      const q = state.route.query || '';
      const st = state.route.stateFilter && state.route.stateFilter !== 'all'
        ? '&state=' + encodeURIComponent(state.route.stateFilter) : '';
      const payload = await apiRead('/api/all?q=' + encodeURIComponent(q) + st);
      setCounts(payload.data.counts);
      html = await renderAll(payload, 'all', q);
    } else if (state.route.name === 'rules') {
      const payload = await apiRead('/api/rules-source-health');
      html = await renderRulesHealth(payload);
    } else if (state.route.name === 'conversation') {
      const payload = await apiRead('/api/conversation/' + encodeURIComponent(state.route.arg));
      html = await renderConversation(payload, state.route.arg);
    } else {
      const payload = await apiRead('/api/needs-me');
      setCounts(payload.data.counts);
      html = await renderNeeds(payload, 'needs');
    }
    view.innerHTML = html;
    loading.hidden = true;
    wire(view);
    document.title = 'Switchboard — ' + state.route.name;
    const heading = view.querySelector('h2');
    if (heading) {
      heading.setAttribute('tabindex', '-1');
      heading.focus({ preventScroll: true });
    }
  } catch (err) {
    loading.hidden = true;
    if (err instanceof OfflineError) {
      const banner = document.getElementById('health-banner');
      banner.hidden = false;
      banner.textContent = 'Grace is unreachable right now. Nothing was lost: instructions and draft '
        + 'text stay on this device, and the durable ledger keeps every job. Reconnect and reload.';
      view.innerHTML = '<div class="empty">Offline — the last view could not be reloaded.</div>';
    } else if (err.message !== 'unauthorised') {
      toast(err.message, 'error', () => render());
    }
    view.innerHTML += '';
  }
}

/* ---------------------------------------------------------------- actions -- */

async function mutate(path, body, opts) {
  const options = opts || {};
  try {
    const payload = await api(path, { method: 'POST', body: body || {} });
    const ok = payload.ok !== false;
    const message = payload.detail || payload.result && payload.result.detail || (ok ? 'Done' : 'Refused');
    toast((ok ? '' : 'Refused: ') + message, ok ? 'info' : 'error');
    if (typeof options.onDone === 'function') options.onDone(payload);
    return payload;
  } catch (err) {
    if (err instanceof OfflineError) {
      /* Deliberately no automatic retry: a write may already have reached a source.
         The human decides with the button below. */
      toast('Could not reach Grace — this action was NOT applied. Your text is kept. ' +
        'Press Retry only after checking the conversation.', 'error', () => mutate(path, body, opts));
    } else if (err.message !== 'unauthorised') {
      toast(err.message, 'error');
    }
    return null;
  }
}

function wire(root) {
  const instruction = root.querySelector('#instruction-text');
  if (instruction) {
    const wsId = state.route.arg;
    instruction.addEventListener('input', () => saveText('instruction', wsId, instruction.value));
    const form = root.querySelector('#instruction-form');
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      saveText('instruction', wsId, instruction.value);
      toast('Text kept on this device.', 'info');
    });
    const picker = root.querySelector('#agent-picker');
    const assignButton = root.querySelector('#assign-button');
    assignButton.addEventListener('click', () => {
      picker.hidden = !picker.hidden;
      assignButton.setAttribute('aria-expanded', String(!picker.hidden));
      if (!picker.hidden) {
        const first = picker.querySelector('input[type=radio]');
        if (first) first.focus();
      }
    });
    picker.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') { picker.hidden = true; assignButton.setAttribute('aria-expanded', 'false'); assignButton.focus(); }
    });
    root.querySelector('#cancel-assign').addEventListener('click', () => {
      picker.hidden = true;
      assignButton.setAttribute('aria-expanded', 'false');
      assignButton.focus();
    });
    root.querySelector('#confirm-assign').addEventListener('click', () => {
      const chosen = picker.querySelector('input[name=assign-agent]:checked');
      const text = instruction.value.trim();
      if (!text) { toast('Write the instruction first — the agent needs something to do.', 'error'); instruction.focus(); return; }
      if (!chosen) { toast('Choose an agent.', 'error'); return; }
      mutate('/api/assign', { ws_conv_id: wsId, instruction: text, agent: chosen.value }, {
        onDone: (payload) => {
          clearText('instruction', wsId);
          if (payload && payload.source_untouched === false) {
            toast('Warning: the source message state changed. That must never happen on assignment.',
              'error');
          }
          render();
        },
      });
    });
  }

  const addForm = root.querySelector('#addinfo-form');
  if (addForm) {
    const jobId = addForm.getAttribute('data-job');
    const text = root.querySelector('#addinfo-text');
    text.addEventListener('input', () => saveText('addinfo', jobId, text.value));
    addForm.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const kind = root.querySelector('#addinfo-kind').value;
      if (!text.value.trim()) { toast('Nothing to add yet.', 'error'); return; }
      mutate('/api/jobs/' + encodeURIComponent(jobId) + '/input', { kind: kind, content: text.value }, {
        onDone: () => { clearText('addinfo', jobId); render(); },
      });
    });
  }

  const draftForm = root.querySelector('#draft-form');
  if (draftForm) {
    const draftId = draftForm.getAttribute('data-draft');
    const version = parseInt(draftForm.getAttribute('data-version'), 10);
    const body = root.querySelector('#draft-body');
    const help = root.querySelector('#draft-help');
    body.addEventListener('input', () => {
      saveText('draft', draftId, body.value);
      if (help) help.textContent = 'Unsaved edit on this device. Saving creates a new immutable '
        + 'version and invalidates any approval of the old one.';
    });
    draftForm.addEventListener('submit', (ev) => {
      ev.preventDefault();
      mutate('/api/drafts/' + encodeURIComponent(draftId) + '/revise',
        { body: body.value, expected_version: version }, {
        onDone: (payload) => {
          clearText('draft', draftId);
          if (payload && payload.ok === false && payload.code === 'conflict') {
            toast('Someone else revised this draft. Reloading the current version for review.', 'error');
          }
          render();
        },
      });
    });
    const approve = root.querySelector('#approve-draft');
    if (approve) {
      approve.addEventListener('click', () => {
        const options = root.querySelector('#approve-options');
        if (options && options.hidden) {
          options.hidden = false;
          toast('Confirm the operation ID, then press approve again. Approval binds this exact version, '
            + 'sender, recipients and destination.', 'info');
          return;
        }
        const operationId = root.querySelector('#operation-id').value;
        const ack = root.querySelector('#ack-limitations');
        mutate('/api/drafts/' + encodeURIComponent(draftId) + '/approve', {
          operation_id: operationId,
          acknowledge_limitations: !!(ack && ack.checked),
        }, { onDone: () => render() });
      });
    }
    const dispatch = root.querySelector('#dispatch-approval');
    if (dispatch) {
      dispatch.addEventListener('click', () => {
        mutate('/api/approvals/' + encodeURIComponent(dispatch.getAttribute('data-approval')) + '/dispatch',
          {}, { onDone: () => render() });
      });
    }
    const changes = root.querySelector('#request-changes');
    if (changes) {
      changes.addEventListener('click', () => {
        const note = window.prompt('What should the agent change? (recorded as a follow-up turn)');
        if (note === null) return;
        mutate('/api/drafts/' + encodeURIComponent(draftId) + '/request-changes', { note: note },
          { onDone: () => render() });
      });
    }
    const reject = root.querySelector('#reject-draft');
    if (reject) {
      reject.addEventListener('click', () => {
        if (!window.confirm('Reject this draft? Nothing will be sent and the approval is revoked.')) return;
        mutate('/api/drafts/' + encodeURIComponent(draftId) + '/reject',
          { reason: 'owner rejected the draft' }, { onDone: () => render() });
      });
    }
  }

  const mockRun = root.querySelector('#mock-run-draft');
  if (mockRun) {
    const jobId = mockRun.getAttribute('data-job');
    const run = (body) => mutate('/api/jobs/' + encodeURIComponent(jobId) + '/run', body,
      { onDone: () => render() });
    mockRun.addEventListener('click', () => run({}));
    const question = root.querySelector('#mock-run-question');
    if (question) question.addEventListener('click', () => run({
      question: 'Which order should I reference? (MOCK question from the simulated worker)',
    }));
    const fail = root.querySelector('#mock-run-fail');
    if (fail) fail.addEventListener('click', () => run({
      fail_with: 'retryable_error',
    }));
  }

  root.querySelectorAll('[data-cancel]').forEach((button) => {
    button.addEventListener('click', () => {
      const jobId = button.getAttribute('data-cancel');
      if (!window.confirm('Cancel this job? Queued work stops; an operation already submitted to a '
        + 'source cannot be recalled and is reconciled separately.')) return;
      mutate('/api/jobs/' + encodeURIComponent(jobId) + '/cancel', { reason: 'owner cancelled' },
        { onDone: () => render() });
    });
  });
  root.querySelectorAll('[data-inspect]').forEach((button) => {
    button.addEventListener('click', () => {
      const jobId = button.getAttribute('data-inspect');
      const selector = window.CSS && CSS.escape ? '#job-' + CSS.escape(jobId) : null;
      const card = selector ? root.querySelector(selector) : null;
      const detail = card && card.querySelector('details');
      if (detail) {
        detail.open = !detail.open;
        button.setAttribute('aria-expanded', String(detail.open));
      }
    });
  });
  root.querySelectorAll('[data-reconcile]').forEach((button) => {
    button.addEventListener('click', () => {
      mutate('/api/effects/' + encodeURIComponent(button.getAttribute('data-reconcile')) + '/reconcile',
        {}, { onDone: () => render() });
    });
  });
  root.querySelectorAll('[data-retry]').forEach((button) => {
    button.addEventListener('click', () => {
      mutate('/api/effects/' + encodeURIComponent(button.getAttribute('data-retry')) + '/retry',
        {}, { onDone: () => render() });
    });
  });

  const search = root.querySelector('#search-form');
  if (search) {
    search.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const q = root.querySelector('#q').value;
      const st = root.querySelector('#state-filter').value;
      navigate('#/all?q=' + encodeURIComponent(q) + (st !== 'all' ? '&state=' + st : ''));
    });
    root.querySelector('#clear-search').addEventListener('click', () => navigate('#/all'));
  }
}

/* ------------------------------------------------------------------ boot --- */

window.addEventListener('hashchange', () => {
  state.route = parseHash();
  render();
});
window.addEventListener('online', () => { state.offline = false; toast('Back online.', 'info'); render(); });
window.addEventListener('offline', () => {
  state.offline = true;
  const banner = document.getElementById('health-banner');
  banner.hidden = false;
  banner.textContent = 'This device is offline. Your instruction and draft text stay on this device; '
    + 'the durable ledger keeps every job. Nothing will be sent until you press the button again.';
});

captureToken();
if (!state.token) {
  document.getElementById('loading').textContent =
    'Not authorised. Open the URL printed by `grace serve --print-url` (it carries ?token=…).';
  authExpired();
} else {
  state.route = parseHash();
  render();
}
