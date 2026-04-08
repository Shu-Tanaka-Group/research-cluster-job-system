> *This document was auto-translated from the [Japanese original](../../docs/architecture/prerequisites.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Environment Prerequisites

## 1. Infrastructure Prerequisites

This system is built on the following assumptions.

- A Kubernetes cluster exists
- Namespaces are isolated per user (created manually or automated via scripts)
- A working PVC exists per user namespace
- The PVC mount path defaults to `/home/jovyan` and can be changed via the `WORKSPACE_MOUNT_PATH` key in ConfigMap
- Kueue is deployed to the Kubernetes cluster
- PostgreSQL is used for state management (new deployment)
- A ReadWriteMany-capable StorageClass is installed (e.g., NFS subdir external provisioner)
- Nodes dedicated to the job queue system are labeled `cjob.io/flavor=<flavor-name>` and tainted with `role=computing:NoSchedule`
- Expected scale: currently 10 users and 2 nodes. The operation model adds nodes proportionally to users, supporting up to 100–150 users for long-running job-centric workloads (see [performance.md](performance.md) §6 for details)

## 2. Execution Environment Prerequisites

- **The Pod that submits jobs and the Pod that executes jobs use the same image**
- The image is automatically obtained from the User Pod's environment variable `CJOB_IMAGE` (without explicit user specification)
- If `CJOB_IMAGE` is not set, it falls back to `JUPYTER_IMAGE` (for backward compatibility with JupyterHub environments). If both are unset, the CLI returns an error
- JupyterHub User Pods have `JUPYTER_IMAGE` set to the current container image name
- The `cjob` CLI is implemented in Rust as a single binary and distributed via GitHub Releases
- Users place the CLI binary in their own home directory (e.g., `/home/jovyan/.local/bin/`)
- The CLI is not included in the image
- The base OS is arbitrary (`/bin/bash` must be available; e.g., Ubuntu 24.04)
- The PVC name matches the username
- The execution shell defaults to `/bin/bash -lc`
- The working directory is restricted to under `${WORKSPACE_MOUNT_PATH}`
- Only exported environment variables are reproduced (including `PATH` / `VIRTUAL_ENV` for virtual environments, excluding variables specified in the user's `env.exclude` configuration)
- Shell functions, aliases, and shell options are not reproduced
- Users create and manage Python virtual environments under `${WORKSPACE_MOUNT_PATH}`
- Since the Job Pod and User Pod use the same image, compatibility of C extension libraries inside venv is maintained

## 3. Scheduling Prerequisites

- Kubernetes Jobs are the unit of execution
- Kueue handles admission, queueing, and fairness
- ResourceQuota is used as a safety net to prevent unintended unlimited consumption due to bugs per namespace (fairness is handled by Kueue's BestEffortFIFO)
- The Dispatcher controls the number of Jobs submitted to Kueue
