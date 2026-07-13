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

// SEC-03: only ever hand off plain http(s) URLs. Job URLs are scraped from
// external boards, so `url` can be `javascript:`/`data:`/`file:` — none of
// which should reach window.open() or the backend's open-url handler.
function isSafeUrl(url) {
    return /^https?:\/\//i.test(String(url == null ? '' : url).trim());
}

async function openExternal(url) {
    if (!url) return;
    if (!isSafeUrl(url)) {
        console.warn('Refusing to open a non-http(s) URL', url);
        toast('This job has an unsafe or malformed link', 'warning');
        return;
    }
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

// SEC-03 (defense in depth): in BOTH shells, block navigation to any anchor
// whose scheme isn't safe. Render paths route job URLs through safeHref(), so
// this should never fire — it's the backstop for any href we missed, and it
// runs in the capture phase so it wins over the handlers below.
// NOTE: this is deliberately a *separate* listener from the desktop-shell
// interceptor below. That one is link *routing* (WKWebView ignores
// target="_blank"), not a security guard, and its `!IS_DESKTOP_SHELL`
// early-return must stay — removing it would make the browser-served SPA
// funnel every external link through the backend's open-url endpoint.
const UNSAFE_SCHEME_RE = /^\s*(javascript|data|vbscript|file):/i;
document.addEventListener('click', (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest('a[href]');
    if (!a) return;
    const raw = a.getAttribute('href') || '';
    if (UNSAFE_SCHEME_RE.test(raw)) {
        ev.preventDefault();
        ev.stopPropagation();
        console.warn('Blocked navigation to an unsafe URL scheme', raw);
        toast('This link was blocked: unsafe URL scheme', 'warning');
    }
}, true);

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

// Page titles for topbar (iOS-aligned IA: Inbox / Pipeline / Activity)
const PAGE_TITLES = {
    dashboard: 'Activity',
    jobs: 'Inbox',
    review: 'Pipeline',
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
    checkBackendPort();        // EOU-03
    startBrowserStatusPoll();  // REL-04

    // Live view refresh: reflect the saved preference in the toggle and start
    // the loop unless the user turned it off.
    const liveToggle = document.getElementById('cfg-live-refresh');
    if (liveToggle) liveToggle.checked = isLiveRefreshEnabled();
    if (isLiveRefreshEnabled()) startLiveRefresh();

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
    const hash = location.hash.replace('#', '') || 'jobs';
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
        case 'review': switchReviewView('shortlisted'); break;
        case 'settings': loadSettings(); break;
        case 'fit-breakdown':
            loadFitBreakdown();
            statsInterval = setInterval(() => { if (!document.hidden) loadFitBreakdown(); }, 5000);
            break;
    }
}

// ---- Live view refresh ----
// When on, the active tab silently re-loads its own data on a timer so
// background changes (new scrapes, completed applies, folder-sync imports from
// the phone) appear without switching tabs and back. UI-only preference stored
// in localStorage (like the theme), default on. It pauses while the window is
// hidden, while a dialog is open, or while the user is typing in a field, and
// never touches Settings (that would clobber a form mid-edit). Scroll position
// is captured and restored around the reload so the view doesn't jump.
let liveRefreshInterval = null;
const LIVE_REFRESH_MS = 8000;

function isLiveRefreshEnabled() {
    return localStorage.getItem('jobsmith_live_refresh') !== 'off';
}

async function refreshActiveView() {
    if (document.hidden) return;                                  // window not visible
    if (document.querySelector('.app-dialog-overlay')) return;    // mid confirm/prompt
    const ae = document.activeElement;
    if (ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) return;  // don't yank while typing

    const hash = location.hash.replace('#', '') || 'jobs';
    // Capture scroll of the window and of the Inbox list pane (its own scroller)
    // so re-rendering the list doesn't bounce the user to the top.
    const winScroller = document.scrollingElement || document.documentElement;
    const winY = winScroller ? winScroller.scrollTop : 0;
    const jobsList = document.getElementById('jobs-list');
    const jobsY = jobsList ? jobsList.scrollTop : 0;

    try {
        switch (hash) {
            case 'dashboard': await loadDashboard(); break;
            case 'jobs': await loadJobs(); break;
            case 'review':
                if (currentReviewView === 'shortlisted') await loadShortlisted();
                else if (currentReviewView === 'pending') await loadReviewQueue();
                else if (currentReviewView === 'submitted') await loadSubmittedApplications();
                else if (currentReviewView === 'in-progress') await loadInProgress();
                else await loadFailedApplications();
                break;
            case 'fit-breakdown': await loadFitBreakdown(); break;
            // 'settings' is intentionally omitted — refreshing would reset edits.
        }
    } catch (e) {
        // A failed refresh is silent; the next tick tries again.
    } finally {
        if (winScroller && winY) winScroller.scrollTop = winY;
        if (jobsList && jobsY) jobsList.scrollTop = jobsY;
    }
}

function startLiveRefresh() {
    if (liveRefreshInterval) return;
    liveRefreshInterval = setInterval(refreshActiveView, LIVE_REFRESH_MS);
}

function stopLiveRefresh() {
    if (liveRefreshInterval) { clearInterval(liveRefreshInterval); liveRefreshInterval = null; }
}

function setLiveRefresh(enabled) {
    localStorage.setItem('jobsmith_live_refresh', enabled ? 'on' : 'off');
    if (enabled) { startLiveRefresh(); toast('Live updates on', 'success'); }
    else { stopLiveRefresh(); toast('Live updates off', 'info'); }
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

// ==========================================================================
// Banners — persistent, page-level status messages (as opposed to toasts,
// which are transient). Rendered into #app-banners at the top of <main>.
//
//   showBanner('id', { message, tone: 'info'|'warn'|'error', dismissible,
//                      actions: [{ label, onClick }] })
//   hideBanner('id')
// ==========================================================================
const _bannerActions = {};
let _bannerActionSeq = 0;

function bannerContainer() {
    return document.getElementById('app-banners');
}

function showBanner(id, opts) {
    const host = bannerContainer();
    if (!host) return;
    const { message = '', tone = 'info', dismissible = true, actions = [] } = opts || {};

    let el = document.getElementById(`banner-${id}`);
    if (!el) {
        el = document.createElement('div');
        el.id = `banner-${id}`;
        host.appendChild(el);
    }
    el.className = `app-banner app-banner-${tone}`;
    el.setAttribute('role', tone === 'error' ? 'alert' : 'status');

    const actionHtml = actions.map((a) => {
        const key = `ba${++_bannerActionSeq}`;
        _bannerActions[key] = a.onClick;
        return `<button class="btn btn-secondary btn-sm" onclick="runBannerAction('${key}')">${escapeHtml(a.label)}</button>`;
    }).join('');
    const dismissHtml = dismissible
        ? `<button class="app-banner-close" aria-label="Dismiss" onclick="dismissBanner('${escapeHtml(id)}')">&times;</button>`
        : '';

    el.innerHTML = `
        <span class="app-banner-msg">${escapeHtml(message)}</span>
        <span class="app-banner-actions">${actionHtml}${dismissHtml}</span>`;
}

function hideBanner(id) {
    const el = document.getElementById(`banner-${id}`);
    if (el) el.remove();
}

// Dismissal is remembered for the session so a dismissed banner doesn't pop
// back on the next poll tick.
const _dismissedBanners = new Set();

function dismissBanner(id) {
    _dismissedBanners.add(id);
    hideBanner(id);
}

function runBannerAction(key) {
    const fn = _bannerActions[key];
    if (typeof fn === 'function') fn();
}

// ---- EOU-03: non-default port breaks the extension ----
// The extension hardcodes http://localhost:8888. When the desktop shell's
// pick_port falls back to a random port (8888 already taken), the extension
// silently talks to nothing. The frontend is the one place that knows the real
// port, so surface it. One-time per port: dismissing sticks across reloads.
function checkBackendPort() {
    const port = location.port;
    if (!port || port === '8888') return;
    const key = `jobsmith_port_banner_dismissed_${port}`;
    if (localStorage.getItem(key) === '1') return;
    showBanner('port-mismatch', {
        tone: 'warn',
        message: `Running on port ${port} — update the extension's backend URL (Settings → Integrations in the extension) to http://localhost:${port}.`,
        actions: [{
            label: 'Got it',
            onClick: () => { localStorage.setItem(key, '1'); dismissBanner('port-mismatch'); },
        }],
        dismissible: false,
    });
}

// ---- REL-04: Chromium install progress / failure ----
// The desktop shell downloads ~150 MB of Chromium on first launch. It used to
// do that behind a static splash spinner with no progress, no error and no
// retry. The backend now records the state and exposes it here.
// The endpoint may not exist (older backend, or the backend half of REL-04 not
// deployed): a 404 means "nothing to report" — stop polling and stay silent.
let _browserStatusTimer = null;
const BROWSER_STATUS_POLL_MS = 4000;

function stopBrowserStatusPoll() {
    if (_browserStatusTimer) clearTimeout(_browserStatusTimer);
    _browserStatusTimer = null;
}

async function pollBrowserStatus() {
    try {
        const resp = await fetch(`${API}/api/system/browser-status`);
        if (resp.status === 404) {
            // Endpoint not present — fail gracefully, never show the banner.
            hideBanner('browser-install');
            stopBrowserStatusPoll();
            return;
        }
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        renderBrowserStatusBanner(data);
        if (data.status === 'ready') {
            hideBanner('browser-install');
            stopBrowserStatusPoll();
            return;
        }
    } catch (e) {
        // Network/parse error: the reconnecting banner already covers a dead
        // backend. Keep polling but don't invent a browser-install failure.
        console.warn('browser-status poll failed', e);
    }
    _browserStatusTimer = setTimeout(pollBrowserStatus, BROWSER_STATUS_POLL_MS);
}

function renderBrowserStatusBanner(data) {
    if (!data || !data.status || data.status === 'ready') return;
    if (_dismissedBanners.has('browser-install')) return;

    if (data.status === 'installing') {
        showBanner('browser-install', {
            tone: 'info',
            message: 'Setting up the automation browser (one-time ~150 MB download). Auto-apply and Assist stay unavailable until this finishes.',
            dismissible: true,
        });
        return;
    }
    if (data.status === 'failed') {
        const detail = data.error ? ` ${data.error}` : '';
        showBanner('browser-install', {
            tone: 'error',
            message: `The automation browser failed to install.${detail} Auto-apply and Assist won't work until it succeeds.`,
            dismissible: true,
            actions: [{ label: 'Retry', onClick: retryBrowserInstall }],
        });
    }
}

async function retryBrowserInstall() {
    try {
        const resp = await fetch(`${API}/api/system/browser-install`, { method: 'POST' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        toast('Reinstalling the automation browser…', 'info');
        // A retry re-arms the banner even if it was dismissed, and restarts the
        // poll so the user sees the outcome.
        _dismissedBanners.delete('browser-install');
        showBanner('browser-install', {
            tone: 'info',
            message: 'Setting up the automation browser (one-time ~150 MB download). Auto-apply and Assist stay unavailable until this finishes.',
            dismissible: true,
        });
        stopBrowserStatusPoll();
        _browserStatusTimer = setTimeout(pollBrowserStatus, BROWSER_STATUS_POLL_MS);
    } catch (e) {
        toast('Failed to start the browser install', 'error');
    }
}

function startBrowserStatusPoll() {
    stopBrowserStatusPoll();
    pollBrowserStatus();
}

// ---- Sidebar Toggle (mobile) ----
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.classList.toggle('open');
}

// ---- Theme Toggle (dark / light) ----
// UX-04: three states, not two. `data-theme` absent means "follow the OS"
// (style.css has a `prefers-color-scheme: light` block gated on
// `body:not([data-theme])`); an explicit choice pins `data-theme` to
// "light"/"dark" and always wins. Previously "dark" was represented by
// *removing* the attribute, which made "explicitly dark" indistinguishable
// from "no preference" and left no way to honour the OS setting.
function systemPrefersLight() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches);
}

function currentTheme() {
    const explicit = document.body.getAttribute('data-theme');
    if (explicit === 'light' || explicit === 'dark') return explicit;
    return systemPrefersLight() ? 'light' : 'dark';
}

function updateThemeToggleIcon() {
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = currentTheme() === 'light' ? '☀' : '☾';
}

(function initTheme() {
    const saved = localStorage.getItem('jobsmith_theme');
    if (saved === 'light' || saved === 'dark') {
        document.body.setAttribute('data-theme', saved);
    }
    updateThemeToggleIcon();
    // Track OS changes while the user has no explicit preference.
    if (window.matchMedia) {
        const mq = window.matchMedia('(prefers-color-scheme: light)');
        const onChange = () => { if (!document.body.hasAttribute('data-theme')) updateThemeToggleIcon(); };
        if (mq.addEventListener) mq.addEventListener('change', onChange);
        else if (mq.addListener) mq.addListener(onChange);
    }
})();

function toggleTheme() {
    const next = currentTheme() === 'light' ? 'dark' : 'light';
    document.body.setAttribute('data-theme', next);
    localStorage.setItem('jobsmith_theme', next);
    updateThemeToggleIcon();
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

// REL-14: the poll used to be a fixed 3s setInterval whose body did
// `.then(r => r.json())` with no `resp.ok` check and a bare `catch {}`. A dead
// or erroring backend was invisible: an HTML error page threw on json(), the
// catch swallowed it, and the loop kept hammering every 3s forever.
// Now: check resp.ok, back off exponentially (3 → 6 → 12 → 24 → 30s cap) on
// consecutive failures, and surface a "Reconnecting…" banner once we've failed
// enough times to be sure it isn't a blip. Any success resets both.
const NOTIF_POLL_BASE_MS = 3000;
const NOTIF_POLL_MAX_MS = 30000;
const NOTIF_FAILURES_BEFORE_BANNER = 2;
let _notifFailures = 0;
let _notifBannerShown = false;

function notifPollDelay() {
    if (_notifFailures === 0) return NOTIF_POLL_BASE_MS;
    // 3 → 6 → 12 → 24 → 30 (cap)
    return Math.min(NOTIF_POLL_BASE_MS * Math.pow(2, _notifFailures), NOTIF_POLL_MAX_MS);
}

function startNotificationPoll() {
    if (notificationPollInterval) return;
    // setTimeout chain (not setInterval) so the delay can change per tick.
    const tick = async () => {
        await pollNotifications();
        notificationPollInterval = setTimeout(tick, notifPollDelay());
    };
    notificationPollInterval = setTimeout(tick, NOTIF_POLL_BASE_MS);
}

function stopNotificationPoll() {
    if (notificationPollInterval) clearTimeout(notificationPollInterval);
    notificationPollInterval = null;
}

function onNotifPollFailure(err) {
    _notifFailures++;
    if (_notifFailures >= NOTIF_FAILURES_BEFORE_BANNER && !_notifBannerShown) {
        _notifBannerShown = true;
        showBanner('reconnecting', {
            tone: 'warn',
            message: 'Reconnecting… Jobsmith can’t reach the backend. Live updates are paused.',
            dismissible: false,
        });
    }
    console.warn(`Notification poll failed (${_notifFailures}x), retrying in ${notifPollDelay() / 1000}s`, err);
}

function onNotifPollSuccess() {
    if (_notifFailures > 0 || _notifBannerShown) {
        _notifFailures = 0;
        _notifBannerShown = false;
        hideBanner('reconnecting');
    }
}

async function pollNotifications() {
    if (_notifPollActive) return;
    _notifPollActive = true;
    try {
        const resp = await fetch(`${API}/api/notifications?since_id=${lastNotificationId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        onNotifPollSuccess();
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
                const hash = location.hash.replace('#', '') || 'jobs';
                if (hash === 'dashboard') loadDashboard();
                if (hash === 'jobs') loadJobs();
            }
            if (n.type === 'tailor') {
                const hash = location.hash.replace('#', '') || 'jobs';
                if (hash === 'dashboard') loadDashboard();
                if (hash === 'jobs') loadJobs();
                if (hash === 'review') {
                    if (currentReviewView === 'pending') loadReviewQueue();
                }
            }
            if (n.type === 'apply') {
                const hash = location.hash.replace('#', '') || 'jobs';
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
        const _hash = location.hash.replace('#', '') || 'jobs';
        if (_hash === 'review' && currentReviewView === 'in-progress' && _inProgressPollTick % 2 === 0) {
            loadInProgress();
        }
    } catch (e) {
        onNotifPollFailure(e);
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
        if (resp.status === 401) {
            // The backend only challenges callers that aren't on its own machine
            // (LAN / Docker — where the container sees us as the bridge gateway,
            // not 127.0.0.1). Trade the token for a session cookie, then retry.
            await promptForToken();
            return api(path, options);
        }
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

// ---- Token gate (only ever shown to off-machine callers) ----
let _tokenPromptOpen = null;

function promptForToken() {
    // Collapse concurrent 401s (the dashboard fires several calls on load) into
    // one prompt, so the user isn't asked N times.
    if (_tokenPromptOpen) return _tokenPromptOpen;

    _tokenPromptOpen = new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'token-gate';
        overlay.innerHTML = `
            <div class="token-gate-card">
                <h2>Enter your Jobsmith token</h2>
                <p>
                    You're reaching Jobsmith from another machine, so it needs the
                    access token. Find it in <code>data/.extension_token</code>
                    (or run <code>docker compose exec jobsmith cat data/.extension_token</code>).
                </p>
                <input type="password" id="token-gate-input" placeholder="Paste token" autocomplete="off">
                <div class="token-gate-error" id="token-gate-error"></div>
                <button id="token-gate-submit">Unlock</button>
            </div>`;
        document.body.appendChild(overlay);

        const input = overlay.querySelector('#token-gate-input');
        const errorEl = overlay.querySelector('#token-gate-error');
        input.focus();

        async function submit() {
            const token = input.value.trim();
            if (!token) return;
            try {
                const resp = await fetch(`${API}/api/auth/login`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token }),
                });
                if (!resp.ok) {
                    errorEl.textContent = 'That token was rejected. Try again.';
                    input.select();
                    return;
                }
                overlay.remove();
                _tokenPromptOpen = null;
                resolve();
            } catch (e) {
                errorEl.textContent = 'Could not reach the backend.';
            }
        }

        overlay.querySelector('#token-gate-submit').addEventListener('click', submit);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    });
    return _tokenPromptOpen;
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
        // The fallback list is already rendered and is usable, so this is a
        // warning, not a dead end — but say so instead of only console.error'ing.
        console.warn('Failed to load sources from API, using defaults', e);
        const note = document.createElement('p');
        note.className = 'hint source-fallback-note';
        note.textContent = 'Showing the default source list — the backend didn’t respond.';
        container.appendChild(note);
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
