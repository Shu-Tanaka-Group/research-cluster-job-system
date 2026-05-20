> *This document was auto-translated from the [Japanese original](../../docs/architecture/cjobctl.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# cjobctl Design

## 1. Overview

`cjobctl` is the administrator-facing CLI tool for the CJob system. It runs on the administrator's local PC and performs system state checks and configuration changes by directly connecting to PostgreSQL and through the Kubernetes API.

While the user-facing CLI `cjob` goes through the Submit API, `cjobctl` accesses the DB and K8s API directly.

```
Administrator PC
├── cjobctl ──→ PostgreSQL (via kubectl port-forward, automatic)
└── cjobctl ──→ Kubernetes API (via kubeconfig)
```

## 2. Technology Stack

| Item | Technology |
|---|---|
| Language | Rust |
| CLI framework | Clap (derive) |
| DB client | tokio-postgres |
| K8s client | kube + k8s-openapi |
| Async runtime | tokio |
| Configuration file | TOML (toml crate) |

## 3. Connection Method

### 3.1 DB Connection

When executing a DB command, `kubectl port-forward` is automatically started. The local port is auto-assigned by the OS (by specifying port 0) to avoid conflicts with existing processes. When the command completes, the port-forward process is automatically terminated.

```
cjobctl → kubectl port-forward (auto-start, random port)
        → 127.0.0.1:<random> → svc/postgres:5432
        → connect via tokio-postgres
        → command completes → kill port-forward process
```

The prerequisites are that `kubectl` is in the PATH and that the cluster is accessible via kubeconfig.

### 3.2 K8s Connection

`kube::Client::try_default()` automatically configures the client from kubeconfig. No port-forward is required.

## 4. Configuration File

`~/.config/cjobctl/config.toml`:

```toml
[database]
database = "cjob"
user = "cjob"
password = "xxx"

[kubernetes]
namespace = "cjob-system"   # default when omitted
```

`host` / `port` are managed by automatic port-forward, so no configuration is needed.

## 5. Command List

### 5.1 DB State Inspection

| Command | Overview | Target table |
|---|---|---|
| `cjobctl jobs list [--namespace <ns>] [--status <s>] [--sort <field>] [--reverse] [-o wide]` | Job list | `jobs` |
| `cjobctl jobs status --namespace <ns> --job-id <id>` | Detail display of individual job (equivalent to `cjob status`) | `jobs` |
| `cjobctl jobs summary` | Job count by namespace × status (pivot table) | `jobs` |
| `cjobctl jobs stalled [--sort <field>] [--reverse]` | Jobs stuck in DISPATCHED state | `jobs` |
| `cjobctl jobs remaining [--sort <field>] [--reverse]` | Remaining time for RUNNING jobs | `jobs` |
| `cjobctl jobs cancel --namespace <ns> [--job-id <id> \| --status <s> \| --all]` | Cancel jobs | `jobs` |
| `cjobctl jobs counters` | job_id counter per namespace | `user_job_counters` |

#### Sort Options

`jobs list`, `jobs stalled`, and `jobs remaining` allow you to change the sort field with the `--sort` option. Combining with `--reverse` produces descending order.

| Command | Available sort fields | Default |
|---|---|---|
| `jobs list` | `NAMESPACE`, `CREATED`, `DISPATCHED`, `STARTED`, `FINISHED` | `NAMESPACE` (composite order of namespace, job_id) |
| `jobs stalled` | `NAMESPACE`, `CREATED` | `CREATED` (ascending by dispatched_at) |
| `jobs remaining` | `NAMESPACE`, `CREATED` | `REMAINING` (ascending by remaining_sec) |

Specifying `--sort FINISHED`, `--sort DISPATCHED`, or `--sort STARTED` with `stalled` / `remaining` is an error (the corresponding columns do not exist).

#### `-o wide` Option

The display columns for `jobs list` are NAMESPACE, JOB_ID, TYPE, STATUS, FLAVOR, COMMAND, CREATED, FINISHED. TYPE displays jobs with `completions IS NULL` as `job` and others as `sweep`.

When `-o wide` (`--output wide`) is specified, the following columns are added to the above:

- **DISPATCHED**: Job dispatch time (DB `dispatched_at` column, displayed as `-` when NULL)
- **STARTED**: Job start time (DB `started_at` column, displayed as `-` when NULL)
- **CPU**: Specified CPU resource amount (DB `cpu` column)
- **MEMORY**: Specified memory resource amount (DB `memory` column)
- **GPU**: Specified GPU count (DB `gpu` column, displayed as `-` when 0)
- **NODE**: Node name where the job runs (DB `node_name` column, displayed as `-` when NULL)

Column order with `-o wide`: NAMESPACE, JOB_ID, TYPE, STATUS, FLAVOR, COMMAND, CREATED, DISPATCHED, STARTED, FINISHED, CPU, MEMORY, GPU, NODE

Since `DISPATCHED` and `STARTED` may include NULL, the NULL handling for `--sort` is the same as `FINISHED` (`NULLS LAST` when `--reverse` is not specified, `NULLS FIRST` when specified).

The node name is acquired by Watcher from the Pod's `spec.nodeName` at the RUNNING transition and recorded in the DB. For jobs that complete instantaneously (transitioning directly to SUCCEEDED/FAILED without going through RUNNING), retrieval is attempted from the Pod as a fallback at the completion transition. Unexecuted jobs such as QUEUED / DISPATCHED are displayed as `-`.

### 5.2 Resource Consumption

| Command | Overview | Target table |
|---|---|---|
| `cjobctl usage list [--namespace <ns>] [--flavor <name>]` | Daily consumption / 7-day window aggregate / DRF dominant share | `namespace_daily_usage`, `namespace_weights`, `flavor_quotas` |
| `cjobctl usage reset [--namespace <ns> \| --all]` | Delete consumption data | `namespace_daily_usage` |
| `cjobctl usage quota [--namespace <ns>]` | ResourceQuota usage status for all namespaces | `namespace_resource_quotas` + K8s namespace list |

The Daily Usage of `usage list` is displayed in ascending date order by default (oldest date at the top). The `--namespace` option narrows the data to a specific namespace (applies to all Daily / 7-Day Window / DRF sections). The `--flavor` option narrows to records of a specific ResourceFlavor (applies to Daily / N-Day Window; the DRF section is hidden, details below). `--namespace` and `--flavor` can be used together, in which case the filtering is by AND condition.

#### `cjobctl usage list`

Reads resource consumption for each namespace from the `namespace_daily_usage` table and outputs three sections in order. The output is always in this order; if no data exists, `No usage data found.` is displayed and execution terminates (when `--flavor` is specified, `No usage data found for flavor '<name>'.` is displayed).

Column structure and unit conversion for each section:

**Daily Usage**

| Column | Content |
|---|---|
| `NAMESPACE` | User namespace |
| `DATE` | `usage_date` (YYYY-MM-DD) |
| `CPU (core·h)` | `SUM(cpu_millicores_seconds) / 1000 / 3600` |
| `Mem (GiB·h)` | `SUM(memory_mib_seconds) / 1024 / 3600` |
| `GPU (h)` | `SUM(gpu_seconds) / 3600` |

The primary key of `namespace_daily_usage` is the composite key `(namespace, usage_date, flavor)`, so for the same `(namespace, date)` there is a row per flavor. Since Daily Usage aggregates across flavors, it uses `GROUP BY namespace, usage_date` to sum them (even when multiple flavors exist, the same date is consolidated into one row). The order is `ORDER BY usage_date ASC, namespace ASC`. When `--flavor <name>` is specified, `AND flavor = $flavor` is added to the `WHERE` clause to aggregate only records of the specified flavor. In this case, the section header becomes `=== Daily Usage (flavor: <name>) ===` to distinguish it from flavor-summed results.

**N-Day Window Aggregate**

| Column | Content |
|---|---|
| `NAMESPACE` | User namespace |
| `CPU (core·h)` | Sum over the past N days (unit conversion same as Daily Usage) |
| `Mem (GiB·h)` | Ditto |
| `GPU (h)` | Ditto |

The aggregation window length N is obtained from the `FAIR_SHARE_WINDOW_DAYS` key of the `cjob-config` ConfigMap in the `cjob-system` namespace (defaults to 7 days on retrieval failure or when the key is unset). The actually used number of days is reflected in the section header as `=== N-Day Window Aggregate ===`. The SQL condition is `usage_date > CURRENT_DATE - N`. When `--flavor <name>` is specified, `AND flavor = $flavor` is added to the `WHERE` clause and the section header becomes `=== N-Day Window Aggregate (flavor: <name>) ===`.

**DRF Dominant Share**

| Column | Content |
|---|---|
| `NAMESPACE` | User namespace |
| `CPU (core·h)` | Flavor-summed value over the past N days (equivalent to Window Aggregate) |
| `Mem (GiB·h)` | Ditto |
| `GPU (h)` | Ditto |
| `WEIGHT` | `namespace_weights.weight` (1.0 if no row exists) |
| `DOM_SHARE` | Weighted DRF score divided by weight |

The formula is identical to that of the Dispatcher (`server/src/cjob/dispatcher/scheduler.py`): compute `dominant_share = GREATEST(cpu_share, mem_share, gpu_share)` per flavor, weight it by `flavor_quotas.drf_weight`, and sum within the namespace:

```
window_seconds        = N × 86400
cpu_share(f)          = cpu_millicores_seconds(ns,f) / (cap_cpu(f) × window_seconds)
mem_share(f)          = memory_mib_seconds(ns,f)     / (cap_mem(f) × window_seconds)
gpu_share(f)          = gpu_seconds(ns,f)            / (cap_gpu(f) × window_seconds)
dominant_share(ns,f)  = MAX(cpu_share, mem_share, gpu_share)
drf_score(ns)         = Σ_f dominant_share(ns,f) × drf_weight(f)
DOM_SHARE(ns)         = drf_score(ns) / namespace_weight(ns)
```

The capacity `cap_*` of each flavor uses the value of `node_resources` allocatable total clamped from above by the `flavor_quotas` nominalQuota (`min(allocatable, nominalQuota)`). When `node_resources` is empty or `flavor_quotas` has no corresponding flavor, allocatable is used as-is as a fallback. When `node_resources` itself is empty, the entire DRF section is replaced with `No node_resources data. DRF disabled.` (the Dispatcher disables DRF sorting and falls back to namespace-name order, but cjobctl explicitly indicates that computation is impossible).

The row order is ascending by `DOM_SHARE` (= namespaces with less consumption at the top). Namespaces with `WEIGHT = 0` have their `DOM_SHARE` treated as equivalent to `inf` and are placed at the end.

When `--flavor <name>` is specified, the DRF Dominant Share section is completely skipped (no computation or output). DRF by definition is a score that performs weighted summation across flavors, and limiting to a single flavor loses the purpose of DRF (fairness across multiple resource dimensions) (the result would simply be a single flavor's dominant share × `drf_weight`, which is misleading). In place of the DRF section, a one-line note `DRF Dominant Share is computed across all flavors; pass no --flavor to see it.` is output.

Output example (with `FAIR_SHARE_WINDOW_DAYS=7`, cpu flavor only configuration):

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

In a configuration with multiple flavors, when `--flavor` is not specified, the Daily Usage section is consolidated to one row per `(namespace, date)` and the flavor-wise breakdown is not displayed. To check the flavor-wise breakdown, use `cjobctl usage list --flavor <name>` (Daily Usage and N-Day Window Aggregate are aggregated only on records of the specified flavor). DRF Dominant Share internally computes the dominant share per flavor and performs weighted summation with `drf_weight`, but the output does not include the flavor column and displays only namespace-level aggregated values.

The name passed to `--flavor` is existence-checked against the `flavor_quotas` table. If an unregistered flavor name is specified, `Flavor '<name>' not found in flavor_quotas. Ensure the Watcher has synced the ClusterQueue.` is displayed and execution terminates abnormally (same policy as `cjobctl cluster set-drf-weight`).

#### `cjobctl usage quota`

Displays ResourceQuota usage status for all user namespaces. Retrieves the user namespace list from the K8s API (same pattern as `weight exclusive`) and cross-references with the DB's `namespace_resource_quotas` table.

- CPU is displayed in cores (millicores / 1000), consistent with `cjob usage` (#105)
- Memory is displayed in GiB (MiB / 1024)
- GPU is displayed in count
- Jobs displays the used/hard of `count/jobs.batch` (`-` if `count/jobs.batch` is not included in the ResourceQuota)
- `updated_at` is displayed as relative time (`Xm ago`, `Xh ago`, etc.) to show freshness
- `--namespace` allows filtering to a specific namespace
- Namespaces without rows in the DB (no ResourceQuota set) display each column as `-`
- "No user namespaces found." is displayed when no user namespace exists

Each column is formatted with dynamic column width (column width determined by the maximum width of header and data, with 3 spaces between columns).

Output example:

```
Namespace      CPU (used/hard)   Memory (used/hard)   GPU (used/hard)   Jobs (used/hard)   Updated
user-alice      20.0 / 300.0      80Gi / 1250Gi       0 / 4             3 / 50             2m ago
user-bob       260.0 / 300.0     800Gi / 1250Gi       1 / 4            12 / 50             2m ago
user-charlie   -                 -                    -                 -                   -
```

### 5.3 namespace weight Management

| Command | Overview | Target |
|---|---|---|
| `cjobctl weight list` | List of weights for all namespaces | DB: `namespace_weights` |
| `cjobctl weight set <namespace> <weight>` | Set weight (UPSERT, real numbers allowed) | DB: `namespace_weights` |
| `cjobctl weight reset <namespace>` | Reset weight to default (1) | DB: `namespace_weights` |
| `cjobctl weight reset --all` | Delete weight overrides for all namespaces | DB: `namespace_weights` |
| `cjobctl weight exclusive <namespace>` | Give the specified namespace exclusive use of the cluster | DB + K8s |

`weight exclusive` enumerates namespaces with the `cjob.io/user-namespace=true` label via the K8s API and sets weight = 0 for all namespaces other than the specified one. cjobctl allows changing the label selector via `user_namespace_label` in the `[kubernetes]` section of `config.toml`.

### 5.4 Cluster Resource Inspection

| Command | Overview | Target |
|---|---|---|
| `cjobctl cluster resources` | Display per-node allocatable, cluster totals, and per-node max (rejection threshold) | DB: `node_resources`, `flavor_quotas` |
| `cjobctl cluster flavor-usage` | Display resource utilization per ResourceFlavor | K8s: ClusterQueue |
| `cjobctl cluster show-quota` | Display nominalQuota per ResourceFlavor of the ClusterQueue | K8s: ClusterQueue |
| `cjobctl cluster set-quota --flavor <name> [--cpu <n>] [--memory <s>] [--gpu <n>] [--force]` | Update nominalQuota of the specified ResourceFlavor | DB + K8s: ClusterQueue |
| `cjobctl cluster set-drf-weight <flavor> <weight>` | Set DRF weight of the specified flavor | DB: `flavor_quotas` |

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

`Per-Flavor Totals` matches the values used by the validation of `cjobctl cluster set-quota`. CPU is summed after flooring each node's `cpu_millicores` to integer cores (considering bin-packing); memory/GPU are simply summed. In contrast, `Cluster Totals (for DRF normalization)` shows the cluster-wide effective allocatable totals (without flooring) used in the Dispatcher's DRF normalization. `DRF Weight` displays the value set by `cjobctl cluster set-drf-weight`.

#### `cjobctl cluster flavor-usage`

For each ResourceFlavor in the ClusterQueue, displays the usage rate of the currently reserved resources (`status.flavorsReservation`) relative to nominalQuota.

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

Displays nominalQuota for each ResourceFlavor of the ClusterQueue.

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

Updates the nominalQuota of the specified ResourceFlavor. `--flavor` is required and specifies the name of the ResourceFlavor to update. `--cpu`, `--memory`, and `--gpu` are all optional; only the specified resources are updated. At least one must be specified.

```bash
# Update CPU and memory of cpu flavor
cjobctl cluster set-quota --flavor cpu --cpu 256 --memory 1000Gi

# Update GPU of gpu-a100 flavor
cjobctl cluster set-quota --flavor gpu-a100 --gpu 4
```

The specified values are validated against the allocatable totals of the `node_resources` table (only for nodes of the specified flavor). If they exceed the allocatable amount, an error is raised, but it can be overridden with `--force`. The name passed to `--flavor` must match the `metadata.name` of the Kueue ResourceFlavor (also consistent with the value of the DB `node_resources.flavor` column).

The CPU allocatable total is calculated by flooring each node's `cpu_millicores` to integer cores before summing (`SUM((cpu_millicores / 1000) * 1000)`). This is based on the idea that since fractional cores per node (e.g., 0.633 cores left over after deducting DaemonSet Pods) cannot be used due to bin-packing constraints of integer-core jobs, the nominalQuota must be kept at or below "the sum of integer-core portions of each node". Memory and GPU are summed without flooring.

#### `cjobctl cluster set-drf-weight`

Sets the DRF weight of the specified flavor. During DRF computation, both consumption and capacity are multiplied by this weight. Set a large value (e.g., 2.0) for valuable resources such as GPU, and a small value (e.g., 0.5) for low-spec flavors. The default is 1.0.

```bash
cjobctl cluster set-drf-weight gpu-a100 2.0
cjobctl cluster set-drf-weight cpu-slow 0.5
# To reset to default
cjobctl cluster set-drf-weight gpu-a100 1.0
```

The weight must be greater than 0. If the specified flavor does not exist in the `flavor_quotas` table, an error is raised (the Watcher must have synced the ClusterQueue).

### 5.5 K8s State Inspection

| Command | Overview | K8s API |
|---|---|---|
| `cjobctl system status` | Pod list of cjob-system | `Api::<Pod>::list()` |
| `cjobctl system logs <component> [--tail <n>]` | Component logs | `Api::<Pod>::logs()` |
| `cjobctl config show` | Contents of cjob-config ConfigMap | `Api::<ConfigMap>::get()` |
| `cjobctl config set <key> <value> [--yes]` | Update a ConfigMap configuration value | `Api::<ConfigMap>::patch()` |
| `cjobctl config set <key> --from-file <path> [--yes]` | Update a configuration value from a file | `Api::<ConfigMap>::patch()` |
| `cjobctl config dump` | Output the ConfigMap as `kubectl apply`-able YAML | `Api::<ConfigMap>::get()` |

Valid component names for `system logs`: `dispatcher`, `watcher`, `submit-api`. Identified by the Pod's `app=<component>` label. The default value of `--tail` is 50.

#### `cjobctl config set`

Updates the value of the specified key of the `cjob-config` ConfigMap. Displays the change content and inserts a `[y/N]` confirmation prompt. `--yes` can skip the confirmation.

```bash
# Update a scalar value
$ cjobctl config set DISPATCH_BATCH_SIZE 100
DISPATCH_BATCH_SIZE: 50 → 100
Proceed? [y/N] y

Updated 'DISPATCH_BATCH_SIZE' in cjob-config.

Restart the following component(s) to apply:
  cjobctl system restart dispatcher

# Update a JSON value (from file)
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

The CLI performs the following validation:

- Key must exist in the ConfigMap (unknown keys are rejected)
- Non-updatable keys (`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`) are rejected (since changes to DB connection require infrastructure work)
- Value type check:
  - Integer-type key: must be parseable as `i64`
  - Boolean-type key: `true` or `false` (case-insensitive, normalized to lowercase when saved)
  - JSON-type key: must be valid JSON
  - String-type key: always valid

**Exclusivity of `value` and `--from-file`:**

`value` (positional argument) and `--from-file` cannot be specified simultaneously. When `--from-file` is specified, `value` is omitted. If neither is specified, it is an error.

**Key-to-Component Mapping:**

After update, the restart commands for the affected components are displayed. The mapping of each key to component is as follows:

| Key | Type | Component |
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
| `NODE_BIN_PACKING_ENABLED` | bool | dispatcher |
| `FAIR_SHARE_WINDOW_DAYS` | int | dispatcher, submit-api |
| `USAGE_RETENTION_DAYS` | int | dispatcher |
| `CPU_LIMIT_BUFFER_MULTIPLIER` | float | dispatcher |
| `RESOURCE_FLAVORS` | json | dispatcher, watcher, submit-api |
| `DEFAULT_FLAVOR` | string | submit-api |
| `NODE_RESOURCE_SYNC_INTERVAL_SEC` | int | watcher |
| `WATCHER_K8S_LIST_PAGE_SIZE` | int | watcher |
| `WATCHER_DISPATCH_GRACE_SEC` | int | watcher |
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
| `POSTGRES_HOST` | Changes to DB connection require infrastructure work |
| `POSTGRES_PORT` | Changes to DB connection require infrastructure work |
| `POSTGRES_DB` | Changes to DB connection require infrastructure work |
| `POSTGRES_USER` | Changes to DB connection require infrastructure work |
| `POSTGRES_PASSWORD` | Changes to DB connection require infrastructure work |

#### `cjobctl config dump`

Outputs the contents of the `cjob-config` ConfigMap to standard output in clean YAML format that can be applied with `kubectl apply -f`. Used for backup or applying to another environment.

Management fields (`managedFields`, `resourceVersion`, `uid`, `creationTimestamp`, `kubectl.kubernetes.io/*` under `annotations`) are stripped.

```bash
$ cjobctl config dump > cjob-config-backup.yaml

# Restore
$ kubectl apply -f cjob-config-backup.yaml
```

### 5.6 CLI Binary Distribution

| Command | Overview | Target |
|---|---|---|
| `cjobctl cli list` | Display the list of registered versions on PVC | K8s Pod + PVC |
| `cjobctl cli deploy --binary <path> --version <version> [--release]` | Place the CLI binary on PVC | K8s Pod + PVC |
| `cjobctl cli remove <version>...` | Delete the binaries of the specified versions on PVC (multiple specifiable) | K8s Pod + PVC |
| `cjobctl cli set-latest <version>` | Change the latest version pointer | K8s Pod + PVC |

All subcommands manipulate the PVC using the pattern of a temporary Pod (busybox) + `kubectl exec`. The temporary Pod uses a minimal image (`busybox`) and mounts the `cli-binary` PVC at `/cli-binary`.

For usage examples, refer to [deployment.md](../deployment.md) §4.1 and [operations.md](../operations.md) §8.

#### `cjobctl cli list`

Displays the list of registered versions from the directory structure on the PVC.

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
4. Sort in descending semver order and display with the latest marker
5. Delete the temporary Pod

#### `cjobctl cli deploy`

Internal processing:
1. Start a temporary Pod with the `cli-binary` PVC (ReadWriteMany) mounted using `kubectl run`
2. Copy the binary into the temporary Pod at `/cli-binary/<version>/cjob` using `kubectl cp`
3. Run `chmod +x` inside the temporary Pod
4. Only when the `--release` option is specified, update the `latest` file
5. Delete the temporary Pod

`--release` cannot be combined with pre-release versions (versions whose string contains `-`). Pre-release determination is based on whether the version string contains `-`.

```bash
# Just place the binary (latest is not updated)
$ cjobctl cli deploy --binary ./cjob --version 1.3.0
Deployed v1.3.0 (latest unchanged: 1.2.0)

# Place the binary and update latest
$ cjobctl cli deploy --binary ./cjob --version 1.3.0 --release
Deployed v1.3.0 (latest updated)

# Beta version placement (--release cannot be used)
$ cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1
Deployed v1.3.1-beta.1 (latest unchanged: 1.3.0)

$ cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1 --release
Error: Cannot use --release with pre-release version 1.3.1-beta.1.
```

#### `cjobctl cli set-latest`

Changes the `latest` file on the PVC to the specified version. Does not place the binary. Used when latest is accidentally updated or when rolling back from a problematic version.

```bash
# Change latest to 1.2.0
$ cjobctl cli set-latest 1.2.0
Latest updated to v1.2.0.

# Non-existent versions are an error
$ cjobctl cli set-latest 9.9.9
Error: Version 9.9.9 not found on PVC. Deploy it first.

# Pre-release versions cannot be specified
$ cjobctl cli set-latest 1.3.0-beta.1
Error: Cannot set pre-release version 1.3.0-beta.1 as latest.
```

Internal processing:
1. Start a temporary Pod
2. Check whether the directory of the specified version exists
3. Update the latest file with `echo "<version>" > /cli-binary/latest`
4. Delete the temporary Pod

#### `cjobctl cli remove`

Deletes the binary directories of the specified versions on the PVC.

```bash
# Delete a single version
$ cjobctl cli remove 1.1.0
Removed CLI v1.1.0.

# Delete multiple versions simultaneously
$ cjobctl cli remove 1.0.0 1.1.0
Removed 2 versions.

$ cjobctl cli remove 1.3.0
Error: Cannot remove version 1.3.0: it is the current latest.
```

Internal processing:
1. Start a temporary Pod
2. Get the latest version with `cat /cli-binary/latest`
3. Validate the specified versions (error if it is the latest, error if it does not exist)
4. Display the confirmation prompt
5. Delete with `rm -rf /cli-binary/<version>` for each version
6. Delete the temporary Pod

### 5.7 User Management

| Command | Overview | Target |
|---|---|---|
| `cjobctl user list [--enabled \| --disabled]` | List of user namespaces | K8s: Namespace |
| `cjobctl user enable --namespace <ns>...` | Enable CJob (multiple specifiable) | K8s: Namespace |
| `cjobctl user disable --namespace <ns>...` | Disable CJob (multiple specifiable) | K8s: Namespace |

User namespaces are identified as Namespaces with the `type=user` label. The username is obtained from each namespace's `cjob.io/username` annotation, and the enabled/disabled state from the value of the `cjob.io/user-namespace` label.

#### `cjobctl user list`

Lists all namespaces with the `type=user` label.

```
$ cjobctl user list
NAMESPACE          USERNAME       ENABLED
user-alice         alice          true
user-bob           bob            true
user-charlie       charlie        false
```

- `--enabled`: Display only namespaces whose `cjob.io/user-namespace` label value is `"true"`
- `--disabled`: Display only namespaces whose `cjob.io/user-namespace` label value is not `"true"`
- `--enabled` and `--disabled` are exclusive (cannot be specified simultaneously)

#### `cjobctl user enable`

Sets the `cjob.io/user-namespace: "true"` label on the specified namespaces. Multiple namespaces can be specified simultaneously.

All namespaces are pre-validated before execution; if a non-existent namespace or a namespace without the `type=user` label is included, an error is returned. No label changes are made until validation passes.

```bash
$ cjobctl user enable --namespace user-charlie
Enabled CJob for namespace 'user-charlie'.

$ cjobctl user enable --namespace user-alice user-bob
Enabled CJob for namespace 'user-alice'.
Enabled CJob for namespace 'user-bob'.
```

#### `cjobctl user disable`

Changes the value of the `cjob.io/user-namespace` label of the specified namespaces to `"false"`. Multiple namespaces can be specified simultaneously.

Pre-validation is the same as `enable` (existence check + `type=user` label verification).

```bash
$ cjobctl user disable --namespace user-bob
Disabled CJob for namespace 'user-bob'.

$ cjobctl user disable --namespace user-alice user-bob
Disabled CJob for namespace 'user-alice'.
Disabled CJob for namespace 'user-bob'.
```

### 5.8 DB Schema Management

| Command | Overview |
|---|---|
| `cjobctl db migrate` | Idempotent schema migration execution |

Uses `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS`, so it is safe to run multiple times.

### 5.9 System Management

| Command | Overview | Target |
|---|---|---|
| `cjobctl system stop [--yes]` | Safe shutdown of the CJob system | DB + K8s: Deployment |
| `cjobctl system start [--submit-api-replicas <n>]` | Start the CJob system | K8s: Deployment |
| `cjobctl system restart <component>` | Rolling restart of a component | K8s: Deployment |
| `cjobctl system status` | Pod list of cjob-system | K8s: Pod |
| `cjobctl system logs <component> [--tail <n>]` | Component logs | K8s: Pod |

#### `cjobctl system stop`

Safely shuts down the CJob system. Executed before maintenance or K8s cluster shutdown. PostgreSQL is not stopped.

Shutdown sequence:

1. Display the active job count and show the confirmation prompt (can be skipped with `--yes`). `QUEUED` / `DISPATCHING` / `DISPATCHED` / `RUNNING` / `HELD` are counted and displayed individually; if there is at least one `HELD`, also display that "it will remain HELD even after restart"
2. Scale Submit API to replicas=0 to block new job submissions
3. Scale Dispatcher to replicas=0 to prevent re-dispatch of jobs
4. Scale Watcher to replicas=0 to prevent DB state overwrites
5. Update the job states in DB:
   - DISPATCHING → QUEUED (reset `retry_after = NULL`, `retry_count = 0`)
   - DISPATCHED → QUEUED
   - RUNNING → FAILED (`last_error = 'system shutdown'`, `finished_at = NOW()`)
   - QUEUED → unchanged
   - HELD → unchanged (remains HELD after restart until the user runs `cjob release`)
6. Delete K8s Jobs (with the `cjob.io/job-id` label) in all user namespaces with `propagationPolicy=Background`

The `cjob.io/user-namespace` label of the namespace is not changed. User access permissions are preserved across restart.

Jobs reverted to QUEUED will be automatically re-dispatched by the Dispatcher after the system starts. The DISPATCHING reset is equivalent to the Dispatcher's startup initialization ([dispatcher.md](dispatcher.md) §2.6).

```bash
$ cjobctl system stop
Active jobs: 16 (QUEUED: 8, DISPATCHING: 1, DISPATCHED: 2, RUNNING: 4, HELD: 1)
This will:
  - Scale down submit-api, dispatcher, watcher to 0 replicas
  - Revert 3 DISPATCHING/DISPATCHED job(s) to QUEUED
  - Fail 4 RUNNING job(s) (last_error: system shutdown)
  - Delete K8s Jobs in all user namespaces
  - 11 QUEUED job(s) will be re-dispatched on next start
  - 1 HELD job(s) will remain held (use cjob release to resume)
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

Starts the CJob system. Scales up each Deployment to the default replica count.

- Dispatcher: 1
- Watcher: 1
- Submit API: 2 (can be changed with `--submit-api-replicas`)

```bash
$ cjobctl system start
Scaled up dispatcher to 1 replica(s).
Scaled up watcher to 1 replica(s).
Scaled up submit-api to 2 replica(s).
CJob system started. Use 'cjobctl system status' to check pod status.
```

#### `cjobctl system restart`

Performs a rolling restart of the Deployment of the specified component. This is equivalent to `kubectl rollout restart`, triggering a K8s rolling update by setting the current time on the Pod template's `kubectl.kubernetes.io/restartedAt` annotation.

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
    ├── main.rs            # Clap definition + command dispatch
    ├── config.rs          # Configuration file loading
    ├── db.rs              # port-forward auto-start + DB connection
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

For the SQL queries executed by each command, refer to the corresponding files under `ctl/src/cmd/`.

## 8. Differences from the cjob CLI

| | cjob (user CLI) | cjobctl (admin CLI) |
|---|---|---|
| Target users | General users | Cluster administrators |
| Execution environment | User Pod inside K8s cluster | Administrator's local PC |
| Communication target | Submit API (HTTP) | PostgreSQL (direct) + K8s API |
| Authentication | ServiceAccount JWT | kubeconfig + DB password |
| Operation scope | Only jobs in own namespace | All namespaces |
| Distribution method | `cjob update` (via Submit API) | Build from source |
