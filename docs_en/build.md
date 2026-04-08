> *This document was auto-translated from the [Japanese original](../docs/build.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Build Instructions

## Overview

CJob's server-side components consist of 3 Docker images.
All are built using the `server/` directory as the build context.

| Image | Dockerfile | Purpose |
|---|---|---|
| `your-registry/cjob-submit-api` | `server/Dockerfile.api` | Submit API |
| `your-registry/cjob-dispatcher` | `server/Dockerfile.dispatcher` | Dispatcher |
| `your-registry/cjob-watcher` | `server/Dockerfile.watcher` | Watcher |

All base images are `python:3.12-slim`.

## Prerequisites

- Docker is installed
- Push permission to the image registry (when pushing)

## Build

Run from the repository root.

```bash
# Submit API
docker build -t your-registry/cjob-submit-api:latest -f server/Dockerfile.api server/

# Dispatcher
docker build -t your-registry/cjob-dispatcher:latest -f server/Dockerfile.dispatcher server/

# Watcher
docker build -t your-registry/cjob-watcher:latest -f server/Dockerfile.watcher server/
```

### Building with Version Tags

```bash
read -r VERSION < VERSION

docker build -t your-registry/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t your-registry/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t your-registry/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/
```

## Pushing to Registry

```bash
docker push your-registry/cjob-submit-api:latest
docker push your-registry/cjob-dispatcher:latest
docker push your-registry/cjob-watcher:latest

# With version tags
docker push your-registry/cjob-submit-api:${VERSION}
docker push your-registry/cjob-dispatcher:${VERSION}
docker push your-registry/cjob-watcher:${VERSION}
```

## Building the CLI (Rust)

The CLI is built as a single binary, not a Docker image.
Users download it from GitHub Releases and place it in `/home/jovyan/.local/bin/`.

### Local Build

```bash
cd cli/
cargo build --release
```

The build artifact is generated at `cli/target/release/cjob`.

### Cross-Compilation (macOS → Linux)

When the development machine is macOS, cross-compilation is required to generate a Linux binary that runs inside K8s Pods.

The `ring` crate, which the TLS backend (rustls) of `reqwest` depends on, contains C/assembly code, so simply specifying `--target x86_64-unknown-linux-gnu` will fail to build. Use one of the following methods.

#### Method 1: musl Target + Cross-Compiler (Recommended)

This method generates a statically linked binary. Since it does not depend on glibc, it works on any Linux distribution.

```bash
# Install musl cross-compiler (first time only)
brew install filosottile/musl-cross/musl-cross
rustup target add x86_64-unknown-linux-musl

# Configure linker (first time only)
mkdir -p cli/.cargo
cat > cli/.cargo/config.toml << 'EOF'
[target.x86_64-unknown-linux-musl]
linker = "x86_64-linux-musl-gcc"
EOF

# Build
cd cli/
cargo build --release --target x86_64-unknown-linux-musl
```

The build artifact is generated at `cli/target/x86_64-unknown-linux-musl/release/cjob`.

#### Method 2: Using cross

[cross](https://github.com/cross-rs/cross) is a tool that performs cross-compilation inside Docker containers. The `docker` command must be available and the Docker daemon must be running.

> **Note**: On Apple Silicon Macs, as of `cross` 0.2.5 there is a known issue where host toolchain resolution fails (errors when trying to install `stable-x86_64-unknown-linux-gnu`). In that case, use Method 1.

```bash
# Install cross (first time only)
cargo install cross

# Build (Docker must be running)
cd cli/
cross build --release --target x86_64-unknown-linux-gnu
```

The build artifact is generated at `cli/target/x86_64-unknown-linux-gnu/release/cjob`.

## Building the Admin CLI (cjobctl)

`cjobctl` is an administrative CLI that runs on the administrator's local PC. It connects directly to the DB and uses the K8s API.

### Build

```bash
cd ctl/
cargo build --release
```

The build artifact is generated at `ctl/target/release/cjobctl`.

### Configuration

Create `~/.config/cjobctl/config.toml`.

```toml
[database]
database = "cjob"
user = "cjob"
password = "xxx"

[kubernetes]
namespace = "cjob-system"
```

When executing DB commands, `kubectl port-forward` is automatically started and stopped (`kubectl` must be in PATH).

```bash
kubectl port-forward svc/postgres 5432:5432 -n cjob-system
```

## Job Pod Runtime Image

The runtime image used by Job Pods (e.g., `your-registry/cjob-jupyter:2.1.0`) is not managed by this repository.
The CLI obtains the image name from the `CJOB_IMAGE` environment variable. If `CJOB_IMAGE` is not set, it falls back to `JUPYTER_IMAGE`. If neither is set, an error occurs.

Normally, `JUPYTER_IMAGE` is automatically set in JupyterHub User Pods, so Job Pods run with the same image as the User Pod. To run Jobs with a different image from the User Pod, explicitly set `CJOB_IMAGE`.
