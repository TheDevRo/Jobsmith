// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ---- Dashboard ----
async function loadDashboard() {
    // Sync the Score Jobs salary-pull hint with the current toggle state.
    api('/api/settings/salary-estimator-auto-ingest')
        .then(r => _applySalaryAutoIngest(!!r.auto_on_ingest))
        .catch(() => { /* non-fatal */ });

    try {
        const [stats, activity] = await Promise.all([
            api('/api/stats'),
            api('/api/activity?limit=20'),
        ]);

        document.getElementById('stat-total').textContent = stats.total_jobs || 0;
        document.getElementById('stat-pending').textContent = stats.pending_review || 0;
        document.getElementById('stat-today').textContent = stats.applied_today || 0;
        document.getElementById('stat-applied').textContent = stats.total_applied || 0;
        document.getElementById('stat-score').textContent = stats.avg_fit_score || 0;

        // Show paused indicator on Pending Review tile
        const pausedCount = stats.paused || 0;
        let pausedEl = document.getElementById('stat-paused-indicator');
        if (pausedCount > 0) {
            if (!pausedEl) {
                pausedEl = document.createElement('div');
                pausedEl.id = 'stat-paused-indicator';
                pausedEl.style.cssText = 'margin-top:6px;display:flex;align-items:center;gap:6px';
                document.getElementById('stat-pending').parentElement.appendChild(pausedEl);
            }
            pausedEl.innerHTML = `<span class="pill pill-paused" style="font-size:11px">${pausedCount} paused</span><button class="btn btn-primary" style="font-size:10px;padding:2px 8px" onclick="event.stopPropagation();location.hash='review'">Resume</button>`;
        } else if (pausedEl) {
            pausedEl.remove();
        }

        const feed = document.getElementById('activity-feed');
        if (activity.length === 0) {
            feed.innerHTML = '<p class="placeholder">No activity yet. Fetch some jobs to get started!</p>';
        } else {
            feed.innerHTML = activity.map(a => `
                <div class="activity-item">
                    <span class="activity-action">${escapeHtml(a.action)}</span>
                    <span class="activity-details">${escapeHtml(a.details || '')}</span>
                    <span class="activity-time">${timeAgo(a.timestamp)}</span>
                </div>
            `).join('');
        }
    } catch (e) {
        toast('Failed to load dashboard', 'error');
    }

    // Outcomes panel — non-fatal if it fails
    try {
        renderOutcomesPanel(await api('/api/analytics/outcomes'));
    } catch (e) {
        renderError('outcomes-panel', 'Failed to load outcome analytics.', loadDashboard);
    }

    // Needs-attention queue — non-fatal if it fails
    try {
        renderDuePanel(await api('/api/applications/due'));
    } catch (e) {
        /* the card just stays hidden */
    }

    // Today's shortlist — non-fatal if it fails
    try {
        renderDigestPanel(await api('/api/digest?limit=5'));
    } catch (e) {
        /* the card just stays hidden */
    }
}

// ---- Apply Today ----
// Ranked by fit, freshness, salary and apply-effort — and by how often each
// source has actually replied to you. Every pick shows why it's here, because a
// ranking you can't interrogate is a ranking you won't trust.
function renderDigestPanel(data) {
    const card = document.getElementById('digest-card');
    const panel = document.getElementById('digest-panel');
    if (!card || !panel) return;

    const jobs = (data && data.jobs) || [];
    if (jobs.length === 0) {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';
    panel.innerHTML = jobs.map(job => {
        const reasons = digestReasons(job, data.conversion_by_source || {});
        return `
            <div class="outcome-bar-row" style="cursor:pointer" onclick="showJobFromDigest('${job.id}')">
                <span class="outcome-bar-label">
                    ${escapeHtml(job.title)} · ${escapeHtml(job.company || '')}
                    <span style="color:var(--text-muted)">${reasons}</span>
                </span>
                <span class="outcome-bar-value">${Math.round(job.fit_score)} fit</span>
            </div>`;
    }).join('');
}

function digestReasons(job, conversionBySource) {
    const bits = [];
    if (job.is_easy_apply) bits.push('easy apply');
    if (job.components && job.components.freshness > 0.9) bits.push('posted just now');
    const rate = conversionBySource[job.source];
    if (rate !== undefined && rate > 0) {
        bits.push(`${escapeHtml(job.source)} replies ${Math.round(rate * 100)}% of the time`);
    }
    return bits.length ? `— ${bits.join(', ')}` : '';
}

function showJobFromDigest(jobId) {
    window.location.hash = '#jobs';
    // The Jobs view owns selection; it picks this up once it has rendered.
    window._pendingJobSelection = jobId;
}

// ---- Needs Attention ----
// A pull queue, deliberately: backend notifications live in an in-memory deque
// the frontend polls, so they only fire while the app is open — useless for a
// "you applied 7 days ago" nudge. The phone, which has real scheduled
// notifications, is the push surface. See PIPELINE_INTELLIGENCE_PLAN.md.
function renderDuePanel(data) {
    const card = document.getElementById('due-card');
    const panel = document.getElementById('due-panel');
    if (!card || !panel) return;

    const groups = [
        ['follow_up', 'Follow up', a => `applied ${daysAgo(a.applied_at)}, no response yet`],
        ['interview', 'Interview coming up', a => `on ${shortDate(a.interview_at)}`],
        ['silent', 'Going quiet', a => `applied ${daysAgo(a.applied_at)} — still awaiting a reply`],
    ].filter(([key]) => (data[key] || []).length > 0);

    if (groups.length === 0) {
        card.style.display = 'none';
        return;
    }
    card.style.display = '';
    panel.innerHTML = groups.map(([key, title, detail]) => `
        <div class="outcome-breakdown">
            <h4>${escapeHtml(title)}</h4>
            ${data[key].map(a => `
                <div class="outcome-bar-row">
                    <span class="outcome-bar-label">${escapeHtml(a.title)} · ${escapeHtml(a.company || '')}</span>
                    <span class="outcome-bar-value">${escapeHtml(detail(a))}</span>
                </div>`).join('')}
        </div>`).join('');
}

function daysAgo(iso) {
    const then = Date.parse(iso);
    if (!then) return 'recently';
    const days = Math.max(0, Math.floor((Date.now() - then) / 86400000));
    return days === 0 ? 'today' : `${days} day${days === 1 ? '' : 's'} ago`;
}

function shortDate(iso) {
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ---- Outcomes panel ----
function _outcomeBarRows(title, rows) {
    if (!rows || rows.length === 0) return '';
    return `
        <div class="outcome-breakdown">
            <h4>${escapeHtml(title)}</h4>
            ${rows.map(r => {
                const pct = Math.max(0, Math.min(100, Number(r.rate) || 0));
                return `
                <div class="outcome-bar-row" title="${r.responded} of ${r.total} received a response">
                    <span class="outcome-bar-label">${escapeHtml(String(r.key))}</span>
                    <div class="outcome-bar-track"><div class="outcome-bar-fill" style="width:${pct}%"></div></div>
                    <span class="outcome-bar-value">${r.responded}/${r.total} &middot; ${pct}%</span>
                </div>`;
            }).join('')}
        </div>`;
}

const _HOP_LABELS = {
    'applied>screening': 'Applied → Screening',
    'screening>interview': 'Screening → Interview',
    'interview>offer': 'Interview → Offer',
};

function _stageDurations(hops) {
    const sampled = (hops || []).filter(h => h.samples > 0);
    if (sampled.length === 0) return '';
    return `
        <div class="outcome-breakdown">
            <h4>Typical Time Between Stages</h4>
            ${sampled.map(h => `
                <div class="outcome-bar-row" title="Median across ${h.samples} application(s)">
                    <span class="outcome-bar-label">${escapeHtml(_HOP_LABELS[`${h.from}>${h.to}`] || `${h.from} → ${h.to}`)}</span>
                    <span class="outcome-bar-value">${h.median_days} days &middot; n=${h.samples}</span>
                </div>`).join('')}
        </div>`;
}

function renderOutcomesPanel(data) {
    const panel = document.getElementById('outcomes-panel');
    if (!panel) return;

    if (!data || !data.total_applied) {
        panel.innerHTML = '<p class="placeholder">No submitted applications yet. Outcomes appear once you start applying.</p>';
        return;
    }

    const stageLabels = { applied: 'Applied', screening: 'Screening', interview: 'Interview', offer: 'Offer' };
    const funnel = (data.funnel || []).map(f => `
        <div class="outcomes-funnel-stage">
            <div class="outcomes-funnel-count">${f.count}</div>
            <div class="outcomes-funnel-label">${escapeHtml(stageLabels[f.stage] || f.stage)}</div>
        </div>`).join('');

    const rr = data.response_rate || {};
    const overall = rr.overall || { total: 0, responded: 0, rate: 0 };
    const bandOrder = ['0-39', '40-69', '70-100', 'unscored'];
    const fitBands = (rr.by_fit_band || []).slice().sort(
        (a, b) => bandOrder.indexOf(a.key) - bandOrder.indexOf(b.key));

    panel.innerHTML = `
        <div class="outcomes-funnel">${funnel}</div>
        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:12px">
            Overall response rate: <strong style="color:var(--text-primary)">${overall.rate}%</strong>
            (${overall.responded} of ${overall.total} applications)
        </div>
        <div class="outcomes-breakdowns">
            ${_outcomeBarRows('Response Rate by Source', rr.by_source)}
            ${_outcomeBarRows('Response Rate by Fit Score', fitBands)}
            ${_outcomeBarRows('Response Rate by Honesty Level', rr.by_honesty)}
            ${_stageDurations(data.stage_durations)}
        </div>`;
}

// ---------------------------------------------------------------------------
// Run-status chip (topbar) — one persistent indicator for both long-running
// background runs (fetch + batch scoring). Each poll pushes its live text in
// ("Searching · 4/9" / "Scoring · 12/50") or null when its run ends; the chip
// shows whatever is active and hides when nothing is. The same state is
// forwarded to the desktop shell (Tauri) so the tray menu can mirror it.
// ---------------------------------------------------------------------------
const _chipRuns = { fetch: null, score: null };
let _lastShellRunStatus = null;

function updateRunChip(kind, text) {
    _chipRuns[kind] = text || null;
    const chip = document.getElementById('run-status-chip');
    const textEl = document.getElementById('run-status-chip-text');
    const parts = [_chipRuns.fetch, _chipRuns.score].filter(Boolean);
    if (chip && textEl) {
        if (parts.length) {
            textEl.textContent = parts.join('  ·  ');
            chip.style.display = '';
        } else {
            chip.style.display = 'none';
        }
    }
    _notifyShellRunStatus(parts.length > 0, parts.join(' · '));
}

// Tell the Tauri shell about run-state changes (tray status line + tooltip,
// and close-to-tray behaviour). No-op in a plain browser; failures are silent
// (an older shell without the command just ignores us).
function _notifyShellRunStatus(active, text) {
    const core = window.__TAURI__ && window.__TAURI__.core;
    if (!core || typeof core.invoke !== 'function') return;
    const key = `${active}|${text}`;
    if (_lastShellRunStatus === key) return;
    _lastShellRunStatus = key;
    try {
        core.invoke('set_run_status', { active, text }).catch(() => {});
    } catch (e) { /* shell without the command */ }
}

// One-shot on page load: if a fetch or scoring batch is already in flight
// (page reload, second window, run started from the API), re-attach the
// button states, progress cards, polls and the header chip to it.
async function reattachActiveRuns() {
    try {
        const s = await api('/api/jobs/fetch/status');
        if (s.active) {
            const btn = document.getElementById('fetch-btn');
            btn.disabled = true;
            btn.textContent = 'Fetching...';
            document.getElementById('fetch-stop-btn').style.display = '';
            document.getElementById('fetch-finish-btn').style.display = '';
            showFetchStatus(true);
            startFetchPoll();
        }
    } catch (e) { /* backend not up yet — the polls start on demand anyway */ }
    try {
        const s = await api('/api/jobs/score-batch/status');
        if (s.status === 'scoring') {
            const btn = document.getElementById('score-btn');
            btn.disabled = true;
            btn.textContent = 'Scoring...';
            document.getElementById('score-stop-btn').style.display = '';
            showScoreStatus(true);
            renderScoreStatus(s);
            startScorePoll();
        }
    } catch (e) { /* older backend without the endpoint, or not up yet */ }
}

let _fetchPollInterval = null;

async function fetchNewJobs() {
    const sources = getSelectedSources();
    if (sources.length === 0) {
        toast('Select at least one source to fetch from', 'error');
        return;
    }
    const btn = document.getElementById('fetch-btn');
    btn.disabled = true;
    btn.textContent = 'Fetching...';
    document.getElementById('fetch-stop-btn').style.display = '';
    document.getElementById('fetch-finish-btn').style.display = '';
    showFetchStatus(true);

    try {
        await api('/api/jobs/fetch', {
            method: 'POST',
            body: JSON.stringify({ sources }),
        });
        startFetchPoll();
    } catch (e) {
        toast('Failed to start job fetch', 'error');
        btn.disabled = false;
        btn.textContent = 'Fetch New Jobs';
        document.getElementById('fetch-stop-btn').style.display = 'none';
        document.getElementById('fetch-finish-btn').style.display = 'none';
        showFetchStatus(false);
    }
}

async function cancelFetch() {
    try {
        await api('/api/jobs/fetch/cancel', { method: 'POST' });
        toast('Stopping job fetch...', 'info');
    } catch (e) {
        toast('Failed to cancel fetch', 'error');
    }
}

async function finishFetch() {
    try {
        await api('/api/jobs/fetch/finish', { method: 'POST' });
        toast('Finishing up — saving what we have...', 'info');
    } catch (e) {
        toast('Failed to finish fetch', 'error');
    }
}

function showFetchStatus(visible) {
    document.getElementById('fetch-status').style.display = visible ? '' : 'none';
}

function startFetchPoll() {
    stopFetchPoll();
    _fetchPollInterval = setInterval(async () => {
        try {
            const s = await api('/api/jobs/fetch/status');
            const textEl = document.getElementById('fetch-status-text');
            const barEl = document.getElementById('fetch-progress-bar');
            const spinnerEl = document.getElementById('fetch-spinner');

            // Navigated away mid-tick: the status card is gone. Tear the poll
            // down instead of dereferencing nulls into the swallowed catch
            // (which would leak the interval).
            if (!textEl || !barEl) {
                stopFetchPoll();
                return;
            }

            textEl.textContent = s.detail || 'Working...';

            let pct = 0;
            if (s.phase === 'fetching' && s.sources_total > 0) {
                pct = Math.round((s.sources_done / s.sources_total) * 80);
            } else if (s.phase === 'saving') {
                pct = 85;
            } else if (s.phase === 'done' || s.phase === 'error') {
                pct = 100;
            }
            barEl.style.width = pct + '%';

            // Header chip mirrors the run from every tab.
            if (s.active) {
                updateRunChip('fetch', s.sources_total > 0
                    ? `Searching · ${s.sources_done}/${s.sources_total}`
                    : (s.phase === 'saving' ? 'Searching · saving' : 'Searching…'));
            } else {
                updateRunChip('fetch', null);
            }

            if (!s.active) {
                stopFetchPoll();
                spinnerEl.style.display = 'none';
                const btn = document.getElementById('fetch-btn');
                btn.disabled = false;
                btn.textContent = 'Fetch New Jobs';
                document.getElementById('fetch-stop-btn').style.display = 'none';
                document.getElementById('fetch-finish-btn').style.display = 'none';

                if (s.phase === 'done') {
                    const msg = s.detail && s.detail.includes('ancelled')
                        ? s.detail
                        : `Found ${s.jobs_found} jobs (${s.jobs_inserted} new)`;
                    toast(msg, 'success');
                    loadJobs();
                    loadDashboard();
                } else if (s.phase === 'error') {
                    toast('Job fetch failed', 'error');
                }

                setTimeout(() => showFetchStatus(false), 5000);
            }
        } catch (e) {
            // Ignore poll errors
        }
    }, 1500);
}

function stopFetchPoll() {
    if (_fetchPollInterval) {
        clearInterval(_fetchPollInterval);
        _fetchPollInterval = null;
    }
}

// ---------------------------------------------------------------------------
// Batch scoring progress — GET /api/jobs/score-batch/status, polled every 2s
// only while a batch is running (same lifecycle as the fetch poll). Drives the
// Score All progress card and the header chip.
// ---------------------------------------------------------------------------
let _scorePollInterval = null;

function showScoreStatus(visible) {
    const el = document.getElementById('score-status');
    if (el) el.style.display = visible ? '' : 'none';
}

function startScorePoll() {
    stopScorePoll();
    _scorePollInterval = setInterval(async () => {
        try {
            renderScoreStatus(await api('/api/jobs/score-batch/status'));
        } catch (e) {
            // Ignore poll errors — the next tick tries again.
        }
    }, 2000);
}

function stopScorePoll() {
    if (_scorePollInterval) {
        clearInterval(_scorePollInterval);
        _scorePollInterval = null;
    }
}

function renderScoreStatus(s) {
    const textEl = document.getElementById('score-status-text');
    const currentEl = document.getElementById('score-status-current');
    const barEl = document.getElementById('score-progress-bar');
    const spinnerEl = document.getElementById('score-spinner');
    if (!textEl || !barEl) return;

    const pct = s.total > 0 ? Math.round((s.done / s.total) * 100) : 0;

    if (s.status === 'scoring') {
        textEl.textContent = s.total > 0 ? `${s.done} of ${s.total} · ${pct}%` : (s.detail || 'Scoring…');
        if (currentEl) currentEl.textContent = s.current || '';
        barEl.style.width = pct + '%';
        if (spinnerEl) spinnerEl.style.display = '';
        updateRunChip('score', s.total > 0 ? `Scoring · ${s.done}/${s.total}` : 'Scoring…');
        return;
    }

    // Terminal (done/cancelled/error) or idle: tear the run UI down.
    stopScorePoll();
    updateRunChip('score', null);
    if (spinnerEl) spinnerEl.style.display = 'none';
    if (currentEl) currentEl.textContent = '';
    const btn = document.getElementById('score-btn');
    if (btn) {
        btn.disabled = false;
        updateScoreBtnLabel();
    }
    const stopBtn = document.getElementById('score-stop-btn');
    if (stopBtn) stopBtn.style.display = 'none';

    if (s.status === 'done') {
        barEl.style.width = '100%';
        textEl.textContent = s.detail || `Scored ${s.done} jobs`;
        toast(textEl.textContent, 'success');
        loadJobs();
        loadDashboard();
    } else if (s.status === 'cancelled') {
        textEl.textContent = s.detail || `Stopped after ${s.done} jobs`;
        toast(textEl.textContent, 'info');
        loadJobs();
        loadDashboard();
    } else if (s.status === 'error') {
        textEl.textContent = s.detail || 'Batch scoring failed';
        toast('Batch scoring failed', 'error');
    }
    setTimeout(() => showScoreStatus(false), 5000);
}

// Add a single job by URL
async function addJobByUrl() {
    const input = document.getElementById('manual-url-input');
    const btn = document.getElementById('manual-add-btn');
    const spinner = document.getElementById('manual-add-spinner');
    const statusEl = document.getElementById('manual-add-status');
    const url = (input.value || '').trim();
    if (!url) {
        toast('Enter a URL first', 'error');
        return;
    }
    btn.disabled = true;
    spinner.style.display = '';
    statusEl.style.display = '';
    statusEl.textContent = 'Fetching...';
    try {
        const res = await api('/api/jobs/ingest-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        const label = `${res.title || '(untitled)'}${res.company ? ' @ ' + res.company : ''}`;
        if (res.status === 'exists') {
            statusEl.textContent = `Already in your list: ${label}`;
            toast('Already in your list', 'info');
        } else if (res.status === 'refilled') {
            statusEl.textContent = `Updated: ${label}`;
            toast(`Updated: ${label}`, 'success');
            input.value = '';
            loadJobs();
        } else {
            statusEl.textContent = `Added: ${label}`;
            toast(`Added: ${label}`, 'success');
            input.value = '';
            loadJobs();
        }
    } catch (e) {
        statusEl.textContent = e.message || 'Failed to add job';
        toast(e.message || 'Failed to add job', 'error');
    } finally {
        btn.disabled = false;
        spinner.style.display = 'none';
        setTimeout(() => { statusEl.style.display = 'none'; }, 6000);
    }
}

// Refetch missing LinkedIn descriptions
let _refetchDescPollInterval = null;
async function refetchMissingDescriptions() {
    const btn = document.getElementById('refetch-desc-btn');
    const stopBtn = document.getElementById('refetch-desc-stop-btn');
    const statusEl = document.getElementById('refetch-desc-status');
    btn.disabled = true;
    btn.textContent = 'Refetching...';
    stopBtn.style.display = '';
    statusEl.style.display = '';
    statusEl.textContent = 'Starting...';
    try {
        await api('/api/jobs/refetch-descriptions', { method: 'POST' });
        _startRefetchDescPoll();
    } catch (e) {
        toast(e.message || 'Failed to start refetch', 'error');
        btn.disabled = false;
        btn.textContent = 'Refetch missing descriptions';
        stopBtn.style.display = 'none';
        statusEl.style.display = 'none';
    }
}

async function cancelRefetchDescriptions() {
    try {
        await api('/api/jobs/refetch-descriptions/cancel', { method: 'POST' });
        toast('Stopping refetch...', 'info');
    } catch (e) {
        toast('Failed to cancel refetch', 'error');
    }
}

function _startRefetchDescPoll() {
    if (_refetchDescPollInterval) clearInterval(_refetchDescPollInterval);
    _refetchDescPollInterval = setInterval(async () => {
        try {
            const s = await api('/api/jobs/refetch-descriptions/status');
            const statusEl = document.getElementById('refetch-desc-status');
            statusEl.textContent = s.detail || 'Working...';
            if (!s.active) {
                clearInterval(_refetchDescPollInterval);
                _refetchDescPollInterval = null;
                const btn = document.getElementById('refetch-desc-btn');
                const stopBtn = document.getElementById('refetch-desc-stop-btn');
                btn.disabled = false;
                btn.textContent = 'Refetch missing descriptions';
                stopBtn.style.display = 'none';
                if (s.total > 0) {
                    toast(`Refetch done — updated ${s.updated}, failed ${s.failed} of ${s.total}`, 'success');
                    loadJobs();
                } else {
                    toast('No LinkedIn jobs with empty descriptions', 'info');
                }
                setTimeout(() => { statusEl.style.display = 'none'; }, 5000);
            }
        } catch (e) {
            // Ignore poll errors
        }
    }, 1500);
}

// Operations poll
let _opsPollInterval = null;
function _startOpsPoll() {
    if (_opsPollInterval) return;
    _opsPollInterval = setInterval(async () => {
        try {
            const s = await api('/api/operations/status');
            if (!s.score_batch) {
                const btn = document.getElementById('score-btn');
                if (btn.disabled) {
                    btn.disabled = false;
                    btn.textContent = document.getElementById('score-rescore-cb')?.checked ? 'Rescore Jobs' : 'Score Unscored';
                    document.getElementById('score-stop-btn').style.display = 'none';
                }
            }
            if (!s.tailor_batch) {
                const btn = document.getElementById('tailor-btn');
                if (btn.disabled) {
                    btn.disabled = false;
                    btn.textContent = 'Tailor All Unprocessed';
                    document.getElementById('tailor-stop-btn').style.display = 'none';
                }
            }
            if (!s.estimate_salaries) {
                const btn = document.getElementById('estimate-salaries-btn');
                if (btn && btn.disabled) {
                    btn.disabled = false;
                    btn.textContent = 'Estimate Missing';
                    document.getElementById('estimate-salaries-stop-btn').style.display = 'none';
                }
            }
            if (!s.score_batch && !s.tailor_batch && !s.apply && !s.estimate_salaries) {
                clearInterval(_opsPollInterval);
                _opsPollInterval = null;
            }
        } catch (e) {}
    }, 2000);
}

// ---------------------------------------------------------------------------
// Detect Apply Types
// ---------------------------------------------------------------------------

let _detectPollInterval = null;

async function detectApplyTypes() {
    const btn = document.getElementById('detect-btn');
    btn.disabled = true;
    btn.textContent = 'Detecting...';
    document.getElementById('detect-stop-btn').style.display = '';
    document.getElementById('detect-result').style.display = 'none';
    try {
        await api('/api/detect-apply-types', { method: 'POST' });
        _startDetectPoll();
    } catch (e) {
        toast('Failed to start apply type detection', 'error');
        btn.disabled = false;
        btn.textContent = 'Run Detection';
        document.getElementById('detect-stop-btn').style.display = 'none';
    }
}

async function cancelDetectApplyTypes() {
    try {
        await api('/api/detect-apply-types/cancel', { method: 'POST' });
        toast('Stopping detection...', 'info');
    } catch (e) {
        toast('Failed to cancel detection', 'error');
    }
}

function _startDetectPoll() {
    if (_detectPollInterval) return;
    _detectPollInterval = setInterval(async () => {
        try {
            const s = await api('/api/detect-apply-types/status');
            if (!s.active) {
                clearInterval(_detectPollInterval);
                _detectPollInterval = null;
                const btn = document.getElementById('detect-btn');
                btn.disabled = false;
                btn.textContent = 'Run Detection';
                document.getElementById('detect-stop-btn').style.display = 'none';
                if (s.processed > 0) {
                    const msg = `Processed ${s.processed} jobs \u2014 ${s.easy_apply} Easy Apply, ${s.quick_apply} Quick Apply, ${s.external} External`;
                    const resultEl = document.getElementById('detect-result');
                    resultEl.textContent = msg;
                    resultEl.style.display = '';
                    toast(msg, 'success');
                } else if (s.detail) {
                    toast(s.detail, 'info');
                }
            }
        } catch (e) {}
    }, 1500);
}

async function tailorAll() {
    try {
        await api('/api/jobs/tailor-batch', {
            method: 'POST',
            body: JSON.stringify({ min_score: 50 }),
        });
        const btn = document.getElementById('tailor-btn');
        btn.disabled = true;
        btn.textContent = 'Tailoring...';
        document.getElementById('tailor-stop-btn').style.display = '';
        toast('Batch tailoring started!', 'success');
        _startOpsPoll();
    } catch (e) {
        toast('Failed to start batch tailoring', 'error');
    }
}

async function cancelTailorBatch() {
    try {
        await api('/api/jobs/tailor-batch/cancel', { method: 'POST' });
        toast('Stopping batch tailoring...', 'info');
    } catch (e) {
        toast('Failed to cancel tailoring', 'error');
    }
}

