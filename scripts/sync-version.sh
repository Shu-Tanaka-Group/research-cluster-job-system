#!/usr/bin/env bash
# Sync VERSION file to pyproject.toml, Cargo.toml, and kustomization.yaml.
# Idempotent: does nothing if versions already match.

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

# Validate semver format (basic check)
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+'; then
    echo "error: VERSION '$VERSION' is not valid semver" >&2
    exit 1
fi

changed=0

# Update pyproject.toml
PYPROJECT="$REPO_ROOT/server/pyproject.toml"
if [ -f "$PYPROJECT" ]; then
    current=$(grep -E '^version = "' "$PYPROJECT" | head -1 | sed 's/version = "\(.*\)"/\1/')
    if [ "$current" != "$VERSION" ]; then
        sed -i.bak "s/^version = \".*\"/version = \"$VERSION\"/" "$PYPROJECT"
        rm -f "$PYPROJECT.bak"
        echo "Updated $PYPROJECT: $current -> $VERSION"
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
