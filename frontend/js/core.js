// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ==========================================================================
// Jobsmith — Frontend Application
// ==========================================================================

const API = '';  // Same origin

// ---- State ----
let currentJobsPage = 0;
const JOBS_PER_PAGE = 30;
let searchDebounce = null;
let statsInterval = null;
let notificationPollInterval = null;
let lastNotificationId = 0;
let _notifPollActive = false;
let _inProgressPollTick = 0;  // incremented each notification poll; loadInProgress every 2nd tick (~6s)
let selectedJobId = null;
window._currentJobs = {};
window._autoApplyEnabled = false;

function applyAutoApplyVisibility(enabled) {
    window._autoApplyEnabled = !!enabled;
    document.body.classList.toggle('auto-apply-disabled', !enabled);
}

// ---- Desktop shell link handling ----
// The Tauri webview (WKWebView) silently ignores target="_blank" anchors and
// window.open(). The desktop window ships a "JobsmithDesktop" UA token; when
// present, route external opens through the backend, which hands the URL to
// the system browser.
const IS_DESKTOP_SHELL = navigator.userAgent.includes('JobsmithDesktop');

async function openExternal(url) {
    if (!url) return;
    if (IS_DESKTOP_SHELL) {
        try {
            await api('/api/system/open-url', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url }),
            });
            return;
        } catch (e) {
            console.error('Backend open-url failed, falling back to window.open', e);
        }
    }
    window.open(url, '_blank', 'noopener');
}

// In the desktop shell, intercept every target="_blank" anchor (including ones
// rendered later) and open via the backend instead of the dead default.
document.addEventListener('click', (ev) => {
    if (!IS_DESKTOP_SHELL) return;
    const a = ev.target && ev.target.closest && ev.target.closest('a[target="_blank"]');
    if (!a || !a.href || !/^https?:/i.test(a.href)) return;
    ev.preventDefault();
    openExternal(a.href);
});

// Notification center state
let _notificationItems = [];
const MAX_NOTIFICATION_ITEMS = 30;

// Page titles for topbar
const PAGE_TITLES = {
    dashboard: 'Dashboard',
    jobs: 'Job Feed',
    review: 'Review Queue',
    settings: 'Settings',
    'fit-breakdown': 'Fit Score Breakdown',
};

// ---- Init ----
document.addEventListener('DOMContentLoaded', () => {
    applyAutoApplyVisibility(false);
    setupTabs();
    handleHash();
    window.addEventListener('hashchange', handleHash);
    requestNotificationPermission();
    startNotificationPoll();

    // Close notification dropdown when clicking outside
    document.addEventListener('click', (e) => {
        const dropdown = document.getElementById('notification-dropdown');
        const bell = document.getElementById('notification-bell');
        if (dropdown && dropdown.style.display !== 'none' &&
            !dropdown.contains(e.target) && !bell.contains(e.target)) {
            dropdown.style.display = 'none';
        }
    });
});

// ---- Tab Routing ----
function setupTabs() {
    document.querySelectorAll('nav .tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            // Let the hash change handle it
        });
    });
}

function handleHash() {
    const hash = location.hash.replace('#', '') || 'dashboard';
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('nav .tab').forEach(el => el.classList.remove('active'));

    const section = document.getElementById(hash);
    // Map sub-pages to their parent nav tab so the sidebar always shows an active item
    const NAV_PARENT = { 'fit-breakdown': 'dashboard' };
    const navHash = NAV_PARENT[hash] || hash;
    const tab = document.querySelector(`nav .tab[data-tab="${navHash}"]`);
    if (section) section.classList.add('active');
    if (tab) tab.classList.add('active');

    // Update page title
    const titleEl = document.getElementById('page-title');
    if (titleEl) titleEl.textContent = PAGE_TITLES[hash] || hash;

    // Close sidebar on mobile after navigation
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.classList.remove('open');

    // Clear previous interval
    if (statsInterval) { clearInterval(statsInterval); statsInterval = null; }

    // Load tab data
    switch (hash) {
        case 'dashboard': loadDashboard(); loadSources(); statsInterval = setInterval(loadDashboard, 30000); break;
        case 'jobs': loadJobs(); break;
        case 'review': switchReviewView('pending'); break;
        case 'settings': loadSettings(); break;
        case 'fit-breakdown':
            loadFitBreakdown();
            statsInterval = setInterval(() => { if (!document.hidden) loadFitBreakdown(); }, 5000);
            break;
    }
}

// ---- Dashboard stat card navigation helpers ----
function goToAppliedToday() {
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    document.getElementById('filter-status').value = 'applied';
    document.getElementById('filter-date-from').value = today;
    document.getElementById('filter-date-to').value = today;
    location.hash = 'jobs';
}

function goToSubmittedView() {
    location.hash = 'review';
    setTimeout(() => switchReviewView('submitted'), 0);
}

function goToScoreFilter(minScore, maxScore, unscoredOnly) {
    document.getElementById('filter-search').value = '';
    document.getElementById('filter-location').value = '';
    document.getElementById('filter-company').value = '';
    document.getElementById('filter-source').value = '';
    document.getElementById('filter-status').value = '';
    document.getElementById('filter-sort').value = 'fit_score-desc';
    document.getElementById('filter-remote').checked = false;
    document.getElementById('filter-easy-apply').checked = false;
    document.getElementById('filter-salary').value = 0;
    document.getElementById('salary-val').textContent = '0';
    document.getElementById('filter-date-from').value = '';
    document.getElementById('filter-date-to').value = '';
    document.getElementById('filter-max-score').value = maxScore !== null ? maxScore : '';
    document.getElementById('filter-unscored-only').value = unscoredOnly ? '1' : '';
    const scoreVal = unscoredOnly ? 0 : (minScore !== null ? minScore : 0);
    document.getElementById('filter-score').value = scoreVal;
    document.getElementById('score-val').textContent = scoreVal;
    currentJobsPage = 0;
    location.hash = 'jobs';
}

function goToStatusFilter(status) {
    document.getElementById('filter-search').value = '';
    document.getElementById('filter-location').value = '';
    document.getElementById('filter-company').value = '';
    document.getElementById('filter-source').value = '';
    document.getElementById('filter-status').value = status;
    document.getElementById('filter-sort').value = 'date_discovered-desc';
    document.getElementById('filter-remote').checked = false;
    document.getElementById('filter-easy-apply').checked = false;
    document.getElementById('filter-score').value = 0;
    document.getElementById('score-val').textContent = '0';
    document.getElementById('filter-salary').value = 0;
    document.getElementById('salary-val').textContent = '0';
    document.getElementById('filter-date-from').value = '';
    document.getElementById('filter-date-to').value = '';
    document.getElementById('filter-max-score').value = '';
    document.getElementById('filter-unscored-only').value = '';
    currentJobsPage = 0;
    location.hash = 'jobs';
}

// ---- Sidebar Toggle (mobile) ----
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.classList.toggle('open');
}

// ---- Theme Toggle (dark / light) ----
(function initTheme() {
    const saved = localStorage.getItem('jobsmith_theme') || 'dark';
    if (saved === 'light') {
        document.body.setAttribute('data-theme', 'light');
        const btn = document.getElementById('theme-toggle');
        if (btn) btn.textContent = '☀';
    }
})();

function toggleTheme() {
    const isLight = document.body.getAttribute('data-theme') === 'light';
    const btn = document.getElementById('theme-toggle');
    if (isLight) {
        document.body.removeAttribute('data-theme');
        localStorage.setItem('jobsmith_theme', 'dark');
        if (btn) btn.textContent = '☾';
    } else {
        document.body.setAttribute('data-theme', 'light');
        localStorage.setItem('jobsmith_theme', 'light');
        if (btn) btn.textContent = '☀';
    }
}

// ---- Forge/heat identity helpers (ported from ios-standalone Theme.swift) ----
// Two-segment steel→amber→ember lerp mirroring Theme.heat(for:).
function heatColor(score) {
    const t = Math.min(Math.max((Number(score) || 0) / 100, 0), 1);
    const lerp = (a, b, k) => Math.round(a + (b - a) * k);
    const mix = (c1, c2, k) => `rgb(${lerp(c1[0], c2[0], k)}, ${lerp(c1[1], c2[1], k)}, ${lerp(c1[2], c2[2], k)})`;
    const steel = [0x6b, 0x7a, 0x94], amber = [0xe8, 0xa1, 0x3c], emberDeep = [0xd9, 0x54, 0x1e];
    return t < 0.6 ? mix(steel, amber, t / 0.6) : mix(amber, emberDeep, (t - 0.6) / 0.4);
}

const FLAME_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2S6 7 6 13a6 6 0 0 0 12 0c0-2-1-3.5-2-5 0 1.5-1 2.5-2 2.5 0-3-2-6.5-2-8.5z"/></svg>';

// Fit score as a heat chip. Null/undefined → outlined empty state.
function renderHeatChip(score) {
    if (score === null || score === undefined || score === '' || isNaN(Number(score))) {
        return `<span class="heat-chip heat-empty">${FLAME_SVG}—</span>`;
    }
    const s = Math.round(Number(score));
    const c = heatColor(s);
    return `<span class="heat-chip" style="background:linear-gradient(135deg, ${c}, ${heatColor(s + 12)})">${FLAME_SVG}${s}</span>`;
}

// Fit score as a detail-pane ring (score + "FIT").
function renderHeatRing(score) {
    const s = Math.round(Number(score) || 0);
    return `<div class="heat-ring" style="--heat:${heatColor(s)};--pct:${Math.min(Math.max(s, 0), 100)}">`
        + `<div style="display:grid;place-items:center"><span class="heat-ring-score">${s}</span>`
        + `<span class="heat-ring-label">FIT</span></div></div>`;
}

// ---- Toast Notifications ----
function toast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 5000);
}

// ---- Dialogs ----
// The desktop shell's webview doesn't implement window.confirm/prompt/alert
// (confirm returns false, prompt returns null, silently), so all
// confirmations go through this in-page modal instead.
function _appDialog({ message, input = null, cancelable = true }) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'app-dialog-overlay';
        const box = document.createElement('div');
        box.className = 'app-dialog';

        const msg = document.createElement('div');
        msg.className = 'app-dialog-message';
        msg.textContent = message;
        box.appendChild(msg);

        let field = null;
        if (input !== null) {
            field = document.createElement('input');
            field.type = 'text';
            field.value = input;
            field.className = 'app-dialog-input';
            box.appendChild(field);
        }

        const cancelValue = input !== null ? null : false;
        const done = (val) => {
            document.removeEventListener('keydown', onKey, true);
            overlay.remove();
            resolve(val);
        };
        const onKey = (e) => {
            if (e.key === 'Escape' && cancelable) { e.preventDefault(); done(cancelValue); }
            else if (e.key === 'Enter') { e.preventDefault(); done(input !== null ? field.value : true); }
        };

        const row = document.createElement('div');
        row.className = 'app-dialog-buttons';
        if (cancelable) {
            const cancelBtn = document.createElement('button');
            cancelBtn.className = 'btn btn-secondary btn-sm';
            cancelBtn.textContent = 'Cancel';
            cancelBtn.onclick = () => done(cancelValue);
            row.appendChild(cancelBtn);
        }
        const okBtn = document.createElement('button');
        okBtn.className = 'btn btn-primary btn-sm';
        okBtn.textContent = 'OK';
        okBtn.onclick = () => done(input !== null ? field.value : true);
        row.appendChild(okBtn);
        box.appendChild(row);

        overlay.appendChild(box);
        document.body.appendChild(overlay);
        document.addEventListener('keydown', onKey, true);
        (field || okBtn).focus();
        if (field) field.select();
    });
}

function appConfirm(message) { return _appDialog({ message }); }
function appPrompt(message, defaultValue = '') { return _appDialog({ message, input: defaultValue }); }
function appAlert(message) { return _appDialog({ message, cancelable: false }); }

// ---- Browser & Push Notifications ----
function requestNotificationPermission() {
    if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission();
    }
}

function sendBrowserNotification(title, body, tag) {
    if ('Notification' in window && Notification.permission === 'granted') {
        const n = new Notification(title, {
            body: body,
            tag: tag || 'jobsmith',
            icon: '/favicon.ico',
        });
        n.onclick = () => { window.focus(); n.close(); };
        setTimeout(() => n.close(), 8000);
    }
}

function startNotificationPoll() {
    if (notificationPollInterval) return;
    notificationPollInterval = setInterval(pollNotifications, 3000);
}

async function pollNotifications() {
    if (_notifPollActive) return;
    _notifPollActive = true;
    try {
        const data = await fetch(`${API}/api/notifications?since_id=${lastNotificationId}`).then(r => r.json());
        if (!data.notifications || data.notifications.length === 0) return;

        for (const n of data.notifications) {
            lastNotificationId = Math.max(lastNotificationId, n.id);

            // In-app toast
            toast(`${n.title}: ${n.message}`, n.status);

            // Browser notification
            sendBrowserNotification(n.title, n.message, `jobsmith-${n.id}`);

            // Add to notification center
            _notificationItems.unshift({
                id: n.id,
                title: n.title,
                message: n.message,
                status: n.status,
                timestamp: n.timestamp,
            });
            if (_notificationItems.length > MAX_NOTIFICATION_ITEMS) {
                _notificationItems = _notificationItems.slice(0, MAX_NOTIFICATION_ITEMS);
            }
            updateNotificationBadge();

            // Auto-refresh relevant views
            if (n.type === 'fetch') {
                const hash = location.hash.replace('#', '') || 'dashboard';
                if (hash === 'dashboard') loadDashboard();
                if (hash === 'jobs') loadJobs();
            }
            if (n.type === 'tailor') {
                const hash = location.hash.replace('#', '') || 'dashboard';
                if (hash === 'dashboard') loadDashboard();
                if (hash === 'jobs') loadJobs();
                if (hash === 'review') {
                    if (currentReviewView === 'pending') loadReviewQueue();
                }
            }
            if (n.type === 'apply') {
                const hash = location.hash.replace('#', '') || 'dashboard';
                if (hash === 'dashboard') loadDashboard();
                if (hash === 'review') {
                    if (currentReviewView === 'submitted') loadSubmittedApplications();
                    else if (currentReviewView === 'failed') loadFailedApplications();
                    else loadReviewQueue();
                }
            }
        }
        // Refresh In Progress tab on every 2nd poll tick (~6s) or immediately on apply events
        _inProgressPollTick++;
        const _hash = location.hash.replace('#', '') || 'dashboard';
        if (_hash === 'review' && currentReviewView === 'in-progress' && _inProgressPollTick % 2 === 0) {
            loadInProgress();
        }
    } catch (e) {
        // Silently ignore poll errors
    } finally {
        _notifPollActive = false;
    }
}

// ---- Notification Center ----
function toggleNotificationCenter() {
    const dropdown = document.getElementById('notification-dropdown');
    if (!dropdown) return;
    const visible = dropdown.style.display !== 'none';
    dropdown.style.display = visible ? 'none' : '';
    if (!visible) renderNotificationCenter();
}

function updateNotificationBadge() {
    const badge = document.getElementById('notification-badge');
    if (!badge) return;
    const count = _notificationItems.length;
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.style.display = '';
    } else {
        badge.style.display = 'none';
    }
}

function renderNotificationCenter() {
    const list = document.getElementById('notification-list');
    if (!list) return;

    if (_notificationItems.length === 0) {
        list.innerHTML = '<p class="placeholder" style="padding:20px;font-size:13px">No new notifications</p>';
        return;
    }

    list.innerHTML = _notificationItems.map(n => {
        const statusColor = { success: 'var(--accent-green)', error: 'var(--accent-red)', info: 'var(--accent-blue)' }[n.status] || 'var(--text-secondary)';
        return `
            <div class="notification-item">
                <div class="notification-item-title" style="color:${statusColor}">${escapeHtml(n.title)}</div>
                <div class="notification-item-message">${escapeHtml(n.message)}</div>
                <div class="notification-item-time">${timeAgo(new Date(n.timestamp * 1000).toISOString())}</div>
            </div>
        `;
    }).join('');
}

function clearNotifications() {
    _notificationItems = [];
    updateNotificationBadge();
    renderNotificationCenter();
}

// ---- API Helpers ----
async function api(path, options = {}) {
    try {
        const resp = await fetch(`${API}${path}`, {
            headers: { 'Content-Type': 'application/json', ...options.headers },
            ...options,
        });
        if (!resp.ok) {
            const err = await resp.text();
            throw new Error(err || `HTTP ${resp.status}`);
        }
        return await resp.json();
    } catch (e) {
        console.error('API error:', e);
        throw e;
    }
}

// ---- Sources ----
const SOURCE_LABELS = {
    remoteok: 'RemoteOK',
    weworkremotely: 'WeWorkRemotely',
    adzuna: 'Adzuna',
    greenhouse: 'Greenhouse',
    lever: 'Lever',
    ashby: 'Ashby',
    workable: 'Workable',
    recruitee: 'Recruitee',
    linkedin: 'LinkedIn',
    arbeitnow: 'Arbeitnow',
    usajobs: 'USAJobs',
    indeed: 'Indeed',
};

async function loadSources() {
    const container = document.getElementById('source-checkboxes');
    const fallbackSources = ['linkedin', 'adzuna', 'remoteok', 'weworkremotely', 'greenhouse', 'ashby', 'workable', 'recruitee', 'arbeitnow', 'usajobs', 'indeed'];
    function renderSources(sources) {
        container.innerHTML = sources.map(s => `
            <label><input type="checkbox" value="${s}" checked> ${SOURCE_LABELS[s] || s}</label>
        `).join('');
    }
    renderSources(fallbackSources);
    try {
        const data = await api('/api/sources');
        if (data.sources) renderSources(data.sources);
    } catch (e) {
        console.error('Failed to load sources from API, using defaults', e);
    }
}

function getSelectedSources() {
    const boxes = document.querySelectorAll('#source-checkboxes input[type="checkbox"]');
    const selected = [];
    boxes.forEach(cb => { if (cb.checked) selected.push(cb.value); });
    return selected;
}

function toggleAllSources(checked) {
    document.querySelectorAll('#source-checkboxes input[type="checkbox"]')
        .forEach(cb => { cb.checked = checked; });
}


// Split a comma-separated list, keeping commas inside brackets — a skill
// like "VPNs (TorGuard, Tailscale, ZeroTier)" is one entry, not three.
function splitCsvSmart(v) {
    const out = [];
    let depth = 0, cur = '';
    for (const ch of (v || '')) {
        if ('([{'.includes(ch)) depth++;
        else if (')]}'.includes(ch)) depth = Math.max(0, depth - 1);
        if (ch === ',' && depth === 0) { out.push(cur.trim()); cur = ''; }
        else cur += ch;
    }
    out.push(cur.trim());
    return out.filter(Boolean);
}
