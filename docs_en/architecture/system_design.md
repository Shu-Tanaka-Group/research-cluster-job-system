> *This document was auto-translated from the [Japanese original](../../docs/architecture/system_design.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# System Design

## 1. List of Features Required to Realize the Desired Functionality

The following features are required to realize this system.

### 1.1 CLI Features

- `cjob add`
- `cjob sweep`
- `cjob list`
- `cjob status`
- `cjob cancel`
- `cjob hold`
- `cjob release`
- `cjob set`
- `cjob delete`
- `cjob usage`
- `cjob reset`
- `cjob logs` (including `--follow` / `--index` options)
- `cjob flavor`
- `cjob update`
- `cjob config`

### 1.2 Submit Features

- Retrieve current working directory
- Retrieve exported environment variables (excluding variables specified in the user configuration file's `env.exclude`)
- Retrieve container image name (obtained from the `CJOB_IMAGE` environment variable, falling back to `JUPYTER_IMAGE` if not set)
- Save command string
- Resolve user namespace (obtained from the ServiceAccount namespace file)
- Check the total job count limit per namespace (sum of QUEUED / DISPATCHING / DISPATCHED / HELD / CANCELLED; RUNNING is excluded from the count as it is limited by `DISPATCH_BUDGET_PER_NAMESPACE`)
- Issue job ID
- Register job in the internal DB (saved in QUEUED state)

### 1.3 Dispatcher Features

- Periodically scan the DB to retrieve QUEUED jobs
- Calculate dispatch budget per `(namespace, flavor)` unit
- Fair scheduling across namespaces using DRF (Dominant Resource Fairness) (consumption-based priority control + round-robin)
- Gap filling to dispatch small jobs while large jobs are waiting
- Pre-check ResourceQuota before dispatch
- Generate Kubernetes Jobs (including `nodeSelector` configuration based on the flavor's `label_selector`)
- Update DB state on Job creation success or failure
- Delayed retry on transient K8s errors (managed with `retry_after` timestamp)
- Reset DISPATCHING state on startup

### 1.4 Kubernetes Execution Features

- Create a Job using the image obtained at submit time (`CJOB_IMAGE` → `JUPYTER_IMAGE`)
- Mount PVC at `${WORKSPACE_MOUNT_PATH}` (default `/home/jovyan`)
- Set `workingDir` to the cwd at submit time
- Inject environment variables captured at submit time into the container `env`
- Execute the command with `/bin/bash -lc "<saved command>"`
- Write logs to PVC using tee
- Attach Kueue queue labels

### 1.5 Monitoring / State Synchronization Features

- Monitor Job / Pod state
- Update DB state
- Determine completion / failure
- Detect orphan Jobs
- Apply cancellations
- Manage jobs eligible for retry

## 2. Packages / Technologies Used

### 2.1 Python Packages

- **FastAPI**: For implementing the Submit API
- **SQLAlchemy**: PostgreSQL ORM / DB access
- **psycopg**: PostgreSQL driver
- **kubernetes**: For creating Kubernetes Jobs and monitoring state
- **Pydantic**: For defining API request / response schemas

### 2.2 Middleware

- **PostgreSQL**
- **Kubernetes**
- **Kueue**

### 2.3 Rust Crates (cjob CLI)

- **clap**: CLI argument parsing
- **reqwest**: HTTP client (communication with the Submit API)
- **tokio**: Async runtime (real-time log tailing for `--follow`)
- **serde / serde_json**: JSON serialization / deserialization

## 3. Implementation Policy for Required Features

### 3.1 Overall Policy

The system adopts a DB-scan Dispatcher + Kueue + Kubernetes Job architecture.
Argo Workflows is not used in this system. The reasons are as follows.

- The goal is to build a job queue system, not a workflow engine
- Although Argo supports queued workflows, it still creates a large number of Kubernetes CRs

### 3.2 Dispatcher Implementation Policy

The Dispatcher periodically scans PostgreSQL to select QUEUED jobs and creates Kubernetes Jobs.
RabbitMQ is not used.

The reasons for this choice are as follows.

- All users' jobs can always be viewed from a global perspective for scheduling (same approach as Slurm)
- Jobs from users with insufficient budget do not block other users
- Fair scheduling is possible while preserving each user's submission order (`created_at` ascending)
- Complex MQ configurations such as DLQ, ack/nack, and prefetch_count are unnecessary
- Retries on K8s errors can also be managed with the DB's `retry_after` timestamp
- At the expected scale (tens to over a hundred users, thousands to tens of thousands of jobs), DB polling load is not a concern (see [performance.md](performance.md) §6)

### 3.3 State Management Implementation Policy

The authoritative source of job state is stored in **PostgreSQL**.

Reasons:

- Easy to implement `list/status/cancel/logs`
- DB state can be used for dispatch budget decisions
- Easy to reconcile on restart

### 3.4 Execution Control Implementation Policy

The Dispatcher scans the DB and materializes Jobs.

- PostgreSQL: Authoritative source of all job states; basis for scheduling decisions
- Kubernetes Job: Unit of execution
- Kueue: Execution admission control

### 3.5 Job Submission Context Reproduction Policy

The following information captured at submit time is applied to the Job Pod.

- `cwd` → Kubernetes container `workingDir`
- `env` → Kubernetes container `env` (exported environment variables including `PATH` / `VIRTUAL_ENV`; variables excluded by the user configuration's `env.exclude` are not included)
- `command` → `bash -lc "<command>"`

### 3.6 Log Retrieval Policy

The Job Pod command is wrapped with tee to save stdout / stderr to PVC.

- Storage path: `${LOG_BASE_DIR}/<job_id>/stdout.log` and `stderr.log` (`LOG_BASE_DIR` defaults to `/home/jovyan/.cjob/logs`)
- The CLI reads files directly from PVC within the User Pod (log path is obtained from the API)
- Real-time tailing is handled by the CLI performing the equivalent of tail -f
- Log deletion is performed either by `cjob delete` (when deleting individual jobs) or `cjob reset` (when resetting all jobs)

To prevent delays in real-time tailing, `PYTHONUNBUFFERED=1` is set in the Job Pod env to disable Python's stdout buffering. For other languages, users should control flushing as appropriate.

To prevent the Pod from terminating before tee's process substitution completes writing after the user command finishes, `exec >&- 2>&-` closes stdout/stderr file descriptors after command execution, and `wait` waits for the tee process to finish.

#### Path-Related Settings

| Setting | Location | Value | Managed By | Scope | Description |
|---|---|---|---|---|---|
| `WORKSPACE_MOUNT_PATH` | ConfigMap | `/home/jovyan` | Dispatcher | Global | Path where PVC is mounted inside the Job Pod |
| `LOG_BASE_DIR` | ConfigMap | `/home/jovyan/.cjob/logs` | Submit API | Global | Base directory for storing job logs. Must be a path under `WORKSPACE_MOUNT_PATH` |

**Note:** The default value of `LOG_BASE_DIR` is set as a path under `WORKSPACE_MOUNT_PATH`. If `WORKSPACE_MOUNT_PATH` is changed, `LOG_BASE_DIR` must also be updated accordingly. If `LOG_BASE_DIR` points outside `WORKSPACE_MOUNT_PATH`, logs will not be written to PVC and will be lost.

### 3.7 Classifying Compute Resources with ResourceFlavor

Compute nodes in the cluster are classified by **ResourceFlavor** according to their purpose and hardware characteristics. For example, general-purpose CPU nodes, A100 GPU nodes, and H100 GPU nodes are each managed as distinct flavors, allowing users to explicitly select the execution target for their jobs.

#### Design Policy

- **Leverage Kueue's ResourceFlavor mechanism**: Each flavor corresponds to a Kueue `ResourceFlavor` object, and target nodes are determined by `nodeLabels`. The Dispatcher sets the flavor's `label_selector` as the K8s Job's `nodeSelector`, and Kueue selects the matching ResourceFlavor to schedule onto nodes
- **Flavor definitions are managed in configuration**: Defined as a JSON array in the ConfigMap `RESOURCE_FLAVORS`. Each flavor has a name (`name`), a node selection label selector (`label_selector`), and an optional GPU resource name (`gpu_resource_name`). Adding or changing flavors requires only configuration changes, not code changes
- **Users specify using the `--flavor` option**: For example, `cjob add --flavor gpu-a100 --gpu 1 -- python train.py`. When omitted, `DEFAULT_FLAVOR` (default: `cpu`) is used
- **Validation is performed per flavor**: The Submit API validates job resource requests against nodes within the specified flavor. Resources not available in the flavor (e.g., a GPU request for a CPU flavor) are rejected
- **GPU resource names are defined per flavor**: GPU resource names for different vendors, such as `nvidia.com/gpu` (NVIDIA) and `amd.com/gpu` (AMD), are specified in the flavor definition's `gpu_resource_name`. The Dispatcher uses this value when generating K8s Job manifests
- **DRF (Dominant Resource Fairness) is calculated cluster-wide**: Dispatch priority is not separated across flavors; per-namespace resource consumption is normalized against the total cluster capacity. This maintains fairness across flavors

#### Flavor Definition Configuration Example

```json
[
  {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
  {"name": "gpu-a100", "label_selector": "cjob.io/flavor=gpu-a100", "gpu_resource_name": "nvidia.com/gpu"},
  {"name": "gpu-h100", "label_selector": "cjob.io/flavor=gpu-h100", "gpu_resource_name": "nvidia.com/gpu"}
]
```

For detailed flavor design, see [resources.md](resources.md); for integration with Kueue, see [kueue.md](kueue.md); for operational procedures for adding flavors, see [../deployment.md](../deployment.md) §16.3.

## 4. System Configuration

### 4.1 Logical Configuration

```text
User Pod (namespace: user-alice)
  └─ cjob CLI
       └─ HTTP + ServiceAccount JWT
            └─ Submit API (namespace: cjob-system)
                 └─ PostgreSQL (registered in QUEUED state)

Dispatcher (namespace: cjob-system)
  ├─ PostgreSQL (scans for QUEUED jobs)
  └─ Kubernetes API
       └─ Job + Kueue LocalQueue (namespace: user-alice)

Watcher / Reconciler (namespace: cjob-system)
  ├─ Kubernetes API
  └─ PostgreSQL

Kubernetes Job Pod (namespace: user-alice)
  ├─ image = same as User Pod (obtained in order: CJOB_IMAGE → JUPYTER_IMAGE)
  ├─ PVC mounted at ${WORKSPACE_MOUNT_PATH} (default /home/jovyan)
  ├─ workingDir = cwd
  ├─ env = submit-time env
  └─ stdout/stderr → ${LOG_BASE_DIR}/<job_id>/
```

### 4.2 Namespace Configuration

```text
cjob-system        : Submit API / Dispatcher / Watcher / PostgreSQL
<user-namespace>   : User Pod / Job Pod / LocalQueue / ResourceQuota / PVC
```

User namespaces can use any name (e.g., `user-alice`, `lab-physics`).
Identification is done via the label `cjob.io/user-namespace=true`, and the username is obtained from the namespace annotation `cjob.io/username`.

### 4.3 Key Components

| Component | Type | Replicas | Namespace |
|---|---|---|---|
| Submit API | Deployment | 2 or more recommended | cjob-system |
| Dispatcher | Deployment | 1 | cjob-system |
| Watcher / Reconciler | Deployment | 1 | cjob-system |
| PostgreSQL | StatefulSet | 1 | cjob-system |
| Kubernetes Job | Job | - | \<user-namespace\> |

The Dispatcher and Watcher are fixed at 1 replica because multiple replicas would cause double dispatch and double updates.
The Submit API is stateless (the authoritative state is in PostgreSQL, authentication is delegated to K8s TokenReview, and job_id assignment is atomic in the DB), so it is safe to increase the replica count. 2 or more replicas are recommended.

#### Role of Each Component

**cjob CLI**
A command-line tool operated by users inside the User Pod. Sends job submission, listing, status checking, cancellation, log viewing, and other operations as HTTP requests to the Submit API. Distributed as a single Rust binary and not included in the image. Can self-update via the Submit API using the `cjob update` command.

**Submit API**
Accepts requests from the CLI and registers jobs in PostgreSQL in QUEUED state. Validates ServiceAccount JWTs using the K8s TokenReview API and ensures operations are limited to jobs within the user's own namespace. Stateless, so multiple replicas are supported.

**PostgreSQL**
The authoritative source (Single Source of Truth) for all job states. Manages job metadata, state, and execution history. The Dispatcher's scheduling decisions, Submit API validation, and CLI display all reference this.

**Dispatcher**
Periodically scans PostgreSQL to select QUEUED jobs and creates Kubernetes Jobs. Controls dispatch budget and fair scheduling (per-namespace round-robin). Fixed at 1 replica (multiple replicas would cause double dispatch).

**Watcher / Reconciler**
Periodically monitors the Kubernetes API and reflects Job / Pod execution state into PostgreSQL. Handles transitions to SUCCEEDED / FAILED, deletion of K8s Jobs for CANCELLED jobs, and DELETING cleanup during reset. Fixed at 1 replica, same as the Dispatcher.

**Kubernetes Job / Job Pod**
The unit of execution created by the Dispatcher. The Job Pod reproduces and executes the user's submission-time environment (image, cwd, env, command) and writes stdout / stderr to the log directory on PVC. Uses the same image as the User Pod.

**Kueue**
Handles admission control for Kubernetes Jobs. Manages cluster-wide resource limits via ClusterQueue and enables fair resource sharing among users via BestEffortFIFO. Preemption is disabled and running jobs are not forcibly terminated.
