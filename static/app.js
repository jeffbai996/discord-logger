// discord-logger UI — vanilla JS, responsive
// All user-controlled data is escaped via esc() before HTML interpolation.

const state = {
  view: 'chat',
  channelId: null,
  channels: [],
  modalMsg: null,
  modalTab: 'redact',
  lastMessages: [],
  sidebarOpen: false,
  // live polling
  liveInterval: 15,       // seconds between polls
  liveTimerId: null,
  nextPollAt: 0,          // unix ms when next poll fires
  countdownId: null,
  fetching: false,
  pendingNewIds: new Set(), // ids seen since user last viewed bottom
  documentVisible: true,
};

const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

// ---------- utility ----------
async function api(path, opts = {}) {
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let body = '';
    try { body = await resp.text(); } catch(e) {}
    throw new Error(`${resp.status}: ${body}`);
  }
  return resp.json();
}

function esc(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function setHtml(el, html) { el.innerHTML = html; }

function toast(msg, kind) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast visible' + (kind ? ' ' + kind : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('visible'), 2400);
}

// ---------- timestamp formatting ----------
function parseTs(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d) ? null : d;
}

function fmtTime(d) {
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtRelativeOrShort(d) {
  const now = new Date();
  const diff = (now - d) / 1000;
  const sameDay = d.toDateString() === now.toDateString();
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();

  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (sameDay) return `today at ${fmtTime(d)}`;
  if (isYesterday) return `yesterday at ${fmtTime(d)}`;
  if (diff < 7 * 86400) {
    return d.toLocaleDateString([], { weekday: 'short' }) + ' at ' + fmtTime(d);
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined }) + ' at ' + fmtTime(d);
}

function fmtDayHeader(d) {
  const now = new Date();
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return 'Today';
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], {
    weekday: 'long', month: 'long', day: 'numeric',
    year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined,
  });
}

// ---------- rendering ----------

// Deterministic author color from their name.
function authorColor(name) {
  if (!name) return 'var(--text)';
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  const hue = Math.abs(h) % 360;
  return `hsl(${hue}, 65%, 70%)`;
}

function renderAttachments(msg) {
  if (!msg.attachments || !msg.attachments.length) return '';
  const items = msg.attachments.map(a => esc(a.filename || 'file')).join(', ');
  const n = msg.attachments.length;
  return `<div class="attachments">📎 ${n} attachment${n > 1 ? 's' : ''}: ${items}</div>`;
}

function renderNotes(msg) {
  if (!msg._notes || !msg._notes.length) return '';
  const lines = msg._notes.map(n =>
    `<div class="note-line"><span>📝</span><span>${esc(n)}</span></div>`
  ).join('');
  return `<div class="notes">${lines}</div>`;
}

function renderMsgLine(msg, opts = {}) {
  const edited = msg._edited ? '<span class="edited-marker">(edited)</span>' : '';
  const deleted = msg._deleted ? ' deleted' : '';
  const d = parseTs(msg.timestamp);
  const tsShort = d ? fmtTime(d) : '';
  return `
    <div class="msg-line${deleted}" data-id="${esc(msg.id)}">
      <span class="msg-line-ts">${esc(tsShort)}</span>
      <span class="content">${esc(msg.content || (msg._deleted ? '[deleted]' : ''))}</span>
      ${edited}
      ${renderAttachments(msg)}
      ${renderNotes(msg)}
      <div class="line-actions">
        <button data-action="edit" aria-label="Edit message">Edit</button>
      </div>
    </div>
  `;
}

// Group consecutive messages from same author within 5 min into one block.
function renderChatFeed(messages) {
  if (!messages.length) {
    return '<div class="empty-state">No messages in this channel.</div>';
  }
  // messages are newest-first from API — reverse for chronological
  const ordered = messages.slice().reverse();

  const chunks = [];
  let lastDayKey = null;
  let currentGroup = null;

  for (const msg of ordered) {
    const d = parseTs(msg.timestamp);
    const dayKey = d ? d.toDateString() : 'unknown';
    if (dayKey !== lastDayKey) {
      if (currentGroup) chunks.push(renderGroup(currentGroup));
      chunks.push(`<div class="day-divider">${esc(d ? fmtDayHeader(d) : 'Unknown date')}</div>`);
      lastDayKey = dayKey;
      currentGroup = null;
    }
    const canContinue = currentGroup
      && currentGroup.author === msg.author_name
      && d && currentGroup.lastTs
      && (d - currentGroup.lastTs) < 5 * 60 * 1000;

    if (canContinue) {
      currentGroup.messages.push(msg);
      currentGroup.lastTs = d;
    } else {
      if (currentGroup) chunks.push(renderGroup(currentGroup));
      currentGroup = {
        author: msg.author_name,
        firstTs: d,
        lastTs: d,
        messages: [msg],
      };
    }
  }
  if (currentGroup) chunks.push(renderGroup(currentGroup));
  return chunks.join('');
}

function renderGroup(g) {
  const color = authorColor(g.author);
  const tsText = g.firstTs ? fmtRelativeOrShort(g.firstTs) : '';
  const lines = g.messages.map(m => renderMsgLine(m)).join('');
  return `
    <div class="msg-group">
      <div class="msg-group-header">
        <span class="author" style="color: ${color}">${esc(g.author || 'unknown')}</span>
        <span class="ts">${esc(tsText)}</span>
      </div>
      <div class="msg-body">${lines}</div>
    </div>
  `;
}

function renderSearchResult(m) {
  const chName = (state.channels.find(c => c.id === m.channel_id) || {}).name || m.channel_id;
  const color = authorColor(m.author_name);
  const d = parseTs(m.timestamp);
  const tsText = d ? fmtRelativeOrShort(d) : '';
  return `
    <div class="msg-group">
      <div class="msg-group-header">
        <span class="author" style="color: ${color}">${esc(m.author_name || 'unknown')}</span>
        <span class="ts">${esc(tsText)}</span>
        <span class="channel-tag">${esc(chName)}</span>
      </div>
      <div class="msg-body">${renderMsgLine(m)}</div>
    </div>
  `;
}

function renderSkeleton() {
  let s = '';
  for (let i = 0; i < 5; i++) {
    s += `
      <div class="skeleton-msg">
        <div class="skeleton-line short"></div>
        <div class="skeleton-line long"></div>
        <div class="skeleton-line med"></div>
      </div>
    `;
  }
  return s;
}

// ---------- loaders ----------
async function loadChannels() {
  try {
    state.channels = await api('/api/channels');
  } catch (e) {
    setHtml($('#channelList'), `<div class="empty-state">Error: ${esc(e.message)}</div>`);
    return;
  }
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
      state.pendingNewIds.clear();
      state.lastMessages = [];
      updateNewMsgPill();
      $$('.channel').forEach(c => c.classList.toggle('active', c.dataset.id === state.channelId));
      updateCurrentChannelLabel();
      closeSidebar();
      setView('chat');  // triggers loadChat + startLivePolling
    });
  });
}

function updateCurrentChannelLabel() {
  const el = $('#currentChannel');
  if (!state.channelId) {
    el.classList.remove('visible');
    el.textContent = '';
    return;
  }
  const c = state.channels.find(x => x.id === state.channelId);
  el.textContent = c ? c.name : state.channelId;
  el.classList.add('visible');
}

async function loadChat(opts = {}) {
  const { silent = false } = opts;
  if (!state.channelId) {
    setHtml($('#content'), '<div class="empty-state">Select a channel to view messages</div>');
    return;
  }
  if (!silent) setHtml($('#content'), renderSkeleton());

  setFetching(true);
  let messages;
  try {
    messages = await api(`/api/messages/${encodeURIComponent(state.channelId)}?limit=300`);
  } catch (e) {
    setFetching(false);
    if (!silent) setHtml($('#content'), `<div class="empty-state">Error: ${esc(e.message)}</div>`);
    return;
  }
  setFetching(false);

  if (silent) {
    // Diff against what's already rendered
    applyLiveUpdate(messages);
  } else {
    state.lastMessages = messages;
    state.pendingNewIds.clear();
    updateNewMsgPill();
    setHtml($('#content'), renderChatFeed(messages));
    attachMessageHandlers();
    scrollToBottom(false);
  }
}

function applyLiveUpdate(newMessages) {
  // API returns newest-first. Existing state.lastMessages is also newest-first.
  const oldIds = new Set(state.lastMessages.map(m => m.id));
  const newArrivals = newMessages.filter(m => !oldIds.has(m.id));

  // Detect edits/deletes too: if any old id now has changed _edited flag or is missing
  const newIds = new Set(newMessages.map(m => m.id));
  const deletedCount = state.lastMessages.filter(m => !newIds.has(m.id)).length;
  const editedCount = newMessages.filter(m => {
    if (!oldIds.has(m.id)) return false;
    const old = state.lastMessages.find(o => o.id === m.id);
    if (!old) return false;
    return JSON.stringify(old) !== JSON.stringify(m);
  }).length;

  state.lastMessages = newMessages;

  const anyChange = newArrivals.length || deletedCount || editedCount;
  if (!anyChange) return;

  const main = $('#main');
  const atBottom = main.scrollTop + main.clientHeight >= main.scrollHeight - 200;

  // Re-render entire feed (simple and correct; 300 msgs is cheap)
  setHtml($('#content'), renderChatFeed(newMessages));
  attachMessageHandlers();

  if (atBottom && newArrivals.length) {
    // User was at bottom — pull them down to see new messages
    scrollToBottom(true);
    pulseLiveDot();
  } else if (newArrivals.length) {
    // Track for the "N new" pill
    newArrivals.forEach(m => state.pendingNewIds.add(m.id));
    updateNewMsgPill();
    pulseLiveDot();
  } else {
    // Only edits/deletes, restore scroll
    pulseLiveDot();
  }

  // Also refresh channel counts
  loadChannels();
}

// ---------- live polling ----------
function startLivePolling() {
  stopLivePolling();
  state.nextPollAt = Date.now() + state.liveInterval * 1000;
  state.countdownId = setInterval(updateLiveCountdown, 500);
  state.liveTimerId = setTimeout(livePoll, state.liveInterval * 1000);
  updateLiveCountdown();
}

function stopLivePolling() {
  if (state.liveTimerId) { clearTimeout(state.liveTimerId); state.liveTimerId = null; }
  if (state.countdownId) { clearInterval(state.countdownId); state.countdownId = null; }
  $('#liveIndicator').textContent = '';
}

async function livePoll() {
  if (!state.documentVisible || state.view !== 'chat' || !state.channelId) {
    // skip this tick; re-schedule
    scheduleNextPoll();
    return;
  }
  await loadChat({ silent: true });
  scheduleNextPoll();
}

function scheduleNextPoll() {
  state.nextPollAt = Date.now() + state.liveInterval * 1000;
  state.liveTimerId = setTimeout(livePoll, state.liveInterval * 1000);
}

function updateLiveCountdown() {
  const ind = $('#liveIndicator');
  if (state.view !== 'chat' || !state.channelId) {
    ind.textContent = '';
    return;
  }
  const remaining = Math.max(0, Math.ceil((state.nextPollAt - Date.now()) / 1000));
  if (state.fetching) {
    ind.textContent = '• fetching';
  } else {
    ind.textContent = `• next ${remaining}s`;
  }
}

function setFetching(v) {
  state.fetching = v;
  $('#liveDot').classList.toggle('fetching', v);
  updateLiveCountdown();
}

function pulseLiveDot() {
  const dot = $('#liveDot');
  dot.classList.remove('new-messages');
  void dot.offsetWidth; // reflow to restart animation
  dot.classList.add('new-messages');
  setTimeout(() => dot.classList.remove('new-messages'), 2500);
}

function updateNewMsgPill() {
  const pill = $('#newMsgPill');
  const count = state.pendingNewIds.size;
  if (count === 0) {
    pill.classList.remove('visible');
    return;
  }
  $('#newMsgCount').textContent = count;
  pill.classList.add('visible');
}

async function runSearch() {
  const q = $('#searchQ').value.trim();
  const author = $('#searchAuthor').value.trim();
  const deleted = $('#searchDeleted').checked;
  if (!q && !author) {
    setHtml($('#content'), '<div class="empty-state">Enter a pattern or author name above.</div>');
    return;
  }
  setHtml($('#content'), renderSkeleton());
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
    setHtml($('#content'), res.map(renderSearchResult).join(''));
    attachMessageHandlers();
    $('#main').scrollTop = 0;
  } catch (e) {
    setHtml($('#content'), `<div class="empty-state">Error: ${esc(e.message)}</div>`);
  }
}

async function loadEditLog() {
  setHtml($('#content'), renderSkeleton());
  try {
    const edits = await api('/api/edits');
    if (!edits.length) {
      setHtml($('#content'), '<div class="empty-state">No edits recorded yet.</div>');
      return;
    }
    edits.reverse();
    setHtml($('#content'), edits.map(e => {
      const detail = e.action === 'note' || e.action === 'redact'
        ? `<div class="detail">${esc(e.content || e.value || '')}</div>`
        : e.action === 'update'
          ? `<div class="detail"><strong>${esc(e.field)}</strong> → ${esc(e.value || '')}</div>`
          : '';
      return `<div class="edit-log-entry">
        <span class="action-tag action-${esc(e.action)}">${esc(e.action)}</span>
        <span class="ts">${esc(e.ts)}</span>
        <span class="msg-id">#${esc(e.msg_id)}</span>
        ${detail}
      </div>`;
    }).join(''));
  } catch (e) {
    setHtml($('#content'), `<div class="empty-state">Error: ${esc(e.message)}</div>`);
  }
}

// ---------- view switching ----------
function setView(view) {
  state.view = view;
  $$('.view-tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $('#searchBar').style.display = view === 'search' ? 'flex' : 'none';
  if (view === 'chat') {
    loadChat();
    if (state.channelId) startLivePolling();
  } else {
    stopLivePolling();
  }
  if (view === 'search') {
    if ($('#searchQ').value || $('#searchAuthor').value) runSearch();
    else setHtml($('#content'), '<div class="empty-state">Enter a pattern and hit Search.</div>');
    setTimeout(() => $('#searchQ').focus(), 50);
  }
  else if (view === 'editlog') loadEditLog();
}

function refreshCurrent() {
  if (state.view === 'chat') loadChat();
  else if (state.view === 'search') runSearch();
  else if (state.view === 'editlog') loadEditLog();
  loadChannels();
}

// ---------- sidebar ----------
function openSidebar() {
  state.sidebarOpen = true;
  $('#sidebar').classList.add('open');
  $('#sidebarBackdrop').classList.add('visible');
}
function closeSidebar() {
  state.sidebarOpen = false;
  $('#sidebar').classList.remove('open');
  $('#sidebarBackdrop').classList.remove('visible');
}
function toggleSidebar() {
  state.sidebarOpen ? closeSidebar() : openSidebar();
}

// ---------- scroll to bottom ----------
function scrollToBottom(smooth) {
  const main = $('#main');
  main.scrollTo({ top: main.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}

function updateScrollBtn() {
  const main = $('#main');
  const atBottom = main.scrollTop + main.clientHeight >= main.scrollHeight - 200;
  const hasContent = state.view === 'chat' && state.lastMessages.length > 0;
  $('#scrollBtn').classList.toggle('visible', hasContent && !atBottom);
  // If user scrolled to bottom, clear any pending-new state
  if (atBottom && state.pendingNewIds.size > 0) {
    state.pendingNewIds.clear();
    updateNewMsgPill();
  }
}

// ---------- modal ----------
function attachMessageHandlers() {
  $$('.msg-line button[data-action="edit"]').forEach(btn => {
    btn.addEventListener('click', e => {
      const msgEl = e.target.closest('.msg-line');
      const msgId = msgEl.dataset.id;
      const group = msgEl.closest('.msg-group');
      const author = group ? group.querySelector('.author').textContent : '';
      openModal(msgId, author, msgEl.querySelector('.content').textContent);
    });
  });
}

function openModal(msgId, author, content) {
  state.modalMsg = { id: msgId, author, content };
  const preview = $('#msgPreview');
  preview.textContent = '';
  const strong = document.createElement('strong');
  strong.textContent = author;
  preview.appendChild(strong);
  preview.appendChild(document.createElement('br'));
  preview.appendChild(document.createTextNode(content));

  $('#updateText').value = content;
  $('#authorText').value = author;
  $('#noteText').value = '';
  $('#redactText').value = '[redacted]';
  setModalTab('redact');
  $('#editModal').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function setModalTab(tab) {
  state.modalTab = tab;
  $$('.modal-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.tab-pane').forEach(p => p.classList.toggle('active', p.dataset.tab === tab));
  const save = $('#modalSave');
  save.className = tab === 'delete' ? 'danger' : 'primary';
  save.textContent = tab === 'delete' ? 'Delete' : 'Save';
}

function closeModal() {
  $('#editModal').classList.remove('open');
  document.body.style.overflow = '';
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
    if (!payload.value.trim()) { toast('Note cannot be empty', 'error'); return; }
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
    toast(tab === 'delete' ? 'Message hidden' : 'Edit saved', 'success');
    refreshCurrent();
  } catch (e) {
    toast('Failed: ' + e.message, 'error');
  }
}

// ---------- init ----------
document.addEventListener('DOMContentLoaded', () => {
  loadChannels();

  $$('.view-tabs button').forEach(b => {
    b.addEventListener('click', () => setView(b.dataset.view));
  });

  $('#searchGo').addEventListener('click', runSearch);
  $('#searchQ').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
  $('#searchAuthor').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
  $('#searchDeleted').addEventListener('change', () => {
    if ($('#searchQ').value || $('#searchAuthor').value) runSearch();
  });

  $('#refreshBtn').addEventListener('click', () => {
    refreshCurrent();
    toast('Refreshed');
  });

  $('#menuBtn').addEventListener('click', toggleSidebar);
  $('#closeSidebarBtn').addEventListener('click', closeSidebar);
  $('#sidebarBackdrop').addEventListener('click', closeSidebar);

  $('#scrollBtn').addEventListener('click', () => scrollToBottom(true));
  $('#newMsgPill').addEventListener('click', () => {
    scrollToBottom(true);
    state.pendingNewIds.clear();
    updateNewMsgPill();
  });
  $('#main').addEventListener('scroll', updateScrollBtn);
  window.addEventListener('resize', updateScrollBtn);

  document.addEventListener('visibilitychange', () => {
    state.documentVisible = !document.hidden;
    if (state.documentVisible && state.view === 'chat' && state.channelId) {
      // Catch up immediately when tab returns to foreground
      livePoll();
    }
  });

  $$('.modal-tabs button').forEach(b => {
    b.addEventListener('click', () => setModalTab(b.dataset.tab));
  });
  $('#modalCancel').addEventListener('click', closeModal);
  $('#modalSave').addEventListener('click', saveEdit);
  $('#editModal').addEventListener('click', e => {
    if (e.target.id === 'editModal') closeModal();
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if ($('#editModal').classList.contains('open')) closeModal();
      else if (state.sidebarOpen) closeSidebar();
    }
    // Keyboard shortcuts only when not in a text field
    const inField = ['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName);
    if (inField) return;
    if (e.key === '/') {
      e.preventDefault();
      setView('search');
    } else if (e.key === 'r' && !e.metaKey && !e.ctrlKey) {
      e.preventDefault();
      refreshCurrent();
      toast('Refreshed');
    } else if (e.key === 'g' && !e.metaKey && !e.ctrlKey) {
      scrollToBottom(true);
    }
  });
});
