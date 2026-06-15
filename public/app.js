// Proposal Generator — multi-user SaaS frontend

const SUPABASE_URL      = 'https://ztlsfnihvtsgulzjknqp.supabase.co';
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inp0bHNmbmlodnRzZ3VsemprbnFwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODE1MTY2ODMsImV4cCI6MjA5NzA5MjY4M30.rJT2tmYMeE6kkRUm_OmZzp047FP7csqVRUTW7VMUM04';

const { createClient } = supabase;
const sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

let session = null;
let currentOnboardingStatus = null;
let selectedMeetingId = null;
let refreshTimer = null;

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  // Handle Google OAuth callback
  const params = new URLSearchParams(window.location.search);
  if (params.get('google') === 'connected' || params.get('google_error')) {
    window.history.replaceState({}, '', '/');
  }

  const { data } = await sb.auth.getSession();
  session = data.session;

  if (session) {
    await routeUser();
  } else {
    showView('auth-view');
  }

  sb.auth.onAuthStateChange(async (event, s) => {
    session = s;
    if (s) {
      await routeUser();
    } else {
      clearInterval(refreshTimer);
      showView('auth-view');
    }
  });
}

async function routeUser() {
  const status = await api('GET', '/api/onboarding/status');
  currentOnboardingStatus = status;

  if (status.onboarded) {
    startDashboard();
  } else {
    startOnboarding(status);
  }
}

// ---------------------------------------------------------------------------
// View helper
// ---------------------------------------------------------------------------
function showView(id) {
  ['auth-view', 'onboarding-view', 'dashboard-view'].forEach(v => {
    document.getElementById(v).classList.toggle('hidden', v !== id);
  });
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
async function signInWithGoogle() {
  const btn = document.getElementById('googleSignInBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner spinner-dark"></span> Redirecting…';
  hideEl('auth-error');

  const { error } = await sb.auth.signInWithOAuth({
    provider: 'google',
    options: { redirectTo: window.location.origin },
  });

  if (error) {
    showEl('auth-error');
    document.getElementById('auth-error').textContent = error.message || 'Sign-in failed';
    btn.disabled = false;
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.18 1.48-4.97 2.31-8.16 2.31-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg> Continue with Google';
  }
  // On success, Supabase redirects to Google and back — onAuthStateChange picks it up
}

async function signOut() {
  clearInterval(refreshTimer);
  await sb.auth.signOut();
}

// ---------------------------------------------------------------------------
// Onboarding
// ---------------------------------------------------------------------------
let currentStep = 1;

function startOnboarding(status) {
  showView('onboarding-view');

  if (status.has_fireflies && !status.has_google) {
    goStep(2);
  } else if (status.has_fireflies && status.has_google) {
    // Both connected but not "onboarded" — shouldn't happen, go to step 3
    goStep(3);
  } else {
    goStep(1);
  }

  // Pre-fill Google connected state if just came back from OAuth
  if (status.has_google) {
    showEl('google-connected-tag');
    document.getElementById('google-connected-email').textContent = status.google_email || '';
    document.getElementById('connectGoogleBtn').textContent = 'Reconnect Google Account';
  }

  if (status.webhook_url) {
    document.getElementById('webhookUrlDisplay').textContent = status.webhook_url;
  }
}

function goStep(n) {
  currentStep = n;
  [1, 2, 3].forEach(i => {
    document.getElementById(`step${i}`).classList.toggle('hidden', i !== n);

    const dot  = document.getElementById(`dot${i}`);
    dot.classList.remove('done', 'active', 'todo');
    dot.classList.add(i < n ? 'done' : i === n ? 'active' : 'todo');
  });
  if (n > 1) document.getElementById('line1').classList.toggle('done', n > 1);
  if (n > 2) document.getElementById('line2').classList.toggle('done', n > 2);
}

async function saveFirefliesKey() {
  const key = document.getElementById('firefliesKey').value.trim();
  if (!key) return;

  const btn = document.getElementById('step1Btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Validating…';
  hideEl('step1-error');

  try {
    await api('POST', '/api/onboarding/fireflies', { api_key: key });
    currentOnboardingStatus = await api('GET', '/api/onboarding/status');

    if (currentOnboardingStatus.has_google) {
      goStep(3);
      document.getElementById('webhookUrlDisplay').textContent = currentOnboardingStatus.webhook_url || '';
    } else {
      goStep(2);
    }
  } catch (err) {
    showEl('step1-error');
    document.getElementById('step1-error').textContent = err.message || 'Invalid API key';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save & Continue';
  }
}

async function connectGoogle() {
  const btn = document.getElementById('connectGoogleBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner spinner-dark"></span> Redirecting…';
  hideEl('step2-error');

  try {
    const { auth_url } = await api('GET', '/api/auth/google');
    window.location.href = auth_url;
  } catch (err) {
    showEl('step2-error');
    document.getElementById('step2-error').textContent = err.message || 'Failed to start Google OAuth';
    btn.disabled = false;
    btn.textContent = 'Connect Google Account';
  }
}

async function finishOnboarding() {
  startDashboard();
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------
function startDashboard() {
  showView('dashboard-view');
  document.getElementById('dashEmail').textContent = session?.user?.email || '';
  loadProposals();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(loadProposals, 15000);
}

async function loadProposals() {
  try {
    const { proposals } = await api('GET', '/api/proposals');
    renderProposals(proposals);
    renderStats(proposals);
  } catch (err) {
    console.error('Failed to load proposals:', err);
  }
}

function renderStats(proposals) {
  const total      = proposals.length;
  const ready      = proposals.filter(p => p.status === 'ready' || p.status === 'ready_no_doc').length;
  const sent       = proposals.filter(p => p.status === 'sent').length;
  const processing = proposals.filter(p => p.status === 'processing').length;

  document.getElementById('statTotal').textContent      = total;
  document.getElementById('statReady').textContent      = ready;
  document.getElementById('statSent').textContent       = sent;
  document.getElementById('statProcessing').textContent = processing;
}

function renderProposals(proposals) {
  const tbody = document.getElementById('tbody');
  if (!proposals.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty">
      <h3>No proposals yet</h3>
      <p>Proposals appear here automatically after each Fireflies call, or click "Process Meeting" to run one manually.</p>
    </div></td></tr>`;
    return;
  }

  tbody.innerHTML = proposals.map(p => {
    const date = p.created_at ? new Date(p.created_at).toLocaleDateString('en-US', { month:'short', day:'numeric' }) : '—';
    const badge = badgeHtml(p.status);
    const actions = actionsHtml(p);

    return `<tr>
      <td><div class="meeting-title" title="${esc(p.meeting_title || p.meeting_id)}">${esc(p.meeting_title || p.meeting_id)}</div></td>
      <td>
        ${p.lead_name ? `<div class="lead-name">${esc(p.lead_name)}</div>` : '<div class="lead-name" style="color:#94a3b8">—</div>'}
        ${p.lead_email ? `<div class="lead-email">${esc(p.lead_email)}</div>` : ''}
      </td>
      <td>${badge}</td>
      <td class="td-date">${date}</td>
      <td><div class="actions">${actions}</div></td>
    </tr>`;
  }).join('');
}

function badgeHtml(status) {
  const labels = {
    processing: '⏳ Processing',
    ready: '✓ Ready',
    ready_no_doc: '⚠ Ready (no doc)',
    sent: '✉ Sent',
    failed: '✗ Failed',
    skipped: '— Skipped',
  };
  return `<span class="badge badge-${status}">${labels[status] || status}</span>`;
}

function actionsHtml(p) {
  const parts = [];
  if (p.doc_url) {
    parts.push(`<a href="${p.doc_url}" target="_blank" class="btn btn-outline btn-sm">Open Doc</a>`);
  }
  if ((p.status === 'ready' || p.status === 'ready_no_doc') && p.lead_email && p.doc_url) {
    parts.push(`<button class="btn btn-primary btn-sm" onclick="sendEmail('${p.id}', this)">Send Email</button>`);
  }
  if (p.status === 'sent') {
    parts.push(`<span style="font-size:12px;color:#065f46;">✓ Sent</span>`);
  }
  if (p.status === 'failed') {
    parts.push(`<button class="btn btn-ghost btn-sm" onclick="retryProposal('${p.meeting_id}', this)" title="${esc(p.error_message || '')}">Retry</button>`);
  }
  return parts.join('') || '<span style="color:#94a3b8;font-size:12px;">—</span>';
}

async function sendEmail(proposalId, btn) {
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    await api('POST', '/api/send', { proposal_id: proposalId });
    toast('Email sent!', 'success');
    loadProposals();
  } catch (err) {
    toast(err.message || 'Failed to send', 'error');
    btn.disabled = false;
    btn.textContent = 'Send Email';
  }
}

async function retryProposal(meetingId, btn) {
  btn.disabled = true;
  btn.textContent = 'Retrying…';
  try {
    await api('POST', '/api/process', { meeting_id: meetingId });
    toast('Processing started', 'success');
    loadProposals();
  } catch (err) {
    toast(err.message || 'Failed', 'error');
    btn.disabled = false;
    btn.textContent = 'Retry';
  }
}

// ---------------------------------------------------------------------------
// Process modal
// ---------------------------------------------------------------------------
async function openProcessModal() {
  selectedMeetingId = null;
  document.getElementById('processBtn').disabled = true;
  showEl('processModal');

  document.getElementById('transcriptList').innerHTML =
    '<div style="text-align:center;padding:40px"><div class="spinner spinner-dark"></div></div>';

  try {
    const { transcripts } = await api('GET', '/api/transcripts');
    renderTranscripts(transcripts);
  } catch (err) {
    document.getElementById('transcriptList').innerHTML =
      `<p style="color:#dc2626;font-size:13px;">${err.message}</p>`;
  }
}

function renderTranscripts(transcripts) {
  if (!transcripts.length) {
    document.getElementById('transcriptList').innerHTML = '<p style="color:#94a3b8;font-size:13px;">No recent transcripts found.</p>';
    return;
  }
  document.getElementById('transcriptList').innerHTML = transcripts.map(t => {
    const date = t.date ? new Date(parseInt(t.date)).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '';
    const dur  = t.duration ? `${Math.round(t.duration)} min` : '';
    return `<div class="transcript-item" id="ti-${t.id}" onclick="selectTranscript('${t.id}')">
      <div class="transcript-name">${esc(t.title || t.id)}</div>
      <div class="transcript-meta">${[date, dur].filter(Boolean).join(' · ')}</div>
    </div>`;
  }).join('');
}

function selectTranscript(id) {
  if (selectedMeetingId) {
    document.getElementById(`ti-${selectedMeetingId}`)?.classList.remove('selected');
  }
  selectedMeetingId = id;
  document.getElementById(`ti-${id}`)?.classList.add('selected');
  document.getElementById('processBtn').disabled = false;
}

async function processSelected() {
  if (!selectedMeetingId) return;
  const btn = document.getElementById('processBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Starting…';

  try {
    await api('POST', '/api/process', { meeting_id: selectedMeetingId });
    closeModal();
    toast('Proposal generation started', 'success');
    loadProposals();
  } catch (err) {
    toast(err.message || 'Failed', 'error');
    btn.disabled = false;
    btn.textContent = 'Generate Proposal';
  }
}

function closeModal() {
  hideEl('processModal');
  selectedMeetingId = null;
}

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------
async function api(method, path, body = null) {
  const headers = { 'Content-Type': 'application/json' };
  if (session?.access_token) headers['Authorization'] = `Bearer ${session.access_token}`;

  const resp = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function showEl(id) { document.getElementById(id)?.classList.remove('hidden'); }
function hideEl(id) { document.getElementById(id)?.classList.add('hidden'); }
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function copyWebhook() {
  const url = document.getElementById('webhookUrlDisplay').textContent;
  navigator.clipboard.writeText(url).then(() => toast('Webhook URL copied!', 'success'));
}

let toastTimer;
function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 3000);
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', boot);
