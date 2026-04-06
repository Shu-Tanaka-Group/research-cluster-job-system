#!/usr/bin/env bash
# Sync VERSION file to pyproject.toml, Cargo.toml, and kustomization.yaml.
# Idempotent: does nothing if versions already match.
#
# VERSION file uses SemVer format: X.Y.Z or X.Y.Z-pre.N
# Supported pre-release tags: alpha, beta, rc (e.g., 1.12.0-beta.1)
#
# pyproject.toml uses PEP 440 format, so pre-release versions are converted:
#   1.12.0-alpha.1 -> 1.12.0a1
#   1.12.0-beta.1  -> 1.12.0b1
#   1.12.0-rc.1    -> 1.12.0rc1

set -euo pipefail

# Resolve symlinks to find the real script location, then derive repo root
SCRIPT_PATH="$(realpath "$0")"
REPO_ROOT="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
VERSION_FILE="$REPO_ROOT/VERSION"

if [ ! -f "$VERSION_FILE" ]; then
    echo "error: VERSION file not found" >&2
    exit 1
fi

VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"

if [ -z "$VERSION" ]; then
    echo "error: VERSION file is empty" >&2
    exit 1
fi

# Validate semver format: X.Y.Z or X.Y.Z-{alpha,beta,rc}.N
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-((alpha|beta|rc)\.[0-9]+))?$'; then
    echo "error: VERSION '$VERSION' is not valid semver" >&2
    echo "  expected: X.Y.Z or X.Y.Z-{alpha,beta,rc}.N (e.g., 1.12.0, 1.12.0-beta.1)" >&2
    exit 1
fi

# Convert SemVer pre-release to PEP 440 for pyproject.toml
# 1.12.0-alpha.1 -> 1.12.0a1, 1.12.0-beta.1 -> 1.12.0b1, 1.12.0-rc.1 -> 1.12.0rc1
pep440_version() {
    local ver="$1"
    echo "$ver" | sed -E 's/-alpha\./a/; s/-beta\./b/; s/-rc\./rc/'
}

PEP440_VERSION="$(pep440_version "$VERSION")"

changed=0

# Update pyproject.toml (uses PEP 440 format)
PYPROJECT="$REPO_ROOT/server/pyproject.toml"
if [ -f "$PYPROJECT" ]; then
    current=$(grep -E '^version = "' "$PYPROJECT" | head -1 | sed 's/version = "\(.*\)"/\1/')
    if [ "$current" != "$PEP440_VERSION" ]; then
        sed -i.bak "s/^version = \".*\"/version = \"$PEP440_VERSION\"/" "$PYPROJECT"
        rm -f "$PYPROJECT.bak"
        echo "Updated $PYPROJECT: $current -> $PEP440_VERSION"
        changed=1
    fi
fi

# Update cli/Cargo.toml (only the [package] version, not dependency versions)
CARGO_CLI="$REPO_ROOT/cli/Cargo.toml"
if [ -f "$CARGO_CLI" ]; then
    # Match only the version line in the first 5 lines ([package] section)
    current=$(head -5 "$CARGO_CLI" | grep -E '^version = "' | sed 's/version = "\(.*\)"/\1/')
    if [ "$current" != "$VERSION" ]; then
        # Replace only the first occurrence of version = "..."
        sed -i.bak "0,/^version = \".*\"/{s/^version = \".*\"/version = \"$VERSION\"/}" "$CARGO_CLI"
        rm -f "$CARGO_CLI.bak"
        echo "Updated $CARGO_CLI: $current -> $VERSION"
        changed=1
    fi
fi

# Update ctl/Cargo.toml (only the [package] version, not dependency versions)
CARGO_CTL="$REPO_ROOT/ctl/Cargo.toml"
if [ -f "$CARGO_CTL" ]; then
    current=$(head -5 "$CARGO_CTL" | grep -E '^version = "' | sed 's/version = "\(.*\)"/\1/')
    if [ "$current" != "$VERSION" ]; then
        sed -i.bak "0,/^version = \".*\"/{s/^version = \".*\"/version = \"$VERSION\"/}" "$CARGO_CTL"
        rm -f "$CARGO_CTL.bak"
        echo "Updated $CARGO_CTL: $current -> $VERSION"
        changed=1
    fi
fi

# Update k8s/overlay-example/kustomization.yaml (newTag fields)
KUSTOMIZATION="$REPO_ROOT/k8s/overlay-example/kustomization.yaml"
if [ -f "$KUSTOMIZATION" ]; then
    current=$(grep -m1 'newTag:' "$KUSTOMIZATION" | sed 's/.*newTag: *"\(.*\)"/\1/')
    if [ "$current" != "$VERSION" ]; then
        sed -i.bak "s/newTag: \".*\"/newTag: \"$VERSION\"/g" "$KUSTOMIZATION"
        rm -f "$KUSTOMIZATION.bak"
        echo "Updated $KUSTOMIZATION: $current -> $VERSION"
        changed=1
    fi
fi

if [ "$changed" -eq 1 ]; then
    # Stage the updated files so they're included in the commit
    git add "$PYPROJECT" "$CARGO_CLI" "$CARGO_CTL" "$KUSTOMIZATION" 2>/dev/null || true
fi
