// Repliix Proposal Engine — Vercel frontend
// State stored in localStorage (no server-side DB needed)

const STORAGE_KEY = 'repliix_proposals_v2';

// ---------------------------------------------------------------------------
// State (localStorage)
// ---------------------------------------------------------------------------
function loadProposals() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
  } catch { return []; }
}

function saveProposals(proposals) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(proposals));
}

function upsertProposal(proposal) {
  const proposals = loadProposals();
  const idx = proposals.findIndex(p => p.id === proposal.id);
  if (idx >= 0) proposals[idx] = { ...proposals[idx], ...proposal };
  else proposals.unshift(proposal);
  saveProposals(proposals);
  return loadProposals();
}

// ---------------------------------------------------------------------------
// Relative time
// ---------------------------------------------------------------------------
function relativeTime(isoStr) {
  if (!isoStr) return '—';
  const diff = (Date.now() - new Date(isoStr + 'Z').getTime()) / 1000;
  if (diff < 5)    return 'just now';
  if (diff < 60)   return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(isoStr + 'Z').toLocaleDateString();
}

// ---------------------------------------------------------------------------
// Render table
// ---------------------------------------------------------------------------
function renderTable(proposals) {
  const tbody = document.getElementById('tbody');

  if (!proposals.length) {
    tbody.innerHTML = `
      <tr><td colspan="6">
        <div class="empty">
          <h3>No proposals yet</h3>
          <p>Click "Load Latest Transcripts" to fetch your Fireflies calls and generate proposals.</p>
        </div>
      </td></tr>`;
    updateStats(proposals);
    return;
  }

  updateStats(proposals);

  tbody.innerHTML = proposals.map(p => {
    const shortTitle = (p.title || 'Discovery Call').substring(0, 45);
    const leadName = p.lead_name || '—';
    const leadEmail = p.lead_email ? `<div class="lead-email">${p.lead_email}</div>` : '';

    const receivedTs = p.received_at
      ? `<span class="ts">${relativeTime(p.received_at)}</span>`
      : '<span class="ts">—</span>';

    let readyCol;
    if (p.status === 'processing') {
      readyCol = `<span class="badge badge-processing"><span class="spinner"></span>Processing</span>`;
    } else if (p.status === 'pending') {
      readyCol = `<span class="badge badge-pending">Queued</span>`;
    } else if (p.status === 'skipped') {
      readyCol = `<span class="ts">Skipped (short call)</span>`;
    } else if (p.status === 'failed') {
      readyCol = `<span style="color:#f87171;font-size:12px" title="${p.error || ''}" >Failed</span>`;
    } else if (p.processed_at) {
      readyCol = `<span class="ts ts-ready">${relativeTime(p.processed_at)}</span>`;
    } else {
      readyCol = '<span class="ts">—</span>';
    }

    const sentCol = p.sent_at
      ? `<span class="ts ts-sent">${relativeTime(p.sent_at)}</span>`
      : '<span class="ts">—</span>';

    const viewBtn = p.doc_url
      ? `<a href="${p.doc_url}" target="_blank" class="btn btn-secondary">View Doc</a>`
      : '';

    let sendBtn = '';
    if (p.status === 'sent') {
      sendBtn = `<span class="btn-sent">✓ Sent</span>`;
    } else if ((p.status === 'ready') && p.lead_email) {
      sendBtn = `<button class="btn btn-send" onclick="sendProposal('${p.id}', this)">Send to Lead</button>`;
    } else if ((p.status === 'ready') && !p.lead_email) {
      sendBtn = `<span style="font-size:11px;color:#555">No email found</span>`;
    }

    const retryBtn = (p.status === 'failed' || p.status === 'skipped')
      ? `<button class="btn btn-secondary" onclick="retryProposal('${p.id}', this)" style="font-size:11px;padding:6px 12px">Retry</button>`
      : '';

    return `
      <tr data-id="${p.id}">
        <td class="td-meeting">
          <div class="meeting-title" title="${p.title || ''}">${shortTitle}</div>
        </td>
        <td>
          <div class="lead-name">${leadName}</div>
          ${leadEmail}
        </td>
        <td>${receivedTs}</td>
        <td>${readyCol}</td>
        <td>${sentCol}</td>
        <td><div class="actions">${viewBtn}${sendBtn}${retryBtn}</div></td>
      </tr>`;
  }).join('');
}

function updateStats(proposals) {
  const statsBar = document.getElementById('statsBar');
  if (!proposals.length) { statsBar.style.display = 'none'; return; }
  statsBar.style.display = 'flex';
  document.getElementById('statTotal').textContent = proposals.length;
  document.getElementById('statReady').textContent = proposals.filter(p => p.status === 'ready' || p.status === 'sent').length;
  document.getElementById('statSent').textContent = proposals.filter(p => p.status === 'sent').length;
  document.getElementById('statProcessing').textContent = proposals.filter(p => p.status === 'processing').length;
}

// ---------------------------------------------------------------------------
// Fetch transcripts + process queue
// ---------------------------------------------------------------------------
let isProcessing = false;

async function fetchTranscripts() {
  const btn = document.getElementById('fetchBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Fetching...';

  try {
    const res = await fetch('/api/transcripts');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Fetch failed');

    const existing = loadProposals();
    const existingIds = new Set(existing.map(p => p.id));

    const now = new Date();
    let added = 0;
    const newOnes = [];

    (data.transcripts || []).forEach((t, i) => {
      if (existingIds.has(t.id)) return;
      // Stagger received_at: each 45s before now so they look like a live feed
      const receivedAt = new Date(now.getTime() - i * 45000).toISOString().replace('Z', '');
      newOnes.push({
        id: t.id,
        title: t.title || 'Discovery Call',
        original_date: t.date || '',
        received_at: receivedAt,
        processed_at: null,
        sent_at: null,
        lead_name: null,
        lead_email: null,
        doc_url: null,
        status: 'pending',
      });
      added++;
    });

    if (added === 0) {
      showToast('No new transcripts — all are already loaded.', 'success');
      renderTable(loadProposals());
      return;
    }

    // Save all new ones first (so table shows up immediately)
    let proposals = loadProposals();
    proposals = [...newOnes, ...proposals];
    saveProposals(proposals);
    renderTable(proposals);
    showToast(`${added} new transcript${added > 1 ? 's' : ''} loaded. Generating proposals...`, 'success');

    // Start processing queue
    processQueue();

  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = 'Load Latest Transcripts';
  }
}

async function processQueue() {
  if (isProcessing) return;
  isProcessing = true;

  while (true) {
    const proposals = loadProposals();
    const next = proposals.find(p => p.status === 'pending');
    if (!next) break;

    upsertProposal({ id: next.id, status: 'processing' });
    renderTable(loadProposals());

    try {
      const res = await fetch('/api/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ meeting_id: next.id }),
      });
      const data = await res.json();

      if (!res.ok) throw new Error(data.detail || 'Processing failed');

      if (data.status === 'skipped') {
        upsertProposal({ id: next.id, status: 'skipped' });
      } else {
        upsertProposal({
          id: next.id,
          status: 'ready',
          processed_at: new Date().toISOString().replace('Z', ''),
          doc_url: data.doc_url,
          lead_name: data.lead_name,
          lead_email: data.lead_email,
        });
      }
    } catch (e) {
      upsertProposal({ id: next.id, status: 'failed', error: e.message });
    }

    renderTable(loadProposals());
  }

  isProcessing = false;
}

// ---------------------------------------------------------------------------
// Send proposal
// ---------------------------------------------------------------------------
async function sendProposal(meetingId, btn) {
  btn.disabled = true;
  btn.textContent = 'Sending...';

  const proposals = loadProposals();
  const p = proposals.find(x => x.id === meetingId);
  if (!p) { showToast('Proposal not found', 'error'); return; }

  try {
    const res = await fetch('/api/send', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lead_name: p.lead_name || '',
        lead_email: p.lead_email || '',
        doc_url: p.doc_url || '',
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Send failed');

    upsertProposal({ id: meetingId, status: 'sent', sent_at: data.sent_at || new Date().toISOString().replace('Z', '') });
    showToast(`Proposal sent to ${p.lead_email}`, 'success');
    renderTable(loadProposals());
  } catch (e) {
    showToast(e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Send to Lead';
  }
}

// ---------------------------------------------------------------------------
// Retry
// ---------------------------------------------------------------------------
async function retryProposal(meetingId) {
  upsertProposal({ id: meetingId, status: 'pending', error: null });
  renderTable(loadProposals());
  processQueue();
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------
let toastTimer = null;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 3500);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  renderTable(loadProposals());
  // Refresh timestamps every 30s
  setInterval(() => renderTable(loadProposals()), 30000);
});
