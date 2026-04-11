> *This document was auto-translated from the [Japanese original](../../docs/architecture/cjobctl.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# cjobctl Design

## 1. Overview

`cjobctl` is an administrator CLI tool for the CJob system. It runs on the administrator's local PC and performs system status checks and configuration changes through direct PostgreSQL connections and the Kubernetes API.

While the user-facing CLI `cjob` communicates via the Submit API, `cjobctl` directly accesses the DB and K8s API.

```
Administrator PC
├── cjobctl ──→ PostgreSQL (via kubectl port-forward, automatic)
└── cjobctl ──→ Kubernetes API (via kubeconfig)
```

## 2. Technology Stack

| Item | Technology |
|---|---|
| Language | Rust |
| CLI Framework | Clap (derive) |
| DB Client | tokio-postgres |
| K8s Client | kube + k8s-openapi |
| Async Runtime | tokio |
| Config File | TOML (toml crate) |

## 3. Connection Methods

### 3.1 DB Connection

`kubectl port-forward` is automatically started when executing DB commands. The local port is auto-assigned by the OS (port 0 specification) to avoid conflicts with existing processes. The port-forward process is automatically terminated when the command completes.

```
cjobctl → kubectl port-forward (auto-start, random port)
        → 127.0.0.1:<random> → svc/postgres:5432
        → Connect with tokio-postgres
        → Command complete → kill port-forward process
```

Prerequisite: `kubectl` must be in PATH and the cluster must be accessible via kubeconfig.

### 3.2 K8s Connection

The client is automatically configured from kubeconfig via `kube::Client::try_default()`. No port-forward is required.

## 4. Configuration File

`~/.config/cjobctl/config.toml`:

```toml
[database]
database = "cjob"
user = "cjob"
password = "xxx"

[kubernetes]
namespace = "cjob-system"   # Default when omitted
```

`host` / `port` are managed by automatic port-forward and do not need to be configured.

## 5. Command List

### 5.1 DB Status Checks

| Command | Description | Target Table |
|---|---|---|
| `cjobctl jobs list [--namespace <ns>] [--status <s>] [--sort <field>] [--reverse] [-o wide]` | Job list | `jobs` |
| `cjobctl jobs status --namespace <ns> --job-id <id>` | Detailed display of individual job (equivalent to `cjob status`) | `jobs` |
| `cjobctl jobs summary` | Job count by namespace × status (pivot table) | `jobs` |
| `cjobctl jobs stalled [--sort <field>] [--reverse]` | Jobs stuck in DISPATCHED state | `jobs` |
| `cjobctl jobs remaining [--sort <field>] [--reverse]` | Remaining time for RUNNING jobs | `jobs` |
| `cjobctl jobs cancel --namespace <ns> [--job-id <id> \| --status <s> \| --all]` | Cancel jobs | `jobs` |
| `cjobctl jobs counters` | job_id counter per namespace | `user_job_counters` |

#### Sort Options

`jobs list`, `jobs stalled`, and `jobs remaining` support the `--sort` option to change the sort field. Combine with `--reverse` for descending order.

| Command | Available Sort Fields | Default |
|---|---|---|
| `jobs list` | `NAMESPACE`, `CREATED`, `DISPATCHED`, `STARTED`, `FINISHED` | `NAMESPACE` (composite order of namespace, job_id) |
| `jobs stalled` | `NAMESPACE`, `CREATED` | `CREATED` (dispatched_at ascending) |
| `jobs remaining` | `NAMESPACE`, `CREATED` | `REMAINING` (remaining_sec ascending) |

Specifying `--sort FINISHED`, `--sort DISPATCHED`, or `--sort STARTED` with `stalled` / `remaining` results in an error (the corresponding column does not exist).

#### `-o wide` Option

Display columns for `jobs list` are NAMESPACE, JOB_ID, TYPE, STATUS, FLAVOR, COMMAND, CREATED, FINISHED. TYPE displays `job` for jobs where `completions IS NULL` and `sweep` for others.

When `-o wide` (`--output wide`) is specified, the following columns are added:

- **DISPATCHED**: Job dispatch time (DB `dispatched_at` column, `-` when NULL)
- **STARTED**: Job start time (DB `started_at` column, `-` when NULL)
- **CPU**: Specified CPU resource amount (DB `cpu` column)
- **MEMORY**: Specified memory resource amount (DB `memory` column)
- **GPU**: Specified GPU count (DB `gpu` column, `-` when 0)
- **NODE**: Job execution node name (DB `node_name` column, `-` when NULL)

Column order with `-o wide`: NAMESPACE, JOB_ID, TYPE, STATUS, FLAVOR, COMMAND, CREATED, DISPATCHED, STARTED, FINISHED, CPU, MEMORY, GPU, NODE

Since `DISPATCHED` and `STARTED` may contain NULL, `--sort` NULL handling is the same as `FINISHED` (`NULLS LAST` without `--reverse`, `NULLS FIRST` with `--reverse`).

Node name is obtained by the Watcher from the Pod's `spec.nodeName` during RUNNING transition and recorded in the DB. For jobs that complete instantly (transitioning directly to SUCCEEDED/FAILED without going through RUNNING), the Watcher attempts to obtain it from the Pod as a fallback during the completion transition. Unexecuted jobs (QUEUED / DISPATCHED, etc.) display `-`.

### 5.2 Resource Consumption

| Command | Description | Target Table |
|---|---|---|
| `cjobctl usage list [--namespace <ns>] [--flavor <name>]` | Daily consumption, 7-day window aggregation, DRF dominant share | `namespace_daily_usage`, `namespace_weights`, `flavor_quotas` |
| `cjobctl usage reset [--namespace <ns> \| --all]` | Delete consumption data | `namespace_daily_usage` |
| `cjobctl usage quota [--namespace <ns>]` | ResourceQuota usage for all namespaces | `namespace_resource_quotas` + K8s namespace list |

The Daily Usage in `usage list` is displayed in ascending date order (oldest date first) by default. The `--namespace` option filters to data for a specific namespace only (applies to all sections: Daily / 7-Day Window / DRF). The `--flavor` option filters to records of a specific ResourceFlavor (applies to Daily / N-Day Window; the DRF section is suppressed — see below). `--namespace` and `--flavor` can be combined: when both are specified, they are applied as an AND condition.

#### `cjobctl usage list`

Reads each namespace's resource consumption from the `namespace_daily_usage` table and prints three sections in order. The output order is always fixed; if no data exists, it prints `No usage data found.` and exits (when `--flavor` is specified, the message is `No usage data found for flavor '<name>'.`).

Column schemas and unit conversions for each section:

**Daily Usage**

| Column | Description |
|---|---|
| `NAMESPACE` | User namespace |
| `DATE` | `usage_date` (YYYY-MM-DD) |
| `CPU (core·h)` | `SUM(cpu_millicores_seconds) / 1000 / 3600` |
| `Mem (GiB·h)` | `SUM(memory_mib_seconds) / 1024 / 3600` |
| `GPU (h)` | `SUM(gpu_seconds) / 3600` |

The primary key of `namespace_daily_usage` is the composite key `(namespace, usage_date, flavor)`, so multiple rows exist for the same `(namespace, date)` — one per flavor. Daily Usage aggregates across flavors by using `GROUP BY namespace, usage_date`, so even with multiple flavors each date collapses to a single row. Order is `ORDER BY usage_date ASC, namespace ASC`. When `--flavor <name>` is specified, `AND flavor = $flavor` is added to the `WHERE` clause so that only records of the specified flavor are aggregated. In that case the section header becomes `=== Daily Usage (flavor: <name>) ===`, making it distinguishable from the flavor-merged result.

**N-Day Window Aggregate**

| Column | Description |
|---|---|
| `NAMESPACE` | User namespace |
| `CPU (core·h)` | Sum over the past N days (same unit conversion as Daily Usage) |
| `Mem (GiB·h)` | Same |
| `GPU (h)` | Same |

The aggregation window size N is read from the key `FAIR_SHARE_WINDOW_DAYS` in the `cjob-config` ConfigMap in the `cjob-system` namespace (fallback is 7 days if retrieval fails or the key is absent). The section header reflects the actual value used, e.g., `=== N-Day Window Aggregate ===`. The SQL condition is `usage_date > CURRENT_DATE - N`. When `--flavor <name>` is specified, `AND flavor = $flavor` is added to the `WHERE` clause and the section header becomes `=== N-Day Window Aggregate (flavor: <name>) ===`.

**DRF Dominant Share**

| Column | Description |
|---|---|
| `NAMESPACE` | User namespace |
| `CPU (core·h)` | Sum across flavors over the past N days (same as Window Aggregate) |
| `Mem (GiB·h)` | Same |
| `GPU (h)` | Same |
| `WEIGHT` | `namespace_weights.weight` (defaults to 1.0 when the row is absent) |
| `DOM_SHARE` | Weighted DRF score divided by the namespace weight |

The formula is identical to the Dispatcher (`server/src/cjob/dispatcher/scheduler.py`): compute `dominant_share = GREATEST(cpu_share, mem_share, gpu_share)` per flavor, then weight each by `flavor_quotas.drf_weight` and sum within the namespace:

```
window_seconds        = N × 86400
cpu_share(f)          = cpu_millicores_seconds(ns,f) / (cap_cpu(f) × window_seconds)
mem_share(f)          = memory_mib_seconds(ns,f)     / (cap_mem(f) × window_seconds)
gpu_share(f)          = gpu_seconds(ns,f)            / (cap_gpu(f) × window_seconds)
dominant_share(ns,f)  = MAX(cpu_share, mem_share, gpu_share)
drf_score(ns)         = Σ_f dominant_share(ns,f) × drf_weight(f)
DOM_SHARE(ns)         = drf_score(ns) / namespace_weight(ns)
```

The per-flavor capacity `cap_*` uses the `node_resources` allocatable total clamped by the `flavor_quotas` nominalQuota (`min(allocatable, nominalQuota)`). If `node_resources` is empty or the flavor is missing from `flavor_quotas`, allocatable is used as-is as a fallback. If `node_resources` is entirely empty, the DRF section is replaced with `No node_resources data. DRF disabled.` (the Dispatcher disables DRF sorting and falls back to namespace name order, but cjobctl is a display tool so it explicitly indicates that calculation is not possible).

Rows are ordered by `DOM_SHARE` ascending (namespaces with lower consumption first). Namespaces with `WEIGHT = 0` are placed at the end by treating `DOM_SHARE` as effectively `inf`.

When `--flavor <name>` is specified, the DRF Dominant Share section is skipped entirely (both computation and output). DRF is by definition a score that combines weighted dominant shares across flavors, so restricting it to a single flavor defeats the point of DRF (the result would merely be the single flavor's dominant share × `drf_weight`, which is misleading). In place of the section, a single-line note `DRF Dominant Share is computed across all flavors; pass no --flavor to see it.` is printed.

Output example (`FAIR_SHARE_WINDOW_DAYS=7`, cpu-flavor-only cluster):

```
=== Daily Usage ===
NAMESPACE            DATE             CPU (core·h)    Mem (GiB·h)    GPU (h)
user-alice           2026-04-05               12.0           48.0        0.0
user-bob             2026-04-05                4.0           16.0        0.0
user-alice           2026-04-06                8.0           32.0        0.0

=== 7-Day Window Aggregate ===
NAMESPACE              CPU (core·h)    Mem (GiB·h)    GPU (h)
user-alice                     20.0           80.0        0.0
user-bob                        4.0           16.0        0.0

=== DRF Dominant Share ===
NAMESPACE              CPU (core·h)    Mem (GiB·h)    GPU (h)   WEIGHT        DOM_SHARE
user-bob                        4.0           16.0        0.0        1         0.001488
user-alice                     20.0           80.0        0.0        1         0.007440
```

In multi-flavor clusters, when `--flavor` is not specified the Daily Usage section collapses to one row per `(namespace, date)` and does not show a per-flavor breakdown. To see a per-flavor breakdown, use `cjobctl usage list --flavor <name>` (Daily Usage and N-Day Window Aggregate will then aggregate only records of the specified flavor). DRF Dominant Share internally computes dominant share per flavor and combines them using `drf_weight`, but the output does not include a flavor column and only shows per-namespace aggregated values.

The name passed to `--flavor` is validated against the `flavor_quotas` table. If an unregistered flavor name is specified, the command prints `Flavor '<name>' not found in flavor_quotas. Ensure the Watcher has synced the ClusterQueue.` and exits with an error (same policy as `cjobctl cluster set-drf-weight`).

#### `cjobctl usage quota`

Displays ResourceQuota usage for all user namespaces. Retrieves the user namespace list from the K8s API (same pattern as `weight exclusive`) and cross-references with the DB's `namespace_resource_quotas` table.

- CPU displayed in cores (millicores / 1000), consistent with `cjob usage` (#105)
- Memory displayed in GiB (MiB / 1024)
- GPU displayed as count
- Jobs displays used/hard of `count/jobs.batch` (`-` when ResourceQuota does not include `count/jobs.batch`)
- `updated_at` displays freshness in relative time (`Xm ago`, `Xh ago`, etc.)
- Filterable by specific namespace with `--namespace`
- Namespaces without DB rows (no ResourceQuota configured) display `-` for each column
- When no user namespaces exist, displays "No user namespaces found."

Each column is formatted with dynamic column widths (column width determined by the maximum width of headers and data, with 3 spaces between columns).

Output example:

```
Namespace      CPU (used/hard)   Memory (used/hard)   GPU (used/hard)   Jobs (used/hard)   Updated
user-alice      20.0 / 300.0      80Gi / 1250Gi       0 / 4             3 / 50             2m ago
user-bob       260.0 / 300.0     800Gi / 1250Gi       1 / 4            12 / 50             2m ago
user-charlie   -                 -                    -                 -                   -
```

### 5.3 Namespace Weight Management

| Command | Description | Target |
|---|---|---|
| `cjobctl weight list` | List weights for all namespaces | DB: `namespace_weights` |
| `cjobctl weight set <namespace> <weight>` | Set weight (UPSERT, real numbers allowed) | DB: `namespace_weights` |
| `cjobctl weight reset <namespace>` | Reset weight to default (1) | DB: `namespace_weights` |
| `cjobctl weight reset --all` | Delete all namespace weight overrides | DB: `namespace_weights` |
| `cjobctl weight exclusive <namespace>` | Grant exclusive cluster access to the specified namespace | DB + K8s |

`weight exclusive` lists namespaces with the `cjob.io/user-namespace=true` label via the K8s API and sets weight = 0 for all namespaces except the specified one. cjobctl can change the label selector via `user_namespace_label` in the `[kubernetes]` section of `config.toml`.

### 5.4 Cluster Resource Checks

| Command | Description | Target |
|---|---|---|
| `cjobctl cluster resources` | Display per-node allocatable, cluster total, and max node value (reject threshold) | DB: `node_resources`, `flavor_quotas` |
| `cjobctl cluster flavor-usage` | Display resource usage rate per ResourceFlavor | K8s: ClusterQueue |
| `cjobctl cluster show-quota` | Display ClusterQueue nominalQuota per ResourceFlavor | K8s: ClusterQueue |
| `cjobctl cluster set-quota --flavor <name> [--cpu <n>] [--memory <s>] [--gpu <n>] [--force]` | Update nominalQuota for the specified ResourceFlavor | DB + K8s: ClusterQueue |
| `cjobctl cluster set-drf-weight <flavor> <weight>` | Set DRF weight for the specified flavor | DB: `flavor_quotas` |

#### `cjobctl cluster resources`

Output example:

```
=== Node Resources ===
NODE              FLAVOR      CPU (cores)   Memory (GiB)   GPU   Updated
node-compute-01   cpu              64         256.0          0   2026-03-27 10:05:00
node-compute-02   cpu              64         256.0          0   2026-03-27 10:05:00
node-gpu-01       gpu-a100         32         128.0          4   2026-03-27 10:05:00

=== Cluster Totals (for DRF normalization) ===
CPU:    160 cores (160000m)
Memory: 640.0 GiB (655360 MiB)
GPU:    4

=== Per-Flavor Totals (set-quota reference) ===
FLAVOR      CPU (cores)   Memory (GiB)   GPU   DRF Weight
cpu              128         512.0          0   1.0
gpu-a100          32         128.0          4   2.0

=== Per-Flavor Max Node Allocatable ===
FLAVOR      CPU (cores)   Memory (GiB)   GPU
cpu               64         256.0          0
gpu-a100          32         128.0          4
```

`Per-Flavor Totals` matches the values used by `cjobctl cluster set-quota` validation. CPU is calculated by truncating each node's `cpu_millicores` to integer cores before summing (considering bin-packing), while memory/GPU are simple sums. Meanwhile, `Cluster Totals (for DRF normalization)` shows the cluster-wide effective allocatable total used for Dispatcher's DRF normalization (without truncation). `DRF Weight` displays the value set with `cjobctl cluster set-drf-weight`.

#### `cjobctl cluster flavor-usage`

Displays the usage rate of currently reserved resources (`status.flavorsReservation`) against the nominalQuota for each ResourceFlavor in the ClusterQueue.

Output example:

```
=== ResourceFlavor Usage (cjob-cluster-queue) ===
FLAVOR          RESOURCE          RESERVED    NOMINAL   USAGE
cpu             cpu                     48        256   18.8%
cpu             memory               192Gi     1000Gi   19.2%
cpu             nvidia.com/gpu           0          0       -
gpu-a100        cpu                     16         64   25.0%
gpu-a100        memory                64Gi      500Gi   12.8%
gpu-a100        nvidia.com/gpu           2          4   50.0%
```

#### `cjobctl cluster show-quota`

Displays the nominalQuota for each ResourceFlavor in the ClusterQueue.

Output example:

```
=== ClusterQueue nominalQuota (cjob-cluster-queue) ===

[cpu]
  CPU:    256
  Memory: 1000Gi
  GPU:    0

[gpu-a100]
  CPU:    64
  Memory: 500Gi
  GPU:    4
```

#### `cjobctl cluster set-quota`

Updates the nominalQuota for the specified ResourceFlavor. `--flavor` is required and specifies the target ResourceFlavor name. `--cpu`, `--memory`, and `--gpu` are all optional, and only specified resources are updated. At least one must be specified.

```bash
# Update CPU and memory for the cpu flavor
cjobctl cluster set-quota --flavor cpu --cpu 256 --memory 1000Gi

# Update GPU for the gpu-a100 flavor
cjobctl cluster set-quota --flavor gpu-a100 --gpu 4
```

Specified values are validated against the allocatable total from the `node_resources` table (only nodes of the specified flavor). If exceeding allocatable, an error is returned, but can be overridden with `--force`. The name specified with `--flavor` must match the Kueue ResourceFlavor's `metadata.name` (also unified with the DB's `node_resources.flavor` column values).

The CPU allocatable total is calculated by truncating each node's `cpu_millicores` to integer cores before summing (`SUM((cpu_millicores / 1000) * 1000)`). This is based on the principle that fractional cores per node (e.g., 0.633 cores remaining after DaemonSet Pod deduction) cannot be used under integer-core job bin-packing constraints, so nominalQuota should be kept at or below "the sum of integer core portions per node." Memory and GPU are summed without truncation.

#### `cjobctl cluster set-drf-weight`

Sets the DRF weight for the specified flavor. During DRF calculation, both consumption and capacity are multiplied by this weight. Set larger values (e.g., 2.0) for precious resources like GPU, and smaller values (e.g., 0.5) for lower-spec flavors. Default is 1.0.

```bash
cjobctl cluster set-drf-weight gpu-a100 2.0
cjobctl cluster set-drf-weight cpu-slow 0.5
# To reset to default
cjobctl cluster set-drf-weight gpu-a100 1.0
```

Weight must be greater than 0. An error is returned if the specified flavor does not exist in the `flavor_quotas` table (the Watcher must have already synced the ClusterQueue).

### 5.5 K8s Status Checks

| Command | Description | K8s API |
|---|---|---|
| `cjobctl system status` | Pod list in cjob-system | `Api::<Pod>::list()` |
| `cjobctl system logs <component> [--tail <n>]` | Component logs | `Api::<Pod>::logs()` |
| `cjobctl config show` | Contents of cjob-config ConfigMap | `Api::<ConfigMap>::get()` |
| `cjobctl config set <key> <value> [--yes]` | Update ConfigMap setting value | `Api::<ConfigMap>::patch()` |
| `cjobctl config set <key> --from-file <path> [--yes]` | Update setting value from file | `Api::<ConfigMap>::patch()` |
| `cjobctl config dump` | Output ConfigMap as `kubectl apply`-compatible YAML | `Api::<ConfigMap>::get()` |

Valid component names for `system logs`: `dispatcher`, `watcher`, `submit-api`. Identified by Pod label `app=<component>`. Default value for `--tail` is 50.

#### `cjobctl config set`

Updates the value of a specified key in ConfigMap `cjob-config`. Displays the change and presents a `[y/N]` confirmation prompt. Confirmation can be skipped with `--yes`.

```bash
# Updating a scalar value
$ cjobctl config set DISPATCH_BATCH_SIZE 100
DISPATCH_BATCH_SIZE: 50 → 100
Proceed? [y/N] y

Updated 'DISPATCH_BATCH_SIZE' in cjob-config.

Restart the following component(s) to apply:
  cjobctl system restart dispatcher

# Updating a JSON value (from file)
$ cjobctl config set RESOURCE_FLAVORS --from-file flavors.json
RESOURCE_FLAVORS: [{"name":"cpu",...}] → [{"name":"cpu",...},{"name":"gpu",...}]
Proceed? [y/N] y

Updated 'RESOURCE_FLAVORS' in cjob-config.

Restart the following component(s) to apply:
  cjobctl system restart dispatcher
  cjobctl system restart watcher
  cjobctl system restart submit-api
```

**Validation:**

The CLI performs the following validations:

- Key must exist in the ConfigMap (unknown keys are rejected)
- Non-updatable keys (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`) are rejected (DB connection changes require infrastructure work)
- Value type checking:
  - Integer-type keys: Must be parsable as `i64`
  - Boolean-type keys: `true` or `false` (case-insensitive, normalized to lowercase on save)
  - JSON-type keys: Must be valid JSON
  - String-type keys: Always valid

**Mutual exclusion of `value` and `--from-file`:**

`value` (positional argument) and `--from-file` cannot be specified simultaneously. When `--from-file` is specified, `value` must be omitted. An error occurs if neither is specified.

**Key-to-component mapping:**

After update, the restart command for affected components is displayed. The mapping between each key and its components is as follows:

| Key | Type | Component(s) |
|---|---|---|
| `DISPATCH_BUDGET_PER_NAMESPACE` | int | dispatcher |
| `DISPATCH_BATCH_SIZE` | int | dispatcher |
| `DISPATCH_FETCH_MULTIPLIER` | int | dispatcher |
| `DISPATCH_ROUND_SIZE` | int | dispatcher |
| `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` | int | dispatcher, watcher |
| `DISPATCH_RETRY_INTERVAL_SEC` | int | dispatcher |
| `DISPATCH_MAX_RETRIES` | int | dispatcher |
| `GAP_FILLING_ENABLED` | bool | dispatcher |
| `GAP_FILLING_STALL_THRESHOLD_SEC` | int | dispatcher |
| `FAIR_SHARE_WINDOW_DAYS` | int | dispatcher, submit-api |
| `USAGE_RETENTION_DAYS` | int | dispatcher |
| `CPU_LIMIT_BUFFER_MULTIPLIER` | float | dispatcher |
| `RESOURCE_FLAVORS` | json | dispatcher, watcher, submit-api |
| `DEFAULT_FLAVOR` | string | submit-api |
| `NODE_RESOURCE_SYNC_INTERVAL_SEC` | int | watcher |
| `CLUSTER_QUEUE_NAME` | string | watcher |
| `RESOURCE_QUOTA_NAME` | string | watcher |
| `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` | int | watcher |
| `USER_NAMESPACE_LABEL` | string | watcher |
| `MAX_QUEUED_JOBS_PER_NAMESPACE` | int | submit-api |
| `MAX_SWEEP_COMPLETIONS` | int | submit-api |
| `DEFAULT_TIME_LIMIT_SECONDS` | int | submit-api |
| `MAX_TIME_LIMIT_SECONDS` | int | submit-api |
| `LOG_BASE_DIR` | string | submit-api |
| `KUEUE_LOCAL_QUEUE_NAME` | string | dispatcher |
| `WORKSPACE_MOUNT_PATH` | string | dispatcher |
| `TTL_SECONDS_AFTER_FINISHED` | int | dispatcher |
| `JOB_NODE_TAINT` | string | dispatcher |
| `WATCHER_METRICS_PORT` | int | watcher |
| `DISPATCHER_METRICS_PORT` | int | dispatcher |
| `LOG_LEVEL` | string | dispatcher, watcher, submit-api |

**Non-updatable keys:**

| Key | Reason |
|---|---|
| `POSTGRES_HOST` | DB connection changes require infrastructure work |
| `POSTGRES_PORT` | DB connection changes require infrastructure work |
| `POSTGRES_DB` | DB connection changes require infrastructure work |
| `POSTGRES_USER` | DB connection changes require infrastructure work |
| `POSTGRES_PASSWORD` | DB connection changes require infrastructure work |

#### `cjobctl config dump`

Outputs the contents of ConfigMap `cjob-config` to standard output in clean YAML format compatible with `kubectl apply -f`. Used for backup or applying to other environments.

Management fields (`managedFields`, `resourceVersion`, `uid`, `creationTimestamp`, `kubectl.kubernetes.io/*` within `annotations`) are removed.

```bash
$ cjobctl config dump > cjob-config-backup.yaml

# Restore
$ kubectl apply -f cjob-config-backup.yaml
```

### 5.6 CLI Binary Distribution

| Command | Description | Target |
|---|---|---|
| `cjobctl cli list` | Display registered versions on PVC | K8s Pod + PVC |
| `cjobctl cli deploy --binary <path> --version <version> [--release]` | Deploy CLI binary to PVC | K8s Pod + PVC |
| `cjobctl cli remove <version>...` | Delete specified version binaries from PVC (multiple allowed) | K8s Pod + PVC |
| `cjobctl cli set-latest <version>` | Change the latest version pointer | K8s Pod + PVC |

All subcommands operate on PVC using the temporary Pod (busybox) + `kubectl exec` pattern. Temporary Pods use a minimal image (`busybox`) and mount the `cli-binary` PVC at `/cli-binary`.

For usage examples, see [deployment.md](../deployment.md) §4.1 and [operations.md](../operations.md) §8.

#### `cjobctl cli list`

Displays registered versions from the directory structure on PVC.

```
$ cjobctl cli list
VERSION            LATEST
1.3.0-beta.1
1.3.0              ← latest
1.2.0
1.1.0
```

Internal processing:
1. Start a temporary Pod
2. Get the list of version directories with `ls /cli-binary/`
3. Get the latest version with `cat /cli-binary/latest`
4. Sort in descending semver order and display with latest marker
5. Delete the temporary Pod

#### `cjobctl cli deploy`

Internal processing:
1. Start a temporary Pod with `kubectl run` mounting the `cli-binary` PVC (ReadWriteMany)
2. Copy the binary to `/cli-binary/<version>/cjob` inside the temporary Pod with `kubectl cp`
3. Execute `chmod +x` inside the temporary Pod
4. Update the `latest` file only when the `--release` option is specified
5. Delete the temporary Pod

`--release` cannot be used with pre-release versions (versions containing `-` in the version string). Pre-release detection is based on whether the version string contains `-`.

```bash
# Deploy binary only (latest is not updated)
$ cjobctl cli deploy --binary ./cjob --version 1.3.0
Deployed v1.3.0 (latest unchanged: 1.2.0)

# Deploy binary + update latest
$ cjobctl cli deploy --binary ./cjob --version 1.3.0 --release
Deployed v1.3.0 (latest updated)

# Deploy beta version (--release cannot be used)
$ cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1
Deployed v1.3.1-beta.1 (latest unchanged: 1.3.0)

$ cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1 --release
Error: Cannot use --release with pre-release version 1.3.1-beta.1.
```

#### `cjobctl cli set-latest`

Changes the `latest` file on PVC to the specified version. Does not deploy any binaries. Used when latest was accidentally updated or when rolling back from a problematic version.

```bash
# Change latest to 1.2.0
$ cjobctl cli set-latest 1.2.0
Latest updated to v1.2.0.

# Non-existent version results in error
$ cjobctl cli set-latest 9.9.9
Error: Version 9.9.9 not found on PVC. Deploy it first.

# Pre-release version cannot be specified
$ cjobctl cli set-latest 1.3.0-beta.1
Error: Cannot set pre-release version 1.3.0-beta.1 as latest.
```

Internal processing:
1. Start a temporary Pod
2. Check if the specified version's directory exists
3. Update the latest file with `echo "<version>" > /cli-binary/latest`
4. Delete the temporary Pod

#### `cjobctl cli remove`

Deletes the specified version's binary directory from PVC.

```bash
# Remove a single version
$ cjobctl cli remove 1.1.0
Removed CLI v1.1.0.

# Remove multiple versions at once
$ cjobctl cli remove 1.0.0 1.1.0
Removed 2 versions.

$ cjobctl cli remove 1.3.0
Error: Cannot remove version 1.3.0: it is the current latest.
```

Internal processing:
1. Start a temporary Pod
2. Get the latest version with `cat /cli-binary/latest`
3. Validate specified versions (error if latest, error if non-existent)
4. Display confirmation prompt
5. Delete each version with `rm -rf /cli-binary/<version>`
6. Delete the temporary Pod

### 5.7 User Management

| Command | Description | Target |
|---|---|---|
| `cjobctl user list [--enabled \| --disabled]` | User namespace list | K8s: Namespace |
| `cjobctl user enable --namespace <ns>...` | Enable CJob (multiple allowed) | K8s: Namespace |
| `cjobctl user disable --namespace <ns>...` | Disable CJob (multiple allowed) | K8s: Namespace |

User namespaces are identified as Namespaces with the `type=user` label. The username is obtained from each namespace's `cjob.io/username` annotation, and the enabled/disabled state from the `cjob.io/user-namespace` label value.

#### `cjobctl user list`

Lists all namespaces with the `type=user` label.

```
$ cjobctl user list
NAMESPACE          USERNAME       ENABLED
user-alice         alice          true
user-bob           bob            true
user-charlie       charlie        false
```

- `--enabled`: Show only namespaces where `cjob.io/user-namespace` label value is `"true"`
- `--disabled`: Show only namespaces where `cjob.io/user-namespace` label value is not `"true"`
- `--enabled` and `--disabled` are mutually exclusive (cannot be specified together)

#### `cjobctl user enable`

Sets the `cjob.io/user-namespace: "true"` label on the specified namespace(s). Multiple namespaces can be specified simultaneously.

All namespaces are pre-validated before execution; an error is returned if any non-existent namespace or namespace without the `type=user` label is included. No label changes are made until validation passes.

```bash
$ cjobctl user enable --namespace user-charlie
Enabled CJob for namespace 'user-charlie'.

$ cjobctl user enable --namespace user-alice user-bob
Enabled CJob for namespace 'user-alice'.
Enabled CJob for namespace 'user-bob'.
```

#### `cjobctl user disable`

Changes the `cjob.io/user-namespace` label value to `"false"` for the specified namespace(s). Multiple namespaces can be specified simultaneously.

Pre-validation is the same as `enable` (existence check + `type=user` label verification).

```bash
$ cjobctl user disable --namespace user-bob
Disabled CJob for namespace 'user-bob'.

$ cjobctl user disable --namespace user-alice user-bob
Disabled CJob for namespace 'user-alice'.
Disabled CJob for namespace 'user-bob'.
```

### 5.8 DB Schema Management

| Command | Description |
|---|---|
| `cjobctl db migrate` | Execute idempotent schema migration |

Uses `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS`, making it safe to run multiple times.

### 5.9 System Management

| Command | Description | Target |
|---|---|---|
| `cjobctl system stop [--yes]` | Safe shutdown of CJob system | DB + K8s: Deployment |
| `cjobctl system start [--submit-api-replicas <n>]` | Start CJob system | K8s: Deployment |
| `cjobctl system restart <component>` | Rolling restart of a component | K8s: Deployment |
| `cjobctl system status` | Pod list in cjob-system | K8s: Pod |
| `cjobctl system logs <component> [--tail <n>]` | Component logs | K8s: Pod |

#### `cjobctl system stop`

Safely stops the CJob system. Execute before maintenance or K8s cluster shutdown. PostgreSQL is not stopped.

Shutdown sequence:

1. Display active job count and show confirmation prompt (skippable with `--yes`)
2. Scale down Submit API to replicas=0 to block new job submissions
3. Scale down Dispatcher to replicas=0 to prevent job re-dispatch
4. Scale down Watcher to replicas=0 to prevent DB state overwrites
5. Update job states in DB:
   - DISPATCHING → QUEUED (reset `retry_after = NULL`, `retry_count = 0`)
   - DISPATCHED → QUEUED
   - RUNNING → FAILED (`last_error = 'system shutdown'`, `finished_at = NOW()`)
   - QUEUED → No change
6. Delete all K8s Jobs (with `cjob.io/job-id` label) in all user namespaces with `propagationPolicy=Background`

The namespace's `cjob.io/user-namespace` label is not changed. User access permissions are preserved across restarts.

Jobs reverted to QUEUED will be automatically re-dispatched by the Dispatcher after system startup. The DISPATCHING reset is equivalent to the Dispatcher's startup initialization (see [dispatcher.md](dispatcher.md) §2.6).

```bash
$ cjobctl system stop
Active jobs: 15 (QUEUED: 8, DISPATCHING: 1, DISPATCHED: 2, RUNNING: 4)
This will:
  - Scale down submit-api, dispatcher, watcher to 0 replicas
  - Revert 3 DISPATCHING/DISPATCHED jobs to QUEUED
  - Fail 4 RUNNING jobs (last_error: system shutdown)
  - Delete K8s Jobs in all user namespaces
  - 8 QUEUED jobs will be re-dispatched on next start
Proceed? [y/N] y
Scaled down submit-api to 0 replicas.
Scaled down dispatcher to 0 replicas.
Scaled down watcher to 0 replicas.
Reverted 1 DISPATCHING job(s) to QUEUED.
Reverted 2 DISPATCHED job(s) to QUEUED.
Failed 4 RUNNING job(s).
Deleted 6 K8s Job(s).
CJob system stopped. PostgreSQL remains running.
```

#### `cjobctl system start`

Starts the CJob system. Scales up each Deployment to default replicas.

- Dispatcher: 1
- Watcher: 1
- Submit API: 2 (changeable with `--submit-api-replicas`)

```bash
$ cjobctl system start
Scaled up dispatcher to 1 replica(s).
Scaled up watcher to 1 replica(s).
Scaled up submit-api to 2 replica(s).
CJob system started. Use 'cjobctl system status' to check pod status.
```

#### `cjobctl system restart`

Performs a rolling restart of the specified component's Deployment. Equivalent to `kubectl rollout restart`, it triggers K8s rolling update by setting the `kubectl.kubernetes.io/restartedAt` annotation on the Pod template to the current time.

Valid component names: `dispatcher`, `watcher`, `submit-api`

```bash
$ cjobctl system restart submit-api
Restarting submit-api... (use 'cjobctl system status' to check)
```

## 6. Safety Measures for Destructive Operations

The following commands display a `[y/N]` confirmation prompt before execution:

- `cjobctl jobs cancel`
- `cjobctl usage reset`
- `cjobctl weight reset --all`
- `cjobctl cli remove`
- `cjobctl system stop`
- `cjobctl config set`

## 7. Source Code Structure

```
ctl/
├── Cargo.toml
└── src/
    ├── main.rs            # Clap definitions + command dispatch
    ├── config.rs          # Configuration file loading
    ├── db.rs              # Auto port-forward startup + DB connection
    ├── k8s.rs             # K8s client initialization
    └── cmd/
        ├── mod.rs
        ├── cli/
        │   ├── mod.rs         # Shared utilities + submodule declarations
        │   ├── deploy.rs      # cli deploy (including beta version support)
        │   ├── list.rs        # cli list
        │   ├── remove.rs      # cli remove
        │   └── set_latest.rs  # cli set-latest
        ├── system/
        │   ├── mod.rs         # Shared constants + scale_deployment helper
        │   ├── stop.rs        # system stop
        │   ├── start.rs       # system start
        │   ├── restart.rs     # system restart (rolling update)
        │   ├── status.rs      # system status (Pod list)
        │   └── logs.rs        # system logs (component logs)
        ├── config/
        │   ├── mod.rs         # Submodule declarations
        │   ├── show.rs        # config show (ConfigMap display)
        │   ├── set.rs         # config set (ConfigMap update)
        │   └── dump.rs        # config dump (ConfigMap YAML output)
        ├── jobs.rs        # jobs list/stalled/remaining/summary/counters
        ├── usage.rs       # usage list/reset + ClusterTotals
        ├── weight.rs      # weight list/set/reset/exclusive
        ├── cluster.rs     # cluster resources
        ├── db_migrate.rs  # db migrate
        └── user.rs        # user list/enable/disable
```

Refer to the corresponding files under `ctl/src/cmd/` for the SQL queries executed by each command.

## 8. Differences from the cjob CLI

| | cjob (User CLI) | cjobctl (Admin CLI) |
|---|---|---|
| Target Users | General users | Cluster administrators |
| Execution Environment | User Pod inside K8s cluster | Administrator's local PC |
| Communication Target | Submit API (HTTP) | PostgreSQL (direct) + K8s API |
| Authentication | ServiceAccount JWT | kubeconfig + DB password |
| Scope | Own namespace jobs only | All namespaces |
| Distribution Method | `cjob update` (via Submit API) | Build from source |
