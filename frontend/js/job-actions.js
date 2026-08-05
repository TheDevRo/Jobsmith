// ==========================================================================
// Job actions registry — Phase 0 of UI_CONSOLIDATION_PLAN.md
//
// Five surfaces used to hard-code their own per-job button strips (Inbox list
// row, Inbox detail pane, deck peek modal, kanban ⋯-menu, classic Pipeline
// cards). They drifted: the same job offered Apply Assist in one place and not
// another, and the recycle-bin Restore/Erase pair existed on some surfaces by
// design and was simply missing from others by accident.
//
// This file is the single rule table. `jobActions(job, ctx)` returns the
// ordered, already-filtered list of action descriptors for one job in one
// context; `renderJobActions(job, ctx)` turns that into the exact markup the
// surface used to emit itself (same CSS classes, same btn-xs/btn-sm sizes,
// same inline handlers, same event.stopPropagation where the row needs it).
//
// Two axes, kept deliberately separate:
//   * VISIBILITY — one rule per action id, shared by every context (below, in
//     JOB_ACTIONS). This is where drift used to live.
//   * MEMBERSHIP + ORDER — per context, per live/deleted state, in
//     JOB_ACTION_ORDER. A context not listing an action simply doesn't offer
//     it; that is intent, not drift (e.g. the Inbox row deliberately offers
//     only Assist Me, and Erase exists only in Recently Deleted).
//
// Loaded before jobs.js / review.js / deck.js in index.html.
// ==========================================================================

// ---- Job field readers (the same rules the surfaces applied inline) ----

// A soft-deleted posting may still carry an application row; in Recently
// Deleted its own 'deleted' status wins over the application's.
function jobIsDeleted(job) {
    return !!job && job.status === 'deleted';
}

function jobEffectiveStatus(job) {
    if (!job) return '';
    return jobIsDeleted(job) ? 'deleted' : (job.app_status || job.status);
}

// UNIFIED RULE (the one intentional behavior change of Phase 0): Apply Assist
// is offered whenever the posting has a real URL to drive — nothing else. It
// used to be gated on `apply_type === 'external'` in the classic list/detail
// while the peek modal and the Pipeline review cards showed it unconditionally.
// No disabled state: an action that can't run isn't rendered.
function jobHasUrl(job) {
    return !!job && typeof safeHref === 'function' && safeHref(job.url) !== '#';
}

// ---- The rule table ----
// kind → button class; `tag: 'a'` renders an anchor instead of a button.
// `visible(job, ctx)` is the ONE visibility rule for that action, everywhere.
// Per-context label/title overrides live in `ctx` (surfaces phrase the same
// action differently: "Erase" in a cramped row, "Erase Permanently" in the
// detail pane) — the action, and whether it shows at all, is identical.
const JOB_ACTIONS = {
    'score': {
        kind: 'secondary',
        label: (job) => (job.fit_score ? 'Rescore' : 'Score'),
        handler: (id) => `scoreJob('${id}')`,
        visible: () => true,
    },
    'tailor': {
        kind: 'primary',
        label: 'Tailor Resume',
        title: 'Generate a resume and cover letter customized to this job',
        handler: (id) => `tailorJob('${id}')`,
        visible: () => true,
        ctx: { 'review-row': { label: 'Tailor' } },
    },
    'assist': {
        kind: 'assist',
        label: 'Apply Assist',
        handler: (id) => `launchAssist('${id}')`,
        visible: (job) => !jobIsDeleted(job) && jobHasUrl(job),
        ctx: { 'list-row': { label: 'Assist Me', title: 'Open Apply Assist browser' } },
    },
    'view-application': {
        kind: 'secondary',
        label: 'View Application',
        handler: (id) => `viewApplicationFor('${id}')`,
        visible: (job) => !!job.app_id,
    },
    'open-url': {
        kind: 'secondary',
        tag: 'a',
        label: 'Open Job URL',
        // External-apply postings route through Apply Assist instead; the raw
        // link would drop the user on the employer portal with no help.
        visible: (job) => job.apply_type !== 'external',
    },
    'mark-applied': {
        kind: 'green',
        label: 'Mark Applied',
        handler: (id) => `markApplied('${id}')`,
        visible: (job) => {
            const s = jobEffectiveStatus(job);
            return s !== 'applied' && s !== 'manual';
        },
    },
    'embellishments': {
        kind: 'secondary',
        label: 'Embellishments',
        handler: (id) => `toggleEmbPanel('${id}')`,
        visible: () => true,
    },
    'delete': {
        kind: 'danger',
        label: 'Delete',
        handler: (id) => `deleteSingleJob('${id}')`,
        visible: () => true,
        ctx: { 'kanban-menu': { label: 'Delete posting', handler: (id) => `_runCardMenuDelete('${id}')` } },
    },
    'restore': {
        kind: 'secondary',
        label: 'Restore to Inbox',
        title: 'Put this posting back in the Inbox',
        handler: (id) => `restoreJob('${id}')`,
        visible: (job) => jobIsDeleted(job),
        ctx: { 'list-row': { label: 'Restore', title: 'Restore this posting to the Inbox' } },
    },
    'erase': {
        kind: 'danger',
        label: 'Erase Permanently',
        title: 'Erase permanently — this posting can come back in future searches',
        handler: (id) => `eraseJob('${id}')`,
        visible: (job) => jobIsDeleted(job),
        ctx: { 'list-row': { label: 'Erase' } },
    },
    'pass': {
        kind: 'ghost',
        label: 'Pass',
        handler: (id) => `passShortlisted('${id}')`,
        visible: () => true,
    },
};

// Membership + order, per context and per live/deleted state. A deleted job
// shows Restore + Erase + (conditionally) Open Job URL and nothing else.
const JOB_ACTION_ORDER = {
    'list-row': {
        live: ['assist'],
        deleted: ['restore', 'erase'],
    },
    'detail': {
        live: ['score', 'tailor', 'assist', 'view-application', 'open-url',
               'mark-applied', 'embellishments', 'delete'],
        deleted: ['restore', 'open-url', 'erase'],
    },
    // The peek modal renders buildJobDetailHtml verbatim, so it is the detail
    // context by construction — the alias keeps that explicit (and keeps the
    // old "modal always assists / pane gates on apply_type" split from
    // reappearing).
    'peek': 'detail',
    'kanban-menu': {
        // Column-to-column moves are driven by the board's transition map, not
        // by the job, so they stay in deck.js. Delete is the only job-level
        // entry; the ✕ hover-delete on the card is not a menu action.
        live: ['delete'],
        deleted: [],
    },
    'review-row': {
        live: ['tailor', 'score', 'pass'],
        deleted: [],
    },
    'review-detail': {
        live: ['assist'],
        deleted: [],
    },
};

// Button size per context (matches what each surface renders today).
const JOB_ACTION_SIZE = {
    'list-row': 'btn-xs',
    'detail': 'btn-sm',
    'review-row': 'btn-xs',
    'review-detail': 'btn-sm',
};

// Contexts whose buttons sit inside a clickable row/card: their handlers must
// not also select the row.
const JOB_ACTION_STOPS_PROPAGATION = { 'list-row': true };

const JOB_ACTION_KIND_CLASS = {
    primary: 'btn-primary',
    secondary: 'btn-secondary',
    danger: 'btn-danger',
    assist: 'btn-assist',
    green: 'btn-green',
    ghost: 'btn-ghost',
};

// The declared whitelist for a context, following the peek → detail alias.
// `state` is 'live' or 'deleted'. Exposed as a function (not the raw const)
// so tests and future consumers can read the table without depending on
// top-level `const` bindings being reachable as globals.
function jobActionWhitelist(ctx, state) {
    const { order } = _resolveJobActionCtx(ctx);
    if (!order) return [];
    return (order[state === 'deleted' ? 'deleted' : 'live'] || []).slice();
}

function _resolveJobActionCtx(ctx) {
    let resolved = JOB_ACTION_ORDER[ctx];
    while (typeof resolved === 'string') { ctx = resolved; resolved = JOB_ACTION_ORDER[ctx]; }
    return { key: ctx, order: resolved };
}

// Reads a field with the context override applied. Labels/titles may be
// functions of the job (Score ⇄ Rescore); `handler` is itself a function and is
// returned as-is (`raw`).
function _jobActionField(def, ctx, name, job, raw) {
    const over = def.ctx && def.ctx[ctx];
    const val = (over && over[name] !== undefined) ? over[name] : def[name];
    return (!raw && typeof val === 'function') ? val(job, ctx) : val;
}

// The public entry point: the ordered, visibility-filtered action descriptors
// for `job` in `ctx`. Every descriptor is already renderable — `html` is the
// exact markup the surface emits.
function jobActions(job, ctx) {
    if (!job) return [];
    const { key, order } = _resolveJobActionCtx(ctx);
    if (!order) return [];
    const ids = jobIsDeleted(job) ? order.deleted : order.live;
    const out = [];
    for (const id of ids) {
        const def = JOB_ACTIONS[id];
        if (!def || !def.visible(job, key)) continue;
        const label = _jobActionField(def, key, 'label', job);
        const title = _jobActionField(def, key, 'title', job);
        const handler = _jobActionField(def, key, 'handler', job, true);
        const sid = safeId(job.id);
        const stop = JOB_ACTION_STOPS_PROPAGATION[key] ? 'event.stopPropagation();' : '';
        const descriptor = {
            id,
            label,
            title: title || '',
            kind: def.kind,
            tag: def.tag || 'button',
            onclickAttr: handler ? stop + handler(sid) : '',
            href: def.tag === 'a' ? safeHref(job.url) : '',
            visible: true,
        };
        descriptor.html = _jobActionHtml(descriptor, job, key);
        out.push(descriptor);
    }
    return out;
}

function _jobActionHtml(a, job, ctx) {
    const titleAttr = a.title ? ` title="${escapeHtml(a.title)}"` : '';
    if (ctx === 'kanban-menu') {
        const cls = a.kind === 'danger' ? ' class="kmenu-danger"' : '';
        return `<button role="menuitem"${cls} onclick="${a.onclickAttr}"${titleAttr}>${escapeHtml(a.label)}</button>`;
    }
    const cls = `btn ${JOB_ACTION_KIND_CLASS[a.kind] || 'btn-secondary'} ${JOB_ACTION_SIZE[ctx] || 'btn-sm'}`;
    if (a.tag === 'a') {
        // data-jobsmith-open-url is what the desktop shell / extension hooks to
        // record "user opened the posting" — keep it on every rendering.
        return `<a class="${cls}" href="${escapeHtml(a.href)}" target="_blank" rel="noopener"`
            + ` data-jobsmith-open-url data-jobsmith-job-id="${escapeHtml(job.id)}"${titleAttr}>${escapeHtml(a.label)}</a>`;
    }
    return `<button class="${cls}" onclick="${a.onclickAttr}"${titleAttr}>${escapeHtml(a.label)}</button>`;
}

// Convenience for the surfaces: the whole strip as one HTML string.
function renderJobActions(job, ctx) {
    return jobActions(job, ctx).map((a) => a.html).join('\n            ');
}

// Ids only — handy for tests and for surfaces that want to know whether a
// strip would be empty before rendering a container around it.
function jobActionIds(job, ctx) {
    return jobActions(job, ctx).map((a) => a.id);
}
