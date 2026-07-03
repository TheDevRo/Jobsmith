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
        const panel = document.getElementById('outcomes-panel');
        if (panel) panel.innerHTML = '<p class="placeholder">Failed to load outcome analytics</p>';
    }
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
        </div>`;
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

