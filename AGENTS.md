# AGENTS.md — idx-financial-scraper

Standing contract for any agent working in this repository.

## Who Does What

- **Codex** — quick fixes and maintenance.
- **Claude** — new features, refactors, schema changes.

The boundary is measured on the diff, never estimated from elapsed time:

- Codex may take the work when the diff is **≤2 files, ≤2 directories, ≤100 changed lines**.
- Everything else goes to Claude.
- **Anything touching a gate file goes to Claude regardless of size:**
  `.github/workflows/`, `.env*`, `AGENTS.md`, `CLAUDE.md`, `docs/WORKFLOW.md`,
  `requirements*.txt`, and any module that reads or writes credentials or tokens.

The agent measures its own diff before opening a PR:

```bash
gh pr view --json changedFiles,additions,deletions
```

If it exceeds the threshold: **do not open the PR.** Hand the branch to the other agent with one
line of context.
