// Facts editor — short true statements the AI gets with every write and edit.
// One widget for both scopes: "Facts about me" (/api/profile/facts, Settings) and
// "Facts for this application" (/api/applications/{id}/facts, AI Edit modal).
// Changes save immediately; the list is built with textContent only.

function mountFactsEditor(host, opts) {
    const { path, label, hint, placeholder } = opts;
    let facts = [];
    let maxFacts = 50;
    let maxChars = 1000;

    const el = (tag, props = {}, ...kids) => {
        const n = Object.assign(document.createElement(tag), props);
        kids.forEach(k => n.append(k));
        return n;
    };
    const fail = (e) => toast(e && e.message ? e.message : 'Could not save the fact', 'error');

    async function load() {
        const res = await api(path);
        facts = res.facts || [];
        maxFacts = res.max_facts || maxFacts;
        maxChars = res.max_chars || maxChars;
        render();
    }

    async function add(text) {
        text = (text || '').trim();
        if (!text) return false;
        try {
            await api(path, { method: 'POST', body: JSON.stringify({ text }) });
        } catch (e) { fail(e); return false; }
        await load();
        return true;
    }

    function row(f) {
        const text = el('span', { className: 'fact-text', textContent: f.text });
        text.style.flex = '1';
        const edit = el('button', { className: 'btn btn-secondary btn-sm', type: 'button', textContent: 'Edit' });
        const remove = el('button', { className: 'btn btn-secondary btn-sm', type: 'button', textContent: 'Remove' });
        const line = el('div', { className: 'fact-row' }, text, edit, remove);
        line.style.cssText = 'display:flex;gap:8px;align-items:center;font-size:13px;margin-bottom:4px';
        remove.onclick = async () => {
            try { await api(`${path}/${f.id}`, { method: 'DELETE' }); } catch (e) { fail(e); return; }
            await load();
        };
        edit.onclick = () => {
            const input = el('textarea', { value: f.text, rows: 2, maxLength: maxChars });
            input.style.cssText = 'flex:1;font-size:13px';
            const save = el('button', { className: 'btn btn-primary btn-sm', type: 'button', textContent: 'Save' });
            const cancel = el('button', { className: 'btn btn-secondary btn-sm', type: 'button', textContent: 'Cancel' });
            line.replaceChildren(input, save, cancel);
            cancel.onclick = render;
            save.onclick = async () => {
                try {
                    await api(`${path}/${f.id}`, { method: 'PATCH', body: JSON.stringify({ text: input.value }) });
                } catch (e) { fail(e); return; }
                await load();
            };
        };
        return line;
    }

    function render() {
        const input = el('input', { type: 'text', placeholder: placeholder || 'Add a fact…', maxLength: maxChars });
        input.style.cssText = 'flex:1;font-size:13px';
        const addBtn = el('button', { className: 'btn btn-secondary btn-sm', type: 'button', textContent: 'Add' });
        const addRow = el('div', {}, input, addBtn);
        addRow.style.cssText = 'display:flex;gap:8px;margin-top:6px';
        const submit = async () => { if (await add(input.value)) input.value = ''; };
        addBtn.onclick = submit;
        input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } };

        const title = el('div', { textContent: `${label} (${facts.length}/${maxFacts})` });
        title.style.cssText = 'font-size:13px;font-weight:500;color:var(--text-secondary);margin-bottom:4px';
        const note = el('div', { textContent: hint });
        note.style.cssText = 'font-size:11px;color:var(--text-muted);margin-bottom:6px';
        host.replaceChildren(title, note, ...facts.map(row), addRow);
    }

    load().catch(fail);
    return { reload: load, add };
}
