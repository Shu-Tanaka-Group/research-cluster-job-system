> *This document was auto-translated from the [Japanese original](../../docs/architecture/prerequisites.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Environment Prerequisites

## 1. Infrastructure Prerequisites

This system is built on the following assumptions.

- A Kubernetes cluster exists (v1.26 or later. The `status.ready` field that the Watcher uses for Job state determination is stably available since v1.26, when the `JobReadyPods` feature reached GA. See [watcher.md](watcher.md) §3.)
- Namespaces are isolated per user (created manually or automated via scripts)
- A working PVC exists per user namespace
- The PVC mount path defaults to `/home/jovyan` and can be changed via the `WORKSPACE_MOUNT_PATH` key in ConfigMap
- Kueue is deployed to the Kubernetes cluster
- PostgreSQL is used for state management (new deployment)
- A ReadWriteMany-capable StorageClass is installed (e.g., NFS subdir external provisioner)
- Nodes dedicated to the job queue system are labeled `cjob.io/flavor=<flavor-name>` and tainted with `role=computing:NoSchedule`
- Expected scale: currently 10 users and 2 nodes. The operation model adds nodes proportionally to users, supporting up to 100–150 users for long-running job-centric workloads (see [performance.md](performance.md) §6 for details)

## 2. Execution Environment Prerequisites

- **By default, the Pod that executes jobs uses the same image as the Pod that submits them.** When the flavor has a default image set, or when the user specifies one explicitly with `cjob add --image`, that takes precedence (see §2.1)
- The submitting Pod's image is automatically obtained from the User Pod's environment variable `CJOB_IMAGE`, falling back to `JUPYTER_IMAGE` if it is not set (for backward compatibility with JupyterHub environments). Even when both are unset, jobs can be submitted as long as the image can be resolved from the flavor default image or `--image`
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
- As long as the Job Pod and User Pod use the same image, compatibility of C extension libraries inside venv is maintained. When a different image is used, the prerequisites in §2.1 must be satisfied

### 2.1 Prerequisites When the Job Pod and Submitting Pod Use Different Images

Using a flavor default image (`RESOURCE_FLAVORS` in [resources.md](resources.md)) or `cjob add --image` can make the Job Pod's image differ from the submitting Pod's. In that case, only images satisfying the following condition may be used.

- **The image must derive from the same base as the submitting Pod's image, with a matching Python version and installation path**

The venv on the PVC is built in the submitting User Pod, and the `VIRTUAL_ENV` / `PATH` collected at submit time are reproduced in the Job Pod. The venv's `pyvenv.cfg` points to the system Python path via `home`, so unless that path is valid in both images, the venv breaks on the Job Pod side. ABI compatibility of C extension libraries likewise depends on the bases matching.

Typical cases that break this premise are setting a flavor default image whose base OS or distribution differs (e.g., an Alpine-based execution image against an Ubuntu-based submitting Pod), or whose Python minor version differs. An image that merely adds libraries on top of the same base, such as one with or without the CUDA runtime, satisfies this condition.

For the operational procedure when setting a flavor default image, see [operations.md](../operations.md) §8.

## 3. Scheduling Prerequisites

- Kubernetes Jobs are the unit of execution
- Kueue handles admission, queueing, and fairness
- ResourceQuota is used as a safety net to prevent unintended unlimited consumption due to bugs per namespace (fairness is handled by Kueue's BestEffortFIFO)
- The Dispatcher controls the number of Jobs submitted to Kueue
