// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ============================================================
// First-run onboarding wizard
// ============================================================
const OB_STEPS = 5;
// rerun: wizard opened with an already-populated profile — finish goes
// through the Review-changes diff panel instead of overwriting config.
// ai: step 0's snapshot (see obCollectAI). only: 'ai' when opened from
// Settings → AI → Change setup… (step 0 alone, back to Settings when done).
// aiPending: a re-run's tested-but-unsaved AI choice, written by the Review step.
let _obState = { step: 0, parsed: null, open: false, rerun: false, diff: [],
                 ai: { mode: '' }, onDeviceStatus: null, only: '', aiPending: null, aiLater: false, cloudWriting: '',
                 savedSearch: {}, companies: null };

document.addEventListener('DOMContentLoaded', () => { obCheckStatus(); });

async function obCheckStatus() {
    try {
        const s = await api('/api/onboarding/status');
        window._onbStatus = s;  // A3: reused by the checklist instead of refetching
        if (s.needs_onboarding) { obOpen({ status: s }); return; }
        if (!s.tour_complete) {
            // C1 — the tour narrates a UI full of jobs; on an empty app it
            // points at blank panes. Only auto-start when there is data to
            // narrate, otherwise offer it alongside the action that creates
            // that data. One extra /api/stats call, and only on this branch.
            let total = 0;
            if (window._lastStats) total = window._lastStats.total_jobs || 0;
            else {
                try {
                    const st = await api('/api/stats');
                    window._lastStats = st;
                    total = st.total_jobs || 0;
                } catch (e) { total = 0; }
            }
            if (total > 0) setTimeout(() => tourStart(), 300);
            else obTourOffer();
        }
    } catch (e) { console.error('onboarding status failed', e); }
}

// ---- C1: tour offer banner (empty app) ----
// Sits alongside the C3 'first-fetch' banner: separate id, separate hide, so
// neither clobbers the other. Fetching from here hides the offer because the
// post-fetch hook in dashboard.js takes over from there.
function obTourOffer() {
    if (typeof showBanner !== 'function') return;
    showBanner('tour-offer', {
        tone: 'info',
        dismissible: true,
        message: 'New here? Fetch your first jobs, then take the 2-minute tour.',
        actions: [
            {
                label: 'Fetch jobs',
                onClick: () => {
                    if (typeof stageFetch === 'function') stageFetch();
                    if (typeof hideBanner === 'function') hideBanner('tour-offer');
                },
            },
            {
                label: 'Start tour anyway',
                onClick: () => {
                    if (typeof hideBanner === 'function') hideBanner('tour-offer');
                    tourStart();
                },
            },
        ],
    });
}

function obOpen({ status, only = '' } = {}) {
    _obState.step = 0;
    _obState.parsed = null;
    _obState.open = true;
    _obState.only = only;
    _obState.aiPending = null;
    _obState.aiLater = false;
    _obState.cloudWriting = '';
    _obState.companies = null;
    _obState.ai = _obNewAI();
    document.getElementById('onboarding-overlay').style.display = 'flex';
    document.getElementById('ob-stepper').style.display = only ? 'none' : '';
    obSelectMode('');
    obGoto(0);
    // Status first (the Local card's real state on first paint), then the
    // presets and saved values, which pick the saved mode back up.
    const statusP = status ? Promise.resolve(status) : api('/api/onboarding/status').catch(() => ({}));
    statusP.then(obApplyAIStatus);
    obLoadSizes();
    Promise.all([statusP, obLoadProviders(), obLoadPrefill()]).then(([s, , cfg]) => obPrefillAI(s || {}, cfg));
}

// Re-opening the wizard shows the saved choice (setup_mode + ai.provider).
function obPrefillAI(s, cfg) {
    const ai = (cfg && cfg.ai) || {};
    const models = ai.models || {};
    const mode = s.setup_mode || '';
    // What reads a résumé until a new choice is saved (see obResumePrivacy).
    _obState.savedAI = {
        mode: mode || (models.strong?.model === OB_ON_DEVICE_MODEL ? 'local' : (models.strong?.model ? 'cloud' : '')),
        provider: ai.provider || '', base_url: ai.base_url || '',
    };
    if (mode === 'cloud' && ai.provider) {
        const sel = document.getElementById('ob-cloud-provider');
        sel.value = ai.provider;
        if (sel.value === ai.provider) {
            obSelectMode('cloud');
            if (ai.provider === 'custom') document.getElementById('ob-cloud-url').value = ai.base_url || '';
            document.getElementById('ob-cloud-key').value = ai.api_key || '';
            obSelectProvider(ai.provider, { keepUrl: true });
            _obState.cloudWriting = models.strong?.model || '';
            const free = document.getElementById('ob-model-id');
            if (free) free.value = _obState.cloudWriting;
            obReadForm();
        }
    } else if (mode === 'local' && _obLocalAvailable()) {
        obSelectMode('local');
    } else if (mode === 'advanced') {
        obSelectMode('advanced');
    }
}

function obHide() {
    document.getElementById('onboarding-overlay').style.display = 'none';
    _obState.open = false;
    _obState.only = '';
}

async function obLoadPrefill() {
    let cfg = null;
    try {
        cfg = await api('/api/config');
        const p = cfg.profile || {};
        const isExample = !p.full_name || p.full_name === 'Jane Doe' || p.email === 'jane.doe@example.com';
        _obState.rerun = !isExample;
        const reviewChip = document.getElementById('ob-step-review');
        if (reviewChip) reviewChip.style.display = _obState.rerun ? '' : 'none';
        // Advanced starts from today's saved values; Cloud is filled by obPrefillAI.
        document.getElementById('ob-ai-url').value = cfg.ai?.base_url || '';
        document.getElementById('ob-ai-api-key').value = cfg.ai?.api_key || '';
        document.getElementById('ob-adv-scoring').value = cfg.ai?.scoring_tier || 'strong';
        document.getElementById('ob-adv-nli').checked = !!cfg.ai?.nli_beta?.enabled;
        const models = cfg.ai?.models || {};
        document.getElementById('ob-ai-model-strong').dataset.preferred = models.strong?.model || '';
        document.getElementById('ob-ai-model-fast').dataset.preferred = models.fast?.model || '';
        document.getElementById('ob-ai-model-utility').dataset.preferred = models.utility?.model || '';
        obPopulateModels([]);
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
        _obState.savedSearch = s;
        document.getElementById('ob-keywords').value = (s.keywords || []).join(', ');
        document.getElementById('ob-locations').value = (s.locations || []).join('\n');
        document.getElementById('ob-salary').value = s.min_salary || '';
        document.getElementById('ob-exclude').value = (s.exclude_keywords || []).join(', ');
        const k = cfg.api_keys || {};
        document.getElementById('ob-adzuna-app-id').value = realKey(k.adzuna_app_id);
        document.getElementById('ob-adzuna-app-key').value = realKey(k.adzuna_app_key);
        document.getElementById('ob-usajobs-email').value = realKey(k.usajobs_email);
        document.getElementById('ob-usajobs-key').value = realKey(k.usajobs_api_key);
        document.getElementById('ob-bls-key').value = cfg.salary_estimator?.bls?.api_key || '';
    } catch (e) { console.error('obLoadPrefill failed', e); }
    return cfg;
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
    else if (step === 0) nextBtn.textContent = 'Continue →';
    else if (step === OB_STEPS - 1) nextBtn.textContent = _obState.rerun ? 'Review changes →' : 'Finish ✓';
    else nextBtn.textContent = 'Next →';
    if (step === 1) obResumePrivacy();
    if (step === 4) obEnterSources();
    const body = document.querySelector('.ob-body');
    if (body) body.scrollTop = 0;
}

// Résumé step: say where the résumé goes, for the AI that will actually read it
// (a re-run parses with the saved setup until the Review step applies a new one).
function obResumePrivacy() {
    const el = document.getElementById('ob-resume-privacy');
    if (!el) return;
    const deferred = _obState.rerun && !_obState.only;
    const ai = (deferred || _obState.aiLater) ? (_obState.savedAI || {}) : _obState.ai;
    const host = (() => { try { return new URL(ai.base_url).host; } catch (e) { return ''; } })();
    let text;
    if (ai.mode === 'local') text = 'Apple Intelligence will fill in your profile. Your résumé stays on this Mac.';
    else if (ai.provider && ai.provider !== 'custom') text = 'your AI will fill in your profile. Your résumé is sent to ' + ai.provider + ' to read it.';
    else if (host) text = 'your AI will fill in your profile. Your résumé is sent to your AI server at ' + host + ' to read it.';
    else text = 'your AI will fill in your profile. Set up AI first, or skip and fill it in by hand.';
    el.textContent = text;
}

function obBack() { if (_obState.step > 0) obGoto(_obState.step - 1); }

function obNext() {
    const step = _obState.step;
    if (step === 0) { obAIContinue(); return; }
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
    if (_obState.only) { obHide(); return; }  // Change setup…: just close
    if (!(await appConfirm('Skip first-time setup? You can re-run it anytime from Settings → App.'))) return;
    try { await api('/api/onboarding/complete', { method: 'POST', body: '{}' }); } catch (e) {}
    obHide();
    toast('You can run setup again from Settings → App', 'info');
    window._onbStatus = null;  // A3: profile/pairing state just changed
    if (typeof checkAIStatus === 'function') checkAIStatus();  // A1
    obFirstFetchNudge();  // C3
}

// ---- C3: post-wizard first-fetch nudge ----
// Leaving the wizard drops the user on an empty app with no obvious next move.
// Park a banner on the dashboard with the one action that matters. It is
// self-contained (own banner id, own hide) so a later 'tour-offer' banner for
// the same moment can sit alongside it. Dismissed for good once jobs exist —
// see hideBanner('first-fetch') in dashboard.js.
function obFirstFetchNudge() {
    location.hash = 'dashboard';
    if (typeof showBanner !== 'function') return;
    showBanner('first-fetch', {
        tone: 'info',
        dismissible: true,
        message: 'Setup complete — fetch your first batch of jobs.',
        actions: [{
            label: 'Fetch now',
            onClick: () => {
                if (typeof stageFetch === 'function') stageFetch();
                if (typeof hideBanner === 'function') hideBanner('first-fetch');
            },
        }],
    });
}

function obRelaunch() { obOpen(); }

// Settings → AI → Change setup…: step 0 only, then back to Settings.
function obChangeSetup() { obOpen({ only: 'ai' }); }

// --- AI step (Setup Assistant: Local / Cloud / Advanced) ---
// _obState.ai is the one snapshot of what step 0 decided; obCollectAI() rebuilds
// it from the form, and the shared exit (obAIContinue) tests then saves exactly
// that snapshot — so nothing stale (bug: the old onDevice flag) can leak in.
const OB_ON_DEVICE_MODEL = 'apple-on-device';  // the sentinel the backend routes on (apple_bridge)
const OB_QUICK_MATCH = 'local-match-model';    // ai.scoring_tier value for Quick match
// Model ids that are obviously not chat models; hidden unless "Show all models".
const OB_NON_CHAT = /embed|whisper|tts|rerank|moderation|dall-e|image/i;
let _obProviders = [];   // GET /api/ai/providers — Custom is appended here, not in the list
let _obModels = [];      // the Cloud picker's last fetched list
let _obAdvModels = [];   // Advanced "Load models" result
let _obKeyTimer = 0;

function _obNewAI(mode = '') {
    return { mode, provider: null, base_url: null, api_key: null,
             models: { strong: '', fast: '', utility: '' },
             scoring_tier: 'strong', nli: null, triage: false };
}

function _obVal(id) { const el = document.getElementById(id); return el ? (el.value || '').trim() : ''; }

// Custom URL clean-up: add a scheme, drop a pasted /chat/completions suffix.
function obNormalizeUrl(u) {
    u = (u || '').trim();
    if (!u) return '';
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(u)) u = 'https://' + u;
    return u.replace(/\/+$/, '').replace(/\/chat\/completions$/i, '').replace(/\/+$/, '');
}

function obFilterModels(models, query, showAll) {
    const q = (query || '').trim().toLowerCase();
    return (models || []).filter(m => (showAll || !OB_NON_CHAT.test(m)) && (!q || m.toLowerCase().includes(q)));
}

async function obLoadProviders() {
    try { _obProviders = await api('/api/ai/providers'); } catch (e) { _obProviders = []; }
    const opts = _obProviders.map(p => '<option value="' + esc(p.name) + '">' + esc(p.name) + '</option>').join('');
    const cloud = document.getElementById('ob-cloud-provider');
    const cur = cloud.value;
    cloud.innerHTML = '<option value="">Choose a provider…</option>' + opts + '<option value="custom">Custom</option>';
    cloud.value = cur;
    const adv = document.getElementById('ob-adv-provider');
    adv.innerHTML = '<option value="">Custom / other</option>' + opts;
}

// "about N MB" is computed from the pinned file sizes the backend reports.
async function obLoadSizes() {
    const mb = b => Math.round((b || 0) / 1e6);
    try {
        const [t, n] = await Promise.all([api('/api/ai/triage/status'), api('/api/ai/nli/status')]);
        document.getElementById('ob-local-size').textContent = mb((t.size_bytes || 0) + (n.size_bytes || 0));
        document.getElementById('ob-triage-size').textContent = mb(t.size_bytes);
    } catch (e) { /* the copy keeps its "…" placeholder */ }
}

function _obLocalAvailable() { return !!(_obState.onDeviceStatus && _obState.onDeviceStatus.available); }

function obLocalReason(od) {
    if (!od || od.available) return '';
    const r = od.reason || '';
    if (!od.supported) return (!r || /macOS|Apple Silicon/i.test(r)) ? 'Needs a Mac with Apple silicon on macOS 26+' : r;
    if (/turned off|not enabled|disabled|turn on|enable/i.test(r)) return 'Turn on Apple Intelligence in System Settings, then tap Check again';
    if (/download|not ready|preparing/i.test(r)) return 'Apple Intelligence is still downloading its model. Tap Check again in a few minutes.';
    return r || 'Apple Intelligence is not available right now';
}

// Paint the Local card from a /api/onboarding/status payload (its on_device field).
function obApplyAIStatus(s) {
    _obState.onDeviceStatus = (s && s.on_device) || { supported: false, available: false, reason: '' };
    const ok = _obLocalAvailable();
    document.getElementById('ob-mode-local').disabled = !ok;
    const reason = document.getElementById('ob-local-reason');
    reason.textContent = obLocalReason(_obState.onDeviceStatus);
    reason.style.display = ok ? 'none' : '';
    document.getElementById('ob-local-recheck').style.display = ok ? 'none' : '';
    if (!ok && _obState.ai.mode === 'local') obSelectMode('');
    obPopulateModels(_obAdvModels);  // offer (or drop) Apple Intelligence in Advanced
}

async function obCheckLocal() {
    try { obApplyAIStatus(await api('/api/onboarding/status')); } catch (e) { /* keep the last answer */ }
}

function obSelectMode(mode) {
    if (mode === 'local' && !_obLocalAvailable()) return;
    ['local', 'cloud', 'advanced'].forEach(m => {
        const card = document.getElementById('ob-mode-' + m);
        card.classList.toggle('selected', m === mode);
        card.setAttribute('aria-checked', String(m === mode));
        document.getElementById('ob-sub-' + m).style.display = m === mode ? '' : 'none';
    });
    _obState.ai = _obNewAI(mode);
    obClearExit();
    obReadForm();
}

// Local = Apple Intelligence on every tier; Settings and older tests call it by this name.
function obUseAppleIntelligence() { obSelectMode('local'); }

function obSelectProvider(name, { keepUrl = false } = {}) {
    const p = _obProviders.find(x => x.name === name);
    const url = document.getElementById('ob-cloud-url');
    if (p) url.value = p.base_url;
    else if (!keepUrl) url.value = '';  // Custom starts empty
    if (!keepUrl) document.getElementById('ob-cloud-key').value = '';  // keys are per provider
    url.readOnly = !!p;
    document.getElementById('ob-cloud-edit-url').style.display = p ? '' : 'none';
    document.getElementById('ob-cloud-url-help').style.display = name === 'custom' ? '' : 'none';
    document.getElementById('ob-cloud-key-opt').style.display = p ? 'none' : '';  // a preset needs a key
    const link = document.getElementById('ob-cloud-key-link');
    link.style.display = p ? '' : 'none';
    if (p) link.href = p.key_url;
    _obModels = [];
    _obState.cloudWriting = '';
    document.getElementById('ob-model-picker').style.display = 'none';
    document.getElementById('ob-model-free').style.display = 'none';
    document.getElementById('ob-model-status').textContent = '';
    obUrlWarn();
    obClearExit();
    obReadForm();
    if (name === 'custom' ? !!url.value : (p && _obVal('ob-cloud-key'))) obListModels();
}

// "Edit address": switch to Custom but keep the preset's URL to edit.
function obEditAddress() {
    document.getElementById('ob-cloud-provider').value = 'custom';
    obSelectProvider('custom', { keepUrl: true });
}

function obUrlWarn() {
    const custom = _obVal('ob-cloud-provider') === 'custom';
    const url = _obVal('ob-cloud-url');
    // Presets (Gemini's path is /v1beta/openai) never warn; Custom warns, never blocks.
    document.getElementById('ob-cloud-url-warn').style.display =
        custom && url && !/\/v1$/.test(obNormalizeUrl(url)) ? '' : 'none';
}

function obCloudUrlChanged() {
    const el = document.getElementById('ob-cloud-url');
    el.value = obNormalizeUrl(el.value);
    obUrlWarn();
    obReadForm();
    if (_obVal('ob-cloud-provider') === 'custom' && el.value) obListModels();
}

function obKeyTyped() {
    clearTimeout(_obKeyTimer);
    _obKeyTimer = setTimeout(obListModels, 700);
}

async function obListModels() {
    clearTimeout(_obKeyTimer);
    obReadForm();
    const ai = _obState.ai;
    if (ai.mode !== 'cloud' || !ai.base_url || !ai.provider) return;
    if (ai.provider !== 'custom' && !ai.api_key) return;  // presets list after a key is entered
    const st = document.getElementById('ob-model-status');
    st.className = 'ob-status busy';
    st.textContent = 'Loading models…';
    let r;
    try {
        r = await api('/api/ai/models', { method: 'POST', body: JSON.stringify({ base_url: ai.base_url, api_key: ai.api_key || '' }) });
    } catch (e) { r = { ok: false, models: [], message: _obApiDetail(e) }; }
    _obModels = (r.models || []).filter(m => m !== OB_ON_DEVICE_MODEL);
    const failed = !r.ok || !_obModels.length;
    document.getElementById('ob-model-picker').style.display = failed ? 'none' : '';
    document.getElementById('ob-model-free').style.display = failed ? '' : 'none';
    document.getElementById('ob-model-error').textContent = failed
        ? (r.message || 'This server did not list any models.') + ' Type the model ID instead.' : '';
    st.className = 'ob-status';
    st.textContent = failed ? '' : _obModels.length + ' model' + (_obModels.length === 1 ? '' : 's');
    obRenderModelList();
}

function obRenderModelList() {
    const showAll = document.getElementById('ob-model-showall').checked;
    const shown = obFilterModels(_obModels, _obVal('ob-model-search'), showAll);
    const list = document.getElementById('ob-model-list');
    list.innerHTML = shown.map(m => '<option value="' + esc(m) + '">' + esc(m) + '</option>').join('');
    list.value = _obState.cloudWriting || '';  // never models[0]
    const chat = obFilterModels(_obModels, '', false);
    ['ob-cloud-scoring', 'ob-cloud-helpers'].forEach(id => {
        const sel = document.getElementById(id);
        const cur = sel.value;
        sel.innerHTML = '<option value="">Same as Writing model</option>'
            + chat.map(m => '<option value="' + esc(m) + '">' + esc(m) + '</option>').join('');
        sel.value = chat.includes(cur) ? cur : '';
    });
}

function obPickWriting(v) {
    _obState.cloudWriting = (v || '').trim();
    obClearExit();
    obReadForm();
}

// Advanced: the preset picker is a shortcut that fills the URL (still editable).
function obAdvProvider(name) {
    const p = _obProviders.find(x => x.name === name);
    if (p) document.getElementById('ob-ai-url').value = p.base_url;
    obReadForm();
}

// Advanced "Load models": lists with the typed values; saves nothing.
async function obTestAI() {
    const statusEl = document.getElementById('ob-adv-status');
    const base_url = obNormalizeUrl(_obVal('ob-ai-url'));
    statusEl.className = 'ob-status busy';
    statusEl.textContent = 'Loading…';
    let r;
    try {
        r = await api('/api/ai/models', { method: 'POST', body: JSON.stringify({ base_url, api_key: _obVal('ob-ai-api-key') }) });
    } catch (e) { r = { ok: false, models: [], message: _obApiDetail(e) }; }
    const models = (r.models || []).filter(m => m !== OB_ON_DEVICE_MODEL);
    _obAdvModels = models;
    statusEl.className = 'ob-status ' + (r.ok ? 'ok' : 'err');
    statusEl.textContent = r.ok ? models.length + ' model' + (models.length === 1 ? '' : 's') + ' available' : (r.message || 'Not connected');
    obPopulateModels(models);
}

// Advanced tier pickers: Apple Intelligence (when available) + the listed
// models. No auto-pick — a tier keeps its saved/current value or stays empty.
function obPopulateModels(models) {
    ['strong', 'fast', 'utility'].forEach(tier => {
        const sel = document.getElementById('ob-ai-model-' + tier);
        const preferred = sel.value || sel.dataset.preferred || '';
        const ids = [];
        if (_obLocalAvailable()) ids.push(OB_ON_DEVICE_MODEL);
        (models || []).forEach(m => { if (!ids.includes(m)) ids.push(m); });
        if (preferred && !ids.includes(preferred)) ids.push(preferred);  // never lose a saved tier
        sel.innerHTML = '<option value="">—</option>' + ids.map(m => '<option value="' + esc(m) + '">'
            + (m === OB_ON_DEVICE_MODEL ? 'Apple Intelligence (on-device)' : esc(m)) + '</option>').join('');
        sel.value = preferred;
    });
    obReadForm();
}

// Rebuild the step-0 snapshot from the form for the chosen mode.
function obCollectAI() {
    const mode = _obState.ai.mode;
    const ai = _obNewAI(mode);
    const S = OB_ON_DEVICE_MODEL;
    if (mode === 'local') {
        const dl = document.getElementById('ob-local-downloads').checked;
        ai.models = { strong: S, fast: S, utility: S };
        ai.scoring_tier = dl ? OB_QUICK_MATCH : 'strong';  // until Quick match is ready, the strong tier (Apple) scores
        ai.nli = dl ? true : null;
        ai.triage = dl;
    } else if (mode === 'cloud') {
        const w = _obState.cloudWriting || '';
        const scoring = _obVal('ob-cloud-scoring');
        ai.provider = _obVal('ob-cloud-provider') || null;
        ai.base_url = obNormalizeUrl(_obVal('ob-cloud-url'));
        ai.api_key = _obVal('ob-cloud-key');
        ai.models = { strong: w, fast: scoring || w, utility: _obVal('ob-cloud-helpers') || w };
        ai.triage = document.getElementById('ob-cloud-triage').checked;
        ai.scoring_tier = ai.triage ? OB_QUICK_MATCH : (scoring ? 'fast' : 'strong');
    } else if (mode === 'advanced') {
        const url = obNormalizeUrl(_obVal('ob-ai-url'));
        const preset = _obProviders.find(p => p.name === _obVal('ob-adv-provider'));
        ai.provider = preset && preset.base_url === url ? preset.name : 'custom';
        ai.base_url = url || null;
        ai.api_key = _obVal('ob-ai-api-key');
        ai.models = { strong: _obVal('ob-ai-model-strong'), fast: _obVal('ob-ai-model-fast'), utility: _obVal('ob-ai-model-utility') };
        ai.scoring_tier = _obVal('ob-adv-scoring') || 'strong';
        ai.triage = ai.scoring_tier === OB_QUICK_MATCH;
        ai.nli = document.getElementById('ob-adv-nli').checked;
    }
    return ai;
}

function obReadForm() { _obState.ai = obCollectAI(); }

function obClearExit() {
    const st = document.getElementById('ob-ai-status');
    st.className = 'ob-status';
    st.textContent = '';
    document.getElementById('ob-ai-details-btn').style.display = 'none';
    document.getElementById('ob-ai-details').style.display = 'none';
    document.getElementById('ob-ai-fail-actions').style.display = 'none';
}

function obExitError(message, detail, canContinue) {
    const st = document.getElementById('ob-ai-status');
    st.className = 'ob-status err';
    st.textContent = message;
    document.getElementById('ob-ai-details').textContent = detail || '';
    document.getElementById('ob-ai-details-btn').style.display = detail ? '' : 'none';
    document.getElementById('ob-ai-fail-actions').style.display = canContinue ? '' : 'none';
}

function obToggleDetails() {
    const d = document.getElementById('ob-ai-details');
    d.style.display = d.style.display === 'none' ? '' : 'none';
}

function obAIPayload(ai, verified) {
    return { mode: ai.mode, provider: ai.provider, base_url: ai.base_url, api_key: ai.api_key,
             models: ai.models, scoring_tier: ai.scoring_tier, nli: ai.nli, triage: ai.triage, verified };
}

// Shared exit: a real 1-token test of the Writing model, then save the AI
// section at once (so the Résumé step uses it). A re-run defers the write to
// the Review step; "Change setup…" saves and returns to Settings.
async function obAIContinue({ anyway = false } = {}) {
    obReadForm();
    const ai = _obState.ai;
    if (!ai.mode) { obExitError('Choose Local, Cloud or Advanced, or set up AI later.'); return; }
    if (!ai.models.strong) {
        obExitError(ai.mode === 'advanced' ? 'The Writing tier is empty. Pick a Writing model first.' : 'Pick a Writing model first.');
        return;
    }
    if (!anyway) {
        const st = document.getElementById('ob-ai-status');
        st.className = 'ob-status busy';
        st.textContent = 'Testing…';
        let r;
        try {
            r = await api('/api/ai/test-chat', { method: 'POST', body: JSON.stringify({
                base_url: ai.mode === 'local' ? '' : (ai.base_url || ''), api_key: ai.api_key || '', model: ai.models.strong }) });
        } catch (e) { r = { ok: false, message: _obApiDetail(e), detail: String(e.message || e) }; }
        if (!r.ok) { obExitError(r.message || 'The test failed', r.detail, true); return; }
    }
    _obState.aiLater = false;
    if (_obState.rerun && !_obState.only) {
        _obState.aiPending = { ...ai, verified: !anyway };
    } else {
        try {
            await api('/api/onboarding/ai', { method: 'POST', body: JSON.stringify(obAIPayload(ai, !anyway)) });
        } catch (e) { obExitError('Could not save: ' + _obApiDetail(e)); return; }
        if (ai.triage || ai.nli) obDownloadChip();
    }
    obClearExit();
    if (_obState.only) {
        obHide();
        toast('AI setup saved.', 'success');
        if (location.hash === '#settings' && typeof loadSettings === 'function') loadSettings();
        if (typeof checkAIStatus === 'function') checkAIStatus();
        return;
    }
    obGoto(1);
}

function obContinueAnyway() { obAIContinue({ anyway: true }); }

// Saves nothing AI-related.
function obSetupLater() {
    _obState.aiPending = null;
    _obState.aiLater = true;
    if (_obState.only) { obHide(); return; }
    obGoto(1);
}

// Header chip while the on-device models download (also shown in Settings → AI).
async function obDownloadChip() {
    const chip = document.getElementById('ob-dl-chip');
    if (!chip) return;
    let t, n;
    try { [t, n] = await Promise.all([api('/api/ai/triage/status'), api('/api/ai/nli/status')]); }
    catch (e) { chip.style.display = 'none'; return; }
    const active = [t, n].filter(s => s && s.state === 'downloading');
    if (!active.length) { chip.style.display = 'none'; return; }
    const pct = Math.round(100 * active.reduce((a, s) => a + (s.progress || 0), 0) / active.length);
    document.getElementById('ob-dl-chip-text').textContent = 'Downloading local models… ' + pct + '%';
    chip.style.display = '';
    setTimeout(obDownloadChip, 3000);
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

// ---- Sources step ----
// [config key, label] for the company watchlists, in Settings' order.
const OB_WATCHLIST_KEYS = [
    ['greenhouse_boards', 'Greenhouse boards'],
    ['lever_companies', 'Lever companies'],
    ['ashby_boards', 'Ashby boards'],
    ['workable_accounts', 'Workable accounts'],
    ['recruitee_companies', 'Recruitee companies'],
];

function obEnterSources() {
    obRenderReadySources();
    // Once per wizard run: coming Back to this step keeps the user's ticks.
    if (_obState.companies === null) obSuggestCompanies();
}

async function obRenderReadySources() {
    const el = document.getElementById('ob-ready-sources');
    if (!el) return;
    let names = ['linkedin', 'remoteok', 'weworkremotely', 'arbeitnow'];
    try {
        const r = await api('/api/sources');
        if (r.details) names = r.details.filter(d => d.kind === 'feed').map(d => d.name);
    } catch (e) { /* the default list above is accurate enough */ }
    el.innerHTML = names.map(n =>
        `<span class="ob-source-chip">${esc(SOURCE_LABELS[n] || n)}${n === 'linkedin' ? ' <span class="hint">(slow)</span>' : ''}</span>`).join('');
}

// Whether a suggestion call can reach an AI. "Set up later" has nothing to
// ask, so don't spend ~20s finding that out. A re-run's new AI choice isn't
// saved until the Review step, so the server still answers with the saved one.
function _obHasAI() {
    if (_obState.rerun) return !!(_obState.savedAI && _obState.savedAI.mode);
    return !_obState.aiLater && !!_obState.ai?.mode;
}

async function obSuggestCompanies({ more = false } = {}) {
    const box = document.getElementById('ob-companies');
    if (!box) return;
    if (!_obHasAI()) {
        _obState.companies = [];
        box.innerHTML = '<p class="ob-hint" style="margin:0">Connect an AI (step 1) and Jobsmith picks companies for you here. You can also find any company\'s board by name later in Settings &rarr; Job Search.</p>';
        return;
    }
    const prior = more ? (_obState.companies || []) : [];
    _obState.companies = prior;
    const status = document.createElement('p');
    status.className = 'ob-hint';
    status.id = 'ob-companies-status';
    status.textContent = 'Finding companies that fit your profile and checking their job boards… (about 20 seconds)';
    if (!more) box.innerHTML = '';
    else document.getElementById('ob-companies-more')?.remove();
    box.appendChild(status);
    const draft = obBuildPayload();
    // The suggester reads the profile only; keep credentials out of the request.
    const { workday_email, workday_password, ...profile } = draft.profile;
    let r;
    try {
        r = await api('/api/sources/suggest-companies', {
            method: 'POST',
            body: JSON.stringify({
                exclude: prior.map(c => c.name),
                profile,
                search: { keywords: draft.search.keywords, locations: draft.search.locations },
            }),
        });
    } catch (e) {
        r = { suggestions: [], ai_error: e.message || String(e) };
    }
    if (!_obState.open) return;
    status.remove();
    const fresh = (r.suggestions || []).map(c => ({ ...c, checked: true }));
    _obState.companies = prior.concat(fresh);
    obRenderCompanies(fresh.length ? '' : (r.ai_error
        ? 'Couldn\'t get suggestions right now (' + r.ai_error + '). Skip this and use Settings → Job Search later.'
        : 'No companies with a live job board this round. Try again, or skip and add companies later.'));
}

function obRenderCompanies(emptyNote = '') {
    const box = document.getElementById('ob-companies');
    const list = _obState.companies || [];
    const rows = list.map((c, i) => {
        const boards = (c.boards || []).map(b =>
            `${esc(SOURCE_LABELS[b.source] || b.source)} · ${b.jobs} open`).join(', ');
        return `<label class="ob-company-row">
            <input type="checkbox" data-company-idx="${i}"${c.checked ? ' checked' : ''}>
            <div><strong>${esc(c.name)}</strong>${c.why ? `<div class="ob-hint" style="margin:2px 0 0">${esc(c.why)}</div>` : ''}<div class="hint">${boards}</div></div>
        </label>`;
    }).join('');
    const note = emptyNote ? `<p class="ob-hint" style="margin:6px 0">${esc(emptyNote)}</p>` : '';
    box.innerHTML = rows + note +
        '<button type="button" class="btn btn-ghost btn-sm" id="ob-companies-more" onclick="obSuggestCompanies({ more: true })">' +
        (list.length ? 'Suggest more' : 'Try again') + '</button>';
    box.querySelectorAll('input[data-company-idx]').forEach(cb => {
        cb.addEventListener('change', () => obToggleCompany(parseInt(cb.dataset.companyIdx, 10), cb.checked));
    });
}

function obToggleCompany(i, checked) {
    const c = (_obState.companies || [])[i];
    if (c) c.checked = checked;
}

// search.* watchlist lists with the ticked suggestions merged into the saved
// ones. Only lists that gain a slug are returned, so an untouched step leaves
// the saved watchlists alone (and the re-run diff shows no row for them).
function obFollowedBoards() {
    const saved = _obState.savedSearch || {};
    const out = {};
    (_obState.companies || []).filter(c => c.checked).forEach(c => {
        (c.boards || []).forEach(b => {
            if (!OB_WATCHLIST_KEYS.some(([k]) => k === b.config_key)) return;
            if (!out[b.config_key]) {
                const base = b.config_key === 'greenhouse_boards'
                    ? (saved.greenhouse_boards || saved.greenhouse_companies || [])
                    : (saved[b.config_key] || []);
                out[b.config_key] = base.filter(x => x && x !== 'example-company');
            }
            if (!out[b.config_key].includes(b.slug)) out[b.config_key].push(b.slug);
        });
    });
    if (out.greenhouse_boards) out.greenhouse_companies = out.greenhouse_boards.slice();
    return out;
}

function obTestSourceKey(source) {
    const fields = source === 'adzuna'
        ? { adzuna_app_id: 'ob-adzuna-app-id', adzuna_app_key: 'ob-adzuna-app-key' }
        : { usajobs_email: 'ob-usajobs-email', usajobs_api_key: 'ob-usajobs-key' };
    return testSourceKey(source, fields, `ob-${source}-test-status`);
}

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
            ...obFollowedBoards(),
        },
        // AI is saved by step 0's shared exit; only a re-run carries it here,
        // for the Review step to diff.
        ...(_obState.aiPending ? { ai: obPendingAI(_obState.aiPending) } : {}),
        api_keys: {
            adzuna_app_id: document.getElementById('ob-adzuna-app-id').value.trim(),
            adzuna_app_key: document.getElementById('ob-adzuna-app-key').value.trim(),
            usajobs_email: document.getElementById('ob-usajobs-email').value.trim(),
            usajobs_api_key: document.getElementById('ob-usajobs-key').value.trim(),
        },
        salary_estimator: { bls: { api_key: document.getElementById('ob-bls-key').value.trim() } },
    };
}

function obPendingAI(p) {
    const ai = {
        scoring_tier: p.scoring_tier,
        models: {
            strong: { model: p.models.strong },
            fast: { model: p.models.fast },
            utility: { model: p.models.utility },
        },
    };
    if (p.provider != null) ai.provider = p.provider;
    if (p.base_url != null) ai.base_url = p.base_url;
    if (p.api_key != null) ai.api_key = p.api_key;
    if (p.nli != null) ai.nli_beta = { enabled: p.nli };
    return ai;
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
        const inSettings = location.hash === '#settings';
        if (inSettings) loadSettings();
        else location.hash = 'dashboard';
        window._onbStatus = null;  // A3: profile/pairing state just changed
    if (typeof checkAIStatus === 'function') checkAIStatus();  // A1
        // C3 — but not for a re-run launched from Settings: that user already has
        // jobs and shouldn't be yanked to the dashboard mid-edit.
        if (!inSettings) obFirstFetchNudge();
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
    // AI rows exist only when step 0 was completed in this re-run; an absent
    // value (e.g. Local leaves the server alone) is "no change".
    { label: 'AI provider', path: ['ai', 'provider'], eq: (cur, nxt) => nxt === undefined || (cur || '') === nxt },
    { label: 'AI server URL', path: ['ai', 'base_url'], eq: (cur, nxt) => nxt === undefined || (cur || '') === nxt },
    { label: 'AI API key', path: ['ai', 'api_key'], secret: true, eq: (cur, nxt) => nxt === undefined || (cur || '') === nxt },
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
    // Absent (no AI step in this re-run) is "no change", not a reset.
    { label: 'AI scoring tier', path: ['ai', 'scoring_tier'],
      eq: (cur, nxt) => !nxt || (cur || 'strong') === nxt },
    { label: 'Local match', path: ['ai', 'nli_beta', 'enabled'],
      eq: (cur, nxt) => nxt === undefined || !!cur === nxt },
    // Watchlist rows exist only when a suggested company was ticked on the
    // Sources step; absent is "no change". Greenhouse also writes the legacy
    // key so a stale list there can't shadow the new one (as Settings does).
    ...OB_WATCHLIST_KEYS.map(([key, label]) => ({
        label, path: ['search', key],
        also: key === 'greenhouse_boards' ? [['search', 'greenhouse_companies']] : undefined,
        eq: (cur, nxt) => nxt === undefined || JSON.stringify(cur || []) === JSON.stringify(nxt),
    })),
    { label: 'Adzuna App ID', path: ['api_keys', 'adzuna_app_id'] },
    { label: 'Adzuna App Key', path: ['api_keys', 'adzuna_app_key'], secret: true },
    { label: 'USAJobs email', path: ['api_keys', 'usajobs_email'] },
    { label: 'USAJobs API key', path: ['api_keys', 'usajobs_api_key'], secret: true },
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
    selected.forEach(r => {
        setIn(payload, r.field.path, r.nxt);
        (r.field.also || []).forEach(p => setIn(payload, p, r.nxt));
    });
    const nextBtn = document.getElementById('ob-next');
    nextBtn.disabled = true;
    nextBtn.textContent = 'Saving…';
    try {
        if (Object.keys(payload).length) {
            await api('/api/config', { method: 'POST', body: JSON.stringify(payload) });
        }
        // The AI values went through /api/config above; this records the mode
        // (per device) and starts any downloads the applied rows asked for.
        const p = _obState.aiPending;
        const applied = label => selected.some(r => r.field.label === label);
        if (p && selected.some(r => r.field.path[0] === 'ai')) {
            await api('/api/onboarding/ai', { method: 'POST', body: JSON.stringify({
                mode: p.mode, verified: p.verified,
                triage: !!p.triage && applied('AI scoring tier'),
                nli: applied('Local match') ? p.nli : null,
            }) });
            if ((p.triage && applied('AI scoring tier')) || (p.nli && applied('Local match'))) obDownloadChip();
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
        title: 'Your Activity view',
        body: 'This is your command center. These stat cards summarize jobs ingested, pending review, submitted applications, and your overall fit. Click any of them to jump to filtered views.',
    },
    {
        hash: '#dashboard',
        selector: '.run-console',
        title: 'Run console',
        body: 'Fetch & Score is the one-button routine: it pulls new jobs, then scores them the moment the fetch finishes. The individual verbs are all still here — Fetch and Score (each caret opens its options), Tailor, and More for Estimate Salaries, Detect Easy Apply, Add Job by URL and refetching descriptions. The live log below tracks every run, and the chip in the header opens the same view from any tab.',
    },
    {
        hash: '#jobs',
        selector: '.filter-bar',
        title: 'Your Inbox',
        body: 'Newly fetched jobs land here to scout. Shortlist the ones worth pursuing (→ or S) and pass on the rest (← or P) — keyboard or buttons. Filter by keyword, location, salary, score, and more; the advanced toggle exposes source, status, and date range.',
    },
    {
        hash: '#jobs',
        selector: '.jobs-toolbar',
        title: 'Two ways to work the Inbox',
        body: 'Card view (the default) deals you one job at a time to triage fast — shortlist, pass, or open. "List view" here (or the L key) switches to the list + detail pane, where you spend most of your time: pick a job on the left → click "Tailor Resume" → wait for it to generate → click "Apply Assist" to open the posting with the extension sidebar → submit on the live ATS → hit "Mark Applied" when done. Same jobs, same actions — pick whichever fits the moment.',
    },
    {
        hash: '#review',
        selector: '#pipeline-funnel',
        title: 'Your Pipeline',
        body: 'Everything you shortlisted flows through here by stage: Shortlisted → Ready to Review → Applied, with Failed and In Progress for auditing. This funnel is the one stage summary in both views: click a segment to filter the board to that stage (click it again, or the ✕ chip, to clear) — or, in Table view, to open that stage’s table. The board (default) lets you drag a job between stages; "Table view" (or the L key) gives you the classic stage tabs and lists. Tailor a shortlisted job to generate its resume and cover letter, use AI Edit to revise, then launch Apply Assist from here or from the Inbox.',
    },
    {
        hash: '#settings',
        selector: '.settings-tabs',
        title: 'Settings, tab by tab',
        body: 'Settings opens in Basic mode — five tabs cover everything you need day-to-day: Profile, Job Search, AI, Apply, and App. The Save Settings button at the top right saves every tab at once. The next stops walk through them so nothing feels like a mystery toggle.',
    },
    {
        hash: '#settings',
        selector: '#stab-search',
        title: 'Job Search',
        body: 'Controls what jobs Jobsmith ingests: keywords, locations, min salary, and exclusion terms — plus company watchlists that follow specific Greenhouse, Lever, Ashby, Workable, and Recruitee boards. Your job sources live here too: LinkedIn and Indeed sign-in for authenticated scraping, and Adzuna / USAJobs API keys for extra feeds. Tighten these if your feed is too noisy; loosen them if you\'re not seeing enough.',
        before: () => _tourSwitchSettingsTab('stab-search'),
    },
    {
        hash: '#settings',
        selector: '#stab-integrations',
        title: 'AI',
        body: 'Everything the AI does: how Jobsmith thinks (Change setup… re-runs the Local / Cloud / Advanced choice), your AI server and the three model picks — Content, Navigator, Utility — plus how honestly it tailors, the resume visual style, and DOCX/PDF output. Advanced mode adds the scoring tier, context window, max experience entries, the AI Edit model, and the full prompt editor.',
        before: () => _tourSwitchSettingsTab('stab-integrations'),
    },
    {
        hash: '#settings',
        selector: '#card-honesty',
        title: 'Honesty levels',
        body: 'Pick how much latitude the AI takes when tailoring: honest (only restate facts), tailored (rephrase for emphasis), embellished (stretch a bit), or fabricated (invent — generally avoid). You can override this per generation. Just below it, the style studio sets how your resume looks.',
        before: () => _tourSwitchSettingsTab('stab-integrations'),
    },
    {
        hash: '#settings',
        selector: '#stab-assist',
        title: 'Apply',
        body: 'This is the main way you apply — and it runs inside your normal Chrome or Firefox via our browser extension (no separate browser is launched). On Firefox, click "Get for Firefox" for a permanent, Mozilla-signed add-on; on Chrome, "Get for Chrome" saves an unpacked folder you load from chrome://extensions. Pairing is automatic; the token below is only a fallback. After that, clicking "Apply Assist" on any job injects a sidebar with your tailored materials and autofills standard fields right on the live ATS page — and you can click any field in the sidebar to copy its value. Your answer bank and ATS/Workday credentials live on this tab too.',
        before: () => _tourSwitchSettingsTab('stab-assist'),
    },
    {
        hash: '#settings',
        selector: '.settings-mode-toggle',
        title: 'Basic vs. Advanced',
        body: 'This toggle controls how much of Settings you see. Basic keeps each tab to the essentials; Advanced (shown now) reveals every other card — prompts, logs, credentials, network, and the deeper knobs. Anything you set in Advanced stays in effect when you switch back.',
        before: () => setSettingsMode('advanced'),
    },
    {
        hash: '#settings',
        selector: '#card-answerbank',
        title: 'Answer Bank',
        body: 'Every time you answer a custom application question (work auth, sponsorship, why this company, etc.), it gets stored here so the next form gets pre-filled automatically. Edit or delete entries any time if your answers change.',
        before: () => { setSettingsMode('advanced'); _tourSwitchSettingsTab('stab-assist'); loadAnswerBank(); },
    },
    {
        hash: '#settings',
        selector: '#card-prompts',
        title: 'Edit the AI\'s prompts',
        body: 'Every prompt Jobsmith sends to your AI — scoring, resume tailoring, cover letters, parsing — is editable at the bottom of the AI tab. Placeholders like {profile_summary} are filled in automatically at run time. Customized prompts are saved to your config; Reset to Default brings any of them back.',
        before: () => { setSettingsMode('advanced'); _tourSwitchSettingsTab('stab-integrations'); loadPrompts(); },
    },
    {
        hash: '#settings',
        selector: '#settings-replay-tour',
        title: 'Replay anytime',
        body: 'Done! The App tab holds folder sync with the iPhone app, live updates, logs, and these two buttons — you can re-run this tour or the setup wizard anytime from Settings → App. Happy applying.',
        before: () => _tourSwitchSettingsTab('stab-sync'),
    },
];

function _tourSwitchSettingsTab(panelId) {
    const btn = document.querySelector(`.settings-tab[onclick*="${panelId}"]`);
    if (btn) switchSettingsTab(btn, panelId);
}

let _tourState = { step: 0, open: false, target: null, rafId: 0, prevSettingsMode: null };

async function tourStart() {
    if (_tourState.open) return;
    _tourState.open = true;
    _tourState.step = 0;
    // The Advanced-mode stops flip the settings mode; restore it on close.
    _tourState.prevSettingsMode = typeof getSettingsMode === 'function' ? getSettingsMode() : null;
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
    toast('Tour skipped — replay it anytime from Settings → App.', 'info');
}

async function tourFinish() {
    await _tourClose(true);
    toast('You’re ready to go!', 'success');
}

async function _tourClose(markComplete) {
    _tourState.open = false;
    if (_tourState.prevSettingsMode && typeof setSettingsMode === 'function') {
        setSettingsMode(_tourState.prevSettingsMode);
        _tourState.prevSettingsMode = null;
    }
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
        // C1 — keep the cached status honest so the post-fetch hook in
        // dashboard.js can't relaunch a tour the user just finished/skipped.
        if (window._onbStatus) window._onbStatus.tour_complete = true;
    }
}

async function tourReplay() {
    try { await api('/api/onboarding/tour-reset', { method: 'POST', body: '{}' }); } catch (e) {}
    tourStart();
}
