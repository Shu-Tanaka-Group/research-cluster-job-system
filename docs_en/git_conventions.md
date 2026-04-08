> *This document was auto-translated from the [Japanese original](../docs/git_conventions.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Git Conventions

## 1. Branch Naming Rules

```
<change-type>/#<issue-number>_<title>
```

- The title should be written in kebab-case (lowercase, words separated by hyphens)
- Minor changes not tied to an issue may be committed directly to main

### Change Types

| Type | Purpose |
|---|---|
| `feature` | Adding a new feature |
| `fix` | Bug fix |
| `docs` | Documentation-only changes |
| `refactor` | Code improvements without functional changes |
| `test` | Adding or modifying tests |

### Examples

```
feature/#2_gap-filling-dispatch-for-large-jobs
fix/#15_cancel-race-condition
docs/#8_update-deployment-guide
```

## 2. Commit Messages

### Format

```
<title line>

<body (optional)>

Co-Authored-By: <model name> <noreply@anthropic.com>
```

- The title line should be written in English
- The title line should start with an imperative verb (Add / Fix / Update / Implement / Remove, etc.)
- Commits tied to an issue should have `(#<issue-number>)` appended to the end of the title
- The body may be written in either Japanese or English. Describe the purpose (why) of the change
- Commits created by Claude should include a `Co-Authored-By` line. Use the running model name for `<model name>` (e.g., `Claude Opus 4.6 (1M context)`, `Claude Sonnet 4.6`, etc.)

### Verb Usage in Title Lines

| Verb | Purpose |
|---|---|
| Add | Adding new files, features, or tests |
| Implement | Implementing a previously designed feature |
| Update | Updating existing features or documentation |
| Fix | Bug fixes, resolving mismatches between design docs and implementation |
| Remove | Deleting files or features |
| Bump | Updating version numbers |

### Examples

```
Add job execution time limit (activeDeadlineSeconds) to design docs

As a countermeasure against starvation of jobs requesting large resources
under BestEffortFIFO, introduce a job execution time limit.

Co-Authored-By: <model name> <noreply@anthropic.com>
```

```
Implement gap filling dispatch logic (#2)

Add stalled job detection and gap-filling filtering.

Co-Authored-By: <model name> <noreply@anthropic.com>
```

## 3. Pull Requests

- Title should be short (under 70 characters)
- The body should include `## Summary` (bullet points) and `## Test plan` (checklist)
- If manual steps are required after applying the changes, add a `## Post-apply actions` section
- If closing an issue, include `Closes #<issue-number>` in the body

## 4. Direct Commits to main

The following cases may be committed directly to main without creating an issue, branch, or PR.

- Minor documentation fixes (typos, structural changes, changes not involving design modifications)
- Adding tests (without functional changes)
- Adjusting configuration values
