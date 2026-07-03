// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ---- Job Selection & Deletion ----
let selectModeActive = false;

function toggleSelectMode() {
    selectModeActive = !selectModeActive;
    const btn = document.getElementById('toggle-select-btn');
    const actions = document.getElementById('select-actions');
    const container = document.getElementById('jobs-list');

    if (selectModeActive) {
        btn.textContent = 'Cancel';
        btn.className = 'btn btn-danger btn-sm';
        actions.style.display = '';
        container.classList.add('select-mode');
    } else {
        btn.textContent = 'Select';
        btn.className = 'btn btn-ghost btn-sm';
        actions.style.display = 'none';
        container.classList.remove('select-mode');
        document.querySelectorAll('.job-checkbox').forEach(cb => { cb.checked = false; });
        document.getElementById('select-all-jobs').checked = false;
        updateSelectedCount();
    }
}

function getSelectedJobIds() {
    const boxes = document.querySelectorAll('.job-checkbox:checked');
    return Array.from(boxes).map(cb => cb.value);
}

function updateSelectedCount() {
    const count = document.querySelectorAll('.job-checkbox:checked').length;
    const el = document.getElementById('selected-count');
    if (el) el.textContent = `${count} selected`;
}

function toggleSelectAllJobs(checked) {
    document.querySelectorAll('.job-checkbox').forEach(cb => { cb.checked = checked; });
    updateSelectedCount();
}

async function tailorSelectedJobs() {
    const ids = getSelectedJobIds();
    if (ids.length === 0) {
        toast('No jobs selected', 'info');
        return;
    }
    let succeeded = 0;
    let failed = 0;
    for (const id of ids) {
        try {
            await api(`/api/jobs/${id}/tailor`, { method: 'POST' });
            succeeded++;
        } catch (e) {
            failed++;
        }
    }
    const msg = `Tailoring started for ${succeeded} job${succeeded !== 1 ? 's' : ''}` + (failed ? `, ${failed} failed` : '');
    toast(msg, failed ? 'error' : 'success');
}

async function deleteSingleJob(jobId) {
    if (!confirm('Delete this job posting?')) return;
    try {
        await api(`/api/jobs/${jobId}`, { method: 'DELETE' });
        toast('Job deleted', 'info');
        if (selectedJobId === jobId) clearDetailPane();
        loadJobs();
    } catch (e) {
        toast('Failed to delete job', 'error');
    }
}

async function deleteSelectedJobs() {
    const ids = getSelectedJobIds();
    if (ids.length === 0) {
        toast('No jobs selected', 'info');
        return;
    }
    if (!confirm(`Delete ${ids.length} selected job${ids.length > 1 ? 's' : ''}?`)) return;
    try {
        const data = await api('/api/jobs/delete', {
            method: 'POST',
            body: JSON.stringify({ job_ids: ids }),
        });
        toast(data.message, 'success');
        clearDetailPane();
        loadJobs();
    } catch (e) {
        toast('Failed to delete jobs', 'error');
    }
}

async function deleteFilteredJobs() {
    const source = document.getElementById('filter-source').value;
    const status = document.getElementById('filter-status').value;
    if (!source && !status) {
        toast('Set a source or status filter first, or use "Delete All"', 'info');
        return;
    }
    const filterDesc = [source, status].filter(Boolean).join(' / ');
    if (!confirm(`Delete ALL jobs matching filter: ${filterDesc}? This cannot be undone.`)) return;
    try {
        const data = await api('/api/jobs/delete', {
            method: 'POST',
            body: JSON.stringify({ source: source || null, status: status || null }),
        });
        toast(data.message, 'success');
        clearDetailPane();
        loadJobs();
    } catch (e) {
        toast('Failed to delete jobs', 'error');
    }
}

async function deleteAllJobs() {
    if (!confirm('Delete ALL job postings? This cannot be undone.')) return;
    if (!confirm('Are you sure? This will permanently delete all jobs and pending applications. Applied entries in the Submitted tab will be preserved.')) return;
    try {
        const data = await api('/api/jobs/delete', {
            method: 'POST',
            body: JSON.stringify({ all: true }),
        });
        toast(data.message, 'success');
        clearDetailPane();
        loadJobs();
        loadDashboard();
    } catch (e) {
        toast('Failed to delete jobs', 'error');
    }
}

// ---- Utilities ----
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function safeParseJSON(str, fallback) {
    if (Array.isArray(str)) return str;
    try { return JSON.parse(str); } catch { return fallback; }
}

function formatSalaryRange(min, max, period) {
    const suffix = period === 'hourly' ? '/hr' : period === 'annual' ? '/yr' : '';
    const fmt = (v) => v == null ? '?' : '$' + Number(v).toLocaleString();
    if (min != null && max != null) return `${fmt(min)} - ${fmt(max)}${suffix}`;
    if (min != null) return `${fmt(min)}${suffix}`;
    if (max != null) return `${fmt(max)}${suffix}`;
    return '';
}

function renderSalarySection(job) {
    const hasReal = !!(job.salary_min || job.salary_max);
    const hasEst = !!(job.estimated_salary_min || job.estimated_salary_max);
    if (!hasReal && !hasEst) {
        return `
            <div class="detail-section">
                <h4>Salary</h4>
                <span style="font-size:13px;color:var(--text-muted)">Not disclosed.
                    <button class="btn btn-ghost btn-xs" style="margin-left:6px" onclick="reestimateSalary('${job.id}')">Estimate from market</button>
                </span>
            </div>`;
    }

    const meta = safeParseJSON(job.estimated_salary_metadata, {}) || {};
    const sourceLabel = (job.estimated_salary_source === 'bls_oews') ? 'BLS OEWS' : (job.estimated_salary_source === 'adzuna') ? 'Adzuna' : 'AI estimate';
    const conf = job.estimated_salary_confidence ? ` · ${job.estimated_salary_confidence} confidence` : '';
    const sample = meta.sample_size ? ` · n=${meta.sample_size}` : '';

    let vsMarketHtml = '';
    if (hasReal && hasEst && meta.p50) {
        const realMid = (job.salary_min && job.salary_max)
            ? (Number(job.salary_min) + Number(job.salary_max)) / 2
            : Number(job.salary_min || job.salary_max);
        const realAnnual = job.salary_period === 'hourly' ? realMid * 2080 : realMid;
        const diff = (realAnnual - meta.p50) / meta.p50;
        const pct = Math.round(diff * 100);
        const cls = pct > 5 ? 'above' : pct < -5 ? 'below' : '';
        const label = pct > 0 ? `+${pct}% vs market` : `${pct}% vs market`;
        vsMarketHtml = `<span class="salary-vs-market ${cls}" title="Comparison against estimated market median (p50: $${Number(meta.p50).toLocaleString()})">${label}</span>`;
    }

    const realBlock = hasReal ? `
        <div class="detail-section">
            <h4>${job.salary_period === 'hourly' ? 'Hourly Rate' : 'Salary'}</h4>
            <span style="font-size:14px;color:var(--text-primary)">
                ${formatSalaryRange(job.salary_min, job.salary_max, job.salary_period)}
            </span>
            ${vsMarketHtml}
        </div>` : '';

    const estBlock = hasEst ? `
        <div class="detail-section">
            <div class="estimated-salary-block">
                <h4>Estimated Salary <span class="badge-estimate" title="Real data from ${escapeHtml(sourceLabel)} — never confused with disclosed compensation. The local AI only canonicalizes the role; salary numbers come from the external source.">AI · ${escapeHtml(sourceLabel)}</span></h4>
                <div class="estimated-salary-value">${formatSalaryRange(job.estimated_salary_min, job.estimated_salary_max, job.estimated_salary_period || 'annual')} · p25–p75</div>
                <div class="estimated-salary-meta">${escapeHtml(sourceLabel)}${conf}${sample} · <a href="javascript:void(0)" onclick="reestimateSalary('${job.id}')">re-estimate</a></div>
            </div>
        </div>` : `
        <div class="detail-section">
            <button class="btn btn-ghost btn-xs" onclick="reestimateSalary('${job.id}')">Estimate market salary</button>
        </div>`;

    return realBlock + estBlock;
}

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const now = new Date();
    const then = new Date(dateStr);
    const diffMs = now - then;
    const diffMins = Math.floor(diffMs / 60000);
    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 7) return `${diffDays}d ago`;
    return then.toLocaleDateString();
}

// ---- Screenshot & Apply Log Viewer ----
async function viewScreenshots(jobId) {
    try {
        const [screenshotData, logData] = await Promise.all([
            api(`/api/jobs/${jobId}/screenshots`),
            api(`/api/jobs/${jobId}/apply-log-v2`),
        ]);
        const screenshots = screenshotData.screenshots || [];
        const applyLog = logData;  // {job_id, entries:[]}

        if (screenshots.length === 0 && (!applyLog.entries || applyLog.entries.length === 0)) {
            toast('No screenshots or apply log available for this job', 'info');
            return;
        }
        openScreenshotModal(screenshots, applyLog);
    } catch (e) {
        toast('Failed to load screenshots', 'error');
    }
}

function _renderApplyLog(applyLog) {
    if (!applyLog || !applyLog.entries || applyLog.entries.length === 0) return '';

    // Derive provider/tier metadata from the last result entry that has them
    const metaEntry = [...applyLog.entries].reverse().find(e => e.provider || e.tier != null);
    const metaHtml = metaEntry
        ? `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">
               Applied via <strong>${escapeHtml(metaEntry.provider || '?')}</strong> adapter
               (Tier ${metaEntry.tier != null ? metaEntry.tier : '?'})
           </div>`
        : '';

    const levelStyle = {
        info:    'background:rgba(74,144,217,0.2);color:var(--accent-blue)',
        warning: 'background:rgba(245,166,35,0.2);color:var(--accent-yellow)',
        warn:    'background:rgba(245,166,35,0.2);color:var(--accent-yellow)',
        error:   'background:rgba(231,76,60,0.2);color:var(--accent-red)',
        result:  'background:rgba(76,175,80,0.2);color:var(--accent-green)',
    };

    const rows = applyLog.entries.map(e => {
        const level = (e.level || 'info').toLowerCase();
        const badge = `<span style="display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;${levelStyle[level] || levelStyle.info}">${escapeHtml(level)}</span>`;
        const ts = e.ts ? `<span style="color:var(--text-muted);white-space:nowrap">${escapeHtml(e.ts.replace('T', ' ').slice(0, 19))}</span>` : '';

        let detail = escapeHtml(e.message || '');
        if (e.field_id) {
            const conf = e.confidence != null ? Number(e.confidence).toFixed(2) : '—';
            const src  = e.source ? escapeHtml(e.source) : '—';
            const val  = e.value  != null ? `<code>${escapeHtml(String(e.value))}</code>` : '—';
            detail += `<br><span style="color:var(--text-secondary)">Field: <strong>${escapeHtml(e.field_id)}</strong> → ${val} (source: ${src}, confidence: ${conf})</span>`;
        }

        const lowConf = e.confidence != null && Number(e.confidence) < 0.60;
        const rowBg   = lowConf ? 'background:rgba(245,166,35,0.08)' : '';

        return `<tr style="border-bottom:1px solid var(--border);${rowBg}">
            <td style="padding:4px 6px;white-space:nowrap">${ts}</td>
            <td style="padding:4px 6px">${badge}</td>
            <td style="padding:4px 6px;font-size:12px">${detail}</td>
        </tr>`;
    }).join('');

    return `
        <div class="apply-log">
            <h4 style="margin:0 0 6px 0;font-size:14px;color:var(--text-primary)">Apply Log</h4>
            ${metaHtml}
            <table style="width:100%;font-size:12px;border-collapse:collapse">
                <thead><tr style="text-align:left;border-bottom:1px solid var(--border)">
                    <th style="padding:4px 6px;width:140px">Time</th>
                    <th style="padding:4px 6px;width:70px">Level</th>
                    <th style="padding:4px 6px">Message</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

function openScreenshotModal(screenshots, applyLog) {
    const existing = document.getElementById('screenshot-modal');
    if (existing) existing.remove();

    const hasScreenshots = screenshots.length > 0;
    const hasLog = applyLog && applyLog.entries && applyLog.entries.length > 0;

    const modal = document.createElement('div');
    modal.id = 'screenshot-modal';
    modal.className = 'screenshot-modal';
    modal.innerHTML = `
        <div class="screenshot-modal-backdrop" onclick="closeScreenshotModal()"></div>
        <div class="screenshot-modal-content" style="${hasLog ? 'max-width:900px' : ''}">
            <div class="screenshot-modal-header">
                <span class="screenshot-modal-title" id="screenshot-label">${hasScreenshots ? '' : 'Apply Log'}</span>
                <button class="screenshot-modal-close" onclick="closeScreenshotModal()">&times;</button>
            </div>
            ${hasLog ? `
                <div style="padding:12px 16px;max-height:200px;overflow-y:auto;border-bottom:1px solid var(--border);background:var(--bg-primary)">
                    ${_renderApplyLog(applyLog)}
                </div>
            ` : ''}
            ${hasScreenshots ? `
                <div class="screenshot-modal-body">
                    ${screenshots.length > 1 ? `<button class="screenshot-nav screenshot-nav-prev" id="screenshot-prev" onclick="event.stopPropagation();screenshotNav(-1)">&lsaquo;</button>` : ''}
                    <img id="screenshot-img" class="screenshot-img" src="" alt="Screenshot">
                    ${screenshots.length > 1 ? `<button class="screenshot-nav screenshot-nav-next" id="screenshot-next" onclick="event.stopPropagation();screenshotNav(1)">&rsaquo;</button>` : ''}
                </div>
                ${screenshots.length > 1 ? `<div class="screenshot-modal-footer"><span id="screenshot-counter"></span></div>` : ''}
            ` : ''}
        </div>
    `;
    document.body.appendChild(modal);

    modal._screenshots = screenshots;
    modal._currentIndex = 0;

    if (hasScreenshots) updateScreenshotView();

    modal._keyHandler = (e) => {
        if (e.key === 'Escape') closeScreenshotModal();
        if (hasScreenshots && e.key === 'ArrowLeft') screenshotNav(-1);
        if (hasScreenshots && e.key === 'ArrowRight') screenshotNav(1);
    };
    document.addEventListener('keydown', modal._keyHandler);
}

function updateScreenshotView() {
    const modal = document.getElementById('screenshot-modal');
    if (!modal || !modal._screenshots.length) return;
    const screenshots = modal._screenshots;
    const idx = modal._currentIndex;
    const filename = screenshots[idx];

    document.getElementById('screenshot-img').src = `/api/screenshots/${filename}`;

    const label = filename.replace(/^[^_]+_/, '').replace('.png', '').replace(/_/g, ' ');
    document.getElementById('screenshot-label').textContent = label;

    const counter = document.getElementById('screenshot-counter');
    if (counter) counter.textContent = `${idx + 1} / ${screenshots.length}`;
}

function screenshotNav(dir) {
    const modal = document.getElementById('screenshot-modal');
    if (!modal) return;
    const len = modal._screenshots.length;
    modal._currentIndex = (modal._currentIndex + dir + len) % len;
    updateScreenshotView();
}

function closeScreenshotModal() {
    const modal = document.getElementById('screenshot-modal');
    if (!modal) return;
    if (modal._keyHandler) document.removeEventListener('keydown', modal._keyHandler);
    modal.remove();
}

// ---- Fit Score Breakdown ----
async function loadFitBreakdown() {
    try {
        const data = await api('/api/fit-breakdown');
        renderFitBreakdown(data);
    } catch (e) {
        console.error('Failed to load fit breakdown', e);
    }
}

function renderFitBreakdown(data) {
    const { score_buckets, avg_fit_score, total_jobs, total_scored, status_breakdown } = data;
    const { high, mid, low, unscored } = score_buckets;

    // Update center avg
    document.getElementById('fit-pie-avg').textContent = avg_fit_score || '--';

    // Draw pie chart
    const canvas = document.getElementById('fit-pie-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const cx = canvas.width / 2;
    const cy = canvas.height / 2;
    const r = Math.min(cx, cy) - 10;
    const innerR = r * 0.55;

    // Slice definitions with filter params
    const sliceDefs = [
        { label: 'High (70-100)', value: high,    color: '#22c55e', minScore: 70,  maxScore: null, unscoredOnly: false },
        { label: 'Mid (40-69)',   value: mid,     color: '#f59e0b', minScore: 40,  maxScore: 69,   unscoredOnly: false },
        { label: 'Low (1-39)',    value: low,     color: '#ef4444', minScore: 1,   maxScore: 39,   unscoredOnly: false },
        { label: 'Unscored',      value: unscored, color: '#4b5563', minScore: null, maxScore: null, unscoredOnly: true },
    ];
    const activeSlices = sliceDefs.filter(s => s.value > 0);

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    // Store slice angle ranges for click detection
    const sliceAngles = [];

    if (activeSlices.length === 0) {
        ctx.fillStyle = '#4b5563';
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.fill();
    } else {
        const total = activeSlices.reduce((s, sl) => s + sl.value, 0);
        let startAngle = -Math.PI / 2;

        activeSlices.forEach((sl) => {
            const sweep = (sl.value / total) * Math.PI * 2;
            const endAngle = startAngle + sweep;

            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.arc(cx, cy, r, startAngle, endAngle);
            ctx.closePath();
            ctx.fillStyle = sl.color;
            ctx.fill();

            // Gap between slices
            if (activeSlices.length > 1) {
                ctx.beginPath();
                ctx.moveTo(cx, cy);
                ctx.arc(cx, cy, r + 1, startAngle, startAngle + 0.01);
                ctx.closePath();
                ctx.strokeStyle = 'var(--bg-card)';
                ctx.lineWidth = 2;
                ctx.stroke();
            }

            sliceAngles.push({ startAngle, endAngle, ...sl });
            startAngle = endAngle;
        });

        // Donut hole
        ctx.beginPath();
        ctx.arc(cx, cy, innerR, 0, Math.PI * 2);
        ctx.fillStyle = 'var(--bg-card)';
        ctx.fill();
    }

    // Pie chart click handler
    canvas.style.cursor = activeSlices.length > 0 ? 'pointer' : 'default';
    canvas.onclick = function(e) {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left - cx;
        const y = e.clientY - rect.top - cy;
        const dist = Math.sqrt(x * x + y * y);
        if (dist < innerR || dist > r) return;
        let angle = Math.atan2(y, x);
        if (angle < -Math.PI / 2) angle += 2 * Math.PI;
        const hit = sliceAngles.find(s => angle >= s.startAngle && angle < s.endAngle);
        if (hit) goToScoreFilter(hit.minScore, hit.maxScore, hit.unscoredOnly);
    };

    // Legend (clickable)
    const legendEl = document.getElementById('fit-legend');
    legendEl.innerHTML = sliceDefs.map(sl => {
        const pct = total_jobs > 0 ? Math.round((sl.value / total_jobs) * 100) : 0;
        const clickAttr = `onclick="goToScoreFilter(${sl.minScore}, ${sl.maxScore}, ${sl.unscoredOnly})"`;
        return `
            <div class="fit-legend-item fit-clickable" ${clickAttr}>
                <span class="fit-legend-dot" style="background:${sl.color}"></span>
                <span class="fit-legend-label">${sl.label}</span>
                <span class="fit-legend-count">${sl.value} <span class="fit-legend-pct">(${pct}%)</span></span>
            </div>`;
    }).join('');

    // Score summary stats (clickable rows for score buckets)
    const statListEl = document.getElementById('fit-stat-list');
    statListEl.innerHTML = `
        <div class="fit-stat-row"><span>Total Jobs</span><strong>${total_jobs}</strong></div>
        <div class="fit-stat-row"><span>Scored Jobs</span><strong>${total_scored}</strong></div>
        <div class="fit-stat-row"><span>Avg Fit Score</span><strong>${avg_fit_score || '--'}</strong></div>
        <div class="fit-stat-row fit-clickable" onclick="goToScoreFilter(70, null, false)"><span>High Fit (&ge;70)</span><strong class="fit-val-high">${high}</strong></div>
        <div class="fit-stat-row fit-clickable" onclick="goToScoreFilter(40, 69, false)"><span>Mid Fit (40-69)</span><strong class="fit-val-mid">${mid}</strong></div>
        <div class="fit-stat-row fit-clickable" onclick="goToScoreFilter(1, 39, false)"><span>Low Fit (&lt;40)</span><strong class="fit-val-low">${low}</strong></div>
        <div class="fit-stat-row fit-clickable" onclick="goToScoreFilter(null, null, true)"><span>Unscored</span><strong>${unscored}</strong></div>
    `;

    // Status bars (clickable)
    const statusBarsEl = document.getElementById('fit-status-bars');
    const statusLabels = {
        discovered: 'New', tailoring: 'Tailoring', review: 'In Review',
        approved: 'Approved', applied: 'Applied', manual: 'Manual', rejected: 'Rejected',
    };
    const statusColors = {
        discovered: '#3b82f6', tailoring: '#8b5cf6', review: '#f59e0b',
        approved: '#10b981', applied: '#22c55e', manual: '#6b7280', rejected: '#ef4444',
    };
    const statusMax = Math.max(...Object.values(status_breakdown), 1);
    statusBarsEl.innerHTML = Object.entries(status_breakdown).map(([status, cnt]) => {
        const pct = Math.round((cnt / statusMax) * 100);
        const color = statusColors[status] || '#6b7280';
        const label = statusLabels[status] || status;
        return `
            <div class="fit-bar-row fit-clickable" onclick="goToStatusFilter('${status}')">
                <span class="fit-bar-label">${label}</span>
                <div class="fit-bar-track">
                    <div class="fit-bar-fill" style="width:${pct}%;background:${color}"></div>
                </div>
                <span class="fit-bar-count">${cnt}</span>
            </div>`;
    }).join('');
}

