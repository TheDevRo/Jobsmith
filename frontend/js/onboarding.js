// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ============================================================
// First-run onboarding wizard
// ============================================================
const OB_STEPS = 5;
// rerun: wizard opened with an already-populated profile — finish goes
// through the Review-changes diff panel instead of overwriting config.
let _obState = { step: 0, parsed: null, open: false, rerun: false, diff: [] };

document.addEventListener('DOMContentLoaded', () => { obCheckStatus(); });

async function obCheckStatus() {
    try {
        const s = await api('/api/onboarding/status');
        if (s.needs_onboarding) { obOpen({ aiStatus: s.ai }); return; }
        if (!s.tour_complete) {
            setTimeout(() => tourStart(), 300);
        }
    } catch (e) { console.error('onboarding status failed', e); }
}

function obOpen({ aiStatus } = {}) {
    _obState.step = 0;
    _obState.parsed = null;
    _obState.open = true;
    document.getElementById('onboarding-overlay').style.display = 'flex';
    obGoto(0);
    obLoadPrefill().then(() => {
        if (aiStatus) obApplyAIStatus(aiStatus);
        else obTestAI(true);
    });
}

function obHide() {
    document.getElementById('onboarding-overlay').style.display = 'none';
    _obState.open = false;
}

async function obLoadPrefill() {
    try {
        const cfg = await api('/api/config');
        const p = cfg.profile || {};
        const isExample = !p.full_name || p.full_name === 'Jane Doe' || p.email === 'jane.doe@example.com';
        _obState.rerun = !isExample;
        const reviewChip = document.getElementById('ob-step-review');
        if (reviewChip) reviewChip.style.display = _obState.rerun ? '' : 'none';
        document.getElementById('ob-ai-url').value = cfg.ai?.base_url || 'http://localhost:1234/v1';
        const models = cfg.ai?.models || {};
        document.getElementById('ob-ai-model-strong').dataset.preferred = models.strong?.model || '';
        document.getElementById('ob-ai-model-fast').dataset.preferred = models.fast?.model || '';
        document.getElementById('ob-ai-model-utility').dataset.preferred = models.utility?.model || '';
        if (!isExample) {
            document.getElementById('ob-name').value = p.full_name || '';
            document.getElementById('ob-email').value = p.email || '';
            document.getElementById('ob-phone').value = p.phone || '';
            document.getElementById('ob-location').value = p.location || '';
            document.getElementById('ob-linkedin').value = p.linkedin || '';
            document.getElementById('ob-summary').value = p.summary || '';
            document.getElementById('ob-skills').value = (p.skills || []).join(', ');
            document.getElementById('ob-certifications').value = (p.certifications || []).join('\n');
            obRenderExperience(p.experience || []);
            obRenderEducation(p.education || []);
            document.getElementById('ob-workday-email').value = p.workday_email || '';
            document.getElementById('ob-workday-password').value = p.workday_password || '';
        } else {
            obRenderExperience([]);
            obRenderEducation([]);
        }
        const s = cfg.search || {};
        document.getElementById('ob-keywords').value = (s.keywords || []).join(', ');
        document.getElementById('ob-locations').value = (s.locations || []).join('\n');
        document.getElementById('ob-salary').value = s.min_salary || '';
        document.getElementById('ob-exclude').value = (s.exclude_keywords || []).join(', ');
        const k = cfg.api_keys || {};
        document.getElementById('ob-adzuna-app-id').value = k.adzuna_app_id || '';
        document.getElementById('ob-adzuna-app-key').value = k.adzuna_app_key || '';
        document.getElementById('ob-bls-key').value = cfg.salary_estimator?.bls?.api_key || '';
    } catch (e) { console.error('obLoadPrefill failed', e); }
}

function obGoto(step) {
    _obState.step = step;
    document.querySelectorAll('.ob-panel').forEach((pnl, i) => pnl.classList.toggle('active', i === step));
    document.querySelectorAll('.ob-step').forEach((el, i) => {
        el.classList.toggle('active', i === step);
        el.classList.toggle('done', i < step);
    });
    document.getElementById('ob-back').style.visibility = step === 0 ? 'hidden' : 'visible';
    const nextBtn = document.getElementById('ob-next');
    if (step === 5) nextBtn.textContent = 'Apply selected ✓';
    else if (step === OB_STEPS - 1) nextBtn.textContent = _obState.rerun ? 'Review changes →' : 'Finish ✓';
    else nextBtn.textContent = 'Next →';
    const body = document.querySelector('.ob-body');
    if (body) body.scrollTop = 0;
}

function obBack() { if (_obState.step > 0) obGoto(_obState.step - 1); }

function obNext() {
    const step = _obState.step;
    if (step === 2 && !obValidateProfile()) return;
    if (step === 5) { obApplyDiff(); return; }
    if (step === OB_STEPS - 1) {
        if (_obState.rerun) { obShowDiff(); return; }
        obFinish();
        return;
    }
    obGoto(step + 1);
}

async function obSkip() {
    if (!(await appConfirm('Skip first-time setup? You can re-run it anytime from Settings → Profile.'))) return;
    try { await api('/api/onboarding/complete', { method: 'POST', body: '{}' }); } catch (e) {}
    obHide();
    toast('Setup skipped — you can re-run it from Settings.', 'info');
}

function obRelaunch() { obOpen(); }

// --- AI step ---
async function obTestAI(silent) {
    const url = document.getElementById('ob-ai-url').value.trim();
    const statusEl = document.getElementById('ob-ai-status');
    statusEl.className = 'ob-status busy';
    statusEl.textContent = silent ? 'Checking…' : 'Testing…';
    try { await api('/api/config', { method: 'POST', body: JSON.stringify({ ai: { base_url: url } }) }); } catch (e) {}
    try {
        const s = await api('/api/ai/status');
        obApplyAIStatus({ ok: s.ok, models: s.models || [], error: s.error });
    } catch (e) {
        statusEl.className = 'ob-status err';
        statusEl.textContent = 'Connection failed';
    }
}

function obApplyAIStatus(s) {
    const statusEl = document.getElementById('ob-ai-status');
    if (s.ok) {
        statusEl.className = 'ob-status ok';
        const n = (s.models || []).length;
        statusEl.textContent = 'Connected — ' + n + ' model' + (n === 1 ? '' : 's') + ' available';
        obPopulateModels(s.models || []);
    } else {
        statusEl.className = 'ob-status err';
        statusEl.textContent = s.error ? 'Not connected: ' + s.error : 'Not connected';
        obPopulateModels([]);
    }
}

function obPopulateModels(models) {
    ['strong', 'fast', 'utility'].forEach(tier => {
        const sel = document.getElementById('ob-ai-model-' + tier);
        const preferred = sel.dataset.preferred || sel.value || '';
        const opts = ['<option value="">—</option>']
            .concat(models.map(m => '<option value="' + esc(m) + '">' + esc(m) + '</option>'));
        sel.innerHTML = opts.join('');
        if (preferred && models.includes(preferred)) sel.value = preferred;
        else if (models.length) sel.value = models[0];
    });
}

// --- Resume step ---
function obFileChosen(input) {
    const f = input.files[0];
    const label = document.getElementById('ob-file-label');
    const dz = document.getElementById('ob-dropzone');
    if (f) { label.textContent = f.name + '  (' + Math.round(f.size/1024) + ' KB)'; dz.classList.add('has-file'); }
    else   { label.textContent = 'Click to choose a PDF / DOCX / TXT file'; dz.classList.remove('has-file'); }
}

async function obParseResume() {
    const file = document.getElementById('ob-resume-file').files[0];
    const text = document.getElementById('ob-resume-text').value.trim();
    const status = document.getElementById('ob-parse-status');
    const btn = document.getElementById('ob-parse-btn');
    if (!file && !text) {
        status.className = 'ob-status err';
        status.textContent = 'Choose a file or paste text first.';
        return;
    }
    const fd = new FormData();
    if (file) fd.append('file', file);
    if (text) fd.append('text', text);
    status.className = 'ob-status busy';
    status.textContent = 'AI is reading your résumé… this can take 20–60 seconds.';
    btn.disabled = true;
    try {
        const resp = await fetch(API + '/api/onboarding/parse-resume', { method: 'POST', body: fd });
        if (!resp.ok) throw new Error(await resp.text());
        const data = await resp.json();
        _obState.parsed = data.profile;
        obFillReviewFromProfile(data.profile);
        const warn = (data.warnings || []).join('  ');
        status.className = warn ? 'ob-status' : 'ob-status ok';
        status.textContent = warn || 'Extracted. Review the next step.';
        obGoto(2);
    } catch (e) {
        status.className = 'ob-status err';
        status.textContent = 'Extraction failed: ' + (e.message || e);
    } finally {
        btn.disabled = false;
    }
}

// --- LinkedIn import (same panel as the résumé step) ---
function _obApiDetail(e) {
    // api() throws with the raw response text; surface FastAPI's detail field.
    try { return JSON.parse(e.message).detail || e.message; } catch (_) { return e.message || String(e); }
}

async function obImportLinkedIn() {
    const btn = document.getElementById('ob-linkedin-btn');
    const status = document.getElementById('ob-linkedin-status');
    btn.disabled = true;
    try {
        // No saved session yet → run the normal login flow first, then import.
        const session = await api('/api/linkedin/session');
        if (!session.has_session) {
            status.className = 'ob-status busy';
            status.textContent = 'A browser window is opening — sign in to LinkedIn there…';
            await api('/api/linkedin/login', { method: 'POST', body: '{}' });
            if (!await obWaitForLinkedInLogin(status)) return;
        }
        status.className = 'ob-status busy';
        status.textContent = 'Reading your LinkedIn profile… this can take 1–2 minutes.';
        const data = await api('/api/onboarding/import-linkedin', { method: 'POST', body: '{}' });
        _obState.parsed = data.profile;
        obFillReviewFromProfile(data.profile);
        const warn = (data.warnings || []).join('  ');
        status.className = warn ? 'ob-status' : 'ob-status ok';
        status.textContent = warn || 'Imported. Review the next step.';
        obGoto(2);
    } catch (e) {
        status.className = 'ob-status err';
        status.textContent = 'Import failed: ' + _obApiDetail(e);
    } finally {
        btn.disabled = false;
    }
}

async function obWaitForLinkedInLogin(status) {
    for (let i = 0; i < 120; i++) { // poll up to ~4 minutes
        await new Promise(r => setTimeout(r, 2000));
        const data = await api('/api/linkedin/session').catch(() => null);
        if (!data) continue;
        if (data.has_session) return true;
        if ((data.login_state || {}).status === 'failed') {
            status.className = 'ob-status err';
            status.textContent = data.login_state.message || 'LinkedIn login failed.';
            return false;
        }
    }
    status.className = 'ob-status err';
    status.textContent = 'LinkedIn login timed out — try again.';
    return false;
}

function obFillReviewFromProfile(p) {
    const set = (id, v) => { const el = document.getElementById(id); if (el && v) el.value = v; };
    set('ob-name', p.full_name);
    set('ob-email', p.email);
    set('ob-phone', p.phone);
    set('ob-location', p.location);
    set('ob-linkedin', p.linkedin);
    set('ob-summary', p.summary);
    if (Array.isArray(p.skills) && p.skills.length) document.getElementById('ob-skills').value = p.skills.join(', ');
    if (Array.isArray(p.certifications) && p.certifications.length) document.getElementById('ob-certifications').value = p.certifications.join('\n');
    if (Array.isArray(p.experience) && p.experience.length) obRenderExperience(p.experience);
    if (Array.isArray(p.education) && p.education.length) obRenderEducation(p.education);
}

// --- Wizard-scoped experience / education ---
// Fields the wizard edits; anything else on an entry (e.g. `pinned`) is
// stashed on the DOM node and merged back so a re-run doesn't strip it.
const OB_EXP_FIELDS = ['title', 'company', 'start_date', 'end_date', 'bullets'];
const OB_EDU_FIELDS = ['degree', 'school', 'year'];

function _obExtraKeys(entry, known) {
    const extra = {};
    Object.keys(entry || {}).forEach(k => { if (!known.includes(k)) extra[k] = entry[k]; });
    return extra;
}

function obRenderExperience(entries) {
    const list = document.getElementById('ob-experience-list');
    list.innerHTML = '';
    (entries || []).forEach((exp, i) => {
        const div = document.createElement('div');
        div.className = 'ob-exp';
        div.dataset.index = i;
        div.dataset.extra = JSON.stringify(_obExtraKeys(exp, OB_EXP_FIELDS));
        div.innerHTML = `
            <div class="ob-exp-header">
                <span>${esc(exp.title || 'New position')}${exp.company ? ' — ' + esc(exp.company) : ''}</span>
                <button onclick="obRemoveExperience(${i})" title="Remove">✕</button>
            </div>
            <div class="form-row-2">
                <div class="form-group"><label>Title</label><input type="text" data-field="title" value="${esc(exp.title || '')}"></div>
                <div class="form-group"><label>Company</label><input type="text" data-field="company" value="${esc(exp.company || '')}"></div>
            </div>
            <div class="form-row-2">
                <div class="form-group"><label>Start</label><input type="text" data-field="start_date" value="${esc(exp.start_date || '')}" placeholder="YYYY-MM"></div>
                <div class="form-group"><label>End</label><input type="text" data-field="end_date" value="${esc(exp.end_date || 'Present')}" placeholder="Present"></div>
            </div>
            <label style="font-size:12px;color:var(--text-secondary);margin:8px 0 4px;display:block">Bullets</label>
            <div data-bullets>
                ${(exp.bullets || []).map((b, j) => `
                    <div class="ob-bullet-row">
                        <textarea data-bullet="${j}">${esc(b)}</textarea>
                        <button onclick="obRemoveBullet(${i},${j})" title="Remove">✕</button>
                    </div>
                `).join('')}
            </div>
            <button class="btn btn-sm" onclick="obAddBullet(${i})" style="margin-top:6px">+ Bullet</button>
        `;
        list.appendChild(div);
    });
}

function obGetExperienceData() {
    const out = [];
    document.querySelectorAll('#ob-experience-list .ob-exp').forEach(div => {
        const get = f => div.querySelector(`[data-field="${f}"]`).value.trim();
        const bullets = [];
        div.querySelectorAll('[data-bullet]').forEach(t => { const v = t.value.trim(); if (v) bullets.push(v); });
        let extra = {};
        try { extra = JSON.parse(div.dataset.extra || '{}'); } catch (e) {}
        const entry = { ...extra, title: get('title'), company: get('company'), start_date: get('start_date'), end_date: get('end_date') || 'Present', bullets };
        if (entry.title || entry.company || bullets.length) out.push(entry);
    });
    return out;
}

function obAddExperience() {
    const cur = obGetExperienceData();
    cur.push({ title: '', company: '', start_date: '', end_date: 'Present', bullets: [''] });
    obRenderExperience(cur);
}
function obRemoveExperience(i) {
    const cur = obGetExperienceData(); cur.splice(i, 1); obRenderExperience(cur);
}
function obAddBullet(i) {
    const cur = obGetExperienceData();
    if (!cur[i]) return;
    cur[i].bullets.push('');
    obRenderExperience(cur);
}
function obRemoveBullet(i, j) {
    const cur = obGetExperienceData();
    if (!cur[i]) return;
    cur[i].bullets.splice(j, 1);
    obRenderExperience(cur);
}

function obRenderEducation(entries) {
    const list = document.getElementById('ob-education-list');
    list.innerHTML = '';
    (entries || []).forEach((edu, i) => {
        const div = document.createElement('div');
        div.className = 'ob-edu';
        div.dataset.index = i;
        div.dataset.extra = JSON.stringify(_obExtraKeys(edu, OB_EDU_FIELDS));
        div.innerHTML = `
            <div class="ob-edu-header">
                <span>${esc(edu.degree || 'New entry')}${edu.school ? ' — ' + esc(edu.school) : ''}</span>
                <button onclick="obRemoveEducation(${i})" title="Remove">✕</button>
            </div>
            <div class="form-row-2">
                <div class="form-group"><label>Degree</label><input type="text" data-field="degree" value="${esc(edu.degree || '')}"></div>
                <div class="form-group"><label>School</label><input type="text" data-field="school" value="${esc(edu.school || '')}"></div>
            </div>
            <div class="form-group"><label>Year</label><input type="text" data-field="year" value="${esc(edu.year || '')}" placeholder="2024"></div>
        `;
        list.appendChild(div);
    });
}
function obGetEducationData() {
    const out = [];
    document.querySelectorAll('#ob-education-list .ob-edu').forEach(div => {
        const get = f => div.querySelector(`[data-field="${f}"]`).value.trim();
        let extra = {};
        try { extra = JSON.parse(div.dataset.extra || '{}'); } catch (e) {}
        const e = { ...extra, degree: get('degree'), school: get('school'), year: get('year') };
        if (e.degree || e.school) out.push(e);
    });
    return out;
}
function obAddEducation() {
    const cur = obGetEducationData(); cur.push({ degree: '', school: '', year: '' }); obRenderEducation(cur);
}
function obRemoveEducation(i) {
    const cur = obGetEducationData(); cur.splice(i, 1); obRenderEducation(cur);
}

// --- Validation + finish ---
function obValidateProfile() {
    const errs = [];
    const name = document.getElementById('ob-name').value.trim();
    const email = document.getElementById('ob-email').value.trim();
    if (!name) errs.push('Full name is required.');
    if (!email) errs.push('Email is required.');
    else if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) errs.push('Email is not a valid address.');
    const box = document.getElementById('ob-validation');
    if (errs.length) {
        box.style.display = 'block';
        box.innerHTML = errs.map(e => '• ' + esc(e)).join('<br>');
        return false;
    }
    box.style.display = 'none';
    return true;
}

function obSplitCsv(v) { return (v || '').split(',').map(s => s.trim()).filter(Boolean); }
function obSplitLines(v) { return (v || '').split('\n').map(s => s.trim()).filter(Boolean); }

function obBuildPayload() {
    return {
        profile: {
            full_name: document.getElementById('ob-name').value.trim(),
            email: document.getElementById('ob-email').value.trim(),
            phone: document.getElementById('ob-phone').value.trim(),
            location: document.getElementById('ob-location').value.trim(),
            linkedin: document.getElementById('ob-linkedin').value.trim(),
            summary: document.getElementById('ob-summary').value.trim(),
            skills: splitCsvSmart(document.getElementById('ob-skills').value),
            experience: obGetExperienceData(),
            education: obGetEducationData(),
            certifications: obSplitLines(document.getElementById('ob-certifications').value),
            workday_email: document.getElementById('ob-workday-email').value.trim(),
            workday_password: document.getElementById('ob-workday-password').value,
        },
        search: {
            keywords: obSplitCsv(document.getElementById('ob-keywords').value),
            locations: obSplitLines(document.getElementById('ob-locations').value),
            min_salary: parseInt(document.getElementById('ob-salary').value, 10) || 0,
            exclude_keywords: obSplitCsv(document.getElementById('ob-exclude').value),
        },
        ai: {
            base_url: document.getElementById('ob-ai-url').value.trim(),
            models: {
                strong: { model: document.getElementById('ob-ai-model-strong').value },
                fast: { model: document.getElementById('ob-ai-model-fast').value },
                utility: { model: document.getElementById('ob-ai-model-utility').value },
            },
        },
        api_keys: {
            adzuna_app_id: document.getElementById('ob-adzuna-app-id').value.trim(),
            adzuna_app_key: document.getElementById('ob-adzuna-app-key').value.trim(),
        },
        salary_estimator: { bls: { api_key: document.getElementById('ob-bls-key').value.trim() } },
    };
}

async function obFinish() {
    if (!obValidateProfile()) { obGoto(2); return; }
    const payload = obBuildPayload();
    const nextBtn = document.getElementById('ob-next');
    nextBtn.disabled = true; nextBtn.textContent = 'Saving…';
    try {
        await api('/api/config', { method: 'POST', body: JSON.stringify(payload) });
        await api('/api/onboarding/complete', { method: 'POST', body: '{}' });
        toast('You’re all set — welcome aboard!', 'success');
        obHide();
        if (location.hash === '#settings') loadSettings();
        else location.hash = 'dashboard';
        setTimeout(() => tourStart(), 400);
    } catch (e) {
        toast('Could not save setup: ' + (e.message || e), 'error');
    } finally {
        nextBtn.disabled = false;
        nextBtn.textContent = 'Finish ✓';
    }
}

// ============================================================
// Re-run mode: Review-changes diff panel
// ============================================================
// Each row: label + path into both the current config and the wizard payload.
// ai.models is one row because POST /api/config shallow-merges the ai
// section — applying a single tier would clobber the other two.
const OB_DIFF_FIELDS = [
    { label: 'Full name', path: ['profile', 'full_name'] },
    { label: 'Email', path: ['profile', 'email'] },
    { label: 'Phone', path: ['profile', 'phone'] },
    { label: 'Location', path: ['profile', 'location'] },
    { label: 'LinkedIn', path: ['profile', 'linkedin'] },
    { label: 'Summary', path: ['profile', 'summary'] },
    { label: 'Skills', path: ['profile', 'skills'] },
    { label: 'Experience', path: ['profile', 'experience'],
      fmt: v => (v || []).length ? (v || []).map(e => `${e.title || '?'} @ ${e.company || '?'}`).join('; ') : '(empty)' },
    { label: 'Education', path: ['profile', 'education'],
      fmt: v => (v || []).length ? (v || []).map(e => `${e.degree || '?'} — ${e.school || '?'}`).join('; ') : '(empty)' },
    { label: 'Certifications', path: ['profile', 'certifications'] },
    { label: 'Workday email', path: ['profile', 'workday_email'] },
    { label: 'Workday password', path: ['profile', 'workday_password'], secret: true },
    { label: 'Search keywords', path: ['search', 'keywords'] },
    { label: 'Search locations', path: ['search', 'locations'] },
    { label: 'Minimum salary', path: ['search', 'min_salary'],
      eq: (a, b) => Number(a || 0) === Number(b || 0) },
    { label: 'Exclude keywords', path: ['search', 'exclude_keywords'] },
    { label: 'LM Studio URL', path: ['ai', 'base_url'] },
    { label: 'AI models', path: ['ai', 'models'], skipIfEmptyNew: true,
      // Tier-wise merge so applying model picks keeps per-tier base_url/api_key overrides
      merge: (cur, nxt) => {
          const out = { ...(cur || {}) };
          ['strong', 'fast', 'utility'].forEach(t => {
              const m = nxt?.[t]?.model;
              if (m) out[t] = { ...(out[t] || {}), model: m };
          });
          return out;
      },
      fmt: v => ['strong', 'fast', 'utility'].map(t => `${t}: ${(v || {})[t]?.model || '—'}`).join(', ') },
    { label: 'Adzuna App ID', path: ['api_keys', 'adzuna_app_id'] },
    { label: 'Adzuna App Key', path: ['api_keys', 'adzuna_app_key'], secret: true },
    { label: 'BLS API key', path: ['salary_estimator', 'bls', 'api_key'], secret: true },
];

function _obGetIn(obj, path) { return path.reduce((o, k) => (o == null ? undefined : o[k]), obj); }

// Canonical form for change detection: trims strings (YAML folded scalars
// keep a trailing newline the form fields lose) and sorts object keys (the
// wizard rebuilds entries in a fixed key order).
function _obStable(v) {
    if (Array.isArray(v)) return v.map(_obStable);
    if (v && typeof v === 'object') {
        const o = {};
        Object.keys(v).sort().forEach(k => { o[k] = _obStable(v[k]); });
        return o;
    }
    return typeof v === 'string' ? v.trim() : v;
}

function _obFmtVal(v, field) {
    if (field.secret) return v ? '••••••' : '(empty)';
    if (field.fmt) return field.fmt(v);
    if (Array.isArray(v)) return v.length ? v.map(x => (typeof x === 'string' ? x : JSON.stringify(x))).join(', ') : '(empty)';
    if (v === undefined || v === null || v === '') return '(empty)';
    return String(v);
}

async function obShowDiff() {
    if (!obValidateProfile()) { obGoto(2); return; }
    let cfg;
    try {
        cfg = await api('/api/config');
    } catch (e) {
        toast('Could not load current config: ' + (e.message || e), 'error');
        return;
    }
    const payload = obBuildPayload();
    const rows = [];
    OB_DIFF_FIELDS.forEach(f => {
        const cur = _obGetIn(cfg, f.path);
        let nxt = _obGetIn(payload, f.path);
        // AI models: nothing selected in the wizard (AI offline) is "no change"
        if (f.skipIfEmptyNew && (!nxt || Object.values(nxt).every(m => !(m && m.model)))) return;
        if (f.merge) nxt = f.merge(cur, nxt);
        const changed = f.eq
            ? !f.eq(cur, nxt)
            : JSON.stringify(_obStable(cur) ?? '') !== JSON.stringify(_obStable(nxt) ?? '');
        if (changed) rows.push({ field: f, cur, nxt });
    });
    _obState.diff = rows;
    const list = document.getElementById('ob-diff-list');
    if (!rows.length) {
        list.innerHTML = '<p class="ob-hint" style="font-size:13px">No changes — everything in the wizard matches your saved config. Applying will leave it untouched.</p>';
    } else {
        list.innerHTML = rows.map((r, i) => `
            <label class="ob-diff-row">
                <input type="checkbox" data-diff-idx="${i}" checked>
                <div class="ob-diff-field">${esc(r.field.label)}</div>
                <div class="ob-diff-vals">
                    <div class="ob-diff-old">${esc(_obFmtVal(r.cur, r.field))}</div>
                    <div class="ob-diff-new">${esc(_obFmtVal(r.nxt, r.field))}</div>
                </div>
            </label>`).join('');
    }
    obGoto(5);
}

async function obApplyDiff() {
    const rows = _obState.diff || [];
    const selected = [];
    document.querySelectorAll('#ob-diff-list input[data-diff-idx]:checked').forEach(cb => {
        const r = rows[parseInt(cb.dataset.diffIdx, 10)];
        if (r) selected.push(r);
    });
    const payload = {};
    const setIn = (obj, path, val) => {
        let o = obj;
        for (let i = 0; i < path.length - 1; i++) { o[path[i]] = o[path[i]] || {}; o = o[path[i]]; }
        o[path[path.length - 1]] = val;
    };
    selected.forEach(r => setIn(payload, r.field.path, r.nxt));
    const nextBtn = document.getElementById('ob-next');
    nextBtn.disabled = true;
    nextBtn.textContent = 'Saving…';
    try {
        if (Object.keys(payload).length) {
            await api('/api/config', { method: 'POST', body: JSON.stringify(payload) });
        }
        await api('/api/onboarding/complete', { method: 'POST', body: '{}' });
        toast(selected.length
            ? `Applied ${selected.length} change${selected.length === 1 ? '' : 's'}.`
            : 'Setup closed — config unchanged.', 'success');
        obHide();
        if (location.hash === '#settings') loadSettings();
    } catch (e) {
        toast('Could not apply changes: ' + (e.message || e), 'error');
    } finally {
        nextBtn.disabled = false;
        nextBtn.textContent = 'Apply selected ✓';
    }
}

// ============================================================
// Post-setup product tour
// ============================================================
const TOUR_STEPS = [
    {
        hash: '#dashboard',
        selector: '.stats-row',
        title: 'Your dashboard',
        body: 'This is your command center. These stat cards summarize jobs ingested, pending review, submitted applications, and your overall fit. Click any of them to jump to filtered views.',
    },
    {
        hash: '#dashboard',
        selector: '.action-cards',
        title: 'Quick actions',
        body: 'Each card kicks off a workflow — fetch new jobs, score them against your profile, estimate salaries, or tailor resumes. Start here for any one-off task.',
    },
    {
        hash: '#jobs',
        selector: '.filter-bar',
        title: 'Find jobs',
        body: 'Filter your ingested jobs by keyword, location, salary, score, and more. The advanced toggle exposes finer controls like source, status, and date range.',
    },
    {
        hash: '#jobs',
        selector: '.jobs-split-pane',
        title: 'Your day-to-day workflow',
        body: 'This is where you spend most of your time. The flow: pick a job on the left → in the detail pane, click "Tailor Resume" → wait for it to generate → click "Apply Assist" to open the posting with the extension sidebar → submit on the live ATS → hit "Mark Applied" from the sidebar or the detail pane when done. The job\'s status updates everywhere.',
    },
    {
        hash: '#review',
        selector: '.review-tab-bar',
        title: 'Review queue',
        body: 'Once you tailor a job, its tailored resume and cover letter live here. Open the Pending tab to read through what the AI produced, use AI Edit to revise anything, then launch Apply Assist from this view (or from the Job Feed detail pane). Submitted / Failed / In Progress tabs let you audit past applications. Note: "Approve" is an audit-trail status — it does not submit.',
    },
    {
        hash: '#settings',
        selector: '.settings-tabs',
        title: 'Settings, tab by tab',
        body: 'Configuration is grouped into seven tabs. The next stops walk through each one so nothing feels like a mystery toggle.',
    },
    {
        hash: '#settings',
        selector: '#stab-search',
        title: 'Search',
        body: 'Controls what jobs Jobsmith ingests: keywords, locations, min salary, exclusion terms, which sources to scrape, and whether to auto-estimate salary on ingest. Tighten these if your feed is too noisy; loosen them if you\'re not seeing enough.',
        before: () => _tourSwitchSettingsTab('stab-search'),
    },
    {
        hash: '#settings',
        selector: '#stab-integrations',
        title: 'Integrations',
        body: 'External services: LM Studio AI endpoint + model selection (Strong / Fast / Utility), LinkedIn login for richer scraping, Adzuna and BLS API keys for salary data, FlareSolverr URL for Cloudflare-protected boards, and n8n webhook URLs if you wire up scheduling. None are required — connect what you have.',
        before: () => _tourSwitchSettingsTab('stab-integrations'),
    },
    {
        hash: '#settings',
        selector: '#stab-honesty',
        title: 'Honesty levels',
        body: 'Pick how much latitude the AI takes when tailoring: honest (only restate facts), tailored (rephrase for emphasis), embellished (stretch a bit), or fabricated (invent — generally avoid). You can override this per generation. This setting shapes every resume, cover letter, and answer the AI produces.',
        before: () => _tourSwitchSettingsTab('stab-honesty'),
    },
    {
        hash: '#settings',
        selector: '#stab-assist',
        title: 'Apply Assist',
        body: 'This is the main way you apply — and it runs inside your normal Chrome or Firefox via our browser extension (no separate browser is launched). On Firefox, click "Install for Firefox (signed)" for a permanent, Mozilla-signed add-on; on Chrome, download the zip and load it unpacked. Then paste the token below into the extension popup. After that, clicking "Apply Assist" on any job injects a sidebar with your tailored materials and autofills standard fields right on the live ATS page — and you can click any field in the sidebar to copy its value.',
        before: () => _tourSwitchSettingsTab('stab-assist'),
    },
    {
        hash: '#settings',
        selector: '#stab-answerbank',
        title: 'Answer Bank',
        body: 'Every time you answer a custom application question (work auth, sponsorship, why this company, etc.), it gets stored here so the next form gets pre-filled automatically. Edit or delete entries any time if your answers change.',
        before: () => _tourSwitchSettingsTab('stab-answerbank'),
    },
    {
        hash: '#settings',
        selector: '#settings-replay-tour',
        title: 'Replay anytime',
        body: 'Done! You can re-run this tour anytime from this button under Settings → Profile. Happy applying.',
        before: () => _tourSwitchSettingsTab('stab-profile'),
    },
];

function _tourSwitchSettingsTab(panelId) {
    const btn = document.querySelector(`.settings-tab[onclick*="${panelId}"]`);
    if (btn) switchSettingsTab(btn, panelId);
}

let _tourState = { step: 0, open: false, target: null, rafId: 0 };

async function tourStart() {
    if (_tourState.open) return;
    _tourState.open = true;
    _tourState.step = 0;
    const overlay = document.getElementById('tour-overlay');
    if (!overlay) { _tourState.open = false; return; }
    overlay.style.display = 'block';
    overlay.setAttribute('aria-hidden', 'false');
    window.addEventListener('resize', _tourReposition, { passive: true });
    window.addEventListener('scroll', _tourReposition, { passive: true, capture: true });
    tourGoto(0);
}

function tourGoto(i) {
    if (i < 0 || i >= TOUR_STEPS.length) return;
    _tourState.step = i;
    const step = TOUR_STEPS[i];
    const needsNav = location.hash !== step.hash;
    if (needsNav) location.hash = step.hash;
    // Give the page a tick to render after hash change
    const delay = needsNav ? 220 : 30;
    setTimeout(() => {
        if (typeof step.before === 'function') { try { step.before(); } catch (e) { console.warn('tour before hook failed', e); } }
        _tourRender();
    }, delay);
}

function _tourRender() {
    if (!_tourState.open) return;
    const step = TOUR_STEPS[_tourState.step];
    const target = document.querySelector(step.selector);
    _tourState.target = target;
    const overlay = document.getElementById('tour-overlay');
    const popover = overlay.querySelector('.tour-popover');
    overlay.querySelector('.tour-popover-title').textContent = step.title;
    overlay.querySelector('.tour-popover-body').textContent = step.body;
    overlay.querySelector('.tour-step-indicator').textContent = `${_tourState.step + 1} / ${TOUR_STEPS.length}`;
    const prevBtn = overlay.querySelector('.tour-prev-btn');
    const nextBtn = overlay.querySelector('.tour-next-btn');
    prevBtn.style.visibility = _tourState.step === 0 ? 'hidden' : 'visible';
    nextBtn.textContent = _tourState.step === TOUR_STEPS.length - 1 ? 'Finish ✓' : 'Next →';
    if (!target) {
        console.warn('tour: target not found for', step.selector);
        // Skip ahead if we can
        if (_tourState.step < TOUR_STEPS.length - 1) { tourGoto(_tourState.step + 1); return; }
    }
    _tourReposition();
    requestAnimationFrame(() => popover.classList.add('tour-popover-visible'));
    // Bring target into view
    if (target && typeof target.scrollIntoView === 'function') {
        try { target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' }); } catch (e) {}
    }
}

function _tourReposition() {
    if (!_tourState.open) return;
    if (_tourState.rafId) cancelAnimationFrame(_tourState.rafId);
    _tourState.rafId = requestAnimationFrame(() => {
        const overlay = document.getElementById('tour-overlay');
        if (!overlay) return;
        const hole = overlay.querySelector('.tour-mask-hole');
        const popover = overlay.querySelector('.tour-popover');
        const target = _tourState.target;
        const padding = 8;
        if (target && hole) {
            const r = target.getBoundingClientRect();
            const x = Math.max(0, r.left - padding);
            const y = Math.max(0, r.top - padding);
            const w = Math.min(window.innerWidth - x, r.width + padding * 2);
            const h = Math.min(window.innerHeight - y, r.height + padding * 2);
            hole.setAttribute('x', x);
            hole.setAttribute('y', y);
            hole.setAttribute('width', w);
            hole.setAttribute('height', h);
            _tourPositionPopover(popover, { x, y, w, h });
        } else if (hole) {
            hole.setAttribute('width', 0);
            hole.setAttribute('height', 0);
            // Center popover
            popover.style.left = `calc(50vw - ${popover.offsetWidth / 2}px)`;
            popover.style.top = `calc(50vh - ${popover.offsetHeight / 2}px)`;
        }
    });
}

function _tourPositionPopover(popover, hole) {
    const margin = 14;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const pw = popover.offsetWidth || 320;
    const ph = popover.offsetHeight || 160;
    // Prefer right of hole
    let left = hole.x + hole.w + margin;
    let top = hole.y;
    if (left + pw > vw - margin) {
        // Try below
        left = Math.max(margin, Math.min(vw - pw - margin, hole.x));
        top = hole.y + hole.h + margin;
        if (top + ph > vh - margin) {
            // Try above
            top = hole.y - ph - margin;
            if (top < margin) {
                // Try left
                left = hole.x - pw - margin;
                top = Math.max(margin, Math.min(vh - ph - margin, hole.y));
                if (left < margin) {
                    // Fall back: bottom-center of viewport
                    left = (vw - pw) / 2;
                    top = vh - ph - margin;
                }
            }
        }
    }
    // Clamp into viewport
    left = Math.max(margin, Math.min(vw - pw - margin, left));
    top = Math.max(margin, Math.min(vh - ph - margin, top));
    popover.style.left = left + 'px';
    popover.style.top = top + 'px';
}

function tourNext() {
    if (_tourState.step >= TOUR_STEPS.length - 1) { tourFinish(); return; }
    const overlay = document.getElementById('tour-overlay');
    overlay.querySelector('.tour-popover').classList.remove('tour-popover-visible');
    tourGoto(_tourState.step + 1);
}

function tourPrev() {
    if (_tourState.step <= 0) return;
    const overlay = document.getElementById('tour-overlay');
    overlay.querySelector('.tour-popover').classList.remove('tour-popover-visible');
    tourGoto(_tourState.step - 1);
}

async function tourSkip() {
    await _tourClose(true);
    toast('Tour skipped — replay it anytime from Settings → Profile.', 'info');
}

async function tourFinish() {
    await _tourClose(true);
    toast('You’re ready to go!', 'success');
}

async function _tourClose(markComplete) {
    _tourState.open = false;
    const overlay = document.getElementById('tour-overlay');
    if (overlay) {
        overlay.style.display = 'none';
        overlay.setAttribute('aria-hidden', 'true');
        overlay.querySelector('.tour-popover').classList.remove('tour-popover-visible');
    }
    window.removeEventListener('resize', _tourReposition);
    window.removeEventListener('scroll', _tourReposition, { capture: true });
    if (markComplete) {
        try { await api('/api/onboarding/tour-complete', { method: 'POST', body: '{}' }); } catch (e) {}
    }
}

async function tourReplay() {
    try { await api('/api/onboarding/tour-reset', { method: 'POST', body: '{}' }); } catch (e) {}
    tourStart();
}
