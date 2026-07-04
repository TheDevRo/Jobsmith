// Jobsmith frontend — split from app.js. Classic scripts loaded in
// order by index.html; all files share the global scope (inline onclick
// handlers in index.html and generated HTML rely on these names).

// ---- Settings ----
async function loadSettings() {
    try {
        const cfg = await api('/api/config');
        document.getElementById('cfg-name').value = cfg.profile?.full_name || '';
        document.getElementById('cfg-middle-name').value = cfg.profile?.middle_name || '';
        document.getElementById('cfg-email').value = cfg.profile?.email || '';
        document.getElementById('cfg-phone').value = cfg.profile?.phone || '';
        document.getElementById('cfg-location').value = cfg.profile?.location || '';
        document.getElementById('cfg-street-address').value = cfg.profile?.street_address || '';
        document.getElementById('cfg-street-address-2').value = cfg.profile?.street_address_2 || '';
        document.getElementById('cfg-city').value = cfg.profile?.city || '';
        document.getElementById('cfg-state').value = cfg.profile?.state || '';
        document.getElementById('cfg-zip').value = cfg.profile?.zip_code || '';
        document.getElementById('cfg-desired-salary').value = cfg.profile?.desired_salary || '';
        document.getElementById('cfg-notice-period').value = cfg.profile?.notice_period || '2 weeks';
        document.getElementById('cfg-available-start').value = cfg.profile?.available_start || 'Immediately';
        document.getElementById('cfg-linkedin').value = cfg.profile?.linkedin || '';
        document.getElementById('cfg-summary').value = cfg.profile?.summary || '';
        document.getElementById('cfg-skills').value = (cfg.profile?.skills || []).join(', ');

        renderExperience(cfg.profile?.experience || []);
        renderEducation(cfg.profile?.education || []);
        document.getElementById('cfg-certifications').value = (cfg.profile?.certifications || []).join('\n');
        renderReferences(cfg.profile?.references || []);

        document.getElementById('cfg-gender').value = cfg.profile?.gender || '';
        document.getElementById('cfg-race').value = cfg.profile?.race_ethnicity || '';
        document.getElementById('cfg-veteran').value = cfg.profile?.veteran_status || '';
        document.getElementById('cfg-disability').value = cfg.profile?.disability_status || '';
        document.getElementById('cfg-work-auth').value = cfg.profile?.work_authorization || '';
        document.getElementById('cfg-sponsorship').value = cfg.profile?.sponsorship_required || '';

        document.getElementById('cfg-keywords').value = (cfg.search?.keywords || []).join(', ');
        document.getElementById('cfg-locations').value = (cfg.search?.locations || []).join('\n');
        document.getElementById('cfg-exclude').value = (cfg.search?.exclude_keywords || []).join(', ');
        document.getElementById('cfg-salary').value = cfg.search?.min_salary || 0;
        // greenhouse_boards is the canonical key the fetcher prefers;
        // greenhouse_companies is the legacy alias.
        const ghBoards = cfg.search?.greenhouse_boards?.length
            ? cfg.search.greenhouse_boards : (cfg.search?.greenhouse_companies || []);
        document.getElementById('cfg-greenhouse').value = ghBoards.filter(s => s !== 'example-company').join(', ');
        document.getElementById('cfg-lever').value = (cfg.search?.lever_companies || []).filter(s => s !== 'example-company').join(', ');
        document.getElementById('cfg-ashby').value = (cfg.search?.ashby_boards || []).filter(s => s !== 'example-company').join(', ');
        document.getElementById('cfg-workable').value = (cfg.search?.workable_accounts || []).filter(s => s !== 'example-company').join(', ');
        document.getElementById('cfg-recruitee').value = (cfg.search?.recruitee_companies || []).filter(s => s !== 'example-company').join(', ');

        const aaEnabled = cfg.auto_apply?.enabled || false;
        document.getElementById('cfg-auto-apply').checked = aaEnabled;
        applyAutoApplyVisibility(aaEnabled);
        const aaToggle = document.getElementById('cfg-auto-apply');
        if (!aaToggle.dataset.bound) {
            aaToggle.dataset.bound = '1';
            aaToggle.addEventListener('change', (e) => {
                applyAutoApplyVisibility(e.target.checked);
                if (currentReviewView === 'pending') loadReviewQueue();
                else if (currentReviewView === 'failed') loadFailedApplications();
            });
        }
        document.getElementById('cfg-auto-approve').checked = cfg.auto_apply?.auto_approve || false;
        document.getElementById('cfg-headless').checked = cfg.auto_apply?.headless !== false;
        document.getElementById('cfg-browser-use').checked = cfg.auto_apply?.use_browser_use || false;
        document.getElementById('cfg-max-daily').value = cfg.auto_apply?.max_daily_applications || 20;
        document.getElementById('cfg-step-ceiling').value = cfg.auto_apply?.step_ceiling ?? 60;
        document.getElementById('cfg-disable-stuck-detection').checked = cfg.auto_apply?.disable_stuck_detection || false;
        document.getElementById('cfg-per-domain-rate-limit').value = cfg.auto_apply?.per_domain_rate_limit ?? 0;
        document.getElementById('cfg-review-unknown-ats').checked = cfg.auto_apply?.review_required_rules?.unknown_ats || false;
        document.getElementById('cfg-min-confidence').value = cfg.auto_apply?.review_required_rules?.min_confidence ?? 0.60;

        document.getElementById('cfg-ai-url').value = cfg.ai?.base_url || '';
        // Context window — snap to nearest option, defaulting to 8192
        const savedCtx = cfg.ai?.context_window || 8192;
        const ctxSel = document.getElementById('cfg-context-window');
        const ctxOptions = [...ctxSel.options].map(o => parseInt(o.value));
        const nearest = ctxOptions.reduce((a, b) => Math.abs(b - savedCtx) < Math.abs(a - savedCtx) ? b : a);
        ctxSel.value = nearest;
        // Populate model dropdowns — load available models then restore saved selections
        const savedFast = cfg.ai?.models?.fast?.model || cfg.ai?.model || '';
        const savedStrong = cfg.ai?.models?.strong?.model || cfg.ai?.model || '';
        const savedUtility = cfg.ai?.models?.utility?.model || '';
        await loadAiModels({ preselect: { fast: savedFast, strong: savedStrong, utility: savedUtility } });
        const scoringTierSel = document.getElementById('cfg-scoring-tier');
        if (scoringTierSel) scoringTierSel.value = cfg.ai?.scoring_tier || 'strong';

        document.getElementById('cfg-adzuna-app-id').value = cfg.api_keys?.adzuna_app_id || '';
        document.getElementById('cfg-adzuna-app-key').value = cfg.api_keys?.adzuna_app_key || '';
        document.getElementById('cfg-usajobs-email').value = cfg.api_keys?.usajobs_email || '';
        document.getElementById('cfg-usajobs-key').value = cfg.api_keys?.usajobs_api_key || '';
        const blsKeyEl = document.getElementById('cfg-bls-api-key');
        if (blsKeyEl) blsKeyEl.value = cfg.salary_estimator?.bls?.api_key || '';


        document.getElementById('cfg-ats-login-password').value = cfg.profile?.ats_login_password || '';
        document.getElementById('cfg-workday-email').value = cfg.profile?.workday_email || '';
        document.getElementById('cfg-workday-password').value = cfg.profile?.workday_password || '';

        document.getElementById('cfg-flaresolverr-url').value = cfg.flaresolverr?.url || '';
    } catch (e) {
        toast('Failed to load settings', 'error');
    }

    // Load honesty level separately (own endpoint, not part of /api/config)
    try {
        const hl = await api('/api/settings/honesty-level');
        _applyHonestyLevel(hl.honesty_level || 'honest');
    } catch (e) { /* non-fatal — leave segmented control in default state */ }

    try {
        const rs = await api('/api/settings/resume-style');
        _applyResumeStyle(rs.resume_style || 'standard');
    } catch (e) { /* non-fatal */ }

    try {
        const df = await api('/api/settings/document-format');
        _applyDocumentFormat(df.document_format || 'docx');
    } catch (e) { /* non-fatal */ }

    try {
        const mt = await api('/api/settings/ai-edit-model-tier');
        _aiEditDefaultTier = mt.model_tier || 'strong';
        _applyAiEditModelTier(_aiEditDefaultTier);
    } catch (e) { /* non-fatal */ }

    try {
        const me = await api('/api/settings/max-resume-experience-entries');
        _applyMaxExpEntries(me.max_resume_experience_entries);
    } catch (e) { /* non-fatal */ }

    try {
        const sa = await api('/api/settings/salary-estimator-auto-ingest');
        _applySalaryAutoIngest(!!sa.auto_on_ingest);
    } catch (e) { /* non-fatal */ }

    checkLinkedInSession();
    checkIndeedSession();
    loadAIStatus();
    loadExtensionToken();
}

async function loadExtensionToken() {
    try {
        const r = await api('/api/extension/token');
        document.getElementById('ext-token').value = r.token || '';
        document.getElementById('ext-token-status').textContent =
            r.token ? 'Paste into the extension popup, then click Save.' : 'No token yet — restart the server.';
    } catch (e) {
        document.getElementById('ext-token-status').textContent = `Failed to load token: ${e.message}`;
    }
}

async function copyExtensionToken() {
    const el = document.getElementById('ext-token');
    const status = document.getElementById('ext-token-status');
    try {
        await navigator.clipboard.writeText(el.value);
        status.textContent = 'Copied to clipboard.';
    } catch {
        el.select(); document.execCommand('copy');
        status.textContent = 'Copied to clipboard.';
    }
}

async function saveExtension(browser) {
    const status = document.getElementById('ext-download-status');
    status.textContent = 'Saving…';
    try {
        const r = await api(`/api/extension/save/${browser}`, { method: 'POST' });
        const where = r.revealed ? `${r.saved_to} (revealed in your file manager)` : r.saved_to;
        if (r.kind === 'xpi') {
            status.textContent = `Signed add-on saved to ${where}. In Firefox: about:addons → ⚙ → "Install Add-on From File…" → pick it.`;
        } else if (browser === 'firefox') {
            status.textContent = `No Mozilla-signed .xpi built yet, so the unpacked extension was saved to ${where}. Load it via about:debugging → This Firefox → "Load Temporary Add-on…" → pick its manifest.json. Firefox removes it on restart — see extension/README.md to sign a permanent .xpi.`;
        } else {
            status.textContent = `Unpacked extension saved to ${where}. In Chrome: chrome://extensions → enable Developer mode → "Load unpacked" → pick that folder.`;
        }
    } catch (e) {
        // Remote (non-loopback) browsers can't use the save-to-disk path,
        // but they can download the zip the normal way.
        if (e.message && e.message.includes('Only served to localhost')) {
            window.location.href = `/api/extension/download/${browser}`;
            status.textContent = 'Downloading zip…';
            return;
        }
        let detail = e.message;
        try { detail = JSON.parse(e.message).detail || detail; } catch {}
        status.textContent = `Save failed: ${detail}`;
    }
}

function toggleExtensionInstall() {
    const panel = document.getElementById('ext-install-instructions');
    const btn = document.getElementById('ext-install-toggle');
    if (!panel || !btn) return;
    const shown = panel.style.display !== 'none';
    panel.style.display = shown ? 'none' : 'block';
    btn.textContent = shown ? 'Show install instructions ▾' : 'Hide install instructions ▴';
}

async function rotateExtensionToken() {
    if (!(await appConfirm('Rotate the extension token? The current token will stop working — you\'ll need to paste the new one into the extension popup.'))) return;
    const status = document.getElementById('ext-token-status');
    try {
        const r = await api('/api/extension/token/rotate', { method: 'POST' });
        document.getElementById('ext-token').value = r.token || '';
        status.textContent = 'Rotated. Paste the new token into the extension popup.';
    } catch (e) {
        status.textContent = `Rotate failed: ${e.message}`;
    }
}

// ---- Company board finder ----

const _BOARD_FIELD_BY_KEY = {
    greenhouse_boards: 'cfg-greenhouse',
    lever_companies: 'cfg-lever',
    ashby_boards: 'cfg-ashby',
    workable_accounts: 'cfg-workable',
    recruitee_companies: 'cfg-recruitee',
};

async function findCompanyBoards() {
    const input = document.getElementById('board-finder-input');
    const btn = document.getElementById('board-finder-btn');
    const results = document.getElementById('board-finder-results');
    const company = input.value.trim();
    if (!company) return;
    btn.disabled = true;
    results.textContent = 'Checking Greenhouse, Lever, Ashby, Workable, and Recruitee…';
    try {
        const r = await api('/api/sources/detect-boards', {
            method: 'POST',
            body: JSON.stringify({ company }),
        });
        if (!r.matches.length) {
            results.textContent = `No live boards found for "${company}" (tried slugs: ${r.tried_slugs.join(', ')}). The company may use a different ATS, or its slug doesn't match its name — check its careers page URL.`;
            return;
        }
        results.innerHTML = r.matches.map(m => `
            <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:0.9rem">
                <span style="flex:1">${SOURCE_LABELS[m.source] || m.source}: <a href="${m.board_url}" target="_blank" rel="noopener"><code>${m.slug}</code></a>${m.company_name ? ` ("${m.company_name}")` : ''} — ${m.jobs} open job${m.jobs === 1 ? '' : 's'}</span>
                <button class="btn btn-secondary btn-sm" onclick="addBoardSlug('${m.config_key}', '${m.slug}', this)">Add</button>
            </div>
        `).join('');
    } catch (e) {
        results.textContent = `Lookup failed: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

function addBoardSlug(configKey, slug, btn) {
    const field = document.getElementById(_BOARD_FIELD_BY_KEY[configKey]);
    if (!field) return;
    const existing = field.value.split(',').map(s => s.trim()).filter(Boolean);
    if (!existing.includes(slug)) {
        existing.push(slug);
        field.value = existing.join(', ');
    }
    btn.textContent = 'Added ✓';
    btn.disabled = true;
    toast('Added to watchlist — click Save Settings to apply', 'info');
}

// ---- AI company recommender ----

const _suggestedCompanyNames = [];

async function suggestCompanies() {
    const btn = document.getElementById('suggest-companies-btn');
    const results = document.getElementById('suggest-companies-results');
    btn.disabled = true;
    results.textContent = 'Mining your feed + asking the AI, then verifying live boards… (can take ~20s)';
    try {
        const r = await api('/api/sources/suggest-companies', {
            method: 'POST',
            body: JSON.stringify({ exclude: _suggestedCompanyNames }),
        });
        r.suggestions.forEach(s => _suggestedCompanyNames.push(s.name));
        if (!r.suggestions.length) {
            results.textContent = r.ai_error
                ? `No verified suggestions this round (AI unavailable: ${r.ai_error}). Feed-mined candidates had no live boards.`
                : 'No new suggestions with live boards this round — try again after fetching more jobs, or broaden your keywords.';
            btn.textContent = 'Suggest more';
            return;
        }
        const rows = r.suggestions.map(s => {
            const origin = s.origin === 'history'
                ? '<span style="font-size:0.75rem;padding:1px 6px;border-radius:8px;background:var(--bg-primary);border:1px solid var(--border)">from your feed</span>'
                : '<span style="font-size:0.75rem;padding:1px 6px;border-radius:8px;background:var(--bg-primary);border:1px solid var(--border)">AI pick</span>';
            const boards = s.boards.map(b => `
                <div style="display:flex;align-items:center;gap:8px;padding:2px 0 2px 14px;font-size:0.85rem">
                    <span style="flex:1">${SOURCE_LABELS[b.source] || b.source}: <a href="${b.board_url}" target="_blank" rel="noopener"><code>${b.slug}</code></a>${b.company_name ? ` ("${b.company_name}")` : ''} — ${b.jobs} open job${b.jobs === 1 ? '' : 's'}</span>
                    <button class="btn btn-secondary btn-sm" onclick="addBoardSlug('${b.config_key}', '${b.slug}', this)">Add</button>
                </div>`).join('');
            return `
                <div style="padding:6px 0;border-bottom:1px solid var(--border)">
                    <div style="display:flex;align-items:center;gap:8px;font-size:0.9rem">
                        <strong>${s.name}</strong> ${origin}
                    </div>
                    ${s.why ? `<div style="font-size:0.85rem;color:var(--text-secondary);margin:2px 0">${s.why}</div>` : ''}
                    ${boards}
                </div>`;
        }).join('');
        const aiNote = r.ai_error ? `<div class="hint" style="margin-top:6px">AI was unavailable (${r.ai_error}) — showing feed-mined suggestions only.</div>` : '';
        results.innerHTML = rows + aiNote;
        btn.textContent = 'Suggest more';
    } catch (e) {
        results.textContent = `Suggestion failed: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

// ---- Logs ----

let logsAutoRefreshTimer = null;

async function loadLogs() {
    const out = document.getElementById('logs-output');
    const status = document.getElementById('logs-status');
    const lines = document.getElementById('logs-lines')?.value || 500;
    try {
        const r = await api(`/api/logs/tail?lines=${lines}`);
        // Keep the user's scroll position unless they're already at the
        // bottom (or this is the first load) — then follow the tail.
        const firstLoad = !out.dataset.loaded;
        const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
        out.textContent = r.lines.length ? r.lines.join('\n') : '(log file is empty)';
        out.dataset.loaded = '1';
        if (firstLoad || atBottom) out.scrollTop = out.scrollHeight;
        status.textContent = `${r.path} — ${(r.size / 1024).toFixed(0)} KB`;
    } catch (e) {
        status.textContent = `Failed to load logs: ${e.message}`;
    }
}

function toggleLogsAutoRefresh() {
    const on = document.getElementById('logs-autorefresh').checked;
    if (on && !logsAutoRefreshTimer) {
        logsAutoRefreshTimer = setInterval(() => {
            // Skip fetches while the Logs panel isn't visible
            const panel = document.getElementById('stab-logs');
            if (panel && panel.offsetParent !== null) loadLogs();
        }, 3000);
    } else if (!on && logsAutoRefreshTimer) {
        clearInterval(logsAutoRefreshTimer);
        logsAutoRefreshTimer = null;
    }
}

async function copyLogs() {
    const out = document.getElementById('logs-output');
    const status = document.getElementById('logs-status');
    try {
        await navigator.clipboard.writeText(out.textContent);
        status.textContent = 'Copied to clipboard.';
    } catch {
        status.textContent = 'Copy failed — select the text manually.';
    }
}

async function revealLogFile() {
    const status = document.getElementById('logs-status');
    try {
        await api('/api/logs/reveal', { method: 'POST' });
    } catch (e) {
        status.textContent = `Reveal failed: ${e.message}`;
    }
}

async function loadAIStatus() {
    // Piggyback on the existing testAI() which uses #ai-status element
    // Just call testAI() silently when loading settings
    await testAI().catch(() => {});
}

// ---- Answer Bank ----

const _AB_KEY_LABELS = {
    tell_us_about_yourself: 'Tell us about yourself',
    why_this_role: 'Why this role?',
    challenging_project: 'Challenging project / STAR story',
    greatest_strength: 'Greatest strength',
    greatest_weakness: 'Greatest weakness',
    career_goal: 'Career goal (5-year plan)',
    salary_expectation: 'Salary expectation',
    cover_letter: 'Cover letter body',
};

async function loadAnswerBank() {
    try {
        const data = await api('/api/answer-bank');
        renderAnswerBankList(data.snippets || {});
        renderCustomAnswerList(data.custom || []);
    } catch (e) {
        toast('Failed to load answer bank', 'error');
    }
}

function renderAnswerBankList(snippets) {
    const container = document.getElementById('answer-bank-list');
    if (!container) return;

    const keys = Object.keys(_AB_KEY_LABELS);
    container.innerHTML = keys.map(key => {
        const label = _AB_KEY_LABELS[key];
        const value = snippets[key] || '';
        const isPlaceholder = value.startsWith('<') && value.endsWith('>');
        return `
        <div class="ab-entry" style="margin-bottom:16px;border:1px solid var(--border);border-radius:8px;padding:12px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <strong style="font-size:0.9rem">${escapeHtml(label)}</strong>
                <span style="font-size:0.75rem;color:var(--text-secondary);font-family:monospace">${key}</span>
            </div>
            <textarea id="ab-value-${key}" rows="3" style="width:100%;box-sizing:border-box;${isPlaceholder ? 'color:var(--text-secondary);font-style:italic' : ''}">${escapeHtml(value)}</textarea>
            <div style="display:flex;gap:8px;margin-top:6px">
                <button class="btn btn-primary btn-sm" onclick="saveAnswerBankEntry('${key}')">Save</button>
            </div>
        </div>`;
    }).join('');
}

function renderCustomAnswerList(custom) {
    const container = document.getElementById('custom-answer-list');
    if (!container) return;

    if (!custom || custom.length === 0) {
        container.innerHTML = '<p style="color:var(--text-secondary);font-size:0.85rem">No custom answers yet.</p>';
        return;
    }

    container.innerHTML = custom.map(entry => `
        <div class="ab-custom-entry" style="margin-bottom:16px;border:1px solid var(--border);border-radius:8px;padding:12px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
                <strong style="font-size:0.9rem">${escapeHtml(entry.label || entry.key)}</strong>
                <button class="btn btn-danger btn-sm" onclick="deleteCustomAnswer('${escapeHtml(entry.key)}')">Delete</button>
            </div>
            <div style="margin-bottom:6px;font-size:0.8rem;color:var(--text-secondary)">
                Keywords: <span style="font-family:monospace">${escapeHtml((entry.keywords || []).join(', '))}</span>
            </div>
            <textarea id="ab-custom-value-${escapeHtml(entry.key)}" rows="3" style="width:100%;box-sizing:border-box">${escapeHtml(entry.value || '')}</textarea>
            <div style="display:flex;gap:8px;margin-top:6px">
                <button class="btn btn-primary btn-sm" onclick="saveCustomAnswerEntry('${escapeHtml(entry.key)}', '${escapeHtml(entry.label || entry.key)}', ${JSON.stringify(entry.keywords || [])})">Save</button>
            </div>
        </div>`).join('');
}

async function saveAnswerBankEntry(key) {
    const el = document.getElementById(`ab-value-${key}`);
    if (!el) return;
    try {
        await api('/api/answer-bank', { method: 'POST', body: JSON.stringify({ key, value: el.value }) });
        toast(`Saved: ${_AB_KEY_LABELS[key] || key}`, 'success');
    } catch (e) {
        toast('Failed to save answer', 'error');
    }
}

async function saveCustomAnswerEntry(key, label, keywords) {
    const el = document.getElementById(`ab-custom-value-${key}`);
    if (!el) return;
    try {
        await api('/api/answer-bank/custom', {
            method: 'POST',
            body: JSON.stringify({ key, label, keywords, value: el.value }),
        });
        toast(`Saved: ${label}`, 'success');
    } catch (e) {
        toast('Failed to save custom answer', 'error');
    }
}

async function deleteCustomAnswer(key) {
    if (!(await appConfirm(`Delete custom answer "${key}"?`))) return;
    try {
        await api(`/api/answer-bank/custom/${encodeURIComponent(key)}`, { method: 'DELETE' });
        toast('Deleted', 'success');
        loadAnswerBank();
    } catch (e) {
        toast('Failed to delete', 'error');
    }
}

async function addCustomAnswer() {
    const label = await appPrompt('Label for this answer (e.g. "Remote work preference"):');
    if (!label) return;
    const keywordsRaw = await appPrompt('Trigger keywords (comma-separated, e.g. "remote, work from home, hybrid"):');
    if (!keywordsRaw) return;
    const key = 'custom_' + label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    const keywords = keywordsRaw.split(',').map(s => s.trim()).filter(Boolean);

    api('/api/answer-bank/custom', {
        method: 'POST',
        body: JSON.stringify({ key, label, keywords, value: '' }),
    }).then(() => {
        toast('Custom answer added — fill in the value and save', 'success');
        loadAnswerBank();
    }).catch(() => toast('Failed to add custom answer', 'error'));
}

async function testAnswerBankMatch() {
    const input = document.getElementById('ab-test-input');
    const resultEl = document.getElementById('ab-test-result');
    if (!input || !resultEl || !input.value.trim()) return;

    try {
        const result = await api('/api/answer-bank/test-match', {
            method: 'POST',
            body: JSON.stringify({ question: input.value.trim() }),
        });
        if (result.matched_key) {
            const label = _AB_KEY_LABELS[result.matched_key] || result.matched_key;
            const preview = result.value ? result.value.substring(0, 80) + (result.value.length > 80 ? '…' : '') : '(placeholder — not yet filled in)';
            resultEl.innerHTML = `<span style="color:var(--green)">Match: <strong>${escapeHtml(label)}</strong> (score: ${result.score})</span><br><span style="color:var(--text-secondary)">${escapeHtml(preview)}</span>`;
        } else {
            resultEl.innerHTML = `<span style="color:var(--yellow)">No match found (best score: ${result.score}, threshold: 60). This question would go to the AI.</span>`;
        }
    } catch (e) {
        resultEl.textContent = 'Test failed';
    }
}

async function saveSettings() {
    const splitTrim = (s) => s.split(',').map(v => v.trim()).filter(Boolean);

    const body = {
        profile: {
            full_name: document.getElementById('cfg-name').value.trim(),
            email: document.getElementById('cfg-email').value.trim(),
            phone: document.getElementById('cfg-phone').value.trim(),
            location: document.getElementById('cfg-location').value.trim(),
            linkedin: document.getElementById('cfg-linkedin').value.trim(),
            summary: document.getElementById('cfg-summary').value.trim(),
            skills: splitCsvSmart(document.getElementById('cfg-skills').value),
            middle_name: document.getElementById('cfg-middle-name').value,
            street_address: document.getElementById('cfg-street-address').value,
            street_address_2: document.getElementById('cfg-street-address-2').value,
            city: document.getElementById('cfg-city').value,
            state: document.getElementById('cfg-state').value,
            zip_code: document.getElementById('cfg-zip').value,
            desired_salary: document.getElementById('cfg-desired-salary').value,
            notice_period: document.getElementById('cfg-notice-period').value || '2 weeks',
            available_start: document.getElementById('cfg-available-start').value || 'Immediately',
            gender: document.getElementById('cfg-gender').value,
            race_ethnicity: document.getElementById('cfg-race').value,
            veteran_status: document.getElementById('cfg-veteran').value,
            disability_status: document.getElementById('cfg-disability').value,
            work_authorization: document.getElementById('cfg-work-auth').value,
            sponsorship_required: document.getElementById('cfg-sponsorship').value,
            ats_login_password: document.getElementById('cfg-ats-login-password').value,
            workday_email: document.getElementById('cfg-workday-email').value,
            workday_password: document.getElementById('cfg-workday-password').value,
            experience: getExperienceData(),
            education: getEducationData(),
            certifications: document.getElementById('cfg-certifications').value.split('\n').map(s => s.trim()).filter(Boolean),
            references: getReferencesData(),
        },
        search: {
            keywords: splitTrim(document.getElementById('cfg-keywords').value),
            locations: document.getElementById('cfg-locations').value.split('\n').map(s => s.trim()).filter(Boolean),
            exclude_keywords: splitTrim(document.getElementById('cfg-exclude').value),
            min_salary: parseInt(document.getElementById('cfg-salary').value) || 0,
            // Write both greenhouse keys: canonical (fetcher prefers it) and
            // legacy (so a stale legacy list can't shadow a cleared field).
            greenhouse_boards: splitTrim(document.getElementById('cfg-greenhouse').value),
            greenhouse_companies: splitTrim(document.getElementById('cfg-greenhouse').value),
            lever_companies: splitTrim(document.getElementById('cfg-lever').value),
            ashby_boards: splitTrim(document.getElementById('cfg-ashby').value),
            workable_accounts: splitTrim(document.getElementById('cfg-workable').value),
            recruitee_companies: splitTrim(document.getElementById('cfg-recruitee').value),
        },
        auto_apply: {
            enabled: document.getElementById('cfg-auto-apply').checked,
            auto_approve: document.getElementById('cfg-auto-approve').checked,
            headless: document.getElementById('cfg-headless').checked,
            use_browser_use: document.getElementById('cfg-browser-use').checked,
            max_daily_applications: parseInt(document.getElementById('cfg-max-daily').value) || 20,
            step_ceiling: parseInt(document.getElementById('cfg-step-ceiling').value),
            disable_stuck_detection: document.getElementById('cfg-disable-stuck-detection').checked,
            per_domain_rate_limit: parseInt(document.getElementById('cfg-per-domain-rate-limit').value) || 0,
            review_required_rules: {
                unknown_ats: document.getElementById('cfg-review-unknown-ats').checked,
                min_confidence: parseFloat(document.getElementById('cfg-min-confidence').value) || 0.60,
            },
        },
        ai: {
            base_url: document.getElementById('cfg-ai-url').value,
            scoring_tier: document.getElementById('cfg-scoring-tier').value || 'strong',
            models: {
                fast: { model: document.getElementById('cfg-ai-model-fast').value || '' },
                strong: { model: document.getElementById('cfg-ai-model-strong').value || '' },
                utility: { model: document.getElementById('cfg-ai-model-utility').value || '' },
            },
        },
        api_keys: {
            adzuna_app_id: document.getElementById('cfg-adzuna-app-id').value,
            adzuna_app_key: document.getElementById('cfg-adzuna-app-key').value,
            usajobs_email: document.getElementById('cfg-usajobs-email').value,
            usajobs_api_key: document.getElementById('cfg-usajobs-key').value,
        },
        flaresolverr: {
            url: document.getElementById('cfg-flaresolverr-url').value,
        },
        salary_estimator: {
            bls: {
                api_key: (document.getElementById('cfg-bls-api-key')?.value || '').trim(),
            },
        },
    };

    try {
        await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
        toast('Settings saved!', 'success');
    } catch (e) {
        toast('Failed to save settings', 'error');
    }
}

// ---- Salary Auto-Ingest Toggle ----

function _applySalaryAutoIngest(flag) {
    const settingsCb = document.getElementById('cfg-salary-auto-ingest');
    const fetchCb = document.getElementById('fetch-auto-estimate-toggle');
    if (settingsCb) settingsCb.checked = flag;
    if (fetchCb) fetchCb.checked = flag;
    window._salaryAutoIngest = flag;
    const toggleBtn = document.getElementById('score-salary-toggle');
    if (toggleBtn) {
        toggleBtn.textContent = flag ? '+ pulls market salaries' : 'x not pulling market salaries';
        toggleBtn.classList.toggle('badge-estimate-off', !flag);
    }
}

async function saveSalaryAutoIngest(ev) {
    const settingsCb = document.getElementById('cfg-salary-auto-ingest');
    const fetchCb = document.getElementById('fetch-auto-estimate-toggle');
    let flag;
    if (ev && ev.toggle) {
        flag = !window._salaryAutoIngest;
    } else {
        const src = ev && ev.target ? ev.target : null;
        flag = !!(src ? src.checked : (settingsCb ? settingsCb.checked : (fetchCb ? fetchCb.checked : true)));
    }
    _applySalaryAutoIngest(flag);
    try {
        await api('/api/settings/salary-estimator-auto-ingest', {
            method: 'PUT',
            body: JSON.stringify({ auto_on_ingest: flag }),
        });
        toast(flag ? 'Auto salary estimates enabled' : 'Auto salary estimates disabled', 'success');
    } catch (e) {
        toast('Failed to update salary auto-estimate setting', 'error');
    }
}

async function reestimateSalary(jobId) {
    try {
        toast('Re-estimating salary...', 'info');
        const data = await api(`/api/jobs/${jobId}/estimate-salary`, { method: 'POST' });
        if (data.status === 'no_data') {
            toast(data.message || 'No market data available for this title/location.', 'warning');
            return;
        }
        if (data.status === 'quota_exceeded') {
            toast(data.message || 'API quota reached — try again tomorrow.', 'warning');
            return;
        }
        if (data.status === 'resource_exhausted') {
            toast(data.message || 'Server out of file descriptors — please restart.', 'error');
            return;
        }
        const est = data.estimate || {};
        const cached = window._currentJobs && window._currentJobs[jobId];
        if (cached) {
            cached.estimated_salary_min = est.min;
            cached.estimated_salary_max = est.max;
            cached.estimated_salary_period = est.period;
            cached.estimated_salary_source = est.source;
            cached.estimated_salary_confidence = est.confidence;
            cached.estimated_salary_metadata = JSON.stringify(est.metadata || {});
        }
        toast('Salary estimate updated', 'success');
        if (selectedJobId === jobId) selectJob(jobId);
    } catch (e) {
        toast('Salary estimate failed — check server logs', 'error');
    }
}

// ---- Honesty Level ----

let _aiEditDefaultHonesty = 'honest';

function _applyHonestyLevel(level) {
    _aiEditDefaultHonesty = level;
    document.querySelectorAll('.honesty-stop').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.level === level);
    });
    const needsWarn = level === 'embellished' || level === 'fabricated';
    const warn = document.getElementById('honesty-inline-warning');
    if (warn) warn.style.display = needsWarn ? '' : 'none';
}

async function setHonestyLevel(level) {
    try {
        await api('/api/settings/honesty-level', {
            method: 'PUT',
            body: JSON.stringify({ honesty_level: level }),
        });
        _applyHonestyLevel(level);
        toast(`Honesty level set to "${level}"`, 'success');
    } catch (e) {
        toast('Failed to update honesty level', 'error');
    }
}

// ---- Resume Style ----

function _applyResumeStyle(style) {
    document.querySelectorAll('.resume-style-stop').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.style === style);
    });
}

async function setResumeStyle(style) {
    try {
        await api('/api/settings/resume-style', {
            method: 'PUT',
            body: JSON.stringify({ resume_style: style }),
        });
        _applyResumeStyle(style);
        toast(`Resume style set to "${style}"`, 'success');
    } catch (e) {
        toast('Failed to update resume style', 'error');
    }
}

// ---- Document Format ----

function _applyDocumentFormat(fmt) {
    document.querySelectorAll('.document-format-stop').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.fmt === fmt);
    });
}

async function setDocumentFormat(fmt) {
    try {
        await api('/api/settings/document-format', {
            method: 'PUT',
            body: JSON.stringify({ document_format: fmt }),
        });
        _applyDocumentFormat(fmt);
        toast(`Document format set to "${fmt.toUpperCase()}"`, 'success');
    } catch (e) {
        toast('Failed to update document format', 'error');
    }
}

// ---- Max Resume Experience Entries ----

function _applyMaxExpEntries(value) {
    const allBox = document.getElementById('max-exp-all');
    const numIn = document.getElementById('max-exp-entries');
    if (!allBox || !numIn) return;
    if (value === null || value === undefined) {
        allBox.checked = true;
        numIn.disabled = true;
        numIn.value = '';
    } else {
        allBox.checked = false;
        numIn.disabled = false;
        numIn.value = value;
    }
}

async function _saveMaxExpEntries(value) {
    try {
        await api('/api/settings/max-resume-experience-entries', {
            method: 'PUT',
            body: JSON.stringify({ max_resume_experience_entries: value }),
        });
        toast(value === null ? 'Resume will include all roles' : `Resume capped at ${value} roles`, 'success');
    } catch (e) {
        toast('Failed to update max experience entries', 'error');
    }
}

function onMaxExpAllToggle() {
    const allBox = document.getElementById('max-exp-all');
    const numIn = document.getElementById('max-exp-entries');
    if (allBox.checked) {
        numIn.disabled = true;
        numIn.value = '';
        _saveMaxExpEntries(null);
    } else {
        numIn.disabled = false;
        const fallback = 3;
        numIn.value = fallback;
        _saveMaxExpEntries(fallback);
    }
}

function onMaxExpEntriesChange() {
    const numIn = document.getElementById('max-exp-entries');
    const v = parseInt(numIn.value, 10);
    if (!Number.isFinite(v) || v < 1 || v > 20) {
        toast('Cap must be between 1 and 20', 'error');
        return;
    }
    _saveMaxExpEntries(v);
}

// ---- AI Edit Model Tier ----

let _aiEditDefaultTier = 'strong';

function _applyAiEditModelTier(tier) {
    document.querySelectorAll('.ai-edit-tier-stop').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tier === tier);
    });
}

async function setAiEditModelTier(tier) {
    try {
        await api('/api/settings/ai-edit-model-tier', {
            method: 'PUT',
            body: JSON.stringify({ model_tier: tier }),
        });
        _aiEditDefaultTier = tier;
        _applyAiEditModelTier(tier);
        toast(`AI edit model set to "${tier}"`, 'success');
    } catch (e) {
        toast('Failed to update AI edit model', 'error');
    }
}

// ---- Embellishment Panel ----

function _renderEmbellishmentContent(log) {
    if (!log) {
        return '<p style="font-size:13px;color:var(--text-muted);padding:8px 0">No embellishment data — tailor this job first.</p>';
    }

    const isFabricated = log.honesty_level === 'fabricated';
    const noChanges = (log.resume_changes || []).length === 0 && (log.cover_letter_changes || []).length === 0;
    const levelColors = { honest: 'var(--accent-green)', tailored: 'var(--accent-blue)', embellished: 'var(--accent-yellow)', fabricated: 'var(--accent-red)' };
    const levelColor = levelColors[log.honesty_level] || 'var(--text-secondary)';

    let html = '';

    if (isFabricated && log.WARNING) {
        html += `<div class="emb-warning-red">${escapeHtml(log.WARNING)}</div>`;
    }

    html += `<div style="margin-bottom:12px;font-size:13px;color:var(--text-secondary)">
        Honesty level: <span class="emb-level-badge" style="color:${levelColor};background:${levelColor}1a">${escapeHtml(log.honesty_level)}</span>
    </div>`;

    if (noChanges) {
        html += `<p style="color:var(--accent-green);font-size:13px">&#10003; No embellishments &mdash; this application uses your unmodified profile.</p>`;
        return html;
    }

    const makeTable = (changes) => {
        if (!changes || changes.length === 0) return '';
        const rows = changes.map(c => `
            <tr>
                <td class="emb-td-field">${escapeHtml(c.field)}</td>
                <td class="emb-td">${escapeHtml(c.original)}</td>
                <td class="emb-td">${escapeHtml(c.modified)}</td>
            </tr>`).join('');
        return `<table class="emb-table">
            <thead><tr><th>Field</th><th>Original</th><th>Modified</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;
    };

    if ((log.resume_changes || []).length > 0) {
        html += `<h5 class="emb-section-label">Resume Changes</h5>${makeTable(log.resume_changes)}`;
    }
    if ((log.cover_letter_changes || []).length > 0) {
        html += `<h5 class="emb-section-label">Cover Letter Changes</h5>${makeTable(log.cover_letter_changes)}`;
    }

    return html;
}

async function toggleEmbPanel(jobId) {
    const panel = document.getElementById(`emb-panel-${jobId}`);
    if (!panel) return;
    const isHidden = panel.style.display === 'none' || panel.style.display === '';
    if (!isHidden) { panel.style.display = 'none'; return; }

    panel.style.display = 'block';
    if (panel.dataset.loaded) return;  // already fetched

    panel.innerHTML = '<p style="font-size:13px;color:var(--text-muted);padding:8px 0">Loading&hellip;</p>';
    try {
        const data = await api(`/api/jobs/${jobId}/embellishment-log`);
        panel.innerHTML = _renderEmbellishmentContent(data.embellishment_log);
        panel.dataset.loaded = '1';
    } catch (e) {
        panel.innerHTML = '<p style="font-size:13px;color:var(--accent-red)">Failed to load embellishment log.</p>';
    }
}

async function loadEmbTab(jobId, containerId) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (el.dataset.loaded) return;

    el.innerHTML = '<p style="font-size:13px;color:var(--text-muted)">Loading&hellip;</p>';
    try {
        const data = await api(`/api/jobs/${jobId}/embellishment-log`);
        el.innerHTML = _renderEmbellishmentContent(data.embellishment_log);
        el.dataset.loaded = '1';
    } catch (e) {
        el.innerHTML = '<p style="font-size:13px;color:var(--accent-red)">Failed to load embellishment log.</p>';
    }
}

async function loadAiModels({ preselect = {} } = {}) {
    const fastSel = document.getElementById('cfg-ai-model-fast');
    const strongSel = document.getElementById('cfg-ai-model-strong');
    const utilitySel = document.getElementById('cfg-ai-model-utility');
    // Save current selections before rebuild (so re-loading doesn't lose choices)
    const curFast = preselect.fast ?? fastSel.value;
    const curStrong = preselect.strong ?? strongSel.value;
    const curUtility = preselect.utility ?? (utilitySel ? utilitySel.value : '');

    const allSelects = [fastSel, strongSel, utilitySel].filter(Boolean);

    try {
        const data = await api('/api/ai/models');
        const models = data.models || [];

        allSelects.forEach(sel => {
            sel.innerHTML = '<option value="">— select a model —</option>';
            models.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m;
                opt.textContent = m;
                sel.appendChild(opt);
            });
        });

        // Restore saved or pre-selected values; if the saved model isn't in the
        // list (e.g. not currently loaded in LM Studio), add it as an option so
        // the user can see what's configured and decide whether to change it.
        const pairs = [[fastSel, curFast], [strongSel, curStrong]];
        if (utilitySel) pairs.push([utilitySel, curUtility]);
        pairs.forEach(([sel, val]) => {
            if (!sel || !val) return;
            if (![...sel.options].some(o => o.value === val)) {
                const opt = document.createElement('option');
                opt.value = val;
                opt.textContent = val + ' (not loaded)';
                sel.appendChild(opt);
            }
            sel.value = val;
        });

        return models;
    } catch (e) {
        // LM Studio not reachable — leave dropdowns with a placeholder
        allSelects.forEach(sel => {
            sel.innerHTML = '<option value="">— LM Studio not reachable —</option>';
        });
        // Still restore saved values as manual entries
        const pairs = [[fastSel, curFast], [strongSel, curStrong]];
        if (utilitySel) pairs.push([utilitySel, curUtility]);
        pairs.forEach(([sel, val]) => {
            if (!sel || !val) return;
            const opt = document.createElement('option');
            opt.value = val;
            opt.textContent = val + ' (saved)';
            sel.appendChild(opt);
            sel.value = val;
        });
        return [];
    }
}

async function loadNavigatorModel() {
    const model = document.getElementById('cfg-ai-model-fast').value;
    const ctx   = parseInt(document.getElementById('cfg-context-window').value) || 8192;
    const statusEl = document.getElementById('ctx-reload-status');

    if (!model) {
        statusEl.textContent = 'Select a Navigator Model first.';
        statusEl.className = 'ai-status disconnected';
        return;
    }

    statusEl.textContent = `Loading ${model} with ${ctx.toLocaleString()} token context… (may take up to 3 min)`;
    statusEl.className = 'ai-status';

    try {
        const data = await api('/api/ai/load-model', {
            method: 'POST',
            body: JSON.stringify({ model, context_window: ctx }),
        });
        statusEl.textContent = `✓ Loaded ${data.model} (${ctx.toLocaleString()} tokens, via ${data.method})`;
        statusEl.className = 'ai-status connected';
        await loadAiModels();   // refresh dropdowns now that a model is loaded
    } catch (e) {
        statusEl.textContent = `Failed to load model: ${e.message || e}`;
        statusEl.className = 'ai-status disconnected';
    }
}

async function applyContextWindow() {
    const ctxSel = document.getElementById('cfg-context-window');
    const statusEl = document.getElementById('ctx-reload-status');
    const ctx = parseInt(ctxSel.value);
    if (!ctx) return;

    statusEl.textContent = `Reloading model with ${ctx.toLocaleString()} token context… (may take up to 2 min for large models)`;
    statusEl.className = 'ai-status';

    try {
        const data = await api('/api/ai/reload-context', {
            method: 'POST',
            body: JSON.stringify({ context_window: ctx }),
        });
        const reloaded = (data.reloaded || []).map(r => r.model).join(', ');
        if (reloaded) {
            statusEl.textContent = `✓ Reloaded with ${ctx.toLocaleString()} token context: ${reloaded}`;
            statusEl.className = 'ai-status connected';
        } else {
            const errs = (data.errors || []).map(e => e.error).join(' | ');
            // Check whether the error is the known "remote LM Studio doesn't support reload" case
            const isRemoteApiLimit = errs.includes('does not support programmatic reload');
            if (isRemoteApiLimit) {
                statusEl.innerHTML =
                    `<strong>Context saved to ${ctx.toLocaleString()} tokens.</strong> ` +
                    `LM Studio's REST API doesn't support remote reload on this version.<br>` +
                    `<strong>To apply now:</strong> open LM Studio on your homelab → ` +
                    `click the loaded model → change <em>Context Length</em> to <strong>${ctx.toLocaleString()}</strong> → click <em>Reload</em>.`;
            } else {
                statusEl.textContent = `Reload failed: ${errs}`;
            }
            statusEl.className = 'ai-status disconnected';
        }
    } catch (e) {
        statusEl.textContent = `Failed: ${e.message || e}`;
        statusEl.className = 'ai-status disconnected';
    }
}

async function testAI() {
    const statusEl = document.getElementById('ai-status');
    statusEl.textContent = 'Testing connection...';
    statusEl.className = 'ai-status';

    try {
        const data = await api('/api/health');
        if (data.ai?.connected) {
            statusEl.textContent = `Connected — ${data.ai.models.length} model(s) available`;
            statusEl.className = 'ai-status connected';
            // Refresh dropdowns with live model list, preserving current selections
            await loadAiModels();
        } else {
            statusEl.textContent = `Not connected: ${data.ai?.error || 'Unknown error'}`;
            statusEl.className = 'ai-status disconnected';
        }
    } catch (e) {
        statusEl.textContent = 'Connection test failed';
        statusEl.className = 'ai-status disconnected';
    }
}

async function verifyLinkedinLocations() {
    const input = document.getElementById('cfg-locations');
    const out = document.getElementById('linkedin-loc-results');
    const raw = (input.value || '').split('\n').map(s => s.trim()).filter(Boolean);
    if (!raw.length) {
        out.innerHTML = '<span class="hint" style="color:#c33">Enter at least one location first.</span>';
        return;
    }
    out.innerHTML = '<span class="hint">Resolving…</span>';
    try {
        const res = await fetch('/api/linkedin/resolve-locations', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({locations: raw}),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const rows = (data.results || []).map(r => {
            const icon = r.ok ? '✓' : '✗';
            const color = r.ok ? '#2a7' : '#c33';
            const previewUrl = r.ok ? `https://www.linkedin.com/jobs/search?geoId=${r.geo_id}` : '';
            const detail = r.ok
                ? `geoId <code>${r.geo_id}</code> <span class="hint">(${r.source})</span> &mdash; <a href="${previewUrl}" target="_blank" rel="noopener">preview on LinkedIn</a>`
                : `<span class="hint">not resolved — LinkedIn will fall back to text matching</span>`;
            return `<div style="font-size:13px;line-height:1.6"><span style="color:${color};font-weight:600">${icon}</span> <strong>${escapeHtml(r.location)}</strong> → ${detail}</div>`;
        }).join('');
        out.innerHTML = rows || '<span class="hint">No locations resolved.</span>';
    } catch (e) {
        out.innerHTML = `<span class="hint" style="color:#c33">Verification failed: ${escapeHtml(e.message)}</span>`;
    }
}


// ============================================================
// AI job-title suggester (Settings → Search + setup wizard)
// ============================================================
const TS_QUESTIONS = [
    { key: 'direction', label: 'What should your next role look like?', type: 'select',
      options: ['Same kind of role as my recent experience', 'A step up in seniority', 'A pivot into a different specialty', 'A move into people management', 'Open to anything'] },
    { key: 'focus', label: 'Which skills or parts of your experience do you most want to use?', type: 'text',
      placeholder: 'e.g. cloud security, incident response, Python automation' },
    { key: 'seniority', label: 'What seniority level should the titles target?', type: 'select',
      options: ['No preference', 'Entry / junior', 'Mid-level', 'Senior', 'Lead / staff', 'Manager / director'] },
    { key: 'avoid', label: 'Anything to avoid?', type: 'text',
      placeholder: 'e.g. sales-adjacent roles, heavy on-call, defense industry' },
];

let _tsState = { targetId: null, useWizardProfile: false, titles: [] };

function openTitleSuggest(targetId, useWizardProfile) {
    _tsState = { targetId, useWizardProfile: !!useWizardProfile, titles: [] };
    _tsRenderQuestions();
    document.getElementById('ts-modal').style.display = 'flex';
}

function tsClose() {
    document.getElementById('ts-modal').style.display = 'none';
}

function _tsRenderQuestions() {
    const body = document.getElementById('ts-body');
    body.innerHTML = `
        <p class="ob-lead" style="margin-top:0">A few quick questions so the suggestions match where you want to go — every field is optional.</p>
        ${TS_QUESTIONS.map(q => `
            <div class="form-group">
                <label>${esc(q.label)}</label>
                ${q.type === 'select'
                    ? `<select data-ts-q="${q.key}">${q.options.map(o => `<option>${esc(o)}</option>`).join('')}</select>`
                    : `<input type="text" data-ts-q="${q.key}" placeholder="${esc(q.placeholder || '')}">`}
            </div>`).join('')}
        <div class="ts-actions">
            <button class="btn btn-primary" id="ts-submit-btn" onclick="tsSubmitAnswers()">Get suggestions</button>
            <span id="ts-status" class="ob-status"></span>
        </div>
    `;
}

// Build a profile object from the wizard's in-progress (unsaved) fields.
function _tsWizardProfile() {
    return {
        full_name: document.getElementById('ob-name').value.trim(),
        summary: document.getElementById('ob-summary').value.trim(),
        skills: splitCsvSmart(document.getElementById('ob-skills').value),
        experience: obGetExperienceData(),
        education: obGetEducationData(),
        certifications: obSplitLines(document.getElementById('ob-certifications').value),
    };
}

function _tsErrMessage(e) {
    try { return JSON.parse(e.message).detail || e.message; } catch (_) { return e.message || String(e); }
}

async function tsSubmitAnswers() {
    const answers = {};
    document.querySelectorAll('#ts-body [data-ts-q]').forEach(el => {
        const v = el.value.trim();
        if (v) answers[el.dataset.tsQ] = v;
    });
    const payload = { answers };
    if (_tsState.useWizardProfile) payload.profile = _tsWizardProfile();
    const btn = document.getElementById('ts-submit-btn');
    const status = document.getElementById('ts-status');
    btn.disabled = true;
    status.className = 'ob-status busy';
    status.textContent = 'Asking your local AI… this can take 10–60 seconds.';
    try {
        const r = await api('/api/settings/suggest-job-titles', { method: 'POST', body: JSON.stringify(payload) });
        _tsState.titles = r.titles || [];
        _tsRenderResults();
    } catch (e) {
        btn.disabled = false;
        status.className = 'ob-status err';
        status.textContent = _tsErrMessage(e);
    }
}

function _tsRenderResults() {
    const body = document.getElementById('ts-body');
    body.innerHTML = `
        <p class="ob-lead" style="margin-top:0">Pick the titles you want to search for — they're added to your keywords, nothing is removed.</p>
        ${_tsState.titles.map((t, i) => `
            <label class="ts-title-row">
                <input type="checkbox" data-ts-idx="${i}" checked>
                <div>
                    <div class="ts-title-name">${esc(t.title)}</div>
                    ${t.reason ? `<div class="ts-title-reason">${esc(t.reason)}</div>` : ''}
                </div>
            </label>`).join('')}
        <div class="ts-actions">
            <button class="btn btn-ghost" onclick="_tsRenderQuestions()">&larr; Adjust answers</button>
            <button class="btn btn-primary" onclick="tsAddSelected()">Add selected to keywords</button>
        </div>
    `;
}

function tsAddSelected() {
    const input = document.getElementById(_tsState.targetId);
    if (!input) { tsClose(); return; }
    const existing = input.value.split(',').map(s => s.trim()).filter(Boolean);
    const have = new Set(existing.map(s => s.toLowerCase()));
    let added = 0;
    document.querySelectorAll('#ts-body input[data-ts-idx]:checked').forEach(cb => {
        const t = (_tsState.titles[parseInt(cb.dataset.tsIdx, 10)] || {}).title;
        if (t && !have.has(t.toLowerCase())) {
            existing.push(t);
            have.add(t.toLowerCase());
            added++;
        }
    });
    input.value = existing.join(', ');
    tsClose();
    if (!added) { toast('No new titles added — they were already in your keywords', 'info'); return; }
    const needsSave = _tsState.targetId === 'cfg-keywords';
    toast(`Added ${added} title${added === 1 ? '' : 's'}${needsSave ? ' — click Save Settings to persist' : ''}`, 'success');
}

// ---- Basic / Advanced settings mode ----
// Basic shows everything needed to run the app; Advanced additionally exposes
// tuning knobs (Auto-Apply, Prompts, Logs tabs + .settings-advanced elements).
// The mode only affects visibility — hidden inputs keep their values and are
// still collected by saveSettings(), so switching modes never loses data.

function getSettingsMode() {
    return localStorage.getItem('jobsmith_settings_mode') === 'advanced' ? 'advanced' : 'basic';
}

function setSettingsMode(mode) {
    localStorage.setItem('jobsmith_settings_mode', mode);
    applySettingsMode();
}

function applySettingsMode() {
    const section = document.getElementById('settings');
    if (!section) return;
    const adv = getSettingsMode() === 'advanced';
    section.classList.toggle('settings-mode-advanced', adv);
    document.getElementById('settings-mode-basic')?.classList.toggle('active', !adv);
    document.getElementById('settings-mode-advanced')?.classList.toggle('active', adv);

    // If the active tab just became hidden (e.g. Prompts open, switch to Basic),
    // fall back to the first visible tab.
    const activeTab = section.querySelector('.settings-tab.active');
    if (!adv && activeTab && activeTab.classList.contains('settings-advanced')) {
        const firstVisible = section.querySelector('.settings-tab:not(.settings-advanced)');
        if (firstVisible) firstVisible.click();
    }
}

document.addEventListener('DOMContentLoaded', applySettingsMode);
