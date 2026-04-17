// discord-logger UI — single-file vanilla JS
// All user-controlled data is escaped via esc() before HTML interpolation.

const state = {
  view: 'chat',
  channelId: null,
  channels: [],
  modalMsg: null,
  modalTab: 'redact',
};

const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`);
  return resp.json();
}

function fmtTs(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d)) return ts.slice(0, 19);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

// Escape for safe HTML interpolation — all user data passes through this.
function esc(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function setHtml(el, html) { el.innerHTML = html; }

function renderMessage(msg) {
  const edited = msg._edited ? '<span class="edited-marker"> (edited)</span>' : '';
  const deleted = msg._deleted ? ' deleted' : '';
  const att = msg.attachments && msg.attachments.length
    ? `<div class="attachments">+${msg.attachments.length} attachment${msg.attachments.length > 1 ? 's' : ''}: ${msg.attachments.map(a => esc(a.filename || '')).join(', ')}</div>`
    : '';
  const notes = msg._notes && msg._notes.length
    ? `<div class="notes">${msg._notes.map(n => `📝 ${esc(n)}`).join('<br>')}</div>`
    : '';
  return `
    <div class="message${deleted}" data-id="${esc(msg.id)}">
      <div class="head">
        <span class="author">${esc(msg.author_name || 'unknown')}</span>
        <span class="ts">${esc(fmtTs(msg.timestamp))}${edited}</span>
      </div>
      <div class="content">${esc(msg.content || '')}</div>
      ${att}
      ${notes}
      <div class="actions">
        <button data-action="edit">Edit</button>
      </div>
    </div>
  `;
}

async function loadChannels() {
  state.channels = await api('/api/channels');
  const html = state.channels.map(c => `
    <div class="channel ${c.id === state.channelId ? 'active' : ''}" data-id="${esc(c.id)}">
      <span class="name">${esc(c.name)}</span>
      <span class="count">${c.count}</span>
    </div>
  `).join('');
  setHtml($('#channelList'), html || '<div class="empty-state">No channels logged yet</div>');

  $$('.channel').forEach(el => {
    el.addEventListener('click', () => {
      state.channelId = el.dataset.id;
      state.view = 'chat';
      setView('chat');
      $$('.channel').forEach(c => c.classList.toggle('active', c.dataset.id === state.channelId));
      loadChat();
    });
  });
}

async function loadChat() {
  if (!state.channelId) {
    $('#content').className = 'content empty';
    setHtml($('#content'), '<div>Select a channel to view messages</div>');
    return;
  }
  $('#content').className = 'content';
  setHtml($('#content'), '<div class="empty-state">Loading...</div>');
  try {
    const messages = await api(`/api/messages/${encodeURIComponent(state.channelId)}?limit=300`);
    if (!messages.length) {
      setHtml($('#content'), '<div class="empty-state">No messages in this channel.</div>');
      return;
    }
    const rendered = messages.slice().reverse().map(renderMessage).join('');
    setHtml($('#content'), rendered);
    attachMessageHandlers();
    $('#content').scrollTop = $('#content').scrollHeight;
  } catch (e) {
    setHtml($('#content'), `<div class="empty-state">Error: ${esc(e.message)}</div>`);
  }
}

async function runSearch() {
  const q = $('#searchQ').value.trim();
  const author = $('#searchAuthor').value.trim();
  const deleted = $('#searchDeleted').checked;
  if (!q && !author) {
    setHtml($('#content'), '<div class="empty-state">Enter a pattern or author name.</div>');
    return;
  }
  setHtml($('#content'), '<div class="empty-state">Searching...</div>');
  try {
    const params = new URLSearchParams({ limit: 200 });
    if (q) params.set('q', q);
    if (author) params.set('author', author);
    if (state.channelId) params.set('channel', state.channelId);
    if (deleted) params.set('show_deleted', '1');
    const res = await api('/api/search?' + params.toString());
    if (res.error) {
      setHtml($('#content'), `<div class="empty-state">Search error: ${esc(res.error)}</div>`);
      return;
    }
    if (!res.length) {
      setHtml($('#content'), '<div class="empty-state">No matches.</div>');
      return;
    }
    const html = res.map(m => {
      const chName = (state.channels.find(c => c.id === m.channel_id) || {}).name || m.channel_id;
      return `<div style="margin-bottom: 12px;">
        <div style="color: var(--text-faint); font-size: 12px; margin-bottom: 2px;">${esc(chName)}</div>
        ${renderMessage(m)}
      </div>`;
    }).join('');
    setHtml($('#content'), html);
    attachMessageHandlers();
  } catch (e) {
    setHtml($('#content'), `<div class="empty-state">Error: ${esc(e.message)}</div>`);
  }
}

async function loadEditLog() {
  setHtml($('#content'), '<div class="empty-state">Loading...</div>');
  try {
    const edits = await api('/api/edits');
    if (!edits.length) {
      setHtml($('#content'), '<div class="empty-state">No edits recorded yet.</div>');
      return;
    }
    edits.reverse();
    const html = edits.map(e => {
      const detail = e.action === 'note' || e.action === 'redact'
        ? ` — ${esc(e.content || e.value || '')}`
        : e.action === 'update'
          ? ` — ${esc(e.field)}=${esc(e.value || '')}`
          : '';
      return `<div class="edit-log-entry">
        <span style="color: var(--text-faint);">${esc(e.ts)}</span>
        <span class="action-tag action-${esc(e.action)}">${esc(e.action)}</span>
        <span>${esc(e.msg_id)}</span>${detail}
      </div>`;
    }).join('');
    setHtml($('#content'), html);
  } catch (e) {
    setHtml($('#content'), `<div class="empty-state">Error: ${esc(e.message)}</div>`);
  }
}

function setView(view) {
  state.view = view;
  $$('header nav button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#searchToolbar').style.display = view === 'search' ? 'flex' : 'none';
  if (view === 'chat') loadChat();
  else if (view === 'search') {
    if ($('#searchQ').value || $('#searchAuthor').value) runSearch();
    else setHtml($('#content'), '<div class="empty-state">Enter a pattern and hit Go.</div>');
  }
  else if (view === 'editlog') loadEditLog();
}

function attachMessageHandlers() {
  $$('.message button[data-action="edit"]').forEach(btn => {
    btn.addEventListener('click', e => {
      const msgEl = e.target.closest('.message');
      const msgId = msgEl.dataset.id;
      openModal(msgId, msgEl);
    });
  });
}

function openModal(msgId, msgEl) {
  state.modalMsg = {
    id: msgId,
    author: msgEl.querySelector('.author').textContent,
    content: msgEl.querySelector('.content').textContent,
  };
  // Use textContent to set preview safely — no HTML interpolation needed.
  const preview = $('#msgPreview');
  preview.textContent = '';
  const strong = document.createElement('strong');
  strong.textContent = state.modalMsg.author;
  preview.appendChild(strong);
  preview.appendChild(document.createElement('br'));
  preview.appendChild(document.createTextNode(state.modalMsg.content));

  $('#updateText').value = state.modalMsg.content;
  $('#authorText').value = state.modalMsg.author;
  $('#noteText').value = '';
  $('#redactText').value = '[redacted]';
  setModalTab('redact');
  $('#editModal').classList.add('open');
}

function setModalTab(tab) {
  state.modalTab = tab;
  $$('.modal .tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.modal .tab-pane').forEach(p => p.classList.toggle('active', p.dataset.tab === tab));
  $('#modalSave').className = tab === 'delete' ? 'danger' : 'primary';
  $('#modalSave').textContent = tab === 'delete' ? 'Delete' : 'Save';
}

function closeModal() {
  $('#editModal').classList.remove('open');
  state.modalMsg = null;
}

async function saveEdit() {
  if (!state.modalMsg) return;
  const payload = { msg_id: state.modalMsg.id };
  const tab = state.modalTab;
  if (tab === 'redact') {
    payload.action = 'redact';
    payload.content = $('#redactText').value || '[redacted]';
  } else if (tab === 'update') {
    payload.action = 'update';
    payload.field = 'content';
    payload.value = $('#updateText').value;
  } else if (tab === 'note') {
    payload.action = 'note';
    payload.value = $('#noteText').value;
    if (!payload.value.trim()) { alert('Note cannot be empty'); return; }
  } else if (tab === 'author') {
    payload.action = 'update';
    payload.field = 'author_name';
    payload.value = $('#authorText').value;
  } else if (tab === 'delete') {
    if (!confirm('Hide this message from all views?')) return;
    payload.action = 'delete';
  }

  try {
    await api('/api/edits', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    closeModal();
    if (state.view === 'chat') loadChat();
    else if (state.view === 'search') runSearch();
    else if (state.view === 'editlog') loadEditLog();
  } catch (e) {
    alert('Failed to save: ' + e.message);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadChannels();

  $$('header nav button').forEach(b => {
    b.addEventListener('click', () => setView(b.dataset.view));
  });

  $('#searchGo').addEventListener('click', runSearch);
  $('#searchQ').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
  $('#searchAuthor').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });

  $$('.modal .tabs button').forEach(b => {
    b.addEventListener('click', () => setModalTab(b.dataset.tab));
  });
  $('#modalCancel').addEventListener('click', closeModal);
  $('#modalSave').addEventListener('click', saveEdit);
  $('#editModal').addEventListener('click', e => {
    if (e.target.id === 'editModal') closeModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && $('#editModal').classList.contains('open')) closeModal();
  });
});
