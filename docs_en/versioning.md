> *This document was auto-translated from the [Japanese original](../docs/versioning.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Version Management

## Overview

CJob manages the entire project version with a single `VERSION` file. By updating the `VERSION` file and running the sync script, the versions of each component are aligned all at once.

## Version Management Mechanism

| File | Role |
|---|---|
| `VERSION` | The authoritative source of the project version (a single semver string) |
| `scripts/sync-version.sh` | Syncs the value in `VERSION` to the configuration files of each component |

### Sync Targets

`scripts/sync-version.sh` updates the `version` field in the following files.

| File | Component |
|---|---|
| `server/pyproject.toml` | Submit API / Dispatcher / Watcher |
| `cli/Cargo.toml` | cjob CLI |
| `ctl/Cargo.toml` | cjobctl |
| `k8s/overlay-example/kustomization.yaml` | Image tag in the overlay example |

## Version Update Procedure

Steps 1-7 below can be executed with the `/release` skill. The skill asks the user to confirm the version number, the link-list summary line, and any push, and delegates Step 4 to the `/deploy-runbook` skill. Creating and pushing the tag (the end of Step 7) is irreversible and is not performed by the skill.

### Step 1: Update the VERSION File

```bash
echo "X.Y.Z" > VERSION
```

### Step 2: Sync Version to Each Component

```bash
bash scripts/sync-version.sh
```

`sync-version.sh` is idempotent — it does nothing if the versions already match.

### Step 3: Update Lock Files

Reflect the version number change in the lock files.

```bash
# CLI
cd cli/ && cargo update -p cjob && cd ..

# Admin CLI
cd ctl/ && cargo update -p cjobctl && cd ..

# Server
cd server/ && uv lock && cd ..
```

Each of these rewrites only the version line of its own package. Do not use `cargo generate-lockfile`: it regenerates the entire lock file and updates every dependency to the latest compatible version. Update dependencies in a separate PR so they are not mixed into the release commit.

### Step 4: Check for Missing Migration Procedures

Review the diff from the previous version tag and verify that no migration procedures are missing from `docs/migration/unreleased.md`.

The `/deploy-runbook` skill automates these checks (deriving required work from the diff, cross-checking it against `unreleased.md`, and reporting omissions).

```bash
# Check diff from previous version tag to current
git diff <old-tag>..HEAD --stat

# Focus especially on the following changes
git diff <old-tag>..HEAD -- k8s/base/configmap-cjob-config.yaml  # ConfigMap key additions/changes
git diff <old-tag>..HEAD -- server/src/cjob/models.py            # DB schema changes
git diff <old-tag>..HEAD -- docs/architecture/kueue.md           # Kueue resource changes
git diff <old-tag>..HEAD -- docs/deployment.md                   # Deployment procedure changes
```

If any of the following changes are present, add migration procedures to `docs/migration/unreleased.md` (create the file if it does not exist):

- ConfigMap key additions or default value changes (need to be reflected in overlay)
- DB schema changes (requires running `cjobctl db migrate`)
- Kueue resource (ResourceFlavor / ClusterQueue) configuration changes
- Node label or Taint changes
- RBAC or Kyverno policy changes
- Any other changes requiring manual configuration changes or data migration

**Section format:** Immediately after each heading, add a one-line reference to the change that made the step necessary.

```markdown
## <Step heading>

> Related: issue #<number>
```

The deployment cross-check (`/deploy-runbook` skill) uses this to map each section of `unreleased.md` to the actual changes mechanically. The PR number is not required because it can be traced from `Closes #<number>` in the PR body, but it may be added as `> Related: issue #<number> / PR #<number>` once known. For a change with no issue, use `> Related: PR #<number>`; for a direct commit with no PR either, use `> Related: <commit hash>`.

### Step 5: Rename the Migration Guide

If `docs/migration/unreleased.md` contains specific migration procedures, update the title at the top of the file, remove the instructions about creating `unreleased.md`, and rename the file to the version name.

```bash
mv docs/migration/unreleased.md docs/migration/vX.Y.Z.md
mv docs_en/migration/unreleased.md docs_en/migration/vX.Y.Z.md
```

**Process the Japanese and English versions the same way.** Dropping the `docs_en/` side leaves the previous version's content sitting in the English `unreleased.md` indefinitely.

Add a `vX.Y.Z` row to the "version-specific migration procedures" link list at the end of both `docs/migration.md` and `docs_en/migration.md` (there is no link to `unreleased` to begin with, so this is an addition rather than a replacement). Summarize that version's main migration work in one line.

After renaming, create a new `docs/migration/unreleased.md` and `docs_en/migration/unreleased.md` using the following template.

````markdown
# Unreleased Migration Procedures

This file is a working file for migration procedures intended for the **next release**. At release time, rename it to the version name (e.g., `v1.11.0.md`) and create a new `unreleased.md` (see [versioning.md](../versioning.md)).

If there are migration procedures specific to the next release in addition to the [standard migration procedures](../migration.md), add them below.
````

For the English template, reuse the existing header of `docs_en/migration/unreleased.md` as-is (including the auto-translation notice).

If `unreleased.md` has no content (no significant changes), you may skip Step 5 entirely (rename and recreate).

### Step 6: Commit

Bundle the version update into a single commit. Files to include:

- `VERSION`
- `server/pyproject.toml`
- `cli/Cargo.toml`
- `cli/Cargo.lock`
- `ctl/Cargo.toml`
- `ctl/Cargo.lock`
- `server/uv.lock`
- `k8s/overlay-example/kustomization.yaml`
- `docs/migration/vX.Y.Z.md` / `docs_en/migration/vX.Y.Z.md` (if renamed)
- `docs/migration/unreleased.md` / `docs_en/migration/unreleased.md` (if recreated from template)
- `docs/migration.md` / `docs_en/migration.md` (if links were updated)

### Step 7: Release Branch, PR, and Tag

Put the Step 6 commit on a `release/vX.Y.Z` branch, create a PR, merge it, and then create the tag (see [git_conventions.md](git_conventions.md) §1).

```bash
git checkout -b release/vX.Y.Z
# Perform Steps 1-6 and commit
git push -u origin release/vX.Y.Z
```

Create the PR with the `/create-pr` skill. Title it `Bump version to X.Y.Z` and include the following in the body.

- `## Summary` — main changes since the previous version, with issue numbers
- `## Post-apply actions` — a summary of `docs/migration/vX.Y.Z.md`, linking to that file for details
- `## Test plan` — idempotency of `sync-version.sh`, version consistency across files, lock file updates, migration guide rename and link addition, and the return of `unreleased.md` to its template state

After merging, update `main` and tag the merge commit, then push. **The tag name has no `v` prefix** (the branch name has `v`, the tag name does not).

```bash
git checkout main && git pull
git tag X.Y.Z
git push origin X.Y.Z
```

Pushing the tag triggers [`.github/workflows/release.yml`](../.github/workflows/release.yml), which builds the cjob CLI (`cjob-linux-x86_64`) and creates a GitHub Release. Pre-releases (`X.Y.Z-alpha.N` / `-beta.N` / `-rc.N`) are automatically treated as prereleases and are not marked latest.

Pushing the tag is an irreversible operation that automatically generates a GitHub Release. Confirm that the PR is merged and that the tag name matches the contents of `VERSION` before running it.

## Notes

- Version format follows [Semantic Versioning](https://semver.org/)
- `sync-version.sh` can also be used as a pre-commit hook (see [Git Conventions](git_conventions.md))
- For migration tasks after a version update (build, deploy, DB migration, etc.), refer to the [Version Migration Guide](migration.md)
