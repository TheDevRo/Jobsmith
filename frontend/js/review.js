// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ---- Review Queue ----
let currentReviewView = 'pending';

function switchReviewView(view) {
    currentReviewView = view;
    document.getElementById('review-tab-shortlisted').classList.toggle('active', view === 'shortlisted');
    document.getElementById('review-tab-pending').classList.toggle('active', view === 'pending');
    document.getElementById('review-tab-submitted').classList.toggle('active', view === 'submitted');
    document.getElementById('review-tab-failed').classList.toggle('active', view === 'failed');
    document.getElementById('review-tab-in-progress').classList.toggle('active', view === 'in-progress');
    document.getElementById('review-shortlisted-view').style.display = view === 'shortlisted' ? '' : 'none';
    document.getElementById('review-pending-view').style.display = view === 'pending' ? '' : 'none';
    document.getElementById('review-submitted-view').style.display = view === 'submitted' ? '' : 'none';
    document.getElementById('review-failed-view').style.display = view === 'failed' ? '' : 'none';
    document.getElementById('review-in-progress-view').style.display = view === 'in-progress' ? '' : 'none';
    renderFunnel(); // cheap: sync the active segment (no fetch on tab switches)
    if (view === 'shortlisted') loadShortlisted();
    else if (view === 'pending') loadReviewQueue();
    else if (view === 'submitted') loadSubmittedApplications();
    else if (view === 'in-progress') loadInProgress();
    else loadFailedApplications();
}

// ---- Pipeline funnel strip ----
// A second, visual tab bar above .review-tab-bar: five proportional segments,
// one per existing Pipeline view, with live counts. Segment flex-grow is
// proportional to count; zero-count segments keep a min-width and dim.
// Counts are fetched once on entering the Pipeline (refreshFunnelCounts) and
// patched in-place by each loader from data it already has — no double-fetch.
const _FUNNEL_SEGS = [
    { view: 'shortlisted', label: 'Shortlisted', cls: 'fseg-steel' },
    { view: 'pending',     label: 'Ready',       cls: 'fseg-ember' },
    { view: 'submitted',   label: 'Applied',     cls: 'fseg-green' },
    { view: 'failed',      label: 'Failed',      cls: 'fseg-red' },
    { view: 'in-progress', label: 'In Progress', cls: 'fseg-amber' },
];
const _funnelCounts = { shortlisted: 0, pending: 0, submitted: 0, failed: 0, 'in-progress': 0 };

function renderFunnel() {
    const el = document.getElementById('pipeline-funnel');
    if (!el) return;
    el.innerHTML = _FUNNEL_SEGS.map(seg => {
        const n = _funnelCounts[seg.view] || 0;
        const active = seg.view === currentReviewView;
        return `<button type="button" role="tab" aria-selected="${active}"
            class="fseg ${seg.cls}${active ? ' active' : ''}${n === 0 ? ' empty' : ''}"
            style="flex-grow:${n}" onclick="switchReviewView('${seg.view}')"
            aria-label="${escapeHtml(seg.label)}: ${n}"><b class="num">${n}</b><span>${escapeHtml(seg.label)}</span></button>`;
    }).join('');
}

function _setFunnelCount(view, n) {
    _funnelCounts[view] = Number(n) || 0;
    renderFunnel();
}

// Fetch all five counts cheaply. Called on entering the Pipeline and after
// status transitions this file makes.
async function refreshFunnelCounts() {
    renderFunnel(); // paint immediately with whatever we have
    const grab = (p) => p.catch(() => null);
    const [s, p, su, f, ip] = await Promise.all([
        grab(api('/api/jobs?status=shortlisted&limit=1').then(d => (d && typeof d.total === 'number') ? d.total : ((d && d.jobs) || []).length)),
        grab(api('/api/applications/pending?limit=200').then(a => (a || []).length)),
        grab(api('/api/applications/submitted?limit=200').then(a => (a || []).length)),
        grab(api('/api/applications/failed?limit=200').then(a => (a || []).length)),
        grab(api('/api/applications/in-progress').then(d => ((d && d.in_progress) || []).length + ((d && d.needs_attention) || []).length)),
    ]);
    if (s !== null) _funnelCounts.shortlisted = s;
    if (p !== null) _funnelCounts.pending = p;
    if (su !== null) _funnelCounts.submitted = su;
    if (f !== null) _funnelCounts.failed = f;
    if (ip !== null) _funnelCounts['in-progress'] = ip;
    renderFunnel();
}

// Pipeline → Shortlisted stage: jobs the user kept while scouting the Inbox
// (job.status === 'shortlisted'), before an application exists. Reuses the
// job list endpoint; no application row yet, so these render as job cards.
async function loadShortlisted() {
    try {
        const data = await api('/api/jobs?status=shortlisted&limit=50&sort_by=fit_score&sort_dir=desc');
        renderShortlisted(data);
    } catch (e) {
        renderError('shortlisted-list', 'Failed to load shortlisted jobs.', loadShortlisted);
    }
}

function renderShortlisted(data) {
    const jobs = (data && data.jobs) || [];
    _setFunnelCount('shortlisted', (data && typeof data.total === 'number') ? data.total : jobs.length);
    const el = document.getElementById('shortlisted-list');
    if (!jobs.length) {
        el.innerHTML = '<p class="placeholder">No shortlisted jobs yet. Scout your Inbox and shortlist the ones worth pursuing.</p>';
        return;
    }
    el.innerHTML = jobs.map(job => `
        <div class="job-card" style="cursor:default">
            <div class="job-card-header">
                <div class="job-card-info">
                    <div class="job-card-title">${escapeHtml(job.title)}</div>
                    <div class="job-card-company">${escapeHtml(job.company || 'Unknown')}</div>
                    <div class="job-card-meta">
                        <span>${escapeHtml(job.location || '')}</span>
                        <span class="source-badge">${escapeHtml(job.source)}</span>
                    </div>
                </div>
                <div class="job-card-right">
                    ${renderHeatChip(job.fit_score)}
                    <div class="scout-actions">
                        <button class="btn btn-primary btn-xs" onclick="tailorJob('${job.id}')">Tailor</button>
                        <button class="btn btn-secondary btn-xs" onclick="scoreJob('${job.id}')">${job.fit_score ? 'Rescore' : 'Score'}</button>
                        <button class="btn btn-ghost btn-xs" onclick="passShortlisted('${job.id}')">Pass</button>
                    </div>
                </div>
            </div>
        </div>`).join('');
}

async function passShortlisted(jobId) {
    try {
        await api(`/api/jobs/${jobId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'passed' }),
        });
        toast('Passed', 'info');
        loadShortlisted();
        refreshFunnelCounts();
    } catch (e) {
        toast('Failed to update', 'error');
    }
}

async function loadReviewQueue() {
    try {
        const apps = await api('/api/applications/pending?limit=50');
        _setFunnelCount('pending', (apps || []).length);
        renderReviewQueue(apps);
    } catch (e) {
        renderError('review-list', 'Failed to load the review queue.', loadReviewQueue);
    }
}

async function loadSubmittedApplications() {
    try {
        const apps = await api('/api/applications/submitted?limit=50');
        _setFunnelCount('submitted', (apps || []).length);
        renderSubmittedApplications(apps);
    } catch (e) {
        renderError('submitted-list', 'Failed to load submitted applications.', loadSubmittedApplications);
    }
}

async function loadFailedApplications() {
    try {
        const apps = await api('/api/applications/failed?limit=50');
        _setFunnelCount('failed', (apps || []).length);
        renderFailedApplications(apps);
    } catch (e) {
        renderError('failed-list', 'Failed to load failed applications.', loadFailedApplications);
    }
}

async function loadInProgress() {
    try {
        const data = await api('/api/applications/in-progress');
        renderInProgress(data);
    } catch (e) {
        renderError('in-progress-list', 'Failed to load in-progress applications.', loadInProgress);
    }
}

function renderInProgress(data) {
    const container = document.getElementById('in-progress-list');
    const inProg = data.in_progress || [];
    const needsAttn = data.needs_attention || [];
    _setFunnelCount('in-progress', inProg.length + needsAttn.length);

    // Update tab badge
    const badge = document.getElementById('in-progress-badge');
    if (badge) {
        if (needsAttn.length > 0) {
            badge.textContent = needsAttn.length;
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }
    }

    const _statusLabel = {
        autofill_complete: 'Autofill Complete',
        needs_review:      'Needs Review',
        rate_limited:      'Rate Limited',
    };
    const _statusClass = {
        autofill_complete: 'pill-autofill_complete',
        needs_review:      'pill-needs_review',
        rate_limited:      'pill-rate_limited',
    };

    // Section A — Currently Applying (includes paused)
    const liveHtml = inProg.length === 0
        ? '<p class="placeholder" style="margin:0 0 8px 0">No applications running.</p>'
        : inProg.map(app => {
            const isPaused = app.paused === true;
            const pill = isPaused
                ? `<span class="pill pill-paused">Paused</span>`
                : `<span class="pill pill-applying">Applying...</span>`;
            const pausedBanner = isPaused
                ? `<div style="margin:8px 0 4px 0;padding:8px 10px;border-radius:6px;background:var(--bg-card);border:1px solid var(--accent-orange);font-size:12px;color:var(--accent-orange)">
                       &#9889; Automation paused — browser is open for manual interaction.
                       Click <strong>Resume</strong> when ready to continue.
                   </div>`
                : '';
            const actions = isPaused
                ? `<div class="review-card-actions">
                       <button class="btn btn-primary btn-sm" onclick="resumeApply('${escapeHtml(app.id || '')}')">Resume</button>
                       <button class="btn btn-danger btn-sm" onclick="forceStopApply()" title="Close browser and stop automation">Force Stop</button>
                   </div>`
                : `<div class="review-card-actions">
                       <button class="btn btn-warning btn-sm" onclick="pauseApply()" title="Freeze automation and keep browser open">Pause</button>
                       <button class="btn btn-danger btn-sm" onclick="forceStopApply()" title="Stop automation and close browser">Force Stop</button>
                   </div>`;
            return `
            <div class="review-card">
                <div class="review-card-header">
                    <div>
                        <div class="review-card-title">${escapeHtml(app.job_title || '')}</div>
                        <div class="review-card-company">${escapeHtml(app.company || '')}</div>
                        ${app.adapter ? `<div style="font-size:12px;color:var(--text-muted);margin-top:4px">Adapter: ${escapeHtml(app.adapter)}</div>` : ''}
                    </div>
                    <div class="job-card-right">${pill}</div>
                </div>
                ${pausedBanner}
                ${actions}
            </div>`;
        }).join('');

    // Section B — Needs Attention
    const attnHtml = needsAttn.length === 0
        ? '<p class="placeholder" style="margin:0">Nothing needs attention.</p>'
        : needsAttn.map(app => {
            const sLabel = _statusLabel[app.state] || app.state;
            const sCls   = _statusClass[app.state] || 'pill-discovered';
            const tierStr = app.tier != null ? ` · Tier ${app.tier}` : '';
            const adapterStr = app.adapter ? `${escapeHtml(app.adapter)}${tierStr}` : '';
            const _skippedNames = Array.isArray(app.skipped_field_names) ? app.skipped_field_names : [];
            const _skippedLabel = `&#9888; ${app.fields_skipped} field${app.fields_skipped > 1 ? 's' : ''} skipped`;
            const skippedHtml = (app.fields_skipped > 0)
                ? (_skippedNames.length
                    ? `<details style="margin:6px 0"><summary style="color:var(--accent-orange);font-size:12px;cursor:pointer;list-style:none;-webkit-appearance:none">${_skippedLabel}</summary><ul style="margin:4px 0 0 14px;padding:0;font-size:11px;color:var(--text-muted)">${_skippedNames.map(n => `<li>${escapeHtml(n)}</li>`).join('')}</ul></details>`
                    : `<span style="color:var(--accent-orange);font-size:12px">${_skippedLabel}</span>`)
                : '';
            const screenshotHtml = app.screenshot_path
                ? `<a class="btn btn-secondary btn-sm" href="/api/jobs/${escapeHtml(app.job_id)}/screenshots" target="_blank" rel="noopener">View Screenshot</a>`
                : '';
            return `
            <div class="review-card">
                <div class="review-card-header">
                    <div>
                        <div class="review-card-title">${escapeHtml(app.job_title || '')}</div>
                        <div class="review-card-company">${escapeHtml(app.company || '')}</div>
                        ${adapterStr ? `<div style="font-size:12px;color:var(--text-muted);margin-top:4px">${adapterStr}</div>` : ''}
                    </div>
                    <div class="job-card-right">
                        <span class="pill ${sCls}">${sLabel}</span>
                    </div>
                </div>
                ${skippedHtml ? `<div>${skippedHtml}</div>` : ''}
                <div class="review-card-actions">
                    <button class="btn btn-secondary btn-sm" onclick="openJobUrl('${escapeHtml(app.job_id)}')">Open Job URL</button>
                    ${screenshotHtml}
                    <a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/resume" download>Download Resume</a>
                    <a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/cover-letter" download>Download Cover Letter</a>
                    <button class="btn btn-primary btn-sm" onclick="requeueNeedsAttention('${escapeHtml(app.job_id)}')">Requeue</button>
                    <button class="btn btn-green btn-sm" onclick="inProgressMarkApplied('${escapeHtml(app.job_id)}')">Mark as Applied</button>
                    <button class="btn btn-secondary btn-sm" onclick="inProgressDismiss('${escapeHtml(app.job_id)}')">Dismiss</button>
                </div>
            </div>`;
        }).join('');

    container.innerHTML = `
        <div style="margin-bottom:18px">
            <h3 style="font-size:13px;font-weight:600;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px 0">Currently Applying</h3>
            ${liveHtml}
        </div>
        <div>
            <h3 style="font-size:13px;font-weight:600;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em;margin:0 0 10px 0">Needs Attention</h3>
            ${attnHtml}
        </div>
    `;
}

async function inProgressMarkApplied(jobId) {
    // Finds the application by job_id via the needs_attention data already rendered.
    // Button passes job_id; we need the app id — re-fetch to resolve it.
    try {
        const data = await api('/api/applications/in-progress');
        const app = (data.needs_attention || []).find(a => a.job_id === jobId);
        if (!app) { toast('Application not found', 'error'); return; }
        await api(`/api/applications/${app.id}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ status: 'applied' }),
        });
        toast('Marked as applied', 'success');
        loadInProgress();
    } catch (e) {
        toast('Failed to update status', 'error');
    }
}

async function inProgressDismiss(jobId) {
    try {
        const data = await api('/api/applications/in-progress');
        const app = (data.needs_attention || []).find(a => a.job_id === jobId);
        if (!app) { toast('Application not found', 'error'); return; }
        await api(`/api/applications/${app.id}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ status: 'manual' }),
        });
        toast('Dismissed', 'info');
        loadInProgress();
    } catch (e) {
        toast('Failed to dismiss', 'error');
    }
}

async function requeueNeedsAttention(jobId) {
    if (!(await appConfirm('Move this application back to the review queue?'))) return;
    try {
        const data = await api('/api/applications/in-progress');
        const app = (data.needs_attention || []).find(a => a.job_id === jobId);
        if (!app) { toast('Application not found', 'error'); return; }
        await api(`/api/applications/${app.id}/requeue`, { method: 'POST' });
        toast('Requeued for review', 'success');
        loadInProgress();
    } catch (e) {
        toast('Failed to requeue', 'error');
    }
}

async function openJobUrl(jobId) {
    try {
        const job = await api(`/api/jobs/${jobId}`);
        if (job && job.url) {
            // Fire-and-forget: signal the browser extension which job is now active.
            signalActiveJobToExtension(jobId);
            openExternal(job.url);
        } else {
            toast('No URL available for this job', 'warning');
        }
    } catch (e) {
        toast('Failed to load job URL', 'error');
    }
}

function signalActiveJobToExtension(jobId) {
    if (!jobId) return;
    fetch('/api/extension/active-job', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: String(jobId) }),
        keepalive: true,
    }).catch(() => { /* extension hint is best-effort; never block UX */ });
}

// Document-level delegate so the anchor-form Open Job URL controls (which
// have no onclick) also signal the extension before the new tab opens.
document.addEventListener('click', (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest('a[data-jobsmith-open-url]');
    if (!a) return;
    const jobId = a.getAttribute('data-jobsmith-job-id');
    if (jobId) signalActiveJobToExtension(jobId);
});

async function failedMarkApplied(appId) {
    try {
        await api(`/api/applications/${appId}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ status: 'applied' }),
        });
        toast('Marked as applied', 'success');
        document.getElementById(`failed-${appId}`)?.remove();
        loadInProgress(); // refresh In Progress badge
    } catch (e) {
        toast('Failed to update status', 'error');
    }
}

async function failedDismiss(appId) {
    try {
        await api(`/api/applications/${appId}/status`, {
            method: 'PATCH',
            body: JSON.stringify({ status: 'manual' }),
        });
        toast('Dismissed', 'info');
        document.getElementById(`failed-${appId}`)?.remove();
    } catch (e) {
        toast('Failed to dismiss', 'error');
    }
}

function renderFailedApplications(apps) {
    const container = document.getElementById('failed-list');
    if (apps.length === 0) {
        container.innerHTML = '<p class="placeholder">No failed applications.</p>';
        return;
    }

    container.innerHTML = apps.map(app => {
        const _statusInfo = {
            manual:            { label: 'Manual Apply',          cls: 'pill-rejected' },
            needs_review:      { label: 'Needs Review',          cls: 'pill-needs_review' },
            autofill_complete: { label: 'Autofill Complete',     cls: 'pill-autofill_complete' },
            already_applied:   { label: 'Already Applied',       cls: 'pill-already_applied' },
            rate_limited:      { label: 'Rate Limited',          cls: 'pill-rate_limited' },
        };
        const { label: statusLabel, cls: statusClass } = _statusInfo[app.status] || { label: 'Failed', cls: 'pill-rejected' };
        const isReset = app.error_message && app.error_message.startsWith('Reset:');
        const displayMessage = app.status === 'rate_limited'
            ? (app.error_message || 'Rate limited — retry later')
            : app.error_message;
        const attempts = app.auto_apply_attempts || 0;
        const canRetry = attempts < 3;
        const attemptBadge = attempts > 0
            ? `<span class="pill" style="background:rgba(231,76,60,0.15);color:var(--accent-red);font-size:10px">Failed ${attempts}x</span>`
            : '';

        return `
            <div class="review-card" id="failed-${escapeHtml(app.id)}">
                <div class="review-card-header">
                    <div>
                        <div class="review-card-title">${escapeHtml(app.title)}</div>
                        <div class="review-card-company">${escapeHtml(app.company || '')} &mdash; ${escapeHtml(app.location || '')}</div>
                        <div style="font-size:12px;color:var(--text-muted);margin-top:4px">${timeAgo(app.created_at)}</div>
                    </div>
                    <div class="job-card-right">
                        ${renderHeatChip(app.fit_score)}
                        <span class="pill ${statusClass}">${statusLabel}</span>
                        ${attemptBadge}
                    </div>
                </div>
                ${displayMessage ? `<div class="failed-reason ${isReset ? 'failed-reason-reset' : ''}">${escapeHtml(displayMessage)}</div>` : ''}
                <div class="review-card-actions">
                    <a class="btn btn-secondary btn-sm" href="${escapeHtml(safeHref(app.url))}" target="_blank" rel="noopener" data-jobsmith-open-url data-jobsmith-job-id="${escapeHtml(app.job_id)}">Open Job URL</a>
                    ${app.resume_content ? `<a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/resume" download>Download Resume</a>` : ''}
                    ${app.cover_letter_content ? `<a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/cover-letter" download>Download Cover Letter</a>` : ''}
                    <a class="btn btn-secondary btn-sm" href="/api/jobs/${escapeHtml(app.job_id)}/screenshots" target="_blank" rel="noopener">View Screenshots</a>
                    ${canRetry && window._autoApplyEnabled ? `<button class="btn btn-primary btn-sm" onclick="retryAutoApply('${escapeHtml(app.id)}')">Retry Auto-Apply</button>` : ''}
                    <button class="btn btn-primary btn-sm" onclick="requeueApplication('${escapeHtml(app.id)}')">Requeue</button>
                    <button class="btn btn-green btn-sm" onclick="failedMarkApplied('${escapeHtml(app.id)}')">Mark as Applied</button>
                    <button class="btn btn-secondary btn-sm" onclick="failedDismiss('${escapeHtml(app.id)}')">Dismiss</button>
                </div>
            </div>
        `;
    }).join('');
}

const OUTCOME_OPTIONS = [
    ['awaiting', 'Awaiting Response'],
    ['no_response', 'No Response'],
    ['screening', 'Screening'],
    ['interview', 'Interview'],
    ['offer', 'Offer'],
    ['rejected', 'Rejected'],
    ['withdrawn', 'Withdrawn'],
];

const OUTCOME_PILL_CLASS = {
    offer: 'pill-outcome-offer',
    interview: 'pill-outcome-interview',
    screening: 'pill-outcome-screening',
    rejected: 'pill-outcome-rejected',
    no_response: 'pill-outcome-muted',
    withdrawn: 'pill-outcome-muted',
};

function renderOutcomeControls(app) {
    const outcome = app.outcome || 'awaiting';
    const label = (OUTCOME_OPTIONS.find(([v]) => v === outcome) || [outcome, outcome])[1];
    const pillClass = OUTCOME_PILL_CLASS[outcome];
    const options = OUTCOME_OPTIONS.map(([v, l]) =>
        `<option value="${v}" ${v === outcome ? 'selected' : ''}>${l}</option>`).join('');
    return `
        <div class="outcome-row">
            <span class="outcome-label">Outcome</span>
            <select class="outcome-select" onchange="updateApplicationOutcome('${escapeHtml(app.id)}', this.value)">${options}</select>
            ${pillClass ? `<span class="pill ${pillClass}">${escapeHtml(label)}</span>` : ''}
        </div>`;
}

async function updateApplicationOutcome(appId, outcome) {
    try {
        await api(`/api/applications/${appId}/outcome`, {
            method: 'PATCH',
            body: JSON.stringify({ outcome }),
        });
        if (window._submittedApps && window._submittedApps[appId]) {
            window._submittedApps[appId].outcome = outcome;
        }
        toast('Outcome updated', 'success');
    } catch (e) {
        toast('Failed to update outcome', 'error');
    }
    loadSubmittedApplications();
}

function renderSubmittedApplications(apps) {
    const container = document.getElementById('submitted-list');
    if (apps.length === 0) {
        container.innerHTML = '<p class="placeholder">No submitted applications yet.</p>';
        return;
    }

    container.innerHTML = apps.map(app => {
        const statusClass = {applied: 'pill-applied', manual: 'pill-manual', approved: 'pill-approved', failed: 'pill-rejected', applying: 'pill-applying', autofill_complete: 'pill-autofill_complete', already_applied: 'pill-already_applied', rate_limited: 'pill-rate_limited', needs_review: 'pill-needs_review', paused: 'pill-paused'}[app.status] || 'pill-discovered';
        const statusLabel = {applied: 'Applied', manual: 'Manual Apply', approved: 'Approved', failed: 'Failed', applying: 'Applying...', autofill_complete: 'Autofill Complete', already_applied: 'Already Applied', rate_limited: 'Rate Limited', needs_review: 'Needs Review', paused: 'Paused'}[app.status] || app.status;
        const isApplying = app.status === 'applying';
        const appliedDate = app.applied_at ? timeAgo(app.applied_at) : timeAgo(app.created_at);

        return `
            <div class="review-card" id="submitted-${escapeHtml(app.id)}">
                <div class="review-card-header">
                    <div>
                        <div class="review-card-title">${escapeHtml(app.title)}</div>
                        <div class="review-card-company">${escapeHtml(app.company || '')} &mdash; ${escapeHtml(app.location || '')}</div>
                        <div style="font-size:12px;color:var(--text-muted);margin-top:4px">${appliedDate}</div>
                    </div>
                    <div class="job-card-right">
                        ${renderHeatChip(app.fit_score)}
                        <span class="pill ${statusClass}">${statusLabel}</span>
                    </div>
                </div>
                ${app.status === 'applied' ? renderOutcomeControls(app) : ''}
                ${app.error_message ? `<div style="font-size:12px;color:${app.status === 'applied' ? 'var(--accent-green)' : 'var(--accent-red)'};margin:8px 0">${escapeHtml(app.error_message)}</div>` : ''}
                ${isApplying ? `<div id="apply-progress-${escapeHtml(app.id)}" class="apply-progress-bar" style="font-size:11px;color:var(--accent-blue);margin:6px 0;padding:6px 8px;background:var(--bg-hover);border-radius:4px">Starting automation…</div>` : ''}
                <div class="review-tabs">
                    <div class="review-tab active" onclick="switchSubmittedTab(this, '${escapeHtml(app.id)}', 'resume')">Tailored Resume</div>
                    <div class="review-tab" onclick="switchSubmittedTab(this, '${escapeHtml(app.id)}', 'cover')">Cover Letter</div>
                    <div class="review-tab" onclick="switchSubmittedTab(this, '${escapeHtml(app.id)}', 'autofill')">Autofill Report</div>
                    <div class="review-tab" onclick="switchSubmittedTab(this, '${escapeHtml(app.id)}', 'emb')">Embellishments</div>
                </div>
                <div class="review-content" id="submitted-content-${escapeHtml(app.id)}">${escapeHtml(app.resume_content || 'No resume generated')}</div>
                <div class="review-card-actions">
                    ${isApplying ? `<button class="btn btn-danger btn-sm" onclick="forceStopApply()" title="Stop automation and close browser">Force Stop</button><button class="btn btn-warning btn-sm" onclick="pauseApply()" title="Freeze automation — browser stays open for manual interaction">Pause</button>` : ''}
                    <a class="btn btn-secondary btn-sm" href="${escapeHtml(safeHref(app.url))}" target="_blank" rel="noopener" data-jobsmith-open-url data-jobsmith-job-id="${escapeHtml(app.job_id)}">Open Job URL</a>
                    <a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/resume" download>Download Resume</a>
                    <a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/cover-letter" download>Download Cover Letter</a>
                    <button class="btn btn-secondary btn-sm" onclick="viewScreenshots('${escapeHtml(app.job_id)}')">Screenshots</button>
                    <button class="btn btn-secondary btn-sm" onclick="editContent('${escapeHtml(app.id)}')">Edit</button>
                    <button class="btn btn-secondary btn-sm" onclick="requeueApplication('${escapeHtml(app.id)}')">Requeue</button>
                </div>
            </div>
        `;
    }).join('');

    window._submittedApps = {};
    apps.forEach(a => { window._submittedApps[a.id] = a; });

    // Start progress polling for any "applying" apps
    apps.filter(a => a.status === 'applying').forEach(a => startApplyProgressPoll(a.id));
}

let _applyProgressPolls = {};  // appId -> intervalId

// Stop polling a run that never reports done — a wedged apply would otherwise
// keep the 2s poll alive forever. 10 minutes is well past any real apply.
const _APPLY_POLL_CEILING_MS = 10 * 60 * 1000;

function startApplyProgressPoll(appId) {
    if (_applyProgressPolls[appId]) return;
    const startedAt = Date.now();
    _applyProgressPolls[appId] = setInterval(async () => {
        try {
            const el = document.getElementById(`apply-progress-${appId}`);
            if (Date.now() - startedAt > _APPLY_POLL_CEILING_MS) {
                clearInterval(_applyProgressPolls[appId]);
                delete _applyProgressPolls[appId];
                if (el) el.textContent = 'Still applying after 10 minutes — use Force Stop if it\'s stuck.';
                return;
            }
            const data = await api(`/api/applications/${appId}/apply-progress`);
            if (!el) {
                clearInterval(_applyProgressPolls[appId]);
                delete _applyProgressPolls[appId];
                return;
            }
            if (!data.active) {
                clearInterval(_applyProgressPolls[appId]);
                delete _applyProgressPolls[appId];
                el.textContent = 'Done — refreshing…';
                setTimeout(() => loadSubmittedApplications(), 2000);
                return;
            }
            const stepLabels = { starting: 'Starting…', navigating: 'Navigating to page…', filling_fields: 'Filling fields…' };
            const step = stepLabels[data.step] || data.step || 'Working…';
            const elapsed = data.elapsed_seconds || 0;
            const adapter = data.adapter ? ` [${data.adapter}]` : '';
            el.textContent = `${step}${adapter} — ${elapsed}s elapsed`;
        } catch (e) {
            // Network error — ignore, keep polling
        }
    }, 2000);
}

function switchSettingsTab(tabEl, panelId) {
    document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.settings-tab-panel').forEach(p => p.classList.remove('active'));
    tabEl.classList.add('active');
    document.getElementById(panelId).classList.add('active');
}

function switchSubmittedTab(tabEl, appId, view) {
    const card = document.getElementById(`submitted-${appId}`);
    card.querySelectorAll('.review-tab').forEach(t => t.classList.remove('active'));
    tabEl.classList.add('active');
    _activeTab[appId] = view;

    const content = document.getElementById(`submitted-content-${appId}`);
    const app = window._submittedApps[appId];
    switch (view) {
        case 'resume': content.textContent = app.resume_content || 'No resume'; break;
        case 'cover': content.textContent = app.cover_letter_content || 'No cover letter'; break;
        case 'autofill': content.innerHTML = _renderAutofillReport(safeParseJSON(app.autofill_result, null)); break;
        case 'emb':
            content.innerHTML = '';
            loadEmbTab(app.job_id, `submitted-content-${appId}`);
            break;
    }
}

function renderReviewMatchChips(app) {
    const report = safeParseJSON(app.match_report, null);
    if (!report) return '';
    const matched = report.matched_skills || [];
    const missing = report.missing_skills || [];
    if (!matched.length && !missing.length) return '';
    return `
        <div class="skill-chips" style="margin-top:4px">
            ${matched.map(s => `<span class="skill-chip matched" title="Job requirement the candidate has">${escapeHtml(s)}</span>`).join('')}
            ${missing.map(s => `<span class="skill-chip missing" title="Job requirement missing from profile">${escapeHtml(s)}</span>`).join('')}
        </div>
    `;
}

function renderReviewQueue(apps) {
    const container = document.getElementById('review-list');
    if (apps.length === 0) {
        container.innerHTML = '<p class="placeholder">No applications pending review. Tailor some jobs first!</p>';
        return;
    }

    container.innerHTML = apps.map(app => {
        const isPaused = app.status === 'paused';
        return `
            <div class="review-card" id="review-${escapeHtml(app.id)}">
                <div class="review-card-header">
                    <div>
                        <div class="review-card-title">${escapeHtml(app.title)}</div>
                        <div class="review-card-company">${escapeHtml(app.company || '')} &mdash; ${escapeHtml(app.location || '')}</div>
                    </div>
                    <div class="job-card-right">
                        ${renderHeatChip(app.fit_score)}
                        ${isPaused ? '<span class="pill pill-paused">Paused</span>' : `<span class="source-badge">${escapeHtml(app.source)}</span>`}
                    </div>
                </div>
                <div class="review-score">${app.fit_reasoning ? escapeHtml(app.fit_reasoning) : ''}</div>
                ${renderReviewMatchChips(app)}
                <div class="review-tabs">
                    <div class="review-tab active" onclick="switchReviewTab(this, '${escapeHtml(app.id)}', 'resume')">Tailored Resume</div>
                    <div class="review-tab" onclick="switchReviewTab(this, '${escapeHtml(app.id)}', 'cover')">Cover Letter</div>
                    <div class="review-tab" onclick="switchReviewTab(this, '${escapeHtml(app.id)}', 'job')">Job Description</div>
                    <div class="review-tab" onclick="switchReviewTab(this, '${escapeHtml(app.id)}', 'autofill')">Autofill Report</div>
                    <div class="review-tab" onclick="switchReviewTab(this, '${escapeHtml(app.id)}', 'emb')">Embellishments</div>
                </div>
                <div class="review-content" id="content-${escapeHtml(app.id)}">${escapeHtml(app.resume_content || 'No resume generated')}</div>
                ${isPaused ? `<div style="margin:8px 0 4px 0;padding:8px 10px;border-radius:6px;background:var(--bg-card);border:1px solid var(--accent-orange);font-size:12px;color:var(--accent-orange)">&#9889; Automation is paused — the browser window is still open for manual interaction. Click <strong>Resume</strong> to continue automation.</div>` : ''}
                <div class="review-card-actions">
                    ${isPaused ? `
                        <button class="btn btn-primary btn-sm" onclick="resumeApply('${escapeHtml(app.id)}')">Resume</button>
                        <button class="btn btn-danger btn-sm" onclick="forceStopApply()">Force Stop</button>
                        <button class="btn btn-danger btn-sm" onclick="rejectApp('${escapeHtml(app.id)}')">Reject</button>
                        <a class="btn btn-secondary btn-sm" href="${escapeHtml(safeHref(app.url))}" target="_blank" rel="noopener" data-jobsmith-open-url data-jobsmith-job-id="${escapeHtml(app.job_id)}">Open Job URL</a>
                    ` : `
                        ${window._autoApplyEnabled ? `<button class="btn btn-primary btn-sm" onclick="autoApply('${escapeHtml(app.id)}')">Auto Apply</button>` : ''}
                        <button class="btn btn-assist btn-sm" onclick="launchAssist('${escapeHtml(app.job_id)}')">Apply Assist</button>
                        <button class="btn btn-green btn-sm" onclick="markAppApplied('${escapeHtml(app.id)}')">Mark Applied</button>
                        <button class="btn btn-danger btn-sm" onclick="rejectApp('${escapeHtml(app.id)}')">Reject</button>
                    `}
                    <a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/resume" download>Download Resume</a>
                    <a class="btn btn-secondary btn-sm" href="/api/resumes/${escapeHtml(app.job_id)}/cover-letter" download>Download Cover Letter</a>
                    <button class="btn btn-secondary btn-sm" onclick="editContent('${escapeHtml(app.id)}')">Edit</button>
                    <button class="btn btn-assist btn-sm" onclick="openAiEditModal('${escapeHtml(app.id)}')">AI Edit</button>
                </div>
            </div>
        `;
    }).join('');

    window._reviewApps = {};
    apps.forEach(a => { window._reviewApps[a.id] = a; });
}

function switchReviewTab(tabEl, appId, view) {
    const card = document.getElementById(`review-${appId}`);
    card.querySelectorAll('.review-tab').forEach(t => t.classList.remove('active'));
    tabEl.classList.add('active');
    _activeTab[appId] = view;

    const content = document.getElementById(`content-${appId}`);
    const app = window._reviewApps[appId];
    switch (view) {
        case 'resume': content.textContent = app.resume_content || 'No resume'; break;
        case 'cover': content.textContent = app.cover_letter_content || 'No cover letter'; break;
        case 'job': content.textContent = app.description || 'No description'; break;
        case 'autofill': content.innerHTML = _renderAutofillReport(safeParseJSON(app.autofill_result, null)); break;
        case 'emb':
            content.innerHTML = '';
            loadEmbTab(app.job_id, `content-${appId}`);
            break;
    }
}

const _activeTab = {};

function _getAppData(appId) {
    return (window._reviewApps && window._reviewApps[appId]) ||
           (window._submittedApps && window._submittedApps[appId]) || null;
}

function _getContentEl(appId) {
    return document.getElementById(`content-${appId}`) ||
           document.getElementById(`submitted-content-${appId}`);
}

function _getCurrentTab(appId) {
    return _activeTab[appId] || 'resume';
}

async function cancelApply(appId) {
    try {
        await api(`/api/applications/${appId}/apply/cancel`, { method: 'POST' });
        toast('Apply cancelled', 'info');
        loadSubmittedApplications();
        loadReviewQueue();
    } catch (e) {
        toast('Failed to cancel apply', 'error');
    }
}

async function forceStopApply() {
    try {
        await api('/api/applications/apply/force-stop', { method: 'POST' });
        toast('Force stopped — browser closed', 'info');
        loadSubmittedApplications();
        loadReviewQueue();
    } catch (e) {
        toast('Failed to force stop', 'error');
    }
}

async function pauseApply() {
    try {
        await api('/api/applications/apply/pause', { method: 'POST' });
        toast('Paused — browser is open, interact manually then click Resume', 'info');
        loadInProgress();
        loadReviewQueue();
    } catch (e) {
        toast('Failed to pause', 'error');
    }
}

async function resumeApply(appId) {
    try {
        // Try to unfreeze the live paused task first.
        const result = await api('/api/applications/apply/resume', { method: 'POST' });
        if (!result.live) {
            // No live task — fall back to starting a fresh apply.
            await api(`/api/applications/${appId}/apply`, { method: 'POST' });
        }
        toast('Resuming application...', 'success');
        loadInProgress();
        loadReviewQueue();
    } catch (e) {
        toast('Failed to resume: ' + e.message, 'error');
    }
}

async function requeueApplication(appId) {
    if (!(await appConfirm('Move this application back to the review queue?'))) return;
    try {
        await api(`/api/applications/${appId}/requeue`, { method: 'POST' });
        document.getElementById(`submitted-${appId}`)?.remove();
        loadReviewQueue();
    } catch (e) {
        appAlert('Failed to requeue: ' + e.message);
    }
}

async function retryAutoApply(appId) {
    if (!(await appConfirm('Retry auto-apply for this application?\n\nThis will attempt auto-apply immediately.'))) return;
    try {
        // Requeue to pending_review first, then trigger apply
        await api(`/api/applications/${appId}/requeue`, { method: 'POST' });
        await api(`/api/applications/${appId}/apply`, { method: 'POST' });
        toast('Retrying auto-apply...', 'info');
        document.getElementById(`failed-${appId}`)?.remove();
        loadReviewQueue();
    } catch (e) {
        toast('Retry failed: ' + e.message, 'error');
    }
}

function editContent(appId) {
    const contentEl = _getContentEl(appId);
    const app = _getAppData(appId);
    if (!contentEl || !app) return;

    const tab = _getCurrentTab(appId);
    if (tab === 'job') {
        toast('Job description is read-only', 'info');
        return;
    }

    const current = tab === 'resume'
        ? (app.resume_content || '')
        : (app.cover_letter_content || '');
    const label = tab === 'resume' ? 'Resume' : 'Cover Letter';

    contentEl.innerHTML = `
        <textarea id="edit-${appId}" class="edit-textarea">${escapeHtml(current)}</textarea>
        <div class="edit-actions">
            <button class="btn btn-primary btn-sm" onclick="saveEdit('${appId}', '${tab}')">Save ${label}</button>
            <button class="btn btn-secondary btn-sm" onclick="cancelEdit('${appId}')">Cancel</button>
        </div>
    `;
    const textarea = document.getElementById(`edit-${appId}`);
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

async function saveEdit(appId, tab) {
    const textarea = document.getElementById(`edit-${appId}`);
    const newContent = textarea.value;
    const app = _getAppData(appId);
    if (!app) return;

    const body = {};
    if (tab === 'resume') {
        body.resume_content = newContent;
        body.cover_letter_content = app.cover_letter_content || '';
    } else {
        body.resume_content = app.resume_content || '';
        body.cover_letter_content = newContent;
    }

    try {
        await api(`/api/applications/${appId}/content`, {
            method: 'PATCH',
            body: JSON.stringify(body),
        });

        if (tab === 'resume') app.resume_content = newContent;
        else app.cover_letter_content = newContent;

        const contentEl = _getContentEl(appId);
        contentEl.textContent = newContent;
        const label = tab === 'resume' ? 'Resume' : 'Cover Letter';
        toast(`${label} updated \u2014 DOCX regenerated`, 'success');
    } catch (e) {
        toast('Failed to save changes', 'error');
    }
}

function cancelEdit(appId) {
    const contentEl = _getContentEl(appId);
    const app = _getAppData(appId);
    if (!contentEl || !app) return;

    const tab = _getCurrentTab(appId);
    if (tab === 'resume') contentEl.textContent = app.resume_content || '';
    else if (tab === 'cover') contentEl.textContent = app.cover_letter_content || '';
    else contentEl.textContent = app.description || '';
}

function openAiEditModal(appId, target) {
    const app = _getAppData(appId);
    if (!app) { toast('Application data not found', 'error'); return; }

    const initialTarget = target || (_getCurrentTab(appId) === 'cover' ? 'cover_letter' : 'resume');
    const existing = document.getElementById('ai-edit-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'ai-edit-modal';
    modal.className = 'screenshot-modal';
    modal.dataset.appId = appId;
    modal.dataset.target = initialTarget;

    modal.innerHTML = `
        <div class="screenshot-modal-backdrop" onclick="closeAiEditModal()"></div>
        <div class="screenshot-modal-content" style="max-width:1100px;width:90vw;display:flex;flex-direction:column;max-height:90vh">
            <div class="screenshot-modal-header">
                <span class="screenshot-modal-title" id="ai-edit-title"></span>
                <button class="screenshot-modal-close" onclick="closeAiEditModal()">&times;</button>
            </div>
            <div style="padding:12px 16px;border-bottom:1px solid var(--border);display:flex;gap:16px;align-items:center;flex-wrap:wrap">
                <label style="display:flex;align-items:center;gap:6px;font-size:13px">
                    <input type="radio" name="ai-edit-target" value="resume" onchange="_aiEditSwitchTarget('resume')"> Resume
                </label>
                <label style="display:flex;align-items:center;gap:6px;font-size:13px">
                    <input type="radio" name="ai-edit-target" value="cover_letter" onchange="_aiEditSwitchTarget('cover_letter')"> Cover Letter
                </label>
                <span style="display:inline-flex;align-items:center;gap:6px;font-size:13px;margin-left:12px;padding-left:12px;border-left:1px solid var(--border)">
                    <span style="color:var(--text-muted)">Model:</span>
                    <label style="display:flex;align-items:center;gap:4px">
                        <input type="radio" name="ai-edit-tier" value="fast"> Fast
                    </label>
                    <label style="display:flex;align-items:center;gap:4px">
                        <input type="radio" name="ai-edit-tier" value="strong"> Strong
                    </label>
                </span>
                <span style="display:inline-flex;align-items:center;gap:6px;font-size:13px;padding-left:12px;border-left:1px solid var(--border)">
                    <span style="color:var(--text-muted)">Honesty:</span>
                    <select id="ai-edit-honesty" style="background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:12px">
                        <option value="honest">Honest</option>
                        <option value="tailored">Tailored</option>
                        <option value="embellished">Embellished</option>
                        <option value="fabricated">Fabricated</option>
                    </select>
                </span>
                <span id="ai-edit-status" style="margin-left:auto;font-size:12px;color:var(--text-muted)"></span>
            </div>
            <div style="padding:12px 16px;border-bottom:1px solid var(--border)">
                <label style="display:block;font-size:12px;margin-bottom:4px;color:var(--text-muted)">Revision instructions</label>
                <textarea id="ai-edit-instructions" class="edit-textarea" style="min-height:70px;width:100%"
                    placeholder="e.g. Make the summary one sentence and emphasize Python. Tighten bullets to two lines each."></textarea>
                <div style="margin-top:8px;display:flex;gap:8px">
                    <button class="btn btn-primary btn-sm" id="ai-edit-generate-btn" onclick="runAiRevision()">Generate</button>
                    <button class="btn btn-secondary btn-sm" onclick="closeAiEditModal()">Cancel</button>
                </div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);flex:1;overflow:hidden">
                <div style="background:var(--bg-primary);display:flex;flex-direction:column;overflow:hidden">
                    <div style="padding:6px 12px;font-size:11px;text-transform:uppercase;color:var(--text-muted);border-bottom:1px solid var(--border)">Original</div>
                    <pre id="ai-edit-original" style="flex:1;overflow:auto;padding:12px;margin:0;white-space:pre-wrap;font-size:12px"></pre>
                </div>
                <div style="background:var(--bg-primary);display:flex;flex-direction:column;overflow:hidden">
                    <div style="padding:6px 12px;font-size:11px;text-transform:uppercase;color:var(--text-muted);border-bottom:1px solid var(--border)">Revised (editable)</div>
                    <textarea id="ai-edit-revised" style="flex:1;padding:12px;border:0;background:var(--bg-primary);color:var(--text-primary);font-family:inherit;font-size:12px;resize:none;white-space:pre-wrap" placeholder="Click Generate to produce a revision..."></textarea>
                </div>
            </div>
            <div style="padding:12px 16px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end">
                <button class="btn btn-secondary btn-sm" onclick="closeAiEditModal()">Discard</button>
                <button class="btn btn-green btn-sm" id="ai-edit-accept-btn" onclick="acceptAiRevision()" disabled>Accept & Save</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    modal._keyHandler = (e) => { if (e.key === 'Escape') closeAiEditModal(); };
    document.addEventListener('keydown', modal._keyHandler);

    // Initialize model-tier toggle: localStorage > global default > "strong"
    const savedTier = localStorage.getItem('aiEditModelTier');
    const initialTier = (savedTier === 'fast' || savedTier === 'strong')
        ? savedTier
        : (_aiEditDefaultTier || 'strong');
    modal.querySelectorAll('input[name="ai-edit-tier"]').forEach(r => {
        r.checked = (r.value === initialTier);
        r.addEventListener('change', () => {
            if (r.checked) localStorage.setItem('aiEditModelTier', r.value);
        });
    });

    // Initialize honesty selector: localStorage > global default > "honest"
    const validHonesty = new Set(['honest', 'tailored', 'embellished', 'fabricated']);
    const savedHonesty = localStorage.getItem('aiEditHonestyLevel');
    const initialHonesty = validHonesty.has(savedHonesty)
        ? savedHonesty
        : (_aiEditDefaultHonesty || 'honest');
    const honestySel = document.getElementById('ai-edit-honesty');
    if (honestySel) {
        honestySel.value = initialHonesty;
        honestySel.addEventListener('change', () => {
            localStorage.setItem('aiEditHonestyLevel', honestySel.value);
        });
    }

    _aiEditSyncTarget(initialTarget);
}

function _aiEditSyncTarget(target) {
    const modal = document.getElementById('ai-edit-modal');
    if (!modal) return;
    const appId = modal.dataset.appId;
    const app = _getAppData(appId);
    if (!app) return;
    modal.dataset.target = target;

    const isResume = target === 'resume';
    const label = isResume ? 'Resume' : 'Cover Letter';
    document.getElementById('ai-edit-title').textContent = `AI Edit — ${label}`;
    const radios = modal.querySelectorAll('input[name="ai-edit-target"]');
    radios.forEach(r => { r.checked = (r.value === target); });

    const original = isResume ? (app.resume_content || '') : (app.cover_letter_content || '');
    document.getElementById('ai-edit-original').textContent = original;
    document.getElementById('ai-edit-revised').value = '';
    document.getElementById('ai-edit-accept-btn').disabled = true;
    document.getElementById('ai-edit-status').textContent = '';
}

function _aiEditSwitchTarget(target) {
    _aiEditSyncTarget(target);
}

async function runAiRevision() {
    const modal = document.getElementById('ai-edit-modal');
    if (!modal) return;
    const appId = modal.dataset.appId;
    const target = modal.dataset.target;
    const instructions = document.getElementById('ai-edit-instructions').value.trim();
    if (!instructions) { toast('Enter revision instructions first', 'info'); return; }

    const tierRadio = modal.querySelector('input[name="ai-edit-tier"]:checked');
    const tier = tierRadio ? tierRadio.value : (_aiEditDefaultTier || 'strong');

    const honestySel = document.getElementById('ai-edit-honesty');
    const honestyLevel = honestySel ? honestySel.value : (_aiEditDefaultHonesty || 'honest');

    const genBtn = document.getElementById('ai-edit-generate-btn');
    const status = document.getElementById('ai-edit-status');
    const acceptBtn = document.getElementById('ai-edit-accept-btn');
    genBtn.disabled = true;
    genBtn.textContent = 'Generating...';
    status.textContent = `Calling local LLM (${tier}, ${honestyLevel}) — this may take 20–60s.`;
    acceptBtn.disabled = true;

    try {
        const res = await api(`/api/applications/${appId}/revise`, {
            method: 'POST',
            body: JSON.stringify({ target, instructions, model_tier: tier, honesty_level: honestyLevel }),
        });
        document.getElementById('ai-edit-revised').value = res.revised_content || '';
        acceptBtn.disabled = !(res.revised_content || '').trim();
        status.textContent = 'Revision ready — review and edit before accepting.';
        genBtn.textContent = 'Re-generate';
    } catch (e) {
        status.textContent = '';
        toast('Revision failed: ' + (e.message || 'unknown error'), 'error');
        genBtn.textContent = 'Generate';
    } finally {
        genBtn.disabled = false;
    }
}

async function acceptAiRevision() {
    const modal = document.getElementById('ai-edit-modal');
    if (!modal) return;
    const appId = modal.dataset.appId;
    const target = modal.dataset.target;
    const revised = document.getElementById('ai-edit-revised').value;
    if (!revised.trim()) { toast('Nothing to save', 'info'); return; }

    const app = _getAppData(appId);
    if (!app) return;

    const body = {
        resume_content: target === 'resume' ? revised : (app.resume_content || ''),
        cover_letter_content: target === 'cover_letter' ? revised : (app.cover_letter_content || ''),
    };

    const acceptBtn = document.getElementById('ai-edit-accept-btn');
    acceptBtn.disabled = true;
    try {
        await api(`/api/applications/${appId}/content`, {
            method: 'PATCH',
            body: JSON.stringify(body),
        });
        if (target === 'resume') app.resume_content = revised;
        else app.cover_letter_content = revised;

        const contentEl = _getContentEl(appId);
        const currentTab = _getCurrentTab(appId);
        if (contentEl && ((target === 'resume' && currentTab === 'resume') || (target === 'cover_letter' && currentTab === 'cover'))) {
            contentEl.textContent = revised;
        }

        const label = target === 'resume' ? 'Resume' : 'Cover Letter';
        toast(`${label} updated — DOCX regenerated`, 'success');
        closeAiEditModal();
    } catch (e) {
        toast('Failed to save revision: ' + (e.message || ''), 'error');
        acceptBtn.disabled = false;
    }
}

function closeAiEditModal() {
    const modal = document.getElementById('ai-edit-modal');
    if (!modal) return;
    if (modal._keyHandler) document.removeEventListener('keydown', modal._keyHandler);
    modal.remove();
}

async function autoApply(appId) {
    try {
        await api(`/api/applications/${appId}/apply`, { method: 'POST' });
        toast('Apply triggered!', 'success');
        const card = document.getElementById(`review-${appId}`);
        if (card) {
            const actions = card.querySelector('.review-card-actions');
            if (actions) {
                const forceBtn = document.createElement('button');
                forceBtn.className = 'btn btn-danger btn-sm';
                forceBtn.textContent = 'Force Stop';
                forceBtn.title = 'Stop automation and close browser';
                forceBtn.onclick = () => forceStopApply();

                const pauseBtn = document.createElement('button');
                pauseBtn.className = 'btn btn-warning btn-sm';
                pauseBtn.textContent = 'Pause';
                pauseBtn.title = 'Freeze automation — browser stays open for manual interaction';
                pauseBtn.onclick = () => pauseApply();

                actions.prepend(pauseBtn);
                actions.prepend(forceBtn);
            }
            const pill = card.querySelector('.pill');
            if (pill) {
                pill.className = 'pill pill-applying';
                pill.textContent = 'Applying...';
            }
        }
    } catch (e) {
        toast('Failed to apply', 'error');
    }
}

async function markAppApplied(appId) {
    if (!(await appConfirm('Mark this application as manually applied?'))) return;
    try {
        await api(`/api/applications/${appId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'applied' }),
        });
        toast('Marked as applied!', 'success');
        document.getElementById(`review-${appId}`)?.remove();
    } catch (e) {
        toast('Failed to mark as applied: ' + e.message, 'error');
    }
}

async function rejectApp(appId) {
    try {
        await api(`/api/applications/${appId}/reject`, { method: 'POST' });
        toast('Application rejected', 'info');
        document.getElementById(`review-${appId}`).remove();
    } catch (e) {
        toast('Failed to reject', 'error');
    }
}

async function bulkReject() {
    const apps = window._reviewApps || {};
    const ids = Object.keys(apps);
    for (const id of ids) {
        try { await api(`/api/applications/${id}/reject`, { method: 'POST' }); } catch {}
    }
    toast(`Rejected ${ids.length} applications`, 'info');
    loadReviewQueue();
}

// ---- Experience / Education Editors ----
function renderExperience(entries) {
    const list = document.getElementById('experience-list');
    list.innerHTML = '';
    (entries || []).forEach((exp, i) => {
        const div = document.createElement('div');
        div.className = 'exp-entry';
        div.dataset.index = i;
        div.innerHTML = `
            <div class="exp-header">
                <strong style="font-size:14px">${exp.title || 'New Position'} ${exp.company ? '\u2014 ' + exp.company : ''}</strong>
                <label style="font-size:12px;display:flex;align-items:center;gap:4px;margin-left:auto;margin-right:8px;cursor:pointer" title="Always include this role on tailored resumes, even if the relevance cap would otherwise drop it.">
                    <input type="checkbox" data-field="pinned" ${exp.pinned ? 'checked' : ''}> Pin to resume
                </label>
                <button onclick="removeExperience(${i})" title="Remove">\u2715</button>
            </div>
            <div class="exp-fields">
                <div><label>Title</label><input type="text" data-field="title" value="${esc(exp.title || '')}"></div>
                <div><label>Company</label><input type="text" data-field="company" value="${esc(exp.company || '')}"></div>
                <div><label>Start Date</label><input type="text" data-field="start_date" value="${esc(exp.start_date || '')}" placeholder="YYYY-MM-DD"></div>
                <div><label>End Date</label><input type="text" data-field="end_date" value="${esc(exp.end_date || '')}" placeholder="Present"></div>
            </div>
            <label>Bullet Points</label>
            <div class="bullet-list" data-exp="${i}">
                ${(exp.bullets || []).map((b, j) => `
                    <div class="bullet-row">
                        <textarea data-bullet="${j}">${esc(b)}</textarea>
                        <button onclick="removeBullet(${i},${j})" title="Remove">\u2715</button>
                    </div>
                `).join('')}
            </div>
            <button class="btn btn-sm" onclick="addBullet(${i})" style="margin-top:4px;font-size:12px;padding:3px 10px">+ Bullet</button>
        `;
        list.appendChild(div);
    });
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function getExperienceData() {
    const entries = [];
    document.querySelectorAll('.exp-entry').forEach(div => {
        const exp = {
            title: div.querySelector('[data-field="title"]').value,
            company: div.querySelector('[data-field="company"]').value,
            start_date: div.querySelector('[data-field="start_date"]').value,
            end_date: div.querySelector('[data-field="end_date"]').value,
            pinned: !!div.querySelector('[data-field="pinned"]')?.checked,
            bullets: [],
        };
        div.querySelectorAll('[data-bullet]').forEach(ta => {
            const v = ta.value.trim();
            if (v) exp.bullets.push(v);
        });
        entries.push(exp);
    });
    return entries;
}

function addExperience() {
    const current = getExperienceData();
    current.push({ title: '', company: '', start_date: '', end_date: 'Present', pinned: false, bullets: [''] });
    renderExperience(current);
}

function removeExperience(i) {
    const current = getExperienceData();
    current.splice(i, 1);
    renderExperience(current);
}

function addBullet(expIdx) {
    const current = getExperienceData();
    current[expIdx].bullets.push('');
    renderExperience(current);
    const bullets = document.querySelectorAll(`.exp-entry[data-index="${expIdx}"] [data-bullet]`);
    if (bullets.length) bullets[bullets.length - 1].focus();
}

function removeBullet(expIdx, bulletIdx) {
    const current = getExperienceData();
    current[expIdx].bullets.splice(bulletIdx, 1);
    renderExperience(current);
}

function renderEducation(entries) {
    const list = document.getElementById('education-list');
    list.innerHTML = '';
    (entries || []).forEach((edu, i) => {
        const div = document.createElement('div');
        div.className = 'edu-entry';
        div.dataset.index = i;
        div.innerHTML = `
            <div class="edu-header">
                <strong style="font-size:14px">${edu.degree || 'New Entry'} ${edu.school ? '\u2014 ' + edu.school : ''}</strong>
                <button onclick="removeEducation(${i})" title="Remove">\u2715</button>
            </div>
            <div class="edu-fields">
                <div><label>Degree / Program</label><input type="text" data-field="degree" value="${esc(edu.degree || '')}"></div>
                <div><label>School</label><input type="text" data-field="school" value="${esc(edu.school || '')}"></div>
                <div><label>Year</label><input type="text" data-field="year" value="${esc(edu.year || '')}" placeholder="2024"></div>
                <div></div>
            </div>
        `;
        list.appendChild(div);
    });
}

function getEducationData() {
    const entries = [];
    document.querySelectorAll('#education-list .edu-entry').forEach(div => {
        entries.push({
            degree: div.querySelector('[data-field="degree"]').value,
            school: div.querySelector('[data-field="school"]').value,
            year: div.querySelector('[data-field="year"]').value,
        });
    });
    return entries;
}

function addEducation() {
    const current = getEducationData();
    current.push({ degree: '', school: '', year: '' });
    renderEducation(current);
}

function removeEducation(i) {
    const current = getEducationData();
    current.splice(i, 1);
    renderEducation(current);
}

function renderReferences(entries) {
    const list = document.getElementById('references-list');
    list.innerHTML = '';
    (entries || []).forEach((ref, i) => {
        const div = document.createElement('div');
        div.className = 'ref-entry edu-entry';
        div.dataset.index = i;
        div.innerHTML = `
            <div class="edu-header">
                <strong style="font-size:14px">${esc(ref.name || 'New Reference')} ${ref.position ? '— ' + esc(ref.position) : ''}</strong>
                <button onclick="removeReference(${i})" title="Remove">✕</button>
            </div>
            <div class="edu-fields">
                <div><label>Name</label><input type="text" data-field="name" value="${esc(ref.name || '')}"></div>
                <div><label>Position / Relationship</label><input type="text" data-field="position" value="${esc(ref.position || '')}"></div>
                <div><label>Email</label><input type="email" data-field="email" value="${esc(ref.email || '')}"></div>
                <div><label>Phone</label><input type="text" data-field="phone" value="${esc(ref.phone || '')}"></div>
            </div>
        `;
        list.appendChild(div);
    });
}

function getReferencesData() {
    const entries = [];
    document.querySelectorAll('#references-list .ref-entry').forEach(div => {
        entries.push({
            name: div.querySelector('[data-field="name"]').value,
            position: div.querySelector('[data-field="position"]').value,
            email: div.querySelector('[data-field="email"]').value,
            phone: div.querySelector('[data-field="phone"]').value,
        });
    });
    return entries;
}

function addReference() {
    const current = getReferencesData();
    current.push({ name: '', position: '', email: '', phone: '' });
    renderReferences(current);
}

function removeReference(i) {
    const current = getReferencesData();
    current.splice(i, 1);
    renderReferences(current);
}

