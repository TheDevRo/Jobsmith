// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ---- Advanced Filters Toggle ----
function toggleAdvancedFilters() {
    const panel = document.getElementById('filter-advanced');
    const btn = document.getElementById('filter-toggle');
    const visible = panel.style.display !== 'none';
    panel.style.display = visible ? 'none' : '';
    btn.classList.toggle('expanded', !visible);
}

// ---- Inline verdict + chip iconography (stroke-2 inline SVG, no assets) ----
const VERDICT_X_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
const VERDICT_CHECK_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>';
const CHIP_X_SVG = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

// ---- Filter chips ----
// Derived purely from the same DOM inputs loadJobs() reads — single source of
// truth, no separate state object. One chip per active non-default filter; the
// ✕ resets just that input to its default and reloads.
const _SOURCE_LABELS = {
    adzuna: 'Adzuna', arbeitnow: 'Arbeitnow', ashby: 'Ashby', greenhouse: 'Greenhouse',
    indeed: 'Indeed', lever: 'Lever', linkedin: 'LinkedIn', recruitee: 'Recruitee',
    remoteok: 'RemoteOK', usajobs: 'USAJobs', weworkremotely: 'WeWorkRemotely', workable: 'Workable',
};
const _STATUS_FILTER_LABELS = {
    discovered: 'Discovered', tailoring: 'Tailoring', review: 'In Review',
    applied: 'Applied', manual: 'Manual Apply', rejected: 'Rejected',
};
const _SORT_LABELS = {
    'date_discovered-desc': 'Newest ↓', 'date_discovered-asc': 'Oldest ↑',
    'fit_score-desc': 'Fit ↓', 'fit_score-asc': 'Fit ↑',
    'applied_at-desc': 'Applied ↓', 'salary_min-desc': 'Salary ↓',
    'quality_score-desc': 'Quality ↓', 'title-asc': 'Title A–Z', 'company-asc': 'Company A–Z',
};

// Returns [{key, label}] for every active non-default filter. Pure (reads DOM,
// no side effects) so the test harness can assert on it directly.
function buildFilterChips() {
    const chips = [];
    const val = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
    const checked = (id) => { const el = document.getElementById(id); return !!(el && el.checked); };

    const search = (val('filter-search') || '').trim();
    if (search) chips.push({ key: 'search', label: `“${search}”` });
    const location = (val('filter-location') || '').trim();
    if (location) chips.push({ key: 'location', label: `Location: ${location}` });
    const company = (val('filter-company') || '').trim();
    if (company) chips.push({ key: 'company', label: `Company: ${company}` });
    const source = val('filter-source');
    if (source) chips.push({ key: 'source', label: `Source: ${_SOURCE_LABELS[source] || source}` });
    const status = val('filter-status');
    if (status) chips.push({ key: 'status', label: `Status: ${_STATUS_FILTER_LABELS[status] || status}` });
    if (checked('filter-remote')) chips.push({ key: 'remote', label: 'Remote only' });
    if (checked('filter-easy-apply')) chips.push({ key: 'easy-apply', label: 'Easy Apply only' });
    const minScore = parseInt(val('filter-score')) || 0;
    if (minScore > 0) chips.push({ key: 'score', label: `Score ≥ ${minScore}` });
    const minSalary = parseInt(val('filter-salary')) || 0;
    if (minSalary > 0) chips.push({ key: 'salary', label: `Salary ≥ $${minSalary.toLocaleString()}` });
    const dateFrom = val('filter-date-from');
    if (dateFrom) chips.push({ key: 'date-from', label: `From ${dateFrom}` });
    const dateTo = val('filter-date-to');
    if (dateTo) chips.push({ key: 'date-to', label: `To ${dateTo}` });
    return chips;
}

function renderFilterChips() {
    const el = document.getElementById('filter-chips');
    if (!el) return;
    const chips = buildFilterChips();
    let html = chips.map(c =>
        `<button type="button" class="fchip" onclick="resetFilter('${c.key}')" aria-label="Remove filter: ${escapeHtml(c.label)}"><span>${escapeHtml(c.label)}</span>${CHIP_X_SVG}</button>`
    ).join('');
    html += `<button type="button" class="fchip addf" onclick="toggleAdvancedFilters()">+ Filter</button>`;
    const sortVal = (document.getElementById('filter-sort') || {}).value || '';
    const sortLabel = _SORT_LABELS[sortVal] || sortVal || '—';
    html += `<button type="button" class="fchip sortlbl" onclick="focusSortSelect()">Sort: ${escapeHtml(sortLabel)}</button>`;
    el.innerHTML = html;
}

function resetFilter(key) {
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    const uncheck = (id) => { const el = document.getElementById(id); if (el) el.checked = false; };
    switch (key) {
        case 'search': set('filter-search', ''); break;
        case 'location': set('filter-location', ''); break;
        case 'company': set('filter-company', ''); break;
        case 'source': set('filter-source', ''); break;
        case 'status': set('filter-status', ''); break;
        case 'remote': uncheck('filter-remote'); break;
        case 'easy-apply': uncheck('filter-easy-apply'); break;
        case 'score': set('filter-score', 0); { const v = document.getElementById('score-val'); if (v) v.textContent = '0'; } break;
        case 'salary': set('filter-salary', 0); { const v = document.getElementById('salary-val'); if (v) v.textContent = '0'; } break;
        case 'date-from': set('filter-date-from', ''); break;
        case 'date-to': set('filter-date-to', ''); break;
    }
    currentJobsPage = 0;
    loadJobs();
}

function focusSortSelect() {
    const sel = document.getElementById('filter-sort');
    if (!sel) return;
    // Advanced drawer isn't where sort lives (it's in the primary row), so just
    // focus it; on browsers that support it, showPicker() opens the dropdown.
    sel.focus();
    if (typeof sel.showPicker === 'function') { try { sel.showPicker(); } catch (e) { /* not user-gesture */ } }
}

// ---- Job Feed ----
async function loadJobs() {
    const source = document.getElementById('filter-source').value;
    const status = document.getElementById('filter-status').value;
    const minScore = parseInt(document.getElementById('filter-score').value) || 0;
    const search = document.getElementById('filter-search').value;
    const location = document.getElementById('filter-location').value;
    const company = document.getElementById('filter-company').value;
    const remoteOnly = document.getElementById('filter-remote').checked;
    const easyApplyOnly = document.getElementById('filter-easy-apply').checked;
    const dateFrom = document.getElementById('filter-date-from').value;
    const dateTo = document.getElementById('filter-date-to').value;
    const minSalary = parseInt(document.getElementById('filter-salary').value) || 0;
    const sortVal = document.getElementById('filter-sort').value;
    const [sortBy, sortDir] = sortVal.split('-');

    const maxScore = parseInt(document.getElementById('filter-max-score').value) || null;
    const unscoredOnly = document.getElementById('filter-unscored-only').value === '1';

    let params = `?limit=${JOBS_PER_PAGE}&offset=${currentJobsPage * JOBS_PER_PAGE}`;
    if (source) params += `&source=${source}`;
    if (status) params += `&status=${status}`;
    if (unscoredOnly) {
        params += `&unscored_only=true`;
    } else {
        if (minScore > 0) params += `&min_score=${minScore}`;
        if (maxScore !== null) params += `&max_score=${maxScore}`;
    }
    if (search) params += `&search=${encodeURIComponent(search)}`;
    if (location) params += `&location=${encodeURIComponent(location)}`;
    if (company) params += `&company=${encodeURIComponent(company)}`;
    if (remoteOnly) params += `&remote_only=true`;
    if (easyApplyOnly) params += `&easy_apply_only=true`;
    if (dateFrom) params += `&date_from=${dateFrom}`;
    if (dateTo) params += `&date_to=${dateTo}`;
    if (minSalary > 0) params += `&min_salary=${minSalary}`;
    const includeEstimated = document.getElementById('filter-include-estimated');
    if (includeEstimated && includeEstimated.checked) params += `&include_estimated=true`;
    if (sortBy) params += `&sort_by=${sortBy}&sort_dir=${sortDir}`;

    // Reflect the active filters as chips on every load (single source of truth).
    renderFilterChips();

    try {
        const data = await api(`/api/jobs${params}`);
        renderJobs(data.jobs, data.total);
    } catch (e) {
        renderError('jobs-list', 'Failed to load jobs.', loadJobs);
        document.getElementById('jobs-pagination').innerHTML = '';
    }
}

function qualityBadge(job) {
    // Ghost-risk badge: only shown when a quality report exists and the
    // hiring-likelihood score is below 70. Amber 50-69, red below 50.
    const report = safeParseJSON(job.quality_report, null);
    if (!report || typeof report.score !== 'number' || report.score >= 70) return '';
    const cls = report.score >= 50 ? 'quality-badge-amber' : 'quality-badge-red';
    return `<span class="quality-badge ${cls}" title="Posting-quality signals suggest this may be a ghost job">⚠ Quality ${Math.round(report.score)}</span>`;
}

function appliedBadge(job) {
    // A repost, or the same role picked up from another board. The dedup only
    // removes duplicates within a single fetch, and the DB only hides the exact
    // job row you applied to — so these otherwise come back looking brand new.
    if (!job.already_applied) return '';
    return '<span class="quality-badge quality-badge-amber" title="You already applied to this role at this company — this looks like a repost or a cross-posting">↩ Already applied</span>';
}

function renderQualitySection(job) {
    const report = safeParseJSON(job.quality_report, null);
    if (!report || !Array.isArray(report.signals) || report.signals.length === 0) return '';
    const rows = report.signals.map(s => `
        <div class="quality-signal-row">
            <span class="quality-signal-name">${escapeHtml(s.signal)}</span>
            <span class="quality-signal-impact ${s.impact >= 0 ? 'positive' : ''}">${s.impact > 0 ? '+' : ''}${s.impact}</span>
            <span class="quality-signal-detail">${escapeHtml(s.detail)}</span>
        </div>`).join('');
    return `
        <div class="detail-section">
            <h4>Posting Quality — ${Math.round(report.score)}/100</h4>
            <div class="quality-signal-list">${rows}</div>
        </div>`;
}

function renderJobs(jobs, total) {
    const container = document.getElementById('jobs-list');
    if (jobs.length === 0) {
        container.innerHTML = '<p class="placeholder">Nothing to scout right now. <a href="#dashboard" style="color:var(--accent)">Fetch new jobs</a> from Activity, or adjust your filters.</p>';
        document.getElementById('jobs-pagination').innerHTML = '';
        clearDetailPane();
        return;
    }

    // Cache jobs for detail panel
    window._currentJobs = {};
    jobs.forEach(j => { window._currentJobs[j.id] = j; });

    document.getElementById('select-all-jobs').checked = false;
    updateSelectedCount();
    if (selectModeActive) container.classList.add('select-mode');

    // Handoff from the dashboard's "Apply Today" card: select the job it sent us
    // to, once the list it lives in actually exists.
    if (window._pendingJobSelection) {
        const wanted = window._pendingJobSelection;
        window._pendingJobSelection = null;
        if (window._currentJobs[wanted]) {
            setTimeout(() => selectJob(wanted), 0);
        }
    }

    container.innerHTML = jobs.map(job => {
        const status = job.app_status || job.status;
        const statusLabel = {tailoring: 'Tailoring...', applying: 'Applying...', applied: 'Applied', discovered: 'New', shortlisted: 'Shortlisted', passed: 'Passed', pending_review: 'Pending', approved: 'Approved', rejected: 'Rejected', failed: 'Failed', manual: 'Manual', autofill_complete: 'Autofill Complete', already_applied: 'Already Applied', rate_limited: 'Rate Limited', needs_review: 'Needs Review', paused: 'Paused'}[status] || status;
        const isSelected = job.id === selectedJobId;

        return `
            <div class="job-card ${isSelected ? 'selected' : ''}" onclick="selectJob('${job.id}')" onkeydown="jobCardKeydown(event, '${job.id}')" data-job-id="${job.id}" role="button" tabindex="0" aria-selected="${isSelected ? 'true' : 'false'}" aria-label="${escapeHtml(`${job.title || 'Untitled'} at ${job.company || 'Unknown'}`)}">
                <div class="job-card-header">
                    <div class="job-card-select" onclick="event.stopPropagation()">
                        <input type="checkbox" class="job-checkbox" value="${job.id}" onchange="updateSelectedCount()">
                    </div>
                    <div class="job-card-info">
                        <div class="job-card-title">${escapeHtml(job.title)}</div>
                        <div class="job-card-company">${escapeHtml(job.company || 'Unknown')}</div>
                        <div class="job-card-meta">
                            <span>${escapeHtml(job.location || '')}</span>
                            <span class="source-badge">${escapeHtml(job.source)}</span>
                            ${job.is_easy_apply ? '<span class="easy-apply-badge">Easy Apply</span>' : ''}
                            ${qualityBadge(job)}
                            ${appliedBadge(job)}
                            <span>${timeAgo(job.date_discovered)}</span>
                        </div>
                    </div>
                    <div class="job-card-right">
                        ${renderHeatChip(job.fit_score)}
                        <span class="pill pill-${status}">${statusLabel}</span>
                        ${status === 'discovered' ? `
                        <div class="row-verdicts" onclick="event.stopPropagation()">
                            <button type="button" class="rverdict no" onclick="passJob('${job.id}')" aria-label="Pass" title="Pass  (X or ←)">${VERDICT_X_SVG}</button>
                            <button type="button" class="rverdict yes" onclick="shortlistJob('${job.id}')" aria-label="Shortlist" title="Shortlist  (S or →)">${VERDICT_CHECK_SVG}</button>
                        </div>` : ''}
                        ${job.apply_type === 'external' ? `<button class="btn btn-assist btn-xs" onclick="event.stopPropagation();launchAssist('${job.id}')" title="Open Applicant Assist browser">Assist Me</button>` : ''}
                    </div>
                </div>
            </div>
        `;
    }).join('');

    // Pagination
    const totalPages = Math.ceil(total / JOBS_PER_PAGE);
    const pag = document.getElementById('jobs-pagination');
    if (totalPages <= 1) {
        pag.innerHTML = '';
        return;
    }
    let buttons = '';
    for (let i = 0; i < Math.min(totalPages, 10); i++) {
        buttons += `<button class="${i === currentJobsPage ? 'active' : ''}" onclick="goToPage(${i})">${i + 1}</button>`;
    }
    pag.innerHTML = buttons;
}

// UX-04: job cards are <div role="button" tabindex="0">, so they must respond
// to Enter/Space the way a real button does. Space is preventDefault'd so it
// selects the card instead of scrolling the list.
function jobCardKeydown(ev, jobId) {
    if (ev.key !== 'Enter' && ev.key !== ' ' && ev.key !== 'Spacebar') return;
    // Don't hijack keys aimed at the checkbox/buttons nested inside the card.
    const t = ev.target;
    if (t && t !== ev.currentTarget && t.closest && t.closest('input, button, a')) return;
    ev.preventDefault();
    selectJob(jobId);
}

function selectJob(jobId) {
    selectedJobId = jobId;

    // Highlight selected card
    document.querySelectorAll('.job-card').forEach(c => {
        const isSel = c.dataset.jobId === jobId;
        c.classList.toggle('selected', isSel);
        c.setAttribute('aria-selected', isSel ? 'true' : 'false');
    });

    const job = window._currentJobs[jobId];
    if (!job) return;

    // Track which job the detail pane is actually rendered for, so Enter can
    // distinguish "open this row's detail" from "open the posting" (already open).
    window._detailJobId = jobId;

    const pane = document.getElementById('job-detail-pane');
    const tags = safeParseJSON(job.tags, []);
    const hasScore = job.fit_score !== null && job.fit_score !== undefined && job.fit_score !== '' && !isNaN(Number(job.fit_score)) && Number(job.fit_score) > 0;
    const status = job.app_status || job.status;
    const statusLabel = {tailoring: 'Tailoring...', applying: 'Applying...', applied: 'Applied', discovered: 'New', shortlisted: 'Shortlisted', passed: 'Passed', pending_review: 'Pending', approved: 'Approved', rejected: 'Rejected', failed: 'Failed', manual: 'Manual', autofill_complete: 'Autofill Complete', already_applied: 'Already Applied', rate_limited: 'Rate Limited', needs_review: 'Needs Review', paused: 'Paused'}[status] || status;

    pane.innerHTML = `
        <div class="detail-header" style="display:flex;align-items:flex-start;gap:16px;justify-content:space-between">
            <div style="min-width:0;flex:1">
                <div class="detail-title">${escapeHtml(job.title)}</div>
                <div class="detail-company">${escapeHtml(job.company || 'Unknown')}${job.location ? ' \u2014 ' + escapeHtml(job.location) : ''}</div>
                <div class="detail-meta">
                    <span class="pill pill-${status}">${statusLabel}</span>
                    <span class="source-badge">${escapeHtml(job.source)}</span>
                    ${job.is_easy_apply ? '<span class="easy-apply-badge">Easy Apply</span>' : ''}
                    ${qualityBadge(job)}
                    ${appliedBadge(job)}
                    <span style="font-size:12px;color:var(--text-muted)">${timeAgo(job.date_discovered)}</span>
                </div>
            </div>
            ${hasScore ? renderHeatRing(job.fit_score) : renderHeatChip(null)}
        </div>

        ${renderSalarySection(job)}

        ${renderFitAnalysis(job)}

        ${renderQualitySection(job)}

        ${tags.length ? `
            <div class="detail-section">
                <h4>Tags</h4>
                <div class="detail-tags">${tags.map(t => `<span class="source-badge">${escapeHtml(t)}</span>`).join('')}</div>
            </div>
        ` : ''}

        <div class="detail-section">
            <h4>Description</h4>
            <div class="detail-description">${escapeHtml((job.description || 'No description available').substring(0, 3000))}</div>
        </div>

        <div class="detail-actions">
            <button class="btn btn-secondary btn-sm" onclick="scoreJob('${job.id}')">${job.fit_score ? 'Rescore' : 'Score'}</button>
            <button class="btn btn-primary btn-sm" onclick="tailorJob('${job.id}')">Tailor Resume</button>
            ${job.apply_type === 'external' ? `<button class="btn btn-assist btn-sm" onclick="launchAssist('${job.id}')">Assist Me</button>` : ''}
            ${job.app_id ? `<button class="btn btn-secondary btn-sm" onclick="location.hash='review'">View Application</button>` : ''}
            ${job.apply_type === 'external' ? '' : `<a class="btn btn-secondary btn-sm" href="${escapeHtml(safeHref(job.url))}" target="_blank" rel="noopener" data-jobsmith-open-url data-jobsmith-job-id="${escapeHtml(job.id)}">Open Job URL</a>`}
            ${status !== 'applied' && status !== 'manual' ? `<button class="btn btn-green btn-sm" onclick="markApplied('${job.id}')">Mark Applied</button>` : ''}
            <button class="btn btn-secondary btn-sm" onclick="toggleEmbPanel('${job.id}')">Embellishments</button>
            <button class="btn btn-danger btn-sm" onclick="deleteSingleJob('${job.id}')">Delete</button>
        </div>

        <div id="emb-panel-${job.id}" class="detail-emb-panel" style="display:none"></div>

        ${job.autofill_result ? `
        <div class="detail-section">
            <h4>Autofill Report</h4>
            ${_renderAutofillReport(safeParseJSON(job.autofill_result, null))}
        </div>
        ` : ''}
    `;
}

function renderFitAnalysis(job) {
    const report = safeParseJSON(job.match_report, null);
    if (!job.fit_reasoning && !report) return '';

    const chips = (items, cls) => items && items.length
        ? `<div class="skill-chips">${items.map(s => `<span class="skill-chip ${cls}">${escapeHtml(s)}</span>`).join('')}</div>`
        : '';

    let reportHtml = '';
    if (report) {
        const matched = report.matched_skills || [];
        const missing = report.missing_skills || [];
        const softMatched = report.matched_soft_skills || [];
        const alignment = report.title_alignment;
        reportHtml = `
            ${alignment ? `<div class="match-report-label">Title Alignment: <span class="title-alignment-badge ${alignment}">${alignment}</span></div>` : ''}
            ${matched.length ? `<div class="match-report-label">Skills You Have (${matched.length})</div>${chips(matched, 'matched')}` : ''}
            ${missing.length ? `<div class="match-report-label">Skills Gap (${missing.length})</div>${chips(missing, 'missing')}` : ''}
            ${softMatched.length ? `<div class="match-report-label">Soft Skills</div>${chips(softMatched, 'soft')}` : ''}
        `;
    }

    return `
        <div class="detail-section">
            <h4>Fit Analysis</h4>
            ${job.fit_reasoning ? `<p style="font-size:13px;color:var(--text-secondary);line-height:1.6">${escapeHtml(job.fit_reasoning)}</p>` : ''}
            ${reportHtml}
        </div>
    `;
}

function clearDetailPane() {
    selectedJobId = null;
    window._detailJobId = null;
    const pane = document.getElementById('job-detail-pane');
    if (pane) {
        pane.innerHTML = `
            <div class="detail-empty">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:0.3"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>
                <p>Select a job to view details</p>
            </div>
        `;
    }
}

function goToPage(page) {
    currentJobsPage = page;
    clearDetailPane();
    loadJobs();
}

function debounceSearch() {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => {
        currentJobsPage = 0;
        loadJobs();
    }, 400);
}

function clearFilters() {
    document.getElementById('filter-search').value = '';
    document.getElementById('filter-location').value = '';
    document.getElementById('filter-company').value = '';
    document.getElementById('filter-source').value = '';
    document.getElementById('filter-status').value = '';
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
    loadJobs();
}

async function scoreJob(jobId) {
    try {
        await api(`/api/jobs/${jobId}/score`, { method: 'POST' });
        toast('Scoring started for this job!', 'success');
    } catch (e) {
        toast('Failed to start scoring', 'error');
    }
}

function updateScoreBtnLabel() {
    const btn = document.getElementById('score-btn');
    const cb = document.getElementById('score-rescore-cb');
    if (btn && !btn.disabled) btn.textContent = (cb && cb.checked) ? 'Rescore Jobs' : 'Score Unscored';
}

async function scoreAll() {
    try {
        const sel = document.getElementById('score-limit-select');
        const limitVal = sel ? sel.value : '';
        const rescore = document.getElementById('score-rescore-cb')?.checked;
        const params = new URLSearchParams();
        if (limitVal) params.set('limit', limitVal);
        if (rescore) params.set('rescore', 'true');
        const qs = params.toString();
        await api(`/api/jobs/score-batch${qs ? '?' + qs : ''}`, { method: 'POST' });
        const btn = document.getElementById('score-btn');
        btn.disabled = true;
        const verb = rescore ? 'Rescoring' : 'Scoring';
        const label = limitVal ? `${verb} (${limitVal})...` : `${verb} all...`;
        btn.textContent = label;
        document.getElementById('score-stop-btn').style.display = '';
        toast('Batch scoring started!', 'success');
        showScoreStatus(true);
        document.getElementById('score-spinner').style.display = '';
        document.getElementById('score-status-text').textContent = 'Starting…';
        document.getElementById('score-status-current').textContent = '';
        document.getElementById('score-progress-bar').style.width = '0%';
        startScorePoll();
        _startOpsPoll();
    } catch (e) {
        toast('Failed to start batch scoring', 'error');
    }
}

async function cancelScoreBatch() {
    try {
        await api('/api/jobs/score-batch/cancel', { method: 'POST' });
        toast('Stopping batch scoring...', 'info');
    } catch (e) {
        toast('Failed to cancel scoring', 'error');
    }
}

async function estimateSalariesAll() {
    try {
        const sel = document.getElementById('estimate-salaries-limit-select');
        const limitVal = sel ? sel.value : '';
        const url = limitVal ? `/api/jobs/estimate-salaries?limit=${limitVal}` : '/api/jobs/estimate-salaries';
        await api(url, { method: 'POST' });
        const btn = document.getElementById('estimate-salaries-btn');
        btn.disabled = true;
        btn.textContent = limitVal ? `Estimating (${limitVal})...` : 'Estimating all...';
        document.getElementById('estimate-salaries-stop-btn').style.display = '';
        toast('Salary estimation started!', 'success');
        _startOpsPoll();
    } catch (e) {
        toast('Failed to start salary estimation', 'error');
    }
}

async function cancelEstimateSalaries() {
    try {
        await api('/api/jobs/estimate-salaries/cancel', { method: 'POST' });
        toast('Stopping salary estimation...', 'info');
    } catch (e) {
        toast('Failed to cancel salary estimation', 'error');
    }
}

async function tailorJob(jobId) {
    try {
        await api(`/api/jobs/${jobId}/tailor`, { method: 'POST' });
        toast('Tailoring started for this job!', 'success');
    } catch (e) {
        toast('Failed to start tailoring', 'error');
    }
}

async function markApplied(jobId) {
    if (!(await appConfirm('Mark this job as manually applied?'))) return;
    try {
        await api(`/api/jobs/${jobId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'manual' }),
        });
        toast('Job marked as applied!', 'success');
        loadJobs();
    } catch (e) {
        toast('Failed to mark as applied', 'error');
    }
}

// ---- Inbox scouting (the desktop translation of the iOS swipe deck) ----
// Shortlist/Pass write a free-text job status via the existing PATCH endpoint
// ('shortlisted' surfaces in Pipeline; 'passed' clears it from the Inbox).
// No backend changes — j.status is a free-text column.

// Undo stack for verdicts — a small bounded history of {jobId, prevStatus,
// title} so `u` (keyboard) or a mis-click can restore the previous status.
const _verdictUndo = [];
const _VERDICT_UNDO_MAX = 10;

function _jobSnapshot(jobId) {
    const job = window._currentJobs && window._currentJobs[jobId];
    return {
        status: job ? (job.app_status || job.status || 'discovered') : 'discovered',
        title: job ? (job.title || 'job') : 'job',
    };
}

function _pushVerdictUndo(jobId, prevStatus, title) {
    _verdictUndo.push({ jobId, prevStatus, title });
    if (_verdictUndo.length > _VERDICT_UNDO_MAX) _verdictUndo.shift();
}

async function shortlistJob(jobId) {
    const snap = _jobSnapshot(jobId);
    try {
        await api(`/api/jobs/${jobId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'shortlisted' }),
        });
        _pushVerdictUndo(jobId, snap.status, snap.title);
        toast('Shortlisted — moved to Pipeline', 'success');
        _afterScout(jobId);
    } catch (e) {
        toast('Failed to shortlist', 'error');
    }
}

async function passJob(jobId) {
    const snap = _jobSnapshot(jobId);
    try {
        await api(`/api/jobs/${jobId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'passed' }),
        });
        _pushVerdictUndo(jobId, snap.status, snap.title);
        toast('Passed', 'info');
        _afterScout(jobId);
    } catch (e) {
        toast('Failed to pass', 'error');
    }
}

// Pop the last verdict and PATCH the previous status back, then reload the list
// and re-select the restored job. Works for both keyboard and button verdicts.
async function undoVerdict() {
    const last = _verdictUndo.pop();
    if (!last) { toast('Nothing to undo', 'info'); return; }
    try {
        await api(`/api/jobs/${last.jobId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: last.prevStatus }),
        });
        toast(`Restored ${last.title}`, 'success');
        selectedJobId = last.jobId;
        await loadJobs();
        if (window._currentJobs && window._currentJobs[last.jobId]) {
            selectJob(last.jobId);
            const card = document.querySelector(`.job-card[data-job-id="${last.jobId}"]`);
            if (card) card.scrollIntoView({ block: 'nearest' });
        }
    } catch (e) {
        _verdictUndo.push(last);
        toast('Failed to undo', 'error');
    }
}

// Optimistically remove the scouted card and advance to the next one, so the
// Inbox feels like working through a deck. Reloads when the list empties.
function _afterScout(jobId) {
    const card = document.querySelector(`.job-card[data-job-id="${jobId}"]`);
    const next = card ? (card.nextElementSibling || card.previousElementSibling) : null;
    if (card) card.remove();
    if (window._currentJobs) delete window._currentJobs[jobId];
    if (next && next.classList.contains('job-card')) {
        selectJob(next.dataset.jobId);
        next.scrollIntoView({ block: 'nearest' });
    } else if (document.querySelectorAll('#jobs-list .job-card').length === 0) {
        loadJobs();
    } else {
        clearDetailPane();
    }
}

function navigateInbox(delta) {
    const cards = Array.from(document.querySelectorAll('#jobs-list .job-card'));
    if (!cards.length) return;
    let idx = cards.findIndex(c => c.dataset.jobId === selectedJobId);
    if (idx < 0) idx = delta > 0 ? -1 : 0;
    idx = Math.min(Math.max(idx + delta, 0), cards.length - 1);
    const card = cards[idx];
    if (card) { selectJob(card.dataset.jobId); card.scrollIntoView({ block: 'nearest' }); }
}

function _selectedIsDiscovered() {
    const job = window._currentJobs && window._currentJobs[selectedJobId];
    return !!(job && (job.app_status || job.status) === 'discovered');
}

// Keyboard scouting — only on the Inbox tab and never while typing in a field.
document.addEventListener('keydown', (e) => {
    if ((location.hash.replace('#', '') || 'jobs') !== 'jobs') return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    switch (e.key) {
        case 'ArrowDown': case 'j': e.preventDefault(); navigateInbox(1); break;
        case 'ArrowUp': case 'k': e.preventDefault(); navigateInbox(-1); break;
        case 'ArrowRight': case 's': case 'S':
            if (selectedJobId && _selectedIsDiscovered()) { e.preventDefault(); shortlistJob(selectedJobId); }
            break;
        case 'ArrowLeft': case 'p': case 'P': case 'x': case 'X':
            if (selectedJobId && _selectedIsDiscovered()) { e.preventDefault(); passJob(selectedJobId); }
            break;
        case 'Enter':
            if (!selectedJobId) break;
            e.preventDefault();
            if (window._detailJobId === selectedJobId) {
                // Already showing this job → open its posting (http/https only).
                const job = window._currentJobs && window._currentJobs[selectedJobId];
                if (job && safeHref(job.url) !== '#') openExternal(job.url);
            } else {
                selectJob(selectedJobId);
            }
            break;
        case 'u': case 'U': e.preventDefault(); undoVerdict(); break;
    }
});

// ---- Applicant Assist ----

async function launchAssist(jobId) {
    toast('Preparing Applicant Assist…', 'info');
    // Tell the extension which job is now active so its sidepanel/autofill
    // can pick up the right resume + answer bank — same hint that the old
    // "Open Job URL" button sent. Fire-and-forget.
    signalActiveJobToExtension(jobId);
    try {
        const data = await api('/api/assist/launch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_id: jobId }),
        });
        if (data && data.mode === 'isolated') {
            toast('Launched in isolated browser', 'success');
            return;
        }
        if (data && data.opened) {
            toast('Opened in your default browser', 'success');
            return;
        }
        if (data && data.launch_url) {
            openExternal(data.launch_url);
            toast('Opening launch page in a new tab', 'info');
            return;
        }
        toast('Apply Assist response was unexpected', 'error');
    } catch (e) {
        toast('Failed to launch Assist: ' + e.message, 'error');
    }
}

function _renderAutofillReport(report) {
    if (!report) {
        return '<p style="color:var(--text-muted);font-size:13px">No autofill report yet — run Applicant Assist to generate one.</p>';
    }
    const filled = (report.filled || []).map(f => `<li>${escapeHtml(f)}</li>`).join('');
    const attention = (report.needs_attention || []).map(f => `<li>${escapeHtml(f)}</li>`).join('');
    return `
        <div class="autofill-report">
            <div class="autofill-report-section">
                <div class="autofill-report-title autofill-ok">&#10003; Fields filled automatically</div>
                <ul class="autofill-list">${filled || '<li class="autofill-none">None</li>'}</ul>
            </div>
            <div class="autofill-report-section">
                <div class="autofill-report-title autofill-warn">&#9888; Fields needing your attention</div>
                <ul class="autofill-list autofill-list-warn">${attention || '<li class="autofill-none">None &#8212; all fields handled!</li>'}</ul>
            </div>
        </div>
    `;
}

