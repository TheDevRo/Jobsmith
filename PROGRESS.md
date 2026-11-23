# Requirement coverage: resume checklist
- [ ] DB: `coverage` column (migration 30), update_job_score stores it, "best_coverage" sort, dismiss helper
- [ ] ai_engine: parse_requirements (new, tolerant), prompt extension, NLI-derived coverage
- [ ] API: dismiss endpoint; coverage in job responses (SELECT j.*)
- [ ] Frontend: N/M badge, detail list, "Best coverage" sort, "I don't have this"
- [ ] Tests (backend + frontend), full pytest, ruff, npm test
- [ ] Remove PROGRESS.md in last commit
