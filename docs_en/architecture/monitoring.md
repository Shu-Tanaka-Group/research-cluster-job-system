> *This document was auto-translated from the [Japanese original](../../docs/architecture/monitoring.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Monitoring Design

## 1. Overview

A Grafana dashboard is provided for users to check the utilization status of the CJob cluster.

**Dashboard purpose**: Enable users to determine at a glance "Is the cluster busy right now? Should I submit a job or wait?"

**Data sources**:
- **Prometheus**: Node/Pod metrics, Kueue metrics, CJob application metrics
- **PostgreSQL**: CJob DB (job status, actual wait times)

## 2. Prerequisites

### 2.1 Enabling Kueue ClusterQueue Resource Metrics

In Kueue v0.16.4, ClusterQueue resource usage metrics (`kueue_cluster_queue_resource_usage` / `kueue_cluster_queue_nominal_quota`) are disabled by default. These are required for the CPU/GPU utilization gauges in the dashboard, so they must be enabled.

Add the following to `controller_manager_config.yaml` in the `kueue-manager-config` ConfigMap:

```yaml
metrics:
  enableClusterQueueResources: true
```

After the change, restart the kueue-controller-manager Pod:

```bash
kubectl rollout restart deployment kueue-controller-manager -n kueue-system
```

### 2.2 Adding PostgreSQL Data Source to Grafana

Create a read-only user in CJob's PostgreSQL and register it as a data source in Grafana.

#### Creating a Read-Only User

```sql
CREATE ROLE grafana_reader LOGIN PASSWORD '<secure-password>';
GRANT CONNECT ON DATABASE cjob TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_reader;
```

#### Grafana Data Source Settings

| Field | Value |
|---|---|
| Name | CJob DB |
| Type | PostgreSQL |
| Host | `postgres.cjob-system.svc.cluster.local:5432` |
| Database | `cjob` |
| User | `grafana_reader` |
| TLS/SSL Mode | Configure according to your environment |

### 2.3 Verifying CJob Application Metrics Scraping

The Submit API and Watcher expose Prometheus counter metrics. Configure scraping via Prometheus Operator's ServiceMonitor / PodMonitor.

| Component | Port | Path | Notes |
|---|---|---|---|
| Submit API | 8080 | `/metrics` | Same port as the FastAPI application |
| Watcher | 9090 (`WATCHER_METRICS_PORT`) | `/metrics` | Served on a separate thread from the main loop |
| Dispatcher | 9090 (`DISPATCHER_METRICS_PORT`) | `/metrics` | Served on a separate thread from the main loop |

**Exposed metrics**:

| Metric name | Type | Labels | Instrumentation point | Description |
|---|---|---|---|---|
| `cjob_jobs_submitted_total` | Counter | — | Submit API | Number of jobs submitted (incremented on successful `submit_job` / `submit_sweep`) |
| `cjob_jobs_completed_total` | Counter | `status` | Watcher / Submit API | Number of completed jobs. `status` is `succeeded` / `failed` / `cancelled` |

Instrumentation points for `cjob_jobs_completed_total`:
- `succeeded` / `failed`: On status transition in Watcher's `reconcile_cycle()`
- `failed` (K8s Job disappeared): On job disappearance detection in Watcher's `reconcile_cycle()`
- `failed` (dispatch failure): In Dispatcher's `mark_failed()` on permanent errors or retry limit exceeded
- `cancelled`: On successful cancellation in Submit API's `cancel_single()`

These counters reset on process restart, but this has no impact on the dashboard since Prometheus's `increase()` / `rate()` functions handle resets automatically.

### 2.4 Verifying Kueue Prometheus Metrics Scraping

Verify that Kueue's Prometheus metrics are being scraped via ServiceMonitor or PodMonitor. When installing Kueue via Helm, a ServiceMonitor is created with `enablePrometheus: true`. For manifest-based installations, configure manually by referring to:

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/prometheus.yaml
```

## 3. Dashboard Design

### 3.1 Basic Information

| Field | Value |
|---|---|
| Title | CJob Cluster Status |
| Default time range | 6 hours |
| Auto-refresh interval | 30 seconds |
| Tags | `cjob` |

### 3.2 Panel Layout

#### Row 1: Summary (Traffic Light)

A summary row placed at the top of the dashboard. Allows understanding cluster status in 5 seconds.

| Panel | Type | DataSource | Content | Thresholds |
|---|---|---|---|---|
| CPU Reservation Rate | Gauge | Prometheus | CPU reservation rate for cpu flavor (total job requests / quota limit) | green < 60%, yellow < 85%, red >= 85% |
| CPU Sub Reservation Rate | Gauge | Prometheus | CPU reservation rate for cpu-sub flavor (total job requests / quota limit) | green < 60%, yellow < 85%, red >= 85% |
| GPU Reservation Rate | Gauge | Prometheus | GPU reservation rate for gpu flavor (total job requests / quota limit) | green < 50%, yellow < 75%, red >= 75% |
| Waiting Jobs | Stat | PostgreSQL | Number of waiting jobs in DB (QUEUED + DISPATCHING + DISPATCHED) | green < 5, yellow < 20, red >= 20 |
| Resource Allocation Wait (P50) | Stat | Prometheus | Median Kueue admission wait time (last 1 hour) | green < 60s, yellow < 300s, red >= 300s |

#### Row 2: Current Job Status

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Job Status Breakdown | Pie chart | PostgreSQL | Job status breakdown for the last 24 hours |
| Running Jobs | Stat | PostgreSQL | Total number of running jobs across all users |
| Success Rate (Last 24 hours) | Stat | Prometheus | SUCCEEDED / (SUCCEEDED + FAILED) |
| Active Users | Stat | PostgreSQL | Number of users (namespaces) with RUNNING jobs |
| Cluster Node Count | Stat | PostgreSQL | Number of records in the node_resources table |

#### Row 3: Queue Status

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Queue Usage by Flavor | Table | PostgreSQL | Number of running, waiting, and held jobs per flavor |
| Queue Job Count Over Time | Time series | Prometheus | Trend of running (admitted_active) and waiting (pending) jobs |
| Job Submission and Completion Over Time | Time series (line) | Prometheus | Submission and completion counts by time period |

#### Row 4: CPU Flavor Details

| Panel | Type | DataSource | Content |
|---|---|---|---|
| CPU Reservation Over Time | Time series | Prometheus | CPU reservation vs quota limit for cpu flavor |
| Memory Reservation Over Time | Time series | Prometheus | Memory reservation vs quota limit for cpu flavor (displayed in GiB) |

#### Row 5: GPU Flavor Details

| Panel | Type | DataSource | Content |
|---|---|---|---|
| GPU Reservation Over Time | Time series | Prometheus | GPU reservation vs quota limit for gpu flavor |
| GPU Node CPU Reservation | Time series | Prometheus | CPU reservation vs quota limit for gpu flavor |
| GPU Node Memory Reservation | Time series | Prometheus | Memory reservation vs quota limit for gpu flavor (displayed in GiB) |

#### Row 6: CPU Sub Flavor Details

| Panel | Type | DataSource | Content |
|---|---|---|---|
| CPU Reservation Over Time | Time series | Prometheus | CPU reservation vs quota limit for cpu-sub flavor |
| Memory Reservation Over Time | Time series | Prometheus | Memory reservation vs quota limit for cpu-sub flavor (displayed in GiB) |

#### Row 7: Wait Time Analysis

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Resource Allocation Wait Time Over Time (P50 / P95) | Time series | Prometheus | Percentile trend of Kueue admission wait time |
| Recent Job Wait Times | Table | PostgreSQL | Actual wait times for jobs in the last 6 hours (started_at - created_at) |

#### Row 8: Hourly Trends

| Panel | Type | DataSource | Content |
|---|---|---|---|
| Hourly Congestion (7-day average) | Bar chart | Prometheus | Average job submissions per hour from 0-23. Allows targeting less busy periods for job submission |

### 3.3 Metric Units (Kueue v0.16.4)

| Resource | Metric unit | Display unit | Conversion |
|---|---|---|---|
| CPU | Cores (float64) | Cores | No conversion needed |
| Memory | Bytes (float64) | GiB | `/ 1024 / 1024 / 1024` |
| GPU | Count (float64) | Count | No conversion needed |

In Kueue v0.16.4, values are converted using `resource.Quantity.AsApproximateFloat64()`, so memory is reported in bytes (e.g., `nominalQuota: "1000Gi"` → `1073741824000`).

Utilization gauges (Row 1) are ratios of usage / quota, so the numerator and denominator are in the same units and cancel out — no conversion needed.

## 4. Key Queries

### 4.1 PromQL

```promql
# Success rate (last 24 hours)
sum(increase(cjob_jobs_completed_total{status="succeeded"}[24h]))
/
sum(increase(cjob_jobs_completed_total{status=~"succeeded|failed"}[24h]))
* 100

# Job submission trend (10-minute intervals)
sum(increase(cjob_jobs_submitted_total[10m]))

# Job completion trend (10-minute intervals)
sum(increase(cjob_jobs_completed_total[10m]))

# Hourly congestion (7-day average, panel time range: 24h)
(
  sum(increase(cjob_jobs_submitted_total[1h]))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 1d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 2d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 3d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 4d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 5d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 6d)) or vector(0))
) / 7
```

```promql
# CPU utilization gauge
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}

# CPU Sub utilization gauge
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu-sub", resource="cpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu-sub", resource="cpu"}

# GPU utilization gauge
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="gpu-a100", resource="nvidia.com/gpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="gpu-a100", resource="nvidia.com/gpu"}

# Wait time P50 (last 1 hour)
histogram_quantile(0.5, rate(kueue_admission_wait_time_seconds_bucket{cluster_queue="cjob-cluster-queue"}[1h]))

# Wait time P95 (last 30 minutes)
histogram_quantile(0.95, rate(kueue_admission_wait_time_seconds_bucket{cluster_queue="cjob-cluster-queue"}[30m]))

# Number of running workloads
kueue_admitted_active_workloads{cluster_queue="cjob-cluster-queue"}

# Number of waiting workloads
sum(kueue_pending_workloads{cluster_queue="cjob-cluster-queue"})

# CPU usage (cores)
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}

# Memory usage (converted to GiB)
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="memory"} / 1024 / 1024 / 1024

# Memory quota limit (converted to GiB)
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="memory"} / 1024 / 1024 / 1024
```

### 4.2 SQL

```sql
-- Recent job wait times (last 6 hours)
SELECT
  namespace AS "User",
  job_id AS "Job ID",
  flavor AS "Flavor",
  cpu || ' CPU / ' || memory || ' / GPU ' || gpu AS "Resources",
  EXTRACT(EPOCH FROM (started_at - created_at))::int AS "Wait time (sec)",
  started_at AS "Start time"
FROM jobs
WHERE status IN ('RUNNING', 'SUCCEEDED', 'FAILED')
  AND started_at IS NOT NULL
  AND created_at >= NOW() - INTERVAL '6 hours'
ORDER BY started_at DESC
LIMIT 15;

-- Job status breakdown (last 24 hours)
SELECT
  CASE status
    WHEN 'QUEUED' THEN 'Waiting'
    WHEN 'DISPATCHING' THEN 'Dispatching'
    WHEN 'DISPATCHED' THEN 'Pending execution'
    WHEN 'RUNNING' THEN 'Running'
    WHEN 'HELD' THEN 'Held'
    WHEN 'SUCCEEDED' THEN 'Succeeded'
    WHEN 'FAILED' THEN 'Failed'
    WHEN 'CANCELLED' THEN 'Cancelled'
    WHEN 'DELETING' THEN 'Deleting'
    ELSE status
  END AS "Status",
  COUNT(*) AS "Count"
FROM jobs
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY status
ORDER BY
  CASE status
    WHEN 'RUNNING' THEN 1
    WHEN 'QUEUED' THEN 2
    WHEN 'HELD' THEN 3
    WHEN 'DISPATCHING' THEN 4
    WHEN 'DISPATCHED' THEN 5
    WHEN 'SUCCEEDED' THEN 6
    WHEN 'FAILED' THEN 7
    WHEN 'CANCELLED' THEN 8
    WHEN 'DELETING' THEN 9
  END;

-- Number of running jobs
SELECT COUNT(*) AS "Running" FROM jobs WHERE status = 'RUNNING';

-- Number of active users
SELECT COUNT(DISTINCT namespace) AS "User count" FROM jobs WHERE status = 'RUNNING';

-- Queue usage by flavor (no query changes needed when adding flavors)
SELECT
  flavor AS "Flavor",
  COUNT(*) FILTER (WHERE status = 'RUNNING') AS "Running",
  COUNT(*) FILTER (WHERE status IN ('QUEUED', 'DISPATCHING', 'DISPATCHED')) AS "Waiting",
  COUNT(*) FILTER (WHERE status = 'HELD') AS "Held"
FROM jobs
WHERE status IN ('RUNNING', 'QUEUED', 'DISPATCHING', 'DISPATCHED', 'HELD')
GROUP BY flavor
ORDER BY flavor;

-- Cluster node count
SELECT COUNT(*) AS "Node count" FROM node_resources;
```

## 5. Dashboard JSON

Place the Grafana import JSON file at `k8s/base/grafana/dashboard-user.json`. Deploy it by uploading the JSON file from `Dashboards > Import` in the Grafana UI.

When importing, the data source UIDs must be configured for your environment. Data source references in the JSON use the following variable names:

| Variable name | Data source |
|---|---|
| `${DS_PROMETHEUS}` | Prometheus |
| `${DS_CJOB_DB}` | CJob PostgreSQL |

## 6. Operational Notes

- Kueue metrics reset immediately after a controller restart, causing temporary data gaps. The impact is minimal if the dashboard time range is set appropriately.
- PostgreSQL queries make use of indexes (`idx_jobs_namespace_status`). Pay attention to query performance if a large number of jobs have accumulated (hundreds of thousands or more).
- The `node_resources` table is synced by the Watcher at 300-second intervals, so node additions/removals may take up to 5 minutes to be reflected.
