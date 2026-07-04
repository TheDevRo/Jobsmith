// prompts.js — Settings → Prompts tab: view/edit/reset the internal AI prompts.
// Loaded once per session; saves are per-prompt via PUT/DELETE /api/prompts/{key}.

let _promptsData = null;   // array from GET /api/prompts, in registry order
let _promptsLoaded = false;

async function loadPrompts() {
    // Load once — a refetch while the user has unsaved edits would clobber them.
    if (_promptsLoaded) return;
    const list = document.getElementById('prompts-list');
    if (!list) return;
    try {
        const data = await api('/api/prompts');
        _promptsData = data.prompts || [];
        _promptsLoaded = true;
        renderPrompts();
    } catch (e) {
        list.innerHTML = `<p class="section-hint">Failed to load prompts: ${esc(e.message)}</p>`;
    }
}

function _promptByKey(key) {
    return (_promptsData || []).find(p => p.key === key);
}

function renderPrompts() {
    const list = document.getElementById('prompts-list');
    if (!list) return;

    const groups = [];
    const byGroup = {};
    for (const p of _promptsData) {
        if (!byGroup[p.group]) { byGroup[p.group] = []; groups.push(p.group); }
        byGroup[p.group].push(p);
    }

    let html = '';
    for (const group of groups) {
        html += `<div class="prompt-group-title">${esc(group)}</div>`;
        for (const p of byGroup[group]) {
            const varChips = Object.entries(p.variables).map(([name, desc]) =>
                `<span class="prompt-var-chip" title="${esc(desc)}" onclick="insertPromptVar('${p.key}', '${name}')">{${esc(name)}}</span>`
            ).join('');
            html += `
            <div class="prompt-card" id="prompt-card-${p.key}">
                <div class="prompt-card-header" onclick="togglePromptCard('${p.key}')">
                    <span class="prompt-card-chevron" id="prompt-chevron-${p.key}">&#9656;</span>
                    <span class="prompt-card-title">${esc(p.label)}</span>
                    <span class="prompt-badge" id="prompt-badge-${p.key}" ${p.customized ? '' : 'hidden'}>Customized</span>
                </div>
                <div class="prompt-card-body" id="prompt-body-${p.key}" hidden>
                    <p class="section-hint">${esc(p.description)}</p>
                    ${varChips ? `<div class="prompt-vars"><span class="prompt-vars-label">Placeholders (click to insert):</span> ${varChips}</div>` : ''}
                    <textarea class="prompt-editor" id="prompt-editor-${p.key}" spellcheck="false"
                        oninput="promptEdited('${p.key}')">${esc(p.override !== null ? p.override : p.default)}</textarea>
                    <div class="prompt-actions">
                        <button class="btn btn-primary btn-sm" id="prompt-save-${p.key}" onclick="savePrompt('${p.key}')" disabled>Save Prompt</button>
                        <button class="btn btn-secondary btn-sm" onclick="resetPrompt('${p.key}')">Reset to Default</button>
                        <span class="prompt-status" id="prompt-status-${p.key}"></span>
                    </div>
                </div>
            </div>`;
        }
    }
    list.innerHTML = html;
}

function togglePromptCard(key) {
    const body = document.getElementById(`prompt-body-${key}`);
    const chevron = document.getElementById(`prompt-chevron-${key}`);
    if (!body) return;
    body.hidden = !body.hidden;
    if (chevron) chevron.innerHTML = body.hidden ? '&#9656;' : '&#9662;';
}

function promptEdited(key) {
    const p = _promptByKey(key);
    const editor = document.getElementById(`prompt-editor-${key}`);
    const saveBtn = document.getElementById(`prompt-save-${key}`);
    if (!p || !editor || !saveBtn) return;
    const current = p.override !== null ? p.override : p.default;
    saveBtn.disabled = editor.value === current;
    const status = document.getElementById(`prompt-status-${key}`);
    if (status) status.textContent = saveBtn.disabled ? '' : 'Unsaved changes';
}

function insertPromptVar(key, name) {
    const editor = document.getElementById(`prompt-editor-${key}`);
    if (!editor) return;
    const token = `{${name}}`;
    const start = editor.selectionStart ?? editor.value.length;
    const end = editor.selectionEnd ?? editor.value.length;
    editor.value = editor.value.slice(0, start) + token + editor.value.slice(end);
    editor.focus();
    editor.selectionStart = editor.selectionEnd = start + token.length;
    promptEdited(key);
}

async function savePrompt(key) {
    const p = _promptByKey(key);
    const editor = document.getElementById(`prompt-editor-${key}`);
    if (!p || !editor) return;
    try {
        const resp = await api(`/api/prompts/${key}`, {
            method: 'PUT',
            body: JSON.stringify({ template: editor.value }),
        });
        p.customized = resp.customized;
        p.override = resp.customized ? editor.value : null;
        if (!resp.customized) editor.value = p.default;
        const badge = document.getElementById(`prompt-badge-${key}`);
        if (badge) badge.hidden = !resp.customized;
        promptEdited(key);
        if (resp.unknown_variables && resp.unknown_variables.length) {
            toast(`Saved, but these placeholders are never filled in and will appear as literal text: ${resp.unknown_variables.map(v => `{${v}}`).join(', ')}`, 'info');
        } else {
            toast(resp.customized ? `Saved custom "${p.label}" prompt` : `"${p.label}" matches the default — override removed`, 'success');
        }
    } catch (e) {
        toast(`Failed to save prompt: ${e.message}`, 'error');
    }
}

async function resetPrompt(key) {
    const p = _promptByKey(key);
    const editor = document.getElementById(`prompt-editor-${key}`);
    if (!p || !editor) return;
    if (p.customized && !(await appConfirm(`Reset "${p.label}" to the built-in default? Your custom version will be deleted.`))) {
        return;
    }
    try {
        if (p.customized) await api(`/api/prompts/${key}`, { method: 'DELETE' });
        p.customized = false;
        p.override = null;
        editor.value = p.default;
        const badge = document.getElementById(`prompt-badge-${key}`);
        if (badge) badge.hidden = true;
        promptEdited(key);
        toast(`"${p.label}" reset to default`, 'success');
    } catch (e) {
        toast(`Failed to reset prompt: ${e.message}`, 'error');
    }
}
