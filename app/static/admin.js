(function () {
  'use strict';

  const AUTH_KEY = 'adminToken';
  const LEGACY_AUTH_KEY = 'kevin_admin_token';

  const state = {
    token: sessionStorage.getItem(AUTH_KEY) || sessionStorage.getItem(LEGACY_AUTH_KEY) || '',
    activeTab: 'command',
    overview: null,
    callStats: null,
    contractors: [],
    calls: [],
    numbers: null,
    auditEvents: [],
    filters: {
      contractorStatus: '',
      contractorTier: '',
      hasTwilio: '',
      query: '',
    },
    pendingAction: null,
  };

  const els = {
    overlay: document.getElementById('auth-overlay'),
    authForm: document.getElementById('auth-box'),
    authInput: document.getElementById('auth-input'),
    authError: document.getElementById('auth-error'),
    refreshDot: document.getElementById('refresh-dot'),
    lastUpdated: document.getElementById('last-updated'),
    globalError: document.getElementById('global-error'),
    toast: document.getElementById('toast'),
    modal: document.getElementById('action-modal'),
    actionForm: document.getElementById('action-form'),
    actionTitle: document.getElementById('action-title'),
    actionBefore: document.getElementById('action-before'),
    actionReason: document.getElementById('action-reason'),
    actionSubmit: document.getElementById('action-submit'),
    actionConfirmRow: document.getElementById('action-confirm-row'),
    actionConfirm: document.getElementById('action-confirm'),
  };

  function esc(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function fmtDate(value) {
    if (!value) return '-';
    const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return '-';
    return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  function badge(status) {
    const safe = esc(status || 'unknown');
    return `<span class="badge badge-${safe}">${safe}</span>`;
  }

  function setError(message) {
    els.globalError.hidden = !message;
    els.globalError.textContent = message || '';
  }

  function setLoading(isLoading) {
    els.refreshDot.classList.toggle('loading', isLoading);
  }

  function toast(message, type = 'ok') {
    els.toast.textContent = message;
    els.toast.className = `toast show ${type}`;
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => {
      els.toast.className = 'toast';
    }, 2800);
  }

  async function apiFetch(path) {
    const res = await fetch(path, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (res.status === 401) {
      sessionStorage.removeItem(AUTH_KEY);
      sessionStorage.removeItem(LEGACY_AUTH_KEY);
      showAuth();
    }
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  async function apiPost(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${state.token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  function showAuth() {
    els.overlay.hidden = false;
    els.overlay.style.display = 'flex';
    els.authInput.focus();
  }

  function hideAuth() {
    els.overlay.hidden = true;
    els.overlay.style.display = 'none';
  }

  async function tryAuth(event) {
    event.preventDefault();
    const token = els.authInput.value.trim();
    if (!token) return;
    els.authError.textContent = '';
    state.token = token;
    sessionStorage.setItem(AUTH_KEY, token);
    hideAuth();
    renderAll();
    loadAll();
  }

  function metric(label, value, tone = '') {
    return `
      <div class="metric">
        <div class="metric-label">${esc(label)}</div>
        <div class="metric-value ${tone}">${esc(value ?? '-')}</div>
      </div>
    `;
  }

  function renderCommand() {
    const overview = state.overview || {};
    document.getElementById('overview-cards').innerHTML = [
      metric('Total Users', overview.total_contractors, 'cyan'),
      metric('Active Trials', overview.trials_active, 'yellow'),
      metric('Paid', overview.paid_subscribers, 'green'),
      metric('Expired', overview.expired, 'red'),
      metric('Expiring 7d', overview.trials_expiring_7d, 'yellow'),
      metric('Promo Used', overview.promo_slots_used == null ? '-' : `${overview.promo_slots_used} / 1000`),
    ].join('');

    const callStats = state.callStats || {};
    document.getElementById('call-stat-cards').innerHTML = [
      metric('Today', callStats.calls_today),
      metric('Last 7 Days', callStats.calls_7d),
      metric('Last 30 Days', callStats.calls_30d),
    ].join('');

    const alerts = [];
    if ((overview.expired || 0) > 0) alerts.push(`${overview.expired} expired subscription accounts`);
    if ((overview.trials_expiring_7d || 0) > 0) alerts.push(`${overview.trials_expiring_7d} trials expiring within 7 days`);
    if (state.numbers?.summary?.contractors_missing_numbers) {
      alerts.push(`${state.numbers.summary.contractors_missing_numbers} active contractors missing Kevin numbers`);
    }
    if (state.numbers?.summary?.orphan_numbers) {
      alerts.push(`${state.numbers.summary.orphan_numbers} Twilio numbers not assigned to contractors`);
    }

    document.getElementById('attention-list').innerHTML = alerts.length
      ? alerts.map(item => `<div class="notice notice-soft">${esc(item)}</div>`).join('')
      : '<div class="notice notice-soft">No current alerts.</div>';
  }

  function filteredContractors() {
    const q = state.filters.query.trim().toLowerCase();
    return state.contractors.filter(contractor => {
      if (state.filters.contractorStatus && contractor.subscription_status !== state.filters.contractorStatus) return false;
      if (state.filters.contractorTier && contractor.subscription_tier !== state.filters.contractorTier) return false;
      if (state.filters.hasTwilio === 'true' && !contractor.twilio_number) return false;
      if (state.filters.hasTwilio === 'false' && contractor.twilio_number) return false;
      if (!q) return true;
      return [
        contractor.contractor_id,
        contractor.business_name,
        contractor.owner_name,
        contractor.twilio_number,
      ].join(' ').toLowerCase().includes(q);
    });
  }

  function renderContractors() {
    const rows = filteredContractors();
    const html = `
      <table>
        <thead>
          <tr>
            <th>Business</th>
            <th>Owner</th>
            <th>Status</th>
            <th>Tier</th>
            <th>Expires</th>
            <th>Kevin #</th>
            <th>Created</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${rows.length ? rows.map(contractorRow).join('') : emptyRow(8, 'No contractors found.')}
        </tbody>
      </table>
    `;
    document.getElementById('contractors-table').innerHTML = html;
  }

  function contractorRow(c) {
    return `
      <tr>
        <td><button class="link-button" type="button" data-detail="${esc(c.contractor_id)}">${esc(c.business_name || c.contractor_id)}</button></td>
        <td>${esc(c.owner_name || '-')}</td>
        <td>${badge(c.subscription_status)}</td>
        <td>${esc(c.subscription_tier || '-')}</td>
        <td>${fmtDate(c.subscription_expires)}</td>
        <td>${esc(c.twilio_number || '-')}</td>
        <td>${fmtDate(c.created_at)}</td>
        <td>
          <div class="row-actions">
            <button class="button button-secondary" type="button" data-action="extend" data-id="${esc(c.contractor_id)}">+7d</button>
            <button class="button button-danger" type="button" data-action="revoke" data-id="${esc(c.contractor_id)}">Revoke</button>
          </div>
        </td>
      </tr>
    `;
  }

  function emptyRow(colspan, label) {
    return `<tr><td colspan="${colspan}" class="empty">${esc(label)}</td></tr>`;
  }

  function renderCalls() {
    const rows = state.calls || [];
    document.getElementById('calls-table').innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Contractor</th>
            <th>Caller</th>
            <th>Name</th>
            <th>Route</th>
            <th>Outcome</th>
            <th>Duration</th>
            <th>Summary</th>
            <th>Transcript</th>
          </tr>
        </thead>
        <tbody>
          ${rows.length ? rows.map(callRow).join('') : emptyRow(9, 'No calls found.')}
        </tbody>
      </table>
    `;
  }

  function callRow(call) {
    return `
      <tr>
        <td>${fmtDate(call.timestamp)}</td>
        <td>${esc(call.contractor_id || '-')}</td>
        <td>${esc(call.caller_phone || '-')}</td>
        <td>${esc(call.caller_name || '-')}</td>
        <td>${esc(call.route || '-')}</td>
        <td>${esc(call.outcome || '-')}</td>
        <td>${esc(call.duration_seconds || 0)}s</td>
        <td>${call.has_summary ? 'Yes' : 'No'}</td>
        <td>${call.has_transcript ? 'Yes' : 'No'}</td>
      </tr>
    `;
  }

  function renderNumbers() {
    const numbers = state.numbers || { summary: {}, assigned_numbers: [], contractors_missing_numbers: [], orphan_numbers: [] };
    document.getElementById('numbers-summary').innerHTML = [
      metric('Assigned', numbers.summary.assigned_numbers ?? '-'),
      metric('Missing', numbers.summary.contractors_missing_numbers ?? '-', 'yellow'),
      metric('Orphan', numbers.summary.orphan_numbers ?? '-', 'red'),
    ].join('');
    document.getElementById('numbers-table').innerHTML = `
      <table>
        <thead><tr><th>Type</th><th>Phone Number</th><th>Contractor</th><th>Twilio SID</th><th>Voice URL</th></tr></thead>
        <tbody>
          ${numberRows(numbers)}
        </tbody>
      </table>
    `;
  }

  function numberRows(numbers) {
    const rows = [];
    numbers.assigned_numbers.forEach(item => {
      rows.push(`<tr><td>Assigned</td><td>${esc(item.phone_number)}</td><td>${esc(item.contractor_id)}</td><td>${esc(item.twilio?.sid || '-')}</td><td>${esc(item.twilio?.voice_url || '-')}</td></tr>`);
    });
    numbers.contractors_missing_numbers.forEach(item => {
      rows.push(`<tr><td>Missing</td><td>-</td><td>${esc(item.contractor_id)}</td><td>-</td><td>-</td></tr>`);
    });
    numbers.orphan_numbers.forEach(item => {
      rows.push(`<tr><td>Orphan</td><td>${esc(item.phone_number)}</td><td>-</td><td>${esc(item.sid || '-')}</td><td>${esc(item.voice_url || '-')}</td></tr>`);
    });
    return rows.length ? rows.join('') : emptyRow(5, 'No number inventory loaded.');
  }

  function renderSubscriptions() {
    const rows = [...state.contractors].sort((a, b) => (a.subscription_expires || 0) - (b.subscription_expires || 0));
    document.getElementById('subscriptions-table').innerHTML = `
      <table>
        <thead><tr><th>Contractor</th><th>Status</th><th>Tier</th><th>Expires</th><th>Actions</th></tr></thead>
        <tbody>
          ${rows.length ? rows.map(c => `
            <tr>
              <td>${esc(c.business_name || c.contractor_id)}</td>
              <td>${badge(c.subscription_status)}</td>
              <td>${esc(c.subscription_tier || '-')}</td>
              <td>${fmtDate(c.subscription_expires)}</td>
              <td><button class="button button-secondary" type="button" data-action="extend" data-id="${esc(c.contractor_id)}">Extend Trial</button></td>
            </tr>
          `).join('') : emptyRow(5, 'No subscriptions found.')}
        </tbody>
      </table>
    `;
  }

  function renderAudit() {
    const rows = state.auditEvents || [];
    document.getElementById('audit-table').innerHTML = `
      <table>
        <thead><tr><th>Time</th><th>Action</th><th>Target</th><th>Reason</th><th>Actor</th><th>Diff</th></tr></thead>
        <tbody>
          ${rows.length ? rows.map(event => `
            <tr>
              <td>${fmtDate(event.created_at)}</td>
              <td>${esc(event.action || '-')}</td>
              <td>${esc(event.target_type || '-')}/${esc(event.target_id || '-')}</td>
              <td>${esc(event.reason || '-')}</td>
              <td>${esc(event.actor_type || '-')}</td>
              <td><details><summary>View</summary><pre>${esc(JSON.stringify({ before: event.before || {}, after: event.after || {} }, null, 2))}</pre></details></td>
            </tr>
          `).join('') : emptyRow(6, 'No audit events loaded.')}
        </tbody>
      </table>
    `;
  }

  function renderAll() {
    renderCommand();
    renderContractors();
    renderCalls();
    renderNumbers();
    renderSubscriptions();
    renderAudit();
  }

  async function loadOptional(path, fallback, assign) {
    try {
      assign(await apiFetch(path));
    } catch (error) {
      assign(fallback);
      return error;
    }
    return null;
  }

  async function loadAll() {
    if (!state.token) return;
    setLoading(true);
    setError('');
    const params = new URLSearchParams();
    if (state.filters.contractorStatus) params.set('status', state.filters.contractorStatus);
    if (state.filters.contractorTier) params.set('tier', state.filters.contractorTier);
    if (state.filters.hasTwilio) params.set('has_twilio', state.filters.hasTwilio);
    if (state.filters.query) params.set('q', state.filters.query);

    const errors = await Promise.all([
      loadOptional('/api/admin/overview', {}, data => { state.overview = data; }),
      loadOptional('/api/admin/calls/stats', {}, data => { state.callStats = data; }),
      loadOptional(`/api/admin/contractors?${params.toString()}`, { contractors: [] }, data => { state.contractors = data.contractors || []; }),
      loadOptional('/api/admin/calls', { calls: [] }, data => { state.calls = data.calls || []; }),
      loadOptional('/api/admin/numbers', null, data => { state.numbers = data; }),
      loadOptional('/api/admin/audit-events', { events: [] }, data => { state.auditEvents = data.events || []; }),
    ]);

    const failed = errors.filter(Boolean);
    if (failed.length) setError(`${failed.length} panel${failed.length === 1 ? '' : 's'} failed to load.`);
    els.lastUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    renderAll();
    setLoading(false);
  }

  async function loadDetail(contractorId) {
    const panel = document.getElementById('contractor-detail');
    panel.hidden = false;
    panel.innerHTML = '<div class="notice notice-soft">Loading detail...</div>';
    try {
      const detail = await apiFetch(`/api/admin/contractors/${encodeURIComponent(contractorId)}`);
      const contractor = detail.contractor || {};
      const device = detail.device || {};
      const diagnostics = detail.diagnostics || [];
      panel.innerHTML = `
        <div class="panel-heading"><h2>${esc(contractor.business_name || contractor.contractor_id)}</h2></div>
        <div class="stack">
          ${diagnostics.length
            ? diagnostics.map(item => `<div class="notice notice-soft">${esc(item.severity)}: ${esc(item.code)}</div>`).join('')
            : '<div class="notice notice-soft">No diagnostics.</div>'}
        </div>
        <dl class="key-value">
          <dt>Contractor ID</dt><dd>${esc(contractor.contractor_id)}</dd>
          <dt>Owner</dt><dd>${esc(contractor.owner_name || '-')}</dd>
          <dt>Status</dt><dd>${badge(contractor.subscription_status)}</dd>
          <dt>Tier</dt><dd>${esc(contractor.subscription_tier || '-')}</dd>
          <dt>Twilio Number</dt><dd>${esc(contractor.twilio_number || '-')}</dd>
          <dt>Push Token</dt><dd>${device.push_token_present ? 'Present' : 'Missing'}</dd>
          <dt>VoIP Token</dt><dd>${device.voip_token_present ? 'Present' : 'Missing'}</dd>
          <dt>Device Updated</dt><dd>${fmtDate(device.updated_at)}</dd>
        </dl>
      `;
    } catch (error) {
      panel.innerHTML = `<div class="notice notice-error">Failed to load detail: ${esc(error.message)}</div>`;
    }
  }

  function openAction(type, contractorId) {
    const contractor = state.contractors.find(item => item.contractor_id === contractorId) || { contractor_id: contractorId };
    const isRevoke = type === 'revoke';
    state.pendingAction = { type, contractorId };
    els.actionTitle.textContent = isRevoke ? 'Revoke Subscription' : 'Extend Trial';
    els.actionBefore.innerHTML = `
      <strong>${esc(contractor.business_name || contractor.contractor_id)}</strong><br>
      Status: ${esc(contractor.subscription_status || '-')}<br>
      Expires: ${fmtDate(contractor.subscription_expires)}
    `;
    els.actionReason.value = '';
    els.actionConfirm.value = '';
    els.actionConfirmRow.hidden = !isRevoke;
    els.actionSubmit.disabled = true;
    els.modal.hidden = false;
    els.actionReason.focus();
  }

  function closeAction() {
    state.pendingAction = null;
    els.modal.hidden = true;
  }

  function validateAction() {
    const reasonOk = els.actionReason.value.trim().length >= 3;
    const pending = state.pendingAction;
    const confirmOk = !pending || pending.type !== 'revoke' || els.actionConfirm.value.trim() === pending.contractorId;
    els.actionSubmit.disabled = !(reasonOk && confirmOk);
  }

  async function submitAction(event) {
    event.preventDefault();
    const pending = state.pendingAction;
    if (!pending) return;
    els.actionSubmit.disabled = true;
    const reason = els.actionReason.value.trim();
    try {
      if (pending.type === 'extend') {
        await apiPost(`/api/admin/contractors/${encodeURIComponent(pending.contractorId)}/extend-trial`, { days: 7, reason });
      } else {
        await apiPost(`/api/admin/contractors/${encodeURIComponent(pending.contractorId)}/revoke`, { reason, mode: 'expire_now' });
      }
      toast(pending.type === 'extend' ? 'Trial extended' : 'Subscription revoked');
      closeAction();
      await loadAll();
    } catch (error) {
      toast(`Error: ${error.message}`, 'err');
      validateAction();
    }
  }

  function bindEvents() {
    els.authForm.addEventListener('submit', tryAuth);
    document.getElementById('manual-refresh-btn').addEventListener('click', loadAll);
    document.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => {
        state.activeTab = tab.dataset.tab;
        document.querySelectorAll('.tab').forEach(item => item.classList.toggle('is-active', item === tab));
        document.querySelectorAll('.tab-panel').forEach(panel => {
          panel.classList.toggle('is-active', panel.dataset.panel === state.activeTab);
        });
      });
    });

    document.getElementById('contractor-query').addEventListener('input', event => {
      state.filters.query = event.target.value;
      renderContractors();
    });
    document.getElementById('contractor-status').addEventListener('change', event => {
      state.filters.contractorStatus = event.target.value;
      loadAll();
    });
    document.getElementById('contractor-tier').addEventListener('change', event => {
      state.filters.contractorTier = event.target.value;
      loadAll();
    });
    document.getElementById('contractor-has-twilio').addEventListener('change', event => {
      state.filters.hasTwilio = event.target.value;
      loadAll();
    });

    document.body.addEventListener('click', event => {
      const detailButton = event.target.closest('[data-detail]');
      if (detailButton) loadDetail(detailButton.dataset.detail);
      const actionButton = event.target.closest('[data-action]');
      if (actionButton) openAction(actionButton.dataset.action, actionButton.dataset.id);
    });

    document.getElementById('action-close').addEventListener('click', closeAction);
    els.actionReason.addEventListener('input', validateAction);
    els.actionConfirm.addEventListener('input', validateAction);
    els.actionForm.addEventListener('submit', submitAction);
  }

  bindEvents();
  if (state.token) {
    hideAuth();
    renderAll();
    loadAll();
  } else {
    showAuth();
  }
  window.setInterval(loadAll, 60000);
})();
