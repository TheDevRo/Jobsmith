// Jobsmith frontend — Phase 3 "Command Deck". Loaded AFTER jobs-actions.js in
// index.html, so every helper it leans on (escapeHtml, safeHref, timeAgo,
// renderHeatChip, formatSalaryRange, api, toast, appConfirm, _pushVerdictUndo,
// undoVerdict, switchReviewView, refreshFunnelCounts, fetchNewJobs, loadJobs …)
// is already a global by the time any deck function runs.
//
// Everything here is ADDITIVE and only active in the "deck" layout
// (getLayout() === 'deck', see core.js). In "classic" layout enterInbox() and
// enterReview() fall straight through to the Phase 1–2 loaders, so the classic
// UI stays pixel-identical.

// ==========================================================================
// Inbox view mode (deck layout only): 'stage' (card deck) | 'list' (Phase-1 UI)
// ==========================================================================
function getInboxView() {
    return localStorage.getItem('jobsmith_inbox_view') === 'list' ? 'list' : 'stage';
}

function setInboxView(mode) {
    localStorage.setItem('jobsmith_inbox_view', mode === 'list' ? 'list' : 'stage');
    enterInbox();
}

function toggleInboxView() {
    setInboxView(getInboxView() === 'stage' ? 'list' : 'stage');
}

// True only when the triage stage owns the Inbox — used to gate the stage
// keyboard handler and to make the classic jobs.js triage handler stand down.
function isInboxStageActive() {
    return isDeckLayout() && getInboxView() === 'stage';
}

// Toggle the classic split-pane off / the stage on (and vice-versa).
function _setInboxStageDom(stageOn) {
    const section = document.getElementById('jobs');
    if (section) section.classList.toggle('jobs-mode-stage', !!stageOn);
    const toggle = document.getElementById('inbox-view-toggle');
    if (toggle) toggle.textContent = getInboxView() === 'stage' ? 'List view' : 'Stage view';
}

// The one entry point core.js's handleHash()/refreshActiveView() calls for #jobs.
function enterInbox() {
    if (!isDeckLayout()) { _setInboxStageDom(false); loadJobs(); return; }
    if (getInboxView() === 'list') { _setInboxStageDom(false); loadJobs(); }
    else { _setInboxStageDom(true); loadStage(); }
}

// ==========================================================================
// Triage stage — one card + queue rail + verdict buttons.
// ==========================================================================
let _stageJobs = [];   // remaining discovered jobs (top of deck = _stageJobs[0])
let _stageTotal = 0;   // server total when the deck was (re)loaded, for "N of M"

// Single fetch for the stage. A thin, explicit wrapper (status=discovered,
// fit desc, capped at 30) — deliberately NOT loadJobs()'s DOM-filter builder,
// which reads the classic filter inputs.
function fetchInboxJobs(paramsOverride) {
    const params = paramsOverride || '?status=discovered&sort_by=fit_score&sort_dir=desc&limit=30';
    return api(`/api/jobs${params}`);
}

async function loadStage() {
    const host = document.getElementById('inbox-stage');
    if (!host) return;
    try {
        const data = await fetchInboxJobs();
        stageSetJobs(data.jobs || [], (data && typeof data.total === 'number') ? data.total : (data.jobs || []).length);
    } catch (e) {
        host.innerHTML = `<div class="deck-empty"><p>Failed to load the triage stage.</p>`
            + `<button class="btn btn-secondary btn-sm" onclick="loadStage()">Retry</button></div>`;
    }
}

// Exposed for tests + used internally: seed the deck and render.
function stageSetJobs(jobs, total) {
    _stageJobs = Array.isArray(jobs) ? jobs.slice() : [];
    _stageTotal = Number(total) || _stageJobs.length;
    renderStage();
}

function _stageTop() { return _stageJobs[0] || null; }

// "Why it fits" bullets, derived from the same fit-analysis data the classic
// detail pane uses (match_report). Degrades to fit_reasoning, then to nothing.
function stageWhyFits(job) {
    const report = safeParseJSON(job.match_report, null);
    const bullets = [];
    if (report) {
        const matched = report.matched_skills || [];
        const missing = report.missing_skills || [];
        if (matched.length) {
            const top = matched.slice(0, 2).join(' + ');
            const total = matched.length + missing.length;
            bullets.push(total ? `${top} — matches ${matched.length} of ${total} required skills`
                                : `${top} — key skills matched`);
        }
        if (report.title_alignment && report.title_alignment !== 'none') {
            bullets.push(`Title alignment: ${report.title_alignment}`);
        }
        const soft = report.matched_soft_skills || [];
        if (soft.length) bullets.push(`Soft skills: ${soft.slice(0, 3).join(', ')}`);
    }
    if (!bullets.length && job.fit_reasoning) bullets.push(job.fit_reasoning);
    return bullets.slice(0, 3);
}

function stageFactPills(job) {
    const pills = [];
    const hasReal = !!(job.salary_min || job.salary_max);
    const hasEst = !!(job.estimated_salary_min || job.estimated_salary_max);
    if (hasReal) {
        pills.push(`<span class="deck-fact money">${escapeHtml(formatSalaryRange(job.salary_min, job.salary_max, job.salary_period))}</span>`);
    } else if (hasEst) {
        const est = formatSalaryRange(job.estimated_salary_min, job.estimated_salary_max, job.estimated_salary_period || 'annual');
        pills.push(`<span class="deck-fact est">~${escapeHtml(est)} est.</span>`);
    }
    if (job.location) pills.push(`<span class="deck-fact">${escapeHtml(job.location)}</span>`);
    if (job.is_easy_apply || job.apply_type === 'easy_apply' || job.apply_type === 'quick_apply') {
        pills.push(`<span class="deck-fact">Easy Apply</span>`);
    }
    const emp = job.employment_type || job.job_type;
    if (emp) pills.push(`<span class="deck-fact">${escapeHtml(emp)}</span>`);
    return pills.join('');
}

const _WHYFIT_CHECK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>';

function _stageTopCardHtml(job) {
    const initial = (job.company || '?').trim().charAt(0).toUpperCase() || '?';
    const why = stageWhyFits(job);
    const whyHtml = why.length
        ? `<ul class="deck-whyfits">${why.map(b => `<li>${_WHYFIT_CHECK}<span>${escapeHtml(b)}</span></li>`).join('')}</ul>`
        : '';
    const snippet = (job.description || '').trim();
    return `
        <div class="deck-tcard b2" aria-hidden="true"></div>
        <div class="deck-tcard b1" aria-hidden="true"></div>
        <div class="deck-tcard deck-top" onclick="openJobModal('${escapeHtml(String(job.id))}')" title="Click for full details &amp; actions">
            <span class="deck-stamp stamp-shortlist" aria-hidden="true">SHORTLIST</span>
            <span class="deck-stamp stamp-pass" aria-hidden="true">PASS</span>
            <div class="deck-co">
                <span class="deck-logo" aria-hidden="true">${escapeHtml(initial)}</span>
                <div class="deck-co-who">
                    <div class="deck-co-name">${escapeHtml(job.company || 'Unknown')}</div>
                    <div class="deck-co-meta">via ${escapeHtml(job.source || '—')} · ${escapeHtml(timeAgo(job.date_discovered) || 'recently')}</div>
                </div>
                <span class="deck-heat">${renderHeatChip(job.fit_score)}</span>
            </div>
            <h4 class="deck-title">${escapeHtml(job.title || 'Untitled role')}</h4>
            <div class="deck-facts">${stageFactPills(job)}</div>
            ${whyHtml}
            ${snippet ? `<p class="deck-snippet">${escapeHtml(snippet.substring(0, 2400))}</p>` : ''}
        </div>`;
}

function _stageQueueHtml() {
    const upNext = _stageJobs.slice(1, 9);
    const rows = upNext.map((j) => `
        <button type="button" class="deck-qrow" onclick="stageJumpTo('${escapeHtml(j.id)}')"
            title="Bring to the top">
            ${renderHeatChip(j.fit_score)}
            <span class="deck-qwho">
                <b>${escapeHtml(j.title || 'Untitled')}</b>
                <span>${escapeHtml(j.company || 'Unknown')}${j.location ? ' · ' + escapeHtml(j.location) : ''}</span>
            </span>
        </button>`).join('');
    const pos = _stageTotal - _stageJobs.length + 1;
    return `
        <div class="deck-queue">
            <p class="eyebrow">Up next</p>
            <div class="deck-qrows">${rows || '<p class="deck-qempty">Nothing queued.</p>'}</div>
            <span class="deck-qcount"><b class="num">${Math.max(pos, 1)} of ${_stageTotal}</b> · sorted by fit</span>
        </div>`;
}

function renderStage() {
    const host = document.getElementById('inbox-stage');
    if (!host) return;

    if (_stageJobs.length === 0) {
        host.innerHTML = `
            <div class="deck-zero">
                <div class="deck-zero-icon" aria-hidden="true">
                    <svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M20 6L9 17l-5-5"/></svg>
                </div>
                <h3>Inbox zero</h3>
                <p>No new jobs to triage. Fetch a fresh batch to keep the momentum going.</p>
                <button class="btn btn-primary" onclick="stageFetch()">Fetch new jobs</button>
                <button class="btn btn-ghost btn-sm" onclick="setInboxView('list')">Switch to list view</button>
            </div>`;
        return;
    }

    const job = _stageTop();
    host.innerHTML = `
        ${_stageQueueHtml()}
        <div class="deck-stage">
            <div class="deck-deck">${_stageTopCardHtml(job)}</div>
            <div class="deck-verdicts">
                <button class="deck-verdict pass" type="button" onclick="stagePass()"><kbd>&larr;</kbd> Pass</button>
                <button class="deck-verdict open" type="button" onclick="stageOpen()"><kbd>&crarr;</kbd> Open</button>
                <button class="deck-verdict short" type="button" onclick="stageShortlist()">Shortlist <kbd>&rarr;</kbd></button>
            </div>
            <div class="deck-legend">
                <span><kbd>U</kbd> undo</span>
                <span><kbd>T</kbd> shortlist + tailor now</span>
                <span><kbd>L</kbd> switch to list view</span>
            </div>
        </div>`;
}

// Fling the top card, then drop the verdict'd job and promote the next one.
function _stageAdvance(jobId, dir) {
    const top = document.querySelector('#inbox-stage .deck-top');
    const reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    const finish = () => {
        _stageJobs = _stageJobs.filter((j) => j.id !== jobId);
        if (_stageJobs.length === 0) loadStage();  // refetch — there may be more beyond the first page
        else renderStage();
    };
    if (top && !reduce) {
        top.classList.add(dir === 'left' ? 'fling-left' : 'fling-right');
        setTimeout(finish, 240);
    } else {
        finish();
    }
}

async function _stageVerdict(jobId, status, msg, tone, dir) {
    const job = _stageJobs.find((j) => j.id === jobId);
    const prev = job ? (job.status || 'discovered') : 'discovered';
    const title = job ? (job.title || 'job') : 'job';
    try {
        await api(`/api/jobs/${jobId}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status }),
        });
        if (typeof _pushVerdictUndo === 'function') _pushVerdictUndo(jobId, prev, title);
        toast(msg, tone);
        _stageAdvance(jobId, dir);
    } catch (e) {
        toast('Failed to update', 'error');
    }
}

function stagePass() {
    const j = _stageTop();
    if (j) _stageVerdict(j.id, 'passed', 'Passed', 'info', 'left');
}

function stageShortlist() {
    const j = _stageTop();
    if (j) _stageVerdict(j.id, 'shortlisted', 'Shortlisted — moved to Pipeline', 'success', 'right');
}

// T — shortlist AND immediately kick off tailoring (a shortcut iOS can't offer).
async function stageShortlistTailor() {
    const j = _stageTop();
    if (!j) return;
    const prev = j.status || 'discovered';
    const title = j.title || 'job';
    try {
        await api(`/api/jobs/${j.id}/status`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'shortlisted' }),
        });
        if (typeof _pushVerdictUndo === 'function') _pushVerdictUndo(j.id, prev, title);
        await api(`/api/jobs/${j.id}/tailor`, { method: 'POST' });
        toast('Shortlisted & tailoring started', 'success');
        _stageAdvance(j.id, 'right');
    } catch (e) {
        toast('Failed to shortlist & tailor', 'error');
    }
}

function stageOpen() {
    const j = _stageTop();
    if (j && safeHref(j.url) !== '#') openExternal(j.url);
}

// U — reuse the shared verdict-undo stack (jobs.js). undoVerdict() reloads the
// classic list too, but that DOM is hidden in stage mode; we then refresh the
// stage so the restored job reappears at the top.
async function stageUndo() {
    if (typeof undoVerdict !== 'function') return;
    await undoVerdict();
    if (isInboxStageActive()) await loadStage();
}

function stageJumpTo(jobId) {
    const idx = _stageJobs.findIndex((j) => j.id === jobId);
    if (idx <= 0) return;
    const [j] = _stageJobs.splice(idx, 1);
    _stageJobs.unshift(j);
    renderStage();
}

async function stageFetch() {
    if (!document.querySelector('#source-checkboxes input') && typeof loadSources === 'function') {
        try { await loadSources(); } catch (e) { /* fall through — fetchNewJobs warns if empty */ }
    }
    if (typeof fetchNewJobs === 'function') fetchNewJobs();
}

// Live refresh (core.js): refresh the queue data WITHOUT yanking the card the
// user is deciding on. Keep the current top if it still exists server-side.
async function refreshStageLive() {
    try {
        const data = await fetchInboxJobs();
        const jobs = data.jobs || [];
        const topId = _stageTop() && _stageTop().id;
        _stageTotal = (data && typeof data.total === 'number') ? data.total : jobs.length;
        if (topId && jobs.some((j) => j.id === topId)) {
            // Rebuild with the current top pinned to the front so the card is stable.
            const rest = jobs.filter((j) => j.id !== topId);
            const cur = jobs.find((j) => j.id === topId);
            _stageJobs = [cur].concat(rest);
        } else {
            _stageJobs = jobs;  // the top disappeared server-side — promote the next one
        }
        renderStage();
    } catch (e) { /* silent — the next tick retries */ }
}

// Stage keyboard triage. Replaces the list-mode keys while the stage is active
// (jobs.js's classic handler stands down via isInboxStageActive()).
document.addEventListener('keydown', (e) => {
    if (typeof isPaletteOpen === 'function' && isPaletteOpen()) return;
    if (isJobModalOpen()) return;   // the peek modal owns the keyboard
    if (!isInboxStageActive()) return;
    if ((location.hash.replace('#', '') || 'jobs') !== 'jobs') return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
    switch (e.key) {
        case 'ArrowLeft': case 'x': case 'X': e.preventDefault(); stagePass(); break;
        case 'ArrowRight': case 's': case 'S': e.preventDefault(); stageShortlist(); break;
        case 'Enter': e.preventDefault(); stageOpen(); break;
        case 'u': case 'U': e.preventDefault(); stageUndo(); break;
        case 't': case 'T': e.preventDefault(); stageShortlistTailor(); break;
        case 'l': case 'L': e.preventDefault(); setInboxView('list'); break;
    }
});

// ==========================================================================
// Kanban Pipeline board.
// Column keys map to real backend stages; a drag is a status transition that
// already exists as a button today (see DECK_TRANSITIONS).
// ==========================================================================
const DECK_COLUMNS = [
    { key: 'shortlisted',     label: 'Shortlisted',      dot: 'var(--steel)' },
    { key: 'tailoring',       label: 'Tailoring',        dot: 'var(--accent-yellow)' },
    { key: 'pending',         label: 'Ready to review',  dot: 'var(--accent-ember)' },
    { key: 'applied',         label: 'Applied',          dot: 'var(--accent-green)' },
    { key: 'needs-attention', label: 'Needs attention',  dot: 'var(--accent-red)' },
];

// The ONLY allowed drag transitions. Each maps to exactly one real endpoint.
// `run(id)` performs the transition; the test drives these with a stubbed api().
const DECK_TRANSITIONS = [
    {
        from: 'shortlisted', to: 'tailoring', label: 'starts tailoring',
        toast: 'Tailoring started',
        run: (id) => api(`/api/jobs/${id}/tailor`, { method: 'POST' }),
    },
    {
        from: 'shortlisted', to: 'applied', label: 'marks applied', confirm: 'Mark as applied manually?',
        toast: 'Marked as applied',
        run: (id) => api(`/api/jobs/${id}/status`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'manual' }),
        }),
    },
    {
        from: 'pending', to: 'applied', label: 'marks applied',
        toast: 'Marked as applied',
        run: (id) => api(`/api/applications/${id}/status`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'applied' }),
        }),
    },
    {
        from: 'needs-attention', to: 'pending', label: 'requeues for review',
        toast: 'Requeued for review',
        run: (id) => api(`/api/applications/${id}/requeue`, { method: 'POST' }),
    },
    {
        from: 'shortlisted', to: 'pass', label: 'passes', undo: true,
        toast: 'Passed',
        run: (id) => api(`/api/jobs/${id}/status`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'passed' }),
        }),
    },
];

function findDeckTransition(from, to) {
    return DECK_TRANSITIONS.find((t) => t.from === from && t.to === to) || null;
}

// Exposed as a function so the top-level `const` (lexical to the eval unit in
// tests) is still reachable as a global.
function allDeckTransitions() { return DECK_TRANSITIONS; }

function deckTransitionsFrom(from) {
    return DECK_TRANSITIONS.filter((t) => t.from === from);
}

let _boardDragging = false;
let _boardDrag = null;  // { id, from }

function isBoardModeActive() {
    if (!isDeckLayout()) return false;
    const rev = document.getElementById('review');
    return !!(rev && rev.classList.contains('review-mode-board') && !rev.classList.contains('review-detail'));
}

// Called by core.js's handleHash() for #review.
function enterReview() {
    const rev = document.getElementById('review');
    if (!isDeckLayout()) {
        if (rev) rev.classList.remove('review-mode-board', 'review-detail');
        switchReviewView('shortlisted');
        refreshFunnelCounts();
        return;
    }
    if (rev) { rev.classList.add('review-mode-board'); rev.classList.remove('review-detail'); }
    refreshFunnelCounts();  // the funnel strip stays above the board
    renderBoard();
}

// core.js live refresh delegates here when the board is showing.
async function refreshBoardLive() {
    if (_boardDragging) return;   // never reshuffle mid-drag
    await renderBoard();
}

const _KMENU_SVG = '&#8943;';  // ⋯

function _kMenuBtn(colKey, id) {
    return `<button type="button" class="kmenu-btn" aria-label="Card actions"
        onclick="event.stopPropagation();boardCardMenu(event,'${escapeHtml(colKey)}','${escapeHtml(String(id))}')">${_KMENU_SVG}</button>`;
}

function _jobKcardHtml(job, colKey) {
    const progress = colKey === 'tailoring'
        ? `<div class="kprogress" aria-hidden="true"><i></i></div>` : '';
    return `
        <div class="kcard" draggable="true" tabindex="0" data-id="${escapeHtml(String(job.id))}" data-from="${escapeHtml(colKey)}"
            role="button" onclick="boardOpenJob('${escapeHtml(String(job.id))}')" aria-label="${escapeHtml(`${job.title || 'Untitled'} at ${job.company || 'Unknown'}`)}">
            <span class="kt">${escapeHtml(job.title || 'Untitled')}</span>
            <span class="kc">${escapeHtml(job.company || 'Unknown')}${job.location ? ' · ' + escapeHtml(job.location) : ''}</span>
            ${progress}
            <span class="kfoot">${renderHeatChip(job.fit_score)}<span class="kage">${escapeHtml(timeAgo(job.date_discovered) || '')}</span></span>
            ${_kMenuBtn(colKey, job.id)}
        </div>`;
}

function _appKcardHtml(app, colKey) {
    let tag = '';
    if (colKey === 'pending') {
        tag = `<span class="oktag">résumé ready</span>`;
    } else if (colKey === 'applied') {
        const when = app.applied_at || app.created_at;
        tag = `<span class="kage">${escapeHtml(timeAgo(when) || '')}</span>`;
    } else if (colKey === 'needs-attention') {
        const reason = app.error_message || app.state || app.status || 'Needs attention';
        tag = `<span class="duetag" title="${escapeHtml(reason)}">&#9888; ${escapeHtml(String(reason).substring(0, 60))}</span>`;
    }
    const heat = (app.fit_score !== undefined && app.fit_score !== null) ? renderHeatChip(app.fit_score) : '';
    const attnCls = colKey === 'needs-attention' ? ' kcard-attn' : '';
    return `
        <div class="kcard${attnCls}" draggable="true" tabindex="0" data-id="${escapeHtml(String(app.id))}" data-from="${escapeHtml(colKey)}"
            role="button" onclick="boardOpenApp('${escapeHtml(colKey)}','${escapeHtml(String(app.id))}','${escapeHtml(String(app.job_id || ''))}')" aria-label="${escapeHtml(app.title || app.job_title || 'Application')}">
            <span class="kt">${escapeHtml(app.title || app.job_title || 'Untitled')}</span>
            <span class="kc">${escapeHtml(app.company || '')}</span>
            <span class="kfoot">${heat}${tag}</span>
            ${_kMenuBtn(colKey, app.id)}
        </div>`;
}

function renderBoard() {
    const host = document.getElementById('pipeline-board');
    if (!host) return Promise.resolve();

    host.innerHTML = DECK_COLUMNS.map((c) => `
        <div class="kcol" data-col="${c.key}">
            <div class="kcolhead">
                <span class="cdot" style="background:${c.dot}"></span>
                <b>${escapeHtml(c.label)}</b>
                <span class="kct num" id="kct-${c.key}">·</span>
            </div>
            <div class="kbatch" id="kbatch-${c.key}" hidden><i></i></div>
            <div class="kcards" id="kcards-${c.key}"><p class="placeholder">Loading…</p></div>
        </div>`).join('')
        + `<div class="kpasszone" id="kpasszone"><span>Drop here to <b>Pass</b></span></div>`
        + `<div class="kboard-foot">Drag: Shortlist → Tailoring/Applied · Ready → Applied · Attention → Ready · Shortlist → Pass</div>`;

    _wireBoardDnD(host);

    return Promise.all([
        loadColShortlisted(),
        loadColTailoring(),
        loadColReady(),
        loadColApplied(),
        loadColAttention(),
    ]).catch(() => {});
}

function _renderCol(key, html, count) {
    const cards = document.getElementById(`kcards-${key}`);
    const ct = document.getElementById(`kct-${key}`);
    if (ct) ct.textContent = count;
    if (!cards) return;
    cards.innerHTML = html || `<p class="placeholder kcol-empty">Empty</p>`;
}

async function loadColShortlisted() {
    try {
        const data = await api('/api/jobs?status=shortlisted&limit=50&sort_by=fit_score&sort_dir=desc');
        const jobs = (data && data.jobs) || [];
        _renderCol('shortlisted', jobs.map((j) => _jobKcardHtml(j, 'shortlisted')).join(''), jobs.length);
    } catch (e) { _renderCol('shortlisted', '<p class="placeholder kcol-empty">Failed to load</p>', '·'); }
}

async function loadColTailoring() {
    try {
        const data = await api('/api/jobs?status=tailoring&limit=50');
        const jobs = (data && data.jobs) || [];
        _renderCol('tailoring', jobs.map((j) => _jobKcardHtml(j, 'tailoring')).join(''), jobs.length);
    } catch (e) { _renderCol('tailoring', '<p class="placeholder kcol-empty">Failed to load</p>', '·'); }
    // Surface an in-flight tailor batch as an indeterminate strip on the column.
    try {
        const ops = await api('/api/operations/status');
        const strip = document.getElementById('kbatch-tailoring');
        if (strip) strip.hidden = !(ops && ops.tailor_batch);
    } catch (e) { /* non-fatal */ }
}

async function loadColReady() {
    try {
        const apps = await api('/api/applications/pending?limit=50');
        const list = apps || [];
        _renderCol('pending', list.map((a) => _appKcardHtml(a, 'pending')).join(''), list.length);
    } catch (e) { _renderCol('pending', '<p class="placeholder kcol-empty">Failed to load</p>', '·'); }
}

async function loadColApplied() {
    try {
        const apps = await api('/api/applications/submitted?limit=50');
        const list = apps || [];
        _renderCol('applied', list.map((a) => _appKcardHtml(a, 'applied')).join(''), list.length);
    } catch (e) { _renderCol('applied', '<p class="placeholder kcol-empty">Failed to load</p>', '·'); }
}

async function loadColAttention() {
    try {
        const [failed, ip] = await Promise.all([
            api('/api/applications/failed?limit=50').catch(() => []),
            api('/api/applications/in-progress').catch(() => ({})),
        ]);
        const needs = (ip && ip.needs_attention) || [];
        const list = (failed || []).concat(needs);
        _renderCol('needs-attention', list.map((a) => _appKcardHtml(a, 'needs-attention')).join(''), list.length);
    } catch (e) { _renderCol('needs-attention', '<p class="placeholder kcol-empty">Failed to load</p>', '·'); }
}

// ---- Card navigation ----
// Clicking a job anywhere in the deck layout opens the peek modal in place —
// it must never flip layout/view prefs or navigate away (that was the
// "stuck in classic" bug: this used to set jobsmith_inbox_view='list' and
// deep-link into the classic Inbox).
function boardOpenJob(id) {
    openJobModal(id);
}

function boardOpenApp(colKey, appId, jobId) {
    if (jobId) { openJobModal(jobId); return; }
    // Fallback (no job id on the card): the old classic-list detour.
    const view = { pending: 'pending', applied: 'submitted', 'needs-attention': 'in-progress' }[colKey] || 'pending';
    const rev = document.getElementById('review');
    if (rev) rev.classList.add('review-detail');
    switchReviewView(view);
}

// "View Application" inside the peek modal (via viewApplicationFor, jobs.js):
// land on the matching classic Review list behind the board's back bar.
function deckShowApplication(appStatus) {
    closeJobModal();
    const view = (appStatus === 'applied') ? 'submitted'
        : (appStatus === 'pending_review' || appStatus === 'paused') ? 'pending'
        : 'in-progress';
    const go = () => {
        const rev = document.getElementById('review');
        if (rev) { rev.classList.add('review-mode-board', 'review-detail'); }
        switchReviewView(view);
    };
    if ((location.hash.replace('#', '') || 'dashboard') !== 'review') {
        location.hash = 'review';
        setTimeout(go, 0);   // after handleHash()'s enterReview() resets classes
    } else {
        go();
    }
}

function backToBoard() {
    const rev = document.getElementById('review');
    if (rev) rev.classList.remove('review-detail');
    renderBoard();
}

// ==========================================================================
// Job peek modal — the deck layout's "click a posting" surface. Renders the
// SAME detail body as the classic split pane (buildJobDetailHtml, jobs.js)
// in a popped-out overlay, so no click ever navigates out of deck mode.
// ==========================================================================
async function openJobModal(jobId) {
    closeJobModal();
    let job = (window._currentJobs && window._currentJobs[jobId]) || null;
    try {
        const fresh = await api(`/api/jobs/${jobId}`);
        if (fresh && fresh.id) {
            // The single-job endpoint nests the application; flatten the two
            // fields the shared detail renderer expects (app_status, app_id).
            if (fresh.application) {
                fresh.app_status = fresh.application.status;
                fresh.app_id = fresh.application.id;
            }
            job = fresh;
        }
    } catch (e) { /* fall back to the cached list row, if any */ }
    if (!job) { toast('Failed to load job details', 'error'); return; }

    // Detail actions (Rescore, Embellishments, undo snapshots…) read the job
    // from this cache, so make sure the modal's job is in it.
    window._currentJobs = window._currentJobs || {};
    window._currentJobs[jobId] = job;

    const ov = document.createElement('div');
    ov.className = 'job-modal-overlay';
    ov.id = 'job-modal-overlay';
    ov.innerHTML = `
        <div class="job-modal" role="dialog" aria-modal="true" aria-label="${escapeHtml(job.title || 'Job details')}">
            <button type="button" class="job-modal-close" aria-label="Close" title="Close (Esc)" onclick="closeJobModal()">&#10005;</button>
            <div class="job-modal-body">${buildJobDetailHtml(job, { assistAlways: true })}</div>
        </div>`;
    ov.addEventListener('mousedown', (e) => { if (e.target === ov) closeJobModal(); });
    document.body.appendChild(ov);
    requestAnimationFrame(() => ov.classList.add('open'));
    const closeBtn = ov.querySelector('.job-modal-close');
    if (closeBtn) closeBtn.focus();
}

function closeJobModal() {
    const ov = document.getElementById('job-modal-overlay');
    if (ov) ov.remove();
}

function isJobModalOpen() { return !!document.getElementById('job-modal-overlay'); }

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isJobModalOpen()) { e.preventDefault(); closeJobModal(); }
});

// ---- Keyboard-accessible fallback: per-card "⋯" menu ----
function boardCardMenu(ev, colKey, id) {
    _closeCardMenu();
    const opts = deckTransitionsFrom(colKey);
    if (!opts.length) { toast('No moves available for this card', 'info'); return; }
    const menu = document.createElement('div');
    menu.className = 'kmenu';
    menu.id = 'kmenu';
    menu.setAttribute('role', 'menu');
    menu.innerHTML = opts.map((t) =>
        `<button role="menuitem" onclick="_runCardMenu('${escapeHtml(colKey)}','${escapeHtml(t.to)}','${escapeHtml(String(id))}')">${escapeHtml(_transitionMenuLabel(t))}</button>`
    ).join('');
    document.body.appendChild(menu);
    const btn = ev.currentTarget || ev.target;
    const r = btn.getBoundingClientRect();
    menu.style.top = `${Math.round(r.bottom + 4)}px`;
    menu.style.left = `${Math.round(Math.min(r.left, window.innerWidth - 210))}px`;
    setTimeout(() => document.addEventListener('click', _closeCardMenuOnce, true), 0);
}

function _transitionMenuLabel(t) {
    const dest = { tailoring: 'Tailoring', applied: 'Applied', pending: 'Ready to review', pass: 'Pass' }[t.to] || t.to;
    return `Move to ${dest} — ${t.label}`;
}

function _runCardMenu(from, to, id) {
    _closeCardMenu();
    runDeckDrop(from, to, id);
}

function _closeCardMenu() {
    const m = document.getElementById('kmenu');
    if (m) m.remove();
    document.removeEventListener('click', _closeCardMenuOnce, true);
}
function _closeCardMenuOnce(e) {
    if (e.target.closest && e.target.closest('#kmenu')) return;
    _closeCardMenu();
}

// ---- The single transition runner (drag AND menu funnel through this) ----
async function runDeckDrop(from, to, id) {
    const t = findDeckTransition(from, to);
    if (!t) return false;                       // refused — not an allowed transition
    if (t.confirm && !(await appConfirm(t.confirm))) return false;
    try {
        if (t.undo && typeof _pushVerdictUndo === 'function') {
            _pushVerdictUndo(id, 'shortlisted', 'job');   // pass is undoable
        }
        await t.run(id);
        toast(t.toast || 'Moved', 'success');
        if (typeof refreshFunnelCounts === 'function') refreshFunnelCounts();
        renderBoard();
        return true;
    } catch (e) {
        toast('Move failed', 'error');
        return false;
    }
}

// ---- HTML5 drag & drop (event delegation on the board) ----
function _wireBoardDnD(host) {
    host.addEventListener('dragstart', (e) => {
        const card = e.target.closest && e.target.closest('.kcard');
        if (!card) return;
        _boardDragging = true;
        _boardDrag = { id: card.dataset.id, from: card.dataset.from };
        card.classList.add('dragging');
        host.classList.toggle('drag-from-shortlisted', _boardDrag.from === 'shortlisted');
        if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; try { e.dataTransfer.setData('text/plain', _boardDrag.id); } catch (x) {} }
    });
    host.addEventListener('dragend', () => {
        _boardDragging = false;
        host.querySelectorAll('.dragging').forEach((c) => c.classList.remove('dragging'));
        host.querySelectorAll('.drop-active').forEach((c) => c.classList.remove('drop-active'));
        host.classList.remove('drag-from-shortlisted');
        _boardDrag = null;
    });
    host.addEventListener('dragover', (e) => {
        const target = _dropTarget(e.target);
        if (!target || !_boardDrag) return;
        if (findDeckTransition(_boardDrag.from, target.to)) {
            e.preventDefault();                          // signals "droppable"
            target.el.classList.add('drop-active');
            _setDropLabel(target, _boardDrag.from);
        }
    });
    host.addEventListener('dragleave', (e) => {
        const target = _dropTarget(e.target);
        if (target && !target.el.contains(e.relatedTarget)) target.el.classList.remove('drop-active');
    });
    host.addEventListener('drop', (e) => {
        const target = _dropTarget(e.target);
        if (!target || !_boardDrag) return;
        const t = findDeckTransition(_boardDrag.from, target.to);
        if (!t) return;
        e.preventDefault();
        const { id, from } = _boardDrag;
        target.el.classList.remove('drop-active');
        runDeckDrop(from, target.to, id);
    });
}

// Resolve the DOM element under the pointer to a drop target { el, to }.
function _dropTarget(node) {
    if (!node || !node.closest) return null;
    const pass = node.closest('#kpasszone');
    if (pass) return { el: pass, to: 'pass' };
    const col = node.closest('.kcol');
    if (col) return { el: col, to: col.dataset.col };
    return null;
}

function _setDropLabel(target, from) {
    if (target.to === 'pass') return;   // the pass zone carries its own label
    const t = findDeckTransition(from, target.to);
    if (!t) return;
    let slot = target.el.querySelector('.kdroplabel');
    if (!slot) {
        slot = document.createElement('div');
        slot.className = 'kdroplabel';
        const cards = target.el.querySelector('.kcards');
        if (cards) cards.prepend(slot); else target.el.appendChild(slot);
    }
    slot.textContent = `Drop → ${t.label}`;
}

// ==========================================================================
// ⌘K command palette — available in BOTH layouts.
// ==========================================================================
let _paletteOpen = false;
let _paletteFlat = [];     // flat, filtered, runnable items in display order
let _paletteSel = 0;
let _palettePrevFocus = null;

function isPaletteOpen() { return _paletteOpen; }

// Static registry (built once). Each item: { group, label, keywords?, hint?, run }.
function _buildRegistry() {
    return [
        // Navigate
        { group: 'Navigate', label: 'Inbox', keywords: 'jobs triage scout', run: () => { location.hash = 'jobs'; } },
        { group: 'Navigate', label: 'Pipeline', keywords: 'review board applications', run: () => { location.hash = 'review'; } },
        { group: 'Navigate', label: 'Activity', keywords: 'dashboard home stats', run: () => { location.hash = 'dashboard'; } },
        { group: 'Navigate', label: 'Settings', keywords: 'preferences config', run: () => { location.hash = 'settings'; } },
        { group: 'Navigate', label: 'Fit Breakdown', keywords: 'score distribution', run: () => { location.hash = 'fit-breakdown'; } },

        // Actions
        { group: 'Actions', label: 'Fetch jobs', keywords: 'search scrape sources', run: paletteFetch },
        { group: 'Actions', label: 'Score jobs', keywords: 'fit rank batch', run: () => _runIf('scoreAll') },
        { group: 'Actions', label: 'Tailor résumés', keywords: 'resume cover batch', run: () => _runIf('tailorAll') },
        { group: 'Actions', label: 'Estimate salaries', keywords: 'market comp pay', run: () => _runIf('estimateSalariesAll') },
        { group: 'Actions', label: 'Detect Easy Apply', keywords: 'apply type classify', run: () => _runIf('detectApplyTypes') },
        { group: 'Actions', label: 'Add job by URL', keywords: 'ingest manual link paste', run: paletteAddUrl },
        { group: 'Actions', label: 'Toggle theme', keywords: 'dark light appearance', run: () => _runIf('toggleTheme') },
        { group: 'Actions', label: 'Toggle layout (Deck / Classic)', keywords: 'command deck classic view', run: () => _runIf('toggleLayout') },
        { group: 'Actions', label: 'Toggle Now rail', keywords: 'runs progress sidebar', run: () => _runIf('toggleNowRail') },

        // Settings panes
        { group: 'Settings', label: 'Profile', keywords: 'name resume experience', run: () => paletteGoSettings('stab-profile') },
        { group: 'Settings', label: 'Search', keywords: 'keywords locations watchlist', run: () => paletteGoSettings('stab-search') },
        { group: 'Settings', label: 'Integrations', keywords: 'ai linkedin adzuna api', run: () => paletteGoSettings('stab-integrations') },
        { group: 'Settings', label: 'Honesty', keywords: 'embellish fabricate level', run: () => paletteGoSettings('stab-honesty') },
        { group: 'Settings', label: 'Applicant Assist', keywords: 'autofill assist', run: () => paletteGoSettings('stab-assist') },
        { group: 'Settings', label: 'Sync', keywords: 'folder device phone', run: () => paletteGoSettings('stab-sync') },
        { group: 'Settings', label: 'Answer Bank', keywords: 'questions answers', run: () => paletteGoSettings('stab-answerbank') },
        { group: 'Settings', label: 'Prompts', keywords: 'ai prompt registry', run: () => paletteGoSettings('stab-prompts') },
        { group: 'Settings', label: 'Logs', keywords: 'debug logs', run: () => paletteGoSettings('stab-logs') },
    ];
}

let _PALETTE_REGISTRY = null;
function _paletteRegistry() {
    if (!_PALETTE_REGISTRY) _PALETTE_REGISTRY = _buildRegistry();
    return _PALETTE_REGISTRY;
}

function _runIf(name) { if (typeof window[name] === 'function') window[name](); }

function paletteFetch() {
    location.hash = 'dashboard';
    if (!document.querySelector('#source-checkboxes input') && typeof loadSources === 'function') {
        loadSources().then(() => { if (typeof fetchNewJobs === 'function') fetchNewJobs(); }).catch(() => {});
    } else if (typeof fetchNewJobs === 'function') {
        fetchNewJobs();
    }
}

function paletteAddUrl() {
    location.hash = 'dashboard';
    setTimeout(() => {
        if (typeof toggleRunPopover === 'function') toggleRunPopover('more');
        const i = document.getElementById('manual-url-input');
        if (i) i.focus();
    }, 0);
}

function paletteGoSettings(panelId) {
    location.hash = 'settings';
    setTimeout(() => {
        const btns = document.querySelectorAll('#settings .settings-tab');
        for (const b of btns) {
            if ((b.getAttribute('onclick') || '').includes(`'${panelId}'`)) { b.click(); return; }
        }
    }, 0);
}

function paletteSearchJobs(q) {
    localStorage.setItem('jobsmith_inbox_view', 'list');
    const el = document.getElementById('filter-search');
    if (el) el.value = q;
    location.hash = 'jobs';
    setTimeout(() => { if (typeof loadJobs === 'function') loadJobs(); }, 0);
}

// case-insensitive substring OR ordered-subsequence match.
function _cmdkMatches(text, q) {
    if (!q) return true;
    text = String(text || '').toLowerCase();
    q = q.toLowerCase();
    if (text.indexOf(q) >= 0) return true;
    let qi = 0;
    for (let i = 0; i < text.length && qi < q.length; i++) {
        if (text[i] === q[qi]) qi++;
    }
    return qi === q.length;
}

// Escape the label, THEN wrap the matched piece(s) in <em>. Contiguous match
// first (clean single span); ordered-subsequence highlight as a fallback.
function paletteHighlight(label, query) {
    const s = String(label == null ? '' : label);
    const q = String(query == null ? '' : query).trim();
    if (!q) return escapeHtml(s);
    const idx = s.toLowerCase().indexOf(q.toLowerCase());
    if (idx >= 0) {
        return escapeHtml(s.slice(0, idx)) + '<em>' + escapeHtml(s.slice(idx, idx + q.length)) + '</em>' + escapeHtml(s.slice(idx + q.length));
    }
    const lq = q.toLowerCase();
    let out = '', qi = 0;
    for (let i = 0; i < s.length; i++) {
        const ch = s[i];
        if (qi < lq.length && ch.toLowerCase() === lq[qi]) { out += '<em>' + escapeHtml(ch) + '</em>'; qi++; }
        else out += escapeHtml(ch);
    }
    return out;
}

// Build the filtered, flat list of runnable palette entries (incl. the fallback
// "Search jobs for …" row). Exposed so tests can assert filtering + escaping.
function buildPalette(query) {
    const q = String(query == null ? '' : query).trim();
    const out = [];
    _paletteRegistry().forEach((item) => {
        if (_cmdkMatches(item.label, q) || (item.keywords && _cmdkMatches(item.keywords, q))) {
            out.push({
                group: item.group,
                label: item.label,
                hint: item.hint || '',
                html: paletteHighlight(item.label, q),
                run: item.run,
            });
        }
    });
    if (q) {
        const label = `Search jobs for “${q}”`;
        out.push({
            group: 'Jobs',
            label,
            hint: '↩',
            html: paletteHighlight(label, q),
            run: () => paletteSearchJobs(q),
        });
    }
    return out;
}

function _paletteOverlay() { return document.getElementById('cmdk-overlay'); }

function openPalette() {
    if (_paletteOpen) return;
    _paletteOpen = true;
    _palettePrevFocus = document.activeElement;
    let overlay = _paletteOverlay();
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'cmdk-overlay';
        overlay.className = 'cmdk-overlay';
        overlay.innerHTML = `
            <div class="cmdk-panel" role="dialog" aria-modal="true" aria-label="Command palette">
                <div class="cmdk-input-row">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                    <input id="cmdk-input" type="text" role="combobox" aria-expanded="true" aria-controls="cmdk-list"
                        aria-autocomplete="list" autocomplete="off" spellcheck="false"
                        placeholder="Type a command or search…">
                </div>
                <div class="cmdk-list" id="cmdk-list" role="listbox" aria-label="Commands"></div>
            </div>`;
        document.body.appendChild(overlay);
        overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) closePalette(); });
        const input = overlay.querySelector('#cmdk-input');
        input.addEventListener('input', () => renderPalette(input.value));
    }
    overlay.style.display = '';
    const input = overlay.querySelector('#cmdk-input');
    input.value = '';
    renderPalette('');
    setTimeout(() => input.focus(), 0);
}

function closePalette() {
    _paletteOpen = false;
    const overlay = _paletteOverlay();
    if (overlay) overlay.style.display = 'none';
    _closeCardMenu();
    if (_palettePrevFocus && typeof _palettePrevFocus.focus === 'function') {
        try { _palettePrevFocus.focus(); } catch (e) {}
    }
    _palettePrevFocus = null;
}

function togglePalette() { _paletteOpen ? closePalette() : openPalette(); }

function renderPalette(query) {
    const list = document.getElementById('cmdk-list');
    if (!list) return;
    _paletteFlat = buildPalette(query);
    _paletteSel = 0;

    if (_paletteFlat.length === 0) {
        list.innerHTML = `<div class="cmdk-empty">No matches.</div>`;
        return;
    }

    let html = '';
    let lastGroup = null;
    let idx = 0;
    for (const item of _paletteFlat) {
        if (item.group !== lastGroup) {
            html += `<div class="cmdk-group">${escapeHtml(item.group)}</div>`;
            lastGroup = item.group;
        }
        html += `<div class="cmdk-row${idx === _paletteSel ? ' sel' : ''}" role="option" id="cmdk-row-${idx}"
            aria-selected="${idx === _paletteSel ? 'true' : 'false'}" data-index="${idx}"
            onmousemove="paletteHover(${idx})" onclick="paletteRun(${idx})">
            <span class="cmdk-label">${item.html}</span>
            ${item.hint ? `<span class="cmdk-hint">${escapeHtml(item.hint)}</span>` : ''}
        </div>`;
        idx++;
    }
    list.innerHTML = html;
}

function _paletteUpdateSelection() {
    const list = document.getElementById('cmdk-list');
    if (!list) return;
    list.querySelectorAll('.cmdk-row').forEach((row) => {
        const i = Number(row.dataset.index);
        const sel = i === _paletteSel;
        row.classList.toggle('sel', sel);
        row.setAttribute('aria-selected', sel ? 'true' : 'false');
        if (sel) row.scrollIntoView({ block: 'nearest' });
    });
}

function paletteHover(i) {
    if (i === _paletteSel) return;
    _paletteSel = i;
    _paletteUpdateSelection();
}

function paletteMove(delta) {
    if (!_paletteFlat.length) return;
    _paletteSel = (_paletteSel + delta + _paletteFlat.length) % _paletteFlat.length;
    _paletteUpdateSelection();
}

function paletteRun(i) {
    const idx = (typeof i === 'number') ? i : _paletteSel;
    const item = _paletteFlat[idx];
    if (!item) return;
    closePalette();
    try { item.run(); } catch (e) { toast('Command failed', 'error'); }
}

// Global opener + in-palette key handling. Capture phase so it wins over the
// bubble-phase triage handlers; while open, it swallows only the control keys
// (Esc/arrows/Enter/Tab) and lets ordinary typing reach the input.
document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault(); e.stopImmediatePropagation();
        togglePalette();
        return;
    }
    if (!_paletteOpen) return;
    switch (e.key) {
        case 'Escape': e.preventDefault(); e.stopImmediatePropagation(); closePalette(); break;
        case 'ArrowDown': e.preventDefault(); e.stopImmediatePropagation(); paletteMove(1); break;
        case 'ArrowUp': e.preventDefault(); e.stopImmediatePropagation(); paletteMove(-1); break;
        case 'Enter': e.preventDefault(); e.stopImmediatePropagation(); paletteRun(); break;
        case 'Tab': e.preventDefault(); e.stopImmediatePropagation(); break;  // focus trap
        default: break;  // typing passes through to the input
    }
}, true);
