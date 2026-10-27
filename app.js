/* ═══════════════════════════════════════════════════════════════════════════════
   DevOps Job Tracker — Dashboard Application
   ═══════════════════════════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  // ── Constants ──────────────────────────────────────────────────────────────
  const STORAGE_KEY = 'devops-job-tracker';
  const TTL_MS = 48 * 60 * 60 * 1000; // 48 hours

  // ── State ──────────────────────────────────────────────────────────────────
  const state = {
    jobs: [],
    config: {},
    lastUpdated: null,
    dismissed: {},    // { jobId: isoTimestamp }
    applied: {},      // { jobId: { date: iso, notes: '' } }
    lastVisit: null,
    activeTab: 'jobs',
    searchQuery: '',
    activeFilter: 'all',
    focusedIndex: -1,
    githubPat: '',
    settingsKeywords: [],
  };

  // ── DOM Refs ───────────────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ── Init ───────────────────────────────────────────────────────────────────
  async function init() {
    loadState();
    state.lastVisit = state.lastVisit || new Date().toISOString();
    bindEvents();
    await fetchJobs();
    render();
    cleanupOld();
    // Update lastVisit after rendering so "new" badges work
    saveState();
    // Fade out kbd hint after 8s
    setTimeout(() => { const h = $('#kbdHint'); if (h) h.style.opacity = '0'; }, 10000);
  }

  // ── Persistence ────────────────────────────────────────────────────────────
  function loadState() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      state.dismissed = saved.dismissed || {};
      state.applied = saved.applied || {};
      state.lastVisit = saved.lastVisit || null;
      state.githubPat = saved.githubPat || '';
    } catch { /* corrupt — start fresh */ }
  }

  function saveState() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      dismissed: state.dismissed,
      applied: state.applied,
      lastVisit: new Date().toISOString(),
      githubPat: state.githubPat,
    }));
  }

  function cleanupOld() {
    const cutoff = Date.now() - TTL_MS;
    for (const [id, ts] of Object.entries(state.dismissed)) {
      if (new Date(ts).getTime() < cutoff) delete state.dismissed[id];
    }
    for (const [id, info] of Object.entries(state.applied)) {
      if (new Date(info.date).getTime() < cutoff) delete state.applied[id];
    }
    saveState();
  }

  // ── Data Fetching ──────────────────────────────────────────────────────────
  async function fetchJobs() {
    try {
      const resp = await fetch('./jobs.json?t=' + Date.now());
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      state.jobs = data.jobs || [];
      state.config = data.config || {};
      state.lastUpdated = data.last_updated || null;
    } catch (e) {
      console.warn('Failed to fetch jobs.json:', e);
      state.jobs = [];
    } finally {
      const el = $('#loadingState');
      if (el) el.hidden = true;
    }
  }

  // ── Filtering ──────────────────────────────────────────────────────────────
  function getVisibleJobs() {
    return state.jobs.filter(job => {
      const id = job.id;
      // Always hide dismissed (unless filter says show all)
      if (state.activeFilter !== 'all' && state.dismissed[id]) return false;
      if (state.activeFilter === 'all' && state.dismissed[id]) return false;
      // Applied jobs go to the Applied tab
      if (state.applied[id]) return false;
      // Priority filter
      if (state.activeFilter === 'priority' && !job.is_priority) return false;
      // Unread filter (new since last visit)
      if (state.activeFilter === 'unread') {
        if (!state.lastVisit) return true;
        const found = new Date(job.found_at || 0);
        if (found <= new Date(state.lastVisit)) return false;
      }
      // Search
      if (state.searchQuery) {
        const q = state.searchQuery.toLowerCase();
        const haystack = `${job.title} ${job.company} ${job.location}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }

  function getAppliedJobs() {
    return state.jobs.filter(job => state.applied[job.id]);
  }

  function countNewSinceLastVisit() {
    if (!state.lastVisit) return state.jobs.length;
    const lv = new Date(state.lastVisit);
    return state.jobs.filter(j => {
      const found = new Date(j.found_at || 0);
      return found > lv && !state.dismissed[j.id] && !state.applied[j.id];
    }).length;
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  function render() {
    renderStats();
    renderTabs();
    if (state.activeTab === 'jobs') {
      renderJobGrid();
      $('#jobsView').hidden = false;
      $('#appliedView').hidden = true;
      $('#filtersBar').style.display = '';
    } else {
      renderAppliedGrid();
      $('#jobsView').hidden = true;
      $('#appliedView').hidden = false;
      $('#filtersBar').style.display = 'none';
    }
  }

  function renderStats() {
    const visible = getVisibleJobs();
    const applied = Object.keys(state.applied).length;
    const newCount = countNewSinceLastVisit();
    const total = state.jobs.length;

    $('#statsBar').innerHTML = `
      <div class="stat stat--accent"><span class="stat__value">${total}</span><span class="stat__label">Total</span></div>
      <div class="stat stat--green"><span class="stat__value">${newCount}</span><span class="stat__label">New</span></div>
      <div class="stat"><span class="stat__value">${visible.length}</span><span class="stat__label">Visible</span></div>
      <div class="stat stat--amber"><span class="stat__value">${applied}</span><span class="stat__label">Applied</span></div>
    `;

    // Update timestamp
    if (state.lastUpdated) {
      $('#lastUpdated').textContent = `Last updated ${timeAgo(state.lastUpdated)}`;
    } else {
      $('#lastUpdated').textContent = 'Waiting for first pipeline run...';
    }
  }

  function renderTabs() {
    const visible = getVisibleJobs();
    const applied = getAppliedJobs();
    $('#jobCount').textContent = visible.length;
    $('#appliedCount').textContent = applied.length;
    $$('.nav-tab').forEach(tab => {
      tab.classList.toggle('nav-tab--active', tab.dataset.tab === state.activeTab);
    });
  }

  function renderJobGrid() {
    const jobs = getVisibleJobs();
    const grid = $('#jobGrid');
    const empty = $('#emptyState');

    if (!jobs.length) {
      grid.innerHTML = '';
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    grid.innerHTML = jobs.map((job, i) => createJobCard(job, i, false)).join('');
    state.focusedIndex = -1;
  }

  function renderAppliedGrid() {
    const jobs = getAppliedJobs();
    const grid = $('#appliedGrid');
    const empty = $('#appliedEmpty');

    if (!jobs.length) {
      grid.innerHTML = '';
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    grid.innerHTML = jobs.map((job, i) => {
      const info = state.applied[job.id] || {};
      return createJobCard(job, i, true, info);
    }).join('');
  }

  function createJobCard(job, index, isApplied, appliedInfo) {
    const isNew = state.lastVisit && new Date(job.found_at || 0) > new Date(state.lastVisit);
    const yoeText = job.yoe_found != null
      ? `${Number.isInteger(job.yoe_found) ? job.yoe_found : job.yoe_found.toFixed(1)}+ YOE`
      : 'YOE N/A';
    const yoeClass = job.yoe_found != null ? 'badge--amber' : 'badge--dim';

    let actions = '';
    if (isApplied) {
      actions = `
        <button class="btn btn--ghost btn--sm" data-action="unapply" data-id="${job.id}">↩ Undo</button>
      `;
    } else {
      actions = `
        <button class="btn btn--primary btn--sm" data-action="apply" data-id="${job.id}" data-url="${job.url || ''}">✓ Applied</button>
        <button class="btn btn--ghost btn--sm" data-action="dismiss" data-id="${job.id}">✕ Dismiss</button>
      `;
    }

    let notesHtml = '';
    if (isApplied && appliedInfo) {
      notesHtml = `
        <div class="job-card__notes">
          <div class="job-card__notes-date">Applied ${timeAgo(appliedInfo.date)}</div>
          ${appliedInfo.notes ? `<div class="job-card__notes-text">"${escHtml(appliedInfo.notes)}"</div>` : ''}
        </div>
      `;
    }

    return `
      <article class="job-card ${job.is_priority ? 'job-card--priority' : ''}"
               data-job-id="${job.id}" style="animation-delay: ${index * 40}ms" tabindex="0">
        <div class="job-card__header">
          <h3 class="job-card__title">
            <a href="${escHtml(job.url || '#')}" target="_blank" rel="noopener">${escHtml(job.title)}</a>
          </h3>
          ${isNew && !isApplied ? '<span class="job-card__new">NEW</span>' : ''}
        </div>
        <div class="job-card__meta">
          <span class="job-card__company">${escHtml(job.company || 'Unknown')}</span>
          <span class="job-card__location">${escHtml(job.location || '')}</span>
          <span class="job-card__time">${escHtml(job.posted_time || '')} · Found ${timeAgo(job.found_at)}</span>
        </div>
        <div class="job-card__badges">
          ${job.is_priority ? '<span class="badge badge--green">★ Priority</span>' : ''}
          <span class="badge ${yoeClass}">${yoeText}</span>
          ${job.is_duplicate ? '<span class="badge badge--dim">Duplicate?</span>' : ''}
        </div>
        <div class="job-card__actions">${actions}</div>
        ${notesHtml}
      </article>
    `;
  }

  // ── Actions ────────────────────────────────────────────────────────────────
  function dismissJob(id) {
    const card = document.querySelector(`[data-job-id="${id}"]`);
    if (card) {
      card.classList.add('job-card--dismissing');
      setTimeout(() => {
        state.dismissed[id] = new Date().toISOString();
        saveState();
        render();
        toast('Job dismissed', 'info');
      }, 280);
    } else {
      state.dismissed[id] = new Date().toISOString();
      saveState();
      render();
    }
  }

  function applyJob(id, url) {
    const notes = '';  // Could prompt for notes in the future
    state.applied[id] = { date: new Date().toISOString(), notes };
    // Remove from dismissed if it was there
    delete state.dismissed[id];
    saveState();
    // Open the LinkedIn URL
    if (url) window.open(url, '_blank');
    render();
    toast('Marked as Applied ✓', 'success');
  }

  function undoApply(id) {
    delete state.applied[id];
    saveState();
    render();
    toast('Removed from Applied', 'info');
  }

  function dismissAllVisible() {
    const visible = getVisibleJobs();
    if (!visible.length) return;
    const now = new Date().toISOString();
    visible.forEach(job => { state.dismissed[job.id] = now; });
    saveState();
    render();
    toast(`Dismissed ${visible.length} job(s)`, 'info');
  }

  // ── Event Binding ──────────────────────────────────────────────────────────
  function bindEvents() {
    // Card actions (delegation)
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      const id = btn.dataset.id;
      if (action === 'dismiss') dismissJob(id);
      else if (action === 'apply') applyJob(id, btn.dataset.url);
      else if (action === 'unapply') undoApply(id);
    });

    // Tab switching
    $$('.nav-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        state.activeTab = tab.dataset.tab;
        render();
      });
    });

    // Filter chips
    const chipsContainer = $('#filterChips');
    if (chipsContainer) {
      chipsContainer.addEventListener('click', (e) => {
        const chip = e.target.closest('.chip');
        if (!chip) return;
        state.activeFilter = chip.dataset.filter;
        $$('.chip').forEach(c => c.classList.remove('chip--active'));
        chip.classList.add('chip--active');
        render();
      });
    }

    // Search
    const searchInput = $('#searchInput');
    if (searchInput) {
      searchInput.addEventListener('input', debounce(() => {
        state.searchQuery = searchInput.value.trim();
        render();
      }, 200));
    }

    // Dismiss all
    const dismissAllBtn = $('#dismissAllBtn');
    if (dismissAllBtn) dismissAllBtn.addEventListener('click', dismissAllVisible);

    // Settings
    $('#settingsBtn').addEventListener('click', openSettings);
    $('#closeSettings').addEventListener('click', closeSettings);
    $('#cancelSettings').addEventListener('click', closeSettings);
    $('#saveSettings').addEventListener('click', saveSettingsToRepo);
    $('#settingsOverlay').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) closeSettings();
    });

    // Keyword tag input
    $('#kwField').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const val = e.target.value.trim();
        if (val && !state.settingsKeywords.includes(val)) {
          state.settingsKeywords.push(val);
          renderKeywordTags();
          e.target.value = '';
        }
      } else if (e.key === 'Backspace' && !e.target.value && state.settingsKeywords.length) {
        state.settingsKeywords.pop();
        renderKeywordTags();
      }
    });

    // Clear data buttons
    $('#clearDismissed').addEventListener('click', () => {
      state.dismissed = {}; saveState(); render(); toast('Dismissed list cleared', 'info');
    });
    $('#clearApplied').addEventListener('click', () => {
      state.applied = {}; saveState(); render(); toast('Applied list cleared', 'info');
    });
    $('#clearAll').addEventListener('click', () => {
      if (!confirm('Reset all local data (dismissed, applied, settings)?')) return;
      localStorage.removeItem(STORAGE_KEY);
      state.dismissed = {}; state.applied = {}; state.githubPat = '';
      render(); toast('All local data cleared', 'info');
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboard);

    // Keyboard help modal
    const kbdHint = $('#kbdHint');
    if (kbdHint) kbdHint.addEventListener('click', () => { $('#kbdOverlay').hidden = false; });
    const closeKbd = $('#closeKbd');
    if (closeKbd) closeKbd.addEventListener('click', () => { $('#kbdOverlay').hidden = true; });
    const kbdOverlay = $('#kbdOverlay');
    if (kbdOverlay) kbdOverlay.addEventListener('click', (e) => {
      if (e.target === e.currentTarget) kbdOverlay.hidden = true;
    });
  }

  // ── Keyboard Shortcuts ─────────────────────────────────────────────────────
  function handleKeyboard(e) {
    // Skip if typing in an input
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }

    // Close modals on Escape
    if (e.key === 'Escape') {
      if (!$('#settingsOverlay').hidden) { closeSettings(); return; }
      if (!$('#kbdOverlay').hidden) { $('#kbdOverlay').hidden = true; return; }
      if (state.searchQuery) {
        state.searchQuery = ''; $('#searchInput').value = ''; render(); return;
      }
    }

    if (e.key === '?') { $('#kbdOverlay').hidden = !$('#kbdOverlay').hidden; return; }
    if (e.key === '/') { e.preventDefault(); $('#searchInput').focus(); return; }
    if (e.key === 's' || e.key === 'S') { openSettings(); return; }
    if (e.key === '1') { state.activeTab = 'jobs'; render(); return; }
    if (e.key === '2') { state.activeTab = 'applied'; render(); return; }

    // Card navigation
    const cards = Array.from($$('.job-card'));
    if (!cards.length) return;

    if (e.key === 'j' || e.key === 'ArrowDown') {
      e.preventDefault();
      state.focusedIndex = Math.min(state.focusedIndex + 1, cards.length - 1);
      focusCard(cards);
    } else if (e.key === 'k' || e.key === 'ArrowUp') {
      e.preventDefault();
      state.focusedIndex = Math.max(state.focusedIndex - 1, 0);
      focusCard(cards);
    } else if ((e.key === 'o' || e.key === 'Enter') && state.focusedIndex >= 0) {
      const card = cards[state.focusedIndex];
      const link = card.querySelector('a');
      if (link) window.open(link.href, '_blank');
    } else if (e.key === 'd' && state.focusedIndex >= 0) {
      const card = cards[state.focusedIndex];
      const id = card.dataset.jobId;
      if (id) dismissJob(id);
    } else if (e.key === 'a' && state.focusedIndex >= 0) {
      const card = cards[state.focusedIndex];
      const id = card.dataset.jobId;
      const link = card.querySelector('a');
      if (id) applyJob(id, link ? link.href : '');
    }
  }

  function focusCard(cards) {
    cards.forEach(c => c.classList.remove('job-card--focused'));
    if (state.focusedIndex >= 0 && state.focusedIndex < cards.length) {
      const card = cards[state.focusedIndex];
      card.classList.add('job-card--focused');
      card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  // ── Settings Modal ─────────────────────────────────────────────────────────
  function openSettings() {
    populateSettings();
    $('#settingsOverlay').hidden = false;
  }

  function closeSettings() {
    $('#settingsOverlay').hidden = true;
  }

  function populateSettings() {
    // Load from current config (what the pipeline is using)
    state.settingsKeywords = [...(state.config.search_keywords || [])];
    renderKeywordTags();
    $('#minYoe').value = state.config.min_experience_years ?? 1;
    $('#maxYoe').value = state.config.max_experience_years ?? 3;
    $('#searchLocation').value = state.config.search_location ?? 'India';
    $('#enableDiscord').checked = state.config.enable_discord ?? false;
    $('#githubPat').value = state.githubPat || '';
  }

  function renderKeywordTags() {
    const container = $('#keywordTags');
    container.innerHTML = state.settingsKeywords.map((kw, i) => `
      <span class="tags-input__tag">
        ${escHtml(kw)}
        <button class="tags-input__tag-remove" data-kw-index="${i}" title="Remove">×</button>
      </span>
    `).join('');

    // Bind remove buttons
    container.querySelectorAll('.tags-input__tag-remove').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.kwIndex);
        state.settingsKeywords.splice(idx, 1);
        renderKeywordTags();
      });
    });
  }

  async function saveSettingsToRepo() {
    const pat = $('#githubPat').value.trim();
    if (!pat) {
      toast('Enter a GitHub PAT to save settings to the repository', 'error');
      return;
    }

    // Save PAT locally
    state.githubPat = pat;
    saveState();

    // Build config
    const newConfig = {
      search_keywords: [...state.settingsKeywords],
      search_location: $('#searchLocation').value.trim() || 'India',
      min_experience_years: parseInt($('#minYoe').value) || 1,
      max_experience_years: parseInt($('#maxYoe').value) || 3,
      enable_discord: $('#enableDiscord').checked,
    };

    const { owner, repo } = getRepoInfo();
    const path = 'user_config.json';
    const apiBase = `https://api.github.com/repos/${owner}/${repo}/contents/${path}`;
    const headers = {
      'Authorization': `token ${pat}`,
      'Accept': 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
    };

    try {
      // Get current file SHA (needed for update)
      let sha = null;
      try {
        const getResp = await fetch(apiBase, { headers });
        if (getResp.ok) {
          const fileData = await getResp.json();
          sha = fileData.sha;
        }
      } catch { /* file doesn't exist yet — we'll create it */ }

      // Commit the new config
      const content = btoa(unescape(encodeURIComponent(JSON.stringify(newConfig, null, 2) + '\n')));
      const body = {
        message: '🔧 Update config via dashboard',
        content: content,
      };
      if (sha) body.sha = sha;

      const putResp = await fetch(apiBase, {
        method: 'PUT',
        headers: headers,
        body: JSON.stringify(body),
      });

      if (putResp.ok) {
        toast('Settings saved! Changes take effect on next pipeline run (~30 min).', 'success');
        closeSettings();
      } else {
        const err = await putResp.json();
        toast(`Save failed: ${err.message || 'Unknown error'}`, 'error');
      }
    } catch (e) {
      toast(`Network error: ${e.message}`, 'error');
    }
  }

  function getRepoInfo() {
    // Derive from GitHub Pages URL: https://<owner>.github.io/<repo>/
    const host = window.location.hostname;
    if (host.endsWith('.github.io')) {
      const owner = host.split('.')[0];
      const pathParts = window.location.pathname.split('/').filter(Boolean);
      const repo = pathParts[0] || 'LinkedInJobs';
      return { owner, repo };
    }
    // Fallback for local dev or custom domain
    return { owner: 'VaranasiRahul', repo: 'LinkedInJobs' };
  }

  // ── Toast Notifications ────────────────────────────────────────────────────
  function toast(message, type = 'info') {
    const container = $('#toasts');
    const el = document.createElement('div');
    el.className = `toast toast--${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
      el.classList.add('toast--exiting');
      setTimeout(() => el.remove(), 300);
    }, 3500);
  }

  // ── Utilities ──────────────────────────────────────────────────────────────
  function escHtml(str) {
    const div = document.createElement('div');
    div.textContent = str || '';
    return div.innerHTML;
  }

  function timeAgo(dateStr) {
    if (!dateStr) return '';
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // ── Start ──────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', init);
})();
