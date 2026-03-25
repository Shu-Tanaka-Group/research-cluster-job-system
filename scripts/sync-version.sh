#!/usr/bin/env bash
# Sync VERSION file to pyproject.toml and Cargo.toml.
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

# Update Cargo.toml (only the [package] version, not dependency versions)
CARGO="$REPO_ROOT/cli/Cargo.toml"
if [ -f "$CARGO" ]; then
    # Match only the version line in the first 5 lines ([package] section)
    current=$(head -5 "$CARGO" | grep -E '^version = "' | sed 's/version = "\(.*\)"/\1/')
    if [ "$current" != "$VERSION" ]; then
        # Replace only the first occurrence of version = "..."
        sed -i.bak "0,/^version = \".*\"/{s/^version = \".*\"/version = \"$VERSION\"/}" "$CARGO"
        rm -f "$CARGO.bak"
        echo "Updated $CARGO: $current -> $VERSION"
        changed=1
    fi
fi

if [ "$changed" -eq 1 ]; then
    # Stage the updated files so they're included in the commit
    git add "$PYPROJECT" "$CARGO" 2>/dev/null || true
fi
