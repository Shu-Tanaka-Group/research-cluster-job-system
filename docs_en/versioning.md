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
cd cli/ && cargo generate-lockfile && cd ..

# Admin CLI
cd ctl/ && cargo generate-lockfile && cd ..

# Server
cd server/ && uv lock && cd ..
```

### Step 4: Check for Missing Migration Steps

Review the diff from the previous version tag and verify that no migration steps are missing from `docs/migration/unreleased.md`.

```bash
# Check diff from previous version tag to current
git diff <old-tag>..HEAD --stat

# Focus especially on the following changes
git diff <old-tag>..HEAD -- k8s/base/configmap-cjob-config.yaml  # ConfigMap key additions/changes
git diff <old-tag>..HEAD -- server/src/cjob/models.py            # DB schema changes
git diff <old-tag>..HEAD -- docs/architecture/kueue.md           # Kueue resource changes
git diff <old-tag>..HEAD -- docs/deployment.md                   # Deployment procedure changes
```

If any of the following changes are present, add migration steps to `docs/migration/unreleased.md` (create the file if it does not exist):

- ConfigMap key additions or default value changes (need to be reflected in overlay)
- DB schema changes (requires running `cjobctl db migrate`)
- Kueue resource (ResourceFlavor / ClusterQueue) configuration changes
- Node label or Taint changes
- RBAC or Kyverno policy changes
- Any other changes requiring manual configuration changes or data migration

### Step 5: Rename the Migration Guide

If `docs/migration/unreleased.md` contains specific migration steps, update the title at the top of the file, remove the instructions about creating `unreleased.md`, and rename the file to the version name.

```bash
mv docs/migration/unreleased.md docs/migration/vX.Y.Z.md
```

Also update the link at the end of `docs/migration.md` (`unreleased` → `vX.Y.Z`).

After renaming, create a new `docs/migration/unreleased.md` using the following template.

````markdown
# Unreleased Migration Steps

This file is a working file for migration steps intended for the **next release**. At release time, rename it to the version name (e.g., `v1.11.0.md`) and create a new `unreleased.md` (see [versioning.md](../versioning.md)).

If there are migration steps specific to the next release in addition to the [standard migration steps](../migration.md), add them below.
````

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
- `docs/migration/vX.Y.Z.md` (if renamed)
- `docs/migration/unreleased.md` (if recreated from template)
- `docs/migration.md` (if link was updated)

## Notes

- Version format follows [Semantic Versioning](https://semver.org/)
- `sync-version.sh` can also be used as a pre-commit hook (see [Git Conventions](git_conventions.md))
- For migration tasks after a version update (build, deploy, DB migration, etc.), refer to the [Version Migration Guide](migration.md)
