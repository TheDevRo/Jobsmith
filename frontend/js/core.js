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
        case 'fit-breakdown': loadFitBreakdown(); break;
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

// ---- Toast Notifications ----
function toast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 5000);
}

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
    linkedin: 'LinkedIn',
    arbeitnow: 'Arbeitnow',
    usajobs: 'USAJobs',
    indeed: 'Indeed',
};

async function loadSources() {
    const container = document.getElementById('source-checkboxes');
    const fallbackSources = ['linkedin', 'adzuna', 'remoteok', 'weworkremotely', 'greenhouse', 'arbeitnow', 'usajobs', 'indeed'];
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
