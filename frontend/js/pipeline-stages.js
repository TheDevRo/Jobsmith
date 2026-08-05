// Jobsmith frontend — the ONE Pipeline stage vocabulary.
//
// Phase 2 of the UI consolidation: the funnel strip, the kanban column heads,
// the classic stage tabs, the drag/menu move labels and the board legend all
// used to carry their own copies of these strings (and drifted). They now all
// render from PIPELINE_STAGES.
//
// Loaded before review.js and deck.js (classic script, shared global scope).
// The list is ordered by how work actually flows:
//   Shortlisted → Tailoring → Ready to Review → Applied, with Failed /
//   In Progress / Needs Attention hanging off the end for auditing.
//
// Each stage carries:
//   key    canonical id, also the key in the shared count store
//   label  the single user-facing string for that stage
//   desc   one-line meaning (funnel tooltips; wording from the tour copy)
//   funnel true → gets a segment in the funnel strip
//   board  true → gets a column on the kanban board
//   col    the board column this stage lives in (Failed and In Progress both
//          land in Needs Attention — the board is coarser than the funnel)
//   tab    the classic stage-table view name (currentReviewView value), if any
//   dot    column dot colour   seg  funnel segment colour class
const PIPELINE_STAGES = [
    {
        key: 'shortlisted', label: 'Shortlisted',
        desc: 'Jobs you kept while scouting the Inbox — no application exists yet.',
        funnel: true, board: true, col: 'shortlisted', tab: 'shortlisted',
        dot: 'var(--steel)', seg: 'fseg-steel',
    },
    {
        key: 'tailoring', label: 'Tailoring',
        desc: 'The AI is writing the résumé and cover letter for these.',
        funnel: false, board: true, col: 'tailoring', tab: null,
        dot: 'var(--accent-yellow)', seg: 'fseg-amber',
    },
    {
        key: 'pending', label: 'Ready to Review',
        desc: 'Tailored applications waiting for your approval before they go out.',
        funnel: true, board: true, col: 'pending', tab: 'pending',
        dot: 'var(--accent-ember)', seg: 'fseg-ember',
    },
    {
        key: 'applied', label: 'Applied',
        desc: 'Applications that have been submitted.',
        funnel: true, board: true, col: 'applied', tab: 'submitted',
        dot: 'var(--accent-green)', seg: 'fseg-green',
    },
    {
        key: 'failed', label: 'Failed',
        desc: 'Submissions that errored out — retry them or apply manually.',
        funnel: true, board: false, col: 'needs-attention', tab: 'failed',
        dot: 'var(--accent-red)', seg: 'fseg-red',
    },
    {
        key: 'in-progress', label: 'In Progress',
        desc: 'Submissions still mid-flight, plus anything that stopped and needs you.',
        funnel: true, board: false, col: 'needs-attention', tab: 'in-progress',
        dot: 'var(--accent-yellow)', seg: 'fseg-amber',
    },
    {
        key: 'needs-attention', label: 'Needs Attention',
        desc: 'Failed or stalled submissions that need a decision from you.',
        funnel: false, board: true, col: 'needs-attention', tab: null,
        dot: 'var(--accent-red)', seg: 'fseg-red',
    },
];

// Exposed as functions so the top-level `const` (lexical, and one eval unit in
// the jsdom tests) is reachable as a global everywhere.
function pipelineStages() { return PIPELINE_STAGES; }
function funnelStages() { return PIPELINE_STAGES.filter((s) => s.funnel); }
function boardStages() { return PIPELINE_STAGES.filter((s) => s.board); }
function stageByKey(key) { return PIPELINE_STAGES.find((s) => s.key === key) || null; }
function stageByTab(tab) { return PIPELINE_STAGES.find((s) => s.tab === tab) || null; }

// Label lookup for anything that speaks in stage keys (board columns, the drag
// map's `to` values). 'pass' is a verdict, not a stage, but the move menu and
// the board legend need a word for it.
function stageLabel(key) {
    if (key === 'pass') return 'Pass';
    const s = stageByKey(key);
    return s ? s.label : String(key);
}

// Count-store key for a classic tab name ('submitted' → 'applied').
function stageKeyForTab(tab) {
    const s = stageByTab(tab);
    return s ? s.key : tab;
}
