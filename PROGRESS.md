# Writing safety: resume checklist

Four stacked branches (each from the previous): A feat/untrusted-posting-guard,
B feat/application-facts, C feat/fact-guard, D feat/section-edits-undo.
Remove this file in the last commit of D.

- [x] A: untrusted-posting guard + fence in prompt_registry.render_prompt; auto_apply llm_client
- [x] B: profile.facts (config) + application_facts table (migration 30-31), CRUD, facts_block appended to every writing prompt, Apply Assist answers, UI (frontend/js/facts.js), tests
- [ ] C: backend/fact_guard.py, flags stored + returned by /revise, UI notice
- [ ] D: section edits, application_revisions + undo/restore, UI; delete this file
