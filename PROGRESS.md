# Writing safety: resume checklist

Four stacked branches (each from the previous): A feat/untrusted-posting-guard,
B feat/application-facts, C feat/fact-guard, D feat/section-edits-undo.
Remove this file in the last commit of D.

- [x] A: untrusted-posting guard + fence in prompt_registry.render_prompt; auto_apply llm_client
- [ ] B: profile facts + application facts, injection, UI, tests
- [ ] C: backend/fact_guard.py, flags stored + returned by /revise, UI notice
- [ ] D: section edits, application_revisions + undo/restore, UI; delete this file
