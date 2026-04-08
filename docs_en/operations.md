> *This document was auto-translated from the [Japanese original](../docs/operations.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Operations Guide

Administrative operations are performed using the `cjobctl` CLI. For setup, see [Build Instructions](build.md).

## 1. Checking DB State

### 1.1 Connecting to PostgreSQL

For ad-hoc queries, you can connect directly to PostgreSQL.

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob
```

You can also execute directly using the `-c` option.

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "<SQL>"
```

### 1.2 Checking Job List

```bash
# Overview of all jobs
cjobctl jobs list

# Filter by namespace
cjobctl jobs list --namespace user-alice

# Filter by status
cjobctl jobs list --status RUNNING

# Detailed display of individual job
cjobctl jobs status --namespace user-alice --job-id 42

# Job count by namespace × status (pivot table)
cjobctl jobs summary
```

### 1.3 Checking Cumulative Resource Consumption

Displays daily consumption, 7-day window aggregation, and DRF dominant share all at once.

```bash
cjobctl usage list
```

### 1.4 Checking Job Counters

```bash
cjobctl jobs counters
```

### 1.5 Checking Stalled Jobs

Check jobs that have been in DISPATCHED state for an extended period (targets for gap filling).

```bash
cjobctl jobs stalled
```

### 1.6 Remaining Time for RUNNING Jobs

```bash
cjobctl jobs remaining
```

### 1.7 Checking Cluster Resource Totals

Check resource information automatically obtained from K8s nodes by the Watcher.

```bash
cjobctl cluster resources
```

The following 3 sections are displayed:

- **Node Resources**: Per-node allocatable (CPU / Memory / GPU) and last update time
- **Cluster Totals**: Total across all nodes. Used for Dispatcher's DRF normalization
- **Max Node Allocatable**: Maximum node allocatable value per flavor. For Submit API resource excess rejection, the effective upper limit is `min(max node allocatable, nominalQuota)` combined with nominalQuota

Node additions and removals are automatically reflected by the Watcher at `NODE_RESOURCE_SYNC_INTERVAL_SEC` (default 300 seconds) intervals. No manual updates are needed.

If the table is empty, it indicates the Watcher has not started or no target nodes exist. Verify that compute nodes have the `cjob.io/flavor=<flavor-name>` label (see [deployment.md](deployment.md) §16).

### 1.8 Checking ClusterQueue nominalQuota

Displays the current nominalQuota set on the Kueue ClusterQueue per ResourceFlavor. If `lendingLimit` is configured, its value is also shown.

```bash
cjobctl cluster show-quota
```

### 1.9 Updating ClusterQueue nominalQuota

Specify the target ResourceFlavor with `--flavor` (required). Specify resources to update with `--cpu`, `--memory`, `--gpu` (at least one required).

```bash
# Update CPU and memory for cpu
cjobctl cluster set-quota --flavor cpu --cpu 256 --memory 1000Gi

# Update only CPU for cpu (memory retains current value)
cjobctl cluster set-quota --flavor cpu --cpu 128

# Update GPU for gpu
cjobctl cluster set-quota --flavor gpu --gpu 4

# Update CPU, memory, and GPU for gpu at once
cjobctl cluster set-quota --flavor gpu --cpu 64 --memory 500Gi --gpu 4

# Remove GPU quota for cpu
cjobctl cluster set-quota --flavor cpu --gpu 0
```

Specified values are validated against the allocatable total from the `node_resources` table.

- **Exceeds allocatable** → Aborts with error. Specifying `--force` allows application with a warning (e.g., when pre-setting quota just before adding nodes)
- **Extremely small value** (less than 10% of allocatable) → Displays a warning but application is allowed

Post-update verification can also be done with:

```bash
kubectl get clusterqueue cjob-cluster-queue -o jsonpath='{range .spec.resourceGroups[*].flavors[*].resources[*]}name={.name} nominalQuota={.nominalQuota}{"\n"}{end}'
```

## 2. Checking Component Status

### 2.1 Pod Status

```bash
cjobctl system status
```

### 2.2 Checking Logs

```bash
# Dispatcher (default: last 50 lines)
cjobctl system logs dispatcher

# Specify number of lines
cjobctl system logs watcher --tail 100

# Submit API
cjobctl system logs submit-api
```

### 2.3 Checking and Modifying ConfigMap

```bash
# List current settings
cjobctl config show

# Change a setting
cjobctl config set DISPATCH_BATCH_SIZE 100

# Change a JSON value (read from file)
cjobctl config set RESOURCE_FLAVORS --from-file flavors.json

# Backup current ConfigMap as YAML
cjobctl config dump > cjob-config-backup.yaml

# Restore from backup
kubectl apply -f cjob-config-backup.yaml
```

`config set` displays a confirmation prompt (`[y/N]`) before making changes. Can be skipped with `--yes`. After update, the restart commands for affected components are displayed; follow them to execute `cjobctl system restart`.

## 3. Namespace Weight Management

Manage the fair sharing weight for each namespace. Namespaces with higher weight receive more resources fairly.

Namespaces without rows in the table are treated as having default weight = 1.

```bash
# Current weight list
cjobctl weight list

# Set weight for a specific namespace
cjobctl weight set user-alice 2

# Reset weight to default (1)
cjobctl weight reset user-alice
```

### Granting Exclusive Cluster Access to a Specific User

Based on K8s namespace labels (`cjob.io/user-namespace=true`), sets weight = 0 (dispatch prohibited) for all namespaces except the designated one.

```bash
# Grant exclusive access to user-alice
cjobctl weight exclusive user-alice

# Release exclusive access (reset all weights to default)
cjobctl weight exclusive --release
```

If a new namespace is created during exclusive access, re-run the exclusive command to set weight = 0 for the additions.

## 3.1 Flavor DRF Weight Management

Manage the DRF contribution weight (drf_weight) per flavor. Set larger values for precious resources like GPU and smaller values for lower-spec flavors.

Default is 1.0 (uniform across all flavors). If the `flavor_quotas` table has no rows, wait until the Watcher syncs from the ClusterQueue.

```bash
# Check current DRF weights (DRF Weight column in Per-Flavor Totals)
cjobctl cluster resources

# Set GPU flavor weight to 2.0
cjobctl cluster set-drf-weight gpu-a100 2.0

# Set lower-spec flavor weight to 0.5
cjobctl cluster set-drf-weight cpu-slow 0.5

# Reset to default (1.0)
cjobctl cluster set-drf-weight gpu-a100 1.0
```

## 4. Manual Reset of Cumulative Resource Consumption

```bash
# Reset for a specific namespace
cjobctl usage reset --namespace user-alice

# Reset for all namespaces
cjobctl usage reset --all
```

## 5. User Access Control

Job submission is controlled by the value of the namespace label `cjob.io/user-namespace`. This label is referenced by NetworkPolicy, and communication to the Submit API is blocked from namespaces where the label value is not `"true"`.

### 5.1 Checking User List

```bash
# List all user namespaces
cjobctl user list

# Show only enabled users
cjobctl user list --enabled

# Show only disabled users
cjobctl user list --disabled
```

### 5.2 Suspending User Access

```bash
# Disable a single namespace
cjobctl user disable --namespace user-bob

# Disable multiple namespaces at once
cjobctl user disable --namespace user-alice user-bob

# Cancel running jobs if necessary
cjobctl jobs cancel --namespace <namespace> --all
```

Even after disabling, jobs already in QUEUED / DISPATCHED / RUNNING state continue to run. To completely stop them, cancel all active jobs in the namespace with `cjobctl jobs cancel --namespace <namespace> --all`.

### 5.3 Resuming User Access

```bash
# Enable a single namespace
cjobctl user enable --namespace user-charlie

# Enable multiple namespaces at once
cjobctl user enable --namespace user-alice user-bob
```

## 6. Updating DB Schema

When adding new tables or columns during version upgrades. Can be executed idempotently using `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`.

```bash
cjobctl db migrate
```

## 7. Adding Compute Nodes to an Existing Flavor

### 7.1 Applying Labels and Taints to Nodes

Apply the following labels and taints to new nodes. The taint value must match `JOB_NODE_TAINT` in ConfigMap `cjob-config` (default: `role=computing:NoSchedule`).

```bash
kubectl label node <node-name> cjob.io/flavor=<flavor-name>
kubectl taint node <node-name> role=computing:NoSchedule
```

The label must match the `label_selector` of the corresponding flavor defined in `RESOURCE_FLAVORS` in ConfigMap `cjob-config`. All flavors use the common key `cjob.io/flavor` with the flavor name as the value.

```bash
# Check current settings (RESOURCE_FLAVORS, JOB_NODE_TAINT)
cjobctl config show
```

This label is referenced in the following 2 places:

| Reference | Purpose |
|---|---|
| Kueue ResourceFlavor (`nodeLabels`) | Schedule Job Pods only to labeled nodes |
| Watcher (`label_selector` in `RESOURCE_FLAVORS`) | Sync allocatable resources of labeled nodes to DB |

Taints prevent non-job Pods from being scheduled on compute nodes. The ConfigMap `JOB_NODE_TAINT`, Kueue ResourceFlavor `nodeTaints`, and node Taint must all have the same value. If `JOB_NODE_TAINT` is an empty string, no taint is applied.

### 7.2 Verifying Resource Information Sync

The Watcher automatically detects nodes and updates the `node_resources` table at `NODE_RESOURCE_SYNC_INTERVAL_SEC` (default 300 seconds) intervals.

```bash
# Verify the node has been recognized
cjobctl cluster resources
```

### 7.3 Updating ClusterQueue nominalQuota

When cluster total resources increase due to node addition, update the ClusterQueue nominalQuota.

```bash
# Check current quota
cjobctl cluster show-quota

# Update to match new totals (specify flavor)
cjobctl cluster set-quota --flavor cpu --cpu <new-total> --memory <new-total>
```

Use `--force` if you want to set the quota before the Watcher sync completes.

```bash
cjobctl cluster set-quota --flavor cpu --cpu <new-total> --memory <new-total> --force
```

## 8. Adding a New Flavor

When adding nodes that don't belong to an existing flavor (different CPU architecture, different GPU model, etc.), a new flavor must be created.

### 8.1 Apply Labels and Taints to Nodes

```bash
kubectl label node <node-name> cjob.io/flavor=<flavor-name>
kubectl taint node <node-name> role=computing:NoSchedule
```

For operations without taints (when sharing nodes with other workloads), do not apply taints.

### 8.2 Create Kueue ResourceFlavor

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: <flavor-name>         # Must match the flavor value in DB
spec:
  nodeLabels:
    cjob.io/flavor: "<flavor-name>"    # Must match the label applied in §8.1
  nodeTaints:               # Omit if not using taints
    - key: "role"
      value: "computing"
      effect: "NoSchedule"
  tolerations:              # Omit if not using taints
    - key: "role"
      operator: "Equal"
      value: "computing"
      effect: "NoSchedule"
```

When using taints, the ConfigMap `JOB_NODE_TAINT`, ResourceFlavor `nodeTaints`, and node taint must all have the same value.

Save the above YAML to a file and apply it.

```bash
kubectl apply -f <flavor-name>-resourceflavor.yaml
```

### 8.3 Add Flavor to ClusterQueue

```bash
kubectl edit clusterqueue cjob-cluster-queue
```

Add a new flavor entry to `spec.resourceGroups[0].flavors`. For flavors without GPU resources, set the `nvidia.com/gpu` nominalQuota to `"0"`. To protect other flavors' resources, set `lendingLimit: "0"`.

### 8.4 Add Definition to ConfigMap `RESOURCE_FLAVORS`

```bash
cjobctl config set RESOURCE_FLAVORS --from-file flavors.json
```

Add the new flavor definition to the `RESOURCE_FLAVORS` JSON array. For flavors with GPU, specify `gpu_resource_name`.

```json
[
  {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
  {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"},
  {"name": "<new-flavor-name>", "label_selector": "cjob.io/flavor=<new-flavor-name>"}
]
```

### 8.5 Restart Components

```bash
cjobctl system restart submit-api
cjobctl system restart dispatcher
cjobctl system restart watcher
```

### 8.6 Verification

```bash
# Verify node sync (reflected in the next sync cycle)
cjobctl cluster resources

# Verify nominalQuota
cjobctl cluster show-quota

# Verify job submission
cjob add --flavor <flavor-name> -- echo hello
```

## 9. CLI Binary Management

Procedure for building and deploying a new version of the `cjob` CLI binary to PVC. After deployment, users can self-update with `cjob update`.

### 9.1 Checking Registered Versions

```bash
cjobctl cli list
```

Output example:

```
VERSION            LATEST
1.3.0-beta.1
1.3.0              ← latest
1.2.0
1.1.0
```

### 9.2 Deploying a Stable Release

```bash
# 1. Build CLI binary (see build.md §3 for build environment setup)
cargo build --release --target x86_64-unknown-linux-musl --manifest-path cli/Cargo.toml

# 2. Deploy binary to PVC (latest is not updated)
cjobctl cli deploy --binary ./cli/target/x86_64-unknown-linux-musl/release/cjob --version <version>

# 3. Update latest to publish to users
cjobctl cli deploy --binary ./cli/target/x86_64-unknown-linux-musl/release/cjob --version <version> --release
```

Without `--release`, the binary is placed on PVC but `latest` is not updated. After verification, either redeploy with `--release` or change latest with `cjobctl cli set-latest`.

### 9.3 Deploying a Beta Version

Beta versions (those containing `-` in the version string) cannot use `--release`. Since latest is not changed, users running `cjob update` will remain on the stable version.

```bash
cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1
```

### 9.4 Changing the Latest Version

Changes latest for an already-deployed version. Used when latest was accidentally updated or when rolling back from a problematic version.

```bash
# Change latest to 1.2.0 (rollback)
cjobctl cli set-latest 1.2.0
```

Pre-release versions cannot be set as latest.

### 9.5 Post-Deployment Verification

```bash
# Verify the latest version is returned by Submit API's /v1/cli/version endpoint
kubectl exec -it -n cjob-system deploy/submit-api -- curl -s http://localhost:8080/v1/cli/version
```

### 9.6 Removing Old Versions

```bash
# Remove a single version
cjobctl cli remove 1.1.0

# Remove multiple versions at once
cjobctl cli remove 1.0.0 1.1.0
```

The version designated as `latest` cannot be removed. A confirmation prompt is displayed before removal.

### 9.7 Internal Processing Details

`cjobctl cli deploy` automatically performs the following (see [cjobctl.md](architecture/cjobctl.md) §5.6):

1. Start a temporary Pod (busybox) with `kubectl run` mounting the `cli-binary` PVC
2. Copy the binary to `/cli-binary/<version>/cjob` with `kubectl cp`
3. Execute `chmod +x` inside the temporary Pod
4. Update the latest file only when `--release` is specified
5. Delete the temporary Pod

## 10. System Shutdown and Startup

Safely shut down the CJob system before maintenance or K8s cluster shutdown. PostgreSQL is not stopped (for data preservation).

### 10.1 System Shutdown

```bash
cjobctl system stop
```

The following processes are executed in order:

1. Scale down Submit API to replicas=0 (block new job submissions)
2. Scale down Dispatcher to replicas=0 (prevent re-dispatch)
3. Scale down Watcher to replicas=0 (prevent DB state overwrites)
4. Update job states in DB:
   - DISPATCHING / DISPATCHED → Revert to QUEUED
   - RUNNING → FAILED (`last_error: system shutdown`)
   - QUEUED → No change (automatically re-dispatched after startup)
   - HELD → No change (remains held until user runs `cjob release`)
5. Delete K8s Jobs in all user namespaces

The user's `cjob.io/user-namespace` label is not changed. Access permissions are preserved across shutdown.

To skip the confirmation prompt:

```bash
cjobctl system stop --yes
```

### 10.2 System Startup

```bash
cjobctl system start
```

Scales up Dispatcher (1), Watcher (1), and Submit API (2). Jobs that remained in QUEUED state are automatically re-dispatched by the Dispatcher.

To change Submit API replicas:

```bash
cjobctl system start --submit-api-replicas 3
```

Post-startup verification:

```bash
cjobctl system status
```

### 10.3 When K8s Cluster Shutdown Is Involved

1. Stop CJob with `cjobctl system stop`
2. Shut down the K8s cluster
3. Start the K8s cluster
4. Wait for the PostgreSQL Pod to become Ready
5. Start CJob with `cjobctl system start`

### 10.4 Component Rolling Restart

Used to apply component image updates or configuration changes (after `cjobctl config set`).

```bash
# Restart a single component
cjobctl system restart dispatcher
cjobctl system restart watcher
cjobctl system restart submit-api
```

Executes processing equivalent to `kubectl rollout restart`. Pods are replaced sequentially, so for Submit API (replicas >= 2), updates can be applied without downtime.

## 11. Parameter Tuning

Key parameters for adjusting system behavior are managed via ConfigMap (see §2.3). After changes, affected components must be restarted.

### 11.1 Parameter List and Design Rationale

For the complete parameter list, relationships between layers, and design rationale, see [Resource Design](architecture/resources.md) §2.

### 11.2 Scheduling Adjustments

For details and tuning guidelines on Dispatcher job dispatch order, frequency, and fairness parameters, see [Dispatcher Design](architecture/dispatcher.md) §1. Key parameters:

| Parameter | Tuning Purpose |
|---|---|
| `DISPATCH_BUDGET_PER_NAMESPACE` | Upper limit on concurrent active jobs per namespace |
| `DISPATCH_BATCH_SIZE` | Upper limit on total dispatches per cycle |
| `DISPATCH_FETCH_MULTIPLIER` | SQL candidate fetch multiplier (fetches `DISPATCH_BATCH_SIZE × multiplier`, then narrows down to `DISPATCH_BATCH_SIZE` after filtering) |
| `DISPATCH_ROUND_SIZE` | Balance control between round-robin and DRF (consumption-based fairness) |
| `FAIR_SHARE_WINDOW_DAYS` | Number of days in the DRF consumption aggregation window |
| `USAGE_RETENTION_DAYS` | Retention days for `namespace_daily_usage` (independent of `FAIR_SHARE_WINDOW_DAYS`) |

### 11.3 Resource Limit Adjustments

For cluster resource limit settings (ResourceQuota, ClusterQueue nominalQuota, etc.) and adjustment methods, see [Resource Design](architecture/resources.md) §1. For ClusterQueue nominalQuota update procedures, see §7.3 of this guide.

## Appendix: Source Code Reference

For details on SQL queries executed by each command, refer to the source code under `ctl/src/cmd/`.

| Source File | Corresponding Command |
|---|---|
| `ctl/src/cmd/jobs.rs` | `cjobctl jobs` subcommands |
| `ctl/src/cmd/usage.rs` | `cjobctl usage list / reset` |
| `ctl/src/cmd/weight.rs` | `cjobctl weight` subcommands |
| `ctl/src/cmd/cluster.rs` | `cjobctl cluster` subcommands |
| `ctl/src/cmd/cli/deploy.rs` | `cjobctl cli deploy` |
| `ctl/src/cmd/cli/list.rs` | `cjobctl cli list` |
| `ctl/src/cmd/cli/remove.rs` | `cjobctl cli remove` |
| `ctl/src/cmd/cli/set_latest.rs` | `cjobctl cli set-latest` |
| `ctl/src/cmd/config/show.rs` | `cjobctl config show` |
| `ctl/src/cmd/config/set.rs` | `cjobctl config set` |
| `ctl/src/cmd/config/dump.rs` | `cjobctl config dump` |
| `ctl/src/cmd/user.rs` | `cjobctl user` subcommands |
| `ctl/src/cmd/system/stop.rs` | `cjobctl system stop` |
| `ctl/src/cmd/system/start.rs` | `cjobctl system start` |
| `ctl/src/cmd/system/restart.rs` | `cjobctl system restart` |
| `ctl/src/cmd/system/status.rs` | `cjobctl system status` |
| `ctl/src/cmd/system/logs.rs` | `cjobctl system logs` |
| `ctl/src/cmd/db_migrate.rs` | `cjobctl db migrate` |
